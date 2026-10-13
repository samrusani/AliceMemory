"""The merge-base read-budget gate fails a stalled read and passes an unchanged one.

The numbers are the noise measured on the required job, not a second run of that job.
A head that sleeps 0.2 seconds past a 0.10 second merge base fails. Ten heads that move
by at most 0.02 seconds, which is the run-to-run move that flipped the old gate, pass.
"""

from tests.performance.read_budget_gate import MERGE_BASE_FACTOR, MERGE_BASE_SLACK_SECONDS, over_merge_base


def test_a_slowed_read_fails_the_merge_base_gate():
    """0.2 seconds of sleep on a read that took 0.10 seconds at the merge base.

    Mutation: set ``MERGE_BASE_FACTOR`` to 100, or make ``over_merge_base`` return false.
    """

    base = 0.10
    slept = base + 0.20
    assert slept > MERGE_BASE_FACTOR * base + MERGE_BASE_SLACK_SECONDS
    assert over_merge_base(slept, base)
    assert not over_merge_base(MERGE_BASE_FACTOR * base + MERGE_BASE_SLACK_SECONDS, base)


def test_ten_unchanged_heads_pass_the_merge_base_gate():
    """Ten samples of an unchanged head, moved by the noise that flipped the old gate, all pass.

    Mutation: make ``over_merge_base`` return true for every pair.
    """

    base = 0.32
    shifts = (0.016, -0.012, 0.020, -0.008, 0.011, 0.004, -0.019, 0.015, 0.0, 0.009)
    assert len(shifts) == 10
    for shift in shifts:
        assert not over_merge_base(base + shift, base), shift
    # The same noise on a short read, where 0.02 seconds is a large fraction of the time.
    short = 0.05
    for shift in shifts:
        assert not over_merge_base(short + abs(shift), short), shift
