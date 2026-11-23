"""Waiting-row correction through the mounted PostgreSQL application."""

from collections import Counter
from copy import deepcopy
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from alicebot_api.vnext_memory_commit import PENDING_WRITE_RESOLVER_REASON, VNextMemoryCommitService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def _declared(name, profile="trusted_local_agent"):
    return {"agent_id": name, "permission_profile": profile}


def _seed(h, state, *, key=None, claims=None, project=None, sensitivity="internal"):
    fields = {
        "title": "Test orchard hours",
        "canonical_text": "The test orchard opens on Saturday.",
        "domain": "project",
        "sensitivity": sensitivity,
        "confidence": 0.7 if state in {"pending", "late"} else 0.96,
        **(claims or {}),
    }
    if state != "proposal":
        fields["source_type"] = "browser_clip" if state == "candidate" else "direct_user_instruction"
    if project is not None:
        fields["project_scope"] = [project]
    path = "/v0/vnext/memory-proposals" if state == "proposal" else "/v0/vnext/memories/commit"
    status, result, _ = h.request("POST", path, key=key, payload=fields)
    assert status == 201, result
    row = result["proposal" if state == "proposal" else "memory"]
    expected = {"candidate": "candidate", "proposal": "candidate", "pending": "needs_review", "late": "needs_review"}
    assert row["status"] == expected.get(state, "active")
    if state == "late":
        metadata = deepcopy(row["metadata_json"])
        metadata["agentic_memory"]["confirmation"]["expires_at"] = "2000-01-01T00:00:00Z"
        with h.store() as store:
            store.update_memory(memory_id=row["id"], patch={"metadata_json": metadata})
    return row["id"], result.get("confirmation_id")


def _stored(h, memory_id):
    with h.store() as store:
        vectors = store.conn.execute(
            "SELECT row_to_json(e) AS embedding FROM memory_embeddings e WHERE memory_id = %s ORDER BY id",
            (memory_id,),
        ).fetchall()
        return store.get_memory(memory_id), store.list_revisions(memory_id), vectors


def _events(h):
    with h.store() as store:
        return {event["id"]: event for event in store.list_events(limit=100)}


def _new_events(h, before):
    return [event for event_id, event in _events(h).items() if event_id not in before]


def _correct(h, memory_id, *, key=None, claims=None):
    status, body, _ = h.request("POST", "/v0/vnext/memories/correct", key=key, payload={
        "memory_id": memory_id,
        "canonical_text": "The test orchard opens on Sunday.",
        **(claims or {}),
    })
    return status, body


def _policy_audit(events, body, memory_id):
    assert Counter(e["event_type"] for e in events) == {
        "agent.identity_upserted": 1, "policy.decision": 1, "agent.policy_blocked": 1,
    }
    decision = body["policy_decision"]
    assert len({event["actor_id"] for event in events}) == 1
    assert all(event["actor_type"] == "agent" and event["actor_id"] for event in events)
    for event in events:
        if event["event_type"] != "agent.identity_upserted":
            assert event["target_id"] == memory_id
            assert event["payload_json"]["policy_decision"] == decision


@pytest.mark.parametrize(("state", "caller"), [
    ("candidate", "peer"), ("candidate", "writer"), ("candidate", "project-writer"),
    ("pending", "peer"), ("candidate", "declared-peer"),
])
def test_waiting_correction_refuses_like_forget_and_writes_only_policy_events(label_harness, state, caller):
    """Check caller standing before changing the row, revisions or embeddings."""
    h = label_harness
    key = claims = None
    if caller == "declared-peer":
        memory_id, _ = _seed(h, state, claims=_declared("writer"))
        claims = _declared("peer")
    else:
        project = "orchard" if caller == "project-writer" else None
        writer = h.key("project_scoped_agent" if project else "trusted_local_agent", project=project)
        memory_id, _ = _seed(h, state, key=writer, project=project)
        key = h.key("trusted_local_agent") if caller == "peer" else writer
    before = _stored(h, memory_id)
    events = _events(h)
    status, answer = _correct(h, memory_id, key=key, claims=claims)
    assert status == 403, answer
    assert answer["policy_decision"]["reasons"] == [PENDING_WRITE_RESOLVER_REASON]
    _policy_audit(_new_events(h, events), answer, memory_id)
    assert _stored(h, memory_id) == before

    events = _events(h)
    status, forget, _ = h.request("POST", "/v0/vnext/memories/forget", key=key, payload={
        "memory_id": memory_id, **(claims or {}),
    })
    assert status == 403
    _policy_audit(_new_events(h, events), forget, memory_id)
    assert set(answer) == set(forget) and answer["detail"] == forget["detail"]
    assert {k: v for k, v in answer["policy_decision"].items() if k not in {"action", "trace_id"}} == {
        k: v for k, v in forget["policy_decision"].items() if k not in {"action", "trace_id"}
    }
    assert _stored(h, memory_id) == before


