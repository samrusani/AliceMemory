"""Query events, key revocation and capture ceilings on PostgreSQL."""

import json
import os
import subprocess
import sys

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire


def _events(h):
    with h.store() as store:
        return store.list_events()


def _wire(h, monkeypatch, key, name, payload):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    return wire(MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id), name, payload)


def _queries(h, monkeypatch, event_type):
    key = h.key("read_only_agent")
    credential = "AK" + "IA" + "23456789ABCDEFGH"
    for query, expected in (("x" * 19000, "x" * 128), ("a\u0007b\u200bc", "abc"),
                            ("request " + credential, "[withheld]"), ("x" * 129 + " " + credential, "[withheld]")):
        before = {str(event["id"]) for event in _events(h)}
        failed, answer = _wire(h, monkeypatch, key, "alice_context_pack", {"query": query})
        assert not failed, answer
        rows = [event for event in _events(h) if str(event["id"]) not in before
                and event["event_type"] == event_type]
        assert len(rows) == 1 and rows[0]["payload_json"]["query"] == expected


def test_postgres_compiled_event_stores_a_clean_bounded_query(label_harness, monkeypatch):
    """Guard: store the raw query in the compiled event."""
    _queries(label_harness, monkeypatch, "retrieval.context_pack_compiled")


def test_postgres_agent_request_event_stores_a_clean_bounded_query(label_harness, monkeypatch):
    """Guard: store the raw query in the agent request event."""
    _queries(label_harness, monkeypatch, "agent.context_pack_requested")


def _old_keys(h):
    with h.store() as store:
        old = [create_agent_key(store, user_id=h.user_id, agent_id=f"old-{i}",
                                permission_profile="read_only_agent") for i in range(2)]
    with h.store() as store:
        for n in range(205):
            create_agent_key(store, user_id=h.user_id, agent_id=f"new-{n}", permission_profile="read_only_agent")
    return old


def _cli(h, *args):
    env = dict(os.environ)
    env.pop("ALICE_AGENT_API_KEY", None)
    return subprocess.run([sys.executable, "-c", "import sys; from alicebot_api.cli import main; sys.exit(main())",
                           "--database-url", h.urls["app"], "--user-id", str(h.user_id), *args],
                          capture_output=True, text=True, env=env, check=False)


def test_postgres_revoke_finds_older_key_by_id_and_prefix(label_harness):
    """Guard: limit the command's revocation search to 200 keys."""
    h = label_harness
    old = _old_keys(h)
    for selector in (str(old[0][0]["id"]), str(old[1][0]["key_prefix"])):
        result = _cli(h, "agent", "keys", "revoke", selector)
        assert result.returncode == 0, result.stdout + result.stderr


def test_postgres_unlimited_key_list_reaches_old_revocations(label_harness):
    """Guard: cap a request for all PostgreSQL keys at 200."""
    h = label_harness
    old = _old_keys(h)
    result = _cli(h, "agent", "keys", "revoke", str(old[0][0]["id"]))
    assert result.returncode == 0, result.stdout + result.stderr


def test_postgres_revoke_accepts_a_unique_partial_prefix(label_harness):
    """Guard: require the entire stored prefix rather than a unique prefix."""
    h = label_harness
    with h.store() as store:
        record, _ = create_agent_key(store, user_id=h.user_id, agent_id="reader", permission_profile="read_only_agent")
    result = _cli(h, "agent", "keys", "revoke", str(record["key_prefix"])[:-1])
    assert result.returncode == 0, result.stdout + result.stderr


def test_postgres_prefix_ambiguity_counts_revoked_keys(label_harness):
    """Guard: remove revoked keys from the ambiguity check."""
    h = label_harness
    with h.store() as store:
        first, _ = create_agent_key(store, user_id=h.user_id, agent_id="first", permission_profile="read_only_agent")
        create_agent_key(store, user_id=h.user_id, agent_id="second", permission_profile="read_only_agent")
        store.revoke_agent_api_key(key_id=str(first["id"]))
    result = _cli(h, "agent", "keys", "revoke", "alice_sk_")
    assert result.returncode != 0
    with h.store() as store:
        assert sum(row["revoked_at"] is None for row in store.list_agent_api_keys(limit=None)) == 1


