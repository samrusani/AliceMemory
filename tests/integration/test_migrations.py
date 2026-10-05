from __future__ import annotations

from alembic import command
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from uuid import UUID

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.provider_configuration import provider_config_fingerprint
from alicebot_api.vnext_capture import VNextCaptureService, capture_dedupe_key_for_text
from alicebot_api.vnext_project_scope import resolve_source_metadata_project_scope
from alicebot_api.vnext_store import PostgresVNextStore


def test_vnext_kernel_upgrade_backfills_data_bearing_append_only_revisions(database_urls):
    """A real pre-vNext row must survive 0066 -> 0067.

    Revision 0004 installs the append-only trigger, while 0067 has to update
    those same legacy rows to populate the vNext revision columns. This is the
    data-bearing upgrade path that an empty-schema migration smoke cannot
    exercise.
    """
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000101"
    memory_id = "00000000-0000-0000-0000-000000000102"
    revision_id = "00000000-0000-0000-0000-000000000103"

    command.upgrade(config, "20260416_0066")
    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "vnext-upgrade@example.com", "vNext Upgrade"),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids
                )
                VALUES (%s, %s, 'upgrade.seed', '{"text":"before"}'::jsonb,
                        'active', '[]'::jsonb)
                """,
                (memory_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO memory_revisions (
                  id, user_id, memory_id, sequence_no, action, memory_key,
                  previous_value, new_value, source_event_ids, candidate
                )
                VALUES (
                  %s, %s, %s, 1, 'ADD', 'upgrade.seed', NULL,
                  '{"text":"before"}'::jsonb, '[]'::jsonb, '{}'::jsonb
                )
                """,
                (revision_id, user_id, memory_id),
            )

    command.upgrade(config, "20260510_0067")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT revision_number, revision_type, text_before, text_after
                FROM memory_revisions
                WHERE id = %s
                """,
                (revision_id,),
            )
            assert cur.fetchone() == (
                1,
                "created",
                None,
                '{"text": "before"}',
            )
            cur.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'memory_revisions'::regclass
                  AND NOT tgisinternal
                """
            )
            assert cur.fetchall() == [("memory_revisions_append_only",)]

        with pytest.raises(psycopg.errors.RaiseException, match="memory revisions are append-only"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory_revisions SET reason = 'must fail' WHERE id = %s",
                    (revision_id,),
                )


def test_lifecycle_invariant_upgrade_canonicalizes_retry_ids_and_installs_edge_trigger(database_urls):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000111"
    first_id = "00000000-0000-0000-0000-000000000112"
    second_id = "00000000-0000-0000-0000-000000000113"
    edge_id = "00000000-0000-0000-0000-000000000114"
    command.upgrade(config, "20260707_0082")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "lifecycle-upgrade@example.com", "Lifecycle Upgrade"),
            )
            for memory_id, key, created_at in (
                (first_id, "retry.first", "2026-01-01T00:00:00Z"),
                (second_id, "retry.second", "2026-01-02T00:00:00Z"),
            ):
                cur.execute(
                    """
                    INSERT INTO memories (
                      id, user_id, memory_key, value, status, source_event_ids,
                      memory_type, canonical_text, commit_digest,
                      confirmation_id, metadata_json, created_at, updated_at
                    )
                    VALUES (
                      %s, %s, %s, '{"text":"same retry"}'::jsonb, 'active',
                      '[]'::jsonb, 'semantic', 'Same retry', 'duplicate-digest',
                      'duplicate-confirmation',
                      '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}'::jsonb,
                      %s::timestamptz, %s::timestamptz
                    )
                    """,
                    (memory_id, user_id, key, created_at, created_at),
                )
            cur.execute(
                """
                INSERT INTO graph_edges (
                  id, user_id, from_type, from_id, to_type, to_id,
                  edge_type, created_by
                )
                VALUES (%s, %s, 'memory', %s, 'entity', %s, 'mentions', 'test')
                """,
                (edge_id, user_id, first_id, "00000000-0000-0000-0000-000000000115"),
            )

    command.upgrade(config, "20260711_0083")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, commit_digest, confirmation_id, metadata_json
                FROM memories
                WHERE id IN (%s, %s)
                ORDER BY created_at, id
                """,
                (first_id, second_id),
            )
            rows = cur.fetchall()
            assert rows[0][0:3] == (first_id, "duplicate-digest", "duplicate-confirmation")
            assert rows[1][0:3] == (second_id, None, None)
            assert rows[1][3]["lifecycle_migration"]["duplicate_commit_digest_canonical_memory_id"] == first_id
            cur.execute(
                "UPDATE memories SET canonical_text = 'Changed text' WHERE id = %s",
                (first_id,),
            )
            cur.execute("SELECT valid_to IS NOT NULL FROM graph_edges WHERE id = %s", (edge_id,))
            assert cur.fetchone() == (True,)


def _seed_tombstone_and_live_duplicate(conn, *, user_id, tombstone_id, live_id):
    """Older archived/deleted row and newer active row sharing identifiers."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
            (user_id, "tombstone-lifecycle@example.com", "Tombstone Lifecycle"),
        )
        for memory_id, key, created_at, status, deleted_at in (
            (tombstone_id, "retry.old", "2026-01-01T00:00:00Z", "archived", "2026-01-01T01:00:00Z"),
            (live_id, "retry.new", "2026-01-02T00:00:00Z", "active", None),
        ):
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, commit_digest, confirmation_id,
                  metadata_json, created_at, updated_at, deleted_at
                )
                VALUES (
                  %s, %s, %s, '{"text":"same retry"}'::jsonb, %s,
                  '[]'::jsonb, 'semantic', 'Same retry', 'duplicate-digest',
                  'duplicate-confirmation',
                  '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}'::jsonb,
                  %s::timestamptz, %s::timestamptz, %s::timestamptz
                )
                """,
                (memory_id, user_id, key, status, created_at, created_at, deleted_at),
            )


def test_lifecycle_invariant_upgrade_keeps_identifiers_on_live_row_over_tombstone(database_urls):
    """Audit P1 #3: a duplicate identifier must land on the live row, not a tombstone.

    Runtime replay (``get_memory_by_commit_digest`` / ``_by_confirmation_id``)
    filters ``deleted_at IS NULL``. When an older archived/deleted row and a
    newer active row share an identifier, the upgrade must keep it on the live
    row; stranding it on the tombstone makes replay return nothing while the
    partial unique index blocks re-insertion of the same key.
    """
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000131"
    tombstone_id = "00000000-0000-0000-0000-000000000132"
    live_id = "00000000-0000-0000-0000-000000000133"
    command.upgrade(config, "20260707_0082")

    with psycopg.connect(database_urls["admin"]) as conn:
        _seed_tombstone_and_live_duplicate(conn, user_id=user_id, tombstone_id=tombstone_id, live_id=live_id)

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, commit_digest, confirmation_id, metadata_json
                FROM memories
                WHERE id IN (%s, %s)
                """,
                (tombstone_id, live_id),
            )
            by_id = {row["id"]: row for row in cur.fetchall()}
        assert by_id[live_id]["commit_digest"] == "duplicate-digest"
        assert by_id[live_id]["confirmation_id"] == "duplicate-confirmation"
        assert by_id[tombstone_id]["commit_digest"] is None
        assert by_id[tombstone_id]["confirmation_id"] is None
        assert (
            by_id[tombstone_id]["metadata_json"]["lifecycle_migration"]["duplicate_commit_digest_canonical_memory_id"]
            == live_id
        )

        store = PostgresVNextStore(conn)
        replay = store.get_memory_by_commit_digest("duplicate-digest")
        assert replay is not None and str(replay["id"]) == live_id
        confirmed = store.get_memory_by_confirmation_id("duplicate-confirmation")
        assert confirmed is not None and str(confirmed["id"]) == live_id


def test_lifecycle_identifier_repair_corrects_database_mis_upgraded_by_0083(database_urls):
    """The corrective follow-up repairs a database the shipped 0083 mis-upgraded.

    0083 already shipped in v0.9.2, so a database may already carry the
    mis-assignment (identifier stranded on the tombstone). Migration 0084 must
    move it onto the oldest live row, and be safe to re-run on already-corrected
    data.
    """
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000141"
    tombstone_id = "00000000-0000-0000-0000-000000000142"
    live_id = "00000000-0000-0000-0000-000000000143"
    command.upgrade(config, "20260707_0082")

    with psycopg.connect(database_urls["admin"]) as conn:
        _seed_tombstone_and_live_duplicate(conn, user_id=user_id, tombstone_id=tombstone_id, live_id=live_id)

    # Apply only the shipped (buggy) 0083 and document the mis-assignment.
    command.upgrade(config, "20260711_0083")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, commit_digest, confirmation_id, metadata_json
                FROM memories
                WHERE id IN (%s, %s)
                """,
                (tombstone_id, live_id),
            )
            mis = {row["id"]: row for row in cur.fetchall()}
        assert mis[tombstone_id]["commit_digest"] == "duplicate-digest"
        assert mis[tombstone_id]["confirmation_id"] == "duplicate-confirmation"
        assert mis[live_id]["commit_digest"] is None
        assert (
            mis[live_id]["metadata_json"]["lifecycle_migration"]["duplicate_commit_digest_canonical_memory_id"]
            == tombstone_id
        )

    def _assert_corrected() -> None:
        with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, commit_digest, confirmation_id, metadata_json
                    FROM memories
                    WHERE id IN (%s, %s)
                    """,
                    (tombstone_id, live_id),
                )
                fixed = {row["id"]: row for row in cur.fetchall()}
            assert fixed[live_id]["commit_digest"] == "duplicate-digest"
            assert fixed[live_id]["confirmation_id"] == "duplicate-confirmation"
            assert fixed[tombstone_id]["commit_digest"] is None
            assert fixed[tombstone_id]["confirmation_id"] is None
            assert "duplicate_commit_digest_canonical_memory_id" not in fixed[live_id]["metadata_json"].get(
                "lifecycle_migration", {}
            )
            store = PostgresVNextStore(conn)
            assert str(store.get_memory_by_commit_digest("duplicate-digest")["id"]) == live_id
            assert str(store.get_memory_by_confirmation_id("duplicate-confirmation")["id"]) == live_id

    # Corrective follow-up moves the identifiers onto the live row.
    command.upgrade(config, "head")
    _assert_corrected()

    # Safe re-run: downgrade (a no-op that keeps the corrected data) then
    # re-upgrade must leave the corrected state unchanged.
    command.downgrade(config, "20260711_0083")
    command.upgrade(config, "head")
    _assert_corrected()


