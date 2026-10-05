"""A project view that asks for global rows also consults the floor."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from alicebot_api.mcp.retrieval_shared import _resource_matches_project_scope
from alicebot_api.session_briefing import _memory_honours_fence
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER
from alicebot_api.vnext_retrieval import _project_scope_meets

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def test_python_view_mirrors_hide_a_foreign_floor() -> None:
    view = (ALPHA, GLOBAL_PROJECT_MARKER)
    assert _project_scope_meets(set(), view, floor=(ALPHA,)) is True
    assert _project_scope_meets(set(), view, floor=(BETA,)) is False
    assert _project_scope_meets(set(), view, floor=()) is True
    foreign = {"metadata_json": {"project_scope": [], "project_floor": [BETA]}}
    home = {"metadata_json": {"project_scope": [], "project_floor": [ALPHA]}}
    assert _resource_matches_project_scope(foreign, view) is False
    assert _resource_matches_project_scope(home, view) is True
    assert _memory_honours_fence(
        {**foreign, "domain": "project", "sensitivity": "internal"},
        effective_domains=(),
        effective_sensitivity_allowed=("internal",),
        effective_project_scope=view,
        exclude_global_domains=frozenset(),
    ) is False


def test_sqlite_global_view_hides_a_row_whose_floor_names_another_project() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "view-floor@example.com", "View Floor")
    store = SQLiteVNextStore(conn, user_id)

    def add(name: str, scope: list[str], floor: list[str]) -> str:
        row = store.create_memory(
            {
                "memory_key": f"memory.{name}",
                "value": {"text": name},
                "status": "active",
                "memory_type": "semantic",
                "title": name,
                "canonical_text": name,
                "summary": name,
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": scope, "project_floor": floor},
            }
        )
        return str(row["id"])

    home = add("home", [], [ALPHA])
    foreign = add("foreign", [], [BETA])
    plain = add("plain", [], [])
    listed = {
        str(row["id"])
        for row in store.list_memories(
            projects=(ALPHA, GLOBAL_PROJECT_MARKER),
            exclude_global_domains=(),
            limit=20,
        )
    }
    assert home in listed
    assert plain in listed
    assert foreign not in listed
    conn.close()
