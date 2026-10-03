#!/usr/bin/env python3
"""alice_bench_gates: how often each search-quality gate passes, for an assumed flip rate.

The gate thresholds in ``gates.json`` were chosen by running this arithmetic, not
by looking at a result. Give it the share of questions a change would improve and
the share it would regress (questions flip independently), and it prints the exact
probability that each gate passes at the held-out sample size, 60 questions on each
of two corpora. A reviewer can rerun it with any other assumption.

Exact arithmetic, no sampling: the number of improved and regressed questions on a
corpus is multinomial, so every outcome is enumerated and the pass rates carry no
simulation noise.

The assumed rates are guesses until the first candidate runs. This script is a
calibration of the rules, not a prediction of any result.

Usage:
    python scripts/alice_bench_gates.py                        # the table of the spec
    python scripts/alice_bench_gates.py --improve 0.12 --regress 0.04
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from math import comb
from pathlib import Path
from typing import Any

DEFAULT_GATES_PATH = Path(__file__).resolve().parents[1] / "gates.json"

# (label, share improved, share regressed): the assumed truths of the spec table.
DEFAULT_ROWS: tuple[tuple[str, float, float], ...] = (
    ("no change, 10% improve and 10% regress", 0.10, 0.10),
    ("no change, 4% each way", 0.04, 0.04),
    ("no change, 15% each way", 0.15, 0.15),
    ("+8 net on average (10% and 3%)", 0.10, 0.03),
    ("+10 net (12% and 4%)", 0.12, 0.04),
    ("+12 net (12% and 2%)", 0.12, 0.02),
    ("+14 net (15% and 3%)", 0.15, 0.03),
    ("harm of 5 (8% and 12%)", 0.08, 0.12),
    ("harm of 10 (6% and 14%)", 0.06, 0.14),
    ("harm of 12 (5% and 15%)", 0.05, 0.15),
)

Outcome = tuple[int, int]
Distribution = dict[Outcome, float]


def load_thresholds(path: Path | None = None) -> dict[str, Any]:
    """``gates.json`` as parsed. The thresholds are read from here and nowhere else."""

    target = DEFAULT_GATES_PATH if path is None else path
    data = json.loads(target.read_text(encoding="utf-8"))
    if data.get("schema") != "alice-bench-gates/1":
        raise ValueError(f"{target.name} is not an alice-bench-gates/1 file")
    return dict(data)


def sign_test_p(count: int, discordant: int) -> float:
    """One-sided exact p: the chance of ``count`` or more of ``discordant`` pairs on one side."""

    if discordant == 0:
        return 1.0
    return sum(comb(discordant, j) for j in range(count, discordant + 1)) / 2**discordant


def outcome_distribution(n: int, improve: float, regress: float) -> Distribution:
    """P(improved = i, regressed = r) for ``n`` questions that flip independently."""

    if improve < 0 or regress < 0 or improve + regress > 1:
        raise ValueError("improve and regress must be shares that add up to at most 1")
    same = 1.0 - improve - regress
    distribution: Distribution = {}
    for i in range(n + 1):
        for r in range(n + 1 - i):
            pmf = comb(n, i) * comb(n - i, r) * improve**i * regress**r * same ** (n - i - r)
            distribution[(i, r)] = pmf
    return distribution


def convolve(first: Mapping[Outcome, float], second: Mapping[Outcome, float]) -> Distribution:
    total: Distribution = {}
    for (i1, r1), p1 in first.items():
        for (i2, r2), p2 in second.items():
            key = (i1 + i2, r1 + r2)
            total[key] = total.get(key, 0.0) + p1 * p2
    return total


def gain_passes(improved: int, regressed: int, *, net_min: int, p_max: float) -> bool:
    return improved - regressed >= net_min and sign_test_p(improved, improved + regressed) < p_max


def tripwire_fails(improved: int, regressed: int, *, p_max: float) -> bool:
    """A tripwire fails only when regressions exceed improvements and the sign test says so."""

    return regressed > improved and sign_test_p(regressed, improved + regressed) < p_max


def pass_rates(improve: float, regress: float, thresholds: Mapping[str, Any]) -> dict[str, float]:
    """The exact pass rate of each gate part for one assumed truth.

    ``g1_verbatim``: G1's verbatim part (a gain, and neither corpus net negative).
    ``tripwire``: the tripwires of G1 (keyword) and G2 (verbatim), which are the same rule.
    ``g2_keyword_gain``: G2's keyword part.
    """

    g1 = thresholds["gates"]["G1"]["verbatim"]
    g2 = thresholds["gates"]["G2"]["keyword"]
    trip = thresholds["gates"]["G2"]["verbatim"]
    corpora = [int(size) for size in thresholds["sample"]["per_corpus"]]
    per_corpus = [outcome_distribution(size, improve, regress) for size in corpora]

    pooled: Distribution = per_corpus[0]
    for distribution in per_corpus[1:]:
        pooled = convolve(pooled, distribution)

    if g1.get("neither_corpus_net_negative"):
        restricted = [{key: p for key, p in dist.items() if key[0] >= key[1]} for dist in per_corpus]
        restricted_pooled = restricted[0]
        for dist in restricted[1:]:
            restricted_pooled = convolve(restricted_pooled, dist)
    else:
        restricted_pooled = pooled

    g1_rate = sum(
        p
        for (i, r), p in restricted_pooled.items()
        if gain_passes(i, r, net_min=int(g1["net_min"]), p_max=float(g1["p_max"]))
    )
    g2_rate = sum(
        p
        for (i, r), p in pooled.items()
        if gain_passes(i, r, net_min=int(g2["net_min"]), p_max=float(g2["p_max"]))
    )
    trip_rate = sum(
        p for (i, r), p in pooled.items() if not tripwire_fails(i, r, p_max=float(trip["p_max"]))
    )
    return {"g1_verbatim": g1_rate, "tripwire": trip_rate, "g2_keyword_gain": g2_rate}


def format_table(rows: Sequence[tuple[str, float, float]], thresholds: Mapping[str, Any]) -> str:
    sizes = thresholds["sample"]["per_corpus"]
    lines = [
        f"Exact pass rates, {sum(int(s) for s in sizes)} questions ({' and '.join(str(s) for s in sizes)} per corpus), "
        "independent flips",
        f"{'assumed truth':42} {'G1 verbatim':>12} {'tripwire':>9} {'G2 keyword gain':>16}",
    ]
    for label, improve, regress in rows:
        rates = pass_rates(improve, regress, thresholds)
        lines.append(
            f"{label:42} {rates['g1_verbatim']:>11.1%} {rates['tripwire']:>9.1%} {rates['g2_keyword_gain']:>16.1%}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exact pass rates of the search-quality gates.")
    parser.add_argument("--improve", type=float, default=None, help="Share of questions a change improves.")
    parser.add_argument("--regress", type=float, default=None, help="Share of questions a change regresses.")
    parser.add_argument("--gates", default=None, help="Path of gates.json.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if (args.improve is None) != (args.regress is None):
        parser.error("give --improve and --regress together")
    thresholds = load_thresholds(Path(args.gates) if args.gates else None)
    if args.improve is None:
        rows: tuple[tuple[str, float, float], ...] = DEFAULT_ROWS
    else:
        rows = ((f"assumed: {args.improve:.1%} improve, {args.regress:.1%} regress", args.improve, args.regress),)
    print(format_table(rows, thresholds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
