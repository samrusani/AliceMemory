"""Shared visibility rule for lessons awaiting or refused publication."""

from collections.abc import Mapping

from alicebot_api.vnext_agent_control import AgentIdentity


REVIEW_LESSON_STATUSES = ("candidate", "needs_review", "private_only", "rejected")
_UNPUBLISHED_WRITE_STATUSES = ("review_required", "confirmation_required")


def review_lesson_reader(identity: AgentIdentity | None) -> str | None:
    """Return the writer ID to use for review visibility, if standing is limited."""
    from alicebot_api.vnext_memory_commit import caller_may_publish_lesson

    if identity is None or caller_may_publish_lesson(identity):
        return None
    return identity.agent_id


def is_unpublished_lesson(row: Mapping[str, object]) -> bool:
    status = row.get("status")
    if status not in REVIEW_LESSON_STATUSES:
        if status != "superseded":
            return False
        metadata = row.get("metadata_json")
        agentic = metadata.get("agentic_memory") if isinstance(metadata, Mapping) else None
        if not isinstance(agentic, Mapping) or agentic.get("status") not in _UNPUBLISHED_WRITE_STATUSES:
            return False
    value = row.get("value")
    return isinstance(value, Mapping) and isinstance(value.get("lesson"), Mapping)


def hidden_review_lesson(row: Mapping[str, object], reader: str | None) -> bool:
    """Apply the additional standing check to a loaded memory."""
    return reader is not None and row.get("created_by_agent_id") != reader and is_unpublished_lesson(row)


def outside_lesson_review_standing(row: Mapping[str, object], identity: AgentIdentity | None) -> bool:
    return is_unpublished_lesson(row) and hidden_review_lesson(row, review_lesson_reader(identity))


def lock_lesson_review_history(store: object, identity: AgentIdentity | None) -> None:
    """Keep a review from adding private history after an exact read admits its row."""
    if review_lesson_reader(identity) is not None:
        lock = getattr(store, "lock_graph_mutation", None)
        if callable(lock):
            lock()


def review_lesson_selection(reader: str | None, *, sqlite: bool) -> tuple[str, tuple[str, ...]]:
    """Apply the same standing rule before a native count or page limit."""
    if reader is None:
        return "", ()
    marker = "?" if sqlite else "%s"
    lesson = "json_type(value, '$.lesson')" if sqlite else "jsonb_typeof(value -> 'lesson')"
    write_status = (
        "json_extract(metadata_json, '$.agentic_memory.status')" if sqlite
        else "metadata_json #>> '{agentic_memory,status}'"
    )
    statuses = ", ".join(f"'{status}'" for status in REVIEW_LESSON_STATUSES)
    unpublished = ", ".join(f"'{status}'" for status in _UNPUBLISHED_WRITE_STATUSES)
    return (
        f" AND NOT ((status IN ({statuses}) OR"
        f" (status = 'superseded' AND COALESCE({write_status} IN ({unpublished}), FALSE)))"
        f" AND COALESCE({lesson} = 'object', FALSE)"
        f" AND (created_by_agent_id IS NULL OR created_by_agent_id <> {marker}))",
        (reader,),
    )
