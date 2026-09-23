from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

import alicebot_api.vnext_store as vnext_store_module
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_capture import capture_dedupe_key_for_text
from alicebot_api.vnext_embeddings import memory_embedding_content_sha256
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_stores.postgres import memory_lifecycle as postgres_memory_lifecycle
from alicebot_api.vnext_store import (
    PostgresVNextStore,
    _jsonb_project_scope_values_sql,
    _search_patterns,
)


class RecordingCursor:
    def __init__(
        self, fetchone_results: list[dict[str, Any]], fetchall_result: list[dict[str, Any]] | None = None
    ) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.fetchone_results = list(fetchone_results)
        self.fetchall_result = fetchall_result or []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        if params is not None:
            assert query.count("%s") == len(params)
        self.executed.append((query, params))

    def fetchone(self) -> dict[str, Any] | None:
        if not self.fetchone_results:
            return None
        return self.fetchone_results.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.fetchall_result


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor) -> None:
        self.cursor_instance = cursor

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def _event_row(target_id: object | None = None) -> dict[str, object]:
    return {
        "id": uuid4(),
        "event_type": "audit",
        "target_id": str(target_id) if target_id is not None else None,
    }


def _event_log_insert_count(cursor: RecordingCursor) -> int:
    return sum(1 for query, _params in cursor.executed if "INSERT INTO event_log" in query)


def test_postgres_project_scope_sql_mirrors_conservative_python_identity() -> None:
    sql = _jsonb_project_scope_values_sql(
        "metadata_json",
        legacy_keys=("project_id", "project", "projects"),
        project_id_expression="project_id",
    )

    assert "octet_length(normalized_scope.value) = char_length(normalized_scope.value)" in sql
    assert "translate(" in sql
    assert "chr(9) || chr(10) || chr(11) || chr(12) || chr(13)" in sql
    assert 'COLLATE "C"' in sql
    assert "[[:space:]]" not in sql
    assert "SELECT DISTINCT lower" not in sql


def test_source_crud_and_chunks_write_audit_events() -> None:
    source_id = str(uuid4())
    chunk_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": source_id},
            _event_row(source_id),
            {"id": source_id},
            {
                "id": source_id,
                "content_hash": "sha256:abc",
                "dedupe_key": "capture-md5:legacy",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"path": "docs/spec.md"},
            },
            {"id": source_id},
            _event_row(source_id),
            {"id": source_id},
            _event_row(source_id),
            {"id": chunk_id, "source_id": source_id},
            _event_row(chunk_id),
        ],
        fetchall_result=[{"id": chunk_id, "source_id": source_id}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    created = store.create_source(
        {
            "id": source_id,
            "source_type": "document",
            "title": "Spec",
            "content_hash": "sha256:abc",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"path": "docs/spec.md"},
        }
    )
    fetched = store.get_source(source_id)
    updated = store.update_source(source_id=source_id, patch={"title": "Spec v2", "metadata_json": {"rev": 2}})
    deleted = store.delete_source(source_id=source_id)
    chunk = store.create_source_chunk(
        {
            "id": chunk_id,
            "source_id": source_id,
            "chunk_index": 0,
            "text": "Alice vNext",
            "token_count": 3,
            "metadata_json": {"section": "intro"},
        }
    )
    chunks = store.list_source_chunks(source_id, limit=17)

    assert created["id"] == source_id
    assert fetched is not None
    assert updated["id"] == source_id
    assert deleted["id"] == source_id
    assert chunk["source_id"] == source_id
    assert chunks == [{"id": chunk_id, "source_id": source_id}]
    assert _event_log_insert_count(cursor) == 4

    source_insert_query, source_insert_params = cursor.executed[0]
    assert "INSERT INTO sources" in source_insert_query
    assert source_insert_params is not None
    assert isinstance(source_insert_params[-1], Jsonb)
    assert source_insert_params[-1].obj == {"path": "docs/spec.md"}

    source_update_query, source_update_params = next(
        (query, params) for query, params in cursor.executed if "UPDATE sources" in query and "SET title" in query
    )
    assert "UPDATE sources" in source_update_query
    assert source_update_params is not None

    chunk_query, chunk_params = cursor.executed[-1]
    assert "WHERE source_id = %s::uuid" in chunk_query
    assert chunk_query.index("WHERE source_id") < chunk_query.index("LIMIT %s")
    assert chunk_params == (source_id, 17)

    with pytest.raises(ValueError, match="limit must be positive"):
        store.list_source_chunks(source_id, limit=0)
    store.list_source_chunks(source_id, limit=10_000)
    assert cursor.executed[-1][1] == (source_id, 501)
    assert isinstance(source_update_params[6], Jsonb)
    assert source_update_params[6].obj == {"rev": 2}


