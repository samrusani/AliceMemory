"""MCP hints follow Codex's default approval rule.

readOnlyHint is true for a read, including one that writes an event log
row or an agent identity row. alice_memory_review is one: it lists review
items or shows one, and changes no memory, source or revision. destructiveHint
is false for a tool that only adds, and true for a tool that can change or
remove an existing record. openWorldHint is false on every tool. Alice is local.
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
        "alice_memory_review",
    }
)
_ADD_ONLY = frozenset({"alice_memory_commit", "alice_capture"})
_DESTRUCTIVE = frozenset(
    {
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
# What alice_memory_review may write: an event log row for the policy decision,
# the calling agent's identity row, and, with an agent API key, the key's
# last-used time. Nothing else.
_REVIEW_WRITE_TABLES = frozenset({"event_log", "agent_identities", "agent_api_keys"})
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
    audit_tables: frozenset[str] = _AUDIT_TABLES,
) -> None:
    assert set(before) == set(after)
    for table, rows in before.items():
        if table in audit_tables:
            continue
        if table == "sqlite_sequence":
            kept = [row for row in rows if row[0] not in audit_tables]
            now = [row for row in after[table] if row[0] not in audit_tables]
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


def _seed_active_and_candidate(context: MCPRuntimeContext) -> tuple[str, str]:
    """One active memory and one candidate, so the queue and both details have rows."""

    active_id = _seed(context)
    captured = _call(
        context,
        "alice_capture",
        raw_text="Decision: Ship the cedar beta on Friday",
        domain="project",
        sensitivity="internal",
    )
    assert captured["status"] == "imported", captured
    assert captured["candidate_memory_count"] == 1, captured
    queue = _call(context, "alice_memory_review", status="pending_review")
    candidates = [str(item["id"]) for item in queue["items"]]
    assert len(candidates) == 1, queue
    return active_id, candidates[0]


def _review_calls(
    context: MCPRuntimeContext, active_id: str, candidate_id: str, *, identity: dict[str, str]
) -> None:
    """Every shape of list and detail, each one after which no other table may differ."""

    for status in ("correction_ready", "pending_review", "active", "all", "stale"):
        listed = _call(context, "alice_memory_review", status=status, limit=5, **identity)
        assert "items" in listed, (status, listed)
    filtered = _call(
        context,
        "alice_memory_review",
        status="all",
        domains=["project"],
        projects=["cedar"],
        sensitivity_allowed=["public", "internal"],
        **identity,
    )
    assert filtered["count"] == 1, filtered
    for memory_id in (candidate_id, active_id):
        detail = _call(context, "alice_memory_review", review_item_id=memory_id, **identity)
        assert detail["mode"] == "vnext_detail", detail
        assert detail["review"]["memory"]["id"] == memory_id


def test_review_list_and_detail_change_only_event_log_identity_and_key_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """alice_memory_review writes no memory, source, revision or link, in any mode.

    List every status and filter and show a candidate and an active memory,
    with no identity, with an agent identity in the payload, and with an agent
    API key. Every table is snapshotted before and after, FTS pages and the
    insert counters included. Only ``event_log``, ``agent_identities`` and, with
    a key, ``agent_api_keys`` may differ. The hint says read-only because of
    this.

    Mutations, each one alone: a list call rewrites a memory row (the no
    identity case fails); a detail call inserts a source or a revision (the
    detail cases fail); the review writes ``entity_relationship_events`` (every
    case fails, because that table is not in the review's own allowance).
    """

    context = _context(tmp_path, monkeypatch)
    active_id, candidate_id = _seed_active_and_candidate(context)
    database = _db_path(context)

    before = _snapshot(database)
    _review_calls(context, active_id, candidate_id, identity={})
    after = _snapshot(database)
    _assert_other_tables_unchanged(before, after, _REVIEW_WRITE_TABLES)
    assert after == before, "with no identity even the audit tables stay as they were"

    before_identity = _snapshot(database)
    _review_calls(context, active_id, candidate_id, identity=_IDENTITY)
    after_identity = _snapshot(database)
    _assert_other_tables_unchanged(before_identity, after_identity, _REVIEW_WRITE_TABLES)
    assert len(after_identity["event_log"]) > len(before_identity["event_log"])
    assert len(after_identity["agent_identities"]) == len(before_identity["agent_identities"]) + 1

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(database, USER_ID) as connection:
        _record, raw_key = create_agent_key(
            SQLiteVNextStore(connection, USER_ID),
            user_id=USER_ID,
            agent_id="hermes",
            permission_profile="trusted_local_agent",
            label="review hint test",
        )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)
    before_key = _snapshot(database)
    _review_calls(context, active_id, candidate_id, identity={})
    _assert_other_tables_unchanged(before_key, _snapshot(database), _REVIEW_WRITE_TABLES)


_READ_ONLY_IDENTITY = {
    "agent_id": "hermes",
    "agent_type": "personal_assistant",
    "permission_profile": "read_only_agent",
}
_UNKNOWN_MEMORY_ID = "00000000-0000-0000-0000-0000000000aa"


def _seed_sensitive(context: MCPRuntimeContext) -> str:
    """One highly sensitive financial memory, which a read-only agent may not see."""

    proposed = _call(
        context,
        "alice_memory_commit",
        title="Tax note",
        canonical_text="The tax filing sits in the blue folder.",
        memory_type="decision",
        domain="financial",
        sensitivity="highly_sensitive",
        confidence=0.96,
        rationale="User said: remember this",
    )
    memory = proposed["memory"]
    assert memory["domain"] == "financial" and memory["sensitivity"] == "highly_sensitive", proposed
    return str(memory["id"])


def _review_refused(context: MCPRuntimeContext, match: str, **arguments: object) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    with pytest.raises(MCPToolError, match=match):
        _call(context, "alice_memory_review", **arguments)


def test_review_refusals_write_nothing_but_the_audit_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused review call writes no memory, source or revision either.

    A policy block raises after the store context has committed, so a write
    made on that path would persist. Both blocked shapes are run, each with
    every table snapshotted around them: a detail call on a memory a read-only
    agent may not see (answered as a memory that does not exist, and writing
    nothing), and a list call for a domain and sensitivity it may not see; then, with a
    project-scoped agent API key, a list call for another project and a detail
    call on a memory outside the key's project. The
    not-found id and the detail call filtered out by the caller's own domains
    or projects are run too. Only ``event_log``, ``agent_identities`` and, with
    a key, ``agent_api_keys`` may differ, and the blocked calls do leave an
    event log row, so the blocked path was reached.

    Mutations, each one alone: a detail call blocked by policy rewrites a
    memory row; a list call blocked by policy rewrites one; a call blocked
    only by the key's project binding rewrites one. This test fails.
    """

    context = _context(tmp_path, monkeypatch)
    active_id, candidate_id = _seed_active_and_candidate(context)
    sensitive_id = _seed_sensitive(context)
    database = _db_path(context)
    blocked = "agent policy blocked"

    before = _snapshot(database)
    # A memory the agent may not see is answered as a memory that does not exist, and writes what a missing id writes.
    _review_refused(context, "was not found", review_item_id=sensitive_id, **_READ_ONLY_IDENTITY)
    assert _snapshot(database) == before
    _review_refused(
        context,
        blocked,
        status="all",
        domains=["financial"],
        sensitivity_allowed=["private"],
        **_READ_ONLY_IDENTITY,
    )
    _review_refused(context, "was not found", review_item_id=_UNKNOWN_MEMORY_ID, **_READ_ONLY_IDENTITY)
    _review_refused(
        context,
        "outside the effective review filters",
        review_item_id=active_id,
        domains=["financial"],
        **_READ_ONLY_IDENTITY,
    )
    after = _snapshot(database)
    _assert_other_tables_unchanged(before, after, _REVIEW_WRITE_TABLES)
    assert len(after["event_log"]) > len(before["event_log"])

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(database, USER_ID) as connection:
        _record, raw_key = create_agent_key(
            SQLiteVNextStore(connection, USER_ID),
            user_id=USER_ID,
            agent_id="hermes",
            permission_profile="trusted_local_agent",
            label="review refusal test",
            project_scope="cedar",
        )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)
    before_key = _snapshot(database)
    _review_refused(context, blocked, status="all", projects=["nope"])
    _review_refused(context, "was not found", review_item_id=candidate_id)
    _review_refused(
        context, "outside the effective review filters", review_item_id=active_id, projects=["nope"]
    )
    after_key = _snapshot(database)
    _assert_other_tables_unchanged(before_key, after_key, _REVIEW_WRITE_TABLES)
    assert len(after_key["event_log"]) > len(before_key["event_log"])


