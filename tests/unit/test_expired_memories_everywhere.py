"""A memory whose ``valid_to`` has passed is left out of every door that shows or sends it.

Recall, the context pack and the embedding door (write, reindex, backfill, the doctor
count) already skipped such a memory in v0.20.0. Four doors did not:

* ``alice_resume``, the session brief (and the SessionStart hook that injects it) and
  ``alice_recent_decisions`` listed memories by status, so an active memory that
  ``alice_memory_manage`` with ``action: expire`` had closed still showed.
* Consolidation and the roll-up semantic tier re-embedded memories for clustering, so
  the text of an expired memory could go to the embeddings endpoint, and a merge or a
  card could carry it into a new memory.
* ``list_accepted_rollup_cards`` had no expiry test, so an expired card still counted
  as the accepted card for its topic.
* Promoting a reviewed artifact made an active memory and never embedded it.

The reads take recall's own test: ``_expiry_clause`` on SQLite and
``POSTGRES_UNEXPIRED_SQL`` on Postgres, applied before ``LIMIT``, with one Python form
(``memory_window_is_open``) for a row read by id or from a store that cannot apply the SQL.

Every test that talks to an embeddings endpoint talks to a fake one on 127.0.0.1, started
by the helper of ``test_reindex_live_memories_only``, that records every text it receives.
Nothing here reaches the network or a paid API, and no Postgres is needed: the Postgres
statements are checked as text, and ``tests/integration/test_expired_memories_postgres.py``
runs them against a live database in CI.

Each test names, in its docstring, the change to the code that must fail it.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import alicebot_api.cli as cli_module
from alicebot_api import session_briefing, vnext_queue
from alicebot_api.config import Settings
from alicebot_api.mcp import evidence_artifacts as mcp_evidence_artifacts
from alicebot_api.mcp import retrieval as mcp_retrieval
from alicebot_api.mcp import runtime as mcp_runtime
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.routers import _vnext_embeddings as route_embeddings
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.session_briefing import FACT_LIMIT, compile_local_session_brief, compile_session_brief
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_agent_control import PolicyDecision
from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
    attach_memory_embedding,
    persist_deferred_memory_embeddings_best_effort,
)
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_queue import VNextQueueService, VNextQueueValidationError
from alicebot_api.vnext_recall_visibility import (
    POSTGRES_UNEXPIRED_SQL,
    drop_expired_memories,
    memory_window_is_open,
    postgres_unexpired_sql,
)
from alicebot_api.vnext_rollups import ROLLUP_CANDIDATE_KIND, VNextRollupService
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.project_view import ProjectView
from tests.unit.test_reindex_live_memories_only import (
    _configure,
    _marker,
    _RecordingEmbeddingsServer,
    _vectors_present,
)

USER = "00000000-0000-4000-8000-000000000001"
PAST = "2020-01-01T00:00:00Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"
ALL_SENSITIVITY = ("public", "internal", "private", "unknown")
SOURCE_ROOT = Path(inspect.getsourcefile(session_briefing)).parent  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient provider, agent key or plugin root, and the full tool list."""

    for name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_MAX_INPUT_CHARS_ENV,
        AGENT_API_KEY_ENV,
        "CLAUDE_PLUGIN_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _memory_store() -> Iterator[SQLiteVNextStore]:
    """A SQLite store in memory, the way the roll-up and consolidation tests build one."""

    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    user_id = str(uuid4())
    ensure_sqlite_user(connection, user_id, "expiry@example.com", "Expiry")
    try:
        yield SQLiteVNextStore(connection, user_id)
    finally:
        connection.close()


def _memory(
    store: SQLiteVNextStore,
    label: str,
    *,
    memory_type: str = "semantic",
    status: str = "active",
    valid_to: str | None = None,
    metadata: dict[str, object] | None = None,
    embed: bool = False,
    domain: str = "project",
    sensitivity: str = "private",
) -> str:
    """One memory whose text is ``_marker(label)``; with ``embed`` it also gets a vector."""

    row = store.create_memory(
        {
            "memory_key": f"expiry-{label}",
            "value": {"text": _marker(label)},
            "memory_type": memory_type,
            "title": f"Title {label}",
            "canonical_text": _marker(label),
            "status": status,
            "domain": domain,
            "sensitivity": sensitivity,
            "valid_to": valid_to,
            "metadata_json": metadata or {},
        }
    )
    if embed:
        attach_memory_embedding(store, row)
    return str(row["id"])


def _expire(store: SQLiteVNextStore, memory_id: str) -> None:
    """Close the window the way a person does, with the ``expire`` action."""

    VNextMemoryCommitService(store).expire(memory_id, reason="no longer true")


