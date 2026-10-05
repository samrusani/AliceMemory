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
from dataclasses import replace
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
    input_admitted,
    labels_raised_payload,
    settle_labels,
    stored_scope,
    union_floor,
)
from alicebot_api.vnext_label_closure import collect_label_rows
from alicebot_api.vnext_event_log import build_event_log_record, integrity_hash_for_event
from alicebot_api.vnext_project_scope import project_floor_shape, project_scope_identity, resolve_project_scope, source_project_scope
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


def held_label_locks(store: Any) -> tuple[bool, bool, bool]:
    """Read the live S, L and exclusive L grants, including savepoint rollback."""

    with store.conn.cursor() as cur:
        cur.execute("""
            SELECT
              coalesce(bool_or(classid = (hashtext('vnext_supersession')::bigint & 4294967295)::oid), false) AS graph,
              coalesce(bool_or(classid = (hashtext('vnext_labels')::bigint & 4294967295)::oid), false) AS labels,
              coalesce(bool_or(classid = (hashtext('vnext_labels')::bigint & 4294967295)::oid
                AND mode = 'ExclusiveLock'), false) AS exclusive
            FROM pg_locks
            WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
              AND objsubid = 2
              AND objid = (hashtext(app.current_user_id()::text)::bigint & 4294967295)::oid
        """)
        row = cur.fetchone()
    if isinstance(row, Mapping):
        return bool(row["graph"]), bool(row["labels"]), bool(row["exclusive"])
    return bool(row[0]), bool(row[1]), bool(row[2])


def before_graph_lock(store: Any) -> None:
    """Strict tests refuse S after L using the current database grants."""

    if STRICT_LOCK_ORDER:
        graph, labels, _exclusive = held_label_locks(store)
        if labels and not graph:
            raise LabelLockOrderError("the graph lock must precede the label lock")


def require_exclusive_label_lock(store: Any) -> None:
    """A changing hook must hold exclusive L before taking any row lock."""

    if _sqlite(store):
        store.lock_label_writes(exclusive=True)
        return
    _graph, _labels, exclusive = held_label_locks(store)
    if exclusive:
        return
    if STRICT_LOCK_ORDER:
        raise LabelLockOrderError("the label change requires the exclusive label lock before row locks")
    acquire_exclusive_label_lock(store)


