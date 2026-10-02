from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore


def invoke_compile_context(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
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
        "method": "POST",
        "scheme": "http",
        "path": "/v0/context/compile",
        "raw_path": b"/v0/context/compile",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_traceable_thread(
    database_url: str,
    *,
    email: str = "owner@example.com",
    display_name: str = "Owner",
) -> dict[str, object]:
    user_id = uuid4()
    included_edge_valid_from = datetime(2026, 3, 12, 10, 0, tzinfo=UTC)

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, display_name)
        thread = store.create_thread("Context thread")
        first_session = store.create_session(thread["id"], status="complete")
        second_session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], first_session["id"], "message.user", {"text": "old"})["id"],
            store.append_event(thread["id"], second_session["id"], "message.assistant", {"text": "newer"})["id"],
            store.append_event(thread["id"], second_session["id"], "message.user", {"text": "newest"})["id"],
        ]
        breakfast_memory = store.create_memory(
            memory_key="user.preference.breakfast",
            value={"likes": "toast"},
            status="active",
            source_event_ids=[str(event_ids[0])],
        )
        coffee_memory = store.create_memory(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            status="active",
            source_event_ids=[str(event_ids[1])],
        )
        deleted_memory = store.create_memory(
            memory_key="user.preference.old",
            value={"likes": "black"},
            status="active",
            source_event_ids=[str(event_ids[1])],
        )
        deleted_memory = store.update_memory(
            memory_id=deleted_memory["id"],
            value=deleted_memory["value"],
            status="deleted",
            source_event_ids=[str(event_ids[2])],
        )
        person = store.create_entity(
            entity_type="person",
            name="Alex",
            source_memory_ids=[str(breakfast_memory["id"])],
        )
        merchant = store.create_entity(
            entity_type="merchant",
            name="Neighborhood Cafe",
            source_memory_ids=[str(coffee_memory["id"])],
        )
        project = store.create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[str(breakfast_memory["id"]), str(coffee_memory["id"])],
        )
        excluded_edge = store.create_entity_edge(
            from_entity_id=person["id"],
            to_entity_id=project["id"],
            relationship_type="visited_by",
            valid_from=None,
            valid_to=None,
            source_memory_ids=[str(breakfast_memory["id"])],
        )
        included_edge = store.create_entity_edge(
            from_entity_id=project["id"],
            to_entity_id=merchant["id"],
            relationship_type="depends_on",
            valid_from=included_edge_valid_from,
            valid_to=None,
            source_memory_ids=[str(coffee_memory["id"])],
        )
        ignored_when_project_only_edge = store.create_entity_edge(
            from_entity_id=person["id"],
            to_entity_id=merchant["id"],
            relationship_type="introduced_to",
            valid_from=None,
            valid_to=None,
            source_memory_ids=[str(breakfast_memory["id"])],
        )
        entities = store.list_entities()
        entity_edges = store.list_entity_edges_for_entities([person["id"], merchant["id"], project["id"]])

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
        "event_ids": event_ids,
        "memories": {
            "breakfast": breakfast_memory,
            "coffee": coffee_memory,
            "deleted": deleted_memory,
        },
        "entities": entities,
        "entity_edges": entity_edges,
        "project_only_candidate_edges": {
            "excluded": excluded_edge,
            "included": included_edge,
            "ignored": ignored_when_project_only_edge,
        },
        "included_edge_valid_from": included_edge_valid_from,
    }


def seed_thread_with_updated_active_memory(database_url: str) -> dict[str, object]:
    user_id = uuid4()
    included_edge_valid_from = datetime(2026, 3, 12, 11, 0, tzinfo=UTC)

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Updated memory thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": "baseline memory evidence"},
            )["id"],
            store.append_event(
                thread["id"],
                session["id"],
                "message.user",
                {"text": "updated memory evidence"},
            )["id"],
        ]
        store.create_memory(
            memory_key="user.preference.breakfast",
            value={"likes": "toast"},
            status="active",
            source_event_ids=[str(event_ids[0])],
        )
        coffee_memory = store.create_memory(
            memory_key="user.preference.coffee",
            value={"likes": "black"},
            status="active",
            source_event_ids=[str(event_ids[0])],
        )
        store.update_memory(
            memory_id=coffee_memory["id"],
            value={"likes": "oat milk"},
            status="active",
            source_event_ids=[str(event_ids[1])],
        )
        routine = store.create_entity(
            entity_type="routine",
            name="Breakfast",
            source_memory_ids=[str(coffee_memory["id"])],
        )
        project = store.create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[str(coffee_memory["id"])],
        )
        included_edge = store.create_entity_edge(
            from_entity_id=project["id"],
            to_entity_id=routine["id"],
            relationship_type="references",
            valid_from=included_edge_valid_from,
            valid_to=None,
            source_memory_ids=[str(coffee_memory["id"])],
        )
        store.create_entity_edge(
            from_entity_id=routine["id"],
            to_entity_id=routine["id"],
            relationship_type="superseded_by",
            valid_from=None,
            valid_to=None,
            source_memory_ids=[str(coffee_memory["id"])],
        )
        entities = store.list_entities()

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
        "event_ids": event_ids,
        "entities": entities,
        "included_edge": included_edge,
        "included_edge_valid_from": included_edge_valid_from,
    }


def seed_embedding_config_for_user(
    database_url: str,
    *,
    user_id: UUID,
    dimensions: int = 3,
) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        config = store.create_embedding_config(
            provider="openai",
            model="text-embedding-3-large",
            version="2026-03-12",
            dimensions=dimensions,
            status="active",
            metadata={"task": "compile_semantic_retrieval"},
        )
    return config["id"]


def seed_memory_embedding_for_user(
    database_url: str,
    *,
    user_id: UUID,
    memory_id: UUID,
    embedding_config_id: UUID,
    vector: list[float],
) -> None:
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_memory_embedding(
            memory_id=memory_id,
            embedding_config_id=embedding_config_id,
            dimensions=len(vector),
            vector=vector,
        )


def seed_task_artifact_chunk_embedding_for_user(
    database_url: str,
    *,
    user_id: UUID,
    task_artifact_chunk_id: UUID,
    embedding_config_id: UUID,
    vector: list[float],
) -> None:
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_task_artifact_chunk_embedding(
            task_artifact_chunk_id=task_artifact_chunk_id,
            embedding_config_id=embedding_config_id,
            dimensions=len(vector),
            vector=vector,
        )


