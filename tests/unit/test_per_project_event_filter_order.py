"""Per-project memory S2: a recent-change read applies the caller's filters before it cuts or fills (spec 6.2).

Spec 6.2 says each fill query runs under the full fence. The event reads of ``alice_resume`` and the recent-change
merge of the session brief carry no domain or sensitivity argument, so those rules ran after the cut: a run of newer
events the caller may not see took the places (the SQL limit, then the fill's places) and was then dropped, and every
older event the caller may see went with it. Both event readers now take ``domains`` and ``sensitivity_allowed`` as
required keyword arguments, and the project view passes the request's own.

Every vault below creates its rows oldest first (each create writes one event, so creation order is recency), and in
the hand-written vaults the rows the caller may not see are the newer ones. The expected rows are written from the
vault, not from the reader. Each test names the edit that makes it fail.
"""

from __future__ import annotations

import inspect
import random
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.retrieval import _event_recency, _resume_event_honours_policy_fence
from alicebot_api.mcp.retrieval_shared import _CONTEXT_MEMORY_STATUSES, _SQLITE_OPEN_LOOP_ACTIVE_STATUSES
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import ProjectView, fetch_in_two_queries, fill_counts, project_first_fill
from alicebot_api.session_briefing import SessionBriefStore, sensitive_global_exclusion
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for, repo_with_remote
from tests.unit.per_project_view_support import PROJECT_A, compile_view_brief, project_view

USER_ID = "00000000-0000-0000-0000-000000000001"

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


# ---------------------------------------------------------------------------------------------------------------
# Vaults
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One memory or open loop. ``side`` is ``P`` for this project and ``G`` for a note with no project."""

    side: str
    kind: str  # "memory" or "loop"
    label: str
    domain: str = "project"
    sensitivity: str = "public"


def _rows(
    side: str, kind: str, prefix: str, count: int, *, domain: str = "project", sensitivity: str = "public"
) -> list[Row]:
    """``count`` rows named ``P loop 0``, ``P loop 1``, ... (``P mem x0`` with the prefix ``x``)."""

    word = "mem" if kind == "memory" else "loop"
    return [Row(side, kind, f"{side} {word} {prefix}{index}", domain, sensitivity) for index in range(count)]


def _project_id(repo: Path) -> str:
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    return detection.context.ids[0]


def _vault(tmp_path: Path, repo: Path, rows: list[Row]) -> Path:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_id(repo)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for row in rows:
            scope = (project_id,) if row.side == "P" else None
            if row.kind == "memory":
                add_memory(
                    store,
                    key=f"key.{row.label}",
                    text=row.label,
                    domain=row.domain,
                    sensitivity=row.sensitivity,
                    scope=scope,
                )
            else:
                add_loop(store, title=row.label, domain=row.domain, sensitivity=row.sensitivity, scope=scope)
    return data_dir


def _resume(data_dir: Path, repo: Path, **arguments: object) -> list[str]:
    """What each ``recent_changes`` row points at, newest first, read by id the way a client would."""

    context = MCPRuntimeContext(
        database_url=sqlite_url_for_path(db_path_for(data_dir)),
        user_id=USER_ID,
        project_dir=str(repo),
    )
    brief = call_mcp_tool(context, name="alice_resume", arguments=dict(arguments))["brief"]
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        labels: list[str] = []
        for event in brief["recent_changes"]:
            if event["target_type"] == "memory":
                row = store.get_memory(str(event["target_id"]))
                assert row is not None
                labels.append(str(row["canonical_text"]))
            else:
                loop = store.get_open_loop(str(event["target_id"]))
                assert loop is not None
                labels.append(str(loop["title"]))
        return labels


def _newest_first(labels: list[str]) -> list[str]:
    return list(reversed(labels))


def _names(rows: list[Row]) -> list[str]:
    return [row.label for row in rows]


# ---------------------------------------------------------------------------------------------------------------
# The reviewer's case and its variants, each with the answer written from the vault
# ---------------------------------------------------------------------------------------------------------------

#: Four older loop events the caller may see and four newer memory events it may not, then the mirror (loops newer).
#: ``hidden`` says how the newer rows differ: by sensitivity, by domain, or only by the default ceiling.
_PUBLIC_ONLY = {"sensitivity_allowed": ["public"]}
_PROJECT_ONLY = {"domains": ["project"]}


def _flood_vault(
    flood_kind: str, hidden: str, *, limit: int, flood_side: str = "P"
) -> tuple[list[Row], list[Row], dict[str, object]]:
    """The project's ``limit`` older rows of the other kind, the newer flood, and the request that hides the flood."""

    seen_kind = "loop" if flood_kind == "memory" else "memory"
    seen = _rows("P", seen_kind, "", limit)
    if hidden == "sensitivity":
        flood = _rows(flood_side, flood_kind, "x", limit, sensitivity="private")
        arguments: dict[str, object] = {**_PUBLIC_ONLY}
    elif hidden == "domain":
        flood = _rows(flood_side, flood_kind, "x", limit, domain="personal")
        arguments = {**_PROJECT_ONLY}
    else:
        # No narrowing argument at all: the default ceiling refuses confidential notes.
        flood = _rows(flood_side, flood_kind, "x", limit, sensitivity="confidential")
        arguments = {}
    arguments["max_recent_changes"] = limit
    return seen, flood, arguments


