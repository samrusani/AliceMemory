"""The sources and memories a stored record cites.

Two readers ask what a stored record cites. The saved-quote reader (``vnext_source_fence``) decides what a caller is
shown of a quote by the ids its reference names. The derived-label closure (``vnext_derived_labels``) decides what a
derived row is raised to by the rows it cites, at a read, at an insert, in a relabel, in the migration and the open pass
that repair stored labels, and in ``labels repair``. The two questions are one question, so they have one parser:
``cited_source_ids`` and ``cited_memory_refs`` of the saved-quote reader, and this module is the only place the closure
calls them from.

A report written before derived labels existed keeps whatever its writer copied into it. The consolidation report copies
the ``source_refs`` of the memories it clusters as strings, and a client wrote those strings: ``memory:<id>`` in any case
and with any number of prefixes, ``alice://memories/<id>``, ``urn:uuid:<id>``, an id in braces, with no hyphens or in
capitals, JSON text such as ``{"memory_id": "<id>", "quote": "..."}`` (or a list of such objects, or JSON text inside JSON
text), a source under ``source_id`` or ``source:<id>`` in the same spellings. The closure used to read the plain
``memory:<id>`` and ``source:<id>`` strings and a few id keys, so a report that cited a confidential memory in any other
shape stayed at the label it was stored with, and a restricted caller read the quote and the id. Redacting the memory did
not hide the report either, because the report had no recorded input to be redacted.

``cited_inputs`` reads a whole record (a row's ``metadata_json``, its ``value``) with the reader's rules, so the closure
finds exactly the ids the reader names, wherever they stand in the record. An id is an input when the reader *names* it:
it follows a ``source:``, ``memory:`` or ``alice://`` marker, stands under a reference key (``source_id``,
``source_refs``, ``memory_id``, ``memories``, ``selected_memory_ids`` and the rest of ``SOURCE_REFERENCE_KEYS`` and
``MEMORY_REFERENCE_KEYS``), is the whole of a reference string, or is inside JSON text. The reader's *incidental* ids (an
id under ``origin``, in a sentence, in an outside URL) may be a chunk or a session and are judged only when they name a
stored row; they are not recorded inputs, here or for a source before this module existed.

A ``quote`` or ``conversation_excerpt`` is text taken from a row and names nothing, as the reader reads it. The closure also leaves
the prose of a text key (``text``, ``title``, ``summary``) out of the record it passes in: a promoted copy of a report holds the
whole report as its ``text``, with a ``[memory:<id>]`` after each line, and those words are the report's.

A citation the parser cannot read is not a citation of nothing. Two shapes say that a reference was meant to name a row and
does not, and ``has_unreadable_citation`` finds both:

* a reference that is meant as JSON (it starts with ``{`` or ``[``), does not decode, and holds an id the reader does not
  name. It was cut or damaged after its writer sent it; the reader scans it as text, where a ``memory_id`` key names
  nothing, so the id it holds could be a memory the row quotes. (``{<id>}`` does not decode either, and is an id in braces,
  which the reader names, so it is a citation like any other.);
* a word of a reference that starts with a marker (``memory:``, ``source:``, ``alice://memories/``, ``alice://sources/``)
  and holds an id, when the reader names no id in that word (``memory:{<id>}#chunk-1``, ``source:<id>/page/2``). The writer
  marked it as a citation and the id is in the word, but not in a spelling the reader takes.

The closure leaves such a row unverified (readable by the owner and an unbound admin key only) instead of judging it by the
inputs it could read. Text with no id in it, and a bracket or a marker in a title or a note, are left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from alicebot_api.vnext_source_fence import (
    _ID_IN_TEXT,
    _MIN_ID_CHARS,
    _REFERENCE_KEYS,
    _TEXT_KEYS,
    _TOKEN_BREAK,
    MEMORY_REFERENCE_KEYS,
    _json_container,
    cited_memory_refs,
    cited_source_ids,
)

# A mapping is wrapped under a key that is no reference key, which makes every key of the record an ordinary key. Without
# it the reader would read an ``id`` or ``ref`` at the top of a metadata object as the id of a source, which it does only
# inside an entry of a reference. A list or a string is a reference already and is read as the reader reads one.
_ROOT = "record"


def cited_inputs(*records: object) -> frozenset[tuple[str, str]]:
    """``(kind, canonical id)`` for every source and memory ``records`` name, with kind ``source`` or ``memory``.

    Each record is a mapping, a list or a string, read as ``cited_source_ids`` and ``cited_memory_refs`` read a stored
    reference. A record that is ``None`` or empty names nothing.
    """

    found: set[tuple[str, str]] = set()
    for record in records:
        if not record or not isinstance(record, (Mapping, list, tuple, str)):
            continue
        read = {_ROOT: record} if isinstance(record, Mapping) else record
        found.update(("source", row_id) for row_id in cited_source_ids(read).named)
        found.update(("memory", row_id) for row_id in cited_memory_refs(read).named)
    return frozenset(found)


_CITATION_KEYS = _REFERENCE_KEYS | MEMORY_REFERENCE_KEYS
_MARKERS = ("memory:", "source:", "alice://memories/", "alice://sources/")


def _ids_the_reader_does_not_name(text: str) -> bool:
    """True when ``text`` holds an id (in any spelling the reader scans for) that ``cited_inputs`` does not name."""

    named = {row_id for _kind, row_id in cited_inputs(text)}
    return any(str(UUID(match.group(0))) not in named for match in _ID_IN_TEXT.finditer(text))


def _marked_word_without_a_readable_id(text: str) -> bool:
    """True when a word of ``text`` starts with a citation marker, holds an id, and the reader names no id in the word."""

    lowered = text.lower()
    if not any(marker in lowered for marker in _MARKERS):
        return False
    for word in _TOKEN_BREAK.split(text.strip()):
        if len(word) < _MIN_ID_CHARS or not word.lower().startswith(_MARKERS):
            continue
        if _ID_IN_TEXT.search(word) is not None and not cited_inputs(word):
            return True
    return False


def has_unreadable_citation(*records: object) -> bool:
    """True when a reference in ``records`` was meant to name a row and the reader cannot name it (see the module text).

    Only a reference is judged: a value under one of the reference keys of the reader (``source_refs``, ``memory_id``,
    ``memories`` and the others), an entry of such a list, and JSON text inside JSON text. Ordinary text that starts with a
    bracket (a title, a note) is not a reference and is left alone, and so is a reference with no id in it. The walk keeps
    its own stack, so a ref nested to any depth is read to the end.
    """

    pending: list[tuple[object, bool]] = [(record, False) for record in records]
    while pending:
        node, in_reference = pending.pop()
        if isinstance(node, str):
            if not in_reference or len(node) < _MIN_ID_CHARS:
                continue
            if node.lstrip()[:1] in ("{", "["):
                nested = _json_container(node)
                if nested is not None:
                    pending.append((nested, True))
                elif _ids_the_reader_does_not_name(node):
                    return True
            elif _marked_word_without_a_readable_id(node):
                return True
        elif isinstance(node, Mapping):
            for key, child in node.items():
                key_text = key.lower() if isinstance(key, str) else None
                if key_text in _TEXT_KEYS:
                    # The text of a quote names nothing. A quote that is an object or a list is read like the rest.
                    if isinstance(child, (Mapping, list, tuple)):
                        pending.append((child, False))
                    continue
                pending.append((child, in_reference or key_text in _CITATION_KEYS))
        elif isinstance(node, (list, tuple)):
            pending.extend((child, in_reference) for child in node)
    return False


__all__ = ["cited_inputs", "has_unreadable_citation"]
