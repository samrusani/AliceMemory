"""The entry beside a refused ref, a quote that carries an id, an escaped surrogate, and the rows that keep refs a writer chose.

Unreleased (on main, not in v0.20.0). The reader of saved quotes (``SavedProvenanceReader``) withholds the words of a
memory or a source the caller may not read from the ref that cites it. These tests hold it to five rules, with a stub store
whose reads are counted:

* an entry beside a refused ref that names no source and no memory the caller may read is part of the refused group: every
  string in it and every field whose name the product does not write is withheld, not only a ``quote`` (a bare string, a
  ``text`` field, a name made of words, a nested list, JSON text). An id that sits under another field (``chunk_id``), in a
  sentence or in a quote, or that names no stored row, names nothing, so adding one to an entry keeps no quote. An entry that
  names a stored source or memory the caller may read through a reference field keeps its text, because the reader cannot tell
  whose words they are, and loses every name the product does not write, because a name holds words whatever the entry names;
* a quote or an excerpt that is an object or a list is a structure and is read for ids like any other, and an id typed in the
  text of a quote or an excerpt (``memory:<id> words``) is incidental: the quote is withheld when it names a stored memory the
  caller may not read;
* a ref that is JSON text with an escaped lone surrogate is decoded, held to the rule and written again in ASCII, so the answer
  can be encoded;
* a source made by the agent-output ingest keeps the refs it was sent, in its metadata and in the raw payload inside it, and
  the reader holds them to the same rule (``sources``), as it holds the metadata of a quality rating and the sources and scope
  of a queued task (``fields``);
* the owner and an unbound admin key are given the rows themselves.

``tests/unit/test_saved_quote_memory_refs_vault.py`` runs the shapes on a real vault with real keys, and
``tests/integration/test_saved_quote_rows_postgres.py`` runs the rows on PostgreSQL through the mounted application. Each test
names the mutation that must fail it, and ``scripts/derived_label_mutations.json`` replays it.
"""

from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest

from alicebot_api.vnext_source_fence import (
    cited_memory_refs,
    cited_source_ids,
)
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from tests.unit.test_saved_provenance_reader import _identity
from tests.unit.test_saved_quote_memory_refs import (
    _REFUSED,
    _SENTINEL,
    _WORDS,
    _commit,
    _reader,
    _Store,
    _unbound,
)

_REFUSED_KINDS = [*_REFUSED, "missing"]


def _refused_memory(store: _Store, kind: str) -> str:
    return store.add_memory(**_REFUSED[kind]) if kind in _REFUSED else str(uuid4())  # type: ignore[arg-type]


def _reader_for(store: _Store, kind: str) -> SavedProvenanceReader:
    """The caller that may not read a memory of this kind: a key bound to the project, or a project scoped key for a domain it
    may not read."""

    return _reader(store, _identity("project_scoped_agent") if kind == "restricted domain" else None)


def _shown_refs(shown: dict[str, object]) -> list[object]:
    return shown["metadata_json"]["agentic_memory"]["source_refs"]  # type: ignore[index,return-value]


def _no_words(value: object) -> bool:
    return _SENTINEL not in json.dumps(value, default=str, ensure_ascii=False)


# -- 1. the entry beside a refused ref ---------------------------------------------------------------------------------

_COMPANIONS = {
    "a bare string": _WORDS,
    "a url with words": f"https://example.test/{_SENTINEL}",
    "a text field": {"text": _WORDS},
    "an excerpt field": {"excerpt": _WORDS},
    "a note field": {"note": _WORDS},
    "a quote key with a trailing space": {"quote ": _WORDS},
    "a name made of words": {_WORDS: 1},
    "a nested list of strings": [[_WORDS]],
    "a nested object": {"evidence": {"line": _WORDS}},
    "JSON text": json.dumps({"note": _WORDS}),
    "JSON text of a list": json.dumps([_WORDS]),
    "a quote and a page": {"quote": _WORDS, "page": 3},
    "a quote beside an id with no reference field": {"quote": _WORDS, "chunk_id": str(uuid4())},
    "a quote with an id of its own text": {"quote": f"{uuid4()} {_WORDS}"},
}


