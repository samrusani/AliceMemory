"""Regenerate fresh source copies without changing earlier rows or their provenance."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from alicebot_api.vnext_capture import extract_candidate_memories
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_project_scope import source_project_scope
from alicebot_api.vnext_projects import _open_loop_candidates
from alicebot_api.vnext_repositories import JsonObject


def regenerate_source_inputs(store: Any, source: JsonObject) -> JsonObject:
    """Create candidate memories and loops from all stored chunks at the current label."""

    chunks = store.read_source_chunks_for_regeneration(str(source["id"]))
    scope = source_project_scope(source)
    project_id = None
    if len(scope) == 1:
        try:
            project_id = str(UUID(scope[0]))
        except ValueError:
            pass
    generation = str(uuid4())
    memories = []
    loops = []
    for index, candidate in enumerate(extract_candidate_memories(chunks)):
        metadata = with_derived_from({
            "source_id": str(source["id"]),
            "source_chunk_id": candidate.source_chunk_id,
            "source_chunk_index": candidate.source_chunk_index,
            "extraction_rule": candidate.extraction_rule,
            "project_scope": list(scope),
            "regeneration_id": generation,
            **({"provenance_role": candidate.provenance_role, "assertion_class": candidate.assertion_class} if candidate.provenance_role is not None else {}),
        }, {"sources": [source]})
        memory = store.create_memory({
            "memory_key": f"regenerated:{generation}:{index}",
            "canonical_text": candidate.text,
            "title": candidate.text[:120],
            "summary": candidate.text[:280],
            "value": {"text": candidate.text, "source_id": str(source["id"]), "source_chunk_id": candidate.source_chunk_id},
            "source_event_ids": [str(source["id"]), candidate.source_chunk_id],
            "status": "candidate", "memory_type": candidate.memory_type,
            "confidence": candidate.confidence,
            "domain": source["domain"], "sensitivity": source["sensitivity"],
            "project_id": project_id, "metadata_json": metadata,
        }, actor_type="user")
        store.create_provenance_link({
            "target_type": "memory", "target_id": str(memory["id"]),
            "source_id": str(source["id"]), "source_chunk_id": candidate.source_chunk_id,
            "quote": candidate.text, "evidence_role": "quoted_from", "confidence": candidate.confidence,
        }, actor_type="user")
        memories.append(str(memory["id"]))
    source_metadata = source.get("metadata_json")
    source_with_text = {**source, "metadata_json": {**(source_metadata if isinstance(source_metadata, dict) else {}), "raw_text": "\n".join(str(chunk["text"]) for chunk in chunks)}}
    for loop_candidate in _open_loop_candidates(source_with_text):
        loop_candidate["project_id"] = project_id
        loop_metadata = loop_candidate.get("metadata_json")
        loop_candidate["metadata_json"] = with_derived_from({
            **(loop_metadata if isinstance(loop_metadata, dict) else {}),
            "source_id": str(source["id"]), "project_scope": list(scope), "regeneration_id": generation,
        }, {"sources": [source]})
        loop = store.create_open_loop(loop_candidate, actor_type="user")
        store.create_provenance_link({
            "target_type": "open_loop", "target_id": str(loop["id"]),
            "source_id": str(source["id"]), "evidence_role": "quoted_from",
        }, actor_type="user")
        loops.append(str(loop["id"]))
    append_event(store, event_type="source.inputs_regenerated", actor_type="user", target_type="source", target_id=str(source["id"]), payload={"memory_count": len(memories), "open_loop_count": len(loops)})
    return {"source_id": str(source["id"]), "memory_ids": memories, "open_loop_ids": loops, "memory_count": len(memories), "open_loop_count": len(loops)}
