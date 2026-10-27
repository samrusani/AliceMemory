"""Add vNext connector settings and state tables.

A database already past this revision never runs this body again. A database
still before it must end in exactly the state the superuser path gives: the
same seed rows for every stored user, whether the migrator is a cluster
superuser or the documented table owner that is NOSUPERUSER NOBYPASSRLS. The
seed writes under forced row-level security whose policies the owner cannot
satisfy unbound, so the owner bypass is used inside this migration's own
transaction, as 0067, 0093 and 0095 to 0097 do, and the forced setting is
restored before the transaction commits.
"""

from __future__ import annotations

from alembic import op


revision = "20260511_0070"
down_revision = "20260511_0069"
branch_labels = None
depends_on = None


DOMAINS = (
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
)

SENSITIVITY_LEVELS = (
    "public",
    "internal",
    "private",
    "confidential",
    "highly_sensitive",
    "sacred",
    "regulated",
    "unknown",
)

SYNC_MODES = ("manual", "polling", "watch", "on_demand", "disabled")

CORE_CONNECTOR_DEFAULTS = (
    ("telegram", "personal", "private", "polling", 60),
    ("local_folder", "project", "private", "watch", 30),
    ("browser_clipper", "professional", "private", "on_demand", None),
    ("agent_output", "project", "private", "on_demand", None),
)

_DOMAINS_SQL = ", ".join(f"'{value}'" for value in DOMAINS)
_SENSITIVITY_SQL = ", ".join(f"'{value}'" for value in SENSITIVITY_LEVELS)
_SYNC_MODES_SQL = ", ".join(f"'{value}'" for value in SYNC_MODES)
_CORE_CONNECTOR_VALUES_SQL = ",\n              ".join(
    f"('{name}', '{domain}', '{sensitivity}', '{sync_mode}', {interval if interval is not None else 'NULL'}::integer)"
    for name, domain, sensitivity, sync_mode, interval in CORE_CONNECTOR_DEFAULTS
)

_UPGRADE_SCHEMA = f"""
        CREATE TABLE connector_settings (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          connector_name text NOT NULL,
          enabled boolean NOT NULL DEFAULT false,
          configured boolean NOT NULL DEFAULT false,
          default_domain text NOT NULL DEFAULT 'unknown',
          default_sensitivity text NOT NULL DEFAULT 'private',
          sync_mode text NOT NULL DEFAULT 'manual',
          poll_interval_seconds integer NULL,
          secret_ref text NULL,
          validation_errors_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          metadata_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          last_configured_at timestamptz NULL,
          UNIQUE (id, user_id),
          UNIQUE (user_id, connector_name),
          CONSTRAINT connector_settings_default_domain_check
            CHECK (default_domain IN ({_DOMAINS_SQL})),
          CONSTRAINT connector_settings_default_sensitivity_check
            CHECK (default_sensitivity IN ({_SENSITIVITY_SQL})),
          CONSTRAINT connector_settings_sync_mode_check
            CHECK (sync_mode IN ({_SYNC_MODES_SQL})),
          CONSTRAINT connector_settings_poll_interval_check
            CHECK (poll_interval_seconds IS NULL OR poll_interval_seconds >= 1),
          CONSTRAINT connector_settings_metadata_json_object_check
            CHECK (jsonb_typeof(metadata_json) = 'object'),
          CONSTRAINT connector_settings_validation_errors_json_array_check
            CHECK (jsonb_typeof(validation_errors_json) = 'array')
        );

        CREATE TABLE connector_state (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          connector_id uuid NULL REFERENCES connector_settings(id) ON DELETE SET NULL,
          connector_name text NOT NULL,
          cursor_type text NOT NULL DEFAULT 'sync_cursor',
          cursor_value text NULL,
          last_sync_at timestamptz NULL,
          last_success_at timestamptz NULL,
          last_failure_at timestamptz NULL,
          last_error text NULL,
          items_seen integer NOT NULL DEFAULT 0,
          items_captured integer NOT NULL DEFAULT 0,
          items_deduped integer NOT NULL DEFAULT 0,
          items_failed integer NOT NULL DEFAULT 0,
          average_processing_time_ms numeric(12, 3) NULL,
          state_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (id, user_id),
          UNIQUE (user_id, connector_name, cursor_type),
          CONSTRAINT connector_state_counter_check
            CHECK (items_seen >= 0 AND items_captured >= 0 AND items_deduped >= 0 AND items_failed >= 0),
          CONSTRAINT connector_state_avg_processing_time_check
            CHECK (average_processing_time_ms IS NULL OR average_processing_time_ms >= 0),
          CONSTRAINT connector_state_json_object_check
            CHECK (jsonb_typeof(state_json) = 'object')
        );

        CREATE INDEX connector_settings_user_connector_idx
          ON connector_settings (user_id, connector_name);
        CREATE INDEX connector_state_user_connector_idx
          ON connector_state (user_id, connector_name, updated_at DESC);
        CREATE INDEX connector_state_user_last_sync_idx
          ON connector_state (user_id, last_sync_at DESC NULLS LAST);
        """

