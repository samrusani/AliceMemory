from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from alicebot_api import vnext_embeddings
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import (
    SQLiteVNextStore,
    _PROJECT_UPDATE_EVENT_LOOKUP_SQL,
    ensure_sqlite_user,
    sqlite_user_connection,
)
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_capture import capture_dedupe_key_for_text
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    memory_embedding_content_sha256,
    memory_embedding_signature_is_current,
    pad_embedding_vector,
)
from alicebot_api.vnext_stores.sqlite import vector_scan
from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS
from alicebot_api.mcp_tools import redact_memory_flow
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError
from alicebot_api.vnext_project_update_guard import PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE


def _open_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    return conn


def _make_store(conn: sqlite3.Connection, *, email: str | None = None) -> SQLiteVNextStore:
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, email or f"{user_id}@example.com", "Test User")
    return SQLiteVNextStore(conn, user_id)


def _create_source(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "source_type": "document",
        "title": "Spec",
        "content_hash": f"sha256:{uuid4()}",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"path": "docs/spec.md"},
    }
    source.update(overrides)
    return store.create_source(source)


def _create_memory(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    memory: dict[str, object] = {
        "memory_key": f"memory.{uuid4()}",
        "value": {"text": "Alice remembers"},
        "status": "active",
        "title": "Alice memory",
        "canonical_text": "Alice remembers everything important",
        "summary": "a memory",
        "domain": "project",
        "sensitivity": "private",
    }
    memory.update(overrides)
    return store.create_memory(memory)


# -- schema ------------------------------------------------------------------


def test_bootstrap_sqlite_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    bootstrap_sqlite_schema(conn)
    bootstrap_sqlite_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    for expected in (
        "users",
        "sources",
        "source_chunks",
        "memories",
        "memory_revisions",
        "provenance_links",
        "open_loops",
        "event_log",
        "agent_identities",
        "agent_api_keys",
        "browser_clip_capabilities",
        "memories_fts",
        "source_chunks_fts",
    ):
        assert expected in tables
    conn.close()


def test_bootstrap_sqlite_schema_is_idempotent_across_file_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "alice.db"
    for _ in range(2):
        conn = sqlite3.connect(str(db_path))
        bootstrap_sqlite_schema(conn)
        conn.commit()
        conn.close()
    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    conn.close()


def test_ensure_sqlite_user_is_idempotent() -> None:
    conn = _open_connection()
    user_id = str(uuid4())
    first = ensure_sqlite_user(conn, user_id, "sam@example.com", "Sam")
    second = ensure_sqlite_user(conn, user_id, "sam@example.com", "Sam")
    assert first["id"] == user_id
    assert second == first
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    conn.close()


def test_sqlite_user_connection_commits_on_success_and_rolls_back_on_error(tmp_path: Path) -> None:
    db_path = tmp_path / "alice.db"
    user_id = str(uuid4())

    with sqlite_user_connection(db_path, user_id) as conn:
        ensure_sqlite_user(conn, user_id, "sam@example.com", "Sam")
        store = SQLiteVNextStore(conn, user_id)
        committed = _create_source(store, content_hash="sha256:committed")

    with pytest.raises(RuntimeError, match="boom"):
        with sqlite_user_connection(db_path, user_id) as conn:
            store = SQLiteVNextStore(conn, user_id)
            _create_source(store, content_hash="sha256:rolled-back")
            raise RuntimeError("boom")

    with sqlite_user_connection(db_path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        assert store.get_source_by_content_hash("sha256:committed") is not None
        assert store.get_source_by_content_hash("sha256:rolled-back") is None
        assert store.get_source(committed["id"]) is not None


def test_sqlite_user_connection_rejects_empty_user_id(tmp_path: Path) -> None:
    with pytest.raises(ContinuityStoreInvariantError):
        with sqlite_user_connection(tmp_path / "alice.db", ""):
            pass  # pragma: no cover
    with pytest.raises(ContinuityStoreInvariantError):
        SQLiteVNextStore(sqlite3.connect(":memory:"), " ")


def test_update_source_recomputes_dedupe_key_from_prospective_identity() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    raw_text = "Fact: Source review can reclassify captured evidence."
    original_key = capture_dedupe_key_for_text(
        raw_text,
        ("Alpha",),
        domain="project",
        sensitivity="private",
    )
    source = _create_source(
        store,
        dedupe_key=original_key,
        metadata_json={"raw_text": raw_text, "project_scope": ["Alpha"]},
    )

    updated = store.update_source(
        source_id=str(source["id"]),
        patch={
            "domain": "professional",
            "metadata_json": {"raw_text": raw_text, "project_scope": ["Beta"]},
        },
    )

    assert updated["dedupe_key"] == capture_dedupe_key_for_text(
        raw_text,
        ("Beta",),
        domain="professional",
        sensitivity="private",
    )
    assert updated["content_hash"] != source["content_hash"]
    assert updated["dedupe_key"] != original_key
    assert any(
        event["event_type"] == "source.updated" and event["target_id"] == source["id"] for event in store.list_events()
    )
    conn.close()


def test_update_source_releases_key_when_changed_identity_cannot_be_recomputed() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    source = _create_source(
        store,
        dedupe_key="capture-md5:legacy-without-raw-text",
        metadata_json={"project_scope": ["Alpha"]},
    )

    updated = store.update_source(
        source_id=str(source["id"]),
        patch={"metadata_json": {"project_scope": ["Beta"]}},
    )

    assert updated["dedupe_key"] is None
    conn.close()


def test_update_source_title_only_preserves_legacy_content_and_dedupe_identity() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    raw_text = "Fact: Legacy source hashes remain stable on unrelated review edits."
    source = _create_source(
        store,
        content_hash="sha256:legacy-unscoped-public-identity",
        dedupe_key=capture_dedupe_key_for_text(
            raw_text,
            ("Alpha",),
            domain="project",
            sensitivity="private",
        ),
        metadata_json={"raw_text": raw_text, "project_scope": ["Alpha"]},
    )

    updated = store.update_source(
        source_id=str(source["id"]),
        patch={"title": "Reviewed title"},
    )

    assert updated["content_hash"] == source["content_hash"]
    assert updated["dedupe_key"] == source["dedupe_key"]
    conn.close()


def test_update_source_identity_collision_fails_without_mutating_sqlite_row() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    raw_text = "Fact: A live capture identity has exactly one owner."
    first = _create_source(
        store,
        dedupe_key=capture_dedupe_key_for_text(
            raw_text,
            ("Alpha",),
            domain="project",
            sensitivity="private",
        ),
        metadata_json={"raw_text": raw_text, "project_scope": ["Alpha"]},
    )
    _create_source(
        store,
        dedupe_key=capture_dedupe_key_for_text(
            raw_text,
            ("Beta",),
            domain="project",
            sensitivity="private",
        ),
        metadata_json={"raw_text": raw_text, "project_scope": ["Beta"]},
    )

    with pytest.raises(ContinuityStoreInvariantError, match="already belongs"):
        store.update_source(
            source_id=str(first["id"]),
            patch={"metadata_json": {"raw_text": raw_text, "project_scope": ["Beta"]}},
        )

    persisted = store.get_source(str(first["id"]))
    assert persisted is not None
    assert persisted["metadata_json"]["project_scope"] == ["Alpha"]
    assert persisted["dedupe_key"] == first["dedupe_key"]
    conn.close()


def test_get_or_create_source_rejects_incompatible_sqlite_conflict_winner() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    dedupe_key = "capture-md5:stale-winner"
    _create_source(
        store,
        content_hash="sha256:same",
        dedupe_key=dedupe_key,
        metadata_json={"project_scope": ["Beta"]},
    )

    with pytest.raises(ContinuityStoreInvariantError, match="does not match capture identity"):
        store.get_or_create_source(
            {
                "source_type": "document",
                "content_hash": "sha256:same",
                "dedupe_key": dedupe_key,
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_scope": ["Alpha"]},
            }
        )
    conn.close()


# -- user scoping --------------------------------------------------------------


def test_every_read_method_is_scoped_to_the_bound_user() -> None:
    conn = _open_connection()
    alice = _make_store(conn)
    mallory = _make_store(conn)

    source = _create_source(alice, content_hash="sha256:scoped")
    chunk = alice.create_source_chunk({"source_id": source["id"], "chunk_index": 0, "text": "chunk text"})
    memory = _create_memory(alice, canonical_text="Kubernetes deployment pipeline notes")
    alice.update_memory_embedding(memory_id=memory["id"], vector=[1.0, 0.0, 0.0])
    revision = alice.append_revision(
        {"memory_id": memory["id"], "memory_key": memory["memory_key"], "text_after": "after"}
    )
    alice.create_provenance_link({"target_type": "memory", "target_id": memory["id"], "source_id": source["id"]})
    loop = alice.create_open_loop({"title": "Ship the SQLite on-ramp"})
    alice.upsert_agent_identity({"agent_id": "hermes"})
    key = alice.create_agent_api_key(
        {
            "agent_id": "hermes",
            "permission_profile": "read_only_agent",
            "key_hash": "a" * 64,
            "key_prefix": "ak_live_a",
        }
    )

    # Alice sees her own data.
    assert alice.get_memory(memory["id"]) is not None
    assert alice.list_memories()
    assert alice.list_events()
    assert [row["id"] for row in alice.list_source_chunks(source["id"])] == [chunk["id"]]
    assert [row["id"] for row in alice.search_source_chunks(query="chunk")] == [chunk["id"]]

    # Mallory must see none of it, on every read method.
    assert mallory.list_events() == []
    assert mallory.list_events(target_type="memory", target_id=str(memory["id"])) == []
    assert mallory.get_source(source["id"]) is None
    assert mallory.get_source_by_content_hash("sha256:scoped") is None
    assert mallory.list_source_chunks(source["id"]) == []
    assert mallory.search_sources(query="spec") == []
    assert mallory.search_source_chunks(query="chunk") == []
    assert mallory.search_source_chunks(query="chunk", match_any=True) == []
    assert mallory.get_memory(memory["id"]) is None
    assert mallory.list_memories() == []
    assert mallory.list_memories(status="active") == []
    assert mallory.search_memories(query="kubernetes") == []
    assert mallory.search_memories_fts(query="kubernetes") == []
    assert mallory.search_memories_vector(query_vector=[1.0, 0.0, 0.0]) == []
    assert mallory.list_revisions(memory["id"]) == []
    assert mallory.list_provenance_links(target_type="memory", target_id=str(memory["id"])) == []
    assert mallory.list_open_loops() == []
    assert mallory.get_open_loop(loop["id"]) is None
    assert mallory.get_agent_api_key_by_hash("a" * 64) is None
    assert mallory.list_agent_api_keys() == []
    assert mallory.count_active_agent_api_keys() == 0
    assert mallory.list_agent_identities() == []
    assert mallory.list_agent_events() == []

    # Mallory cannot mutate Alice's rows either.
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.update_memory(memory_id=memory["id"], patch={"title": "stolen"})
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.update_open_loop(loop_id=loop["id"], patch={"title": "stolen"})
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.update_open_loop_status(loop_id=loop["id"], status="resolved")
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.touch_agent_api_key(key_id=key["id"])
    assert mallory.revoke_agent_api_key(key_id=key["id"]) is None
    assert mallory.update_memory_embedding(memory_id=memory["id"], vector=[1.0]) is None

    # Nothing Mallory attempted altered Alice's view.
    assert alice.get_memory(memory["id"])["title"] == memory["title"]
    assert alice.count_active_agent_api_keys() == 1
    assert [row["id"] for row in alice.list_revisions(memory["id"])] == [revision["id"]]
    conn.close()


def test_open_loop_digest_lookup_applies_project_and_person_scope_before_result() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    expected = store.create_open_loop(
        {
            "title": "Scoped loop",
            "project_id": "project-a",
            "person_id": "person-a",
            "metadata_json": {"automation_digest": "same-digest"},
        }
    )
    store.create_open_loop(
        {
            "title": "Other loop",
            "project_id": "project-b",
            "person_id": "person-b",
            "metadata_json": {"automation_digest": "same-digest"},
        }
    )

    matched = store.find_open_loop_by_automation_digest(
        digest="same-digest",
        project_id="project-a",
        person_id="person-a",
    )
    assert matched is not None
    assert matched["id"] == expected["id"]
    assert (
        store.find_open_loop_by_automation_digest(
            digest="same-digest",
            project_id="project-a",
            person_id="person-b",
        )
        is None
    )
    conn.close()


def test_project_scoped_memory_and_rollup_queries_filter_before_limit() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    project_a = _create_memory(
        store,
        canonical_text="Project A stale input",
        memory_type="project_state",
        last_confirmed_at="2020-01-01T00:00:00Z",
        metadata_json={"project_scope": ["project-a"]},
    )
    _create_memory(
        store,
        canonical_text="Project B newer input",
        memory_type="project_state",
        last_confirmed_at="2020-01-01T00:00:00Z",
        metadata_json={"project_scope": ["project-b"]},
    )
    pending_a = _create_memory(
        store,
        status="candidate",
        metadata_json={
            "project_scope": ["project-a"],
            "candidate_kind": "memory_rollup",
            "rollup_digest": "digest-a",
        },
    )
    _create_memory(
        store,
        status="candidate",
        metadata_json={
            "project_scope": ["project-b"],
            "candidate_kind": "memory_rollup",
            "rollup_digest": "digest-b",
        },
    )
    accepted_a = _create_memory(
        store,
        metadata_json={
            "project_scope": ["project-a"],
            "candidate_kind": "memory_rollup",
            "rollup_key": "topic:a",
        },
    )

    assert [row["id"] for row in store.list_memories(projects=("project-a",), limit=1)] == [accepted_a["id"]]
    assert store.count_memories(status="active", projects=("project-a",)) == 2
    stale = store.list_memories_for_staleness_sweep(
        reference_time=datetime(2026, 1, 1, tzinfo=UTC),
        confirmation_before=datetime(2025, 1, 1, tzinfo=UTC),
        review_memory_types=("project_state",),
        projects=("project-a",),
        limit=1,
    )
    assert [row["id"] for row in stale] == [project_a["id"]]
    pending = store.list_pending_rollup_candidates(
        rollup_digests=("digest-a", "digest-b"),
        domains=None,
        sensitivity_allowed=["private"],
        candidate_kind="memory_rollup",
        projects=("project-a",),
        limit=2,
    )
    assert [row["id"] for row in pending] == [pending_a["id"]]
    accepted = store.list_accepted_rollup_cards(
        rollup_keys=("topic:a",),
        domains=None,
        sensitivity_allowed=["private"],
        candidate_kind="memory_rollup",
        projects=("project-a",),
        limit=1,
    )
    assert [row["id"] for row in accepted] == [accepted_a["id"]]
    conn.close()


# -- capture-shaped flow --------------------------------------------------------


def test_capture_flow_source_chunks_memory_provenance_and_events() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    source = _create_source(store, content_hash="sha256:capture")
    assert store.get_source_by_content_hash("sha256:capture")["id"] == source["id"]

    chunk_one = store.create_source_chunk(
        {"source_id": source["id"], "chunk_index": 0, "text": "first", "token_count": 1}
    )
    chunk_two = store.create_source_chunk(
        {"source_id": source["id"], "chunk_index": 1, "text": "second", "metadata_json": {"kind": "para"}}
    )
    chunks = store.list_source_chunks(source["id"])
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
    assert chunks[0]["id"] == chunk_one["id"]
    assert chunks[1]["metadata_json"] == {"kind": "para"}

    memory = _create_memory(store, memory_type="decision", status="active")
    assert memory["memory_type"] == "decision"
    assert memory["value"] == {"text": "Alice remembers"}
    assert memory["source_event_ids"] == []
    assert memory["metadata_json"] == {}
    assert isinstance(memory["id"], str)
    assert isinstance(memory["created_at"], str)
    assert memory["created_at"].endswith("Z")

    link = store.create_provenance_link(
        {
            "target_type": "memory",
            "target_id": memory["id"],
            "source_id": source["id"],
            "source_chunk_id": chunk_one["id"],
            "quote": "first",
        }
    )
    assert link["evidence_role"] == "supports"
    assert link["confidence"] == 0.5
    links = store.list_provenance_links(target_type="memory", target_id=str(memory["id"]))
    assert [row["id"] for row in links] == [link["id"]]

    event_types = [event["event_type"] for event in store.list_events()]
    assert event_types.count("source.created") == 1
    assert event_types.count("source_chunk.created") == 2
    assert event_types.count("memory.created") == 1
    assert event_types.count("provenance_link.created") == 1

    memory_events = store.list_events(target_type="memory", target_id=str(memory["id"]))
    assert {event["event_type"] for event in memory_events} == {
        "memory.created",
        "provenance_link.created",
    }
    limited = store.list_events(limit=2)
    assert len(limited) == 2
    conn.close()


def test_mutation_events_carry_payload_and_integrity_fields() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    source = _create_source(store)
    events = store.list_events(target_type="source", target_id=str(source["id"]))
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "source.created"
    assert event["actor_type"] == "system"
    assert event["payload_json"]["operation"] == "create"
    assert "content_hash" in event["payload_json"]["fields"]
    assert event["integrity_hash"]
    assert event["user_id"] == store.user_id
    conn.close()


# -- event log append-only --------------------------------------------------------


def test_event_log_rejects_update_and_delete() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    _create_source(store)
    assert conn.execute("SELECT count(*) FROM event_log").fetchone()[0] >= 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE event_log SET event_type = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event_log")
    conn.close()


def test_memory_revisions_reject_update_and_delete() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store)
    store.append_revision({"memory_id": memory["id"], "memory_key": memory["memory_key"], "text_after": "x"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE memory_revisions SET reason = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM memory_revisions")
    conn.close()


# -- memory CRUD and constraints ---------------------------------------------------


def test_create_memory_rejects_invalid_enum_values() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, memory_type="not_a_type")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, status="not_a_status")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, domain="not_a_domain")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, sensitivity="not_a_sensitivity")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, trust_class="not_a_trust_class")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, confidence=1.5)
    conn.close()


def test_duplicate_memory_key_for_same_profile_is_rejected() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    _create_memory(store, memory_key="memory.duplicate")
    with pytest.raises(sqlite3.IntegrityError):
        _create_memory(store, memory_key="memory.duplicate")
    conn.close()


def test_update_memory_applies_patch_and_archives() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, status="candidate")

    updated = store.update_memory(
        memory_id=memory["id"],
        patch={
            "status": "active",
            "title": "Updated title",
            "project_id": "project-new",
            "metadata_json": {
                "reviewed": True,
                "project_id": "project-new",
                "project_scope": ["project-new"],
            },
        },
    )
    assert updated["status"] == "active"
    assert updated["title"] == "Updated title"
    assert updated["project_id"] == "project-new"
    assert updated["project_scope"] == ["project-new"]
    assert updated["metadata_json"] == {
        "reviewed": True,
        "project_id": "project-new",
        "project_scope": ["project-new"],
    }
    assert updated["canonical_text"] == memory["canonical_text"]
    assert updated["updated_at"] >= memory["updated_at"]

    update_events = [
        event
        for event in store.list_events(target_type="memory", target_id=str(memory["id"]))
        if event["event_type"] == "memory.updated"
    ]
    assert len(update_events) == 1
    assert update_events[0]["payload_json"]["changes"]["status"] == "active"

    archived = store.update_memory(memory_id=memory["id"], patch={"status": "archived"})
    assert archived["deleted_at"] is not None
    assert store.get_memory(memory["id"]) is None
    assert store.list_memories() == []

    with pytest.raises(ContinuityStoreInvariantError):
        store.update_memory(memory_id=memory["id"], patch={"title": "gone"})
    with pytest.raises(ContinuityStoreInvariantError):
        store.update_memory(memory_id=str(uuid4()), patch={"title": "missing"})
    conn.close()


def test_list_memories_filters_by_status_and_orders_by_recency() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    first = _create_memory(store, status="candidate")
    second = _create_memory(store, status="active")
    store.update_memory(memory_id=first["id"], patch={"salience": 0.9})

    everything = store.list_memories()
    assert {row["id"] for row in everything} == {first["id"], second["id"]}
    assert everything[0]["id"] == first["id"]  # most recently updated first

    candidates = store.list_memories(status="candidate")
    assert [row["id"] for row in candidates] == [first["id"]]
    conn.close()


def test_list_memories_applies_scope_and_limit_in_query() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    _create_memory(store, domain="project", sensitivity="private", title="older project")
    expected = _create_memory(store, domain="unknown", sensitivity="private", title="unknown is in scope")
    _create_memory(store, domain="personal", sensitivity="private", title="wrong domain")
    _create_memory(store, domain="project", sensitivity="public", title="wrong sensitivity")

    rows = store.list_memories(
        status="active",
        domains=["project"],
        sensitivity_allowed=["private"],
        limit=1,
    )

    assert [row["id"] for row in rows] == [expected["id"]]
    assert store.list_memories(status="active", sensitivity_allowed=[]) == []
    with pytest.raises(ValueError, match="limit must be positive"):
        store.list_memories(limit=0)
    conn.close()


def test_memory_queries_use_ascii_case_insensitive_literal_substrings() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    rows = {
        "title": _create_memory(store, title="Release title", canonical_text="unrelated canonical text"),
        "canonical": _create_memory(store, title="Unrelated title", canonical_text="Release canonical text"),
        "summary": _create_memory(
            store,
            title="Unrelated title",
            canonical_text="unrelated canonical text",
            summary="Release summary",
        ),
        "arende": _create_memory(store, title="Ärende row", canonical_text="unrelated canonical text"),
        "strasse": _create_memory(store, title="Straße row", canonical_text="unrelated canonical text"),
        "literals": _create_memory(
            store,
            title=r"100% under_score path\segment",
            canonical_text="unrelated canonical text",
        ),
    }
    for index, row in enumerate(rows.values()):
        store.append_event(
            {
                "id": f"memory-query-event-{index}",
                "event_type": "memory.reviewed",
                "actor_type": "system",
                "target_type": "memory",
                "target_id": row["id"],
                "occurred_at": f"2030-07-10T12:{index:02d}:00Z",
                "payload_json": {},
            }
        )

    expectations = {
        "release": {str(rows[key]["id"]) for key in ("title", "canonical", "summary")},
        "RELEASE": {str(rows[key]["id"]) for key in ("title", "canonical", "summary")},
        "ärende": set(),
        "Ärende": {str(rows["arende"]["id"])},
        "STRASSE": set(),
        "Straße": {str(rows["strasse"]["id"])},
        "%": {str(rows["literals"]["id"])},
        "_": {str(rows["literals"]["id"])},
        "\\": {str(rows["literals"]["id"])},
        r"missing%_\path": set(),
    }
    for query, expected_ids in expectations.items():
        memories = store.list_memories(query=query, order_by_created_at=True, limit=50)
        resume_events = store.list_resume_memory_events(statuses=("active",), query=query, limit=100)
        assert {str(row["id"]) for row in memories} == expected_ids
        assert {str(event["target_id"]) for event in resume_events} == expected_ids

    assert len(store.list_memories(query="   ", limit=50)) == len(rows)
    assert {
        str(event["target_id"])
        for event in store.list_resume_memory_events(statuses=("active",), query="   ", limit=100)
    } == {str(row["id"]) for row in rows.values()}
    conn.close()