class _Vault:
    """A SQLite vault on disk, reached through the MCP tools, the brief and the hook."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = resolve_db_path(data_dir=str(tmp_path), db=None)
        bootstrap_database(self.db_path, user_id=USER, user_email="local@alice")
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.db_path), user_id=USER)

    def add(self, label: str, **kwargs: object) -> str:
        with sqlite_user_connection(self.db_path, USER) as connection:
            return _memory(SQLiteVNextStore(connection, USER), label, **kwargs)  # type: ignore[arg-type]

    def expire(self, memory_id: str) -> None:
        with sqlite_user_connection(self.db_path, USER) as connection:
            _expire(SQLiteVNextStore(connection, USER), memory_id)

    def unexpire(self, memory_id: str) -> None:
        with sqlite_user_connection(self.db_path, USER) as connection:
            VNextMemoryCommitService(SQLiteVNextStore(connection, USER)).unexpire(memory_id, reason="still true")

    def call(self, name: str, **arguments: object) -> dict[str, object]:
        return call_mcp_tool(self.context, name=name, arguments=arguments)

    def brief(self) -> str:
        return compile_local_session_brief(
            self.db_path, user_id=USER, query=None,
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
        )


def _titles(rows: object) -> list[str]:
    assert isinstance(rows, list)
    return [str(row["title"]).strip('"') for row in rows]


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valid_to", "open_window"),
    [
        (None, True),
        ("", True),
        (FAR_FUTURE, True),
        (datetime(2999, 1, 1, tzinfo=UTC), True),
        (PAST, False),
        ("2020-01-01T00:00:00+00:00", False),
        ("2020-01-01T00:00:00", False),
        (datetime(2020, 1, 1, tzinfo=UTC), False),
        ("not a date", True),
    ],
)
def test_the_python_window_test_and_the_row_filter_agree_on_every_form(valid_to: object, open_window: bool) -> None:
    """``memory_window_is_open`` and ``drop_expired_memories`` read ``valid_to`` the way recall's SQL does.

    A row with no end, an empty end or one that is not a date has an open window. The
    filter keeps the order of the rows it keeps.

    Mutations, each one alone: make ``memory_window_is_open`` return ``True`` for every row
    (the past rows stay); negate it (the open rows go); make ``drop_expired_memories`` sort
    or reverse the rows it keeps (the order check fails).
    """

    row = {"id": "x", "valid_to": valid_to}
    assert memory_window_is_open(row) is open_window
    first, last = {"id": "first", "valid_to": None}, {"id": "last", "valid_to": FAR_FUTURE}
    kept = drop_expired_memories([last, row, first])
    assert [item["id"] for item in kept] == (["last", "x", "first"] if open_window else ["last", "first"])


def test_the_postgres_test_takes_its_text_from_one_function_with_an_alias_form() -> None:
    """The statements that join a table alias use the same text as the ones that do not.

    Mutations, each one alone: let ``postgres_unexpired_sql`` ignore its prefix (the alias
    form loses ``m.``); change the comparison in it (``>`` for ``>=``): the shared constant,
    which the reindex list uses, changes with it and the equality below still holds, so the
    assertion on the literal text fails.
    """

    assert POSTGRES_UNEXPIRED_SQL == postgres_unexpired_sql()
    assert postgres_unexpired_sql() == "(valid_to IS NULL OR valid_to >= clock_timestamp())"
    assert postgres_unexpired_sql("m.") == "(m.valid_to IS NULL OR m.valid_to >= clock_timestamp())"


# ---------------------------------------------------------------------------
# The SQLite reads, on a real store
# ---------------------------------------------------------------------------


def test_the_sqlite_list_and_count_leave_out_an_expired_memory_before_the_limit() -> None:
    """``include_expired=False`` skips the newest memory, which is expired, and ``LIMIT 1`` returns the next one.

    The default still lists and counts it: review, confirm, unexpire and export need to see
    an expired memory.

    Mutations, each one alone: make the ``_expiry_clause`` call of ``list_memories`` take ``True``,
    which takes the clause and its parameter out of the statement (the limited list returns the
    expired row); do the same in ``count_memories`` (the count is 3); change the default of
    ``include_expired`` to ``False`` (the default list loses the row).
    """

    with _memory_store() as store:
        _memory(store, "old")
        middle = _memory(store, "middle")
        gone = _memory(store, "gone", valid_to=PAST)
        assert [row["id"] for row in store.list_memories(status="active", order_by_created_at=True, limit=1)] == [gone]
        open_only = store.list_memories(
            status="active", order_by_created_at=True, limit=1, include_expired=False
        )
        assert [row["id"] for row in open_only] == [middle]
        assert store.count_memories(status="active") == 3
        assert store.count_memories(status="active", include_expired=False) == 2
        # a window that closes later is open now
        _memory(store, "later", valid_to=FAR_FUTURE)
        assert store.count_memories(status="active", include_expired=False) == 3


def test_the_sqlite_resume_events_leave_out_an_expired_memory_before_the_limit() -> None:
    """The newest event is the creation of an expired memory; ``LIMIT 1`` returns the event before it.

    Mutation: make the ``_expiry_clause`` call in ``list_resume_memory_events`` in ``sqlite_store.py``
    take ``True``. The limited list returns the event of the expired memory.
    """

    with _memory_store() as store:
        _memory(store, "old")
        middle = _memory(store, "middle")
        gone = _memory(store, "gone", valid_to=PAST)
        events = store.list_resume_memory_events(statuses=("active",), limit=1, domains=None, sensitivity_allowed=None)
        assert [event["target_id"] for event in events] == [middle]
        every = store.list_resume_memory_events(statuses=("active",), limit=10, domains=None, sensitivity_allowed=None)
        assert gone not in {event["target_id"] for event in every}


def test_the_sqlite_rollup_input_list_and_count_leave_out_an_expired_memory() -> None:
    """The roll-up groups what recall can return: the list and the count skip the newest, expired memory.

    Mutations, each one alone: make the ``_expiry_clause`` call of ``list_rollup_input_memories``
    take ``True`` (the limited list returns the expired row); do the same in
    ``count_rollup_input_memories`` (the count is 3).
    """

    with _memory_store() as store:
        _memory(store, "old")
        middle = _memory(store, "middle")
        _memory(store, "gone", valid_to=PAST)
        arguments = {
            "domains": None,
            "sensitivity_allowed": list(ALL_SENSITIVITY),
            "excluded_candidate_kind": ROLLUP_CANDIDATE_KIND,
        }
        assert [row["id"] for row in store.list_rollup_input_memories(limit=1, **arguments)] == [middle]
        assert store.count_rollup_input_memories(**arguments) == 2


def _card(store: SQLiteVNextStore, label: str, key: str, *, valid_to: str | None = None) -> str:
    return _memory(
        store,
        label,
        status="accepted",
        valid_to=valid_to,
        metadata={"candidate_kind": ROLLUP_CANDIDATE_KIND, "rollup_key": key},
    )


def _accepted_cards(store: object, key: str = "topic:games") -> list[dict[str, object]]:
    return store.list_accepted_rollup_cards(  # type: ignore[attr-defined]
        rollup_keys=(key,),
        domains=None,
        sensitivity_allowed=list(ALL_SENSITIVITY),
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=5,
    )


def test_the_sqlite_accepted_card_lookup_skips_an_expired_card_and_picks_the_open_one() -> None:
    """The newest card for a key has expired; the older card that is still open is the accepted card.

    The test sits inside the ranking query, so it runs before the one card per key is picked.
    With every card expired there is no accepted card.

    Mutation: make the ``_expiry_clause`` call of ``list_accepted_rollup_cards`` take ``True`` (the
    lookup returns the expired card, as v0.20.0 did).
    """

    with _memory_store() as store:
        older = _card(store, "older", "topic:games")
        _card(store, "newer", "topic:games", valid_to=PAST)
        assert [row["id"] for row in _accepted_cards(store)] == [older]
        _expire(store, older)
        assert _accepted_cards(store) == []


# ---------------------------------------------------------------------------
# The Postgres statements, as text
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.queries.append((query, params))

    def fetchall(self) -> list[dict[str, object]]:
        return []

    def fetchone(self) -> dict[str, object]:
        return {"count": 0}


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def _postgres_statements() -> dict[str, tuple[str, tuple[object, ...]]]:
    """The statement and the parameters each Postgres read sends, keyed by what it is."""

    connection = _Connection()
    store = PostgresVNextStore(connection)  # type: ignore[arg-type]
    rollup_arguments = {
        "domains": None,
        "sensitivity_allowed": list(ALL_SENSITIVITY),
        "excluded_candidate_kind": ROLLUP_CANDIDATE_KIND,
    }
    calls = (
        ("list_default", lambda: store.list_memories(status="active", limit=3)),
        ("list_open", lambda: store.list_memories(status="active", limit=3, include_expired=False)),
        ("count_default", lambda: store.count_memories(status="active")),
        ("count_open", lambda: store.count_memories(status="active", include_expired=False)),
        ("rollup_list", lambda: store.list_rollup_input_memories(limit=3, **rollup_arguments)),
        ("rollup_count", lambda: store.count_rollup_input_memories(**rollup_arguments)),
        ("cards", lambda: _accepted_cards(store)),
        ("events", lambda: store.list_resume_memory_events(statuses=("active",), limit=3, domains=None, sensitivity_allowed=None)),
    )
    statements: dict[str, tuple[str, tuple[object, ...]]] = {}
    for name, call in calls:
        before = len(connection.cursor_instance.queries)
        call()
        assert len(connection.cursor_instance.queries) == before + 1, name
        statements[name] = connection.cursor_instance.queries[-1]
    return statements


def test_the_postgres_reads_hold_the_shared_unexpired_test_and_the_default_list_does_not() -> None:
    """Each Postgres read sends recall's test before ``LIMIT``; the management default sends none.

    The list and the count take it only when ``include_expired=False``. The roll-up reads,
    the accepted-card lookup and the resume events always take it, and the event join takes
    the alias form because ``event_log`` is joined to ``memories AS m``.

    Mutations, each one alone: drop ``{expiry_sql}`` from the list or the count (the open
    form lacks the text); make it unconditional (the default form has the text); drop
    ``AND {POSTGRES_UNEXPIRED_SQL}`` from the roll-up list, the roll-up count or the card
    lookup; drop ``{postgres_unexpired_sql("m.")}`` from ``list_resume_memory_events`` or use
    the unprefixed form there.
    """

    statements = _postgres_statements()
    for name in ("list_open", "count_open", "rollup_list", "rollup_count", "cards"):
        query, params = statements[name]
        assert POSTGRES_UNEXPIRED_SQL in query, name
        # no parameter is added: the test reads clock_timestamp(), as recall does
        assert query.count("%s") == len(params), name
    for name in ("list_default", "count_default"):
        assert POSTGRES_UNEXPIRED_SQL not in statements[name][0], name
        assert "clock_timestamp" not in statements[name][0], name
    list_query = statements["list_open"][0]
    assert list_query.index(POSTGRES_UNEXPIRED_SQL) < list_query.index("ORDER BY") < list_query.index("LIMIT")
    cards = statements["cards"][0]
    assert cards.index(POSTGRES_UNEXPIRED_SQL) < cards.index("ORDER BY")
    events, event_params = statements["events"]
    assert postgres_unexpired_sql("m.") in events
    assert events.index(postgres_unexpired_sql("m.")) < events.index("ORDER BY") < events.index("LIMIT")
    assert events.count("%s") == len(event_params)


# ---------------------------------------------------------------------------
# alice_recent_decisions, alice_resume, the brief and the hook, on a SQLite vault
# ---------------------------------------------------------------------------


def test_recent_decisions_leaves_out_an_expired_decision_and_unexpire_brings_it_back(tmp_path: Path) -> None:
    """The newest decision is closed with ``expire``: the tool lists the other two. ``unexpire`` lists it again.

    In v0.20.0 the tool listed all three.

    Mutation: delete ``include_expired=False`` from the ``list_memories`` call in
    ``_vnext_recent_decisions`` (the expired decision is listed).
    """

    vault = _Vault(tmp_path)
    vault.add("old", memory_type="decision")
    vault.add("middle", memory_type="decision")
    gone = vault.add("gone", memory_type="decision")
    assert _titles(vault.call("alice_recent_decisions")["decisions"]) == ["Title gone", "Title middle", "Title old"]

    vault.expire(gone)
    payload = vault.call("alice_recent_decisions")
    assert _titles(payload["decisions"]) == ["Title middle", "Title old"]
    assert payload["count"] == 2
    assert _marker("gone") not in json.dumps(payload)

    vault.unexpire(gone)
    assert _titles(vault.call("alice_recent_decisions")["decisions"]) == ["Title gone", "Title middle", "Title old"]


def test_resume_names_the_last_open_decision_and_the_events_of_open_memories(tmp_path: Path) -> None:
    """The newest decision is expired: ``last_decision`` is the one before it, and one recent change names it.

    ``max_recent_changes`` is 1 and the newest event is the expire event of the closed decision,
    so a join that does not leave it out fills the one place with an event that is then dropped:
    the brief shows no change at all.

    Mutations, each one alone: delete ``include_expired=False`` from the decision read in
    ``_vnext_resume`` (``last_decision`` is the expired decision); drop the expiry test from
    ``list_resume_memory_events`` in ``sqlite_store.py`` (``recent_changes`` is empty).
    """

    vault = _Vault(tmp_path)
    vault.add("old", memory_type="decision")
    middle = vault.add("middle", memory_type="decision")
    gone = vault.add("gone", memory_type="decision")
    vault.expire(gone)

    brief = vault.call("alice_resume", max_recent_changes=1)["brief"]
    assert brief["last_decision"]["id"] == middle  # type: ignore[index]
    assert [change["target_id"] for change in brief["recent_changes"]] == [middle]  # type: ignore[index]
    assert gone not in json.dumps(brief)


def test_resume_next_action_is_the_open_commitment_when_the_newest_one_is_expired(tmp_path: Path) -> None:
    """With no open loop, the next action is the newest commitment that has not expired.

    Mutation: delete ``include_expired=False`` from the ``todo_memories`` read in
    ``_vnext_resume`` (the next action is the expired commitment).
    """

    vault = _Vault(tmp_path)
    older = vault.add("older", memory_type="commitment")
    gone = vault.add("gone", memory_type="commitment")
    vault.expire(gone)

    brief = vault.call("alice_resume")["brief"]
    assert brief["open_loops"] == []  # type: ignore[index]
    assert brief["next_action"]["id"] == older  # type: ignore[index]


def test_the_session_brief_shows_every_place_to_open_memories_when_the_newest_is_expired(tmp_path: Path) -> None:
    """The brief lists ``FACT_LIMIT`` facts. An expired newest memory does not take one of the places.

    The vault holds ``FACT_LIMIT`` open memories and a newer expired one. The brief names all
    ``FACT_LIMIT`` open ones and not the expired one. A brief that only dropped the expired row
    after the limit would show one fact fewer. ``unexpire`` brings the memory back as a fact.

    Mutation: delete ``include_expired=False`` from the ``list_memories`` call in
    ``compile_session_brief`` (the brief shows ``FACT_LIMIT - 1`` open facts).
    """

    vault = _Vault(tmp_path)
    for index in range(FACT_LIMIT):
        vault.add(f"open-{index}")
    gone = vault.add("gone")
    vault.expire(gone)

    brief = vault.brief()
    assert brief.count("**fact**") == FACT_LIMIT
    assert _marker("gone") not in brief
    assert all(_marker(f"open-{index}") in brief for index in range(FACT_LIMIT))

    vault.unexpire(gone)
    assert _marker("gone") in vault.brief()


def test_the_three_decision_vault_of_the_changelog_entry_shows_what_the_entry_says(tmp_path: Path) -> None:
    """Three decisions, the newest closed with ``expire``: the numbers the changelog entry states.

    ``alice_recent_decisions`` lists 3 and then 2, ``alice_resume`` names the closed decision as ``last_decision`` and
    then the next one, and ``alice-memory brief`` has 3 fact lines and then 2.

    Mutations, each one alone: delete ``include_expired=False`` from the ``list_memories`` call of
    ``_vnext_recent_decisions``, from the decision read of ``_vnext_resume``, or from ``compile_session_brief``
    together with the ``memory_window_is_open`` test of ``_brief_omits_memory`` (the brief keeps 3 fact lines).
    """

    vault = _Vault(tmp_path)
    vault.add("old", memory_type="decision")
    middle = vault.add("middle", memory_type="decision")
    gone = vault.add("gone", memory_type="decision")
    assert vault.call("alice_recent_decisions")["count"] == 3
    assert vault.call("alice_resume")["brief"]["last_decision"]["id"] == gone  # type: ignore[index]
    assert vault.brief().count("**fact**") == 3

    vault.expire(gone)
    assert vault.call("alice_recent_decisions")["count"] == 2
    assert vault.call("alice_resume")["brief"]["last_decision"]["id"] == middle  # type: ignore[index]
    assert vault.brief().count("**fact**") == 2


def test_the_session_start_hook_injects_no_expired_memory(tmp_path: Path) -> None:
    """The hook Claude Code runs at session start prints the brief, and the brief leaves out an expired memory.

    The hook is run as the command the host runs, ``python -m`` on its module, in its own process with the plugin
    root unset and ``--data-dir`` pointing at a vault under ``tmp_path``, so it reads nothing outside it. It is a
    subprocess and not an import so that this file is not one of the tests that start the real-host CI job.

    Mutation: put the v0.20.0 brief back: delete ``include_expired=False`` from ``compile_session_brief`` and the
    ``memory_window_is_open`` test from ``_brief_omits_memory`` (the hook prints the expired text).
    """

    vault = _Vault(tmp_path)
    vault.add("open")
    gone = vault.add("gone")
    vault.expire(gone)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "CLAUDE_PLUGIN_ROOT" and not name.startswith("ALICE_")
    }
    environment["PYTHONPATH"] = str(Path(inspect.getsourcefile(session_briefing)).parents[1])  # type: ignore[arg-type]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alicebot_api.session_start_hook",
            "--data-dir",
            str(tmp_path),
            "--user-id",
            USER,
            "--format",
            "markdown",
        ],
        input="",
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert _marker("open") in completed.stdout
    assert _marker("gone") not in completed.stdout


class _StoreThatIgnoresTheExpiryFlag(SQLiteVNextStore):
    """A store whose ``list_memories`` ignores ``include_expired``, standing in for any store that cannot filter.

    It records what the caller asked, so the call site is pinned as well as the backstop.
    """

    asked: list[object]

    def list_memories(self, **kwargs):  # type: ignore[override]
        if not hasattr(self, "asked"):
            self.asked = []
        self.asked.append(kwargs.get("include_expired", "not passed"))
        kwargs["include_expired"] = True
        return super().list_memories(**kwargs)


def _brief_from(store: SQLiteVNextStore) -> str:
    return compile_session_brief(
        store,
        effective_domains=(),
        effective_sensitivity_allowed=ALL_SENSITIVITY,
        effective_project_scope=(),
        query=None,
        project_view=ProjectView.unscoped(),
        exclude_global_domains=frozenset(),
    )


def test_the_brief_asks_for_unexpired_memories_and_drops_an_expired_row_a_store_returns_anyway() -> None:
    """``compile_session_brief`` states ``include_expired=False``, and drops an expired row if the store returns one.

    The store ignores the flag, so the row comes back; the brief's last stage drops it.

    Mutations, each one alone: delete ``include_expired=False`` from the call (the recorded value is
    ``not passed``); delete the ``memory_window_is_open`` test from ``_brief_omits_memory`` (the
    expired text is in the brief).
    """

    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    user_id = str(uuid4())
    ensure_sqlite_user(connection, user_id, "expiry@example.com", "Expiry")
    store = _StoreThatIgnoresTheExpiryFlag(connection, user_id)
    _memory(store, "open")
    _memory(store, "gone", valid_to=PAST)
    brief = _brief_from(store)
    assert store.asked == [False]
    assert _marker("open") in brief and _marker("gone") not in brief
    connection.close()


def test_the_brief_drops_the_expired_memory_a_recent_change_event_points_at() -> None:
    """A recent-change event can put a memory back among the facts. An expired one is dropped there too.

    The store returns an event that names the expired memory, as it would if the join did not
    leave it out. The brief reads the memory by id and omits it.

    Mutation: delete the ``memory_window_is_open`` test from ``_brief_omits_memory`` (the expired
    text is a fact).
    """

    class StoreWithAStaleEvent(SQLiteVNextStore):
        gone_id = ""

        def list_resume_memory_events(self, **kwargs):  # type: ignore[override]
            return [
                {"id": "event-1", "target_type": "memory", "target_id": self.gone_id, "occurred_at": "2026-01-01"}
            ]

    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    user_id = str(uuid4())
    ensure_sqlite_user(connection, user_id, "expiry@example.com", "Expiry")
    store = StoreWithAStaleEvent(connection, user_id)
    _memory(store, "open")
    store.gone_id = _memory(store, "gone", valid_to=PAST)
    brief = _brief_from(store)
    assert _marker("open") in brief and _marker("gone") not in brief
    connection.close()


def test_a_resume_event_for_an_expired_memory_fails_the_fence_when_read_by_id() -> None:
    """The row behind a resume event is read by id, and an expired one does not pass the fence.

    Only a memory target is held to the validity test; an open-loop target has none.

    Mutation: delete the ``memory_window_is_open`` test from ``_resume_event_honours_policy_fence``
    in ``mcp/retrieval.py`` (the event of the expired memory is kept).
    """

    class Rows:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def get_memory(self, memory_id: str) -> dict[str, object]:
            return self.row

        def get_open_loop(self, loop_id: str) -> dict[str, object]:
            return self.row

    base = {"domain": "project", "sensitivity": "private"}
    event = {"target_type": "memory", "target_id": "m1"}

    def honoured(row: dict[str, object], target_type: str = "memory") -> bool:
        return mcp_retrieval._resume_event_honours_policy_fence(  # type: ignore[attr-defined]
            Rows(row),  # type: ignore[arg-type]
            {**event, "target_type": target_type},
            effective_domains=(),
            effective_sensitivity_allowed=ALL_SENSITIVITY,
            exclude_global_domains=frozenset(),
        )

    assert honoured({**base, "valid_to": None}) is True
    assert honoured({**base, "valid_to": FAR_FUTURE}) is True
    assert honoured({**base, "valid_to": PAST}) is False
    assert honoured({**base, "valid_to": PAST}, "open_loop") is True


def _calls_of(path: Path, function: str, method: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == method
            ]
    raise AssertionError(f"{function} not found in {path}")


def test_every_memory_list_in_the_three_doors_states_include_expired_false() -> None:
    """A new ``list_memories`` call in a door cannot forget the expiry test: it must say ``include_expired=False``.

    The management default is ``True``, so a call that leaves the keyword out would show an
    expired memory. This pins the keyword on every call in ``_vnext_resume`` (two),
    ``_vnext_recent_decisions`` (one) and ``compile_session_brief`` (one).

    Mutation: add a ``store.list_memories(...)`` call without the keyword to any of the three, or
    delete the keyword from one.
    """

    expected = {
        (SOURCE_ROOT / "mcp" / "retrieval.py", "_vnext_resume"): 2,
        (SOURCE_ROOT / "mcp" / "retrieval.py", "_vnext_recent_decisions"): 1,
        (SOURCE_ROOT / "session_briefing.py", "compile_session_brief"): 1,
    }
    for (path, function), count in expected.items():
        calls = _calls_of(path, function, "list_memories")
        assert len(calls) == count, (function, len(calls))
        for call in calls:
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            value = keywords.get("include_expired")
            assert isinstance(value, ast.Constant) and value.value is False, (function, call.lineno)


def test_the_brief_store_protocol_requires_include_expired_with_no_default() -> None:
    """The brief's store protocol makes ``include_expired`` a required keyword, so mypy fails a brief that omits it.

    Mutation: give the parameter a default (``include_expired: bool = True``) in ``SessionBriefStore``.
    """

    parameter = inspect.signature(session_briefing.SessionBriefStore.list_memories).parameters["include_expired"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Consolidation and the roll-up semantic tier, with a recording endpoint on 127.0.0.1
# ---------------------------------------------------------------------------


class _ArtifactShim:
    """The SQLite store plus the one artifact write consolidation needs, as the other tests build it."""

    def __init__(self, inner: SQLiteVNextStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def create_artifact(self, artifact: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        return {"id": str(uuid4()), **artifact}


LABELS = ("a", "b", "c", "d", "e", "f")


def _seed_embedded_then_expire(store: SQLiteVNextStore, expired: tuple[str, ...]) -> dict[str, str]:
    """Six memories with a vector each, made while the window was open; then ``expired`` are closed."""

    ids = {label: _memory(store, label, embed=True) for label in LABELS}
    for label in expired:
        _expire(store, ids[label])
    return ids


def _received(server: _RecordingEmbeddingsServer, after: int) -> list[str]:
    """The labels whose text reached the endpoint after the first ``after`` texts."""

    blob = "\n".join(server.texts[after:])
    return [label for label in LABELS if _marker(label) in blob]


def test_consolidation_sends_the_text_of_open_memories_only_and_counts_only_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six memories hold vectors and two are then expired: the endpoint gets four texts and the report counts four.

    The cap is four, with the two expired memories among the newest, so a list that did not leave
    them out in SQL would fill the cap with rows that are then dropped, and the report would count
    fewer than four. In v0.20.0 the endpoint got all six texts.

    Mutations, each one alone: make ``_unexpired_only_kwargs`` return nothing (the list is filled
    with expired rows and ``embedded_memories`` is below 4, and the count reads 6); drop
    ``count_expiry_kwargs`` from ``_count`` (``active_memories`` is 6).
    """

    with _memory_store() as store, _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        _seed_embedded_then_expire(store, ("e", "f"))
        seeded = len(server.texts)
        artifact = VNextConsolidationService(_ArtifactShim(store)).generate_memory_consolidation(  # type: ignore[arg-type]
            MemoryConsolidationRequest(metadata_json={"consolidation_options": {"max_embedded_memories": 4}})
        )
        assert _received(server, seeded) == ["a", "b", "c", "d"]
        counts = artifact["metadata_json"]["input_counts"]
        assert (counts["active_memories"], counts["embedded_memories"]) == (4, 4)
        assert counts["active_memories_exact"] is True


