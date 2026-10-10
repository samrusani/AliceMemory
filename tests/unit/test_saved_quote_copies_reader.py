"""Every copy of a saved quote is withheld with the quote, and every source a memory cites is judged.

Unreleased (on main, not in v0.20.0). The reader of saved quotes (``SavedProvenanceReader``) withheld the quote of a
link whose source the caller may not read, the memory's own copies of it, and the quote on another link of the same
memory when that link said the same text as a link that was left out. An outside review found the case that rule
did not reach: a commit with ``{"source_ids": [A, B]}`` makes a link to A only, and saves the one excerpt both as the
quote of that link and as ``agentic_memory.conversation_excerpt``. When B was made confidential the memory's own copy
was withheld (it names B) and the link to A, which the caller may read, kept the same bytes. These tests hold the
reader itself, with a stub store, to two rules:

* the text of every copy it withholds (a link that is left out, and each copy removed from the memory or from one of
  its revisions) is withheld from the memory's other links, whatever the copy was saved for;
* a memory is judged by every source id its refs name, in every shape a writer stores (``cited_source_ids``), and by
  the ids of other keys only when they name a stored source.

``tests/unit/test_saved_quote_every_copy.py`` runs the same rules on a real vault, with real keys, every door and every
reader. Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the
file was restored by copying the saved copy back.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.mcp import evidence_artifacts
from alicebot_api.vnext_source_fence import (
    CitedSourceIds,
    SavedProvenanceReader,
    SourceReadFence,
    cited_source_ids,
    cited_source_ids_in_memory_audit,
)
from tests.unit.test_saved_provenance_reader import (
    _ExplainStore,
    _Store,
    _identity,
    _reader,
    _revision,
    authorized_ids,  # noqa: F401  (a fixture of the sibling module)
)

_ROOT = Path(__file__).resolve().parents[2]
_QUOTE = "zinnwald-quote-8841 cone ten firing kiln log marlin-oxide-5520"


class _MemoryStore(_Store):
    """The stub store with ``get_memory`` and the bulk ``get_memories_by_ids``, as both real stores have them.

    ``memory_reads`` counts the single reads. ``memory_batches`` holds the size of each bulk read, which is how the reader looks
    up the memories a row may cite (``include_deleted`` returns a soft-deleted row, as the real stores do).
    """

    def __init__(self) -> None:
        super().__init__()
        self.memories: dict[str, dict[str, object]] = {}
        self.memory_reads = 0
        self.memory_batches: list[int] = []

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        self.memory_reads += 1
        return self.memories.get(memory_id)

    def get_memories_by_ids(self, ids: list[str], *, include_deleted: bool = False) -> list[dict[str, object]]:
        self.memory_batches.append(len(ids))
        return [
            self.memories[i] for i in ids if i in self.memories and (include_deleted or self.memories[i].get("deleted_at") is None)
        ]


def _row(
    memory_id: str,
    *,
    refs: list[object],
    copy_kind: str = "conversation_excerpt",
    text: str = _QUOTE,
    provenance_source: str | None = None,
) -> dict[str, object]:
    """A memory row with one saved copy of a quote and the refs it was committed with."""

    metadata: dict[str, object] = {"agentic_memory": {"kind": "agentic_memory_commit", "source_refs": list(refs)}}
    if copy_kind == "conversation_excerpt":
        metadata["agentic_memory"]["conversation_excerpt"] = text  # type: ignore[index]
    elif copy_kind in ("provenance", "replacement_provenance"):
        metadata[copy_kind] = {"source_id": provenance_source, "quote": text}
    return {
        "id": memory_id,
        "memory_key": f"key.{memory_id}",
        "canonical_text": "The cone ten firing schedule is posted.",
        "value": {"text": "The cone ten firing schedule is posted.", "source_refs": list(refs)},
        "metadata_json": metadata,
    }


def _shape_cases(a: str, b: str, c: str, m: str) -> dict[str, tuple[object, set[str], set[str]]]:
    """Every shape a writer stores that cites ``b``, with the ids ``cited_source_ids`` must find as named and as
    incidental. ``a`` is a source named beside it, ``c`` an id that is not a source and ``m`` a memory id."""

    upper = b.upper()
    hyphenless = b.replace("-", "")
    return {
        "plain": (b, {b}, set()),
        "upper case": (upper, {b}, set()),
        "no hyphens": (hyphenless, {b}, set()),
        "braces": ("{" + b + "}", {b}, set()),
        "urn:uuid:": ("urn:uuid:" + b, {b}, set()),
        "URN:UUID: upper case": ("URN:UUID:" + upper, {b}, set()),
        "source: prefix": ("source:" + b, {b}, set()),
        "SOURCE: prefix": ("SOURCE:" + b, {b}, set()),
        "Source: prefix": ("Source:" + b, {b}, set()),
        "source:source:": ("source:source:" + b, {b}, set()),
        "padded": ("  " + b + "  ", {b}, set()),
        "an integer of 32 digits": (int("1" * 32), {str(__import__("uuid").UUID("1" * 32))}, set()),
        "source ids list": ({"source_ids": [a, b]}, {a, b}, set()),
        "source_ids reversed": ({"source_ids": [b, a]}, {a, b}, set()),
        "list in list": ([[a, b]], {a, b}, set()),
        "source_references": ([a, {"source_references": [b]}], {a, b}, set()),
        "selected_source_ids": ({"source_id": a, "selected_source_ids": [b]}, {a, b}, set()),
        "list under a scalar key": ({"source_id": [a, b]}, {a, b}, set()),
        "nested under an unlisted key": ({"source_id": a, "meta": {"source_ids": [b]}}, {a, b}, set()),
        "id key with padding": ({"source_id": a, "id": "  " + b + "  "}, {a, b}, set()),
        "ref key": ({"source_id": a, "ref": "source:" + b}, {a, b}, set()),
        "comma joined": (f"{a},{b}", {a, b}, set()),
        "JSON text": (json.dumps({"source_ids": [a, b]}), {a, b}, set()),
        "URL form": ("alice://sources/" + b, {b}, set()),
        "chunk suffix": (f"source:{b}#chunk-1", {b}, set()),
        "five deep": ({"source_ids": [[[[{"source_id": b}]]]]}, {b}, set()),
        "other key": ({"source_id": a, "origin": b}, {a}, {b}),
        "ids as dict values": ({"source_ids": {"first": a, "second": b}}, set(), {a, b}),
        "url under sources": ({"source_id": a, "sources": [{"url": "source:" + b}]}, {a}, {b}),
        "memory ref": (f"memory:{m}", set(), set()),
        "memory ref in upper case": (f"MEMORY:{m}", set(), set()),
        "chunk id beside a source": ({"source_id": a, "chunk_id": c, "page": 3}, {a}, {c}),
        "a url": ("https://example.test/doc", set(), set()),
        "a label": ("meeting notes", set(), set()),
        "an id inside a quote": ({"source_id": a, "quote": f"seen in {b}"}, {a}, set()),
        "an id inside an excerpt key": ({"source_id": a, "conversation_excerpt": f"seen in {b}"}, {a}, set()),
        "nothing": (None, set(), set()),
    }


def test_cited_source_ids_reads_every_shape_and_spelling_a_writer_stores() -> None:
    """The reader judges a memory by what is stored, so it reads a ref in every shape the commit route stores raw, and
    not only in the shapes the link writer reads (a plain id, ``source:``, braces, ``urn:``, no hyphens, and the keys
    ``source_id``, ``id``, ``ref``, ``source_ref`` and the nested ``source_ids``, ``source_refs`` and ``sources``). The
    shapes the writer did not read (``selected_source_ids``, ``source_references``, a list or an object under a scalar
    key, an upper case ``SOURCE:``, ``URN:UUID:``, an id with padding under a key, several ids in one string or in a JSON
    string, a URL, a chunk suffix, an id under any other key) are read here. An id under a key that holds a reference is
    named; an id under any other key is incidental (it may be a chunk id or a session id); the text of a quote is not read.

    Mutations, each alone, in ``vnext_source_fence.py``: drop ``selected_source_ids`` from ``SOURCE_REFERENCE_KEYS`` (the
    ``selected_source_ids`` row fails); drop the ``.lower()`` of ``UUID(candidate.lower())`` in ``_uuid_text`` (the
    ``URN:UUID:`` row fails); make the ``source:`` prefix case sensitive in ``_SOURCE_PREFIX`` (the ``SOURCE:`` and
    ``Source:`` rows fail); iterate only the first entry of a list (``node[:1]`` in the list branch of ``cited_source_ids``: every
    row that holds a second id fails); make ``_json_container`` return None (the JSON row fails); skip the ``_TEXT_KEYS``
    test (the quote rows name ``b``).
    """

    a, b, c, m = (str(uuid4()) for _ in range(4))
    for label, (ref, named, incidental) in _shape_cases(a, b, c, m).items():
        cited = cited_source_ids(ref)
        assert (set(cited.named), set(cited.incidental)) == (named, incidental), label
    assert cited_source_ids([{"source_ids": [a, b]}, "meeting notes", c]) == CitedSourceIds(
        named=frozenset({a, b, c}), incidental=frozenset()
    ), "an entry of a ref list that is an id names a source, whatever it is: only a label holds no id"


def test_a_ref_nested_deeper_than_the_recursion_limit_is_read_to_the_end() -> None:
    """The walk keeps its own stack, so an id at the bottom of a ref nested thousands of levels deep is still found, and a
    nesting that deep does not raise.

    Mutation: make ``cited_source_ids`` recursive (a call to itself for each child): the deep ref raises ``RecursionError``.
    """

    b = str(uuid4())
    deep: object = {"source_id": b}
    for _ in range(5000):
        deep = [deep]
    assert cited_source_ids(deep).named == frozenset({b})


# -- the reader --------------------------------------------------------------------------------------------------


def _two_sources(store: _Store) -> tuple[str, str]:
    """A source the caller may read and one it may not (above the ceiling of a key bound to the project)."""

    return store.add_source(), store.add_source(sensitivity="confidential")


@pytest.mark.parametrize("copy_kind", ["conversation_excerpt", "provenance", "replacement_provenance"])
def test_a_link_to_a_readable_source_loses_a_quote_that_a_withheld_copy_says(copy_kind: str) -> None:
    """The finding of the outside review. A commit with ``{"source_ids": [A, B]}`` links only A, and the quote on that link
    is the excerpt the memory also keeps as its own copy, which names B. B is refused, so the copy is removed, and the link
    to A (which the caller may read) says the same text and loses its quote. This holds for each of the three copies a
    writer saves, whichever of them is the one that names the refused source. The link stays, with its id and its role.

    Mutations: drop ``texts |= _memory_copy_quote_texts(row)`` in ``SavedProvenanceReader._verdict`` (every case fails:
    this is the rule of #541, which compared only with the links that were left out); make the test in ``_shown`` read
    ``if False:`` (the same cases fail).
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[{"source_ids": [readable, refused]}], copy_kind=copy_kind, provenance_source=readable)
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable, quote="  " + _QUOTE.replace(" ", "\n\t ") + "  ")
    reader = _reader(store)
    shown_row = reader.memory(row)
    metadata = shown_row["metadata_json"]
    assert "provenance" not in metadata and "replacement_provenance" not in metadata  # type: ignore[operator]
    assert "conversation_excerpt" not in metadata["agentic_memory"]  # type: ignore[index]
    assert metadata["agentic_memory"]["source_refs"] == []  # type: ignore[index]
    (shown_link,) = reader.links(memory_id)
    assert shown_link == {**link, "quote": None}
    assert store.links[0]["quote"] is not None, "the stored link is not changed by the read"
    assert reader.shown_link(link) == {**link, "quote": None}


