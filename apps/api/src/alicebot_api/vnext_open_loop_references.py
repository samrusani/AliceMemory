"""Which references of an open loop its reader may be shown.

An open loop keeps the ``source_id`` and ``memory_id`` it was created with in columns of its own, and its
``metadata_json`` can name sources and memories as well (the daily brief writes ``source_id`` there). Every reader that
returned the row whole returned those ids too, to anyone who could read the loop. The write door
(``vnext_source_fence``) holds a new reference to the *writer's* read fence, once, when the loop is made. It says
nothing about who reads the loop later, and it never touched a loop saved before it existed. So a loop an
``admin_agent`` key made over a confidential source gave that source's id to every key of the project that could read
the loop, and so did a loop saved before the write door.

The rule here is the reader's own read fence, applied to the row on its way out. A reference is shown only if
``SourceReadFence`` admits the row it names, the same test ``alice_explain`` applies to each source and memory it
discloses and the write door applies to each id it stores. Everything else is withheld the same way a reference to
nothing is: a reference to a source that is protected, deleted, missing or not an id at all reads alike.

* The ``source_id`` and ``memory_id`` columns keep their key and become ``None``, the value a loop with no reference
  has, so the row keeps the shape it has when nothing is linked.
* ``metadata_json`` loses the id. Under a key that names a reference (``source_id``, ``source_refs`` and the others
  in ``SOURCE_REFERENCE_KEYS`` and ``MEMORY_REFERENCE_KEYS``, at any depth, and everything nested below one) every id
  must be a source or memory the fence admits, and an id that names no row at all goes with the rest. Anywhere else
  in the metadata an id goes only when it names a row the fence refuses, because an id under an unknown key may be a
  run id or a trace id, which names no stored row and is not a reference. A string that is only an id is removed with
  its key or list slot, and an id inside longer text is replaced by ``(id withheld)``.
* An id is read in every spelling the link writer reads (upper case, no hyphens, braces, ``urn:uuid:``, a ``source:``
  or ``memory:`` prefix) and looked up in canonical form. A column value that is no id in any of those spellings is
  withheld as a missing one is. What is shown is the stored value, untouched.

``fence`` is a required keyword-only argument of every function here, with no default. ``SourceReadFence.unfenced()``
is the owner's fence, written at the call site: it admits any live row and refuses a deleted one, so a loop that
points at a deleted source shows no id to anyone.

A store that cannot look rows up by id (no ``get_sources_by_ids`` and no ``get_source``, or no ``get_memories_by_ids``
and no ``get_memory``) admits nothing, so every reference is withheld. The two reads are one batched statement each,
at most ``_LOOKUP_BATCH`` ids at a time, and are skipped when the rows name nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from alicebot_api.vnext_source_fence import SOURCE_REFERENCE_KEYS, SourceReadFence

JsonObject = dict[str, object]

# ``SOURCE_REFERENCE_KEYS`` (defined in ``vnext_source_fence``, which the saved-quote reader also reads) holds the keys
# the open-loop reverse lookup of a source reads (``list_open_loops_referencing_source``), so a loop that names a
# source under one of them is found by the id and withheld by the same words.
# No writer in the product puts a memory id into the metadata of a loop. The keys are the spellings a writer would
# reach for, and the scan for any id that names a refused row is the net for the rest.
MEMORY_REFERENCE_KEYS = frozenset({"memory_id", "memory_ids", "memory_ref", "memory_refs", "source_memory_ids"})

_REFERENCE_KEYS = SOURCE_REFERENCE_KEYS | MEMORY_REFERENCE_KEYS
_ID_WITHHELD = "(id withheld)"
# A SQLite build from before 3.32 allows 999 bound variables, so ids are looked up in slices.
_LOOKUP_BATCH = 500
# Metadata the product writes is a few levels deep. Recursing without a bound on text a store let through could raise
# before it finished, so the scan stops here and drops what is below.
_METADATA_MAX_DEPTH = 64
_UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DROPPED = object()


def withhold_unreadable_references(
    store: object,
    loops: Sequence[Mapping[str, object]],
    *,
    fence: SourceReadFence,
) -> list[JsonObject]:
    """Copies of ``loops`` that name only the sources and memories ``fence`` admits.

    The rows that come in are not changed. A row with nothing to withhold comes back equal to what went in.
    """

    rows: list[JsonObject] = [dict(loop) for loop in loops]
    if not rows:
        return rows
    source_ids: set[str] = set()
    memory_ids: set[str] = set()
    metadata_ids: set[str] = set()
    for row in rows:
        source = _canonical_id(row.get("source_id"))
        if source is not None:
            source_ids.add(source)
        memory = _canonical_id(row.get("memory_id"))
        if memory is not None:
            memory_ids.add(memory)
        _collect_ids(row.get("metadata_json"), metadata_ids, depth=0)
    source_rows = _rows_by_id(store, sorted(source_ids | metadata_ids), bulk="get_sources_by_ids", single="get_source")
    memory_rows = _rows_by_id(store, sorted(memory_ids | metadata_ids), bulk="get_memories_by_ids", single="get_memory")
    admitted_sources = frozenset(key for key, row in source_rows.items() if fence.admits(row))
    admitted_memories = frozenset(key for key, row in memory_rows.items() if fence.admits_memory(row))
    refused = frozenset((set(source_rows) - admitted_sources) | (set(memory_rows) - admitted_memories))
    admitted = admitted_sources | admitted_memories
    for row in rows:
        if row.get("source_id") is not None and _canonical_id(row["source_id"]) not in admitted_sources:
            row["source_id"] = None
        if row.get("memory_id") is not None and _canonical_id(row["memory_id"]) not in admitted_memories:
            row["memory_id"] = None
        metadata = row.get("metadata_json")
        if isinstance(metadata, Mapping):
            scrubbed = _scrub(metadata, strict=False, depth=0, admitted=admitted, refused=refused)
            if scrubbed is _DROPPED:
                scrubbed = {}
            if scrubbed != metadata:
                row["metadata_json"] = scrubbed
    return rows


def withhold_unreadable_references_from_loop(
    store: object,
    loop: Mapping[str, object],
    *,
    fence: SourceReadFence,
) -> JsonObject:
    """The one-row form of ``withhold_unreadable_references``, for a reader that returns one loop."""

    return withhold_unreadable_references(store, [loop], fence=fence)[0]


def withhold_unreadable_references_from_pack(
    store: object,
    pack: Mapping[str, object],
    *,
    fence: SourceReadFence,
) -> JsonObject:
    """A copy of a compiled context pack whose ``open_loops`` name only what ``fence`` admits.

    The pack's other sections are not changed here: its loops are the only rows it returns whole with their ids.
    """

    copied: JsonObject = dict(pack)
    loops = pack.get("open_loops")
    if isinstance(loops, Sequence) and not isinstance(loops, (str, bytes, bytearray)):
        copied["open_loops"] = withhold_unreadable_references(
            store, [loop for loop in loops if isinstance(loop, Mapping)], fence=fence
        )
    return copied


def _canonical_id(value: object) -> str | None:
    """The lower case hyphenated form of a stored id in any spelling the link writer reads, or ``None``."""

    if value is None:
        return None
    text = str(value).strip()
    for prefix in ("source:", "memory:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _ids_in_text(text: str) -> set[str]:
    found = {match.lower() for match in _UUID_TEXT.findall(text)}
    whole = _canonical_id(text)
    if whole is not None:
        found.add(whole)
    return found


def _collect_ids(value: object, found: set[str], *, depth: int) -> None:
    """Add the id of every string and key in ``value``, canonical. Below the depth limit nothing is read."""

    if depth > _METADATA_MAX_DEPTH:
        return
    if isinstance(value, str):
        found.update(_ids_in_text(value))
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                found.update(_ids_in_text(key))
            _collect_ids(nested, found, depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for nested in value:
            _collect_ids(nested, found, depth=depth + 1)


def _rows_by_id(
    store: object, ids: Sequence[str], *, bulk: str, single: str
) -> dict[str, Mapping[str, object]]:
    """The live rows for ``ids`` by lower case id. An id the store does not return is not in the answer.

    No ids means no store call: the slices and the one-at-a-time loop below are empty then.
    """

    found: dict[str, Mapping[str, object]] = {}
    bulk_read = getattr(store, bulk, None)
    if callable(bulk_read):
        for start in range(0, len(ids), _LOOKUP_BATCH):
            for row in bulk_read(list(ids[start : start + _LOOKUP_BATCH])):
                if isinstance(row, Mapping):
                    found[str(row.get("id")).lower()] = row
        return found
    single_read = getattr(store, single, None)
    if callable(single_read):
        for row_id in ids:
            row = single_read(row_id)
            if isinstance(row, Mapping):
                found[row_id] = row
    return found


def _scrub_text(text: str, *, strict: bool, admitted: frozenset[str], refused: frozenset[str]) -> object:
    """``text`` without the ids to withhold, or ``_DROPPED`` when the text is only such an id.

    ``strict`` is set under a key that names a reference: an id there that names no admitted row is withheld, so a
    protected, a deleted and a missing id read alike. Elsewhere only an id that names a refused row is withheld.
    """

    found = _ids_in_text(text)
    bad = {found_id for found_id in found if found_id not in admitted} if strict else found & refused
    if not bad:
        return text
    if _canonical_id(text) in bad:
        return _DROPPED
    return _UUID_TEXT.sub(lambda match: _ID_WITHHELD if match.group(0).lower() in bad else match.group(0), text)


def _scrub(value: object, *, strict: bool, depth: int, admitted: frozenset[str], refused: frozenset[str]) -> object:
    if depth > _METADATA_MAX_DEPTH:
        return _DROPPED
    if isinstance(value, str):
        return _scrub_text(value, strict=strict, admitted=admitted, refused=refused)
    if isinstance(value, Mapping):
        output: dict[object, object] = {}
        for key, nested in value.items():
            new_key: object = key
            if isinstance(key, str):
                new_key = _scrub_text(key, strict=strict, admitted=admitted, refused=refused)
                if new_key is _DROPPED:
                    continue
            names_reference = isinstance(key, str) and key.lower() in _REFERENCE_KEYS
            new_value = _scrub(nested, strict=strict or names_reference, depth=depth + 1, admitted=admitted, refused=refused)
            if new_value is _DROPPED:
                continue
            output[new_key] = new_value
        return output
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = (_scrub(nested, strict=strict, depth=depth + 1, admitted=admitted, refused=refused) for nested in value)
        return [item for item in items if item is not _DROPPED]
    return value


__all__ = [
    "MEMORY_REFERENCE_KEYS",
    "SOURCE_REFERENCE_KEYS",
    "withhold_unreadable_references",
    "withhold_unreadable_references_from_loop",
    "withhold_unreadable_references_from_pack",
]
