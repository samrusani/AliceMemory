"""Commit requests and assertions shared by the SQLite and PostgreSQL tests."""

import pytest

from tests.support.keyless_team_contract import FACT, LESSON, PROJECT, PROPOSAL, TRUSTED
from tests.unit.test_identity_claim_codes import wire


OBSERVATION = {"source_type": "agent_observation"}
DIRECT = ("direct_user_instruction", "trusted_agent", "local_conversation")
OBSERVATIONS = ("agent_observation", "Agent_Observation", "AGENT_OBSERVATION", " agent_observation ")
OTHER = ("agent-observation", "agent_inference", "agent_output", "tool_output", "web_search")
OUTSIDE = ("web_page", "research", "generated_artifact")
BULK = {"source_refs": [f"fixture:{index}" for index in range(9)]}
RULE = {"rule": {"rule": "Read the full sample.", "reason": "It can be incomplete."}}
LABELED_RULE = {**RULE, **OBSERVATION}
PROMOTION_PERSONA = "personal"
BODIES = {"fact": FACT, "lesson": {"lesson": LESSON}}
CALLERS = {
    "unnamed": {},
    "implicit": {"agent_id": "fixture-writer"},
    "trusted": TRUSTED,
    "admin": {"agent_id": "fixture-admin", "permission_profile": "admin_agent"},
    "project": {**PROJECT, "project_scope": ["alpha"]},
    "proposal": PROPOSAL,
}
COMMITTED = ("committed", "explicit_trusted_memory_commit")
NON_DIRECT = ("review_required", "non_direct_source_requires_review")
EXTERNAL = ("review_required", "external_source_requires_review")
PROPOSAL_ONLY = ("review_required", "memory_proposal_agent_review_only")
APPROVAL = "lesson_requires_approval"

GATES = {
    "low": ({"confidence": 0.4}, ("review_required", "low_confidence_requires_review")),
    "medium": ({"confidence": 0.7}, ("confirmation_required", "medium_confidence_requires_confirmation")),
    "health": ({"domain": "health"}, ("confirmation_required", "sensitive_memory_requires_confirmation")),
    "confidential": ({"sensitivity": "confidential"}, ("confirmation_required", "sensitive_memory_requires_confirmation")),
    "bulk": (BULK, ("review_required", "bulk_source_refs_require_review")),
    "intent": ({"intent": "implicit"}, ("review_required", "explicit_memory_intent_required")),
    "contradiction": (
        {"contradiction_refs": ["A fixture disagrees."]}, ("confirmation_required", "contradiction_requires_confirmation"),
    ),
}


def matrix_cases(transport):
    for caller, claims in CALLERS.items():
        if transport == "http" and caller == "project":
            continue
        for kind, body in BODIES.items():
            args = {**body, "domain": "project", "sensitivity": "internal", **claims}
            sources = [(None, COMMITTED), ("", COMMITTED)]
            sources += [(label, COMMITTED) for label in (*DIRECT, *OBSERVATIONS)]
            sources += [(label, NON_DIRECT) for label in OTHER]
            sources += [(label, EXTERNAL) for label in OUTSIDE]
            for label, expected in sources:
                fields = {} if label is None else {"source_type": label}
                if label == "" and transport == "http":
                    expected = ("422", "string_too_short")
                elif caller == "proposal" and expected != EXTERNAL:
                    expected = PROPOSAL_ONLY
                case = "omitted" if label is None else "empty" if label == "" else label
                yield pytest.param(caller, kind, {**args, **fields}, expected, id=f"{caller}-{kind}-{case}")
            for gate, (fields, expected) in GATES.items():
                if (
                    (gate in {"intent", "contradiction"} and transport != "http")
                    or (gate == "health" and caller in {"project", "proposal"})
                    or (gate == "confidential" and caller != "unnamed")
                ):
                    continue
                if caller == "proposal" and gate not in {"bulk", "intent"}:
                    expected = PROPOSAL_ONLY
                for source, label in (("omitted", {}), ("observation", OBSERVATION)):
                    yield pytest.param(caller, kind, {**args, **fields, **label}, expected,
                                       id=f"{caller}-{kind}-{gate}-{source}")


def call_mcp(context, monkeypatch, action, payload, key=None):
    if key is None:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    if action == "confirm":
        payload = {**payload, "confirmation_action": "confirm"}
    failed, result = wire(context, "alice_memory_commit", payload)
    return result["error"]["code"] if failed else "ok", result


