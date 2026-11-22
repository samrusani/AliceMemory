"""PostgreSQL publication records for an ordered lesson candidate set."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_stores.postgres.columns import MEMORY_COLUMNS, REVISION_COLUMNS
from alicebot_api.vnext_stores.postgres.query_predicates import _MEMORY_PROJECT_SCOPE_SQL

_MEMORY_FIELDS = frozenset(
    {"id", "user_id", "value", "metadata_json", "created_by_agent_id", "created_at", "domain", "sensitivity", "project_id"}
)
_VECTOR_FIELDS = frozenset(
    {"id", "user_id", "metadata_json", "domain", "sensitivity", "project_id", "created_at", "updated_at", "valid_from", "canonical_text", "title", "summary"}
)
LESSON_MEMORY_COLUMNS = ", ".join(name.strip() for name in MEMORY_COLUMNS.split(",") if name.strip() in _MEMORY_FIELDS)
LESSON_VECTOR_COLUMNS = ", ".join(name.strip() for name in MEMORY_COLUMNS.split(",") if name.strip() in _VECTOR_FIELDS)

_REVISION_FIELDS = frozenset(
    {"memory_id", "revision_number", "sequence_no", "id", "action", "revision_type", "actor_type", "actor_id", "created_at", "text_before", "text_after"}
)


def stream_lesson_memories(store: Any, *, domains, sensitivity, projects, lazy_metadata):
    """Intersect possible publication records with the store's ordinary read fence."""
    with store.conn.cursor() as cursor:
        cursor.execute(
            "SELECT m.id FROM memories AS m WHERE m.status = 'active' "
            "AND m.memory_type = 'procedure' AND m.deleted_at IS NULL "
            "AND EXISTS (SELECT 1 FROM memory_revisions AS r "
            "WHERE r.memory_id = m.id AND r.user_id = m.user_id "
            "AND r.new_value -> 'lesson' IS NOT NULL AND ("
            "(r.action = 'agentic_memory_commit' AND r.revision_type = 'created') OR "
            "(r.action = 'agentic_memory_confirm_confirm' AND r.revision_type = 'promoted') OR "
            "(r.action IN ('memory_correct_approve', 'memory_review_accept', 'memory_review_promote') "
            "AND r.revision_type = 'promoted')))"
        )
        possible = {str(row["id"]) for row in cursor}
    if not possible:
        return
    rows = store.list_memories(
        status="active", memory_types=("procedure",), domains=domains,
        sensitivity_allowed=sensitivity, projects=projects, include_expired=False,
        lessons_only=True, lesson_projection=True, lesson_lazy_metadata=lazy_metadata,
    )
    yield from (row for row in rows if str(row["id"]) in possible)


def select_lesson_outcome_rows(store: Any, *, writer: str, projects: Sequence[str], updated_since: datetime):
    """Select a writer's waiting and recent candidates before loading revisions."""
    project_ids = list(project_scope_identity(projects))
    scope = f"AND ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[]" if project_ids else ""
    columns = f"{LESSON_MEMORY_COLUMNS}, status, source_event_ids"
    # Only column and predicate constants are interpolated; user values are parameters.
    base = (
        f"SELECT {columns} FROM memories "  # nosec B608
        "WHERE user_id = app.current_user_id() AND created_by_agent_id = %s "
        "AND deleted_at IS NULL "
        "AND CASE WHEN memory_type = 'procedure' THEN jsonb_typeof(value -> 'lesson') END = 'object' "
        f"{scope}"
    )
    params = (writer, project_ids) if project_ids else (writer,)
    return store._fetch_all(
        f"""{base} AND status = 'candidate'
        UNION ALL
        {base} AND status IN ('active', 'rejected', 'private_only', 'superseded')
          AND updated_at >= %s::timestamptz
        """,
        (*params, *params, updated_since),
    )


def stream_lesson_revisions(
    self: Any, memory_ids: Sequence[str], *, include_reason: bool = False,
) -> Iterator[dict[str, object]]:
    fields = _REVISION_FIELDS | {"reason"} if include_reason else _REVISION_FIELDS
    columns = ", ".join("r." + name.strip() for name in REVISION_COLUMNS.split(",") if name.strip() in fields)
    with self.conn.cursor() as cursor:
        # Column names come from the shared constants; IDs are bound parameters.
        cursor.execute(
            f"SELECT {columns}, r.new_value -> 'lesson' AS publication_lesson "  # nosec B608
            "FROM unnest(%s::uuid[]) WITH ORDINALITY AS selected(memory_id, rank) "
            "JOIN memory_revisions AS r ON r.memory_id = selected.memory_id "
            "ORDER BY selected.rank, r.revision_number ASC, r.sequence_no ASC, r.id ASC",
            (list(memory_ids),),
        )
        yield from cursor


stream_lesson_revisions.__module__ = "alicebot_api.vnext_store"
stream_lesson_revisions.__qualname__ = "PostgresVNextStore.stream_lesson_revisions"
