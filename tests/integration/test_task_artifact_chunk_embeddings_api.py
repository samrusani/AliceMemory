from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
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
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_task_artifact_with_chunks(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Artifact chunk embedding thread")
        tool = store.create_tool(
            tool_key="proxy.echo",
            name="Proxy Echo",
            description="Deterministic proxy handler.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["proxy"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        task = store.create_task(
            thread_id=thread["id"],
            tool_id=tool["id"],
            status="approved",
            request={
                "thread_id": str(thread["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {},
            },
            tool={
                "id": str(tool["id"]),
                "tool_key": "proxy.echo",
                "name": "Proxy Echo",
                "description": "Deterministic proxy handler.",
                "version": "1.0.0",
                "metadata_version": "tool_metadata_v0",
                "active": True,
                "tags": ["proxy"],
                "action_hints": ["tool.run"],
                "scope_hints": ["workspace"],
                "domain_hints": [],
                "risk_hints": [],
                "metadata": {"transport": "proxy"},
                "created_at": tool["created_at"].isoformat(),
            },
            latest_approval_id=None,
            latest_execution_id=None,
        )
        workspace = store.create_task_workspace(
            task_id=task["id"],
            status="active",
            local_path=f"/tmp/task-workspaces/{user_id}/{task['id']}",
        )
        artifact = store.create_task_artifact(
            task_id=task["id"],
            task_workspace_id=workspace["id"],
            status="registered",
            ingestion_status="ingested",
            relative_path="docs/spec.txt",
            media_type_hint="text/plain",
        )
        first_chunk = store.create_task_artifact_chunk(
            task_artifact_id=artifact["id"],
            sequence_no=1,
            char_start=0,
            char_end_exclusive=12,
            text="alpha chunk",
        )
        second_chunk = store.create_task_artifact_chunk(
            task_artifact_id=artifact["id"],
            sequence_no=2,
            char_start=12,
            char_end_exclusive=24,
            text="beta chunk",
        )

    return {
        "user_id": user_id,
        "task_id": task["id"],
        "task_artifact_id": artifact["id"],
        "first_chunk_id": first_chunk["id"],
        "second_chunk_id": second_chunk["id"],
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
            metadata={"task": "artifact_chunk_retrieval"},
        )
    return created["id"]


def test_task_artifact_chunk_embedding_endpoints_persist_and_read_embeddings(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_task_artifact_with_chunks(
        migrated_database_urls["app"],
        email="owner@example.com",
    )
    first_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-small",
        version="2026-03-14",
        dimensions=3,
    )
    second_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
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

    second_write_status, second_write_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "task_artifact_chunk_id": str(seeded["second_chunk_id"]),
            "embedding_config_id": str(first_config_id),
            "vector": [0.4, 0.5, 0.6],
        },
    )
    first_write_status, first_write_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "task_artifact_chunk_id": str(seeded["first_chunk_id"]),
            "embedding_config_id": str(second_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    update_status, update_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(seeded["user_id"]),
            "task_artifact_chunk_id": str(seeded["second_chunk_id"]),
            "embedding_config_id": str(first_config_id),
            "vector": [0.9, 0.8, 0.7],
        },
    )
    artifact_list_status, artifact_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{seeded['task_artifact_id']}/chunk-embeddings",
        query_params={"user_id": str(seeded["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifact-chunks/{seeded['second_chunk_id']}/embeddings",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/task-artifact-chunk-embeddings/{update_payload['embedding']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert second_write_status == 201
    assert second_write_payload["write_mode"] == "created"
    assert first_write_status == 201
    assert first_write_payload["write_mode"] == "created"
    assert update_status == 201
    assert update_payload["write_mode"] == "updated"
    assert update_payload["embedding"]["vector"] == [0.9, 0.8, 0.7]
    assert artifact_list_status == 200
    assert artifact_list_payload["summary"] == {
        "total_count": 2,
        "order": ["task_artifact_chunk_sequence_no_asc", "created_at_asc", "id_asc"],
        "scope": {
            "kind": "artifact",
            "task_artifact_id": str(seeded["task_artifact_id"]),
        },
    }
    assert chunk_list_status == 200
    assert chunk_list_payload["summary"] == {
        "total_count": 1,
        "order": ["task_artifact_chunk_sequence_no_asc", "created_at_asc", "id_asc"],
        "scope": {
            "kind": "chunk",
            "task_artifact_id": str(seeded["task_artifact_id"]),
            "task_artifact_chunk_id": str(seeded["second_chunk_id"]),
        },
    }
    assert detail_status == 200
    assert detail_payload["embedding"]["id"] == update_payload["embedding"]["id"]
    assert detail_payload["embedding"]["task_artifact_chunk_sequence_no"] == 2
    assert set(detail_payload["embedding"]) == {
        "id",
        "task_artifact_id",
        "task_artifact_chunk_id",
        "task_artifact_chunk_sequence_no",
        "embedding_config_id",
        "dimensions",
        "vector",
        "created_at",
        "updated_at",
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored = ContinuityStore(conn).list_task_artifact_chunk_embeddings_for_artifact(seeded["task_artifact_id"])

    assert [item["id"] for item in artifact_list_payload["items"]] == [str(embedding["id"]) for embedding in stored]
    assert [item["task_artifact_chunk_id"] for item in artifact_list_payload["items"]] == [
        str(seeded["first_chunk_id"]),
        str(seeded["second_chunk_id"]),
    ]


def test_task_artifact_chunk_embedding_writes_reject_invalid_refs_dimension_mismatches_and_cross_user_refs(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_task_artifact_with_chunks(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task_artifact_with_chunks(
        migrated_database_urls["app"],
        email="intruder@example.com",
    )
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-14",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-14",
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
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "task_artifact_chunk_id": str(owner["first_chunk_id"]),
            "embedding_config_id": str(uuid4()),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    missing_chunk_status, missing_chunk_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "task_artifact_chunk_id": str(uuid4()),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    mismatch_status, mismatch_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "task_artifact_chunk_id": str(owner["first_chunk_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2],
        },
    )
    cross_user_chunk_status, cross_user_chunk_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(intruder["user_id"]),
            "task_artifact_chunk_id": str(owner["first_chunk_id"]),
            "embedding_config_id": str(intruder_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    cross_user_config_status, cross_user_config_payload = invoke_request(
        "POST",
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(intruder["user_id"]),
            "task_artifact_chunk_id": str(intruder["first_chunk_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )

    assert missing_config_status == 400
    assert missing_config_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert missing_chunk_status == 400
    assert missing_chunk_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert cross_user_chunk_status == 400
    assert cross_user_chunk_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})
    assert cross_user_config_status == 400
    assert cross_user_config_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})


def test_task_artifact_chunk_embedding_reads_respect_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_task_artifact_with_chunks(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task_artifact_with_chunks(
        migrated_database_urls["app"],
        email="intruder@example.com",
    )
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-14",
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
        "/v0/task-artifact-chunk-embeddings",
        payload={
            "user_id": str(owner["user_id"]),
            "task_artifact_chunk_id": str(owner["first_chunk_id"]),
            "embedding_config_id": str(owner_config_id),
            "vector": [0.1, 0.2, 0.3],
        },
    )
    artifact_list_status, artifact_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{owner['task_artifact_id']}/chunk-embeddings",
        query_params={"user_id": str(intruder["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifact-chunks/{owner['first_chunk_id']}/embeddings",
        query_params={"user_id": str(intruder["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/task-artifact-chunk-embeddings/{write_payload['embedding']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )

    assert write_status == 201
    assert artifact_list_status == 404
    assert artifact_list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert chunk_list_status == 404
    assert chunk_list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
