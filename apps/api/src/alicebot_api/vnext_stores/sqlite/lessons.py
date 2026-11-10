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
