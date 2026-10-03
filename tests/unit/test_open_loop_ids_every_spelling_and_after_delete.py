"""An open loop shows no withheld id in any spelling, and deleting a source does not bring its id back.

Unreleased (on main, not in v0.20.0). The read-side rule of ``vnext_open_loop_references`` (the sibling file
``test_open_loop_references_read_fence.py``) withholds from a loop the ids of sources and memories its reader may not
read. An outside review of that change reproduced two gaps, both identifier exposures with no source content:

1. An id written without hyphens (32 hex digits) inside a URL or a longer string was not recognised, so it survived in
   ``metadata_json``, under ``source_refs`` too, while the hyphenated spelling of the same id was replaced.
2. An id a loop repeated under a key that names no reference (``evidence.quote_from``) was withheld while the source
   it named was refused, and shown again once that source was soft-deleted: the lookup left deleted rows out, the id
   then named no row, and an id that names no row is kept outside a reference key because it may be a trace id.

Now an id is read in every spelling the link writer reads, whole or inside longer text, and compared in canonical form
(section 1). The lookup reads deleted sources and memories too, so a deleted row is refused and not unknown, and the
ids the response withholds from a reference position are withheld from every other position of every row of that
response (sections 2 and 3). Section 4 holds what must not change: readable ids, trace ids, shas and digests. Section
5 pins the store reads, section 6 runs every surface over a real SQLite vault with real agent keys, and the last
section pins the words.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back.
"""

from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import alicebot_api.sqlite_store as sqlite_store
import alicebot_api.vnext_store as postgres_store
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_open_loop_references import (
    withhold_unreadable_references,
    withhold_unreadable_references_from_pack,
)
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.vnext_stores.postgres import memory_access as postgres_memory
from tests.unit.test_open_loop_references_read_fence import (  # noqa: F401  (``vault`` and ``world`` are fixtures)
    _FENCE,
    _loop,
    _World,
    vault,
    world,
)
from tests.unit.test_source_refs_read_fence import (
    _ROOT,
    _USER_ID,
    _Vault,
    _memories,
    _memory_row,
    _source,
)

_CUT = "(id withheld)"
_DELETED = "2026-01-01T00:00:00Z"


# -- the spellings ------------------------------------------------------------------------------------------------


def _odd(compact: str) -> str:
    """The 32 digits with hyphens in groups of four, a place ``UUID()`` reads and the standard layout does not use."""

    return "-".join(compact[start : start + 4] for start in range(0, 32, 4))


# name, text before the id, shape of the 32 digits, upper case, text after the id. Every one is read by the link
# writer (``UUID()``, with the ``source:`` and ``memory:`` prefixes removed first) and by ``_canonical_id``.
_SPELLINGS = (
    ("hyphen_lower", "", "hyphen", False, ""),
    ("hyphen_upper", "", "hyphen", True, ""),
    ("compact_lower", "", "compact", False, ""),
    ("compact_upper", "", "compact", True, ""),
    ("odd_hyphens", "", "odd", False, ""),
    ("odd_hyphens_upper", "", "odd", True, ""),
    ("braces", "{", "hyphen", False, "}"),
    ("braces_compact", "{", "compact", False, "}"),
    ("urn_uuid", "urn:uuid:", "hyphen", False, ""),
    ("urn_uuid_compact", "urn:uuid:", "compact", False, ""),
    ("uuid_prefix", "uuid:", "hyphen", False, ""),
    ("source_prefix", "source:", "hyphen", False, ""),
    ("source_prefix_compact", "source:", "compact", True, ""),
    ("memory_prefix", "memory:", "hyphen", False, ""),
    ("memory_prefix_compact", "memory:", "compact", False, ""),
)
_SPELLING_NAMES = [spelling[0] for spelling in _SPELLINGS]


def _parts(identifier: str, name: str) -> tuple[str, str, str]:
    """``(before, id as written, after)``. The whole string is the three joined, and what stays when the id is
    withheld from longer text is ``before`` and ``after`` around ``(id withheld)``."""

    _name, before, shape, upper, after = next(spelling for spelling in _SPELLINGS if spelling[0] == name)
    compact = identifier.replace("-", "")
    core = {"hyphen": identifier, "compact": compact, "odd": _odd(compact)}[shape]
    return before, core.upper() if upper else core, after


def _reference_part(kind: str, parts: tuple[str, str, str]) -> tuple[dict[str, object], dict[str, object]]:
    """The metadata a loop keeps under reference keys for one id, and what is left of it when the id is withheld.

    A whole-string id goes with its key or list slot. An id inside a URL or a sentence is replaced.
    """

    before, core, after = parts
    whole, cut = before + core + after, before + _CUT + after
    scalar, listed = ("source_id", "source_refs") if kind == "source" else ("memory_id", "memory_ids")
    stored = {
        scalar: whole,
        listed: [whole, f"https://example.test/{kind}/{whole}", f"derived from {whole} on import", "a label"],
    }
    shown = {listed: [f"https://example.test/{kind}/{cut}", f"derived from {cut} on import", "a label"]}
    return stored, shown


def _plain_part(parts: tuple[str, str, str]) -> tuple[dict[str, object], dict[str, object]]:
    """The same, under keys that name no reference: a value, a sentence, a URL, a list slot, a key, a key in a sentence."""

    before, core, after = parts
    whole, cut = before + core + after, before + _CUT + after
    stored = {
        "evidence": {
            "quote_from": whole,
            "note": f"derived from {whole} on import",
            "link": f"https://example.test/notes/{whole}",
            "all": [whole, "kept"],
        },
        "audit": {"parent": {whole: "x", f"run {whole}": 2, "kept": 1}},
    }
    shown = {
        "evidence": {
            "note": f"derived from {cut} on import",
            "link": f"https://example.test/notes/{cut}",
            "all": ["kept"],
        },
        "audit": {"parent": {f"run {cut}": 2, "kept": 1}},
    }
    return stored, shown


class _Store:
    """The two bulk reads as both real stores have them: live rows by default, a deleted one with ``include_deleted``.

    It records every call, so a test can see whether the reader asked for deleted rows.
    """

    def __init__(self, sources: list[dict[str, object]] = (), memories: list[dict[str, object]] = ()) -> None:  # type: ignore[assignment]
        self.sources = {str(row["id"]).lower(): row for row in sources}
        self.memories = {str(row["id"]).lower(): row for row in memories}
        self.calls: list[tuple[str, tuple[str, ...], bool]] = []

    def get_sources_by_ids(self, source_ids: list[str], *, include_deleted: bool = False) -> list[dict[str, object]]:
        self.calls.append(("sources", tuple(source_ids), include_deleted))
        rows = [self.sources[i] for i in source_ids if i in self.sources]
        return [row for row in rows if include_deleted or row.get("deleted_at") is None]

    def get_memories_by_ids(self, memory_ids: list[str], *, include_deleted: bool = False) -> list[dict[str, object]]:
        self.calls.append(("memories", tuple(memory_ids), include_deleted))
        rows = [self.memories[i] for i in memory_ids if i in self.memories]
        return [row for row in rows if include_deleted or row.get("deleted_at") is None]


