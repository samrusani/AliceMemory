"""How the saved-quote reader reads a ref: what it costs, which ids it names, which it judges, and what a revision holds.

Unreleased (on main, not in v0.20.0). An outside review of ``SavedProvenanceReader`` found four defects in the way it reads
the refs a memory was stored with, and these tests hold each of them with a stub store. ``tests/unit/test_saved_quote_ref_reading_vault.py``
runs the same cases on a real vault with real keys.

* Cost. The reader found each id in a ref string and then rescanned the whole string before it to see whether a ``source:``
  or ``memory:`` marker stood just ahead of the id, so reading a ref was quadratic in its length. The proposal door stores
  ``source_refs`` as sent and bounds only the size of the request, so a stored ref of 111 KB made ``alice_memory_review`` of
  the memory take 12 s and ``alice_explain`` 5 s for every key. Every parser is now linear and the tests time each shape.
* What an id names. An id inside a sentence or an outside URL (``copied from source: <id>``,
  ``https://host/projects/1/sources/<id>``) was counted as a named source, and a named id that names no stored source counts
  as missing, so a memory with such a ref lost its quote for every key, the admin key included, and ``alice_explain`` refused
  it. Such an id is now incidental: judged when it names a stored source, left alone when it names none.
* Archived sources. An incidental id was judged only if it named a source the store returns, and neither store returns an
  archived source, so a quote saved under an id that names an archived source stayed readable. The reader now asks for the
  rows of an archived source as well (``get_sources_by_ids(..., include_deleted=True)``) and so does ``alice_explain``.
* Revisions. A revision row keeps its own ``metadata_json``, where a memory proposal writes the ``source_refs`` it was given,
  and the reader neither judged nor scrubbed it.

Each test names the mutation that must fail it. The mutations were made by hand on a copy of the module and the file was
restored by copying the saved original back.
"""

from __future__ import annotations

import json
import random
import re
import time
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp import evidence_artifacts
from alicebot_api.vnext_source_fence import (
    _ids_in_text,
    cited_source_ids,
    cited_source_ids_in_memory_audit,
    source_rows_including_archived,
)
from tests.unit.test_saved_provenance_reader import (
    _ExplainStore,
    _Store,
    _identity,
    _reader,
    _revision,
    authorized_ids,  # noqa: F401  (a fixture of the sibling module)
)
from tests.unit.test_saved_quote_copies_reader import _QUOTE, _MemoryStore, _row, _two_sources

# A parser that is linear takes a few hundredths of a second on every input below on a slow machine; one that rescans the
# text before each id takes 12 seconds or more on 8,000 ids. The limit sits between them with a wide margin each way.
_TIME_LIMIT_SECONDS = 1.5
_IDS = 8_000


def _new_ids(count: int) -> list[str]:
    return [str(uuid4()) for _ in range(count)]


class _ArchiveStore(_MemoryStore):
    """The stub store with the lookup both real stores have, which also returns an archived source (``deleted_at`` set)."""

    def __init__(self) -> None:
        super().__init__()
        self.archive_reads: list[int] = []

    def archive(self, source_id: str) -> None:
        self.sources[source_id]["deleted_at"] = "2026-10-01T00:00:00+00:00"

    def get_sources_by_ids(self, ids: list[str], *, include_deleted: bool = False) -> list[dict[str, object]]:
        if not include_deleted:
            return super().get_sources_by_ids(ids)
        self.archive_reads.append(len(ids))
        return [self.sources[source_id] for source_id in ids if source_id in self.sources]


# -- 1. cost ---------------------------------------------------------------------------------------------------------


def _cost_cases() -> dict[str, tuple[str, set[str], set[str]]]:
    """``(ref, named, incidental)``: a ref string several hundred thousand characters long and the ids it must give."""

    ids = _new_ids(_IDS)
    first = ids[0]
    return {
        "ids joined by commas": (",".join(ids), set(ids), set()),
        "source: words": (",".join("source:" + source_id for source_id in ids), set(ids), set()),
        "JSON text of ids": (json.dumps({"source_ids": ids}), set(ids), set()),
        "a sentence with a marker before each id": (" ".join("see source: " + i for i in ids), set(), set(ids)),
        "outside URLs": (" ".join("https://host.example/sources/" + i for i in ids), set(), set(ids)),
        "a run of source: prefixes": ("source:" * 400_000, set(), set()),
        "a run of source: prefixes and then an id": ("source:" * 300_000 + first, {first}, set()),
        "a run of whitespace and then an id": (" " * 2_000_000 + first, {first}, set()),
    }


