"""Some PostgreSQL memory audit records carry less identity than their neighbours."""

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire


def test_postgres_redaction_event_and_replacement_identity(label_harness, monkeypatch):
    """The limitations page changes when the redaction event names its actor.

    Mutation: add an actor id to the PostgreSQL memory.redacted receipt.
    """
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    with h.store() as store:
        admin = create_agent_key(store, user_id=h.user_id, agent_id="admin", permission_profile="admin_agent")[1]
        writer = create_agent_key(store, user_id=h.user_id, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha")[1]
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    status, result, _ = h.request("POST", "/v0/vnext/memories/commit", key=admin, payload={
        "title": "Narrow", "canonical_text": "Narrow note", "domain": "project", "sensitivity": "internal",
        "permission_profile": "trusted_local_agent",
    })
    assert status == 201 and result["status"] == "committed"
    with h.store() as store:
        assert next(row for row in store.list_agent_identities() if row["agent_id"] == "admin")["permission_profile"] == "trusted_local_agent"
    status, original, _ = h.request("POST", "/v0/vnext/memories/commit", key=writer, payload={
        "title": "Synthetic", "canonical_text": "Synthetic original", "domain": "project", "sensitivity": "internal",
    })
    assert status == 201 and original["status"] == "committed"
    memory_id = original["memory"]["id"]
    failed, correction = call(admin, "alice_memory_correct", {
        "action": "supersede-existing", "review_item_id": memory_id, "replacement_title": "Replacement",
        "replacement_body": {"text": "New synthetic text"},
    })
    assert not failed and correction["replacement_object"]["metadata_json"]["agent_identity"]["agent_id"] == "writer"
    status, result, _ = h.request("POST", "/v0/vnext/memories/redact", key=admin, payload={
        "memory_id": memory_id, "reason": "Synthetic",
    })
    assert status == 200, result
    with h.store() as store:
        events = store.list_events(target_type="memory", target_id=memory_id)
    [redacted] = [event for event in events if event["event_type"] == "memory.redacted"]
    assert redacted["actor_type"] == "agent" and redacted["actor_id"] is None
    assert any(event.get("actor_id") == "admin" for event in events)
