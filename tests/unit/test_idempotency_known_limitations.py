"""Idempotency keys share one vault-wide space across agent keys."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import hash_agent_key
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


@pytest.mark.parametrize("state", ["committed", "confirmation", "waiting", "rejected", "forgotten", "undone", "expired"])
@pytest.mark.parametrize("same_project", [True, False])
@pytest.mark.parametrize("other_text", [True, False])
def test_vault_wide_idempotency_replay_by_a_second_key(tmp_path, monkeypatch, state, same_project, other_text):
    """The limitations page changes with this replay behaviour.

    Mutation: make ``_idempotent_memory`` return None instead of the digest lookup result.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("first-admin", "admin_agent")
    writer = vault.key("first-writer", "memory_proposal_agent", "alpha") if state in {"waiting", "rejected"} else admin
    second = vault.key("second", "project_scoped_agent", "alpha" if same_project else "beta")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    args = {
        "title": "Synthetic fact", "canonical_text": "Synthetic original text", "domain": "project",
        "sensitivity": "internal", "project_scope": ["alpha"], "confidence": 0.7 if state == "confirmation" else 0.99,
        "idempotency_key": "synthetic-retry",
    }
    failed, first = call(writer, "alice_memory_commit", args)
    assert not failed, first
    memory_id = first["memory"]["id"]
    if state == "rejected":
        failed, answer = call(admin, "alice_memory_correct", {"action": "reject", "review_item_id": memory_id})
        assert not failed, answer
    if state in {"forgotten", "undone", "expired"}:
        action = {"forgotten": "forget", "undone": "undo", "expired": "expire"}[state]
        failed, answer = call(admin, "alice_memory_manage", {"action": action, "memory_id": memory_id, "reason": "Synthetic reason"})
        assert not failed, answer
    with sqlite_user_connection(vault.path, vault.user) as conn:
        tables = ("memories", "event_log", "agent_identities", "memory_revisions")
        before = {table: conn.execute(f"select count(*) as n from {table}").fetchone()["n"] for table in tables}
        assert SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(second))["last_used_at"] is None
    retry = {**args, "project_scope": ["alpha" if same_project else "beta"]}
    if other_text:
        retry["canonical_text"] = "Synthetic changed text"
    failed, result = call(second, "alice_memory_commit", retry)
    if not same_project or other_text:
        assert failed and result["error"]["code"] == "tool_request_failed", result
    else:
        expected = {"confirmation": "confirmation_required", "waiting": "review_required", "rejected": "review_required"}.get(state, "committed")
        assert not failed and result["status"] == expected, result
    with sqlite_user_connection(vault.path, vault.user) as conn:
        after = {table: conn.execute(f"select count(*) as n from {table}").fetchone()["n"] for table in tables}
        assert SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(second))["last_used_at"] is not None
    assert after["memories"] == before["memories"] and after["memory_revisions"] == before["memory_revisions"]
    events = 2 if same_project and not other_text else 0 if same_project else 3
    assert after["event_log"] - before["event_log"] == events
    assert after["agent_identities"] - before["agent_identities"] == (1 if events else 0)


@pytest.mark.parametrize("state", ["committed", "confirmation", "waiting", "rejected", "forgotten", "undone", "expired"])
def test_a_known_key_distinguishes_a_hidden_exact_write_from_other_text(tmp_path, monkeypatch, state):
    """The limitations page changes when hidden replay answers no longer differ.

    Mutation: skip the content comparison in the blocked replay path.
    """
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("first", "admin_agent")
    reader = vault.key("second", "trusted_local_agent")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, args):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, "alice_memory_commit", args)

    args = {"title": "Synthetic", "canonical_text": "Synthetic hidden text", "domain": "project",
            "sensitivity": "confidential", "project_scope": ["alpha"], "idempotency_key": "hidden-key",
            "source_type": "browser_clip" if state in {"waiting", "rejected"} else "direct_user_instruction"}
    failed, pending = call(admin, args)
    assert not failed and pending["status"] == ("review_required" if state in {"waiting", "rejected"} else "confirmation_required")
    memory_id = pending["memory"]["id"]
    if state in {"committed", "forgotten", "undone", "expired"}:
        failed, confirmed = call(admin, {"confirmation_id": pending["confirmation_id"], "confirmation_action": "confirm"})
        assert not failed and confirmed["status"] == "committed"
    if state in {"rejected", "forgotten", "undone", "expired"}:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
        if state == "rejected":
            failed, result = wire(context, "alice_memory_correct", {"action": "reject", "review_item_id": memory_id})
        else:
            action = {"forgotten": "forget", "undone": "undo", "expired": "expire"}[state]
            failed, result = wire(context, "alice_memory_manage", {"action": action, "memory_id": memory_id, "reason": "Synthetic"})
        assert not failed, result
    for text, code in ((args["canonical_text"], "not_permitted"), ("Different", "tool_request_failed")):
        with sqlite_user_connection(vault.path, vault.user) as conn:
            before = conn.execute("select count(*) as n from event_log").fetchone()["n"]
        failed, result = call(reader, {**args, "canonical_text": text})
        assert failed and result["error"]["code"] == code, result
        with sqlite_user_connection(vault.path, vault.user) as conn:
            assert conn.execute("select count(*) as n from event_log").fetchone()["n"] - before == 3
            assert SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(reader))["last_used_at"] is not None
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert conn.execute("select count(*) as n from memories").fetchone()["n"] == 1


def test_an_agent_can_claim_a_key_before_the_intended_writer(tmp_path, monkeypatch):
    """The limitations page changes if idempotency keys stop sharing one vault-wide space.

    Mutation: make ``_idempotent_memory`` return None instead of the digest lookup result.
    """
    vault = Vault(tmp_path / "vault.db")
    first = vault.key("first", "project_scoped_agent", "alpha")
    second = vault.key("second", "project_scoped_agent", "alpha")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, "alice_memory_commit", arguments)

    args = {"title": "Synthetic", "canonical_text": "First writer's text", "domain": "project",
            "sensitivity": "internal", "idempotency_key": "preempted-key"}
    failed, original = call(first, args)
    assert not failed and original["status"] == "committed" and not original.get("idempotent_replay", False)
    failed, replay = call(second, args)
    assert not failed and replay["status"] == "committed" and replay["idempotent_replay"]
    failed, answer = call(second, {**args, "canonical_text": "Second writer's text"})
    assert failed and answer["error"]["code"] == "tool_request_failed"
    failed, fresh = call(second, {**args, "canonical_text": "Second writer's text", "idempotency_key": "unused-key"})
    assert not failed and fresh["status"] == "committed" and not fresh.get("idempotent_replay", False)
