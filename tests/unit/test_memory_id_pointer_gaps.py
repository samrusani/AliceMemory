"""Pins for the memory id pointer fence that no test failed on, and two small fixes.

A mutation sweep of the pointer residue change (``vnext_retrieval.py``: the walk to
a current fact, the pointer fence helper and the scan of ``metadata_json`` for
ids) found branches that could be changed with every test still passing. This
module pins each of them. It also pins two behaviours the sweep did not cover:

* the walk's cycle guard names no id. A chain that loops back to a row it has
  already read is made of superseded rows, so none of them is current. The guard
  used to return the id of the row it had come back to, which the docstring of
  ``_current_memory_id`` says a walk never does.
* a successor closed with ``alice_memory_manage`` expire (the status stays
  ``active`` and ``valid_to`` is set) is not named as ``current_memory_id``,
  because recall and the pack leave such a row out.

The pack's scan of ``metadata_json`` runs under the pack's fence, and the last three
tests pin that the person link, the people scope and the time window of that fence are
the ones the pack was asked for. A mutation that changed only the scan's predicate
(M1 no person links, M2 no time window, M4 no people) passed the whole unit suite
before they were added, because the pack's other readers share the predicate and fail
on the changes that reach them.

Every test names the mutation that must fail it. The mutations were made by hand
in ``vnext_retrieval.py`` and the file was restored from a saved copy each time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.test_memory_id_pointer_residue import (
    ALL_LEVELS,
    NEW,
    OLD,
    _call,
    _captured,
    _context,
    _MemoryStore,
    _mint,
    _pack,
    _pack_memory,
    _recall_source,
    _pack_source,
    _row,
    _Rows,
    _set,
    _store,
    _supersede,
)

HIDDEN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECOND_HIDDEN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SHOWN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEAD = "00000000-0000-4000-8000-00000000dead"


# -- the fence helper records what it dropped -------------------------------------


def test_the_helper_records_an_unresolved_pointer_it_dropped_on_a_scoped_read() -> None:
    """A pointer to no row is dropped on a scoped read, and the holder is reported.

    The caller uses the report to keep ``validity.superseded`` true when the
    pointer is gone. Without the report a scoped read of a row whose pointer
    names no row loses the flag, because the pointer was the only thing that
    said the row was superseded. On a read that is not scoped the pointer stays
    and nothing is reported.

    Mutation: delete ``dropped.append((holder, pointer_key))`` in the
    ``target is None`` branch of ``_drop_pointers_outside_fence``.
    """
    from alicebot_api.vnext_retrieval import _drop_pointers_outside_fence

    holder: dict[str, object] = {"id": "m1", "superseded_by": DEAD}
    dropped = _drop_pointers_outside_fence(
        [holder],
        ("supersedes", "superseded_by"),
        targets={},
        memory_visible=lambda _row: True,
        fail_closed_when_unresolved=True,
    )
    assert dropped == [(holder, "superseded_by")]
    assert "superseded_by" not in holder

    kept: dict[str, object] = {"id": "m2", "superseded_by": DEAD}
    assert (
        _drop_pointers_outside_fence(
            [kept],
            ("superseded_by",),
            targets={},
            memory_visible=lambda _row: True,
            fail_closed_when_unresolved=False,
        )
        == []
    )
    assert kept["superseded_by"] == DEAD


def test_a_scoped_pack_keeps_superseded_true_for_a_pointer_to_no_row(tmp_path: Path, monkeypatch) -> None:
    """The same case end to end. A project-scoped key fails closed on a pointer to no row.

    The pointer and its id are gone from the pack. The row is still superseded, so
    ``validity.superseded`` stays true and carries no id, as it does in recall.

    Mutation: delete ``dropped.append((holder, pointer_key))`` in the
    ``target is None`` branch of ``_drop_pointers_outside_fence``. The pack then
    has no ``validity`` for the row.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    _set(context, memory_id, status="active", superseded_by=DEAD)
    reader = _mint(context, profile="read_only_agent", project="acme")

    pack = _call(context, "alice_context_pack", {"query": OLD}, key=reader, full=True)
    packed = next(row for row in pack["memories"] if row["id"] == memory_id)
    assert "superseded_by" not in packed
    assert packed["validity"]["superseded"] is True
    assert "superseded_by_memory_id" not in packed["validity"]
    assert DEAD not in json.dumps(pack)


# -- only a dropped superseded_by pointer makes a row superseded -------------------


