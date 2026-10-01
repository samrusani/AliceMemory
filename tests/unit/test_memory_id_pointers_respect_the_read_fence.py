"""A memory id a read returns must pass the fence that read runs under (DB-012).

A pointer id is itself sensitive metadata. The correction label and the pack's
``supersedes`` and ``superseded_by`` fields already go through the fence. Three
more places named an id with no sensitivity or domain check, and each is pinned
here as a keyed reader sees it:

* ``validity.superseded_by_memory_id`` and ``validity.supersedes_memory_id`` on
  an ``alice_recall`` result;
* the ``target_id`` of each ``recent_changes`` entry on a context pack;
* the id and title of each revision in a ``context_depth: high`` pack's
  ``supersession_context``.

Ids copied into a stored memory's ``metadata_json`` are a documented gap and are
not asserted here.

Each fence input (sensitivity ceiling, domain, project, person, time window) is
pinned on its own for each place, and a vault the caller mostly cannot read
still gets a pack.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

import alicebot_api.vnext_retrieval as vnext_retrieval_module
from alicebot_api.vnext_retrieval import (
    MEMORY_ID_LOOKUP_BATCH_SIZE,
    VNextRetrievalCompletenessError,
    VNextRetrievalRequest,
    VNextRetrievalService,
    _drop_pointers_outside_fence,
    _fetch_filtered_prefix,
    _ResolvedRetrievalScope,
)
from tests.unit.test_correction_label_respects_the_read_fence import (
    NEW,
    OLD,
    _call,
    _captured,
    _context,
    _mint,
    _pack,
    _set,
    _store,
    _supersede,
)

CONFIDENTIAL_TOO = ("public", "internal", "private", "confidential", "unknown")
DEAD = "00000000-0000-4000-8000-00000000dead"
MARKER = "HIDDEN-REVISION-TITLE-MARKER"
THIRD = "The kettle is stored on the second shelf."
FOURTH = "The kettle is stored in the pantry cupboard."


def _hide_successor(context, monkeypatch) -> tuple[str, str]:
    """M still says ``active`` and points at R, which is above every reader's ceiling.

    The one-sided pointer is the shape that put R's id on v0.18.0 and v0.19.0.
    Returns (M, R).
    """
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, sensitivity="confidential")
    _set(context, memory_id, status="active")
    return memory_id, replacement


def _recall(context, *, key=None, **arguments) -> dict:
    return _call(context, "alice_recall", {"query": OLD, **arguments}, key=key)


def _result(payload: dict, memory_id: str) -> dict:
    rows = [row for row in payload["results"] if row.get("id") == memory_id]
    assert rows, "the row was not recalled; the assertions below would be vacuous"
    return rows[0]


def _change_targets(pack: dict) -> list[str]:
    return [str(change.get("target_id")) for change in pack.get("recent_changes") or []]


def _metadata(context, memory_id: str) -> dict:
    row = _store(context, lambda s: s.get_memory(memory_id))
    return dict(row.get("metadata_json") or {})


# -- (a) validity on an alice_recall result ------------------------------------------


def test_recall_validity_withholds_a_successor_above_the_ceiling(tmp_path: Path, monkeypatch) -> None:
    """The id goes and the ``superseded`` flag stays.

    The old row is still recalled, so the agent must not read it as current, but
    it is not told which memory replaced it. Mutation: make
    ``fence_validity_memory_ids`` return at the top.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    reader = _mint(context, profile="read_only_agent", project="acme")

    payload = _recall(context, key=reader)
    validity = _result(payload, memory_id)["validity"]
    assert validity["superseded"] is True
    assert "superseded_by_memory_id" not in validity
    assert replacement not in json.dumps(payload, default=str)

    allowed = _recall(context, sensitivity_allowed=list(CONFIDENTIAL_TOO))
    assert _result(allowed, memory_id)["validity"]["superseded_by_memory_id"] == replacement, (
        "the control: a caller whose ceiling reaches it is told"
    )


def test_recall_validity_withholds_a_successor_outside_the_requested_domains(tmp_path: Path, monkeypatch) -> None:
    """The domain half of the fence.

    Mutation: pass ``domains=[]`` to ``fence_validity_memory_ids`` in the recall
    handler, or leave the domain out of the predicate.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    _set(context, replacement, sensitivity="private", domain="professional")

    narrowed = _recall(context, domains=["project"])
    assert "superseded_by_memory_id" not in _result(narrowed, memory_id)["validity"]
    assert replacement not in json.dumps(narrowed, default=str)
    open_fence = _recall(context)
    assert _result(open_fence, memory_id)["validity"]["superseded_by_memory_id"] == replacement, (
        "the control: no domain filter names it"
    )


def test_recall_validity_withholds_a_successor_in_another_project(tmp_path: Path, monkeypatch) -> None:
    """The project half. The reader is bound to ``acme``; the successor is now in ``beta``.

    Mutation: build the recall scope as ``None`` for ``fence_validity_memory_ids``,
    or leave the project out of the scope the predicate gets.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    # Public, so that only the project keeps it from the read-only reader.
    _set(context, replacement, sensitivity="public")
    _set(
        context,
        replacement,
        metadata_json={**_metadata(context, replacement), "project_id": "beta", "project_scope": ["beta"]},
    )
    reader = _mint(context, profile="read_only_agent", project="acme")

    payload = _recall(context, key=reader)
    assert "superseded_by_memory_id" not in _result(payload, memory_id)["validity"]
    assert replacement not in json.dumps(payload, default=str)
    unbound = _recall(context)
    assert _result(unbound, memory_id)["validity"]["superseded_by_memory_id"] == replacement, (
        "the control: a caller bound to no project is told"
    )


