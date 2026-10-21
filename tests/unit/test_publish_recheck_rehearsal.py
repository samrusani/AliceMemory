"""The publish recheck rehearsal runs the release's commands on a runner before a tag exists.

``publish-pypi.yml`` reruns the whole unit suite in one process in its ``build-and-verify``
job, and that step only ever ran from a release tag. A run from a tag uses the workflow at
the tag, so a failure there burns a version number (v0.20.1 stopped at its old time limit).
``publish-recheck-rehearsal.yml`` runs the same install step and recheck step on any ref, on
dispatch and once a week. It is only a rehearsal if it runs the same commands, so these tests
read both workflow files and fail when the two drift apart, when the rehearsal gains a trigger,
a write scope, an environment or a credential reference, or when its report step stops
enforcing the rule that a release-only step may not take more than half its limit.

These tests read the workflow files and run the report step's shell script against a stub
``python``. They do not call GitHub.

Mutations that must fail this file, each alone (``MUTATIONS`` below applies each one to a
copy of the workflow text and expects the named finding):

* change one character of the recheck command in ``publish-pypi.yml`` or in the rehearsal;
* change the install step in the rehearsal;
* change ``timeout-minutes`` of the rehearsal or of the ``build-and-verify`` job;
* change the pinned SHA of the checkout or setup-python action, the ``python-version`` or the
  ``cache`` setting in the rehearsal;
* add ``pull_request`` to the rehearsal, drop its ``schedule``, or move the schedule to the
  hour of ``semantic-release-gate.yml``;
* give the rehearsal a write permission, an ``environment`` or a reference to ``secrets``;
* rename the recheck step in either file, or move another step after the report step;
* change ``JOB_TIMEOUT_MINUTES`` of the report step so it no longer equals the timeout, or make
  the report step compare against the whole limit instead of half of it
  (``test_the_report_step_fails_only_past_half_the_limit``).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH_PATH = WORKFLOWS / "publish-pypi.yml"
REHEARSAL_PATH = WORKFLOWS / "publish-recheck-rehearsal.yml"
SEMANTIC_GATE_PATH = WORKFLOWS / "semantic-release-gate.yml"

PUBLISH_JOB = "build-and-verify"
INSTALL_STEP = "Install release dependencies"
RECHECK_STEP = "Recheck release-critical source tests and coverage"
CHECKOUT = "actions/checkout@"
SETUP_PYTHON = "actions/setup-python@"


class _WorkflowLoader(yaml.SafeLoader):
    """Safe YAML that keeps the workflow key ``on`` as a string."""


_WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in patterns if tag != "tag:yaml.org,2002:bool"]
    for key, patterns in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _load(text: str) -> dict[str, Any]:
    loaded = yaml.load(text, Loader=_WorkflowLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _named(job: dict[str, Any], name: str, label: str, found: list[str]) -> dict[str, Any] | None:
    matching = [step for step in _steps(job) if step.get("name") == name]
    if len(matching) != 1:
        found.append(f"{label} must have exactly one step named {name!r}, found {len(matching)}")
        return None
    return matching[0]


def _using(job: dict[str, Any], prefix: str, label: str, found: list[str]) -> dict[str, Any] | None:
    matching = [step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)]
    if len(matching) != 1:
        found.append(f"{label} must have exactly one step that uses {prefix}, found {len(matching)}")
        return None
    return matching[0]


def _cron_hours(workflow: dict[str, Any]) -> set[str]:
    triggers = workflow.get("on")
    schedules = triggers.get("schedule") if isinstance(triggers, dict) else None
    if not isinstance(schedules, list):
        return set()
    return {
        str(entry["cron"]).split()[1]
        for entry in schedules
        if isinstance(entry, dict) and isinstance(entry.get("cron"), str) and len(entry["cron"].split()) == 5
    }


def findings(publish_text: str, rehearsal_text: str, semantic_text: str) -> list[str]:
    """Every way the rehearsal fails to be a faithful, safe rehearsal of the publish job."""

    found: list[str] = []
    publish = _load(publish_text)
    rehearsal = _load(rehearsal_text)
    semantic = _load(semantic_text)

    publish_job = publish.get("jobs", {}).get(PUBLISH_JOB)
    if not isinstance(publish_job, dict):
        return [f"publish-pypi.yml has no job {PUBLISH_JOB!r}"]
    jobs = rehearsal.get("jobs")
    if not isinstance(jobs, dict) or len(jobs) != 1:
        return ["the rehearsal must have exactly one job"]
    (job,) = jobs.values()
    if not isinstance(job, dict):
        return ["the rehearsal job is not a mapping"]
    label = "the rehearsal"

    # The commands. Equal to the byte, as the parser reads them.
    for name in (INSTALL_STEP, RECHECK_STEP):
        published = _named(publish_job, name, f"the {PUBLISH_JOB} job of publish-pypi.yml", found)
        rehearsed = _named(job, name, label, found)
        if published is None or rehearsed is None:
            continue
        if not isinstance(published.get("run"), str) or published.get("run") != rehearsed.get("run"):
            found.append(f"the {name!r} step of the rehearsal does not run the same commands as publish-pypi.yml")

    # The actions, the interpreter and the time limit.
    for prefix in (CHECKOUT, SETUP_PYTHON):
        published = _using(publish_job, prefix, f"the {PUBLISH_JOB} job of publish-pypi.yml", found)
        rehearsed = _using(job, prefix, label, found)
        if published is None or rehearsed is None:
            continue
        if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{40}", str(rehearsed.get("uses"))):
            found.append(f"the {prefix} step of the rehearsal is not pinned by a full SHA")
        if published.get("uses") != rehearsed.get("uses"):
            found.append(f"the {prefix} pin of the rehearsal differs from publish-pypi.yml")
        if prefix == SETUP_PYTHON and published.get("with") != rehearsed.get("with"):
            found.append("the setup-python python-version or cache of the rehearsal differs from publish-pypi.yml")
        if prefix == CHECKOUT and (rehearsed.get("with") or {}).get("fetch-depth") != 0:
            found.append("the rehearsal checkout must fetch full history and tags (fetch-depth 0)")
    timeout = job.get("timeout-minutes")
    if not isinstance(timeout, int) or timeout != publish_job.get("timeout-minutes"):
        found.append("timeout-minutes of the rehearsal differs from the build-and-verify job of publish-pypi.yml")
    if job.get("runs-on") != publish_job.get("runs-on"):
        found.append("runs-on of the rehearsal differs from the build-and-verify job of publish-pypi.yml")

    # The order: checkout, interpreter, install, then the recheck, then the report as the last step.
    names = [step.get("name") for step in _steps(job)]
    if INSTALL_STEP in names and RECHECK_STEP in names and names.index(INSTALL_STEP) > names.index(RECHECK_STEP):
        found.append("the install step must come before the recheck step")
    steps = _steps(job)
    if steps and not str(steps[0].get("uses", "")).startswith(CHECKOUT):
        found.append("the first step of the rehearsal must be the checkout")
    if RECHECK_STEP in names:
        before = steps[names.index(RECHECK_STEP) - 1] if names.index(RECHECK_STEP) > 0 else {}
        if "RECHECK_STARTED_AT" not in str(before.get("run", "")):
            found.append("the step before the recheck must record RECHECK_STARTED_AT")
    if not steps or "GITHUB_STEP_SUMMARY" not in str(steps[-1].get("run", "")):
        found.append("the last step of the rehearsal must write the job summary")
    elif str((steps[-1].get("env") or {}).get("JOB_TIMEOUT_MINUTES")) != str(timeout):
        found.append("JOB_TIMEOUT_MINUTES of the last step must equal timeout-minutes of the job")

    # The triggers: dispatch and a weekly slot, never a pull request, push or release.
    triggers = rehearsal.get("on")
    if not isinstance(triggers, dict) or set(triggers) != {"workflow_dispatch", "schedule"}:
        found.append("the rehearsal must have exactly the triggers workflow_dispatch and schedule")
    elif not _cron_hours(rehearsal):
        found.append("the rehearsal schedule must hold at least one five field cron")
    elif _cron_hours(rehearsal) & _cron_hours(semantic):
        found.append("the rehearsal schedule must not use the hour of semantic-release-gate.yml")

    # Least privilege: read the repository, no environment, no credentials.
    if rehearsal.get("permissions") != {"contents": "read"}:
        found.append("the rehearsal workflow permissions must be contents: read and nothing else")
    if job.get("permissions") not in (None, {"contents": "read"}):
        found.append("the rehearsal job permissions must be contents: read and nothing else")
    if "environment" in job:
        found.append("the rehearsal job must not name an environment")
    if re.search(r"\bsecrets\b", rehearsal_text):
        found.append("the rehearsal must not reference secrets")
    return found


def _texts() -> tuple[str, str, str]:
    return (
        PUBLISH_PATH.read_text(encoding="utf-8"),
        REHEARSAL_PATH.read_text(encoding="utf-8"),
        SEMANTIC_GATE_PATH.read_text(encoding="utf-8"),
    )


def test_the_rehearsal_is_a_faithful_and_safe_copy_of_the_publish_recheck() -> None:
    """The rehearsal and the publish job agree on everything the rehearsal exists to prove.

    Mutation: any one of ``MUTATIONS`` below, applied to the real file text.
    """

    assert findings(*_texts()) == []


def test_the_steps_that_are_compared_are_the_ones_that_matter() -> None:
    """The comparison cannot pass on two empty or renamed steps.

    Mutation: remove the whole unit suite rerun, the LongMemEval evidence check or the editable
    install from ``publish-pypi.yml``. This test fails, and so do the others in this file
    that find the steps by name.
    """

    publish_job = _load(PUBLISH_PATH.read_text(encoding="utf-8"))["jobs"][PUBLISH_JOB]
    by_name = {step.get("name"): step for step in _steps(publish_job)}
    recheck = by_name[RECHECK_STEP]["run"]
    assert "python -m pytest tests/unit -q --cov=alicebot_api" in recheck
    assert "python scripts/check_python_coverage.py" in recheck
    assert "python -m pytest eval/longmemeval -q" in recheck
    assert recheck.rstrip().endswith("python scripts/check_longmemeval_evidence.py")
    assert "python -m pip install -e '.[dev]'" in by_name[INSTALL_STEP]["run"]
    assert "build==" in by_name[INSTALL_STEP]["run"]


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


_SEMANTIC_GATE_HOUR = sorted(_cron_hours(_load(SEMANTIC_GATE_PATH.read_text(encoding="utf-8"))))[0]

# (name, which file, text to find once, replacement, a phrase the finding must contain)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("publish recheck, one character", "publish", "--cov-fail-under=50", "--cov-fail-under=51", RECHECK_STEP),
    ("rehearsal recheck, one character", "rehearsal", "--min-percent 45", "--min-percent 44", RECHECK_STEP),
    ("rehearsal install", "rehearsal", "build==1.5.0", "build==1.5.1", INSTALL_STEP),
    ("rehearsal timeout", "rehearsal", "timeout-minutes: 120", "timeout-minutes: 119", "timeout-minutes of the rehearsal"),
    ("publish timeout", "publish", "timeout-minutes: 120", "timeout-minutes: 121", "timeout-minutes of the rehearsal"),
    (
        "rehearsal checkout pin",
        "rehearsal",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b2",
        "checkout@ pin",
    ),
    (
        "rehearsal setup-python pin",
        "rehearsal",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b98",
        "setup-python@ pin",
    ),
    ("rehearsal python version", "rehearsal", 'python-version: "3.12"', 'python-version: "3.13"', "python-version"),
    ("rehearsal cache setting", "rehearsal", "          cache: pip\n", "", "python-version or cache"),
    ("rehearsal fetch depth", "rehearsal", "          fetch-depth: 0\n", "", "fetch-depth 0"),
    ("pull request trigger", "rehearsal", "\n  workflow_dispatch:\n", "\n  pull_request:\n  workflow_dispatch:\n", "exactly the triggers"),
    ("push trigger", "rehearsal", "\n  workflow_dispatch:\n", "\n  push:\n  workflow_dispatch:\n", "exactly the triggers"),
    ("no schedule", "rehearsal", '  schedule:\n    - cron: "37 5 * * 3"\n', "", "exactly the triggers"),
    ("no dispatch", "rehearsal", "\n  workflow_dispatch:\n", "\n", "exactly the triggers"),
    (
        "hour of the semantic gate",
        "rehearsal",
        '"37 5 * * 3"',
        f'"37 {_SEMANTIC_GATE_HOUR} * * 3"',
        "hour of semantic-release-gate",
    ),
    ("write permission", "rehearsal", "permissions:\n  contents: read\n", "permissions:\n  contents: write\n", "contents: read and nothing else"),
    (
        "extra permission",
        "rehearsal",
        "permissions:\n  contents: read\n",
        "permissions:\n  contents: read\n  id-token: write\n",
        "contents: read and nothing else",
    ),
    ("environment", "rehearsal", "    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    environment: pypi\n", "environment"),
    (
        "credential reference",
        "rehearsal",
        "RECHECK_OUTCOME: ${{ steps.recheck.outcome }}",
        "RECHECK_OUTCOME: ${{ secrets.GITHUB_TOKEN }}",
        "secrets",
    ),
    (
        "recheck step renamed in the rehearsal",
        "rehearsal",
        "      - name: Recheck release-critical source tests and coverage\n",
        "      - name: Recheck release-critical source tests and coverage again\n",
        RECHECK_STEP,
    ),
    (
        "recheck step renamed in the publish job",
        "publish",
        "      - name: Recheck release-critical source tests and coverage\n",
        "      - name: Recheck source tests and coverage\n",
        RECHECK_STEP,
    ),
    (
        "step after the report",
        "rehearsal",
        "            exit 1\n          fi\n",
        "            exit 1\n          fi\n\n      - name: Later\n        run: echo later\n",
        "must write the job summary",
    ),
    ("report limit differs from the timeout", "rehearsal", 'JOB_TIMEOUT_MINUTES: "120"', 'JOB_TIMEOUT_MINUTES: "121"', "JOB_TIMEOUT_MINUTES"),
    (
        "stamp step removed",
        "rehearsal",
        '      - name: Record the time the recheck starts\n        run: echo "RECHECK_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"\n\n',
        "",
        "must record RECHECK_STARTED_AT",
    ),
]


@pytest.mark.parametrize(
    ("name", "which", "old", "new", "expected"), MUTATIONS, ids=[mutation[0] for mutation in MUTATIONS]
)
def test_each_mutation_is_found(name: str, which: str, old: str, new: str, expected: str) -> None:
    """A copy of the workflow text with one change is reported, and only the real text is clean.

    Mutation: delete the check that the named finding comes from. That case fails.
    """

    publish, rehearsal, semantic = _texts()
    if which == "publish":
        publish = _once(publish, old, new)
    else:
        rehearsal = _once(rehearsal, old, new)
    reported = findings(publish, rehearsal, semantic)
    assert reported, f"{name}: no finding"
    assert any(expected in line for line in reported), (name, reported)


# --- the report step runs, against a stub python -----------------------------------------------


def _rehearsal_steps() -> list[dict[str, Any]]:
    return _steps(next(iter(_load(REHEARSAL_PATH.read_text(encoding="utf-8"))["jobs"].values())))


def _run_script(
    tmp_path: Path, name: str, script: str, env: dict[str, str], stub_python: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a step's script the way the runner does (bash with ``-e -o pipefail``)."""

    assert "${{" not in script, "a step script must not hold an expression: the runner would fill it in"
    path = tmp_path / f"{name}.sh"
    path.write_text(script, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if stub_python is not None:
        stub = bin_dir / "python"
        stub.write_text("#!/bin/sh\n" + stub_python, encoding="utf-8")
        stub.chmod(0o755)
    full_env = dict(os.environ)
    full_env["PATH"] = f"{bin_dir}{os.pathsep}{full_env['PATH']}"
    full_env.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(path)],
        env=full_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _report(tmp_path: Path, seconds_ago: int, stub_python: str) -> tuple[subprocess.CompletedProcess[str], str]:
    steps = _rehearsal_steps()
    names = [step.get("name") for step in steps]
    stamp = steps[names.index(RECHECK_STEP) - 1]
    report = steps[-1]
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    stamped = _run_script(tmp_path, "stamp", stamp["run"], {"GITHUB_ENV": str(github_env)})
    assert stamped.returncode == 0, stamped.stderr
    lines = github_env.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    variable, _, value = lines[0].partition("=")
    assert value.isdigit(), lines
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {
        **{key: str(item) for key, item in (report.get("env") or {}).items() if "${{" not in str(item)},
        "RECHECK_OUTCOME": "success",
        "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
        "GITHUB_STEP_SUMMARY": str(summary),
        variable: str(int(value) - seconds_ago),
    }
    result = _run_script(tmp_path, "report", report["run"], env, stub_python)
    return result, summary.read_text(encoding="utf-8")


