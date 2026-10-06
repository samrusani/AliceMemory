from __future__ import annotations

from alembic import command
import psycopg
import pytest

from alicebot_api.migrations import make_alembic_config


_PARTIALLY_COMMITTED_SCHEMA = (
    """
    ALTER TABLE scheduler_workflows
      ADD COLUMN claim_token text NULL,
      ADD COLUMN claim_version bigint NOT NULL DEFAULT 0,
      ADD COLUMN claim_expires_at timestamptz NULL,
      ADD CONSTRAINT scheduler_workflows_claim_version_non_negative_check
        CHECK (claim_version >= 0)
    """,
    """
    ALTER TABLE scheduler_runs
      ADD COLUMN claim_token text NULL,
      ADD COLUMN claim_version bigint NOT NULL DEFAULT 0,
      ADD COLUMN claim_expires_at timestamptz NULL,
      ADD COLUMN scheduled_for timestamptz NULL,
      ADD CONSTRAINT scheduler_runs_claim_version_non_negative_check
        CHECK (claim_version >= 0),
      ADD CONSTRAINT scheduler_runs_claim_shape_check
        CHECK (
          (claim_token IS NULL AND claim_expires_at IS NULL)
          OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
        )
    """,
    """
    ALTER TABLE memories
      ADD CONSTRAINT memories_status_check
      CHECK (
        status IN (
          'deleted', 'candidate', 'active', 'accepted', 'rejected',
          'superseded', 'stale', 'archived', 'needs_review', 'private_only'
        )
      )
      NOT VALID
    """,
)

_IDEMPOTENCY_INDEX_NAME = "generated_artifacts_user_idempotency_digest_uidx"
_IDEMPOTENCY_INDEX_BUILD = f"""
    CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_IDEMPOTENCY_INDEX_NAME}
      ON generated_artifacts (
        user_id,
        artifact_type,
        (metadata_json ->> 'workflow'),
        (metadata_json ->> 'idempotency_digest')
      )
      WHERE metadata_json ->> 'idempotency_digest' IS NOT NULL
    """


def _idempotency_index_state(database_url: str) -> tuple[bool, bool, bool] | None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT metadata.indisvalid, metadata.indisready, metadata.indisunique
                FROM pg_catalog.pg_class AS index_class
                JOIN pg_catalog.pg_index AS metadata
                  ON metadata.indexrelid = index_class.oid
                WHERE index_class.relname = %s
                """,
                (_IDEMPOTENCY_INDEX_NAME,),
            )
            return cur.fetchone()


def test_0087_retries_after_committed_ddl_and_invalid_concurrent_unique_index(
    database_urls,
) -> None:
    """Reproduce the catalog state left by an interrupted 0087 attempt.

    Entering Alembic's autocommit block commits the preceding column and
    constraint DDL. A later failed concurrent unique-index build then leaves
    the database at 0086 with both the schema additions and an invalid named
    index. Retrying must neither raise DuplicateColumn nor accept that invalid
    uniqueness fence as satisfying ``IF NOT EXISTS``.
    """

    database_url = database_urls["admin"]
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260713_0086")

    user_id = "00000000-0000-0000-0000-000000008701"
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for statement in _PARTIALLY_COMMITTED_SCHEMA:
                cur.execute(statement)
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "migration-0087-retry@example.com", "Migration 0087 Retry"),
            )
            cur.execute(
                """
                INSERT INTO generated_artifacts (
                  user_id, artifact_type, title, content_markdown,
                  generated_by, metadata_json
                )
                SELECT
                  %s::uuid,
                  'daily_brief',
                  'retry fixture',
                  'retry fixture',
                  'migration-test',
                  '{
                    "workflow": "daily_brief",
                    "idempotency_digest": "duplicate-before-retry"
                  }'::jsonb
                FROM generate_series(1, 2)
                """,
                (user_id,),
            )

    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(_IDEMPOTENCY_INDEX_BUILD)

    assert _idempotency_index_state(database_url) == (False, False, True)

    # Remove the transient data conflict but deliberately retain the invalid
    # catalog row. This models an operator correcting the build cause before
    # rerunning the still-unapplied Alembic revision.
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM generated_artifacts
                WHERE id IN (
                  SELECT id
                  FROM generated_artifacts
                  WHERE user_id = %s::uuid
                  ORDER BY id
                  OFFSET 1
                )
                """,
                (user_id,),
            )

    command.upgrade(config, "20260713_0087")

    assert _idempotency_index_state(database_url) == (True, True, True)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone() == ("20260713_0087",)
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND (
                    (table_name = 'scheduler_workflows'
                     AND column_name IN ('claim_token', 'claim_version', 'claim_expires_at'))
                    OR
                    (table_name = 'scheduler_runs'
                     AND column_name IN (
                       'claim_token', 'claim_version', 'claim_expires_at', 'scheduled_for'
                     ))
                  )
                """
            )
            assert len(cur.fetchall()) == 7
            cur.execute(
                """
                SELECT conname
                FROM pg_catalog.pg_constraint
                WHERE conname IN (
                  'scheduler_workflows_claim_version_non_negative_check',
                  'scheduler_runs_claim_version_non_negative_check',
                  'scheduler_runs_claim_shape_check',
                  'memories_status_check'
                )
                """
            )
            assert len(cur.fetchall()) == 4

    command.downgrade(config, "20260713_0086")

    assert _idempotency_index_state(database_url) is None
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name IN ('scheduler_workflows', 'scheduler_runs')
                  AND column_name IN (
                    'claim_token', 'claim_version', 'claim_expires_at', 'scheduled_for'
                  )
                """
            )
            assert cur.fetchone() == (0,)
