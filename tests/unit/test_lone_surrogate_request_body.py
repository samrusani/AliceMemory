"""A lone surrogate in a JSON request body is a 422, never a 500.

v0.19.2 answered HTTP 500 for a body that carried one. Pydantic refuses it in a
str field and puts it in the error's ``input``, and the framework's handler then
failed to encode that error as UTF-8. A surrogate in a place pydantic does not
check as a str (a dict or list of any kind, a key) went on to the database
driver, which cannot encode it either.

Three places hold the line. The ``/v1`` and vNext agent-key layers refuse the
body where they parse it, ``reject_lone_surrogate_json_body`` is the innermost
middleware and refuses it for every other route, and
``_alice_request_validation_error`` keeps a surrogate out of the validation
error a route still raises. The tests drive the real app in process over raw
ASGI, so the bytes of the body are exactly what the test wrote.

Mutations that restore v0.19.2 as a whole: copy the saved v0.19.2 ``main.py``
back over the changed one. Each test names the single-layer mutation that
fails it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import anyio
import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.lone_surrogates import (
    body_lone_surrogate_location,
    declares_json_body,
    find_lone_surrogate,
    reject_lone_surrogate_json_body,
    withhold_lone_surrogate_errors,
)
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.routers import vnext_memories as vnext_memories_router
from tests.unit.test_vnext_main import FakeVNextStore

USER_ID = str(uuid4())
SURROGATE_MESSAGE = "Input should be a valid string, unable to parse raw data as a unicode string"
# Text around the surrogate in every body. It must never come back in a response.
SENTINEL = "SENTINEL-4471"


class _HandlerReached(Exception):
    """A route handler opened its store connection."""


def _asgi_exchange(
    app: Any,
    method: str,
    path: str,
    body: bytes,
    *,
    headers: list[tuple[bytes, bytes]],
    client: tuple[str, int] = ("127.0.0.1", 50000),
) -> tuple[int, dict[str, str], bytes, int]:
    """One request over raw ASGI: the status, the response headers, the body, and how many times the body was read."""

    messages: list[dict[str, Any]] = []
    body_reads = 0
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received, body_reads
        if received:
            return {"type": "http.disconnect"}
        received = True
        body_reads += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": client,
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return int(start["status"]), response_headers, raw, body_reads


def _asgi_call(
    app: Any,
    method: str,
    path: str,
    body: bytes,
    *,
    headers: list[tuple[bytes, bytes]],
) -> tuple[int, bytes]:
    status, _headers, raw, _reads = _asgi_exchange(app, method, path, body, headers=headers)
    return status, raw


@pytest.fixture()
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real app with every handler's store connection replaced by a tripwire.

    A name lands in the list when a route handler runs past the middleware and
    reaches for its store. The agent-key auth layers get a connection that
    yields nothing, so a request with no key resolves no identity.
    """

    calls: list[str] = []
    settings = Settings(app_env="development", auth_user_id="", database_url="postgresql://db")

    def tripwire(name: str) -> Callable[..., Any]:
        def connection(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise _HandlerReached(name)

        return connection

    @contextmanager
    def auth_connection(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "user_connection", auth_connection)
    monkeypatch.setattr(main_module, "PostgresVNextStore", lambda _conn: FakeVNextStore(None))
    for module in (continuity_router, vnext_memories_router):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(module, "user_connection", tripwire(module.__name__.rsplit(".", 1)[1]))
    return calls


@pytest.fixture()
def auth_payloads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record the body each of the two agent-key auth layers is handed.

    Both read a value from the body before the route does, and both go on to a
    database lookup, so neither may ever see a lone surrogate.
    """

    seen: list[dict[str, object]] = []

    def record_v1(**kwargs: Any) -> None:
        seen.append(kwargs["payload"])
        return None

    def record_vnext(**kwargs: Any) -> tuple[None, None]:
        seen.append(kwargs["payload"])
        return None, None

    monkeypatch.setattr(main_module, "_resolve_v1_http_auth", record_v1)
    monkeypatch.setattr(main_module, "_resolve_vnext_http_auth", record_vnext)
    return seen


def _post(
    path: str,
    body: bytes,
    *,
    content_type: str | None = "application/json",
) -> tuple[int | None, bytes]:
    """POST to the real app. The status is None when a handler was reached."""

    headers = [(b"x-alicebot-user-id", USER_ID.encode())]
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    try:
        return _asgi_call(main_module.app, "POST", path, body, headers=headers)
    except _HandlerReached:
        return None, b""


def _escaped(payload: object) -> bytes:
    """The body as the Python json module writes it: a surrogate becomes a ``\\ud800`` escape."""

    return json.dumps(payload).encode("ascii")


def _raw(payload: object) -> bytes:
    """The body with each surrogate written as its three UTF-8 bytes, which ``json.loads`` accepts."""

    return json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass")


def _error(*location: str | int) -> dict[str, object]:
    return {"detail": [{"type": "string_unicode", "loc": ["body", *location], "msg": SURROGATE_MESSAGE}]}


def _assert_refused_without_echo(status: int | None, raw: bytes, *, expected: dict[str, object]) -> None:
    assert status == 422, raw
    assert json.loads(raw) == expected
    text = raw.decode("utf-8")
    assert SENTINEL not in text
    assert "ud800" not in text.lower()
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in text)


_CANDIDATES = "/v0/continuity/captures/candidates"
_CAPTURES = "/v0/continuity/captures"
_COMMIT = "/v0/continuity/captures/commit"
_V1_GENERATE = "/v1/memory/operations/candidates/generate"
_INGEST = "/v0/vnext/agents/ingest-output"

_ROUTE_CASES = {
    "str-field-user-content": (_CANDIDATES, {"user_content": f"{SENTINEL} \ud800"}, ("user_content",)),
    "str-field-assistant-content": (_CANDIDATES, {"assistant_content": f"\udfff {SENTINEL}"}, ("assistant_content",)),
    "str-field-raw-content": (_CAPTURES, {"raw_content": f"a {SENTINEL} \ud83d b"}, ("raw_content",)),
    "str-field-on-v1": (_V1_GENERATE, {"user_content": f"{SENTINEL} \ud800"}, ("user_content",)),
    "list-of-dicts-value": (
        _COMMIT,
        {"candidates": [{"title": "ok", "details": {"notes": ["fine", f"{SENTINEL} \ud800"]}}]},
        ("candidates", 0, "details", "notes", 1),
    ),
    "list-of-dicts-key": (_COMMIT, {"candidates": [{f"{SENTINEL}\ud800": 1}]}, ("candidates", 0, "[key]")),
    "unknown-top-level-key": (_CAPTURES, {"raw_content": "fine", f"{SENTINEL}\ud800": 1}, ("[key]",)),
    "any-typed-list-on-vnext": (
        _INGEST,
        {
            "agent_id": "agent-1",
            "title": "t",
            "content": "c",
            "source_refs": [{"ref": [f"{SENTINEL} \ud800"]}],
        },
        ("source_refs", 0, "ref", 0),
    ),
}


@pytest.mark.parametrize("encode", (_escaped, _raw), ids=("escape", "raw-bytes"))
@pytest.mark.parametrize("case", sorted(_ROUTE_CASES))
def test_a_lone_surrogate_anywhere_in_a_body_is_a_422_that_never_reaches_a_handler(
    reached: list[str],
    case: str,
    encode: Callable[[object], bytes],
) -> None:
    """The body is refused with a validation error and no route runs.

    Eight places across the continuity, ``/v1`` and vNext routes: a str field,
    a value inside a dict or list the route types as ``object``, an object key
    in that structure, and an unknown top-level key, each as a ``\\ud800``
    escape and as the raw UTF-8 bytes of a surrogate. The error has the shape of any
    validation error, says where, and echoes nothing from the body.

    Mutations, each one alone. Unregister ``reject_lone_surrogate_json_body`` in
    ``main.py``: the continuity commit cases (a value and a key inside the dict
    the route takes as any value) reach their handler, and the unknown top-level
    key answers the framework's own ``extra_forbidden``, so those fail. The
    string-field cases still pass, because the handler layer answers the same 422
    for a string field, and the ``/v1`` and vNext cases pass because those layers
    refuse the body where they read it. Make ``body_lone_surrogate_location``
    return None: the same cases fail. Copy the saved v0.19.2 ``main.py`` back:
    every case fails with a 500 or reaches its handler.
    """

    path, extra, location = _ROUTE_CASES[case]
    payload = {"user_id": USER_ID, **extra}
    status, raw = _post(path, encode(payload))

    _assert_refused_without_echo(status, raw, expected=_error(*location))
    assert reached == []


@pytest.mark.parametrize("encode", (_escaped, _raw), ids=("escape", "raw-bytes"))
def test_the_user_id_the_header_supplies_does_not_hide_a_surrogate(
    reached: list[str], encode: Callable[[object], bytes]
) -> None:
    """A header-only client, whose body the identity middleware rewrites, is refused too.

    The Hermes plugin sends ``user_id`` only in a header. The middleware that
    adds it to the body writes the whole body back as ASCII, so a raw-byte
    surrogate reaches the guard as an escape. The key case reaches no
    validation, so only the guard can refuse it. Mutation: unregister
    ``reject_lone_surrogate_json_body``. The handler is reached.
    """

    body = encode({"candidates": [{f"{SENTINEL}\ud800": 1}]})
    assert b"user_id" not in body
    status, raw = _post(_COMMIT, body)

    _assert_refused_without_echo(status, raw, expected=_error("candidates", 0, "[key]"))
    assert reached == []


def test_no_auth_layer_reads_a_value_from_a_body_that_holds_a_surrogate(
    reached: list[str], auth_payloads: list[dict[str, object]]
) -> None:
    """The ``/v1`` and vNext agent-key layers refuse the body where they read it.

    Both read the body before they authenticate and hand fields of it to a
    database lookup. Mutations, each one alone: delete the
    ``find_lone_surrogate(payload)`` check from ``enforce_v1_agent_authentication``;
    delete it from ``_vnext_protected_http_auth``. That layer is then handed the
    body, ``auth_payloads`` is not empty, and the request gets no 422.
    """

    v1_status, v1_raw = _post(_V1_GENERATE, _escaped({"agent_id": f"{SENTINEL} \ud800", "user_content": "x"}))
    vnext_status, vnext_raw = _post(
        _INGEST,
        _escaped({"agent_id": f"{SENTINEL} \ud800", "title": "t", "content": "c"}),
    )

    _assert_refused_without_echo(v1_status, v1_raw, expected=_error("agent_id"))
    _assert_refused_without_echo(vnext_status, vnext_raw, expected=_error("agent_id"))
    assert auth_payloads == []
    assert reached == []


def test_the_guard_is_the_innermost_layer() -> None:
    """The order of the middleware stack, outermost first.

    Identity, security headers, the ``/v1`` key check and the vNext check all
    come before the guard, so it reads a body only for a request they let
    through. Mutations: register the guard after the vNext layer, or before
    ``enforce_v1_agent_authentication``.
    """

    order = [middleware.kwargs["dispatch"].__name__ for middleware in main_module.app.user_middleware]

    assert order == [
        "enforce_authenticated_user_identity",
        "apply_http_security_posture",
        "enforce_v1_agent_authentication",
        "_vnext_protected_http_auth",
        "reject_lone_surrogate_json_body",
    ]


def test_a_request_that_is_refused_before_its_body_is_needed_has_its_body_left_unread(
    monkeypatch: pytest.MonkeyPatch, reached: list[str]
) -> None:
    """The guard adds no body read before authentication.

    A keyless ``/v1`` request from outside loopback gets its 401, and a ``/v0``
    request the server does not serve in production gets its 404, with the body
    never read, as in v0.19.2. Mutation: register the guard outside
    ``enforce_v1_agent_authentication``, or outside
    ``enforce_authenticated_user_identity``. The first case then reads the body
    and the count is one.
    """

    body = _escaped({"user_id": USER_ID, "user_content": "x"})
    json_headers = [(b"content-type", b"application/json"), (b"x-alicebot-user-id", USER_ID.encode())]

    status, _headers, raw, reads = _asgi_exchange(
        main_module.app, "POST", _V1_GENERATE, body, headers=json_headers, client=("203.0.113.9", 50000)
    )
    assert status == 401
    assert json.loads(raw)["detail"]["code"] == "authentication_failed"
    assert reads == 0

    production = Settings(
        app_env="production", auth_user_id="", legacy_v0_enabled_outside_dev=False, database_url="postgresql://db"
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: production)
    status, _headers, raw, reads = _asgi_exchange(main_module.app, "POST", _CANDIDATES, body, headers=json_headers)
    assert status == 404
    assert reads == 0
    assert reached == []


def test_a_refused_body_leaves_with_the_security_headers(reached: list[str]) -> None:
    """The 422 passes back out through the security-posture middleware.

    Mutation: register the guard outside ``apply_http_security_posture``. The
    headers are missing.
    """

    body = _escaped({"user_id": USER_ID, "raw_content": "\ud800"})
    headers = [(b"content-type", b"application/json"), (b"x-alicebot-user-id", USER_ID.encode())]
    status, response_headers, _raw, _reads = _asgi_exchange(main_module.app, "POST", _CAPTURES, body, headers=headers)

    assert status == 422
    assert response_headers["x-content-type-options"] == "nosniff"
    assert reached == []


@pytest.mark.parametrize(
    ("path", "payload", "expected_reached"),
    (
        (_CANDIDATES, {"user_content": "a pair \U0001f600 is one emoji"}, "continuity"),
        (_CANDIDATES, {"user_content": "a literal backslash text \\ud800 is not a surrogate"}, "continuity"),
        (_CAPTURES, {"raw_content": "caf\u00e9 \u65e5\u672c\u8a9e \ud55c"}, "continuity"),
        (_COMMIT, {"candidates": [{"title": "pair \U0001f600", "k\U0001f600": 1}]}, "continuity"),
        (_INGEST, {"agent_id": "a", "title": "t", "content": "c", "source_refs": ["\U0001f600"]}, "vnext_memories"),
    ),
)
def test_text_that_is_not_a_lone_surrogate_still_reaches_its_handler(
    reached: list[str], path: str, payload: dict[str, object], expected_reached: str
) -> None:
    """A valid pair written as two escapes, an escaped backslash and non-ASCII text pass.

    The decoder joins ``\\ud83d\\ude00`` into one emoji, so only a surrogate left
    over after decoding is refused. Mutation: refuse any ``\\ud8`` escape in the
    body, or any non-ASCII byte. The handler is no longer reached.
    """

    body = _escaped({"user_id": USER_ID, **payload})
    assert (b"\\ud83d" in body) or (b"\\\\ud800" in body) or (b"\\u65e5" in body)
    status, _raw_response = _post(path, body)

    assert status is None
    assert reached == [expected_reached]


def test_a_body_that_is_not_json_or_has_another_content_type_is_left_alone(reached: list[str]) -> None:
    """The guard reads only a body FastAPI decodes as JSON.

    A ``text/plain`` body is not decoded as JSON, even when its text is, so the
    framework refuses it as a non-object and the guard does not. A body that is
    not JSON is the framework's to refuse. Mutation: scan every body whatever
    its content type. The ``text/plain`` body then answers ``string_unicode``.
    """

    status, raw = _post(_CAPTURES, _escaped({"user_id": USER_ID, "raw_content": "\ud800"}), content_type="text/plain")
    assert status == 422
    assert json.loads(raw)["detail"][0]["type"] == "model_attributes_type"

    status, raw = _post(_CAPTURES, b'{"raw_content": "\\ud800"', content_type="application/json")
    assert status == 422
    assert json.loads(raw)["detail"][0]["type"] == "json_invalid"
    assert reached == []


def test_a_body_without_a_content_type_is_decoded_as_json_so_it_is_scanned(reached: list[str]) -> None:
    """FastAPI decodes a body with no content type as JSON, so the guard does too.

    Mutation: let ``declares_json_body`` answer False for an empty content type.
    The body then reaches the route's validation.
    """

    status, raw = _post(_CAPTURES, _escaped({"user_id": USER_ID, "raw_content": "\ud800"}), content_type=None)

    assert status == 422
    assert json.loads(raw) == _error("raw_content")
    assert reached == []


def test_an_ordinary_validation_error_keeps_its_input_and_its_shape(reached: list[str]) -> None:
    """Only an error that carries a surrogate is cut. Every other error is the framework's.

    Mutation: make ``withhold_lone_surrogate_errors`` drop ``input`` from every
    error, or have the handler build the detail itself. ``input`` goes missing.
    """

    status, raw = _post(_CANDIDATES, _escaped({"user_id": USER_ID, "user_content": "x" * 4001}))

    detail = json.loads(raw)["detail"]
    assert status == 422
    assert [item["type"] for item in detail] == ["string_too_long"]
    assert detail[0]["loc"] == ["body", "user_content"]
    assert detail[0]["input"] == "x" * 4001
    assert reached == []


# The handler layer, without the guard in front of it.


def _handler_response(exc: RequestValidationError, *, path: str) -> tuple[int, dict[str, Any]]:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    response = anyio.run(main_module._alice_request_validation_error, Request(scope), exc)
    body = bytes(response.body)
    body.decode("utf-8")
    return response.status_code, json.loads(body)


def _validation_errors(model: type, payload: dict[str, object]) -> list[Any]:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return list(caught.value.errors())


@pytest.mark.parametrize(
    "payload",
    (
        {"user_id": USER_ID, "user_content": f"{SENTINEL} \ud800"},
        {"user_id": f"{SENTINEL} \ud800"},
        {"user_id": USER_ID, "session_id": [f"{SENTINEL} \ud800"]},
    ),
    ids=("str-field", "uuid-field", "wrong-type"),
)
def test_the_handler_answers_422_for_a_real_error_that_holds_a_surrogate(payload: dict[str, object]) -> None:
    """The v0.19.2 failure: the handler raised ``UnicodeEncodeError`` rendering ``input``.

    The errors are the ones pydantic raises for the real candidates request
    model. Mutation: return ``request_validation_exception_handler`` (the
    framework's) from ``_alice_request_validation_error`` for the non-clipper
    path. The call raises ``UnicodeEncodeError``.
    """

    errors = _validation_errors(continuity_router.ContinuityCaptureCandidatesRequest, payload)
    assert any(0xD800 <= ord(char) <= 0xDFFF for char in json.dumps(errors, default=str, ensure_ascii=False))

    status, body = _handler_response(RequestValidationError(errors), path=_CANDIDATES)

    assert status == 422
    text = json.dumps(body, ensure_ascii=False)
    assert SENTINEL not in text
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in text)
    assert all(set(item) == {"type", "loc", "msg"} for item in body["detail"])


def test_the_handler_keeps_the_error_types_and_locations_it_can() -> None:
    """The type and the location stay. Only text that held a surrogate is replaced.

    A key that holds one becomes ``[key]`` in the location. Mutation: drop
    ``loc`` from a withheld error, or replace its ``type`` with a constant.
    """

    errors = [
        {
            "type": "extra_forbidden",
            "loc": ("candidates", 0, f"{SENTINEL}\ud800"),
            "msg": "Extra inputs are not permitted",
            "input": f"{SENTINEL} \ud800",
        },
        {"type": "list_type", "loc": ("candidates",), "msg": "Input should be a valid list", "input": "\ud800"},
    ]

    status, body = _handler_response(RequestValidationError(errors), path=_COMMIT)

    assert status == 422
    assert body["detail"] == [
        {"type": "extra_forbidden", "loc": ["candidates", 0, "[key]"], "msg": "Extra inputs are not permitted"},
        {"type": "list_type", "loc": ["candidates"], "msg": "Input should be a valid list"},
    ]


def test_the_handler_replaces_a_message_and_a_context_that_quote_the_text() -> None:
    """A validator can put the offending text in ``msg`` or ``ctx``. Both are dropped.

    Mutation: check only ``input`` for a surrogate. This error has none there,
    so it reaches the encoder and the call raises.
    """

    errors = [
        {
            "type": "value_error",
            "loc": ("mode",),
            "msg": f"Value error, bad character in {SENTINEL} \ud800",
            "input": "fine",
            "ctx": {"error": f"{SENTINEL} \ud800"},
        }
    ]

    status, body = _handler_response(RequestValidationError(errors), path=_COMMIT)

    assert status == 422
    assert body["detail"] == [{"type": "value_error", "loc": ["mode"], "msg": SURROGATE_MESSAGE}]


def test_the_browser_clip_capture_route_still_answers_its_fixed_error() -> None:
    """The route that keeps capability tokens out of validation bodies is unchanged.

    Mutation: remove the clipper branch from ``_alice_request_validation_error``.
    The token comes back in ``input``.
    """

    token = f"alice_clip_{'S' * 43}"
    errors = [{"type": "string_too_long", "loc": ("capture_capability",), "msg": "too long", "input": token}]

    status, body = _handler_response(
        RequestValidationError(errors), path="/v0/vnext/connectors/browser-clipper/capture"
    )

    assert status == 422
    assert body == {"detail": [{"type": "value_error", "loc": ["body"], "msg": "Input validation failed"}]}


def test_withholding_leaves_an_error_with_no_surrogate_as_the_same_object() -> None:
    """An error with no surrogate in it is not copied or reshaped.

    Mutation: rebuild every error as ``type``, ``loc`` and ``msg``.
    """

    plain = {"type": "missing", "loc": ["body", "x"], "msg": "Field required", "input": {"a": 1}}
    carrying = {"type": "missing", "loc": ["body", "x"], "msg": "m", "input": "\ud800"}

    cleaned = withhold_lone_surrogate_errors([plain, carrying])

    assert cleaned[0] is plain
    assert cleaned[1] == {"type": "missing", "loc": ["body", "x"], "msg": "m"}


# The finder, and the body decoder in front of it.


@pytest.mark.parametrize(
    ("value", "location"),
    (
        ("fine", None),
        ("\ud800", ()),
        ({"a": "fine", "b": 3, "c": None, "d": [1.5, True]}, None),
        ({"a": "\ud800"}, ("a",)),
        ({"a\udc00": 1}, ("[key]",)),
        ({"a": {"b": [1, {"c": ["x", "y\udbff"]}]}}, ("a", "b", 1, "c", 1)),
        ({"outer": {"in\ud800ner": "v"}}, ("outer", "[key]")),
        (["ok", ["ok", "\udfff"]], (1, 1)),
        ([[], {}, [[]], {"a": []}], None),
        ({"first": "ok", "second": {"third": "\ud800"}, "fourth": "\ud800"}, ("second", "third")),
        ({"a": [1, 2], "b": "\ud800"}, ("b",)),
        ({"a": {"x": {"y": [3]}, "z": 1}, "b": {"c": "\ud800"}}, ("b", "c")),
        ([["ok", {"k": "ok"}], "\ud800"], (1,)),
    ),
)
def test_find_lone_surrogate_names_the_position_without_the_text(value: object, location: tuple[Any, ...] | None) -> None:
    """The location of the first surrogate, with ``[key]`` for a key that holds one.

    Mutations: skip the key check; skip lists; put the key itself, not
    ``[key]``, in the location (it would echo the key); stop descending after
    the first level; leave the path alone when a nested container ends (the
    cases that find a surrogate in a sibling after a container was walked then
    report it under that container).
    """

    assert find_lone_surrogate(value) == location


def test_find_lone_surrogate_survives_nesting_the_interpreter_cannot_recurse_into() -> None:
    """The walk keeps its own stack. Mutation: make it recursive. 5,000 levels raises RecursionError."""

    value: object = "\ud800"
    for _ in range(5000):
        value = [value]

    location = find_lone_surrogate(value)

    assert location is not None
    assert len(location) == 5000


@pytest.mark.parametrize(
    ("body", "found"),
    (
        (b'{"a": "\\ud800"}', True),
        (b'{"a": "\\uD800"}', True),
        (b'{"a": "\\udfff"}', True),
        (b'{"a": "x\\ud83dy"}', True),
        (b'{"a": "\\ud83d\\u0041"}', True),
        (b'{"a": "\\ude00\\ud83d"}', True),
        (b'{"a": "\\ud83d\\ude00"}', False),
        (b'{"a": "\\\\ud800"}', False),
        (b'{"a": "\\u00e9 \\u65e5 \\ud55c"}', False),
        (b'{"a": "plain"}', False),
        (b'{"a": "\xed\xa0\x80"}', True),
        (b'{"a": "\xed\xbf\xbf"}', True),
        (b'{"a": "\xed\x9f\xbf"}', False),
        ('{"a": "\\ud800"}'.encode("utf-16"), True),
        ('{"a": "\\ud800"}'.encode("utf-32-le"), True),
        (b"", False),
        (b"not json \\ud800", False),
        (b'{"a": "\\ud800"', False),
        (b'"\\ud800"', True),
    ),
)
def test_body_lone_surrogate_location_reads_every_form_a_surrogate_can_take(body: bytes, found: bool) -> None:
    """Escapes in either case, raw UTF-8 bytes, UTF-16 and UTF-32 bodies, and the forms that are not one.

    The decoder is ``json.loads`` on the bytes, the one ``Request.json()`` calls.
    A valid pair of escapes is one character, a doubled backslash is text, and
    ED 9F BF is U+D7FF, which is a character. Mutations: narrow the byte
    prefilter to lowercase ``\\ud``, drop its ``\\xed`` branch, or drop its NUL
    branch. The matching cases stop finding.
    """

    assert (body_lone_surrogate_location(body) is not None) is found


def test_a_body_too_deep_for_the_decoder_is_left_to_the_framework() -> None:
    """``json.loads`` raises ``RecursionError`` at about a thousand levels. The guard answers None.

    Mutation: let the exception out of ``body_lone_surrogate_location``.
    """

    body = b"[" * 100_000 + b'"\\ud800"' + b"]" * 100_000

    assert body_lone_surrogate_location(body) is None


@pytest.mark.parametrize(
    ("content_type", "expected"),
    (
        ("", True),
        ("application/json", True),
        ("Application/JSON; charset=utf-8", True),
        ("application/vnd.api+json", True),
        ("text/plain", False),
        ("text/plain;charset=UTF-8", False),
        ("application/x-www-form-urlencoded", False),
        ("multipart/form-data; boundary=x", False),
        ("application/jsonx", False),
    ),
)
def test_declares_json_body_follows_the_rule_fastapi_decodes_by(content_type: str, expected: bool) -> None:
    """No content type, ``application/json`` and ``application/*+json`` are JSON.

    Mutation: test the content type with ``in`` (``application/jsonx`` then
    passes) or drop the ``+json`` branch.
    """

    assert declares_json_body(content_type) is expected


# The guard on its own, so a pydantic-checked field cannot hide a missing layer.


def _guard_only_app(seen: list[bytes]) -> Starlette:
    async def echo(request: Request) -> Response:
        body = await request.body()
        seen.append(body)
        # json.dumps writes ASCII, so a body that holds a surrogate can still be answered.
        return Response(
            json.dumps({"length": len(body), "json": json.loads(body) if body else None}),
            media_type="application/json",
        )

    return Starlette(
        routes=[Route("/echo", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=reject_lone_surrogate_json_body)],
    )


@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH", "DELETE"))
def test_the_guard_refuses_a_surrogate_for_every_method_that_carries_a_body(method: str) -> None:
    """POST, PUT, PATCH and DELETE bodies are scanned. Mutation: scan POST only."""

    seen: list[bytes] = []
    status, raw = _asgi_call(
        _guard_only_app(seen),
        method,
        "/echo",
        _escaped({"a": [{"b": f"{SENTINEL} \ud800"}]}),
        headers=[(b"content-type", b"application/json")],
    )

    _assert_refused_without_echo(status, raw, expected=_error("a", 0, "b"))
    assert seen == []


def test_the_guard_does_not_read_a_get_and_passes_a_clean_body_through_whole() -> None:
    """A clean body reaches the route byte for byte, after the guard has read it.

    Mutation: return ``call_next(request)`` after ``await request.body()``
    without the replay Starlette gives it, or scan a GET. The route then sees
    an empty body, or a GET with a surrogate in its body is refused.
    """

    seen: list[bytes] = []
    app = _guard_only_app(seen)
    clean = _escaped({"a": "caf\u00e9 \U0001f600", "pair": "\U0001f600"})

    status, raw = _asgi_call(app, "POST", "/echo", clean, headers=[(b"content-type", b"application/json")])
    assert status == 200
    assert seen == [clean]
    assert json.loads(raw)["json"]["pair"] == "\U0001f600"

    status, raw = _asgi_call(
        app, "GET", "/echo", _escaped({"a": "\ud800"}), headers=[(b"content-type", b"application/json")]
    )
    assert status == 200


def test_the_guard_leaves_a_body_it_cannot_decode_to_the_route() -> None:
    """Invalid JSON, and bytes that are not UTF-8, are not the guard's to refuse.

    Mutation: answer 422 for a body that fails to decode.
    """

    seen: list[bytes] = []
    app = _guard_only_app(seen)
    for body in (b'{"a": "\\ud800"', b"\xff\xfe\\ud800 not json"):
        seen.clear()
        try:
            status, _raw_response = _asgi_call(app, "POST", "/echo", body, headers=[(b"content-type", b"application/json")])
        except Exception:
            status = 500  # the echo route itself cannot parse it
        assert status != 422
        assert seen == [body]
