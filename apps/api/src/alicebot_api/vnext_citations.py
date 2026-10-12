"""The ids a stored record may cite, found with the parser of the saved-quote reader.

Two readers ask what a stored record cites. The saved-quote reader (``vnext_source_fence``) decides what a caller is
shown of a quote by the ids its reference names. The derived-label closure (``vnext_derived_labels``) decides what a
derived row is raised to by the rows it cites, at a read, at an insert, in a relabel, in the migrations and the open pass
that repair stored labels, and in ``labels repair``. The two ask one question, and the closure asks it with the reader's
own functions, ``cited_source_ids`` and ``cited_memory_refs``.

A report written before derived labels existed keeps whatever its writer copied into it. The consolidation report copies
the ``source_refs`` of the memories it clusters as strings, and a client wrote those strings: ``memory:<id>`` in any case
and with any number of prefixes, ``alice://memories/<id>``, ``urn:uuid:<id>``, an id in braces, with no hyphens or in
capitals, JSON text such as ``{"memory_id": "<id>", "quote": "..."}`` (or a list of such objects, or JSON text inside JSON
text), an id in a sentence, in an outside URL, under a key such as ``origin``. A report that cited a confidential memory in
any of these shapes kept the label it was stored with, so a caller with a lower ceiling read the quote and the id, and
redacting the memory did not hide the report either, because the report had no recorded input to be redacted.

The closure has two kinds of input. The recorded ones (``vnext_derived_labels``) are the keys that name a row
(``source_id``, ``derived_from``, ``cluster_member_ids`` and the rest), the strings every producer writes in
``source_refs`` (``memory:<id>`` and ``source:<id>``) and the sources the reader names there; its ``_source_ids_from``
calls ``cited_source_ids`` for them. A row that records an input that is not stored is unverified. This module finds the
rest, by resolve or ignore: an id is a candidate when the reader finds it, a candidate is an input when it names a stored
memory or source of the same user, and it is ignored when it names none. There are two places to look.

* A *reference position*: a ``source_refs``, ``provenance`` or ``replacement_provenance`` value, at any depth. Every id the
  reader finds there is a candidate, the ones it names and the incidental ones (an id under ``origin``, in a sentence, in
  ``mem:<id>``, in an outside URL, in a Markdown link, in JSON text that does not decode). These are the positions where
  the producers copy a client's references, and they read them the same way (``memories_named_by_refs`` and
  ``sources_named_by_refs`` resolve ``.every``).
* Anywhere else in the metadata and the value, only an id the reader *names* is a candidate: it follows a ``memory:``,
  ``source:`` or ``alice://`` marker, stands under a reference key (``source_id``, ``memory_ids``, ``selected_memory_ids``
  and the rest of ``SOURCE_REFERENCE_KEYS`` and ``MEMORY_REFERENCE_KEYS``) or is inside JSON text. The bare ids of a
  ``trace_id`` or an ``agent_run_id`` are not candidates, and neither is the prose of a text key (``text``, ``title``,
  ``summary``): a promoted copy of a report holds the whole report as its ``text``, with a ``[memory:<id>]`` after each
  line, and those words are the report's.

A word that marks itself as a citation (``memory:`` or ``source:`` or an ``alice://`` URL) and holds an id in a spelling
the scan cannot take apart (hyphens in places ``uuid.UUID`` accepts and the reader's pattern does not) is *unreadable*: its id
is held apart, and the closure leaves the row unverified only when that id resolves to a stored row. Syntax alone never
makes a row unverified.

The scan is one pass over the record. A string in the one spelling every producer writes (``memory:<id>``, ``source:<id>``
or a bare id, all in lower case) is taken without the reader; any other string that can hold a citation goes to the reader.
What the reader finds in a string is a function of the string alone, so it is kept for the next record that holds the same
string (``read_reference``): a report is read again at every request, and its strings do not change. A record with no such
string costs one walk.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from alicebot_api.vnext_source_fence import (
    _MIN_ID_CHARS,
    _REFERENCE_KEYS,
    _TOKEN_BREAK,
    MEMORY_REFERENCE_KEYS,
    _uuid_text,
    cited_memory_refs,
    cited_source_ids,
)

# Where a client's references live in a record. Every id the reader finds under these keys is a candidate.
REFERENCE_POSITION_KEYS = frozenset({"source_refs", "provenance", "replacement_provenance"})
# The keys under which the reader reads an id as a citation by position.
CITATION_KEYS = _REFERENCE_KEYS | MEMORY_REFERENCE_KEYS

_CANONICAL = re.compile(r"(?:memory:|source:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MARKERS = ("memory:", "source:", "alice://memories/", "alice://sources/")
# Where the id of a marked word ends: a fragment, a path, a query, a closing bracket or quote.
_WORD_END = re.compile(r"[#/?)\]>\"'\\]")


@dataclass(frozen=True, slots=True)
class Citations:
    """What a record may cite, as ``(kind, canonical id)`` with kind ``memory`` or ``source``, and what it holds in a word
    the scan cannot take apart.

    The kind is the one the reader reads the id as: an incidental id of a ``memory:`` word or of a quote is a memory the
    reader judges when the store holds a row for it, and never a source, as ``sources_named_by_refs`` and
    ``memories_named_by_refs`` look the two kinds up apart.
    """

    refs: frozenset[tuple[str, str]] = frozenset()
    unreadable: frozenset[tuple[str, str]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.refs or self.unreadable)


NO_CITATIONS = Citations()


@dataclass(frozen=True, slots=True)
class Reading:
    """What the reader finds in one string of a reference position.

    ``refs`` is every id it finds, named or incidental, with the kind it is read as. ``named_sources`` are the sources it
    names, which the recorded inputs of a row take. ``unreadable`` are the ids of marked words that it does not find.
    """

    refs: frozenset[tuple[str, str]]
    named_sources: frozenset[str]
    unreadable: frozenset[tuple[str, str]]


# A string longer than this is read each time and not kept: the cache is for the short references a report copies.
_KEPT_CHARS = 2048
_KEPT_STRINGS = 4096


def _read(text: str) -> Reading:
    unit = [text]
    source = cited_source_ids(unit)
    memory = cited_memory_refs(unit)
    refs = frozenset({("source", row_id) for row_id in source.every} | {("memory", row_id) for row_id in memory.every})
    unreadable = frozenset(taken for taken in _marked_ids(text) if taken not in refs)
    return Reading(refs, frozenset(source.named), unreadable)


@lru_cache(maxsize=_KEPT_STRINGS)
def _read_kept(text: str) -> Reading:
    return _read(text)


_NOTHING = Reading(frozenset(), frozenset(), frozenset())


def read_reference(text: str) -> Reading:
    """What the saved-quote reader finds in a string that is a reference (an entry of ``source_refs``)."""

    if len(text) < _MIN_ID_CHARS:
        return _NOTHING  # too short to hold an id in any spelling
    if len(text) > _KEPT_CHARS:
        return _read(text)
    return _read_kept(text)


def clear_reading_cache() -> None:
    _read_kept.cache_clear()


def _worth_the_reader(text: str) -> bool:
    """True when a string outside a reference position can name a memory: it is JSON text, or it holds a marker."""

    first = text[0]
    if first == "{" or first == "[":
        return True
    if first.isspace() and text.lstrip()[:1] in ("{", "["):
        return True
    if ":" in text:
        lowered = text.lower()
        return "memory:" in lowered or "alice://memories/" in lowered
    return False


def _strings_of(entry: object) -> list[str]:
    """Every string of a nested entry, for the words that mark themselves as a citation."""

    found: list[str] = []
    pending = [entry]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, Mapping):
            pending.extend(node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return found


def _marked_ids(text: str) -> set[tuple[str, str]]:
    """The ``(kind, id)`` that the marked words of ``text`` hold, cut where a fragment, a path or a bracket starts.

    ``memory:{<id>}#chunk-1`` holds the id in braces before a fragment; ``uuid.UUID`` takes the id with its hyphens in any
    places, which the reader's pattern does not, and a word that marks itself as a citation and holds such an id is a citation
    the scan cannot take apart.
    """

    lowered = text.lower()
    if not any(marker in lowered for marker in _MARKERS):
        return set()
    found: set[tuple[str, str]] = set()
    for word in _TOKEN_BREAK.split(text.strip()):
        if len(word) < _MIN_ID_CHARS:
            continue
        rest = word
        kind = ""
        while True:
            lowered_rest = rest.lower()
            for marker in _MARKERS:
                if lowered_rest.startswith(marker):
                    rest = rest[len(marker) :].lstrip()
                    kind = "memory" if "memor" in marker else "source"
                    break
            else:
                break
        if not kind:
            continue
        token = _WORD_END.split(rest.strip(" {([<"), maxsplit=1)[0]
        taken = _uuid_text(token) if len(token) >= _MIN_ID_CHARS else None
        if taken is not None:
            found.add((kind, taken))
    return found


def scan_citations(records: Sequence[object], *, prose: frozenset[str] = frozenset()) -> Citations:
    """The ids the records (a row's metadata and value, as plain JSON values) may cite. See the module text.

    ``prose`` names the keys whose string values are text and not references, outside a reference position.
    """

    refs: set[tuple[str, str]] = set()
    unreadable: set[tuple[str, str]] = set()
    in_references: list[object] = []
    seen: set[str] = set()
    named: list[tuple[str, str]] = []
    keyed: list[tuple[str, str]] = []
    # (node, nearest ancestor key that makes its children citations, "" when there is none)
    pending: list[tuple[object, str]] = [(record, "") for record in records if record]
    while pending:
        node, cited_by = pending.pop()
        if type(node) is dict or isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    continue
                lowered = key if key.islower() else key.lower()
                if lowered in REFERENCE_POSITION_KEYS:
                    for entry in child if type(child) is list else (child,):
                        if type(entry) is str:
                            if len(entry) < _MIN_ID_CHARS:
                                continue
                            if _CANONICAL.fullmatch(entry):
                                _take_canonical(entry, refs)
                            elif entry not in seen:
                                seen.add(entry)
                                in_references.append(entry)
                        elif entry is not None and type(entry) not in (int, float, bool):
                            in_references.append(entry)
                    continue
                inside = cited_by or (lowered if lowered in CITATION_KEYS else "")
                if inside and len(key) >= _MIN_ID_CHARS:
                    keyed.append((inside, key))
                kind = type(child)
                if kind is str:
                    if len(child) < _MIN_ID_CHARS:
                        continue
                    if inside:
                        if _CANONICAL.fullmatch(child):
                            _take_both(child[-36:], refs)
                        else:
                            named.append((inside, child))
                    elif lowered not in prose and _worth_the_reader(child):
                        named.append(("", child))
                elif kind is dict or kind is list:
                    pending.append((child, inside))
                elif isinstance(child, (Mapping, list, tuple)):
                    pending.append((child, inside))
        elif type(node) is list or isinstance(node, (list, tuple)):
            for child in node:
                kind = type(child)
                if kind is str:
                    if len(child) < _MIN_ID_CHARS:
                        continue
                    if cited_by:
                        if _CANONICAL.fullmatch(child):
                            _take_both(child[-36:], refs)
                        else:
                            named.append((cited_by, child))
                    elif _worth_the_reader(child):
                        named.append(("", child))
                elif kind is dict or kind is list or isinstance(child, (Mapping, list, tuple)):
                    pending.append((child, cited_by))
    for entry in in_references:
        if type(entry) is str:
            reading = read_reference(entry)
            refs |= reading.refs
            unreadable |= reading.unreadable
            continue
        unit = [entry]
        found = {("source", row_id) for row_id in cited_source_ids(unit).every}
        found |= {("memory", row_id) for row_id in cited_memory_refs(unit).every}
        refs |= found
        for text in _strings_of(entry):
            for taken in _marked_ids(text):
                if taken not in found:
                    unreadable.add(taken)
    done: set[tuple[str, str]] = set()
    for cited_by, text in named:
        if (cited_by, text) in done:
            continue
        done.add((cited_by, text))
        skeleton = {cited_by or "value": text}
        found = {("source", row_id) for row_id in cited_source_ids(skeleton).named}
        found |= {("memory", row_id) for row_id in cited_memory_refs(skeleton).named}
        refs |= found
        if cited_by:
            for taken in _marked_ids(text):
                if taken not in found:
                    unreadable.add(taken)
    for cited_by, key in keyed:
        under_key: dict[str, object] = {cited_by: {key: None}}
        refs |= {("source", row_id) for row_id in cited_source_ids(under_key).named}
        refs |= {("memory", row_id) for row_id in cited_memory_refs(under_key).named}
    if not refs and not unreadable:
        return NO_CITATIONS
    return Citations(frozenset(refs), frozenset(unreadable))


def _take_both(row_id: str, refs: set[tuple[str, str]]) -> None:
    refs.add(("source", row_id))
    refs.add(("memory", row_id))


def _take_canonical(entry: str, refs: set[tuple[str, str]]) -> None:
    """A string in the one spelling every producer writes, in a reference position.

    ``memory:<id>`` names a memory and, to the reader, no source. ``source:<id>`` names a source, and the reader also
    finds the id as an incidental memory. A bare id is a source to the reader and an incidental memory.
    """

    row_id = entry[-36:]
    if entry[0] == "m":
        refs.add(("memory", row_id))
    else:
        _take_both(row_id, refs)


__all__ = [
    "CITATION_KEYS",
    "NO_CITATIONS",
    "REFERENCE_POSITION_KEYS",
    "Citations",
    "Reading",
    "clear_reading_cache",
    "read_reference",
    "scan_citations",
]
