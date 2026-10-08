"""PostgreSQL event-log and memory-revision store seam."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.postgres.columns import EVENT_LOG_COLUMNS, REVISION_COLUMNS
from alicebot_api.vnext_stores.postgres.primitives import _json_list, _json_object, _json_safe
from alicebot_api.vnext_label_writes import takes_label_lock

VNextRow = dict[str, object]

_PROJECT_UPDATE_EVENT_TYPES_SQL = """
                    'project.update_candidate_created',
                    'project.update_candidate_accepted',
                    'project.update_candidate_rejected'
                  """

_PROJECT_UPDATE_EVENT_LINKAGE_SQL = (
    "target_type = 'artifact' AND target_id = %s",
    "target_type = 'memory' AND target_id = %s",
    "payload_artifact_id = %s",
    "payload_candidate_memory_id = %s",
    "payload_memory_id = %s",
)

_PROJECT_UPDATE_EVENT_LOOKUP_SQL = (
    "\nUNION\n".join(
        f"""
                SELECT {EVENT_LOG_COLUMNS}
                FROM event_log
                WHERE user_id = app.current_user_id()
                  AND event_type IN (
{_PROJECT_UPDATE_EVENT_TYPES_SQL}
                  )
                  AND {linkage_sql}
        """
        for linkage_sql in _PROJECT_UPDATE_EVENT_LINKAGE_SQL
    )
    + """
                ORDER BY occurred_at DESC, id DESC
    """
)


def _append_mutation_event(
    self,
    *,
    event_type: str,
    actor_type: str,
    target_type: str,
    target_id: object,
    payload: JsonObject,
    actor_id: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
) -> VNextRow:
    return self.append_event(
        build_event_log_record(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            target_type=target_type,
            target_id=str(target_id),
            payload=cast(JsonObject, _json_safe(payload)),
            trace_id=trace_id,
            run_id=run_id,
        )
    )


def append_event(self, event: JsonObject) -> VNextRow:
    return self._fetch_one(
        "append_event",
        f"""
                INSERT INTO event_log (
                  id,
                  user_id,
                  event_type,
                  actor_type,
                  actor_id,
                  target_type,
                  target_id,
                  occurred_at,
                  payload_json,
                  trace_id,
                  run_id,
                  integrity_hash
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {EVENT_LOG_COLUMNS}
                """,
        (
            event.get("id"),
            event["event_type"],
            event["actor_type"],
            event.get("actor_id"),
            event.get("target_type"),
            event.get("target_id"),
            event.get("occurred_at"),
            _json_object(event.get("payload_json")),
            event.get("trace_id"),
            event.get("run_id"),
            event.get("integrity_hash"),
        ),
    )


def list_events(
    self,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    occurred_at_start: datetime | None = None,
    occurred_at_end: datetime | None = None,
    limit: int | None = None,
    reject_sensitivity_allowed: Sequence[str] = (),
) -> list[VNextRow]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not reject_sensitivity_allowed and target_type is None and target_id is None and occurred_at_start is None and occurred_at_end is None:
        limit_sql = ""
        params: list[object] = []
        if limit is not None:
            limit_sql = " LIMIT %s"
            params.append(limit)
        return self._fetch_all(
            f"""
                    SELECT {EVENT_LOG_COLUMNS}
                    FROM event_log
                    ORDER BY occurred_at DESC, id DESC{limit_sql}
                    """,
            tuple(params),
        )
    clauses = [
        "(%s::text IS NULL OR target_type = %s)",
        "(%s::text IS NULL OR target_id = %s)",
    ]
    params = [target_type, target_type, target_id, target_id]
    if reject_sensitivity_allowed:
        from alicebot_api.vnext_label_sql import hidden_memory_event_sql
        clauses.append(hidden_memory_event_sql(reject_sensitivity_allowed, sqlite=False))
    if occurred_at_start is not None:
        clauses.append("occurred_at >= %s::timestamptz")
        params.append(occurred_at_start)
    if occurred_at_end is not None:
        clauses.append("occurred_at <= %s::timestamptz")
        params.append(occurred_at_end)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT %s"
        params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {EVENT_LOG_COLUMNS}
                FROM event_log
                {where_sql}
                ORDER BY occurred_at DESC, id DESC{limit_sql}
                """,
        tuple(params),
    )


