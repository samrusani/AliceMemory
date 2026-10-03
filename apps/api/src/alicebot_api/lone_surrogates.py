"""Find lone surrogates in a request body and keep them out of an error body.

Python's JSON decoder turns an escape such as ``"\\ud800"`` into a str that
holds a lone surrogate, and ``json.loads`` on bytes accepts the UTF-8 form of
one too (it decodes with ``surrogatepass``). Such a str is not text: encoding it
as UTF-8 raises ``UnicodeEncodeError``, which is what a database driver, the
JSON response writer and the framework's validation error rendering all do with
it. Pydantic refuses it in a ``str`` field and puts it in the ``input`` of the
error it raises, so the rendering of that very error used to be the thing that
failed. A valid pair written as two escapes is decoded into one character and
is not a lone surrogate; any surrogate that is left in a decoded str is one.

Three places use this module, all wired in ``main.py``. The ``/v1`` and vNext
agent-key layers read a body themselves, before they authenticate, and check
what they parsed with ``payload_lone_surrogate_location`` before they use any
value from it. ``reject_lone_surrogate_json_body`` is the innermost middleware
and covers a request those layers let through, for a route that takes the
method: a field the route does not validate as a str (a dict or list of any
kind, a key) would otherwise reach the database driver. A request with no
matching route, or a path whose route does not take the method, keeps the 404
or 405 the router gives it and its body is not read. And
``render_validation_error`` is the last line, for a validation error a route
still raises, so rendering that error cannot fail.

A body the framework hands to a route as raw bytes is the other input that
rendering used to fail on. It does so for a request with no content type or a
non-JSON one (``text/plain``, ``application/octet-stream``), whatever route
takes it, and the error pydantic raises for it carries those bytes as its
``input``. Bytes that are not UTF-8 (a UTF-16 or UTF-32 JSON body, a body that
is not text) cannot be decoded for the response, which is ``UnicodeDecodeError``
in the framework's encoder. The renderer cuts such an error to ``type``, ``loc``
and ``msg``, so nothing from the body is echoed and the answer is the 422 of
any other validation error. In v0.19.2 it answered HTTP 500.

The check stands on the JSON decoder. A body nested more than
``request_limits.MAX_JSON_NESTING`` levels deep is not decoded at all: the guard
answers it with the 422 ``request_limits.json_too_deep_response`` builds, as the
layers that parse a body themselves do. In v0.19.2 such a body, when it was too
deep for the decoder or nested about 975 levels, raised ``RecursionError`` and
answered HTTP 500.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.routing import Match

from alicebot_api.request_limits import json_nesting_exceeds, json_too_deep_response

# Same text pydantic uses for a str field that holds one. The guard answers
# with it so a client sees one message whichever layer caught the body.
SURROGATE_MESSAGE = "Input should be a valid string, unable to parse raw data as a unicode string"
SURROGATE_ERROR_TYPE = "string_unicode"

# Stands in the error location for an object key that holds a lone surrogate.
# The key is never echoed.
KEY_LOCATION_MARKER = "[key]"

_LONE_SURROGATE = re.compile("[\\ud800-\\udfff]")

# Every way a body can decode to a lone surrogate, and no way to decode to one
# that this does not match: a ``\uD800`` to ``\uDFFF`` escape, the UTF-8 form
# (ED followed by A0 to BF) that ``surrogatepass`` accepts, and any NUL byte,
# because a UTF-16 or UTF-32 body spells every ASCII character with one. A body
# that matches none of them cannot hold a lone surrogate and is not parsed.
_MAY_HOLD_LONE_SURROGATE = re.compile(rb"\\u[dD][89a-fA-F][0-9a-fA-F]{2}|\xed[\xa0-\xbf]|\x00")

Location = tuple[str | int, ...]


def has_lone_surrogate(text: str) -> bool:
    """Report whether the text holds a surrogate code point."""

    return _LONE_SURROGATE.search(text) is not None


def _children(node: dict[Any, Any] | list[Any] | tuple[Any, ...]) -> Iterator[tuple[Any, Any]]:
    if isinstance(node, dict):
        return iter(node.items())
    return iter(enumerate(node))


def find_lone_surrogate(value: object) -> Location | None:
    """Return where a decoded JSON value holds a surrogate, or None.

    The location lists the object keys and list indexes down to the offending
    str, with ``KEY_LOCATION_MARKER`` last when the surrogate is in a key. A
    key that holds one is never part of the location. The walk keeps its own
    stack, so a deeply nested value cannot overflow the interpreter's.
    """

    if isinstance(value, str):
        return () if has_lone_surrogate(value) else None
    if not isinstance(value, (dict, list, tuple)):
        return None
    path: list[str | int] = []
    stack = [_children(value)]
    while stack:
        entry = next(stack[-1], None)
        if entry is None:
            stack.pop()
            if path:
                path.pop()
            continue
        key, child = entry
        if isinstance(key, str) and has_lone_surrogate(key):
            return (*path, KEY_LOCATION_MARKER)
        if isinstance(child, str):
            if has_lone_surrogate(child):
                return (*path, key)
        elif isinstance(child, (dict, list, tuple)):
            path.append(key)
            stack.append(_children(child))
    return None


def body_lone_surrogate_location(raw_body: bytes) -> Location | None:
    """Return where a JSON request body holds a surrogate, or None.

    The body is decoded the way ``Request.json()`` decodes it, so the answer is
    about the value the route would receive. A body that is not JSON, or is too
    deep for the decoder, is left to the framework and answers None.
    """

    if not raw_body or _MAY_HOLD_LONE_SURROGATE.search(raw_body) is None:
        return None
    try:
        decoded = json.loads(raw_body)
    except (ValueError, RecursionError):
        return None
    return find_lone_surrogate(decoded)


async def payload_lone_surrogate_location(request: Request, payload: object) -> Location | None:
    """Return where a payload parsed from the request body holds a surrogate, or None.

    The walk over a parsed body is slower than parsing it, so it runs only when
    the bytes the payload came from could decode to a lone surrogate: a body
    with no ``\\uD800`` to ``\\uDFFF`` escape, no UTF-8 surrogate and no NUL byte
    cannot hold one. An empty payload holds nothing and the body is not asked
    for. The caller has read the body already, so asking for it again is a
    cached read.
    """

    if not payload:
        return None
    if _MAY_HOLD_LONE_SURROGATE.search(await request.body()) is None:
        return None
    return find_lone_surrogate(payload)


def lone_surrogate_error(location: Location) -> dict[str, object]:
    """Build the validation error for a surrogate at a body location."""

    return {
        "type": SURROGATE_ERROR_TYPE,
        "loc": ["body", *location],
        "msg": SURROGATE_MESSAGE,
    }


def _clean_text(value: object, fallback: str) -> str:
    if isinstance(value, str) and not has_lone_surrogate(value):
        return value
    return fallback


def _clean_location(location: object) -> list[str | int]:
    if not isinstance(location, (list, tuple)):
        return ["body"]
    return [
        KEY_LOCATION_MARKER if isinstance(part, str) and has_lone_surrogate(part) else part
        for part in location
        if isinstance(part, (str, int))
    ]


def _cut_error(error: object) -> dict[str, object]:
    """Cut an error to ``type``, ``loc`` and ``msg``, which hold no part of the body.

    ``input`` and ``ctx`` are dropped. A part that holds a surrogate is replaced
    by the text pydantic itself uses for a str that cannot be decoded.
    """

    fields = error if isinstance(error, dict) else {}
    return {
        "type": _clean_text(fields.get("type"), SURROGATE_ERROR_TYPE),
        "loc": _clean_location(fields.get("loc")),
        "msg": _clean_text(fields.get("msg"), SURROGATE_MESSAGE),
    }


def withhold_lone_surrogate_errors(errors: list[Any]) -> list[Any]:
    """Rewrite the encoded validation errors that carry a surrogate.

    Takes the JSON-compatible list the framework's handler would send. An error
    with no surrogate in it is returned as it is. One that has a surrogate in
    its ``input``, ``loc``, ``msg`` or ``ctx`` is cut to ``type``, ``loc`` and
    ``msg``: ``input`` and ``ctx`` are dropped, a ``loc`` part or a ``msg`` that
    holds one is replaced. Nothing that holds a surrogate reaches the response.
    """

    return [_cut_error(error) if find_lone_surrogate(error) is not None else error for error in errors]


def _encode_error(error: Any) -> Any:
    """Encode one validation error the way the framework does, or cut it.

    An ``input`` that is bytes the framework cannot decode as UTF-8 makes its
    encoder raise ``UnicodeDecodeError``. That error is cut to ``type``, ``loc``
    and ``msg``, so none of the bytes are echoed.
    """

    try:
        return jsonable_encoder(error)
    except UnicodeDecodeError:
        return _cut_error(error)


def validation_error_response(errors: Sequence[Any]) -> JSONResponse:
    """Build the 422 the framework's handler builds, with the unencodable parts withheld.

    The framework sends ``jsonable_encoder`` of the errors under ``detail``. An
    error whose input is bytes that are not UTF-8 is cut by ``_encode_error``,
    and one that carries a surrogate is cut by ``withhold_lone_surrogate_errors``,
    so encoding the response cannot raise. An error with neither is sent as the
    framework sends it.
    """

    encoded = [_encode_error(error) for error in errors]
    return JSONResponse(status_code=422, content={"detail": withhold_lone_surrogate_errors(encoded)})


async def render_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Answer a validation error with the framework's own handler.

    The framework's handler writes each error's ``input`` into the response. It
    raises ``UnicodeEncodeError`` for a surrogate in it and ``UnicodeDecodeError``
    for bytes that are not UTF-8. Only then does the 422 come from
    ``validation_error_response``, which withholds what it cannot encode, so an
    error with neither in it is answered by the framework as it is answered in
    v0.19.2.
    """

    try:
        return await request_validation_exception_handler(request, exc)
    except UnicodeError:
        return validation_error_response(exc.errors())