def _people_vault(context, monkeypatch, *, link_successor: bool) -> tuple[str, str]:
    """M names Ada in its own metadata, so a people-scoped recall returns it.

    Only the entity graph can tie R to Ada, and only when ``link_successor`` is true.
    """
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, memory_id, status="active", metadata_json={**_metadata(context, memory_id), "people": ["ada"]})
    if link_successor:
        person = _store(context, lambda s: s.create_entity({"entity_type": "person", "name": "Ada"}))
        _store(
            context,
            lambda s: s.create_graph_edge(
                {
                    "from_type": "memory",
                    "from_id": replacement,
                    "to_type": "entity",
                    "to_id": str(person["id"]),
                    "edge_type": "mentions",
                    "confidence": 1.0,
                    "explanation": "test link",
                    "created_by": "test",
                }
            ),
        )
    return memory_id, replacement


def test_recall_validity_counts_a_person_link_made_through_the_entity_graph(tmp_path: Path, monkeypatch) -> None:
    """The successor is tied to the person only through a graph edge, and is still named.

    Mutation: hand ``fence_validity_memory_ids`` a predicate built with
    ``person_linked_memory_ids=frozenset()``. The id of a successor the
    people-scoped stages do return is then withheld.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _people_vault(context, monkeypatch, link_successor=True)
    payload = _recall(context, people=["ada"])
    assert _result(payload, memory_id)["validity"]["superseded_by_memory_id"] == replacement


def test_recall_validity_withholds_a_successor_the_person_is_not_linked_to(tmp_path: Path, monkeypatch) -> None:
    """No graph edge ties the successor to the person.

    Mutation: make the predicate ignore an active people scope.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _people_vault(context, monkeypatch, link_successor=False)
    payload = _recall(context, people=["ada"])
    assert "superseded_by_memory_id" not in _result(payload, memory_id)["validity"]
    assert replacement not in json.dumps(payload, default=str)


def test_recall_validity_fences_each_pointer_on_its_own(tmp_path: Path, monkeypatch) -> None:
    """``supersedes_memory_id`` is fenced like ``superseded_by_memory_id``, one key at a time.

    R is superseded by a visible row X and says it supersedes M, which is above
    the ceiling. Only M goes. Mutation: fence ``superseded_by_memory_id`` only
    (drop ``supersedes_memory_id`` from ``id_keys``), or drop both when either is hidden.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, old = _captured(context, monkeypatch)
    middle = _supersede(context, old)
    newest = _supersede(context, middle, THIRD)
    _set(context, old, sensitivity="confidential")
    _set(context, middle, status="active")
    reader = _mint(context, profile="read_only_agent", project="acme")

    payload = _recall(context, key=reader)
    validity = _result(payload, middle)["validity"]
    assert validity["superseded_by_memory_id"] == newest
    assert "supersedes_memory_id" not in validity
    assert old not in json.dumps(payload, default=str)

    allowed = _recall(context, sensitivity_allowed=list(CONFIDENTIAL_TOO))
    assert _result(allowed, middle)["validity"]["supersedes_memory_id"] == old, "the control"


def test_recall_validity_keeps_a_pointer_to_no_row_unless_the_read_is_scoped(tmp_path: Path, monkeypatch) -> None:
    """A pointer to a row that cannot be found is not a hidden row.

    An unscoped read keeps it, as in v0.19.0. A scoped read fails closed on it,
    as the pack does. Mutation: always drop it, or always keep it.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    _set(context, memory_id, status="active", superseded_by=DEAD)
    reader = _mint(context, profile="read_only_agent", project="acme")

    assert _result(_recall(context), memory_id)["validity"]["superseded_by_memory_id"] == DEAD
    scoped = _result(_recall(context, key=reader), memory_id)["validity"]
    assert scoped["superseded"] is True
    assert "superseded_by_memory_id" not in scoped


# -- (b) recent_changes on a context pack --------------------------------------------


