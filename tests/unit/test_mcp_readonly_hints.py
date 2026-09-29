"""MCP hints follow Codex's default approval rule.

readOnlyHint is true for a read, including one that writes a policy audit
row or an agent identity row. destructiveHint is false for a tool that
only adds, and true for a tool that can change or remove an existing
record. openWorldHint is false on every tool. Alice is local.
"""

from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS
from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext

USER_ID = "00000000-0000-0000-0000-000000000001"
_READ_ONLY = frozenset(
    {
        "alice_recall",
        "alice_resume",
        "alice_context_pack",
        "alice_recent_decisions",
        "alice_explain",
    }
)
_ADD_ONLY = frozenset({"alice_memory_commit", "alice_capture"})
_DESTRUCTIVE = frozenset(
    {
        "alice_memory_review",
        "alice_memory_correct",
        "alice_memory_manage",
        "alice_open_loops",
    }
)
_AUDIT_TABLES = frozenset(
    {
        "event_log",
        "entity_relationship_events",
        "agent_identities",
        "agent_api_keys",
    }
)
_IDENTITY = {
    "agent_id": "hermes",
    "agent_type": "personal_assistant",
    "permission_profile": "trusted_local_agent",
}


def _expected_annotations(name: str) -> dict[str, bool]:
    annotations = {"openWorldHint": False}
    if name in _READ_ONLY:
        annotations["readOnlyHint"] = True
    elif name in _ADD_ONLY:
        annotations["destructiveHint"] = False
    elif name in _DESTRUCTIVE:
        annotations["destructiveHint"] = True
    return annotations


def _listed_tools(monkeypatch: pytest.MonkeyPatch, *, full: bool) -> list[dict[str, object]]:
    if full:
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    else:
        monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
    context = MCPRuntimeContext(
        database_url="sqlite:///unused.db",
        user_id=UUID(USER_ID),
    )
    server = MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response is not None
    tools = response["result"]["tools"]
    assert tools == list_mcp_tools()
    return tools