def lone_surrogate_response(location: Location) -> JSONResponse:
    """Build the 422 for a surrogate found at a body location."""

    return JSONResponse(status_code=422, content={"detail": [lone_surrogate_error(location)]})


_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def declares_json_body(content_type: str) -> bool:
    """Report whether a request is one FastAPI decodes as JSON.

    A request with no content type is decoded as JSON too. ``text/plain`` is
    not: the browser clipper's simple request is parsed, and bounded, by its own
    code in ``main.py``, and its fields are all typed str.
    """

    media_type = content_type.split(";", 1)[0].strip().casefold()
    return (
        media_type == ""
        or media_type == "application/json"
        or (media_type.startswith("application/") and media_type.endswith("+json"))
    )


def request_has_matching_route(request: Request) -> bool:
    """Report whether a route takes this path and this method.

    A request that matches no route answers 404, and one that matches the path
    but not the method answers 405, whatever its body holds. Both are the
    router's to answer. A router that was included reports a full match for
    any route inside it, so the top-level routes are enough.
    """

    scope = request.scope
    return any(route.matches(scope)[0] is Match.FULL for route in request.app.router.routes)


async def reject_lone_surrogate_json_body(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Answer HTTP 422 for a JSON body that holds a lone surrogate.

    The error has the shape of any other validation error, a ``detail`` list of
    ``type``, ``loc`` and ``msg``, and never repeats the text. It is the error
    pydantic raises for a str field that holds one, minus its ``input``. The
    body is read only for a request that a route takes, by path and by method.
    A body nested more than ``MAX_JSON_NESTING`` levels deep gets the same shape
    of 422, with type ``json_too_deep``, before any route or the framework
    decodes it.
    """

    if (
        request.method.upper() in _BODY_METHODS
        and declares_json_body(request.headers.get("content-type", ""))
        and request_has_matching_route(request)
    ):
        raw_body = await request.body()
        if json_nesting_exceeds(raw_body):
            return json_too_deep_response()
        location = body_lone_surrogate_location(raw_body)
        if location is not None:
            return lone_surrogate_response(location)
    return await call_next(request)
