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


def seed_review_memories(database_url: str) -> dict[str, str]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "reviewer@example.com", "Reviewer")
        thread = store.create_thread("Memory review thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], session["id"], "message.user", {"text": "likes black coffee"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "likes salty snacks"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "reads science fiction"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "enjoys hiking"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "forget the snack preference"})[
                "id"
            ],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "actually likes oat milk"})["id"],
        ]

        coffee = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "black"},
                source_event_ids=(event_ids[0],),
            ),
        )
        snack = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.snack",
                value={"likes": "chips"},
                source_event_ids=(event_ids[1],),
            ),
        )
        book = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.book",
                value={"genre": "science fiction"},
                source_event_ids=(event_ids[2],),
            ),
        )
        hobby = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.hobby",
                value={"likes": "hiking"},
                source_event_ids=(event_ids[3],),
            ),
        )
        admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.snack",
                value=None,
                source_event_ids=(event_ids[4],),
                delete_requested=True,
            ),
        )
        admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(event_ids[5],),
            ),
        )

    return {
        "user_id": str(user_id),
        "coffee_memory_id": coffee.memory["id"],
        "snack_memory_id": snack.memory["id"],
        "book_memory_id": book.memory["id"],
        "hobby_memory_id": hobby.memory["id"],
        "coffee_add_event_id": str(event_ids[0]),
        "coffee_update_event_id": str(event_ids[5]),
        "book_add_event_id": str(event_ids[2]),
        "hobby_add_event_id": str(event_ids[3]),
        "snack_delete_event_id": str(event_ids[4]),
    }


def seed_review_queue_state(database_url: str) -> dict[str, str]:
    seeded = seed_review_memories(database_url)

    with user_connection(database_url, UUID(seeded["user_id"])) as conn:
        store = ContinuityStore(conn)
        store.create_memory_review_label(
            memory_id=UUID(seeded["hobby_memory_id"]),
            label="correct",
            note="Already reviewed.",
        )
        store.create_memory_review_label(
            memory_id=UUID(seeded["snack_memory_id"]),
            label="outdated",
            note="Deleted memory remains part of evaluation counts only.",
        )

    return seeded


def seed_memory_evaluation_state(database_url: str) -> dict[str, str]:
    seeded = seed_review_memories(database_url)

    with user_connection(database_url, UUID(seeded["user_id"])) as conn:
        store = ContinuityStore(conn)
        store.create_memory_review_label(
            memory_id=UUID(seeded["coffee_memory_id"]),
            label="correct",
            note="Matches the latest coffee preference.",
        )
        store.create_memory_review_label(
            memory_id=UUID(seeded["coffee_memory_id"]),
            label="insufficient_evidence",
            note="One source event is still a thin basis.",
        )
        store.create_memory_review_label(
            memory_id=UUID(seeded["snack_memory_id"]),
            label="outdated",
            note="The deleted snack preference is superseded.",
        )

    return seeded


def seed_review_queue_priority_state(database_url: str) -> dict[str, str]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "priority@example.com", "Priority Reviewer")
        thread = store.create_thread("Queue priority thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], session["id"], "message.user", {"text": "oldest"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "middle"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "newest"})["id"],
        ]

        oldest = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.priority.oldest",
                value={"rank": "oldest"},
                source_event_ids=(event_ids[0],),
                confirmation_status="contested",
                confidence=0.9,
                valid_to=datetime(2026, 3, 1, 0, 0, tzinfo=UTC),
            ),
        )
        middle = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.priority.middle",
                value={"rank": "middle"},
                source_event_ids=(event_ids[1],),
                confirmation_status="confirmed",
                confidence=0.95,
            ),
        )
        newest = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.priority.newest",
                value={"rank": "newest"},
                source_event_ids=(event_ids[2],),
                confirmation_status="confirmed",
                confidence=0.2,
            ),
        )

    return {
        "user_id": str(user_id),
        "oldest_memory_id": oldest.memory["id"],
        "middle_memory_id": middle.memory["id"],
        "newest_memory_id": newest.memory["id"],
    }