def test_consolidation_drops_an_expired_row_from_a_store_that_cannot_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store with no ``include_expired`` keyword returns the expired rows; consolidation still sends none.

    The adapter lists and counts every active memory, as a third-party store written before the
    keyword would.

    Mutation: delete the ``drop_expired_memories`` wrapper in ``_list_memories_bounded`` (the
    endpoint receives the text of both expired memories).
    """

    class OldAdapter(_ArtifactShim):
        def list_memories(self, *, status=None, domains=None, sensitivity_allowed=None, projects=None, limit=None):
            return self._inner.list_memories(
                status=status, domains=domains, sensitivity_allowed=sensitivity_allowed, limit=limit
            )

        def count_memories(self, *, status=None, domains=None, sensitivity_allowed=None, projects=None):
            return self._inner.count_memories(
                status=status, domains=domains, sensitivity_allowed=sensitivity_allowed
            )

    with _memory_store() as store, _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        _seed_embedded_then_expire(store, ("e", "f"))
        seeded = len(server.texts)
        adapter = OldAdapter(store)
        assert "include_expired" not in inspect.signature(adapter.list_memories).parameters
        VNextConsolidationService(adapter).generate_memory_consolidation(MemoryConsolidationRequest())  # type: ignore[arg-type]
        assert _received(server, seeded) == ["a", "b", "c", "d"]


def test_the_rollup_semantic_tier_sends_the_text_of_open_memories_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roll-up pass groups four open memories and sends their text; the two expired ones are not sent.

    The six memories share no word, so no lexical group claims them and every open row reaches the
    semantic tier. In v0.20.0 the endpoint got all six.

    Mutation: put the v0.20.0 read back, which is both edits together: drop ``{expiry_sql}`` from
    ``list_rollup_input_memories`` and delete the ``drop_expired_memories`` line in
    ``VNextRollupService._collect_rows`` (the texts of ``e`` and ``f`` go out and the groupable count is
    6). Either edit alone is caught by the store test and by the adapter test that follows.
    """

    with _memory_store() as store, _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        _seed_embedded_then_expire(store, ("e", "f"))
        seeded = len(server.texts)
        outcome = VNextRollupService(store).propose_rollups()
        assert _received(server, seeded) == ["a", "b", "c", "d"]
        assert outcome.semantic is not None
        assert (outcome.semantic["ungrouped_rows"], outcome.semantic["embedded_rows"]) == (4, 4)
        assert (outcome.groupable_count, outcome.groupable_total_count, outcome.groupable_total_exact) == (4, 4, True)


