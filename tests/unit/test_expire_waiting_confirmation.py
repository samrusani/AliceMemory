"""A temporal expiry does not cancel a waiting inline confirmation."""

from datetime import UTC, datetime

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_expired_waiting_write_can_be_confirmed_but_is_not_recalled(tmp_path, monkeypatch):
    """The limitations page changes when expiry or confirmation changes.

    Mutation: replace ``valid_to_iso = _valid_to_iso(valid_to)`` with a far-future date in expire.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    key = vault.key("author", "project_scoped_agent", "alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    failed, pending = wire(context, "alice_memory_commit", {
        "title": "amber windmill", "canonical_text": "The amber windmill is adjacent to the orchard.",
        "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], "confidence": 0.7,
    })
    assert not failed and pending["status"] == "confirmation_required"
    memory_id, confirmation_id = pending["memory"]["id"], pending["confirmation_id"]
    failed, expired = wire(context, "alice_memory_manage", {
        "action": "expire", "memory_id": memory_id, "reason": "Synthetic expiry",
    })
    assert not failed and expired["status"] == "expired"
    failed, confirmed = wire(context, "alice_memory_commit", {
        "confirmation_id": confirmation_id, "confirmation_action": "confirm",
    })
    assert not failed and confirmed["status"] == "committed"
    row = confirmed["memory"]
    assert row["status"] == "active"
    assert datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00")) < datetime.now(UTC)
    failed, recall = wire(context, "alice_recall", {"query": "amber windmill orchard"})
    assert not failed and all(item["id"] != memory_id for item in recall["results"])
    failed, pack = wire(context, "alice_context_pack", {"query": "amber windmill orchard"})
    assert not failed and all(item["id"] != memory_id for item in pack["memories"])
    assert any(item.get("target_id") == memory_id for item in pack["recent_changes"])