def _limit_seconds() -> int:
    timeout = next(iter(_load(REHEARSAL_PATH.read_text(encoding="utf-8"))["jobs"].values()))["timeout-minutes"]
    return timeout * 60 // 2


_COLLECTED = 'printf "tests/unit/test_a.py::test_one\\n\\n5341 tests collected in 9.87s\\n"\n'


def test_the_report_step_writes_the_duration_and_the_count_and_passes_under_the_limit(tmp_path: Path) -> None:
    """Under half the limit the step passes and the summary holds the duration and the count.

    Mutation: drop the duration row or the count row from the summary, or read the start time
    from a variable the stamp step does not write. This test fails.
    """

    result, summary = _report(tmp_path, seconds_ago=_limit_seconds() - 600, stub_python=_COLLECTED)
    assert result.returncode == 0, result.stdout + result.stderr
    duration = re.search(r"\| Recheck step duration \| (\d+) s \((\d+) min (\d+) s\) \|", summary)
    assert duration, summary
    elapsed = int(duration.group(1))
    assert abs(elapsed - (_limit_seconds() - 600)) <= 10
    assert int(duration.group(2)) * 60 + int(duration.group(3)) == elapsed
    assert "| Unit tests | 5341 tests collected in 9.87s |" in summary
    assert f"| Most the rule allows (half of {_limit_seconds() // 30} min) | {_limit_seconds()} s |" in summary
    assert "| Recheck step outcome | success |" in summary
    assert "`0123456789abcdef0123456789abcdef01234567`" in summary


