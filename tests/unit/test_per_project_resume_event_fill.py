"""Per-project memory S2: the recent changes of ``alice_resume`` fill their places once, across both event kinds (spec 6.2, 6.6).

Spec 6.2 gives one number to every list read without a query: the project's rows first, and
``limit // 4`` places held for global rows when the project has more than enough and global has any.
``alice_resume`` reads two kinds of event (memory events and open loop events) and returns one list of
``max_recent_changes`` rows, so the places belong to that list. Filling each kind to the limit, merging, and
cutting by time lets global events take up to twice the reserve, and lets newer global events of one kind push
out a project event of the other kind.

The vaults below create the project's events first and the global ones after, so every global event is newer
than every project event: the sort by time cannot help the project, and only the fill keeps its places.
Each test names the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import fill_counts
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for, repo_with_remote

USER_ID = "00000000-0000-0000-0000-000000000001"

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
    changes: list[Change] = []
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for event in brief["recent_changes"]:
            if event["target_type"] == "memory":
                row = store.get_memory(event["target_id"])
                assert row is not None
                changes.append(("memory", str(row["canonical_text"])))
            else:
                loop = store.get_open_loop(event["target_id"])
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
