"""Repair stale lifecycle pointers left in duplicate groups of three or more.

The published 20260712_0084 revision moves retry and confirmation identifiers
from a deleted canonical holder to the oldest live duplicate.  In a group of
three or more, however, later duplicate rows still point at the deleted holder
instead of the live row that now owns the identifier.  Existing v0.9.4
databases are already stamped at 0084, so this delta must live in a new
revision rather than changing the published migration in place.

For each identifier, this revision follows the two-hop repair trail written by
0083/0084 (sibling -> deleted former holder -> live current holder) and rewrites
the sibling pointer to the current holder.  Once rewritten the two-hop pattern
no longer matches, making the pass idempotent.  Downgrade is intentionally a
no-op because restoring stale pointers would reintroduce the defect.

Revision ID: 20260713_0086
Revises: 20260713_0085
"""

from __future__ import annotations

from alembic import op


revision = "20260713_0086"
down_revision = "20260713_0085"
branch_labels = None
depends_on = None


_REPAIR_SPECS: tuple[tuple[str, str], ...] = (
    ("commit_digest", "duplicate_commit_digest_canonical_memory_id"),
    ("confirmation_id", "duplicate_confirmation_id_canonical_memory_id"),
)


def _repair_statement(column: str, pointer_key: str) -> str:
    pointer_path = "{lifecycle_migration," + pointer_key + "}"
    return f"""
        WITH repair AS (
          SELECT
            sibling.id AS sibling_id,
            canonical.id AS canonical_id
          FROM memories AS sibling
          JOIN memories AS former_holder
            ON former_holder.user_id = sibling.user_id
           AND former_holder.id::text = (sibling.metadata_json #>> '{pointer_path}')
          JOIN memories AS canonical
            ON canonical.user_id = sibling.user_id
           AND canonical.id::text = (former_holder.metadata_json #>> '{pointer_path}')
          WHERE sibling.id <> canonical.id
            AND former_holder.deleted_at IS NOT NULL
            AND former_holder.{column} IS NULL
            AND canonical.deleted_at IS NULL
            AND canonical.{column} IS NOT NULL
        )
        UPDATE memories AS memory
        SET metadata_json = jsonb_set(
          memory.metadata_json,
          '{pointer_path}',
          to_jsonb(repair.canonical_id::text),
          true
        )
        FROM repair
        WHERE memory.id = repair.sibling_id
        """


_UPGRADE_STATEMENTS: tuple[str, ...] = tuple(
    _repair_statement(column, pointer_key)
    for column, pointer_key in _REPAIR_SPECS
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
    # Data-only correction: reversing it would make the metadata untruthful.
    pass
