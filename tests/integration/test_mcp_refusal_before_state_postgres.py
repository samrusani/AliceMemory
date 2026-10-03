"""Live PostgreSQL checks of the follow-up to the typed MCP error codes (PR 528).

Unreleased (on main, not in v0.20.0). Three things the unit tests can only stub on a database that is not there:

* a caller the project scope refuses hears the refusal, not the state of the row, on the HTTP routes and over MCP, on
  rows the real Postgres store reads (``FOR UPDATE``), including a row ``redact`` reads after it was redacted;
* an unknown id in ``source_refs`` is a real foreign key failure of the driver (``ForeignKeyViolation``), answered
  ``precondition_failed`` as the SQLite one is;
* ``alice_explain`` of an entity that is not there is ``not_found`` for a caller with no agent key, raised by the real
  temporal function against the real store.

Each test names the mutation that must fail it. A mutation is made in a scratch edit of the source file, the test is
seen to fail, and the file is restored by copying the saved file back.
"""

from __future__ import annotations

import json
from io import BytesIO
from uuid import UUID, uuid4

import alicebot_api.main as main_module
from alicebot_api import mcp_server
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import MCPRuntimeContext, redact_memory_flow
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user

_FIXED_MESSAGE = "The tool request could not be processed"
_OWN_PROJECT = "alicebot"
_OTHER_PROJECT = "other-project"


def _memory(store: PostgresVNextStore, **overrides: object) -> dict[str, object]:
    text = f"A fact held for the refusal test {uuid4().hex}."
    row = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": text},
            "status": "active",
            "memory_type": "semantic",
            "title": "Refusal test fact",
            "canonical_text": text,
            "summary": "A fact.",
            "domain": "project",
            "sensitivity": "internal",
            "confirmation_status": "confirmed",
            "project_scope": [_OTHER_PROJECT],
            "project_id": _OTHER_PROJECT,
            **overrides,
        }
    )
    return dict(row)


def _states(app_url: str, user_id: UUID) -> dict[str, str]:
    """Four rows of the other project: ordinary, pending project update, open project-update artifact, redacted."""

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        ordinary = _memory(store)
        pending = _memory(
            store,
            memory_key=f"project_update.alice.{uuid4().hex}",
            status="candidate",
            metadata_json={"candidate": True, "workflow": "project_auto_update"},
        )
        coupled = _memory(store)
        for candidate in (pending, coupled):
            store.create_artifact(
                {
                    "artifact_type": "project_update",
                    "title": "Open project update",
                    "content_markdown": "# Open project update",
                    "status": "needs_review",
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {"workflow": "project_auto_update", "candidate_memory_id": str(candidate["id"])},
                }
            )
        redacted = _memory(store)
        # The owner redacts it: the row keeps its id, loses its content, and gets ``deleted_at``.
        redact_memory_flow(store, memory_id=str(redacted["id"]), reason="the owner asked for removal")
    return {
        "ordinary": str(ordinary["id"]),
        "pending project update": str(pending["id"]),
        "open project update artifact": str(coupled["id"]),
        "redacted": str(redacted["id"]),
    }


def _bind_http(monkeypatch, app_url: str) -> None:
    settings = Settings(database_url=app_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: settings)


def test_a_key_bound_to_another_project_is_refused_on_every_http_route_whatever_the_row_state(
    migrated_database_urls, monkeypatch
) -> None:
    """403 for a row the caller can see in every state, and for a redacted row the answer of an unknown id.

    The routes call the commit service, which used to read a pending project update, and ``redact_memory_flow``, which
    used to read an open project-update artifact, before it asked the policy. A key bound to ``alicebot`` that aimed at
    a row of ``other-project`` got 400 for those rows and 403 for an ordinary one. A redacted row is read by redact
    on purpose, so redact answered 403 for it where every other route answers as for an id that is not there.

    Mutations, each one alone: in ``vnext_memory_commit.py`` delete the ``refuse_unauthorized_write`` call in ``forget``,
    ``undo`` or ``correct`` (that route answers 400 for the pending project update); in ``mcp/memories.py`` delete the
    ``refuse_unauthorized_write`` call in ``redact_memory_flow`` (redact answers 400 for the pending update and for the
    open artifact) or its ``deleted_at`` branch (redact answers 403 for the redacted row).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="refusal-before-state-http@example.com")
    _bind_http(monkeypatch, app_url)
    states = _states(app_url, user_id)
    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="scoped-admin",
            permission_profile="admin_agent",
            project_scope=_OWN_PROJECT,
        )
    authorization = f"Bearer {raw_key}"
    unknown = str(uuid4())

    def post(route: str, memory_id: str) -> int:
        payload = {
            "forget": {"reason": "should not land"},
            "undo": {"reason": "should not land"},
            "correct": {"canonical_text": "A correction that must not land."},
            "redact": {"reason": "should not land"},
        }[route]
        status, _body = invoke_request(
            "POST",
            f"/v0/vnext/memories/{route}",
            payload={"user_id": str(user_id), "memory_id": memory_id, **payload},
            authorization=authorization,
        )
        return status

    seen = {
        (route, state): post(route, memory_id)
        for route in ("forget", "undo", "correct", "redact")
        for state, memory_id in states.items()
    }
    gone = {route: post(route, unknown) for route in ("forget", "undo", "correct", "redact")}

    expected = {
        (route, state): gone[route] if state == "redacted" else 403
        for route in ("forget", "undo", "correct", "redact")
        for state in states
    }
    assert seen == expected
    # An id the vault never held is a 400 on three of these routes and a 404 on redact, which is what the redacted row
    # now gets; the refusal of a row the caller can see is the 403 above.
    assert gone == {"forget": 400, "undo": 400, "correct": 400, "redact": 404}


def test_the_owner_still_hears_the_state_of_a_pending_project_update_on_postgres(
    migrated_database_urls, monkeypatch
) -> None:
    """The control: with no agent key the same calls answer the state, so the fix did not turn every state into 403.

    Mutation: in ``_write_policy_decision`` (``vnext_memory_commit.py``) return ``blocked`` for an owner call too (the
    pending project update cells answer 403).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="refusal-before-state-owner@example.com")
    _bind_http(monkeypatch, app_url)
    states = _states(app_url, user_id)

    def post(route: str, memory_id: str) -> int:
        payload = {"forget": {"reason": "no"}, "undo": {"reason": "no"}, "redact": {"reason": "no"}}[route]
        status, _body = invoke_request(
            "POST",
            f"/v0/vnext/memories/{route}",
            payload={"user_id": str(user_id), "memory_id": memory_id, **payload},
        )
        return status

    for route in ("forget", "undo", "redact"):
        assert post(route, states["pending project update"]) == 400, route
    assert post("redact", states["open project update artifact"]) == 400
    # Redact reads a redacted row on purpose, so for the owner it replays instead of answering not found.
    with user_connection(app_url, user_id) as conn:
        replay = redact_memory_flow(
            PostgresVNextStore(conn), memory_id=states["redacted"], reason="the owner asked again"
        )
    assert replay["status"] == "redacted"
    assert replay["idempotent_replay"] is True


