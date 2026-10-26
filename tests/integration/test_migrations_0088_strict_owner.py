"""Migration 0088 must also work for the documented table owner on a vault that carries provider rows.

``POST /v1/providers`` was unconditional in v0.9.0 and v0.10.0, so a vault upgraded from one of
those releases can carry ``model_providers`` and ``provider_capabilities`` rows. Both tables carry
forced row-level security whose owner policies bind
``app.hosted_workspace_owner_allowed(workspace_id)``, which no unbound migration connection
satisfies, so the strict owner stopped at 0087 with ``NotNullViolation: column
"config_fingerprint_sha256" of relation "model_providers" contains null values`` while a
superuser upgraded the same vault. The migration now brackets its data statements with the owner
bypass, as 0067, 0070, 0092, 0093 and 0095 to 0097 do, and restores the forced setting before its
transaction commits. These tests seed one provider row with its capability row at 0087, upgrade
one database as such an owner and its twin as a superuser, and compare the end states.
"""

from __future__ import annotations

import pytest
from alembic import command

from alicebot_api.migrations import make_alembic_config
from tests.integration.test_migrations_0067_strict_owner import (  # noqa: F401  (fixtures)
    strict_owner_urls,
    superuser_reference_urls,
)

WORKSPACE_ID = "00000000-0000-0000-0000-000000000a01"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000a02"
PROVIDER_ID = "00000000-0000-0000-0000-000000000a03"
CAPABILITY_ID = "00000000-0000-0000-0000-000000000a04"


def _seed_provider_rows(admin_url: str) -> None:
    """One provider row with its capability row, as POST /v1/providers wrote it."""

    import psycopg

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO user_accounts (id, email, display_name) VALUES (%s, 'provider-a@example.com', 'pa')
            ON CONFLICT (id) DO NOTHING
            """,
            (ACCOUNT_ID,),
        )
        conn.execute(
            """
            INSERT INTO workspaces (id, owner_user_account_id, slug, name, bootstrap_status)
            VALUES (%s, %s, 'provider-a-vault', 'Provider A Vault', 'ready')
            ON CONFLICT (id) DO NOTHING
            """,
            (WORKSPACE_ID, ACCOUNT_ID),
        )
        conn.execute(
            """
            INSERT INTO model_providers (
              id, workspace_id, created_by_user_account_id, provider_key, model_provider,
              display_name, base_url, api_key, auth_mode, default_model, status, metadata
            )
            VALUES (
              %s, %s, %s, 'openai-compatible-a', 'openai_responses',
              'Provider A', 'https://provider-a.example/v1', 'ref:provider-a-secret', 'bearer',
              'gpt-5-mini', 'active', '{}'::jsonb
            )
            ON CONFLICT (id) DO NOTHING
            """,
            (PROVIDER_ID, WORKSPACE_ID, ACCOUNT_ID),
        )
        conn.execute(
            """
            INSERT INTO provider_capabilities (
              id, workspace_id, provider_id, discovered_by_user_account_id, adapter_key,
              discovery_status, capability_snapshot
            )
            VALUES (%s, %s, %s, %s, 'openai-compatible', 'ready', '{}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            (CAPABILITY_ID, WORKSPACE_ID, PROVIDER_ID, ACCOUNT_ID),
        )


def _provider_rows(admin_url: str) -> tuple:
    import psycopg

    with psycopg.connect(admin_url) as conn:
        provider = conn.execute(
            """
            SELECT config_revision, config_fingerprint_sha256
            FROM model_providers WHERE id = %s::uuid
            """,
            (PROVIDER_ID,),
        ).fetchall()
        capability = conn.execute(
            """
            SELECT provider_config_revision, provider_config_fingerprint_sha256
            FROM provider_capabilities WHERE provider_id = %s::uuid
            """,
            (PROVIDER_ID,),
        ).fetchall()
        return tuple(map(tuple, provider + capability))


def test_strict_owner_provider_cas_backfill_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """A vault with one provider row upgrades past 0088 for the owner too."""

    owner_config = make_alembic_config(strict_owner_urls["strict"])
    reference_config = make_alembic_config(superuser_reference_urls["admin"])
    command.upgrade(owner_config, "20260713_0087")
    command.upgrade(reference_config, "20260713_0087")
    _seed_provider_rows(strict_owner_urls["admin"])
    _seed_provider_rows(superuser_reference_urls["admin"])

    command.upgrade(owner_config, "head")
    command.upgrade(reference_config, "head")

    owner_rows = _provider_rows(strict_owner_urls["admin"])
    reference_rows = _provider_rows(superuser_reference_urls["admin"])
    assert owner_rows == reference_rows
    # The backfill really ran on both paths: the revision moved to 1 with a
    # fingerprint, and the capability row carries the provider's tokens.
    assert len(owner_rows) == 2
    revision, fingerprint = owner_rows[0]
    assert revision == 1 and fingerprint is not None and len(fingerprint) == 64
    assert owner_rows[1] == (revision, fingerprint)


def test_strict_owner_forced_flags_restored_after_0088(strict_owner_urls, superuser_reference_urls):
    """The forced row-level security settings survive the owner-path upgrade."""

    owner_config = make_alembic_config(strict_owner_urls["strict"])
    command.upgrade(owner_config, "20260713_0087")
    _seed_provider_rows(strict_owner_urls["admin"])
    command.upgrade(owner_config, "head")

    import psycopg

    with psycopg.connect(strict_owner_urls["admin"]) as conn:
        flags = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname IN ('model_providers', 'provider_capabilities')
            ORDER BY c.relname
            """
        ).fetchall()
    assert sorted(flags) == [
        ("model_providers", True, True),
        ("provider_capabilities", True, True),
    ]
