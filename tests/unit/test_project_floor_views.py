"""Project views agree across both SQL builders and every Python mirror."""

from __future__ import annotations

import inspect
import itertools
import json
import sqlite3

import pytest

from alicebot_api.mcp.retrieval_shared import _resource_matches_project_scope
from alicebot_api.session_briefing import _memory_honours_fence
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER, project_scopes_overlap
from alicebot_api.vnext_retrieval import _ResolvedRetrievalScope, _project_scope_meets, _row_matches_scope
from alicebot_api.vnext_stores.sqlite.query_predicates import (
    _ensure_project_scope_identity_sqlite,
    _project_view_sql,
    _view_membership_sql,
)

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16
SCOPES = ((), (ALPHA,), (BETA,), (ALPHA, BETA), ("Alice",), (ALPHA.upper(),))
VIEWS = ((), (ALPHA,), (BETA,), (ALPHA, GLOBAL_PROJECT_MARKER),
         (BETA, GLOBAL_PROJECT_MARKER), (GLOBAL_PROJECT_MARKER,), (ALPHA, BETA, GLOBAL_PROJECT_MARKER))


def expected(scope, floor, view):
    if not view:
        return True
    ids = {value.lower() for value in view if value != GLOBAL_PROJECT_MARKER}
    stored = {value.lower() for value in scope}
    alice = lambda values: {value for value in values if value in {ALPHA, BETA}}
    return bool(stored & ids) or (
        GLOBAL_PROJECT_MARKER in view and not alice(stored) and alice(value.lower() for value in floor) <= ids
    )


@pytest.mark.parametrize("scope,floor,view", itertools.product(SCOPES, SCOPES, VIEWS))
def test_sql_and_python_project_view_membership_grid(scope, floor, view):
    row = {"domain": "project", "sensitivity": "public", "metadata_json": {
        "project_scope": list(scope), "project_floor": list(floor),
    }}
    want = expected(scope, floor, view)
    assert project_scopes_overlap(scope, view, floor=floor) == (want if view else False)
    assert _project_scope_meets(set(scope), view, floor=floor) == (want if view else False)
    assert _resource_matches_project_scope(row, view) == want
    resolved = _ResolvedRetrievalScope(frozenset(view), frozenset(), None, None, frozenset())
    assert _row_matches_scope(row, resolved) == want
    assert _memory_honours_fence(row, effective_domains=(), effective_sensitivity_allowed=("public",),
                                effective_project_scope=view, exclude_global_domains=frozenset()) == want
    with sqlite3.connect(":memory:") as conn:
        _ensure_project_scope_identity_sqlite(conn)
        conn.execute("CREATE TABLE rows(metadata_json TEXT, project_id TEXT, domain TEXT)")
        conn.execute("INSERT INTO rows VALUES (?, NULL, 'project')", (json.dumps(row["metadata_json"]),))
        placeholders = lambda values: ",".join("?" for _ in values)
        kwargs = dict(placeholders=placeholders, scope_expression="alice_project_scope_identity(metadata_json,project_id)",
                      text_expressions=("metadata_json", "project_id"), domain_expression="domain",
                      floor_expression="alice_project_floor_identity(metadata_json)")
        clause, params = _project_view_sql(**kwargs, projects=view, global_excluded_domains=())
        assert bool(conn.execute("SELECT count(*) FROM rows WHERE 1=1" + clause, params).fetchone()[0]) == want
        if view:
            ids = tuple(value.lower() for value in view if value != GLOBAL_PROJECT_MARKER)
            for partition in (False, True):
                sql, params = _view_membership_sql(**kwargs, ids=ids, wants_global=GLOBAL_PROJECT_MARKER in view,
                                                   global_excluded_domains=(), partition=partition)
                got = conn.execute("SELECT " + sql + " FROM rows", params).fetchone()[0]
                assert (got is not None if partition else bool(got)) == want


def test_every_marker_aware_python_mirror_passes_the_floor():
    for function in (_project_scope_meets, _row_matches_scope, _resource_matches_project_scope, _memory_honours_fence,
                     _view_membership_sql, _project_view_sql):
        assert "floor" in inspect.getsource(function), function.__name__