@pytest.mark.parametrize("label", list(_cost_cases()))
def test_a_ref_string_is_read_in_time_that_grows_with_its_length(label: str) -> None:
    """Each shape of a long ref string (8,000 ids, or megabytes of prefixes and whitespace) is read in well under the limit
    and gives exactly the ids it holds, so a parser that is fast because it stops early fails the second assertion.

    Mutations, each alone, in ``vnext_source_fence.py``: add ``_REFLOW.search(text, 0, start)`` with
    ``_REFLOW = re.compile(r"memory:$", re.IGNORECASE)`` to the loop of ``_ids_in_text``, which rescans the text before
    each id as the shipped reader did (every shape with thousands of ids fails on time: 12 s and more); restore the
    ``while candidate[:7].lower() == "source:": candidate = candidate[7:].strip()`` loop of the previous ``_whole_id`` in
    place of the prefix regex (the two runs of ``source:`` fail on time).
    """

    ref, named, incidental = _cost_cases()[label]
    started = time.perf_counter()
    cited = cited_source_ids(ref)
    took = time.perf_counter() - started
    assert (set(cited.named), set(cited.incidental)) == (named, incidental)
    assert took < _TIME_LIMIT_SECONDS, f"{label}: {took:.2f} s for {len(ref):,} characters"


def test_the_reader_and_the_audit_read_a_memory_whose_ref_is_huge_in_time_that_grows_with_its_size() -> None:
    """The end to end of the first case. A memory (the proposal door stores whatever ``source_refs`` it is sent) holds a
    ref of 8,000 ids in its metadata, in ``agentic_memory``, in ``value`` and in a revision, and the audit that
    ``alice_explain`` judges holds it in its events as well. The reader (memory, revision and links) and the audit scan
    together stay under the limit, the entry that names the refused ids is dropped, and the readable source stays.

    Mutation: the first mutation of the test above (rescan from offset 0 for each id).
    """

    store = _MemoryStore()
    readable = store.add_source()
    huge = ",".join(_new_ids(_IDS))
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable, huge])
    store.memories[memory_id] = row
    store.add_link(memory_id, readable)
    revision = {**_revision(memory_id, [readable, huge]), "metadata_json": {"source_refs": [readable, huge]}}
    audit = {
        "memory": row,
        "revisions": [revision],
        "events": [{"payload_json": {"source_refs": [readable, huge], "changes": {"source_refs": [huge]}}}],
    }
    reader = _reader(store)
    started = time.perf_counter()
    shown = reader.memory(row)
    shown_revision = reader.revision(revision)
    shown_links = reader.links(memory_id)
    cited = cited_source_ids_in_memory_audit(audit)
    took = time.perf_counter() - started
    assert took < _TIME_LIMIT_SECONDS, f"{took:.2f} s"
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == [readable]  # type: ignore[index]
    assert shown_revision["metadata_json"]["source_refs"] == [readable]  # type: ignore[index]
    assert [link["quote"] for link in shown_links] == [None]
    assert len(cited.named) == _IDS + 1


# -- 2. which ids a string names -------------------------------------------------------------------------------------


def _tier_cases(a: str, b: str) -> dict[str, tuple[object, set[str], set[str]]]:
    """``(ref, named, incidental)`` for strings that hold ``b`` (and ``a``) in a sentence, a URL or with punctuation, and
    for the forms that do say an id is a source. Every id here is a made-up one, a stand-in for an id that names a chunk."""

    return {
        "outside URL": (f"https://app.example.test/projects/1/sources/{b}", set(), {b}),
        "outside URL with a path after the id": (f"see https://app.example.test/sources/{b}/chunks/3", set(), {b}),
        "note with a source label": (f"copied from source: {b}", set(), {b}),
        "label and id": (f"source_id: {b}", set(), {b}),
        "id in parentheses": (f"({b})", set(), {b}),
        "id in quotes": (f'"{b}"', set(), {b}),
        "key and id": (f"id={b}", set(), {b}),
        "another prefix": (f"src:{b}", set(), {b}),
        "bracketed text": (f"[{a}, {b}]", set(), {a, b}),
        "a sentence with two ids": (f"{a} and {b}", set(), {a, b}),
        "a sentence after a list": (f"{a}, {b} (kiln log)", set(), {a, b}),
        "a source: word in a sentence": (f"copied from source:{b}", {b}, set()),
        "an alice URL in a sentence": (f"see alice://sources/{b} for the log", {b}, set()),
        "an alice URL": (f"alice://sources/{b}", {b}, set()),
        "an upper case alice URL": (f"ALICE://SOURCES/{b.upper()}", {b}, set()),
        "a source: word with a suffix": (f"source:{b}#chunk-1", {b}, set()),
        "a list of ids": (f"{a} {b}", {a, b}, set()),
        "ids and semicolons": (f"{a};{b}", {a, b}, set()),
        "source: words in a list": (f"source:{a}, SOURCE:{b}", {a, b}, set()),
        "a prefix, a space and an id": (f"source: {b}", {b}, set()),
        "a memory ref": (f"memory:{b}", set(), set()),
        "a memory ref in a sentence": (f"rolled up from MEMORY:{b}", set(), set()),
    }


