from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import continuity as continuity_router
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
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start_message["status"], json.loads(body)


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def test_continuity_review_queue_and_confirm_flow(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="reviewer@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep conservative rollout",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep conservative rollout",
            body={"decision_text": "Keep conservative rollout"},
            provenance={"thread_id": "thread-1"},
            confidence=0.94,
        )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    queue_status, queue_payload = invoke_request(
        "GET",
        "/v0/continuity/review-queue",
        query_params={
            "user_id": str(user_id),
            "status": "correction_ready",
            "limit": "20",
        },
    )

    assert queue_status == 200
    assert queue_payload["summary"] == {
        "status": "correction_ready",
        "limit": 20,
        "returned_count": 1,
        "total_count": 1,
        "order": ["updated_at_desc", "created_at_desc", "id_desc"],
    }
    assert queue_payload["items"][0]["id"] == str(continuity_object["id"])
    assert queue_payload["items"][0]["last_confirmed_at"] is None

    confirm_status, confirm_payload = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{continuity_object['id']}/corrections",
        payload={
            "user_id": str(user_id),
            "action": "confirm",
            "reason": "Verified in continuity review",
        },
    )

    assert confirm_status == 200
    assert confirm_payload["continuity_object"]["status"] == "active"
    assert confirm_payload["continuity_object"]["last_confirmed_at"] is not None
    assert confirm_payload["correction_event"]["action"] == "confirm"

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/continuity/review-queue/{continuity_object['id']}",
        query_params={"user_id": str(user_id)},
    )
    assert detail_status == 200
    assert detail_payload["review"]["correction_events"][0]["action"] == "confirm"


def test_continuity_review_supersede_updates_recall_and_resumption_immediately(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="reviewer2@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Decision: Legacy plan",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Legacy plan",
            body={"decision_text": "Legacy plan"},
            provenance={"thread_id": str(thread_id)},
            confidence=0.9,
        )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    supersede_status, supersede_payload = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{continuity_object['id']}/corrections",
        payload={
            "user_id": str(user_id),
            "action": "supersede",
            "reason": "Contradicted by latest decision",
            "replacement_title": "Decision: Updated plan",
            "replacement_body": {"decision_text": "Updated plan"},
            "replacement_provenance": {"thread_id": str(thread_id)},
            "replacement_confidence": 0.97,
        },
    )

    assert supersede_status == 200
    assert supersede_payload["continuity_object"]["status"] == "superseded"
    assert supersede_payload["replacement_object"]["status"] == "active"

    replacement_id = supersede_payload["replacement_object"]["id"]

    recall_status, recall_payload = invoke_request(
        "GET",
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "20",
        },
    )
    assert recall_status == 200
    assert recall_payload["items"][0]["id"] == replacement_id
    assert {item["status"] for item in recall_payload["items"]} == {"active", "superseded"}

    brief_status, brief_payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_recent_changes": "5",
            "max_open_loops": "5",
        },
    )
    assert brief_status == 200
    assert brief_payload["brief"]["last_decision"]["item"]["id"] == replacement_id

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/continuity/review-queue/{continuity_object['id']}",
        query_params={"user_id": str(user_id)},
    )
    assert detail_status == 200
    assert detail_payload["review"]["supersession_chain"]["superseded_by"]["id"] == replacement_id
    assert detail_payload["review"]["correction_events"][0]["action"] == "supersede"


def test_continuity_review_mark_stale_and_delete_posture(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="reviewer3@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        stale_capture = store.create_continuity_capture_event(
            raw_content="Decision: Might be stale",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        stale_object = store.create_continuity_object(
            capture_event_id=stale_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Might be stale",
            body={"decision_text": "Might be stale"},
            provenance={"thread_id": "thread-3"},
            confidence=0.8,
        )

        delete_capture = store.create_continuity_capture_event(
            raw_content="Decision: Remove this",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        delete_object = store.create_continuity_object(
            capture_event_id=delete_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Remove this",
            body={"decision_text": "Remove this"},
            provenance={"thread_id": "thread-3"},
            confidence=0.8,
        )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    stale_status, stale_payload = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{stale_object['id']}/corrections",
        payload={
            "user_id": str(user_id),
            "action": "mark_stale",
        },
    )
    assert stale_status == 200
    assert stale_payload["continuity_object"]["status"] == "stale"

    delete_status, delete_payload = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{delete_object['id']}/corrections",
        payload={
            "user_id": str(user_id),
            "action": "delete",
            "reason": "No longer relevant",
        },
    )
    assert delete_status == 200
    assert delete_payload["continuity_object"]["status"] == "deleted"

    stale_queue_status, stale_queue_payload = invoke_request(
        "GET",
        "/v0/continuity/review-queue",
        query_params={
            "user_id": str(user_id),
            "status": "stale",
            "limit": "20",
        },
    )
    assert stale_queue_status == 200
    assert [item["id"] for item in stale_queue_payload["items"]] == [str(stale_object["id"])]

    recall_status, recall_payload = invoke_request(
        "GET",
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "limit": "20",
        },
    )
    assert recall_status == 200
    assert all(item["id"] != str(delete_object["id"]) for item in recall_payload["items"])