def test_list_memories_endpoint_returns_filtered_memories_with_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_review_memories(migrated_database_urls["app"])
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
        "GET",
        "/v0/memories",
        query_params={
            "user_id": seeded["user_id"],
            "status": "active",
            "limit": "2",
        },
    )

    assert status_code == 200
    assert [item["id"] for item in payload["items"]] == [
        seeded["coffee_memory_id"],
        seeded["hobby_memory_id"],
    ]
    assert payload["items"][0]["status"] == "active"
    assert payload["items"][0]["value"] == {"likes": "oat milk"}
    assert payload["items"][0]["source_event_ids"] == [seeded["coffee_update_event_id"]]
    assert payload["items"][0]["memory_type"] == "preference"
    assert payload["items"][0]["confirmation_status"] == "unconfirmed"
    assert payload["items"][0]["trust_class"] == "deterministic"
    assert payload["items"][0]["promotion_eligibility"] == "promotable"
    assert payload["items"][0]["evidence_count"] is None
    assert payload["items"][0]["independent_source_count"] is None
    assert payload["items"][0]["extracted_by_model"] is None
    assert payload["items"][0]["trust_reason"] is None
    assert payload["items"][0]["confidence"] is None
    assert payload["items"][0]["salience"] is None
    assert payload["items"][0]["valid_from"] is None
    assert payload["items"][0]["valid_to"] is None
    assert payload["items"][0]["last_confirmed_at"] is None
    assert payload["summary"] == {
        "status": "active",
        "limit": 2,
        "returned_count": 2,
        "total_count": 3,
        "has_more": True,
        "order": ["updated_at_desc", "created_at_desc", "id_desc"],
    }

    deleted_status, deleted_payload = invoke_request(
        "GET",
        "/v0/memories",
        query_params={
            "user_id": seeded["user_id"],
            "status": "deleted",
            "limit": "5",
        },
    )

    assert deleted_status == 200
    assert deleted_payload["items"] == [
        {
            "id": seeded["snack_memory_id"],
            "memory_key": "user.preference.snack",
            "value": {"likes": "chips"},
            "status": "deleted",
            "source_event_ids": [seeded["snack_delete_event_id"]],
            "memory_type": "preference",
            "confidence": None,
            "salience": None,
            "confirmation_status": "unconfirmed",
            "trust_class": "deterministic",
            "promotion_eligibility": "promotable",
            "evidence_count": None,
            "independent_source_count": None,
            "extracted_by_model": None,
            "trust_reason": None,
            "valid_from": None,
            "valid_to": None,
            "last_confirmed_at": None,
            "created_at": deleted_payload["items"][0]["created_at"],
            "updated_at": deleted_payload["items"][0]["updated_at"],
            "deleted_at": deleted_payload["items"][0]["deleted_at"],
        }
    ]
    assert deleted_payload["summary"] == {
        "status": "deleted",
        "limit": 5,
        "returned_count": 1,
        "total_count": 1,
        "has_more": False,
        "order": ["updated_at_desc", "created_at_desc", "id_desc"],
    }


def test_memory_review_endpoints_return_current_memory_and_revision_history(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_review_memories(migrated_database_urls["app"])
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

    memory_status, memory_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['coffee_memory_id']}",
        query_params={"user_id": seeded["user_id"]},
    )
    revisions_status, revisions_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['coffee_memory_id']}/revisions",
        query_params={"user_id": seeded["user_id"], "limit": "5"},
    )

    assert memory_status == 200
    assert memory_payload["memory"]["id"] == seeded["coffee_memory_id"]
    assert memory_payload["memory"]["memory_key"] == "user.preference.coffee"
    assert memory_payload["memory"]["status"] == "active"
    assert memory_payload["memory"]["value"] == {"likes": "oat milk"}
    assert memory_payload["memory"]["source_event_ids"] == [seeded["coffee_update_event_id"]]
    assert memory_payload["memory"]["memory_type"] == "preference"
    assert memory_payload["memory"]["confidence"] is None
    assert memory_payload["memory"]["salience"] is None
    assert memory_payload["memory"]["confirmation_status"] == "unconfirmed"
    assert memory_payload["memory"]["valid_from"] is None
    assert memory_payload["memory"]["valid_to"] is None
    assert memory_payload["memory"]["last_confirmed_at"] is None

    assert revisions_status == 200
    assert [item["sequence_no"] for item in revisions_payload["items"]] == [1, 2]
    assert [item["action"] for item in revisions_payload["items"]] == ["ADD", "UPDATE"]
    assert revisions_payload["items"][0]["new_value"] == {"likes": "black"}
    assert revisions_payload["items"][0]["source_event_ids"] == [seeded["coffee_add_event_id"]]
    assert revisions_payload["items"][1]["previous_value"] == {"likes": "black"}
    assert revisions_payload["items"][1]["new_value"] == {"likes": "oat milk"}
    assert revisions_payload["items"][1]["source_event_ids"] == [seeded["coffee_update_event_id"]]
    assert revisions_payload["summary"] == {
        "memory_id": seeded["coffee_memory_id"],
        "limit": 5,
        "returned_count": 2,
        "total_count": 2,
        "has_more": False,
        "order": ["sequence_no_asc"],
    }


