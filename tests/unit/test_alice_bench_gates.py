"""The gate arithmetic and the lock (scripts/alice_bench_gates.py and gates.json).

The thresholds of the search-quality gates are written down before any held-out number
exists, in ``gates.json``, and chosen by running this arithmetic, not by looking at a result.
The script computes the exact chance that each gate passes for an assumed rate of improved and
regressed questions. These tests pin two rows of the spec's table, pin the thresholds, and check
the CI time budget against the workflow it describes.

Every test names the mutation that must fail it. Test ids (TH15, ...) follow the spec of the
search-quality release.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest

import scripts.alice_bench_gates as gate_sim

REPO_ROOT = Path(__file__).resolve().parents[2]
GATES = REPO_ROOT / "gates.json"


def _thresholds() -> dict[str, object]:
    return gate_sim.load_thresholds(GATES)


def test_the_simulation_pins_two_rows_of_the_spec_table() -> None:
    """TH15. The exact pass rates for a no-change truth and for a real gain.

    The spec's table came from a sampled simulation and is rounded to whole percent. These are the
    exact values for the same rules, and each is within a point of the spec's figure.

    Mutation: change a threshold the script reads (``net_min`` of G1 or G2, or ``p_max``), or the
    corpus split, in ``gates.json``, or hardcode another one in the script. A pinned rate moves.
    """

    thresholds = _thresholds()
    null = gate_sim.pass_rates(0.10, 0.10, thresholds)
    gain = gate_sim.pass_rates(0.12, 0.04, thresholds)
    assert null["g1_verbatim"] == pytest.approx(0.032778, abs=2e-6)
    assert null["tripwire"] == pytest.approx(0.966522, abs=2e-6)
    assert null["g2_keyword_gain"] == pytest.approx(0.033478, abs=2e-6)
    assert gain["g1_verbatim"] == pytest.approx(0.640159, abs=2e-6)
    assert gain["tripwire"] == pytest.approx(0.999979, abs=2e-6)
    assert gain["g2_keyword_gain"] == pytest.approx(0.656089, abs=2e-6)
    # The spec's own, rounded figures for the same rows: 3%, 97%, 3% and 64%, 100%, 66%.
    assert round(null["g1_verbatim"] * 100) == 3 and round(null["tripwire"] * 100) == 97
    assert round(gain["g1_verbatim"] * 100) == 64 and round(gain["g2_keyword_gain"] * 100) == 66


def test_a_harm_is_caught_by_the_tripwire_at_the_rates_the_spec_states() -> None:
    """TH15. A truly worse change usually fails the tripwire, and never passes the gain gates.

    Mutation: make ``tripwire_fails`` ignore the sign test (fail on any regression excess), or
    treat a gain gate as a tripwire. The harm rows would move to 0% or 100%.
    """

    thresholds = _thresholds()
    harm5 = gate_sim.pass_rates(0.08, 0.12, thresholds)
    harm12 = gate_sim.pass_rates(0.05, 0.15, thresholds)
    assert harm5["tripwire"] == pytest.approx(0.804081, abs=2e-6)
    assert harm12["tripwire"] == pytest.approx(0.243995, abs=2e-6)
    assert harm5["g1_verbatim"] < 0.01 and harm12["g2_keyword_gain"] < 0.001


def test_the_building_blocks_of_the_simulation_are_exact() -> None:
    """The sign test, the multinomial and the two rule shapes, on cases that can be done by hand.

    Mutation: make ``sign_test_p`` count ``count`` or fewer (the wrong tail), or make
    ``gain_passes`` ignore the net minimum.
    """

    assert gate_sim.sign_test_p(8, 8) == pytest.approx(1 / 256)
    assert gate_sim.sign_test_p(5, 8) == pytest.approx(93 / 256)
    assert gate_sim.sign_test_p(0, 0) == 1.0
    distribution = gate_sim.outcome_distribution(3, 0.2, 0.3)
    assert sum(distribution.values()) == pytest.approx(1.0)
    assert distribution[(1, 1)] == pytest.approx(6 * 0.2 * 0.3 * 0.5)
    assert gate_sim.gain_passes(12, 4, net_min=8, p_max=0.05) is True
    assert gate_sim.gain_passes(11, 4, net_min=8, p_max=0.05) is False, "net 7 is below the minimum"
    assert gate_sim.gain_passes(7, 0, net_min=8, p_max=0.05) is False, "p is 1/128, but net 7 is below the minimum"
    assert gate_sim.gain_passes(8, 0, net_min=8, p_max=0.05) is True
    assert gate_sim.gain_passes(8, 0, net_min=8, p_max=0.0001) is False, "p of 1/256 is not below 0.0001"
    assert gate_sim.tripwire_fails(3, 15, p_max=0.05) is True
    assert gate_sim.tripwire_fails(15, 3, p_max=0.05) is False
    with pytest.raises(ValueError):
        gate_sim.outcome_distribution(10, 0.7, 0.7)


def test_the_command_prints_the_table_for_an_assumed_flip_rate() -> None:
    """TH15. ``--improve`` and ``--regress`` print one row, and the default prints the spec's ten.

    Mutation: ignore ``--improve`` and always print the default table.
    """

    one = io.StringIO()
    with contextlib.redirect_stdout(one):
        assert gate_sim.main(["--improve", "0.12", "--regress", "0.04"]) == 0
    lines = one.getvalue().strip().splitlines()
    assert len(lines) == 3 and "12.0% improve, 4.0% regress" in lines[2]
    assert re.search(r"\b64\.0%", lines[2]) and "G1 verbatim" in lines[1]
    with pytest.raises(SystemExit):
        gate_sim.main(["--improve", "0.1"])


def test_the_thresholds_are_the_ones_the_spec_locks() -> None:
    """The numbers of spec 9.5 and 4.8, as they sit in gates.json.

    Mutation: change any threshold in ``gates.json``. A change after the lock is a new commit that
    says why, and this test is where it has to be said.
    """

    data = _thresholds()
    gates = data["gates"]
    assert isinstance(gates, dict)
    assert gates["G1"]["verbatim"] == {"kind": "gain", "net_min": 8, "p_max": 0.05, "neither_corpus_net_negative": True}
    assert gates["G1"]["keyword"]["kind"] == "tripwire" and gates["G1"]["keyword"]["p_max"] == 0.05
    assert gates["G2"]["keyword"] == {"kind": "gain", "net_min": 4, "p_max": 0.05}
    assert gates["G2"]["verbatim"]["kind"] == "tripwire" and gates["G2"]["verbatim"]["p_max"] == 0.05
    assert (gates["G3"]["net_min"], gates["G3"]["p_max"]) == (6, 0.05)
    assert gates["G4"]["kind"] == "tripwire" and gates["G4"]["z"] == 1.645
    assert data["tier1"]["byte_budgets"] == [4096, 8192] and data["tier1"]["gate_budget_bytes"] == 8192
    assert data["tier1"]["anchor_min_length"] == 12 and data["tier1"]["anchor_max_files"] == 3
    assert data["search"]["grep_cap_bytes"] == 16384 and data["search"]["searches_per_run"] == 3
    assert data["search"]["recall_defaults"] == {"limit": 8, "context_depth": "low", "include_sources": True}
    assert data["passage_cap_rule"]["caps"] == [2, 3, 4]
    assert data["p1b_arm_rule"]["beats_plain_by_questions"] == 3
    assert data["sample"]["per_corpus"] == [60, 60] and sum(data["sample"]["per_corpus"]) == data["sample"]["heldout_questions"]
    assert data["import_orders"] == ["sorted", "reverse", "shuffle:20261002"]
    counts = data["run_counts"]
    assert counts["baseline"]["answering_runs"] == sum(counts["baseline"]["arms"].values()) == 720
    assert counts["candidate"]["answering_runs"] == sum(counts["candidate"]["arms"].values()) == 840
    assert counts["baseline"]["judge_calls"] == counts["candidate"]["judge_calls"] == 184


def test_the_hash_fields_are_empty_on_purpose_and_say_who_fills_them() -> None:
    """The prompt, scorer and question-set hashes are the tower's to fill, with a comment saying so.

    Mutation: put a made-up hash in one of these fields, or remove the comment. A hash nobody
    computed would look like a lock.
    """

    hashes = _thresholds()["hashes"]
    assert isinstance(hashes, dict)
    assert "tower fills each field" in hashes["_comment"]
    assert hashes["scorer_sha256"] is None
    assert set(hashes["prompts"]) == {"alice_arm", "grep_arm", "no_search", "judge"}
    assert all(value is None for value in hashes["prompts"].values())
    assert set(hashes["question_sets"]) == {"heldout_wiki", "heldout_docs"}
    assert all(value is None for value in hashes["question_sets"].values())
    assert "holds no question" in _thresholds()["_comment"]

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {name for child in node.values() for name in keys(child)}
        if isinstance(node, list):
            return {name for child in node for name in keys(child)}
        return set()

    assert not keys(_thresholds()) & {"question", "questions", "gold", "anchors", "facts"}, "gates.json holds no question"


def test_the_ci_time_budget_matches_the_workflow_it_describes() -> None:
    """13.2. The budget for the new tests, measured against the jobs that run them.

    The single unit job had a 20 minute limit and a measurement of 70 successful runs on 2026-10-02
    and 2026-10-03 put it at a median of 13.9 and a maximum of 17.8 minutes, past the 17 minute line.
    The job is now three parallel shard jobs with the same limit and line, behind a summary job that
    keeps the old name, the required status check. The tests of this release may add 90 seconds, and
    the shards are split again before another test lands if a shard passes 17 minutes.

    Mutation: change ``timeout-minutes`` of the shard job in the workflow (the budget then describes
    a different job), change the shard count or the required check name in the workflow alone, record
    a split threshold at or above the timeout, write the pre-split sample as fewer runs than it was
    taken over, record a shard measurement at or above the split line, or record a count of tests or
    files of the harness (it would go stale with the next test: the entry holds the measured duration
    and its note only).
    """

    import yaml

    budget = _thresholds()["ci_time"]
    assert isinstance(budget, dict)
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    shard_job = workflow["jobs"]["python-unit-shards"]
    summary_job = workflow["jobs"]["python-unit"]
    assert shard_job["timeout-minutes"] == budget["timeout_minutes"] == 20
    assert budget["shard_count"] == len(shard_job["strategy"]["matrix"]["include"]) == 3
    assert budget["required_check"] == summary_job["name"] == "Unit tests + live eval battery (SQLite)"
    assert budget["job"].startswith("python-unit-shards ") and "shard N of 3" in budget["job"]
    assert budget["split_threshold_minutes"] == 17 < budget["timeout_minutes"]
    assert budget["new_test_budget_seconds"] == 90
    measured = budget["measured_seconds"]
    assert 0 < measured["min"] <= measured["median"] <= measured["max"] < budget["split_threshold_minutes"] * 60
    assert budget["measured_runs"] >= 1
    assert "longest of the three shard jobs" in budget["measured_scope"]
    before = budget["before_split"]
    assert before["job"] == "python-unit (Unit tests + live eval battery, SQLite)"
    assert before["measured_runs"] == 70
    before_seconds = before["measured_seconds"]
    assert (before_seconds["min"], before_seconds["median"], before_seconds["max"]) == (482, 836, 1070)
    assert before_seconds["max"] >= budget["split_threshold_minutes"] * 60, "the sample is why the job was split"
    assert "every successful run" in before["measured_scope"] and "2026-10-02" in before["measured_scope"]
    assert budget["p0a_tests"]["seconds_measured_locally"] < budget["new_test_budget_seconds"]
    assert set(budget["p0a_tests"]) == {"seconds_measured_locally", "note"}, "no count of tests or files that can go stale"
    assert "new_test_budget_seconds" in budget["p0a_tests"]["note"] and "count" in budget["p0a_tests"]["note"]
    assert "split_threshold_minutes" in budget["rule"] and "new_test_budget_seconds" in budget["rule"]


def test_a_file_that_is_not_a_gates_file_is_refused_and_the_properties_read_the_locked_numbers(tmp_path: Path) -> None:
    """The harness reads its numbers through ``Gates`` and refuses a file of another kind.

    Mutation: drop the schema check in ``load_gates``, or read a property from the wrong key (the
    budgets from the grep cap, the searches per run from the budgets).
    """

    import scripts.alice_bench as bench

    wrong = tmp_path / "gates.json"
    wrong.write_text(json.dumps({"schema": "something-else/1"}), encoding="utf-8")
    with pytest.raises(bench.BenchError, match="not an alice-bench-gates/1 file"):
        bench.load_gates(wrong)
    gates = bench.load_gates(GATES)
    assert gates.path == GATES and len(gates.sha256) == 64
    assert gates.budgets == (4096, 8192) and gates.gate_budget == 8192
    assert (gates.searches_per_run, gates.grep_cap) == (3, 16384)
    assert (gates.anchor_min_length, gates.anchor_max_files) == (12, 3)
    assert gates.import_orders == ("sorted", "reverse", "shuffle:20261002")
    assert gates.prompt_hashes == {"alice_arm": None, "grep_arm": None, "no_search": None, "judge": None}
