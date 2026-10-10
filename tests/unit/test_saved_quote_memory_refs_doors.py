"""The doors that return a memory row apply the reader of saved quotes, and a limited caller is the only one it is applied for.

Unreleased (on main, not in v0.20.0). ``SavedProvenanceReader`` withholds the quote of a source or a memory the caller may not
read. It does so only where a door asks it. The doors that matter most are PostgreSQL routes (the workspace, the project
dashboard, the source trace routes, the memory audit route), which a SQLite test cannot call whole, so this file pins the place
each one asks the reader, by reading the source of the function with ``ast``, and the condition under which it asks. The behaviour
at the same doors is checked on SQLite in ``test_saved_quote_memory_refs_vault.py`` (the services the routes call) and on
PostgreSQL in ``tests/integration/test_saved_quote_memory_refs_postgres.py`` (the routes themselves).

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from alicebot_api.mcp import evidence_artifacts
from alicebot_api.routers import _vnext_shared, vnext_memories, workspaces
from alicebot_api.vnext_projects import VNextProjectService


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


def test_the_source_trace_holds_its_memories_to_the_reader_for_a_caller_with_limits() -> None:
    """``GET /v0/vnext/traces/sources/{id}``, the trace in a source review and the traces of the workspace list the memories
    that cite a source, with their metadata. The loader passes them through the reader when the caller has limits.

    Mutation: delete the ``memories`` call on the reader, or replace ``entity_read_fenced`` in its condition with ``False``.
    """

    tree = _tree(_vnext_shared._vnext_load_source_trace)
    assert len(_calls(tree, "memories")) == 1
    assert "entity_read_fenced" in _condition_of_the_reader_call(tree, "memories")