def test_the_links_of_a_memory_are_judged_against_the_row_the_store_holds_when_the_reader_was_not_given_it() -> None:
    """``links`` (and ``shown_link``) can be asked for a memory the reader was never given a row for. The reader reads the
    row from the store once, judges the sources it names, and withholds the quote the same way. A store with no
    ``get_memory`` leaves only the links to judge by, which is the rule of the first version.

    Mutation: make ``_row_for`` find no ``get_memory`` on the store (``getter = None``): the quote stays on the link.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    store.memories[memory_id] = _row(memory_id, refs=[{"source_ids": [readable, refused]}])
    store.add_link(memory_id, readable)
    reader = _reader(store)
    assert [link["quote"] for link in reader.links(memory_id)] == [None]
    assert [link["quote"] for link in reader.links(memory_id)] == [None]
    assert store.memory_reads == 1
    bare = _Store()
    readable, refused = _two_sources(bare)
    bare.add_link(memory_id, readable)
    assert [link["quote"] for link in _reader(bare).links(memory_id)] == [_QUOTE]


@pytest.mark.parametrize(
    "excerpt",
    [
        pytest.param("tkn-start " + "lorem ipsum dolor sit amet " * 140 + " tkn-middle " + "x " * 20 + "tkn-end", id="long"),
        pytest.param("  lead\tTAB nb sp em sp\r\nCRLF\n\nblank tkn-odd  ", id="odd whitespace"),
        pytest.param("tkn-" + "w" * 3996, id="4000 characters"),
    ],
)
def test_the_match_is_the_words_of_the_quote_whatever_the_whitespace_and_the_length(excerpt: str) -> None:
    """The writers store one string as the link's quote and the copy: the commit door collapses the whitespace of the
    excerpt once and stores that string in both places, the review doors store the quote as sent, and nothing cuts either
    to a length (``tests/unit/test_saved_quote_every_copy.py`` runs the writers to show it). So the rule is equality of the
    words, with the whitespace between them ignored. A long excerpt, one with tabs, a no-break space, an em space and CRLF,
    and one of the 4,000 characters the HTTP model allows are each withheld from the link, whether the link holds the
    collapsed text or the text as sent.

    Mutation: compare the quotes as raw strings (``_quote_text`` returning ``str(value)``): the as-sent variant stays on
    the link.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    collapsed = " ".join(excerpt.split())
    store.memories[memory_id] = _row(memory_id, refs=[{"source_ids": [readable, refused]}], text=collapsed)
    store.add_link(memory_id, readable, quote=collapsed)
    store.add_link(memory_id, readable, quote=excerpt)
    reader = _reader(store)
    reader.memory(store.memories[memory_id])
    assert [link["quote"] for link in reader.links(memory_id)] == [None, None]


