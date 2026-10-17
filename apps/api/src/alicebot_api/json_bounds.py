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
route stops at the body limit of 256 levels), can still nest deeper than the readers can walk. The common case pays
nothing for that: ``loads_stored`` is ``json.loads`` and a read of one context variable. A stored value is returned as
it was stored, whole, as long as every reader can walk it. When a reader cannot, the request fails with
``RecursionError`` at the boundary of the route or the tool, and the boundary runs the request once more with the
context variable set (``retry_cutting_deep_json``, the ASGI middleware in ``deep_row_retry``): from then on every stored
column is decoded by ``loads_bounded``, which reads a text nested no deeper than ``MAX_READ_JSON_DEPTH`` levels as
``json.loads`` does and replaces the part of a deeper text below that depth by the string ``DEEP_PLACEHOLDER`` before
it decodes it. A text that the decoder itself cannot take (nested past what ``json.loads`` allows) is cut the same way
at once. So no route fails for every caller because of one stored row, and the rest of the row is returned as it was.
The cut is made by a scan that keeps its own counter, never by recursion. A writer that reads a row in the retry (or one
nested past what the decoder takes) and writes it back stores the placeholder in the place of the part that was cut.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import deque
from collections.abc import Callable, Hashable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import accumulate
from typing import Any, TypeVar

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
_OPENERS = frozenset("[{")
_CLOSERS = frozenset("]}")


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


# A JSON string alone, with the same possessive quantifiers as ``_TOKEN``.
_STRING = re.compile(r'"(?:[^"\\]++|\\.|\\\Z)*+(?:"|\Z)', re.DOTALL)
# Every byte that is not a bracket, for ``bytes.translate``.
_NOT_A_BRACKET = bytes(value for value in range(256) if value not in b"[]{}")
# What each bracket does to the depth.
_STEP = {0x5B: 1, 0x7B: 1, 0x5D: -1, 0x7D: -1}


def text_nests_deeper_than(text: str, limit: int) -> bool:
    """Whether JSON text opens more than ``limit`` levels of arrays and objects at once, without decoding it.

    A text with no more than ``limit`` opening brackets in all cannot nest that deep, and ``str.count`` answers that at
    C speed, so an ordinary column costs two counts. Otherwise the strings are blanked (a bracket inside one is text),
    only the brackets are kept, and the running depth is taken with ``itertools.accumulate``: every step is a C call,
    so a large document costs a few passes at C speed and no Python loop per bracket, and nothing recurses.
    """

    if text.count("[") + text.count("{") <= limit:
        return False
    brackets = _STRING.sub('""', text).encode("utf-8", "surrogatepass").translate(None, _NOT_A_BRACKET)
    return max(accumulate(map(_STEP.__getitem__, brackets)), default=0) > limit


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
        piece = match.group()
        if piece in _OPENERS:
            depth += 1
            if depth == limit + 1:
                pieces.append(text[kept_from : match.start()])
                pieces.append(json.dumps(DEEP_PLACEHOLDER))
                cutting = True
        elif piece in _CLOSERS:
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


logger = logging.getLogger(__name__)

# Set while a request or a tool call runs its second attempt, after a reader overflowed on a deep stored row.
_CUT_DEEP: ContextVar[bool] = ContextVar("alicebot_cut_deep_json", default=False)
_Result = TypeVar("_Result")


@contextmanager
def cutting_deep_json() -> Iterator[None]:
    """Decode every stored column with ``loads_bounded`` for the code that runs inside, in this context and the tasks and
    threads that copy it."""

    token = _CUT_DEEP.set(True)
    try:
        yield
    finally:
        _CUT_DEEP.reset(token)


def loads_stored(data: object) -> Any:
    """The decoder of every stored JSON column: ``json.loads``, read as it was stored.

    It costs what ``json.loads`` costs and one context variable read. It cuts the part of a text below
    ``MAX_READ_JSON_DEPTH`` levels in two cases only: the decoder itself cannot take the text (``RecursionError``), and
    the code that is running is the second attempt of a request or a tool call that overflowed on a deep row
    (``cutting_deep_json``).
    """

    if _CUT_DEEP.get():
        return loads_bounded(data)
    try:
        return json.loads(data)  # type: ignore[arg-type]
    except RecursionError:
        return loads_bounded(data)