def test_recent_changes_omit_events_about_a_memory_above_the_ceiling(tmp_path: Path, monkeypatch) -> None:
    """Keyed and keyless, the pack lists only events whose target the caller may read.

    Mutation: keep every event in ``_select_events``, or build the predicate in
    ``_recent_changes`` as always True.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    reader = _mint(context, profile="read_only_agent", project="acme")

    keyed = _call(context, "alice_context_pack", {"query": OLD}, key=reader, full=True)
    assert memory_id in _change_targets(keyed), "the readable memory's events stay; the test is not vacuous"
    assert replacement not in _change_targets(keyed)
    assert replacement not in json.dumps(keyed, default=str)

    # The keyless owner runs under the default ceiling, which hides it too.
    owner = _pack(context, OLD)
    assert memory_id in _change_targets(owner)
    assert replacement not in _change_targets(owner)

    allowed = _pack(context, OLD, sensitivity_allowed=CONFIDENTIAL_TOO)
    assert replacement in _change_targets(allowed), "the control: a ceiling that reaches it lists it"


def test_recent_changes_omit_events_outside_the_requested_domains(tmp_path: Path, monkeypatch) -> None:
    """The domain half. Mutation: build the predicate in ``_recent_changes`` with ``domains=[]``."""
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    _set(context, replacement, sensitivity="private", domain="professional")

    narrowed = _pack(context, OLD, domains=("project",))
    assert memory_id in _change_targets(narrowed)
    assert replacement not in _change_targets(narrowed)
    assert replacement in _change_targets(_pack(context, OLD)), "the control: no domain filter lists it"


def test_recent_changes_count_a_memory_corrected_inside_a_time_window(tmp_path: Path, monkeypatch) -> None:
    """The window bounds the event, not the row the event is about.

    The memory's own validity starts in 2020, so the row would fail a seven day
    window. Its event is from today, so it stays. Mutation: build the predicate in
    ``_recent_changes`` from the pack's full scope instead of the window-free identity scope.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    _set(context, memory_id, valid_from="2020-01-01T00:00:00Z")

    windowed = _pack(context, OLD, time_window="7d")
    assert memory_id in _change_targets(windowed)


def test_recent_changes_look_past_a_run_of_hidden_events(tmp_path: Path, monkeypatch) -> None:
    """A run of newer events the caller may not read does not crowd the readable ones out.

    Thirty confidential memories are written after the readable one, so its events
    are older than every hidden event, and more than the four-fold overfetch of
    the default limit. Mutation: take one fetch of ``limit`` or of ``limit * 4`` events and
    filter it, instead of deepening the prefix until enough readable events survive.
    """
    context = _context(tmp_path, monkeypatch)
    visible = _call(
        context,
        "alice_memory_commit",
        {"title": "Readable kettle", "canonical_text": "The readable kettle sits by the sink.", "sensitivity": "private"},
        full=True,
    )
    visible_id = str(visible["memory"]["id"])
    for index in range(30):
        _call(
            context,
            "alice_memory_commit",
            {
                "title": f"Hidden {index}",
                "canonical_text": f"Hidden fact number {index} stays in the vault.",
                "sensitivity": "confidential",
            },
            full=True,
        )

    targets = _change_targets(_pack(context, "readable kettle"))
    assert visible_id in targets
    hidden = {
        str(row["id"]) for row in _store(context, lambda s: s.list_memories(status=None, limit=100))
        if row.get("sensitivity") == "confidential"
    }
    assert len(hidden) == 30 and not hidden.intersection(targets)


def test_recent_changes_keep_an_event_for_no_row_unless_the_pack_is_scoped() -> None:
    """An event whose target cannot be found is not about a hidden row.

    An unscoped pack keeps it, as before. A scoped pack fails closed on it, as before.
    Mutation: drop it on an unscoped pack. Mutation: keep it on a scoped pack.
    """
    from tests.unit.test_vnext_retrieval import InMemoryVNextRetrievalStore, _memory_row
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest

    event = {
        "id": "event-ghost",
        "event_type": "memory.updated",
        "actor_type": "system",
        "target_type": "memory",
        "target_id": "memory-ghost",
        "occurred_at": "2026-07-01T00:00:00Z",
    }
    store = InMemoryVNextRetrievalStore(
        memories=[_memory_row("memory-real", "Alice ghost event row.", project_id="alicebot")],
        sources=[],
        seeded_events=[event],
    )
    service = VNextRetrievalService(store)  # type: ignore[arg-type]
    unscoped = service.compile_context_pack(VNextRetrievalRequest(query="Alice ghost event"))
    assert _change_targets(unscoped) == ["memory-ghost"]
    scoped = service.compile_context_pack(VNextRetrievalRequest(query="Alice ghost event", projects=("alicebot",)))
    assert _change_targets(scoped) == []


# -- (c) supersession_context on a context_depth: high pack --------------------------


def _chain(context, monkeypatch, *, length: int = 3) -> list[str]:
    """M superseded by R1, superseded by R2, and so on. Returns [M, R1, R2, ...], newest last."""
    _source_id, memory_id = _captured(context, monkeypatch)
    ids = [memory_id]
    for text in (NEW, THIRD, FOURTH)[: length - 1]:
        ids.append(_supersede(context, ids[-1], text))
    return ids


