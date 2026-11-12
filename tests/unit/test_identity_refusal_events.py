"""A refused key claim leaves one bounded audit event and no other request write."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import logging
import time
from uuid import uuid4

import pytest

from alicebot_api import onramp, vnext_agent_keys, vnext_key_rejection_events
from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.session_briefing import session_brief_identity
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import summarize_agent_policy_telemetry
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key
from tests.unit.test_identity_claim_codes import keyed, wire  # noqa: F401
from tests.unit.test_lessons_read import LESSON
from tests.unit.test_reserved_marker_key_binding import store_key, world  # noqa: F401


def _events(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        return SQLiteVNextStore(conn, vault.user).list_events()


def _rejections(vault):
    return [row for row in _events(vault) if row["event_type"] == "agent.key_escalation_rejected"]


def _refuse(context, claim):
    failed, answer = wire(context, "alice_lessons", {"task": "reviewing a change", **claim})
    assert failed and answer["error"]["code"] == "identity_claim_refused", answer
    return answer


@pytest.mark.parametrize(("claim", "reason"), [
    ({"agent_id": "different-agent"}, "agent_id_mismatch"),
    ({"permission_profile": "admin_agent"}, "permission_profile_escalation"),
    ({"permission_profile": "unknown-profile"}, "permission_profile_escalation"),
    ({"project_scope": ["beta"]}, "project_scope_escalation"),
])
def test_mcp_claim_refusal_keeps_one_event_and_no_other_write(keyed, claim, reason):
    """Mutation: remove ``persist_after_rollback(conn, user_id, exc)`` at the SQLite boundary."""
    vault, context = keyed
    with sqlite_user_connection(vault.path, vault.user) as conn:
        before = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                  for table in ("memories", "memory_revisions", "event_log")}
    first = _refuse(context, claim)
    assert _refuse(context, claim) == first
    with sqlite_user_connection(vault.path, vault.user) as conn:
        after = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                 for table in ("memories", "memory_revisions", "event_log")}
    assert after == {**before, "event_log": before["event_log"] + 1}
    [event] = _rejections(vault)
    assert event["payload_json"]["reason"] == reason and event["target_type"] == "agent_api_key"
    payload = event["payload_json"]
    assert payload["key_prefix"] and payload["granted_permission_profile"] == "memory_proposal_agent"
    if "agent_id" in claim:
        assert payload["claimed_agent_id"] == claim["agent_id"]
    if "permission_profile" in claim:
        assert payload["claimed_permission_profile"] == claim["permission_profile"]
    if "project_scope" in claim:
        assert payload["claimed_project_scope"] == claim["project_scope"] and payload["granted_project_scope"] == "alpha"


def test_a_refused_call_rolls_back_a_prior_write(keyed, monkeypatch):
    """Mutation: change the SQLite boundary's ``conn.rollback()`` to ``conn.commit()``."""
    vault, context = keyed
    original = vnext_agent_keys.verify_agent_key
    marker = f"rollback-{uuid4().hex}"
    before_events = len(_events(vault))
    with sqlite_user_connection(vault.path, vault.user) as conn:
        before_revisions = conn.execute("SELECT count(*) AS n FROM memory_revisions").fetchone()["n"]

    def write_then_verify(store, raw_key):
        store.create_memory({"memory_key": marker, "canonical_text": marker, "title": marker,
                             "status": "active", "domain": "project", "sensitivity": "public"})
        return original(store, raw_key)

    monkeypatch.setattr(vnext_agent_keys, "verify_agent_key", write_then_verify)
    _refuse(context, {"agent_id": "different-agent"})
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert conn.execute("SELECT count(*) AS n FROM memories WHERE memory_key = ?", (marker,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM memory_revisions").fetchone()["n"] == before_revisions
    assert len(_events(vault)) == before_events + 1
    assert len(_rejections(vault)) == 1


def test_a_long_claim_is_bounded(keyed):
    """Mutation: change ``return clean[:CLAIM_VALUE_LIMIT]`` to ``return clean``."""
    vault, context = keyed
    _refuse(context, {"agent_id": "x" * 200_000})
    [event] = _rejections(vault)
    assert event["payload_json"]["claimed_agent_id"] == "x" * 128


def test_a_credential_claim_is_withheld(keyed):
    """Mutation: replace the credential check in ``_clean`` with ``if False:``."""
    vault, context = keyed
    synthetic = "AK" + "IAIOSFODNN7EXAMPLE"
    _refuse(context, {"agent_id": synthetic})
    [event] = _rejections(vault)
    assert event["payload_json"]["claimed_agent_id"] == "[withheld]"
    assert synthetic not in json.dumps(event)


def test_control_and_format_characters_are_removed_from_claims(keyed):
    """Mutation: replace the control-free ``clean`` assignment with ``clean = text``."""
    vault, context = keyed
    _refuse(context, {"agent_id": "different\u0001\u200dagent"})
    [event] = _rejections(vault)
    assert event["payload_json"]["claimed_agent_id"] == "differentagent"


def test_a_scope_claim_list_is_bounded(keyed):
    """Mutation: replace ``value[:CLAIM_LIST_LIMIT]`` with ``value`` in ``rejection_notice``."""
    vault, context = keyed
    scopes = [f"other-{i}" for i in range(12)]
    _refuse(context, {"project_scope": scopes})
    [event] = _rejections(vault)
    assert event["payload_json"]["claimed_project_scope"] == scopes[:8]


def test_identical_refusals_are_deduplicated_for_sixty_seconds(keyed, monkeypatch):
    """Mutation: replace the duplicate-event condition in ``_append_once`` with ``if False:``."""
    vault, context = keyed
    claim = {"agent_id": "different-agent"}
    _refuse(context, claim)
    _refuse(context, claim)
    assert len(_rejections(vault)) == 1
    real_datetime = datetime

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz or UTC) + timedelta(seconds=61)

    monkeypatch.setattr(vnext_key_rejection_events, "datetime", Later)
    _refuse(context, claim)
    assert len(_rejections(vault)) == 2


def test_distinct_truncated_claims_are_not_retries(keyed):
    """Mutation: replace the claim fingerprint with an empty string."""
    vault, context = keyed
    for claim in ("x" * 128 + "a", "x" * 128 + "b", "x" * 128 + "a"):
        _refuse(context, {"agent_id": claim})
    events = _rejections(vault)
    assert len(events) == 2
    assert all(event["payload_json"]["claimed_agent_id"] == "x" * 128 for event in events)
    assert len({event["payload_json"]["claim_fingerprint"] for event in events}) == 2
    for tail in ("IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLF"):
        _refuse(context, {"agent_id": "AKIA" + tail})
    withheld = [event["payload_json"] for event in _rejections(vault)
                if event["payload_json"]["claimed_agent_id"] == "[withheld]"]
    assert len(withheld) == 2 and len({payload["claim_fingerprint"] for payload in withheld}) == 2


def test_simultaneous_sqlite_refusals_are_deduplicated(keyed, monkeypatch, caplog):
    """Mutation: change ``BEGIN IMMEDIATE`` to ``BEGIN`` in ``persist_after_rollback``."""
    vault, context = keyed
    original = SQLiteVNextStore.list_events

    def slow_read(store, *args, **kwargs):
        rows = original(store, *args, **kwargs)
        if kwargs.get("target_type") == "agent_api_key":
            time.sleep(0.1)
        return rows

    monkeypatch.setattr(SQLiteVNextStore, "list_events", slow_read)
    with caplog.at_level(logging.WARNING), ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda _: _refuse(context, {"agent_id": "other-agent"}), range(4)))
    assert len(answers) == 4 and len(_rejections(vault)) == 1
    assert "could not record an agent key identity refusal" not in caplog.text


