"""Explicit sensitivity limits through the mounted PostgreSQL handlers."""

from io import BytesIO
import json
from uuid import uuid4

import pytest
from psycopg import sql

from alicebot_api.cli.runner import main as cli_main
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.mcp_server import MCPServer
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401 (fixture)
from tests.integration.test_label_change_policy_postgres import (
    _assert_key_use,
    _assert_only_refusal_recorded,
    _assert_other_tables_equal,
    _new_events,
    _snapshot,
)
from tests.sensitivity_intakes import CREATE, FAN_OUT, HTTP, LEVELS, WITHIN


REASONS = ["restricted_sensitivity_filtered", "sensitivity_above_agent_ceiling"]
SYNTHETIC_CREDENTIAL = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
FIVE_DOORS = {
    ("POST", "/v0/vnext/memory-proposals", "sensitivity"): ("memory.propose", "proposal", "unknown", "memories"),
    ("POST", "/v0/vnext/open-loops", "sensitivity"): ("open_loop.create", "open_loop", "unknown", "open_loops"),
    ("POST", "/v0/vnext/queue/tasks", "sensitivity"): ("queue_task.create", None, "unknown", "task_queue"),
    ("POST", "/v0/vnext/projects", "sensitivity"): ("project.create", "project", "private", "projects"),
    ("PUT", "/v0/vnext/settings/brain-charter", "sensitivity"): (
        "brain_charter.update", "brain_charter", "private", "brain_charters",
    ),
}
WALKED = [entry for entry, data in HTTP.items() if data.kind in {CREATE, FAN_OUT}]
OPERATORS = {"/v0/vnext/projects", "/v0/vnext/settings/brain-charter"}
PROPOSALS = "/v0/vnext/memory-proposals"


def _key(h, profile, *, bound=False):
    agent_id = "admission-control"
    with h.store() as store:
        if bound:
            store.create_project({"name": "Synthetic scope", "slug": "synthetic-scope"})
        _, key = create_agent_key(
            store, user_id=h.user_id, agent_id=agent_id, permission_profile=profile,
            project_scope="synthetic-scope" if bound else None,
        )
    return agent_id, key


def _call(h, entry, level, *, key=None, claims=None, agent_id=None):
    method, path, field = entry
    door = HTTP[entry]
    payload = {**door.payload, **(claims or {})}
    if "name" in payload:
        payload["name"] = f"Synthetic project {uuid4()}"
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if level is not None:
        payload[field] = level
    return h.request(method, door.request_path or path, payload=payload, key=key)[:2]


def _assert_ceiling(status, body, *, action=None):
    assert status == 403
    assert body["detail"] == "agent policy blocked this action"
    decision = body["policy_decision"]
    assert decision["decision"] == "blocked"
    assert decision["reasons"] == REASONS
    if action:
        assert decision["action"] == action


def _assert_stored_sensitivity(h, table, row, level):
    assert row["sensitivity"] == level
    with h.store() as store:
        stored = store.conn.execute(
            sql.SQL("SELECT sensitivity FROM {} WHERE id = %s").format(sql.Identifier(table)), (row["id"],),
        ).fetchone()
    assert stored == {"sensitivity": level}


def _new_policy_refusal(before, after, action):
    policy = [
        row["payload_json"]["policy_decision"] for row in _new_events(before, after)
        if row["event_type"] == "agent.policy_blocked"
    ]
    (decision,) = policy
    _assert_ceiling(403, {"detail": "agent policy blocked this action", "policy_decision": decision}, action=action)
    return decision


@pytest.mark.parametrize("entry", WALKED, ids=[entry[1] for entry in WALKED])
def test_registry_http_creates_hold_the_ceiling(label_harness, entry):
    """Guard edit: change the HTTP policy refusal to status 422."""
    h = label_harness
    _, path, _ = entry
    door = HTTP[entry]
    agent_id, key = _key(h, "trusted_local_agent")
    for level in LEVELS:
        before = _snapshot(h)
        status, body = _call(
            h, entry, level, key=key, agent_id=agent_id if path.endswith("/ingest-output") else None,
        )
        after = _snapshot(h)
        _assert_key_use(before["agent_api_keys"], after["agent_api_keys"], key)
        _assert_other_tables_equal(before, after, {"agent_api_keys", "agent_identities", "event_log"})
        if door.unavailable:
            assert status == 422
            _assert_other_tables_equal(before, after, {"agent_api_keys"})
        elif path.endswith("/memories/commit"):
            assert status == 200 and body["status"] == "rejected"
            assert "sensitivity_above_agent_ceiling" in body["reasons"]
        else:
            _assert_ceiling(status, body)
    if not door.unavailable:
        status, body = _call(
            h, entry, "private", key=key, agent_id=agent_id if path.endswith("/ingest-output") else None,
        )
        assert status == door.success, body
        if path.endswith("/memories/commit"):
            assert body["status"] != "rejected"