def test_a_sentence_or_an_outside_url_that_holds_an_id_does_not_name_it() -> None:
    """The ids of a string are named when the string says they are sources (the whole string is one id or a list of ids, a
    word starts with ``source:`` or ``alice://sources/``) and are incidental when they sit in a sentence, an outside URL or
    punctuation. A ``memory:`` ref names no source.

    Mutations, each alone, in ``_ids_in_text``: name an id that has ``source:`` or ``sources/`` just before it anywhere in
    the text (the shipped rule: the URL and sentence rows move to named); make ``every_word_is_an_id`` always true (the
    sentence rows move to named); ignore ``explicit`` (the ``source:`` word and alice URL rows in a sentence become
    incidental).
    """

    a, b = str(uuid4()), str(uuid4())
    for label, (ref, named, incidental) in _tier_cases(a, b).items():
        cited = cited_source_ids(ref)
        assert (set(cited.named), set(cited.incidental)) == (named, incidental), label


def test_a_memory_whose_ref_holds_an_id_in_a_sentence_or_a_url_is_the_stored_row_for_a_caller_who_may_read_the_sources() -> None:
    """The case an outside review found. A commit with ``[A, "https://host/projects/1/sources/<id>"]`` or
    ``[A, "copied from source: <id>"]`` succeeds (the write fence reads neither), and the id names nothing. The reader
    counted it as a named source that does not exist, so the quote went for every key, the admin key included, and
    ``alice_explain`` refused the memory; the owner was unaffected. Now a caller who may read the source it cites gets the
    stored row as the same object, the link keeps its quote, the audit names no id but ``A``, and a revision is the stored
    revision. The id of a ref that does say it is a source (``source:<id>``) and names nothing is still refused.

    Mutation: the first mutation of the test above (an id after a ``source:`` or ``sources/`` marker anywhere is named).
    """

    for label, (ref, named, incidental) in _tier_cases(str(uuid4()), str(uuid4())).items():
        store = _MemoryStore()
        readable = store.add_source()
        stray = next(iter(named | incidental), None)
        if stray is None:
            continue
        refs = [readable, ref]
        memory_id = str(uuid4())
        row = _row(memory_id, refs=refs)
        store.memories[memory_id] = row
        link = store.add_link(memory_id, readable)
        reader = _reader(store)
        shown = reader.memory(row)
        revision = _revision(memory_id, refs)
        audit_ids = cited_source_ids_in_memory_audit({"memory": row, "revisions": [revision]})
        if named:
            # The ref says its ids are sources and they are not stored: refused, as a missing named source always is.
            assert shown is not row, label
            assert [shown_link["quote"] for shown_link in reader.links(memory_id)] == [None], label
        else:
            assert shown is row, label
            assert reader.links(memory_id)[0] is link, label
            assert reader.revision(revision) is revision, label
            assert audit_ids.named == frozenset({readable}), label


# -- 3. the reading, checked against a slow rule ---------------------------------------------------------------------

_ID = r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"


