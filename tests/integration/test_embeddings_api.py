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


def seed_user_with_memory(database_url: str, *, email: str) -> dict[str, object]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Embedding source thread")
        session = store.create_session(thread["id"], status="active")
        event_id = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "likes oat milk"},
        )["id"]
        memory = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(event_id,),
            ),
        )

    return {
        "user_id": user_id,
        "memory_id": UUID(memory.memory["id"]),
    }


def seed_embedding_config(
    database_url: str,
    *,
    user_id: UUID,
    provider: str,
    model: str,
    version: str,
    dimensions: int,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        created = ContinuityStore(conn).create_embedding_config(
            provider=provider,
            model=model,
            version=version,
            dimensions=dimensions,
            status="active",
            metadata={"task": "memory_retrieval"},
        )
    return created["id"]


def seed_memory_with_embedding(
    database_url: str,
    *,
    user_id: UUID,
    memory_key: str,
    value: dict[str, object],
    embedding_config_id: UUID,
    vector: list[float],
    delete_requested: bool = False,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.create_thread(f"Semantic retrieval thread for {memory_key}")
        session = store.create_session(thread["id"], status="active")
        event_id = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"memory_key": memory_key, "value": value},
        )["id"]
        admitted = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key=memory_key,
                value=value,
                source_event_ids=(event_id,),
            ),
        )
        memory_id = UUID(admitted.memory["id"])
        store.create_memory_embedding(
            memory_id=memory_id,
            embedding_config_id=embedding_config_id,
            dimensions=len(vector),
            vector=vector,
        )
        if delete_requested:
            delete_event_id = store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"memory_key": memory_key, "delete_requested": True},
            )["id"]
            admit_memory_candidate(
                store,
                user_id=user_id,
                candidate=MemoryCandidateInput(
                    memory_key=memory_key,
                    value=None,
                    source_event_ids=(delete_event_id,),
                    delete_requested=True,
                ),
            )
    return memory_id


