"""Legacy proposal and queue sensitivity checks on the SQLite on-ramp."""

from contextlib import closing
from io import BytesIO
import json
import sqlite3
from uuid import UUID

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.mcp_server import MCPServer
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from tests.sensitivity_intakes import LEVELS, WITHIN


USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _snapshot(database):
    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        result = {}
        for table, in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            result[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {quoted}")]
        return result


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent", None])
@pytest.mark.parametrize(("tool", "action", "required"), [
    ("alice_vnext_propose_memory", "memory.propose", {"canonical_text": "A synthetic proposal note."}),
    ("alice_vnext_queue_task", "queue_task.create", {"title": "Synthetic task", "task_type": "summarize",
                                                   "instructions": "Summarize synthetic notes."}),
])
def test_sqlite_legacy_creates_check_the_requested_sensitivity(tmp_path, monkeypatch, tool, action, required, profile):
    """Guard edit: change the MCP policy refusal to invalid_request."""
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    database = tmp_path / "test.sqlite3"
    bootstrap_database(database, user_id=str(USER_ID), user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    server = MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    claims = {"agent_id": "declared-sqlite", "permission_profile": profile} if profile else {}
    for level in (*LEVELS, *WITHIN):
        before = _snapshot(database)
        arguments = {**required, **claims, **({"sensitivity": level} if level is not None else {})}
        answer = server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments},
        })["result"]
        body = json.loads(answer["content"][0]["text"])
        after = _snapshot(database)
        if profile == "trusted_local_agent" and level in LEVELS:
            assert answer["isError"] is True
            assert body["error"] == {"code": "not_permitted", "message": "The tool request could not be processed"}
            assert before.keys() == after.keys()
            for table in before.keys() - {"event_log", "agent_identities"}:
                assert before[table] == after[table], table
            (caller,) = after["agent_identities"]
            assert caller["agent_id"] == "declared-sqlite"
            if before["agent_identities"]:
                (previous,) = before["agent_identities"]
                assert caller == {**previous, "updated_at": caller["updated_at"]}
            assert all(row in after["event_log"] for row in before["event_log"])
            events = [row for row in after["event_log"] if row not in before["event_log"]]
            assert sorted(row["event_type"] for row in events) == [
                "agent.identity_upserted", "agent.policy_blocked", "policy.decision",
            ]
            for event in events:
                if event["event_type"] == "agent.identity_upserted":
                    continue
                decision = json.loads(event["payload_json"])["policy_decision"]
                assert (decision["action"], decision["decision"]) == (action, "blocked")
                assert decision["reasons"] == ["restricted_sensitivity_filtered", "sensitivity_above_agent_ceiling"]
                assert event["target_type"] is None and event["target_id"] is None
        elif action == "queue_task.create" or profile is None:
            assert answer["isError"] is True
            assert body["error"]["code"] == ("tool_execution_failed" if action == "queue_task.create" else "invalid_request")
            assert after == before
        else:
            assert answer["isError"] is False
            proposal = body["proposal"]
            assert proposal["sensitivity"] == (level or "unknown")
            (stored,) = [row for row in after["memories"] if row["id"] == proposal["id"]]
            assert stored["sensitivity"] == (level or "unknown")
