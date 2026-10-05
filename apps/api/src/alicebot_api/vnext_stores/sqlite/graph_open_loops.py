"""SQLite graph, entity, and open-loop store seam."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from alicebot_api.source_search_limits import literal_match_operand
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_entity_names import ENTITY_IMMUTABLE_PATCH_FIELDS, normalize_entity_name
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER, normalize_project_scope
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.sqlite.columns import (
    ENTITY_COLUMNS,
    ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS,
    OPEN_LOOP_COLUMNS,
)
from alicebot_api.vnext_stores.sqlite.source_retirement import open_loops_naming_sources
from alicebot_api.vnext_stores.sqlite.primitives import (
    _iso_or_none,
    _iso_or_now,
    _json_list_text,
    _json_object_text,
    _new_id,
    _sorted_field_names,
    _utc_now_iso,
    _uuid_text,
)
from alicebot_api.vnext_label_writes import takes_label_lock
from alicebot_api.vnext_stores.sqlite.query_predicates import (
    _project_scope_value_sqlite,
    CTE_MATERIALIZED_HINT,
    _project_view_partition_sql,
    _sqlite_ascii_literal_contains_sql,
    _stated_exclusion,
)

VNextRow = dict[str, object]

def create_graph_edge(self, edge: JsonObject, *, actor_type: str = "system") -> VNextRow:
    edge_id = _new_id(edge.get("id"))
    now = _utc_now_iso()
    observed_at = _iso_or_none(edge.get("observed_at"))
    metadata_value = edge.get("metadata_json")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    if observed_at is None:
        observed_at = now
        metadata.setdefault("observed_at_source", "now")
    valid_from = _iso_or_none(edge.get("valid_from")) or observed_at
    self._execute(
        """
                INSERT INTO graph_edges (
                  id,
                  user_id,
                  from_type,
                  from_id,
                  to_type,
                  to_id,
                  edge_type,
                  confidence,
                  explanation,
                  created_by,
                  created_at,
                  observed_at,
                  valid_from,
                  valid_to,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            edge_id,
            self.user_id,
            edge["from_type"],
            edge["from_id"],
            edge["to_type"],
            edge["to_id"],
            edge["edge_type"],
            edge.get("confidence", 0.5),
            edge.get("explanation"),
            edge.get("created_by", actor_type),
            now,
            observed_at,
            valid_from,
            _iso_or_none(edge.get("valid_to")),
            _json_object_text(metadata),
        ),
    )
    row = self._get_row("create_graph_edge", "graph_edges", GRAPH_EDGE_COLUMNS, edge_id)
    self._append_mutation_event(
        event_type="graph_edge.created",
        actor_type=actor_type,
        target_type="graph_edge",
        target_id=row["id"],
        payload={"operation": "create", "edge_type": str(row["edge_type"])},
    )
    return row

