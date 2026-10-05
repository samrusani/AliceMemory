"""Real keys, operator screens and complete readable counts on PostgreSQL."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.routers import vnext_review, vnext_retrieval, vnext_projects, vnext_memories, workspaces
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.test_derived_labels_real_keys import READERS, expected_read, real_reader_key, seed_read_rows


def _user(app_url):
    user_id = uuid4()
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "synthetic@example.invalid", "Synthetic")
    return user_id


@pytest.mark.parametrize("reader", READERS)
def test_postgres_real_key_core_doors(migrated_database_urls, monkeypatch, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    monkeypatch.setattr(vnext_memories, "get_settings", lambda: Settings(database_url=app_url))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        rows = seed_read_rows(store)
        key = real_reader_key(store, user_id, reader)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    for state, row in rows.items():
        admitted = expected_read(reader, state)
        audit = vnext_memories.get_vnext_memory_audit(UUID(str(row["id"])), user_id, authorization=f"Bearer {key}" if key else None)
        assert audit.status_code == (200 if admitted else 403), (reader, state, audit.body)
        if not admitted:
            assert str(row["id"]) not in audit.body.decode()
        for tool in ("alice_recall", "alice_context_pack", "alice_recent_decisions", "alice_explain", "alice_resume"):
            arguments = {"memory_id": str(row["id"])} if tool == "alice_explain" else {"query": str(row["canonical_text"]), "sensitivity_allowed": list(ALL_SENSITIVITY)}
            try:
                result = call_mcp_tool(context, name=tool, arguments=arguments)
            except MCPToolError:
                assert not admitted, (reader, state, tool)
                continue
            rendered = json.dumps(result, default=str)
            assert (str(row["id"]) in rendered) is admitted, (reader, state, tool, rendered)
            if not admitted:
                assert str(row["title"]) not in rendered


@pytest.mark.parametrize("reader", ("owner", "admin", "trusted"))
def test_all_five_operator_screens_with_real_keys(migrated_database_urls, monkeypatch, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    for module in (vnext_review, vnext_retrieval, vnext_projects):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=app_url))
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Cedar hidden source", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "confidential"})
        public_source = store.create_source({"source_type": "note", "title": "Public source", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public"})
        memory = store.create_memory({"memory_key": "belief", "canonical_text": "Cedar hidden belief", "status": "active", "domain": "project", "sensitivity": "confidential"})
        artifact = store.create_artifact({"artifact_type": "daily_brief", "title": "Cedar hidden artifact", "content_markdown": "Cedar hidden artifact", "domain": "project", "sensitivity": "confidential", "metadata_json": {"derived_from": {"v": 1, "sources": [str(public_source["id"])], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [], "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}}, "source_refs": [str(public_source["id"])]}})
        project = store.create_project({"name": "Cedar hidden project", "slug": "cedar-hidden", "current_state": "Cedar hidden state", "domain": "project", "sensitivity": "confidential"})
        belief_id = uuid4()
        conn.execute("INSERT INTO beliefs(id,user_id,memory_id,claim) VALUES (%s,%s,%s,%s)", (belief_id, user_id, memory["id"], "Cedar hidden belief"))
        key = real_reader_key(store, user_id, reader)
    auth = f"Bearer {key}" if key else None
    responses = (
        (vnext_review.list_vnext_artifacts(user_id, authorization=auth), str(artifact["id"])),
        (vnext_retrieval.get_vnext_source_trace(UUID(str(source["id"])), user_id, authorization=auth), str(source["id"])),
        (vnext_projects.list_vnext_projects(user_id, authorization=auth), str(project["id"])),
        (vnext_projects.get_vnext_project_dashboard(str(project["id"]), user_id, authorization=auth), str(project["id"])),
        (vnext_review.get_vnext_belief_state(str(belief_id), user_id, authorization=auth), str(belief_id)),
    )
    for response, identifier in responses:
        body = response.body.decode()
        if reader == "trusted":
            assert identifier not in body
            assert "Cedar hidden" not in body
            assert response.status_code in {200, 404}
            if response.status_code == 200:
                assert json.loads(body)["count"] == 0
        else:
            assert response.status_code == 200, body
            assert identifier in body
    # A visible source trace must not carry a hidden artifact or its count.
    trace = vnext_retrieval.get_vnext_source_trace(UUID(str(public_source["id"])), user_id, authorization=auth)
    body = json.loads(trace.body)
    assert body["summary"]["artifact_count"] == (0 if reader == "trusted" else 1)
    assert (str(artifact["id"]) in trace.body.decode()) is (reader != "trusted")


def test_workspace_counts_full_population_with_sql_hidden_and_stale_rows(migrated_database_urls, monkeypatch):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        secret = store.create_source({"source_type": "note", "title": "Cedar hidden", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "confidential"})
        for index in range(205):
            store.create_source({"source_type": "note", "title": "Public source", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public"})
        for index in range(35):
            store.create_memory({"memory_key": f"visible-{index}", "canonical_text": "Public fact", "status": "candidate", "domain": "project", "sensitivity": "public"})
        with without_insert_floor():
            for index in range(40):
                store.create_memory({"memory_key": f"hidden-{index}", "canonical_text": "Cedar hidden", "status": "candidate", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": str(secret["id"])}})
        store.create_artifact({"artifact_type": "daily_brief", "title": "Cedar hidden", "content_markdown": "Cedar hidden", "domain": "project", "sensitivity": "confidential", "metadata_json": {"derived_from": {"v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [], "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}}}})
        store.create_project({"name": "Cedar hidden", "slug": "hidden", "domain": "project", "sensitivity": "confidential"})
        body = workspaces._vnext_workspace_payload(store)
        assert body["summary"]["source_count"] == 205
        assert body["summary"]["candidate_memory_count"] == 35
        assert body["summary"]["artifact_count"] == 0
        assert body["summary"]["project_count"] == 0
        assert body["samples"]["sources"]["has_more"] is True
        assert "Cedar hidden" not in json.dumps(body, default=str)
        batches = list(store.iter_label_rows("source", batch_size=100))
        assert [len(batch) for batch in batches] == [100, 100, 6]
        assert all("content_markdown" not in row and "title" not in row for batch in batches for row in batch)


@pytest.mark.parametrize("reader", ("owner", "admin", "trusted"))
def test_workspace_activity_uses_actual_key_and_current_targets(migrated_database_urls, monkeypatch, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    monkeypatch.setattr(workspaces, "get_settings", lambda: Settings(database_url=app_url))
    hidden_ids = []
    visible_ids = []
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Cedar hidden parent", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "confidential"})
        for hidden in (False, True):
            metadata = {"agentic_memory": {"kind": "agentic_memory_commit", "confirmation": {"status": "pending"}}}
            if hidden:
                metadata["source_id"] = str(source["id"])
            with without_insert_floor():
                memory = store.create_memory({"memory_key": f"activity-{hidden}", "canonical_text": "Cedar hidden activity" if hidden else "Public activity", "status": "needs_review", "confirmation_status": "unconfirmed", "domain": "project", "sensitivity": "public", "metadata_json": metadata})
            (hidden_ids if hidden else visible_ids).append(str(memory["id"]))
            event = append_event(store, event_type="agent.policy_blocked", actor_type="agent", actor_id="synthetic-reader", target_type="memory", target_id=str(memory["id"]), payload={"decision": {"decision": "blocked", "target_id": str(memory["id"])}})
            (hidden_ids if hidden else visible_ids).append(str(event["id"]))
        key = real_reader_key(store, user_id, reader)
    response = workspaces.get_vnext_workspace(user_id, authorization=f"Bearer {key}" if key else None)
    assert response.status_code == 200
    rendered = response.body.decode()
    assert all(identifier not in rendered for identifier in hidden_ids)
    assert all(identifier in rendered for identifier in visible_ids)
    assert "Cedar hidden" not in rendered
    body = json.loads(rendered)
    assert len(body["agent_activity"]["policy_blocks"]) == 1
    assert len(body["agent_activity"]["recent_commits"]) == 1
    assert len(body["agent_activity"]["inline_confirmations"]) == 1
    assert body["dogfooding"]["sample_scope"]["memories"]["total_count"] == 1


@pytest.mark.parametrize("reader", ("owner", "admin", "trusted"))
def test_telemetry_and_quality_ratings_use_current_target_labels(migrated_database_urls, monkeypatch, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    for module in (vnext_projects, vnext_review):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=app_url))
    hidden_ids = []
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Cedar hidden parent", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "confidential"})
        for hidden in (False, True):
            metadata = {"agent_id": "synthetic-reader"}
            if hidden:
                metadata["source_id"] = str(source["id"])
            derived = {"v": 1, "sources": [str(source["id"])] if hidden else [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [], "counts": {"sources": int(hidden), "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}}
            with without_insert_floor():
                memory = store.create_memory({"memory_key": f"telemetry-{hidden}", "canonical_text": "Cedar hidden telemetry" if hidden else "Public telemetry", "domain": "project", "sensitivity": "public", "metadata_json": metadata})
                artifact = store.create_artifact({"artifact_type": "daily_brief", "title": "Cedar hidden artifact" if hidden else "Public artifact", "content_markdown": "Public stored copy", "domain": "project", "sensitivity": "public", "metadata_json": {"agent_id": "synthetic-reader", "generated_by": "agent", "derived_from": derived}})
            rating = store.create_artifact_quality_rating({"artifact_id": str(artifact["id"]), "reviewer_id": "synthetic-reviewer", "verbosity": "right_sized", "comments": "Cedar hidden feedback" if hidden else "Public feedback"})
            append_event(store, event_type="agent.policy_blocked", actor_type="agent", actor_id="synthetic-reader", target_type="memory", target_id=str(memory["id"]), payload={})
            if hidden:
                hidden_ids.extend((str(artifact["id"]), str(rating["id"])))
        key = real_reader_key(store, user_id, reader)
    auth = f"Bearer {key}" if key else None
    telemetry = vnext_projects.get_vnext_agent_policy_telemetry(user_id, authorization=auth)
    summary = json.loads(telemetry.body)["summary"]
    admitted_count = 1 if reader == "trusted" else 2
    assert summary["total_agent_events"] == admitted_count
    assert summary["memory_proposals_by_agent"] == [{"agent_id": "synthetic-reader", "count": admitted_count}]
    assert summary["artifact_generation_by_agent"] == [{"agent_id": "synthetic-reader", "count": admitted_count}]
    ratings = vnext_review.list_vnext_quality_evals(user_id, authorization=auth)
    assert json.loads(ratings.body)["count"] == admitted_count
    if reader == "trusted":
        assert all(identifier not in ratings.body.decode() for identifier in hidden_ids)
        assert "Cedar hidden feedback" not in ratings.body.decode()
    else:
        assert all(identifier in ratings.body.decode() for identifier in hidden_ids)
