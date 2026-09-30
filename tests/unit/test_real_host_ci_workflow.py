"""The real-host workflow stays path-filtered, pinned, and free of secrets.

A Linux trial ran ``claude doctor`` and ``hermes config get`` headless
with no credentials, and the two real-host tests passed. The pinned job
is therefore a failing check on the pull requests that touch host
install. Pytest calls the setup-python interpreter that installed Alice,
not the Hermes virtualenv. These tests read the workflow file. They do
not call GitHub, claude, or hermes.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
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
PINNED_IF = (
    "${{ github.event_name == 'pull_request' || (github.event_name == 'workflow_dispatch' "
    "&& (inputs.job == 'all' || inputs.job == 'pinned')) }}"
)
CANARY_IF = "${{ github.event_name == 'schedule' }}"
DISPATCH_JOBS = ("pinned", "hook-trial", "plugin-hook-trial", "marketplace-check")


def _dispatch_if(name: str) -> str:
    return (
        "${{ github.event_name == 'workflow_dispatch' && "
        f"(inputs.job == 'all' || inputs.job == '{name}') }}}}"
    )
OPS_TITLE = "[ops] real-host canary failure"
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")
_SETUP_PYTHON = "setup-python"
_HERMES_VENV_PYTHON = "hermes-venv"
_HERMES_VENV_BIN = "$RUNNER_TEMP/hermes-venv/bin"
_ALICE_PYTHON_RECORD = (
    'echo "ALICE_PYTHON=$(python -c \'import sys; print(sys.executable)\')" >> "$GITHUB_ENV"'
)


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
    found.add("scripts/fuzz_codex_config_writer.py")
    found.add("plugins/alice-memory/**")
    found.add(".claude-plugin/**")
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
        and "test_real_opencode_reads_the_written_config" in step.get("run", "")
        and "test_real_opencode_rejects_a_broken_alice_entry" in step.get("run", "")
        and "test_real_codex_reads_the_written_config" in step.get("run", "")
        and "test_real_codex_rejects_a_broken_alice_entry" in step.get("run", "")
        and "test_real_codex_tools_key_and_table" in step.get("run", "")
        and "test_real_claude_validates_the_plugin" in step.get("run", "")
        and "test_real_claude_plugin_install_and_run" in step.get("run", "")
        and "test_real_codex_runs_the_session_start_hook" in step.get("run", "")
        and "test_real_codex_ignores_json_hook_output" in step.get("run", "")
        and "test_real_codex_skips_a_hooks_file_with_an_http_handler" in step.get("run", "")
        and "test_real_codex_and_install_agree_on_hooks_json_number_and_spelling_rules"
        in step.get("run", "")
        and "test_real_codex_does_not_spill_a_large_brief" in step.get("run", "")
    ]
    assert len(matched) == 1
    return matched[0]


def test_pinned_job_runs_only_when_host_files_change() -> None:
    """Pull requests do not start this heavy job unless a host file changed.

    ``tests/unit/test_sleep_time_proposals.py`` imports
    ``alicebot_api.session_start_hook``, so it is a trigger, listed immediately
    after ``tests/unit/test_session_brief.py``. Mutation: delete
    ``host_install.py`` from ``pull_request.paths``, or drop the sleep
    proposals path. This test fails.
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
    session_brief = "tests/unit/test_session_brief.py"
    sleep_proposals = "tests/unit/test_sleep_time_proposals.py"
    assert session_brief in paths and sleep_proposals in paths
    assert paths.index(sleep_proposals) == paths.index(session_brief) + 1
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
    assert "opencode-ai@1.18.32" in script
    assert "@openai/codex@0.158.0" in script
    assert "codex-cli 0.158.0" in script
    assert "ran != 15" in script
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
    assert "opencode-ai@latest" in script
    assert "opencode-ai@1.18.32" not in script
    assert "@openai/codex@latest" in script
    assert "@openai/codex@0.158.0" not in script
    assert "ran != 15" in script
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
    assert "opencode" in script
    assert "codex" in script
    assert "SessionStart hook" in script
    assert "github.rest.issues.listForRepo" in script
    assert "github.rest.issues.createComment" in script
    assert "await github.rest.issues.create({" in script
    assert "!issue.pull_request" in script
    assert job.get("permissions") == {"contents": "read", "issues": "write"}
    _assert_failure_fails_the_job(job)


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _github_path_additions(script: str) -> list[str]:
    return re.findall(r'echo\s+"([^"]+)"\s*>>\s*"\$GITHUB_PATH"', script)


def _same_step_path_prefixes(script: str, end: int) -> list[str]:
    """PATH directories this step prepends before ``end``.

    A ``GITHUB_PATH`` line applies on the next step. ``PATH="dir:$PATH"``
    applies to later lines in this step. The last prepend is first.
    """

    prefixes: list[str] = []
    pattern = re.compile(r'(?:export\s+)?PATH="([^"]+):\$PATH"')
    for match in pattern.finditer(script[:end]):
        prefixes.insert(0, match.group(1))
    return prefixes


