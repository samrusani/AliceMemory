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

The comparison with the pre-labels commit (``ALICE_READ_MAIN_CHECKOUT``,
pinned at 48873b03) is a ratio only when that variable is set. The required
jobs do not set it. The maximum-workload smoke job does, and it is not a gate.
The absolute wall limit is a separate check and is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MERGE_BASE_ENV = "ALICE_READ_MERGE_BASE_CHECKOUT"
PRELABELS_ENV = "ALICE_READ_MAIN_CHECKOUT"
MERGE_BASE_FACTOR = 1.25
MERGE_BASE_SLACK_SECONDS = 0.05


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
