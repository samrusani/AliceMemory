"""Size and shape limits for an HTTP request body, applied before any layer reads it.

In v0.19.2 the vNext and ``/v1`` gates, the identity middleware and FastAPI each
read and parsed a JSON body in full, with no limit, and one anonymous request of
200 MiB peaked near 6.3 GiB of memory and stalled the event loop for about two
seconds. Even a request with a valid key was costly: the framework's 422 echoes
the whole submitted input back. A body nested a thousand levels deep raised
``RecursionError`` out of three of those parse sites and answered HTTP 500.

Two limits live here and ``main.py`` wires both.

* ``RequestBodyLimitMiddleware`` is a pure ASGI middleware, registered last so
  it is outermost. It answers 413 from a declared ``Content-Length`` before it
  reads a byte. For a chunked or undeclared body it counts the bytes the layers
  below pull through ``receive`` and refuses as soon as the total crosses the
  cap. It is not a ``BaseHTTPMiddleware``: those buffer the body themselves,
  which is what the limit has to come before.
* A JSON body may nest at most ``MAX_JSON_NESTING`` levels. ``read_json_body`` is
  the one place the three layers that parse a body themselves decode it, and it
  reports a body over that depth instead of decoding it. ``json_too_deep_response``
  is the 422 they answer, with the shape of any validation error and nothing
  from the body in it.

The nesting limit sits well below the decoder's own: ``json.loads`` fails near
10,000 levels, but the code that renders a validation error recurses in Python
and raised ``RecursionError`` for a body nested about 975 levels deep. A body is
checked as bytes first, so a hostile body never reaches the decoder at all, and
``read_json_body`` still catches whatever the decoder raises, so a decoder that
gives up earlier on another Python build is a refused body too.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, NamedTuple

from fastapi import Request
from fastapi.responses import JSONResponse

from alicebot_api.public_errors import REQUEST_TOO_LARGE

logger = logging.getLogger(__name__)

# The most levels of arrays and objects a JSON request body may nest.
MAX_JSON_NESTING = 256

JSON_TOO_DEEP_ERROR_TYPE = "json_too_deep"
JSON_TOO_DEEP_MESSAGE = f"JSON body is nested more than {MAX_JSON_NESTING} levels deep"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# A JSON string, escapes included. Possessive quantifiers keep the match linear.
_JSON_STRING = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+"', re.DOTALL)
_NOT_A_BRACKET = bytes(value for value in range(256) if value not in b"[]{}")
_OPENERS = (0x5B, 0x7B)


def json_nesting_exceeds(raw: bytes, limit: int = MAX_JSON_NESTING) -> bool:
    """Report whether a JSON body nests more than ``limit`` levels, without decoding it.

    A body with no more than ``limit`` opening brackets in all cannot nest that
    deep, and ``bytes.count`` answers that at C speed, so an ordinary body costs
    nothing. Otherwise the strings are blanked (a bracket inside a string is
    text) and the brackets that are left are walked with a counter, which stops
    at the first byte over the limit. The walk never recurses. A body that is
    not UTF-8 is re-encoded first, because a UTF-16 or UTF-32 body spells each
    ASCII character with extra bytes. A body that cannot be decoded is not the
    nesting check's to refuse: the decoder reports it.
    """

    if raw.count(b"[") + raw.count(b"{") <= limit:
        return False
    try:
        encoding = json.detect_encoding(raw)
    except (UnicodeDecodeError, ValueError):
        return False
    if encoding not in ("utf-8", "utf-8-sig"):
        try:
            raw = raw.decode(encoding).encode("utf-8", "surrogatepass")
        except (UnicodeError, ValueError):
            return False
    depth = 0
    for byte in _JSON_STRING.sub(b'""', raw).translate(None, _NOT_A_BRACKET):
        if byte in _OPENERS:
            depth += 1
            if depth > limit:
                return True
        elif depth:
            depth -= 1
    return False


class JsonBody(NamedTuple):
    """What a layer that parses a body itself learns from it.

    ``payload`` is the decoded top-level object, or None when the body is empty,
    is not JSON, is not an object, or cannot be decoded. ``too_deep`` is True
    when the body nests more than ``MAX_JSON_NESTING`` levels and was therefore
    not decoded.
    """

    payload: dict[str, object] | None
    too_deep: bool


async def read_json_body(request: Request) -> JsonBody:
    """Decode a request's JSON object, the one way the layers that parse a body do it.

    Catches ``RecursionError`` and every ``ValueError`` (a malformed body, bytes
    that are not text, an integer of more than 4,300 digits) next to
    ``JSONDecodeError``, so a body the decoder gives up on is a body with no
    payload, never an exception out of a middleware.
    """

    raw = await request.body()
    if json_nesting_exceeds(raw):
        return JsonBody(None, True)
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        return JsonBody(None, False)
    return JsonBody(decoded if isinstance(decoded, dict) else None, False)


def json_too_deep_response() -> JSONResponse:
    """Build the 422 for a body nested too deeply: the shape of a validation error, nothing echoed."""

    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": JSON_TOO_DEEP_ERROR_TYPE,
                    "loc": ["body"],
                    "msg": JSON_TOO_DEEP_MESSAGE,
                }
            ]
        },
    )


_CONNECTOR_SYNC_PATH = re.compile(r"/v0/vnext/connectors/[^/]+/sync")


def body_limit_for(scope: Scope, *, request_limit: int, connector_sync_limit: int) -> int:
    """Pick the body cap for a request.

    The connector sync routes take a list of whole documents (``items``,
    ``updates``) that no model bounds, so they get their own, higher cap. Every
    other route, whatever its caller, gets the general one.
    """

    if scope.get("method") == "POST" and _CONNECTOR_SYNC_PATH.fullmatch(str(scope.get("path", ""))):
        return connector_sync_limit
    return request_limit


def _declared_length(scope: Scope) -> int | None:
    """The largest well-formed Content-Length the request declares, or None."""

    declared: int | None = None
    for name, value in scope.get("headers", ()):
        if name.lower() != b"content-length":
            continue
        text = value.strip()
        if text.isascii() and text.isdigit():
            length = int(text)
            declared = length if declared is None else max(declared, length)
    return declared


class RequestBodyLimitMiddleware:
    """Refuse a request body over a byte cap with 413, before any layer reads it.

    ``limit_for`` maps the ASGI scope to the cap for that request. A declared
    ``Content-Length`` over the cap is refused without reading a byte. Otherwise
    the body is counted as the layers below pull it, and the request is refused
    as soon as the total crosses the cap, so a chunked body cannot stream past
    it. The refusal closes the connection (``Connection: close``), so the server
    stops reading the rest. A layer that was mid-read sees the client disconnect,
    and whatever it then tries to answer is dropped: the 413 is the response.
    """

    def __init__(self, app: ASGIApp, *, limit_for: Callable[[Scope], int]) -> None:
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            logger.warning("request body over the limit refused before it was read (limit=%d)", limit)
            await _refuse(scope, receive, send)
            return

        refused = False
        response_started = False
        received = 0

        async def limited_receive() -> Message:
            nonlocal refused, received
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            received += len(message.get("body", b""))
            if received > limit:
                refused = True
                if not response_started:
                    logger.warning("request body over the limit refused while it was read (limit=%d)", limit)
                    await _refuse(scope, receive, send)
                return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if refused:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not refused:
                raise
            # A layer that was reading the body saw the disconnect and raised.
            # The client has its 413; there is nothing more to say.


async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=REQUEST_TOO_LARGE.status_code,
        content={"detail": {"code": REQUEST_TOO_LARGE.code, "message": REQUEST_TOO_LARGE.message}},
        headers={"Connection": "close"},
    )
    await response(scope, receive, send)
