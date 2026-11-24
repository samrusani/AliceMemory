"""Waiting memory correction through SQLite MCP and the correction service."""

from collections import Counter
from copy import deepcopy

import pytest

from alicebot_api.vnext_agent_control import AgentPolicyBlockedError
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_protected_agent_identity
from alicebot_api.vnext_memory_commit import (
    PENDING_WRITE_RESOLVER_REASON,
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
)
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
from tests.unit.test_expired_memories_everywhere import _ArtifactShim
from tests.unit.test_mcp_refusal_is_independent_of_state import _store_do, _wire, context  # noqa: F401
from tests.waiting_correction_cases import (
    DEADLINES,
    NON_PENDING_RECORDS,
    confirmation_record,
    deadline_metadata,
    non_pending_metadata,
)


@pytest.fixture(autouse=True)
def legacy_surface(context, monkeypatch):
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")


def _seed(context, state, writer, *, tool="alice_vnext_commit_memory"):
    error, response = _wire(context, tool, {
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
    ("pending", "writer"), ("pending", "admin_agent"), ("late", "owner"), ("late", "admin_agent"), ("active", "peer"),
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


def _key(context, name, *, profile="trusted_local_agent", project=None):
    _, key = _store_do(context, lambda store: create_agent_key(
        store, user_id=context.user_id, agent_id=name, permission_profile=profile, project_scope=project,
    ))
    return key


def _service_correct(context, memory_id, *, key, claims):
    def correct(store):
        identity = resolve_protected_agent_identity(store, user_id=context.user_id, raw_key=key, payload=claims)
        try:
            return VNextMemoryCommitService(store).correct(
                identity=identity, memory_id=memory_id, canonical_text="The test orchard opens on Sunday.",
            ), None
        except AgentPolicyBlockedError as exc:
            # Like the mounted HTTP route, commit the refusal events.
            return None, exc.decision

    return _store_do(context, correct)


def _assert_policy_refusal(context, memory_id, before, actor, reason):
    events = [event for event_id, event in _events(context).items() if event_id not in before]
    assert Counter(event["event_type"] for event in events) == {
        "agent.identity_upserted": 1, "policy.decision": 1, "agent.policy_blocked": 1,
    }
    assert {(event["actor_type"], event["actor_id"]) for event in events} == {("agent", actor)}
    for event in events:
        if event["event_type"] != "agent.identity_upserted":
            assert event["target_id"] == memory_id
            assert event["payload_json"]["policy_decision"]["reasons"] == [reason]


@pytest.mark.parametrize("shape", NON_PENDING_RECORDS)
@pytest.mark.parametrize("caller", ["peer", "writer", "declared-peer", "owner", "admin", "bound-admin"])
def test_sqlite_needs_review_without_pending_record_requires_standing(context, monkeypatch, shape, caller):
    key = None
    project = "orchard" if caller == "bound-admin" else None
    if caller in {"declared-peer", "owner"}:
        memory_id, _ = _seed(context, "pending", {"agent_id": "writer", "permission_profile": "trusted_local_agent"})
    else:
        key = _key(context, "writer")
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        scope = {"project_scope": [project]} if project else {}
        memory_id, _ = _seed(context, "pending", scope, tool="alice_memory_commit")
    metadata = non_pending_metadata(_state(context, memory_id)[0]["metadata_json"], shape)
    _store_do(context, lambda store: store.update_memory(memory_id=memory_id, patch={"metadata_json": metadata}))

    if caller in {"peer", "admin", "bound-admin"}:
        profile = "trusted_local_agent" if caller == "peer" else "admin_agent"
        key = _key(context, caller, profile=profile, project=project)
    claims = {"agent_id": caller, "permission_profile": "trusted_local_agent"} if caller == "declared-peer" else {}
    stored = _state(context, memory_id)
    events = _events(context)
    answer, refusal = _service_correct(context, memory_id, key=key, claims=claims)
    if caller in {"owner", "admin", "bound-admin"}:
        assert refusal is None
        assert (answer["status"], answer["write_mode"]) == ("committed", "commit")
        updated, revisions, _ = _state(context, memory_id)
        assert (updated["status"], updated["confirmation_status"]) == ("active", "confirmed")
        assert updated["canonical_text"] == "The test orchard opens on Sunday."
        assert confirmation_record(updated) == confirmation_record(stored[0])
        assert [revision["action"] for revision in revisions if revision not in stored[1]] == ["agentic_memory_correct"]
    else:
        assert refusal is not None and refusal.reasons == (PENDING_WRITE_RESOLVER_REASON,), answer
        assert _state(context, memory_id) == stored
        _assert_policy_refusal(context, memory_id, events, caller, PENDING_WRITE_RESOLVER_REASON)


@pytest.mark.parametrize("shape", DEADLINES)
def test_sqlite_confirmation_deadline_applies_to_both_writer_paths(context, monkeypatch, shape):
    key = _key(context, "writer")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    memory_id, confirmation_id = _seed(context, "pending", {}, tool="alice_memory_commit")
    metadata = deadline_metadata(_state(context, memory_id)[0]["metadata_json"], shape)
    _store_do(context, lambda store: store.update_memory(memory_id=memory_id, patch={"metadata_json": metadata}))
    stored = _state(context, memory_id)
    events = _events(context)
    answer, refusal = _service_correct(context, memory_id, key=key, claims={})
    assert refusal is not None and refusal.reasons == ("confirmation_expired",), answer
    assert _state(context, memory_id) == stored
    _assert_policy_refusal(context, memory_id, events, "writer", "confirmation_expired")

    events = _events(context)
    error, answer = _wire(context, "alice_memory_commit", {
        "confirmation_id": confirmation_id, "confirmation_action": "confirm",
    })
    assert not error, answer
    assert (answer["status"], answer["reason"]) == ("rejected", "confirmation_expired")
    updated, revisions, vector = _state(context, memory_id)
    assert updated["status"] == "rejected"
    assert updated["canonical_text"] == stored[0]["canonical_text"] and vector == stored[2]
    record = confirmation_record(updated)
    assert record["status"] == "expired" and record["expired_at"]
    assert record == confirmation_record(answer["memory"])
    assert [revision["action"] for revision in revisions if revision not in stored[1]] == [
        "agentic_memory_confirmation_expired",
    ]
    assert [event["target_id"] for event_id, event in _events(context).items()
            if event_id not in events and event["event_type"] == "agent.memory_confirmation_expired"] == [memory_id]


def test_sqlite_trusted_correction_keeps_a_sweep_marked_memory_writable(context):
    def sweep(store):
        row = store.create_memory({
            "memory_key": "orchard.hours",
            "canonical_text": "The test orchard opens on Saturday.",
            "status": "active",
            "memory_type": "project_state",
            "domain": "project",
            "sensitivity": "internal",
            "last_confirmed_at": "2025-01-01T00:00:00Z",
            "valid_to": "2025-01-02T00:00:00Z",
        })
        VNextSchedulerService(_ArtifactShim(store))._run_staleness_sweep(
            SchedulerRunRequest(workflow_type="staleness_sweep", options={"reference_time": "2026-01-01T00:00:00Z"}),
            metadata={},
        )
        return row["id"]

    memory_id = _store_do(context, sweep)
    before, _, _ = _state(context, memory_id)
    assert before["status"] == "stale" and before["metadata_json"]["staleness"]["reason"] == "valid_to_expired"

    error, answer = _correct(context, memory_id, {"agent_id": "peer", "permission_profile": "trusted_local_agent"})
    assert not error and answer["status"] == "committed", answer
    after, _, _ = _state(context, memory_id)
    assert after["status"] == "active"
    assert after["last_confirmed_at"] > before["last_confirmed_at"]
    assert after["valid_to"] == before["valid_to"]


@pytest.mark.parametrize("status", ["candidate", "needs_review"])
def test_sqlite_trusted_correction_keeps_consolidation_on_its_decision_path(context, status):
    row = _store_do(context, lambda store: store.create_memory({
        "memory_key": "orchard.rollup",
        "canonical_text": "Orchard opening hours summary.",
        "status": status,
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {"consolidation": {"member_snapshots": []}},
    }))
    memory_id = row["id"]
    key = _key(context, "peer")
    before = _state(context, memory_id)
    events = _events(context)

    with pytest.raises(VNextMemoryCommitValidationError, match="approved through accept_consolidation"):
        _service_correct(context, memory_id, key=key, claims={})

    assert _state(context, memory_id) == before and _events(context) == events