def test_a_quote_that_says_something_else_stays_on_the_link_and_a_part_of_an_excerpt_is_not_the_excerpt() -> None:
    """The rule withholds the text a copy says and nothing else. A link whose quote is another sentence keeps it (the link
    from a captured candidate to its own source), and so does a link whose quote is a part of the withheld excerpt, or
    one that contains the excerpt: no writer makes either relation, so it is a different quote. A short quote that happens
    to be a word inside a long excerpt is not withheld.

    Mutation: widen the match to any substring (in ``_shown``: ``any(quote in text or text in quote for text in ...)``): the
    short and the long quote are both withheld and this test fails.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    excerpt = "Alpha pottery log. The kiln was fired to cone ten on Monday. " + "Details follow. " * 20
    store.memories[memory_id] = _row(memory_id, refs=[{"source_ids": [readable, refused]}], text=excerpt)
    short = store.add_link(memory_id, readable, quote="kiln")
    part = store.add_link(memory_id, readable, quote="The kiln was fired to cone ten on Monday.")
    longer = store.add_link(memory_id, readable, quote=excerpt + " And one more sentence.")
    other = store.add_link(memory_id, readable, quote="Parentnote: the alpha kiln is fired on Mondays")
    same = store.add_link(memory_id, readable, quote=excerpt)
    reader = _reader(store)
    reader.memory(store.memories[memory_id])
    shown = reader.links(memory_id)
    assert [link["quote"] for link in shown] == [
        short["quote"],
        part["quote"],
        longer["quote"],
        other["quote"],
        None,
    ]
    assert shown[0] is short and shown[3] is other, "a link that keeps its quote is the stored one"
    assert same["quote"] == excerpt


def test_a_memory_that_cites_only_readable_sources_is_returned_as_the_same_object_in_every_shape() -> None:
    """Authorized callers are unchanged. For every shape in the table above, when the caller may read both sources the
    memory is the stored row itself (not a copy), its link keeps its quote and is the stored link, and a revision is the
    stored revision. The ids that are not sources (a chunk id, a memory id) and a quote that holds a source id change
    nothing: they are incidental, and an incidental id is judged only when it names a stored source the caller may not read.

    Mutation: judge an incidental id like a named one (in ``SavedProvenanceReader._verdict``, test
    ``cited.incidental`` without ``self._exists.get(...)``): the rows with a chunk id, a memory ref and a quote lose their copies.
    """

    store = _MemoryStore()
    a, b = store.add_source(), store.add_source()
    c, m = str(uuid4()), str(uuid4())
    # The memory the ``memory ref`` rows name is one the caller may read (a ref to a missing memory is refused).
    store.memories[m] = {"id": m, "memory_key": "k", "domain": "project", "sensitivity": "internal", "metadata_json": {"project_scope": ["alpha"]}}
    for label, (ref, named, _incidental) in _shape_cases(a, b, c, m).items():
        if not named <= {a, b}:
            continue  # the integer names an id of its own, which is no stored source and so is refused, as a named id is
        memory_id = str(uuid4())
        row = _row(memory_id, refs=[ref])
        before = copy.deepcopy(row)
        store.memories[memory_id] = row
        link = store.add_link(memory_id, a)
        reader = _reader(store)
        assert reader.memory(row) is row, label
        assert row == before, label
        assert reader.links(memory_id)[0] is link, label
        revision = _revision(memory_id, [ref])
        assert reader.revision(revision) is revision, label


def test_a_ref_in_any_shape_that_names_a_refused_source_is_dropped_with_the_quote_and_the_rest_is_kept() -> None:
    """For every shape that names the refused source ``b``, the reader refuses it (a named id that names no stored
    source, an archived one, is refused as a missing one is; an incidental id that names a stored source the caller may not
    read is refused), the copies of the quote are removed, the entry that names ``b`` is dropped from every ref list, and a
    ref beside it that names only ``a`` stays. The link to ``a`` loses its quote. Nothing else on the row changes.

    Mutations: drop the ``cited.incidental`` set from ``refused`` in ``_verdict`` (the ``other key``, ``ids as dict
    values`` and ``url under sources`` rows keep the quote); drop ``cited.named`` from it (every row that has no link to
    the refused source keeps it).
    """

    for kind in ("confidential", "missing"):
        store = _MemoryStore()
        a = store.add_source()
        b = store.add_source(sensitivity="confidential") if kind == "confidential" else str(uuid4())
        c, m = str(uuid4()), str(uuid4())
        for label, (ref, named, incidental) in _shape_cases(a, b, c, m).items():
            if b not in named | incidental:
                continue
            if kind == "missing" and b not in named:
                continue  # an incidental id that names no stored source is not a reference (see CitedSourceIds)
            memory_id = str(uuid4())
            keep = f"source:{a}"  # an entry that names only the source the caller may read stays
            row = _row(memory_id, refs=[ref, keep])
            store.memories[memory_id] = row
            link = store.add_link(memory_id, a)
            reader = _reader(store)
            shown = reader.memory(row)
            assert shown is not row, (kind, label)
            metadata = shown["metadata_json"]
            assert "conversation_excerpt" not in metadata["agentic_memory"], (kind, label)  # type: ignore[operator]
            assert metadata["agentic_memory"]["source_refs"] == [keep], (kind, label)  # type: ignore[index]
            assert shown["value"]["source_refs"] == [keep], (kind, label)  # type: ignore[index]
            assert b not in json.dumps(shown), (kind, label)
            assert [row["quote"] for row in reader.links(memory_id)] == [None], (kind, label)
            assert store.links[-1] is link and link["quote"] == _QUOTE


def test_a_source_ref_that_is_not_a_list_and_names_a_refused_source_is_removed() -> None:
    """The writers store ``source_refs`` as a list, but a value of another shape that a store holds is judged too: a ref
    that is a dict or a string and names a refused source goes with its key, and one that names nothing stays.

    Mutation: return a non-list value unchanged from ``_without_refused_refs``.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable])
    row["metadata_json"]["source_refs"] = {"source_ids": [readable, refused]}  # type: ignore[index]
    row["metadata_json"]["agentic_memory"]["source_refs"] = f"{readable},{refused}"  # type: ignore[index]
    row["value"]["source_refs"] = "https://example.test/doc"  # type: ignore[index]
    shown = _reader(store).memory(row)
    assert "source_refs" not in shown["metadata_json"] and "source_refs" not in shown["metadata_json"]["agentic_memory"]  # type: ignore[operator,index]
    assert shown["value"]["source_refs"] == "https://example.test/doc"  # type: ignore[index]
    assert refused not in json.dumps(shown)


