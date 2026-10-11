"""Bounds on the JSON a caller hands the product to store, and a decoder that a stored row cannot overflow.

Two problems, one module.

**What a write door accepts.** A caller's JSON (the ``source_refs`` of a memory commit, the ``provenance`` of a
correction, an agent output, the arguments of a tool) used to be stored as it came. Nothing limited how deep it nested or
which numbers it held. A ref nested about 950 levels deep passed the commit door, because the door measured the
serialized size of a ref and not its depth, and every later read of that row raised ``RecursionError`` in a recursive
encoder (the memory audit route, the recent commits route, ``alice_memory_review``). ``NaN``, ``Infinity`` and ``1e999``
(a float that overflows to infinity) are not JSON: PostgreSQL's ``jsonb`` and the ``json_valid`` check of the SQLite
columns refuse them, so a ref that held one ended as a database error and a server error. ``refuse_unstorable_json`` is
the one check, and every door that stores caller JSON asks it.

The depth limit is ``MAX_STORED_JSON_DEPTH``, counted as the request-body limit counts it: one level for each array or
object, so ``[]`` is one level and a scalar is none. The product's own producers write at most seven levels on SQLite
(measured over every row the unit suite makes) and the PostgreSQL producers are measured in the changelog entry, so 64
leaves several times that. A caller's JSON is wrapped before it is stored (``metadata_json.agentic_memory.source_refs``
is three levels down), and a backup refuses a column nested more than ``MAX_READ_JSON_DEPTH`` levels, so the write limit
keeps every stored column well inside what a restore reads.

**What a reader takes.** A row stored before the limit, or by a door that has no limit of its own (a PostgreSQL HTTP
route stops at the body limit of 256 levels), can still nest deeper than the readers can walk. ``loads_bounded`` decodes
stored JSON text and never raises ``RecursionError``: text nested no deeper than ``MAX_READ_JSON_DEPTH`` is decoded as
``json.loads`` decodes it, and the part of a deeper text below that depth is replaced by the string ``DEEP_PLACEHOLDER``
before it is decoded. No route fails for every caller because of one stored row, and the rest of the row is returned as
it was. The cut is made by a scan that keeps its own counter, never by recursion. A writer that reads such a row and
writes it back stores the placeholder in the place of the part that was cut.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

# The most levels of arrays and objects a caller's JSON may nest where the product stores it.
MAX_STORED_JSON_DEPTH = 64
# The most levels a stored column is read at. It is the limit of an HTTP request body, of a backup's JSON columns and of
# the key claim walk (``request_limits.MAX_JSON_NESTING``, held equal by a test), so a row inside it is read whole.
MAX_READ_JSON_DEPTH = 256
# Stands in the place of a part of stored JSON that is nested deeper than ``MAX_READ_JSON_DEPTH``.
DEEP_PLACEHOLDER = "[nested too deeply]"

TOO_DEEP = "too_deep"
NOT_FINITE = "not_finite"


class JsonShapeError(ValueError):
    """Caller JSON that cannot be stored. ``reason`` is ``TOO_DEEP`` or ``NOT_FINITE``; the message never repeats the value."""

    reason = ""

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        super().__init__(message)


class JsonTooDeep(JsonShapeError):
    reason = TOO_DEEP

    def __init__(self, name: str) -> None:
        super().__init__(name, f"{name} must nest at most {MAX_STORED_JSON_DEPTH} levels of arrays and objects")


class JsonNotFinite(JsonShapeError):
    reason = NOT_FINITE

    def __init__(self, name: str) -> None:
        super().__init__(name, f"{name} must not hold NaN, Infinity or a number too large for a float")


def first_json_problem(value: object, *, limit: int = MAX_STORED_JSON_DEPTH) -> str | None:
    """``TOO_DEEP``, ``NOT_FINITE`` or ``None`` for a decoded JSON value.

    The walk keeps its own stack, so a value nested a million levels deep (or one that holds itself) is judged, not
    recursed into. A mapping or a list is one level; a ``float`` that is not finite is refused wherever it stands.
    """

    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, float):
            if not math.isfinite(node):
                return NOT_FINITE
        elif isinstance(node, Mapping):
            if depth + 1 > limit:
                return TOO_DEEP
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, (list, tuple)):
            if depth + 1 > limit:
                return TOO_DEEP
            stack.extend((child, depth + 1) for child in node)
    return None


def refuse_unstorable_json(value: object, *, name: str, limit: int = MAX_STORED_JSON_DEPTH) -> None:
    """Raise ``JsonTooDeep`` or ``JsonNotFinite`` when a caller's JSON could not be stored and read back."""

    problem = first_json_problem(value, limit=limit)
    if problem == TOO_DEEP:
        raise JsonTooDeep(name)
    if problem == NOT_FINITE:
        raise JsonNotFinite(name)


# A JSON string, escapes included, or one bracket. Strings are inert: a bracket inside one is text. Every quantifier is
# possessive, and a string that is never closed (or ends in a lone backslash) is consumed to the end of the text, so a text
# that is mostly escaped quotes costs one pass and not the square of its length.
_TOKEN = re.compile(r'"(?:[^"\\]++|\\.|\\\Z)*+(?:"|\Z)|[\[\]{}]', re.DOTALL)
_OPENERS = "[{"