def test_released_0084_database_upgrades_through_current_head(database_urls):
    """A database already stamped with released 0084 receives the 3+ repair.

    The published 0084 moved the identifiers but left the third row pointing
    at the deleted former holder. Existing v0.9.4 databases will never rerun
    0084, so only the new 0086 revision may repair that stale pointer.
    """
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000151"
    tombstone_id = "00000000-0000-0000-0000-000000000152"
    canonical_live_id = "00000000-0000-0000-0000-000000000153"
    later_live_id = "00000000-0000-0000-0000-000000000154"
    command.upgrade(config, "20260707_0082")

    with psycopg.connect(database_urls["admin"]) as conn:
        _seed_tombstone_and_live_duplicate(
            conn,
            user_id=user_id,
            tombstone_id=tombstone_id,
            live_id=canonical_live_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, commit_digest, confirmation_id,
                  metadata_json, created_at, updated_at
                )
                VALUES (
                  %s, %s, 'retry.third', '{"text":"same retry"}'::jsonb,
                  'active', '[]'::jsonb, 'semantic', 'Same retry',
                  'duplicate-digest', 'duplicate-confirmation',
                  '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}'::jsonb,
                  '2026-01-03T00:00:00Z'::timestamptz,
                  '2026-01-03T00:00:00Z'::timestamptz
                )
                """,
                (later_live_id, user_id),
            )

    # Execute the restored published 0084 and stamp the database exactly as an
    # existing v0.9.4 installation would be before this release upgrade.
    command.upgrade(config, "20260712_0084")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()["version_num"] == "20260712_0084"
            cur.execute(
                """
                SELECT id::text AS id, commit_digest, metadata_json
                FROM memories
                WHERE id IN (%s, %s, %s)
                """,
                (tombstone_id, canonical_live_id, later_live_id),
            )
            released_rows = {row["id"]: row for row in cur.fetchall()}
        assert released_rows[canonical_live_id]["commit_digest"] == "duplicate-digest"
        assert released_rows[tombstone_id]["commit_digest"] is None
        assert released_rows[later_live_id]["commit_digest"] is None
        assert (
            released_rows[later_live_id]["metadata_json"]["lifecycle_migration"][
                "duplicate_commit_digest_canonical_memory_id"
            ]
            == tombstone_id
        )

    def _assert_all_pointers_truthful() -> None:
        with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, commit_digest, confirmation_id, metadata_json
                    FROM memories
                    WHERE id IN (%s, %s, %s)
                    """,
                    (tombstone_id, canonical_live_id, later_live_id),
                )
                rows = {row["id"]: row for row in cur.fetchall()}
            assert rows[canonical_live_id]["commit_digest"] == "duplicate-digest"
            assert rows[canonical_live_id]["confirmation_id"] == "duplicate-confirmation"
            assert rows[tombstone_id]["commit_digest"] is None
            assert rows[later_live_id]["commit_digest"] is None
            for duplicate_id in (tombstone_id, later_live_id):
                migration = rows[duplicate_id]["metadata_json"]["lifecycle_migration"]
                assert migration["duplicate_commit_digest_canonical_memory_id"] == canonical_live_id
                assert migration["duplicate_confirmation_id_canonical_memory_id"] == canonical_live_id
            canonical_migration = rows[canonical_live_id]["metadata_json"].get("lifecycle_migration", {})
            assert "duplicate_commit_digest_canonical_memory_id" not in canonical_migration
            assert "duplicate_confirmation_id_canonical_memory_id" not in canonical_migration

    command.upgrade(config, "head")
    _assert_all_pointers_truthful()

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()["version_num"] == "20261005_0096"

    # Repeat the additive post-release migrations through their downgrade
    # boundary. The already repaired data remains correct and the second
    # upgrade matches nothing.
    command.downgrade(config, "20260713_0085")
    _assert_all_pointers_truthful()
    command.upgrade(config, "head")
    _assert_all_pointers_truthful()


def test_migration_0088_supports_rolling_provider_writes_and_rejects_token_rewind(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    user_account_id = "00000000-0000-0000-0000-000000000131"
    workspace_id = "00000000-0000-0000-0000-000000000132"
    provider_id = "00000000-0000-0000-0000-000000000133"

    command.upgrade(config, "20260713_0087")
    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_accounts (id, email, display_name)
                VALUES (%s, 'migration-0088@example.com', 'Migration 0088')
                """,
                (user_account_id,),
            )
            cur.execute(
                """
                INSERT INTO workspaces (
                  id, owner_user_account_id, slug, name, bootstrap_status
                )
                VALUES (%s, %s, 'migration-0088', 'Migration 0088', 'ready')
                """,
                (workspace_id, user_account_id),
            )
            cur.execute(
                """
                INSERT INTO model_providers (
                  id,
                  workspace_id,
                  created_by_user_account_id,
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
                )
                VALUES (
                  %s, %s, %s, 'openai_compatible', 'openai_responses',
                  'Pre-0088 Provider', 'https://provider.example/v1',
                  'provider_secret_ref:migration-0088', 'bearer', 'gpt-5-mini',
                  'active', '/models', '/models', '/responses', '', '', '{}'::jsonb
                )
                """,
                (provider_id, workspace_id, user_account_id),
            )
            cur.execute(
                """
                INSERT INTO provider_capabilities (
                  workspace_id,
                  provider_id,
                  discovered_by_user_account_id,
                  adapter_key,
                  discovery_status,
                  capability_snapshot,
                  discovery_error
                )
                VALUES (
                  %s, %s, %s, 'openai_compatible', 'ready',
                  '{"models":["pre-0088"]}'::jsonb, NULL
                )
                """,
                (workspace_id, provider_id, user_account_id),
            )

    command.upgrade(config, "head")

    with psycopg.connect(
        database_urls["admin"],
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config_revision, config_fingerprint_sha256
                FROM model_providers
                WHERE id = %s
                """,
                (provider_id,),
            )
            provider_fence = cur.fetchone()
            assert provider_fence is not None
            assert provider_fence["config_revision"] == 1
            expected_migrated_fingerprint = provider_config_fingerprint(
                provider_key="openai_compatible",
                model_provider="openai_responses",
                display_name="Pre-0088 Provider",
                base_url="https://provider.example/v1",
                api_key="provider_secret_ref:migration-0088",
                auth_mode="bearer",
                default_model="gpt-5-mini",
                status="active",
                model_list_path="/models",
                healthcheck_path="/models",
                invoke_path="/responses",
                azure_api_version="",
                azure_auth_secret_ref="",
                metadata={},
            )
            assert provider_fence["config_fingerprint_sha256"] == expected_migrated_fingerprint

            cur.execute(
                """
                SELECT
                  provider_config_revision,
                  provider_config_fingerprint_sha256
                FROM provider_capabilities
                WHERE provider_id = %s
                """,
                (provider_id,),
            )
            backfilled_capability_fence = cur.fetchone()
            assert backfilled_capability_fence == {
                "provider_config_revision": provider_fence["config_revision"],
                "provider_config_fingerprint_sha256": provider_fence["config_fingerprint_sha256"],
            }

            cur.execute(
                "DELETE FROM provider_capabilities WHERE provider_id = %s",
                (provider_id,),
            )

            # This is the exact pre-0088 column set. During a rolling deploy,
            # the previous binary must still be able to take both INSERT and
            # ON CONFLICT paths without knowing about the new fence columns.
            legacy_upsert_sql = """
                INSERT INTO provider_capabilities (
                  workspace_id,
                  provider_id,
                  discovered_by_user_account_id,
                  adapter_key,
                  discovery_status,
                  capability_snapshot,
                  discovery_error,
                  discovered_at,
                  created_at,
                  updated_at
                )
                VALUES (
                  %s, %s, %s, 'openai_compatible', 'ready', %s::jsonb, NULL,
                  clock_timestamp(), clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (provider_id) DO UPDATE
                SET workspace_id = EXCLUDED.workspace_id,
                    discovered_by_user_account_id = EXCLUDED.discovered_by_user_account_id,
                    adapter_key = EXCLUDED.adapter_key,
                    discovery_status = EXCLUDED.discovery_status,
                    capability_snapshot = EXCLUDED.capability_snapshot,
                    discovery_error = EXCLUDED.discovery_error,
                    discovered_at = EXCLUDED.discovered_at,
                    updated_at = clock_timestamp()
                RETURNING id
            """
            cur.execute(
                legacy_upsert_sql,
                (
                    workspace_id,
                    provider_id,
                    user_account_id,
                    '{"models":["legacy-insert"]}',
                ),
            )
            assert cur.fetchone() is not None
            cur.execute(
                legacy_upsert_sql,
                (
                    workspace_id,
                    provider_id,
                    user_account_id,
                    '{"models":["legacy-conflict-update"]}',
                ),
            )
            assert cur.fetchone() is not None

            cur.execute(
                """
                SELECT
                  provider_config_revision,
                  provider_config_fingerprint_sha256,
                  capability_snapshot
                FROM provider_capabilities
                WHERE provider_id = %s
                """,
                (provider_id,),
            )
            rolling_capability = cur.fetchone()
            assert rolling_capability is not None
            assert rolling_capability["provider_config_revision"] == 1
            assert len(rolling_capability["provider_config_fingerprint_sha256"]) == 64
            assert rolling_capability["capability_snapshot"] == {"models": ["legacy-conflict-update"]}

            current_fingerprint = provider_fence["config_fingerprint_sha256"]
            # A migrated row must immediately accept the current application's
            # semantic no-op contract: one revision advance with the exact
            # canonical fingerprint unchanged.
            cur.execute(
                """
                UPDATE model_providers
                SET config_revision = config_revision + 1,
                    config_fingerprint_sha256 = %s,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING config_revision, config_fingerprint_sha256
                """,
                (current_fingerprint, provider_id),
            )
            assert cur.fetchone() == {
                "config_revision": 2,
                "config_fingerprint_sha256": current_fingerprint,
            }

            application_fingerprint = "a" * 64 if current_fingerprint != "a" * 64 else "b" * 64
            cur.execute(
                """
                UPDATE model_providers
                SET display_name = 'Application Updated Provider',
                    config_revision = config_revision + 1,
                    config_fingerprint_sha256 = %s,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING config_revision, config_fingerprint_sha256
                """,
                (application_fingerprint, provider_id),
            )
            assert cur.fetchone() == {
                "config_revision": 3,
                "config_fingerprint_sha256": application_fingerprint,
            }

            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="cannot be rewound independently of active configuration",
            ):
                cur.execute(
                    """
                    UPDATE model_providers
                    SET config_revision = 1,
                        config_fingerprint_sha256 = %s
                    WHERE id = %s
                    """,
                    (current_fingerprint, provider_id),
                )

            cur.execute(
                """
                SELECT config_revision, config_fingerprint_sha256
                FROM model_providers
                WHERE id = %s
                """,
                (provider_id,),
            )
            assert cur.fetchone() == {
                "config_revision": 3,
                "config_fingerprint_sha256": application_fingerprint,
            }

            # A previous binary changes active configuration without mentioning
            # either token. The trigger must advance both on its behalf.
            cur.execute(
                """
                UPDATE model_providers
                SET display_name = 'Legacy Updated Provider',
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING config_revision, config_fingerprint_sha256
                """,
                (provider_id,),
            )
            legacy_update_fence = cur.fetchone()
            assert legacy_update_fence is not None
            assert legacy_update_fence["config_revision"] == 4
            assert legacy_update_fence["config_fingerprint_sha256"] != application_fingerprint

            # The current application can legitimately issue a semantic no-op:
            # the revision advances once while the deterministic fingerprint is
            # unchanged.
            cur.execute(
                """
                UPDATE model_providers
                SET config_revision = config_revision + 1,
                    config_fingerprint_sha256 = %s,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING config_revision, config_fingerprint_sha256
                """,
                (
                    legacy_update_fence["config_fingerprint_sha256"],
                    provider_id,
                ),
            )
            assert cur.fetchone() == {
                "config_revision": 5,
                "config_fingerprint_sha256": legacy_update_fence["config_fingerprint_sha256"],
            }


