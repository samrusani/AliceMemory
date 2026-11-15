"""Keyed identity claims use every supplied location."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    return Vault(tmp_path / "vault.db")


def _call(vault, monkeypatch, key, name, args):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    return wire(MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user), name, args)


def _events(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        return [row for row in SQLiteVNextStore(conn, vault.user).list_events()
                if row["event_type"] == "agent.key_escalation_rejected"]


def test_mixed_claims_are_checked_in_every_location(vault, monkeypatch):
    """Guard: make the keyed identity source ignore flat fields when a nested block exists."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    for claim in (
        {"agent_id": "other-one", "agent_identity": {}},
        {"agent_id": "other-two", "agent_identity": {"agent_id": "bound"}},
        {"permission_profile": "admin_agent", "agent_identity": {"agent_id": "bound"}},
        {"project_scope": ["beta"], "agent_identity": {}},
    ):
        before = len(_events(vault))
        failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", **claim})
        assert failed and result["error"]["code"] == "identity_claim_refused", result
        assert len(_events(vault)) == before + 1
    bound_admin = vault.key("bound-admin", "admin_agent", "alpha")
    failed, result = _call(vault, monkeypatch, bound_admin, "alice_recall", {
        "query": "example", "project_scope": ["alpha"], "agent_identity": {"agent_id": "bound-admin"},
    })
    assert not failed, result


def test_a_mixed_lower_profile_is_the_identity_used(vault, monkeypatch):
    """Guard: choose the highest claimed profile rather than the lowest."""
    key = vault.key("admin", "admin_agent")
    for claim in ({"permission_profile": "read_only_agent", "agent_identity": {}},
                  {"permission_profile": "admin_agent", "agent_identity": {"permission_profile": "read_only_agent"}}):
        failed, result = _call(vault, monkeypatch, key, "alice_memory_commit", {
            "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project", **claim,
        })
        assert not failed and result["status"] == "rejected", result
    from alicebot_api.vnext_agent_keys import resolve_agent_identity

    with sqlite_user_connection(vault.path, vault.user) as conn:
        identity = resolve_agent_identity(
            SQLiteVNextStore(conn, vault.user), user_id=vault.user, raw_key=key,
            payload={"project_scope": ["alpha", "beta"], "agent_identity": {"project_scope": ["beta"]}},
        )
    assert identity is not None and identity.project_scope == ("beta",)