def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {", ".join(GRAPH_EDGE_COLUMNS)}
                FROM graph_edges
                WHERE user_id = ?
                  AND (? IS NULL OR from_id = ?)
                  AND (? IS NULL OR to_id = ?)
                  AND valid_to IS NULL
                ORDER BY created_at DESC, id DESC
                """,
        (self.user_id, from_id, from_id, to_id, to_id),
    )

def list_memory_entity_edges(
    self,
    *,
    entity_ids: Sequence[str],
    edge_types: Sequence[str] = ("mentions", "about"),
) -> list[VNextRow]:
    ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
    types = list(dict.fromkeys(str(edge_type) for edge_type in edge_types if edge_type))
    if not ids or not types:
        return []
    id_placeholders = self._placeholders(ids)
    type_placeholders = self._placeholders(types)
    return self._fetch_all(
        f"""
                SELECT {", ".join(GRAPH_EDGE_COLUMNS)}
                FROM graph_edges
                WHERE user_id = ?
                  AND valid_to IS NULL
                  AND edge_type IN ({type_placeholders})
                  AND (
                    (from_type = 'memory' AND to_type = 'entity' AND to_id IN ({id_placeholders}))
                    OR
                    (from_type = 'entity' AND to_type = 'memory' AND from_id IN ({id_placeholders}))
                  )
                ORDER BY created_at DESC, id DESC
                """,
        (self.user_id, *types, *ids, *ids),
    )

def expire_edge(self, *, edge_id: str, actor_type: str = "system") -> VNextRow:
    now = _utc_now_iso()
    cursor = self._execute(
        """
                UPDATE graph_edges
                SET valid_to = ?
                WHERE id = ?
                  AND user_id = ?
                  AND valid_to IS NULL
                """,
        (now, str(edge_id), self.user_id),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError("expire_edge did not update an active edge")
    row = self._get_row("expire_edge", "graph_edges", GRAPH_EDGE_COLUMNS, str(edge_id))
    self._append_mutation_event(
        event_type="graph_edge.expired",
        actor_type=actor_type,
        target_type="graph_edge",
        target_id=row["id"],
        payload={"operation": "expire"},
    )
    return row

def list_edges_as_of(self, at: object, *, limit: int = 50) -> list[VNextRow]:
    """Edges that were in effect at ``at``: valid_from <= at < valid_to.

        Edges written before the temporal slice carry NULL ``valid_from``
        and are excluded (their event time was never recorded).
        """
    at_iso = _iso_or_none(at)
    return self._fetch_all(
        f"""
                SELECT {", ".join(GRAPH_EDGE_COLUMNS)}
                FROM graph_edges
                WHERE user_id = ?
                  AND valid_from IS NOT NULL
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
                ORDER BY valid_from DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        (self.user_id, at_iso, at_iso, limit),
    )

def create_entity(self, entity: JsonObject, *, actor_type: str = "system") -> VNextRow:
    entity_id = _new_id(entity.get("id"))
    name = str(entity["name"])
    normalized_name = str(entity.get("normalized_name") or normalize_entity_name(name))
    now = _utc_now_iso()
    self._execute(
        """
                INSERT INTO vnext_entities (
                  id,
                  user_id,
                  entity_type,
                  name,
                  normalized_name,
                  aliases,
                  metadata_json,
                  first_observed_at,
                  last_observed_at,
                  mention_count,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            entity_id,
            self.user_id,
            entity["entity_type"],
            name,
            normalized_name,
            _json_list_text(entity.get("aliases")),
            _json_object_text(entity.get("metadata_json")),
            _iso_or_none(entity.get("first_observed_at")),
            _iso_or_none(entity.get("last_observed_at")),
            entity.get("mention_count", 0),
            now,
            now,
        ),
    )
    row = self._get_row("create_entity", "vnext_entities", ENTITY_COLUMNS, entity_id)
    self._append_mutation_event(
        event_type="entity.created",
        actor_type=actor_type,
        target_type="entity",
        target_id=row["id"],
        payload={
            "operation": "create",
            "entity_type": str(row["entity_type"]),
            "fields": _sorted_field_names(entity),
        },
    )
    return row

def get_entity(self, entity_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(ENTITY_COLUMNS)}
                FROM vnext_entities
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (str(entity_id), self.user_id),
    )

def get_entity_by_normalized_name(self, entity_type: str, normalized_name: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(ENTITY_COLUMNS)}
                FROM vnext_entities
                WHERE entity_type = ?
                  AND normalized_name = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                LIMIT 1
                """,
        (entity_type, normalized_name, self.user_id),
    )

def find_entities_by_names(self, normalized_names: tuple[str, ...]) -> list[VNextRow]:
    """One-round-trip resolution lookup for query-time entity linking.

        Matches ``normalized_name`` OR any element of the ``aliases``
        JSON array (via ``json_each``). Alias values are expected to
        already be normalized via ``normalize_entity_name`` -- matching
        is exact string equality. Most-mentioned entities sort first so
        callers can take the top match per name.
        """
    if not normalized_names:
        return []
    names = [str(name) for name in normalized_names]
    placeholders = self._placeholders(names)
    return self._fetch_all(
        f"""
                SELECT {", ".join(ENTITY_COLUMNS)}
                FROM vnext_entities
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND (
                    normalized_name IN ({placeholders})
                    OR EXISTS (
                      SELECT 1
                      FROM json_each(vnext_entities.aliases)
                      WHERE json_each.value IN ({placeholders})
                    )
                  )
                ORDER BY mention_count DESC, updated_at DESC, id DESC
                """,
        (self.user_id, *names, *names),
    )