def _python_provider(directory: str) -> str | None:
    if directory == _SETUP_PYTHON:
        return _SETUP_PYTHON
    if directory.rstrip("/") == _HERMES_VENV_BIN:
        return _HERMES_VENV_PYTHON
    return None


def _first_python(path: list[str]) -> str | None:
    for directory in path:
        provider = _python_provider(directory)
        if provider:
            return provider
    return None


def _interpreter_tokens(header: str) -> list[tuple[str, int]]:
    pattern = re.compile(
        r'("\$ALICE_PYTHON"|\$RUNNER_TEMP/hermes-venv/bin/python|\bpython)(?=\s+-)'
    )
    return [(match.group(1), match.start()) for match in pattern.finditer(header)]


def _resolve_token(token: str, path: list[str], recorded: str | None) -> str:
    if token == '"$ALICE_PYTHON"':
        assert recorded is not None
        return recorded
    if token == "$RUNNER_TEMP/hermes-venv/bin/python":
        return _HERMES_VENV_PYTHON
    resolved = _first_python(path)
    assert token == "python"
    assert resolved is not None
    return resolved


def _pytest_interpreters(job: dict) -> list[str]:
    """Interpreters the pytest step would run, in order.

    ``setup-python`` is the interpreter that ran ``pip install -e '.[dev]'``.
    ``hermes-venv`` is ``$RUNNER_TEMP/hermes-venv/bin/python``. GitHub prepends
    each ``GITHUB_PATH`` entry, so that virtualenv hides ``python``.
    """

    path = [_SETUP_PYTHON]
    recorded: str | None = None
    install = "python -m pip install -e '.[dev]'"
    for step in _steps(job):
        script = step.get("run") or ""
        if install in script:
            record_at = script.find(_ALICE_PYTHON_RECORD)
            install_at = script.find(install)
            assert record_at != -1 and install_at < record_at
            path_at_record = _same_step_path_prefixes(script, record_at) + path
            recorded = _first_python(path_at_record)
            assert recorded is not None
        if (
            "test_real_claude_doctor_accepts_the_written_settings" in script
            and "test_real_hermes_loads_the_written_config" in script
        ):
            header, marker, _body = script.partition("<<'PY'")
            assert marker == "<<'PY'"
            tokens = _interpreter_tokens(header)
            assert tokens
            resolved: list[str] = []
            for token, index in tokens:
                live = _same_step_path_prefixes(header, index) + path
                resolved.append(_resolve_token(token, live, recorded))
            return resolved
        for addition in _github_path_additions(script):
            path.insert(0, addition)
    raise AssertionError("pytest step not found")


def _assert_hermes_stays_on_path(job: dict) -> None:
    additions: list[str] = []
    for step in _steps(job):
        additions.extend(_github_path_additions(step.get("run") or ""))
    assert _HERMES_VENV_BIN in additions
    assert "command -v hermes" in _pytest_step(job)["run"]


def test_pytest_uses_the_setup_python_interpreter_not_the_hermes_venv() -> None:
    """Pytest uses the interpreter that installed Alice, not the Hermes venv.

    The Hermes virtualenv bin is first on PATH so ``hermes`` resolves, and
    that install does not include pytest. Mutation: in the pinned job,
    change the pytest command to ``python -m pytest``. This test fails.
    """

    for name in ("pinned", "canary"):
        job = _job(name)
        resolved = _pytest_interpreters(job)
        assert resolved, name
        assert all(item == _SETUP_PYTHON for item in resolved), (name, resolved)
        _assert_hermes_stays_on_path(job)


def test_hook_trial_is_dispatch_only_pinned_and_uploads() -> None:
    """The hook trial runs only from workflow_dispatch and uploads its report.

    Mutation: add pull_request to the job if, install @latest, or remove
    always() from the upload step. This test fails.
    """

    job = _job("hook-trial")
    assert job.get("if") == _dispatch_if("hook-trial")
    script = _run_text(job)
    assert CLAUDE_NPM in script
    assert "@latest" not in script
    steps = job.get("steps")
    assert isinstance(steps, list)
    uploads = [step for step in steps if isinstance(step.get("uses"), str) and "upload-artifact@" in step["uses"]]
    assert len(uploads) == 1
    assert "always()" in str(uploads[0].get("if"))
    assert job.get("permissions") == {"contents": "read"}
    _assert_actions_are_sha_pinned(job)
    _assert_failure_fails_the_job(job)
    for name in ("pinned", "canary"):
        assert "real_host_hook_trial.py" not in _run_text(_job(name))