def seed_compile_artifact_scope(
    database_url: str,
    *,
    user_id: UUID,
    thread_id: UUID,
) -> dict[str, object]:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key="artifact.search",
            name="Artifact Search",
            description="Compile artifact retrieval fixture",
            version="2026-03-14",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=[],
            action_hints=["retrieve"],
            scope_hints=["task"],
            domain_hints=[],
            risk_hints=[],
            metadata={},
        )
        task = store.create_task(
            thread_id=thread_id,
            tool_id=tool["id"],
            status="approved",
            request={"action": "retrieve"},
            tool={"tool_key": "artifact.search"},
            latest_approval_id=None,
            latest_execution_id=None,
        )
        workspace = store.create_task_workspace(
            task_id=task["id"],
            status="active",
            local_path=f"/tmp/alicebot/{task['id']}",
        )
        docs_artifact = store.create_task_artifact(
            task_id=task["id"],
            task_workspace_id=workspace["id"],
            status="registered",
            ingestion_status="ingested",
            relative_path="docs/a.txt",
            media_type_hint="text/plain",
        )
        notes_artifact = store.create_task_artifact(
            task_id=task["id"],
            task_workspace_id=workspace["id"],
            status="registered",
            ingestion_status="ingested",
            relative_path="notes/b.md",
            media_type_hint="text/markdown",
        )
        pending_artifact = store.create_task_artifact(
            task_id=task["id"],
            task_workspace_id=workspace["id"],
            status="registered",
            ingestion_status="pending",
            relative_path="notes/hidden.txt",
            media_type_hint="text/plain",
        )
        weak_artifact = store.create_task_artifact(
            task_id=task["id"],
            task_workspace_id=workspace["id"],
            status="registered",
            ingestion_status="ingested",
            relative_path="notes/c.txt",
            media_type_hint="text/plain",
        )
        docs_chunk = store.create_task_artifact_chunk(
            task_artifact_id=docs_artifact["id"],
            sequence_no=1,
            char_start=0,
            char_end_exclusive=14,
            text="beta alpha doc",
        )
        notes_chunk = store.create_task_artifact_chunk(
            task_artifact_id=notes_artifact["id"],
            sequence_no=1,
            char_start=0,
            char_end_exclusive=15,
            text="alpha beta note",
        )
        pending_chunk = store.create_task_artifact_chunk(
            task_artifact_id=pending_artifact["id"],
            sequence_no=1,
            char_start=0,
            char_end_exclusive=17,
            text="alpha beta hidden",
        )
        weak_chunk = store.create_task_artifact_chunk(
            task_artifact_id=weak_artifact["id"],
            sequence_no=1,
            char_start=0,
            char_end_exclusive=9,
            text="beta only",
        )

    return {
        "task_id": task["id"],
        "artifact_ids": {
            "docs": docs_artifact["id"],
            "notes": notes_artifact["id"],
            "pending": pending_artifact["id"],
            "weak": weak_artifact["id"],
        },
        "chunk_ids": {
            "docs": docs_chunk["id"],
            "notes": notes_chunk["id"],
            "pending": pending_chunk["id"],
            "weak": weak_chunk["id"],
        },
    }


