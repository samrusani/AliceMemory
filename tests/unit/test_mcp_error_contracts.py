from __future__ import annotations

from io import BytesIO
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp.types import MCPInvalidRequestError
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, MCPToolNotFoundError


_SENTINEL = "PRIVATE-MCP-EXCEPTION-SENTINEL"
_CONTEXT = MCPRuntimeContext(
    database_url="postgresql://localhost/alicebot",
    user_id=UUID("11111111-1111-4111-8111-111111111111"),
)


def _server(*, input_bytes: bytes = b"") -> tuple[mcp_server.MCPServer, BytesIO]:
    output = BytesIO()
    return (
        mcp_server.MCPServer(
            context=_CONTEXT,
            input_stream=BytesIO(input_bytes),
            output_stream=output,
        ),
        output,
    )


def _call_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "alice_recall", "arguments": {}},
    }


def _tool_error_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1
    item = content[0]
    assert isinstance(item, dict)
    text = item["text"]
    assert isinstance(text, str)
    assert _SENTINEL not in text
    payload = json.loads(text)
    assert json.dumps(payload, separators=(",", ":"), sort_keys=True) == text
    return payload


@pytest.mark.parametrize(
    ("exception", "expected"),
    (
        (
            MCPToolNotFoundError(_SENTINEL),
            {
                "error": {
                    "code": "tool_not_found",
                    "message": "The requested tool is not available",
                }
            },
        ),
        (
            MCPToolError(_SENTINEL),
            {
                "error": {
                    "code": "tool_request_failed",
                    "message": "The tool request could not be processed",
                }
            },
        ),
        (
            RuntimeError(_SENTINEL),
            {
                "error": {
                    "code": "tool_execution_failed",
                    "message": "The tool could not be executed",
                }
            },
        ),
        # The one failure whose message is not static: counts and a limit the
        # client can act on. It is caught before its base class, MCPToolError.
        (
            MCPInvalidRequestError("query has 500 distinct search terms; the limit is 499. Use a shorter query."),
            {
                "error": {
                    "code": "invalid_request",
                    "message": "query has 500 distinct search terms; the limit is 499. Use a shorter query.",
                }
            },
        ),
    ),
)
def test_mcp_wire_tool_errors_are_stable_and_serialized_once(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected: dict[str, object],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise exception

    monkeypatch.setattr(mcp_server, "call_mcp_tool", fail)
    server, _output = _server()

    response = server._handle_request(_call_request())

    assert response is not None
    assert _tool_error_payload(response) == expected


def test_an_invalid_request_error_is_still_an_mcp_tool_error() -> None:
    """Code that catches ``MCPToolError`` keeps catching it.

    Mutation: make ``MCPInvalidRequestError`` a plain ``ValueError``. The
    Hermes compatibility adapter and any other ``except MCPToolError`` miss it.
    """

    assert issubclass(MCPInvalidRequestError, MCPToolError)


@pytest.mark.parametrize(
    ("request_payload", "code", "message"),
    (
        ({"jsonrpc": "PRIVATE", "id": 1, "method": "ping"}, -32600, "Invalid Request"),
        ({"jsonrpc": "2.0", "id": 1, "method": 7}, -32600, "Invalid Request"),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": _SENTINEL}, -32602, "Invalid params"),
        ({"jsonrpc": "2.0", "id": 1, "method": _SENTINEL}, -32601, "Method not found"),
    ),
)
def test_jsonrpc_request_errors_are_static(
    request_payload: dict[str, object],
    code: int,
    message: str,
) -> None:
    server, _output = _server()

    response = server._handle_request(request_payload)

    assert response is not None
    assert response["error"] == {"code": code, "message": message}
    assert _SENTINEL not in json.dumps(response)


@pytest.mark.parametrize(
    ("framed_input", "expected_code", "expected_message"),
    (
        (b"{" + _SENTINEL.encode() + b"}\n", -32700, "Parse error"),
        (_SENTINEL.encode() + b"\r\n\r\n", -32600, "Invalid Request"),
    ),
)
def test_jsonrpc_parse_and_framing_errors_are_static(
    framed_input: bytes,
    expected_code: int,
    expected_message: str,
) -> None:
    server, output = _server(input_bytes=framed_input)

    assert server.run() == 0
    output.seek(0)
    framed_response = mcp_server._read_message(output)

    assert framed_response is not None
    response, transport = framed_response
    assert transport == "content-length"
    assert response["error"] == {"code": expected_code, "message": expected_message}
    assert _SENTINEL not in json.dumps(response)


def test_mcp_startup_error_is_one_machine_readable_static_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: object) -> MCPRuntimeContext:
        raise RuntimeError(_SENTINEL)

    monkeypatch.setattr(mcp_server, "_build_runtime_context", fail)

    assert mcp_server.main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert _SENTINEL not in captured.err
    assert captured.err.count("\n") == 1
    assert json.loads(captured.err) == {
        "error": {
            "code": "mcp_startup_failed",
            "message": "The Alice MCP server could not start",
        }
    }


def _load_hermes_smoke_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_hermes_mcp_smoke.py"
    spec = importlib.util.spec_from_file_location("run_hermes_mcp_smoke_error_contract_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_compat_registry(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> Any:
    register, _shutdown, registry = module._build_local_mcp_compat_runtime()
    monkeypatch.setattr(module, "list_mcp_tools", lambda: [{"name": "alice_recall"}])
    registered = register(
        {
            "alice_core": {
                "env": {
                    "DATABASE_URL": "postgresql://localhost/alicebot",
                    "ALICEBOT_AUTH_USER_ID": "11111111-1111-4111-8111-111111111111",
                },
                "tools": {"include": ["alice_recall"]},
            }
        }
    )
    assert registered == ["mcp_alice_core_alice_recall"]
    return registry


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    (
        (MCPToolError(_SENTINEL), "tool_request_failed"),
        (RuntimeError(_SENTINEL), "tool_execution_failed"),
    ),
)
def test_hermes_compat_adapter_never_returns_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected_code: str,
) -> None:
    module = _load_hermes_smoke_module()
    registry = _registered_compat_registry(monkeypatch, module)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise exception

    monkeypatch.setattr(module, "call_mcp_tool", fail)

    raw = registry.dispatch("mcp_alice_core_alice_recall", {})

    assert _SENTINEL not in raw
    assert json.loads(raw) == {
        "error": {
            "code": expected_code,
            "message": (
                "The tool request could not be processed"
                if expected_code == "tool_request_failed"
                else "The tool could not be executed"
            ),
        }
    }


def test_hermes_compat_adapter_unknown_tool_is_stable() -> None:
    module = _load_hermes_smoke_module()
    _register, _shutdown, registry = module._build_local_mcp_compat_runtime()

    raw = registry.dispatch(_SENTINEL, {})

    assert _SENTINEL not in raw
    assert json.loads(raw) == {
        "error": {
            "code": "tool_not_found",
            "message": "The requested tool is not available",
        }
    }