def test_postgres_ambiguous_prefix_asks_for_an_id(label_harness):
    """Guard: remove the safe warning that asks for a key id."""
    h = label_harness
    h.key("read_only_agent")
    h.key("read_only_agent")
    result = _cli(h, "agent", "keys", "revoke", "alice_sk_")
    assert result.returncode != 0 and "revoke by key id" in result.stderr


def test_postgres_mcp_capture_refuses_above_ceiling(label_harness, monkeypatch):
    """Guard: disable the capture sensitivity ceiling on the MCP door."""
    h = label_harness
    for profile, project, level in (("project_scoped_agent", "alpha", "regulated"),
                                    ("trusted_local_agent", None, "confidential")):
        key = h.key(profile, project=project)
        before = h.snapshot()
        failed, result = _wire(h, monkeypatch, key, "alice_capture", {
            "raw_text": f"Above ceiling {profile}.", "domain": "project", "sensitivity": level})
        assert failed and result["error"]["code"] == "not_permitted", result
        after = h.snapshot()
        assert before["sources"] == after["sources"]
        events = [event for event in after["event_log"] if event["id"] not in {e["id"] for e in before["event_log"]}]
        assert {event["event_type"] for event in events} == {"agent.identity_upserted", "policy.decision", "agent.policy_blocked"}
        assert "sensitivity_above_agent_ceiling" in json.dumps(events, default=str)


def test_postgres_http_capture_refuses_above_ceiling_and_preserves_allowed(label_harness, tmp_path):
    """Guard: disable the capture sensitivity ceiling on the HTTP source door."""
    h = label_harness
    status, body, _ = h.request("POST", "/v0/vnext/sources", payload={
        "raw_text": "Owner source fixture.", "domain": "project", "sensitivity": "regulated"})
    assert status == 201, body
    for profile, project, level in (("project_scoped_agent", "alpha", "regulated"),
                                    ("trusted_local_agent", None, "confidential")):
        key = h.key(profile, project=project)
        before = h.snapshot()
        status, body, _ = h.request("POST", "/v0/vnext/sources", key=key, payload={
            "raw_text": f"Above ceiling {profile}.", "domain": "project", "sensitivity": level})
        assert status == 403, (status, body)
        after = h.snapshot()
        assert before["sources"] == after["sources"]
        events = [event for event in after["event_log"] if event["id"] not in {e["id"] for e in before["event_log"]}]
        assert {event["event_type"] for event in events} == {"agent.identity_upserted", "policy.decision", "agent.policy_blocked"}
        assert "sensitivity_above_agent_ceiling" in json.dumps(events, default=str)
    for profile, project, level in (("project_scoped_agent", "alpha", "private"),
                                    ("admin_agent", None, "regulated"), ("admin_agent", "alpha", "regulated")):
        key = h.key(profile, project=project)
        status, body, _ = h.request("POST", "/v0/vnext/sources", key=key, payload={
            "raw_text": f"Allowed {profile} {project}.", "domain": "project", "sensitivity": level})
        assert status == 201, (status, body)
    text_file = tmp_path / "source.txt"
    text_file.write_text("Local owner file fixture.")
    for args in (("vnext", "sources", "capture-text", "Local owner text fixture.", "--sensitivity", "regulated"),
                 ("vnext", "sources", "capture-file", str(text_file), "--sensitivity", "regulated")):
        result = _cli(h, *args)
        assert result.returncode == 0, result.stdout + result.stderr