def test_the_rollup_pass_drops_an_expired_row_from_a_store_that_cannot_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store whose roll-up input read returns expired rows: the pass still sends none of their text.

    Mutation: delete the ``drop_expired_memories`` line in ``VNextRollupService._collect_rows`` (the
    endpoint receives the text of both expired memories).
    """

    class OldAdapter(_ArtifactShim):
        def list_rollup_input_memories(self, *, domains, sensitivity_allowed, excluded_candidate_kind, limit, projects=None):
            return self._inner.list_memories(
                statuses=("active", "accepted"),
                sensitivity_allowed=list(sensitivity_allowed),
                order_by_created_at=True,
                limit=limit,
            )

        def count_rollup_input_memories(self, *, domains, sensitivity_allowed, excluded_candidate_kind, projects=None):
            return len(
                self._inner.list_memories(
                    statuses=("active", "accepted"), sensitivity_allowed=list(sensitivity_allowed)
                )
            )

    with _memory_store() as store, _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        _seed_embedded_then_expire(store, ("e", "f"))
        seeded = len(server.texts)
        VNextRollupService(OldAdapter(store)).propose_rollups()  # type: ignore[arg-type]
        assert _received(server, seeded) == ["a", "b", "c", "d"]


def test_the_rollup_pass_does_not_count_an_expired_card_as_the_accepted_card() -> None:
    """``_existing_rollup_state`` returns no accepted card for a key whose card has expired.

    With the card open the pass sees it; once the card is expired it does not, so the topic can get a
    new card. An adapter that returns the expired card is filtered by the same test.

    Mutation: delete the ``memory_window_is_open`` test from the card loop in ``_existing_rollup_state`` (the
    adapter's expired card is kept). Dropping ``{expiry_sql}`` from the bundled store's query is not caught here,
    because that loop still drops the card; the store test above catches it.
    """

    class ReturnsEveryCard(_ArtifactShim):
        def list_accepted_rollup_cards(self, **kwargs):
            return self._inner.list_memories(statuses=("accepted",))

    with _memory_store() as store:
        card = _card(store, "card", "topic:games")
        for service_store in (store, ReturnsEveryCard(store)):
            _pending, accepted = VNextRollupService(service_store, embedding_provider=None)._existing_rollup_state(  # type: ignore[arg-type]
                rollup_digests=(),
                rollup_keys=("topic:games",),
                domains=None,
                sensitivity_allowed=list(ALL_SENSITIVITY),
                projects=(),
            )
            assert [row["id"] for row in accepted.values()] == [card]
        _expire(store, card)
        for service_store in (store, ReturnsEveryCard(store)):
            _pending, accepted = VNextRollupService(service_store, embedding_provider=None)._existing_rollup_state(  # type: ignore[arg-type]
                rollup_digests=(),
                rollup_keys=("topic:games",),
                domains=None,
                sensitivity_allowed=list(ALL_SENSITIVITY),
                projects=(),
            )
            assert accepted == {}


def test_an_expired_card_is_not_the_accepted_card_and_does_not_make_the_pass_raise() -> None:
    """A group whose card has expired is no longer covered by it, and the pass neither raises nor proposes it again as is.

    Four game memories make one topic group. The first pass proposes a card, the card is made active as an
    accepted one would be, and the second pass reports the group as already covered. After ``expire`` closes the
    card, the third pass reports the group as ``expired_card_members_unchanged`` and proposes nothing: a card for
    the same members would have the memory key the expired card holds. When a fifth memory joins the group, the
    members differ, so the fourth pass proposes a new card, and it is not a revision of the expired one. In v0.20.0
    the third and fourth passes reported the group as covered by the expired card.

    Mutations, each one alone: delete the ``_expired_card_for_digest`` check from ``propose_rollups`` (the third pass
    raises ``IntegrityError`` on the card's key); make ``_expired_card_for_digest`` return nothing for an expired card
    (same). Put the v0.20.0 lookup back, which is both edits of the accepted-card tests together: drop ``{expiry_sql}``
    from the ``ranked_rollups`` query and delete the ``memory_window_is_open`` test from the card loop in
    ``_existing_rollup_state`` (the third pass says ``already_covered_by_accepted``).
    """

    from tests.unit.test_vnext_rollups import _rollup_candidates, _seed_game_memories

    with _memory_store() as store:
        _seed_game_memories(store, order=(0, 1, 2, 3))
        service = VNextRollupService(store, embedding_provider=None)
        assert len(service.propose_rollups().proposals) == 1
        card = _rollup_candidates(store)[0]
        store.conn.execute("UPDATE memories SET status = 'active' WHERE id = ?", (card["id"],))
        second = service.propose_rollups()
        assert second.proposals == []
        assert [group["state"] for group in second.groups] == ["already_covered_by_accepted"]

        _expire(store, str(card["id"]))
        third = service.propose_rollups()
        assert third.proposals == []
        assert [group["state"] for group in third.groups] == ["expired_card_members_unchanged"]
        assert third.groups[0]["expired_memory_id"] == str(card["id"])

        _seed_game_memories(store, order=(4,))
        fourth = service.propose_rollups()
        assert [group["state"] for group in fourth.groups] == ["created"]
        assert len(fourth.proposals) == 1 and fourth.proposals[0]["revises_memory_id"] is None


def test_a_card_the_staleness_sweep_marked_stale_still_holds_its_unchanged_group_back() -> None:
    """After the sweep marks the expired card stale, the pass still reports its unchanged group and does not raise.

    The first pass proposes a card for four game memories, the card is made active as an accepted one would be, and
    ``expire`` closes it. The staleness sweep then runs for real over the store: it marks the card ``stale`` and keeps
    its ``valid_to``. The card is now in a status no accepted-card read returns, but it still holds the memory key of
    its digest, so a pass that proposed the unchanged group again would raise on that key, as it did in v0.20.0. The
    pass reports ``expired_card_members_unchanged`` and names the stale card. A fifth memory changes the members, so
    the next pass proposes a new card, and it is not a revision of the stale one.

    Mutations, each one alone: put the status test back into ``_expired_card_for_digest``
    (``str(row.get("status")) not in MEMORY_SEARCHABLE_STATUSES``), which makes the pass after the sweep raise
    ``IntegrityError`` on the card's key; delete the ``_expired_card_for_digest`` check from ``propose_rollups`` (same).
    """

    from tests.unit.test_vnext_rollups import _rollup_candidates, _seed_game_memories

    with _memory_store() as store:
        _seed_game_memories(store, order=(0, 1, 2, 3))
        service = VNextRollupService(store, embedding_provider=None)
        assert len(service.propose_rollups().proposals) == 1
        card = _rollup_candidates(store)[0]
        card_id = str(card["id"])
        store.conn.execute("UPDATE memories SET status = 'active' WHERE id = ?", (card_id,))
        VNextMemoryCommitService(store).expire(card_id, valid_to=PAST, reason="no longer true")

        report = VNextSchedulerService(_ArtifactShim(store))._run_staleness_sweep(  # type: ignore[arg-type]
            SchedulerRunRequest(workflow_type="staleness_sweep"), metadata={}
        )
        assert report["metadata_json"]["stale_marked_memory_ids"] == [card_id]
        swept = store.get_memory(card_id)
        assert swept is not None and swept["status"] == "stale" and swept["valid_to"] is not None

        after_sweep = service.propose_rollups()
        assert after_sweep.proposals == []
        assert [group["state"] for group in after_sweep.groups] == ["expired_card_members_unchanged"]
        assert after_sweep.groups[0]["expired_memory_id"] == card_id

        _seed_game_memories(store, order=(4,))
        changed = service.propose_rollups()
        assert [group["state"] for group in changed.groups] == ["created"]
        assert len(changed.proposals) == 1 and changed.proposals[0]["revises_memory_id"] is None


def test_the_pass_hands_its_own_fence_to_the_expired_card_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``propose_rollups`` gives ``_expired_card_for_digest`` the domains, sensitivity ceiling and projects it was called with.

    Four game memories sit in project ``alpha``, domain ``project`` and sensitivity ``internal``, and the pass runs
    under a fence that takes them in and is not the default. The first pass makes the card, ``expire`` closes it,
    and the next pass reaches the read. The read gets the pass's own fence and the group's roll-up key, and the real
    read names the card, so the fence does not shut out the very card the pass made.

    Mutations, each one alone, at the ``_expired_card_for_digest`` call in ``propose_rollups``: pass ``domains=None``,
    pass ``sensitivity_allowed=list(ALL_SENSITIVITY)``, pass ``projects=()``, pass a fixed ``rollup_key``.
    """

    from tests.unit.test_vnext_rollups import GAME_SPECS, _rollup_candidates

    fence = {"domains": ["project"], "sensitivity_allowed": ["internal", "private"], "projects": ("alpha",)}
    calls: list[dict[str, object]] = []
    original = VNextRollupService._expired_card_for_digest

    def spy(self: VNextRollupService, rollup_digest: str, **kwargs: object) -> dict[str, object] | None:
        calls.append(kwargs)
        return original(self, rollup_digest, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(VNextRollupService, "_expired_card_for_digest", spy)
    with _memory_store() as store:
        for index, (_key, text, session_date, _speaker) in enumerate(GAME_SPECS[:4]):
            store.create_memory(
                {
                    "memory_key": f"memory.alpha.{index}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "episode",
                    "title": text,
                    "canonical_text": text,
                    "summary": text,
                    "domain": "project",
                    "sensitivity": "internal",
                    "project_id": "alpha",
                    "metadata_json": {"session_date": session_date, "project_scope": ["alpha"]},
                }
            )
        service = VNextRollupService(store, embedding_provider=None)
        assert len(service.propose_rollups(**fence).proposals) == 1  # type: ignore[arg-type]
        card = _rollup_candidates(store)[0]
        store.conn.execute("UPDATE memories SET status = 'active' WHERE id = ?", (card["id"],))
        _expire(store, str(card["id"]))
        calls.clear()

        held = service.propose_rollups(**fence)  # type: ignore[arg-type]
        assert [group["state"] for group in held.groups] == ["expired_card_members_unchanged"]
        assert held.groups[0]["expired_memory_id"] == str(card["id"])
        assert calls == [{"rollup_key": held.groups[0]["rollup_key"], **fence}]


def _digest_card(
    store: SQLiteVNextStore,
    digest: str,
    *,
    rollup_key: str = "topic:games",
    status: str = "active",
    valid_to: str | None = PAST,
    domain: str = "project",
    sensitivity: str = "private",
    project: str | None = None,
    candidate_kind: str = ROLLUP_CANDIDATE_KIND,
) -> dict[str, object]:
    """One roll-up card holding the memory key a pass derives from ``digest``.

    A card with status ``archived`` is archived the way the store archives one, through ``update_memory``,
    which sets ``deleted_at`` and keeps ``memory_key``. Creating the row with ``status="archived"`` leaves
    ``deleted_at`` empty, which is not a soft-deleted row and does not reach the read that skips them.
    """

    metadata: dict[str, object] = {"candidate_kind": candidate_kind, "rollup_key": rollup_key, "rollup_digest": digest}
    if project is not None:
        metadata["project_scope"] = [project]
    card = store.create_memory(
        {
            "memory_key": f"vnext.rollup.{digest}",
            "value": {"text": digest},
            "memory_type": "semantic",
            "title": digest,
            "canonical_text": digest,
            "status": "active" if status == "archived" else status,
            "domain": domain,
            "sensitivity": sensitivity,
            "project_id": project,
            "valid_to": valid_to,
            "metadata_json": metadata,
        }
    )
    if status == "archived":
        store.update_memory(memory_id=str(card["id"]), patch={"status": "archived"})
        row = store.conn.execute(
            "SELECT status, deleted_at FROM memories WHERE id = ?", (str(card["id"]),)
        ).fetchone()
        assert row[0] == "archived" and row[1] is not None
        assert store.get_memory(str(card["id"])) is None
    return card


def _expired_card(service: VNextRollupService, digest: str, **fence: object) -> dict[str, object] | None:
    """What ``_expired_card_for_digest`` names for ``digest`` under a fence that lets everything through, with overrides."""

    arguments: dict[str, object] = {
        "rollup_key": "topic:games",
        "domains": None,
        "sensitivity_allowed": list(ALL_SENSITIVITY),
        "projects": (),
    }
    arguments.update(fence)
    return service._expired_card_for_digest(digest, **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["candidate", "active", "accepted", "stale", "rejected", "superseded", "archived"])
def test_a_card_whose_window_has_closed_is_held_back_in_every_status(status: str) -> None:
    """``_expired_card_for_digest`` names the card at the digest key once its window has closed, whatever its status.

    The row holds the memory key in every status, so a new card for the same members collides with it. The case
    that matters most is ``stale``: the staleness sweep marks an expired ``active`` card stale and leaves its
    ``valid_to``, and the next pass must still hold the group back.

    Mutation: put the status test back (``str(row.get("status")) not in MEMORY_SEARCHABLE_STATUSES``, returning
    nothing for a status other than ``active`` and ``accepted``), which fails the ``candidate``, ``stale``,
    ``rejected``, ``superseded`` and ``archived`` cases.
    """

    with _memory_store() as store:
        card = _digest_card(store, "digest-closed", status=status)
        named = _expired_card(VNextRollupService(store, embedding_provider=None), "digest-closed")
        assert named is not None and named["id"] == card["id"]


def test_a_card_that_is_open_or_absent_is_not_held_back() -> None:
    """``_expired_card_for_digest`` names nothing for a card with an open window, a key no row holds, or a store with no key lookup.

    Mutations, each one alone: drop the ``memory_window_is_open`` test (the open card is named); delete the
    ``callable(getter)`` test (the store with no key lookup raises); read the key from anything but
    ``vnext.rollup.{digest}`` (``vnext.rollup.{rollup_key}``: the closed card is not found).
    """

    class WithoutKeyLookup:
        pass

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        _digest_card(store, "digest-open", valid_to=FAR_FUTURE)
        _digest_card(store, "digest-closed")
        assert _expired_card(service, "digest-open") is None
        assert _expired_card(service, "digest-no-row") is None
        assert _expired_card(service, "digest-closed") is not None
        assert (
            _expired_card(VNextRollupService(WithoutKeyLookup(), embedding_provider=None), "digest-closed")  # type: ignore[arg-type]
            is None
        )


@pytest.mark.parametrize(
    ("card", "outside", "inside"),
    [
        pytest.param({"domain": "personal"}, {"domains": ["project"]}, {"domains": ["personal"]}, id="domain"),
        pytest.param(
            {"sensitivity": "private"},
            {"sensitivity_allowed": ["public", "internal"]},
            {"sensitivity_allowed": ["public", "internal", "private"]},
            id="sensitivity",
        ),
        pytest.param({"project": "alpha"}, {"projects": ("beta",)}, {"projects": ("alpha",)}, id="project"),
    ],
)
def test_a_closed_card_outside_the_fence_of_the_pass_is_not_named(
    card: dict[str, object], outside: dict[str, object], inside: dict[str, object]
) -> None:
    """The domain, the sensitivity ceiling and the projects of the pass each keep a closed card from being named.

    The accepted-card read takes all three, so this read does too. The card is a real closed roll-up card at the
    digest key. Outside the fence nothing is named, and a pass whose fence takes the card in names it. A card whose
    domain is ``unknown`` is inside any domain list, as in the accepted-card read.

    Mutations, each one alone, in the ``_scoped_rows`` call of ``_may_name_card`` (the controls
    ``_expired_card_for_digest`` applies): pass ``domains=None``
    (the ``domain`` case fails), pass ``sensitivity_allowed=list(ALL_SENSITIVITY)`` (the ``sensitivity`` case
    fails), pass ``projects=()`` (the ``project`` case fails). Deleting the ``_scoped_rows`` call fails all three.
    """

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        row = _digest_card(store, "digest-fenced", **card)  # type: ignore[arg-type]
        assert _expired_card(service, "digest-fenced", **outside) is None
        named = _expired_card(service, "digest-fenced", **inside)
        assert named is not None and named["id"] == row["id"]
        assert _expired_card(service, "digest-fenced") is not None
        unknown = _digest_card(store, "digest-unknown-domain", domain="unknown")
        named_unknown = _expired_card(service, "digest-unknown-domain", domains=["project"])
        assert named_unknown is not None and named_unknown["id"] == unknown["id"]


def test_a_row_at_the_digest_key_that_is_not_this_groups_rollup_card_is_not_named() -> None:
    """A closed row that is not a roll-up card, or is the card of another roll-up key, is not named.

    The accepted-card read asks for the roll-up candidate kind and the requested roll-up keys, so this read does too.

    Mutations, each one alone, in ``_may_name_card`` (the controls ``_expired_card_for_digest`` applies): delete the
    ``_is_rollup_card`` test (the row of another kind is named); delete the ``rollup_key`` comparison (the card of
    another key is named).
    """

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        _digest_card(store, "digest-other-kind", candidate_kind="consolidation_candidate")
        _digest_card(store, "digest-other-key", rollup_key="topic:cooking")
        _digest_card(store, "digest-this-key")
        assert _expired_card(service, "digest-other-kind") is None
        assert _expired_card(service, "digest-other-key") is None
        assert _expired_card(service, "digest-this-key") is not None


def test_every_control_of_the_expired_card_read_is_a_required_keyword_argument() -> None:
    """``_expired_card_for_digest`` takes the roll-up key, the domains, the sensitivity ceiling and the projects as required keywords.

    A caller cannot leave one out (and mypy rejects it), the way the brief's store protocol requires
    ``include_expired``.

    Mutation: give any of the four a default (``projects: tuple[str, ...] = ()``), or make one positional-or-keyword.
    """

    parameters = inspect.signature(VNextRollupService._expired_card_for_digest).parameters
    for name in ("rollup_key", "domains", "sensitivity_allowed", "projects"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is inspect.Parameter.empty, name


# ---------------------------------------------------------------------------
# Artifact promotion embeds the memory it makes
# ---------------------------------------------------------------------------


class _ArtifactStore(SQLiteVNextStore):
    """The SQLite store with in-memory artifacts, which SQLite has no table for.

    The memories, their events and their vectors are real SQLite rows, so the vector the test reads is
    the one the embedding door wrote.
    """

    artifacts: dict[str, dict[str, object]] = {}
    refuse_status_update = False

    def create_artifact(self, artifact: dict[str, object], **_kwargs: object) -> dict[str, object]:
        row = {"id": f"artifact-{len(self.artifacts) + 1}", **artifact}
        self.artifacts[str(row["id"])] = row
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def update_artifact_status(
        self, *, artifact_id: str, status: str, expected_status: str | None = None, **_kwargs: object
    ) -> dict[str, object] | None:
        if self.refuse_status_update:
            return None
        artifact = self.artifacts[artifact_id]
        artifact["status"] = status
        return artifact


def _artifact_vault(tmp_path: Path) -> tuple[Path, str]:
    """A vault on disk and the id of one reviewed artifact held by the store class."""

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER, user_email="local@alice")
    _ArtifactStore.artifacts = {}
    _ArtifactStore.refuse_status_update = False
    with sqlite_user_connection(db_path, USER) as connection:
        artifact = _ArtifactStore(connection, USER).create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Release findings",
                "content_markdown": f"# Release findings\n\n{_marker('promoted')}",
                "status": "reviewed",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {},
            }
        )
    return db_path, str(artifact["id"])


def test_promotion_queues_the_memory_for_embedding_and_the_caller_sends_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promoting a reviewed artifact queues the new active memory; embedding it sends its text once and stores a vector.

    The dispatcher returns the snapshot and sends nothing itself, the way the project-update review does, so the
    provider is not called inside the caller's transaction. Persisting the snapshot after the commit sends the text
    once. Promoting again names the same memory and sends nothing. In v0.20.0 the promoted memory had no vector.

    Mutations, each one alone: delete the ``_embed_or_defer_promoted_memory`` call from ``_promote_artifact`` (no
    snapshot, no text, no vector); make the dispatcher build the queue service with ``defer_embeddings=False`` (the
    text is sent inside the review and the snapshot list is empty).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        with sqlite_user_connection(db_path, USER) as connection:
            result = dispatch_vnext_artifact_review(
                _ArtifactStore(connection, USER), artifact_id=artifact_id, action="promote"
            )
        assert server.texts == []
        assert [snapshot.memory_id for snapshot in result.deferred_embedding_inputs] == [
            result.artifact["promoted_memory_id"]
        ]
        (snapshot,) = result.deferred_embedding_inputs
        assert snapshot.status == "active" and snapshot.valid_to is None
        assert _marker("promoted") in (snapshot.canonical_text or "")

        @contextmanager
        def store_context() -> Iterator[SQLiteVNextStore]:
            with sqlite_user_connection(db_path, USER) as connection:
                yield _ArtifactStore(connection, USER)

        stored = persist_deferred_memory_embeddings_best_effort(
            result.deferred_embedding_inputs, store_context=store_context
        )
        assert stored == 1
        assert sum(_marker("promoted") in text for text in server.texts) == 1 and len(server.texts) == 1
        assert _vectors_present(db_path)[snapshot.memory_id] is True

        with sqlite_user_connection(db_path, USER) as connection:
            again = dispatch_vnext_artifact_review(
                _ArtifactStore(connection, USER), artifact_id=artifact_id, action="promote"
            )
        assert again.artifact["promoted_memory_id"] == snapshot.memory_id
        assert again.deferred_embedding_inputs == ()
        assert len(server.texts) == 1


def test_the_review_command_embeds_the_promoted_memory_after_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``alicebot vnext artifacts review ID --action promote`` sends the promoted text once and stores its vector.

    The CLI runs against the SQLite store with in-memory artifacts, through the patch point the other CLI
    embedding tests use.

    Mutation: delete the ``_embed_or_defer_promoted_memory`` call from ``_promote_artifact`` (nothing is sent and
    the memory has no vector).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)

    @contextmanager
    def store_context(_ctx: object) -> Iterator[SQLiteVNextStore]:
        with sqlite_user_connection(db_path, USER) as connection:
            yield _ArtifactStore(connection, USER)

    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        monkeypatch.setattr(cli_module, "_vnext_store_context", store_context)
        monkeypatch.setattr(
            cli_module, "get_settings", lambda: Settings(database_url="postgresql://db", auth_user_id=USER)
        )
        code = cli_module.main(["vnext", "artifacts", "review", artifact_id, "--action", "promote"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "promoted_to_memory"
        assert sum(_marker("promoted") in text for text in server.texts) == 1 and len(server.texts) == 1
        assert _vectors_present(db_path)[payload["promoted_memory_id"]] is True


def test_the_mcp_review_tool_embeds_the_promoted_memory_after_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``alice_vnext_artifact_review`` with ``promote`` sends the promoted text once and stores its vector.

    The tool runs against the SQLite store with in-memory artifacts, through the store context of the MCP
    handler and the one the persist helper reopens after the commit. The dispatcher sends nothing while it holds
    the review transaction, so the text the endpoint receives comes from the handler's persist call.

    Mutations, each one alone: delete the ``_persist_vnext_deferred_embedding_inputs`` call from
    ``_handle_alice_vnext_artifact_review`` (nothing is sent, no vector); delete the
    ``deferred_embedding_inputs = result.deferred_embedding_inputs`` line there (the persist call gets nothing).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)

    @contextmanager
    def store_context(_context: object) -> Iterator[SQLiteVNextStore]:
        with sqlite_user_connection(db_path, USER) as connection:
            yield _ArtifactStore(connection, USER)

    monkeypatch.setattr(mcp_evidence_artifacts, "_vnext_store_context", store_context)
    monkeypatch.setattr(mcp_runtime, "_vnext_store_context", store_context)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db_path), user_id=USER)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        payload = call_mcp_tool(
            context,
            name="alice_vnext_artifact_review",
            arguments={"artifact_id": artifact_id, "action": "promote"},
        )
        assert payload["status"] == "promoted_to_memory"
        assert sum(_marker("promoted") in text for text in server.texts) == 1 and len(server.texts) == 1
        assert _vectors_present(db_path)[str(payload["promoted_memory_id"])] is True


