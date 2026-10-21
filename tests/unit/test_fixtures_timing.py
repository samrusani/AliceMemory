"""The growth check of ``fixtures_timing`` accepts a linear scan, refuses a quadratic one, and ignores noise.

The linear-time tests are only as good as this measure, so it is tested with a
clock the test moves, not with the time of anything real: a "run" here costs a
number of seconds the test chose, and nothing in this file waits.

Mutations: keep the last round instead of the smallest (the noise test
fails); run all the small rounds and then all the large ones (the order test
fails); leave the collector off when a run raises, or never turn it off (the
collector tests fail); drop the early stop (the give-up test fails); set
``GROWTH_LIMIT`` to ``QUADRATIC_RATIO`` (the limits test and the test of a scan
that is most of the way to quadratic fail).
"""

from __future__ import annotations

import gc

import pytest

from tests.unit import fixtures_timing
from tests.unit.fixtures_timing import (
    GROWTH_LIMIT,
    LINEAR_RATIO,
    QUADRATIC_RATIO,
    ROUNDS,
    SIZE_RATIO,
    assert_grows_linearly,
    smallest_cpu_seconds,
)


class _Clock:
    """A clock that only moves when a run says how long it took."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(fixtures_timing, "_clock", fake)
    return fake


def _runs(clock: _Clock, small_costs: list[float], large_costs: list[float], order: list[str] | None = None):
    """Two runs that take, round after round, the next cost of their list."""

    small_left, large_left = list(small_costs), list(large_costs)

    def run_small() -> None:
        clock.now += small_left.pop(0)
        if order is not None:
            order.append("small")

    def run_large() -> None:
        clock.now += large_left.pop(0)
        if order is not None:
            order.append("large")

    return run_small, run_large


def test_the_limits_sit_between_a_linear_and_a_quadratic_scan() -> None:
    assert (SIZE_RATIO, LINEAR_RATIO, QUADRATIC_RATIO) == (8, 8, 64)
    assert GROWTH_LIMIT == pytest.approx(QUADRATIC_RATIO / 3)
    assert 2 * LINEAR_RATIO < GROWTH_LIMIT < QUADRATIC_RATIO / 2


def test_a_linear_scan_passes_and_a_quadratic_one_fails(clock: _Clock) -> None:
    small, large = smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [0.01 * LINEAR_RATIO] * ROUNDS))
    assert (round(small, 6), round(large, 6)) == (0.01, 0.08)
    assert_grows_linearly(small, large, what="linear")

    small, large = smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [0.01 * QUADRATIC_RATIO] * ROUNDS))
    with pytest.raises(AssertionError, match="quadratic"):
        assert_grows_linearly(small, large, what="quadratic")

    # Most of the way to quadratic is refused too: 30 times is under 64 and over the limit.
    small, large = smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [0.01 * 30] * ROUNDS))
    with pytest.raises(AssertionError, match="most of the way"):
        assert_grows_linearly(small, large, what="most of the way")


def test_the_limit_is_a_third_of_the_quadratic_ratio() -> None:
    assert_grows_linearly(1.0, GROWTH_LIMIT * 0.99, what="just under", floor_seconds=0.0)
    with pytest.raises(AssertionError):
        assert_grows_linearly(1.0, GROWTH_LIMIT * 1.01, what="just over", floor_seconds=0.0)


def test_a_large_run_under_the_floor_passes_whatever_the_small_one_took() -> None:
    assert_grows_linearly(0.0001, 0.019, what="too short to time")
    with pytest.raises(AssertionError):
        assert_grows_linearly(0.0001, 0.021, what="over the floor")


def test_noise_in_most_rounds_does_not_move_the_smallest(clock: _Clock) -> None:
    # Only one round of each size is quiet, and they are not the same round.
    small_costs = [0.05, 0.01, 0.04, 0.09, 0.03]
    large_costs = [0.30, 0.50, 0.08, 0.40, 0.25]
    small, large = smallest_cpu_seconds(*_runs(clock, small_costs, large_costs))
    assert (round(small, 6), round(large, 6)) == (0.01, 0.08)
    assert_grows_linearly(small, large, what="noisy")


def test_the_rounds_alternate_small_and_large(clock: _Clock) -> None:
    order: list[str] = []
    smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [0.08] * ROUNDS, order))
    assert order == ["small", "large"] * ROUNDS


def test_a_scan_far_over_its_budget_is_not_run_all_the_rounds(clock: _Clock) -> None:
    order: list[str] = []
    smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [9.0] * ROUNDS, order), give_up_over=2.0)
    assert order == ["small", "large"] * 2
    # Under the limit, or with no limit, every round runs.
    order = []
    smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [0.08] * ROUNDS, order), give_up_over=2.0)
    assert len(order) == 2 * ROUNDS
    order = []
    smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [9.0] * ROUNDS, order))
    assert len(order) == 2 * ROUNDS


def test_one_slow_first_round_does_not_end_the_rounds_early(clock: _Clock) -> None:
    order: list[str] = []
    smallest_cpu_seconds(*_runs(clock, [0.01] * ROUNDS, [9.0, 0.08, 0.08, 0.08, 0.08], order), give_up_over=2.0)
    assert len(order) == 2 * ROUNDS


@pytest.mark.parametrize("was_on", [True, False])
def test_the_collector_is_off_while_it_measures_and_as_it_was_after(was_on: bool, clock: _Clock) -> None:
    seen: list[bool] = []

    def run() -> None:
        seen.append(gc.isenabled())

    before = gc.isenabled()
    (gc.enable if was_on else gc.disable)()
    try:
        smallest_cpu_seconds(run, run)
        assert seen == [False] * (2 * ROUNDS)
        assert gc.isenabled() is was_on
    finally:
        (gc.enable if before else gc.disable)()


def test_the_collector_comes_back_when_a_run_raises(clock: _Clock) -> None:
    def fails() -> None:
        raise RuntimeError("the scan failed")

    before = gc.isenabled()
    gc.enable()
    try:
        with pytest.raises(RuntimeError):
            smallest_cpu_seconds(lambda: None, fails)
        assert gc.isenabled()
    finally:
        (gc.enable if before else gc.disable)()