def call_http(harness, action, payload, key=None):
    if action == "confirm":
        payload = {**payload, "action": "confirm"}
    status, result, _ = harness.request("POST", f"/v0/vnext/memories/{action}", payload=payload, key=key)
    return "ok" if status in {200, 201} else str(status), result


def assert_policy(receipt, expected, *, lesson_waits=False):
    status, reason = expected
    reasons = [] if expected == COMMITTED else [reason]
    if lesson_waits:
        status, reason = "review_required", APPROVAL
        reasons.append(reason)
    assert receipt["status"] == status, receipt
    policy = receipt["policy_decision"]
    assert (policy["reason"], policy["reasons"]) == (reason, reasons), policy
    assert receipt["memory"]["status"] == {
        "committed": "active", "review_required": "candidate", "confirmation_required": "needs_review",
    }[status]


def check_matrix_cell(call, caller, kind, payload, expected):
    code, receipt = call("commit", payload)
    if expected[0] == "422":
        assert code == "422", receipt
        assert [(error["loc"], error["type"]) for error in receipt["detail"]] == [
            (["body", "source_type"], expected[1]),
        ]
        return
    assert code == "ok", receipt
    assert_policy(receipt, expected, lesson_waits=caller == "proposal" and kind == "lesson")
    if expected[0] == "confirmation_required":
        code, confirmed = call("confirm", {
            "confirmation_id": receipt["confirmation_id"], **CALLERS[caller],
        })
        assert code == "ok", confirmed
        assert confirmed["status"] == "committed" and confirmed["memory"]["status"] == "active", confirmed
        assert confirmed["memory"]["metadata_json"]["agentic_memory"]["source_type"] == (
            payload.get("source_type") or "direct_user_instruction"
        )


def check_keyed_cell(call, profile, kind, label):
    code, receipt = call("commit", {**BODIES[kind], "domain": "project", "project_scope": ["alpha"],
                                    "sensitivity": "internal", "source_type": label})
    expected = EXTERNAL if label == "web_page" else NON_DIRECT
    if profile == "memory_proposal_agent" and expected != EXTERNAL:
        expected = PROPOSAL_ONLY
    assert code == "ok", receipt
    assert_policy(receipt, expected, lesson_waits=kind == "lesson" and profile not in {"admin_agent", "bound-admin"})


def differing_fields(first, second, prefix=""):
    assert first.keys() == second.keys(), (prefix, first.keys(), second.keys())
    fields = set()
    for field, value in first.items():
        path = f"{prefix}{field}"
        if isinstance(value, dict) and isinstance(second[field], dict):
            fields.update(differing_fields(value, second[field], f"{path}."))
        elif value != second[field]:
            fields.add(path)
    return fields


def check_stored_pair(call, store_context, kind):
    rows = []
    for source in ({}, OBSERVATION):
        code, receipt = call("commit", {**BODIES[kind], **TRUSTED, **source})
        assert code == "ok", receipt
        assert_policy(receipt, COMMITTED)
        with store_context() as store:
            rows.append(store.get_memory(str(receipt["memory"]["id"])))
    control, observation = rows
    metadata = "metadata_json.agentic_memory."
    assert control["metadata_json"]["agentic_memory"]["source_type"] == "direct_user_instruction"
    assert observation["metadata_json"]["agentic_memory"]["source_type"] == "agent_observation"
    differences = differing_fields(control, observation)
    assert {metadata + "source_type", metadata + "request_fingerprint"} <= differences
    assert differences <= {
        "id", "memory_key", "created_at", "updated_at", "valid_from", "last_confirmed_at",
        "first_seen_at", "last_seen_at", "metadata_json.policy_decision.trace_id",
        metadata + "source_type", metadata + "request_fingerprint", metadata + "trace_id",
        metadata + "policy_decision.trace_id", metadata + "policy_decision.policy_decision.trace_id",
    }, differences


def check_promotion_fact(call, *, keyed):
    code, receipt = call("commit", {**FACT, **OBSERVATION})
    assert code == "ok", receipt
    assert_policy(receipt, NON_DIRECT if keyed else COMMITTED)
    promotion = receipt["policy_decision"]["promotion"]
    assert promotion["persona"] == "personal" and promotion["auto_promote"] is False
    assert promotion["hard_floor_rules_fired"] == []
    assert promotion["escalation_filters_fired"] == ["indirect_provenance"]