def _note(pack: dict, memory_id: str) -> dict:
    notes = [note for note in pack.get("supersession_context") or [] if note.get("memory_id") == memory_id]
    assert notes, "the packed memory has no supersession note; the assertions below would be vacuous"
    return notes[0]


def _named(note: dict, direction: str) -> list[str]:
    return [str(entry.get("id")) for entry in note[direction]]


def test_a_hidden_revision_is_neither_named_nor_titled_to_a_keyed_reader(tmp_path: Path, monkeypatch) -> None:
    """R2 supersedes R1 supersedes M, and M is above the reader's ceiling.

    R1 is one step from R2 and visible. M is two steps away and hidden. Mutation:
    drop the ``memory_visible`` check in ``_walk_supersession_chain``. M's id and
    its title come back.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second = _chain(context, monkeypatch)
    _set(context, old, sensitivity="confidential", title=MARKER)
    reader = _mint(context, profile="read_only_agent", project="acme")

    pack = _call(
        context,
        "alice_context_pack",
        {"query": "kettle second shelf", "context_depth": "high"},
        key=reader,
        full=True,
    )
    note = _note(pack, second)
    assert _named(note, "supersedes") == [first]
    assert note["supersedes"][0]["title"], "the visible revision is still titled"
    assert old not in json.dumps(pack, default=str)
    assert MARKER not in json.dumps(pack, default=str)


def test_a_hidden_revision_is_neither_named_nor_titled_on_an_unscoped_pack(tmp_path: Path, monkeypatch) -> None:
    """No scope is active, so a hidden row must not be shown as an id-only reference.

    The default ceiling hides the confidential row. Mutation: treat a hidden row like a
    missing one, which an unscoped pack shows as ``{"id": ...}``.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second = _chain(context, monkeypatch)
    _set(context, old, sensitivity="confidential", title=MARKER)

    pack = _pack(context, "kettle second shelf", context_depth="high")
    note = _note(pack, second)
    assert _named(note, "supersedes") == [first]
    assert old not in json.dumps(pack, default=str)
    assert MARKER not in json.dumps(pack, default=str)

    allowed = _pack(context, "kettle second shelf", context_depth="high", sensitivity_allowed=CONFIDENTIAL_TOO)
    control = _note(allowed, second)
    assert _named(control, "supersedes") == [first, old], "the control: a ceiling that reaches it names it"
    assert MARKER in json.dumps(control, default=str)


def test_a_hidden_revision_ends_the_walk_and_names_nothing_past_it(tmp_path: Path, monkeypatch) -> None:
    """R3 -> R2 -> R1 (hidden) -> M (visible). The walk stops at R1, so M is not named either.

    Mutation: skip a hidden hop and keep walking, naming the rows beyond it.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second, third = _chain(context, monkeypatch, length=4)
    _set(context, first, sensitivity="confidential")

    pack = _pack(context, "kettle pantry cupboard", context_depth="high")
    note = _note(pack, third)
    assert _named(note, "supersedes") == [second]
    # Section by section: the readable M still has events in recent_changes.
    context_section = json.dumps(pack["supersession_context"], default=str)
    assert first not in context_section
    assert old not in context_section, "the readable row beyond the hidden one is not named"
    assert first not in json.dumps(pack, default=str)


def test_a_hidden_newer_revision_is_withheld_like_an_older_one(tmp_path: Path, monkeypatch) -> None:
    """The ``superseded_by`` direction goes through the same fence.

    M is packed, points at R1 (visible), and R1 points at R2 (hidden). Mutation: apply the
    check to the ``supersedes`` direction only.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second = _chain(context, monkeypatch)
    _set(context, second, sensitivity="confidential", title=MARKER)
    _set(context, old, status="active")

    pack = _pack(context, OLD, context_depth="high")
    note = _note(pack, old)
    assert _named(note, "superseded_by") == [first]
    serialized = json.dumps(pack, default=str)
    assert second not in serialized
    assert MARKER not in serialized


def _move_to_project(context, memory_id: str, project: str) -> None:
    _set(
        context,
        memory_id,
        metadata_json={**_metadata(context, memory_id), "project_id": project, "project_scope": [project]},
    )