def test_a_dropped_supersedes_pointer_does_not_make_the_row_superseded(tmp_path: Path, monkeypatch) -> None:
    """The replacement is current. Its pointer to a hidden predecessor goes, and nothing else changes.

    ``supersedes`` is the replacement's pointer to the row it replaced. Dropping it
    must not mark the replacement as superseded: it is the live fact. A row that
    never had a pointer keeps no ``validity`` either.

    Mutation: report the holder for every pointer key that was dropped, that is
    change ``pointer_key == "superseded_by" and holder.get("id")`` in
    ``_sanitize_memory_scope_pointers`` to ``holder.get("id")``. Or pass
    ``superseded_pointer_withheld=True`` for every row at the
    ``_validity_annotation`` call in ``compile_context_pack``.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    _set(context, memory_id, sensitivity="confidential")

    pack = _pack(context, NEW)
    packed = _pack_memory(pack, replacement)
    assert "supersedes" not in packed, "the pointer to the hidden predecessor was dropped"
    assert "superseded" not in (packed.get("validity") or {})
    assert memory_id not in json.dumps(pack, default=str)

    owner = _pack_memory(_pack(context, NEW, sensitivity_allowed=ALL_LEVELS), replacement)
    assert owner["supersedes"] == memory_id, "the pointer is there to be dropped"
    assert "superseded" not in (owner.get("validity") or {})


# -- the walk to a current fact ----------------------------------------------------


def _chain(length: int, last: dict[str, object]) -> _Rows:
    """``n0`` to ``n{length - 1}`` superseded in turn, then ``last`` as the row after them."""
    rows = [_row(f"n{index}", "superseded", f"n{index + 1}") for index in range(length)]
    return _Rows(*rows, last)


def test_the_walk_after_the_depth_cap_names_no_hidden_row() -> None:
    """Nine rows, eight hops. The ninth is live but the caller may not read it.

    The loop checks every row it steps over. The row it is holding when the cap
    ends the loop has not been checked, so the check after the loop has to ask.

    Mutation: delete ``not memory_visible(current)`` from the check after the loop
    in ``_current_memory_id``.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    store = _chain(8, _row("n8", "active"))
    assert _current_memory_id(store, store.rows["n0"], memory_visible=lambda row: True) == "n8"
    assert _current_memory_id(store, store.rows["n0"], memory_visible=lambda row: row["id"] != "n8") == ""


def test_the_walk_after_the_depth_cap_names_nothing_when_the_chain_goes_on() -> None:
    """Nine rows, eight hops, and the ninth is itself superseded: the chain is longer than the walk.

    The current fact is not known, so no id is named, and the last row read is not
    named in its place.

    Mutation: delete ``_memory_is_superseded(current)`` from the check after the
    loop in ``_current_memory_id``.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    store = _chain(8, _row("n8", "active", "n9"))
    assert _current_memory_id(store, store.rows["n0"], memory_visible=lambda row: True) == ""
    ended = _chain(8, _row("n8", "active"))
    assert _current_memory_id(ended, ended.rows["n0"], memory_visible=lambda row: True) == "n8"


def test_the_walk_names_nothing_when_the_store_cannot_look_a_successor_up() -> None:
    """A store with no ``get_memory`` cannot resolve the pointer, and a pointer id is not named unresolved.

    Mutation: return the id of the row the walk holds in place of ``""`` when
    ``getattr(store, "get_memory", None)`` is not callable.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    class _NoLookup:
        pass

    start = _row("a", "superseded", "b")
    assert _current_memory_id(_NoLookup(), start, memory_visible=lambda row: True) == ""


