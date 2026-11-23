"""Waiting memory correction through SQLite MCP and the correction service."""

from collections import Counter
from copy import deepcopy

import pytest

from alicebot_api.vnext_agent_control import AgentPolicyBlockedError
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_protected_agent_identity
from alicebot_api.vnext_memory_commit import PENDING_WRITE_RESOLVER_REASON, VNextMemoryCommitService
from tests.unit.test_mcp_refusal_is_independent_of_state import _store_do, _wire, context  # noqa: F401


@pytest.fixture(autouse=True)
def legacy_surface(context, monkeypatch):
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")


def _seed(context, state, writer):
    error, response = _wire(context, "alice_vnext_commit_memory", {
        "title": "Test orchard hours",
        "canonical_text": "The test orchard opens on Saturday.",
        "domain": "project",
        "sensitivity": "internal",
        "confidence": 0.7 if state in {"pending", "late"} else 0.96,
        "source_type": "browser_clip" if state == "candidate" else "direct_user_instruction",
        **writer,
    })
    assert not error, response
    row = response["memory"]
    assert row["status"] == {"candidate": "candidate", "pending": "needs_review", "late": "needs_review"}.get(state, "active")
    if state == "late":
        metadata = deepcopy(row["metadata_json"])
        metadata["agentic_memory"]["confirmation"]["expires_at"] = "2000-01-01T00:00:00Z"
        _store_do(context, lambda store: store.update_memory(memory_id=row["id"], patch={"metadata_json": metadata}))
    return row["id"], response.get("confirmation_id")


def _state(context, memory_id):
    def snapshot(store):
        vector = store.conn.execute("SELECT embedding FROM memories WHERE id = ?", (memory_id,)).fetchone()["embedding"]
        return store.get_memory(memory_id), store.list_revisions(memory_id), vector

    return _store_do(context, snapshot)


def _events(context):
    return _store_do(context, lambda store: {event["id"]: event for event in store.list_events(limit=100)})


def _correct(context, memory_id, claims):
    return _wire(context, "alice_vnext_correct_memory", {
        "memory_id": memory_id, "canonical_text": "The test orchard opens on Sunday.", **claims,
    })


@pytest.mark.parametrize(("state", "name", "profile", "reason"), [
    ("candidate", "peer", "trusted_local_agent", PENDING_WRITE_RESOLVER_REASON),
    ("candidate", "writer", "trusted_local_agent", PENDING_WRITE_RESOLVER_REASON),
    ("candidate", "writer", "project_scoped_agent", PENDING_WRITE_RESOLVER_REASON),
    ("pending", "peer", "trusted_local_agent", PENDING_WRITE_RESOLVER_REASON),
    ("late", "writer", "trusted_local_agent", "confirmation_expired"),
])
def test_sqlite_waiting_correction_preserves_the_row_when_refused(context, state, name, profile, reason):
    scope = {"project_scope": ["orchard"]} if profile == "project_scoped_agent" else {}
    memory_id, _ = _seed(context, state, {"agent_id": "writer", "permission_profile": profile, **scope})
    stored = _state(context, memory_id)
    before = _events(context)
    error, response = _correct(context, memory_id, {"agent_id": name, "permission_profile": profile, **scope})
    assert error and response["error"]["code"] == "not_permitted", response
    assert _state(context, memory_id) == stored
    events = [event for event_id, event in _events(context).items() if event_id not in before]
    assert Counter(event["event_type"] for event in events) == {
        "agent.identity_upserted": 1, "policy.decision": 1, "agent.policy_blocked": 1,
    }
    assert {event["actor_id"] for event in events} == {name}
    for event in events:
        if event["event_type"] != "agent.identity_upserted":
            assert event["target_id"] == memory_id
            assert event["payload_json"]["policy_decision"]["reasons"] == [reason]


