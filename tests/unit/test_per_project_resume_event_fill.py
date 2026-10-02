"""Per-project memory S2: the recent changes of ``alice_resume`` fill their places once, across both event kinds (spec 6.2, 6.6).

Spec 6.2 gives one number to every list read without a query: the project's rows first, and
``limit // 4`` places held for global rows when the project has more than enough and global has any.
``alice_resume`` reads two kinds of event (memory events and open loop events) and returns one list of
``max_recent_changes`` rows, so the places belong to that list. Filling each kind to the limit, merging, and
cutting by time lets global events take far more than the reserve (every place, when the project has events of
one kind only), and lets newer global events of one kind push out a project event of the other kind.

The vaults below create the project's events first and the global ones after, so every global event is newer
than every project event: the sort by time cannot help the project, and only the fill keeps its places.
Each test names the edit that makes it fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.retrieval_shared import _CONTEXT_MEMORY_STATUSES, _SQLITE_OPEN_LOOP_ACTIVE_STATUSES
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import ProjectView, fetch_in_two_queries, fill_counts, project_first_fill
from alicebot_api.session_briefing import sensitive_global_exclusion
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for, repo_with_remote

USER_ID = "00000000-0000-0000-0000-000000000001"

REPO_ROOT = Path(__file__).resolve().parents[2]

Change = tuple[str, str]  # (kind, label): ("memory", "P mem 3") or ("loop", "G loop 1")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _project_id(repo: Path) -> str:
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    return detection.context.ids[0]


def _vault(
    tmp_path: Path,
    repo: Path,
    *,
    project_memories: int,
    project_loops: int,
    global_memories: int,
    global_loops: int,
) -> Path:
    """The project's events, oldest first, then the global ones, so every global event is the newer.

    Within each side the two kinds alternate, so the newest event of a side is a loop event when the side has
    as many loops as memories. Every create writes one event, and a label says which side and kind it is.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_id(repo)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for side, scope, memories, loops in (
            ("P", (project_id,), project_memories, project_loops),
            ("G", None, global_memories, global_loops),
        ):
            for index in range(max(memories, loops)):
                if index < memories:
                    add_memory(store, key=f"{side}.mem.{index}", text=f"{side} mem {index}", scope=scope)
                if index < loops:
                    add_loop(store, title=f"{side} loop {index}", scope=scope)
    return data_dir


def _recent_changes(data_dir: Path, repo: Path, **arguments: object) -> list[Change]:
    """What each ``recent_changes`` row points at, newest first, read by id the way a client would."""

    context = MCPRuntimeContext(
        database_url=sqlite_url_for_path(db_path_for(data_dir)),
        user_id=USER_ID,
        project_dir=str(repo),
    )
    brief = call_mcp_tool(context, name="alice_resume", arguments=dict(arguments))["brief"]
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        return _labels(SQLiteVNextStore(connection, USER_ID), brief["recent_changes"])


def _labels(store: SQLiteVNextStore, events: list[dict[str, object]]) -> list[Change]:
    """The kind and label of the row each event points at, in the order of ``events``."""

    changes: list[Change] = []
    for event in events:
        if event["target_type"] == "memory":
            row = store.get_memory(str(event["target_id"]))
            assert row is not None
            changes.append(("memory", str(row["canonical_text"])))
        else:
            loop = store.get_open_loop(str(event["target_id"]))
            assert loop is not None
            changes.append(("loop", str(loop["title"])))
    return changes


def _sides(changes: list[Change]) -> tuple[int, int]:
    """How many rows are the project's and how many are global."""

    return (
        sum(1 for _kind, label in changes if label.startswith("P ")),
        sum(1 for _kind, label in changes if label.startswith("G ")),
    )


def test_the_global_reserve_holds_for_the_whole_list_and_not_once_per_event_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: fill each event kind on its own again in the project branch of ``_vnext_resume``, one
    ``project_first_fill`` over ``list_resume_memory_events`` and another over ``list_open_loop_events``, then
    merge and cut.

    Four places. The project has four memory events and four loop events, and global has two of each, all newer.
    Spec 6.2 gives ``fill_counts(limit=4, 8 project, 4 global)``, three project rows and one global row. A fill
    per kind keeps one global row of each kind (``4 // 4`` each), and the two newest of those sort ahead of the
    project's, which gives two and two. The rows that stay are the project's three newest events and the
    newest global one, newest first. The control is the same vault with scoping off, which is v0.20.0 and
    returns the four newest events of the vault, all global: the fixture really holds the newer global events.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_memories=4, project_loops=4, global_memories=2, global_loops=2)
    control = _recent_changes(data_dir, repo, max_recent_changes=4)
    assert _sides(control) == (0, 4), control
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=4)
    assert _sides(changes) == fill_counts(limit=4, project_available=8, global_available=4) == (3, 1), changes
    assert changes == [
        ("loop", "G loop 1"),
        ("loop", "P loop 3"),
        ("memory", "P mem 3"),
        ("loop", "P loop 2"),
    ]