def _evaluate(node: ast.AST, names: dict[str, str]) -> bool | str:
    """Evaluate the only expression shapes these workflows use, and refuse the rest."""

    if isinstance(node, ast.Expression):
        return _evaluate(node.body, names)
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(item, names) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
        return _evaluate(node.left, names) == _evaluate(node.comparators[0], names)
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise AssertionError(f"unsupported expression node: {ast.dump(node)}")


def _job_runs(condition: str, event: str, job_input: str) -> bool:
    match = re.fullmatch(r"\$\{\{ (.*) \}\}", condition)
    assert match, condition
    text = match.group(1).replace("&&", " and ").replace("||", " or ")
    text = text.replace("github.event_name", "event_name").replace("inputs.job", "job_input")
    return bool(_evaluate(ast.parse(text, mode="eval"), {"event_name": event, "job_input": job_input}))


def _jobs_that_run(event: str, job_input: str) -> list[str]:
    jobs = _load_workflow()["jobs"]
    return [name for name, job in jobs.items() if _job_runs(job["if"], event, job_input)]


def test_dispatch_input_selects_which_dispatch_job_runs() -> None:
    """A dispatch with a job name runs that job alone. ``all`` runs every dispatch job.

    Pull requests still run only the pinned job, and the schedule only the canary.
    The ``if`` lines are evaluated, not compared as text. Mutation: drop the
    input test from one job, run the pinned job on any dispatch, add
    ``pull_request`` to a dispatch-only job, or leave a job out of the options.
    This test fails.
    """

    workflow = _load_workflow()
    dispatch = workflow["on"]["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    option = dispatch["inputs"]["job"]
    assert option["type"] == "choice" and option["default"] == "all"
    assert option["options"] == ["all", *DISPATCH_JOBS]
    dispatch_jobs = {
        name for name, job in workflow["jobs"].items() if "'workflow_dispatch'" in job["if"]
    }
    assert dispatch_jobs == set(DISPATCH_JOBS)
    for name in DISPATCH_JOBS:
        assert _jobs_that_run("workflow_dispatch", name) == [name], name
    assert _jobs_that_run("workflow_dispatch", "all") == list(
        job for job in workflow["jobs"] if job in DISPATCH_JOBS
    )
    assert "canary" not in _jobs_that_run("workflow_dispatch", "all")
    for stray in ("", "plugin-hook-trial", "all"):
        assert _jobs_that_run("pull_request", stray) == ["pinned"], stray
    assert _jobs_that_run("schedule", "") == ["canary"]
    assert _jobs_that_run("workflow_dispatch", "") == []


def test_plugin_hook_trial_is_dispatch_only_pinned_and_uploads_its_own_artifact() -> None:
    """The plugin hook trial runs the script on the pinned claude and always uploads its report.

    The upload has its own name, so it does not collide with the hook trial's
    artifact when ``all`` runs both. The runner setup is pinned too, so a later
    run reads on the same Python, Node and time limit. Mutation: install
    @latest, remove always() from the upload, drop the artifact name, point the
    upload at another directory, remove the ``Set up Python`` step, change its
    version, change ``node-version`` or ``timeout-minutes``, move the job to
    another runner OS, or drop the two updater flags from the job env. This
    test fails.
    """

    job = _job("plugin-hook-trial")
    assert job.get("if") == _dispatch_if("plugin-hook-trial")
    assert job.get("runs-on") == "ubuntu-latest"
    assert job.get("timeout-minutes") == 30
    assert job.get("env") == {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
    setup = {
        step["name"]: step.get("with", {})
        for step in _steps(job)
        if isinstance(step.get("uses"), str) and "setup-" in step["uses"]
    }
    assert setup == {
        "Set up Python": {"python-version": "3.12"},
        "Set up Node": {"node-version": "22.14.0"},
    }
    assert [step["name"] for step in _steps(job)] == [
        "Checkout",
        "Set up Python",
        "Set up Node",
        "Install pinned Claude Code",
        "Run the plugin hook trial",
        "Upload trial artifacts",
    ]
    script = _run_text(job)
    assert CLAUDE_NPM in script
    assert "@latest" not in script
    runs = [step["run"] for step in _steps(job) if "real_host_plugin_hook_trial.py" in step.get("run", "")]
    assert runs == [
        'python scripts/real_host_plugin_hook_trial.py run "$RUNNER_TEMP/plugin-hook-trial" '
        '"$RUNNER_TEMP/plugin-hook-work"'
    ]
    uploads = [step for step in _steps(job) if isinstance(step.get("uses"), str) and "upload-artifact@" in step["uses"]]
    assert len(uploads) == 1
    assert "always()" in str(uploads[0].get("if"))
    upload_with = uploads[0].get("with", {})
    assert upload_with.get("name") == "plugin-hook-trial"
    assert upload_with.get("path") == "${{ runner.temp }}/plugin-hook-trial/"
    assert upload_with.get("if-no-files-found") == "error"
    assert job.get("permissions") == {"contents": "read"}
    _assert_actions_are_sha_pinned(job)
    _assert_failure_fails_the_job(job)
    for name in ("pinned", "canary", "hook-trial", "marketplace-check"):
        assert "real_host_plugin_hook_trial.py" not in _run_text(_job(name))


MARKETPLACE_URL = "https://github.com/samrusani/AliceMemory.git"
MARKETPLACE_SHORTHAND = "samrusani/AliceMemory"
PLUGIN_LIST_SCRIPT = REPO_ROOT / "scripts" / "check_marketplace_plugin_list.py"
MARKETPLACE_STEPS = (
    "Checkout",
    "Set up Node",
    "Install pinned Claude Code",
    "Validate the committed marketplace",
    "Add the marketplace from the HTTPS URL",
    "Try the owner/repo shorthand",
)


def _marketplace_step(name: str) -> dict:
    matched = [step for step in _steps(_job("marketplace-check")) if step.get("name") == name]
    assert len(matched) == 1, name
    return matched[0]


def _run_plugin_list_check(version: str, payload: object) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plugins.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(PLUGIN_LIST_SCRIPT), version, str(path)],
            text=True,
            capture_output=True,
            check=False,
        )


