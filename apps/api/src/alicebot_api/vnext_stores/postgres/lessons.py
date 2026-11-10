"""PostgreSQL publication records for an ordered lesson candidate set."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from alicebot_api.vnext_stores.postgres.columns import REVISION_COLUMNS

_REVISION_FIELDS = frozenset(
    {"memory_id", "revision_number", "sequence_no", "id", "action", "revision_type", "actor_type", "actor_id", "created_at", "text_before", "text_after"}
)


def stream_lesson_revisions(self: Any, memory_ids: Sequence[str]) -> Iterator[dict[str, object]]:
    columns = ", ".join("r." + name.strip() for name in REVISION_COLUMNS.split(",") if name.strip() in _REVISION_FIELDS)
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
