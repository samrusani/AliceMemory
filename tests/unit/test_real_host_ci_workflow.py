"""The real-host workflow stays path-filtered, pinned, and free of secrets.

A Linux trial ran ``claude doctor`` and ``hermes config get`` headless
with no credentials, and the two real-host tests passed. The pinned job
is therefore a failing check on the pull requests that touch host
install. These tests read the workflow file. They do not call GitHub,
claude, or hermes.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "real-host-ci.yml"
WORKFLOW_RELATIVE = ".github/workflows/real-host-ci.yml"
HOST_MODULES = ("host_install", "host_launcher", "session_start_hook")
SOURCE_PATHS = tuple(f"apps/api/src/alicebot_api/{name}.py" for name in HOST_MODULES)
CLAUDE_NPM = "@anthropic-ai/claude-code@2.1.281"
HERMES_PIP = "hermes-agent==0.19.0"
CLAUDE_VERSION = "2.1.281 (Claude Code)"
HERMES_VERSION = "Hermes Agent v0.19.0 (2026.7.20)"
PINNED_IF = "${{ github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' }}"
CANARY_IF = "${{ github.event_name == 'schedule' }}"
OPS_TITLE = "[ops] real-host canary failure"
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")


class _WorkflowLoader(yaml.SafeLoader):
    """Safe YAML that keeps the workflow key ``on`` as a string."""


def _without_bool_resolver(loader: type[yaml.SafeLoader]) -> None:
    """Copy resolvers so ``on`` is not parsed as true, without touching SafeLoader."""

    copied = {
        key: list(patterns) for key, patterns in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for key, patterns in copied.items():
        copied[key] = [
            (tag, pattern) for tag, pattern in patterns if tag != "tag:yaml.org,2002:bool"
        ]
    loader.yaml_implicit_resolvers = copied


_without_bool_resolver(_WorkflowLoader)


def _load_workflow() -> dict:
    assert WORKFLOW_PATH.is_file(), WORKFLOW_RELATIVE
    loaded = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=_WorkflowLoader)
    assert isinstance(loaded, dict)
    return loaded


def _job(name: str) -> dict:
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow has no jobs"
    job = jobs.get(name)
    assert isinstance(job, dict), name
    return job


def _run_text(job: dict) -> str:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return "\n".join(step.get("run", "") for step in steps if isinstance(step, dict))


def _imports_host_module(text: str) -> bool:
    module = "|".join(HOST_MODULES)
    if re.search(rf"\bimport alicebot_api\.(?:{module})\b", text):
        return True
    if re.search(rf"\bfrom alicebot_api\.(?:{module})\b", text):
        return True
    if re.search(rf"\bfrom alicebot_api import [^\n#]*\b(?:{module})\b", text):
        return True
    for match in re.finditer(r"from alicebot_api import \((.*?)\)", text, re.S):
        if re.search(rf"\b(?:{module})\b", match.group(1)):
            return True
    return False


def _host_change_paths() -> set[str]:
    """Source files, tests that import them, and this workflow."""

    found = set(SOURCE_PATHS)
    found.add(WORKFLOW_RELATIVE)
    tests = REPO_ROOT / "tests"
    for path in tests.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _imports_host_module(text):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def _is_weekly(cron: str) -> bool:
    fields = cron.split()
    if len(fields) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = fields
    return (
        minute != "*"
        and hour != "*"
        and day_of_month == "*"
        and month == "*"
        and day_of_week in {"0", "1", "2", "3", "4", "5", "6", "7"}
    )


def _assert_actions_are_sha_pinned(job: dict) -> None:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps
    for step in steps:
        uses = step.get("uses")
        if not uses:
            continue
        _action, separator, ref = uses.partition("@")
        assert separator == "@", uses
        assert ACTION_SHA.fullmatch(ref), uses


def _assert_failure_fails_the_job(job: dict) -> None:
    assert job.get("continue-on-error") in (None, False)
    steps = job.get("steps")
    assert isinstance(steps, list)
    for step in steps:
        assert step.get("continue-on-error") in (None, False)


def _pytest_step(job: dict) -> dict:
    steps = job.get("steps")
    assert isinstance(steps, list)
    matched = [
        step
        for step in steps
        if "test_real_claude_doctor_accepts_the_written_settings" in step.get("run", "")
        and "test_real_hermes_loads_the_written_config" in step.get("run", "")
    ]
    assert len(matched) == 1
    return matched[0]


def test_pinned_job_runs_only_when_host_files_change() -> None:
    """Pull requests do not start this heavy job unless a host file changed.

    Mutation: delete ``host_install.py`` from ``pull_request.paths``.
    This test fails.
    """

    triggers = _load_workflow().get("on")
    assert isinstance(triggers, dict)
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "pull_request is not limited to a path list"
    paths = pull_request.get("paths")
    assert isinstance(paths, list) and paths, "pull_request has no paths filter"
    assert not pull_request.get("paths-ignore")
    expected = _host_change_paths()
    assert set(paths) == expected, sorted(set(paths) ^ expected)
    assert not any(path in {"**", "*", "**/*"} for path in paths)
    for source in SOURCE_PATHS:
        assert source in paths
    pinned = _job("pinned")
    assert pinned.get("if") == PINNED_IF
    assert pinned.get("runs-on") == "ubuntu-latest"


def test_pinned_job_pins_the_trialed_hosts_and_refuses_a_skip() -> None:
    """The pull request job installs the versions the trial ran.

    A skipped real-host test would otherwise exit 0. Mutation: install
    ``@anthropic-ai/claude-code@latest`` in the pinned job. This test fails.
    """

    job = _job("pinned")
    script = _run_text(job)
    assert CLAUDE_NPM in script
    assert HERMES_PIP in script
    assert "@latest" not in script
    assert CLAUDE_VERSION in script
    assert HERMES_VERSION in script
    # hermes --version prints extra lines. The pin compares the first line.
    assert 'hermes_version="${hermes_report%%$' in script
    step = _pytest_step(job)
    assert step.get("env", {}).get("ALICE_TEST_REAL_HOSTS") == "1"
    assert "real host tests did not all run" in step["run"]
    assert "skipped != 0" in step["run"]
    _assert_failure_fails_the_job(job)
    _assert_actions_are_sha_pinned(job)


def test_canary_runs_weekly_and_not_on_pull_requests() -> None:
    """The unpinned run is a Monday schedule, not a pull request check.

    Mutation: change the cron day-of-week to ``*``. This test fails.
    """

    triggers = _load_workflow().get("on")
    assert isinstance(triggers, dict)
    schedules = triggers.get("schedule")
    assert isinstance(schedules, list) and len(schedules) == 1
    cron = schedules[0].get("cron") if isinstance(schedules[0], dict) else None
    assert isinstance(cron, str) and _is_weekly(cron), cron
    canary = _job("canary")
    assert canary.get("if") == CANARY_IF
    assert "pull_request" not in str(canary.get("if"))


def test_weekly_canary_does_not_pin_claude_or_hermes() -> None:
    """The canary installs current claude and hermes, not the trial pins.

    Mutation: change the canary pip line to ``hermes-agent==0.19.0``.
    This test fails.
    """

    script = _run_text(_job("canary"))
    assert "hermes-agent==" not in script
    assert not re.search(r"@anthropic-ai/claude-code@(?!latest\b)\S+", script)
    assert "2.1.281" not in script
    assert "0.19.0" not in script
    assert "2026.7.20" not in script
    assert "@anthropic-ai/claude-code@latest" in script
    assert re.search(r"(^|\s)hermes-agent($|\s)", script)
    step = _pytest_step(_job("canary"))
    assert step.get("env", {}).get("ALICE_TEST_REAL_HOSTS") == "1"


def test_weekly_canary_opens_an_ops_issue_on_failure() -> None:
    """A failed canary opens one [ops] issue, or comments if it is already open.

    Mutation: remove ``[ops]`` from the issue title. This test fails.
    """

    job = _job("canary")
    steps = [step for step in job["steps"] if step.get("if") == "failure()"]
    assert len(steps) == 1
    step = steps[0]
    uses = step.get("uses", "")
    assert uses.startswith("actions/github-script@")
    assert ACTION_SHA.fullmatch(uses.rsplit("@", 1)[1])
    script = step.get("with", {}).get("script", "")
    assert OPS_TITLE in script
    assert "github.rest.issues.listForRepo" in script
    assert "github.rest.issues.createComment" in script
    assert "await github.rest.issues.create({" in script
    assert "!issue.pull_request" in script
    assert job.get("permissions") == {"contents": "read", "issues": "write"}
    _assert_failure_fails_the_job(job)


def test_real_host_workflow_grants_contents_read_and_no_secrets() -> None:
    """The pinned job cannot open issues or read a secret.

    The canary's ``issues: write`` is the archive-maintenance pattern.
    Mutation: add ``pull-requests: write`` to the workflow permissions.
    This test fails.
    """

    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = _load_workflow()
    assert workflow.get("permissions") == {"contents": "read"}
    pinned = _job("pinned")
    assert pinned.get("permissions") == {"contents": "read"}
    assert "issues" not in pinned["permissions"]
    assert "secrets." not in text
    assert "GITHUB_TOKEN" not in text
    assert "GH_TOKEN" not in text
