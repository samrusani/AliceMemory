"""The per-project scoping switch (spec 5.3).

The switch has three layers, strongest first:

1. ``ALICE_PROJECT_SCOPING`` (``on`` or ``off``) in the environment, when it is
   set to a valid value. A value that is neither is ignored.
2. The vault's ``project_scoping`` row in ``alice_schema_state``, written by
   ``alice-memory project scoping on|off``.
3. The release default, ``RELEASE_DEFAULT``.

Most hosts cannot be given an environment variable per entry, so the row is
persisted in the vault. ``alice_schema_state`` is not exported, so a restore
would silently return a switch set to ``off`` to the release default. Every
change therefore also appends one ``scoping.changed`` event, and the event
rides the export. ``alice-memory import`` applies the newest such event in the
file when the target vault has no ``project_scoping`` row. That is a
latest-wins read of one event type, not a fold. The event type does not start
with ``project.``, because the ``project.update_candidate_*`` events exist and a
prefix query would sweep both.

Nothing reads the switch to change a read or a write yet. This module only
stores it, reports it and carries it through a backup.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_repositories import EventStore

ScopingValue = Literal["on", "off"]
ScopingOrigin = Literal["environment", "vault", "default"]

SCOPING_ENV = "ALICE_PROJECT_SCOPING"
SCOPING_STATE_KEY = "project_scoping"
SCOPING_EVENT_TYPE = "scoping.changed"
SCOPING_EVENT_TARGET_TYPE = "setting"
#: Until the release that turns per-project memory on, the default is off, so
#: main behaves as the last release.
RELEASE_DEFAULT: ScopingValue = "off"
_UNSET = "unset"


def parse_scoping_value(text: object) -> ScopingValue | None:
    """``on`` or ``off`` (any case, surrounding space ignored), else ``None``."""

    if not isinstance(text, str):
        return None
    word = text.strip().lower()
    if word == "on":
        return "on"
    if word == "off":
        return "off"
    return None


@dataclass(frozen=True, slots=True)
class ScopingState:
    """The effective switch and where it came from."""

    value: ScopingValue
    origin: ScopingOrigin
    vault_value: ScopingValue | None
    environment_value: ScopingValue | None
    #: ``ALICE_PROJECT_SCOPING`` is set but is not ``on`` or ``off``.
    environment_ignored: bool

    @property
    def enabled(self) -> bool:
        return self.value == "on"


def resolve_scoping(*, environ: Mapping[str, str], vault_value: object) -> ScopingState:
    """Apply the precedence. The caller supplies the environment and the row."""

    raw_environment = environ.get(SCOPING_ENV)
    environment_value = parse_scoping_value(raw_environment)
    environment_ignored = bool(raw_environment) and environment_value is None
    stored = parse_scoping_value(vault_value)
    if environment_value is not None:
        return ScopingState(environment_value, "environment", stored, environment_value, False)
    if stored is not None:
        return ScopingState(stored, "vault", stored, None, environment_ignored)
    return ScopingState(RELEASE_DEFAULT, "default", None, None, environment_ignored)


@dataclass(frozen=True, slots=True)
class VaultSetting:
    """What the vault says. ``readable`` is false when the file could not be read."""

    value: ScopingValue | None
    readable: bool


def read_vault_setting(db_path: Path) -> VaultSetting:
    """Read the ``project_scoping`` row without writing anything.

    A missing vault file, a missing table and a missing row all mean no
    setting, and the file is not created. The read uses a read-only connection,
    so it leaves the database file as it found it.
    """

    from alicebot_api.onramp import _BackupError, _read_only_sqlite_connection

    if not db_path.is_file():
        return VaultSetting(None, True)
    try:
        connection = _read_only_sqlite_connection(db_path)
    except (_BackupError, sqlite3.Error, OSError):
        return VaultSetting(None, False)
    try:
        row = connection.execute(
            "SELECT value FROM alice_schema_state WHERE key = ?", (SCOPING_STATE_KEY,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return VaultSetting(None, True)
        return VaultSetting(None, False)
    except sqlite3.Error:
        return VaultSetting(None, False)
    finally:
        connection.close()
    return VaultSetting(parse_scoping_value(_row_value(row)), True)


def _row_value(row: object) -> object:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get("value")
    if isinstance(row, (tuple, list)) and row:
        return row[0]
    return None


@dataclass(frozen=True, slots=True)
class ScopingChange:
    changed: bool
    value: ScopingValue
    previous: ScopingValue | None


def set_vault_setting(
    connection: sqlite3.Connection, store: EventStore, value: ScopingValue
) -> ScopingChange:
    """Write the row and one ``scoping.changed`` event, in the caller's transaction.

    Setting the value the row already holds writes nothing and appends no event.
    The event holds the setting name, the new value and the previous value
    (``unset`` when there was none). It holds no path, URL or project id.
    """

    row = connection.execute(
        "SELECT value FROM alice_schema_state WHERE key = ?", (SCOPING_STATE_KEY,)
    ).fetchone()
    previous = parse_scoping_value(_row_value(row))
    if row is not None and previous == value:
        return ScopingChange(changed=False, value=value, previous=previous)
    connection.execute(
        """
        INSERT INTO alice_schema_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SCOPING_STATE_KEY, value),
    )
    append_event(
        store,
        event_type=SCOPING_EVENT_TYPE,
        actor_type="user",
        payload={
            "setting": SCOPING_STATE_KEY,
            "value": value,
            "previous": previous if previous is not None else _UNSET,
        },
        target_type=SCOPING_EVENT_TARGET_TYPE,
        target_id=SCOPING_STATE_KEY,
    )
    return ScopingChange(changed=True, value=value, previous=previous)


