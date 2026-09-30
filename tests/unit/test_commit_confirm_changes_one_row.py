"""`alice_memory_commit` can finish a pending write, and that changes exactly one memory row.

Security review finding DB-007. v0.19.0 annotates `alice_memory_commit` with `destructiveHint: false`,
the same as `alice_capture`. By the approval rule the code records for Codex's default
mode, such a tool runs with no prompt. The hint is true of adding a fact. It is not true
of the call that carries `confirmation_id` and `confirmation_action`: that call updates
the pending row (`needs_review` to `active`, or to `rejected`), writes a revision and
events, and touches the caller's agent identity row when it carries one. Nothing in
Alice can tell whether the agent asked the user first, so the confirm step is not a
control.

The annotation and the route are unchanged. The docs now say what the call does. These
tests pin the claim the docs make, so the claim cannot grow without a failing test:

- the call changes one `memories` row, the named pending one, and no other row in any table
  beyond its own revision, its own events, and the caller's identity row;
- a different keyed agent cannot resolve someone else's pending write, and a refused
  attempt changes no memory row and no revision.

Every test here goes through `alice_memory_commit`, which is one of the three default tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

USER_ID = "00000000-0000-0000-0000-000000000001"
TITLE = "Billing deploy day"
TEXT = "The team deploys the billing service on Thursdays."
OTHER_TEXT = "The search team reviews ranking changes on Wednesdays."
# Tables a confirm or reject may touch. `memories_fts*` are the full-text index
# tables SQLite keeps for `memories`; they follow that one row.
ALLOWED_TABLES = {"memories", "memory_revisions", "event_log", "agent_identities"}


@pytest.fixture(autouse=True)
def keyless_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)


def _context(tmp_path: Path):
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _call(context, **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name="alice_memory_commit", arguments=arguments)


def _store(context, reader):
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as connection:
        return reader(SQLiteVNextStore(connection, USER_ID))


def _pending(context, *, agent_id: str, text: str = TEXT, title: str = TITLE, **identity) -> dict:
    payload = _call(context, title=title, canonical_text=text, confidence=0.7, agent_id=agent_id, **identity)
    assert payload["status"] == "confirmation_required", payload
    assert payload["memory"]["status"] == "needs_review", payload
    return payload


def _snapshot(context) -> dict[str, list[str]]:
    """Every row of every table, as sorted JSON strings, minus the FTS index tables."""

    def read(store) -> dict[str, list[str]]:
        names = [
            dict(row)["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        tables: dict[str, list[str]] = {}
        for name in names:
            if name.startswith("memories_fts"):
                continue
            rows = [dict(row) for row in store.conn.execute(f"SELECT * FROM {name}").fetchall()]
            tables[name] = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
        return tables

    return _store(context, read)


def _diff(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict[str, tuple[list[dict], list[dict]]]:
    """Per changed table: (rows only in `before`, rows only in `after`)."""

    changed: dict[str, tuple[list[dict], list[dict]]] = {}
    for name in sorted(set(before) | set(after)):
        old, new = set(before.get(name, [])), set(after.get(name, []))
        if old != new:
            changed[name] = (
                [json.loads(row) for row in sorted(old - new)],
                [json.loads(row) for row in sorted(new - old)],
            )
    return changed


def _memory_status(context, memory_id: str) -> str:
    return str(_store(context, lambda store: store.get_memory(memory_id))["status"])


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("confirm", "active"), ("reject", "rejected")],
)
def test_finishing_a_pending_write_changes_one_memory_row(
    tmp_path: Path, action: str, expected_status: str
) -> None:
    """The row named by `confirmation_id` moves, and no other memory row or other table does.

    Two bystanders sit in the same vault: a committed memory from another agent and
    another agent's pending write. The call may not touch either.

    Mutation: make `VNextMemoryCommitService.confirm` also update a second memory row
    (any other row, including another agent's). The `memories` assertions fail.
    Mutation: make it write a row to a table outside the list, such as an open loop.
    The changed-tables assertion fails.
    """

    context = _context(tmp_path)
    committed = _call(
        context,
        title="Search review day",
        canonical_text=OTHER_TEXT,
        confidence=0.95,
        agent_id="bystander",
        permission_profile="trusted_local_agent",
    )
    assert committed["status"] == "committed", committed
    bystander_pending = _pending(
        context,
        agent_id="bystander",
        text="The search team pairs on Fridays.",
        title="Search pairing",
        permission_profile="trusted_local_agent",
    )
    pending = _pending(context, agent_id="hermes")
    pending_id = str(pending["memory"]["id"])

    before = _snapshot(context)
    result = _call(
        context,
        confirmation_id=pending["confirmation_id"],
        confirmation_action=action,
        agent_id="hermes",
    )
    after = _snapshot(context)

    assert result["status"] == ("committed" if action == "confirm" else "rejected"), result
    changed = _diff(before, after)
    assert set(changed) <= ALLOWED_TABLES, sorted(set(changed) - ALLOWED_TABLES)

    removed, added = changed["memories"]
    assert [row["id"] for row in removed] == [pending_id], "exactly the named pending row was replaced"
    assert [row["id"] for row in added] == [pending_id]
    assert removed[0]["status"] == "needs_review"
    assert added[0]["status"] == expected_status
    assert _memory_status(context, str(bystander_pending["memory"]["id"])) == "needs_review"
    assert _memory_status(context, str(committed["memory"]["id"])) == "active"

    revisions_removed, revisions_added = changed["memory_revisions"]
    assert revisions_removed == [], "a revision is added, never rewritten"
    assert [row["memory_id"] for row in revisions_added] == [pending_id]

    events_removed, events_added = changed["event_log"]
    assert events_removed == [], "the event log is append-only"
    for event in events_added:
        if event["target_type"] == "memory":
            assert event["target_id"] == pending_id, event["event_type"]
        else:
            assert event["target_type"] == "agent_identity", event["event_type"]
            assert event["event_type"] == "agent.identity_upserted", event["event_type"]
    assert {event["event_type"] for event in events_added if event["target_type"] == "memory"} >= {
        "agent.memory_confirmed" if action == "confirm" else "agent.memory_confirmation_rejected",
        "memory.updated",
    }

    identities_removed, identities_added = changed["agent_identities"]
    assert [row["agent_id"] for row in identities_removed] == ["hermes"]
    assert [row["agent_id"] for row in identities_added] == ["hermes"]


def _mint_key(context, monkeypatch: pytest.MonkeyPatch, *, agent_id: str) -> None:
    """Mint a key for `agent_id` and make the MCP boundary resolve every call with it."""

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
    from alicebot_api.vnext_agent_keys import create_agent_key

    _record, raw = _store(
        context,
        lambda store: create_agent_key(
            store, user_id=USER_ID, agent_id=agent_id, permission_profile="trusted_local_agent"
        ),
    )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw)


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_a_different_keyed_agent_changes_no_memory_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A key for another agent cannot resolve this agent's pending write.

    Both agents hold a trusted_local_agent key with no project bound, so the author
    check in `caller_may_resolve_pending_write` is the only gate between them. A refused
    attempt writes an audit trail and the key's own last-used stamp, and nothing else: no
    memory row and no revision change.

    Mutation: drop the author check in `caller_may_resolve_pending_write`, so it returns
    True for any keyed caller. The refusal assertion fails.
    """

    from alicebot_api.mcp_tools import MCPToolError
    from alicebot_api.vnext_memory_commit import PENDING_WRITE_RESOLVER_REASON

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="alpha")
    pending = _pending(context, agent_id="alpha")
    pending_id = str(pending["memory"]["id"])

    _mint_key(context, monkeypatch, agent_id="beta")
    before = _snapshot(context)
    with pytest.raises(MCPToolError, match=PENDING_WRITE_RESOLVER_REASON):
        _call(context, confirmation_id=pending["confirmation_id"], confirmation_action=action)
    after = _snapshot(context)

    changed = _diff(before, after)
    assert "memories" not in changed
    assert "memory_revisions" not in changed
    # The key's own last-used stamp moves when it is presented, refused or not.
    assert set(changed) <= {"event_log", "agent_identities", "agent_api_keys"}, sorted(changed)
    for table in ("agent_identities", "agent_api_keys"):
        for row in changed.get(table, ([], []))[1]:
            assert row["agent_id"] == "beta", (table, row["agent_id"])
    assert _memory_status(context, pending_id) == "needs_review"
    _removed, events_added = changed.get("event_log", ([], []))
    assert "agent.memory_confirmed" not in {event["event_type"] for event in events_added}
    assert "agent.memory_confirmation_rejected" not in {event["event_type"] for event in events_added}

    # The author, holding the same kind of key, can finish it, so the refusal above
    # was the author check and not something else about this row.
    _mint_key(context, monkeypatch, agent_id="alpha")
    finished = _call(context, confirmation_id=pending["confirmation_id"], confirmation_action="confirm")
    assert finished["status"] == "committed", finished
    assert _memory_status(context, pending_id) == "active"
