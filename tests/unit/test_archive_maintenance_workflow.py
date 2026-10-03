"""Archive maintenance installs packages in a job with no write scope, and a separate job opens its alert (DB-008).

The nightly job installs ``pip --upgrade pip`` and the project's dev extras by
version range. It used to hold ``issues: write`` for the whole workflow, so a
package that ran in it could open and comment on issues. The job now holds
``contents: read`` and its checkout keeps no credentials. A failed run opens its
``[ops]`` issue from ``archive-alert``, a job with ``issues: write``, no checkout,
no shell and no install. These tests read the workflow file and run the alert
script under ``node`` against a stand-in for the GitHub client. They do not call
GitHub.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "archive-maintenance.yml"
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")
ALERT_IF = "${{ always() && needs.archive-maintenance.result == 'failure' }}"
NODE = shutil.which("node")


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(name: str) -> dict:
    job = _workflow()["jobs"].get(name)
    assert isinstance(job, dict), name
    return job


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _alert_runs(condition: str, result: str) -> bool:
    match = re.fullmatch(r"\$\{\{ (.*) \}\}", condition)
    assert match, condition
    text = match.group(1).replace("&&", " and ").replace("||", " or ")
    text = text.replace("needs.archive-maintenance.result", "install_result")
    names = {"install_result": result, "always": lambda: True}

    def evaluate(node: ast.AST) -> object:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.BoolOp):
            values = [evaluate(item) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            equal = evaluate(node.left) == evaluate(node.comparators[0])
            return equal if isinstance(node.ops[0], ast.Eq) else not equal
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args:
            return names[node.func.id]()
        if isinstance(node, ast.Name):
            return names[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        raise AssertionError(f"unsupported expression node: {ast.dump(node)}")

    return bool(evaluate(ast.parse(text, mode="eval")))


def test_the_installing_job_holds_no_write_scope_and_no_token_path() -> None:
    """The job that installs packages holds contents: read and nothing that reaches a token.

    Mutation: add ``issues: write`` (or ``contents: write`` or ``id-token: write``)
    to the workflow or to the job; put a ``github-script`` step, a ``GITHUB_TOKEN``,
    ``GH_TOKEN``, ``github.token`` or ``secrets.`` reference back in it; remove
    ``persist-credentials: false`` from its checkout or set it to true; give the
    job ``outputs``. This test fails.
    """

    workflow = _workflow()
    job = _job("archive-maintenance")
    assert workflow["permissions"] == {"contents": "read"}
    assert job.get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert "outputs" not in job
    text = yaml.dump(job)
    for needle in ("github-script", "GITHUB_TOKEN", "GH_TOKEN", "github.token", "secrets."):
        assert needle not in text, needle
    checkouts = [step for step in _steps(job) if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert str(checkouts[0].get("with", {}).get("persist-credentials")).lower() == "false"


def test_archive_alert_is_the_only_issue_writer_and_installs_nothing() -> None:
    """The alert job holds issues: write and never installs, checks out, runs a shell or reads an output.

    It runs only when the maintenance job's own result is failure. Mutation: drop
    ``needs``; change the condition to ``!= 'success'`` or ``always()`` alone; add a
    ``run`` step, a checkout or a second action; put ``if: failure()`` back on the
    step; add a scope beside ``issues: write``; add a job-level ``env``,
    ``container``, ``services`` or ``outputs`` key; add a step ``env``; interpolate
    any ``${{ }}`` expression into the script; change the timeout. This test fails.
    """

    job = _job("archive-alert")
    assert set(job) == {"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "steps"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 5
    assert job["needs"] == "archive-maintenance"
    assert job["if"] == ALERT_IF
    assert job["permissions"] == {"issues": "write"}
    steps = _steps(job)
    assert [str(step.get("uses", "")).split("@")[0] for step in steps] == ["actions/github-script"]
    assert ACTION_SHA.fullmatch(str(steps[0]["uses"]).rsplit("@", 1)[1])
    assert set(steps[0]) == {"name", "uses", "with"}
    assert set(steps[0]["with"]) == {"script"}
    # A value from the job that installs packages would reach code that holds the write scope.
    assert "${{" not in steps[0]["with"]["script"]
    for result, expected in (("failure", True), ("success", False), ("skipped", False), ("cancelled", False)):
        assert _alert_runs(job["if"], result) is expected, result
    writers = [
        name for name, other in _workflow()["jobs"].items() if "write" in (other.get("permissions") or {}).values()
    ]
    assert writers == ["archive-alert"], writers


_RUNNER = """
const fs = require('fs');
const [scriptPath, contextJson, openIssuesJson] = process.argv.slice(2);
const script = fs.readFileSync(scriptPath, 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const calls = [];
const github = { rest: { issues: {
  listForRepo: async () => ({ data: JSON.parse(openIssuesJson) }),
  createComment: async (a) => { calls.push(['createComment', a.issue_number]); },
  create: async (a) => { calls.push(['create', a.title]); },
} } };
const context = Object.assign(
  { serverUrl: 'https://example.invalid', repo: { owner: 'o', repo: 'r' }, runId: 1, sha: 'abc' },
  JSON.parse(contextJson),
);
new AsyncFunction('github', 'context', script)(github, context)
  .then(() => console.log(JSON.stringify(calls)))
  .catch((error) => { console.log(JSON.stringify({ error: String(error) })); process.exit(1); });
"""


def _run_alert(tmp_path: Path, context: dict, open_issues: list[dict] | None = None) -> object:
    script = _steps(_job("archive-alert"))[0]["with"]["script"]
    (tmp_path / "alert.js").write_text(script, encoding="utf-8")
    (tmp_path / "runner.js").write_text(_RUNNER, encoding="utf-8")
    result = subprocess.run(
        [
            str(NODE),
            str(tmp_path / "runner.js"),
            str(tmp_path / "alert.js"),
            json.dumps(context),
            json.dumps(open_issues or []),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("context", "suffix"),
    [
        ({"eventName": "schedule", "payload": {"schedule": "17 2 * * *"}}, "nightly"),
        ({"eventName": "schedule", "payload": {"schedule": "23 4 * * 1"}}, "weekly"),
        ({"eventName": "workflow_dispatch", "payload": {"inputs": {"schedule": "manual"}}}, "manual"),
        ({"eventName": "workflow_dispatch", "payload": {"inputs": {"schedule": "weekly"}}}, "weekly"),
        ({"eventName": "workflow_dispatch", "payload": {"inputs": {"schedule": "nightly"}}}, "nightly"),
        # a value outside the three the workflow offers is named unknown, and is never run as code
        (
            {"eventName": "workflow_dispatch", "payload": {"inputs": {"schedule": "x`); process.exit(7); //"}}},
            "unknown",
        ),
        ({"eventName": "workflow_dispatch", "payload": {}}, "unknown"),
    ],
)
def test_the_alert_names_the_schedule_from_the_event_that_started_the_run(
    tmp_path: Path, context: dict, suffix: str
) -> None:
    """The title carries the schedule the run was started with, read from the event.

    Run under ``node`` with a stand-in for the GitHub client. Mutations, each one
    alone: read the schedule from a job output; map the weekly cron to nightly;
    take a dispatch input without the allowed list; drop the dispatch branch. This
    test fails.
    """

    calls = _run_alert(tmp_path, context)

    assert calls == [["create", f"[ops] archive maintenance failure ({suffix})"]]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_alert_comments_on_an_open_issue_with_the_same_title(tmp_path: Path) -> None:
    """An open issue with the title gets a comment, a pull request with the title does not, and none is created.

    Mutation: drop the ``!issue.pull_request`` test, or create an issue when one is
    open. This test fails.
    """

    context = {"eventName": "schedule", "payload": {"schedule": "17 2 * * *"}}
    title = "[ops] archive maintenance failure (nightly)"
    open_issues = [
        {"number": 4, "title": title, "pull_request": {"url": "x"}},
        {"number": 9, "title": title},
    ]

    calls = _run_alert(tmp_path, context, open_issues)

    assert calls == [["createComment", 9]]
