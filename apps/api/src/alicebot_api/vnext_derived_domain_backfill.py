"""Data-only repair of derived labels from recorded, same-user inputs.

No content, timestamps, provenance or project labels are rewritten. Missing
inputs are not inferred from text. Already restricted rows remain restricted.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_domain import derived_domain

INPUT_TABLES = ('sources', 'memories', 'open_loops', 'generated_artifacts')
_ID_KEYS = {
    'source_ids': 'sources', 'memory_ids': 'memories', 'open_loop_ids': 'open_loops',
    'artifact_ids': 'generated_artifacts', 'member_ids': 'memories',
    'cluster_member_ids': 'memories', 'cluster_membership': 'memories',
    'stale_marked_memory_ids': 'memories', 'belief_ids': 'beliefs',
}
_REF_TYPES = {'source': 'sources', 'memory': 'memories', 'open_loop': 'open_loops', 'artifact': 'generated_artifacts'}


def _object(value: object) -> Mapping[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, Mapping) else {}


def _strings(value: object):
    if isinstance(value, str):
        yield value
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
            elif key == 'source_refs':
                for ref in _strings(nested):
                    kind, separator, row_id = ref.partition(':')
                    if separator and kind in _REF_TYPES and row_id:
                        found.add((_REF_TYPES[kind], row_id))
            elif isinstance(nested, (Mapping, list)):
                found.update(recorded_inputs(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(recorded_inputs(nested))
    return found


def plan_relabels(tables: Mapping[str, Sequence[Mapping[str, object]]]) -> list[tuple[str, str, str, str]]:
    """Return (table, user_id, id, domain) updates in dependency order.

    Propagation is monotone: each row moves from unrestricted to restricted at
    most once, so cycles terminate without weakening a label or oscillating.
    """
    rows = {(table, str(row['user_id']), str(row['id'])): row for table, values in tables.items() for row in values}
    labels = {key: row.get('domain', 'unknown') for key, row in rows.items()}
    inputs: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for key, row in rows.items():
        table, user, _ = key
        metadata = _object(row.get('metadata_json'))
        if metadata.get('redacted') is True:
            continue
        if table == 'memories' and not (
            isinstance(metadata.get('consolidation'), Mapping)
            or metadata.get('discovered_by') == 'vnext_weekly_synthesis'
            or metadata.get('workflow') == 'project_auto_update'
            or metadata.get('candidate_kind') in ('memory_consolidation', 'memory_rollup')
        ):
            continue
        if table not in ('memories', 'generated_artifacts'):
            continue
        refs = recorded_inputs(metadata) | recorded_inputs(_object(row.get('value')))
        inputs[key] = {(kind, user, row_id) for kind, row_id in refs}
        # Old weekly candidates have no own input list. The parent report
        # records both its inputs and its outputs, providing the missing link.
        if table == 'generated_artifacts' and metadata.get('input_summary'):
            for candidate in _strings(metadata.get('candidate_memory_ids')):
                candidate_key = ('memories', user, candidate)
                candidate_row = rows.get(candidate_key)
                if candidate_row and _object(candidate_row.get('metadata_json')).get('discovered_by') == 'vnext_weekly_synthesis':
                    inputs.setdefault(candidate_key, set()).update((kind, user, row_id) for kind, row_id in refs)
    # Beliefs have no domain column. Resolve each alias to the backing memory
    # before propagation so a newly repaired memory also repairs its reports.
    for key, linked_refs in inputs.items():
        inputs[key] = {
            ('memories', ref[1], str(rows[ref].get('memory_id'))) if ref[0] == 'beliefs' and ref in rows else ref
            for ref in linked_refs
        }
    updates = []
    while True:
        changed = False
        for key in sorted(inputs):
            if labels.get(key) in RESTRICTED_DOMAINS:
                continue
            domain = derived_domain(({'domain': labels[ref]} for ref in sorted(inputs[key]) if ref in labels), fallback='unknown')
            if domain in RESTRICTED_DOMAINS:
                labels[key] = domain
                updates.append((*key, domain))
                changed = True
        if not changed:
            return updates


def relabel_sqlite(conn) -> None:
    """One upgrade step, versioned by the existing SQLite schema-state table."""
    state_key = 'derived_restricted_domains_v1'
    if conn.execute('SELECT value FROM alice_schema_state WHERE key = ?', (state_key,)).fetchone():
        return
    tables = {}
    available = {row[0] if not isinstance(row, dict) else row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in INPUT_TABLES:
        if table not in available:
            continue
        columns = 'id, user_id, domain'
        if table in ('memories', 'generated_artifacts'):
            columns += ', metadata_json'
        if table == 'memories':
            columns += ', value'
        cursor = conn.execute(f'SELECT {columns} FROM {table}')
        names = [column[0] for column in cursor.description]
        tables[table] = [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
    for table, user, row_id, domain in plan_relabels(tables):
        conn.execute(f'UPDATE {table} SET domain = ? WHERE user_id = ? AND id = ?', (domain, user, row_id))
    conn.execute('INSERT INTO alice_schema_state (key, value) VALUES (?, ?)', (state_key, '1'))
