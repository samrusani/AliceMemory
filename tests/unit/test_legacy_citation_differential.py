"""The closure finds at least every input that its reading of ``source_refs`` found before the saved-quote parser joined it.

``previous_closure_reading`` holds that reading word for word. These tests run it and the closure on a corpus of citation
shapes in every place a client's reference is copied to, on object entries that hold a typed ref or a source under any key
(the text keys included), and on seeded random records, and fail when the closure finds fewer inputs than the previous reading
did. The closure may find more: the reader's shapes and its incidental ids are candidates that are inputs when they name a
stored row. It may never find less.

The comparison is on ``(kind, id)``: an ``artifact:<id>`` that the previous reading found is lost if the closure finds that id
only as a memory or a source, because the row it names is then never looked up. Every pair the previous reading found must be a
hard input, a candidate or an unreadable id of the closure (``test_the_closure_finds_every_input_the_previous_reading_found``).
The one exception is an id that the closure holds as a hard input of another kind: no id names a row in two tables, and the
row is unverified whenever that hard input is not stored.

And what a producer writes (``memory:<id>`` and ``source:<id>``) and the keys that name a row stay hard inputs in the same
kind: a row that cites a row that is not stored by one of them is unverified, as it was. What the previous reading took from a word that is
not an id (``source:'<id>'``, ``memory:<id>#chunk-1``), from a bare id, or from an object of the list is a candidate now: it
resolves to a stored row of its kind or names nothing, and syntax alone does not make a row unverified.

Mutations (``scripts/derived_label_mutations.json``): in ``_source_refs_strings`` drop the strings of ``source_refs`` from the
hard inputs; drop the sources or the typed refs that the objects of ``source_refs`` give; read a ``source_refs`` that is one
object as nothing.
"""

from __future__ import annotations

import json
import random
import re
from uuid import UUID, uuid4

import pytest

from alicebot_api.vnext_derived_labels import dependencies_of, soft_inputs
from tests.unit.legacy_citation_support import corpus_entries, positions, typed_object_entries
from tests.unit.previous_closure_reading import previous_dependencies

MEMORY = "11111111-2222-4333-8444-555555555555"
SOURCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ARTIFACT = "5eadbeef-0000-4000-8000-0000000000ff"
OPEN_LOOP = "77777777-6666-4555-8444-333333333333"
GHOST_ID = "99999999-8888-4777-8666-555555555555"


def _record(kind: str, extra: dict[str, object], *, where: str = "metadata") -> dict[str, object]:
    if kind == "artifact":
        return {"kind": "artifact", "id": str(uuid4()), "user_id": "u", "artifact_type": "memory_consolidation",
                "metadata_json": {"workflow": "memory_consolidation", "project_scope": [], **extra}}
    if kind == "open_loop":
        return {"kind": "open_loop", "id": str(uuid4()), "user_id": "u", "source_id": None,
                "metadata_json": {"discovered_by": "vnext_daily_brief", "project_scope": [], **extra}}
    meta = {"candidate_kind": "memory_consolidation", "project_scope": [], "consolidation": {"proposal_kind": "dedup", "cluster_member_ids": []}}
    if where == "value":
        return {"kind": "memory", "id": str(uuid4()), "user_id": "u", "metadata_json": meta, "value": {"kind": "memory_consolidation_proposal", "text": "t", **extra}}
    return {"kind": "memory", "id": str(uuid4()), "user_id": "u", "metadata_json": {**meta, **extra}, "value": {"kind": "memory_consolidation_proposal", "text": "t"}}


def _canonical(ids: set[str]) -> set[str]:
    found = set()
    for item in ids:
        try:
            if str(UUID(item)) == item:
                found.add(item)
        except ValueError:
            continue
    return found


def _closure_ids(kind: str, row: dict[str, object]) -> set[str]:
    return {row_id for _kind, row_id in _closure_pairs(kind, row)}


def _closure_pairs(kind: str, row: dict[str, object]) -> set[tuple[str, str]]:
    hard = set(dependencies_of(kind, row))
    candidates, unreadable = soft_inputs(kind, row)
    return hard | set(candidates) | set(unreadable)


