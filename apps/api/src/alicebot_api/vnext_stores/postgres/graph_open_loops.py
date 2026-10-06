"""PostgreSQL graph, entity, belief, and open-loop store seam."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_entity_names import ENTITY_IMMUTABLE_PATCH_FIELDS, normalize_entity_name
from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.postgres.columns import (
    BELIEF_COLUMNS,
    ENTITY_COLUMNS,
    ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS,
    OPEN_LOOP_COLUMNS,
)
from alicebot_api.vnext_stores.postgres.primitives import (
    _json_list,
    _json_object,
    _sorted_field_names,
)
from alicebot_api.vnext_label_writes import takes_label_lock
from alicebot_api.vnext_stores.postgres.query_predicates import (
    _OPEN_LOOP_SCOPE_EVENT_TIME_SQL,
    _OPEN_LOOP_SCOPE_PEOPLE_SQL,
    _OPEN_LOOP_SCOPE_PROJECT_SQL,
    _SCOPED_MEMORY_DIRECT_PEOPLE_SQL,
    _SCOPED_MEMORY_EVENT_TIME_SQL,
    _SCOPED_MEMORY_PROJECT_SQL,
    _escape_like_literal,
    _postgres_ascii_literal_contains_sql,
)

VNextRow = dict[str, object]

def create_edge(self, edge: JsonObject, *, actor_type: str = "system") -> VNextRow:
    # observed_at is event time: when the observation the edge encodes
    # actually happened (callers pass the source's source_created_at,
    # falling back to captured_at). Creation sites without source
    # context fall back to write time, noted in the edge metadata so
    # as-of readers can tell real event time from an ingestion-time
    # stand-in. valid_from defaults to observed_at so the validity
    # interval starts when the observation happened, not when it was
    # written. now() is transaction-stable, so defaulted observed_at
    # and valid_from land on the same instant.
    metadata_value = edge.get("metadata_json")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    if edge.get("observed_at") is None:
        metadata.setdefault("observed_at_source", "now")
    row = self._fetch_one(
        "create_edge",
        f"""
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
                  observed_at,
                  valid_from,
                  valid_to,
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
                  COALESCE(%s::timestamptz, now()),
                  COALESCE(%s::timestamptz, %s::timestamptz, now()),
                  %s,
                  %s
                )
                RETURNING {GRAPH_EDGE_COLUMNS}
                """,
        (
            edge.get("id"),
            edge["from_type"],
            edge["from_id"],
            edge["to_type"],
            edge["to_id"],
            edge["edge_type"],
            edge.get("confidence", 0.5),
            edge.get("explanation"),
            edge.get("created_by", actor_type),
            edge.get("observed_at"),
            edge.get("valid_from"),
            edge.get("observed_at"),
            edge.get("valid_to"),
            _json_object(metadata),
        ),
    )
    self._append_mutation_event(
        event_type="graph_edge.created",
        actor_type=actor_type,
        target_type="graph_edge",
        target_id=row["id"],
        payload={"operation": "create", "edge_type": str(row["edge_type"])},
    )
    return row

def find_edge_by_idempotency_digest(self, *, digest: str) -> VNextRow | None:
    """Resolve one workflow-produced graph edge by its logical identity."""

    normalized_digest = str(digest).strip()
    if not normalized_digest:
        return None
    return self._fetch_optional_one(
        f"""
                SELECT {GRAPH_EDGE_COLUMNS}
                FROM graph_edges
                WHERE metadata_json ->> 'idempotency_digest' = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (normalized_digest,),
    )