class _LiveOnlyStore:
    """A store whose bulk reads take no ``include_deleted`` (a stand-in, like the scheduler's test store)."""

    def __init__(self, store: _Store) -> None:
        self._store = store

    def get_sources_by_ids(self, source_ids: list[str]) -> list[dict[str, object]]:
        return self._store.get_sources_by_ids(source_ids)

    def get_memories_by_ids(self, memory_ids: list[str]) -> list[dict[str, object]]:
        return self._store.get_memories_by_ids(memory_ids)


def _state(kind: str, state: str) -> tuple[_Store, str]:
    """A store holding one row of ``kind`` in ``state`` for a reader of project ``alpha``, and the row's id.

    refused: a live row of another project. soft_deleted: a row of the reader's own project that was deleted.
    archived and redacted (memories): the forms a deleted memory takes. hard_deleted: no row at all.
    """

    identifier = str(uuid4())
    if state == "hard_deleted":
        return _Store(), identifier
    if state == "admitted":
        row = _source(identifier, scope=["alpha"]) if kind == "source" else _memory_row(identifier, scope=["alpha"])
    elif state == "refused":
        row = _source(identifier, scope=["beta"]) if kind == "source" else _memory_row(identifier, scope=["beta"])
    else:
        row = (
            _source(identifier, scope=["alpha"], deleted_at=_DELETED)
            if kind == "source"
            else _memory_row(identifier, scope=["alpha"], deleted_at=_DELETED)
        )
        if state == "archived":
            row["status"] = "archived"
        if state == "redacted":
            row.update(status="archived", title="[redacted]", canonical_text="[redacted]", metadata_json={"redacted_at": _DELETED})
    return (_Store([row]) if kind == "source" else _Store(memories=[row])), identifier


_ROW_STATES = (
    ("source", "refused"),
    ("source", "soft_deleted"),
    ("memory", "refused"),
    ("memory", "soft_deleted"),
    ("memory", "archived"),
    ("memory", "redacted"),
)
_ROW_STATE_IDS = [f"{kind}-{state}" for kind, state in _ROW_STATES]


# -- 1. every spelling, in every position, for every state of the row -----------------------------------------------


@pytest.mark.parametrize("spelling", _SPELLING_NAMES)
@pytest.mark.parametrize(("kind", "state"), _ROW_STATES, ids=_ROW_STATE_IDS)
def test_a_withheld_id_is_withheld_in_every_spelling_at_every_position_when_the_loop_names_it_under_a_reference_key(
    kind: str, state: str, spelling: str
) -> None:
    """The id of a source or memory the reader may not read (another project's row, a deleted row, an archived or a
    redacted memory) is gone from a loop in all fifteen spellings. The loop names it under a reference key and under
    keys that name no reference, as a whole value, in a URL, in a sentence, in a list and as a key. A whole-string id is
    removed with its key or slot and an id inside text is replaced by ``(id withheld)``, with the text around it kept.

    On main a spelling without hyphens or with its hyphens in other places survived inside a URL or a sentence, under
    a reference key too. Mutations: scan text for the hyphenated spelling only (the compact and odd-hyphen rows fail),
    or leave a state out of the lookup (the row's id is then read as a missing one).
    """

    store, identifier = _state(kind, state)
    parts = _parts(identifier, spelling)
    stored_ref, shown_ref = _reference_part(kind, parts)
    stored_plain, shown_plain = _plain_part(parts)
    loop = _loop(metadata_json={"project_scope": ["alpha"], **stored_ref, **stored_plain})
    out = withhold_unreadable_references(store, [loop], fence=_FENCE)
    assert out[0]["metadata_json"] == {"project_scope": ["alpha"], **shown_ref, **shown_plain}
    assert identifier.replace("-", "").lower() not in re.sub(r"[^0-9a-f]", "", json.dumps(out[0]).lower())


@pytest.mark.parametrize("spelling", _SPELLING_NAMES)
@pytest.mark.parametrize(("kind", "state"), _ROW_STATES, ids=_ROW_STATE_IDS)
def test_a_refused_or_deleted_id_is_withheld_under_keys_that_name_no_reference_even_when_nothing_in_the_response_links_it(
    kind: str, state: str, spelling: str
) -> None:
    """The case behind the second finding, with no reference position at all: the loop repeats the id only under
    ``evidence`` and ``audit``, with no ``source_id`` column and no reference key anywhere in the response. A deleted
    row is refused and not unknown, because the lookup reads deleted rows, so its id goes as a refused row's does.

    Mutation: ask the stores for live rows only (drop ``include_deleted`` from the call in ``_rows_by_id``). The
    soft-deleted, archived and redacted rows then name nothing, and the id under an unknown key stays.
    """

    store, identifier = _state(kind, state)
    stored_plain, shown_plain = _plain_part(_parts(identifier, spelling))
    out = withhold_unreadable_references(store, [_loop(metadata_json={"project_scope": ["alpha"], **stored_plain})], fence=_FENCE)
    assert out[0]["metadata_json"] == {"project_scope": ["alpha"], **shown_plain}
    assert any(call[2] for call in store.calls), "the lookup must have asked for deleted rows"


@pytest.mark.parametrize("spelling", _SPELLING_NAMES)
@pytest.mark.parametrize("kind", ["source", "memory"])
@pytest.mark.parametrize("link", ["reference_key", "column", "other_loop"])
def test_the_id_of_a_row_that_is_gone_is_withheld_everywhere_when_the_response_links_it_somewhere(
    kind: str, link: str, spelling: str
) -> None:
    """A row removed outright (or an id that never named one) cannot be told from a trace id, so under a key that names
    no reference it is kept. But once the same response links that id at a reference position, as the ``source_id``
    or ``memory_id`` column, under a reference key, or in another loop of the response, every other position of every
    row withholds it too, in every spelling. The link is written in the canonical spelling and the repeats in the
    spelling under test, so the match is by canonical id.

    Mutation: drop the ``referenced`` ids from ``withheld`` in ``withhold_unreadable_references`` (the per-response
    rule). Every row here is then kept.
    """

    store, identifier = _state(kind, "hard_deleted")
    stored_plain, shown_plain = _plain_part(_parts(identifier, spelling))
    column = "source_id" if kind == "source" else "memory_id"
    metadata: dict[str, object] = {"project_scope": ["alpha"], **stored_plain}
    expected: dict[str, object] = {"project_scope": ["alpha"], **shown_plain}
    loops = [_loop(metadata_json=metadata)]
    if link == "reference_key":
        listed = "source_refs" if kind == "source" else "memory_ids"
        metadata[listed] = [identifier, "a label"]
        expected[listed] = ["a label"]
    elif link == "column":
        loops[0][column] = identifier
    else:
        loops.append(_loop(**{column: identifier}))
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    assert out[0]["metadata_json"] == expected
    assert out[0][column] is None