@pytest.mark.parametrize("kind", [*_REFUSED_KINDS, "refused source"])
def test_every_string_and_every_name_of_an_entry_beside_a_refused_ref_is_withheld(kind: str) -> None:
    """The commit that cites ``memory:<id>`` and then writes the words anywhere else in the entry beside it keeps them on main
    until this change, unless the field is exactly ``quote`` or ``conversation_excerpt``. Each shape of the table above, beside a
    ref that names a memory the caller may not read (every way a memory stops being readable, and a memory that is missing) and
    beside a ref that names a refused source, loses the words. The ref keeps its id, and an entry that is a reference alone
    stays.

    Mutations, each alone, in ``vnext_source_fence.py``: apply only the quote rule to the entry beside (``_withhold_entry_text``
    replaced by a function that nulls the keys ``quote`` and ``conversation_excerpt`` and nothing else, in the second branch of
    ``_without_refused_refs``); leave a bare string alone there.
    """

    store = _Store()
    if kind == "refused source":
        cited_ref: object = f"source:{store.add_source(sensitivity='confidential')}"
        expected_first: object = None
    else:
        refused = _refused_memory(store, kind)
        cited_ref = {"memory_id": refused, "quote": _WORDS}
        expected_first = {"memory_id": refused, "quote": None}
    reader = _reader_for(store, kind) if kind != "refused source" else _reader(store)
    for label, companion in _COMPANIONS.items():
        row = _commit(str(uuid4()), [cited_ref, companion, f"memory:{uuid4()}"], copy_kind="none")
        before = copy.deepcopy(row)
        shown = reader.memory(row)
        assert row == before, "the stored row is not changed by the read"
        refs = _shown_refs(shown)
        assert _no_words(shown), label
        assert refs[-1] == row["value"]["source_refs"][-1], (label, "a reference alone stays")  # type: ignore[index]
        if kind == "refused source":
            assert len(refs) == 2, (label, "the entry that names a refused source is dropped")
        else:
            assert len(refs) == 3 and refs[0] == expected_first, label


def test_an_entry_beside_a_refused_ref_keeps_its_text_only_when_it_names_a_readable_row() -> None:
    """The reader cannot tell whether the quote of an entry that names a stored source or memory belongs to it, so such an entry
    keeps its text. An id that is not in a reference field (under ``chunk_id``, in a sentence, in the text of a quote) names
    nothing, and neither does an id of a row the caller may not read or that does not exist, so a writer cannot keep a quote by
    adding an id to its entry.

    Mutations, each alone: count every id of the entry (``.every``) as naming, in the second branch of ``_without_refused_refs``
    (the ``chunk_id`` rows keep the words); count an id that names no stored row (``readable`` replaced by a set that holds every
    id); stop recording a readable source in ``_judge`` (the row that names a readable source loses its text).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    readable_memory = store.add_memory()
    readable_source = store.add_source()
    other_source = store.add_source()
    kept = [
        {"quote": "readable words", "source_id": readable_source},
        {"quote": "readable words", "memory_id": readable_memory},
        f"source:{readable_source}",
        f"memory:{readable_memory}",
        {"quote": "readable words", "sources": [readable_source]},
    ]
    withheld = [
        {"quote": _WORDS, "chunk_id": str(uuid4())},
        {"quote": _WORDS, "chunk_id": readable_source},
        {"quote": _WORDS, "page": 3, "origin": readable_memory},
        {"quote": f"seen in {readable_source} {_WORDS}"},
        {"quote": f"memory:{readable_memory} {_WORDS}"},
        f"see {readable_source} {_WORDS}",
        {"note": _WORDS, "meta": {"first": other_source}},
    ]
    row = _commit(str(uuid4()), [{"memory_id": refused, "quote": _WORDS}, *kept, *withheld], copy_kind="none")
    refs = _shown_refs(_reader(store).memory(row))
    assert refs[0] == {"memory_id": refused, "quote": None}
    assert refs[1 : 1 + len(kept)] == kept, "an entry that names a readable row keeps its text"
    assert len(refs) == 1 + len(kept) + len(withheld)
    assert _no_words(refs[1 + len(kept) :])


def test_an_entry_that_names_a_missing_or_refused_row_is_judged_as_that_row() -> None:
    """A named id that names no stored row counts as a missing row: an entry that names a missing source is dropped, one that
    names a missing memory loses its words and keeps its id. They never count as a readable row.

    Mutation: drop the ``named`` test and use ``every`` in the second branch of ``_without_refused_refs`` (a missing id then
    keeps nothing either, but the ``chunk_id`` rows of the test above keep their words).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    ghost_source, ghost_memory = str(uuid4()), str(uuid4())
    row = _commit(
        str(uuid4()),
        [
            {"memory_id": refused, "quote": _WORDS},
            {"source_id": ghost_source, "quote": _WORDS},
            {"memory_id": ghost_memory, "quote": _WORDS},
        ],
        copy_kind="none",
    )
    refs = _shown_refs(_reader(store).memory(row))
    assert refs == [{"memory_id": refused, "quote": None}, {"memory_id": ghost_memory, "quote": None}]