def upsert_edge_by_idempotency_digest(
    self,
    edge: JsonObject,
    *,
    digest: str,
    actor_type: str = "system",
) -> VNextRow:
    """Atomically create or replay one workflow-produced graph edge."""

    normalized_digest = str(digest).strip()
    if not normalized_digest:
        raise ValueError("digest must not be empty")
    existing = self.find_edge_by_idempotency_digest(digest=normalized_digest)
    if existing is not None:
        return existing
    metadata_value = edge.get("metadata_json")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    metadata["idempotency_digest"] = normalized_digest
    if edge.get("observed_at") is None:
        metadata.setdefault("observed_at_source", "now")
    row = self._fetch_optional_one(
        f"""
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
                  observed_at,
                  valid_from,
                  valid_to,
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
                  COALESCE(%s::timestamptz, now()),
                  COALESCE(%s::timestamptz, %s::timestamptz, now()),
                  %s,
                  %s
                )
                ON CONFLICT DO NOTHING
                RETURNING {GRAPH_EDGE_COLUMNS}
                """,
        (
            edge.get("id"),
            edge["from_type"],
            edge["from_id"],
            edge["to_type"],
            edge["to_id"],
            edge["edge_type"],
            edge.get("confidence", 0.5),
            edge.get("explanation"),
            edge.get("created_by", actor_type),
            edge.get("observed_at"),
            edge.get("valid_from"),
            edge.get("observed_at"),
            edge.get("valid_to"),
            _json_object(metadata),
        ),
    )
    created = row is not None
    if row is None:
        row = self.find_edge_by_idempotency_digest(digest=normalized_digest)
    if row is None:
        raise ContinuityStoreInvariantError(
            "upsert_edge_by_idempotency_digest could not resolve the persisted edge"
        )
    if created:
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
                SELECT {GRAPH_EDGE_COLUMNS}
                FROM graph_edges
                WHERE (%s::text IS NULL OR from_id = %s)
                  AND (%s::text IS NULL OR to_id = %s)
                  AND valid_to IS NULL
                ORDER BY created_at DESC, id DESC
                """,
        (from_id, from_id, to_id, to_id),
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
    return self._fetch_all(
        f"""
                SELECT {GRAPH_EDGE_COLUMNS}
                FROM graph_edges
                WHERE valid_to IS NULL
                  AND edge_type = ANY(%s::text[])
                  AND (
                    (from_type = 'memory' AND to_type = 'entity' AND to_id = ANY(%s::text[]))
                    OR
                    (from_type = 'entity' AND to_type = 'memory' AND from_id = ANY(%s::text[]))
                  )
                ORDER BY created_at DESC, id DESC
                """,
        (types, ids, ids),
    )

def list_edges_as_of(self, at: object, *, limit: int = 50) -> list[VNextRow]:
    """Edges that were in effect at ``at``: valid_from <= at < valid_to.

        User scoping comes from the graph_edges RLS policy, like every
        other read. Edges written before the temporal slice carry NULL
        ``valid_from`` and are excluded (their event time was never
        recorded).
        """
    return self._fetch_all(
        f"""
                SELECT {GRAPH_EDGE_COLUMNS}
                FROM graph_edges
                WHERE valid_from IS NOT NULL
                  AND valid_from <= %s::timestamptz
                  AND (valid_to IS NULL OR valid_to > %s::timestamptz)
                ORDER BY valid_from DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (at, at, limit),
    )

def update_edge_status(self, *, edge_id: str, status: str, actor_type: str = "system") -> VNextRow:
    row = self._fetch_one(
        "update_edge_status",
        f"""
                UPDATE graph_edges
                SET metadata_json = metadata_json || %s,
                    valid_to = CASE
                      WHEN %s = 'rejected' THEN clock_timestamp()
                      ELSE valid_to
                    END
                WHERE id = %s::uuid
                RETURNING {GRAPH_EDGE_COLUMNS}
                """,
        (_json_object({"status": status, "candidate": status != "accepted"}), status, edge_id),
    )
    self._append_mutation_event(
        event_type="graph_edge.updated",
        actor_type=actor_type,
        target_type="graph_edge",
        target_id=row["id"],
        payload={"operation": "update_status", "status": status},
    )
    return row

