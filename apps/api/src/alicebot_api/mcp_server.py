from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, BinaryIO, Literal
from uuid import UUID

from alicebot_api import __version__
from alicebot_api.config import Settings, get_runtime_settings, get_settings
from alicebot_api.mcp_tools import (
    MCPRuntimeContext,
    MCPToolError,
    MCPToolNotFoundError,
    call_mcp_tool,
    list_mcp_tools,
)
from alicebot_api.recall_framing import serialize_mcp_tool_result


_JSONRPC_VERSION = "2.0"
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_SERVER_NAME = "alice-core-mcp"
_DEFAULT_MCP_USER_ID = "00000000-0000-0000-0000-000000000001"
_TransportMode = Literal["content-length", "json-line"]
_TRANSPORT_CONTENT_LENGTH: _TransportMode = "content-length"
_TRANSPORT_JSON_LINE: _TransportMode = "json-line"
_JSONRPC_PARSE_ERROR_MESSAGE = "Parse error"
_JSONRPC_INVALID_REQUEST_MESSAGE = "Invalid Request"
_JSONRPC_INVALID_PARAMS_MESSAGE = "Invalid params"
_JSONRPC_METHOD_NOT_FOUND_MESSAGE = "Method not found"
_TOOL_NOT_FOUND_CODE = "tool_not_found"
_TOOL_NOT_FOUND_MESSAGE = "The requested tool is not available"
_TOOL_REQUEST_FAILED_CODE = "tool_request_failed"
_TOOL_REQUEST_FAILED_MESSAGE = "The tool request could not be processed"
_TOOL_EXECUTION_FAILED_CODE = "tool_execution_failed"
_TOOL_EXECUTION_FAILED_MESSAGE = "The tool could not be executed"
_MCP_STARTUP_FAILED_CODE = "mcp_startup_failed"
_MCP_STARTUP_FAILED_MESSAGE = "The Alice MCP server could not start"

logger = logging.getLogger(__name__)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid UUID value: {value}") from exc


def _resolve_user_id(settings: Settings, user_id_flag: str | None) -> UUID:
    if user_id_flag is not None:
        return _parse_uuid(user_id_flag)
    if settings.auth_user_id != "":
        return UUID(settings.auth_user_id)
    return UUID(os.getenv("ALICEBOT_AUTH_USER_ID", _DEFAULT_MCP_USER_ID))


def _settings_with_runtime_overrides(args: argparse.Namespace) -> Settings:
    """Apply stdio-runtime overrides before validating MCP-relevant settings."""

    if args.database_url is None and args.user_id is None:
        if os.getenv("APP_ENV", "development") in {"development", "test"}:
            return get_settings()
        return get_runtime_settings()
    effective_env = dict(os.environ)
    if args.database_url is not None:
        effective_env["DATABASE_URL"] = args.database_url
    if args.user_id is not None:
        try:
            UUID(args.user_id)
        except ValueError as exc:
            raise ValueError(f"--user-id must be a UUID, got: {args.user_id}") from exc
        effective_env["ALICEBOT_AUTH_USER_ID"] = args.user_id
    return Settings.from_env(
        effective_env,
        require_production_services=False,
    )


def _build_runtime_context(args: argparse.Namespace) -> MCPRuntimeContext:
    settings = _settings_with_runtime_overrides(args)
    database_url = settings.database_url
    user_id = _resolve_user_id(settings, args.user_id)
    return MCPRuntimeContext(database_url=database_url, user_id=user_id)


def _parse_json_rpc_payload(raw_payload: str) -> dict[str, Any]:
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise ValueError("JSON-RPC payload must be an object")
    return payload


def _read_message(stream: BinaryIO) -> tuple[dict[str, Any], _TransportMode] | None:
    first_line = stream.readline()
    if first_line == b"":
        return None

    # MCP SDK >=1.0 stdio transport sends one JSON-RPC message per line.
    stripped_first_line = first_line.strip()
    if stripped_first_line.startswith(b"{"):
        payload = _parse_json_rpc_payload(stripped_first_line.decode("utf-8"))
        return payload, _TRANSPORT_JSON_LINE

    headers: dict[str, str] = {}
    line = first_line
    while True:
        if line in {b"\r\n", b"\n"}:
            break

        decoded = line.decode("utf-8").strip()
        if ":" not in decoded:
            raise ValueError("invalid MCP header line")
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()

        line = stream.readline()
        if line == b"":
            return None

    content_length_raw = headers.get("content-length")
    if content_length_raw is None:
        raise ValueError("missing Content-Length header")
    try:
        content_length = int(content_length_raw)
    except ValueError as exc:
        raise ValueError("invalid Content-Length header") from exc
    if content_length < 0:
        raise ValueError("invalid Content-Length header")

    body = stream.read(content_length)
    if len(body) != content_length:
        return None
    payload = _parse_json_rpc_payload(body.decode("utf-8"))
    return payload, _TRANSPORT_CONTENT_LENGTH


def _write_message(
    stream: BinaryIO,
    message: dict[str, Any],
    *,
    transport_mode: _TransportMode,
) -> None:
    encoded = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if transport_mode == _TRANSPORT_JSON_LINE:
        stream.write(encoded + b"\n")
    else:
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stream.write(header)
        stream.write(encoded)
    stream.flush()


def _response_success(request_id: object, *, result: object) -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    }