def test_compile_context_endpoint_persists_trace_and_trace_events(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    user_id = seeded["user_id"]
    thread_id = seeded["thread_id"]
    event_ids = seeded["event_ids"]
    entities = seeded["entities"]
    included_entity = entities[-1]
    project_only_candidate_edges = seeded["project_only_candidate_edges"]
    included_entity_edge = project_only_candidate_edges["included"]
    excluded_entity_edge = project_only_candidate_edges["excluded"]
    ignored_entity_edge = project_only_candidate_edges["ignored"]
    included_edge_valid_from = seeded["included_edge_valid_from"]
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_sessions": 1,
            "max_events": 1,
            "max_memories": 1,
            "max_entities": 1,
            "max_entity_edges": 1,
        }
    )

    assert status_code == 200
    assert payload["trace_event_count"] > 0
    assert payload["context_pack"]["limits"] == {
        "max_sessions": 1,
        "max_events": 1,
        "max_memories": 1,
        "max_entities": 1,
        "max_entity_edges": 1,
    }
    assert [session["status"] for session in payload["context_pack"]["sessions"]] == ["active"]
    assert [event["sequence_no"] for event in payload["context_pack"]["events"]] == [3]
    assert payload["context_pack"]["memories"] == [
        {
            "id": payload["context_pack"]["memories"][0]["id"],
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": [str(event_ids[1])],
            "memory_type": "preference",
            "confidence": None,
            "salience": None,
            "confirmation_status": "unconfirmed",
            "trust_class": "deterministic",
            "promotion_eligibility": "promotable",
            "evidence_count": None,
            "independent_source_count": None,
            "extracted_by_model": None,
            "trust_reason": None,
            "valid_from": None,
            "valid_to": None,
            "last_confirmed_at": None,
            "created_at": payload["context_pack"]["memories"][0]["created_at"],
            "updated_at": payload["context_pack"]["memories"][0]["updated_at"],
            "source_provenance": {"sources": ["symbolic"], "semantic_score": None},
        }
    ]
    assert payload["context_pack"]["memory_summary"] == {
        "candidate_count": 2,
        "included_count": 1,
        "excluded_deleted_count": 1,
        "excluded_limit_count": 0,
        "hybrid_retrieval": {
            "requested": False,
            "embedding_config_id": None,
            "query_vector_dimensions": 0,
            "semantic_limit": 0,
            "symbolic_selected_count": 1,
            "semantic_selected_count": 0,
            "merged_candidate_count": 1,
            "deduplicated_count": 0,
            "included_symbolic_only_count": 1,
            "included_semantic_only_count": 0,
            "included_dual_source_count": 0,
            "similarity_metric": None,
            "source_precedence": ["symbolic", "semantic"],
            "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
            "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
        },
    }
    assert payload["context_pack"]["artifact_chunks"] == []
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": False,
        "lexical_requested": False,
        "semantic_requested": False,
        "scope": None,
        "query": None,
        "query_terms": [],
        "embedding_config_id": None,
        "query_vector_dimensions": 0,
        "limit": 0,
        "lexical_limit": 0,
        "semantic_limit": 0,
        "searched_artifact_count": 0,
        "lexical_candidate_count": 0,
        "semantic_candidate_count": 0,
        "merged_candidate_count": 0,
        "deduplicated_count": 0,
        "included_count": 0,
        "included_lexical_only_count": 0,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 0,
        "excluded_limit_count": 0,
        "matching_rule": None,
        "similarity_metric": None,
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }
    assert payload["context_pack"]["entities"] == [
        {
            "id": str(included_entity["id"]),
            "entity_type": included_entity["entity_type"],
            "name": included_entity["name"],
            "source_memory_ids": included_entity["source_memory_ids"],
            "created_at": included_entity["created_at"].isoformat(),
        }
    ]
    assert payload["context_pack"]["entity_summary"] == {
        "candidate_count": 3,
        "included_count": 1,
        "excluded_limit_count": 2,
    }
    assert payload["context_pack"]["entity_edges"] == [
        {
            "id": str(included_entity_edge["id"]),
            "from_entity_id": str(included_entity_edge["from_entity_id"]),
            "to_entity_id": str(included_entity_edge["to_entity_id"]),
            "relationship_type": included_entity_edge["relationship_type"],
            "valid_from": included_edge_valid_from.isoformat(),
            "valid_to": None,
            "source_memory_ids": included_entity_edge["source_memory_ids"],
            "created_at": payload["context_pack"]["entity_edges"][0]["created_at"],
        }
    ]
    assert payload["context_pack"]["entity_edge_summary"] == {
        "anchor_entity_count": 1,
        "candidate_count": 2,
        "included_count": 1,
        "excluded_limit_count": 1,
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(trace_id)
        trace_events = store.list_trace_events(trace_id)

    assert trace["thread_id"] == thread_id
    assert trace["kind"] == "context.compile"
    assert trace["limits"] == {
        "max_sessions": 1,
        "max_events": 1,
        "max_memories": 1,
        "max_entities": 1,
        "max_entity_edges": 1,
    }
    assert trace_events[0]["kind"] == "context.included"
    assert trace_events[-1]["kind"] == "context.summary"
    assert any(
        event["payload"]["reason"] == "session_limit_exceeded"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "event_limit_exceeded"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_memory_deleted"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "within_hybrid_memory_limit"
        and event["payload"]["memory_key"] == "user.preference.coffee"
        and event["payload"]["selected_sources"] == ["symbolic"]
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "entity_limit_exceeded"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "within_entity_limit"
        and event["payload"]["name"] == included_entity["name"]
        and event["payload"]["record_entity_type"] == included_entity["entity_type"]
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "entity_edge_limit_exceeded"
        and event["payload"]["entity_id"] == str(excluded_entity_edge["id"])
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "within_entity_edge_limit"
        and event["payload"]["entity_id"] == str(included_entity_edge["id"])
        and event["payload"]["valid_from"] == included_edge_valid_from.isoformat()
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert all(event["payload"].get("entity_id") != str(ignored_entity_edge["id"]) for event in trace_events)
    assert trace_events[-1]["payload"]["included_memory_count"] == 1
    assert trace_events[-1]["payload"]["excluded_deleted_memory_count"] == 1
    assert trace_events[-1]["payload"]["excluded_memory_limit_count"] == 0
    assert trace_events[-1]["payload"]["hybrid_memory_requested"] is False
    assert trace_events[-1]["payload"]["hybrid_memory_candidate_count"] == 2
    assert trace_events[-1]["payload"]["hybrid_memory_merged_candidate_count"] == 1
    assert trace_events[-1]["payload"]["hybrid_memory_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 0
    assert trace_events[-1]["payload"]["included_entity_count"] == 1
    assert trace_events[-1]["payload"]["excluded_entity_limit_count"] == 2
    assert trace_events[-1]["payload"]["included_entity_edge_count"] == 1
    assert trace_events[-1]["payload"]["excluded_entity_edge_limit_count"] == 1

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error, match="append-only"):
                cur.execute("UPDATE trace_events SET kind = 'mutated' WHERE trace_id = %s", (trace_id,))


def test_compile_context_prefers_updated_active_memory_within_same_transaction(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_thread_with_updated_active_memory(migrated_database_urls["app"])
    user_id = seeded["user_id"]
    thread_id = seeded["thread_id"]
    event_ids = seeded["event_ids"]
    entities = seeded["entities"]
    excluded_entity = entities[0]
    included_edge = seeded["included_edge"]
    included_edge_valid_from = seeded["included_edge_valid_from"]
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_sessions": 1,
            "max_events": 2,
            "max_memories": 1,
            "max_entities": 1,
            "max_entity_edges": 1,
        }
    )

    assert status_code == 200
    assert payload["trace_event_count"] > 0
    assert payload["context_pack"]["memories"] == [
        {
            "id": payload["context_pack"]["memories"][0]["id"],
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": [str(event_ids[1])],
            "memory_type": "preference",
            "confidence": None,
            "salience": None,
            "confirmation_status": "unconfirmed",
            "trust_class": "deterministic",
            "promotion_eligibility": "promotable",
            "evidence_count": None,
            "independent_source_count": None,
            "extracted_by_model": None,
            "trust_reason": None,
            "valid_from": None,
            "valid_to": None,
            "last_confirmed_at": None,
            "created_at": payload["context_pack"]["memories"][0]["created_at"],
            "updated_at": payload["context_pack"]["memories"][0]["updated_at"],
            "source_provenance": {"sources": ["symbolic"], "semantic_score": None},
        }
    ]
    assert payload["context_pack"]["memory_summary"] == {
        "candidate_count": 1,
        "included_count": 1,
        "excluded_deleted_count": 0,
        "excluded_limit_count": 0,
        "hybrid_retrieval": {
            "requested": False,
            "embedding_config_id": None,
            "query_vector_dimensions": 0,
            "semantic_limit": 0,
            "symbolic_selected_count": 1,
            "semantic_selected_count": 0,
            "merged_candidate_count": 1,
            "deduplicated_count": 0,
            "included_symbolic_only_count": 1,
            "included_semantic_only_count": 0,
            "included_dual_source_count": 0,
            "similarity_metric": None,
            "source_precedence": ["symbolic", "semantic"],
            "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
            "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
        },
    }
    assert payload["context_pack"]["artifact_chunks"] == []
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": False,
        "lexical_requested": False,
        "semantic_requested": False,
        "scope": None,
        "query": None,
        "query_terms": [],
        "embedding_config_id": None,
        "query_vector_dimensions": 0,
        "limit": 0,
        "lexical_limit": 0,
        "semantic_limit": 0,
        "searched_artifact_count": 0,
        "lexical_candidate_count": 0,
        "semantic_candidate_count": 0,
        "merged_candidate_count": 0,
        "deduplicated_count": 0,
        "included_count": 0,
        "included_lexical_only_count": 0,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 0,
        "excluded_limit_count": 0,
        "matching_rule": None,
        "similarity_metric": None,
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }
    assert payload["context_pack"]["entity_summary"] == {
        "candidate_count": 2,
        "included_count": 1,
        "excluded_limit_count": 1,
    }
    assert payload["context_pack"]["entity_edges"] == [
        {
            "id": str(included_edge["id"]),
            "from_entity_id": str(included_edge["from_entity_id"]),
            "to_entity_id": str(included_edge["to_entity_id"]),
            "relationship_type": included_edge["relationship_type"],
            "valid_from": included_edge_valid_from.isoformat(),
            "valid_to": None,
            "source_memory_ids": included_edge["source_memory_ids"],
            "created_at": payload["context_pack"]["entity_edges"][0]["created_at"],
        }
    ]
    assert payload["context_pack"]["entity_edge_summary"] == {
        "anchor_entity_count": 1,
        "candidate_count": 1,
        "included_count": 1,
        "excluded_limit_count": 0,
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_memory_limit"
        and event["payload"]["memory_key"] == "user.preference.coffee"
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "entity_limit_exceeded" and event["payload"]["name"] == excluded_entity["name"]
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "within_entity_edge_limit"
        and event["payload"]["entity_id"] == str(included_edge["id"])
        for event in trace_events
        if event["kind"] == "context.included"
    )