def _readable_row_companions(source: str, memory: str) -> dict[str, tuple[object, object]]:
    """Entries that name a stored row the caller may read (``source`` and ``memory``) and put words in the name of a field, each
    with the entry the caller is shown: the names the product writes stay with their values, every other name goes with its value."""

    return {
        "the id as the name": ({source: _WORDS}, {}),
        "the id behind a marker as the name": ({f"source:{source}": _WORDS}, {}),
        "words as a name beside a source id": ({"source_id": source, _WORDS: None}, {"source_id": source}),
        "words as a name beside a memory id": ({"memory_id": memory, _WORDS: None}, {"memory_id": memory}),
        "words as a name with a number": ({"source_id": source, _WORDS: 1}, {"source_id": source}),
        "words as a name with a string": ({"source_id": source, _WORDS: "x"}, {"source_id": source}),
        "a name made of words nested under a name the product writes": (
            {"sources": [{"source_id": source, _WORDS: 1}]},
            {"sources": [{"source_id": source}]},
        ),
        "a name nested under a name the product does not write": (
            {"source_id": source, "details": {_WORDS: None}},
            {"source_id": source},
        ),
        "a page and a note": ({"source_id": source, "page": 3, "note": "x"}, {"source_id": source}),
        "the product's names in any case": ({"Source_ID": source, _WORDS: None}, {"Source_ID": source}),
        "a name that is not a string": ({"source_id": source, 7: "x", None: _WORDS}, {"source_id": source}),
        "JSON text": (json.dumps({"source_id": source, _WORDS: 1}), json.dumps({"source_id": source})),
        "JSON text in JSON text": (
            json.dumps([json.dumps({"source_id": source, _WORDS: 1})]),
            json.dumps([json.dumps({"source_id": source})]),
        ),
    }


@pytest.mark.parametrize("kind", [*_REFUSED_KINDS, "refused source"])
def test_an_entry_that_names_a_readable_row_loses_the_names_the_product_does_not_write(kind: str) -> None:
    """An entry beside a refused ref that names a stored row the caller may read keeps its text, but a name holds words whatever
    the entry names, so every name the product does not write goes with its value, at any depth and inside JSON text, as it does
    from the entry that names the refused memory. The names the product writes stay.

    Mutations, each alone, in ``vnext_source_fence.py``: pass ``names_only=False`` for the entry that names a readable row (the
    words in a quote go, and the shapes below fail on the other side: the quote of the kept row below is lost); drop the rule on
    names in ``names_only`` mode (``lowered is None`` kept: every row keeps its words).
    """

    store = _Store()
    readable_source, readable_memory = store.add_source(), store.add_memory()
    if kind == "refused source":
        cited_ref: object = f"source:{store.add_source(sensitivity='confidential')}"
    else:
        cited_ref = {"memory_id": _refused_memory(store, kind), "quote": _WORDS}
    reader = _reader_for(store, kind) if kind != "refused source" else _reader(store)
    for label, (companion, expected) in _readable_row_companions(readable_source, readable_memory).items():
        row = _commit(str(uuid4()), [cited_ref, companion], copy_kind="none")
        before = copy.deepcopy(row)
        shown = reader.memory(row)
        assert row == before, (label, "the stored row is not changed by the read")
        refs = _shown_refs(shown)
        assert _no_words(shown), (kind, label)
        assert refs[-1] == expected, (kind, label)
    kept = {"source_id": readable_source, "quote": "words of the readable source"}
    same = _commit(str(uuid4()), [cited_ref, kept], copy_kind="none")
    assert _shown_refs(reader.memory(same))[-1] == kept, "an entry made of the names the product writes is shown as it was stored"