def test_a_copy_of_a_quote_in_a_revision_is_removed_and_its_text_is_withheld_from_the_links() -> None:
    """The ``previous_value`` and ``new_value`` of a revision can hold the same copies a memory row does. No writer saves a
    quote there today, so the revision rows below are planted. A revision that cites a refused source, or whose memory
    withholds its quotes, loses the copies (``conversation_excerpt``, ``provenance``, ``replacement_provenance``) and the
    refs that name the source, and a link to a readable source whose quote says the text of such a copy loses its quote,
    when the revision is read before the links, as the review reads them. A revision of a memory that is read
    entirely is the stored revision.

    Mutations: drop ``_without_quote_copies`` from ``_revision_without_refused_refs`` (the copies stay); drop the
    ``self._revision_texts`` update in ``_revision`` (the link keeps its quote); ignore ``memory_withholds`` (the
    revision of a memory with a refused link keeps its copies).
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable], copy_kind="none")
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable, quote="the planted revision quote")
    revision = {
        **_revision(memory_id, [readable, refused]),
        "new_value": {
            "text": "x",
            "source_refs": [readable, refused],
            "conversation_excerpt": "the planted revision quote",
            "provenance": {"source_id": readable, "quote": "another planted quote"},
        },
    }
    reader = _reader(store)
    reader.memory(row)
    shown_revision = reader.revision(revision)
    assert shown_revision["new_value"] == {"text": "x", "source_refs": [readable]}
    assert revision["new_value"]["conversation_excerpt"] == "the planted revision quote"  # type: ignore[index]
    assert [shown["quote"] for shown in reader.links(memory_id)] == [None]
    assert link["quote"] == "the planted revision quote"
    # A memory whose own link is refused: its revision holds copies and no ref that names a refused source.
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable], copy_kind="none")
    store.memories[memory_id] = row
    store.add_link(memory_id, refused)
    revision = {
        **_revision(memory_id, [readable]),
        "new_value": {"text": "x", "source_refs": [readable], "conversation_excerpt": "kept nowhere"},
    }
    reader = _reader(store)
    reader.memory(row)
    assert reader.revision(revision)["new_value"] == {"text": "x", "source_refs": [readable]}
    # A revision of a memory with nothing refused is the stored revision.
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable])
    store.memories[memory_id] = row
    store.add_link(memory_id, readable)
    revision = {**_revision(memory_id, [readable]), "new_value": {"text": "x", "conversation_excerpt": "stays"}}
    reader = _reader(store)
    reader.memory(row)
    assert reader.revision(revision) is revision


class _BatchStore(_MemoryStore):
    """The stub store that records how many ids each source lookup was given."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[int] = []

    def get_sources_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        self.batches.append(len(ids))
        return super().get_sources_by_ids(ids)


