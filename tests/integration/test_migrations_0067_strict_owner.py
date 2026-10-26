"""Migration 0067 and successors must also work for the documented table owner.

The deployment guide describes a database owner that is ``NOSUPERUSER
NOBYPASSRLS``. A forced row-level security table makes the owner subject to
its policies, so a migration that writes data under a forced policy that does
not cover the write fails for the owner while the same migration passes for a
superuser. These tests run whole Alembic chains as such an owner and compare
the resulting schema, byte for byte at the metadata level, against the
superuser path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
import psycopg
from psycopg import sql

from alicebot_api.migrations import make_alembic_config

DEFAULT_ADMIN_URL = "postgresql://alicebot_admin:alicebot_admin@localhost:5432/alicebot"
DEFAULT_APP_URL = "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot"

LEGACY_USER_ID = "00000000-0000-0000-0000-000000000301"
LEGACY_MEMORY_ID = "00000000-0000-0000-0000-000000000302"
LEGACY_REVISION_ID = "00000000-0000-0000-0000-000000000303"


def _admin_root_url() -> str:
    return os.getenv("DATABASE_ADMIN_URL", DEFAULT_ADMIN_URL)


def _app_root_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_APP_URL)


def _swap_database(url: str, name: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))


@pytest.fixture
def strict_owner_urls() -> Iterator[dict[str, str]]:
    """One database owned by a LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS role.

    The cluster superuser only creates the database, hands it to its owner
    and installs pgcrypto and vector into it, which mirrors the deployment
    guide's one-time template1 step. The owner then runs the migrations and
    seeds every legacy row itself.
    """

    database_name = f"alicebot_test_{uuid4().hex[:12]}"
    owner_role = "alicebot_strict_owner"
    owner_password = "alicebot_strict_owner"
    with psycopg.connect(_admin_root_url(), autocommit=True) as conn:
        conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alicebot_strict_owner') THEN
                EXECUTE format(
                  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                  'alicebot_strict_owner', 'alicebot_strict_owner'
                );
              END IF;
            END
            $$;
            """
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database_name), sql.Identifier(owner_role)
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO alicebot_app").format(
                sql.Identifier(database_name)
            )
        )
    admin_root = _admin_root_url()
    parsed = urlsplit(admin_root)
    owner_host = parsed.hostname or "localhost"
    owner_port = parsed.port or 5432
    owner_url = f"postgresql://{owner_role}:{owner_password}@{owner_host}:{owner_port}/{database_name}"
    with psycopg.connect(_swap_database(admin_root, database_name), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO alicebot_app")
    urls = {
        "strict": owner_url,
        "admin": _swap_database(_admin_root_url(), database_name),
        "app": _swap_database(_app_root_url(), database_name),
    }
    try:
        yield urls
    finally:
        with psycopg.connect(_admin_root_url(), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))
            # The owner role owns nothing once its database is gone. Another
            # concurrent run on the same cluster may be inside its own fixture
            # and own its database through this role, so the role is dropped
            # only when no database of it is left.
            dependent = conn.execute(
                """
                SELECT count(*) FROM pg_database
                WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = 'alicebot_strict_owner')
                """
            ).fetchone()[0]
            if not dependent:
                conn.execute("DROP ROLE IF EXISTS alicebot_strict_owner")


@pytest.fixture
def superuser_reference_urls() -> Iterator[dict[str, str]]:
    """One superuser-migrated database with the same legacy rows, for comparison."""

    database_name = f"alicebot_test_{uuid4().hex[:12]}"
    admin_root_url = _admin_root_url()
    app_root_url = _app_root_url()
    with psycopg.connect(admin_root_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        conn.execute(
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO alicebot_app").format(
                sql.Identifier(database_name)
            )
        )
    urls = {
        "admin": _swap_database(admin_root_url, database_name),
        "app": _swap_database(app_root_url, database_name),
    }
    try:
        yield urls
    finally:
        with psycopg.connect(admin_root_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))


def _set_legacy_identity(conn: psycopg.Connection) -> None:
    conn.execute(
        "SELECT set_config(%s, %s, %s)",
        ("app.current_user_id", LEGACY_USER_ID, not conn.autocommit),
    )


