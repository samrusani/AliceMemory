"""Both legacy dashboard aliases honor a declared reader's ceiling."""
import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.vnext_derived_labels import with_derived_from
from tests.integration.derived_labels_postgres_support import label_harness


@pytest.mark.parametrize("tool", ["alice_vnext_project_dashboard", "alice_project_dashboard"])
@pytest.mark.parametrize("profile", ["read_only_agent", "trusted_local_agent"])
def test_legacy_dashboard_refuses_confidential_derived_project(label_harness, tool, profile):
    h = label_harness
    source = h.source(sensitivity="confidential")
    with h.store() as store:
        project = store.create_project({"name": "Synthetic secret project", "slug": "synthetic",
            "domain": "project", "sensitivity": "public",
            "metadata_json": with_derived_from({}, {"sources": [source]})})
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    owner = call_mcp_tool(context, name=tool, arguments={"project_id": str(project["id"])})
    assert "Synthetic secret project" in str(owner)
    with pytest.raises(MCPToolError):
        call_mcp_tool(context, name=tool, arguments={"project_id": str(project["id"]),
            "agent_id": "synthetic-reader", "permission_profile": profile})