_ROOT = Path(__file__).resolve().parents[2]


def test_the_docs_and_the_hint_comment_say_review_is_read_only_from_v0192() -> None:
    """v0.19.2 marks the review tool read-only, and the docs keep v0.19.0's flag as history.

    The changelog's v0.19.2 section holds the change and names v0.19.0's flag.
    The README says it in a ``From v0.19.2`` paragraph, and its ``From v0.19.0``
    paragraph still describes v0.19.0's hints. The comment above the read-only
    set says event log row, which is what the tools write.

    Mutations, each one alone: move the entry out of the v0.19.2 section; drop
    the ``In v0.19.0`` clause; put ``alice_memory_review`` back among the
    destructive tools in the README; put ``A policy audit row`` back into the
    comment; drop the note from the control documents; drop the README's ``no
    test here runs that prompt`` caveat; change the tables the changelog says
    review writes with an identity or a key. This test fails.
    """

    sections = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ")
    # Unreleased and the v0.20.0 section may hold entries for changes made after v0.19.2.
    assert sections[1].startswith("Unreleased")
    assert sections[2].startswith("v0.20.0 \u2014 2026-10-02\n")
    # The released hint entry stays in the v0.19.2 section, not in a newer one.
    assert "readOnlyHint" not in sections[1]
    assert "readOnlyHint" not in sections[2]
    assert sections[3].startswith("v0.19.2 \u2014 2026-10-01\n")
    released_now = " ".join(sections[3].split())
    assert "`alice_memory_review` sets `readOnlyHint` and no longer sets `destructiveHint`." in released_now
    assert (
        "In v0.19.0 `alice_memory_review` sets `destructiveHint` to true, grouped with the tools "
        "that act on the review queue, and Codex still asks before it runs."
    ) in released_now
    assert (
        "With an identity only `event_log` and `agent_identities` rows change, and with a key the "
        "key's last-used time changes too"
    ) in released_now
    assert "No test here runs a Codex approval prompt." in released_now

    readme = (_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    released = [line for line in readme if line.startswith("From v0.19.0, every MCP tool declares hints.")]
    assert len(released) == 1
    assert (
        "From v0.19.2, `alice_recall`, `alice_resume`, `alice_context_pack`, `alice_recent_decisions`, "
        "`alice_explain`, and `alice_memory_review` are marked read-only."
    ) in released[0]
    assert "v0.19.0 marked it destructive" in released[0]
    assert "`alice_memory_correct`, `alice_memory_manage`, and `alice_open_loops` are marked destructive" in released[0]
    assert "no test here runs that prompt" in released[0]
    assert not any(line.startswith("On main, not yet released") for line in readme)

    source = (_ROOT / "apps" / "api" / "src" / "alicebot_api" / "mcp" / "definitions.py").read_text(encoding="utf-8")
    assert "A policy audit row" not in source
    assert "An event log row or\n# an agent identity row is not a state change the client asked for." in source

    state = " ".join((_ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8").split())
    assert (
        "From `v0.19.2`, `alice_memory_review` is read-only, so with the full tool "
        "set six tools are read-only and three are destructive."
    ) in state
