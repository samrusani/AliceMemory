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
REVISIONS = {"merge_base": "a" * 40, "head": "b" * 40, "prelabels": "c" * 40}
KEYS = {"trusted_local_agent": "synthetic-credential", "admin_agent": "synthetic-credential"}
PATH_REVISION = {"paired-main": "merge_base", "prelabels-checkout": "prelabels"}


def fake_children(monkeypatch, tmp_path, *, gate=None, capture_status="complete", timings=None, returned=None):
    events = []
    evidence = tmp_path / "samples.jsonl"
    monkeypatch.setenv("ALICE_READ_MERGE_BASE_CHECKOUT", "paired-main")
    monkeypatch.delenv("ALICE_READ_MAIN_CHECKOUT", raising=False)
    monkeypatch.setenv("ALICE_READ_BUDGET_EVIDENCE", str(evidence))
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_GATE", "1")
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", raising=False)
    monkeypatch.setattr(
        budgets.subprocess, "check_output",
        lambda args, **kwargs: REVISIONS[PATH_REVISION.get(args[2], "head")],
    )

    class Child:
        def __init__(self, args, **kwargs):
            self.revision = PATH_REVISION.get(args[2], "head")
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
                expected.extend(("measurement", profile, revision, action) for revision in ("merge_base", "head"))
                for sample in range(10):
                    expected.extend(("measurement", profile, revision, action)
                                    for revision in (("merge_base", "head") if sample % 2 == 0 else ("head", "merge_base")))
            expected.extend(("measurement", profile, revision, "stop") for revision in ("merge_base", "head"))
        assert measured == expected
        diagnostics = [event for event in events if event[0] == "diagnostic"]
        assert len(diagnostics) == (4 if profiling else 0)
        if profiling:
            assert all(event[0] == "measurement" for event in events[:-4])
    assert rows[0] == rows[1]
    for row in rows[1]:
        for revision in ("merge_base", "head"):
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
    assert {path.parent.name for path in summaries} == {"merge_base", "head"}
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
    # The SQLite samples are written, and uploaded under a name of their own, by the budget parts.
    assert "round3-sqlite-read-budgets.jsonl" not in upload["with"]["path"]
    assert "job-index" not in upload["with"]["name"]


def test_ci_applies_the_absolute_gate_only_in_the_maximum_smoke_and_with_the_shared_runner_limit():
    """The absolute wall gate needs a known machine. The one job that sets it runs on shared runners and
    sets the backstop limit; the same-runner paired rule in the same step catches one slow pull request.

    Mutations: delete ``ALICE_READ_ABSOLUTE_LIMIT_SECONDS`` from the step (the job gates at one second again); set
    ``ALICE_READ_ABSOLUTE_GATE`` on another step.
    """
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    names = ("ALICE_READ_ABSOLUTE_GATE", "ALICE_READ_ABSOLUTE_LIMIT_SECONDS")
    assert not any(name in workflow.get("env", {}) for name in names)
    for job in workflow["jobs"].values():
        assert not any(name in job.get("env", {}) for name in names)
    carriers = [step for job in workflow["jobs"].values() for step in job.get("steps", []) if any(name in step.get("env", {}) for name in names)]
    assert [step["name"] for step in carriers] == ["Maximum-workload read-budget controls"]
    assert carriers[0]["env"]["ALICE_READ_ABSOLUTE_GATE"] == "1"
    assert carriers[0]["env"]["ALICE_READ_ABSOLUTE_LIMIT_SECONDS"] == "2.0"


# ---- the gates themselves: each clock, each action and each returned count is pinned ----------------


def measured_failures(monkeypatch, tmp_path, *, case="half-hidden", limit=None, **options):
    fake_children(monkeypatch, tmp_path, **options)
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    if limit is not None:
        monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", limit)
    failures = budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                      case=case, source_count=3000, repaired=False, assert_budget=False)
    failed = set()
    for failure in failures:
        if len(failure) == 5:
            profile, _check, action, clock, _row = failure
        else:
            profile, action, clock, _row = failure
        failed.add((profile, action, clock))
    return failed


