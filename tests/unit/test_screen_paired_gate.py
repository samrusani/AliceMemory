"""The operator screens of the read-budget smoke answer to two checks, and
the three timing modules run them through one shared helper.

The paired rule (1.25 times the merge base plus 0.05 seconds, on this
runner) catches one pull request that slows a screen; the absolute limit
catches large slowdowns and very slow runners. The checks live in
``screen_gate_failures`` (``tests/performance/read_budget_gate.py``) and the
round-three, round-two and handoff modules call it when the absolute gate is
on and a merge-base checkout is set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.performance.read_budget_gate import screen_gate_failures

MODULES = (
    "tests/performance/test_label_round3_read_budget.py",
    "tests/performance/test_label_round2_read_budget.py",
    "tests/integration/test_label_read_handoff_budget_postgres.py",
)


def _head(base, factor=1.0):
    return {"workspace": {"minimum_wall": base, "median": base},
            "dogfooding": {"minimum_wall": base, "median": base}} if factor == 1.0 else \
        {"workspace": {"minimum_wall": base * factor, "median": base * factor},
         "dogfooding": {"minimum_wall": base * factor, "median": base * factor}}


def test_a_head_over_one_and_a_quarter_times_the_base_plus_the_slack_fails() -> None:
    """Guard: the paired comparison in ``screen_gate_failures`` (dropping it
    leaves only the absolute limit, and a same-runner slowdown passes). Each
    row names the check that failed."""

    head = {"workspace": {"minimum_wall": 0.31, "median": 0.31},
            "dogfooding": {"minimum_wall": 0.1, "median": 0.1}}
    base = {"workspace": {"minimum_wall": 0.1, "median": 0.1},
            "dogfooding": {"minimum_wall": 0.1, "median": 0.1}}
    rows = screen_gate_failures(head, base, statistic="minimum_wall", limit=2.0)
    assert rows == [("paired_merge_base", "workspace", "minimum_wall", 0.31, 0.1)], rows
    assert all(row[0] in ("paired_merge_base", "absolute_limit") for row in rows)


def test_a_head_equal_to_its_base_passes() -> None:
    head = {"workspace": {"minimum_wall": 0.9, "median": 0.9},
            "dogfooding": {"minimum_wall": 0.9, "median": 0.9}}
    base = {"workspace": {"minimum_wall": 0.9, "median": 0.9},
            "dogfooding": {"minimum_wall": 0.9, "median": 0.9}}
    assert screen_gate_failures(head, base, statistic="minimum_wall", limit=2.0) == []


def test_a_head_exactly_on_the_paired_boundary_passes() -> None:
    """The boundary itself is not a failure: 1.25 times the base plus 0.05
    seconds passes, exactly as pack and recall pass on it."""

    base = 1.2
    boundary = 1.25 * base + 0.05
    head = {"workspace": {"minimum_wall": boundary, "median": boundary},
            "dogfooding": {"minimum_wall": boundary, "median": boundary}}
    equal_base = {"workspace": {"minimum_wall": base, "median": base},
                  "dogfooding": {"minimum_wall": base, "median": base}}
    assert screen_gate_failures(head, equal_base, statistic="minimum_wall", limit=2.0) == []


def test_the_absolute_limit_catches_what_the_paired_rule_cannot(tmp_path: Path) -> None:
    """A screen whose merge base is equally slow passes the paired rule but
    fails the absolute limit; a head at the limit itself passes. The absolute
    rows name the absolute limit, so a red smoke job says which check tripped."""

    head = {"workspace": {"minimum_wall": 1.4, "median": 1.4},
            "dogfooding": {"minimum_wall": 1.4, "median": 1.4}}
    base = {"workspace": {"minimum_wall": 1.2, "median": 1.2},
            "dogfooding": {"minimum_wall": 1.2, "median": 1.2}}
    assert screen_gate_failures(head, base, statistic="minimum_wall", limit=1.5) == []
    head = {"workspace": {"minimum_wall": 1.500001, "median": 1.500001},
            "dogfooding": {"minimum_wall": 1.500001, "median": 1.500001}}
    assert screen_gate_failures(head, base, statistic="minimum_wall", limit=1.5) == [
        ("absolute_limit", "workspace", "minimum_wall", 1.500001, 1.2),
        ("absolute_limit", "dogfooding", "minimum_wall", 1.500001, 1.2),
    ]


def test_when_both_checks_fail_both_rows_appear_and_are_told_apart() -> None:
    """A screen past both bounds answers two rows, and the check names keep
    them distinguishable (the same numbers twice would not say whether the
    pull request or the runner tripped the job)."""

    head = {"workspace": {"minimum_wall": 0.31, "median": 0.31},
            "dogfooding": {"minimum_wall": 0.1, "median": 0.1}}
    base = {"workspace": {"minimum_wall": 0.1, "median": 0.1},
            "dogfooding": {"minimum_wall": 0.1, "median": 0.1}}
    rows = screen_gate_failures(head, base, statistic="minimum_wall", limit=0.3)
    assert rows == [
        ("paired_merge_base", "workspace", "minimum_wall", 0.31, 0.1),
        ("absolute_limit", "workspace", "minimum_wall", 0.31, 0.1),
    ], rows


def test_the_screens_are_not_checked_without_the_merge_base(tmp_path: Path) -> None:
    """Guard: without a merge-base checkout the paired rule has nothing to
    compare and the caller does not run the check (the handoff module's solo
    branch keeps the absolute limit only)."""

    repo = Path(__file__).resolve().parents[2]
    handoff = (repo / "tests/integration/test_label_read_handoff_budget_postgres.py").read_text()
    paired_block = handoff.index("screen_gate_failures(")
    gate_line = handoff.index('ALICE_READ_ABSOLUTE_GATE') 
    merge_branch = handoff.index('if merge_base:')
    assert handoff.index("ALICE_READ_MERGE_BASE_CHECKOUT") < merge_branch
    assert merge_branch < paired_block
    assert gate_line >= 0


@pytest.mark.parametrize("module", MODULES)
def test_each_timing_module_runs_the_screens_through_the_shared_helper(module: str) -> None:
    """Guard: each of the three timing modules checks the screens through
    ``screen_gate_failures`` (a module that drops the call fails here, and
    with it its mutation case)."""

    repo = Path(__file__).resolve().parents[2]
    source = (repo / module).read_text()
    assert "screen_gate_failures(" in source, module
    assert "ALICE_READ_ABSOLUTE_GATE" in source, module


@pytest.mark.parametrize("module", MODULES)
def test_each_module_checks_the_screens_only_under_the_absolute_gate(module: str) -> None:
    """The screens are timed only when the absolute gate is on, so each
    module's screen check sits behind that variable: the gate-off guard and
    the helper call appear in the same block, with the gate line first."""

    repo = Path(__file__).resolve().parents[2]
    source = (repo / module).read_text()
    gate = source.index('"ALICE_READ_ABSOLUTE_GATE"')
    call = source.index("screen_gate_failures(")
    assert gate < call, module


def test_the_smoke_step_keeps_the_backstop_limit_at_two_seconds() -> None:
    """The absolute limit is a backstop for large slowdowns and very slow
    runners; the paired rule catches same-runner slowdowns.

    Mutation: set ``ALICE_READ_ABSOLUTE_LIMIT_SECONDS`` back to ``1.5`` in
    the step (the limit the evidence shows a runner needs to exceed to fail).
    """

    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text()
    assert 'ALICE_READ_ABSOLUTE_LIMIT_SECONDS: "2.0"' in workflow
    assert 'ALICE_READ_ABSOLUTE_LIMIT_SECONDS: "1.5"' not in workflow