def test_get_source_by_content_hash_uses_dedupe_lookup() -> None:
    source_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[{"id": source_id, "content_hash": "sha256:abc"}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    source = store.get_source_by_content_hash("sha256:abc")

    assert source is not None
    assert source["id"] == source_id
    query, params = cursor.executed[0]
    assert "FROM sources" in query
    assert "content_hash = %s" in query
    assert "deleted_at IS NULL" in query
    assert params == ("sha256:abc",)


def test_get_or_create_source_uses_partial_unique_dedupe_claim() -> None:
    source_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": source_id, "content_hash": "sha256:abc", "dedupe_key": "sha256:scoped"},
            _event_row(source_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    source, created = store.get_or_create_source(
        {
            "id": source_id,
            "source_type": "manual_text",
            "content_hash": "sha256:abc",
            "dedupe_key": "sha256:scoped",
        }
    )

    assert created is True
    assert source["id"] == source_id
    query, _params = cursor.executed[0]
    assert "ON CONFLICT (user_id, dedupe_key)" in query
    assert "WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL" in query
    assert "DO NOTHING" in query


def test_get_or_create_source_returns_concurrent_winner_without_create_event() -> None:
    source_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            None,  # INSERT lost the unique-key race.
            {"id": source_id, "content_hash": "sha256:abc", "dedupe_key": "sha256:scoped"},
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    source, created = store.get_or_create_source(
        {
            "source_type": "manual_text",
            "content_hash": "sha256:abc",
            "dedupe_key": "sha256:scoped",
        }
    )

    assert created is False
    assert source["id"] == source_id
    assert len(cursor.executed) == 2
    assert "SELECT" in cursor.executed[1][0]
    assert _event_log_insert_count(cursor) == 0


def test_get_or_create_source_rejects_incompatible_postgres_conflict_winner() -> None:
    source_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            None,
            {
                "id": source_id,
                "content_hash": "sha256:abc",
                "dedupe_key": "capture-md5:stale",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_scope": ["Beta"]},
            },
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ContinuityStoreInvariantError, match="does not match capture identity"):
        store.get_or_create_source(
            {
                "source_type": "manual_text",
                "content_hash": "sha256:abc",
                "dedupe_key": "capture-md5:stale",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_scope": ["Alpha"]},
            }
        )

    assert _event_log_insert_count(cursor) == 0


def test_update_source_recomputes_postgres_dedupe_key_with_the_same_statement() -> None:
    source_id = str(uuid4())
    raw_text = "Fact: PostgreSQL source review rotates the capture identity."
    current = {
        "id": source_id,
        "content_hash": "sha256:abc",
        "dedupe_key": capture_dedupe_key_for_text(
            raw_text,
            ("Alpha",),
            domain="project",
            sensitivity="private",
        ),
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": raw_text, "project_scope": ["Alpha"]},
    }
    cursor = RecordingCursor(
        fetchone_results=[
            current,
            None,
            {**current, "domain": "professional"},
            _event_row(source_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.update_source(
        source_id=source_id,
        patch={
            "domain": "professional",
            "metadata_json": {"raw_text": raw_text, "project_scope": ["Beta"]},
        },
    )

    update_query, update_params = next(
        (query, params) for query, params in cursor.executed if "UPDATE sources" in query and "SET title" in query
    )
    assert "metadata_json = COALESCE(%s, metadata_json)" in update_query
    assert "dedupe_key = %s" in update_query
    assert update_params is not None
    assert update_params[8] == capture_dedupe_key_for_text(
        raw_text,
        ("Beta",),
        domain="professional",
        sensitivity="private",
    )


def test_update_source_postgres_collision_fails_before_mutation_event() -> None:
    source_id = str(uuid4())
    raw_text = "Fact: Collision owners remain unchanged."
    current = {
        "id": source_id,
        "content_hash": "sha256:first",
        "dedupe_key": capture_dedupe_key_for_text(
            raw_text,
            ("Alpha",),
            domain="project",
            sensitivity="private",
        ),
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": raw_text, "project_scope": ["Alpha"]},
    }
    cursor = RecordingCursor(fetchone_results=[current, {"id": str(uuid4())}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ContinuityStoreInvariantError, match="already belongs"):
        store.update_source(
            source_id=source_id,
            patch={"metadata_json": {"raw_text": raw_text, "project_scope": ["Beta"]}},
        )

    assert not any("UPDATE sources" in query for query, _params in cursor.executed)
    assert _event_log_insert_count(cursor) == 0


def test_update_source_postgres_releases_key_when_changed_identity_has_no_raw_text() -> None:
    source_id = str(uuid4())
    current = {
        "id": source_id,
        "content_hash": "sha256:legacy",
        "dedupe_key": "capture-md5:legacy",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"project_scope": ["Alpha"]},
    }
    cursor = RecordingCursor(
        fetchone_results=[
            current,
            {**current, "dedupe_key": None, "metadata_json": {"project_scope": ["Beta"]}},
            _event_row(source_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    updated = store.update_source(
        source_id=source_id,
        patch={"metadata_json": {"project_scope": ["Beta"]}},
    )

    update_params = next(
        params for query, params in cursor.executed if "UPDATE sources" in query and "SET title" in query
    )
    assert update_params is not None
    assert update_params[8] is None
    assert updated["dedupe_key"] is None


def test_search_patterns_strip_quotes_and_add_keyword_fallbacks() -> None:
    patterns = _search_patterns('"agent-first /vnext audit correction cockpit"')

    assert patterns[0] == "%agent-first /vnext audit correction cockpit%"
    assert "%agent-first%" in patterns
    assert "%vnext%" in patterns
    assert "%audit%" in patterns
    assert "%correction%" in patterns
    assert "%cockpit%" in patterns


def test_search_patterns_drop_the_full_snowball_stopword_list() -> None:
    # Regression: the LIKE fallback used a private 11-word stopword set
    # while claiming parity with the snowball FTS_QUERY_STOPWORDS list, so
    # question words like "how"/"did"/"our" became %how% patterns that
    # matched nearly every row. Both paths now share the snowball list.
    patterns = _search_patterns("How did our announcement go out today")

    assert patterns[0] == "%How did our announcement go out today%"
    assert "%announcement%" in patterns
    assert "%go%" in patterns
    assert "%today%" in patterns
    for stopword_pattern in ("%how%", "%did%", "%our%", "%out%"):
        assert stopword_pattern not in patterns


def test_keyword_search_methods_apply_domain_sensitivity_and_limit_filters() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[
            {"id": "matched-1", "domain": "project", "sensitivity": "private"},
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    memories = store.search_memories(
        query="Alice provenance",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=4,
    )
    sources = store.search_sources(
        query="Alice provenance",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=3,
    )
    open_loops = store.list_open_loops(
        status="open",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=2,
    )

    assert memories[0]["id"] == "matched-1"
    assert sources[0]["id"] == "matched-1"
    assert open_loops[0]["id"] == "matched-1"
    memory_query, memory_params = cursor.executed[0]
    source_query, source_params = cursor.executed[1]
    open_loop_query, open_loop_params = cursor.executed[2]
    assert "FROM memories" in memory_query
    assert "status IN ('active', 'accepted')" in memory_query
    assert "domain = ANY" in memory_query
    assert "sensitivity = ANY" in memory_query
    assert "memory_type = ANY" in memory_query
    assert "metadata_json -> 'project_scope'" in memory_query
    assert "?| %s::text[]" in memory_query
    assert "created_by_agent_id = ANY" in memory_query
    assert "run_id = %s" in memory_query
    assert "valid_to IS NULL OR valid_to >= clock_timestamp()" in memory_query
    assert "ILIKE ANY" in memory_query
    assert memory_params is not None
    # Unset filters arrive as NULLs, expiry defaults to excluded (False).
    assert memory_params[4] is None  # memory_types
    assert memory_params[6] is None  # projects
    assert memory_params[8] is None  # created_by_agent_ids
    assert memory_params[10] is None  # run_id
    assert memory_params[12] is False  # include_expired
    assert memory_params[13] == ["%Alice provenance%", "%alice%", "%provenance%"]
    assert memory_params[-1] == 4
    assert "FROM sources" in source_query
    assert "ILIKE ANY" in source_query
    assert source_params is not None
    assert source_params[12] == ["%Alice provenance%", "%alice%", "%provenance%"]
    assert source_params[-1] == 3
    assert "FROM open_loops" in open_loop_query
    assert "%s::text IS NULL OR status = %s" in open_loop_query
    assert "%s::uuid IS NULL OR project_id = %s::uuid" in open_loop_query
    assert "%s::uuid IS NULL OR person_id = %s::uuid" in open_loop_query
    assert open_loop_params == (
        "open",
        "open",
        ["project"],
        ["project"],
        ["public", "private"],
        ["public", "private"],
        None,  # exact project id
        None,
        None,  # exact person id
        None,
        None,  # scoped projects
        None,
        None,  # scoped people
        None,
        None,
        None,  # window start
        None,
        None,  # window end
        None,
        2,
    )


def test_project_scope_sql_uses_canonical_key_precedence_for_all_resources() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))
    project = " Canonical-Project "

    store.list_memories(projects=(project,), limit=1)
    store.search_sources(query="scope marker", scope_projects=(project,), limit=1)
    store.list_artifacts(scope_projects=(project,), limit=1)
    store.list_open_loops(scope_projects=(project,), limit=1)

    memory_query, source_query, artifact_query, open_loop_query = [query for query, _ in cursor.executed]
    for query, metadata_expression in (
        (memory_query, "metadata_json"),
        (artifact_query, "metadata_json"),
        (open_loop_query, "metadata_json"),
    ):
        canonical_guard = f"{metadata_expression} ? 'project_scope'"
        nested_fallback = f"{metadata_expression} #> '{{agentic_memory,project_scope}}'"
        assert canonical_guard in query
        assert query.index(canonical_guard) < query.index(nested_fallback)
        assert f"jsonb_typeof({metadata_expression} -> 'project_scope') = 'array'" in query
        assert "ELSE '[]'::jsonb" in query
        assert f"{metadata_expression} #> '{{agent_identity,project_scope}}'" in query
        assert f"({metadata_expression} -> 'agentic_memory') ? 'project_scope'" in query
        assert f"({metadata_expression} -> 'agent_identity') ? 'project_scope'" in query
        assert "WITH RECURSIVE" in query
        assert "'string', 'boolean'" in query
        assert "'number'" in query
        assert "::numeric = trunc(" in query
        assert "THEN '0'" in query
        assert "octet_length(normalized_scope.value) = char_length(normalized_scope.value)" in query
        assert "translate(" in query
        assert "chr(9) || chr(10) || chr(11) || chr(12) || chr(13)" in query
        assert 'COLLATE "C"' in query
        assert "[[:space:]]" not in query
        assert "?| %s::text[]" in query

    source_canonical_guards = (
        "WHEN source_resource.value ? 'project_scope'",
        "WHEN source_containers.metadata_json ? 'project_scope'",
        "WHEN source_containers.scope_json ? 'project_scope'",
    )
    assert all(guard in source_query for guard in source_canonical_guards)
    assert [source_query.index(guard) for guard in source_canonical_guards] == sorted(
        source_query.index(guard) for guard in source_canonical_guards
    )
    assert "source_nested.agentic_memory -> 'project_scope'" in source_query
    assert "source_nested.agent_identity -> 'project_scope'" in source_query
    assert "source_candidates.nested_scope_present" in source_query
    for nested_type, nested_presence in (
        (
            "source_nested.agentic_memory",
            "source_nested.agentic_memory ? 'project_scope'",
        ),
        (
            "source_nested.agent_identity",
            "source_nested.agent_identity ? 'project_scope'",
        ),
        (
            "source_containers.scope_json -> 'agentic_memory'",
            "(source_containers.scope_json -> 'agentic_memory') ? 'project_scope'",
        ),
        (
            "source_containers.scope_json -> 'agent_identity'",
            "(source_containers.scope_json -> 'agent_identity') ? 'project_scope'",
        ),
    ):
        assert f"jsonb_typeof({nested_type}) = 'object'" in source_query
        assert nested_presence in source_query
    assert "nested_candidate_value" not in source_query
    assert "WITH RECURSIVE" in source_query
    assert "jsonb_array_elements" in source_query
    assert "'string', 'boolean'" in source_query
    assert "'number'" in source_query
    assert "::numeric = trunc(" in source_query
    assert "THEN '0'" in source_query
    assert "^-?(0|[1-9][0-9]*)$" not in source_query
    assert "source_containers.scope_json #> '{agent_identity,project_id}'" not in source_query
    assert "?| %s::text[]" in source_query

    assert "OR project_id::text = ANY" not in open_loop_query
    for _query, params in cursor.executed:
        assert params is not None
        assert "canonical-project" in str(params)
        assert project not in str(params)


def test_exact_memory_scope_lookup_uses_conservative_order_insensitive_identity() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.find_live_memory_by_canonical_text(
        "Same fact",
        domain="project",
        sensitivity="private",
        project_scope=(" Beta ", "ALICE", "alice"),
    )

    query, params = cursor.executed[0]
    assert "octet_length(normalized_scope.value) = char_length(normalized_scope.value)" in query
    assert 'COLLATE "C"' in query
    assert "metadata_json ? 'project_scope'" in query
    assert params is not None
    assert isinstance(params[-1], Jsonb)
    assert params[-1].obj == ["alice", "beta"]


def test_search_memories_by_time_builds_window_predicate_and_proximity_order() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "memory-march", "valid_from": "2023-03-15T00:00:00Z"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))
    window_start = datetime(2023, 3, 1, tzinfo=UTC)
    window_end = datetime(2023, 4, 1, tzinfo=UTC)

    rows = store.search_memories_by_time(
        window_start=window_start,
        window_end=window_end,
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        memory_types=("decision",),
        limit=4,
    )

    assert rows[0]["id"] == "memory-march"
    query, params = cursor.executed[0]
    assert "FROM memories" in query
    assert "deleted_at IS NULL" in query
    assert "status IN ('active', 'accepted')" in query
    # Event time: explicit validity start, then observation, then write time.
    assert "COALESCE(valid_from, first_seen_at, created_at)" in query
    assert "COALESCE(valid_from, first_seen_at, created_at) >= %s::timestamptz" in query
    assert "COALESCE(valid_from, first_seen_at, created_at) < %s::timestamptz" in query
    # Closed [valid_from, valid_to) validity intervals overlap the window too.
    assert "valid_from IS NOT NULL" in query
    assert "valid_to IS NOT NULL" in query
    # Default expiry gate stays in force (include_expired=False).
    assert "valid_to IS NULL OR valid_to >= clock_timestamp()" in query
    # Proximity-to-center ordering with deterministic tie-breaks.
    assert "ABS(EXTRACT(EPOCH FROM (COALESCE(valid_from, first_seen_at, created_at) - %s::timestamptz)))" in query
    assert "updated_at DESC" in query
    assert params is not None
    assert params[0] == ["project"]  # domains
    assert params[2] == ["public", "private"]  # sensitivity_allowed
    assert params[4] == ["decision"]  # memory_types
    assert params[6] is None  # projects
    assert params[8] is None  # created_by_agent_ids
    assert params[10] is None  # run_id
    assert params[12] is False  # include_expired
    assert params[13] == window_start
    assert params[14] == window_end
    assert params[15] == window_end  # interval-overlap clause
    assert params[16] == window_start
    assert params[17] == datetime(2023, 3, 16, 12, 0, tzinfo=UTC)  # window center
    assert params[-1] == 4


def test_search_memories_by_time_treats_naive_windows_as_utc() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.search_memories_by_time(
        window_start=datetime(2023, 3, 1),
        window_end=datetime(2023, 4, 1),
    )

    _query, params = cursor.executed[0]
    assert params is not None
    assert params[13] == datetime(2023, 3, 1, tzinfo=UTC)
    assert params[14] == datetime(2023, 4, 1, tzinfo=UTC)


def test_search_memories_by_time_accepts_an_explicit_proximity_pivot() -> None:
    # Open "before X"/"since X" windows pass their closed edge so ordering
    # ranks events nearest the named boundary instead of a meaningless
    # century-spanning midpoint.
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))
    pivot = datetime(2023, 4, 1, tzinfo=UTC)

    store.search_memories_by_time(
        window_start=datetime(1900, 1, 1, tzinfo=UTC),
        window_end=pivot,
        window_center=pivot,
    )

    _query, params = cursor.executed[0]
    assert params is not None
    assert params[17] == pivot


def test_list_artifacts_applies_type_domain_sensitivity_and_limit_filters() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[
            {"id": "artifact-1", "artifact_type": "daily_brief", "domain": "project", "sensitivity": "private"},
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    artifacts = store.list_artifacts(
        artifact_type="daily_brief",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=5,
    )

    assert artifacts[0]["id"] == "artifact-1"
    query, params = cursor.executed[0]
    assert "FROM generated_artifacts" in query
    assert "%s::text IS NULL OR artifact_type = %s" in query
    assert "domain = ANY" in query
    assert "sensitivity = ANY" in query
    assert "project_scope" in query
    assert params == (
        "daily_brief",
        "daily_brief",
        ["project"],
        ["project"],
        ["public", "private"],
        ["public", "private"],
        None,
        None,
        5,
    )


def test_artifact_quality_ratings_insert_and_export_json_safe_payloads() -> None:
    artifact_id = str(uuid4())
    rating_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": artifact_id, "artifact_type": "daily_brief", "status": "needs_review"},
            {"id": rating_id, "artifact_id": artifact_id, "reviewer_id": "samir"},
            _event_row(artifact_id),
        ],
        fetchall_result=[{"id": rating_id, "artifact_id": artifact_id, "usefulness": 5}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    created = store.create_artifact_quality_rating(
        {
            "id": rating_id,
            "artifact_id": artifact_id,
            "reviewer_id": "samir",
            "usefulness": 5,
            "accuracy": 4,
            "source_grounding": 5,
            "novel_connections": 3,
            "actionability": 4,
            "hallucination_risk": 1,
            "verbosity": "right_sized",
            "missed_context": "Needs one more source.",
            "comments": "Useful artifact.",
            "metadata_json": {"prompt_hash": "sha256:test"},
        }
    )
    rows = store.list_artifact_quality_ratings(artifact_id=artifact_id, limit=10)

    assert created["id"] == rating_id
    assert rows == [{"id": rating_id, "artifact_id": artifact_id, "usefulness": 5}]
    assert _event_log_insert_count(cursor) == 1
    assert "FOR UPDATE" in cursor.executed[0][0]
    insert_query, insert_params = cursor.executed[1]
    assert "INSERT INTO artifact_quality_ratings" in insert_query
    assert insert_params is not None
    assert isinstance(insert_params[-1], Jsonb)
    assert insert_params[-1].obj == {"prompt_hash": "sha256:test"}
    list_query, list_params = cursor.executed[3]
    assert "FROM artifact_quality_ratings" in list_query
    assert list_params == (artifact_id, artifact_id, None, None, 10)


def test_artifact_quality_ratings_upsert_on_artifact_reviewer_conflict() -> None:
    artifact_id = str(uuid4())
    rating_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": artifact_id, "artifact_type": "daily_brief", "status": "needs_review"},
            {"id": rating_id, "artifact_id": artifact_id, "reviewer_id": "samir", "usefulness": 2},
            _event_row(artifact_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    created = store.create_artifact_quality_rating(
        {
            "artifact_id": artifact_id,
            "reviewer_id": "samir",
            "usefulness": 2,
            "verbosity": "too_shallow",
            "metadata_json": {},
        }
    )

    assert created["id"] == rating_id
    upsert_query, _upsert_params = cursor.executed[1]
    assert "ON CONFLICT (artifact_id, reviewer_id) DO UPDATE SET" in upsert_query
    assert "usefulness = EXCLUDED.usefulness" in upsert_query
    assert "metadata_json = EXCLUDED.metadata_json" in upsert_query


def test_quality_rating_rejects_exact_redacted_artifact_before_insert() -> None:
    artifact_id = str(uuid4())
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": artifact_id,
                "artifact_type": "project_update",
                "status": "accepted",
                "title": "[REDACTED]",
                "content_markdown": "[REDACTED]",
                "prompt_hash": None,
                "model_info_json": {"redacted": True},
                "metadata_json": {
                    "redacted": True,
                    "redacted_at": "2026-07-16T00:00:00Z",
                    "workflow": "project_auto_update",
                    "project_id": "project-1",
                    "project_scope": ["project-1"],
                    "candidate_memory_id": memory_id,
                    "review_action": "accept",
                },
            }
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ValueError, match="ratings cannot be added to a redacted artifact"):
        store.create_artifact_quality_rating({"artifact_id": artifact_id, "usefulness": 5})

    assert len(cursor.executed) == 1
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert not any("INSERT INTO artifact_quality_ratings" in query for query, _params in cursor.executed)


def test_list_beliefs_joins_memory_domain_sensitivity_filters() -> None:
    belief_id = str(uuid4())
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[
            {
                "id": belief_id,
                "memory_id": memory_id,
                "claim": "Alice should preserve provenance.",
                "domain": "project",
                "sensitivity": "private",
            },
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    beliefs = store.list_beliefs(
        status="active",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=6,
    )

    assert beliefs[0]["id"] == belief_id
    query, params = cursor.executed[0]
    assert "FROM beliefs b" in query
    assert "JOIN memories m" in query
    assert "%s::text IS NULL OR b.status = %s" in query
    assert "m.domain = ANY" in query
    assert "m.sensitivity = ANY" in query
    assert params == (
        "active",
        "active",
        ["project"],
        ["project"],
        ["public", "private"],
        ["public", "private"],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        6,
    )


def test_memory_revision_provenance_and_graph_methods_write_audit_events() -> None:
    memory_id = str(uuid4())
    revision_id = str(uuid4())
    provenance_id = str(uuid4())
    edge_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": memory_id},
            _event_row(memory_id),
            {"id": memory_id},
            # update_memory to a searchable status reads the stored row first,
            # for the credential activation check (S4.4 round 2, ruling C2).
            {"id": memory_id, "status": "candidate", "canonical_text": "Alice vNext is being built."},
            {"id": memory_id},
            _event_row(memory_id),
            {"id": revision_id, "memory_id": memory_id},
            _event_row(memory_id),
            {"id": provenance_id, "target_type": "memory", "target_id": memory_id},
            _event_row(memory_id),
            {"id": edge_id, "edge_type": "supports"},
            _event_row(edge_id),
            {"id": edge_id, "edge_type": "supports"},
            _event_row(edge_id),
            {"id": edge_id, "edge_type": "supports", "metadata_json": {"status": "accepted"}},
            _event_row(edge_id),
        ],
        fetchall_result=[{"id": memory_id}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_memory(
        {
            "id": memory_id,
            "memory_key": "project.alice.status",
            "value": {"status": "building"},
            "canonical_text": "Alice vNext is being built.",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"source": "spec"},
        }
    )
    store.get_memory(memory_id)
    store.update_memory(memory_id=memory_id, patch={"status": "active", "metadata_json": {"reviewed": True}})
    store.list_memories(status="active")
    store.append_revision(
        {
            "id": revision_id,
            "memory_id": memory_id,
            "memory_key": "project.alice.status",
            "previous_value": {"status": "candidate"},
            "new_value": {"status": "active"},
            "text_after": "Alice vNext is active.",
            "revision_type": "promoted",
        }
    )
    store.list_revisions(memory_id)
    store.create_provenance_link(
        {
            "id": provenance_id,
            "target_type": "memory",
            "target_id": memory_id,
            "evidence_role": "supports",
            "confidence": 0.9,
        }
    )
    store.list_provenance_links(target_type="memory", target_id=memory_id)
    store.create_edge(
        {
            "id": edge_id,
            "from_type": "memory",
            "from_id": memory_id,
            "to_type": "project",
            "to_id": "alice-vnext",
            "edge_type": "supports",
            "created_by": "system",
        }
    )
    store.list_edges(from_id=memory_id)
    store.update_edge_status(edge_id=edge_id, status="accepted")
    store.expire_edge(edge_id=edge_id)

    assert _event_log_insert_count(cursor) == 7
    memory_insert_query = cursor.executed[0][0]
    assert "INSERT INTO memories" in memory_insert_query
    assert "canonical_text" in memory_insert_query
    assert "domain" in memory_insert_query
    assert "sensitivity" in memory_insert_query
    assert "metadata_json" in memory_insert_query
    assert "ON CONFLICT DO NOTHING" in memory_insert_query
    assert "WHERE commit_digest IS NOT NULL" not in memory_insert_query
    revision_queries = [query for query, _params in cursor.executed if "next_revision AS" in query]
    assert len(revision_queries) == 1
    assert "locked_memory AS" in revision_queries[0]
    assert "FOR UPDATE" in revision_queries[0]
    assert any("INSERT INTO provenance_links" in query for query, _params in cursor.executed)
    assert any("INSERT INTO graph_edges" in query for query, _params in cursor.executed)
    assert any("UPDATE graph_edges" in query for query, _params in cursor.executed)
    assert any("%s::text IS NULL OR from_id = %s" in query for query, _params in cursor.executed)
    update_edge_query, update_edge_params = cursor.executed[-4]
    assert "metadata_json = metadata_json || %s" in update_edge_query
    assert update_edge_params is not None
    assert update_edge_params[1] == "accepted"


def test_create_memory_persists_canonical_multi_project_scope_metadata() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": memory_id,
                "memory_key": "shared.scope",
                "canonical_text": "Shared scope",
                "metadata_json": {"project_scope": ["alicebot", "hermes"]},
                "project_id": "alicebot",
            },
            _event_row(memory_id),
        ],
        fetchall_result=[],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.create_memory(
        {
            "id": memory_id,
            "memory_key": "shared.scope",
            "canonical_text": "Shared scope",
            "project_scope": ["alicebot", "hermes"],
        }
    )

    assert row["project_scope"] == ["alicebot", "hermes"]
    insert_params = cursor.executed[0][1]
    assert insert_params is not None
    metadata_values = [param.obj for param in insert_params if isinstance(param, Jsonb)]
    assert {"project_scope": ["alicebot", "hermes"]} in metadata_values


