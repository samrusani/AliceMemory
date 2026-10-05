"""Repair stored derived labels from the same rules as a read.

The open pass never stops a vault from opening. A restore still aborts,
because a published vault must not stay unrepaired.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TypedDict

from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, require_changed
from alicebot_api.vnext_derived_labels import labels_raised_payload, settle_labels
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_project_scope import project_scope_identity


class LabelParts(TypedDict):
    domain: str
    sensitivity: str
    project_scope: list[str]
    project_floor: list[str]


LabelRepair = tuple[str, str, str, LabelParts, LabelParts, dict[str, object]]

REPAIR_STATE_KEY = "derived_labels_v3"
_TABLE_KIND = {
    "sources": "source",
    "memories": "memory",
    "open_loops": "open_loop",
    "generated_artifacts": "artifact",
    "projects": "project",
    "beliefs": "belief",
}
_WRITABLE = frozenset({"memories", "open_loops", "generated_artifacts", "projects"})
INPUT_SELECTS_V3 = {
    "sources": "SELECT id, user_id, domain, sensitivity, metadata_json, deleted_at FROM sources",
    "memories": ("SELECT id, user_id, domain, sensitivity, metadata_json, value, project_id, deleted_at FROM memories"),
    "open_loops": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, project_id, source_id, memory_id FROM open_loops"
    ),
    "generated_artifacts": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, artifact_type FROM generated_artifacts"
    ),
    "projects": "SELECT id, user_id, domain, sensitivity, metadata_json FROM projects",
    "beliefs": "SELECT id, user_id, memory_id FROM beliefs",
}
_UPDATES = {
    "memories": "UPDATE memories SET domain = ?, sensitivity = ?, metadata_json = json_patch(metadata_json, ?), project_id = ? WHERE user_id = ? AND id = ? AND domain = ? AND sensitivity = ? AND metadata_json = ? AND project_id IS ?",
    "open_loops": (
        "UPDATE open_loops SET domain = ?, sensitivity = ?, metadata_json = json_patch(metadata_json, ?), project_id = ? WHERE user_id = ? AND id = ? AND domain = ? AND sensitivity = ? AND metadata_json = ? AND project_id IS ?"
    ),
}


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _same(previous: Mapping[str, object], new: Mapping[str, object]) -> bool:
    return (
        previous["domain"] == new["domain"]
        and previous["sensitivity"] == new["sensitivity"]
        and project_scope_identity(previous["project_scope"]) == project_scope_identity(new["project_scope"])
        and project_scope_identity(previous["project_floor"]) == project_scope_identity(new["project_floor"])
    )


def plan_label_repairs(
    tables: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[LabelRepair]:
    """Label changes for derived rows whose stored label is below the inputs.

    A malformed record makes its row unverified and is not rewritten. A cycle
    that does not settle raises ``DerivedDomainRepairError``.
    """

    nodes: list[dict[str, object]] = []
    index: list[tuple[str, dict[str, object]]] = []
    for table, rows in tables.items():
        kind = _TABLE_KIND.get(table, table)
        for row in rows:
            node = dict(row)
            node["kind"] = kind
            node["_stored_metadata"] = row.get("metadata_json")
            node["metadata_json"] = _json_object(row.get("metadata_json"))
            if isinstance(row.get("value"), str):
                node["value"] = _json_object(row.get("value"))
            nodes.append(node)
            index.append((table, node))
    settled = settle_labels(nodes, on_cycle="raise")
    changes: list[LabelRepair] = []
    for (table, node), label in zip(index, settled.rows, strict=True):
        if not label.derived or label.unverified or table not in _WRITABLE:
            continue
        previous: LabelParts = {
            "domain": str(node.get("domain") or "unknown"),
            "sensitivity": str(node.get("sensitivity") or "unknown"),
            "project_scope": list(label.stored_scope),
            "project_floor": list(label.stored_floor),
        }
        new: LabelParts = {
            "domain": label.domain,
            "sensitivity": label.sensitivity,
            "project_scope": list(label.project_scope),
            "project_floor": list(label.project_floor),
        }
        if _same(previous, new):
            continue
        changes.append((table, str(node.get("user_id") or ""), str(node.get("id") or ""), previous, new, node))
    return changes


def label_project_id(scope: Sequence[object]) -> str | None:
    """Mirror a single UUID scope; named and global scopes use metadata only."""
    from uuid import UUID

    if len(scope) != 1:
        return None
    try:
        return str(UUID(str(scope[0])))
    except ValueError:
        return None


def _load_tables(conn) -> dict[str, list[dict[str, object]]]:
    available = {
        row[0] if not isinstance(row, dict) else row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    tables: dict[str, list[dict[str, object]]] = {}
    for table, statement in INPUT_SELECTS_V3.items():
        if table not in available:
            continue
        cursor = conn.execute(statement)
        names = [column[0] for column in cursor.description]
        tables[table] = [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
    return tables


def _stamped(conn) -> bool:
    return (
        conn.execute("SELECT value FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,)).fetchone() is not None
    )


def relabel_labels_sqlite(conn, *, restoring: bool = False, explicit: bool = False) -> int:
    """Raise stored derived labels once, or always for a restore or owner repair.

    When no transaction is open this begins one and reads the state key inside
    it. A caller that already has a transaction keeps it.
    """

    owns = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        owns = True
    try:
        if not restoring and not explicit and _stamped(conn):
            if owns:
                conn.commit()
            return 0
        changes = plan_label_repairs(_load_tables(conn))
        for table, user, stored, previous, new, node in changes:
            metadata = {"project_scope": list(new["project_scope"]), "project_floor": list(new["project_floor"])}
            raw_metadata = node.get("_stored_metadata")
            changed = conn.execute(
                _UPDATES[table],
                (
                    new["domain"],
                    new["sensitivity"],
                    json.dumps(metadata),
                    label_project_id(new["project_scope"]),
                    user,
                    stored,
                    previous["domain"],
                    previous["sensitivity"],
                    raw_metadata,
                    node.get("project_id"),
                ),
            ).rowcount
            require_changed(changed, table, stored)
        for table, user, stored, previous, new, _node in changes:
            target = "memory" if table == "memories" else "open_loop"
            event = build_event_log_record(
                event_type=f"{target}.labels_raised",
                actor_type="system",
                target_type=target,
                target_id=stored,
                payload=labels_raised_payload(cause="repair_v3", previous=previous, new=new),
            )
            conn.execute(
                """INSERT INTO event_log (id, user_id, event_type, actor_type, target_type, target_id,
                   occurred_at, payload_json, integrity_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["id"],
                    user,
                    event["event_type"],
                    event["actor_type"],
                    event["target_type"],
                    stored,
                    event["occurred_at"],
                    json.dumps(event["payload_json"]),
                    event["integrity_hash"],
                ),
            )
        conn.execute("INSERT OR REPLACE INTO alice_schema_state (key, value) VALUES (?, ?)", (REPAIR_STATE_KEY, "1"))
        if owns:
            conn.commit()
        return len(changes)
    except Exception:
        if owns:
            conn.rollback()
        raise


