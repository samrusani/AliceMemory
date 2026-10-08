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

A review of the next head found a fifth, in the spellings. ``uuid.UUID`` strips whitespace through ``int(..., 16)``, so 31
digits and a space are an id that starts with ``0``; the link writer reads it and the commit route links and fences the
source, and the reader, which stripped a string before it parsed it, named nothing. The tests of section 3c run every
spelling the writer reads, for ids with one to three leading zeros, against the reader.

Each test names the mutation that must fail it. The mutations were made by hand on a copy of the module and the file was
restored by copying the saved original back.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp import evidence_artifacts
from alicebot_api.vnext_source_fence import (
    _ids_in_text,
    cited_source_ids,
    cited_source_ids_in_memory_audit,
    source_rows_including_archived,
    source_uuids_in_ref,
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

# The cost of reading is checked by how it grows, not by a number of seconds, because the seconds depend on the machine and
# on the coverage tracer that CI runs the unit tests under (one run of this file failed there at 1.59 s against a limit of
# 1.5 s that a laptop meets with a wide margin). The same work is done on an input and on one eight times larger. A reader whose
# cost is linear takes about eight times as long on the larger input; one that rescans the text before each id takes about
# sixty-four times as long. The limit is a little over twice the linear ratio, so it sits between them with a margin each way.
# Both runs are measured in CPU seconds of this process, best of three, so a loaded machine slows both alike.
_SMALL_IDS = 1_000
_LARGE_IDS = 8 * _SMALL_IDS
_GROWTH_FACTOR = 2.25
_NOISE_FLOOR_SECONDS = 0.02
_RUNS = 3


def _cpu_seconds(run: Callable[[], object]) -> float:
    started = time.process_time()
    run()
    return time.process_time() - started


def assert_cost_grows_linearly(
    run_small: Callable[[], object], run_large: Callable[[], object], *, size_ratio: int, what: str
) -> None:
    """``run_large`` does the work of ``run_small`` on an input ``size_ratio`` times larger. Fails when it takes more than
    ``_GROWTH_FACTOR * size_ratio`` times as long (never less than a noise floor of 0.02 s for the small input). A slow
    first run of the large input is not repeated: only one that is close to the limit is run again, to rule out noise."""

    small = min(_cpu_seconds(run_small) for _ in range(_RUNS))
    limit = _GROWTH_FACTOR * size_ratio * max(small, _NOISE_FLOOR_SECONDS)
    large = _cpu_seconds(run_large)
    for _ in range(_RUNS - 1):
        if large < limit or large > 3 * limit:
            break
        large = min(large, _cpu_seconds(run_large))
    assert large < limit, (
        f"{what}: {large:.2f} s on the larger input, {small:.2f} s on one {size_ratio} times smaller "
        f"(limit {limit:.2f} s, a linear reader takes about {small * size_ratio:.2f} s)"
    )


def _new_ids(count: int) -> list[str]:
    return [str(uuid4()) for _ in range(count)]


def _zero_led_id(zeros: int) -> str:
    """A UUID whose first ``zeros`` hex digits are ``0`` and whose next one is not, as the id of a source can be."""

    digits = uuid4().hex
    while digits[zeros] == "0":
        digits = uuid4().hex
    return str(UUID("0" * zeros + digits[zeros:]))


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


def _cost_cases(ids_count: int) -> dict[str, tuple[str, set[str], set[str]]]:
    """``(ref, named, incidental)``: a ref string with ``ids_count`` ids, or with runs of prefixes and whitespace that scale
    with it (several hundred thousand characters at 8,000), and the ids it must give."""

    ids = _new_ids(ids_count)
    first = ids[0]
    return {
        "ids joined by commas": (",".join(ids), set(ids), set()),
        "source: words": (",".join("source:" + source_id for source_id in ids), set(ids), set()),
        "JSON text of ids": (json.dumps({"source_ids": ids}), set(ids), set()),
        "a sentence with a marker before each id": (" ".join("see source: " + i for i in ids), set(), set(ids)),
        "outside URLs": (" ".join("https://host.example/sources/" + i for i in ids), set(), set(ids)),
        "a run of source: prefixes": ("source:" * (50 * ids_count), set(), set()),
        "a run of source: prefixes and then an id": ("source:" * (37 * ids_count) + first, {first}, set()),
        "a run of whitespace and then an id": (" " * (250 * ids_count) + first, {first}, set()),
    }


_COST_CASES = list(_cost_cases(1))


@pytest.mark.parametrize("label", _COST_CASES)
def test_a_ref_string_is_read_in_time_that_grows_with_its_length(label: str) -> None:
    """Each shape of a long ref string (8,000 ids, or megabytes of prefixes and whitespace) is read in time that grows with
    its length (the larger input takes no more than about twice as many times as long as it is larger) and gives exactly
    the ids it holds, so a parser that is fast because it stops early fails the second assertion.

    Mutations, each alone, in ``vnext_source_fence.py``: add ``_REFLOW.search(text, 0, start)`` with
    ``_REFLOW = re.compile(r"memory:$", re.IGNORECASE)`` to the loop of ``_ids_in_text``, which rescans the text before
    each id as the shipped reader did (every shape with thousands of ids fails on growth); restore the
    ``while candidate[:7].lower() == "source:": candidate = candidate[7:].strip()`` loop of the previous ``_whole_id`` in
    place of the prefix regex (the two runs of prefixes fail on growth).
    """

    ref, _named, _incidental = _cost_cases(_SMALL_IDS)[label]
    large_ref, named, incidental = _cost_cases(_LARGE_IDS)[label]
    cited = cited_source_ids(large_ref)
    assert (set(cited.named), set(cited.incidental)) == (named, incidental)
    assert_cost_grows_linearly(
        lambda: cited_source_ids(ref),
        lambda: cited_source_ids(large_ref),
        size_ratio=_LARGE_IDS // _SMALL_IDS,
        what=f"{label} ({len(large_ref):,} characters)",
    )


def _huge_ref_memory(ids_count: int) -> tuple[Callable[[], object], int]:
    """A memory (the proposal door stores whatever ``source_refs`` it is sent) whose metadata, ``agentic_memory``, ``value``,
    a revision and an audit event all hold a ref of ``ids_count`` ids, as a function that reads all of it once with a fresh
    reader, and the number of ids the audit must name."""

    store = _MemoryStore()
    readable = store.add_source()
    huge = ",".join(_new_ids(ids_count))
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
    results: dict[str, object] = {}

    def read() -> None:
        reader = _reader(store)
        results["memory"] = reader.memory(row)
        results["revision"] = reader.revision(revision)
        results["links"] = reader.links(memory_id)
        results["cited"] = cited_source_ids_in_memory_audit(audit)
        results["readable"] = readable

    read()
    shown = results["memory"]
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == [readable]  # type: ignore[index]
    assert results["revision"]["metadata_json"]["source_refs"] == [readable]  # type: ignore[index]
    assert [link["quote"] for link in results["links"]] == [None]  # type: ignore[union-attr]
    return read, len(results["cited"].named)  # type: ignore[attr-defined]


def test_the_reader_and_the_audit_read_a_memory_whose_ref_is_huge_in_time_that_grows_with_its_size() -> None:
    """The end to end of the first case. A memory (the proposal door stores whatever ``source_refs`` it is sent) holds a
    ref of thousands of ids in its metadata, in ``agentic_memory``, in ``value`` and in a revision, and the audit that
    ``alice_explain`` judges holds it in its events as well. The reader (memory, revision and links) and the audit scan
    together grow linearly with the size of the ref, the entry that names the refused ids is dropped, and the readable
    source stays.

    Mutation: the first mutation of the test above (rescan from offset 0 for each id).
    """

    read_small, small_named = _huge_ref_memory(_SMALL_IDS)
    read_large, large_named = _huge_ref_memory(_LARGE_IDS)
    assert (small_named, large_named) == (_SMALL_IDS + 1, _LARGE_IDS + 1)
    assert_cost_grows_linearly(
        read_small, read_large, size_ratio=_LARGE_IDS // _SMALL_IDS, what="the reader and the audit on a huge ref"
    )


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
        for attempt in (candidate, candidate.strip()):
            try:
                return str(UUID(attempt.lower()))
            except ValueError:
                continue
        return None

    def after_prefixes(candidate: str) -> str:
        while True:
            found = re.match(r"\s*source:", candidate, re.IGNORECASE)
            if found is None:
                return candidate
            candidate = candidate[found.end() :]

    def whole_id(candidate: str) -> str | None:
        for attempt in (candidate, candidate.strip()):
            found = uuid_of(after_prefixes(attempt))
            if found is not None:
                return found
        return None

    named: set[str] = set()
    incidental: set[str] = set()
    whole = whole_id(text)
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
                rest, explicit = after_prefixes(word), True
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
        # An id that starts with 0, written with a whitespace character in the place of the 0 (31 digits and one space):
        # ``uuid.UUID`` reads it, and a read that strips first does not.
        lambda i: " " + i.replace("-", "")[1:],
        lambda i: "\t" + i[1:],
        lambda i: i.replace("-", "")[1:] + "\n",
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
    pool = [*_new_ids(30), *(_zero_led_id(zeros) for zeros in (1, 1, 1, 2, 2, 3, 1, 1, 2, 1))]
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
        texts.append(rng.choice(("", " ", "  ")) + text + rng.choice(("", " ", ",", "\n")))
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


# -- 3b. every id the link writer reads ------------------------------------------------------------------------------


def _zero_led_spellings(source_id: str) -> tuple[str, ...]:
    """The strings ``uuid.UUID`` reads as ``source_id`` that are not the 32 digits with its zeros: for an id whose first
    ``k`` digits are ``0`` each drops ``k`` leading zeros and puts ``k`` characters in their place (whitespace, ``0x``, a
    sign, an underscore), and for any other id the plain spelling. ``uuid.UUID`` hands the string to ``int(..., 16)``, which
    strips whitespace and takes ``0x``, ``+`` and ``_``, so every one of these names the id."""

    digits = source_id.replace("-", "")
    zeros = len(digits) - len(digits.lstrip("0"))
    if zeros == 0:
        return (source_id,)
    short = digits[1:]
    spellings = [
        " " + short,
        "\t" + short,
        short + "\n",
        "\u00a0" + short,
        " " + source_id[1:],
        "+" + short,
        short[:9] + "_" + short[9:],
        "source: " + short,
        "source:\t" + short,
        "source: " + source_id[1:],
        "source: " + short + "\n",
        "source:" + short + " ",
    ]
    if zeros >= 2:
        spellings += ["0x" + digits[2:], "0X" + digits[2:], " " * 2 + digits[2:], digits[2:] + "\n\t"]
    return tuple(spellings)


def _random_ref(rng: random.Random, pool: list[str], depth: int = 0, used: list[str] | None = None) -> object:
    """A ref value in the shapes the link writer reads (strings, lists, objects under ``source_id``, ``id``, ``ref``,
    ``source_ref``, ``source_ids``, ``source_refs`` and ``sources``) and in shapes it ignores (other keys, labels, ints).
    Each string of ``_zero_led_spellings`` that is used is appended to ``used``."""

    def spelled(source_id: str) -> str:
        zero_led = _zero_led_spellings(source_id)
        choice = rng.choice(
            (
                source_id,
                source_id.upper(),
                source_id.replace("-", ""),
                "{" + source_id + "}",
                "urn:uuid:" + source_id,
                "uuid:" + source_id,
                "  " + source_id + "  ",
                "source:" + source_id,
                "source:" + source_id.upper(),
                "source:" + source_id.replace("-", ""),
                *zero_led,
            )
        )
        if used is not None and len(zero_led) > 1 and choice in zero_led:
            used.append(choice)
        return choice

    roll = rng.random()
    if depth > 3 or roll < 0.35:
        leaf = rng.random()
        if leaf < 0.7:
            return spelled(rng.choice(pool))
        if leaf < 0.8:
            return rng.choice(("notes", "https://x.test/y", "memory:" + rng.choice(pool), "chunk-1", 7, None, True))
        return f"{spelled(rng.choice(pool))} {spelled(rng.choice(pool))}"
    if roll < 0.6:
        return [_random_ref(rng, pool, depth + 1, used) for _ in range(rng.randint(0, 4))]
    keys = ("source_id", "id", "ref", "source_ref", "source_ids", "source_refs", "sources", "origin", "other", "chunk_id")
    return {rng.choice(keys): _random_ref(rng, pool, depth + 1, used) for _ in range(rng.randint(1, 3))}


def test_every_id_the_link_writer_reads_is_named_by_the_reader() -> None:
    """The commit route makes its link, and the write fence checks its source, from the ids ``source_uuids_in_ref`` reads. The
    reader must name every one of them, in whatever shape the ref was stored, or a link could exist for a source the reader
    does not judge as a reference. 6,000 random refs built from the shapes the writer reads (nested lists and objects under
    the keys it reads, padding, braces, ``urn:uuid:``, ``uuid:``, ``source:`` in either case, and for ids that start with
    zeros the spellings with whitespace, ``0x``, a sign or an underscore in the place of the zeros) and from shapes it
    ignores. More than 300 of them must hold the writer's read of such a spelling.

    Mutations, each alone, in ``vnext_source_fence.py``: read a whole-string id as incidental when it is read as a ref
    (``incidental.add(whole)`` in ``_ids_in_text``); skip the second entry of a list in ``cited_source_ids`` (``node[:1]``);
    read only ``text.strip()`` in ``_uuid_text`` (``for candidate in (text.strip(),)``: the spellings with whitespace in the
    place of a zero fail).
    """

    rng = random.Random(544)
    pool = [*_new_ids(24), *(_zero_led_id(zeros) for zeros in (1, 1, 1, 2, 2, 3))]
    zero_led = set(pool[24:])
    used: list[str] = []
    covered = 0
    for _ in range(6_000):
        before = len(used)
        value = _random_ref(rng, pool, used=used)
        written = set(source_uuids_in_ref(value))
        assert written <= set(cited_source_ids(value).named), value
        covered += len(used) > before and bool(written & zero_led)
    assert covered > 300, "the refs must hold the writer's reads of ids that start with 0 for the check above to cover them"


# -- 3c. ids that start with 0 -----------------------------------------------------------------------------------------

_FIXED_ZERO_LED = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_an_id_that_starts_with_zero_and_is_written_with_a_space_in_place_of_the_zero_is_named_by_the_reader() -> None:
    """The case of the outside review of the second head. ``uuid.UUID`` hands a string of 32 characters to ``int(..., 16)``,
    which strips whitespace, so ``" "`` and the 31 other digits of ``0a1b2c3d-...`` is that id. The link writer reads it
    (``source_uuids_in_ref`` removes only a lower case ``source:`` and does not strip what follows) and the commit route
    stores a ref with it, so the memory names a source that the reader did not name: the reader stripped the string before
    it handed it to ``uuid.UUID`` and was left with 31 characters.

    Mutation: in ``_uuid_text`` read only ``text.strip()`` (``for candidate in (text.strip(),)``): the reader names nothing
    for the four refs and the property tests below fail.
    """

    zero_led = _FIXED_ZERO_LED
    other = str(uuid4())
    short = zero_led.replace("-", "")[1:]
    ref = {"source_ids": [other, "source: " + short]}
    assert zero_led in source_uuids_in_ref(ref), "the writer reads the id"
    assert zero_led in cited_source_ids([ref]).named, "so the reader names it"
    for label, spelled in {
        "source: and a space": {"source_ids": [other, "source: " + short]},
        "source: and a tab": {"source_ids": [other, "source:\t" + short]},
        "hyphens kept": {"source_ids": [other, "source: " + zero_led[1:]]},
        "nested objects": {"source_refs": [{"source_id": other}, {"source_id": "source: " + short}]},
        "own id key": {"source_id": other, "id": " " + short},
        "a newline after the id, stripped first by the writer": ["source: " + short + "\n"],
    }.items():
        assert zero_led in source_uuids_in_ref(spelled), label
        assert zero_led in cited_source_ids(spelled).named, label


def _zero_led_grid() -> list[tuple[str, str, int, object]]:
    """``(label, id, spelling index, ref)`` for every spelling of an id with 1 to 3 leading zeros, in each of several
    prefixes and containers."""

    prefixes = ("", "source:", "source: ", "source:\t", "SOURCE:", "Source: ", "source:source:", "source: source: ", " source: ")
    containers = (
        lambda s: s,
        lambda s: [s],
        lambda s: {"source_id": s},
        lambda s: {"id": s},
        lambda s: {"ref": s},
        lambda s: {"source_ref": s},
        lambda s: {"source_ids": [s]},
        lambda s: {"sources": [{"id": s}]},
        lambda s: {"source_refs": [{"source_id": s}]},
        lambda s: [[{"source_ids": ["00000000-0000-4000-8000-000000000001", s]}]],
    )
    cases: list[tuple[str, str, int, object]] = []
    for zeros in (1, 2, 3):
        source_id = _zero_led_id(zeros)
        for index, spelling in enumerate(_zero_led_spellings(source_id)):
            for prefix in prefixes:
                for build_index, build in enumerate(containers):
                    label = f"{zeros} zeros, spelling {index}, prefix {prefix!r}, container {build_index}"
                    cases.append((label, source_id, index, build(prefix + spelling)))
    return cases


def test_every_spelling_of_an_id_with_leading_zeros_that_the_link_writer_reads_is_named_by_the_reader() -> None:
    """The grid behind the case above: for ids with one, two and three leading zeros, every spelling ``uuid.UUID`` reads
    (whitespace of any kind in place of the zeros, before or after the digits, with ``source:`` and a space or a tab after
    it, hyphens kept, ``0x``, a sign, an underscore), in each of nine prefixes and ten containers. Wherever the writer reads
    the id the reader names it. The check is not vacuous: the writer must read the id in several hundred of the cases, and
    every spelling in at least one of them.

    Mutations, each alone, in ``vnext_source_fence.py``: read only ``text.strip()`` in ``_uuid_text`` (the cases with a
    space or a newline in the place of a zero fail); in ``_whole_id`` try only ``text`` and not ``text.strip()`` (the
    cases where the writer strips before it removes the prefix fail: a newline after the id, with ``source:`` and a
    space in front); let ``_source_prefixes_end`` take the whitespace after the prefix again (skip whitespace after each prefix as well as before it) (the
    cases with ``source:`` and a space or a tab in place of the zero fail).
    """

    grid = _zero_led_grid()
    live: set[tuple[str, int]] = set()
    written_cases = 0
    for label, source_id, index, ref in grid:
        if source_id not in source_uuids_in_ref(ref):
            continue
        written_cases += 1
        live.add((source_id, index))
        assert source_id in cited_source_ids(ref).named, (label, ref)
    assert written_cases > 500, written_cases
    assert live == {(source_id, index) for _label, source_id, index, _ref in grid}, (
        "a spelling the writer never reads is not a test of the reader"
    )


@pytest.mark.parametrize("zeros", [1, 3])
@pytest.mark.parametrize(
    "build_ref",
    [
        pytest.param(lambda a, b: {"source_ids": [a, "source: " + b.replace("-", "")[1:]]}, id="source and a space"),
        pytest.param(lambda a, b: {"source_ids": [a, "source:\t" + b[1:]]}, id="source and a tab, hyphens kept"),
        pytest.param(lambda a, b: {"source_id": a, "id": " " + b.replace("-", "")[1:]}, id="a space under an id key"),
        pytest.param(lambda a, b: {"source_ids": [a, "source: " + b.replace("-", "")[1:] + "\n"]}, id="a newline after the id"),
    ],
)
def test_a_memory_that_names_a_source_with_a_leading_zero_by_a_spelling_with_whitespace_is_judged_by_that_source(
    zeros: int, build_ref: object
) -> None:
    """The reader, with a stub store. A commit with ``{"source_ids": [A, <spelling of B>]}`` links A only and saves the
    excerpt as the quote of that link and as its own copy. When B is above the key's ceiling the copy, the ref and the quote
    on A's link are withheld, the audit of ``alice_explain`` names B, and a key that may read B is shown the stored row as
    the same object. B's id starts with zeros, so before this change the reader did not name it and judged nothing.

    Mutation: the one of the test above (``_uuid_text`` reads only the stripped text): the quote stays on the link and the
    row is returned whole.
    """

    store = _MemoryStore()
    readable = store.add_source()
    refused_id = store.add_source(sensitivity="confidential")
    source_row = store.sources.pop(refused_id)
    refused = _zero_led_id(zeros)
    store.sources[refused] = {**source_row, "id": refused}
    ref = build_ref(readable, refused)  # type: ignore[operator]
    memory_id = str(uuid4())
    row = _row(memory_id, refs=[ref])
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable)
    assert refused in source_uuids_in_ref(ref)
    shown = _reader(store).memory(row)
    assert "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"]  # type: ignore[index]
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == []  # type: ignore[index]
    assert [shown_link["quote"] for shown_link in _reader(store).links(memory_id)] == [None]
    assert refused in cited_source_ids_in_memory_audit({"memory": row}).named
    store.sources[refused]["sensitivity"] = "internal"
    readable_reader = _reader(store)
    assert readable_reader.memory(row) is row
    assert readable_reader.links(memory_id)[0] is link


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