def _slow_rule(text: str, as_ref: bool) -> tuple[set[str], set[str]]:
    """The rule of ``_ids_in_text``, written the obvious quadratic way and sharing none of its code."""

    def uuid_of(candidate: str) -> str | None:
        try:
            return str(UUID(candidate.strip().lower()))
        except ValueError:
            return None

    def strip_prefixes(candidate: str) -> str:
        candidate = candidate.strip()
        while candidate[:7].lower() == "source:":
            candidate = candidate[7:].strip()
        return candidate

    named: set[str] = set()
    incidental: set[str] = set()
    whole = uuid_of(strip_prefixes(text))
    if whole is not None:
        return ({whole}, set()) if as_ref else (set(), {whole})
    if as_ref:
        found_words: list[tuple[str, bool]] = []
        every_word_is_an_id = True
        for word in re.split(r"[\s,;|]+", text.strip()):
            if not word:
                continue
            lowered = word.lower()
            explicit = False
            rest = word
            if lowered.startswith("alice://sources/"):
                rest, explicit = word[len("alice://sources/") :], True
            elif lowered.startswith("source:"):
                rest, explicit = strip_prefixes(word), True
            found = uuid_of(rest)
            if found is None and explicit:
                match = re.match(_ID + r"(?![0-9a-fA-F])", rest)
                found = str(UUID(match.group(0))) if match else None
            if found is None:
                every_word_is_an_id = False
            else:
                found_words.append((found, explicit))
        for found, explicit in found_words:
            (named if explicit or every_word_is_an_id else incidental).add(found)
    for match in re.finditer(r"(?<![0-9a-fA-F])" + _ID + r"(?![0-9a-fA-F])", text):
        if re.search(r"memory:$", text[: match.start()], re.IGNORECASE):
            continue
        incidental.add(str(UUID(match.group(0))))
    return named, incidental - named


def _random_texts(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    spellings = (
        lambda i: i,
        lambda i: i.upper(),
        lambda i: i.replace("-", ""),
        lambda i: "{" + i + "}",
        lambda i: "urn:uuid:" + i,
    )
    wrappers = (
        lambda s: s,
        lambda s: "source:" + s,
        lambda s: "SOURCE:" + s,
        lambda s: "Source: " + s,
        lambda s: "source:source:" + s,
        lambda s: "alice://sources/" + s,
        lambda s: "ALICE://SOURCES/" + s,
        lambda s: "alice://sources/" + s + "/chunks/2",
        lambda s: "https://h.example/sources/" + s,
        lambda s: "https://h.example/x/" + s,
        lambda s: "memory:" + s,
        lambda s: "MEMORY:" + s,
        lambda s: "(" + s + ")",
        lambda s: '"' + s + '"',
        lambda s: "[" + s,
        lambda s: s + "]",
        lambda s: s + "#chunk-1",
        lambda s: s + ".",
        lambda s: s + "ab",
        lambda s: "id=" + s,
        lambda s: "src:" + s,
    )
    words = ("copied", "from", "see", "note:", "source:", "src:", "and", "deadbeef", "a" * 34, "x")
    separators = (", ", " ", ";", "|", "\n", "\t", ",", "", "  ")
    pool = _new_ids(40)
    texts: list[str] = []
    for _ in range(count):
        parts: list[str] = []
        for _ in range(rng.randint(1, 6)):
            if rng.random() < 0.25:
                parts.append(rng.choice(words))
            else:
                parts.append(rng.choice(wrappers)(rng.choice(spellings)(rng.choice(pool))))
        text = parts[0]
        for part in parts[1:]:
            text += rng.choice(separators) + part
        texts.append(rng.choice(("", " ", "  ")) + text + rng.choice(("", " ", ",")))
    return texts


def test_the_fast_reading_of_a_string_is_the_slow_rule_on_random_strings() -> None:
    """The linear reader and a plain quadratic version of the same rule agree on 6,000 random strings built from ids in five
    spellings, ``source:`` and ``alice://sources/`` prefixes in several cases, outside URLs, ``memory:`` refs, words,
    punctuation, suffixes, hex that is one character too long, and every separator, read as a ref and as other text.

    Mutations, each alone, in ``_ids_in_text``: look for ``memory:`` one character to the left (``start - len(_MEMORY_PREFIX) - 1``);
    make ``every_word_is_an_id`` always true; ignore ``explicit``; set ``_MIN_ID_CHARS`` to 40 (an id in the spellings of
    32, 36 and 38 characters is no longer found).
    """

    for text in _random_texts(3_000, seed=541):
        for as_ref in (True, False):
            assert _ids_in_text(text, as_ref=as_ref) == _slow_rule(text, as_ref), (text, as_ref)


# -- 4. archived sources ---------------------------------------------------------------------------------------------


def _incidental_shapes(a: str, b: str) -> dict[str, object]:
    """Refs that hold ``b`` where the reader cannot be sure it is a source, with ``a`` the source the link names."""

    return {
        "another key": {"source_id": a, "origin": b},
        "a sentence": [a, f"copied from source: {b}"],
        "an outside URL": [a, f"https://host.example/projects/1/sources/{b}"],
        "parentheses": [a, f"({b})"],
        "quotes": [a, f'"{b}"'],
        "key and id": [a, f"id={b}"],
        "another prefix": [a, f"src:{b}"],
        "bracketed text": [f"[{a}, {b}]"],
    }


@pytest.mark.parametrize("shape", list(_incidental_shapes("a", "b")))
def test_an_id_that_names_an_archived_source_is_judged_wherever_it_is_found(shape: str) -> None:
    """``get_sources_by_ids`` leaves an archived source out, so the reader could not tell an id that names one from an id that
    names nothing, and judged neither. It now reads the rows of archived sources too, and an incidental id that names one is
    refused (nobody reads an archived source): the memory's copies of the quote are removed, the entry is dropped from the
    ref lists, and the link to the readable source loses the quote. The same ref with an id that names no source, and the same
    ref with the source not archived, keep everything.

    Mutation: ask ``_rows_by_id`` and not ``source_rows_including_archived`` in ``SavedProvenanceReader._judge``: the archived
    id looks like an id that names nothing and every case keeps the quote.
    """

    for state in ("archived", "stored", "nothing"):
        store = _ArchiveStore()
        a = store.add_source()
        b = store.add_source() if state != "nothing" else str(uuid4())
        if state == "archived":
            store.archive(b)
        ref = _incidental_shapes(a, b)[shape]
        refs = ref if isinstance(ref, list) else [ref]
        memory_id = str(uuid4())
        row = _row(memory_id, refs=refs)
        store.memories[memory_id] = row
        store.add_link(memory_id, a)
        reader = _reader(store)
        shown = reader.memory(row)
        quote = reader.links(memory_id)[0]["quote"]
        if state == "archived":
            assert shown is not row, shape
            assert "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"], shape  # type: ignore[index]
            assert b not in json.dumps(shown), shape
            assert quote is None, shape
        else:
            assert shown is row, (shape, state)
            assert quote == _QUOTE, (shape, state)


