"""Diagnostics retain useful evidence without becoming benchmark samples."""

import io
import json
from pathlib import Path
import pstats
import subprocess
from types import SimpleNamespace
import threading

import pytest
import yaml

from tests.performance import round3_budget_probe as probe
from tests.performance import test_label_round3_read_budget as budgets


READ_LIMIT = budgets.READ_LIMIT
REVISIONS = {"main": "a" * 40, "head": "b" * 40}
KEYS = {"trusted_local_agent": "synthetic-credential", "admin_agent": "synthetic-credential"}


def fake_children(monkeypatch, tmp_path, *, gate=None, capture_status="complete", timings=None, returned=None):
    events = []
    evidence = tmp_path / "samples.jsonl"
    monkeypatch.setenv("ALICE_READ_MAIN_CHECKOUT", "paired-main")
    monkeypatch.setenv("ALICE_READ_BUDGET_EVIDENCE", str(evidence))
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_GATE", "1")
    monkeypatch.setattr(budgets.subprocess, "check_output", lambda args, **kwargs: REVISIONS["main" if args[2] == "paired-main" else "head"])

    class Child:
        def __init__(self, args, **kwargs):
            self.revision = "main" if args[2] == "paired-main" else "head"
            self.profile = args[-2]
            self.commands = []
            self.stdin = self.stdout = self.stderr = self
            self.stopped = False

        def write(self, command):
            action = command.strip()
            events.append(("measurement", self.profile, self.revision, action))
            self.commands.append(action)
            self.stopped = action == "stop"

        def flush(self):
            pass

        def readline(self):
            action = self.commands.pop(0)
            wall = cpu = .1
            if self.revision == "head":
                if gate == "absolute" and action == "workspace":
                    wall = 1.01
                if gate == "relative" and action == "pack":
                    wall = cpu = .31
            if timings is not None:
                wall, cpu = timings(self.revision, self.profile, action)
            count = READ_LIMIT if returned is None else returned(self.revision, self.profile, action)
            return json.dumps({"wall": wall, "cpu": cpu, "memories": count}) + "\n"

        def read(self):
            return ""

        def poll(self):
            return 0 if self.stopped else None

        def communicate(self, **kwargs):
            return "", ""

    def diagnostic(args, *, input, env, **kwargs):
        # Both profiles and all four actions were retained before any capture.
        assert len(evidence.read_text().splitlines()) == 2
        assert sum(event[-1] == "stop" for event in events if event[0] == "measurement") == 4
        assert input == "workspace\nprofile:workspace\ndogfooding\nprofile:dogfooding\nstop\n"
        context = json.loads(env["ALICE_READ_PROFILE_CONTEXT"])
        events.append(("diagnostic", context["profile"], context["revision_label"], context["case"]))
        captures = [{**context, "diagnostic": True, "action": action, "status": capture_status,
                     "wall": 999, "cpu": 999} for action in ("workspace", "dogfooding")]
        return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(item) for item in captures), stderr="sensitive-test-detail")

    monkeypatch.setattr(budgets.subprocess, "Popen", Child)
    monkeypatch.setattr(budgets.subprocess, "run", diagnostic)
    return evidence, events


@pytest.mark.parametrize("gate", [None, "absolute", "relative"])
def test_diagnostics_do_not_change_samples_or_budget_verdict(monkeypatch, tmp_path, gate):
    evidence, events = fake_children(monkeypatch, tmp_path, gate=gate)
    rows = []
    for profiling in (False, True):
        evidence.unlink(missing_ok=True)
        events.clear()
        if profiling:
            monkeypatch.setenv("ALICE_READ_PROFILE", str(tmp_path / "profiles"))
        else:
            monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
        if gate:
            with pytest.raises(AssertionError) as caught:
                budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                       case="all-visible", source_count=3000, repaired=False)
            message = str(caught.value)
            assert ("workspace" if gate == "absolute" else "pack") in message
            assert "minimum_wall" in message and "captures" not in message
        else:
            assert budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                          case="all-visible", source_count=3000, repaired=False) == []
        rows.append([json.loads(line) for line in evidence.read_text().splitlines()])
        measured = [event for event in events if event[0] == "measurement"]
        expected = []
        for profile in KEYS:
            for action in ("pack", "recall", "workspace", "dogfooding"):
                expected.extend(("measurement", profile, revision, action) for revision in ("main", "head"))
                for sample in range(10):
                    expected.extend(("measurement", profile, revision, action)
                                    for revision in (("main", "head") if sample % 2 == 0 else ("head", "main")))
            expected.extend(("measurement", profile, revision, "stop") for revision in ("main", "head"))
        assert measured == expected
        diagnostics = [event for event in events if event[0] == "diagnostic"]
        assert len(diagnostics) == (4 if profiling else 0)
        if profiling:
            assert all(event[0] == "measurement" for event in events[:-4])
    assert rows[0] == rows[1]
    for row in rows[1]:
        for revision in REVISIONS:
            for action in ("pack", "recall", "workspace", "dogfooding"):
                assert len(row[revision][action]["wall"]) == len(row[revision][action]["cpu"]) == 10
                assert 999 not in row[revision][action]["wall"]


