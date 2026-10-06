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


def set_continuity_lifecycle_flags(
    admin_database_url: str,
    *,
    continuity_object_id: UUID,
    is_searchable: bool | None = None,
    is_promotable: bool | None = None,
) -> None:
    assignments: list[str] = []
    values: list[object] = []
    if is_searchable is not None:
        assignments.append("is_searchable = %s")
        values.append(is_searchable)
    if is_promotable is not None:
        assignments.append("is_promotable = %s")
        values.append(is_promotable)
    if not assignments:
        return

    values.append(continuity_object_id)
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE continuity_objects SET {', '.join(assignments)} WHERE id = %s",
                tuple(values),
            )


def test_continuity_recall_api_returns_provenance_backed_scoped_results(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder_id = seed_user(migrated_database_urls["app"], email="intruder@example.com")

    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    task_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        capture_primary = store.create_continuity_capture_event(
            raw_content="Decision: Keep rollout phased",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        primary_object = store.create_continuity_object(
            capture_event_id=capture_primary["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep rollout phased",
            body={"decision_text": "Keep rollout phased"},
            provenance={
                "thread_id": str(thread_id),
                "task_id": str(task_id),
                "project": "Project Phoenix",
                "person": "Alex",
                "confirmation_status": "confirmed",
                "source_event_ids": ["event-1"],
            },
            confidence=0.95,
        )

        capture_other = store.create_continuity_capture_event(
            raw_content="Decision: unrelated",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        other_object = store.create_continuity_object(
            capture_event_id=capture_other["id"],
            object_type="Decision",
            status="active",
            title="Decision: unrelated",
            body={"decision_text": "unrelated"},
            provenance={
                "thread_id": str(uuid4()),
                "task_id": str(uuid4()),
                "project": "Project Atlas",
                "person": "Taylor",
                "confirmation_status": "unconfirmed",
                "source_event_ids": ["event-2"],
            },
            confidence=0.9,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=primary_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=other_object["id"],
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
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "task_id": str(task_id),
            "project": "Project Phoenix",
            "person": "Alex",
            "query": "rollout",
            "since": "2026-03-29T09:30:00+00:00",
            "until": "2026-03-29T11:00:00+00:00",
            "limit": "20",
        },
    )

    assert status == 200
    assert payload["summary"] == {
        "query": "rollout",
        "filters": {
            "thread_id": str(thread_id),
            "task_id": str(task_id),
            "project": "Project Phoenix",
            "person": "Alex",
            "since": "2026-03-29T09:30:00+00:00",
            "until": "2026-03-29T11:00:00+00:00",
        },
        "limit": 20,
        "returned_count": 1,
        "total_count": 1,
        "order": ["relevance_desc", "created_at_desc", "id_desc"],
    }
    explanation = payload["items"][0]["explanation"]
    assert explanation["trust"]["provenance_posture"] == "strong"
    assert explanation["evidence_segments"][0]["source_kind"] == "continuity_capture_event"
    assert explanation["timestamps"]["created_at"] == "2026-03-29T10:00:00+00:00"
    assert payload["items"][0]["title"] == "Decision: Keep rollout phased"
    assert payload["items"][0]["confirmation_status"] == "confirmed"
    assert payload["items"][0]["admission_posture"] == "DERIVED"
    assert payload["items"][0]["provenance_references"] == [
        {"source_kind": "continuity_capture_event", "source_id": payload["items"][0]["capture_event_id"]},
        {"source_kind": "source_event", "source_id": "event-1"},
        {"source_kind": "task", "source_id": str(task_id)},
        {"source_kind": "thread", "source_id": str(thread_id)},
    ]
    assert payload["items"][0]["ordering"]["freshness_posture"] == "fresh"
    assert payload["items"][0]["ordering"]["provenance_posture"] == "strong"
    assert payload["items"][0]["ordering"]["supersession_posture"] == "current"

    intruder_status, intruder_payload = invoke_request(
        "GET",
        "/v0/continuity/recall",
        query_params={"user_id": str(intruder_id), "limit": "20"},
    )
    assert intruder_status == 200
    assert intruder_payload == {
        "items": [],
        "summary": {
            "query": None,
            "filters": {"since": None, "until": None},
            "limit": 20,
            "returned_count": 0,
            "total_count": 0,
            "order": ["relevance_desc", "created_at_desc", "id_desc"],
        },
    }


def test_continuity_recall_api_rejects_invalid_time_window(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner2@example.com")

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
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "since": "2026-03-29T11:00:00+00:00",
            "until": "2026-03-29T10:00:00+00:00",
            "limit": "20",
        },
    )

    assert status == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_continuity_recall_debug_api_persists_and_exposes_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="trace-owner@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep rollout phased",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep rollout phased",
            body={"decision_text": "Keep rollout phased"},
            provenance={"project": "Project Phoenix", "confirmation_status": "confirmed"},
            confidence=0.95,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=continuity_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
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
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "query": "rollout",
            "limit": "20",
            "debug": "true",
        },
    )

    assert status == 200
    assert payload["debug"]["candidate_count"] == 1
    assert payload["debug"]["candidates"][0]["stage_scores"]["lexical"]["matched"] is True
    retrieval_run_id = payload["debug"]["retrieval_run_id"]
    assert retrieval_run_id is not None

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/continuity/retrieval-runs",
        query_params={"user_id": str(user_id), "limit": "10"},
    )
    assert list_status == 200
    assert list_payload["items"][0]["id"] == retrieval_run_id

    trace_status, trace_payload = invoke_request(
        "GET",
        f"/v0/continuity/retrieval-runs/{retrieval_run_id}",
        query_params={"user_id": str(user_id)},
    )
    assert trace_status == 200
    assert trace_payload["retrieval_run"]["id"] == retrieval_run_id
    assert trace_payload["candidates"][0]["object_id"] == str(continuity_object["id"])


