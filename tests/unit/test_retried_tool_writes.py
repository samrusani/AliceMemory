"""A tool call that is run again after a reader overflowed writes the events an ordinary call writes, once.

Six tools are marked read-only (``alice_recall``, ``alice_resume``, ``alice_context_pack``, ``alice_recent_decisions``,
``alice_explain``, ``alice_memory_review``), and ``alice_open_loops`` with its ``list`` action reads too. A caller that
declares an agent, or holds a key, still makes them write: the agent identity row and the events of the policy check
(``agent.identity_upserted``, ``policy.decision``, ``agent.policy_filtered`` for a list), and a context pack its own two.
When a reader overflows on a deep stored row the call is run once more (``retry_cutting_deep_json``). The first attempt's
transaction for the policy check had committed already, and the second attempt wrote the events again: at 990 levels a call
of ``alice_recall`` or ``alice_resume`` left two of each, and the list of ``alice_memory_review`` two of each of three.

The second attempt now takes the result of what the first committed (``durable_unit`` in the store context of the tool,
``durable_once`` around the compile of a context pack) and writes nothing of it again. A first attempt that rolled back
left nothing, and the second writes as an ordinary call does.

Each call is made twice on one vault, once as it is and once with a handler that does its work and then overflows, as a
reader does after the transaction closed. The events the second call wrote must equal the events the first wrote. The same
holds on a vault whose row is really too deep, where the overflow comes from the reader. Both are run for a caller that
declares an agent in the arguments and for a caller that holds a key.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``_policy_checked`` does not return the result the first attempt committed: both tests, for every tool.
* ``_vnext_store_context`` is not a ``durable_unit``: the same.
* the compile of a context pack is not run through ``durable_once``: the ``alice_context_pack`` cases.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from uuid import UUID

import pytest

from alicebot_api import json_bounds
from alicebot_api.mcp import registry
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.unit.test_importer_per_file_savepoint import USER_ID
from tests.unit.test_stored_deep_rows_are_read import _calls, _vault_with_a_deep_row

DECLARED = {"agent_id": "probe-agent", "agent_type": "coding_agent", "permission_profile": "trusted_local_agent"}
WHO = ("a declared agent", "a key")
# The calls of ``test_stored_deep_rows_are_read``: both forms of ``alice_memory_review``, ``alice_explain``, ``alice_recall``,
# ``alice_context_pack``, ``alice_resume``, ``alice_recent_decisions`` and the list action of ``alice_open_loops``.
COUNT = len(_calls("x"))


@pytest.fixture(autouse=True)
def _surface(monkeypatch):
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)


def _events(db) -> Counter:
    with closing(sqlite3.connect(db)) as conn:
        return Counter(dict(conn.execute("SELECT event_type, count(*) FROM event_log GROUP BY 1").fetchall()))


def _caller(db, who: str, monkeypatch) -> dict:
    """The arguments a caller adds to every call: a declared agent, or nothing when it holds a key (the key is in the
    environment, as the server reads it)."""

    if who == "a declared agent":
        return dict(DECLARED)
    with sqlite_user_connection(db, USER_ID) as conn:
        _record, raw = create_agent_key(
            SQLiteVNextStore(conn, USER_ID), user_id=UUID(USER_ID), agent_id="probe-key", permission_profile="trusted_local_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
    return {}


def _call(db, name: str, arguments: dict):
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID))
    return call_mcp_tool(context, name=name, arguments=dict(arguments))


def _written(db, name: str, arguments: dict) -> Counter:
    before = _events(db)
    _call(db, name, arguments)
    after = _events(db)
    return Counter({kind: after[kind] - before[kind] for kind in after if after[kind] != before[kind]})


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("index", range(COUNT))
def test_a_call_whose_handler_overflows_after_its_work_writes_the_events_of_an_ordinary_call(tmp_path, monkeypatch, who: str, index: int) -> None:
    db, memory_id = _vault_with_a_deep_row(tmp_path, 3)
    name, base = _calls(memory_id)[index]
    arguments = {**base, **_caller(db, who, monkeypatch)}
    ordinary = _written(db, name, arguments)
    assert ordinary["policy.decision"] == 1, (name, ordinary)

    real = registry._TOOL_HANDLERS[name]
    attempts: list[bool] = []

    def overflows_once(context, handler_arguments):
        result = real(context, handler_arguments)
        attempts.append(json_bounds._CUT_DEEP.get())
        if not json_bounds._CUT_DEEP.get():
            raise RecursionError("a reader, after the transaction closed")
        return result

    monkeypatch.setitem(registry._TOOL_HANDLERS, name, overflows_once)
    retried = _written(db, name, arguments)
    assert attempts == [False, True], (name, attempts)
    assert retried == ordinary, (name, who, dict(retried), dict(ordinary))


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("index", range(COUNT))
def test_a_call_that_a_really_deep_row_makes_run_again_writes_the_events_of_an_ordinary_call(tmp_path, monkeypatch, who: str, index: int) -> None:
    shallow, shallow_id = _vault_with_a_deep_row(tmp_path / "shallow", 3)
    deep, deep_id = _vault_with_a_deep_row(tmp_path / "deep", 990)
    name, _ = _calls(shallow_id)[index]
    ordinary = _written(shallow, name, {**_calls(shallow_id)[index][1], **_caller(shallow, who, monkeypatch)})
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    retried = _written(deep, name, {**_calls(deep_id)[index][1], **_caller(deep, who, monkeypatch)})
    assert retried == ordinary, (name, who, dict(retried), dict(ordinary))


def test_a_first_attempt_that_rolled_back_is_written_by_the_second(tmp_path, monkeypatch) -> None:
    """The handler overflows inside the transaction that made its policy check, so nothing of the first attempt is in the
    file and the second attempt must write the events, once. The baseline is the same handler on a call that does not
    overflow."""

    from alicebot_api.mcp.policy import _agent_identity_from_arguments, _policy_checked
    from alicebot_api.mcp.runtime import _vnext_store_context
    from alicebot_api.project_view import ProjectView

    db, _memory_id = _vault_with_a_deep_row(tmp_path, 3)
    arguments = {"query": "cobalt door", **DECLARED}

    def checks_then_overflows(context, handler_arguments):
        with _vnext_store_context(context) as store:
            _policy_checked(store, identity=_agent_identity_from_arguments(context, handler_arguments),
                            action="memory.recall", project_view=ProjectView.unscoped())
            if not json_bounds._CUT_DEEP.get():
                raise RecursionError("inside the transaction")
        return {"ok": True}

    monkeypatch.setitem(registry._TOOL_HANDLERS, "alice_recall", checks_then_overflows)
    with json_bounds.cutting_deep_json():
        ordinary = _written(db, "alice_recall", arguments)
    retried = _written(db, "alice_recall", arguments)
    assert ordinary["policy.decision"] == 1
    assert retried == ordinary, (dict(retried), dict(ordinary))


def test_the_ledger_keeps_a_result_only_when_its_unit_commits() -> None:
    seen: list[str] = []

    def call() -> None:
        with json_bounds.durable_unit():
            if json_bounds.is_remembered(json_bounds.replay_durable("committed")):
                seen.append("replayed committed")
            else:
                json_bounds.remember_durable("committed", 1)
                seen.append("made committed")
        with json_bounds.durable_unit():
            if json_bounds.is_remembered(json_bounds.replay_durable("rolled back")):
                seen.append("replayed rolled back")
            else:
                json_bounds.remember_durable("rolled back", 2)
                seen.append("made rolled back")
            if not json_bounds._CUT_DEEP.get():
                raise RecursionError("the first attempt fails inside the second unit")

    json_bounds.retry_cutting_deep_json(call)
    assert seen == ["made committed", "made rolled back", "replayed committed", "made rolled back"]
    # Outside a call that can be run again nothing is remembered.
    with json_bounds.durable_unit():
        json_bounds.remember_durable("x", 1)
    assert not json_bounds.is_remembered(json_bounds.replay_durable("x"))


def test_a_value_is_cut_in_memory_as_the_decoder_cuts_a_text() -> None:
    for depth in (10, 255, 256, 257, 300, 5000):
        text = "[" * depth + '"x"' + "]" * depth
        value: object = "x"
        for _ in range(depth):
            value = [value]
        wrapped = {"keep": 1, "note": value, "tuple": (1, [2, {"k": [3]}])}
        cut = json_bounds.cut_deep_value(wrapped)
        decoded = json_bounds.loads_bounded('{"keep": 1, "note": ' + text + ', "tuple": [1, [2, {"k": [3]}]]}')
        assert cut == decoded, depth
    assert json_bounds.cut_deep_value("x") == "x"
    assert json_bounds.cut_deep_value([1, 2]) == [1, 2]