def test_sqlite_project_update_event_lookup_preserves_target_and_payload_only_linkage() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    artifact_id = "artifact-1"
    candidate_memory_id = "memory-1"
    expected_ids = {
        "creation-target",
        "creation-payload",
        "accepted-target",
        "rejected-payload",
    }
    events = (
        {
            "id": "creation-target",
            "event_type": "project.update_candidate_created",
            "target_type": "artifact",
            "target_id": artifact_id,
            "payload_json": {
                "artifact_id": artifact_id,
                "candidate_memory_id": candidate_memory_id,
                "memory_id": candidate_memory_id,
            },
        },
        {
            "id": "creation-payload",
            "event_type": "project.update_candidate_created",
            "target_type": "artifact",
            "target_id": "competing-artifact",
            "payload_json": {"memory_id": candidate_memory_id},
        },
        {
            "id": "accepted-target",
            "event_type": "project.update_candidate_accepted",
            "target_type": "memory",
            "target_id": candidate_memory_id,
            "payload_json": {},
        },
        {
            "id": "rejected-payload",
            "event_type": "project.update_candidate_rejected",
            "target_type": "project",
            "target_id": "project-1",
            "payload_json": {"artifact_id": artifact_id},
        },
        {
            "id": "unrelated-project-update",
            "event_type": "project.update_candidate_rejected",
            "target_type": "artifact",
            "target_id": "artifact-2",
            "payload_json": {"candidate_memory_id": "memory-2"},
        },
        {
            "id": "wrong-event-type",
            "event_type": "memory.reviewed",
            "target_type": "artifact",
            "target_id": artifact_id,
            "payload_json": {"candidate_memory_id": candidate_memory_id},
        },
    )
    for index, event in enumerate(events):
        store.append_event(
            {
                **event,
                "actor_type": "system",
                "occurred_at": f"2030-07-10T12:{index:02d}:00Z",
            }
        )

    actual = store.list_project_update_events(
        artifact_id=artifact_id,
        candidate_memory_id=candidate_memory_id,
    )

    assert {str(event["id"]) for event in actual} == expected_ids
    assert len(actual) == len(expected_ids)
    assert [str(event["id"]) for event in actual] == [
        "rejected-payload",
        "accepted-target",
        "creation-payload",
        "creation-target",
    ]

    plan_rows = conn.execute(
        f"EXPLAIN QUERY PLAN {_PROJECT_UPDATE_EVENT_LOOKUP_SQL}",
        (
            store.user_id,
            artifact_id,
            store.user_id,
            candidate_memory_id,
            store.user_id,
            artifact_id,
            store.user_id,
            candidate_memory_id,
            store.user_id,
            candidate_memory_id,
        ),
    ).fetchall()
    plan = "\n".join(str(row[3]) for row in plan_rows)
    assert plan.count("USING INDEX event_log_project_update_target_idx") == 2
    assert plan.count("USING INDEX event_log_project_update_artifact_id_idx") == 1
    assert plan.count("USING INDEX event_log_project_update_candidate_memory_id_idx") == 1
    assert plan.count("USING INDEX event_log_project_update_memory_id_idx") == 1
    assert "event_log_user_occurred_idx" not in plan
    conn.close()


@pytest.mark.parametrize("marker", ["workflow", "memory_key"])
@pytest.mark.parametrize("operation", ["correct", "forget", "undo", "redact"])
def test_sqlite_pending_project_update_candidate_blocks_generic_memory_mutations(
    marker: str,
    operation: str,
) -> None:
    conn = _open_connection()
    store = _make_store(conn)
    metadata: dict[str, object] = {"candidate": True}
    memory_key = "ordinary.pending.candidate"
    if marker == "workflow":
        metadata["workflow"] = "project_auto_update"
    else:
        memory_key = "project_update.alice.digest"
    memory = _create_memory(
        store,
        memory_key=memory_key,
        status="candidate",
        metadata_json=metadata,
        canonical_text="Proposed project state.",
    )
    state_before = (
        store.get_memory(str(memory["id"])),
        store.list_revisions(str(memory["id"])),
        store.list_events(),
    )

    with pytest.raises(
        VNextMemoryCommitValidationError,
        match=f"^{PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE}$",
    ):
        service = VNextMemoryCommitService(store)
        if operation == "correct":
            service.correct(
                identity=None,
                memory_id=str(memory["id"]),
                canonical_text="Generic correction must not apply.",
            )
        elif operation == "forget":
            service.forget(identity=None, memory_id=str(memory["id"]), reason="Must not apply.")
        elif operation == "undo":
            service.undo(identity=None, memory_id=str(memory["id"]), reason="Must not apply.")
        else:
            redact_memory_flow(store, memory_id=str(memory["id"]), reason="Must not apply.")

    assert (
        store.get_memory(str(memory["id"])),
        store.list_revisions(str(memory["id"])),
        store.list_events(),
    ) == state_before
    conn.close()


# -- revisions -----------------------------------------------------------------------


def test_append_revision_assigns_sequential_numbers_and_lists_in_order() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store)

    first = store.append_revision(
        {
            "memory_id": memory["id"],
            "memory_key": memory["memory_key"],
            "new_value": {"text": "v1"},
            "revision_type": "created",
            "text_after": "v1",
        }
    )
    second = store.append_revision(
        {
            "memory_id": memory["id"],
            "memory_key": memory["memory_key"],
            "previous_value": {"text": "v1"},
            "new_value": {"text": "v2"},
            "text_after": "v2",
            "reason": "correction",
        }
    )

    assert first["sequence_no"] == 1
    assert first["revision_number"] == 1
    assert first["revision_type"] == "created"
    assert second["sequence_no"] == 2
    assert second["revision_number"] == 2
    assert second["revision_type"] == "edited"
    assert second["previous_value"] == {"text": "v1"}
    assert second["new_value"] == {"text": "v2"}
    assert second["candidate"] == {}

    listed = store.list_revisions(memory["id"])
    assert [row["id"] for row in listed] == [first["id"], second["id"]]

    revision_events = [
        event
        for event in store.list_events(target_type="memory", target_id=str(memory["id"]))
        if event["event_type"] == "memory_revision.created"
    ]
    assert len(revision_events) == 2
    conn.close()


# -- keyword (LIKE) search --------------------------------------------------------------


def test_search_memories_like_fallback_matches_text_and_json_value() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    match = _create_memory(
        store,
        title="Deployment runbook",
        canonical_text="The Kubernetes deployment pipeline uses ArgoCD",
        value={"tool": "argocd"},
    )
    _create_memory(store, title="Groceries", canonical_text="Buy oat milk", value={"list": True})
    _create_memory(store, title="Hidden candidate", status="candidate", canonical_text="ArgoCD secrets")

    rows = store.search_memories(query="argocd")
    assert [row["id"] for row in rows] == [match["id"]]

    quoted = store.search_memories(query='"Kubernetes deployment"')
    assert [row["id"] for row in quoted] == [match["id"]]

    # value::text JSON match
    value_rows = store.search_memories(query="tool")
    assert [row["id"] for row in value_rows] == [match["id"]]
    conn.close()


def test_search_memories_applies_domain_and_sensitivity_filters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    project = _create_memory(store, canonical_text="shared keyword alpha", domain="project", sensitivity="private")
    unknown = _create_memory(store, canonical_text="shared keyword beta", domain="unknown", sensitivity="private")
    _create_memory(store, canonical_text="shared keyword gamma", domain="health", sensitivity="highly_sensitive")

    rows = store.search_memories(
        query="shared keyword",
        domains=["project"],
        sensitivity_allowed=["private", "internal"],
    )
    assert {row["id"] for row in rows} == {project["id"], unknown["id"]}  # unknown domain passes through
    conn.close()


def test_search_sources_matches_metadata_and_ranks_title_first() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    titled = _create_source(store, title="Retrieval design", content_hash="sha256:t1")
    metadata_only = _create_source(
        store,
        title="Other",
        content_hash="sha256:t2",
        metadata_json={"topic": "retrieval design"},
    )
    rows = store.search_sources(query="retrieval design")
    assert [row["id"] for row in rows][0] == titled["id"]
    assert {row["id"] for row in rows} == {titled["id"], metadata_only["id"]}
    conn.close()


# -- FTS search ---------------------------------------------------------------------------


def test_search_memories_fts_ranks_relevant_memory_first() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    relevant = _create_memory(
        store,
        title="Kubernetes deployment",
        canonical_text="Kubernetes deployment pipeline promotes builds through staging",
        summary="Kubernetes deployment notes",
    )
    _create_memory(
        store,
        title="Cooking",
        canonical_text="Slow-cooked ragu recipe with pasta",
        summary="dinner ideas",
    )
    weaker = _create_memory(
        store,
        title="Infra weekly",
        canonical_text="Notes that mention deployment once",
        summary="weekly notes",
    )

    rows = store.search_memories_fts(query="kubernetes deployment")
    assert [row["id"] for row in rows] == [relevant["id"]]

    rows = store.search_memories_fts(query="deployment")
    assert {row["id"] for row in rows} == {relevant["id"], weaker["id"]}
    assert rows[0]["id"] == relevant["id"]
    assert all("fts_score" in row for row in rows)
    assert rows[0]["fts_score"] >= rows[-1]["fts_score"]
    conn.close()


def test_search_memories_fts_supports_quoted_phrases() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    phrase = _create_memory(store, canonical_text="continuity layer for AI agents")
    _create_memory(store, canonical_text="agents crave a layer of continuity eventually")

    rows = store.search_memories_fts(query='"continuity layer"')
    assert [row["id"] for row in rows] == [phrase["id"]]
    conn.close()


def test_search_memories_fts_is_safe_against_fts5_metacharacters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, canonical_text="deployment notes with NEAR misses")

    hostile_queries = [
        'col:*(NEAR "unclosed AND ^',
        "a AND OR NOT (",
        '"unbalanced',
        "title:deployment*",
        "x ^ y NEAR/3 z",
        "(((((",
        "-deployment",
        '"" "" ""',
    ]
    for hostile in hostile_queries:
        rows = store.search_memories_fts(query=hostile)  # must not raise
        assert isinstance(rows, list)

    # Sanitized empty queries return no rows instead of erroring.
    assert store.search_memories_fts(query="") == []
    assert store.search_memories_fts(query="   ") == []
    assert store.search_memories_fts(query="()^*:") == []

    # A hostile query containing a real term still matches.
    rows = store.search_memories_fts(query="deployment:*(")
    assert [row["id"] for row in rows] == [memory["id"]]
    conn.close()


def test_search_memories_fts_match_any_ors_terms_the_strict_pass_misses() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(
        store,
        canonical_text="Decision: Alice public announcement goes out Monday after the pre-launch audit passes.",
    )

    # Strict AND semantics demand every non-stopword term: "go" never
    # appears (the text says "goes"), so the natural-language question
    # returns nothing.
    question = "When does the Alice public announcement go out?"
    assert store.search_memories_fts(query=question) == []

    rows = store.search_memories_fts(query=question, match_any=True)
    assert [row["id"] for row in rows] == [memory["id"]]

    # Single-term queries behave identically under both modes.
    strict = store.search_memories_fts(query="announcement")
    relaxed = store.search_memories_fts(query="announcement", match_any=True)
    assert [row["id"] for row in strict] == [row["id"] for row in relaxed] == [memory["id"]]
    conn.close()


def test_search_memories_fts_match_any_is_safe_against_fts5_metacharacters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, canonical_text="deployment notes with NEAR misses")

    hostile_queries = [
        'col:*(NEAR "unclosed AND ^',
        "a AND OR NOT (",
        '"unbalanced',
        "x ^ y NEAR/3 z",
        "-deployment stray",
    ]
    for hostile in hostile_queries:
        rows = store.search_memories_fts(query=hostile, match_any=True)  # must not raise
        assert isinstance(rows, list)

    # Sanitized empty queries return no rows instead of erroring.
    assert store.search_memories_fts(query="", match_any=True) == []
    assert store.search_memories_fts(query="()^*:", match_any=True) == []

    # A leading '-' is stripped, not parsed as NOT: the real term still ORs.
    rows = store.search_memories_fts(query="-deployment unrelatedterm", match_any=True)
    assert [row["id"] for row in rows] == [memory["id"]]
    conn.close()


def test_search_memories_fts_reflects_updates_and_archives() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, canonical_text="original searchable phrase")

    store.update_memory(memory_id=memory["id"], patch={"canonical_text": "replacement wording"})
    assert store.search_memories_fts(query="searchable") == []
    assert [row["id"] for row in store.search_memories_fts(query="replacement")] == [memory["id"]]

    store.update_memory(memory_id=memory["id"], patch={"status": "archived"})
    assert store.search_memories_fts(query="replacement") == []
    conn.close()


def test_search_memories_fts_applies_domain_and_sensitivity_filters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    visible = _create_memory(store, canonical_text="filtered term", domain="project", sensitivity="private")
    _create_memory(store, canonical_text="filtered term", domain="health", sensitivity="sacred")

    rows = store.search_memories_fts(
        query="filtered",
        domains=["project"],
        sensitivity_allowed=["private"],
    )
    assert [row["id"] for row in rows] == [visible["id"]]
    conn.close()


def test_search_memories_fts_applies_people_and_time_scope_before_limit() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    _create_memory(
        store,
        canonical_text="scope predicate deployment",
        metadata_json={"people": ["Alex"]},
        valid_from="2026-06-01T00:00:00Z",
    )
    valid = _create_memory(
        store,
        canonical_text="scope predicate deployment",
        metadata_json={"people": ["Sam"]},
        valid_from="2026-07-08T00:00:00Z",
    )

    rows = store.search_memories_fts(
        query="scope predicate deployment",
        limit=1,
        scope_people=("sam",),
        scope_window_start=datetime(2026, 7, 3, tzinfo=UTC),
        scope_window_end=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert [row["id"] for row in rows] == [valid["id"]]
    conn.close()


def test_search_memories_fts_applies_end_only_scope_window() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    before_end = _create_memory(
        store,
        canonical_text="end-only predicate rehearsal",
        valid_from="2019-06-01T00:00:00Z",
    )
    _create_memory(
        store,
        canonical_text="end-only predicate rehearsal",
        valid_from="2022-06-01T00:00:00Z",
    )

    rows = store.search_memories_fts(
        query="end-only predicate rehearsal",
        scope_window_end=datetime(2020, 12, 31, 23, 59, 59, tzinfo=UTC),
    )

    assert [row["id"] for row in rows] == [before_end["id"]]
    conn.close()


def test_bulk_retrieval_resolvers_return_complete_graph_and_provenance_rows() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memories = [_create_memory(store, canonical_text=f"Bulk memory {index}") for index in range(3)]
    sources = [_create_source(store, content_hash=f"sha256:bulk-{index}") for index in range(3)]
    entity = store.create_entity({"entity_type": "person", "name": "Sam", "normalized_name": "sam"})
    for memory, source in zip(memories, sources, strict=True):
        store.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": memory["id"],
                "to_type": "entity",
                "to_id": entity["id"],
                "edge_type": "mentions",
            }
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory["id"],
                "source_id": source["id"],
            }
        )

    assert {row["id"] for row in store.get_memories_by_ids([row["id"] for row in memories])} == {
        row["id"] for row in memories
    }
    assert {row["id"] for row in store.get_sources_by_ids([row["id"] for row in sources])} == {
        row["id"] for row in sources
    }
    assert len(store.list_memory_entity_edges(entity_ids=[str(entity["id"])])) == 3
    assert (
        len(
            store.list_provenance_links_for_targets(
                target_type="memory",
                target_ids=[str(memory["id"]) for memory in memories],
            )
        )
        == 3
    )
    conn.close()


# -- source-chunk FTS search ---------------------------------------------------------


def test_search_source_chunks_matches_content_the_title_search_cannot_see() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    early = _create_source(store, title="Chat session 12", content_hash="sha256:early")
    hit = store.create_source_chunk(
        {
            "source_id": early["id"],
            "chunk_index": 0,
            "text": "I adopted a golden retriever named Biscuit last weekend.",
        }
    )
    recent = _create_source(store, title="Chat session 99", content_hash="sha256:recent")
    store.create_source_chunk(
        {"source_id": recent["id"], "chunk_index": 0, "text": "Spreadsheet formulas and pivot tables."}
    )

    # The content-blind search_sources cannot find the answer session...
    assert store.search_sources(query="golden retriever Biscuit") == []
    # ...the chunk search can, and every row carries source_id + fts_score.
    rows = store.search_source_chunks(query="golden retriever Biscuit")
    assert [row["id"] for row in rows] == [hit["id"]]
    assert rows[0]["source_id"] == early["id"]
    assert "fts_score" in rows[0]
    conn.close()


def test_search_source_chunks_ranks_best_hit_first_and_respects_limit() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    source = _create_source(store, content_hash="sha256:ranked")
    strong = store.create_source_chunk(
        {"source_id": source["id"], "chunk_index": 0, "text": "deployment deployment pipeline"}
    )
    store.create_source_chunk(
        {
            "source_id": source["id"],
            "chunk_index": 1,
            "text": "one deployment mention buried in a much longer paragraph about many unrelated things",
        }
    )

    rows = store.search_source_chunks(query="deployment")
    assert len(rows) == 2
    assert rows[0]["id"] == strong["id"]
    assert rows[0]["fts_score"] >= rows[-1]["fts_score"]

    assert len(store.search_source_chunks(query="deployment", limit=1)) == 1
    conn.close()


def test_search_source_chunks_match_any_ors_terms_the_strict_pass_misses() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    source = _create_source(store, content_hash="sha256:fallback")
    chunk = store.create_source_chunk(
        {
            "source_id": source["id"],
            "chunk_index": 0,
            "text": "Decision: Alice public announcement goes out Monday after the pre-launch audit passes.",
        }
    )

    # Strict AND semantics demand every non-stopword term: "go" never
    # appears (the text says "goes"), so the question returns nothing.
    question = "When does the Alice public announcement go out?"
    assert store.search_source_chunks(query=question) == []

    rows = store.search_source_chunks(query=question, match_any=True)
    assert [row["id"] for row in rows] == [chunk["id"]]

    # Single-term queries behave identically under both modes.
    strict = store.search_source_chunks(query="announcement")
    relaxed = store.search_source_chunks(query="announcement", match_any=True)
    assert [row["id"] for row in strict] == [row["id"] for row in relaxed] == [chunk["id"]]
    conn.close()


def test_search_source_chunks_is_safe_against_fts5_metacharacters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    source = _create_source(store, content_hash="sha256:hostile")
    chunk = store.create_source_chunk(
        {"source_id": source["id"], "chunk_index": 0, "text": "deployment notes with NEAR misses"}
    )

    hostile_queries = [
        'col:*(NEAR "unclosed AND ^',
        "a AND OR NOT (",
        '"unbalanced',
        "title:deployment*",
        "x ^ y NEAR/3 z",
        "(((((",
        "-deployment",
    ]
    for hostile in hostile_queries:
        for match_any in (False, True):
            rows = store.search_source_chunks(query=hostile, match_any=match_any)  # must not raise
            assert isinstance(rows, list)

    # Sanitized empty queries return no rows instead of erroring.
    assert store.search_source_chunks(query="") == []
    assert store.search_source_chunks(query="()^*:") == []
    assert store.search_source_chunks(query="()^*:", match_any=True) == []

    # A hostile query containing a real term still matches.
    rows = store.search_source_chunks(query="deployment:*(")
    assert [row["id"] for row in rows] == [chunk["id"]]
    conn.close()


def test_search_source_chunks_applies_parent_source_domain_sensitivity_and_deletion() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    visible = _create_source(store, content_hash="sha256:visible", domain="project", sensitivity="private")
    store.create_source_chunk({"source_id": visible["id"], "chunk_index": 0, "text": "shared gate keyword"})
    hidden = _create_source(store, content_hash="sha256:hidden", domain="health", sensitivity="sacred")
    store.create_source_chunk({"source_id": hidden["id"], "chunk_index": 0, "text": "shared gate keyword"})

    rows = store.search_source_chunks(
        query="gate keyword",
        domains=["project"],
        sensitivity_allowed=["private"],
    )
    assert [row["source_id"] for row in rows] == [visible["id"]]

    # Chunks of a soft-deleted source disappear from content search.
    conn.execute(
        "UPDATE sources SET deleted_at = '2026-07-01T00:00:00Z' WHERE id = ?",
        (visible["id"],),
    )
    assert store.search_source_chunks(query="gate keyword", domains=["project"], sensitivity_allowed=["private"]) == []
    conn.close()


def test_source_chunks_fts_backfills_existing_rows_when_index_first_appears(tmp_path: Path) -> None:
    # Simulate a database file written before the source_chunks_fts table
    # shipped: drop the index and its sync triggers, then re-bootstrap.
    db_path = tmp_path / "alice.db"
    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "backfill@example.com", "Backfill")
    store = SQLiteVNextStore(conn, user_id)
    source = _create_source(store, content_hash="sha256:backfill")
    chunk = store.create_source_chunk(
        {"source_id": source["id"], "chunk_index": 0, "text": "historic rows must stay searchable"}
    )
    for trigger in (
        "source_chunks_fts_after_insert",
        "source_chunks_fts_after_delete",
        "source_chunks_fts_after_update",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE source_chunks_fts")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    store = SQLiteVNextStore(conn, user_id)
    rows = store.search_source_chunks(query="historic searchable")
    assert [row["id"] for row in rows] == [chunk["id"]]
    conn.close()


# -- memory_type / project / staleness read filters ------------------------------------


def test_search_memories_filters_by_memory_type_across_all_three_methods() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    decision = _create_memory(store, memory_type="decision", canonical_text="shared retrieval keyword decision")
    procedure = _create_memory(store, memory_type="procedure", canonical_text="shared retrieval keyword procedure")
    _create_memory(store, memory_type="preference", canonical_text="shared retrieval keyword preference")
    store.update_memory_embedding(memory_id=decision["id"], vector=[1.0, 0.0])
    store.update_memory_embedding(memory_id=procedure["id"], vector=[0.0, 1.0])

    like_rows = store.search_memories(query="shared retrieval", memory_types=("decision", "procedure"))
    assert {row["id"] for row in like_rows} == {decision["id"], procedure["id"]}

    fts_rows = store.search_memories_fts(query="retrieval", memory_types=("decision",))
    assert [row["id"] for row in fts_rows] == [decision["id"]]

    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0], memory_types=("procedure",))
    assert [row["id"] for row in vector_rows] == [procedure["id"]]

    # Empty tuple means no filter.
    assert len(store.search_memories(query="shared retrieval", memory_types=())) == 3
    conn.close()