def test_failed_budget_keeps_samples_and_incomplete_capture_receipts(monkeypatch, tmp_path):
    evidence, _events = fake_children(monkeypatch, tmp_path, gate="absolute", capture_status="incomplete")
    monkeypatch.setenv("ALICE_READ_PROFILE", str(tmp_path / "profiles"))
    with pytest.raises(AssertionError) as caught:
        budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                               case="extra-rows", source_count=3000, repaired=True)
    assert "workspace" in str(caught.value) and "captures" not in str(caught.value)
    assert len(evidence.read_text().splitlines()) == 2
    summaries = list((tmp_path / "profiles").rglob("capture-summary.json"))
    assert len(summaries) == 4
    for path in summaries:
        receipt = json.loads(path.read_text())
        assert receipt["status"] == "incomplete"
        assert len(receipt["captures"]) == 2
        assert "synthetic-credential" not in path.read_text()
        assert "sensitive-test-detail" not in path.read_text()


def test_profile_cases_have_distinct_directories_and_incomplete_captures_fail(monkeypatch, tmp_path):
    evidence, events = fake_children(monkeypatch, tmp_path, capture_status="incomplete")
    monkeypatch.setenv("ALICE_READ_PROFILE", str(tmp_path / "profiles"))
    for case, repaired in (("all-visible", False), ("extra-rows", True)):
        evidence.unlink(missing_ok=True)
        events.clear()
        with pytest.raises(AssertionError):
            budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                   case=case, source_count=3000, repaired=repaired)
        assert len(evidence.read_text().splitlines()) == 2
    summaries = list((tmp_path / "profiles").rglob("capture-summary.json"))
    assert len(summaries) == 8
    assert all(json.loads(path.read_text())["status"] == "incomplete" for path in summaries)
    assert all(len(json.loads(path.read_text())["captures"]) == 2 for path in summaries)
    assert {path.parent.parent.name for path in summaries} == set(KEYS)
    assert {path.parent.name for path in summaries} == set(REVISIONS)
    assert {path.parent.parent.parent.name for path in summaries} == {
        "all-visible-3000-unrepaired", "extra-rows-3000-repaired",
    }


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_diagnostic_process_failure_is_retained_without_sensitive_output(monkeypatch, tmp_path, failure):
    evidence, _events = fake_children(monkeypatch, tmp_path)
    monkeypatch.setenv("ALICE_READ_PROFILE", str(tmp_path / "profiles"))

    def broken(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 60, output="sensitive-test-detail", stderr="synthetic-credential")
        return SimpleNamespace(returncode=3, stdout="sensitive-test-detail", stderr="synthetic-credential")

    monkeypatch.setattr(budgets.subprocess, "run", broken)
    with pytest.raises(AssertionError):
        budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                               case="all-visible", source_count=3000, repaired=False)
    assert len(evidence.read_text().splitlines()) == 2
    summaries = list((tmp_path / "profiles").rglob("capture-summary.json"))
    assert len(summaries) == 4
    for path in summaries:
        assert json.loads(path.read_text())["status"] == "error"
        assert "synthetic-credential" not in path.read_text()
        assert "sensitive-test-detail" not in path.read_text()


