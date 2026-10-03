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
        thread = store.create_thread("Task run thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def seed_task(database_url: str, *, user_id: UUID, thread_id: UUID) -> UUID:
    with user_connection(database_url, user_id) as conn:
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
        task = store.create_task(
            thread_id=thread_id,
            tool_id=tool["id"],
            status="approved",
            request={
                "thread_id": str(thread_id),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {},
            },
            tool={
                "id": str(tool["id"]),
                "tool_key": tool["tool_key"],
                "name": tool["name"],
                "description": tool["description"],
                "version": tool["version"],
                "metadata_version": tool["metadata_version"],
                "active": tool["active"],
                "tags": tool["tags"],
                "action_hints": tool["action_hints"],
                "scope_hints": tool["scope_hints"],
                "domain_hints": tool["domain_hints"],
                "risk_hints": tool["risk_hints"],
                "metadata": tool["metadata"],
                "created_at": tool["created_at"].isoformat(),
            },
            latest_approval_id=None,
            latest_execution_id=None,
        )
    return task["id"]


def test_task_run_endpoints_create_list_get_tick_and_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    task_id = seed_task(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    create_status, create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{task_id}/runs",
        payload={
            "user_id": str(owner["user_id"]),
            "max_ticks": 3,
            "checkpoint": {
                "cursor": 0,
                "target_steps": 2,
                "wait_for_signal": False,
            },
        },
    )

    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{task_id}/runs",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/task-runs/{create_payload['task_run']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    tick_status, tick_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{create_payload['task_run']['id']}/tick",
        payload={"user_id": str(owner["user_id"])},
    )

    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        f"/v0/tasks/{task_id}/runs",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/task-runs/{create_payload['task_run']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert create_status == 201
    assert create_payload["task_run"]["task_id"] == str(task_id)
    assert create_payload["task_run"]["status"] == "queued"
    assert create_payload["task_run"]["tick_count"] == 0
    assert create_payload["task_run"]["step_count"] == 0
    assert create_payload["task_run"]["stop_reason"] is None
    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [create_payload["task_run"]["id"]]
    assert list_payload["summary"] == {
        "task_id": str(task_id),
        "total_count": 1,
        "order": ["created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"task_run": create_payload["task_run"]}
    assert tick_status == 200
    assert tick_payload["previous_status"] == "queued"
    assert tick_payload["task_run"]["status"] == "running"
    assert tick_payload["task_run"]["checkpoint"]["cursor"] == 1
    assert tick_payload["task_run"]["tick_count"] == 1
    assert tick_payload["task_run"]["step_count"] == 1

    assert isolated_list_status == 404
    assert isolated_list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_run_endpoints_cover_budget_wait_resume_pause_cancel_and_conflicts(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-lifecycle@example.com")
    task_id = seed_task(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    budget_create_status, budget_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{task_id}/runs",
        payload={
            "user_id": str(owner["user_id"]),
            "max_ticks": 1,
            "checkpoint": {
                "cursor": 0,
                "target_steps": 3,
                "wait_for_signal": False,
            },
        },
    )
    assert budget_create_status == 201
    budget_run_id = budget_create_payload["task_run"]["id"]

    first_tick_status, first_tick_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{budget_run_id}/tick",
        payload={"user_id": str(owner["user_id"])},
    )
    second_tick_status, second_tick_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{budget_run_id}/tick",
        payload={"user_id": str(owner["user_id"])},
    )
    budget_resume_status, budget_resume_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{budget_run_id}/resume",
        payload={"user_id": str(owner["user_id"])},
    )

    assert first_tick_status == 200
    assert first_tick_payload["task_run"]["status"] == "running"
    assert second_tick_status == 200
    assert second_tick_payload["task_run"]["status"] == "failed"
    assert second_tick_payload["task_run"]["stop_reason"] == "budget_exhausted"
    assert second_tick_payload["task_run"]["failure_class"] == "budget"
    assert second_tick_payload["task_run"]["retry_posture"] == "terminal"
    assert budget_resume_status == 409
    assert budget_resume_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    wait_create_status, wait_create_payload = invoke_request(
        "POST",
        f"/v0/tasks/{task_id}/runs",
        payload={
            "user_id": str(owner["user_id"]),
            "max_ticks": 3,
            "checkpoint": {
                "cursor": 0,
                "target_steps": 2,
                "wait_for_signal": True,
            },
        },
    )
    assert wait_create_status == 201
    wait_run_id = wait_create_payload["task_run"]["id"]

    wait_tick_status, wait_tick_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{wait_run_id}/tick",
        payload={"user_id": str(owner["user_id"])},
    )
    wait_resume_status, wait_resume_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{wait_run_id}/resume",
        payload={"user_id": str(owner["user_id"])},
    )
    wait_pause_status, wait_pause_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{wait_run_id}/pause",
        payload={"user_id": str(owner["user_id"])},
    )
    wait_cancel_status, wait_cancel_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{wait_run_id}/cancel",
        payload={"user_id": str(owner["user_id"])},
    )
    wait_resume_conflict_status, wait_resume_conflict_payload = invoke_request(
        "POST",
        f"/v0/task-runs/{wait_run_id}/resume",
        payload={"user_id": str(owner["user_id"])},
    )

    assert wait_tick_status == 200
    assert wait_tick_payload["task_run"]["status"] == "waiting_user"
    assert wait_tick_payload["task_run"]["stop_reason"] == "waiting_user"
    assert wait_resume_status == 200
    assert wait_resume_payload["task_run"]["status"] == "running"
    assert wait_resume_payload["task_run"]["checkpoint"]["wait_for_signal"] is False
    assert wait_pause_status == 200
    assert wait_pause_payload["task_run"]["status"] == "paused"
    assert wait_pause_payload["task_run"]["stop_reason"] == "paused"
    assert wait_cancel_status == 200
    assert wait_cancel_payload["task_run"]["status"] == "cancelled"
    assert wait_cancel_payload["task_run"]["stop_reason"] == "cancelled"
    assert wait_resume_conflict_status == 409
    assert wait_resume_conflict_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }


def test_task_run_create_endpoint_rejects_invalid_checkpoint(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner-invalid@example.com")
    task_id = seed_task(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    status_code, payload = invoke_request(
        "POST",
        f"/v0/tasks/{task_id}/runs",
        payload={
            "user_id": str(owner["user_id"]),
            "max_ticks": 1,
            "checkpoint": {
                "cursor": "zero",
                "target_steps": 1,
                "wait_for_signal": False,
            },
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
