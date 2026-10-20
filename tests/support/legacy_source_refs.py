"""Plant ``source_refs`` the way a v0.20.2 vault stored them.

The write doors on main refuse an entry no producer writes: an object, a number, a boolean, null, a blank string, a nested
array and the text of a JSON object or array. A read-protection test still needs the stored shape, so it commits the string ids
the link writer would have linked, then this module writes the original entries into the row and into its revisions. That is
the store path, not a write door.

A v0.20.2 commit wrote the entries to the memory row (``metadata_json`` and ``value``) and to its creation revision
(``new_value``), as a restored backup of that release holds them. Revisions are append-only, so the plant lifts the update
trigger of ``memory_revisions`` for the length of one statement and puts it back from the SQL SQLite holds for it. Events keep the
string stand-in.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from psycopg.types.json import Jsonb

from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_source_fence import is_producer_source_ref, source_uuids_in_ref

REVISION_TRIGGER = "memory_revisions_append_only_update"


def needs_legacy_plant(refs: Sequence[object]) -> bool:
    """True when a door of main would refuse an entry of ``refs``, so the test has to plant it."""

    return any(not is_producer_source_ref(ref) for ref in refs)


def link_strings(refs: Sequence[object]) -> list[str]:
    """Strings the current commit door can store, and the first source id of each other entry.

    The link writer links the first source id of a ref. Passing that id as a string keeps the
    same link, and the plant step puts the original entry back. JSON text names no source id to
    the link writer, so it leaves no stand-in.
    """

    strings: list[str] = []
    for ref in refs:
        if is_producer_source_ref(ref):
            strings.append(str(ref))
            continue
        named = source_uuids_in_ref(ref)
        if named:
            strings.append(named[0])
    return strings


def _replace_standin(node: object, standin: list[str], original: list[object]) -> object:
    if isinstance(node, dict):
        replaced: dict[str, object] = {}
        for key, value in node.items():
            if key == "source_refs" and value == standin:
                replaced[key] = original
            else:
                replaced[key] = _replace_standin(value, standin, original)
        return replaced
    if isinstance(node, list):
        return [_replace_standin(item, standin, original) for item in node]
    return node


def _load(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def _dump(value: object) -> str:
    return json.dumps(value, default=str)


def plant_legacy_source_refs(store: Any, memory_id: str, refs: Sequence[object], standin: Sequence[str]) -> None:
    """Overwrite the commit's string stand-in with ``refs`` on this connection."""

    original = list(refs)
    standin_list = list(standin)
    conn = store.conn
    if isinstance(conn, sqlite3.Connection):
        _plant_sqlite(conn, memory_id, original, standin_list)
    else:
        _plant_postgres(conn, memory_id, original, standin_list)


def plant_legacy_source_metadata(store: Any, source_id: str, refs: Sequence[object], standin: Sequence[str]) -> None:
    """Overwrite the string stand-in in a source row an ingest stored."""

    original = list(refs)
    standin_list = list(standin)
    conn = store.conn
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute("SELECT metadata_json FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise AssertionError(f"source {source_id} was not stored")
        metadata = _replace_standin(_load(_field(row, "metadata_json", 0)), standin_list, original)
        conn.execute("UPDATE sources SET metadata_json = ? WHERE id = ?", (_dump(metadata), source_id))
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata_json FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise AssertionError(f"source {source_id} was not stored")
            metadata = _replace_standin(_load(row["metadata_json"]), standin_list, original)
            cur.execute(
                "UPDATE sources SET metadata_json = %s WHERE id = %s",
                (_jsonb(metadata), source_id),
            )


def _field(row: object, name: str, index: int) -> object:
    if isinstance(row, sqlite3.Row):
        return row[name]
    if isinstance(row, dict):
        return row[name]
    return row[index]  # type: ignore[index]


def _plant_sqlite(conn: sqlite3.Connection, memory_id: str, original: list[object], standin: list[str]) -> None:
    row = conn.execute("SELECT metadata_json, value FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise AssertionError(f"memory {memory_id} was not stored")
    metadata = _replace_standin(_load(_field(row, "metadata_json", 0)), standin, original)
    value = _replace_standin(_load(_field(row, "value", 1)), standin, original)
    conn.execute(
        "UPDATE memories SET metadata_json = ?, value = ? WHERE id = ?",
        (_dump(metadata), _dump(value), memory_id),
    )
    _plant_sqlite_revisions(conn, memory_id, original, standin)


@contextmanager
def _revisions_open_to_update(conn: sqlite3.Connection) -> Iterator[None]:
    """``memory_revisions`` lets a row change for the length of the block, and holds again after it.

    The trigger is read from ``sqlite_master`` and created again from that SQL, so the guard that comes back is the one the
    schema defines.
    """

    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (REVISION_TRIGGER,)).fetchone()
    if row is None:
        raise AssertionError(f"{REVISION_TRIGGER} is not in the schema")
    definition = _field(row, "sql", 0)
    conn.execute(f"DROP TRIGGER {REVISION_TRIGGER}")
    try:
        yield
    finally:
        conn.execute(definition)
        restored = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (REVISION_TRIGGER,)).fetchone()
        assert restored is not None and _field(restored, "sql", 0) == definition


_REVISION_COLUMNS = ("previous_value", "new_value", "candidate", "metadata_json")


def _plant_sqlite_revisions(conn: sqlite3.Connection, memory_id: str, original: list[object], standin: list[str]) -> None:
    """The revisions of the memory carry the entries as the release that wrote them did, not only the memory row."""

    rows = conn.execute(
        f"SELECT id, {', '.join(_REVISION_COLUMNS)} FROM memory_revisions WHERE memory_id = ?", (memory_id,)
    ).fetchall()
    changes: list[tuple[str, dict[str, str]]] = []
    for row in rows:
        planted: dict[str, str] = {}
        for column in _REVISION_COLUMNS:
            raw = _field(row, column, 1 + _REVISION_COLUMNS.index(column))
            if raw is None:
                continue
            loaded = _load(raw)
            replaced = _replace_standin(loaded, standin, original)
            if replaced != loaded:
                planted[column] = _dump(replaced)
        if planted:
            changes.append((str(_field(row, "id", 0)), planted))
    if not changes:
        return
    with _revisions_open_to_update(conn):
        for revision_id, planted in changes:
            assignments = ", ".join(f"{column} = ?" for column in planted)
            conn.execute(f"UPDATE memory_revisions SET {assignments} WHERE id = ?", (*planted.values(), revision_id))


def _jsonb(value: object) -> Jsonb:
    return Jsonb(json_safe(value))


def _plant_postgres(conn: Any, memory_id: str, original: list[object], standin: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT metadata_json, value FROM memories WHERE id = %s", (memory_id,))
        row = cur.fetchone()
        if row is None:
            raise AssertionError(f"memory {memory_id} was not stored")
        cur.execute(
            "UPDATE memories SET metadata_json = %s, value = %s WHERE id = %s",
            (
                _jsonb(_replace_standin(_load(row["metadata_json"]), standin, original)),
                _jsonb(_replace_standin(_load(row["value"]), standin, original)),
                memory_id,
            ),
        )
