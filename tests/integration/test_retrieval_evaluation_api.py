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


def test_retrieval_evaluation_api_returns_deterministic_fixture_precision_summary(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="retrieval-eval@example.com")

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
        "/v0/continuity/retrieval-evaluation",
        query_params={"user_id": str(user_id)},
    )

    assert status == 200
    assert payload["summary"]["fixture_count"] == 7
    assert payload["summary"]["evaluated_fixture_count"] == 7
    assert payload["summary"]["status"] == "pass"
    assert payload["summary"]["precision_at_k_mean"] >= payload["summary"]["precision_target"]
    assert payload["summary"]["precision_at_k_mean"] > payload["summary"]["baseline_precision_at_k_mean"]
    assert payload["summary"]["precision_at_k_lift"] > 0.0
    assert [fixture["fixture_id"] for fixture in payload["fixtures"]] == [
        "confirmed_fresh_truth_preferred",
        "provenance_breaks_tie",
        "supersession_chain_prefers_current_truth",
        "semantic_similarity_recovers_non_exact_query",
        "entity_signal_reduces_cross_entity_noise",
        "temporal_trust_supersession_prefers_current_valid_truth",
        "entity_edge_expansion_recovers_related_owner",
    ]
    assert payload["fixtures"][0]["top_result_ordering"]["freshness_posture"] == "fresh"
