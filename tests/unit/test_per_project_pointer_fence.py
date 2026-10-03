"""Per-project memory S2: the pointer fence takes the project view (spec 9, test 27).

A superseding or superseded memory in another project is not named in a result in this project's view, exactly as
an id outside a key's binding is dropped today. ``memory_visible`` is built from the domains, the sensitivity
ceiling and the resolved scope, and the view's request tuple (this project's ids and the global marker) is the scope's
project set. Retrieval takes the view in S3. This slice proves the fence already understands it: the predicate, the
supersession walk and the recent-change events, all with a marker-bearing scope.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import json

from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER
from alicebot_api.vnext_retrieval import _memory_visibility_predicate, _ResolvedRetrievalScope
from tests.unit.per_project_s2_support import PROJECT_A, PROJECT_B
from tests.unit.test_memory_id_pointers_respect_the_read_fence import DEFAULT_CEILING, _event, _fake_service
from tests.unit.test_vnext_retrieval import _memory_row

VIEW = _ResolvedRetrievalScope(
    projects=frozenset({PROJECT_A, GLOBAL_PROJECT_MARKER}),
    people=frozenset(),
    window_start=None,
    window_end=None,
    exclude_global_domains=frozenset(),
)
NO_VIEW = _ResolvedRetrievalScope(
    projects=frozenset(), people=frozenset(), window_start=None, window_end=None, exclude_global_domains=frozenset()
)


def _row(memory_id: str, **overrides: object) -> dict[str, object]:
    return _memory_row(memory_id, f"Fact {memory_id}.", **overrides)


MEMORIES = [
    _row("m-a", project_scope=[PROJECT_A]),
    _row("m-b", project_scope=[PROJECT_B]),
    _row("m-global"),
    _row("m-named", project_scope=["acme"]),
    _row("m-both", project_scope=[PROJECT_A, PROJECT_B]),
    _row("m-health", project_scope=[], domain="health"),
]


def test_the_predicate_admits_this_project_and_global_rows_and_not_another_projects() -> None:
    """Mutation: build ``memory_visible`` without the view.

    With the view's tuple as the scope, a row of this project, a row that belongs to no project, a row filed under a
    free-form name and a row that is in both projects are visible. A row of the other project alone is not.
    """

    visible = _memory_visibility_predicate(domains=[], sensitivity_allowed=DEFAULT_CEILING, scope=VIEW)
    got = {str(row["id"]) for row in MEMORIES if visible(row)}
    assert got == {"m-a", "m-global", "m-named", "m-both", "m-health"}
    open_fence = _memory_visibility_predicate(domains=[], sensitivity_allowed=DEFAULT_CEILING, scope=NO_VIEW)
    assert {str(row["id"]) for row in MEMORIES if open_fence(row)} == {str(row["id"]) for row in MEMORIES}


def test_a_superseded_memory_in_another_project_is_not_named_in_this_projects_view() -> None:
    """Mutation: build ``memory_visible`` without the view in the supersession walk.

    The packed memory belongs to this project and says it supersedes three older ones: one in this project, one that
    belongs to no project and one in the other project. The view names the first two and the third is nowhere in the
    notes. With no view the control names all three.
    """

    service = _fake_service([], [_row("old-a", project_scope=[PROJECT_A]), _row("old-global"), _row("old-b", project_scope=[PROJECT_B])])
    for old_id, named_in_view in (("old-a", True), ("old-global", True), ("old-b", False)):
        packed = _row("new", project_scope=[PROJECT_A], supersedes=old_id)
        visible = _memory_visibility_predicate(domains=[], sensitivity_allowed=DEFAULT_CEILING, scope=VIEW)
        notes = service._supersession_context([packed], scope=VIEW, memory_visible=visible)
        names = [entry["id"] for entry in notes[0]["supersedes"]]
        assert (old_id in names) is named_in_view, old_id
        assert named_in_view or old_id not in json.dumps(notes)
        control = _memory_visibility_predicate(domains=[], sensitivity_allowed=DEFAULT_CEILING, scope=NO_VIEW)
        control_notes = service._supersession_context([packed], scope=NO_VIEW, memory_visible=control)
        assert old_id in [entry["id"] for entry in control_notes[0]["supersedes"]], "the control names it"


def test_recent_changes_in_a_project_view_leave_out_another_projects_events() -> None:
    """Mutation: leave the project out of the predicate ``_recent_changes`` builds.

    One event for this project's memory, one for the other project's, one for a global one and one for no row at
    all. The view keeps the first and the third and drops the other project's. An event for no row is dropped too,
    because a scoped read fails closed on a row it cannot find. With no view the control keeps the other project's.
    """

    events = [_event("e-a", "m-a"), _event("e-b", "m-b"), _event("e-global", "m-global"), _event("e-ghost", "m-ghost")]
    service = _fake_service(events, MEMORIES)
    kwargs = {"person_linked_memory_ids": frozenset(), "domains": [], "sensitivity_allowed": DEFAULT_CEILING}
    in_view = {str(change["target_id"]) for change in service._recent_changes(scope=VIEW, **kwargs)}
    assert in_view == {"m-a", "m-global"}
    open_changes = {str(change["target_id"]) for change in service._recent_changes(scope=NO_VIEW, **kwargs)}
    assert open_changes == {"m-a", "m-b", "m-global", "m-ghost"}


def test_the_view_scope_and_the_sensitivity_ceiling_both_apply() -> None:
    """Mutation: let the view relax the ceiling, or the ceiling relax the view.

    A confidential global row stays out of a default-ceiling read in the project view, and a row of the other project
    stays out of a read whose ceiling would allow it.
    """

    rows = [_row("c-global", sensitivity="confidential"), _row("c-b", project_scope=[PROJECT_B], sensitivity="public")]
    default = _memory_visibility_predicate(domains=[], sensitivity_allowed=DEFAULT_CEILING, scope=VIEW)
    assert [default(row) for row in rows] == [False, False]
    wide = _memory_visibility_predicate(
        domains=[], sensitivity_allowed=[*DEFAULT_CEILING, "confidential"], scope=VIEW
    )
    assert [wide(row) for row in rows] == [True, False]
