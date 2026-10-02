from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.config import Settings
import alicebot_api.calendar as calendar_module
from alicebot_api.db import user_connection
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


def _build_calendar_secret_manager_url(root: Path) -> str:
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
        thread = store.create_thread("Calendar thread")
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


def _connect_calendar_account(
    *,
    user_id: UUID,
    provider_account_id: str,
    email_address: str,
) -> tuple[int, dict[str, Any]]:
    return invoke_request(
        "POST",
        "/v0/calendar-accounts",
        payload={
            "user_id": str(user_id),
            "provider_account_id": provider_account_id,
            "email_address": email_address,
            "display_name": email_address.split("@", 1)[0].title(),
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
            "access_token": f"token-for-{provider_account_id}",
        },
    )


def test_calendar_account_endpoints_connect_list_detail_and_isolate(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )

    create_status, create_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/calendar-accounts",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{create_payload['account']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    duplicate_status, duplicate_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/calendar-accounts",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{create_payload['account']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert create_status == 201
    assert create_payload == {
        "account": {
            "id": create_payload["account"]["id"],
            "provider": "google_calendar",
            "auth_kind": "oauth_access_token",
            "provider_account_id": "acct-owner-001",
            "email_address": "owner@gmail.example",
            "display_name": "Owner",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
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

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
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
                (UUID(create_payload["account"]["id"]),),
            )
            credential_row = cur.fetchone()

    assert credential_row is not None
    assert credential_row[0] == "oauth_access_token"
    assert credential_row[1] == "calendar_oauth_access_token_v1"
    assert credential_row[2] == "file_v1"
    assert credential_row[4] is True
    assert credential_row[3] is not None
    secret_payload = json.loads((calendar_secret_root / credential_row[3]).read_text(encoding="utf-8"))
    assert secret_payload == {
        "credential_kind": "calendar_oauth_access_token_v1",
        "access_token": "token-for-acct-owner-001",
    }


def test_calendar_event_list_endpoint_is_deterministic_and_limit_bounded(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_list_payload",
        lambda **_kwargs: [
            {
                "id": "evt-c",
                "summary": "Third",
                "status": "confirmed",
                "start": {"dateTime": "2026-03-25T10:00:00+02:00"},
                "end": {"dateTime": "2026-03-25T10:30:00+02:00"},
                "htmlLink": "https://calendar.google.com/event?eid=evt-c",
                "updated": "2026-03-24T09:00:00+00:00",
            },
            {
                "id": "evt-a",
                "summary": "First",
                "status": "tentative",
                "start": {"date": "2026-03-20"},
                "end": {"date": "2026-03-21"},
                "updated": "2026-03-19T09:00:00+00:00",
            },
            {
                "id": "evt-b",
                "summary": "Second",
                "status": "confirmed",
                "start": {"dateTime": "2026-03-25T08:30:00+00:00"},
                "end": {"dateTime": "2026-03-25T09:15:00+00:00"},
                "updated": "2026-03-24T08:30:00+00:00",
            },
        ],
    )

    _, account_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    status, payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events",
        query_params={
            "user_id": str(owner["user_id"]),
            "limit": "2",
            "time_min": "2026-03-20T00:00:00+00:00",
            "time_max": "2026-03-27T00:00:00+00:00",
        },
    )
    tighter_status, tighter_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events",
        query_params={
            "user_id": str(owner["user_id"]),
            "limit": "1",
        },
    )

    assert status == 200
    assert payload == {
        "account": account_payload["account"],
        "items": [
            {
                "provider_event_id": "evt-a",
                "status": "tentative",
                "summary": "First",
                "start_time": "2026-03-20",
                "end_time": "2026-03-21",
                "html_link": None,
                "updated_at": "2026-03-19T09:00:00+00:00",
            },
            {
                "provider_event_id": "evt-c",
                "status": "confirmed",
                "summary": "Third",
                "start_time": "2026-03-25T10:00:00+02:00",
                "end_time": "2026-03-25T10:30:00+02:00",
                "html_link": "https://calendar.google.com/event?eid=evt-c",
                "updated_at": "2026-03-24T09:00:00+00:00",
            },
        ],
        "summary": {
            "total_count": 2,
            "limit": 2,
            "order": ["start_time_asc", "provider_event_id_asc"],
            "time_min": "2026-03-20T00:00:00+00:00",
            "time_max": "2026-03-27T00:00:00+00:00",
        },
    }
    assert tighter_status == 200
    assert tighter_payload["summary"]["limit"] == 1
    assert tighter_payload["summary"]["total_count"] == 1
    assert len(tighter_payload["items"]) == 1


def test_calendar_event_list_endpoint_isolates_users_and_handles_missing_accounts(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_list_payload",
        lambda **_kwargs: [],
    )

    _, account_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    isolated_status, isolated_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events",
        query_params={"user_id": str(intruder["user_id"])},
    )
    missing_status, missing_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{uuid4()}/events",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert isolated_status == 404
    assert isolated_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert missing_status == 404
    assert missing_payload == {
        "detail": {
            "code": "not_found",
            "message": "The requested resource was not found",
        }
    }


