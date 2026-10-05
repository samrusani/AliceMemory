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
  in the metadata an id goes when it names a row the fence refuses, a soft-deleted one included, or when the response
  withholds the same id from a reference position (see below). Any other id there is kept, because an id under an
  unknown key may be a run id or a trace id, which names no stored row and is not a reference. A string that is only
  an id is removed with its key or list slot, and an id inside longer text is replaced by ``(id withheld)``.
* An id is read in every spelling the link writer reads, whole or inside longer text: lower or upper case, hyphenated
  or as 32 hex digits with no hyphen, in braces, and after ``urn:uuid:``, ``uuid:``, ``source:`` or ``memory:``. It is
  compared in canonical form. Inside longer text the hyphenated spelling (groups of 8, 4, 4, 4 and 12 digits) is read
  wherever it stands, and 32 hex digits in a row are an id only when no hex digit stands next to them, so a 40-digit
  git sha or a 64-digit digest is never cut into an id, and neither is the id of a row the reader may read when hex
  digits follow it after a hyphen (``<id>-20261003``). A string that is only an id is read as the link writer reads it
  (``UUID()``, which also ignores hyphens in other places). Inside longer text only ASCII hex digits in those two
  layouts are read, and an id with its hyphens in other places, split or otherwise encoded is not an id to this scan.
  The hyphenated layout is read with no boundary, so hex digits glued to an id in that layout can form a second window
  that names no row: a hyphen and groups of 4, 4, 4 and 12 digits right after an id, or groups of 8, 4, 4 and 4 digits
  and a hyphen right before a 32-digit id. Under a reference key that window is cut, and with it part of an id the
  reader may read, there and wherever the same response repeats it. Under other keys alone it is kept. No id the
  reader may not read is shown by it; a boundary there would let a window of unrelated digits hide the front of a
  withheld id.
  A column value that is no id in any of those spellings is withheld as a missing one is. What is shown is the stored
  value, untouched.
* The withheld ids are collected for the whole call, over every row it returns (one loop, a list, or every loop of a
  context pack). An id withheld from a reference position of any row (the ``source_id`` or ``memory_id`` column, or any
  id under a reference key) is withheld from every other position of every row of that response, in every spelling,
  whatever the lookup says about it. So a loop that links an id and repeats it under ``evidence.quote_from`` shows it in
  neither place, whether the row it names is refused, deleted, removed or never existed, and two loops of one pack
  cannot be set against each other.
* A deleted row is refused, not unknown. The lookup reads soft-deleted sources and memories too (``include_deleted``
  on the two bulk reads), so an id under an unknown key that names a deleted source or memory is withheld even when no
  reference position of the response names it. The one case left is a row that was removed outright (or an id that
  never named a row) and that no reference position of the response links: under an unknown key it cannot be told
  from a trace id and is kept.
* The free-text columns of a loop (``title``, ``description``, ``resolution_note``) are returned as stored and are not
  scanned, so an id a writer put there is shown. The extractor of candidate loops used to write the id of a source
  with no title into the ``description`` and no longer does.

``fence`` is a required keyword-only argument of every function here, with no default. ``SourceReadFence.unfenced()``
is the owner's fence, written at the call site: it admits any live row and refuses a deleted one, so a loop that
points at a deleted source shows no id to anyone.

A store that cannot look rows up by id (no ``get_sources_by_ids`` and no ``get_source``, or no ``get_memories_by_ids``
and no ``get_memory``) admits nothing, so every reference is withheld. A store whose bulk read takes no
``include_deleted`` argument, or that has only the single-id read, is asked for live rows, and a deleted row then reads
as a missing one: the rule above still withholds a deleted id the response links itself. The two reads are one batched
statement each, at most ``_LOOKUP_BATCH`` ids at a time, and are skipped when the rows name nothing.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from uuid import UUID