def expire_edge(self, *, edge_id: str, actor_type: str = "system") -> VNextRow:
    row = self._fetch_one(
        "expire_edge",
        f"""
                UPDATE graph_edges
                SET valid_to = clock_timestamp()
                WHERE id = %s::uuid
                  AND valid_to IS NULL
                RETURNING {GRAPH_EDGE_COLUMNS}
                """,
        (edge_id,),
    )
    self._append_mutation_event(
        event_type="graph_edge.expired",
        actor_type=actor_type,
        target_type="graph_edge",
        target_id=row["id"],
        payload={"operation": "expire"},
    )
    return row

def create_entity(self, entity: JsonObject, *, actor_type: str = "system") -> VNextRow:
    name = str(entity["name"])
    normalized_name = str(entity.get("normalized_name") or normalize_entity_name(name))
    row = self._fetch_one(
        "create_entity",
        f"""
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
                  mention_count
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::timestamptz,
                  %s::timestamptz,
                  %s
                )
                RETURNING {ENTITY_COLUMNS}
                """,
        (
            entity.get("id"),
            entity["entity_type"],
            name,
            normalized_name,
            _json_list(entity.get("aliases")),
            _json_object(entity.get("metadata_json")),
            entity.get("first_observed_at"),
            entity.get("last_observed_at"),
            entity.get("mention_count", 0),
        ),
    )
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
                SELECT {ENTITY_COLUMNS}
                FROM vnext_entities
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                """,
        (entity_id,),
    )

def get_entity_by_normalized_name(self, entity_type: str, normalized_name: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {ENTITY_COLUMNS}
                FROM vnext_entities
                WHERE entity_type = %s
                  AND normalized_name = %s
                  AND deleted_at IS NULL
                LIMIT 1
                """,
        (entity_type, normalized_name),
    )

def find_entities_by_names(self, normalized_names: tuple[str, ...]) -> list[VNextRow]:
    """One-round-trip resolution lookup for query-time entity linking.

        Matches ``normalized_name`` OR any element of the ``aliases``
        jsonb array (``?|`` = "array contains any of these strings").
        Alias values are expected to already be normalized via
        ``normalize_entity_name`` -- matching is exact string equality.
        Most-mentioned entities sort first so callers can take the top
        match per name.
        """
    if not normalized_names:
        return []
    names = [str(name) for name in normalized_names]
    return self._fetch_all(
        f"""
                SELECT {ENTITY_COLUMNS}
                FROM vnext_entities
                WHERE deleted_at IS NULL
                  AND (normalized_name = ANY(%s::text[]) OR aliases ?| %s::text[])
                ORDER BY mention_count DESC, updated_at DESC, id DESC
                """,
        (names, names),
    )

def list_entities(
    self,
    *,
    entity_type: str | None = None,
    limit: int = 100,
) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {ENTITY_COLUMNS}
                FROM vnext_entities
                WHERE (%s::text IS NULL OR entity_type = %s)
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (entity_type, entity_type, limit),
    )

