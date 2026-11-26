"""Observation commits through PostgreSQL MCP and HTTP."""

import json
from uuid import uuid4

import pytest

from alicebot_api.cli.runner import main as cli_main
from alicebot_api.mcp import policy
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import _create_role_separated_database, _drop_database
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.keyless_observation_contract import (
    BODIES, BULK, COMMITTED, FACT, LABELED_RULE, NON_DIRECT, OBSERVATION, PROMOTION_PERSONA, RULE, assert_policy, call_http, call_mcp,
    check_keyed_cell, check_matrix_cell, check_promotion_fact, check_stored_pair, matrix_cases,
)
from tests.unit.test_identity_claim_codes import wire


@pytest.fixture(scope="module")
def migrated_database_urls(migrated_template_database):
    """Reuse the database while label_harness creates a fresh user per test."""
    name = f"alicebot_test_{uuid4().hex[:12]}"
    urls = _create_role_separated_database(name, template=migrated_template_database)
    try:
        yield urls
    finally:
        _drop_database(name)


def call_for(harness, monkeypatch, transport, key=None):
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    if transport == "http":
        return lambda action, args: call_http(harness, action, args, key)
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    return lambda action, args: call_mcp(context, monkeypatch, action, args, key)


@pytest.fixture(params=["mcp", "http"])
def transport(request):
    return request.param


@pytest.mark.parametrize(("caller", "kind", "payload", "expected"), list(matrix_cases("mcp")))
def test_postgres_mcp_observation_matrix(label_harness, monkeypatch, caller, kind, payload, expected):
    """Guard edit: submit the hyphenated spelling for the plain observation cell."""
    check_matrix_cell(call_for(label_harness, monkeypatch, "mcp"), caller, kind, payload, expected)


@pytest.mark.parametrize(("caller", "kind", "payload", "expected"), list(matrix_cases("http")))
def test_postgres_http_observation_matrix(label_harness, monkeypatch, caller, kind, payload, expected):
    """Guard edit: submit the hyphenated spelling for the plain observation cell."""
    check_matrix_cell(call_for(label_harness, monkeypatch, "http"), caller, kind, payload, expected)


@pytest.mark.parametrize("kind", BODIES)
@pytest.mark.parametrize("label", ["agent_observation", "agent_output", "web_page"])
@pytest.mark.parametrize("profile", ["admin_agent", "trusted_local_agent", "memory_proposal_agent", "bound-admin"])
def test_postgres_keyed_sources(label_harness, monkeypatch, transport, profile, kind, label):
    """Guard edit: use a direct source for the keyed observation cell."""
    key = (label_harness.key("admin_agent", project=["alpha"]) if profile == "bound-admin"
           else label_harness.key(profile))
    check_keyed_cell(call_for(label_harness, monkeypatch, transport, key), profile, kind, label)


@pytest.mark.parametrize("kind", BODIES)
def test_postgres_observation_stored_row(label_harness, monkeypatch, transport, kind):
    """Guard edit: use an uppercase observation in the observed row of the pair."""
    check_stored_pair(call_for(label_harness, monkeypatch, transport), label_harness.store, kind)


def test_postgres_rule_refuses_a_source_label(label_harness, monkeypatch, transport):
    """Guard edit: leave the source label out of the rule fixture."""
    code, receipt = call_for(label_harness, monkeypatch, transport)("commit", LABELED_RULE)
    if transport == "http":
        assert code == "422", receipt
        message = receipt["detail"][0]["msg"]
    else:
        assert code == "invalid_request", receipt
        message = receipt["error"]["message"]
    assert "source_type must be left out" in message


def test_postgres_first_key_and_last_revocation(label_harness, monkeypatch, transport):
    """Guard edit: use a direct source for the before and after requests."""
    h = label_harness
    args = {**FACT, **OBSERVATION}
    call = call_for(h, monkeypatch, transport)
    assert_policy(call("commit", args)[1], COMMITTED)
    with h.store() as store:
        row, key = create_agent_key(store, user_id=h.user_id, agent_id="fixture-admin", permission_profile="admin_agent")
    assert call("commit", args)[0] == ("401" if transport == "http" else "not_permitted")
    assert_policy(call_for(h, monkeypatch, transport, key)("commit", args)[1], NON_DIRECT)
    with h.store() as store:
        store.revoke_agent_api_key(key_id=str(row["id"]))
    assert_policy(call("commit", args)[1], COMMITTED)


