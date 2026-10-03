"""A saved quote follows its source on a real migrated Postgres store.

``tests/unit/test_saved_quotes_follow_the_source_fence.py`` runs the lifecycle over SQLite, with real agent keys and
the shipped handlers. The reader, the pack and the verbs also run on Postgres, where the rows come back with ``UUID``
objects for ids, ``jsonb`` columns arrive decoded, the link and source reads are the Postgres store's own SQL
(``list_provenance_links_for_targets``, ``get_sources_by_ids``) and a soft delete is ``sources.deleted_at``. This test
saves a quote on a memory (a link and the metadata copy), then raises the source's sensitivity and later archives it,
and reads the memory through the reader and through the context pack for a ``trusted_local_agent`` identity bound to the
project, for an ``admin_agent`` identity and for the owner.

The second test runs two more lifecycles through the same readers. A memory with no provenance link (a commit held for
review or confirmed inline, then approved, keeps its quote and its source id only in its own metadata) is read through a
pack with a project scope, whose scope pass removes the refs of the rows before they are judged unless the reader is
asked first. And a memory with two links that carry the same quote (the commit route saves one excerpt as the quote of
every link) loses the quote on the link to the readable source when the other source is made confidential.

The third test is the case an outside review found. A commit with ``{"source_ids": [A, B]}`` links only A, with the
excerpt as the link's quote, and keeps the same excerpt as its own copy, which names B. When B is made confidential or
archived the copy is withheld and the quote on the link to A must go with it, whether the reader is asked for the links
of a memory it was given a row for (``memory`` first, as the review does) or not (``links`` alone, which reads the row
with ``get_memory``, a store read the reader did not make before). The refs are read in the shapes a writer stores:
``source_ids`` and ``selected_source_ids``.
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


def _make_source(store: PostgresVNextStore, title: str) -> str:
    source = store.create_source(
        {
            "source_type": "document",
            "title": title,
            "content_hash": f"sha256:{title.replace(' ', '-')}",
            "captured_at": "2026-01-05T00:00:00Z",
            "domain": "project",
            "sensitivity": "internal",
            "metadata_json": {"project_scope": ["alpha"]},
        }
    )
    return str(source["id"])


def _pack_for(store: PostgresVNextStore, fence: SourceReadFence) -> dict[str, object]:
    return VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(
            query="cone ten firing schedule posted",
            projects=("alpha",),
            include_sources=False,
            include_contradictions=False,
        ),
        source_fence=fence,
    )


def run_linkless_and_sibling_lifecycle(store: PostgresVNextStore) -> None:
    """A memory with no link and a memory with two links of the same quote, read before and after a source changes."""

    refused = _make_source(store, "Alpha held log")
    kept = _make_source(store, "Alpha second log")
    linkless = store.create_memory(
        {
            "memory_key": "project.held-cone-ten-firing",
            "memory_type": "project_fact",
            "title": "Held cone ten firing",
            "canonical_text": "The held cone ten firing schedule is posted by the kiln door.",
            "status": "active",
            "domain": "project",
            "sensitivity": "internal",
            "project_id": "alpha",
            "value": {"text": "held cone ten firing schedule", "source_refs": [refused]},
            "metadata_json": {
                "project_scope": ["alpha"],
                "agentic_memory": {"source_refs": [refused], "conversation_excerpt": _QUOTE},
            },
        }
    )
    linkless_id = str(linkless["id"])
    assert store.list_provenance_links(target_type="memory", target_id=linkless_id) == [], "the memory has no link"
    twin = store.create_memory(
        {
            "memory_key": "project.twin-cone-ten-firing",
            "memory_type": "project_fact",
            "title": "Twin cone ten firing",
            "canonical_text": "The twin cone ten firing schedule is posted by the kiln window.",
            "status": "active",
            "domain": "project",
            "sensitivity": "internal",
            "project_id": "alpha",
            "value": {"text": "twin cone ten firing schedule", "source_refs": [refused, kept]},
            "metadata_json": {
                "project_scope": ["alpha"],
                "agentic_memory": {"source_refs": [refused, kept], "conversation_excerpt": _QUOTE},
            },
        }
    )
    twin_id = str(twin["id"])
    for source_id in (refused, kept):
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": twin_id,
                "source_id": source_id,
                "quote": _QUOTE,
                "evidence_role": "supports",
            }
        )

    trusted = SourceReadFence.for_identity(_identity("trusted_local_agent"))
    admin = SourceReadFence.for_identity(_identity("admin_agent"))
    owner = SourceReadFence.unfenced()

    def seen(fence: SourceReadFence) -> dict[str, object]:
        pack = _pack_for(store, fence)
        rows = {str(row["id"]): row for row in pack["relevant_memories"]}  # type: ignore[attr-defined]
        assert {linkless_id, twin_id} <= set(rows), "both memories stay in the pack"
        reader = SavedProvenanceReader(store, fence=fence)
        return {
            "linkless_rows": rows[linkless_id],
            "linkless_memory": reader.memory(store.get_memory(linkless_id)),  # type: ignore[arg-type]
            "twin_links": reader.links(twin_id),
            "twin_evidence": [row for row in pack["supporting_evidence"] if str(row["target_id"]) == twin_id],  # type: ignore[attr-defined]
        }

    # The control: before the change every reader is shown the quote everywhere, and both links of the twin.
    for fence in (trusted, admin, owner):
        shown = seen(fence)
        assert all(_holds_quote(shown[name]) for name in shown), fence
        assert len(shown["twin_links"]) == 2 and len(shown["twin_evidence"]) == 2  # type: ignore[arg-type]

    # The first source is made confidential. The trusted key may not read it any more: the memory with no link is shown
    # without its quote in the pack rows (the pack's scope pass has removed the refs, so only the copy of the rows made
    # before it can refuse) and in the reader, and the twin keeps the link to the second source with no quote.
    store.update_source(source_id=refused, patch={"sensitivity": "confidential"}, actor_type="user")
    shown = seen(trusted)
    assert not _holds_quote(shown["linkless_rows"]) and not _holds_quote(shown["linkless_memory"]), shown
    assert refused not in json.dumps(shown, default=str)
    assert [str(link["source_id"]) for link in shown["twin_links"]] == [kept]  # type: ignore[attr-defined]
    assert [str(row["source_id"]) for row in shown["twin_evidence"]] == [kept]  # type: ignore[attr-defined]
    assert all(row["quote"] is None for row in (*shown["twin_links"], *shown["twin_evidence"]))  # type: ignore[attr-defined]
    for fence in (admin, owner):
        kept_all = seen(fence)
        assert all(_holds_quote(kept_all[name]) for name in kept_all), fence
        assert len(kept_all["twin_links"]) == 2 and len(kept_all["twin_evidence"]) == 2  # type: ignore[arg-type]

    # The source is archived: no key reads it, the owner still does.
    store.delete_source(source_id=refused, actor_type="user")
    for fence in (trusted, admin):
        gone = seen(fence)
        assert not _holds_quote(gone["linkless_rows"]) and not _holds_quote(gone["linkless_memory"]), fence
        assert all(row["quote"] is None for row in (*gone["twin_links"], *gone["twin_evidence"])), fence  # type: ignore[attr-defined]
    owner_sees = seen(owner)
    assert _holds_quote(owner_sees["linkless_rows"]) and _holds_quote(owner_sees["linkless_memory"])


def test_a_memory_with_no_link_and_a_sibling_quote_are_withheld_on_postgres(migrated_database_urls) -> None:
    """Mutations: move the ``saved_provenance.memories(ranked_memories)`` statement of ``compile_context_pack`` below the
    ``_sanitize_memory_scope_references`` call (the ``linkless_rows`` assertion fails for the trusted key), or return
    ``link`` from ``SavedProvenanceReader._shown`` whenever it is admitted (the ``twin`` assertions fail)."""

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "saved-quote-linkless@example.invalid", "Saved quote linkless")
        run_linkless_and_sibling_lifecycle(PostgresVNextStore(conn))


def run_nested_reference_lifecycle(store: PostgresVNextStore) -> None:
    """Two memories that cite a readable source and one that is reclassified, in one ref, read before and after."""

    readable = _make_source(store, "Alpha second log")
    refused = _make_source(store, "Alpha nested log")
    shapes = {
        "nested": [{"source_ids": [readable, refused]}],
        "selected": [{"source_id": readable, "selected_source_ids": [refused]}],
    }
    memory_ids: dict[str, str] = {}
    for name, refs in shapes.items():
        memory = store.create_memory(
            {
                "memory_key": f"project.{name}-cone-ten-firing",
                "memory_type": "project_fact",
                "title": f"{name} cone ten firing",
                "canonical_text": f"The {name} cone ten firing schedule is posted on the wall calendar.",
                "status": "active",
                "domain": "project",
                "sensitivity": "internal",
                "project_id": "alpha",
                "value": {"text": f"{name} cone ten firing schedule", "source_refs": refs},
                "metadata_json": {
                    "project_scope": ["alpha"],
                    "agentic_memory": {"source_refs": refs, "conversation_excerpt": _QUOTE},
                },
            }
        )
        memory_ids[name] = str(memory["id"])
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory_ids[name],
                "source_id": readable,
                "quote": _QUOTE,
                "evidence_role": "supports",
            }
        )

    trusted = SourceReadFence.for_identity(_identity("trusted_local_agent"))
    admin = SourceReadFence.for_identity(_identity("admin_agent"))
    owner = SourceReadFence.unfenced()

    def seen(fence: SourceReadFence) -> dict[str, dict[str, object]]:
        pack = _pack_for(store, fence)
        out: dict[str, dict[str, object]] = {}
        for name, memory_id in memory_ids.items():
            row = store.get_memory(memory_id)
            assert row is not None
            asked_for_links_alone = SavedProvenanceReader(store, fence=fence).links(memory_id)
            reader = SavedProvenanceReader(store, fence=fence)
            memory = reader.memory(row)
            out[name] = {
                "links_alone": asked_for_links_alone,
                "links_after_the_row": reader.links(memory_id),
                "memory": memory,
                "pack_rows": [item for item in pack["relevant_memories"] if str(item["id"]) == memory_id],  # type: ignore[attr-defined]
                "evidence": [item for item in pack["supporting_evidence"] if str(item["target_id"]) == memory_id],  # type: ignore[attr-defined]
            }
            assert out[name]["pack_rows"], (name, "the memory stays in the pack")
        return out

    # The control: every reader is shown the quote in every place, and the link to the readable source.
    for fence in (trusted, admin, owner):
        for name, shown in seen(fence).items():
            assert all(_holds_quote(shown[part]) for part in shown), (name, fence)

    for label, change in (
        ("confidential", lambda: store.update_source(source_id=refused, patch={"sensitivity": "confidential"}, actor_type="user")),
        ("archived", lambda: store.delete_source(source_id=refused, actor_type="user")),
    ):
        change()
        for name, shown in seen(trusted).items():
            assert not _holds_quote(shown), (label, name)
            for part in ("links_alone", "links_after_the_row", "evidence"):
                assert [str(item["source_id"]) for item in shown[part]] == [readable], (label, name, part)  # type: ignore[attr-defined,union-attr]
                assert all(item["quote"] is None for item in shown[part]), (label, name, part)  # type: ignore[attr-defined,union-attr]
            assert refused not in json.dumps(shown, default=str), (label, name)
        for name, shown in seen(owner).items():
            assert all(_holds_quote(shown[part]) for part in shown), (label, name, "the owner is shown what was stored")
        if label == "confidential":
            for name, shown in seen(admin).items():
                assert all(_holds_quote(shown[part]) for part in shown), (label, name, "the admin key may read it")
        else:
            for name, shown in seen(admin).items():
                assert not _holds_quote(shown), (label, name, "nobody reads an archived source")


def test_a_nested_reference_withholds_the_quote_on_the_link_to_the_readable_source_on_postgres(
    migrated_database_urls,
) -> None:
    """Mutations: drop ``texts |= _memory_copy_quote_texts(row)`` in ``SavedProvenanceReader._verdict`` (every
    ``links`` assertion fails: the rule of the first version); make ``_row_for`` return None for a row the reader was not
    given (the ``links_alone`` assertions fail while ``links_after_the_row`` passes); drop ``selected_source_ids`` from
    ``SOURCE_REFERENCE_KEYS`` (the ``selected`` memory keeps its quote)."""

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "saved-quote-nested@example.invalid", "Saved quote nested")
        run_nested_reference_lifecycle(PostgresVNextStore(conn))
