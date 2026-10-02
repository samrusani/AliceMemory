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


def seed_user(
    database_url: str,
    *,
    email: str,
    agent_profile_id: str = "assistant_default",
) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Approval thread", agent_profile_id=agent_profile_id)

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def test_approval_request_persists_record_for_approval_required_route(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        policy = store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    status, payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )

    assert status == 200
    assert list(payload) == [
        "request",
        "decision",
        "tool",
        "reasons",
        "task",
        "approval",
        "routing_trace",
        "trace",
    ]
    assert payload["decision"] == "approval_required"
    assert payload["task"]["status"] == "pending_approval"
    assert payload["task"]["latest_approval_id"] == payload["approval"]["id"]
    assert payload["task"]["latest_execution_id"] is None
    assert payload["approval"] is not None
    assert payload["approval"]["status"] == "pending"
    assert payload["approval"]["task_step_id"] is not None
    assert payload["approval"]["resolution"] is None
    assert payload["approval"]["request"] == payload["request"]
    assert payload["approval"]["tool"] == payload["tool"]
    assert payload["approval"]["routing"] == {
        "decision": "approval_required",
        "reasons": payload["reasons"],
        "trace": payload["routing_trace"],
    }
    assert payload["reasons"][-1] == {
        "code": "policy_effect_require_approval",
        "source": "policy",
        "message": "Policy effect resolved the decision to 'require_approval'.",
        "tool_id": str(tool["id"]),
        "policy_id": str(policy["id"]),
        "consent_key": None,
    }
    assert payload["routing_trace"]["trace_event_count"] == 3
    assert payload["trace"]["trace_event_count"] == 8

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        approvals = store.list_approvals()
        tasks = store.list_tasks()
        task_steps = store.list_task_steps_for_task(tasks[0]["id"])
        approval_trace = store.get_trace(UUID(payload["trace"]["trace_id"]))
        approval_trace_events = store.list_trace_events(UUID(payload["trace"]["trace_id"]))

    assert len(approvals) == 1
    assert len(tasks) == 1
    assert len(task_steps) == 1
    assert approvals[0]["id"] == UUID(payload["approval"]["id"])
    assert approvals[0]["task_step_id"] == task_steps[0]["id"]
    assert tasks[0]["id"] == UUID(payload["task"]["id"])
    assert approval_trace["kind"] == "approval.request"
    assert approval_trace["compiler_version"] == "approval_request_v0"
    assert approval_trace["limits"] == {
        "order": ["created_at_asc", "id_asc"],
        "persisted": True,
    }
    assert [event["kind"] for event in approval_trace_events] == [
        "approval.request.request",
        "approval.request.routing",
        "approval.request.persisted",
        "approval.request.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert approval_trace_events[1]["payload"] == {
        "decision": "approval_required",
        "tool_id": str(tool["id"]),
        "tool_key": "shell.exec",
        "tool_version": "1.0.0",
        "routing_trace_id": payload["routing_trace"]["trace_id"],
        "routing_trace_event_count": 3,
        "reasons": payload["reasons"],
    }
    assert approval_trace_events[4]["payload"] == {
        "task_id": payload["task"]["id"],
        "source": "approval_request",
        "previous_status": None,
        "current_status": "pending_approval",
        "latest_approval_id": payload["approval"]["id"],
        "latest_execution_id": None,
    }
    assert approval_trace_events[2]["payload"] == {
        "approval_id": payload["approval"]["id"],
        "task_step_id": payload["approval"]["task_step_id"],
        "decision": "approval_required",
        "persisted": True,
    }
    assert approval_trace_events[6]["payload"] == {
        "task_id": payload["task"]["id"],
        "task_step_id": str(task_steps[0]["id"]),
        "source": "approval_request",
        "sequence_no": 1,
        "kind": "governed_request",
        "previous_status": None,
        "current_status": "created",
        "trace": {
            "trace_id": payload["trace"]["trace_id"],
            "trace_kind": "approval.request",
        },
    }