def overflowed(error: BaseException) -> bool:
    """Whether ``error``, or an error it was raised from or while handling, or one in an exception group, is a
    ``RecursionError``. The walk keeps its own stack and visits each error once."""

    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RecursionError):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
        pending.extend(getattr(current, "exceptions", ()))
    return False


class _Scope:
    """The attempts of one tool call: whether the call has been run again, and what its first attempt left behind that must
    not be done twice. ``durable`` holds the results of work the first attempt committed (the policy check of the agent
    and the compiled context pack, which write audit events); ``replay`` hands them back to the second attempt in the
    order they were made."""

    __slots__ = ("retried", "durable", "replay")

    def __init__(self) -> None:
        self.retried = False
        self.durable: list[tuple[Hashable, Any]] = []
        self.replay: dict[Hashable, deque[Any]] = {}


# The scope of the tool call that is running, set by the outermost ``retry_cutting_deep_json``.
_SCOPE: ContextVar[_Scope | None] = ContextVar("alicebot_retry_scope", default=None)
# The results remembered by the transaction that is open, kept only if it commits.
_UNIT: ContextVar[list[tuple[Hashable, Any]] | None] = ContextVar("alicebot_retry_unit", default=None)
_NOT_REMEMBERED: Any = object()


def retry_cutting_deep_json(call: Callable[[], _Result]) -> _Result:
    """Run ``call``; when it fails with a ``RecursionError`` (see ``overflowed``), run it once more with the deep part of
    every stored column cut. A second failure, or a failure of any other kind, is raised as it is.

    The retry is once for the whole tool call, however many layers ask for it: the first caller owns the scope, and a
    caller inside it (the registry inside the server) neither starts another nor runs a third attempt.
    """

    scope = _SCOPE.get()
    token = None
    if scope is None:
        scope = _Scope()
        token = _SCOPE.set(scope)
    try:
        try:
            return call()
        except Exception as error:
            if scope.retried or _CUT_DEEP.get() or not overflowed(error):
                raise
        scope.retried = True
        scope.replay = {}
        for key, value in scope.durable:
            scope.replay.setdefault(key, deque()).append(value)
        scope.durable = []
        logger.warning("A stored row nests too deeply for a reader; the call runs again with the deep part cut")
        with cutting_deep_json():
            return call()
    finally:
        if token is not None:
            _SCOPE.reset(token)


@contextmanager
def durable_unit() -> Iterator[None]:
    """One transaction of a tool call that may be run again. What ``remember_durable`` is given inside is kept for the
    second attempt only if the block ends without an error, which is when the transaction commits; a block that fails
    rolled its writes back, so the second attempt has to make them."""

    scope = _SCOPE.get()
    if scope is None:
        yield
        return
    unit: list[tuple[Hashable, Any]] = []
    token = _UNIT.set(unit)
    try:
        yield
        scope.durable.extend(unit)  # not reached when the block raised
    finally:
        _UNIT.reset(token)


def replay_durable(key: Hashable) -> Any:
    """The result the first attempt of this tool call committed for ``key``, or ``_NOT_REMEMBERED`` (test with
    ``is_remembered``). Results come back in the order they were made, so a call that makes the same check twice gets each."""

    scope = _SCOPE.get()
    if scope is not None:
        queue = scope.replay.get(key)
        if queue:
            return queue.popleft()
    return _NOT_REMEMBERED


def is_remembered(value: Any) -> bool:
    return value is not _NOT_REMEMBERED


def remember_durable(key: Hashable, value: Any) -> None:
    """Note a result made inside a ``durable_unit``; it is replayed on the second attempt if that unit committed."""

    unit = _UNIT.get()
    if unit is not None:
        unit.append((key, value))


