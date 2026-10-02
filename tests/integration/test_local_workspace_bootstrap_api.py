from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import UUID, uuid4

import anyio
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import set_current_user, set_current_user_account, user_connection
from alicebot_api.local_workspace import LOCAL_WORKSPACE_NAME, local_workspace_id
from alicebot_api.routers import workspaces as workspaces_router
from alicebot_api.store import ContinuityStore


ROOT = Path(__file__).resolve().parents[2]
LEAST_PRIVILEGE_DEPLOYMENT_ENV = "ALICEBOT_LEAST_PRIVILEGE_DEPLOYMENT"


def invoke_request(
    method: str,
    path: str,
    *,
    user_id: UUID | str | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    request_headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if user_id is not None:
        request_headers.append((b"x-alicebot-user-id", str(user_id).encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": request_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start_message["status"]), json.loads(body)


def configure_local_api(
    monkeypatch: Any,
    database_urls: dict[str, str],
    *,
    settings: Settings | None = None,
) -> None:
    if settings is None:
        settings = Settings(
            app_env="test",
            database_url=database_urls["app"],
            database_admin_url=database_urls["admin"],
        )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(workspaces_router, "get_settings", lambda: settings)


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


@contextmanager
def least_privilege_seed_url(
    admin_database_url: str,
    *,
    require_supplied_role: bool,
) -> Iterator[str]:
    """Yield a non-superuser, non-BYPASSRLS URL that can upsert ``users``."""

    with psycopg.connect(admin_database_url) as conn:
        role_row = conn.execute(
            """
            SELECT current_user, current_database(), rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname = current_user
            """
        ).fetchone()
    assert role_row is not None
    _current_role, database_name, is_superuser, bypasses_rls = role_row
    if not bool(is_superuser) and not bool(bypasses_rls):
        yield admin_database_url
        return

    assert require_supplied_role is False, (
        f"{LEAST_PRIVILEGE_DEPLOYMENT_ENV}=1 requires DATABASE_ADMIN_URL to use a NOSUPERUSER, NOBYPASSRLS role"
    )

    role_name = f"alicebot_seed_test_{uuid4().hex[:12]}"
    password = uuid4().hex
    with psycopg.connect(admin_database_url) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                sql.Identifier(role_name),
                sql.Literal(password),
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(str(database_name)),
                sql.Identifier(role_name),
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA app, public TO {}").format(
                sql.Identifier(role_name),
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE users TO {}").format(
                sql.Identifier(role_name),
            )
        )

    seed_database_url = make_conninfo(
        admin_database_url,
        user=role_name,
        password=password,
    )
    try:
        with psycopg.connect(seed_database_url) as conn:
            role_bits = conn.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
        assert role_bits == (False, False)
        yield seed_database_url
    finally:
        with psycopg.connect(admin_database_url) as conn:
            conn.execute(
                sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name)),
            )
            conn.execute(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)),
            )


