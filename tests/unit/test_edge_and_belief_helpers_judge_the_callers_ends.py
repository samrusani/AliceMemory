"""The helpers of the two review routes hand back a graph edge or a belief only when the caller may read what it joins.

``_vnext_readable_edge`` and ``_vnext_readable_belief`` stand in front of the edge and belief review routes. Behind them the
services check the caller's ceiling again, so over HTTP either fence alone gives the same answers, and a route test cannot
tell which one held. These tests call the helpers themselves, over the SQLite store with the edge and belief readers added, for
an unbound trusted key and for the owner:

* an edge is handed back when each end it names is a source, a memory, a belief or an entity the caller may read, and not
  otherwise (an end above the ceiling, redacted, built from a redacted memory, missing, or of a kind with no check);
* a belief is handed back when the memory behind it is readable;
* the owner has no identity and is handed everything that exists.

A last test reads the source of the two routes and pins that each passes the caller's identity to its helper.

Mutations that must fail this file, each alone: let the helper treat every caller as the owner; stop checking the source,
memory or belief end of an edge, or the fence of a memory; read a memory that does not exist as readable; make a route pass
no identity to its helper.
"""
from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.routers._vnext_shared import _vnext_readable_belief, _vnext_readable_edge
from alicebot_api.vnext_agent_control import AgentIdentity
from tests.unit.review_store_support import world
from tests.unit.test_redacted_input_containment_sqlite import ALPHA, vault  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
TRUSTED = AgentIdentity(agent_id="trusted-key", permission_profile="trusted_local_agent")


def _edges(vault, store, ends) -> dict[str, str]:
    """One edge per end, from a readable source. The names say whether the trusted key may read the end."""
    source_id = str(vault.source["id"])
    other_source = store.create_source(
        {"source_type": "note", "title": "SOURCE two", "content_hash": str(uuid4()), "domain": "project",
         "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}}
    )
    secret_source = store.create_source(
        {"source_type": "note", "title": "SECRET source", "content_hash": str(uuid4()), "domain": "project",
         "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}}
    )
    artifact_id = str(uuid4())
    return {
        "clear memory": store.add_edge(source_id, "memory", ends["clear"]),
        "redacted memory": store.add_edge(source_id, "memory", ends["redacted"]),
        "memory built from the redacted one": store.add_edge(source_id, "memory", ends["contained copy"]),
        "memory above the ceiling": store.add_edge(source_id, "memory", ends["above the ceiling"]),
        "memory that does not exist": store.add_edge(source_id, "memory", ends["missing"]),
        "clear source": store.add_edge(source_id, "source", str(other_source["id"])),
        "source above the ceiling": store.add_edge(source_id, "source", str(secret_source["id"])),
        "source that does not exist": store.add_edge(source_id, "source", str(uuid4())),
        "belief of a clear memory": store.add_edge(source_id, "belief", store.add_belief(ends["clear"])),
        "belief of a memory above the ceiling": store.add_edge(source_id, "belief", store.add_belief(ends["above the ceiling"])),
        "entity": store.add_edge(source_id, "entity", str(uuid4())),
        "artifact": store.add_edge(source_id, "artifact", artifact_id),
    }


READABLE = {"clear memory", "clear source", "belief of a clear memory", "entity"}


def test_the_edge_helper_hands_back_an_edge_only_when_the_caller_may_read_each_end_it_names(vault) -> None:
    conn_context, store, ends = world(vault)
    try:
        edges = _edges(vault, store, ends)
        assert set(edges) - READABLE, "the cases must include rows the key may not read"
        for name, edge_id in edges.items():
            got = _vnext_readable_edge(store, TRUSTED, edge_id)  # type: ignore[arg-type]
            if name in READABLE:
                assert got is not None and str(got["id"]) == edge_id, name
            else:
                assert got is None, name
        # The owner has no identity and is not fenced: every edge that exists is handed back.
        for name, edge_id in edges.items():
            assert _vnext_readable_edge(store, None, edge_id) is not None, name  # type: ignore[arg-type]
        # An id that is not an edge, or not an id, is no edge for anyone.
        for caller in (TRUSTED, None):
            assert _vnext_readable_edge(store, caller, str(uuid4())) is None  # type: ignore[arg-type]
            assert _vnext_readable_edge(store, caller, "not-an-id") is None  # type: ignore[arg-type]
    finally:
        conn_context.__exit__(None, None, None)


def test_the_belief_helper_hands_back_a_belief_only_when_the_caller_may_read_the_memory_behind_it(vault) -> None:
    conn_context, store, ends = world(vault)
    try:
        beliefs = {name: store.add_belief(row_id) for name, row_id in ends.items()}
        for name, belief_id in beliefs.items():
            got = _vnext_readable_belief(store, TRUSTED, belief_id)  # type: ignore[arg-type]
            if name == "clear":
                assert got is not None and str(got["id"]) == belief_id
            else:
                assert got is None, name
            assert _vnext_readable_belief(store, None, belief_id) is not None, name  # type: ignore[arg-type]
        for caller in (TRUSTED, None):
            assert _vnext_readable_belief(store, caller, str(uuid4())) is None  # type: ignore[arg-type]
            assert _vnext_readable_belief(store, caller, "not-an-id") is None  # type: ignore[arg-type]
    finally:
        conn_context.__exit__(None, None, None)


def _helper_calls(function: str, helper: str) -> list[ast.Call]:
    tree = ast.parse((ROOT / "apps/api/src/alicebot_api/routers/vnext_review.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            found += [
                call for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == helper
            ]
    return found


@pytest.mark.parametrize(
    ("function", "helper", "calls"),
    [("review_vnext_graph_edge", "_vnext_readable_edge", 1), ("review_vnext_belief", "_vnext_readable_belief", 2)],
)
def test_each_review_route_hands_the_callers_identity_to_its_helper(function, helper, calls) -> None:
    """The belief route calls its helper twice: for the belief and for the belief that would replace it."""
    found = _helper_calls(function, helper)
    assert len(found) == calls, (function, helper)
    for call in found:
        assert [getattr(argument, "id", None) for argument in call.args[:2]] == ["store", "identity"], (function, helper)
