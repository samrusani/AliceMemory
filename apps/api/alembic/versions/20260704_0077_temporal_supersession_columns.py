"""Temporal cheap slice: supersession pointer columns and edge event time.

Answering "what did I believe before / what changed" without the legacy
engine needs two things the vNext schema recorded only softly:

* Memory supersession pointers lived in ``metadata_json``
  (``metadata_json ->> 'superseded_by'`` on the retired row and
  ``metadata_json ->> 'supersedes'`` on its replacement, written by the
  SQLite-mode supersede-existing review flow) while beliefs already had a
  real ``superseded_by`` column. This migration promotes both pointers to
  real columns on ``memories``:

  - ``memories.superseded_by uuid NULL`` -- the memory that replaced this
    row.
  - ``memories.supersedes uuid NULL`` -- the memory this row replaced.

  Both are plain nullable uuid columns with NO foreign key on purpose: a
  supersession pointer is a historical annotation, not a lifecycle
  dependency. An FK to ``memories.id`` would couple delete order (a
  replacement row could not be hard-deleted or archived before its
  predecessor's pointer was cleared, and vice versa) and would break the
  undo/redo paths that retire either side independently. A dangling
  pointer simply ends the supersession chain walk.

* ``graph_edges.observed_at timestamptz NULL`` -- the event time of the
  observation an edge encodes (the source's ``source_created_at``, falling
  back to ``captured_at``, falling back to write time), as opposed to
  ``created_at`` which is purely ingestion time. The write path also
  starts populating the previously dead ``valid_from`` column with the
  same instant, which is what makes as-of edge queries possible.

Backfill: ``superseded_by``/``supersedes`` are copied from the exact
``metadata_json`` keys the review flow writes, guarded by a uuid-shape
match so a malformed metadata value cannot fail the cast; the metadata
copies are kept for backward compatibility. Existing edges keep NULL
``observed_at``/``valid_from`` (their event time was never recorded).

A partial index on ``(user_id, superseded_by) WHERE superseded_by IS NOT
NULL`` backs "what replaced X" lookups without taxing the (vast) majority
of never-superseded rows.

Reversible: the downgrade drops the index and all three columns.

Revision ID: 20260704_0077
Revises: 20260704_0076
"""

from __future__ import annotations

from alembic import op


revision = "20260704_0077"
down_revision = "20260704_0076"
branch_labels = None
depends_on = None


# Exact metadata keys the backfill reads. Both are written by the
# SQLite-mode supersede-existing flow in mcp_tools._sqlite_memory_correct:
# the retired row gets metadata_json ->> 'superseded_by' and the
# replacement row gets metadata_json ->> 'supersedes'.
_SUPERSEDED_BY_METADATA_SQL = "metadata_json ->> 'superseded_by'"
_SUPERSEDES_METADATA_SQL = "metadata_json ->> 'supersedes'"

# Only copy values that are shaped like a uuid; anything else would fail
# the ::uuid cast and abort the migration.
_UUID_SHAPE_REGEX_SQL = (
    "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    "-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
)

_UPGRADE_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE memories ADD COLUMN superseded_by uuid NULL",
    "ALTER TABLE memories ADD COLUMN supersedes uuid NULL",
    "ALTER TABLE graph_edges ADD COLUMN observed_at timestamptz NULL",
    f"""
        UPDATE memories
        SET superseded_by = ({_SUPERSEDED_BY_METADATA_SQL})::uuid
        WHERE superseded_by IS NULL
          AND {_SUPERSEDED_BY_METADATA_SQL} ~ {_UUID_SHAPE_REGEX_SQL}
        """,
    f"""
        UPDATE memories
        SET supersedes = ({_SUPERSEDES_METADATA_SQL})::uuid
        WHERE supersedes IS NULL
          AND {_SUPERSEDES_METADATA_SQL} ~ {_UUID_SHAPE_REGEX_SQL}
        """,
    """
        CREATE INDEX memories_user_superseded_by_idx
          ON memories (user_id, superseded_by)
          WHERE superseded_by IS NOT NULL
        """,
)

_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS memories_user_superseded_by_idx",
    "ALTER TABLE memories DROP COLUMN IF EXISTS superseded_by",
    "ALTER TABLE memories DROP COLUMN IF EXISTS supersedes",
    "ALTER TABLE graph_edges DROP COLUMN IF EXISTS observed_at",
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