def test_calendar_event_list_endpoint_maps_credential_fetch_and_validation_failures(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )

    _, account_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    account_id = account_payload["account"]["id"]

    monkeypatch.setattr(
        calendar_module,
        "resolve_calendar_access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            calendar_module.CalendarCredentialNotFoundError(
                f"calendar account {account_id} is missing protected credentials"
            )
        ),
    )
    credential_status, credential_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_id}/events",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert credential_status == 409
    assert credential_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    monkeypatch.setattr(
        calendar_module,
        "resolve_calendar_access_token",
        lambda *_args, **_kwargs: "token-for-acct-owner-001",
    )
    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_list_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            calendar_module.CalendarEventFetchError("calendar events could not be fetched")
        ),
    )
    fetch_status, fetch_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_id}/events",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert fetch_status == 502
    assert fetch_payload == {"detail": {"code": "upstream_failure", "message": "An upstream service failed"}}

    invalid_window_status, invalid_window_payload = invoke_request(
        "GET",
        f"/v0/calendar-accounts/{account_id}/events",
        query_params={
            "user_id": str(owner["user_id"]),
            "time_min": "2026-03-27T00:00:00+00:00",
            "time_max": "2026-03-20T00:00:00+00:00",
        },
    )
    assert invalid_window_status == 400
    assert invalid_window_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_calendar_event_ingestion_endpoint_persists_artifact_and_chunks(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_payload",
        lambda **_kwargs: {
            "id": "evt-001",
            "summary": "Sprint Planning",
            "description": "Discuss sprint scope and timelines.",
            "location": "Room 1",
            "status": "confirmed",
            "start": {"dateTime": "2026-03-20T09:00:00+00:00"},
            "end": {"dateTime": "2026-03-20T09:30:00+00:00"},
            "organizer": {"email": "owner@gmail.example"},
            "htmlLink": "https://calendar.google.com/event?eid=evt-001",
        },
    )

    account_status, account_payload = _connect_calendar_account(
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
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events/evt-001/ingest",
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
        "event": {
            "provider_event_id": "evt-001",
            "artifact_relative_path": "calendar/acct-owner-001/evt-001.txt",
            "media_type": "text/plain",
        },
        "artifact": {
            "id": ingest_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "calendar/acct-owner-001/evt-001.txt",
            "media_type_hint": "text/plain",
            "created_at": ingest_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": ingest_payload["summary"]["total_count"],
            "total_characters": ingest_payload["summary"]["total_characters"],
            "media_type": "text/plain",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }
    assert ingest_payload["summary"]["total_count"] >= 1
    artifact_file = Path(workspace_payload["workspace"]["local_path"]) / "calendar" / "acct-owner-001" / "evt-001.txt"
    assert artifact_file.is_file()
    artifact_text = artifact_file.read_text(encoding="utf-8")
    assert "Summary: Sprint Planning" in artifact_text
    assert "Start: 2026-03-20T09:00:00+00:00" in artifact_text

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        artifact_rows = store.list_task_artifacts_for_task(owner["task_id"])
        assert len(artifact_rows) == 1
        assert artifact_rows[0]["relative_path"] == "calendar/acct-owner-001/evt-001.txt"
        assert artifact_rows[0]["ingestion_status"] == "ingested"
        chunk_rows = store.list_task_artifact_chunks(artifact_rows[0]["id"])
        assert len(chunk_rows) == ingest_payload["summary"]["total_count"]
        assert chunk_rows[0]["text"].startswith("Provider: google_calendar")


def test_calendar_event_ingestion_endpoint_rejects_cross_user_workspace_access(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_payload",
        lambda **_kwargs: {
            "id": "evt-001",
            "start": {"dateTime": "2026-03-20T09:00:00+00:00"},
            "end": {"dateTime": "2026-03-20T09:30:00+00:00"},
        },
    )

    _, owner_workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    _, intruder_account_payload = _connect_calendar_account(
        user_id=intruder["user_id"],
        provider_account_id="acct-intruder-001",
        email_address="intruder@gmail.example",
    )
    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/calendar-accounts/{intruder_account_payload['account']['id']}/events/evt-001/ingest",
        payload={
            "user_id": str(intruder["user_id"]),
            "task_workspace_id": owner_workspace_payload["workspace"]["id"],
        },
    )

    assert ingest_status == 404
    assert ingest_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_calendar_event_ingestion_endpoint_rejects_missing_and_unsupported_events(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    calendar_secret_root = tmp_path / "calendar-secrets"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
            calendar_secret_manager_url=_build_calendar_secret_manager_url(calendar_secret_root),
        ),
    )

    _, account_payload = _connect_calendar_account(
        user_id=owner["user_id"],
        provider_account_id="acct-owner-001",
        email_address="owner@gmail.example",
    )
    _, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )

    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            calendar_module.CalendarEventNotFoundError("calendar event evt-missing was not found")
        ),
    )
    missing_status, missing_payload = invoke_request(
        "POST",
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events/evt-missing/ingest",
        payload={
            "user_id": str(owner["user_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
        },
    )

    monkeypatch.setattr(
        calendar_module,
        "fetch_calendar_event_payload",
        lambda **_kwargs: {
            "id": "evt-unsupported",
            "start": {"dateTime": "2026-03-20T09:00:00+00:00"},
        },
    )
    unsupported_status, unsupported_payload = invoke_request(
        "POST",
        f"/v0/calendar-accounts/{account_payload['account']['id']}/events/evt-unsupported/ingest",
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
