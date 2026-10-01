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
from alicebot_api.continuity_contradictions import sync_contradictions
from alicebot_api.contracts import ContradictionSyncInput
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
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
    request_headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        request_headers.append((key.lower().encode(), value.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": request_headers,
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


def identity_header(user_id: UUID | str) -> dict[str, str]:
    return {"x-alicebot-user-id": str(user_id)}


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


def test_continuity_brief_api_returns_one_call_bundle(
    migrated_database_urls,
    monkeypatch,
) -> None:
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

    user_id = seed_user(
        migrated_database_urls["app"],
        email="continuity-brief@example.com",
    )
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        fact_one = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Decision: Deployment owner is Operations",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )["id"],
            object_type="Decision",
            status="active",
            title="Decision: Deployment owner is Operations",
            body={
                "fact_key": "deployment_owner",
                "fact_value": "Operations",
                "decision_text": "Deployment owner is Operations",
            },
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-api-1"]},
            confidence=0.92,
        )
        fact_two = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Decision: Deployment owner is Engineering",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )["id"],
            object_type="Decision",
            status="active",
            title="Decision: Deployment owner is Engineering",
            body={
                "fact_key": "deployment_owner",
                "fact_value": "Engineering",
                "decision_text": "Deployment owner is Engineering",
            },
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-api-2"]},
            confidence=0.88,
        )
        blocker = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Blocker: Missing deployment credentials",
                explicit_signal="blocker",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_blocker",
            )["id"],
            object_type="Blocker",
            status="active",
            title="Blocker: Missing deployment credentials",
            body={"blocker_text": "Missing deployment credentials"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-api-3"]},
            confidence=0.9,
        )
        decision = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Decision: Keep deploy rollout phased",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep deploy rollout phased",
            body={"decision_text": "Keep deploy rollout phased"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-api-4"]},
            confidence=0.96,
        )
        next_action = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Next Action: Draft deploy checklist",
                explicit_signal="next_action",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_next_action",
            )["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Draft deploy checklist",
            body={"action_text": "Draft deploy checklist"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-api-5"]},
            confidence=0.95,
        )
        sync_contradictions(
            store,
            user_id=user_id,
            request=ContradictionSyncInput(limit=20),
        )

    for continuity_object_id, created_at in (
        (fact_one["id"], datetime(2026, 4, 10, 9, 0, tzinfo=UTC)),
        (fact_two["id"], datetime(2026, 4, 10, 9, 5, tzinfo=UTC)),
        (blocker["id"], datetime(2026, 4, 10, 9, 10, tzinfo=UTC)),
        (decision["id"], datetime(2026, 4, 10, 9, 15, tzinfo=UTC)),
        (next_action["id"], datetime(2026, 4, 10, 9, 20, tzinfo=UTC)),
    ):
        set_continuity_timestamps(
            migrated_database_urls["admin"],
            continuity_object_id=continuity_object_id,
            created_at=created_at,
        )

    status, payload = invoke_request(
        "POST",
        "/v1/continuity/brief",
        payload={
            "brief_type": "agent_handoff",
            "thread_id": str(thread_id),
            "query": "deployment",
            "max_relevant_facts": 4,
            "max_recent_changes": 4,
            "max_open_loops": 3,
            "max_conflicts": 3,
            "max_timeline_highlights": 4,
        },
        headers=identity_header(user_id),
    )

    assert status == 200
    brief = payload["brief"]
    assert brief["assembly_version"] == "continuity_brief_v0"
    assert brief["brief_type"] == "agent_handoff"
    assert brief["selection_strategy"]["task_brief_mode"] == "agent_handoff"
    assert brief["summary"].startswith("agent handoff brief.")
    assert brief["relevant_facts"]["summary"]["returned_count"] >= 1
    assert brief["recent_changes"]["summary"]["returned_count"] >= 1
    assert brief["open_loops"]["summary"]["total_count"] == 1
    assert brief["conflicts"]["summary"]["total_count"] >= 1
    assert brief["timeline_highlights"]["summary"]["returned_count"] >= 1
    assert brief["next_suggested_action"]["title"] == "Next Action: Draft deploy checklist"
    assert brief["trust_posture"]["open_conflict_count"] >= 1
    assert brief["trust_posture"]["active_signal_count"] >= 1
    assert brief["provenance_bundle"]["summary"]["reference_count"] >= 1