@pytest.mark.parametrize(("caller", "profile", "bound"), [
    ("key", "trusted_local_agent", False),
    ("key", "trusted_local_agent", True),
    ("key", "project_scoped_agent", True),
    ("key", "memory_proposal_agent", True),
    ("declared", "trusted_local_agent", False),
])
@pytest.mark.parametrize("entry", FIVE_DOORS, ids=[entry[1] for entry in FIVE_DOORS])
def test_admitted_http_agents_are_held_to_the_selected_label(label_harness, entry, caller, profile, bound):
    """Guard edit: change the HTTP policy refusal to status 422."""
    h = label_harness
    _, path, _ = entry
    action, field, default, table = FIVE_DOORS[entry]
    if caller == "key":
        agent_id, key = _key(h, profile, bound=bound)
        claims = {}
    else:
        agent_id, key = "declared-control", None
        claims = {"agent_id": agent_id, "permission_profile": profile}
    operator_blocks = path in OPERATORS and (bound or profile != "trusted_local_agent")
    policy_blocks = profile == "memory_proposal_agent" and path != PROPOSALS
    for level in (*LEVELS, *WITHIN):
        before = _snapshot(h)
        status, body = _call(h, entry, level, key=key, claims=claims)
        if operator_blocks or policy_blocks:
            assert status == 403
            decision = body["policy_decision"]
            if operator_blocks:
                assert decision["action"] == "http.operator.access"
            else:
                assert "memory_proposal_agent_cannot_mutate" in decision["reasons"]
            assert "sensitivity_above_agent_ceiling" not in decision["reasons"]
        elif level in LEVELS:
            _assert_ceiling(status, body, action=action)
            _assert_only_refusal_recorded(
                before, _snapshot(h), body, None, None, key=key, declared_agent_id=agent_id,
            )
        else:
            assert status == HTTP[entry].success, body
            row = body if field is None else body[field]
            _assert_stored_sensitivity(h, table, row, level or default)


@pytest.mark.parametrize("caller", ["owner", "declared-admin", "key-admin", "bound-admin"])
@pytest.mark.parametrize("entry", FIVE_DOORS, ids=[entry[1] for entry in FIVE_DOORS])
def test_http_ceiling_exempt_callers_keep_their_admission(label_harness, entry, caller):
    """Guard edit: apply the create ceiling to an admitted admin."""
    h = label_harness
    _, path, _ = entry
    _, field, default, table = FIVE_DOORS[entry]
    key = _key(h, "admin_agent", bound=caller == "bound-admin")[1] if caller in {"key-admin", "bound-admin"} else None
    claims = {"agent_id": "declared-admin", "permission_profile": "admin_agent"} if caller == "declared-admin" else {}
    for level in (*WITHIN, *LEVELS):
        before = _snapshot(h)
        status, body = _call(h, entry, level, key=key, claims=claims)
        if caller == "owner" and path == PROPOSALS:
            assert (status, body) == (400, {"detail": "agent identity is required for memory proposals"})
            assert _snapshot(h) == before
        elif caller == "bound-admin" and path in OPERATORS:
            assert status == 403
            assert body["policy_decision"]["reasons"] == ["unbound_operator_key_required"]
            after = _snapshot(h)
            _assert_other_tables_equal(before, after, {"agent_api_keys", "event_log"})
        else:
            assert status == HTTP[entry].success, body
            row = body if field is None else body[field]
            _assert_stored_sensitivity(h, table, row, level or default)


def _mcp_result(context, name, arguments):
    server = MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    wire = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments},
    })["result"]
    return wire["isError"], json.loads(wire["content"][0]["text"])


