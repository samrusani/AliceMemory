from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
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


def create_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def seed_user_with_continuity(database_url: str, *, email: str) -> dict[str, object]:
    user_id = create_user(database_url, email=email)

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        first_thread = store.create_thread("Alpha thread")
        second_thread = store.create_thread("Beta thread")
        first_session = store.create_session(second_thread["id"], status="completed")
        second_session = store.create_session(second_thread["id"], status="active")
        first_event = store.append_event(
            second_thread["id"],
            second_session["id"],
            "message.user",
            {"text": "Hello"},
        )
        second_event = store.append_event(
            second_thread["id"],
            second_session["id"],
            "message.assistant",
            {"text": "Hello back"},
        )

    return {
        "user_id": user_id,
        "first_thread": first_thread,
        "second_thread": second_thread,
        "first_session": first_session,
        "second_session": second_session,
        "first_event": first_event,
        "second_event": second_event,
    }


def set_thread_timestamps(
    admin_database_url: str,
    *,
    thread_id: UUID,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE threads SET created_at = %s, updated_at = %s WHERE id = %s",
                (created_at, updated_at, thread_id),
            )


def set_session_timestamps(
    admin_database_url: str,
    *,
    session_id: UUID,
    started_at: datetime,
    ended_at: datetime | None,
    created_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET started_at = %s, ended_at = %s, created_at = %s WHERE id = %s",
                (started_at, ended_at, created_at, session_id),
            )


def serialize_thread(
    *,
    thread_id: UUID,
    title: str,
    created_at: datetime,
    updated_at: datetime,
    agent_profile_id: str,
) -> dict[str, Any]:
    return {
        "id": str(thread_id),
        "title": title,
        "agent_profile_id": agent_profile_id,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def serialize_session(
    *,
    session_id: UUID,
    thread_id: UUID,
    status: str,
    started_at: datetime | None,
    ended_at: datetime | None,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "id": str(session_id),
        "thread_id": str(thread_id),
        "status": status,
        "started_at": None if started_at is None else started_at.isoformat(),
        "ended_at": None if ended_at is None else ended_at.isoformat(),
        "created_at": created_at.isoformat(),
    }


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(event["id"]),
        "thread_id": str(event["thread_id"]),
        "session_id": None if event["session_id"] is None else str(event["session_id"]),
        "sequence_no": event["sequence_no"],
        "kind": event["kind"],
        "payload": event["payload"],
        "created_at": event["created_at"].isoformat(),
    }


