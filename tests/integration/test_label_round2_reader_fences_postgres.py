"""The two added reader doors preserve owner and admitted controls."""
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness


@pytest.mark.parametrize("profile,bound", [("trusted_local_agent", False), ("admin_agent", False), ("trusted_local_agent", True), ("admin_agent", True)])
def test_source_get_uses_the_callers_entire_fence(label_harness, profile, bound):
    h = label_harness
    alpha, beta = str(uuid4()), str(uuid4())
    with h.store() as store:
        for identifier in (alpha, beta):
            store.create_project({"id": identifier, "name": identifier, "slug": identifier})
    visible = h.source(scope=(alpha,))
    private = h.source(scope=(alpha,), sensitivity="confidential")
    other = h.source(scope=(beta,))
    # The owner's control precedes key provisioning, as the real key gate requires.
    for source in (visible, private, other):
        status, body, _ = h.request("GET", "/v0/vnext/sources/" + str(source["id"]))
        assert status == 200 and str(body["id"]) == str(source["id"])
    key = h.key(profile, project=alpha if bound else None)
    status, body, _ = h.request("GET", "/v0/vnext/sources/" + str(visible["id"]), key=key)
    assert status == 200 and str(body["id"]) == str(visible["id"])
    for source, permitted in ((private, profile == "admin_agent"), (other, not bound)):
        status, body, _ = h.request("GET", "/v0/vnext/sources/" + str(source["id"]), key=key)
        assert status == (200 if permitted else 404), (profile, bound, body)
        if not permitted:
            assert body == {"detail": f"vNext source {source['id']} was not found"}


@pytest.mark.parametrize("profile,nested", [("trusted_local_agent", False), ("trusted_local_agent", True), ("admin_agent", False), ("read_only_agent", True)])
def test_legacy_review_list_fences_effective_candidates(label_harness, profile, nested):
    h = label_harness
    alpha, beta = "round2-alpha", "round2-beta"
    hidden_source = h.source(scope=(alpha,), sensitivity="confidential")
    with h.store() as store:
        # Raw legacy copy: stored public, effective confidential.
        hidden = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden synthetic candidate",
                                      "status": "candidate", "domain": "project", "sensitivity": "public",
                                      "metadata_json": {"project_scope": [alpha]}})
        store.conn.execute("UPDATE memories SET metadata_json=metadata_json || %s::jsonb WHERE id=%s",
                           ('{"source_id":"' + str(hidden_source["id"]) + '"}', hidden["id"]))
        other = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Other synthetic candidate", "status": "candidate",
                                     "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [beta]}})
        visible = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Visible synthetic candidate", "status": "candidate",
                                       "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [alpha]}})
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    owner = call_mcp_tool(context, name="alice_vnext_review_items", arguments={"limit": 50})
    assert {str(row["id"]) for row in owner["items"]} == {str(row["id"]) for row in (visible, hidden, other)}
    identity = {"agent_id": "round2-reader", "permission_profile": profile}
    if nested:
        identity["project_scope"] = [alpha]
    arguments = {"agent_identity": identity} if nested else identity
    if nested:
        # Nested identity is a handler compatibility form, outside the flat
        # advertised tool schema. Exercise that boundary directly.
        from alicebot_api.mcp.evidence_artifacts import _handle_alice_vnext_review_items
        result = _handle_alice_vnext_review_items(context, {"limit": 50, **arguments})
    else:
        result = call_mcp_tool(context, name="alice_vnext_review_items", arguments={"limit": 50, **arguments})
    ids = {str(row["id"]) for row in result["items"]}
    expected = {str(visible["id"])}
    # A declared local scope is a request hint. Only resolved locked identity
    # imposes a key binding; the additional test below covers that form.
    expected.add(str(other["id"]))
    if profile == "admin_agent":
        expected.add(str(hidden["id"]))
    assert ids == expected, (profile, result)
    assert result["count"] == len(expected)


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent"])
def test_legacy_review_list_honors_resolved_locked_identity_and_refills(label_harness, profile):
    from dataclasses import replace
    from alicebot_api.vnext_agent_control import AgentIdentity
    h = label_harness
    alpha, beta = "round2-alpha", "round2-beta"
    with h.store() as store:
        visible = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Visible synthetic candidate", "status": "candidate",
                                       "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [alpha]}})
        for _ in range(55):
            store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Other synthetic candidate", "status": "candidate",
                                 "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [beta]}})
        # A stale public copy whose effective floor also needs beta.
        store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Floor-bound synthetic candidate", "status": "candidate",
                             "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [alpha], "project_floor": [beta]}})
    identity = AgentIdentity(agent_id="resolved-reader", permission_profile=profile, project_scope=(alpha,), project_scope_locked=True)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id, agent_identity=identity, agent_identity_resolved=True)
    result = call_mcp_tool(context, name="alice_vnext_review_items", arguments={"limit": 1})
    assert [str(row["id"]) for row in result["items"]] == [str(visible["id"])]
    assert result["count"] == 1
    owner = call_mcp_tool(replace(context, agent_identity=None), name="alice_vnext_review_items", arguments={"limit": 50})
    assert owner["count"] == 50