def _plugin_row(version: str = "1.2.3", enabled: object = True, plugin_id: str = "alice-memory@alicememory") -> dict:
    return {"id": plugin_id, "enabled": enabled, "version": version}


def test_marketplace_check_compares_the_installed_version() -> None:
    """The dispatch-only job fails unless the installed plugin version matches.

    Every marketplace step hands the plugin list to one script, and that script
    is executed here. An inverted comparison, a dropped ``enabled`` test, a
    dropped id test or ``if False`` accepts a wrong row. Mutation: drop the
    version comparison from ``scripts/check_marketplace_plugin_list.py``. This
    test fails.
    """

    job = _job("marketplace-check")
    assert job.get("if") == _dispatch_if("marketplace-check")
    script = _run_text(job)
    assert "@anthropic-ai/claude-code@2.1.281" in script
    assert "claude plugin validate . --strict --json" in script
    assert 'claude plugin install "alice-memory@alicememory"' in script
    assert "python3 -c" in script
    assert 'version="${ref#v}"' in script
    assert "python3 - " not in script
    source = PLUGIN_LIST_SCRIPT.read_text(encoding="utf-8")
    assert 'str(row.get("version")) == expected_version' in source

    assert _run_plugin_list_check("1.2.3", [_plugin_row()]).returncode == 0
    assert _run_plugin_list_check("1.2.3", {"plugins": [_plugin_row()]}).returncode == 0
    assert _run_plugin_list_check("1.2.3", {"items": [_plugin_row()]}).returncode == 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(version="9.9.9")]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(version="v1.2.3")]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(enabled=False)]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(enabled="true")]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(enabled=1)]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(enabled=None)]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(plugin_id="other@alicememory")]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(), _plugin_row()]).returncode != 0
    assert _run_plugin_list_check("1.2.3", [_plugin_row(), {"id": "x@y", "enabled": True, "version": "1"}]).returncode != 0
    assert _run_plugin_list_check("1.2.3", []).returncode != 0
    assert _run_plugin_list_check("1.2.3", {"plugins": "not a list"}).returncode != 0
    assert _run_plugin_list_check("1.2.3", "not a list").returncode != 0
    assert _run_plugin_list_check("1.2.3", [[_plugin_row()]]).returncode != 0


def test_plugin_list_check_fails_on_a_missing_or_broken_file_and_on_bad_arguments(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    for arguments in (
        ["1.2.3", str(tmp_path / "missing.json")],
        ["1.2.3", str(broken)],
        ["1.2.3"],
        ["", str(broken)],
    ):
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_LIST_SCRIPT), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 1, arguments
        assert completed.stderr.strip(), arguments