@pytest.mark.parametrize("name", ["workspace", "dogfooding"])
@pytest.mark.parametrize("capture", ["complete", "incomplete", "error"])
def test_explicit_profile_protocol_and_capture_completeness(tmp_path, name, capture):
    def _vnext_workspace_payload():
        return {}

    def get_vnext_workspace():
        return _vnext_workspace_payload()

    def dashboard():
        return {}

    def get_vnext_dogfooding_dashboard():
        return dashboard()

    def incomplete():
        return {}

    def failed():
        raise ValueError("sensitive-test-detail")

    def worker():
        thread = threading.Thread(target=get_vnext_workspace if name == "workspace" else get_vnext_dogfooding_dashboard)
        thread.start()
        thread.join()
        return {}

    action = {"complete": worker, "incomplete": incomplete, "error": failed}[capture]
    output = io.StringIO()
    trace = tmp_path / "capture"
    probe.serve({name: incomplete}, [name + "\n", "stop\n"], output, trace=trace)
    assert len(json.loads(output.getvalue())) == 2
    assert not trace.exists(), "normal commands must never profile the warmup or gate samples"
    output = io.StringIO()
    probe.serve({name: action}, ["profile:" + name + "\n", "stop\n"], output,
                trace=trace, profile_context=lambda: {"revision": REVISIONS["head"]})
    receipt = json.loads(output.getvalue())
    assert receipt["diagnostic"] is True
    assert receipt["status"] == capture
    assert "wall" not in receipt and "cpu" not in receipt
    assert receipt["runtime"]["python"]
    assert receipt["revision"] == REVISIONS["head"]
    assert receipt["missing_functions"] == ([] if capture == "complete" else sorted(probe.EXPECTED_CAPTURE_FUNCTIONS[name]))
    assert json.loads((trace / (name + ".json")).read_text()) == receipt
    assert "sensitive-test-detail" not in output.getvalue()
    stats = pstats.Stats(str(trace / (name + ".prof")))
    assert all("/" not in key[0] for key in stats.stats)
    report = (trace / (name + ".txt")).read_text()
    assert "Ordered by: cumulative time" in report
    assert "Ordered by: internal time" in report
    assert str(tmp_path) not in report


def test_ci_profiles_only_the_maximum_smoke_and_retains_attempts():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["python-integration"]["steps"]
    profile_steps = [step for step in steps if "ALICE_READ_PROFILE" in step.get("env", {})]
    assert len(profile_steps) == 1
    assert profile_steps[0]["name"] == "Maximum-workload read-budget controls"
    assert "[False-3000-all-visible]" in profile_steps[0]["run"]
    assert "[True-3000-extra-rows]" in profile_steps[0]["run"]
    upload = next(step for step in steps if step.get("name") == "Retain paired read-budget samples, including failed runs")
    assert upload["if"].startswith("always()")
    assert "${{ github.run_attempt }}" in upload["with"]["name"]
    assert "round3-read-profiles/" in upload["with"]["path"]
    assert "round3-postgres-read-budgets.jsonl" in upload["with"]["path"]
    assert "round3-sqlite-read-budgets.jsonl" in upload["with"]["path"]


# ---- the gates themselves: each clock, each action and each returned count is pinned ----------------


def measured_failures(monkeypatch, tmp_path, *, case="half-hidden", **options):
    fake_children(monkeypatch, tmp_path, **options)
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    failures = budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                      case=case, source_count=3000, repaired=False, assert_budget=False)
    return {(profile, action, clock) for profile, action, clock, _row in failures}


def slow_head(action, *, wall=.1, cpu=.1):
    return lambda revision, profile, name: (wall, cpu) if revision == "head" and name == action else (.1, .1)


@pytest.mark.parametrize("action", ["pack", "recall"])
@pytest.mark.parametrize("clock,slow", [("minimum_wall", {"wall": .31}), ("minimum_cpu", {"cpu": .31})])
def test_each_clock_of_pack_and_recall_has_its_own_relative_gate(monkeypatch, tmp_path, action, clock, slow):
    """Mutations: read the wall clock for both, read the CPU clock for both, skip recall."""
    failed = measured_failures(monkeypatch, tmp_path, timings=slow_head(action, **slow))
    assert failed == {(profile, action, clock) for profile in KEYS}


