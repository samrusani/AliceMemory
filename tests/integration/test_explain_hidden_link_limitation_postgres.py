"""A readable memory can have an unreadable source that makes explain fail."""

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire


def test_postgres_explain_of_readable_memory_with_hidden_source_fails(label_harness, monkeypatch):
    """The limitations page changes if explain no longer refuses the hidden source.

    Mutation: skip the linked source authorization in ``_authorize_memory_audit_provenance``.
    """
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    admin, reader = h.key("admin_agent"), h.key("trusted_local_agent")
    source = h.source(scope=["alpha"], sensitivity="confidential")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    status, created, _ = h.request("POST", "/v0/vnext/memories/commit", key=admin, payload={
        "title": "Visible memory", "canonical_text": "Synthetic visible memory", "domain": "project",
        "sensitivity": "internal", "project_scope": ["alpha"], "source_refs": [str(source["id"])],
    })
    assert status == 201 and created["status"] == "committed"
    memory_id = created["memory"]["id"]
    status, audit, _ = h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=reader)
    assert status == 200 and str(audit["memory"]["id"]) == str(memory_id)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    failed, answer = wire(context, "alice_explain", {"memory_id": memory_id})
    assert failed and answer["error"]["code"] == "tool_request_failed"
