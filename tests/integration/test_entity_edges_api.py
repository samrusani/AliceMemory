from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
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
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_user_with_source_memories(database_url: str, *, email: str) -> dict[str, object]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Entity edge source thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], session["id"], "message.user", {"text": "works on AliceBot"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "works with Neighborhood Cafe"})[
                "id"
            ],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "coffee preference"})["id"],
        ]

        first_memory = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.project.current",
                value={"name": "AliceBot"},
                source_event_ids=(event_ids[0],),
            ),
        )
        second_memory = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.merchant",
                value={"name": "Neighborhood Cafe"},
                source_event_ids=(event_ids[1],),
            ),
        )
        third_memory = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(event_ids[2],),
            ),
        )

    return {
        "user_id": user_id,
        "memory_ids": [
            UUID(first_memory.memory["id"]),
            UUID(second_memory.memory["id"]),
            UUID(third_memory.memory["id"]),
        ],
    }


def seed_entities(
    database_url: str,
    *,
    user_id: UUID,
    memory_ids: list[UUID],
) -> dict[str, UUID]:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        person = store.create_entity(
            entity_type="person",
            name="Alex",
            source_memory_ids=[str(memory_ids[2])],
        )
        merchant = store.create_entity(
            entity_type="merchant",
            name="Neighborhood Cafe",
            source_memory_ids=[str(memory_ids[1])],
        )
        project = store.create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[str(memory_ids[0])],
        )

    return {
        "person": person["id"],
        "merchant": merchant["id"],
        "project": project["id"],
    }


def test_create_entity_edge_endpoint_persists_user_scoped_edge_with_temporal_metadata(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    entities = seed_entities(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_ids=seeded["memory_ids"],
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

    status_code, payload = invoke_request(
        "POST",
        "/v0/entity-edges",
        payload={
            "user_id": str(seeded["user_id"]),
            "from_entity_id": str(entities["person"]),
            "to_entity_id": str(entities["project"]),
            "relationship_type": "works_on",
            "valid_from": "2026-03-12T10:00:00+00:00",
            "valid_to": "2026-03-12T12:00:00+00:00",
            "source_memory_ids": [str(seeded["memory_ids"][0]), str(seeded["memory_ids"][2])],
        },
    )

    assert status_code == 201
    assert payload["edge"]["from_entity_id"] == str(entities["person"])
    assert payload["edge"]["to_entity_id"] == str(entities["project"])
    assert payload["edge"]["relationship_type"] == "works_on"
    assert payload["edge"]["valid_from"] == "2026-03-12T10:00:00+00:00"
    assert payload["edge"]["valid_to"] == "2026-03-12T12:00:00+00:00"
    assert payload["edge"]["source_memory_ids"] == [
        str(seeded["memory_ids"][0]),
        str(seeded["memory_ids"][2]),
    ]

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored_edges = ContinuityStore(conn).list_entity_edges_for_entity(entities["person"])

    assert len(stored_edges) == 1
    assert stored_edges[0]["id"] == UUID(payload["edge"]["id"])
    assert stored_edges[0]["relationship_type"] == "works_on"
    assert stored_edges[0]["source_memory_ids"] == [
        str(seeded["memory_ids"][0]),
        str(seeded["memory_ids"][2]),
    ]


def test_entity_edge_list_endpoint_returns_incident_edges_in_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    entities = seed_entities(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_ids=seeded["memory_ids"],
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

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        created_edges = [
            store.create_entity_edge(
                from_entity_id=entities["person"],
                to_entity_id=entities["project"],
                relationship_type="works_on",
                valid_from=None,
                valid_to=None,
                source_memory_ids=[str(seeded["memory_ids"][0])],
            ),
            store.create_entity_edge(
                from_entity_id=entities["merchant"],
                to_entity_id=entities["project"],
                relationship_type="supplies",
                valid_from=None,
                valid_to=None,
                source_memory_ids=[str(seeded["memory_ids"][1])],
            ),
            store.create_entity_edge(
                from_entity_id=entities["project"],
                to_entity_id=entities["merchant"],
                relationship_type="references",
                valid_from=None,
                valid_to=None,
                source_memory_ids=[str(seeded["memory_ids"][2])],
            ),
        ]

    status_code, payload = invoke_request(
        "GET",
        f"/v0/entities/{entities['project']}/edges",
        query_params={"user_id": str(seeded["user_id"])},
    )

    expected_edges = sorted(created_edges, key=lambda edge: (edge["created_at"], edge["id"]))

    assert status_code == 200
    assert [item["id"] for item in payload["items"]] == [str(edge["id"]) for edge in expected_edges]
    assert payload["summary"] == {
        "entity_id": str(entities["project"]),
        "total_count": 3,
        "order": ["created_at_asc", "id_asc"],
    }


def test_entity_edge_endpoints_enforce_per_user_isolation_and_reference_validation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    owner_entities = seed_entities(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        memory_ids=owner["memory_ids"],
    )
    intruder = seed_user_with_source_memories(migrated_database_urls["app"], email="intruder@example.com")
    intruder_entities = seed_entities(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        memory_ids=intruder["memory_ids"],
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

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        ContinuityStore(conn).create_entity_edge(
            from_entity_id=owner_entities["person"],
            to_entity_id=owner_entities["project"],
            relationship_type="works_on",
            valid_from=None,
            valid_to=None,
            source_memory_ids=[str(owner["memory_ids"][0])],
        )

    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/entities/{owner_entities['project']}/edges",
        query_params={"user_id": str(intruder["user_id"])},
    )
    entity_status, entity_payload = invoke_request(
        "POST",
        "/v0/entity-edges",
        payload={
            "user_id": str(intruder["user_id"]),
            "from_entity_id": str(owner_entities["person"]),
            "to_entity_id": str(intruder_entities["project"]),
            "relationship_type": "works_on",
            "source_memory_ids": [str(intruder["memory_ids"][0])],
        },
    )
    memory_status, memory_payload = invoke_request(
        "POST",
        "/v0/entity-edges",
        payload={
            "user_id": str(intruder["user_id"]),
            "from_entity_id": str(intruder_entities["person"]),
            "to_entity_id": str(intruder_entities["project"]),
            "relationship_type": "works_on",
            "source_memory_ids": [str(owner["memory_ids"][0])],
        },
    )

    assert list_status == 404
    assert list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert entity_status == 400
    assert entity_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert memory_status == 400
    assert memory_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_create_entity_edge_endpoint_rejects_invalid_temporal_range(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    entities = seed_entities(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_ids=seeded["memory_ids"],
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

    status_code, payload = invoke_request(
        "POST",
        "/v0/entity-edges",
        payload={
            "user_id": str(seeded["user_id"]),
            "from_entity_id": str(entities["person"]),
            "to_entity_id": str(entities["project"]),
            "relationship_type": "works_on",
            "valid_from": "2026-03-12T12:00:00+00:00",
            "valid_to": "2026-03-12T10:00:00+00:00",
            "source_memory_ids": [str(seeded["memory_ids"][0])],
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
