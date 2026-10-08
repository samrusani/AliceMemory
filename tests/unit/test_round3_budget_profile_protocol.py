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


REVISIONS = {"main": "a" * 40, "head": "b" * 40}
KEYS = {"trusted_local_agent": "synthetic-credential", "admin_agent": "synthetic-credential"}


def fake_children(monkeypatch, tmp_path, *, gate=None, capture_status="complete"):
    events = []
    evidence = tmp_path / "samples.jsonl"
    monkeypatch.setenv("ALICE_READ_MAIN_CHECKOUT", "paired-main")
    monkeypatch.setenv("ALICE_READ_BUDGET_EVIDENCE", str(evidence))
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
            return json.dumps({"wall": wall, "cpu": cpu, "memories": 7}) + "\n"

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