def test_a_cycle_in_the_chain_names_no_id() -> None:
    """a is superseded by b and b by a. Every row on the loop is superseded, so none is current.

    The walk used to return the id of the row it came back to, which is a
    superseded row and the one thing the walk is documented never to name.

    Mutation: return ``memory_id`` in place of ``""`` in the
    ``memory_id == "" or memory_id in seen`` guard of ``_current_memory_id``.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    store = _Rows(_row("a", "superseded", "b"), _row("b", "superseded", "a"))
    assert _current_memory_id(store, store.rows["a"], memory_visible=lambda row: True) == ""
    assert _current_memory_id(store, store.rows["b"], memory_visible=lambda row: True) == ""

    selfish = _Rows(_row("s", "superseded", "s"))
    assert _current_memory_id(selfish, selfish.rows["s"], memory_visible=lambda row: True) == ""

    live = _Rows(_row("a", "superseded", "b"), _row("b", "active"))
    assert _current_memory_id(live, live.rows["a"], memory_visible=lambda row: True) == "b"


# -- a successor whose validity window closed ---------------------------------------


def _expired(days: int = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def test_the_walk_does_not_name_a_row_whose_validity_window_closed() -> None:
    """``valid_to`` in the past ends the walk without an id. A window still open, or none, names the row.

    The far-future stand-in for no expiry is not in the past. The same rule holds
    for the row the walk starts on when it was corrected in place, and for the row
    held when the depth cap ends the walk.

    Mutation: delete the ``_memory_validity_has_closed`` check in
    ``_current_memory_id``, in the not-superseded branch or in the check after the
    loop. Or make it compare ``valid_to > now``. Or read the real clock and ignore
    the ``now`` that was passed.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    # A fixed clock well before today, so a window that is open at ``now`` is already
    # closed at the real time and a walk that ignored ``now`` would show it.
    now = datetime(2020, 6, 1, 12, 0, tzinfo=UTC)

    def visible(_row: object) -> bool:
        return True

    def end_row(**fields: object) -> _Rows:
        return _Rows(_row("a", "superseded", "b"), {**_row("b", "active"), **fields})

    closed = end_row(valid_to="2020-05-01T12:00:00Z")
    assert _current_memory_id(closed, closed.rows["a"], memory_visible=visible, now=now) == ""
    closed_aware = end_row(valid_to=datetime(2020, 5, 1, tzinfo=UTC))
    assert _current_memory_id(closed_aware, closed_aware.rows["a"], memory_visible=visible, now=now) == ""
    closed_naive = end_row(valid_to=datetime(2020, 5, 1))
    assert _current_memory_id(closed_naive, closed_naive.rows["a"], memory_visible=visible, now=now) == ""
    for fields in (
        {"valid_to": "2020-07-01T12:00:00Z"},
        {"valid_to": "9999-12-31T23:59:59Z"},
        {"valid_to": None},
        {},
    ):
        open_window = end_row(**fields)
        assert _current_memory_id(open_window, open_window.rows["a"], memory_visible=visible, now=now) == "b", fields

    in_place = _Rows({**_row("a", "active"), "valid_to": "2020-05-01T12:00:00Z"})
    assert _current_memory_id(in_place, in_place.rows["a"], memory_visible=visible, now=now) == ""

    after_cap = _chain(8, {**_row("n8", "active"), "valid_to": "2020-05-01T12:00:00Z"})
    assert _current_memory_id(after_cap, after_cap.rows["n0"], memory_visible=visible, now=now) == ""
    after_cap_open = _chain(8, {**_row("n8", "active"), "valid_to": "2020-07-01T12:00:00Z"})
    assert _current_memory_id(after_cap_open, after_cap_open.rows["n0"], memory_visible=visible, now=now) == "n8"


def test_the_walk_reads_the_validity_window_against_the_current_time_by_default() -> None:
    """With no ``now`` given the clock is the present, so yesterday is closed and tomorrow is open.

    Mutation: default ``now`` to a fixed past time, or drop the default so the
    clock is never read.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    closed = _Rows(_row("a", "superseded", "b"), {**_row("b", "active"), "valid_to": _expired()})
    assert _current_memory_id(closed, closed.rows["a"], memory_visible=lambda row: True) == ""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    open_window = _Rows(_row("a", "superseded", "b"), {**_row("b", "active"), "valid_to": tomorrow})
    assert _current_memory_id(open_window, open_window.rows["a"], memory_visible=lambda row: True) == "b"


def test_an_expired_successor_is_not_named_when_recall_and_the_pack_leave_it_out(
    tmp_path: Path, monkeypatch
) -> None:
    """``alice_memory_manage`` expire closes the window and leaves the status ``active``.

    The first assertions show the premise: the successor is named while it is open,
    and after expiry neither recall nor the pack returns it. The label then names no
    id and still says the passage was corrected. Un-expiring it brings the id back.

    Mutation: delete the ``_memory_validity_has_closed`` check in
    ``_current_memory_id``. The label then names a row that no search returns.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    for source in (_recall_source(context), _pack_source(context)):
        assert source["current_memory_id"] == replacement

    _call(
        context,
        "alice_memory_manage",
        {
            "action": "expire",
            "memory_id": replacement,
            "reason": "no longer true",
            "valid_to": _expired(),
        },
        full=True,
    )

    recall = _call(context, "alice_recall", {"query": NEW})
    assert replacement not in json.dumps(recall.get("results"))
    pack = _call(context, "alice_context_pack", {"query": NEW}, full=True)
    assert replacement not in json.dumps(pack.get("memories"))
    for source in (_recall_source(context), _pack_source(context)):
        assert source["derived_memory_corrected"] is True
        assert "current_memory_id" not in source
        assert replacement not in json.dumps(source)

    _call(
        context,
        "alice_memory_manage",
        {"action": "unexpire", "memory_id": replacement, "reason": "still true"},
        full=True,
    )
    for source in (_recall_source(context), _pack_source(context)):
        assert source["current_memory_id"] == replacement


