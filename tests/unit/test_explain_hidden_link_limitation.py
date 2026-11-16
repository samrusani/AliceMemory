"""A readable memory can have an unreadable source that makes explain fail."""

from uuid import uuid4

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_explain_of_readable_memory_with_hidden_source_fails(tmp_path, monkeypatch):
    """The limitations page changes if explain no longer refuses the hidden source.

    Mutation: skip the linked source authorization in ``_authorize_memory_audit_provenance``.
    """
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    admin, reader = vault.key("admin", "admin_agent"), vault.key("reader", "project_scoped_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as connection:
        source = SQLiteVNextStore(connection, vault.user).create_source({
            "source_type": "note", "title": "Hidden source", "content_hash": str(uuid4()),
            "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": ["alpha"]},
        })
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    failed, created = call(admin, "alice_memory_commit", {
        "title": "Visible memory", "canonical_text": "Synthetic visible memory", "domain": "project",
        "sensitivity": "internal", "project_scope": ["alpha"], "source_refs": [str(source["id"])],
    })
    assert not failed and created["status"] == "committed"
    memory_id = created["memory"]["id"]
    failed, recall = call(reader, "alice_recall", {"query": "Synthetic visible memory"})
    assert not failed and memory_id in [item["id"] for item in recall["results"]]
    failed, answer = call(reader, "alice_explain", {"memory_id": memory_id})
    assert failed and answer["error"]["code"] == "tool_request_failed"
