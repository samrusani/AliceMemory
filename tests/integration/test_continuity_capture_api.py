from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

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
        "headers": [(b"content-type", b"application/json")],
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


def test_continuity_capture_create_list_and_detail_support_deterministic_signal_mapping(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="owner@example.com")
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(user_id),
            "raw_content": "Finalize launch checklist",
            "explicit_signal": "task",
        },
    )

    assert create_status == 201
    capture_id = create_payload["capture"]["capture_event"]["id"]
    assert create_payload["capture"]["capture_event"] == {
        "id": capture_id,
        "raw_content": "Finalize launch checklist",
        "explicit_signal": "task",
        "admission_posture": "DERIVED",
        "admission_reason": "explicit_signal_task",
        "created_at": create_payload["capture"]["capture_event"]["created_at"],
    }
    assert create_payload["capture"]["derived_object"]["object_type"] == "NextAction"
    assert create_payload["capture"]["derived_object"]["body"] == {
        "action_text": "Finalize launch checklist",
        "raw_content": "Finalize launch checklist",
        "explicit_signal": "task",
    }
    assert create_payload["capture"]["derived_object"]["provenance"]["capture_event_id"] == capture_id

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/continuity/captures",
        query_params={
            "user_id": str(user_id),
            "limit": "20",
        },
    )
    assert list_status == 200
    assert list_payload["summary"] == {
        "limit": 20,
        "returned_count": 1,
        "total_count": 1,
        "derived_count": 1,
        "triage_count": 0,
        "order": ["created_at_desc", "id_desc"],
    }
    assert list_payload["items"][0]["capture_event"]["id"] == capture_id

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/continuity/captures/{capture_id}",
        query_params={"user_id": str(user_id)},
    )
    assert detail_status == 200
    assert detail_payload["capture"]["capture_event"]["id"] == capture_id
    assert detail_payload["capture"]["derived_object"]["object_type"] == "NextAction"


def test_continuity_capture_ambiguous_input_is_preserved_with_triage_posture(
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(user_id),
            "raw_content": "Maybe revisit this next month",
        },
    )

    assert create_status == 201
    assert create_payload["capture"]["capture_event"]["admission_posture"] == "TRIAGE"
    assert create_payload["capture"]["capture_event"]["admission_reason"] == "ambiguous_capture_requires_triage"
    assert create_payload["capture"]["derived_object"] is None

    capture_id = create_payload["capture"]["capture_event"]["id"]
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/continuity/captures/{capture_id}",
        query_params={"user_id": str(user_id)},
    )
    assert detail_status == 200
    assert detail_payload["capture"]["derived_object"] is None


def test_continuity_capture_rejects_invalid_signal_and_enforces_user_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner_id = seed_user(migrated_database_urls["app"], email="owner3@example.com")
    intruder_id = seed_user(migrated_database_urls["app"], email="intruder@example.com")
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

    invalid_status, invalid_payload = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(owner_id),
            "raw_content": "Call the supplier",
            "explicit_signal": "invalid_signal",
        },
    )
    assert invalid_status == 422
    assert invalid_payload["detail"][0]["loc"] == ["body", "explicit_signal"]
    assert invalid_payload["detail"][0]["type"] == "literal_error"

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(owner_id),
            "raw_content": "Decision: keep intake conservative",
        },
    )
    assert create_status == 201

    intruder_detail_status, intruder_detail_payload = invoke_request(
        "GET",
        f"/v0/continuity/captures/{create_payload['capture']['capture_event']['id']}",
        query_params={"user_id": str(intruder_id)},
    )
    assert intruder_detail_status == 404
    assert intruder_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    intruder_list_status, intruder_list_payload = invoke_request(
        "GET",
        "/v0/continuity/captures",
        query_params={"user_id": str(intruder_id), "limit": "20"},
    )
    assert intruder_list_status == 200
    assert intruder_list_payload == {
        "items": [],
        "summary": {
            "limit": 20,
            "returned_count": 0,
            "total_count": 0,
            "derived_count": 0,
            "triage_count": 0,
            "order": ["created_at_desc", "id_desc"],
        },
    }