def list_events_for_source_trace(
    self,
    *,
    source_id: str,
    memory_ids: Sequence[str] = (),
    artifact_ids: Sequence[str] = (),
    open_loop_ids: Sequence[str] = (),
    limit: int = 500,
) -> list[VNextRow]:
    """Bound source-trace events with relationship predicates before LIMIT."""

    if limit < 1:
        raise ValueError("limit must be positive")
    memories = list(dict.fromkeys(str(value) for value in memory_ids if value)) or None
    artifacts = list(dict.fromkeys(str(value) for value in artifact_ids if value)) or None
    open_loops = list(dict.fromkeys(str(value) for value in open_loop_ids if value)) or None
    source_ref = f"source:{source_id}"
    return self._fetch_all(
        f"""
                SELECT {EVENT_LOG_COLUMNS}
                FROM event_log
                WHERE (
                  (target_type = 'source' AND target_id = %s)
                  OR (%s::text[] IS NOT NULL AND target_type = 'memory' AND target_id = ANY(%s::text[]))
                  OR (%s::text[] IS NOT NULL AND target_type = 'artifact' AND target_id = ANY(%s::text[]))
                  OR (%s::text[] IS NOT NULL AND target_type = 'open_loop' AND target_id = ANY(%s::text[]))
                  OR payload_json ->> 'source_id' = %s
                  OR payload_json ->> 'source_ref' IN (%s, %s)
                  OR payload_json -> 'source_ids' ? %s
                  OR payload_json -> 'source_refs' ? %s
                  OR payload_json -> 'source_refs' ? %s
                  OR payload_json -> 'source_references' ? %s
                  OR payload_json -> 'source_references' ? %s
                  OR payload_json -> 'selected_source_ids' ? %s
                )
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s
                """,
        (
            source_id,
            memories,
            memories,
            artifacts,
            artifacts,
            open_loops,
            open_loops,
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


def list_project_update_events(
    self,
    *,
    artifact_id: str,
    candidate_memory_id: str,
) -> list[VNextRow]:
    """Return every creation/decision event coupled to one project update.

        Direct targets and every supported payload-only linkage are selected
        in SQL so terminal replay is proportional to the coupled evidence,
        rather than to the user's complete append-only event log.
        """

    # UNION (rather than one five-way OR or UNION ALL) gives each linkage
    # arm its own indexable scan while collapsing an event that carries
    # more than one valid linkage before the deterministic final ordering.
    return self._fetch_all(
        _PROJECT_UPDATE_EVENT_LOOKUP_SQL,
        (
            artifact_id,
            candidate_memory_id,
            artifact_id,
            candidate_memory_id,
            candidate_memory_id,
        ),
    )


def count_events(
    self,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
) -> int:
    """Count matching event rows without materializing the append-only log."""
    row = self._fetch_one(
        "count events",
        """
                SELECT COUNT(*)::bigint AS count
                FROM event_log
                WHERE (%s::text IS NULL OR target_type = %s)
                  AND (%s::text IS NULL OR target_id = %s)
                """,
        (target_type, target_type, target_id, target_id),
    )
    return int(cast(int, row["count"]))


@takes_label_lock
def append_revision(self, revision: JsonObject, *, actor_type: str = "system") -> VNextRow:
    row = self._fetch_one(
        "append_revision",
        f"""
                WITH locked_memory AS (
                  SELECT id
                  FROM memories
                  WHERE id = %s::uuid
                    AND user_id = app.current_user_id()
                  FOR UPDATE
                ),
                next_revision AS (
                  SELECT
                    COALESCE(MAX(sequence_no) + 1, 1) AS sequence_no,
                    COALESCE(MAX(revision_number) + 1, 1) AS revision_number
                  FROM memory_revisions
                  WHERE memory_id = (SELECT id FROM locked_memory)
                    AND user_id = app.current_user_id()
                )
                INSERT INTO memory_revisions (
                  id,
                  user_id,
                  memory_id,
                  sequence_no,
                  action,
                  memory_key,
                  previous_value,
                  new_value,
                  source_event_ids,
                  candidate,
                  revision_number,
                  revision_type,
                  text_before,
                  text_after,
                  reason,
                  actor_type,
                  actor_id,
                  metadata_json
                )
                SELECT
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  COALESCE(%s, next_revision.sequence_no),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s, next_revision.revision_number),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                FROM next_revision
                RETURNING {REVISION_COLUMNS}
                """,
        (
            revision["memory_id"],
            revision.get("id"),
            revision["memory_id"],
            revision.get("sequence_no"),
            revision.get("action", "UPDATE"),
            revision["memory_key"],
            _json_object(revision["previous_value"]) if "previous_value" in revision else None,
            _json_object(revision.get("new_value")),
            _json_list(revision.get("source_event_ids")),
            _json_object(revision.get("candidate")),
            revision.get("revision_number"),
            revision.get("revision_type", "edited"),
            revision.get("text_before"),
            revision.get("text_after", ""),
            revision.get("reason"),
            revision.get("actor_type", actor_type),
            revision.get("actor_id"),
            _json_object(revision.get("metadata_json")),
        ),
    )
    self._append_mutation_event(
        event_type="memory_revision.created",
        actor_type=actor_type,
        target_type="memory",
        target_id=row["memory_id"],
        payload={"operation": "create_revision", "revision_id": str(row["id"])},
    )
    return row


def list_revisions(self, memory_id: str) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {REVISION_COLUMNS}
                FROM memory_revisions
                WHERE memory_id = %s::uuid
                ORDER BY revision_number ASC, sequence_no ASC, id ASC
                """,
        (memory_id,),
    )


for _store_method in (
    _append_mutation_event,
    append_event,
    list_events,
    list_events_for_source_trace,
    list_project_update_events,
    count_events,
    append_revision,
    list_revisions,
):
    _store_method.__module__ = "alicebot_api.vnext_store"
    _store_method.__qualname__ = f"PostgresVNextStore.{_store_method.__name__}"
del _store_method
