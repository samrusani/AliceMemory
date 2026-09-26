"""The session template and its clones match a database migrated from empty."""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import uuid4

from alembic.script import ScriptDirectory
import psycopg
import pytest

from alicebot_api.migrations import make_alembic_config

from tests.integration.conftest import _create_role_separated_database, _drop_database


def _database_name(database_url: str) -> str:
    return urlsplit(database_url).path.removeprefix("/")


def _snapshot(database_url: str) -> dict[str, object]:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.alembic_version')")
            if cur.fetchone()[0] is None:
                alembic_version: list[tuple[object, ...]] = []
            else:
                cur.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
                alembic_version = cur.fetchall()
            cur.execute(
                """
                SELECT pg_get_userbyid(datdba), datacl::text
                FROM pg_database
                WHERE datname = current_database()
                """
            )
            owner, database_acl = cur.fetchone()
            cur.execute(
                """
                SELECT nspname, pg_get_userbyid(nspowner), nspacl::text
                FROM pg_namespace
                WHERE nspname IN ('public', 'app')
                ORDER BY nspname
                """
            )
            schemas = cur.fetchall()
            cur.execute(
                """
                SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),
                       c.relacl::text, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname IN ('public', 'app')
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                ORDER BY 1, 2, 3
                """
            )
            relations = cur.fetchall()
            cur.execute(
                """
                SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                       pg_get_userbyid(p.proowner), p.proacl::text
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'app'
                ORDER BY 1, 2
                """
            )
            functions = cur.fetchall()
            cur.execute(
                """
                SELECT schemaname, tablename, policyname, permissive, roles::text, cmd, qual, with_check
                FROM pg_policies
                WHERE schemaname IN ('public', 'app')
                ORDER BY 1, 2, 3
                """
            )
            policies = cur.fetchall()
            cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
            extensions = cur.fetchall()
            cur.execute(
                """
                SELECT pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype, d.defaclacl::text
                FROM pg_default_acl d
                LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
                ORDER BY 1, 2, 3, 4
                """
            )
            default_acls = cur.fetchall()
    return {
        "alembic_version": alembic_version,
        "owner": owner,
        "database_acl": database_acl,
        "schemas": schemas,
        "relations": relations,
        "functions": functions,
        "policies": policies,
        "extensions": extensions,
        "default_acls": default_acls,
    }


def _template_migration_count(request: pytest.FixtureRequest) -> int:
    fixturedefs = request._fixturemanager.getfixturedefs("migrated_template_database", request.node)
    assert fixturedefs is not None
    return int(fixturedefs[0].func.__globals__["TEMPLATE_MIGRATION_COUNT"])


def test_template_clone_matches_a_fresh_migrate(database_urls, migrated_database_urls) -> None:
    config = make_alembic_config(database_urls["admin"])
    from alembic import command

    command.upgrade(config, "head")
    fresh = _snapshot(database_urls["admin"])
    clone = _snapshot(migrated_database_urls["admin"])
    head = ScriptDirectory.from_config(config).get_current_head()
    assert fresh["alembic_version"] == [(head,)]
    assert len(fresh["relations"]) > 100
    database_acl = fresh["database_acl"]
    assert isinstance(database_acl, str)
    assert "alicebot_app=Tc/" in database_acl
    assert clone["alembic_version"] == fresh["alembic_version"]
    assert clone["owner"] == fresh["owner"]
    assert clone["database_acl"] == fresh["database_acl"]
    assert clone["schemas"] == fresh["schemas"]
    assert clone["relations"] == fresh["relations"]
    assert clone["functions"] == fresh["functions"]
    assert clone["policies"] == fresh["policies"]
    assert clone["extensions"] == fresh["extensions"]
    assert clone["default_acls"] == fresh["default_acls"]


def test_migrated_database_is_a_private_clone_of_the_session_template(
    migrated_database_urls, migrated_template_database
) -> None:
    clone_name = _database_name(migrated_database_urls["admin"])
    assert clone_name != migrated_template_database
    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE _clone_probe (id integer)")
            cur.execute("INSERT INTO _clone_probe (id) VALUES (1)")
    second_name = f"alicebot_test_{uuid4().hex[:12]}"
    second = _create_role_separated_database(second_name, template=migrated_template_database)
    try:
        with psycopg.connect(second["admin"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public._clone_probe')")
                assert cur.fetchone() == (None,)
    finally:
        _drop_database(second_name)
    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datallowconn FROM pg_database WHERE datname = %s",
                (migrated_template_database,),
            )
            assert cur.fetchone() == (False,)


@pytest.mark.parametrize("pass_index", [1, 2])
def test_template_is_migrated_once_per_session(
    pass_index: int, migrated_database_urls, request: pytest.FixtureRequest
) -> None:
    del pass_index, migrated_database_urls
    assert _template_migration_count(request) == 1
