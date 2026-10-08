"""SQLite lifecycle and saved-quote regressions from the independent review."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService

USER = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("stale", [False, True])
def test_sqlite_digest_replays_after_project_moves(tmp_path, monkeypatch, stale):
    path = tmp_path / "moves.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    alpha, beta = str(uuid4()), str(uuid4())
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({"source_type": "note", "title": "Synthetic task", "content_hash": "move",
                                      "domain": "project", "sensitivity": "public",
                                      "metadata_json": {"project_scope": [alpha], "raw_text": "TODO: Round trip task"}})
        request = ProjectAutomationRequest(agent_identity=None, project_id=alpha)
        initial = VNextProjectService(store).extract_open_loops(request)
        assert len(initial) == 1
        store.update_source(source_id=str(source["id"]), patch={"metadata_json": {**source["metadata_json"], "project_scope": [beta]}})
        if not stale:
            store.update_source(source_id=str(source["id"]), patch={"metadata_json": {**source["metadata_json"], "project_scope": [alpha]}})
        assert store.get_open_loop(str(initial[0]["id"]))["project_id"] is None
        if stale:
            monkeypatch.setattr(store, "search_sources", lambda **_kwargs: [deepcopy(source)])
        for _ in range(2):
            replay = VNextProjectService(store).extract_open_loops(request)
            assert [row["id"] for row in replay] == [initial[0]["id"]]
        rows = store.list_open_loops(status=None, limit=20)
        assert len(rows) == 1
        assert set(rows[0]["metadata_json"]["project_floor"]) == {alpha, beta}
        assert label_gap_counts(store) == (0, 0)


@pytest.mark.parametrize("profile", ["trusted_local_agent", "read_only_agent", "admin_agent"])
@pytest.mark.parametrize("writer", ["imported", "legacy_commit"])
def test_legacy_review_list_rechecks_saved_quotes(tmp_path, monkeypatch, profile, writer):
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    path = tmp_path / "quotes.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    quote = "Synthetic saved-quote sentinel"
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({"source_type": "note", "title": "Synthetic source", "content_hash": "quote",
                                      "domain": "project", "sensitivity": "public"})
        if writer == "imported":
            memory = store.create_memory({"memory_key": "original", "canonical_text": "Original synthetic candidate", "status": "candidate",
                                          "domain": "project", "sensitivity": "public",
                                          "metadata_json": {"provenance": {"source_id": str(source["id"]), "quote": quote}}})
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=USER)
    if writer == "legacy_commit":
        committed = call_mcp_tool(context, name="alice_vnext_commit_memory", arguments={
            "agent_id": "writer", "permission_profile": "trusted_local_agent", "title": "Synthetic observation",
            "canonical_text": "Original synthetic candidate", "intent": "inferred_observation", "domain": "project",
            "sensitivity": "public", "source_refs": [str(source["id"])], "conversation_excerpt": quote})
        memory = committed["memory"]
        assert memory["status"] == "candidate"
    arguments = {"limit": 20, "agent_id": "reader", "permission_profile": profile}
    before = call_mcp_tool(context, name="alice_vnext_review_items", arguments=arguments)
    assert quote in json.dumps(before)
    with sqlite_user_connection(path, USER) as conn:
        SQLiteVNextStore(conn, USER).update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
    after = call_mcp_tool(context, name="alice_vnext_review_items", arguments=arguments)
    assert [str(row["id"]) for row in after["items"]] == [str(memory["id"])]
    assert (quote in json.dumps(after)) is (profile == "admin_agent")
    owner = call_mcp_tool(context, name="alice_vnext_review_items", arguments={"limit": 20})
    assert quote in json.dumps(owner)
