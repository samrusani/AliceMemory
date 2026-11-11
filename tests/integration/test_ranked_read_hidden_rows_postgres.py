"""PostgreSQL ranks each row independently of hidden corpus contents."""

import json

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_mcp_server import seed_user
from tests.unit.test_ranked_read_hidden_rows import QUERY, TEXTS, add_hidden


@pytest.mark.parametrize("hiding", ("project", "sensitivity"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_postgres_rank_does_not_depend_on_hidden_rows(migrated_database_urls, monkeypatch, hiding, stage):
    """Guard: ts_rank reads only the matched row, for both memories and chunks."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="rank-order@example.invalid")
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_RERANKER_BASE_URL", "ALICE_RERANKER_MODEL", "ALICE_SEARCH_QUALITY"):
        monkeypatch.delenv(name, raising=False)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(store, user_id=user, agent_id="publisher", permission_profile="admin_agent")
        _, key = create_agent_key(store, user_id=user, agent_id="reader", permission_profile="project_scoped_agent", project_scope="alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    ids = []
    for text in TEXTS:
        labels = {"domain": "project", "sensitivity": "public", "project_scope": ["alpha"]}
        if stage == "memory":
            result = call_mcp_tool(context, name="alice_memory_commit", arguments={"title": "Migration note", "canonical_text": text, "memory_type": "procedure", **labels})
            assert result["status"] == "committed"
            ids.append(str(result["memory"]["id"]))
        else:
            result = call_mcp_tool(context, name="alice_capture", arguments={"title": "Captured note", "raw_text": text, **labels})
            ids.append(str(result["source_id"]))
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)

    def read():
        return json.loads(json.dumps(call_mcp_tool(context, name="alice_recall", arguments={"query": QUERY, "include_sources": stage == "source", "limit": 5}), default=str))

    before = read()
    section = "results" if stage == "memory" else "sources"
    assert {row["id"] for row in before[section]} == set(ids)
    with user_connection(url, user) as conn:
        add_hidden(PostgresVNextStore(conn), stage, hiding)
    assert read() == before