def test_the_words_under_a_name_the_product_writes_stay_in_an_entry_that_names_a_readable_row() -> None:
    """The limit the pages state. An entry that names a stored row the caller may read keeps the strings under the names the
    product writes: the reader cannot tell whether the ``quote`` of such an entry is the row's, and a string beside a ``source_id``
    is the writer's. A writer who attaches a readable source id to an entry keeps the text of that entry; the names it made up
    are gone. This pins the choice, so a change to it is a decision.

    Mutation: none; the test fails when the strings under a name the product writes are withheld from that entry.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    readable_source = store.add_source()
    entries = [
        {"source_id": readable_source, "quote": _WORDS},
        {"source_id": [readable_source, _WORDS]},
        {"sources": [{"source_id": readable_source, "quote": _WORDS}]},
    ]
    row = _commit(str(uuid4()), [{"memory_id": refused, "quote": _WORDS}, *entries], copy_kind="none")
    refs = _shown_refs(_reader(store).memory(row))
    assert refs[0] == {"memory_id": refused, "quote": None}
    assert refs[1:] == entries


def test_the_words_of_a_name_dropped_beside_a_readable_row_are_withheld_from_a_link_that_says_the_same() -> None:
    """The names an entry loses beside a refused ref join the words withheld from the links of the memory when the entry names a
    readable row as well: a link whose quote is the dropped name is shown without it.

    Mutation: make ``_note_dropped`` return without adding (the link keeps its quote).
    """

    store = _Store()
    source = store.add_source()
    refused = store.add_memory(sensitivity="confidential")
    memory_id = str(uuid4())
    row = _commit(memory_id, [{"memory_id": refused}, {"source_id": source, _WORDS: None}], copy_kind="none")
    named = store.add_link(memory_id, source, quote=_WORDS)
    other = store.add_link(memory_id, source, quote="a different quote")
    reader = _reader(store)
    reader.memory(row)
    assert [link["quote"] for link in reader.links(memory_id)] == [None, "a different quote"]
    assert named["quote"] == _WORDS and other["quote"] == "a different quote", "the stored links are not changed"


def test_a_companion_nested_past_the_bound_is_withheld_with_its_subtree() -> None:
    """The entry beside a refused ref is rebuilt by the same function as the entry that names it, which has a bound on depth, so
    a companion nested thousands of levels deep is withheld with the subtree that holds the words and does not raise.

    Mutation: raise the bound to 100,000 (``_SCRUB_DEPTH``): the nested row raises ``RecursionError``.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    for shape in ("dict", "list"):
        deep: object = {"quote": _WORDS}
        for _ in range(3000):
            deep = {"memories": deep} if shape == "dict" else [deep]
        row = _commit(str(uuid4()), [{"memory_id": refused}, deep], copy_kind="none")
        shown = _reader(store).memory(row)
        assert _no_words(json.dumps(shown, default=str)[:300_000]), shape


# -- 2. a quote or an excerpt that carries an id ----------------------------------------------------------------------


def _quote_shapes(m: str) -> dict[str, list[object]]:
    return {
        "quote is an object that holds the id": [{"quote": {"memory_id": m, "text": _WORDS}}],
        "excerpt is an object that holds the id": [{"conversation_excerpt": {"memory_id": m, "text": _WORDS}}],
        "quote is a list of objects that hold the id": [{"quote": [{"memory_id": m, "text": _WORDS}]}],
        "quote is a list of strings that name the id": [{"quote": [f"memory:{m}", _WORDS]}],
        "quote string with the marker in it": [{"quote": f"memory:{m} {_WORDS}"}],
        "quote string with the id in it": [{"quote": f"{_WORDS} (memory {m})"}],
        "quote string with an alice url in it": [{"quote": f"alice://memories/{m} {_WORDS}"}],
    }


@pytest.mark.parametrize("kind", list(_REFUSED))
def test_a_quote_that_carries_the_id_of_a_refused_memory_is_withheld(kind: str) -> None:
    """A quote or an excerpt can be an object or a list, and can have the id typed in its own text. On main until this change
    the reader skipped the whole value of a quote key, so none of these named the memory and the words stayed. A quote that is a
    structure is read for ids like any other value, and an id in the text of a quote or an excerpt is incidental: it takes the
    quote when it names a stored memory the caller may not read.

    Mutations, each alone, in ``vnext_source_fence.py``: skip the value of a quote key whatever it is (the ``isinstance`` test in
    ``cited_memory_refs``); drop the ``_every_id_in_text`` read of a quote string (the string rows keep the words).
    """

    store = _Store()
    refused = _refused_memory(store, kind)
    for label, refs in _quote_shapes(refused).items():
        row = _commit(str(uuid4()), refs, copy_kind="none")
        shown = _reader_for(store, kind).memory(row)
        assert _no_words(shown), (kind, label)


def test_a_quote_that_is_a_structure_naming_a_missing_memory_is_withheld_and_one_with_the_id_in_its_text_is_not() -> None:
    """An id in a reference position inside a quote that is a structure is named, so a memory that does not exist is refused as
    a missing memory is. An id in the text of a quote is incidental, and an incidental id that names no stored memory is left
    alone, because it may be a chunk or a session.

    Mutation: read the ids of a quote string as named (the last two rows lose their words).
    """

    store = _Store()
    ghost = str(uuid4())
    shapes = _quote_shapes(ghost)
    structures = [
        "quote is an object that holds the id",
        "excerpt is an object that holds the id",
        "quote is a list of objects that hold the id",
        "quote is a list of strings that name the id",
    ]
    for label in structures:
        row = _commit(str(uuid4()), shapes[label], copy_kind="none")
        assert _no_words(_reader(store).memory(row)), label
    for label in set(shapes) - set(structures):
        row = _commit(str(uuid4()), shapes[label], copy_kind="none")
        assert _reader(store).memory(row) is row, label


