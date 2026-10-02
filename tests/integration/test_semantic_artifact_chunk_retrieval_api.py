from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
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


def seed_task_with_workspace(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Semantic artifact retrieval thread")
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

    return {
        "user_id": user_id,
        "task_id": task["id"],
        "task_workspace_id": workspace["id"],
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
            metadata={"task": "semantic_artifact_chunk_retrieval"},
        )
    return created["id"]


def create_artifact_with_chunk_embeddings(
    database_url: str,
    *,
    user_id: UUID,
    task_id: UUID,
    task_workspace_id: UUID,
    embedding_config_id: UUID | None,
    relative_path: str,
    chunks: list[tuple[str, list[float] | None]],
    ingestion_status: str = "ingested",
    media_type_hint: str | None = "text/plain",
) -> dict[str, object]:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        artifact = store.create_task_artifact(
            task_id=task_id,
            task_workspace_id=task_workspace_id,
            status="registered",
            ingestion_status=ingestion_status,
            relative_path=relative_path,
            media_type_hint=media_type_hint,
        )
        created_chunks: list[dict[str, object]] = []
        char_start = 0
        for sequence_no, (text, vector) in enumerate(chunks, start=1):
            chunk = store.create_task_artifact_chunk(
                task_artifact_id=artifact["id"],
                sequence_no=sequence_no,
                char_start=char_start,
                char_end_exclusive=char_start + len(text),
                text=text,
            )
            char_start += len(text)
            created_chunks.append(chunk)
            if embedding_config_id is not None and vector is not None:
                store.create_task_artifact_chunk_embedding(
                    task_artifact_chunk_id=chunk["id"],
                    embedding_config_id=embedding_config_id,
                    dimensions=len(vector),
                    vector=vector,
                )

    return {
        "artifact_id": artifact["id"],
        "chunk_ids": [chunk["id"] for chunk in created_chunks],
    }


def test_semantic_artifact_chunk_retrieval_endpoints_return_deterministic_task_and_artifact_results(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_task_with_workspace(migrated_database_urls["app"], email="owner@example.com")
    config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
        dimensions=3,
    )
    docs = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=config_id,
        relative_path="docs/a.txt",
        chunks=[("alpha doc", [1.0, 0.0, 0.0])],
        media_type_hint="text/plain",
    )
    notes = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=config_id,
        relative_path="notes/b.md",
        chunks=[("alpha note", [1.0, 0.0, 0.0])],
        media_type_hint="text/markdown",
    )
    weak = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=config_id,
        relative_path="notes/c.txt",
        chunks=[("beta weak", [0.0, 1.0, 0.0])],
        media_type_hint="text/plain",
    )
    pending = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=config_id,
        relative_path="notes/pending.txt",
        chunks=[("hidden pending", [1.0, 0.0, 0.0])],
        ingestion_status="pending",
        media_type_hint="text/plain",
    )
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    task_status, task_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 10,
        },
    )
    artifact_status, artifact_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{notes['artifact_id']}/chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 10,
        },
    )

    assert task_status == 200
    assert task_payload["summary"] == {
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "limit": 10,
        "returned_count": 3,
        "searched_artifact_count": 3,
        "similarity_metric": "cosine_similarity",
        "order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "scope": {"kind": "task", "task_id": str(owner["task_id"])},
    }
    assert [item["id"] for item in task_payload["items"]] == [
        str(docs["chunk_ids"][0]),
        str(notes["chunk_ids"][0]),
        str(weak["chunk_ids"][0]),
    ]
    assert str(pending["chunk_ids"][0]) not in {item["id"] for item in task_payload["items"]}
    assert task_payload["items"][0]["relative_path"] == "docs/a.txt"
    assert task_payload["items"][1]["relative_path"] == "notes/b.md"
    assert task_payload["items"][0]["score"] == pytest.approx(1.0)
    assert task_payload["items"][1]["score"] == pytest.approx(1.0)
    assert task_payload["items"][2]["score"] == pytest.approx(0.0)
    assert set(task_payload["items"][0]) == {
        "id",
        "task_id",
        "task_artifact_id",
        "relative_path",
        "media_type",
        "sequence_no",
        "char_start",
        "char_end_exclusive",
        "text",
        "score",
    }

    assert artifact_status == 200
    assert artifact_payload["summary"] == {
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "limit": 10,
        "returned_count": 1,
        "searched_artifact_count": 1,
        "similarity_metric": "cosine_similarity",
        "order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "scope": {
            "kind": "artifact",
            "task_id": str(owner["task_id"]),
            "task_artifact_id": str(notes["artifact_id"]),
        },
    }
    assert artifact_payload["items"] == [
        {
            "id": str(notes["chunk_ids"][0]),
            "task_id": str(owner["task_id"]),
            "task_artifact_id": str(notes["artifact_id"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": len("alpha note"),
            "text": "alpha note",
            "score": artifact_payload["items"][0]["score"],
        }
    ]
    assert artifact_payload["items"][0]["score"] == pytest.approx(1.0)


def test_semantic_artifact_chunk_retrieval_rejects_invalid_config_dimension_mismatch_and_cross_user_scope(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_task_with_workspace(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task_with_workspace(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
        dimensions=3,
    )
    owner_artifact = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=owner_config_id,
        relative_path="docs/spec.txt",
        chunks=[("owner chunk", [1.0, 0.0, 0.0])],
    )
    create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        task_id=intruder["task_id"],
        task_workspace_id=intruder["task_workspace_id"],
        embedding_config_id=intruder_config_id,
        relative_path="docs/intruder.txt",
        chunks=[("intruder chunk", [1.0, 0.0, 0.0])],
    )
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        legacy_gated_router,
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
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(uuid4()),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    mismatch_status, mismatch_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(owner_config_id),
            "query_vector": [1.0, 0.0],
            "limit": 5,
        },
    )
    cross_user_task_status, cross_user_task_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(intruder["user_id"]),
            "embedding_config_id": str(intruder_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    cross_user_artifact_status, cross_user_artifact_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{owner_artifact['artifact_id']}/chunks/semantic-retrieval",
        payload={
            "user_id": str(intruder["user_id"]),
            "embedding_config_id": str(intruder_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    cross_user_config_status, cross_user_config_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(intruder_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )

    assert missing_status == 400
    assert missing_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert cross_user_task_status == 404
    assert cross_user_task_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert cross_user_artifact_status == 404
    assert cross_user_artifact_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert cross_user_config_status == 400
    assert cross_user_config_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})


def test_semantic_artifact_chunk_retrieval_supports_empty_results_and_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_task_with_workspace(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task_with_workspace(migrated_database_urls["app"], email="intruder@example.com")
    owner_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
        dimensions=3,
    )
    owner_empty_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        provider="openai",
        model="text-embedding-3-small",
        version="2026-03-15",
        dimensions=3,
    )
    intruder_config_id = seed_embedding_config(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        provider="openai",
        model="text-embedding-3-large",
        version="2026-03-15",
        dimensions=3,
    )
    owner_artifact = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        task_id=owner["task_id"],
        task_workspace_id=owner["task_workspace_id"],
        embedding_config_id=owner_config_id,
        relative_path="docs/owner.txt",
        chunks=[("owner semantic", [1.0, 0.0, 0.0])],
    )
    intruder_artifact = create_artifact_with_chunk_embeddings(
        migrated_database_urls["app"],
        user_id=intruder["user_id"],
        task_id=intruder["task_id"],
        task_workspace_id=intruder["task_workspace_id"],
        embedding_config_id=intruder_config_id,
        relative_path="docs/intruder.txt",
        chunks=[("intruder semantic", [1.0, 0.0, 0.0])],
    )
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    owner_status, owner_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(owner_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    intruder_status, intruder_payload = invoke_request(
        "POST",
        f"/v0/tasks/{intruder['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(intruder["user_id"]),
            "embedding_config_id": str(intruder_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )
    empty_status, empty_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/semantic-retrieval",
        payload={
            "user_id": str(owner["user_id"]),
            "embedding_config_id": str(owner_empty_config_id),
            "query_vector": [1.0, 0.0, 0.0],
            "limit": 5,
        },
    )

    assert owner_status == 200
    assert [item["id"] for item in owner_payload["items"]] == [str(owner_artifact["chunk_ids"][0])]
    assert intruder_status == 200
    assert [item["id"] for item in intruder_payload["items"]] == [str(intruder_artifact["chunk_ids"][0])]
    assert empty_status == 200
    assert empty_payload == {
        "items": [],
        "summary": {
            "embedding_config_id": str(owner_empty_config_id),
            "query_vector_dimensions": 3,
            "limit": 5,
            "returned_count": 0,
            "searched_artifact_count": 1,
            "similarity_metric": "cosine_similarity",
            "order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
            "scope": {"kind": "task", "task_id": str(owner["task_id"])},
        },
    }
