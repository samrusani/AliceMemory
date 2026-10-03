from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
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


def create_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def seed_user_with_traces(database_url: str, *, email: str) -> dict[str, object]:
    user_id = create_user(database_url, email=email)

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.create_thread("Trace review thread")
        first_trace = store.create_trace(
            user_id=user_id,
            thread_id=thread["id"],
            kind="context.compile",
            compiler_version="continuity_v0",
            status="completed",
            limits={"max_sessions": 3, "max_events": 8},
        )
        second_trace = store.create_trace(
            user_id=user_id,
            thread_id=thread["id"],
            kind="tool.proxy.execute",
            compiler_version="response_generation_v0",
            status="completed",
            limits={"max_sessions": 1, "max_events": 2},
        )
        first_trace_event = store.append_trace_event(
            trace_id=second_trace["id"],
            sequence_no=2,
            kind="tool.proxy.execute.summary",
            payload={"approval_id": "approval-2"},
        )
        second_trace_event = store.append_trace_event(
            trace_id=second_trace["id"],
            sequence_no=1,
            kind="tool.proxy.execute.request",
            payload={"approval_id": "approval-2"},
        )
        third_trace_event = store.append_trace_event(
            trace_id=first_trace["id"],
            sequence_no=1,
            kind="context.summary",
            payload={"thread_id": str(thread["id"])},
        )

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
        "first_trace": first_trace,
        "second_trace": second_trace,
        "events": {
            "second_trace_second": first_trace_event,
            "second_trace_first": second_trace_event,
            "first_trace_only": third_trace_event,
        },
    }


def serialize_trace_summary(trace: dict[str, Any], *, trace_event_count: int) -> dict[str, Any]:
    return {
        "id": str(trace["id"]),
        "thread_id": str(trace["thread_id"]),
        "kind": trace["kind"],
        "compiler_version": trace["compiler_version"],
        "status": trace["status"],
        "created_at": trace["created_at"].isoformat(),
        "trace_event_count": trace_event_count,
    }


def serialize_trace_detail(trace: dict[str, Any], *, trace_event_count: int) -> dict[str, Any]:
    return {
        **serialize_trace_summary(trace, trace_event_count=trace_event_count),
        "limits": trace["limits"],
    }


def serialize_trace_event(trace_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(trace_event["id"]),
        "trace_id": str(trace_event["trace_id"]),
        "sequence_no": trace_event["sequence_no"],
        "kind": trace_event["kind"],
        "payload": trace_event["payload"],
        "created_at": trace_event["created_at"].isoformat(),
    }


def test_trace_review_endpoints_list_detail_and_events_with_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_traces(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/traces",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/traces/{seeded['second_trace']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )
    events_status, events_payload = invoke_request(
        "GET",
        f"/v0/traces/{seeded['second_trace']['id']}/events",
        query_params={"user_id": str(seeded["user_id"])},
    )

    expected_trace_order = sorted(
        [seeded["first_trace"], seeded["second_trace"]],
        key=lambda trace: (trace["created_at"], trace["id"]),
        reverse=True,
    )

    assert list_status == 200
    assert list_payload == {
        "items": [
            serialize_trace_summary(
                expected_trace_order[0],
                trace_event_count=2 if expected_trace_order[0]["id"] == seeded["second_trace"]["id"] else 1,
            ),
            serialize_trace_summary(
                expected_trace_order[1],
                trace_event_count=2 if expected_trace_order[1]["id"] == seeded["second_trace"]["id"] else 1,
            ),
        ],
        "summary": {
            "total_count": 2,
            "order": ["created_at_desc", "id_desc"],
        },
    }

    assert detail_status == 200
    assert detail_payload == {"trace": serialize_trace_detail(seeded["second_trace"], trace_event_count=2)}

    assert events_status == 200
    assert events_payload == {
        "items": [
            serialize_trace_event(seeded["events"]["second_trace_first"]),
            serialize_trace_event(seeded["events"]["second_trace_second"]),
        ],
        "summary": {
            "trace_id": str(seeded["second_trace"]["id"]),
            "total_count": 2,
            "order": ["sequence_no_asc", "id_asc"],
        },
    }


def test_trace_review_endpoints_enforce_user_isolation_and_not_found(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_traces(migrated_database_urls["app"], email="owner@example.com")
    intruder_id = create_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/traces",
        query_params={"user_id": str(intruder_id)},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/traces/{owner['second_trace']['id']}",
        query_params={"user_id": str(intruder_id)},
    )
    events_status, events_payload = invoke_request(
        "GET",
        f"/v0/traces/{owner['second_trace']['id']}/events",
        query_params={"user_id": str(intruder_id)},
    )

    assert list_status == 200
    assert list_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["created_at_desc", "id_desc"],
        },
    }
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert events_status == 404
    assert events_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