# -- the scan of metadata_json for ids -----------------------------------------------


def _scan(rows: dict[str, dict], memories: list[dict], *, visible_ids: frozenset[str] = frozenset({SHOWN})) -> _MemoryStore:
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    store = _MemoryStore(rows)
    VNextRetrievalService(store)._drop_hidden_memory_ids_from_metadata(  # type: ignore[arg-type]
        memories, memory_visible=lambda row: row["id"] in visible_ids
    )
    return store


def _two_rows() -> dict[str, dict]:
    return {
        SHOWN: {"id": SHOWN},
        HIDDEN: {"id": HIDDEN},
        SECOND_HIDDEN: {"id": SECOND_HIDDEN},
    }


def test_a_hidden_id_that_is_only_a_key_is_looked_up_and_removed() -> None:
    """The id appears nowhere as a value, so the key is the only thing that gives it away.

    Mutation: delete the loop body that collects ids from a mapping's keys in
    ``_collect_memory_id_texts``. The id is then never looked up, and a key that
    names a memory the caller may not read stays.
    """
    memories = [{"id": "m1", "metadata_json": {HIDDEN: "a note about it", "kept": "yes", SHOWN: "visible"}}]
    _scan(_two_rows(), memories)
    assert memories[0]["metadata_json"] == {"kept": "yes", SHOWN: "visible"}


def test_an_id_in_another_case_is_found_and_removed_wherever_it_is() -> None:
    """A memory id is a UUID, and one written in capitals is the same id.

    The lookup is by the lower-case form, so the scan has to fold what it finds, in
    a value, in a key and inside a longer string.

    Mutation: delete ``.lower()`` where ``_collect_memory_id_texts`` collects from a
    string value, or where it collects from a key, or in the replacement inside a
    longer string in ``_scrub_memory_id_text``.
    """
    memories = [
        {"id": "m1", "metadata_json": {"value": HIDDEN.upper()}},
        {"id": "m2", "metadata_json": {HIDDEN.upper(): "keyed"}},
        {"id": "m3", "metadata_json": {"note": f"see {HIDDEN.upper()} for it"}},
    ]
    _scan(_two_rows(), memories)
    assert memories[0]["metadata_json"] == {}
    assert memories[1]["metadata_json"] == {}
    assert memories[2]["metadata_json"] == {"note": "see (id withheld) for it"}


def test_a_string_with_a_hidden_id_and_a_readable_one_loses_only_the_hidden_id() -> None:
    """Inside a longer string the readable id stays and the hidden one is replaced.

    Mutation: replace every UUID-shaped match in the string, that is change
    ``match.group(0).lower() in hidden`` in the replacement to ``True``.
    """
    memories = [{"id": "m1", "metadata_json": {"note": f"{SHOWN} replaced {HIDDEN} and {SHOWN.upper()}"}}]
    _scan(_two_rows(), memories)
    assert memories[0]["metadata_json"] == {"note": f"{SHOWN} replaced (id withheld) and {SHOWN.upper()}"}


def test_a_hidden_id_padded_with_white_space_is_removed_whole() -> None:
    """A string that is only the id and some white space is the id: its key goes with it.

    Mutation: match the whole-id pattern against ``text`` and not ``text.strip()``
    in ``_scrub_memory_id_text``. The key then stays, holding ``" (id withheld) "``.
    """
    memories = [{"id": "m1", "metadata_json": {"padded": f"  {HIDDEN}\n", "kept": "yes"}}]
    _scan(_two_rows(), memories)
    assert memories[0]["metadata_json"] == {"kept": "yes"}