def run_documented_local_user_seed(
    *,
    database_admin_url: str,
    user_id: UUID,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ALICEBOT_AUTH_USER_ID"] = str(user_id)
    environment["DATABASE_ADMIN_URL"] = database_admin_url
    environment["DATABASE_URL"] = "postgresql://runtime-fallback-must-not-be-used.invalid/alicebot"
    source_path = str(ROOT / "apps" / "api" / "src")
    environment["PYTHONPATH"] = (
        source_path
        if environment.get("PYTHONPATH", "") == ""
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    return subprocess.run(
        [sys.executable, "scripts/seed_local_user.py"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_local_workspace_bootstrap_requires_a_valid_identity_header(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(app_env="test"))
    monkeypatch.setattr(workspaces_router, "get_settings", lambda: Settings(app_env="test"))

    missing_status, missing_payload = invoke_request("POST", "/v1/workspaces/bootstrap")
    assert missing_status == 400
    assert missing_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    invalid_status, invalid_payload = invoke_request(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id="not-a-uuid",
    )
    assert invalid_status == 400
    assert invalid_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_documented_empty_users_seed_then_workspace_bootstrap_under_least_privilege_roles(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    require_supplied_role = os.getenv(LEAST_PRIVILEGE_DEPLOYMENT_ENV, "").strip() == "1"
    if require_supplied_role:
        database_urls = {
            "admin": os.environ["DATABASE_ADMIN_URL"],
            "app": os.environ["DATABASE_URL"],
        }
    else:
        database_urls = request.getfixturevalue("migrated_database_urls")

    user_id = uuid4()
    workspace_id = local_workspace_id(user_id)
    settings = Settings(
        app_env="production",
        auth_user_id=str(user_id),
        database_url=database_urls["app"],
    )
    configure_local_api(monkeypatch, database_urls, settings=settings)

    with least_privilege_seed_url(
        database_urls["admin"],
        require_supplied_role=require_supplied_role,
    ) as seed_database_url:
        with psycopg.connect(seed_database_url) as conn:
            rls_flags = conn.execute(
                """
                SELECT relrowsecurity,
                       relforcerowsecurity,
                       pg_get_userbyid(relowner),
                       current_user
                FROM pg_class
                WHERE oid = 'users'::regclass
                """
            ).fetchone()
            assert rls_flags is not None
            row_security_enabled, row_security_forced, table_owner, current_role = rls_flags
            assert (row_security_enabled, row_security_forced) == (True, True)
            if require_supplied_role:
                assert table_owner == current_role
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege,
                match="row-level security policy",
            ):
                conn.execute(
                    """
                    INSERT INTO users (id, email, display_name)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        user_id,
                        f"local-alpha-{user_id}@alicebot.local",
                        "Local Alpha User",
                    ),
                )
            conn.rollback()
            with conn.transaction():
                set_current_user(conn, user_id)
                before_seed = conn.execute(
                    "SELECT count(*) FROM users WHERE id = %s",
                    (user_id,),
                ).fetchone()
        assert before_seed == (0,)

        first_seed = run_documented_local_user_seed(
            database_admin_url=seed_database_url,
            user_id=user_id,
        )
        assert first_seed.returncode == 0, first_seed.stderr
        assert first_seed.stdout == "local_user_seed=ready\n"
        assert first_seed.stderr == ""

        first_status, first_payload = invoke_request(
            "POST",
            "/v1/workspaces/bootstrap",
        )
        assert first_status == 200
        assert first_payload["workspace"]["id"] == str(workspace_id)
        assert first_payload["workspace"]["owner_user_account_id"] == str(user_id)
        assert first_payload["bootstrap"]["status"] == "ready"

        second_seed = run_documented_local_user_seed(
            database_admin_url=seed_database_url,
            user_id=user_id,
        )
        assert second_seed.returncode == 0, second_seed.stderr
        assert second_seed.stdout == "local_user_seed=ready\n"
        assert second_seed.stderr == ""

        second_status, second_payload = invoke_request(
            "POST",
            "/v1/workspaces/bootstrap",
        )
        assert second_status == 200
        assert second_payload["workspace"]["id"] == str(workspace_id)
        assert second_payload["bootstrap"]["bootstrapped_at"] == first_payload["bootstrap"]["bootstrapped_at"]

        with user_connection(database_urls["app"], user_id) as conn:
            set_current_user_account(conn, user_id)
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT u.id AS core_user_id,
                           u.email AS core_email,
                           ua.id AS user_account_id,
                           ua.email AS account_email,
                           w.id AS workspace_id,
                           w.owner_user_account_id,
                           wm.role,
                           (SELECT count(*) FROM users WHERE id = %s) AS core_user_count,
                           (
                             SELECT count(*)
                             FROM user_accounts
                             WHERE id = %s
                           ) AS user_account_count,
                           (
                             SELECT count(*)
                             FROM workspaces
                             WHERE id = %s
                           ) AS workspace_count,
                           (
                             SELECT count(*)
                             FROM workspace_members
                             WHERE workspace_id = %s
                               AND user_account_id = %s
                           ) AS member_count
                    FROM users AS u
                    JOIN user_accounts AS ua ON ua.id = u.id
                    JOIN workspaces AS w ON w.owner_user_account_id = ua.id
                    JOIN workspace_members AS wm
                      ON wm.workspace_id = w.id
                     AND wm.user_account_id = ua.id
                    WHERE u.id = %s
                      AND w.id = %s
                    """,
                    (
                        user_id,
                        user_id,
                        workspace_id,
                        workspace_id,
                        user_id,
                        user_id,
                        workspace_id,
                    ),
                )
                persisted = cur.fetchone()

        assert persisted is not None
        assert persisted["core_user_id"] == user_id
        assert persisted["core_email"] == f"local-alpha-{user_id}@alicebot.local"
        assert persisted["user_account_id"] == user_id
        assert persisted["account_email"] == f"local+{user_id.hex}@alicebot.invalid"
        assert persisted["workspace_id"] == workspace_id
        assert persisted["owner_user_account_id"] == user_id
        assert persisted["role"] == "owner"
        assert persisted["core_user_count"] == 1
        assert persisted["user_account_count"] == 1
        assert persisted["workspace_count"] == 1
        assert persisted["member_count"] == 1


def test_local_workspace_bootstrap_is_deterministic_idempotent_and_identity_isolated(
    migrated_database_urls: dict[str, str],
    monkeypatch: Any,
) -> None:
    configure_local_api(monkeypatch, migrated_database_urls)

    unknown_user_id = uuid4()
    unknown_status, unknown_payload = invoke_request(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id=unknown_user_id,
    )
    assert unknown_status == 404
    assert unknown_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}

    owner_id = seed_user(migrated_database_urls["app"], email="local-owner@example.com")
    other_id = seed_user(migrated_database_urls["app"], email="local-other@example.com")

    before_status, before_payload = invoke_request(
        "GET",
        "/v1/workspaces/bootstrap/status",
        user_id=owner_id,
    )
    assert before_status == 404
    assert before_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}

    create_status, create_payload = invoke_request(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id=owner_id,
    )
    assert create_status == 200
    expected_workspace_id = local_workspace_id(owner_id)
    assert create_payload["workspace"]["id"] == str(expected_workspace_id)
    assert create_payload["workspace"]["owner_user_account_id"] == str(owner_id)
    assert create_payload["workspace"]["slug"] == f"local-{owner_id.hex}"
    assert create_payload["workspace"]["name"] == LOCAL_WORKSPACE_NAME
    assert create_payload["workspace"]["bootstrap_status"] == "ready"
    assert create_payload["bootstrap"]["workspace_id"] == str(expected_workspace_id)
    assert create_payload["bootstrap"]["status"] == "ready"
    assert create_payload["seeded_provider_count"] == 0

    repeat_status, repeat_payload = invoke_request(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id=owner_id,
    )
    assert repeat_status == 200
    assert repeat_payload["workspace"]["id"] == str(expected_workspace_id)
    assert repeat_payload["bootstrap"]["bootstrapped_at"] == create_payload["bootstrap"]["bootstrapped_at"]

    status_code, status_payload = invoke_request(
        "GET",
        "/v1/workspaces/bootstrap/status",
        user_id=owner_id,
    )
    assert status_code == 200
    assert status_payload["workspace"]["id"] == str(expected_workspace_id)
    assert status_payload["bootstrap"]["status"] == "ready"

    other_before_status, _ = invoke_request(
        "GET",
        "/v1/workspaces/bootstrap/status",
        user_id=other_id,
    )
    assert other_before_status == 404

    other_create_status, other_create_payload = invoke_request(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id=other_id,
    )
    assert other_create_status == 200
    assert other_create_payload["workspace"]["id"] == str(local_workspace_id(other_id))
    assert other_create_payload["workspace"]["id"] != str(expected_workspace_id)

    with user_connection(migrated_database_urls["app"], owner_id) as conn:
        set_current_user_account(conn, owner_id)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT w.id, w.owner_user_account_id, wm.role,
                       ua.email, ua.display_name
                FROM workspaces AS w
                JOIN workspace_members AS wm
                  ON wm.workspace_id = w.id
                 AND wm.user_account_id = w.owner_user_account_id
                JOIN user_accounts AS ua
                  ON ua.id = w.owner_user_account_id
                WHERE w.id = %s
                """,
                (expected_workspace_id,),
            )
            persisted = cur.fetchone()

    assert persisted is not None
    assert persisted["owner_user_account_id"] == owner_id
    assert persisted["role"] == "owner"
    assert persisted["email"] == f"local+{owner_id.hex}@alicebot.invalid"
    assert persisted["display_name"] == "Alice local operator"
