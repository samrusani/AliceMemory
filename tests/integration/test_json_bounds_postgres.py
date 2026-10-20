"""PostgreSQL: the HTTP write doors refuse JSON that cannot be stored, and a row stored too deep no longer breaks a route.

``tests/unit/test_write_doors_json_bounds.py`` and its twins hold the services to the limits without a database. These tests
drive the mounted application against PostgreSQL:

* each door that stores caller JSON answers a value nested past 64 levels as it answers other bad input (HTTP 400 on the
  connector routes; HTTP 422 on a field that no service holds), and NaN, Infinity and ``1e999`` are HTTP 422 everywhere; a
  refused request writes nothing. The ``source_refs`` of the memory commit, the proposal and the agent output doors take
  strings only, so a ref that nests, to the limit or past it, is HTTP 422 as a non-string and never reaches the depth check;
* a row already stored with JSON nested far past the readers' recursion (written by SQL, as an older door or a hand edit
  could leave it) no longer makes the memory audit route, the recent commits route, ``alice_memory_review`` or
  ``alice_explain`` fail with ``RecursionError``, for the owner and for a key with limits. A row the route can walk is read
  whole (400 levels). One it cannot (1500 and 5000) runs again with the part of the row below 256 levels read as one
  placeholder string, the rest whole, and so does one that the decoder cannot read (12,000), which is cut at once;
* a write is never run twice: ``forget`` and ``expire`` of a row nested 1,500 levels fail as they did before and change
  nothing, of a row the decoder reads (400) work and keep the row whole, and of a row the decoder cannot read (12,000) work
  and write the placeholder back, into the row and into the ``memory.updated`` event.

Mutations, each one alone: ``set_json_loads(loads_stored)`` removed from ``alicebot_api.db`` (the 5000 level test fails),
``RetryDeepRowMiddleware`` removed from the application (the 1500 level route tests fail), ``retry_cutting_deep_json`` removed
from the MCP boundary (the MCP test fails);
``bounded_json`` removed from a field type (``test_a_typed_field_refuses_...``); the service checks of ``_object_tuple``,
``MemoryProposal``, ``normalize_connector_item`` or ``apply_continuity_correction`` removed (the matching door test fails).
"""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.json_bounds import DEEP_PLACEHOLDER, MAX_STORED_JSON_DEPTH
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_vnext_live_workspace_api import seed_user

LIMIT = MAX_STORED_JSON_DEPTH
TEXT = "The cobalt door opens on Monday."


def _call(method: str, path: str, *, body: bytes = b"", query: dict[str, str] | None = None, authorization: str | None = None) -> tuple[int, Any]:
    messages: list[dict[str, object]] = []
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": urlencode(query or {}).encode(), "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


def _post(path: str, payload: dict[str, Any], **kwargs: Any) -> tuple[int, Any]:
    return _call("POST", path, body=json.dumps(payload).encode(), **kwargs)


def _nested(depth: int, leaf: object = "x") -> object:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


@pytest.fixture
def app(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user_id = seed_user(url, email="json-bounds@example.com")
    for name, module in list(sys.modules.items()):
        if name.startswith("alicebot_api") and module is not None and hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=url))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")

    class App:
        pass

    handle = App()
    handle.url, handle.admin_url, handle.user_id, handle.user = url, migrated_database_urls["admin"], user_id, str(user_id)
    return handle


def _counts(app) -> tuple[int, ...]:
    """Rows of memories, revisions, events, sources and provenance links. A connector route records its own lifecycle events
    for a refused batch, so the connector test reads ``_stored`` (no events) instead."""

    with psycopg.connect(app.admin_url) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("memories", "memory_revisions", "event_log", "sources", "provenance_links")
        )


def _stored(app) -> tuple[int, ...]:
    with psycopg.connect(app.admin_url) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("memories", "memory_revisions", "sources", "source_chunks", "provenance_links")
        )


def _event_types(app) -> list[str]:
    with psycopg.connect(app.admin_url) as conn:
        return sorted(row[0] for row in conn.execute("SELECT DISTINCT event_type FROM event_log"))


AGENT = {"agent_id": "hermes", "agent_type": "personal_assistant", "permission_profile": "trusted_local_agent", "project_scope": ["Alice"]}


def _commit_body(app, ref: object, key: str) -> dict[str, Any]:
    return {"user_id": app.user, "agent": AGENT, "title": f"t {key}", "canonical_text": f"Synthetic fact {key} about the cobalt door.",
            "idempotency_key": key, "source_refs": [ref]}