def update_entity(self, *, entity_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    immutable = sorted(set(patch) & ENTITY_IMMUTABLE_PATCH_FIELDS)
    if immutable:
        raise ContinuityStoreInvariantError(f"update_entity cannot modify immutable fields: {', '.join(immutable)}")
    row = self._fetch_one(
        "update_entity",
        f"""
                UPDATE vnext_entities
                SET name = COALESCE(%s, name),
                    aliases = COALESCE(%s, aliases),
                    metadata_json = COALESCE(%s, metadata_json),
                    mention_count = COALESCE(%s, mention_count),
                    first_observed_at = COALESCE(%s::timestamptz, first_observed_at),
                    last_observed_at = COALESCE(%s::timestamptz, last_observed_at),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING {ENTITY_COLUMNS}
                """,
        (
            patch.get("name"),
            _json_list(patch["aliases"]) if "aliases" in patch else None,
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            patch.get("mention_count"),
            patch.get("first_observed_at"),
            patch.get("last_observed_at"),
            entity_id,
        ),
    )
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
        """
    if observed_at is None:
        raise ContinuityStoreInvariantError("record_entity_mention requires observed_at")
    row = self._fetch_one(
        "record_entity_mention",
        f"""
                UPDATE vnext_entities
                SET mention_count = mention_count + 1,
                    first_observed_at = LEAST(COALESCE(first_observed_at, %s::timestamptz), %s::timestamptz),
                    last_observed_at = GREATEST(COALESCE(last_observed_at, %s::timestamptz), %s::timestamptz),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING {ENTITY_COLUMNS}
                """,
        (observed_at, observed_at, observed_at, observed_at, entity_id),
    )
    self._append_mutation_event(
        event_type="entity.mention_recorded",
        actor_type=actor_type,
        target_type="entity",
        target_id=row["id"],
        payload={
            "operation": "record_mention",
            "observed_at": observed_at,
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
    current = self._fetch_optional_one(
        """
                SELECT metadata_json ->> 'relationship_type' AS relationship_type_before
                FROM vnext_entities
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                """,
        (entity_id,),
    )
    if current is None:
        raise ContinuityStoreInvariantError("record_relationship_change requires an existing entity")
    before = current.get("relationship_type_before")
    row = self._fetch_one(
        "record_relationship_change",
        f"""
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
                VALUES (
                  gen_random_uuid(),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s::uuid,
                  %s
                )
                RETURNING {ENTITY_RELATIONSHIP_EVENT_COLUMNS}
                """,
        (entity_id, before, relationship_type, changed_at, source_id, _json_object(metadata_json)),
    )
    self._fetch_one(
        "record_relationship_change",
        """
                UPDATE vnext_entities
                SET metadata_json = metadata_json || %s,
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING id
                """,
        (_json_object({"relationship_type": relationship_type}), entity_id),
    )
    self._append_mutation_event(
        event_type="entity.relationship_changed",
        actor_type=actor_type,
        target_type="entity",
        target_id=entity_id,
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
                SELECT {ENTITY_RELATIONSHIP_EVENT_COLUMNS}
                FROM entity_relationship_events
                WHERE entity_id = %s::uuid
                ORDER BY changed_at DESC, id DESC
                """,
        (entity_id,),
    )

def create_belief(self, belief: JsonObject, *, actor_type: str = "system") -> VNextRow:
    row = self._fetch_one(
        "create_belief",
        f"""
                INSERT INTO beliefs (
                  id,
                  user_id,
                  memory_id,
                  claim,
                  status,
                  confidence,
                  first_seen_at,
                  last_reinforced_at,
                  last_challenged_at,
                  superseded_by,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s,
                  %s,
                  %s::uuid,
                  %s
                )
                RETURNING {BELIEF_COLUMNS}
                """,
        (
            belief.get("id"),
            belief["memory_id"],
            belief["claim"],
            belief.get("status", "active"),
            belief.get("confidence", 0.5),
            belief.get("first_seen_at"),
            belief.get("last_reinforced_at"),
            belief.get("last_challenged_at"),
            belief.get("superseded_by"),
            _json_object(belief.get("metadata_json")),
        ),
    )
    self._append_mutation_event(
        event_type="belief.created",
        actor_type=actor_type,
        target_type="belief",
        target_id=row["id"],
        payload={"operation": "create", "memory_id": str(row["memory_id"])},
    )
    return row

