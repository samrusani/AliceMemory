"""Local SQLite vault census for ``alice-memory doctor``.

Reports what is already stored for one ``user_id``. Sources and searchable
chunks first. Committed facts next. The last brief character count uses
``compile_local_session_brief`` with ``query=None``. Candidates next.
Sleep proposals last. That count is sidecar rows for this user, not
memory rows. When the sidecar cannot be read, that line is
``sleep proposals: unreadable`` and the other lines still print.

This is not ``alicebot vnext doctor`` and must not wrap it. Import is a
source. Commit is a fact. Counts bind ``user_id``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from alicebot_api.legacy_credential_check import commit_door_fields_verdict
from alicebot_api.session_briefing import (
    COMMITTED_MEMORY_STATUSES,
    SESSION_BRIEF_CHAR_CAP,
    brief_char_len,
    compile_local_session_brief,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import SleepError, count_sleep_proposals, sleep_proposals_path

CANDIDATE_STATUS = "candidate"

SOURCE_COUNT_SQL = """
SELECT COUNT(*) AS n
FROM sources
WHERE user_id = ?
  AND deleted_at IS NULL
"""

SEARCHABLE_CHUNK_COUNT_SQL = """
SELECT COUNT(*) AS n
FROM source_chunks c
JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
WHERE c.user_id = ?
  AND s.user_id = ?
  AND s.deleted_at IS NULL
"""

# Static placeholders. Bandit B608 fires on an f-string here even when
# the only interpolated text is ``?, ?``. The statuses stay bound.
COMMITTED_FACT_COUNT_SQL = """
SELECT COUNT(*) AS n
FROM memories
WHERE user_id = ?
  AND deleted_at IS NULL
  AND status IN (?, ?)
"""

CANDIDATE_COUNT_SQL = """
SELECT COUNT(*) AS n
FROM memories
WHERE user_id = ?
  AND deleted_at IS NULL
  AND status = ?
"""

COUNT_SQL_TEXTS = (
    SOURCE_COUNT_SQL,
    SEARCHABLE_CHUNK_COUNT_SQL,
    COMMITTED_FACT_COUNT_SQL,
    CANDIDATE_COUNT_SQL,
)


def compile_local_vault_doctor(
    db_path: Path,
    *,
    user_id: UUID | str,
) -> str:
    """Render the vault census for the acting local user."""

    if COMMITTED_MEMORY_STATUSES != ("active", "accepted"):
        raise RuntimeError(
            "committed-fact COUNT SQL is written for active and accepted only"
        )

    resolved = Path(db_path).expanduser().resolve()
    with sqlite_user_connection(resolved, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        uid = store.user_id
        source_count = _scalar_count(store, SOURCE_COUNT_SQL, (uid,))
        chunk_count = _scalar_count(store, SEARCHABLE_CHUNK_COUNT_SQL, (uid, uid))
        fact_count = _scalar_count(
            store,
            COMMITTED_FACT_COUNT_SQL,
            (uid, *COMMITTED_MEMORY_STATUSES),
        )
        candidate_count = _scalar_count(
            store,
            CANDIDATE_COUNT_SQL,
            (uid, CANDIDATE_STATUS),
        )
        flagged_ids = _flagged_source_ids(store)
        try:
            proposal_count = count_sleep_proposals(sleep_proposals_path(resolved), user_id=uid)
            proposal_line = f"sleep proposals: {proposal_count}"
        except SleepError as exc:
            if str(exc) != "sidecar could not be read":
                raise
            proposal_line = "sleep proposals: unreadable"

    markdown = compile_local_session_brief(resolved, user_id=user_id, query=None)
    character_count = brief_char_len(markdown)
    return "\n".join(
        (
            f"db: {resolved}",
            f"sources: {source_count}",
            f"searchable chunks: {chunk_count}",
            f"committed facts: {fact_count}",
            f"last brief: {character_count} / {SESSION_BRIEF_CHAR_CAP} characters",
            f"candidates waiting: {candidate_count}",
            proposal_line,
            f"flagged sources: {len(flagged_ids)}",
            "flagged source ids: " + ", ".join(flagged_ids),
        )
    )


def _cell(row: object, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None


def source_row_is_flagged(row: object) -> bool:
    """True when a stored source still carries credential material."""

    metadata = _cell(row, "metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    raw_text = metadata.get("raw_text")
    # Same verdict as the commit door. The floor alone misses a low-entropy
    # AKIA-shaped key that the legacy gate refuses.
    return (
        commit_door_fields_verdict(
            _cell(row, "title"),
            _cell(row, "author"),
            _cell(row, "uri"),
            _cell(row, "raw_path"),
            _cell(row, "external_id"),
            raw_text,
            metadata,
        )
        is not None
    )


def _flagged_source_ids(store: SQLiteVNextStore) -> list[str]:
    """Sources that still carry credential material, in id order.

    A source is flagged through its own row fields and ``raw_text``, or through
    the text of any of its chunks. The chunk text is what recall, the brief and
    the session hook read, and a backup import restores it unchanged, so a
    token that sits only in a chunk flags its source.
    """

    rows = store.conn.execute(
        """
        SELECT id, title, author, uri, raw_path, external_id, metadata_json
        FROM sources
        WHERE user_id = ?
          AND deleted_at IS NULL
        ORDER BY id
        """,
        (store.user_id,),
    ).fetchall()
    flagged: set[str] = set()
    for row in rows:
        if source_row_is_flagged(row):
            source_id = _cell(row, "id")
            if source_id is not None:
                flagged.add(str(source_id))
    # Streamed, so a large vault is not held in memory. A source already flagged
    # is not read again.
    chunks = store.conn.execute(
        """
        SELECT c.source_id, c.text
        FROM source_chunks c
        JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
        WHERE c.user_id = ?
          AND s.deleted_at IS NULL
        ORDER BY c.source_id, c.chunk_index
        """,
        (store.user_id,),
    )
    for chunk in chunks:
        source_id = _cell(chunk, "source_id")
        if source_id is None or str(source_id) in flagged:
            continue
        if commit_door_fields_verdict(_cell(chunk, "text")) is not None:
            flagged.add(str(source_id))
    return sorted(flagged)


def _scalar_count(
    store: SQLiteVNextStore,
    sql: str,
    params: tuple[object, ...],
) -> int:
    row = store.conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    value = row["n"] if isinstance(row, dict) else row[0]
    return int(value)


__all__ = [
    "CANDIDATE_COUNT_SQL",
    "CANDIDATE_STATUS",
    "COMMITTED_FACT_COUNT_SQL",
    "COUNT_SQL_TEXTS",
    "SEARCHABLE_CHUNK_COUNT_SQL",
    "SOURCE_COUNT_SQL",
    "compile_local_vault_doctor",
]
