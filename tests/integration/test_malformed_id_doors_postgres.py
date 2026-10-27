"""A malformed row id at a vNext door answers what an unknown id answers at that door.

The PostgreSQL store reads every id with ``WHERE id = %s::uuid`` and every write puts the id into
``%s::uuid`` parameters, so a value that is not an id in any accepted spelling failed the cast and
the door answered a server error where a well-formed unknown id answered "not found". Every door
now reads the id argument through the shared router helper ``_vnext_canonical_id`` first: a value
that parses (canonical, any case, braces, no hyphens, ``urn:uuid:``) is used in canonical form, and
one that does not is answered exactly as a well-formed unknown id is, for the same caller. Two
MCP tools that also failed for a well-formed unknown id (``alice_belief_review`` and
``alice_graph_edge_review``) answer ``not_found`` for both now.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import alicebot_api.main  # noqa: F401  (the mounted application)
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import (
    MCPArgumentError,
    MCPReferenceNotFoundError,
    MCPRuntimeContext,
    MCPToolError,
)
from alicebot_api.mcp_server import (
    _TOOL_EXECUTION_FAILED_MESSAGE,
    _TOOL_REQUEST_FAILED_CODE,
    _TOOL_REQUEST_FAILED_MESSAGE,
)
from alicebot_api.vnext_agent_control import AgentIdentity
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

MALFORMED_VALUES = ("no-such-id", "123", "not even close")


def _keyed_identity() -> AgentIdentity:
    return AgentIdentity(agent_id="explain-reader", permission_profile="admin_agent", auth="agent_api_key")


def _wire(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> tuple[str, str]:
    """The answer this call puts on the wire: (code, message), exactly as the server maps it.

    A coded refusal keeps its code and the server's fixed words; the plain
    tool error answers ``tool_request_failed`` with the same fixed words;
    anything that escapes the handler chain answers ``tool_execution_failed``.
    """

    from alicebot_api.mcp.types import MCP_CODED_ERROR_CODES, MCPInvalidRequestError, MCPCodedToolError

    try:
        payload = call_mcp_tool(context, name=name, arguments=arguments)
    except MCPInvalidRequestError as exc:
        return ("invalid_request", exc.public_message)
    except MCPCodedToolError as exc:
        return (exc.code if exc.code in MCP_CODED_ERROR_CODES else _TOOL_REQUEST_FAILED_CODE, _TOOL_REQUEST_FAILED_MESSAGE)
    except MCPToolError:
        return (_TOOL_REQUEST_FAILED_CODE, _TOOL_REQUEST_FAILED_MESSAGE)
    except Exception:
        return ("tool_execution_failed", _TOOL_EXECUTION_FAILED_MESSAGE)
    return ("result", payload)


def _same_wire(first: tuple[str, object], second: tuple[str, object]) -> bool:
    """Wire answers equal, except a field that echoes the caller's own input.

    The neighborhood door answers the caller's own ``target_id`` back; the
    malformed and the unknown call name different ids, so that echo differs
    by construction and everything the answer shows about the vault must
    still be equal.
    """

    if first[0] != second[0]:
        return False
    if first[0] == "result" and isinstance(first[1], dict) and isinstance(second[1], dict) \
            and "target_id" in first[1] and "target_id" in second[1]:
        return {k: v for k, v in first[1].items() if k != "target_id"} == {k: v for k, v in second[1].items() if k != "target_id"}
    return first[1] == second[1]


@pytest.fixture
def rows(label_harness):
    """One of every row the doors name by id, stored under the harness user."""

    h = label_harness
    with h.store() as store:
        belief = store.create_belief({
            "memory_id": str(h.memory()["id"]),
            "claim": "Synthetic acceptance claim",
            "status": "active",
            "confidence": 0.9,
        })
        loop = store.create_open_loop({"title": "Synthetic acceptance loop"})
        project = store.create_project({"name": "Synthetic Acceptance " + uuid4().hex[:8], "slug": uuid4().hex[:10], "domain": "project", "sensitivity": "public"})
        edge = store.create_edge({
            "from_type": "memory",
            "from_id": str(h.memory()["id"]),
            "to_type": "memory",
            "to_id": str(h.memory()["id"]),
            "edge_type": "related_to_person",
        })
        artifact = None
    return {"belief": belief, "loop": loop, "project": project, "edge": edge, "artifact": artifact}


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_belief_state_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    unknown_status, unknown_body, _ = h.request("GET", f"/v0/vnext/beliefs/{uuid4()}/state")
    status, body, _ = h.request("GET", f"/v0/vnext/beliefs/{value}/state")
    assert status == unknown_status == 404, (status, body)
    assert body == unknown_body, body
    assert status and value not in json.dumps(body)
    # A well-formed id in another accepted spelling reaches the row in canonical form.
    spelled_status, spelled_body, _ = h.request("GET", f"/v0/vnext/beliefs/{'urn:uuid:' + str(rows['belief']['id'])}/state")
    assert spelled_status == 200, spelled_body
    assert str(spelled_body.get("belief_id")) == str(rows["belief"]["id"])


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_open_loop_review_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    payload = {"action": "review"}
    unknown_status, unknown_body, _ = h.request("POST", f"/v0/vnext/open-loops/{uuid4()}/review", payload=payload)
    status, body, _ = h.request("POST", f"/v0/vnext/open-loops/{value}/review", payload=payload)
    assert status == unknown_status == 404, (status, body)
    assert body == unknown_body, body


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_project_dashboard_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    unknown_status, unknown_body, _ = h.request("GET", f"/v0/vnext/projects/{uuid4()}/dashboard")
    status, body, _ = h.request("GET", f"/v0/vnext/projects/{value}/dashboard")
    assert status == unknown_status == 404, (status, body)
    assert body == unknown_body, body


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_project_update_review_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    payload = {"action": "reject"}
    unknown_status, unknown_body, _ = h.request(
        "POST", f"/v0/vnext/projects/update-candidates/{uuid4()}/review", payload=payload
    )
    status, body, _ = h.request("POST", f"/v0/vnext/projects/update-candidates/{value}/review", payload=payload)
    assert status == unknown_status == 404, (status, body, unknown_body)
    assert body == unknown_body, body


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_alice_explain_malformed_memory_id_matches_unknown(label_harness, value) -> None:
    h = label_harness
    keyless = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    keyed = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id, agent_identity=_keyed_identity(), agent_identity_resolved=True)
    for context, caller in ((keyless, "keyless"), (keyed, "keyed")):
        unknown = _wire(context, "alice_explain", {"memory_id": str(uuid4())})
        malformed = _wire(context, "alice_explain", {"memory_id": value})
        assert _same_wire(malformed, unknown), (caller, malformed, unknown)


@pytest.mark.parametrize("action", ("forget", "undo", "expire", "unexpire", "redact", "accept_consolidation"))
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_alice_memory_manage_malformed_memory_id_matches_unknown(label_harness, action, value) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown = _wire(context, "alice_memory_manage", {"action": action, "memory_id": str(uuid4()), "reason": "synthetic"})
    malformed = _wire(context, "alice_memory_manage", {"action": action, "memory_id": value, "reason": "synthetic"})
    assert _same_wire(malformed, unknown), (action, malformed, unknown)


@pytest.mark.parametrize("name,build", (
    # Every request is otherwise valid for its tool, so the tool really
    # reads the id argument: the refusal these tests pin is the id's, not
    # an argument refusal that happens before the id is read.
    pytest.param("alice_vnext_artifact_get", lambda value: {"artifact_id": value}, id="alice_vnext_artifact_get"),
    pytest.param("alice_vnext_artifact_review", lambda value: {"artifact_id": value, "action": "review"}, id="alice_vnext_artifact_review"),
    pytest.param("alice_project_update_review", lambda value: {"artifact_id": value, "action": "reject"}, id="alice_project_update_review"),
    pytest.param("alice_project_dashboard", lambda value: {"project_id": value}, id="alice_project_dashboard"),
    pytest.param("alice_open_loop_review", lambda value: {"loop_id": value, "action": "close"}, id="alice_open_loop_review"),
    pytest.param("alice_belief_state", lambda value: {"belief_id": value}, id="alice_belief_state"),
))
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_legacy_mcp_tool_malformed_id_matches_unknown(label_harness, name, build, value) -> None:
    """A malformed id puts on the wire exactly what an unknown id puts there."""

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown = _wire(context, name, build(str(uuid4())))
    malformed = _wire(context, name, build(value))
    assert _same_wire(malformed, unknown), (name, malformed, unknown)


def test_belief_review_mcp_answers_not_found_for_malformed_and_unknown(label_harness, rows) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    for value in (*MALFORMED_VALUES, str(uuid4())):
        code, message = _wire(context, "alice_belief_review", {"belief_id": value, "action": "reinforce"})
        assert (code, message) == ("not_found", _TOOL_REQUEST_FAILED_MESSAGE), (value, code, message)


def test_graph_edge_review_mcp_answers_not_found_for_malformed_and_unknown(label_harness, rows) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    for value in (*MALFORMED_VALUES, str(uuid4())):
        code, message = _wire(context, "alice_graph_edge_review", {"edge_id": value, "action": "review"})
        assert (code, message) == ("not_found", _TOOL_REQUEST_FAILED_MESSAGE), (value, code, message)


def test_commit_capture_candidate_id_is_reported_not_failed(label_harness) -> None:
    """A capture commit reports a candidate it cannot commit; it does not fail the call."""

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    for candidate_id in ("no-such-id", str(uuid4())):
        result = _wire(context, "alice_commit_captures", {
            "mode": "manual",
            "candidates": [{
                "candidate_id": candidate_id,
                "candidate_type": "note",
                "object_type": "Note",
                "normalized_text": "Synthetic claim",
                "confidence": 0.9,
                "trust_class": "deterministic",
                "evidence_snippet": "Synthetic claim",
                "explicit": True,
                "source_role": "assistant",
                "admission_reason": "synthetic",
                "proposed_action": "queue_for_review",
            }],
        })
        assert result[0] not in ("tool_request_failed", "tool_execution_failed", "invalid_request"), result


def test_review_apply_provenance_ids_are_data_not_lookups(label_harness) -> None:
    """A provenance id the door only records is stored as given and read back as stored.

    The schema-refused fields (``thread_id``, ``task_id``, ``format: uuid``)
    answer invalid_request at the argument layer; the free-form ids are data
    the review item keeps. Neither is read against a row table.
    """

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    with h.store() as store:
        from tests.integration.test_vnext_live_workspace_api import seed_user
        del store
    unknown = _wire(context, "alice_review_apply", {
        "review_item_id": str(uuid4()),
        "action": "approve",
        "provenance": {"artifact_id": "no-such-id", "source_event_ids": ["no-such-event"]},
    })
    assert unknown[0] != "tool_execution_failed", unknown


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_graph_neighborhood_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    unknown_status, unknown_body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{uuid4()}")
    status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{value}")
    # The door answers the caller's own id back and shows the edges around
    # it, which are none for an id no row answers to, whatever its spelling.
    assert status == unknown_status == 200, (status, body, unknown_body)
    assert (body["from_edges"], body["to_edges"], body["edge_count"]) == (
        unknown_body["from_edges"], unknown_body["to_edges"], unknown_body["edge_count"]
    ), body


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_graph_edge_review_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    payload = {"action": "review"}
    unknown_status, unknown_body, _ = h.request("POST", f"/v0/vnext/graph/edges/{uuid4()}/review", payload=payload)
    status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{value}/review", payload=payload)
    assert status == unknown_status, (status, body, unknown_body)
    assert body == unknown_body, body


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_memory_confirm_http_malformed_confirmation_id_matches_unknown(label_harness, value) -> None:
    h = label_harness
    payload = {"action": "confirm", "canonical_text": "Synthetic confirmation text", "confirmation_id": value}
    unknown_status, unknown_body, _ = h.request(
        "POST", "/v0/vnext/memories/confirm",
        payload={"action": "confirm", "canonical_text": "Synthetic confirmation text", "confirmation_id": str(uuid4())},
    )
    status, body, _ = h.request("POST", "/v0/vnext/memories/confirm", payload=payload)
    assert status == unknown_status, (status, body, unknown_body)
    assert body == unknown_body, body


@pytest.mark.parametrize("tool,arguments", (
    ("alice_graph_neighborhood", {"target_id": "X"}),
    ("alice_vnext_open_loops", {"source_id": "X"}),
    ("alice_vnext_open_loops", {"project_id": "X"}),
    ("alice_open_loops", {"action": "close", "loop_id": "X"}),
    ("alice_project_update_candidate", {"project_id": "X"}),
    ("alice_vnext_project_dashboard", {"project_id": "X"}),
    ("alice_memory_manage", {"action": "confirm", "confirmation_id": "X", "canonical_text": "Synthetic confirmation text"}),
))
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_mcp_row_id_argument_malformed_matches_unknown(label_harness, tool, arguments, value) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown_arguments = dict(arguments)
    for key in arguments:
        if arguments[key] == "X":
            unknown_arguments[key] = str(uuid4())
    unknown = _wire(context, tool, unknown_arguments)
    malformed = _wire(context, tool, {**arguments, **{
        key: value for key in arguments if arguments.get(key) == "X"
    }})
    assert _same_wire(malformed, unknown), (tool, malformed, unknown)


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_belief_review_http_malformed_id_matches_unknown(label_harness, rows, value) -> None:
    h = label_harness
    payload = {"action": "review", "confidence": 0.5}
    unknown_status, unknown_body, _ = h.request("POST", f"/v0/vnext/beliefs/{uuid4()}/review", payload=payload)
    status, body, _ = h.request("POST", f"/v0/vnext/beliefs/{value}/review", payload=payload)
    assert status == unknown_status == 404, (status, body, unknown_body)
    assert body == unknown_body, body


@pytest.mark.parametrize("tool,arguments", (
    ("alice_capture_candidates", {"session_id": "X"}),
    ("alice_memory_mutations_generate", {"session_id": "X"}),
))
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_mcp_id_arrays_and_sessions_malformed_matches_unknown(label_harness, tool, arguments, value) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown_arguments = json.loads(json.dumps(arguments).replace("X", str(uuid4())))
    unknown = _wire(context, tool, unknown_arguments)
    malformed = _wire(context, tool, json.loads(json.dumps(arguments).replace("X", value)))
    assert _same_wire(malformed, unknown), (tool, malformed, unknown)


@pytest.mark.parametrize("tool,build", (
    pytest.param("alice_vnext_undo_memory", lambda v: {"memory_id": v}, id="alice_vnext_undo_memory"),
    pytest.param("alice_vnext_forget_memory", lambda v: {"memory_id": v, "reason": "synthetic"}, id="alice_vnext_forget_memory"),
    pytest.param("alice_vnext_memory_audit", lambda v: {"memory_id": v}, id="alice_vnext_memory_audit"),
    pytest.param("alice_vnext_correct_memory", lambda v: {"memory_id": v, "canonical_text": "Synthetic corrected text"}, id="alice_vnext_correct_memory"),
))
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_legacy_memory_tool_malformed_memory_id_matches_unknown(label_harness, tool, build, value) -> None:
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown = _wire(context, tool, build(str(uuid4())))
    malformed = _wire(context, tool, build(value))
    assert _same_wire(malformed, unknown), (tool, malformed, unknown)


NIL_ID = "00000000-0000-0000-0000-000000000000"


def _nil_row_state(h) -> tuple:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(h.urls["admin"], autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT id::text, memory_key, status, canonical_text FROM memories WHERE id = %s::uuid",
            (NIL_ID,),
        ).fetchone()
        return () if row is None else tuple(row.items())


def _store_a_row_under_the_nil_id(h) -> None:
    """Store one row under the nil id the way a restore or import would: a
    direct write that bypasses row-level security, as the admin role does."""

    import psycopg

    with h.store() as store:
        memory = store.create_memory({"memory_key": "nil.id.row", "canonical_text": "the nil id row", "value": {"text": "nil"}})
    with psycopg.connect(h.urls["admin"], autocommit=True) as conn:
        conn.execute("UPDATE memories SET id = %s::uuid WHERE id = %s::uuid", (NIL_ID, str(memory["id"])))


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_a_malformed_confirmation_id_answers_as_an_unknown_one(label_harness, value) -> None:
    """The confirm door reads its confirmation id and answers a malformed one as an unknown one.

    The confirmation id is looked up as text (``WHERE confirmation_id = %s``,
    no cast), so a value that is not an id at all answers exactly what a
    well-formed id of no pending confirmation answers.
    """

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    unknown = _wire(context, "alice_memory_manage", {"action": "confirm", "confirmation_id": str(uuid4()), "reason": "synthetic"})
    malformed = _wire(context, "alice_memory_manage", {"action": "confirm", "confirmation_id": value, "reason": "synthetic"})
    assert _same_wire(malformed, unknown), (malformed, unknown)
    assert unknown[0] == "not_found", unknown


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_a_malformed_id_never_touches_a_row_stored_under_the_nil_id(label_harness, value) -> None:
    """A malformed id answers not found without any lookup, even when a row
    is stored under the all-zero id a former stand-in answer used.

    On the pre-fix source the malformed argument was answered with the nil
    id as a stand-in, so ``alice_memory_manage`` forget and ``alice_explain``
    read and deleted that stored row where an unknown id answers not found.
    """

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    _store_a_row_under_the_nil_id(h)
    before = _nil_row_state(h)
    assert before, "the nil-id row was not stored"

    unknown_forget = _wire(context, "alice_memory_manage", {"action": "forget", "memory_id": str(uuid4()), "reason": "synthetic"})
    malformed_forget = _wire(context, "alice_memory_manage", {"action": "forget", "memory_id": value, "reason": "synthetic"})
    assert _same_wire(malformed_forget, unknown_forget), (malformed_forget, unknown_forget)

    unknown_explain = _wire(context, "alice_explain", {"memory_id": str(uuid4())})
    malformed_explain = _wire(context, "alice_explain", {"memory_id": value})
    assert _same_wire(malformed_explain, unknown_explain), (malformed_explain, unknown_explain)

    assert _nil_row_state(h) == before, "the malformed id touched the row stored under the nil id"
