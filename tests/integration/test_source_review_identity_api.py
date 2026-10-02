"""Live HTTP regressions for source-review capture identity maintenance."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import (
    VNextCaptureService,
    capture_dedupe_key_for_text,
    content_hash_for_text,
)
from alicebot_api.vnext_store import PostgresVNextStore


def _invoke_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}
        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode({}).encode(),
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start["status"]), json.loads(body)


def _seed_user(database_url: str, *, label: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"source-review-{label}-{user_id}@example.invalid",
            f"Source review {label}",
        )
    return user_id


def test_source_review_http_rotates_identity_and_recapture_uses_new_envelope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    database_url = migrated_database_urls["app"]
    user_id = _seed_user(database_url, label="rotate")
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=database_url),
    )
    monkeypatch.setattr(
        vnext_memories_router,
        "get_settings",
        lambda: Settings(database_url=database_url),
    )
    text = "Fact: HTTP source review rotates the complete capture identity."
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        captured = VNextCaptureService(store).capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Alpha",),
        )
        source_id = str(captured.source_id)
        original = store.get_source(source_id)
        assert original is not None
        original_key = original["dedupe_key"]

    status, payload = _invoke_request(
        "POST",
        f"/v0/vnext/sources/{source_id}/review",
        payload={
            "user_id": str(user_id),
            "action": "assign_project",
            "project_id": "Beta",
            "domain": "professional",
            "sensitivity": "internal",
            "review_note": "Move to the reviewed envelope.",
        },
    )

    assert status == 200
    reviewed = payload["source"]
    assert reviewed["metadata_json"]["project_scope"] == ["Beta"]
    assert reviewed["domain"] == "professional"
    assert reviewed["sensitivity"] == "internal"
    assert reviewed["content_hash"] == content_hash_for_text(text, ("Beta",))
    assert reviewed["dedupe_key"] == capture_dedupe_key_for_text(
        text,
        ("Beta",),
        domain="professional",
        sensitivity="internal",
    )

    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_source_by_dedupe_key(str(original_key)) is None
        repeated = VNextCaptureService(store).capture_text(
            text,
            domain="professional",
            sensitivity="internal",
            project_scope=("Beta",),
        )
        assert repeated.status == "duplicate"
        assert repeated.source_id == source_id
        edges = store.list_edges(from_id=source_id)
        assert [(edge["edge_type"], edge["to_id"]) for edge in edges] == [("belongs_to_project", "Beta")]
        event_types = {
            str(event["event_type"]) for event in store.list_events(target_type="source", target_id=source_id)
        }
        assert {"source.updated", "source.assigned_project"}.issubset(event_types)


def test_source_review_http_collision_returns_409_after_full_transaction_rollback(
    migrated_database_urls,
    monkeypatch,
) -> None:
    database_url = migrated_database_urls["app"]
    user_id = _seed_user(database_url, label="collision")
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=database_url),
    )
    monkeypatch.setattr(
        vnext_memories_router,
        "get_settings",
        lambda: Settings(database_url=database_url),
    )
    text = "Fact: HTTP collision rollback leaves source evidence untouched."
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        service = VNextCaptureService(store)
        alpha = service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Alpha",),
        )
        beta = service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Beta",),
        )
        alpha_id = str(alpha.source_id)
        beta_id = str(beta.source_id)
        alpha_before = store.get_source(alpha_id)
        beta_before = store.get_source(beta_id)
        assert alpha_before is not None and beta_before is not None
        edges_before = store.list_edges(from_id=alpha_id)
        event_count_before = store.count_events(target_type="source", target_id=alpha_id)

    status, payload = _invoke_request(
        "POST",
        f"/v0/vnext/sources/{alpha_id}/review",
        payload={
            "user_id": str(user_id),
            "action": "assign_project",
            "project_id": "Beta",
            "review_note": "This conflicts with Beta's live source.",
        },
    )

    assert status == 409
    assert payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_source(alpha_id) == alpha_before
        assert store.get_source(beta_id) == beta_before
        assert store.list_edges(from_id=alpha_id) == edges_before
        assert store.count_events(target_type="source", target_id=alpha_id) == event_count_before
