from __future__ import annotations

from collections.abc import Iterator
import os
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from alembic import command
import psycopg
from psycopg import sql
import pytest

import alicebot_api.main as main_module
from alicebot_api.migrations import make_alembic_config


DEFAULT_ADMIN_URL = "postgresql://alicebot_admin:alicebot_admin@localhost:5432/alicebot"
DEFAULT_APP_URL = "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot"
_EXECUTED_TEST_COUNT = 0
TEMPLATE_MIGRATION_COUNT = 0


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-executed-tests",
        action="store_true",
        help="fail when every selected integration test is skipped",
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _EXECUTED_TEST_COUNT
    if report.when == "call" and not report.skipped:
        _EXECUTED_TEST_COUNT += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if (
        session.config.getoption("--require-executed-tests")
        and exitstatus == pytest.ExitCode.OK
        and _EXECUTED_TEST_COUNT == 0
    ):
        session.exitstatus = pytest.ExitCode.NO_TESTS_COLLECTED


def swap_database_name(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment))


def database_role(database_url: str) -> str:
    role = unquote(urlsplit(database_url).username or "")
    if not role:
        raise ValueError("database URL must include a role")
    return role


def _role_urls(name: str) -> tuple[str, str, str, str, str, str]:
    admin_root_url = os.getenv("DATABASE_ADMIN_URL", DEFAULT_ADMIN_URL)
    app_root_url = os.getenv("DATABASE_URL", DEFAULT_APP_URL)
    lifecycle_root_url = os.getenv("DATABASE_LIFECYCLE_URL", admin_root_url)
    return (
        admin_root_url,
        app_root_url,
        lifecycle_root_url,
        swap_database_name(admin_root_url, name),
        swap_database_name(app_root_url, name),
        swap_database_name(lifecycle_root_url, name),
    )


def _create_role_separated_database(name: str, *, template: str | None = None) -> dict[str, str]:
    """Create one empty database, or a clone of ``template``.

    A clone keeps schema ACLs and extensions and loses database ACLs, so both
    database grants run again. The schema grant is only for a fresh database.
    """

    admin_root_url, _app_root_url, lifecycle_root_url, admin_database_url, app_database_url, lifecycle_database_url = (
        _role_urls(name)
    )
    admin_role = database_role(admin_root_url)
    app_role = database_role(os.getenv("DATABASE_URL", DEFAULT_APP_URL))
    with psycopg.connect(lifecycle_root_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            if template is None:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            else:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(name),
                        sql.Identifier(template),
                    )
                )
            cur.execute(
                sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(name),
                    sql.Identifier(admin_role),
                )
            )
            cur.execute(
                sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                    sql.Identifier(name),
                    sql.Identifier(app_role),
                )
            )
    if template is None:
        with psycopg.connect(lifecycle_database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("GRANT CREATE, USAGE ON SCHEMA public TO {}").format(sql.Identifier(admin_role))
                )
    return {"admin": admin_database_url, "app": app_database_url}


def _drop_database(name: str) -> None:
    _admin_root_url, _app_root_url, lifecycle_root_url, _admin_database_url, _app_database_url, _lifecycle_database_url = (
        _role_urls(name)
    )
    with psycopg.connect(lifecycle_root_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database_urls() -> Iterator[dict[str, str]]:
    database_name = f"alicebot_test_{uuid4().hex[:12]}"
    urls = _create_role_separated_database(database_name)
    try:
        yield urls
    finally:
        _drop_database(database_name)


@pytest.fixture(scope="session")
def migrated_template_database() -> Iterator[str]:
    """One migrated database per session. Clones copy it instead of replaying Alembic."""

    global TEMPLATE_MIGRATION_COUNT
    name = f"alicebot_tmpl_{uuid4().hex[:12]}"
    database_urls = _create_role_separated_database(name)
    try:
        config = make_alembic_config(database_urls["admin"])
        command.upgrade(config, "head")
        TEMPLATE_MIGRATION_COUNT += 1
        _admin_root_url, _app_root_url, lifecycle_root_url, _admin_url, _app_url, _lifecycle_url = _role_urls(name)
        with psycopg.connect(lifecycle_root_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(sql.Identifier(name))
                )
        yield name
    finally:
        _drop_database(name)


@pytest.fixture
def migrated_database_urls(migrated_template_database: str) -> Iterator[dict[str, str]]:
    database_name = f"alicebot_test_{uuid4().hex[:12]}"
    urls = _create_role_separated_database(database_name, template=migrated_template_database)
    try:
        yield urls
    finally:
        _drop_database(database_name)
