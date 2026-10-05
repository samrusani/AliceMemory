from __future__ import annotations

import itertools
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import psycopg

from alicebot_api.db import UserConnection
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_capture import (
    capture_content_hash_for_source,
    capture_dedupe_key_for_source,
    source_capture_raw_text,
)
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    memory_embedding_signature_is_current,
)
from alicebot_api.vnext_entity_names import ENTITY_IMMUTABLE_PATCH_FIELDS, normalize_entity_name
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_label_writes import takes_label_lock
from alicebot_api.vnext_project_scope import (
    expose_memory_project_scope,
    project_scope_identity,
    source_capture_identity_matches,
    source_project_scope,
)
from alicebot_api.vnext_recall_visibility import postgres_unexpired_sql
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.memory_lifecycle_common import (
    PRIOR_REDACTED_MEMORY_METADATA_KEYS as PRIOR_REDACTED_MEMORY_METADATA_KEYS,
    PROJECT_UPDATE_REDACTED_METADATA_KEYS as PROJECT_UPDATE_REDACTED_METADATA_KEYS,
    REDACTED_JSON_VALUE as REDACTED_JSON_VALUE,
    REDACTED_MEMORY_METADATA_KEYS as REDACTED_MEMORY_METADATA_KEYS,
    REDACTION_MARKER as REDACTION_MARKER,
    _is_redacted_memory_shape as _is_redacted_memory_shape,
    is_prior_redacted_memory_marker as is_prior_redacted_memory_marker,
    is_redacted_memory as is_redacted_memory,
    is_redacted_project_update_artifact as is_redacted_project_update_artifact,
    redacted_memory_metadata as redacted_memory_metadata,
)
from alicebot_api.vnext_stores.postgres.columns import (
    ARTIFACT_COLUMNS as ARTIFACT_COLUMNS,
    BELIEF_COLUMNS as BELIEF_COLUMNS,
    ENTITY_COLUMNS as ENTITY_COLUMNS,
    ENTITY_RELATIONSHIP_EVENT_COLUMNS as ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    EVENT_LOG_COLUMNS as EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS as GRAPH_EDGE_COLUMNS,
    MEMORY_COLUMNS as MEMORY_COLUMNS,
    OPEN_LOOP_COLUMNS as OPEN_LOOP_COLUMNS,
    PROVENANCE_COLUMNS as PROVENANCE_COLUMNS,
    REVISION_COLUMNS as REVISION_COLUMNS,
)
from alicebot_api.vnext_stores.postgres.browser_clip_capabilities import (
    consume_browser_clip_capability as _browser_clip_consume_capability,
    create_browser_clip_capability as _browser_clip_create_capability,
)
from alicebot_api.vnext_stores.postgres.embedding_cas import (
    _MEMORY_EMBEDDING_CONTENT_SHA256_SQL as _MEMORY_EMBEDDING_CONTENT_SHA256_SQL,
    _PYTHON_312_STRIP_CHARS_SQL as _PYTHON_312_STRIP_CHARS_SQL,
    _PYTHON_312_STRIP_CODEPOINTS as _PYTHON_312_STRIP_CODEPOINTS,
    _python_312_strip_sql as _python_312_strip_sql,
    _vector_literal as _vector_literal,
    clear_memory_embedding as _clear_memory_embedding,
    list_memories_missing_embeddings as _list_memories_missing_embeddings,
    update_memory_embedding as _update_memory_embedding,
)
from alicebot_api.vnext_stores.postgres.events_revisions import (
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
from alicebot_api.vnext_stores.postgres.graph_open_loops import (
    create_edge as _graph_create_edge,
    find_edge_by_idempotency_digest as _graph_find_edge_by_idempotency_digest,
    upsert_edge_by_idempotency_digest as _graph_upsert_edge_by_idempotency_digest,
    list_edges as _graph_list_edges,
    list_memory_entity_edges as _graph_list_memory_entity_edges,
    list_edges_as_of as _graph_list_edges_as_of,
    update_edge_status as _graph_update_edge_status,
    expire_edge as _graph_expire_edge,
    create_entity as _graph_create_entity,
    get_entity as _graph_get_entity,
    get_entity_by_normalized_name as _graph_get_entity_by_normalized_name,
    find_entities_by_names as _graph_find_entities_by_names,
    list_entities as _graph_list_entities,
    update_entity as _graph_update_entity,
    record_entity_mention as _graph_record_entity_mention,
    record_relationship_change as _graph_record_relationship_change,
    list_relationship_events as _graph_list_relationship_events,
    create_belief as _graph_create_belief,
    get_belief as _graph_get_belief,
    list_beliefs as _graph_list_beliefs,
    update_belief_status as _graph_update_belief_status,
    create_open_loop as _graph_create_open_loop,
    upsert_open_loop_by_automation_digest as _graph_upsert_open_loop_by_automation_digest,
    get_open_loop as _graph_get_open_loop,
    find_open_loop_by_automation_digest as _graph_find_open_loop_by_automation_digest,
    list_open_loops_referencing_source as _graph_list_open_loops_referencing_source,
    list_open_loops as _graph_list_open_loops,
    list_open_loop_events as _graph_list_open_loop_events,
    update_open_loop as _graph_update_open_loop,
    update_open_loop_status as _graph_update_open_loop_status,
)
from alicebot_api.vnext_stores.postgres.memory_lifecycle import (
    create_memory as _lifecycle_create_memory,
    upsert_memory_by_key as _lifecycle_upsert_memory_by_key,
    get_memory_for_update as _lifecycle_get_memory_for_update,
    get_memory_for_redaction as _lifecycle_get_memory_for_redaction,
    lock_project_update_artifacts_for_redaction as _lifecycle_lock_project_update_artifacts_for_redaction,
    memory_redaction_bundle_is_exact as _lifecycle_memory_redaction_bundle_is_exact,
    lock_graph_mutation as _lifecycle_lock_graph_mutation,
    list_memory_ids_with_embeddings as _lifecycle_list_memory_ids_with_embeddings,
    update_memory_fact_keys as _lifecycle_update_memory_fact_keys,
    list_memories_missing_fact_keys as _lifecycle_list_memories_missing_fact_keys,
    update_memory as _lifecycle_update_memory,
    _redaction_mode as _lifecycle__redaction_mode,
    redact_memory_bundle as _lifecycle_redact_memory_bundle,
    redact_memory_content as _lifecycle_redact_memory_content,
    redact_memory_revisions as _lifecycle_redact_memory_revisions,
    redact_memory_events as _lifecycle_redact_memory_events,
    create_provenance_link as _lifecycle_create_provenance_link,
    list_provenance_links as _lifecycle_list_provenance_links,
    list_provenance_links_for_targets as _lifecycle_list_provenance_links_for_targets,
)
from alicebot_api.vnext_stores.postgres.memory_access import (
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
from alicebot_api.vnext_stores.postgres.primitives import (
    _json_list as _json_list,
    _json_object as _json_object,
    _json_safe as _json_safe,
    _sorted_field_names as _sorted_field_names,
)
from alicebot_api.vnext_stores.postgres.query_predicates import (
    _ARTIFACT_SCOPE_PROJECT_SQL as _ARTIFACT_SCOPE_PROJECT_SQL,
    _PROJECT_FLOOR_SQL as _PROJECT_FLOOR_SQL,
    _MEMORY_GROUP_SCOPE_SQL as _MEMORY_GROUP_SCOPE_SQL,
    _jsonb_string_array_identity_sql as _jsonb_string_array_identity_sql,
    _ASCII_PROJECT_LOWER as _ASCII_PROJECT_LOWER,
    _ASCII_PROJECT_UPPER as _ASCII_PROJECT_UPPER,
    _MEMORY_DIRECT_PEOPLE_SQL as _MEMORY_DIRECT_PEOPLE_SQL,
    _MEMORY_PROJECT_SCOPE_SQL as _MEMORY_PROJECT_SCOPE_SQL,
    _MEMORY_SCOPE_EVENT_TIME_SQL as _MEMORY_SCOPE_EVENT_TIME_SQL,
    _OPEN_LOOP_SCOPE_EVENT_TIME_SQL as _OPEN_LOOP_SCOPE_EVENT_TIME_SQL,
    _OPEN_LOOP_SCOPE_PEOPLE_SQL as _OPEN_LOOP_SCOPE_PEOPLE_SQL,
    _OPEN_LOOP_SCOPE_PROJECT_SQL as _OPEN_LOOP_SCOPE_PROJECT_SQL,
    _PROJECT_ASCII_WHITESPACE_PATTERN_SQL as _PROJECT_ASCII_WHITESPACE_PATTERN_SQL,
    _SCOPED_MEMORY_DIRECT_PEOPLE_SQL as _SCOPED_MEMORY_DIRECT_PEOPLE_SQL,
    _SCOPED_MEMORY_EVENT_TIME_SQL as _SCOPED_MEMORY_EVENT_TIME_SQL,
    _SCOPED_MEMORY_PROJECT_SQL as _SCOPED_MEMORY_PROJECT_SQL,
    _SOURCE_SCOPE_EVENT_TIME_SQL as _SOURCE_SCOPE_EVENT_TIME_SQL,
    _SOURCE_SCOPE_PEOPLE_SQL as _SOURCE_SCOPE_PEOPLE_SQL,
    _SOURCE_SCOPE_PROJECT_SQL as _SOURCE_SCOPE_PROJECT_SQL,
    _escape_like_literal as _escape_like_literal,
    _jsonb_project_scope_leaf_values_sql as _jsonb_project_scope_leaf_values_sql,
    _jsonb_project_scope_values_sql as _jsonb_project_scope_values_sql,
    _jsonb_scope_values_sql as _jsonb_scope_values_sql,
    _jsonb_source_project_scope_values_sql as _jsonb_source_project_scope_values_sql,
    _normalized_project_identifier_sql as _normalized_project_identifier_sql,
    _postgres_ascii_literal_contains_sql as _postgres_ascii_literal_contains_sql,
    _project_identifier_identity_sql as _project_identifier_identity_sql,
    _tsquery_any_expression as _tsquery_any_expression,
)
from alicebot_api.vnext_stores.retrieval_common import (
    FTS_QUERY_STOPWORDS as FTS_QUERY_STOPWORDS,
    _search_patterns as _search_patterns,
    fts_fallback_tokens as fts_fallback_tokens,
)


JsonList = list[object]
VNextRow = dict[str, object]
MAX_SOURCE_CHUNKS_PER_READ = 501
# Names for nested savepoints. One counter for the process, so a nested block
# never reuses its parent's name.
_SAVEPOINT_NAMES = itertools.count(1)



CONNECTOR_SETTINGS_COLUMNS = """
                  id,
                  user_id,
                  connector_name,
                  enabled,
                  configured,
                  default_domain,
                  default_sensitivity,
                  sync_mode,
                  poll_interval_seconds,
                  secret_ref,
                  validation_errors_json,
                  metadata_json,
                  created_at,
                  updated_at,
                  last_configured_at
                """

CONNECTOR_STATE_COLUMNS = """
                  id,
                  user_id,
                  connector_id,
                  connector_name,
                  cursor_type,
                  cursor_value,
                  last_sync_at,
                  last_success_at,
                  last_failure_at,
                  last_error,
                  items_seen,
                  items_captured,
                  items_deduped,
                  items_failed,
                  average_processing_time_ms,
                  state_json,
                  updated_at
                """

SOURCE_COLUMNS = """
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
                  metadata_json,
                  deleted_at
                """

SOURCE_CHUNK_COLUMNS = """
                  id,
                  user_id,
                  source_id,
                  chunk_index,
                  text,
                  token_count,
                  metadata_json,
                  created_at
                """

# c.-prefixed chunk columns for search_source_chunks, whose JOIN to
# sources would otherwise make id/user_id/metadata_json/created_at
# ambiguous.
_SOURCE_CHUNK_SEARCH_COLUMNS = ", ".join(f"c.{column.strip()}" for column in SOURCE_CHUNK_COLUMNS.split(","))




PROJECT_COLUMNS = """
                  id,
                  user_id,
                  name,
                  slug,
                  status,
                  description,
                  current_state,
                  domain,
                  sensitivity,
                  created_at,
                  updated_at,
                  metadata_json
                """

PERSON_COLUMNS = """
                  id,
                  user_id,
                  name,
                  aliases_json,
                  relationship_type,
                  organization,
                  sensitivity,
                  notes,
                  created_at,
                  updated_at,
                  metadata_json
                """






QUALITY_RATING_COLUMNS = """
                  id,
                  user_id,
                  artifact_id,
                  reviewer_id,
                  usefulness,
                  accuracy,
                  source_grounding,
                  novel_connections,
                  actionability,
                  hallucination_risk,
                  verbosity,
                  missed_context,
                  comments,
                  created_at,
                  metadata_json
                """

TASK_COLUMNS = """
                  id,
                  user_id,
                  title,
                  task_type,
                  instructions,
                  status,
                  requested_by,
                  scope_json,
                  allowed_sources_json,
                  domain,
                  sensitivity,
                  write_policy,
                  scheduled_for,
                  started_at,
                  completed_at,
                  failed_at,
                  error_message,
                  output_artifact_id,
                  created_at,
                  updated_at,
                  metadata_json
                """

BRAIN_CHARTER_COLUMNS = """
                  id,
                  user_id,
                  content_markdown,
                  owner_json,
                  memory_philosophy_json,
                  life_domains_json,
                  active_projects_json,
                  communication_style_json,
                  priorities_json,
                  autonomous_rules_json,
                  quality_standard_json,
                  sensitivity,
                  created_at,
                  updated_at
                """

AGENT_IDENTITY_COLUMNS = """
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
                """

AGENT_API_KEY_COLUMNS = """
                  id,
                  user_id,
                  agent_id,
                  permission_profile,
                  project_scope,
                  key_hash,
                  key_prefix,
                  label,
                  created_at,
                  revoked_at,
                  last_used_at
                """

SCHEDULER_WORKFLOW_COLUMNS = """
                  id,
                  user_id,
                  workflow_type,
                  enabled,
                  paused,
                  schedule_json,
                  timezone,
                  next_run_at,
                  last_run_id,
                  last_run_at,
                  last_result,
                  last_error,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  created_at,
                  updated_at,
                  metadata_json
                """

SCHEDULER_RUN_COLUMNS = """
                  id,
                  user_id,
                  workflow_id,
                  workflow_type,
                  status,
                  triggered_by,
                  trace_id,
                  started_at,
                  finished_at,
                  artifact_id,
                  error_message,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  scheduled_for,
                  policy_decision_json,
                  agent_identity_json,
                  metadata_json
                """










class PostgresVNextStore:
    """SQL-backed vNext repository facade for the second-brain kernel."""

    def __init__(self, conn: UserConnection):
        self.conn = conn

    def lock_label_writes(self, *, exclusive: bool = False) -> None:
        """Shared label lock for a write, or the exclusive lock for a relabel.

        The lock is transaction scoped. A session that already holds it takes
        the same statement again, which Postgres grants without waiting.
        """

        mode = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {mode}(hashtext('vnext_labels'), hashtext(app.current_user_id()::text))"
            )

    def read_label_rows(self, kind: str, ids: Sequence[str]) -> list[VNextRow]:
        """Narrow label rows for the insert floor. No text columns."""

        wanted = [str(item) for item in ids if str(item)]
        if not wanted:
            return []
        table = {
            "source": "sources",
            "memory": "memories",
            "open_loop": "open_loops",
            "artifact": "generated_artifacts",
            "project": "projects",
            "belief": "beliefs",
        }.get(kind)
        if table is None:
            return []
        if table == "beliefs":
            return self._fetch_all(
                """SELECT b.id, b.user_id, m.domain, m.sensitivity, b.metadata_json, b.memory_id
                   FROM beliefs b JOIN memories m ON m.id = b.memory_id AND m.user_id = b.user_id
                   WHERE b.id = ANY(%s::uuid[])""",
                (wanted,),
            )
        extra = ""
        if table == "memories":
            extra = ", value, project_id"
        elif table == "open_loops":
            extra = ", project_id, source_id, memory_id"
        elif table == "beliefs":
            extra = ", memory_id"
        elif table == "generated_artifacts":
            extra = ", artifact_type"
        return self._fetch_all(
            f"""
                SELECT id, user_id, domain, sensitivity, metadata_json{extra}
                FROM {table}
                WHERE id = ANY(%s::uuid[])
                """,
            (wanted,),
        )

    def _fetch_one(
        self,
        operation_name: str,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> VNextRow:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

        if row is None:
            raise ContinuityStoreInvariantError(
                f"{operation_name} did not return a row from the database",
            )

        return expose_memory_project_scope(cast(VNextRow, row))

    def _fetch_optional_one(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> VNextRow | None:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        if row is None:
            return None
        return expose_memory_project_scope(cast(VNextRow, row))

    def _fetch_all(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> list[VNextRow]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [expose_memory_project_scope(cast(VNextRow, row)) for row in rows]

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
        domains: list[str] | None,
        sensitivity_allowed: list[str] | None,
    ) -> list[VNextRow]:
        """Return events joined to resume-admitted memories before LIMIT.

        A memory is admitted when its status is in ``statuses`` and its
        ``valid_to`` has not passed (the test recall uses), so the event of an
        expired memory is not listed and does not use up the ``LIMIT``.

        ``domains`` and ``sensitivity_allowed`` have no defaults, as on the SQLite
        store, where they fence the read before ``LIMIT``. The Postgres runtime
        resolves no project view and has no such read, so only ``None`` is accepted
        and anything else raises instead of being ignored.
        """

        if domains is not None or sensitivity_allowed is not None:
            raise ValueError("the Postgres event reads take no domain or sensitivity fence; pass None")
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return []
        project_list = list(project_scope_identity(projects or ())) or None
        normalized_query = str(query).strip() if query is not None else None
        if normalized_query == "":
            normalized_query = None
        escaped_query = _escape_like_literal(normalized_query) if normalized_query is not None else None
        qualified_columns = ", ".join(f"event.{column.strip()}" for column in EVENT_LOG_COLUMNS.split(","))
        return self._fetch_all(
            f"""
                SELECT {qualified_columns}
                FROM event_log AS event
                JOIN memories AS m
                  ON event.target_type = 'memory'
                 AND event.target_id = m.id::text
                 AND event.user_id = m.user_id
                WHERE m.deleted_at IS NULL
                  AND m.status = ANY(%s::text[])
                  AND {postgres_unexpired_sql("m.")}
                  AND (
                    %s::text[] IS NULL
                    OR ({_SCOPED_MEMORY_PROJECT_SQL}) ?| %s::text[]
                  )
                  AND (
                    %s::text IS NULL
                    OR {_postgres_ascii_literal_contains_sql("COALESCE(m.title, '')")}
                    OR {_postgres_ascii_literal_contains_sql("COALESCE(m.canonical_text, '')")}
                    OR {_postgres_ascii_literal_contains_sql("COALESCE(m.summary, '')")}
                  )
                  AND (%s::timestamptz IS NULL OR event.occurred_at >= %s::timestamptz)
                  AND (%s::timestamptz IS NULL OR event.occurred_at <= %s::timestamptz)
                ORDER BY event.occurred_at DESC, event.id DESC
                LIMIT %s
                """,
            (
                normalized_statuses,
                project_list,
                project_list,
                escaped_query,
                escaped_query,
                escaped_query,
                escaped_query,
                occurred_at_start,
                occurred_at_start,
                occurred_at_end,
                occurred_at_end,
                limit,
            ),
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
        """Return memory-targeted events with target scope applied pre-LIMIT."""
        if limit < 1:
            raise ValueError("limit must be positive")
        project_list = list(project_scope_identity(scope_projects)) or None
        people_list = [str(value).strip().casefold() for value in scope_people if str(value).strip()] or None
        person_memory_ids = [str(value) for value in scope_person_memory_ids if str(value)] or None
        prefix_pattern = f"{event_type_prefix}%" if event_type_prefix is not None else None
        return self._fetch_all(
            f"""
                SELECT
                  e.id,
                  e.user_id,
                  e.event_type,
                  e.actor_type,
                  e.actor_id,
                  e.target_type,
                  e.target_id,
                  e.occurred_at,
                  e.payload_json,
                  e.trace_id,
                  e.run_id,
                  e.integrity_hash
                FROM event_log e
                JOIN memories m
                  ON e.target_type = 'memory'
                 AND e.target_id = m.id::text
                 AND e.user_id = m.user_id
                WHERE m.deleted_at IS NULL
                  AND (%s::text IS NULL OR e.event_type LIKE %s)
                  AND (%s::text[] IS NULL OR ({_SCOPED_MEMORY_PROJECT_SQL}) ?| %s::text[])
                  AND (
                    %s::text[] IS NULL
                    OR m.id::text = ANY(%s::text[])
                    OR {_SCOPED_MEMORY_DIRECT_PEOPLE_SQL}
                  )
                  AND (%s::timestamptz IS NULL OR e.occurred_at >= %s::timestamptz)
                  AND (%s::timestamptz IS NULL OR e.occurred_at <= %s::timestamptz)
                ORDER BY e.occurred_at DESC, e.id DESC
                LIMIT %s
                """,
            (
                prefix_pattern,
                prefix_pattern,
                project_list,
                project_list,
                people_list,
                person_memory_ids,
                people_list,
                scope_window_start,
                scope_window_start,
                scope_window_end,
                scope_window_end,
                limit,
            ),
        )

    count_events = _events_count_events

    def count_sources(self) -> int:
        row = self._fetch_one("count sources", "SELECT COUNT(*)::bigint AS count FROM sources WHERE deleted_at IS NULL")
        return int(cast(int, row["count"]))

    def count_artifacts(self) -> int:
        row = self._fetch_one("count artifacts", "SELECT COUNT(*)::bigint AS count FROM generated_artifacts")
        return int(cast(int, row["count"]))

    def count_artifacts_by_status(self) -> dict[str, int]:
        rows = self._fetch_all(
            "SELECT status, COUNT(*)::bigint AS count FROM generated_artifacts GROUP BY status ORDER BY status"
        )
        return {str(row["status"]): int(cast(int, row["count"])) for row in rows}

    def count_artifact_quality_ratings(self) -> int:
        row = self._fetch_one(
            "count artifact quality ratings",
            "SELECT COUNT(*)::bigint AS count FROM artifact_quality_ratings",
        )
        return int(cast(int, row["count"]))

    def count_projects(self) -> int:
        row = self._fetch_one("count projects", "SELECT COUNT(*)::bigint AS count FROM projects")
        return int(cast(int, row["count"]))

    def count_open_loops(self, *, status: str | None = None) -> int:
        row = self._fetch_one(
            "count open loops",
            "SELECT COUNT(*)::bigint AS count FROM open_loops WHERE (%s::text IS NULL OR status = %s)",
            (status, status),
        )
        return int(cast(int, row["count"]))

    def count_open_loops_by_status(self) -> dict[str, int]:
        rows = self._fetch_all(
            "SELECT status, COUNT(*)::bigint AS count FROM open_loops GROUP BY status ORDER BY status"
        )
        return {str(row["status"]): int(cast(int, row["count"])) for row in rows}

    def count_agent_identities(self) -> int:
        row = self._fetch_one(
            "count agent identities",
            "SELECT COUNT(*)::bigint AS count FROM agent_identities",
        )
        return int(cast(int, row["count"]))

    def list_connector_settings(self) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {CONNECTOR_SETTINGS_COLUMNS}
                FROM connector_settings
                ORDER BY connector_name ASC
            """
        )

    def get_connector_setting(self, connector_name: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {CONNECTOR_SETTINGS_COLUMNS}
                FROM connector_settings
                WHERE connector_name = %s
                """,
            (connector_name,),
        )

    def upsert_connector_setting(self, setting: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "upsert_connector_setting",
            f"""
                INSERT INTO connector_settings (
                  user_id,
                  connector_name,
                  enabled,
                  configured,
                  default_domain,
                  default_sensitivity,
                  sync_mode,
                  poll_interval_seconds,
                  secret_ref,
                  validation_errors_json,
                  metadata_json,
                  last_configured_at
                )
                VALUES (
                  app.current_user_id(),
                  %s,
                  COALESCE(%s, false),
                  COALESCE(%s, false),
                  %s,
                  %s,
                  COALESCE(%s, 'manual'),
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp())
                )
                ON CONFLICT (user_id, connector_name)
                DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  configured = EXCLUDED.configured,
                  default_domain = EXCLUDED.default_domain,
                  default_sensitivity = EXCLUDED.default_sensitivity,
                  sync_mode = EXCLUDED.sync_mode,
                  poll_interval_seconds = EXCLUDED.poll_interval_seconds,
                  secret_ref = COALESCE(EXCLUDED.secret_ref, connector_settings.secret_ref),
                  validation_errors_json = EXCLUDED.validation_errors_json,
                  metadata_json = connector_settings.metadata_json || EXCLUDED.metadata_json,
                  updated_at = clock_timestamp(),
                  last_configured_at = EXCLUDED.last_configured_at
                RETURNING {CONNECTOR_SETTINGS_COLUMNS}
                """,
            (
                setting["connector_name"],
                setting.get("enabled"),
                setting.get("configured"),
                setting["default_domain"],
                setting["default_sensitivity"],
                setting.get("sync_mode", "manual"),
                setting.get("poll_interval_seconds"),
                setting.get("secret_ref"),
                _json_list(setting.get("validation_errors_json")),
                _json_object(setting.get("metadata_json")),
                setting.get("last_configured_at"),
            ),
        )
        self._append_mutation_event(
            event_type="connector.settings_updated",
            actor_type=actor_type,
            target_type="connector",
            target_id=row["connector_name"],
            payload={
                "connector_id": row["id"],
                "connector_name": row["connector_name"],
                "enabled": row["enabled"],
                "configured": row["configured"],
                "default_domain": row["default_domain"],
                "default_sensitivity": row["default_sensitivity"],
                "sync_mode": row["sync_mode"],
                "poll_interval_seconds": row["poll_interval_seconds"],
                "secret_ref": row["secret_ref"],
                "validation_errors_json": row["validation_errors_json"],
            },
        )
        return row

    def list_connector_states(self) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {CONNECTOR_STATE_COLUMNS}
                FROM connector_state
                ORDER BY connector_name ASC, cursor_type ASC
            """
        )

    def get_connector_state(self, connector_name: str, *, cursor_type: str = "sync_cursor") -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {CONNECTOR_STATE_COLUMNS}
                FROM connector_state
                WHERE connector_name = %s
                  AND cursor_type = %s
                """,
            (connector_name, cursor_type),
        )

    def upsert_connector_state(self, state: JsonObject, *, actor_type: str = "system") -> VNextRow:
        connector_name = str(state["connector_name"])
        cursor_type = str(state.get("cursor_type") or "sync_cursor")
        row = self._fetch_one(
            "upsert_connector_state",
            f"""
                INSERT INTO connector_state (
                  user_id,
                  connector_id,
                  connector_name,
                  cursor_type,
                  cursor_value,
                  last_sync_at,
                  last_success_at,
                  last_failure_at,
                  last_error,
                  items_seen,
                  items_captured,
                  items_deduped,
                  items_failed,
                  average_processing_time_ms,
                  state_json
                )
                VALUES (
                  app.current_user_id(),
                  (
                    SELECT id
                    FROM connector_settings
                    WHERE user_id = app.current_user_id()
                      AND connector_name = %s
                    LIMIT 1
                  ),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s, 0),
                  COALESCE(%s, 0),
                  COALESCE(%s, 0),
                  COALESCE(%s, 0),
                  %s,
                  %s
                )
                ON CONFLICT (user_id, connector_name, cursor_type)
                DO UPDATE SET
                  connector_id = COALESCE(connector_state.connector_id, EXCLUDED.connector_id),
                  cursor_value = COALESCE(EXCLUDED.cursor_value, connector_state.cursor_value),
                  last_sync_at = COALESCE(EXCLUDED.last_sync_at, connector_state.last_sync_at),
                  last_success_at = COALESCE(EXCLUDED.last_success_at, connector_state.last_success_at),
                  last_failure_at = COALESCE(EXCLUDED.last_failure_at, connector_state.last_failure_at),
                  last_error = EXCLUDED.last_error,
                  items_seen = connector_state.items_seen + EXCLUDED.items_seen,
                  items_captured = connector_state.items_captured + EXCLUDED.items_captured,
                  items_deduped = connector_state.items_deduped + EXCLUDED.items_deduped,
                  items_failed = connector_state.items_failed + EXCLUDED.items_failed,
                  average_processing_time_ms = COALESCE(
                    EXCLUDED.average_processing_time_ms,
                    connector_state.average_processing_time_ms
                  ),
                  state_json = connector_state.state_json || EXCLUDED.state_json,
                  updated_at = clock_timestamp()
                RETURNING {CONNECTOR_STATE_COLUMNS}
                """,
            (
                connector_name,
                connector_name,
                cursor_type,
                state.get("cursor_value"),
                state.get("last_sync_at"),
                state.get("last_success_at"),
                state.get("last_failure_at"),
                state.get("last_error"),
                state.get("items_seen_delta", state.get("items_seen", 0)),
                state.get("items_captured_delta", state.get("items_captured", 0)),
                state.get("items_deduped_delta", state.get("items_deduped", 0)),
                state.get("items_failed_delta", state.get("items_failed", 0)),
                state.get("average_processing_time_ms"),
                _json_object(state.get("state_json")),
            ),
        )
        self._append_mutation_event(
            event_type="connector.state_updated",
            actor_type=actor_type,
            target_type="connector",
            target_id=row["connector_name"],
            payload={
                "connector_id": row["connector_id"],
                "connector_name": row["connector_name"],
                "cursor_type": row["cursor_type"],
                "cursor_value": row["cursor_value"],
                "last_sync_at": row["last_sync_at"],
                "last_success_at": row["last_success_at"],
                "last_failure_at": row["last_failure_at"],
                "items_seen": row["items_seen"],
                "items_captured": row["items_captured"],
                "items_deduped": row["items_deduped"],
                "items_failed": row["items_failed"],
            },
        )
        return row

    def connector_storage_status(self) -> VNextRow:
        return self._fetch_one(
            "connector_storage_status",
            """
                SELECT
                  to_regclass('public.connector_settings') IS NOT NULL AS connector_settings_exists,
                  to_regclass('public.connector_state') IS NOT NULL AS connector_state_exists,
                  to_regclass('public.artifact_quality_ratings') IS NOT NULL AS artifact_quality_ratings_exists,
                  to_regclass('public.scheduler_workflows') IS NOT NULL AS scheduler_workflows_exists,
                  to_regclass('public.scheduler_runs') IS NOT NULL AS scheduler_runs_exists,
                  (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector_version,
                  NULL::text AS migration_revision
            """,
        )

    create_browser_clip_capability = _browser_clip_create_capability
    consume_browser_clip_capability = _browser_clip_consume_capability

    def list_sources(
        self,
        *,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 20,
    ) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE deleted_at IS NULL
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                ORDER BY captured_at DESC, id DESC
                LIMIT %s
                """,
            (domains, domains, sensitivity_allowed, sensitivity_allowed, limit),
        )

    @takes_label_lock
    def create_source(self, source: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "create_source",
            f"""
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
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {SOURCE_COLUMNS}
                """,
            (
                source.get("id"),
                source["source_type"],
                source.get("title"),
                source.get("author"),
                source.get("uri"),
                source.get("raw_path"),
                source["content_hash"],
                source.get("dedupe_key", source["content_hash"]),
                source.get("captured_at"),
                source.get("source_created_at"),
                source.get("source_modified_at"),
                source.get("connector_name"),
                source.get("external_id"),
                source.get("domain", "unknown"),
                source.get("sensitivity", "unknown"),
                _json_object(source.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="source.created",
            actor_type=actor_type,
            target_type="source",
            target_id=row["id"],
            payload={"operation": "create", "fields": _sorted_field_names(source)},
        )
        return row

    @takes_label_lock
    def get_or_create_source(
        self,
        source: JsonObject,
        *,
        actor_type: str = "system",
    ) -> tuple[VNextRow, bool]:
        """Atomically claim one live capture identity and return its source."""
        dedupe_key = str(source.get("dedupe_key") or source["content_hash"])
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                    INSERT INTO sources (
                      id, user_id, source_type, title, author, uri, raw_path,
                      content_hash, dedupe_key, captured_at, source_created_at,
                      source_modified_at, connector_name, external_id, domain,
                      sensitivity, metadata_json
                    )
                    VALUES (
                      COALESCE(%s::uuid, gen_random_uuid()), app.current_user_id(),
                      %s, %s, %s, %s, %s, %s, %s,
                      COALESCE(%s::timestamptz, clock_timestamp()),
                      %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (user_id, dedupe_key)
                      WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
                    DO NOTHING
                    RETURNING {SOURCE_COLUMNS}
                    """,
                (
                    source.get("id"),
                    source["source_type"],
                    source.get("title"),
                    source.get("author"),
                    source.get("uri"),
                    source.get("raw_path"),
                    source["content_hash"],
                    dedupe_key,
                    source.get("captured_at"),
                    source.get("source_created_at"),
                    source.get("source_modified_at"),
                    source.get("connector_name"),
                    source.get("external_id"),
                    source.get("domain", "unknown"),
                    source.get("sensitivity", "unknown"),
                    _json_object(source.get("metadata_json")),
                ),
            )
            raw_row = cur.fetchone()
            created = raw_row is not None
            if raw_row is None:
                cur.execute(
                    f"""
                        SELECT {SOURCE_COLUMNS}
                        FROM sources
                        WHERE dedupe_key = %s
                          AND deleted_at IS NULL
                        ORDER BY captured_at DESC, id DESC
                        LIMIT 1
                        """,
                    (dedupe_key,),
                )
                raw_row = cur.fetchone()
        if raw_row is None:  # pragma: no cover - unique-index invariant backstop
            raise ContinuityStoreInvariantError("get_or_create_source did not return a source")
        row = cast(VNextRow, raw_row)
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
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                """,
            (source_id,),
        )

    def get_sources_by_ids(self, source_ids: Sequence[str], *, include_deleted: bool = False) -> list[VNextRow]:
        """The sources at ``source_ids``. A soft-deleted row is returned only with ``include_deleted``, which a reader
        that must tell a deleted source from one that never existed passes (the open-loop id filter)."""

        ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        if not ids:
            return []
        deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
        return self._fetch_all(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE id = ANY(%s::uuid[])
                  {deleted_clause}
                """,
            (ids,),
        )

    def get_source_by_content_hash(self, content_hash: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE content_hash = %s
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
            (content_hash,),
        )

    def get_sources_by_content_hash(self, content_hash: str) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE content_hash = %s
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                """,
            (content_hash,),
        )

    def get_source_by_dedupe_key(self, dedupe_key: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE dedupe_key = %s
                  AND deleted_at IS NULL
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
            (dedupe_key,),
        )

    @takes_label_lock
    def update_source(self, *, source_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                    SELECT {SOURCE_COLUMNS}
                    FROM sources
                    WHERE id = %s::uuid
                      AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                (source_id,),
            )
            raw_current = cur.fetchone()
            if raw_current is None:
                raise ContinuityStoreInvariantError("update_source did not return a row from the database")
            current = cast(VNextRow, raw_current)
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
            dedupe_key = (
                capture_dedupe_key_for_source(prospective) if dedupe_input_changed else current.get("dedupe_key")
            )
            content_input_changed = scope_changed or raw_text_changed
            content_hash = (
                capture_content_hash_for_source(prospective) or str(current["content_hash"])
                if content_input_changed
                else str(current["content_hash"])
            )
            if dedupe_key is not None and dedupe_key != current.get("dedupe_key"):
                cur.execute(
                    """
                        SELECT id
                        FROM sources
                        WHERE dedupe_key = %s
                          AND id <> %s::uuid
                          AND deleted_at IS NULL
                        LIMIT 1
                        FOR UPDATE
                        """,
                    (dedupe_key, source_id),
                )
                if cur.fetchone() is not None:
                    raise ContinuityStoreInvariantError(
                        "source capture identity already belongs to another live source"
                    )
            try:
                cur.execute(
                    f"""
                        UPDATE sources
                        SET title = COALESCE(%s, title),
                            author = COALESCE(%s, author),
                            uri = COALESCE(%s, uri),
                            raw_path = COALESCE(%s, raw_path),
                            domain = COALESCE(%s, domain),
                            sensitivity = COALESCE(%s, sensitivity),
                            metadata_json = COALESCE(%s, metadata_json),
                            content_hash = %s,
                            dedupe_key = %s
                        WHERE id = %s::uuid
                          AND deleted_at IS NULL
                        RETURNING {SOURCE_COLUMNS}
                        """,
                    (
                        patch.get("title"),
                        patch.get("author"),
                        patch.get("uri"),
                        patch.get("raw_path"),
                        patch.get("domain"),
                        patch.get("sensitivity"),
                        _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                        content_hash,
                        dedupe_key,
                        source_id,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ContinuityStoreInvariantError(
                    "source capture identity already belongs to another live source"
                ) from exc
            raw_row = cur.fetchone()
        if raw_row is None:
            raise ContinuityStoreInvariantError("update_source did not return a row from the database")
        row = cast(VNextRow, raw_row)
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

    @takes_label_lock
    def delete_source(self, *, source_id: str, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "delete_source",
            f"""
                UPDATE sources
                SET deleted_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING {SOURCE_COLUMNS}
                """,
            (source_id,),
        )
        self._append_mutation_event(
            event_type="source.deleted",
            actor_type=actor_type,
            target_type="source",
            target_id=row["id"],
            payload={"operation": "delete"},
        )
        return row

    def create_source_chunk(self, chunk: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "create_source_chunk",
            f"""
                INSERT INTO source_chunks (
                  id,
                  user_id,
                  source_id,
                  chunk_index,
                  text,
                  token_count,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {SOURCE_CHUNK_COLUMNS}
                """,
            (
                chunk.get("id"),
                chunk["source_id"],
                chunk["chunk_index"],
                chunk["text"],
                chunk.get("token_count"),
                _json_object(chunk.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="source_chunk.created",
            actor_type=actor_type,
            target_type="source_chunk",
            target_id=row["id"],
            payload={"operation": "create", "source_id": str(row["source_id"])},
        )
        return row

    def list_source_chunks(self, source_id: str, *, limit: int = 500) -> list[VNextRow]:
        if limit < 1:
            raise ValueError("limit must be positive")
        bounded_limit = min(limit, MAX_SOURCE_CHUNKS_PER_READ)
        return self._fetch_all(
            f"""
                SELECT {SOURCE_CHUNK_COLUMNS}
                FROM source_chunks
                WHERE source_id = %s::uuid
                ORDER BY chunk_index ASC, id ASC
                LIMIT %s
                """,
            (source_id, bounded_limit),
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

        Rank comes back as ``fts_score`` on each chunk row (row order IS
        the rank), and every row carries ``source_id`` so the retrieval
        service can promote the parent source. The domain/sensitivity
        gates live on the parent source row, so the query joins sources
        and applies them there, mirroring ``search_sources``. Strict
        pass: ``websearch_to_tsquery`` ANDs every non-stopword term;
        ``match_any`` ORs the sanitized lexemes instead (the retrieval
        service's one-shot fallback for multi-word questions the strict
        pass missed). Uses the ``search_tsv`` generated column + GIN
        index from migration 20260707_0081.
        """
        if match_any:
            tsquery_sql = "to_tsquery('english', %s)"
            tsquery_text = _tsquery_any_expression(query)
            if tsquery_text is None:
                return []
        else:
            tsquery_sql = "websearch_to_tsquery('english', %s)"
            tsquery_text = query
        scope_projects_list = list(project_scope_identity(scope_projects)) or None
        scope_people_list = list(scope_people) or None
        project_scope_sql = _jsonb_source_project_scope_values_sql("s.metadata_json")
        people_scope_sql = _SOURCE_SCOPE_PEOPLE_SQL.replace("metadata_json", "s.metadata_json")
        event_time_sql = (
            _SOURCE_SCOPE_EVENT_TIME_SQL.replace("source_created_at", "s.source_created_at")
            .replace("captured_at", "s.captured_at")
            .replace("metadata_json", "s.metadata_json")
        )
        return self._fetch_all(
            f"""
                SELECT {_SOURCE_CHUNK_SEARCH_COLUMNS},
                  ts_rank(c.search_tsv, {tsquery_sql}) AS fts_score
                FROM source_chunks c
                JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
                WHERE s.deleted_at IS NULL
                  AND (%s::text[] IS NULL OR s.domain = ANY(%s::text[]) OR s.domain = 'unknown')
                  AND (%s::text[] IS NULL OR s.sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({project_scope_sql}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR {people_scope_sql})
                  AND (%s::timestamptz IS NULL OR {event_time_sql} >= %s::timestamptz)
                  AND (%s::timestamptz IS NULL OR {event_time_sql} <= %s::timestamptz)
                  AND c.search_tsv @@ {tsquery_sql}
                ORDER BY fts_score DESC, c.created_at DESC, c.id DESC
                LIMIT %s
                """,
            (
                tsquery_text,
                domains,
                domains,
                sensitivity_allowed,
                sensitivity_allowed,
                scope_projects_list,
                scope_projects_list,
                scope_people_list,
                scope_people_list,
                scope_window_start,
                scope_window_start,
                scope_window_end,
                scope_window_end,
                tsquery_text,
                limit,
            ),
        )

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

    list_memories = _memory_list_memories

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

    get_memory_by_commit_digest = _memory_get_memory_by_commit_digest

    get_memory_by_confirmation_id = _memory_get_memory_by_confirmation_id

    latest_agentic_commit_memory = _memory_latest_agentic_commit_memory

    update_memory = _lifecycle_update_memory

    append_revision = _events_append_revision
    list_revisions = _events_list_revisions

    # -- true redaction ----------------------------------------------------
    #
    # Alice's forget is a soft delete; redaction expunges CONTENT while
    # preserving the audit SKELETON (ids, timestamps, event/revision
    # types, actor columns). The append-only triggers on event_log and
    # memory_revisions (replaced by migration 20260706_0079) only admit
    # these updates while the app.redaction_in_progress session flag is
    # 'on' AND the change is marker-shaped; _redaction_mode manages the
    # flag and resets it even on error paths.

    _redaction_mode = _lifecycle__redaction_mode

    redact_memory_bundle = _lifecycle_redact_memory_bundle

    redact_memory_content = _lifecycle_redact_memory_content

    redact_memory_revisions = _lifecycle_redact_memory_revisions

    redact_memory_events = _lifecycle_redact_memory_events

    create_provenance_link = _lifecycle_create_provenance_link

    list_provenance_links = _lifecycle_list_provenance_links

    list_provenance_links_for_targets = _lifecycle_list_provenance_links_for_targets

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
        patterns = _search_patterns(query)
        exact_pattern = patterns[0]
        scope_projects_list = list(project_scope_identity(scope_projects)) or None
        scope_people_list = list(scope_people) or None
        return self._fetch_all(
            f"""
                SELECT {SOURCE_COLUMNS}
                FROM sources
                WHERE deleted_at IS NULL
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_SOURCE_SCOPE_PROJECT_SQL}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR {_SOURCE_SCOPE_PEOPLE_SQL})
                  AND (
                    %s::timestamptz IS NULL
                    OR {_SOURCE_SCOPE_EVENT_TIME_SQL} >= %s::timestamptz
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_SOURCE_SCOPE_EVENT_TIME_SQL} <= %s::timestamptz
                  )
                  AND (
                    title ILIKE ANY(%s::text[])
                    OR author ILIKE ANY(%s::text[])
                    OR uri ILIKE ANY(%s::text[])
                    OR raw_path ILIKE ANY(%s::text[])
                    OR content_hash ILIKE ANY(%s::text[])
                    OR metadata_json::text ILIKE ANY(%s::text[])
                  )
                ORDER BY
                  CASE
                    WHEN title ILIKE %s THEN 0
                    WHEN title ILIKE ANY(%s::text[]) THEN 1
                    ELSE 2
                  END,
                  captured_at DESC,
                  id DESC
                LIMIT %s
                """,
            (
                domains,
                domains,
                sensitivity_allowed,
                sensitivity_allowed,
                scope_projects_list,
                scope_projects_list,
                scope_people_list,
                scope_people_list,
                scope_window_start,
                scope_window_start,
                scope_window_end,
                scope_window_end,
                patterns,
                patterns,
                patterns,
                patterns,
                patterns,
                patterns,
                exact_pattern,
                patterns,
                limit,
            ),
        )

    create_edge = _graph_create_edge

    find_edge_by_idempotency_digest = _graph_find_edge_by_idempotency_digest

    upsert_edge_by_idempotency_digest = _graph_upsert_edge_by_idempotency_digest

    list_edges = _graph_list_edges

    list_memory_entity_edges = _graph_list_memory_entity_edges

    list_edges_as_of = _graph_list_edges_as_of

    update_edge_status = _graph_update_edge_status

    expire_edge = _graph_expire_edge

    @takes_label_lock
    def create_project(self, project: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "create_project",
            f"""
                INSERT INTO projects (
                  id,
                  user_id,
                  name,
                  slug,
                  status,
                  description,
                  current_state,
                  domain,
                  sensitivity,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {PROJECT_COLUMNS}
                """,
            (
                project.get("id"),
                project["name"],
                project["slug"],
                project.get("status", "active"),
                project.get("description"),
                project.get("current_state"),
                project.get("domain", "professional"),
                project.get("sensitivity", "private"),
                _json_object(project.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="project.created",
            actor_type=actor_type,
            target_type="project",
            target_id=row["id"],
            payload={"operation": "create", "fields": _sorted_field_names(project)},
        )
        return row

    def get_project(self, project_id: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {PROJECT_COLUMNS}
                FROM projects
                WHERE id = %s::uuid
                """,
            (project_id,),
        )

    @takes_label_lock
    def get_project_for_update(self, project_id: str) -> VNextRow | None:
        """Lock a project while an artifact review applies its state."""

        return self._fetch_optional_one(
            f"""
                SELECT {PROJECT_COLUMNS}
                FROM projects
                WHERE id = %s::uuid
                FOR UPDATE
                """,
            (project_id,),
        )

    def list_projects(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        scope_projects: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[VNextRow]:
        project_scope = list(project_scope_identity(scope_projects or ())) or None
        normalized_scope = project_scope
        slug_scope_identity_sql = _project_identifier_identity_sql("slug")
        name_scope_identity_sql = _project_identifier_identity_sql("name")
        return self._fetch_all(
            f"""
                SELECT {PROJECT_COLUMNS}
                FROM projects
                WHERE (%s::text IS NULL OR status = %s)
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (
                    %s::text[] IS NULL
                    OR id::text = ANY(%s::text[])
                    OR {slug_scope_identity_sql} = ANY(%s::text[])
                    OR {name_scope_identity_sql} = ANY(%s::text[])
                  )
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
            (
                status,
                status,
                domains,
                domains,
                sensitivity_allowed,
                sensitivity_allowed,
                project_scope,
                project_scope,
                normalized_scope,
                normalized_scope,
                limit,
            ),
        )

    @takes_label_lock
    def update_project(self, *, project_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "update_project",
            f"""
                UPDATE projects
                SET name = COALESCE(%s, name),
                    status = COALESCE(%s, status),
                    description = COALESCE(%s, description),
                    current_state = COALESCE(%s, current_state),
                    domain = COALESCE(%s, domain),
                    sensitivity = COALESCE(%s, sensitivity),
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {PROJECT_COLUMNS}
                """,
            (
                patch.get("name"),
                patch.get("status"),
                patch.get("description"),
                patch.get("current_state"),
                patch.get("domain"),
                patch.get("sensitivity"),
                _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                project_id,
            ),
        )
        self._append_mutation_event(
            event_type="project.updated",
            actor_type=actor_type,
            target_type="project",
            target_id=row["id"],
            payload={"operation": "update", "changes": patch},
        )
        return row

    def create_person(self, person: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "create_person",
            f"""
                INSERT INTO people (
                  id,
                  user_id,
                  name,
                  aliases_json,
                  relationship_type,
                  organization,
                  sensitivity,
                  notes,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {PERSON_COLUMNS}
                """,
            (
                person.get("id"),
                person["name"],
                _json_list(person.get("aliases_json")),
                person.get("relationship_type"),
                person.get("organization"),
                person.get("sensitivity", "private"),
                person.get("notes"),
                _json_object(person.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="person.created",
            actor_type=actor_type,
            target_type="person",
            target_id=row["id"],
            payload={"operation": "create", "fields": _sorted_field_names(person)},
        )
        return row

    def get_person(self, person_id: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {PERSON_COLUMNS}
                FROM people
                WHERE id = %s::uuid
            """,
            (person_id,),
        )

    def list_people(
        self,
        *,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {PERSON_COLUMNS}
                FROM people
                WHERE (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
            (sensitivity_allowed, sensitivity_allowed, limit),
        )

    def update_person(self, *, person_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "update_person",
            f"""
                UPDATE people
                SET name = COALESCE(%s, name),
                    aliases_json = COALESCE(%s, aliases_json),
                    relationship_type = COALESCE(%s, relationship_type),
                    organization = COALESCE(%s, organization),
                    sensitivity = COALESCE(%s, sensitivity),
                    notes = COALESCE(%s, notes),
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {PERSON_COLUMNS}
                """,
            (
                patch.get("name"),
                _json_list(patch["aliases_json"]) if "aliases_json" in patch else None,
                patch.get("relationship_type"),
                patch.get("organization"),
                patch.get("sensitivity"),
                patch.get("notes"),
                _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                person_id,
            ),
        )
        self._append_mutation_event(
            event_type="person.updated",
            actor_type=actor_type,
            target_type="person",
            target_id=row["id"],
            payload={"operation": "update", "changes": patch},
        )
        return row

    # -- entities ----------------------------------------------------------
    #
    # Generic entity substrate (migration 20260705_0078): one row per
    # resolved real-world thing, keyed for resolution by
    # (entity_type, normalized_name). Entities participate in the graph
    # without edge changes: graph_edges.from_type/from_id and
    # to_type/to_id are free-text node references (no FK, no node-type
    # CHECK), so an edge can point at an entity with
    # from_type='entity', from_id=<entity id> today; only edge_type is
    # constrained (to EDGE_TYPES from migration 20260510_0067).

    create_entity = _graph_create_entity

    get_entity = _graph_get_entity

    get_entity_by_normalized_name = _graph_get_entity_by_normalized_name

    find_entities_by_names = _graph_find_entities_by_names

    list_entities = _graph_list_entities

    update_entity = _graph_update_entity

    record_entity_mention = _graph_record_entity_mention

    record_relationship_change = _graph_record_relationship_change

    list_relationship_events = _graph_list_relationship_events

    create_belief = _graph_create_belief

    get_belief = _graph_get_belief

    list_beliefs = _graph_list_beliefs

    update_belief_status = _graph_update_belief_status

    create_open_loop = _graph_create_open_loop

    upsert_open_loop_by_automation_digest = _graph_upsert_open_loop_by_automation_digest

    get_open_loop = _graph_get_open_loop

    find_open_loop_by_automation_digest = _graph_find_open_loop_by_automation_digest

    list_open_loops_referencing_source = _graph_list_open_loops_referencing_source

    list_open_loops = _graph_list_open_loops

    list_open_loop_events = _graph_list_open_loop_events

    update_open_loop = _graph_update_open_loop

    update_open_loop_status = _graph_update_open_loop_status

    @takes_label_lock
    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> VNextRow:
        from alicebot_api.vnext_label_writes import apply_insert_floor, remember_floor_event

        artifact, floor_event = apply_insert_floor(self, "artifact", artifact)
        row = self._fetch_one(
            "create_artifact",
            f"""
                INSERT INTO generated_artifacts (
                  id,
                  user_id,
                  artifact_type,
                  title,
                  content_markdown,
                  status,
                  domain,
                  sensitivity,
                  generated_by,
                  prompt_hash,
                  model_info_json,
                  reviewed_at,
                  promoted_at,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::timestamptz,
                  %s
                )
                RETURNING {ARTIFACT_COLUMNS}
                """,
            (
                artifact.get("id"),
                artifact["artifact_type"],
                artifact["title"],
                artifact["content_markdown"],
                artifact.get("status", "draft"),
                artifact.get("domain", "unknown"),
                artifact.get("sensitivity", "unknown"),
                artifact.get("generated_by", actor_type),
                artifact.get("prompt_hash"),
                _json_object(artifact.get("model_info_json")),
                artifact.get("reviewed_at"),
                artifact.get("promoted_at"),
                _json_object(artifact.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="artifact.created",
            actor_type=actor_type,
            target_type="artifact",
            target_id=row["id"],
            payload={"operation": "create", "artifact_type": str(row["artifact_type"])},
        )
        remember_floor_event(self, floor_event, row["id"])
        return row

    @takes_label_lock
    def upsert_artifact_by_workflow_digest(
        self,
        artifact: JsonObject,
        *,
        workflow: str,
        digest: str,
        actor_type: str = "system",
    ) -> VNextRow:
        """Atomically persist or replay one workflow artifact.

        New writes carry the canonical ``idempotency_digest`` protected by a
        unique partial index.  The fallback lookup also recognizes legacy
        workflow-specific digest keys so upgrades replay an existing result
        without rewriting historical rows.
        """

        normalized_workflow = str(workflow).strip()
        normalized_digest = str(digest).strip()
        if not normalized_workflow or not normalized_digest:
            raise ValueError("workflow and digest must not be empty")
        existing = self.find_artifact_by_workflow_digest(
            artifact_type=str(artifact["artifact_type"]),
            workflow=normalized_workflow,
            digest=normalized_digest,
        )
        if existing is not None:
            return existing
        from alicebot_api.vnext_label_writes import apply_insert_floor, remember_floor_event

        artifact, floor_event = apply_insert_floor(self, "artifact", artifact)
        metadata_value = artifact.get("metadata_json")
        metadata: JsonObject = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        metadata.update(
            {
                "workflow": normalized_workflow,
                "idempotency_digest": normalized_digest,
            }
        )
        row = self._fetch_optional_one(
            f"""
                INSERT INTO generated_artifacts (
                  id,
                  user_id,
                  artifact_type,
                  title,
                  content_markdown,
                  status,
                  domain,
                  sensitivity,
                  generated_by,
                  prompt_hash,
                  model_info_json,
                  reviewed_at,
                  promoted_at,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::timestamptz,
                  %s,
                  %s
                )
                ON CONFLICT DO NOTHING
                RETURNING {ARTIFACT_COLUMNS}
                """,
            (
                artifact.get("id"),
                artifact["artifact_type"],
                artifact["title"],
                artifact["content_markdown"],
                artifact.get("status", "draft"),
                artifact.get("domain", "unknown"),
                artifact.get("sensitivity", "unknown"),
                artifact.get("generated_by", actor_type),
                artifact.get("prompt_hash"),
                _json_object(artifact.get("model_info_json")),
                artifact.get("reviewed_at"),
                artifact.get("promoted_at"),
                _json_object(metadata),
            ),
        )
        created = row is not None
        if row is None:
            row = self.find_artifact_by_workflow_digest(
                artifact_type=str(artifact["artifact_type"]),
                workflow=normalized_workflow,
                digest=normalized_digest,
            )
        if row is None:
            raise ContinuityStoreInvariantError(
                "upsert_artifact_by_workflow_digest could not resolve the persisted artifact"
            )
        if created:
            self._append_mutation_event(
                event_type="artifact.created",
                actor_type=actor_type,
                target_type="artifact",
                target_id=row["id"],
                payload={"operation": "create", "artifact_type": str(row["artifact_type"])},
            )
            remember_floor_event(self, floor_event, row["id"])
        return row

    def get_artifact(self, artifact_id: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE id = %s::uuid
                """,
            (artifact_id,),
        )

    @takes_label_lock
    def get_artifact_for_update(self, artifact_id: str) -> VNextRow | None:
        """Lock one persisted artifact before an authorized side effect."""

        return self._fetch_optional_one(
            f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE id = %s::uuid
                FOR UPDATE
                """,
            (artifact_id,),
        )

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        scope_projects: tuple[str, ...] = (),
        limit: int = 8,
    ) -> list[VNextRow]:
        project_list = list(project_scope_identity(scope_projects)) or None
        return self._fetch_all(
            f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE (%s::text IS NULL OR artifact_type = %s)
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_ARTIFACT_SCOPE_PROJECT_SQL}) ?| %s::text[]
                       OR ({_PROJECT_FLOOR_SQL}) ?| %s::text[])
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
            (
                artifact_type,
                artifact_type,
                domains,
                domains,
                sensitivity_allowed,
                sensitivity_allowed,
                project_list,
                project_list,
                project_list,
                limit,
            ),
        )

    def list_artifacts_referencing_source(self, *, source_id: str, limit: int = 500) -> list[VNextRow]:
        """Bound artifacts related to one source before ordering and LIMIT."""

        if limit < 1:
            raise ValueError("limit must be positive")
        source_ref = f"source:{source_id}"
        qualified_columns = ", ".join(f"a.{column.strip()}" for column in ARTIFACT_COLUMNS.split(",") if column.strip())
        return self._fetch_all(
            f"""
                SELECT {qualified_columns}
                FROM generated_artifacts AS a
                WHERE (
                  EXISTS (
                    SELECT 1
                    FROM provenance_links AS p
                    WHERE p.target_type = 'artifact'
                      AND p.target_id = a.id::text
                      AND p.source_id = %s::uuid
                  )
                  OR a.metadata_json ->> 'source_id' = %s
                  OR a.metadata_json ->> 'source_ref' IN (%s, %s)
                  OR a.metadata_json -> 'source_ids' ? %s
                  OR a.metadata_json -> 'source_refs' ? %s
                  OR a.metadata_json -> 'source_refs' ? %s
                  OR a.metadata_json -> 'source_references' ? %s
                  OR a.metadata_json -> 'source_references' ? %s
                  OR a.metadata_json -> 'selected_source_ids' ? %s
                )
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT %s
                """,
            (
                source_id,
                source_id,
                source_id,
                source_ref,
                source_id,
                source_id,
                source_ref,
                source_id,
                source_ref,
                source_id,
                limit,
            ),
        )

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: Sequence[str] | None = None,
    ) -> VNextRow | None:
        """Find one exact idempotency artifact without scanning a recent prefix."""
        normalized_digest = str(digest).strip()
        if not normalized_digest:
            return None
        project_list = list(project_scope_identity(scope_projects or ())) or None
        return self._fetch_optional_one(
            f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE artifact_type = %s
                  AND metadata_json ->> 'workflow' = %s
                  AND COALESCE(
                    metadata_json ->> 'idempotency_digest',
                    metadata_json ->> 'workflow_digest',
                    metadata_json ->> 'automation_digest',
                    metadata_json ->> 'consolidation_digest'
                  ) = %s
                  AND (%s::text[] IS NULL OR ({_ARTIFACT_SCOPE_PROJECT_SQL}) ?| %s::text[])
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
            (
                str(artifact_type),
                str(workflow),
                normalized_digest,
                project_list,
                project_list,
            ),
        )

    @takes_label_lock
    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        metadata_json: JsonObject | None = None,
        actor_type: str = "system",
    ) -> VNextRow | None:
        """Apply a compare-and-set artifact transition.

        ``expected_status`` is optional for compatibility with lower-level
        callers, but review services always provide it after locking the row.
        A lost comparison returns ``None`` instead of overwriting the winner.
        """

        row = self._fetch_optional_one(
            f"""
                UPDATE generated_artifacts
                SET status = %s,
                    reviewed_at = CASE
                      WHEN %s IN ('reviewed', 'accepted', 'rejected') THEN clock_timestamp()
                      ELSE reviewed_at
                    END,
                    promoted_at = CASE
                      WHEN %s = 'promoted_to_memory' THEN clock_timestamp()
                      ELSE promoted_at
                    END,
                    metadata_json = CASE
                      WHEN %s::jsonb IS NULL THEN metadata_json
                      ELSE metadata_json || %s::jsonb
                    END
                WHERE id = %s::uuid
                  AND (%s::text IS NULL OR status = %s)
                RETURNING {ARTIFACT_COLUMNS}
                """,
            (
                status,
                status,
                status,
                _json_object(metadata_json) if metadata_json is not None else None,
                _json_object(metadata_json) if metadata_json is not None else None,
                artifact_id,
                expected_status,
                expected_status,
            ),
        )
        if row is None:
            return None
        self._append_mutation_event(
            event_type="artifact.updated",
            actor_type=actor_type,
            target_type="artifact",
            target_id=row["id"],
            payload={"operation": "update_status", "status": status},
        )
        return row

    def create_artifact_quality_rating(self, rating: JsonObject, *, actor_type: str = "user") -> VNextRow:
        artifact = self.get_artifact_for_update(str(rating.get("artifact_id") or ""))
        if artifact is not None and is_redacted_project_update_artifact(artifact):
            raise ValueError("ratings cannot be added to a redacted artifact")
        row = self._fetch_one(
            "create_artifact_quality_rating",
            f"""
                INSERT INTO artifact_quality_ratings (
                  id,
                  user_id,
                  artifact_id,
                  reviewer_id,
                  usefulness,
                  accuracy,
                  source_grounding,
                  novel_connections,
                  actionability,
                  hallucination_risk,
                  verbosity,
                  missed_context,
                  comments,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                ON CONFLICT (artifact_id, reviewer_id) DO UPDATE SET
                  usefulness = EXCLUDED.usefulness,
                  accuracy = EXCLUDED.accuracy,
                  source_grounding = EXCLUDED.source_grounding,
                  novel_connections = EXCLUDED.novel_connections,
                  actionability = EXCLUDED.actionability,
                  hallucination_risk = EXCLUDED.hallucination_risk,
                  verbosity = EXCLUDED.verbosity,
                  missed_context = EXCLUDED.missed_context,
                  comments = EXCLUDED.comments,
                  metadata_json = EXCLUDED.metadata_json
                RETURNING {QUALITY_RATING_COLUMNS}
                """,
            (
                rating.get("id"),
                rating["artifact_id"],
                rating.get("reviewer_id"),
                rating.get("usefulness"),
                rating.get("accuracy"),
                rating.get("source_grounding"),
                rating.get("novel_connections"),
                rating.get("actionability"),
                rating.get("hallucination_risk"),
                rating.get("verbosity"),
                rating.get("missed_context"),
                rating.get("comments"),
                _json_object(rating.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="artifact.quality_rated",
            actor_type=actor_type,
            target_type="artifact",
            target_id=row["artifact_id"],
            payload={
                "operation": "upsert_quality_rating",
                "quality_rating_id": str(row["id"]),
                "artifact_id": str(row["artifact_id"]),
                "reviewer_id": row.get("reviewer_id"),
            },
        )
        return row

    def list_artifact_quality_ratings(
        self,
        *,
        artifact_id: str | None = None,
        scope_projects: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[VNextRow]:
        project_list = list(project_scope_identity(scope_projects or ())) or None
        return self._fetch_all(
            f"""
                SELECT {QUALITY_RATING_COLUMNS}
                FROM artifact_quality_ratings
                WHERE (%s::uuid IS NULL OR artifact_id = %s::uuid)
                  AND (
                    %s::text[] IS NULL
                    OR artifact_id IN (
                      SELECT id
                      FROM generated_artifacts
                      WHERE ({_ARTIFACT_SCOPE_PROJECT_SQL}) ?| %s::text[]
                    )
                  )
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
            (artifact_id, artifact_id, project_list, project_list, limit),
        )

    def create_task(self, task: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "create_task",
            f"""
                INSERT INTO task_queue (
                  id,
                  user_id,
                  title,
                  task_type,
                  instructions,
                  status,
                  requested_by,
                  scope_json,
                  allowed_sources_json,
                  domain,
                  sensitivity,
                  write_policy,
                  scheduled_for,
                  output_artifact_id,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::uuid,
                  %s
                )
                RETURNING {TASK_COLUMNS}
                """,
            (
                task.get("id"),
                task["title"],
                task["task_type"],
                task["instructions"],
                task.get("status", "pending"),
                task.get("requested_by", actor_type),
                _json_object(task.get("scope_json")),
                _json_list(task.get("allowed_sources_json")),
                task.get("domain", "unknown"),
                task.get("sensitivity", "unknown"),
                task.get("write_policy", "proposal_only"),
                task.get("scheduled_for"),
                task.get("output_artifact_id"),
                _json_object(task.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="task.created",
            actor_type=actor_type,
            target_type="task",
            target_id=row["id"],
            payload={"operation": "create", "task_type": str(row["task_type"])},
        )
        return row

    def claim_next_task(self, *, actor_type: str = "system") -> VNextRow | None:
        row = self._fetch_optional_one(
            f"""
                WITH next_task AS (
                  SELECT id
                  FROM task_queue
                  WHERE status = 'pending'
                    AND (scheduled_for IS NULL OR scheduled_for <= clock_timestamp())
                  ORDER BY scheduled_for ASC NULLS FIRST, created_at ASC, id ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE task_queue
                SET status = 'running',
                    started_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                FROM next_task
                WHERE task_queue.id = next_task.id
                RETURNING {TASK_COLUMNS}
                """
        )
        if row is not None:
            self._append_mutation_event(
                event_type="task.claimed",
                actor_type=actor_type,
                target_type="task",
                target_id=row["id"],
                payload={"operation": "claim"},
            )
        return row

    def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        details: JsonObject | None = None,
        actor_type: str = "system",
    ) -> VNextRow:
        details = details or {}
        row = self._fetch_one(
            "update_task_status",
            f"""
                UPDATE task_queue
                SET status = %s,
                    completed_at = CASE
                      WHEN %s = 'completed' THEN clock_timestamp()
                      ELSE completed_at
                    END,
                    failed_at = CASE
                      WHEN %s = 'failed' THEN clock_timestamp()
                      ELSE failed_at
                    END,
                    error_message = COALESCE(%s, error_message),
                    output_artifact_id = COALESCE(%s::uuid, output_artifact_id),
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {TASK_COLUMNS}
                """,
            (
                status,
                status,
                status,
                details.get("error_message"),
                details.get("output_artifact_id"),
                _json_object(details["metadata_json"]) if "metadata_json" in details else None,
                task_id,
            ),
        )
        self._append_mutation_event(
            event_type="task.updated",
            actor_type=actor_type,
            target_type="task",
            target_id=row["id"],
            payload={"operation": "update_status", "status": status, "details": details},
        )
        return row

    def list_tasks(self, *, status: str | None = None, limit: int = 8) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {TASK_COLUMNS}
                FROM task_queue
                WHERE (%s::text IS NULL OR status = %s)
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
            (status, status, limit),
        )

    def upsert_brain_charter(self, charter: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_one(
            "upsert_brain_charter",
            f"""
                INSERT INTO brain_charters (
                  id,
                  user_id,
                  content_markdown,
                  owner_json,
                  memory_philosophy_json,
                  life_domains_json,
                  active_projects_json,
                  communication_style_json,
                  priorities_json,
                  autonomous_rules_json,
                  quality_standard_json,
                  sensitivity
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                  content_markdown = EXCLUDED.content_markdown,
                  owner_json = EXCLUDED.owner_json,
                  memory_philosophy_json = EXCLUDED.memory_philosophy_json,
                  life_domains_json = EXCLUDED.life_domains_json,
                  active_projects_json = EXCLUDED.active_projects_json,
                  communication_style_json = EXCLUDED.communication_style_json,
                  priorities_json = EXCLUDED.priorities_json,
                  autonomous_rules_json = EXCLUDED.autonomous_rules_json,
                  quality_standard_json = EXCLUDED.quality_standard_json,
                  sensitivity = EXCLUDED.sensitivity,
                  updated_at = clock_timestamp()
                RETURNING {BRAIN_CHARTER_COLUMNS}
                """,
            (
                charter.get("id"),
                charter["content_markdown"],
                _json_object(charter.get("owner_json")),
                _json_object(charter.get("memory_philosophy_json")),
                _json_object(charter.get("life_domains_json")),
                _json_list(charter.get("active_projects_json")),
                _json_object(charter.get("communication_style_json")),
                _json_object(charter.get("priorities_json")),
                _json_list(charter.get("autonomous_rules_json")),
                _json_list(charter.get("quality_standard_json")),
                charter.get("sensitivity", "private"),
            ),
        )
        self._append_mutation_event(
            event_type="brain_charter.upserted",
            actor_type=actor_type,
            target_type="brain_charter",
            target_id=row["id"],
            payload={"operation": "upsert", "fields": _sorted_field_names(charter)},
        )
        return row

    def get_brain_charter(self) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {BRAIN_CHARTER_COLUMNS}
                FROM brain_charters
                LIMIT 1
                """
        )

    def upsert_agent_identity(self, agent: JsonObject, *, actor_type: str = "agent") -> VNextRow:
        row = self._fetch_one(
            "upsert_agent_identity",
            f"""
                INSERT INTO agent_identities (
                  id,
                  user_id,
                  agent_id,
                  agent_type,
                  permission_profile,
                  display_name,
                  project_scope_json,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                ON CONFLICT (user_id, agent_id)
                DO UPDATE SET
                  agent_type = EXCLUDED.agent_type,
                  permission_profile = EXCLUDED.permission_profile,
                  display_name = COALESCE(EXCLUDED.display_name, agent_identities.display_name),
                  project_scope_json = EXCLUDED.project_scope_json,
                  metadata_json = agent_identities.metadata_json || EXCLUDED.metadata_json,
                  updated_at = clock_timestamp()
                RETURNING {AGENT_IDENTITY_COLUMNS}
                """,
            (
                agent.get("id"),
                agent["agent_id"],
                agent.get("agent_type", "unknown"),
                agent.get("permission_profile", "read_only_agent"),
                agent.get("display_name"),
                _json_list(agent.get("project_scope_json") or agent.get("project_scope")),
                _json_object(agent.get("metadata_json")),
            ),
        )
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
                SELECT {AGENT_IDENTITY_COLUMNS}
                FROM agent_identities
                ORDER BY updated_at DESC, id DESC
                LIMIT %s
                """,
            (limit,),
        )

    def list_agent_events(self, *, agent_id: str | None = None, limit: int = 50) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {EVENT_LOG_COLUMNS}
                FROM event_log
                WHERE actor_type = 'agent'
                  AND (%s::text IS NULL OR actor_id = %s)
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s
                """,
            (agent_id, agent_id, limit),
        )

    def list_agent_policy_artifacts(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[VNextRow]:
        """Bound only agent-generated artifacts for policy telemetry."""

        if limit < 1:
            raise ValueError("limit must be positive")
        return self._fetch_all(
            f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE metadata_json ->> 'generated_by' = 'agent'
                  AND (%s::text IS NULL OR metadata_json ->> 'agent_id' = %s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
            (agent_id, agent_id, limit),
        )

    def list_agent_policy_memories(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[VNextRow]:
        """Bound only agent-attributed memory proposals for telemetry."""

        if limit < 1:
            raise ValueError("limit must be positive")
        return self._fetch_all(
            f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND metadata_json ->> 'agent_id' IS NOT NULL
                  AND (%s::text IS NULL OR metadata_json ->> 'agent_id' = %s)
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
            (agent_id, agent_id, limit),
        )

    def create_agent_api_key(self, key: JsonObject, *, actor_type: str = "user") -> VNextRow:
        row = self._fetch_one(
            "create_agent_api_key",
            f"""
                INSERT INTO agent_api_keys (
                  id,
                  user_id,
                  agent_id,
                  permission_profile,
                  project_scope,
                  key_hash,
                  key_prefix,
                  label
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {AGENT_API_KEY_COLUMNS}
                """,
            (
                key.get("id"),
                key["agent_id"],
                key["permission_profile"],
                key.get("project_scope"),
                key["key_hash"],
                key["key_prefix"],
                key.get("label"),
            ),
        )
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
                SELECT {AGENT_API_KEY_COLUMNS}
                FROM agent_api_keys
                WHERE key_hash = %s
                """,
            (key_hash,),
        )

    def list_agent_api_keys(self, *, limit: int = 50) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {AGENT_API_KEY_COLUMNS}
                FROM agent_api_keys
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
            (limit,),
        )

    def revoke_agent_api_key(self, *, key_id: str, actor_type: str = "user") -> VNextRow | None:
        row = self._fetch_optional_one(
            f"""
                UPDATE agent_api_keys
                SET revoked_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND revoked_at IS NULL
                RETURNING {AGENT_API_KEY_COLUMNS}
                """,
            (key_id,),
        )
        if row is None:
            return None
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
        return self._fetch_one(
            "touch_agent_api_key",
            f"""
                UPDATE agent_api_keys
                SET last_used_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {AGENT_API_KEY_COLUMNS}
                """,
            (key_id,),
        )

    def count_active_agent_api_keys(self) -> int:
        row = self._fetch_one(
            "count_active_agent_api_keys",
            """
                SELECT count(*) AS active_count
                FROM agent_api_keys
                WHERE revoked_at IS NULL
                """,
        )
        return int(cast(int, row["active_count"]))

    def upsert_scheduler_workflow(self, workflow: JsonObject, *, actor_type: str = "system") -> VNextRow:
        row = self._fetch_optional_one(
            f"""
                INSERT INTO scheduler_workflows (
                  id,
                  user_id,
                  workflow_type,
                  enabled,
                  paused,
                  schedule_json,
                  timezone,
                  next_run_at,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  COALESCE(%s, false),
                  COALESCE(%s, false),
                  %s,
                  COALESCE(%s, 'UTC'),
                  %s,
                  %s
                )
                ON CONFLICT (user_id, workflow_type)
                DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  paused = EXCLUDED.paused,
                  schedule_json = EXCLUDED.schedule_json,
                  timezone = EXCLUDED.timezone,
                  next_run_at = EXCLUDED.next_run_at,
                  metadata_json = scheduler_workflows.metadata_json || EXCLUDED.metadata_json,
                  claim_token = NULL,
                  claim_version = scheduler_workflows.claim_version + 1,
                  claim_expires_at = NULL,
                  updated_at = clock_timestamp()
                WHERE scheduler_workflows.enabled IS DISTINCT FROM EXCLUDED.enabled
                   OR scheduler_workflows.paused IS DISTINCT FROM EXCLUDED.paused
                   OR scheduler_workflows.schedule_json IS DISTINCT FROM EXCLUDED.schedule_json
                   OR scheduler_workflows.timezone IS DISTINCT FROM EXCLUDED.timezone
                   OR scheduler_workflows.next_run_at IS DISTINCT FROM EXCLUDED.next_run_at
                   OR NOT (scheduler_workflows.metadata_json @> EXCLUDED.metadata_json)
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
            (
                workflow.get("id"),
                workflow["workflow_type"],
                workflow.get("enabled"),
                workflow.get("paused"),
                _json_object(workflow.get("schedule_json")),
                workflow.get("timezone"),
                workflow.get("next_run_at"),
                _json_object(workflow.get("metadata_json")),
            ),
        )
        mutated = row is not None
        if row is None:
            row = self.get_scheduler_workflow(str(workflow["workflow_type"]))
        if row is None:
            raise ContinuityStoreInvariantError("upsert_scheduler_workflow could not resolve the persisted workflow")
        if mutated:
            self._append_mutation_event(
                event_type="scheduler.workflow_upserted",
                actor_type=actor_type,
                target_type="scheduler_workflow",
                target_id=row["id"],
                payload={
                    "operation": "upsert",
                    "workflow_type": str(row["workflow_type"]),
                    "enabled": bool(row["enabled"]),
                    "paused": bool(row["paused"]),
                },
            )
        return row

    def update_scheduler_workflow(
        self, *, workflow_type: str, patch: JsonObject, actor_type: str = "system"
    ) -> VNextRow:
        result_keys = {
            "last_run_id",
            "last_run_at",
            "last_result",
            "last_error",
            "next_run_at",
        }
        preserve_live_claim = "last_run_id" in patch and set(patch) <= result_keys
        row = self._fetch_one(
            "update_scheduler_workflow",
            f"""
                UPDATE scheduler_workflows
                SET enabled = COALESCE(%s, enabled),
                    paused = COALESCE(%s, paused),
                    schedule_json = COALESCE(%s, schedule_json),
                    timezone = COALESCE(%s, timezone),
                    next_run_at = CASE
                      WHEN %s THEN %s::timestamptz
                      ELSE next_run_at
                    END,
                    last_run_id = COALESCE(%s::uuid, last_run_id),
                    last_run_at = COALESCE(%s::timestamptz, last_run_at),
                    last_result = COALESCE(%s, last_result),
                    last_error = CASE
                      WHEN %s THEN %s
                      ELSE last_error
                    END,
                    metadata_json = COALESCE(%s, metadata_json),
                    claim_token = CASE WHEN %s THEN claim_token ELSE NULL END,
                    claim_version = claim_version + CASE WHEN %s THEN 0 ELSE 1 END,
                    claim_expires_at = CASE WHEN %s THEN claim_expires_at ELSE NULL END,
                    updated_at = clock_timestamp()
                WHERE workflow_type = %s
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
            (
                patch.get("enabled"),
                patch.get("paused"),
                _json_object(patch["schedule_json"]) if "schedule_json" in patch else None,
                patch.get("timezone"),
                "next_run_at" in patch,
                patch.get("next_run_at"),
                patch.get("last_run_id"),
                patch.get("last_run_at"),
                patch.get("last_result"),
                "last_error" in patch,
                patch.get("last_error"),
                _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                preserve_live_claim,
                preserve_live_claim,
                preserve_live_claim,
                workflow_type,
            ),
        )
        self._append_mutation_event(
            event_type="scheduler.workflow_updated",
            actor_type=actor_type,
            target_type="scheduler_workflow",
            target_id=row["id"],
            payload={"operation": "update", "workflow_type": workflow_type, "changes": patch},
        )
        return row

    def get_scheduler_workflow(self, workflow_type: str) -> VNextRow | None:
        return self._fetch_optional_one(
            f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE workflow_type = %s
                """,
            (workflow_type,),
        )

    def list_scheduler_workflows(self) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                ORDER BY workflow_type ASC
                """
        )

    def create_scheduler_run(self, run: JsonObject, *, actor_type: str = "scheduler") -> VNextRow:
        row = self._fetch_one(
            "create_scheduler_run",
            f"""
                INSERT INTO scheduler_runs (
                  id,
                  user_id,
                  workflow_id,
                  workflow_type,
                  status,
                  triggered_by,
                  trace_id,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  scheduled_for,
                  policy_decision_json,
                  agent_identity_json,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  COALESCE(%s, 'started'),
                  COALESCE(%s, 'scheduler'),
                  %s,
                  %s,
                  COALESCE(%s, 0),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {SCHEDULER_RUN_COLUMNS}
                """,
            (
                run.get("id"),
                run.get("workflow_id"),
                run["workflow_type"],
                run.get("status"),
                run.get("triggered_by"),
                run["trace_id"],
                run.get("claim_token"),
                run.get("claim_version"),
                run.get("claim_expires_at"),
                run.get("scheduled_for"),
                _json_object(run.get("policy_decision_json")),
                _json_object(run.get("agent_identity_json")),
                _json_object(run.get("metadata_json")),
            ),
        )
        self._append_mutation_event(
            event_type="scheduler.run_started",
            actor_type=actor_type,
            target_type="scheduler_run",
            target_id=row["id"],
            trace_id=str(row["trace_id"]),
            run_id=str(row["id"]),
            payload={"workflow_type": str(row["workflow_type"]), "triggered_by": str(row["triggered_by"])},
        )
        return row

    def update_scheduler_run(self, *, run_id: str, patch: JsonObject, actor_type: str = "scheduler") -> VNextRow:
        row = self._fetch_one(
            "update_scheduler_run",
            f"""
                UPDATE scheduler_runs
                SET status = COALESCE(%s, status),
                    finished_at = CASE
                      WHEN %s IN ('succeeded', 'failed') THEN clock_timestamp()
                      ELSE finished_at
                    END,
                    artifact_id = COALESCE(%s::uuid, artifact_id),
                    error_message = COALESCE(%s, error_message),
                    policy_decision_json = COALESCE(%s, policy_decision_json),
                    agent_identity_json = COALESCE(%s, agent_identity_json),
                    metadata_json = CASE
                      WHEN %s::jsonb IS NULL THEN metadata_json
                      ELSE metadata_json || %s::jsonb
                    END
                WHERE id = %s::uuid
                RETURNING {SCHEDULER_RUN_COLUMNS}
                """,
            (
                patch.get("status"),
                patch.get("status"),
                patch.get("artifact_id"),
                patch.get("error_message"),
                _json_object(patch["policy_decision_json"]) if "policy_decision_json" in patch else None,
                _json_object(patch["agent_identity_json"]) if "agent_identity_json" in patch else None,
                _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
                run_id,
            ),
        )
        event_type = (
            "scheduler.run_succeeded"
            if row["status"] == "succeeded"
            else "scheduler.run_failed"
            if row["status"] == "failed"
            else "scheduler.run_updated"
        )
        self._append_mutation_event(
            event_type=event_type,
            actor_type=actor_type,
            target_type="scheduler_run",
            target_id=row["id"],
            trace_id=str(row["trace_id"]),
            run_id=str(row["id"]),
            payload={
                "workflow_type": str(row["workflow_type"]),
                "status": str(row["status"]),
                "artifact_id": str(row["artifact_id"]) if row.get("artifact_id") is not None else None,
                "error_message": row.get("error_message"),
            },
        )
        return row

    def list_scheduler_runs(self, *, workflow_type: str | None = None, limit: int = 20) -> list[VNextRow]:
        return self._fetch_all(
            f"""
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM scheduler_runs
                WHERE (%s::text IS NULL OR workflow_type = %s)
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
            (workflow_type, workflow_type, limit),
        )

    def claim_due_scheduler_workflow(
        self,
        *,
        checked_at: datetime,
        lease_expires_at: datetime,
        triggered_by: str,
        policy_decision_json: JsonObject | None = None,
        agent_identity_json: JsonObject | None = None,
    ) -> VNextRow | None:
        """Atomically claim the next due workflow and create its durable run."""

        candidate = self._fetch_optional_one(
            f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE enabled = true
                  AND paused = false
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                ORDER BY next_run_at ASC, workflow_type ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
            (checked_at, checked_at),
        )
        if candidate is None:
            return None
        workflow_type = str(candidate["workflow_type"])
        if not self.try_scheduler_workflow_lock(workflow_type):
            return None
        # Re-read after both locks.  A configuration transaction may have
        # changed enabled/paused/next_run_at between the daemon's snapshot and
        # this claim attempt.
        current = self._fetch_optional_one(
            f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE id = %s::uuid
                  AND enabled = true
                  AND paused = false
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                FOR UPDATE
                """,
            (str(candidate["id"]), checked_at, checked_at),
        )
        if current is None:
            return None
        scheduled_for = current["next_run_at"]
        claim_token = str(uuid4())
        claimed_workflow = self._fetch_one(
            "claim_due_scheduler_workflow",
            f"""
                UPDATE scheduler_workflows
                SET claim_token = %s,
                    claim_version = claim_version + 1,
                    claim_expires_at = %s::timestamptz,
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND enabled = true
                  AND paused = false
                  AND next_run_at = %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
            (
                claim_token,
                lease_expires_at,
                str(current["id"]),
                scheduled_for,
                checked_at,
            ),
        )
        claim_version = int(cast(int, claimed_workflow["claim_version"]))
        trace_id = str(uuid4())
        run = self.create_scheduler_run(
            {
                "workflow_id": claimed_workflow["id"],
                "workflow_type": workflow_type,
                "status": "started",
                "triggered_by": triggered_by,
                "trace_id": trace_id,
                "claim_token": claim_token,
                "claim_version": claim_version,
                "claim_expires_at": lease_expires_at,
                "scheduled_for": scheduled_for,
                "policy_decision_json": policy_decision_json or {},
                "agent_identity_json": agent_identity_json or {},
                "metadata_json": {
                    "durable_claim": True,
                    "scheduled_for": scheduled_for,
                },
            },
            actor_type=triggered_by,
        )
        return {
            "workflow": claimed_workflow,
            "run": run,
            "claim_token": claim_token,
            "claim_version": claim_version,
            "claim_expires_at": lease_expires_at,
            "scheduled_for": scheduled_for,
        }

    def heartbeat_scheduler_claim(
        self,
        *,
        run_id: str,
        claim_token: str,
        claim_version: int,
        lease_expires_at: datetime,
    ) -> bool:
        row = self._fetch_one(
            "heartbeat_scheduler_claim",
            """
                WITH eligible AS (
                  SELECT r.id AS run_id, r.workflow_id
                  FROM scheduler_runs AS r
                  JOIN scheduler_workflows AS w
                    ON w.id = r.workflow_id
                   AND w.user_id = r.user_id
                  WHERE r.id = %s::uuid
                    AND r.status = 'started'
                    AND r.claim_token = %s
                    AND r.claim_version = %s
                    AND r.claim_expires_at > clock_timestamp()
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                    AND w.claim_expires_at > clock_timestamp()
                  FOR UPDATE OF r, w
                ),
                updated_run AS (
                  UPDATE scheduler_runs AS r
                  SET claim_expires_at = %s::timestamptz
                  FROM eligible AS e
                  WHERE r.id = e.run_id
                  RETURNING r.id
                ),
                updated_workflow AS (
                  UPDATE scheduler_workflows AS w
                  SET claim_expires_at = %s::timestamptz,
                      updated_at = clock_timestamp()
                  FROM eligible AS e
                  WHERE w.id = e.workflow_id
                    AND EXISTS (SELECT 1 FROM updated_run)
                  RETURNING w.id
                )
                SELECT (
                  EXISTS (SELECT 1 FROM updated_run)
                  AND EXISTS (SELECT 1 FROM updated_workflow)
                ) AS renewed
                """,
            (
                run_id,
                claim_token,
                claim_version,
                claim_token,
                claim_version,
                lease_expires_at,
                lease_expires_at,
            ),
        )
        return bool(row.get("renewed"))

    def lock_scheduler_claim_for_publish(
        self,
        *,
        run_id: str,
        claim_token: str,
        claim_version: int,
    ) -> bool:
        """Lock and validate the live claim before publishing staged writes."""

        row = self._fetch_optional_one(
            """
                SELECT r.id AS run_id
                FROM scheduler_runs AS r
                JOIN scheduler_workflows AS w
                  ON w.id = r.workflow_id
                 AND w.user_id = r.user_id
                WHERE r.id = %s::uuid
                  AND r.status = 'started'
                  AND r.claim_token = %s
                  AND r.claim_version = %s
                  AND r.claim_expires_at > clock_timestamp()
                  AND w.claim_token = %s
                  AND w.claim_version = %s
                  AND w.claim_expires_at > clock_timestamp()
                FOR UPDATE OF r, w
                """,
            (
                run_id,
                claim_token,
                claim_version,
                claim_token,
                claim_version,
            ),
        )
        return row is not None

    def finalize_scheduler_claim(
        self,
        *,
        run_id: str,
        claim_token: str,
        claim_version: int,
        status: str,
        artifact_id: str | None,
        error_message: str | None,
        next_run_at: str | None,
        metadata_json: JsonObject,
        actor_type: str = "scheduler",
    ) -> VNextRow | None:
        """Finalize only the still-live matching scheduler fence."""

        if status not in {"succeeded", "failed"}:
            raise ValueError("status must be succeeded or failed")
        row = self._fetch_optional_one(
            f"""
                WITH eligible AS (
                  SELECT r.id AS run_id, r.workflow_id
                  FROM scheduler_runs AS r
                  JOIN scheduler_workflows AS w
                    ON w.id = r.workflow_id
                   AND w.user_id = r.user_id
                  WHERE r.id = %s::uuid
                    AND r.status = 'started'
                    AND r.claim_token = %s
                    AND r.claim_version = %s
                    AND r.claim_expires_at > clock_timestamp()
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                    AND w.claim_expires_at > clock_timestamp()
                  FOR UPDATE OF r, w
                ),
                updated_run AS (
                  UPDATE scheduler_runs AS r
                  SET status = %s,
                      finished_at = clock_timestamp(),
                      artifact_id = %s::uuid,
                      error_message = %s,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      metadata_json = r.metadata_json || %s::jsonb
                  FROM eligible AS e
                  WHERE r.id = e.run_id
                  RETURNING r.*
                ),
                updated_workflow AS (
                  UPDATE scheduler_workflows AS w
                  SET last_run_id = r.id,
                      last_run_at = r.finished_at,
                      last_result = r.status,
                      last_error = r.error_message,
                      next_run_at = %s::timestamptz,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      updated_at = clock_timestamp()
                  FROM updated_run AS r
                  WHERE w.id = r.workflow_id
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                  RETURNING w.id
                )
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM updated_run
                WHERE EXISTS (SELECT 1 FROM updated_workflow)
                """,
            (
                run_id,
                claim_token,
                claim_version,
                claim_token,
                claim_version,
                status,
                artifact_id,
                error_message,
                _json_object(metadata_json),
                next_run_at,
                claim_token,
                claim_version,
            ),
        )
        if row is None:
            return None
        self._append_mutation_event(
            event_type="scheduler.run_succeeded" if status == "succeeded" else "scheduler.run_failed",
            actor_type=actor_type,
            target_type="scheduler_run",
            target_id=row["id"],
            trace_id=str(row["trace_id"]),
            run_id=str(row["id"]),
            payload={
                "workflow_type": str(row["workflow_type"]),
                "status": status,
                "artifact_id": artifact_id,
                "error_message": error_message,
                "claim_version": claim_version,
            },
        )
        return row

    def reap_expired_scheduler_claims(
        self,
        *,
        reference_time: datetime,
        limit: int = 100,
        actor_type: str = "scheduler",
    ) -> list[VNextRow]:
        """Fence abandoned runs and release their workflow leases."""

        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._fetch_all(
            f"""
                WITH expired AS MATERIALIZED (
                  SELECT r.id, r.workflow_id, r.claim_token, r.claim_version
                  FROM scheduler_runs AS r
                  WHERE r.status = 'started'
                    AND r.claim_token IS NOT NULL
                    AND r.claim_expires_at <= %s::timestamptz
                  ORDER BY r.claim_expires_at ASC, r.id ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT %s
                ),
                updated_runs AS (
                  UPDATE scheduler_runs AS r
                  SET status = 'failed',
                      finished_at = clock_timestamp(),
                      error_message = COALESCE(r.error_message, 'scheduler claim lease expired'),
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      metadata_json = r.metadata_json || jsonb_build_object(
                        'claim_reaped', true,
                        'claim_reaped_at', %s::timestamptz
                      )
                  FROM expired AS e
                  WHERE r.id = e.id
                    AND r.status = 'started'
                    AND r.claim_token = e.claim_token
                    AND r.claim_version = e.claim_version
                  RETURNING r.*, e.claim_token AS expired_claim_token,
                    e.claim_version AS expired_claim_version
                ),
                cleared_workflows AS (
                  UPDATE scheduler_workflows AS w
                  SET last_run_id = r.id,
                      last_run_at = r.finished_at,
                      last_result = 'failed',
                      last_error = r.error_message,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      updated_at = clock_timestamp()
                  FROM updated_runs AS r
                  WHERE w.id = r.workflow_id
                    AND w.claim_token = r.expired_claim_token
                    AND w.claim_version = r.expired_claim_version
                  RETURNING w.id
                )
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM updated_runs
                ORDER BY finished_at ASC, id ASC
                """,
            (reference_time, limit, reference_time),
        )
        for row in rows:
            self._append_mutation_event(
                event_type="scheduler.run_failed",
                actor_type=actor_type,
                target_type="scheduler_run",
                target_id=row["id"],
                trace_id=str(row["trace_id"]),
                run_id=str(row["id"]),
                payload={
                    "workflow_type": str(row["workflow_type"]),
                    "status": "failed",
                    "error_message": row.get("error_message"),
                    "claim_reaped": True,
                },
            )
        return rows

    def try_scheduler_workflow_lock(self, workflow_type: str) -> bool:
        row = self._fetch_one(
            "try_scheduler_workflow_lock",
            """
                SELECT pg_try_advisory_xact_lock(
                  hashtextextended(
                    concat_ws(':', 'vnext_scheduler', app.current_user_id()::text, %s::text),
                    17
                  )
                ) AS acquired
                """,
            (workflow_type,),
        )
        return bool(row.get("acquired"))

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """Run one unit of writes that lands whole or leaves no row.

        An exception rolls back everything written inside the block, including
        the events it appended, and is re-raised for the caller to report. The
        enclosing transaction stays open and commits once, when its owner
        commits. The rollback also clears the aborted state a failed statement
        leaves in the transaction, so the caller can log the failure and carry
        on with the next unit.

        This is a plain ``SAVEPOINT`` and never a transaction of its own. On a
        pooled product connection the transaction is already open. On any other
        connection that is not in autocommit mode, the first statement begins
        the transaction and its owner commits it. On an autocommit connection
        Postgres refuses a savepoint outside a transaction block, so it fails
        loudly instead of committing at the end of the block.
        """

        name = f"alice_savepoint_{next(_SAVEPOINT_NAMES)}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            try:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
            except psycopg.Error:
                # The connection is gone or the transaction already ended, so
                # there is no savepoint left to roll back to. The caller's
                # error is the one to report, not this one.
                pass
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")


__all__ = [
    "PostgresVNextStore",
    "VNextRow",
]
