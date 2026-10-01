from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.routers import continuity as continuity_router
import scripts.run_phase6_quality_evidence as quality_evidence
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
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start_message["status"], json.loads(body)


def seed_quality_gate_state(
    database_url: str,
    *,
    correct_count: int,
    incorrect_count: int,
    add_unlabeled: bool,
    add_high_risk: bool,
    add_stale_truth: bool,
    add_outdated_conflict: bool,
) -> str:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, f"quality-{user_id}@example.com", "Quality Reviewer")
        thread = store.create_thread("Memory quality gate state")
        session = store.create_session(thread["id"], status="active")

        memory_ids: list[UUID] = []
        total_base = max(10, correct_count + incorrect_count)
        for index in range(total_base):
            event_id = store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": f"base memory {index}"},
            )["id"]
            decision = admit_memory_candidate(
                store,
                user_id=user_id,
                candidate=MemoryCandidateInput(
                    memory_key=f"user.quality.base_{index}",
                    value={"index": index},
                    source_event_ids=(event_id,),
                    confirmation_status="confirmed",
                    confidence=0.95,
                ),
            )
            assert decision.memory is not None
            memory_ids.append(UUID(decision.memory["id"]))

        cursor = 0
        for _ in range(correct_count):
            store.create_memory_review_label(
                memory_id=memory_ids[cursor],
                label="correct",
                note="Correct.",
            )
            cursor += 1
        for _ in range(incorrect_count):
            store.create_memory_review_label(
                memory_id=memory_ids[cursor],
                label="incorrect",
                note="Incorrect.",
            )
            cursor += 1

        if add_outdated_conflict:
            store.create_memory_review_label(
                memory_id=memory_ids[0],
                label="outdated",
                note="Superseded active conflict.",
            )

        if add_unlabeled:
            event_id = store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": "unlabeled memory"},
            )["id"]
            admit_memory_candidate(
                store,
                user_id=user_id,
                candidate=MemoryCandidateInput(
                    memory_key="user.quality.unlabeled",
                    value={"state": "unlabeled"},
                    source_event_ids=(event_id,),
                    confirmation_status="confirmed",
                    confidence=0.95,
                ),
            )

        if add_high_risk:
            event_id = store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": "high risk memory"},
            )["id"]
            admit_memory_candidate(
                store,
                user_id=user_id,
                candidate=MemoryCandidateInput(
                    memory_key="user.quality.high_risk",
                    value={"state": "high_risk"},
                    source_event_ids=(event_id,),
                    confirmation_status="unconfirmed",
                    confidence=0.2,
                ),
            )

        if add_stale_truth:
            event_id = store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": "stale truth memory"},
            )["id"]
            admit_memory_candidate(
                store,
                user_id=user_id,
                candidate=MemoryCandidateInput(
                    memory_key="user.quality.stale_truth",
                    value={"state": "stale"},
                    source_event_ids=(event_id,),
                    confirmation_status="contested",
                    confidence=0.95,
                    valid_to=datetime(2026, 3, 1, tzinfo=UTC),
                ),
            )

    return str(user_id)


def test_memory_quality_gate_endpoint_returns_canonical_status_transitions(
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

    insufficient_user = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=5,
        incorrect_count=0,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
    )
    degraded_precision_user = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=7,
        incorrect_count=3,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
    )
    degraded_conflict_user = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=True,
    )
    needs_review_user = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=True,
        add_high_risk=True,
        add_stale_truth=False,
        add_outdated_conflict=False,
    )
    healthy_user = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
    )

    insufficient_status, insufficient_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": insufficient_user},
    )
    degraded_precision_status, degraded_precision_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": degraded_precision_user},
    )
    degraded_conflict_status, degraded_conflict_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": degraded_conflict_user},
    )
    needs_review_status, needs_review_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": needs_review_user},
    )
    healthy_status, healthy_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": healthy_user},
    )

    assert insufficient_status == 200
    assert degraded_precision_status == 200
    assert degraded_conflict_status == 200
    assert needs_review_status == 200
    assert healthy_status == 200

    assert insufficient_payload["summary"]["status"] == "insufficient_sample"
    assert degraded_precision_payload["summary"]["status"] == "degraded"
    assert degraded_conflict_payload["summary"]["status"] == "degraded"
    assert needs_review_payload["summary"]["status"] == "needs_review"
    assert healthy_payload["summary"]["status"] == "healthy"
    assert degraded_conflict_payload["summary"]["superseded_active_conflict_count"] > 0
    assert needs_review_payload["summary"]["high_risk_memory_count"] > 0
    assert healthy_payload["summary"]["unlabeled_memory_count"] == 0
    assert healthy_payload["summary"]["counts"]["adjudicated_correct_count"] == 10

    for payload in (
        insufficient_payload,
        degraded_precision_payload,
        degraded_conflict_payload,
        needs_review_payload,
        healthy_payload,
    ):
        assert payload["summary"]["status"] in {
            "healthy",
            "needs_review",
            "insufficient_sample",
            "degraded",
        }
        assert "precision_target" in payload["summary"]
        assert "minimum_adjudicated_sample" in payload["summary"]
        assert "counts" in payload["summary"]