# The memory commit door.


def test_the_commit_route_refuses_a_ref_nested_past_the_limit_with_a_422_and_writes_nothing(app) -> None:
    before = _counts(app)
    status, answer = _post("/v0/vnext/memories/commit", _commit_body(app, _nested(LIMIT + 1), "deep"))
    assert status == 422, answer
    assert answer["detail"][0]["type"] == "string_type"
    assert answer["detail"][0]["loc"] == ["body", "source_refs", 0]
    assert _counts(app) == before


@pytest.mark.parametrize(
    ("raw", "location"),
    (
        (b'{"score": NaN}', ["body", "source_refs", 0, "score"]),
        (b"[Infinity]", ["body", "source_refs", 0, 0]),
        (b"1e999", ["body", "source_refs", 0]),
    ),
)
def test_the_commit_route_answers_a_number_that_is_not_finite_with_a_422_and_writes_nothing(app, raw: bytes, location: list) -> None:
    before = _counts(app)
    body = (
        b'{"user_id": "' + app.user.encode() + b'", "agent": ' + json.dumps(AGENT).encode() +
        b', "title": "t", "canonical_text": "Synthetic fact about the cobalt door.", "source_refs": [' + raw + b"]}"
    )
    status, answer = _call("POST", "/v0/vnext/memories/commit", body=body)
    assert status == 422, answer
    # A number that is not finite is refused by the body scan before the string model runs.
    assert answer == {"detail": [{"type": "finite_number", "loc": location, "msg": "Input should be a finite number"}]}
    assert _counts(app) == before


def test_a_ref_nested_exactly_to_the_limit_is_refused_by_the_commit_route_as_a_non_string(app) -> None:
    """An object entry is refused at the commit door, including one nested only to the limit.

    v0.20.2 stored that shape. A backup that already holds it still restores (see the source-ref shape tests).
    """

    before = _counts(app)
    status, answer = _post("/v0/vnext/memories/commit", _commit_body(app, _nested(LIMIT), "limit"))
    assert status == 422, answer
    assert answer["detail"][0]["type"] == "string_type"
    assert _counts(app) == before


# The proposal and the agent output doors.


def test_the_proposal_route_answers_a_nested_ref_with_a_422_and_takes_a_string(app) -> None:
    before = _counts(app)
    body = {"user_id": app.user, "agent": AGENT, "title": "t", "canonical_text": "A proposal about the cobalt door.",
            "source_refs": [_nested(LIMIT + 1)]}
    status, answer = _post("/v0/vnext/memory-proposals", body)
    assert status == 422, answer
    assert answer["detail"][0]["type"] == "string_type"
    status, answer = _call("POST", "/v0/vnext/memory-proposals", body=json.dumps(body).replace(json.dumps(_nested(LIMIT + 1)), "[NaN]").encode())
    assert status == 422 and answer["detail"][0]["type"] == "finite_number"
    ok = {**body, "source_refs": ["source:cobalt-door"]}
    status, answer = _post("/v0/vnext/memory-proposals", ok)
    assert status == 201, answer
    assert _counts(app)[0] == before[0] + 1


def test_the_agent_output_route_refuses_what_cannot_be_stored(app) -> None:
    before = _counts(app)
    body = {"user_id": app.user, "agent_id": "hermes", "agent_type": "personal_assistant", "title": "Output", "content": "An output.",
            "project_scope": ["Alice"], "source_refs": [_nested(LIMIT + 1)]}
    status, answer = _post("/v0/vnext/agents/ingest-output", body)
    assert status == 422, answer
    assert answer["detail"][0]["type"] == "string_type"
    assert _counts(app) == before
    status, answer = _post("/v0/vnext/agents/ingest-output", {**body, "source_refs": ["source:cobalt-door"]})
    assert status == 201, answer


# A field no service holds, and the connector routes.


