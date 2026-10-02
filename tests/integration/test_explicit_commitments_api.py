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
from alicebot_api.explicit_commitments import _build_memory_key
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


def seed_explicit_commitment_events(database_url: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Explicit commitment extraction")
        session = store.create_session(thread["id"], status="active")
        commitment_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Remind me to submit tax forms."},
        )["id"]
        unsupported_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I had coffee yesterday."},
        )["id"]
        clause_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Remember to if we can reschedule."},
        )["id"]
        assistant_event = store.append_event(
            thread["id"],
            session["id"],
            "message.assistant",
            {"text": "Remind me to submit tax forms."},
        )["id"]

    return {
        "user_id": user_id,
        "commitment_event_id": commitment_event,
        "unsupported_event_id": unsupported_event,
        "clause_event_id": clause_event,
        "assistant_event_id": assistant_event,
    }


def test_extract_explicit_commitments_endpoint_persists_memory_open_loop_and_remains_idempotent_on_repeat(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_commitment_events(migrated_database_urls["app"])
    memory_key = _build_memory_key("submit tax forms")
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
        "/v0/open-loops/extract-explicit-commitments",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["commitment_event_id"]),
        },
    )
    repeat_status, repeat_payload = invoke_request(
        "POST",
        "/v0/open-loops/extract-explicit-commitments",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["commitment_event_id"]),
        },
    )

    assert first_status == 200
    assert first_payload["candidates"] == [
        {
            "memory_key": memory_key,
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
    assert first_payload["admissions"][0]["decision"] == "ADD"
    assert first_payload["admissions"][0]["open_loop"]["decision"] == "CREATED"
    assert first_payload["summary"] == {
        "source_event_id": str(seeded["commitment_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
        "open_loop_created_count": 1,
        "open_loop_noop_count": 0,
    }

    assert repeat_status == 200
    assert repeat_payload["admissions"][0]["decision"] == "NOOP"
    assert repeat_payload["admissions"][0]["open_loop"]["decision"] == "NOOP_ACTIVE_EXISTS"
    assert repeat_payload["summary"] == {
        "source_event_id": str(seeded["commitment_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 0,
        "noop_count": 1,
        "open_loop_created_count": 0,
        "open_loop_noop_count": 1,
    }

    memories_status, memories_payload = invoke_request(
        "GET",
        "/v0/memories",
        query_params={
            "user_id": str(seeded["user_id"]),
            "status": "active",
            "limit": "20",
        },
    )
    open_loops_status, open_loops_payload = invoke_request(
        "GET",
        "/v0/open-loops",
        query_params={
            "user_id": str(seeded["user_id"]),
            "status": "open",
            "limit": "20",
        },
    )

    assert memories_status == 200
    assert open_loops_status == 200
    assert [item["memory_key"] for item in memories_payload["items"]] == [memory_key]
    assert len(open_loops_payload["items"]) == 1
    assert open_loops_payload["items"][0]["memory_id"] == memories_payload["items"][0]["id"]

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        memories = store.list_memories()
        open_loops = store.list_open_loops(status="open")

    assert [memory["memory_key"] for memory in memories] == [memory_key]
    assert len(open_loops) == 1


def test_extract_explicit_commitments_endpoint_returns_no_candidates_for_unsupported_or_clause_text(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_commitment_events(migrated_database_urls["app"])
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

    unsupported_status, unsupported_payload = invoke_request(
        "POST",
        "/v0/open-loops/extract-explicit-commitments",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["unsupported_event_id"]),
        },
    )
    clause_status, clause_payload = invoke_request(
        "POST",
        "/v0/open-loops/extract-explicit-commitments",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["clause_event_id"]),
        },
    )

    assert unsupported_status == 200
    assert unsupported_payload == {
        "candidates": [],
        "admissions": [],
        "summary": {
            "source_event_id": str(seeded["unsupported_event_id"]),
            "source_event_kind": "message.user",
            "candidate_count": 0,
            "admission_count": 0,
            "persisted_change_count": 0,
            "noop_count": 0,
            "open_loop_created_count": 0,
            "open_loop_noop_count": 0,
        },
    }
    assert clause_status == 200
    assert clause_payload == {
        "candidates": [],
        "admissions": [],
        "summary": {
            "source_event_id": str(seeded["clause_event_id"]),
            "source_event_kind": "message.user",
            "candidate_count": 0,
            "admission_count": 0,
            "persisted_change_count": 0,
            "noop_count": 0,
            "open_loop_created_count": 0,
            "open_loop_noop_count": 0,
        },
    }


def test_extract_explicit_commitments_endpoint_rejects_invalid_source_event_and_user_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_commitment_events(migrated_database_urls["app"])
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
        "/v0/open-loops/extract-explicit-commitments",
        payload={
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["assistant_event_id"]),
        },
    )
    intruder_status, intruder_payload = invoke_request(
        "POST",
        "/v0/open-loops/extract-explicit-commitments",
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
