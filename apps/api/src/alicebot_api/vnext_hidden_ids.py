"""Withhold the id of a row the caller may not read.

A readable report can list the ids of rows it was made from. Those ids used to come back as stored.
A caller with limits now loses them, in place: a list drops the entry, a single id becomes ``None``, a dict key
that is an id drops its pair, and a count that described a list the filter shortened is recomputed (or dropped
when the filter cannot tell which of several counts the dropped id belonged to). The owner and an unbound
admin key keep every id.

Which ids are looked at
-----------------------

* Every JSON string value whose whole value is an id is checked wherever it sits, under any key and at any depth,
  in any spelling ``uuid.UUID`` takes (any case, hyphens in any place, braces, ``urn:uuid:``) and behind a
  ``memory:``, ``source:``, ``artifact:`` or ``open_loop:`` prefix. A dict key that is an id is checked
  the same way.
* A token inside a string is looked for only where a producer prints ids: an entry of a list or the value of a field
  the producers write ids into (``candidate_memory_ids``, ``source_refs``, ``artifact_id`` and the rest) and the
  ``content_markdown`` of a report. The token is found by shape (32 hex digits with hyphens anywhere), parsed with
  ``identifier`` and compared in its canonical form, and it is removed as it was written.
* An id inside free text (a title, a note, a quote, the words a report copied) is not looked for. It is the
  writer's text, and the label of its own row governs it.

What is withheld
----------------

An id is judged against the rows the caller may read. In a position that is typed (a field or a prefix that says the
id names a memory, a source, an artifact or an open loop) an id that does not resolve to a row the caller may read is
withheld, whether the row is hidden or does not exist, so a key cannot tell the two apart. In any other position the
filter cannot tell an id of one of these rows from the id of an event, a task or a chunk, so it withholds only the id
of a row that exists and that the caller may not read.

An id the caller supplied
-------------------------

An id the caller supplied is not an exemption. It may appear in the answer only where the answer repeats the caller's own
input: at a key that is named like a declared input of the request (a path parameter, a declared query parameter, a field
of the JSON body, an argument of a tool), reached from the top of the answer through objects only, and only the ids that
input held. A query parameter or a body field the route does not declare supplies nothing. A path parameter also repeats at the
``id`` of the object it names. A key in a list of result rows is not an echo, and neither is an id at any other key of the answer, whatever the caller sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from alicebot_api.vnext_derived_labels import identifier
from alicebot_api.vnext_source_fence import _SCRUB_DEPTH, _TEXT_KEYS, SourceReadFence, _product_key

_READER: ContextVar[tuple[str | None, str | None, Any] | None] = ContextVar("hidden_id_reader", default=None)


def remember_reader(store: Any, identity: Any) -> None:
    """Remember who a command-line dump is for, until the process ends the command.

    The store's transaction ends before the dump. What is kept is the database URL and the user id,
    so the dump can open a fresh authenticated connection.
    """

    url = getattr(store, "database_url", None)
    user_id = getattr(store, "user_id", None)
    _READER.set((str(url) if url else None, str(user_id) if user_id else None, identity))


@contextmanager
def reader_scope() -> Iterator[None]:
    """Run a command with no reader remembered, and forget the one it remembers when it ends."""

    token = _READER.set(None)
    try:
        yield
    finally:
        _READER.reset(token)


def reader_for_dump() -> tuple[str | None, str | None, Any]:
    current = _READER.get()
    if current is None:
        return None, None, None
    return current


class HiddenIdFilterError(RuntimeError):
    """The filter was asked to judge a response it has no authenticated user for. Nothing is sent unfiltered."""


# -- how an id is found ----------------------------------------------------------------------------------------------

# Thirty-two hex digits with any number of hyphens between them, not part of a longer run of hex digits. This is the
# shape of every spelling ``uuid.UUID`` takes once its wrappers (braces, ``urn:uuid:``) are set aside. It only finds a
# candidate. ``identifier`` decides what the candidate is.
_ID_SHAPE_PATTERN = r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]-*){31}[0-9A-Fa-f](?![0-9A-Fa-f])"
_ID_SHAPE = re.compile(_ID_SHAPE_PATTERN)
# The ways a ref says what it names. ``uuid:`` and ``urn:uuid:`` are read by ``identifier`` and say no kind.
_KIND_PREFIX = r"(?:(?:memory|source|artifact|open_loop):|alice://(?:memories|sources)/)"
_LEADING_KIND = re.compile(r"\s*" + _KIND_PREFIX, re.IGNORECASE)
_WHITESPACE = re.compile(r"\s*")
# What can stand in front of an id in a printed line, longest first. They are read backwards from the id, one fixed
# size comparison for each, so the time is the length of the run and not the length of the text.
_PRINTED_PREFIXES = (
    "alice://memories/",
    "alice://sources/",
    "open_loop:",
    "artifact:",
    "urn:uuid:",
    "memory:",
    "source:",
    "uuid:",
)
# A whole value that is made only of characters an id spelling can hold. The shape test of the whole value is
# ``identifier`` itself, which takes every spelling ``uuid.UUID`` takes; this keeps it off prose.
_ID_CHARS = re.compile(r"[0-9a-fA-FxXuUrRnNiIdD_+\-{}:\s]+")
_CANONICAL = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MIN_ID_CHARS = 32
_MAX_PLAIN_CHARS = 128
# The longest JSON text in a ref that is read as the object or list it holds.
_MAX_JSON_TEXT = 100_000

_KINDS = ("memory", "source", "artifact", "open_loop")

# Lists whose entries the producers fill with ids of memories, sources, artifacts and open loops. An entry that is
# the whole of an id is withheld when the caller may not read the row, hidden or missing.
_TYPED_LISTS = frozenset(
    {
        "candidate_memory_ids",
        "candidate_open_loop_ids",
        "sources",
        "memories",
        "artifacts",
        "open_loops",
        "source_ids",
        "memory_ids",
        "open_loop_ids",
        "artifact_ids",
        "stale_marked_memory_ids",
        "member_ids",
        "cluster_member_ids",
        "source_refs",
    }
)
# Lists of ids that the filter reads but cannot resolve for every store, or that hold ids of other rows (an event, a
# chunk, a belief, an edge). Only the id of a row that exists and is unreadable is withheld from them.
_OTHER_LISTS = frozenset({"candidate_edge_ids", "beliefs", "belief_ids", "source_event_ids", "provenance"})
_TYPED_SCALARS = frozenset(
    {
        "candidate_memory_id",
        "belief_memory_id",
        "memory_id",
        "artifact_id",
        "output_artifact_id",
        "source_artifact_id",
        "source_id",
    }
)
_OTHER_SCALARS = frozenset({"target_id", "source_item", "connected_item"})
_TYPED_FIELDS = _TYPED_LISTS | _TYPED_SCALARS
_ID_FIELDS = _TYPED_FIELDS | _OTHER_LISTS | _OTHER_SCALARS
# The fields that print ids inside text.
_TEXT_FIELDS = frozenset({"content_markdown"})
# Lists of objects that stand for a row. The entry goes when the id it is marked with is withheld.
_OBJECT_MARKERS = {"member_snapshots": "id", "instances": "memory_id"}
# The refs a client wrote. Every id inside one, under any key and at any depth, is a citation of a row.
_REF_FIELDS = frozenset({"source_refs"})


def _kind_prefixes_end(text: str) -> int:
    """Where the kind prefixes at the start of ``text`` end, or 0 when it starts with none.

    Each prefix is matched at an index of the same string. Nothing is sliced, so a run of ten thousand prefixes takes
    ten thousand matches and copies nothing.
    """

    position = 0
    while True:
        lead = _LEADING_KIND.match(text, position)
        if lead is None:
            return position
        position = lead.end()


def _whole_id(text: str) -> tuple[str, bool] | None:
    """The canonical id that the whole of ``text`` is, and whether a kind prefix said what it names."""

    if len(text) < _MIN_ID_CHARS:
        return None
    start = _kind_prefixes_end(text)
    prefixed = start > 0
    core = text[start:].strip()
    if len(core) < _MIN_ID_CHARS:
        return None
    if len(core) > _MAX_PLAIN_CHARS and _ID_SHAPE.search(core) is None:
        return None
    if _ID_CHARS.fullmatch(core) is None:
        return None
    # ``uuid.UUID`` strips a lower case ``urn:`` and ``uuid:`` only, so the text is lower cased first, as the saved-quote
    # reader does.
    canonical = identifier(core.lower())
    if _CANONICAL.fullmatch(canonical) is None:
        return None
    return canonical, prefixed


def _leading_id(text: str) -> tuple[str, bool] | None:
    """The canonical id a string starts with after its kind prefixes, and whether there was a prefix.

    ``source:<id>#chunk-1`` and ``memory:<id> some words`` name the row the id belongs to, and what follows it is
    not part of the id.
    """

    start = _kind_prefixes_end(text)
    prefixed = start > 0
    blank = _WHITESPACE.match(text, start)
    match = _ID_SHAPE.match(text, blank.end() if blank is not None else start)
    if match is None:
        return None
    canonical = identifier(match.group())
    if _CANONICAL.fullmatch(canonical) is None:
        return None
    return canonical, prefixed


def _printed_extent(text: str, start: int, end: int) -> tuple[int, int]:
    """The span of an id printed in a line, with its braces and the prefixes in front of it.

    The prefixes are read backwards from the id, so a long run of them is read once.
    """

    if start > 0 and end < len(text) and text[start - 1] == "{" and text[end] == "}":
        start, end = start - 1, end + 1
    while True:
        for prefix in _PRINTED_PREFIXES:
            size = len(prefix)
            if start >= size and text[start - size : start].lower() == prefix:
                start -= size
                break
        else:
            return start, end


def _names_a_row(field: str | None, refs: bool) -> bool:
    """True when a whole-value id at ``field`` is a reference to a row, so an id that names no readable row goes.

    A field the producers write ids of rows into does. Inside a ref, so does a field the product reads as a reference
    (``memory_id``, ``source_id``, ``id``, ``ref`` and the rest of ``PRODUCT_REF_KEYS``). An id under any other name in a
    ref (``origin``, ``chunk_id``, ``evidence``) may name a chunk or a session, as the saved-quote reader holds.
    """

    if field in _TYPED_FIELDS:
        return True
    if not refs or field is None:
        return False
    lowered = _product_key(field)
    return lowered is not None and lowered not in _TEXT_KEYS


def _tokens(text: str) -> list[str]:
    """The canonical id of every token of ``text`` that has the shape of an id."""

    if len(text) < _MIN_ID_CHARS:
        return []
    found = []
    for match in _ID_SHAPE.finditer(text):
        canonical = identifier(match.group())
        if _CANONICAL.fullmatch(canonical) is not None:
            found.append(canonical)
    return found


def _canonical_ids(value: object, depth: int = 0) -> set[str]:
    """Every id token in ``value``: in strings of any kind, in dict keys and in ``UUID`` objects.

    This is the wide net used for the ids a caller named in a request. The walk stops at the saved-quote reader's
    depth, where a deeper row is the one the deep-row cut already replaces.
    """

    if depth > _SCRUB_DEPTH:
        return set()
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_tokens(value))
    elif isinstance(value, UUID):
        found.add(str(value))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                found.update(_tokens(key))
            found.update(_canonical_ids(child, depth + 1))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_canonical_ids(child, depth + 1))
    return found


# -- who is limited --------------------------------------------------------------------------------------------------


def _caller_is_limited(identity: Any) -> bool:
    """True when this caller has a read ceiling. An unbound admin keeps every id.

    A request may name a project without binding the key. That selection is not a limit.
    """

    if identity is None:
        return False
    if getattr(identity, "permission_profile", None) == "admin_agent" and not getattr(identity, "project_scope_locked", False):
        return False
    return SourceReadFence.for_identity(identity).entity_read_fenced


# -- which ids the caller may read -----------------------------------------------------------------------------------


def resolve_ids(store: Any, identity: Any, candidates: set[str]) -> tuple[set[str], set[str]] | None:
    """``(hidden, readable)`` among ``candidates``, or ``None`` when the store cannot look rows up.

    ``hidden`` holds the ids of stored rows the caller may not read: above its ceiling, outside its domains or
    project binding, archived or deleted. ``readable`` holds the ids of stored rows it may read. An id in neither is
    the id of no row. It costs one label query for each kind of row, whatever the number of ids.
    """

    reader = getattr(store, "read_label_rows", None)
    if not callable(reader):
        return None
    if not candidates:
        return set(), set()
    guard = _guard_at_full_ceiling(store, identity)
    ordered = sorted(candidates)
    hidden: set[str] = set()
    readable: set[str] = set()
    for kind in _KINDS:
        rows = [row for row in reader(kind, ordered) if isinstance(row, Mapping) and row.get("id") is not None]
        if guard is None:
            hidden.update(identifier(row.get("id")) for row in rows)
            continue
        admitted = {identifier(row.get("id")) for row in guard.admit_rows(kind, rows)}
        gone = _deleted_source_ids(store, [identifier(row.get("id")) for row in rows]) if kind == "source" else set()
        for row in rows:
            row_id = identifier(row.get("id"))
            if row_id not in admitted or row_id in gone or _row_is_gone(row):
                hidden.add(row_id)
            else:
                readable.add(row_id)
    return hidden, readable - hidden


def _guard_at_full_ceiling(store: Any, identity: Any) -> Any:
    """The caller's whole read ceiling and project binding, not the default recall selection.

    ``guard_for_caller`` asks the policy for ``memory.recall`` with no sensitivity, so the default
    selection drops confidential. A project-bound admin may read confidential rows inside its project.
    """

    from alicebot_api.vnext_agent_control import (
        ALL_SENSITIVITY,
        VNEXT_DOMAINS,
        AgentPolicyBlockedError,
    )
    from alicebot_api.vnext_label_guard import LabelGuard, clamp_request_filters

    try:
        domains, sensitivity = clamp_request_filters(
            identity,
            domains=VNEXT_DOMAINS,
            sensitivity_allowed=ALL_SENSITIVITY,
            action="memory.recall",
        )
    except AgentPolicyBlockedError:
        return None
    locked = bool(getattr(identity, "project_scope_locked", False))
    projects = tuple(getattr(identity, "project_scope", ()) or ()) if locked else ()
    return LabelGuard.for_filters(
        store,
        domains,
        sensitivity,
        projects,
        all_of=projects if locked else None,
    )


def _row_is_gone(row: Mapping[str, Any]) -> bool:
    """An archived or deleted row is not readable. A forgotten memory still shows in history."""

    if row.get("deleted_at") is not None:
        return True
    return str(row.get("status") or "") == "archived"


def _deleted_source_ids(store: Any, ids: list[str]) -> set[str]:
    """Source label rows omit deleted_at. A deleted source is still a row the caller may not read."""

    getter = getattr(store, "get_sources_by_ids", None)
    if not ids or not callable(getter):
        return set()
    try:
        found = getter(ids, include_deleted=True)
    except TypeError:
        return set()
    return {
        identifier(row.get("id"))
        for row in found
        if isinstance(row, Mapping) and row.get("deleted_at") is not None and row.get("id") is not None
    }


# -- what is removed -------------------------------------------------------------------------------------------------


class _Judge:
    """Which ids of a payload are withheld."""

    __slots__ = ("hidden", "readable")

    def __init__(self, hidden: set[str], readable: set[str] | None) -> None:
        self.hidden = hidden
        self.readable = readable

    def withheld(self, canonical: str, typed: bool) -> bool:
        if typed and self.readable is not None:
            return canonical not in self.readable
        return canonical in self.hidden


_DROP = object()

# Words that carry no meaning when a count is matched to the list it counts.
_NOISE = frozenset({"count", "counts", "id", "ids", "displayed", "total", "input"})
# Counts that describe a list under a name that shares no word with it. A roll-up proposal counts its members as
# instances, and the stale sweep counts the memories it marked in two groups.
_COUNTS_OF = {
    "member_ids": frozenset({"instance_count", "displayed_instance_count"}),
    "stale_marked_memory_ids": frozenset({"expired_marked", "unconfirmed_marked"}),
}


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(name: str) -> frozenset[str]:
    """The meaningful words of a field name, in the singular."""

    return frozenset(word for word in (_singular(part) for part in name.lower().split("_")) if word and word not in _NOISE)


def _related(count_name: str, list_name: str) -> bool:
    """True when ``count_name`` counts what ``list_name`` lists: every word of the count is a word of the list."""

    if count_name == "count" or count_name in _COUNTS_OF.get(list_name, ()):
        return True
    words = _words(count_name)
    return bool(words) and words <= _words(list_name)


class _Walk:
    """One pass over a payload. With no judge it removes nothing and collects the ids it would judge.

    ``inputs`` holds what the caller supplied, by the name of the input. ``scope`` is the set of ids that the position
    being visited repeats from the caller's own input. It is empty until the walk reaches a key named like an input,
    and it is opened only by a key reached through objects from the top of the payload (``may_open``), so a key inside a
    list of result rows opens nothing.
    """

    __slots__ = ("judge", "seen", "inputs", "scope", "may_open")

    def __init__(self, judge: _Judge | None, inputs: Mapping[str, frozenset[str]] | None = None) -> None:
        self.judge = judge
        self.seen: set[str] = set()
        self.inputs: Mapping[str, frozenset[str]] = inputs or {}
        self.scope: frozenset[str] = frozenset()
        self.may_open = True

    def withheld(self, canonical: str, typed: bool, honour_echo: bool = True) -> bool:
        if honour_echo and canonical in self.scope:
            return False
        self.seen.add(canonical)
        return self.judge is not None and self.judge.withheld(canonical, typed)

    def text(self, text: str) -> str:
        """``text`` with every withheld id token removed as it was written, with the prefix and braces around it."""

        if len(text) < _MIN_ID_CHARS:
            return text
        cuts: list[tuple[int, int]] = []
        for match in _ID_SHAPE.finditer(text):
            canonical = identifier(match.group())
            if _CANONICAL.fullmatch(canonical) is not None and self.withheld(canonical, False):
                cuts.append(_printed_extent(text, match.start(), match.end()))
        if not cuts:
            return text
        pieces: list[str] = []
        kept = 0
        for start, end in cuts:
            pieces.append(text[kept : max(start, kept)])
            kept = max(kept, end)
        pieces.append(text[kept:])
        return "".join(pieces)

    def string(self, text: str, field: str | None, refs: bool = False, depth: int = 0) -> str | None:
        """``text`` itself when it stays, ``None`` when the whole value is withheld."""

        if field in _TEXT_FIELDS:
            return self.text(text)
        whole = _whole_id(text)
        if whole is not None:
            canonical, prefixed = whole
            return None if self.withheld(canonical, prefixed or _names_a_row(field, refs)) else text
        if _LEADING_KIND.match(text):
            named = _leading_id(text)
            if named is not None and self.withheld(named[0], True):
                return None
        if refs and field not in _TEXT_KEYS and text[:1] in "{[" and len(text) <= _MAX_JSON_TEXT and depth < _SCRUB_DEPTH:
            # A ref can be JSON text. It is read like the object or list it holds, so the same id gives the same answer.
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                # Not JSON, or nested past what the decoder reads. It is read as text, like any other string of a ref.
                parsed = None
            if isinstance(parsed, (dict, list)):
                judged = self.value(parsed, field, depth + 1, True)
                if judged is not parsed:
                    return json.dumps(judged)
                return text
        if field in _ID_FIELDS or (refs and field not in _TEXT_KEYS):
            for canonical in _tokens(text):
                if self.withheld(canonical, False):
                    return None
        return text

    def value(self, node: Any, field: str | None, depth: int, refs: bool = False) -> Any:
        if depth > _SCRUB_DEPTH:
            return node
        if isinstance(node, str):
            return self.string(node, field, refs, depth)
        if isinstance(node, UUID):
            return None if self.withheld(str(node), _names_a_row(field, refs)) else node
        if isinstance(node, (list, tuple)):
            return self.items(node, field, depth, refs)
        if isinstance(node, Mapping):
            return self.mapping(node, depth, refs)
        return node

    def items(self, node: list[Any] | tuple[Any, ...], field: str | None, depth: int, refs: bool) -> Any:
        marker_key = _OBJECT_MARKERS.get(field or "")
        kept: list[Any] = []
        changed = False
        may_open = self.may_open
        if not self.scope:
            # A list outside the scope of an input is a list of rows. No key inside it repeats an input.
            self.may_open = False
        try:
            for item in node:
                if marker_key is not None and isinstance(item, Mapping) and isinstance(item.get(marker_key), str):
                    whole = _whole_id(item[marker_key])
                    if whole is not None and self.withheld(whole[0], True):
                        changed = True
                        continue
                new = self.value(item, field, depth + 1, refs)
                if new is None and isinstance(item, (str, UUID)):
                    changed = True
                    continue
                changed = changed or new is not item
                kept.append(new)
        finally:
            self.may_open = may_open
        return kept if changed else node

    def child(self, child: Any, field: str, depth: int, refs: bool) -> Any:
        """Visit the value of ``field``. A key named like an input of the request opens the scope of that input's ids."""

        if field not in self.inputs or not (self.scope or self.may_open):
            return self.value(child, field, depth + 1, refs)
        scope, may_open = self.scope, self.may_open
        self.scope = scope | self.inputs[field]
        self.may_open = True
        try:
            return self.value(child, field, depth + 1, refs)
        finally:
            self.scope, self.may_open = scope, may_open

    def mapping(self, node: Mapping[Any, Any], depth: int, refs: bool) -> Any:
        output: dict[Any, Any] = {}
        shortened: dict[str, tuple[int, int]] = {}
        changed = False
        for key, child in node.items():
            if isinstance(key, str):
                whole = _whole_id(key)
                if whole is not None and self.withheld(whole[0], whole[1]):
                    changed = True
                    continue
            field = key if isinstance(key, str) else str(key)
            if refs and isinstance(child, str) and _product_key(key) is None:
                # A field the product does not write is dropped with its value from a ref that names a row, as the
                # saved-quote reader drops it for a hidden row, even when the caller sent the id: the reader's answer
                # to a hidden id and a missing id must be one answer.
                named = _whole_id(child)
                if named is not None and named[1] and self.withheld(named[0], True, honour_echo=False):
                    changed = True
                    continue
            new = self.child(child, field, depth, refs or field in _REF_FIELDS)
            if new is None and isinstance(child, (str, UUID)):
                changed = True
                if refs and _product_key(key) is None:
                    continue
                output[key] = None
                continue
            if new is not child:
                changed = True
                if isinstance(child, (list, tuple)) and isinstance(new, list) and len(new) < len(child):
                    shortened[field] = (len(child), len(new))
            output[key] = new
        if not changed:
            return node
        if shortened:
            _fix_counts(output, shortened)
        return output


