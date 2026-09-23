"""Commit author and committer emails on a pull request must be allowlisted.

The self-test plants bad-author@example.com on a commit inside the merge
range. Allowing every address, or returning no commits for that range, makes
this test fail by assertion.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import scripts.check_commit_authors as commit_authors


REPO_ROOT = Path(__file__).resolve().parents[2]
BAD_EMAIL = "bad-author@example.com"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "user.name", "Alex")
    _git(path, "config", "user.email", "noreply@github.com")
    return path


def _commit(repo: Path, *, author: str, committer: str, message: str) -> str:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Alex"
    env["GIT_AUTHOR_EMAIL"] = author
    env["GIT_COMMITTER_NAME"] = "Alex"
    env["GIT_COMMITTER_EMAIL"] = committer
    _git(repo, "commit", "--allow-empty", "-m", message, env=env)
    recorded = _git(repo, "log", "-1", "--format=%ae%x1f%ce").strip()
    assert recorded == f"{author}\x1f{committer}"
    return _git(repo, "rev-parse", "HEAD").strip()


def _run(
    repo: Path,
    base: str,
    head: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    code = commit_authors.main(["--repo", str(repo), "--base", base, "--head", head])
    return code, capsys.readouterr().out


def test_rejects_made_up_author_address(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """bad-author@example.com in the merge range fails and names the SHA."""

    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="base",
    )
    head = _commit(
        repo,
        author=BAD_EMAIL,
        committer=BAD_EMAIL,
        message="outside the allowlist",
    )

    code, output = _run(repo, base, head, capsys)

    assert code == 1
    assert f"{head} author {BAD_EMAIL}" in output
    assert f"{head} committer {BAD_EMAIL}" in output


def test_rejects_disallowed_author_when_committer_is_allowed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="base",
    )
    head = _commit(
        repo,
        author=BAD_EMAIL,
        committer="cursoragent@cursor.com",
        message="author only",
    )

    code, output = _run(repo, base, head, capsys)

    assert code == 1
    assert f"{head} author {BAD_EMAIL}" in output
    assert f"{head} committer " not in output


def test_rejects_disallowed_committer_when_author_is_allowed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="base",
    )
    head = _commit(
        repo,
        author="49699333+dependabot[bot]@users.noreply.github.com",
        committer=BAD_EMAIL,
        message="committer only",
    )

    code, output = _run(repo, base, head, capsys)

    assert code == 1
    assert f"{head} committer {BAD_EMAIL}" in output
    assert f"{head} author " not in output


def test_ignores_disallowed_email_outside_the_pull_request_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, author=BAD_EMAIL, committer=BAD_EMAIL, message="already on base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    head = _commit(
        repo,
        author="noreply@github.com",
        committer="cursoragent@cursor.com",
        message="pull request",
    )

    code, output = _run(repo, base, head, capsys)

    assert code == 0
    assert BAD_EMAIL not in output
    assert "PASS (1 commits)" in output


def test_allows_role_and_noreply_addresses_in_the_pull_request_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="base",
    )
    _commit(
        repo,
        author="49699333+dependabot[bot]@users.noreply.github.com",
        committer="noreply@github.com",
        message="dependabot",
    )
    head = _commit(
        repo,
        author="cursoragent@cursor.com",
        committer="example-user@users.noreply.github.com",
        message="agent and noreply",
    )

    code, output = _run(repo, base, head, capsys)

    assert code == 0
    assert "PASS (2 commits)" in output


@pytest.mark.parametrize(
    "email",
    (
        "noreply@github.com",
        "cursoragent@cursor.com",
        "CursorAgent@Cursor.com",
        "49699333+dependabot[bot]@users.noreply.github.com",
        "example-user@users.noreply.github.com",
    ),
)
def test_allowlist_accepts_role_and_noreply_addresses(email: str) -> None:
    assert commit_authors.email_allowed(email)


@pytest.mark.parametrize(
    "email",
    (
        BAD_EMAIL,
        "person@example.com",
        "",
        "not-an-email",
        "@users.noreply.github.com",
        "user@users.noreply.github.com.example.com",
        "noreply@github.com.example.com",
    ),
)
def test_allowlist_rejects_other_addresses(email: str) -> None:
    assert not commit_authors.email_allowed(email)


def test_exact_allowlist_is_only_machine_and_role_addresses() -> None:
    assert commit_authors.EXACT_ALLOWLIST == frozenset(
        {
            "noreply@github.com",
            "cursoragent@cursor.com",
        }
    )
    assert commit_authors.GITHUB_NOREPLY_DOMAIN == "users.noreply.github.com"


def test_git_failure_is_a_failed_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path / "repo")
    head = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="only",
    )

    code, output = _run(repo, "0123456789abcdef0123456789abcdef01234567", head, capsys)

    assert code == 1
    assert "Commit author check: FAIL" in output


def test_pull_request_workflow_reads_the_merge_range() -> None:
    workflow = (REPO_ROOT / ".github/workflows/commit-author-check.yml").read_text(encoding="utf-8")

    assert "\n  pull_request:\n" in workflow
    assert "\n  push:\n" not in workflow
    assert "contents: read" in workflow
    assert "fetch-depth: 0" in workflow
    assert "scripts/check_commit_authors.py" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
