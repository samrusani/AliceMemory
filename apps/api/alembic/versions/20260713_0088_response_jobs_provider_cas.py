"""Add durable response jobs and provider-configuration fencing.

Revision ID: 20260713_0088
Revises: 20260713_0087

A database already past this revision never runs this body again. A database
still before it must end in exactly the state the superuser path gives: the
same backfilled fingerprints, whether the migrator is a cluster superuser or
the documented table owner that is NOSUPERUSER NOBYPASSRLS. The backfill
reads and writes under forced row-level security that the owner cannot
satisfy unbound, so the owner bypass is used inside this migration's own
transaction, as 0067, 0070, 0092, 0093 and 0095 to 0097 do, and the forced
setting is restored before the transaction commits.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from alicebot_api.provider_configuration import provider_config_fingerprint_v1


revision = "20260713_0088"
down_revision = "20260713_0087"
branch_labels = None
depends_on = None


def _backfill_provider_config_fingerprints() -> None:
    """Backfill with the same versioned canonical algorithm as the runtime."""

    bind = op.get_bind()
    rows = bind.execute(
        text(
            """
            SELECT
              id,
              provider_key,
              model_provider,
              display_name,
              base_url,
              api_key,
              auth_mode,
              default_model,
              status,
              model_list_path,
              healthcheck_path,
              invoke_path,
              azure_api_version,
              azure_auth_secret_ref,
              metadata
            FROM model_providers
            WHERE config_fingerprint_sha256 IS NULL
            """
        )
    ).mappings()
    updates: list[dict[str, object]] = []
    for row in rows:
        metadata = row["metadata"]
        if not isinstance(metadata, dict):
            raise RuntimeError("model provider metadata must be a JSON object")
        updates.append(
            {
                "provider_id": row["id"],
                "config_fingerprint_sha256": provider_config_fingerprint_v1(
                    provider_key=str(row["provider_key"]),
                    model_provider=str(row["model_provider"]),
                    display_name=str(row["display_name"]),
                    base_url=str(row["base_url"]),
                    api_key=str(row["api_key"]),
                    auth_mode=str(row["auth_mode"]),
                    default_model=str(row["default_model"]),
                    status=str(row["status"]),
                    model_list_path=str(row["model_list_path"]),
                    healthcheck_path=str(row["healthcheck_path"]),
                    invoke_path=str(row["invoke_path"]),
                    azure_api_version=str(row["azure_api_version"]),
                    azure_auth_secret_ref=str(row["azure_auth_secret_ref"]),
                    metadata=metadata,
                ),
            }
        )
    if updates:
        bind.execute(
            text(
                """
                UPDATE model_providers
                SET config_fingerprint_sha256 = :config_fingerprint_sha256
                WHERE id = :provider_id
                  AND config_fingerprint_sha256 IS NULL
                """
            ),
            updates,
        )


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_providers
          ADD COLUMN config_revision bigint NOT NULL DEFAULT 1,
          ADD COLUMN config_fingerprint_sha256 text
        """
    )
    # The backfill reads and writes rows on model_providers, and the
    # provider_capabilities update further down reads model_providers and
    # writes provider_capabilities. Both tables carry forced row-level
    # security whose owner policies are bound to
    # app.hosted_workspace_owner_allowed(workspace_id), which no unbound
    # migration connection satisfies, so the backfill silently matched no
    # rows and the SET NOT NULL after it failed for a vault that carries
    # provider rows while a superuser upgrade completed. Temporarily use
    # the owner bypass, as 0067, 0070, 0092, 0093 and 0095 to 0097 do,
    # inside Alembic's single transaction. Concurrent sessions never see
    # the relaxed FORCE setting; failure rolls back the bracket, the
    # backfill and the schema change together, and the forced setting is
    # restored before the transaction commits.
    op.execute("ALTER TABLE model_providers NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_capabilities NO FORCE ROW LEVEL SECURITY")
    _backfill_provider_config_fingerprints()
    op.execute(
        """
        ALTER TABLE model_providers
          ALTER COLUMN config_fingerprint_sha256 SET DEFAULT
            encode(digest(gen_random_uuid()::text, 'sha256'), 'hex'),
          ALTER COLUMN config_fingerprint_sha256 SET NOT NULL,
          ADD CONSTRAINT model_providers_config_revision_check
            CHECK (config_revision >= 1),
          ADD CONSTRAINT model_providers_config_fingerprint_check
            CHECK (config_fingerprint_sha256 ~ '^[0-9a-f]{64}$')
        """
    )
    op.execute(
        """
        CREATE FUNCTION advance_model_provider_config_fence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          active_config_changed boolean;
          revision_changed boolean;
          fingerprint_changed boolean;
        BEGIN
          active_config_changed := ROW(
            NEW.provider_key,
            NEW.model_provider,
            NEW.display_name,
            NEW.base_url,
            NEW.api_key,
            NEW.auth_mode,
            NEW.default_model,
            NEW.status,
            NEW.model_list_path,
            NEW.healthcheck_path,
            NEW.invoke_path,
            NEW.azure_api_version,
            NEW.azure_auth_secret_ref,
            NEW.metadata
          ) IS DISTINCT FROM ROW(
            OLD.provider_key,
            OLD.model_provider,
            OLD.display_name,
            OLD.base_url,
            OLD.api_key,
            OLD.auth_mode,
            OLD.default_model,
            OLD.status,
            OLD.model_list_path,
            OLD.healthcheck_path,
            OLD.invoke_path,
            OLD.azure_api_version,
            OLD.azure_auth_secret_ref,
            OLD.metadata
          );
          revision_changed := NEW.config_revision IS DISTINCT FROM OLD.config_revision;
          fingerprint_changed := NEW.config_fingerprint_sha256
            IS DISTINCT FROM OLD.config_fingerprint_sha256;

          IF NOT active_config_changed THEN
            -- The application may intentionally issue a semantic no-op update.
            -- It advances the revision by exactly one while its deterministic
            -- fingerprint remains unchanged. Other token-only writes are not
            -- valid: in particular, a previous revision/fingerprint pair must
            -- not be restored independently of the active configuration.
            IF fingerprint_changed
              OR (
                revision_changed
                AND NEW.config_revision IS DISTINCT FROM OLD.config_revision + 1
              )
            THEN
              RAISE EXCEPTION
                'model provider config fence tokens cannot be rewound independently of active configuration'
                USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
          END IF;

          IF NOT revision_changed AND NOT fingerprint_changed THEN
            -- A previous binary does not know about either fence column. Keep
            -- its configuration update safe during a rolling deployment.
            NEW.config_revision := OLD.config_revision + 1;
            NEW.config_fingerprint_sha256 := encode(
              digest(gen_random_uuid()::text, 'sha256'),
              'hex'
            );
            RETURN NEW;
          END IF;

          -- Current application writes must advance both tokens as one fence.
          IF NEW.config_revision IS DISTINCT FROM OLD.config_revision + 1
            OR NOT fingerprint_changed
          THEN
            RAISE EXCEPTION
              'model provider config fence tokens must advance with active configuration'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER model_providers_config_fence_trigger
        BEFORE UPDATE ON model_providers
        FOR EACH ROW
        EXECUTE FUNCTION advance_model_provider_config_fence()
        """
    )

    op.execute(
        """
        ALTER TABLE provider_capabilities
          ADD COLUMN provider_config_revision bigint DEFAULT 1,
          ADD COLUMN provider_config_fingerprint_sha256 text DEFAULT
            encode(digest(gen_random_uuid()::text, 'sha256'), 'hex')
        """
    )
    op.execute(
        """
        UPDATE provider_capabilities AS capability
        SET provider_config_revision = provider.config_revision,
            provider_config_fingerprint_sha256 = provider.config_fingerprint_sha256
        FROM model_providers AS provider
        WHERE provider.id = capability.provider_id
        """
    )
    op.execute("ALTER TABLE model_providers FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_capabilities FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        ALTER TABLE provider_capabilities
          ALTER COLUMN provider_config_revision SET NOT NULL,
          ALTER COLUMN provider_config_fingerprint_sha256 SET NOT NULL,
          ADD CONSTRAINT provider_capabilities_config_revision_check
            CHECK (provider_config_revision >= 1),
          ADD CONSTRAINT provider_capabilities_config_fingerprint_check
            CHECK (provider_config_fingerprint_sha256 ~ '^[0-9a-f]{64}$')
        """
    )

    op.execute(
        """
        CREATE TABLE response_generation_jobs (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          workspace_id uuid NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          endpoint text NOT NULL,
          idempotency_key_hash text NOT NULL,
          idempotency_key_preview text NOT NULL,
          request_fingerprint_sha256 text NOT NULL,
          state text NOT NULL DEFAULT 'pending',
          lease_token uuid NULL,
          lease_expires_at timestamptz NULL,
          provider_call_started_at timestamptz NULL,
          user_event_id uuid NULL UNIQUE REFERENCES events(id) ON DELETE RESTRICT,
          user_event_sequence_no integer NULL,
          response_status_code integer NULL,
          response_payload jsonb NULL,
          error_payload jsonb NULL,
          completed_at timestamptz NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          UNIQUE (user_id, endpoint, idempotency_key_hash),
          CONSTRAINT response_generation_jobs_endpoint_check
            CHECK (endpoint IN ('v0_responses', 'v1_runtime_invoke')),
          CONSTRAINT response_generation_jobs_idempotency_hash_check
            CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT response_generation_jobs_request_fingerprint_check
            CHECK (request_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT response_generation_jobs_state_check
            CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
          CONSTRAINT response_generation_jobs_event_sequence_check
            CHECK (user_event_sequence_no IS NULL OR user_event_sequence_no >= 1),
          CONSTRAINT response_generation_jobs_status_code_check
            CHECK (response_status_code IS NULL OR response_status_code BETWEEN 100 AND 599),
          CONSTRAINT response_generation_jobs_running_shape_check
            CHECK (
              state <> 'running'
              OR (
                lease_token IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND provider_call_started_at IS NOT NULL
                AND user_event_id IS NOT NULL
                AND user_event_sequence_no IS NOT NULL
              )
            ),
          CONSTRAINT response_generation_jobs_terminal_shape_check
            CHECK (
              state NOT IN ('succeeded', 'failed')
              OR (
                response_status_code IS NOT NULL
                AND completed_at IS NOT NULL
                AND (
                  (state = 'succeeded' AND response_payload IS NOT NULL AND error_payload IS NULL)
                  OR (state = 'failed' AND error_payload IS NOT NULL)
                )
              )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX response_generation_jobs_user_state_updated_idx
        ON response_generation_jobs (user_id, state, updated_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX response_generation_jobs_running_lease_idx
        ON response_generation_jobs (lease_expires_at ASC, id ASC)
        WHERE state = 'running'
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON response_generation_jobs TO alicebot_app")
    op.execute("ALTER TABLE response_generation_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE response_generation_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY response_generation_jobs_read_own ON response_generation_jobs
          FOR SELECT
          USING (user_id = app.current_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY response_generation_jobs_insert_own ON response_generation_jobs
          FOR INSERT
          WITH CHECK (user_id = app.current_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY response_generation_jobs_update_own ON response_generation_jobs
          FOR UPDATE
          USING (user_id = app.current_user_id())
          WITH CHECK (user_id = app.current_user_id())
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS response_generation_jobs")
    op.execute(
        """
        ALTER TABLE provider_capabilities
          DROP COLUMN IF EXISTS provider_config_fingerprint_sha256,
          DROP COLUMN IF EXISTS provider_config_revision
        """
    )
    op.execute("DROP TRIGGER IF EXISTS model_providers_config_fence_trigger ON model_providers")
    op.execute("DROP FUNCTION IF EXISTS advance_model_provider_config_fence()")
    op.execute(
        """
        ALTER TABLE model_providers
          DROP COLUMN IF EXISTS config_fingerprint_sha256,
          DROP COLUMN IF EXISTS config_revision
        """
    )