def test_metadata_that_is_only_a_hidden_id_becomes_an_empty_mapping() -> None:
    """The whole value is the id, so nothing is left and the column is an empty mapping.

    Mutation: store the scrub result as it is when it is the dropped marker, that
    is delete the ``{} if scrubbed is _SCRUB_DROPPED`` branch. The marker object,
    which is not JSON, would then be returned in the row.
    """
    memories = [{"id": "m1", "metadata_json": HIDDEN}]
    _scan(_two_rows(), memories)
    assert memories[0]["metadata_json"] == {}


def test_every_packed_row_is_checked_against_one_lookup_of_all_the_ids() -> None:
    """Two rows name two different hidden ids. Both go, and the store is asked once.

    Mutation: build the lookup from the first row that has ids and not from every
    row. The second row's id is then never looked up and stays.
    """
    memories = [
        {"id": "m1", "metadata_json": {"link": HIDDEN}},
        {"id": "m2", "metadata_json": {"link": SECOND_HIDDEN, "ok": SHOWN}},
    ]
    store = _scan(_two_rows(), memories)
    assert store.asked == [sorted([SHOWN, HIDDEN, SECOND_HIDDEN])]
    assert memories[0]["metadata_json"] == {}
    assert memories[1]["metadata_json"] == {"ok": SHOWN}


def _nested(levels: int, leaf: object) -> object:
    node: object = leaf
    for _ in range(levels):
        node = {"down": node}
    return node


@pytest.mark.parametrize("shape", ["mapping", "list"])
def test_a_deep_value_is_cut_even_when_a_later_sibling_is_shallow(shape: str) -> None:
    """The scan reports "too deep" for the whole value if any one branch was.

    The deep branch comes first and a plain one comes after it. The id sits below
    the depth the scan reads, so only the cut removes it. A flag that the last
    sibling overwrites would see the plain one, report nothing, and leave the id.

    Mutation: delete ``or too_deep`` where ``_collect_memory_id_texts`` combines the
    flag over the values of a mapping (the mapping case), or over the items of a
    list (the list case).
    """
    deep = _nested(80, {"leaf": HIDDEN})
    metadata = {"holder": {"deep": deep, "plain": "text"}} if shape == "mapping" else {"holder": [deep, "text"]}
    memories = [{"id": "m1", "metadata_json": metadata}]
    _scan(_two_rows(), memories)
    assert HIDDEN not in json.dumps(memories[0]["metadata_json"])
    holder = memories[0]["metadata_json"]["holder"]
    if shape == "mapping":
        assert holder["plain"] == "text"
    else:
        assert "text" in holder


# -- the pack's metadata scan carries the person link, the people scope and the time window -----


def _commit_memory(context, key: str, title: str, text: str) -> str:
    done = _call(
        context,
        "alice_memory_commit",
        {"title": title, "canonical_text": text, "memory_type": "decision", "domain": "project", "sensitivity": "public"},
        key=key,
        full=True,
    )
    return str(done["memory"]["id"])


def _link_to_person(context, memory_id: str, entity_id: str) -> None:
    """Tie a memory to a person through the entity graph only, with nothing in its metadata."""
    _store(
        context,
        lambda s: s.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": memory_id,
                "to_type": "entity",
                "to_id": entity_id,
                "edge_type": "mentions",
                "confidence": 1.0,
                "explanation": "test link",
                "created_by": "test",
            }
        ),
    )


def _holder_with_three_neighbours(context) -> tuple[str, str, str, str]:
    """A packed decision that names three other memories in its ``metadata_json``.

    The holder names Ada in its own metadata, so a people-scoped pack returns it.
    ``linked`` is tied to Ada only through a graph edge. ``stranger`` is not tied to
    her at all. ``old`` is tied to her through a graph edge and is dated 400 days ago.
    All three are public, so only the person and the time half of the fence can
    tell them apart.
    """
    writer = _mint(context, profile="project_scoped_agent", project="acme")
    holder = _commit_memory(context, writer, "Holder note", "The holder note says the deploy window is Tuesday.")
    linked = _commit_memory(context, writer, "Linked note", "The linked note covers the staging cluster.")
    stranger = _commit_memory(context, writer, "Stranger note", "The stranger note covers the billing export.")
    old = _commit_memory(context, writer, "Old note", "The old note covers last year's rollout.")
    person = _store(context, lambda s: s.create_entity({"entity_type": "person", "name": "Ada"}))
    _link_to_person(context, linked, str(person["id"]))
    _link_to_person(context, old, str(person["id"]))
    _set(context, old, valid_from=(datetime.now(UTC) - timedelta(days=400)).isoformat())
    _set(
        context,
        holder,
        metadata_json={
            **_store(context, lambda s: s.get_memory(holder))["metadata_json"],
            "people": ["ada"],
            "related_linked": linked,
            "related_stranger": stranger,
            "related_old": old,
        },
    )
    return holder, linked, stranger, old


