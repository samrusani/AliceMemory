"""The default MCP list includes lesson reads on PostgreSQL."""

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_mcp_server import seed_user


def test_keyed_default_server_lists_and_reads_lessons(migrated_database_urls, monkeypatch):
    """Guard: take alice_lessons off the default order and its call is refused."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="fixture-lessons-default@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(store, user_id=user, agent_id="admin", permission_profile="admin_agent", project_scope="alpha")
        _, reader = create_agent_key(store, user_id=user, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha")
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    assert [tool["name"] for tool in list_mcp_tools()] == ["alice_memory_commit", "alice_recall", "alice_resume", "alice_lessons"]
    context = MCPRuntimeContext(database_url=url, user_id=user)
    created = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": {
        "applies_when": "editing the release notes", "what_happened": "A check failed.", "do_instead": "Review the check."
    }})
    assert created["status"] == "committed"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    answer = call_mcp_tool(context, name="alice_lessons", arguments={"task": "editing release notes"})
    assert [row["id"] for row in answer["lessons"]] == [str(created["memory"]["id"])]
