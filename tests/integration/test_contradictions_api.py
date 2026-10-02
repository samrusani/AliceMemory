from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import alicebot_api.main as main_module
from alicebot_api.routers import continuity as continuity_router
import psycopg

from alicebot_api.config import Settings
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
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def identity_header(user_id: UUID | str) -> dict[str, str]:
    return {"x-alicebot-user-id": str(user_id)}


def set_continuity_timestamp(
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


def test_contradictions_api_detects_surfaces_penalties_and_resolves(
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
        email="contradictions-api@example.com",
    )
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        clean_capture = store.create_continuity_capture_event(
            raw_content="Decision: Release strategy phased",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        clean_object = store.create_continuity_object(
            capture_event_id=clean_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Release strategy phased",
            body={
                "fact_key": "release_strategy",
                "fact_value": "phased",
                "decision_text": "Release strategy phased",
            },
            provenance={"thread_id": str(thread_id)},
            confidence=0.95,
        )

        canary_capture = store.create_continuity_capture_event(
            raw_content="Decision: Release mode canary",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        canary_object = store.create_continuity_object(
            capture_event_id=canary_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Release mode canary",
            body={
                "fact_key": "release_mode",
                "fact_value": "canary",
                "decision_text": "Release mode canary",
            },
            provenance={"thread_id": str(thread_id)},
            confidence=0.95,
        )

        beta_capture = store.create_continuity_capture_event(
            raw_content="Decision: Release mode beta",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        beta_object = store.create_continuity_object(
            capture_event_id=beta_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Release mode beta",
            body={
                "fact_key": "release_mode",
                "fact_value": "beta",
                "decision_text": "Release mode beta",
            },
            provenance={"thread_id": str(thread_id)},
            confidence=0.95,
        )

    set_continuity_timestamp(
        migrated_database_urls["admin"],
        continuity_object_id=clean_object["id"],
        created_at=datetime(2026, 4, 14, 9, 0, tzinfo=UTC),
    )
    set_continuity_timestamp(
        migrated_database_urls["admin"],
        continuity_object_id=canary_object["id"],
        created_at=datetime(2026, 4, 14, 9, 5, tzinfo=UTC),
    )
    set_continuity_timestamp(
        migrated_database_urls["admin"],
        continuity_object_id=beta_object["id"],
        created_at=datetime(2026, 4, 14, 9, 10, tzinfo=UTC),
    )

    detect_status, detect_payload = invoke_request(
        "POST",
        "/v1/contradictions/detect",
        payload={"limit": 20},
        headers=identity_header(user_id),
    )
    assert detect_status == 200
    assert detect_payload["summary"]["open_case_count"] == 1
    assert detect_payload["summary"]["updated_case_count"] == 1
    assert detect_payload["items"][0]["kind"] == "direct_fact_conflict"
    contradiction_case_id = detect_payload["items"][0]["id"]

    explain_status, explain_payload = invoke_request(
        "GET",
        f"/v0/continuity/explain/{canary_object['id']}",
        query_params={"user_id": str(user_id)},
    )
    assert explain_status == 200
    contradiction_summary = explain_payload["explain"]["explanation"]["contradictions"]
    assert contradiction_summary["open_case_count"] == 1
    assert contradiction_summary["resolved_case_count"] == 0
    assert contradiction_summary["penalty_score"] == 2.0

    recall_status, recall_payload = invoke_request(
        "GET",
        "/v0/continuity/recall",
        query_params={
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "query": "release",
            "limit": "20",
        },
    )
    assert recall_status == 200
    assert recall_payload["items"][0]["id"] == str(clean_object["id"])
    contradicted_items = [item for item in recall_payload["items"] if item["ordering"]["open_contradiction_count"] == 1]
    assert len(contradicted_items) == 2
    assert {item["ordering"]["contradiction_penalty_score"] for item in contradicted_items} == {2.0}

    trust_status, trust_payload = invoke_request(
        "GET",
        "/v1/trust/signals",
        query_params={
            "continuity_object_id": str(canary_object["id"]),
            "signal_state": "active",
            "limit": "20",
        },
        headers=identity_header(user_id),
    )
    assert trust_status == 200
    assert trust_payload["summary"]["returned_count"] == 1
    assert trust_payload["items"][0]["signal_type"] == "contradiction"
    assert trust_payload["items"][0]["signal_state"] == "active"
    assert trust_payload["items"][0]["contradiction_case_id"] == contradiction_case_id

    resolve_status, resolve_payload = invoke_request(
        "POST",
        f"/v1/contradictions/cases/{contradiction_case_id}/resolve",
        payload={
            "action": "confirm_primary",
            "note": "Primary record remains current.",
        },
        headers=identity_header(user_id),
    )
    assert resolve_status == 200
    assert resolve_payload["contradiction_case"]["status"] == "resolved"
    assert resolve_payload["contradiction_case"]["resolution_action"] == "confirm_primary"
    assert resolve_payload["contradiction_case"]["resolution_note"] == "Primary record remains current."

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v1/contradictions/cases/{contradiction_case_id}",
        headers=identity_header(user_id),
    )
    assert detail_status == 200
    assert detail_payload["contradiction_case"]["status"] == "resolved"

    active_trust_after_status, active_trust_after_payload = invoke_request(
        "GET",
        "/v1/trust/signals",
        query_params={
            "continuity_object_id": str(canary_object["id"]),
            "signal_state": "active",
            "limit": "20",
        },
        headers=identity_header(user_id),
    )
    assert active_trust_after_status == 200
    assert active_trust_after_payload["items"] == []


def test_contradictions_api_requires_local_identity(
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

    status, payload = invoke_request(
        "GET",
        "/v1/contradictions/cases",
        query_params={"status": "open", "limit": "20"},
    )

    assert status == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
