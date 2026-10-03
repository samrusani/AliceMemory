from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

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


def set_continuity_timestamps(
    admin_database_url: str,
    *,
    continuity_object_id: UUID,
    created_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE continuity_objects SET created_at = %s, updated_at = %s WHERE id = %s",
                (created_at, created_at, continuity_object_id),
            )


def set_continuity_lifecycle_flags(
    admin_database_url: str,
    *,
    continuity_object_id: UUID,
    is_promotable: bool,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE continuity_objects SET is_promotable = %s WHERE id = %s",
                (is_promotable, continuity_object_id),
            )


def test_continuity_resumption_api_returns_required_sections(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: Freeze API contract",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        decision_object = store.create_continuity_object(
            capture_event_id=decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Freeze API contract",
            body={"decision_text": "Freeze API contract"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        waiting_capture = store.create_continuity_capture_event(
            raw_content="Waiting For: Vendor quote",
            explicit_signal="waiting_for",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_waiting_for",
        )
        waiting_object = store.create_continuity_object(
            capture_event_id=waiting_capture["id"],
            object_type="WaitingFor",
            status="active",
            title="Waiting For: Vendor quote",
            body={"waiting_for_text": "Vendor quote"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        next_capture = store.create_continuity_capture_event(
            raw_content="Next Action: Send approval email",
            explicit_signal="next_action",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_next_action",
        )
        next_object = store.create_continuity_object(
            capture_event_id=next_capture["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Send approval email",
            body={"action_text": "Send approval email"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        latest_decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep rollout phased",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        latest_decision_object = store.create_continuity_object(
            capture_event_id=latest_decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep rollout phased",
            body={"decision_text": "Keep rollout phased"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision_object["id"],
        created_at=datetime(2026, 3, 29, 9, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=waiting_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_object["id"],
        created_at=datetime(2026, 3, 29, 10, 5, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=latest_decision_object["id"],
        created_at=datetime(2026, 3, 29, 10, 10, tzinfo=UTC),
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

    status, payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_recent_changes": "3",
            "max_open_loops": "2",
        },
    )

    assert status == 200
    brief = payload["brief"]
    assert brief["assembly_version"] == "continuity_resumption_brief_v0"
    assert brief["last_decision"]["item"]["title"] == "Decision: Keep rollout phased"
    assert brief["last_decision"]["item"]["explanation"]["trust"]["trust_class"] == "llm_single_source"
    assert brief["open_loops"]["summary"] == {
        "limit": 2,
        "returned_count": 1,
        "total_count": 1,
        "order": ["created_at_desc", "id_desc"],
    }
    assert [item["title"] for item in brief["recent_changes"]["items"]] == [
        "Decision: Keep rollout phased",
        "Next Action: Send approval email",
        "Waiting For: Vendor quote",
    ]
    assert brief["next_action"]["item"]["title"] == "Next Action: Send approval email"


def test_continuity_resumption_api_returns_explicit_empty_states(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner2@example.com")
    task_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Note: context only",
            explicit_signal="note",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_note",
        )
        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type="Note",
            status="active",
            title="Note: context only",
            body={"body": "context only"},
            provenance={"task_id": str(task_id)},
            confidence=1.0,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=continuity_object["id"],
        created_at=datetime(2026, 3, 29, 9, 0, tzinfo=UTC),
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

    status, payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "task_id": str(task_id),
            "max_recent_changes": "2",
            "max_open_loops": "2",
        },
    )

    assert status == 200
    assert payload["brief"]["last_decision"] == {
        "item": None,
        "empty_state": {
            "is_empty": True,
            "message": "No decision found in the requested scope.",
        },
    }
    assert payload["brief"]["open_loops"]["empty_state"] == {
        "is_empty": True,
        "message": "No open loops found in the requested scope.",
    }
    assert payload["brief"]["next_action"] == {
        "item": None,
        "empty_state": {
            "is_empty": True,
            "message": "No next action found in the requested scope.",
        },
    }


def test_continuity_resumption_api_selects_latest_sections_beyond_recall_limit(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner3@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    base_time = datetime(2026, 3, 29, 8, 0, tzinfo=UTC)

    historical_object_ids: list[UUID] = []

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        for index in range(110):
            capture = store.create_continuity_capture_event(
                raw_content=f"Decision: historical {index}",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )
            continuity_object = store.create_continuity_object(
                capture_event_id=capture["id"],
                object_type="Decision",
                status="active",
                title=f"Decision: historical {index}",
                body={"decision_text": f"historical {index}"},
                provenance={"thread_id": str(thread_id)},
                confidence=1.0,
            )
            historical_object_ids.append(continuity_object["id"])

        latest_decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: newest low confidence",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        latest_decision_object = store.create_continuity_object(
            capture_event_id=latest_decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: newest low confidence",
            body={"decision_text": "newest low confidence"},
            provenance={"thread_id": str(thread_id)},
            confidence=0.01,
        )

        latest_next_action_capture = store.create_continuity_capture_event(
            raw_content="Next Action: newest low confidence",
            explicit_signal="next_action",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_next_action",
        )
        latest_next_action_object = store.create_continuity_object(
            capture_event_id=latest_next_action_capture["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: newest low confidence",
            body={"action_text": "newest low confidence"},
            provenance={"thread_id": str(thread_id)},
            confidence=0.01,
        )

    for index, continuity_object_id in enumerate(historical_object_ids):
        set_continuity_timestamps(
            migrated_database_urls["admin"],
            continuity_object_id=continuity_object_id,
            created_at=base_time + timedelta(minutes=index),
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=latest_decision_object["id"],
        created_at=base_time + timedelta(minutes=200),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=latest_next_action_object["id"],
        created_at=base_time + timedelta(minutes=201),
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

    status, payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_recent_changes": "2",
            "max_open_loops": "1",
        },
    )

    assert status == 200
    brief = payload["brief"]
    assert brief["last_decision"]["item"]["title"] == "Decision: newest low confidence"
    assert brief["next_action"]["item"]["title"] == "Next Action: newest low confidence"
    assert [item["title"] for item in brief["recent_changes"]["items"]] == [
        "Next Action: newest low confidence",
        "Decision: newest low confidence",
    ]


def test_continuity_resumption_api_uses_promotable_facts_by_default_with_override(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="promotable@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        fact_capture = store.create_continuity_capture_event(
            raw_content="Remember: hidden from brief",
            explicit_signal="remember_this",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_remember_this",
        )
        fact_object = store.create_continuity_object(
            capture_event_id=fact_capture["id"],
            object_type="MemoryFact",
            status="active",
            title="Memory Fact: hidden from brief",
            body={"fact_text": "hidden from brief"},
            provenance={"thread_id": str(thread_id)},
            confidence=0.9,
        )
        decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: visible in brief",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        decision_object = store.create_continuity_object(
            capture_event_id=decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: visible in brief",
            body={"decision_text": "visible in brief"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

    set_continuity_lifecycle_flags(
        migrated_database_urls["admin"],
        continuity_object_id=fact_object["id"],
        is_promotable=False,
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=fact_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision_object["id"],
        created_at=datetime(2026, 3, 29, 10, 5, tzinfo=UTC),
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

    default_status, default_payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_recent_changes": "5",
            "max_open_loops": "2",
        },
    )
    override_status, override_payload = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_recent_changes": "5",
            "max_open_loops": "2",
            "include_non_promotable_facts": "true",
        },
    )

    assert default_status == 200
    assert [item["title"] for item in default_payload["brief"]["recent_changes"]["items"]] == [
        "Decision: visible in brief",
    ]

    assert override_status == 200
    assert [item["title"] for item in override_payload["brief"]["recent_changes"]["items"]] == [
        "Decision: visible in brief",
        "Memory Fact: hidden from brief",
    ]
