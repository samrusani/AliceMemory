"""Shared calls for the keyless team contract on SQLite and PostgreSQL."""

from tests.unit.test_identity_claim_codes import wire

LESSON = {
    "applies_when": "checking an amber fixture",
    "what_happened": "The sample was incomplete.",
    "do_instead": "Read the full sample.",
}
FACT = {"title": "Amber fixture", "canonical_text": "An amber fixture records an ordinary fact."}
TRUSTED = {"agent_id": "fixture-writer", "permission_profile": "trusted_local_agent"}
PROJECT = {"agent_id": "fixture-writer", "permission_profile": "project_scoped_agent"}
PROPOSAL = {"agent_id": "fixture-writer", "permission_profile": "memory_proposal_agent"}


def mcp_door(context):
    def call(action, arguments):
        names = {
            "commit": "alice_memory_commit",
            "recall": "alice_recall",
            "expire": "alice_memory_manage",
            "proposal": "alice_vnext_propose_memory",
            "review": "alice_memory_review",
            "lessons": "alice_lessons",
        }
        if action == "expire":
            arguments = {**arguments, "action": "expire"}
        failed, result = wire(context, names[action], arguments)
        return result.get("error", {}).get("code") if failed else None, result

    return call


def http_door(harness, key=None):
    def call(action, arguments):
        paths = {
            "commit": "memories/commit",
            "recall": "context-packs",
            "expire": "memories/expire",
            "proposal": "memory-proposals",
        }
        status, result, _ = harness.request("POST", f"/v0/vnext/{paths[action]}", payload=arguments, key=key)
        return str(status) if status >= 400 else None, result

    return call


def writer_record(result):
    return result["memory"]["metadata_json"]["agent_identity"]


def check_implicit_local_profile(call, implicit_claims, explicit_claims):
    _, explicit = call("commit", {**FACT, **explicit_claims})
    _, implicit = call("commit", {**FACT, **implicit_claims})
    assert explicit["status"] == implicit["status"] == "committed", implicit
    assert writer_record(explicit) == writer_record(implicit)
    assert writer_record(implicit)["permission_profile"] == "trusted_local_agent"
    assert writer_record(implicit)["agent_id"] == TRUSTED["agent_id"]
    assert writer_record(implicit)["auth"] == "unauthenticated_local"


def profile_claims(shape, blank, *, agent_type=None):
    claims = {"agent_id": TRUSTED["agent_id"]}
    if agent_type is not None:
        claims["agent_type"] = agent_type
    explicit = {**claims, "permission_profile": "trusted_local_agent"}
    if blank != "omitted":
        claims["permission_profile"] = blank
    if shape == "flat":
        return claims, explicit
    return {shape: claims}, {shape: explicit}


def check_lesson_standing(call, claims):
    _, published = call("commit", {"lesson": LESSON, "domain": "project", **claims})
    assert published["status"] == "committed", published
    assert published["memory"]["status"] == "active"
    assert published["policy_decision"]["reason"] == "explicit_trusted_memory_commit"
    error, retired = call(
        "expire", {"memory_id": published["memory"]["id"], "reason": "The fixture is complete.", **claims},
    )
    assert error is None and retired["status"] == "expired", retired


def check_proposal_identity_contract(call, *, missing, bad_lesson):
    assert call("proposal", FACT)[0] == missing
    assert call("proposal", {**FACT, **TRUSTED, "lesson": LESSON})[0] == bad_lesson
    for claims in (TRUSTED, {"agent_id": TRUSTED["agent_id"]}):
        error, result = call("proposal", {**FACT, **claims})
        assert error is None and result["review_required"] is True, result
        assert result["proposal"]["status"] == "candidate"


def check_marker_does_not_restore(identity):
    from alicebot_api.vnext_agent_control import AgentIdentity
    from alicebot_api.vnext_memory_commit import caller_may_publish_lesson

    rebuilt = AgentIdentity.from_payload(identity.to_record())
    assert rebuilt is not None and not caller_may_publish_lesson(rebuilt)
    assert not caller_may_publish_lesson(AgentIdentity(**TRUSTED))
    assert AgentIdentity.from_payload({"agent_id": "fixture-agent"}).permission_profile == "read_only_agent"