@pytest.mark.parametrize("hidden", ["sensitivity", "domain", "default-ceiling"])
@pytest.mark.parametrize("flood_kind", ["memory", "loop"])
@pytest.mark.parametrize("limit", [4, 8])
def test_newer_events_the_caller_may_not_see_do_not_push_out_older_ones_it_may(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flood_kind: str, hidden: str, limit: int
) -> None:
    """Mutation: pass ``sensitivity_allowed=None`` (the sensitivity cases and the default ceiling) or ``domains=None``
    (the domain cases) in the memory event read in ``read_events`` of ``_vnext_resume`` (the cases whose newer rows
    are memory events), or in the open loop event read (the cases whose newer rows are loop events). Leaving an
    argument out raises, because both are required.

    The project holds ``limit`` events of one kind the caller may see, created first, and ``limit`` newer events of the
    other kind that it may not (private under a public-only request, in another domain under a ``domains`` request,
    or confidential under the default ceiling). Spec 6.2 gives ``limit`` events, the older ones. The read used to cut
    both kinds to ``limit`` rows before it fenced them: the newer rows filled every place, the fence dropped them, and
    the result held none. The control is the same vault under scoping off, which reads each kind to its own limit and
    returns ``limit`` events. The second control asks as a caller who may see the newer rows (no narrowing argument,
    and the newer rows are not confidential), so the fixture really holds the newer events.
    """

    repo = repo_with_remote(tmp_path / "repo")
    seen, flood, arguments = _flood_vault(flood_kind, hidden, limit=limit)
    data_dir = _vault(tmp_path, repo, [*seen, *flood])

    assert len(_resume(data_dir, repo, **arguments)) == limit  # scoping off: the control
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, **arguments) == _newest_first(_names(seen))
    if hidden != "default-ceiling":
        # A caller who may see the newer rows gets them, so the fixture holds them.
        assert _resume(data_dir, repo, max_recent_changes=limit) == _newest_first(_names(flood))


def test_the_reviewers_case_through_alice_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: pass ``sensitivity_allowed=None`` in the memory event read in ``read_events``.

    The finding as written: four newer private memory events and four older public loop events, a public-only request
    at four places. The loops come back, newest first, and no memory does.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [*_rows("P", "loop", "", 4), *_rows("P", "memory", "x", 4, sensitivity="private")]
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, max_recent_changes=4, sensitivity_allowed=["public"]) == [
        "P loop 3",
        "P loop 2",
        "P loop 1",
        "P loop 0",
    ]


def test_a_partial_flood_still_returns_every_older_permitted_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: pass ``sensitivity_allowed=None`` in the memory event read in ``read_events``.

    Four older public loop events and only two newer private memory events, four places. The two private ones used
    to take two places, and the result held two events where four are permitted.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [*_rows("P", "loop", "", 4), *_rows("P", "memory", "x", 2, sensitivity="private")]
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, max_recent_changes=4, sensitivity_allowed=["public"]) == [
        "P loop 3",
        "P loop 2",
        "P loop 1",
        "P loop 0",
    ]