def test_unknown_values_do_not_hide_a_refused_key_claim(vault, monkeypatch):
    """Guard: validate an identity before checking the key's grants."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    for claim in (
        {"agent_id": "other-one", "permission_profile": "not-a-profile"},
        {"agent_id": "other-two", "agent_type": "not-a-type"},
        {"agent_id": "bound", "permission_profile": "not-a-profile"},
        {"agent_id": "bound", "agent_type": "not-a-type", "project_scope": ["beta"]},
        {"agent_id": "bound", "agent_type": "not-a-type", "permission_profile": "trusted_local_agent"},
        {"agent_identity": {"agent_id": "bound", "agent_type": "not-a-type", "permission_profile": "admin_agent"}},
        {"agent_identity": {"agent_id": "bound", "permission_profile": "unknown-scope", "project_scope": ["beta-two"]}},
        {"agent_id": "other-blank-type", "agent_type": "not-a-type", "project_scope": [""]},
        {"agent_id": "other-blank-profile", "permission_profile": "not-a-profile", "project_scope": [""]},
        {"agent_id": "bound", "permission_profile": "unknown-with-blank", "project_scope": [""]},
    ):
        before = len(_events(vault))
        failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", **claim})
        assert failed and result["error"]["code"] == "identity_claim_refused", result
        assert len(_events(vault)) == before + 1
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {
        "query": "example", "agent_id": "bound", "agent_type": "not-a-type",
    })
    assert failed and result["error"]["code"] == "invalid_request"
    before = len(_events(vault))
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {
        "query": "example", "agent_id": "bound", "agent_type": "not-a-type", "project_scope": [""],
    })
    assert failed and result["error"]["code"] == "invalid_request"
    assert len(_events(vault)) == before


def test_the_checked_claim_and_running_identity_use_one_source(vault):
    """Guard: build the running identity from the original payload instead of the checked source."""
    from alicebot_api.vnext_agent_keys import resolve_agent_identity

    key = vault.key("admin", "admin_agent")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        identity = resolve_agent_identity(
            SQLiteVNextStore(conn, vault.user), user_id=vault.user, raw_key=key,
            payload={"agent_type": "coding_agent", "agent_identity": {"agent_id": "admin"}},
        )
    assert identity is not None and identity.agent_id == "admin" and identity.agent_type == "coding_agent"


def test_sqlite_mcp_claim_table_uses_the_combined_source(vault, monkeypatch):
    """Guard: make the keyed identity source ignore flat fields."""
    from tests.identity_claims_cases import claim_cases

    keys = {"bound": vault.key("bound", "project_scoped_agent", "alpha"), "admin": vault.key("admin", "admin_agent")}
    for key, action, claim, expected, event in claim_cases():
        before = len(_events(vault))
        name = "alice_recall" if action == "read" else "alice_memory_commit"
        fields = {"query": "example"} if action == "read" else {
            "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project", "sensitivity": "internal",
        }
        failed, result = _call(vault, monkeypatch, keys[key], name, {**fields, **claim})
        answer = result["error"]["code"] if failed else result.get("status", "answered")
        assert answer == {"refused": "identity_claim_refused", "invalid": "invalid_request",
                          "empty": "answered", "partial": "rejected"}.get(expected, expected), (claim, result)
        assert len(_events(vault)) == before + event, claim


def test_keyless_calls_keep_the_existing_source(vault, monkeypatch):
    """Guard: combine keyed sources for a keyless call too."""
    from alicebot_api.vnext_agent_keys import resolve_agent_identity

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    with sqlite_user_connection(vault.path, vault.user) as conn:
        identity = resolve_agent_identity(
            SQLiteVNextStore(conn, vault.user), user_id=vault.user, raw_key=None,
            payload={"agent_id": "declared", "agent_identity": {}},
        )
    assert identity is None
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    failed, result = wire(context, "alice_memory_commit", {
        "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project",
        "agent_id": "declared", "permission_profile": "project_scoped_agent", "project_scope": ["alpha"],
        "agent_identity": {},
    })
    assert not failed and result["status"] == "committed" and result["memory"]["created_by_agent_id"] is None
    vault.key("issued", "read_only_agent", "alpha")
    failed, result = wire(context, "alice_recall", {"query": "example"})
    assert failed and result["error"]["code"] == "not_permitted"


def test_mixed_narrowing_records_the_same_identity_as_one_form(vault, monkeypatch):
    """Guard: make the keyed identity source ignore flat fields beside a nested block."""
    key = vault.key("admin", "admin_agent")
    forms = (
        ({}, "admin_agent", []),
        ({"permission_profile": "project_scoped_agent", "project_scope": ["alpha"]}, "project_scoped_agent", ["alpha"]),
        ({"agent_identity": {"permission_profile": "project_scoped_agent", "project_scope": ["alpha"]}}, "project_scoped_agent", ["alpha"]),
        ({"permission_profile": "project_scoped_agent", "project_scope": ["alpha"], "agent_identity": {}}, "project_scoped_agent", ["alpha"]),
        ({"permission_profile": "project_scoped_agent", "project_scope": ["alpha"], "agent_identity": {"agent_id": "admin"}}, "project_scoped_agent", ["alpha"]),
    )
    for claim, profile, project in forms:
        failed, result = _call(vault, monkeypatch, key, "alice_memory_commit", {
            "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project", **claim,
        })
        assert not failed and result["status"] == "committed", (claim, result)
        recorded = result["memory"]["metadata_json"]["agent_identity"]
        assert recorded["permission_profile"] == profile and recorded["project_scope"] == project, (claim, recorded)


def test_malformed_fields_do_not_mask_key_claims(vault, monkeypatch):
    """Guard: remove the keyed claim preflight after an identity schema error."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    claims = (
        {"agent_id": "other-wrong-scope", "project_scope": "wrong"},
        {"agent_id": "other-reserved", "project_scope": ["~global"]},
        {"permission_profile": "admin_agent", "project_scope": "wrong"},
        {"permission_profile": "unknown-reserved", "project_scope": ["~global"]},
        {"agent_id": 123, "permission_profile": "trusted_local_agent"},
        {"agent_type": 123, "project_scope": ["beta-type"]},
        {"agent_id": "bound", "project_scope": ["beta-blank", ""]},
        {"agent_id": "bound", "permission_profile": "unknown-blank", "project_scope": [""]},
        {"agent_identity": {"agent_id": "other-nested", "project_scope": "wrong"}},
    )
    for claim in claims:
        before = len(_events(vault))
        failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", **claim})
        assert failed and result["error"]["code"] == "identity_claim_refused", (claim, result)
        assert len(_events(vault)) == before + 1, claim


def test_malformed_fields_without_overclaim_leave_key_and_events_unchanged(vault, monkeypatch):
    """Guard: remove the rollback after a non-escalating keyed preflight."""
    from alicebot_api.vnext_agent_keys import hash_agent_key

    key = vault.key("bound", "project_scoped_agent", "alpha")
    for claim in ({"project_scope": "wrong"}, {"project_scope": ["~global"]}):
        with sqlite_user_connection(vault.path, vault.user) as conn:
            before = SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(key))
        events_before = _events(vault)
        failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", **claim})
        assert failed and result["error"]["code"] == "invalid_request", result
        with sqlite_user_connection(vault.path, vault.user) as conn:
            after = SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(key))
        assert after == before and _events(vault) == events_before