def test_the_default_limit_keeps_four_project_events_and_one_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: the same edit as above (a fill per event kind).

    The default is five places, and ``5 // 4`` reserves one. A fill per kind kept one global row of each kind
    and the two sorted ahead of the project's four newest, which gave three project rows and two global ones.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_memories=5, project_loops=5, global_memories=2, global_loops=2)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo)
    assert _sides(changes) == fill_counts(limit=5, project_available=10, global_available=4) == (4, 1), changes
    assert changes == [
        ("loop", "G loop 1"),
        ("loop", "P loop 4"),
        ("memory", "P mem 4"),
        ("loop", "P loop 3"),
        ("memory", "P mem 3"),
    ]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(1, [("memory", "P mem 0")], id="limit-1"),
        pytest.param(2, [("loop", "G loop 2"), ("memory", "P mem 0")], id="limit-2"),
        pytest.param(3, [("loop", "G loop 2"), ("loop", "G loop 1"), ("memory", "P mem 0")], id="limit-3"),
    ],
)
def test_a_project_with_events_of_one_kind_only_still_gets_its_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, expected: list[Change]
) -> None:
    """Mutation: the same edit as above (a fill per event kind).

    The project has one memory event and no loop events. Global has three newer loop events and no memory
    events. Limits 1 to 3 reserve nothing (``limit // 4`` is 0), so the project's event comes first and a caller
    who asks for one item gets it (spec 6.2). A fill per kind finds no project rows in the loop kind and
    back-fills that kind with global loop events, and the newest of them sort ahead of the project's only event:
    at limit 1 the result is a global event and the project's is gone.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_memories=1, project_loops=0, global_memories=0, global_loops=3)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=limit)
    assert changes == expected
    assert _sides(changes) == fill_counts(limit=limit, project_available=1, global_available=3)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(
            4,
            [("loop", "G loop 3"), ("memory", "P mem 3"), ("memory", "P mem 2"), ("memory", "P mem 1")],
            id="limit-4",
        ),
        pytest.param(
            8,
            [
                ("loop", "G loop 7"),
                ("loop", "G loop 6"),
                ("memory", "P mem 7"),
                ("memory", "P mem 6"),
                ("memory", "P mem 5"),
                ("memory", "P mem 4"),
                ("memory", "P mem 3"),
                ("memory", "P mem 2"),
            ],
            id="limit-8",
        ),
    ],
)
def test_a_project_with_events_of_one_kind_only_keeps_most_places_at_larger_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, expected: list[Change]
) -> None:
    """Mutation: the same edit as above (a fill per event kind).

    This pins the far side of the old defect. The project has ``limit`` memory events and no loop events, and
    global has ``limit`` newer loop events and no memory events. Spec 6.2 gives ``fill_counts``, three project
    rows and one global row at four places, six and two at eight. A fill per kind reads the project's whole
    memory side (``limit`` rows) and, because the project has no loop events, back-fills the loop side with
    ``limit`` global loop events. All of those are newer, so they fill every place and the project gets none:
    zero and four at four places, zero and eight at eight.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_memories=limit, project_loops=0, global_memories=0, global_loops=limit)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=limit)
    assert _sides(changes) == fill_counts(limit=limit, project_available=limit, global_available=limit), changes
    assert _sides(changes) == {4: (3, 1), 8: (6, 2)}[limit], changes
    assert changes == expected