def test_lifecycle_upgrade_promotes_and_reads_legacy_nested_multi_project_scope(database_urls):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000121"
    memory_id = "00000000-0000-0000-0000-000000000122"
    canonical_memory_id = "00000000-0000-0000-0000-000000000123"
    command.upgrade(config, "20260707_0082")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "legacy-scope@example.com", "Legacy Scope"),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, domain, sensitivity, metadata_json
                )
                VALUES (
                  %s, %s, 'legacy.nested.scope', '{}'::jsonb, 'active',
                  '[]'::jsonb, 'semantic', 'Legacy nested scope memory',
                  'project', 'internal',
                  '{"agentic_memory":{"project_scope":[" alicebot ","hermes","alicebot"]}}'::jsonb
                )
                """,
                (memory_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, domain, sensitivity, project_id,
                  metadata_json
                )
                VALUES (
                  %s, %s, 'canonical.scope', '{}'::jsonb, 'active',
                  '[]'::jsonb, 'semantic', 'Legacy nested scope canonical guard',
                  'project', 'internal', 'alicebot',
                  '{"project_scope":["alicebot"],"agentic_memory":{"project_scope":["alicebot","hermes"]}}'::jsonb
                )
                """,
                (canonical_memory_id, user_id),
            )

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        legacy_store = PostgresVNextStore(conn)
        legacy_row = legacy_store.get_memory(memory_id)
        assert legacy_row is not None
        assert legacy_row["project_scope"] == ["alicebot", "hermes"]
        assert [
            str(row["id"])
            for row in legacy_store.search_memories(
                query="legacy nested scope",
                projects=("hermes",),
            )
        ] == [memory_id]

    command.upgrade(config, "20260711_0083")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id, metadata_json FROM memories WHERE id = %s",
                (memory_id,),
            )
            persisted = cur.fetchone()
            assert persisted is not None
            assert persisted["project_id"] is None
            assert persisted["metadata_json"]["project_scope"] == ["alicebot", "hermes"]
            cur.execute(
                "SELECT metadata_json FROM memories WHERE id = %s",
                (canonical_memory_id,),
            )
            canonical_persisted = cur.fetchone()
            assert canonical_persisted is not None
            assert canonical_persisted["metadata_json"]["project_scope"] == ["alicebot"]

        store = PostgresVNextStore(conn)
        upgraded = store.get_memory(memory_id)
        assert upgraded is not None
        assert upgraded["project_scope"] == ["alicebot", "hermes"]
        assert [
            str(row["id"])
            for row in store.search_memories(
                query="legacy nested scope",
                projects=("hermes",),
            )
        ] == [memory_id]
        assert (
            store.search_memories(
                query="legacy nested scope",
                projects=("unrelated",),
            )
            == []
        )


def test_pre_lifecycle_upgrade_preserves_present_canonical_project_scope_to_head(
    database_urls,
):
    """0082 rows must not resurrect stale nested scope while upgrading to head."""

    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000124"
    row_ids = {
        "empty": "00000000-0000-0000-0000-000000000125",
        "null": "00000000-0000-0000-0000-000000000126",
        "malformed": "00000000-0000-0000-0000-000000000127",
        "nonempty": "00000000-0000-0000-0000-000000000128",
        "absent": "00000000-0000-0000-0000-000000000129",
    }
    stale_project = "stale-project"
    canonical_project = "canonical-project"
    legacy_project = "legacy-project"
    metadata_by_kind = {
        "empty": {
            "project_scope": [],
            "agentic_memory": {"project_scope": [stale_project]},
        },
        "null": {
            "project_scope": None,
            "agentic_memory": {"project_scope": [stale_project]},
        },
        "malformed": {
            "project_scope": "not-an-array",
            "agentic_memory": {"project_scope": [stale_project]},
        },
        "nonempty": {
            "project_scope": [canonical_project],
            "agentic_memory": {"project_scope": [stale_project]},
        },
        "absent": {
            "agentic_memory": {"project_scope": [f" {legacy_project} "]},
        },
    }

    command.upgrade(config, "20260707_0082")
    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "scope-precedence-0083@example.com", "Scope Precedence 0083"),
            )
            for kind, memory_id in row_ids.items():
                cur.execute(
                    """
                    INSERT INTO memories (
                      id, user_id, memory_key, value, status, source_event_ids,
                      memory_type, canonical_text, domain, sensitivity,
                      metadata_json
                    ) VALUES (
                      %s, %s, %s, '{}'::jsonb, 'active', '[]'::jsonb,
                      'semantic', 'Scope precedence migration', 'project',
                      'internal', %s
                    )
                    """,
                    (
                        memory_id,
                        user_id,
                        f"scope.precedence.{kind}",
                        Jsonb(metadata_by_kind[kind]),
                    ),
                )

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, project_id, metadata_json
                FROM memories
                WHERE id = ANY(%s::uuid[])
                """,
                (list(row_ids.values()),),
            )
            persisted = {row["id"]: row for row in cur.fetchall()}

        for kind in ("empty", "null", "malformed", "nonempty"):
            row = persisted[row_ids[kind]]
            assert row["project_id"] is None
            assert row["metadata_json"] == metadata_by_kind[kind]

        legacy_row = persisted[row_ids["absent"]]
        assert legacy_row["project_id"] == legacy_project
        assert legacy_row["metadata_json"] == {
            **metadata_by_kind["absent"],
            "project_scope": [legacy_project],
        }

        store = PostgresVNextStore(conn)
        assert (
            store.search_memories(
                query="scope precedence migration",
                projects=(stale_project,),
                limit=20,
            )
            == []
        )
        assert [
            str(row["id"])
            for row in store.search_memories(
                query="scope precedence migration",
                projects=(canonical_project,),
                limit=20,
            )
        ] == [row_ids["nonempty"]]
        assert [
            str(row["id"])
            for row in store.search_memories(
                query="scope precedence migration",
                projects=(legacy_project,),
                limit=20,
            )
        ] == [row_ids["absent"]]


def test_pre_lifecycle_upgrade_preserves_unicode_project_whitespace_exactly_to_head(
    database_urls,
):
    """0083 must not reinterpret Unicode whitespace as the ASCII contract."""

    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000130"
    memory_id = "00000000-0000-0000-0000-000000000131"
    unicode_scope = "\u2003Alice\u2003"
    metadata = {"agentic_memory": {"project_scope": [unicode_scope]}}

    command.upgrade(config, "20260707_0082")
    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "unicode-scope-0083@example.com", "Unicode Scope 0083"),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, domain, sensitivity, metadata_json
                ) VALUES (
                  %s, %s, 'scope.unicode.0083', '{}'::jsonb, 'active',
                  '[]'::jsonb, 'semantic', 'Unicode project whitespace',
                  'project', 'internal', %s
                )
                """,
                (memory_id, user_id, Jsonb(metadata)),
            )

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id, metadata_json FROM memories WHERE id = %s",
                (memory_id,),
            )
            persisted = cur.fetchone()
    assert persisted == {
        "project_id": unicode_scope,
        "metadata_json": {**metadata, "project_scope": [unicode_scope]},
    }