def classify_stored_labels(
    tables: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[list[LabelRepair], dict[str, list[str]]]:
    """Rows below their inputs, and unverified ids grouped by reason.

    A cycle is reported as unverified instead of raising. ``labels check`` uses this.
    """

    nodes: list[dict[str, object]] = []
    index: list[tuple[str, dict[str, object]]] = []
    for table, rows in tables.items():
        kind = _TABLE_KIND.get(table, table)
        for row in rows:
            node = dict(row)
            node["kind"] = kind
            node["_stored_metadata"] = row.get("metadata_json")
            node["metadata_json"] = _json_object(row.get("metadata_json"))
            if isinstance(row.get("value"), str):
                node["value"] = _json_object(row.get("value"))
            nodes.append(node)
            index.append((table, node))
    settled = settle_labels(nodes, on_cycle="unverified")
    below: list[LabelRepair] = []
    unverified: dict[str, list[str]] = {}
    for (table, node), label in zip(index, settled.rows, strict=True):
        if not label.derived:
            continue
        row_id = str(node.get("id") or "")
        if label.unverified:
            unverified.setdefault(label.reason or "unverified", []).append(row_id)
            continue
        if table not in _WRITABLE:
            continue
        previous: LabelParts = {
            "domain": str(node.get("domain") or "unknown"),
            "sensitivity": str(node.get("sensitivity") or "unknown"),
            "project_scope": list(label.stored_scope),
            "project_floor": list(label.stored_floor),
        }
        new: LabelParts = {
            "domain": label.domain,
            "sensitivity": label.sensitivity,
            "project_scope": list(label.project_scope),
            "project_floor": list(label.project_floor),
        }
        if not _same(previous, new):
            below.append((table, str(node.get("user_id") or ""), row_id, previous, new, node))
    return below, unverified


def format_label_check(
    below: Sequence[tuple],
    unverified: Mapping[str, Sequence[str]],
    *,
    limit: int = 20,
) -> str:
    """Counts and up to ``limit`` ids per cause. No row text."""

    lines = [f"below_inputs {len(below)}"]
    for _table, _user, row_id, _previous, _new, _node in list(below)[:limit]:
        lines.append(f"  {row_id}")
    for reason in sorted(unverified):
        ids = list(unverified[reason])
        lines.append(f"{reason} {len(ids)}")
        for row_id in ids[:limit]:
            lines.append(f"  {row_id}")
    return "\n".join(lines)


class LabelCheckUnavailable(RuntimeError):
    """The label planner could not read this store; no count is available."""


def load_postgres_label_tables(conn) -> dict[str, list[dict[str, object]]]:
    """Read every label input using PostgreSQL's current RLS identity."""

    tables = {}
    for table, statement in INPUT_SELECTS_V3.items():
        cursor = conn.execute(statement)
        names = [column[0] for column in cursor.description]
        tables[table] = [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
    return tables


def label_gap_counts(store: object) -> tuple[int, int]:
    """Counts from the real store, or an explicit unavailable result.

    PostgreSQL reads use a savepoint so an unavailable table does not poison
    the doctor's surrounding application transaction.
    """

    conn = getattr(store, "conn", None)
    if conn is None:
        raise LabelCheckUnavailable("derived label counts are unavailable for this store")
    module = type(conn).__module__
    try:
        if module.startswith("sqlite3"):
            tables = _load_tables(conn)
        elif module.startswith("psycopg"):
            with conn.transaction():
                tables = load_postgres_label_tables(conn)
        else:
            raise LabelCheckUnavailable("derived label counts are unavailable for this store")
        below, unverified = classify_stored_labels(tables)
    except LabelCheckUnavailable:
        raise
    except Exception as exc:
        raise LabelCheckUnavailable("derived label counts could not be read") from exc
    return len(below), sum(len(ids) for ids in unverified.values())


def recorded_sqlite_label_repairs(conn, user_id: str) -> dict[tuple[str, str, str], set[str]]:
    """Previous label values recorded for each row and dimension.

    The key is (table, stored id, dimension). A value is explained only when an
    event of this vault says the row held it before.
    """

    repairs: dict[tuple[str, str, str], set[str]] = {}
    statements = (
        (
            "memories",
            "SELECT target_id, payload_json FROM event_log WHERE user_id = ? AND event_type = 'memory.domain_relabelled'",
        ),
        (
            "memories",
            "SELECT target_id, payload_json FROM event_log WHERE user_id = ? AND event_type = 'memory.labels_raised'",
        ),
        (
            "open_loops",
            "SELECT target_id, payload_json FROM event_log WHERE user_id = ? AND event_type = 'open_loop.labels_raised'",
        ),
    )
    for table, statement in statements:
        cursor = conn.execute(statement, (str(user_id),))
        names = [column[0] for column in cursor.description]
        for raw in cursor.fetchall():
            row = raw if isinstance(raw, dict) else dict(zip(names, raw))
            payload = _json_object(row.get("payload_json"))
            row_id = str(row.get("target_id") or "")
            if not row_id:
                continue
            if "previous_domain" in payload:
                repairs.setdefault((table, row_id, "domain"), set()).add(str(payload.get("previous_domain")))
            previous = payload.get("previous")
            if not isinstance(previous, Mapping):
                continue
            repairs.setdefault((table, row_id, "domain"), set()).add(str(previous.get("domain") or "unknown"))
            repairs.setdefault((table, row_id, "sensitivity"), set()).add(str(previous.get("sensitivity") or "unknown"))
            scope = previous.get("project_scope")
            floor = previous.get("project_floor")
            repairs.setdefault((table, row_id, "project_scope"), set()).add(
                json.dumps(list(project_scope_identity(scope if isinstance(scope, list) else [])), sort_keys=True)
            )
            repairs.setdefault((table, row_id, "project_floor"), set()).add(
                json.dumps(list(project_scope_identity(floor if isinstance(floor, list) else [])), sort_keys=True)
            )
    return repairs


__all__ = [
    "INPUT_SELECTS_V3",
    "LabelCheckUnavailable",
    "load_postgres_label_tables",
    "REPAIR_STATE_KEY",
    "DerivedDomainRepairError",
    "classify_stored_labels",
    "format_label_check",
    "label_gap_counts",
    "plan_label_repairs",
    "recorded_sqlite_label_repairs",
    "relabel_labels_sqlite",
]
