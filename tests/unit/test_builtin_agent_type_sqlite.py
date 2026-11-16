"""Built-in keyed agent ids get their MCP defaults on SQLite."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


@pytest.mark.parametrize(("agent_id", "expected"), [
    ("openclaw", "coding_agent"), ("hermes", "personal_assistant"), ("other", "unknown"),
])
def test_sqlite_keyed_mcp_records_builtin_agent_type(tmp_path, monkeypatch, agent_id, expected):
    """The limitations page changes if MCP no longer records the built-in default.

    Mutation: make the ``openclaw`` default agent type ``unknown``.
    """
    vault = Vault(tmp_path / "vault.db")
    key = vault.key(agent_id, "project_scoped_agent", "alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    failed, result = wire(context, "alice_memory_commit", {
        "title": "Synthetic", "canonical_text": "Synthetic fact", "domain": "project", "sensitivity": "internal",
    })
    assert not failed and result["status"] == "committed", result
    assert result["memory"]["metadata_json"]["agent_identity"]["agent_type"] == expected