@pytest.mark.parametrize("action", ["pack", "recall"])
def test_the_relative_gate_is_twice_main_plus_a_tenth_and_inclusive(monkeypatch, tmp_path, action):
    at_the_gate = slow_head(action, wall=2 * .1 + .1, cpu=2 * .1 + .1)
    assert measured_failures(monkeypatch, tmp_path, timings=at_the_gate) == set()
    just_over = slow_head(action, wall=2 * .1 + .1 + 1e-6, cpu=.1)
    assert measured_failures(monkeypatch, tmp_path, timings=just_over) == {(profile, action, "minimum_wall") for profile in KEYS}


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_workspace_and_dogfooding_each_have_the_one_second_gate(monkeypatch, tmp_path, action):
    """Mutations: gate only the workspace, gate only dogfooding, gate the CPU clock."""
    assert measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=1.000001, cpu=.1)) == {
        (profile, action, "minimum_wall") for profile in KEYS}
    assert measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=1.0, cpu=5.0)) == set()


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_the_one_second_gate_applies_only_when_the_absolute_gate_is_set(monkeypatch, tmp_path, action):
    """Without ALICE_READ_ABSOLUTE_GATE a slow screen is recorded, not failed; the relative gates still fail.

    Mutations: apply the one-second gate whatever the variable says (the first assertion fails); drop the
    relative pack gate when the variable is unset (the second assertion fails).
    """
    fake_children(monkeypatch, tmp_path, timings=slow_head(action, wall=5.0, cpu=.1))
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_GATE")
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    assert budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                  case="extra-rows", source_count=3000, repaired=False, assert_budget=False) == []
    fake_children(monkeypatch, tmp_path, timings=slow_head("pack", wall=2 * .1 + .1 + 1e-6, cpu=.1))
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_GATE")
    failures = budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                      case="extra-rows", source_count=3000, repaired=False, assert_budget=False)
    assert {(profile, action_name, clock) for profile, action_name, clock, _row in failures} == {
        (profile, "pack", "minimum_wall") for profile in KEYS}


def test_the_absolute_gate_does_not_apply_to_the_sqlite_actions(monkeypatch, tmp_path):
    fake_children(monkeypatch, tmp_path, timings=lambda revision, profile, action: (5, 5) if revision == "main" else (.1, .1))
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    assert budgets.paired_budgets("sqlite", "synthetic-database", budgets.USER, KEYS,
                                  case="half-hidden", source_count=300, repaired=False, assert_budget=False) == []


def returning(head, main=READ_LIMIT):
    return lambda revision, profile, action: head if revision == "head" else main


