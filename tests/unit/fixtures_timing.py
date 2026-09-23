"""Time budgets for the linear-time tests, allowing for a coverage tracer.

Why (S4 CI, 2026-09-23). CI runs the unit suite under coverage, which traces
every Python line. There the re-implemented v0.16.0 check took 2.55 s and
2.73 s on two 200 KB shapes of test_legacy_credential_check.py, against a
2 s budget it meets in at most 0.43 s untraced. Run locally under coverage,
the same shapes took at most 1.04 s and four times the input still cost 3.9
to 4.0 times the time: the tracer adds a constant factor, not a power of the
input.

So a linear-time test keeps its ratio assertion (time at four times the
input against time at one), which a tracer does not change, and only its
absolute budget scales, by TRACED_SLOWDOWN, while a tracer is active.
Untraced, the budget is unchanged. A quadratic scan still fails the scaled
budget: the old assignment pattern took 43 s on 50 KB untraced.
"""

from __future__ import annotations

import sys

TRACED_SLOWDOWN = 5


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