_SEED_DEFAULTS = f"""
        WITH connector_defaults (
          connector_name,
          default_domain,
          default_sensitivity,
          sync_mode,
          poll_interval_seconds
        ) AS (
          VALUES
              {_CORE_CONNECTOR_VALUES_SQL}
        ),
        inserted_settings AS (
          INSERT INTO connector_settings (
            user_id,
            connector_name,
            default_domain,
            default_sensitivity,
            sync_mode,
            poll_interval_seconds,
            metadata_json
          )
          SELECT
            u.id,
            d.connector_name,
            d.default_domain,
            d.default_sensitivity,
            d.sync_mode,
            d.poll_interval_seconds,
            jsonb_build_object('initialized_by', '20260511_0070')
          FROM users u
          CROSS JOIN connector_defaults d
          ON CONFLICT (user_id, connector_name) DO NOTHING
          RETURNING id, user_id, connector_name
        )
        INSERT INTO connector_state (
          user_id,
          connector_id,
          connector_name,
          cursor_type,
          state_json
        )
        SELECT
          s.user_id,
          s.id,
          s.connector_name,
          'sync_cursor',
          jsonb_build_object('initialized_by', '20260511_0070')
        FROM connector_settings s
        WHERE s.connector_name IN (SELECT connector_name FROM connector_defaults)
        ON CONFLICT (user_id, connector_name, cursor_type) DO NOTHING;
        """

_GRANTS = (
    "GRANT SELECT, INSERT, UPDATE ON connector_settings TO alicebot_app",
    "GRANT SELECT, INSERT, UPDATE ON connector_state TO alicebot_app",
)

_POLICIES = """
        CREATE POLICY connector_settings_is_owner ON connector_settings
          USING (user_id = app.current_user_id())
          WITH CHECK (user_id = app.current_user_id());

        CREATE POLICY connector_state_is_owner ON connector_state
          USING (user_id = app.current_user_id())
          WITH CHECK (user_id = app.current_user_id());
        """

_DOWNGRADE = (
    "DROP TABLE IF EXISTS connector_state",
    "DROP TABLE IF EXISTS connector_settings",
)


def _execute_statements(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    op.execute(_UPGRADE_SCHEMA)
    _execute_statements(_GRANTS)
    op.execute("ALTER TABLE connector_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_settings FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_state FORCE ROW LEVEL SECURITY")
    op.execute(_POLICIES)
    # The seed below writes each stored user's connector rows under the
    # forced row-level security of both tables, whose owner policies bind
    # app.current_user_id. For the documented table owner (NOSUPERUSER
    # NOBYPASSRLS) that binding is unset during an upgrade, so the seed
    # selected no users, wrote no rows, and a data-bearing vault ended
    # without the defaults a superuser upgrade writes. Temporarily use the
    # owner bypass, as 0067, 0093 and 0095 to 0097 do, inside Alembic's
    # single transaction. Concurrent sessions never see the relaxed FORCE
    # setting; failure rolls back the bracket, the seed and the schema
    # change together, and the forced setting is restored before the
    # transaction commits.
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_settings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_state NO FORCE ROW LEVEL SECURITY")
    op.execute(_SEED_DEFAULTS)
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_settings FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_state FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    _execute_statements(_DOWNGRADE)