def test_marketplace_check_adds_the_marketplace_by_path_url_and_shorthand() -> None:
    """The job adds the marketplace from ./, from the HTTPS URL, then tries the shorthand.

    Each form gets its own fresh HOME, installs ``alice-memory@alicememory``
    and lists it. The path and URL steps fail the job. The shorthand step is
    the only step that may not. The URL in the second step is the one the
    marketplace file pins for the plugin source, so the two cannot drift.

    Mutations: drop the URL step; change its URL; run any ``claude plugin``
    command without ``HOME="$home"``; reuse one home for two steps; drop the
    list check from the URL step; mark the URL step ``continue-on-error``;
    drop ``continue-on-error`` from the shorthand step; change the shorthand;
    drop ``timeout 180`` or ``< /dev/null`` from a shorthand command; drop
    ``2>&1`` from the shorthand add. This test fails.
    """

    job = _job("marketplace-check")
    assert [step["name"] for step in _steps(job)] == list(MARKETPLACE_STEPS)
    assert job.get("continue-on-error") in (None, False)
    assert job.get("permissions") == {"contents": "read"}
    assert job.get("timeout-minutes") == 30
    _assert_actions_are_sha_pinned(job)

    path_step = _marketplace_step("Validate the committed marketplace")
    url_step = _marketplace_step("Add the marketplace from the HTTPS URL")
    shorthand_step = _marketplace_step("Try the owner/repo shorthand")

    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["source"]["url"] == MARKETPLACE_URL

    for step in (path_step, url_step):
        assert step.get("continue-on-error") in (None, False), step["name"]
    assert path_step.get("if") is None
    assert url_step.get("if") == "${{ !cancelled() }}"
    assert shorthand_step.get("if") == "${{ !cancelled() }}"
    # The workflow loader here reads the YAML word true as a string.
    assert shorthand_step.get("continue-on-error") in (True, "true")
    assert shorthand_step.get("timeout-minutes") == 15
    assert shorthand_step.get("id") == "shorthand"

    sources = {
        "path": (path_step, "claude plugin marketplace add ./ < /dev/null"),
        "url": (url_step, f"claude plugin marketplace add {MARKETPLACE_URL} < /dev/null"),
        "shorthand": (shorthand_step, f'claude plugin marketplace add "$source_arg" < /dev/null'),
    }
    for label, (step, add_command) in sources.items():
        run = step["run"]
        assert run.count("mktemp -d") == 1, label
        assert add_command in run, label
        assert 'claude plugin install "alice-memory@alicememory" < /dev/null' in run, label
        assert "claude plugin list --json" in run, label
        assert "scripts/check_marketplace_plugin_list.py" in run, label
        commands = 0
        for line in run.splitlines():
            if line.lstrip().startswith(("echo", "#")):
                continue
            if re.search(r"\bclaude plugin (marketplace|install|list)\b", line):
                commands += 1
                assert line.lstrip().startswith('HOME="$home" '), (label, line)
        assert commands == 3, label
    assert f'source_arg="{MARKETPLACE_SHORTHAND}"' in shorthand_step["run"]
    # The shorthand can hang, so each of its three claude commands runs under a
    # 180 second timeout, with stdin from /dev/null, in its own HOME.
    shorthand_commands = [
        line.strip()
        for line in shorthand_step["run"].splitlines()
        if re.search(r"\bclaude plugin (marketplace|install|list)\b", line)
        and not line.lstrip().startswith(("echo", "#"))
    ]
    assert len(shorthand_commands) == 3
    for command in shorthand_commands:
        assert command.startswith('HOME="$home" timeout 180 claude plugin '), command
        assert " < /dev/null" in command, command
    assert shorthand_commands[0].endswith('< /dev/null > "$add_log" 2>&1')
    assert "plugins.json" in path_step["run"]
    assert "plugins-https.json" in url_step["run"]
    assert "plugins-shorthand.json" in shorthand_step["run"]
    # The expected version is read once, from the marketplace file, and shared.
    assert path_step["run"].count("EXPECTED_PLUGIN_VERSION") == 1
    assert 'echo "EXPECTED_PLUGIN_VERSION=$version" >> "$GITHUB_ENV"' in path_step["run"]
    assert "EXPECTED_PLUGIN_VERSION" in url_step["run"]
    assert "EXPECTED_PLUGIN_VERSION" in shorthand_step["run"]
    assert 'python3 scripts/check_marketplace_plugin_list.py "$version"' in path_step["run"]
    assert 'python3 scripts/check_marketplace_plugin_list.py "$expected"' in url_step["run"]


STDIN_SENTINEL = "unredirected stdin\n"