def test_a_typed_field_refuses_depth_and_non_finite_numbers_with_a_422_that_does_not_echo_the_value(app) -> None:
    nested = b'{"k":' * (LIMIT + 1) + b"1" + b"}" * (LIMIT + 1)
    status, answer = _call("POST", "/v0/vnext/context-packs", body=b'{"user_id": "' + app.user.encode() + b'", "query": "cobalt", "options": ' + nested + b"}")
    assert status == 422
    assert answer["detail"] == [{"type": "json_too_deep", "loc": ["body", "options"], "msg": "Value must nest at most 64 levels of arrays and objects"}]
    status, answer = _call("POST", "/v0/vnext/context-packs", body=b'{"user_id": "' + app.user.encode() + b'", "query": "cobalt", "scope": {"a": [NaN]}}')
    assert status == 422 and answer["detail"][0]["type"] == "finite_number"
    inside = b'{"k":' * (LIMIT - 1) + b"1" + b"}" * (LIMIT - 1)
    status, answer = _call("POST", "/v0/vnext/context-packs", body=b'{"user_id": "' + app.user.encode() + b'", "query": "cobalt", "options": ' + inside + b"}")
    assert status in (200, 201), answer


def test_the_connector_routes_refuse_what_cannot_be_stored(app) -> None:
    before = _stored(app)
    item = {"filename": "a.txt", "text": "Some text about the cobalt door.", "extra": _nested(LIMIT + 1)}
    status, answer = _post("/v0/vnext/connectors/pdf_document/sync", {"user_id": app.user, "items": [item]})
    assert status == 400, answer
    assert _stored(app) == before
    good = {"filename": "b.txt", "text": "Other text about the cobalt door.", "extra": _nested(LIMIT - 5)}
    status, answer = _post("/v0/vnext/connectors/pdf_document/sync", {"user_id": app.user, "items": [good, item]})
    # A sync reports each item on its own (HTTP 207): the item inside the limits is stored, the other one is not.
    assert status == 207, answer
    stored = _stored(app)
    assert stored[2] == before[2] + 1, "one source: the item inside the limits"
    assert (answer["status"], answer["imported_count"], answer["failed_count"], answer["failed_external_ids"]) == (
        "partial", 1, 1, ["a.txt"])
    assert answer["errors"] == ["Connector item could not be imported"]
    status, answer = _call("PATCH", "/v0/vnext/connectors/pdf_document/config", body=json.dumps(
        {"user_id": app.user, "config_json": {"deep": _nested(LIMIT + 1)}}).encode())
    assert status == 400, answer


def test_the_correction_route_refuses_what_cannot_be_stored(app) -> None:
    with user_connection(app.url, app.user_id) as conn:
        store = ContinuityStore(conn)
        capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep conservative rollout", explicit_signal="decision",
            admission_posture="DERIVED", admission_reason="explicit_signal_decision")
        obj = store.create_continuity_object(
            capture_event_id=capture["id"], object_type="Decision", status="active", title="Decision: Keep conservative rollout",
            body={"decision_text": "Keep conservative rollout"}, provenance={"thread_id": "thread-1"}, confidence=0.94)
    path = f"/v0/continuity/review-queue/{obj['id']}/corrections"
    deep: object = {"text": "x"}
    for _ in range(LIMIT):
        deep = {"k": deep}
    status, answer = _post(path, {"user_id": app.user, "action": "edit", "provenance": deep})
    assert status == 400, answer
    status, answer = _call("POST", path, body=b'{"user_id": "' + app.user.encode() + b'", "action": "edit", "body": {"text": "x", "score": NaN}}')
    assert status == 422 and answer["detail"][0]["type"] == "finite_number"
    status, answer = _post(path, {"user_id": app.user, "action": "edit", "title": "Decision: Updated", "body": {"decision_text": "Updated"}})
    assert status == 200, answer


# A row stored too deep.


def _store_a_deep_row(app, depth: int, key: str = "stored-deep") -> str:
    request = memory_commit_request_from_payload({"title": "cobalt door", "canonical_text": TEXT, "idempotency_key": key}, user_id=app.user)
    with user_connection(app.url, app.user_id) as conn:
        result = VNextMemoryCommitService(PostgresVNextStore(conn)).commit(identity=None, request=request)
    memory_id = str(result["memory"]["id"])
    deep = "[" * depth + '"x"' + "]" * depth
    with psycopg.connect(app.admin_url) as conn:
        conn.execute("UPDATE memories SET metadata_json = jsonb_set(metadata_json, '{note}', %s::jsonb) WHERE id = %s", (deep, memory_id))
    return memory_id


# What the audit route answers for ``note``: a row the routes can walk is whole, one they cannot is cut on the second attempt.
READ = {400: (400, "x"), 1500: (255, DEEP_PLACEHOLDER), 5000: (255, DEEP_PLACEHOLDER), 12000: (255, DEEP_PLACEHOLDER)}