def open_read_retry() -> tuple[_Scope, Any]:
    """The scope of one HTTP read, shared by its first attempt and the retry. Returns ``(scope, token)``."""

    scope = _Scope()
    return scope, _SCOPE.set(scope)


def close_read_retry(token: Any) -> None:
    _SCOPE.reset(token)


def promote_committed(scope: _Scope) -> None:
    """Hand the first attempt's committed results to the retry, in the order they were made."""

    scope.retried = True
    scope.replay = {}
    for key, value in scope.durable:
        scope.replay.setdefault(key, deque()).append(value)
    scope.durable = []


def committed_once(key: Hashable, compute: Callable[[], _Result], *, replay: Callable[[Any], Any] | None = None) -> _Result:
    """``compute()``, or on the retry of an HTTP read whose first attempt committed it, that result.

    The result is kept only when ``compute`` runs inside a ``durable_unit`` that then commits, which is the transaction
    of the request. A first attempt that rolled back left nothing, and the retry runs ``compute`` again.
    """

    done = replay_durable(key)
    if is_remembered(done):
        return done if replay is None else replay(done)
    result = compute()
    remember_durable(key, result)
    return result


def durable_once(key: Hashable, compute: Callable[[], _Result], *, replay: Callable[[Any], Any] | None = None) -> _Result:
    """``compute()``, or on the second attempt of a tool call that already finished it, the result of the first attempt
    (through ``replay`` when given). ``compute`` commits its own transactions: that it returned is the proof, so a result
    is remembered only when it returns, and an error leaves nothing to replay."""

    scope = _SCOPE.get()
    if scope is None:
        return compute()
    queue = scope.replay.get(key)
    if queue:
        value = queue.popleft()
        return value if replay is None else replay(value)
    result = compute()
    scope.durable.append((key, result))
    return result


def cut_deep_value(value: Any, limit: int = MAX_READ_JSON_DEPTH) -> Any:
    """A copy of a decoded JSON value with each array or object nested deeper than ``limit`` levels replaced by
    ``DEEP_PLACEHOLDER``, as ``loads_bounded`` cuts a text. The walk keeps its own stack, so it cannot overflow."""

    if not isinstance(value, (dict, list, tuple)):
        return value
    root: dict[Any, Any] | list[Any] = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any, int]] = [(value, root, 1)]
    while stack:
        source, target, depth = stack.pop()
        for key, child in source.items() if isinstance(source, dict) else enumerate(source):
            if isinstance(child, (dict, list, tuple)):
                if depth + 1 > limit:
                    copied: Any = DEEP_PLACEHOLDER
                else:
                    copied = {} if isinstance(child, dict) else []
                    stack.append((child, copied, depth + 1))
            else:
                copied = child
            if isinstance(target, dict):
                target[key] = copied
            else:
                target.append(copied)
    return root


def _refuse_constant(_name: str) -> object:
    raise JsonNotFinite("the JSON text")


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise JsonNotFinite("the JSON text")
    return number


# Every way a JSON text can hold a number that is not finite, and no way to hold one that this does not match: the words
# ``NaN`` and ``Infinity`` (``-Infinity`` holds the second), a float with an exponent of three digits or more, and a run of
# 200 digits or more (a float of 300 digits overflows, and so does one of 200 to 299 digits with an exponent of 11 or more;
# a shorter run needs an exponent of 111 or more). A text that matches none of them is decoded without the hooks.
_MAY_HOLD_NON_FINITE = re.compile(r"NaN|Infinity|[0-9][eE][+-]?[0-9]{3,}|[0-9]{200,}")


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
    "close_read_retry",
    "committed_once",
    "cut_deep_json",
    "cut_deep_value",
    "cutting_deep_json",
    "durable_once",
    "durable_unit",
    "open_read_retry",
    "promote_committed",
    "first_json_problem",
    "is_remembered",
    "loads_bounded",
    "loads_caller_json",
    "loads_finite",
    "loads_stored",
    "overflowed",
    "refuse_unstorable_json",
    "remember_durable",
    "replay_durable",
    "retry_cutting_deep_json",
    "text_nests_deeper_than",
]
