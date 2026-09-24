"""Offline sleep pass for ``alice-memory sleep``.

Writes a capped sidecar of source proposals. Import stays a source.
Commit stays a fact. Search is unchanged. Accept is a later commit.

Does not create memories, does not rewrite sources or committed facts,
and does not call consolidation. Counts and sidecar rows bind ``user_id``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from alicebot_api.legacy_credential_check import commit_door_secret_verdict
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
    sources_withheld: int,
    existing_rows_removed: int,
) -> str:
    """Receipt counts only. No excerpts, values, or source ids."""

    return "\n".join(
        (
            f"proposals written: {written}",
            f"already present: {already_present}",
            f"skipped as already linked: {skipped_linked}",
            f"cap: {cap}",
            f"sources withheld: {sources_withheld}",
            f"existing rows removed: {existing_rows_removed}",
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
        kept_rows: list[dict[str, object]] = []
        existing_rows_removed = 0
        for row in existing:
            if _row_refused(store, row, caller_id=uid):
                if str(row.get("user_id") or "") == uid:
                    existing_rows_removed += 1
                continue
            kept_rows.append(row)
        existing_ids = {
            str(row["source_id"])
            for row in kept_rows
            if str(row.get("user_id") or "") == uid
        }
        written_rows: list[dict[str, object]] = []
        already_present = 0
        skipped_linked = 0
        sources_withheld = 0
        for source_id in _list_source_ids(store):
            if _has_committed_fact(store, source_id):
                skipped_linked += 1
                continue
            if source_id in existing_ids:
                already_present += 1
                continue
            chunk = _first_chunk_text(store, source_id)
            excerpt = _short_excerpt(chunk) if chunk else ""
            if _text_refused(excerpt) or _text_refused(chunk):
                sources_withheld += 1
                continue
            if len(existing_ids) + len(written_rows) >= SLEEP_PROPOSAL_CAP:
                continue
            written_rows.append(
                {
                    "excerpt": excerpt,
                    "source_id": source_id,
                    "status": PROPOSED_STATUS,
                    "user_id": uid,
                }
            )

    dropped = len(existing) - len(kept_rows)
    if written_rows or dropped:
        try:
            _write_jsonl(sidecar, [*kept_rows, *written_rows])
        except OSError as exc:
            raise SleepError("sidecar could not be written") from exc
    else:
        try:
            _restrict_sidecar_mode(sidecar)
        except OSError as exc:
            raise SleepError("sidecar could not be written") from exc

    return format_sleep_receipt(
        written=len(written_rows),
        already_present=already_present,
        skipped_linked=skipped_linked,
        cap=SLEEP_PROPOSAL_CAP,
        sources_withheld=sources_withheld,
        existing_rows_removed=existing_rows_removed,
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


def _first_chunk_text(store: SQLiteVNextStore, source_id: str) -> str:
    for chunk in store.list_source_chunks(source_id):
        text = chunk.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _text_refused(text: object) -> bool:
    """True when the commit door would refuse this one string."""

    if not isinstance(text, str) or text == "":
        return False
    return commit_door_secret_verdict("", text) is not None


def _row_refused(store: SQLiteVNextStore, row: dict[str, object], *, caller_id: str) -> bool:
    """Caller rows: excerpt and the source's first chunk. Other users: excerpt."""

    if _text_refused(row.get("excerpt")):
        return True
    if str(row.get("user_id") or "") != caller_id:
        return False
    source_id = row.get("source_id")
    if not isinstance(source_id, str) or source_id == "":
        return False
    if store.get_source(source_id) is None:
        return False
    return _text_refused(_first_chunk_text(store, source_id))


def _short_excerpt(text: str) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= SLEEP_EXCERPT_MAX:
        return flattened
    return flattened[:SLEEP_EXCERPT_MAX].rstrip()


def _restrict_sidecar_mode(path: Path) -> None:
    """Tighten an existing sidecar that still has group or other bits."""

    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        for row in rows
    )
    tmp_path = path.with_name(f"{path.name}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    except Exception:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    os.close(fd)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


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
