"""A hidden memory and a missing one leave no event on a refused write."""

from uuid import uuid4

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_hidden_and_missing_memory_expiry_leave_no_event(tmp_path, monkeypatch):
    """The limitations page changes if a refused hidden write starts recording events.

    Mutation: skip the outside-caller-limits check in the expire path.
    """
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    admin, reader = vault.key("admin", "admin_agent"), vault.key("reader", "project_scoped_agent", "alpha")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, args):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, args)

    failed, created = call(admin, "alice_memory_commit", {
        "title": "Hidden", "canonical_text": "Hidden synthetic", "domain": "project",
        "sensitivity": "confidential", "project_scope": ["alpha"],
    })
    assert not failed and created["status"] == "confirmation_required"
    answers = []
    for memory_id in (created["memory"]["id"], str(uuid4())):
        with sqlite_user_connection(vault.path, vault.user) as connection:
            before = connection.execute("select count(*) as n from event_log").fetchone()["n"]
        failed, answer = call(reader, "alice_memory_manage", {"action": "expire", "memory_id": memory_id, "reason": "Synthetic"})
        assert failed
        answers.append(answer)
        with sqlite_user_connection(vault.path, vault.user) as connection:
            assert connection.execute("select count(*) as n from event_log").fetchone()["n"] == before
    assert answers[0] == answers[1] and answers[0]["error"]["code"] == "not_found"