@pytest.mark.parametrize("spelling", _SPELLING_NAMES)
@pytest.mark.parametrize("kind", ["source", "memory"])
def test_a_row_that_is_gone_and_linked_nowhere_is_kept_under_an_unknown_key_like_a_trace_id(kind: str, spelling: str) -> None:
    """The one case that is not withheld, stated in the docstring of the module: an id that names no row, under keys
    that name no reference, with nothing in the response that links it. It may be a run id or a trace id. It is
    returned as stored, in every spelling.

    Mutation: withhold every id under an unknown key that names no row (the trace-id tests of section 4 fail too).
    """

    store, identifier = _state(kind, "hard_deleted")
    stored_plain, _shown = _plain_part(_parts(identifier, spelling))
    metadata = {"project_scope": ["alpha"], **stored_plain}
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata)], fence=_FENCE)
    assert out[0]["metadata_json"] == metadata and out[0]["metadata_json"] is metadata


@pytest.mark.parametrize("spelling", _SPELLING_NAMES)
@pytest.mark.parametrize("kind", ["source", "memory"])
def test_an_id_the_reader_may_read_is_never_withheld_in_any_spelling(kind: str, spelling: str) -> None:
    """The control that keeps a blanket removal from passing. An own-project source or memory is admitted, so the loop
    that names it in all fifteen spellings and every position comes back as stored, the same metadata object.

    Mutation: make the scrub withhold ids that are admitted (drop the ``admitted`` test from ``_scrub_text``).
    """

    store, identifier = _state(kind, "admitted")
    parts = _parts(identifier, spelling)
    stored_ref, _shown_ref = _reference_part(kind, parts)
    stored_plain, _shown_plain = _plain_part(parts)
    column = "source_id" if kind == "source" else "memory_id"
    metadata = {"project_scope": ["alpha"], **stored_ref, **stored_plain}
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata, **{column: identifier})], fence=_FENCE)
    assert out[0]["metadata_json"] == metadata and out[0]["metadata_json"] is metadata
    assert out[0][column] == identifier


def test_a_memory_id_under_a_source_key_and_a_source_id_under_a_memory_key_are_held_to_the_same_rule() -> None:
    """The reference keys are matched by name, not by kind of row. A refused memory under ``source_refs`` and a refused
    source under ``memory_ids`` are withheld, and an admitted memory under ``source_refs`` stays.

    Mutation: let a source key admit only an admitted source and a memory key only an admitted memory (the admitted
    memory under ``source_refs`` is then dropped).
    """

    refused_memory = _state("memory", "refused")
    refused_source = _state("source", "refused")
    admitted_memory = _state("memory", "admitted")
    store = _Store(
        sources=list(refused_source[0].sources.values()),
        memories=[*refused_memory[0].memories.values(), *admitted_memory[0].memories.values()],
    )
    metadata = {
        "source_refs": [refused_memory[1], admitted_memory[1], "a label"],
        "memory_ids": [refused_source[1].replace("-", ""), admitted_memory[1]],
    }
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata)], fence=_FENCE)
    assert out[0]["metadata_json"] == {"source_refs": [admitted_memory[1], "a label"], "memory_ids": [admitted_memory[1]]}


# -- 2. the withheld ids are collected over the whole response -----------------------------------------------------


def test_one_loop_cannot_show_an_id_that_another_loop_of_the_same_call_withholds() -> None:
    """Loop A links an id the store does not hold (the id of a row removed outright) in its ``source_id`` column.
    Loop B repeats it under ``evidence.quote_from`` and in a sentence, compact. In one call B loses it. Called alone, B
    keeps it, because nothing in that response links it: the rule is per response, and the docstring says so.

    Mutations: build the withheld ids per row (B then keeps the id in the shared call), or build them per call but
    only from the ``refused`` rows.
    """

    ghost = str(uuid4())
    link = _loop(source_id=ghost)
    repeat = _loop(metadata_json={"evidence": {"quote_from": ghost}, "note": f"copied from {ghost.replace('-', '')}"})
    both = withhold_unreadable_references(_Store(), [link, repeat], fence=_FENCE)
    assert both[0]["source_id"] is None
    assert both[1]["metadata_json"] == {"evidence": {}, "note": f"copied from {_CUT}"}
    alone = withhold_unreadable_references(_Store(), [repeat], fence=_FENCE)
    assert alone[0]["metadata_json"] == repeat["metadata_json"]


def test_the_loops_of_one_context_pack_are_one_response() -> None:
    """The pack form passes every loop through one call, so a loop that links an id withholds it from the loops beside
    it. A loop of another pack is not affected.

    Mutation: call ``withhold_unreadable_references`` once per loop in ``withhold_unreadable_references_from_pack``.
    """

    ghost = str(uuid4())
    link = _loop(metadata_json={"source_refs": [ghost]})
    repeat = _loop(metadata_json={"evidence": {"quote_from": ghost.upper()}})
    pack = {"open_loops": [link, repeat], "trace": {"id": ghost}}
    out = withhold_unreadable_references_from_pack(_Store(), pack, fence=_FENCE)
    assert out["open_loops"][0]["metadata_json"] == {"source_refs": []}
    assert out["open_loops"][1]["metadata_json"] == {"evidence": {}}
    assert out["trace"] == {"id": ghost}, "the other sections of a pack are not scanned here"
    other = withhold_unreadable_references_from_pack(_Store(), {"open_loops": [repeat]}, fence=_FENCE)
    assert other["open_loops"][0]["metadata_json"] == repeat["metadata_json"]


def test_an_id_the_reader_may_read_is_shown_in_every_loop_even_when_another_loop_links_it() -> None:
    """The per-response rule only adds ids that were withheld. A readable source that one loop links and another repeats
    under ``evidence.quote_from`` is shown in both, and a readable id and a withheld one in the same loop are told apart.

    Mutation: add every id of a reference position to the withheld set, admitted or not.
    """

    store, own = _state("source", "admitted")
    ghost = str(uuid4())
    loops = [
        _loop(source_id=own, metadata_json={"source_refs": [own, ghost]}),
        _loop(metadata_json={"evidence": {"quote_from": own, "also": ghost}}),
    ]
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    assert out[0]["source_id"] == own and out[0]["metadata_json"] == {"source_refs": [own]}
    assert out[1]["metadata_json"] == {"evidence": {"quote_from": own}}


# -- 3. text that holds more than one window --------------------------------------------------------------------