def test_the_report_step_fails_only_past_half_the_limit(tmp_path: Path) -> None:
    """A recheck that took more than half the job limit fails the rehearsal, and the summary is still written.

    Mutation: compare against the whole limit (drop the ``/ 2``), or exit 0 after the error.
    This test fails.
    """

    result, summary = _report(tmp_path, seconds_ago=_limit_seconds() + 60, stub_python=_COLLECTED)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stdout
    assert "more than half" in result.stdout
    assert "| Unit tests | 5341 tests collected in 9.87s |" in summary
    assert "| Most the rule allows" in summary


def test_a_failed_count_does_not_fail_the_rehearsal(tmp_path: Path) -> None:
    """The count is a report line: if collection fails, the summary says so and the step passes.

    Mutation: drop the ``|| true`` after the collection. This test fails.
    """

    result, summary = _report(tmp_path, seconds_ago=30, stub_python="exit 3\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "| Unit tests | not counted |" in summary


def test_the_report_step_runs_after_a_failed_recheck_but_not_after_a_skipped_one() -> None:
    """The duration of a failing recheck is worth having; a skipped one has no start time.

    Mutation: use the default condition (the step then does not run after a failure), or drop
    the skipped test (the step then fails on a missing start time). This test fails.
    """

    condition = str(_rehearsal_steps()[-1].get("if"))
    assert "!cancelled()" in condition
    assert "steps.recheck.outcome != 'skipped'" in condition
    recheck = next(step for step in _rehearsal_steps() if step.get("name") == RECHECK_STEP)
    assert recheck.get("id") == "recheck"