@pytest.mark.parametrize(("state", "role"), [
    ("candidate", "owner"), ("candidate", "admin_agent"), ("pending", "owner"),
    ("pending", "writer"), ("late", "owner"), ("late", "admin_agent"), ("active", "peer"),
])
def test_sqlite_allowed_correction_resolves_the_record(context, state, role):
    writer = {"agent_id": "writer", "permission_profile": "trusted_local_agent"}
    memory_id, confirmation_id = _seed(context, state, writer)
    claims = {
        "owner": {}, "writer": writer, "admin_agent": {"agent_id": "reviewer", "permission_profile": "admin_agent"},
        "peer": {"agent_id": "peer", "permission_profile": "trusted_local_agent"},
    }[role]
    error, result = _correct(context, memory_id, claims)
    assert not error and result["status"] == "committed", result
    row, revisions, _ = _state(context, memory_id)
    assert (row["status"], row["confirmation_status"]) == ("active", "confirmed")
    assert any(revision["action"] == "agentic_memory_correct" for revision in revisions)
    if confirmation_id is None:
        return
    record = row["metadata_json"]["agentic_memory"]["confirmation"]
    assert record["status"] == "confirmed" and ("confirmed_at" in record) is (role != "writer")
    waiting = _store_do(context, lambda store: VNextMemoryCommitService(store).inline_confirmations())
    assert all(item["id"] != memory_id for item in waiting)


@pytest.mark.parametrize("caller", ["peer", "writer", "project-peer", "declared-peer", "owner", "admin"])
def test_sqlite_private_only_correction_requires_review_standing(context, caller):
    project = "orchard" if caller == "project-peer" else None
    writer = {"agent_id": "writer", "permission_profile": "trusted_local_agent"}
    if project:
        writer["project_scope"] = [project]
    memory_id, _ = _seed(context, "candidate", writer)
    _store_do(context, lambda store: store.update_memory(
        memory_id=memory_id, patch={"status": "private_only", "sensitivity": "private"},
    ))
    stored = _state(context, memory_id)
    assert (stored[0]["status"], stored[0]["sensitivity"]) == ("private_only", "private")
    key, claims = None, {}
    if caller == "declared-peer":
        claims = {"agent_id": caller, "permission_profile": "trusted_local_agent"}
    elif caller != "owner":
        profile = {"admin": "admin_agent", "project-peer": "project_scoped_agent"}.get(
            caller, "trusted_local_agent",
        )
        _, key = _store_do(context, lambda store: create_agent_key(
            store, user_id=context.user_id, agent_id=caller, permission_profile=profile, project_scope=project,
        ))
    before = _events(context)
    allowed = caller in {"owner", "admin"}

    def correct(store):
        identity = resolve_protected_agent_identity(store, user_id=context.user_id, raw_key=key, payload=claims)
        service = VNextMemoryCommitService(store)
        correction = {"memory_id": memory_id, "canonical_text": "The test orchard opens on Sunday."}
        if allowed:
            return service.correct(identity=identity, **correction)
        # The mounted route catches the policy exception within the transaction too.
        with pytest.raises(AgentPolicyBlockedError) as failure:
            service.correct(identity=identity, **correction)
        assert failure.value.decision.reasons == (PENDING_WRITE_RESOLVER_REASON,)

    result = _store_do(context, correct)
    events = [event for event_id, event in _events(context).items() if event_id not in before]
    row, revisions, _ = _state(context, memory_id)
    if allowed:
        assert (result["status"], result["write_mode"]) == ("committed", "commit")
        assert (row["status"], row["confirmation_status"], row["sensitivity"]) == ("active", "confirmed", "private")
        assert row["canonical_text"] == "The test orchard opens on Sunday."
        assert [revision["action"] for revision in revisions if revision not in stored[1]] == ["agentic_memory_correct"]
        expected = ["memory.updated", "memory_revision.created", "agent.memory_corrected"]
        if caller == "admin":
            expected += ["agent.identity_upserted", "policy.decision"]
        assert Counter(event["event_type"] for event in events) == Counter(expected)
        return

    assert _state(context, memory_id) == stored
    assert Counter(event["event_type"] for event in events) == {
        "agent.identity_upserted": 1, "policy.decision": 1, "agent.policy_blocked": 1,
    }
    assert {(event["actor_type"], event["actor_id"]) for event in events} == {("agent", caller)}
    for event in events:
        if event["event_type"] != "agent.identity_upserted":
            assert event["target_id"] == memory_id
            assert event["payload_json"]["policy_decision"]["reasons"] == [PENDING_WRITE_RESOLVER_REASON]
