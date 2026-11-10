"""Published lesson retrieval through MCP on PostgreSQL."""

import json

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_mcp_server import seed_user


def test_published_lessons_use_postgres_fence_and_revision(migrated_database_urls, monkeypatch):
    """Guard: the Postgres lesson stage includes only the caller's published rows."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="lessons-postgres@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(
            store, user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha"
        )
        _, reader = create_agent_key(
            store, user_id=user, agent_id="reader", permission_profile="project_scoped_agent", project_scope="alpha"
        )
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    block = {
        "applies_when": "rebasing a branch",
        "what_happened": "A change was hidden.",
        "do_instead": "Inspect both sides.",
        "paths": ["src/*.py"],
    }
    committed = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    result = call_mcp_tool(
        context,
        name="alice_lessons",
        arguments={"task": "rebasing a branch", "paths": ["src/a.py"], "agent_id": "reader"},
    )
    assert result["count"] == 1
    assert result["lessons"][0]["id"] == str(committed["memory"]["id"])
    assert result["lessons"][0]["published"]["how"] == "committed"
    assert result["lessons"][0]["published"]["by"] == "publisher"
    assert result["lessons"][0]["paths"] == block["paths"]
    assert "rebasing a branch" not in json.dumps(result["lessons"][0]["published"])


def test_postgres_skips_planted_blocks_and_returns_path_matches(migrated_database_urls, monkeypatch):
    """Guard: the publishing revision must contain the lesson and path-only matches still use the caller fence."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="lessons-path-postgres@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(
            store, user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha"
        )
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    block = {
        "applies_when": "rebasing a branch",
        "what_happened": "A change was hidden.",
        "do_instead": "Inspect both sides.",
        "paths": ["src/*.py"],
    }
    committed = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    plain = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": "plain",
            "canonical_text": "A plain procedure.",
            "memory_type": "procedure",
            "agent_id": "publisher",
        },
    )
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        row = store.get_memory(str(plain["memory"]["id"]))
        value = dict(row["value"])
        value["lesson"] = block
        store.update_memory(memory_id=str(row["id"]), patch={"value": value})
    result = call_mcp_tool(
        context,
        name="alice_lessons",
        arguments={"task": "unrelatedtopic", "paths": ["src/example.py"], "agent_id": "publisher"},
    )
    assert [row["id"] for row in result["lessons"]] == [str(committed["memory"]["id"])]


def test_postgres_http_review_publishing_acts(migrated_database_urls, monkeypatch):
    """Guard: both HTTP review promotions count as publishing acts."""
    from tests.integration.test_lesson_block_http_postgres import _route_patches
    from tests.integration.test_vnext_live_workspace_api import invoke_request

    url = migrated_database_urls["app"]
    _route_patches(monkeypatch, url)
    user = seed_user(url, email="lessons-review-postgres@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(
            store, user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha"
        )
        _, writer = create_agent_key(
            store, user_id=user, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha"
        )
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    block = {
        "applies_when": "rebasing a branch",
        "what_happened": "A change was hidden.",
        "do_instead": "Inspect both sides.",
        "paths": ["src/*.py"],
    }
    ids = []
    for action in ("accept", "promote"):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", writer)
        held = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "writer"})
        assert held["status"] == "review_required"
        mid = str(held["memory"]["id"])
        ids.append(mid)
        status, _ = invoke_request(
            "POST",
            f"/v0/vnext/memories/{mid}/review",
            payload={
                "user_id": str(user),
                "action": action,
                "agent": {
                    "agent_id": "publisher",
                    "agent_type": "coding_agent",
                    "permission_profile": "admin_agent",
                    "project_scope": ["alpha"],
                },
            },
            authorization=f"Bearer {admin}",
        )
        assert status == 200
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    result = call_mcp_tool(
        context, name="alice_lessons", arguments={"task": "rebasing", "agent_id": "publisher", "limit": 10}
    )
    assert {row["id"] for row in result["lessons"]} == set(ids)
    assert all(
        row["published"]["how"] == "approved" and row["published"]["by"] == "publisher" for row in result["lessons"]
    )