def test_tool_execution_task_step_linkage_migration_backfills_existing_rows(database_urls):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000001"
    thread_id = "00000000-0000-0000-0000-000000000002"
    trace_id = "00000000-0000-0000-0000-000000000003"
    tool_id = "00000000-0000-0000-0000-000000000004"
    approval_id = "00000000-0000-0000-0000-000000000005"
    task_id = "00000000-0000-0000-0000-000000000006"
    task_step_id = "00000000-0000-0000-0000-000000000007"
    execution_id = "00000000-0000-0000-0000-000000000008"

    command.upgrade(config, "20260313_0020")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (%s, 'migration@example.com', 'Migration User')
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO threads (id, user_id, title)
                VALUES (%s, %s, 'Migration Thread')
                """,
                (thread_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO traces (
                  id,
                  user_id,
                  thread_id,
                  kind,
                  compiler_version,
                  status,
                  limits
                )
                VALUES (
                  %s,
                  %s,
                  %s,
                  'migration.seed',
                  'v0',
                  'completed',
                  '{}'::jsonb
                )
                """,
                (trace_id, user_id, thread_id),
            )
            cur.execute(
                """
                INSERT INTO tools (
                  id,
                  user_id,
                  tool_key,
                  name,
                  description,
                  version,
                  metadata_version,
                  active,
                  tags,
                  action_hints,
                  scope_hints,
                  domain_hints,
                  risk_hints,
                  metadata
                )
                VALUES (
                  %s,
                  %s,
                  'proxy.echo',
                  'Proxy Echo',
                  'Seed tool for migration coverage',
                  '1.0.0',
                  'tool_metadata_v0',
                  TRUE,
                  '[]'::jsonb,
                  '[]'::jsonb,
                  '[]'::jsonb,
                  '[]'::jsonb,
                  '[]'::jsonb,
                  '{}'::jsonb
                )
                """,
                (tool_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO approvals (
                  id,
                  user_id,
                  thread_id,
                  tool_id,
                  task_step_id,
                  status,
                  request,
                  tool,
                  routing,
                  routing_trace_id,
                  resolved_at,
                  resolved_by_user_id
                )
                VALUES (
                  %s,
                  %s,
                  %s,
                  %s,
                  NULL,
                  'approved',
                  '{"action":"echo"}'::jsonb,
                  '{"id":"tool"}'::jsonb,
                  '{"decision":"approval_required"}'::jsonb,
                  %s,
                  now(),
                  %s
                )
                """,
                (approval_id, user_id, thread_id, tool_id, trace_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO tasks (
                  id,
                  user_id,
                  thread_id,
                  tool_id,
                  status,
                  request,
                  tool,
                  latest_approval_id,
                  latest_execution_id
                )
                VALUES (
                  %s,
                  %s,
                  %s,
                  %s,
                  'approved',
                  '{"action":"echo"}'::jsonb,
                  '{"id":"tool"}'::jsonb,
                  %s,
                  NULL
                )
                """,
                (task_id, user_id, thread_id, tool_id, approval_id),
            )
            cur.execute(
                """
                INSERT INTO task_steps (
                  id,
                  user_id,
                  task_id,
                  sequence_no,
                  kind,
                  status,
                  request,
                  outcome,
                  trace_id,
                  trace_kind
                )
                VALUES (
                  %s,
                  %s,
                  %s,
                  1,
                  'governed_request',
                  'approved',
                  '{"action":"echo"}'::jsonb,
                  '{"routing_decision":"approval_required","approval_id":"00000000-0000-0000-0000-000000000005","approval_status":"approved","execution_id":null,"execution_status":null,"blocked_reason":null}'::jsonb,
                  %s,
                  'migration.seed'
                )
                """,
                (task_step_id, user_id, task_id, trace_id),
            )
            cur.execute(
                """
                INSERT INTO tool_executions (
                  id,
                  user_id,
                  approval_id,
                  thread_id,
                  tool_id,
                  trace_id,
                  request_event_id,
                  result_event_id,
                  status,
                  handler_key,
                  request,
                  tool,
                  result
                )
                VALUES (
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  NULL,
                  NULL,
                  'blocked',
                  NULL,
                  '{"action":"echo"}'::jsonb,
                  '{"id":"tool"}'::jsonb,
                  '{"blocked_reason":"seed"}'::jsonb
                )
                """,
                (execution_id, user_id, approval_id, thread_id, tool_id, trace_id),
            )
        conn.commit()

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_step_id
                FROM tool_executions
                WHERE id = %s
                """,
                (execution_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert str(row[0]) == task_step_id
            cur.execute(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'tool_executions'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchone() == ("NO",)


def test_gmail_account_credentials_migration_round_trip_preserves_tokens(database_urls):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000101"
    gmail_account_id = "00000000-0000-0000-0000-000000000102"

    command.upgrade(config, "20260316_0026")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (%s, 'gmail-migration@example.com', 'Gmail Migration User')
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO gmail_accounts (
                  id,
                  user_id,
                  provider_account_id,
                  email_address,
                  display_name,
                  scope,
                  access_token
                )
                VALUES (
                  %s,
                  %s,
                  'acct-migration-001',
                  'owner@gmail.example',
                  'Owner',
                  'https://www.googleapis.com/auth/gmail.readonly',
                  'token-before-hardening'
                )
                """,
                (gmail_account_id, user_id),
            )
        conn.commit()

    command.upgrade(config, "20260316_0027")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'gmail_accounts'
                  AND column_name = 'access_token'
                """
            )
            assert cur.fetchone() is None
            cur.execute(
                """
                SELECT
                  auth_kind,
                  credential_blob ->> 'credential_kind',
                  credential_blob ->> 'access_token'
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == (
                "oauth_access_token",
                "gmail_oauth_access_token_v1",
                "token-before-hardening",
            )

    command.downgrade(config, "20260316_0026")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'gmail_accounts'
                  AND column_name = 'access_token'
                """
            )
            assert cur.fetchone() == ("access_token",)
            cur.execute(
                """
                SELECT access_token
                FROM gmail_accounts
                WHERE id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == ("token-before-hardening",)
            cur.execute("SELECT to_regclass('public.gmail_account_credentials')")
            assert cur.fetchone() == (None,)


