"""Pass or fail a read budget against the merge base, and record the pre-labels comparison.

The required jobs compare this checkout with its merge base with main
(``ALICE_READ_MERGE_BASE_CHECKOUT``), measured on the same runner. The head
passes when the measured time is at most 1.25 times the merge base plus 0.05
seconds.

That tolerance comes from the evidence of the required job on main at
b0cee54e (artifact ``round3-paired-read-budgets-0-attempt-1``). For pack and
recall, the gap from the minimum sample to the median sample on the head was
1.9 percent at the median and 8.2 percent at the worst SQLite row, and 4.2
percent at the median and 14.1 percent at the worst PostgreSQL row. The same
artifact has several SQLite pack and recall rows whose minimum is within a
few hundredths of twice the pre-labels minimum. The runs named in the
2026-10-12 close-out moved by 0.01 to 0.02 seconds across that line (a
keyless pack of 0.322 to 0.338 seconds against a gate of 0.314 to 0.323
seconds, and a recall of 0.216 seconds against 0.214 seconds). 1.25 times
plus 0.05 seconds leaves that noise inside the gate. A stall of 0.2 seconds
fails while the merge base is under 0.6 seconds, which covers these fixtures.
A move of 0.02 seconds passes on a read of 0.32 seconds and on a read of
0.05 seconds.

The handoff budgets (``test_label_read_handoff_budget.py`` and its PostgreSQL
twin) read ten interleaved samples a revision through ``paired_summary`` and
``paired_over_merge_base``. On the shared runners the noise is time a read
spends off the processor. In run 39746461326 (the round-two SQLite budget,
attempt 1) every head recall took 0.18 to 0.30 seconds of wall time and 0.055
to 0.058 seconds of CPU time, where a quiet sample took 0.059 seconds of wall
time. A SQLite read also commits writes to the vault file, so a slow disk
flush can add wall time as well. CPU time counts neither, so the median CPU
time is the comparison for the work a read does, and it fails a slowdown in
most of the calls, as the wall median did. It cannot see a read that waits (a
sleep, a lock, a flush, a child process), so the smallest wall time is
compared too. Noise only adds time and has to reach every sample to move the
smallest, while a wait in every call raises every sample. A wait in only some
of the calls passes. The handoff budgets compared the median wall time of five
samples before, and in 100 runs of the required SQLite job (2026-10-10 to
2026-10-24) that failed three times with no change to the read path (run
40035434435, and run 40078278634 in both attempts): three of the five head
samples took 0.10 to 0.25 seconds where the quiet ones took 0.042 to 0.046. The
round-three budgets compare the smallest wall and CPU time of ten samples and
did not fail in those 100 runs.

The comparison with the pre-labels commit (``ALICE_READ_MAIN_CHECKOUT``,
pinned at 48873b03) is a ratio only when that variable is set. The required
jobs do not set it. The maximum-workload smoke job does, and it is not a gate.
The absolute wall limit is a separate check and is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import statistics

MERGE_BASE_ENV = "ALICE_READ_MERGE_BASE_CHECKOUT"
PRELABELS_ENV = "ALICE_READ_MAIN_CHECKOUT"
MERGE_BASE_FACTOR = 1.25
MERGE_BASE_SLACK_SECONDS = 0.05
# The handoff budgets: measured calls a revision and action, and the statistics of them the gate compares.
PAIRED_SAMPLES = 10
PAIRED_STATISTICS = ("median_cpu", "minimum_wall")


def merge_base_checkout() -> str:
    """The checkout the gate measures against, or an assertion naming the missing variable."""

    checkout = os.environ.get(MERGE_BASE_ENV, "").strip()
    if not checkout:
        raise AssertionError("read budgets require the merge-base checkout (" + MERGE_BASE_ENV + ")")
    return checkout


def prelabels_checkout() -> str | None:
    """The pre-labels checkout, when the job asked for the recorded comparison."""

    checkout = os.environ.get(PRELABELS_ENV, "").strip()
    return checkout or None


def over_merge_base(head: float, base: float) -> bool:
    """True when ``head`` is past 1.25 times ``base`` plus 0.05 seconds.

    The boundary itself passes: a time equal to the limit is not a failure.
    """

    return head > MERGE_BASE_FACTOR * base + MERGE_BASE_SLACK_SECONDS


def paired_summary(walls: list[float], cpus: list[float]) -> dict[str, object]:
    """One revision's samples of one action and the statistics ``paired_over_merge_base`` reads.

    ``median`` is the median wall time and ``samples`` the wall samples, the names the evidence rows had before
    CPU time was recorded.
    """

    return {
        "median": statistics.median(walls),
        "samples": walls,
        "minimum_wall": min(walls),
        "cpu": cpus,
        "median_cpu": statistics.median(cpus),
    }


def paired_over_merge_base(head: dict, base: dict) -> list[str]:
    """The statistics of ``PAIRED_STATISTICS`` on which ``head`` is past the limit of ``base``. Empty passes."""

    return [name for name in PAIRED_STATISTICS if over_merge_base(head[name], base[name])]


def ratio(head: float, base: float) -> float | None:
    """``head / base``, or ``None`` when the base time is zero."""

    if base == 0:
        return None
    return head / base


def prelabels_action(head: float, prelabels: float) -> dict[str, float | None | bool]:
    """One recorded comparison. ``gated`` is false: nothing here fails the job."""

    return {"head": head, "prelabels": prelabels, "ratio": ratio(head, prelabels), "gated": False}


def append_evidence(row: dict) -> None:
    """Append one JSON object to ``ALICE_READ_BUDGET_EVIDENCE``, when that variable is set."""

    evidence_path = os.environ.get("ALICE_READ_BUDGET_EVIDENCE", "").strip()
    if not evidence_path:
        return
    evidence = Path(evidence_path)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row) + "\n")