def _fix_counts(output: dict[Any, Any], shortened: dict[str, tuple[int, int]]) -> None:
    """Make the counts of ``output`` agree with the lists the filter shortened.

    A count that equals the old length of a list it counts becomes the new length. A count that counts such a list
    and equals no old length (it counts something wider, or the filter cannot tell which of several counts the
    dropped id belonged to) is dropped, so it cannot tell how many entries were withheld.
    """

    def adjusted(name: str, value: int) -> int | object | None:
        if value == 0:
            return None  # nothing was counted, so nothing withheld was in it
        related = [(old, new) for key, (old, new) in shortened.items() if _related(name, key)]
        if not related:
            return None
        matches = {new for old, new in related if old == value}
        if len(matches) == 1:
            return matches.pop()
        if any(
            key not in shortened and isinstance(items, list) and len(items) == value and _related(name, str(key))
            for key, items in output.items()
        ):
            return None  # it counts a list that lost nothing
        return None if name == "count" and not matches else _DROP

    for name in list(output):
        value = output[name]
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            result = adjusted(str(name), value)
            if result is _DROP:
                del output[name]
            elif result is not None:
                output[name] = result
        elif isinstance(value, Mapping) and "count" in str(name).lower():
            inner = dict(value)
            for sub, number in value.items():
                if isinstance(number, bool) or not isinstance(number, int):
                    continue
                result = adjusted(str(sub), number)
                if result is _DROP:
                    del inner[sub]
                elif result is not None:
                    inner[sub] = result
            output[name] = inner
    if isinstance(output.get("instances_truncated"), bool) and shortened.keys() & {"instances", "member_ids"}:
        instances, members = output.get("instances"), output.get("member_ids")
        if isinstance(instances, list) and isinstance(members, list):
            output["instances_truncated"] = len(instances) < len(members)
        else:
            del output["instances_truncated"]


