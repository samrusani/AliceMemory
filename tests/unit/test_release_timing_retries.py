"""Duration retries keep the limit and do a bounded amount of work."""

from unittest.mock import Mock

import pytest

from tests.support.timing import best_duration
from tests.unit import test_saved_quote_ref_reading as saved_quote


def test_absolute_limit_rejects_every_slow_sample() -> None:
    """Mutation: return no more than half the limit from best_duration."""
    sample = Mock(side_effect=[2.0, 1.5, 3.0])
    assert best_duration(sample, limit=1.0) == 1.5


def test_absolute_limit_accepts_a_later_fast_sample() -> None:
    """Mutation: keep the maximum duration after the first sample."""
    sample = Mock(side_effect=[5.0, 0.5, 3.0])
    assert best_duration(sample, limit=1.0) < 1.0


def test_absolute_limit_tries_at_most_three_samples() -> None:
    """Mutation: permit a fourth sample."""
    sample = Mock(return_value=1.0)
    assert best_duration(sample, limit=1.0) == 1.0
    assert sample.call_count == 3


def test_absolute_limit_stops_at_the_first_fast_sample() -> None:
    """Mutation: keep sampling after passing the limit."""
    sample = Mock(side_effect=[1.0, 0.9, 0.1])
    assert best_duration(sample, limit=1.0) == 0.9
    assert sample.call_count == 2


def _growth_check(monkeypatch, large_times: list[float]) -> Mock:
    durations = Mock(side_effect=[0.02, 0.02, 0.02, *large_times])
    monkeypatch.setattr(saved_quote, "_cpu_seconds", durations)
    return durations


def _assert_growth() -> None:
    saved_quote.assert_cost_grows_linearly(lambda: None, lambda: None, size_ratio=8, what="fixture")


def test_growth_limit_rejects_every_slow_sample(monkeypatch) -> None:
    """Mutation: remove the failing assertion from assert_cost_grows_linearly."""
    _growth_check(monkeypatch, [2.2, 1.2, 1.6])
    with pytest.raises(AssertionError, match=r"fixture: 1\.20 s on the larger input"):
        _assert_growth()


def test_growth_limit_recovers_after_a_far_over_sample(monkeypatch) -> None:
    """Mutation: restore the early exit above three times the limit."""
    _growth_check(monkeypatch, [2.2, 0.25])
    _assert_growth()


def test_growth_limit_tries_at_most_three_large_samples(monkeypatch) -> None:
    """Mutation: permit another large sample beyond the run limit."""
    durations = _growth_check(monkeypatch, [2.0, 1.9, 1.8, 1.7])
    with pytest.raises(AssertionError, match="fixture"):
        _assert_growth()
    assert durations.call_count == 6


def test_growth_limit_stops_at_the_first_fast_large_sample(monkeypatch) -> None:
    """Mutation: keep sampling after the first fast large sample."""
    durations = _growth_check(monkeypatch, [0.7, 0.25, 0.1])
    _assert_growth()
    assert durations.call_count == 5
