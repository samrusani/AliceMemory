"""Refused key claims are recorded after rollback through the PostgreSQL doors."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import time
from uuid import uuid4

import pytest

from alicebot_api import main as main_module, vnext_agent_keys, vnext_key_rejection_events
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import MCPNotPermittedError, MCPRuntimeContext
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.test_hidden_id_cli_postgres import _run
from tests.integration.test_reserved_marker_binding_postgres import stored_key


def _events(harness):
    with harness.store() as store:
        return store.list_events()


def _rejections(harness):
    return [row for row in _events(harness) if row["event_type"] == "agent.key_escalation_rejected"]


def _call(harness, monkeypatch, key, claim):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    with pytest.raises(MCPNotPermittedError) as error:
        call_mcp_tool(context, name="alice_lessons", arguments={"task": "review", **claim})
    return error.value


@pytest.mark.parametrize(("kind", "claim", "reason"), [
    ("normal", {"agent_id": "other-agent"}, "agent_id_mismatch"),
    ("normal", {"permission_profile": "admin_agent"}, "permission_profile_escalation"),
    ("normal", {"permission_profile": "unknown-profile"}, "permission_profile_escalation"),
    ("normal", {"project_scope": ["beta"]}, "project_scope_escalation"),
    ("reserved", {}, "reserved_project_scope_binding"),
    ("invalid", {}, "invalid_project_scope_binding"),
])
def test_each_refusal_reason_is_recorded_once_in_postgres(label_harness, monkeypatch, kind, claim, reason):
    """Mutation: remove ``persist_after_rollback(conn, user_id, exc)`` at the PostgreSQL transaction boundary."""
    h = label_harness
    key = (h.key("project_scoped_agent", project="alpha") if kind == "normal" else
           stored_key(h, "project_scoped_agent", "~global" if kind == "reserved" else ""))
    before = len(_events(h))
    _call(h, monkeypatch, key, claim)
    _call(h, monkeypatch, key, claim)
    events = _events(h)
    [event] = [row for row in events if row["event_type"] == "agent.key_escalation_rejected"]
    assert len(events) == before + 1
    assert event["payload_json"]["reason"] == reason and event["actor_type"] == "system"
    payload = event["payload_json"]
    assert payload["key_prefix"] and payload["granted_permission_profile"] == "project_scoped_agent"
    if "agent_id" in claim:
        assert payload["claimed_agent_id"] == claim["agent_id"]
    if "permission_profile" in claim:
        assert payload["claimed_permission_profile"] == claim["permission_profile"]
    if "project_scope" in claim:
        assert payload["claimed_project_scope"] == claim["project_scope"] and payload["granted_project_scope"] == "alpha"


def test_vnext_v1_and_cli_refusals_keep_their_event(label_harness, monkeypatch):
    """Mutation: remove ``persist_after_rollback(conn, user_id, exc)`` at the PostgreSQL transaction boundary."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    settings = Settings(database_url=h.urls["app"], auth_user_id=str(h.user_id))
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    status, body, _ = h.request("POST", "/v0/vnext/context-packs", key=key,
                                payload={"query": "x", "agent_id": "vnext-other"})
    assert status == 403, body
    status, body, _ = h.request("POST", "/v1/workspaces/bootstrap", key=key, payload={"agent_id": "v1-other"})
    assert status == 403, body
    monkeypatch.setenv("DATABASE_URL", h.urls["app"])
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    code, output = _run(["--user-id", str(h.user_id), "vnext", "memories", "audit", str(uuid4()),
                         "--agent-id", "cli-other", "--permission-profile", "project_scoped_agent"])
    assert code != 0, output
    events = _rejections(h)
    assert len(events) == 3 and {e["payload_json"].get("claimed_agent_id") for e in events} == {
        "vnext-other", "v1-other", "cli-other"
    }, events