from alicebot_api.vnext_source_fence import SOURCE_REFERENCE_KEYS, SourceReadFence, _json_container, cited_source_ids

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
# An id in text is found by a lookahead, so every window that reads as an id is found and one that overlaps another is
# not skipped: a scan that consumed each match would let a window of unrelated hex digits next to an id eat the front of
# it and leave the rest unrecognised. Group 1 is the id.
# The standard hyphenated spelling is read wherever it stands, as it always was.
_HYPHENATED_ID = re.compile(r"(?=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))")
# 32 hex digits in a row. No hex digit may stand next to the run, so a sha or a digest is not cut, and a hyphen next to
# it is not a hex digit. Hyphens between the digits are not read here (a whole string that is only an id is read by
# ``_canonical_id``): a pattern that let hyphens stand anywhere among the 32 digits found windows that start inside a
# hyphenated id and run into the hex digits after it, and under a reference key every such window that names no
# admitted row was cut out of an id the reader may read.
_BARE_ID = re.compile(r"(?<![0-9a-fA-F])(?=([0-9a-fA-F]{32})(?![0-9a-fA-F]))")
_DROPPED = object()


def withhold_unreadable_references(
    store: object,
    loops: Sequence[Mapping[str, object]],
    *,
    fence: SourceReadFence,
) -> list[JsonObject]:
    """Copies of ``loops`` that name only the sources and memories ``fence`` admits.

    The rows that come in are not changed. A row with nothing to withhold comes back equal to what went in. The ids to
    withhold are worked out over all of ``loops`` together, so pass every loop of one response in one call.
    """

    rows: list[JsonObject] = [dict(loop) for loop in loops]
    if not rows:
        return rows
    source_ids: set[str] = set()
    memory_ids: set[str] = set()
    metadata_ids: set[str] = set()
    referenced: set[str] = set()
    for row in rows:
        source = _canonical_id(row.get("source_id"))
        if source is not None:
            source_ids.add(source)
            referenced.add(source)
        memory = _canonical_id(row.get("memory_id"))
        if memory is not None:
            memory_ids.add(memory)
            referenced.add(memory)
        named = cited_source_ids(row.get("metadata_json")).named
        metadata_ids.update(named)
        referenced.update(named)
        _collect_ids(row.get("metadata_json"), metadata_ids, referenced, at_reference=False, depth=0)
    source_rows = _rows_by_id(store, sorted(source_ids | metadata_ids), bulk="get_sources_by_ids", single="get_source")
    memory_rows = _rows_by_id(store, sorted(memory_ids | metadata_ids), bulk="get_memories_by_ids", single="get_memory")
    admitted_sources = frozenset(key for key, row in source_rows.items() if fence.admits(row))
    from alicebot_api.vnext_label_guard import LabelGuard

    guard = LabelGuard.for_fence(store, fence)
    admitted_memories = frozenset(
        key for key, row in memory_rows.items()
        if isinstance(effective := guard.effective_row("memory", row), Mapping) and fence.admits_memory(effective)
    )
    refused = (set(source_rows) - admitted_sources) | (set(memory_rows) - admitted_memories)
    # Every id of a row the fence refuses, and every id at a reference position that names no admitted row (a refused,
    # a deleted, a removed or a missing one), is withheld at every position of every row of the response, in every
    # spelling. This is the one set the text scan uses: a position under a reference key needs no test of its own,
    # because an id there that is not admitted is in it already.
    withheld = frozenset(refused | (referenced - admitted_sources - admitted_memories))
    for row in rows:
        if row.get("source_id") is not None and _canonical_id(row["source_id"]) not in admitted_sources:
            row["source_id"] = None
        if row.get("memory_id") is not None and _canonical_id(row["memory_id"]) not in admitted_memories:
            row["memory_id"] = None
        metadata = row.get("metadata_json")
        if isinstance(metadata, Mapping):
            scrubbed = _scrub(metadata, depth=0, withheld=withheld)
            if scrubbed is _DROPPED:
                scrubbed = {}
            if scrubbed != metadata:
                row["metadata_json"] = scrubbed
    return rows