def test_get_memory_for_update_uses_a_row_lock() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[{"id": memory_id}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.get_memory_for_update(memory_id) == {"id": memory_id}
    query, params = cursor.executed[0]
    assert "FROM memories" in query
    assert "FOR UPDATE" in query
    assert params == (memory_id,)


def test_pending_derived_candidate_lookup_uses_snapshots_and_row_locks() -> None:
    member_id = str(uuid4())
    excluded_id = str(uuid4())
    candidate_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[{"id": candidate_id}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.list_pending_derived_candidates_for_member(
        member_id=member_id,
        exclude_memory_id=excluded_id,
    ) == [{"id": candidate_id}]

    query, params = cursor.executed[0]
    assert "status IN ('candidate', 'needs_review')" in query
    assert "member_snapshots" in query
    assert "jsonb_array_elements" in query
    assert "FOR UPDATE OF candidate" in query
    assert params == (excluded_id, excluded_id, member_id)


def test_list_memories_pushes_scope_and_limit_into_postgres_query() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert (
        store.list_memories(
            status="active",
            domains=["project"],
            sensitivity_allowed=["private"],
            limit=7,
        )
        == []
    )

    query, params = cursor.executed[-1]
    assert "status = %s" in query
    assert "domain = ANY(%s::text[]) OR domain = 'unknown'" in query
    assert "COALESCE(sensitivity, 'unknown') = ANY(%s::text[])" in query
    assert "LIMIT %s" in query
    assert params == ("active", ["project"], ["private"], 7)
    with pytest.raises(ValueError, match="limit must be positive"):
        store.list_memories(limit=0)


def test_resume_store_queries_apply_admission_predicates_before_limit() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))
    since = datetime(2030, 7, 10, tzinfo=UTC)
    until = datetime(2030, 7, 11, tzinfo=UTC)

    store.list_memories(
        status=None,
        statuses=("active", "candidate"),
        memory_types=("decision",),
        projects=("Project A",),
        created_at_start=since,
        created_at_end=until,
        query=r"Release%_\Marker",
        order_by_created_at=True,
        limit=1,
    )
    store.list_open_loops(
        status=None,
        statuses=("open", "waiting"),
        query=r"Release%_\Marker",
        scope_projects=("Project A",),
        scope_window_start=since,
        scope_window_end=until,
        limit=1,
    )
    store.list_resume_memory_events(
        statuses=("active", "candidate"),
        projects=("Project A",),
        query=r"Release%_\Marker",
        occurred_at_start=since,
        occurred_at_end=until,
        limit=2,
    )
    store.list_open_loop_events(
        statuses=("open", "waiting"),
        scope_projects=("Project A",),
        query=r"Release%_\Marker",
        occurred_at_start=since,
        occurred_at_end=until,
        limit=2,
    )
    # Established context-tree consumers retain the original method contract.
    store.list_memory_events(
        scope_projects=("Project A",),
        scope_window_start=since,
        scope_window_end=until,
        limit=2,
    )

    memory_query, loop_query, memory_event_query, loop_event_query, shared_event_query = (
        query for query, _params in cursor.executed
    )
    memory_params = cursor.executed[0][1]
    loop_params = cursor.executed[1][1]
    memory_event_params = cursor.executed[2][1]
    loop_event_params = cursor.executed[3][1]
    assert "status = ANY(%s::text[])" in memory_query
    assert "memory_type = ANY(%s::text[])" in memory_query
    assert "created_at >= %s::timestamptz" in memory_query
    assert "translate(COALESCE(title, ''), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')" in memory_query
    assert memory_query.count('COLLATE "C"') >= 6
    assert memory_query.count("ESCAPE E'\\\\'") == 3
    assert "strpos(lower(" not in memory_query
    assert memory_params is not None
    assert memory_params.count(r"Release\%\_\\Marker") == 3
    assert memory_query.index("status = ANY") < memory_query.index("LIMIT %s")
    assert memory_query.index("created_at >=") < memory_query.index("LIMIT %s")
    assert "status = ANY(%s::text[])" in loop_query
    assert "translate(COALESCE(title, ''), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')" in loop_query
    assert "metadata_json ->> 'next_action'" in loop_query
    for guard, match in (
        (
            "jsonb_typeof(metadata_json -> 'next_action') = 'string'",
            "COALESCE(metadata_json ->> 'next_action', '')",
        ),
        (
            "jsonb_typeof(metadata_json #> '{agentic_memory,next_action}') = 'string'",
            "COALESCE(metadata_json #>> '{agentic_memory,next_action}', '')",
        ),
    ):
        assert guard in loop_query
        assert loop_query.index(guard) < loop_query.index(match)
    assert "metadata_json::text" not in loop_query
    assert "CAST(metadata_json AS text)" not in loop_query
    assert loop_query.count('COLLATE "C"') >= 8
    assert loop_query.count("ESCAPE E'\\\\'") == 4
    assert "strpos(lower(" not in loop_query
    assert loop_params is not None
    assert loop_params.count(r"Release\%\_\\Marker") == 4
    assert loop_query.index("status = ANY") < loop_query.index("LIMIT %s")
    assert loop_query.index("opened_at, updated_at, created_at") < loop_query.index("LIMIT %s")
    for event_query in (memory_event_query, loop_event_query):
        assert "JOIN" in event_query
        assert "event.occurred_at >= %s::timestamptz" in event_query
        assert event_query.index("event.occurred_at >=") < event_query.index("LIMIT %s")
        assert event_query.index("ORDER BY event.occurred_at DESC") < event_query.index("LIMIT %s")
    assert (
        "translate(COALESCE(m.title, ''), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
        in memory_event_query
    )
    assert memory_event_query.count('COLLATE "C"') >= 6
    assert memory_event_query.count("ESCAPE E'\\\\'") == 3
    assert "strpos(lower(" not in memory_event_query
    assert memory_event_params is not None
    assert memory_event_params.count(r"Release\%\_\\Marker") == 4
    assert "loop.metadata_json ->> 'next_action'" in loop_event_query
    for guard, match in (
        (
            "jsonb_typeof(loop.metadata_json -> 'next_action') = 'string'",
            "COALESCE(loop.metadata_json ->> 'next_action', '')",
        ),
        (
            "jsonb_typeof(loop.metadata_json #> '{agentic_memory,next_action}') = 'string'",
            "COALESCE(loop.metadata_json #>> '{agentic_memory,next_action}', '')",
        ),
    ):
        assert guard in loop_event_query
        assert loop_event_query.index(guard) < loop_event_query.index(match)
    assert "loop.metadata_json::text" not in loop_event_query
    assert "CAST(loop.metadata_json AS text)" not in loop_event_query
    assert "jsonb_path_query(" in loop_event_query
    assert '@.type() == "string"' in loop_event_query
    assert "payload_leaf.value #>> '{}'" in loop_event_query
    assert loop_event_query.count('COLLATE "C"') >= 10
    assert loop_event_query.count("ESCAPE E'\\\\'") == 5
    assert "strpos(lower(" not in loop_event_query
    assert loop_event_params is not None
    assert loop_event_params.count(r"Release\%\_\\Marker") == 5
    assert "event.payload_json ->> 'text'" not in loop_event_query
    assert "event.payload_json::text" not in loop_event_query
    assert "event_type_prefix" not in shared_event_query
    assert "JOIN memories m" in shared_event_query


def test_project_update_event_lookup_is_one_bounded_target_and_payload_query() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.list_project_update_events(
        artifact_id="artifact-1",
        candidate_memory_id="memory-1",
    )

    assert rows == []
    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert query.count("SELECT") == 5
    assert query.count("user_id = app.current_user_id()") == 5
    assert query.count("event_type IN (") == 5
    assert query.count("'project.update_candidate_created'") == 5
    assert query.count("'project.update_candidate_accepted'") == 5
    assert query.count("'project.update_candidate_rejected'") == 5
    assert query.count("\nUNION\n") == 4
    assert " UNION ALL" not in query
    assert " OR " not in query
    assert "target_type = 'artifact' AND target_id = %s" in query
    assert "target_type = 'memory' AND target_id = %s" in query
    assert "payload_json @>" not in query
    assert "payload_artifact_id = %s" in query
    assert "payload_candidate_memory_id = %s" in query
    assert "payload_memory_id = %s" in query
    assert "ORDER BY occurred_at DESC, id DESC" in query
    assert params == (
        "artifact-1",
        "memory-1",
        "artifact-1",
        "memory-1",
        "memory-1",
    )