def test_an_id_next_to_hex_digits_that_form_another_window_is_still_found_and_cut() -> None:
    """Windows of an id can overlap: a hyphenated window of unrelated digits that ends inside the real id, or a
    group of hex digits joined to it by a hyphen. A scan that consumed each match would let the first window eat the
    front of the id and leave the rest unrecognised. Every window is read, and the id is cut wherever it stands.

    Mutation: find ids with a consuming pattern (``findall``, no lookahead) in ``_id_windows``: the bogus window of the
    first case then swallows the front of the id and the rest is shown.
    """

    store, identifier = _state("source", "refused")
    straddling = f"aaaaaaaa-bbbb-cccc-dddd-abcd{identifier[:8]}-{identifier[9:]}"
    joined = f"deadbeefcafe-{identifier}"
    unrelated_hex = f"feed{identifier}"
    metadata = {"a": straddling, "b": joined, "c": unrelated_hex, "d": f"{identifier.replace('-', '')}-0123abcd"}
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata)], fence=_FENCE)
    shown = out[0]["metadata_json"]
    assert shown["a"] == f"aaaaaaaa-bbbb-cccc-dddd-abcd{_CUT}"  # type: ignore[index]
    assert shown["b"] == f"deadbeefcafe-{_CUT}"  # type: ignore[index]
    assert shown["c"] == f"feed{_CUT}"  # type: ignore[index]
    assert shown["d"] == f"{_CUT}-0123abcd"  # type: ignore[index]
    assert identifier.replace("-", "") not in _squashed(shown)


def test_two_withheld_ids_in_one_string_are_both_replaced_and_the_text_between_is_kept() -> None:
    """Replacement cuts each window that reads as a withheld id and leaves everything else of the string as it was.

    Mutation: stop after the first cut, or cut from the first window to the last.
    """

    first_store, first = _state("source", "refused")
    second_store, second = _state("source", "soft_deleted")
    store = _Store([*first_store.sources.values(), *second_store.sources.values()])
    text = f"from {first.replace('-', '')} via {second.upper()} and {first} then 0123abcd"
    out = withhold_unreadable_references(store, [_loop(metadata_json={"note": text})], fence=_FENCE)
    assert out[0]["metadata_json"] == {"note": f"from {_CUT} via {_CUT} and {_CUT} then 0123abcd"}


# -- 4. what must not change: readable ids, trace ids, shas and digests ------------------------------------------


def test_shas_digests_and_trace_ids_under_unknown_keys_are_returned_as_stored() -> None:
    """Hex that is not an id is kept. A 40-digit git sha, a 64-digit digest, a 32-digit md5 and a trace UUID (the
    hyphenated one and its compact form), whole, inside a URL and inside a sentence, under the keys the product uses
    (``automation_digest``, ``trace_id``, ``run.trace_id``, ``policy_decision.trace_id``) and under unknown ones.

    Mutation: withhold every 32-digit run or every UUID-shaped string under an unknown key.
    """

    sha, digest, md5 = uuid4().hex + uuid4().hex[:8], uuid4().hex + uuid4().hex, uuid4().hex
    trace = str(uuid4())
    metadata = {
        "project_scope": ["alpha"],
        "automation_digest": digest,
        "workflow_digest": f"sha256:{digest}",
        "git": {"sha": sha, "url": f"https://example.test/commit/{sha}", "note": f"fixed in {sha} on main"},
        "files": {"md5": md5, "note": f"md5 {md5} matches", "url": f"https://example.test/blob/{md5}"},
        "trace_id": trace,
        "run": {"trace_id": trace, "agent_run_id": "run-7"},
        "policy_decision": {"trace_id": trace.replace("-", ""), "note": f"trace {trace} and {trace.replace('-', '')}"},
        "digests": [digest, f"digest of {digest} ok", f"https://example.test/{digest}"],
    }
    out = withhold_unreadable_references(_Store(), [_loop(metadata_json=metadata)], fence=_FENCE)
    assert out[0]["metadata_json"] == metadata and out[0]["metadata_json"] is metadata


def test_a_sha_or_digest_that_contains_a_withheld_id_is_not_cut() -> None:
    """The hex boundary. A run of 32 digits with no hyphen is an id only when no hex digit stands next to it. A 40-digit
    sha, a 64-digit digest and a 36-digit run that start or end with the 32 digits of a withheld id are returned whole,
    and two withheld ids written one after the other with nothing between them (a 64-digit run) are not read as
    two ids. The same ids with a non-hex character beside them are cut.

    Mutation: drop the two hex lookarounds of ``_BARE_ID`` (the runs of this test are then cut at the id).
    """

    store, identifier = _state("source", "refused")
    other_store, other = _state("source", "refused")
    store = _Store([*store.sources.values(), *other_store.sources.values()])
    compact, other_compact = identifier.replace("-", ""), other.replace("-", "")
    whole = {
        "sha_after": compact + "0123abcd",
        "sha_before": "0123abcd" + compact,
        "digest_after": compact + uuid4().hex,
        "digest_before": uuid4().hex + compact,
        "digest_two_ids": compact + other_compact,
        "run_36": compact + "0123",
        "odd_after_hex": "f" + _odd(compact),
        "odd_before_hex": _odd(compact) + "f",
        "in_url": f"https://example.test/blob/{compact}0123abcd",
        "in_sentence": f"built from {compact}0123abcd today",
    }
    cut_or_not = {
        "letter_after": (f"{compact}g", f"{_CUT}g"),
        "letter_before": (f"x{compact}", f"x{_CUT}"),
        "underscore": (f"id_{compact}_end", f"id_{_CUT}_end"),
        "slash": (f"/{compact}/", f"/{_CUT}/"),
        "equals": (f"id={compact};", f"id={_CUT};"),
        "hyphen_neighbours": (f"0123-{compact}-abcd", f"0123-{_CUT}-abcd"),
        "odd_with_spaces": (f"id {_odd(compact)} end", f"id {_CUT} end"),
    }
    metadata = {**whole, **{key: stored for key, (stored, _shown) in cut_or_not.items()}}
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata)], fence=_FENCE)
    shown = out[0]["metadata_json"]
    assert {key: shown[key] for key in whole} == whole  # type: ignore[index]
    assert {key: shown[key] for key in cut_or_not} == {key: value for key, (_stored, value) in cut_or_not.items()}  # type: ignore[index]


def test_a_sha_and_a_digest_under_a_reference_key_are_returned_whole_and_an_md5_is_withheld_as_an_id_that_names_no_row() -> None:
    """Under a reference key the rule is strict, so an id that names no admitted row goes. A 32-digit run on its own is
    an id (the link writer reads it), so a bare md5 there is removed and inside text is replaced, as the hyphenated
    trace UUID has always been. A 40-digit sha and a 64-digit digest are no id, and stay.

    Mutation: treat a 40-digit or a 64-digit run as an id in a reference position (the first assertion fails), or stop
    reading a bare 32-digit run under a reference key (the second fails).
    """

    sha, digest, md5 = uuid4().hex + uuid4().hex[:8], uuid4().hex + uuid4().hex, uuid4().hex
    metadata = {"source_refs": [sha, digest, f"https://example.test/commit/{sha}", f"https://example.test/{digest}"]}
    out = withhold_unreadable_references(_Store(), [_loop(metadata_json=metadata)], fence=_FENCE)
    assert out[0]["metadata_json"] == metadata
    removed = withhold_unreadable_references(
        _Store(), [_loop(metadata_json={"source_refs": [md5, f"https://example.test/blob/{md5}", "label"]})], fence=_FENCE
    )
    assert removed[0]["metadata_json"] == {"source_refs": [f"https://example.test/blob/{_CUT}", "label"]}


