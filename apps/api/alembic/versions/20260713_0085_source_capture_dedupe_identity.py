"""Add an atomic, backward-compatible source capture dedupe identity.

``sources.content_hash`` is a public identity and is intentionally not
rewritten.  v0.9.4 folded project scope into new hashes, while older scoped
rows retained the raw normalized-text hash.  ``dedupe_key`` backfills the
v0.9.4 algorithm from preserved raw text plus canonical project scope, so both
generations participate in one partial unique index.

If historical duplicates already exist, only the oldest live row receives the
key.  The other rows and all of their evidence remain intact; future retries
conflict with the canonical row instead of minting another duplicate.

Revision ID: 20260713_0085
Revises: 20260712_0084
"""

from __future__ import annotations

from alembic import op


revision = "20260713_0085"
down_revision = "20260712_0084"
branch_labels = None
depends_on = None


_UPGRADE_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE sources ADD COLUMN dedupe_key text NULL",
    """
    WITH computed AS (
      SELECT
        id,
        user_id,
        captured_at,
        deleted_at,
        CASE
          WHEN jsonb_typeof(metadata_json -> 'raw_text') = 'string' THEN
            'capture-md5:' || md5(
              btrim(
                replace(
                  replace(metadata_json ->> 'raw_text', E'\r\n', E'\n'),
                  E'\r',
                  E'\n'
                )
              ) ||
              CASE
                WHEN jsonb_typeof(metadata_json -> 'project_scope') = 'array'
                     AND EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements_text(metadata_json -> 'project_scope')
                         AS nonblank_scope(value)
                       WHERE btrim(nonblank_scope.value) <> ''
                     )
                THEN chr(31) || 'project_scope:' || (
                  SELECT string_agg(
                    regexp_replace(btrim(scope_value), '\\s+', ' ', 'g'),
                    chr(31)
                    ORDER BY regexp_replace(btrim(scope_value), '\\s+', ' ', 'g')
                  )
                  FROM jsonb_array_elements_text(metadata_json -> 'project_scope')
                    AS scope_values(scope_value)
                  WHERE btrim(scope_value) <> ''
                )
                ELSE ''
              END
            )
          ELSE content_hash
        END AS computed_key
      FROM sources
    ),
    ranked AS (
      SELECT
        *,
        row_number() OVER (
          PARTITION BY user_id, computed_key, (deleted_at IS NULL)
          ORDER BY captured_at ASC, id ASC
        ) AS duplicate_rank
      FROM computed
    )
    UPDATE sources AS source
    SET dedupe_key = CASE
      WHEN ranked.deleted_at IS NOT NULL OR ranked.duplicate_rank = 1
      THEN ranked.computed_key
      ELSE NULL
    END
    FROM ranked
    WHERE source.id = ranked.id
    """,
    """
    ALTER TABLE sources
      ADD CONSTRAINT sources_dedupe_key_length_check
      CHECK (dedupe_key IS NULL OR char_length(dedupe_key) BETWEEN 1 AND 200)
    """,
    """
    CREATE UNIQUE INDEX sources_user_dedupe_key_unique_idx
      ON sources (user_id, dedupe_key)
      WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
    """,
)


_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS sources_user_dedupe_key_unique_idx",
    "ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_dedupe_key_length_check",
    "ALTER TABLE sources DROP COLUMN IF EXISTS dedupe_key",
)


def upgrade() -> None:
    # The dedupe_key backfill below reads and writes rows on sources, whose
    # forced row-level security binds the documented table owner
    # (NOSUPERUSER NOBYPASSRLS) to owner policies that bind
    # app.current_user_id(), unset during an upgrade, so the backfill
    # silently matched no rows for a vault that already carries sources
    # while a superuser upgrade rewrote them. Temporarily use the owner
    # bypass, as 0067, 0070, 0092, 0093 and 0095 to 0097 do, inside Alembic's
    # single transaction. Concurrent sessions never see the relaxed FORCE
    # setting; failure rolls back the bracket, the backfill and the schema
    # change together, and the forced setting is restored before the
    # transaction commits.
    op.execute("ALTER TABLE sources NO FORCE ROW LEVEL SECURITY")
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)
    op.execute("ALTER TABLE sources FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
