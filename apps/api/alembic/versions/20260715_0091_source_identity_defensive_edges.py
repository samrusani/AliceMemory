"""Close source-identity defensive edges and bound project-event replay.

Revision ID: 20260715_0091
Revises: 20260714_0090

Migration 0090 intentionally mirrored Python's source-capture identity, but
historical rows can contain a string ``raw_text`` made entirely of Python
whitespace.  The application rejects that input, so such rows have no valid
capture identity: clear their live dedupe key instead of preserving a key that
cannot be reproduced through the supported capture surface.  Missing or
non-string ``raw_text`` continues to use the source content hash assigned by
0090.

The same forward migration adds bounded lookup indexes for coupled project
update events.  The target index covers canonical event targets. Three nullable
stored columns extract string-only historical payload identifiers, allowing
tenant-leading B-tree indexes to serve terminal replay without evaluating
arbitrary payload JSON across tenants.
"""

from __future__ import annotations

from alembic import op


revision = "20260715_0091"
down_revision = "20260714_0090"
branch_labels = None
depends_on = None


# CPython 3.12's fixed Unicode whitespace table used by ``str.strip()``.
# Keep this explicit and in lockstep with migration 0090: PostgreSQL POSIX
# classes are locale-dependent and omit controls such as U+001C on supported
# installations.
_PYTHON_312_STRIP_CODEPOINTS = (
    0x0009,
    0x000A,
    0x000B,
    0x000C,
    0x000D,
    0x001C,
    0x001D,
    0x001E,
    0x001F,
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    0x2000,
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
_PYTHON_312_STRIP_CHARS_SQL = " || ".join(f"chr({codepoint})" for codepoint in _PYTHON_312_STRIP_CODEPOINTS)

_CLEAR_LIVE_WHITESPACE_RAW_TEXT_DEDUPE_KEYS = f"""
UPDATE sources
SET dedupe_key = NULL
WHERE deleted_at IS NULL
  AND jsonb_typeof(metadata_json -> 'raw_text') = 'string'
  AND btrim(
        metadata_json ->> 'raw_text',
        {_PYTHON_312_STRIP_CHARS_SQL}
      ) = ''
"""

_PROJECT_UPDATE_EVENT_TYPES_SQL = """
'project.update_candidate_created',
'project.update_candidate_accepted',
'project.update_candidate_rejected'
""".strip()

_ADD_PROJECT_UPDATE_EVENT_LINKAGE_COLUMNS = """
    ALTER TABLE event_log
      ADD COLUMN payload_artifact_id text
        GENERATED ALWAYS AS (
          CASE
            WHEN jsonb_typeof(payload_json -> 'artifact_id') = 'string'
              THEN payload_json ->> 'artifact_id'
            ELSE NULL
          END
        ) STORED,
      ADD COLUMN payload_candidate_memory_id text
        GENERATED ALWAYS AS (
          CASE
            WHEN jsonb_typeof(payload_json -> 'candidate_memory_id') = 'string'
              THEN payload_json ->> 'candidate_memory_id'
            ELSE NULL
          END
        ) STORED,
      ADD COLUMN payload_memory_id text
        GENERATED ALWAYS AS (
          CASE
            WHEN jsonb_typeof(payload_json -> 'memory_id') = 'string'
              THEN payload_json ->> 'memory_id'
            ELSE NULL
          END
        ) STORED
"""

_PROJECT_UPDATE_EVENT_INDEX_STATEMENTS: tuple[str, ...] = (
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_target_idx
      ON event_log (
        user_id,
        target_type,
        target_id,
        event_type,
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
        AND target_type IS NOT NULL
        AND target_id IS NOT NULL
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_artifact_id_idx
      ON event_log (
        user_id,
        event_type,
        payload_artifact_id,
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
        AND payload_artifact_id IS NOT NULL
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_candidate_memory_id_idx
      ON event_log (
        user_id,
        event_type,
        payload_candidate_memory_id,
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
        AND payload_candidate_memory_id IS NOT NULL
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_memory_id_idx
      ON event_log (
        user_id,
        event_type,
        payload_memory_id,
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
        AND payload_memory_id IS NOT NULL
    """,
)

_PROJECT_UPDATE_EVENT_INDEX_NAMES = (
    "event_log_project_update_target_idx",
    "event_log_project_update_artifact_id_idx",
    "event_log_project_update_candidate_memory_id_idx",
    "event_log_project_update_memory_id_idx",
)

_PROJECT_UPDATE_EVENT_LINKAGE_COLUMNS = (
    "payload_artifact_id",
    "payload_candidate_memory_id",
    "payload_memory_id",
)


def upgrade() -> None:
    # The whitespace clear below writes rows on sources, whose forced
    # row-level security binds the documented table owner (NOSUPERUSER
    # NOBYPASSRLS) to owner policies that bind app.current_user_id(), unset
    # during an upgrade, so it silently matched no rows for a vault that
    # already carries sources while a superuser upgrade cleared them.
    # Temporarily use the owner bypass, as 0067, 0070, 0092, 0093 and 0095
    # to 0097 do, inside Alembic's single transaction. Concurrent sessions
    # never see the relaxed FORCE setting; failure rolls back the bracket,
    # the clear and the schema change together, and the forced setting is
    # restored before the transaction commits.
    op.execute("ALTER TABLE sources NO FORCE ROW LEVEL SECURITY")
    op.execute(_CLEAR_LIVE_WHITESPACE_RAW_TEXT_DEDUPE_KEYS)
    op.execute("ALTER TABLE sources FORCE ROW LEVEL SECURITY")
    op.execute(_ADD_PROJECT_UPDATE_EVENT_LINKAGE_COLUMNS)
    for statement in _PROJECT_UPDATE_EVENT_INDEX_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # A whitespace-only source has no supported runtime capture identity, so
    # the cleared value remains NULL. Only the additive query substrate unwinds.
    for index_name in reversed(_PROJECT_UPDATE_EVENT_INDEX_NAMES):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    for column_name in reversed(_PROJECT_UPDATE_EVENT_LINKAGE_COLUMNS):
        op.execute(f"ALTER TABLE event_log DROP COLUMN IF EXISTS {column_name}")