def test_invalid_revoked_and_foreign_postgres_keys_write_nothing(label_harness, monkeypatch):
    """Mutation: replace the revoked-key condition in ``verify_agent_key`` with ``if False:``."""
    h = label_harness
    revoked = h.key("read_only_agent", project="alpha")
    with h.store() as store:
        record = store.get_agent_api_key_by_hash(hash_agent_key(revoked))
        store.revoke_agent_api_key(key_id=str(record["id"]))
    other_user = uuid4()
    with user_connection(h.urls["app"], other_user) as conn:
        ContinuityStore(conn).create_user(other_user, f"other-{other_user}@example.invalid", "Synthetic")
        _record, foreign = create_agent_key(PostgresVNextStore(conn), user_id=other_user,
                                             agent_id="foreign", permission_profile="read_only_agent")
    before = len(_events(h))
    for key in ("alice_sk_not_issued", revoked, foreign):
        error = _call(h, monkeypatch, key, {})
        assert error.code == "not_permitted"
    assert len(_events(h)) == before and _rejections(h) == []


def test_postgres_claims_are_bounded_and_clean(label_harness, monkeypatch):
    """Mutation: change ``return clean[:CLAIM_VALUE_LIMIT]`` to ``return clean``."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    synthetic = "AK" + "IAIOSFODNN7EXAMPLE"
    for claim in ({"agent_id": "x" * 200_000}, {"agent_id": synthetic},
                  {"agent_id": "different\u0001\u200dagent"}, {"project_scope": [f"other-{i}" for i in range(12)]}):
        _call(h, monkeypatch, key, claim)
    payloads = [event["payload_json"] for event in _rejections(h)]
    assert len(payloads) == 4
    ids = [payload.get("claimed_agent_id") for payload in payloads]
    assert "x" * 128 in ids and "[withheld]" in ids and "differentagent" in ids
    assert synthetic not in json.dumps(payloads)
    [scope] = [payload["claimed_project_scope"] for payload in payloads if "claimed_project_scope" in payload]
    assert scope == [f"other-{i}" for i in range(8)]


def test_distinct_truncated_postgres_claims_are_not_retries(label_harness, monkeypatch):
    """Mutation: replace the claim fingerprint with an empty string."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    for claim in ("x" * 128 + "a", "x" * 128 + "b", "x" * 128 + "a"):
        _call(h, monkeypatch, key, {"agent_id": claim})
    events = _rejections(h)
    assert len(events) == 2
    assert all(event["payload_json"]["claimed_agent_id"] == "x" * 128 for event in events)
    assert len({event["payload_json"]["claim_fingerprint"] for event in events}) == 2
    for tail in ("IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLF"):
        _call(h, monkeypatch, key, {"agent_id": "AKIA" + tail})
    withheld = [event["payload_json"] for event in _rejections(h)
                if event["payload_json"]["claimed_agent_id"] == "[withheld]"]
    assert len(withheld) == 2 and len({payload["claim_fingerprint"] for payload in withheld}) == 2


