from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import workspaces as workspaces_router
from alicebot_api.store import ContinuityStore


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HTTP_OPERATION_COUNT = 183
DEFAULT_MCP_TOOL_NAMES = [
    "alice_memory_commit",
    "alice_recall",
    "alice_resume",
]

# The ordinary PostgreSQL integration runner intentionally enables the legacy
# HTTP surface. This smoke has its own required CI matrix row, so do not let a
# flag-on run masquerade as default-surface evidence.
pytestmark = pytest.mark.skipif(
    "ALICE_LEGACY_SURFACES" in os.environ,
    reason="default-surface smoke runs in its dedicated flag-off CI context",
)


def _invoke_http(
    method: str,
    path: str,
    *,
    user_id: UUID,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}
        request_received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": urlencode({}).encode("ascii"),
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"x-alicebot-user-id", str(user_id).encode("ascii")),
        ],
        # An in-process caller is a loopback peer; a synthetic host would be
        # refused by the keyless loopback gate before reaching the route.
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body)


def _write_mcp_message(stream: Any, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    stream.write(encoded)
    stream.flush()


def _read_mcp_message(stream: Any) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            raise RuntimeError("MCP server closed stdout unexpectedly")
        if line in {b"\r\n", b"\n"}:
            break
        key, value = line.decode("utf-8").strip().split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return json.loads(stream.read(int(headers["content-length"])).decode("utf-8"))


class _MCPClient:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._next_id = 1

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        _write_mcp_message(self.process.stdin, request)
        response = _read_mcp_message(self.process.stdout)
        assert response.get("id") == request_id
        return response

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        assert self.process.stdin is not None
        request: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        _write_mcp_message(self.process.stdin, request)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _start_mcp_client(*, database_url: str, user_id: UUID) -> _MCPClient:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["ALICEBOT_AUTH_USER_ID"] = str(user_id)
    for name in (
        "ALICE_LEGACY_SURFACES",
        "ALICE_MCP_LEGACY_TOOLS",
        "ALICE_MCP_FULL_TOOLS",
        "ALICE_AGENT_API_KEY",
    ):
        env.pop(name, None)
    pythonpath = [str(REPO_ROOT / "apps" / "api" / "src"), str(REPO_ROOT / "workers")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    process = subprocess.Popen(
        [sys.executable, "-m", "alicebot_api.mcp_server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    client = _MCPClient(process)
    initialized = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "pytest-default-surface", "version": "1.0"},
            "capabilities": {},
        },
    )
    assert initialized["result"]["protocolVersion"] == "2024-11-05"
    client.notify("notifications/initialized", {})
    return client


def _call_tool(
    client: _MCPClient,
    *,
    name: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    response = client.request("tools/call", {"name": name, "arguments": arguments})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def test_default_http_and_mcp_surfaces_complete_core_round_trip(
    migrated_database_urls: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ALICE_LEGACY_SURFACES",
        "ALICE_MCP_LEGACY_TOOLS",
        "ALICE_AGENT_API_KEY",
    ):
        assert name not in os.environ

    schema = main_module.app.openapi()
    operations = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert len(operations) == DEFAULT_HTTP_OPERATION_COUNT
    assert operations.isdisjoint(main_module.LEGACY_HTTP_OPERATION_KEYS)

    settings = Settings(
        app_env="test",
        database_url=migrated_database_urls["app"],
        database_admin_url=migrated_database_urls["admin"],
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(workspaces_router, "get_settings", lambda: settings)

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            "default-surface@example.invalid",
            "Default Surface Smoke",
        )

    bootstrap_status, bootstrap = _invoke_http(
        "POST",
        "/v1/workspaces/bootstrap",
        user_id=user_id,
    )
    assert bootstrap_status == 200
    assert bootstrap["workspace"]["bootstrap_status"] == "ready"
    assert bootstrap["workspace"]["owner_user_account_id"] == str(user_id)

    client = _start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        listed_tools = client.request("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in listed_tools] == DEFAULT_MCP_TOOL_NAMES

        committed = _call_tool(
            client,
            name="alice_memory_commit",
            arguments={
                "title": "Default surface smoke decision",
                "canonical_text": "The default surface smoke protects the core round trip.",
                "memory_type": "decision",
                "domain": "project",
                "sensitivity": "internal",
                "confidence": 0.96,
            },
        )
        assert committed["status"] == "committed"
        memory_id = committed["memory"]["id"]

        recalled = _call_tool(
            client,
            name="alice_recall",
            arguments={"query": "default surface core round trip"},
        )
        assert any(item["id"] == memory_id for item in recalled["results"])

        resumed = _call_tool(
            client,
            name="alice_resume",
            arguments={},
        )
        assert resumed["brief"]["last_decision"]["id"] == memory_id
    finally:
        client.close()
