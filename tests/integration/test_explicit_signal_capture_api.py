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
from alicebot_api.explicit_commitments import _build_memory_key as build_commitment_memory_key
from alicebot_api.explicit_preferences import _build_memory_key as build_preference_memory_key
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


def seed_explicit_signal_capture_events(database_url: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Unified explicit signal capture")
        session = store.create_session(thread["id"], status="active")
        preference_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I like black coffee."},
        )["id"]
        commitment_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Remind me to submit tax forms."},
        )["id"]
        assistant_event = store.append_event(
            thread["id"],
            session["id"],
            "message.assistant",
            {"text": "I like black coffee."},
        )["id"]

    return {
        "user_id": user_id,
        "preference_event_id": preference_event,
        "commitment_event_id": commitment_event,
        "assistant_event_id": assistant_event,
    }


def test_capture_explicit_signals_endpoint_returns_unified_sections_and_aggregate_summary(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_signal_capture_events(migrated_database_urls["app"])
    preference_memory_key = build_preference_memory_key("black coffee")
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

    status_code, payload = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["preference_event_id"]),
        },
    )

    assert status_code == 200
    assert payload["preferences"]["candidates"] == [
        {
            "memory_key": preference_memory_key,
            "value": {
                "kind": "explicit_preference",
                "preference": "like",
                "text": "black coffee",
            },
            "source_event_ids": [str(seeded["preference_event_id"])],
            "delete_requested": False,
            "pattern": "i_like",
            "subject_text": "black coffee",
        }
    ]
    assert payload["preferences"]["admissions"][0]["decision"] == "ADD"
    assert payload["preferences"]["summary"] == {
        "source_event_id": str(seeded["preference_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
    }
    assert payload["commitments"] == {
        "candidates": [],
        "admissions": [],
        "summary": {
            "source_event_id": str(seeded["preference_event_id"]),
            "source_event_kind": "message.user",
            "candidate_count": 0,
            "admission_count": 0,
            "persisted_change_count": 0,
            "noop_count": 0,
            "open_loop_created_count": 0,
            "open_loop_noop_count": 0,
        },
    }
    assert payload["summary"] == {
        "source_event_id": str(seeded["preference_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
        "open_loop_created_count": 0,
        "open_loop_noop_count": 0,
        "preference_candidate_count": 1,
        "preference_admission_count": 1,
        "commitment_candidate_count": 0,
        "commitment_admission_count": 0,
    }


def test_capture_explicit_signals_endpoint_remains_idempotent_for_open_loop_creation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_signal_capture_events(migrated_database_urls["app"])
    commitment_memory_key = build_commitment_memory_key("submit tax forms")
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

    first_status, first_payload = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["commitment_event_id"]),
        },
    )
    repeat_status, repeat_payload = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["commitment_event_id"]),
        },
    )

    assert first_status == 200
    assert first_payload["commitments"]["candidates"] == [
        {
            "memory_key": commitment_memory_key,
            "value": {
                "kind": "explicit_commitment",
                "text": "submit tax forms",
            },
            "source_event_ids": [str(seeded["commitment_event_id"])],
            "delete_requested": False,
            "pattern": "remind_me_to",
            "commitment_text": "submit tax forms",
            "open_loop_title": "Remember to submit tax forms",
        }
    ]
    assert first_payload["commitments"]["admissions"][0]["decision"] == "ADD"
    assert first_payload["commitments"]["admissions"][0]["open_loop"]["decision"] == "CREATED"
    assert first_payload["summary"] == {
        "source_event_id": str(seeded["commitment_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
        "open_loop_created_count": 1,
        "open_loop_noop_count": 0,
        "preference_candidate_count": 0,
        "preference_admission_count": 0,
        "commitment_candidate_count": 1,
        "commitment_admission_count": 1,
    }

    assert repeat_status == 200
    assert repeat_payload["commitments"]["admissions"][0]["decision"] == "NOOP"
    assert repeat_payload["commitments"]["admissions"][0]["open_loop"]["decision"] == "NOOP_ACTIVE_EXISTS"
    assert repeat_payload["summary"] == {
        "source_event_id": str(seeded["commitment_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 0,
        "noop_count": 1,
        "open_loop_created_count": 0,
        "open_loop_noop_count": 1,
        "preference_candidate_count": 0,
        "preference_admission_count": 0,
        "commitment_candidate_count": 1,
        "commitment_admission_count": 1,
    }

    open_loops_status, open_loops_payload = invoke_request(
        "GET",
        "/v0/open-loops",
        query_params={
            "user_id": str(seeded["user_id"]),
            "status": "open",
            "limit": "20",
        },
    )
    assert open_loops_status == 200
    assert len(open_loops_payload["items"]) == 1


def test_capture_explicit_signals_endpoint_rejects_invalid_source_event_and_user_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_signal_capture_events(migrated_database_urls["app"])
    intruder_id = uuid4()
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

    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        ContinuityStore(conn).create_user(intruder_id, "intruder@example.com", "Intruder")

    assistant_status, assistant_payload = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["assistant_event_id"]),
        },
    )
    intruder_status, intruder_payload = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={
            "user_id": str(intruder_id),
            "source_event_id": str(seeded["commitment_event_id"]),
        },
    )

    assert assistant_status == 400
    assert assistant_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert intruder_status == 400
    assert intruder_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        store = ContinuityStore(conn)
        assert store.list_memories() == []
        assert store.list_open_loops(status="open") == []