def test_postgres_refusal_rolls_back_an_earlier_write(label_harness, monkeypatch):
    """Mutation: remove ``persist_after_rollback(conn, user_id, exc)`` at the PostgreSQL transaction boundary."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    marker = f"rollback-{uuid4().hex}"
    original = vnext_agent_keys.verify_agent_key
    before = len(_events(h))
    with h.store() as store:
        revisions = store.conn.execute("SELECT count(*) AS n FROM memory_revisions").fetchone()["n"]

    def write_then_verify(store, raw_key):
        store.create_memory({"memory_key": marker, "canonical_text": marker, "title": marker,
                             "status": "active", "domain": "project", "sensitivity": "public"})
        return original(store, raw_key)

    monkeypatch.setattr(vnext_agent_keys, "verify_agent_key", write_then_verify)
    _call(h, monkeypatch, key, {"agent_id": "other-agent"})
    with h.store() as store:
        assert not store.list_memories(query=marker, status=None)
        assert store.conn.execute("SELECT count(*) AS n FROM memory_revisions").fetchone()["n"] == revisions
    assert len(_events(h)) == before + 1 and len(_rejections(h)) == 1


def test_simultaneous_postgres_refusals_are_deduplicated(label_harness, monkeypatch):
    """Mutation: remove the ``pg_advisory_xact_lock`` statement in ``persist_after_rollback``."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    original = PostgresVNextStore.list_events

    def slow_read(store, *args, **kwargs):
        rows = original(store, *args, **kwargs)
        if kwargs.get("target_type") == "agent_api_key":
            time.sleep(0.2)
        return rows

    monkeypatch.setattr(PostgresVNextStore, "list_events", slow_read)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def refuse(_):
        with pytest.raises(MCPNotPermittedError):
            call_mcp_tool(context, name="alice_lessons", arguments={"task": "review", "agent_id": "other-agent"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(refuse, range(4)))
    assert len(_rejections(h)) == 1


def test_postgres_write_failure_preserves_the_http_refusal(label_harness, monkeypatch):
    """Mutation: replace the failed-write warning with ``raise``."""
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    path = "/v0/vnext/context-packs"
    first = h.request("POST", path, key=key, payload={"query": "x", "agent_id": "different"})[:2]
    calls = []
    warnings = []
    original_warning = vnext_key_rejection_events.logger.warning

    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("synthetic write failure")

    def warning(message, *args, **kwargs):
        warnings.append(message)
        original_warning(message, *args, **kwargs)

    monkeypatch.setattr(vnext_key_rejection_events, "_append_once", fail)
    monkeypatch.setattr(vnext_key_rejection_events.logger, "warning", warning)
    second = h.request("POST", path, key=key, payload={"query": "x", "agent_id": "different"})[:2]
    assert second == first and first[0] == 403
    assert calls and len(_rejections(h)) == 1
    assert warnings == ["could not record an agent key identity refusal"]


def test_restricted_http_and_mcp_readers_do_not_receive_the_event(label_harness, monkeypatch):
    """Mutation: change the rejection event's ``actor_type=\"system\"`` to ``actor_type=\"agent\"``."""
    h = label_harness
    bad = stored_key(h, "project_scoped_agent", "~global")
    status, body, _ = h.request("POST", "/v0/vnext/context-packs", key=bad, payload={"query": "x"})
    assert status == 403, body
    [event] = _rejections(h)
    assert event["actor_type"] == "system"
    with h.store() as store:
        assert str(event["id"]) not in json.dumps(store.list_agent_events(), default=str)
    operator = h.key("admin_agent")
    status, response, _ = h.request("GET", "/v0/vnext/workspace", key=operator)
    assert status == 200 and str(event["id"]) in json.dumps(response, default=str)
    arguments = {
        "alice_recall": {"query": "agent key"}, "alice_resume": {"query": "agent key"},
        "alice_context_pack": {"query": "agent key"}, "alice_lessons": {"task": "agent key"},
        "alice_what_changed": {"topic": "agent key"}, "alice_explain": {"memory_id": str(event["id"])},
        "alice_memory_review": {"status": "all"}, "alice_open_loops": {"action": "list"},
    }
    for profile in ("read_only_agent", "memory_proposal_agent", "project_scoped_agent", "admin_agent"):
        key = h.key(profile, project="alpha")
        for path in ("/v0/vnext/workspace", "/v0/vnext/agents/policy-telemetry"):
            status, response, _ = h.request("GET", path, key=key)
            assert status == 403 and str(event["id"]) not in json.dumps(response), (profile, path, status)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        for full in (False, True):
            if full:
                monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
            else:
                monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
            for definition in list_mcp_tools():
                try:
                    answer = call_mcp_tool(context, name=definition["name"], arguments=arguments.get(definition["name"], {}))
                except Exception as exc:
                    answer = str(exc)
                rendered = json.dumps(answer, default=str)
                assert str(event["id"]) not in rendered and event["event_type"] not in rendered, (
                    profile, definition["name"]
                )
