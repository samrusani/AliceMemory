"""SQLite source retirement and the derived state owned by a source."""
from __future__ import annotations

import json
from pathlib import Path
from contextlib import nullcontext

from alicebot_api.vault_file_lock import vault_file_lock
from alicebot_api.vnext_stores.memory_lifecycle_common import is_redacted_memory

from alicebot_api.source_supersede import classification_refusal, eligible_source
from alicebot_api.vnext_entities import ENTITY_MENTION_EDGE_TYPE
from alicebot_api.vnext_project_scope import source_project_scope
from alicebot_api.vnext_stores.sqlite.primitives import _utc_now_iso

REMOVAL_MARKER = "[removed by the owner]"


def markdown_sources_by_path(self):
    rows = self._fetch_all(
        "SELECT * FROM sources WHERE user_id = ? AND source_type = 'markdown' "
        "AND connector_name = 'markdown_folder' AND deleted_at IS NULL "
        "ORDER BY captured_at, id", (self.user_id,))
    matches = {}
    for row in rows:
        if eligible_source(row):
            matches.setdefault((row["connector_name"], row["raw_path"]), []).append(row)
    return matches


def prune_sleep_rows(self, source_ids, *, dry_run=False):
    from alicebot_api.vault_sleep import load_sleep_proposals, sleep_proposals_path, _write_jsonl
    database = next((row["file"] for row in self._fetch_all("PRAGMA database_list") if row["name"] == "main"), "")
    if not database:
        return 0
    sidecar = sleep_proposals_path(Path(database))
    # Writers take the SQLite writer lock before the sidecar lock. Sleep's
    # publisher takes the same locks and rechecks liveness after this commits.
    with nullcontext() if dry_run else vault_file_lock(sidecar):
        rows = load_sleep_proposals(sidecar)
        kept = [row for row in rows if not (
            str(row.get("user_id")) == self.user_id and str(row.get("source_id")) in source_ids)]
        removed = len(rows) - len(kept)
        if removed and not dry_run:
            _write_jsonl(sidecar, kept)
        return removed



def citing_memories(self, source_id):
    # No bounded list: a scrub must cover every candidate, including old rows
    # whose only reference is in value or metadata rather than a provenance link.
    return self._fetch_all(
        """SELECT m.* FROM memories m WHERE m.user_id = ? AND (
        EXISTS (SELECT 1 FROM provenance_links p WHERE p.user_id = m.user_id
                AND p.target_type = 'memory' AND p.target_id = m.id
                AND (p.source_id = ? OR EXISTS (
                    SELECT 1 FROM source_chunks c WHERE c.user_id = p.user_id
                    AND c.id = p.source_chunk_id AND c.source_id = ?)))
        OR EXISTS (SELECT 1 FROM json_tree(m.metadata_json) j WHERE j.type = 'text'
                   AND j.value IN (?, ?))
        OR EXISTS (SELECT 1 FROM json_tree(m.value) j WHERE j.type = 'text' AND j.value IN (?, ?))
        OR EXISTS (SELECT 1 FROM json_each(m.source_event_ids) j WHERE j.value = ?)
        ) ORDER BY m.id""",
        (self.user_id, source_id, source_id, source_id, 'source:' + source_id,
         source_id, 'source:' + source_id, source_id))


def close_mention_edges(self, from_type, from_id, now):
    edges = self._fetch_all(
        "SELECT * FROM graph_edges WHERE user_id = ? AND from_type = ? AND from_id = ? "
        "AND to_type = 'entity' AND edge_type = ?",
        (self.user_id, from_type, from_id, ENTITY_MENTION_EDGE_TYPE))
    for edge in edges:
        # Clear both surface and normalized aliases, even on an already closed edge.
        self._execute(
            "UPDATE graph_edges SET valid_to = COALESCE(valid_to, MAX(?, COALESCE(valid_from, ?))), "
            "explanation = '', metadata_json = '{}' WHERE id = ? AND user_id = ?",
            (now, now, edge['id'], self.user_id))
        if edge.get('valid_to') is None:
            self._execute(
                "UPDATE vnext_entities SET mention_count = MAX(0, mention_count - 1), updated_at = ? "
                "WHERE id = ? AND user_id = ?", (now, edge['to_id'], self.user_id))
        self._execute(
            """UPDATE vnext_entities SET name = ?, normalized_name = 'removed:' || id,
            aliases = '[]', metadata_json = '{}', deleted_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            AND json_extract(metadata_json, '$.created_by') = 'vnext_entity_linker'
            AND NOT EXISTS (SELECT 1 FROM graph_edges e WHERE e.user_id = vnext_entities.user_id
                AND e.valid_to IS NULL AND ((e.to_type = 'entity' AND e.to_id = vnext_entities.id)
                OR (e.from_type = 'entity' AND e.from_id = vnext_entities.id)))""",
            (REMOVAL_MARKER, now, now, edge['to_id'], self.user_id))
    return len(edges)


def retire_dependents(self, source_id, *, now):
    memories = [row for row in citing_memories(self, source_id) if not is_redacted_memory(row)]
    pending = [row for row in memories if row['status'] in {'candidate', 'needs_review'}]
    retained = [str(row['id']) for row in memories]
    for memory in pending:
        mid = str(memory['id'])
        self.update_memory(memory_id=mid, patch={'status': 'rejected'}, actor_type='user')
        close_mention_edges(self, 'memory', mid, now)
    edges = close_mention_edges(self, 'source', source_id, now)
    loops = self._execute(
        """UPDATE open_loops SET title = ?, description = ?, status = 'dismissed',
        resolved_at = ?, closed_at = ?, resolution_note = ?, metadata_json = '{}', updated_at = ?
        WHERE source_id = ? AND user_id = ?""",
        (REMOVAL_MARKER, REMOVAL_MARKER, now, now, REMOVAL_MARKER, now, source_id, self.user_id)).rowcount
    return {'candidate_memories': len(pending), 'mention_edges': edges, 'open_loops': loops,
            'memories_citing_replaced': retained}


def supersede_source(self, source_id, *, superseded_by, allow_looser_classification=False, dry_run=False):
    with self.savepoint():
        old = self.get_source(source_id)
        new = self.get_source(superseded_by)
        if old is None or new is None or source_id == superseded_by:
            raise ValueError('Source replacement requires two live sources')
        if not eligible_source(old) or not eligible_source(new) or (
                old['raw_path'], old['connector_name']) != (new['raw_path'], new['connector_name']):
            raise ValueError('Source replacement requires the same Markdown path')
        reason = classification_refusal([old], domain=new['domain'], sensitivity=new['sensitivity'],
                                        project_scope=source_project_scope(new),
                                        allow_looser=allow_looser_classification)
        if reason:
            raise ValueError(reason)
        now = _utc_now_iso()
        # The sidecar goes first. A later rollback may lose proposals, which
        # can be generated again, but cannot leave retired evidence in it.
        prune_sleep_rows(self, {source_id}, dry_run=dry_run)
        counts = retire_dependents(self, source_id, now=now)
        metadata = {**old['metadata_json'], 'superseded_by': superseded_by,
                    'superseded_at': now, 'supersede_reason': 'markdown_reimport'}
        self._execute("UPDATE sources SET deleted_at = ?, metadata_json = ? WHERE id = ? AND user_id = ?",
                      (now, json.dumps(metadata), source_id, self.user_id))
        self._append_mutation_event(event_type='source.superseded', target_type='source', target_id=source_id,
                                    actor_type='user', payload={'superseded_by': superseded_by, **counts})
        return counts