def test_an_excerpt_that_names_a_refused_memory_in_its_own_text_is_withheld_with_its_copies() -> None:
    """A commit with no ref whose excerpt says ``[memory:<id>] words`` saves the excerpt as ``agentic_memory.conversation_excerpt``
    and as the quote of no link. The excerpt is read for ids, and the memory it names takes the copy with it.

    Mutation: drop the read of the excerpt in ``_memory_ids_named_by_memory_copies`` (the copy stays).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    row = _commit(str(uuid4()), [], text=f"[memory:{refused}] {_WORDS}")
    shown = _reader(store).memory(row)
    assert _no_words(shown)
    assert "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"]  # type: ignore[index,operator]
    plain = _commit(str(uuid4()), [], text=_WORDS)
    assert _reader(store).memory(plain) is plain, "an excerpt that names nothing is returned as it was stored"


def test_a_quote_that_mentions_a_readable_or_unknown_row_keeps_its_words() -> None:
    """The ids in the text of a quote are looked up, and the quote is withheld only for a stored memory the caller may not
    read. A quote that mentions a readable memory, an id that names no row, or no id at all is shown as it was stored.

    Mutation: treat an id in the text of a quote as named (the quote that mentions an id that names no row loses its words).
    """

    store = _Store()
    readable = store.add_memory()
    for text in (f"seen in memory:{readable} {_WORDS}", f"{_WORDS} {uuid4()}", f"memory:{uuid4()} {_WORDS}", _WORDS):
        row = _commit(str(uuid4()), [{"memory_id": readable, "quote": text}], copy_kind="none")
        assert _reader(store).memory(row) is row, text


def test_ids_in_a_quote_that_is_a_structure_are_read_and_ids_in_quote_text_name_nothing_to_the_source_reader() -> None:
    """``cited_source_ids`` skips the text of a quote (it names no source) and reads a quote that is an object or a list as a
    structure. ``cited_memory_refs`` reads the same structure and adds the ids typed in quote text as incidental.

    Mutation: skip every value under a quote key in ``cited_source_ids`` (the structure rows find nothing).
    """

    a, m = str(uuid4()), str(uuid4())
    assert cited_source_ids({"quote": {"source_id": a}}).named == {a}
    assert cited_source_ids({"quote": [{"source_id": a}]}).named == {a}
    assert cited_source_ids({"quote": f"source:{a} words"}) == cited_source_ids(None)
    assert cited_memory_refs({"quote": {"memory_id": m}}).named == {m}
    assert cited_memory_refs({"quote": f"memory:{m} words"}).named == frozenset()
    assert cited_memory_refs({"quote": f"memory:{m} words"}).incidental == {m}
    assert cited_memory_refs({"conversation_excerpt": f"words {m}"}).incidental == {m}
    assert cited_memory_refs({"quote": "words"}).every == frozenset()


# -- 3. an escaped lone surrogate --------------------------------------------------------------------------------------


def _encodes(value: object) -> bool:
    """What a JSON response does with a value: write it with ``ensure_ascii=False`` and encode it as UTF-8."""

    json.dumps(value, ensure_ascii=False).encode("utf-8")
    return True


def test_a_ref_that_is_json_text_with_an_escaped_surrogate_gives_an_answer_that_can_be_encoded() -> None:
    """A ref string such as ``{"quote": "q", "note": "\\ud800"}`` is six ASCII characters in a string field, and decodes to a
    lone surrogate. The reader decoded it, withheld the quote, and wrote the text again with the surrogate in it, which a response
    cannot encode: the recent commits and audit routes failed with an exception for an unbound ``trusted_local_agent`` key,
    whether or not the cited id named a memory. An entry that is rebuilt keeps only the names the product writes and the
    references, so the surrogate is in a field that is dropped, in a key that is dropped or in a quote that is nulled, in every
    shape here, and the answer encodes; the owner and an unbound admin key read the text as it was stored.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    ghost = str(uuid4())
    poison = '{"quote": "q", "note": "\\ud800"}'
    for label, refs in {
        "entry that names a refused memory": [json.dumps({"memory_id": refused, "quote": _WORDS, "note": "x"}).replace('"x"', '"\\ud800"')],
        "a key": [json.dumps({"memory_id": refused, "k": 1}).replace('"k"', '"\\ud800k"')],
        "a quote": [json.dumps({"memory_id": refused, "quote": "x"}).replace('"x"', '"\\ud800"')],
        "the entry beside": [f"memory:{refused}", poison],
        "a memory that does not exist": [f"memory:{ghost}", poison],
        "a refused source": [f"source:{store.add_source(sensitivity='confidential')}", poison],
        "inside a nested JSON text": [json.dumps({"memory_id": refused, "inner": poison})],
    }.items():
        row = _commit(str(uuid4()), refs, copy_kind="none")
        for reader in (_reader(store), _reader(store, _unbound("trusted_local_agent"))):
            shown = reader.memory(row)
            assert _encodes(shown), label
            assert _encodes(reader.audit({"memory": row, "revisions": [], "events": [], "provenance_links": []})), label
        assert "\\ud800" in json.dumps(row), "the stored text is the escape and not a surrogate"
    owner = SavedProvenanceReader(store, fence=SourceReadFence.unfenced())
    assert owner.memory(row) is row


