"""Write-path label lock, insert floor, and metadata merge.

The pure rules live in ``vnext_derived_labels``. This module is what a store
calls when it writes a row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from typing import Any

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, require_changed
from alicebot_api.vnext_derived_labels import (
    MARKER_KEYS,
    PROPAGATION_BOUND,
    SENSITIVITY_RANK,
    dependencies_of,
    LabelPropagationTooLarge,
    generation_domain,
    identifier,
    is_derived,
    labels_raised_payload,
    settle_labels,
)
from alicebot_api.vnext_event_log import build_event_log_record, integrity_hash_for_event
from alicebot_api.vnext_project_scope import project_scope_identity, resolve_project_scope, source_project_scope
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


RETRYABLE_DETAIL = (
    "the label change was not applied because another change was running; nothing was changed; try again"
)
REFUSED_DETAIL = "the label change could not be applied to every dependent row; nothing was changed"


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


def _sqlite(store: Any) -> bool:
    return type(getattr(store, "conn", None)).__module__.startswith("sqlite3")


def _compact_id(value: object) -> str:
    return re.sub(r"[-{}\s]", "", str(value).lower())


def should_propagate(kind: str, before: Mapping[str, object] | None, after: Mapping[str, object] | None) -> bool:
    """True when a relabel must walk dependants. A pure loosening does not."""

    if not before or not after:
        return False
    old_domain = str(before.get("domain") or "unknown")
    new_domain = str(after.get("domain") or "unknown")
    old_sensitivity = str(before.get("sensitivity") or "unknown")
    new_sensitivity = str(after.get("sensitivity") or "unknown")
    rank_up = SENSITIVITY_RANK.get(new_sensitivity, 2) > SENSITIVITY_RANK.get(old_sensitivity, 2)
    domain_move = new_domain in RESTRICTED_DOMAINS and new_domain != old_domain
    if kind == "source":
        old_scope = source_project_scope(before)
        new_scope = source_project_scope(after)
    else:
        old_scope = resolve_project_scope(before).values
        new_scope = resolve_project_scope(after).values
    scope_move = project_scope_identity(old_scope) != project_scope_identity(new_scope)
    return bool(rank_up or domain_move or scope_move)


def acquire_exclusive_label_lock(store: Any) -> None:
    """Take L exclusive. On Postgres, wait at most 3 seconds."""

    if _sqlite(store):
        store.lock_label_writes(exclusive=True)
        return
    with store.conn.cursor() as cur:
        cur.execute("SELECT current_setting('lock_timeout') AS lock_timeout")
        row = cur.fetchone()
        previous = row["lock_timeout"] if isinstance(row, Mapping) else row[0]
        cur.execute("SET LOCAL lock_timeout = '3s'")
        try:
            store.lock_label_writes(exclusive=True)
        finally:
            try:
                cur.execute("SELECT set_config('lock_timeout', %s, true)", (str(previous),))
            except Exception:
                # A lock timeout aborts the transaction. Rollback drops the local setting.
                pass


def list_dependants(store: Any, ids: Sequence[str]) -> list[dict[str, object]]:
    """Rows whose recorded text may name one of ``ids``. The caller filters exactly."""

    wanted = [str(item) for item in ids if str(item)]
    if not wanted:
        return []
    compacts = [_compact_id(item) for item in wanted if len(_compact_id(item)) >= 8]
    if not compacts:
        compacts = [_compact_id(item) for item in wanted if _compact_id(item)]
    found: list[dict[str, object]] = []
    if _sqlite(store):
        found.extend(_sqlite_dependants(store, "memories", "memory", compacts, wanted, with_value=True))
        found.extend(_sqlite_dependants(store, "open_loops", "open_loop", compacts, wanted, with_value=False))
    else:
        found.extend(_postgres_dependants(store, "memories", "memory", compacts, with_value=True))
        found.extend(_postgres_dependants(store, "open_loops", "open_loop", compacts, with_value=False))
        found.extend(_postgres_dependants(store, "generated_artifacts", "artifact", compacts, with_value=False))
        found.extend(_postgres_dependants(store, "projects", "project", compacts, with_value=False))
    return found


def _like_clause(column: str, count: int, *, qmark: bool) -> tuple[str, list[str]]:
    mark = "?" if qmark else "%s"
    parts = []
    params: list[str] = []
    for _ in range(count):
        parts.append(
            "replace(replace(replace(replace(lower(coalesce("
            + column
            + ",'')),'-',''),'{',''),'}',''),' ','') LIKE "
            + mark
        )
    return " OR ".join(parts), params


def _sqlite_dependants(store: Any, table: str, kind: str, compacts: Sequence[str], raw_ids: Sequence[str], *, with_value: bool) -> list[dict[str, object]]:
    extra = ", value, project_id" if with_value else ", NULL AS value, project_id, source_id, memory_id"
    if table == "memories":
        extra = ", value, project_id, NULL AS source_id, NULL AS memory_id"
    text_clause, _ = _like_clause("metadata_json", len(compacts), qmark=True)
    params: list[object] = [store.user_id, *[f"%{item}%" for item in compacts]]
    value_sql = ""
    if with_value:
        value_clause, _ = _like_clause("value", len(compacts), qmark=True)
        value_sql = f" OR {value_clause}"
        params.extend(f"%{item}%" for item in compacts)
    column_sql = ""
    if table == "open_loops":
        marks = ",".join("?" for _ in raw_ids)
        column_sql = f" OR source_id IN ({marks}) OR memory_id IN ({marks})"
        params.extend(raw_ids)
        params.extend(raw_ids)
    rows = store._fetch_all(
        f"""
            SELECT id, user_id, domain, sensitivity, metadata_json{extra}
            FROM {table}
            WHERE user_id = ? AND ({text_clause}{value_sql}{column_sql})
            """,
        tuple(params),
    )
    for row in rows:
        row["kind"] = kind
    return rows


def _postgres_dependants(store: Any, table: str, kind: str, compacts: Sequence[str], *, with_value: bool) -> list[dict[str, object]]:
    extra = ""
    if table == "memories":
        extra = ", value, project_id"
    elif table == "open_loops":
        extra = ", NULL::jsonb AS value, project_id, source_id, memory_id"
    elif table == "generated_artifacts":
        extra = ", NULL::jsonb AS value, artifact_type"
    else:
        extra = ", NULL::jsonb AS value"
    text_clause, _ = _like_clause("metadata_json::text", len(compacts), qmark=False)
    params: list[object] = [f"%{item}%" for item in compacts]
    value_sql = ""
    if with_value:
        value_clause, _ = _like_clause("value::text", len(compacts), qmark=False)
        value_sql = f" OR {value_clause}"
        params.extend(f"%{item}%" for item in compacts)
    rows = store._fetch_all(
        f"""
            SELECT id::text AS id, user_id::text AS user_id, domain, sensitivity, metadata_json{extra}
            FROM {table}
            WHERE ({text_clause}{value_sql})
            """,
        tuple(params),
    )
    for row in rows:
        row["kind"] = kind
    return rows


def _depends_on(row: Mapping[str, object], frontier: set[str]) -> bool:
    kind = str(row.get("kind") or "")
    try:
        deps = dependencies_of(kind, row)
    except ValueError:
        return False
    return any(identifier(dep_id) in frontier or _compact_id(dep_id) in frontier for _dep_kind, dep_id in deps)


def walk_dependants(store: Any, roots: Sequence[str]) -> list[dict[str, object]]:
    """Exact dependants of ``roots``, including later rows in the chain."""

    frontier = {identifier(item) for item in roots}
    frontier |= {_compact_id(item) for item in roots}
    seen = set(frontier)
    found: list[dict[str, object]] = []
    pending = [str(item) for item in roots]
    while pending:
        if len(seen) > PROPAGATION_BOUND:
            raise LabelPropagationTooLarge(f"label propagation stopped after {PROPAGATION_BOUND} rows")
        batch = pending[:200]
        pending = pending[200:]
        matched: list[dict[str, object]] = []
        batch_ids = {identifier(item) for item in batch} | {_compact_id(item) for item in batch}
        for row in list_dependants(store, batch):
            row_id = identifier(row.get("id"))
            if row_id in seen or _compact_id(row.get("id")) in seen:
                continue
            if not _depends_on(row, batch_ids):
                continue
            seen.add(row_id)
            seen.add(_compact_id(row.get("id")))
            matched.append(row)
        found.extend(matched)
        pending.extend(str(row["id"]) for row in matched)
    return found


def _label_fields(row: Mapping[str, object]) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    metadata = row.get("metadata_json")
    meta = metadata if isinstance(metadata, Mapping) else {}
    scope = meta.get("project_scope", ())
    floor = meta.get("project_floor", ())
    return (
        str(row.get("domain") or "unknown"),
        str(row.get("sensitivity") or "unknown"),
        tuple(scope) if isinstance(scope, (list, tuple)) else (),
        tuple(floor) if isinstance(floor, (list, tuple)) else (),
    )


def write_settled_label(
    store: Any,
    *,
    kind: str,
    row_id: str,
    domain: str,
    sensitivity: str,
    metadata: Mapping[str, object],
    project_id: str | None,
    expected_domain: str,
    expected_sensitivity: str,
) -> None:
    """Label-only update. A statement that changes no row refuses the whole relabel."""

    table = {"memory": "memories", "open_loop": "open_loops", "artifact": "generated_artifacts", "project": "projects"}[kind]
    blob = json.dumps(dict(metadata))
    if _sqlite(store):
        project_sql = ", project_id = ?" if table in {"memories", "open_loops"} else ""
        params: list[object] = [domain, sensitivity, blob]
        if project_sql:
            params.append(project_id)
        params.extend([str(row_id), store.user_id, expected_domain, expected_sensitivity])
        cursor = store._execute(
            f"""
                UPDATE {table}
                SET domain = ?, sensitivity = ?, metadata_json = ?{project_sql}
                WHERE id = ? AND user_id = ? AND domain = ? AND sensitivity = ?
                """,
            tuple(params),
        )
        require_changed(int(cursor.rowcount), table, str(row_id))
        return
    project_sql = ", project_id = %s" if table in {"memories", "open_loops"} else ""
    params = [domain, sensitivity, blob]
    if project_sql:
        params.append(project_id)
    params.extend([str(row_id), expected_domain, expected_sensitivity])
    store._fetch_one(
        "write_settled_label",
        f"""
            UPDATE {table}
            SET domain = %s, sensitivity = %s, metadata_json = %s::jsonb{project_sql}
            WHERE id = %s::uuid AND domain = %s AND sensitivity = %s
            RETURNING id
            """,
        tuple(params),
    )


def propagate(store: Any, changed: Sequence[tuple[str, str]], *, cause: str) -> int:
    """Recompute dependants of ``changed`` rows and write the ones that rise."""

    store.lock_label_writes(exclusive=True)
    roots = [row_id for _kind, row_id in changed]
    affected = walk_dependants(store, roots)
    if not affected:
        return 0
    nodes: list[dict[str, object]] = []
    reader = getattr(store, "read_label_rows", None)
    if callable(reader):
        for kind, row_id in changed:
            for row in reader(kind, [row_id]):
                copied = dict(row)
                copied["kind"] = kind
                nodes.append(copied)
    needed: dict[str, list[str]] = {}
    for row in affected:
        for dep_kind, dep_id in dependencies_of(str(row.get("kind")), row):
            needed.setdefault(dep_kind, []).append(dep_id)
    if callable(reader):
        for dep_kind, dep_ids in needed.items():
            for row in reader(dep_kind, dep_ids):
                copied = dict(row)
                copied["kind"] = dep_kind
                nodes.append(copied)
    for row in affected:
        nodes.append(dict(row))
    settled = settle_labels(nodes)
    written = 0
    for row in affected:
        label = settled.by_stored(str(row.get("kind")), str(row.get("id")))
        if label.unverified:
            continue
        previous = _label_fields(row)
        current = (label.domain, label.sensitivity, tuple(label.project_scope), tuple(label.project_floor))
        if (
            previous[0] == current[0]
            and previous[1] == current[1]
            and project_scope_identity(previous[2]) == project_scope_identity(current[2])
            and project_scope_identity(previous[3]) == project_scope_identity(current[3])
        ):
            continue
        metadata = dict(row.get("metadata_json")) if isinstance(row.get("metadata_json"), Mapping) else {}
        metadata["project_scope"] = list(label.project_scope)
        metadata["project_floor"] = list(label.project_floor)
        project_id = label.project_scope[0] if len(project_scope_identity(label.project_scope)) == 1 else None
        if project_id is not None:
            try:
                from uuid import UUID

                UUID(str(project_id))
            except ValueError:
                project_id = None
        write_settled_label(
            store,
            kind=str(row.get("kind")),
            row_id=str(row.get("id")),
            domain=label.domain,
            sensitivity=label.sensitivity,
            metadata=metadata,
            project_id=project_id,
            expected_domain=previous[0],
            expected_sensitivity=previous[1],
        )
        event = build_event_log_record(
            event_type=f"{label.kind}.labels_raised",
            actor_type="system",
            target_type=label.kind,
            target_id=str(row.get("id")),
            payload=labels_raised_payload(
                cause=cause,
                previous={
                    "domain": previous[0],
                    "sensitivity": previous[1],
                    "project_scope": list(previous[2]),
                    "project_floor": list(previous[3]),
                },
                new={
                    "domain": label.domain,
                    "sensitivity": label.sensitivity,
                    "project_scope": list(label.project_scope),
                    "project_floor": list(label.project_floor),
                },
            ),
        )
        append = getattr(store, "append_event", None)
        if callable(append):
            append(event)
        written += 1
    return written


def propagate_after_write(store: Any, *, kind: str, before: Mapping[str, object] | None, after: Mapping[str, object] | None, cause: str = "input_relabelled") -> int:
    if not should_propagate(kind, before, after) or after is None:
        return 0
    return propagate(store, [(kind, str(after.get("id")))], cause=cause)


def count_rows_hidden_by_scope_move(store: Any, source: Mapping[str, object], new_scope: Sequence[str]) -> int:
    """How many derived rows a project-bound key would lose if ``source`` moved."""

    source_id = str(source.get("id") or "")
    if not source_id:
        return 0
    affected = walk_dependants(store, [source_id])
    if not affected:
        return 0
    current = dict(source)
    current["kind"] = "source"
    moved = dict(source)
    moved["kind"] = "source"
    metadata = dict(source.get("metadata_json")) if isinstance(source.get("metadata_json"), Mapping) else {}
    metadata["project_scope"] = list(new_scope)
    moved["metadata_json"] = metadata
    before = settle_labels([current, *[dict(row) for row in affected]])
    after = settle_labels([moved, *[dict(row) for row in affected]])
    hidden = 0
    for row in affected:
        old = before.by_stored(str(row.get("kind")), str(row.get("id")))
        new = after.by_stored(str(row.get("kind")), str(row.get("id")))
        old_ids = set(project_scope_identity(old.project_scope))
        new_ids = set(project_scope_identity(new.project_scope))
        if old_ids - new_ids:
            hidden += 1
    return hidden


def raise_source_to_replacement(store: Any, old: Mapping[str, object], replacement: Mapping[str, object]) -> None:
    """Raise a retiring source to the replacement when that label is stricter, then propagate."""

    old_domain = str(old.get("domain") or "unknown")
    new_domain = str(replacement.get("domain") or "unknown")
    domain = new_domain if new_domain in RESTRICTED_DOMAINS and old_domain not in RESTRICTED_DOMAINS else old_domain
    sensitivity = str(old.get("sensitivity") or "unknown")
    replacement_sensitivity = str(replacement.get("sensitivity") or "unknown")
    if SENSITIVITY_RANK.get(replacement_sensitivity, 2) > SENSITIVITY_RANK.get(sensitivity, 2):
        sensitivity = replacement_sensitivity
    if domain == old_domain and sensitivity == str(old.get("sensitivity") or "unknown"):
        return
    store.update_source(
        source_id=str(old.get("id")),
        patch={"domain": domain, "sensitivity": sensitivity},
        actor_type="system",
    )


def label_error_response(exc: BaseException) -> tuple[int, str, str | None] | None:
    """``(status, detail, retry_after)`` for a relabel failure, or None."""

    if isinstance(exc, (LabelPropagationTooLarge, DerivedDomainRepairError)):
        return 409, REFUSED_DETAIL, None
    if type(exc).__name__ in {"LockNotAvailable", "DeadlockDetected", "SerializationFailure"}:
        return 503, RETRYABLE_DETAIL, "2"
    return None


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
    "REFUSED_DETAIL",
    "RETRYABLE_DETAIL",
    "acquire_exclusive_label_lock",
    "count_rows_hidden_by_scope_move",
    "label_error_response",
    "propagate",
    "propagate_after_write",
    "raise_source_to_replacement",
    "remember_floor_event",
    "should_propagate",
    "takes_label_lock",
    "without_insert_floor",
]
