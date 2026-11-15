"""Keyed identity claims through PostgreSQL MCP and HTTP."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire
from tests.identity_claims_cases import claim_cases


@pytest.fixture
def claims(label_harness, monkeypatch):
    h = label_harness
    keys = {}
    with h.store() as store:
        for name, profile, project in (("bound", "project_scoped_agent", "alpha"), ("admin", "admin_agent", None)):
            _, keys[name] = create_agent_key(store, user_id=h.user_id, agent_id=name,
                                             permission_profile=profile, project_scope=project)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    return h, keys


def _events(h):
    with h.store() as store:
        return [row for row in store.list_events() if row["event_type"] == "agent.key_escalation_rejected"]




def test_postgres_mcp_claim_table_uses_the_combined_source(claims, monkeypatch):
    """Guard: make the keyed identity source ignore flat fields."""
    h, keys = claims
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    for key, action, claim, expected, event in claim_cases():
        before = len(_events(h))
        monkeypatch.setenv("ALICE_AGENT_API_KEY", keys[key])
        name = "alice_recall" if action == "read" else "alice_memory_commit"
        fields = {"query": "example"} if action == "read" else {
            "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project", "sensitivity": "internal",
        }
        failed, result = wire(context, name, {**fields, **claim})
        answer = result["error"]["code"] if failed else result.get("status", "answered")
        assert answer == {"refused": "identity_claim_refused", "invalid": "invalid_request",
                          "empty": "answered", "partial": "rejected"}.get(expected, expected), (claim, result)
        assert len(_events(h)) == before + event, claim


def test_postgres_http_claim_table_preserves_validation_and_narrowing(claims):
    """Guard: disable acceptance of a partial nested identity after a keyed mixed claim."""
    h, keys = claims
    for key, action, claim, expected, event in claim_cases():
        before = len(_events(h))
        path = "/v0/vnext/context-packs" if action == "read" else "/v0/vnext/memories/commit"
        fields = {"query": "example"} if action == "read" else {
            "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project", "sensitivity": "internal",
        }
        status, result, _ = h.request("POST", path, key=keys[key], payload={**fields, **claim})
        wanted = {"refused": 403, "invalid": 400, "empty": 422, "partial": 422,
                  "answered": 201, "rejected": 200}[expected]
        assert status == wanted, (claim, status, result)
        if expected == "rejected":
            assert result["status"] == "rejected", result
        assert len(_events(h)) == before + event, claim
    # The alias reaches the same keyed source as agent_identity.
    status, _, _ = h.request("POST", "/v0/vnext/context-packs", key=keys["bound"], payload={
        "query": "example", "agent": {"agent_id": "other-alias"},
    })
    assert status == 403

    status, body, _ = h.request("POST", "/v0/vnext/memories/commit", key=keys["admin"], payload={
        "title": "A note", "canonical_text": "An ordinary procedure.", "domain": "project",
        "permission_profile": "admin_agent", "agent": {"agent_id": "admin", "permission_profile": "read_only_agent"},
    })
    assert status == 200 and body["status"] == "rejected"
    status, _, _ = h.request("POST", "/v0/vnext/context-packs", payload={"query": "example"})
    assert status == 401

    from uuid import uuid4
    from alicebot_api.db import user_connection
    from alicebot_api.store import ContinuityStore
    from tests.integration.derived_labels_postgres_support import invoke

    user = uuid4()
    with user_connection(h.urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"fixture-{user}", "Fixture")
    for claim, expected in (({"agent_identity": {}}, 422),
                            ({"agent_id": "declared", "agent_identity": {}}, 422),
                            ({"agent_id": "declared", "permission_profile": "not-a-profile"}, 400)):
        status, _, _ = invoke("POST", "/v0/vnext/context-packs", user_id=user,
                              payload={"user_id": str(user), "query": "example", **claim})
        assert status == expected, (claim, status)


def test_recorded_identity_defaults_stay_the_same_per_door_and_form(label_harness, monkeypatch):
    """Guard: omit the keyed HTTP nested-identity default before resolving claims."""
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    forms = ("none", "flat", "nested", "nested_project", "empty", "flat_run", "nested_run")
    for agent, mcp_type in (("openclaw", "coding_agent"), ("hermes", "personal_assistant"), ("fixture", "unknown")):
        with h.store() as store:
            _, key = create_agent_key(store, user_id=h.user_id, agent_id=agent,
                                      permission_profile="project_scoped_agent", project_scope="alpha")
        for form in forms:
            identity = {"none": {}, "flat": {"agent_id": agent}, "nested": {"agent_identity": {"agent_id": agent}},
                        "nested_project": {"agent_identity": {"agent_id": agent, "project_scope": ["alpha"]}},
                        "empty": {"agent_identity": {}},
                        "flat_run": {"agent_id": agent, "agent_run_id": "run-a", "task_id": "task-a"},
                        "nested_run": {"agent_identity": {"agent_id": agent, "agent_run_id": "run-a", "task_id": "task-a"}}}[form]
            fields = {"title": "A note", "canonical_text": "An ordinary procedure.",
                      "domain": "project", "sensitivity": "internal", **identity}
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
            failed, result = wire(context, "alice_memory_commit", fields)
            assert not failed and result["status"] == "committed", (agent, form, result)
            recorded = result["memory"]["metadata_json"]["agent_identity"]
            expected = {"agent_id": agent, "agent_type": mcp_type, "agent_run_id": "run-a" if form.endswith("_run") else None,
                        "task_id": "task-a" if form.endswith("_run") else None, "permission_profile": "project_scoped_agent",
                        "project_scope": ["alpha"], "project_scope_locked": True, "auth": "agent_api_key"}
            assert recorded == expected, (agent, form, recorded)
            status, result, _ = h.request("POST", "/v0/vnext/memories/commit", key=key, payload=fields)
            if form == "empty":
                assert status == 422, (agent, form, status, result)
                continue
            assert status == 201 and result["status"] == "committed", (agent, form, status, result)
            recorded = result["memory"]["metadata_json"]["agent_identity"]
            expected["agent_type"] = "unknown" if form in {"nested", "nested_project", "nested_run"} else mcp_type
            assert recorded == expected, (agent, form, recorded)


def test_postgres_malformed_identity_fields_do_not_mask_claims(claims, monkeypatch):
    """Guard: raise identity validation errors before checking the key's claims."""
    h, keys = claims
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", keys["bound"])
    for door in ("mcp", "http"):
        for claim in (
            {"agent_id": f"other-{door}", "project_scope": "wrong"},
            {"permission_profile": f"unknown-{door}", "project_scope": ["~global"]},
            {"agent_type": 123, "project_scope": [f"beta-{door}"]},
        ):
            before = len(_events(h))
            fields = {"query": "example", **claim}
            if door == "mcp":
                failed, body = wire(context, "alice_recall", fields)
                assert failed and body["error"]["code"] == "identity_claim_refused", (claim, body)
            else:
                status, body, _ = h.request("POST", "/v0/vnext/context-packs", key=keys["bound"], payload=fields)
                assert status == 403, (claim, status, body)
            assert len(_events(h)) == before + 1, claim
