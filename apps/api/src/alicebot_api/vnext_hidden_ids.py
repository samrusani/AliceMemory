"""Withhold the id of a row the caller may not read.

A readable report can list the ids of rows it was made from. Those ids used to come back as stored.
A caller with limits now loses them, in place: a list drops the entry, a single id becomes ``None``, and a
``count`` beside ``items`` matches the list that remains. The owner and an unbound admin key keep every id.
The words a report copied stay; this only removes ids.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

_READER: ContextVar[tuple[Any, Any] | None] = ContextVar("hidden_id_reader", default=None)


def remember_reader(store: Any, identity: Any) -> None:
    """Remember who a command-line dump is for, until the process ends the command."""

    _READER.set((store, identity))


def reader_for_dump() -> tuple[Any, Any]:
    current = _READER.get()
    if current is None:
        return None, None
    return current

from alicebot_api.vnext_source_fence import SourceReadFence

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_KINDS = ("memory", "source", "artifact", "open_loop")
_LIST_FIELDS = frozenset(
    {
        "candidate_memory_ids",
        "candidate_open_loop_ids",
        "candidate_edge_ids",
        "sources",
        "memories",
        "artifacts",
        "open_loops",
        "beliefs",
        "source_ids",
        "memory_ids",
        "open_loop_ids",
        "artifact_ids",
        "belief_ids",
        "source_refs",
        "stale_marked_memory_ids",
        "provenance",
        "member_ids",
        "cluster_member_ids",
        "source_event_ids",
        "member_snapshots",
        "instances",
    }
)
_SCALAR_FIELDS = frozenset(
    {
        "candidate_memory_id",
        "belief_memory_id",
        "artifact_id",
        "output_artifact_id",
        "source_id",
        "memory_id",
        "target_id",
        "source_item",
        "connected_item",
    }
)


def _uuids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_UUID.findall(value))
    elif isinstance(value, Mapping):
        for child in value.values():
            found.update(_uuids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_uuids(child))
    return found


def _names(text: str, hidden: set[str]) -> bool:
    return any(item in text for item in hidden)


def _caller_is_limited(identity: Any) -> bool:
    """True when this caller has a read ceiling. An unbound admin keeps every id.

    A request may name a project without binding the key. That selection is not a limit.
    """

    if identity is None:
        return False
    if getattr(identity, "permission_profile", None) == "admin_agent" and not getattr(identity, "project_scope_locked", False):
        return False
    return SourceReadFence.for_identity(identity).entity_read_fenced


def unreadable_ids(store: Any, identity: Any, payload: object) -> set[str]:
    """Ids of memories, sources, artifacts and open loops in ``payload`` that ``identity`` may not read.

    The owner and an unbound admin key have no limits, so this is empty and the payload is left as it was.
    An id that names no stored row is left out of the set: nothing has shown it to be a row.
    """

    if not _caller_is_limited(identity):
        return set()
    candidates = _uuids(payload)
    if not candidates:
        return set()
    reader = getattr(store, "read_label_rows", None)
    if not callable(reader):
        return set()
    from alicebot_api.vnext_agent_control import AgentPolicyBlockedError
    from alicebot_api.vnext_label_guard import guard_for_caller

    # memory.recall is a read. http.operator.access refuses a read-only key and a bound key, which would
    # raise here and skip the withholding on the tools those keys can still call.
    try:
        guard = guard_for_caller(store, identity, action="memory.recall")
    except AgentPolicyBlockedError:
        guard = None
    hidden: set[str] = set()
    for kind in _KINDS:
        rows = [row for row in reader(kind, sorted(candidates)) if isinstance(row, Mapping) and row.get("id") is not None]
        if guard is None:
            hidden.update(str(row.get("id")) for row in rows)
            continue
        admitted = {str(row.get("id")) for row in guard.admit_rows(kind, rows)}
        for row in rows:
            row_id = str(row.get("id"))
            if row_id not in admitted or _row_is_gone(row):
                hidden.add(row_id)
        if kind == "source":
            hidden.update(_deleted_source_ids(store, [str(row.get("id")) for row in rows]))
    return hidden


def _row_is_gone(row: Mapping[str, Any]) -> bool:
    """An archived or deleted row is not readable. A forgotten memory still shows in history."""

    if row.get("deleted_at") is not None:
        return True
    return str(row.get("status") or "") == "archived"


def _deleted_source_ids(store: Any, ids: list[str]) -> set[str]:
    """Source label rows omit deleted_at. A deleted source is still a row the caller may not read."""

    getter = getattr(store, "get_sources_by_ids", None)
    if not ids or not callable(getter):
        return set()
    try:
        found = getter(ids, include_deleted=True)
    except TypeError:
        return set()
    return {
        str(row.get("id"))
        for row in found
        if isinstance(row, Mapping) and row.get("deleted_at") is not None and row.get("id") is not None
    }


def _drop_text(value: str, hidden: set[str]) -> str | None:
    if _names(value, hidden) and (_UUID.fullmatch(value) or any(value.endswith(item) for item in hidden)):
        return None
    cleaned = value
    for item in hidden:
        cleaned = cleaned.replace(item, "")
    return cleaned


def strip_hidden_ids(value: Any, hidden: set[str]) -> Any:
    """``value`` with every id in ``hidden`` removed from the lists and fields that carry one.

    ``hidden`` empty returns ``value`` unchanged, the same object. A ``count`` beside ``items`` is the length of
    ``items`` after the drop.
    """

    if not hidden:
        return value
    stripped, _changed = _strip(value, hidden, field=None)
    return stripped


def _strip(value: Any, hidden: set[str], *, field: str | None) -> tuple[Any, bool]:
    if isinstance(value, str):
        if field in _SCALAR_FIELDS or field in _LIST_FIELDS or field == "content_markdown":
            cleaned = _drop_text(value, hidden)
            return cleaned, cleaned != value
        return value, False
    if isinstance(value, list):
        kept: list[Any] = []
        changed = False
        for item in value:
            if isinstance(item, str) and (field in _LIST_FIELDS or field in _SCALAR_FIELDS) and _names(item, hidden):
                changed = True
                continue
            if isinstance(item, Mapping) and field in {"member_snapshots", "instances"}:
                marker = item.get("id") if field == "member_snapshots" else item.get("memory_id")
                if isinstance(marker, str) and _names(marker, hidden):
                    changed = True
                    continue
            child, child_changed = _strip(item, hidden, field=field)
            changed = changed or child_changed
            kept.append(child)
        return kept, changed
    if isinstance(value, Mapping):
        output: dict[Any, Any] = {}
        changed = False
        for key, child in value.items():
            key_text = str(key)
            if key_text in _SCALAR_FIELDS and isinstance(child, str) and _names(child, hidden):
                output[key] = None
                changed = True
                continue
            if key_text == "content_markdown" and isinstance(child, str):
                cleaned = _drop_text(child, hidden)
                output[key] = cleaned
                changed = changed or cleaned != child
                continue
            child_field = key_text if key_text in _LIST_FIELDS or key_text in _SCALAR_FIELDS else field
            stripped, child_changed = _strip(child, hidden, field=child_field)
            output[key] = stripped
            changed = changed or child_changed
        if changed and isinstance(output.get("items"), list) and "count" in output:
            output["count"] = len(output["items"])
        return output, changed
    return value, False


def withhold_json_body(request: Any, body: bytes, *, database_url: str | None = None) -> bytes | None:
    """JSON bytes with hidden ids removed, or ``None`` when the body should be sent unchanged."""

    identity = getattr(getattr(request, "state", None), "vnext_agent_identity", None)
    path = getattr(getattr(request, "url", None), "path", "")
    if identity is None or not str(path).startswith("/v0/vnext") or not body:
        return None
    if not _caller_is_limited(identity):
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    raw_user = request.query_params.get("user_id")
    if not raw_user:
        return None
    from uuid import UUID

    from alicebot_api.db import user_connection
    from alicebot_api.vnext_store import PostgresVNextStore

    try:
        user_id = UUID(str(raw_user))
    except ValueError:
        return None
    if database_url is None:
        from alicebot_api.config import get_settings

        database_url = get_settings().database_url
    if str(database_url).startswith("sqlite"):
        return None
    with user_connection(database_url, user_id) as conn:
        filtered = apply_hidden_id_withholding(PostgresVNextStore(conn), identity, payload)
    if filtered is payload:
        return None
    return json.dumps(filtered).encode("utf-8")


def filter_http_response(request: Any, response: Any, *, database_url: str | None = None) -> Any:
    """A `/v0/vnext` JSON response with hidden ids removed, or ``response`` itself when there is nothing to remove."""

    from fastapi.responses import JSONResponse

    body = getattr(response, "body", b"") or b""
    updated = withhold_json_body(request, body, database_url=database_url)
    if updated is None:
        return response
    return JSONResponse(status_code=response.status_code, content=json.loads(updated))


def apply_hidden_id_withholding(store: Any, identity: Any, payload: Any) -> Any:
    """The payload a caller may be shown. Unchanged, and the same object, when nothing is withheld."""

    hidden = unreadable_ids(store, identity, payload)
    if not hidden:
        return payload
    return strip_hidden_ids(payload, hidden)