def test_a_ref_that_is_json_text_is_written_again_in_ascii() -> None:
    """The text of an entry that is rebuilt is written with ``ensure_ascii=True``, so a character in a kept reference list that a
    response could not carry (a lone surrogate, were one ever to stay) is written as an escape, and so is a separator such as
    U+2003 that a list of references keeps as written. The JSON the text holds is the same.

    Mutation: write the decoded value again with ``ensure_ascii=False`` in ``_json_text`` (``vnext_source_fence.py``).
    """

    store = _Store()
    refused, other = store.add_memory(sensitivity="confidential"), str(uuid4())
    ref = json.dumps({"memory_id": f"{refused}\u2003{other}", "quote": _WORDS}, ensure_ascii=False)
    assert "\u2003" in ref and not ref.isascii()
    row = _commit(str(uuid4()), [ref], copy_kind="none")
    shown = _reader(store).memory(row)
    text = _shown_refs(shown)[0]
    assert isinstance(text, str) and text.isascii()
    assert json.loads(text) == {"memory_id": f"{refused}\u2003{other}", "quote": None}


# -- 4. the rows that keep refs a writer chose -------------------------------------------------------------------------


def _source_row(source_id: str, refs: object, *, raw: object = None, extra: dict[str, object] | None = None) -> dict[str, object]:
    """A source as the agent-output ingest stores it: the refs it was sent in its metadata and again in the raw payload."""

    return {
        "id": source_id,
        "source_type": "agent_output",
        "title": "Agent output",
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {
            "agent_id": "writer",
            "source_refs": refs,
            "raw_payload": {"title": "Agent output", "content": "The agent wrote a note.", "source_refs": refs if raw is None else raw},
            **(extra or {}),
        },
    }


@pytest.mark.parametrize("kind", _REFUSED_KINDS)
def test_a_source_made_by_an_agent_loses_the_words_of_a_memory_it_cites_that_the_caller_may_not_read(kind: str) -> None:
    """A source row returned whole by a route keeps the refs its ingest was sent, in ``metadata_json.source_refs`` and in
    ``metadata_json.raw_payload.source_refs``. The reader holds both to the rule of a commit's refs: the entry that names the
    memory keeps its id and loses its quote and every name the product does not write, the entry beside it loses its words, and
    nothing else on the row changes (the title, the content of the raw payload, the agent).

    Mutations, each alone: skip the ``raw_payload`` holder in ``_source_ref_holders`` (the copy there keeps the words); skip the
    ``metadata_json`` refs in ``SavedProvenanceReader._source`` (the first copy keeps them).
    """

    store = _Store()
    refused = _refused_memory(store, kind)
    refs = [{"memory_id": refused, "quote": _WORDS, _WORDS: True}, f"memory:{refused}", {"quote": _WORDS}, f"see {_WORDS}"]
    row = _source_row(str(uuid4()), refs)
    before = copy.deepcopy(row)
    shown = _reader_for(store, kind).source(row)
    assert row == before, "the stored row is not changed by the read"
    assert _no_words(shown)
    expected = [{"memory_id": refused, "quote": None}, f"memory:{refused}", {"quote": None}, None]
    assert shown["metadata_json"]["source_refs"] == expected  # type: ignore[index]
    assert shown["metadata_json"]["raw_payload"]["source_refs"] == expected  # type: ignore[index]
    assert shown["metadata_json"]["raw_payload"]["content"] == "The agent wrote a note."  # type: ignore[index]
    assert shown["title"] == row["title"] and shown["id"] == row["id"]


def test_a_source_made_by_an_agent_loses_the_entry_that_cites_a_source_the_caller_may_not_read() -> None:
    """An entry that names a source the caller may not read (raised above its ceiling, archived or missing) is dropped from both
    copies of the refs, as it is from a commit's. An entry that names a readable source stays.

    Mutation: pass an empty set of refused sources to ``_without_refused_refs`` in ``_shown_refs`` (the entry stays).
    """

    store = _Store()
    readable = store.add_source()
    refused = store.add_source(sensitivity="confidential")
    archived = store.add_source()
    store.sources[archived]["deleted_at"] = "2026-10-01T00:00:00Z"
    for gone in (refused, archived, str(uuid4())):
        refs = [{"source_id": readable, "quote": "readable words"}, {"source_id": gone, "quote": _WORDS}, f"source:{gone}"]
        shown = _reader(store, _unbound("trusted_local_agent")).source(_source_row(str(uuid4()), refs))
        expected = [{"source_id": readable, "quote": "readable words"}]
        assert shown["metadata_json"]["source_refs"] == expected  # type: ignore[index]
        assert shown["metadata_json"]["raw_payload"]["source_refs"] == expected  # type: ignore[index]


