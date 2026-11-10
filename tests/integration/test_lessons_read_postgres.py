"""Published lesson retrieval through MCP on PostgreSQL."""

import json

import pytest

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
            store, user_id=user, agent_id="publisher", permission_profile="admin_agent"
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
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={"lesson": block, "agent_id": "publisher", "project_scope": ["alpha"]},
    )
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
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    for fields in (
        {"project_scope": ["beta"]},
        {"project_scope": ["alpha"], "domain": "family"},
        {"project_scope": ["alpha"], "sensitivity": "confidential"},
    ):
        hidden = call_mcp_tool(
            context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher", **fields}
        )
        if hidden["status"] == "confirmation_required":
            call_mcp_tool(
                context,
                name="alice_memory_manage",
                arguments={
                    "action": "confirm",
                    "memory_id": hidden["memory"]["id"],
                    "confirmation_id": hidden["confirmation_id"],
                    "agent_id": "publisher",
                },
            )
        else:
            assert hidden["status"] == "committed"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    assert (
        call_mcp_tool(
            context,
            name="alice_lessons",
            arguments={"task": "rebasing a branch", "paths": ["src/a.py"], "agent_id": "reader"},
        )
        == result
    )


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


def test_postgres_lesson_rank_counts_title_twice(migrated_database_urls, monkeypatch):
    """Guard: equal matched-term counts tie before creation time and id."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="lessons-rank-postgres@example.invalid")
    with user_connection(url, user) as conn:
        _, admin = create_agent_key(
            PostgresVNextStore(conn), user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha"
        )
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    block = {"applies_when": "rebasing", "what_happened": "Conflict.", "do_instead": "Inspect both."}
    older = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    newer = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        store.update_memory(memory_id=str(older["memory"]["id"]), patch={"title": "rebasing", "canonical_text": "rebasing"})
        store.update_memory(memory_id=str(newer["memory"]["id"]), patch={"title": "other", "canonical_text": "rebasing rebasing rebasing"})
    result = call_mcp_tool(context, name="alice_lessons", arguments={"task": "rebasing", "agent_id": "publisher"})
    assert [row["id"] for row in result["lessons"]] == [str(newer["memory"]["id"]), str(older["memory"]["id"])]


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


@pytest.mark.parametrize("mode", ["text", "vector"])
def test_postgres_search_excludes_unreadable_candidates(migrated_database_urls, monkeypatch, mode):
    """Guard: hidden matches cannot displace a readable lesson."""
    import alicebot_api.vnext_retrieval as retrieval
    from alicebot_api.vnext_embeddings import EMBEDDING_SIGNATURE_VERSION, memory_embedding_content_sha256
    from tests.unit.test_vnext_retrieval import StubEmbeddingProvider

    url = migrated_database_urls["app"]
    user = seed_user(url, email="lessons-vector-fence@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(store, user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha")
        _, reader = create_agent_key(store, user_id=user, agent_id="reader", permission_profile="project_scoped_agent", project_scope="alpha")
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    monkeypatch.setattr(retrieval, "get_embedding_provider", lambda: StubEmbeddingProvider() if mode == "vector" else None)
    original_page = retrieval.page_after_label_drop
    monkeypatch.setattr(retrieval, "page_after_label_drop", lambda **kwargs: original_page(**kwargs, cap=1))
    block = {"applies_when": "rebasing", "what_happened": "Conflict.", "do_instead": "Inspect both."}
    visible = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        memory = store.get_memory(str(visible["memory"]["id"]))
        store.update_memory_embedding(memory_id=str(memory["id"]), vector=[1.0, *([0.0] * 1535)], provider="stub", model="stub-embedding", content_sha256=memory_embedding_content_sha256(memory), signature_version=EMBEDDING_SIGNATURE_VERSION)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    args = {"task": "rebasing" if mode == "text" else "unrelatedtoken", "agent_id": "reader"}
    before = call_mcp_tool(context, name="alice_lessons", arguments=args)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    hidden = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": block, "agent_id": "publisher"})
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Restricted input", "domain": "project", "sensitivity": "confidential", "content_hash": "sha256:restricted", "metadata_json": {"project_scope": ["alpha"]}})
        public = store.create_source({"source_type": "note", "title": "Public input", "domain": "project", "sensitivity": "internal", "content_hash": "sha256:public-input", "metadata_json": {"project_scope": ["alpha"]}})
        memory = store.get_memory(str(hidden["memory"]["id"]))
        metadata = dict(memory["metadata_json"])
        metadata["derived_from"] = {"sources": [str(public["id"]), str(source["id"])]}
        conn.execute("UPDATE memories SET metadata_json = %s::jsonb WHERE id = %s::uuid", (json.dumps(metadata), str(memory["id"])))
        memory = store.get_memory(str(memory["id"]))
        store.update_memory_embedding(memory_id=str(memory["id"]), vector=[0.1] * 1536, provider="stub", model="stub-embedding", content_sha256=memory_embedding_content_sha256(memory), signature_version=EMBEDDING_SIGNATURE_VERSION)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    after = call_mcp_tool(context, name="alice_lessons", arguments=args)
    assert before == after
    assert [item["id"] for item in after["lessons"]] == [str(visible["memory"]["id"])]


def test_postgres_project_key_does_not_emit_a_sensitivity_filter_event(migrated_database_urls, monkeypatch):
    """Guard: the PostgreSQL policy request stays within the caller's ceiling."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="lessons-policy-postgres@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, publisher = create_agent_key(
            store, user_id=user, agent_id="publisher", permission_profile="admin_agent", project_scope="alpha"
        )
        _, reader = create_agent_key(
            store, user_id=user, agent_id="reader", permission_profile="project_scoped_agent", project_scope="alpha"
        )
    context = MCPRuntimeContext(database_url=url, user_id=user)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", publisher)
    lesson = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "agent_id": "publisher",
            "lesson": {"applies_when": "rebasing", "what_happened": "Conflict.", "do_instead": "Inspect both."},
        },
    )
    with user_connection(url, user) as conn:
        before = {str(row["id"]) for row in PostgresVNextStore(conn).list_events()}
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    answer = call_mcp_tool(context, name="alice_lessons", arguments={"task": "rebasing", "agent_id": "reader"})
    with user_connection(url, user) as conn:
        events = [row for row in PostgresVNextStore(conn).list_events() if str(row["id"]) not in before]
    assert [item["id"] for item in answer["lessons"]] == [str(lesson["memory"]["id"])]
    assert sum(row["event_type"] == "agent.lessons_requested" for row in events) == 1
    assert all(row["event_type"] != "agent.policy_filtered" for row in events)