@pytest.mark.parametrize(("state", "caller"), [
    ("candidate", "owner"), ("candidate", "admin"), ("candidate", "declared-admin"),
    ("candidate", "bound-admin"),
    ("pending", "owner"), ("pending", "admin"), ("pending", "writer"), ("pending", "declared-admin"),
    ("late", "owner"), ("late", "admin"), ("late", "declared-admin"), ("active", "peer"),
])
def test_correction_keeps_the_receipt_and_resolves_the_record_with_the_callers_standing(label_harness, state, caller):
    h = label_harness
    writer_claims = _declared("writer") if caller in {"owner", "declared-admin"} else None
    writer = None if writer_claims is not None else h.key("trusted_local_agent")
    project = "orchard" if caller == "bound-admin" else None
    memory_id, confirmation_id = _seed(h, state, key=writer, claims=writer_claims, project=project)
    key = writer if caller == "writer" else None
    if caller in {"admin", "bound-admin", "peer"}:
        key = h.key("trusted_local_agent" if caller == "peer" else "admin_agent", project=project)
    claims = _declared("reviewer", "admin_agent") if caller == "declared-admin" else None
    before, revisions, _ = _stored(h, memory_id)
    events = _events(h)
    status, answer = _correct(h, memory_id, key=key, claims=claims)
    assert status == 200, answer
    assert set(answer) == {"status", "write_mode", "memory"}
    assert (answer["status"], answer["write_mode"]) == ("committed", "commit")
    updated, after_revisions, _ = _stored(h, memory_id)
    assert updated["status"] == "active" and updated["confirmation_status"] == "confirmed"
    assert updated["canonical_text"] == "The test orchard opens on Sunday."
    assert updated["metadata_json"]["review_required"] is False
    added = [revision for revision in after_revisions if revision not in revisions]
    assert len(added) == 1 and added[0]["action"] == "agentic_memory_correct"
    expected = ["memory.updated", "memory_revision.created", "agent.memory_corrected"]
    if caller != "owner":
        expected += ["agent.identity_upserted", "policy.decision"]
    assert Counter(e["event_type"] for e in _new_events(h, events)) == Counter(expected)
    if confirmation_id is None:
        return

    record = updated["metadata_json"]["agentic_memory"]["confirmation"]
    expected_record = {**before["metadata_json"]["agentic_memory"]["confirmation"], "status": "confirmed"}
    if caller != "writer":
        expected_record["confirmed_at"] = updated["metadata_json"]["agentic_memory"]["corrections"][-1]["corrected_at"]
    assert record == expected_record == answer["memory"]["metadata_json"]["agentic_memory"]["confirmation"]
    with h.store() as store:
        assert all(row["id"] != updated["id"] for row in VNextMemoryCommitService(store).inline_confirmations())
    stored = _stored(h, memory_id)
    events = _events(h)
    for action in ("confirm", "reject"):
        status, reply, _ = h.request("POST", "/v0/vnext/memories/confirm", key=writer, payload={
            "confirmation_id": confirmation_id, "action": action, **(writer_claims or {}),
        })
        assert status == 400 and reply["detail"]["code"] == "invalid_request"
    assert _stored(h, memory_id) == stored and _new_events(h, events) == []


@pytest.mark.parametrize("declared", [False, True])
def test_late_writer_correction_refuses_without_expiring_the_confirmation(label_harness, declared):
    h = label_harness
    claims = _declared("writer") if declared else None
    key = None if declared else h.key("trusted_local_agent")
    memory_id, confirmation_id = _seed(h, "late", key=key, claims=claims)
    stored = _stored(h, memory_id)
    events = _events(h)
    status, answer = _correct(h, memory_id, key=key, claims=claims)
    assert status == 403 and answer["policy_decision"]["reasons"] == ["confirmation_expired"]
    _policy_audit(_new_events(h, events), answer, memory_id)
    assert _stored(h, memory_id) == stored

    status, expiry, _ = h.request("POST", "/v0/vnext/memories/confirm", key=key, payload={
        "confirmation_id": confirmation_id, **(claims or {}),
    })
    assert status == 200 and (expiry["status"], expiry["reason"]) == ("rejected", "confirmation_expired")
    assert expiry["memory"]["metadata_json"]["agentic_memory"]["confirmation"]["status"] == "expired"


@pytest.mark.parametrize("bound", [False, True])
def test_waiting_correction_matches_missing_for_a_target_outside_the_read_fence(label_harness, bound):
    h = label_harness
    writer = h.key("admin_agent")
    key = h.key("admin_agent", project="orchard") if bound else h.key("trusted_local_agent")
    memory_id, _ = _seed(h, "pending", key=writer, project="elsewhere", sensitivity="confidential")
    events = _events(h)
    stored = _stored(h, memory_id)
    assert _correct(h, memory_id, key=key) == _correct(h, str(uuid4()), key=key)
    assert _new_events(h, events) == [] and _stored(h, memory_id) == stored


def test_owner_cli_can_correct_a_candidate(label_harness):
    h = label_harness
    memory_id, _ = _seed(h, "candidate", claims=_declared("writer"))
    result = subprocess.run(
        [sys.executable, "-m", "alicebot_api", "--user-id", str(h.user_id), "vnext", "memories", "correct",
         memory_id, "--text", "The owner corrected the test orchard hours."],
        env={**os.environ, "DATABASE_URL": h.urls["app"]}, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)["memory"]["status"] == "active"


def test_proposal_writer_keeps_the_existing_correction_refusal(label_harness):
    h = label_harness
    key = h.key("memory_proposal_agent")
    memory_id, _ = _seed(h, "proposal", key=key)
    before = _stored(h, memory_id)
    status, answer = _correct(h, memory_id, key=key)
    assert status == 403 and answer["policy_decision"]["reasons"] == ["memory_proposal_agent_cannot_mutate"]
    assert _stored(h, memory_id) == before


@pytest.mark.parametrize("kind", ["consolidation", "retired"])
def test_owner_correction_keeps_the_existing_state_refusals(label_harness, kind):
    h = label_harness
    with h.store() as store:
        row = store.create_memory({
            "memory_key": "orchard.hours", "canonical_text": "Saturday orchard hours.",
            "status": "candidate" if kind == "consolidation" else "superseded",
            "metadata_json": {"consolidation": {}} if kind == "consolidation" else {},
        })
    memory_id = str(row["id"])
    before = _stored(h, memory_id)
    events = _events(h)
    status, answer = _correct(h, memory_id)
    assert status == 400 and answer["detail"]["code"] == "invalid_request"
    assert _stored(h, memory_id) == before and _new_events(h, events) == []
