"""HTTP retries use the vault-wide idempotency lookup on PostgreSQL."""

from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_keys import hash_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def test_postgres_vault_wide_idempotency_replay_by_a_second_key(label_harness):
    """The limitations page changes with this replay behaviour.

    Mutation: make ``_idempotent_memory`` return None instead of the digest lookup result.
    """
    h = label_harness
    admin = h.key("admin_agent")
    proposal = h.key("memory_proposal_agent", project="alpha")
    for state in ("committed", "confirmation", "waiting", "rejected", "forgotten", "undone", "expired"):
        for project in ("alpha", "beta"):
            for other_text in (False, True):
                second = h.key("project_scoped_agent", project=project)
                args = {
                    "title": "Synthetic fact", "canonical_text": "Synthetic original text", "domain": "project",
                    "sensitivity": "internal", "project_scope": ["alpha"],
                    "confidence": 0.7 if state == "confirmation" else 0.99, "idempotency_key": str(uuid4()),
                }
                status, first, _ = h.request(
                    "POST", "/v0/vnext/memories/commit", payload=args,
                    key=proposal if state in {"waiting", "rejected"} else admin,
                )
                assert status in (200, 201), first
                memory_id = first["memory"]["id"]
                if state == "rejected":
                    status, answer, _ = h.request(
                        "POST", f"/v0/vnext/memories/{memory_id}/review", payload={"action": "reject"}, key=admin,
                    )
                    assert status == 200, answer
                if state in {"forgotten", "undone", "expired"}:
                    action = {"forgotten": "forget", "undone": "undo", "expired": "expire"}[state]
                    status, answer, _ = h.request(
                        "POST", f"/v0/vnext/memories/{action}",
                        payload={"memory_id": memory_id, "reason": "Synthetic reason"}, key=admin,
                    )
                    assert status == 200, answer
                with h.store() as store:
                    tables = ("memories", "event_log", "agent_identities", "memory_revisions")
                    before = {table: store.conn.execute(f"select count(*) as n from {table}").fetchone()["n"] for table in tables}
                    assert store.get_agent_api_key_by_hash(hash_agent_key(second))["last_used_at"] is None
                retry = {**args, "project_scope": [project]}
                if other_text:
                    retry["canonical_text"] = "Synthetic changed text"
                status, answer, _ = h.request("POST", "/v0/vnext/memories/commit", payload=retry, key=second)
                if project != "alpha" or other_text:
                    assert status == 400 and answer["detail"]["code"] == "invalid_request", (state, project, answer)
                else:
                    expected = {"confirmation": "confirmation_required", "waiting": "review_required", "rejected": "rejected"}.get(state, "committed")
                    assert status in (200, 201) and answer["status"] == expected, (state, answer)
                with h.store() as store:
                    after = {table: store.conn.execute(f"select count(*) as n from {table}").fetchone()["n"] for table in tables}
                    assert store.get_agent_api_key_by_hash(hash_agent_key(second))["last_used_at"] is not None
                assert after["memories"] == before["memories"] and after["memory_revisions"] == before["memory_revisions"]
                events = 2 if project == "alpha" and not other_text else 0 if project == "alpha" else 3
                assert after["event_log"] - before["event_log"] == events
                assert after["agent_identities"] - before["agent_identities"] == (1 if events else 0)


@pytest.mark.parametrize("state", ["committed", "confirmation", "waiting", "rejected", "forgotten", "undone", "expired"])
def test_postgres_known_key_distinguishes_hidden_exact_write_from_other_text(label_harness, state):
    """The limitations page changes when hidden replay answers no longer differ.

    Mutation: skip the content comparison in the blocked replay path.
    """
    h = label_harness
    admin, reader = h.key("admin_agent"), h.key("trusted_local_agent")
    args = {"title": "Synthetic", "canonical_text": "Synthetic hidden text", "domain": "project",
            "sensitivity": "confidential", "project_scope": ["alpha"], "idempotency_key": "hidden-key",
            "source_type": "browser_clip" if state in {"waiting", "rejected"} else "direct_user_instruction"}
    status, pending, _ = h.request("POST", "/v0/vnext/memories/commit", payload=args, key=admin)
    assert status == 201 and pending["status"] == ("review_required" if state in {"waiting", "rejected"} else "confirmation_required")
    memory_id = pending["memory"]["id"]
    if state in {"committed", "forgotten", "undone", "expired"}:
        status, confirmed, _ = h.request("POST", "/v0/vnext/memories/confirm", key=admin, payload={
            "confirmation_id": pending["confirmation_id"], "action": "confirm",
        })
        assert status == 200 and confirmed["status"] == "committed"
    if state == "rejected":
        status, result, _ = h.request("POST", f"/v0/vnext/memories/{memory_id}/review", key=admin, payload={"action": "reject"})
        assert status == 200, result
    if state in {"forgotten", "undone", "expired"}:
        action = {"forgotten": "forget", "undone": "undo", "expired": "expire"}[state]
        status, result, _ = h.request("POST", f"/v0/vnext/memories/{action}", key=admin, payload={
            "memory_id": memory_id, "reason": "Synthetic",
        })
        assert status == 200, result
    for text, expected in ((args["canonical_text"], 403), ("Different", 400)):
        with h.store() as store:
            before = store.conn.execute("select count(*) as n from event_log").fetchone()["n"]
        status, result, _ = h.request("POST", "/v0/vnext/memories/commit", payload={**args, "canonical_text": text}, key=reader)
        assert status == expected, result
        with h.store() as store:
            assert store.conn.execute("select count(*) as n from event_log").fetchone()["n"] - before == 3
            assert store.get_agent_api_key_by_hash(hash_agent_key(reader))["last_used_at"] is not None
    with h.store() as store:
        assert store.conn.execute("select count(*) as n from memories").fetchone()["n"] == 1


def test_postgres_agent_can_claim_a_key_before_the_intended_writer(label_harness):
    """The limitations page changes if idempotency keys stop sharing one vault-wide space.

    Mutation: make ``_idempotent_memory`` return None instead of the digest lookup result.
    """
    h = label_harness
    first, second = h.key("project_scoped_agent", project="alpha"), h.key("project_scoped_agent", project="alpha")
    args = {"title": "Synthetic", "canonical_text": "First writer's text", "domain": "project",
            "sensitivity": "internal", "idempotency_key": "preempted-key"}
    status, original, _ = h.request("POST", "/v0/vnext/memories/commit", payload=args, key=first)
    assert status == 201 and not original.get("idempotent_replay", False)
    status, replay, _ = h.request("POST", "/v0/vnext/memories/commit", payload=args, key=second)
    assert status == 201 and replay["idempotent_replay"]
    status, answer, _ = h.request("POST", "/v0/vnext/memories/commit", payload={**args, "canonical_text": "Second writer's text"}, key=second)
    assert status == 400 and answer["detail"]["code"] == "invalid_request"
    status, fresh, _ = h.request("POST", "/v0/vnext/memories/commit", payload={**args, "canonical_text": "Second writer's text", "idempotency_key": "unused-key"}, key=second)
    assert status == 201 and not fresh.get("idempotent_replay", False)
