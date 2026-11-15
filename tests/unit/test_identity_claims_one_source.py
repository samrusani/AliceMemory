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


def test_invalid_key_with_malformed_identity_keeps_its_answer(vault, monkeypatch):
    """Guard: skip the deferred identity error when a key is invalid or revoked."""
    from alicebot_api.vnext_agent_keys import hash_agent_key

    revoked = vault.key("revoked", "read_only_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        record = store.get_agent_api_key_by_hash(hash_agent_key(revoked))
        store.revoke_agent_api_key(key_id=str(record["id"]))
    for key in ("alice_sk_never_issued", revoked):
        before = _events(vault)
        failed, result = _call(vault, monkeypatch, key, "alice_recall", {
            "query": "example", "agent_id": "declared", "permission_profile": "not-a-profile",
        })
        assert failed and result["error"]["code"] == "invalid_request", result
        assert _events(vault) == before


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
    """Guard: commit a non-escalating keyed preflight instead of rolling it back."""
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


def _assert_new_refusal(vault, monkeypatch, key, claim, reason, claimed_field, claimed_value):
    before = {event["id"] for event in _events(vault)}
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", **claim})
    assert failed and result["error"]["code"] == "identity_claim_refused", result
    [event] = [event for event in _events(vault) if event["id"] not in before]
    payload = event["payload_json"]
    assert payload["reason"] == reason and payload[claimed_field] == claimed_value, payload
    return payload


def test_profile_precedes_scope_in_the_recorded_refusal(vault, monkeypatch):
    """Guard: check the project scope before the profile."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    payload = _assert_new_refusal(vault, monkeypatch, key,
        {"permission_profile": "admin_agent", "project_scope": ["beta-priority"]},
        "permission_profile_escalation", "claimed_permission_profile", "admin_agent")
    assert "claimed_project_scope" not in payload
    _assert_new_refusal(vault, monkeypatch, key,
        {"agent_id": "other-priority", "permission_profile": "trusted_local_agent", "project_scope": ["beta-id"]},
        "agent_id_mismatch", "claimed_agent_id", "other-priority")
    _assert_new_refusal(vault, monkeypatch, key,
        {"permission_profile": "read_only_agent", "project_scope": ["beta-scope"]},
        "project_scope_escalation", "claimed_project_scope", ["beta-scope"])


def test_nested_agent_id_is_checked_beside_own_flat_id(vault, monkeypatch):
    """Guard: select the first agent ID instead of a different claimed ID."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    _assert_new_refusal(vault, monkeypatch, key,
        {"agent_id": "bound", "agent_identity": {"agent_id": "other-nested-id"}},
        "agent_id_mismatch", "claimed_agent_id", "other-nested-id")


def test_nested_profile_is_checked_beside_a_lower_flat_profile(vault, monkeypatch):
    """Guard: select the lowest profile without first checking for escalation."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    for profile in ("admin_agent", "unknown-nested-profile"):
        _assert_new_refusal(vault, monkeypatch, key,
            {"permission_profile": "read_only_agent", "agent_identity": {"permission_profile": profile}},
            "permission_profile_escalation", "claimed_permission_profile", profile)


def test_nested_scope_is_checked_beside_the_bound_flat_scope(vault, monkeypatch):
    """Guard: check only the first supplied project scope."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    _assert_new_refusal(vault, monkeypatch, key,
        {"project_scope": ["alpha"], "agent_identity": {"project_scope": ["beta-nested"]}},
        "project_scope_escalation", "claimed_project_scope", ["alpha", "beta-nested"])


def test_disagreeing_run_ids_are_refused(vault, monkeypatch):
    """Guard: ignore conflicts between non-permission identity fields."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    before = _events(vault)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {
        "query": "example", "agent_run_id": "run-flat",
        "agent_identity": {"agent_id": "bound", "agent_run_id": "run-nested"},
    })
    assert failed and result["error"]["code"] == "invalid_request", result
    assert _events(vault) == before


def test_nonidentity_argument_errors_do_not_hide_key_claims(vault, monkeypatch):
    """Guard: skip claim preflight after a core tool's schema argument error."""
    from alicebot_api.vnext_agent_keys import hash_agent_key

    key = vault.key("bound", "project_scoped_agent", "alpha")
    for field, value, agent in (("limit", "abc", "other-limit"),
                                ("not_a_parameter", True, "other-unknown")):
        _assert_new_refusal(vault, monkeypatch, key, {field: value, "agent_id": agent},
                            "agent_id_mismatch", "claimed_agent_id", agent)
        before = _events(vault)
        with sqlite_user_connection(vault.path, vault.user) as conn:
            key_before = SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(key))
        for raw_key in (key, "alice_sk_never_issued"):
            failed, result = _call(vault, monkeypatch, raw_key, "alice_recall", {
                "query": "example", field: value, "agent_id": "bound",
            })
            assert failed and result["error"]["code"] == "invalid_request", result
        failed, result = _call(vault, monkeypatch, "alice_sk_never_issued", "alice_recall", {
            "query": "example", field: value, "agent_id": "other-invalid-key",
        })
        assert failed and result["error"]["code"] == "invalid_request", result
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
        failed, result = wire(context, "alice_recall", {"query": "example", field: value, "agent_id": "other-keyless"})
        assert failed and result["error"]["code"] == "invalid_request", result
        with sqlite_user_connection(vault.path, vault.user) as conn:
            key_after = SQLiteVNextStore(conn, vault.user).get_agent_api_key_by_hash(hash_agent_key(key))
        assert key_after == key_before
        assert _events(vault) == before