def test_the_review_route_embeds_the_promoted_memory_after_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /v0/vnext/artifacts/{id}/review`` with ``promote`` sends the promoted text once and stores its vector.

    The route handler runs against the SQLite store with in-memory artifacts, standing in for the Postgres
    connection, and the persist helper reopens the same store after the commit. The text reaches the endpoint
    only through the route's persist call.

    Mutations, each one alone: delete the ``_persist_vnext_deferred_embeddings`` call from ``review_vnext_artifact``;
    turn its ``if review_result is not None and review_result.deferred_embedding_inputs:`` guard into ``if False:``
    (nothing is sent in either case, and no vector is stored).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)
    decision = PolicyDecision(decision="allowed", action="artifact.review", permission_profile="admin_agent", trace_id="t")

    @contextmanager
    def user_connection(_database_url: str, _user_id: object) -> Iterator[sqlite3.Connection]:
        with sqlite_user_connection(db_path, USER) as connection:
            yield connection

    @contextmanager
    def embedding_store_context(_database_url: str, _user_id: object) -> Iterator[SQLiteVNextStore]:
        with sqlite_user_connection(db_path, USER) as connection:
            yield _ArtifactStore(connection, USER)

    monkeypatch.setattr(vnext_review_router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(vnext_review_router, "user_connection", user_connection)
    monkeypatch.setattr(vnext_review_router, "PostgresVNextStore", lambda conn: _ArtifactStore(conn, USER))
    monkeypatch.setattr(vnext_review_router, "_vnext_authenticated_agent_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        vnext_review_router, "_vnext_authorized_artifact", lambda **_kwargs: ({"id": artifact_id}, decision)
    )
    monkeypatch.setattr(route_embeddings, "_vnext_embedding_store_context", embedding_store_context)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        response = vnext_review_router.review_vnext_artifact(
            artifact_id,  # type: ignore[arg-type]
            vnext_review_router.VNextArtifactReviewRequest(user_id=UUID(USER), action="promote"),
        )
        payload = json.loads(response.body)
        assert response.status_code == 200 and payload["status"] == "promoted_to_memory"
        assert sum(_marker("promoted") in text for text in server.texts) == 1 and len(server.texts) == 1
        assert _vectors_present(db_path)[str(payload["promoted_memory_id"])] is True


def test_the_queue_service_embeds_in_place_when_nothing_defers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Used directly, ``VNextQueueService`` embeds the promoted memory itself, once.

    Mutation: delete the non-deferred ``attach_memory_embedding`` call from ``_embed_or_defer_promoted_memory``
    (nothing is sent).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        with sqlite_user_connection(db_path, USER) as connection:
            promoted = VNextQueueService(_ArtifactStore(connection, USER)).review_artifact(
                artifact_id=artifact_id, action="promote"
            )
        assert sum(_marker("promoted") in text for text in server.texts) == 1 and len(server.texts) == 1
        assert _vectors_present(db_path)[str(promoted["promoted_memory_id"])] is True


@pytest.mark.parametrize("deferred", [False, True])
def test_a_promotion_that_conflicts_sends_and_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deferred: bool
) -> None:
    """When another reviewer wins the artifact, the promotion raises and its memory is never embedded.

    The embedding comes after every write, so a promotion that raises on the status update has sent no text and
    queued no snapshot.

    Mutation: move the ``_embed_or_defer_promoted_memory`` call above the ``update_artifact_status`` call (the
    text is sent, or the snapshot is queued, before the conflict).
    """

    db_path, artifact_id = _artifact_vault(tmp_path)
    _ArtifactStore.refuse_status_update = True
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        with sqlite_user_connection(db_path, USER) as connection:
            service = VNextQueueService(_ArtifactStore(connection, USER), defer_embeddings=deferred)
            with pytest.raises(VNextQueueValidationError, match="conflicted"):
                service.review_artifact(artifact_id=artifact_id, action="promote")
            assert service.deferred_embedding_inputs == ()
        assert server.texts == []


