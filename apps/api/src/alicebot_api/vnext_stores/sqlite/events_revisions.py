"""SQLite event-log and memory-revision store seam."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.sqlite.columns import EVENT_LOG_COLUMNS, REVISION_COLUMNS
from alicebot_api.vnext_stores.sqlite.primitives import (
    _iso_or_none,
    _iso_or_now,
    _json_list_text,
    _json_object_text,
    _new_id,
    _utc_now_iso,
    _uuid_text,
)

VNextRow = dict[str, object]

_PROJECT_UPDATE_EVENT_TYPES_SQL = """
                    'project.update_candidate_created',
                    'project.update_candidate_accepted',
                    'project.update_candidate_rejected'
                  """

_PROJECT_UPDATE_EVENT_LINKAGE_SQL = (
    ("event_log_project_update_target_idx", "target_type = 'artifact' AND target_id = ?"),
    ("event_log_project_update_target_idx", "target_type = 'memory' AND target_id = ?"),
    ("event_log_project_update_artifact_id_idx", "json_extract(payload_json, '$.artifact_id') = ?"),
    (
        "event_log_project_update_candidate_memory_id_idx",
        "json_extract(payload_json, '$.candidate_memory_id') = ?",
    ),
    ("event_log_project_update_memory_id_idx", "json_extract(payload_json, '$.memory_id') = ?"),
)

_PROJECT_UPDATE_EVENT_LOOKUP_SQL = (
    "\nUNION\n".join(
        f"""
                SELECT {", ".join(EVENT_LOG_COLUMNS)}
                FROM event_log INDEXED BY {index_name}
                WHERE user_id = ?
                  AND event_type IN (
{_PROJECT_UPDATE_EVENT_TYPES_SQL}
                  )
                  AND {linkage_sql}
        """
        for index_name, linkage_sql in _PROJECT_UPDATE_EVENT_LINKAGE_SQL
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
            payload=cast(JsonObject, json_safe(payload)),
            trace_id=trace_id,
            run_id=run_id,
        )
    )


def append_event(self, event: JsonObject) -> VNextRow:
    event_id = _new_id(event.get("id"))
    self._execute(
        """
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            event_id,
            self.user_id,
            event["event_type"],
            event["actor_type"],
            event.get("actor_id"),
            event.get("target_type"),
            event.get("target_id"),
            _iso_or_now(event.get("occurred_at")),
            _json_object_text(event.get("payload_json")),
            event.get("trace_id"),
            event.get("run_id"),
            event.get("integrity_hash"),
        ),
    )
    return self._get_row("append_event", "event_log", EVENT_LOG_COLUMNS, event_id)


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
    clauses = ["user_id = ?"]
    params: list[object] = [self.user_id]
    if reject_sensitivity_allowed:
        from alicebot_api.vnext_label_sql import hidden_memory_event_sql
        clauses.append(hidden_memory_event_sql(reject_sensitivity_allowed, sqlite=True))
    if target_type is not None:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if occurred_at_start is not None:
        clauses.append("julianday(occurred_at) >= julianday(?)")
        params.append(_iso_or_none(occurred_at_start))
    if occurred_at_end is not None:
        clauses.append("julianday(occurred_at) <= julianday(?)")
        params.append(_iso_or_none(occurred_at_end))
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(EVENT_LOG_COLUMNS)}
                FROM event_log
                WHERE {" AND ".join(clauses)}
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
    """Bound source-trace events with predicates before LIMIT."""

    if limit < 1:
        raise ValueError("limit must be positive")
    alternatives = ["(target_type = 'source' AND target_id = ?)"]
    params: list[object] = [self.user_id, source_id]
    for target_type, values in (
        ("memory", memory_ids),
        ("artifact", artifact_ids),
        ("open_loop", open_loop_ids),
    ):
        ids = list(dict.fromkeys(str(value) for value in values if value))
        if not ids:
            continue
        alternatives.append(f"(target_type = '{target_type}' AND target_id IN ({self._placeholders(ids)}))")
        params.extend(ids)
    alternatives.append(
        "EXISTS ("
        "SELECT 1 FROM json_tree(event_log.payload_json) AS ref "
        "WHERE ref.key IN ('source_id', 'source_ids', 'source_ref', 'source_refs', "
        "'source_references', 'selected_source_ids') "
        "AND CAST(ref.value AS TEXT) IN (?, ?)"
        ")"
    )
    params.extend((source_id, f"source:{source_id}", limit))
    return self._fetch_all(
        f"""
                SELECT {", ".join(EVENT_LOG_COLUMNS)}
                FROM event_log
                WHERE user_id = ?
                  AND ({" OR ".join(alternatives)})
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
    )


def list_project_update_events(
    self,
    *,
    artifact_id: str,
    candidate_memory_id: str,
) -> list[VNextRow]:
    """Return every creation/decision event coupled to one project update."""

    # UNION makes every linkage arm independently indexable. It also
    # deduplicates one full event row that matches multiple linkage arms
    # before applying the stable replay order.
    return self._fetch_all(
        _PROJECT_UPDATE_EVENT_LOOKUP_SQL,
        (
            self.user_id,
            artifact_id,
            self.user_id,
            candidate_memory_id,
            self.user_id,
            artifact_id,
            self.user_id,
            candidate_memory_id,
            self.user_id,
            candidate_memory_id,
        ),
    )


def count_events(
    self,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
) -> int:
    """Count matching event rows without materializing the event log."""
    clauses = ["user_id = ?"]
    params: list[object] = [self.user_id]
    if target_type is not None:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    row = self._fetch_one(
        "count events",
        f"SELECT COUNT(*) AS count FROM event_log WHERE {' AND '.join(clauses)}",
        tuple(params),
    )
    return int(cast(int, row["count"]))


def append_revision(self, revision: JsonObject, *, actor_type: str = "system") -> VNextRow:
    revision_id = _new_id(revision.get("id"))
    memory_id = _uuid_text(revision["memory_id"])
    # Allocate both counters inside the INSERT statement. SQLite
    # serializes writers before a write statement evaluates its SELECT,
    # avoiding the former read-MAX / later-INSERT race across connections.
    self._execute(
        """
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
                  metadata_json,
                  created_at
                )
                SELECT
                  ?, ?, ?,
                  COALESCE(?, COALESCE(MAX(sequence_no) + 1, 1)),
                  ?, ?, ?, ?, ?, ?,
                  COALESCE(?, COALESCE(MAX(revision_number) + 1, 1)),
                  ?, ?, ?, ?, ?, ?, ?, ?
                FROM memory_revisions
                WHERE memory_id = ?
                  AND user_id = ?
                """,
        (
            revision_id,
            self.user_id,
            memory_id,
            revision.get("sequence_no"),
            revision.get("action", "UPDATE"),
            revision["memory_key"],
            _json_object_text(revision["previous_value"]) if "previous_value" in revision else None,
            _json_object_text(revision.get("new_value")),
            _json_list_text(revision.get("source_event_ids")),
            _json_object_text(revision.get("candidate")),
            revision.get("revision_number"),
            revision.get("revision_type", "edited"),
            revision.get("text_before"),
            revision.get("text_after", ""),
            revision.get("reason"),
            revision.get("actor_type", actor_type),
            revision.get("actor_id"),
            _json_object_text(revision.get("metadata_json")),
            _utc_now_iso(),
            memory_id,
            self.user_id,
        ),
    )
    row = self._get_row("append_revision", "memory_revisions", REVISION_COLUMNS, revision_id)
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
                SELECT {", ".join(REVISION_COLUMNS)}
                FROM memory_revisions
                WHERE memory_id = ?
                  AND user_id = ?
                ORDER BY revision_number ASC, sequence_no ASC, id ASC
                """,
        (str(memory_id), self.user_id),
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
    _store_method.__module__ = "alicebot_api.sqlite_store"
    _store_method.__qualname__ = f"SQLiteVNextStore.{_store_method.__name__}"
del _store_method