def test_compile_context_endpoint_merges_hybrid_memory_provenance_and_trace_events(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    user_id = seeded["user_id"]
    thread_id = seeded["thread_id"]
    memories = seeded["memories"]
    config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=user_id,
    )
    seed_memory_embedding_for_user(
        migrated_database_urls["app"],
        user_id=user_id,
        memory_id=memories["breakfast"]["id"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_memory_embedding_for_user(
        migrated_database_urls["app"],
        user_id=user_id,
        memory_id=memories["coffee"]["id"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_memory_embedding_for_user(
        migrated_database_urls["app"],
        user_id=user_id,
        memory_id=memories["deleted"]["id"],
        embedding_config_id=config_id,
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "max_sessions": 1,
            "max_events": 1,
            "max_memories": 1,
            "max_entities": 1,
            "max_entity_edges": 1,
            "semantic": {
                "embedding_config_id": str(config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["trace_event_count"] > 0
    assert payload["context_pack"]["memories"] == [
        {
            "id": str(memories["coffee"]["id"]),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": memories["coffee"]["source_event_ids"],
            "memory_type": memories["coffee"]["memory_type"],
            "confidence": memories["coffee"]["confidence"],
            "salience": memories["coffee"]["salience"],
            "confirmation_status": memories["coffee"]["confirmation_status"],
            "trust_class": memories["coffee"]["trust_class"],
            "promotion_eligibility": memories["coffee"]["promotion_eligibility"],
            "evidence_count": memories["coffee"]["evidence_count"],
            "independent_source_count": memories["coffee"]["independent_source_count"],
            "extracted_by_model": memories["coffee"]["extracted_by_model"],
            "trust_reason": memories["coffee"]["trust_reason"],
            "valid_from": (
                None if memories["coffee"]["valid_from"] is None else memories["coffee"]["valid_from"].isoformat()
            ),
            "valid_to": (
                None if memories["coffee"]["valid_to"] is None else memories["coffee"]["valid_to"].isoformat()
            ),
            "last_confirmed_at": (
                None
                if memories["coffee"]["last_confirmed_at"] is None
                else memories["coffee"]["last_confirmed_at"].isoformat()
            ),
            "created_at": memories["coffee"]["created_at"].isoformat(),
            "updated_at": memories["coffee"]["updated_at"].isoformat(),
            "source_provenance": {
                "sources": ["symbolic", "semantic"],
                "semantic_score": 1.0,
            },
        }
    ]
    assert payload["context_pack"]["memory_summary"] == {
        "candidate_count": 3,
        "included_count": 1,
        "excluded_deleted_count": 1,
        "excluded_limit_count": 1,
        "hybrid_retrieval": {
            "requested": True,
            "embedding_config_id": str(config_id),
            "query_vector_dimensions": 3,
            "semantic_limit": 2,
            "symbolic_selected_count": 1,
            "semantic_selected_count": 2,
            "merged_candidate_count": 2,
            "deduplicated_count": 1,
            "included_symbolic_only_count": 0,
            "included_semantic_only_count": 0,
            "included_dual_source_count": 1,
            "similarity_metric": "cosine_similarity",
            "source_precedence": ["symbolic", "semantic"],
            "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
            "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
        },
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_memory_limit"
        and event["payload"]["entity_id"] == str(memories["coffee"]["id"])
        and event["payload"]["embedding_config_id"] == str(config_id)
        and event["payload"]["semantic_score"] == 1.0
        and event["payload"]["selected_sources"] == ["symbolic", "semantic"]
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_memory_deduplicated"
        and event["payload"]["entity_id"] == str(memories["coffee"]["id"])
        and event["payload"]["embedding_config_id"] == str(config_id)
        and event["payload"]["semantic_score"] == 1.0
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_memory_limit_exceeded"
        and event["payload"]["entity_id"] == str(memories["breakfast"]["id"])
        and event["payload"]["embedding_config_id"] == str(config_id)
        and event["payload"]["semantic_score"] == 1.0
        and event["payload"]["selected_sources"] == ["semantic"]
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_memory_deleted"
        and event["payload"]["entity_id"] == str(memories["deleted"]["id"])
        and event["payload"]["embedding_config_id"] == str(config_id)
        and event["payload"]["semantic_score"] is None
        and event["payload"]["selected_sources"] == ["symbolic"]
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert trace_events[-1]["payload"]["hybrid_memory_requested"] is True
    assert trace_events[-1]["payload"]["hybrid_memory_candidate_count"] == 3
    assert trace_events[-1]["payload"]["hybrid_memory_merged_candidate_count"] == 2
    assert trace_events[-1]["payload"]["hybrid_memory_deduplicated_count"] == 1
    assert trace_events[-1]["payload"]["included_dual_source_memory_count"] == 1


def test_compile_context_semantic_validation_rejects_missing_config_dimension_mismatch_and_cross_user_access(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_traceable_thread(migrated_database_urls["app"])
    intruder = seed_traceable_thread(
        migrated_database_urls["app"],
        email="intruder@example.com",
        display_name="Intruder",
    )
    owner_config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
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

    missing_status, missing_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "semantic": {
                "embedding_config_id": str(uuid4()),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 1,
            },
        }
    )
    mismatch_status, mismatch_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "semantic": {
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0],
                "limit": 1,
            },
        }
    )
    cross_user_status, cross_user_payload = invoke_compile_context(
        {
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "semantic": {
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 1,
            },
        }
    )

    assert missing_status == 400
    assert missing_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert cross_user_status == 400
    assert cross_user_payload["detail"] == ({"code": "invalid_request", "message": "The request is invalid"})


def test_compile_context_artifact_retrieval_integrates_chunks_traces_and_exclusion_rules(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        thread_id=seeded["thread_id"],
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "artifact_retrieval": {
                "kind": "task",
                "task_id": str(artifact_scope["task_id"]),
                "query": "Alpha beta",
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["context_pack"]["artifact_chunks"] == [
        {
            "id": str(artifact_scope["chunk_ids"]["docs"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["docs"]),
            "relative_path": "docs/a.txt",
            "media_type": "text/plain",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 14,
            "text": "beta alpha doc",
            "source_provenance": {
                "sources": ["lexical"],
                "lexical_match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
                "semantic_score": None,
            },
        },
        {
            "id": str(artifact_scope["chunk_ids"]["notes"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 15,
            "text": "alpha beta note",
            "source_provenance": {
                "sources": ["lexical"],
                "lexical_match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
                "semantic_score": None,
            },
        },
    ]
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": True,
        "lexical_requested": True,
        "semantic_requested": False,
        "scope": {"kind": "task", "task_id": str(artifact_scope["task_id"])},
        "query": "Alpha beta",
        "query_terms": ["alpha", "beta"],
        "embedding_config_id": None,
        "query_vector_dimensions": 0,
        "limit": 2,
        "lexical_limit": 2,
        "semantic_limit": 0,
        "searched_artifact_count": 3,
        "lexical_candidate_count": 3,
        "semantic_candidate_count": 0,
        "merged_candidate_count": 3,
        "deduplicated_count": 0,
        "included_count": 2,
        "included_lexical_only_count": 2,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 1,
        "excluded_limit_count": 1,
        "matching_rule": "casefolded_unicode_word_overlap_unique_query_terms_v1",
        "similarity_metric": None,
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }
    assert payload["context_pack"]["memories"]
    assert payload["context_pack"]["entities"]

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["docs"])
        and event["payload"]["relative_path"] == "docs/a.txt"
        and event["payload"]["matched_query_terms"] == ["alpha", "beta"]
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["notes"])
        and event["payload"]["relative_path"] == "notes/b.md"
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_artifact_chunk_limit_exceeded"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["weak"])
        and event["payload"]["relative_path"] == "notes/c.txt"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_artifact_not_ingested"
        and event["payload"]["entity_id"] == str(artifact_scope["artifact_ids"]["pending"])
        and event["payload"]["relative_path"] == "notes/hidden.txt"
        and event["payload"]["ingestion_status"] == "pending"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert trace_events[-1]["payload"]["artifact_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_retrieval_scope_kind"] == "task"
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["included_artifact_chunk_count"] == 2
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 0
    assert trace_events[-1]["payload"]["excluded_artifact_chunk_limit_count"] == 1
    assert trace_events[-1]["payload"]["excluded_uningested_artifact_count"] == 1


def test_compile_context_artifact_scoped_retrieval_returns_only_visible_artifact_chunks(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        thread_id=seeded["thread_id"],
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "artifact_retrieval": {
                "kind": "artifact",
                "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
                "query": "Alpha beta",
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["context_pack"]["artifact_chunks"] == [
        {
            "id": str(artifact_scope["chunk_ids"]["notes"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 15,
            "text": "alpha beta note",
            "source_provenance": {
                "sources": ["lexical"],
                "lexical_match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
                "semantic_score": None,
            },
        }
    ]
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": True,
        "lexical_requested": True,
        "semantic_requested": False,
        "scope": {
            "kind": "artifact",
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
        },
        "query": "Alpha beta",
        "query_terms": ["alpha", "beta"],
        "embedding_config_id": None,
        "query_vector_dimensions": 0,
        "limit": 2,
        "lexical_limit": 2,
        "semantic_limit": 0,
        "searched_artifact_count": 1,
        "lexical_candidate_count": 1,
        "semantic_candidate_count": 0,
        "merged_candidate_count": 1,
        "deduplicated_count": 0,
        "included_count": 1,
        "included_lexical_only_count": 1,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 0,
        "excluded_limit_count": 0,
        "matching_rule": "casefolded_unicode_word_overlap_unique_query_terms_v1",
        "similarity_metric": None,
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["notes"])
        and event["payload"]["scope_kind"] == "artifact"
        and event["payload"]["task_artifact_id"] == str(artifact_scope["artifact_ids"]["notes"])
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert trace_events[-1]["payload"]["artifact_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_retrieval_scope_kind"] == "artifact"
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 1
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 1
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["included_artifact_chunk_count"] == 1
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 0
    assert trace_events[-1]["payload"]["excluded_artifact_chunk_limit_count"] == 0
    assert trace_events[-1]["payload"]["excluded_uningested_artifact_count"] == 0


def test_compile_context_hybrid_artifact_merge_preserves_dual_source_provenance_and_limits(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        thread_id=seeded["thread_id"],
    )
    config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["docs"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["notes"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["weak"],
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "artifact_retrieval": {
                "kind": "task",
                "task_id": str(artifact_scope["task_id"]),
                "query": "Alpha beta",
                "limit": 2,
            },
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_id": str(artifact_scope["task_id"]),
                "embedding_config_id": str(config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["context_pack"]["artifact_chunks"] == [
        {
            "id": str(artifact_scope["chunk_ids"]["docs"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["docs"]),
            "relative_path": "docs/a.txt",
            "media_type": "text/plain",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 14,
            "text": "beta alpha doc",
            "source_provenance": {
                "sources": ["lexical", "semantic"],
                "lexical_match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
                "semantic_score": 1.0,
            },
        },
        {
            "id": str(artifact_scope["chunk_ids"]["notes"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 15,
            "text": "alpha beta note",
            "source_provenance": {
                "sources": ["lexical", "semantic"],
                "lexical_match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
                "semantic_score": 1.0,
            },
        },
    ]
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": True,
        "lexical_requested": True,
        "semantic_requested": True,
        "scope": {"kind": "task", "task_id": str(artifact_scope["task_id"])},
        "query": "Alpha beta",
        "query_terms": ["alpha", "beta"],
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "limit": 2,
        "lexical_limit": 2,
        "semantic_limit": 2,
        "searched_artifact_count": 3,
        "lexical_candidate_count": 3,
        "semantic_candidate_count": 3,
        "merged_candidate_count": 3,
        "deduplicated_count": 3,
        "included_count": 2,
        "included_lexical_only_count": 0,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 2,
        "excluded_uningested_artifact_count": 1,
        "excluded_limit_count": 1,
        "matching_rule": "casefolded_unicode_word_overlap_unique_query_terms_v1",
        "similarity_metric": "cosine_similarity",
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "hybrid_artifact_chunk_deduplicated"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["docs"])
        and event["payload"]["selected_sources"] == ["lexical", "semantic"]
        and event["payload"]["score"] == 1.0
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["notes"])
        and event["payload"]["selected_sources"] == ["lexical", "semantic"]
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_artifact_chunk_limit_exceeded"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["weak"])
        and event["payload"]["selected_sources"] == ["lexical", "semantic"]
        and event["payload"]["score"] == 0.0
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert trace_events[-1]["payload"]["artifact_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_retrieval_scope_kind"] == "task"
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 3
    assert trace_events[-1]["payload"]["included_artifact_chunk_count"] == 2
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 2
    assert trace_events[-1]["payload"]["excluded_artifact_chunk_limit_count"] == 1
    assert trace_events[-1]["payload"]["excluded_uningested_artifact_count"] == 1


def test_compile_context_semantic_artifact_retrieval_integrates_chunks_traces_and_exclusion_rules(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        thread_id=seeded["thread_id"],
    )
    config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["docs"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["notes"],
        embedding_config_id=config_id,
        vector=[1.0, 0.0, 0.0],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["weak"],
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_id": str(artifact_scope["task_id"]),
                "embedding_config_id": str(config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["context_pack"]["artifact_chunks"] == [
        {
            "id": str(artifact_scope["chunk_ids"]["docs"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["docs"]),
            "relative_path": "docs/a.txt",
            "media_type": "text/plain",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 14,
            "text": "beta alpha doc",
            "source_provenance": {
                "sources": ["semantic"],
                "lexical_match": None,
                "semantic_score": 1.0,
            },
        },
        {
            "id": str(artifact_scope["chunk_ids"]["notes"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 15,
            "text": "alpha beta note",
            "source_provenance": {
                "sources": ["semantic"],
                "lexical_match": None,
                "semantic_score": 1.0,
            },
        },
    ]
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": True,
        "lexical_requested": False,
        "semantic_requested": True,
        "scope": {"kind": "task", "task_id": str(artifact_scope["task_id"])},
        "query": None,
        "query_terms": [],
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "limit": 2,
        "lexical_limit": 0,
        "semantic_limit": 2,
        "searched_artifact_count": 3,
        "lexical_candidate_count": 0,
        "semantic_candidate_count": 3,
        "merged_candidate_count": 3,
        "deduplicated_count": 0,
        "included_count": 2,
        "included_lexical_only_count": 0,
        "included_semantic_only_count": 2,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 1,
        "excluded_limit_count": 1,
        "matching_rule": None,
        "similarity_metric": "cosine_similarity",
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["docs"])
        and event["payload"]["relative_path"] == "docs/a.txt"
        and event["payload"]["score"] == 1.0
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["notes"])
        and event["payload"]["relative_path"] == "notes/b.md"
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_artifact_chunk_limit_exceeded"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["weak"])
        and event["payload"]["relative_path"] == "notes/c.txt"
        and event["payload"]["score"] == 0.0
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert any(
        event["payload"]["reason"] == "hybrid_artifact_not_ingested"
        and event["payload"]["entity_id"] == str(artifact_scope["artifact_ids"]["pending"])
        and event["payload"]["relative_path"] == "notes/hidden.txt"
        and event["payload"]["ingestion_status"] == "pending"
        for event in trace_events
        if event["kind"] == "context.excluded"
    )
    assert trace_events[-1]["payload"]["artifact_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_retrieval_scope_kind"] == "task"
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 3
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["included_artifact_chunk_count"] == 2
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 0
    assert trace_events[-1]["payload"]["excluded_artifact_chunk_limit_count"] == 1
    assert trace_events[-1]["payload"]["excluded_uningested_artifact_count"] == 1


def test_compile_context_semantic_artifact_scoped_retrieval_returns_only_visible_artifact_chunks(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        thread_id=seeded["thread_id"],
    )
    config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
    )
    seed_task_artifact_chunk_embedding_for_user(
        migrated_database_urls["app"],
        user_id=seeded["user_id"],
        task_artifact_chunk_id=artifact_scope["chunk_ids"]["notes"],
        embedding_config_id=config_id,
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "artifact",
                "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
                "embedding_config_id": str(config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )

    assert status_code == 200
    assert payload["context_pack"]["artifact_chunks"] == [
        {
            "id": str(artifact_scope["chunk_ids"]["notes"]),
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
            "relative_path": "notes/b.md",
            "media_type": "text/markdown",
            "sequence_no": 1,
            "char_start": 0,
            "char_end_exclusive": 15,
            "text": "alpha beta note",
            "source_provenance": {
                "sources": ["semantic"],
                "lexical_match": None,
                "semantic_score": 1.0,
            },
        }
    ]
    assert payload["context_pack"]["artifact_chunk_summary"] == {
        "requested": True,
        "lexical_requested": False,
        "semantic_requested": True,
        "scope": {
            "kind": "artifact",
            "task_id": str(artifact_scope["task_id"]),
            "task_artifact_id": str(artifact_scope["artifact_ids"]["notes"]),
        },
        "query": None,
        "query_terms": [],
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "limit": 2,
        "lexical_limit": 0,
        "semantic_limit": 2,
        "searched_artifact_count": 1,
        "lexical_candidate_count": 0,
        "semantic_candidate_count": 1,
        "merged_candidate_count": 1,
        "deduplicated_count": 0,
        "included_count": 1,
        "included_lexical_only_count": 0,
        "included_semantic_only_count": 1,
        "included_dual_source_count": 0,
        "excluded_uningested_artifact_count": 0,
        "excluded_limit_count": 0,
        "matching_rule": None,
        "similarity_metric": "cosine_similarity",
        "source_precedence": ["lexical", "semantic"],
        "lexical_order": [
            "matched_query_term_count_desc",
            "first_match_char_start_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
        "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
        "merged_order": [
            "source_precedence_asc",
            "lexical_rank_asc",
            "semantic_rank_asc",
            "relative_path_asc",
            "sequence_no_asc",
            "id_asc",
        ],
    }

    trace_id = UUID(payload["trace_id"])
    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        trace_events = ContinuityStore(conn).list_trace_events(trace_id)

    assert any(
        event["payload"]["reason"] == "within_hybrid_artifact_chunk_limit"
        and event["payload"]["entity_id"] == str(artifact_scope["chunk_ids"]["notes"])
        and event["payload"]["scope_kind"] == "artifact"
        and event["payload"]["task_artifact_id"] == str(artifact_scope["artifact_ids"]["notes"])
        for event in trace_events
        if event["kind"] == "context.included"
    )
    assert trace_events[-1]["payload"]["artifact_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_retrieval_scope_kind"] == "artifact"
    assert trace_events[-1]["payload"]["artifact_lexical_retrieval_requested"] is False
    assert trace_events[-1]["payload"]["artifact_semantic_retrieval_requested"] is True
    assert trace_events[-1]["payload"]["artifact_lexical_candidate_count"] == 0
    assert trace_events[-1]["payload"]["artifact_semantic_candidate_count"] == 1
    assert trace_events[-1]["payload"]["artifact_merged_candidate_count"] == 1
    assert trace_events[-1]["payload"]["artifact_deduplicated_count"] == 0
    assert trace_events[-1]["payload"]["included_artifact_chunk_count"] == 1
    assert trace_events[-1]["payload"]["included_dual_source_artifact_chunk_count"] == 0
    assert trace_events[-1]["payload"]["excluded_artifact_chunk_limit_count"] == 0
    assert trace_events[-1]["payload"]["excluded_uningested_artifact_count"] == 0


def test_compile_context_semantic_artifact_retrieval_validation_and_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_traceable_thread(migrated_database_urls["app"])
    intruder = seed_traceable_thread(
        migrated_database_urls["app"],
        email="intruder@example.com",
        display_name="Intruder",
    )
    owner_artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
    )
    owner_config_id = seed_embedding_config_for_user(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
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

    invalid_shape_status, invalid_shape_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_artifact_id": str(owner_artifact_scope["artifact_ids"]["docs"]),
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0, 0.0],
            },
        }
    )
    missing_status, missing_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_id": str(owner_artifact_scope["task_id"]),
                "embedding_config_id": str(uuid4()),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )
    mismatch_status, mismatch_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_id": str(owner_artifact_scope["task_id"]),
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0],
                "limit": 2,
            },
        }
    )
    isolated_task_status, isolated_task_payload = invoke_compile_context(
        {
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "task",
                "task_id": str(owner_artifact_scope["task_id"]),
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )
    isolated_artifact_status, isolated_artifact_payload = invoke_compile_context(
        {
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "semantic_artifact_retrieval": {
                "kind": "artifact",
                "task_artifact_id": str(owner_artifact_scope["artifact_ids"]["docs"]),
                "embedding_config_id": str(owner_config_id),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 2,
            },
        }
    )

    assert invalid_shape_status == 422
    assert "task_id" in json.dumps(invalid_shape_payload)
    assert missing_status == 400
    assert missing_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert mismatch_status == 400
    assert mismatch_payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}
    assert isolated_task_status == 404
    assert isolated_task_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert isolated_artifact_status == 404
    assert isolated_artifact_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_compile_context_artifact_retrieval_validation_and_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_traceable_thread(migrated_database_urls["app"])
    intruder = seed_traceable_thread(
        migrated_database_urls["app"],
        email="intruder@example.com",
        display_name="Intruder",
    )
    owner_artifact_scope = seed_compile_artifact_scope(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
        thread_id=owner["thread_id"],
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

    blank_query_status, blank_query_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "artifact_retrieval": {
                "kind": "task",
                "task_id": str(owner_artifact_scope["task_id"]),
                "query": "   ",
                "limit": 2,
            },
        }
    )
    invalid_shape_status, invalid_shape_payload = invoke_compile_context(
        {
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "artifact_retrieval": {
                "kind": "task",
                "task_artifact_id": str(owner_artifact_scope["artifact_ids"]["docs"]),
                "query": "alpha beta",
            },
        }
    )
    isolated_task_status, isolated_task_payload = invoke_compile_context(
        {
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "artifact_retrieval": {
                "kind": "task",
                "task_id": str(owner_artifact_scope["task_id"]),
                "query": "alpha beta",
                "limit": 2,
            },
        }
    )
    isolated_artifact_status, isolated_artifact_payload = invoke_compile_context(
        {
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "artifact_retrieval": {
                "kind": "artifact",
                "task_artifact_id": str(owner_artifact_scope["artifact_ids"]["docs"]),
                "query": "alpha beta",
                "limit": 2,
            },
        }
    )

    assert blank_query_status == 400
    assert blank_query_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert invalid_shape_status == 422
    assert "task_id" in json.dumps(invalid_shape_payload)
    assert isolated_task_status == 404
    assert isolated_task_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert isolated_artifact_status == 404
    assert isolated_artifact_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_traces_and_trace_events_respect_per_user_isolation(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_traceable_thread(migrated_database_urls["app"])
    owner_id = seeded["user_id"]
    thread_id = seeded["thread_id"]
    owner_event_ids = seeded["event_ids"]
    owner_entities = seeded["entities"]
    owner_entity_edges = seeded["entity_edges"]
    intruder_id = uuid4()
    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(intruder_id, "intruder@example.com", "Intruder")
        intruder_thread = store.create_thread("Intruder thread")
        intruder_session = store.create_session(intruder_thread["id"], status="active")
        intruder_event = store.append_event(
            intruder_thread["id"],
            intruder_session["id"],
            "message.user",
            {"text": "intruder memory"},
        )
        store.create_memory(
            memory_key="user.preference.coffee",
            value={"likes": "black"},
            status="active",
            source_event_ids=[str(intruder_event["id"])],
        )
        intruder_memory = store.create_memory(
            memory_key="user.preference.tea",
            value={"likes": "green"},
            status="active",
            source_event_ids=[str(intruder_event["id"])],
        )
        store.create_entity(
            entity_type="merchant",
            name="Intruder Cafe",
            source_memory_ids=[str(intruder_memory["id"])],
        )
        intruder_project = store.create_entity(
            entity_type="project",
            name="Intruder Project",
            source_memory_ids=[str(intruder_memory["id"])],
        )
        store.create_entity_edge(
            from_entity_id=intruder_project["id"],
            to_entity_id=store.list_entities()[0]["id"],
            relationship_type="hidden_from_owner",
            valid_from=None,
            valid_to=None,
            source_memory_ids=[str(intruder_memory["id"])],
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

    status_code, payload = invoke_compile_context(
        {
            "user_id": str(owner_id),
            "thread_id": str(thread_id),
        }
    )

    assert status_code == 200
    trace_id = UUID(payload["trace_id"])
    assert [memory["source_event_ids"] for memory in payload["context_pack"]["memories"]] == [
        [str(owner_event_ids[0])],
        [str(owner_event_ids[1])],
    ]
    assert [memory["source_provenance"] for memory in payload["context_pack"]["memories"]] == [
        {"sources": ["symbolic"], "semantic_score": None},
        {"sources": ["symbolic"], "semantic_score": None},
    ]
    assert [entity["id"] for entity in payload["context_pack"]["entities"]] == [
        str(entity["id"]) for entity in owner_entities
    ]
    assert [edge["id"] for edge in payload["context_pack"]["entity_edges"]] == [
        str(edge["id"]) for edge in owner_entity_edges
    ]

    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        store = ContinuityStore(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM traces WHERE id = %s", (trace_id,))
            trace_count = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS count FROM trace_events WHERE trace_id = %s", (trace_id,))
            trace_event_count = cur.fetchone()

        assert trace_count["count"] == 0
        assert trace_event_count["count"] == 0
        assert store.list_trace_events(trace_id) == []


def test_compile_context_scopes_memories_to_active_thread_profile(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "scoped@example.com", "Scoped User")
        assistant_thread = store.create_thread("Assistant thread")
        coach_thread = store.create_thread("Coach thread", agent_profile_id="coach_default")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        coach_session = store.create_session(coach_thread["id"], status="active")
        assistant_event = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "remember assistant preference"},
        )
        coach_event = store.append_event(
            coach_thread["id"],
            coach_session["id"],
            "message.user",
            {"text": "remember coaching preference"},
        )
        # Omitted profile attribution must default deterministically to assistant_default.
        store.create_memory(
            memory_key="user.preference.assistant.profile_scope",
            value={"likes": "espresso"},
            status="active",
            source_event_ids=[str(assistant_event["id"])],
        )
        store.create_memory(
            memory_key="user.preference.coach.profile_scope",
            value={"likes": "pour over"},
            status="active",
            source_event_ids=[str(coach_event["id"])],
            agent_profile_id="coach_default",
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

    assistant_status, assistant_payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(assistant_thread["id"]),
            "max_memories": 10,
        }
    )
    coach_status, coach_payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(coach_thread["id"]),
            "max_memories": 10,
        }
    )

    assert assistant_status == 200
    assert assistant_payload["metadata"] == {"agent_profile_id": "assistant_default"}
    assert [memory["memory_key"] for memory in assistant_payload["context_pack"]["memories"]] == [
        "user.preference.assistant.profile_scope"
    ]

    assert coach_status == 200
    assert coach_payload["metadata"] == {"agent_profile_id": "coach_default"}
    assert [memory["memory_key"] for memory in coach_payload["context_pack"]["memories"]] == [
        "user.preference.coach.profile_scope"
    ]


def test_compile_context_scopes_semantic_memory_retrieval_to_active_thread_profile(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "semantic-scope@example.com", "Semantic Scope")
        assistant_thread = store.create_thread("Assistant semantic thread")
        coach_thread = store.create_thread("Coach semantic thread", agent_profile_id="coach_default")
        assistant_session = store.create_session(assistant_thread["id"], status="active")
        coach_session = store.create_session(coach_thread["id"], status="active")
        assistant_event = store.append_event(
            assistant_thread["id"],
            assistant_session["id"],
            "message.user",
            {"text": "assistant memory evidence"},
        )
        coach_event = store.append_event(
            coach_thread["id"],
            coach_session["id"],
            "message.user",
            {"text": "coach memory evidence"},
        )
        assistant_memory = store.create_memory(
            memory_key="user.preference.semantic.assistant_scope",
            value={"likes": "latte"},
            status="active",
            source_event_ids=[str(assistant_event["id"])],
        )
        coach_memory = store.create_memory(
            memory_key="user.preference.semantic.coach_scope",
            value={"likes": "flat white"},
            status="active",
            source_event_ids=[str(coach_event["id"])],
            agent_profile_id="coach_default",
        )
        embedding_config = store.create_embedding_config(
            provider="openai",
            model="text-embedding-3-large",
            version="2026-03-24",
            dimensions=3,
            status="active",
            metadata={"task": "semantic_scope_validation"},
        )
        store.create_memory_embedding(
            memory_id=assistant_memory["id"],
            embedding_config_id=embedding_config["id"],
            dimensions=3,
            vector=[1.0, 0.0, 0.0],
        )
        store.create_memory_embedding(
            memory_id=coach_memory["id"],
            embedding_config_id=embedding_config["id"],
            dimensions=3,
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

    assistant_status, assistant_payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(assistant_thread["id"]),
            "max_memories": 10,
            "semantic": {
                "embedding_config_id": str(embedding_config["id"]),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 10,
            },
        }
    )
    coach_status, coach_payload = invoke_compile_context(
        {
            "user_id": str(user_id),
            "thread_id": str(coach_thread["id"]),
            "max_memories": 10,
            "semantic": {
                "embedding_config_id": str(embedding_config["id"]),
                "query_vector": [1.0, 0.0, 0.0],
                "limit": 10,
            },
        }
    )

    assert assistant_status == 200
    assert [memory["memory_key"] for memory in assistant_payload["context_pack"]["memories"]] == [
        "user.preference.semantic.assistant_scope"
    ]

    assert coach_status == 200
    assert [memory["memory_key"] for memory in coach_payload["context_pack"]["memories"]] == [
        "user.preference.semantic.coach_scope"
    ]
