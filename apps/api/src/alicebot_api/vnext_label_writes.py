"""Write-path label lock, insert floor, and metadata merge.

The pure rules live in ``vnext_derived_labels``. This module is what a store
calls when it writes a row.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from typing import Any

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_labels import (
    MARKER_KEYS,
    dependencies_of,
    generation_domain,
    is_derived,
    labels_raised_payload,
    settle_labels,
)
from alicebot_api.vnext_event_log import build_event_log_record, integrity_hash_for_event
from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_repositories import JsonObject

LABEL_METADATA_KEYS = ("project_scope", "project_floor", "derived_from")
STRICT_LOCK_ORDER = False
_FLOOR_ENABLED = True

_KIND_EVENT = {
    "memory": "memory",
    "open_loop": "open_loop",
    "artifact": "artifact",
    "project": "project",
    "source": "source",
}


class LabelLockOrderError(RuntimeError):
    """A label write ran outside a transaction, or it took locks in the wrong order."""


def takes_label_lock(fn: Any) -> Any:
    """Take the shared label lock before a store method writes or row-locks a label table."""

    @wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        lock = getattr(self, "lock_label_writes", None)
        if callable(lock):
            lock(exclusive=False)
        return fn(self, *args, **kwargs)

    wrapper.__takes_label_lock__ = True  # type: ignore[attr-defined]
    return wrapper


@contextmanager
def without_insert_floor() -> Iterator[None]:
    """Let a test store a derived row with the labels it wrote."""

    global _FLOOR_ENABLED
    previous = _FLOOR_ENABLED
    _FLOOR_ENABLED = False
    try:
        yield
    finally:
        _FLOOR_ENABLED = previous


def merge_protected_metadata(
    stored: Mapping[str, object] | None,
    patch: Mapping[str, object] | None,
    *,
    label_write: bool,
) -> dict[str, object]:
    """Keep label keys and omitted marker keys when a caller writes metadata back.

    ``label_write`` is for the relabel itself, which may change the label keys.
    A marker key that the patch omits stays as stored either way.
    """

    stored_map = dict(stored) if isinstance(stored, Mapping) else {}
    if patch is None:
        return stored_map
    patch_map = dict(patch)
    result = dict(patch_map)
    if not label_write:
        for key in LABEL_METADATA_KEYS:
            if key in stored_map:
                result[key] = stored_map[key]
            else:
                result.pop(key, None)
    for key in MARKER_KEYS:
        if key not in patch_map and key in stored_map:
            result[key] = stored_map[key]
    return result


def _in_transaction(store: Any) -> bool:
    conn = getattr(store, "conn", None)
    if conn is None:
        return True
    info = getattr(conn, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is not None:
        # psycopg TransactionStatus.IDLE is 0.
        return int(status) != 0
    in_transaction = getattr(conn, "in_transaction", None)
    if isinstance(in_transaction, bool):
        return in_transaction
    return True


def _label_tuple(payload: Mapping[str, object]) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    metadata = payload.get("metadata_json")
    meta = metadata if isinstance(metadata, Mapping) else {}
    scope = meta.get("project_scope", payload.get("project_scope", ()))
    floor = meta.get("project_floor", ())
    scope_values = tuple(scope) if isinstance(scope, (list, tuple)) else ()
    floor_values = tuple(floor) if isinstance(floor, (list, tuple)) else ()
    return (
        str(payload.get("domain") or "unknown"),
        str(payload.get("sensitivity") or "unknown"),
        project_scope_identity(scope_values),
        project_scope_identity(floor_values),
    )


def apply_insert_floor(store: Any, kind: str, payload: Mapping[str, object]) -> tuple[JsonObject, JsonObject | None]:
    """Raise a derived payload to its inputs. Returns ``(payload, event or None)``.

    The event has no target id yet. The insert fills that in after the row exists.
    An unverified dependency does not refuse the insert.
    """

    body = dict(payload)
    if not _FLOOR_ENABLED or not is_derived(kind, body):
        return body, None
    if not _in_transaction(store):
        raise LabelLockOrderError("the insert floor requires an open transaction")
    user_id = str(getattr(store, "user_id", body.get("user_id") or ""))
    nodes: list[dict[str, object]] = []
    grouped: dict[str, list[str]] = {}
    for dep_kind, dep_id in dependencies_of(kind, body):
        grouped.setdefault(dep_kind, []).append(dep_id)
    reader = getattr(store, "read_label_rows", None)
    if callable(reader):
        for dep_kind, ids in grouped.items():
            for row in reader(dep_kind, ids):
                copied = dict(row)
                copied["kind"] = dep_kind
                copied.setdefault("user_id", user_id)
                nodes.append(copied)
    if not user_id and nodes:
        user_id = str(nodes[0].get("user_id") or "")
    own_id = str(body.get("id") or "new-derived-row")
    own = dict(body)
    own["kind"] = kind
    own["id"] = own_id
    own["user_id"] = user_id
    nodes.append(own)
    settled = settle_labels(nodes).by_stored(kind, own_id, user_id=user_id or None)
    if settled.unverified:
        return body, None
    domain = settled.domain
    if str(body.get("domain") or "unknown") in RESTRICTED_DOMAINS:
        domain = generation_domain(str(body.get("domain")), [settled.domain])
    metadata = dict(body.get("metadata_json")) if isinstance(body.get("metadata_json"), Mapping) else {}
    metadata["project_scope"] = list(settled.project_scope)
    metadata["project_floor"] = list(settled.project_floor)
    updated = dict(body)
    updated["domain"] = domain
    updated["sensitivity"] = settled.sensitivity
    updated["metadata_json"] = metadata
    if len(project_scope_identity(settled.project_scope)) != 1:
        updated["project_id"] = None
    before = _label_tuple(body)
    after = _label_tuple(updated)
    if before == after:
        return updated, None
    event = build_event_log_record(
        event_type=f"{_KIND_EVENT.get(kind, kind)}.labels_raised",
        actor_type="system",
        target_type=_KIND_EVENT.get(kind, kind),
        payload=labels_raised_payload(
            cause="insert_floor",
            previous={
                "domain": before[0],
                "sensitivity": before[1],
                "project_scope": list(before[2]),
                "project_floor": list(before[3]),
            },
            new={
                "domain": after[0],
                "sensitivity": after[1],
                "project_scope": list(settled.project_scope),
                "project_floor": list(settled.project_floor),
            },
        ),
    )
    return updated, event


def remember_floor_event(store: Any, event: JsonObject | None, target_id: object) -> None:
    """Append an insert-floor event once the row id is known."""

    if event is None:
        return
    event = dict(event)
    event["target_id"] = str(target_id) if target_id else None
    event.pop("integrity_hash", None)
    event["integrity_hash"] = integrity_hash_for_event(event)
    append = getattr(store, "append_event", None)
    if callable(append):
        append(event)


__all__ = [
    "LABEL_METADATA_KEYS",
    "STRICT_LOCK_ORDER",
    "LabelLockOrderError",
    "apply_insert_floor",
    "merge_protected_metadata",
    "remember_floor_event",
    "takes_label_lock",
    "without_insert_floor",
]
