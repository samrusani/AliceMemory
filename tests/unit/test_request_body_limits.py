"""A request body is capped before any layer reads it, and a body nested too deeply is a 422 (DB-006).

The internal security review of v0.19.0 found that the HTTP API read and parsed a
JSON body in full before it checked credentials, with no size limit: one
anonymous request of 200 MiB peaked near 6.3 GiB of memory and stalled the event
loop for about two seconds. Separately, a body nested a thousand levels deep
raised ``RecursionError`` out of three of the layers that parse a body and
answered HTTP 500.

The tests drive the real app in process over raw ASGI with a fake Postgres
store. A streamed body is generated on demand and the test counts the bytes the
app pulled through ``receive``. Each test names the mutation that must fail it.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import anyio
import pytest
import uvicorn

import alicebot_api.main as main_module
import alicebot_api.request_limits as request_limits
from alicebot_api.config import (
    DEFAULT_MAX_CONNECTOR_SYNC_BODY_BYTES,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    Settings,
)
from alicebot_api.public_errors import REQUEST_TOO_LARGE
from alicebot_api.request_limits import (
    MAX_JSON_NESTING,
    RequestBodyLimitMiddleware,
    body_limit_for,
    json_nesting_exceeds,
)
from tests.unit.api_edge_harness import KEY, SENTINEL, USER, Edge, Outcome

CAP = DEFAULT_MAX_REQUEST_BODY_BYTES
CHUNK = 1 << 20
COMMIT_PATH = "/v0/vnext/memories/commit"
SOURCES_PATH = "/v0/vnext/sources"
V1_COMMIT_PATH = "/v1/memory/operations/commit"
CAPTURES_PATH = "/v0/continuity/captures"
CLIP_CAPTURE_PATH = "/v0/vnext/connectors/browser-clipper/capture"
SYNC_PATH = "/v0/vnext/connectors/pdf_document/sync"
TELEGRAM_SYNC_PATH = "/v0/vnext/connectors/telegram/sync"
JSON = "application/json"
USER_HEADER = [("x-alicebot-user-id", USER)]


@pytest.fixture()
def edge(monkeypatch: pytest.MonkeyPatch) -> Edge:
    return Edge(monkeypatch)


def _deep(depth: int, *, user: bool = True) -> bytes:
    prefix = b'{"user_id": "' + USER.encode() + b'", "a": ' if user else b'{"a": '
    return prefix + b"[" * depth + b"]" * depth + b"}"


def _assert_too_large(outcome: Outcome) -> None:
    assert outcome.status == 413
    assert json.loads(outcome.body) == {
        "detail": {"code": REQUEST_TOO_LARGE.code, "message": REQUEST_TOO_LARGE.message}
    }
    assert outcome.response_starts == 1


# The size limit.


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read(edge: Edge) -> None:
    """413 with zero bytes pulled and zero calls to ``receive``, and the connection is told to close.

    Mutation: delete the declared-length check in ``RequestBodyLimitMiddleware``.
    The request is then refused only after the first chunk is read.
    """

    edge.configure()
    outcome = edge.call(
        "POST", COMMIT_PATH, stream_bytes=CAP + 1, declared_length=CAP + 1, content_type=JSON
    )
    _assert_too_large(outcome)
    assert outcome.pulled_bytes == 0
    assert outcome.receive_calls == 0
    assert (outcome.headers or {})["connection"] == "close"


def test_a_streamed_body_with_no_declared_length_is_cut_off_near_the_cap(edge: Edge) -> None:
    """Four times the cap, undeclared: 413, and the app pulled at most the cap plus one chunk.

    Mutation: delete the byte counter in ``limited_receive``. The whole 16 MiB
    is pulled and the answer is not a 413.
    """

    edge.configure()
    outcome = edge.call("POST", COMMIT_PATH, stream_bytes=CAP * 4, chunk_size=CHUNK, content_type=JSON)
    _assert_too_large(outcome)
    assert outcome.pulled_bytes <= CAP + CHUNK


def test_a_body_exactly_at_the_cap_is_not_refused(edge: Edge) -> None:
    """The cap is inclusive, declared or streamed.

    Mutations, each one alone: change ``declared > limit`` to ``>=``; change
    ``received > limit`` to ``>=``. A body of exactly the cap is then a 413.
    """

    edge.configure()
    declared = edge.call(
        "POST", COMMIT_PATH, stream_bytes=CAP, declared_length=CAP, chunk_size=65536, content_type=JSON
    )
    assert declared.status != 413
    assert declared.pulled_bytes == CAP
    undeclared = edge.call("POST", COMMIT_PATH, stream_bytes=CAP, chunk_size=65536, content_type=JSON)
    assert undeclared.status != 413
    assert undeclared.pulled_bytes == CAP
    one_over = edge.call("POST", COMMIT_PATH, stream_bytes=CAP + 1, chunk_size=65536, content_type=JSON)
    _assert_too_large(one_over)


@pytest.mark.parametrize(
    ("path", "bound"),
    (
        (V1_COMMIT_PATH, True),
        (V1_COMMIT_PATH, False),
        (COMMIT_PATH, True),
        (COMMIT_PATH, False),
        (SOURCES_PATH, False),
        (CAPTURES_PATH, True),
    ),
)
def test_the_cap_covers_every_surface_whoever_the_user_is(edge: Edge, path: str, bound: bool) -> None:
    """``/v1``, vNext and legacy ``/v0``, with and without a bound user, are all capped.

    Mutation: make ``body_limit_for`` return an unbounded limit for a path that
    does not start with ``/v0/vnext``, or skip the middleware for ``/v1``.
    """

    edge.configure(bound=bound)
    outcome = edge.call(
        "POST", path, stream_bytes=CAP * 2, chunk_size=CHUNK, content_type=JSON, extra_headers=USER_HEADER
    )
    _assert_too_large(outcome)
    assert outcome.pulled_bytes <= CAP + CHUNK


def test_the_cap_covers_a_caller_with_a_valid_key(edge: Edge) -> None:
    """An admin key does not exempt a request from the cap, declared or streamed.

    Mutation: skip the limit when an ``Authorization`` header is present.
    """

    edge.configure(keys=1)
    streamed = edge.call(
        "POST", COMMIT_PATH, stream_bytes=CAP * 2, chunk_size=CHUNK, content_type=JSON, authorization=f"Bearer {KEY}"
    )
    _assert_too_large(streamed)
    assert streamed.pulled_bytes <= CAP + CHUNK
    declared = edge.call(
        "POST",
        COMMIT_PATH,
        stream_bytes=CAP * 2,
        declared_length=CAP * 2,
        content_type=JSON,
        authorization=f"Bearer {KEY}",
    )
    _assert_too_large(declared)
    assert declared.pulled_bytes == 0


def test_the_cap_covers_a_declared_body_on_a_get(edge: Edge) -> None:
    """The declared length is checked whatever the method. Mutation: apply the check to methods that carry a body only."""

    edge.configure()
    outcome = edge.call(
        "GET", "/v0/vnext/projects", query={"user_id": USER}, stream_bytes=CAP * 2, declared_length=CAP * 2
    )
    _assert_too_large(outcome)
    assert outcome.pulled_bytes == 0


def test_the_connector_sync_routes_have_their_own_higher_cap(edge: Edge) -> None:
    """A sync body takes whole documents, so it may exceed the general cap but has a cap of its own.

    Mutations: return the general cap for a sync path in ``body_limit_for``; return
    the connector cap for every path.
    """

    edge.configure(max_request_body_bytes=1000, max_connector_sync_body_bytes=5000)
    for path in (SYNC_PATH, TELEGRAM_SYNC_PATH, "/v0/vnext/connectors/local-folder/sync"):
        inside = edge.call("POST", path, stream_bytes=4000, declared_length=4000, content_type=JSON)
        assert inside.status != 413, path
        over = edge.call("POST", path, stream_bytes=5001, declared_length=5001, content_type=JSON)
        _assert_too_large(over)
    other = edge.call("POST", COMMIT_PATH, stream_bytes=1001, declared_length=1001, content_type=JSON)
    _assert_too_large(other)
    assert edge.call("GET", SYNC_PATH, stream_bytes=1001, declared_length=1001).status == 413


def test_body_limit_for_picks_by_method_and_exact_path() -> None:
    """Only ``POST`` to ``/v0/vnext/connectors/<name>/sync`` gets the connector cap.

    Mutation: match by prefix or substring, or ignore the method.
    """

    def limit(method: str, path: str) -> int:
        return body_limit_for({"method": method, "path": path}, request_limit=1, connector_sync_limit=2)

    assert limit("POST", "/v0/vnext/connectors/pdf_document/sync") == 2
    assert limit("POST", "/v0/vnext/connectors/telegram/sync") == 2
    assert limit("GET", "/v0/vnext/connectors/telegram/sync") == 1
    assert limit("POST", "/v0/vnext/connectors/telegram/sync/extra") == 1
    assert limit("POST", "/v0/vnext/connectors/sync") == 1
    assert limit("POST", "/v0/vnext/connectors/a/b/sync") == 1
    assert limit("POST", "/v0/vnext/sources") == 1
    assert limit("POST", "/x/v0/vnext/connectors/telegram/sync") == 1


def test_the_limit_is_read_from_the_settings_on_every_request(edge: Edge) -> None:
    """A smaller cap in the settings is the cap. Mutation: hold the default in the middleware."""

    edge.configure(max_request_body_bytes=2048)
    assert edge.call("POST", COMMIT_PATH, stream_bytes=2049, declared_length=2049, content_type=JSON).status == 413
    assert edge.call("POST", COMMIT_PATH, stream_bytes=2048, declared_length=2048, content_type=JSON).status != 413


def test_the_settings_read_both_caps_from_the_environment_and_refuse_a_bad_value() -> None:
    """Defaults are 4 MiB and 32 MiB. A value that is not a positive integer is refused.

    Mutations: skip either environment read; drop either positive check.
    """

    assert Settings.from_env({}).max_request_body_bytes == CAP == 4 * 1024 * 1024
    assert Settings.from_env({}).max_connector_sync_body_bytes == DEFAULT_MAX_CONNECTOR_SYNC_BODY_BYTES
    settings = Settings.from_env(
        {"ALICEBOT_MAX_REQUEST_BODY_BYTES": "1000", "ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES": "2000"}
    )
    assert (settings.max_request_body_bytes, settings.max_connector_sync_body_bytes) == (1000, 2000)
    for name in ("ALICEBOT_MAX_REQUEST_BODY_BYTES", "ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES"):
        for bad in ("0", "-5", "many"):
            with pytest.raises(ValueError, match=name):
                Settings.from_env({name: bad})


def test_the_size_limit_is_the_outermost_layer() -> None:
    """It is a pure ASGI middleware registered last, so every body read comes after it.

    Mutation: register it before ``enforce_authenticated_user_identity``, or as a
    ``BaseHTTPMiddleware`` dispatch function.
    """

    first = main_module.app.user_middleware[0]
    assert first.cls is RequestBodyLimitMiddleware
    assert not issubclass(first.cls, main_module.BaseHTTPMiddleware) if hasattr(main_module, "BaseHTTPMiddleware") else True


# The middleware on its own.


async def _drive(
    app: Any,
    *,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> tuple[list[dict[str, Any]], int]:
    messages: list[dict[str, Any]] = []
    queue = list(chunks)
    pulled = 0

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        if not queue:
            return {"type": "http.disconnect"}
        body = queue.pop(0)
        pulled += len(body)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {"type": scope_type, "method": "POST", "path": "/echo", "headers": headers or [], "client": ("127.0.0.1", 1)}
    await app(scope, receive, send)
    return messages, pulled


def _echo_app(seen: list[bytes]) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        seen.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_a_body_under_the_cap_reaches_the_app_byte_for_byte() -> None:
    """Counting does not alter the body or the response. Mutation: drop or merge chunks in ``limited_receive``."""

    seen: list[bytes] = []
    app = RequestBodyLimitMiddleware(_echo_app(seen), limit_for=lambda scope: 10)
    messages, _ = anyio.run(lambda: _drive(app, chunks=[b"abc", b"def", b"gh"]))
    assert seen == [b"abcdefgh"]
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]
    assert messages[0]["status"] == 200


def test_a_refusal_swallows_what_a_reading_layer_then_raises_and_sends_one_response() -> None:
    """A layer mid-read sees a disconnect and raises. The 413 is the only response, with no traceback out.

    Mutations: let the exception out of ``__call__`` when the request was
    refused; let the app's own response through after the refusal.
    """

    async def reading_app(scope: Any, receive: Any, send: Any) -> None:
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise RuntimeError("client disconnected")
        finally:
            # A late response from a layer that lost the body must not reach the client.
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"late"})

    app = RequestBodyLimitMiddleware(reading_app, limit_for=lambda scope: 5)
    messages, pulled = anyio.run(lambda: _drive(app, chunks=[b"abc", b"def", b"ghi", b"jkl"]))
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]
    assert messages[0]["status"] == 413
    assert pulled == 6  # the chunk that crossed the cap, not the rest


def test_an_exception_from_the_app_is_not_swallowed_when_the_body_was_within_the_cap() -> None:
    """Only a refused request has its errors absorbed. Mutation: swallow every exception."""

    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("a real bug")

    app = RequestBodyLimitMiddleware(failing_app, limit_for=lambda scope: 100)
    with pytest.raises(RuntimeError, match="a real bug"):
        anyio.run(lambda: _drive(app, chunks=[b"abc"]))


def test_a_refusal_after_the_response_has_started_sends_nothing_more() -> None:
    """An app that answers before it has read the body keeps its response. Mutation: send the 413 anyway."""

    async def early_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted", "more_body": True})
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = RequestBodyLimitMiddleware(early_app, limit_for=lambda scope: 5)
    messages, _ = anyio.run(lambda: _drive(app, chunks=[b"abc", b"def", b"ghi"]))
    assert [m["type"] for m in messages].count("http.response.start") == 1
    assert messages[0]["status"] == 202


def test_a_scope_that_is_not_http_passes_straight_through() -> None:
    """Websocket and lifespan scopes are not bodies. Mutation: apply the limit to every scope type."""

    seen: list[tuple[str, Any]] = []
    limit_asked: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append((scope["type"], receive))

    def limit_for(scope: Any) -> int:
        limit_asked.append(scope["type"])
        return 0

    wrapped = RequestBodyLimitMiddleware(app, limit_for=limit_for)
    for scope_type in ("websocket", "lifespan"):
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        anyio.run(wrapped, {"type": scope_type, "headers": []}, receive, send)
        assert seen[-1] == (scope_type, receive)
        assert messages == []
    assert limit_asked == []


@pytest.mark.parametrize(
    ("headers", "refused"),
    (
        ([(b"content-length", b"11")], True),
        ([(b"content-length", b"10")], False),
        ([(b"content-length", b"abc")], False),
        ([(b"content-length", b"-5")], False),
        ([(b"content-length", b" 11 ")], True),
        ([(b"content-length", b"5"), (b"content-length", b"11")], True),
        ([(b"Content-Length", b"11")], True),
        ([], False),
    ),
)
def test_the_declared_length_is_read_the_way_a_server_would(headers: list[tuple[bytes, bytes]], refused: bool) -> None:
    """The largest well-formed value counts, whatever its case. A malformed value is not a length.

    Mutations: take the first Content-Length instead of the largest; compare the
    header name case-sensitively; accept a signed or non-numeric value.
    """

    seen: list[bytes] = []
    app = RequestBodyLimitMiddleware(_echo_app(seen), limit_for=lambda scope: 10)
    messages, pulled = anyio.run(lambda: _drive(app, chunks=[b"x"], headers=headers))
    assert (messages[0]["status"] == 413) is refused
    if refused:
        assert pulled == 0


# The ordering of the refusals against the body read.


@pytest.mark.parametrize("bound", (False, True), ids=("unbound", "bound"))
def test_a_keyless_request_from_another_peer_is_refused_before_its_body_is_read(edge: Edge, bound: bool) -> None:
    """401 with zero bytes pulled, for a body under the cap, with and without a bound user.

    The vNext gate used to read and parse the body before its peer check, and the
    identity layer reads it first when a user is bound. Mutations, each one
    alone: move the peer check in ``_vnext_protected_http_auth`` back below the
    body read (the unbound case pulls the body); delete the ``refused_by_gate``
    skip in ``enforce_authenticated_user_identity`` (the bound case pulls it).
    """

    edge.configure(bound=bound)
    body = json.dumps({"user_id": USER, "raw_text": "x" * 100_000}).encode()
    outcome = edge.call(
        "POST",
        SOURCES_PATH,
        body=body,
        declared_length=len(body),
        content_type=JSON,
        peer="203.0.113.9",
        extra_headers=USER_HEADER,
    )
    assert outcome.refused
    assert outcome.pulled_bytes == 0
    assert outcome.receive_calls == 0


@pytest.mark.parametrize("bound", (False, True), ids=("unbound", "bound"))
def test_a_keyless_request_with_a_hostile_host_is_refused_before_its_body_is_read(edge: Edge, bound: bool) -> None:
    """The Host and Origin rules also come before the body read. Mutation: run them only after it."""

    edge.configure(bound=bound)
    body = json.dumps({"user_id": USER, "raw_text": "x" * 100_000}).encode()
    for kwargs in ({"host": "rebind.evil.example:8000"}, {"origin": "https://evil.example"}):
        outcome = edge.call(
            "POST",
            SOURCES_PATH,
            body=body,
            declared_length=len(body),
            content_type=JSON,
            extra_headers=USER_HEADER,
            **kwargs,
        )
        assert outcome.refused, kwargs
        assert outcome.pulled_bytes == 0, kwargs


def test_the_browser_clip_capture_route_still_reads_its_body_to_find_the_capability(edge: Edge) -> None:
    """The one exception to the early refusal. A capability capture from another peer is not refused at the edge.

    Mutation: apply the early refusal to the capture route too. A capability
    capture then gets the 401 before its body is read.
    """

    edge.configure()
    body = json.dumps({"user_id": USER, "url": "https://example.org/a", "capture_capability": "cap-1"}).encode()
    outcome = edge.call(
        "POST",
        CLIP_CAPTURE_PATH,
        body=body,
        content_type="text/plain;charset=UTF-8",
        peer="203.0.113.9",
        origin="https://evil.example",
        host="rebind.evil.example:8000",
    )
    assert outcome.pulled_bytes > 0
    assert outcome.reached_handler


def test_a_capture_with_no_capability_from_another_peer_is_still_refused_after_its_body_is_read(edge: Edge) -> None:
    """The later check stays for the capture route. Mutation: delete the post-read keyless check."""

    edge.configure()
    body = json.dumps({"user_id": USER, "url": "https://example.org/a"}).encode()
    outcome = edge.call("POST", CLIP_CAPTURE_PATH, body=body, content_type=JSON, peer="203.0.113.9")
    assert outcome.refused
    assert outcome.pulled_bytes > 0


# (label, path, bound user, active keys, request options, refused at the gate before the body is read)
_ROWS = (
    ("vnext-unbound-keyless", COMMIT_PATH, False, 0, {}, False),
    ("vnext-bound-keyless", COMMIT_PATH, True, 0, {}, False),
    ("vnext-unbound-remote", COMMIT_PATH, False, 0, {"peer": "203.0.113.9"}, True),
    ("vnext-bound-remote", COMMIT_PATH, True, 0, {"peer": "203.0.113.9"}, True),
    ("vnext-hostile-host", COMMIT_PATH, True, 0, {"host": "rebind.evil.example:8000"}, True),
    ("vnext-junk-key", COMMIT_PATH, True, 0, {"authorization": "Bearer alice_sk_v1_junk_000000000000"}, False),
    ("vnext-valid-key", COMMIT_PATH, True, 1, {"authorization": f"Bearer {KEY}"}, False),
    ("v1-keyless", V1_COMMIT_PATH, True, 0, {}, False),
    ("v1-remote", V1_COMMIT_PATH, True, 0, {"peer": "203.0.113.9"}, True),
    ("v1-hostile-origin", V1_COMMIT_PATH, True, 0, {"origin": "https://evil.example"}, True),
    ("v1-valid-key", V1_COMMIT_PATH, True, 1, {"authorization": f"Bearer {KEY}"}, False),
    ("legacy-bound", CAPTURES_PATH, True, 0, {}, False),
    ("legacy-unbound", CAPTURES_PATH, False, 0, {}, False),
    ("sync-keyless", SYNC_PATH, True, 0, {}, False),
)


@pytest.mark.parametrize("declared", (True, False), ids=("declared", "streamed"))
@pytest.mark.parametrize(
    ("label", "path", "bound", "keys", "kwargs", "gate_refuses"), _ROWS, ids=[row[0] for row in _ROWS]
)
def test_no_row_of_the_ordering_table_pulls_more_than_the_cap_from_a_64_mib_body(
    edge: Edge, label: str, path: str, bound: bool, keys: int, kwargs: dict[str, Any], gate_refuses: bool, declared: bool
) -> None:
    """Every route, peer, user binding and key state: a 64 MiB body pulls at most the cap.

    The connector sync row has its own cap. A declared body is a 413 that pulls
    nothing. A streamed body is a 413 after at most the cap plus a chunk, except
    where a keyless gate refuses the request first: then it is the gate's 401 and
    nothing is pulled at all. Mutation: remove the limit for any one row's path,
    or restore any layer's unbounded read in front of it.
    """

    edge.configure(keys=keys, bound=bound)
    cap = edge.settings.max_connector_sync_body_bytes if path == SYNC_PATH else CAP
    total = 64 * 1024 * 1024
    outcome = edge.call(
        "POST",
        path,
        stream_bytes=total,
        chunk_size=CHUNK,
        declared_length=total if declared else None,
        content_type=JSON,
        extra_headers=USER_HEADER,
        **kwargs,
    )
    if gate_refuses and not declared:
        assert outcome.refused, label
        assert outcome.pulled_bytes == 0, label
        return
    _assert_too_large(outcome)
    assert outcome.pulled_bytes <= (0 if declared else cap + CHUNK), label


# The nesting limit.


def test_json_nesting_exceeds_counts_levels_of_arrays_and_objects() -> None:
    """256 levels pass and 257 do not, for arrays, objects and a mix.

    Mutations: change ``depth > limit`` to ``>=``; count only arrays; count only
    objects; return False when the byte prefilter passes.
    """

    assert not json_nesting_exceeds(b"[" * 256 + b"]" * 256)
    assert json_nesting_exceeds(b"[" * 257 + b"]" * 257)
    assert not json_nesting_exceeds(b'{"a":' * 256 + b"1" + b"}" * 256)
    assert json_nesting_exceeds(b'{"a":' * 257 + b"1" + b"}" * 257)
    assert json_nesting_exceeds(b'[{"a":' * 129 + b"1" + b"}]" * 129)
    assert not json_nesting_exceeds(b'[{"a":' * 128 + b"1" + b"}]" * 128)
    assert json_nesting_exceeds(b"[" * 100_000)
    assert MAX_JSON_NESTING == 256
    # Exactly at the limit, with more than 256 brackets in all so the walk runs.
    assert not json_nesting_exceeds(b"[" * 255 + b"[],[]" + b"]" * 255)
    assert json_nesting_exceeds(b"[" * 255 + b"[[]]" + b"]" * 255)
    assert not json_nesting_exceeds(b'{"a":' * 254 + b'{"b":[],"c":[]}' + b"}" * 254)
    assert json_nesting_exceeds(b'{"a":' * 254 + b'{"b":[[]]}' + b"}" * 254)


def test_brackets_inside_strings_and_flat_bodies_do_not_count() -> None:
    """A bracket in a string is text, an escaped quote does not end a string, and a flat list is depth one.

    Mutations: skip blanking the strings; end a string at an escaped quote; never
    decrement the depth on a closing bracket.
    """

    assert not json_nesting_exceeds(json.dumps(["[[[[" * 1000] * 50).encode())
    assert not json_nesting_exceeds(json.dumps({"t": '\\"[[[' * 3000}).encode())
    assert not json_nesting_exceeds(json.dumps({"t": '"[{' * 3000}).encode())
    assert not json_nesting_exceeds(b"[" + b",".join([b"[]"] * 5000) + b"]")
    assert not json_nesting_exceeds(b"[" + b",".join([b'{"a":[1]}'] * 5000) + b"]")
    assert json_nesting_exceeds(b'["' + b'\\"' * 5 + b'"' + b"," + b"[" * 300 + b"]" * 300 + b"]")


def test_a_non_utf8_body_is_checked_as_the_text_it_decodes_to() -> None:
    """UTF-16 and UTF-32 spell each bracket with extra bytes. Mutation: scan the raw bytes."""

    deep = "[" * 300 + "]" * 300
    for encoding in ("utf-16", "utf-16-le", "utf-32", "utf-8-sig"):
        assert json_nesting_exceeds(deep.encode(encoding)), encoding
    shallow = json.dumps({"t": "\\\"" + "[" * 300})
    for encoding in ("utf-16", "utf-32"):
        assert not json_nesting_exceeds(shallow.encode(encoding)), encoding
    # Bytes that do not decode are the decoder's to report, not a nesting verdict.
    assert not json_nesting_exceeds(b"\xff\xfe" + b"[\x00" * 300 + b"\x00")


def _body_routes() -> list[tuple[str, str]]:
    """Every method and path the app serves that takes a request body, from its own OpenAPI schema."""

    routes: list[tuple[str, str]] = []
    for path, item in main_module.app.openapi()["paths"].items():
        for method in item:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                routes.append((method.upper(), path))
    return sorted(routes)


def _fill(path: str) -> str:
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}")
        out = out[:start] + str(uuid4()) + out[end + 1 :]
    return out


_DEEP_ECHO = b"[[[[["


@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
@pytest.mark.parametrize("depth", (MAX_JSON_NESTING + 1, 1000, 100_000))
def test_a_body_nested_too_deeply_is_a_422_on_every_route_that_takes_a_body(edge: Edge, depth: int, bound: bool) -> None:
    """Every POST, PUT, PATCH and DELETE route answers 422 ``json_too_deep`` with nothing from the body in it.

    The routes are read from the app, so a route added later is covered. In
    v0.19.2 a body about 975 levels deep or more answered HTTP 500. Mutations,
    each one alone: delete the ``json_nesting_exceeds`` check from
    ``reject_lone_surrogate_json_body`` (the legacy routes then answer the
    framework's 422 with the nested body echoed back, or 500); delete the
    ``too_deep`` answer from the vNext gate; delete it from the ``/v1`` gate.
    """

    edge.configure(bound=bound)
    body = _deep(depth)
    routes = _body_routes()
    assert len(routes) > 60
    for expected in (COMMIT_PATH, V1_COMMIT_PATH, CAPTURES_PATH, CLIP_CAPTURE_PATH, "/v0/vnext/connectors/{connector_name}/sync"):
        assert ("POST", expected) in routes, expected
    for method, path in routes:
        outcome = edge.call(
            method,
            _fill(path),
            body=body,
            content_type=JSON,
            extra_headers=USER_HEADER,
            query=None if bound else {"user_id": USER},
        )
        if outcome.reached_handler:
            pytest.fail(f"{method} {path} reached its handler with a body nested {depth} levels deep")
        assert outcome.status == 422, (method, path, outcome.status, outcome.body[:200])
        assert json.loads(outcome.body) == {
            "detail": [{"type": "json_too_deep", "loc": ["body"], "msg": request_limits.JSON_TOO_DEEP_MESSAGE}]
        }, (method, path)
        assert _DEEP_ECHO not in outcome.body


@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
def test_the_gates_answer_a_too_deep_body_before_any_routing(edge: Edge, bound: bool) -> None:
    """The vNext and ``/v1`` gates read the body before the router runs, so they answer for it, as for a surrogate.

    A path no route takes is the case only the gates can answer: the innermost
    guard does not read a body for a 404. Mutations, each one alone: delete the
    ``too_deep`` answer from the vNext gate; delete it from the ``/v1`` gate.
    """

    edge.configure(bound=bound)
    for path in ("/v0/vnext/no-such-route", "/v1/no-such-route"):
        outcome = edge.call(
            "POST",
            path,
            body=_deep(MAX_JSON_NESTING + 1),
            content_type=JSON,
            extra_headers=USER_HEADER,
            query=None if bound else {"user_id": USER},
        )
        assert outcome.status == 422, (path, outcome.status)
        assert json.loads(outcome.body)["detail"][0]["type"] == "json_too_deep", path


def test_a_body_at_the_limit_is_not_refused_for_its_depth(edge: Edge) -> None:
    """256 levels reaches the route's own validation. Mutation: refuse at ``MAX_JSON_NESTING - 1``."""

    edge.configure()
    outcome = edge.call("POST", COMMIT_PATH, body=_deep(MAX_JSON_NESTING - 1), content_type=JSON, query={"user_id": USER})
    assert outcome.status == 422
    assert json.loads(outcome.body)["detail"][0]["type"] != "json_too_deep"


@pytest.mark.parametrize(
    ("path", "bound"),
    (
        (COMMIT_PATH, True),
        (COMMIT_PATH, False),
        (V1_COMMIT_PATH, True),
        (V1_COMMIT_PATH, False),
        (CAPTURES_PATH, True),
    ),
)
def test_a_deep_body_with_a_junk_key_is_a_client_error_at_every_parse_site(edge: Edge, path: str, bound: bool) -> None:
    """The three layers that parse a body answer below 500, whatever Authorization says.

    The vNext gate, the ``/v1`` gate and the identity layer each parse the body.
    Mutation: restore the inline ``request.json()`` and its narrow ``except`` in
    any one of them. That layer raises ``RecursionError`` out of the app.
    """

    edge.configure(keys=1, bound=bound)
    for authorization in ("Bearer alice_sk_v1_junk_000000000000", None, f"Bearer {KEY}"):
        outcome = edge.call(
            "POST",
            path,
            body=_deep(100_000),
            content_type=JSON,
            authorization=authorization,
            extra_headers=USER_HEADER,
            query=None if bound else {"user_id": USER},
        )
        assert 400 <= outcome.status < 500, (path, bound, authorization, outcome.status)
        assert _DEEP_ECHO not in outcome.body


@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
def test_the_decoder_giving_up_is_a_body_with_no_payload_even_when_the_scan_misses_it(
    edge: Edge, monkeypatch: pytest.MonkeyPatch, bound: bool
) -> None:
    """The byte scan is the first line. ``read_json_body`` still catches what the decoder raises.

    With the scan switched off, a body 100,000 levels deep reaches ``json.loads``,
    which raises ``RecursionError``, and an integer of 5,000 digits raises a
    plain ``ValueError``. Both end below 500 at every parse site. Mutations, each
    one alone: drop ``RecursionError`` from the ``except`` in ``read_json_body``;
    narrow ``ValueError`` to ``JSONDecodeError`` and ``UnicodeDecodeError``.
    """

    monkeypatch.setattr(request_limits, "json_nesting_exceeds", lambda raw, limit=MAX_JSON_NESTING: False)
    edge.configure(keys=1, bound=bound)
    huge_int = b'{"user_id": "' + USER.encode() + b'", "n": ' + b"1" * 5000 + b"}"
    for path in (COMMIT_PATH, V1_COMMIT_PATH, CAPTURES_PATH):
        for body in (_deep(100_000), huge_int):
            outcome = edge.call(
                "POST",
                path,
                body=body,
                content_type=JSON,
                authorization="Bearer alice_sk_v1_junk_000000000000",
                extra_headers=USER_HEADER,
                query=None if bound else {"user_id": USER},
            )
            assert outcome.status < 500, (path, outcome.status)


def test_a_huge_integer_in_a_body_is_no_longer_a_500_or_a_false_401(edge: Edge) -> None:
    """A body with a 5,000-digit integer: ``/v1`` answered HTTP 500 and the identity layer a 401 for it.

    Mutation: narrow the ``except`` in ``read_json_body`` to ``JSONDecodeError``
    and ``UnicodeDecodeError``. ``/v1`` then raises ``ValueError`` out of the app.
    """

    edge.configure(bound=True)
    body = b'{"user_id": "' + USER.encode() + b'", "n": ' + b"1" * 5000 + b"}"
    for path in (V1_COMMIT_PATH, CAPTURES_PATH, COMMIT_PATH):
        outcome = edge.call("POST", path, body=body, content_type=JSON, extra_headers=USER_HEADER)
        assert outcome.status < 500, path
        assert outcome.status != 401 or path == COMMIT_PATH or b"authentication_failed" not in outcome.body, path


def test_a_browser_clip_text_plain_body_nested_too_deeply_is_a_400(edge: Edge) -> None:
    """The clipper's simple request is parsed by its own code. Mutation: restore its narrow ``except``."""

    edge.configure()
    outcome = edge.call(
        "POST", CLIP_CAPTURE_PATH, body=_deep(100_000), content_type="text/plain;charset=UTF-8", extra_headers=USER_HEADER
    )
    assert outcome.status == 400
    assert _DEEP_ECHO not in outcome.body


def test_an_ordinary_nested_body_is_unchanged(edge: Edge) -> None:
    """Depth well inside the limit reaches the route's own validation and is echoed there as before.

    Mutation: refuse a body whose depth is at or above a small constant such as 8.
    """

    edge.configure()
    body = json.dumps({"user_id": USER, "title": "t", "meta": {"a": [{"b": [{"c": [1, 2, {"d": SENTINEL}]}]}]}}).encode()
    outcome = edge.call("POST", COMMIT_PATH, body=body, content_type=JSON, query={"user_id": USER})
    assert outcome.status in (200, 201, 422)
    assert json.loads(outcome.body).get("detail", [{}])[0].get("type") != "json_too_deep"


# A real server, over real sockets.


@contextmanager
def _live_server(app: Any) -> Iterator[int]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "the server did not start"
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(10)


def _flood(port: int, path: str, megabytes: int, *, declared: bool) -> tuple[int, dict[str, str], bytes, int]:
    """Send ``megabytes`` MiB of ``x`` to the server. Returns the status, headers, body and MiB sent before it answered."""

    chunk = b"x" * (1 << 20)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.putrequest("POST", path)
    connection.putheader("Content-Type", JSON)
    connection.putheader("X-AliceBot-User-Id", USER)
    if declared:
        connection.putheader("Content-Length", str(megabytes << 20))
    else:
        connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    sent = 0
    try:
        for _ in range(megabytes):
            connection.send(chunk if declared else b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            sent += 1
        if not declared:
            connection.send(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        pass  # the server answered and closed while the client was still sending
    response = connection.getresponse()
    headers = {key.lower(): value for key, value in response.getheaders()}
    return response.status, headers, response.read(), sent


@pytest.mark.parametrize("declared", (True, False), ids=("declared", "chunked"))
def test_a_live_server_refuses_a_flood_closes_the_connection_and_keeps_serving(edge: Edge, declared: bool) -> None:
    """A 64 MiB body to the real app over a socket: a 413 well before the client has sent it all.

    The refusal says ``Connection: close`` so the server drops the rest, and a
    new connection is served as before. Mutations, each one alone: delete the
    byte counter (the whole body is read and the answer is not a 413); delete
    the ``Connection: close`` header; delete the declared-length check (a
    declared body is read until the counter trips).
    """

    edge.configure()
    with _live_server(main_module.app) as port:
        status, headers, body, sent = _flood(port, COMMIT_PATH, 64, declared=declared)
        assert status == 413
        assert json.loads(body)["detail"]["code"] == "request_too_large"
        assert headers.get("connection") == "close"
        assert sent < 64
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request("GET", f"/v0/vnext/settings/brain-charter?user_id={USER}")
        follow_up = connection.getresponse()
        assert follow_up.status == 200
        assert SENTINEL.encode() in follow_up.read()