def test_the_walk_stops_at_a_revision_in_another_project(tmp_path: Path, monkeypatch) -> None:
    """The project half of the walk's fence, at a hop the pointer sanitizer never sees.

    R3 -> R2 -> R1 -> M. R1 is two steps from the packed row and now belongs to
    ``beta``. A pack scoped to ``acme`` names R2 only. Mutation: hand the walk a
    predicate built with no scope.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second, third = _chain(context, monkeypatch, length=4)
    for memory_id in (old, first, second, third):
        _set(context, memory_id, sensitivity="public")
    _move_to_project(context, first, "beta")

    scoped = _note(_pack(context, "kettle pantry cupboard", context_depth="high", projects=("acme",)), third)
    assert _named(scoped, "supersedes") == [second]
    assert first not in json.dumps(scoped, default=str)
    open_fence = _note(_pack(context, "kettle pantry cupboard", context_depth="high"), third)
    assert _named(open_fence, "supersedes") == [second, first, old], "the control: no project scope names them"


def test_the_walk_stops_at_a_revision_the_person_is_not_linked_to(tmp_path: Path, monkeypatch) -> None:
    """The person half. The packed row and its neighbour are tied to Ada through the graph only.

    R3 -> R2 -> R1 -> M. R1 has no link, so a people-scoped pack names R2 and stops.
    Mutation: hand the walk a predicate built with no person links, which hides R2
    as well, or one with no scope, which names R1.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second, third = _chain(context, monkeypatch, length=4)
    for memory_id in (old, first, second, third):
        _set(context, memory_id, sensitivity="public")
    person = _store(context, lambda s: s.create_entity({"entity_type": "person", "name": "Ada"}))
    for linked_id in (second, third):
        _store(
            context,
            lambda s, linked_id=linked_id: s.create_graph_edge(
                {
                    "from_type": "memory",
                    "from_id": linked_id,
                    "to_type": "entity",
                    "to_id": str(person["id"]),
                    "edge_type": "mentions",
                    "confidence": 1.0,
                    "explanation": "test link",
                    "created_by": "test",
                }
            ),
        )

    pack = _pack(context, "kettle pantry cupboard", context_depth="high", people=("ada",))
    assert _named(_note(pack, third), "supersedes") == [second]
    assert first not in json.dumps(pack["supersession_context"], default=str)


def test_the_walk_stops_at_a_revision_outside_the_time_window(tmp_path: Path, monkeypatch) -> None:
    """The time half. R1 became valid in 2020, so a seven day window does not reach it.

    Mutation: hand the walk a predicate built with no scope, or with a scope that has no window.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second, third = _chain(context, monkeypatch, length=4)
    for memory_id in (old, first, second, third):
        _set(context, memory_id, sensitivity="public")
    _set(context, first, valid_from="2020-01-01T00:00:00Z")

    windowed = _note(_pack(context, "kettle pantry cupboard", context_depth="high", time_window="7d"), third)
    assert _named(windowed, "supersedes") == [second]
    open_fence = _note(_pack(context, "kettle pantry cupboard", context_depth="high"), third)
    assert _named(open_fence, "supersedes") == [second, first, old], "the control: no window names them"


def test_the_walk_keeps_an_id_only_reference_to_no_row_unless_the_pack_is_scoped(tmp_path: Path, monkeypatch) -> None:
    """A pointer to no row is not a hidden row, and the two are told apart in both directions.

    An unscoped pack still shows it as an id-only reference, as in v0.19.0. A scoped pack
    drops it. Mutation: stop showing it on an unscoped pack. Mutation: show it on a scoped one.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second = _chain(context, monkeypatch)
    _set(context, first, supersedes=DEAD)

    unscoped = _note(_pack(context, "kettle second shelf", context_depth="high"), second)
    assert _named(unscoped, "supersedes") == [first, DEAD]
    assert unscoped["supersedes"][1] == {"id": DEAD}
    scoped = _note(_pack(context, "kettle second shelf", context_depth="high", projects=("acme",)), second)
    assert _named(scoped, "supersedes") == [first, old][:1]
    assert DEAD not in json.dumps(scoped, default=str)


# -- each fence input, pinned on its own where the tests above leave it out ------------


def test_recall_validity_withholds_a_predecessor_for_the_keyless_owner(tmp_path: Path, monkeypatch) -> None:
    """The pointers of a middle row are looked up together, not one key at a time.

    An unscoped read keeps a pointer it never looked up, so only a lookup of both
    ids can find that the predecessor is above the ceiling. Mutation: build
    ``pointer_ids`` in ``fence_validity_memory_ids`` from the first id key only.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, old = _captured(context, monkeypatch)
    middle = _supersede(context, old)
    newest = _supersede(context, middle, THIRD)
    _set(context, old, sensitivity="confidential")
    _set(context, middle, status="active")

    payload = _recall(context)
    validity = _result(payload, middle)["validity"]
    assert validity["superseded_by_memory_id"] == newest
    assert "supersedes_memory_id" not in validity
    assert old not in json.dumps(payload, default=str)


def test_recall_validity_withholds_a_successor_outside_the_time_window(tmp_path: Path, monkeypatch) -> None:
    """The time half of the recall fence. The successor became valid in 2020.

    Mutation: build the recall scope for ``fence_validity_memory_ids`` with no
    window, or with ``None``.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _hide_successor(context, monkeypatch)
    _set(context, replacement, sensitivity="public", valid_from="2020-01-01T00:00:00Z")

    windowed = _recall(context, since="2026-01-01T00:00:00Z")
    assert "superseded_by_memory_id" not in _result(windowed, memory_id)["validity"]
    assert replacement not in json.dumps(windowed, default=str)
    open_window = _recall(context)
    assert _result(open_window, memory_id)["validity"]["superseded_by_memory_id"] == replacement, "the control"


