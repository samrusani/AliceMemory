"""Per-project memory S2: project first, a quarter of the places held for global (spec 6.2, tests 36 and 26).

``project_first_fill`` is the one helper behind the brief and resume. The project's rows
come first, global rows fill what is left, and ``limit // 4`` places are held for global
rows when the project has more than enough and global has any. Each test names the edit
that makes it fail.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alicebot_api.project_view import (
    ProjectView,
    fetch_in_two_queries,
    fill_counts,
    project_first_fill,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for
from tests.unit.per_project_view_support import (
    PROJECT_A,
    PROJECT_B,
    USER_ID,
    compile_view_brief,
    project_view,
)


@pytest.mark.parametrize(
    ("limit", "reserved"),
    [(1, 0), (2, 0), (3, 0), (4, 1), (5, 1), (7, 1), (8, 2), (11, 2), (12, 3), (16, 4)],
)
def test_the_reserve_is_the_limit_divided_by_four_rounded_down(limit: int, reserved: int) -> None:
    """Mutation: round up (``ceil``), or drop the floor.

    With the project holding far more than ``limit`` rows and global holding far more too, the
    global share is exactly ``limit // 4``.
    """

    project_take, global_take = fill_counts(limit=limit, project_available=1000, global_available=1000)
    assert global_take == reserved
    assert project_take == limit - reserved


def test_at_limit_one_the_projects_note_wins_even_when_global_notes_exist() -> None:
    """Mutation: round the reserve up, which gave the only slot to a global note.

    A caller who asks for one item gets the project's.
    """

    assert fill_counts(limit=1, project_available=1, global_available=5) == (1, 0)
    assert fill_counts(limit=3, project_available=3, global_available=9) == (3, 0)


@pytest.mark.parametrize(
    ("limit", "project_rows", "global_rows", "expected"),
    [
        (8, 10, 10, (6, 2)),
        (8, 3, 10, (3, 5)),
        (8, 0, 10, (0, 8)),
        (8, 10, 1, (7, 1)),
        (8, 10, 0, (8, 0)),
        (8, 6, 2, (6, 2)),
        (8, 7, 1, (7, 1)),
        (4, 9, 9, (3, 1)),
        (12, 20, 20, (9, 3)),
    ],
)
def test_global_rows_fill_what_the_project_leaves(
    limit: int, project_rows: int, global_rows: int, expected: tuple[int, int]
) -> None:
    """Mutation: give unused project places back to nobody.

    When the project has fewer rows than its share, global takes the rest, up to its own count;
    with few global rows the project keeps the places global cannot use.
    """

    assert fill_counts(limit=limit, project_available=project_rows, global_available=global_rows) == expected


def test_the_fill_helper_needs_the_project_view_and_a_frozenset() -> None:
    """Mutation: let the helper run for another view, or accept a list for the exclusion."""

    def fetch(ids, excluded, limit):  # type: ignore[no-untyped-def]
        return [], []

    for other in (ProjectView.unscoped(), project_view(choice="project_only"), project_view(choice="global"), project_view(choice="all")):
        with pytest.raises(ValueError):
            project_first_fill(limit=8, view=other, exclude_global_domains=frozenset(), fetch=fetch)
    with pytest.raises(TypeError):
        project_first_fill(limit=8, view=project_view(), exclude_global_domains=["health"], fetch=fetch)  # type: ignore[arg-type]


def test_the_fill_helper_hands_the_exclusion_to_the_global_query_only() -> None:
    """Mutation: pass the exclusion to the project query too.

    ``fetch_in_two_queries`` runs one ordinary query twice. The project query names the ids and
    leaves nothing out, the global query names the marker and carries the excluded domains.
    """

    calls: list[tuple[tuple[str, ...], frozenset[str], int]] = []

    def one(scope: tuple[str, ...], excluded: frozenset[str], limit: int) -> list[str]:
        calls.append((scope, excluded, limit))
        return []

    rows = project_first_fill(
        limit=8,
        view=project_view((PROJECT_A,)),
        exclude_global_domains=frozenset({"health"}),
        fetch=fetch_in_two_queries(one),
    )
    assert rows == []
    assert calls == [
        ((PROJECT_A,), frozenset(), 8),
        (("~global",), frozenset({"health"}), 8),
    ]


def _lines(brief: str, label: str, *, global_rows: bool) -> list[str]:
    prefix = f"**{label}** (global):" if global_rows else f"**{label}**:"
    return [line for line in brief.splitlines() if line.startswith(prefix)]


def _fill_vault(tmp_path: Path, *, project_notes: int, global_notes: int, other_notes: int = 0) -> Path:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(global_notes):
            add_memory(store, key=f"fact.g.{index}", text=f"Global fact number {index} gamma")
            add_loop(store, title=f"Global loop number {index} gamma")
        for index in range(other_notes):
            add_memory(store, key=f"fact.o.{index}", text=f"Other project fact number {index} omega", scope=(PROJECT_B,))
            add_loop(store, title=f"Other project loop number {index} omega", scope=(PROJECT_B,))
        for index in range(project_notes):
            add_memory(store, key=f"fact.p.{index}", text=f"Project fact number {index} alpha", scope=(PROJECT_A,))
            add_loop(store, title=f"Project loop number {index} alpha", scope=(PROJECT_A,))
    return data_dir


@pytest.mark.parametrize(
    ("project_notes", "global_notes", "expected_project", "expected_global"),
    [(10, 10, 6, 2), (3, 10, 3, 5), (0, 10, 0, 8), (10, 1, 7, 1), (10, 0, 8, 0)],
)
def test_the_brief_keeps_two_global_facts_and_two_global_loops_when_the_project_has_plenty(
    tmp_path: Path, project_notes: int, global_notes: int, expected_project: int, expected_global: int
) -> None:
    """Mutation: round up, drop the floor, or give unused project slots back to nobody.

    Limit 8 reserves ``8 // 4`` = 2. With six or more project notes and two or more global notes the brief
    keeps exactly two global facts and two global loops; with fewer project notes the rest goes to global.
    Other projects' notes never appear.
    """

    data_dir = _fill_vault(tmp_path, project_notes=project_notes, global_notes=global_notes, other_notes=4)
    brief = compile_view_brief(data_dir, project_view())
    for label in ("fact", "open loop"):
        assert len(_lines(brief, label, global_rows=False)) == expected_project, (label, brief)
        assert len(_lines(brief, label, global_rows=True)) == expected_global, (label, brief)
    assert "omega" not in brief


def test_the_projects_rows_come_before_global_rows_and_each_group_is_newest_first(tmp_path: Path) -> None:
    """Mutation: sort the two groups together by age.

    Global notes are the newest in the vault, and the project's older notes still come first.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(3):
            add_memory(store, key=f"fact.p.{index}", text=f"Project fact {index} alpha", scope=(PROJECT_A,))
        for index in range(3):
            add_memory(store, key=f"fact.g.{index}", text=f"Global fact {index} gamma")
    facts = [line for line in compile_view_brief(data_dir, project_view()).splitlines() if line.startswith("**fact**")]
    assert facts == [
        '**fact**: "Project fact 2 alpha"',
        '**fact**: "Project fact 1 alpha"',
        '**fact**: "Project fact 0 alpha"',
        '**fact** (global): "Global fact 2 gamma"',
        '**fact** (global): "Global fact 1 gamma"',
        '**fact** (global): "Global fact 0 gamma"',
    ]