def test_tools_list_annotations_for_default_and_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both advertised sets carry the hints, and every tool is local.

    Mutation: drop readOnlyHint, or leave openWorldHint unset. This test fails.
    """

    for full in (False, True):
        tools = _listed_tools(monkeypatch, full=full)
        names = [str(tool["name"]) for tool in tools]
        if full:
            assert set(_READ_ONLY | _ADD_ONLY | _DESTRUCTIVE) <= set(names)
        else:
            assert names == ["alice_memory_commit", "alice_recall", "alice_resume"]
        for tool in tools:
            name = str(tool["name"])
            assert tool["annotations"] == _expected_annotations(name), name

    for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS):
        name = str(tool["name"])
        assert tool["annotations"] == _expected_annotations(name), name


def _context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
    from alicebot_api.vnext_embeddings import (
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
    )

    for name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
        "ALICE_MCP_LEGACY_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _db_path(context: MCPRuntimeContext) -> Path:
    from alicebot_api.mcp_tools import _sqlite_path_from_url

    return Path(_sqlite_path_from_url(context.database_url))


def _snapshot(database: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(database)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        snap: dict[str, list[tuple[object, ...]]] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
            snap[table] = sorted(tuple(row) for row in rows)
        sequence = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'sqlite_sequence'"
        ).fetchone()
        if sequence is not None:
            rows = connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
            snap["sqlite_sequence"] = sorted((str(name), int(seq)) for name, seq in rows)
        return snap
    finally:
        connection.close()


def _assert_other_tables_unchanged(
    before: dict[str, list[tuple[object, ...]]],
    after: dict[str, list[tuple[object, ...]]],
) -> None:
    assert set(before) == set(after)
    for table, rows in before.items():
        if table in _AUDIT_TABLES:
            continue
        if table == "sqlite_sequence":
            kept = [row for row in rows if row[0] not in _AUDIT_TABLES]
            now = [row for row in after[table] if row[0] not in _AUDIT_TABLES]
            assert now == kept, table
            continue
        assert after[table] == rows, table


def _assert_no_existing_row_changed(
    before: dict[str, list[tuple[object, ...]]],
    after: dict[str, list[tuple[object, ...]]],
) -> None:
    for table, rows in before.items():
        # sqlite_sequence is the insert counter. A new row bumps it.
        # FTS shadow pages are rewritten when a new document is indexed.
        # The stored memory and source rows are the rows this check covers.
        if table == "sqlite_sequence" or "_fts" in table:
            continue
        remaining = list(after[table])
        for row in rows:
            assert row in remaining, (table, row)
            remaining.remove(row)


def _call(context: MCPRuntimeContext, name: str, **arguments: object) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _seed(context: MCPRuntimeContext) -> str:
    committed = _call(
        context,
        "alice_memory_commit",
        title="Seed fact",
        canonical_text="The cedar launch checklist stays on the north wall.",
        memory_type="decision",
        domain="project",
        sensitivity="public",
        confidence=0.96,
        project_scope=["cedar"],
        rationale="User said: remember this",
    )
    assert committed["status"] == "committed", committed
    captured = _call(
        context,
        "alice_capture",
        raw_text="Cedar launch notes for the north wall checklist.",
        title="Cedar notes",
        domain="project",
        sensitivity="public",
    )
    assert captured["status"] == "imported", captured
    return str(committed["memory"]["id"])


def _read_only_calls(context: MCPRuntimeContext, memory_id: str, *, identity: dict[str, str]) -> None:
    recalled = _call(context, "alice_recall", query="cedar launch checklist", **identity)
    assert recalled.get("results") is not None
    resumed = _call(context, "alice_resume", **identity)
    assert "brief" in resumed
    packed = _call(context, "alice_context_pack", query="cedar launch", **identity)
    assert packed.get("memories") is not None or "error" not in packed
    decisions = _call(context, "alice_recent_decisions", **identity)
    assert decisions is not None
    explained = _call(context, "alice_explain", memory_id=memory_id, **identity)
    assert explained is not None


def test_read_only_tools_leave_every_other_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read changes no table except events and agent identity.

    Run each read-only tool with no identity and with an agent identity.
    Mutation: a read rewrites a memory row. This test fails.
    """

    context = _context(tmp_path, monkeypatch)
    memory_id = _seed(context)
    database = _db_path(context)
    before = _snapshot(database)
    _read_only_calls(context, memory_id, identity={})
    _assert_other_tables_unchanged(before, _snapshot(database))

    before_identity = _snapshot(database)
    _read_only_calls(context, memory_id, identity=_IDENTITY)
    _assert_other_tables_unchanged(before_identity, _snapshot(database))

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(database, USER_ID) as connection:
        _record, raw_key = create_agent_key(
            SQLiteVNextStore(connection, USER_ID),
            user_id=USER_ID,
            agent_id="hermes",
            permission_profile="trusted_local_agent",
            label="hint test",
        )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)
    before_key = _snapshot(database)
    _read_only_calls(context, memory_id, identity={})
    _assert_other_tables_unchanged(before_key, _snapshot(database))


def test_add_only_tools_do_not_change_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit and capture may insert. They do not delete or update a row.

    Mutation: an add rewrites the seeded memory. This test fails.
    """

    context = _context(tmp_path, monkeypatch)
    _seed(context)
    database = _db_path(context)
    before = _snapshot(database)
    added = _call(
        context,
        "alice_memory_commit",
        title="Added fact",
        canonical_text="A second cedar note was added after the seed.",
        memory_type="semantic",
        domain="project",
        sensitivity="public",
        confidence=0.96,
        rationale="User said: remember this",
        **_IDENTITY,
    )
    assert added["status"] == "committed", added
    captured = _call(
        context,
        "alice_capture",
        raw_text="A second captured cedar note, added after the seed.",
        title="More cedar notes",
        domain="project",
        sensitivity="public",
        **_IDENTITY,
    )
    assert captured["status"] == "imported", captured
    _assert_no_existing_row_changed(before, _snapshot(database))