@pytest.mark.parametrize("keyed", [False, True])
def test_postgres_observation_with_promotion(label_harness, monkeypatch, transport, keyed):
    """Guard edit: select the enterprise persona in this fixture."""
    monkeypatch.setenv("ALICE_MEMORY_PERSONA", PROMOTION_PERSONA)
    key = label_harness.key("admin_agent") if keyed else None
    check_promotion_fact(call_for(label_harness, monkeypatch, transport, key), keyed=keyed)


def test_postgres_mcp_key_count_error_keeps_the_refusal(label_harness, monkeypatch):
    """Guard edit: make the count stand-in return a successful zero."""
    def failed_count(_context):
        raise OSError("synthetic count failure")

    monkeypatch.setattr(policy, "_postgres_has_active_key", failed_count)
    code, receipt = call_for(label_harness, monkeypatch, "mcp")("commit", {**FACT, **OBSERVATION})
    assert code == "not_permitted"
    assert policy.MCPPostgresKeyRequiredError.public_message == receipt["error"]["message"]


def test_http_key_count_error_keeps_the_refusal(label_harness, monkeypatch):
    """Guard edit: make the count stand-in return a successful zero."""
    class CountError(Exception):
        pass

    def failed_count(_store):
        raise CountError("synthetic count failure")

    monkeypatch.setattr(PostgresVNextStore, "count_active_agent_api_keys", failed_count)
    with pytest.raises(CountError, match="synthetic count failure"):
        call_http(label_harness, "commit", {**FACT, **OBSERVATION})
    with label_harness.store() as store:
        assert store.conn.execute("SELECT count(*) AS total FROM memories").fetchone()["total"] == 0


@pytest.mark.parametrize(("transport", "fields", "reason"), [
    ("http", {"intent": "implicit"}, "explicit_memory_intent_required"),
    ("http", BULK, "bulk_source_refs_require_review"),
    ("mcp", BULK, "bulk_source_refs_require_review"),
], ids=["http-intent", "http-bulk", "mcp-bulk"])
def test_waiting_rule_under_promotion_has_no_promotion_decision(label_harness, monkeypatch, transport, fields, reason):
    """Guard edit: send the rule without the field that makes it wait."""
    monkeypatch.setenv("ALICE_MEMORY_PERSONA", PROMOTION_PERSONA)
    key = label_harness.key("admin_agent")
    code, receipt = call_for(label_harness, monkeypatch, transport, key)("commit", {**RULE, **fields})
    assert (code, receipt["status"], receipt["reason"]) == ("ok", "rejected", "rule_cannot_wait"), receipt
    assert receipt["reasons"] == [reason, "rule_cannot_wait"]
    assert "promotion" not in receipt["policy_decision"]
    with label_harness.store() as store:
        for table in ("memories", "memory_revisions", "provenance_links"):
            assert store.conn.execute(f"SELECT count(*) AS total FROM {table}").fetchone()["total"] == 0


def test_bound_admin_reads_a_matching_project_fact(label_harness, monkeypatch, transport):
    """Guard edit: ask for a different project in the matching-project read."""
    h = label_harness
    key = h.key("admin_agent", project=["alpha"])
    code, receipt = call_for(h, monkeypatch, transport, key)("commit", {
        **FACT, "domain": "project", "project_scope": ["alpha"], "sensitivity": "internal",
    })
    assert code == "ok", receipt
    assert_policy(receipt, COMMITTED)
    memory_id = str(receipt["memory"]["id"])
    query = "amber fixture"
    read_scope = ["alpha"]
    if transport == "http":
        status, body, _ = h.request("POST", "/v0/vnext/context-packs", payload={"query": query, "project_scope": read_scope}, key=key)
        assert status == 201, body
        results = body["relevant_memories"]
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        failed, body = wire(context, "alice_recall", {"query": query, "project_scope": read_scope})
        assert not failed, body
        results = body["results"]
    assert any(memory_id == str(result["id"]) for result in results), body


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("source_type", ["agent_observation", ""], ids=["observation", "empty"])
def test_cli_observation_keeps_waiting(label_harness, capsys, keyed, source_type):
    """Guard edit: pass a direct source to the command line."""
    h = label_harness
    if keyed:
        h.key("admin_agent")
    result = cli_main([
        "--database-url", h.urls["app"], "--user-id", str(h.user_id),
        "vnext", "memories", "commit", "--title", FACT["title"], "--text", FACT["canonical_text"],
        "--source-type", source_type,
    ])
    output = capsys.readouterr()
    if source_type:
        assert result == 0, output.err
        assert_policy(json.loads(output.out), NON_DIRECT)
    else:
        assert result == 1
        assert json.loads(output.err)["error"]["code"] == "invalid_request"
