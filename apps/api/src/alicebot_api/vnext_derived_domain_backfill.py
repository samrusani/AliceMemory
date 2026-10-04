"""Data-only repair of derived labels from recorded, same-user inputs.

No content, timestamps, provenance or project labels are rewritten. Missing
inputs are not inferred from text. Already restricted rows remain restricted.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from uuid import UUID

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_event_log import build_event_log_record

REPAIR_STATE_KEY = "derived_restricted_domains_v2"

INPUT_SELECTS = {
    "sources": "SELECT id, user_id, domain FROM sources",
    "memories": "SELECT id, user_id, domain, metadata_json, value FROM memories",
    "open_loops": "SELECT id, user_id, domain FROM open_loops",
    "generated_artifacts": "SELECT id, user_id, domain, metadata_json FROM generated_artifacts",
}
_SQLITE_UPDATES = {
    "memories": "UPDATE memories SET domain = ? WHERE user_id = ? AND id = ?",
    "generated_artifacts": "UPDATE generated_artifacts SET domain = ? WHERE user_id = ? AND id = ?",
}
_ID_KEYS = {
    "source_ids": "sources",
    "memory_ids": "memories",
    "open_loop_ids": "open_loops",
    "artifact_ids": "generated_artifacts",
    "member_ids": "memories",
    "cluster_member_ids": "memories",
    "cluster_membership": "memories",
    "stale_marked_memory_ids": "memories",
    "belief_ids": "beliefs",
    "artifact_id": "generated_artifacts",
    "source_artifact_id": "generated_artifacts",
}
_REF_TYPES = {"source": "sources", "memory": "memories", "open_loop": "open_loops", "artifact": "generated_artifacts"}


def _object(value: object) -> Mapping[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, Mapping) else {}


def _identifier(value: object) -> str:
    # PostgreSQL UUID lookup accepts compact and upper-case caller spellings.
    # Producers can persist that original spelling in recorded references.
    try:
        return str(UUID(str(value)))
    except ValueError:
        return str(value)


def _strings(value: object):
    if isinstance(value, str):
        yield _identifier(value)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def recorded_inputs(value: object) -> set[tuple[str, str]]:
    """Read the ID fields the supported producers persist, including nested groups."""
    found: set[tuple[str, str]] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _ID_KEYS:
                found.update((_ID_KEYS[key], item) for item in _strings(nested))
            elif key == "source_refs":
                for ref in _strings(nested):
                    kind, separator, row_id = ref.partition(":")
                    if separator and kind in _REF_TYPES and row_id:
                        found.add((_REF_TYPES[kind], _identifier(row_id)))
            elif isinstance(nested, (Mapping, list)):
                found.update(recorded_inputs(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(recorded_inputs(nested))
    return found


def plan_relabels(tables: Mapping[str, Sequence[Mapping[str, object]]]) -> list[tuple[str, str, str, str]]:
    """Return (table, user_id, id, domain) updates in dependency order.

    Revisit dependants whenever an input label changes. Return only settled
    final labels, independent of the traversal order for a dependency chain.
    Refuse a graph that does not settle within a bounded number of changes;
    callers must roll back rather than publish intermediate labels.
    """
    rows = {(table, str(row["user_id"]), _identifier(row["id"])): row for table, values in tables.items() for row in values}
    labels = {key: row.get("domain", "unknown") for key, row in rows.items()}
    inputs: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for key, row in rows.items():
        table, user, _ = key
        metadata = _object(row.get("metadata_json"))
        if metadata.get("redacted") is True:
            continue
        if table == "memories" and not (
            isinstance(metadata.get("consolidation"), Mapping)
            or metadata.get("discovered_by") == "vnext_weekly_synthesis"
            or metadata.get("workflow") == "project_auto_update"
            or metadata.get("candidate_kind") in ("memory_consolidation", "memory_rollup")
            or _object(row.get("value")).get("kind") == "promoted_artifact"
            or isinstance(metadata.get("source_artifact_id"), str)
        ):
            continue
        if table not in ("memories", "generated_artifacts"):
            continue
        refs = recorded_inputs(metadata) | recorded_inputs(_object(row.get("value")))
        inputs[key] = {(kind, user, row_id) for kind, row_id in refs}
        # Old weekly candidates have no own input list. The parent report
        # records both its inputs and its outputs, providing the missing link.
        if table == "generated_artifacts" and metadata.get("input_summary"):
            for candidate in _strings(metadata.get("candidate_memory_ids")):
                candidate_key = ("memories", user, candidate)
                candidate_row = rows.get(candidate_key)
                if (
                    candidate_row
                    and _object(candidate_row.get("metadata_json")).get("discovered_by") == "vnext_weekly_synthesis"
                ):
                    inputs.setdefault(candidate_key, set()).update((kind, user, row_id) for kind, row_id in refs)
    # Beliefs have no domain column. Resolve each alias to the backing memory
    # before propagation so a newly repaired memory also repairs its reports.
    for key, linked_refs in inputs.items():
        inputs[key] = {
            ("memories", ref[1], str(rows[ref].get("memory_id"))) if ref[0] == "beliefs" and ref in rows else ref
            for ref in linked_refs
        }
    dependants: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for key, linked_inputs in inputs.items():
        for ref in linked_inputs:
            dependants.setdefault(ref, set()).add(key)
    pending = deque(sorted(inputs))
    queued = set(inputs)
    remaining_changes = max(1, len(inputs)) * (len(RESTRICTED_DOMAINS) + 1)
    while pending:
        key = pending.popleft()
        queued.remove(key)
        domain = derived_domain(
            ({"domain": labels[ref]} for ref in sorted(inputs[key]) if ref in labels),
            fallback=str(labels[key]),
        )
        if domain not in RESTRICTED_DOMAINS or domain == labels[key]:
            continue
        remaining_changes -= 1
        if remaining_changes < 0:
            raise ValueError("derived domain repair did not settle")
        labels[key] = domain
        for dependant in sorted(dependants.get(key, ())):
            if dependant not in queued:
                pending.append(dependant)
                queued.add(dependant)
    return [(*key, str(labels[key])) for key in sorted(inputs) if labels[key] != rows[key].get("domain", "unknown")]


def relabel_event(table: str, user: str, row_id: str, previous: object, domain: str):
    """One auditable label change, with no copied text or input identifiers."""
    target_type = "memory" if table == "memories" else "artifact"
    event = build_event_log_record(
        event_type=f"{target_type}.domain_relabelled",
        actor_type="system",
        target_type=target_type,
        target_id=row_id,
        payload={"repair": REPAIR_STATE_KEY, "previous_domain": previous, "domain": domain},
    )
    event["user_id"] = user
    return event


def _fetch_dicts(cursor):
    names = [column[0] for column in cursor.description]
    return [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]


def recorded_sqlite_domain_repairs(conn, user_id: str) -> set[tuple[str, str, str]]:
    """Known old-to-new labels for collision checking on a repeated restore."""
    repairs = set()
    for row in _fetch_dicts(
        conn.execute(
            "SELECT target_id, payload_json FROM event_log WHERE user_id = ? AND event_type = 'memory.domain_relabelled'",
            (str(user_id),),
        )
    ):
        payload = _object(row.get("payload_json"))
        if payload.get("repair") == REPAIR_STATE_KEY:
            repairs.add((str(row["target_id"]), str(payload.get("previous_domain")), str(payload.get("domain"))))
    return repairs


def relabel_sqlite(conn, *, restoring: bool = False) -> None:
    """Upgrade once, or repair a complete staged restore before publication."""
    state_key = REPAIR_STATE_KEY
    if not restoring and conn.execute("SELECT value FROM alice_schema_state WHERE key = ?", (state_key,)).fetchone():
        return
    tables = {}
    available = {
        row[0] if not isinstance(row, dict) else row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table, statement in INPUT_SELECTS.items():
        if table not in available:
            continue
        tables[table] = _fetch_dicts(conn.execute(statement))
    previous = {
        (table, str(row["user_id"]), _identifier(row["id"])): row.get("domain")
        for table, rows in tables.items()
        for row in rows
    }
    for table, user, row_id, domain in plan_relabels(tables):
        conn.execute(_SQLITE_UPDATES[table], (domain, user, row_id))
        event = relabel_event(table, user, row_id, previous[table, user, row_id], domain)
        conn.execute(
            """INSERT INTO event_log (id, user_id, event_type, actor_type, target_type, target_id,
               occurred_at, payload_json, integrity_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["id"],
                user,
                event["event_type"],
                event["actor_type"],
                event["target_type"],
                row_id,
                event["occurred_at"],
                json.dumps(event["payload_json"]),
                event["integrity_hash"],
            ),
        )
    conn.execute("INSERT OR REPLACE INTO alice_schema_state (key, value) VALUES (?, ?)", (state_key, "1"))
