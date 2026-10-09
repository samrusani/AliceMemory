"""The edge and belief review services refuse a row the caller's ceiling does not admit, and write nothing when they do.

Over HTTP the routes look the row up first and answer a row the caller may not read before the service is called, so the
service's own check is a second fence behind the route's. It is also the only fence for a caller that reaches the service
without the route. These tests call the services over the SQLite store, with the two readers and two writers of an edge and a
belief that only PostgreSQL has added and recorded, on a vault where a memory is redacted afterwards. A caller with a ceiling
reviews an edge when it may read both ends and a belief when it may read the memory behind it. The owner, who passes no
ceiling, reviews them all. A third test reads the routes' source and pins that each hands the caller's ceiling to the service.

Mutations that must fail this file, each alone: drop the ceiling check of ``review_edge`` or ``review_belief``, or the check
inside ``_readable_belief``; make a route pass no ceiling to the service.
"""
from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.vnext_connections import VNextConnectionNotFoundError, VNextConnectionService
from alicebot_api.vnext_contradictions import VNextContradictionNotFoundError, VNextContradictionService
from tests.unit.review_store_support import CEILING, event_types as _event_types, world as _world
from tests.unit.test_redacted_input_containment_sqlite import vault  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]


def test_a_caller_with_a_ceiling_reviews_an_edge_only_when_it_may_read_both_ends_and_a_refusal_writes_nothing(vault):
    conn_context, store, ends = _world(vault)
    try:
        source_id = str(vault.source["id"])
        edges = {name: store.add_edge(source_id, "memory", row_id) for name, row_id in ends.items()}
        edges["entity"] = store.add_edge(source_id, "entity", str(uuid4()))
        service = VNextConnectionService(store)  # type: ignore[arg-type]
        readable = {"clear", "entity"}
        for name, edge_id in edges.items():
            if name in readable:
                continue
            with pytest.raises(VNextConnectionNotFoundError):
                service.review_edge(edge_id=edge_id, action="reject", sensitivity_allowed=CEILING)
            assert "graph_edge.reviewed" not in _event_types(store, "graph_edge", edge_id), name
        assert store.writes == []
        for name in readable:
            reviewed = service.review_edge(edge_id=edges[name], action="accept", sensitivity_allowed=CEILING)
            assert reviewed["metadata_json"]["status"] == "accepted", name
        assert {write[1] for write in store.writes} == {edges[name] for name in readable}
        assert all("graph_edge.reviewed" in _event_types(store, "graph_edge", edges[name]) for name in readable)
        # The owner passes no ceiling, so every edge is reviewed, the redacted memory's too.
        store.writes.clear()
        for edge_id in edges.values():
            service.review_edge(edge_id=edge_id, action="reject")
        assert {write[1] for write in store.writes} == set(edges.values())
    finally:
        conn_context.__exit__(None, None, None)


def test_a_caller_with_a_ceiling_reviews_a_belief_only_when_it_may_read_the_memory_behind_it_and_a_refusal_writes_nothing(vault):
    conn_context, store, ends = _world(vault)
    try:
        beliefs = {name: store.add_belief(row_id) for name, row_id in ends.items() if name != "missing"}
        service = VNextContradictionService(store)  # type: ignore[arg-type]
        for name, belief_id in beliefs.items():
            if name == "clear":
                continue
            with pytest.raises(VNextContradictionNotFoundError):
                service.review_belief(belief_id=belief_id, action="retire", sensitivity_allowed=CEILING)
            assert _event_types(store, "belief", belief_id) == [], name
        assert store.writes == []
        reviewed = service.review_belief(belief_id=beliefs["clear"], action="retire", sensitivity_allowed=CEILING)
        assert reviewed["status"] == "retired"
        assert [write[1] for write in store.writes] == [beliefs["clear"]]
        store.writes.clear()
        for belief_id in beliefs.values():
            service.review_belief(belief_id=belief_id, action="retire")
        assert {write[1] for write in store.writes} == set(beliefs.values())
    finally:
        conn_context.__exit__(None, None, None)


def _keyword_values(function: str, method: str) -> list[ast.expr]:
    """The value of ``sensitivity_allowed`` in each call of ``method`` inside ``function`` in the review routes."""
    tree = ast.parse((ROOT / "apps/api/src/alicebot_api/routers/vnext_review.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == method:
                    found += [item.value for item in call.keywords if item.arg == "sensitivity_allowed"]
    return found


@pytest.mark.parametrize(
    ("function", "method"),
    [
        ("review_vnext_graph_edge", "review_edge"),
        ("review_vnext_belief", "review_belief"),
        ("get_vnext_graph_neighborhood", "graph_neighborhood"),
    ],
)
def test_each_route_hands_the_callers_ceiling_to_the_service(function, method) -> None:
    """The ceiling is the one of the key that made the call: ``sensitivity_ceiling(identity)``, and not a constant."""
    values = _keyword_values(function, method)
    assert len(values) == 1, (function, method)
    value = values[0]
    assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "sensitivity_ceiling"
    assert [getattr(argument, "id", None) for argument in value.args] == ["identity"]
