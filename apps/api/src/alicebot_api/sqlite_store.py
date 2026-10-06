"""SQLite-backed vNext store for the zero-infrastructure on-ramp.

``SQLiteVNextStore`` mirrors the method signatures and return shapes of
``alicebot_api.vnext_store.PostgresVNextStore`` for the store surface the
core MCP tools use, backed by a local SQLite file instead of
Postgres.

Tenancy: Postgres scopes every statement with row-level security bound to
``app.current_user_id()``. SQLite has no RLS, so this store binds the
``user_id`` given at construction into EVERY statement it issues. A query
without the ``user_id`` predicate is a security bug.

Value conventions (differences forced by SQLite storage types):
- ids and timestamps come back as TEXT (``str(uuid)`` / ISO-8601 UTC with
  a trailing ``Z``) instead of ``uuid.UUID`` / ``datetime`` objects.
  Callers already run rows through ``vnext_json.json_safe`` which maps
  those objects to exactly these strings.
- JSON columns are decoded back into dicts/lists, matching psycopg jsonb.
"""

from __future__ import annotations

import itertools
import json
import sqlite3

from alicebot_api.vnext_label_writes import takes_label_lock
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import numpy as np

from alicebot_api.source_search_limits import (
    literal_match_operand,
    require_literal_match_query,
    require_source_search_query,
)
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_capture import (
    capture_content_hash_for_source,
    capture_dedupe_key_for_source,
    source_capture_raw_text,
)
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    EMBEDDING_VECTOR_DIMENSIONS,
    memory_embedding_signature_is_current,
    pad_embedding_vector,
)
from alicebot_api.vnext_entity_names import ENTITY_IMMUTABLE_PATCH_FIELDS, normalize_entity_name
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_project_scope import (
    expose_memory_project_scope,
    normalize_project_scope,
    project_scope_identity,
    resolve_project_scope,
    resolve_source_metadata_project_scope,
    source_capture_identity_matches,
    source_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.memory_lifecycle_common import (
    REDACTED_JSON_VALUE as REDACTED_JSON_VALUE,
    REDACTION_MARKER as REDACTION_MARKER,
    is_prior_redacted_memory_marker as is_prior_redacted_memory_marker,
    is_redacted_memory as is_redacted_memory,
    redacted_memory_metadata as redacted_memory_metadata,
)
from alicebot_api.vnext_stores.retrieval_common import (
    FTS_QUERY_STOPWORDS as _FTS_QUERY_STOPWORDS,
    _search_patterns as _search_patterns,
    fts_fallback_tokens as fts_fallback_tokens,
)
from alicebot_api.vnext_stores.sqlite.source_retirement import (
    markdown_sources_by_path as _markdown_sources_by_path,
    supersede_source as _supersede_source,
    scrub_source as _scrub_source,
    source_inventory as _source_inventory,
    prunable_sources as _prunable_sources,
)
from alicebot_api.vnext_stores.sqlite.columns import (
    ENTITY_COLUMNS as ENTITY_COLUMNS,
    ENTITY_RELATIONSHIP_EVENT_COLUMNS as ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    EVENT_LOG_COLUMNS as EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS as GRAPH_EDGE_COLUMNS,
    MEMORY_COLUMNS as MEMORY_COLUMNS,
    OPEN_LOOP_COLUMNS as OPEN_LOOP_COLUMNS,
    PROVENANCE_COLUMNS as PROVENANCE_COLUMNS,
    REVISION_COLUMNS as REVISION_COLUMNS,
)
from alicebot_api.vnext_stores.sqlite.browser_clip_capabilities import (
    consume_browser_clip_capability as _browser_clip_consume_capability,
    create_browser_clip_capability as _browser_clip_create_capability,
)
from alicebot_api.vnext_stores.sqlite.embedding_cas import (
    _embedding_content_sha256_sqlite as _embedding_content_sha256_sqlite,
    _ensure_embedding_content_sha256_sqlite as _ensure_embedding_content_sha256_sqlite,
    clear_memory_embedding as _clear_memory_embedding,
    list_memories_missing_embeddings as _list_memories_missing_embeddings,
    update_memory_embedding as _update_memory_embedding,
)
from alicebot_api.vnext_stores.sqlite.events_revisions import (
    _PROJECT_UPDATE_EVENT_LINKAGE_SQL as _PROJECT_UPDATE_EVENT_LINKAGE_SQL,
    _PROJECT_UPDATE_EVENT_LOOKUP_SQL as _PROJECT_UPDATE_EVENT_LOOKUP_SQL,
    _PROJECT_UPDATE_EVENT_TYPES_SQL as _PROJECT_UPDATE_EVENT_TYPES_SQL,
    _append_mutation_event as _events_append_mutation_event,
    append_event as _events_append_event,
    append_revision as _events_append_revision,
    count_events as _events_count_events,
    list_events as _events_list_events,
    list_events_for_source_trace as _events_list_events_for_source_trace,
    list_project_update_events as _events_list_project_update_events,
    list_revisions as _events_list_revisions,
)
from alicebot_api.vnext_stores.sqlite.graph_open_loops import (
    create_graph_edge as _graph_create_graph_edge,
    list_edges as _graph_list_edges,
    list_memory_entity_edges as _graph_list_memory_entity_edges,
    expire_edge as _graph_expire_edge,
    list_edges_as_of as _graph_list_edges_as_of,
    create_entity as _graph_create_entity,
    get_entity as _graph_get_entity,
    get_entity_by_normalized_name as _graph_get_entity_by_normalized_name,
    find_entities_by_names as _graph_find_entities_by_names,
    list_entities as _graph_list_entities,
    update_entity as _graph_update_entity,
    record_entity_mention as _graph_record_entity_mention,
    record_relationship_change as _graph_record_relationship_change,
    list_relationship_events as _graph_list_relationship_events,
    create_open_loop as _graph_create_open_loop,
    upsert_open_loop_by_automation_digest as _graph_upsert_open_loop_by_automation_digest,
    get_open_loop as _graph_get_open_loop,
    find_open_loop_by_automation_digest as _graph_find_open_loop_by_automation_digest,
    list_open_loops_referencing_source as _graph_list_open_loops_referencing_source,
    list_open_loops as _graph_list_open_loops,
    list_open_loops_view_partitions as _graph_list_open_loops_view_partitions,
    list_open_loop_events as _graph_list_open_loop_events,
    update_open_loop as _graph_update_open_loop,
    update_open_loop_status as _graph_update_open_loop_status,
)
from alicebot_api.vnext_stores.sqlite.memory_lifecycle import (
    create_memory as _lifecycle_create_memory,
    upsert_memory_by_key as _lifecycle_upsert_memory_by_key,
    get_memory_for_update as _lifecycle_get_memory_for_update,
    get_memory_for_redaction as _lifecycle_get_memory_for_redaction,
    lock_project_update_artifacts_for_redaction as _lifecycle_lock_project_update_artifacts_for_redaction,
    memory_redaction_bundle_is_exact as _lifecycle_memory_redaction_bundle_is_exact,
    update_memory as _lifecycle_update_memory,
    lock_graph_mutation as _lifecycle_lock_graph_mutation,
    list_memory_ids_with_embeddings as _lifecycle_list_memory_ids_with_embeddings,
    update_memory_fact_keys as _lifecycle_update_memory_fact_keys,
    list_memories_missing_fact_keys as _lifecycle_list_memories_missing_fact_keys,
    _redaction_mode as _lifecycle__redaction_mode,
    redact_memory_bundle as _lifecycle_redact_memory_bundle,
    redact_memory_content as _lifecycle_redact_memory_content,
    redact_memory_revisions as _lifecycle_redact_memory_revisions,
    redact_memory_events as _lifecycle_redact_memory_events,
    create_provenance_link as _lifecycle_create_provenance_link,
    list_provenance_links as _lifecycle_list_provenance_links,
    list_provenance_links_for_targets as _lifecycle_list_provenance_links_for_targets,
)
from alicebot_api.vnext_stores.sqlite.memory_access import (
    _MEMORY_SEARCHABLE_STATUSES_SQL as _MEMORY_SEARCHABLE_STATUSES_SQL,
    count_memories as _memory_count_memories,
    count_memories_by_status as _memory_count_memories_by_status,
    count_rollup_input_memories as _memory_count_rollup_input_memories,
    find_live_memory_by_canonical_text as _memory_find_live_memory_by_canonical_text,
    get_memories_by_ids as _memory_get_memories_by_ids,
    get_memory as _memory_get_memory,
    get_memory_by_commit_digest as _memory_get_memory_by_commit_digest,
    get_memory_by_confirmation_id as _memory_get_memory_by_confirmation_id,
    get_memory_by_key as _memory_get_memory_by_key,
    latest_agentic_commit_memory as _memory_latest_agentic_commit_memory,
    list_accepted_rollup_cards as _memory_list_accepted_rollup_cards,
    list_memories as _memory_list_memories,
    list_memories_by_statuses as _memory_list_memories_by_statuses,
    list_memories_view_partitions as _memory_list_memories_view_partitions,
    list_memories_for_staleness_sweep as _memory_list_memories_for_staleness_sweep,
    list_memories_referencing_source as _memory_list_memories_referencing_source,
    list_memories_referencing_sources as _memory_list_memories_referencing_sources,
    list_pending_derived_candidates_for_member as _memory_list_pending_derived_candidates_for_member,
    list_pending_inline_confirmations as _memory_list_pending_inline_confirmations,
    list_pending_rollup_candidates as _memory_list_pending_rollup_candidates,
    list_recent_agentic_commits as _memory_list_recent_agentic_commits,
    list_rollup_input_memories as _memory_list_rollup_input_memories,
    search_memories as _memory_search_memories,
    search_memories_by_time as _memory_search_memories_by_time,
    search_memories_fts as _memory_search_memories_fts,
    search_memories_vector as _memory_search_memories_vector,
)
from alicebot_api.vnext_stores.sqlite.primitives import (
    _iso_or_none as _iso_or_none,
    _iso_or_now as _iso_or_now,
    _json_list_text as _json_list_text,
    _json_object_text as _json_object_text,
    _new_id as _new_id,
    _sorted_field_names as _sorted_field_names,
    _utc_now_iso as _utc_now_iso,
    _uuid_text as _uuid_text,
)
from alicebot_api.vnext_stores.sqlite.query_predicates import (
    _created_by_clause as _query_created_by_clause,
    _domain_clause as _query_domain_clause,
    _ensure_project_scope_identity_sqlite as _ensure_project_scope_identity_sqlite,
    _escape_like_literal as _escape_like_literal,
    _expiry_clause as _query_expiry_clause,
    _fts_match_any_expression as _fts_match_any_expression,
    _fts_match_expression as _fts_match_expression,
    _like_any as _query_like_any,
    _memory_type_clause as _query_memory_type_clause,
    _metadata_scope_clause as _query_metadata_scope_clause,
    _placeholders as _query_placeholders,
    _project_clause as _query_project_clause,
    _project_scope_identity_json_sqlite as _project_scope_identity_json_sqlite,
    _project_scope_value_sqlite as _project_scope_value_sqlite,
    _retrieval_scope_clause as _query_retrieval_scope_clause,
    _run_clause as _query_run_clause,
    _sensitivity_clause as _query_sensitivity_clause,
    _source_project_scope_identity_json_sqlite as _source_project_scope_identity_json_sqlite,
    _sqlite_ascii_literal_contains_sql as _sqlite_ascii_literal_contains_sql,
    _stated_exclusion as _query_stated_exclusion,
)

VNextRow = dict[str, object]



SOURCE_COLUMNS = (
    "id",
    "user_id",
    "source_type",
    "title",
    "author",
    "uri",
    "raw_path",
    "content_hash",
    "dedupe_key",
    "captured_at",
    "source_created_at",
    "source_modified_at",
    "connector_name",
    "external_id",
    "domain",
    "sensitivity",
    "metadata_json",
    "deleted_at",
)

SOURCE_CHUNK_COLUMNS = (
    "id",
    "user_id",
    "source_id",
    "chunk_index",
    "text",
    "token_count",
    "metadata_json",
    "created_at",
)





AGENT_IDENTITY_COLUMNS = (
    "id",
    "user_id",
    "agent_id",
    "agent_type",
    "permission_profile",
    "display_name",
    "project_scope_json",
    "metadata_json",
    "created_at",
    "updated_at",
)

AGENT_API_KEY_COLUMNS = (
    "id",
    "user_id",
    "agent_id",
    "permission_profile",
    "project_scope",
    "key_hash",
    "key_prefix",
    "label",
    "created_at",
    "revoked_at",
    "last_used_at",
)



# Names for nested savepoints. One counter for the process, so two stores on
# two connections never share a name and a nested block never reuses its
# parent's.
_SAVEPOINT_NAMES = itertools.count(1)

# Columns stored as JSON TEXT that must decode back to dicts/lists so
# returned rows match psycopg's jsonb decoding.
_JSON_COLUMNS = frozenset(
    {
        "aliases",
        "candidate",
        "metadata_json",
        "new_value",
        "payload_json",
        "previous_value",
        "project_scope_json",
        "source_event_ids",
        "value",
    }
)



def _dict_row_factory(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def _row_as_dict(cursor: sqlite3.Cursor, row: object) -> VNextRow:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [description[0] for description in cursor.description]
    return dict(zip(names, cast(tuple[object, ...], row)))


def ensure_sqlite_user(
    conn: sqlite3.Connection,
    user_id: UUID | str,
    email: str,
    display_name: str | None = None,
) -> VNextRow:
    """Bootstrap the local user row this store scopes everything to."""
    uid = str(user_id)
    cursor = conn.execute(
        "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
        (uid,),
    )
    row = cursor.fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO users (id, email, display_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (uid, email, display_name, _utc_now_iso()),
        )
        cursor = conn.execute(
            "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
            (uid,),
        )
        row = cursor.fetchone()
    if row is None:  # pragma: no cover - defensive
        raise ContinuityStoreInvariantError("ensure_sqlite_user did not return a row from the database")
    return _row_as_dict(cursor, row)


@contextmanager
def sqlite_user_connection(path: str | Path, user_id: UUID | str, *, repair_labels: bool = True) -> Iterator[sqlite3.Connection]:
    """Open a bootstrapped SQLite connection wrapped in one transaction.

    Mirrors ``alicebot_api.db.user_connection`` semantics: dict rows, the
    schema is present, statements run inside a transaction that commits on
    clean exit and rolls back on error. The ``user_id`` is validated here;
    binding it into statements is the job of ``SQLiteVNextStore``.
    """
    if str(user_id).strip() == "":
        raise ContinuityStoreInvariantError("sqlite_user_connection requires a non-empty user_id")
    conn = sqlite3.connect(str(path))
    conn.row_factory = _dict_row_factory
    try:
        if repair_labels:
            bootstrap_sqlite_schema(conn)
        else:
            bootstrap_sqlite_schema(conn, repair_labels=False)
        conn.commit()
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class SQLiteVNextStore:
    """SQLite-backed vNext repository facade for the second-brain kernel."""

    #: Retrieval-trace label for the full-text stage (FTS5, not Postgres tsvector).
    fts_stage_source = "sqlite_fts"

    def __init__(self, conn: sqlite3.Connection, user_id: UUID | str):
        if str(user_id).strip() == "":
            raise ContinuityStoreInvariantError("SQLiteVNextStore requires a non-empty user_id")
        self.conn = conn
        self.user_id = str(user_id)
        _ensure_embedding_content_sha256_sqlite(self.conn)
        _ensure_project_scope_identity_sqlite(self.conn)

    def lock_label_writes(self, *, exclusive: bool = False) -> None:
        """The SQLite writer lock is the label lock. Begin it when none is open."""

        del exclusive
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")

    def read_label_rows(self, kind: str, ids: Sequence[str]) -> list[VNextRow]:
        """Narrow label rows for the insert floor. No text columns."""

        wanted = [str(item) for item in ids if str(item)]
        if not wanted:
            return []
        table = {"source": "sources", "memory": "memories", "open_loop": "open_loops"}.get(kind)
        if table is None:
            return []
        extra = ""
        if table == "memories":
            extra = ", value, project_id, source_event_ids, deleted_at, status"
        elif table == "open_loops":
            extra = ", project_id, source_id, memory_id"
        from uuid import UUID
        canonical = []
        for item in wanted:
            try:
                canonical.append(UUID(item).hex)
            except (ValueError, TypeError):
                pass
        marks = ",".join("?" for _ in wanted)
        alias_sql = ""
        if canonical:
            alias_marks = ",".join("?" for _ in canonical)
            alias_sql = f" OR replace(replace(replace(replace(lower(id),'urn:uuid:',''),'-',''),'{{',''),'}}','') IN ({alias_marks})"
        return self._fetch_all(
            f"""
                SELECT id, user_id, domain, sensitivity, metadata_json{extra}
                FROM {table}
                WHERE user_id = ? AND (id IN ({marks}){alias_sql})
                """,
            (self.user_id, *wanted, *canonical),
        )

    def iter_label_rows(self, kind: str, *, batch_size: int = 200) -> Iterator[list[VNextRow]]:
        """Complete counted population, in narrow tenant-bound keyset batches."""

        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        table = {"source": "sources", "memory": "memories", "open_loop": "open_loops"}.get(kind)
        if table is None:
            raise ValueError("unsupported label kind")
        extra = ""
        if kind == "memory":
            extra = ", status, value, project_id, source_event_ids, deleted_at"
        elif kind == "open_loop":
            extra = ", status, project_id, source_id, memory_id"
        live = " AND deleted_at IS NULL" if kind in {"source", "memory"} else ""
        after = ""
        while True:
            rows = self._fetch_all(
                f"""SELECT id, user_id, domain, sensitivity, metadata_json{extra}
                    FROM {table} WHERE user_id = ? AND id > ?{live}
                    ORDER BY id LIMIT ?""",
                (self.user_id, after, batch_size),
            )
            if not rows:
                return
            yield rows
            after = str(rows[-1]["id"])

    def iter_label_events(self, *, batch_size: int = 200) -> Iterator[list[VNextRow]]:
        """Complete event targets for readable counts, without event payloads."""

        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        after = ""
        while True:
            rows = self._fetch_all(
                """SELECT id, target_type, target_id, event_type FROM event_log
                   WHERE user_id = ? AND id > ? ORDER BY id LIMIT ?""",
                (self.user_id, after, batch_size),
            )
            if not rows:
                return
            yield rows
            after = str(rows[-1]["id"])

    # -- fetch helpers (mirror PostgresVNextStore conventions) ------------

    def _execute(self, query: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(query, params)

    def _decode_row(self, row: VNextRow) -> VNextRow:
        decoded: VNextRow = {}
        for key, value in row.items():
            if key in _JSON_COLUMNS and isinstance(value, str):
                decoded[key] = json.loads(value)
            else:
                decoded[key] = value
        return expose_memory_project_scope(decoded)

    def _fetch_one(
        self,
        operation_name: str,
        query: str,
        params: tuple[object, ...] = (),
    ) -> VNextRow:
        row = self._fetch_optional_one(query, params)
        if row is None:
            raise ContinuityStoreInvariantError(
                f"{operation_name} did not return a row from the database",
            )
        return row

    def _fetch_optional_one(
        self,
        query: str,
        params: tuple[object, ...] = (),
    ) -> VNextRow | None:
        cursor = self._execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None
        return self._decode_row(_row_as_dict(cursor, row))

    def _fetch_all(
        self,
        query: str,
        params: tuple[object, ...] = (),
    ) -> list[VNextRow]:
        cursor = self._execute(query, params)
        return [self._decode_row(_row_as_dict(cursor, row)) for row in cursor.fetchall()]

    def _get_row(
        self,
        operation_name: str,
        table: str,
        columns: tuple[str, ...],
        row_id: str,
    ) -> VNextRow:
        return self._fetch_one(
            operation_name,
            f"""
                SELECT {", ".join(columns)}
                FROM {table}
                WHERE id = ?
                  AND user_id = ?
                """,
            (row_id, self.user_id),
        )

    # -- filter helpers ----------------------------------------------------

    _placeholders = _query_placeholders

    _domain_clause = _query_domain_clause

    _sensitivity_clause = _query_sensitivity_clause

    _memory_type_clause = _query_memory_type_clause

    _project_clause = _query_project_clause

    _created_by_clause = _query_created_by_clause

    _run_clause = _query_run_clause

    _expiry_clause = _query_expiry_clause

    _retrieval_scope_clause = _query_retrieval_scope_clause

    _metadata_scope_clause = _query_metadata_scope_clause

    _like_any = _query_like_any

    # -- event log ----------------------------------------------------------

    _append_mutation_event = _events_append_mutation_event
    append_event = _events_append_event
    list_events = _events_list_events
    list_events_for_source_trace = _events_list_events_for_source_trace

    def list_resume_memory_events(
        self,
        *,
        statuses: Sequence[str],
        projects: Sequence[str] | None = None,
        query: str | None = None,
        occurred_at_start: datetime | None = None,
        occurred_at_end: datetime | None = None,
        limit: int = 20,
        exclude_global_domains: Sequence[str] | None = None,
        # No defaults for the domain filter and the sensitivity ceiling, as in
        # ``list_memories_view_partitions``: a caller states what it admits, and
        # ``None`` is the statement "no filter".
        domains: list[str] | None,
        sensitivity_allowed: list[str] | None,
    ) -> list[VNextRow]:
        """Return events joined to resume-admitted memories before LIMIT.

        A memory is admitted when its status is in ``statuses`` and its
        ``valid_to`` has not passed (recall's ``_expiry_clause``), so the event of
        an expired memory is not listed and does not use up the ``LIMIT``.

        ``projects`` may hold the reserved global marker (spec 6.1). With it,
        ``exclude_global_domains`` leaves out events of global memories in those
        domains before ``LIMIT``, and it must be stated (an empty tuple leaves none
        out): ``None`` raises.

        ``domains`` and ``sensitivity_allowed`` are the rules ``list_memories``
        applies to the memory itself (a memory in an unlisted domain, or above the
        ceiling, has its events left out before ``LIMIT``), so an event the caller
        may not see never takes a place that one it may see needs. An empty
        ``sensitivity_allowed`` admits nothing: it returns no rows and builds no
        ``sensitivity IN ()``.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        if sensitivity_allowed is not None and not sensitivity_allowed:
            return []
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return []
        project_sql, project_params = self._project_clause(
            tuple(normalize_project_scope(projects or ())),
            prefix="memory.",
            global_excluded_domains=_query_stated_exclusion(exclude_global_domains),
        )
        expiry_sql, expiry_params = self._expiry_clause(False, prefix="memory.")
        domain_sql, domain_params = self._domain_clause(domains, prefix="memory.")
        sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed, prefix="memory.")
        clauses = [
            "event.user_id = ?",
            "event.target_type = 'memory'",
            "memory.deleted_at IS NULL",
            f"memory.status IN ({self._placeholders(normalized_statuses)})",
        ]
        params: list[object] = [
            self.user_id,
            *normalized_statuses,
            *project_params,
            *expiry_params,
            *domain_params,
            *sensitivity_params,
        ]
        scoped_where_sql = " AND ".join(clauses) + project_sql + expiry_sql + domain_sql + sensitivity_sql
        filters: list[str] = []
        normalized_query = str(query).strip() if query is not None else ""
        if normalized_query:
            filters.append(
                f"({_sqlite_ascii_literal_contains_sql("COALESCE(memory.title, '')")}"
                f" OR {_sqlite_ascii_literal_contains_sql("COALESCE(memory.canonical_text, '')")}"
                f" OR {_sqlite_ascii_literal_contains_sql("COALESCE(memory.summary, '')")})"
            )
            escaped_query = literal_match_operand(normalized_query)
            params.extend((escaped_query, escaped_query, escaped_query))
        if occurred_at_start is not None:
            filters.append("julianday(event.occurred_at) >= julianday(?)")
            params.append(_iso_or_none(occurred_at_start))
        if occurred_at_end is not None:
            filters.append("julianday(event.occurred_at) <= julianday(?)")
            params.append(_iso_or_none(occurred_at_end))
        filter_sql = "".join(f" AND {predicate}" for predicate in filters)
        params.append(limit)
        return self._fetch_all(
            f"""
                SELECT {", ".join(f"event.{column}" for column in EVENT_LOG_COLUMNS)}
                FROM event_log AS event
                JOIN memories AS memory
                  ON memory.user_id = event.user_id
                 AND memory.id = event.target_id
                WHERE {scoped_where_sql}{filter_sql}
                ORDER BY event.occurred_at DESC, event.id DESC
                LIMIT ?
                """,
            tuple(params),
        )

    list_project_update_events = _events_list_project_update_events

    def list_memory_events(
        self,
        *,
        event_type_prefix: str | None = None,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_person_memory_ids: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
        limit: int = 20,
    ) -> list[VNextRow]:
        """Memory events whose target row matches scope before LIMIT."""
        if limit < 1:
            raise ValueError("limit must be positive")
        project_sql, project_params = self._project_clause(scope_projects, prefix="m.")
        people = [str(value).strip().casefold() for value in scope_people if str(value).strip()]
        person_ids = [str(value) for value in scope_person_memory_ids if str(value)]
        params: list[object] = [self.user_id]
        prefix_sql = ""
        if event_type_prefix is not None:
            prefix_sql = " AND e.event_type LIKE ?"
            params.append(f"{event_type_prefix}%")
        params.extend(project_params)
        people_sql = ""
        if people or person_ids:
            people_placeholders = self._placeholders(people)
            ids_placeholders = self._placeholders(person_ids)
            people_terms: list[str] = []
            if person_ids:
                people_terms.append(f"m.id IN ({ids_placeholders})")
                params.extend(person_ids)
            if people:
                metadata_paths = ("person_id", "person_ids", "person", "people", "people_ids")
                path_terms = " OR ".join(
                    "EXISTS (SELECT 1 FROM json_each(json_extract(m.metadata_json, ?)) AS scoped_person "
                    f"WHERE lower(trim(CAST(scoped_person.value AS TEXT))) IN ({people_placeholders}))"
                    for _path in metadata_paths
                )
                people_terms.append(f"({path_terms})")
                for path in metadata_paths:
                    params.extend((f"$.{path}", *people))
            people_sql = f" AND ({' OR '.join(people_terms)})"
        window_sql = ""
        if scope_window_start is not None:
            window_sql += " AND julianday(e.occurred_at) >= julianday(?)"
            params.append(scope_window_start.isoformat())
        if scope_window_end is not None:
            window_sql += " AND julianday(e.occurred_at) <= julianday(?)"
            params.append(scope_window_end.isoformat())
        params.append(limit)
        qualified_columns = ", ".join(f"e.{column}" for column in EVENT_LOG_COLUMNS)
        return self._fetch_all(
            f"""
                SELECT {qualified_columns}
                FROM event_log e
                JOIN memories m
                  ON e.target_type = 'memory'
                 AND e.target_id = m.id
                 AND e.user_id = m.user_id
                WHERE e.user_id = ?
                  AND m.deleted_at IS NULL
                  {prefix_sql}
                  {project_sql}
                  {people_sql}
                  {window_sql}
                ORDER BY e.occurred_at DESC, e.id DESC
                LIMIT ?
                """,
            tuple(params),
        )

    count_events = _events_count_events

    # -- sources -------------------------------------------------------------

    markdown_sources_by_path = _markdown_sources_by_path
    supersede_source = _supersede_source
    scrub_source = _scrub_source
    source_inventory = _source_inventory
    prunable_sources = _prunable_sources

    @takes_label_lock
    def create_source(self, source: JsonObject, *, actor_type: str = "system") -> VNextRow:
        source_id = _new_id(source.get("id"))
        self._execute(
            """
                INSERT INTO sources (
                  id,
                  user_id,
                  source_type,
                  title,
                  author,
                  uri,
                  raw_path,
                  content_hash,
                  dedupe_key,
                  captured_at,
                  source_created_at,
                  source_modified_at,
                  connector_name,
                  external_id,
                  domain,
                  sensitivity,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                source_id,
                self.user_id,
                source["source_type"],
                source.get("title"),
                source.get("author"),
                source.get("uri"),
                source.get("raw_path"),
                source["content_hash"],
                source.get("dedupe_key", source["content_hash"]),
                _iso_or_now(source.get("captured_at")),
                _iso_or_none(source.get("source_created_at")),
                _iso_or_none(source.get("source_modified_at")),
                source.get("connector_name"),
                source.get("external_id"),
                source.get("domain", "unknown"),
                source.get("sensitivity", "unknown"),
                _json_object_text(source.get("metadata_json")),
            ),
        )
        row = self._get_row("create_source", "sources", SOURCE_COLUMNS, source_id)
        self._append_mutation_event(
            event_type="source.created",
            actor_type=actor_type,
            target_type="source",
            target_id=row["id"],
            payload={"operation": "create", "fields": _sorted_field_names(source)},
        )
        return row

    create_browser_clip_capability = _browser_clip_create_capability
    consume_browser_clip_capability = _browser_clip_consume_capability

    @takes_label_lock
    def get_or_create_source(
        self,
        source: JsonObject,
        *,
        actor_type: str = "system",
    ) -> tuple[VNextRow, bool]:
        """Atomically claim a live capture identity under SQLite's writer lock."""
        source_id = _new_id(source.get("id"))
        dedupe_key = str(source.get("dedupe_key") or source["content_hash"])
        cursor = self._execute(
            """
                INSERT INTO sources (
                  id, user_id, source_type, title, author, uri, raw_path,
                  content_hash, dedupe_key, captured_at, source_created_at,
                  source_modified_at, connector_name, external_id, domain,
                  sensitivity, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, dedupe_key)
                  WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
            (
                source_id,
                self.user_id,
                source["source_type"],
                source.get("title"),
                source.get("author"),
                source.get("uri"),
                source.get("raw_path"),
                source["content_hash"],
                dedupe_key,
                _iso_or_now(source.get("captured_at")),
                _iso_or_none(source.get("source_created_at")),
                _iso_or_none(source.get("source_modified_at")),
                source.get("connector_name"),
                source.get("external_id"),
                source.get("domain", "unknown"),
                source.get("sensitivity", "unknown"),
                _json_object_text(source.get("metadata_json")),
            ),
        )
        created = cursor.fetchone() is not None
        row = (
            self._get_row("get_or_create_source", "sources", SOURCE_COLUMNS, source_id)
            if created
            else self._fetch_one(
                "get_or_create_source",
                f"""
                    SELECT {", ".join(SOURCE_COLUMNS)}
                    FROM sources
                    WHERE user_id = ?
                      AND dedupe_key = ?
                      AND deleted_at IS NULL
                    ORDER BY captured_at DESC, id DESC
                    LIMIT 1
                    """,
                (self.user_id, dedupe_key),
            )
        )
        if not created and not source_capture_identity_matches(
            row,
            content_hashes=(str(source["content_hash"]),),
            project_scope=source_project_scope(source),
            domain=source.get("domain", "unknown"),
            sensitivity=source.get("sensitivity", "unknown"),
        ):
            raise ContinuityStoreInvariantError("atomic source dedupe winner does not match capture identity")
        if created:
            self._append_mutation_event(
                event_type="source.created",
                actor_type=actor_type,
                target_type="source",
                target_id=row["id"],
                payload={"operation": "create", "fields": _sorted_field_names(source)},
            )
        return row, created

    def get_source(self, source_id: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
            (str(source_id), self.user_id),
        )

    def get_sources_by_ids(self, source_ids: Sequence[str], *, include_deleted: bool = False) -> list[VNextRow]:
        """The sources at ``source_ids``. A soft-deleted row is returned only with ``include_deleted``, which a reader
        that must tell a deleted source from one that never existed passes (the open-loop id filter)."""

        ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        if not ids:
            return []
        placeholders = self._placeholders(ids)
        deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
        return self._fetch_all(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE user_id = ?
                  {deleted_clause}
                  AND id IN ({placeholders})
                """,
            (self.user_id, *ids),
        )

    def get_source_by_content_hash(self, content_hash: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE content_hash = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
            (content_hash, self.user_id),
        )

    def get_sources_by_content_hash(self, content_hash: str) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE content_hash = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                """,
            (content_hash, self.user_id),
        )

    def get_source_by_dedupe_key(self, dedupe_key: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE dedupe_key = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
            (dedupe_key, self.user_id),
        )

    @takes_label_lock
    def update_source(
        self,
        *,
        source_id: str,
        patch: JsonObject,
        actor_type: str = "system",
    ) -> VNextRow:
        """Update a source and keep its live capture identity atomic."""

        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        current = self.get_source(source_id)
        if current is None:
            raise ContinuityStoreInvariantError("update_source did not return a row from the database")
        prospective = dict(current)
        for field in (
            "title",
            "author",
            "uri",
            "raw_path",
            "domain",
            "sensitivity",
            "metadata_json",
        ):
            if field in patch and patch[field] is not None:
                prospective[field] = patch[field]

        current_scope = project_scope_identity(source_project_scope(current))
        prospective_scope = project_scope_identity(source_project_scope(prospective))
        scope_changed = current_scope != prospective_scope
        identity_changed = not source_capture_identity_matches(
            current,
            content_hashes=(),
            project_scope=prospective_scope,
            domain=prospective.get("domain", "unknown"),
            sensitivity=prospective.get("sensitivity", "unknown"),
        )
        raw_text_changed = source_capture_raw_text(current) != source_capture_raw_text(prospective)
        dedupe_input_changed = identity_changed or raw_text_changed
        dedupe_key = capture_dedupe_key_for_source(prospective) if dedupe_input_changed else current.get("dedupe_key")
        content_input_changed = scope_changed or raw_text_changed
        content_hash = (
            capture_content_hash_for_source(prospective) or str(current["content_hash"])
            if content_input_changed
            else str(current["content_hash"])
        )
        if dedupe_key is not None and dedupe_key != current.get("dedupe_key"):
            owner = self._fetch_optional_one(
                """
                    SELECT id
                    FROM sources
                    WHERE user_id = ?
                      AND dedupe_key = ?
                      AND id <> ?
                      AND deleted_at IS NULL
                    LIMIT 1
                    """,
                (self.user_id, dedupe_key, str(source_id)),
            )
            if owner is not None:
                raise ContinuityStoreInvariantError("source capture identity already belongs to another live source")
        try:
            row = self._fetch_one(
                "update_source",
                f"""
                    UPDATE sources
                    SET title = COALESCE(?, title),
                        author = COALESCE(?, author),
                        uri = COALESCE(?, uri),
                        raw_path = COALESCE(?, raw_path),
                        domain = COALESCE(?, domain),
                        sensitivity = COALESCE(?, sensitivity),
                        metadata_json = COALESCE(?, metadata_json),
                        content_hash = ?,
                        dedupe_key = ?
                    WHERE id = ?
                      AND user_id = ?
                      AND deleted_at IS NULL
                    RETURNING {", ".join(SOURCE_COLUMNS)}
                    """,
                (
                    patch.get("title"),
                    patch.get("author"),
                    patch.get("uri"),
                    patch.get("raw_path"),
                    patch.get("domain"),
                    patch.get("sensitivity"),
                    _json_object_text(patch["metadata_json"]) if "metadata_json" in patch else None,
                    content_hash,
                    dedupe_key,
                    str(source_id),
                    self.user_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContinuityStoreInvariantError(
                "source capture identity already belongs to another live source"
            ) from exc
        self._append_mutation_event(
            event_type="source.updated",
            actor_type=actor_type,
            target_type="source",
            target_id=row["id"],
            payload={"operation": "update", "changes": patch},
        )
        from alicebot_api.vnext_label_writes import propagate_after_write

        propagate_after_write(self, kind="source", before=current, after=row)
        return row

    def create_source_chunk(self, chunk: JsonObject, *, actor_type: str = "system") -> VNextRow:
        chunk_id = _new_id(chunk.get("id"))
        self._execute(
            """
                INSERT INTO source_chunks (
                  id,
                  user_id,
                  source_id,
                  chunk_index,
                  text,
                  token_count,
                  metadata_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                chunk_id,
                self.user_id,
                _uuid_text(chunk["source_id"]),
                chunk["chunk_index"],
                chunk["text"],
                chunk.get("token_count"),
                _json_object_text(chunk.get("metadata_json")),
                _utc_now_iso(),
            ),
        )
        row = self._get_row("create_source_chunk", "source_chunks", SOURCE_CHUNK_COLUMNS, chunk_id)
        self._append_mutation_event(
            event_type="source_chunk.created",
            actor_type=actor_type,
            target_type="source_chunk",
            target_id=row["id"],
            payload={"operation": "create", "source_id": str(row["source_id"])},
        )
        return row

    def list_source_chunks(self, source_id: str) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {", ".join(SOURCE_CHUNK_COLUMNS)}
                FROM source_chunks
                WHERE source_id = ?
                  AND user_id = ?
                ORDER BY chunk_index ASC, id ASC
                """,
            (str(source_id), self.user_id),
        )

    def search_source_chunks(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 50,
        match_any: bool = False,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> list[VNextRow]:
        """Content search over source_chunks.text; best chunk hit first.

        Mirrors ``PostgresVNextStore.search_source_chunks``: rank comes
        back as ``fts_score`` on each chunk row (row order IS the rank),
        every row carries ``source_id``, and the domain/sensitivity gates
        are applied on the joined parent source row, like
        ``search_sources``. Strict pass ANDs every non-stopword term;
        ``match_any`` (the retrieval OR-fallback) ORs them instead. Both
        share ``search_memories_fts``'s sanitized MATCH builders, so FTS5
        metacharacters cannot inject query syntax.
        """
        match_expression = _fts_match_any_expression(query) if match_any else _fts_match_expression(query)
        if match_expression is None:
            return []
        domain_sql, domain_params = self._domain_clause(domains, prefix="s.")
        sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed, prefix="s.")
        scope_sql, scope_params = self._metadata_scope_clause(
            metadata_expression="s.metadata_json",
            persisted_source_envelope=True,
            scope_projects=scope_projects,
            scope_people=scope_people,
            event_time_expression=(
                "COALESCE(julianday(s.source_created_at), "
                "julianday(json_extract(s.metadata_json, '$.session_date')), "
                "julianday(json_extract(s.metadata_json, '$.event_date')), "
                "julianday(json_extract(s.metadata_json, '$.date')), "
                "julianday(s.captured_at))"
            ),
            scope_window_start=scope_window_start,
            scope_window_end=scope_window_end,
            # Stated, an empty tuple: a source is held back in Python (the brief's
            # ``_source_honours_fence``), not in this read, so it leaves nothing out itself.
            global_excluded_domains=(),
        )
        prefixed_columns = ", ".join(f"c.{column}" for column in SOURCE_CHUNK_COLUMNS)
        params: list[object] = [match_expression, self.user_id]
        params.extend(domain_params)
        params.extend(sensitivity_params)
        params.extend(scope_params)
        params.append(limit)
        try:
            return self._fetch_all(
                f"""
                    SELECT {prefixed_columns},
                      -bm25(source_chunks_fts) AS fts_score
                    FROM source_chunks_fts
                    JOIN source_chunks c ON c.rowid = source_chunks_fts.rowid
                    JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
                    WHERE source_chunks_fts MATCH ?
                      AND c.user_id = ?
                      AND s.deleted_at IS NULL{domain_sql}{sensitivity_sql}{scope_sql}
                    ORDER BY fts_score DESC, c.created_at DESC, c.id DESC
                    LIMIT ?
                    """,
                tuple(params),
            )
        except sqlite3.OperationalError as exc:  # pragma: no cover - sanitizer backstop
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                return []
            raise

    def check_source_search_query(self, query: str) -> None:
        """Refuse a query ``search_sources`` could not run, with a typed error.

        SQLite fails that search with ``Expression tree is too large`` or
        ``LIKE or GLOB pattern too complex``. The limits and how they were
        measured are in ``source_search_limits``. A caller that wants
        to refuse before it does other work calls this first; ``search_sources``
        calls it too, so no caller reaches SQLite with such a query. The
        Postgres store has no such limit and defines no such method.
        """

        require_source_search_query(query)

    def check_literal_match_query(self, query: str) -> None:
        """Refuse a query the literal substring reads could not run, with a typed error.

        ``list_memories``, ``list_open_loops``, ``list_open_loop_events`` and
        ``list_resume_memory_events`` bind a query as one LIKE operand, and
        SQLite fails them with ``LIKE or GLOB pattern too complex`` past 50,000
        bytes. Each of the four refuses such a query itself, through
        ``literal_match_operand``. A caller that wants the refusal before it does
        other work, and whatever the vault holds or the caller may read, calls
        this first. The Postgres store has no such limit and defines no such
        method.
        """

        require_literal_match_query(query)

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> list[VNextRow]:
        self.check_source_search_query(query)
        patterns = [pattern.casefold() for pattern in _search_patterns(query)]
        exact_pattern = patterns[0]
        domain_sql, domain_params = self._domain_clause(domains)
        sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
        scope_sql, scope_params = self._metadata_scope_clause(
            metadata_expression="metadata_json",
            persisted_source_envelope=True,
            scope_projects=scope_projects,
            scope_people=scope_people,
            event_time_expression=(
                "COALESCE(julianday(source_created_at), "
                "julianday(json_extract(metadata_json, '$.session_date')), "
                "julianday(json_extract(metadata_json, '$.event_date')), "
                "julianday(json_extract(metadata_json, '$.date')), "
                "julianday(captured_at))"
            ),
            scope_window_start=scope_window_start,
            scope_window_end=scope_window_end,
            # Stated, an empty tuple: see ``search_source_chunks``.
            global_excluded_domains=(),
        )
        count = len(patterns)
        match_columns = ("title", "author", "uri", "raw_path", "content_hash", "metadata_json")
        match_sql = " OR ".join(self._like_any(column, count) for column in match_columns)
        params: list[object] = [self.user_id]
        params.extend(domain_params)
        params.extend(sensitivity_params)
        params.extend(scope_params)
        for _column in match_columns:
            params.extend(patterns)
        params.append(exact_pattern)
        params.extend(patterns)
        params.append(limit)
        return self._fetch_all(
            f"""
                SELECT {", ".join(SOURCE_COLUMNS)}
                FROM sources
                WHERE user_id = ?
                  AND deleted_at IS NULL{domain_sql}{sensitivity_sql}{scope_sql}
                  AND ({match_sql})
                ORDER BY
                  CASE
                    WHEN LOWER(COALESCE(title, '')) LIKE ? THEN 0
                    WHEN {self._like_any("title", count)} THEN 1
                    ELSE 2
                  END,
                  captured_at DESC,
                  id DESC
                LIMIT ?
                """,
            tuple(params),
        )

    # -- memories -------------------------------------------------------------

    create_memory = _lifecycle_create_memory

    get_memory_by_key = _memory_get_memory_by_key

    upsert_memory_by_key = _lifecycle_upsert_memory_by_key

    get_memory = _memory_get_memory

    get_memories_by_ids = _memory_get_memories_by_ids

    list_memories_referencing_source = _memory_list_memories_referencing_source

    list_memories_referencing_sources = _memory_list_memories_referencing_sources

    get_memory_for_update = _lifecycle_get_memory_for_update

    get_memory_for_redaction = _lifecycle_get_memory_for_redaction

    lock_project_update_artifacts_for_redaction = _lifecycle_lock_project_update_artifacts_for_redaction

    memory_redaction_bundle_is_exact = _lifecycle_memory_redaction_bundle_is_exact

    list_pending_derived_candidates_for_member = _memory_list_pending_derived_candidates_for_member

    get_memory_by_commit_digest = _memory_get_memory_by_commit_digest

    latest_agentic_commit_memory = _memory_latest_agentic_commit_memory

    get_memory_by_confirmation_id = _memory_get_memory_by_confirmation_id

    list_memories = _memory_list_memories

    list_memories_view_partitions = _memory_list_memories_view_partitions

    list_memories_by_statuses = _memory_list_memories_by_statuses

    count_memories_by_status = _memory_count_memories_by_status

    list_recent_agentic_commits = _memory_list_recent_agentic_commits

    list_pending_inline_confirmations = _memory_list_pending_inline_confirmations

    find_live_memory_by_canonical_text = _memory_find_live_memory_by_canonical_text

    list_memories_for_staleness_sweep = _memory_list_memories_for_staleness_sweep

    count_memories = _memory_count_memories

    list_rollup_input_memories = _memory_list_rollup_input_memories

    count_rollup_input_memories = _memory_count_rollup_input_memories

    list_pending_rollup_candidates = _memory_list_pending_rollup_candidates

    list_accepted_rollup_cards = _memory_list_accepted_rollup_cards

    update_memory = _lifecycle_update_memory

    # -- memory search ---------------------------------------------------------

    search_memories = _memory_search_memories

    search_memories_fts = _memory_search_memories_fts

    search_memories_vector = _memory_search_memories_vector

    search_memories_by_time = _memory_search_memories_by_time

    update_memory_embedding = _update_memory_embedding
    clear_memory_embedding = _clear_memory_embedding
    list_memories_missing_embeddings = _list_memories_missing_embeddings

    lock_graph_mutation = _lifecycle_lock_graph_mutation

    list_memory_ids_with_embeddings = _lifecycle_list_memory_ids_with_embeddings

    update_memory_fact_keys = _lifecycle_update_memory_fact_keys

    list_memories_missing_fact_keys = _lifecycle_list_memories_missing_fact_keys

    # -- revisions ---------------------------------------------------------------

    append_revision = _events_append_revision
    list_revisions = _events_list_revisions

    # -- true redaction ------------------------------------------------------------
    #
    # Mirrors PostgresVNextStore: redaction expunges CONTENT while
    # preserving the audit SKELETON. SQLite has no session variables, so
    # the append-only triggers (sqlite_schema) consult the one-row
    # redaction_mode flag table instead of a Postgres session setting;
    # _redaction_mode flips it around the redaction statements and resets
    # it on every exit path.

    _redaction_mode = _lifecycle__redaction_mode

    redact_memory_bundle = _lifecycle_redact_memory_bundle

    redact_memory_content = _lifecycle_redact_memory_content

    redact_memory_revisions = _lifecycle_redact_memory_revisions

    redact_memory_events = _lifecycle_redact_memory_events

    # -- provenance ----------------------------------------------------------------

    create_provenance_link = _lifecycle_create_provenance_link

    list_provenance_links = _lifecycle_list_provenance_links

    list_provenance_links_for_targets = _lifecycle_list_provenance_links_for_targets

    # -- graph edges -----------------------------------------------------------------
    #
    # Minimal graph substrate for the on-ramp: create + list + as-of list.
    # Temporal semantics mirror PostgresVNextStore.create_edge:
    # ``observed_at`` is event time (when the connected observation
    # happened), defaulting to write time with the fallback noted in the
    # edge metadata; ``valid_from`` defaults to ``observed_at``.

    create_graph_edge = _graph_create_graph_edge

    list_edges = _graph_list_edges

    list_memory_entity_edges = _graph_list_memory_entity_edges

    expire_edge = _graph_expire_edge

    list_edges_as_of = _graph_list_edges_as_of

    # -- entities -----------------------------------------------------------
    #
    # Mirrors PostgresVNextStore's entity substrate (migration
    # 20260705_0078): one row per resolved real-world thing, keyed for
    # resolution by (entity_type, normalized_name). Entities participate
    # in the graph without edge changes: graph_edges.from_type/from_id
    # and to_type/to_id are free-text node references, so an edge can
    # point at an entity with from_type='entity', from_id=<entity id>;
    # only edge_type is CHECK-constrained.

    create_entity = _graph_create_entity

    get_entity = _graph_get_entity

    get_entity_by_normalized_name = _graph_get_entity_by_normalized_name

    find_entities_by_names = _graph_find_entities_by_names

    list_entities = _graph_list_entities

    update_entity = _graph_update_entity

    record_entity_mention = _graph_record_entity_mention

    record_relationship_change = _graph_record_relationship_change

    list_relationship_events = _graph_list_relationship_events

    # -- open loops -------------------------------------------------------------------

    create_open_loop = _graph_create_open_loop

    upsert_open_loop_by_automation_digest = _graph_upsert_open_loop_by_automation_digest

    get_open_loop = _graph_get_open_loop

    find_open_loop_by_automation_digest = _graph_find_open_loop_by_automation_digest

    list_open_loops_referencing_source = _graph_list_open_loops_referencing_source

    list_open_loops = _graph_list_open_loops

    list_open_loops_view_partitions = _graph_list_open_loops_view_partitions

    list_open_loop_events = _graph_list_open_loop_events

    update_open_loop = _graph_update_open_loop

    update_open_loop_status = _graph_update_open_loop_status

    # -- agent identities and API keys ---------------------------------------------

    def upsert_agent_identity(self, agent: JsonObject, *, actor_type: str = "agent") -> VNextRow:
        agent_id = str(agent["agent_id"])
        agent_type = agent.get("agent_type", "unknown")
        permission_profile = agent.get("permission_profile", "read_only_agent")
        display_name = agent.get("display_name")
        project_scope = _json_list_text(agent.get("project_scope_json") or agent.get("project_scope"))
        metadata = cast(JsonObject, json_safe(agent.get("metadata_json") or {}))
        existing = self._fetch_optional_one(
            f"""
                SELECT {", ".join(AGENT_IDENTITY_COLUMNS)}
                FROM agent_identities
                WHERE user_id = ?
                  AND agent_id = ?
                """,
            (self.user_id, agent_id),
        )
        if existing is None:
            identity_id = _new_id(agent.get("id"))
            now = _utc_now_iso()
            self._execute(
                """
                    INSERT INTO agent_identities (
                      id,
                      user_id,
                      agent_id,
                      agent_type,
                      permission_profile,
                      display_name,
                      project_scope_json,
                      metadata_json,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    identity_id,
                    self.user_id,
                    agent_id,
                    agent_type,
                    permission_profile,
                    display_name,
                    project_scope,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        else:
            identity_id = str(existing["id"])
            existing_metadata = cast(dict[str, object], existing.get("metadata_json") or {})
            # Shallow merge mirrors Postgres jsonb `||` semantics.
            merged_metadata = {**existing_metadata, **metadata}
            self._execute(
                """
                    UPDATE agent_identities
                    SET agent_type = ?,
                        permission_profile = ?,
                        display_name = COALESCE(?, display_name),
                        project_scope_json = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE user_id = ?
                      AND agent_id = ?
                    """,
                (
                    agent_type,
                    permission_profile,
                    display_name,
                    project_scope,
                    json.dumps(merged_metadata, ensure_ascii=False, separators=(",", ":")),
                    _utc_now_iso(),
                    self.user_id,
                    agent_id,
                ),
            )
        row = self._get_row("upsert_agent_identity", "agent_identities", AGENT_IDENTITY_COLUMNS, identity_id)
        self._append_mutation_event(
            event_type="agent.identity_upserted",
            actor_type=actor_type,
            actor_id=str(row["agent_id"]),
            target_type="agent_identity",
            target_id=row["id"],
            payload={"operation": "upsert", "agent_id": str(row["agent_id"])},
        )
        return row

    def list_agent_identities(self, *, limit: int = 20) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {", ".join(AGENT_IDENTITY_COLUMNS)}
                FROM agent_identities
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
            (self.user_id, limit),
        )

    def list_agent_events(self, *, agent_id: str | None = None, limit: int = 50) -> list[VNextRow]:
        clauses = ["user_id = ?", "actor_type = 'agent'"]
        params: list[object] = [self.user_id]
        if agent_id is not None:
            clauses.append("actor_id = ?")
            params.append(agent_id)
        params.append(limit)
        return self._fetch_all(
            f"""
                SELECT {", ".join(EVENT_LOG_COLUMNS)}
                FROM event_log
                WHERE {" AND ".join(clauses)}
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
            tuple(params),
        )

    def list_agent_policy_artifacts(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[VNextRow]:
        """SQLite has no generated-artifact table in the local core."""

        if limit < 1:
            raise ValueError("limit must be positive")
        return []

    def list_agent_policy_memories(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[VNextRow]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return self._fetch_all(
            f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND json_extract(metadata_json, '$.agent_id') IS NOT NULL
                  AND (? IS NULL OR json_extract(metadata_json, '$.agent_id') = ?)
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
            (self.user_id, agent_id, agent_id, limit),
        )

    def create_agent_api_key(self, key: JsonObject, *, actor_type: str = "user") -> VNextRow:
        key_id = _new_id(key.get("id"))
        self._execute(
            """
                INSERT INTO agent_api_keys (
                  id,
                  user_id,
                  agent_id,
                  permission_profile,
                  project_scope,
                  key_hash,
                  key_prefix,
                  label,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                key_id,
                self.user_id,
                key["agent_id"],
                key["permission_profile"],
                key.get("project_scope"),
                key["key_hash"],
                key["key_prefix"],
                key.get("label"),
                _utc_now_iso(),
            ),
        )
        row = self._get_row("create_agent_api_key", "agent_api_keys", AGENT_API_KEY_COLUMNS, key_id)
        self._append_mutation_event(
            event_type="agent.key_created",
            actor_type=actor_type,
            target_type="agent_api_key",
            target_id=row["id"],
            payload={
                "operation": "create",
                "agent_id": str(row["agent_id"]),
                "permission_profile": str(row["permission_profile"]),
                "project_scope": row.get("project_scope"),
                "key_prefix": str(row["key_prefix"]),
                "label": row.get("label"),
            },
        )
        return row

    def get_agent_api_key_by_hash(self, key_hash: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {", ".join(AGENT_API_KEY_COLUMNS)}
                FROM agent_api_keys
                WHERE key_hash = ?
                  AND user_id = ?
                """,
            (key_hash, self.user_id),
        )

    def list_agent_api_keys(self, *, limit: int = 50) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {", ".join(AGENT_API_KEY_COLUMNS)}
                FROM agent_api_keys
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
            (self.user_id, limit),
        )

    def revoke_agent_api_key(self, *, key_id: str, actor_type: str = "user") -> VNextRow | None:
        cursor = self._execute(
            """
                UPDATE agent_api_keys
                SET revoked_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND revoked_at IS NULL
                """,
            (_utc_now_iso(), str(key_id), self.user_id),
        )
        if cursor.rowcount == 0:
            return None
        row = self._get_row("revoke_agent_api_key", "agent_api_keys", AGENT_API_KEY_COLUMNS, str(key_id))
        self._append_mutation_event(
            event_type="agent.key_revoked",
            actor_type=actor_type,
            target_type="agent_api_key",
            target_id=row["id"],
            payload={
                "operation": "revoke",
                "agent_id": str(row["agent_id"]),
                "permission_profile": str(row["permission_profile"]),
                "key_prefix": str(row["key_prefix"]),
            },
        )
        return row

    def touch_agent_api_key(self, *, key_id: str) -> VNextRow:
        cursor = self._execute(
            """
                UPDATE agent_api_keys
                SET last_used_at = ?
                WHERE id = ?
                  AND user_id = ?
                """,
            (_utc_now_iso(), str(key_id), self.user_id),
        )
        if cursor.rowcount == 0:
            raise ContinuityStoreInvariantError(
                "touch_agent_api_key did not return a row from the database",
            )
        return self._get_row("touch_agent_api_key", "agent_api_keys", AGENT_API_KEY_COLUMNS, str(key_id))

    def count_active_agent_api_keys(self) -> int:
        row = self._fetch_one(
            "count_active_agent_api_keys",
            """
                SELECT count(*) AS active_count
                FROM agent_api_keys
                WHERE revoked_at IS NULL
                  AND user_id = ?
                """,
            (self.user_id,),
        )
        return int(cast(int, row["active_count"]))

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """Run one unit of writes that lands whole or leaves no row.

        An exception rolls back everything written inside the block, including
        the events it appended, and is re-raised for the caller to report. The
        enclosing transaction stays open and commits once, when its owner
        commits.

        Python's ``sqlite3`` opens its transaction at the first write, so a
        savepoint issued before any write would be the outermost one, and its
        release would commit. The transaction is therefore begun here when none
        is open, the way ``update_source`` begins its own. A connection that
        commits every statement on its own (``isolation_level`` of ``None``, or
        ``autocommit=True``) keeps doing so: there the release commits the
        block, which is what that connection means by a commit.
        """

        conn = self.conn
        legacy_transaction_control = getattr(conn, "autocommit", -1) == -1
        if legacy_transaction_control and conn.isolation_level is not None and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        name = f"alice_savepoint_{next(_SAVEPOINT_NAMES)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                conn.execute(f"RELEASE SAVEPOINT {name}")
            except sqlite3.Error:
                # SQLite ended the whole transaction itself (a full disk is one
                # case), so there is no savepoint left to roll back to. The
                # caller's error is the one to report, not this one.
                pass
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")


__all__ = [
    "REDACTION_MARKER",
    "SQLiteVNextStore",
    "ensure_sqlite_user",
    "sqlite_user_connection",
]
