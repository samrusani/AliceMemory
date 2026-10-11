"""A stored row too deep for a reader is read whole when it can be and cut when it cannot, and a call that wrote never runs twice.

``loads_stored`` is ``json.loads``. A reader that overflows on what it decoded fails the request or the tool call once, and
a call that only reads runs again with ``cutting_deep_json()`` set, in which every stored column is decoded by
``loads_bounded``. These tests hold the pieces: the decoder, the walk that finds a ``RecursionError`` in a chain of
errors, the retry, the ASGI layer for an HTTP request and the MCP server for a tool call.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``loads_stored`` stops falling back when ``json.loads`` overflows: ``test_the_stored_decoder_falls_back_when_the_decoder_overflows``.
* ``loads_stored`` ignores ``cutting_deep_json``: ``test_the_stored_decoder_cuts_on_the_second_attempt``.
* ``overflowed`` stops reading ``__cause__``, ``__context__`` or the members of a group: ``test_a_recursion_error_is_found_wherever_the_chain_holds_it``.
* ``retry_cutting_deep_json`` retries an error that is not a ``RecursionError``, or retries twice:
  ``test_the_retry_runs_a_call_that_overflowed_once_more_and_nothing_else``.
* the ASGI layer replays a request that wrote, or one whose response has started, or does not replay the body:
  ``test_a_get_that_overflowed_is_replayed_with_the_same_request`` and ``test_a_request_that_may_have_written_is_not_replayed``.
* ``READ_ONLY_POSTS`` names a path that is no route: ``test_the_read_only_posts_are_posts_of_the_application``.
* the MCP server runs a tool that writes a second time: ``test_a_tool_that_writes_is_not_run_twice``.
"""

from __future__ import annotations

import asyncio
import json
from io import BytesIO

import pytest

from alicebot_api import mcp_server
from alicebot_api.deep_row_retry import READ_ONLY_POSTS, RetryDeepRowMiddleware
from alicebot_api.json_bounds import (
    DEEP_PLACEHOLDER,
    MAX_READ_JSON_DEPTH,
    cutting_deep_json,
    loads_stored,
    overflowed,
    retry_cutting_deep_json,
)
from alicebot_api.mcp.definitions import READ_ONLY_TOOL_NAMES
from alicebot_api.mcp.types import MCPRuntimeContext


def _text(depth: int) -> str:
    return "[" * depth + '"x"' + "]" * depth


def _levels(value: object) -> tuple[int, object]:
    depth = 0
    while isinstance(value, list):
        depth += 1
        value = value[0]
    return depth, value


# The decoder.


def test_the_stored_decoder_is_json_loads_for_text_the_decoder_reads() -> None:
    for text in ('{"a": [1, 2, {"b": null}]}', _text(MAX_READ_JSON_DEPTH + 300), "NaN"):
        assert repr(loads_stored(text)) == repr(json.loads(text))
    assert loads_stored(b'{"a": 1}') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        loads_stored("{not json")


def test_the_stored_decoder_falls_back_when_the_decoder_overflows() -> None:
    """About 5000 levels is more than ``json.loads`` can decode; the row is still read, cut."""

    with pytest.raises(RecursionError):
        json.loads(_text(20_000))
    assert _levels(loads_stored(_text(20_000))) == (MAX_READ_JSON_DEPTH, DEEP_PLACEHOLDER)


def test_the_stored_decoder_cuts_on_the_second_attempt() -> None:
    text = _text(MAX_READ_JSON_DEPTH + 300)
    assert _levels(loads_stored(text)) == (MAX_READ_JSON_DEPTH + 300, "x")
    with cutting_deep_json():
        assert _levels(loads_stored(text)) == (MAX_READ_JSON_DEPTH, DEEP_PLACEHOLDER)
    assert _levels(loads_stored(text)) == (MAX_READ_JSON_DEPTH + 300, "x")  # the flag was reset


# The walk.


def _raised_from(inner: BaseException) -> Exception:
    """An error whose ``__cause__`` is ``inner`` and whose ``__context__`` is not set."""

    outer = ValueError("outer")
    outer.__cause__ = inner
    return outer


def _raised_while_handling(inner: BaseException) -> Exception:
    """An error whose ``__context__`` is ``inner`` and whose ``__cause__`` is not set."""

    outer = ValueError("outer")
    outer.__context__ = inner
    return outer


def test_a_recursion_error_is_found_wherever_the_chain_holds_it() -> None:
    assert overflowed(RecursionError("direct"))
    assert overflowed(_raised_from(RecursionError("cause")))
    assert overflowed(_raised_while_handling(RecursionError("context")))
    assert overflowed(ExceptionGroup("group", [ValueError("a"), _raised_from(RecursionError("in a group"))]))
    assert not overflowed(ValueError("plain"))
    assert not overflowed(_raised_from(KeyError("other")))
    assert not overflowed(ExceptionGroup("group", [ValueError("a"), KeyError("b")]))


