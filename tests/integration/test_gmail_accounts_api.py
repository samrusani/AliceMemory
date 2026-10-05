from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.config import Settings
import alicebot_api.gmail as gmail_module
from alicebot_api.db import set_current_user, user_connection
from alicebot_api.store import ContinuityStore


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = b"" if payload is None else json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    query_string = urlencode(query_params or {}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def _build_rfc822_email_bytes(*, subject: str, plain_body: str) -> bytes:
    return "\r\n".join(
        [
            "From: Alice <alice@example.com>",
            "To: Bob <bob@example.com>",
            f"Subject: {subject}",
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            plain_body,
        ]
    ).encode("utf-8")


def _build_gmail_secret_manager_url(root: Path) -> str:
    return root.resolve().as_uri()


def seed_user(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())

    return {"user_id": user_id}


def seed_task(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Gmail thread")
        tool = store.create_tool(
            tool_key="proxy.echo",
            name="Proxy Echo",
            description="Deterministic proxy handler.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["proxy"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        task = store.create_task(
            thread_id=thread["id"],
            tool_id=tool["id"],
            status="approved",
            request={
                "thread_id": str(thread["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {},
            },
            tool={
                "id": str(tool["id"]),
                "tool_key": "proxy.echo",
                "name": "Proxy Echo",
                "description": "Deterministic proxy handler.",
                "version": "1.0.0",
                "metadata_version": "tool_metadata_v0",
                "active": True,
                "tags": ["proxy"],
                "action_hints": ["tool.run"],
                "scope_hints": ["workspace"],
                "domain_hints": [],
                "risk_hints": [],
                "metadata": {"transport": "proxy"},
                "created_at": tool["created_at"].isoformat(),
            },
            latest_approval_id=None,
            latest_execution_id=None,
        )

    return {
        "user_id": user_id,
        "task_id": task["id"],
    }


def _connect_gmail_account(
    *,
    user_id: UUID,
    provider_account_id: str,
    email_address: str,
    credential_overrides: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = {
        "user_id": str(user_id),
        "provider_account_id": provider_account_id,
        "email_address": email_address,
        "display_name": email_address.split("@", 1)[0].title(),
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_token": f"token-for-{provider_account_id}",
    }
    if credential_overrides is not None:
        payload.update(credential_overrides)

    return invoke_request(
        "POST",
        "/v0/gmail-accounts",
        payload=payload,
    )


def test_gmail_account_endpoints_connect_list_detail_and_isolate(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    create_status, create_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/gmail-accounts",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/gmail-accounts/{create_payload['account']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    duplicate_status, duplicate_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/gmail-accounts",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/gmail-accounts/{create_payload['account']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert create_status == 201
    assert create_payload == {
        "account": {
            "id": create_payload["account"]["id"],
            "provider": "gmail",
            "auth_kind": "oauth_access_token",
            "provider_account_id": "acct-owner-001",
            "email_address": "owner@gmail.example",
            "display_name": "Owner",
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
            "created_at": create_payload["account"]["created_at"],
            "updated_at": create_payload["account"]["updated_at"],
        }
    }
    assert list_status == 200
    assert list_payload == {
        "items": [create_payload["account"]],
        "summary": {"total_count": 1, "order": ["created_at_asc", "id_asc"]},
    }
    assert detail_status == 200
    assert detail_payload == {"account": create_payload["account"]}
    assert duplicate_status == 409
    assert duplicate_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["created_at_asc", "id_asc"]},
    }
    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert '"access_token":' not in json.dumps(create_payload)
    assert '"access_token":' not in json.dumps(list_payload)
    assert '"access_token":' not in json.dumps(detail_payload)
    assert '"refresh_token":' not in json.dumps(create_payload)
    assert '"client_secret":' not in json.dumps(create_payload)

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'gmail_accounts'
                ORDER BY ordinal_position
                """
            )
            gmail_account_columns = {row[0] for row in cur.fetchall()}
            assert "access_token" not in gmail_account_columns
            cur.execute(
                """
                SELECT
                  auth_kind,
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob IS NULL
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (UUID(create_payload["account"]["id"]),),
            )
            credential_row = cur.fetchone()

    assert credential_row is not None
    assert credential_row[0] == "oauth_access_token"
    assert credential_row[1] == "gmail_oauth_access_token_v1"
    assert credential_row[2] == "file_v1"
    assert credential_row[4] is True
    assert credential_row[3] is not None
    secret_payload = json.loads((gmail_secret_root / credential_row[3]).read_text(encoding="utf-8"))
    assert secret_payload == {
        "credential_kind": "gmail_oauth_access_token_v1",
        "access_token": "token-for-acct-owner-001",
    }


def test_gmail_message_ingestion_endpoint_persists_artifact_and_chunks(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    raw_bytes = _build_rfc822_email_bytes(subject="Inbox Update", plain_body="ingest this message")
    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        lambda **_kwargs: raw_bytes,
    )

    account_status, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert account_status == 201
    assert workspace_status == 201
    assert ingest_status == 200
    assert ingest_payload == {
        "account": account_payload["account"],
        "message": {
            "provider_message_id": "msg-001",
            "artifact_relative_path": "gmail/acct-owner-001/msg-001.eml",
            "media_type": "message/rfc822",
        },
        "artifact": {
            "id": ingest_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "gmail/acct-owner-001/msg-001.eml",
            "media_type_hint": "message/rfc822",
            "created_at": ingest_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": ingest_payload["summary"]["total_count"],
            "total_characters": ingest_payload["summary"]["total_characters"],
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }
    assert ingest_payload["summary"]["total_count"] >= 1
    artifact_file = Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-001" / "msg-001.eml"
    assert artifact_file.read_bytes() == raw_bytes

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        artifact_rows = store.list_task_artifacts_for_task(owner["task_id"])
        assert len(artifact_rows) == 1
        assert artifact_rows[0]["relative_path"] == "gmail/acct-owner-001/msg-001.eml"
        assert artifact_rows[0]["ingestion_status"] == "ingested"
        chunk_rows = store.list_task_artifact_chunks(artifact_rows[0]["id"])
        assert len(chunk_rows) == ingest_payload["summary"]["total_count"]
        assert chunk_rows[0]["text"].startswith("From: Alice <alice@example.com>")


def test_gmail_message_ingestion_endpoint_renews_expired_access_token(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    raw_bytes = _build_rfc822_email_bytes(subject="Inbox Update", plain_body="renewed token path")
    fetch_tokens: list[str] = []

    monkeypatch.setattr(
        gmail_module,
        "refresh_gmail_access_token",
        lambda **_kwargs: gmail_module.RefreshedGmailCredential(
            access_token="token-refreshed",
            access_token_expires_at=datetime.fromisoformat("2030-01-01T00:05:00+00:00"),
        ),
    )

    def fake_fetch_gmail_message_raw_bytes(*, access_token: str, **_kwargs) -> bytes:
        fetch_tokens.append(access_token)
        return raw_bytes

    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        fake_fetch_gmail_message_raw_bytes,
    )

    account_status, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-refresh-001",
        email_address="owner@gmail.example",
        credential_overrides={
            "refresh_token": "refresh-owner-001",
            "client_id": "client-owner-001",
            "client_secret": "secret-owner-001",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert account_status == 201
    assert workspace_status == 201
    assert ingest_status == 200
    assert fetch_tokens == ["token-refreshed"]
    assert ingest_payload["message"] == {
        "provider_message_id": "msg-001",
        "artifact_relative_path": "gmail/acct-owner-refresh-001/msg-001.eml",
        "media_type": "message/rfc822",
    }
    assert '"refresh_token":' not in json.dumps(ingest_payload)
    assert '"client_secret":' not in json.dumps(ingest_payload)

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob IS NULL
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (UUID(account_payload["account"]["id"]),),
            )
            credential_row = cur.fetchone()

    assert credential_row is not None
    assert credential_row[0] == "gmail_oauth_refresh_token_v2"
    assert credential_row[1] == "file_v1"
    assert credential_row[3] is True
    assert credential_row[2] is not None
    secret_payload = json.loads((gmail_secret_root / credential_row[2]).read_text(encoding="utf-8"))
    assert secret_payload == {
        "credential_kind": "gmail_oauth_refresh_token_v2",
        "access_token": "token-refreshed",
        "refresh_token": "refresh-owner-001",
        "client_id": "client-owner-001",
        "client_secret": "secret-owner-001",
        "access_token_expires_at": "2030-01-01T00:05:00+00:00",
    }


def test_gmail_message_ingestion_endpoint_persists_rotated_refresh_token(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    raw_bytes = _build_rfc822_email_bytes(subject="Inbox Update", plain_body="rotated token path")
    fetch_tokens: list[str] = []

    monkeypatch.setattr(
        gmail_module,
        "refresh_gmail_access_token",
        lambda **_kwargs: gmail_module.RefreshedGmailCredential(
            access_token="token-refreshed-rotated",
            access_token_expires_at=datetime.fromisoformat("2030-01-01T00:05:00+00:00"),
            refresh_token="refresh-owner-rotated-002",
        ),
    )

    def fake_fetch_gmail_message_raw_bytes(*, access_token: str, **_kwargs) -> bytes:
        fetch_tokens.append(access_token)
        return raw_bytes

    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        fake_fetch_gmail_message_raw_bytes,
    )

    account_status, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-rotated-001",
        email_address="owner@gmail.example",
        credential_overrides={
            "refresh_token": "refresh-owner-001",
            "client_id": "client-owner-001",
            "client_secret": "secret-owner-001",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert account_status == 201
    assert workspace_status == 201
    assert ingest_status == 200
    assert fetch_tokens == ["token-refreshed-rotated"]
    assert ingest_payload == {
        "account": {
            **account_payload["account"],
        },
        "message": {
            "provider_message_id": "msg-001",
            "artifact_relative_path": "gmail/acct-owner-rotated-001/msg-001.eml",
            "media_type": "message/rfc822",
        },
        "artifact": {
            "id": ingest_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "gmail/acct-owner-rotated-001/msg-001.eml",
            "media_type_hint": "message/rfc822",
            "created_at": ingest_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": ingest_payload["summary"]["total_count"],
            "total_characters": ingest_payload["summary"]["total_characters"],
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }
    assert '"refresh_token":' not in json.dumps(ingest_payload)
    assert '"client_secret":' not in json.dumps(ingest_payload)

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob IS NULL
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (UUID(account_payload["account"]["id"]),),
            )
            credential_row = cur.fetchone()

    assert credential_row is not None
    assert credential_row[0] == "gmail_oauth_refresh_token_v2"
    assert credential_row[1] == "file_v1"
    assert credential_row[3] is True
    assert credential_row[2] is not None
    secret_payload = json.loads((gmail_secret_root / credential_row[2]).read_text(encoding="utf-8"))
    assert secret_payload == {
        "credential_kind": "gmail_oauth_refresh_token_v2",
        "access_token": "token-refreshed-rotated",
        "refresh_token": "refresh-owner-rotated-002",
        "client_id": "client-owner-001",
        "client_secret": "secret-owner-001",
        "access_token_expires_at": "2030-01-01T00:05:00+00:00",
    }


def test_gmail_message_ingestion_endpoint_fails_deterministically_when_rotated_credentials_cannot_be_persisted(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    monkeypatch.setattr(
        gmail_module,
        "refresh_gmail_access_token",
        lambda **_kwargs: gmail_module.RefreshedGmailCredential(
            access_token="token-refreshed-rotated",
            access_token_expires_at=datetime.fromisoformat("2030-01-01T00:05:00+00:00"),
            refresh_token="refresh-owner-rotated-002",
        ),
    )

    def fail_update_gmail_account_credential(self, **_kwargs):
        raise psycopg.Error("simulated credential persistence failure")

    def fail_fetch(**_kwargs):
        raise AssertionError("fetch_gmail_message_raw_bytes should not be called")

    monkeypatch.setattr(
        ContinuityStore,
        "update_gmail_account_credential",
        fail_update_gmail_account_credential,
    )
    monkeypatch.setattr(gmail_module, "fetch_gmail_message_raw_bytes", fail_fetch)

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-rotated-001",
        email_address="owner@gmail.example",
        credential_overrides={
            "refresh_token": "refresh-owner-001",
            "client_id": "client-owner-001",
            "client_secret": "secret-owner-001",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 409
    assert ingest_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    artifact_file = (
        Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-rotated-001" / "msg-001.eml"
    )
    assert not artifact_file.exists()

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_task_artifacts_for_task(owner["task_id"]) == []

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  credential_kind,
                  secret_manager_kind,
                  secret_ref,
                  credential_blob IS NULL
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (UUID(account_payload["account"]["id"]),),
            )
            credential_row = cur.fetchone()

    assert credential_row is not None
    assert credential_row[0] == "gmail_oauth_refresh_token_v2"
    assert credential_row[1] == "file_v1"
    assert credential_row[3] is True
    assert credential_row[2] is not None
    secret_payload = json.loads((gmail_secret_root / credential_row[2]).read_text(encoding="utf-8"))
    assert secret_payload == {
        "credential_kind": "gmail_oauth_refresh_token_v2",
        "access_token": f"token-for-{account_payload['account']['provider_account_id']}",
        "refresh_token": "refresh-owner-001",
        "client_id": "client-owner-001",
        "client_secret": "secret-owner-001",
        "access_token_expires_at": "2020-01-01T00:00:00+00:00",
    }


def test_gmail_message_ingestion_endpoint_rejects_cross_user_workspace_access(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        lambda **_kwargs: _build_rfc822_email_bytes(
            subject="Inbox Update",
            plain_body="ingest this message",
        ),
    )

    _, owner_workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    _, intruder_account_payload = _connect_gmail_account(
        user_id=intruder["user_id"],
        provider_account_id="acct-intruder-001",
        email_address="intruder@gmail.example",
    )
    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{intruder_account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(intruder["user_id"]),
            "task_workspace_id": owner_workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 404
    assert ingest_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_gmail_message_ingestion_endpoint_rejects_missing_protected_credentials_without_side_effects(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    def fail_fetch(**_kwargs):
        raise AssertionError("fetch_gmail_message_raw_bytes should not be called")

    monkeypatch.setattr(gmail_module, "fetch_gmail_message_raw_bytes", fail_fetch)

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM gmail_account_credentials WHERE gmail_account_id = %s",
                (UUID(account_payload["account"]["id"]),),
            )
        conn.commit()

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 409
    assert ingest_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    artifact_file = Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-001" / "msg-001.eml"
    assert not artifact_file.exists()

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_task_artifacts_for_task(owner["task_id"]) == []


def test_gmail_message_ingestion_endpoint_rejects_missing_external_secret_without_side_effects(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("fetch_gmail_message_raw_bytes should not be called")),
    )

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-secret-missing-001",
        email_address="owner@gmail.example",
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT secret_ref
                FROM gmail_account_credentials
                WHERE gmail_account_id = %s
                """,
                (UUID(account_payload["account"]["id"]),),
            )
            secret_ref_row = cur.fetchone()

    assert secret_ref_row is not None
    secret_path = gmail_secret_root / secret_ref_row[0]
    secret_path.unlink()

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 409
    assert ingest_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    artifact_file = (
        Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-secret-missing-001" / "msg-001.eml"
    )
    assert not artifact_file.exists()

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_task_artifacts_for_task(owner["task_id"]) == []


def test_gmail_message_ingestion_endpoint_rejects_invalid_refresh_credentials_without_side_effects(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-refresh-001",
        email_address="owner@gmail.example",
        credential_overrides={
            "refresh_token": "refresh-owner-001",
            "client_id": "client-owner-001",
            "client_secret": "secret-owner-001",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    def fail_refresh(**_kwargs):
        raise gmail_module.GmailCredentialInvalidError(
            f"gmail account {account_payload['account']['id']} refresh credentials were rejected"
        )

    monkeypatch.setattr(gmail_module, "refresh_gmail_access_token", fail_refresh)
    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("fetch_gmail_message_raw_bytes should not be called")),
    )

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 409
    assert ingest_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    artifact_file = (
        Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-refresh-001" / "msg-001.eml"
    )
    assert not artifact_file.exists()

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_task_artifacts_for_task(owner["task_id"]) == []


def test_gmail_message_ingestion_endpoint_rejects_sanitized_path_collisions_without_overwrite(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    first_bytes = _build_rfc822_email_bytes(subject="First", plain_body="first message body")
    second_bytes = _build_rfc822_email_bytes(subject="Second", plain_body="second message body")

    def fake_fetch_gmail_message_raw_bytes(*, provider_message_id: str, **_kwargs) -> bytes:
        if provider_message_id == "msg+001":
            return first_bytes
        if provider_message_id == "msg:001":
            return second_bytes
        raise AssertionError(f"unexpected provider_message_id: {provider_message_id}")

    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        fake_fetch_gmail_message_raw_bytes,
    )

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    first_ingest_status, first_ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg+001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )
    second_ingest_status, second_ingest_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg:001/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    artifact_file = Path(workspace_payload["workspace"]["local_path"]) / "gmail" / "acct-owner-001" / "msg_001.eml"

    assert first_ingest_status == 200
    assert second_ingest_status == 409
    assert second_ingest_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    assert artifact_file.read_bytes() == first_bytes
    assert first_ingest_payload["artifact"]["relative_path"] == "gmail/acct-owner-001/msg_001.eml"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        artifact_rows = store.list_task_artifacts_for_task(owner["task_id"])
        assert len(artifact_rows) == 1
        assert artifact_rows[0]["relative_path"] == "gmail/acct-owner-001/msg_001.eml"


def test_gmail_message_ingestion_endpoint_rejects_missing_and_unsupported_messages(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    gmail_secret_root = tmp_path / "gmail-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            gmail_secret_manager_url=_build_gmail_secret_manager_url(gmail_secret_root),
        ),
    )

    _, account_payload = _connect_gmail_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    def fake_missing(**_kwargs):
        raise gmail_module.GmailMessageNotFoundError("gmail message msg-missing was not found")

    monkeypatch.setattr(gmail_module, "fetch_gmail_message_raw_bytes", fake_missing)
    missing_status, missing_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-missing/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    monkeypatch.setattr(
        gmail_module,
        "fetch_gmail_message_raw_bytes",
        lambda **_kwargs: b"not-a-valid-rfc822-email",
    )
    unsupported_status, unsupported_payload = invoke_request(
        "POST",
        f"/v0/gmail-accounts/{account_payload['account']['id']}/messages/msg-unsupported/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    assert missing_status == 404
    assert missing_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert unsupported_status == 400
    assert unsupported_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_task_artifacts_for_task(owner["task_id"]) == []