def judged_ids(payload: object, echo: Mapping[str, frozenset[str]] | None = None) -> set[str]:
    """The ids of ``payload`` whose answer depends on the rows the caller may read. No store is read.

    An id that stands only where the answer repeats an input of the caller (``echo``) is not one of them.
    """

    walk = _Walk(None, echo)
    walk.value(payload, None, 0)
    return walk.seen


def strip_hidden_ids(
    value: Any,
    hidden: set[str],
    *,
    readable: set[str] | None = None,
    echo: Mapping[str, frozenset[str]] | None = None,
) -> Any:
    """``value`` with every withheld id removed from the positions that carry one.

    ``hidden`` holds the ids of rows that exist and that the caller may not read. ``readable``, when given, holds
    the ids of rows it may read: an id in a typed position that is in neither set is the id of no row, and goes
    with the hidden ones. ``echo`` holds what the caller supplied, by the name of the input: an id stays where the
    answer repeats the input that held it, and nowhere else. Nothing to remove returns ``value``, the same object.
    """

    if not hidden and readable is None:
        return value
    return _Walk(_Judge(hidden, readable), echo).value(value, None, 0)


def apply_hidden_id_withholding(
    store: Any, identity: Any, payload: Any, *, echo: Mapping[str, frozenset[str]] | None = None
) -> Any:
    """The payload a caller may be shown. Unchanged, and the same object, when nothing is withheld.

    A row nested past the saved-quote depth makes the label walk overflow. That call is the one the deep-row cut
    runs again. This step leaves that payload as the cut reader returned it.
    """

    return withhold_with(lambda: nullcontext(store), identity, payload, echo=echo)