def test_a_store_without_the_lookup_for_archived_sources_is_read_as_before() -> None:
    """A store whose ``get_sources_by_ids`` has no ``include_deleted`` keyword (a stub) is read with the plain call, which leaves
    an archived source out, so the answer is the one the reader gave before. Both real stores take the keyword.

    Mutation: return an empty dict from ``source_rows_including_archived`` when the store lacks the keyword: the rows of
    every source are missing and the stored source is refused.
    """

    store = _Store()
    live, archived = store.add_source(), store.add_source()
    store.sources[archived]["deleted_at"] = "2026-10-01T00:00:00+00:00"
    assert set(source_rows_including_archived(store, [live, archived, str(uuid4())])) == {live}
    with_method = _ArchiveStore()
    kept, gone = with_method.add_source(), with_method.add_source()
    with_method.archive(gone)
    assert set(source_rows_including_archived(with_method, [kept, gone, str(uuid4())])) == {kept, gone}


# -- 5. what alice_explain does with an incidental id ----------------------------------------------------------------


class _ArchiveExplainStore(_ExplainStore):
    """The explain stub with a lookup that returns archived rows and counts how many ids each call is given."""

    def __init__(self, sources: dict[str, dict[str, object]]) -> None:
        super().__init__(sources)
        self.batches: list[int] = []

    def get_sources_by_ids(self, ids: list[str], *, include_deleted: bool = False) -> list[dict[str, object]]:
        assert include_deleted, "explain looks incidental ids up with the archived sources included"
        self.batches.append(len(ids))
        return [self.sources[source_id] for source_id in ids if source_id in self.sources]


def test_explain_refuses_an_incidental_id_that_names_an_archived_source_and_leaves_one_that_names_nothing(
    authorized_ids: list[str],
) -> None:
    """For a key, ``alice_explain`` authorizes each incidental id that names a stored source like any other and refuses the
    call for an archived one (``get_source`` returns none for it, which used to read as an id that names nothing). An id
    that names no source row is left alone.

    Mutation: remove the ``deleted_at`` test in ``_authorize_memory_audit_provenance`` (the archived source falls through to
    the policy question, which the stub answers as allowed, and the call returns).
    """

    readable, archived, nothing = str(uuid4()), str(uuid4()), str(uuid4())
    store = _ArchiveExplainStore(
        {
            readable: {"id": readable, "sensitivity": "internal", "deleted_at": None},
            archived: {"id": archived, "sensitivity": "internal", "deleted_at": "2026-10-01T00:00:00+00:00"},
        }
    )
    authorize = evidence_artifacts._authorize_memory_audit_provenance
    identity = _identity()
    assert authorize(
        store, identity=identity, provenance_links=[], copied_source_ids=set(), incidental_source_ids={readable, nothing}
    ) == {readable}
    with pytest.raises(evidence_artifacts._ExplainAuthorizationError):
        authorize(
            store, identity=identity, provenance_links=[], copied_source_ids=set(), incidental_source_ids={readable, archived}
        )


