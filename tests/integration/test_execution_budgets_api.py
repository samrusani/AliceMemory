from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.execution_budgets import EXECUTION_BUDGET_LIFECYCLE_REJECTION_REASON
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


def seed_user(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Budget lifecycle thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def create_budget(
    *,
    user_id: UUID,
    agent_profile_id: str | None = None,
    tool_key: str | None,
    domain_hint: str | None,
    max_completed_executions: int,
    rolling_window_seconds: int | None = None,
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {
        "user_id": str(user_id),
        "max_completed_executions": max_completed_executions,
    }
    if agent_profile_id is not None:
        payload["agent_profile_id"] = agent_profile_id
    if tool_key is not None:
        payload["tool_key"] = tool_key
    if domain_hint is not None:
        payload["domain_hint"] = domain_hint
    if rolling_window_seconds is not None:
        payload["rolling_window_seconds"] = rolling_window_seconds
    return invoke_request("POST", "/v0/execution-budgets", payload=payload)


def test_execution_budget_endpoints_create_list_and_get_in_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    second_status, second_payload = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=2,
        rolling_window_seconds=3600,
    )
    first_status, first_payload = create_budget(
        user_id=owner["user_id"],
        tool_key=None,
        domain_hint="docs",
        max_completed_executions=1,
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/execution-budgets",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/execution-budgets/{second_payload['execution_budget']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/execution-budgets",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert first_status == 201
    assert second_status == 201
    assert second_payload["execution_budget"]["agent_profile_id"] is None
    assert second_payload["execution_budget"]["status"] == "active"
    assert second_payload["execution_budget"]["deactivated_at"] is None
    assert second_payload["execution_budget"]["rolling_window_seconds"] == 3600
    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        second_payload["execution_budget"]["id"],
        first_payload["execution_budget"]["id"],
    ]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"execution_budget": second_payload["execution_budget"]}
    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["created_at_asc", "id_asc"]},
    }

    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/execution-budgets/{first_payload['execution_budget']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_create_execution_budget_endpoint_requires_at_least_one_selector(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    status_code, payload = invoke_request(
        "POST",
        "/v0/execution-budgets",
        payload={
            "user_id": str(owner["user_id"]),
            "max_completed_executions": 1,
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_create_execution_budget_endpoint_rejects_duplicate_active_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    first_status, _ = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint="docs",
        max_completed_executions=1,
    )
    second_status, second_payload = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint="docs",
        max_completed_executions=2,
    )

    assert first_status == 201
    assert second_status == 400
    assert second_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_create_execution_budget_endpoint_rejects_unknown_agent_profile_id(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    status_code, payload = create_budget(
        user_id=owner["user_id"],
        agent_profile_id="profile_missing",
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_create_execution_budget_endpoint_allows_same_selector_across_profile_scopes(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    scoped_status, scoped_payload = create_budget(
        user_id=owner["user_id"],
        agent_profile_id="assistant_default",
        tool_key="proxy.echo",
        domain_hint="docs",
        max_completed_executions=1,
    )
    global_status, global_payload = create_budget(
        user_id=owner["user_id"],
        agent_profile_id=None,
        tool_key="proxy.echo",
        domain_hint="docs",
        max_completed_executions=2,
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/execution-budgets",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert scoped_status == 201
    assert global_status == 201
    assert scoped_payload["execution_budget"]["agent_profile_id"] == "assistant_default"
    assert global_payload["execution_budget"]["agent_profile_id"] is None
    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        scoped_payload["execution_budget"]["id"],
        global_payload["execution_budget"]["id"],
    ]


def test_deactivate_execution_budget_endpoint_updates_reads_and_emits_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    create_status, create_payload = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )
    assert create_status == 201

    deactivate_status, deactivate_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}/deactivate",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
        },
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/execution-budgets",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_status, isolated_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}/deactivate",
        payload={
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
        },
    )

    assert deactivate_status == 200
    assert deactivate_payload["execution_budget"]["status"] == "inactive"
    assert deactivate_payload["execution_budget"]["deactivated_at"] is not None
    assert deactivate_payload["trace"]["trace_event_count"] == 3
    assert list_status == 200
    assert list_payload["items"][0] == deactivate_payload["execution_budget"]
    assert detail_status == 200
    assert detail_payload == {"execution_budget": deactivate_payload["execution_budget"]}
    assert isolated_status == 404
    assert isolated_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(UUID(deactivate_payload["trace"]["trace_id"]))
        trace_events = store.list_trace_events(UUID(deactivate_payload["trace"]["trace_id"]))

    assert trace["kind"] == "execution_budget.lifecycle"
    assert trace["compiler_version"] == "execution_budget_lifecycle_v0"
    assert trace["limits"]["requested_action"] == "deactivate"
    assert [event["kind"] for event in trace_events] == [
        "execution_budget.lifecycle.request",
        "execution_budget.lifecycle.state",
        "execution_budget.lifecycle.summary",
    ]
    assert trace_events[1]["payload"]["current_status"] == "inactive"
    assert trace_events[2]["payload"]["outcome"] == "deactivated"


def test_supersede_execution_budget_endpoint_replaces_active_budget_and_emits_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    create_status, create_payload = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint="docs",
        max_completed_executions=1,
        rolling_window_seconds=1800,
    )
    assert create_status == 201

    supersede_status, supersede_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}/supersede",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "max_completed_executions": 3,
        },
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/execution-budgets",
        query_params={"user_id": str(owner["user_id"])},
    )
    original_detail_status, original_detail_payload = invoke_request(
        "GET",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    replacement_detail_status, replacement_detail_payload = invoke_request(
        "GET",
        f"/v0/execution-budgets/{supersede_payload['replacement_budget']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert supersede_status == 200
    assert supersede_payload["superseded_budget"]["status"] == "superseded"
    assert supersede_payload["replacement_budget"]["status"] == "active"
    assert supersede_payload["replacement_budget"]["rolling_window_seconds"] == 1800
    assert supersede_payload["replacement_budget"]["supersedes_budget_id"] == create_payload["execution_budget"]["id"]
    assert (
        supersede_payload["superseded_budget"]["superseded_by_budget_id"]
        == supersede_payload["replacement_budget"]["id"]
    )
    assert list_status == 200
    assert [item["status"] for item in list_payload["items"]] == ["superseded", "active"]
    assert original_detail_status == 200
    assert original_detail_payload == {"execution_budget": supersede_payload["superseded_budget"]}
    assert replacement_detail_status == 200
    assert replacement_detail_payload == {"execution_budget": supersede_payload["replacement_budget"]}

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(UUID(supersede_payload["trace"]["trace_id"]))
        trace_events = store.list_trace_events(UUID(supersede_payload["trace"]["trace_id"]))

    assert trace["limits"]["requested_action"] == "supersede"
    assert trace["limits"]["outcome"] == "superseded"
    assert trace_events[1]["payload"]["replacement_budget_id"] == supersede_payload["replacement_budget"]["id"]
    assert trace_events[2]["payload"]["active_budget_id"] == supersede_payload["replacement_budget"]["id"]


def test_execution_budget_lifecycle_rejects_invalid_transition_deterministically(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    create_status, create_payload = create_budget(
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )
    assert create_status == 201

    first_status, _ = invoke_request(
        "POST",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}/deactivate",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
        },
    )
    second_status, second_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{create_payload['execution_budget']['id']}/deactivate",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
        },
    )

    assert first_status == 200
    assert second_status == 409
    assert second_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace_rows = store.conn.execute(
            "SELECT id FROM traces WHERE kind = %s ORDER BY created_at ASC, id ASC",
            ("execution_budget.lifecycle",),
        ).fetchall()
        rejected_trace_events = store.list_trace_events(trace_rows[-1]["id"])

    assert (
        rejected_trace_events[1]["payload"]["rejection_reason"]
        == EXECUTION_BUDGET_LIFECYCLE_REJECTION_REASON
        == second_payload["detail"]["message"]
    )
    assert rejected_trace_events[2]["payload"]["outcome"] == "rejected"


def test_execution_budget_active_scope_uniqueness_is_enforced_in_database(
    migrated_database_urls,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_execution_budget(
            tool_key="proxy.echo",
            domain_hint="docs",
            max_completed_executions=1,
        )

        with pytest.raises(psycopg.IntegrityError):
            with conn.transaction():
                store.create_execution_budget(
                    tool_key="proxy.echo",
                    domain_hint="docs",
                    max_completed_executions=2,
                )