def get_belief(self, belief_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {BELIEF_COLUMNS}
                FROM beliefs
                WHERE id = %s::uuid
                """,
        (belief_id,),
    )

def list_beliefs(
    self,
    *,
    status: str | None = "active",
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    scope_projects: tuple[str, ...] = (),
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
    limit: int = 8,
) -> list[VNextRow]:
    project_list = list(project_scope_identity(scope_projects)) or None
    people_list = [str(value).strip().casefold() for value in scope_people if str(value).strip()] or None
    person_memory_ids = [str(value) for value in scope_person_memory_ids if str(value)] or None
    return self._fetch_all(
        f"""
                SELECT
                  b.id,
                  b.user_id,
                  b.memory_id,
                  b.claim,
                  b.status,
                  b.confidence,
                  b.first_seen_at,
                  b.last_reinforced_at,
                  b.last_challenged_at,
                  b.superseded_by,
                  b.metadata_json,
                  m.domain,
                  m.sensitivity,
                  m.memory_type,
                  m.canonical_text AS memory_canonical_text
                FROM beliefs b
                JOIN memories m
                  ON m.id = b.memory_id
                 AND m.user_id = b.user_id
                WHERE (%s::text IS NULL OR b.status = %s)
                  AND m.deleted_at IS NULL
                  AND (%s::text[] IS NULL OR m.domain = ANY(%s::text[]) OR m.domain = 'unknown')
                  AND (%s::text[] IS NULL OR m.sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_SCOPED_MEMORY_PROJECT_SQL}) ?| %s::text[])
                  AND (
                    %s::text[] IS NULL
                    OR m.id::text = ANY(%s::text[])
                    OR {_SCOPED_MEMORY_DIRECT_PEOPLE_SQL}
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_SCOPED_MEMORY_EVENT_TIME_SQL} >= %s::timestamptz
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_SCOPED_MEMORY_EVENT_TIME_SQL} <= %s::timestamptz
                  )
                ORDER BY
                  b.last_challenged_at DESC NULLS LAST,
                  b.last_reinforced_at DESC NULLS LAST,
                  b.first_seen_at DESC,
                  b.id DESC
                LIMIT %s
                """,
        (
            status,
            status,
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
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

def update_belief_status(
    self,
    *,
    belief_id: str,
    status: str,
    confidence: float | None = None,
    superseded_by: str | None = None,
    actor_type: str = "system",
) -> VNextRow:
    row = self._fetch_one(
        "update_belief_status",
        f"""
                UPDATE beliefs
                SET status = %s,
                    confidence = COALESCE(%s, confidence),
                    last_reinforced_at = CASE
                      WHEN %s = 'active' THEN clock_timestamp()
                      ELSE last_reinforced_at
                    END,
                    last_challenged_at = CASE
                      WHEN %s = 'challenged' THEN clock_timestamp()
                      ELSE last_challenged_at
                    END,
                    superseded_by = COALESCE(%s::uuid, superseded_by)
                WHERE id = %s::uuid
                RETURNING {BELIEF_COLUMNS}
                """,
        (status, confidence, status, status, superseded_by, belief_id),
    )
    self._append_mutation_event(
        event_type="belief.updated",
        actor_type=actor_type,
        target_type="belief",
        target_id=row["id"],
        payload={"operation": "update_status", "status": status},
    )
    return row

@takes_label_lock
def create_open_loop(self, loop: JsonObject, *, actor_type: str = "system") -> VNextRow:
    from alicebot_api.vnext_label_writes import apply_insert_floor, remember_floor_event

    loop, floor_event = apply_insert_floor(self, "open_loop", loop)
    row = self._fetch_one(
        "create_open_loop",
        f"""
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
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::uuid,
                  %s::uuid,
                  %s::uuid,
                  %s,
                  %s,
                  %s,
                  %s,
                  clock_timestamp(),
                  clock_timestamp()
                )
                ON CONFLICT DO NOTHING
                RETURNING {OPEN_LOOP_COLUMNS}
                """,
        (
            loop.get("id"),
            loop.get("memory_id"),
            loop["title"],
            loop.get("status", "open"),
            loop.get("opened_at"),
            loop.get("due_at"),
            loop.get("resolved_at"),
            loop.get("resolution_note"),
            loop.get("description"),
            loop.get("priority", "normal"),
            loop.get("project_id"),
            loop.get("person_id"),
            loop.get("source_id"),
            loop.get("closed_at"),
            loop.get("domain", "unknown"),
            loop.get("sensitivity", "unknown"),
            _json_object(loop.get("metadata_json")),
        ),
    )
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
    """Create or replay one exact automation-discovered open loop."""

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
    except ContinuityStoreInvariantError:
        # The unique digest belongs to the user, not the mutable project or
        # person columns. Label propagation may have emptied those columns.
        existing = self.find_open_loop_by_automation_digest(
            digest=normalized_digest,
        )
        if existing is None:
            raise
        return existing

def get_open_loop(self, loop_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {OPEN_LOOP_COLUMNS}
                FROM open_loops
                WHERE id = %s::uuid
                """,
        (loop_id,),
    )