@pytest.mark.parametrize("depth", sorted(READ))
def test_a_row_stored_too_deep_does_not_break_the_audit_and_recent_commits_routes_for_the_owner(app, depth: int) -> None:
    memory_id = _store_a_deep_row(app, depth)
    status, recent = _call("GET", "/v0/vnext/memories/recent-commits", query={"user_id": app.user, "limit": "5"})
    assert status == 200 and memory_id in json.dumps(recent)
    status, audit = _call("GET", f"/v0/vnext/memories/{memory_id}/audit", query={"user_id": app.user})
    assert status == 200 and audit["memory"]["canonical_text"] == TEXT
    note = audit["memory"]["metadata_json"]["note"]
    levels = 0
    while isinstance(note, list):
        levels += 1
        note = note[0]
    assert (levels, note) == READ[depth]
    assert audit["memory"]["metadata_json"]["agentic_memory"]["kind"] == "agentic_memory_commit"


@pytest.mark.parametrize("profile", ("trusted_local_agent", "admin_agent"))
def test_a_row_stored_too_deep_does_not_break_the_routes_for_a_key_either(app, profile: str) -> None:
    memory_id = _store_a_deep_row(app, 1500)
    with user_connection(app.url, app.user_id) as conn:
        _record, raw_key = create_agent_key(PostgresVNextStore(conn), user_id=app.user_id, agent_id=f"{profile}-reader", permission_profile=profile)
    header = f"Bearer {raw_key}"
    status, recent = _call("GET", "/v0/vnext/memories/recent-commits", query={"user_id": app.user, "limit": "5"}, authorization=header)
    assert status == 200, recent
    status, audit = _call("GET", f"/v0/vnext/memories/{memory_id}/audit", query={"user_id": app.user}, authorization=header)
    assert status == 200, audit


def test_a_row_stored_too_deep_does_not_break_the_mcp_review_and_explain_tools(app) -> None:
    memory_id = _store_a_deep_row(app, 1500)
    context = MCPRuntimeContext(database_url=app.url, user_id=app.user_id)
    review = call_mcp_tool(context, name="alice_memory_review", arguments={"review_item_id": memory_id})
    assert TEXT in json.dumps(review)
    explain = call_mcp_tool(context, name="alice_explain", arguments={"memory_id": memory_id})
    assert TEXT in json.dumps(explain)
    assert call_mcp_tool(context, name="alice_recall", arguments={"query": "cobalt door"})


@pytest.mark.parametrize("action", ("forget", "expire"))
def test_a_write_is_not_run_twice_and_writes_back_what_it_read(app, action: str) -> None:
    """``forget`` and ``expire`` are writes: they are not run a second time when a reader overflows on the row."""

    path = f"/v0/vnext/memories/{action}"

    def run(depth: int) -> tuple[object, str, tuple[int, object], list[str], bool]:
        memory_id = _store_a_deep_row(app, depth, key=f"deep-{depth}")
        try:
            outcome: object = _post(path, {"user_id": app.user, "memory_id": memory_id, "reason": "probe"})[0]
        except RecursionError as error:
            outcome = type(error).__name__
        with psycopg.connect(app.admin_url) as conn:
            note = conn.execute("SELECT metadata_json->'note' FROM memories WHERE id = %s", (memory_id,)).fetchone()[0]
            events = [row[0] for row in conn.execute("SELECT event_type FROM event_log WHERE target_id = %s", (memory_id,))]
            in_event = conn.execute("SELECT bool_or(payload_json::text LIKE '%%nested too deeply%%') FROM event_log WHERE target_id = %s",
                                    (memory_id,)).fetchone()[0]
        levels = 0
        while isinstance(note, list):
            levels += 1
            note = note[0]
        return outcome, memory_id, (levels, note), events, bool(in_event)

    # A row the decoder reads and the writer can walk: kept whole.
    outcome, _, stored, events, in_event = run(400)
    assert outcome == 200 and stored == (400, "x") and not in_event
    # A row nested past what the writer walks: the write fails as it did before, writes nothing, and ran once.
    outcome, _, stored, events, in_event = run(1500)
    assert outcome == "RecursionError" and stored == (1500, "x") and not in_event
    assert not any(name in events for name in ("memory.updated", f"agent.memory_{'forgotten' if action == 'forget' else 'expired'}"))
    # A row the decoder cannot read is read cut, so the write puts the placeholder back, into the row and the event.
    outcome, _, stored, events, in_event = run(12000)
    assert outcome == 200 and stored == (255, DEEP_PLACEHOLDER) and in_event and "memory.updated" in events


