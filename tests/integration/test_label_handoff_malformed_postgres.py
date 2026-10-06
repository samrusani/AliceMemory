"""Corrupt recorded identifiers restrict reads without crashing PostgreSQL."""
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness


@pytest.mark.parametrize("key", ["source_refs", "derived_from", "source_id", "artifact_id"])
def test_non_uuid_record_does_not_crash_get_or_recall(label_harness, key):
    h = label_harness
    project = str(uuid4())
    if key == "source_refs":
        metadata = {key: ["meeting-notes"]}
    elif key == "derived_from":
        metadata = {key: {"v": 1, "sources": ["meeting-notes"], "memories": [], "artifacts": [], "open_loops": [], "beliefs": [],
                          "counts": {"sources": 1, "memories": 0, "artifacts": 0, "open_loops": 0, "beliefs": 0}}}
    else:
        metadata = {key: "meeting-notes"}
    with h.store() as store:
        store.create_project({"id": project, "name": "Alpha", "slug": "alpha"})
        artifact = store.create_artifact({"artifact_type": "daily_brief", "title": "Synthetic malformed report",
            "content_markdown": "Synthetic malformed report", "sensitivity": "public", "domain": "project",
            "metadata_json": {"workflow": "daily_brief", "project_scope": [project], **metadata}})
        memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Synthetic malformed memory",
            "status": "active", "sensitivity": "public", "domain": "project",
            "metadata_json": {"candidate_kind": "memory_rollup", "project_scope": [project], **metadata}})
        assert store.read_label_rows("source", ["meeting-notes"]) == []
    reader = h.key("trusted_local_agent")
    bound = h.key("admin_agent", project=project)
    owner = h.key("admin_agent")
    assert h.request("GET", f"/v0/vnext/artifacts/{artifact['id']}", key=owner)[0] == 200
    for key_value in (reader, bound):
        status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{artifact['id']}", key=key_value)
        # A free-text source_refs entry is not a dependency. The remaining
        # explicitly typed or canonical records cannot be resolved.
        if key != "source_refs":
            assert status == 403, body
            if key_value == bound:
                assert "derived_labels_unverified" in str(body), body
    if key != "source_refs":
        from alicebot_api.routers import vnext_memories
        from uuid import UUID
        response = vnext_memories.get_vnext_memory_audit(UUID(str(memory["id"])), h.user_id,
                                                       authorization=f"Bearer {bound}")
        assert response.status_code == 403, response.body
        assert b"derived_labels_unverified" in response.body
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    result = call_mcp_tool(context, name="alice_recall", arguments={"query": "Synthetic malformed", "agent_id": "synthetic-reader", "permission_profile": "read_only_agent"})
    if key != "source_refs":
        assert str(memory["id"]) not in str(result)
