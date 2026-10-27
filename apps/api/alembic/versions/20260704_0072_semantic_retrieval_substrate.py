"""Add the semantic retrieval substrate to the shared memories table.

Additive only:
- ``embedding_vector vector(1536)`` with an HNSW cosine index for vector search.
- ``search_tsv`` stored generated tsvector over title/canonical_text/summary
  with a GIN index for full-text search.
- ``commit_digest`` and ``confirmation_id`` columns (backfilled from the
  agentic-commit JSONB metadata) with partial indexes so agentic memory
  commit lookups stop scanning the full table in Python.
- A partial index for the latest-agentic-commit lookup.
"""

from __future__ import annotations

from alembic import op


revision = "20260704_0072"
down_revision = "20260621_0071"
branch_labels = None
depends_on = None


_UPGRADE_STATEMENTS = (
    """
        ALTER TABLE memories
          ADD COLUMN embedding_vector vector(1536) NULL
        """,
    """
        CREATE INDEX memories_embedding_vector_hnsw_idx
          ON memories USING hnsw (embedding_vector vector_cosine_ops)
        """,
    """
        ALTER TABLE memories
          ADD COLUMN search_tsv tsvector
          GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A')
            || setweight(to_tsvector('english', coalesce(canonical_text, '')), 'B')
            || setweight(to_tsvector('english', coalesce(summary, '')), 'C')
          ) STORED
        """,
    """
        CREATE INDEX memories_search_tsv_gin_idx
          ON memories USING gin (search_tsv)
        """,
    """
        ALTER TABLE memories
          ADD COLUMN commit_digest text NULL,
          ADD COLUMN confirmation_id text NULL
        """,
    """
        UPDATE memories
        SET commit_digest = metadata_json #>> '{agentic_memory,idempotency_key}'
        WHERE commit_digest IS NULL
          AND metadata_json #>> '{agentic_memory,idempotency_key}' IS NOT NULL
        """,
    """
        UPDATE memories
        SET confirmation_id = metadata_json #>> '{agentic_memory,confirmation,confirmation_id}'
        WHERE confirmation_id IS NULL
          AND metadata_json #>> '{agentic_memory,confirmation,confirmation_id}' IS NOT NULL
        """,
    """
        CREATE INDEX memories_commit_digest_idx
          ON memories (user_id, commit_digest)
          WHERE commit_digest IS NOT NULL
        """,
    """
        CREATE INDEX memories_confirmation_id_idx
          ON memories (user_id, confirmation_id)
          WHERE confirmation_id IS NOT NULL
        """,
    """
        CREATE INDEX memories_agentic_commit_updated_idx
          ON memories (user_id, updated_at DESC, id DESC)
          WHERE (metadata_json #>> '{agentic_memory,kind}') = 'agentic_memory_commit'
        """,
)

_DOWNGRADE_STATEMENTS = (
    "DROP INDEX IF EXISTS memories_agentic_commit_updated_idx",
    "DROP INDEX IF EXISTS memories_confirmation_id_idx",
    "DROP INDEX IF EXISTS memories_commit_digest_idx",
    "ALTER TABLE memories DROP COLUMN IF EXISTS confirmation_id",
    "ALTER TABLE memories DROP COLUMN IF EXISTS commit_digest",
    "DROP INDEX IF EXISTS memories_search_tsv_gin_idx",
    "ALTER TABLE memories DROP COLUMN IF EXISTS search_tsv",
    "DROP INDEX IF EXISTS memories_embedding_vector_hnsw_idx",
    "ALTER TABLE memories DROP COLUMN IF EXISTS embedding_vector",
)


def upgrade() -> None:
    # The data statements in _UPGRADE_STATEMENTS write rows on memories,
    # whose forced row-level security binds the documented table owner
    # (NOSUPERUSER NOBYPASSRLS) to owner policies that bind
    # app.current_user_id(), unset during an upgrade, so the backfill
    # silently matched no rows for a data-bearing vault while a superuser
    # upgrade wrote them. Temporarily use the owner bypass, as 0067, 0070,
    # 0092, 0093 and 0095 to 0097 do, inside Alembic's single transaction.
    # Concurrent sessions never see the relaxed FORCE setting; failure rolls
    # back the bracket, the backfill and the schema change together, and the
    # forced setting is restored before the transaction commits.
    op.execute("ALTER TABLE memories NO FORCE ROW LEVEL SECURITY")
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)
    op.execute("ALTER TABLE memories FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