def test_postgres_agent_output_refuses_above_ceiling(label_harness):
    """Guard: disable the HTTP agent-output ceiling."""
    h = label_harness
    with h.store() as store:
        _, key = create_agent_key(store, user_id=h.user_id, agent_id="writer",
                                  permission_profile="project_scoped_agent", project_scope="alpha")
    before = h.snapshot()
    status, body, _ = h.request("POST", "/v0/vnext/agents/ingest-output", key=key, payload={
        "agent_id": "writer", "title": "Output", "content": "Synthetic agent output.",
        "domain": "project", "sensitivity": "regulated"})
    assert status == 403, (status, body)
    assert h.snapshot()["sources"] == before["sources"]
    status, body, _ = h.request("POST", "/v0/vnext/agents/ingest-output", key=key, payload={
        "agent_id": "writer", "title": "Output", "content": "Allowed agent output.",
        "domain": "project", "sensitivity": "private"})
    assert status == 201, (status, body)


def test_postgres_browser_clip_refuses_above_ceiling(label_harness):
    """Guard: skip the browser clip's ceiling refusal."""
    h = label_harness
    status, body, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", payload={
        "url": "https://example.invalid/owner", "page_text": "Owner browser fixture.", "sensitivity": "regulated"})
    assert status == 201, (status, body)
    key = h.key("trusted_local_agent")
    before = h.snapshot()
    payload = {"url": "https://example.invalid/page", "page_text": "Synthetic browser fixture.",
               "domain": "project", "sensitivity": "confidential"}
    status, body, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", key=key, payload=payload)
    assert status == 403, (status, body)
    assert h.snapshot()["sources"] == before["sources"]
    status, body, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", key=key,
                                payload={**payload, "sensitivity": "private"})
    assert status == 201, (status, body)
    admin = h.key("admin_agent")
    status, body, _ = h.request("POST", "/v0/vnext/connectors/browser-clipper/capture", key=admin,
                                payload={**payload, "url": "https://example.invalid/admin"})
    assert status == 201, (status, body)


def test_postgres_connector_sync_refuses_above_ceiling(label_harness):
    """Guard: skip the connector sync's ceiling refusal."""
    h = label_harness
    status, body, _ = h.request("POST", "/v0/vnext/connectors/local_folder/sync", payload={
        "default_sensitivity": "regulated", "items": [{"path": "/fixture/owner.md", "text": "Owner source."}]})
    assert status == 201, (status, body)
    key = h.key("trusted_local_agent")
    before = h.snapshot()
    payload = {"default_sensitivity": "confidential", "items": [
        {"path": "/fixture/source.md", "text": "Synthetic connector fixture."}]}
    status, body, _ = h.request("POST", "/v0/vnext/connectors/local_folder/sync", key=key, payload=payload)
    assert status == 403, (status, body)
    assert h.snapshot()["sources"] == before["sources"]
    status, body, _ = h.request("POST", "/v0/vnext/connectors/local_folder/sync", key=key,
                                payload={**payload, "default_sensitivity": "private"})
    assert status == 201, (status, body)
    admin = h.key("admin_agent")
    status, body, _ = h.request("POST", "/v0/vnext/connectors/local_folder/sync", key=admin, payload={
        **payload, "items": [{"path": "/fixture/admin.md", "text": "Admin source."}]})
    assert status == 201, (status, body)


def test_postgres_connector_uses_configured_sensitivity_for_ceiling(label_harness):
    """Guard: ignore the connector's configured default sensitivity."""
    h = label_harness
    status, body, _ = h.request("PATCH", "/v0/vnext/connectors/local_folder/config", payload={
        "default_sensitivity": "confidential"})
    assert status == 200, (status, body)
    key = h.key("trusted_local_agent")
    status, body, _ = h.request("POST", "/v0/vnext/connectors/local_folder/sync", key=key, payload={
        "items": [{"path": "/fixture/default.md", "text": "Synthetic configured fixture."}]})
    assert status == 403, (status, body)


def test_postgres_cli_agent_output_refuses_above_ceiling(label_harness):
    """Guard: skip the ceiling check in the agent-output command."""
    h = label_harness
    before = h.snapshot()
    result = _cli(h, "vnext", "agents", "ingest-output", "--agent-id", "writer", "--permission-profile",
                  "project_scoped_agent", "--project-scope", "alpha", "--title", "Output", "--domain", "project",
                  "--sensitivity", "regulated", "Synthetic CLI output.")
    assert result.returncode != 0
    assert h.snapshot()["sources"] == before["sources"]