def _response_error(request_id: object, *, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _stable_error_json(*, code: str, message: str) -> str:
    """Serialize a stable public error object exactly once for MCP text content."""

    return json.dumps(
        {"error": {"code": code, "message": message}},
        separators=(",", ":"),
        sort_keys=True,
    )


def _tool_error_result(*, code: str, message: str) -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": _stable_error_json(code=code, message=message),
            }
        ],
        "isError": True,
    }


def _write_stderr_error(*, code: str, message: str) -> None:
    """Write the deterministic startup contract without exception internals."""

    print(_stable_error_json(code=code, message=message), file=sys.stderr)


class MCPServer:
    def __init__(self, *, context: MCPRuntimeContext, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
        self._context = context
        self._input_stream = input_stream
        self._output_stream = output_stream
        self._transport_mode: _TransportMode = _TRANSPORT_CONTENT_LENGTH

    def _handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("jsonrpc") != _JSONRPC_VERSION:
            return _response_error(
                request.get("id"),
                code=-32600,
                message=_JSONRPC_INVALID_REQUEST_MESSAGE,
            )

        method = request.get("method")
        if not isinstance(method, str):
            return _response_error(
                request.get("id"),
                code=-32600,
                message=_JSONRPC_INVALID_REQUEST_MESSAGE,
            )

        request_id = request.get("id")
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _response_error(
                request_id,
                code=-32602,
                message=_JSONRPC_INVALID_PARAMS_MESSAGE,
            )

        if method == "notifications/initialized":
            return None

        if request_id is None:
            return None

        if method == "initialize":
            return _response_success(
                request_id,
                result={
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": _MCP_SERVER_NAME,
                        "version": __version__,
                    },
                },
            )

        if method == "ping":
            return _response_success(request_id, result={})

        if method == "tools/list":
            return _response_success(
                request_id,
                result={
                    "tools": list_mcp_tools(),
                },
            )

        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _response_error(
                    request_id,
                    code=-32602,
                    message=_JSONRPC_INVALID_PARAMS_MESSAGE,
                )

            arguments = params.get("arguments")
            try:
                structured = call_mcp_tool(
                    self._context,
                    name=name,
                    arguments=arguments,
                )
            except MCPToolNotFoundError:
                logger.info("MCP tool was not found name=%s", name, exc_info=True)
                return _response_success(
                    request_id,
                    result=_tool_error_result(
                        code=_TOOL_NOT_FOUND_CODE,
                        message=_TOOL_NOT_FOUND_MESSAGE,
                    ),
                )
            except MCPToolError:
                logger.warning("MCP tool request failed name=%s", name, exc_info=True)
                return _response_success(
                    request_id,
                    result=_tool_error_result(
                        code=_TOOL_REQUEST_FAILED_CODE,
                        message=_TOOL_REQUEST_FAILED_MESSAGE,
                    ),
                )
            except Exception:
                logger.exception("MCP tool execution failed name=%s", name)
                return _response_success(
                    request_id,
                    result=_tool_error_result(
                        code=_TOOL_EXECUTION_FAILED_CODE,
                        message=_TOOL_EXECUTION_FAILED_MESSAGE,
                    ),
                )

            # Protocol 2024-11-05 clients read tool results from content[].text,
            # so the payload is serialized exactly once. structuredContent is a
            # later-protocol field and would double the bytes on the wire.
            return _response_success(
                request_id,
                result={
                    "content": [
                        {
                            "type": "text",
                            "text": serialize_mcp_tool_result(structured),
                        }
                    ],
                    "isError": False,
                },
            )

        return _response_error(
            request_id,
            code=-32601,
            message=_JSONRPC_METHOD_NOT_FOUND_MESSAGE,
        )

    def run(self) -> int:
        while True:
            try:
                framed_request = _read_message(self._input_stream)
            except json.JSONDecodeError:
                logger.warning("MCP JSON-RPC parse failed", exc_info=True)
                response = _response_error(
                    None,
                    code=-32700,
                    message=_JSONRPC_PARSE_ERROR_MESSAGE,
                )
                _write_message(
                    self._output_stream,
                    response,
                    transport_mode=self._transport_mode,
                )
                continue
            except ValueError:
                logger.warning("MCP JSON-RPC framing was invalid", exc_info=True)
                response = _response_error(
                    None,
                    code=-32600,
                    message=_JSONRPC_INVALID_REQUEST_MESSAGE,
                )
                _write_message(
                    self._output_stream,
                    response,
                    transport_mode=self._transport_mode,
                )
                continue

            if framed_request is None:
                return 0

            request, transport_mode = framed_request
            self._transport_mode = transport_mode
            request_response = self._handle_request(request)
            if request_response is not None:
                _write_message(
                    self._output_stream,
                    request_response,
                    transport_mode=self._transport_mode,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alicebot-mcp",
        description=(
            "Local MCP server exposing Alice's memory tools over stdio. "
            "Set ALICE_MCP_LEGACY_TOOLS=1 to expose the legacy long-tail tools."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override database URL. Defaults to settings/env DATABASE_URL.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help=(
            "Override acting user UUID. Defaults to ALICEBOT_AUTH_USER_ID when set, "
            f"otherwise {_DEFAULT_MCP_USER_ID}."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        context = _build_runtime_context(args)
    except Exception:
        _write_stderr_error(
            code=_MCP_STARTUP_FAILED_CODE,
            message=_MCP_STARTUP_FAILED_MESSAGE,
        )
        return 1

    server = MCPServer(
        context=context,
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
    )
    return server.run()


__all__ = ["MCPServer", "build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
