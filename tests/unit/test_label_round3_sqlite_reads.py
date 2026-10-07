"""Native prelimit rejection and refill preserve ordinary restricted reads."""
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_label_writes import without_insert_floor

USER = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("tool", ["alice_recall", "alice_context_pack"])
def test_hidden_later_inputs_do_not_underfill_ranked_memory_reads(tmp_path, monkeypatch, tool):
    path = tmp_path / "refill.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn, without_insert_floor():
        store = SQLiteVNextStore(conn, USER)
        public = store.create_source({"source_type": "note", "title": "Visible", "content_hash": "public", "sensitivity": "public", "domain": "project"})
        hidden = store.create_source({"source_type": "note", "title": "Hidden", "content_hash": "hidden", "sensitivity": "confidential", "domain": "project"})
        visible_ids = set()
        for i in range(8):
            row = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "refill observation", "status": "active",
                                       "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": public["id"]}})
            visible_ids.add(str(row["id"]))
        for i in range(100):
            metadata = with_derived_from({"workflow": "project_auto_update"}, {"sources": [public, hidden]})
            # Keep the public parent first: the conservative SQL hint alone
            # cannot reject these rows. Effective admission must refill.
            metadata["derived_from"]["sources"] = [str(public["id"]), str(hidden["id"])]
            store.create_memory({"memory_key": str(uuid4()), "canonical_text": "refill observation", "status": "active",
                                 "domain": "project", "sensitivity": "public", "metadata_json": metadata})
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    result = call_mcp_tool(MCPRuntimeContext(database_url="sqlite:///" + str(path), user_id=USER), name=tool,
                           arguments={"query": "refill observation", "agent_id": "synthetic-reader"})
    rows = result["results"] if tool == "alice_recall" else result["memories"]
    assert len(rows) == 8
    assert {str(row["id"]) for row in rows} == visible_ids


def test_native_sql_prefilter_rejects_hidden_inputs_before_limit(tmp_path):
    path = tmp_path / "prefilter.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn, without_insert_floor():
        store = SQLiteVNextStore(conn, USER)
        hidden = store.create_source({"source_type": "note", "title": "Hidden", "content_hash": "hidden", "sensitivity": "confidential", "domain": "project"})
        visible = store.create_memory({"memory_key": "visible", "canonical_text": "prefilter observation", "status": "active", "domain": "project", "sensitivity": "public"})
        for i in range(12):
            parent = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "other", "status": "active", "domain": "project",
                                          "sensitivity": "public", "metadata_json": {"source_id": hidden["id"]}})
            store.create_memory({"memory_key": str(uuid4()), "canonical_text": "prefilter observation", "status": "active", "domain": "project",
                                 "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [parent["id"]]}}})
        rows = store.search_memories_fts(query="prefilter observation", sensitivity_allowed=["public", "internal"], limit=1)
        assert [row["id"] for row in rows] == [visible["id"]]


def test_scope_clamp_clears_the_legacy_project_alias_and_records_the_attempt(tmp_path):
    path = tmp_path / "clamp.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        sources = [store.create_source({"source_type": "note", "title": "Input", "content_hash": project, "sensitivity": "public",
                                        "domain": "project", "metadata_json": {"project_scope": [project]}}) for project in ("P1", "P2")]
        meta = with_derived_from({"discovered_by": "vnext_weekly_synthesis"}, {"sources": sources})
        memory = store.create_memory({"memory_key": "weekly", "canonical_text": "weekly observation", "status": "candidate", "domain": "project",
                                      "sensitivity": "public", "metadata_json": meta})
        conn.execute("UPDATE memories SET project_id='P1' WHERE id=?", (str(memory["id"]),))
        updated = store.update_memory(memory_id=str(memory["id"]), patch={"project_id": "P1", "metadata_json": {**memory["metadata_json"], "project_scope": ["P1"]}}, label_write=True)
        assert updated["project_scope"] == []
        assert updated["project_id"] is None
        assert store._label_floor_applied is True
        assert label_gap_counts(store) == (0, 0)


def test_unfenced_status_counts_use_the_native_total():
    class Native:
        def count_memories_by_status(self):
            return {"active": 5000}

        def iter_label_rows(self, kind):
            raise AssertionError("unfenced counts must not enumerate the population")

    assert LabelGuard(Native(), active=False).readable_status_counts("memory") == {"active": 5000}