def prepare_label_patch(
    store: Any, kind: str, before: Mapping[str, object] | None, patch: Mapping[str, object]
) -> JsonObject:
    """Check a proposed label change before its UPDATE or FOR UPDATE statement."""

    proposed = dict(before or {})
    proposed.update({key: value for key, value in patch.items() if value is not None})
    proposed["kind"] = kind
    old = _label_fields({**before, "kind": kind}) if before else None
    new = _label_fields(proposed)
    if old and (old[:2] != new[:2] or project_scope_identity(old[2]) != project_scope_identity(new[2]) or project_scope_identity(old[3]) != project_scope_identity(new[3])):
        require_exclusive_label_lock(store)
    return dict(patch)


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
    own_id = str(body.get("id") or "new-derived-row")
    own = dict(body)
    own["kind"] = kind
    own["id"] = own_id
    own["user_id"] = user_id
    nodes, exceeded = collect_label_rows(store, [own], max_nodes=PROPAGATION_BOUND)
    if exceeded:
        raise LabelPropagationTooLarge(f"label propagation stopped after {PROPAGATION_BOUND} rows")
    if not user_id:
        tenants = {str(node.get("user_id")) for node in nodes if node.get("user_id")}
        if len(tenants) == 1:
            user_id = tenants.pop()
            nodes[0]["user_id"] = user_id
    settled = settle_labels(nodes, on_cycle="unverified").by_stored(kind, own_id, user_id=user_id or None)
    domain = settled.domain
    if settled.unverified:
        domain = generation_domain(str(body.get("domain") or "unknown"), [str(node.get("domain") or "unknown") for node in nodes])
    if str(body.get("domain") or "unknown") in RESTRICTED_DOMAINS:
        domain = generation_domain(str(body.get("domain")), [settled.domain])
    raw_metadata = body.get("metadata_json")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    metadata["project_scope"] = list(settled.project_scope)
    metadata["project_floor"] = list(settled.project_floor)
    if settled.unverified:
        metadata["project_floor"] = list(union_floor(settled.project_floor, [
            *(stored_scope(str(node["kind"]), node) for node in nodes),
            *(project_floor_shape(node)[1] for node in nodes),
        ]))
    updated = dict(body)
    updated["domain"] = domain
    updated["sensitivity"] = "regulated" if settled.unverified else settled.sensitivity
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
                "project_scope": list(after[2]),
                "project_floor": list(after[3]),
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
            except Exception:  # nosec B110 # aborted transactions cannot restore local settings; rollback clears them
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
    # A nested JSON string may encode every character of an id. Keep all
    # escaped candidates for the canonical dependency parser below.
    text_clause += " OR instr(coalesce(metadata_json, ''), char(92)) > 0"
    params: list[object] = [store.user_id, *[f"%{item}%" for item in compacts]]
    value_sql = ""
    if with_value:
        value_clause, _ = _like_clause("value", len(compacts), qmark=True)
        value_sql = f" OR {value_clause} OR instr(coalesce(value, ''), char(92)) > 0"
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
            """,  # nosec B608 # internal literal table/columns; every external value is bound
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
        extra = ", NULL::jsonb AS value, project_id, source_id::text AS source_id, memory_id::text AS memory_id"
    elif table == "generated_artifacts":
        extra = ", NULL::jsonb AS value, artifact_type"
    else:
        extra = ", NULL::jsonb AS value"
    # Normalize each column once per row, rather than once per frontier id.
    # This remains only a superset lookup; the canonical parser selects exact edges.
    pattern = "(?:" + "|".join(re.escape(item) for item in compacts) + ")"

    def candidate_clause(column: str) -> str:
        return (
            "replace(replace(replace(replace(lower(coalesce("
            + column
            + ",'')),'-',''),'{',''),'}',''),' ','') ~ %s"
        )

    text_clause = candidate_clause("metadata_json::text")
    text_clause += " OR strpos(coalesce(metadata_json::text, ''), chr(92)) > 0"
    params: list[object] = [pattern]
    value_sql = ""
    if with_value:
        value_clause = candidate_clause("value::text")
        value_sql = f" OR {value_clause} OR strpos(coalesce(value::text, ''), chr(92)) > 0"
        params.append(pattern)
    column_sql = ""
    if table == "open_loops":
        for column in ("source_id", "memory_id"):
            clause = candidate_clause(f"{column}::text")
            column_sql += f" OR {clause}"
            params.append(pattern)
    rows = store._fetch_all(
        f"""
            SELECT id::text AS id, user_id::text AS user_id, domain, sensitivity, metadata_json{extra}
            FROM {table}
            WHERE ({text_clause}{value_sql}{column_sql})
            """,  # nosec B608 # internal literal table/columns; every external value is bound
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
        belief_aliases = getattr(store, "list_belief_ids_for_memories", None)
        if callable(belief_aliases):
            for belief_id in belief_aliases(batch):
                alias = identifier(belief_id)
                if alias not in seen:
                    seen.add(alias)
                    seen.add(_compact_id(belief_id))
                    pending.append(str(belief_id))
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
    scope = source_project_scope(row) if row.get("kind") == "source" or "source_type" in row else resolve_project_scope(row).values
    floor = meta.get("project_floor", ())
    return (
        str(row.get("domain") or "unknown"),
        str(row.get("sensitivity") or "unknown"),
        tuple(scope) if isinstance(scope, (list, tuple)) else (),
        tuple(floor) if isinstance(floor, (list, tuple)) else (),
    )


def clamp_owner_patch(
    store: Any, *, kind: str, before: Mapping[str, object] | None, patch: Mapping[str, object]
) -> JsonObject:
    """Keep a derived row at or above its inputs when an edit would lower it.

    The store writes the higher label, records ``labels_raised`` with cause
    ``floor_clamped`` when the stored label changes, and sets
    ``store._label_floor_applied`` so the review answer can name it.
    """

    store._label_floor_applied = False
    proposed_patch = dict(patch)
    if before is None or not is_derived(kind, before):
        return proposed_patch
    proposed = dict(before)
    for key in ("domain", "sensitivity", "project_id"):
        if key in proposed_patch and proposed_patch[key] is not None:
            proposed[key] = proposed_patch[key]
    patch_metadata = proposed_patch.get("metadata_json")
    if isinstance(patch_metadata, dict):
        stored_meta = before.get("metadata_json")
        meta = dict(stored_meta) if isinstance(stored_meta, dict) else {}
        meta.update(patch_metadata)
        proposed["metadata_json"] = meta
    proposed["kind"] = kind
    nodes, exceeded = collect_label_rows(store, [proposed], max_nodes=PROPAGATION_BOUND)
    if exceeded:
        return proposed_patch
    try:
        label = settle_labels(nodes).by_stored(kind, str(before.get("id") or ""))
    except KeyError:
        return proposed_patch
    if label.unverified:
        return proposed_patch
    requested = _label_fields(proposed)
    settled = (label.domain, label.sensitivity, tuple(label.project_scope), tuple(label.project_floor))
    if (
        requested[0] == settled[0]
        and requested[1] == settled[1]
        and project_scope_identity(requested[2]) == project_scope_identity(settled[2])
        and project_scope_identity(requested[3]) == project_scope_identity(settled[3])
    ):
        return proposed_patch
    proposed_patch["domain"] = label.domain
    proposed_patch["sensitivity"] = label.sensitivity
    proposed_metadata = proposed.get("metadata_json")
    metadata = dict(proposed_metadata) if isinstance(proposed_metadata, Mapping) else {}
    metadata["project_scope"] = list(label.project_scope)
    metadata["project_floor"] = list(label.project_floor)
    proposed_patch["metadata_json"] = metadata
    stored = _label_fields(before)
    if not (
        stored[0] == settled[0]
        and stored[1] == settled[1]
        and project_scope_identity(stored[2]) == project_scope_identity(settled[2])
        and project_scope_identity(stored[3]) == project_scope_identity(settled[3])
    ):
        require_exclusive_label_lock(store)
        event = build_event_log_record(
            event_type=f"{kind}.labels_raised",
            actor_type="system",
            target_type=kind,
            target_id=str(before.get("id") or ""),
            payload=labels_raised_payload(
                cause="floor_clamped",
                previous={
                    "domain": stored[0],
                    "sensitivity": stored[1],
                    "project_scope": list(stored[2]),
                    "project_floor": list(stored[3]),
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
    store._label_floor_applied = True
    return proposed_patch


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
    blob = json.dumps({key: metadata[key] for key in ("project_scope", "project_floor") if key in metadata})
    if _sqlite(store):
        project_sql = ", project_id = ?" if table in {"memories", "open_loops"} else ""
        params: list[object] = [domain, sensitivity, blob]
        if project_sql:
            params.append(project_id)
        params.extend([str(row_id), store.user_id, expected_domain, expected_sensitivity])
        cursor = store._execute(
            f"""
                UPDATE {table}
                SET domain = ?, sensitivity = ?, metadata_json = json_patch(metadata_json, ?){project_sql}
                WHERE id = ? AND user_id = ? AND domain = ? AND sensitivity = ?
                """,  # nosec B608 # table comes from the closed kind map; values are bound
            tuple(params),
        )
        require_changed(int(cursor.rowcount), table, str(row_id))
        return
    project_sql = ", project_id = %s" if table in {"memories", "open_loops"} else ""
    params = [domain, sensitivity, blob]
    if project_sql:
        params.append(project_id)
    params.extend([str(row_id), expected_domain, expected_sensitivity])
    row = store._fetch_optional_one(
        f"""
            UPDATE {table}
            SET domain = %s, sensitivity = %s, metadata_json = metadata_json || %s::jsonb{project_sql}
            WHERE id = %s::uuid AND domain = %s AND sensitivity = %s
            RETURNING id
            """,  # nosec B608 # table comes from the closed kind map; values are bound
        tuple(params),
    )
    require_changed(int(row is not None), table, str(row_id))


LABEL_TABLE_ORDER = ("generated_artifacts", "projects", "open_loops", "memories")
_LABEL_TABLES = {"artifact": "generated_artifacts", "project": "projects", "open_loop": "open_loops", "memory": "memories"}


def lock_settled_label_rows(store: Any, changes: Sequence[Mapping[str, object]]) -> None:
    """Lock exactly the changed rows in a stable table and UUID order."""

    if _sqlite(store):
        return
    grouped: dict[str, set[str]] = {}
    for row in changes:
        grouped.setdefault(_LABEL_TABLES[str(row["kind"])], set()).add(str(row["id"]))
    with store.conn.cursor() as cur:
        for table in LABEL_TABLE_ORDER:
            ids = sorted(grouped.get(table, ()))
            if ids:
                cur.execute(
                    f"SELECT id FROM {table} WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE",  # nosec B608 # closed internal table map
                    (ids,),
                )
                locked = cur.fetchall()
                if len(locked) != len(ids):
                    raise DerivedDomainRepairError("a planned label row disappeared before it could be locked")


def propagate(store: Any, changed: Sequence[tuple[str, str]], *, cause: str) -> int:
    """Recompute dependants of ``changed`` rows and write the ones that rise."""

    require_exclusive_label_lock(store)
    roots = [row_id for _kind, row_id in changed]
    affected = walk_dependants(store, roots)
    if not affected:
        return 0
    roots_rows = [dict(row) for row in affected]
    reader = getattr(store, "read_label_rows", None)
    if callable(reader):
        for kind, row_id in changed:
            roots_rows.extend({**dict(row), "kind": kind} for row in reader(kind, [identifier(row_id)]))
    nodes, exceeded = collect_label_rows(store, roots_rows, max_nodes=PROPAGATION_BOUND)
    if exceeded:
        raise LabelPropagationTooLarge(f"label propagation stopped after {PROPAGATION_BOUND} rows")
    settled = settle_labels(nodes)
    changes: list[tuple[dict[str, object], Any, tuple[str, str, tuple[str, ...], tuple[str, ...]]]] = []
    for row in affected:
        label = settled.by_stored(str(row.get("kind")), str(row.get("id")))
        if label.unverified:
            ancestry, exceeded = collect_label_rows(store, [row], max_nodes=PROPAGATION_BOUND)
            if exceeded:
                raise LabelPropagationTooLarge(f"label propagation stopped after {PROPAGATION_BOUND} rows")
            label = replace(
                label,
                domain=generation_domain(label.domain, [str(node.get("domain") or "unknown") for node in ancestry]),
                sensitivity="regulated",
                project_floor=union_floor(label.project_floor, [
                    *(stored_scope(str(node["kind"]), node) for node in ancestry),
                    *(project_floor_shape(node)[1] for node in ancestry),
                ]),
            )
        previous = _label_fields(row)
        current = (label.domain, label.sensitivity, tuple(label.project_scope), tuple(label.project_floor))
        if (
            previous[0] == current[0]
            and previous[1] == current[1]
            and project_scope_identity(previous[2]) == project_scope_identity(current[2])
            and project_scope_identity(previous[3]) == project_scope_identity(current[3])
        ):
            continue
        changes.append((row, label, previous))
    lock_settled_label_rows(store, [row for row, _label, _previous in changes])
    written = 0
    for row, label, previous in sorted(changes, key=lambda item: (LABEL_TABLE_ORDER.index(_LABEL_TABLES[str(item[0]["kind"])]), str(item[0]["id"]))):
        raw_metadata = row.get("metadata_json")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
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
    raw_metadata = source.get("metadata_json")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    metadata["project_scope"] = list(new_scope)
    moved["metadata_json"] = metadata
    nodes, exceeded = collect_label_rows(store, [current, *[dict(row) for row in affected]], max_nodes=PROPAGATION_BOUND)
    if exceeded:
        raise LabelPropagationTooLarge("the source move preview exceeded the label propagation bound")
    before = settle_labels(nodes, on_cycle="unverified")
    moved_nodes = [moved if str(row.get("kind")) == "source" and str(row.get("id")) == source_id else row for row in nodes]
    after = settle_labels(moved_nodes, on_cycle="unverified")
    hidden = 0
    for row in affected:
        old = before.by_stored(str(row.get("kind")), str(row.get("id")))
        new = after.by_stored(str(row.get("kind")), str(row.get("id")))
        if old.unverified or not old.project_scope:
            continue
        binding = project_scope_identity([*old.project_scope, *old.project_floor])
        new_row = {**row, "metadata_json": {"project_scope": list(new.project_scope), "project_floor": list(new.project_floor)}}
        if new.unverified or not new.project_scope or not input_admitted(str(row["kind"]), new_row, binding):
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

    if isinstance(exc, LabelPropagationTooLarge):
        return 409, REFUSED_DETAIL + "; cause: propagation_bound", None
    if isinstance(exc, DerivedDomainRepairError):
        cause = "row_changed" if "changed no row" in str(exc) or "disappeared" in str(exc) else "dependency_cycle"
        return 409, REFUSED_DETAIL + "; cause: " + cause, None
    if isinstance(exc, LabelLockOrderError):
        return 409, REFUSED_DETAIL + "; cause: lock_order", None
    if type(exc).__name__ in {"LockNotAvailable", "DeadlockDetected", "SerializationFailure"}:
        return 503, RETRYABLE_DETAIL, "2"
    if type(exc).__module__.startswith("psycopg"):
        return 409, REFUSED_DETAIL + "; cause: database_error", None
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
    "clamp_owner_patch",
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
