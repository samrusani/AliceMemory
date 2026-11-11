"""SQLite publication records for an ordered lesson candidate set."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS, REVISION_COLUMNS

_MEMORY_FIELDS = frozenset(
    {"id", "user_id", "value", "metadata_json", "created_by_agent_id", "created_at", "domain", "sensitivity", "project_id"}
)
_VECTOR_FIELDS = frozenset(
    {"id", "user_id", "metadata_json", "domain", "sensitivity", "project_id", "created_at", "updated_at", "valid_from", "canonical_text", "title", "summary"}
)
LESSON_MEMORY_COLUMNS = ", ".join(column for column in MEMORY_COLUMNS if column in _MEMORY_FIELDS)
LESSON_VECTOR_COLUMNS = ", ".join(column for column in MEMORY_COLUMNS if column in _VECTOR_FIELDS)

_REVISION_FIELDS = frozenset(
    {"memory_id", "revision_number", "sequence_no", "id", "action", "revision_type", "actor_type", "actor_id", "created_at", "text_before", "text_after"}
)


def stream_lesson_memories(store: Any, *, domains, sensitivity, projects, lazy_metadata):
    """Stream the same candidates as the lesson list, without collecting them."""
    from alicebot_api.vnext_project_scope import normalize_project_scope
    from alicebot_api.vnext_stores.sqlite.memory_access import _memory_list_clauses
    from alicebot_api.vnext_stores.sqlite.query_predicates import _stated_exclusion

    clauses = _memory_list_clauses(
        store, status="active", statuses=None, memory_types=("procedure",), domains=domains,
        sensitivity_allowed=sensitivity, created_at_start=None, created_at_end=None,
        query=None, include_expired=False,
    )
    if clauses is None:
        return
    head, head_params, tail, tail_params = clauses
    project, project_params = store._project_clause(
        tuple(normalize_project_scope(projects or ())), global_excluded_domains=_stated_exclusion(())
    )
    columns = LESSON_MEMORY_COLUMNS
    if lazy_metadata:
        columns = columns.replace("metadata_json", "metadata_json AS lesson_metadata_raw")
    cursor = store._execute(
        f"SELECT {columns} FROM memories {head} {tail} "  # nosec B608
        "AND json_type(value, '$.lesson') = 'object' "
        "AND EXISTS (SELECT 1 FROM memory_revisions AS r "
        "WHERE r.memory_id = memories.id AND r.user_id = memories.user_id "
        "AND json_type(r.new_value, '$.lesson') IS NOT NULL AND ("
        "(r.action = 'agentic_memory_commit' AND r.revision_type = 'created') OR "
        "(r.action = 'agentic_memory_confirm_confirm' AND r.revision_type = 'promoted') OR "
        "(r.action IN ('memory_correct_approve', 'memory_review_accept', 'memory_review_promote') "
        "AND r.revision_type = 'promoted'))) "
        f"{project} ORDER BY updated_at DESC, id DESC",
        tuple([*head_params, *tail_params, *project_params]),
    )
    try:
        for row in cursor:
            yield store._decode_row(row)
    finally:
        cursor.close()


def stream_lesson_revisions(self: Any, memory_ids: Sequence[str]) -> Iterator[dict[str, object]]:
    columns = ", ".join("r." + column for column in REVISION_COLUMNS if column in _REVISION_FIELDS)
    # Column names come from the shared constants; IDs are bound parameters.
    cursor = self._execute(
        f"SELECT {columns}, json_extract(r.new_value, '$.lesson') AS publication_lesson "  # nosec B608
        "FROM json_each(?) AS selected "
        "JOIN memory_revisions AS r ON r.memory_id = selected.value AND r.user_id = ? "
        "ORDER BY CAST(selected.key AS INTEGER), r.revision_number ASC, r.sequence_no ASC, r.id ASC",
        (json.dumps(list(memory_ids)), self.user_id),
    )
    for row in cursor:
        yield self._decode_row(row)


stream_lesson_revisions.__module__ = "alicebot_api.sqlite_store"
stream_lesson_revisions.__qualname__ = "SQLiteVNextStore.stream_lesson_revisions"
