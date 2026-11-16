"""Some memory audit records carry less identity than their neighbours."""

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_sqlite_redaction_event_and_replacement_identity(tmp_path, monkeypatch):
    """The limitations page changes when the redaction event names its actor.

    Mutation: add an actor id to the SQLite memory.redacted receipt.
    """
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("admin", "admin_agent")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    failed, narrow = call(admin, "alice_memory_commit", {
        "title": "Narrow", "canonical_text": "Narrow note", "domain": "project", "sensitivity": "internal",
        "permission_profile": "trusted_local_agent",
    })
    assert not failed and narrow["status"] == "committed"
    with sqlite_user_connection(vault.path, vault.user) as connection:
        agents = SQLiteVNextStore(connection, vault.user).list_agent_identities()
        assert next(row for row in agents if row["agent_id"] == "admin")["permission_profile"] == "trusted_local_agent"
    failed, original = call(writer, "alice_memory_commit", {
        "title": "Synthetic", "canonical_text": "Synthetic original", "domain": "project", "sensitivity": "internal",
    })
    assert not failed and original["status"] == "committed"
    memory_id = original["memory"]["id"]
    failed, correction = call(admin, "alice_memory_correct", {
        "action": "supersede-existing", "review_item_id": memory_id, "replacement_title": "Replacement",
        "replacement_body": {"text": "New synthetic text"},
    })
    assert not failed and correction["replacement_object"]["metadata_json"]["agent_identity"]["agent_id"] == "writer"
    failed, result = call(admin, "alice_memory_manage", {"action": "redact", "memory_id": memory_id, "reason": "Synthetic"})
    assert not failed, result
    with sqlite_user_connection(vault.path, vault.user) as connection:
        events = SQLiteVNextStore(connection, vault.user).list_events(target_type="memory", target_id=memory_id)
    [redacted] = [event for event in events if event["event_type"] == "memory.redacted"]
    assert redacted["actor_type"] == "agent" and redacted["actor_id"] is None
    assert any(event.get("actor_id") == "admin" for event in events)
