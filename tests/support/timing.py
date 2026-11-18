"""Sample a test's timed work again when the clock reports a slow run."""

from collections.abc import Callable
from math import inf


def best_duration(sample: Callable[[], float], *, limit: float) -> float:
    """Return the shortest duration from up to three equivalent runs."""
    best = inf
    for _ in range(3):
        best = min(best, sample())
        if best < limit:
            break
    return best