def test_embedding_config_endpoints_create_and_list_in_deterministic_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-small",
        version="2026-03-11",
        dimensions=1536,
    )
    seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3072,
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

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/embedding-configs",
        payload={
            "user_id": str(seeded["user_id"]),
            "provider": "openai",
            "model": "text-embedding-3-large",
            "version": "2026-03-13",
            "dimensions": 3,
            "status": "active",
            "metadata": {"task": "memory_retrieval"},
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/embedding-configs",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert create_status == 201
    assert create_payload["embedding_config"]["provider"] == "openai"
    assert create_payload["embedding_config"]["version"] == "2026-03-13"
    assert list_status == 200
    assert list_payload["summary"] == {
        "total_count": 3,
        "order": ["created_at_asc", "id_asc"],
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        expected_configs = ContinuityStore(conn).list_embedding_configs()

    assert [item["id"] for item in list_payload["items"]] == [str(config["id"]) for config in expected_configs]


def test_embedding_config_create_rejects_duplicate_provider_model_version(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
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
        "POST",
        "/v0/embedding-configs",
        payload={
            "user_id": str(seeded["user_id"]),
            "provider": "openai",
            "model": "text-embedding-3-large",
            "version": "2026-03-12",
            "dimensions": 3,
            "status": "active",
            "metadata": {"task": "memory_retrieval"},
        },
    )
    second_status, second_payload = invoke_request(
        "POST",
        "/v0/embedding-configs",
        payload={
            "user_id": str(seeded["user_id"]),
            "provider": "openai",
            "model": "text-embedding-3-large",
            "version": "2026-03-12",
            "dimensions": 3,
            "status": "active",
            "metadata": {"task": "memory_retrieval"},
        },
    )

    assert first_status == 201
    assert first_payload["embedding_config"]["version"] == "2026-03-12"
    assert second_status == 400
    assert second_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_memory_embedding_endpoints_persist_and_read_embeddings(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    first_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-small",
        version="2026-03-11",
        dimensions=3,
    )
    second_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
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

    first_write_status, first_write_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "embedding_config_id": str(first_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    second_write_status, second_write_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "embedding_config_id": str(second_config_id),
            "vector": [0.4, 0.5, 0.6],
        },
    )
    update_status, update_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "memory_id": str(seeded["memory_id"]),
            "embedding_config_id": str(first_config_id),
            "vector": [0.9, 0.8, 0.7],
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['memory_id']}/embeddings",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/memory-embeddings/{first_write_payload['embedding']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert first_write_status == 201
    assert first_write_payload["write_mode"] == "created"
    assert second_write_status == 201
    assert second_write_payload["write_mode"] == "created"
    assert update_status == 201
    assert update_payload["write_mode"] == "updated"
    assert update_payload["embedding"]["id"] == first_write_payload["embedding"]["id"]
    assert update_payload["embedding"]["vector"] == [0.9, 0.8, 0.7]
    assert list_status == 200
    assert list_payload["summary"] == {
        "memory_id": str(seeded["memory_id"]),
        "total_count": 2,
        "order": ["created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload["embedding"]["id"] == first_write_payload["embedding"]["id"]
    assert detail_payload["embedding"]["vector"] == [0.9, 0.8, 0.7]

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored = ContinuityStore(conn).list_memory_embeddings_for_memory(seeded["memory_id"])

    assert [item["id"] for item in list_payload["items"]] == [str(embedding["id"]) for embedding in stored]
    assert len(stored) == 2
    assert stored[0]["embedding_config_id"] == first_config_id
    assert stored[0]["vector"] == [0.9, 0.8, 0.7]
    assert stored[1]["embedding_config_id"] == second_config_id


def test_memory_embedding_writes_reject_invalid_references_dimension_mismatches_and_cross_user_refs(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user_with_memory(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
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

    missing_config_status, missing_config_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_id": str(owner["memory_id"]),
            "embedding_config_id": str(uuid4()),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    missing_memory_status, missing_memory_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_id": str(uuid4()),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    mismatch_status, mismatch_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_id": str(owner["memory_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2],
        },
    )
    cross_user_status, cross_user_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(intruder["user_id"]),
            "memory_id": str(owner["memory_id"]),
            "embedding_config_id": str(intruder_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    cross_user_config_status, cross_user_config_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(intruder["user_id"]),
            "memory_id": str(intruder["memory_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )

    assert missing_config_status == 400
    assert missing_config_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert missing_memory_status == 400
    assert missing_memory_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert cross_user_status == 400
    assert cross_user_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})
    assert cross_user_config_status == 400
    assert cross_user_config_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})


def test_embedding_reads_respect_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user_with_memory(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
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

    write_status, write_payload = invoke_request(
        "POST",
        "/v0/memory-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_id": str(owner["memory_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    config_list_status, config_list_payload = invoke_request(
        "GET",
        "/v0/embedding-configs",
        query_params={"user_id": str(intruder["user_id"])},
    )
    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/memories/{owner['memory_id']}/embeddings",
        query_params={"user_id": str(intruder["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/memory-embeddings/{write_payload['embedding']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert write_status == 201
    assert config_list_status == 200
    assert config_list_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert list_status == 404
    assert list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}


def test_semantic_memory_retrieval_returns_deterministic_results_and_excludes_deleted_memories(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    first_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_key="user.preference.breakfast",
        value={"likes": "porridge"},
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    deleted_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_key="user.preference.deleted",
        value={"likes": "hidden"},
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
        delete_requested=True,
    )
    second_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_key="user.preference.lunch",
        value={"likes": "ramen"},
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    third_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        memory_key="user.preference.music",
        value={"likes": "jazz"},
        embedding_config_id=config_id,
        vector=[0.0, 1.0, 0.0],
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

    status, payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(seeded["user_id"]),
            "embedding_config_id": str(config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 10,
        },
    )

    assert status == 200
    assert payload["summary"] == {
        "embedding_config_id": str(config_id),
        "limit": 10,
        "returned_count": 3,
        "similarity_metric": "cosine_similarity",
        "order": ["score_desc", "created_at_asc", "id_asc"],
    }
    assert [item["memory_id"] for item in payload["items"]] == [
        str(first_memory_id),
        str(second_memory_id),
        str(third_memory_id),
    ]
    assert str(deleted_memory_id) not in {item["memory_id"] for item in payload["items"]}
    assert payload["items"][0]["score"] == payload["items"][1]["score"]
    assert payload["items"][0]["score"] > payload["items"][2]["score"]
    assert set(payload["items"][0]) == {
        "memory_id",
        "memory_key",
        "value",
        "source_event_ids",
        "memory_type",
        "confidence",
        "salience",
        "confirmation_status",
        "trust_class",
        "promotion_eligibility",
        "evidence_count",
        "independent_source_count",
        "extracted_by_model",
        "trust_reason",
        "valid_from",
        "valid_to",
        "last_confirmed_at",
        "created_at",
        "updated_at",
        "score",
    }


def test_semantic_memory_retrieval_rejects_invalid_config_dimension_mismatch_and_cross_user_access(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user_with_memory(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        memory_key="user.preference.owner",
        value={"likes": "oat milk"},
        embedding_config_id=owner_config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        memory_key="user.preference.intruder",
        value={"likes": "almond milk"},
        embedding_config_id=intruder_config_id,
        vector=[1.0, 0.0, 0.0],
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

    missing_status, missing_payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(uuid4()),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    mismatch_status, mismatch_payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(owner_config_id),
            "query_vector": [1.0, 0.0],
            "limit": 5,
        },
    )
    cross_user_status, cross_user_payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(intruder["user_id"]),
            "embedding_config_id": str(owner_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )

    assert missing_status == 400
    assert missing_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert cross_user_status == 400
    assert cross_user_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})


def test_semantic_memory_retrieval_scopes_results_per_user(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user_with_memory(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user_with_memory(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-12",
        dimensions=3,
    )
    owner_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        memory_key="user.preference.owner.semantic",
        value={"likes": "espresso"},
        embedding_config_id=owner_config_id,
        vector=[1.0, 0.0, 0.0],
    )
    intruder_memory_id = seed_memory_with_embedding(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        memory_key="user.preference.intruder.semantic",
        value={"likes": "matcha"},
        embedding_config_id=intruder_config_id,
        vector=[1.0, 0.0, 0.0],
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

    owner_status, owner_payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(owner_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    intruder_status, intruder_payload = invoke_request(
        "POST",
        "/v0/memories/semantic-retrieval",
        payload={
            "user_id": str(intruder["user_id"]),
            "embedding_config_id": str(intruder_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )

    assert owner_status == 200
    assert [item["memory_id"] for item in owner_payload["items"]] == [str(owner_memory_id)]
    assert intruder_status == 200
    assert [item["memory_id"] for item in intruder_payload["items"]] == [str(intruder_memory_id)]