@pytest.mark.parametrize("where", ("memories.value", "sources.metadata_json"))
def test_a_context_pack_request_is_read_again_with_its_body_when_a_row_is_too_deep(app, where: str) -> None:
    """``POST /v0/vnext/context-packs`` is the one POST route the layer may run again, with the same body. The pack builder
    reads ``memories.value`` and ``sources.metadata_json`` for the rows it returns, and a value nested 1,500 levels in either
    overflows it: without the layer the request fails with ``RecursionError`` and no pack is compiled. With it the answer is
    201, it names the row, and exactly one compile event was written (the first attempt rolled back)."""

    from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService

    memory_id = _store_a_deep_row(app, 10, key="pack-deep")
    with user_connection(app.url, app.user_id) as conn:
        captured = VNextCaptureService(PostgresVNextStore(conn)).capture_source(SourceCaptureInput(
            source_type="note", title="cobalt door note", raw_text="The cobalt door opens on Monday for the vendor.",
            connector_name="manual", external_id="pack-deep", domain="project", sensitivity="private"))
    source_id = str(captured.source_id)
    table, column = where.split(".")
    with psycopg.connect(app.admin_url, autocommit=True) as conn:
        conn.execute(f"UPDATE {table} SET {column} = jsonb_set(coalesce({column}, '{{}}'::jsonb), '{{note}}', %s::jsonb) WHERE id = %s",
                     ("[" * 1500 + '"x"' + "]" * 1500, memory_id if table == "memories" else source_id))
    before = _event_counts(app)
    status, answer = _post("/v0/vnext/context-packs", {"user_id": app.user, "query": "cobalt door", "options": {"include_sources": True}})
    assert status == 201, answer
    assert (memory_id if table == "memories" else source_id) in json.dumps(answer)
    assert _new_events(before, _event_counts(app)).get("retrieval.context_pack_compiled") == 1


def _event_counts(app) -> dict[str, int]:
    with psycopg.connect(app.admin_url) as conn:
        return dict(conn.execute("SELECT event_type, count(*) FROM event_log GROUP BY 1").fetchall())