def test_nonidentity_reserved_scope_does_not_hide_key_claim(vault, monkeypatch):
    """Guard: skip claim preflight after a reserved project argument error."""
    key = vault.key("bound", "project_scoped_agent", "alpha")
    _assert_new_refusal(vault, monkeypatch, key,
        {"agent_id": "other-reserved-project", "projects": ["~global"]},
        "agent_id_mismatch", "claimed_agent_id", "other-reserved-project")
    before = _events(vault)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {
        "query": "example", "agent_id": "bound", "projects": ["~global"],
    })
    assert failed and result["error"]["code"] == "invalid_request", result
    assert _events(vault) == before


def test_argument_error_wording_does_not_choose_claim_preflight(vault, monkeypatch):
    """Guard: skip claim preflight after a core tool's schema argument error."""
    from alicebot_api.mcp import registry
    from alicebot_api.mcp.types import MCPArgumentError

    key = vault.key("bound", "project_scoped_agent", "alpha")

    def invalid(*args, **kwargs):
        raise MCPArgumentError("a diagnostic without a field name")

    monkeypatch.setattr(registry, "_validate_mcp_arguments_against_advertised_schema", invalid)
    _assert_new_refusal(vault, monkeypatch, key, {"agent_id": "other-neutral-message"},
                        "agent_id_mismatch", "claimed_agent_id", "other-neutral-message")
    before = _events(vault)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", "agent_id": "bound"})
    assert failed and result["error"]["code"] == "invalid_request", result
    assert _events(vault) == before


def test_unstorable_argument_does_not_hide_a_key_claim(vault, monkeypatch):
    """Guard: skip claim preflight after the JSON bounds check rejects arguments."""
    from alicebot_api.json_bounds import MAX_STORED_JSON_DEPTH
    from alicebot_api.mcp import registry

    definition = dict(registry._TOOL_DEFINITIONS_BY_NAME["alice_recall"])
    definition["inputSchema"] = {
        "type": "object", "additionalProperties": False,
        "properties": {"query": {"type": "string"}, "agent_id": {"type": "string"}, "data": {"type": "object"}},
    }
    monkeypatch.setitem(registry._TOOL_DEFINITIONS_BY_NAME, "alice_recall", definition)
    deep = {}
    for _ in range(MAX_STORED_JSON_DEPTH):
        deep = {"nested": deep}
    key = vault.key("bound", "project_scoped_agent", "alpha")
    _assert_new_refusal(vault, monkeypatch, key, {"agent_id": "other-deep", "data": deep},
                        "agent_id_mismatch", "claimed_agent_id", "other-deep")
    before = _events(vault)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", {"query": "example", "agent_id": "bound", "data": deep})
    assert failed and result["error"]["code"] == "invalid_request", result
    assert _events(vault) == before


def test_nonobject_argument_error_runs_claim_preflight(vault, monkeypatch):
    """Guard: skip claim preflight after the argument object is rejected."""
    from alicebot_api.mcp import registry

    key = vault.key("bound", "project_scoped_agent", "alpha")
    observed = []
    original = registry._preflight_key_claim

    def record(context, arguments):
        observed.append(arguments)
        return original(context, arguments)

    monkeypatch.setattr(registry, "_preflight_key_claim", record)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", [])
    assert failed and result["error"]["code"] == "invalid_request", result
    assert observed == [{}] and _events(vault) == []


def test_nonobject_argument_error_needs_no_store(vault, monkeypatch):
    """Guard: attempt a store lookup when an argument object contains no fields."""
    from alicebot_api.mcp import policy

    key = vault.key("bound", "project_scoped_agent", "alpha")

    def unavailable(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(policy, "_vnext_store_context", unavailable)
    failed, result = _call(vault, monkeypatch, key, "alice_recall", [])
    assert failed and result["error"]["code"] == "invalid_request", result
