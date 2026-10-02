from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
from alicebot_api.routers import workspaces as workspaces_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore


REPO_ROOT = Path(__file__).resolve().parents[2]


def _invoke_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query_params: dict[str, str] | None = None,
    authorization: str | None = None,
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

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query_params or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


def _seed_user(database_url: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"review-dashboard-{uuid4().hex[:12]}@example.invalid",
            "Review Dashboard Demo",
        )
    return user_id


def test_review_dashboard_demo_routes_keyed_browser_review_to_vnext() -> None:
    guide = (REPO_ROOT / "docs/alpha/review-dashboard-demo.md").read_text(encoding="utf-8")

    assert "With an active key, open `/vnext`" in guide
    assert "Only in the zero-key local/demo posture may `/memories` and `/traces`" in guide
    assert "cannot forward" in guide
    assert "browser-memory Bearer" in guide
    assert 'auth_args=(-H "Authorization: Bearer ${ALICE_AGENT_API_KEY}")' not in guide
    assert 'chmod 600 "$auth_config"' in guide
    assert 'auth_args=(--config "$auth_config")' in guide


@pytest.mark.parametrize("auth_mode", ["keyless", "unbound_admin"])
def test_public_review_dashboard_demo_keeps_trace_truth_and_redaction_boundary(
    migrated_database_urls,
    monkeypatch,
    auth_mode: str,
) -> None:
    """Exercise the documented API path without claiming a browser redaction control."""

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    settings = Settings(database_url=migrated_database_urls["app"])
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_retrieval_router, "get_settings", lambda: settings)
    monkeypatch.setattr(workspaces_router, "get_settings", lambda: settings)

    user_id = _seed_user(migrated_database_urls["app"])
    user_id_text = str(user_id)
    authorization: str | None = None
    if auth_mode == "unbound_admin":
        with user_connection(migrated_database_urls["app"], user_id) as conn:
            _key_record, raw_key = create_agent_key(
                PostgresVNextStore(conn),
                user_id=user_id,
                agent_id="review-dashboard-operator",
                permission_profile="admin_agent",
                label="Review dashboard integration",
            )
        authorization = f"Bearer {raw_key}"

    raw_sentinel = f"DASHBOARD-DEMO-RAW-{uuid4().hex}"
    source_status, source_payload = _invoke_request(
        "POST",
        "/v0/vnext/sources",
        payload={
            "user_id": user_id_text,
            "raw_text": f"Decision: {raw_sentinel} is accepted only after operator review.",
            "title": "Review dashboard public-API demo",
            "domain": "professional",
            "sensitivity": "private",
        },
        authorization=authorization,
    )
    assert source_status == 201
    assert source_payload["candidate_memory_count"] == 1
    source_id = source_payload["source_id"]

    source_review_status, source_review_payload = _invoke_request(
        "POST",
        f"/v0/vnext/sources/{source_id}/review",
        payload={
            "user_id": user_id_text,
            "action": "review",
            "review_note": "Reviewed in the scripted enterprise demo.",
        },
        authorization=authorization,
    )
    assert source_review_status == 200
    source_trace = source_review_payload["trace"]
    assert source_trace["trace_kind"] == "capture_to_brief"
    assert source_trace["summary"]["source_id"] == source_id
    assert source_trace["sampling"]["trace_complete"] is True
    assert len(source_trace["candidate_memories"]) == 1
    memory_id = source_trace["candidate_memories"][0]["id"]

    accept_status, accept_payload = _invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": user_id_text, "action": "accept"},
        authorization=authorization,
    )
    assert accept_status == 200
    assert accept_payload["memory"]["status"] == "active"

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        source_before_trace = store.get_source(source_id)
        source_events_before_trace = store.list_events(
            target_type="source",
            target_id=source_id,
            limit=500,
        )
    assert source_before_trace is not None

    trace_status, trace = _invoke_request(
        "GET",
        f"/v0/vnext/traces/sources/{source_id}",
        query_params={"user_id": user_id_text},
        authorization=authorization,
    )
    assert trace_status == 200
    traced_memory = next(item for item in trace["candidate_memories"] if item["id"] == memory_id)
    assert traced_memory["status"] == "active"
    assert trace["summary"]["candidate_memory_count"] == 1
    assert any(event["event_type"] == "review.item_accepted" for event in trace["events"])

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        source_after_trace = store.get_source(source_id)
        source_events_after_trace = store.list_events(
            target_type="source",
            target_id=source_id,
            limit=500,
        )
    assert source_after_trace is not None
    assert source_after_trace == source_before_trace
    assert source_after_trace["metadata_json"] == source_before_trace["metadata_json"]
    assert source_events_after_trace == source_events_before_trace

    redact_status, redact_payload = _invoke_request(
        "POST",
        "/v0/vnext/memories/redact",
        payload={
            "user_id": user_id_text,
            "memory_id": memory_id,
            "reason": "Scripted enterprise demo cleanup.",
        },
        authorization=authorization,
    )
    assert redact_status == 200
    assert redact_payload["status"] == "redacted"
    assert redact_payload["forgotten_first"] is True

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        redacted_memory = store.get_memory_for_redaction(memory_id)
        revisions = store.list_revisions(memory_id)
        events = store.list_events(
            target_type="memory",
            target_id=memory_id,
            limit=500,
        )
        source = store.get_source(source_id)

    assert redacted_memory is not None
    assert str(redacted_memory["id"]) == memory_id
    assert redacted_memory["status"] == "archived"
    assert redacted_memory["canonical_text"] == "[REDACTED]"
    assert redacted_memory["metadata_json"]["redacted"] is True
    assert revisions
    assert any(event["event_type"] == "memory.redacted" for event in events)
    governed_memory_graph = json.dumps(
        {"memory": redacted_memory, "revisions": revisions, "events": events},
        default=str,
        sort_keys=True,
    )
    assert raw_sentinel not in governed_memory_graph

    # True memory redaction intentionally preserves separately governed source
    # evidence. The demo and its test state this boundary instead of claiming a
    # global erasure that the public API does not perform.
    assert source is not None
    assert str(source["id"]) == source_id
