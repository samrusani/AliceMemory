"""A request body that is not UTF-8 is a typed 4xx, never a 500.

The framework hands a route the raw bytes of a body when the request has no
content type, or one that is not JSON (``text/plain``, ``application/octet-stream``).
Pydantic refuses bytes where a model is expected and puts them in the ``input``
of the error it raises. The framework's handler then encodes that error with
``jsonable_encoder``, which decodes bytes as UTF-8, and a body that is not UTF-8
(UTF-16 or UTF-32 JSON, text in another charset, arbitrary bytes) raises
``UnicodeDecodeError``. v0.19.2 caught only ``UnicodeEncodeError``, so the answer
was HTTP 500, on every route that takes a body. The tests drive the real app in
process over raw ASGI, so the bytes of the body are exactly what the test wrote.

Mutations that restore v0.19.2 as a whole: copy the saved v0.19.2
``lone_surrogates.py`` back over the changed one. Each test names the
single-point mutation that fails it.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.lone_surrogates import SURROGATE_MESSAGE, render_validation_error
from tests.unit.test_lone_surrogate_request_body import _asgi_exchange
from tests.unit.test_vnext_main import FakeVNextStore

ROOT = Path(__file__).resolve().parents[2]
USER_ID = str(uuid4())
# Text inside the body of every case. It must never come back in a response.
SENTINEL = "SENTINEL-6602"


class _HandlerReached(Exception):
    """A route handler opened its store connection."""


@pytest.fixture()
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real app with every route's store connection replaced by a tripwire.

    A route handler that runs past validation raises ``_HandlerReached``. The
    agent-key layers in ``main.py`` get a connection that yields nothing, so a
    request with no key resolves no identity.
    """

    settings = Settings(app_env="development", auth_user_id="", database_url="postgresql://db")

    def tripwire(*args: object, **kwargs: object) -> None:
        raise _HandlerReached

    @contextmanager
    def auth_connection(*args: object, **kwargs: object) -> Iterator[object]:
        yield object()

    for name, module in list(sys.modules.items()):
        if not name.startswith("alicebot_api.") or module is None:
            continue
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
        if module is not main_module and hasattr(module, "user_connection"):
            monkeypatch.setattr(module, "user_connection", tripwire)
    monkeypatch.setattr(main_module, "user_connection", auth_connection)
    monkeypatch.setattr(main_module, "PostgresVNextStore", lambda _conn: FakeVNextStore(None))


def _exchange(
    method: str,
    path: str,
    body: bytes,
    *,
    content_type: str | None,
) -> tuple[int | None, bytes]:
    """One request to the real app. The status is None when a handler was reached.

    An exception that leaves the app is what the server turns into HTTP 500, so
    it is reported as 500.
    """

    headers = [(b"host", b"127.0.0.1:8000"), (b"x-alicebot-user-id", USER_ID.encode())]
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    try:
        status, _headers, raw, _reads = _asgi_exchange(main_module.app, method, path, body, headers=headers)
    except _HandlerReached:
        return None, b""
    except Exception as exc:  # noqa: BLE001 - the server answers any escaped exception with 500
        return 500, type(exc).__name__.encode()
    return status, raw


def _document() -> str:
    return json.dumps({"title": f"{SENTINEL} \u00e9", "note": "\u00e9", "canonical_text": "x"}, ensure_ascii=False)


# Every body here is a request a client could send, and none is valid UTF-8.
_UNDECODABLE: dict[str, bytes] = {
    "utf16-with-bom": _document().encode("utf-16"),
    "utf32-with-bom": _document().encode("utf-32"),
    "utf16-le-no-bom": _document().encode("utf-16-le"),
    "utf16-be-no-bom": _document().encode("utf-16-be"),
    "latin1": _document().encode("latin-1"),
    "high-bytes": bytes(range(128, 256)) + SENTINEL.encode(),
    "ff-fe-00": b"\xff\xfe\x00",
    "truncated-sequence": SENTINEL.encode() + b"\xe2\x82",
    "utf8-surrogate-bytes": b"\xed\xa0\x80 " + SENTINEL.encode(),
}

_NON_JSON_CONTENT_TYPES: tuple[str | None, ...] = (
    None,
    "text/plain",
    "application/octet-stream",
    "text/plain; charset=utf-16",
)