def test_the_walk_stops_at_a_revision_outside_the_requested_domains(tmp_path: Path, monkeypatch) -> None:
    """The domain half of the walk's fence. R1 is in another domain, so a pack for ``project`` stops there.

    Mutation: hand the walk a predicate built with ``domains=[]``.
    """
    context = _context(tmp_path, monkeypatch)
    old, first, second = _chain(context, monkeypatch)
    for memory_id in (old, first, second):
        _set(context, memory_id, sensitivity="public")
    _set(context, old, domain="professional", title=MARKER)

    pack = _pack(context, "kettle second shelf", context_depth="high", domains=("project",))
    assert _named(_note(pack, second), "supersedes") == [first]
    assert old not in json.dumps(pack, default=str)
    assert MARKER not in json.dumps(pack, default=str)
    control = _pack(context, "kettle second shelf", context_depth="high")
    assert _named(_note(control, second), "supersedes") == [first, old], "the control: no domain filter names it"


def test_supersession_context_names_nothing_when_the_first_hop_is_hidden() -> None:
    """The walk checks the first hop itself, not only the hops after it.

    The pointer sanitizer normally drops a hidden first hop before the walk runs,
    so this calls the walk directly. Mutation: check ``memory_visible`` only on
    the hops after the first.
    """
    from tests.unit.test_vnext_retrieval import _memory_row

    service = _fake_service([], [_memory_row("m-old", "Old kettle fact.")])
    packed = _memory_row("m-new", "New kettle fact.", supersedes="m-old")
    notes = service._supersession_context(
        [packed],
        scope=_ResolvedRetrievalScope(projects=frozenset(), people=frozenset(), window_start=None, window_end=None),
        memory_visible=lambda row: False,
    )
    assert notes[0]["supersedes"] == []
    assert "m-old" not in json.dumps(notes)


def _fake_service(events: list[dict], memories: list[dict]) -> VNextRetrievalService:
    """A store with no scoped event listing, so ``_recent_changes`` takes the legacy path."""
    from tests.unit.test_vnext_retrieval import InMemoryVNextRetrievalStore

    store = InMemoryVNextRetrievalStore(memories=memories, sources=[], seeded_events=events)
    return VNextRetrievalService(store)  # type: ignore[arg-type]


def _event(event_id: str, target_id: str) -> dict:
    return {
        "id": event_id,
        "event_type": "memory.updated",
        "actor_type": "system",
        "target_type": "memory",
        "target_id": target_id,
        "occurred_at": "2026-07-01T00:00:00Z",
    }


NO_SCOPE = _ResolvedRetrievalScope(projects=frozenset(), people=frozenset(), window_start=None, window_end=None)
DEFAULT_CEILING = ["public", "internal", "private", "unknown"]