class _StubbedClaude:
    """Runs a workflow step's script with a stand-in for claude, timeout and python3.

    The real claude is never reached: PATH holds the stub directory, then
    ``/usr/bin:/bin``. The stand-in records the HOME and the arguments of each
    call and answers from the environment. The step's own HOME is a scratch
    directory, so a call made without ``HOME="$home"`` still touches nothing
    real.
    """

    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.runner_home = tmp_path / "runner-home"
        self.runner_home.mkdir()
        self.runner_temp = tmp_path / "runner-temp"
        self.runner_temp.mkdir()
        self.tmpdir = tmp_path / "tmp"
        self.tmpdir.mkdir()
        self.log = tmp_path / "calls.log"
        self.stdin_log = tmp_path / "stdin.log"
        self.timeout_log = tmp_path / "timeouts.log"
        self.output = tmp_path / "github-output"
        self.summary = tmp_path / "github-summary"
        self.github_env = tmp_path / "github-env"
        for path in (self.log, self.stdin_log, self.timeout_log, self.output, self.summary, self.github_env):
            path.write_text("", encoding="utf-8")
        claude = self.bin / "claude"
        claude.write_text(
            "#!/bin/bash\n"
            'echo "$HOME|$*" >> "$STUB_LOG"\n'
            'size="$(cat | wc -c)"\n'
            'echo "$((size))" >> "$STUB_STDIN_LOG"\n'
            'case "$2" in\n'
            "  marketplace)\n"
            '    if [ "$STUB_ADD_STREAM" = stderr ]; then echo "$STUB_ADD_OUTPUT" >&2; else echo "$STUB_ADD_OUTPUT"; fi\n'
            '    exit "${STUB_ADD_STATUS:-0}" ;;\n'
            '  install) exit "${STUB_INSTALL_STATUS:-0}" ;;\n'
            '  list) echo "$STUB_LIST_JSON"; exit "${STUB_LIST_STATUS:-0}" ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        timeout = self.bin / "timeout"
        timeout.write_text(
            '#!/bin/bash\necho "$1|$2" >> "$STUB_TIMEOUT_LOG"\nshift\nexec "$@"\n',
            encoding="utf-8",
        )
        for path in (claude, timeout):
            path.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.path = f"{self.bin}:/usr/bin:/bin"
        found = shutil.which("claude", path=self.path)
        assert found == str(claude), found

    def run(self, step: dict, **stub: str) -> subprocess.CompletedProcess[str]:
        script = self.root / "step.sh"
        script.write_text(step["run"], encoding="utf-8")
        env = {
            "PATH": self.path,
            "HOME": str(self.runner_home),
            "RUNNER_TEMP": str(self.runner_temp),
            "TMPDIR": str(self.tmpdir),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_ENV": str(self.github_env),
            "STUB_LOG": str(self.log),
            "STUB_STDIN_LOG": str(self.stdin_log),
            "STUB_TIMEOUT_LOG": str(self.timeout_log),
            "STUB_LIST_JSON": json.dumps([_plugin_row("0.19.0")]),
            "EXPECTED_PLUGIN_VERSION": "0.19.0",
            **stub,
        }
        env = {key: value for key, value in env.items() if value is not None}
        return subprocess.run(
            ["/bin/bash", "-e", str(script)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            # A command that does not redirect stdin reads this and the stub logs it.
            input=STDIN_SENTINEL,
        )

    def calls(self) -> list[tuple[str, str]]:
        rows = [line.split("|", 1) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [(home, arguments) for home, arguments in rows]

    def stdin_sizes(self) -> list[int]:
        """Bytes of stdin each claude call received, in call order."""

        return [int(line) for line in self.stdin_log.read_text(encoding="utf-8").splitlines()]

    def timeouts(self) -> list[tuple[str, str]]:
        """The duration and the command of each ``timeout`` call, in call order."""

        rows = [line.split("|", 1) for line in self.timeout_log.read_text(encoding="utf-8").splitlines()]
        return [(duration, command) for duration, command in rows]


def test_path_step_reads_the_expected_version_and_adds_from_a_fresh_home(tmp_path: Path) -> None:
    """Step one records the marketplace ref as the version, then adds ./ into its own HOME.

    Mutation: add the marketplace without ``HOME="$home"``, skip the list
    check, or write the ref with its ``v``. This test fails.
    """

    stub = _StubbedClaude(tmp_path)
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    ref = marketplace["plugins"][0]["source"]["ref"]
    version = ref.removeprefix("v")
    step = _marketplace_step("Validate the committed marketplace")

    completed = stub.run(step, EXPECTED_PLUGIN_VERSION=None, STUB_LIST_JSON=json.dumps([_plugin_row(version)]))

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert stub.github_env.read_text(encoding="utf-8") == f"EXPECTED_PLUGIN_VERSION={version}\n"
    calls = stub.calls()
    assert [arguments for _, arguments in calls] == [
        "plugin validate . --strict --json",
        "plugin marketplace add ./",
        "plugin install alice-memory@alicememory",
        "plugin list --json",
    ]
    homes = {home for home, arguments in calls if "validate" not in arguments}
    assert len(homes) == 1
    assert homes != {str(stub.runner_home)}

    wrong = _StubbedClaude(tmp_path / "wrong")
    failed = wrong.run(step, EXPECTED_PLUGIN_VERSION=None, STUB_LIST_JSON=json.dumps([_plugin_row("9.9.9")]))
    assert failed.returncode != 0


def test_url_step_adds_the_https_url_installs_and_checks_the_list(tmp_path: Path) -> None:
    """Step two adds the HTTPS URL into a fresh HOME and fails the job on any miss.

    Mutation: change the URL, drop the install, drop the list check, drop the
    ``${EXPECTED_PLUGIN_VERSION:?}`` guard, or add ``|| true`` to a command.
    This test fails.
    """

    step = _marketplace_step("Add the marketplace from the HTTPS URL")
    stub = _StubbedClaude(tmp_path / "ok")

    completed = stub.run(step)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = stub.calls()
    assert [arguments for _, arguments in calls] == [
        f"plugin marketplace add {MARKETPLACE_URL}",
        "plugin install alice-memory@alicememory",
        "plugin list --json",
    ]
    assert len({home for home, _ in calls}) == 1
    assert calls[0][0] != str(stub.runner_home)

    failures = {
        "add": {"STUB_ADD_STATUS": "1"},
        "install": {"STUB_INSTALL_STATUS": "1"},
        "list": {"STUB_LIST_STATUS": "1"},
        "version": {"STUB_LIST_JSON": json.dumps([_plugin_row("9.9.9")])},
        "enabled": {"STUB_LIST_JSON": json.dumps([_plugin_row("0.19.0", enabled=False)])},
        "id": {"STUB_LIST_JSON": json.dumps([_plugin_row("0.19.0", plugin_id="other@alicememory")])},
        "unset version": {"EXPECTED_PLUGIN_VERSION": None},
    }
    for label, overrides in failures.items():
        bad = _StubbedClaude(tmp_path / label.replace(" ", "-"))
        result = bad.run(step, **overrides)
        assert result.returncode != 0, label
        if label == "unset version":
            assert "the expected plugin version was not read" in result.stderr
            assert bad.calls() == []


# Each case is the stub's answers, the result the step records, a fragment of
# the reason it gives, and how many claude calls the step makes before it stops.
# The add output of a failed add is on stderr, which is where ssh and git write
# it. Each of the three SSH signs has a case of its own, so dropping any one
# alternative from the step's grep fails a case.
SHORTHAND_CASES = {
    "works": (
        {"STUB_ADD_OUTPUT": "marketplace added on stdout"},
        "works",
        "the add and the install worked, and the plugin list held alice-memory@alicememory 0.19.0, enabled",
        3,
    ),
    "ssh": (
        {
            "STUB_ADD_STATUS": "128",
            "STUB_ADD_STREAM": "stderr",
            "STUB_ADD_OUTPUT": "git@github.com: Permission denied (publickey).",
        },
        "add-failed",
        "cloned over SSH and the runner has no SSH key (exit 128)",
        1,
    ),
    "ssh-permission-denied": (
        {"STUB_ADD_STATUS": "128", "STUB_ADD_STREAM": "stderr", "STUB_ADD_OUTPUT": "Permission denied (publickey)."},
        "add-failed",
        "cloned over SSH and the runner has no SSH key (exit 128)",
        1,
    ),
    "ssh-address": (
        {
            "STUB_ADD_STATUS": "128",
            "STUB_ADD_STREAM": "stderr",
            "STUB_ADD_OUTPUT": "could not clone from git@github.com:samrusani/AliceMemory.git",
        },
        "add-failed",
        "cloned over SSH and the runner has no SSH key (exit 128)",
        1,
    ),
    "host-key": (
        {"STUB_ADD_STATUS": "128", "STUB_ADD_STREAM": "stderr", "STUB_ADD_OUTPUT": "Host key verification failed."},
        "add-failed",
        "cloned over SSH",
        1,
    ),
    "other": (
        {"STUB_ADD_STATUS": "1", "STUB_ADD_STREAM": "stderr", "STUB_ADD_OUTPUT": "repository not found"},
        "add-failed",
        "the add failed with exit 1, and the output above says why",
        1,
    ),
    "other-on-stdout": (
        {"STUB_ADD_STATUS": "1", "STUB_ADD_OUTPUT": "repository not found on stdout"},
        "add-failed",
        "the add failed with exit 1, and the output above says why",
        1,
    ),
    "timeout": (
        {"STUB_ADD_STATUS": "124", "STUB_ADD_OUTPUT": ""},
        "add-failed",
        "did not finish in 180 seconds",
        1,
    ),
    "install": ({"STUB_INSTALL_STATUS": "3"}, "install-failed", "the add worked, and the install failed with exit 3", 2),
    "list": (
        {"STUB_LIST_STATUS": "4"},
        "list-failed",
        "the add and the install worked, and the plugin list failed with exit 4",
        3,
    ),
    "mismatch": (
        {"STUB_LIST_JSON": json.dumps([_plugin_row("9.9.9")])},
        "list-mismatch",
        "the plugin list did not hold alice-memory@alicememory 0.19.0, enabled",
        3,
    ),
}


@pytest.mark.parametrize("case", sorted(SHORTHAND_CASES))
def test_shorthand_step_reports_each_outcome_and_never_fails(case: str, tmp_path: Path) -> None:
    """Whatever the shorthand does, the step ends 0, says what happened, and records it.

    The failure is an annotation, a line in the log, a step summary and the
    step output ``result``. A clone over SSH is named as that only when the
    output shows one, and each of the three signs of one is enough. The step
    stops at the first failed command, so a failed add never reaches the
    install. Every claude call runs under ``timeout 180`` and with stdin from
    ``/dev/null``. The add output, stdout and stderr both, is printed in the
    step log above the reason, because the reason can say "the output above".

    Mutation: drop ``exit 0``, drop ``set +e``, drop the ``result`` output,
    treat every add failure as SSH, run the install after a failed add, drop
    ``HOME="$home"``, drop ``timeout 180`` from any claude call, drop the
    ``cat`` of the add log, drop ``2>&1`` from the add, drop one alternative
    of the SSH grep, or drop ``< /dev/null`` from any claude call. This test
    fails.
    """

    overrides, result, message, calls_made = SHORTHAND_CASES[case]
    stub = _StubbedClaude(tmp_path)
    step = _marketplace_step("Try the owner/repo shorthand")

    completed = stub.run(step, **overrides)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert stub.output.read_text(encoding="utf-8") == f"result={result}\n"
    combined = completed.stdout
    verdict = f"claude plugin marketplace add {MARKETPLACE_SHORTHAND}: {result},"
    assert verdict in combined
    assert message in combined
    raw = overrides.get("STUB_ADD_OUTPUT", "")
    if raw:
        assert raw in combined, (completed.stdout, completed.stderr)
        assert raw not in completed.stderr
        assert combined.index(raw) < combined.index(verdict)
    if result == "works":
        assert "::warning" not in combined
    else:
        assert f"::warning title=Marketplace shorthand did not work::claude plugin marketplace add {MARKETPLACE_SHORTHAND} gave {result}:" in combined
        assert "::notice" not in combined
    if case in {"other", "other-on-stdout", "timeout"}:
        assert "SSH" not in combined
    summary = stub.summary.read_text(encoding="utf-8")
    assert "### Marketplace shorthand" in summary
    assert f"**{result}**" in summary
    calls = stub.calls()
    assert len(calls) == calls_made
    assert calls[0][1] == f"plugin marketplace add {MARKETPLACE_SHORTHAND}"
    assert {home for home, _ in calls} != {str(stub.runner_home)}
    assert len({home for home, _ in calls}) == 1
    if result in {"add-failed"}:
        assert all("install" not in arguments for _, arguments in calls)
    # Every claude call is wrapped in timeout 180, and none reads the step's stdin.
    assert stub.timeouts() == [("180", "claude")] * calls_made
    assert stub.stdin_sizes() == [0] * calls_made


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


def _collected_count(node_id: str) -> int:
    """How many tests pytest collects for a node id, read from the source.

    A plain function is one test. Each literal ``parametrize`` list multiplies
    it. A parametrize whose values are not a literal list or tuple fails the
    test, so a new shape is decided here rather than guessed.
    """

    path, _, function = node_id.partition("::")
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]
    assert len(matches) == 1, node_id
    count = 1
    for decorator in matches[0].decorator_list:
        if not (isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "pytest.mark.parametrize"):
            continue
        values = decorator.args[1] if len(decorator.args) > 1 else None
        assert isinstance(values, (ast.List, ast.Tuple)), (node_id, "non-literal parametrize")
        count *= len(values.elts)
    return count


def test_the_gate_count_is_the_listed_real_host_tests_in_both_jobs() -> None:
    """Both jobs list the same real-host tests, each one exists, and ``ran != N`` is what they collect.

    A merge from main can add tests to one side of the list and leave the gate
    number on the other side's count. The gate would then fail every run or,
    if loosened to pass, stop catching a skipped test. The count is read from
    the workflow text and compared with the node ids in the same step, where a
    parametrized id counts once per case (the OpenCode read test runs twice).

    Mutations: change either ``ran != N`` to another number; drop a test from
    one job's list (the step lookup finds no step, because it needs every
    test named in one step); list a test that no longer exists in its file;
    list one test twice; count a parametrized test once. This test fails.
    """

    listed: dict[str, list[str]] = {}
    for name in ("pinned", "canary"):
        run = _pytest_step(_job(name))["run"]
        ids = re.findall(r"tests/unit/\w+\.py::test_real_\w+", run)
        assert ids, name
        assert len(ids) == len(set(ids)), (name, "a test is listed twice")
        gate = re.findall(r"if ran != (\d+) or skipped != 0", run)
        expected = sum(_collected_count(node_id) for node_id in ids)
        assert gate == [str(expected)], (name, gate, expected)
        listed[name] = ids
    assert sorted(listed["pinned"]) == sorted(listed["canary"])
    for node_id in listed["pinned"]:
        assert _collected_count(node_id) >= 1, node_id