def withhold_with(
    open_store: Callable[[], AbstractContextManager[Any]],
    identity: Any,
    payload: Any,
    *,
    echo: Mapping[str, frozenset[str]] | None = None,
) -> Any:
    """Like ``apply_hidden_id_withholding``, with a store opened only when the payload holds an id to judge.

    ``open_store`` returns a context manager that yields a store. It is not called for the owner, for an unbound
    admin key, or for a payload with no id in a position the filter looks at.
    """

    if not _caller_is_limited(identity):
        return payload
    candidates = judged_ids(payload, echo)
    if not candidates:
        return payload
    try:
        with open_store() as store:
            resolved = resolve_ids(store, identity, candidates)
    except RecursionError:
        return payload
    if resolved is None:
        return payload
    hidden, readable = resolved
    return strip_hidden_ids(payload, hidden, readable=readable, echo=echo)


def echo_of_arguments(arguments: object) -> dict[str, frozenset[str]]:
    """What a tool call supplied, by the name of the argument. The tool schema refuses an argument it does not declare."""

    if not isinstance(arguments, Mapping):
        return {}
    return _ids_by_name(arguments)


def echo_of_request(request: Any, named_body: bytes = b"") -> dict[str, frozenset[str]]:
    """What a request supplied through the inputs its route declares, by the name of the input.

    The path parameters, the query parameters the route declares and the fields of the JSON body the route declares.
    A query parameter or a body field the route does not declare supplies nothing, so a key cannot make an id
    appear in an answer by adding it to the request. A request with no matched route supplies nothing.
    """

    scope = getattr(request, "scope", None)
    if not isinstance(scope, Mapping):
        return {}
    dependant = getattr(scope.get("route"), "dependant", None)
    if dependant is None:
        return {}
    from fastapi.dependencies.utils import get_flat_dependant

    flat = get_flat_dependant(dependant)
    supplied: dict[str, set[str]] = {}

    def add(name: object, value: object) -> None:
        ids = _canonical_ids(value)
        if ids and isinstance(name, str):
            supplied.setdefault(name, set()).update(ids)

    for name, value in (scope.get("path_params") or {}).items():
        add(name, str(value))
        # The URL names the row, so the object that describes it repeats the id as its own ``id``.
        add("id", str(value))
    query = getattr(request, "query_params", None)
    if query is not None:
        for parameter in flat.query_params:
            for value in query.getlist(parameter.alias):
                add(parameter.alias, value)
    declared: set[str] = set()
    for parameter in flat.body_params:
        fields = getattr(parameter.field_info.annotation, "model_fields", None)
        if isinstance(fields, Mapping):
            declared.update(field.alias or name for name, field in fields.items())
        else:
            declared.add(parameter.alias)
    if declared and named_body:
        try:
            body = json.loads(named_body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            body = None
        if isinstance(body, Mapping):
            for name in declared & {key for key in body if isinstance(key, str)}:
                add(name, body[name])
    return {name: frozenset(ids) for name, ids in supplied.items()}


def _ids_by_name(items: Mapping[Any, Any]) -> dict[str, frozenset[str]]:
    found: dict[str, frozenset[str]] = {}
    for name, value in items.items():
        ids = _canonical_ids(value)
        if ids and isinstance(name, str):
            found[name] = frozenset(ids)
    return found


def withhold_json_body(
    request: Any, body: bytes, *, database_url: str | None = None, named_body: bytes = b""
) -> bytes | None:
    """JSON bytes with hidden ids removed, or ``None`` when the body should be sent unchanged.

    The user is the one the vNext gate authenticated the key for. It is not read from the query or the body.
    """

    state = getattr(request, "state", None)
    identity = getattr(state, "vnext_agent_identity", None)
    path = getattr(getattr(request, "url", None), "path", "")
    if identity is None or not str(path).startswith("/v0/vnext") or not body:
        return None
    if not _caller_is_limited(identity):
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    echo = echo_of_request(request, named_body)
    if not judged_ids(payload, echo):
        return None
    user_id = getattr(state, "vnext_authenticated_user_id", None)
    if user_id is None:
        raise HiddenIdFilterError("a limited key was authenticated without a user to read rows for")
    from alicebot_api.db import user_connection
    from alicebot_api.vnext_store import PostgresVNextStore

    if database_url is None:
        from alicebot_api.config import get_settings

        database_url = get_settings().database_url
    if str(database_url).startswith("sqlite"):
        return None

    @contextmanager
    def open_store() -> Any:
        with user_connection(database_url, UUID(str(user_id))) as conn:
            yield PostgresVNextStore(conn)

    filtered = withhold_with(open_store, identity, payload, echo=echo)
    if filtered is payload:
        return None
    return json.dumps(filtered).encode("utf-8")