def test_a_source_that_cites_nothing_the_caller_may_not_read_is_returned_as_the_same_object() -> None:
    """Nothing is copied when nothing is withheld, so a reader who may read everything a source cites is shown the stored row.

    Mutation: return a copy even when no ref is refused (``_source``).
    """

    store = _Store()
    readable_source, readable_memory = store.add_source(), store.add_memory()
    for refs in (
        [],
        None,
        "meeting notes",
        [f"source:{readable_source}", {"memory_id": readable_memory, "quote": "readable words"}],
        [{"source_id": readable_source, "quote": "words", "page": 3}],
    ):
        row = _source_row(str(uuid4()), refs)
        assert _reader(store, _unbound("trusted_local_agent")).source(row) is row, refs
    no_metadata = {"id": str(uuid4()), "title": "x", "metadata_json": None}
    assert _reader(store).source(no_metadata) is no_metadata  # type: ignore[arg-type]


def test_the_owner_and_an_unbound_admin_key_are_given_every_source_as_it_was_stored() -> None:
    """The owner and an unbound admin key have no limits, so ``sources`` and ``fields`` return the rows themselves and read no
    source and no memory. A key bound to a project is judged.

    Mutation: drop the ``_limited`` test from ``sources`` (the admin key is judged and the lookups are counted).
    """

    store = _Store()
    refused = store.add_memory(deleted=True)
    row = _source_row(str(uuid4()), [{"memory_id": refused, "quote": _WORDS}])
    rating = {"id": str(uuid4()), "metadata_json": {"memory_id": refused, "quote": _WORDS}}
    owner = SavedProvenanceReader(store, fence=SourceReadFence.unfenced())
    assert owner.source(row) is row and owner.sources([row])[0] is row
    assert owner.fields([rating], ("metadata_json",))[0] is rating
    admin = _reader(store, _unbound("admin_agent"))
    assert admin.source(row) is row and admin.fields([rating], ("metadata_json",))[0] is rating
    assert store.memory_batches == [] and store.memory_reads == 0 and store.source_reads == 0
    bound = _reader(store, _identity("admin_agent", "alpha"))
    assert bound.source(row) is not row and _no_words(bound.source(row))


def test_the_sources_of_many_rows_are_looked_up_together() -> None:
    """A workspace lists twenty sources. The sources and the memories their refs name are judged in one lookup each, and an id
    that two rows cite is judged once.

    Mutation: look the ids up one row at a time in ``SavedProvenanceReader.sources`` (drop ``_prefetch_refs``): the batches are
    one id each.
    """

    store = _Store()
    shared = store.add_memory(sensitivity="confidential")
    own = [store.add_memory() for _ in range(5)]
    in_the_payload = [store.add_memory() for _ in range(5)]
    rows = [
        _source_row(
            str(uuid4()),
            [{"memory_id": shared, "quote": "a"}, {"memory_id": i, "quote": "b"}],
            raw=[{"memory_id": shared, "quote": "a"}, {"memory_id": j, "quote": "c"}],
        )
        for i, j in zip(own, in_the_payload, strict=True)
    ]
    reader = _reader(store, _unbound("trusted_local_agent"))
    shown = reader.sources(rows)
    assert all(_SENTINEL not in json.dumps(item) for item in shown)
    assert all(item["metadata_json"]["source_refs"][0] == {"memory_id": shared, "quote": None} for item in shown)  # type: ignore[index]
    assert store.memory_batches == [11], store.memory_batches


