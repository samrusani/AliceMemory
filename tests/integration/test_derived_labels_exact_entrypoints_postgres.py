"""Exact entrypoints use the shared effective project floor with real keys."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import MCPToolNotFoundError, call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.routers import vnext_memories, vnext_projects, vnext_retrieval, vnext_review
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_derived_labels_read_acceptance_postgres import _user
from tests.unit.test_derived_labels_real_keys import ALPHA, real_reader_key

DOORS = ("artifact_get", "artifact_trace", "artifact_export", "artifact_review", "artifact_feedback", "artifact_rating",
         "legacy_artifact_get", "legacy_artifact_review", "memory_review_http", "memory_review_mcp",
         "memory_correct_mcp", "memory_redact_mcp", "loop_review_http", "loop_review_mcp")

def _invoke(door, *, app_url, user_id, target_id, key, tmp_path):
    auth = f"Bearer {key}" if key else None
    if door == "artifact_get":
        return vnext_review.get_vnext_artifact(UUID(target_id), user_id, authorization=auth)
    if door == "artifact_trace":
        return vnext_retrieval.get_vnext_artifact_trace(UUID(target_id), user_id, authorization=auth)
    if door == "artifact_export":
        return vnext_review.export_vnext_artifact(UUID(target_id), vnext_review.VNextArtifactExportRequest(user_id=user_id, output_dir=str(tmp_path)), authorization=auth)
    if door == "artifact_review":
        return vnext_review.review_vnext_artifact(UUID(target_id), vnext_review.VNextArtifactReviewRequest(user_id=user_id, action="reject"), authorization=auth)
    if door == "artifact_feedback":
        return vnext_review.record_vnext_artifact_insight_feedback(UUID(target_id), vnext_review.VNextArtifactInsightFeedbackRequest(user_id=user_id, useful_insight="yes"), authorization=auth)
    if door == "artifact_rating":
        return vnext_review.rate_vnext_artifact_quality(UUID(target_id), vnext_review.VNextArtifactQualityRatingRequest(user_id=user_id), authorization=auth)
    if door == "memory_review_http":
        return vnext_memories.review_vnext_memory(UUID(target_id), vnext_memories.VNextMemoryReviewRequest(user_id=user_id, action="accept"), authorization=auth)
    if door == "loop_review_http":
        return vnext_projects.review_vnext_open_loop(target_id, vnext_projects.VNextOpenLoopReviewRequest(user_id=user_id, action="close"), authorization=auth)
    name, args = {
        "legacy_artifact_get": ("alice_vnext_artifact_get", {"artifact_id": target_id}),
        "legacy_artifact_review": ("alice_vnext_artifact_review", {"artifact_id": target_id, "action": "reject"}),
        "memory_review_mcp": ("alice_memory_review", {"review_item_id": target_id}),
        "memory_correct_mcp": ("alice_memory_correct", {"review_item_id": target_id, "action": "approve"}),
        "memory_redact_mcp": ("alice_memory_manage", {"memory_id": target_id, "action": "redact", "reason": "Synthetic test"}),
        "loop_review_mcp": ("alice_open_loops", {"loop_id": target_id, "action": "close"}),
    }[door]
    return call_mcp_tool(MCPRuntimeContext(database_url=app_url, user_id=user_id), name=name, arguments=args)

@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("reader", ("owner", "bound_admin"))
def test_exact_entrypoint_checks_effective_floor(migrated_database_urls, monkeypatch, tmp_path, door, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    for module in (vnext_memories, vnext_projects, vnext_retrieval, vnext_review):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=app_url))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        key = real_reader_key(store, user_id, reader)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    for hidden in (True, False):
        with user_connection(app_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            source = store.create_source({"source_type": "note", "title": "Parent", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["prj_" + "b" * 16 if hidden else ALPHA]}})
            metadata = {"source_id": str(source["id"]), "project_scope": [ALPHA]}
            derived = {"v": 1, "sources": [str(source["id"])], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [], "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}}
            with without_insert_floor():
                if "artifact" in door:
                    row = store.create_artifact({"artifact_type": "daily_brief", "title": "Door sentinel", "content_markdown": "Door sentinel", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA], "derived_from": derived}})
                elif door.startswith("loop_"):
                    row = store.create_open_loop({"title": "Door sentinel", "source_id": str(source["id"]), "domain": "project", "sensitivity": "public", "metadata_json": {**metadata, "discovered_by": "vnext_daily_capture"}})
                else:
                    row = store.create_memory({"memory_key": str(uuid4()), "title": "Door sentinel", "canonical_text": "Door sentinel", "status": "candidate", "domain": "project", "sensitivity": "public", "metadata_json": metadata})
        blocked = hidden and reader == "bound_admin"
        if door.startswith("legacy_") and key:
            with pytest.raises(MCPToolNotFoundError, match="disabled whenever"):
                _invoke(door, app_url=app_url, user_id=user_id, target_id=str(row["id"]), key=key, tmp_path=tmp_path)
            continue
        try:
            result = _invoke(door, app_url=app_url, user_id=user_id, target_id=str(row["id"]), key=key, tmp_path=tmp_path)
        except MCPToolError as exc:
            assert blocked, (door, reader, hidden, type(exc).__name__, str(exc))
            assert "policy" in type(exc).__name__.lower() or "project" in str(exc).lower(), (type(exc).__name__, str(exc))
        else:
            if hasattr(result, "status_code"):
                assert (result.status_code == 403) is blocked, (door, reader, hidden, result.status_code, result.body)
                if blocked:
                    assert str(row["id"]) not in result.body.decode()
                    assert "Door sentinel" not in result.body.decode()
                else:
                    assert result.status_code in {200, 201}, (door, result.status_code, result.body)
            else:
                assert not blocked, (door, reader, hidden, json.dumps(result, default=str))
                assert str(row["id"]) in json.dumps(result, default=str)


@pytest.mark.parametrize("reader", ("owner", "bound_admin"))
def test_review_queue_list_uses_current_parent_scope(migrated_database_urls, monkeypatch, reader):
    app_url = migrated_database_urls["app"]
    user_id = _user(app_url)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        key = real_reader_key(store, user_id, reader)
        rows = []
        for hidden in (False, True):
            source = store.create_source({"source_type": "note", "title": "Parent", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["prj_" + "b" * 16 if hidden else ALPHA]}})
            with without_insert_floor():
                rows.append(store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Queue sentinel", "status": "candidate", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA], "source_id": str(source["id"])}}))
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    result = call_mcp_tool(MCPRuntimeContext(database_url=app_url, user_id=user_id), name="alice_memory_review", arguments={"status": "all"})
    rendered = json.dumps(result, default=str)
    assert str(rows[0]["id"]) in rendered
    assert (str(rows[1]["id"]) in rendered) is (reader == "owner")