def test_an_id_that_is_split_or_has_a_non_hex_digit_is_not_an_id_and_is_not_withheld() -> None:
    """Inside text the scan reads 32 ASCII hex digits, with hyphens or none, and nothing else. An id with one digit
    replaced by a letter outside the hex range and an id split in two by a space are not ids, so they are returned as
    stored. The docstring and the docs say so, so their claim is not wider than the code. (An id written backwards is
    another id, and names no row.)

    Mutation: let the scan skip over a space or a non-hex letter inside a window (the two values are then cut).
    """

    store, identifier = _state("source", "refused")
    compact = identifier.replace("-", "")
    metadata = {
        "swapped": f"see {compact[:10]}g{compact[11:]} now",
        "split": f"see {compact[:16]} {compact[16:]} now",
        "reversed": compact[::-1],
    }
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata)], fence=_FENCE)
    assert out[0]["metadata_json"] == metadata


# -- 5. the stores -----------------------------------------------------------------------------------------------


def test_a_store_whose_bulk_read_takes_no_include_deleted_is_asked_for_live_rows_and_a_linked_deleted_id_is_still_withheld() -> None:
    """A stand-in store with the old read (like the scheduler's test store) is called without the keyword, and a store
    with only the single-id read is asked one id at a time. A deleted row then reads as a missing one, so an id under an
    unknown key that nothing links is kept (the limit the docstring states), while one the response links is withheld.

    Mutation: pass ``include_deleted=True`` to every bulk read without looking at its signature (the old read raises
    ``TypeError``).
    """

    deleted_store, identifier = _state("source", "soft_deleted")
    linked = str(uuid4())
    old = _LiveOnlyStore(deleted_store)
    loops = [
        _loop(metadata_json={"evidence": {"quote_from": identifier}}),
        _loop(source_id=linked, metadata_json={"evidence": {"quote_from": linked}}),
    ]
    out = withhold_unreadable_references(old, loops, fence=_FENCE)
    assert out[0]["metadata_json"] == {"evidence": {"quote_from": identifier}}
    assert out[1]["metadata_json"] == {"evidence": {}}

    class _Single:
        def get_source(self, source_id: str) -> dict[str, object] | None:
            return None

    single = withhold_unreadable_references(_Single(), loops, fence=_FENCE)
    assert single[1]["metadata_json"] == {"evidence": {}}


def test_the_lookup_asks_both_stores_for_deleted_rows_in_slices() -> None:
    """The reader passes ``include_deleted=True`` to the source read and to the memory read, in slices of at most 500.

    Mutation: pass ``include_deleted=False`` or leave the argument out.
    """

    ids = [str(uuid4()) for _ in range(1200)]
    store = _Store()
    withhold_unreadable_references(store, [_loop(metadata_json={"evidence": {"all": ids}})], fence=_FENCE)
    assert [(kind, len(asked), deleted) for kind, asked, deleted in store.calls] == [
        ("sources", 500, True),
        ("sources", 500, True),
        ("sources", 200, True),
        ("memories", 500, True),
        ("memories", 500, True),
        ("memories", 200, True),
    ]


def test_the_sqlite_bulk_reads_return_a_deleted_row_only_when_asked(vault: _Vault) -> None:
    """``get_sources_by_ids`` and ``get_memories_by_ids`` leave a soft-deleted row out by default, as every other caller
    has always read them, and return it with ``include_deleted=True``, with ``deleted_at`` set so the fence refuses it.
    An archived and a redacted memory are soft-deleted rows. An id that names no row is returned by neither.

    Mutations: make either read ignore ``include_deleted`` and always filter (the deleted row is not returned when
    asked), or never filter (the default read returns it).
    """

    memory_ids = _memories(vault)
    archived = str(vault.commit("alpha_trusted", [])["payload"]["memory"]["id"])  # type: ignore[index]
    redacted = str(vault.commit("alpha_trusted", [])["payload"]["memory"]["id"])  # type: ignore[index]
    vault.sql("UPDATE memories SET status = 'archived', deleted_at = ? WHERE id = ?", (_DELETED, archived))
    path = _sqlite_path_from_url(vault.context.database_url)
    ghost = str(uuid4())
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        store.redact_memory_content(memory_id=redacted, actor_type="user")
        live_source, dead_source = vault.sources["own"], vault.sources["deleted"]
        asked = [dead_source, live_source, ghost]
        assert [row["id"] for row in store.get_sources_by_ids(asked)] == [live_source]
        assert [row["id"] for row in store.get_sources_by_ids(asked, include_deleted=False)] == [live_source]
        found = {row["id"]: row for row in store.get_sources_by_ids(asked, include_deleted=True)}
        assert set(found) == {dead_source, live_source}
        assert found[dead_source]["deleted_at"] is not None and found[live_source]["deleted_at"] is None
        asked_memories = [memory_ids["deleted"], memory_ids["own"], archived, redacted, ghost]
        assert {row["id"] for row in store.get_memories_by_ids(asked_memories)} == {memory_ids["own"]}
        found_memories = {row["id"]: row for row in store.get_memories_by_ids(asked_memories, include_deleted=True)}
        assert set(found_memories) == {memory_ids["deleted"], memory_ids["own"], archived, redacted}
        assert found_memories[memory_ids["own"]]["deleted_at"] is None
        assert all(found_memories[key]["deleted_at"] is not None for key in (memory_ids["deleted"], archived, redacted))
        assert store.get_sources_by_ids([]) == [] and store.get_memories_by_ids([], include_deleted=True) == []
        # The fence refuses every row the deleted-aware read hands over.
        fence = SourceReadFence.unfenced()
        assert not fence.admits(found[dead_source]) and fence.admits(found[live_source])
        assert not any(fence.admits_memory(found_memories[key]) for key in (memory_ids["deleted"], archived, redacted))
        assert fence.admits_memory(found_memories[memory_ids["own"]])


def test_the_postgres_bulk_reads_filter_deleted_rows_only_by_default() -> None:
    """The Postgres reads build the same query: ``deleted_at IS NULL`` by default and with ``include_deleted=False``,
    no such clause with ``include_deleted=True``, and the same ``ANY(uuid[])`` parameter. The store is not opened here
    (``tests/integration/test_open_loop_references_postgres.py`` runs the real statements). The functions are called on
    a stand-in that records the SQL and the parameters.

    Mutations, in ``vnext_store.py`` and ``vnext_stores/postgres/memory_access.py``: always add the clause (the
    ``include_deleted=True`` assertion fails), never add it (the default assertion fails), or drop the parameter (the
    call raises ``TypeError``).
    """

    seen: list[tuple[str, tuple[object, ...]]] = []

    def fetch_all(sql: str, params: tuple[object, ...]) -> list[object]:
        seen.append((sql, params))
        return []

    store = SimpleNamespace(_fetch_all=fetch_all)
    ids = [str(uuid4()), str(uuid4())]
    for read in (postgres_store.PostgresVNextStore.get_sources_by_ids, postgres_memory.get_memories_by_ids):
        seen.clear()
        read(store, ids)
        read(store, ids, include_deleted=False)
        read(store, ids, include_deleted=True)
        default_sql, explicit_false_sql, include_sql = (entry[0] for entry in seen)
        assert "deleted_at IS NULL" in default_sql and "deleted_at IS NULL" in explicit_false_sql
        assert "deleted_at IS NULL" not in include_sql and "ANY(%s::uuid[])" in include_sql
        assert all(entry[1] == (ids,) for entry in seen)
        assert read(store, []) == [] and len(seen) == 3, "no ids means no statement"