def test_a_memory_that_names_many_ids_reads_the_sources_in_slices_and_memory_refs_cost_no_read() -> None:
    """A rollup card writes one ``memory:<id>`` ref per member into ``source_refs`` and a client can send 64 refs of any
    shape, so the reader must not hand a store thousands of ids in one lookup (a SQLite build from before 3.32 allows 999
    bound variables). The ids that name stored sources are looked up in slices of 500, in as few reads as that allows, and
    a ``memory:`` ref names a memory, so it is not looked up as a source at all.

    Mutations: look the ids up in one call (``_judge`` without the slices: the largest batch is 1,200); read the id after
    ``memory:`` as a source id (the first read is 300 ids and the second assertion fails); drop the test for a quote at the
    top of ``_memory_ids_named_by_memory_copies`` (the 300 members of the roll-up are looked up, one read each).
    """

    store = _BatchStore()
    readable = store.add_source()
    # A roll-up card lists its members and holds no quote, so its memories are not looked up either.
    rollup = _row(str(uuid4()), refs=[f"memory:{uuid4()}" for _ in range(300)], copy_kind="none")
    memories_only = _reader(store).memory(rollup)
    assert memories_only is rollup and store.batches == [], "a memory ref is not a source id"
    assert store.memory_reads == 0, "a row with no quote has nothing to withhold, so its memories are not read"
    many = [{"source_id": readable, "origin": str(uuid4())} for _ in range(1200)]
    row = _row(str(uuid4()), refs=many)
    assert _reader(store).memory(row) is row
    assert store.batches == [500, 500, 201], store.batches