def source_rows_by_ids(store: object, source_ids: Iterable[object]) -> list[JsonObject]:
    """The stored rows of the sources ``source_ids`` name, in id order, one row for each source.

    This is the one batched read a report producer uses to find out how strict the sources it prints are, so that its
    label can cover them (see ``sources_named_by_loops`` and ``sources_named_by_refs``). An id is read in any spelling
    the link writer reads and counted once. A source that was archived is returned too, with ``deleted_at`` set, because
    an archived source is still printed and its label still counts. A store that cannot look sources up by id, or an id
    that names no row, adds no row. No id means no store call.
    """

    wanted: set[str] = set()
    for value in source_ids:
        source = _canonical_id(value)
        if source is not None:
            wanted.add(source)
    rows = _rows_by_id(store, sorted(wanted), bulk="get_sources_by_ids", single="get_source")
    return [dict(rows[source_id]) for source_id in sorted(wanted) if source_id in rows]


def sources_named_by_loops(store: object, loops: Sequence[Mapping[str, object]]) -> list[JsonObject]:
    """The source rows that the ``source_id`` column of ``loops`` names, one row for each source.

    A report that prints the id of each loop's source (the open-loop review does) is read behind a label, and the
    label has to cover those sources as well as the loops. Pass the loops as ``withhold_unreadable_references`` returned
    them: an id it withheld is ``None`` there, so only the sources the report really names are read.
    """

    return source_rows_by_ids(store, [loop.get("source_id") for loop in loops])


def sources_named_by_refs(store: object, refs: Iterable[object]) -> list[JsonObject]:
    """The source rows that ``refs`` name, one row for each source.

    ``refs`` is what a report prints as references, copied from rows as they were stored (the consolidation report
    copies ``metadata_json.source_refs`` of its cluster members). They are read the way a reader of a saved reference
    reads them (``cited_source_ids``), in every spelling a stored value can have: ``source:<id>`` in any case, the bare
    id, an id with no hyphens or in braces, ``alice://sources/<id>``, or an id inside longer text that names a stored
    source. An id after ``memory:`` names a memory and is not read. The report keeps printing the refs, so its label has
    to be at least as strict as every source they name.
    """

    return source_rows_by_ids(store, sorted(cited_source_ids(list(refs)).every))


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

    The pack's other sections are not changed here: its loops are the only rows it returns whole with their ids. Every
    loop of the pack goes through one call, so the ids one loop withholds are withheld from the others.
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


def _id_windows(text: str) -> list[tuple[int, int, str]]:
    """Every part of ``text`` that reads as an id, as ``(start, end, canonical id)``. The windows may overlap."""

    windows: list[tuple[int, int, str]] = []
    for pattern in (_HYPHENATED_ID, _BARE_ID):
        for match in pattern.finditer(text):
            start, end = match.span(1)
            windows.append((start, end, str(UUID(hex=text[start:end].replace("-", "")))))
    return windows


def _ids_in_text(text: str) -> set[str]:
    found = {canonical for _start, _end, canonical in _id_windows(text)}
    whole = _canonical_id(text)
    if whole is not None:
        found.add(whole)
    return found