def test_explain_looks_up_the_incidental_ids_of_a_huge_ref_in_slices() -> None:
    """A ref full of ids that name nothing costs a few reads and not one per id: 1,300 incidental ids are looked up in three
    slices of at most 500, whether or not any of them names a stored source. (A store read per id was 1,300 round trips on
    Postgres for a ref a proposal can make as large as a request.)

    Mutation: look the ids up one at a time (call ``get_source`` for each id, as the first version of the loop did): the
    store records 1,300 reads of ``get_source`` and no batch, and the assertion on batches fails.
    """

    store = _ArchiveExplainStore({})
    incidental = set(_new_ids(1_300))
    assert (
        evidence_artifacts._authorize_memory_audit_provenance(
            store, identity=_identity(), provenance_links=[], copied_source_ids=set(), incidental_source_ids=incidental
        )
        == set()
    )
    assert sorted(store.batches, reverse=True) == [500, 500, 300]
    assert store.reads == [], "no per-id read of get_source"


# -- 6. revisions ----------------------------------------------------------------------------------------------------


def test_the_metadata_of_a_revision_is_judged_and_scrubbed_like_the_metadata_of_a_memory() -> None:
    """A memory proposal writes its metadata, ``source_refs`` included, into the metadata of the first revision of the
    memory. The reader judged a revision by its ``previous_value`` and ``new_value`` only, so the id of a source the caller
    may not read stayed in ``revisions[0].metadata_json.source_refs`` of ``alice_memory_review`` (no quote, an id, and a
    sign that the source exists). Now the entry that names a refused source is dropped, the quote copies a revision's
    metadata holds are removed when the revision or its memory withholds, and a link to a readable source whose quote says
    the same text loses it.

    Mutations: drop the ``metadata_json`` read from ``_source_ids_named_by_revision`` (the refused id stays and the audit does
    not name it); drop the ``metadata_json`` scrub from ``_revision_without_refused_refs`` (the id stays in the row); drop the
    ``texts |= _memory_copy_quote_texts(...)`` line from ``_revision`` (the link keeps its quote).
    """

    store = _MemoryStore()
    readable, refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable], copy_kind="none")
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable, quote="the planted metadata quote")
    revision = {
        **_revision(memory_id, [readable]),
        "metadata_json": {
            "proposal_id": "p-1",
            "rationale": "kept",
            "source_refs": [readable, refused, f"source:{refused}"],
            "provenance": {"source_id": readable, "quote": "the planted metadata quote"},
            "agentic_memory": {"conversation_excerpt": "another planted quote", "source_refs": [refused, readable]},
        },
    }
    before = json.dumps(revision, sort_keys=True)
    audit_ids = cited_source_ids_in_memory_audit({"memory": row, "revisions": [revision]})
    assert refused in audit_ids.named, "explain judges the id a revision's metadata holds"
    reader = _reader(store)
    reader.memory(row)
    shown = reader.revision(revision)
    assert shown["metadata_json"] == {
        "proposal_id": "p-1",
        "rationale": "kept",
        "source_refs": [readable],
        "agentic_memory": {"source_refs": [readable]},
    }
    assert refused not in json.dumps(shown)
    assert json.dumps(revision, sort_keys=True) == before, "the stored revision is not changed by the read"
    assert [shown_link["quote"] for shown_link in reader.links(memory_id)] == [None]
    assert link["quote"] == "the planted metadata quote"


def test_a_revision_whose_metadata_names_only_readable_sources_is_the_stored_revision() -> None:
    """A caller who may read every source a revision names, in its values and in its metadata, is handed the stored row as
    the same object, so the change reaches no authorized caller.

    Mutation: build the scrubbed copy of ``metadata_json`` even when nothing is dropped and return it from ``_revision``
    (drop the ``if not refused and not texts: return row`` test): the identity assertion fails.
    """

    store = _MemoryStore()
    readable, _refused = _two_sources(store)
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[readable])
    store.memories[memory_id] = row
    store.add_link(memory_id, readable)
    revision = {**_revision(memory_id, [readable]), "metadata_json": {"source_refs": [readable], "note": "x"}}
    reader = _reader(store)
    reader.memory(row)
    assert reader.revision(revision) is revision