def test_approval_request_routing_excludes_profile_mismatched_policies(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(
        migrated_database_urls["app"],
        email="owner@example.com",
        agent_profile_id="coach_default",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        mismatched = store.create_policy(
            agent_profile_id="assistant_default",
            name="Mismatched deny shell",
            action="tool.run",
            scope="workspace",
            effect="deny",
            priority=1,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )
        global_allow = store.create_policy(
            agent_profile_id=None,
            name="Global allow shell",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    status, payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )

    assert status == 200
    assert payload["decision"] == "ready"
    assert payload["task"]["status"] == "approved"
    assert payload["approval"] is None
    assert payload["reasons"][-1] == {
        "code": "policy_effect_allow",
        "source": "policy",
        "message": "Policy effect resolved the decision to 'allow'.",
        "tool_id": str(tool["id"]),
        "policy_id": str(global_allow["id"]),
        "consent_key": None,
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        approvals = store.list_approvals()
        routing_trace = store.get_trace(UUID(payload["routing_trace"]["trace_id"]))
        routing_events = store.list_trace_events(UUID(payload["routing_trace"]["trace_id"]))

    assert approvals == []
    assert routing_trace["limits"]["active_policy_count"] == 1
    assert routing_events[1]["payload"]["matched_policy_id"] == str(global_allow["id"])
    assert routing_events[1]["payload"]["matched_policy_id"] != str(mismatched["id"])


def test_approval_request_does_not_create_records_for_ready_or_denied_routes(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="web_access",
            status="granted",
            metadata={"source": "settings"},
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
            name="Allow docs browser",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=10,
            active=True,
            conditions={"tool_key": "browser.open", "domain_hint": "docs"},
            required_consents=["web_access"],
        )

    ready_status, ready_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(ready_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {},
        },
    )
    denied_status, denied_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(denied_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert ready_status == 200
    assert ready_payload["decision"] == "ready"
    assert ready_payload["task"]["status"] == "approved"
    assert ready_payload["approval"] is None
    assert denied_status == 200
    assert denied_payload["decision"] == "denied"
    assert denied_payload["task"]["status"] == "denied"
    assert denied_payload["approval"] is None

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        approvals = store.list_approvals()
        tasks = store.list_tasks()
        ready_task_steps = store.list_task_steps_for_task(tasks[0]["id"])
        denied_task_steps = store.list_task_steps_for_task(tasks[1]["id"])

    assert approvals == []
    assert [task["status"] for task in tasks] == ["approved", "denied"]
    assert [task_step["status"] for task_step in ready_task_steps] == ["approved"]
    assert [task_step["status"] for task_step in denied_task_steps] == ["denied"]


def test_approval_endpoints_list_and_detail_are_deterministic_and_user_scoped(
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
        first_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        second_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="2.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    first_status, first_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(first_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "pwd"},
        },
    )
    second_status, second_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(second_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/approvals",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/approvals/{second_payload['approval']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/approvals",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/approvals/{first_payload['approval']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert first_status == 200
    assert second_status == 200
    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        first_payload["approval"]["id"],
        second_payload["approval"]["id"],
    ]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"approval": second_payload["approval"]}

    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["created_at_asc", "id_asc"]},
    }
    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_approval_resolution_endpoints_update_reads_and_emit_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        first_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        second_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="2.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    _, first_request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(first_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "pwd"},
        },
    )
    _, second_request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(second_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )
    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{first_request_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    reject_status, reject_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_request_payload['approval']['id']}/reject",
        payload={"user_id": str(owner["user_id"])},
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/approvals",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/approvals/{second_request_payload['approval']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert approve_status == 200
    assert list(approve_payload) == ["approval", "trace"]
    assert approve_payload["approval"]["status"] == "approved"
    assert approve_payload["approval"]["task_step_id"] == first_request_payload["approval"]["task_step_id"]
    assert approve_payload["approval"]["resolution"] is not None
    assert approve_payload["trace"]["trace_event_count"] == 7

    assert reject_status == 200
    assert list(reject_payload) == ["approval", "trace"]
    assert reject_payload["approval"]["status"] == "rejected"
    assert reject_payload["approval"]["task_step_id"] == second_request_payload["approval"]["task_step_id"]
    assert reject_payload["approval"]["resolution"] is not None
    assert reject_payload["trace"]["trace_event_count"] == 7

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        first_request_payload["approval"]["id"],
        second_request_payload["approval"]["id"],
    ]
    assert [item["status"] for item in list_payload["items"]] == ["approved", "rejected"]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"approval": reject_payload["approval"]}

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        approve_trace = store.get_trace(UUID(approve_payload["trace"]["trace_id"]))
        approve_trace_events = store.list_trace_events(UUID(approve_payload["trace"]["trace_id"]))
        reject_trace = store.get_trace(UUID(reject_payload["trace"]["trace_id"]))
        reject_trace_events = store.list_trace_events(UUID(reject_payload["trace"]["trace_id"]))

    assert approve_trace["kind"] == "approval.resolve"
    assert approve_trace["compiler_version"] == "approval_resolution_v0"
    assert approve_trace["limits"] == {
        "order": ["created_at_asc", "id_asc"],
        "requested_action": "approve",
        "outcome": "resolved",
    }
    assert [event["kind"] for event in approve_trace_events] == [
        "approval.resolution.request",
        "approval.resolution.state",
        "approval.resolution.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert approve_trace_events[1]["payload"]["current_status"] == "approved"
    assert approve_trace_events[1]["payload"]["task_step_id"] == first_request_payload["approval"]["task_step_id"]
    assert approve_trace_events[1]["payload"]["resolved_by_user_id"] == str(owner["user_id"])

    assert reject_trace["kind"] == "approval.resolve"
    assert reject_trace["compiler_version"] == "approval_resolution_v0"
    assert reject_trace["limits"] == {
        "order": ["created_at_asc", "id_asc"],
        "requested_action": "reject",
        "outcome": "resolved",
    }
    assert [event["kind"] for event in reject_trace_events] == [
        "approval.resolution.request",
        "approval.resolution.state",
        "approval.resolution.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert reject_trace_events[1]["payload"]["current_status"] == "rejected"
    assert reject_trace_events[1]["payload"]["task_step_id"] == second_request_payload["approval"]["task_step_id"]
    assert reject_trace_events[1]["payload"]["resolved_by_user_id"] == str(owner["user_id"])


def test_approval_resolution_does_not_reopen_cancelled_linked_task_run(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-cancelled-run@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )
    assert create_status == 200
    task_id = create_payload["task"]["id"]
    approval_id = create_payload["approval"]["id"]

    run_create_status, run_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{task_id}/runs",
        payload={
            "user_id": str(owner["user_id"]),
            "max_ticks": 3,
            "checkpoint": {
                "cursor": 0,
                "target_steps": 1,
                "wait_for_signal": False,
            },
        },
    )
    assert run_create_status == 201
    run_id = run_create_payload["task_run"]["id"]

    run_tick_status, run_tick_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{run_id}/tick",
        payload={"user_id": str(owner["user_id"])},
    )
    assert run_tick_status == 200
    assert run_tick_payload["task_run"]["status"] == "waiting_approval"

    run_cancel_status, run_cancel_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{run_id}/cancel",
        payload={"user_id": str(owner["user_id"])},
    )
    assert run_cancel_status == 200
    assert run_cancel_payload["task_run"]["status"] == "cancelled"
    assert run_cancel_payload["task_run"]["stop_reason"] == "cancelled"

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{approval_id}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"
    assert approve_payload["trace"]["trace_event_count"] == 7

    run_detail_status, run_detail_payload = invoke_request(
        "GET",
        f"/v0/task-runs/{run_id}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert run_detail_status == 200
    assert run_detail_payload["task_run"]["status"] == "cancelled"
    assert run_detail_payload["task_run"]["stop_reason"] == "cancelled"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace_events = store.list_trace_events(UUID(approve_payload["trace"]["trace_id"]))

    assert "approval.resolution.run" not in [event["kind"] for event in trace_events]


def test_approval_resolution_rejects_duplicate_conflicting_and_cross_user_attempts(
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
        tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    _, request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"command": "ls"},
        },
    )
    approval_id = request_payload["approval"]["id"]

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{approval_id}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    duplicate_status, duplicate_payload = invoke_request(
        "POST",
        f"/v0/approvals/{approval_id}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    conflict_status, conflict_payload = invoke_request(
        "POST",
        f"/v0/approvals/{approval_id}/reject",
        payload={"user_id": str(owner["user_id"])},
    )
    intruder_status, intruder_payload = invoke_request(
        "POST",
        f"/v0/approvals/{approval_id}/reject",
        payload={"user_id": str(intruder["user_id"])},
    )

    assert first_approve_status == 200
    assert duplicate_status == 409
    assert duplicate_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    assert conflict_status == 409
    assert conflict_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    assert intruder_status == 404
    assert intruder_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        approval = store.get_approval_optional(UUID(approval_id))
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, limits
                FROM traces
                WHERE thread_id = %s
                  AND kind = 'approval.resolve'
                ORDER BY created_at ASC, id ASC
                """,
                (owner["thread_id"],),
            )
            trace_rows = cur.fetchall()
        duplicate_trace = trace_rows[-2]
        conflict_trace = trace_rows[-1]
        duplicate_events = store.list_trace_events(duplicate_trace["id"])
        conflict_events = store.list_trace_events(conflict_trace["id"])

    assert approval is not None
    assert approval["status"] == "approved"
    assert duplicate_trace["limits"] == {
        "order": ["created_at_asc", "id_asc"],
        "requested_action": "approve",
        "outcome": "duplicate_rejected",
    }
    assert [event["kind"] for event in duplicate_events] == [
        "approval.resolution.request",
        "approval.resolution.state",
        "approval.resolution.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert duplicate_events[1]["payload"] == {
        "approval_id": approval_id,
        "task_step_id": str(approval["task_step_id"]),
        "requested_action": "approve",
        "previous_status": "approved",
        "outcome": "duplicate_rejected",
        "current_status": "approved",
        "resolved_at": approval["resolved_at"].isoformat(),
        "resolved_by_user_id": str(owner["user_id"]),
    }
    assert conflict_trace["limits"] == {
        "order": ["created_at_asc", "id_asc"],
        "requested_action": "reject",
        "outcome": "conflict_rejected",
    }
    assert [event["kind"] for event in conflict_events] == [
        "approval.resolution.request",
        "approval.resolution.state",
        "approval.resolution.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert conflict_events[1]["payload"] == {
        "approval_id": approval_id,
        "task_step_id": str(approval["task_step_id"]),
        "requested_action": "reject",
        "previous_status": "approved",
        "outcome": "conflict_rejected",
        "current_status": "approved",
        "resolved_at": approval["resolved_at"].isoformat(),
        "resolved_by_user_id": str(owner["user_id"]),
    }


def test_approval_resolution_rejects_inconsistent_linkage_without_mutating_task_steps(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-boundary@example.com")
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

    _, request_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "initial"},
        },
    )
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
    assert detail_status == 200
    assert step_list_status == 200
    initial_execution_id = detail_payload["task"]["latest_execution_id"]
    assert initial_execution_id is not None

    create_step_status, create_step_payload = invoke_request(
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
                "parent_step_id": step_list_payload["items"][0]["id"],
                "source_approval_id": request_payload["approval"]["id"],
                "source_execution_id": initial_execution_id,
            },
        },
    )
    assert create_step_status == 201

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        conn.execute(
            "UPDATE approvals SET task_step_id = %s WHERE id = %s",
            (
                create_step_payload["task_step"]["id"],
                request_payload["approval"]["id"],
            ),
        )

    boundary_status, boundary_payload = invoke_request(
        "POST",
        f"/v0/approvals/{request_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )

    assert boundary_status == 409
    assert boundary_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        task = store.get_task_optional(UUID(request_payload["task"]["id"]))
        task_steps = store.list_task_steps_for_task(UUID(request_payload["task"]["id"]))
        approval = store.get_approval_optional(UUID(request_payload["approval"]["id"]))
        approval_resolve_traces = store.conn.execute(
            """
            SELECT id
            FROM traces
            WHERE thread_id = %s
              AND kind = 'approval.resolve'
            ORDER BY created_at ASC, id ASC
            """,
            (owner["thread_id"],),
        ).fetchall()

    assert task is not None
    assert approval is not None
    assert task["status"] == "pending_approval"
    assert task["latest_approval_id"] == UUID(request_payload["approval"]["id"])
    assert task["latest_execution_id"] is None
    assert len(task_steps) == 2
    assert task_steps[0]["status"] == "executed"
    assert task_steps[0]["trace_id"] == UUID(execute_payload["trace"]["trace_id"])
    assert task_steps[0]["outcome"]["execution_id"] == initial_execution_id
    assert task_steps[1]["status"] == "created"
    assert task_steps[1]["id"] == UUID(create_step_payload["task_step"]["id"])
    assert task_steps[1]["trace_kind"] == "task.step.continuation"
    assert approval["status"] == "approved"
    assert approval["task_step_id"] == UUID(create_step_payload["task_step"]["id"])
    assert len(approval_resolve_traces) == 1
