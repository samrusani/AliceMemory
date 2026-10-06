from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.config import Settings
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


def seed_user(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Proxy execution thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def create_thread(
    database_url: str,
    *,
    user_id: UUID,
    title: str,
    agent_profile_id: str,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.create_thread(title, agent_profile_id)
    return thread["id"]


def create_tool_and_policy(
    database_url: str,
    *,
    user_id: UUID,
    tool_key: str,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key=tool_key,
            name="Proxy Tool",
            description="Deterministic proxy tool.",
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
            name=f"Require approval for {tool_key}",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": tool_key},
            required_consents=[],
        )
    return tool["id"]


def create_pending_approval(
    *,
    user_id: UUID,
    thread_id: UUID,
    tool_id: UUID,
) -> tuple[int, dict[str, Any]]:
    return invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "tool_id": str(tool_id),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {"message": "hello", "count": 2},
        },
    )


def create_execution_budget(
    database_url: str,
    *,
    user_id: UUID,
    agent_profile_id: str | None = None,
    tool_key: str | None,
    domain_hint: str | None,
    max_completed_executions: int,
    rolling_window_seconds: int | None = None,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        budget = store.create_execution_budget(
            agent_profile_id=agent_profile_id,
            tool_key=tool_key,
            domain_hint=domain_hint,
            max_completed_executions=max_completed_executions,
            rolling_window_seconds=rolling_window_seconds,
            supersedes_budget_id=None,
        )
    return budget["id"]


def set_execution_executed_at(
    admin_database_url: str,
    user_id: UUID,
    *,
    execution_id: UUID,
    executed_at_sql: str,
) -> None:
    with psycopg.connect(admin_database_url) as conn:
        set_current_user(conn, user_id)
        conn.execute(
            f"UPDATE tool_executions SET executed_at = {executed_at_sql} WHERE id = %s",
            (execution_id,),
        )
        conn.commit()


def set_approval_request_thread_id(
    admin_database_url: str,
    user_id: UUID,
    *,
    approval_id: UUID,
    request_thread_id: str,
) -> None:
    with psycopg.connect(admin_database_url) as conn:
        set_current_user(conn, user_id)
        conn.execute(
            """
            UPDATE approvals
            SET request = jsonb_set(request, '{thread_id}', to_jsonb(%s::text), true)
            WHERE id = %s
            """,
            (request_thread_id, approval_id),
        )
        conn.commit()


def test_execute_approved_proxy_endpoint_executes_only_approved_requests(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert execute_status == 200
    assert list(execute_payload) == ["request", "approval", "tool", "result", "events", "trace"]
    assert execute_payload["request"] == {
        "approval_id": create_payload["approval"]["id"],
        "task_step_id": create_payload["approval"]["task_step_id"],
    }
    assert execute_payload["approval"]["id"] == create_payload["approval"]["id"]
    assert execute_payload["approval"]["status"] == "approved"
    assert execute_payload["tool"]["id"] == str(tool_id)
    assert execute_payload["tool"]["tool_key"] == "proxy.echo"
    assert execute_payload["result"] == {
        "handler_key": "proxy.echo",
        "status": "completed",
        "output": {
            "mode": "no_side_effect",
            "tool_key": "proxy.echo",
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": None,
            "risk_hint": None,
            "attributes": {"message": "hello", "count": 2},
        },
    }
    assert execute_payload["events"]["request_sequence_no"] == 1
    assert execute_payload["events"]["result_sequence_no"] == 2
    assert execute_payload["trace"]["trace_event_count"] == 9

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        thread_events = store.list_thread_events(owner["thread_id"])
        tasks = store.list_tasks()
        task_steps = store.list_task_steps_for_task(tasks[0]["id"])
        tool_executions = store.list_tool_executions()
        execute_trace = store.get_trace(UUID(execute_payload["trace"]["trace_id"]))
        execute_trace_events = store.list_trace_events(UUID(execute_payload["trace"]["trace_id"]))

    assert [event["kind"] for event in thread_events] == [
        "tool.proxy.execution.request",
        "tool.proxy.execution.result",
    ]
    assert len(tool_executions) == 1
    assert len(tasks) == 1
    assert len(task_steps) == 1
    assert tasks[0]["status"] == "executed"
    assert tasks[0]["latest_execution_id"] == tool_executions[0]["id"]
    assert task_steps[0]["status"] == "executed"
    assert tool_executions[0]["approval_id"] == UUID(create_payload["approval"]["id"])
    assert tool_executions[0]["task_step_id"] == task_steps[0]["id"]
    assert tool_executions[0]["thread_id"] == owner["thread_id"]
    assert tool_executions[0]["tool_id"] == tool_id
    assert tool_executions[0]["trace_id"] == UUID(execute_payload["trace"]["trace_id"])
    assert tool_executions[0]["handler_key"] == "proxy.echo"
    assert tool_executions[0]["status"] == "completed"
    assert tool_executions[0]["request"] == thread_events[0]["payload"]["request"]
    assert tool_executions[0]["tool"]["tool_key"] == "proxy.echo"
    assert tool_executions[0]["result"] == {
        "handler_key": "proxy.echo",
        "status": "completed",
        "output": execute_payload["result"]["output"],
        "reason": None,
    }
    assert thread_events[0]["payload"] == {
        "approval_id": create_payload["approval"]["id"],
        "task_step_id": create_payload["approval"]["task_step_id"],
        "tool_id": str(tool_id),
        "tool_key": "proxy.echo",
        "request": {
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool_id),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": None,
            "risk_hint": None,
            "attributes": {"message": "hello", "count": 2},
        },
    }
    assert execute_trace["kind"] == "tool.proxy.execute"
    assert execute_trace["compiler_version"] == "proxy_execution_v0"
    assert execute_trace["limits"] == {
        "approval_status": "approved",
        "enabled_handler_keys": ["proxy.echo"],
        "budget_match_order": ["specificity_desc", "created_at_asc", "id_asc"],
    }
    assert [event["kind"] for event in execute_trace_events] == [
        "tool.proxy.execute.request",
        "tool.proxy.execute.approval",
        "tool.proxy.execute.budget",
        "tool.proxy.execute.dispatch",
        "tool.proxy.execute.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert execute_trace_events[0]["payload"] == {
        "approval_id": create_payload["approval"]["id"],
        "task_step_id": create_payload["approval"]["task_step_id"],
    }
    assert execute_trace_events[1]["payload"]["task_step_id"] == create_payload["approval"]["task_step_id"]
    assert execute_trace_events[2]["payload"]["decision"] == "allow"
    assert execute_trace_events[3]["payload"]["dispatch_status"] == "executed"
    assert execute_trace_events[3]["payload"]["task_step_id"] == create_payload["approval"]["task_step_id"]
    assert execute_trace_events[4]["payload"]["request_event_id"] == execute_payload["events"]["request_event_id"]
    assert execute_trace_events[4]["payload"]["task_step_id"] == create_payload["approval"]["task_step_id"]
    assert execute_trace_events[7]["payload"] == {
        "task_id": create_payload["task"]["id"],
        "task_step_id": str(task_steps[0]["id"]),
        "source": "proxy_execution",
        "sequence_no": 1,
        "kind": "governed_request",
        "previous_status": "approved",
        "current_status": "executed",
        "trace": {
            "trace_id": execute_payload["trace"]["trace_id"],
            "trace_kind": "tool.proxy.execute",
        },
    }


def test_execute_approved_proxy_endpoint_rejects_pending_approval(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert execute_status == 409
    assert execute_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace_rows = store.conn.execute(
            "SELECT id, kind, limits FROM traces WHERE kind = %s ORDER BY created_at ASC, id ASC",
            ("tool.proxy.execute",),
        ).fetchall()
        trace_events = store.list_trace_events(trace_rows[-1]["id"])
        thread_events = store.list_thread_events(owner["thread_id"])

    assert thread_events == []
    assert trace_rows[-1]["limits"] == {
        "approval_status": "pending",
        "enabled_handler_keys": ["proxy.echo"],
        "budget_match_order": ["specificity_desc", "created_at_asc", "id_asc"],
    }
    assert trace_events[2]["payload"]["dispatch_status"] == "blocked"
    assert trace_events[3]["payload"]["execution_status"] == "blocked"


def test_execute_approved_proxy_endpoint_rejects_rejected_approval(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    reject_status, reject_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/reject",
        payload={"user_id": str(owner["user_id"])},
    )
    assert reject_status == 200
    assert reject_payload["approval"]["status"] == "rejected"

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert execute_status == 409
    assert execute_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }


def test_execute_approved_proxy_endpoint_rejects_missing_handler(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.missing",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert execute_status == 409
    assert execute_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace_rows = store.conn.execute(
            "SELECT id FROM traces WHERE kind = %s ORDER BY created_at ASC, id ASC",
            ("tool.proxy.execute",),
        ).fetchall()
        trace_events = store.list_trace_events(trace_rows[-1]["id"])
        tool_executions = store.list_tool_executions()
        thread_events = store.list_thread_events(owner["thread_id"])

    assert thread_events == []
    assert len(tool_executions) == 1
    assert tool_executions[0]["approval_id"] == UUID(create_payload["approval"]["id"])
    assert tool_executions[0]["task_step_id"] == UUID(create_payload["approval"]["task_step_id"])
    assert tool_executions[0]["handler_key"] is None
    assert tool_executions[0]["status"] == "blocked"
    assert tool_executions[0]["request_event_id"] is None
    assert tool_executions[0]["result_event_id"] is None
    assert tool_executions[0]["result"] == {
        "handler_key": None,
        "status": "blocked",
        "output": None,
        "reason": "tool 'proxy.missing' has no registered proxy handler",
    }
    assert trace_events[2]["payload"]["decision"] == "allow"
    assert trace_events[3]["payload"] == {
        "approval_id": create_payload["approval"]["id"],
        "task_step_id": create_payload["approval"]["task_step_id"],
        "tool_id": str(tool_id),
        "tool_key": "proxy.missing",
        "handler_key": None,
        "dispatch_status": "blocked",
        "reason": "tool 'proxy.missing' has no registered proxy handler",
        "result_status": "blocked",
        "output": None,
    }

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tool-executions",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tool-executions/{tool_executions[0]['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert list_status == 200
    assert list_payload["items"][0]["task_step_id"] == create_payload["approval"]["task_step_id"]
    assert list_payload["items"][0]["status"] == "blocked"
    assert list_payload["items"][0]["request_event_id"] is None
    assert list_payload["items"][0]["result_event_id"] is None
    assert list_payload["items"][0]["result"]["reason"] == "tool 'proxy.missing' has no registered proxy handler"
    assert detail_status == 200
    assert detail_payload == {"execution": list_payload["items"][0]}


def test_execute_approved_proxy_endpoint_marks_linked_run_failed_when_blocked(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-linked-run@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.missing",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    run_create_status, run_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{create_payload['task']['id']}/runs",
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

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"
    assert approve_payload["approval"]["task_run_id"] == run_id

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert execute_status == 409
    assert execute_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    run_detail_status, run_detail_payload = invoke_request(
        "GET",
        f"/v0/task-runs/{run_id}",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert run_detail_status == 200
    assert run_detail_payload["task_run"]["status"] == "failed"
    assert run_detail_payload["task_run"]["stop_reason"] == "policy_blocked"
    assert run_detail_payload["task_run"]["failure_class"] == "policy"
    assert run_detail_payload["task_run"]["retry_posture"] == "terminal"
    assert run_detail_payload["task_run"]["checkpoint"]["last_execution_status"] == "blocked"
    assert run_detail_payload["task_run"]["checkpoint"]["resolved_approval_id"] == create_payload["approval"]["id"]


def test_execute_approved_proxy_endpoint_enforces_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    other_user = seed_user(migrated_database_urls["app"], email="other@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(other_user["user_id"])},
    )

    assert execute_status == 404
    assert execute_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_execute_approved_proxy_endpoint_updates_the_explicitly_linked_later_step(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-step-linkage@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert execute_status == 200

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tasks/{create_payload['task']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    step_list_status, step_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{create_payload['task']['id']}/steps",
        query_params={"user_id": str(owner["user_id"])},
    )
    assert detail_status == 200
    assert step_list_status == 200
    initial_execution_id = detail_payload["task"]["latest_execution_id"]
    assert initial_execution_id is not None

    create_step_status, create_step_payload = invoke_request(
        "POST",
        f"/v0/tasks/{create_payload['task']['id']}/steps",
        payload={
            "user_id": str(owner["user_id"]),
            "kind": "governed_request",
            "status": "created",
            "request": {
                "thread_id": str(owner["thread_id"]),
                "tool_id": str(tool_id),
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
                "source_approval_id": create_payload["approval"]["id"],
                "source_execution_id": initial_execution_id,
            },
        },
    )
    assert create_step_status == 201

    transition_status, transition_payload = invoke_request(
        "POST",
        f"/v0/task-steps/{create_step_payload['task_step']['id']}/transition",
        payload={
            "user_id": str(owner["user_id"]),
            "status": "approved",
            "outcome": {
                "routing_decision": "approval_required",
                "approval_status": "approved",
                "approval_id": create_payload["approval"]["id"],
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
        },
    )
    assert transition_status == 200
    assert transition_payload["task_step"]["status"] == "approved"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        relinked = store.update_approval_task_step_optional(
            approval_id=UUID(create_payload["approval"]["id"]),
            task_step_id=UUID(create_step_payload["task_step"]["id"]),
        )
        assert relinked is not None

    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert second_execute_status == 200
    assert second_execute_payload["request"] == {
        "approval_id": create_payload["approval"]["id"],
        "task_step_id": create_step_payload["task_step"]["id"],
    }
    assert second_execute_payload["approval"]["task_step_id"] == create_step_payload["task_step"]["id"]

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        task = store.get_task_optional(UUID(create_payload["task"]["id"]))
        task_steps = store.list_task_steps_for_task(UUID(create_payload["task"]["id"]))
        tool_executions = store.list_tool_executions()
        proxy_traces = store.conn.execute(
            """
            SELECT id
            FROM traces
            WHERE thread_id = %s
              AND kind = 'tool.proxy.execute'
            ORDER BY created_at ASC, id ASC
            """,
            (owner["thread_id"],),
        ).fetchall()

    assert task is not None
    assert task["status"] == "executed"
    assert task["latest_approval_id"] == UUID(create_payload["approval"]["id"])
    assert len(task_steps) == 2
    assert task_steps[0]["status"] == "executed"
    assert task_steps[0]["trace_id"] == UUID(execute_payload["trace"]["trace_id"])
    assert task_steps[0]["outcome"]["execution_id"] == initial_execution_id
    assert task_steps[1]["status"] == "executed"
    assert task_steps[1]["id"] == UUID(create_step_payload["task_step"]["id"])
    assert task_steps[1]["trace_id"] == UUID(second_execute_payload["trace"]["trace_id"])
    assert task_steps[1]["outcome"]["approval_id"] == create_payload["approval"]["id"]
    assert task_steps[1]["outcome"]["execution_status"] == "completed"
    assert len(tool_executions) == 2
    assert task["latest_execution_id"] == tool_executions[1]["id"]
    assert tool_executions[1]["task_step_id"] == UUID(create_step_payload["task_step"]["id"])
    assert task_steps[1]["outcome"]["execution_id"] == str(tool_executions[1]["id"])
    assert len(proxy_traces) == 2


def test_execute_approved_proxy_endpoint_fail_closes_when_runtime_context_is_invalid(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    create_status, create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert create_status == 200

    approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200

    set_approval_request_thread_id(
        migrated_database_urls["admin"],
        user_id=owner["user_id"],
        approval_id=UUID(create_payload["approval"]["id"]),
        request_thread_id="not-a-uuid",
    )

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert execute_status == 200
    assert execute_payload["events"] is None
    assert execute_payload["result"] == {
        "handler_key": None,
        "status": "blocked",
        "output": None,
        "reason": (
            "execution budget invariance blocks execution: invalid request thread/profile "
            "context: request.thread_id 'not-a-uuid' is not a valid UUID"
        ),
        "budget_decision": {
            "matched_budget_id": None,
            "tool_key": "proxy.echo",
            "domain_hint": None,
            "budget_tool_key": None,
            "budget_domain_hint": None,
            "max_completed_executions": None,
            "rolling_window_seconds": None,
            "count_scope": "lifetime",
            "window_started_at": None,
            "completed_execution_count": 0,
            "projected_completed_execution_count": 1,
            "decision": "block",
            "reason": "invalid_request_context",
            "order": ["specificity_desc", "created_at_asc", "id_asc"],
            "history_order": ["executed_at_asc", "id_asc"],
            "request_thread_id": "not-a-uuid",
            "context_resolution": "invalid",
            "context_reason": "request.thread_id 'not-a-uuid' is not a valid UUID",
        },
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        stored_executions = store.list_tool_executions()
        blocked_trace_events = store.list_trace_events(UUID(execute_payload["trace"]["trace_id"]))
        thread_events = store.list_thread_events(owner["thread_id"])

    assert len(stored_executions) == 1
    assert stored_executions[0]["status"] == "blocked"
    assert stored_executions[0]["result"] == execute_payload["result"]
    assert stored_executions[0]["request_event_id"] is None
    assert stored_executions[0]["result_event_id"] is None
    assert thread_events == []
    assert blocked_trace_events[2]["payload"] == execute_payload["result"]["budget_decision"]
    assert blocked_trace_events[3]["payload"]["dispatch_status"] == "blocked"
    assert blocked_trace_events[3]["payload"]["budget_context"] == {
        "request_thread_id": "not-a-uuid",
        "context_resolution": "invalid",
        "context_reason": "request.thread_id 'not-a-uuid' is not a valid UUID",
    }


def test_execute_approved_proxy_endpoint_blocks_when_execution_budget_is_exceeded(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    first_create_status, first_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_create_status, second_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_create_status == 200
    assert second_create_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, first_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_execute_status == 200
    assert second_execute_status == 200
    assert second_execute_payload["events"] is None
    assert second_execute_payload["result"] == {
        "handler_key": None,
        "status": "blocked",
        "output": None,
        "reason": (
            f"execution budget {budget_id} blocks execution: projected completed executions 2 would exceed limit 1"
        ),
        "budget_decision": {
            "matched_budget_id": str(budget_id),
            "tool_key": "proxy.echo",
            "domain_hint": None,
            "budget_tool_key": "proxy.echo",
            "budget_domain_hint": None,
            "max_completed_executions": 1,
            "rolling_window_seconds": None,
            "count_scope": "lifetime",
            "window_started_at": None,
            "completed_execution_count": 1,
            "projected_completed_execution_count": 2,
            "decision": "block",
            "reason": "budget_exceeded",
            "order": ["specificity_desc", "created_at_asc", "id_asc"],
            "history_order": ["executed_at_asc", "id_asc"],
        },
    }

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        stored_executions = store.list_tool_executions()
        blocked_trace = store.get_trace(UUID(second_execute_payload["trace"]["trace_id"]))
        blocked_trace_events = store.list_trace_events(UUID(second_execute_payload["trace"]["trace_id"]))
        thread_events = store.list_thread_events(owner["thread_id"])

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tool-executions",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tool-executions/{stored_executions[1]['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert len(stored_executions) == 2
    assert [row["status"] for row in stored_executions] == ["completed", "blocked"]
    assert stored_executions[1]["task_step_id"] == UUID(second_execute_payload["request"]["task_step_id"])
    assert stored_executions[1]["result"] == second_execute_payload["result"]
    assert stored_executions[1]["request_event_id"] is None
    assert stored_executions[1]["result_event_id"] is None
    assert [event["kind"] for event in thread_events] == [
        "tool.proxy.execution.request",
        "tool.proxy.execution.result",
    ]
    assert blocked_trace["limits"] == {
        "approval_status": "approved",
        "enabled_handler_keys": ["proxy.echo"],
        "budget_match_order": ["specificity_desc", "created_at_asc", "id_asc"],
    }
    assert [event["kind"] for event in blocked_trace_events] == [
        "tool.proxy.execute.request",
        "tool.proxy.execute.approval",
        "tool.proxy.execute.budget",
        "tool.proxy.execute.dispatch",
        "tool.proxy.execute.summary",
        "task.lifecycle.state",
        "task.lifecycle.summary",
        "task.step.lifecycle.state",
        "task.step.lifecycle.summary",
    ]
    assert blocked_trace_events[0]["payload"] == second_execute_payload["request"]
    assert blocked_trace_events[1]["payload"]["task_step_id"] == second_execute_payload["request"]["task_step_id"]
    assert blocked_trace_events[2]["payload"] == second_execute_payload["result"]["budget_decision"]
    assert blocked_trace_events[3]["payload"]["dispatch_status"] == "blocked"
    assert blocked_trace_events[3]["payload"]["task_step_id"] == second_execute_payload["request"]["task_step_id"]
    assert list_status == 200
    assert list_payload["items"][1]["task_step_id"] == second_execute_payload["request"]["task_step_id"]
    assert [item["status"] for item in list_payload["items"]] == ["completed", "blocked"]
    assert list_payload["items"][1]["result"] == second_execute_payload["result"]
    assert detail_status == 200
    assert detail_payload == {"execution": list_payload["items"][1]}


def test_execute_approved_proxy_endpoint_allows_when_recent_history_is_within_rolling_window_limit(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=2,
        rolling_window_seconds=3600,
    )

    first_create_status, first_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_create_status, second_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_create_status == 200
    assert second_create_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_execute_status == 200
    assert second_execute_status == 200
    assert second_execute_payload["result"]["status"] == "completed"
    assert second_execute_payload["events"] is not None

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        execute_trace_events = store.list_trace_events(UUID(second_execute_payload["trace"]["trace_id"]))

    assert execute_trace_events[2]["payload"]["matched_budget_id"] is not None
    assert execute_trace_events[2]["payload"]["rolling_window_seconds"] == 3600
    assert execute_trace_events[2]["payload"]["count_scope"] == "rolling_window"
    assert execute_trace_events[2]["payload"]["window_started_at"] is not None
    assert execute_trace_events[2]["payload"]["completed_execution_count"] == 1
    assert execute_trace_events[2]["payload"]["projected_completed_execution_count"] == 2
    assert execute_trace_events[2]["payload"]["decision"] == "allow"
    assert execute_trace_events[2]["payload"]["reason"] == "within_budget"
    assert execute_trace_events[2]["payload"]["history_order"] == ["executed_at_asc", "id_asc"]


def test_execute_approved_proxy_endpoint_blocks_when_recent_window_history_exceeds_limit(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
        rolling_window_seconds=3600,
    )

    first_create_status, first_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_create_status, second_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_create_status == 200
    assert second_create_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_execute_status == 200
    assert second_execute_status == 200
    assert second_execute_payload["events"] is None
    assert list(second_execute_payload["result"]) == [
        "handler_key",
        "status",
        "output",
        "reason",
        "budget_decision",
    ]
    assert second_execute_payload["result"]["handler_key"] is None
    assert second_execute_payload["result"]["status"] == "blocked"
    assert second_execute_payload["result"]["output"] is None
    assert second_execute_payload["result"]["reason"] == (
        f"execution budget {budget_id} blocks execution: projected completed executions "
        "2 within rolling window 3600 seconds would exceed limit 1"
    )
    assert second_execute_payload["result"]["budget_decision"]["matched_budget_id"] == str(budget_id)
    assert second_execute_payload["result"]["budget_decision"]["rolling_window_seconds"] == 3600
    assert second_execute_payload["result"]["budget_decision"]["count_scope"] == "rolling_window"
    assert second_execute_payload["result"]["budget_decision"]["window_started_at"] is not None
    assert second_execute_payload["result"]["budget_decision"]["completed_execution_count"] == 1
    assert second_execute_payload["result"]["budget_decision"]["projected_completed_execution_count"] == 2
    assert second_execute_payload["result"]["budget_decision"]["history_order"] == [
        "executed_at_asc",
        "id_asc",
    ]

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        stored_executions = store.list_tool_executions()
        blocked_trace_events = store.list_trace_events(UUID(second_execute_payload["trace"]["trace_id"]))

    assert [row["status"] for row in stored_executions] == ["completed", "blocked"]
    assert stored_executions[1]["task_step_id"] == UUID(second_execute_payload["request"]["task_step_id"])
    assert stored_executions[1]["result"] == second_execute_payload["result"]
    assert blocked_trace_events[2]["payload"] == second_execute_payload["result"]["budget_decision"]


def test_execute_approved_proxy_endpoint_excludes_old_window_history_and_keeps_counts_user_scoped(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    other_user = seed_user(migrated_database_urls["app"], email="other@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    owner_tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    other_tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=other_user["user_id"],
        tool_key="proxy.echo",
    )
    budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
        rolling_window_seconds=60,
    )

    owner_first_status, owner_first_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=owner_tool_id,
    )
    owner_second_status, owner_second_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=owner_tool_id,
    )
    other_status, other_payload = create_pending_approval(
        user_id=other_user["user_id"],
        thread_id=other_user["thread_id"],
        tool_id=other_tool_id,
    )
    assert owner_first_status == 200
    assert owner_second_status == 200
    assert other_status == 200

    for approval_payload, user_id in (
        (owner_first_payload, owner["user_id"]),
        (owner_second_payload, owner["user_id"]),
        (other_payload, other_user["user_id"]),
    ):
        approve_status, _ = invoke_request(
            "POST",
            f"/v0/approvals/{approval_payload['approval']['id']}/approve",
            payload={"user_id": str(user_id)},
        )
        assert approve_status == 200

    owner_first_execute_status, owner_first_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{owner_first_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    other_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{other_payload['approval']['id']}/execute",
        payload={"user_id": str(other_user["user_id"])},
    )
    assert owner_first_execute_status == 200
    assert other_execute_status == 200

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        owner_first_execution_id = store.list_tool_executions()[0]["id"]

    set_execution_executed_at(
        migrated_database_urls["admin"],
        user_id=owner["user_id"],
        execution_id=owner_first_execution_id,
        executed_at_sql="clock_timestamp() - interval '2 hours'",
    )

    owner_second_execute_status, owner_second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{owner_second_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert owner_second_execute_status == 200
    assert owner_second_execute_payload["result"]["status"] == "completed"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        execute_trace_events = store.list_trace_events(UUID(owner_second_execute_payload["trace"]["trace_id"]))

    assert execute_trace_events[2]["payload"]["matched_budget_id"] == str(budget_id)
    assert execute_trace_events[2]["payload"]["rolling_window_seconds"] == 60
    assert execute_trace_events[2]["payload"]["count_scope"] == "rolling_window"
    assert execute_trace_events[2]["payload"]["window_started_at"] is not None
    assert execute_trace_events[2]["payload"]["completed_execution_count"] == 0
    assert execute_trace_events[2]["payload"]["projected_completed_execution_count"] == 1
    assert execute_trace_events[2]["payload"]["reason"] == "within_budget"


def test_execute_approved_proxy_endpoint_ignores_deactivated_budget(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    first_create_status, first_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_create_status, second_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_create_status == 200
    assert second_create_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    deactivate_status, deactivate_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{budget_id}/deactivate",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
        },
    )
    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_execute_status == 200
    assert deactivate_status == 200
    assert deactivate_payload["execution_budget"]["status"] == "inactive"
    assert second_execute_status == 200
    assert second_execute_payload["result"]["status"] == "completed"
    assert second_execute_payload["trace"]["trace_event_count"] == 9

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        execute_trace_events = store.list_trace_events(UUID(second_execute_payload["trace"]["trace_id"]))

    assert execute_trace_events[2]["payload"] == {
        "matched_budget_id": None,
        "tool_key": "proxy.echo",
        "domain_hint": None,
        "budget_tool_key": None,
        "budget_domain_hint": None,
        "max_completed_executions": None,
        "rolling_window_seconds": None,
        "count_scope": "lifetime",
        "window_started_at": None,
        "completed_execution_count": 0,
        "projected_completed_execution_count": 1,
        "decision": "allow",
        "reason": "no_matching_budget",
        "order": ["specificity_desc", "created_at_asc", "id_asc"],
        "history_order": ["executed_at_asc", "id_asc"],
    }


def test_execute_approved_proxy_endpoint_uses_replacement_budget_after_supersession(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    first_create_status, first_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_create_status, second_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_create_status == 200
    assert second_create_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    supersede_status, supersede_payload = invoke_request(
        "POST",
        f"/v0/execution-budgets/{budget_id}/supersede",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "max_completed_executions": 2,
        },
    )
    second_execute_status, second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{second_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_execute_status == 200
    assert supersede_status == 200
    assert supersede_payload["superseded_budget"]["status"] == "superseded"
    assert supersede_payload["replacement_budget"]["status"] == "active"
    assert second_execute_status == 200
    assert second_execute_payload["result"]["status"] == "completed"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        execute_trace_events = store.list_trace_events(UUID(second_execute_payload["trace"]["trace_id"]))

    assert execute_trace_events[2]["payload"] == {
        "matched_budget_id": supersede_payload["replacement_budget"]["id"],
        "tool_key": "proxy.echo",
        "domain_hint": None,
        "budget_tool_key": "proxy.echo",
        "budget_domain_hint": None,
        "max_completed_executions": 2,
        "rolling_window_seconds": None,
        "count_scope": "lifetime",
        "window_started_at": None,
        "completed_execution_count": 1,
        "projected_completed_execution_count": 2,
        "decision": "allow",
        "reason": "within_budget",
        "order": ["specificity_desc", "created_at_asc", "id_asc"],
        "history_order": ["executed_at_asc", "id_asc"],
    }


def test_execute_approved_proxy_endpoint_applies_profile_scope_before_global_fallback(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    coach_thread_id = create_thread(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        title="Coach profile thread",
        agent_profile_id="coach_default",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    assistant_budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        agent_profile_id="assistant_default",
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )
    global_budget_id = create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        agent_profile_id=None,
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    assistant_first_status, assistant_first_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assistant_second_status, assistant_second_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    coach_first_status, coach_first_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=coach_thread_id,
        tool_id=tool_id,
    )
    coach_second_status, coach_second_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=coach_thread_id,
        tool_id=tool_id,
    )
    assert assistant_first_status == 200
    assert assistant_second_status == 200
    assert coach_first_status == 200
    assert coach_second_status == 200

    for approval_payload in (
        assistant_first_payload,
        assistant_second_payload,
        coach_first_payload,
        coach_second_payload,
    ):
        approve_status, _ = invoke_request(
            "POST",
            f"/v0/approvals/{approval_payload['approval']['id']}/approve",
            payload={"user_id": str(owner["user_id"])},
        )
        assert approve_status == 200

    assistant_first_execute_status, assistant_first_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{assistant_first_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    coach_first_execute_status, coach_first_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{coach_first_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    coach_second_execute_status, coach_second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{coach_second_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assistant_second_execute_status, assistant_second_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{assistant_second_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )

    assert assistant_first_execute_status == 200
    assert coach_first_execute_status == 200
    assert coach_second_execute_status == 200
    assert assistant_second_execute_status == 200
    assert assistant_first_execute_payload["result"]["status"] == "completed"
    assert coach_first_execute_payload["result"]["status"] == "completed"
    assert coach_second_execute_payload["result"]["status"] == "blocked"
    assert assistant_second_execute_payload["result"]["status"] == "blocked"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        assistant_first_trace_events = store.list_trace_events(
            UUID(assistant_first_execute_payload["trace"]["trace_id"])
        )
        coach_first_trace_events = store.list_trace_events(UUID(coach_first_execute_payload["trace"]["trace_id"]))
        coach_second_trace_events = store.list_trace_events(UUID(coach_second_execute_payload["trace"]["trace_id"]))
        assistant_second_trace_events = store.list_trace_events(
            UUID(assistant_second_execute_payload["trace"]["trace_id"])
        )

    assert assistant_first_trace_events[2]["payload"]["matched_budget_id"] == str(assistant_budget_id)
    assert assistant_first_trace_events[2]["payload"]["completed_execution_count"] == 0
    assert assistant_first_trace_events[2]["payload"]["projected_completed_execution_count"] == 1
    assert assistant_first_trace_events[2]["payload"]["reason"] == "within_budget"

    assert coach_first_trace_events[2]["payload"]["matched_budget_id"] == str(global_budget_id)
    assert coach_first_trace_events[2]["payload"]["completed_execution_count"] == 0
    assert coach_first_trace_events[2]["payload"]["projected_completed_execution_count"] == 1
    assert coach_first_trace_events[2]["payload"]["reason"] == "within_budget"

    assert coach_second_trace_events[2]["payload"]["matched_budget_id"] == str(global_budget_id)
    assert coach_second_trace_events[2]["payload"]["completed_execution_count"] == 1
    assert coach_second_trace_events[2]["payload"]["projected_completed_execution_count"] == 2
    assert coach_second_trace_events[2]["payload"]["reason"] == "budget_exceeded"

    assert assistant_second_trace_events[2]["payload"]["matched_budget_id"] == str(assistant_budget_id)
    assert assistant_second_trace_events[2]["payload"]["completed_execution_count"] == 1
    assert assistant_second_trace_events[2]["payload"]["projected_completed_execution_count"] == 2
    assert assistant_second_trace_events[2]["payload"]["reason"] == "budget_exceeded"


def test_execute_approved_proxy_execution_budget_is_user_scoped(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    other_user = seed_user(migrated_database_urls["app"], email="other@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    owner_tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )
    other_tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=other_user["user_id"],
        tool_key="proxy.echo",
    )
    create_execution_budget(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
        domain_hint=None,
        max_completed_executions=1,
    )

    owner_create_status, owner_create_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=owner_tool_id,
    )
    other_create_status, other_create_payload = create_pending_approval(
        user_id=other_user["user_id"],
        thread_id=other_user["thread_id"],
        tool_id=other_tool_id,
    )
    assert owner_create_status == 200
    assert other_create_status == 200

    owner_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{owner_create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    other_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{other_create_payload['approval']['id']}/approve",
        payload={"user_id": str(other_user["user_id"])},
    )
    assert owner_approve_status == 200
    assert other_approve_status == 200

    owner_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{owner_create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    other_execute_status, other_execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{other_create_payload['approval']['id']}/execute",
        payload={"user_id": str(other_user["user_id"])},
    )

    assert owner_execute_status == 200
    assert other_execute_status == 200
    assert other_execute_payload["result"]["status"] == "completed"


def test_tool_execution_review_endpoints_are_deterministic_and_user_scoped(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )
    tool_id = create_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        tool_key="proxy.echo",
    )

    first_status, first_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    second_status, second_payload = create_pending_approval(
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
        tool_id=tool_id,
    )
    assert first_status == 200
    assert second_status == 200

    first_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    second_approve_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_approve_status == 200
    assert second_approve_status == 200

    first_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{first_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    second_execute_status, _ = invoke_request(
        "POST",
        f"/v0/approvals/{second_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert first_execute_status == 200
    assert second_execute_status == 200

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        stored_executions = store.list_tool_executions()

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tool-executions",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tool-executions/{stored_executions[1]['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/tool-executions",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        str(stored_executions[0]["id"]),
        str(stored_executions[1]["id"]),
    ]
    assert [item["task_step_id"] for item in list_payload["items"]] == [
        str(stored_executions[0]["task_step_id"]),
        str(stored_executions[1]["task_step_id"]),
    ]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["executed_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {
        "execution": next(item for item in list_payload["items"] if item["id"] == str(stored_executions[1]["id"]))
    }
    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["executed_at_asc", "id_asc"]},
    }

    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/tool-executions/{stored_executions[0]['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