@pytest.mark.parametrize("action_count", [0, 1, READ_LIMIT - 1])
def test_a_head_that_returns_too_little_fails_whatever_its_speed(monkeypatch, tmp_path, action_count):
    failed = measured_failures(monkeypatch, tmp_path, returned=returning(action_count))
    assert failed == {(profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}


def test_the_returned_count_floor_is_the_default_limit_and_applies_to_each_action(monkeypatch, tmp_path):
    assert measured_failures(monkeypatch, tmp_path, returned=returning(READ_LIMIT)) == set()
    only_recall = lambda revision, profile, action: 0 if revision == "head" and action == "recall" else READ_LIMIT
    assert measured_failures(monkeypatch, tmp_path, returned=only_recall) == {
        (profile, "recall", "returned_memories") for profile in KEYS}
    only_pack = lambda revision, profile, action: 0 if revision == "head" and action == "pack" else READ_LIMIT
    assert measured_failures(monkeypatch, tmp_path, returned=only_pack) == {
        (profile, "pack", "returned_memories") for profile in KEYS}


def test_without_hidden_input_head_must_return_what_main_returns(monkeypatch, tmp_path):
    """The admin key has no ceiling; an all-visible or identical-copy fixture hides nothing."""
    more = returning(READ_LIMIT + 1)
    for case in ("all-visible", "identical-copies"):
        assert measured_failures(monkeypatch, tmp_path, case=case, returned=more) == {
            (profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}
    assert measured_failures(monkeypatch, tmp_path, case="half-hidden", returned=more) == {
        ("admin_agent", action, "returned_memories") for action in ("pack", "recall")}
    assert measured_failures(monkeypatch, tmp_path, case="extra-rows", returned=more) == {
        ("admin_agent", action, "returned_memories") for action in ("pack", "recall")}


def test_a_probe_that_reports_no_count_fails_the_returned_floor(monkeypatch, tmp_path):
    """A child that stops reporting what it returned must not pass by omission."""
    fake_children(monkeypatch, tmp_path)
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    original = budgets.subprocess.Popen

    class Silent(original):
        def readline(self):
            return json.dumps({key: value for key, value in json.loads(super().readline()).items() if key != "memories"}) + "\n"

    monkeypatch.setattr(budgets.subprocess, "Popen", Silent)
    failures = budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                      case="half-hidden", source_count=3000, repaired=False, assert_budget=False)
    assert {(profile, action, clock) for profile, action, clock, _row in failures} == {
        (profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}


# ---- the SQLite grid test ends in an assertion on both keyless and keyed failures -------------------


def run_sqlite_grid_test(monkeypatch, tmp_path, *, outcomes):
    """Run the real test body with the database and the children replaced by recorders."""
    calls = []

    class Connection:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    def fake_paired(backend, location, user, keys, **options):
        calls.append((tuple(keys), options.get("assert_budget")))
        return [("profile", "pack", "minimum_wall", {})] if outcomes[len(calls) - 1] else []

    monkeypatch.setattr(budgets, "bootstrap_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(budgets, "sqlite_user_connection", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(budgets, "SQLiteVNextStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(budgets, "seed_grid", lambda *args, **kwargs: {})
    monkeypatch.setattr(budgets, "budget_keys", lambda store: {"trusted_local_agent": "synthetic-credential"})
    monkeypatch.setattr(budgets, "repair_fixture", lambda *args, **kwargs: 0)
    monkeypatch.setattr(budgets, "paired_budgets", fake_paired)
    budgets.test_sqlite_round3_random_read_budgets(tmp_path, "extra-rows", 300, False)
    return calls


@pytest.mark.parametrize("outcomes", [(False, False), (True, False), (False, True), (True, True)])
def test_sqlite_grid_fails_when_either_measurement_fails(monkeypatch, tmp_path, outcomes):
    """Mutations: drop the final assertion; forget the keyless failures; forget the keyed failures."""
    if any(outcomes):
        with pytest.raises(AssertionError):
            run_sqlite_grid_test(monkeypatch, tmp_path, outcomes=outcomes)
    else:
        calls = run_sqlite_grid_test(monkeypatch, tmp_path, outcomes=outcomes)
        assert [keys for keys, _assert in calls] == [("keyless_agent_id",), ("trusted_local_agent",)]
        assert [flag for _keys, flag in calls] == [False, False]


# ---- every PostgreSQL budget fixture gets planner statistics before its first sample ---------------


class AnalyzeHarness:
    urls = {"app": "app-url", "admin": "admin-url"}
    user_id = budgets.USER

    def store(self):
        class Store:
            def __enter__(self):
                return object()

            def __exit__(self, *exc):
                return False

        return Store()


@pytest.mark.parametrize("name,arguments", [
    ("test_postgres_round3_identical_copy_control", ()),
    ("test_postgres_round3_random_read_budgets", ("extra-rows", 300, False)),
    ("test_postgres_round3_random_read_budgets", ("extra-rows", 300, True)),
    ("test_postgres_round3_random_read_budgets", ("all-visible", 3000, False)),
])
def test_postgres_budget_fixtures_analyze_every_table_before_the_first_sample(monkeypatch, name, arguments):
    """Without statistics main's pack read took 6.6 to 7.9 s on the identical-copy fixture, so the
    gate measured against it could not fail. Mutation: drop the call from either test."""
    from tests.integration import test_label_round3_read_budget_postgres as module

    order, statements = [], []

    class Admin:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            statements.append(statement)

    def connect(url, **kwargs):
        order.append("connect " + url)
        assert kwargs == {"autocommit": True}
        return Admin()

    monkeypatch.setattr(module, "seed_grid", lambda *args, **kwargs: order.append("seed") or {})
    monkeypatch.setattr(module, "repair_fixture", lambda *args, **kwargs: order.append("repair"))
    monkeypatch.setattr(module.psycopg, "connect", connect)
    monkeypatch.setattr(module, "paired_budgets", lambda *args, **kwargs: order.append("measure"))
    getattr(module, name)(AnalyzeHarness(), *arguments)
    repaired = bool(arguments) and arguments[2]
    assert order == ["seed", *(["repair"] if repaired else []), "connect admin-url", "measure"]
    assert statements == ["ANALYZE " + table for table in ("sources", "memories", "event_log", "generated_artifacts", "open_loops")]


def test_ci_sqlite_budget_step_runs_the_round_two_complete_count_test():
    """The round-two test asserts complete counts at 5,000 rows and in the one-hidden-source case.
    It stopped running in CI when the round-three file replaced it. Mutation: drop it from the step."""
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["python-integration"]["steps"]
    step = next(step for step in steps if step.get("name", "").startswith("SQLite random mixed-parent and keyless read budgets"))
    command = step["run"].split()
    for name in ("test_label_round3_read_budget.py", "test_label_read_handoff_budget.py", "test_label_round2_read_budget.py"):
        assert "tests/performance/" + name in command
    assert step["env"]["ALICE_READ_MAIN_CHECKOUT"].endswith("/.read-budget-baseline")
    source = (Path(__file__).resolve().parents[2] / "tests/performance/test_label_round2_read_budget.py").read_text()
    assert 'expected = 5000 if profile == "admin_agent" else 2500 if case == "mixed" else 0' in source
    assert '"one-hidden"' in source and "assert complete_count(store, ceiling) ==" in source