def _new_events(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {kind: after.get(kind, 0) - before.get(kind, 0) for kind in after if after.get(kind, 0) != before.get(kind, 0)}


def _tool_calls(memory_id: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("alice_recall", {"query": "cobalt door"}),
        ("alice_resume", {"query": "cobalt"}),
        ("alice_context_pack", {"query": "cobalt door"}),
        ("alice_recent_decisions", {}),
        ("alice_memory_review", {"review_item_id": memory_id}),
        ("alice_memory_review", {"status": "all"}),
        ("alice_explain", {"memory_id": memory_id}),
        ("alice_open_loops", {"action": "list"}),
    ]


@pytest.mark.parametrize("index", range(8))
def test_a_retried_mcp_read_tool_writes_the_events_of_an_ordinary_call_on_postgres(app, monkeypatch, index: int) -> None:
    """The tools marked read-only write the audit events of their policy check. A declared agent makes them, and a call that
    is run again must not write them twice (the first head left two of each for ``alice_resume``). The handler here does
    its work, commits, and then overflows once, as a reader does after its transaction closed."""

    from alicebot_api import json_bounds
    from alicebot_api.mcp import registry

    memory_id = _store_a_deep_row(app, 10, key="events-shallow")
    name, base = _tool_calls(memory_id)[index]
    arguments = {**base, "agent_id": "probe-agent", "agent_type": "coding_agent", "permission_profile": "trusted_local_agent"}
    context = MCPRuntimeContext(database_url=app.url, user_id=app.user_id)
    before = _event_counts(app)
    call_mcp_tool(context, name=name, arguments=dict(arguments))
    ordinary = _new_events(before, _event_counts(app))
    assert ordinary.get("policy.decision") == 1, (name, ordinary)

    real = registry._TOOL_HANDLERS[name]
    attempts: list[bool] = []

    def overflows_once(handler_context, handler_arguments):
        result = real(handler_context, handler_arguments)
        attempts.append(json_bounds._CUT_DEEP.get())
        if not json_bounds._CUT_DEEP.get():
            raise RecursionError("a reader, after the transaction closed")
        return result

    monkeypatch.setitem(registry._TOOL_HANDLERS, name, overflows_once)
    before = _event_counts(app)
    call_mcp_tool(context, name=name, arguments=dict(arguments))
    retried = _new_events(before, _event_counts(app))
    assert attempts == [False, True], (name, attempts)
    assert retried == ordinary, (name, retried, ordinary)


def _policy_counts(app) -> dict[tuple[str, str], int]:
    with psycopg.connect(app.admin_url) as conn:
        return {
            (row[0], row[1]): row[2]
            for row in conn.execute(
                "SELECT target_type, target_id, count(*) FROM event_log WHERE event_type = 'policy.decision' GROUP BY 1, 2"
            )
        }


def test_an_http_read_that_is_retried_writes_each_policy_event_once(app) -> None:
    """A stored row nested about 1,000 levels makes a read run again after its transaction has committed. The retry
    remembers the policy decisions and the context-pack compile of the first attempt, so each is written once."""

    from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService

    deep = "[" * 1000 + '"x"' + "]" * 1000
    with user_connection(app.url, app.user_id) as conn:
        store = PostgresVNextStore(conn)
        captured = VNextCaptureService(store).capture_source(SourceCaptureInput(
            source_type="note", title="deep source", raw_text=TEXT, connector_name="manual", external_id="deep-source",
            domain="project", sensitivity="internal",
        ))
        source_id = str(captured.source_id)
        artifact = store.create_artifact({
            "artifact_type": "daily_brief", "title": "brief", "content_markdown": TEXT, "status": "draft",
            "domain": "project", "sensitivity": "internal",
            "metadata_json": {"source_refs": [f"source:{source_id}"], "source_ids": [source_id]},
        })
        artifact_id = str(artifact["id"])
        memory_id = _store_a_deep_row(app, 10, key="audit-deep")
        _record, admin = create_agent_key(
            store, user_id=app.user_id, agent_id="admin-reader", permission_profile="admin_agent",
        )
        _record, trusted = create_agent_key(
            store, user_id=app.user_id, agent_id="trusted-reader", permission_profile="trusted_local_agent",
        )
    with psycopg.connect(app.admin_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE sources SET metadata_json = jsonb_set(coalesce(metadata_json, '{}'::jsonb), '{note}', %s::jsonb) WHERE id = %s",
            (deep, source_id),
        )
        conn.execute(
            "UPDATE generated_artifacts SET metadata_json = jsonb_set(metadata_json, '{note}', %s::jsonb) WHERE id = %s",
            (deep, artifact_id),
        )
        conn.execute(
            "UPDATE memories SET metadata_json = jsonb_set(metadata_json, '{note}', %s::jsonb) WHERE id = %s",
            (deep, memory_id),
        )

    def once(method: str, path: str, *, body: dict[str, Any] | None = None, authorization: str) -> int:
        before = _policy_counts(app)
        compiles = _event_counts(app).get("retrieval.context_pack_compiled", 0)
        if body is None:
            status, _answer = _call(method, path, query={"user_id": app.user}, authorization=authorization)
        else:
            status, _answer = _post(path, body, authorization=authorization)
        assert status in {200, 201}, (method, path, status)
        after = _policy_counts(app)
        added = {key: after.get(key, 0) - before.get(key, 0) for key in after}
        added = {key: count for key, count in added.items() if count}
        assert added and all(count == 1 for count in added.values()), (method, path, added)
        assert _event_counts(app).get("retrieval.context_pack_compiled", 0) - compiles in {0, 1}
        return _event_counts(app).get("retrieval.context_pack_compiled", 0) - compiles

    admin_header = f"Bearer {admin}"
    once("GET", f"/v0/vnext/traces/artifacts/{artifact_id}", authorization=admin_header)
    once("GET", f"/v0/vnext/artifacts/{artifact_id}", authorization=admin_header)
    once("GET", f"/v0/vnext/memories/{memory_id}/audit", authorization=admin_header)
    compiled = once(
        "POST", "/v0/vnext/context-packs",
        body={"user_id": app.user, "query": "cobalt door", "options": {"include_sources": True}},
        authorization=admin_header,
    )
    assert compiled == 1
    # A key with limits still gets one event: its first attempt rolls back inside the label walk, and the retry writes.
    before = _policy_counts(app)
    status, _answer = _call(
        "GET", f"/v0/vnext/traces/artifacts/{artifact_id}", query={"user_id": app.user}, authorization=f"Bearer {trusted}",
    )
    assert status == 200
    after = _policy_counts(app)
    added = {key: after.get(key, 0) - before.get(key, 0) for key in after if after.get(key, 0) != before.get(key, 0)}
    assert added and all(count == 1 for count in added.values()), added