def test_continuity_capture_candidate_and_commit_pipeline_supports_assist_mode_autosave(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="pipeline-assist@example.com")
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

    candidates_status, candidates_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/candidates",
        payload={
            "user_id": str(user_id),
            "user_content": "Decision: keep provider-plus-MCP architecture",
            "assistant_content": "Correction: default bridge mode is assist",
        },
    )
    assert candidates_status == 200
    assert candidates_payload["summary"]["candidate_count"] == 2
    assert {item["candidate_type"] for item in candidates_payload["candidates"]} == {"decision", "correction"}

    commit_status, commit_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "sync_fingerprint": "sync-assist-pipeline-001",
            "candidates": candidates_payload["candidates"],
        },
    )
    assert commit_status == 200
    assert commit_payload["summary"] == {
        "mode": "assist",
        "candidate_count": 2,
        "auto_saved_count": 1,
        "review_queued_count": 1,
        "noop_count": 0,
        "duplicate_noop_count": 0,
        "auto_saved_types": ["decision"],
        "review_queued_types": ["correction"],
    }
    by_type = {item["candidate_type"]: item for item in commit_payload["commits"]}
    assert by_type["decision"]["decision"] == "auto_saved"
    assert by_type["correction"]["decision"] == "queued_for_review"


def test_continuity_capture_pipeline_routes_disallowed_or_low_confidence_candidates_to_review_queue(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="pipeline-review@example.com")
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

    candidates_status, candidates_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/candidates",
        payload={
            "user_id": str(user_id),
            "user_content": "Note: broad recap for later refinement",
            "assistant_content": "",
        },
    )
    assert candidates_status == 200
    assert candidates_payload["candidates"][0]["candidate_type"] == "note"

    commit_status, commit_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "sync_fingerprint": "sync-review-pipeline-001",
            "candidates": candidates_payload["candidates"],
        },
    )
    assert commit_status == 200
    assert commit_payload["summary"]["auto_saved_count"] == 0
    assert commit_payload["summary"]["review_queued_count"] == 1
    assert commit_payload["summary"]["review_queued_types"] == ["note"]
    assert commit_payload["commits"][0]["decision"] == "queued_for_review"
    assert commit_payload["commits"][0]["continuity_object"]["status"] == "stale"

    review_status, review_payload = invoke_request(
        "GET",
        "/v0/continuity/review-queue",
        query_params={"user_id": str(user_id), "status": "stale", "limit": "20"},
    )
    assert review_status == 200
    assert review_payload["summary"]["returned_count"] == 1
    assert review_payload["items"][0]["status"] == "stale"


def test_continuity_capture_pipeline_noop_and_repeated_sync_are_write_safe(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="pipeline-noop@example.com")
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

    noop_candidates_status, noop_candidates_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/candidates",
        payload={
            "user_id": str(user_id),
            "user_content": "thanks",
            "assistant_content": "ok",
        },
    )
    assert noop_candidates_status == 200
    assert noop_candidates_payload["summary"]["no_op_count"] == 1

    noop_commit_status, noop_commit_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "sync_fingerprint": "sync-noop-001",
            "candidates": noop_candidates_payload["candidates"],
        },
    )
    assert noop_commit_status == 200
    assert noop_commit_payload["summary"]["noop_count"] == 1
    assert noop_commit_payload["summary"]["auto_saved_count"] == 0
    assert noop_commit_payload["summary"]["review_queued_count"] == 0

    list_status_before, list_payload_before = invoke_request(
        "GET",
        "/v0/continuity/captures",
        query_params={"user_id": str(user_id), "limit": "20"},
    )
    assert list_status_before == 200
    assert list_payload_before["summary"]["total_count"] == 0

    candidates_status, candidates_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/candidates",
        payload={
            "user_id": str(user_id),
            "user_content": "Decision: ship deterministic commit policy",
            "assistant_content": "",
        },
    )
    assert candidates_status == 200

    first_commit_status, first_commit_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "sync_fingerprint": "sync-repeat-001",
            "candidates": candidates_payload["candidates"],
        },
    )
    assert first_commit_status == 200
    assert first_commit_payload["summary"]["auto_saved_count"] == 1

    second_commit_status, second_commit_payload = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "sync_fingerprint": "sync-repeat-001",
            "candidates": candidates_payload["candidates"],
        },
    )
    assert second_commit_status == 200
    assert second_commit_payload["summary"]["auto_saved_count"] == 0
    assert second_commit_payload["summary"]["duplicate_noop_count"] == 1

    list_status_after, list_payload_after = invoke_request(
        "GET",
        "/v0/continuity/captures",
        query_params={"user_id": str(user_id), "limit": "20"},
    )
    assert list_status_after == 200
    assert list_payload_after["summary"]["total_count"] == 1
