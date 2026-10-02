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
        thread = store.create_thread("Entity source thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], session["id"], "message.user", {"text": "works on AliceBot"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "drinks oat milk"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "shops at cafe"})["id"],
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
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(event_ids[1],),
            ),
        )
        third_memory = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.merchant",
                value={"name": "Neighborhood Cafe"},
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


def test_create_entity_endpoint_persists_entity_backed_by_user_owned_source_memories(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
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
        "/v0/entities",
        payload={
            "user_id": str(seeded["user_id"]),
            "entity_type": "project",
            "name": "AliceBot",
            "source_memory_ids": [str(seeded["memory_ids"][0]), str(seeded["memory_ids"][1])],
        },
    )

    assert status_code == 201
    assert payload["entity"]["entity_type"] == "project"
    assert payload["entity"]["name"] == "AliceBot"
    assert payload["entity"]["source_memory_ids"] == [
        str(seeded["memory_ids"][0]),
        str(seeded["memory_ids"][1]),
    ]

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored_entities = ContinuityStore(conn).list_entities()

    assert len(stored_entities) == 1
    assert stored_entities[0]["id"] == UUID(payload["entity"]["id"])
    assert stored_entities[0]["entity_type"] == "project"
    assert stored_entities[0]["name"] == "AliceBot"
    assert stored_entities[0]["source_memory_ids"] == [
        str(seeded["memory_ids"][0]),
        str(seeded["memory_ids"][1]),
    ]


def test_entity_endpoints_list_and_get_entities_in_deterministic_user_scoped_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
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
        created_entities = [
            store.create_entity(
                entity_type="person",
                name="Alex",
                source_memory_ids=[str(seeded["memory_ids"][0])],
            ),
            store.create_entity(
                entity_type="merchant",
                name="Neighborhood Cafe",
                source_memory_ids=[str(seeded["memory_ids"][2])],
            ),
            store.create_entity(
                entity_type="project",
                name="AliceBot",
                source_memory_ids=[str(seeded["memory_ids"][0]), str(seeded["memory_ids"][1])],
            ),
        ]

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/entities",
        query_params={"user_id": str(seeded["user_id"])},
    )

    expected_entities = sorted(created_entities, key=lambda entity: (entity["created_at"], entity["id"]))

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [str(entity["id"]) for entity in expected_entities]
    assert list_payload["summary"] == {
        "total_count": 3,
        "order": ["created_at_asc", "id_asc"],
    }

    target_entity = expected_entities[1]
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/entities/{target_entity['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert detail_status == 200
    assert detail_payload == {
        "entity": {
            "id": str(target_entity["id"]),
            "entity_type": target_entity["entity_type"],
            "name": target_entity["name"],
            "source_memory_ids": target_entity["source_memory_ids"],
            "created_at": target_entity["created_at"].isoformat(),
        }
    }


def test_entity_endpoints_enforce_per_user_isolation_and_not_found_behavior(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user_with_source_memories(migrated_database_urls["app"], email="intruder@example.com")
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
        entity = ContinuityStore(conn).create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[str(owner["memory_ids"][0])],
        )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/entities",
        query_params={"user_id": str(intruder["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/entities/{entity['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )
    create_status, create_payload = invoke_request(
        "POST",
        "/v0/entities",
        payload={
            "user_id": str(intruder["user_id"]),
            "entity_type": "project",
            "name": "Hidden Project",
            "source_memory_ids": [str(owner["memory_ids"][0])],
        },
    )

    assert list_status == 200
    assert list_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert create_status == 400
    assert create_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}


def test_create_entity_endpoint_rejects_missing_source_memory_ids(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user_with_source_memories(migrated_database_urls["app"], email="owner@example.com")
    missing_memory_id = uuid4()
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
        "/v0/entities",
        payload={
            "user_id": str(seeded["user_id"]),
            "entity_type": "routine",
            "name": "Morning Coffee",
            "source_memory_ids": [str(missing_memory_id)],
        },
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