def _collect_ids(value: object, found: set[str], referenced: set[str], *, at_reference: bool, depth: int) -> None:
    """Add the id of every string and key in ``value`` to ``found``, canonical, and to ``referenced`` those that stand
    in a reference position (under a key that names a reference, at any depth). Below the depth limit nothing is read.
    """

    if depth > _METADATA_MAX_DEPTH:
        return
    if isinstance(value, str):
        decoded = _json_container(value)
        if decoded is not None:
            _collect_ids(decoded, found, referenced, at_reference=at_reference, depth=depth + 1)
            return
        ids = _ids_in_text(value)
        if at_reference:
            ids |= set(cited_source_ids(value).named)
        found.update(ids)
        if at_reference:
            referenced.update(ids)
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                ids = _ids_in_text(key)
                found.update(ids)
                if at_reference:
                    referenced.update(ids)
            names_reference = isinstance(key, str) and key.lower() in _REFERENCE_KEYS
            _collect_ids(nested, found, referenced, at_reference=at_reference or names_reference, depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for nested in value:
            _collect_ids(nested, found, referenced, at_reference=at_reference, depth=depth + 1)


def _reads_deleted_rows(read: Callable[..., object]) -> bool:
    """True when the bulk read takes the ``include_deleted`` argument (both stores do)."""

    try:
        return "include_deleted" in inspect.signature(read).parameters
    except (TypeError, ValueError):
        return False


def _rows_by_id(
    store: object, ids: Sequence[str], *, bulk: str, single: str
) -> dict[str, Mapping[str, object]]:
    """The rows for ``ids`` by lower case id, soft-deleted ones included when the store can return them. An id the
    store does not return is not in the answer.

    No ids means no store call: the slices and the one-at-a-time loop below are empty then.
    """

    found: dict[str, Mapping[str, object]] = {}
    bulk_read = getattr(store, bulk, None)
    if callable(bulk_read):
        options: dict[str, object] = {"include_deleted": True} if _reads_deleted_rows(bulk_read) else {}
        for start in range(0, len(ids), _LOOKUP_BATCH):
            for row in bulk_read(list(ids[start : start + _LOOKUP_BATCH]), **options):
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


def _scrub_text(text: str, *, withheld: frozenset[str]) -> object:
    """``text`` without the ids in ``withheld``, or ``_DROPPED`` when the text is only such an id."""

    windows = _id_windows(text)
    whole = _canonical_id(text)
    found = {canonical for _start, _end, canonical in windows}
    if whole is not None:
        found.add(whole)
    bad = found & withheld
    if not bad:
        return text
    if whole in bad:
        return _DROPPED
    # Every window that reads as a withheld id is cut, and windows that overlap are cut as one.
    cuts: list[list[int]] = []
    for start, end in sorted((start, end) for start, end, canonical in windows if canonical in bad):
        if cuts and start < cuts[-1][1]:
            cuts[-1][1] = max(cuts[-1][1], end)
        else:
            cuts.append([start, end])
    pieces: list[str] = []
    position = 0
    for start, end in cuts:
        pieces.append(text[position:start])
        pieces.append(_ID_WITHHELD)
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _scrub(value: object, *, depth: int, withheld: frozenset[str]) -> object:
    if depth > _METADATA_MAX_DEPTH:
        return _DROPPED
    if isinstance(value, str):
        decoded = _json_container(value)
        if decoded is not None:
            checked = _scrub(decoded, depth=depth + 1, withheld=withheld)
            if checked is _DROPPED:
                return _DROPPED
            return value if checked == decoded else json.dumps(checked)
        return _scrub_text(value, withheld=withheld)
    if isinstance(value, Mapping):
        output: dict[object, object] = {}
        for key, nested in value.items():
            new_key: object = key
            if isinstance(key, str):
                new_key = _scrub_text(key, withheld=withheld)
                if new_key is _DROPPED:
                    continue
            new_value = _scrub(nested, depth=depth + 1, withheld=withheld)
            if new_value is _DROPPED:
                continue
            output[new_key] = new_value
        return output
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = (_scrub(nested, depth=depth + 1, withheld=withheld) for nested in value)
        return [item for item in items if item is not _DROPPED]
    return value


__all__ = [
    "MEMORY_REFERENCE_KEYS",
    "SOURCE_REFERENCE_KEYS",
    "source_rows_by_ids",
    "sources_named_by_loops",
    "sources_named_by_refs",
    "withhold_unreadable_references",
    "withhold_unreadable_references_from_loop",
    "withhold_unreadable_references_from_pack",
]