def test_recent_changes_count_a_memory_tied_to_the_person_through_the_entity_graph(
    tmp_path: Path, monkeypatch
) -> None:
    """The person links reach the ``recent_changes`` predicate.

    The memory is tied to Ada only through a graph edge, so only the links the
    pack resolved can keep its events. Mutation: build the predicate in
    ``_recent_changes`` with ``person_linked_memory_ids=frozenset()``, or pass an
    empty set from ``compile_context_pack``.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    _set(context, memory_id, status="active")
    person = _store(context, lambda s: s.create_entity({"entity_type": "person", "name": "Ada"}))
    _store(
        context,
        lambda s: s.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": memory_id,
                "to_type": "entity",
                "to_id": str(person["id"]),
                "edge_type": "mentions",
                "confidence": 1.0,
                "explanation": "test link",
                "created_by": "test",
            }
        ),
    )
    pack = _pack(context, OLD, people=("ada",))
    assert pack["relevant_memories"], "the people-scoped pack returns the memory; the assertion below is not vacuous"
    assert memory_id in _change_targets(pack)


def test_recent_changes_on_a_store_without_scoped_events_apply_the_person_scope() -> None:
    """A store that cannot scope its event listing is fenced here, person included.

    Ada's memory keeps its event and Bob's does not. Mutation: build the identity
    scope in ``_recent_changes`` with ``people=frozenset()``.
    """
    from tests.unit.test_vnext_retrieval import _memory_row

    service = _fake_service(
        [_event("e-ada", "m-ada"), _event("e-bob", "m-bob")],
        [
            _memory_row("m-ada", "Kettle note for Ada.", metadata_json={"people": ["ada"]}),
            _memory_row("m-bob", "Kettle note for Bob.", metadata_json={"people": ["bob"]}),
        ],
    )
    scope = _ResolvedRetrievalScope(projects=frozenset(), people=frozenset({"ada"}), window_start=None, window_end=None)
    changes = service._recent_changes(
        scope=scope,
        person_linked_memory_ids=frozenset(),
        domains=[],
        sensitivity_allowed=DEFAULT_CEILING,
    )
    assert [change["target_id"] for change in changes] == ["m-ada"]


def test_recent_changes_keep_an_event_for_no_row_on_a_window_only_pack() -> None:
    """A pack scoped only by a time window is not an identity-scoped pack.

    The event for no row stays, as it does with no scope at all. Mutation: decide
    whether to fail closed on a missing row with ``scope.active`` instead of
    ``identity_scope.active``.
    """
    service = _fake_service([_event("e-ghost", "m-ghost")], [])
    scope = _ResolvedRetrievalScope(
        projects=frozenset(),
        people=frozenset(),
        window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        window_end=None,
    )
    changes = service._recent_changes(
        scope=scope,
        person_linked_memory_ids=frozenset(),
        domains=[],
        sensitivity_allowed=DEFAULT_CEILING,
    )
    assert [change["target_id"] for change in changes] == ["m-ghost"]


# -- a vault the caller mostly cannot read still gets a pack ---------------------------


def _seed_memories(context, *, count: int, sensitivity: str, label: str) -> list[str]:
    """Create ``count`` memories straight in the store, oldest first. Each writes memory events."""

    def seed(store) -> list[str]:
        return [
            str(
                store.create_memory(
                    {
                        "memory_key": f"memory.{uuid4()}",
                        "value": {"text": f"{label} {index}"},
                        "status": "active",
                        "title": f"{label} {index}",
                        "canonical_text": f"{label} filler fact {index} stays put.",
                        "summary": "x",
                        "domain": "project",
                        "sensitivity": sensitivity,
                    }
                )["id"]
            )
            for index in range(count)
        ]

    return _store(context, seed)


def test_a_pack_keeps_the_visible_changes_found_before_the_scan_ceiling(tmp_path: Path, monkeypatch) -> None:
    """A run of hidden events longer than the scan returns a short list, not a failed pack.

    Two readable memories are newest, then 100 confidential ones, then one readable
    memory that is older than every hidden event. The scan stops at 64 rows. The two
    newest readable events are kept, the older readable one is past the ceiling and is
    not listed, and no hidden id is named. Mutation: do not catch the completeness
    error in ``_recent_changes``. Mutation: return an empty list when it is caught.
    Mutation: leave ``max_rows`` out of the ``_fetch_filtered_prefix`` call, which
    scans to the 16384 row ceiling and lists the older readable event as well.
    """
    monkeypatch.setattr(vnext_retrieval_module, "RECENT_CHANGES_SCAN_MAX_ROWS", 64)
    context = _context(tmp_path, monkeypatch)
    (old_readable,) = _seed_memories(context, count=1, sensitivity="private", label="Old readable")
    hidden = _seed_memories(context, count=100, sensitivity="confidential", label="Hidden")
    new_readable = _seed_memories(context, count=2, sensitivity="private", label="New readable")

    try:
        pack = _pack(context, "filler fact stays put")
    except VNextRetrievalCompletenessError as error:
        raise AssertionError(f"the pack raised instead of listing what it found: {error}") from error
    targets = _change_targets(pack)
    assert set(new_readable) <= set(targets), "the visible events found before the ceiling are kept"
    assert old_readable not in targets, "the older readable event is past the scan ceiling"
    assert not set(hidden).intersection(targets)
    assert not set(hidden).intersection(json.dumps(pack, default=str).split('"'))


def test_a_pack_on_a_store_without_scoped_events_keeps_the_visible_changes_found(monkeypatch) -> None:
    """The legacy listing path degrades the same way as the scoped one.

    Mutation: swallow the completeness error only when the store lists events with scope
    parameters, and let it propagate from the plain listing.
    """
    from tests.unit.test_vnext_retrieval import _memory_row

    monkeypatch.setattr(vnext_retrieval_module, "RECENT_CHANGES_SCAN_MAX_ROWS", 64)
    memories = [_memory_row(f"m-hidden-{index}", "Kettle hidden fact.", sensitivity="confidential") for index in range(100)]
    memories += [_memory_row("m-new-a", "Kettle visible fact a."), _memory_row("m-new-b", "Kettle visible fact b.")]
    # The store lists newest first, so the later events are the newer ones.
    events = [_event(f"e-hidden-{index}", f"m-hidden-{index}") for index in range(100)]
    events += [_event("e-new-a", "m-new-a"), _event("e-new-b", "m-new-b")]
    service = _fake_service(events, memories)

    try:
        pack = service.compile_context_pack(VNextRetrievalRequest(query="kettle fact"))
    except VNextRetrievalCompletenessError as error:
        raise AssertionError(f"the pack raised instead of listing what it found: {error}") from error
    assert _change_targets(pack) == ["m-new-b", "m-new-a"]


def test_the_scan_ceiling_can_be_lowered_and_never_raised(monkeypatch) -> None:
    """``max_rows`` lowers the ceiling a prefix is deepened to, and cannot lift it.

    Mutation: use ``max_rows`` as given, without ``min`` against ``LEGACY_SCOPED_SCAN_MAX_ROWS``.
    Mutation: ignore ``max_rows``.
    """
    asked: list[int] = []

    def endless(limit: int) -> tuple[list[dict], str]:
        asked.append(limit)
        return [{"id": f"row-{index}"} for index in range(limit)], "legacy"

    with pytest.raises(VNextRetrievalCompletenessError, match="within 100 rows"):
        _fetch_filtered_prefix(endless, select_rows=lambda rows: [], target=1, initial_limit=10, max_rows=100)
    assert asked == [10, 20, 40, 80, 100]

    asked.clear()
    monkeypatch.setattr(vnext_retrieval_module, "LEGACY_SCOPED_SCAN_MAX_ROWS", 64)
    with pytest.raises(VNextRetrievalCompletenessError, match="within 64 rows"):
        _fetch_filtered_prefix(endless, select_rows=lambda rows: [], target=1, initial_limit=10, max_rows=1000)
    assert asked == [10, 20, 40, 64]


def test_the_recent_changes_scan_ceiling_sits_between_the_first_fetch_and_the_legacy_ceiling() -> None:
    """The scan has to reach past the first fetch and stay well short of the 16384 row legacy ceiling.

    Mutation: set ``RECENT_CHANGES_SCAN_MAX_ROWS`` to the legacy ceiling. Mutation: set it
    below the first fetch of ``limit * 4`` rows.
    """
    first_fetch = vnext_retrieval_module.DEFAULT_RECENT_CHANGES_LIMIT * 4
    ceiling = vnext_retrieval_module.RECENT_CHANGES_SCAN_MAX_ROWS
    assert first_fetch < ceiling <= vnext_retrieval_module.LEGACY_SCOPED_SCAN_MAX_ROWS // 4


def test_memory_ids_go_to_the_store_in_batches_older_sqlite_builds_take() -> None:
    """Deepening can look up thousands of event targets, and SQLite 3.31 and older bind at most 999.

    Every id is still looked up. Mutation: send all the ids in one call. Mutation: drop
    the last, partial batch.
    """

    class Recording:
        def __init__(self) -> None:
            self.batches: list[int] = []

        def get_memories_by_ids(self, memory_ids):
            self.batches.append(len(memory_ids))
            return [{"id": memory_id} for memory_id in memory_ids]

    store = Recording()
    ids = [f"memory-{index}" for index in range(2 * MEMORY_ID_LOOKUP_BATCH_SIZE + 77)]
    found = VNextRetrievalService(store)._memories_by_ids(ids)  # type: ignore[arg-type]
    assert sorted(found) == sorted(ids)
    assert max(store.batches) <= 999
    assert len(store.batches) == 3


# -- the controls cannot be left out ------------------------------------------------


def test_the_pointer_fence_helper_tells_a_missing_row_from_a_hidden_one() -> None:
    """``targets`` holds the rows the pointers resolved to.

    Mutation: skip the visibility check. Mutation: drop an unresolved pointer when
    ``fail_closed_when_unresolved`` is false, or keep one when it is true.
    """
    rows = {"hidden": {"id": "hidden"}, "shown": {"id": "shown"}}

    def holders():
        return [{"a": "hidden", "b": "shown", "c": "missing", "d": None, "e": ""}]

    for fail_closed, expected in ((False, {"b", "c"}), (True, {"b"})):
        held = holders()
        _drop_pointers_outside_fence(
            held,
            ("a", "b", "c", "d", "e"),
            targets=rows,
            memory_visible=lambda row: row["id"] != "hidden",
            fail_closed_when_unresolved=fail_closed,
        )
        kept = {key for key, value in held[0].items() if value}
        assert kept == expected, fail_closed


def test_every_fence_control_is_required() -> None:
    """A defaulted control is 'no fence' for the next caller that forgets it.

    Mutation: give any of these parameters a default.
    """
    required = {
        VNextRetrievalService.memory_visibility: ("domains", "sensitivity_allowed", "scope"),
        VNextRetrievalService.fence_validity_memory_ids: ("domains", "sensitivity_allowed", "scope"),
        VNextRetrievalService._supersession_context: ("memory_visible",),
        VNextRetrievalService._walk_supersession_chain: ("memory_visible",),
        VNextRetrievalService._recent_changes: ("domains", "sensitivity_allowed"),
        _drop_pointers_outside_fence: ("memory_visible", "fail_closed_when_unresolved"),
    }
    for function, names in required.items():
        parameters = inspect.signature(function).parameters
        for name in names:
            parameter = parameters[name]
            assert parameter.default is inspect.Parameter.empty, f"{function.__qualname__}.{name}"
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{function.__qualname__}.{name}"
