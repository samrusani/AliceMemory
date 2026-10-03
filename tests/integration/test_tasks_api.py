from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.config import Settings
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


def seed_user(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Task thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def test_task_endpoints_list_detail_lifecycle_and_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="web_access",
            status="granted",
            metadata={"source": "settings"},
        )
        approval_tool = store.create_tool(
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
        ready_tool = store.create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        denied_tool = store.create_tool(
            tool_key="calendar.read",
            name="Calendar Read",
            description="Read calendars.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["calendar"],
            action_hints=["calendar.read"],
            scope_hints=["calendar"],
            domain_hints=[],
            risk_hints=[],
            metadata={},
        )
        store.create_policy(
            name="Require proxy approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "proxy.echo"},
            required_consents=[],
        )
        store.create_policy(
            name="Allow docs browser",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=20,
            active=True,
            conditions={"tool_key": "browser.open", "domain_hint": "docs"},
            required_consents=["web_access"],
        )

    pending_status, pending_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(approval_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "hello"},
        },
    )
    assert pending_status == 200
    assert pending_payload["task"]["status"] == "pending_approval"

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{pending_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{pending_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert execute_status == 200
    assert execute_payload["result"]["status"] == "completed"

    ready_status, ready_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(ready_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {},
        },
    )
    assert ready_status == 200
    assert ready_payload["task"]["status"] == "approved"

    denied_status, denied_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(denied_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )
    assert denied_status == 200
    assert denied_payload["task"]["status"] == "denied"

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tasks",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{pending_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    step_list_status, step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{pending_payload['task']['id']}/steps",
        query_params={"user_id": str(owner["user_id"])},
    )
    step_detail_status, step_detail_payload = invoke_request(
        "GET",
        f"/v0/task-steps/{step_list_payload['items'][0]['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/tasks",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{pending_payload['task']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_step_list_status, isolated_step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{pending_payload['task']['id']}/steps",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_step_detail_status, isolated_step_detail_payload = invoke_request(
        "GET",
        f"/v0/task-steps/{step_list_payload['items'][0]['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        pending_payload["task"]["id"],
        ready_payload["task"]["id"],
        denied_payload["task"]["id"],
    ]
    assert [item["status"] for item in list_payload["items"]] == [
        "executed",
        "approved",
        "denied",
    ]
    assert list_payload["summary"] == {
        "total_count": 3,
        "order": ["created_at_asc", "id_asc"],
    }

    assert detail_status == 200
    assert detail_payload["task"]["id"] == pending_payload["task"]["id"]
    assert detail_payload["task"]["status"] == "executed"
    assert detail_payload["task"]["latest_approval_id"] == pending_payload["approval"]["id"]
    assert detail_payload["task"]["latest_execution_id"] is not None
    assert step_list_status == 200
    assert [item["sequence_no"] for item in step_list_payload["items"]] == [1]
    assert step_list_payload["summary"] == {
        "task_id": pending_payload["task"]["id"],
        "total_count": 1,
        "latest_sequence_no": 1,
        "latest_status": "executed",
        "next_sequence_no": 2,
        "append_allowed": True,
        "order": ["sequence_no_asc", "created_at_asc", "id_asc"],
    }
    assert step_list_payload["items"][0] == {
        "id": step_list_payload["items"][0]["id"],
        "task_id": pending_payload["task"]["id"],
        "sequence_no": 1,
        "lineage": {
            "parent_step_id": None,
            "source_approval_id": None,
            "source_execution_id": None,
        },
        "kind": "governed_request",
        "status": "executed",
        "request": pending_payload["request"],
        "outcome": {
            "routing_decision": "approval_required",
            "approval_id": pending_payload["approval"]["id"],
            "approval_status": "approved",
            "execution_id": detail_payload["task"]["latest_execution_id"],
            "execution_status": "completed",
            "blocked_reason": None,
        },
        "trace": {
            "trace_id": execute_payload["trace"]["trace_id"],
            "trace_kind": "tool.proxy.execute",
        },
        "created_at": step_list_payload["items"][0]["created_at"],
        "updated_at": step_list_payload["items"][0]["updated_at"],
    }
    assert step_detail_status == 200
    assert step_detail_payload == {"task_step": step_list_payload["items"][0]}

    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["created_at_asc", "id_asc"]},
    }
    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert isolated_step_list_status == 404
    assert isolated_step_list_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert isolated_step_detail_status == 404
    assert isolated_step_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_step_sequence_and_transition_endpoints_preserve_parent_consistency_trace_and_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-sequence@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder-sequence@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
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
        store.create_policy(
            name="Require proxy approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "proxy.echo"},
            required_consents=[],
        )

    request_status, request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "seed-step"},
        },
    )
    assert request_status == 200
    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{request_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{request_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert execute_status == 200
    initial_detail_status, initial_detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{request_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert initial_detail_status == 200
    initial_step_list_status, initial_step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{request_payload['task']['id']}/steps",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert initial_step_list_status == 200
    initial_execution_id = initial_detail_payload["task"]["latest_execution_id"]
    assert initial_execution_id is not None

    create_status, create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{request_payload['task']['id']}/steps",
        payload={
            "user_id": str(owner["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "attributes": {"message": "step-2"},
            },
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": None,
                "approval_id": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            "lineage": {
                "parent_step_id": initial_step_list_payload["items"][0]["id"],
                "source_approval_id": request_payload["approval"]["id"],
                "source_execution_id": initial_execution_id,
            },
        },
    )

    assert create_status == 201
    assert create_payload["task"]["status"] == "pending_approval"
    assert create_payload["task"]["latest_approval_id"] == request_payload["approval"]["id"]
    assert create_payload["task_step"]["sequence_no"] == 2
    assert create_payload["task_step"]["status"] == "created"
    assert create_payload["task_step"]["lineage"] == {
        "parent_step_id": initial_step_list_payload["items"][0]["id"],
        "source_approval_id": request_payload["approval"]["id"],
        "source_execution_id": initial_execution_id,
    }
    assert create_payload["sequencing"] == {
        "task_id": request_payload["task"]["id"],
        "total_count": 2,
        "latest_sequence_no": 2,
        "latest_status": "created",
        "next_sequence_no": 3,
        "append_allowed": False,
        "order": ["sequence_no_asc", "created_at_asc", "id_asc"],
    }

    duplicate_create_status, duplicate_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{request_payload['task']['id']}/steps",
        payload={
            "user_id": str(owner["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "attributes": {"message": "step-3"},
            },
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": None,
                "approval_id": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            "lineage": {
                "parent_step_id": create_payload["task_step"]["id"],
                "source_approval_id": request_payload["approval"]["id"],
                "source_execution_id": initial_execution_id,
            },
        },
    )
    assert duplicate_create_status == 409
    assert duplicate_create_payload["detail"] == (
        {"code": "conflict", "message": "The request conflicts with the current resource state"}
    )

    invalid_transition_status, invalid_transition_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "executed",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "execution_status": "completed",
                "blocked_reason": None,
            },
        },
    )
    assert invalid_transition_status == 409
    assert invalid_transition_payload["detail"] == (
        {"code": "conflict", "message": "The request conflicts with the current resource state"}
    )

    approve_step_status, approve_step_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "approved",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": request_payload["approval"]["id"],
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
        },
    )
    assert approve_step_status == 200
    assert approve_step_payload["task"]["status"] == "approved"
    assert approve_step_payload["task"]["latest_approval_id"] == request_payload["approval"]["id"]
    assert approve_step_payload["task"]["latest_execution_id"] is None
    assert approve_step_payload["task_step"]["status"] == "approved"

    execute_step_status, execute_step_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "executed",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": request_payload["approval"]["id"],
                "execution_id": initial_execution_id,
                "execution_status": "completed",
                "blocked_reason": None,
            },
        },
    )
    assert execute_step_status == 200
    assert execute_step_payload["task"]["status"] == "executed"
    assert execute_step_payload["task"]["latest_approval_id"] == request_payload["approval"]["id"]
    assert execute_step_payload["task"]["latest_execution_id"] == initial_execution_id
    assert execute_step_payload["task_step"]["status"] == "executed"
    assert execute_step_payload["sequencing"] == {
        "task_id": request_payload["task"]["id"],
        "total_count": 2,
        "latest_sequence_no": 2,
        "latest_status": "executed",
        "next_sequence_no": 3,
        "append_allowed": True,
        "order": ["sequence_no_asc", "created_at_asc", "id_asc"],
    }

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{request_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    step_list_status, step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{request_payload['task']['id']}/steps",
        query_params={"user_id": str(owner["user_id"])},
    )
    step_detail_status, step_detail_payload = invoke_request(
        "GET",
        f"/v0/task-steps/{create_payload['task_step']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert detail_status == 200
    assert detail_payload["task"]["status"] == "executed"
    assert detail_payload["task"]["latest_approval_id"] == request_payload["approval"]["id"]
    assert detail_payload["task"]["latest_execution_id"] == initial_execution_id
    assert step_list_status == 200
    assert [item["sequence_no"] for item in step_list_payload["items"]] == [1, 2]
    assert step_list_payload["items"][1]["lineage"] == create_payload["task_step"]["lineage"]
    assert step_list_payload["summary"] == {
        "task_id": request_payload["task"]["id"],
        "total_count": 2,
        "latest_sequence_no": 2,
        "latest_status": "executed",
        "next_sequence_no": 3,
        "append_allowed": True,
        "order": ["sequence_no_asc", "created_at_asc", "id_asc"],
    }
    assert step_detail_status == 200
    assert step_detail_payload["task_step"] == step_list_payload["items"][1]
    assert step_detail_payload["task_step"]["lineage"] == create_payload["task_step"]["lineage"]
    assert step_detail_payload["task_step"]["outcome"] == {
        "routing_decision": "approval_required",
        "approval_id": request_payload["approval"]["id"],
        "approval_status": "approved",
        "execution_id": initial_execution_id,
        "execution_status": "completed",
        "blocked_reason": None,
    }

    isolated_create_status, isolated_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{request_payload['task']['id']}/steps",
        payload={
            "user_id": str(intruder["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "attributes": {},
            },
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": None,
                "approval_id": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            "lineage": {
                "parent_step_id": create_payload["task_step"]["id"],
                "source_approval_id": request_payload["approval"]["id"],
                "source_execution_id": initial_execution_id,
            },
        },
    )
    isolated_transition_status, isolated_transition_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(intruder["user_id"]),
            "status": "approved",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": str(uuid4()),
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
        },
    )
    assert isolated_create_status == 404
    assert isolated_create_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert isolated_transition_status == 404
    assert isolated_transition_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        create_trace_events = store.list_trace_events(UUID(create_payload["trace"]["trace_id"]))
        transition_trace_events = store.list_trace_events(UUID(execute_step_payload["trace"]["trace_id"]))

    assert [event["kind"] for event in create_trace_events] == [
        "task.step.continuation.request",
        "task.step.continuation.lineage",
        "task.step.continuation.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert create_trace_events[1]["payload"] == {
        "task_id": request_payload["task"]["id"],
        "parent_task_step_id": step_list_payload["items"][0]["id"],
        "parent_sequence_no": 1,
        "parent_status": "executed",
        "source_approval_id": request_payload["approval"]["id"],
        "source_execution_id": initial_execution_id,
    }
    assert [event["kind"] for event in transition_trace_events] == [
        "task.step.transition.request",
        "task.step.transition.state",
        "task.step.transition.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert transition_trace_events[1]["payload"] == {
        "task_id": request_payload["task"]["id"],
        "task_step_id": create_payload["task_step"]["id"],
        "sequence_no": 2,
        "previous_status": "approved",
        "current_status": "executed",
        "allowed_next_statuses": ["executed", "blocked"],
        "trace": {
            "trace_id": execute_step_payload["trace"]["trace_id"],
            "trace_kind": "task.step.transition",
        },
    }
    assert transition_trace_events[2]["payload"] == {
        "task_id": request_payload["task"]["id"],
        "task_step_id": create_payload["task_step"]["id"],
        "sequence_no": 2,
        "final_status": "executed",
        "parent_task_status": "executed",
        "trace": {
            "trace_id": execute_step_payload["trace"]["trace_id"],
            "trace_kind": "task.step.transition",
        },
    }