def test_memory_and_rollup_counts_are_exact_scoped_database_reads() -> None:
    cursor = RecordingCursor(fetchone_results=[{"count": 7}, {"count": 5}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert (
        store.count_memories(
            status="active",
            domains=["project"],
            sensitivity_allowed=["private"],
        )
        == 7
    )
    assert (
        store.count_rollup_input_memories(
            domains=["project"],
            sensitivity_allowed=["private"],
            excluded_candidate_kind="memory_rollup",
        )
        == 5
    )

    memory_query, memory_params = cursor.executed[0]
    assert "SELECT COUNT(*) AS count" in memory_query
    assert "status = %s" in memory_query
    assert memory_params == ("active", ["project"], ["private"])
    rollup_query, rollup_params = cursor.executed[1]
    assert "SELECT COUNT(*) AS count" in rollup_query
    assert "status IN ('active', 'accepted')" in rollup_query
    assert "candidate_kind" in rollup_query
    assert rollup_params == (
        "memory_rollup",
        ["project"],
        ["project"],
        ["private"],
        None,
        None,
    )


def test_rollup_reads_push_status_scope_exact_keys_order_and_limits_into_postgres() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.list_rollup_input_memories(
        domains=["project"],
        sensitivity_allowed=["private"],
        excluded_candidate_kind="memory_rollup",
        limit=501,
    )
    store.list_pending_rollup_candidates(
        rollup_digests=("digest-b", "digest-a", "digest-a"),
        domains=["project"],
        sensitivity_allowed=["private"],
        candidate_kind="memory_rollup",
        limit=99,
    )
    store.list_accepted_rollup_cards(
        rollup_keys=("topic:zeta", "topic:alpha", "topic:alpha"),
        domains=["project"],
        sensitivity_allowed=["private"],
        candidate_kind="memory_rollup",
        limit=99,
    )

    input_query, input_params = cursor.executed[0]
    assert "status IN ('active', 'accepted')" in input_query
    assert "COALESCE(metadata_json ->> 'candidate_kind', '') <> %s" in input_query
    assert "domain = ANY(%s::text[]) OR domain = 'unknown'" in input_query
    assert "COALESCE(sensitivity, 'unknown') = ANY(%s::text[])" in input_query
    assert "ORDER BY created_at DESC, id DESC" in input_query
    assert "LIMIT %s" in input_query
    assert input_params == (
        "memory_rollup",
        ["project"],
        ["project"],
        ["private"],
        None,
        None,
        501,
    )

    pending_query, pending_params = cursor.executed[1]
    assert "DISTINCT ON (metadata_json ->> 'rollup_digest')" in pending_query
    assert "status = 'candidate'" in pending_query
    assert "metadata_json ->> 'rollup_digest' = ANY(%s::text[])" in pending_query
    assert "ORDER BY metadata_json ->> 'rollup_digest', updated_at DESC" in pending_query
    assert "LIMIT %s" in pending_query
    assert pending_params == (
        "memory_rollup",
        ["digest-a", "digest-b"],
        ["project"],
        ["project"],
        ["private"],
        None,
        None,
        2,
    )

    accepted_query, accepted_params = cursor.executed[2]
    assert "DISTINCT ON (metadata_json ->> 'rollup_key')" in accepted_query
    assert "status IN ('active', 'accepted')" in accepted_query
    assert "metadata_json ->> 'rollup_key' = ANY(%s::text[])" in accepted_query
    assert "CASE WHEN status = 'active' THEN 0 ELSE 1 END" in accepted_query
    assert "LIMIT %s" in accepted_query
    assert accepted_params == (
        "memory_rollup",
        ["topic:alpha", "topic:zeta"],
        ["project"],
        ["project"],
        ["private"],
        None,
        None,
        2,
    )

    store.list_rollup_input_memories(
        domains=[],
        sensitivity_allowed=["internal"],
        excluded_candidate_kind="memory_rollup",
        limit=1,
    )
    assert cursor.executed[3][1] == (
        "memory_rollup",
        None,
        None,
        ["internal"],
        None,
        None,
        1,
    )

    with pytest.raises(ValueError, match="limit must be positive"):
        store.list_rollup_input_memories(
            domains=None,
            sensitivity_allowed=["internal"],
            excluded_candidate_kind="memory_rollup",
            limit=0,
        )


def test_project_people_belief_and_open_loop_methods_write_audit_events() -> None:
    project_id = str(uuid4())
    person_id = str(uuid4())
    memory_id = str(uuid4())
    belief_id = str(uuid4())
    loop_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": project_id},
            _event_row(project_id),
            {"id": project_id},
            {"id": project_id},
            _event_row(project_id),
            {"id": person_id},
            _event_row(person_id),
            {"id": person_id},
            {"id": person_id},
            _event_row(person_id),
            {"id": belief_id, "memory_id": memory_id},
            _event_row(belief_id),
            {"id": belief_id, "memory_id": memory_id},
            {"id": belief_id, "memory_id": memory_id},
            _event_row(belief_id),
            {"id": loop_id},
            _event_row(loop_id),
            {"id": loop_id},
            {"id": loop_id},
            _event_row(loop_id),
            {"id": loop_id},
            _event_row(loop_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_project({"id": project_id, "name": "Alice vNext", "slug": "alice-vnext"})
    store.get_project(project_id)
    store.list_projects(status="active", domains=["project"], sensitivity_allowed=["private"], limit=3)
    store.update_project(project_id=project_id, patch={"current_state": "Sprint 1"})
    store.create_person({"id": person_id, "name": "Samir", "aliases_json": ["owner"]})
    store.get_person(person_id)
    store.update_person(person_id=person_id, patch={"notes": "Project owner"})
    store.create_belief({"id": belief_id, "memory_id": memory_id, "claim": "Provenance is mandatory."})
    store.get_belief(belief_id)
    store.update_belief_status(belief_id=belief_id, status="challenged", confidence=0.4)
    store.create_open_loop({"id": loop_id, "title": "Validate migration on Postgres", "priority": "high"})
    store.get_open_loop(loop_id)
    store.update_open_loop_status(loop_id=loop_id, status="resolved", resolution_note="Covered by CI")
    store.update_open_loop(loop_id=loop_id, patch={"title": "Validate migration", "priority": "normal"})

    assert _event_log_insert_count(cursor) == 9
    assert "INSERT INTO projects" in cursor.executed[0][0]
    assert "FROM projects" in cursor.executed[3][0]
    assert "%s::text IS NULL OR status = %s" in cursor.executed[3][0]
    assert cursor.executed[3][1] == (
        "active",
        "active",
        ["project"],
        ["project"],
        ["private"],
        ["private"],
        None,
        None,
        None,
        None,
        3,
    )
    assert "UPDATE projects" in cursor.executed[4][0]
    assert "INSERT INTO people" in cursor.executed[6][0]
    assert "UPDATE people" in cursor.executed[9][0]
    assert "INSERT INTO beliefs" in cursor.executed[11][0]
    assert "UPDATE beliefs" in cursor.executed[14][0]
    assert "INSERT INTO open_loops" in cursor.executed[16][0]
    assert "UPDATE open_loops" in cursor.executed[19][0]
    assert "UPDATE open_loops" in cursor.executed[21][0]


def test_get_artifact_for_update_locks_the_persisted_authorization_target() -> None:
    artifact_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[{"id": artifact_id, "artifact_type": "daily_brief"}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    artifact = store.get_artifact_for_update(artifact_id)

    assert artifact == {"id": artifact_id, "artifact_type": "daily_brief"}
    query, params = cursor.executed[0]
    assert "FROM generated_artifacts" in query
    assert "FOR UPDATE" in query
    assert params == (artifact_id,)


def test_exact_open_loop_and_artifact_digest_lookups_scope_before_limit() -> None:
    project_id = str(uuid4())
    person_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[{"id": "loop-1"}, {"id": "artifact-1"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.find_open_loop_by_automation_digest(
        digest="loop-digest",
        project_id=project_id,
        person_id=person_id,
    ) == {"id": "loop-1"}
    assert store.find_artifact_by_workflow_digest(
        artifact_type="project_update",
        workflow="project_auto_update",
        digest="artifact-digest",
        scope_projects=(project_id,),
    ) == {"id": "artifact-1"}

    loop_query, loop_params = cursor.executed[0]
    assert loop_query.index("automation_digest") < loop_query.index("LIMIT 1")
    assert "project_id = %s::uuid" in loop_query
    assert "person_id = %s::uuid" in loop_query
    assert loop_params == (
        "loop-digest",
        project_id,
        project_id,
        person_id,
        person_id,
    )
    artifact_query, artifact_params = cursor.executed[1]
    assert artifact_query.index("automation_digest") < artifact_query.index("LIMIT 1")
    assert "consolidation_digest" in artifact_query
    assert artifact_params == (
        "project_update",
        "project_auto_update",
        "artifact-digest",
        [project_id],
        [project_id],
    )


def test_source_trace_and_policy_telemetry_queries_filter_before_limit() -> None:
    source_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.list_memories_referencing_source(source_id=source_id, limit=11)
    store.list_artifacts_referencing_source(source_id=source_id, limit=12)
    store.list_open_loops_referencing_source(source_id=source_id, limit=13)
    store.list_events_for_source_trace(
        source_id=source_id,
        memory_ids=(str(uuid4()),),
        artifact_ids=(str(uuid4()),),
        open_loop_ids=(str(uuid4()),),
        limit=14,
    )
    store.list_agent_policy_artifacts(agent_id="hermes", limit=15)
    store.list_agent_policy_memories(agent_id="hermes", limit=16)

    for query, _params in cursor.executed[:4]:
        assert query.index("source_id") < query.index("LIMIT %s")
    assert "provenance_links" in cursor.executed[0][0]
    assert "provenance_links" in cursor.executed[1][0]
    assert "target_type = 'source'" in cursor.executed[3][0]
    assert "generated_by' = 'agent'" in cursor.executed[4][0]
    assert "agent_id' IS NOT NULL" in cursor.executed[5][0]


def test_artifact_task_and_brain_charter_methods_write_audit_events() -> None:
    artifact_id = str(uuid4())
    task_id = str(uuid4())
    charter_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": artifact_id, "artifact_type": "context_pack"},
            _event_row(artifact_id),
            {"id": artifact_id, "artifact_type": "context_pack"},
            {"id": artifact_id, "artifact_type": "context_pack"},
            _event_row(artifact_id),
            {"id": task_id, "task_type": "synthesize"},
            _event_row(task_id),
            {"id": task_id, "task_type": "synthesize"},
            _event_row(task_id),
            {"id": task_id, "task_type": "synthesize"},
            _event_row(task_id),
            {"id": charter_id},
            _event_row(charter_id),
            {"id": charter_id},
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_artifact(
        {
            "id": artifact_id,
            "artifact_type": "context_pack",
            "title": "Sprint 1 Context",
            "content_markdown": "# Context",
            "generated_by": "system",
        }
    )
    store.get_artifact(artifact_id)
    store.update_artifact_status(artifact_id=artifact_id, status="reviewed")
    store.create_task(
        {
            "id": task_id,
            "title": "Synthesize evidence",
            "task_type": "synthesize",
            "instructions": "Create a context pack.",
            "scope_json": {"project": "alice-vnext"},
        }
    )
    claimed = store.claim_next_task()
    store.update_task_status(task_id=task_id, status="completed", details={"metadata_json": {"ok": True}})
    store.upsert_brain_charter(
        {
            "id": charter_id,
            "content_markdown": "# ALICE.md - Brain Charter",
            "owner_json": {"name": "Owner"},
            "autonomous_rules_json": ["Always preserve provenance."],
            "quality_standard_json": ["Do not fabricate."],
        }
    )
    store.get_brain_charter()

    assert claimed is not None
    assert _event_log_insert_count(cursor) == 6
    assert "INSERT INTO generated_artifacts" in cursor.executed[0][0]
    assert "UPDATE generated_artifacts" in cursor.executed[3][0]
    assert "INSERT INTO task_queue" in cursor.executed[5][0]
    assert "FOR UPDATE SKIP LOCKED" in cursor.executed[7][0]
    assert "UPDATE task_queue" in cursor.executed[9][0]
    assert "ON CONFLICT (user_id)" in cursor.executed[11][0]


def test_append_and_list_event_log_records_use_integrity_payload() -> None:
    event = build_event_log_record(
        event_type="memory.created",
        actor_type="system",
        target_type="memory",
        target_id="memory-1",
        payload={"b": 2, "a": 1},
        occurred_at="2026-05-10T12:00:00Z",
    )
    cursor = RecordingCursor(fetchone_results=[_event_row("memory-1")], fetchall_result=[_event_row("memory-1")])
    store = PostgresVNextStore(RecordingConnection(cursor))

    appended = store.append_event(event)
    events = store.list_events(target_type="memory", target_id="memory-1")
    all_events = store.list_events()

    assert appended["target_id"] == "memory-1"
    assert events[0]["target_id"] == "memory-1"
    assert all_events[0]["target_id"] == "memory-1"
    event_insert_query, event_insert_params = cursor.executed[0]
    assert "INSERT INTO event_log" in event_insert_query
    assert event_insert_params is not None
    assert event_insert_params[1:6] == (
        "memory.created",
        "system",
        None,
        "memory",
        "memory-1",
    )
    assert isinstance(event_insert_params[7], Jsonb)
    assert event_insert_params[7].obj == {"b": 2, "a": 1}
    assert event_insert_params[10] == event["integrity_hash"]
    event_list_query = cursor.executed[1][0]
    assert "%s::text IS NULL OR target_type = %s" in event_list_query
    assert "%s::text IS NULL OR target_id = %s" in event_list_query


def test_connector_settings_and_state_methods_use_dedicated_tables_and_audit_events() -> None:
    setting_id = str(uuid4())
    state_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": setting_id,
                "connector_name": "browser_clipper",
                "enabled": True,
                "configured": True,
                "default_domain": "personal",
                "default_sensitivity": "private",
                "sync_mode": "on_demand",
                "poll_interval_seconds": None,
                "secret_ref": "browser.capture_token.default",
                "validation_errors_json": [],
            },
            _event_row("browser_clipper"),
            {"id": setting_id, "connector_name": "browser_clipper"},
            {
                "id": state_id,
                "connector_id": setting_id,
                "connector_name": "browser_clipper",
                "cursor_type": "sync_cursor",
                "cursor_value": "42",
                "last_sync_at": "2026-05-11T12:00:00Z",
                "last_success_at": "2026-05-11T12:00:00Z",
                "last_failure_at": None,
                "items_seen": 3,
                "items_captured": 1,
                "items_deduped": 1,
                "items_failed": 1,
            },
            _event_row("browser_clipper"),
            {"id": state_id, "connector_name": "browser_clipper", "cursor_value": "42"},
            {"connector_settings_exists": True, "connector_state_exists": True, "migration_revision": "20260511_0070"},
        ],
        fetchall_result=[{"id": setting_id, "connector_name": "browser_clipper"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    setting = store.upsert_connector_setting(
        {
            "connector_name": "browser_clipper",
            "enabled": True,
            "configured": True,
            "default_domain": "personal",
            "default_sensitivity": "private",
            "sync_mode": "on_demand",
            "poll_interval_seconds": None,
            "secret_ref": "browser.capture_token.default",
            "validation_errors_json": [],
            "metadata_json": {"config_json": {"allowed_origins": ["http://localhost:3000"]}},
        }
    )
    settings = store.list_connector_settings()
    fetched_setting = store.get_connector_setting("browser_clipper")
    state = store.upsert_connector_state(
        {
            "connector_name": "browser_clipper",
            "cursor_value": "42",
            "last_sync_at": "2026-05-11T12:00:00Z",
            "last_success_at": "2026-05-11T12:00:00Z",
            "items_seen_delta": 3,
            "items_captured_delta": 1,
            "items_deduped_delta": 1,
            "items_failed_delta": 1,
            "average_processing_time_ms": 12.5,
            "state_json": {"last_status": "partial"},
        }
    )
    fetched_state = store.get_connector_state("browser_clipper")
    storage_status = store.connector_storage_status()

    assert setting["id"] == setting_id
    assert settings == [{"id": setting_id, "connector_name": "browser_clipper"}]
    assert fetched_setting is not None
    assert state["cursor_value"] == "42"
    assert fetched_state is not None
    assert storage_status["connector_settings_exists"] is True
    assert _event_log_insert_count(cursor) == 2
    setting_query, setting_params = cursor.executed[0]
    assert "INSERT INTO connector_settings" in setting_query
    assert "ON CONFLICT (user_id, connector_name)" in setting_query
    assert setting_params is not None
    assert isinstance(setting_params[8], Jsonb)
    assert setting_params[8].obj == []
    assert isinstance(setting_params[9], Jsonb)
    assert setting_params[9].obj == {"config_json": {"allowed_origins": ["http://localhost:3000"]}}
    state_query, state_params = cursor.executed[4]
    assert "INSERT INTO connector_state" in state_query
    assert "items_seen = connector_state.items_seen + EXCLUDED.items_seen" in state_query
    assert state_params is not None
    assert isinstance(state_params[-1], Jsonb)
    assert state_params[-1].obj == {"last_status": "partial"}


def test_workspace_list_methods_apply_bounded_filters() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[
            {"id": "workspace-row-1", "status": "active", "sensitivity": "private"},
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    sources = store.list_sources(domains=["project"], sensitivity_allowed=["private"], limit=7)
    people = store.list_people(sensitivity_allowed=["private"], limit=5)
    tasks = store.list_tasks(status=None, limit=4)
    events = store.list_events(limit=3)

    assert sources[0]["id"] == "workspace-row-1"
    assert people[0]["id"] == "workspace-row-1"
    assert tasks[0]["id"] == "workspace-row-1"
    assert events[0]["id"] == "workspace-row-1"

    source_query, source_params = cursor.executed[0]
    people_query, people_params = cursor.executed[1]
    task_query, task_params = cursor.executed[2]
    event_query, event_params = cursor.executed[3]
    assert "FROM sources" in source_query
    assert "deleted_at IS NULL" in source_query
    assert source_params == (["project"], ["project"], ["private"], ["private"], 7)
    assert "FROM people" in people_query
    assert people_params == (["private"], ["private"], 5)
    assert "FROM task_queue" in task_query
    assert task_params == (None, None, 4)
    assert "FROM event_log" in event_query
    assert "LIMIT %s" in event_query
    assert event_params == (3,)


def test_jsonb_and_event_hash_normalize_postgres_scalar_values() -> None:
    project_id = uuid4()
    captured_at = datetime(2026, 5, 10, 12, 30, tzinfo=UTC)
    event = build_event_log_record(
        event_type="project.update_candidate_created",
        actor_type="system",
        target_type="project",
        target_id=str(project_id),
        payload={
            "project_id": project_id,
            "source": {
                "captured_at": captured_at,
            },
        },
    )
    cursor = RecordingCursor(fetchone_results=[{"id": str(project_id)}, _event_row(project_id)])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_project(
        {
            "id": str(project_id),
            "name": "Alice vNext",
            "slug": "alice-vnext",
            "metadata_json": {
                "candidate_memory_id": project_id,
                "source_captured_at": captured_at,
            },
        }
    )

    assert event["payload_json"] == {
        "project_id": str(project_id),
        "source": {
            "captured_at": "2026-05-10T12:30:00+00:00",
        },
    }
    project_insert_params = cursor.executed[0][1]
    assert project_insert_params is not None
    assert isinstance(project_insert_params[-1], Jsonb)
    assert project_insert_params[-1].obj == {
        "candidate_memory_id": str(project_id),
        "source_captured_at": "2026-05-10T12:30:00+00:00",
    }


def test_fts_search_builds_websearch_tsquery_with_pushed_down_filters() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "memory-1", "fts_score": 0.42}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.search_memories_fts(
        query="Alice provenance retrieval",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=25,
    )

    assert rows[0]["id"] == "memory-1"
    query, params = cursor.executed[0]
    assert "FROM memories" in query
    assert "websearch_to_tsquery('english', %s)" in query
    assert "search_tsv @@ websearch_to_tsquery('english', %s)" in query
    assert "ts_rank(search_tsv, websearch_to_tsquery('english', %s)) AS fts_score" in query
    assert "status IN ('active', 'accepted')" in query
    assert "deleted_at IS NULL" in query
    assert "domain = ANY" in query
    assert "sensitivity = ANY" in query
    assert "memory_type = ANY" in query
    assert "metadata_json -> 'project_scope'" in query
    assert "?| %s::text[]" in query
    assert "created_by_agent_id = ANY" in query
    assert "run_id = %s" in query
    assert "valid_to IS NULL OR valid_to >= clock_timestamp()" in query
    assert "ORDER BY fts_score DESC" in query
    assert params == (
        "Alice provenance retrieval",
        ["project"],
        ["project"],
        ["public", "private"],
        ["public", "private"],
        None,  # memory_types unset
        None,
        None,  # projects unset
        None,
        None,  # created_by_agent_ids unset
        None,
        None,  # run_id unset
        None,
        False,  # include_expired defaults to excluded
        None,  # scope_thread_id unset
        None,
        None,  # scope_task_id unset
        None,
        None,  # scope_people unset
        None,  # scope_person_memory_ids unset
        None,  # direct people predicate unset
        None,  # scope_window_start unset
        None,
        None,  # scope_window_end unset
        None,
        "Alice provenance retrieval",
        25,
    )


def test_fts_search_pushes_down_memory_type_project_agent_run_and_expiry_filters() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "memory-1", "fts_score": 0.42}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.search_memories_fts(
        query="Alice provenance retrieval",
        domains=["project"],
        sensitivity_allowed=["private"],
        limit=25,
        memory_types=("decision", "procedure"),
        projects=("alicebot",),
        created_by_agent_ids=("openclaw", "hermes"),
        run_id="run-2026-07-04-001",
        include_expired=True,
    )

    _query, params = cursor.executed[0]
    assert params == (
        "Alice provenance retrieval",
        ["project"],
        ["project"],
        ["private"],
        ["private"],
        ["decision", "procedure"],
        ["decision", "procedure"],
        ["alicebot"],
        ["alicebot"],
        ["openclaw", "hermes"],
        ["openclaw", "hermes"],
        "run-2026-07-04-001",
        "run-2026-07-04-001",
        True,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "Alice provenance retrieval",
        25,
    )


def test_fts_search_pushes_people_and_time_scope_before_ranked_limit() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))
    linked_memory_id = str(uuid4())
    window_start = datetime(2026, 7, 3, tzinfo=UTC)
    window_end = datetime(2026, 7, 10, tzinfo=UTC)

    store.search_memories_fts(
        query="deployment",
        scope_people=("sam",),
        scope_person_memory_ids=(linked_memory_id,),
        scope_window_start=window_start,
        scope_window_end=window_end,
        limit=1,
    )

    query, params = cursor.executed[0]
    assert "jsonb_path_query" in query
    assert "id::text = ANY" in query
    assert "COALESCE(valid_from, last_seen_at, updated_at, first_seen_at, created_at)" in query
    assert params[-9:] == (
        ["sam"],
        [linked_memory_id],
        ["sam"],
        window_start,
        window_start,
        window_end,
        window_end,
        "deployment",
        1,
    )


def test_search_source_chunks_builds_websearch_tsquery_over_chunk_text() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "chunk-1", "source_id": "source-1", "fts_score": 0.42}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.search_source_chunks(
        query="golden retriever Biscuit",
        domains=["project"],
        sensitivity_allowed=["public", "private"],
        limit=32,
    )

    assert rows[0]["source_id"] == "source-1"
    query, params = cursor.executed[0]
    assert "FROM source_chunks c" in query
    assert "JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id" in query
    assert "s.deleted_at IS NULL" in query
    assert "s.domain = ANY" in query
    assert "s.sensitivity = ANY" in query
    assert "c.search_tsv @@ websearch_to_tsquery('english', %s)" in query
    assert "ts_rank(c.search_tsv, websearch_to_tsquery('english', %s)) AS fts_score" in query
    assert "ORDER BY fts_score DESC" in query
    assert params == (
        "golden retriever Biscuit",
        ["project"],
        ["project"],
        ["public", "private"],
        ["public", "private"],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "golden retriever Biscuit",
        32,
    )


def test_search_source_chunks_match_any_ors_sanitized_lexemes() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "chunk-1", "source_id": "source-1", "fts_score": 0.11}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.search_source_chunks(
        query="When does the announcement & !audit go out?",
        match_any=True,
    )

    query, params = cursor.executed[0]
    assert "c.search_tsv @@ to_tsquery('english', %s)" in query
    # Stopwords and tsquery metacharacters are stripped; each surviving
    # token is individually quoted so nothing can inject query syntax.
    assert params is not None
    assert params[0] == "'announcement' | 'audit' | 'go'"


def test_search_source_chunks_match_any_returns_empty_without_content_tokens() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    # Stopword/metacharacter-only queries sanitize to no lexemes: no SQL runs.
    assert store.search_source_chunks(query="&|!():*<->", match_any=True) == []
    assert store.search_source_chunks(query="when was the", match_any=True) == []
    assert cursor.executed == []


def test_vector_search_orders_by_cosine_distance_and_skips_null_embeddings() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "memory-1", "vector_distance": 0.12}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.search_memories_vector(
        query_vector=[0.25, -1.0],
        domains=["project"],
        sensitivity_allowed=["private"],
        limit=12,
    )

    assert rows[0]["id"] == "memory-1"
    query, params = next((q, p) for q, p in cursor.executed if "vector_distance" in q)
    assert "FROM memories" in query
    assert "embedding_vector IS NOT NULL" in query
    assert "status IN ('active', 'accepted')" in query
    assert "memory_type = ANY" in query
    assert "metadata_json -> 'project_scope'" in query
    assert "?| %s::text[]" in query
    assert "created_by_agent_id = ANY" in query
    assert "run_id = %s" in query
    assert "valid_to IS NULL OR valid_to >= clock_timestamp()" in query
    assert "ORDER BY embedding_vector <=> %s::vector" in query
    assert "(embedding_vector <=> %s::vector) AS vector_distance" in query
    assert params == (
        "[0.25,-1.0]",
        ["project"],
        ["project"],
        ["private"],
        ["private"],
        None,  # memory_types unset
        None,
        None,  # projects unset
        None,
        None,  # created_by_agent_ids unset
        None,
        None,  # run_id unset
        None,
        None,  # scope_thread_id unset
        None,
        None,  # scope_task_id unset
        None,
        None,  # scope_people unset
        None,  # scope_person_memory_ids unset
        None,  # direct people predicate unset
        None,  # scope_window_start unset
        None,
        None,  # scope_window_end unset
        None,
        False,  # include_expired defaults to excluded
        "[0.25,-1.0]",
        12,
    )