def test_the_merged_read_of_one_side_returns_its_newest_events_across_both_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: in the merged event reader of ``_vnext_resume``, drop the sort by time (so the memory events
    come before the loop events), or read only one event kind.

    The project's newest events alternate between the kinds (loop 3, memory 3, loop 2, memory 2, ...). At seven
    places the project keeps six of them. A reader that puts all memory events first would hand the fill memory
    events 3, 2, 1 and 0 and loop events 3 and 2, so the result would hold memory 0 and lose loop 1, which is
    newer. A reader that reads one kind only could not return a row of the other.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_memories=4, project_loops=4, global_memories=2, global_loops=2)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=7)
    # Seven places reserve one (7 // 4): six project rows, then the newest global row.
    assert _sides(changes) == fill_counts(limit=7, project_available=8, global_available=4) == (6, 1), changes
    assert changes == [
        ("loop", "G loop 1"),
        ("loop", "P loop 3"),
        ("memory", "P mem 3"),
        ("loop", "P loop 2"),
        ("memory", "P mem 2"),
        ("loop", "P loop 1"),
        ("memory", "P mem 1"),
    ]


# ---------------------------------------------------------------------------------------------------------------
# The numbers and the wording of the CHANGELOG entry, rebuilt from the fixtures.
# ---------------------------------------------------------------------------------------------------------------

_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight")
_CHANGELOG_BULLET_START = "- `alice_resume` with per-project scoping on now fills its `recent_changes` once"


def _words(number: int) -> str:
    return _NUMBER_WORDS[number]


def _changelog_bullet() -> str:
    """The one Unreleased bullet about this change, with runs of whitespace collapsed."""

    bullets = [
        line
        for line in (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if line.startswith(_CHANGELOG_BULLET_START)
    ]
    assert len(bullets) == 1
    return re.sub(r"\s+", " ", bullets[0])


def _per_kind_fill_changes(data_dir: Path, repo: Path, limit: int) -> list[Change]:
    """What ``_vnext_resume`` returned before this change, rebuilt from the same store readers.

    One ``project_first_fill`` over the memory events and another over the open loop events, each to ``limit``,
    then the two lists merged, sorted by time and cut to ``limit``. The CHANGELOG quotes what that gave on the
    vaults below, and nothing on this branch gives it any more, so this is the only way to recompute it. Run
    against ``main`` before this change, it returned the same rows in the same order as the real ``alice_resume``
    on every vault of this file at each limit tried (13 cases).
    """

    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    view = ProjectView.for_project(detection.context)
    held_back = sensitive_global_exclusion(view)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        memory_events = project_first_fill(
            limit=limit,
            view=view,
            exclude_global_domains=held_back,
            fetch=fetch_in_two_queries(
                lambda scope, excluded, count: store.list_resume_memory_events(
                    statuses=tuple(_CONTEXT_MEMORY_STATUSES),
                    projects=scope,
                    limit=count,
                    exclude_global_domains=tuple(sorted(excluded)),
                )
            ),
        )
        loop_events = project_first_fill(
            limit=limit,
            view=view,
            exclude_global_domains=held_back,
            fetch=fetch_in_two_queries(
                lambda scope, excluded, count: store.list_open_loop_events(
                    statuses=tuple(_SQLITE_OPEN_LOOP_ACTIVE_STATUSES),
                    scope_projects=scope,
                    limit=count,
                    exclude_global_domains=tuple(sorted(excluded)),
                )
            ),
        )
        merged = sorted(
            [*memory_events, *loop_events],
            key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("id") or "")),
            reverse=True,
        )
        return _labels(store, merged[:limit])


