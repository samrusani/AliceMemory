"""Keyless teams through the PostgreSQL tool and HTTP boundaries."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.keyless_team_contract import (
    LESSON, PROJECT, TRUSTED, check_implicit_local_profile, check_lesson_standing,
    check_proposal_identity_contract, http_door, mcp_door, profile_claims,
)
from tests.unit.test_identity_claim_codes import wire


@pytest.fixture(params=["mcp", "http"])
def call(label_harness, request):
    if request.param == "http":
        return http_door(label_harness)
    return mcp_door(MCPRuntimeContext(database_url=label_harness.urls["app"], user_id=label_harness.user_id))


@pytest.mark.parametrize("shape", ["flat", "agent_identity"])
@pytest.mark.parametrize("blank", ["omitted", None, "", "   "])
def test_postgres_default_uses_the_count_and_records_the_declared_writer(call, shape, blank):
    """Guard edit: use the original read-only fallback for an ordinary name."""
    check_implicit_local_profile(call, *profile_claims(shape, blank))


@pytest.mark.parametrize("claims", [TRUSTED, {**PROJECT, "project_scope": ["alpha"]}])
def test_postgres_keyless_writers_publish_and_retire(call, claims):
    """Guard edit: omit keyless publishing standing from the shared predicate."""
    check_lesson_standing(call, claims)


def test_http_proposal_keeps_its_identity_and_shape_requirements(label_harness):
    """Guard edit: apply the read-only fallback at the HTTP proposal route."""
    check_proposal_identity_contract(http_door(label_harness), missing="400", bad_lesson="422")


def test_keyless_reading_a_waiting_lesson_preserves_review_mutation_answers(label_harness):
    """Guard edit: make the reviewing caller the lesson's writer."""
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    failed, receipt = wire(context, "alice_memory_commit", {
        "lesson": LESSON, "sensitivity": "internal",
        "agent_id": "other-writer", "permission_profile": "memory_proposal_agent",
    })
    assert not failed and receipt["status"] == "review_required"
    memory_id = receipt["memory"]["id"]
    claims = {**TRUSTED, "agent_id": "local-reviewer"}
    failed, answer = wire(context, "alice_memory_review", {"review_item_id": memory_id, **claims})
    assert not failed and answer["review"]["memory"]["id"] == memory_id
    for action in ("approve", "reject"):
        failed, answer = wire(context, "alice_memory_correct", {
            "action": action, "review_item_id": memory_id, "reason": "A synthetic review", **claims,
        })
        assert failed and answer["error"]["code"] == "not_found", answer
    for action in ("accept", "reject", "edit"):
        status, answer, _ = h.request(
            "POST", f"/v0/vnext/memories/{memory_id}/review", payload={"action": action, **claims},
        )
        assert (status, answer) == (404, {"detail": "vNext memory was not found"})
    status, answer, _ = h.request("POST", "/v0/vnext/memories/redact", payload={
        "memory_id": memory_id, "reason": "A synthetic review", **claims,
    })
    assert (status, answer) == (404, {"detail": "vNext memory was not found"})


def test_browser_capability_uses_a_blank_profile_only_on_a_keyless_vault(label_harness):
    """Guard edit: keep the original empty field validation for capability captures."""
    h = label_harness
    origin = "https://fixture.example.invalid"
    key = None
    for expected in (201, 422):
        status, issued, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capabilities",
                                      payload={"origin": origin}, key=key)
        assert status == 201, issued
        status, receipt, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", origin=origin, payload={
            "url": f"{origin}/test-{expected}", "page_text": "A synthetic note on browser captures.",
            "capture_capability": issued["capability"], "sensitivity": "private",
            "agent_identity": {"agent_id": "clip-writer", "permission_profile": ""},
        })
        assert status == expected, receipt
        if key is None:
            key = h.key("admin_agent")


@pytest.mark.parametrize("shape", ["flat", "agent_identity"])
def test_browser_capture_records_the_inferred_profile_like_a_declared_one(label_harness, shape):
    """Guard edit: leave the inferred profile out of the stored identity envelope."""
    from alicebot_api.vnext_connectors import VNextConnectorService

    h = label_harness
    with h.store() as store:
        VNextConnectorService(store).update_config("browser_clipper", enabled=True)
    origin = "https://fixture.example.invalid"
    profiles = []
    for index, claims in enumerate(profile_claims(shape, "omitted")):
        status, issued, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capabilities",
                                      payload={"origin": origin})
        assert status == 201, issued
        status, receipt, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", origin=origin, payload={
            "url": f"{origin}/record-{index}", "page_text": f"Synthetic note about browser capture {index}.",
            "capture_capability": issued["capability"], "sensitivity": "private", **claims,
        })
        assert status == 201 and receipt["imported_count"] == 1, receipt
        with h.store() as store:
            row = store.get_source(receipt["source_ids"][0])
        payload = row["metadata_json"]["raw_payload"]
        identity = payload if shape == "flat" else payload[shape]
        profiles.append(identity["permission_profile"])
    assert profiles == ["trusted_local_agent", "trusted_local_agent"]