# ---------------------------------------------------------------------------
# Import step (spec 5.3)
# ---------------------------------------------------------------------------


def _event_time(value: object) -> datetime:
    floor = datetime.min.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return floor
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return floor
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _event_payload_value(record: Mapping[str, object]) -> ScopingValue | None:
    payload = record.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, Mapping):
        return None
    return parse_scoping_value(payload.get("value"))


def newest_scoping_event_value(records: Iterable[Mapping[str, object]]) -> ScopingValue | None:
    """The value of the newest ``scoping.changed`` event with a valid value.

    Newest is by ``occurred_at``, then by id, so the answer does not depend on
    the order of the file. An event whose payload holds no valid value is
    skipped.
    """

    best: tuple[datetime, str, str] | None = None
    best_value: ScopingValue | None = None
    for record in records:
        if record.get("event_type") != SCOPING_EVENT_TYPE:
            continue
        value = _event_payload_value(record)
        if value is None:
            continue
        key = (_event_time(record.get("occurred_at")), str(record.get("occurred_at")), str(record.get("id")))
        if best is None or key > best:
            best, best_value = key, value
    return best_value


@dataclass(frozen=True, slots=True)
class ImportedScoping:
    """What the import did about the switch."""

    event_value: ScopingValue
    #: True when the vault had no row and the import set it.
    applied: bool


def apply_imported_scoping(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> ImportedScoping | None:
    """Set the row from the file's newest event when the vault has no row.

    Returns ``None`` when the file has no usable event, so an ordinary import
    prints nothing about scoping. A vault that already has a row keeps it,
    whatever the event says.
    """

    value = newest_scoping_event_value(records)
    if value is None:
        return None
    existing = connection.execute(
        "SELECT 1 FROM alice_schema_state WHERE key = ?", (SCOPING_STATE_KEY,)
    ).fetchone()
    if existing is not None:
        return ImportedScoping(event_value=value, applied=False)
    connection.execute(
        "INSERT INTO alice_schema_state (key, value) VALUES (?, ?)",
        (SCOPING_STATE_KEY, value),
    )
    return ImportedScoping(event_value=value, applied=True)


def import_receipt_line(result: ImportedScoping) -> str:
    """The one receipt line: which value the import set, or left, and why."""

    if result.applied:
        return (
            f"project scoping: set to {result.event_value} from the newest "
            f"{SCOPING_EVENT_TYPE} event in the file (this vault had no setting)"
        )
    return (
        "project scoping: left as it is (this vault already has a setting; the newest "
        f"{SCOPING_EVENT_TYPE} event in the file says {result.event_value})"
    )


__all__ = [
    "ImportedScoping",
    "RELEASE_DEFAULT",
    "SCOPING_ENV",
    "SCOPING_EVENT_TARGET_TYPE",
    "SCOPING_EVENT_TYPE",
    "SCOPING_STATE_KEY",
    "ScopingChange",
    "ScopingOrigin",
    "ScopingState",
    "ScopingValue",
    "VaultSetting",
    "apply_imported_scoping",
    "import_receipt_line",
    "newest_scoping_event_value",
    "parse_scoping_value",
    "read_vault_setting",
    "resolve_scoping",
    "set_vault_setting",
]