def test_event_write_failure_logs_without_changing_the_refusal(keyed, monkeypatch, caplog):
    """Mutation: replace the failed-write warning with ``raise``."""
    vault, context = keyed
    expected = _refuse(context, {"agent_id": "first-different-agent"})
    before = len(_rejections(vault))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(vnext_key_rejection_events, "_append_once", fail)
    with caplog.at_level(logging.WARNING):
        answer = _refuse(context, {"agent_id": "second-different-agent"})
    assert answer == expected and len(_rejections(vault)) == before
    assert "could not record an agent key identity refusal" in caplog.text


def test_bad_keys_and_unrecorded_scope_claims_leave_no_event(keyed, monkeypatch):
    """Mutation: replace the revoked-key condition in ``verify_agent_key`` with ``if False:``."""
    vault, context = keyed
    before = len(_rejections(vault))
    _refuse(context, {"project_scope": [" "]})
    failed, answer = wire(context, "alice_lessons", {"task": "review", "project_scope": "alpha"})
    assert failed and answer["error"]["code"] == "invalid_request"
    revoked = vault.key("revoked", "read_only_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        record = store.get_agent_api_key_by_hash(hash_agent_key(revoked))
        store.revoke_agent_api_key(key_id=str(record["id"]))
    other_user = uuid4()
    onramp.bootstrap_database(vault.path, user_id=other_user, user_email="fixture-other-user")
    with sqlite_user_connection(vault.path, other_user) as conn:
        _record, foreign = create_agent_key(SQLiteVNextStore(conn, other_user), user_id=other_user,
                                             agent_id="foreign", permission_profile="read_only_agent")
    for raw in ("alice_sk_not_issued", revoked, foreign):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
        failed, answer = wire(context, "alice_lessons", {"task": "review"})
        assert failed and answer["error"]["code"] == "not_permitted", answer
    assert len(_rejections(vault)) == before


def test_session_brief_records_one_event_for_each_bad_binding(world, monkeypatch):
    """Mutation: catch ``AgentKeyAuthenticationError`` inside the SQLite transaction and return quiet there."""
    for binding, reason in (("~global", "reserved_project_scope_binding"), ("", "invalid_project_scope_binding")):
        key = store_key(world, profile="project_scoped_agent", binding=binding)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        assert session_brief_identity(world.path, world.user_id) == (None, True)
        assert session_brief_identity(world.path, world.user_id) == (None, True)
        with world.store() as conn:
            events = SQLiteVNextStore(conn, world.user_id).list_events()
        [event] = [event for event in events if event["event_type"] == "agent.key_escalation_rejected" and
                   event["payload_json"].get("reason") == reason]
        assert event["payload_json"]["key_prefix"] and event["payload_json"]["granted_permission_profile"] == "project_scoped_agent"


def test_refusal_is_not_agent_activity_or_visible_in_mcp(keyed, monkeypatch):
    """Mutation: change the rejection event's ``actor_type=\"system\"`` to ``actor_type=\"agent\"``."""
    vault, context = keyed
    failed, first = wire(context, "alice_memory_commit", {"lesson": LESSON})
    assert not failed
    with sqlite_user_connection(vault.path, vault.user) as conn:
        before = SQLiteVNextStore(conn, vault.user).list_agent_events()
    _refuse(context, {"agent_id": "different-agent"})
    [event] = _rejections(vault)
    with sqlite_user_connection(vault.path, vault.user) as conn:
        after = SQLiteVNextStore(conn, vault.user).list_agent_events()
    assert event["actor_type"] == "system" and after == before
    assert summarize_agent_policy_telemetry(agent_events=after, artifacts=[], memories=[])["total_agent_events"] == len(before)
    failed, second = wire(context, "alice_memory_commit", {"lesson": {**LESSON, "what_happened": "A second change."}})
    assert not failed and (first["status"], first["write_mode"]) == (second["status"], second["write_mode"])
    assert first["memory"]["trust_class"] == second["memory"]["trust_class"]
    arguments = {
        "alice_recall": {"query": "agent key"}, "alice_resume": {"query": "agent key"},
        "alice_context_pack": {"query": "agent key"}, "alice_lessons": {"task": "agent key"},
        "alice_what_changed": {"topic": "agent key"}, "alice_explain": {"memory_id": str(event["id"])},
        "alice_memory_review": {"status": "all"}, "alice_open_loops": {"action": "list"},
    }
    for profile in ("read_only_agent", "memory_proposal_agent", "project_scoped_agent"):
        raw = vault.key(f"reader-{profile}", profile, "alpha")
        monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
        for full in (False, True):
            if full:
                monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
            else:
                monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
            for definition in list_mcp_tools():
                _failed, answer = wire(context, definition["name"], arguments.get(definition["name"], {}))
                rendered = json.dumps(answer)
                assert str(event["id"]) not in rendered and event["event_type"] not in rendered, (
                    profile, full, definition["name"]
                )


def test_owner_export_contains_the_recorded_refusal(keyed, tmp_path):
    """Mutation: remove ``persist_after_rollback(conn, user_id, exc)`` at the SQLite boundary."""
    vault, context = keyed
    _refuse(context, {"agent_id": "different-agent"})
    [event] = _rejections(vault)
    target = tmp_path / "export.jsonl"
    assert onramp.main(["export", "--db", str(vault.path), "--user-id", str(vault.user), "--out", str(target)]) == 0
    records = [json.loads(line) for line in target.read_text().splitlines()]
    assert any(row.get("record_type") == "event" and row["record"]["id"] == event["id"] for row in records)