def test_a_promoted_memory_that_is_not_recall_visible_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion goes through the one embedding door, which withholds a memory recall cannot return.

    The store made the memory with a ``valid_to`` in the past, as a store that stamps one would. The door
    withholds it, so no text is sent and no vector is stored.

    Mutation: send the text from ``_embed_or_defer_promoted_memory`` without ``attach_memory_embedding`` (a
    direct provider call), which bypasses the door's ``is_embeddable`` check.
    """

    class StoreThatStampsAnEnd(_ArtifactStore):
        def create_memory(self, memory, **kwargs):  # type: ignore[override]
            return super().create_memory({**memory, "valid_to": PAST}, **kwargs)

    db_path, artifact_id = _artifact_vault(tmp_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        with sqlite_user_connection(db_path, USER) as connection:
            promoted = VNextQueueService(StoreThatStampsAnEnd(connection, USER)).review_artifact(
                artifact_id=artifact_id, action="promote"
            )
        assert server.texts == []
        assert _vectors_present(db_path)[str(promoted["promoted_memory_id"])] is False


def test_the_promotion_module_names_the_door_it_sends_through() -> None:
    """``vnext_queue`` reaches the provider only through the embedding door, never by a direct call.

    Mutation: add ``embed_batch`` or ``embed_text`` to ``vnext_queue.py``.
    """

    source = inspect.getsource(vnext_queue)
    assert "embed_batch(" not in source and "embed_text(" not in source
    assert "attach_memory_embedding" in source and "DeferredMemoryEmbedding" in source
