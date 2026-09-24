"""Local SQLite vault census for ``alice-memory doctor``.

Reports what is already stored for one ``user_id``. Sources and searchable
chunks first. Committed facts next. The last brief token estimate uses
``compile_local_session_brief`` with ``query=None``. Candidates next.
Sleep proposals last. That count is sidecar rows for this user, not
memory rows.

This is not ``alicebot vnext doctor`` and must not wrap it. Import is a
source. Commit is a fact. Counts bind ``user_id``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from alicebot_api.session_briefing import (
    COMMITTED_MEMORY_STATUSES,
    SESSION_BRIEF_TOKEN_BUDGET,
    compile_local_session_brief,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import count_sleep_proposals, sleep_proposals_path
from alicebot_api.vnext_retrieval import estimate_item_tokens

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
        proposal_count = count_sleep_proposals(sleep_proposals_path(resolved), user_id=uid)

    markdown = compile_local_session_brief(resolved, user_id=user_id, query=None)
    token_estimate = estimate_item_tokens({"text": markdown})
    return "\n".join(
        (
            f"db: {resolved}",
            f"sources: {source_count}",
            f"searchable chunks: {chunk_count}",
            f"committed facts: {fact_count}",
            f"last brief: {token_estimate} / {SESSION_BRIEF_TOKEN_BUDGET} tokens",
            f"candidates waiting: {candidate_count}",
            f"sleep proposals: {proposal_count}",
        )
    )


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