def test_task_step_mutations_reject_visible_links_from_other_task_lineages(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-lineage@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
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
        store.create_policy(
            name="Require proxy approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "proxy.echo"},
            required_consents=[],
        )

    first_request_status, first_request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "first"},
        },
    )
    assert first_request_status == 200
    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_request_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_request_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_execute_status == 200
    first_detail_status, first_detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{first_request_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert first_detail_status == 200
    first_step_list_status, first_step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{first_request_payload['task']['id']}/steps",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert first_step_list_status == 200
    first_step_id = first_step_list_payload["items"][0]["id"]
    first_execution_id = first_detail_payload["task"]["latest_execution_id"]
    assert first_execution_id is not None

    second_request_status, second_request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "second"},
        },
    )
    assert second_request_status == 200
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_request_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert second_approve_status == 200
    second_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_request_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert second_execute_status == 200
    second_detail_status, second_detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{second_request_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert second_detail_status == 200
    second_execution_id = second_detail_payload["task"]["latest_execution_id"]
    assert second_execution_id is not None

    wrong_create_status, wrong_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{first_request_payload['task']['id']}/steps",
        payload={
            "user_id": str(owner["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "attributes": {"message": "lineage-mismatch"},
            },
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": None,
                "approval_id": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            "lineage": {
                "parent_step_id": first_step_id,
                "source_approval_id": second_request_payload["approval"]["id"],
                "source_execution_id": None,
            },
        },
    )
    assert wrong_create_status == 409
    assert wrong_create_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    create_status, create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{first_request_payload['task']['id']}/steps",
        payload={
            "user_id": str(owner["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "attributes": {"message": "valid"},
            },
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": None,
                "approval_id": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            "lineage": {
                "parent_step_id": first_step_id,
                "source_approval_id": first_request_payload["approval"]["id"],
                "source_execution_id": first_execution_id,
            },
        },
    )
    assert create_status == 201

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "approved",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": first_request_payload["approval"]["id"],
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
        },
    )
    assert approve_status == 200

    wrong_execute_status, wrong_execute_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "executed",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": first_request_payload["approval"]["id"],
                "execution_id": second_execution_id,
                "execution_status": "completed",
                "blocked_reason": None,
            },
        },
    )
    assert wrong_execute_status == 409
    assert wrong_execute_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    assert first_execution_id != second_execution_id
    assert first_request_payload["approval"]["id"] != second_request_payload["approval"]["id"]
    assert approve_payload["task"]["latest_approval_id"] == first_request_payload["approval"]["id"]
