from __future__ import annotations

import json
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.explicit_preferences import _build_memory_key
from alicebot_api.store import ContinuityStore


def invoke_extract_explicit_preferences(payload: dict[str, str]) -> tuple[int, dict[str, object]]:
    messages: list[dict[str, object]] = []
    encoded_body = json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v0/memories/extract-explicit-preferences",
        "raw_path": b"/v0/memories/extract-explicit-preferences",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_explicit_preference_events(database_url: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Explicit preference extraction")
        session = store.create_session(thread["id"], status="active")
        like_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I like black coffee."},
        )["id"]
        dislike_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I don't like black coffee."},
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
            {"text": "I prefer that we meet tomorrow."},
        )["id"]
        cpp_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I like C++."},
        )["id"]
        csharp_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "I like C#."},
        )["id"]
        assistant_event = store.append_event(
            thread["id"],
            session["id"],
            "message.assistant",
            {"text": "I like black coffee."},
        )["id"]

    return {
        "user_id": user_id,
        "like_event_id": like_event,
        "dislike_event_id": dislike_event,
        "unsupported_event_id": unsupported_event,
        "clause_event_id": clause_event,
        "cpp_event_id": cpp_event,
        "csharp_event_id": csharp_event,
        "assistant_event_id": assistant_event,
    }


def test_extract_explicit_preferences_endpoint_admits_supported_candidates_and_persists_revisions(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_preference_events(migrated_database_urls["app"])
    memory_key = _build_memory_key("black coffee")
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

    add_status, add_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["like_event_id"]),
        }
    )
    update_status, update_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["dislike_event_id"]),
        }
    )

    assert add_status == 200
    assert add_payload["candidates"] == [
        {
            "memory_key": memory_key,
            "value": {
                "kind": "explicit_preference",
                "preference": "like",
                "text": "black coffee",
            },
            "source_event_ids": [str(seeded["like_event_id"])],
            "delete_requested": False,
            "pattern": "i_like",
            "subject_text": "black coffee",
        }
    ]
    assert add_payload["admissions"][0]["decision"] == "ADD"
    assert add_payload["summary"] == {
        "source_event_id": str(seeded["like_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
    }

    assert update_status == 200
    assert update_payload["candidates"] == [
        {
            "memory_key": memory_key,
            "value": {
                "kind": "explicit_preference",
                "preference": "dislike",
                "text": "black coffee",
            },
            "source_event_ids": [str(seeded["dislike_event_id"])],
            "delete_requested": False,
            "pattern": "i_dont_like",
            "subject_text": "black coffee",
        }
    ]
    assert update_payload["admissions"][0]["decision"] == "UPDATE"
    assert update_payload["summary"] == {
        "source_event_id": str(seeded["dislike_event_id"]),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
    }

    memory_id = UUID(str(update_payload["admissions"][0]["memory"]["id"]))
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        memories = store.list_memories()
        revisions = store.list_memory_revisions(memory_id)

    assert len(memories) == 1
    assert memories[0]["id"] == memory_id
    assert memories[0]["memory_key"] == memory_key
    assert memories[0]["value"] == {
        "kind": "explicit_preference",
        "preference": "dislike",
        "text": "black coffee",
    }
    assert [revision["action"] for revision in revisions] == ["ADD", "UPDATE"]
    assert revisions[0]["candidate"] == {
        "memory_key": memory_key,
        "agent_profile_id": "assistant_default",
        "value": {
            "kind": "explicit_preference",
            "preference": "like",
            "text": "black coffee",
        },
        "source_event_ids": [str(seeded["like_event_id"])],
        "delete_requested": False,
    }
    assert revisions[1]["candidate"] == {
        "memory_key": memory_key,
        "agent_profile_id": "assistant_default",
        "value": {
            "kind": "explicit_preference",
            "preference": "dislike",
            "text": "black coffee",
        },
        "source_event_ids": [str(seeded["dislike_event_id"])],
        "delete_requested": False,
    }


def test_extract_explicit_preferences_endpoint_returns_no_candidates_for_unsupported_text(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_preference_events(migrated_database_urls["app"])
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

    status_code, payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["unsupported_event_id"]),
        }
    )

    assert status_code == 200
    assert payload == {
        "candidates": [],
        "admissions": [],
        "summary": {
            "source_event_id": str(seeded["unsupported_event_id"]),
            "source_event_kind": "message.user",
            "candidate_count": 0,
            "admission_count": 0,
            "persisted_change_count": 0,
            "noop_count": 0,
        },
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_memories() == []


def test_extract_explicit_preferences_endpoint_rejects_clause_style_tail(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_preference_events(migrated_database_urls["app"])
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

    status_code, payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["clause_event_id"]),
        }
    )

    assert status_code == 200
    assert payload == {
        "candidates": [],
        "admissions": [],
        "summary": {
            "source_event_id": str(seeded["clause_event_id"]),
            "source_event_kind": "message.user",
            "candidate_count": 0,
            "admission_count": 0,
            "persisted_change_count": 0,
            "noop_count": 0,
        },
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        assert store.list_memories() == []


def test_extract_explicit_preferences_endpoint_keeps_symbol_subjects_in_distinct_memories(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_preference_events(migrated_database_urls["app"])
    cpp_key = _build_memory_key("C++")
    csharp_key = _build_memory_key("C#")
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

    cpp_status, cpp_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["cpp_event_id"]),
        }
    )
    csharp_status, csharp_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["csharp_event_id"]),
        }
    )

    assert cpp_status == 200
    assert cpp_payload["candidates"][0]["memory_key"] == cpp_key
    assert cpp_payload["admissions"][0]["decision"] == "ADD"
    assert csharp_status == 200
    assert csharp_payload["candidates"][0]["memory_key"] == csharp_key
    assert csharp_payload["admissions"][0]["decision"] == "ADD"
    assert cpp_key != csharp_key

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        memories = sorted(store.list_memories(), key=lambda memory: memory["memory_key"])

    assert [memory["memory_key"] for memory in memories] == sorted([cpp_key, csharp_key])
    assert {memory["memory_key"]: memory["value"] for memory in memories} == {
        cpp_key: {
            "kind": "explicit_preference",
            "preference": "like",
            "text": "C++",
        },
        csharp_key: {
            "kind": "explicit_preference",
            "preference": "like",
            "text": "C#",
        },
    }


def test_extract_explicit_preferences_endpoint_validates_source_event_and_user_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_explicit_preference_events(migrated_database_urls["app"])
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

    assistant_status, assistant_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(seeded["user_id"]),
            "source_event_id": str(seeded["assistant_event_id"]),
        }
    )
    intruder_status, intruder_payload = invoke_extract_explicit_preferences(
        {
            "user_id": str(intruder_id),
            "source_event_id": str(seeded["like_event_id"]),
        }
    )

    assert assistant_status == 400
    assert assistant_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert intruder_status == 400
    assert intruder_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        store = ContinuityStore(conn)
        assert store.list_memories() == []
