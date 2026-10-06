from __future__ import annotations

from datetime import datetime, timedelta
import json
import time
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import continuity as continuity_router
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


def _set_temporal_timestamps(
    admin_database_url: str,
    *,
    entity_id: UUID,
    edge_id: UUID,
    entity_created_at: datetime,
    edge_created_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE entities SET created_at = %s WHERE id = %s",
                (entity_created_at, entity_id),
            )
            cur.execute(
                "UPDATE entity_edges SET created_at = %s, valid_from = %s WHERE id = %s",
                (edge_created_at, edge_created_at, edge_id),
            )


def seed_temporal_entity_graph(database_url: str, admin_database_url: str) -> dict[str, object]:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "temporal-test@example.invalid", "Temporal Test")
        thread = store.create_thread("Temporal state thread")
        session = store.create_session(thread["id"], status="active")
        first_event = store.append_event(thread["id"], session["id"], "message.user", {"text": "AliceBot v1"})["id"]
        second_event = store.append_event(thread["id"], session["id"], "message.user", {"text": "AliceBot v2"})["id"]

        add_result = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.project.current",
                value={"name": "AliceBot v1"},
                source_event_ids=(first_event,),
                confidence=0.91,
                confirmation_status="confirmed",
                trust_class="human_curated",
                trust_reason="initial capture",
            ),
        )
    time.sleep(0.1)
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        update_result = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.project.current",
                value={"name": "AliceBot v2"},
                source_event_ids=(second_event,),
                confidence=0.98,
                trust_reason="confirmed by owner",
            ),
        )
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        entity = store.create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[add_result.memory["id"]],
        )
        person = store.create_entity(
            entity_type="person",
            name="Alex",
            source_memory_ids=[add_result.memory["id"]],
        )
        edge = store.create_entity_edge(
            from_entity_id=person["id"],
            to_entity_id=entity["id"],
            relationship_type="works_on",
            valid_from=datetime.fromisoformat("2026-03-12T09:30:00+00:00"),
            valid_to=None,
            source_memory_ids=[add_result.memory["id"]],
        )

    add_at = datetime.fromisoformat(add_result.revision["created_at"])
    update_at = datetime.fromisoformat(update_result.revision["created_at"])
    midpoint = add_at + (update_at - add_at) / 2
    _set_temporal_timestamps(
        admin_database_url,
        entity_id=entity["id"],
        edge_id=edge["id"],
        entity_created_at=add_at - timedelta(seconds=1),
        edge_created_at=midpoint,
    )
    return {
        "user_id": user_id,
        "entity_id": entity["id"],
        "first_event_id": str(first_event),
        "historical_at": midpoint.isoformat(),
        "current_at": (update_at + timedelta(seconds=1)).isoformat(),
    }


def test_temporal_state_endpoint_distinguishes_historical_and_current_fact_values(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_temporal_entity_graph(
        migrated_database_urls["app"],
        migrated_database_urls["admin"],
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

    historical_status, historical_payload = invoke_request(
        "GET",
        "/v0/state-at",
        query_params={
            "user_id": str(seeded["user_id"]),
            "entity_id": str(seeded["entity_id"]),
            "at": seeded["historical_at"],
        },
    )
    current_status, current_payload = invoke_request(
        "GET",
        "/v0/state-at",
        query_params={
            "user_id": str(seeded["user_id"]),
            "entity_id": str(seeded["entity_id"]),
            "at": seeded["current_at"],
        },
    )

    assert historical_status == 200
    assert current_status == 200
    assert historical_payload["state_at"]["facts"][0]["value"] == {"name": "AliceBot v1"}
    assert current_payload["state_at"]["facts"][0]["value"] == {"name": "AliceBot v2"}
    assert historical_payload["state_at"]["edges"][0]["relationship_type"] == "works_on"


def test_temporal_timeline_and_explain_endpoints_return_chronology_and_explainability(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_temporal_entity_graph(
        migrated_database_urls["app"],
        migrated_database_urls["admin"],
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

    timeline_status, timeline_payload = invoke_request(
        "GET",
        "/v0/timeline",
        query_params={
            "user_id": str(seeded["user_id"]),
            "entity_id": str(seeded["entity_id"]),
            "limit": "10",
        },
    )
    explain_status, explain_payload = invoke_request(
        "GET",
        "/v0/explain",
        query_params={
            "user_id": str(seeded["user_id"]),
            "entity_id": str(seeded["entity_id"]),
            "at": seeded["historical_at"],
        },
    )

    assert timeline_status == 200
    assert [event["event_type"] for event in timeline_payload["timeline"]["events"]] == [
        "entity_created",
        "fact_add",
        "edge_recorded",
        "fact_update",
    ]
    assert explain_status == 200
    fact_explain = explain_payload["explain"]["facts"][0]
    assert fact_explain["trust"]["trust_class"] == "human_curated"
    assert fact_explain["provenance"]["source_event_ids"] == [seeded["first_event_id"]]
    assert [item["sequence_no"] for item in fact_explain["supersession_chain"]] == [1, 2]