def test_postgres_vector_boundary_rejects_non_finite_values() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ContinuityStoreInvariantError, match="finite numbers"):
        store.search_memories_vector(query_vector=[1.0, float("nan")])
    with pytest.raises(ContinuityStoreInvariantError, match="finite numbers"):
        store.update_memory_embedding(
            memory_id=str(uuid4()),
            vector=[1.0, float("inf")],
        )

    assert cursor.executed == []


def test_vector_search_can_require_matching_embedding_signature() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="openai_compatible",
        embedding_model="embed-v1",
        embedding_signature_version=1,
    )

    query, params = next((q, p) for q, p in cursor.executed if "vector_distance" in q)
    assert "metadata_json -> '_alice_embedding' ->> 'provider' = %s" in query
    assert "metadata_json -> '_alice_embedding' ->> 'model' = %s" in query
    assert "->> 'version' = %s" in query
    assert params[-6:-3] == ("openai_compatible", "embed-v1", "1")


def test_vector_search_enables_iterative_hnsw_scan() -> None:
    # Filters applied alongside an approximate HNSW ORDER BY can silently
    # underfill; the store must enable iterative index scan so a filtered
    # vector search still returns up to LIMIT valid rows.
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.search_memories_vector(query_vector=[1.0, 0.0], limit=20)

    statements = [q for q, _ in cursor.executed]
    assert any("hnsw.iterative_scan" in q and "strict_order" in q for q in statements), statements
    # The iterative-scan setting must precede the vector SELECT.
    set_index = next(i for i, q in enumerate(statements) if "hnsw.iterative_scan" in q)
    select_index = next(i for i, q in enumerate(statements) if "vector_distance" in q)
    assert set_index < select_index


def test_vector_search_discards_stale_content_signatures_after_database_read() -> None:
    current = {
        "id": "memory-current",
        "canonical_text": "Current vector content",
        "metadata_json": {},
        "vector_distance": 0.2,
    }
    current["metadata_json"] = {
        "_alice_embedding": {
            "version": 2,
            "provider": "openai_compatible",
            "model": "embed-v1",
            "endpoint": "host-a",
            "content_sha256": memory_embedding_content_sha256(current),
        }
    }
    stale = {
        "id": "memory-stale",
        "canonical_text": "Text changed after this vector was made",
        "metadata_json": {"_alice_embedding": {"content_sha256": "0" * 64}},
        "vector_distance": 0.1,
    }
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[stale, current])
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.search_memories_vector(
        query_vector=[1.0, 0.0],
        limit=1,
        embedding_provider="openai_compatible",
        embedding_model="embed-v1",
        embedding_endpoint="host-a",
        embedding_signature_version=2,
    )

    assert [row["id"] for row in rows] == ["memory-current"]
    _query, select_params = next((q, p) for q, p in cursor.executed if "vector_distance" in q)
    vector_query = next(q for q, _p in cursor.executed if "vector_distance" in q)
    assert "content_sha256" in vector_query
    assert "digest(" in vector_query
    assert select_params[-1] == 4


def test_scheduler_lock_key_includes_current_rls_user() -> None:
    cursor = RecordingCursor(fetchone_results=[{"acquired": True}])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.try_scheduler_workflow_lock("daily_brief") is True

    query, params = cursor.executed[0]
    assert "app.current_user_id()::text" in query
    assert "concat_ws" in query
    assert params == ("daily_brief",)