def slow_head(action, *, wall=.1, cpu=.1):
    return lambda revision, profile, name: (wall, cpu) if revision == "head" and name == action else (.1, .1)


@pytest.mark.parametrize("action", ["pack", "recall"])
@pytest.mark.parametrize("clock,slow", [("minimum_wall", {"wall": .31}), ("median_cpu", {"cpu": .31})])
def test_each_clock_of_pack_and_recall_has_its_own_relative_gate(monkeypatch, tmp_path, action, clock, slow):
    """Mutations: drop either statistic from ``PAIRED_STATISTICS`` in ``read_budget_gate``; skip recall."""
    failed = measured_failures(monkeypatch, tmp_path, timings=slow_head(action, **slow))
    assert failed == {(profile, action, clock) for profile in KEYS}


@pytest.mark.parametrize("action", ["pack", "recall"])
def test_the_merge_base_gate_is_one_and_a_quarter_plus_fifty_milliseconds_and_inclusive(monkeypatch, tmp_path, action):
    """1.25 times the merge base plus 0.05 seconds passes on the boundary and fails just past it.

    Mutation: raise ``MERGE_BASE_FACTOR`` (a stall of a quarter no longer fails) or drop the slack.
    """
    limit = 1.25 * .1 + .05
    at_the_gate = slow_head(action, wall=limit, cpu=limit)
    assert measured_failures(monkeypatch, tmp_path, timings=at_the_gate) == set()
    just_over = slow_head(action, wall=limit + 1e-6, cpu=.1)
    assert measured_failures(monkeypatch, tmp_path, timings=just_over) == {(profile, action, "minimum_wall") for profile in KEYS}


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_workspace_and_dogfooding_each_have_the_one_second_gate(monkeypatch, tmp_path, action):
    """Mutations: gate only the workspace, gate only dogfooding, gate the CPU clock.

    The screens answer to the same-runner paired rule and the absolute limit;
    the paired rule needs the merge base read at the same speed, so the base
    child reads the same wall time here and the CPU clock stays ungated.
    """
    assert measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=1.000001, cpu=.1)) == {
        (profile, action, "minimum_wall") for profile in KEYS}

    def even_wall_slow_cpu(revision, profile, name):
        if revision == "head" and name == action:
            return (1.0, 5.0)
        return (1.0, .1) if name == action else (.1, .1)

    assert measured_failures(monkeypatch, tmp_path, timings=even_wall_slow_cpu) == set()


def test_the_absolute_limit_is_one_second_unless_the_environment_sets_it(monkeypatch):
    """Mutations: change the default (``DEFAULT_ABSOLUTE_LIMIT_SECONDS``) to another number; ignore the variable (``limit = float(raw)``
    replaced by the default)."""
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", raising=False)
    assert budgets.absolute_limit_seconds() == 1.0
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", "")
    assert budgets.absolute_limit_seconds() == 1.0
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", "1.5")
    assert budgets.absolute_limit_seconds() == 1.5
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", " 2 ")
    assert budgets.absolute_limit_seconds() == 2.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "one second", "1.5s"])
