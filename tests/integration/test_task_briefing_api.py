from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
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


def test_task_brief_compile_compare_and_show_are_deterministic(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="task-briefs@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        decision = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Decision: Freeze rollout scope",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )["id"],
            object_type="Decision",
            status="active",
            title="Decision: Freeze rollout scope",
            body={"decision_text": "Freeze rollout scope"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )
        waiting_for = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Waiting For: Security review",
                explicit_signal="waiting_for",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_waiting_for",
            )["id"],
            object_type="WaitingFor",
            status="active",
            title="Waiting For: Security review",
            body={"waiting_for_text": "Security review"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )
        next_action = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Next Action: Send release checklist",
                explicit_signal="next_action",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_next_action",
            )["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Send release checklist",
            body={"action_text": "Send release checklist"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )
        fact = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Memory Fact: Launch must stay artifact-only",
                explicit_signal="remember_this",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_memory",
            )["id"],
            object_type="MemoryFact",
            status="active",
            title="Memory Fact: Launch must stay artifact-only",
            body={"fact_text": "artifact-only"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision["id"],
        created_at=datetime(2026, 4, 14, 8, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=waiting_for["id"],
        created_at=datetime(2026, 4, 14, 8, 5, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_action["id"],
        created_at=datetime(2026, 4, 14, 8, 10, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=fact["id"],
        created_at=datetime(2026, 4, 14, 8, 15, tzinfo=UTC),
    )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    compile_payload = {
        "user_id": str(user_id),
        "mode": "worker_subtask",
        "thread_id": str(thread_id),
        "briefing_strategy": "compact",
    }
    first_status, first_payload = invoke_request("POST", "/v0/task-briefs/compile", payload=compile_payload)
    second_status, second_payload = invoke_request("POST", "/v0/task-briefs/compile", payload=compile_payload)
    assert first_status == 201
    assert second_status == 201
    assert first_payload["task_brief"] == second_payload["task_brief"]
    assert first_payload["task_brief"]["strategy"]["briefing_strategy"] == "compact"
    assert (
        not {
            "workspace_id",
            "pack_id",
            "pack_version",
            "model_pack_strategy",
        }
        & first_payload["task_brief"]["strategy"].keys()
    )

    show_status, show_payload = invoke_request(
        "GET",
        f"/v0/task-briefs/{first_payload['persistence']['task_brief_id']}",
        query_params={"user_id": str(user_id)},
    )
    assert show_status == 200
    assert show_payload == first_payload

    compare_status, compare_payload = invoke_request(
        "POST",
        "/v0/task-briefs/compare",
        payload={
            "user_id": str(user_id),
            "primary": {
                "mode": "worker_subtask",
                "thread_id": str(thread_id),
                "briefing_strategy": "compact",
            },
            "secondary": {
                "mode": "user_recall",
                "thread_id": str(thread_id),
                "briefing_strategy": "detailed",
            },
        },
    )
    assert compare_status == 200
    assert compare_payload["comparison"]["smaller_mode"] == "worker_subtask"
    assert (
        compare_payload["primary"]["summary"]["estimated_tokens"]
        < compare_payload["secondary"]["summary"]["estimated_tokens"]
    )