def test_a_confidential_global_note_stays_out_of_a_default_ceiling_project_session(tmp_path: Path) -> None:
    """Mutation: build the global query without the sensitivity argument.

    The default ceiling is public, internal, private and unknown. The three newest global facts and the three newest
    global loops are confidential, above ten plain ones each. They stay out of the project brief, and, because the
    ceiling is part of the query, they do not use up places: eight plain facts and eight plain loops still fill it.
    Filtering them only afterwards would leave five of each.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(10):
            add_memory(store, key=f"fact.open.{index}", text=f"Plain global fact number {index} qzpublicfact")
            add_loop(store, title=f"Plain global loop number {index} qzpublicloop")
        for index in range(3):
            add_memory(store, key=f"fact.secret.{index}", text=f"Confidential global fact {index} qzconfidential", sensitivity="confidential")
            add_loop(store, title=f"Confidential global loop {index} qzconfidential", sensitivity="confidential")
    brief = compile_view_brief(data_dir, project_view())
    assert "qzconfidential" not in brief
    assert brief.count("qzpublicfact") == 8
    assert brief.count("qzpublicloop") == 8


def test_a_global_note_outside_the_domain_filter_stays_out_of_a_domain_filtered_session(tmp_path: Path) -> None:
    """Mutation: build the global query without the domain argument.

    A caller that filters to the ``project`` domain gets no global note in ``personal``, though the sensitive-domain
    rule does not name that domain, so this tests the filter and not the rule. The three newest global facts and
    loops are in ``personal``, above ten in ``project``, and the filter is part of the query, so the places are not
    spent on them: eight and eight still come back.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(10):
            add_memory(store, key=f"fact.project.{index}", text=f"Global project fact number {index} qzprojectfact")
            add_loop(store, title=f"Global project loop number {index} qzprojectloop")
        for index in range(3):
            add_memory(store, key=f"fact.personal.{index}", text=f"Global personal fact {index} qzpersonal", domain="personal")
            add_loop(store, title=f"Global personal loop {index} qzpersonal", domain="personal")
    brief = compile_view_brief(data_dir, project_view(), effective_domains=("project",))
    assert "qzpersonal" not in brief
    assert brief.count("qzprojectfact") == 8
    assert brief.count("qzprojectloop") == 8