def test_thread_continuity_endpoints_create_list_detail_sessions_and_events(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_continuity(migrated_database_urls["app"], email="owner@example.com")
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/threads",
        payload={
            "user_id": str(seeded["user_id"]),
            "title": "Gamma thread",
            "agent_profile_id": "coach_default",
        },
    )

    assert create_status == 201
    assert create_payload["thread"]["title"] == "Gamma thread"
    assert create_payload["thread"]["agent_profile_id"] == "coach_default"

    api_thread_id = UUID(create_payload["thread"]["id"])
    shared_created_at = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
    newer_created_at = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)
    first_session_start = shared_created_at
    first_session_end = shared_created_at + timedelta(minutes=5)
    second_session_start = shared_created_at + timedelta(hours=1)

    set_thread_timestamps(
        migrated_database_urls["admin"],
        thread_id=seeded["first_thread"]["id"],
        created_at=shared_created_at,
        updated_at=shared_created_at,
    )
    set_thread_timestamps(
        migrated_database_urls["admin"],
        thread_id=seeded["second_thread"]["id"],
        created_at=shared_created_at,
        updated_at=shared_created_at,
    )
    set_thread_timestamps(
        migrated_database_urls["admin"],
        thread_id=api_thread_id,
        created_at=newer_created_at,
        updated_at=newer_created_at,
    )
    set_session_timestamps(
        migrated_database_urls["admin"],
        session_id=seeded["first_session"]["id"],
        started_at=first_session_start,
        ended_at=first_session_end,
        created_at=first_session_start,
    )
    set_session_timestamps(
        migrated_database_urls["admin"],
        session_id=seeded["second_session"]["id"],
        started_at=second_session_start,
        ended_at=None,
        created_at=second_session_start,
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored_thread_ids = [thread["id"] for thread in ContinuityStore(conn).list_threads()]

    assert api_thread_id in stored_thread_ids

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/threads",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/threads/{seeded['second_thread']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )
    sessions_status, sessions_payload = invoke_request(
        "GET",
        f"/v0/threads/{seeded['second_thread']['id']}/sessions",
        query_params={"user_id": str(seeded["user_id"])},
    )
    events_status, events_payload = invoke_request(
        "GET",
        f"/v0/threads/{seeded['second_thread']['id']}/events",
        query_params={"user_id": str(seeded["user_id"])},
    )
    tied_threads = sorted(
        [seeded["first_thread"], seeded["second_thread"]],
        key=lambda thread: (thread["created_at"], thread["id"]),
        reverse=True,
    )

    assert list_status == 200
    assert list_payload == {
        "items": [
            serialize_thread(
                thread_id=api_thread_id,
                title="Gamma thread",
                created_at=newer_created_at,
                updated_at=newer_created_at,
                agent_profile_id="coach_default",
            ),
            serialize_thread(
                thread_id=tied_threads[0]["id"],
                title=tied_threads[0]["title"],
                created_at=shared_created_at,
                updated_at=shared_created_at,
                agent_profile_id="assistant_default",
            ),
            serialize_thread(
                thread_id=tied_threads[1]["id"],
                title=tied_threads[1]["title"],
                created_at=shared_created_at,
                updated_at=shared_created_at,
                agent_profile_id="assistant_default",
            ),
        ],
        "summary": {
            "total_count": 3,
            "order": ["created_at_desc", "id_desc"],
        },
    }

    assert detail_status == 200
    assert detail_payload == {
        "thread": serialize_thread(
            thread_id=seeded["second_thread"]["id"],
            title="Beta thread",
            created_at=shared_created_at,
            updated_at=shared_created_at,
            agent_profile_id="assistant_default",
        )
    }

    assert sessions_status == 200
    assert sessions_payload == {
        "items": [
            serialize_session(
                session_id=seeded["first_session"]["id"],
                thread_id=seeded["second_thread"]["id"],
                status="completed",
                started_at=first_session_start,
                ended_at=first_session_end,
                created_at=first_session_start,
            ),
            serialize_session(
                session_id=seeded["second_session"]["id"],
                thread_id=seeded["second_thread"]["id"],
                status="active",
                started_at=second_session_start,
                ended_at=None,
                created_at=second_session_start,
            ),
        ],
        "summary": {
            "thread_id": str(seeded["second_thread"]["id"]),
            "total_count": 2,
            "order": ["started_at_asc", "created_at_asc", "id_asc"],
        },
    }

    assert events_status == 200
    assert events_payload == {
        "items": [
            serialize_event(seeded["first_event"]),
            serialize_event(seeded["second_event"]),
        ],
        "summary": {
            "thread_id": str(seeded["second_thread"]["id"]),
            "total_count": 2,
            "order": ["sequence_no_asc"],
        },
    }


def test_thread_creation_defaults_agent_profile_id_when_omitted(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = create_user(migrated_database_urls["app"], email="owner@example.com")
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

    status, payload = invoke_request(
        "POST",
        "/v0/threads",
        payload={
            "user_id": str(user_id),
            "title": "Default profile thread",
        },
    )

    assert status == 201
    assert payload["thread"]["agent_profile_id"] == "assistant_default"

    thread_id = UUID(payload["thread"]["id"])
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        stored_thread = ContinuityStore(conn).get_thread(thread_id)

    assert stored_thread["agent_profile_id"] == "assistant_default"


def test_thread_resumption_brief_endpoint_returns_bounded_sections_and_workflow_posture(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_continuity(migrated_database_urls["app"], email="owner@example.com")
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

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_memory(
            memory_key="user.preference.tea",
            value={"likes": "green"},
            status="active",
            source_event_ids=[],
            memory_type="preference",
        )
        store.create_memory(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            status="active",
            source_event_ids=[],
            memory_type="preference",
        )
        store.create_memory(
            memory_key="user.preference.deleted",
            value={"likes": "espresso"},
            status="deleted",
            source_event_ids=[],
            memory_type="preference",
        )
        store.create_open_loop(
            memory_id=None,
            title="Older open loop",
            status="open",
            opened_at=datetime(2026, 3, 18, 9, 0, tzinfo=UTC),
            due_at=None,
            resolved_at=None,
            resolution_note=None,
        )
        store.create_open_loop(
            memory_id=None,
            title="Latest open loop",
            status="open",
            opened_at=datetime(2026, 3, 18, 10, 0, tzinfo=UTC),
            due_at=None,
            resolved_at=None,
            resolution_note=None,
        )
        store.create_open_loop(
            memory_id=None,
            title="Resolved open loop",
            status="resolved",
            opened_at=datetime(2026, 3, 18, 11, 0, tzinfo=UTC),
            due_at=None,
            resolved_at=datetime(2026, 3, 18, 11, 5, tzinfo=UTC),
            resolution_note="resolved",
        )
        tool = store.create_tool(
            tool_key="proxy.echo",
            name="Proxy Echo",
            description="Deterministic proxy handler.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["proxy"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        task = store.create_task(
            thread_id=seeded["second_thread"]["id"],
            tool_id=tool["id"],
            status="approved",
            request={
                "thread_id": str(seeded["second_thread"]["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {"mode": "resumption"},
            },
            tool={
                "id": str(tool["id"]),
                "tool_key": "proxy.echo",
                "name": "Proxy Echo",
                "description": "Deterministic proxy handler.",
                "version": "1.0.0",
                "metadata_version": "tool_metadata_v0",
                "active": True,
                "tags": ["proxy"],
                "action_hints": ["tool.run"],
                "scope_hints": ["workspace"],
                "domain_hints": [],
                "risk_hints": [],
                "metadata": {"transport": "proxy"},
                "created_at": tool["created_at"].isoformat(),
            },
            latest_approval_id=None,
            latest_execution_id=None,
        )
        first_step_trace = store.create_trace(
            user_id=seeded["user_id"],
            thread_id=seeded["second_thread"]["id"],
            kind="task.step.sequence",
            compiler_version="task_step_sequence_v0",
            status="completed",
            limits={"max_steps": 1},
        )
        second_step_trace = store.create_trace(
            user_id=seeded["user_id"],
            thread_id=seeded["second_thread"]["id"],
            kind="task.step.transition",
            compiler_version="task_step_transition_v0",
            status="completed",
            limits={"max_steps": 1},
        )
        store.create_task_step(
            task_id=task["id"],
            sequence_no=1,
            kind="governed_request",
            status="approved",
            request={
                "thread_id": str(seeded["second_thread"]["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {"step": 1},
            },
            outcome={
                "routing_decision": "ready",
                "approval_id": None,
                "approval_status": None,
                "execution_id": None,
                "execution_status": None,
                "blocked_reason": None,
            },
            trace_id=first_step_trace["id"],
            trace_kind="task.step.sequence",
        )
        latest_step = store.create_task_step(
            task_id=task["id"],
            sequence_no=2,
            kind="governed_request",
            status="executed",
            request={
                "thread_id": str(seeded["second_thread"]["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {"step": 2},
            },
            outcome={
                "routing_decision": "ready",
                "approval_id": None,
                "approval_status": None,
                "execution_id": "execution-1",
                "execution_status": "completed",
                "blocked_reason": None,
            },
            trace_id=second_step_trace["id"],
            trace_kind="task.step.transition",
        )

    status, payload = invoke_request(
        "GET",
        f"/v0/threads/{seeded['second_thread']['id']}/resumption-brief",
        query_params={
            "user_id": str(seeded["user_id"]),
            "max_events": "1",
            "max_open_loops": "1",
            "max_memories": "1",
        },
    )

    assert status == 200
    assert payload["brief"]["assembly_version"] == "resumption_brief_v0"
    assert payload["brief"]["thread"]["id"] == str(seeded["second_thread"]["id"])
    assert payload["brief"]["conversation"]["summary"] == {
        "limit": 1,
        "returned_count": 1,
        "total_count": 2,
        "order": ["sequence_no_asc"],
        "kinds": ["message.user", "message.assistant"],
    }
    assert [item["sequence_no"] for item in payload["brief"]["conversation"]["items"]] == [2]
    assert payload["brief"]["open_loops"]["summary"] == {
        "limit": 1,
        "returned_count": 1,
        "total_count": 2,
        "order": ["opened_at_desc", "created_at_desc", "id_desc"],
    }
    assert [item["title"] for item in payload["brief"]["open_loops"]["items"]] == ["Latest open loop"]
    assert payload["brief"]["memory_highlights"]["summary"] == {
        "limit": 1,
        "returned_count": 1,
        "total_count": 2,
        "order": ["updated_at_asc", "created_at_asc", "id_asc"],
    }
    assert [item["memory_key"] for item in payload["brief"]["memory_highlights"]["items"]] == ["user.preference.coffee"]
    assert payload["brief"]["workflow"]["task"]["id"] == str(task["id"])
    assert payload["brief"]["workflow"]["latest_task_step"]["id"] == str(latest_step["id"])
    assert payload["brief"]["workflow"]["latest_task_step"]["sequence_no"] == 2
    assert payload["brief"]["workflow"]["summary"] == {
        "present": True,
        "task_order": ["created_at_asc", "id_asc"],
        "task_step_order": ["sequence_no_asc", "created_at_asc", "id_asc"],
    }
    assert payload["brief"]["sources"] == [
        "threads",
        "events",
        "open_loops",
        "memories",
        "tasks",
        "task_steps",
    ]


def test_thread_continuity_endpoints_enforce_user_isolation_and_not_found(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_continuity(migrated_database_urls["app"], email="owner@example.com")
    intruder_id = create_user(migrated_database_urls["app"], email="intruder@example.com")
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
        "/v0/threads",
        query_params={"user_id": str(intruder_id)},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/threads/{owner['second_thread']['id']}",
        query_params={"user_id": str(intruder_id)},
    )
    sessions_status, sessions_payload = invoke_request(
        "GET",
        f"/v0/threads/{owner['second_thread']['id']}/sessions",
        query_params={"user_id": str(intruder_id)},
    )
    events_status, events_payload = invoke_request(
        "GET",
        f"/v0/threads/{owner['second_thread']['id']}/events",
        query_params={"user_id": str(intruder_id)},
    )
    brief_status, brief_payload = invoke_request(
        "GET",
        f"/v0/threads/{owner['second_thread']['id']}/resumption-brief",
        query_params={"user_id": str(intruder_id)},
    )
    missing_thread_id = uuid4()
    missing_brief_status, missing_brief_payload = invoke_request(
        "GET",
        f"/v0/threads/{missing_thread_id}/resumption-brief",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert list_status == 200
    assert list_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["created_at_desc", "id_desc"],
        },
    }
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert sessions_status == 404
    assert sessions_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert events_status == 404
    assert events_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert brief_status == 404
    assert brief_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert missing_brief_status == 404
    assert missing_brief_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_thread_creation_rejects_invalid_agent_profile_id_with_deterministic_422(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = create_user(migrated_database_urls["app"], email="owner@example.com")
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

    status, payload = invoke_request(
        "POST",
        "/v0/threads",
        payload={
            "user_id": str(user_id),
            "title": "Invalid profile thread",
            "agent_profile_id": "not_a_profile",
        },
    )

    assert status == 422
    assert payload == {
        "detail": {
            "code": "invalid_agent_profile_id",
            "message": "agent_profile_id must be one of: assistant_default, coach_default",
            "allowed_agent_profile_ids": ["assistant_default", "coach_default"],
        }
    }


def test_agent_profiles_endpoint_returns_deterministic_registry_payload(
    migrated_database_urls,
    monkeypatch,
) -> None:
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

    status, payload = invoke_request("GET", "/v0/agent-profiles")

    assert status == 200
    assert payload == {
        "items": [
            {
                "id": "assistant_default",
                "name": "Assistant Default",
                "description": "General-purpose assistant profile for baseline conversations.",
                "model_provider": "openai_responses",
                "model_name": "gpt-5-mini",
            },
            {
                "id": "coach_default",
                "name": "Coach Default",
                "description": "Coaching-oriented profile focused on guidance and accountability.",
                "model_provider": "openai_responses",
                "model_name": "gpt-5",
            },
        ],
        "summary": {"total_count": 2, "order": ["id_asc"]},
    }


def test_context_compile_includes_active_agent_profile_metadata(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = create_user(migrated_database_urls["app"], email="owner@example.com")
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.create_thread("Profile metadata thread", agent_profile_id="coach_default")
        session = store.create_session(thread["id"], status="active")
        store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "Compile metadata check"},
        )

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

    status, payload = invoke_request(
        "POST",
        "/v0/context/compile",
        payload={
            "user_id": str(user_id),
            "thread_id": str(thread["id"]),
        },
    )

    assert status == 200
    assert payload["metadata"] == {"agent_profile_id": "coach_default"}
