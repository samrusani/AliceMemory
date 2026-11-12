"""Key claim refusals carry a stable code, without exposing their text."""

import ast
from collections import Counter
from contextlib import nullcontext
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from typing import get_args

import pytest

from alicebot_api import mcp_server, vnext_agent_keys
from alicebot_api.mcp import policy, registry
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import AgentKeyAuthenticationError, hash_agent_key
from tests.unit.test_lessons_read import LESSON, Vault

MESSAGE = "The tool request could not be processed"
ROOT = Path(__file__).resolve().parents[2]


def wire(context, name, arguments):
    server = mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    result = server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    vault = Vault(tmp_path / "vault.db")
    key = vault.key("fixture-agent", "memory_proposal_agent", "alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    return vault, MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)


@pytest.mark.parametrize(("tool", "arguments"), [
    ("alice_memory_commit", {"lesson": LESSON}), ("alice_lessons", {"task": "reviewing a change"}),
    ("alice_recall", {}), ("alice_resume", {}),
])
@pytest.mark.parametrize("claim", [
    {"agent_id": "other-agent"},
    {"agent_identity": {"agent_id": "other-agent"}},
    {"permission_profile": "trusted_local_agent"},
    {"permission_profile": "not-a-profile"},
    {"project_scope": ["other-project"]},
    {"project_scope": ["alpha", "other-project"]},
    {"project_scope": [" "]},
])
def test_claim_refusals_use_the_fixed_identity_code(keyed, tool, arguments, claim):
    """Guard: classify agent, profile, scope and blank-scope claims by their typed reason."""
    _, context = keyed
    failed, payload = wire(context, tool, {**arguments, **claim})
    assert failed
    assert payload["error"] == {"code": "identity_claim_refused", "message": MESSAGE}


def test_keyless_invalid_key_and_accepted_claims_keep_their_answers(keyed, monkeypatch):
    """Guard: invalid keys must not be classified as identity claims."""
    vault, context = keyed
    failed, payload = wire(context, "alice_memory_commit", {"lesson": LESSON})
    assert not failed and payload["status"] == "review_required"
    for claim in ({}, {"agent_id": "fixture-agent"}, {"project_scope": ["alpha"]}, {"project_scope": []}):
        failed, payload = wire(context, "alice_lessons", {"task": "reviewing a change", **claim})
        assert not failed and payload["lessons"] == []
    failed, payload = wire(context, "alice_memory_commit", {"lesson": LESSON, "permission_profile": "read_only_agent"})
    assert not failed and payload["status"] == "rejected"
    failed, payload = wire(context, "alice_lessons", {"task": "reviewing a change", "project_scope": ["~global"]})
    assert failed and payload["error"]["code"] == "invalid_request"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", "alice_sk_not_issued")
    failed, payload = wire(context, "alice_lessons", {"task": "reviewing a change"})
    assert failed and payload["error"] == {"code": "not_permitted", "message": MESSAGE}
    revoked = vault.key("fixture-revoked", "read_only_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as connection:
        store = SQLiteVNextStore(connection, vault.user)
        record = store.get_agent_api_key_by_hash(hash_agent_key(revoked))
        store.revoke_agent_api_key(key_id=str(record["id"]))
    monkeypatch.setenv("ALICE_AGENT_API_KEY", revoked)
    failed, payload = wire(context, "alice_lessons", {"task": "reviewing a change"})
    assert failed and payload["error"] == {"code": "not_permitted", "message": MESSAGE}
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    failed, payload = wire(context, "alice_lessons", {"task": "reviewing a change"})
    assert failed and payload["error"] == {"code": "not_permitted", "message": MESSAGE}


def test_every_key_error_site_names_its_reason_and_both_mappings_use_only_claim_reasons(keyed, monkeypatch):
    """Guard: removing the reason at a key-error site is detected."""
    expected = Counter({"key_required": 2, "invalid_scope_type": 2, "invalid_key": 1, "wrong_user": 1,
                        "reserved_binding": 1, "agent_id_mismatch": 1, "profile_escalation": 1,
                        "invalid_binding": 1, "scope_escalation": 1, "blank_scope": 1,
                        "reserved_scope": 1, "reviewer_mismatch": 1})
    reasons = []
    for relative in ("apps/api/src/alicebot_api/vnext_agent_keys.py",
                     "apps/api/src/alicebot_api/routers/vnext_review.py"):
        for node in ast.walk(ast.parse((ROOT / relative).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentKeyAuthenticationError":
                fields = [item.value for item in node.keywords if item.arg == "reason"]
                assert len(fields) == 1 and isinstance(fields[0], ast.Constant), (relative, node.lineno)
                reasons.append(fields[0].value)
    assert Counter(reasons) == expected
    assert set(reasons) | {"other"} == set(get_args(vnext_agent_keys.AgentKeyFailureReason))
    reasons.append("other")
    _, context = keyed
    claims = {"agent_id_mismatch", "profile_escalation", "scope_escalation", "blank_scope"}
    for reason in reasons:
        expected_code = "identity_claim_refused" if reason in claims else "not_permitted"
        def refuse(*args, **kwargs):
            raise AgentKeyAuthenticationError("private detail", reason=reason)
        with monkeypatch.context() as patch:
            patch.setattr(policy, "_vnext_store_context", lambda _: nullcontext(None))
            patch.setattr(policy, "resolve_agent_identity", refuse)
            failed, answer = wire(context, "alice_lessons", {"task": "reviewing a change"})
            assert failed and answer["error"] == {"code": expected_code, "message": MESSAGE}
        with monkeypatch.context() as patch:
            patch.setitem(registry._TOOL_HANDLERS, "alice_lessons", refuse)
            failed, answer = wire(replace(context, agent_identity_resolved=True), "alice_lessons",
                                  {"task": "reviewing a change"})
            assert failed and answer["error"] == {"code": expected_code, "message": MESSAGE}


def test_core_identity_descriptions_explain_key_derived_fields():
    """Guard: restoring the old agent-id description hides that the key sets it."""
    from alicebot_api.mcp.definitions import _AGENT_IDENTITY_SCHEMA_PROPERTIES

    fields = _AGENT_IDENTITY_SCHEMA_PROPERTIES
    for name in ("agent_id", "agent_identity", "permission_profile", "project_scope"):
        text = fields[name]["description"].lower()
        assert "agent key" in text and "omit" in text and "key sets" in text, name
    assert "narrow" in fields["agent_identity"]["description"]
    assert "narrow" in fields["permission_profile"]["description"]
    assert "narrow" in fields["project_scope"]["description"]
    assert "keyless" in fields["permission_profile"]["description"]


def test_default_tools_list_stays_within_the_byte_budget(monkeypatch):
    """Guard: an oversized repeated identity description exceeds the default-list budget."""
    monkeypatch.setenv("ALICE_AGENT_API_KEY", "fixture-key")
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    context = MCPRuntimeContext(database_url="sqlite:///unused.db", user_id=__import__("uuid").uuid4())
    server = mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    result = server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    size = len(json.dumps(result, separators=(",", ":"), sort_keys=True).encode())
    assert size <= 20_202 + 1_000