def test_the_single_scan_reader_returns_what_two_ordinary_queries_return(tmp_path: Path) -> None:
    """Mutation: change the single-scan reader's ordering, filters or partition test.

    ``list_memories_view_partitions`` and ``list_open_loops_view_partitions`` read once and split into the
    project's rows and the global rows. They must equal two ordinary ``list_memories`` and ``list_open_loops``
    calls, one over the project's ids and one over the marker, on a vault of mixed rows with a held-back domain, in
    both orderings the memory list has (by creation, and by last update). Two old rows are touched afterwards, so
    the two orderings differ.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    ceiling = ["public", "internal", "private", "unknown"]
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        made = []
        for index in range(12):
            scope = [(PROJECT_A,), (PROJECT_B,), ("acme",), None][index % 4]
            domain = ["project", "health", "project", "family"][index % 4]
            made.append(
                add_memory(
                    store,
                    key=f"fact.{index}",
                    text=f"Mixed fact number {index} mixed",
                    scope=scope,
                    domain=domain,
                    sensitivity="private" if domain != "project" else "public",
                )
            )
            add_loop(store, title=f"Mixed loop number {index} mixed", scope=scope, domain=domain)
        for old in (made[0], made[2]):
            store.update_memory(memory_id=str(old["id"]), patch={"summary": "touched"})
        for excluded in ((), ("health", "family")):
            for by_creation in (True, False):
                common = dict(
                    status=None,
                    statuses=("active", "accepted"),
                    sensitivity_allowed=ceiling,
                    order_by_created_at=by_creation,
                    include_expired=False,
                )
                project_rows, global_rows = store.list_memories_view_partitions(
                    project_ids=(PROJECT_A,), exclude_global_domains=excluded, per_partition_limit=5, **common
                )
                expected_project = store.list_memories(projects=(PROJECT_A,), limit=5, **common)
                expected_global = store.list_memories(
                    projects=("~global",), limit=5, exclude_global_domains=excluded, **common
                )
                assert [row["id"] for row in project_rows] == [row["id"] for row in expected_project]
                assert [row["id"] for row in global_rows] == [row["id"] for row in expected_global]
                assert project_rows and global_rows
            loop_project, loop_global = store.list_open_loops_view_partitions(
                project_ids=(PROJECT_A,),
                exclude_global_domains=excluded,
                per_partition_limit=5,
                status=None,
                statuses=("open",),
                sensitivity_allowed=ceiling,
            )
            loop_expected_project = store.list_open_loops(
                status=None, statuses=("open",), sensitivity_allowed=ceiling, limit=5, scope_projects=(PROJECT_A,)
            )
            loop_expected_global = store.list_open_loops(
                status=None,
                statuses=("open",),
                sensitivity_allowed=ceiling,
                limit=5,
                scope_projects=("~global",),
                exclude_global_domains=excluded,
            )
            assert [row["id"] for row in loop_project] == [row["id"] for row in loop_expected_project]
            assert [row["id"] for row in loop_global] == [row["id"] for row in loop_expected_global]
            assert loop_project and loop_global
    # The two orderings must differ on this vault, or the comparison above could not tell them apart.
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        by_update = [row["id"] for row in store.list_memories(limit=5, order_by_created_at=False)]
        by_creation = [row["id"] for row in store.list_memories(limit=5, order_by_created_at=True)]
        assert by_update != by_creation


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35, 0),
    reason="AS MATERIALIZED needs SQLite 3.35; an older SQLite reads the same rows without it, twice as slowly",
)
def test_the_single_scan_labels_each_row_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: label rows in a second pass, as two ordinary queries would.

    The label is the scan's Python call, one per row. Two ordinary queries would call it twice for every
    row, which is what the single-scan shape exists to avoid (spec 12). The test counts the calls to
    ``alice_project_scope_identity`` that one partition read makes over 30 memories, each of which holds an
    Alice id so the native fast path decides none of them. SQLite does not share a common table expression
    between two references on its own (68 calls without the hint on 3.49.1), so ``CTE_MATERIALIZED_HINT`` is what
    this test pins.
    """

    from alicebot_api.vnext_stores.sqlite import query_predicates

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    calls = {"n": 0}
    original = query_predicates._project_scope_identity_json_sqlite

    def counting(metadata_json, project_id):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return original(metadata_json, project_id)

    # The function is registered per connection from this module attribute, so patch it before opening one.
    monkeypatch.setattr(query_predicates, "_project_scope_identity_json_sqlite", counting)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        scopes = [(PROJECT_A,), (PROJECT_B,), (PROJECT_A, PROJECT_B), ("acme", PROJECT_B)]
        for index in range(30):
            add_memory(store, key=f"fact.{index}", text=f"Counted fact number {index} counted", scope=scopes[index % 4])
        calls["n"] = 0
        project_rows, _global_rows = store.list_memories_view_partitions(
            project_ids=(PROJECT_A,),
            exclude_global_domains=(),
            per_partition_limit=8,
            status=None,
            statuses=("active", "accepted"),
            order_by_created_at=True,
        )
        assert project_rows
        assert calls["n"] == 30, calls["n"]