@pytest.mark.parametrize(("tool", "action", "required"), [
    ("alice_vnext_propose_memory", "memory.propose", {"canonical_text": "A synthetic proposal note."}),
    ("alice_vnext_queue_task", "queue_task.create", {"title": "Synthetic task", "task_type": "summarize",
                                                   "instructions": "Summarize the synthetic notes."}),
])
@pytest.mark.parametrize("role", ["trusted_local_agent", "admin_agent", None])
def test_keyless_mcp_creates_use_the_declared_role(label_harness, tool, action, required, role):
    """Guard edit: change the MCP policy refusal to invalid_request."""
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    agent_id = "declared-mcp"
    identity = {"agent_id": agent_id, "permission_profile": role} if role else {}
    for level in (*LEVELS, *WITHIN):
        before = _snapshot(h)
        arguments = {**required, **identity, **({"sensitivity": level} if level is not None else {})}
        error, payload = _mcp_result(context, tool, arguments)
        if role is None and action == "memory.propose":
            assert error and payload["error"]["code"] == "invalid_request"
            assert _snapshot(h) == before
        elif role == "trusted_local_agent" and level in LEVELS:
            assert error and payload["error"] == {
                "code": "not_permitted", "message": "The tool request could not be processed",
            }
            after = _snapshot(h)
            decision = _new_policy_refusal(before, after, action)
            _assert_only_refusal_recorded(
                before, after, {"policy_decision": decision}, None, None, key=None, declared_agent_id=agent_id,
            )
        else:
            assert not error, payload
            row = payload["proposal"] if action == "memory.propose" else payload
            table = "memories" if action == "memory.propose" else "task_queue"
            _assert_stored_sensitivity(h, table, row, level or "unknown")


@pytest.mark.parametrize("caller", ["key", "declared", "admin", "key-admin", "no-identity"])
def test_cli_proposal_uses_its_stored_label(label_harness, monkeypatch, capsys, caller):
    """Guard edit: change the CLI ceiling refusal status in standard output."""
    h = label_harness
    profile = "admin_agent" if caller in {"admin", "key-admin"} else "trusted_local_agent"
    if caller in {"key", "key-admin"}:
        agent_id, key = _key(h, profile)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    else:
        agent_id = "declared-cli"
    args = ["--database-url", h.urls["app"], "--user-id", str(h.user_id),
            "vnext", "agents", "propose-memory", "--title", "Synthetic proposal",
            "--canonical-text", "A synthetic proposal note."]
    if caller != "no-identity":
        args += ["--agent-id", agent_id, "--permission-profile", profile]
    for level in (*LEVELS, *WITHIN):
        before = _snapshot(h)
        rc = cli_main([*args, *([] if level is None else ["--sensitivity", level])])
        capture = capsys.readouterr()
        if caller == "no-identity":
            assert rc == 1 and json.loads(capture.err)["error"]["code"] == "invalid_request"
            assert _snapshot(h) == before
        elif profile == "trusted_local_agent" and level in LEVELS:
            assert rc == 1
            assert json.loads(capture.out) == {"status": "rejected", "reason": "sensitivity_above_agent_ceiling"}
            assert _snapshot(h) == before
        else:
            assert rc == 0, capture
            _assert_stored_sensitivity(h, "memories", json.loads(capture.out)["proposal"], level or "unknown")


def test_ceiling_respects_existing_http_refusal_order(label_harness):
    """Guard edit: change the HTTP sensitivity refusal to status 422."""
    h = label_harness
    _, key = _key(h, "trusted_local_agent")
    before = _snapshot(h)
    proposal = {
        "title": "Synthetic admission control", "canonical_text": SYNTHETIC_CREDENTIAL, "sensitivity": "confidential",
    }
    status, body, _ = h.request("POST", PROPOSALS, payload=proposal, key=key)
    assert (status, body) == (400, {"detail": "vNext memory proposal carries credential material"})
    _assert_other_tables_equal(before, _snapshot(h), {"agent_api_keys"})

    payload = {"title": "Synthetic loop", "source_id": str(uuid4()), "sensitivity": "confidential"}
    status, body, _ = h.request("POST", "/v0/vnext/open-loops", payload=payload, key=key)
    _assert_ceiling(status, body, action="open_loop.create")

    with h.store() as store:
        store.create_project({"name": "Synthetic slug", "slug": "synthetic-slug"})
        store.upsert_brain_charter({"content_markdown": "A stored fixture charter.", "sensitivity": "confidential"})
    for label, expected in [("private", 409), ("confidential", 403)]:
        status, body, _ = h.request("POST", "/v0/vnext/projects", key=key, payload={
            "name": "Synthetic slug", "slug": "synthetic-slug", "sensitivity": label,
        })
        assert status == expected
        if expected == 403:
            _assert_ceiling(status, body, action="project.create")

    status, body, _ = h.request("PUT", "/v0/vnext/settings/brain-charter", key=key, payload={
        "content_markdown": "A synthetic charter request.", "sensitivity": "confidential",
    })
    assert status == 403
    assert body["policy_decision"]["reasons"] == ["brain_charter_outside_caller_limits"]
