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
agent-key layers read a body themselves, before they authenticate, and call
``find_lone_surrogate`` on what they parsed. ``reject_lone_surrogate_json_body``
is the innermost middleware and covers every other route for a request those
layers let through: a field the route does not validate as a str (a dict or list
of any kind, a key) would otherwise reach the database driver. And
``validation_error_response`` is the last line, for a validation error a route
still raises, so rendering that error cannot fail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

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


def withhold_lone_surrogate_errors(errors: list[Any]) -> list[Any]:
    """Rewrite the encoded validation errors that carry a surrogate.

    Takes the JSON-compatible list the framework's handler would send. An error
    with no surrogate in it is returned as it is. One that has a surrogate in
    its ``input``, ``loc``, ``msg`` or ``ctx`` is cut to ``type``, ``loc`` and
    ``msg``: ``input`` and ``ctx`` are dropped, a ``loc`` part or a ``msg`` that
    holds one is replaced. Nothing that holds a surrogate reaches the response.
    """

    cleaned: list[Any] = []
    for error in errors:
        if find_lone_surrogate(error) is None:
            cleaned.append(error)
            continue
        fields = error if isinstance(error, dict) else {}
        cleaned.append(
            {
                "type": _clean_text(fields.get("type"), SURROGATE_ERROR_TYPE),
                "loc": _clean_location(fields.get("loc")),
                "msg": _clean_text(fields.get("msg"), SURROGATE_MESSAGE),
            }
        )
    return cleaned


def validation_error_response(errors: Sequence[Any]) -> JSONResponse:
    """Build the 422 the framework's handler builds, with every surrogate withheld.

    The framework sends ``jsonable_encoder`` of the errors under ``detail``. An
    error that carries a surrogate is cut by ``withhold_lone_surrogate_errors``
    first, so encoding the response cannot raise.
    """

    return JSONResponse(
        status_code=422,
        content={"detail": withhold_lone_surrogate_errors(jsonable_encoder(errors))},
    )


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


async def reject_lone_surrogate_json_body(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Answer HTTP 422 for a JSON body that holds a lone surrogate.

    The error has the shape of any other validation error, a ``detail`` list of
    ``type``, ``loc`` and ``msg``, and never repeats the text. It is the error
    pydantic raises for a str field that holds one, minus its ``input``.
    """

    if request.method.upper() in _BODY_METHODS and declares_json_body(request.headers.get("content-type", "")):
        location = body_lone_surrogate_location(await request.body())
        if location is not None:
            return lone_surrogate_response(location)
    return await call_next(request)