def test_search_memories_filters_by_project_id_in_metadata_json() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    alicebot = _create_memory(
        store,
        canonical_text="project keyword alicebot",
        metadata_json={"project_id": "alicebot"},
    )
    hermes = _create_memory(
        store,
        canonical_text="project keyword hermes",
        metadata_json={"project_id": "hermes"},
    )
    shared = _create_memory(
        store,
        canonical_text="project keyword shared",
        project_id="alicebot",
        project_scope=["alicebot", "hermes"],
    )
    unscoped = _create_memory(store, canonical_text="project keyword unscoped")
    store.update_memory_embedding(memory_id=alicebot["id"], vector=[1.0, 0.0])
    store.update_memory_embedding(memory_id=hermes["id"], vector=[0.0, 1.0])
    store.update_memory_embedding(memory_id=shared["id"], vector=[0.8, 0.2])

    rows = store.search_memories(query="project keyword", projects=("alicebot",))
    assert {row["id"] for row in rows} == {alicebot["id"], shared["id"]}

    rows = store.search_memories(query="project keyword", projects=("alicebot", "hermes"))
    assert {row["id"] for row in rows} == {alicebot["id"], hermes["id"], shared["id"]}

    fts_rows = store.search_memories_fts(query="keyword", projects=("hermes",))
    assert {row["id"] for row in fts_rows} == {hermes["id"], shared["id"]}

    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0], projects=("alicebot",))
    assert {row["id"] for row in vector_rows} == {alicebot["id"], shared["id"]}
    assert shared["project_scope"] == ["alicebot", "hermes"]
    stored_shared_metadata = conn.execute(
        "SELECT metadata_json FROM memories WHERE id = ?", (shared["id"],)
    ).fetchone()[0]
    assert json.loads(stored_shared_metadata)["project_scope"] == ["alicebot", "hermes"]

    # No projects filter returns everything, including unscoped memories.
    rows = store.search_memories(query="project keyword")
    assert {row["id"] for row in rows} == {
        alicebot["id"],
        hermes["id"],
        shared["id"],
        unscoped["id"],
    }
    conn.close()


def test_search_memories_project_filter_prefers_the_real_column_over_metadata() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    column_scoped = _create_memory(
        store,
        canonical_text="scope column keyword one",
        project_id="alicebot",
    )
    metadata_scoped = _create_memory(
        store,
        canonical_text="scope column keyword two",
        metadata_json={"project_id": "alicebot"},
    )
    # Column wins over stale metadata when both are present.
    column_wins = _create_memory(
        store,
        canonical_text="scope column keyword three",
        project_id="hermes",
        metadata_json={"project_id": "alicebot"},
    )

    rows = store.search_memories(query="scope column keyword", projects=("alicebot",))
    assert {row["id"] for row in rows} == {column_scoped["id"], metadata_scoped["id"]}

    rows = store.search_memories(query="scope column keyword", projects=("hermes",))
    assert [row["id"] for row in rows] == [column_wins["id"]]
    conn.close()


def test_create_memory_persists_scope_columns_and_rows_return_them() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    scoped = _create_memory(
        store,
        canonical_text="scoped fact",
        project_id="alicebot",
        created_by_agent_id="openclaw",
        run_id="run-2026-07-04-001",
    )
    unscoped = _create_memory(store, canonical_text="unscoped fact")

    fetched = store.get_memory(scoped["id"])
    assert fetched is not None
    assert fetched["project_id"] == "alicebot"
    assert fetched["created_by_agent_id"] == "openclaw"
    assert fetched["run_id"] == "run-2026-07-04-001"
    plain = store.get_memory(unscoped["id"])
    assert plain is not None
    assert plain["project_id"] is None
    assert plain["created_by_agent_id"] is None
    assert plain["run_id"] is None
    conn.close()


def test_search_memories_filters_by_created_by_agent_and_run_across_all_three_methods() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    openclaw_run_1 = _create_memory(
        store,
        canonical_text="agent scope keyword alpha",
        created_by_agent_id="openclaw",
        run_id="run-1",
    )
    openclaw_run_2 = _create_memory(
        store,
        canonical_text="agent scope keyword beta",
        created_by_agent_id="openclaw",
        run_id="run-2",
    )
    hermes = _create_memory(
        store,
        canonical_text="agent scope keyword gamma",
        created_by_agent_id="hermes",
        run_id="run-3",
    )
    user_written = _create_memory(store, canonical_text="agent scope keyword delta")
    store.update_memory_embedding(memory_id=openclaw_run_1["id"], vector=[1.0, 0.0])
    store.update_memory_embedding(memory_id=hermes["id"], vector=[0.0, 1.0])

    rows = store.search_memories(query="agent scope keyword", created_by_agent_ids=("openclaw",))
    assert {row["id"] for row in rows} == {openclaw_run_1["id"], openclaw_run_2["id"]}

    rows = store.search_memories(query="agent scope keyword", created_by_agent_ids=("openclaw", "hermes"))
    assert {row["id"] for row in rows} == {openclaw_run_1["id"], openclaw_run_2["id"], hermes["id"]}

    rows = store.search_memories(query="agent scope keyword", run_id="run-2")
    assert [row["id"] for row in rows] == [openclaw_run_2["id"]]

    fts_rows = store.search_memories_fts(query="keyword", created_by_agent_ids=("hermes",))
    assert [row["id"] for row in fts_rows] == [hermes["id"]]
    fts_rows = store.search_memories_fts(query="keyword", run_id="run-1")
    assert [row["id"] for row in fts_rows] == [openclaw_run_1["id"]]

    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0], created_by_agent_ids=("openclaw",))
    assert [row["id"] for row in vector_rows] == [openclaw_run_1["id"]]
    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0], run_id="run-3")
    assert [row["id"] for row in vector_rows] == [hermes["id"]]

    # No filter returns every writer's memories, including the user's own.
    rows = store.search_memories(query="agent scope keyword")
    assert {row["id"] for row in rows} == {
        openclaw_run_1["id"],
        openclaw_run_2["id"],
        hermes["id"],
        user_written["id"],
    }
    conn.close()


def test_create_agent_api_key_round_trips_project_scope_binding() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    bound = store.create_agent_api_key(
        {
            "agent_id": "openclaw",
            "permission_profile": "project_scoped_agent",
            "project_scope": "alicebot",
            "key_hash": "c" * 64,
            "key_prefix": "alice_sk_ghi",
        }
    )
    unbound = store.create_agent_api_key(
        {
            "agent_id": "hermes",
            "permission_profile": "trusted_local_agent",
            "key_hash": "d" * 64,
            "key_prefix": "alice_sk_jkl",
        }
    )

    assert bound["project_scope"] == "alicebot"
    assert unbound["project_scope"] is None
    fetched = store.get_agent_api_key_by_hash("c" * 64)
    assert fetched is not None
    assert fetched["project_scope"] == "alicebot"
    listed = {row["id"]: row for row in store.list_agent_api_keys()}
    assert listed[bound["id"]]["project_scope"] == "alicebot"
    assert listed[unbound["id"]]["project_scope"] is None
    conn.close()


def test_bootstrap_upgrades_a_pre_existing_db_file_with_scope_columns(tmp_path: Path) -> None:
    """Existing sqlite files (created before the scope columns shipped) are
    upgraded in place by the PRAGMA-guarded ALTER TABLE in bootstrap."""
    db_path = tmp_path / "alice.db"
    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    # Rewind the file to the pre-scope-column schema: drop the new index and
    # columns so the file looks like it was created by the old bootstrap.
    for index_name in (
        "memories_user_project_idx",
        "memories_user_project_staleness_idx",
        "memories_user_project_rollup_digest_idx",
        "memories_user_project_rollup_key_idx",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    for table, column in (
        ("memories", "project_id"),
        ("memories", "created_by_agent_id"),
        ("memories", "run_id"),
        ("agent_api_keys", "project_scope"),
    ):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.commit()
    old_memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    assert "project_id" not in old_memory_columns
    conn.close()

    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    assert {"project_id", "created_by_agent_id", "run_id"} <= memory_columns
    key_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_api_keys)")}
    assert "project_scope" in key_columns
    index_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    assert "memories_user_project_idx" in index_names

    # The upgraded file is fully usable, including the new filters.
    store = _make_store(conn)
    scoped = _create_memory(
        store,
        canonical_text="upgraded db keyword",
        project_id="alicebot",
        created_by_agent_id="openclaw",
        run_id="run-1",
    )
    rows = store.search_memories(query="upgraded db keyword", projects=("alicebot",))
    assert [row["id"] for row in rows] == [scoped["id"]]
    rows = store.search_memories(query="upgraded db keyword", created_by_agent_ids=("openclaw",))
    assert [row["id"] for row in rows] == [scoped["id"]]
    conn.close()


def test_pending_confirmation_query_cannot_be_crowded_by_resolved_or_active_rows() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    pending_metadata = {
        "agentic_memory": {
            "kind": "agentic_memory_commit",
            "confirmation": {"status": "pending"},
        }
    }
    for _index in range(5):
        _create_memory(
            store,
            status="active",
            confirmation_status="confirmed",
            metadata_json=pending_metadata,
        )
    actionable = _create_memory(
        store,
        status="needs_review",
        confirmation_status="unconfirmed",
        metadata_json=pending_metadata,
    )
    _create_memory(
        store,
        status="needs_review",
        confirmation_status="confirmed",
        metadata_json={
            "agentic_memory": {
                "kind": "agentic_memory_commit",
                "confirmation": {"status": "confirmed"},
            }
        },
    )

    rows = store.list_pending_inline_confirmations(limit=1)

    assert [row["id"] for row in rows] == [actionable["id"]]
    conn.close()


def test_find_live_memory_by_canonical_text_requires_exact_project_scope() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    expected = _create_memory(
        store,
        canonical_text="Repeated scoped fact",
        metadata_json={"project_scope": ["alpha"]},
    )
    _create_memory(
        store,
        canonical_text="Repeated scoped fact",
        metadata_json={"project_scope": ["beta"]},
    )

    found = store.find_live_memory_by_canonical_text(
        "repeated scoped fact",
        domain="project",
        sensitivity="private",
        project_scope=("alpha",),
    )

    assert found is not None
    assert found["id"] == expected["id"]
    conn.close()


def test_explicit_empty_scope_is_authoritative_across_sqlite_read_predicates() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(
        store,
        canonical_text="Explicit empty scope sentinel",
        project_id="stale-project",
        metadata_json={
            "project_scope": [],
            "project_id": "stale-project",
            "agentic_memory": {"project_scope": ["stale-project"]},
        },
    )
    store.update_memory_embedding(memory_id=memory["id"], vector=[1.0, 0.0])
    source = _create_source(
        store,
        title="Explicit empty source sentinel",
        metadata_json={
            "project_scope": [],
            "project_id": "stale-project",
            "agentic_memory": {"project_scope": ["stale-project"]},
        },
    )
    store.create_source_chunk(
        {
            "source_id": source["id"],
            "chunk_index": 0,
            "text": "explicit empty source sentinel",
        }
    )
    store.create_open_loop(
        {
            "title": "Explicit empty loop sentinel",
            "project_id": "stale-project",
            "metadata_json": {
                "project_scope": [],
                "agentic_memory": {"project_scope": ["stale-project"]},
            },
        }
    )

    assert store.list_memories(projects=("stale-project",)) == []
    assert store.search_memories(query="explicit empty scope sentinel", projects=("stale-project",)) == []
    assert store.search_memories_fts(query="explicit empty scope sentinel", projects=("stale-project",)) == []
    assert store.search_memories_vector(query_vector=[1.0, 0.0], projects=("stale-project",)) == []
    assert (
        store.search_memories_by_time(
            window_start=datetime(2020, 1, 1, tzinfo=UTC),
            window_end=datetime(2030, 1, 1, tzinfo=UTC),
            projects=("stale-project",),
        )
        == []
    )
    assert store.search_sources(query="empty source", scope_projects=("stale-project",)) == []
    assert store.search_source_chunks(query="empty source", scope_projects=("stale-project",)) == []
    assert store.list_open_loops(scope_projects=("stale-project",)) == []
    assert (
        store.find_live_memory_by_canonical_text(
            "Explicit empty scope sentinel",
            domain="project",
            sensitivity="private",
            project_scope=("stale-project",),
        )
        is None
    )
    unscoped = store.find_live_memory_by_canonical_text(
        "Explicit empty scope sentinel",
        domain="project",
        sensitivity="private",
        project_scope=(),
    )
    assert unscoped is not None
    assert unscoped["id"] == memory["id"]


def test_nested_canonical_scope_is_presence_aware_for_sqlite_memories_and_open_loops() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    stale_project = "stale-project"
    scalar_metadata = {
        "project_id": stale_project,
        "agentic_memory": {"project_scope": " Alpha "},
        "agent_identity": {"project_scope": [1, 1e0, 1e1, True]},
    }
    memory = _create_memory(
        store,
        canonical_text="Nested canonical SQLite memory",
        project_id=stale_project,
        metadata_json=scalar_metadata,
    )
    open_loop = store.create_open_loop(
        {
            "title": "Nested canonical SQLite loop",
            "project_id": stale_project,
            "metadata_json": scalar_metadata,
        }
    )

    for nested_key, malformed in (
        ("agentic_memory", []),
        ("agentic_memory", None),
        ("agent_identity", {"leak": stale_project}),
    ):
        metadata = {
            "project_id": stale_project,
            nested_key: {"project_scope": malformed},
        }
        _create_memory(
            store,
            canonical_text=f"Fail closed nested {nested_key} {malformed!r}",
            project_id=stale_project,
            metadata_json=metadata,
        )
        store.create_open_loop(
            {
                "title": f"Fail closed nested loop {nested_key} {malformed!r}",
                "project_id": stale_project,
                "metadata_json": metadata,
            }
        )

    assert store.list_memories(projects=(stale_project,), limit=1) == []
    assert store.list_open_loops(scope_projects=(stale_project,), limit=1) == []
    for accepted in ("alpha", "1", "10", "TRUE"):
        assert [row["id"] for row in store.list_memories(projects=(accepted,), limit=1)] == [memory["id"]]
        assert [row["id"] for row in store.list_open_loops(scope_projects=(accepted,), limit=1)] == [open_loop["id"]]
    conn.close()


def test_persisted_source_envelope_scope_precedes_stale_root_alias_in_sqlite() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    marker = "persisted source envelope sentinel"
    stale_project = "stale-project"
    real_project = "real-project"

    def create_source_with_chunk(
        suffix: str,
        metadata_json: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        source = _create_source(
            store,
            title=f"{marker} {suffix}",
            metadata_json=metadata_json,
        )
        chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": f"{marker} {suffix}",
            }
        )
        return source, chunk

    empty_source, _empty_chunk = create_source_with_chunk(
        "empty",
        {
            "project_id": stale_project,
            "metadata_json": {"project_scope": []},
        },
    )
    real_source, real_chunk = create_source_with_chunk(
        "real",
        {
            "project_id": stale_project,
            "metadata_json": {"project_scope": [real_project]},
        },
    )
    scalar_source, scalar_chunk = create_source_with_chunk(
        "scalar parity",
        {
            "project_id": stale_project,
            "metadata_json": {
                "project_scope": [
                    [" Alpha "],
                    7,
                    True,
                    1.5,
                    {"leak": "wrong-project"},
                    None,
                    " ",
                ]
            },
        },
    )

    def source_ids(project: str) -> set[object]:
        return {
            row["id"]
            for row in store.search_sources(
                query=marker,
                scope_projects=(project,),
                limit=20,
            )
        }

    def chunk_ids(project: str) -> set[object]:
        return {
            row["id"]
            for row in store.search_source_chunks(
                query=marker,
                scope_projects=(project,),
                limit=20,
            )
        }

    assert source_ids(stale_project) == set()
    assert chunk_ids(stale_project) == set()
    assert source_ids(real_project) == {real_source["id"]}
    assert chunk_ids(real_project) == {real_chunk["id"]}
    for scalar_identity in ("alpha", "7", "TRUE"):
        assert source_ids(scalar_identity) == {scalar_source["id"]}
        assert chunk_ids(scalar_identity) == {scalar_chunk["id"]}
    for rejected_identity in ("1.5", "wrong-project"):
        assert source_ids(rejected_identity) == set()
        assert chunk_ids(rejected_identity) == set()
    assert empty_source["id"] not in source_ids(real_project)
    conn.close()


def test_persisted_source_nested_scope_presence_blocks_stale_aliases_in_sqlite() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    marker = "nested source presence sentinel"
    stale_project = "stale-project"

    def create_source_with_chunk(
        suffix: str,
        metadata_json: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        source = _create_source(
            store,
            title=f"{marker} {suffix}",
            metadata_json=metadata_json,
        )
        chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": f"{marker} {suffix}",
            }
        )
        return source, chunk

    valid_source, valid_chunk = create_source_with_chunk(
        "valid",
        {
            "project_id": stale_project,
            "agentic_memory": {"project_scope": " Alpha "},
            "agent_identity": {"project_scope": [7, 1e1, True]},
        },
    )
    invalid_sources: set[object] = set()
    invalid_chunks: set[object] = set()
    for index, nested in enumerate(
        (
            {"agentic_memory": {"project_scope": ["\t\n"]}},
            {"agent_identity": {"project_scope": None}},
            {"agentic_memory": {"project_scope": {"leak": stale_project}}},
            {"agent_identity": {"project_scope": 1.5}},
        )
    ):
        source, chunk = create_source_with_chunk(
            f"invalid-{index}",
            {"project_id": stale_project, **nested},
        )
        invalid_sources.add(source["id"])
        invalid_chunks.add(chunk["id"])

    def source_ids(project: str) -> set[object]:
        return {
            row["id"]
            for row in store.search_sources(
                query=marker,
                scope_projects=(project,),
                limit=20,
            )
        }

    def chunk_ids(project: str) -> set[object]:
        return {
            row["id"]
            for row in store.search_source_chunks(
                query=marker,
                scope_projects=(project,),
                limit=20,
            )
        }

    for accepted in ("alpha", "7", "10", "TRUE"):
        assert source_ids(accepted) == {valid_source["id"]}
        assert chunk_ids(accepted) == {valid_chunk["id"]}
    assert source_ids(stale_project) == set()
    assert chunk_ids(stale_project) == set()
    assert invalid_sources.isdisjoint(source_ids("alpha"))
    assert invalid_chunks.isdisjoint(chunk_ids("alpha"))
    conn.close()


def test_sqlite_scope_identity_is_case_order_whitespace_and_duplicate_insensitive() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(
        store,
        canonical_text="Canonical scope identity sentinel",
        metadata_json={"project_scope": [" Beta ", "ALICE", "alice"]},
    )

    rows = store.list_memories(projects=(" beta ",))
    exact = store.find_live_memory_by_canonical_text(
        "canonical scope identity sentinel",
        domain="project",
        sensitivity="private",
        project_scope=("alice", "BETA", "beta"),
    )

    assert [row["id"] for row in rows] == [memory["id"]]
    assert exact is not None
    assert exact["id"] == memory["id"]


def test_sqlite_scope_identity_preserves_non_ascii_case_and_unicode_whitespace() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    def visible_ids(scope: str) -> set[object]:
        return {row["id"] for row in store.list_memories(projects=(scope,), limit=20)}

    scoped = {
        scope: _create_memory(
            store,
            canonical_text=f"Unicode project scope {index}",
            metadata_json={"project_scope": [scope]},
        )
        for index, scope in enumerate(
            (
                "İ",
                "i",
                "Straße",
                "STRASSE",
                "Σ",
                "σ",
                "ς",
                "\u00a0Alice\u00a0",
                "\u00a0alice\u00a0",
            )
        )
    }

    for scope, memory in scoped.items():
        assert visible_ids(scope) == {memory["id"]}

    assert visible_ids("\t I \n") == {scoped["i"]["id"]}
    assert visible_ids("straße") == set()


def test_sqlite_scope_identity_uses_deterministic_mixed_unicode_order() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(
        store,
        canonical_text="Mixed project identity ordering",
        metadata_json={"project_scope": ["é", "Z", "Ä", "a", "z", "İ", "i"]},
    )

    exact = store.find_live_memory_by_canonical_text(
        "Mixed project identity ordering",
        domain="project",
        sensitivity="private",
        project_scope=("İ", "i", "Ä", "z", "a", "é"),
    )

    assert exact is not None
    assert exact["id"] == memory["id"]


def test_search_memories_excludes_expired_valid_to_unless_include_expired() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    current = _create_memory(store, canonical_text="expiry keyword current")
    future = _create_memory(
        store,
        canonical_text="expiry keyword future",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2999-01-01T00:00:00Z",
    )
    expired = _create_memory(
        store,
        canonical_text="expiry keyword expired",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2021-01-01T00:00:00Z",
    )
    store.update_memory_embedding(memory_id=current["id"], vector=[1.0, 0.0])
    store.update_memory_embedding(memory_id=expired["id"], vector=[0.0, 1.0])

    rows = store.search_memories(query="expiry keyword")
    assert {row["id"] for row in rows} == {current["id"], future["id"]}

    rows = store.search_memories(query="expiry keyword", include_expired=True)
    assert {row["id"] for row in rows} == {current["id"], future["id"], expired["id"]}

    fts_rows = store.search_memories_fts(query="expiry")
    assert {row["id"] for row in fts_rows} == {current["id"], future["id"]}
    fts_rows = store.search_memories_fts(query="expiry", include_expired=True)
    assert {row["id"] for row in fts_rows} == {current["id"], future["id"], expired["id"]}

    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert [row["id"] for row in vector_rows] == [current["id"]]
    vector_rows = store.search_memories_vector(query_vector=[1.0, 0.0], include_expired=True)
    assert {row["id"] for row in vector_rows} == {current["id"], expired["id"]}
    conn.close()


