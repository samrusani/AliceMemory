"""A lesson created before the first key keeps its original standing."""

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import LESSON


def test_postgres_lesson_written_without_keys_keeps_declared_writer(label_harness, monkeypatch):
    """The limitations page changes if a pre-key writer is treated as key verified.

    Mutation: replace the declared-on-keyless writer label with verified-by-key.
    """
    h = label_harness
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    failed, result = wire(context, "alice_memory_commit", {
        "lesson": LESSON, "agent_id": "declared", "permission_profile": "admin_agent", "project_scope": ["alpha"],
    })
    assert not failed and result["status"] == "committed", result
    memory_id = result["memory"]["id"]
    status, owner, _ = h.request("GET", f"/v0/vnext/memories/{memory_id}/audit")
    assert status == 200
    old_identity = owner["memory"]["metadata_json"]["agent_identity"]
    assert old_identity["auth"] == "unauthenticated_local"
    key = h.key("trusted_local_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    failed, lessons = wire(context, "alice_lessons", {"task": "rebasing"})
    assert not failed and lessons["lessons"][0]["writer"] == {
        "id": "declared", "established": "declared_on_keyless_install",
    }
    status, keyed, _ = h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=key)
    assert status == 200 and keyed["memory"]["metadata_json"]["agent_identity"] == old_identity


def test_postgres_memory_written_without_keys_keeps_declared_writer(label_harness, monkeypatch):
    """The limitations page changes if a pre-key write becomes a verified-key write.

    Mutation: replace the declared-on-keyless writer label with verified-by-key.
    """
    h = label_harness
    status, created, _ = h.request("POST", "/v0/vnext/memories/commit", payload={
        "agent_id": "old-agent", "permission_profile": "admin_agent", "project_scope": ["alpha"],
        "title": "Synthetic", "canonical_text": "An amber keyless memory", "domain": "project", "sensitivity": "internal",
    })
    assert status == 201 and created["status"] == "committed"
    key = h.key("read_only_agent", project="alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    failed, result = wire(context, "alice_recall", {"query": "amber keyless memory"})
    assert not failed
    [memory] = [item for item in result["results"] if str(item["id"]) == str(created["memory"]["id"])]
    assert memory["writer"] == {"id": "old-agent", "established": "declared_on_keyless_install"}