def test_gmail_refresh_token_lifecycle_migration_round_trip_preserves_downgrade_compatibility(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000201"
    gmail_account_id = "00000000-0000-0000-0000-000000000202"

    command.upgrade(config, "20260316_0027")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (%s, 'gmail-refresh@example.com', 'Gmail Refresh User')
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO gmail_accounts (
                  id,
                  user_id,
                  provider_account_id,
                  email_address,
                  display_name,
                  scope
                )
                VALUES (
                  %s,
                  %s,
                  'acct-refresh-001',
                  'owner@gmail.example',
                  'Owner',
                  'https://www.googleapis.com/auth/gmail.readonly'
                )
                """,
                (gmail_account_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO gmail_account_credentials (
                  gmail_account_id,
                  user_id,
                  auth_kind,
                  credential_blob
                )
                VALUES (
                  %s,
                  %s,
                  'oauth_access_token',
                  jsonb_build_object(
                    'credential_kind', 'gmail_oauth_access_token_v1',
                    'access_token', 'token-before-refresh-lifecycle'
                  )
                )
                """,
                (gmail_account_id, user_id),
            )
        conn.commit()

    command.upgrade(config, "20260316_0028")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gmail_account_credentials
                SET credential_blob = jsonb_build_object(
                  'credential_kind', 'gmail_oauth_refresh_token_v2',
                  'access_token', 'token-after-refresh',
                  'refresh_token', 'refresh-001',
                  'client_id', 'client-001',
                  'client_secret', 'secret-001',
                  'access_token_expires_at', '2030-01-01T00:05:00+00:00'
                )
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            cur.execute(
                """
                SELECT
                  credential_blob ->> 'credential_kind',
                  credential_blob ->> 'access_token',
                  credential_blob ->> 'refresh_token'
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == (
                "gmail_oauth_refresh_token_v2",
                "token-after-refresh",
                "refresh-001",
            )
        conn.commit()

    command.downgrade(config, "20260316_0027")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_blob ->> 'credential_kind',
                  credential_blob ->> 'access_token',
                  credential_blob ? 'refresh_token'
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == (
                "gmail_oauth_access_token_v1",
                "token-after-refresh",
                False,
            )


def test_gmail_external_secret_manager_migration_round_trip_preserves_legacy_transition_rows(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000301"
    gmail_account_id = "00000000-0000-0000-0000-000000000302"

    command.upgrade(config, "20260316_0028")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (%s, 'gmail-secret-manager@example.com', 'Gmail Secret Manager User')
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO gmail_accounts (
                  id,
                  user_id,
                  provider_account_id,
                  email_address,
                  display_name,
                  scope
                )
                VALUES (
                  %s,
                  %s,
                  'acct-secret-manager-001',
                  'owner@gmail.example',
                  'Owner',
                  'https://www.googleapis.com/auth/gmail.readonly'
                )
                """,
                (gmail_account_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO gmail_account_credentials (
                  gmail_account_id,
                  user_id,
                  auth_kind,
                  credential_blob
                )
                VALUES (
                  %s,
                  %s,
                  'oauth_access_token',
                  jsonb_build_object(
                    'credential_kind', 'gmail_oauth_refresh_token_v2',
                    'access_token', 'token-before-externalization',
                    'refresh_token', 'refresh-001',
                    'client_id', 'client-001',
                    'client_secret', 'secret-001',
                    'access_token_expires_at', '2030-01-01T00:05:00+00:00'
                  )
                )
                """,
                (gmail_account_id, user_id),
            )
        conn.commit()

    command.upgrade(config, "20260316_0029")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob ->> 'access_token'
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == (
                "gmail_oauth_refresh_token_v2",
                "legacy_db_v0",
                None,
                "token-before-externalization",
            )

    command.downgrade(config, "20260316_0028")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_blob ->> 'credential_kind',
                  credential_blob ->> 'access_token',
                  credential_blob ->> 'refresh_token'
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (gmail_account_id,),
            )
            assert cur.fetchone() == (
                "gmail_oauth_refresh_token_v2",
                "token-before-externalization",
                "refresh-001",
            )


def test_calendar_account_migration_round_trip_preserves_table_shape(database_urls):
    config = make_alembic_config(database_urls["admin"])
    user_id = "00000000-0000-0000-0000-000000000401"
    calendar_account_id = "00000000-0000-0000-0000-000000000402"

    command.upgrade(config, "20260316_0029")
    command.upgrade(config, "20260319_0030")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name)
                VALUES (%s, 'calendar-migration@example.com', 'Calendar Migration User')
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO calendar_accounts (
                  id,
                  user_id,
                  provider_account_id,
                  email_address,
                  display_name,
                  scope
                )
                VALUES (
                  %s,
                  %s,
                  'acct-calendar-001',
                  'owner@gmail.example',
                  'Owner',
                  'https://www.googleapis.com/auth/calendar.readonly'
                )
                """,
                (calendar_account_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO calendar_account_credentials (
                  calendar_account_id,
                  user_id,
                  auth_kind,
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob
                )
                VALUES (
                  %s,
                  %s,
                  'oauth_access_token',
                  'calendar_oauth_access_token_v1',
                  'file_v1',
                  'users/00000000-0000-0000-0000-000000000401/calendar-account-credentials/cred.json',
                  NULL
                )
                """,
                (calendar_account_id, user_id),
            )
        conn.commit()

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  auth_kind,
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob IS NULL
                FROM calendar_account_credentials
                WHERE calendar_account_id = %s
                """,
                (calendar_account_id,),
            )
            assert cur.fetchone() == (
                "oauth_access_token",
                "calendar_oauth_access_token_v1",
                "file_v1",
                "users/00000000-0000-0000-0000-000000000401/calendar-account-credentials/cred.json",
                True,
            )

    command.downgrade(config, "20260316_0029")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.calendar_account_credentials')")
            assert cur.fetchone() == (None,)
            cur.execute("SELECT to_regclass('public.calendar_accounts')")
            assert cur.fetchone() == (None,)


def test_migrations_upgrade_and_downgrade(database_urls):
    config = make_alembic_config(database_urls["admin"])

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            assert cur.fetchone()[0] == "users"
            cur.execute("SELECT to_regclass('public.threads')")
            assert cur.fetchone()[0] == "threads"
            cur.execute("SELECT to_regclass('public.sessions')")
            assert cur.fetchone()[0] == "sessions"
            cur.execute("SELECT to_regclass('public.events')")
            assert cur.fetchone()[0] == "events"
            cur.execute("SELECT to_regclass('public.memories')")
            assert cur.fetchone()[0] == "memories"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'memories'
                  AND column_name IN (
                    'memory_type',
                    'confidence',
                    'salience',
                    'confirmation_status',
                    'trust_class',
                    'promotion_eligibility',
                    'evidence_count',
                    'independent_source_count',
                    'extracted_by_model',
                    'trust_reason',
                    'valid_from',
                    'valid_to',
                    'last_confirmed_at'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == [
                ("confidence",),
                ("confirmation_status",),
                ("evidence_count",),
                ("extracted_by_model",),
                ("independent_source_count",),
                ("last_confirmed_at",),
                ("memory_type",),
                ("promotion_eligibility",),
                ("salience",),
                ("trust_class",),
                ("trust_reason",),
                ("valid_from",),
                ("valid_to",),
            ]
            cur.execute("SELECT to_regclass('public.memory_revisions')")
            assert cur.fetchone()[0] == "memory_revisions"
            cur.execute("SELECT to_regclass('public.memory_review_labels')")
            assert cur.fetchone()[0] == "memory_review_labels"
            cur.execute("SELECT to_regclass('public.entities')")
            assert cur.fetchone()[0] == "entities"
            cur.execute("SELECT to_regclass('public.entity_edges')")
            assert cur.fetchone()[0] == "entity_edges"
            cur.execute("SELECT to_regclass('public.embedding_configs')")
            assert cur.fetchone()[0] == "embedding_configs"
            cur.execute("SELECT to_regclass('public.memory_embeddings')")
            assert cur.fetchone()[0] == "memory_embeddings"
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] == "consents"
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] == "policies"
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] == "tools"
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] == "approvals"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'approvals'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchall() == [("task_step_id",)]
            cur.execute("SELECT to_regclass('public.tasks')")
            assert cur.fetchone()[0] == "tasks"
            cur.execute("SELECT to_regclass('public.task_workspaces')")
            assert cur.fetchone()[0] == "task_workspaces"
            cur.execute("SELECT to_regclass('public.task_artifacts')")
            assert cur.fetchone()[0] == "task_artifacts"
            cur.execute("SELECT to_regclass('public.task_artifact_chunks')")
            assert cur.fetchone()[0] == "task_artifact_chunks"
            cur.execute("SELECT to_regclass('public.task_artifact_chunk_embeddings')")
            assert cur.fetchone()[0] == "task_artifact_chunk_embeddings"
            cur.execute("SELECT to_regclass('public.task_steps')")
            assert cur.fetchone()[0] == "task_steps"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'task_steps'
                  AND column_name IN (
                    'parent_step_id',
                    'source_approval_id',
                    'source_execution_id'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == [
                ("parent_step_id",),
                ("source_approval_id",),
                ("source_execution_id",),
            ]
            cur.execute("SELECT to_regclass('public.tool_executions')")
            assert cur.fetchone()[0] == "tool_executions"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'tool_executions'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchall() == [("task_step_id",)]
            cur.execute("SELECT to_regclass('public.execution_budgets')")
            assert cur.fetchone()[0] == "execution_budgets"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'execution_budgets'
                  AND column_name IN (
                    'status',
                    'deactivated_at',
                    'superseded_by_budget_id',
                    'supersedes_budget_id'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == [
                ("deactivated_at",),
                ("status",),
                ("superseded_by_budget_id",),
                ("supersedes_budget_id",),
            ]
            cur.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class AS c
                JOIN pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN (
                    'users',
                    'threads',
                    'sessions',
                    'events',
                    'memories',
                    'memory_revisions',
                    'memory_review_labels',
                    'entities',
                    'entity_edges',
                    'embedding_configs',
                    'memory_embeddings',
                    'consents',
                    'policies',
                    'tools',
                    'approvals',
                    'tasks',
                    'task_workspaces',
                    'task_artifacts',
                    'task_artifact_chunks',
                    'task_artifact_chunk_embeddings',
                    'task_steps',
                    'execution_budgets',
                    'tool_executions'
                  )
                ORDER BY c.relname
                """
            )
            assert cur.fetchall() == [
                ("approvals", True, True),
                ("consents", True, True),
                ("embedding_configs", True, True),
                ("entities", True, True),
                ("entity_edges", True, True),
                ("events", True, True),
                ("execution_budgets", True, True),
                ("memories", True, True),
                ("memory_embeddings", True, True),
                ("memory_review_labels", True, True),
                ("memory_revisions", True, True),
                ("policies", True, True),
                ("sessions", True, True),
                ("task_artifact_chunk_embeddings", True, True),
                ("task_artifact_chunks", True, True),
                ("task_artifacts", True, True),
                ("task_steps", True, True),
                ("task_workspaces", True, True),
                ("tasks", True, True),
                ("threads", True, True),
                ("tool_executions", True, True),
                ("tools", True, True),
                ("users", True, True),
            ]
            cur.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'events'::regclass
                  AND NOT tgisinternal
                """
            )
            assert cur.fetchall() == [("events_append_only",)]
            cur.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'memory_revisions'::regclass
                  AND NOT tgisinternal
                """
            )
            assert cur.fetchall() == [("memory_revisions_append_only",)]
            cur.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'memory_review_labels'::regclass
                  AND NOT tgisinternal
                """
            )
            assert cur.fetchall() == [("memory_review_labels_append_only",)]
            cur.execute(
                """
                SELECT
                  has_table_privilege('alicebot_app', 'users', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'threads', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'sessions', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'memories', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'memory_revisions', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'memory_revisions', 'DELETE'),
                  has_table_privilege('alicebot_app', 'memory_review_labels', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'memory_review_labels', 'DELETE'),
                  has_table_privilege('alicebot_app', 'entities', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'entities', 'DELETE'),
                  has_table_privilege('alicebot_app', 'entity_edges', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'entity_edges', 'DELETE'),
                  has_table_privilege('alicebot_app', 'embedding_configs', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'embedding_configs', 'DELETE'),
                  has_table_privilege('alicebot_app', 'memory_embeddings', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'memory_embeddings', 'DELETE'),
                  has_table_privilege('alicebot_app', 'consents', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'consents', 'DELETE'),
                  has_table_privilege('alicebot_app', 'policies', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'policies', 'DELETE'),
                  has_table_privilege('alicebot_app', 'tools', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'tools', 'DELETE'),
                  has_table_privilege('alicebot_app', 'approvals', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'approvals', 'DELETE'),
                  has_table_privilege('alicebot_app', 'tasks', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'tasks', 'DELETE'),
                  has_table_privilege('alicebot_app', 'task_workspaces', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'task_workspaces', 'DELETE'),
                  has_table_privilege('alicebot_app', 'task_artifacts', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'task_artifacts', 'DELETE'),
                  has_table_privilege('alicebot_app', 'task_artifact_chunks', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'task_artifact_chunks', 'DELETE'),
                  has_table_privilege('alicebot_app', 'task_artifact_chunk_embeddings', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'task_artifact_chunk_embeddings', 'DELETE'),
                  has_table_privilege('alicebot_app', 'task_steps', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'task_steps', 'DELETE'),
                  has_table_privilege('alicebot_app', 'execution_budgets', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'execution_budgets', 'DELETE'),
                  has_table_privilege('alicebot_app', 'tool_executions', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'tool_executions', 'DELETE')
                """
            )
            assert cur.fetchone() == (
                False,
                False,
                False,
                True,
                # memory_revisions UPDATE: granted by migration 20260706_0079
                # for the trigger-guarded redaction mode; the append-only
                # trigger still rejects every non-redaction UPDATE.
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                True,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                True,
                False,
                False,
                False,
                True,
                False,
                False,
                False,
                True,
                False,
                True,
                False,
                True,
                False,
                False,
                False,
            )

    command.downgrade(config, "20260314_0024")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.task_artifact_chunk_embeddings')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.task_artifact_chunks')")
            assert cur.fetchone()[0] == "task_artifact_chunks"
            cur.execute("SELECT to_regclass('public.task_artifacts')")
            assert cur.fetchone()[0] == "task_artifacts"
            cur.execute("SELECT to_regclass('public.task_workspaces')")
            assert cur.fetchone()[0] == "task_workspaces"

    command.downgrade(config, "20260313_0021")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.task_artifact_chunks')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.task_artifact_chunk_embeddings')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.task_artifacts')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.task_workspaces')")
            assert cur.fetchone()[0] is None
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'approvals'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchall() == [("task_step_id",)]

    command.downgrade(config, "20260313_0018")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'approvals'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchall() == []
            cur.execute("SELECT to_regclass('public.task_steps')")
            assert cur.fetchone()[0] == "task_steps"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'tool_executions'
                  AND column_name = 'task_step_id'
                """
            )
            assert cur.fetchall() == []
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'task_steps'
                  AND column_name IN (
                    'parent_step_id',
                    'source_approval_id',
                    'source_execution_id'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == []
            cur.execute("SELECT to_regclass('public.tasks')")
            assert cur.fetchone()[0] == "tasks"

    command.downgrade(config, "20260313_0017")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.task_steps')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tasks')")
            assert cur.fetchone()[0] == "tasks"

    command.downgrade(config, "20260313_0014")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.execution_budgets')")
            assert cur.fetchone()[0] == "execution_budgets"
            cur.execute("SELECT to_regclass('public.tasks')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.task_steps')")
            assert cur.fetchone()[0] is None
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'execution_budgets'
                  AND column_name IN (
                    'status',
                    'deactivated_at',
                    'superseded_by_budget_id',
                    'supersedes_budget_id'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == []
            cur.execute("SELECT has_table_privilege('alicebot_app', 'execution_budgets', 'UPDATE')")
            assert cur.fetchone()[0] is False

    command.downgrade(config, "20260313_0013")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.execution_budgets')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tool_executions')")
            assert cur.fetchone()[0] == "tool_executions"

    command.downgrade(config, "20260312_0012")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.tool_executions')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] == "approvals"

    command.downgrade(config, "20260312_0011")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] == "approvals"
            cur.execute(
                """
                SELECT
                  has_table_privilege('alicebot_app', 'approvals', 'UPDATE'),
                  EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'approvals'
                      AND column_name = 'resolved_at'
                  ),
                  EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'approvals'
                      AND column_name = 'resolved_by_user_id'
                  )
                """
            )
            assert cur.fetchone() == (
                False,
                False,
                False,
            )

    command.downgrade(config, "20260312_0010")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] == "tools"
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] == "consents"
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] == "policies"

    command.downgrade(config, "20260312_0009")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] == "consents"
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] == "policies"

    command.downgrade(config, "20260312_0008")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.embedding_configs')")
            assert cur.fetchone()[0] == "embedding_configs"
            cur.execute("SELECT to_regclass('public.memory_embeddings')")
            assert cur.fetchone()[0] == "memory_embeddings"

    command.downgrade(config, "20260312_0007")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.embedding_configs')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_embeddings')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.approvals')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memories')")
            assert cur.fetchone()[0] == "memories"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'memories'
                  AND column_name IN (
                    'memory_type',
                    'confidence',
                    'salience',
                    'confirmation_status',
                    'valid_from',
                    'valid_to',
                    'last_confirmed_at'
                  )
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == []
            cur.execute("SELECT to_regclass('public.entity_edges')")
            assert cur.fetchone()[0] == "entity_edges"

    command.downgrade(config, "20260311_0003")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memories')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_revisions')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_review_labels')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.entities')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.entity_edges')")
            assert cur.fetchone()[0] is None
            cur.execute(
                """
                SELECT
                  has_table_privilege('alicebot_app', 'users', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'threads', 'UPDATE'),
                  has_table_privilege('alicebot_app', 'sessions', 'UPDATE')
                """
            )
            # Revision 20260310_0001 already leaves the runtime role without UPDATE
            # access, so downgrading from head must preserve that same privilege floor.
            assert cur.fetchone() == (False, False, False)

    command.downgrade(config, "20260310_0001")

    command.downgrade(config, "base")

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.threads')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.sessions')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.events')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memories')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_revisions')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_review_labels')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.entities')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.entity_edges')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.embedding_configs')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.memory_embeddings')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.consents')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.policies')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.tools')")
            assert cur.fetchone()[0] is None
            cur.execute(
                """
                SELECT extname
                FROM pg_extension
                WHERE extname IN ('pgcrypto', 'vector')
                ORDER BY extname
                """
            )
            assert [row[0] for row in cur.fetchall()] == ["pgcrypto", "vector"]


def test_project_scope_identity_upgrade_repairs_dedupe_without_widening_empty_scope(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260713_0089")
    user_id = "00000000-0000-0000-0000-000000000301"
    source_a = "00000000-0000-0000-0000-000000000302"
    source_b = "00000000-0000-0000-0000-000000000303"
    memory_id = "00000000-0000-0000-0000-000000000304"
    stale_project = "00000000-0000-0000-0000-000000000305"
    raw_text = "\tFact: migration scope identity is deterministic.\n"
    empty_metadata = {
        "project_scope": [],
        "agentic_memory": {"project_scope": [stale_project]},
    }
    unicode_sources = {
        scope: f"00000000-0000-0000-0003-{index:012d}"
        for index, scope in enumerate(
            ("İ", "i", "Straße", "STRASSE", "Σ", "σ", "ς"),
            start=1,
        )
    }
    numeric_sources = (
        ("00000000-0000-0000-0004-000000000001", "2026-01-10T00:00:00Z", [1]),
        ("00000000-0000-0000-0004-000000000002", "2026-01-11T00:00:00Z", [1.0]),
        ("00000000-0000-0000-0004-000000000003", "2026-01-12T00:00:00Z", [-0.0]),
        ("00000000-0000-0000-0004-000000000004", "2026-01-13T00:00:00Z", [0]),
        ("00000000-0000-0000-0004-000000000005", "2026-01-14T00:00:00Z", [True]),
    )

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "scope-0090@example.com", "Scope 0090"),
            )
            for source_id, captured_at, scope, old_key in (
                (
                    source_a,
                    "2026-01-01T00:00:00Z",
                    [" Beta ", "ALICE", "alice"],
                    "capture-md5:old-a",
                ),
                (
                    source_b,
                    "2026-01-02T00:00:00Z",
                    ["alice", "beta"],
                    "capture-md5:old-b",
                ),
            ):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_id,
                        user_id,
                        f"sha256:{source_id}",
                        old_key,
                        captured_at,
                        Jsonb({"raw_text": raw_text, "project_scope": scope}),
                    ),
                )
            for index, (scope, source_id) in enumerate(unicode_sources.items(), start=3):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_id,
                        user_id,
                        f"sha256:{source_id}",
                        f"capture-md5:old-unicode-{index}",
                        f"2026-01-{index:02d}T00:00:00Z",
                        Jsonb({"raw_text": raw_text, "project_scope": [scope]}),
                    ),
                )
            for index, (source_id, captured_at, scope) in enumerate(numeric_sources, start=1):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_id,
                        user_id,
                        f"sha256:{source_id}",
                        f"capture-md5:old-numeric-{index}",
                        captured_at,
                        Jsonb({"raw_text": raw_text, "project_scope": scope}),
                    ),
                )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, domain, sensitivity, project_id,
                  metadata_json
                ) VALUES (
                  %s, %s, 'scope.0090.empty', '{}'::jsonb, 'active',
                  '[]'::jsonb, 'semantic', '0090 explicit empty', 'project',
                  'internal', %s::uuid, %s
                )
                """,
                (memory_id, user_id, stale_project, Jsonb(empty_metadata)),
            )

    command.upgrade(config, "head")

    expected_key = capture_dedupe_key_for_text(
        raw_text,
        ("alice", "beta"),
        domain="project",
        sensitivity="private",
    )
    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                ORDER BY captured_at, id
                """,
                ([source_a, source_b],),
            )
            assert cur.fetchall() == [
                {"id": source_a, "dedupe_key": expected_key},
                {"id": source_b, "dedupe_key": None},
            ]
            cur.execute(
                """
                SELECT id::text AS id, metadata_json -> 'project_scope' ->> 0 AS scope,
                       dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                ORDER BY captured_at, id
                """,
                (list(unicode_sources.values()),),
            )
            repaired_unicode = {row["scope"]: row for row in cur.fetchall()}
            assert set(repaired_unicode) == set(unicode_sources)
            for scope, source_id in unicode_sources.items():
                assert repaired_unicode[scope] == {
                    "id": source_id,
                    "scope": scope,
                    "dedupe_key": capture_dedupe_key_for_text(
                        raw_text,
                        (scope,),
                        domain="project",
                        sensitivity="private",
                    ),
                }
            assert len({row["dedupe_key"] for row in repaired_unicode.values()}) == 7
            cur.execute(
                """
                SELECT id::text AS id, dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                ORDER BY captured_at, id
                """,
                ([source_id for source_id, _captured_at, _scope in numeric_sources],),
            )
            repaired_numeric = cur.fetchall()
            assert repaired_numeric == [
                {
                    "id": numeric_sources[0][0],
                    "dedupe_key": capture_dedupe_key_for_text(
                        raw_text,
                        ("1",),
                        domain="project",
                        sensitivity="private",
                    ),
                },
                {"id": numeric_sources[1][0], "dedupe_key": None},
                {
                    "id": numeric_sources[2][0],
                    "dedupe_key": capture_dedupe_key_for_text(
                        raw_text,
                        ("0",),
                        domain="project",
                        sensitivity="private",
                    ),
                },
                {"id": numeric_sources[3][0], "dedupe_key": None},
                {
                    "id": numeric_sources[4][0],
                    "dedupe_key": capture_dedupe_key_for_text(
                        raw_text,
                        ("True",),
                        domain="project",
                        sensitivity="private",
                    ),
                },
            ]
            cur.execute(
                "SELECT project_id::text AS project_id, metadata_json FROM memories WHERE id = %s",
                (memory_id,),
            )
            assert cur.fetchone() == {
                "project_id": stale_project,
                "metadata_json": empty_metadata,
            }


def test_project_scope_identity_upgrade_resolves_all_legacy_source_forms_and_blocks_recapture(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260713_0089")
    user_id = "00000000-0000-0000-0000-000000000331"
    target_scope = "Legacy Project"
    cases = {
        "root_canonical": {"project_scope": [f" {target_scope} "]},
        "metadata_canonical": {"metadata_json": {"project_scope": [f" {target_scope} "]}},
        "scope_canonical": {"scope_json": {"project_scope": [f" {target_scope} "]}},
        "direct_identity_scope": {
            "project_id": "stale-project",
            "agent_identity": {"project_scope": [f" {target_scope} "]},
        },
        "metadata_agentic_scope": {"metadata_json": {"agentic_memory": {"project_scope": [f" {target_scope} "]}}},
        "root_project_alias": {"project": f" {target_scope} "},
        "metadata_project_id_alias": {"metadata_json": {"project_id": f" {target_scope} "}},
        "scope_agentic_projects_alias": {"scope_json": {"agentic_memory": {"projects": [f" {target_scope} "]}}},
    }
    source_ids = {name: f"00000000-0000-0000-0004-{index:012d}" for index, name in enumerate(cases, start=1)}
    raw_texts = {name: f"Fact: {name} keeps its migrated source scope." for name in cases}

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "legacy-source-forms@example.com", "Legacy source forms"),
            )
            for index, (name, scope_metadata) in enumerate(cases.items(), start=1):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_ids[name],
                        user_id,
                        f"sha256:legacy-source-{index}",
                        f"capture-md5:pre-0090-{index}",
                        f"2026-02-{index:02d}T00:00:00Z",
                        Jsonb({"raw_text": raw_texts[name], **scope_metadata}),
                    ),
                )

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                """,
                (list(source_ids.values()),),
            )
            repaired = {row["id"]: row["dedupe_key"] for row in cur.fetchall()}
    for name, source_id in source_ids.items():
        scoped_key = capture_dedupe_key_for_text(
            raw_texts[name],
            (target_scope,),
            domain="project",
            sensitivity="private",
        )
        assert repaired[source_id] == scoped_key
        assert repaired[source_id] != capture_dedupe_key_for_text(
            raw_texts[name],
            domain="project",
            sensitivity="private",
        )

    with user_connection(database_urls["app"], UUID(user_id)) as conn:
        service = VNextCaptureService(PostgresVNextStore(conn))
        for name, source_id in source_ids.items():
            result = service.capture_text(
                raw_texts[name],
                project_scope=(target_scope,),
                domain="project",
                sensitivity="private",
            )
            assert result.duplicate is True
            assert result.source_id == source_id
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM sources WHERE deleted_at IS NULL")
            assert cur.fetchone()["count"] == len(cases)


