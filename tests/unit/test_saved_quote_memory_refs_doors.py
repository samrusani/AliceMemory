"""The doors that return a memory row apply the reader of saved quotes, and a limited caller is the only one it is applied for.

Unreleased (on main, not in v0.20.0). ``SavedProvenanceReader`` withholds the quote of a source or a memory the caller may not
read. It does so only where a door asks it. The doors that matter most are PostgreSQL routes (the workspace, the project
dashboard, the source trace routes, the memory audit route), which a SQLite test cannot call whole, so this file pins the place
each one asks the reader, by reading the source of the function with ``ast``, and the condition under which it asks. The behaviour
at the same doors is checked on SQLite in ``test_saved_quote_memory_refs_vault.py`` (the services the routes call) and on
PostgreSQL in ``tests/integration/test_saved_quote_memory_refs_postgres.py`` (the routes themselves). The workspace builder is
also run whole, on a stub store that lists the agent events, in the last test of the file.

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

from alicebot_api.mcp import evidence_artifacts
from alicebot_api.routers import _vnext_shared, vnext_memories, workspaces
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_projects import VNextProjectService
from tests.unit.test_complete_readable_counts import PopulationStore, _quiet_services, _row


def _tree(function: object) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(function)))  # type: ignore[arg-type]


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            label = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
            if label == name:
                found.append(node)
    return found


def _condition_of_the_reader_call(tree: ast.AST, method: str) -> str:
    """The condition of the ``if`` whose body asks the reader of saved quotes for ``method`` (``audit`` or ``memories``)."""

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for call in (item for statement in node.body for item in ast.walk(statement) if isinstance(item, ast.Call)):
            target = call.func
            if (
                isinstance(target, ast.Attribute)
                and target.attr == method
                and isinstance(target.value, ast.Call)
                and isinstance(target.value.func, ast.Name)
                and target.value.func.id == "SavedProvenanceReader"
            ):
                found.append(ast.unparse(node.test))
    assert len(found) == 1, found
    return found[0]


def test_the_memory_audit_route_holds_the_audit_to_the_reader_for_a_caller_with_limits() -> None:
    """``GET /v0/vnext/memories/{id}/audit`` hands the reader the whole envelope (memory, revisions, links, events) when the
    caller has limits, and the owner and an unbound admin key are handed the envelope itself.

    Mutations: delete the ``audit`` call; replace ``entity_read_fenced`` in its condition with ``True`` (the unbound admin
    key is judged, and an archived source takes its links).
    """

    tree = _tree(vnext_memories.get_vnext_memory_audit)
    assert len(_calls(tree, "audit")) == 2, "the service call and the reader call"
    assert "entity_read_fenced" in _condition_of_the_reader_call(tree, "audit")


def test_alice_explain_holds_the_audit_to_the_reader_for_a_key_with_limits() -> None:
    """``alice_explain`` and the legacy audit tool apply the reader after the cited sources are authorized, for a key with
    limits. A call that declares a profile and no key is not held, as before.

    Mutations: delete the ``audit`` call; drop ``_is_key_bound_explain`` from the condition (a declared profile is held, and the
    documented limit stops being true); drop ``entity_read_fenced`` (an unbound admin key is judged).
    """

    tree = _tree(evidence_artifacts._handle_alice_vnext_memory_audit)
    assert len(_calls(tree, "audit")) == 2
    condition = _condition_of_the_reader_call(tree, "audit")
    assert "_is_key_bound_explain" in condition and "entity_read_fenced" in condition


def test_the_workspace_holds_every_list_of_memories_to_the_reader_for_a_caller_with_limits() -> None:
    """The workspace lists review memories, recent commits and inline confirmations, and embeds them in the source traces
    and the pending review items. Each list passes through the reader once, and the reader is made only for a caller with
    limits (the owner and an unbound admin key are shown the rows as stored).

    Mutations: skip the reader for the review memories (delete the first ``withhold_saved_quotes`` call); make the reader for
    every caller (replace ``None if unfenced else`` with a plain construction).
    """

    source = inspect.getsource(workspaces._vnext_workspace_payload)
    tree = ast.parse(textwrap.dedent(source))
    assert len(_calls(tree, "withhold_saved_quotes")) == 3
    assert "SavedProvenanceReader(store" in source
    assert "None if unfenced else SavedProvenanceReader" in source
    review = source.index("review_memories = withhold_saved_quotes(")
    traces = source.index("memories=review_memories")
    assert review < traces, "the traces and the pending items are built from the held rows"


def test_the_project_dashboard_holds_its_memories_to_the_reader_for_a_caller_with_limits() -> None:
    """``project_dashboard`` (the route, the workspace dashboards and the legacy tool) passes its memories through the reader
    when the caller has limits.

    Mutation: delete the ``memories`` call on the reader, or replace ``entity_read_fenced`` in its condition with ``False``.
    """

    tree = _tree(VNextProjectService.project_dashboard)
    assert len(_calls(tree, "memories")) == 1
    assert "entity_read_fenced" in _condition_of_the_reader_call(tree, "memories")


def test_the_workspace_holds_its_recent_events_to_the_reader_for_a_caller_with_limits() -> None:
    """The workspace lists the 20 newest events a caller may read. An event that confirms or edits a commit holds the refs and
    the quote the commit was sent with, and the feed admits it by the commit it is about, so the events pass through the reader
    once, after the guard has admitted them, and only when the reader exists (a caller with limits).

    Mutation: delete the ``events`` call on ``recent_events`` in ``_vnext_workspace_payload`` (the quote of a redacted memory
    stays in the feed).
    """

    source = inspect.getsource(workspaces._vnext_workspace_payload)
    tree = ast.parse(textwrap.dedent(source))
    assert len(_calls(tree, "events")) == 2, "the recent events and the agent events"
    admitted = source.index("recent_events = guard.newest_admitted_events(")
    held = source.index("recent_events = saved_quotes.events(recent_events)")
    assert admitted < held, "the reader sees the events the guard admitted"
    assert "if saved_quotes is not None:\n        recent_events = saved_quotes.events(recent_events)" in source
    assert source.index("events=recent_events") > held, "the feed is passed on only after the reader has held it"


def test_the_workspace_holds_its_agent_events_to_the_reader_for_a_caller_with_limits() -> None:
    """The agent activity of the workspace lists the 50 newest events an agent key caused, and the policy blocks and the policy
    telemetry are drawn from the same list. An agent that confirms or edits a commit appends the same ``memory.updated`` event
    as the owner does, with the refs and the quote the commit was sent with in its payload, and the feed admits it by the commit
    it is about. The events pass through the reader once, after the guard has admitted them and before the telemetry and the
    activity are built from them, and only when the reader exists (a caller with limits).

    Mutation: delete the ``events`` call on ``agent_events`` in ``_vnext_workspace_payload`` (the quote of a redacted memory
    stays in ``agent_activity.recent_events``).
    """

    source = inspect.getsource(workspaces._vnext_workspace_payload)
    admitted = source.index("agent_events = guard.newest_admitted_events(")
    held = source.index("agent_events = saved_quotes.events(agent_events)")
    assert admitted < held, "the reader sees the events the guard admitted"
    assert "if saved_quotes is not None:\n        agent_events = saved_quotes.events(agent_events)" in source
    assert held < source.index("agent_events=agent_events"), "the telemetry is drawn from the held events"
    assert held < source.index('"recent_events": agent_events'), "the activity lists the held events"
    assert held < source.index("for event in agent_events"), "the policy blocks are drawn from the held events"


def test_the_source_trace_holds_its_events_to_the_reader_for_a_caller_with_limits() -> None:
    """The events of a source trace are the events about the memories that cite the source, with the payload the commit was
    confirmed or edited with. They pass through the reader when the caller has limits.

    Mutation: delete the ``events`` call, or replace ``entity_read_fenced`` in its condition with ``False``.
    """

    tree = _tree(_vnext_shared._vnext_load_source_trace)
    assert len(_calls(tree, "events")) == 1
    assert "entity_read_fenced" in _condition_of_the_reader_call(tree, "events")


def test_the_source_trace_holds_its_memories_to_the_reader_for_a_caller_with_limits() -> None:
    """``GET /v0/vnext/traces/sources/{id}``, the trace in a source review and the traces of the workspace list the memories
    that cite a source, with their metadata. The loader passes them through the reader when the caller has limits.

    Mutation: delete the ``memories`` call on the reader, or replace ``entity_read_fenced`` in its condition with ``False``.
    """

    tree = _tree(_vnext_shared._vnext_load_source_trace)
    assert len(_calls(tree, "memories")) == 1
    assert "entity_read_fenced" in _condition_of_the_reader_call(tree, "memories")


# -- the workspace, run whole ------------------------------------------------------------------------------------------

_WORDS = "Atlas played ZQXAGENTEVENT for 115 hours"
_TRUSTED = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent")
_ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent")


def _event(event_id: str, commit_id: str, cited_id: str, *, actor_type: str) -> dict[str, object]:
    """The ``memory.updated`` event a confirmation appends: the changes hold the refs and the excerpt the commit was sent with."""

    refs = [{"memory_id": cited_id, "quote": _WORDS}]
    return {
        "id": event_id,
        "event_type": "memory.updated",
        "actor_type": actor_type,
        "actor_id": "writer",
        "target_type": "memory",
        "target_id": commit_id,
        "occurred_at": "2026-10-10T08:00:00+00:00",
        "payload_json": {
            "changes": {
                "metadata_json": {"agentic_memory": {"source_refs": refs, "conversation_excerpt": _WORDS}},
                "value": {"source_refs": refs},
            }
        },
    }


def _workspace_of_a_confirmed_commit(monkeypatch, *, cited_deleted: bool, identity: AgentIdentity) -> dict[str, object]:
    _quiet_services(monkeypatch)
    store = PopulationStore()
    cited = _row("00000000-0000-0000-0000-0000000000a1", "public", status="active", deleted_at="2026-10-01T00:00:00Z" if cited_deleted else None)
    commit = _row("00000000-0000-0000-0000-0000000000b2", "public", status="active")
    store.rows["memory"] = [cited, commit]
    store.events = [_event("00000000-0000-0000-0000-0000000000c3", str(commit["id"]), str(cited["id"]), actor_type="agent")]
    store.list_agent_events = lambda **kwargs: [event for event in store.events if event["actor_type"] == "agent"]
    # The counts an unbound admin key reads from the store's own counters.
    store.count_artifacts_by_status = lambda **kwargs: {}
    store.count_open_loops_by_status = lambda **kwargs: {}
    return workspaces._vnext_workspace_payload(store, identity=identity)  # type: ignore[return-value]


def test_the_agent_activity_of_the_workspace_loses_the_quote_of_a_memory_the_caller_may_not_read(monkeypatch) -> None:
    """A commit that quotes a memory is confirmed by an agent key, and the memory is redacted afterwards. The confirmation is an
    agent event, so the workspace lists it twice: among the recent events and in the agent activity. A caller with limits is
    shown the ref with the id of the memory and a ``null`` quote in both, and no excerpt. A control with the memory readable
    shows the feed carries the quote before the redaction, and an unbound admin key reads it as stored.

    Mutations: delete the ``events`` call on ``agent_events`` in ``_vnext_workspace_payload`` (this test fails on the trusted
    caller); delete the one on ``recent_events`` (the first assertion on the recent events fails); make the reader for the
    unbound admin key as well (the last assertion fails).
    """

    cited = "00000000-0000-0000-0000-0000000000a1"
    held = _workspace_of_a_confirmed_commit(monkeypatch, cited_deleted=True, identity=_TRUSTED)
    activity = held["agent_activity"]["recent_events"]  # type: ignore[index]
    assert [event["event_type"] for event in activity] == ["memory.updated"], "the feed lists the event, so the test is not vacuous"
    for feed in (held["recent_events"], activity):
        changes = feed[0]["payload_json"]["changes"]  # type: ignore[index]
        assert changes["metadata_json"]["agentic_memory"]["source_refs"] == [{"memory_id": cited, "quote": None}]
        assert changes["value"]["source_refs"] == [{"memory_id": cited, "quote": None}]
        assert "conversation_excerpt" not in changes["metadata_json"]["agentic_memory"]
    assert "ZQXAGENTEVENT" not in json.dumps(held, default=str)

    readable = _workspace_of_a_confirmed_commit(monkeypatch, cited_deleted=False, identity=_TRUSTED)
    for feed in (readable["recent_events"], readable["agent_activity"]["recent_events"]):  # type: ignore[index]
        assert feed[0]["payload_json"]["changes"]["metadata_json"]["agentic_memory"]["source_refs"][0]["quote"] == _WORDS  # type: ignore[index]

    admin = _workspace_of_a_confirmed_commit(monkeypatch, cited_deleted=True, identity=_ADMIN)
    for feed in (admin["recent_events"], admin["agent_activity"]["recent_events"]):  # type: ignore[index]
        assert feed[0]["payload_json"]["changes"]["metadata_json"]["agentic_memory"]["source_refs"][0]["quote"] == _WORDS  # type: ignore[index]