def test_memory_review_endpoints_roundtrip_non_default_typed_metadata(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "typed@example.com", "Typed Reviewer")
        thread = store.create_thread("Typed metadata thread")
        session = store.create_session(thread["id"], status="active")
        source_event_id = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Morning standup prep is my preferred daily routine."},
        )["id"]

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
            "user_id": str(user_id),
            "memory_key": "user.routine.morning_prep",
            "value": {"window": "07:30", "activity": "standup prep"},
            "source_event_ids": [str(source_event_id)],
            "memory_type": "routine",
            "confidence": 0.92,
            "salience": 0.73,
            "confirmation_status": "confirmed",
            "valid_from": "2026-03-01T07:30:00Z",
            "valid_to": "2026-12-31T07:30:00Z",
            "last_confirmed_at": "2026-03-20T09:00:00Z",
        },
    )

    assert admit_status == 200
    assert admit_payload["decision"] == "ADD"
    admitted_memory_id = admit_payload["memory"]["id"]

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/memories",
        query_params={"user_id": str(user_id), "status": "active", "limit": "10"},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/memories/{admitted_memory_id}",
        query_params={"user_id": str(user_id)},
    )

    assert list_status == 200
    assert detail_status == 200
    listed_memory = next(item for item in list_payload["items"] if item["id"] == admitted_memory_id)
    detailed_memory = detail_payload["memory"]

    for payload in (listed_memory, detailed_memory):
        assert payload["memory_type"] == "routine"
        assert payload["confidence"] == 0.92
        assert payload["salience"] == 0.73
        assert payload["confirmation_status"] == "confirmed"
        assert payload["valid_from"].startswith("2026-03-01T07:30:00")
        assert payload["valid_to"].startswith("2026-12-31T07:30:00")
        assert payload["last_confirmed_at"].startswith("2026-03-20T09:00:00")


def test_memory_admit_endpoint_scopes_same_memory_key_by_thread_profile(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "profiled-admit@example.com", "Profiled Admit")
        assistant_thread = store.create_thread("Assistant memory thread")
        coach_thread = store.create_thread("Coach memory thread", agent_profile_id="coach_default")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        coach_session = store.create_session(coach_thread["id"], status="active")
        assistant_event_id = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "assistant profile memory evidence"},
        )["id"]
        coach_event_id = store.append_event(
            coach_thread["id"],
            coach_session["id"],
            "message.user",
            {"text": "coach profile memory evidence"},
        )["id"]

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

    assistant_status, assistant_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(assistant_event_id)],
        },
    )
    coach_add_status, coach_add_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.coffee",
            "value": {"likes": "oat milk"},
            "source_event_ids": [str(coach_event_id)],
        },
    )
    coach_update_status, coach_update_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.coffee",
            "value": {"likes": "cortado"},
            "source_event_ids": [str(coach_event_id)],
        },
    )

    assert assistant_status == 200
    assert assistant_payload["decision"] == "ADD"
    assert coach_add_status == 200
    assert coach_add_payload["decision"] == "ADD"
    assert coach_update_status == 200
    assert coach_update_payload["decision"] == "UPDATE"
    assert assistant_payload["memory"]["id"] != coach_add_payload["memory"]["id"]
    assert coach_update_payload["memory"]["id"] == coach_add_payload["memory"]["id"]

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        assistant_memory = store.get_memory(UUID(assistant_payload["memory"]["id"]))
        coach_memory = store.get_memory(UUID(coach_add_payload["memory"]["id"]))

    assert assistant_memory["agent_profile_id"] == "assistant_default"
    assert assistant_memory["value"] == {"likes": "black"}
    assert coach_memory["agent_profile_id"] == "coach_default"
    assert coach_memory["value"] == {"likes": "cortado"}