@pytest.mark.parametrize("hidden", ["sensitivity", "domain", "default-ceiling"])
@pytest.mark.parametrize("flood_kind", ["memory", "loop"])
def test_global_events_the_caller_may_not_see_do_not_spend_the_reserved_global_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flood_kind: str, hidden: str
) -> None:
    """Mutation: the same edits as the first test. The two reads of ``read_events`` serve both sides of the fill, and
    the case here is the global side.

    The project holds four events the caller may see. Global holds four newer events it may not. Four places reserve
    one for global (``4 // 4``), and the global side is read with the same arguments, so it holds nothing the caller
    may see and the project keeps all four places. The read used to hand the fill the four hidden global events, the
    fill gave one of them the reserved place, the fence dropped it, and three events came back where four are
    permitted.
    """

    repo = repo_with_remote(tmp_path / "repo")
    seen, flood, arguments = _flood_vault(flood_kind, hidden, limit=4, flood_side="G")
    data_dir = _vault(tmp_path, repo, [*seen, *flood])
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert fill_counts(limit=4, project_available=4, global_available=0) == (4, 0)
    assert _resume(data_dir, repo, **arguments) == _newest_first(_names(seen))


def test_the_reserved_global_place_goes_to_the_global_event_the_caller_may_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: pass ``sensitivity_allowed=None`` in the memory event read in ``read_events``.

    The project holds four public loop events. Global holds one public memory event, older than four private memory
    events that are the newest of the vault. A public-only request at four places has four project rows and one
    global row to give, and ``fill_counts`` makes it three and one: the global event is the newest of the three it
    keeps, then the project's three newest. The read used to return the four private events as the global side, the
    fill took one of them, the fence dropped it, and the public global event never got its place.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [
        *_rows("P", "loop", "", 4),
        *_rows("G", "memory", "", 1),
        *_rows("G", "memory", "x", 4, sensitivity="private"),
    ]
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert fill_counts(limit=4, project_available=4, global_available=1) == (3, 1)
    assert _resume(data_dir, repo, max_recent_changes=4, sensitivity_allowed=["public"]) == [
        "G mem 0",
        "P loop 3",
        "P loop 2",
        "P loop 1",
    ]


def test_an_unknown_domain_event_stays_visible_under_a_domains_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: change ``_domain_clause`` in ``query_predicates.py`` so that a domain of ``unknown`` is no longer
    admitted (drop ``OR {prefix}domain = 'unknown'``).

    ``list_memories`` and the fence after the join both admit a row whose domain is ``unknown`` when a ``domains``
    request names others, so the event of such a row has to come back from the event reads too. Rows, oldest first:
    a project loop, a personal memory (left out), an unknown-domain memory and an unknown-domain loop.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [
        Row("P", "loop", "P loop 0"),
        Row("P", "memory", "P mem personal", domain="personal"),
        Row("P", "memory", "P mem unknown", domain="unknown"),
        Row("P", "loop", "P loop unknown", domain="unknown"),
    ]
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, max_recent_changes=8, domains=["project"]) == [
        "P loop unknown",
        "P mem unknown",
        "P loop 0",
    ]


def test_an_empty_request_for_domains_filters_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: pass ``domains=effective_domains`` (the tuple, or its list) instead of ``domain_filter`` in either
    event read of ``read_events``, so that an empty request becomes ``domain IN ()``.

    A request with no ``domains`` filters nothing, as in every other read of ``_vnext_resume`` (an empty tuple is
    ``None`` in ``domain_filter``). With the empty list passed through, every event of every non-``unknown`` domain
    would be left out.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [*_rows("P", "loop", "", 2), *_rows("P", "memory", "", 2, domain="personal")]
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, max_recent_changes=8) == ["P mem 1", "P mem 0", "P loop 1", "P loop 0"]


