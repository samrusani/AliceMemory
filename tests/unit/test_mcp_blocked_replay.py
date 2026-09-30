"""A blocked MCP replay keeps its policy rows and answers tool_request_failed."""

from __future__ import annotations

import json
from io import BytesIO
from uuid import uuid4

from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

CONFLICT = "idempotency_key was already used for a different memory request"


def _arguments(agent_id: str, profile: str, text: str, key: str) -> dict[str, object]:
    return {
        "title": "Replay fence",
        "canonical_text": text,
        "domain": "professional",
        "sensitivity": "internal",
        "confidence": 0.96,
        "idempotency_key": key,
        "agent_id": agent_id,
        "agent_type": "personal_assistant",
        "permission_profile": profile,
    }


def _call(context: MCPRuntimeContext, arguments: dict[str, object]) -> MCPToolError:
    try:
        call_mcp_tool(context, name="alice_memory_commit", arguments=arguments)
    except Exception as exc:
        assert type(exc) is MCPToolError
        return exc
    raise AssertionError("expected MCPToolError")


def _wire_code(context: MCPRuntimeContext, arguments: dict[str, object]) -> str:
    server = MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    response = server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "alice_memory_commit", "arguments": arguments},
        }
    )
    assert response is not None
    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)
    return str(payload["error"]["code"])


def test_read_only_mcp_replays_keep_policy_rows(tmp_path, monkeypatch) -> None:
    """Same-content and different-content replays both stay MCPToolError.

    pytest.raises would let AgentPolicyBlockedError escape without an
    assertion failure. The policy rows are counted after both replays.
    """

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    database = tmp_path / "replay.db"
    user_id = uuid4()
    context = MCPRuntimeContext(database_url=f"sqlite:///{database}", user_id=user_id)
    key = "mcp-blocked-replay"
    original = "A later read-only replay of this note must keep its policy rows."
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments=_arguments("writer", "trusted_local_agent", original, key),
    )
    assert committed["status"] == "committed"
    same = _call(context, _arguments("readonly", "read_only_agent", original, key))
    different = _call(
        context,
        _arguments("readonly", "read_only_agent", "This replay asks for a different note.", key),
    )
    assert CONFLICT in str(different)
    assert CONFLICT not in str(same)
    with sqlite_user_connection(database, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        events = store.list_events()
        identities = store.list_agent_identities(limit=20)
    readonly_events = [event for event in events if event.get("actor_id") == "readonly"]
    assert sum(event.get("event_type") == "policy.decision" for event in readonly_events) == 2
    assert sum(event.get("event_type") == "agent.policy_blocked" for event in readonly_events) == 2
    assert sum(row.get("agent_id") == "readonly" for row in identities) == 1
    assert _wire_code(context, _arguments("readonly", "read_only_agent", original, key)) == "tool_request_failed"
    assert (
        _wire_code(context, _arguments("readonly", "read_only_agent", "Another different note.", key))
        == "tool_request_failed"
    )
