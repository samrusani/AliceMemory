"""Repair stored derived labels from the same rules as a read.

The open pass never stops a vault from opening. A restore still aborts,
because a published vault must not stay unrepaired.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, require_changed
from alicebot_api.vnext_derived_labels import labels_raised_payload, settle_labels
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_project_scope import project_scope_identity

REPAIR_STATE_KEY = "derived_labels_v3"
_TABLE_KIND = {
    "sources": "source",
    "memories": "memory",
    "open_loops": "open_loop",
}
INPUT_SELECTS_V3 = {
    "sources": "SELECT id, user_id, domain, sensitivity, metadata_json FROM sources",
    "memories": "SELECT id, user_id, domain, sensitivity, metadata_json, value, project_id FROM memories",
    "open_loops": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, project_id, source_id, memory_id "
        "FROM open_loops"
    ),
}
_UPDATES = {
    "memories": "UPDATE memories SET domain = ?, sensitivity = ?, metadata_json = ? WHERE user_id = ? AND id = ?",
    "open_loops": (
        "UPDATE open_loops SET domain = ?, sensitivity = ?, metadata_json = ? WHERE user_id = ? AND id = ?"
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
) -> list[tuple[str, str, str, dict[str, object], dict[str, object], dict[str, object]]]:
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
            node["metadata_json"] = _json_object(row.get("metadata_json"))
            if isinstance(row.get("value"), str):
                node["value"] = _json_object(row.get("value"))
            nodes.append(node)
            index.append((table, node))
    settled = settle_labels(nodes, on_cycle="raise")
    changes = []
    for (table, node), label in zip(index, settled.rows, strict=True):
        if not label.derived or label.unverified or table not in _UPDATES:
            continue
        previous = {
            "domain": str(node.get("domain") or "unknown"),
            "sensitivity": str(node.get("sensitivity") or "unknown"),
            "project_scope": list(label.stored_scope),
            "project_floor": list(label.stored_floor),
        }
        new = {
            "domain": label.domain,
            "sensitivity": label.sensitivity,
            "project_scope": list(label.project_scope),
            "project_floor": list(label.project_floor),
        }
        if _same(previous, new):
            continue
        changes.append((table, str(node.get("user_id") or ""), str(node.get("id") or ""), previous, new, node))
    return changes


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
    return conn.execute("SELECT value FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,)).fetchone() is not None


def relabel_labels_sqlite(conn, *, restoring: bool = False) -> None:
    """Raise stored derived labels once, or again on a restore.

    When no transaction is open this begins one and reads the state key inside
    it. A caller that already has a transaction keeps it.
    """

    owns = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        owns = True
    try:
        if not restoring and _stamped(conn):
            if owns:
                conn.commit()
            return
        changes = plan_label_repairs(_load_tables(conn))
        for table, user, stored, _previous, new, node in changes:
            metadata = dict(node.get("metadata_json") or {})
            metadata["project_scope"] = list(new["project_scope"])
            metadata["project_floor"] = list(new["project_floor"])
            changed = conn.execute(
                _UPDATES[table],
                (new["domain"], new["sensitivity"], json.dumps(metadata), user, stored),
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
    except Exception:
        if owns:
            conn.rollback()
        raise


__all__ = [
    "INPUT_SELECTS_V3",
    "REPAIR_STATE_KEY",
    "DerivedDomainRepairError",
    "plan_label_repairs",
    "relabel_labels_sqlite",
]
