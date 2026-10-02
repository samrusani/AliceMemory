from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.contracts import MemoryCandidateInput
from alicebot_api.db import user_connection
from alicebot_api.memory import admit_memory_candidate
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


def seed_user_with_memory(database_url: str) -> dict[str, object]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Open-loop thread")
        session = store.create_session(thread["id"], status="active")
        source_event_id = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Remember to confirm reorder details."},
        )["id"]
        decision = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(source_event_id,),
            ),
        )

    assert decision.memory is not None
    return {
        "user_id": user_id,
        "thread_id": thread["id"],
        "source_event_id": source_event_id,
        "memory_id": UUID(decision.memory["id"]),
    }


def test_open_loop_endpoints_create_list_detail_and_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"])
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/open-loops",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "title": "Confirm order details before submission",
            "due_at": "2026-03-28T09:00:00+00:00",
        },
    )

    assert create_status == 201
    assert create_payload["open_loop"]["status"] == "open"
    assert create_payload["open_loop"]["memory_id"] == str(seeded["memory_id"])

    open_loop_id = create_payload["open_loop"]["id"]
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/open-loops",
        query_params={"user_id": str(seeded["user_id"]), "status": "open", "limit": "10"},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/open-loops/{open_loop_id}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [open_loop_id]
    assert list_payload["summary"]["order"] == ["opened_at_desc", "created_at_desc", "id_desc"]
    assert detail_status == 200
    assert detail_payload["open_loop"]["id"] == open_loop_id

    intruder_id = uuid4()
    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        ContinuityStore(conn).create_user(intruder_id, "intruder@example.com", "Intruder")

    intruder_status, intruder_payload = invoke_request(
        "GET",
        "/v0/open-loops",
        query_params={"user_id": str(intruder_id), "status": "open", "limit": "10"},
    )
    assert intruder_status == 200
    assert intruder_payload["items"] == []
    assert intruder_payload["summary"]["total_count"] == 0

    intruder_detail_status, intruder_detail_payload = invoke_request(
        "GET",
        f"/v0/open-loops/{open_loop_id}",
        query_params={"user_id": str(intruder_id)},
    )
    assert intruder_detail_status == 404
    assert intruder_detail_payload["detail"] == {
        "code": "not_found",
        "message": "The requested resource was not found",
    }

    intruder_mutation_status, intruder_mutation_payload = invoke_request(
        "POST",
        f"/v0/open-loops/{open_loop_id}/status",
        payload={
            "user_id": str(intruder_id),
            "status": "resolved",
            "resolution_note": "Unauthorized user should not mutate this row.",
        },
    )
    assert intruder_mutation_status == 404
    assert intruder_mutation_payload["detail"] == {
        "code": "not_found",
        "message": "The requested resource was not found",
    }


def test_open_loop_status_endpoint_rejects_invalid_values_and_persists_audit_fields(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"])
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/open-loops",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "title": "Confirm order details before submission",
        },
    )
    assert create_status == 201
    open_loop_id = create_payload["open_loop"]["id"]

    invalid_status, invalid_payload = invoke_request(
        "POST",
        f"/v0/open-loops/{open_loop_id}/status",
        payload={
            "user_id": str(seeded["user_id"]),
            "status": "pending_review",
        },
    )
    assert invalid_status == 400
    assert invalid_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    resolve_status, resolve_payload = invoke_request(
        "POST",
        f"/v0/open-loops/{open_loop_id}/status",
        payload={
            "user_id": str(seeded["user_id"]),
            "status": "resolved",
            "resolution_note": "Resolved after checking the latest cart.",
        },
    )
    assert resolve_status == 200
    assert resolve_payload["open_loop"]["status"] == "resolved"
    assert resolve_payload["open_loop"]["resolved_at"] is not None
    assert resolve_payload["open_loop"]["resolution_note"] == "Resolved after checking the latest cart."

    repeat_status, repeat_payload = invoke_request(
        "POST",
        f"/v0/open-loops/{open_loop_id}/status",
        payload={
            "user_id": str(seeded["user_id"]),
            "status": "dismissed",
        },
    )
    assert repeat_status == 400
    assert repeat_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_open_loop_status_endpoint_supports_open_to_dismissed_with_audit_fields(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"])
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/open-loops",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "title": "Dismiss this candidate after confirming no action is needed",
        },
    )
    assert create_status == 201
    open_loop_id = create_payload["open_loop"]["id"]

    dismiss_status, dismiss_payload = invoke_request(
        "POST",
        f"/v0/open-loops/{open_loop_id}/status",
        payload={
            "user_id": str(seeded["user_id"]),
            "status": "dismissed",
            "resolution_note": "No follow-up required after manual verification.",
        },
    )

    assert dismiss_status == 200
    assert dismiss_payload["open_loop"]["status"] == "dismissed"
    assert dismiss_payload["open_loop"]["resolved_at"] is not None
    assert dismiss_payload["open_loop"]["resolution_note"] == "No follow-up required after manual verification."


def test_memory_admission_can_create_open_loop_when_requested(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"])
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

    admit_status, admit_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_key": "user.preference.delivery.window",
            "value": {"window": "weekday_morning"},
            "source_event_ids": [str(seeded["source_event_id"])],
            "open_loop": {
                "title": "Reconfirm delivery window before next order",
                "due_at": "2026-03-29T09:00:00+00:00",
            },
        },
    )

    assert admit_status == 200
    assert admit_payload["decision"] == "ADD"
    assert admit_payload["open_loop"]["title"] == "Reconfirm delivery window before next order"
    assert admit_payload["open_loop"]["status"] == "open"
    assert admit_payload["open_loop"]["memory_id"] == admit_payload["memory"]["id"]

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/open-loops",
        query_params={"user_id": str(seeded["user_id"]), "status": "open", "limit": "10"},
    )
    assert list_status == 200
    assert any(item["title"] == "Reconfirm delivery window before next order" for item in list_payload["items"])


def test_context_compile_includes_bounded_open_loop_slice_when_present(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"])
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

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_open_loop(
            memory_id=seeded["memory_id"],
            title="Older open loop",
            status="open",
            opened_at=datetime(2026, 3, 23, 8, 0, tzinfo=UTC),
            due_at=None,
            resolved_at=None,
            resolution_note=None,
        )
        store.create_open_loop(
            memory_id=seeded["memory_id"],
            title="Newer open loop",
            status="open",
            opened_at=datetime(2026, 3, 23, 9, 0, tzinfo=UTC),
            due_at=None,
            resolved_at=None,
            resolution_note=None,
        )

    compile_status, compile_payload = invoke_request(
        "POST",
        "/v0/context/compile",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "max_sessions": 3,
            "max_events": 8,
            "max_memories": 1,
            "max_entities": 5,
            "max_entity_edges": 10,
        },
    )

    assert compile_status == 200
    assert compile_payload["context_pack"]["open_loops"] == [
        {
            "id": compile_payload["context_pack"]["open_loops"][0]["id"],
            "memory_id": str(seeded["memory_id"]),
            "title": "Newer open loop",
            "status": "open",
            "opened_at": "2026-03-23T09:00:00+00:00",
            "due_at": None,
            "resolved_at": None,
            "resolution_note": None,
            "created_at": compile_payload["context_pack"]["open_loops"][0]["created_at"],
            "updated_at": compile_payload["context_pack"]["open_loops"][0]["updated_at"],
        }
    ]
    assert compile_payload["context_pack"]["open_loop_summary"] == {
        "candidate_count": 2,
        "included_count": 1,
        "excluded_limit_count": 1,
        "order": ["opened_at_desc", "created_at_desc", "id_desc"],
    }
