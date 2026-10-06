"""Ordinary reviews share the label lock; label changes give a retryable refusal."""
import time

import pytest

from alicebot_api.cli.automation import _run_vnext_artifact_review
from alicebot_api.cli.models import CLIContext
from alicebot_api.config import Settings
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPInvalidRequestError
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from tests.integration.derived_labels_postgres_support import label_harness, today


@pytest.mark.parametrize("entry", ["memory", "artifact", "mcp_memory", "mcp_artifact", "project_reject"])
def test_non_label_review_succeeds_while_shared_label_lock_is_held(label_harness, entry):
    h = label_harness
    source = h.source()
    memory = h.memory(source=source)
    with h.store() as store:
        if entry == "project_reject":
            store.lock_graph_mutation()
            store.lock_label_writes(exclusive=True)
            project = store.create_project({"name": "Synthetic acceptance", "slug": "synthetic"})
            store.update_source(source_id=str(source["id"]), patch={"metadata_json": {"project_scope": [str(project["id"])]}})
            artifact = VNextProjectService(store).generate_project_update_candidate(ProjectAutomationRequest(agent_identity=None, project_id=str(project["id"])))
        else:
            artifact = VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(agent_identity=None, generated_for=today(), discover_open_loops=False))
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    with h.store() as holder:
        holder.lock_label_writes()
        started = time.monotonic()
        if entry == "memory":
            result = h.request("POST", f"/v0/vnext/memories/{memory['id']}/review", payload={"action": "accept"})
            assert result[0] == 200, result
        elif entry == "mcp_memory":
            result = call_mcp_tool(context, name="alice_memory_correct", arguments={"review_item_id": str(memory["id"]), "action": "approve"})
        elif entry == "mcp_artifact":
            result = call_mcp_tool(context, name="alice_vnext_artifact_review", arguments={"artifact_id": str(artifact["id"]), "action": "accept"})
        else:
            path = f"/v0/vnext/projects/update-candidates/{artifact['id']}/review" if entry == "project_reject" else f"/v0/vnext/artifacts/{artifact['id']}/review"
            result = h.request("POST", path, payload={"action": "reject" if entry == "project_reject" else "accept"})
            assert result[0] == 200, result
        assert time.monotonic() - started < 2.0
        # Keep the shared grant live beyond the exclusive wait bound. The
        # action above completed while that grant still existed.
        time.sleep(max(0, 3.1 - (time.monotonic() - started)))


@pytest.mark.parametrize("entry", ["memory", "artifact", "project", "mcp_project"])
def test_label_changing_review_times_out_with_retry_after(label_harness, entry):
    h = label_harness
    source = h.source()
    memory = h.memory(source=source)
    with h.store() as store:
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        project = store.create_project({"name": "Synthetic acceptance", "slug": "synthetic"})
        store.update_source(source_id=str(source["id"]), patch={"metadata_json": {"project_scope": [str(project["id"])]}})
        artifact = VNextProjectService(store).generate_project_update_candidate(ProjectAutomationRequest(agent_identity=None, project_id=str(project["id"])))
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    before = h.snapshot()
    with h.store() as holder:
        holder.lock_label_writes()
        started = time.monotonic()
        if entry == "mcp_project":
            with pytest.raises(MCPInvalidRequestError, match="HTTP 503; Retry-After: 2"):
                call_mcp_tool(context, name="alice_project_update_review", arguments={"artifact_id": str(artifact["id"]), "action": "accept"})
        else:
            path = f"/v0/vnext/memories/{memory['id']}/review" if entry == "memory" else (
                f"/v0/vnext/artifacts/{artifact['id']}/review" if entry == "artifact" else f"/v0/vnext/projects/update-candidates/{artifact['id']}/review")
            payload = {"action": "edit", "sensitivity": "confidential"} if entry == "memory" else {"action": "accept"}
            status, body, headers = h.request("POST", path, payload=payload)
            assert status == 503, body
            assert headers[b"retry-after"] == b"2"
            assert "nothing was changed" in body["detail"]
        assert 2.5 <= time.monotonic() - started < 4.0
    assert h.snapshot() == before
