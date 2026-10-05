"""Data-only repair of derived labels from recorded, same-user inputs.

No content, timestamps, provenance or project labels are rewritten. Missing
inputs are not inferred from text. Already restricted rows remain restricted.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from uuid import UUID

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_event_log import build_event_log_record

REPAIR_STATE_KEY = "derived_restricted_domains_v2"
_UNSETTLED_ROWS_SHOWN = 5

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


class DerivedDomainRepairError(ValueError):
    """The repair has no labels it can publish: derived rows keep relabeling each other, or an update changed no row.

    Still a ``ValueError``: the upgrade and the restore abort on it as before.
    """


def _unsettled_message(rows: Sequence[tuple[str, str, str]]) -> str:
    shown = ", ".join(f"{table} {row_id}" for table, _user, row_id in rows[:_UNSETTLED_ROWS_SHOWN])
    if len(rows) > _UNSETTLED_ROWS_SHOWN:
        shown += f" and {len(rows) - _UNSETTLED_ROWS_SHOWN} more"
    return (
        "derived domain repair did not settle: derived rows record each other as inputs in a cycle, "
        f"so their restricted labels kept changing (rows: {shown}). "
        "The repair stopped before it changed any row. Remove the circular input references from those rows, "
        "or restore a backup made before they were added, then run the upgrade or open the database again."
    )


def require_changed(changed: int, table: str, row_id: str) -> None:
    """Refuse a relabel whose update changed no row, so no event or completion stamp describes a change that did not happen.

    ``changed`` is the row count of the ``UPDATE``. Both stores call this after each update, before they record anything.
    """
    if changed == 0:
        raise DerivedDomainRepairError(
            f"derived domain repair changed no row for {table} {row_id}: the update found no row stored under that id. "
            "The repair stopped before it recorded any change or marked the database as repaired, "
            "and the upgrade or restore that ran it did not publish anything."
        )


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


def _input_groups(inputs: Mapping[tuple[str, str, str], set[tuple[str, str, str]]]) -> list[list[tuple[str, str, str]]]:
    """The derived rows in groups that record one another as inputs, each group after every group it reads.

    Tarjan's strongly connected components, without recursion so a long chain
    cannot exhaust the stack. A row that is in no cycle is a group of its own.
    Rows and their inputs are visited in sorted order, so the result is the same
    on every run.
    """
    index: dict[tuple[str, str, str], int] = {}
    lowest: dict[tuple[str, str, str], int] = {}
    stack: list[tuple[str, str, str]] = []
    on_stack: set[tuple[str, str, str]] = set()
    groups: list[list[tuple[str, str, str]]] = []
    for root in sorted(inputs):
        if root in index:
            continue
        index[root] = lowest[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(ref for ref in inputs[root] if ref in inputs)))]
        while work:
            key, refs = work[-1]
            for ref in refs:
                if ref not in index:
                    index[ref] = lowest[ref] = len(index)
                    stack.append(ref)
                    on_stack.add(ref)
                    work.append((ref, iter(sorted(nested for nested in inputs[ref] if nested in inputs))))
                    break
                if ref in on_stack:
                    lowest[key] = min(lowest[key], index[ref])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowest[parent] = min(lowest[parent], lowest[key])
                if lowest[key] == index[key]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.remove(member)
                        group.append(member)
                        if member == key:
                            break
                    groups.append(sorted(group))
    return groups


def plan_relabels(tables: Mapping[str, Sequence[Mapping[str, object]]]) -> list[tuple[str, str, str, str]]:
    """Return (table, user_id, id, domain) updates for the settled labels.

    Rows are labelled in dependency order: each group of rows that record one
    another as inputs (a cycle, or one row on its own) is settled after every
    row it reads, so a row outside a cycle is labelled once, from final input
    labels, however long the chain. Inside a cycle the rows are revisited until
    their labels settle. Refuse a cycle that does not settle within a bounded
    number of changes; callers must roll back rather than publish intermediate
    labels.
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
    # A group is settled once every group it reads is final. A row in no cycle is one group and is read once.
    for component in _input_groups(inputs):
        members = set(component)
        pending = deque(component)
        queued = set(component)
        remaining_changes = len(component) * (len(RESTRICTED_DOMAINS) + 1)
        changes: Counter[tuple[str, str, str]] = Counter()
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
            changes[key] += 1
            if remaining_changes < 0:
                raise DerivedDomainRepairError(_unsettled_message(sorted(row for row, count in changes.items() if count > 1)))
            labels[key] = domain
            for dependant in sorted(dependants.get(key, set()) & members):
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
    """Upgrade once, or repair a complete staged restore before publication.

    The plan matches rows and recorded inputs by a normalised id, so it names a row as ``_identifier`` writes it. SQLite
    keeps an id as the text it was given (capitals, no hyphens, braces and ``urn:uuid:`` all stand), so each planned row
    is updated and recorded under the id it is stored with, and two stored spellings of one id both take the planned
    label. An update that changes no row stops the repair with ``DerivedDomainRepairError`` before any event or the
    completion stamp is written; the caller's transaction is then rolled back by the upgrade or the restore it aborts.
    """
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
    previous = {}
    stored_ids: dict[tuple[str, str, str], list[str]] = {}
    for table, rows in tables.items():
        for row in rows:
            user, stored = str(row["user_id"]), str(row["id"])
            previous[table, user, stored] = row.get("domain")
            stored_ids.setdefault((table, user, _identifier(stored)), []).append(stored)
    changes = [
        (table, user, stored, domain)
        for table, user, row_id, domain in plan_relabels(tables)
        for stored in stored_ids[table, user, row_id]
        if previous[table, user, stored] != domain
    ]
    # Every update is made and checked before the first event is written.
    for table, user, stored, domain in changes:
        require_changed(conn.execute(_SQLITE_UPDATES[table], (domain, user, stored)).rowcount, table, stored)
    for table, user, stored, domain in changes:
        event = relabel_event(table, user, stored, previous[table, user, stored], domain)
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
    conn.execute("INSERT OR REPLACE INTO alice_schema_state (key, value) VALUES (?, ?)", (state_key, "1"))