# ---------------------------------------------------------------------------------------------------------------
# Many vaults against an oracle that does not use the reader's code
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_resume_matches_the_rule_on_random_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    """Mutation: any of the four edits of the first test (``None`` for ``sensitivity_allowed`` or ``domains`` in the
    memory or the loop event read of ``read_events``).

    Each seed builds a small vault: a random run of memories and loops on two sides (this project and global), each
    one either visible to the request or hidden by sensitivity, by domain or by the default ceiling, and a random
    limit from 1 to 8. The expected list is ``fill_counts(limit, visible project rows, visible global rows)``: the
    newest that many of each side, newest first. A hand-written case pins a few shapes. Every limit and mix goes
    through here, and with any one read unfenced 7 to 24 of the 60 seeds fail (24 and 18 for the two sensitivity
    edits, 7 and 9 for the two domain edits).
    """

    generator = random.Random(seed)
    hidden = generator.choice(["sensitivity", "domain", "default-ceiling"])
    arguments: dict[str, object] = {}
    hidden_fields: dict[str, str]
    if hidden == "sensitivity":
        arguments = {**_PUBLIC_ONLY}
        hidden_fields = {"sensitivity": "private"}
    elif hidden == "domain":
        arguments = {**_PROJECT_ONLY}
        hidden_fields = {"domain": "personal"}
    else:
        hidden_fields = {"sensitivity": "confidential"}
    limit = generator.randint(1, 8)
    arguments["max_recent_changes"] = limit

    rows: list[Row] = []
    for index in range(generator.randint(6, 14)):
        side = generator.choice("PG")
        kind = generator.choice(["memory", "loop"])
        is_hidden = generator.random() < 0.5
        rows.append(
            Row(
                side,
                kind,
                f"{side} {kind} {index}{' hidden' if is_hidden else ''}",
                **(hidden_fields if is_hidden else {}),
            )
        )
    visible = [row for row in rows if not row.label.endswith(" hidden")]
    project_rows = [row for row in visible if row.side == "P"]
    global_rows = [row for row in visible if row.side == "G"]
    project_take, global_take = fill_counts(
        limit=limit, project_available=len(project_rows), global_available=len(global_rows)
    )
    kept = [*reversed(project_rows)][:project_take] + [*reversed(global_rows)][:global_take]
    index_of = {row.label: position for position, row in enumerate(rows)}
    expected = [row.label for row in sorted(kept, key=lambda row: index_of[row.label], reverse=True)]

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, rows)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert _resume(data_dir, repo, **arguments) == expected


# ---------------------------------------------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------------------------------------------


def _event_labels(store: SQLiteVNextStore, events: list[dict[str, object]]) -> list[str]:
    labels: list[str] = []
    for event in events:
        if event["target_type"] == "memory":
            row = store.get_memory(str(event["target_id"]))
            assert row is not None
            labels.append(str(row["canonical_text"]))
        else:
            loop = store.get_open_loop(str(event["target_id"]))
            assert loop is not None
            labels.append(str(loop["title"]))
    return labels