def test_memory_admit_endpoint_rejects_mixed_profile_source_events(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "mixed-profile@example.com", "Mixed Profile")
        assistant_thread = store.create_thread("Assistant mixed thread")
        coach_thread = store.create_thread("Coach mixed thread", agent_profile_id="coach_default")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        coach_session = store.create_session(coach_thread["id"], status="active")
        assistant_event_id = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "assistant profile evidence"},
        )["id"]
        coach_event_id = store.append_event(
            coach_thread["id"],
            coach_session["id"],
            "message.user",
            {"text": "coach profile evidence"},
        )["id"]

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
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.invalid",
            "value": {"likes": "espresso"},
            "source_event_ids": [str(assistant_event_id), str(coach_event_id)],
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_memory_admit_endpoint_rejects_explicit_agent_profile_mismatch(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "mismatch-profile@example.com", "Mismatch Profile")
        assistant_thread = store.create_thread("Assistant mismatch thread")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        assistant_event_id = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "assistant profile mismatch evidence"},
        )["id"]

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
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.mismatch",
            "value": {"likes": "espresso"},
            "source_event_ids": [str(assistant_event_id)],
            "agent_profile_id": "coach_default",
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_memory_admit_endpoint_rejects_unknown_agent_profile_id(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "unknown-profile@example.com", "Unknown Profile")
        assistant_thread = store.create_thread("Assistant unknown-profile thread")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        assistant_event_id = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "assistant profile unknown evidence"},
        )["id"]

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
        "/v0/memories/admit",
        payload={
            "user_id": str(user_id),
            "memory_key": "user.preference.profiled.unknown",
            "value": {"likes": "espresso"},
            "source_event_ids": [str(assistant_event_id)],
            "agent_profile_id": "unknown_profile",
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_memory_review_endpoints_enforce_per_user_isolation_and_not_found_behavior(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_review_memories(migrated_database_urls["app"])
    intruder_id = uuid4()
    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        ContinuityStore(conn).create_user(intruder_id, "intruder@example.com", "Intruder")

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
        "/v0/memories",
        query_params={"user_id": str(intruder_id), "status": "all", "limit": "10"},
    )
    memory_status, memory_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['coffee_memory_id']}",
        query_params={"user_id": str(intruder_id)},
    )
    revisions_status, revisions_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['coffee_memory_id']}/revisions",
        query_params={"user_id": str(intruder_id), "limit": "10"},
    )

    assert list_status == 200
    assert list_payload == {
        "items": [],
        "summary": {
            "status": "all",
            "limit": 10,
            "returned_count": 0,
            "total_count": 0,
            "has_more": False,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }
    assert memory_status == 404
    assert memory_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert revisions_status == 404
    assert revisions_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_memory_review_queue_endpoint_returns_only_active_unlabeled_memories_in_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_review_queue_state(migrated_database_urls["app"])
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
        "GET",
        "/v0/memories/review-queue",
        query_params={
            "user_id": seeded["user_id"],
            "limit": "2",
        },
    )

    assert status_code == 200
    assert payload == {
        "items": [
            {
                "id": seeded["coffee_memory_id"],
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": [seeded["coffee_update_event_id"]],
                "memory_type": "preference",
                "confidence": None,
                "salience": None,
                "confirmation_status": "unconfirmed",
                "trust_class": "deterministic",
                "promotion_eligibility": "promotable",
                "evidence_count": None,
                "independent_source_count": None,
                "extracted_by_model": None,
                "trust_reason": None,
                "valid_from": None,
                "valid_to": None,
                "last_confirmed_at": None,
                "is_high_risk": True,
                "is_stale_truth": False,
                "is_promotable": True,
                "queue_priority_mode": "recent_first",
                "priority_reason": "recent_first",
                "created_at": payload["items"][0]["created_at"],
                "updated_at": payload["items"][0]["updated_at"],
            },
            {
                "id": seeded["book_memory_id"],
                "memory_key": "user.preference.book",
                "value": {"genre": "science fiction"},
                "status": "active",
                "source_event_ids": [seeded["book_add_event_id"]],
                "memory_type": "preference",
                "confidence": None,
                "salience": None,
                "confirmation_status": "unconfirmed",
                "trust_class": "deterministic",
                "promotion_eligibility": "promotable",
                "evidence_count": None,
                "independent_source_count": None,
                "extracted_by_model": None,
                "trust_reason": None,
                "valid_from": None,
                "valid_to": None,
                "last_confirmed_at": None,
                "is_high_risk": True,
                "is_stale_truth": False,
                "is_promotable": True,
                "queue_priority_mode": "recent_first",
                "priority_reason": "recent_first",
                "created_at": payload["items"][1]["created_at"],
                "updated_at": payload["items"][1]["updated_at"],
            },
        ],
        "summary": {
            "memory_status": "active",
            "review_state": "unlabeled",
            "priority_mode": "recent_first",
            "available_priority_modes": [
                "oldest_first",
                "recent_first",
                "high_risk_first",
                "stale_truth_first",
            ],
            "limit": 2,
            "returned_count": 2,
            "total_count": 2,
            "has_more": False,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }


def test_memory_review_queue_endpoint_supports_all_priority_modes(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_review_queue_priority_state(migrated_database_urls["app"])
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

    oldest_status, oldest_payload = invoke_request(
        "GET",
        "/v0/memories/review-queue",
        query_params={
            "user_id": seeded["user_id"],
            "limit": "3",
            "priority_mode": "oldest_first",
        },
    )
    recent_status, recent_payload = invoke_request(
        "GET",
        "/v0/memories/review-queue",
        query_params={
            "user_id": seeded["user_id"],
            "limit": "3",
            "priority_mode": "recent_first",
        },
    )
    high_risk_status, high_risk_payload = invoke_request(
        "GET",
        "/v0/memories/review-queue",
        query_params={
            "user_id": seeded["user_id"],
            "limit": "3",
            "priority_mode": "high_risk_first",
        },
    )
    stale_truth_status, stale_truth_payload = invoke_request(
        "GET",
        "/v0/memories/review-queue",
        query_params={
            "user_id": seeded["user_id"],
            "limit": "3",
            "priority_mode": "stale_truth_first",
        },
    )

    assert oldest_status == 200
    assert recent_status == 200
    assert high_risk_status == 200
    assert stale_truth_status == 200

    assert [item["id"] for item in oldest_payload["items"]] == [
        seeded["oldest_memory_id"],
        seeded["middle_memory_id"],
        seeded["newest_memory_id"],
    ]
    assert [item["id"] for item in recent_payload["items"]] == [
        seeded["newest_memory_id"],
        seeded["middle_memory_id"],
        seeded["oldest_memory_id"],
    ]
    assert [item["id"] for item in high_risk_payload["items"]] == [
        seeded["newest_memory_id"],
        seeded["oldest_memory_id"],
        seeded["middle_memory_id"],
    ]
    assert [item["id"] for item in stale_truth_payload["items"]] == [
        seeded["oldest_memory_id"],
        seeded["newest_memory_id"],
        seeded["middle_memory_id"],
    ]
    assert oldest_payload["summary"]["priority_mode"] == "oldest_first"
    assert recent_payload["summary"]["priority_mode"] == "recent_first"
    assert high_risk_payload["summary"]["priority_mode"] == "high_risk_first"
    assert stale_truth_payload["summary"]["priority_mode"] == "stale_truth_first"
    assert stale_truth_payload["items"][0]["is_stale_truth"] is True


def test_memory_evaluation_summary_endpoint_returns_explicit_consistent_counts(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_evaluation_state(migrated_database_urls["app"])
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
        "GET",
        "/v0/memories/evaluation-summary",
        query_params={"user_id": seeded["user_id"]},
    )

    assert status_code == 200
    assert payload == {
        "summary": {
            "total_memory_count": 4,
            "active_memory_count": 3,
            "deleted_memory_count": 1,
            "labeled_memory_count": 2,
            "unlabeled_memory_count": 2,
            "total_label_row_count": 3,
            "label_row_counts_by_value": {
                "correct": 1,
                "incorrect": 0,
                "outdated": 1,
                "insufficient_evidence": 1,
            },
            "label_value_order": [
                "correct",
                "incorrect",
                "outdated",
                "insufficient_evidence",
            ],
        }
    }


def test_memory_review_queue_and_evaluation_summary_enforce_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_evaluation_state(migrated_database_urls["app"])
    intruder_id = uuid4()
    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        ContinuityStore(conn).create_user(intruder_id, "intruder@example.com", "Intruder")

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

    queue_status, queue_payload = invoke_request(
        "GET",
        "/v0/memories/review-queue",
        query_params={"user_id": str(intruder_id), "limit": "10"},
    )
    summary_status, summary_payload = invoke_request(
        "GET",
        "/v0/memories/evaluation-summary",
        query_params={"user_id": str(intruder_id)},
    )

    assert seeded["user_id"] != str(intruder_id)
    assert queue_status == 200
    assert queue_payload == {
        "items": [],
        "summary": {
            "memory_status": "active",
            "review_state": "unlabeled",
            "priority_mode": "recent_first",
            "available_priority_modes": [
                "oldest_first",
                "recent_first",
                "high_risk_first",
                "stale_truth_first",
            ],
            "limit": 10,
            "returned_count": 0,
            "total_count": 0,
            "has_more": False,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }
    assert summary_status == 200
    assert summary_payload == {
        "summary": {
            "total_memory_count": 0,
            "active_memory_count": 0,
            "deleted_memory_count": 0,
            "labeled_memory_count": 0,
            "unlabeled_memory_count": 0,
            "total_label_row_count": 0,
            "label_row_counts_by_value": {
                "correct": 0,
                "incorrect": 0,
                "outdated": 0,
                "insufficient_evidence": 0,
            },
            "label_value_order": [
                "correct",
                "incorrect",
                "outdated",
                "insufficient_evidence",
            ],
        }
    }