def test_the_metadata_of_a_rating_and_what_a_task_may_use_are_held_to_the_same_rule() -> None:
    """The metadata of a quality rating and the sources and scope a queued task was given are structures their writer chose. A
    structure that names a memory the caller may not read keeps its ids and loses its words and its names; one that names a
    source the caller may not read is set to ``None`` when it is not a list (a list drops the entry). Fields that name nothing
    refused are returned as they were.

    Mutation: skip the ``_shown_refs`` call in ``fields`` (every field keeps the words).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    hidden_source = store.add_source(sensitivity="confidential")
    readable = store.add_source()
    rating = {"id": str(uuid4()), "artifact_id": str(uuid4()), "comments": "typed", "metadata_json": {
        "memory_id": refused, "quote": _WORDS, _WORDS: 1, "artifact_type": "daily_brief"}}
    clean_rating = {"id": str(uuid4()), "metadata_json": {"artifact_type": "daily_brief", "agent_identity": {"agent_id": "a"}}}
    task = {
        "id": str(uuid4()),
        "title": "Summarize",
        "scope_json": {"note": _WORDS, "memory_ids": [refused]},
        "allowed_sources_json": [{"memory_id": refused, "quote": _WORDS}, {"source_id": hidden_source, "quote": _WORDS}, {"source_id": readable}],
    }
    reader = _reader(store, _unbound("trusted_local_agent"))
    shown_rating, shown_clean = reader.fields([rating, clean_rating], ("metadata_json",))
    assert shown_rating["metadata_json"] == {"memory_id": refused, "quote": None}
    assert shown_rating["comments"] == "typed" and shown_rating["id"] == rating["id"]
    assert shown_clean is clean_rating
    (shown_task,) = reader.fields([task], ("allowed_sources_json", "scope_json"))
    assert shown_task["allowed_sources_json"] == [{"memory_id": refused, "quote": None}, {"source_id": readable}]
    assert shown_task["scope_json"] == {"memory_ids": [refused]}
    assert _no_words([shown_rating, shown_task])
    only_hidden = {"id": str(uuid4()), "scope_json": {"source_id": hidden_source, "label": _WORDS}}
    assert reader.fields([only_hidden], ("scope_json",))[0]["scope_json"] is None


# -- 5. what the unbound admin key keeps, what a link says, and a key a JSON text repeats ----------------------------------


def test_an_unbound_admin_key_keeps_the_text_of_an_entry_that_names_a_memory_beside_a_dangling_source_ref() -> None:
    """An unbound ``admin_agent`` key has no limits, so every memory is readable to it and none is looked up, but a named source
    that does not exist is refused to it as to anyone, and its entry is dropped. The entry beside it that names a memory keeps its
    text: the memory counts as readable without a lookup.

    Mutation: leave the unlimited branch of ``_judge_memories`` out of ``self._readable`` (the entry beside loses its quote).
    """

    store = _Store()
    named = store.add_memory(sensitivity="confidential")
    refs = [{"source_id": str(uuid4()), "quote": "gone"}, {"memory_id": named, "quote": "readable words"}]
    row = _commit(str(uuid4()), refs, copy_kind="none")
    shown = _reader(store, _unbound("admin_agent")).memory(row)
    assert _shown_refs(shown) == [{"memory_id": named, "quote": "readable words"}]
    assert store.memory_batches == [], "an unbound admin key reads no memory"


def test_the_words_of_a_quote_that_is_a_structure_are_withheld_from_a_link_that_says_the_same() -> None:
    """The words of every quote that is withheld are withheld from the memory's links, so a quote written as an object or a list
    cannot be recovered from a link that holds the same text.

    Mutation: drop the strings of a quote that is a structure from the words withheld (``_note_withheld`` adds nothing for it).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    readable = store.add_source()
    row = _commit(str(uuid4()), [{"memory_id": refused, "quote": {"text": _WORDS}}], copy_kind="none")
    memory_id = str(row["id"])
    kept = store.add_link(memory_id, readable, quote=_WORDS)
    other = store.add_link(memory_id, readable, quote="a different quote")
    reader = _reader(store)
    shown = reader.memory(row)
    assert _shown_refs(shown) == [{"memory_id": refused, "quote": None}]
    links = {link["id"]: link for link in reader.links(memory_id)}
    assert links[kept["id"]]["quote"] is None and links[other["id"]]["quote"] == "a different quote"


def test_a_key_that_a_json_text_repeats_holds_one_quote_for_each_value() -> None:
    """``json.loads`` keeps the last value of a repeated key, so the reader keeps every value as a list. Under a quote key such a
    list is the quotes the key was written with, and it is read as text, not as a structure: the ids typed in them are incidental
    for a memory (every id in them, a source's too, may be a memory) and read for no source. A list the writer wrote under the key is
    a structure.

    Mutation: read the repeated values as one list the writer wrote (``_RepeatedValues`` tested as a plain ``list``): the ids in
    the quote text are named.
    """

    a, m = str(uuid4()), str(uuid4())
    repeated = f'{{"quote": "source:{a} words", "quote": "memory:{m} words"}}'
    assert cited_source_ids(repeated) == cited_source_ids(None)
    assert cited_memory_refs(repeated).named == frozenset() and cited_memory_refs(repeated).incidental == {a, m}
    structure = json.dumps({"quote": [f"source:{a}", f"memory:{m}"]})
    assert cited_source_ids(structure).incidental == {a}, "the strings of a structure under a quote key are read like any other"
    assert cited_memory_refs(structure).named == {m}
    mixed = f'{{"quote": "words", "quote": {{"memory_id": "{m}"}}}}'
    assert cited_memory_refs(mixed).named == {m}