# Seven routes over the legacy, ``/v1`` and vNext surfaces, three methods.
_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/memory/operations/commit"),
    ("POST", "/v1/memory/operations/candidates/generate"),
    ("POST", "/v0/threads"),
    ("POST", "/v0/vnext/memories/commit"),
    ("POST", "/v0/continuity/captures/commit"),
    ("PUT", "/v0/vnext/settings/brain-charter"),
    ("PATCH", f"/v1/providers/{uuid4()}"),
)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _echoes(raw: bytes) -> list[str]:
    """The ways the sentinel could be written back from the body, in each encoding the table uses.

    The response is decoded first, so a NUL between the letters of UTF-16 text
    is a character and not the escape ``\\u0000``.
    """

    texts = list(_strings(json.loads(raw)))
    variants = {
        SENTINEL.encode(encoding).decode("latin-1")
        for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "latin-1")
    }
    return [variant for variant in variants if any(variant in text for text in texts)]


def _assert_typed_422_without_echo(status: int | None, raw: bytes) -> None:
    assert status == 422, raw
    detail = json.loads(raw)["detail"]
    assert isinstance(detail, list) and detail
    for error in detail:
        assert set(error) == {"type", "loc", "msg"}, error
        assert error["loc"][0] == "body"
        assert isinstance(error["type"], str) and isinstance(error["msg"], str)
    assert _echoes(raw) == []


@pytest.mark.parametrize("body_name", sorted(_UNDECODABLE))
def test_every_body_in_the_table_is_not_utf8(body_name: str) -> None:
    """The cases below mean what they say.

    Mutation: replace one body with text that is valid UTF-8 (the UTF-8 encoding
    of ``_document()``). Its decode succeeds and this test fails.
    """

    with pytest.raises(UnicodeDecodeError):
        _UNDECODABLE[body_name].decode("utf-8")


@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES, ids=lambda value: str(value))
@pytest.mark.parametrize("body_name", sorted(_UNDECODABLE))
@pytest.mark.parametrize("route", _ROUTES, ids=lambda route: f"{route[0]} {route[1][:30]}")
def test_a_body_that_is_not_utf8_gets_a_422_that_echoes_nothing(
    no_database: None,
    route: tuple[str, str],
    body_name: str,
    content_type: str | None,
) -> None:
    """The error names its type, its location and its message, and no part of the body.

    Seven routes, nine bodies and four content types, none of them JSON: the
    framework passes the bytes to pydantic, which refuses them, and the handler
    answers. In v0.19.2 each of these was HTTP 500.

    Mutations, each one alone. Catch only ``UnicodeEncodeError`` in
    ``render_validation_error`` (v0.19.2): every case answers 500. In
    ``_encode_error``, return ``{**_cut_error(error), "input": error["input"].decode("utf-8", "replace")}``
    for the undecodable error: the key check fails for every case. Return
    ``{**_cut_error(error), "msg": error["input"].decode("latin-1")}`` instead: the
    keys are right and the echo check fails for every body that holds the
    sentinel, which is every body but ``ff-fe-00``.
    """

    method, path = route
    status, raw = _exchange(method, path, _UNDECODABLE[body_name], content_type=content_type)

    _assert_typed_422_without_echo(status, raw)


@pytest.mark.parametrize("body_name", sorted(_UNDECODABLE))
@pytest.mark.parametrize("route", _ROUTES, ids=lambda route: f"{route[0]} {route[1][:30]}")
def test_a_body_that_is_not_utf8_with_a_json_content_type_is_a_4xx_and_not_a_500(
    no_database: None,
    route: tuple[str, str],
    body_name: str,
) -> None:
    """The JSON path was never the failure: it answers 400 or 422, or the route takes the body.

    The decoder reads UTF-16 and UTF-32 JSON by itself, so a valid document in
    either reaches validation, and a route that accepts it runs its handler (the
    tripwire). A body the decoder cannot read is a 400 from the framework. What
    must never happen is a 500. Mutation: add ``raw_body.decode("utf-8")`` to
    ``reject_lone_surrogate_json_body`` after it reads the body, the other way to
    handle a non-UTF-8 body. Every case here answers 500.
    """

    method, path = route
    status, raw = _exchange(method, path, _UNDECODABLE[body_name], content_type="application/json")

    assert status is None or 400 <= status < 500, raw
    if status == 400:
        assert _echoes(raw) == []