def test_the_event_readers_filter_before_the_limit(tmp_path: Path) -> None:
    """Mutation: drop ``+ domain_sql`` or ``+ sensitivity_sql`` from ``scoped_where_sql`` in
    ``list_resume_memory_events`` (``sqlite_store.py``) or in ``list_open_loop_events`` (``graph_open_loops.py``), or
    cut after the query instead (filter the returned rows in Python).

    Rows, oldest first, for each reader: ``A`` (project domain, public), ``B`` (personal domain, public) and ``C``
    (project domain, private). With ``limit=1`` and a public-only request the answer is ``B``, the newest row that
    qualifies, where a limit applied first would take ``C`` and leave nothing. A ``domains`` request for ``project``
    returns ``C`` and ``A`` and not ``B``. With neither argument (``None`` for both) all three come back, so the
    rows are all there.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for label, domain, sensitivity in (
            ("A", "project", "public"),
            ("B", "personal", "public"),
            ("C", "project", "private"),
        ):
            add_memory(store, key=f"key.{label}", text=f"mem {label}", domain=domain, sensitivity=sensitivity)
            add_loop(store, title=f"loop {label}", domain=domain, sensitivity=sensitivity)

        def memory_events(**kwargs: object) -> list[str]:
            return _event_labels(
                store,
                store.list_resume_memory_events(statuses=("active",), **kwargs),  # type: ignore[arg-type]
            )

        def loop_events(**kwargs: object) -> list[str]:
            return _event_labels(
                store,
                store.list_open_loop_events(statuses=("open",), **kwargs),  # type: ignore[arg-type]
            )

        for name, read, prefix in (("memory", memory_events, "mem"), ("loop", loop_events, "loop")):
            everything = read(limit=10, domains=None, sensitivity_allowed=None)
            assert everything == [f"{prefix} C", f"{prefix} B", f"{prefix} A"], name
            assert read(limit=1, domains=None, sensitivity_allowed=["public"]) == [f"{prefix} B"], name
            assert read(limit=10, domains=None, sensitivity_allowed=["public"]) == [f"{prefix} B", f"{prefix} A"], name
            assert read(limit=10, domains=["project"], sensitivity_allowed=None) == [f"{prefix} C", f"{prefix} A"], name
            assert read(limit=1, domains=["project"], sensitivity_allowed=["public"]) == [f"{prefix} A"], name


def test_an_empty_sensitivity_list_admits_nothing_and_builds_no_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: delete the early ``return []`` for an empty ``sensitivity_allowed`` in ``list_resume_memory_events``
    or in ``list_open_loop_events`` (the result stays empty, because ``IN ()`` matches nothing, so the test counts
    queries), or let the empty list mean no ceiling (``sensitivity_allowed or None`` in the clause call).

    An empty ceiling is deny-all. The reader answers it without a query, so no ``sensitivity IN ()`` is ever built.
    The control, a ceiling that names ``public``, runs one query for each reader and finds the event.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    queries: list[str] = []
    original = SQLiteVNextStore._fetch_all

    def counting_fetch_all(self: SQLiteVNextStore, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        queries.append(query)
        return original(self, query, params)

    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="key.one", text="mem one")
        add_loop(store, title="loop one")
        monkeypatch.setattr(SQLiteVNextStore, "_fetch_all", counting_fetch_all)
        assert store.list_resume_memory_events(statuses=("active",), domains=None, sensitivity_allowed=[]) == []
        assert store.list_open_loop_events(statuses=("open",), domains=None, sensitivity_allowed=[]) == []
        assert queries == []
        assert len(store.list_resume_memory_events(statuses=("active",), domains=None, sensitivity_allowed=["public"])) == 1
        assert len(store.list_open_loop_events(statuses=("open",), domains=None, sensitivity_allowed=["public"])) == 1
        assert len(queries) == 2


def test_the_fence_arguments_of_the_event_readers_have_no_defaults() -> None:
    """Mutation: give ``domains`` or ``sensitivity_allowed`` a default (``= None``) in any of these six signatures
    (the two SQLite readers, the two Postgres readers, the two methods of ``SessionBriefStore``).

    The fence rule: the control is a required argument, so that a caller that forgets it fails at the call and not
    after a cut. ``None`` is a statement ("no filter"), not an omission.
    """

    readers = (
        SQLiteVNextStore.list_resume_memory_events,
        SQLiteVNextStore.list_open_loop_events,
        PostgresVNextStore.list_resume_memory_events,
        PostgresVNextStore.list_open_loop_events,
        SessionBriefStore.list_resume_memory_events,
        SessionBriefStore.list_open_loop_events,
    )
    for reader in readers:
        parameters = inspect.signature(reader).parameters
        for name in ("domains", "sensitivity_allowed"):
            assert name in parameters, (reader, name)
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (reader, name)
            assert parameters[name].default is inspect.Parameter.empty, (reader, name)


def test_the_postgres_event_readers_refuse_a_fence_they_cannot_apply() -> None:
    """Mutation: delete the ``raise ValueError`` for a non-``None`` fence from ``list_resume_memory_events`` in
    ``vnext_store.py`` or from ``list_open_loop_events`` in the Postgres ``graph_open_loops.py``.

    The Postgres runtime resolves no project view, so its readers take the two arguments (the shared unscoped branch
    passes ``None`` for both) and refuse anything else, instead of taking a fence and leaving it out of the query. The
    refusal comes before the connection is touched, so a store with no connection shows it.
    """

    store = PostgresVNextStore(None)  # type: ignore[arg-type]
    for fence in ({"domains": ["project"], "sensitivity_allowed": None}, {"domains": None, "sensitivity_allowed": ["public"]}):
        with pytest.raises(ValueError, match="no domain or sensitivity fence"):
            store.list_resume_memory_events(statuses=("active",), **fence)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="no domain or sensitivity fence"):
            store.list_open_loop_events(statuses=("open",), **fence)  # type: ignore[arg-type]


def test_the_unscoped_resume_reads_state_no_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: pass ``domains=domain_filter`` or ``sensitivity_allowed=sensitivity_filter`` in either event read of
    the unscoped branch of ``_vnext_resume`` (the ``else`` of ``if project_view.mode == "project"``).

    Scoping off is v0.20.0: each event kind is read to ``max_recent_changes`` rows and the fence runs on what came
    back. Its result does not change with these two arguments on the vaults the parity tests use, so this test watches
    the calls: with scoping off both readers get ``None`` for both, and with scoping on every call carries the
    request's own lists.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [*_rows("P", "loop", "", 2), *_rows("P", "memory", "", 2)]
    data_dir = _vault(tmp_path, repo, rows)
    recorded: list[tuple[str, dict[str, object]]] = []
    for name in ("list_resume_memory_events", "list_open_loop_events"):
        original = getattr(SQLiteVNextStore, name)

        def spy(self: SQLiteVNextStore, _name: str = name, _original: object = original, **kwargs: object) -> object:
            recorded.append((_name, dict(kwargs)))
            return _original(self, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(SQLiteVNextStore, name, spy)

    arguments: dict[str, object] = {"max_recent_changes": 3, "domains": ["project"], "sensitivity_allowed": ["public"]}
    _resume(data_dir, repo, **arguments)
    assert {name for name, _ in recorded} == {"list_resume_memory_events", "list_open_loop_events"}
    for name, kwargs in recorded:
        assert kwargs["domains"] is None and kwargs["sensitivity_allowed"] is None, (name, kwargs)

    recorded.clear()
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    _resume(data_dir, repo, **arguments)
    assert len(recorded) == 4  # two readers, once for the project and once for global
    for name, kwargs in recorded:
        assert kwargs["domains"] == ["project"], (name, kwargs)
        assert kwargs["sensitivity_allowed"] == ["public"], (name, kwargs)


@pytest.mark.parametrize("kind", ["memory", "loop"])
def test_with_scoping_off_a_same_kind_flood_still_hides_older_events_as_in_v0200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Mutation: pass ``sensitivity_allowed=sensitivity_filter`` (or ``domains=domain_filter`` with a ``domains``
    request) in the memory event read (the memory case) or the open loop event read (the loop case) of the unscoped
    branch of ``_vnext_resume``.

    This pins what the change leaves alone. Scoping off is v0.20.0, and v0.20.0 reads each event kind to
    ``max_recent_changes`` rows and fences them afterwards. Four older public events and four newer private events of
    one kind, a public-only request at four places: the four newest events of that kind are the private ones, the
    fence drops them, and the result is empty. With scoping on the same request returns the four public events. The
    CHANGELOG says so ("as it did in v0.20.0"), and this test is that sentence: changing the unscoped read changes
    the release.
    """

    repo = repo_with_remote(tmp_path / "repo")
    rows = [*_rows("P", kind, "", 4), *_rows("P", kind, "x", 4, sensitivity="private")]
    data_dir = _vault(tmp_path, repo, rows)
    request = {"max_recent_changes": 4, "sensitivity_allowed": ["public"]}
    assert _resume(data_dir, repo, **request) == []
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    word = "mem" if kind == "memory" else "loop"
    assert _resume(data_dir, repo, **request) == [f"P {word} 3", f"P {word} 2", f"P {word} 1", f"P {word} 0"]