def _one_kind_only_vault_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, events_each: int, places: int
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Project and global rows at ``places``: before this change, now, and what spec 6.2 gives.

    The project holds ``events_each`` memory events and no loop events. Global holds ``events_each`` loop events
    and no memory events, all newer than the project's.
    """

    base = tmp_path / f"one-kind-{events_each}"
    base.mkdir()
    repo = repo_with_remote(base / "repo")
    data_dir = _vault(
        base, repo, project_memories=events_each, project_loops=0, global_memories=0, global_loops=events_each
    )
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    before = _sides(_per_kind_fill_changes(data_dir, repo, places))
    now = _sides(_recent_changes(data_dir, repo, max_recent_changes=places))
    rule = fill_counts(limit=places, project_available=events_each, global_available=events_each)
    return before, now, rule


def test_the_changelog_sentences_about_one_kind_of_event_hold_on_the_vaults_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: change one number in either sentence of the CHANGELOG bullet that begins "With the project
    holding" and "With eight memory events for the project" (for example "six and two" to "five and three", or
    "eight global ones" to "seven global ones"), or put the eight-place numbers back on the four-event vault, or
    change the size of one of the two vaults below.

    The first sentence names a vault of four memory events for the project, none of the other kind, and four newer
    loop events for global, at four places. The second names eight of each at eight places. Both are rebuilt here
    from the fixtures, in the CHANGELOG's own words, and compared with the bullet. The numbers before the change
    come from ``_per_kind_fill_changes``, the numbers now from ``alice_resume``, and the rule from ``fill_counts``.
    An earlier draft gave the eight-place numbers on the four-event vault, where the old fill and the rule agree
    (four and four), so the last assertion also checks that each vault is one where the old fill went wrong.
    """

    bullet = _changelog_bullet()
    expected: list[str] = []
    for events_each, places in ((4, 4), (8, 8)):
        before, now, rule = _one_kind_only_vault_numbers(
            tmp_path, monkeypatch, events_each=events_each, places=places
        )
        assert now == rule, (events_each, places, now, rule)
        assert before[0] == 0, (events_each, places, before)
        assert before != rule, (events_each, places, before, rule)
        rule_project, rule_global = rule
        opening = (
            f"With the project holding {_words(events_each)} memory events and no loop events and global holding "
            f"{_words(events_each)} newer loop events, the list held"
            if events_each == 4
            else f"With {_words(events_each)} memory events for the project and {_words(events_each)} newer loop "
            "events for global, it held"
        )
        expected.append(
            f"{opening} no project event and {_words(before[1])} global ones at {_words(places)} places, "
            f"where the rule gives {_words(rule_project)} and {_words(rule_global)}."
        )
    for sentence in expected:
        assert sentence in bullet, sentence
    # The numbers are the ones the PR measured: three and one at four places, six and two at eight.
    assert "where the rule gives three and one." in expected[0]
    assert "where the rule gives six and two." in expected[1]