@pytest.mark.parametrize(
    ("patch", "preserves_claim"),
    (
        (
            {
                "last_run_id": str(uuid4()),
                "last_run_at": "2026-07-13T09:00:00Z",
                "last_result": "succeeded",
                "last_error": None,
                "next_run_at": "2026-07-14T09:00:00Z",
            },
            True,
        ),
        ({"enabled": False, "next_run_at": None}, False),
    ),
)
def test_scheduler_workflow_updates_only_preserve_claim_for_run_bookkeeping(
    patch: dict[str, object], preserves_claim: bool
) -> None:
    workflow_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": workflow_id, "workflow_type": "daily_brief"},
            _event_row(workflow_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.update_scheduler_workflow(
        workflow_type="daily_brief",
        patch=patch,
        actor_type="test",
    )

    query, params = cursor.executed[0]
    assert "claim_token = CASE WHEN %s THEN claim_token ELSE NULL END" in query
    assert "claim_version = claim_version + CASE WHEN %s THEN 0 ELSE 1 END" in query
    assert params is not None
    assert params[-4:] == (
        preserves_claim,
        preserves_claim,
        preserves_claim,
        "daily_brief",
    )


def test_artifact_status_update_uses_expected_status_compare_and_set() -> None:
    artifact_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": artifact_id, "status": "accepted"},
            _event_row(artifact_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.update_artifact_status(
        artifact_id=artifact_id,
        status="accepted",
        expected_status="needs_review",
        metadata_json={"review_status": "accepted"},
    )

    assert row is not None
    query, params = cursor.executed[0]
    assert "AND (%s::text IS NULL OR status = %s)" in query
    assert "metadata_json || %s::jsonb" in query
    assert params is not None
    assert params[-3:] == (artifact_id, "needs_review", "needs_review")


def test_scheduler_claim_rechecks_due_state_and_persists_fence() -> None:
    workflow_id = str(uuid4())
    run_id = str(uuid4())
    scheduled_for = datetime(2026, 7, 13, 8, tzinfo=UTC)
    checked_at = datetime(2026, 7, 13, 8, 1, tzinfo=UTC)
    lease_expires_at = datetime(2026, 7, 13, 8, 6, tzinfo=UTC)
    workflow = {
        "id": workflow_id,
        "workflow_type": "daily_brief",
        "next_run_at": scheduled_for,
        "claim_version": 0,
    }
    claimed_workflow = {**workflow, "claim_version": 1, "claim_token": "server-token"}
    cursor = RecordingCursor(
        fetchone_results=[
            workflow,
            {"acquired": True},
            workflow,
            claimed_workflow,
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "workflow_type": "daily_brief",
                "status": "started",
                "trace_id": "trace-1",
                "triggered_by": "scheduler",
            },
            _event_row(run_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    claim = store.claim_due_scheduler_workflow(
        checked_at=checked_at,
        lease_expires_at=lease_expires_at,
        triggered_by="scheduler",
    )

    assert claim is not None
    assert claim["claim_version"] == 1
    assert claim["scheduled_for"] == scheduled_for
    statements = [query for query, _params in cursor.executed]
    assert "FOR UPDATE SKIP LOCKED" in statements[0]
    assert "enabled = true" in statements[2]
    assert "claim_version = claim_version + 1" in statements[3]
    assert "INSERT INTO scheduler_runs" in statements[4]
    assert "claim_token" in statements[4]
    assert "scheduled_for" in statements[4]


def test_scheduler_heartbeat_finalize_and_reaper_are_fenced() -> None:
    run_id = str(uuid4())
    workflow_id = str(uuid4())
    now = datetime(2026, 7, 13, 9, tzinfo=UTC)
    cursor = RecordingCursor(
        fetchone_results=[
            {"renewed": True},
            {"run_id": run_id},
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "workflow_type": "daily_brief",
                "status": "succeeded",
                "trace_id": "trace-1",
            },
            _event_row(run_id),
            _event_row(run_id),
        ],
        fetchall_result=[
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "workflow_type": "daily_brief",
                "status": "failed",
                "trace_id": "trace-1",
                "error_message": "scheduler claim lease expired",
            }
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.heartbeat_scheduler_claim(
        run_id=run_id,
        claim_token="token-1",
        claim_version=3,
        lease_expires_at=now,
    )
    assert store.lock_scheduler_claim_for_publish(
        run_id=run_id,
        claim_token="token-1",
        claim_version=3,
    )
    finalized = store.finalize_scheduler_claim(
        run_id=run_id,
        claim_token="token-1",
        claim_version=3,
        status="succeeded",
        artifact_id=None,
        error_message=None,
        next_run_at="2026-07-14T08:00:00Z",
        metadata_json={"artifact_id": None},
    )
    reaped = store.reap_expired_scheduler_claims(reference_time=now, limit=5)

    assert finalized is not None
    assert reaped[0]["status"] == "failed"
    heartbeat_query = cursor.executed[0][0]
    publish_lock_query = cursor.executed[1][0]
    finalize_query = cursor.executed[2][0]
    reap_query = cursor.executed[4][0]
    for query in (heartbeat_query, publish_lock_query, finalize_query):
        assert "claim_token = %s" in query
        assert "claim_version = %s" in query
        assert "claim_expires_at > clock_timestamp()" in query
    assert "FOR UPDATE OF r, w" in publish_lock_query
    assert "claim_token = NULL" in finalize_query
    assert "FOR UPDATE SKIP LOCKED" in reap_query
    assert "claim_expires_at <= %s::timestamptz" in reap_query
    assert "claim_token = NULL" in reap_query
    assert "w.claim_token = r.expired_claim_token" in reap_query
    assert "FROM updated_runs\n                WHERE EXISTS" not in reap_query
    assert "next_run_at" not in reap_query.split("cleared_workflows AS", 1)[1]


def test_pending_confirmation_query_enforces_all_actionable_invariants_before_limit() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.list_pending_inline_confirmations(limit=3)

    query, params = cursor.executed[0]
    assert "status = 'needs_review'" in query
    assert "confirmation_status = 'unconfirmed'" in query
    assert "confirmation,status" in query
    assert query.index("status = 'needs_review'") < query.index("LIMIT %s")
    assert params == (3,)


def test_update_memory_embedding_and_missing_embedding_listing() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[{"id": memory_id}],
        fetchall_result=[{"id": memory_id}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    updated = store.update_memory_embedding(memory_id=memory_id, vector=[1.0, 0.5])
    missing = store.list_memories_missing_embeddings(limit=64, after_id=memory_id)

    assert updated == {"id": memory_id}
    assert missing[0]["id"] == memory_id
    update_query, update_params = cursor.executed[0]
    assert "SET embedding_vector = %s::vector" in update_query
    assert update_params == ("[1.0,0.5]", memory_id)
    missing_query, missing_params = cursor.executed[1]
    assert "embedding_vector IS NULL" in missing_query
    assert "%s::uuid IS NULL OR id > %s::uuid" in missing_query
    assert "ORDER BY id ASC" in missing_query
    assert missing_params == (memory_id, memory_id, 64)


def test_signed_embedding_update_compares_current_memory_content_digest() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert (
        store.update_memory_embedding(
            memory_id=memory_id,
            vector=[1.0, 0.5],
            provider="stub",
            model="embed-v1",
            endpoint="stub-endpoint",
            content_sha256="a" * 64,
            signature_version=2,
        )
        is None
    )

    query, params = cursor.executed[0]
    assert "digest(" in query
    assert "NULLIF(btrim(title, chr(9)" in query
    assert "[[:space:]]" not in query
    assert "= %s" in query
    assert params[-2:] == (memory_id, "a" * 64)


def test_embedding_digest_sql_uses_exact_python_strip_table_at_every_cas_boundary() -> None:
    python_strip_codepoints = (
        9,
        10,
        11,
        12,
        13,
        28,
        29,
        30,
        31,
        32,
        133,
        160,
        5760,
        8192,
        8193,
        8194,
        8195,
        8196,
        8197,
        8198,
        8199,
        8200,
        8201,
        8202,
        8232,
        8233,
        8239,
        8287,
        12288,
    )
    memory_id = str(uuid4())

    vector_cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    PostgresVNextStore(RecordingConnection(vector_cursor)).search_memories_vector(
        query_vector=[1.0, 0.0],
        embedding_provider="stub",
        embedding_model="embed-v1",
        embedding_signature_version=2,
    )
    vector_query = next(query for query, _params in vector_cursor.executed if "vector_distance" in query)

    update_cursor = RecordingCursor(fetchone_results=[])
    PostgresVNextStore(RecordingConnection(update_cursor)).update_memory_embedding(
        memory_id=memory_id,
        vector=[1.0, 0.0],
        provider="stub",
        model="embed-v1",
        endpoint="stub-endpoint",
        content_sha256="a" * 64,
        signature_version=2,
    )
    update_query = update_cursor.executed[0][0]

    missing_cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    PostgresVNextStore(RecordingConnection(missing_cursor)).list_memories_missing_embeddings(
        embedding_provider="stub",
        embedding_model="embed-v1",
        embedding_signature_version=2,
    )
    missing_query = missing_cursor.executed[0][0]

    for query in (vector_query, update_query, missing_query):
        assert "[[:space:]]" not in query
        assert "regexp_replace(title" not in query
        assert "regexp_replace(canonical_text" not in query
        assert "regexp_replace(summary" not in query
        for codepoint in python_strip_codepoints:
            assert query.count(f"chr({codepoint})") >= 3
        assert "NULLIF(btrim(title," in query
        assert "NULLIF(btrim(canonical_text," in query
        assert "NULLIF(btrim(summary," in query


def test_embedding_backfill_includes_unsigned_or_incompatible_vectors() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.list_memories_missing_embeddings(
        limit=32,
        embedding_provider="openai_compatible",
        embedding_model="embed-v2",
        embedding_signature_version=1,
    )

    query, params = cursor.executed[0]
    assert "embedding_vector IS NULL" in query
    assert "IS DISTINCT FROM %s" in query
    assert "content_sha256" in query
    assert "digest(" in query
    assert "concat_ws(" in query
    assert "embedding_present" in query
    assert params == ("openai_compatible", "embed-v2", "1", None, None, 32)


def test_clear_memory_embedding_removes_signature_metadata() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[{"id": memory_id}], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    assert store.clear_memory_embedding(memory_id=memory_id) == {"id": memory_id}
    query, _params = cursor.executed[0]
    assert "embedding_vector = NULL" in query
    assert "metadata_json = metadata_json - '_alice_embedding'" in query
    assert "{EMBEDDING_SIGNATURE_METADATA_KEY}" not in query


def test_targeted_memory_lookups_use_indexed_columns() -> None:
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": "memory-digest"},
            {"id": "memory-confirmation"},
            {"id": "memory-latest"},
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    by_digest = store.get_memory_by_commit_digest("digest-1")
    by_confirmation = store.get_memory_by_confirmation_id("confirm-1")
    latest = store.latest_agentic_commit_memory(agent_id="hermes")

    assert by_digest == {"id": "memory-digest"}
    assert by_confirmation == {"id": "memory-confirmation"}
    assert latest == {"id": "memory-latest"}
    digest_query, digest_params = cursor.executed[0]
    assert "WHERE commit_digest = %s" in digest_query
    assert "LIMIT 1" in digest_query
    assert digest_params == ("digest-1",)
    confirmation_query, confirmation_params = cursor.executed[1]
    assert "WHERE confirmation_id = %s" in confirmation_query
    assert "LIMIT 1" in confirmation_query
    assert confirmation_params == ("confirm-1",)
    latest_query, latest_params = cursor.executed[2]
    assert "metadata_json #>> '{agentic_memory,kind}' = 'agentic_memory_commit'" in latest_query
    assert "status = 'active'" in latest_query
    assert "metadata_json #>> '{agentic_memory,agent_identity,agent_id}' = %s" in latest_query
    assert "ORDER BY updated_at DESC, created_at DESC, id DESC" in latest_query
    assert latest_params == ("hermes", "hermes", "hermes")


def test_create_memory_persists_commit_digest_and_confirmation_id_columns() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": memory_id, "memory_key": "agentic_memory.semantic.digest-1"},
            _event_row(memory_id),
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_memory(
        {
            "memory_key": "agentic_memory.semantic.digest-1",
            "value": {"text": "Fact"},
            "canonical_text": "Fact",
            "commit_digest": "digest-1",
            "confirmation_id": "confirm-1",
        }
    )

    insert_query, insert_params = cursor.executed[0]
    assert "commit_digest" in insert_query
    assert "confirmation_id" in insert_query
    assert insert_params is not None
    # Tail: commit_digest, confirmation_id, then the (unset) scope and
    # supersession-pointer columns.
    assert insert_params[-7:] == ("digest-1", "confirm-1", None, None, None, None, None)


def test_create_memory_persists_first_class_scope_columns() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": memory_id, "memory_key": "agentic_memory.project_fact.scope-1"},
            _event_row(memory_id),
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_memory(
        {
            "memory_key": "agentic_memory.project_fact.scope-1",
            "value": {"text": "Scoped fact"},
            "canonical_text": "Scoped fact",
            "project_id": "alicebot",
            "created_by_agent_id": "openclaw",
            "run_id": "run-2026-07-04-001",
        }
    )

    insert_query, insert_params = cursor.executed[0]
    assert "project_id" in insert_query
    assert "created_by_agent_id" in insert_query
    assert "run_id" in insert_query
    assert insert_params is not None
    # Tail: the scope columns, then the (unset) supersession pointers.
    assert insert_params[-5:] == ("alicebot", "openclaw", "run-2026-07-04-001", None, None)


def test_create_agent_api_key_persists_project_scope_binding() -> None:
    key_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": key_id,
                "agent_id": "openclaw",
                "permission_profile": "project_scoped_agent",
                "project_scope": "alicebot",
                "key_prefix": "alice_sk_abc",
                "label": None,
            },
            _event_row(key_id),
        ],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.create_agent_api_key(
        {
            "agent_id": "openclaw",
            "permission_profile": "project_scoped_agent",
            "project_scope": "alicebot",
            "key_hash": "a" * 64,
            "key_prefix": "alice_sk_abc",
        }
    )

    assert row["project_scope"] == "alicebot"
    insert_query, insert_params = cursor.executed[0]
    assert "INSERT INTO agent_api_keys" in insert_query
    assert "project_scope" in insert_query
    assert insert_params is not None
    assert insert_params[3] == "alicebot"
    # Unbound keys keep a NULL project_scope.
    cursor.executed.clear()
    cursor.fetchone_results = [
        {
            "id": key_id,
            "agent_id": "hermes",
            "permission_profile": "trusted_local_agent",
            "project_scope": None,
            "key_prefix": "alice_sk_def",
            "label": None,
        },
        _event_row(key_id),
    ]
    unbound = store.create_agent_api_key(
        {
            "agent_id": "hermes",
            "permission_profile": "trusted_local_agent",
            "key_hash": "b" * 64,
            "key_prefix": "alice_sk_def",
        }
    )
    assert unbound["project_scope"] is None
    assert cursor.executed[0][1][3] is None


# -- temporal slice: edge event time, as-of reads, supersession pointers -------


def test_create_edge_populates_observed_at_and_defaults_valid_from_to_event_time() -> None:
    edge_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": edge_id, "edge_type": "supports"},
            _event_row(edge_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_edge(
        {
            "id": edge_id,
            "from_type": "source",
            "from_id": "source-1",
            "to_type": "memory",
            "to_id": "memory-1",
            "edge_type": "supports",
            "created_by": "system",
            "observed_at": "2026-07-01T00:00:00Z",
        }
    )

    insert_query, insert_params = cursor.executed[0]
    assert "observed_at" in insert_query
    # observed_at defaults to write time; valid_from defaults to observed_at
    # (then write time), so the validity interval starts at event time.
    assert "COALESCE(%s::timestamptz, now())" in insert_query
    assert "COALESCE(%s::timestamptz, %s::timestamptz, now())" in insert_query
    assert insert_params is not None
    assert insert_params[9] == "2026-07-01T00:00:00Z"  # observed_at
    assert insert_params[10] is None  # valid_from not passed explicitly...
    assert insert_params[11] == "2026-07-01T00:00:00Z"  # ...so it falls back to observed_at
    metadata_param = insert_params[-1]
    assert isinstance(metadata_param, Jsonb)
    # Real event time was provided: no write-time fallback note.
    assert "observed_at_source" not in metadata_param.obj


def test_create_edge_without_event_time_notes_the_write_time_fallback_in_metadata() -> None:
    edge_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": edge_id, "edge_type": "belongs_to_project"},
            _event_row(edge_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_edge(
        {
            "id": edge_id,
            "from_type": "memory",
            "from_id": "memory-1",
            "to_type": "project",
            "to_id": "alice-vnext",
            "edge_type": "belongs_to_project",
            "created_by": "user",
            "metadata_json": {"review_action": "assign_project"},
        }
    )

    _insert_query, insert_params = cursor.executed[0]
    assert insert_params is not None
    metadata_param = insert_params[-1]
    assert isinstance(metadata_param, Jsonb)
    assert metadata_param.obj["observed_at_source"] == "now"
    # Caller metadata is preserved alongside the note.
    assert metadata_param.obj["review_action"] == "assign_project"


def test_edge_digest_upsert_creates_once_and_replays_without_a_second_event() -> None:
    edge_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            None,  # no existing digest row
            {"id": edge_id, "edge_type": "supports"},
            _event_row(edge_id),
            {"id": edge_id, "edge_type": "supports"},
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))
    payload = {
        "from_type": "source",
        "from_id": "source-1",
        "to_type": "memory",
        "to_id": "memory-1",
        "edge_type": "supports",
        "metadata_json": {"workflow": "connection_finder"},
    }

    created = store.upsert_edge_by_idempotency_digest(payload, digest="edge-digest")
    replayed = store.upsert_edge_by_idempotency_digest(payload, digest="edge-digest")

    assert created["id"] == replayed["id"] == edge_id
    insert_query, insert_params = next(
        (query, params) for query, params in cursor.executed if "INSERT INTO graph_edges" in query
    )
    assert "ON CONFLICT DO NOTHING" in insert_query
    assert insert_params is not None
    metadata_param = insert_params[-1]
    assert isinstance(metadata_param, Jsonb)
    assert metadata_param.obj["idempotency_digest"] == "edge-digest"
    assert _event_log_insert_count(cursor) == 1
    assert sum("INSERT INTO graph_edges" in query for query, _params in cursor.executed) == 1


def test_list_edges_as_of_filters_on_the_validity_interval_with_limit() -> None:
    cursor = RecordingCursor(fetchone_results=[], fetchall_result=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.list_edges_as_of("2026-07-01T00:00:00Z", limit=5)

    query, params = cursor.executed[0]
    assert "FROM graph_edges" in query
    # Half-open interval: valid_from <= at < valid_to; NULL valid_from
    # (pre-slice edges with unrecorded event time) never matches.
    assert "valid_from IS NOT NULL" in query
    assert "valid_from <= %s::timestamptz" in query
    assert "valid_to IS NULL OR valid_to > %s::timestamptz" in query
    assert "LIMIT %s" in query
    assert params == ("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", 5)


def test_memory_writes_accept_supersession_pointer_columns() -> None:
    memory_id = str(uuid4())
    successor_id = str(uuid4())
    predecessor_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": memory_id},
            _event_row(memory_id),
            {"id": memory_id},
            _event_row(memory_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.create_memory(
        {
            "memory_key": "agentic_memory.semantic.replacement-1",
            "value": {"text": "Replacement fact"},
            "canonical_text": "Replacement fact",
            "supersedes": predecessor_id,
        }
    )
    store.update_memory(
        memory_id=memory_id,
        patch={"status": "superseded", "superseded_by": successor_id},
    )

    insert_query, insert_params = cursor.executed[0]
    assert "supersedes" in insert_query
    assert insert_params is not None
    assert insert_params[-2:] == (None, predecessor_id)  # (superseded_by, supersedes)

    update_query, update_params = cursor.executed[2]
    assert "superseded_by = COALESCE(%s::uuid, superseded_by)" in update_query
    assert "supersedes = COALESCE(%s::uuid, supersedes)" in update_query
    assert update_params is not None
    assert successor_id in update_params


def test_update_memory_reassigns_first_class_and_canonical_project_scope_together() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": memory_id,
                "memory_key": "project.release.scope",
                "canonical_text": "Release scope moved.",
                "project_id": "project-new",
                "metadata_json": {
                    "project_id": "project-new",
                    "project_scope": ["project-new"],
                },
            },
            _event_row(memory_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.update_memory(
        memory_id=memory_id,
        patch={
            "project_id": "project-new",
            "metadata_json": {
                "project_id": "project-new",
                "project_scope": ["project-new"],
            },
        },
    )

    query, params = cursor.executed[0]
    assert "project_id = COALESCE(%s, project_id)" in query
    assert params is not None
    metadata_param = next(param for param in params if isinstance(param, Jsonb))
    assert metadata_param.obj["project_scope"] == ["project-new"]
    assert "project-new" in params
    assert row["project_scope"] == ["project-new"]


# -- entity substrate: resolution, mentions, relationship history ---------------


def test_entity_crud_methods_normalize_names_and_write_audit_events() -> None:
    entity_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": entity_id, "entity_type": "organization"},
            _event_row(entity_id),
            {"id": entity_id},
            {"id": entity_id},
            {"id": entity_id},
            _event_row(entity_id),
        ],
        fetchall_result=[{"id": entity_id, "entity_type": "organization"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    created = store.create_entity(
        {
            "id": entity_id,
            "entity_type": "organization",
            "name": "OpenAI, Inc.",
            "aliases": ["open ai"],
            "metadata_json": {"hq": "sf"},
        }
    )
    fetched = store.get_entity(entity_id)
    by_name = store.get_entity_by_normalized_name("organization", "openai inc")
    listed = store.list_entities(entity_type="organization", limit=7)
    updated = store.update_entity(entity_id=entity_id, patch={"name": "OpenAI", "aliases": ["oai"]})

    assert created["id"] == entity_id
    assert fetched is not None
    assert by_name is not None
    assert listed[0]["id"] == entity_id
    assert updated["id"] == entity_id
    assert _event_log_insert_count(cursor) == 2

    insert_query, insert_params = cursor.executed[0]
    assert "INSERT INTO vnext_entities" in insert_query
    assert "app.current_user_id()" in insert_query
    assert insert_params is not None
    assert insert_params[0] == entity_id
    assert insert_params[1] == "organization"
    assert insert_params[2] == "OpenAI, Inc."  # display name keeps its casing
    assert insert_params[3] == "openai inc"  # resolution key computed via normalize_entity_name
    assert isinstance(insert_params[4], Jsonb)
    assert insert_params[4].obj == ["open ai"]
    assert isinstance(insert_params[5], Jsonb)
    assert insert_params[5].obj == {"hq": "sf"}
    assert insert_params[8] == 0  # mention_count defaults to zero

    get_query, get_params = cursor.executed[2]
    assert "FROM vnext_entities" in get_query
    assert "WHERE id = %s::uuid" in get_query
    assert "deleted_at IS NULL" in get_query
    assert get_params == (entity_id,)

    by_name_query, by_name_params = cursor.executed[3]
    assert "entity_type = %s" in by_name_query
    assert "normalized_name = %s" in by_name_query
    assert "LIMIT 1" in by_name_query
    assert by_name_params == ("organization", "openai inc")

    list_query, list_params = cursor.executed[4]
    assert "%s::text IS NULL OR entity_type = %s" in list_query
    assert "ORDER BY updated_at DESC, created_at DESC, id DESC" in list_query
    assert list_params == ("organization", "organization", 7)

    update_query, update_params = cursor.executed[5]
    assert "UPDATE vnext_entities" in update_query
    assert "name = COALESCE(%s, name)" in update_query
    assert "aliases = COALESCE(%s, aliases)" in update_query
    assert "deleted_at IS NULL" in update_query
    assert update_params is not None
    assert update_params[0] == "OpenAI"
    assert isinstance(update_params[1], Jsonb)
    assert update_params[1].obj == ["oai"]
    assert update_params[-1] == entity_id


def test_update_entity_rejects_immutable_patch_fields_before_touching_sql() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    for immutable_patch in (
        {"normalized_name": "hijacked"},
        {"entity_type": "person"},
        {"id": str(uuid4())},
        {"user_id": str(uuid4())},
    ):
        with pytest.raises(ContinuityStoreInvariantError, match="immutable"):
            store.update_entity(entity_id=str(uuid4()), patch=immutable_patch)
    assert cursor.executed == []


def test_find_entities_by_names_matches_normalized_names_and_aliases_in_one_query() -> None:
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "entity-1", "normalized_name": "openai"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.find_entities_by_names(("openai", "type3.capital"))

    assert rows[0]["id"] == "entity-1"
    assert len(cursor.executed) == 1  # one round trip covers both match paths
    query, params = cursor.executed[0]
    assert "FROM vnext_entities" in query
    assert "normalized_name = ANY(%s::text[])" in query
    assert "aliases ?| %s::text[]" in query
    assert "deleted_at IS NULL" in query
    assert "ORDER BY mention_count DESC, updated_at DESC, id DESC" in query
    assert params == (["openai", "type3.capital"], ["openai", "type3.capital"])

    # An empty name tuple short-circuits without touching the database.
    assert store.find_entities_by_names(()) == []
    assert len(cursor.executed) == 1


def test_record_entity_mention_increments_count_and_widens_window() -> None:
    entity_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"id": entity_id},
            _event_row(entity_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.record_entity_mention(entity_id=entity_id, observed_at="2026-05-01T00:00:00Z", source_id="source-1")

    assert row["id"] == entity_id
    assert _event_log_insert_count(cursor) == 1
    query, params = cursor.executed[0]
    assert "mention_count = mention_count + 1" in query
    assert "LEAST(COALESCE(first_observed_at, %s::timestamptz), %s::timestamptz)" in query
    assert "GREATEST(COALESCE(last_observed_at, %s::timestamptz), %s::timestamptz)" in query
    assert "deleted_at IS NULL" in query
    assert params == (
        "2026-05-01T00:00:00Z",
        "2026-05-01T00:00:00Z",
        "2026-05-01T00:00:00Z",
        "2026-05-01T00:00:00Z",
        entity_id,
    )

    # Missing observed_at fails loudly before any SQL runs.
    fresh_cursor = RecordingCursor(fetchone_results=[])
    fresh_store = PostgresVNextStore(RecordingConnection(fresh_cursor))
    with pytest.raises(ContinuityStoreInvariantError, match="observed_at"):
        fresh_store.record_entity_mention(entity_id=entity_id, observed_at=None)
    assert fresh_cursor.executed == []


def test_record_relationship_change_appends_history_and_updates_current_pointer() -> None:
    entity_id = str(uuid4())
    event_id = str(uuid4())
    source_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"relationship_type_before": "advisor"},
            {"id": event_id, "relationship_type_after": "investor"},
            {"id": entity_id},
            _event_row(entity_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.record_relationship_change(
        entity_id=entity_id,
        relationship_type="investor",
        changed_at="2026-02-01T00:00:00Z",
        source_id=source_id,
        metadata_json={"round": "seed"},
    )

    assert row["id"] == event_id
    assert _event_log_insert_count(cursor) == 1

    before_query, before_params = cursor.executed[0]
    assert "metadata_json ->> 'relationship_type'" in before_query
    assert "deleted_at IS NULL" in before_query
    assert before_params == (entity_id,)

    insert_query, insert_params = cursor.executed[1]
    assert "INSERT INTO entity_relationship_events" in insert_query
    assert "app.current_user_id()" in insert_query
    assert insert_params is not None
    assert insert_params[0] == entity_id
    assert insert_params[1] == "advisor"  # before comes from the current pointer
    assert insert_params[2] == "investor"
    assert insert_params[3] == "2026-02-01T00:00:00Z"
    assert insert_params[4] == source_id
    assert isinstance(insert_params[5], Jsonb)
    assert insert_params[5].obj == {"round": "seed"}

    pointer_query, pointer_params = cursor.executed[2]
    assert "UPDATE vnext_entities" in pointer_query
    assert "metadata_json = metadata_json || %s" in pointer_query
    assert pointer_params is not None
    assert isinstance(pointer_params[0], Jsonb)
    assert pointer_params[0].obj == {"relationship_type": "investor"}
    assert pointer_params[1] == entity_id

    event_query, event_params = cursor.executed[3]
    assert "INSERT INTO event_log" in event_query
    assert event_params is not None
    assert isinstance(event_params[7], Jsonb)
    assert event_params[7].obj["relationship_type_before"] == "advisor"
    assert event_params[7].obj["relationship_type_after"] == "investor"
    assert event_params[7].obj["relationship_event_id"] == event_id

    # A missing entity aborts after the lookup, before any history is written.
    missing_cursor = RecordingCursor(fetchone_results=[])
    missing_store = PostgresVNextStore(RecordingConnection(missing_cursor))
    with pytest.raises(ContinuityStoreInvariantError, match="existing entity"):
        missing_store.record_relationship_change(entity_id=entity_id, relationship_type="advisor")
    assert len(missing_cursor.executed) == 1


def test_list_relationship_events_reads_history_most_recent_first() -> None:
    entity_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[],
        fetchall_result=[{"id": "event-1", "relationship_type_after": "investor"}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    rows = store.list_relationship_events(entity_id)

    assert rows[0]["id"] == "event-1"
    query, params = cursor.executed[0]
    assert "FROM entity_relationship_events" in query
    assert "WHERE entity_id = %s::uuid" in query
    assert "ORDER BY changed_at DESC, id DESC" in query
    assert params == (entity_id,)


# -- true redaction ------------------------------------------------------------


class FailingCursor(RecordingCursor):
    """Records like RecordingCursor, then raises on a chosen statement."""

    def __init__(self, fail_on: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        super().execute(query, params)
        if self.fail_on in query:
            raise RuntimeError("boom mid-redaction")


def _redaction_flag_statements(cursor: RecordingCursor) -> list[str]:
    return [query for query, _params in cursor.executed if "app.redaction_in_progress" in query]


def test_redaction_marker_constant() -> None:
    from alicebot_api.vnext_store import REDACTION_MARKER, is_redacted_memory, redacted_memory_metadata

    assert REDACTION_MARKER == "[REDACTED]"
    scrubbed = redacted_memory_metadata(
        {
            "note": "content-bearing prose",
            "project_id": "proj-1",
            "consolidation_digest": "digest-1",
            "superseded_by": "mem-2",
        },
        redacted_at="2026-07-06T00:00:00Z",
    )
    assert scrubbed == {
        "project_id": "proj-1",
        "superseded_by": "mem-2",
        "redacted": True,
        "redacted_at": "2026-07-06T00:00:00Z",
    }
    exact_memory = {
        "id": "memory-1",
        "memory_key": "redacted.memory-1",
        "title": None,
        "canonical_text": REDACTION_MARKER,
        "summary": REDACTION_MARKER,
        "trust_reason": None,
        "value": {"redacted": True},
        "source_event_ids": [],
        "metadata_json": scrubbed,
        "commit_digest": None,
        "confirmation_id": None,
        "status": "archived",
        "deleted_at": "2026-07-06T00:00:00Z",
    }
    assert is_redacted_memory(exact_memory) is True
    assert is_redacted_memory({**exact_memory, "canonical_text": "secret"}) is False
    assert is_redacted_memory(
        {**exact_memory, "metadata_json": {**scrubbed, "source_refs": ["secret"]}}
    ) is False


@pytest.mark.parametrize("target_type", ["memory", "artifact"])
def test_quoted_provenance_rejects_exact_redacted_target_before_insert(target_type: str) -> None:
    target_id = str(uuid4())
    if target_type == "memory":
        target = {
            "id": target_id,
            "memory_key": f"redacted.{target_id}",
            "title": None,
            "canonical_text": "[REDACTED]",
            "summary": "[REDACTED]",
            "trust_reason": None,
            "value": {"redacted": True},
            "source_event_ids": [],
            "metadata_json": {"redacted": True, "redacted_at": "2026-07-16T00:00:00Z"},
            "commit_digest": None,
            "confirmation_id": None,
            "status": "archived",
            "deleted_at": "2026-07-16T00:00:00Z",
        }
    else:
        target = {
            "id": target_id,
            "artifact_type": "project_update",
            "status": "accepted",
            "title": "[REDACTED]",
            "content_markdown": "[REDACTED]",
            "prompt_hash": None,
            "model_info_json": {"redacted": True},
            "metadata_json": {
                "redacted": True,
                "redacted_at": "2026-07-16T00:00:00Z",
                "workflow": "project_auto_update",
                "project_id": "project-1",
                "project_scope": ["project-1"],
                "candidate_memory_id": str(uuid4()),
                "review_action": "accept",
            },
        }
    cursor = RecordingCursor(fetchone_results=[target])
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ValueError, match="quoted provenance cannot be added to a redacted target"):
        store.create_provenance_link(
            {
                "target_type": target_type,
                "target_id": target_id,
                "quote": "must not survive",
            }
        )

    assert len(cursor.executed) == 1
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert not any("INSERT INTO provenance_links" in query for query, _params in cursor.executed)


@pytest.mark.parametrize(
    "metadata_patch",
    [
        {"workflow": "fabricated"},
        {"project_scope": ["other-project"]},
        {"project_scope": "project-1"},
    ],
)
def test_redact_memory_bundle_rejects_malformed_terminal_artifact_provenance(
    metadata_patch: dict[str, object],
) -> None:
    memory_id = str(uuid4())
    artifact_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {
                "id": memory_id,
                "metadata_json": {},
            }
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))
    metadata = {
        "workflow": "project_auto_update",
        "project_id": "project-1",
        "project_scope": ["project-1"],
        "candidate_memory_id": memory_id,
        "review_action": "accept",
        **metadata_patch,
    }

    with pytest.raises(
        ContinuityStoreInvariantError,
        match="project-update redaction requires exact terminal artifact linkage",
    ):
        store.redact_memory_bundle(
            memory_id=memory_id,
            project_update_artifacts=[
                {
                    "id": artifact_id,
                    "artifact_type": "project_update",
                    "status": "accepted",
                    "metadata_json": metadata,
                }
            ],
        )

    assert len(cursor.executed) == 2
    assert "FROM event_log" in cursor.executed[1][0]
    assert _redaction_flag_statements(cursor) == []


@pytest.mark.parametrize("has_prior_receipt", [False, True])
def test_redact_memory_bundle_only_reuses_authorized_prior_redaction_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    has_prior_receipt: bool,
) -> None:
    memory_id = str(uuid4())
    prior_timestamp = "2001-02-03T04:05:06Z"
    minted_timestamp = "2026-07-16T12:34:56Z"

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(minted_timestamp.replace("Z", "+00:00"))

    monkeypatch.setattr(postgres_memory_lifecycle, "datetime", FixedDateTime)
    current = {
        "id": memory_id,
        "memory_key": "legacy.content.derived.key",
        "title": "[REDACTED]",
        "canonical_text": "[REDACTED]",
        "summary": None,
        "trust_reason": "[REDACTED]",
        "value": {"redacted": True},
        # These were not cleared by the pre-0092 path and are deliberately
        # not part of prior-marker eligibility.
        "source_event_ids": [str(uuid4())],
        "metadata_json": {
            "redacted": True,
            "redacted_at": prior_timestamp,
            "source_refs": ["legacy-ref"],
        },
        "commit_digest": "legacy-digest",
        "confirmation_id": "legacy-confirmation",
        "status": "archived",
        "deleted_at": "2026-07-15T00:00:00Z",
        "_redaction_embedding_cleared": True,
        "_redaction_fact_keys_cleared": True,
    }
    updated = {**current, "memory_key": f"redacted.{memory_id}"}
    cursor = RecordingCursor(
        fetchone_results=[
            current,
            {"id": str(uuid4())} if has_prior_receipt else None,  # type: ignore[list-item]
            updated,
            _event_row(memory_id),
        ],
        fetchall_result=[],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    store.redact_memory_bundle(memory_id=memory_id, project_update_artifacts=[])

    update_query, update_params = next(
        (query, params)
        for query, params in cursor.executed
        if "UPDATE memories" in query and "embedding_vector = NULL" in query
    )
    assert update_params is not None
    metadata_params = [param.obj for param in update_params if isinstance(param, Jsonb) and "redacted_at" in param.obj]
    assert metadata_params
    assert metadata_params[0]["redacted_at"] == (prior_timestamp if has_prior_receipt else minted_timestamp)

    event_update_query = next(
        query
        for query, _params in cursor.executed
        if "UPDATE event_log" in query and "jsonb_build_object" in query
    )
    # PostgreSQL cannot infer a type for a bare bind used only as a
    # jsonb_build_object value.  Keep both copies explicitly textual so the
    # role-separated live path cannot regress to IndeterminateDatatype.
    assert event_update_query.count("'memory_id', %s::text") == 2


def test_redact_memory_content_wraps_marker_update_in_redaction_mode() -> None:
    from alicebot_api.vnext_store import REDACTION_MARKER

    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[
            {"metadata_json": {"note": "SECRET", "project_id": "proj-1"}},
            {"id": memory_id, "status": "archived"},
            _event_row(memory_id),
        ]
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    row = store.redact_memory_content(memory_id=memory_id)

    assert row["id"] == memory_id
    queries = [query for query, _params in cursor.executed]
    assert "SELECT metadata_json" in queries[0]
    assert "set_config('app.redaction_in_progress', 'on', false)" in queries[1]
    assert "UPDATE memories" in queries[2]
    assert "set_config('app.redaction_in_progress', 'off', false)" in queries[3]
    assert "INSERT INTO event_log" in queries[4]

    update_query, update_params = cursor.executed[2]
    # Content columns become the marker; skeleton and scope survive.
    assert "CASE WHEN title IS NULL THEN NULL ELSE %s END" in update_query
    assert "canonical_text = %s" in update_query
    assert "CASE WHEN summary IS NULL THEN NULL ELSE %s END" in update_query
    assert "CASE WHEN trust_reason IS NULL THEN NULL ELSE %s END" in update_query
    assert "embedding_vector = NULL" in update_query
    assert "status = 'archived'" in update_query
    assert "deleted_at = COALESCE(deleted_at, clock_timestamp())" in update_query
    # No deleted_at filter: soft-deleted memories are the primary target.
    assert "deleted_at IS NULL" not in update_query
    assert update_params is not None
    assert update_params[:4] == (REDACTION_MARKER,) * 4
    assert isinstance(update_params[4], Jsonb)
    assert update_params[4].obj == {"redacted": True}
    assert isinstance(update_params[5], Jsonb)
    scrubbed = update_params[5].obj
    assert scrubbed["project_id"] == "proj-1"
    assert scrubbed["redacted"] is True
    assert "redacted_at" in scrubbed
    assert "note" not in scrubbed
    assert update_params[6] == memory_id

    event_query, event_params = cursor.executed[4]
    assert event_params is not None
    assert event_params[1] == "memory.redacted"
    payload = next(param for param in event_params if isinstance(param, Jsonb))
    assert payload.obj == {"operation": "redact_memory_content"}
    assert "SECRET" not in repr(payload.obj)


def test_redact_memory_content_raises_when_memory_is_missing() -> None:
    cursor = RecordingCursor(fetchone_results=[])
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(ContinuityStoreInvariantError, match="did not find the memory"):
        store.redact_memory_content(memory_id=str(uuid4()))
    # Redaction mode was never entered.
    assert _redaction_flag_statements(cursor) == []


def test_redact_memory_revisions_scrubs_content_columns_only() -> None:
    from alicebot_api.vnext_store import REDACTION_MARKER

    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[_event_row(memory_id)],
        fetchall_result=[{"id": str(uuid4())}, {"id": str(uuid4())}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    result = store.redact_memory_revisions(memory_id=memory_id)

    assert result == {"memory_id": memory_id, "redacted_revisions": 2}
    update_query, update_params = next(
        (query, params) for query, params in cursor.executed if "UPDATE memory_revisions" in query
    )
    # NULL content stays NULL; non-NULL content becomes the marker shape.
    assert "CASE WHEN previous_value IS NULL THEN NULL ELSE %s END" in update_query
    assert "CASE WHEN new_value IS NULL THEN NULL ELSE %s END" in update_query
    assert "CASE WHEN text_before IS NULL THEN NULL ELSE %s END" in update_query
    assert "text_after = %s" in update_query
    # Reasons can carry content, so reason is redacted too.
    assert "CASE WHEN reason IS NULL THEN NULL ELSE %s END" in update_query
    assert "WHERE memory_id = %s::uuid" in update_query
    assert "RETURNING id" in update_query
    # Skeleton columns are never assigned.
    for column in ("sequence_no", "revision_number", "revision_type", "actor_type", "created_at"):
        assert f"{column} =" not in update_query
    assert update_params is not None
    assert update_params[-1] == memory_id
    assert REDACTION_MARKER in update_params
    jsonb_params = [param.obj for param in update_params if isinstance(param, Jsonb)]
    assert jsonb_params == [{"redacted": True}] * 4

    flags = _redaction_flag_statements(cursor)
    assert "'on'" in flags[0] and "'off'" in flags[1]
    event_query, event_params = cursor.executed[-1]
    assert "INSERT INTO event_log" in event_query
    assert event_params is not None
    assert event_params[1] == "memory.redacted"
    payload = next(param for param in event_params if isinstance(param, Jsonb))
    assert payload.obj == {"operation": "redact_memory_revisions", "redacted_revisions": 2}


def test_redact_memory_events_scrubs_payloads_and_clears_integrity_hash() -> None:
    memory_id = str(uuid4())
    cursor = RecordingCursor(
        fetchone_results=[_event_row(memory_id)],
        fetchall_result=[{"id": str(uuid4())}],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    result = store.redact_memory_events(memory_id=memory_id)

    assert result == {"memory_id": memory_id, "redacted_events": 1}
    update_query, update_params = next(
        (query, params) for query, params in cursor.executed if "UPDATE event_log" in query
    )
    assert "jsonb_build_object" in update_query
    assert "'redacted', true" in update_query
    assert "'event_type', event_type" in update_query
    assert "integrity_hash = NULL" in update_query
    assert "target_type = 'memory' AND target_id = %s" in update_query
    assert "payload_candidate_memory_id = %s" in update_query
    assert "payload_memory_id = %s" in update_query
    assert "payload_json::text" not in update_query
    assert "LIKE" not in update_query
    # Skeleton columns are never assigned in the SET clause.
    set_clause = update_query.split("WHERE")[0].replace("'event_type', event_type", "")
    for column in ("event_type =", "actor_type =", "occurred_at =", "target_id ="):
        assert column not in set_clause
    assert update_params == (memory_id, memory_id, memory_id, memory_id)

    flags = _redaction_flag_statements(cursor)
    assert len(flags) == 2 and "'on'" in flags[0] and "'off'" in flags[1]
    event_query, event_params = cursor.executed[-1]
    assert "INSERT INTO event_log" in event_query
    assert event_params is not None
    assert event_params[1] == "memory.redacted"
    payload = next(param for param in event_params if isinstance(param, Jsonb))
    assert payload.obj == {"operation": "redact_memory_events", "redacted_events": 1}


def test_redaction_mode_resets_even_when_the_update_fails() -> None:
    memory_id = str(uuid4())
    cursor = FailingCursor(
        fail_on="UPDATE memory_revisions",
        fetchone_results=[],
        fetchall_result=[],
    )
    store = PostgresVNextStore(RecordingConnection(cursor))

    with pytest.raises(RuntimeError, match="boom mid-redaction"):
        store.redact_memory_revisions(memory_id=memory_id)

    flags = _redaction_flag_statements(cursor)
    assert len(flags) == 2
    assert "'on'" in flags[0]
    assert "'off'" in flags[1]
    # The reset is the last statement issued; no event is appended after
    # a failed redaction.
    assert "app.redaction_in_progress" in cursor.executed[-1][0]
    assert not any("INSERT INTO event_log" in query for query, _params in cursor.executed)
