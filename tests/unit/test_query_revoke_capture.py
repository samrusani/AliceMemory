"""Query events, key revocation and capture ceilings through the SQLite doors."""

import json
import os
import subprocess
import sys

import pytest

from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return Vault(tmp_path / "vault.db")


def _events(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        return SQLiteVNextStore(conn, vault.user).list_events()


def _call(vault, monkeypatch, key, name, arguments):
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    else:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return wire(MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user), name, arguments)


def _query_events(vault, monkeypatch, event_type):
    from alicebot_api.onramp import main

    key = vault.key("reader", "read_only_agent")
    credential = "AK" + "IA" + "23456789ABCDEFGH"
    queries = [("x" * 19000, "x" * 128), ("a\u0007b\u200bc", "abc"),
               ("request " + credential, "[withheld]"), ("x" * 129 + " " + credential, "[withheld]")]
    for query, expected in queries:
        before = {str(event["id"]) for event in _events(vault)}
        failed, answer = _call(vault, monkeypatch, key, "alice_context_pack", {"query": query})
        assert not failed, answer
        rows = [event for event in _events(vault) if str(event["id"]) not in before
                and event["event_type"] == event_type]
        assert len(rows) == 1
        assert rows[0]["payload_json"]["query"] == expected
    export = vault.path.parent / "export.jsonl"
    assert main(["export", "--db", str(vault.path), "--user-id", str(vault.user), "--out", str(export)]) == 0
    assert credential not in export.read_text()


def test_compiled_event_stores_a_clean_bounded_query(vault, monkeypatch):
    """Guard: store the raw query in the compiled event."""
    _query_events(vault, monkeypatch, "retrieval.context_pack_compiled")


def test_agent_request_event_stores_a_clean_bounded_query(vault, monkeypatch):
    """Guard: store the raw query in the agent request event."""
    _query_events(vault, monkeypatch, "agent.context_pack_requested")


def _make_old_keys(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        old = [create_agent_key(store, user_id=vault.user, agent_id=f"old-{n}",
                                permission_profile="read_only_agent") for n in range(2)]
        for n in range(205):
            create_agent_key(store, user_id=vault.user, agent_id=f"new-{n}", permission_profile="read_only_agent")
    return old


def _revoke(vault, selector):
    env = dict(os.environ)
    env.pop("ALICE_AGENT_API_KEY", None)
    return subprocess.run([sys.executable, "-m", "alicebot_api.onramp", "keys", "revoke", selector,
                           "--db", str(vault.path), "--user-id", str(vault.user)],
                          capture_output=True, text=True, env=env, check=False)


def test_sqlite_revoke_finds_older_key_by_id_and_prefix(vault):
    """Guard: limit the revocation search to the newest 200 keys."""
    old = _make_old_keys(vault)
    for selector in (str(old[0][0]["id"]), str(old[1][0]["key_prefix"])):
        result = _revoke(vault, selector)
        assert result.returncode == 0, result.stdout + result.stderr


def test_sqlite_unlimited_key_list_reaches_old_revocations(vault):
    """Guard: cap a request for all SQLite keys at 200."""
    old = _make_old_keys(vault)
    result = _revoke(vault, str(old[0][0]["id"]))
    assert result.returncode == 0, result.stdout + result.stderr


def test_sqlite_revoke_accepts_a_unique_partial_prefix(vault):
    """Guard: require the entire stored prefix rather than a unique prefix."""
    with sqlite_user_connection(vault.path, vault.user) as conn:
        record, _ = create_agent_key(SQLiteVNextStore(conn, vault.user), user_id=vault.user,
                                     agent_id="reader", permission_profile="read_only_agent")
    result = _revoke(vault, str(record["key_prefix"])[:-1])
    assert result.returncode == 0, result.stdout + result.stderr


def test_sqlite_prefix_ambiguity_counts_revoked_keys(vault):
    """Guard: remove revoked keys from the ambiguity check."""
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        first, _ = create_agent_key(store, user_id=vault.user, agent_id="first", permission_profile="read_only_agent")
        create_agent_key(store, user_id=vault.user, agent_id="second", permission_profile="read_only_agent")
        store.revoke_agent_api_key(key_id=str(first["id"]))
    result = _revoke(vault, "alice_sk_")
    assert result.returncode != 0 and "several keys" in result.stderr + result.stdout


def test_sqlite_capture_refuses_above_ceiling_and_preserves_allowed(vault, monkeypatch):
    """Guard: disable the capture sensitivity ceiling on the MCP door."""
    failed, result = _call(vault, monkeypatch, None, "alice_capture", {
        "raw_text": "Owner fixture source.", "domain": "project", "sensitivity": "regulated"})
    assert not failed and result["status"] == "imported", result
    keys = [(vault.key("project", "project_scoped_agent", "alpha"), "regulated"),
            (vault.key("trusted", "trusted_local_agent"), "confidential")]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        before_sources = conn.execute("SELECT count(*) AS total FROM sources").fetchone()["total"]
    for index, (key, level) in enumerate(keys):
        before = {str(event["id"]) for event in _events(vault)}
        failed, result = _call(vault, monkeypatch, key, "alice_capture", {
            "raw_text": f"Above ceiling fixture {index}.", "domain": "project", "sensitivity": level})
        assert failed and result["error"]["code"] == "not_permitted", result
        new = [event for event in _events(vault) if str(event["id"]) not in before]
        assert {event["event_type"] for event in new} == {
            "agent.identity_upserted", "policy.decision", "agent.policy_blocked"}
        assert any("sensitivity_above_agent_ceiling" in event["payload_json"].get("reasons", [])
                   or "sensitivity_above_agent_ceiling" in json.dumps(event["payload_json"]) for event in new)
    failed, result = _call(vault, monkeypatch, keys[0][0], "alice_capture", {
        "raw_text": "Restricted domain fixture.", "domain": "health", "sensitivity": "private"})
    assert failed and result["error"]["code"] == "not_permitted", result
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert conn.execute("SELECT count(*) AS total FROM sources").fetchone()["total"] == before_sources
    for name, profile, project, level in (("limited", "project_scoped_agent", "alpha", "private"),
                                           ("admin", "admin_agent", None, "regulated"),
                                           ("bound-admin", "admin_agent", "alpha", "regulated")):
        key = vault.key(name, profile, project)
        failed, answer = _call(vault, monkeypatch, key, "alice_capture", {
            "raw_text": f"Allowed {name} fixture.", "domain": "project", "sensitivity": level})
        assert not failed and answer["status"] == "imported", answer


def test_sqlite_legacy_agent_output_refuses_above_ceiling(vault, monkeypatch):
    """Guard: disable the ceiling on the legacy agent-output tool."""
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    failed, answer = _call(vault, monkeypatch, None, "alice_vnext_ingest_agent_output", {
        "agent_id": "writer", "permission_profile": "project_scoped_agent", "project_scope": ["alpha"],
        "title": "Output", "content": "A synthetic output.", "domain": "project", "sensitivity": "regulated"})
    assert failed and answer["error"]["code"] == "not_permitted", answer
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert conn.execute("SELECT count(*) AS total FROM sources").fetchone()["total"] == 0
