"""Time budgets and growth checks for the linear-time tests, allowing for a coverage tracer and a busy machine.

Why the budget scales under a tracer (S4 CI, 2026-09-23). CI runs the unit
suite under coverage, which traces every Python line. There the
re-implemented v0.16.0 check took 2.55 s and 2.73 s on two 200 KB shapes of
test_legacy_credential_check.py, against a 2 s budget it meets in at most
0.43 s untraced. Run locally under coverage, the same shapes took at most
1.04 s and four times the input still cost 3.9 to 4.0 times the time: the
tracer adds a constant factor, not a power of the input.

So a linear-time test keeps its ratio assertion (time at eight times the
input against time at one), which a tracer does not change, and only its
absolute budget scales, by TRACED_SLOWDOWN, while a tracer is active.
Untraced, the budget is unchanged. A quadratic scan still fails the scaled
budget: the old assignment pattern took 43 s on 50 KB untraced.

Why the ratio is measured the way it is. The ratio tests used to time one scan
at 50 KB and one at 200 KB on the wall clock, best of three at each size, and
failed above 8 where a linear scan gives 4 and a quadratic one 16. Run in one
process with the rest of the unit suite they failed at 13.8 where linear is
about 4. Two things add time to the larger run and not to the smaller:

- The cyclic collector. A scan that makes a hundred thousand small objects
  wakes it often, and each full pass walks every object the suite has left
  alive, so the cost grows with the size of the process and with the input.
- A busy machine. The processor is taken from a run for a share of its length,
  so the longer run loses more, and the best of three did not find a quiet
  moment for it.

So a measurement here takes the CPU seconds of the calling thread, which the
processor being taken away does not add to; turns the collector off while it
measures; runs the small and the large input in alternating rounds and keeps
the smallest of each; and compares inputs eight times apart, so that linear
(8) and quadratic (64) are far apart. The limit is a third of the quadratic
ratio (21.3), which leaves room on both sides: a linear scan would have to
cost 2.7 times what it should at the larger size to fail, and a scan fails
once the quadratic part of its cost, at the larger size, is two and a half
times the linear part (the wall clock version, at 4 times the input and a
limit of 8, failed it at two times). The patterns these tests guard cost
hundreds of times their linear part, so both limits are far from them.

The scans are regular expressions and a pattern that backtracks does its
extra work inside one call, where no count of calls, slices or matches can see
it. A count would pass the very pattern these tests exist to refuse, so they
time the scan.
"""

from __future__ import annotations

import gc
import math
import sys
import time
from collections.abc import Callable

TRACED_SLOWDOWN = 5

# How much larger the larger input is, and what that costs a scan whose time is
# linear in the input and one whose time is quadratic.
SIZE_RATIO = 8
LINEAR_RATIO = SIZE_RATIO
QUADRATIC_RATIO = SIZE_RATIO**2
# The most the larger run may cost, in times the smaller: a third of the
# quadratic ratio.
GROWTH_LIMIT = QUADRATIC_RATIO / 3
ROUNDS = 5

# Kept as a module attribute so a test of this module can feed it a clock.
_clock = time.thread_time


def tracer_active() -> bool:
    """A trace function (coverage's C tracer, a debugger) or coverage's sys.monitoring tool."""

    if sys.gettrace() is not None:
        return True
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return False
    return monitoring.get_tool(monitoring.COVERAGE_ID) is not None


def budget(seconds: float) -> float:
    """The budget for an untraced run, scaled while a tracer is active."""

    return seconds * TRACED_SLOWDOWN if tracer_active() else seconds


def _cpu_seconds(run: Callable[[], object]) -> float:
    started = _clock()
    run()
    return _clock() - started


def smallest_cpu_seconds(
    run_small: Callable[[], object],
    run_large: Callable[[], object],
    *,
    rounds: int = ROUNDS,
    give_up_over: float | None = None,
) -> tuple[float, float]:
    """The smallest CPU seconds of ``run_small`` and of ``run_large``.

    Each round runs the small one and then the large one, so a stretch of noise
    reaches both. The cyclic collector is off while this runs and is turned on
    again after, if it was on. ``give_up_over`` is an absolute limit on the
    large run: when the smallest large run of two rounds is over it, the
    remaining rounds are not run, so a scan that is far too slow fails after
    two large runs and not after all of them.
    """

    collector_was_on = gc.isenabled()
    gc.disable()
    try:
        smallest_small = smallest_large = math.inf
        for round_number in range(rounds):
            smallest_small = min(smallest_small, _cpu_seconds(run_small))
            smallest_large = min(smallest_large, _cpu_seconds(run_large))
            if give_up_over is not None and round_number >= 1 and smallest_large > give_up_over:
                break
        return smallest_small, smallest_large
    finally:
        if collector_was_on:
            gc.enable()


def assert_grows_linearly(
    small_seconds: float, large_seconds: float, *, what: str, floor_seconds: float = 0.02
) -> None:
    """Fails when the run on ``SIZE_RATIO`` times the input took more than ``GROWTH_LIMIT`` times as long.

    A large run under ``floor_seconds`` always passes, so a small run too short
    to time cannot fail the ratio.
    """

    limit = max(GROWTH_LIMIT * small_seconds, floor_seconds)
    assert large_seconds < limit, (
        f"{what}: {large_seconds:.4f} s at {SIZE_RATIO} times the input against {small_seconds:.4f} s at one "
        f"(limit {limit:.4f} s, {GROWTH_LIMIT:.1f} times; a linear scan takes about {LINEAR_RATIO} times, "
        f"a quadratic one about {QUADRATIC_RATIO})"
    )