def _wire(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> tuple[bool, dict[str, object]]:
    server = mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert response is not None
    result = response["result"]
    return bool(result["isError"]), json.loads(result["content"][0]["text"])


def _code(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> str:
    is_error, payload = _wire(context, name, arguments)
    assert is_error is True, payload
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["message"] == _FIXED_MESSAGE, error
    return str(error["code"])


def test_a_key_bound_mcp_caller_gets_not_permitted_in_every_state_and_not_found_for_a_redacted_row(
    migrated_database_urls, monkeypatch
) -> None:
    """The MCP twin of the HTTP test, with ``alice_memory_manage`` on the real Postgres store.

    Mutation: the same ones as the HTTP test; a failing cell answers ``precondition_failed`` for the pending project
    update, the open artifact (redact) or ``not_permitted`` for the redacted row (redact).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="refusal-before-state-mcp@example.com")
    states = _states(app_url, user_id)
    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="scoped-admin",
            permission_profile="admin_agent",
            project_scope=_OWN_PROJECT,
        )
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw_key)
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)

    seen = {
        (action, state): _code(
            context,
            "alice_memory_manage",
            {"action": action, "memory_id": memory_id, "reason": "should not land"},
        )
        for action in ("forget", "undo", "redact")
        for state, memory_id in states.items()
    }
    missing = {
        action: _code(
            context, "alice_memory_manage", {"action": action, "memory_id": str(uuid4()), "reason": "should not land"}
        )
        for action in ("forget", "undo", "redact")
    }

    assert missing == {"forget": "not_found", "undo": "not_found", "redact": "not_found"}
    assert seen == {
        (action, state): "not_found" if state == "redacted" else "not_permitted"
        for action in ("forget", "undo", "redact")
        for state in states
    }


def test_an_unknown_source_ref_is_a_foreign_key_failure_answered_precondition_failed_on_postgres(
    migrated_database_urls, monkeypatch
) -> None:
    """``source_refs`` naming a source the vault does not hold fails ``provenance_links_source_fkey``.

    The driver raises ``psycopg.errors.ForeignKeyViolation`` and the dispatcher answers ``precondition_failed``, the code
    SQLite gives for the same call. The failed commit leaves no memory behind, because the whole call rolls back.

    Mutation: delete the ``except ForeignKeyViolation`` clause in ``mcp/registry.py`` (the code is
    ``tool_execution_failed``).
    """

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="foreign-key-postgres@example.com")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    arguments: dict[str, object] = {
        "title": "A fact that cites a source",
        "canonical_text": "A fact that cites a source the vault does not hold.",
        "domain": "project",
        "sensitivity": "internal",
        "confidence": 0.95,
        "source_refs": [str(uuid4())],
    }

    assert _code(context, "alice_memory_commit", arguments) == "precondition_failed"

    with user_connection(app_url, user_id) as conn:
        assert PostgresVNextStore(conn).list_memories(status=None) == []


def test_alice_explain_of_a_missing_entity_is_not_found_without_a_key_and_opaque_with_one_on_postgres(
    migrated_database_urls, monkeypatch
) -> None:
    """The real ``get_temporal_explain`` raises ``TemporalStateNotFoundError`` against the real store.

    No agent key: ``not_found``. A key-bound caller keeps the one opaque answer, ``tool_request_failed``, by decision.

    Mutations: delete ``TemporalStateNotFoundError`` from the ``MCPReferenceNotFoundError`` tuple in ``mcp/registry.py``
    (the keyless answer is ``tool_execution_failed``); delete the key-bound check in the ``except LookupError`` of the
    entity path of ``_handle_alice_explain`` (the key-bound answer is ``not_found``).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="explain-missing-entity-postgres@example.com")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    entity_id = str(uuid4())

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    assert _code(context, "alice_explain", {"entity_id": entity_id}) == "not_found"

    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="keyed-explainer",
            permission_profile="trusted_local_agent",
        )
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw_key)
    assert _code(context, "alice_explain", {"entity_id": entity_id}) == "tool_request_failed"
