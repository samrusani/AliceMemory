"""Observation commits through the default SQLite MCP tool."""

from contextlib import contextmanager
from uuid import uuid4

import pytest

from alicebot_api.mcp import policy
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from tests.support.keyless_observation_contract import (
    BODIES, COMMITTED, FACT, LABELED_RULE, NON_DIRECT, OBSERVATION, PROMOTION_PERSONA, TRUSTED, assert_policy, call_mcp,
    check_keyed_cell, check_matrix_cell, check_promotion_fact, check_stored_pair, matrix_cases,
)
from tests.unit.test_lessons_read import Vault


@pytest.fixture
def sqlite_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    for name in ("ALICE_AGENT_API_KEY", "ALICE_MCP_FULL_TOOLS", "ALICE_MEMORY_PERSONA"):
        monkeypatch.delenv(name, raising=False)
    vault = Vault(tmp_path / "vault.db")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    @contextmanager
    def store_context():
        with sqlite_user_connection(vault.path, vault.user) as conn:
            yield SQLiteVNextStore(conn, vault.user)

    def call(action, payload, key=None):
        return call_mcp(context, monkeypatch, action, payload, key)

    return vault, context, store_context, call


@pytest.mark.parametrize(("caller", "kind", "payload", "expected"), list(matrix_cases("mcp")))
def test_sqlite_observation_matrix(sqlite_observations, caller, kind, payload, expected):
    """Guard edit: submit the hyphenated spelling for the plain observation cell."""
    check_matrix_cell(sqlite_observations[3], caller, kind, payload, expected)


@pytest.mark.parametrize("kind", BODIES)
@pytest.mark.parametrize("label", ["agent_observation", "agent_output", "web_page"])
@pytest.mark.parametrize("profile", ["admin_agent", "trusted_local_agent", "memory_proposal_agent", "bound-admin"])
def test_sqlite_keyed_sources(sqlite_observations, profile, kind, label):
    """Guard edit: use a direct source for the keyed observation cell."""
    vault, _, _, call = sqlite_observations
    key = vault.key("fixture-writer", "admin_agent" if profile == "bound-admin" else profile,
                    "alpha" if profile == "bound-admin" else None)
    check_keyed_cell(lambda action, args: call(action, args, key), profile, kind, label)


@pytest.mark.parametrize("kind", BODIES)
def test_sqlite_observation_stored_row(sqlite_observations, kind):
    """Guard edit: use an uppercase observation in the observed row of the pair."""
    _, _, store_context, call = sqlite_observations
    check_stored_pair(call, store_context, kind)


def test_sqlite_rule_refuses_a_source_label(sqlite_observations):
    """Guard edit: leave the source label out of the rule fixture."""
    error, receipt = sqlite_observations[3]("commit", LABELED_RULE)
    assert error == "invalid_request"
    assert "source_type must be left out" in receipt["error"]["message"]


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("empty_file", [False, True])
def test_sqlite_observation_without_a_key_table(tmp_path, monkeypatch, claimed, empty_file):
    """Guard edit: bootstrap a key table before submitting the uncounted request."""
    path = tmp_path / "uncounted.db"
    if empty_file:
        path.touch()
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=uuid4())
    error, receipt = call_mcp(context, monkeypatch, "commit", {**FACT, **OBSERVATION, **(TRUSTED if claimed else {})})
    assert error == "ok", receipt
    assert_policy(receipt, NON_DIRECT)


@pytest.mark.parametrize("claimed", [False, True])
def test_sqlite_service_without_a_key_count(sqlite_observations, claimed):
    """Guard edit: supply a verified zero count to the unnamed service call."""
    vault, _, store_context, _ = sqlite_observations
    request = memory_commit_request_from_payload({**FACT, **OBSERVATION}, user_id=vault.user)
    with store_context() as store:
        identity = AgentIdentity(**TRUSTED) if claimed else None
        receipt = VNextMemoryCommitService(store).commit(identity=identity, request=request)
        assert_policy(receipt, NON_DIRECT)


def test_sqlite_key_count_error_keeps_the_refusal(sqlite_observations, monkeypatch):
    """Guard edit: make the count stand-in return a successful zero."""
    def failed_count(_path):
        raise OSError("synthetic count failure")

    monkeypatch.setattr(policy, "_vault_has_active_key", failed_count)
    error, receipt = sqlite_observations[3]("commit", {**FACT, **OBSERVATION})
    assert error == "not_permitted"
    assert receipt["error"]["message"] == "The tool request could not be processed"


def test_sqlite_first_key_and_last_revocation(sqlite_observations):
    """Guard edit: use a direct source for the before and after requests."""
    vault, _, store_context, call = sqlite_observations
    args = {**FACT, **OBSERVATION}
    assert_policy(call("commit", args)[1], COMMITTED)
    with store_context() as store:
        row, key = create_agent_key(store, user_id=vault.user, agent_id="fixture-admin", permission_profile="admin_agent")
    assert call("commit", args)[0] == "not_permitted"
    assert_policy(call("commit", args, key)[1], NON_DIRECT)
    with store_context() as store:
        store.revoke_agent_api_key(key_id=str(row["id"]))
    assert_policy(call("commit", args)[1], COMMITTED)


@pytest.mark.parametrize("keyed", [False, True])
def test_sqlite_observation_with_promotion(sqlite_observations, monkeypatch, keyed):
    """Guard edit: select the enterprise persona in this fixture."""
    vault, _, _, call = sqlite_observations
    monkeypatch.setenv("ALICE_MEMORY_PERSONA", PROMOTION_PERSONA)
    key = vault.key("fixture-admin", "admin_agent") if keyed else None
    check_promotion_fact(lambda action, args: call(action, args, key), keyed=keyed)