# ---------------------------------------------------------------------------------------------------------------
# The brief's recent-change merge, in the project view
# ---------------------------------------------------------------------------------------------------------------


def _brief_vault(tmp_path: Path, *, kind: str, flood: int, **flood_fields: str) -> Path:
    """Eight newer rows, one old row touched since, then ``flood`` newer rows the caller may not see.

    The brief's fact list (or open loop list) holds the eight newest rows by creation, and the old one is not among
    them. The merge of recent changes adds the old one back, because an event touched it after all eight were made,
    and it reads the five newest events to find it. Rows are in the project ``PROJECT_A``.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        if kind == "memory":
            old = add_memory(store, key="key.old", text="OLD note", scope=(PROJECT_A,))
            for index in range(8):
                add_memory(store, key=f"key.n{index}", text=f"N{index} note", scope=(PROJECT_A,))
            store.update_memory(memory_id=str(old["id"]), patch={"summary": "touched since"})
            for index in range(flood):
                add_memory(store, key=f"key.f{index}", text=f"FLOOD{index} note", scope=(PROJECT_A,), **flood_fields)
        else:
            old = add_loop(store, title="OLD note", scope=(PROJECT_A,))
            for index in range(8):
                add_loop(store, title=f"N{index} note", scope=(PROJECT_A,))
            store.update_open_loop(loop_id=str(old["id"]), patch={"description": "touched since"})
            for index in range(flood):
                add_loop(store, title=f"FLOOD{index} note", scope=(PROJECT_A,), **flood_fields)
    return data_dir


@pytest.mark.parametrize("kind", ["memory", "loop"])
@pytest.mark.parametrize("hidden", ["sensitivity", "domain"])
def test_the_brief_merge_finds_a_touched_note_behind_newer_events_the_caller_may_not_see(
    tmp_path: Path, kind: str, hidden: str
) -> None:
    """Mutation: pass ``sensitivity_allowed=None`` (the sensitivity cases) or ``domains=None`` (the domain cases) in
    the memory event read of ``_merge_recent_change_targets`` (the memory cases), or in its open loop event read (the
    loop cases).

    The merge reads the five newest events. Six newer events of rows the caller may not see (confidential under the
    default ceiling, or in another domain under a ``domains`` request) used to take all five, the fence dropped them,
    and the old note that was touched after the eight newest were made fell out of the brief. The control is the same
    vault with no newer events, which shows the old note, so the fixture holds it.
    """

    if hidden == "sensitivity":
        flood_fields = {"sensitivity": "confidential"}
        request: dict[str, object] = {}
    else:
        flood_fields = {"domain": "personal"}
        request = {"effective_domains": ("project",)}
    (tmp_path / "control").mkdir()
    (tmp_path / "flood").mkdir()
    control_dir = _brief_vault(tmp_path / "control", kind=kind, flood=0, **flood_fields)
    flood_dir = _brief_vault(tmp_path / "flood", kind=kind, flood=6, **flood_fields)
    assert "OLD note" in compile_view_brief(control_dir, project_view(), **request)  # type: ignore[arg-type]
    brief = compile_view_brief(flood_dir, project_view(), **request)  # type: ignore[arg-type]
    assert "OLD note" in brief
    assert "FLOOD" not in brief
    for index in range(8):
        assert f"N{index} note" in brief


def test_the_unscoped_brief_keeps_the_v0200_merge(tmp_path: Path) -> None:
    """Mutation: drop the ``if in_project_view`` test in ``_merge_recent_change_targets`` (so that the unscoped brief
    passes the fence to the readers too), or pass ``in_project_view=True`` from ``compile_session_brief``.

    The vault is the one above with six newer confidential events. The unscoped brief is v0.20.0's: it reads the five
    newest events, fences them afterwards, and loses the touched note, as v0.20.0 did. The project view of the same
    vault shows it. The CHANGELOG says the unscoped read is unchanged, and this is that sentence for the brief.
    """

    (tmp_path / "vault").mkdir()
    data_dir = _brief_vault(tmp_path / "vault", kind="memory", flood=6, sensitivity="confidential")
    assert "OLD note" not in compile_view_brief(data_dir, ProjectView.unscoped())
    assert "OLD note" in compile_view_brief(data_dir, project_view())


def test_the_unscoped_brief_merge_states_no_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop the ``if in_project_view`` test in ``_merge_recent_change_targets`` (so that the unscoped brief
    passes the fence to the readers too), or pass ``in_project_view=True`` from ``compile_session_brief``.

    Scoping off is v0.20.0 and the unscoped brief keeps that read: both event readers get ``None`` for both
    arguments. In the project view they get the request's own lists, which this test also checks.
    """

    data_dir = _brief_vault(tmp_path, kind="memory", flood=0)
    recorded: list[tuple[str, dict[str, object]]] = []
    for name in ("list_resume_memory_events", "list_open_loop_events"):
        original = getattr(SQLiteVNextStore, name)

        def spy(self: SQLiteVNextStore, _name: str = name, _original: object = original, **kwargs: object) -> object:
            recorded.append((_name, dict(kwargs)))
            return _original(self, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(SQLiteVNextStore, name, spy)

    compile_view_brief(data_dir, ProjectView.unscoped(), effective_domains=("project",))
    assert {name for name, _ in recorded} == {"list_resume_memory_events", "list_open_loop_events"}
    for name, kwargs in recorded:
        assert kwargs["domains"] is None and kwargs["sensitivity_allowed"] is None, (name, kwargs)

    recorded.clear()
    compile_view_brief(data_dir, project_view(), effective_domains=("project",))
    assert {name for name, _ in recorded} == {"list_resume_memory_events", "list_open_loop_events"}
    for name, kwargs in recorded:
        assert kwargs["domains"] == ["project"], (name, kwargs)
        assert kwargs["sensitivity_allowed"], (name, kwargs)


# ---------------------------------------------------------------------------------------------------------------
# The CHANGELOG sentence, rebuilt from the vaults it names
# ---------------------------------------------------------------------------------------------------------------

_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight")
_CHANGELOG_BULLET_START = "- `alice_resume` with per-project scoping on now fills its `recent_changes` once"


def _changelog_bullet() -> str:
    bullets = [
        line
        for line in (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if line.startswith(_CHANGELOG_BULLET_START)
    ]
    assert len(bullets) == 1
    return re.sub(r"\s+", " ", bullets[0])


def _before_the_fix(data_dir: Path, repo: Path, *, limit: int, domains: list[str] | None, ceiling: list[str]) -> list[str]:
    """What ``alice_resume`` returned in the project view before this change, rebuilt from the same readers.

    One ``project_first_fill`` whose fetch reads both event kinds with no domain or sensitivity argument and cuts the
    merged list to the limit, then the by-id fence, a sort by time and a cut. The CHANGELOG quotes what that gave on
    the vaults below, and nothing on this branch gives it any more, so this is the only way to recompute it. Run
    against origin/main 5d46f7e3 it returned the same events in the same order as the real ``alice_resume`` on the
    vaults of this test.
    """

    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    view = ProjectView.for_project(detection.context)
    held_back = sensitive_global_exclusion(view)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)

        def read(scope: tuple[str, ...], excluded: frozenset[str], count: int) -> list[dict[str, object]]:
            events = [
                *store.list_resume_memory_events(
                    statuses=tuple(_CONTEXT_MEMORY_STATUSES),
                    projects=scope,
                    limit=count,
                    exclude_global_domains=tuple(sorted(excluded)),
                    domains=None,
                    sensitivity_allowed=None,
                ),
                *store.list_open_loop_events(
                    statuses=tuple(_SQLITE_OPEN_LOOP_ACTIVE_STATUSES),
                    scope_projects=scope,
                    limit=count,
                    exclude_global_domains=tuple(sorted(excluded)),
                    domains=None,
                    sensitivity_allowed=None,
                ),
            ]
            events.sort(key=_event_recency, reverse=True)
            return events[:count]

        filled = project_first_fill(
            limit=limit, view=view, exclude_global_domains=held_back, fetch=fetch_in_two_queries(read)
        )
        kept = [
            event
            for event in filled
            if _resume_event_honours_policy_fence(
                store,
                event,
                effective_domains=tuple(domains or ()),
                effective_sensitivity_allowed=tuple(ceiling),
                exclude_global_domains=held_back,
            )
        ]
        kept.sort(key=_event_recency, reverse=True)
        return _event_labels(store, kept[:limit])


def _events(count: int) -> str:
    return "no events" if count == 0 else f"{_NUMBER_WORDS[count]} event" + ("" if count == 1 else "s")


def test_the_changelog_sentences_about_the_filter_order_hold_on_the_vaults_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: change one number in the CHANGELOG sentences that begin "A request for public notes only" and "With
    the newer private events on the global side" (for example "no events" to "one event"), or change the size of one
    of the vaults below.

    The first sentence names four older public loop events and four newer private memory events, a public-only request
    at four places: the old read held no events and the rule gives four. The second names four project events and
    four newer private global events: the old read held three. Both are rebuilt from the fixtures in the CHANGELOG's
    own words, the old numbers from ``_before_the_fix`` and the new ones from ``alice_resume``. The assertions on
    ``before`` check that each vault is one where the old read went wrong.
    """

    bullet = _changelog_bullet()
    repo = repo_with_remote(tmp_path / "repo")
    request = {"max_recent_changes": 4, "sensitivity_allowed": ["public"]}
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    sentences: list[str] = []
    vaults = (
        (
            [*_rows("P", "loop", "", 4), *_rows("P", "memory", "x", 4, sensitivity="private")],
            "A request for public notes only, with the project holding four public loop events and four newer "
            "private memory events, held {before} at four places where the rule gives {rule}.",
        ),
        (
            [*_rows("P", "loop", "", 4), *_rows("G", "memory", "x", 4, sensitivity="private")],
            "With the newer private events on the global side instead, it held {before} at four places where the "
            "rule gives {rule}.",
        ),
    )
    for index, (rows, wording) in enumerate(vaults):
        vault_root = tmp_path / f"vault-{index}"
        vault_root.mkdir()
        data_dir = _vault(vault_root, repo, rows)
        before = _before_the_fix(data_dir, repo, limit=4, domains=None, ceiling=["public"])
        now = _resume(data_dir, repo, **request)
        assert len(now) == 4, now
        assert len(before) < 4, before
        sentences.append(wording.format(before=_events(len(before)), rule=_NUMBER_WORDS[len(now)]))
    for sentence in sentences:
        assert sentence in bullet, sentence
    # The numbers are the ones the change measured: none and three, against four.
    assert "held no events at four places where the rule gives four." in sentences[0]
    assert "it held three events at four places where the rule gives four." in sentences[1]