def test_stale_status_is_valid_in_schema_but_excluded_from_search() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    active = _create_memory(store, canonical_text="staleness keyword active")
    stale = _create_memory(store, canonical_text="staleness keyword stale", status="stale")
    store.update_memory_embedding(memory_id=active["id"], vector=[1.0, 0.0])
    store.update_memory_embedding(memory_id=stale["id"], vector=[1.0, 0.0])

    # 'stale' passes the CHECK constraint (Wave-1 sibling writes it) ...
    assert stale["status"] == "stale"
    # ... but the read path treats it like superseded/rejected.
    assert [row["id"] for row in store.search_memories(query="staleness keyword")] == [active["id"]]
    assert [row["id"] for row in store.search_memories_fts(query="staleness")] == [active["id"]]
    assert [row["id"] for row in store.search_memories_vector(query_vector=[1.0, 0.0])] == [active["id"]]
    conn.close()


# -- time-window search ------------------------------------------------------


def test_search_memories_by_time_matches_window_and_orders_by_proximity() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    # Window [2023-03-01, 2023-04-01); center 2023-03-16T12:00.
    closest = _create_memory(store, canonical_text="march event closest", valid_from="2023-03-15T00:00:00Z")
    near_end = _create_memory(store, canonical_text="march event near end", valid_from="2023-03-31T00:00:00Z")
    at_start = _create_memory(store, canonical_text="march event at start", valid_from="2023-03-01T00:00:00Z")
    _create_memory(store, canonical_text="may event outside", valid_from="2023-05-01T00:00:00Z")
    # No valid_from: first_seen_at defaults to write time (now), outside.
    _create_memory(store, canonical_text="undated event")
    # Still-valid closed interval spanning the window joins via overlap;
    # its event start is far from the center, so it ranks last.
    spanning = _create_memory(
        store,
        canonical_text="long running fact",
        valid_from="2022-01-01T00:00:00Z",
        valid_to="2999-01-01T00:00:00Z",
    )

    rows = store.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
    )
    assert [row["id"] for row in rows] == [closest["id"], near_end["id"], at_start["id"], spanning["id"]]

    # An explicit pivot (the closed edge of an open window) reorders by
    # proximity to that edge instead of the midpoint.
    pivoted = store.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
        window_center=datetime(2023, 3, 1, tzinfo=UTC),
    )
    assert [row["id"] for row in pivoted] == [at_start["id"], closest["id"], near_end["id"], spanning["id"]]
    conn.close()


def test_search_memories_by_time_window_is_start_inclusive_end_exclusive() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    at_start = _create_memory(store, canonical_text="boundary start", valid_from="2023-03-01T00:00:00Z")
    _create_memory(store, canonical_text="boundary end", valid_from="2023-04-01T00:00:00Z")

    rows = store.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
    )

    assert [row["id"] for row in rows] == [at_start["id"]]
    conn.close()


def test_search_memories_by_time_falls_back_to_first_seen_at() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    observed = _create_memory(
        store,
        canonical_text="observed in march",
        first_seen_at="2023-03-10T00:00:00Z",
    )
    _create_memory(store, canonical_text="observed elsewhere", first_seen_at="2022-01-01T00:00:00Z")

    rows = store.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
    )

    assert [row["id"] for row in rows] == [observed["id"]]
    conn.close()


def test_search_memories_by_time_applies_scoping_filters_and_limit() -> None:
    conn = _open_connection()
    alice = _make_store(conn)
    mallory = _make_store(conn)
    kwargs = {"valid_from": "2023-03-15T00:00:00Z"}
    matched = _create_memory(alice, canonical_text="scoped march decision", memory_type="decision", **kwargs)
    _create_memory(alice, canonical_text="scoped march candidate", status="candidate", **kwargs)
    _create_memory(alice, canonical_text="scoped march internal", sensitivity="internal", **kwargs)
    _create_memory(alice, canonical_text="scoped march personal", domain="personal", **kwargs)

    rows = alice.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
        domains=["project"],
        sensitivity_allowed=["private"],
        memory_types=("decision",),
    )
    assert [row["id"] for row in rows] == [matched["id"]]

    # Same window, bound to another user: nothing leaks.
    assert (
        mallory.search_memories_by_time(
            window_start=datetime(2023, 3, 1, tzinfo=UTC),
            window_end=datetime(2023, 4, 1, tzinfo=UTC),
        )
        == []
    )

    # Limit keeps the proximity order's head.
    second = _create_memory(
        alice, canonical_text="scoped march later", valid_from="2023-03-01T00:00:00Z", memory_type="decision"
    )
    limited = alice.search_memories_by_time(
        window_start=datetime(2023, 3, 1, tzinfo=UTC),
        window_end=datetime(2023, 4, 1, tzinfo=UTC),
        domains=["project"],
        sensitivity_allowed=["private"],
        memory_types=("decision",),
        limit=1,
    )
    assert [row["id"] for row in limited] == [matched["id"]]
    assert second["id"] not in {row["id"] for row in limited}
    conn.close()


def test_search_memories_by_time_expired_rows_need_include_expired() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    # True only historically: valid_to passed, so the default expiry gate
    # hides it even though its window intersects the queried one.
    expired = _create_memory(
        store,
        canonical_text="expired march fact",
        valid_from="2023-03-10T00:00:00Z",
        valid_to="2023-04-01T00:00:00Z",
    )

    window = {
        "window_start": datetime(2023, 3, 1, tzinfo=UTC),
        "window_end": datetime(2023, 4, 1, tzinfo=UTC),
    }
    assert store.search_memories_by_time(**window) == []
    rows = store.search_memories_by_time(**window, include_expired=True)
    assert [row["id"] for row in rows] == [expired["id"]]
    conn.close()


# -- vector search ---------------------------------------------------------------------


def test_vector_store_and_search_roundtrip_with_dimension_padding() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    close = _create_memory(store, canonical_text="vector close")
    far = _create_memory(store, canonical_text="vector far")
    _create_memory(store, canonical_text="no embedding")

    # Short vectors are zero-padded to the 1536-dim storage convention.
    assert store.update_memory_embedding(memory_id=close["id"], vector=[1.0, 0.0, 0.0]) == {"id": close["id"]}
    assert store.update_memory_embedding(memory_id=far["id"], vector=[0.0, 1.0, 0.0]) == {"id": far["id"]}

    blob = conn.execute("SELECT embedding FROM memories WHERE id = ?", (close["id"],)).fetchone()[0]
    assert len(blob) == 1536 * 4  # float32 payload at storage width

    rows = store.search_memories_vector(query_vector=[0.9, 0.1, 0.0])
    assert [row["id"] for row in rows] == [close["id"], far["id"]]
    assert rows[0]["vector_distance"] < rows[1]["vector_distance"]
    assert rows[0]["vector_distance"] == pytest.approx(1.0 - 0.9 / (0.9**2 + 0.1**2) ** 0.5, abs=1e-6)
    assert "embedding" not in rows[0]

    # Same-direction query at full storage width matches the padded vector exactly.
    full_width = [1.0, 0.0, 0.0] + [0.0] * 1533
    exact = store.search_memories_vector(query_vector=full_width, limit=1)
    assert [row["id"] for row in exact] == [close["id"]]
    assert exact[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)

    filtered = store.search_memories_vector(query_vector=[1.0, 0.0], sensitivity_allowed=["public"])
    assert filtered == []

    with pytest.raises(ContinuityStoreInvariantError):
        store.search_memories_vector(query_vector=[])
    with pytest.raises(ContinuityStoreInvariantError):
        store.update_memory_embedding(memory_id=close["id"], vector=[])
    assert store.update_memory_embedding(memory_id=str(uuid4()), vector=[1.0]) is None
    conn.close()


def test_signed_embedding_compare_and_set_rejects_vector_prepared_before_text_edit() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, title="Old title", canonical_text="Old fact", summary="Old")
    old_digest = memory_embedding_content_sha256(memory)

    store.update_memory(
        memory_id=str(memory["id"]),
        patch={"canonical_text": "New fact", "summary": "New"},
    )
    assert (
        store.update_memory_embedding(
            memory_id=str(memory["id"]),
            vector=[1.0, 0.0],
            provider="stub",
            model="embed-v1",
            endpoint="stub-endpoint",
            content_sha256=old_digest,
            signature_version=2,
        )
        is None
    )

    row = conn.execute(
        "SELECT embedding, metadata_json FROM memories WHERE id = ?",
        (str(memory["id"]),),
    ).fetchone()
    assert row[0] is None
    assert EMBEDDING_SIGNATURE_METADATA_KEY not in json.loads(row[1])
    conn.close()


def test_vector_search_rejects_embeddings_from_a_different_model_signature() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, canonical_text="signed vector memory")
    assert (
        store.update_memory_embedding(
            memory_id=str(memory["id"]),
            vector=[1.0, 0.0],
            provider="openai_compatible",
            model="embed-v1",
            endpoint="host-a",
            content_sha256=memory_embedding_content_sha256(memory),
            signature_version=2,
        )
        is not None
    )

    matching = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="openai_compatible",
        embedding_model="embed-v1",
        embedding_endpoint="host-a",
        embedding_signature_version=2,
    )
    mismatched = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="openai_compatible",
        embedding_model="embed-v2",
        embedding_signature_version=2,
    )
    # Same provider/model but a different endpoint must not be pooled.
    mismatched_endpoint = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="openai_compatible",
        embedding_model="embed-v1",
        embedding_endpoint="host-b",
        embedding_signature_version=2,
    )

    assert [row["id"] for row in matching] == [memory["id"]]
    assert mismatched == []
    assert mismatched_endpoint == []
    assert (
        store.list_memories_missing_embeddings(
            embedding_provider="openai_compatible",
            embedding_model="embed-v1",
            embedding_signature_version=2,
        )
        == []
    )
    incompatible = store.list_memories_missing_embeddings(
        embedding_provider="openai_compatible",
        embedding_model="embed-v2",
        embedding_signature_version=2,
    )
    assert [row["id"] for row in incompatible] == [memory["id"]]
    assert incompatible[0]["embedding_present"] == 1
    metadata = conn.execute("SELECT metadata_json FROM memories WHERE id = ?", (str(memory["id"]),)).fetchone()[0]
    parsed_metadata = json.loads(metadata)
    assert parsed_metadata["_alice_embedding"] == {
        "content_sha256": memory_embedding_content_sha256(memory),
        "model": "embed-v1",
        "provider": "openai_compatible",
        "endpoint": "host-a",
        "version": 2,
    }

    # Simulate a stale restored snapshot or third-party adapter that puts the
    # old vector/signature back after text changed. Signature-aware search must
    # reject it even when lifecycle hooks and update triggers were bypassed.
    embedding_blob = conn.execute("SELECT embedding FROM memories WHERE id = ?", (str(memory["id"]),)).fetchone()[0]
    conn.execute(
        "UPDATE memories SET canonical_text = ? WHERE id = ?",
        ("signed vector memory changed", str(memory["id"])),
    )
    conn.execute(
        "UPDATE memories SET embedding = ?, metadata_json = ? WHERE id = ?",
        (embedding_blob, metadata, str(memory["id"])),
    )
    assert (
        store.search_memories_vector(
            query_vector=[1.0, 0.0],
            embedding_provider="openai_compatible",
            embedding_model="embed-v1",
            embedding_signature_version=2,
        )
        == []
    )
    stale_backfill = store.list_memories_missing_embeddings(
        embedding_provider="openai_compatible",
        embedding_model="embed-v1",
        embedding_endpoint="host-a",
        embedding_signature_version=2,
    )
    assert [row["id"] for row in stale_backfill] == [memory["id"]]
    assert stale_backfill[0]["embedding_present"] == 1

    assert store.clear_memory_embedding(memory_id=str(memory["id"])) is not None
    cleared = conn.execute(
        "SELECT embedding, metadata_json FROM memories WHERE id = ?", (str(memory["id"]),)
    ).fetchone()
    assert cleared[0] is None
    assert "_alice_embedding" not in json.loads(cleared[1])
    conn.close()


# -- vectorized vector-search equivalence -----------------------------------------------
#
# search_memories_vector was rewritten from a per-row Python loop into a
# vectorized numpy scan plus top-k verify/hydrate. These tests pin exact
# behavioral equivalence: the oracle below is a literal port of the OLD
# per-row algorithm (candidate SELECT, signature re-verification, pad/truncate,
# float32 BLAS scoring, (distance, updated_at, id) sort, [:limit] slice).


