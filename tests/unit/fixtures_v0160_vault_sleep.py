# v0.16.0 apps/api/src/alicebot_api/vault_sleep.py, copied verbatim below this header.
# Tag v0.16.0 is 3491d77a7920d75818c40f86f8b663f95e6fd86e.
# This file last changed in 86d05b310b6315bd1e6dba546f126a55a9327410.
"""Offline sleep pass for ``alice-memory sleep``.

Writes a capped sidecar of source proposals. Import stays a source.
Commit stays a fact. Search is unchanged. Accept is a later commit.

Does not create memories, does not rewrite sources or committed facts,
and does not call consolidation. Counts and sidecar rows bind ``user_id``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from alicebot_api.session_briefing import COMMITTED_MEMORY_STATUSES
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

SLEEP_PROPOSAL_CAP = 8
SLEEP_PROPOSAL_FILENAME = "sleep_proposals.jsonl"
SLEEP_EXCERPT_MAX = 160
PROPOSED_STATUS = "proposed"

LIST_SOURCE_IDS_SQL = """
SELECT id
FROM sources
WHERE user_id = ?
  AND deleted_at IS NULL
ORDER BY captured_at ASC, id ASC
"""


class SleepError(ValueError):
    """A user-facing sleep failure with a static CLI error code."""


def sleep_proposals_path(db_path: Path) -> Path:
    """Sidecar JSONL next to the SQLite file."""

    return Path(db_path).expanduser().resolve().parent / SLEEP_PROPOSAL_FILENAME


def format_sleep_receipt(
    *,
    written: int,
    already_present: int,
    skipped_linked: int,
    cap: int,
) -> str:
    return "\n".join(
        (
            f"proposals written: {written}",
            f"already present: {already_present}",
            f"skipped as already linked: {skipped_linked}",
            f"cap: {cap}",
        )
    )


def load_sleep_proposals(path: Path) -> list[dict[str, object]]:
    """Read existing sidecar rows. Fail closed on a corrupt file."""

    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SleepError("sidecar could not be read") from exc
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SleepError("sidecar is invalid") from exc
        if not isinstance(parsed, dict):
            raise SleepError("sidecar is invalid")
        if "user_id" not in parsed or "source_id" not in parsed:
            raise SleepError("sidecar is invalid")
        rows.append(parsed)
    return rows


def run_local_vault_sleep(
    db_path: Path,
    *,
    user_id: UUID | str,
) -> str:
    """Propose unlinked imported sources into the sidecar. Cap, then stop."""

    if COMMITTED_MEMORY_STATUSES != ("active", "accepted"):
        raise RuntimeError(
            "sleep committed-fact filter is written for active and accepted only"
        )

    resolved = Path(db_path).expanduser().resolve()
    sidecar = sleep_proposals_path(resolved)
    existing = load_sleep_proposals(sidecar)

    with sqlite_user_connection(resolved, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        uid = store.user_id
        existing_ids = {
            str(row["source_id"])
            for row in existing
            if str(row.get("user_id") or "") == uid
        }
        written_rows: list[dict[str, object]] = []
        already_present = 0
        skipped_linked = 0
        for source_id in _list_source_ids(store):
            if _has_committed_fact(store, source_id):
                skipped_linked += 1
                continue
            if source_id in existing_ids:
                already_present += 1
                continue
            if len(existing_ids) + len(written_rows) >= SLEEP_PROPOSAL_CAP:
                continue
            written_rows.append(
                {
                    "excerpt": _source_excerpt(store, source_id),
                    "source_id": source_id,
                    "status": PROPOSED_STATUS,
                    "user_id": uid,
                }
            )

    if written_rows:
        try:
            _write_jsonl(sidecar, [*existing, *written_rows])
        except OSError as exc:
            raise SleepError("sidecar could not be written") from exc

    return format_sleep_receipt(
        written=len(written_rows),
        already_present=already_present,
        skipped_linked=skipped_linked,
        cap=SLEEP_PROPOSAL_CAP,
    )


def _list_source_ids(store: SQLiteVNextStore) -> list[str]:
    rows = store.conn.execute(LIST_SOURCE_IDS_SQL, (store.user_id,)).fetchall()
    ids: list[str] = []
    for row in rows:
        value = row["id"] if isinstance(row, dict) else row[0]
        ids.append(str(value))
    return ids


def _has_committed_fact(store: SQLiteVNextStore, source_id: str) -> bool:
    linked = store.list_memories_referencing_source(source_id=source_id)
    return any(str(row.get("status") or "") in COMMITTED_MEMORY_STATUSES for row in linked)


def _source_excerpt(store: SQLiteVNextStore, source_id: str) -> str:
    for chunk in store.list_source_chunks(source_id):
        text = chunk.get("text")
        if isinstance(text, str) and text.strip():
            return _short_excerpt(text)
    return ""


def _short_excerpt(text: str) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= SLEEP_EXCERPT_MAX:
        return flattened
    return flattened[:SLEEP_EXCERPT_MAX].rstrip()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        for row in rows
    )
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


__all__ = [
    "LIST_SOURCE_IDS_SQL",
    "PROPOSED_STATUS",
    "SLEEP_EXCERPT_MAX",
    "SLEEP_PROPOSAL_CAP",
    "SLEEP_PROPOSAL_FILENAME",
    "SleepError",
    "format_sleep_receipt",
    "load_sleep_proposals",
    "run_local_vault_sleep",
    "sleep_proposals_path",
]