def test_memory_quality_gate_endpoint_is_deterministic_for_fixed_state(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
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

    first_status, first_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": user_id},
    )
    second_status, second_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": user_id},
    )

    assert first_status == 200
    assert second_status == 200
    assert first_payload == second_payload


def test_memory_quality_gate_endpoint_enforces_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner_id = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=False,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
    )
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

    owner_status, owner_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": owner_id},
    )
    intruder_status, intruder_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": str(intruder_id)},
    )

    assert owner_status == 200
    assert intruder_status == 200
    assert owner_payload["summary"]["counts"]["active_memory_count"] > 0
    assert intruder_payload == {
        "summary": {
            "status": "insufficient_sample",
            "precision": None,
            "precision_target": 0.8,
            "adjudicated_sample_count": 0,
            "minimum_adjudicated_sample": 10,
            "remaining_to_minimum_sample": 10,
            "unlabeled_memory_count": 0,
            "high_risk_memory_count": 0,
            "stale_truth_count": 0,
            "superseded_active_conflict_count": 0,
            "counts": {
                "active_memory_count": 0,
                "labeled_active_memory_count": 0,
                "adjudicated_correct_count": 0,
                "adjudicated_incorrect_count": 0,
                "outdated_label_count": 0,
                "insufficient_evidence_label_count": 0,
            },
        }
    }


def test_memory_trust_dashboard_endpoint_aggregates_canonical_quality_inputs(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=True,
        add_high_risk=True,
        add_stale_truth=True,
        add_outdated_conflict=False,
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
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    dashboard_status, dashboard_payload = invoke_request(
        "GET",
        "/v0/memories/trust-dashboard",
        query_params={"user_id": user_id},
    )
    gate_status, gate_payload = invoke_request(
        "GET",
        "/v0/memories/quality-gate",
        query_params={"user_id": user_id},
    )
    retrieval_status, retrieval_payload = invoke_request(
        "GET",
        "/v0/continuity/retrieval-evaluation",
        query_params={"user_id": user_id},
    )

    assert dashboard_status == 200
    assert gate_status == 200
    assert retrieval_status == 200
    assert dashboard_payload["dashboard"]["quality_gate"] == gate_payload["summary"]
    assert dashboard_payload["dashboard"]["queue_posture"]["total_count"] == gate_payload["summary"][
        "unlabeled_memory_count"
    ]
    assert dashboard_payload["dashboard"]["retrieval_quality"] == retrieval_payload["summary"]
    assert dashboard_payload["dashboard"]["correction_freshness"] == {
        "total_open_loop_count": 0,
        "stale_open_loop_count": 0,
        "correction_recurrence_count": 0,
        "freshness_drift_count": 0,
    }
    assert dashboard_payload["dashboard"]["recommended_review"]["priority_mode"] == "high_risk_first"
    assert dashboard_payload["dashboard"]["recommended_review"]["action"] == "review_high_risk_queue"


def test_memory_trust_dashboard_endpoint_is_deterministic_for_fixed_state(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=True,
        add_high_risk=False,
        add_stale_truth=False,
        add_outdated_conflict=False,
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

    first_status, first_payload = invoke_request(
        "GET",
        "/v0/memories/trust-dashboard",
        query_params={"user_id": user_id},
    )
    second_status, second_payload = invoke_request(
        "GET",
        "/v0/memories/trust-dashboard",
        query_params={"user_id": user_id},
    )

    assert first_status == 200
    assert second_status == 200
    assert first_payload == second_payload


def test_phase6_quality_evidence_script_writes_deterministic_artifact_matching_dashboard(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    user_id = seed_quality_gate_state(
        migrated_database_urls["app"],
        correct_count=10,
        incorrect_count=0,
        add_unlabeled=True,
        add_high_risk=True,
        add_stale_truth=False,
        add_outdated_conflict=False,
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
    monkeypatch.setattr(
        quality_evidence,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    dashboard_status, dashboard_payload = invoke_request(
        "GET",
        "/v0/memories/trust-dashboard",
        query_params={"user_id": user_id},
    )
    assert dashboard_status == 200

    artifact_path = tmp_path / "phase6_quality_evidence.json"
    exit_code = quality_evidence.main(
        ["--user-id", user_id, "--output", str(artifact_path)],
    )
    assert exit_code == 0
    assert artifact_path.exists()

    written_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert written_payload["artifact_version"] == quality_evidence.ARTIFACT_VERSION
    assert written_payload["artifact_path"] == str(artifact_path)
    assert written_payload["user_id"] == user_id
    assert written_payload["dashboard"] == dashboard_payload["dashboard"]
