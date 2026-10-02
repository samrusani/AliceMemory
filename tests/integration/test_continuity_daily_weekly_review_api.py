from __future__ import annotations

from datetime import UTC, datetime
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
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
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


def test_daily_and_weekly_review_endpoints_are_deterministic(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="daily-weekly@example.com")
    thread_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

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

        blocker_capture = store.create_continuity_capture_event(
            raw_content="Blocker: Missing API key",
            explicit_signal="blocker",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_blocker",
        )
        blocker_object = store.create_continuity_object(
            capture_event_id=blocker_capture["id"],
            object_type="Blocker",
            status="active",
            title="Blocker: Missing API key",
            body={"blocking_reason": "Missing API key"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        stale_capture = store.create_continuity_capture_event(
            raw_content="Waiting For: Stale finance reply",
            explicit_signal="waiting_for",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_waiting_for",
        )
        stale_object = store.create_continuity_object(
            capture_event_id=stale_capture["id"],
            object_type="WaitingFor",
            status="stale",
            title="Waiting For: Stale finance reply",
            body={"waiting_for_text": "Stale finance reply"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        next_capture = store.create_continuity_capture_event(
            raw_content="Next Action: Send follow-up",
            explicit_signal="next_action",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_next_action",
        )
        next_object = store.create_continuity_object(
            capture_event_id=next_capture["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Send follow-up",
            body={"action_text": "Send follow-up"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

        store.create_continuity_correction_event(
            continuity_object_id=waiting_object["id"],
            action="confirm",
            reason="weekly-check-1",
            before_snapshot={"status": "active"},
            after_snapshot={"status": "active"},
            payload={"source": "weekly_review_seed"},
        )
        store.create_continuity_correction_event(
            continuity_object_id=waiting_object["id"],
            action="edit",
            reason="weekly-check-2",
            before_snapshot={"status": "active"},
            after_snapshot={"status": "active"},
            payload={"source": "weekly_review_seed"},
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=waiting_object["id"],
        created_at=datetime(2026, 3, 30, 8, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=blocker_object["id"],
        created_at=datetime(2026, 3, 30, 8, 5, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=stale_object["id"],
        created_at=datetime(2026, 3, 30, 8, 10, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_object["id"],
        created_at=datetime(2026, 3, 30, 8, 15, tzinfo=UTC),
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

    first_daily_status, first_daily = invoke_request(
        "GET",
        "/v0/continuity/daily-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "3",
        },
    )
    second_daily_status, second_daily = invoke_request(
        "GET",
        "/v0/continuity/daily-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "3",
        },
    )

    assert first_daily_status == 200
    assert second_daily_status == 200
    assert first_daily == second_daily
    assert [item["title"] for item in first_daily["brief"]["waiting_for_highlights"]["items"]] == [
        "Waiting For: Vendor quote",
    ]
    assert (
        first_daily["brief"]["waiting_for_highlights"]["items"][0]["explanation"]["trust"]["trust_class"]
        == "human_curated"
    )
    assert [item["title"] for item in first_daily["brief"]["blocker_highlights"]["items"]] == [
        "Blocker: Missing API key",
    ]
    assert [item["title"] for item in first_daily["brief"]["stale_items"]["items"]] == [
        "Waiting For: Stale finance reply",
    ]
    assert first_daily["brief"]["next_suggested_action"]["item"]["title"] == "Next Action: Send follow-up"

    first_weekly_status, first_weekly = invoke_request(
        "GET",
        "/v0/continuity/weekly-review",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "5",
        },
    )
    second_weekly_status, second_weekly = invoke_request(
        "GET",
        "/v0/continuity/weekly-review",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "5",
        },
    )

    assert first_weekly_status == 200
    assert second_weekly_status == 200
    assert first_weekly == second_weekly
    assert first_weekly["review"]["rollup"] == {
        "total_count": 4,
        "waiting_for_count": 1,
        "blocker_count": 1,
        "stale_count": 1,
        "correction_recurrence_count": 1,
        "freshness_drift_count": 1,
        "next_action_count": 1,
        "posture_order": ["waiting_for", "blocker", "stale", "next_action"],
    }


def test_daily_and_weekly_review_endpoints_emit_explicit_empty_states(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="daily-weekly-empty@example.com")
    thread_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        note_capture = store.create_continuity_capture_event(
            raw_content="Note: context only",
            explicit_signal="note",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_note",
        )
        note_object = store.create_continuity_object(
            capture_event_id=note_capture["id"],
            object_type="Note",
            status="active",
            title="Note: context only",
            body={"body": "context only"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=note_object["id"],
        created_at=datetime(2026, 3, 30, 9, 0, tzinfo=UTC),
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

    daily_status, daily_payload = invoke_request(
        "GET",
        "/v0/continuity/daily-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "3",
        },
    )
    weekly_status, weekly_payload = invoke_request(
        "GET",
        "/v0/continuity/weekly-review",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "3",
        },
    )

    assert daily_status == 200
    assert daily_payload["brief"]["waiting_for_highlights"]["empty_state"] == {
        "is_empty": True,
        "message": "No waiting-for highlights for today in the requested scope.",
    }
    assert daily_payload["brief"]["blocker_highlights"]["empty_state"] == {
        "is_empty": True,
        "message": "No blocker highlights for today in the requested scope.",
    }
    assert daily_payload["brief"]["stale_items"]["empty_state"] == {
        "is_empty": True,
        "message": "No stale items for today in the requested scope.",
    }
    assert daily_payload["brief"]["next_suggested_action"] == {
        "item": None,
        "empty_state": {
            "is_empty": True,
            "message": "No next suggested action in the requested scope.",
        },
    }

    assert weekly_status == 200
    assert weekly_payload["review"]["rollup"]["total_count"] == 0
    assert weekly_payload["review"]["rollup"]["correction_recurrence_count"] == 0
    assert weekly_payload["review"]["rollup"]["freshness_drift_count"] == 0
    assert weekly_payload["review"]["waiting_for"]["empty_state"] == {
        "is_empty": True,
        "message": "No waiting-for items in the requested scope.",
    }
    assert weekly_payload["review"]["blocker"]["empty_state"] == {
        "is_empty": True,
        "message": "No blocker items in the requested scope.",
    }
    assert weekly_payload["review"]["stale"]["empty_state"] == {
        "is_empty": True,
        "message": "No stale items in the requested scope.",
    }
    assert weekly_payload["review"]["next_action"]["empty_state"] == {
        "is_empty": True,
        "message": "No next-action items in the requested scope.",
    }


def test_daily_and_weekly_review_endpoints_reject_mixed_naive_and_offset_aware_time_window(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="daily-weekly-window-validation@example.com")

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

    for path in ("/v0/continuity/daily-brief", "/v0/continuity/weekly-review"):
        status, payload = invoke_request(
            "GET",
            path,
            query_params={
                "user_id": str(user_id),
                "since": "2026-03-30T10:00:00Z",
                "until": "2026-03-30T10:01:00",
            },
        )
        assert status == 400
        assert payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