def test_continuity_resumption_debug_api_includes_underlying_retrieval_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="resume-trace@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep rollout phased",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        decision = store.create_continuity_object(
            capture_event_id=decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep rollout phased",
            body={"decision_text": "Keep rollout phased"},
            provenance={"thread_id": str(thread_id), "confirmation_status": "confirmed"},
            confidence=0.95,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
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
            "debug": "true",
        },
    )

    assert status == 200
    assert payload["brief"]["last_decision"]["item"]["id"] == str(decision["id"])
    assert payload["debug"]["retrieval"]["candidate_count"] >= 1


def test_continuity_recall_api_prefers_confirmed_fresh_active_truth_over_superseded_chain(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="freshness@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        current_capture = store.create_continuity_capture_event(
            raw_content="Decision: API timeout is 30s",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        current_object = store.create_continuity_object(
            capture_event_id=current_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: API timeout is 30s",
            body={"decision_text": "api timeout is 30 seconds"},
            provenance={"confirmation_status": "confirmed", "source_event_ids": ["event-current"]},
            confidence=0.62,
            last_confirmed_at=datetime(2026, 3, 29, 10, 30, tzinfo=UTC),
        )

        stale_capture = store.create_continuity_capture_event(
            raw_content="Decision: API timeout was 45s",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        stale_object = store.create_continuity_object(
            capture_event_id=stale_capture["id"],
            object_type="Decision",
            status="stale",
            title="Decision: API timeout was 45s",
            body={"decision_text": "api timeout is 45 seconds"},
            provenance={"confirmation_status": "confirmed"},
            confidence=0.99,
            last_confirmed_at=datetime(2026, 3, 20, 9, 30, tzinfo=UTC),
        )

        superseded_capture = store.create_continuity_capture_event(
            raw_content="Decision: API timeout was 60s",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        superseded_object = store.create_continuity_object(
            capture_event_id=superseded_capture["id"],
            object_type="Decision",
            status="superseded",
            title="Decision: API timeout was 60s",
            body={"decision_text": "api timeout is 60 seconds"},
            provenance={"confirmation_status": "confirmed"},
            confidence=1.0,
            last_confirmed_at=datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
            superseded_by_object_id=current_object["id"],
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=current_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=stale_object["id"],
        created_at=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=superseded_object["id"],
        created_at=datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
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
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "query": "api timeout",
            "limit": "20",
        },
    )

    assert status == 200
    assert [item["title"] for item in payload["items"]] == [
        "Decision: API timeout is 30s",
        "Decision: API timeout was 45s",
        "Decision: API timeout was 60s",
    ]
    assert payload["items"][0]["ordering"]["freshness_posture"] == "fresh"
    assert payload["items"][0]["ordering"]["supersession_posture"] == "current"
    assert payload["items"][-1]["ordering"]["supersession_posture"] == "superseded"


def test_continuity_recall_api_excludes_preserved_but_non_searchable_objects(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="nonsearchable@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        hidden_capture = store.create_continuity_capture_event(
            raw_content="Note: internal scratchpad",
            explicit_signal="note",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_note",
        )
        hidden_object = store.create_continuity_object(
            capture_event_id=hidden_capture["id"],
            object_type="Note",
            status="active",
            title="Note: internal scratchpad",
            body={"body": "internal scratchpad"},
            provenance={},
            confidence=1.0,
        )
        visible_capture = store.create_continuity_capture_event(
            raw_content="Decision: public outcome",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        visible_object = store.create_continuity_object(
            capture_event_id=visible_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: public outcome",
            body={"decision_text": "public outcome"},
            provenance={},
            confidence=1.0,
        )

    set_continuity_lifecycle_flags(
        migrated_database_urls["admin"],
        continuity_object_id=hidden_object["id"],
        is_searchable=False,
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=hidden_object["id"],
        created_at=datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=visible_object["id"],
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

    status, payload = invoke_request(
        "GET",
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "limit": "20",
        },
    )

    assert status == 200
    assert [item["title"] for item in payload["items"]] == ["Decision: public outcome"]


def test_continuity_lifecycle_debug_endpoints_expose_flags(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="lifecycle-debug@example.com")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Remember: searchable but not promotable",
            explicit_signal="remember_this",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_remember_this",
        )
        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type="MemoryFact",
            status="active",
            title="Memory Fact: searchable but not promotable",
            body={"fact_text": "searchable but not promotable"},
            provenance={},
            confidence=0.9,
        )

    set_continuity_lifecycle_flags(
        migrated_database_urls["admin"],
        continuity_object_id=continuity_object["id"],
        is_promotable=False,
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

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/admin/debug/continuity/lifecycle",
        query_params={"user_id": str(user_id), "limit": "20"},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/admin/debug/continuity/lifecycle/{continuity_object['id']}",
        query_params={"user_id": str(user_id)},
    )

    assert list_status == 200
    assert list_payload["summary"]["counts"]["preserved_count"] == 1
    assert list_payload["summary"]["counts"]["searchable_count"] == 1
    assert list_payload["summary"]["counts"]["promotable_count"] == 0
    assert list_payload["items"][0]["lifecycle"] == {
        "is_preserved": True,
        "preservation_status": "preserved",
        "is_searchable": True,
        "searchability_status": "searchable",
        "is_promotable": False,
        "promotion_status": "not_promotable",
    }

    assert detail_status == 200
    assert detail_payload["continuity_object"]["id"] == str(continuity_object["id"])
    assert detail_payload["continuity_object"]["lifecycle"]["is_promotable"] is False
