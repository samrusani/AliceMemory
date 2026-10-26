"""Repair retry/confirmation identifiers 0083 stranded on tombstones.

Revision 20260711_0083 made ``commit_digest`` and ``confirmation_id`` unique by
keeping the *earliest* duplicate as the canonical lookup target. It ordered the
duplicates purely by ``created_at``/``id`` and never considered deletion, so an
older archived/deleted row (a tombstone) would win the identifier while a newer
active row was cleared. The runtime replay lookups
(``get_memory_by_commit_digest`` / ``get_memory_by_confirmation_id``) filter
``deleted_at IS NULL``, so a stranded identifier makes replay return nothing
while the partial unique index still blocks re-inserting the same key — an
idempotency key or confirmation id that can neither be found nor reused.

0083 already shipped in v0.9.2, and Alembic replays revisions by id rather than
by body, so editing 0083 in place would not re-run on databases that already
applied it. This corrective follow-up therefore repairs the data instead. It is
safe on both populations: a database already mis-upgraded by 0083 is repaired
here, and a database upgrading fresh runs 0083 (which may strand an identifier)
immediately followed by this revision (which moves it back). The pass is
idempotent — on already-correct data it matches no rows.

For every deleted row that still holds an identifier whose cleared sibling (the
one 0083 back-pointed at it via ``metadata_json.lifecycle_migration``) is live,
the identifier is released from the tombstone and restored onto the oldest such
live row. The move is release-then-restore across two statements so the partial
unique index — already in place from 0083 — is never momentarily violated. The
live row is also given back its mirrored ``agentic_memory`` metadata value so it
is indistinguishable from a row a correct 0083 would have left canonical.

Downgrade is intentionally a no-op: this revision only corrects data (no schema
change), and reversing it would re-strand identifiers on tombstones, i.e.
re-introduce the defect. This also keeps the revision safe to re-run.

Revision ID: 20260712_0084
Revises: 20260711_0083
"""

from __future__ import annotations

from alembic import op


revision = "20260712_0084"
down_revision = "20260711_0083"
branch_labels = None
depends_on = None


# (column, canonical-pointer key written by 0083, mirrored metadata path,
#  scratch temp-table name)
_REPAIR_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "commit_digest",
        "duplicate_commit_digest_canonical_memory_id",
        "{agentic_memory,idempotency_key}",
        "_lifecycle_0084_commit_digest_repair",
    ),
    (
        "confirmation_id",
        "duplicate_confirmation_id_canonical_memory_id",
        "{agentic_memory,confirmation,confirmation_id}",
        "_lifecycle_0084_confirmation_id_repair",
    ),
)


def _repair_statements(
    column: str, pointer_key: str, mirror_path: str, temp_table: str
) -> tuple[str, ...]:
    pointer_path = "{lifecycle_migration," + pointer_key + "}"
    return (
        f"DROP TABLE IF EXISTS {temp_table}",
        # The oldest live row that 0083 cleared and back-pointed at each
        # tombstone still holding the identifier. CROSS JOIN LATERAL drops any
        # tombstone without such a live sibling, so nothing is moved unless the
        # mis-assignment is real.
        f"""
        CREATE TEMP TABLE {temp_table} AS
        WITH holder AS (
          SELECT id, user_id, {column} AS holder_value
          FROM memories
          WHERE {column} IS NOT NULL
            AND deleted_at IS NOT NULL
        )
        SELECT
          holder.id AS holder_id,
          holder.holder_value,
          live.candidate_id
        FROM holder
        CROSS JOIN LATERAL (
          SELECT candidate.id AS candidate_id
          FROM memories AS candidate
          WHERE candidate.user_id = holder.user_id
            AND candidate.deleted_at IS NULL
            AND candidate.{column} IS NULL
            AND (candidate.metadata_json #>> '{pointer_path}') = holder.id::text
          ORDER BY candidate.created_at ASC, candidate.id ASC
          LIMIT 1
        ) AS live
        """,
        # Release the identifier from the tombstone and record where it went.
        f"""
        UPDATE memories AS m
        SET
          {column} = NULL,
          metadata_json = jsonb_set(
            m.metadata_json,
            '{{lifecycle_migration}}',
            COALESCE(m.metadata_json -> 'lifecycle_migration', '{{}}'::jsonb)
              || jsonb_build_object('{pointer_key}', r.candidate_id::text),
            true
          )
        FROM {temp_table} AS r
        WHERE m.id = r.holder_id
        """,
        # Restore the identifier onto the live row, drop its stale back-pointer,
        # and re-populate the mirrored metadata value.
        f"""
        UPDATE memories AS m
        SET
          {column} = r.holder_value,
          metadata_json = jsonb_set(
            m.metadata_json #- '{pointer_path}',
            '{mirror_path}',
            to_jsonb(r.holder_value),
            true
          )
        FROM {temp_table} AS r
        WHERE m.id = r.candidate_id
        """,
        f"DROP TABLE {temp_table}",
    )


_UPGRADE_STATEMENTS: tuple[str, ...] = tuple(
    statement for spec in _REPAIR_SPECS for statement in _repair_statements(*spec)
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
    # Data-only correction: reversing it would re-strand identifiers on
    # tombstones (the original defect), so downgrade deliberately does nothing.
    pass
