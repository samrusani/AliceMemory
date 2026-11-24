"""Check the paired budget verdict without a clock or a database."""

from tests.support.closest_lessons_budget import budget_decision


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
