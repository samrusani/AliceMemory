"""The read-budget job picks a different base on a push to main than on a pull request."""

from pathlib import Path
import subprocess

import yaml


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _init(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")


def test_push_uses_the_first_parent_and_a_pull_request_uses_the_merge_base(tmp_path: Path) -> None:
    """Dry-run of scripts/select_read_budget_base.sh for both events.

    Mutation: on a push, print the merge base with origin/main (the push compares the code with itself).
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    parent = _commit(repo, "parent")
    head = _commit(repo, "push")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    script = Path(__file__).resolve().parents[2] / "scripts" / "select_read_budget_base.sh"
    push_base = subprocess.run(["sh", str(script), "push"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert push_base == parent
    assert push_base != head

    _git(repo, "checkout", "-b", "feature", parent)
    feature = _commit(repo, "feature")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "-m", "merge feature", "feature")
    merge = _git(repo, "rev-parse", "HEAD")
    assert merge != head and feature != merge
    pull_base = subprocess.run(
        ["sh", str(script), "pull_request"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert pull_base == head
    assert pull_base != merge


def test_the_job_step_names_which_base_each_event_uses() -> None:
    """Every job that measures a read budget picks its base the same way.

    The smoke job, the integration shards and the SQLite budget parts each check out the
    history and add the same merge base worktree.

    Mutation: change the command of the step in any one of the three jobs (drop the push
    branch, or the pull request branch), or remove the step from one of them.
    """

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text())
    commands = []
    for job_id in ("python-integration", "python-integration-shards", "python-sqlite-read-budgets"):
        step = next(
            item for item in workflow["jobs"][job_id]["steps"]
            if item.get("name") == "Merge base with main for the read-budget gate"
        )
        command = step["run"]
        assert "read-budget comparison: push to main, first parent" in command, job_id
        assert "read-budget comparison: pull request, merge base with main" in command, job_id
        assert "select_read_budget_base.sh push" in command, job_id
        assert "select_read_budget_base.sh pull_request" in command, job_id
        commands.append(command)
    assert len(set(commands)) == 1