def _canonical_pairs(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    kept = _canonical({row_id for _kind, row_id in pairs})
    return {(kind, row_id) for kind, row_id in pairs if row_id in kept}


def _lost(kind: str, row: dict[str, object], *, other_kind_is_found: bool) -> set[tuple[str, str]]:
    """The ``(kind, id)`` pairs of canonical ids that the previous reading found and the closure does not.

    ``other_kind_is_found`` lets an id stand when the closure holds it as a hard input of another kind (see the module text)."""

    lost = _canonical_pairs(previous_dependencies(kind, row)) - _closure_pairs(kind, row)
    if other_kind_is_found:
        hard_ids = {row_id for _kind, row_id in dependencies_of(kind, row)}
        lost = {pair for pair in lost if pair[1] not in hard_ids}
    return lost


ENTRIES = {
    **corpus_entries(MEMORY, SOURCE),
    **typed_object_entries(MEMORY, SOURCE, ARTIFACT, OPEN_LOOP),
}
PLACES = ("top", "proposal", "agentic", "provenance", "replacement", "nested", "bare")
CASES = [
    (holder, position, name, entry)
    for name, entry in ENTRIES.items()
    for position in PLACES
    for holder in ("artifact", "memory", "open_loop")
]


def _is_object_entry(entry: object) -> bool:
    return not isinstance(entry, str)


@pytest.mark.parametrize("holder,position,name,entry", CASES, ids=lambda value: value if isinstance(value, str) else "e")
def test_the_closure_finds_every_input_the_previous_reading_found(holder: str, position: str, name: str, entry: object) -> None:
    row = _record(holder, positions(entry)[position])
    assert not _lost(holder, row, other_kind_is_found=False), (holder, position, name, _lost(holder, row, other_kind_is_found=False))


@pytest.mark.parametrize("holder,position,name,entry", [case for case in CASES if isinstance(case[3], str)],
                         ids=lambda value: value if isinstance(value, str) else "e")
def test_what_the_previous_reading_took_from_a_string_as_an_id_is_still_a_hard_input(holder: str, position: str, name: str, entry: object) -> None:
    """A string of ``source_refs`` that the previous reading took as a row that must be stored (``memory:<id>`` and ``source:<id>`` in any
    spelling of the id, a bare id, a source the reader names) is still one: a row that cites a row that is not stored by one of
    them is unverified, as it was. What the previous reading took from a word that is no id (``source:'<id>'``,
    ``memory:<id>#chunk-1``) names nothing, and an id inside it is a candidate."""

    row = _record(holder, positions(entry)[position])
    expected = {(kind, row_id) for kind, row_id in previous_dependencies(holder, row) if row_id in _canonical({row_id})}
    assert expected <= set(dependencies_of(holder, row)), (holder, position, name)


@pytest.mark.parametrize("key", ["source_id", "source_ids", "memory_ids", "open_loop_ids", "artifact_ids", "belief_ids",
                                 "stale_marked_memory_ids", "source_artifact_id", "artifact_id"])
def test_what_the_previous_reading_took_from_the_id_keys_is_still_a_hard_input(key: str) -> None:
    for value in ([GHOST_ID], GHOST_ID):
        row = _record("artifact", {key: value, "input_summary": {"memory_ids": [GHOST_ID], "source_ids": [GHOST_ID]}})
        assert previous_dependencies("artifact", row) <= set(dependencies_of("artifact", row)), (key, value)


def test_the_corpus_is_not_vacuous() -> None:
    """The previous reading found something in many of the shapes, and the closure finds something it did not in many more."""

    found_before = found_more = typed = 0
    for holder, position, _name, entry in CASES:
        row = _record(holder, positions(entry)[position])
        previous = _canonical_pairs(previous_dependencies(holder, row))
        closure = _closure_pairs(holder, row)
        found_before += bool(previous)
        found_more += bool(closure - previous)
        typed += bool({kind for kind, _row_id in previous} & {"artifact", "open_loop"})
    assert found_before > 800 and found_more > 800 and typed > 150, (found_before, found_more, typed)


def test_the_value_of_a_memory_is_read_as_well() -> None:
    row = _record("memory", {"source_refs": [json.dumps({"memory_id": MEMORY})]}, where="value")
    assert MEMORY in _closure_ids("memory", row)


# --- seeded random records -----------------------------------------------------------------------------------------------

IDS = (MEMORY, SOURCE, ARTIFACT)
PREFIX = ("", "", "memory:", "source:", "MEMORY:", "Source:", "alice://memories/", "alice://sources/", "open_loop:", "artifact:",
          "memory: ", "source: ", "memory:memory:")
KEYS = ("memory_id", "memory_ids", "source_id", "source_ids", "source_ref", "id", "ref", "origin", "chunk_id", "ref_id", "title",
        "text", "summary", "description", "content", "body", "quote", "note", "memories", "sources", "selected_memory_ids",
        "parent_memory_id", "label", "url", "excerpt", "conversation_excerpt")


def _spell(rng: random.Random, ident: str) -> str:
    return rng.choice([ident, ident, ident.upper(), ident.replace("-", ""), "{" + ident + "}", "urn:uuid:" + ident, "uuid:" + ident])


def _atom(rng: random.Random) -> str:
    text = rng.choice(PREFIX) + _spell(rng, rng.choice(IDS))
    roll = rng.random()
    if roll < 0.15:
        text = f"see {text} for details"
    elif roll < 0.2:
        text += "#chunk-1"
    elif roll < 0.23:
        text = f"[{text}]"
    elif roll < 0.26:
        text = f"https://h.example/x/{text}"
    elif roll < 0.28:
        text += "/page/2"
    return text


def _node(rng: random.Random, depth: int) -> object:
    roll = rng.random()
    if depth <= 0 or roll < 0.35:
        return _atom(rng)
    if roll < 0.6:
        return {rng.choice(KEYS): _node(rng, depth - 1) for _ in range(rng.randint(1, 2))}
    if roll < 0.8:
        return [_node(rng, depth - 1) for _ in range(rng.randint(1, 3))]
    if roll < 0.93:
        return json.dumps(_node(rng, depth - 1))
    return json.dumps({rng.choice(KEYS): _node(rng, depth - 1)})


@pytest.mark.parametrize("seed", range(300))
def test_random_records_give_the_closure_every_id_the_previous_reading_found(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(20):
        entry = _node(rng, 3)
        holder = rng.choice(["list", "bare", "nested"])
        extra = {"source_refs": [entry]} if holder == "list" else {"source_refs": entry} if holder == "bare" else {"x": {"source_refs": [entry]}}
        kind = rng.choice(["artifact", "memory", "open_loop"])
        row = _record(kind, extra)
        try:
            expected = previous_dependencies(kind, row)
        except Exception:
            continue
        lost = _lost(kind, row, other_kind_is_found=True)
        assert not lost, (kind, json.dumps(extra, sort_keys=True), lost)