def _oracle_vector_search_per_row(
    store: SQLiteVNextStore,
    *,
    query_vector: list[float],
    limit: int = 50,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_endpoint: str | None = None,
    embedding_signature_version: int | None = None,
) -> list[dict[str, object]]:
    """Reference reimplementation of the pre-vectorization algorithm."""
    padded = pad_embedding_vector(query_vector)
    query_array = np.asarray(padded, dtype=np.float32)
    query_norm = float(np.linalg.norm(query_array))
    signature_sql = ""
    signature_params: list[object] = []
    if embedding_provider is not None or embedding_model is not None:
        signature_sql = " AND json_extract(metadata_json, ?) = ? AND json_extract(metadata_json, ?) = ?"
        signature_params.extend(
            (
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.provider",
                embedding_provider,
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.model",
                embedding_model,
            )
        )
        if embedding_endpoint is not None:
            signature_sql += " AND json_extract(metadata_json, ?) = ?"
            signature_params.extend((f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.endpoint", embedding_endpoint))
        if embedding_signature_version is not None:
            signature_sql += " AND json_extract(metadata_json, ?) = ?"
            signature_params.extend((f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.version", embedding_signature_version))
    candidates = store._fetch_all(
        f"""
        SELECT {", ".join(MEMORY_COLUMNS)}, embedding
        FROM memories
        WHERE user_id = ?
          AND deleted_at IS NULL
          AND embedding IS NOT NULL
          AND status IN ('active', 'accepted'){signature_sql}
        """,
        (store.user_id, *signature_params),
    )
    scored: list[dict[str, object]] = []
    for row in candidates:
        if signature_sql and not memory_embedding_signature_is_current(row):
            continue
        blob = row.pop("embedding")
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.size != 1536:
            resized = np.zeros(1536, dtype=np.float32)
            resized[: min(vector.size, 1536)] = vector[:1536]
            vector = resized
        vector_norm = float(np.linalg.norm(vector))
        if query_norm == 0.0 or vector_norm == 0.0:
            distance = 1.0
        else:
            similarity = float(np.dot(query_array, vector)) / (query_norm * vector_norm)
            distance = 1.0 - similarity
        row["vector_distance"] = distance
        scored.append(row)
    scored.sort(
        key=lambda item: (
            item["vector_distance"],
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        )
    )
    return scored[:limit]


def _seed_vector_row(
    store: SQLiteVNextStore,
    conn: sqlite3.Connection,
    *,
    canonical_text: str,
    vector: list[float] | None = None,
    blob: bytes | None = None,
    status: str = "active",
    updated_at: str = "2026-07-01T00:00:00+00:00",
    signature: dict[str, object] | None = None,
    stale_sha: bool = False,
) -> str:
    """Insert a memory with a raw embedding blob, bypassing lifecycle CAS."""
    memory = _create_memory(store, canonical_text=canonical_text)
    memory_id = str(memory["id"])
    if blob is None:
        blob = np.asarray(pad_embedding_vector(vector), dtype=np.float32).tobytes()
    metadata: dict[str, object] = {}
    if signature is not None:
        current = store.get_memory(memory_id)
        assert current is not None
        digest = memory_embedding_content_sha256(current)
        if stale_sha:
            digest = "0" * 64
        metadata[EMBEDDING_SIGNATURE_METADATA_KEY] = {**signature, "content_sha256": digest}
    conn.execute(
        "UPDATE memories SET embedding = ?, status = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
        (blob, status, json.dumps(metadata), updated_at, memory_id),
    )
    return memory_id


_SIGNATURE_A = {"version": 2, "provider": "prov-a", "model": "model-a", "endpoint": "ep-a"}
_SIGNATURE_B = {"version": 2, "provider": "prov-b", "model": "model-b", "endpoint": "ep-b"}


def _sig_kwargs(signature: dict[str, object]) -> dict[str, object]:
    return {
        "embedding_provider": signature["provider"],
        "embedding_model": signature["model"],
        "embedding_endpoint": signature["endpoint"],
        "embedding_signature_version": signature["version"],
    }


def _seed_adversarial_vector_corpus(store: SQLiteVNextStore, conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Seed the adversarial vector corpus; return the differential scenarios.

    Shared between the Stage 1 (stateless-scan) oracle test and the Stage 2
    (resident-cache) three-way differential test so both exercise the exact
    same corpus and query mix.
    """
    rng = np.random.default_rng(42)

    # Signature population A (12 rows, one of them stale-sha and nearest to
    # the query so signature-filtered search must refill past it).
    for position in range(12):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"population a row {position}",
            vector=list(rng.standard_normal(1536)),
            updated_at=f"2026-07-01T00:00:{position:02d}+00:00",
            signature=_SIGNATURE_A,
        )
    _seed_vector_row(
        store,
        conn,
        canonical_text="population a stale nearest row",
        vector=[1.0, 0.0, 0.0],
        updated_at="2026-07-01T00:01:00+00:00",
        signature=_SIGNATURE_A,
        stale_sha=True,
    )
    # Signature population B (different provider/model/endpoint).
    for position in range(6):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"population b row {position}",
            vector=list(rng.standard_normal(1536)),
            updated_at=f"2026-07-01T00:02:{position:02d}+00:00",
            signature=_SIGNATURE_B,
        )
    # Unsigned rows across lifecycle statuses; only active/accepted are
    # searchable.
    for position, status in enumerate(
        ("active", "accepted", "candidate", "superseded", "stale", "rejected", "archived")
    ):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"lifecycle {status} row {position}",
            vector=list(rng.standard_normal(1536)),
            status=status,
            updated_at=f"2026-07-01T00:03:{position:02d}+00:00",
        )
    # Zero-norm vector: distance must be exactly 1.0.
    _seed_vector_row(
        store,
        conn,
        canonical_text="zero norm row",
        vector=[0.0] * 1536,
        updated_at="2026-07-01T00:04:00+00:00",
    )
    # Non-1536-dim blobs: shorter and longer than the storage width, exercising
    # the pad/truncate path.
    _seed_vector_row(
        store,
        conn,
        canonical_text="short blob row",
        blob=np.asarray([0.5, 0.25, -0.75, 1.5], dtype=np.float32).tobytes(),
        updated_at="2026-07-01T00:05:00+00:00",
    )
    _seed_vector_row(
        store,
        conn,
        canonical_text="long blob row",
        blob=np.asarray(list(rng.standard_normal(2000)), dtype=np.float32).tobytes(),
        updated_at="2026-07-01T00:05:01+00:00",
    )
    # Equal-distance rows (identical vectors, distinct updated_at/id).
    for position in range(3):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"tie row {position}",
            vector=[0.0, 1.0, 1.0],
            updated_at="2026-07-01T00:06:00+00:00" if position < 2 else "2026-07-01T00:06:01+00:00",
        )

    query = list(rng.standard_normal(64))
    return [
        {"query_vector": query, "limit": 100},
        {"query_vector": query, "limit": 3},
        {"query_vector": [0.0, 1.0, 1.0], "limit": 5},
        {"query_vector": [1.0, 0.0, 0.0], "limit": 4, **_sig_kwargs(_SIGNATURE_A)},
        {"query_vector": query, "limit": 100, **_sig_kwargs(_SIGNATURE_A)},
        {"query_vector": query, "limit": 2, **_sig_kwargs(_SIGNATURE_B)},
    ]


def test_vector_search_matches_per_row_oracle_on_adversarial_corpus() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    scenarios = _seed_adversarial_vector_corpus(store, conn)
    for scenario in scenarios:
        expected = _oracle_vector_search_per_row(store, **scenario)
        actual = store.search_memories_vector(**scenario)
        assert [(row["id"], row["vector_distance"]) for row in actual] == [
            (row["id"], row["vector_distance"]) for row in expected
        ], scenario
        # Full hydrated rows are identical too (bitwise-equal distances).
        assert actual == expected, scenario
    conn.close()


def test_vector_search_never_pools_across_signature_populations() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    a_ids = [
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"pool a {position}",
            vector=[1.0, float(position) * 0.1],
            updated_at=f"2026-07-02T00:00:{position:02d}+00:00",
            signature=_SIGNATURE_A,
        )
        for position in range(3)
    ]
    b_ids = [
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"pool b {position}",
            vector=[1.0, 0.0],
            updated_at=f"2026-07-02T00:01:{position:02d}+00:00",
            signature=_SIGNATURE_B,
        )
        for position in range(3)
    ]
    # Same provider/model as A but a different endpoint fingerprint: the SQL
    # json_extract clauses must exclude it from endpoint-qualified searches.
    other_endpoint_id = _seed_vector_row(
        store,
        conn,
        canonical_text="pool a other endpoint",
        vector=[1.0, 0.0],
        updated_at="2026-07-02T00:02:00+00:00",
        signature={**_SIGNATURE_A, "endpoint": "ep-other"},
    )
    unsigned_id = _seed_vector_row(
        store,
        conn,
        canonical_text="pool unsigned",
        vector=[1.0, 0.0],
        updated_at="2026-07-02T00:03:00+00:00",
    )

    rows_a = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="prov-a",
        embedding_model="model-a",
        embedding_endpoint="ep-a",
        embedding_signature_version=2,
    )
    assert sorted(str(row["id"]) for row in rows_a) == sorted(a_ids)
    assert {str(row["id"]) for row in rows_a}.isdisjoint({*b_ids, other_endpoint_id, unsigned_id})

    rows_b = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="prov-b",
        embedding_model="model-b",
        embedding_endpoint="ep-b",
        embedding_signature_version=2,
    )
    assert sorted(str(row["id"]) for row in rows_b) == sorted(b_ids)

    # Unsigned search pools everything with an embedding, exactly like the
    # per-row implementation did.
    unsigned_rows = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert {str(row["id"]) for row in unsigned_rows} == {*a_ids, *b_ids, other_endpoint_id, unsigned_id}
    for scenario in (
        {"query_vector": [1.0, 0.0]},
        {
            "query_vector": [1.0, 0.0],
            "embedding_provider": "prov-a",
            "embedding_model": "model-a",
            "embedding_endpoint": "ep-a",
            "embedding_signature_version": 2,
        },
    ):
        assert store.search_memories_vector(**scenario) == _oracle_vector_search_per_row(store, **scenario)
    conn.close()


def test_vector_search_refills_past_stale_signature_rows_across_batches() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    # 40 rows ranked by increasing distance from the query; the closest 33
    # are stale-sha, spanning more than one hydration batch (batch size for
    # limit=2 is 32), so refill must walk into the second batch.
    ids: list[str] = []
    for position in range(40):
        angle = 0.01 * (position + 1)
        ids.append(
            _seed_vector_row(
                store,
                conn,
                canonical_text=f"refill row {position:02d}",
                vector=[float(np.cos(angle)), float(np.sin(angle))],
                updated_at=f"2026-07-03T00:00:{position:02d}+00:00",
                signature=_SIGNATURE_A,
                stale_sha=position < 33,
            )
        )
    kwargs = {
        "query_vector": [1.0, 0.0],
        "limit": 2,
        "embedding_provider": "prov-a",
        "embedding_model": "model-a",
        "embedding_endpoint": "ep-a",
        "embedding_signature_version": 2,
    }
    rows = store.search_memories_vector(**kwargs)
    assert [str(row["id"]) for row in rows] == [ids[33], ids[34]]
    assert {str(row["id"]) for row in rows}.isdisjoint(set(ids[:33]))
    assert rows == _oracle_vector_search_per_row(store, **kwargs)
    conn.close()


def test_vector_search_orders_equal_distances_by_updated_at_then_id() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    # Four rows share one embedding (bitwise-equal distances); two of them
    # also share updated_at so the id tie-break decides. A closer and a
    # farther row bracket the tie group.
    tie_ids = [
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"tiebreak row {position}",
            vector=[0.6, 0.8],
            updated_at=updated_at,
        )
        for position, updated_at in enumerate(
            (
                "2026-07-04T00:00:05+00:00",
                "2026-07-04T00:00:01+00:00",
                "2026-07-04T00:00:05+00:00",
                "2026-07-04T00:00:03+00:00",
            )
        )
    ]
    closest = _seed_vector_row(
        store,
        conn,
        canonical_text="tiebreak closest",
        vector=[1.0, 0.05],
        updated_at="2026-07-04T00:00:09+00:00",
    )
    farthest = _seed_vector_row(
        store,
        conn,
        canonical_text="tiebreak farthest",
        vector=[-1.0, 0.2],
        updated_at="2026-07-04T00:00:00+00:00",
    )

    rows = store.search_memories_vector(query_vector=[1.0, 0.0])
    tie_distances = {row["vector_distance"] for row in rows if str(row["id"]) in set(tie_ids)}
    assert len(tie_distances) == 1  # bitwise-equal distances
    shared_updated = [tie_ids[0], tie_ids[2]]
    expected_tie_order = [tie_ids[1], tie_ids[3], *sorted(shared_updated)]
    assert [str(row["id"]) for row in rows] == [closest, *expected_tie_order, farthest]
    assert rows == _oracle_vector_search_per_row(store, query_vector=[1.0, 0.0])
    # Determinism across repeated calls (stateless scan).
    assert rows == store.search_memories_vector(query_vector=[1.0, 0.0])
    conn.close()


def _run_vector_hydrate_window_scenario(
    tmp_path: Path, mutate_sql: str
) -> tuple[list[dict[str, object]], str, list[str]]:
    """Run a vector search with a concurrent writer committing between phases.

    ``search_memories_vector`` runs two SQL phases (vectorized scan, then
    ranked hydrate) with no transaction spanning them (autocommit;
    per-statement snapshots), so another connection can commit a mutation of a
    scanned row before it is hydrated. This helper seeds three rows on a
    file-backed database, then fires ``mutate_sql`` against the nearest row
    via a SECOND connection after the scan but before the first hydrate SELECT
    (the first ``_fetch_all`` call; the scan itself uses ``_execute``).
    Returns (rows, mutated_target_id, surviving_ids_in_rank_order).
    """
    db_path = str(tmp_path / f"hydrate-window-{uuid4()}.db")
    conn = sqlite3.connect(db_path)
    bootstrap_sqlite_schema(conn)
    conn.commit()
    store = _make_store(conn)
    conn.commit()

    def _seed(text: str, vector: list[float], updated_at: str) -> str:
        memory_id = _seed_vector_row(store, conn, canonical_text=text, vector=vector, updated_at=updated_at)
        conn.commit()
        return memory_id

    target = _seed("hydrate window target nearest", [1.0, 0.0], "2026-07-01T00:00:00+00:00")
    survivor_near = _seed("hydrate window other a", [0.9, 0.1], "2026-07-01T00:00:01+00:00")
    survivor_far = _seed("hydrate window other b", [0.5, 0.5], "2026-07-01T00:00:02+00:00")

    writer = sqlite3.connect(db_path)
    original_fetch_all = store._fetch_all
    fired = {"done": False}

    def interleaved_fetch_all(query: str, params: tuple[object, ...] = ()):
        if not fired["done"]:
            fired["done"] = True
            writer.execute(mutate_sql, (target,))
            writer.commit()
        return original_fetch_all(query, params)

    store._fetch_all = interleaved_fetch_all  # type: ignore[method-assign]
    try:
        # limit=2 with 3 candidates: the mutated nearest row must not consume
        # a limit slot, so both survivors still come back.
        rows = store.search_memories_vector(query_vector=[1.0, 0.0], limit=2)
    finally:
        conn.close()
        writer.close()
    return rows, target, [survivor_near, survivor_far]


def test_vector_search_excludes_rows_soft_deleted_between_scan_and_hydrate(tmp_path: Path) -> None:
    rows, target, survivors = _run_vector_hydrate_window_scenario(
        tmp_path,
        "UPDATE memories SET deleted_at = '2026-07-01T00:10:00+00:00', status = 'rejected' WHERE id = ?",
    )
    # The mutated row is excluded, every returned payload satisfies the read
    # gates, and the refill fills the freed slot from further down the ranking.
    assert [str(row["id"]) for row in rows] == survivors
    for row in rows:
        assert str(row["id"]) != target
        assert row.get("deleted_at") is None
        assert row.get("status") in ("active", "accepted")


def test_vector_search_survives_embedding_nulled_between_scan_and_hydrate(tmp_path: Path) -> None:
    # Same window with the embedding cleared: the exact-recompute step must
    # never see a NULL blob (historically a TypeError in np.frombuffer).
    rows, target, survivors = _run_vector_hydrate_window_scenario(
        tmp_path,
        "UPDATE memories SET embedding = NULL WHERE id = ?",
    )
    assert [str(row["id"]) for row in rows] == survivors
    assert target not in {str(row["id"]) for row in rows}


# -- Stage 2 resident vector cache ------------------------------------------------------
#
# search_memories_vector gained a process-local resident vector cache
# (vnext_stores/sqlite/vector_scan.py) validated against the one-row
# embedding_stamp token. The cache holds ONLY vectors/norms/id-map; every
# predicate -- including the embedding-signature json_extract clauses --
# runs as fresh candidate SQL per query. These tests pin: three-way
# differential equality (cached == stateless == per-row oracle), the
# invalidation contract for clear-then-re-embed / reindex / redaction,
# embed-on-write upserts without rebuilds, warm-path signature parity with
# the stateless SQL (including metadata_json rewrites that never touch the
# embedding column), the atomicity of the bump-deciding presence read, the
# off-switch and byte cap, and the :memory: bypass.


def _clear_vector_cache_env(monkeypatch) -> None:
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV, raising=False)
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_MAX_MB_ENV, raising=False)


def _open_file_connection(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    db_path = str(tmp_path / f"vector-cache-{uuid4()}.db")
    conn = sqlite3.connect(db_path)
    bootstrap_sqlite_schema(conn)
    conn.commit()
    return db_path, conn


def _vector_cache_entry(db_path: str, user_id: str):
    return vector_scan._REGISTRY.get((os.path.realpath(db_path), str(user_id)))


def _stamp_token(db_path: str) -> str:
    probe = sqlite3.connect(db_path)
    try:
        return str(probe.execute("SELECT token FROM embedding_stamp WHERE id = 1").fetchone()[0])
    finally:
        probe.close()


def _id_distance_pairs(rows: list[dict[str, object]]) -> list[tuple[str, object]]:
    return [(str(row["id"]), row["vector_distance"]) for row in rows]


class _SubstringVectorProvider:
    """Deterministic embedding stub: marker substrings map to fixed directions."""

    provider = "stub_provider"
    model = "stub-embed"
    base_url = "https://stub.invalid/v1"

    def __init__(self, routes: dict[str, list[float]]):
        self._routes = routes

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            for marker, vector in self._routes.items():
                if marker in text:
                    vectors.append(list(vector))
                    break
            else:
                raise AssertionError(f"no stub vector for text: {text!r}")
        return vectors


def test_vector_cache_matches_stateless_and_oracle_on_adversarial_corpus(tmp_path: Path, monkeypatch) -> None:
    # (a) Three-way differential: cached path == stateless path == the
    # per-row oracle, on the same adversarial corpus, cold and warm.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    scenarios = _seed_adversarial_vector_corpus(store, conn)
    conn.commit()
    for round_name in ("cold", "warm"):
        for scenario in scenarios:
            expected = _oracle_vector_search_per_row(store, **scenario)
            monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
            stateless = store.search_memories_vector(**scenario)
            monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
            cached = store.search_memories_vector(**scenario)
            assert _id_distance_pairs(cached) == _id_distance_pairs(stateless) == _id_distance_pairs(expected), (
                round_name,
                scenario,
            )
            # Full hydrated rows are identical too (bitwise-equal distances).
            assert cached == stateless == expected, (round_name, scenario)
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None
    assert entry.rebuilds == 1  # served warm across every scenario
    conn.close()


def test_vector_cache_clear_then_reembed_via_commit_service_scores_new_vector(tmp_path: Path, monkeypatch) -> None:
    # (b) THE CLEAR-THEN-RE-EMBED HOLE: a text update through the real
    # commit-service flow clears then re-embeds the SAME row id. The clear
    # must bump the stamp so a WARM cache stops scoring the old vector.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    provider = _SubstringVectorProvider({"tea": [0.0, 1.0], "coffee": [1.0, 0.0]})
    monkeypatch.setattr("alicebot_api.vnext_embeddings.get_embedding_provider", lambda: provider)
    memory = _create_memory(store, canonical_text="Sam prefers coffee before noon.")
    assert vnext_embeddings.attach_memory_embedding(store, memory, actor_type="user") is True
    conn.commit()

    warm = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert [str(row["id"]) for row in warm] == [str(memory["id"])]
    assert warm[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None
    rebuilds_before = entry.rebuilds
    token_before = _stamp_token(db_path)

    service = VNextMemoryCommitService(store)
    result = service.correct(
        identity=None,
        memory_id=str(memory["id"]),
        canonical_text="Sam prefers tea before noon.",
    )
    assert result["status"] == "committed"
    conn.commit()

    # The clear bumped the token even though the re-embed (NULL -> vector,
    # current content sha) did not.
    assert _stamp_token(db_path) != token_before

    # The next query scores by the NEW vector...
    new_direction = store.search_memories_vector(query_vector=[0.0, 1.0])
    assert [str(row["id"]) for row in new_direction] == [str(memory["id"])]
    assert new_direction[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)
    old_direction = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert old_direction[0]["vector_distance"] == pytest.approx(1.0, abs=1e-6)
    assert entry.rebuilds > rebuilds_before  # invalidation, not upsert

    # ...including from a second sqlite3 connection to the same file.
    conn2 = sqlite3.connect(db_path)
    store2 = SQLiteVNextStore(conn2, store.user_id)
    second = store2.search_memories_vector(query_vector=[0.0, 1.0])
    assert second == new_direction
    assert second == _oracle_vector_search_per_row(store2, query_vector=[0.0, 1.0])
    conn2.close()
    conn.close()


def test_vector_cache_reindex_overwrite_and_cas_rollback_stay_consistent(tmp_path: Path, monkeypatch) -> None:
    # (c) Reindex/backfill overwrites bump the token and later queries (from
    # a second connection) see the new vectors; a rolled-back overwrite
    # leaves both the token and subsequent results consistent.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    rng = np.random.default_rng(7)
    ids: list[str] = []
    for position in range(4):
        memory = _create_memory(store, canonical_text=f"reindex row {position}")
        ids.append(str(memory["id"]))
        assert store.update_memory_embedding(memory_id=ids[-1], vector=list(rng.standard_normal(1536))) is not None
    conn.commit()
    store.search_memories_vector(query_vector=[1.0, 0.0])  # warm the cache
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None and entry.rebuilds == 1
    token_before = _stamp_token(db_path)

    # Overwrite every existing vector (the backfill/reindex shape).
    for position, memory_id in enumerate(ids):
        basis = [0.0] * 4
        basis[position] = 1.0
        assert store.update_memory_embedding(memory_id=memory_id, vector=basis) is not None
    conn.commit()
    token_after = _stamp_token(db_path)
    assert token_after != token_before

    conn2 = sqlite3.connect(db_path)
    store2 = SQLiteVNextStore(conn2, store.user_id)
    rows = store2.search_memories_vector(query_vector=[0.0, 0.0, 1.0, 0.0])
    assert str(rows[0]["id"]) == ids[2]
    assert rows[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)
    assert rows == _oracle_vector_search_per_row(store2, query_vector=[0.0, 0.0, 1.0, 0.0])

    # CAS rollback: an uncommitted overwrite is visible to its own
    # connection (snapshot consistency)...
    assert store2.update_memory_embedding(memory_id=ids[0], vector=[0.5, 0.5, 0.0, 0.0]) is not None
    uncommitted = store2.search_memories_vector(query_vector=[0.5, 0.5, 0.0, 0.0])
    assert str(uncommitted[0]["id"]) == ids[0]
    assert uncommitted[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)
    conn2.rollback()
    # ...and after rollback the committed token survives and results match
    # the stateless path again (fresh-uuid rewrite: no token aliasing).
    assert _stamp_token(db_path) == token_after
    rolled_back = store2.search_memories_vector(query_vector=[1.0, 0.0, 0.0, 0.0])
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless = store2.search_memories_vector(query_vector=[1.0, 0.0, 0.0, 0.0])
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    assert rolled_back == stateless
    assert str(rolled_back[0]["id"]) == ids[0]
    assert rolled_back[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)

    # Re-bootstrap keeps the token: INSERT OR IGNORE, never re-seeded.
    bootstrap_sqlite_schema(conn)
    conn.commit()
    assert _stamp_token(db_path) == token_after
    conn2.close()
    conn.close()


def test_vector_cache_embed_on_write_upserts_without_rebuild(tmp_path: Path, monkeypatch) -> None:
    # (d) Newly captured memories become searchable WITHOUT a rebuild: the
    # NULL -> vector write does not bump, and the warm path upserts the new
    # id into the same entry object.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    for position, vector in enumerate(([0.0, 1.0], [0.6, 0.8], [-1.0, 0.2])):
        memory = _create_memory(store, canonical_text=f"resident row {position}")
        assert store.update_memory_embedding(memory_id=str(memory["id"]), vector=vector) is not None
    conn.commit()
    store.search_memories_vector(query_vector=[1.0, 0.0])  # warm the cache
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None
    assert entry.rebuilds == 1
    assert len(entry.row_index) == 3
    token_before = _stamp_token(db_path)

    captured = _create_memory(store, canonical_text="embed on write row")
    assert store.update_memory_embedding(memory_id=str(captured["id"]), vector=[1.0, 0.0]) is not None
    conn.commit()
    assert _stamp_token(db_path) == token_before  # embed-on-write must NOT bump

    rows = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert str(rows[0]["id"]) == str(captured["id"])
    assert rows[0]["vector_distance"] == pytest.approx(0.0, abs=1e-6)
    entry_after = _vector_cache_entry(db_path, store.user_id)
    assert entry_after is entry  # cache entry object identity survives
    assert entry.rebuilds == 1  # upsert path, no rebuild
    assert len(entry.row_index) == 4
    assert rows == _oracle_vector_search_per_row(store, query_vector=[1.0, 0.0])
    conn.close()


def test_vector_cache_warm_signature_filter_matches_sql_clauses(tmp_path: Path, monkeypatch) -> None:
    # (e) The warm cached path applies the SAME signature json_extract SQL
    # clauses as the stateless path (fresh per query, nothing captured), so
    # it must select EXACTLY the same rows on a mixed-population corpus
    # including adversarial value types.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    for position in range(3):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"warm sig a {position}",
            vector=[1.0, float(position) * 0.1],
            updated_at=f"2026-07-05T00:00:{position:02d}+00:00",
            signature=_SIGNATURE_A,
        )
    for position in range(3):
        _seed_vector_row(
            store,
            conn,
            canonical_text=f"warm sig b {position}",
            vector=[1.0, 0.0],
            updated_at=f"2026-07-05T00:01:{position:02d}+00:00",
            signature=_SIGNATURE_B,
        )
    _seed_vector_row(
        store,
        conn,
        canonical_text="warm sig other endpoint",
        vector=[1.0, 0.0],
        updated_at="2026-07-05T00:02:00+00:00",
        signature={**_SIGNATURE_A, "endpoint": "ep-other"},
    )
    _seed_vector_row(
        store,
        conn,
        canonical_text="warm sig unsigned",
        vector=[1.0, 0.0],
        updated_at="2026-07-05T00:03:00+00:00",
    )
    # Nearest stale-sha row: SQL clauses admit it, the hydrate sha recheck
    # must reject it -- identically on both paths.
    _seed_vector_row(
        store,
        conn,
        canonical_text="warm sig stale nearest",
        vector=[1.0, 0.0],
        updated_at="2026-07-05T00:04:00+00:00",
        signature=_SIGNATURE_A,
        stale_sha=True,
    )
    # Adversarial: version stored as TEXT '2'. SQLite `'2' = 2` is false
    # (storage-class mismatch), so both paths must reject it -- never
    # coerce.
    _seed_vector_row(
        store,
        conn,
        canonical_text="warm sig text version",
        vector=[1.0, 0.0],
        updated_at="2026-07-05T00:05:00+00:00",
        signature={**_SIGNATURE_A, "version": "2"},
    )
    conn.commit()

    scenarios: list[dict[str, object]] = [
        {"query_vector": [1.0, 0.0]},
        {"query_vector": [1.0, 0.0], **_sig_kwargs(_SIGNATURE_A)},
        {
            "query_vector": [1.0, 0.0],
            "embedding_provider": "prov-a",
            "embedding_model": "model-a",
        },
        {"query_vector": [1.0, 0.0], **{**_sig_kwargs(_SIGNATURE_A), "embedding_signature_version": 3}},
        {"query_vector": [1.0, 0.0], **_sig_kwargs(_SIGNATURE_B)},
        {"query_vector": [1.0, 0.0], "embedding_provider": "prov-none", "embedding_model": "model-none"},
    ]
    for round_name in ("cold", "warm"):
        for scenario in scenarios:
            expected = _oracle_vector_search_per_row(store, **scenario)
            monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
            stateless = store.search_memories_vector(**scenario)
            monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
            cached = store.search_memories_vector(**scenario)
            assert _id_distance_pairs(cached) == _id_distance_pairs(stateless) == _id_distance_pairs(expected), (
                round_name,
                scenario,
            )
            assert cached == stateless == expected, (round_name, scenario)
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None
    assert entry.rebuilds == 1  # the warm round served without a rebuild
    conn.close()


def test_vector_cache_signature_metadata_patch_without_bump_stays_bit_identical(tmp_path: Path, monkeypatch) -> None:
    # (e2) update_memory(metadata_json=...) can rewrite the embedding
    # signature WITHOUT touching the embedding column, so no stamp bump
    # happens (correctly: the vector bytes are unchanged). Because signature
    # filtering is SQL-side on every query -- never captured into the
    # resident data -- a WARM cache must admit the row the rewritten
    # signature now matches, bit-identical to the stateless path.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)

    target = _create_memory(store, canonical_text="signature swap target")
    decoy = _create_memory(store, canonical_text="signature decoy")
    sha_target = memory_embedding_content_sha256(store.get_memory(str(target["id"])))
    sha_decoy = memory_embedding_content_sha256(store.get_memory(str(decoy["id"])))
    assert (
        store.update_memory_embedding(
            memory_id=str(target["id"]),
            vector=[1.0, 0.0],
            provider="prov-a",
            model="model-a",
            endpoint="ep-a",
            content_sha256=sha_target,
            signature_version=2,
        )
        is not None
    )
    assert (
        store.update_memory_embedding(
            memory_id=str(decoy["id"]),
            vector=[0.0, 1.0],
            provider="prov-b",
            model="model-b",
            endpoint="ep-b",
            content_sha256=sha_decoy,
            signature_version=2,
        )
        is not None
    )
    conn.commit()

    # Warm the cache while target still carries the prov-a signature.
    store.search_memories_vector(query_vector=[1.0, 0.0])
    conn.commit()
    token_before = _stamp_token(db_path)

    # Rewrite the signature metadata WITHOUT touching the embedding column.
    new_metadata = {
        EMBEDDING_SIGNATURE_METADATA_KEY: {
            "version": 2,
            "provider": "prov-b",
            "model": "model-b",
            "endpoint": "ep-b",
            "content_sha256": sha_target,
        }
    }
    store.update_memory(memory_id=str(target["id"]), patch={"metadata_json": new_metadata})
    conn.commit()
    assert _stamp_token(db_path) == token_before, "no bump expected: embedding column untouched"

    query = {
        "query_vector": [1.0, 0.0],
        "embedding_provider": "prov-b",
        "embedding_model": "model-b",
        "embedding_endpoint": "ep-b",
        "embedding_signature_version": 2,
    }
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless = store.search_memories_vector(**query)
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    cached = store.search_memories_vector(**query)
    # SQL admits the patched target (nearest) plus the genuinely-prov-b decoy.
    assert [str(row["id"]) for row in stateless] == [str(target["id"]), str(decoy["id"])]
    assert _id_distance_pairs(cached) == _id_distance_pairs(stateless)
    assert cached == stateless

    # And the reverse rewrite excludes the row again, on both paths.
    store.update_memory(memory_id=str(target["id"]), patch={"metadata_json": {}})
    conn.commit()
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless_after = store.search_memories_vector(**query)
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    cached_after = store.search_memories_vector(**query)
    assert [str(row["id"]) for row in stateless_after] == [str(decoy["id"])]
    assert cached_after == stateless_after
    conn.close()


def test_vector_cache_presence_read_is_atomic_with_update_and_bump(tmp_path: Path, monkeypatch) -> None:
    # (e3) The bump-vs-no-bump decision comes from an embedding-presence
    # point-read. Executed in autocommit it races embed-on-write: a
    # concurrent NULL -> V1 commit between the read and the UPDATE would
    # turn this write into an overwrite whose bump the stale read skips,
    # and the cache would serve V1 forever. The fix takes BEGIN IMMEDIATE
    # before the read, so this test proves (1) the read runs inside the
    # writer transaction and (2) no concurrent embedding write can commit
    # inside the gap -- the interleaving that skipped the bump is
    # impossible.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn1 = _open_file_connection(tmp_path)
    store1 = _make_store(conn1)
    # Short busy timeout: the blocked writer must fail fast, not stall.
    conn2 = sqlite3.connect(db_path, timeout=0.2)
    store2 = SQLiteVNextStore(conn2, store1.user_id)

    target = _create_memory(store1, canonical_text="toctou target")
    for position in range(8):
        angle = 0.05 + 0.01 * position
        filler = _create_memory(store1, canonical_text=f"toctou filler {position}")
        assert (
            store1.update_memory_embedding(
                memory_id=str(filler["id"]),
                vector=[float(np.cos(angle)), float(np.sin(angle))],
            )
            is not None
        )
    conn1.commit()
    target_id = str(target["id"])

    original_fetch = store1._fetch_optional_one
    state: dict[str, object] = {"fired": False, "in_transaction_at_preread": None, "concurrent_error": None}

    def hooked(query: str, params: tuple[object, ...] = ()):  # noqa: ANN001
        row = original_fetch(query, params)
        if not state["fired"] and "embedding_present" in query:
            state["fired"] = True
            # (1) The presence read shares the writer transaction.
            state["in_transaction_at_preread"] = conn1.in_transaction
            # (2) A concurrent embed-on-write cannot commit inside the gap:
            # the writer lock is already held, so it fails with SQLITE_BUSY
            # instead of silently landing between the read and the UPDATE.
            try:
                store2.update_memory_embedding(memory_id=target_id, vector=[0.0, 1.0])
                conn2.commit()
            except sqlite3.OperationalError as exc:
                state["concurrent_error"] = str(exc)
        return row

    store1._fetch_optional_one = hooked  # type: ignore[method-assign]
    token_before = _stamp_token(db_path)
    assert store1.update_memory_embedding(memory_id=target_id, vector=[1.0, 0.0]) is not None
    store1._fetch_optional_one = original_fetch  # type: ignore[method-assign]
    conn1.commit()

    assert state["fired"] is True
    assert state["in_transaction_at_preread"] is True
    assert "locked" in str(state["concurrent_error"])
    # A's write was a true embed-on-write (NULL -> vector): no bump.
    assert _stamp_token(db_path) == token_before

    # Warm the cache on the second connection, then overwrite the now-live
    # vector: the presence read (inside the writer lock) sees it and bumps.
    store2.search_memories_vector(query_vector=[1.0, 0.0])
    assert store2.update_memory_embedding(memory_id=target_id, vector=[0.0, 1.0]) is not None
    conn2.commit()
    assert _stamp_token(db_path) != token_before

    # Quiescent end state: cached and stateless paths agree bit-identically.
    query = {"query_vector": [1.0, 0.0], "limit": 1}
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless = store2.search_memories_vector(**query)
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    cached = store2.search_memories_vector(**query)
    assert target_id not in {str(row["id"]) for row in stateless}
    assert _id_distance_pairs(cached) == _id_distance_pairs(stateless)
    assert cached == stateless
    conn1.close()
    conn2.close()


def test_vector_cache_redaction_evicts_the_row_and_bumps_the_token(tmp_path: Path, monkeypatch) -> None:
    # (f) Redacting a cached row NULLs its embedding inline; the stamp bump
    # (owner-decided prompt eviction) must stop the vector from
    # participating, including for a fresh second connection.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    target = _create_memory(store, canonical_text="redaction target row")
    assert store.update_memory_embedding(memory_id=str(target["id"]), vector=[1.0, 0.0]) is not None
    for position, vector in enumerate(([0.9, 0.1], [0.5, 0.5])):
        survivor = _create_memory(store, canonical_text=f"redaction survivor {position}")
        assert store.update_memory_embedding(memory_id=str(survivor["id"]), vector=vector) is not None
    conn.commit()
    warm = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert str(warm[0]["id"]) == str(target["id"])
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None
    rebuilds_before = entry.rebuilds
    token_before = _stamp_token(db_path)

    redact_memory_flow(store, memory_id=str(target["id"]), reason="user requested removal")
    conn.commit()
    assert _stamp_token(db_path) != token_before

    conn2 = sqlite3.connect(db_path)
    store2 = SQLiteVNextStore(conn2, store.user_id)
    rows = store2.search_memories_vector(query_vector=[1.0, 0.0])
    assert str(target["id"]) not in {str(row["id"]) for row in rows}
    assert len(rows) == 2
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless = store2.search_memories_vector(query_vector=[1.0, 0.0])
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    assert rows == stateless
    assert rows == _oracle_vector_search_per_row(store2, query_vector=[1.0, 0.0])
    assert entry.rebuilds > rebuilds_before
    conn2.close()
    conn.close()


def test_vector_cache_off_switch_and_over_cap_match_cached_results(tmp_path: Path, monkeypatch) -> None:
    # (g) ALICEBOT_SQLITE_VECTOR_CACHE=off and an over-cap
    # ALICEBOT_SQLITE_VECTOR_CACHE_MAX_MB both fall back to the stateless
    # path with identical results on the same corpus.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    rng = np.random.default_rng(11)
    for position in range(8):
        memory = _create_memory(store, canonical_text=f"cap row {position}")
        assert (
            store.update_memory_embedding(memory_id=str(memory["id"]), vector=list(rng.standard_normal(1536)))
            is not None
        )
    conn.commit()
    query = list(rng.standard_normal(1536))
    expected = _oracle_vector_search_per_row(store, query_vector=query)

    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    off_rows = store.search_memories_vector(query_vector=query)
    assert _vector_cache_entry(db_path, store.user_id) is None  # never built
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)

    cached_rows = store.search_memories_vector(query_vector=query)
    assert _vector_cache_entry(db_path, store.user_id) is not None
    assert cached_rows == off_rows == expected

    monkeypatch.setenv(vector_scan.VECTOR_CACHE_MAX_MB_ENV, "0")
    capped_rows = store.search_memories_vector(query_vector=query)
    assert capped_rows == expected
    assert _vector_cache_entry(db_path, store.user_id) is None  # dropped at the cap gate

    # An unparseable cap falls back to the default and the cache serves again.
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_MAX_MB_ENV, "not-a-number")
    default_cap_rows = store.search_memories_vector(query_vector=query)
    assert default_cap_rows == expected
    assert _vector_cache_entry(db_path, store.user_id) is not None
    conn.close()


def test_vector_cache_rebuild_streams_into_one_owned_matrix_and_survives_stale_counts(
    tmp_path: Path, monkeypatch
) -> None:
    # (i) Build-path memory discipline: the rebuild decodes every blob chunk
    # straight into ONE preallocated float32 matrix (slice assignment
    # copies), so the resident arrays own their bytes -- no frombuffer view
    # can keep SQLite blob buffers alive after the build -- and a stale
    # live-row COUNT (rows appearing or vanishing between the sizing query
    # and the scan) still yields results bit-identical to the stateless
    # path.
    _clear_vector_cache_env(monkeypatch)
    db_path, conn = _open_file_connection(tmp_path)
    store = _make_store(conn)
    monkeypatch.setattr(vector_scan, "_SCAN_CHUNK_ROWS", 2)  # force multiple chunks
    rng = np.random.default_rng(23)
    for position in range(7):
        memory = _create_memory(store, canonical_text=f"stream row {position}")
        assert (
            store.update_memory_embedding(memory_id=str(memory["id"]), vector=list(rng.standard_normal(1536)))
            is not None
        )
    conn.commit()
    query = list(rng.standard_normal(1536))
    expected = _oracle_vector_search_per_row(store, query_vector=query)

    # Decode-seam probe: record the array every blob was decoded into.
    original_decode = vector_scan._decode_blob_into
    decode_targets: list[object] = []

    def tracking_decode(out_row: np.ndarray, blob: object) -> None:
        decode_targets.append(out_row.base)
        original_decode(out_row, blob)

    monkeypatch.setattr(vector_scan, "_decode_blob_into", tracking_decode)
    cached = store.search_memories_vector(query_vector=query)
    monkeypatch.setenv(vector_scan.VECTOR_CACHE_ENV, "off")
    stateless = store.search_memories_vector(query_vector=query)
    monkeypatch.delenv(vector_scan.VECTOR_CACHE_ENV)
    assert cached == stateless == expected

    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None and entry.rebuilds == 1
    # Every blob went straight into a row view of THE resident matrix:
    # one allocation, no accumulate-then-stack copy.
    assert len(decode_targets) == 7
    assert all(target is entry.matrix for target in decode_targets)
    # The resident arrays own their data outright, so no cache attribute
    # can be a view retaining blob bytes (frombuffer views have a base).
    assert entry.matrix.flags["OWNDATA"] and entry.matrix.base is None
    assert entry.norms.flags["OWNDATA"] and entry.norms.base is None
    assert entry.matrix.dtype == np.float32 and entry.matrix.shape == (7, 1536)
    monkeypatch.setattr(vector_scan, "_decode_blob_into", original_decode)

    # Rows APPEARING after the COUNT (undercount by 2): the scan stops at
    # capacity and the missed rows arrive through the in-query upsert.
    real_count = vector_scan._count_live_embedding_rows
    conn.execute("UPDATE embedding_stamp SET token = ? WHERE id = 1", (uuid4().hex,))
    conn.commit()
    monkeypatch.setattr(
        vector_scan,
        "_count_live_embedding_rows",
        lambda inner_conn, inner_user: max(real_count(inner_conn, inner_user) - 2, 0),
    )
    undercounted = store.search_memories_vector(query_vector=query)
    assert undercounted == expected
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None and entry.rebuilds == 2
    assert len(entry.row_index) == 7 and entry.matrix.shape == (7, 1536)
    assert entry.matrix.flags["OWNDATA"] and entry.matrix.base is None

    # Rows VANISHING after the COUNT (overcount by 3): the owning matrix is
    # shrunk in place to the rows actually scanned.
    conn.execute("UPDATE embedding_stamp SET token = ? WHERE id = 1", (uuid4().hex,))
    conn.commit()
    monkeypatch.setattr(
        vector_scan,
        "_count_live_embedding_rows",
        lambda inner_conn, inner_user: real_count(inner_conn, inner_user) + 3,
    )
    overcounted = store.search_memories_vector(query_vector=query)
    assert overcounted == expected
    entry = _vector_cache_entry(db_path, store.user_id)
    assert entry is not None and entry.rebuilds == 3
    assert len(entry.row_index) == 7 and entry.matrix.shape == (7, 1536)
    assert entry.matrix.flags["OWNDATA"] and entry.matrix.base is None
    assert entry.norms.shape == (7,)
    conn.close()


def test_vector_cache_bypassed_for_in_memory_databases(monkeypatch) -> None:
    # (h) :memory: databases have no stable identity: the registry must not
    # gain an entry and results still match the oracle.
    _clear_vector_cache_env(monkeypatch)
    conn = _open_connection()
    store = _make_store(conn)
    for position, vector in enumerate(([1.0, 0.0], [0.0, 1.0])):
        memory = _create_memory(store, canonical_text=f"memory-db row {position}")
        assert store.update_memory_embedding(memory_id=str(memory["id"]), vector=vector) is not None
    rows = store.search_memories_vector(query_vector=[1.0, 0.0])
    assert rows == _oracle_vector_search_per_row(store, query_vector=[1.0, 0.0])
    assert all(key[1] != store.user_id for key in vector_scan._REGISTRY)
    conn.close()


# -- open loops -------------------------------------------------------------------------


def test_open_loop_crud_lifecycle() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store)

    loop = store.create_open_loop(
        {
            "title": "Follow up with the design partner",
            "memory_id": memory["id"],
            "priority": "high",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"origin": "capture"},
        }
    )
    assert loop["status"] == "open"
    assert loop["priority"] == "high"
    assert loop["metadata_json"] == {"origin": "capture"}
    assert store.get_open_loop(loop["id"])["id"] == loop["id"]

    listed = store.list_open_loops()
    assert [row["id"] for row in listed] == [loop["id"]]
    assert store.list_open_loops(status="resolved") == []
    assert [row["id"] for row in store.list_open_loops(status=None)] == [loop["id"]]

    updated = store.update_open_loop(
        loop_id=loop["id"],
        patch={"description": "Ping them Friday", "priority": "urgent"},
    )
    assert updated["description"] == "Ping them Friday"
    assert updated["priority"] == "urgent"
    assert updated["title"] == loop["title"]

    resolved = store.update_open_loop_status(loop_id=loop["id"], status="resolved", resolution_note="Partner confirmed")
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    assert resolved["closed_at"] is not None
    assert resolved["resolution_note"] == "Partner confirmed"
    assert store.list_open_loops() == []

    reopened = store.update_open_loop_status(loop_id=loop["id"], status="open")
    assert reopened["resolved_at"] is None
    assert reopened["closed_at"] is None
    assert reopened["resolution_note"] is None

    loop_events = [
        event["event_type"] for event in store.list_events(target_type="open_loop", target_id=str(loop["id"]))
    ]
    assert loop_events.count("open_loop.created") == 1
    assert loop_events.count("open_loop.updated") == 3

    with pytest.raises(sqlite3.IntegrityError):
        store.create_open_loop({"title": "bad", "priority": "someday"})
    with pytest.raises(sqlite3.IntegrityError):
        store.create_open_loop({"title": "bad", "status": "unknown_status"})
    conn.close()


def test_list_open_loops_applies_domain_and_sensitivity_filters() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    visible = store.create_open_loop({"title": "visible", "domain": "project", "sensitivity": "private"})
    unknown = store.create_open_loop({"title": "unknown-domain", "domain": "unknown", "sensitivity": "private"})
    store.create_open_loop({"title": "hidden", "domain": "health", "sensitivity": "sacred"})

    rows = store.list_open_loops(domains=["project"], sensitivity_allowed=["private"])
    assert {row["id"] for row in rows} == {visible["id"], unknown["id"]}
    conn.close()


# -- agent identities and keys --------------------------------------------------------------


def test_upsert_agent_identity_merges_metadata_and_keeps_display_name() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    first = store.upsert_agent_identity(
        {
            "agent_id": "hermes",
            "agent_type": "coding_agent",
            "permission_profile": "trusted_local_agent",
            "display_name": "Hermes",
            "metadata_json": {"a": 1, "keep": True},
        }
    )
    assert first["agent_type"] == "coding_agent"
    assert first["project_scope_json"] == []

    second = store.upsert_agent_identity(
        {
            "agent_id": "hermes",
            "agent_type": "coding_agent",
            "permission_profile": "read_only_agent",
            "metadata_json": {"a": 2, "b": 3},
            "project_scope": ["alicebot"],
        }
    )
    assert second["id"] == first["id"]
    assert second["permission_profile"] == "read_only_agent"
    assert second["display_name"] == "Hermes"  # COALESCE keeps the existing name
    assert second["metadata_json"] == {"a": 2, "b": 3, "keep": True}
    assert second["project_scope_json"] == ["alicebot"]
    assert second["updated_at"] >= first["updated_at"]

    identities = store.list_agent_identities()
    assert [row["id"] for row in identities] == [first["id"]]

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_agent_identity({"agent_id": "bad", "permission_profile": "root"})
    conn.close()


def test_agent_api_key_create_verify_touch_revoke_and_count() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    key = store.create_agent_api_key(
        {
            "agent_id": "hermes",
            "permission_profile": "read_only_agent",
            "key_hash": "b" * 64,
            "key_prefix": "ak_live_b",
            "label": "laptop",
        }
    )
    assert key["label"] == "laptop"
    assert key["revoked_at"] is None
    assert key["last_used_at"] is None

    fetched = store.get_agent_api_key_by_hash("b" * 64)
    assert fetched["id"] == key["id"]
    assert store.get_agent_api_key_by_hash("c" * 64) is None

    touched = store.touch_agent_api_key(key_id=key["id"])
    assert touched["last_used_at"] is not None

    assert store.count_active_agent_api_keys() == 1
    listed = store.list_agent_api_keys()
    assert [row["id"] for row in listed] == [key["id"]]

    revoked = store.revoke_agent_api_key(key_id=key["id"])
    assert revoked["revoked_at"] is not None
    assert store.count_active_agent_api_keys() == 0
    assert store.revoke_agent_api_key(key_id=key["id"]) is None  # already revoked

    key_events = [
        event["event_type"] for event in store.list_events(target_type="agent_api_key", target_id=str(key["id"]))
    ]
    assert key_events.count("agent.key_created") == 1
    assert key_events.count("agent.key_revoked") == 1

    with pytest.raises(sqlite3.IntegrityError):  # duplicate hash
        store.create_agent_api_key(
            {
                "agent_id": "other",
                "permission_profile": "read_only_agent",
                "key_hash": "b" * 64,
                "key_prefix": "ak_live_b2",
            }
        )
    with pytest.raises(sqlite3.IntegrityError):  # invalid profile
        store.create_agent_api_key(
            {
                "agent_id": "other",
                "permission_profile": "root",
                "key_hash": "d" * 64,
                "key_prefix": "ak_live_d",
            }
        )
    conn.close()


# -- events --------------------------------------------------------------------------------


def test_append_event_and_list_events_roundtrip() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    row = store.append_event(
        {
            "event_type": "capture.completed",
            "actor_type": "agent",
            "actor_id": "hermes",
            "target_type": "source",
            "target_id": "abc",
            "payload_json": {"items": 3},
            "trace_id": "trace-1",
            "run_id": "run-1",
            "integrity_hash": "deadbeef",
        }
    )
    assert row["event_type"] == "capture.completed"
    assert row["payload_json"] == {"items": 3}
    assert row["user_id"] == store.user_id
    assert isinstance(row["occurred_at"], str) and row["occurred_at"].endswith("Z")

    by_target = store.list_events(target_type="source", target_id="abc")
    assert [event["id"] for event in by_target] == [row["id"]]
    assert store.list_events(target_type="memory", target_id="abc") == []

    agent_events = store.list_agent_events(agent_id="hermes")
    assert [event["id"] for event in agent_events] == [row["id"]]

    with pytest.raises(sqlite3.IntegrityError):  # empty event_type violates length CHECK
        store.append_event({"event_type": "", "actor_type": "system"})
    conn.close()


# -- temporal slice: graph edges, as-of reads, supersession pointers -----------


def _create_edge(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    edge: dict[str, object] = {
        "from_type": "source",
        "from_id": "source-1",
        "to_type": "memory",
        "to_id": "memory-1",
        "edge_type": "supports",
        "confidence": 0.7,
        "created_by": "system",
    }
    edge.update(overrides)
    return store.create_graph_edge(edge)


def test_create_graph_edge_defaults_event_time_to_now_and_notes_the_fallback() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    edge = _create_edge(store)

    assert isinstance(edge["observed_at"], str) and edge["observed_at"].endswith("Z")
    # valid_from starts the validity interval at event time, so it is no
    # longer a dead column.
    assert edge["valid_from"] == edge["observed_at"]
    assert edge["valid_to"] is None
    # No source context was available, so write time stands in and the
    # fallback is recorded on the edge.
    assert edge["metadata_json"]["observed_at_source"] == "now"
    events = store.list_events(target_type="graph_edge", target_id=str(edge["id"]))
    assert [event["event_type"] for event in events] == ["graph_edge.created"]
    conn.close()


def test_create_graph_edge_keeps_provided_event_time_and_metadata() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    edge = _create_edge(
        store,
        observed_at="2026-01-05T08:00:00Z",
        metadata_json={"observed_at_source": "source_created_at", "status": "candidate"},
    )

    assert edge["observed_at"] == "2026-01-05T08:00:00Z"
    assert edge["valid_from"] == "2026-01-05T08:00:00Z"
    assert edge["metadata_json"]["observed_at_source"] == "source_created_at"
    assert edge["metadata_json"]["status"] == "candidate"

    # An explicit valid_from wins over the observed_at default.
    explicit = _create_edge(
        store,
        observed_at="2026-01-05T08:00:00Z",
        valid_from="2026-01-06T08:00:00Z",
        edge_type="similar_to",
    )
    assert explicit["valid_from"] == "2026-01-06T08:00:00Z"
    conn.close()


def test_create_graph_edge_rejects_invalid_edge_type() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _create_edge(store, edge_type="not-an-edge-type")
    conn.close()


def test_list_edges_filters_by_endpoint_and_excludes_closed_edges() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    open_edge = _create_edge(store, from_id="source-1", to_id="memory-1")
    other_edge = _create_edge(store, from_id="source-2", to_id="memory-2", edge_type="mentions")
    _closed = _create_edge(
        store,
        from_id="source-1",
        to_id="memory-3",
        edge_type="contradicts",
        observed_at="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
    )

    assert {row["id"] for row in store.list_edges()} == {open_edge["id"], other_edge["id"]}
    assert [row["id"] for row in store.list_edges(from_id="source-1")] == [open_edge["id"]]
    assert [row["id"] for row in store.list_edges(to_id="memory-2")] == [other_edge["id"]]
    conn.close()


def test_list_edges_as_of_returns_the_graph_as_it_was_at_that_instant() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    early = _create_edge(store, observed_at="2026-01-01T00:00:00Z")
    closed = _create_edge(
        store,
        edge_type="contradicts",
        observed_at="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
    )
    late = _create_edge(store, edge_type="similar_to", observed_at="2026-03-01T00:00:00Z")

    # Mid-January: both January edges were in effect; March's did not exist yet.
    assert {row["id"] for row in store.list_edges_as_of("2026-01-15T00:00:00Z")} == {
        early["id"],
        closed["id"],
    }
    # The interval is half-open: an edge closed exactly at 'at' is out.
    assert {row["id"] for row in store.list_edges_as_of("2026-02-01T00:00:00Z")} == {early["id"]}
    # After March both open edges are in effect, and limit caps the result.
    assert {row["id"] for row in store.list_edges_as_of("2026-03-02T00:00:00Z")} == {
        early["id"],
        late["id"],
    }
    assert len(store.list_edges_as_of("2026-03-02T00:00:00Z", limit=1)) == 1
    # Before any edge existed the graph was empty.
    assert store.list_edges_as_of("2025-12-31T00:00:00Z") == []
    conn.close()


def test_edge_reads_are_scoped_to_the_bound_user() -> None:
    conn = _open_connection()
    alice = _make_store(conn)
    mallory = _make_store(conn)

    edge = _create_edge(alice, observed_at="2026-01-01T00:00:00Z")

    assert [row["id"] for row in alice.list_edges()] == [edge["id"]]
    assert [row["id"] for row in alice.list_edges_as_of("2026-01-02T00:00:00Z")] == [edge["id"]]
    assert mallory.list_edges() == []
    assert mallory.list_edges_as_of("2026-01-02T00:00:00Z") == []
    conn.close()


def test_memory_supersession_pointer_columns_roundtrip() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    old = _create_memory(store, canonical_text="superseded fact original")
    replacement = _create_memory(
        store,
        canonical_text="superseded fact replacement",
        supersedes=old["id"],
    )

    updated = store.update_memory(
        memory_id=str(old["id"]),
        patch={"status": "superseded", "superseded_by": replacement["id"]},
    )

    assert replacement["supersedes"] == old["id"]
    assert replacement["superseded_by"] is None
    assert updated["superseded_by"] == replacement["id"]
    assert store.get_memory(str(old["id"]))["superseded_by"] == replacement["id"]
    assert store.get_memory(str(replacement["id"]))["supersedes"] == old["id"]
    conn.close()


def test_bootstrap_upgrades_a_pre_existing_db_file_with_the_temporal_slice(tmp_path: Path) -> None:
    """Files created before the temporal slice shipped gain the supersession
    pointer columns (backfilled from the metadata_json copies, mirroring
    Postgres migration 20260704_0077) and the graph_edges substrate."""
    db_path = tmp_path / "alice.db"
    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Test User")
    store = SQLiteVNextStore(conn, user_id)
    # Rows whose supersession was recorded in metadata only (the pre-column
    # convention of the supersede-existing review flow).
    old = _create_memory(
        store,
        canonical_text="pre-column original",
        status="superseded",
        metadata_json={"superseded_by": "11111111-1111-4111-8111-111111111111"},
    )
    replacement = _create_memory(
        store,
        canonical_text="pre-column replacement",
        metadata_json={"supersedes": str(old["id"])},
    )
    conn.commit()

    # Rewind the file to the pre-temporal-slice schema.
    conn.execute("DROP TRIGGER IF EXISTS memories_expire_derived_entity_edges")
    conn.execute("DROP INDEX IF EXISTS memories_user_superseded_by_idx")
    conn.execute("DROP INDEX IF EXISTS graph_edges_user_edge_idx")
    conn.execute("DROP TABLE graph_edges")
    conn.execute("ALTER TABLE memories DROP COLUMN superseded_by")
    conn.execute("ALTER TABLE memories DROP COLUMN supersedes")
    conn.commit()
    assert "superseded_by" not in {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    conn.close()

    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)

    memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    assert {"superseded_by", "supersedes"} <= memory_columns
    table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "graph_edges" in table_names
    index_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    assert {"memories_user_superseded_by_idx", "graph_edges_user_edge_idx"} <= index_names

    # The metadata-only pointers were backfilled into the real columns.
    upgraded = SQLiteVNextStore(conn, user_id)
    assert upgraded.get_memory(str(old["id"]))["superseded_by"] == "11111111-1111-4111-8111-111111111111"
    assert upgraded.get_memory(str(replacement["id"]))["supersedes"] == old["id"]

    # A second bootstrap does not re-run the backfill or fail.
    bootstrap_sqlite_schema(conn)
    assert upgraded.get_memory(str(replacement["id"]))["supersedes"] == old["id"]

    # The upgraded file has the full graph substrate.
    edge = upgraded.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(replacement["id"]),
            "to_type": "memory",
            "to_id": str(old["id"]),
            "edge_type": "supersedes",
            "created_by": "system",
        }
    )
    assert [row["id"] for row in upgraded.list_edges()] == [edge["id"]]
    conn.close()


# -- entity substrate: resolution, mentions, relationship history ---------------


def _create_entity(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    entity: dict[str, object] = {
        "entity_type": "organization",
        "name": "OpenAI",
    }
    entity.update(overrides)
    return store.create_entity(entity)


def test_create_entity_get_roundtrip_computes_normalized_name_and_defaults() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    created = _create_entity(store, name="OpenAI, Inc.")
    assert created["name"] == "OpenAI, Inc."
    assert created["normalized_name"] == "openai inc"
    assert created["entity_type"] == "organization"
    assert created["aliases"] == []
    assert created["metadata_json"] == {}
    assert created["mention_count"] == 0
    assert created["first_observed_at"] is None
    assert created["last_observed_at"] is None
    assert created["deleted_at"] is None
    assert isinstance(created["created_at"], str) and created["created_at"].endswith("Z")

    fetched = store.get_entity(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["normalized_name"] == "openai inc"
    assert store.get_entity(str(uuid4())) is None

    by_name = store.get_entity_by_normalized_name("organization", "openai inc")
    assert by_name is not None
    assert by_name["id"] == created["id"]
    assert store.get_entity_by_normalized_name("person", "openai inc") is None
    assert store.get_entity_by_normalized_name("organization", "OpenAI, Inc.") is None

    # A caller-provided normalized_name wins over the computed one.
    explicit = _create_entity(store, name="Whatever Display Name", normalized_name="custom key")
    assert explicit["normalized_name"] == "custom key"
    assert store.get_entity_by_normalized_name("organization", "custom key")["id"] == explicit["id"]

    # Creates emit a mutation event with the audited field list.
    events = store.list_events(target_type="entity", target_id=str(created["id"]))
    assert [event["event_type"] for event in events] == ["entity.created"]
    assert events[0]["payload_json"]["operation"] == "create"
    assert events[0]["payload_json"]["entity_type"] == "organization"
    assert "name" in events[0]["payload_json"]["fields"]
    conn.close()


def test_create_entity_enforces_normalized_name_uniqueness_per_user_and_type() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    _create_entity(store, name="OpenAI")

    # Different surface spellings of the same normalized name collide.
    with pytest.raises(sqlite3.IntegrityError):
        _create_entity(store, name='"OpenAI,"')

    # The same normalized name under another entity_type is a new entity.
    topic = _create_entity(store, name="OpenAI", entity_type="topic")
    assert topic["normalized_name"] == "openai"

    # Another user's namespace is independent.
    other = _make_store(conn)
    assert _create_entity(other, name="openai")["normalized_name"] == "openai"

    with pytest.raises(sqlite3.IntegrityError):
        _create_entity(store, name="Acme", entity_type="not_a_type")
    # A pure-punctuation name normalizes to "" and fails the length CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        _create_entity(store, name="...")
    conn.close()


def test_find_entities_by_names_matches_normalized_names_and_aliases_in_one_call() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    openai = _create_entity(store, name="OpenAI", aliases=["open ai"])
    northwind = _create_entity(store, name="Northwind Capital")
    _create_entity(store, name="Anthropic")

    # Mentions push northwind ahead in the mention_count DESC ordering.
    store.record_entity_mention(entity_id=northwind["id"], observed_at="2026-07-01T00:00:00Z")

    rows = store.find_entities_by_names(("northwind capital", "open ai"))
    assert [row["id"] for row in rows] == [northwind["id"], openai["id"]]

    # Alias matching is exact string equality, not substring.
    assert store.find_entities_by_names(("open",)) == []
    assert store.find_entities_by_names(()) == []
    conn.close()


def test_update_entity_replaces_aliases_and_rejects_immutable_fields() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    created = _create_entity(store, name="OpenAI", aliases=["oai"])

    updated = store.update_entity(
        entity_id=created["id"],
        patch={"name": "OpenAI (research lab)", "aliases": ["oai", "open ai"]},
    )
    assert updated["name"] == "OpenAI (research lab)"
    assert updated["aliases"] == ["oai", "open ai"]
    assert updated["normalized_name"] == created["normalized_name"]  # resolution key untouched
    assert updated["updated_at"] >= created["updated_at"]
    assert [row["id"] for row in store.find_entities_by_names(("open ai",))] == [created["id"]]

    update_events = [
        event
        for event in store.list_events(target_type="entity", target_id=str(created["id"]))
        if event["event_type"] == "entity.updated"
    ]
    assert len(update_events) == 1
    assert update_events[0]["payload_json"]["changes"]["aliases"] == ["oai", "open ai"]

    for immutable_patch in (
        {"normalized_name": "hijacked"},
        {"entity_type": "person"},
        {"id": str(uuid4())},
        {"user_id": str(uuid4())},
    ):
        with pytest.raises(ContinuityStoreInvariantError, match="immutable"):
            store.update_entity(entity_id=created["id"], patch=immutable_patch)
    with pytest.raises(ContinuityStoreInvariantError):
        store.update_entity(entity_id=str(uuid4()), patch={"name": "missing"})
    conn.close()


def test_record_entity_mention_counts_and_widens_the_observation_window() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    entity = _create_entity(store)

    first = store.record_entity_mention(entity_id=entity["id"], observed_at="2026-02-01T00:00:00Z")
    assert first["mention_count"] == 1
    assert first["first_observed_at"] == "2026-02-01T00:00:00Z"
    assert first["last_observed_at"] == "2026-02-01T00:00:00Z"

    # An out-of-order earlier mention widens the start but not the end.
    earlier = store.record_entity_mention(entity_id=entity["id"], observed_at="2026-01-01T00:00:00Z")
    assert earlier["mention_count"] == 2
    assert earlier["first_observed_at"] == "2026-01-01T00:00:00Z"
    assert earlier["last_observed_at"] == "2026-02-01T00:00:00Z"

    later = store.record_entity_mention(
        entity_id=entity["id"], observed_at="2026-03-01T00:00:00Z", source_id=str(uuid4())
    )
    assert later["mention_count"] == 3
    assert later["first_observed_at"] == "2026-01-01T00:00:00Z"
    assert later["last_observed_at"] == "2026-03-01T00:00:00Z"

    with pytest.raises(ContinuityStoreInvariantError, match="observed_at"):
        store.record_entity_mention(entity_id=entity["id"], observed_at=None)
    with pytest.raises(ContinuityStoreInvariantError):
        store.record_entity_mention(entity_id=str(uuid4()), observed_at="2026-03-01T00:00:00Z")

    mention_events = [
        event
        for event in store.list_events(target_type="entity", target_id=str(entity["id"]))
        if event["event_type"] == "entity.mention_recorded"
    ]
    assert len(mention_events) == 3
    assert mention_events[0]["payload_json"]["operation"] == "record_mention"
    conn.close()


def test_record_relationship_change_appends_history_and_tracks_current_type() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    entity = _create_entity(store, name="Jane Advisor", entity_type="person", metadata_json={"note": "keep"})

    first = store.record_relationship_change(
        entity_id=entity["id"], relationship_type="advisor", changed_at="2026-01-01T00:00:00Z"
    )
    assert first["relationship_type_before"] is None
    assert first["relationship_type_after"] == "advisor"
    assert first["changed_at"] == "2026-01-01T00:00:00Z"
    assert first["metadata_json"] == {}

    second = store.record_relationship_change(
        entity_id=entity["id"],
        relationship_type="investor",
        changed_at="2026-02-01T00:00:00Z",
        metadata_json={"round": "seed"},
    )
    assert second["relationship_type_before"] == "advisor"
    assert second["relationship_type_after"] == "investor"
    assert second["metadata_json"] == {"round": "seed"}

    # The entity carries the current pointer; caller metadata survives the merge.
    current = store.get_entity(entity["id"])
    assert current["metadata_json"] == {"note": "keep", "relationship_type": "investor"}

    # History lists most recent change first.
    listed = store.list_relationship_events(entity["id"])
    assert [row["id"] for row in listed] == [second["id"], first["id"]]

    with pytest.raises(ContinuityStoreInvariantError, match="existing entity"):
        store.record_relationship_change(entity_id=str(uuid4()), relationship_type="advisor")

    change_events = [
        event
        for event in store.list_events(target_type="entity", target_id=str(entity["id"]))
        if event["event_type"] == "entity.relationship_changed"
    ]
    assert len(change_events) == 2
    payloads = {event["payload_json"]["relationship_event_id"]: event["payload_json"] for event in change_events}
    assert payloads[str(second["id"])]["relationship_type_before"] == "advisor"
    assert payloads[str(second["id"])]["relationship_type_after"] == "investor"
    conn.close()


def test_entity_relationship_events_reject_update_and_delete() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    entity = _create_entity(store)
    store.record_relationship_change(entity_id=entity["id"], relationship_type="advisor")
    assert conn.execute("SELECT count(*) FROM entity_relationship_events").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE entity_relationship_events SET relationship_type_after = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM entity_relationship_events")
    conn.close()


def test_list_entities_filters_by_type_and_orders_by_recency() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    org = _create_entity(store, name="Northwind Capital")
    person = _create_entity(store, name="Sam Rusani", entity_type="person")
    store.update_entity(entity_id=org["id"], patch={"name": "Northwind.Example"})

    everything = store.list_entities()
    assert [row["id"] for row in everything] == [org["id"], person["id"]]  # most recently updated first
    assert [row["id"] for row in store.list_entities(entity_type="person")] == [person["id"]]
    assert store.list_entities(entity_type="market") == []
    assert len(store.list_entities(limit=1)) == 1
    conn.close()


def test_entity_reads_and_writes_are_scoped_to_the_bound_user() -> None:
    conn = _open_connection()
    alice = _make_store(conn)
    mallory = _make_store(conn)

    entity = _create_entity(alice, name="OpenAI", aliases=["open ai"])
    event = alice.record_relationship_change(entity_id=entity["id"], relationship_type="vendor")

    # Every new read method comes back empty for the other user.
    assert mallory.get_entity(entity["id"]) is None
    assert mallory.get_entity_by_normalized_name("organization", "openai") is None
    assert mallory.find_entities_by_names(("openai",)) == []
    assert mallory.find_entities_by_names(("open ai",)) == []
    assert mallory.list_entities() == []
    assert mallory.list_relationship_events(entity["id"]) == []

    # Mutations against another user's entity fail loudly.
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.update_entity(entity_id=entity["id"], patch={"name": "stolen"})
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.record_entity_mention(entity_id=entity["id"], observed_at="2026-07-01T00:00:00Z")
    with pytest.raises(ContinuityStoreInvariantError):
        mallory.record_relationship_change(entity_id=entity["id"], relationship_type="stolen")

    # Nothing Mallory attempted altered Alice's view.
    assert alice.get_entity(entity["id"])["name"] == "OpenAI"
    assert alice.get_entity(entity["id"])["mention_count"] == 0
    assert [row["id"] for row in alice.list_relationship_events(entity["id"])] == [event["id"]]
    conn.close()


def test_bootstrap_upgrades_a_pre_existing_db_file_with_the_entity_substrate(tmp_path: Path) -> None:
    """Files created before the entity substrate shipped gain the entities and
    entity_relationship_events tables (with their indexes and append-only
    triggers) from the idempotent bootstrap."""
    db_path = tmp_path / "alice.db"
    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    # Rewind the file to the pre-entity-substrate schema (dropping a table
    # drops its indexes and triggers with it).
    conn.execute("DROP TABLE entity_relationship_events")
    conn.execute("DROP TABLE vnext_entities")
    conn.commit()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "vnext_entities" not in tables
    conn.close()

    conn = sqlite3.connect(str(db_path))
    bootstrap_sqlite_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert {"vnext_entities", "entity_relationship_events"} <= tables
    index_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    assert "vnext_entities_user_normalized_name_idx" in index_names
    assert "entity_relationship_events_entity_changed_idx" in index_names
    trigger_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
    assert "entity_relationship_events_append_only_update" in trigger_names
    assert "entity_relationship_events_append_only_delete" in trigger_names

    # The upgraded file is fully usable, append-only enforcement included.
    store = _make_store(conn)
    entity = _create_entity(store, name="Northwind Capital")
    store.record_relationship_change(entity_id=entity["id"], relationship_type="portfolio")
    assert [row["relationship_type_after"] for row in store.list_relationship_events(entity["id"])] == ["portfolio"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM entity_relationship_events")
    conn.close()


# -- true redaction ------------------------------------------------------------


def _secret_memory(store: SQLiteVNextStore) -> dict[str, object]:
    return _create_memory(
        store,
        title="SECRET-TITLE quantum sprocket",
        canonical_text="SECRET-BODY the quantum sprocket calibration ritual",
        summary="SECRET-SUMMARY sprocket notes",
        value={"text": "SECRET-VALUE sprocket"},
        trust_reason="user said SECRET-TRUST",
        metadata_json={
            "note": "SECRET-META",
            "project_id": "proj-123",
            "consolidation_digest": "digest-abc",
        },
    )


def _secret_revision(store: SQLiteVNextStore, memory: dict[str, object]) -> dict[str, object]:
    return store.append_revision(
        {
            "memory_id": memory["id"],
            "memory_key": memory["memory_key"],
            "previous_value": {"text": "SECRET-OLD"},
            "new_value": {"text": "SECRET-NEW"},
            "candidate": {"text": "SECRET-CAND"},
            "text_before": "SECRET-BEFORE",
            "text_after": "SECRET-AFTER",
            "reason": "correcting SECRET-REASON",
            "revision_type": "edited",
            "metadata_json": {"note": "SECRET-REV-META"},
        }
    )


def _table_dump(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return repr(rows)


def test_redact_memory_content_expunges_content_and_archives() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    store.update_memory_embedding(memory_id=str(memory["id"]), vector=[0.5, 0.25])

    row = store.redact_memory_content(memory_id=str(memory["id"]))

    assert row["title"] == "[REDACTED]"
    assert row["canonical_text"] == "[REDACTED]"
    assert row["summary"] == "[REDACTED]"
    assert row["trust_reason"] == "[REDACTED]"
    assert row["value"] == {"redacted": True}
    assert row["status"] == "archived"
    assert row["deleted_at"] is not None
    metadata = row["metadata_json"]
    assert metadata["redacted"] is True
    assert metadata["redacted_at"]
    # Structural keys survive; content-bearing keys are gone.
    assert metadata["project_id"] == "proj-123"
    assert "consolidation_digest" not in metadata
    assert "source_refs" not in metadata
    assert "note" not in metadata
    # Skeleton is intact and the embedding is really gone.
    direct = conn.execute(
        "SELECT id, memory_key, created_at, embedding FROM memories WHERE id = ?",
        (str(memory["id"]),),
    ).fetchone()
    assert direct[0] == memory["id"]
    assert direct[1] == f"redacted.{memory['id']}"
    assert direct[2] == memory["created_at"]
    assert direct[3] is None
    assert "SECRET" not in _table_dump(conn, "memories")
    # Exactly one memory.redacted event was appended, itself content-free.
    redaction_events = [
        event
        for event in store.list_events(target_type="memory", target_id=str(memory["id"]))
        if event["event_type"] == "memory.redacted"
    ]
    assert len(redaction_events) == 1
    assert redaction_events[0]["payload_json"] == {"operation": "redact_memory_content"}
    conn.close()


def test_redact_memory_content_works_on_soft_deleted_memories() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    store.update_memory(memory_id=str(memory["id"]), patch={"status": "archived"})

    row = store.redact_memory_content(memory_id=str(memory["id"]))

    assert row["status"] == "archived"
    assert row["canonical_text"] == "[REDACTED]"
    assert "SECRET" not in _table_dump(conn, "memories")
    conn.close()


def test_redact_memory_content_missing_memory_raises() -> None:
    conn = _open_connection()
    store = _make_store(conn)

    with pytest.raises(ContinuityStoreInvariantError, match="did not find the memory"):
        store.redact_memory_content(memory_id=str(uuid4()))
    conn.close()


def test_redact_memory_revisions_scrubs_content_and_preserves_skeleton() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    created = store.append_revision(
        {
            "memory_id": memory["id"],
            "memory_key": memory["memory_key"],
            "new_value": {"text": "SECRET-FIRST"},
            "candidate": {"text": "SECRET-FIRST"},
            "text_after": "SECRET-FIRST",
            "revision_type": "created",
            "action": "ADD",
        }
    )
    edited = _secret_revision(store, memory)
    before = store.list_revisions(str(memory["id"]))
    assert len(before) == 2

    result = store.redact_memory_revisions(memory_id=str(memory["id"]))

    assert result == {"memory_id": str(memory["id"]), "redacted_revisions": 2}
    after = store.list_revisions(str(memory["id"]))
    assert len(after) == 2
    # Audit skeleton intact: ids, ordering numbers, types, actors, timestamps.
    for before_row, after_row in zip(before, after):
        for column in (
            "id",
            "memory_id",
            "sequence_no",
            "action",
            "revision_number",
            "revision_type",
            "actor_type",
            "actor_id",
            "created_at",
        ):
            assert after_row[column] == before_row[column]
        assert after_row["memory_key"] == f"redacted.{memory['id']}"
        assert after_row["source_event_ids"] == []
    by_id = {row["id"]: row for row in after}
    created_after = by_id[created["id"]]
    edited_after = by_id[edited["id"]]
    # NULL content stays NULL so the created-vs-edited shape survives.
    assert created_after["previous_value"] is None
    assert created_after["text_before"] is None
    assert created_after["reason"] is None
    assert created_after["text_after"] == "[REDACTED]"
    assert created_after["new_value"] == {"redacted": True}
    assert created_after["candidate"] == {"redacted": True}
    assert edited_after["previous_value"] == {"redacted": True}
    assert edited_after["new_value"] == {"redacted": True}
    assert edited_after["candidate"] == {"redacted": True}
    assert edited_after["text_before"] == "[REDACTED]"
    assert edited_after["text_after"] == "[REDACTED]"
    assert edited_after["reason"] == "[REDACTED]"
    assert edited_after["metadata_json"] == {"redacted": True}
    assert "SECRET" not in _table_dump(conn, "memory_revisions")
    redaction_events = [
        event
        for event in store.list_events(target_type="memory", target_id=str(memory["id"]))
        if event["event_type"] == "memory.redacted"
    ]
    assert len(redaction_events) == 1
    assert redaction_events[0]["payload_json"] == {
        "operation": "redact_memory_revisions",
        "redacted_revisions": 2,
    }
    conn.close()


@pytest.mark.parametrize(
    ("review_action", "terminal_status", "revision_type", "event_target_type"),
    [
        ("accept", "accepted", "promoted", "project"),
        ("reject", "rejected", "rejected", "artifact"),
    ],
)
def test_project_update_review_evidence_skeleton_survives_sqlite_redaction(
    review_action: str,
    terminal_status: str,
    revision_type: str,
    event_target_type: str,
) -> None:
    conn = _open_connection()
    store = _make_store(conn)
    project_id = str(uuid4())
    artifact_id = str(uuid4())
    actor_id = str(uuid4())
    memory = _create_memory(
        store,
        canonical_text=f"SECRET-{terminal_status}-PROJECT-STATE",
        project_id=project_id,
        project_scope=[project_id],
        memory_type="project_state",
    )
    memory_id = str(memory["id"])
    revision = store.append_revision(
        {
            "memory_id": memory_id,
            "memory_key": memory["memory_key"],
            "action": "project_update_review",
            "revision_type": revision_type,
            "text_before": f"SECRET-{terminal_status}-BEFORE",
            "text_after": f"SECRET-{terminal_status}-AFTER",
            "reason": f"SECRET-{terminal_status}-REASON",
            "actor_type": "user",
            "actor_id": actor_id,
            "metadata_json": {
                "artifact_id": artifact_id,
                "project_id": project_id,
                "action": review_action,
            },
        }
    )
    event_type = f"project.update_candidate_{terminal_status}"
    event_target_id = project_id if terminal_status == "accepted" else artifact_id
    event_payload = (
        {
            "artifact_id": artifact_id,
            "candidate_memory_id": memory_id,
            "action": review_action,
        }
        if terminal_status == "accepted"
        else {"project_id": project_id, "source_ids": []}
    )
    event = store.append_event(
        {
            "event_type": event_type,
            "actor_type": "user",
            "actor_id": actor_id,
            "target_type": event_target_type,
            "target_id": event_target_id,
            "payload_json": event_payload,
        }
    )

    # SQLite intentionally has no project/artifact repository surface. This
    # is store-evidence parity only; VNextProjectService replay belongs to the
    # live PostgreSQL test above its real repository implementation.
    store.redact_memory_revisions(memory_id=memory_id)
    store.redact_memory_events(memory_id=memory_id)

    redacted_revision = next(row for row in store.list_revisions(memory_id) if row["id"] == revision["id"])
    for field in (
        "id",
        "memory_id",
        "sequence_no",
        "action",
        "revision_number",
        "revision_type",
        "actor_type",
        "actor_id",
        "created_at",
    ):
        assert redacted_revision[field] == revision[field]
    assert redacted_revision["memory_key"] == f"redacted.{memory_id}"
    assert redacted_revision["source_event_ids"] == []
    assert redacted_revision["action"] == "project_update_review"
    assert redacted_revision["revision_type"] == revision_type
    assert redacted_revision["metadata_json"] == {"redacted": True}
    assert redacted_revision["text_before"] == "[REDACTED]"
    assert redacted_revision["text_after"] == "[REDACTED]"
    assert redacted_revision["reason"] == "[REDACTED]"

    redacted_event = next(row for row in store.list_events() if row["id"] == event["id"])
    for field in (
        "id",
        "event_type",
        "actor_type",
        "actor_id",
        "target_type",
        "target_id",
        "occurred_at",
        "trace_id",
        "run_id",
    ):
        assert redacted_event[field] == event[field]
    assert redacted_event["event_type"] == event_type
    assert redacted_event["target_type"] == event_target_type
    assert redacted_event["target_id"] == event_target_id
    if terminal_status == "accepted":
        assert redacted_event["payload_json"] == {
            "redacted": True,
            "memory_id": memory_id,
            "event_type": event_type,
        }
        assert redacted_event["integrity_hash"] is None
    else:
        assert redacted_event["payload_json"] == event_payload
    conn.close()


def test_project_update_candidate_created_event_keeps_exact_sqlite_redaction_skeleton() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _create_memory(store, status="candidate", memory_type="project_state")
    memory_id = str(memory["id"])
    event = store.append_event(
        {
            "event_type": "project.update_candidate_created",
            "actor_type": "agent",
            "actor_id": "review-agent",
            "target_type": "artifact",
            "target_id": str(uuid4()),
            "trace_id": "trace-candidate-created",
            "run_id": "run-candidate-created",
            "payload_json": {
                "candidate_memory_id": memory_id,
                "summary": "SECRET candidate content",
            },
            "integrity_hash": "secret-derived-hash",
        }
    )

    store.redact_memory_events(memory_id=memory_id)

    redacted = next(row for row in store.list_events() if row["id"] == event["id"])
    for field in (
        "id",
        "event_type",
        "actor_type",
        "actor_id",
        "target_type",
        "target_id",
        "occurred_at",
        "trace_id",
        "run_id",
    ):
        assert redacted[field] == event[field]
    assert redacted["payload_json"] == {
        "redacted": True,
        "memory_id": memory_id,
        "event_type": "project.update_candidate_created",
    }
    assert redacted["integrity_hash"] is None
    conn.close()


def test_redact_memory_events_scrubs_payloads_and_preserves_skeleton() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    # memory.updated carries the content patch in its payload.
    store.update_memory(memory_id=str(memory["id"]), patch={"summary": "SECRET-PATCH"})
    # An event referencing the memory only inside its payload.
    store.append_event(
        {
            "event_type": "custom.note",
            "actor_type": "system",
            "payload_json": {"memory_id": str(memory["id"]), "text": "SECRET-EVT"},
            "integrity_hash": "hash-abc",
        }
    )
    unrelated = store.append_event(
        {
            "event_type": "custom.other",
            "actor_type": "system",
            "payload_json": {"text": "UNRELATED-SECRET"},
        }
    )
    unrelated_uuid_prose = store.append_event(
        {
            "event_type": "custom.prose",
            "actor_type": "system",
            "payload_json": {"text": f"The unrelated note mentions {memory['id']} in prose."},
            "integrity_hash": "unrelated-prose-hash",
        }
    )
    before = {
        row["id"]: row
        for row in store.list_events()
        if (row["target_type"] == "memory" and row["target_id"] == str(memory["id"]))
        or (
            isinstance(row["payload_json"], dict)
            and (
                row["payload_json"].get("memory_id") == str(memory["id"])
                or row["payload_json"].get("candidate_memory_id") == str(memory["id"])
            )
        )
    }
    assert len(before) >= 3

    result = store.redact_memory_events(memory_id=str(memory["id"]))

    assert result["redacted_events"] == len(before)
    after = {row["id"]: row for row in store.list_events()}
    for event_id, before_row in before.items():
        after_row = after[event_id]
        # Skeleton intact.
        for column in (
            "event_type",
            "actor_type",
            "actor_id",
            "target_type",
            "target_id",
            "occurred_at",
            "trace_id",
            "run_id",
        ):
            assert after_row[column] == before_row[column]
        # Content gone: exactly the redaction shape, hash cleared.
        assert after_row["payload_json"] == {
            "redacted": True,
            "memory_id": str(memory["id"]),
            "event_type": before_row["event_type"],
        }
        assert after_row["integrity_hash"] is None
    # Unrelated events are untouched.
    assert after[unrelated["id"]]["payload_json"] == {"text": "UNRELATED-SECRET"}
    assert after[unrelated_uuid_prose["id"]]["payload_json"] == {
        "text": f"The unrelated note mentions {memory['id']} in prose."
    }
    assert after[unrelated_uuid_prose["id"]]["integrity_hash"] == "unrelated-prose-hash"
    dump = _table_dump(conn, "event_log")
    assert "SECRET-PATCH" not in dump
    assert "SECRET-EVT" not in dump
    assert "hash-abc" not in dump
    redaction_events = [row for row in after.values() if row["event_type"] == "memory.redacted"]
    assert len(redaction_events) == 1
    assert redaction_events[0]["payload_json"] == {
        "operation": "redact_memory_events",
        "redacted_events": len(before),
    }
    conn.close()


def test_append_only_still_enforced_for_normal_updates_after_redaction_support() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    revision = _secret_revision(store, memory)
    event = store.append_event({"event_type": "custom.note", "actor_type": "system", "payload_json": {"text": "x"}})

    # Without redaction mode: every UPDATE and DELETE is rejected.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE event_log SET payload_json = '{}' WHERE id = ?", (event["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event_log WHERE id = ?", (event["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE memory_revisions SET text_after = 'tampered' WHERE id = ?",
            (revision["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM memory_revisions WHERE id = ?", (revision["id"],))

    # Even WITH redaction mode on: skeleton mutations and non-marker
    # content are still rejected, and DELETE stays impossible.
    conn.execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE event_log SET event_type = 'evil' WHERE id = ?", (event["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                'UPDATE event_log SET payload_json = \'{"free": "rewrite"}\' WHERE id = ?',
                (event["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE memory_revisions SET text_after = 'rewritten history' WHERE id = ?",
                (revision["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM event_log WHERE id = ?", (event["id"],))
    finally:
        conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")
    conn.close()


@pytest.mark.parametrize(
    "linkage",
    [
        "memory_target",
        "payload_memory",
        "payload_candidate_memory",
        "artifact_target",
        "payload_artifact",
    ],
)
def test_sqlite_event_redaction_trigger_accepts_legitimate_old_linkage(linkage: str) -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory_id = str(uuid4())
    artifact_id = str(uuid4())
    event_type = "custom.content_bearing_event"
    target_type: str | None = None
    target_id: str | None = None
    payload: dict[str, object] = {"content": "SECRET"}

    if linkage == "memory_target":
        target_type = "memory"
        target_id = memory_id
    elif linkage == "payload_memory":
        payload["memory_id"] = memory_id
    elif linkage == "payload_candidate_memory":
        payload["candidate_memory_id"] = memory_id
    else:
        store.append_event(
            {
                "event_type": "project.update_candidate_created",
                "actor_type": "system",
                "target_type": "artifact",
                "target_id": artifact_id,
                "payload_json": {
                    "artifact_id": artifact_id,
                    "candidate_memory_id": memory_id,
                },
            }
        )
        if linkage == "artifact_target":
            target_type = "artifact"
            target_id = artifact_id
        else:
            payload["artifact_id"] = artifact_id

    event = store.append_event(
        {
            "event_type": event_type,
            "actor_type": "system",
            "target_type": target_type,
            "target_id": target_id,
            "payload_json": payload,
            "integrity_hash": "content-derived-hash",
        }
    )
    marker = json.dumps(
        {"redacted": True, "memory_id": memory_id, "event_type": event_type},
        separators=(",", ":"),
    )

    conn.execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        conn.execute(
            "UPDATE event_log SET payload_json = ?, integrity_hash = NULL WHERE id = ?",
            (marker, event["id"]),
        )
    finally:
        conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")

    redacted = next(row for row in store.list_events() if row["id"] == event["id"])
    assert redacted["payload_json"] == {
        "redacted": True,
        "memory_id": memory_id,
        "event_type": event_type,
    }
    assert redacted["integrity_hash"] is None
    conn.close()


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_memory_target",
        "conflicting_direct_links",
        "wrong_artifact_resolution",
        "conflicting_artifact_resolution",
        "other_user_artifact_resolution",
        "unlinked",
    ],
)
def test_sqlite_event_redaction_trigger_rejects_fabricated_or_unlinked_memory_id(
    failure: str,
) -> None:
    conn = _open_connection()
    store = _make_store(conn)
    linked_memory_id = str(uuid4())
    marker_memory_id = linked_memory_id
    competing_memory_id = str(uuid4())
    artifact_id = str(uuid4())
    target_type: str | None = None
    target_id: str | None = None
    payload: dict[str, object] = {"content": "SECRET"}

    if failure == "wrong_memory_target":
        target_type = "memory"
        target_id = linked_memory_id
        marker_memory_id = competing_memory_id
    elif failure == "conflicting_direct_links":
        target_type = "memory"
        target_id = linked_memory_id
        payload["candidate_memory_id"] = competing_memory_id
    elif failure != "unlinked":
        resolver_store = _make_store(conn) if failure == "other_user_artifact_resolution" else store
        resolver_store.append_event(
            {
                "event_type": "project.update_candidate_created",
                "actor_type": "system",
                "target_type": "artifact",
                "target_id": artifact_id,
                "payload_json": {
                    "artifact_id": artifact_id,
                    "candidate_memory_id": linked_memory_id,
                },
            }
        )
        if failure == "conflicting_artifact_resolution":
            store.append_event(
                {
                    "event_type": "project.update_candidate_accepted",
                    "actor_type": "system",
                    "payload_json": {
                        "artifact_id": artifact_id,
                        "candidate_memory_id": competing_memory_id,
                    },
                }
            )
        if failure == "wrong_artifact_resolution":
            marker_memory_id = competing_memory_id
            payload["artifact_id"] = artifact_id
        else:
            target_type = "artifact"
            target_id = artifact_id

    event_type = "custom.content_bearing_event"
    event = store.append_event(
        {
            "event_type": event_type,
            "actor_type": "system",
            "target_type": target_type,
            "target_id": target_id,
            "payload_json": payload,
            "integrity_hash": "content-derived-hash",
        }
    )
    marker = json.dumps(
        {
            "redacted": True,
            "memory_id": marker_memory_id,
            "event_type": event_type,
        },
        separators=(",", ":"),
    )

    conn.execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="event_log is append-only"):
            conn.execute(
                "UPDATE event_log SET payload_json = ?, integrity_hash = NULL WHERE id = ?",
                (marker, event["id"]),
            )
    finally:
        conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")

    unchanged = next(row for row in store.list_events() if row["id"] == event["id"])
    assert unchanged["payload_json"] == payload
    assert unchanged["integrity_hash"] == "content-derived-hash"
    conn.close()


def test_sqlite_redaction_trigger_preserves_revision_nullability_skeleton() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    nonnull_revision = _secret_revision(store, memory)
    null_revision = store.append_revision(
        {
            "memory_id": memory["id"],
            "memory_key": memory["memory_key"],
            "previous_value": None,
            "new_value": None,
            "candidate": {},
            "text_before": None,
            "text_after": "created",
            "reason": None,
            "metadata_json": {},
        }
    )
    conn.execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        update_sql = """
            UPDATE memory_revisions
            SET memory_key = 'redacted.' || memory_id,
                source_event_ids = '[]',
                candidate = '{"redacted":true}',
                text_before = ?,
                text_after = '[REDACTED]',
                reason = ?,
                previous_value = ?,
                new_value = ?,
                metadata_json = '{"redacted":true}'
            WHERE id = ?
        """
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                update_sql,
                ("[REDACTED]", None, None, None, null_revision["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                update_sql,
                (
                    None,
                    "[REDACTED]",
                    '{"redacted":true}',
                    '{"redacted":true}',
                    nonnull_revision["id"],
                ),
            )
    finally:
        conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")
    conn.close()


def test_redaction_mode_resets_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    _secret_revision(store, memory)
    original_execute = store._execute

    def failing_execute(query: str, params: tuple[object, ...] = ()):
        if "UPDATE memory_revisions" in query:
            raise RuntimeError("boom mid-redaction")
        return original_execute(query, params)

    monkeypatch.setattr(store, "_execute", failing_execute)
    with pytest.raises(RuntimeError, match="boom mid-redaction"):
        store.redact_memory_revisions(memory_id=str(memory["id"]))

    enabled = conn.execute("SELECT enabled FROM redaction_mode WHERE id = 1").fetchone()[0]
    assert enabled == 0
    # And append-only is still enforced afterwards.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE memory_revisions SET text_after = 'tampered'")
    conn.close()


def test_redaction_is_user_scoped() -> None:
    conn = _open_connection()
    store_a = _make_store(conn)
    store_b = _make_store(conn)
    memory = _secret_memory(store_a)
    _secret_revision(store_a, memory)

    with pytest.raises(ContinuityStoreInvariantError, match="did not find the memory"):
        store_b.redact_memory_content(memory_id=str(memory["id"]))
    # B sees none of A's events referencing the memory. (Run before the
    # revisions call: that call appends B's own memory.redacted audit
    # event, which a later redact_memory_events would legitimately match.)
    assert store_b.redact_memory_events(memory_id=str(memory["id"]))["redacted_events"] == 0
    assert store_b.redact_memory_revisions(memory_id=str(memory["id"]))["redacted_revisions"] == 0

    # User A's content is untouched.
    assert "SECRET-TITLE" in _table_dump(conn, "memories")
    assert "SECRET-BEFORE" in _table_dump(conn, "memory_revisions")
    assert [row for row in store_a.list_events() if row["event_type"] == "memory.redacted"] == []
    conn.close()


def test_redacted_memory_is_invisible_to_search() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    store.update_memory_embedding(memory_id=str(memory["id"]), vector=[0.5, 0.25])
    assert store.search_memories(query="sprocket") != []
    assert store.search_memories_fts(query="sprocket") != []
    assert store.search_memories_vector(query_vector=[0.5, 0.25]) != []

    store.redact_memory_content(memory_id=str(memory["id"]))

    assert store.search_memories(query="sprocket") == []
    assert store.search_memories_fts(query="sprocket") == []
    assert store.search_memories_vector(query_vector=[0.5, 0.25]) == []
    # The FTS shadow index itself no longer matches the redacted content.
    assert conn.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'sprocket'").fetchone()[0] == 0
    conn.close()


def test_quoted_provenance_cannot_reintroduce_content_after_sqlite_redaction() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)
    store.redact_memory_bundle(memory_id=str(memory["id"]), project_update_artifacts=[])

    with pytest.raises(ValueError, match="quoted provenance cannot be added to a redacted target"):
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": str(memory["id"]),
                "quote": "must not survive",
            }
        )

    assert store.list_provenance_links(target_type="memory", target_id=str(memory["id"])) == []
    conn.close()


def test_sqlite_redaction_timestamp_requires_marker_and_receipt_but_preserves_legacy_marker() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    fabricated = _create_memory(
        store,
        metadata_json={"redacted_at": "2001-02-03T04:05:06Z"},
        canonical_text="ordinary content",
    )
    first = store.redact_memory_bundle(memory_id=str(fabricated["id"]), project_update_artifacts=[])
    first_metadata = first["memory"]["metadata_json"]
    assert isinstance(first_metadata, dict)
    assert first_metadata["redacted_at"] != "2001-02-03T04:05:06Z"

    legacy = _create_memory(store, canonical_text="legacy content")
    legacy_id = str(legacy["id"])
    prior_timestamp = "2002-03-04T05:06:07Z"
    conn.execute(
        """
        UPDATE memories
        SET title = '[REDACTED]', canonical_text = '[REDACTED]', summary = NULL,
            trust_reason = '[REDACTED]', value = '{"redacted":true}',
            metadata_json = ?, embedding = NULL, fact_keys = NULL,
            status = 'archived', deleted_at = '2026-07-15T00:00:00Z'
        WHERE id = ? AND user_id = ?
        """,
        (json.dumps({"redacted": True, "redacted_at": prior_timestamp}), legacy_id, store.user_id),
    )
    store.append_event(
        {
            "event_type": "memory.redacted",
            "actor_type": "user",
            "target_type": "memory",
            "target_id": legacy_id,
            "payload_json": {"operation": "legacy_redaction"},
        }
    )

    repaired = store.redact_memory_bundle(memory_id=legacy_id, project_update_artifacts=[])
    repaired_metadata = repaired["memory"]["metadata_json"]
    assert isinstance(repaired_metadata, dict)
    assert repaired_metadata["redacted_at"] == prior_timestamp
    conn.close()


def test_sqlite_redact_flow_replay_proves_full_bundle_before_no_write_shortcut() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    memory = _secret_memory(store)

    first = redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")
    assert first["idempotent_replay"] is False
    frozen = (
        _table_dump(conn, "memories"),
        _table_dump(conn, "memory_revisions"),
        _table_dump(conn, "event_log"),
        _table_dump(conn, "provenance_links"),
    )

    second = redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")
    assert second["idempotent_replay"] is True
    assert second["redacted_revisions"] == 0
    assert second["redacted_events"] == 0
    assert (
        _table_dump(conn, "memories"),
        _table_dump(conn, "memory_revisions"),
        _table_dump(conn, "event_log"),
        _table_dump(conn, "provenance_links"),
    ) == frozen
    conn.close()


def test_bootstrap_upgrades_legacy_strict_append_only_triggers() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    # Simulate a database file created before the redaction-aware triggers.
    for table in ("event_log", "memory_revisions"):
        conn.execute(f"DROP TRIGGER {table}_append_only_update")
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )

    bootstrap_sqlite_schema(conn)

    for table in ("event_log", "memory_revisions"):
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (f"{table}_append_only_update",),
        ).fetchone()[0]
        assert "redaction_mode" in sql
        assert "[REDACTED]" in sql or table == "event_log"
        delete_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (f"{table}_append_only_delete",),
        ).fetchone()[0]
        assert "redaction_mode" not in delete_sql
    # The flag row exists and is off.
    assert conn.execute("SELECT enabled FROM redaction_mode WHERE id = 1").fetchone()[0] == 0
    conn.close()


def test_latest_agentic_commit_memory_finds_newest_by_agent() -> None:
    conn = _open_connection()
    store = _make_store(conn)
    mallory = _make_store(conn)
    first = _create_memory(
        store,
        memory_key="agentic-1",
        title="First commit",
        status="active",
        metadata_json={"agentic_memory": {"kind": "agentic_memory_commit", "agent_id": "hermes"}},
    )
    second = _create_memory(
        store,
        memory_key="agentic-2",
        title="Second commit",
        status="active",
        metadata_json={
            "agentic_memory": {
                "kind": "agentic_memory_commit",
                "agent_identity": {"agent_id": "openclaw"},
            }
        },
    )
    _create_memory(store, memory_key="manual-1", title="Manual note", status="active")

    newest_any = store.latest_agentic_commit_memory()
    assert newest_any is not None and newest_any["id"] == second["id"]
    hermes = store.latest_agentic_commit_memory(agent_id="hermes")
    assert hermes is not None and hermes["id"] == first["id"]
    openclaw = store.latest_agentic_commit_memory(agent_id="openclaw")
    assert openclaw is not None and openclaw["id"] == second["id"]
    assert store.latest_agentic_commit_memory(agent_id="unknown") is None
    # Cross-user isolation: another user's store sees nothing.
    assert mallory.latest_agentic_commit_memory() is None


def test_utc_now_iso_always_carries_fractional_seconds(monkeypatch) -> None:
    # A whole-second clock reading must not produce '...59Z': that string
    # sorts lexicographically after every '...59.000123Z' sibling and
    # corrupts timestamp ordering roughly once per million writes.
    from datetime import datetime as real_datetime

    from alicebot_api import sqlite_store
    from alicebot_api.vnext_stores.sqlite import primitives as sqlite_primitives

    class WholeSecondDatetime:
        @staticmethod
        def now(tz):
            return real_datetime(2026, 7, 7, 23, 59, 59, 0, tzinfo=tz)

    monkeypatch.setattr(sqlite_primitives, "datetime", WholeSecondDatetime)
    stamped = sqlite_store._utc_now_iso()
    assert stamped == "2026-07-07T23:59:59.000000Z"
    assert stamped < "2026-07-07T23:59:59.000001Z"
