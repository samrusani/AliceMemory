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


def test_continuity_open_loop_dashboard_groups_posture_and_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="dashboard@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

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

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=waiting_object["id"],
        created_at=datetime(2026, 3, 30, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=blocker_object["id"],
        created_at=datetime(2026, 3, 30, 10, 5, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_object["id"],
        created_at=datetime(2026, 3, 30, 10, 10, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=stale_object["id"],
        created_at=datetime(2026, 3, 30, 10, 15, tzinfo=UTC),
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
        "/v0/continuity/open-loops",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "limit": "20",
        },
    )

    assert status == 200
    dashboard = payload["dashboard"]
    assert dashboard["summary"] == {
        "limit": 20,
        "total_count": 4,
        "posture_order": ["waiting_for", "blocker", "stale", "next_action"],
        "item_order": ["created_at_desc", "id_desc"],
    }
    assert [item["title"] for item in dashboard["waiting_for"]["items"]] == [
        "Waiting For: Vendor quote",
    ]
    assert [item["title"] for item in dashboard["blocker"]["items"]] == [
        "Blocker: Missing API key",
    ]
    assert [item["title"] for item in dashboard["stale"]["items"]] == [
        "Waiting For: Stale finance reply",
    ]
    assert [item["title"] for item in dashboard["next_action"]["items"]] == [
        "Next Action: Send follow-up",
    ]


def test_open_loop_review_actions_update_resumption_immediately(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="review-actions@example.com")
    thread_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

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

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=waiting_object["id"],
        created_at=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
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

    done_status, done_payload = invoke_request(
        "POST",
        f"/v0/continuity/open-loops/{waiting_object['id']}/review-action",
        payload={
            "user_id": str(user_id),
            "action": "done",
            "note": "Closed after follow-up",
        },
    )
    assert done_status == 200
    assert done_payload["review_action"] == "done"
    assert done_payload["lifecycle_outcome"] == "completed"
    assert done_payload["continuity_object"]["status"] == "completed"

    resumption_after_done_status, resumption_after_done = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_open_loops": "5",
            "max_recent_changes": "5",
        },
    )
    assert resumption_after_done_status == 200
    assert resumption_after_done["brief"]["open_loops"]["items"] == []

    still_blocked_status, still_blocked_payload = invoke_request(
        "POST",
        f"/v0/continuity/open-loops/{waiting_object['id']}/review-action",
        payload={
            "user_id": str(user_id),
            "action": "still_blocked",
        },
    )
    assert still_blocked_status == 200
    assert still_blocked_payload["lifecycle_outcome"] == "active"
    assert still_blocked_payload["continuity_object"]["status"] == "active"

    resumption_after_still_blocked_status, resumption_after_still_blocked = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_open_loops": "5",
            "max_recent_changes": "5",
        },
    )
    assert resumption_after_still_blocked_status == 200
    assert [item["id"] for item in resumption_after_still_blocked["brief"]["open_loops"]["items"]] == [
        str(waiting_object["id"]),
    ]

    deferred_status, deferred_payload = invoke_request(
        "POST",
        f"/v0/continuity/open-loops/{waiting_object['id']}/review-action",
        payload={
            "user_id": str(user_id),
            "action": "deferred",
        },
    )
    assert deferred_status == 200
    assert deferred_payload["lifecycle_outcome"] == "stale"
    assert deferred_payload["continuity_object"]["status"] == "stale"

    resumption_after_deferred_status, resumption_after_deferred = invoke_request(
        "GET",
        "/v0/continuity/resumption-brief",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_open_loops": "5",
            "max_recent_changes": "5",
        },
    )
    assert resumption_after_deferred_status == 200
    assert resumption_after_deferred["brief"]["open_loops"]["items"] == []
    assert [item["status"] for item in resumption_after_deferred["brief"]["recent_changes"]["items"]][:1] == [
        "stale",
    ]


def test_open_loop_dashboard_rejects_mixed_naive_and_offset_aware_time_window(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="open-loop-window-validation@example.com")

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
        "/v0/continuity/open-loops",
        query_params={
            "user_id": str(user_id),
            "since": "2026-03-30T10:00:00Z",
            "until": "2026-03-30T10:01:00",
        },
    )

    assert status == 400
    assert payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