def test_the_walk_ends_on_a_chain_that_loops_and_on_one_a_million_errors_long() -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__context__, second.__context__ = second, first
    assert not overflowed(first)
    tail: BaseException | None = None
    head: BaseException = ValueError("0")
    for index in range(100_000):
        head = ValueError(str(index))
        head.__cause__ = tail
        tail = head
    assert not overflowed(head)


# The retry.


def test_the_retry_runs_a_call_that_overflowed_once_more_and_nothing_else() -> None:
    seen: list[bool] = []

    def overflows_once() -> str:
        with_cut = _levels(loads_stored(_text(MAX_READ_JSON_DEPTH + 10)))[1] == DEEP_PLACEHOLDER
        seen.append(with_cut)
        if not with_cut:
            raise RecursionError("a reader")
        return "ok"

    assert retry_cutting_deep_json(overflows_once) == "ok"
    assert seen == [False, True]

    calls: list[int] = []

    def always_overflows() -> None:
        calls.append(1)
        raise RecursionError("still")

    with pytest.raises(RecursionError):
        retry_cutting_deep_json(always_overflows)
    assert len(calls) == 2

    other: list[int] = []

    def refuses() -> None:
        other.append(1)
        raise ValueError("not a recursion error")

    with pytest.raises(ValueError):
        retry_cutting_deep_json(refuses)
    assert len(other) == 1

    inner: list[int] = []

    def nested() -> None:
        inner.append(1)
        retry_cutting_deep_json(always_overflows)

    with pytest.raises(RecursionError):
        retry_cutting_deep_json(nested)
    # The first attempt of ``nested`` retried inside (two calls); its second attempt ran with the cut on and did not (one).
    assert len(inner) == 2 and len(calls) == 2 + 2 + 1


# The ASGI layer.