def test_project_scope_identity_upgrade_keeps_present_empty_nested_source_scope_authoritative(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260713_0089")
    user_id = "00000000-0000-0000-0000-000000000351"
    stale_project = "Legacy Project"
    cases: dict[str, dict[str, object]] = {
        "nested_blank": {
            "project_id": stale_project,
            "agent_identity": {"project_scope": ["\t\n"]},
        },
        "nested_null": {
            "project_id": stale_project,
            "agentic_memory": {"project_scope": None},
        },
        "nested_malformed": {
            "project_id": stale_project,
            "agent_identity": {"project_scope": {"leak": stale_project}},
        },
        "nested_fractional": {
            "project_id": stale_project,
            "agentic_memory": {"project_scope": 1.5},
        },
        "nested_merged_valid": {
            "project_id": stale_project,
            "agentic_memory": {"project_scope": " Alpha "},
            "agent_identity": {"project_scope": [7, 1e1, True]},
        },
    }
    source_ids = {name: f"00000000-0000-0000-0005-{index:012d}" for index, name in enumerate(cases, start=1)}
    raw_texts = {name: f"Fact: migration {name} preserves nested scope presence." for name in cases}

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "nested-presence-0090@example.com", "Nested presence 0090"),
            )
            for index, (name, metadata) in enumerate(cases.items(), start=1):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_ids[name],
                        user_id,
                        f"sha256:nested-presence-{index}",
                        f"capture-md5:pre-0090-nested-{index}",
                        f"2026-03-{index:02d}T00:00:00Z",
                        Jsonb({"raw_text": raw_texts[name], **metadata}),
                    ),
                )

    command.upgrade(config, "20260714_0090")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                """,
                (list(source_ids.values()),),
            )
            repaired = {row["id"]: row["dedupe_key"] for row in cur.fetchall()}

    for name, metadata in cases.items():
        resolution = resolve_source_metadata_project_scope(metadata)
        assert resolution.present is True
        if name == "nested_merged_valid":
            assert resolution.values == ("Alpha", "7", "10", "True")
        else:
            assert resolution.values == ()
        assert repaired[source_ids[name]] == capture_dedupe_key_for_text(
            raw_texts[name],
            resolution.values,
            domain="project",
            sensitivity="private",
        )
        assert repaired[source_ids[name]] != capture_dedupe_key_for_text(
            raw_texts[name],
            (stale_project,),
            domain="project",
            sensitivity="private",
        )


def test_project_scope_identity_upgrade_matches_python_strip_and_blocks_unicode_whitespace_recapture(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260713_0089")
    user_id = "00000000-0000-0000-0000-000000000341"
    project_scope = ("Legacy Project",)
    raw_texts = {
        "nbsp": "\u00a0Fact: NBSP boundary\u00a0",
        "nel": "\u0085Fact: NEL boundary\u0085",
        "em_space": "\u2003Fact: EM SPACE boundary\u2003",
    }
    source_ids = {name: f"00000000-0000-0000-0005-{index:012d}" for index, name in enumerate(raw_texts, start=1)}

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "source-strip-0090@example.com", "Source strip 0090"),
            )
            for index, (name, raw_text) in enumerate(raw_texts.items(), start=1):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_ids[name],
                        user_id,
                        f"sha256:strip-{name}",
                        f"capture-md5:pre-strip-{name}",
                        f"2026-03-{index:02d}T00:00:00Z",
                        Jsonb(
                            {
                                "raw_text": raw_text,
                                "project_scope": list(project_scope),
                            }
                        ),
                    ),
                )

    command.upgrade(config, "head")

    expected_keys = {
        name: capture_dedupe_key_for_text(
            raw_text,
            project_scope,
            domain="project",
            sensitivity="private",
        )
        for name, raw_text in raw_texts.items()
    }
    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS id, dedupe_key
                FROM sources
                WHERE id = ANY(%s::uuid[])
                """,
                (list(source_ids.values()),),
            )
            repaired = {row["id"]: row["dedupe_key"] for row in cur.fetchall()}
    assert repaired == {source_ids[name]: expected_keys[name] for name in raw_texts}

    with user_connection(database_urls["app"], UUID(user_id)) as conn:
        service = VNextCaptureService(PostgresVNextStore(conn))
        for name, raw_text in raw_texts.items():
            result = service.capture_text(
                raw_text,
                project_scope=project_scope,
                domain="project",
                sensitivity="private",
            )
            assert result.duplicate is True
            assert result.source_id == source_ids[name]
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM sources WHERE deleted_at IS NULL")
            assert cur.fetchone()["count"] == len(raw_texts)