def test_the_owner_is_given_the_stored_links_and_rows_whatever_the_refs_name() -> None:
    """The owner reads nothing and is shown what was stored, for every shape.

    Mutation: make ``SourceReadFence.fenced`` return ``True``: the owner's row is withheld from and the counters move.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[{"source_ids": [readable, refused]}])
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable)
    reader = SavedProvenanceReader(store, fence=SourceReadFence.unfenced())
    assert reader.memory(row) is row
    assert reader.links(memory_id) == [link]
    assert reader.shown_link(link) is link
    assert (store.source_reads, store.memory_reads) == (0, 0)


def test_the_pack_reads_each_memory_row_and_source_once_for_its_links() -> None:
    """The memory rows the reader was given (``memories``) are the rows it judges the links against, so ``shown_link`` for
    the links of a packed memory costs no read of the store for the row, and the sources the rows name are read in the same
    one read as the sources of the links.

    Mutation: drop the ``self._rows`` registration in ``_prefetch``: the row is looked up in the store, and the counters
    read the number of links.
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    rows = []
    for _ in range(6):
        memory_id = str(uuid4())
        row = _row(memory_id, refs=[{"source_ids": [readable, refused]}])
        store.memories[memory_id] = row
        store.add_link(memory_id, readable)
        rows.append(row)
    reader = _reader(store)
    reader.memories(rows)
    reader.judge_links(store.links)
    shown = [reader.shown_link(link) for link in store.links]
    assert all(link is not None and link["quote"] is None for link in shown)
    assert (store.link_reads, store.source_reads, store.memory_reads) == (1, 1, 0)
    # The ids the rows cite are looked up as possible memories as well, once for all six rows.
    assert store.memory_batches == [2]