@pytest.mark.parametrize(
    "body",
    (
        b'{"title": "x"}',
        b'{"title": "x"}'.decode("ascii").encode("utf-16-le"),
        b'{"title": "x"}'.decode("ascii").encode("utf-16-be"),
        "{\"title\": \"caf\u00e9\"}".encode("utf-8"),
    ),
    ids=("ascii-json", "utf16-le-ascii-only", "utf16-be-ascii-only", "utf8-json"),
)
def test_a_body_that_is_utf8_keeps_the_framework_answer_and_its_input(no_database: None, body: bytes) -> None:
    """Only an error whose input cannot be decoded is cut. A decodable raw body is answered as in v0.19.2.

    The input is the text of the body, as the framework wrote it. UTF-16 text
    that is all ASCII is valid UTF-8 with NUL characters in it. Mutation: in
    ``render_validation_error``, answer with
    ``JSONResponse(status_code=422, content={"detail": [_cut_error(error) for error in exc.errors()]})``
    and never ask the framework. ``input`` goes missing.
    """

    status, raw = _exchange("POST", "/v0/vnext/memories/commit", body, content_type="text/plain")

    assert status == 422, raw
    detail = json.loads(raw)["detail"]
    assert [error["type"] for error in detail] == ["model_attributes_type"]
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["input"] == body.decode("utf-8")


def _bare_request() -> Request:
    path = "/v0/vnext/memories/commit"
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "root_path": "",
            "app": main_module.app,
        }
    )


def test_one_error_with_bytes_does_not_cost_the_other_errors_their_input() -> None:
    """The renderer cuts each error that cannot be encoded and keeps the rest whole.

    Four errors in one response: bytes that are not UTF-8, bytes nested in a
    dict, a lone surrogate, and an ordinary error. The first three are cut to
    ``type``, ``loc`` and ``msg``, and the ordinary one keeps its ``input``.
    Mutations, each one alone. Make ``validation_error_response`` call
    ``jsonable_encoder(errors)`` on the whole list, as v0.19.2 did: one bad error
    raises for all four. Make ``_encode_error`` re-raise: the call raises. Make
    ``_encode_error`` return ``_cut_error(error)`` for every error: the ordinary
    error loses its ``input``.
    """

    errors = [
        {"type": "model_attributes_type", "loc": ("body",), "msg": "m1", "input": b"\xff\xfe\x00" + SENTINEL.encode()},
        {"type": "string_type", "loc": ("body", "a"), "msg": "m2", "input": {"k": b"\xc0\xaf", "t": SENTINEL}},
        {"type": "string_unicode", "loc": ("body", "b"), "msg": "m3", "input": f"{SENTINEL} \ud800"},
        {"type": "missing", "loc": ("body", "c"), "msg": "Field required", "input": {"a": 1}},
    ]

    response = anyio.run(render_validation_error, _bare_request(), RequestValidationError(errors))

    assert response.status_code == 422
    body = bytes(response.body)
    assert SENTINEL.encode() not in body
    assert json.loads(body) == {
        "detail": [
            {"type": "model_attributes_type", "loc": ["body"], "msg": "m1"},
            {"type": "string_type", "loc": ["body", "a"], "msg": "m2"},
            {"type": "string_unicode", "loc": ["body", "b"], "msg": "m3"},
            {"type": "missing", "loc": ["body", "c"], "msg": "Field required", "input": {"a": 1}},
        ]
    }


def test_an_error_that_cannot_be_encoded_and_has_no_usable_fields_still_gets_a_422() -> None:
    """The cut never raises, whatever the error holds in ``type``, ``loc`` and ``msg``.

    Mutation: build the cut error with ``fields["type"]``, ``fields["loc"]`` and
    ``fields["msg"]``. The call raises ``KeyError`` for the bare error.
    """

    errors = [{"input": b"\xff"}, {"type": 7, "loc": "body", "msg": None, "input": b"\xff"}]

    response = anyio.run(render_validation_error, _bare_request(), RequestValidationError(errors))

    assert response.status_code == 422
    detail = json.loads(bytes(response.body))["detail"]
    assert [set(error) for error in detail] == [{"type", "loc", "msg"}] * 2
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["msg"] == SURROGATE_MESSAGE


@lru_cache(maxsize=1)
def _body_routes() -> tuple[tuple[str, str], ...]:
    """Every method and path in the OpenAPI schema that takes a request body, path parameters filled."""

    routes: list[tuple[str, str]] = []
    for path, item in main_module.app.openapi()["paths"].items():
        for method, operation in item.items():
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and "requestBody" in operation:
                routes.append((method.upper(), re.sub(r"\{[^}]+\}", str(uuid4()), path)))
    return tuple(routes)


