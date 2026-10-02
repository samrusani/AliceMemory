"""Mechanical MCP retrieval shared carrier."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from alicebot_api.sqlite_store import SQLiteVNextStore
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_project_scope import (
    is_global_scope,
    project_identifier_identity,
    project_scopes_overlap,
)

from .shared import _json_object

_SQLITE_REVIEWABLE_STATUSES = frozenset({"active", "candidate"})

# Agent context reads active memory only. Candidates stay in the review
# tools until a reviewer promotes them. ``_vnext_resume`` is the SQLite and
# Postgres resume path, so this tuple is what both stores are asked for.
_CONTEXT_MEMORY_STATUSES = frozenset({"active"})


_SQLITE_NEXT_ACTION_MEMORY_TYPES = frozenset({"open_loop", "commitment"})


_SQLITE_OPEN_LOOP_ACTIVE_STATUSES = frozenset({"open", "waiting"})


_ASCII_QUERY_CASE_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def _utc_now_iso_text() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _row_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    value = row.get(key)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _row_in_window(
    row: Mapping[str, object],
    *,
    key: str,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if since is None and until is None:
        return True
    moment = _row_datetime(row, key)
    if moment is None:
        return False
    if since is not None:
        bounded_since = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
        if moment < bounded_since:
            return False
    if until is not None:
        bounded_until = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
        if moment > bounded_until:
            return False
    return True


def _memory_matches_query(row: Mapping[str, object], query: str | None) -> bool:
    if query is None:
        return True
    needle = query.translate(_ASCII_QUERY_CASE_TRANSLATION)
    for key in ("title", "canonical_text", "summary"):
        value = row.get(key)
        if isinstance(value, str) and needle in value.translate(_ASCII_QUERY_CASE_TRANSLATION):
            return True
    return False


def _memory_matches_project(row: Mapping[str, object], project: str | None) -> bool:
    if project is None:
        return True
    needle = project_identifier_identity(project)
    if needle == "":
        return False
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        project_id = metadata.get("project_id")
        if isinstance(project_id, str) and project_identifier_identity(project_id) == needle:
            return True
    domain = row.get("domain")
    return isinstance(domain, str) and project_identifier_identity(domain) == needle


def _created_at_sort_key(row: Mapping[str, object]) -> tuple[str, str]:
    return str(row.get("created_at") or ""), str(row.get("id") or "")


def _compact_vnext_memory(row: Mapping[str, object], *, provenance_count: int) -> JsonObject:
    return _json_object(
        {
            "id": str(row.get("id")),
            "title": row.get("title"),
            "canonical_text": row.get("canonical_text"),
            "created_at": row.get("created_at"),
            "domain": row.get("domain"),
            "status": row.get("status"),
            "memory_type": row.get("memory_type"),
            "confidence": row.get("confidence"),
            "provenance_count": provenance_count,
        }
    )


def _compact_vnext_open_loop(row: Mapping[str, object]) -> JsonObject:
    return _json_object(
        {
            "kind": "open_loop",
            "id": str(row.get("id")),
            "title": row.get("title"),
            "status": row.get("status"),
            "priority": row.get("priority"),
            "due_at": row.get("due_at"),
            "opened_at": row.get("opened_at"),
            "domain": row.get("domain"),
            "project_id": row.get("project_id"),
        }
    )


def _compact_vnext_event(row: Mapping[str, object]) -> JsonObject:
    return _json_object(
        {
            "id": str(row.get("id")),
            "event_type": row.get("event_type"),
            "actor_type": row.get("actor_type"),
            "target_type": row.get("target_type"),
            "target_id": row.get("target_id"),
            "occurred_at": row.get("occurred_at"),
        }
    )


def _provenance_count(store: SQLiteVNextStore, memory_id: object) -> int:
    return len(store.list_provenance_links(target_type="memory", target_id=str(memory_id)))


def _resource_matches_project_scope(resource: Mapping[str, object], project_scope: tuple[str, ...]) -> bool:
    if not project_scope:
        return True
    return project_scopes_overlap(resource_project_scope(resource), project_scope)


def _resource_is_held_back_global(resource: Mapping[str, object], exclude_global_domains: frozenset[str]) -> bool:
    """A global row (its scope holds no Alice project id) in a domain the project view holds back.

    The test is on the row, as the brief's ``_is_held_back`` is: a note of this project in the
    same domain is not held back, and a global note in any other domain is not either. An empty
    set holds nothing back.
    """

    return (
        bool(exclude_global_domains)
        and resource.get("domain") in exclude_global_domains
        and is_global_scope(resource_project_scope(resource))
    )


def _resource_matches_domains(resource: Mapping[str, object], domains: tuple[str, ...]) -> bool:
    """Same admit rule as sqlite list_memories: empty is unscoped; unknown stays visible."""

    if not domains:
        return True
    domain = resource.get("domain")
    return domain in domains or domain == "unknown"


def _resource_matches_sensitivity(
    resource: Mapping[str, object],
    sensitivity_allowed: tuple[str, ...] | None,
) -> bool:
    """Same admit rule as sqlite list_memories. None is unscoped; empty admits nothing."""

    if sensitivity_allowed is None:
        return True
    if not sensitivity_allowed:
        return False
    return (resource.get("sensitivity") or "unknown") in sensitivity_allowed