def test_both_stores_give_the_bulk_reads_the_same_signature() -> None:
    """The SQLite and Postgres reads take the same arguments (the memory reads are also compared by
    ``test_store_memory_access_split.py``), and ``include_deleted`` is keyword-only with a false default, so a positional
    call cannot ask for deleted rows by accident.

    Mutation: make ``include_deleted`` positional, or default it to true, in either store.
    """

    for name in ("get_sources_by_ids", "get_memories_by_ids"):
        sqlite_signature = inspect.signature(getattr(sqlite_store.SQLiteVNextStore, name))
        postgres_signature = inspect.signature(getattr(postgres_store.PostgresVNextStore, name))
        assert sqlite_signature == postgres_signature, name
        parameter = sqlite_signature.parameters["include_deleted"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is False, name


# -- 6. every surface, over a real SQLite vault with real agent keys -----------------------------------------------

_SURFACES = ("mcp_list", "mcp_edit", "http_review", "http_pack")


def _read(world: _World, surface: str, name: str, reader: str = "alpha_project") -> dict[str, object]:
    """The loop ``name`` as ``reader`` is shown it by one surface."""

    loop_id = world.loops[name]
    if surface == "mcp_list":
        return next(row for row in world.list_items(reader) if row["id"] == loop_id)
    if surface == "mcp_edit":
        return world.update(reader, name)["open_loop"]  # type: ignore[return-value]
    if surface == "http_review":
        status, result = world.review_route(reader, name)
        assert status == 200, result
        return result
    status, pack = world.pack_route(reader)
    assert status == 201, pack
    found = [row for row in pack["open_loops"] if row["id"] == loop_id]  # type: ignore[attr-defined]
    assert found, "the loop was planted last, so it must be among the pack's newest eight"
    return found[0]  # type: ignore[no-any-return]


def _squashed(value: object) -> str:
    """The row as text with everything but hex digits removed, so an id is found in any spelling."""

    return re.sub(r"[^0-9a-f]", "", json.dumps(value).lower())


@pytest.mark.parametrize("surface", _SURFACES)
def test_a_compact_id_inside_reference_text_is_withheld_on_every_surface(world: _World, surface: str) -> None:
    """The first finding as the reviewer reproduced it: a confidential source linked in the column, repeated in a URL
    under ``source_refs`` in the hyphenated and the compact spelling and in a sentence. The project scoped key is shown
    neither spelling on any surface.

    Mutation: find ids in text by the hyphenated spelling only (the compact URL and the sentence are shown).
    """

    source_id = world.sources["confidential"]
    compact = UUID(source_id).hex
    world._plant(
        "compact_probe",
        source_kind="confidential",
        metadata={
            "project_scope": ["alpha"],
            "source_refs": [f"https://example.test/source/{source_id}", f"https://example.test/source/{compact}"],
            "note": f"derived from {compact} on import",
        },
    )
    row = _read(world, surface, "compact_probe")
    assert row["source_id"] is None
    assert source_id not in json.dumps(row)
    assert compact not in json.dumps(row), row["metadata_json"]
    assert row["metadata_json"] == {
        "project_scope": ["alpha"],
        "source_refs": [f"https://example.test/source/{_CUT}", f"https://example.test/source/{_CUT}"],
        "note": f"derived from {_CUT} on import",
    }


@pytest.mark.parametrize("surface", _SURFACES)
def test_deleting_a_source_does_not_show_an_id_the_loop_repeated_under_another_key(world: _World, surface: str) -> None:
    """The second finding as the reviewer reproduced it. The loop links a confidential source in the column and in
    ``metadata_json.source_id`` and repeats the id under ``evidence.quote_from``. The id is withheld before the source is
    soft-deleted and after, on every surface.

    The two halves of the fix each cover this case alone (the lookup reads the deleted row, and the column and the
    ``source_id`` key link the id), so it fails only when both are removed. Mutation: ask the stores for live rows only
    and drop the per-response rule (the id under ``evidence.quote_from`` is then shown after the delete). The cases
    that each half must fail alone are in sections 1 and 2.
    """

    source_id = world.sources["confidential"]
    world._plant(
        "delete_probe",
        source_kind="confidential",
        metadata={"project_scope": ["alpha"], "source_id": source_id, "evidence": {"quote_from": source_id}},
    )
    before = _read(world, surface, "delete_probe")
    assert source_id not in json.dumps(before), before
    world.vault.sql("UPDATE sources SET deleted_at = '2026-10-03T00:00:00Z' WHERE id = ?", (source_id,))
    after = _read(world, surface, "delete_probe")
    assert after["source_id"] is None
    assert source_id not in json.dumps(after), after["metadata_json"]


@pytest.mark.parametrize("surface", _SURFACES)
def test_a_source_the_reader_may_read_is_shown_until_it_is_deleted_and_then_it_is_not(world: _World, surface: str) -> None:
    """The same loop over an own-project source, which the project scoped key may read. Every copy of the id is shown as
    stored (the column, the reference key and ``evidence.quote_from``) until the source is soft-deleted, and after that
    none of them is, in the compact spelling too.

    Mutation: as for the test above, both halves removed (the copy under ``evidence`` is then shown after the delete).
    """

    source_id = world.sources["own"]
    compact = UUID(source_id).hex
    world._plant(
        "own_probe",
        source_kind="own",
        metadata={
            "project_scope": ["alpha"],
            "source_id": source_id,
            "evidence": {"quote_from": source_id, "note": f"see {compact}"},
        },
    )
    before = _read(world, surface, "own_probe")
    assert before["source_id"] == source_id
    assert before["metadata_json"]["source_id"] == source_id  # type: ignore[index]
    assert before["metadata_json"]["evidence"] == {"quote_from": source_id, "note": f"see {compact}"}  # type: ignore[index]
    world.vault.sql("UPDATE sources SET deleted_at = '2026-10-03T00:00:00Z' WHERE id = ?", (source_id,))
    after = _read(world, surface, "own_probe")
    assert after["source_id"] is None
    assert compact not in _squashed(after) and source_id not in json.dumps(after)
    assert after["metadata_json"] == {"project_scope": ["alpha"], "evidence": {"note": f"see {_CUT}"}}


def _make_row(world: _World, state: str) -> tuple[str, str]:
    """``(kind, id)`` of a row of the vault in ``state`` that the project scoped key of ``alpha`` may not be shown."""

    vault = world.vault
    path = _sqlite_path_from_url(vault.context.database_url)

    def new_memory() -> str:
        made = vault.commit("alpha_trusted", [])
        assert made["is_error"] is False, made
        return str(made["payload"]["memory"]["id"])  # type: ignore[index]

    if state == "source_refused":
        return "source", world.sources["confidential"]
    if state == "source_soft_deleted":
        return "source", world.sources["deleted"]
    if state == "source_hard_deleted":
        source_id = vault._capture("Alpha note removed outright: kiln-gloss-81 applies on Fridays.", vault.keys["alpha_trusted"])
        vault.sql("DELETE FROM sources WHERE id = ?", (source_id,))
        return "source", source_id
    if state == "memory_refused":
        return "memory", world.memories["confidential"]
    if state == "memory_soft_deleted":
        return "memory", world.memories["deleted"]
    memory_id = new_memory()
    if state == "memory_archived":
        vault.sql("UPDATE memories SET status = 'archived', deleted_at = '2026-10-03T00:00:00Z' WHERE id = ?", (memory_id,))
    elif state == "memory_redacted":
        with sqlite_user_connection(path, _USER_ID) as conn:
            SQLiteVNextStore(conn, _USER_ID).redact_memory_content(memory_id=memory_id, actor_type="user")
    else:
        assert state == "memory_hard_deleted", state
        vault.sql("DELETE FROM memories WHERE id = ?", (memory_id,))
    return "memory", memory_id


_REAL_STATES = (
    "source_refused",
    "source_soft_deleted",
    "source_hard_deleted",
    "memory_refused",
    "memory_soft_deleted",
    "memory_archived",
    "memory_redacted",
    "memory_hard_deleted",
)


@pytest.mark.parametrize("link", ["column", "unlinked"])
@pytest.mark.parametrize("state", _REAL_STATES)
def test_every_state_of_a_row_is_withheld_in_every_spelling_on_every_surface(world: _World, state: str, link: str) -> None:
    """A loop repeats one id under ``evidence`` in six spellings (hyphenated upper, compact, compact upper in a URL, odd
    hyphens, braces, ``urn:uuid:`` in a sentence) next to a readable own-project source in the compact spelling. The row
    is refused, soft-deleted, removed outright, or a memory archived or redacted. The project scoped key reads the loop
    through the MCP list, ``edit``, the HTTP review route and the HTTP context pack.

    ``column`` also links the id in the ``source_id`` or ``memory_id`` column, so every state is withheld. With
    ``unlinked`` nothing links it: a refused, deleted, archived or redacted row is still withheld, because the lookup
    reads deleted rows, and the id of a row removed outright is kept, as the trace-id rule says. The readable own source
    stays in every case.

    Mutations: ask the stores for live rows only (the unlinked deleted, archived and redacted rows are then shown), drop
    the per-response rule (the column-linked removed-outright rows are then shown), or scan text for the hyphenated
    spelling only (the compact, odd-hyphen and braces copies are shown).
    """

    kind, identifier = _make_row(world, state)
    compact = identifier.replace("-", "")
    own = world.sources["own"]
    own_compact = UUID(own).hex
    metadata: dict[str, object] = {
        "project_scope": ["alpha"],
        "evidence": {
            "upper": identifier.upper(),
            "url": f"https://example.test/notes/{compact.upper()}",
            "odd": _odd(compact),
            "braces": "{" + compact + "}",
            "sentence": f"copied from urn:uuid:{identifier} today",
            "plain": f"quoted {compact}",
            "readable": f"own note {own_compact}",
        },
    }
    fields: dict[str, object] = {}
    if link == "column":
        fields = {"source_value": identifier} if kind == "source" else {"memory_value": identifier}
    world._plant(f"state_{state}_{link}", metadata=metadata, **fields)  # type: ignore[arg-type]
    still_shown = state.endswith("hard_deleted") and link == "unlinked"
    for surface in _SURFACES:
        row = _read(world, surface, f"state_{state}_{link}")
        text = _squashed(row)
        assert (compact.lower() in text) is still_shown, (surface, state, link, row["metadata_json"])
        assert own_compact in text, (surface, "the readable own source must stay")
        assert row["metadata_json"]["evidence"]["readable"] == f"own note {own_compact}"  # type: ignore[index]
        if not still_shown:
            assert row["source_id"] is None and row["memory_id"] is None, (surface, state)


@pytest.mark.parametrize("action", ["close", "snooze", "reopen"])
def test_the_other_update_actions_withhold_a_compact_and_a_deleted_id_too(world: _World, action: str) -> None:
    """``close``, ``snooze`` and ``reopen`` share the branch of ``edit``, so a loop they return is held to the same
    rule: a compact id in a sentence and an id of a soft-deleted source under ``evidence`` are withheld.

    Mutation: return the updated row instead of the checked copy in ``_handle_alice_open_loops``.
    """

    deleted = world.sources["deleted"]
    confidential = world.sources["confidential"]
    world._plant(
        "update_probe",
        metadata={
            "project_scope": ["alpha"],
            "evidence": {"quote_from": deleted, "note": f"from {UUID(confidential).hex} today"},
        },
    )
    arguments: dict[str, object] = {"action": action, "loop_id": world.loops["update_probe"]}
    if action == "snooze":
        arguments["due_at"] = "2030-01-01T00:00:00Z"
    answer = world.vault.wire("alice_open_loops", arguments, key=world.vault.keys["alpha_project"])
    assert answer["is_error"] is False, answer
    item = answer["payload"]["open_loop"]  # type: ignore[index]
    assert item["metadata_json"] == {"project_scope": ["alpha"], "evidence": {"note": f"from {_CUT} today"}}


def test_two_loops_of_one_context_pack_and_one_list_are_one_response_on_the_real_surfaces(world: _World) -> None:
    """Loop A links an id that names no row in its column, and loop B repeats it under ``evidence.quote_from``. In the
    list and in the context pack both are in the response, so B loses the id. Read alone through ``edit`` or the review
    route, B is a response of its own that links nothing, so it keeps an id that names no row: the per-response rule,
    stated in the docs.

    Mutation: build the withheld ids per loop in ``withhold_unreadable_references`` (B keeps the id in the list and the
    pack).
    """

    ghost = str(uuid4())
    world._plant("link_loop", source_value=ghost)
    world._plant("quote_loop", metadata={"project_scope": ["alpha"], "evidence": {"quote_from": ghost}})
    listed = {row["id"]: row for row in world.list_items("alpha_project")}
    assert listed[world.loops["link_loop"]]["source_id"] is None
    assert listed[world.loops["quote_loop"]]["metadata_json"] == {"project_scope": ["alpha"], "evidence": {}}
    status, pack = world.pack_route("alpha_project")
    assert status == 201
    in_pack = {row["id"]: row for row in pack["open_loops"]}  # type: ignore[attr-defined]
    assert {world.loops["link_loop"], world.loops["quote_loop"]} <= set(in_pack)
    assert in_pack[world.loops["quote_loop"]]["metadata_json"] == {"project_scope": ["alpha"], "evidence": {}}
    for surface in ("mcp_edit", "http_review"):
        alone = _read(world, surface, "quote_loop")
        assert alone["metadata_json"]["evidence"] == {"quote_from": ghost}, surface  # type: ignore[index]


@pytest.mark.parametrize("reader", ["alpha_trusted", "alpha_project", "alpha_admin", "alpha_read_only", "owner"])
def test_each_reader_is_shown_in_every_spelling_exactly_the_ids_its_own_fence_admits(world: _World, reader: str) -> None:
    """One loop per kind of source and of memory in the vault (own, health, confidential, another project's, global,
    deleted), each repeating its row's id in six spellings under keys that name no reference, with nothing linking it.
    Each reader is shown the loop as stored where its fence admits the row (the trusted key reads health, the admin key
    reads health and confidential, the project and read-only keys read only their own project, the owner reads every
    live row) and none of the six spellings where it does not, a deleted row included.

    This is the control that keeps the deleted-aware lookup and the spelling scan from over-withholding: the readable
    ids are all still there. Mutations: withhold ids that are admitted (as in the admitted-id tests), or skip the
    lookup for the owner.
    """

    def forms(identifier: str) -> tuple[dict[str, object], dict[str, object]]:
        compact = identifier.replace("-", "")
        stored: dict[str, object] = {
            "upper": identifier.upper(),
            "compact": compact,
            "odd": _odd(compact),
            "braces": "{" + compact + "}",
            "sentence": f"copied from urn:uuid:{identifier} today",
            "url": f"https://example.test/notes/{compact.upper()}",
        }
        shown: dict[str, object] = {
            "sentence": f"copied from urn:uuid:{_CUT} today",
            "url": f"https://example.test/notes/{_CUT}",
        }
        return stored, shown

    kinds = ("own", "health", "confidential", "beta", "global", "deleted")
    readable = {"owner": {"own", "health", "confidential", "beta", "global"}}.get(reader) or {
        "alpha_trusted": {"own", "health"},
        "alpha_project": {"own"},
        "alpha_read_only": {"own"},
        "alpha_admin": {"own", "health", "confidential"},
    }[reader]
    expected: dict[str, dict[str, object]] = {}
    for kind in kinds:
        for noun, ids in (("source", world.sources), ("memory", world.memories)):
            stored, shown = forms(ids[kind])
            name = f"{noun}_{kind}"
            world._plant(name, metadata={"project_scope": ["alpha"], "evidence": stored})
            expected[world.loops[name]] = {"project_scope": ["alpha"], "evidence": stored if kind in readable else shown}
    if reader == "owner":
        answer = world.vault.wire("alice_open_loops", {"status": "all", "limit": 100}, key=None)
        assert answer["is_error"] is False, answer
        items = answer["payload"]["items"]  # type: ignore[index]
    else:
        items = world.list_items(reader)
    by_id = {str(item["id"]): item for item in items}
    for loop_id, metadata in expected.items():
        assert by_id[loop_id]["metadata_json"] == metadata, (reader, loop_id)


def test_the_owner_is_shown_no_id_of_a_deleted_row_in_any_spelling(world: _World) -> None:
    """The owner's fence refuses a deleted row, so the owner is shown no id of one under an unknown key either, in the
    compact spelling too. The owner is shown a live row's id (the own source here) as stored.

    Mutation: skip the lookup for the owner (``fence.identity is None``).
    """

    deleted, own = world.sources["deleted"], world.sources["own"]
    world._plant(
        "owner_probe",
        metadata={
            "project_scope": ["alpha"],
            "evidence": {"quote_from": UUID(deleted).hex, "own": own, "note": f"see {deleted.upper()}"},
        },
    )
    answer = world.vault.wire("alice_open_loops", {"status": "all", "limit": 100}, key=None)
    assert answer["is_error"] is False, answer
    item = next(row for row in answer["payload"]["items"] if row["id"] == world.loops["owner_probe"])  # type: ignore[index]
    assert item["metadata_json"] == {"project_scope": ["alpha"], "evidence": {"own": own, "note": f"see {_CUT}"}}


# -- 7. the words -------------------------------------------------------------------------------------------------


def test_the_docs_state_the_spellings_the_per_response_rule_and_the_residual() -> None:
    """The CHANGELOG entry of the read fence is amended in place (one entry, not two) and describes the end behaviour, and
    ``mcp-tools.md`` and ``known-limitations.md`` carry the rule: every spelling, a deleted row withheld, the
    per-response collection, the hex boundary, and the two things that remain (the free-text columns, and an id of a row
    removed outright that nothing links).

    Mutation: delete any one of the sentences below from the file that carries it.
    """

    def squashed(path: str) -> str:
        return " ".join((_ROOT / path).read_text(encoding="utf-8").split())

    changelog = squashed("CHANGELOG.md")
    tools = squashed("docs/alpha/mcp-tools.md")
    limitations = squashed("docs/alpha/known-limitations.md")
    assert changelog.count("- An open loop no longer shows its reader the id of a source or memory the reader may not read.") == 1
    for sentence in (
        "A deleted source or memory is a row the reader may not read: the lookup reads soft-deleted rows too",
        "The withheld ids are collected over every loop of one response",
        "A run of 32 hex digits counts as an id only when no hex digit stands next to it",
        "An id without hyphens inside a URL or a sentence is withheld like the hyphenated one.",
        "and the free-text columns of a loop (`title`, `description`, `resolution_note`), which are returned as stored and are not scanned for ids.",
    ):
        assert changelog.count(sentence) == 1, sentence
    for sentence in (
        "The rule for ids inside `metadata_json`.",
        "The withheld ids are collected over every loop of one response",
        "A run of 32 hex digits is an id only when no hex digit stands next to it",
        "The free-text columns of a loop (`title`, `description`, `resolution_note`) are returned as stored and are not scanned.",
    ):
        assert tools.count(sentence) == 1, sentence
    for sentence in (
        "in every spelling and after the source or memory is deleted",
        "the id of a row that was removed outright, under a key that names no reference and linked nowhere in the response, reads like a trace id and is kept.",
    ):
        assert limitations.count(sentence) == 1, sentence


def test_the_module_docstring_states_the_rule_it_implements() -> None:
    """The docstring of ``vnext_open_loop_references`` is the rule's statement and names the three limits: the spellings
    it reads, the per-response collection, and the removed-outright residual. A change to the behaviour must change it.

    Mutation: delete any one of the phrases below from the docstring.
    """

    from alicebot_api import vnext_open_loop_references as module

    text = " ".join(str(module.__doc__).split())
    for phrase in (
        "The withheld ids are collected for the whole call, over every row it returns",
        "A deleted row is refused, not unknown.",
        "The one case left is a row that was removed outright",
        "Any other run of 32 hex digits (with no hyphen, or with hyphens in other places) is an id only when no hex digit stands next to it",
        "Inside longer text only ASCII hex digits are read",
    ):
        assert phrase in text, phrase