# -- alice_explain -----------------------------------------------------------------------------------------------


def test_the_audit_names_every_shape_in_the_memory_the_revisions_and_the_events() -> None:
    """``alice_explain`` authorizes the ids ``cited_source_ids_in_memory_audit`` finds in the memory row, its revisions and
    its event payloads, in every shape the reader reads, the ids at positions that hold a reference as named and the others as
    incidental. A shape that the link writer never read is found in each of the three places.

    Mutation: drop the ``revisions`` loop of ``cited_source_ids_in_memory_audit``: the revision's id is missing.
    """

    a, b, c = (str(uuid4()) for _ in range(3))
    audit = {
        "memory": {"id": "m1", "metadata_json": {"agentic_memory": {"source_refs": [{"selected_source_ids": [a]}]}}},
        "revisions": [{"revision_number": 1, "new_value": {"source_refs": ["SOURCE:" + b]}}],
        "events": [
            {"payload_json": {"changes": {"metadata_json": {"agentic_memory": {"source_refs": [{"origin": c}]}}}}}
        ],
    }
    cited = cited_source_ids_in_memory_audit(audit)
    assert (set(cited.named), set(cited.incidental)) == ({a, b}, {c})


def test_explain_authorizes_an_incidental_id_that_names_a_stored_source_and_ignores_one_that_names_none(
    authorized_ids: list[str],
) -> None:
    """An id under a key that does not name a source (``origin``) is authorized when it names a stored source, so a key that
    may not read that source is refused the call, and it is left alone when it names none (a chunk id, a session id), so a
    memory that carries one is still explained to a key that may read what it cites.

    Mutations: drop the ``incidental_source_ids`` loop of ``_authorize_memory_audit_provenance`` (the confidential case
    returns an id set); make the loop raise for an id that names no stored source (the unknown case raises).
    """

    readable, confidential, unknown = str(uuid4()), str(uuid4()), str(uuid4())
    store = _ExplainStore(
        {readable: {"id": readable, "sensitivity": "internal"}, confidential: {"sensitivity": "confidential"}}
    )
    authorize = evidence_artifacts._authorize_memory_audit_provenance
    identity = _identity()
    assert authorize(
        store, identity=identity, provenance_links=[], copied_source_ids={readable}, incidental_source_ids={unknown}
    ) == {readable}
    with pytest.raises(evidence_artifacts._ExplainAuthorizationError):
        authorize(
            store,
            identity=identity,
            provenance_links=[],
            copied_source_ids={readable},
            incidental_source_ids={confidential},
        )
    authorized_ids.clear()
    assert authorize(
        store,
        identity=identity,
        provenance_links=[{"source_id": readable}],
        copied_source_ids={readable},
        incidental_source_ids={readable},
    ) == {readable}
    assert authorized_ids == [readable], "a source named twice, as named and as incidental, is asked about once"


def test_the_audit_handler_passes_both_groups_of_ids_to_the_authorization() -> None:
    """``_handle_alice_vnext_memory_audit`` passes ``copied_source_ids`` and ``incidental_source_ids`` from
    ``cited_source_ids_in_memory_audit``. The second parameter has a default so that the keyless tests of the first
    version still call the function, and this pin is what stops the one production call from leaving it out.

    Mutation: remove the ``incidental_source_ids=`` keyword from the call.
    """

    tree = ast.parse((_ROOT / "apps/api/src/alicebot_api/mcp/evidence_artifacts.py").read_text(encoding="utf-8"))
    handler = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_handle_alice_vnext_memory_audit"
    )
    calls = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_authorize_memory_audit_provenance"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["copied_source_ids"] == "cited.named"
    assert keywords["incidental_source_ids"] == "cited.incidental"