def _seed_legacy_rows(url: str) -> None:
    """Insert the users, memories and memory_revisions rows a pre-vNext vault has."""

    with psycopg.connect(url) as conn:
        _set_legacy_identity(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (LEGACY_USER_ID, "strict-owner@example.com", "Strict Owner"),
            )
            cur.execute(
                """
                INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids)
                VALUES (%s, %s, 'strict.owner.seed', '{"text":"before"}'::jsonb, 'active', '[]'::jsonb)
                """,
                (LEGACY_MEMORY_ID, LEGACY_USER_ID),
            )
            cur.execute(
                """
                INSERT INTO memory_revisions (
                  id, user_id, memory_id, sequence_no, action, memory_key,
                  previous_value, new_value, source_event_ids, candidate
                )
                VALUES (
                  %s, %s, %s, 7, 'UPDATE', 'strict.owner.seed',
                  '{"text":"before"}'::jsonb, '{"text":"after"}'::jsonb, '[]'::jsonb, '{}'::jsonb
                )
                """,
                (LEGACY_REVISION_ID, LEGACY_USER_ID, LEGACY_MEMORY_ID),
            )


def _legacy_revision_backfill(url: str) -> tuple:
    with psycopg.connect(url) as conn:
        _set_legacy_identity(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT revision_number, revision_type, text_before, text_after
                FROM memory_revisions WHERE id = %s
                """,
                (LEGACY_REVISION_ID,),
            )
            return cur.fetchone()


def _schema_fingerprint(url: str) -> tuple:
    """Every public table's shape, without owners or privileges that differ by role."""

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY c.relname
                """
            )
            tables = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, column_name
                """
            )
            columns = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace AND conrelid != 0
                ORDER BY 1, 2, 3
                """
            )
            constraints = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT c.relname::text, t.tgname, pg_get_triggerdef(t.oid)
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE NOT t.tgisinternal AND n.nspname = 'public'
                ORDER BY 1, 2, 3
                """
            )
            triggers = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT tablename, policyname, permissive, roles, cmd, qual, with_check
                FROM pg_policies
                WHERE schemaname = 'public'
                ORDER BY 1, 2, 3
                """
            )
            policies = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                ORDER BY 1, 2, 3
                """
            )
            indexes = tuple(map(tuple, cur.fetchall()))
    return (tables, columns, constraints, triggers, policies, indexes)


def _legacy_data_fingerprint(url: str) -> tuple:
    with psycopg.connect(url) as conn:
        _set_legacy_identity(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, memory_id, sequence_no, action, memory_key,
                       previous_value::text, new_value::text,
                       revision_number, revision_type, text_before, text_after
                FROM memory_revisions
                ORDER BY id
                """
            )
            revisions = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT id, user_id, memory_key, value::text, status
                FROM memories
                ORDER BY id
                """
            )
            return revisions + tuple(map(tuple, cur.fetchall()))


@pytest.mark.integration
class TestStrictOwnerMigrationChain:
    def test_strict_owner_with_legacy_rows_upgrades_to_head(self, strict_owner_urls):
        """The documented owner replays 0001 through head over a data-bearing vault."""

        config = make_alembic_config(strict_owner_urls["strict"])
        command.upgrade(config, "20260416_0066")
        _seed_legacy_rows(strict_owner_urls["strict"])
        command.upgrade(config, "head")
        assert _legacy_revision_backfill(strict_owner_urls["strict"]) == (
            7,
            "edited",
            '{"text": "before"}',
            '{"text": "after"}',
        )

    def test_strict_owner_end_state_equals_superuser_end_state(
        self, strict_owner_urls, superuser_reference_urls
    ):
        """A database at 0066 must end exactly where the superuser path ends.

        Every public table's columns, constraints, triggers, policies, indexes
        and the row-level security plus forced flags must match, and the
        legacy rows must carry the same backfilled values.
        """

        strict_config = make_alembic_config(strict_owner_urls["strict"])
        command.upgrade(strict_config, "20260416_0066")
        _seed_legacy_rows(strict_owner_urls["strict"])
        command.upgrade(strict_config, "head")

        reference_config = make_alembic_config(superuser_reference_urls["admin"])
        command.upgrade(reference_config, "20260416_0066")
        _seed_legacy_rows(superuser_reference_urls["admin"])
        command.upgrade(reference_config, "head")

        assert _schema_fingerprint(strict_owner_urls["strict"]) == _schema_fingerprint(
            superuser_reference_urls["admin"]
        )
        assert _legacy_data_fingerprint(strict_owner_urls["strict"]) == _legacy_data_fingerprint(
            superuser_reference_urls["admin"]
        )

    def test_strict_owner_without_legacy_rows_upgrades_to_head(self, strict_owner_urls):
        """The documented owner replays the chain over an empty vault too."""

        config = make_alembic_config(strict_owner_urls["strict"])
        command.upgrade(config, "head")
