"""A saved quote follows its source on a real migrated Postgres store.

``tests/unit/test_saved_quotes_follow_the_source_fence.py`` runs the lifecycle over SQLite, with real agent keys and
the shipped handlers. The reader, the pack and the verbs also run on Postgres, where the rows come back with ``UUID``
objects for ids, ``jsonb`` columns arrive decoded, the link and source reads are the Postgres store's own SQL
(``list_provenance_links_for_targets``, ``get_sources_by_ids``) and a soft delete is ``sources.deleted_at``. This test
saves a quote on a memory (a link and the metadata copy), then raises the source's sensitivity and later archives it,
and reads the memory through the reader and through the context pack for a ``trusted_local_agent`` identity bound to the
project, for an ``admin_agent`` identity and for the owner.
"""

from __future__ import annotations

import json
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from alicebot_api.vnext_store import PostgresVNextStore

_QUOTE = "zinnwald-quote-8841 cone ten firing kiln log marlin-oxide-5520"


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"{profile}-alpha",
        permission_profile=profile,
        project_scope=("alpha",),
        auth="agent_api_key",
        project_scope_locked=True,
    )


def _holds_quote(value: object) -> bool:
    text = json.dumps(value, default=str)
    return "zinnwald-quote-8841" in text or "marlin-oxide-5520" in text


def _read(store: PostgresVNextStore, memory_id: str, fence: SourceReadFence) -> dict[str, object]:
    """What one reader is shown of the memory: the review's reader and the full rows and evidence of the pack."""

    reader = SavedProvenanceReader(store, fence=fence)
    memory = store.get_memory(memory_id)
    assert memory is not None
    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(
            query="cone ten firing schedule wall calendar",
            projects=("alpha",),
            include_sources=False,
            include_contradictions=False,
        ),
        source_fence=fence,
    )
    assert memory_id in [str(row["id"]) for row in pack["relevant_memories"]], "the memory stays in the pack"
    return {
        "links": reader.links(memory_id),
        "memory": reader.memory(memory),
        "supporting_evidence": pack["supporting_evidence"],
        "pack_rows": pack["relevant_memories"],
    }


def run_lifecycle(store: PostgresVNextStore) -> None:
    """The lifecycle over one store: save a quote, make the source confidential, archive it, read it three ways."""

    source = store.create_source(
        {
            "source_type": "document",
            "title": "Alpha pottery log",
            "content_hash": "sha256:saved-quote",
            "captured_at": "2026-01-05T00:00:00Z",
            "domain": "project",
            "sensitivity": "internal",
            "metadata_json": {"project_scope": ["alpha"]},
        }
    )
    source_id = str(source["id"])
    memory = store.create_memory(
        {
            "memory_key": "project.cone-ten-firing",
            "memory_type": "project_fact",
            "title": "Cone ten firing",
            "canonical_text": "The cone ten firing schedule is posted on the wall calendar.",
            "status": "active",
            "domain": "project",
            "sensitivity": "internal",
            "project_id": "alpha",
            "value": {"text": "cone ten firing schedule", "source_refs": [source_id]},
            "metadata_json": {
                "project_scope": ["alpha"],
                "provenance": {"source_id": source_id, "quote": _QUOTE},
                "agentic_memory": {"source_refs": [source_id], "conversation_excerpt": _QUOTE},
            },
        }
    )
    memory_id = str(memory["id"])
    store.create_provenance_link(
        {
            "target_type": "memory",
            "target_id": memory_id,
            "source_id": source_id,
            "quote": _QUOTE,
            "evidence_role": "quoted_from",
        }
    )
    trusted = SourceReadFence.for_identity(_identity("trusted_local_agent"))
    admin = SourceReadFence.for_identity(_identity("admin_agent"))
    owner = SourceReadFence.unfenced()

    # The control: before the change of label every reader is shown the quote in every place.
    for fence in (trusted, admin, owner):
        shown = _read(store, memory_id, fence)
        assert all(_holds_quote(shown[name]) for name in shown), fence

    # The source is made confidential: the trusted key (default ceiling) loses it, the admin key and the owner keep it.
    store.update_source(source_id=source_id, patch={"sensitivity": "confidential"}, actor_type="user")
    shown = _read(store, memory_id, trusted)
    assert shown["links"] == []
    assert shown["supporting_evidence"] == []
    assert not _holds_quote(shown), shown
    assert source_id not in json.dumps(shown, default=str)
    for fence in (admin, owner):
        kept = _read(store, memory_id, fence)
        assert all(_holds_quote(kept[name]) for name in kept), fence

    # The source is archived (a soft delete): no key reads it. The owner still reads the link and the rows. The pack's
    # own scope test leaves the evidence of a source it cannot find out for a pack that has a scope, as it always did.
    store.delete_source(source_id=source_id, actor_type="user")
    for fence in (trusted, admin):
        gone = _read(store, memory_id, fence)
        assert gone["links"] == [] and gone["supporting_evidence"] == []
        assert not _holds_quote(gone), fence
    kept = _read(store, memory_id, owner)
    assert all(_holds_quote(kept[name]) for name in ("links", "memory", "pack_rows"))


def test_a_saved_quote_is_withheld_after_the_source_is_reclassified_or_archived_on_postgres(
    migrated_database_urls,
) -> None:
    """Mutations: make ``SavedProvenanceReader._judge`` mark every source admitted (the trusted reader keeps the quote
    after the source is made confidential), or remove the ``admits_link`` test in ``_supporting_evidence`` (the
    ``supporting_evidence`` assertions fail while the reader assertions pass)."""

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "saved-quote@example.invalid", "Saved quote")
        run_lifecycle(PostgresVNextStore(conn))