def list_entities(
    self,
    *,
    entity_type: str | None = None,
    limit: int = 100,
) -> list[VNextRow]:
    type_sql = ""
    params: list[object] = [self.user_id]
    if entity_type is not None:
        type_sql = " AND entity_type = ?"
        params.append(entity_type)
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(ENTITY_COLUMNS)}
                FROM vnext_entities
                WHERE user_id = ?{type_sql}
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
    )

def update_entity(self, *, entity_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    immutable = sorted(set(patch) & ENTITY_IMMUTABLE_PATCH_FIELDS)
    if immutable:
        raise ContinuityStoreInvariantError(f"update_entity cannot modify immutable fields: {', '.join(immutable)}")
    cursor = self._execute(
        """
                UPDATE vnext_entities
                SET name = COALESCE(?, name),
                    aliases = COALESCE(?, aliases),
                    metadata_json = COALESCE(?, metadata_json),
                    mention_count = COALESCE(?, mention_count),
                    first_observed_at = COALESCE(?, first_observed_at),
                    last_observed_at = COALESCE(?, last_observed_at),
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (
            patch.get("name"),
            _json_list_text(patch["aliases"]) if "aliases" in patch else None,
            _json_object_text(patch["metadata_json"]) if "metadata_json" in patch else None,
            patch.get("mention_count"),
            _iso_or_none(patch.get("first_observed_at")),
            _iso_or_none(patch.get("last_observed_at")),
            _utc_now_iso(),
            str(entity_id),
            self.user_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError(
            "update_entity did not return a row from the database",
        )
    row = self._get_row("update_entity", "vnext_entities", ENTITY_COLUMNS, str(entity_id))
    self._append_mutation_event(
        event_type="entity.updated",
        actor_type=actor_type,
        target_type="entity",
        target_id=row["id"],
        payload={"operation": "update", "changes": patch},
    )
    return row

def record_entity_mention(
    self,
    *,
    entity_id: str,
    observed_at: object,
    source_id: str | None = None,
    actor_type: str = "system",
) -> VNextRow:
    """Count a mention and widen the observation window.

        ``first_observed_at``/``last_observed_at`` take COALESCE min/max
        semantics: out-of-order observations only ever widen the window.
        MIN/MAX over the store's ISO-8601 UTC "Z" TEXT convention orders
        lexicographically like the Postgres LEAST/GREATEST on
        timestamptz.
        """
    observed = _iso_or_none(observed_at)
    if observed is None:
        raise ContinuityStoreInvariantError("record_entity_mention requires observed_at")
    cursor = self._execute(
        """
                UPDATE vnext_entities
                SET mention_count = mention_count + 1,
                    first_observed_at = MIN(COALESCE(first_observed_at, ?), ?),
                    last_observed_at = MAX(COALESCE(last_observed_at, ?), ?),
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (
            observed,
            observed,
            observed,
            observed,
            _utc_now_iso(),
            str(entity_id),
            self.user_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError(
            "record_entity_mention did not return a row from the database",
        )
    row = self._get_row("record_entity_mention", "vnext_entities", ENTITY_COLUMNS, str(entity_id))
    self._append_mutation_event(
        event_type="entity.mention_recorded",
        actor_type=actor_type,
        target_type="entity",
        target_id=row["id"],
        payload={
            "operation": "record_mention",
            "observed_at": observed,
            "source_id": source_id,
        },
    )
    return row

def record_relationship_change(
    self,
    *,
    entity_id: str,
    relationship_type: str,
    changed_at: object | None = None,
    source_id: str | None = None,
    metadata_json: JsonObject | None = None,
    actor_type: str = "system",
) -> VNextRow:
    """Append a relationship transition and update the entity.

        update_person overwrites relationship_type in place; this keeps
        the "advisor -> investor" history in the append-only
        entity_relationship_events table while the entity's
        metadata_json carries the current relationship_type.
        """
    entity = self.get_entity(entity_id)
    if entity is None:
        raise ContinuityStoreInvariantError("record_relationship_change requires an existing entity")
    current_metadata = cast(dict[str, object], entity.get("metadata_json") or {})
    before_value = current_metadata.get("relationship_type")
    before = None if before_value is None else str(before_value)
    event_id = _new_id(None)
    self._execute(
        """
                INSERT INTO entity_relationship_events (
                  id,
                  user_id,
                  entity_id,
                  relationship_type_before,
                  relationship_type_after,
                  changed_at,
                  source_id,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            event_id,
            self.user_id,
            str(entity_id),
            before,
            relationship_type,
            _iso_or_now(changed_at),
            _uuid_text(source_id),
            _json_object_text(metadata_json),
        ),
    )
    # Shallow merge mirrors the Postgres jsonb `||` update.
    merged_metadata = {**current_metadata, "relationship_type": relationship_type}
    self._execute(
        """
                UPDATE vnext_entities
                SET metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (_json_object_text(merged_metadata), _utc_now_iso(), str(entity_id), self.user_id),
    )
    row = self._get_row(
        "record_relationship_change",
        "entity_relationship_events",
        ENTITY_RELATIONSHIP_EVENT_COLUMNS,
        event_id,
    )
    self._append_mutation_event(
        event_type="entity.relationship_changed",
        actor_type=actor_type,
        target_type="entity",
        target_id=str(entity_id),
        payload={
            "operation": "record_relationship_change",
            "relationship_type_before": before,
            "relationship_type_after": relationship_type,
            "relationship_event_id": str(row["id"]),
            "source_id": source_id,
        },
    )
    return row

def list_relationship_events(self, entity_id: str) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {", ".join(ENTITY_RELATIONSHIP_EVENT_COLUMNS)}
                FROM entity_relationship_events
                WHERE entity_id = ?
                  AND user_id = ?
                ORDER BY changed_at DESC, id DESC
                """,
        (str(entity_id), self.user_id),
    )

@takes_label_lock
def create_open_loop(self, loop: JsonObject, *, actor_type: str = "system") -> VNextRow:
    from alicebot_api.vnext_label_writes import apply_insert_floor, remember_floor_event

    loop, floor_event = apply_insert_floor(self, "open_loop", loop)
    loop_id = _new_id(loop.get("id"))
    now = _utc_now_iso()
    self._execute(
        """
                INSERT INTO open_loops (
                  id,
                  user_id,
                  memory_id,
                  title,
                  status,
                  opened_at,
                  due_at,
                  resolved_at,
                  resolution_note,
                  description,
                  priority,
                  project_id,
                  person_id,
                  source_id,
                  closed_at,
                  domain,
                  sensitivity,
                  metadata_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            loop_id,
            self.user_id,
            _uuid_text(loop.get("memory_id")),
            loop["title"],
            loop.get("status", "open"),
            _iso_or_now(loop.get("opened_at")),
            _iso_or_none(loop.get("due_at")),
            _iso_or_none(loop.get("resolved_at")),
            loop.get("resolution_note"),
            loop.get("description"),
            loop.get("priority", "normal"),
            _uuid_text(loop.get("project_id")),
            _uuid_text(loop.get("person_id")),
            _uuid_text(loop.get("source_id")),
            _iso_or_none(loop.get("closed_at")),
            loop.get("domain", "unknown"),
            loop.get("sensitivity", "unknown"),
            _json_object_text(loop.get("metadata_json")),
            now,
            now,
        ),
    )
    row = self._get_row("create_open_loop", "open_loops", OPEN_LOOP_COLUMNS, loop_id)
    self._append_mutation_event(
        event_type="open_loop.created",
        actor_type=actor_type,
        target_type="open_loop",
        target_id=row["id"],
        payload={"operation": "create", "fields": _sorted_field_names(loop)},
    )
    remember_floor_event(self, floor_event, row["id"])
    return row

def upsert_open_loop_by_automation_digest(
    self,
    loop: JsonObject,
    *,
    digest: str,
    actor_type: str = "system",
) -> VNextRow:
    normalized_digest = str(digest).strip()
    if normalized_digest == "":
        raise ValueError("digest must not be empty")
    existing = self.find_open_loop_by_automation_digest(
        digest=normalized_digest,
        project_id=str(loop["project_id"]) if loop.get("project_id") is not None else None,
        person_id=str(loop["person_id"]) if loop.get("person_id") is not None else None,
    )
    if existing is not None:
        return existing
    metadata_value = loop.get("metadata_json")
    metadata: JsonObject = dict(metadata_value) if isinstance(metadata_value, dict) else {}
    metadata.update(
        {
            "automation_digest": normalized_digest,
            "idempotency_digest": normalized_digest,
        }
    )
    record: JsonObject = {**loop, "metadata_json": metadata}
    try:
        return self.create_open_loop(record, actor_type=actor_type)
    except sqlite3.IntegrityError:
        existing = self.find_open_loop_by_automation_digest(
            digest=normalized_digest,
            project_id=str(loop["project_id"]) if loop.get("project_id") is not None else None,
            person_id=str(loop["person_id"]) if loop.get("person_id") is not None else None,
        )
        if existing is None:
            raise
        return existing

def get_open_loop(self, loop_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(OPEN_LOOP_COLUMNS)}
                FROM open_loops
                WHERE id = ?
                  AND user_id = ?
                """,
        (str(loop_id), self.user_id),
    )

def find_open_loop_by_automation_digest(
    self,
    *,
    digest: str,
    project_id: str | None = None,
    person_id: str | None = None,
) -> VNextRow | None:
    normalized_digest = str(digest).strip()
    if not normalized_digest:
        return None
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(OPEN_LOOP_COLUMNS)}
                FROM open_loops
                WHERE user_id = ?
                  AND COALESCE(
                    json_extract(metadata_json, '$.idempotency_digest'),
                    json_extract(metadata_json, '$.automation_digest')
                  ) = ?
                  AND (? IS NULL OR alice_project_scope_value(project_id) = ?)
                  AND (? IS NULL OR person_id = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (
            self.user_id,
            normalized_digest,
            project_id,
            _project_scope_value_sqlite(project_id),
            person_id,
            person_id,
        ),
    )

def list_open_loops_referencing_source(self, *, source_id: str, limit: int = 500) -> list[VNextRow]:
    """Open loops that name one source, by the spellings ``cited_source_ids`` names, before LIMIT."""

    if limit < 1:
        raise ValueError("limit must be positive")
    return open_loops_naming_sources(self, [source_id]).get(str(source_id), [])[:limit]

def list_open_loops(
    self,
    *,
    status: str | None = "open",
    statuses: Sequence[str] | None = None,
    query: str | None = None,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    project_id: str | None = None,
    person_id: str | None = None,
    limit: int = 8,
    scope_projects: Sequence[str] | None = None,
    scope_people: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
    exclude_global_domains: Sequence[str] | None = None,
) -> list[VNextRow]:
    """List open loops.

    ``scope_projects`` may hold the reserved global marker (spec 6.1). With it,
    ``exclude_global_domains`` leaves out global loops in those domains before
    ``LIMIT``. It has no effect on a request that does not hold the marker. With the
    marker it must be stated, an empty tuple when nothing is left out: ``None`` raises.
    """

    domain_sql, domain_params = self._domain_clause(domains)
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
    scope_sql, scope_params = self._metadata_scope_clause(
        metadata_expression="metadata_json",
        scope_projects=tuple(normalize_project_scope(scope_projects or ())),
        scope_people=scope_people,
        direct_project_expression="project_id",
        direct_person_expression="person_id",
        event_time_expression="COALESCE(julianday(opened_at), julianday(updated_at), julianday(created_at))",
        scope_window_start=scope_window_start,
        scope_window_end=scope_window_end,
        domain_expression="domain",
        global_excluded_domains=_stated_exclusion(exclude_global_domains),
    )
    clauses = ["user_id = ?"]
    params: list[object] = [self.user_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if statuses is not None:
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return []
        clauses.append(f"status IN ({self._placeholders(normalized_statuses)})")
        params.extend(normalized_statuses)
    params.extend(domain_params)
    params.extend(sensitivity_params)
    params.extend(scope_params)
    extra_sql = ""
    if project_id is not None:
        extra_sql += " AND alice_project_scope_value(project_id) = ?"
        params.append(_project_scope_value_sqlite(project_id))
    if person_id is not None:
        extra_sql += " AND person_id = ?"
        params.append(str(person_id))
    query_sql = ""
    normalized_query = str(query).strip() if query is not None else ""
    if normalized_query:
        root_next_action = _sqlite_ascii_literal_contains_sql(
            "COALESCE(json_extract(metadata_json, '$.next_action'), '')"
        )
        nested_next_action = _sqlite_ascii_literal_contains_sql(
            "COALESCE(json_extract(metadata_json, '$.agentic_memory.next_action'), '')"
        )
        query_sql = (
            " AND ("
            + _sqlite_ascii_literal_contains_sql("COALESCE(title, '')")
            + " OR "
            + _sqlite_ascii_literal_contains_sql("COALESCE(description, '')")
            + " OR (json_type(metadata_json, '$.next_action') = 'text' AND "
            + root_next_action
            + ") OR (json_type(metadata_json, '$.agentic_memory.next_action') = 'text' AND "
            + nested_next_action
            + ")"
            + ")"
        )
        params.extend((literal_match_operand(normalized_query),) * 4)
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(OPEN_LOOP_COLUMNS)}
                FROM open_loops
                WHERE {" AND ".join(clauses)}{domain_sql}{sensitivity_sql}{scope_sql}{extra_sql}{query_sql}
                ORDER BY opened_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
    )

def list_open_loops_view_partitions(
    self,
    *,
    project_ids: Sequence[str],
    exclude_global_domains: Sequence[str],
    per_partition_limit: int,
    # No defaults for the domain filter and the sensitivity ceiling, as in
    # ``list_memories_view_partitions``.
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
    status: str | None = "open",
    statuses: Sequence[str] | None = None,
) -> tuple[list[VNextRow], list[VNextRow]]:
    """The project's open loops and the global open loops in one scan (spec 6.2, 12).

    Returns ``(project_rows, global_rows)``, newest first and at most
    ``per_partition_limit`` long each, under the filters ``list_open_loops`` applies.
    A loop is global when its scope holds no Alice project id, and global loops in
    ``exclude_global_domains`` are left out before the limit. One pass labels every
    row into a narrow common table expression, which is read once per label as a
    top-N and joined back for the full rows (see ``list_memories_view_partitions``).
    """

    if per_partition_limit < 1:
        raise ValueError("limit must be positive")
    domain_sql, domain_params = self._domain_clause(domains)
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
    clauses = ["user_id = ?"]
    params: list[object] = [self.user_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if statuses is not None:
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return [], []
        clauses.append(f"status IN ({self._placeholders(normalized_statuses)})")
        params.extend(normalized_statuses)
    params.extend(domain_params)
    params.extend(sensitivity_params)
    partition_sql, partition_params = _project_view_partition_sql(
        placeholders=self._placeholders,
        projects=(*project_ids, GLOBAL_PROJECT_MARKER),
        scope_expression="alice_project_scope_identity(metadata_json, project_id)",
        text_expressions=("metadata_json", "project_id"),
        domain_expression="domain",
        global_excluded_domains=tuple(sorted(exclude_global_domains)),
        floor_expression="alice_project_floor_identity(metadata_json)",
    )
    columns = ", ".join(f"l.{column}" for column in OPEN_LOOP_COLUMNS)
    rows = self._fetch_all(
        f"""
                WITH alice_labelled AS{CTE_MATERIALIZED_HINT} (
                  SELECT id, opened_at, created_at, {partition_sql} AS alice_partition
                  FROM open_loops
                  WHERE {" AND ".join(clauses)}{domain_sql}{sensitivity_sql}
                )
                SELECT {columns}, picked.alice_partition AS alice_partition
                FROM (
                  SELECT id, alice_partition FROM (
                    SELECT id, alice_partition FROM alice_labelled
                    WHERE alice_partition = 1
                    ORDER BY opened_at DESC, created_at DESC, id DESC
                    LIMIT ?
                  )
                  UNION ALL
                  SELECT id, alice_partition FROM (
                    SELECT id, alice_partition FROM alice_labelled
                    WHERE alice_partition = 0
                    ORDER BY opened_at DESC, created_at DESC, id DESC
                    LIMIT ?
                  )
                ) AS picked
                JOIN open_loops AS l ON l.id = picked.id
                ORDER BY picked.alice_partition DESC, l.opened_at DESC, l.created_at DESC, l.id DESC
                """,
        (*partition_params, *params, per_partition_limit, per_partition_limit),
    )
    project_rows: list[VNextRow] = []
    global_rows: list[VNextRow] = []
    for row in rows:
        partition = row.pop("alice_partition")
        (project_rows if partition == 1 else global_rows).append(row)
    return project_rows, global_rows

def list_open_loop_events(
    self,
    *,
    statuses: Sequence[str],
    scope_projects: Sequence[str] | None = None,
    query: str | None = None,
    occurred_at_start: datetime | None = None,
    occurred_at_end: datetime | None = None,
    limit: int = 20,
    exclude_global_domains: Sequence[str] | None = None,
    # No defaults for the domain filter and the sensitivity ceiling, as in
    # ``list_open_loops_view_partitions``: a caller states what it admits, and
    # ``None`` is the statement "no filter".
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
) -> list[VNextRow]:
    """Return scoped events for active loops without bounding loop age.

    ``scope_projects`` may hold the reserved global marker (spec 6.1). With it,
    ``exclude_global_domains`` leaves out events of global loops in those domains
    before ``LIMIT``, and it must be stated (an empty tuple leaves none out):
    ``None`` raises.

    ``domains`` and ``sensitivity_allowed`` are the rules ``list_open_loops``
    applies to the loop itself (a loop in an unlisted domain, or above the ceiling,
    has its events left out before ``LIMIT``), so an event the caller may not see
    never takes a place that one it may see needs. An empty ``sensitivity_allowed``
    admits nothing: it returns no rows and builds no ``sensitivity IN ()``.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    if sensitivity_allowed is not None and not sensitivity_allowed:
        return []
    normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
    if not normalized_statuses:
        return []
    project_sql, project_params = self._metadata_scope_clause(
        metadata_expression="loop.metadata_json",
        scope_projects=tuple(normalize_project_scope(scope_projects or ())),
        direct_project_expression="loop.project_id",
        event_time_expression="julianday(event.occurred_at)",
        domain_expression="loop.domain",
        global_excluded_domains=_stated_exclusion(exclude_global_domains),
    )
    domain_sql, domain_params = self._domain_clause(domains, prefix="loop.")
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed, prefix="loop.")
    clauses = [
        "event.user_id = ?",
        "event.target_type = 'open_loop'",
        f"loop.status IN ({self._placeholders(normalized_statuses)})",
    ]
    params: list[object] = [
        self.user_id,
        *normalized_statuses,
        *project_params,
        *domain_params,
        *sensitivity_params,
    ]
    scoped_where_sql = " AND ".join(clauses) + project_sql + domain_sql + sensitivity_sql
    query_sql = ""
    normalized_query = str(query).strip() if query is not None else ""
    if normalized_query:
        root_next_action = _sqlite_ascii_literal_contains_sql(
            "COALESCE(json_extract(loop.metadata_json, '$.next_action'), '')"
        )
        nested_next_action = _sqlite_ascii_literal_contains_sql(
            "COALESCE(json_extract(loop.metadata_json, '$.agentic_memory.next_action'), '')"
        )
        row_predicates = (
            _sqlite_ascii_literal_contains_sql("COALESCE(loop.title, '')")
            + " OR "
            + _sqlite_ascii_literal_contains_sql("COALESCE(loop.description, '')")
            + " OR (json_type(loop.metadata_json, '$.next_action') = 'text' AND "
            + root_next_action
            + ") OR (json_type(loop.metadata_json, '$.agentic_memory.next_action') = 'text' AND "
            + nested_next_action
            + ")"
        )
        payload_predicate = _sqlite_ascii_literal_contains_sql("CAST(payload_leaf.value AS TEXT)")
        query_sql = (
            f" AND ({row_predicates} OR EXISTS ("
            "SELECT 1 FROM json_tree(event.payload_json) AS payload_leaf "
            "WHERE payload_leaf.type = 'text' "
            f"AND {payload_predicate}"
            "))"
        )
        params.extend((literal_match_operand(normalized_query),) * 5)
    window_sql = ""
    if occurred_at_start is not None:
        window_sql += " AND julianday(event.occurred_at) >= julianday(?)"
        params.append(_iso_or_none(occurred_at_start))
    if occurred_at_end is not None:
        window_sql += " AND julianday(event.occurred_at) <= julianday(?)"
        params.append(_iso_or_none(occurred_at_end))
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(f"event.{column}" for column in EVENT_LOG_COLUMNS)}
                FROM event_log AS event
                JOIN open_loops AS loop
                  ON loop.user_id = event.user_id
                 AND loop.id = event.target_id
                WHERE {scoped_where_sql}{query_sql}{window_sql}
                ORDER BY event.occurred_at DESC, event.id DESC
                LIMIT ?
                """,
        tuple(params),
    )

@takes_label_lock
def update_open_loop(self, *, loop_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    cursor = self._execute(
        """
                UPDATE open_loops
                SET title = COALESCE(?, title),
                    description = COALESCE(?, description),
                    priority = COALESCE(?, priority),
                    due_at = COALESCE(?, due_at),
                    project_id = COALESCE(?, project_id),
                    person_id = COALESCE(?, person_id),
                    domain = COALESCE(?, domain),
                    sensitivity = COALESCE(?, sensitivity),
                    metadata_json = COALESCE(?, metadata_json),
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                """,
        (
            patch.get("title"),
            patch.get("description"),
            patch.get("priority"),
            _iso_or_none(patch.get("due_at")),
            _uuid_text(patch.get("project_id")),
            _uuid_text(patch.get("person_id")),
            patch.get("domain"),
            patch.get("sensitivity"),
            _json_object_text(patch["metadata_json"]) if "metadata_json" in patch else None,
            _utc_now_iso(),
            str(loop_id),
            self.user_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError(
            "update_open_loop did not return a row from the database",
        )
    row = self._get_row("update_open_loop", "open_loops", OPEN_LOOP_COLUMNS, str(loop_id))
    self._append_mutation_event(
        event_type="open_loop.updated",
        actor_type=actor_type,
        target_type="open_loop",
        target_id=row["id"],
        payload={"operation": "update", "changes": patch},
    )
    return row

@takes_label_lock
def update_open_loop_status(
    self,
    *,
    loop_id: str,
    status: str,
    resolution_note: str | None = None,
    actor_type: str = "system",
) -> VNextRow:
    now = _utc_now_iso()
    cursor = self._execute(
        """
                UPDATE open_loops
                SET status = ?,
                    resolved_at = CASE
                      WHEN ? = 'open' THEN NULL
                      ELSE ?
                    END,
                    closed_at = CASE
                      WHEN ? = 'open' THEN NULL
                      ELSE ?
                    END,
                    resolution_note = CASE
                      WHEN ? = 'open' THEN NULL
                      ELSE ?
                    END,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                """,
        (
            status,
            status,
            now,
            status,
            now,
            status,
            resolution_note,
            now,
            str(loop_id),
            self.user_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError(
            "update_open_loop_status did not return a row from the database",
        )
    row = self._get_row("update_open_loop_status", "open_loops", OPEN_LOOP_COLUMNS, str(loop_id))
    self._append_mutation_event(
        event_type="open_loop.updated",
        actor_type=actor_type,
        target_type="open_loop",
        target_id=row["id"],
        payload={"operation": "update_status", "status": status},
    )
    return row

for _method in (
    create_graph_edge,
    list_edges,
    list_memory_entity_edges,
    expire_edge,
    list_edges_as_of,
    create_entity,
    get_entity,
    get_entity_by_normalized_name,
    find_entities_by_names,
    list_entities,
    update_entity,
    record_entity_mention,
    record_relationship_change,
    list_relationship_events,
    create_open_loop,
    upsert_open_loop_by_automation_digest,
    get_open_loop,
    find_open_loop_by_automation_digest,
    list_open_loops_referencing_source,
    list_open_loops,
    list_open_loops_view_partitions,
    list_open_loop_events,
    update_open_loop,
    update_open_loop_status,
):
    _method.__module__ = "alicebot_api.sqlite_store"
    _method.__qualname__ = f"SQLiteVNextStore.{_method.__name__}"
del _method
