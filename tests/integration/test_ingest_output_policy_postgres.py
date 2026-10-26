"""PostgreSQL: ``POST /v0/vnext/agents/ingest-output`` writes only what the caller's agent policy allows.

The route builds a policy decision for ``source.capture`` before it calls the connector service. These tests drive the
mounted application against PostgreSQL and count the rows before and after:

* a request the policy blocks answers what ``POST /v0/vnext/sources`` answers for a blocked request (HTTP 403, the
  ``agent policy blocked this action`` detail and the decision), and writes no source, chunk, artifact, provenance link or
  memory. The policy events of the refusal are stored, because the route returns inside the connection;
* a request the policy allows (a ``trusted_local_agent`` key, and a keyless call that declares that profile) still
  writes a source, a ``needs_review`` artifact and a provenance link, and a candidate memory when ``propose_memory`` is set;
* credential material in the title, the content or the source refs answers HTTP 400 with the route's fixed text, repeats
  none of the request, and writes nothing (it used to answer HTTP 500).

Mutations, each one alone: the ``blocked`` check taken out of ``ingest_vnext_agent_output``, with the service refusal in place
(the service still refuses, so the answer is 403 and no row is written, but the policy events roll back with the connection and
``test_the_policy_events_of_a_refusal_are_stored`` fails); that check and the service refusal both taken out (the 403 tests
fail); the ``VNextCaptureValidationError`` class taken out of the route's ``except`` (the 400 tests fail).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_vnext_live_workspace_api import seed_user

INGEST = "/v0/vnext/agents/ingest-output"
SOURCES = "/v0/vnext/sources"
BLOCKED_DETAIL = "agent policy blocked this action"
INVALID_DETAIL = "vNext agent output ingest request is invalid"
# Built, not written out, so the source carries no scanner-shaped token.
TOKEN = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
TABLES = ("sources", "source_chunks", "generated_artifacts", "provenance_links", "memories")


def _asgi_post(path: str, payload: dict[str, Any], authorization: str | None = None) -> tuple[int, Any, BaseException | None]:
    """POST through the application's ASGI entry. The server-error middleware sends a 500 and then raises the error again,
    so the status and the raised error are both returned."""

    messages: list[dict[str, Any]] = []
    body = json.dumps(payload).encode()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    raised: BaseException | None = None
    try:
        anyio.run(main_module.app, scope, receive, send)
    except BaseException as exc:  # noqa: BLE001 - the middleware raises the error it already answered
        raised = exc
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    try:
        answer: Any = json.loads(raw)
    except ValueError:
        answer = raw.decode("utf-8", "replace")
    return int(start["status"]), answer, raised


@pytest.fixture
def env(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user_id = seed_user(url, email="ingest-policy@example.invalid")
    for name, module in list(sys.modules.items()):
        if name.startswith("alicebot_api") and module is not None and hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=url))
    return SimpleNamespace(url=url, user_id=user_id, user=str(user_id))


def _key(env, profile: str, agent_id: str, project_scope: str | None = None) -> str:
    with user_connection(env.url, env.user_id) as conn:
        _record, raw = create_agent_key(
            PostgresVNextStore(conn),
            user_id=env.user_id,
            agent_id=agent_id,
            permission_profile=profile,
            project_scope=project_scope,
        )
    return f"Bearer {raw}"


def _state(env: Any) -> dict[str, Any]:
    """The rows the route can write, and every event by type."""

    with user_connection(env.url, UUID(env.user)) as conn:
        counts = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in TABLES}
        events = Counter(row["event_type"] for row in conn.execute("SELECT event_type FROM event_log").fetchall())
    return {**counts, "events": events}


def _body(env: Any, agent_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "user_id": env.user,
        "agent_id": agent_id,
        "title": "Kiln schedule note",
        "content": "The kiln schedule moves to Tuesday.",
        **fields,
    }


def _new_events(before: dict[str, Any], after: dict[str, Any]) -> Counter[str]:
    return after["events"] - before["events"]


def _without_trace(answer: dict[str, Any]) -> dict[str, Any]:
    decision = {key: value for key, value in answer["policy_decision"].items() if key != "trace_id"}
    return {**answer, "policy_decision": decision}


def _nothing_written(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert {table: after[table] for table in TABLES} == {table: before[table] for table in TABLES}


# -- a blocked decision writes nothing --------------------------------------------------------------------------


@pytest.mark.parametrize("propose_memory", [False, True], ids=["artifact only", "propose memory"])
def test_a_read_only_key_gets_the_sibling_refusal_and_writes_nothing(env, propose_memory: bool) -> None:
    auth = _key(env, "read_only_agent", "probe-agent")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "probe-agent", propose_memory=propose_memory), auth)
    after = _state(env)
    assert raised is None
    assert status == 403, answer
    assert answer["detail"] == BLOCKED_DETAIL
    assert answer["policy_decision"]["decision"] == "blocked"
    assert answer["policy_decision"]["action"] == "source.capture"
    assert "read_only_agent_cannot_write" in answer["policy_decision"]["reasons"]
    _nothing_written(before, after)

    # The refusal is the one POST /v0/vnext/sources gives the same key for the same labels.
    sibling_status, sibling = _asgi_post(
        SOURCES, {"user_id": env.user, "agent_id": "probe-agent", "raw_text": "Plain note.", "domain": "project", "sensitivity": "private"}, auth
    )[:2]
    assert sibling_status == status
    assert _without_trace(sibling) == _without_trace(answer)


def test_the_policy_events_of_a_refusal_are_stored(env) -> None:
    auth = _key(env, "read_only_agent", "probe-agent")
    before = _state(env)
    status, answer, _raised = _asgi_post(INGEST, _body(env, "probe-agent"), auth)
    assert status == 403, answer
    after = _state(env)
    new = _new_events(before, after)
    assert new["policy.decision"] == 1 and new["agent.policy_blocked"] == 1
    assert set(new) <= {"policy.decision", "agent.policy_blocked", "agent.identity_upserted"}, new
    with user_connection(env.url, env.user_id) as conn:
        rows = conn.execute(
            "SELECT event_type, target_type, target_id, actor_id, payload_json FROM event_log WHERE event_type = 'agent.policy_blocked'"
        ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["target_type"], rows[0]["target_id"], rows[0]["actor_id"]) == ("connector", "agent_output", "probe-agent")
    assert rows[0]["payload_json"]["policy_decision"]["trace_id"] == answer["policy_decision"]["trace_id"]


def test_a_key_whose_domains_exclude_the_requested_domain_is_refused_and_writes_nothing(env) -> None:
    auth = _key(env, "project_scoped_agent", "scoped-agent", project_scope="Alpha")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "scoped-agent", domain="health", project_scope=["Alpha"]), auth)
    assert raised is None
    assert status == 403, answer
    assert answer["detail"] == BLOCKED_DETAIL
    assert {"restricted_domain_filtered", "all_requested_domains_restricted"} <= set(answer["policy_decision"]["reasons"])
    _nothing_written(before, _state(env))


@pytest.mark.parametrize("sensitivity", ["confidential", "regulated"])
def test_a_key_whose_sensitivity_labels_exclude_the_request_is_refused_and_writes_nothing(env, sensitivity: str) -> None:
    """``memory_proposal_agent`` may not capture, and its labels stop at ``private``: both reasons are on the decision."""

    auth = _key(env, "memory_proposal_agent", "proposal-agent")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "proposal-agent", sensitivity=sensitivity), auth)
    assert raised is None
    assert status == 403, answer
    assert {"restricted_sensitivity_filtered", "memory_proposal_agent_cannot_mutate"} <= set(answer["policy_decision"]["reasons"])
    _nothing_written(before, _state(env))


@pytest.mark.parametrize("profile", ["project_scoped_agent", "trusted_local_agent"])
def test_a_key_bound_to_one_project_that_asks_for_another_is_refused_and_writes_nothing(env, profile: str) -> None:
    """The key's binding is judged when the key is resolved, before the policy decision, so this refusal is not the policy
    body. Whichever layer answers, a refusal writes nothing, and the same key writes inside its binding."""

    auth = _key(env, profile, "bound-agent", project_scope="Alpha")
    before = _state(env)
    for asked in (["Beta"], ["Alpha", "Beta"]):
        status, answer, raised = _asgi_post(INGEST, _body(env, "bound-agent", project_scope=asked), auth)
        assert raised is None
        assert status == 403, answer
        if "policy_decision" in answer:
            assert answer["policy_decision"]["decision"] == "blocked"
        _nothing_written(before, _state(env))
    status, answer, _raised = _asgi_post(INGEST, _body(env, "bound-agent", project_scope=["Alpha"]), auth)
    assert status == 201, answer
    assert _state(env)["sources"] == before["sources"] + 1


def test_a_read_only_key_bound_to_a_project_is_refused_inside_its_binding_and_writes_nothing(env) -> None:
    auth = _key(env, "read_only_agent", "bound-reader", project_scope="Alpha")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "bound-reader", project_scope=["Alpha"], propose_memory=True), auth)
    assert raised is None
    assert status == 403, answer
    assert answer["detail"] == BLOCKED_DETAIL
    assert "read_only_agent_cannot_write" in answer["policy_decision"]["reasons"]
    _nothing_written(before, _state(env))


def test_a_blocked_decision_stops_a_request_that_also_carries_credential_material(env) -> None:
    """The policy answers first, so the caller learns nothing about the content, and the token is not echoed."""

    auth = _key(env, "read_only_agent", "probe-agent")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "probe-agent", content=f"Deploy with {TOKEN} tonight."), auth)
    assert raised is None
    assert status == 403, answer
    assert TOKEN not in json.dumps(answer)
    _nothing_written(before, _state(env))


# -- an allowed decision still writes ---------------------------------------------------------------------------


def test_a_trusted_local_agent_key_still_writes_a_source_an_artifact_and_a_link(env) -> None:
    auth = _key(env, "trusted_local_agent", "trusted-agent")
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "trusted-agent", output_type="sprint_summary"), auth)
    after = _state(env)
    assert raised is None
    assert status == 201, answer
    assert answer["policy_decision"]["decision"] == "allowed"
    assert answer["source_id"] and answer["artifact_id"]
    assert answer["memory_id"] is None
    assert after["sources"] == before["sources"] + 1
    assert after["generated_artifacts"] == before["generated_artifacts"] + 1
    assert after["provenance_links"] == before["provenance_links"] + 1
    assert after["source_chunks"] > before["source_chunks"]
    assert after["memories"] == before["memories"]
    with user_connection(env.url, env.user_id) as conn:
        artifact = conn.execute("SELECT status FROM generated_artifacts WHERE id = %s", (answer["artifact_id"],)).fetchone()
    assert artifact["status"] == "needs_review"


def test_a_trusted_local_agent_key_that_proposes_a_memory_still_writes_a_candidate(env) -> None:
    auth = _key(env, "trusted_local_agent", "trusted-agent")
    before = _state(env)
    status, answer, _raised = _asgi_post(INGEST, _body(env, "trusted-agent", propose_memory=True), auth)
    assert status == 201, answer
    assert answer["memory_id"]
    assert _state(env)["memories"] == before["memories"] + 1
    with user_connection(env.url, env.user_id) as conn:
        memory = conn.execute("SELECT status FROM memories WHERE id = %s", (answer["memory_id"],)).fetchone()
    assert memory["status"] == "candidate"


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param({"agent_id": "hermes"}, id="hermes defaults to trusted_local_agent"),
        pytest.param({"agent_id": "kiln-bot", "permission_profile": "trusted_local_agent"}, id="declared trusted_local_agent"),
        pytest.param({"agent_id": "kiln-bot", "permission_profile": "admin_agent"}, id="declared admin_agent"),
    ],
)
def test_a_keyless_call_that_declares_a_profile_which_may_write_still_writes(env, declared: dict[str, str]) -> None:
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, **declared, propose_memory=True))
    after = _state(env)
    assert raised is None
    assert status == 201, answer
    assert answer["policy_decision"]["decision"] in {"allowed", "allowed_with_filtering"}
    assert after["sources"] == before["sources"] + 1
    assert after["generated_artifacts"] == before["generated_artifacts"] + 1
    assert after["memories"] == before["memories"] + 1


def test_a_keyless_call_that_declares_no_profile_is_held_to_read_only_agent(env) -> None:
    """With no key and no profile, an ``agent_id`` other than ``hermes`` and ``openclaw`` is a ``read_only_agent``, which
    the MCP tool and the command line already refused and the sources route refuses."""

    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "kiln-bot"))
    assert raised is None
    assert status == 403, answer
    assert answer["detail"] == BLOCKED_DETAIL
    sibling_status = _asgi_post(SOURCES, {"user_id": env.user, "agent_id": "kiln-bot", "raw_text": "Plain note."})[0]
    assert sibling_status == 403
    _nothing_written(before, _state(env))


# -- credential material is a 400 that writes nothing ----------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"source_refs": ["source:abc", TOKEN]}, id="source ref"),
        pytest.param({"content": f"Deploy with {TOKEN} tonight."}, id="content"),
        pytest.param({"title": f"Token {TOKEN}"}, id="title"),
    ],
)
@pytest.mark.parametrize("keyed", [True, False], ids=["trusted key", "keyless"])
def test_credential_material_is_a_400_that_writes_nothing_and_repeats_nothing(env, fields: dict[str, Any], keyed: bool) -> None:
    auth = _key(env, "trusted_local_agent", "trusted-agent") if keyed else None
    before = _state(env)
    status, answer, raised = _asgi_post(INGEST, _body(env, "trusted-agent" if keyed else "hermes", **fields), auth)
    after = _state(env)
    assert raised is None, repr(raised)
    assert status == 400, answer
    assert answer == {"detail": INVALID_DETAIL}
    assert TOKEN not in json.dumps(answer)
    assert after == before

    # The same request without the token writes.
    clean = {key: value for key, value in _body(env, "trusted-agent" if keyed else "hermes").items()}
    status, answer, _raised = _asgi_post(INGEST, clean, auth)
    assert status == 201, answer