def _holder_metadata(pack: dict, holder: str) -> dict:
    return _pack_memory(pack, holder)["metadata_json"]


def test_the_metadata_scan_keeps_an_id_tied_to_the_person_through_the_graph_and_drops_a_stranger(
    tmp_path: Path, monkeypatch
) -> None:
    """The people scope reaches the id scan in both directions, through the entity graph.

    ``linked`` is readable under a people scope only because the graph ties it to Ada,
    and ``stranger`` is public but not tied to her. The id of the first stays in the
    holder's metadata and the id of the second goes. With no people scope both stay.

    Mutation M1: build the predicate for the ``_drop_hidden_memory_ids_from_metadata``
    call in ``compile_context_pack`` with ``person_linked_memory_ids=frozenset()``.
    The id of ``linked`` is then dropped, because no direct people value names Ada.
    Mutation M4: build that predicate with the scope replaced by one that has
    ``people=frozenset()``. The id of ``stranger`` then stays.
    """
    context = _context(tmp_path, monkeypatch)
    holder, linked, stranger, _old = _holder_with_three_neighbours(context)

    scoped = _pack(context, "holder note deploy window Tuesday", people=("ada",))
    metadata = _holder_metadata(scoped, holder)
    assert metadata.get("related_linked") == linked, "a person link made through the entity graph keeps the id"
    assert "related_stranger" not in metadata, "a memory that is not tied to the person loses its id"

    open_fence = _holder_metadata(_pack(context, "holder note deploy window Tuesday"), holder)
    assert open_fence["related_linked"] == linked
    assert open_fence["related_stranger"] == stranger, "the control: with no people scope the id is kept"


def test_the_metadata_scan_follows_the_time_window(tmp_path: Path, monkeypatch) -> None:
    """An id that names a memory dated outside the window goes, and one inside it stays.

    ``old`` is public and dated 400 days ago. A 30 day window does not reach it, so its
    id is dropped from the holder's metadata and the ids of the memories made today
    stay. With no window all of them stay.

    Mutation M2: build the predicate for the ``_drop_hidden_memory_ids_from_metadata``
    call in ``compile_context_pack`` with the scope replaced by one that has
    ``window_start=None`` and ``window_end=None``. The id of ``old`` then stays.
    """
    context = _context(tmp_path, monkeypatch)
    holder, linked, stranger, old = _holder_with_three_neighbours(context)

    windowed = _pack(context, "holder note deploy window Tuesday", time_window="30d")
    metadata = _holder_metadata(windowed, holder)
    assert "related_old" not in metadata, "an id outside the window is dropped"
    assert old not in json.dumps(metadata), "and nowhere else in the holder's metadata"
    assert metadata["related_linked"] == linked and metadata["related_stranger"] == stranger

    open_window = _holder_metadata(_pack(context, "holder note deploy window Tuesday"), holder)
    assert open_window["related_old"] == old, "the control: with no window the id is kept"


def test_the_metadata_scan_applies_the_person_link_and_the_window_together(tmp_path: Path, monkeypatch) -> None:
    """Both halves at once: the person link keeps ``linked`` and the window drops ``old``.

    ``old`` is tied to Ada through the graph, so the people scope alone keeps it, and
    only the window removes it. ``linked`` is inside the window and tied to her.
    ``stranger`` is inside the window and not tied to her.

    Mutation M1, M2 and M4 together: build the predicate for the metadata scan with no
    person links, no window and no people. Each one alone is also killed by the two
    tests above; this one fails if the combined scope is not what the scan runs under.
    """
    context = _context(tmp_path, monkeypatch)
    holder, linked, _stranger, _old = _holder_with_three_neighbours(context)

    pack = _pack(context, "holder note deploy window Tuesday", people=("ada",), time_window="30d")
    metadata = _holder_metadata(pack, holder)
    assert metadata["related_linked"] == linked
    assert "related_stranger" not in metadata
    assert "related_old" not in metadata
