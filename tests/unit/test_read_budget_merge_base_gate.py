"""The merge-base read-budget gate fails a stalled read and passes an unchanged one.

The numbers are the noise measured on the required job, not a second run of that job.
A head that sleeps 0.2 seconds past a 0.10 second merge base fails. Ten heads that move
by at most 0.02 seconds, which is the run-to-run move that flipped the old gate, pass.

The handoff budgets compare the median CPU time and the smallest wall time of ten calls a
revision (``paired_over_merge_base``). The wall samples of the three handoff runs that failed
with no change to the read path pass it. A wait in every call fails on the wall time, and more
work in most of the calls fails on the CPU time.
"""

import statistics

import pytest

from tests.performance.read_budget_gate import (
    MERGE_BASE_FACTOR,
    MERGE_BASE_SLACK_SECONDS,
    PAIRED_SAMPLES,
    over_merge_base,
    paired_over_merge_base,
    paired_summary,
)


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


# Wall samples of the handoff budget on the required SQLite job, merge base then head, in the order taken, to a tenth of
# a millisecond. These are the failed runs named in read_budget_gate; the read path did not change in any of them.
CI_NOISE = {
    "run 40078278634 attempt 1, recall": ([0.0577, 0.0421, 0.0415, 0.0636, 0.0418], [0.1057, 0.0418, 0.0421, 0.1212, 0.1650]),
    "run 40078278634 attempt 2, pack": ([0.1564, 0.0542, 0.0455, 0.0465, 0.0447], [0.0459, 0.1377, 0.1083, 0.1340, 0.0458]),
    "run 40035434435, pack": ([0.0456, 0.0454, 0.2199, 0.0758, 0.0445], [0.1839, 0.0452, 0.2499, 0.1996, 0.0452]),
}


@pytest.mark.parametrize("run", sorted(CI_NOISE))
def test_the_noise_that_failed_the_wall_median_passes_the_paired_gate(run):
    """The median wall time failed each of these runs. The smallest wall time passes each.

    The probe did not record CPU time then, so the CPU samples here are the same on both sides and only the wall
    comparison is under test.

    Mutation: give the median wall time as ``minimum_wall`` in ``paired_summary``.
    """

    base_walls, head_walls = CI_NOISE[run]
    assert over_merge_base(statistics.median(head_walls), statistics.median(base_walls))
    cpus = [0.04] * len(head_walls)
    assert paired_over_merge_base(paired_summary(head_walls, cpus), paired_summary(base_walls, cpus)) == []


def test_a_wait_in_every_call_fails_on_the_wall_time():
    """0.08 seconds of waiting in every call of a 0.045 second read: 0.125 seconds against a limit of 0.106.

    The CPU time is the merge base's, and noise reaches nine of the ten merge-base samples and half of the head's.

    Mutation: drop ``minimum_wall`` from ``PAIRED_STATISTICS``.
    """

    cpus = [0.044] * PAIRED_SAMPLES
    base = paired_summary([0.045] + [0.2] * (PAIRED_SAMPLES - 1), cpus)
    head = paired_summary([0.125, 0.3] * (PAIRED_SAMPLES // 2), cpus)
    assert paired_over_merge_base(head, base) == ["minimum_wall"]


def test_more_work_fails_on_the_cpu_time_also_when_some_calls_are_quick():
    """Three times the work in six of ten calls fails on the median CPU time; the smallest wall time is a quick call's.

    The same work in every call fails on both.

    Mutations: give the smallest CPU time as ``median_cpu`` in ``paired_summary``; drop ``median_cpu`` from
    ``PAIRED_STATISTICS``.
    """

    base = paired_summary([0.046] * PAIRED_SAMPLES, [0.044] * PAIRED_SAMPLES)
    cpus = [0.132, 0.044, 0.132, 0.132, 0.044, 0.132, 0.044, 0.132, 0.132, 0.044]
    assert len(cpus) == PAIRED_SAMPLES
    head = paired_summary([cpu + 0.002 for cpu in cpus], cpus)
    assert paired_over_merge_base(head, base) == ["median_cpu"]
    every = paired_summary([0.134] * PAIRED_SAMPLES, [0.132] * PAIRED_SAMPLES)
    assert paired_over_merge_base(every, base) == ["median_cpu", "minimum_wall"]


def test_an_unchanged_head_passes_the_paired_gate():
    """The same samples on both sides pass, and a CPU time just past the limit fails.

    Mutation: make ``paired_over_merge_base`` name every statistic.
    """

    walls, cpus = [0.045] * PAIRED_SAMPLES, [0.044] * PAIRED_SAMPLES
    assert paired_over_merge_base(paired_summary(walls, cpus), paired_summary(walls, cpus)) == []
    limit = MERGE_BASE_FACTOR * 0.044 + MERGE_BASE_SLACK_SECONDS
    over = paired_summary(walls, [limit + 0.000001] * PAIRED_SAMPLES)
    assert paired_over_merge_base(over, paired_summary(walls, cpus)) == ["median_cpu"]