class _App:
    """An ASGI application that reads its whole body, then fails with ``RecursionError`` unless the cut is on."""

    def __init__(self, *, fail_after_start: bool = False, always_fail: bool = False) -> None:
        self.attempts: list[dict] = []
        self.fail_after_start = fail_after_start
        self.always_fail = always_fail

    async def __call__(self, scope, receive, send) -> None:
        entered = dict(scope)
        # Starlette puts its own keys into the scope as the request goes in; the replay must not see them.
        scope["state"] = {"first_attempt": True}
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        cut = _levels(loads_stored(_text(MAX_READ_JSON_DEPTH + 10)))[1] == DEEP_PLACEHOLDER
        self.attempts.append({"scope": entered, "body": body, "cut": cut})
        if self.fail_after_start:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        if self.always_fail or not cut:
            raise RecursionError("a reader")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(app, *, method: str, path: str, body: bytes = b""):
    sent: list[dict] = []
    queue = [{"type": "http.request", "body": body[:3], "more_body": True}, {"type": "http.request", "body": body[3:], "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b"a=1"}
    error: BaseException | None = None
    try:
        asyncio.run(RetryDeepRowMiddleware(app)(scope, receive, send))
    except BaseException as exc:  # noqa: BLE001 - the test reads what came out
        error = exc
    return sent, error


@pytest.mark.parametrize(("method", "path"), [("GET", "/v0/vnext/workspace"), ("HEAD", "/v0/vnext/workspace"), ("POST", "/v0/vnext/context-packs")])
def test_a_get_that_overflowed_is_replayed_with_the_same_request(method: str, path: str) -> None:
    app = _App()
    sent, error = _drive(app, method=method, path=path, body=b"abcdefghij")
    assert error is None and [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert [attempt["cut"] for attempt in app.attempts] == [False, True]
    assert app.attempts[0]["body"] == app.attempts[1]["body"] == b"abcdefghij"
    assert app.attempts[0]["scope"] == app.attempts[1]["scope"]
    assert "state" not in app.attempts[1]["scope"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v0/vnext/memories/commit"),
        ("POST", "/v0/vnext/memories/forget"),
        ("PUT", "/v0/vnext/settings/brain-charter"),
        ("DELETE", "/v0/vnext/sources/abc"),
        ("PATCH", "/v0/vnext/connectors/x/config"),
        ("POST", "/v0/vnext/context-packs/extra"),
    ],
)
def test_a_request_that_may_have_written_is_not_replayed(method: str, path: str) -> None:
    app = _App()
    _sent, error = _drive(app, method=method, path=path, body=b"abcdefghij")
    assert isinstance(error, RecursionError) and len(app.attempts) == 1


def test_a_response_that_has_started_is_not_replayed_and_a_second_overflow_is_raised() -> None:
    started = _App(fail_after_start=True)
    _sent, error = _drive(started, method="GET", path="/v0/vnext/workspace")
    assert isinstance(error, RecursionError) and len(started.attempts) == 1
    again = _App(always_fail=True)
    _sent, error = _drive(again, method="GET", path="/v0/vnext/workspace")
    assert isinstance(error, RecursionError) and len(again.attempts) == 2


def test_a_failure_that_is_not_an_overflow_is_not_replayed() -> None:
    class Refuses(_App):
        async def __call__(self, scope, receive, send) -> None:
            self.attempts.append({})
            raise ValueError("a bug")

    app = Refuses()
    _sent, error = _drive(app, method="GET", path="/v0/vnext/workspace")
    assert isinstance(error, ValueError) and len(app.attempts) == 1


def test_the_read_only_posts_are_posts_of_the_application() -> None:
    from alicebot_api.main import app

    paths = app.openapi()["paths"]
    for path in READ_ONLY_POSTS:
        assert "post" in paths[path], path


def test_the_layer_is_registered_in_the_application() -> None:
    from alicebot_api.main import app

    assert RetryDeepRowMiddleware in [layer.cls for layer in app.user_middleware]


# The MCP server.


class _Server:
    def __init__(self, monkeypatch, tool: str, *, fail_when_whole: bool = True) -> None:
        self.calls: list[bool] = []

        def call(_context, *, name, arguments):
            cut = _levels(loads_stored(_text(MAX_READ_JSON_DEPTH + 10)))[1] == DEEP_PLACEHOLDER
            self.calls.append(cut)
            if fail_when_whole and not cut:
                raise RecursionError("a reader")
            return {"tool": name, "cut": cut}

        monkeypatch.setattr(mcp_server, "call_mcp_tool", call)
        self.server = mcp_server.MCPServer(context=MCPRuntimeContext(database_url="x", user_id=None), input_stream=BytesIO(), output_stream=BytesIO())  # type: ignore[arg-type]
        self.tool = tool

    def call(self) -> dict | None:
        return self.server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": self.tool, "arguments": {}}})


def test_a_tool_that_reads_is_run_again_with_the_cut(monkeypatch) -> None:
    server = _Server(monkeypatch, "alice_explain")
    answer = server.call()
    assert server.calls == [False, True]
    assert answer is not None and "result" in answer and '"cut":true' in answer["result"]["content"][0]["text"]


def test_a_tool_that_writes_is_not_run_twice(monkeypatch) -> None:
    for tool in ("alice_memory_commit", "alice_capture", "alice_memory_correct", "alice_memory_manage", "alice_open_loops"):
        assert tool not in READ_ONLY_TOOL_NAMES
        server = _Server(monkeypatch, tool)
        try:
            answer = server.call()
        except RecursionError:
            answer = None
        # Whatever the server answers, the tool ran once.
        assert server.calls == [False], (tool, answer)


def test_the_registry_runs_a_write_tool_once_and_a_read_tool_again(monkeypatch, tmp_path) -> None:
    """``call_mcp_tool`` is the boundary every caller crosses, the server or not."""

    from uuid import UUID

    from alicebot_api.mcp import registry
    from alicebot_api.onramp import sqlite_url_for_path
    from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault

    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(_vault(tmp_path)), user_id=UUID(USER_ID))
    calls: dict[str, list[bool]] = {"alice_memory_commit": [], "alice_recall": []}

    def handler_for(tool: str):
        def handler(_context, _arguments):
            cut = _levels(loads_stored(_text(MAX_READ_JSON_DEPTH + 10)))[1] == DEEP_PLACEHOLDER
            calls[tool].append(cut)
            if not cut:
                raise RecursionError("a writer")
            return {"ok": True}

        return handler

    for tool in calls:
        monkeypatch.setitem(registry._TOOL_HANDLERS, tool, handler_for(tool))
    with pytest.raises(RecursionError):
        registry.call_mcp_tool(context, name="alice_memory_commit", arguments={"title": "t", "canonical_text": "c"})
    assert calls["alice_memory_commit"] == [False]
    assert registry.call_mcp_tool(context, name="alice_recall", arguments={"query": "q"}) == {"ok": True}
    assert calls["alice_recall"] == [False, True]


def test_the_tools_that_read_are_the_tools_hinted_as_read_only() -> None:
    from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

    hinted = {
        tool["name"]
        for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS)
        if tool["annotations"].get("readOnlyHint")  # type: ignore[index, union-attr]
    }
    assert hinted == set(READ_ONLY_TOOL_NAMES)