@pytest.mark.parametrize("content_type", (None, "text/plain"), ids=("no-content-type", "text-plain"))
@pytest.mark.parametrize("body_name", ("ff-fe-00", "utf16-with-bom", "utf8-surrogate-bytes"))
def test_no_route_that_takes_a_body_answers_500_for_a_body_that_is_not_utf8(
    no_database: None, body_name: str, content_type: str | None
) -> None:
    """The fix is in the renderer, so it holds for every route, including one added later.

    The routes come from the OpenAPI schema. Each takes a body model, so bytes
    fail validation, and a route that took bytes as they are would reach its
    handler, which the tripwire reports as None. At least 60 routes must answer
    422 so a schema that lost its routes cannot pass this. Mutation: catch only
    ``UnicodeEncodeError`` in ``render_validation_error`` (v0.19.2). Most of the
    routes answer 500.
    """

    routes = _body_routes()
    assert len(routes) >= 60
    statuses = {route: _exchange(*route, _UNDECODABLE[body_name], content_type=content_type) for route in routes}

    server_errors = {route: status for route, (status, _raw) in statuses.items() if status is not None and status >= 500}
    assert server_errors == {}
    refused = [route for route, (status, _raw) in statuses.items() if status == 422]
    assert len(refused) >= 60
    for route in refused:
        _assert_typed_422_without_echo(*statuses[route])


# The docs.


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _unreleased_changelog() -> str:
    changelog = _read("CHANGELOG.md")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]


_CHANGELOG_START = "A request body that is not valid UTF-8"
_GUIDE_MARKER = "Unreleased (on main, not in v0.19.2): a request body that is not valid UTF-8"


def test_the_changelog_entry_sits_under_unreleased_and_states_v0192() -> None:
    """One Unreleased entry says what main answers and what v0.19.2 answered.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete
    the sentence that says v0.19.2 answered HTTP 500; delete the sentence that
    names the routes; delete the clipper exception; delete the sentence that
    says nothing from the body is echoed; delete the sentence about a JSON
    content type; write an em dash or an en dash into the entry.
    """

    entries = [item for item in _unreleased_changelog().split("\n- ")[1:] if item.startswith(_CHANGELOG_START)]
    assert len(entries) == 1
    entry = _flat(entries[0])
    assert (
        "A request body that is not valid UTF-8 and has no JSON content type, for example UTF-16 or UTF-32 JSON "
        "or arbitrary bytes sent with no `Content-Type` header or with `text/plain`, is now answered with HTTP 422"
    ) in entry
    assert "one validation error of type `model_attributes_type`, with `loc` `[\"body\"]`" in entry
    assert "Nothing from the body is echoed" in entry
    assert "on every route that takes a body, including `POST /v1/memory/operations/commit`, `POST /v0/threads`" in entry
    assert (
        "except the browser-clipper capture route, which keeps its own fixed `value_error` and answers 400 for a "
        "`text/plain` body it cannot parse, both as before."
    ) in entry
    assert "A body that is valid UTF-8 is answered as before, with the text of the body in `input`." in entry
    assert "With `Content-Type: application/json` the answer is unchanged: 422 for UTF-16 and UTF-32 JSON that" in entry
    assert "In v0.19.2 the same request answered HTTP 500." in entry
    assert "renders a validation error" in entry
    assert "\u2014" not in entry and "\u2013" not in entry

    released = _read("CHANGELOG.md")[_read("CHANGELOG.md").index("## v0.19.2") :]
    assert "is not valid UTF-8 and has no JSON content type" not in released


def test_the_agent_guide_marks_the_new_422_as_main_and_keeps_v0192() -> None:
    """The HTTP error section says what main answers and what v0.19.2 answers, marked.

    Mutations: delete the marker; delete the v0.19.2 sentence; state main's
    answer without the marker; put the paragraph after the Scopes heading;
    write an em dash or an en dash into it.
    """

    guide = _flat(_read("docs/alpha/agent-integration.md"))
    assert guide.count(_GUIDE_MARKER) == 1
    paragraph = guide[guide.index(_GUIDE_MARKER) :].split(" ## Scopes")[0]
    assert guide.index(_GUIDE_MARKER) < guide.index(" ## Scopes")
    assert (
        "is answered with HTTP 422 and the array `detail` of a validation error when the request has no JSON "
        "content type"
    ) in paragraph
    assert "The error gives its type, its location and its message, and none of the body." in paragraph
    assert "v0.19.2 answers HTTP 500 for such a body" in paragraph
    assert "\u2014" not in paragraph and "\u2013" not in paragraph