def test_an_absolute_limit_that_is_not_a_positive_finite_number_is_refused(monkeypatch, value):
    """A limit that is zero, negative, not a number or infinite would pass every screen or fail every screen without saying so.

    Mutation: drop the range check (``if not (math.isfinite(limit) and limit > 0)`` made false): the numbers are accepted.
    """
    monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", value)
    with pytest.raises(ValueError):
        budgets.absolute_limit_seconds()


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_the_absolute_gate_holds_the_screens_to_the_limit_the_environment_sets(monkeypatch, tmp_path, action):
    """With a limit of 1.5 seconds a screen at 1.4 passes and a screen just over 1.5 fails; without it the same 1.4 fails.

    The merge base reads 1.2 on the same screen, so the paired rule (1.25
    times 1.2 plus 0.05) passes at 1.4 and at 1.500001 and the failures here
    are the absolute limit's alone.

    Mutations: compare the wall clock to one second whatever the variable says (the first assertion fails); read the variable and
    leave the comparison at the default (the second assertion fails).
    """

    def runs(revision, profile, name):
        if name == action:
            return (1.4, .1) if revision == "head" else (1.2, .1)
        return (.1, .1)

    assert measured_failures(monkeypatch, tmp_path, timings=runs, limit="1.5") == set()
    assert measured_failures(monkeypatch, tmp_path, timings=runs) == {(profile, action, "minimum_wall") for profile in KEYS}

    def just_over(revision, profile, name):
        if name == action:
            return (1.500001, .1) if revision == "head" else (1.2, .1)
        return (.1, .1)

    assert measured_failures(monkeypatch, tmp_path, timings=just_over, limit="1.5") == {
        (profile, action, "minimum_wall") for profile in KEYS}


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_the_one_second_gate_applies_only_when_the_absolute_gate_is_set(monkeypatch, tmp_path, action):
    """Without ALICE_READ_ABSOLUTE_GATE the screens are not timed, so a slow screen cannot fail; the relative gates still fail.

    Mutations: time the screens on every postgres run (the evidence then names the screen); drop the
    relative pack gate when the variable is unset (the second assertion fails).
    """
    evidence, _events = fake_children(monkeypatch, tmp_path, timings=slow_head(action, wall=5.0, cpu=.1))
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_GATE")
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    assert budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                  case="extra-rows", source_count=3000, repaired=False, assert_budget=False) == []
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert rows and all(set(row["head"]) == {"pack", "recall"} for row in rows)
    fake_children(monkeypatch, tmp_path, timings=slow_head("pack", wall=1.25 * .1 + .05 + 1e-6, cpu=.1))
    monkeypatch.delenv("ALICE_READ_ABSOLUTE_GATE")
    failures = budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                                      case="extra-rows", source_count=3000, repaired=False, assert_budget=False)
    assert {(profile, action_name, clock) for profile, action_name, clock, _row in failures} == {
        (profile, "pack", "minimum_wall") for profile in KEYS}


def test_the_absolute_gate_does_not_apply_to_the_sqlite_actions(monkeypatch, tmp_path):
    fake_children(monkeypatch, tmp_path, timings=lambda revision, profile, action: (5, 5) if revision == "merge_base" else (.1, .1))
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    assert budgets.paired_budgets("sqlite", "synthetic-database", budgets.USER, KEYS,
                                  case="half-hidden", source_count=300, repaired=False, assert_budget=False) == []


def returning(head, main=READ_LIMIT):
    return lambda revision, profile, action: head if revision == "head" else main


