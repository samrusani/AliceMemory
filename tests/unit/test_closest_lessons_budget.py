"""Check the paired budget verdict without a clock or a database."""

import pytest

from tests.support.closest_lessons_budget import _require_paired_budget, budget_decision


def test_the_budget_uses_the_lowest_base_and_head_across_fresh_rounds() -> None:
    """Mutation: compare the lowest head median against the highest base median."""
    assert budget_decision([(50.0, 53.0)], 3.0) == "pass"

    head_recovers = [(50.0, 80.0), (51.0, 50.0)]
    assert budget_decision(head_recovers[:1], 3.0) == "measure again"
    assert budget_decision(head_recovers, 3.0) == "pass"

    head_stays_slow = [(50.0, 60.0), (51.0, 61.0), (49.0, 59.0)]
    assert budget_decision(head_stays_slow[:2], 3.0) == "measure again"
    assert budget_decision(head_stays_slow, 3.0) == "fail"

    base_varies = [(60.0, 71.0), (50.0, 56.0), (59.0, 57.0)]
    assert budget_decision(base_varies, 3.0) == "fail"


@pytest.mark.parametrize("rounds", [
    pytest.param([(50.0, 53.0)], id="first"),
    pytest.param([(50.0, 60.0), (51.0, 61.0), (49.0, 52.0)], id="third"),
])
def test_the_paired_budget_returns_on_a_passing_round(
    rounds: list[tuple[float, float]], capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: return when the verdict is measure again."""
    measurements = iter(rounds)
    _require_paired_budget(lambda: next(measurements), samples=7, db_type="sqlite", case="lessons", limit=3)
    assert next(measurements, None) is None
    assert capsys.readouterr().out.count("CLOSEST_BUDGET ") == len(rounds)


def test_the_paired_budget_reports_all_three_rounds_if_the_head_stays_slow(capsys: pytest.CaptureFixture[str]) -> None:
    """Mutations: measure only twice or remove the failure after the loop."""
    rounds = [(50.0, 60.0), (51.0, 61.0), (49.0, 59.0)]
    measurements = iter(rounds)
    with pytest.raises(AssertionError) as exc:
        _require_paired_budget(lambda: next(measurements), samples=7, db_type="sqlite", case="lessons", limit=3)
    assert exc.value.args == (("sqlite", "lessons", rounds, 3),)
    assert capsys.readouterr().out.count("CLOSEST_BUDGET ") == 3