def test_source_identity_0091_clears_only_live_whitespace_strings_and_installs_event_indexes(
    database_urls,
):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260714_0090")
    user_id = "00000000-0000-0000-0007-000000000001"
    whitespace_cases = {
        "ascii": " \t\r\n",
        "unit_separator_control": "\u001c\u001f",
        "nbsp": "\u00a0",
        "nel": "\u0085",
        "em_space": "\u2003",
    }
    source_ids = {
        name: f"00000000-0000-0000-0007-{index:012d}" for index, name in enumerate(whitespace_cases, start=10)
    }
    nonempty_id = "00000000-0000-0000-0007-000000000020"
    absent_id = "00000000-0000-0000-0007-000000000021"
    nonstring_id = "00000000-0000-0000-0007-000000000022"
    deleted_id = "00000000-0000-0000-0007-000000000023"

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "source-defensive-0091@example.com", "Source defensive 0091"),
            )
            for index, (name, raw_text) in enumerate(whitespace_cases.items(), start=1):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s
                    )
                    """,
                    (
                        source_ids[name],
                        user_id,
                        f"sha256:whitespace-{name}",
                        f"capture-md5:pre-0091-{name}",
                        f"2026-04-{index:02d}T00:00:00Z",
                        Jsonb({"raw_text": raw_text}),
                    ),
                )
            controls = (
                (
                    nonempty_id,
                    "sha256:nonempty",
                    "capture-md5:pre-0091-nonempty",
                    {"raw_text": "\u00a0Fact: nonempty remains identified.\u2003"},
                    None,
                ),
                (
                    absent_id,
                    "sha256:absent-raw-text",
                    "sha256:absent-raw-text",
                    {"source": "legacy"},
                    None,
                ),
                (
                    nonstring_id,
                    "sha256:nonstring-raw-text",
                    "sha256:nonstring-raw-text",
                    {"raw_text": ["not", "text"]},
                    None,
                ),
                (
                    deleted_id,
                    "sha256:deleted-whitespace",
                    "capture-md5:deleted-whitespace",
                    {"raw_text": "\u00a0"},
                    "2026-04-12T00:00:00Z",
                ),
            )
            for index, (source_id, content_hash, dedupe_key, metadata, deleted_at) in enumerate(
                controls,
                start=10,
            ):
                cur.execute(
                    """
                    INSERT INTO sources (
                      id, user_id, source_type, content_hash, dedupe_key,
                      captured_at, domain, sensitivity, metadata_json, deleted_at
                    ) VALUES (
                      %s, %s, 'manual_text', %s, %s, %s::timestamptz,
                      'project', 'private', %s, %s::timestamptz
                    )
                    """,
                    (
                        source_id,
                        user_id,
                        content_hash,
                        dedupe_key,
                        f"2026-04-{index:02d}T00:00:00Z",
                        Jsonb(metadata),
                        deleted_at,
                    ),
                )

    command.upgrade(config, "head")

    def identity_snapshot() -> dict[str, str | None]:
        with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, dedupe_key
                    FROM sources
                    WHERE user_id = %s
                    ORDER BY id
                    """,
                    (user_id,),
                )
                return {row["id"]: row["dedupe_key"] for row in cur.fetchall()}

    expected = {source_id: None for source_id in source_ids.values()}
    expected.update(
        {
            nonempty_id: "capture-md5:pre-0091-nonempty",
            absent_id: "sha256:absent-raw-text",
            nonstring_id: "sha256:nonstring-raw-text",
            deleted_id: "capture-md5:deleted-whitespace",
        }
    )
    assert identity_snapshot() == expected

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname LIKE 'event_log_project_update_%'
                ORDER BY indexname
                """
            )
            definitions = {row["indexname"]: row["indexdef"] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, is_generated, generation_expression
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'event_log'
                  AND column_name IN (
                    'payload_artifact_id',
                    'payload_candidate_memory_id',
                    'payload_memory_id'
                  )
                ORDER BY column_name
                """
            )
            linkage_columns = {row["column_name"]: row for row in cur.fetchall()}
    assert set(definitions) == {
        "event_log_project_update_artifact_id_idx",
        "event_log_project_update_candidate_memory_id_idx",
        "event_log_project_update_memory_id_idx",
        "event_log_project_update_target_idx",
    }
    target_definition = definitions["event_log_project_update_target_idx"]
    assert "user_id, target_type, target_id, event_type, occurred_at DESC, id DESC" in target_definition
    for index_name, column_name in (
        ("event_log_project_update_artifact_id_idx", "payload_artifact_id"),
        (
            "event_log_project_update_candidate_memory_id_idx",
            "payload_candidate_memory_id",
        ),
        ("event_log_project_update_memory_id_idx", "payload_memory_id"),
    ):
        definition = definitions[index_name]
        assert f"user_id, event_type, {column_name}, occurred_at DESC, id DESC" in definition
        assert f"{column_name} IS NOT NULL" in definition
        assert "USING btree" in definition
        assert "USING gin" not in definition
    for definition in definitions.values():
        for event_type in (
            "project.update_candidate_created",
            "project.update_candidate_accepted",
            "project.update_candidate_rejected",
        ):
            assert event_type in definition
    assert set(linkage_columns) == {
        "payload_artifact_id",
        "payload_candidate_memory_id",
        "payload_memory_id",
    }
    for column_name, payload_key in (
        ("payload_artifact_id", "artifact_id"),
        ("payload_candidate_memory_id", "candidate_memory_id"),
        ("payload_memory_id", "memory_id"),
    ):
        column = linkage_columns[column_name]
        assert column["data_type"] == "text"
        assert column["is_nullable"] == "YES"
        assert column["is_generated"] == "ALWAYS"
        expression = str(column["generation_expression"])
        assert f"jsonb_typeof((payload_json -> '{payload_key}'::text)) = 'string'::text" in expression
        assert f"payload_json ->> '{payload_key}'::text" in expression

    # Re-crossing the forward boundary is data-idempotent.
    command.downgrade(config, "20260714_0090")
    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'event_log'
                  AND column_name IN (
                    'payload_artifact_id',
                    'payload_candidate_memory_id',
                    'payload_memory_id'
                  )
                """
            )
            assert cur.fetchall() == []
    command.upgrade(config, "head")
    assert identity_snapshot() == expected


def test_0092_backfills_prior_authorized_project_update_redaction(database_urls) -> None:
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260715_0091")
    user_id = "00000000-0000-0000-0092-000000000001"
    memory_id = "00000000-0000-0000-0092-000000000002"
    artifact_id = "00000000-0000-0000-0092-000000000003"
    revision_id = "00000000-0000-0000-0092-000000000004"
    rating_id = "00000000-0000-0000-0092-000000000005"
    provenance_id = "00000000-0000-0000-0092-000000000006"
    project_id = "00000000-0000-0000-0092-000000000007"
    redacted_at = "2026-07-15T23:59:01Z"
    sentinel = "0092-OLD-REDACTION-SECRET"

    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "redaction-0092@example.com", "Redaction 0092"),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, value, status, source_event_ids,
                  memory_type, title, canonical_text, summary, trust_reason,
                  domain, sensitivity, metadata_json, commit_digest,
                  confirmation_id, deleted_at
                ) VALUES (
                  %s, %s, %s, '{"redacted":true}'::jsonb, 'archived', %s,
                  'project_state', '[REDACTED]', '[REDACTED]', '[REDACTED]',
                  '[REDACTED]', 'project', 'private', %s, %s, %s, now()
                )
                """,
                (
                    memory_id,
                    user_id,
                    f"project_update.{sentinel}",
                    Jsonb([f"event-{sentinel}"]),
                    Jsonb(
                        {
                            "redacted": True,
                            "redacted_at": redacted_at,
                            "project_id": project_id,
                            "project_scope": [project_id],
                            "source_refs": [sentinel],
                            "consolidation_digest": f"digest-{sentinel}",
                        }
                    ),
                    f"commit-{sentinel}",
                    f"confirmation-{sentinel}",
                ),
            )
            cur.execute(
                """
                INSERT INTO memory_revisions (
                  id, user_id, memory_id, sequence_no, action, memory_key,
                  previous_value, new_value, source_event_ids, candidate,
                  revision_number, revision_type, text_before, text_after,
                  reason, actor_type, metadata_json
                ) VALUES (
                  %s, %s, %s, 1, 'project_update_review', %s,
                  '{"redacted":true}'::jsonb, '{"redacted":true}'::jsonb,
                  %s, '{"redacted":true}'::jsonb, 1, 'promoted',
                  '[REDACTED]', '[REDACTED]', '[REDACTED]', 'user',
                  '{"redacted":true}'::jsonb
                )
                """,
                (
                    revision_id,
                    user_id,
                    memory_id,
                    f"project_update.{sentinel}",
                    Jsonb([f"event-{sentinel}"]),
                ),
            )
            cur.execute(
                """
                INSERT INTO generated_artifacts (
                  id, user_id, artifact_type, title, content_markdown, status,
                  domain, sensitivity, generated_by, prompt_hash,
                  model_info_json, metadata_json
                ) VALUES (
                  %s, %s, 'project_update', %s, %s, 'accepted', 'project',
                  'private', 'system', %s, %s, %s
                )
                """,
                (
                    artifact_id,
                    user_id,
                    f"Title {sentinel}",
                    f"Content {sentinel}",
                    f"prompt-{sentinel}",
                    Jsonb({"provider": sentinel}),
                    Jsonb(
                        {
                            "workflow": "project_auto_update",
                            "project_id": project_id,
                            "project_scope": [project_id],
                            "candidate_memory_id": memory_id,
                            "review_action": "accept",
                            "candidate": False,
                            "review_status": "accepted",
                            "automation_digest": f"automation-{sentinel}",
                            "suggested_current_state": sentinel,
                            "accepted_current_state": sentinel,
                        }
                    ),
                ),
            )
            cur.execute(
                """
                INSERT INTO artifact_quality_ratings (
                  id, user_id, artifact_id, reviewer_id, usefulness, accuracy,
                  verbosity, missed_context, comments, metadata_json
                ) VALUES (%s, %s, %s, 'reviewer-1', 5, 4, 'right_sized', %s, %s, %s)
                """,
                (rating_id, user_id, artifact_id, sentinel, sentinel, Jsonb({"secret": sentinel})),
            )
            cur.execute(
                """
                INSERT INTO provenance_links (
                  id, user_id, target_type, target_id, quote, evidence_role, confidence
                ) VALUES (%s, %s, 'artifact', %s, %s, 'supports', 0.9)
                """,
                (provenance_id, user_id, artifact_id, sentinel),
            )
            for index, (event_type, target_type, target_id, payload) in enumerate(
                (
                    (
                        "project.update_candidate_created",
                        "artifact",
                        artifact_id,
                        {"artifact_id": artifact_id, "candidate_memory_id": memory_id, "secret": sentinel},
                    ),
                    (
                        "project.update_candidate_accepted",
                        "project",
                        project_id,
                        {"artifact_id": artifact_id, "candidate_memory_id": memory_id, "action": "accept"},
                    ),
                    (
                        "artifact.insight_feedback_recorded",
                        "artifact",
                        artifact_id,
                        {"artifact_id": artifact_id, "comments": sentinel},
                    ),
                    (
                        "memory.redacted",
                        "memory",
                        memory_id,
                        {"operation": "redact_memory_events", "secret": sentinel},
                    ),
                ),
                start=10,
            ):
                cur.execute(
                    """
                    INSERT INTO event_log (
                      id, user_id, event_type, actor_type, target_type,
                      target_id, payload_json, integrity_hash
                    ) VALUES (%s, %s, %s, 'user', %s, %s, %s, %s)
                    """,
                    (
                        f"00000000-0000-0000-0092-{index:012d}",
                        user_id,
                        event_type,
                        target_type,
                        target_id,
                        Jsonb(payload),
                        f"hash-{sentinel}-{index}",
                    ),
                )

    command.upgrade(config, "head")

    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_key, source_event_ids, metadata_json,
                       commit_digest, confirmation_id
                FROM memories WHERE id = %s
                """,
                (memory_id,),
            )
            memory = cur.fetchone()
            assert memory == {
                "memory_key": f"redacted.{memory_id}",
                "source_event_ids": [],
                "metadata_json": {
                    "project_id": project_id,
                    "project_scope": [project_id],
                    "redacted": True,
                    "redacted_at": redacted_at,
                },
                "commit_digest": None,
                "confirmation_id": None,
            }
            cur.execute(
                """
                SELECT title, content_markdown, prompt_hash, model_info_json,
                       status, domain, sensitivity, generated_by, metadata_json
                FROM generated_artifacts WHERE id = %s
                """,
                (artifact_id,),
            )
            artifact = cur.fetchone()
            assert artifact["title"] == artifact["content_markdown"] == "[REDACTED]"
            assert artifact["prompt_hash"] is None
            assert artifact["model_info_json"] == {"redacted": True}
            assert artifact["metadata_json"] == {
                "redacted": True,
                "redacted_at": redacted_at,
                "workflow": "project_auto_update",
                "project_id": project_id,
                "project_scope": [project_id],
                "candidate_memory_id": memory_id,
                "review_action": "accept",
            }
            assert (artifact["status"], artifact["domain"], artifact["sensitivity"], artifact["generated_by"]) == (
                "accepted",
                "project",
                "private",
                "system",
            )
            cur.execute(
                "SELECT missed_context, comments, metadata_json, usefulness, accuracy FROM artifact_quality_ratings WHERE id = %s",
                (rating_id,),
            )
            assert cur.fetchone() == {
                "missed_context": "[REDACTED]",
                "comments": "[REDACTED]",
                "metadata_json": {"redacted": True},
                "usefulness": 5,
                "accuracy": 4,
            }
            cur.execute("SELECT quote, evidence_role, confidence FROM provenance_links WHERE id = %s", (provenance_id,))
            assert cur.fetchone() == {"quote": "[REDACTED]", "evidence_role": "supports", "confidence": 0.9}
            cur.execute(
                """
                SELECT memory_key, source_event_ids, previous_value, new_value,
                       candidate, text_before, text_after, reason, metadata_json
                FROM memory_revisions WHERE id = %s
                """,
                (revision_id,),
            )
            revision = cur.fetchone()
            assert revision["memory_key"] == f"redacted.{memory_id}"
            assert revision["source_event_ids"] == []
            for field in ("previous_value", "new_value", "candidate", "metadata_json"):
                assert revision[field] == {"redacted": True}
            assert revision["text_before"] == revision["text_after"] == revision["reason"] == "[REDACTED]"
            cur.execute(
                "SELECT event_type, payload_json, integrity_hash FROM event_log WHERE user_id = %s ORDER BY event_type, id",
                (user_id,),
            )
            events = cur.fetchall()
            assert events
            for event in events:
                assert event["payload_json"] == {
                    "redacted": True,
                    "memory_id": memory_id,
                    "event_type": event["event_type"],
                }
                assert event["integrity_hash"] is None
            cur.execute(
                """
                SELECT has_table_privilege('alicebot_app', 'provenance_links', 'UPDATE') AS provenance_update,
                       has_table_privilege('alicebot_app', 'artifact_quality_ratings', 'UPDATE') AS rating_update
                """
            )
            assert cur.fetchone() == {"provenance_update": True, "rating_update": True}

    command.downgrade(config, "20260715_0091")
    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT has_table_privilege('alicebot_app', 'event_log', 'UPDATE') AS event_update,
                       has_table_privilege('alicebot_app', 'memory_revisions', 'UPDATE') AS revision_update,
                       has_table_privilege('alicebot_app', 'provenance_links', 'UPDATE') AS provenance_update,
                       has_table_privilege('alicebot_app', 'artifact_quality_ratings', 'UPDATE') AS rating_update
                """
            )
            assert cur.fetchone() == {
                "event_update": True,
                "revision_update": True,
                "provenance_update": False,
                "rating_update": False,
            }
            cur.execute("SELECT content_markdown FROM generated_artifacts WHERE id = %s", (artifact_id,))
            assert cur.fetchone() == {"content_markdown": "[REDACTED]"}
    command.upgrade(config, "head")
    with psycopg.connect(database_urls["admin"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT has_table_privilege(
                         'alicebot_app', 'provenance_links', 'UPDATE'
                       ) AS provenance_update,
                       has_table_privilege(
                         'alicebot_app', 'artifact_quality_ratings', 'UPDATE'
                       ) AS rating_update,
                       EXISTS (
                         SELECT 1
                         FROM pg_trigger
                         WHERE tgrelid = 'generated_artifacts'::regclass
                           AND tgname = 'generated_artifacts_redaction_guard'
                           AND NOT tgisinternal
                       ) AS artifact_guard
                """
            )
            assert cur.fetchone() == {
                "provenance_update": True,
                "rating_update": True,
                "artifact_guard": True,
            }
            cur.execute(
                """
                SELECT pg_get_functiondef(
                  'app.reject_event_log_mutation()'::regprocedure
                ) AS definition
                """
            )
            definition = str(cur.fetchone()["definition"])
            assert "OLD.payload_candidate_memory_id" in definition
            assert "OLD.payload_artifact_id" in definition
            assert "artifact.user_id = OLD.user_id" in definition


def test_postgres_source_constraints_reject_noncanonical_classifications(database_urls) -> None:
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "head")
    user_id = "00000000-0000-0000-0007-000000000030"
    with psycopg.connect(database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, "classification-postgres@example.com", "Classification constraints"),
            )

            invalid_cases = (
                ("domain", "", "sources_domain_check"),
                ("domain", " ", "sources_domain_check"),
                ("domain", "\u00a0", "sources_domain_check"),
                ("domain", "prøject", "sources_domain_check"),
                ("sensitivity", "", "sources_sensitivity_check"),
                ("sensitivity", " ", "sources_sensitivity_check"),
                ("sensitivity", "\u0085", "sources_sensitivity_check"),
                ("sensitivity", "prívate", "sources_sensitivity_check"),
            )
            for index, (column, invalid_value, constraint_name) in enumerate(
                invalid_cases,
                start=1,
            ):
                domain = invalid_value if column == "domain" else "project"
                sensitivity = invalid_value if column == "sensitivity" else "private"
                cur.execute("SAVEPOINT invalid_classification")
                with pytest.raises(psycopg.errors.CheckViolation, match=constraint_name):
                    cur.execute(
                        """
                        INSERT INTO sources (
                          id, user_id, source_type, content_hash, domain, sensitivity
                        ) VALUES (%s, %s, 'manual_text', %s, %s, %s)
                        """,
                        (
                            f"00000000-0000-0000-0007-{index + 30:012d}",
                            user_id,
                            f"sha256:invalid-classification-{index}",
                            domain,
                            sensitivity,
                        ),
                    )
                cur.execute("ROLLBACK TO SAVEPOINT invalid_classification")
                cur.execute("RELEASE SAVEPOINT invalid_classification")

            cur.execute("SELECT COUNT(*) FROM sources WHERE user_id = %s", (user_id,))
            assert cur.fetchone()[0] == 0