@pytest.mark.parametrize("action_count", [0, 1, READ_LIMIT - 1])
def test_a_head_that_returns_too_little_fails_whatever_its_speed(monkeypatch, tmp_path, action_count):
    failed = measured_failures(monkeypatch, tmp_path, returned=returning(action_count))
    assert failed == {(profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}


@pytest.mark.parametrize("action_count", [0, 1, READ_LIMIT - 1])
def test_the_floor_fails_when_the_head_and_the_merge_base_return_the_same_too_small_count(
    monkeypatch, tmp_path, action_count
):
    """Both revisions can return too little, which is what a regression already on main looks like.

    The counts match, so the equality check passes and only the floor fails the read.
    Mutation: delete ``min(head) < min(READ_LIMIT, VISIBLE_ORIGINALS)``.
    """

    same = lambda revision, profile, action: action_count
    failed = measured_failures(monkeypatch, tmp_path, returned=same)
    assert failed == {(profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}


def test_the_returned_count_floor_is_the_default_limit_and_applies_to_each_action(monkeypatch, tmp_path):
    assert measured_failures(monkeypatch, tmp_path, returned=returning(READ_LIMIT)) == set()
    only_recall = lambda revision, profile, action: 0 if revision == "head" and action == "recall" else READ_LIMIT
    assert measured_failures(monkeypatch, tmp_path, returned=only_recall) == {
        (profile, "recall", "returned_memories") for profile in KEYS}
    only_pack = lambda revision, profile, action: 0 if revision == "head" and action == "pack" else READ_LIMIT
    assert measured_failures(monkeypatch, tmp_path, returned=only_pack) == {
        (profile, "pack", "returned_memories") for profile in KEYS}


def test_head_must_return_what_the_merge_base_returns(monkeypatch, tmp_path):
    """The merge base runs the same read. A different count fails for every caller, hidden rows included."""
    more = returning(READ_LIMIT + 1)
    expected = {(profile, action, "returned_memories") for profile in KEYS for action in ("pack", "recall")}
    for case in ("all-visible", "identical-copies", "half-hidden", "extra-rows"):
        assert measured_failures(monkeypatch, tmp_path, case=case, returned=more) == expected


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
    It stopped running in CI when the round-three file replaced it. Mutation: drop it from the selection of the part that holds it."""
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"]["python-sqlite-read-budgets"]
    step = next(step for step in job["steps"] if step.get("name", "").startswith("SQLite random mixed-parent and keyless read budgets"))
    assert "pytest ${{ matrix.selection }} -q" in step["run"]
    selections = " ".join(entry["selection"] for entry in job["strategy"]["matrix"]["include"]).split()
    for name in (
        "test_resume_hidden_rows_budget.py",
        "test_label_round3_read_budget.py",
        "test_label_read_handoff_budget.py",
        "test_label_round2_read_budget.py",
    ):
        assert "tests/performance/" + name in selections
    assert "ALICE_READ_MAIN_CHECKOUT" not in step["env"]
    assert step["env"]["ALICE_READ_MERGE_BASE_CHECKOUT"].endswith("/.read-budget-merge-base")
    source = (Path(__file__).resolve().parents[2] / "tests/performance/test_label_round2_read_budget.py").read_text()
    assert 'expected = 5000 if profile == "admin_agent" else 2500 if case == "mixed" else 0' in source
    assert '"one-hidden"' in source and "assert complete_count(store, ceiling) ==" in source
    budget_steps = [
        step for job in workflow["jobs"].values() for step in job.get("steps", [])
        if "ALICE_READ_BUDGET_EVIDENCE" in step.get("env", {})
    ]
    assert len(budget_steps) == 3
    for budget_step in budget_steps:
        assert budget_step["env"]["ALICE_READ_MERGE_BASE_CHECKOUT"].endswith("/.read-budget-merge-base")
    smoke = next(step for step in budget_steps if step.get("name") == "Maximum-workload read-budget controls")
    assert smoke["env"]["ALICE_READ_MAIN_CHECKOUT"].endswith("/.read-budget-baseline")
    required = [step for step in budget_steps if step is not smoke]
    assert required and all("ALICE_READ_MAIN_CHECKOUT" not in step["env"] for step in required)


def test_the_required_jobs_do_not_measure_the_prelabels_checkout():
    """The required integration step and the SQLite budget step measure the head and the merge base.

    The pre-labels checkout is checked out, and ``ALICE_READ_MAIN_CHECKOUT`` is set, only for the
    maximum-workload smoke job. Those required steps also do not set ``ALICE_READ_ABSOLUTE_GATE``,
    in the step environment or in the command, so they do not time the screens or apply the
    fixed-second limits. Mutation: put ``ALICE_READ_MAIN_CHECKOUT`` back on the integration step,
    or set ``ALICE_READ_ABSOLUTE_GATE`` there. That step then measures a third revision, or applies
    the absolute gate, and the assertion fails.
    """

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["python-integration"]["steps"]
    baseline = next(step for step in steps if str(step.get("name", "")).startswith("Paired read-budget baseline"))
    assert baseline["if"] == "matrix.integration_check == 'Read-budget maximum workload smoke (Postgres)'"
    # The required measurements run in the shards and in the SQLite budget parts, not in the smoke job.
    integration = next(step for step in workflow["jobs"]["python-integration-shards"]["steps"] if step.get("name") == "Integration tests of this shard")
    sqlite = next(
        step for step in workflow["jobs"]["python-sqlite-read-budgets"]["steps"]
        if str(step.get("name", "")).startswith("SQLite random mixed-parent")
    )
    smoke = next(step for step in steps if step.get("name") == "Maximum-workload read-budget controls")
    assert "ALICE_READ_MAIN_CHECKOUT" not in integration.get("env", {})
    assert "ALICE_READ_MAIN_CHECKOUT" not in sqlite.get("env", {})
    assert smoke["env"]["ALICE_READ_MAIN_CHECKOUT"].endswith("/.read-budget-baseline")
    for step in (integration, sqlite):
        assert "ALICE_READ_ABSOLUTE_GATE" not in step.get("env", {})
        assert "ALICE_READ_ABSOLUTE_GATE" not in step.get("run", "")
    assert smoke["env"]["ALICE_READ_ABSOLUTE_GATE"] == "1"
    assert "--session-timeout=1500" in integration["run"]
    assert "--session-timeout=1500" in sqlite["run"]


def test_the_maximum_smoke_runs_a_round_two_postgres_case_and_a_handoff_postgres_case():
    """The screens and their fixed limits still run in the smoke job, on one case from each suite.

    Mutation: drop either node id from the smoke step.
    """

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["python-integration"]["steps"]
    smoke = next(step for step in steps if step.get("name") == "Maximum-workload read-budget controls")
    assert "test_postgres_round2_read_budgets[one-hidden]" in smoke["run"]
    assert "test_postgres_five_thousand_native_read_budgets[False]" in smoke["run"]


def test_a_slow_prelabels_checkout_is_recorded_and_does_not_fail_the_gate(monkeypatch, tmp_path):
    """The pre-labels ratio can be far past the old 2x line and the merge-base gate still passes.

    Mutation: fail the job on ``prelabels_report`` (this assertion sees a failure).
    """
    evidence, _events = fake_children(
        monkeypatch, tmp_path,
        timings=lambda revision, profile, action: (0.01, 0.01) if revision == "prelabels" else (0.1, 0.1),
    )
    monkeypatch.setenv("ALICE_READ_MAIN_CHECKOUT", "prelabels-checkout")
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    assert budgets.paired_budgets(
        "postgres", "synthetic-database", budgets.USER, KEYS,
        case="half-hidden", source_count=3000, repaired=False, assert_budget=False,
    ) == []
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        report = row["prelabels_report"]
        assert report["gated"] is False
        wall = report["actions"]["pack"]["minimum_wall"]
        assert wall == {"head": 0.1, "prelabels": 0.01, "ratio": 10.0, "gated": False}


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_a_screen_past_the_paired_rule_fails_on_the_same_runner(monkeypatch, tmp_path, action):
    """The screens answer to the same paired rule pack and recall use: a head
    over 1.25 times the merge base plus 0.05 seconds fails, with the failure
    naming the screen's own minimum_wall and nothing else.

    Mutation: drop the screen call of ``screen_gate_failures`` (the screen
    then answers only through the absolute limit).
    """
    failed = measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=.31))
    assert failed == {(profile, action, "minimum_wall") for profile in KEYS}


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_a_screen_at_the_merge_base_or_at_the_paired_boundary_passes(monkeypatch, tmp_path, action):
    """A head equal to its merge base passes, and so does a head exactly on
    the paired bound (1.25 times 0.1 plus 0.05): the boundary itself is not a
    failure, as with pack and recall.
    """
    assert measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=.1)) == set()
    boundary = 1.25 * .1 + .05
    assert measured_failures(monkeypatch, tmp_path, timings=slow_head(action, wall=boundary)) == set()


def test_the_screen_paired_rule_needs_the_merge_base_checkout(monkeypatch, tmp_path):
    """Without a merge-base checkout there is no same-runner comparison and
    the paired rule cannot run: the import-scoped budgets assert the missing
    variable before any screen is measured.
    """
    fake_children(monkeypatch, tmp_path, timings=slow_head("workspace", wall=.31))
    monkeypatch.delenv("ALICE_READ_MERGE_BASE_CHECKOUT")
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    with pytest.raises(AssertionError, match="merge-base checkout"):
        budgets.paired_budgets("postgres", "synthetic-database", budgets.USER, KEYS,
                               case="all-visible", source_count=3000, repaired=False, assert_budget=False)