@pytest.mark.parametrize(
    ("global_memories", "global_loops", "global_rows"),
    [
        pytest.param(0, 0, 0, id="no-global-events"),
        pytest.param(1, 0, 1, id="one-global-event"),
        pytest.param(2, 2, 2, id="four-global-events"),
    ],
)
def test_at_most_a_quarter_of_the_places_is_held_for_global_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_memories: int, global_loops: int, global_rows: int
) -> None:
    """Mutation: in ``fill_counts``, hold ``reserved`` places whether or not global has that many rows (replace
    ``min(reserved, global_available)`` with ``reserved``), or hold ``limit // 4`` global rows from a vault that
    has fewer.

    The project holds ten events (five of each kind), and the limit is eight, so a quarter is two places. With no
    global event the project fills all eight, with one global event it gets seven and global one, and with four
    global events the project gets six and global two. The CHANGELOG and the docs say "at most a quarter", and this
    is the case that makes the words true: the quarter is a ceiling and not a reserve that stays empty.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(
        tmp_path,
        repo,
        project_memories=5,
        project_loops=5,
        global_memories=global_memories,
        global_loops=global_loops,
    )
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=8)
    assert _sides(changes) == (8 - global_rows, global_rows), changes
    assert _sides(changes) == fill_counts(
        limit=8, project_available=10, global_available=global_memories + global_loops
    )


def test_the_changelog_and_the_docs_both_say_at_most_a_quarter() -> None:
    """Mutation: take "At most" out of the CHANGELOG bullet (leaving "A quarter of the places"), or "at most" out of
    the sentence about ``recent_changes`` in ``docs/alpha/mcp-tools.md``.

    A quarter of the places is a ceiling: it holds only when the project has more than enough events and global
    has at least that many (the test above), so both places say "at most".
    """

    assert (
        "At most a quarter of the places (`max_recent_changes // 4`, rounded down) is held for global ones whenever "
        "the project has more than enough events and global has any"
    ) in _changelog_bullet()
    docs = re.sub(r"\s+", " ", (REPO_ROOT / "docs" / "alpha" / "mcp-tools.md").read_text(encoding="utf-8"))
    assert (
        "at most a quarter of its places, rounded down, are held for global events (whichever kind they are) when "
        "the project has more than enough events of its own"
    ) in docs


# ---------------------------------------------------------------------------------------------------------------
# What ``read_events`` hands to the two store readers.
# ---------------------------------------------------------------------------------------------------------------


def _filter_vault(tmp_path: Path, repo: Path) -> Path:
    """Four events for the project and four for global, two of each kind on each side, one of each pair matching
    the word ``needle``. Global is created second, so its events are the newer."""

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_id(repo)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for side, scope in (("P", (project_id,)), ("G", None)):
            add_memory(store, key=f"{side}.mem.a", text=f"{side} mem needle", scope=scope)
            add_memory(store, key=f"{side}.mem.b", text=f"{side} mem hay", scope=scope)
            add_loop(store, title=f"{side} loop needle", scope=scope)
            add_loop(store, title=f"{side} loop hay", scope=scope)
    return data_dir


def test_the_query_narrows_both_event_kinds_on_both_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop ``query=query`` from the memory event read, or from the open loop event read, in
    ``read_events`` of ``_vnext_resume``.

    Eight places are enough for every event of the vault. The query ``needle`` matches one memory and one loop on
    each side, so the list holds those four. With the query dropped from the memory read, the two memories that say
    ``hay`` come back as well, and with it dropped from the loop read the two loops that say ``hay`` do.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _filter_vault(tmp_path, repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    unnarrowed = _recent_changes(data_dir, repo, max_recent_changes=8)
    assert len(unnarrowed) == 8, unnarrowed
    changes = _recent_changes(data_dir, repo, max_recent_changes=8, query="needle")
    assert changes == [
        ("loop", "G loop needle"),
        ("memory", "G mem needle"),
        ("loop", "P loop needle"),
        ("memory", "P mem needle"),
    ]


@pytest.mark.parametrize(
    ("window", "expected_rows"),
    [
        pytest.param({"since": "2100-01-01T00:00:00Z"}, 0, id="since-after-every-event"),
        pytest.param({"until": "2000-01-01T00:00:00Z"}, 0, id="until-before-every-event"),
        pytest.param({"since": "2000-01-01T00:00:00Z", "until": "2100-01-01T00:00:00Z"}, 8, id="window-holds-them-all"),
    ],
)
def test_the_time_window_reaches_both_event_kinds_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: dict[str, str], expected_rows: int
) -> None:
    """Mutation: drop ``occurred_at_start=since`` or ``occurred_at_end=until`` from the memory event read, or from
    the open loop event read, in ``read_events`` of ``_vnext_resume``, or swap the two.

    Every event of the vault was written just now. A ``since`` in 2100 leaves out all of them, and an ``until`` in
    2000 leaves out all of them, so a read that lost its bound returns that kind (two events on each side) and the
    list is no longer empty. The third case is the control: a window that holds every event returns all eight, so
    the empty lists come from the bound and from nothing else.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _filter_vault(tmp_path, repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    changes = _recent_changes(data_dir, repo, max_recent_changes=8, **window)
    assert len(changes) == expected_rows, changes


def _record_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[dict[str, object]]:
    """Wrap one reader of ``SQLiteVNextStore`` so that it records the keyword arguments of each call."""

    recorded: list[dict[str, object]] = []
    original = getattr(SQLiteVNextStore, name)

    def spy(self: SQLiteVNextStore, **kwargs: object) -> object:
        recorded.append(dict(kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(SQLiteVNextStore, name, spy)
    return recorded


def test_the_limit_reaches_both_event_readers_for_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: drop ``limit=count`` from the memory event read, or from the open loop event read, in
    ``read_events`` of ``_vnext_resume``.

    The limit cannot be seen in the result. The readers default to 20 rows, which is also the most
    ``max_recent_changes`` can be, and ``read_events`` cuts its own list to ``count``, so a read that lost the limit
    still returns the same rows and only reads more of them. So this test watches the calls. Each reader is called
    once for the project and once for global, with the limit that the fill asked for (three here), and the other
    three bounds in the same calls are the ones the caller gave.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _filter_vault(tmp_path, repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    calls = {
        "list_resume_memory_events": _record_calls(monkeypatch, "list_resume_memory_events"),
        "list_open_loop_events": _record_calls(monkeypatch, "list_open_loop_events"),
    }
    _recent_changes(
        data_dir,
        repo,
        max_recent_changes=3,
        query="needle",
        since="2000-01-01T00:00:00Z",
        until="2100-01-01T00:00:00Z",
    )
    for name, recorded in calls.items():
        assert len(recorded) == 2, (name, recorded)
        for kwargs in recorded:
            assert kwargs.get("limit") == 3, (name, kwargs)
            assert kwargs.get("query") == "needle", (name, kwargs)
            assert str(kwargs.get("occurred_at_start")).startswith("2000-01-01"), (name, kwargs)
            assert str(kwargs.get("occurred_at_end")).startswith("2100-01-01"), (name, kwargs)