def _as_text(data: object) -> str | None:
    if isinstance(data, str):
        return data
    if isinstance(data, memoryview):
        data = bytes(data)
    if isinstance(data, (bytes, bytearray)):
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def text_nests_deeper_than(text: str, limit: int) -> bool:
    """Whether JSON text opens more than ``limit`` levels of arrays and objects at once, without decoding it.

    A text with no more than ``limit`` opening brackets in all cannot nest that deep, and ``str.count`` answers that at
    C speed, so an ordinary column costs two counts. Otherwise the scan walks the brackets with a counter and stops at
    the first level over the limit.
    """

    if text.count("[") + text.count("{") <= limit:
        return False
    depth = 0
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token in _OPENERS:
            depth += 1
            if depth > limit:
                return True
        elif token in "]}":
            depth -= 1
    return False


def cut_deep_json(text: str, limit: int = MAX_READ_JSON_DEPTH) -> str:
    """The JSON text with every array or object opened below ``limit`` levels replaced by ``DEEP_PLACEHOLDER``.

    One pass over the tokens keeps a counter; the part from the opening bracket that passes the limit to its closing
    bracket is dropped and the placeholder string takes its place. Text that is not JSON comes back cut at the same
    places and the decoder refuses it, as it would have refused the original.
    """

    pieces: list[str] = []
    kept_from = 0
    depth = 0
    cutting = False
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token in _OPENERS:
            depth += 1
            if depth == limit + 1:
                pieces.append(text[kept_from : match.start()])
                pieces.append(json.dumps(DEEP_PLACEHOLDER))
                cutting = True
        elif token in "]}":
            if cutting and depth == limit + 1:
                cutting = False
                kept_from = match.end()
            depth -= 1
    if cutting:
        return "".join(pieces)
    pieces.append(text[kept_from:])
    return "".join(pieces)


def loads_bounded(data: object, *, limit: int = MAX_READ_JSON_DEPTH) -> Any:
    """``json.loads`` of stored JSON text (``str`` or UTF-8 bytes) that never raises ``RecursionError``.

    A text nested no deeper than ``limit`` is decoded as it stands. A deeper one is decoded with the part below ``limit``
    replaced by ``DEEP_PLACEHOLDER``. Anything else ``json.loads`` raises (invalid JSON) is raised unchanged.
    """

    # The common case first: two counts and the plain decode.
    if type(data) is str and data.count("[") + data.count("{") <= limit:
        return json.loads(data)
    if type(data) is bytes and data.count(b"[") + data.count(b"{") <= limit:
        return json.loads(data)
    text = _as_text(data)
    if text is None:
        return json.loads(data)  # type: ignore[arg-type]
    if text_nests_deeper_than(text, limit):
        text = cut_deep_json(text, limit)
    try:
        return json.loads(text)
    except RecursionError:
        # A text that passed the count above cannot be this deep; a build whose decoder gives up earlier than the
        # limit still must not turn one stored row into a failure of the route that read it.
        return json.loads(cut_deep_json(text, max(1, limit // 2)))


def _refuse_constant(_name: str) -> object:
    raise JsonNotFinite("the JSON text")


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise JsonNotFinite("the JSON text")
    return number


# Every way a JSON text can hold a number that is not finite, and no way to hold one that this does not match: the words
# ``NaN`` and ``Infinity`` (``-Infinity`` holds the second), a float with an exponent of three digits or more, and a run of
# 300 digits or more (a float of that size overflows). A text that matches none of them is decoded without the hooks.
_MAY_HOLD_NON_FINITE = re.compile(r"NaN|Infinity|[0-9][eE][+-]?[0-9]{3,}|[0-9]{300,}")


def loads_finite(text: str, *, name: str = "the JSON text") -> Any:
    """``json.loads`` that refuses ``NaN``, ``Infinity``, ``-Infinity`` and a float that overflows to infinity (``1e999``).

    Raises ``JsonNotFinite``. The hooks that find those cost a call for every float, so they run only for a text that
    could hold one (see ``_MAY_HOLD_NON_FINITE``). The depth is not judged here: a caller that needs it asks
    ``text_nests_deeper_than`` first.
    """

    if _MAY_HOLD_NON_FINITE.search(text) is None:
        return json.loads(text)
    try:
        return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)
    except JsonNotFinite:
        raise JsonNotFinite(name) from None


def loads_caller_json(text: str, *, name: str, limit: int = MAX_STORED_JSON_DEPTH) -> Any:
    """``json.loads`` of JSON text a caller typed or supplied in a file, held to the write limits.

    Refuses text nested more than ``limit`` levels before it is decoded (so the decoder cannot overflow), and refuses
    ``NaN``, ``Infinity``, ``-Infinity`` and a float that overflows to infinity (``1e999``) while it decodes. Raises
    ``JsonTooDeep`` or ``JsonNotFinite`` for those, and ``json.JSONDecodeError`` for text that is not JSON.
    """

    if text_nests_deeper_than(text, limit):
        raise JsonTooDeep(name)
    try:
        return loads_finite(text, name=name)
    except RecursionError:
        raise JsonTooDeep(name) from None


__all__ = [
    "DEEP_PLACEHOLDER",
    "JsonNotFinite",
    "JsonShapeError",
    "JsonTooDeep",
    "MAX_READ_JSON_DEPTH",
    "MAX_STORED_JSON_DEPTH",
    "NOT_FINITE",
    "TOO_DEEP",
    "cut_deep_json",
    "first_json_problem",
    "loads_bounded",
    "loads_caller_json",
    "loads_finite",
    "refuse_unstorable_json",
    "text_nests_deeper_than",
]
