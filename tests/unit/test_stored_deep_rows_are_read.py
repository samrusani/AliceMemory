"""A row stored with JSON nested past what the readers walk no longer breaks a door for every caller.

A row can nest deeper than the write limit allows: it was stored before the limit, or by a door that has none, or by hand.
The readers (``jsonable_encoder`` at the memory audit and recent commits routes, ``frame_disclosed_tree`` and the saved
provenance reader at ``alice_memory_review``, the recall and context pack builders, the SQLite predicates that read a
column) walk a value with recursion, and a value nested near the interpreter's limit raised ``RecursionError`` in all of
them, for the owner as for a key with limits, until the row was fixed by hand.

A stored column is decoded by ``loads_stored``, which is ``json.loads`` and costs what it costs. A row that nests past what a
reader can walk raises ``RecursionError`` in the reader; the request or the tool call then runs once more with
``cutting_deep_json()`` set, and the decoder reads every column to 256 levels with one placeholder string below that. A row
the decoder itself cannot read (about 5000 levels, PostgreSQL only) is cut at once.

The PostgreSQL routes (audit, recent commits) are covered by ``tests/integration/test_json_bounds_postgres.py``.

Mutations, each one alone: ``retry_cutting_deep_json`` stops retrying
(``test_a_row_stored_deeper_than_the_decoder_reads_does_not_break_a_door``); ``SQLiteVNextStore._decode_row`` goes back to
``json.loads`` (``test_the_cut_is_a_placeholder_and_the_rest_is_whole``); the three sqlite_store hint decoders go back to
``json.loads`` (``test_the_sqlite_functions_that_read_a_column_survive_a_deep_one``); ``loads_bounded`` stops cutting (both);
``tool_only_reads`` stops naming the list action of ``alice_open_loops``
(``test_one_deep_open_loop_does_not_break_the_list_action_for_any_caller``).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import UUID, uuid4

import pytest

from alicebot_api.json_bounds import DEEP_PLACEHOLDER, MAX_READ_JSON_DEPTH, cutting_deep_json
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault

TEXT = "The cobalt door opens on Monday."


def _deep(depth: int) -> str:
    return "[" * depth + '"x"' + "]" * depth


@pytest.fixture(autouse=True)
def _surface(monkeypatch):
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)


def _vault_with_a_deep_row(tmp_path, depth: int):
    """A vault whose one memory holds JSON ``depth`` levels deep in its metadata, its value and a revision, as an older door
    or a hand edit could leave it. SQLite takes JSON to 1000 levels, so the rows are written with SQL."""

    db = _vault(tmp_path)
    request = memory_commit_request_from_payload({"title": "cobalt door", "canonical_text": TEXT}, user_id=USER_ID)
    with sqlite_user_connection(db, USER_ID) as conn:
        result = VNextMemoryCommitService(SQLiteVNextStore(conn, USER_ID)).commit(identity=None, request=request)
    memory_id = str(result["memory"]["id"])
    deep = _deep(depth)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "UPDATE memories SET metadata_json = json_set(metadata_json, '$.note', json(?)), "
            "value = json_set(value, '$.source_refs', json(?)) WHERE id = ?",
            (deep, "[" + deep + "]", memory_id),
        )
        # A revision is append-only, so the deep one is a new row copied from the first.
        columns = [row[1] for row in conn.execute("PRAGMA table_info(memory_revisions)")]
        first = dict(zip(columns, conn.execute("SELECT * FROM memory_revisions WHERE memory_id = ?", (memory_id,)).fetchone()))
        first.update(id=str(uuid4()), sequence_no=first["sequence_no"] + 1, revision_number=first["revision_number"] + 1,
                     new_value=json.dumps({"text": TEXT, "note": "NOTE"}).replace('"NOTE"', deep))
        conn.execute(
            f"INSERT INTO memory_revisions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [first[name] for name in columns],
        )
        conn.commit()
        stored = conn.execute("SELECT length(metadata_json) FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
    assert stored > depth * 2
    return db, memory_id


# The caller, and whether the memory (which belongs to no project) is one it may read.
CALLERS = {
    "the owner": (None, True),
    "a key with limits": (
        AgentIdentity(agent_id="reader", permission_profile="project_scoped_agent", auth="agent_api_key",
                      project_scope=("Alice",), project_scope_locked=True),
        False,
    ),
    "a read only key": (AgentIdentity(agent_id="ro", permission_profile="read_only_agent", auth="agent_api_key"), True),
}


def _calls(memory_id: str):
    return (
        ("alice_memory_review", {"review_item_id": memory_id}),
        ("alice_memory_review", {"status": "all"}),
        ("alice_explain", {"memory_id": memory_id}),
        ("alice_recall", {"query": "cobalt door"}),
        ("alice_context_pack", {"query": "cobalt door", "include_sources": True}),
        ("alice_resume", {"query": "cobalt"}),
        ("alice_recent_decisions", {}),
        ("alice_open_loops", {"action": "list"}),
    )


@pytest.mark.parametrize("depth", (MAX_READ_JSON_DEPTH - 8, 400, 900, 990))
@pytest.mark.parametrize("caller", list(CALLERS))
def test_a_row_stored_deeper_than_the_decoder_reads_does_not_break_a_door(tmp_path, depth: int, caller: str) -> None:
    db, memory_id = _vault_with_a_deep_row(tmp_path, depth)
    identity, may_read = CALLERS[caller]
    keyword = {} if identity is None else {"agent_identity": identity, "agent_identity_resolved": True}
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID), **keyword)
    for name, arguments in _calls(memory_id):
        try:
            answer = call_mcp_tool(context, name=name, arguments=dict(arguments))
        except Exception as exc:
            # A key may be refused by policy or not find the row; it must never be a RecursionError.
            assert type(exc).__name__.startswith("MCP"), (caller, name, type(exc).__name__)
            continue
        text = json.dumps(answer, default=str)
        if may_read and (name in ("alice_recall", "alice_explain") or "review_item_id" in arguments):
            assert TEXT in text, (caller, name)


def _levels(note) -> tuple[int, object]:
    levels = 0
    while isinstance(note, list):
        levels += 1
        note = note[0]
    return levels, note


@pytest.mark.parametrize("depth", (400, 990))
def test_the_cut_is_a_placeholder_and_the_rest_is_whole(tmp_path, depth: int) -> None:
    db, memory_id = _vault_with_a_deep_row(tmp_path, depth)
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        whole = store.get_memory(memory_id)
        with cutting_deep_json():
            cut = store.get_memory(memory_id)
    # Read plain, the row is the whole value.
    assert _levels(whole["metadata_json"]["note"]) == (depth, "x")
    # Read on the second attempt, ``note`` sits one level down in the object, so the list under it keeps 255 levels.
    assert cut["canonical_text"] == TEXT
    assert _levels(cut["metadata_json"]["note"]) == (MAX_READ_JSON_DEPTH - 1, DEEP_PLACEHOLDER)
    assert cut["metadata_json"]["agentic_memory"]["kind"] == "agentic_memory_commit"


def test_a_call_that_a_deep_row_overflows_runs_again_with_the_cut(tmp_path, caplog) -> None:
    """The retry is what lets a door read a row too deep for its own recursion, so it must be seen to happen."""

    db, memory_id = _vault_with_a_deep_row(tmp_path, 990)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID))
    with caplog.at_level("WARNING", logger="alicebot_api.json_bounds"):
        answer = call_mcp_tool(context, name="alice_explain", arguments={"memory_id": memory_id})
    assert TEXT in json.dumps(answer, default=str)
    assert any("runs again with the deep part cut" in record.getMessage() for record in caplog.records)


def test_a_row_inside_the_read_limit_is_read_whole(tmp_path) -> None:
    db, memory_id = _vault_with_a_deep_row(tmp_path, MAX_READ_JSON_DEPTH - 8)
    with sqlite_user_connection(db, USER_ID) as conn:
        row = SQLiteVNextStore(conn, USER_ID).get_memory(memory_id)
    note = row["metadata_json"]["note"]
    levels = 0
    while isinstance(note, list):
        levels += 1
        note = note[0]
    assert levels == MAX_READ_JSON_DEPTH - 8 and note == "x"


def test_the_sqlite_functions_that_read_a_column_survive_a_deep_one(tmp_path) -> None:
    """The functions registered on the connection read ``metadata_json`` as text inside a query. One that raised
    ``RecursionError`` failed the whole query, for every row, not only for the deep one."""

    db, memory_id = _vault_with_a_deep_row(tmp_path, 990)
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for sql in (
            "SELECT alice_direct_source_hint(metadata_json), alice_direct_memory_hint(metadata_json) FROM memories",
            "SELECT alice_project_scope_identity_json(metadata_json, project_id) FROM memories",
        ):
            try:
                assert len(store._fetch_all(sql)) == 1
            except sqlite3.OperationalError as exc:
                assert "no such function" in str(exc), sql
        assert [str(row["id"]) for row in store.list_memories(status=None)] == [memory_id]
        assert store.search_memories(query="cobalt door", limit=5)


@pytest.mark.parametrize("arguments", ({}, {"action": "list"}))
@pytest.mark.parametrize("caller", list(CALLERS))
def test_one_deep_open_loop_does_not_break_the_list_action_for_any_caller(tmp_path, caplog, caller: str, arguments: dict) -> None:
    """``alice_open_loops`` lists with its default action. One loop whose metadata nests 990 levels made the list fail with
    ``RecursionError`` for every caller until the list action was run again with the cut like the six read-only tools; the
    other actions change a loop and are never run twice."""

    from tests.unit.test_importer_per_file_savepoint import _folder
    from tests.unit.test_source_supersede import run_import

    db = _vault(tmp_path)
    source_id = run_import(db, _folder(tmp_path, note=TEXT)).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        loop_id = str(SQLiteVNextStore(conn, USER_ID).create_open_loop(
            {"title": "find the lantern", "description": "d", "resolution_note": None, "source_id": source_id, "status": "open",
             "metadata_json": {}})["id"])
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE open_loops SET metadata_json = ? WHERE id = ?",
                     ('{"keep":1,"note":' + "[" * 989 + '"x"' + "]" * 989 + "}", loop_id))
        conn.commit()
    identity, may_read = CALLERS[caller]
    keyword = {} if identity is None else {"agent_identity": identity, "agent_identity_resolved": True}
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID), **keyword)
    with caplog.at_level("WARNING", logger="alicebot_api.json_bounds"):
        try:
            answer = call_mcp_tool(context, name="alice_open_loops", arguments=dict(arguments))
        except Exception as exc:
            # A key may be refused by policy; it must never be a RecursionError.
            assert type(exc).__name__.startswith("MCP"), (caller, type(exc).__name__)
            return
    assert answer is not None
    # A key that may not read the loop never decodes it, so only the callers that may read it need the second attempt.
    if may_read:
        assert any("runs again with the deep part cut" in record.getMessage() for record in caplog.records)