def find_open_loop_by_automation_digest(
    self,
    *,
    digest: str,
    project_id: str | None = None,
    person_id: str | None = None,
) -> VNextRow | None:
    """Find an exact automation result with scope predicates in SQL."""
    normalized_digest = str(digest).strip()
    if not normalized_digest:
        return None
    return self._fetch_optional_one(
        f"""
                SELECT {OPEN_LOOP_COLUMNS}
                FROM open_loops
                WHERE COALESCE(
                    metadata_json ->> 'idempotency_digest',
                    metadata_json ->> 'automation_digest'
                  ) = %s
                  AND (%s::uuid IS NULL OR project_id = %s::uuid)
                  AND (%s::uuid IS NULL OR person_id = %s::uuid)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (
            normalized_digest,
            project_id,
            project_id,
            person_id,
            person_id,
        ),
    )

def list_open_loops_referencing_source(self, *, source_id: str, limit: int = 500) -> list[VNextRow]:
    """Bound open loops related to one source before LIMIT."""

    if limit < 1:
        raise ValueError("limit must be positive")
    source_ref = f"source:{source_id}"
    return self._fetch_all(
        f"""
                SELECT {OPEN_LOOP_COLUMNS}
                FROM open_loops
                WHERE source_id = %s::uuid
                  OR metadata_json ->> 'source_id' = %s
                  OR metadata_json ->> 'source_ref' IN (%s, %s)
                  OR metadata_json -> 'source_ids' ? %s
                  OR metadata_json -> 'source_refs' ? %s
                  OR metadata_json -> 'source_refs' ? %s
                  OR metadata_json -> 'source_references' ? %s
                  OR metadata_json -> 'source_references' ? %s
                  OR metadata_json -> 'selected_source_ids' ? %s
                ORDER BY updated_at DESC, created_at DESC, id DESC
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
) -> list[VNextRow]:
    scope_projects_list = list(project_scope_identity(scope_projects or ())) or None
    scope_people_list = list(scope_people) or None
    normalized_statuses = None
    statuses_sql = ""
    statuses_params: tuple[object, ...] = ()
    if statuses is not None:
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return []
        statuses_sql = " AND status = ANY(%s::text[])"
        statuses_params = (normalized_statuses,)
    normalized_query = str(query).strip() if query is not None else ""
    query_sql = ""
    query_params: tuple[object, ...] = ()
    if normalized_query:
        query_sql = f"""
                  AND (
                    {_postgres_ascii_literal_contains_sql("COALESCE(title, '')")}
                    OR {_postgres_ascii_literal_contains_sql("COALESCE(description, '')")}
                    OR (
                      jsonb_typeof(metadata_json -> 'next_action') = 'string'
                      AND {_postgres_ascii_literal_contains_sql("COALESCE(metadata_json ->> 'next_action', '')")}
                    )
                    OR (
                      jsonb_typeof(metadata_json #> '{{agentic_memory,next_action}}') = 'string'
                      AND {_postgres_ascii_literal_contains_sql("COALESCE(metadata_json #>> '{agentic_memory,next_action}', '')")}
                    )
                  )
            """
        query_params = (_escape_like_literal(normalized_query),) * 4
    return self._fetch_all(
        f"""
                SELECT {OPEN_LOOP_COLUMNS}
                FROM open_loops
                WHERE (%s::text IS NULL OR status = %s)
                  {statuses_sql}
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::uuid IS NULL OR project_id = %s::uuid)
                  AND (%s::uuid IS NULL OR person_id = %s::uuid)
                  AND (
                    %s::text[] IS NULL
                    OR ({_OPEN_LOOP_SCOPE_PROJECT_SQL}) ?| %s::text[]
                  )
                  AND (
                    %s::text[] IS NULL
                    OR person_id::text = ANY(%s::text[])
                    OR {_OPEN_LOOP_SCOPE_PEOPLE_SQL}
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_OPEN_LOOP_SCOPE_EVENT_TIME_SQL} >= %s::timestamptz
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_OPEN_LOOP_SCOPE_EVENT_TIME_SQL} <= %s::timestamptz
                  )
                  {query_sql}
                ORDER BY opened_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (
            status,
            status,
            *statuses_params,
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
            project_id,
            project_id,
            person_id,
            person_id,
            scope_projects_list,
            scope_projects_list,
            scope_people_list,
            scope_people_list,
            scope_people_list,
            scope_window_start,
            scope_window_start,
            scope_window_end,
            scope_window_end,
            *query_params,
            limit,
        ),
    )

def list_open_loop_events(
    self,
    *,
    statuses: Sequence[str],
    scope_projects: Sequence[str] | None = None,
    query: str | None = None,
    occurred_at_start: datetime | None = None,
    occurred_at_end: datetime | None = None,
    limit: int = 20,
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
) -> list[VNextRow]:
    """Return scoped events for active loops without bounding loop age.

    ``domains`` and ``sensitivity_allowed`` have no defaults, as on the SQLite store,
    where they fence the read before ``LIMIT``. The Postgres runtime resolves no
    project view and has no such read, so only ``None`` is accepted and anything
    else raises instead of being ignored.
    """

    if domains is not None or sensitivity_allowed is not None:
        raise ValueError("the Postgres event reads take no domain or sensitivity fence; pass None")
    if limit < 1:
        raise ValueError("limit must be positive")
    normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
    if not normalized_statuses:
        return []
    scope_projects_list = list(project_scope_identity(scope_projects or ())) or None
    normalized_query = str(query).strip() if query is not None else ""
    query_sql = ""
    query_params: tuple[object, ...] = ()
    if normalized_query:
        query_sql = f"""
                  AND (
                    {_postgres_ascii_literal_contains_sql("COALESCE(loop.title, '')")}
                    OR {_postgres_ascii_literal_contains_sql("COALESCE(loop.description, '')")}
                    OR (
                      jsonb_typeof(loop.metadata_json -> 'next_action') = 'string'
                      AND {_postgres_ascii_literal_contains_sql("COALESCE(loop.metadata_json ->> 'next_action', '')")}
                    )
                    OR (
                      jsonb_typeof(loop.metadata_json #> '{{agentic_memory,next_action}}') = 'string'
                      AND {_postgres_ascii_literal_contains_sql("COALESCE(loop.metadata_json #>> '{agentic_memory,next_action}', '')")}
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM jsonb_path_query(
                        event.payload_json,
                        'strict $.** ? (@.type() == "string")'
                      ) AS payload_leaf(value)
                      WHERE {_postgres_ascii_literal_contains_sql("payload_leaf.value #>> '{}'")}
                    )
                  )
            """
        query_params = (_escape_like_literal(normalized_query),) * 5
    qualified_columns = ", ".join(f"event.{column.strip()}" for column in EVENT_LOG_COLUMNS.split(","))
    return self._fetch_all(
        f"""
                SELECT {qualified_columns}
                FROM event_log AS event
                JOIN open_loops AS loop
                  ON event.target_type = 'open_loop'
                 AND event.target_id = loop.id::text
                 AND event.user_id = loop.user_id
                WHERE loop.status = ANY(%s::text[])
                  AND (
                    %s::text[] IS NULL
                    OR ({_OPEN_LOOP_SCOPE_PROJECT_SQL}) ?| %s::text[]
                  )
                  {query_sql}
                  AND (%s::timestamptz IS NULL OR event.occurred_at >= %s::timestamptz)
                  AND (%s::timestamptz IS NULL OR event.occurred_at <= %s::timestamptz)
                ORDER BY event.occurred_at DESC, event.id DESC
                LIMIT %s
                """,
        (
            normalized_statuses,
            scope_projects_list,
            scope_projects_list,
            *query_params,
            occurred_at_start,
            occurred_at_start,
            occurred_at_end,
            occurred_at_end,
            limit,
        ),
    )

@takes_label_lock
def update_open_loop(self, *, loop_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    from alicebot_api.vnext_label_writes import clamp_owner_patch, merge_protected_metadata, prepare_label_patch, propagate_after_write

    before = self.get_open_loop(loop_id)
    patch = dict(patch)
    metadata = patch.get("metadata_json")
    if before is not None and isinstance(metadata, dict):
        patch["metadata_json"] = merge_protected_metadata(
            before.get("metadata_json") if isinstance(before.get("metadata_json"), dict) else {},
            metadata, label_write=False,
        )
    patch = prepare_label_patch(self, "open_loop", before, patch)
    patch = clamp_owner_patch(self, kind="open_loop", before=before, patch=patch)
    row = self._fetch_one(
        "update_open_loop",
        f"""
                UPDATE open_loops
                SET title = COALESCE(%s, title),
                    description = COALESCE(%s, description),
                    priority = COALESCE(%s, priority),
                    due_at = COALESCE(%s::timestamptz, due_at),
                    project_id = COALESCE(%s::uuid, project_id),
                    person_id = COALESCE(%s::uuid, person_id),
                    domain = COALESCE(%s, domain),
                    sensitivity = COALESCE(%s, sensitivity),
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {OPEN_LOOP_COLUMNS}
                """,
        (
            patch.get("title"),
            patch.get("description"),
            patch.get("priority"),
            patch.get("due_at"),
            patch.get("project_id"),
            patch.get("person_id"),
            patch.get("domain"),
            patch.get("sensitivity"),
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            loop_id,
        ),
    )
    self._append_mutation_event(
        event_type="open_loop.updated",
        actor_type=actor_type,
        target_type="open_loop",
        target_id=row["id"],
        payload={"operation": "update", "changes": patch},
    )
    propagate_after_write(self, kind="open_loop", before=before, after=row)
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
    row = self._fetch_one(
        "update_open_loop_status",
        f"""
                UPDATE open_loops
                SET status = %s,
                    resolved_at = CASE
                      WHEN %s = 'open' THEN NULL
                      ELSE clock_timestamp()
                    END,
                    closed_at = CASE
                      WHEN %s = 'open' THEN NULL
                      ELSE clock_timestamp()
                    END,
                    resolution_note = CASE
                      WHEN %s = 'open' THEN NULL
                      ELSE %s
                    END,
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                RETURNING {OPEN_LOOP_COLUMNS}
                """,
        (status, status, status, status, resolution_note, loop_id),
    )
    self._append_mutation_event(
        event_type="open_loop.updated",
        actor_type=actor_type,
        target_type="open_loop",
        target_id=row["id"],
        payload={"operation": "update_status", "status": status},
    )
    return row

for _method in (
    create_edge,
    find_edge_by_idempotency_digest,
    upsert_edge_by_idempotency_digest,
    list_edges,
    list_memory_entity_edges,
    list_edges_as_of,
    update_edge_status,
    expire_edge,
    create_entity,
    get_entity,
    get_entity_by_normalized_name,
    find_entities_by_names,
    list_entities,
    update_entity,
    record_entity_mention,
    record_relationship_change,
    list_relationship_events,
    create_belief,
    get_belief,
    list_beliefs,
    update_belief_status,
    create_open_loop,
    upsert_open_loop_by_automation_digest,
    get_open_loop,
    find_open_loop_by_automation_digest,
    list_open_loops_referencing_source,
    list_open_loops,
    list_open_loop_events,
    update_open_loop,
    update_open_loop_status,
):
    _method.__module__ = "alicebot_api.vnext_store"
    _method.__qualname__ = f"PostgresVNextStore.{_method.__name__}"
del _method
