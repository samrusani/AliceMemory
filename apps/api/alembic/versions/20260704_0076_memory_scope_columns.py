"""Real multi-scope memory: first-class scope columns on memories.

Until now the only hard scope was ``user_id`` (RLS/binding); project scope
lived as soft metadata (``metadata_json ->> 'project_id'``) and agent
attribution only inside the agentic-commit metadata block. This migration
promotes those scopes to real, filterable columns:

* ``memories.project_id text NULL`` -- backfilled from
  ``metadata_json ->> 'project_id'`` (the key the capture/read paths already
  use for the soft project filter).
* ``memories.created_by_agent_id text NULL`` -- backfilled from the
  agentic-commit metadata written by
  ``vnext_memory_commit.VNextMemoryCommitService._base_metadata`` /
  ``vnext_agent_control.agent_metadata``: the top-level
  ``metadata_json ->> 'agent_id'`` key, falling back to the nested
  ``metadata_json #>> '{agentic_memory,agent_identity,agent_id}'`` key.
* ``memories.run_id text NULL`` -- backfilled the same way from
  ``metadata_json ->> 'agent_run_id'`` falling back to
  ``metadata_json #>> '{agentic_memory,agent_identity,agent_run_id}'``.
  run_id stays metadata-plus-filter only: there is deliberately no session
  entity or foreign key behind it.

A partial index on ``(user_id, project_id) WHERE project_id IS NOT NULL``
backs project-scoped retrieval without taxing unscoped rows.

The trust side of project scoping lands in the same revision:
``agent_api_keys.project_scope text NULL`` binds an issued key to one
project so ``resolve_agent_identity`` can stop trusting the payload's
self-asserted ``project_scope`` whenever a binding exists.

Reversible: the downgrade drops the index and all four columns.

Revision ID: 20260704_0076
Revises: 20260704_0075
"""

from __future__ import annotations

from alembic import op


revision = "20260704_0076"
down_revision = "20260704_0075"
branch_labels = None
depends_on = None


# Exact metadata keys the backfill reads. The top-level keys come from
# vnext_agent_control.agent_metadata (spread into metadata_json by
# _base_metadata); the nested keys come from the agentic_memory block's
# agent_identity record (AgentIdentity.to_record).
_PROJECT_ID_METADATA_SQL = "metadata_json ->> 'project_id'"
_AGENT_ID_METADATA_SQL = (
    "COALESCE(metadata_json ->> 'agent_id', "
    "metadata_json #>> '{agentic_memory,agent_identity,agent_id}')"
)
_RUN_ID_METADATA_SQL = (
    "COALESCE(metadata_json ->> 'agent_run_id', "
    "metadata_json #>> '{agentic_memory,agent_identity,agent_run_id}')"
)

_UPGRADE_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE memories ADD COLUMN project_id text NULL",
    "ALTER TABLE memories ADD COLUMN created_by_agent_id text NULL",
    "ALTER TABLE memories ADD COLUMN run_id text NULL",
    f"""
        UPDATE memories
        SET project_id = {_PROJECT_ID_METADATA_SQL}
        WHERE project_id IS NULL
          AND {_PROJECT_ID_METADATA_SQL} IS NOT NULL
        """,
    f"""
        UPDATE memories
        SET created_by_agent_id = {_AGENT_ID_METADATA_SQL}
        WHERE created_by_agent_id IS NULL
          AND {_AGENT_ID_METADATA_SQL} IS NOT NULL
        """,
    f"""
        UPDATE memories
        SET run_id = {_RUN_ID_METADATA_SQL}
        WHERE run_id IS NULL
          AND {_RUN_ID_METADATA_SQL} IS NOT NULL
        """,
    """
        CREATE INDEX memories_user_project_idx
          ON memories (user_id, project_id)
          WHERE project_id IS NOT NULL
        """,
    "ALTER TABLE agent_api_keys ADD COLUMN project_scope text NULL",
)

_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS memories_user_project_idx",
    "ALTER TABLE memories DROP COLUMN IF EXISTS run_id",
    "ALTER TABLE memories DROP COLUMN IF EXISTS created_by_agent_id",
    "ALTER TABLE memories DROP COLUMN IF EXISTS project_id",
    "ALTER TABLE agent_api_keys DROP COLUMN IF EXISTS project_scope",
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
