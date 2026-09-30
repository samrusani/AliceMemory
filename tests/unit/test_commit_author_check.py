"""Commit author and committer emails on a pull request must be allowlisted.

The self-test plants bad-author@example.com on a commit inside the merge
range. Allowing every address, or returning no commits for that range, makes
this test fail by assertion.

The allowlist is exact addresses only. No domain is allowed as a whole, so a
misspelling of the owner's GitHub noreply address fails.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import scripts.check_commit_authors as commit_authors


REPO_ROOT = Path(__file__).resolve().parents[2]
BAD_EMAIL = "bad-author@example.com"
OWNER_NOREPLY = "samrusani@users.noreply.github.com"
DEPENDABOT = "49699333+dependabot[bot]@users.noreply.github.com"
GITHUB_ACTIONS = "41898282+github-actions[bot]@users.noreply.github.com"
ALLOWED_ADDRESSES = (
    OWNER_NOREPLY,
    "noreply@github.com",
    "cursoragent@cursor.com",
    DEPENDABOT,
    GITHUB_ACTIONS,
)
# One letter dropped, one letter doubled, and a letter swapped, each at the
# noreply domain. None of them is the owner's address.
MISSPELLED_OWNER_NOREPLY = (
    "samrusan@users.noreply.github.com",
    "samrusanni@users.noreply.github.com",
    "samrusoni@users.noreply.github.com",
)


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


def test_ignores_a_disallowed_email_that_landed_on_base_after_the_branch_point(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The range is base..head, so a commit only the base has is not the pull request's.

    The pull request branched from the fork point. The base has moved on with
    a commit that carries a disallowed address. A symmetric range (base...head)
    would count that commit against the pull request.

    Mutation: change ``base..head`` to ``base...head`` in commits_in_range.
    This test fails.
    """

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, author="noreply@github.com", committer="noreply@github.com", message="fork point")
    _git(repo, "checkout", "-b", "feature")
    head = _commit(repo, author=OWNER_NOREPLY, committer=OWNER_NOREPLY, message="pull request")
    _git(repo, "checkout", "main")
    base = _commit(repo, author=BAD_EMAIL, committer=BAD_EMAIL, message="landed on base after the branch point")
    assert _git(repo, "merge-base", base, head).strip() not in {base, head}

    code, output = _run(repo, base, head, capsys)

    assert code == 0, output
    assert BAD_EMAIL not in output
    assert "PASS (1 commits)" in output


def test_allows_every_listed_address_in_the_pull_request_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each listed address passes as author and as committer inside the range.

    Mutation: drop any one address from EXACT_ALLOWLIST. This test fails and
    the failure names that commit.
    """

    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author="noreply@github.com",
        committer="noreply@github.com",
        message="base",
    )
    head = base
    for email in ALLOWED_ADDRESSES:
        _commit(repo, author=email, committer="noreply@github.com", message=f"author {email}")
        head = _commit(repo, author="noreply@github.com", committer=email, message=f"committer {email}")

    code, output = _run(repo, base, head, capsys)

    assert code == 0, output
    assert f"PASS ({2 * len(ALLOWED_ADDRESSES)} commits)" in output


@pytest.mark.parametrize("email", MISSPELLED_OWNER_NOREPLY)
def test_rejects_a_misspelled_owner_noreply_address_in_the_range(
    email: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A noreply address that is not the owner's fails, as author and as committer.

    The old check allowed any address at the GitHub noreply domain, so these
    passed. Mutation: allow the noreply domain again. This test fails.
    """

    repo = _init_repo(tmp_path / "repo")
    base = _commit(
        repo,
        author=OWNER_NOREPLY,
        committer="noreply@github.com",
        message="base",
    )
    as_author = _commit(repo, author=email, committer=OWNER_NOREPLY, message="misspelled author")
    as_committer = _commit(repo, author=OWNER_NOREPLY, committer=email, message="misspelled committer")

    code, output = _run(repo, base, as_committer, capsys)

    assert code == 1
    assert f"{as_author} author {email}" in output
    assert f"{as_author} committer " not in output
    assert f"{as_committer} committer {email}" in output
    assert f"{as_committer} author " not in output


@pytest.mark.parametrize(
    "email",
    (
        *ALLOWED_ADDRESSES,
        "CursorAgent@Cursor.com",
        "SamRusani@Users.NoReply.GitHub.com",
        "49699333+Dependabot[bot]@users.noreply.github.com",
        "41898282+GitHub-Actions[bot]@users.noreply.github.com",
        " noreply@github.com ",
    ),
)
def test_allowlist_accepts_the_listed_addresses(email: str) -> None:
    assert commit_authors.email_allowed(email)


@pytest.mark.parametrize(
    "email",
    (
        BAD_EMAIL,
        "person@example.com",
        "alex@gmail.com",
        "sam@example.org",
        "",
        "not-an-email",
        "@users.noreply.github.com",
        *MISSPELLED_OWNER_NOREPLY,
        # Another account at the GitHub noreply domain, with and without an id.
        "example-user@users.noreply.github.com",
        "12345+example-user@users.noreply.github.com",
        # The bots with the wrong id, and with no id.
        "49699334+dependabot[bot]@users.noreply.github.com",
        "dependabot[bot]@users.noreply.github.com",
        "41898283+github-actions[bot]@users.noreply.github.com",
        "github-actions[bot]@users.noreply.github.com",
        # Lookalike domains: each allowed address with something added to the end.
        "user@users.noreply.github.com.example.com",
        f"{OWNER_NOREPLY}.example.com",
        "noreply@github.com.example.com",
        "noreply@github.co",
        "cursoragent@cursor.com.example.org",
        "cursoragent@cursor.co",
        f"{DEPENDABOT}.example.com",
        f"{GITHUB_ACTIONS}.example.com",
        # Lookalike local parts: something added in front of each address.
        f"x{OWNER_NOREPLY}",
        "evilnoreply@github.com",
        "not-cursoragent@cursor.com",
        f"x{DEPENDABOT}",
        f"x{GITHUB_ACTIONS}",
        # Lookalike domains: the local part kept, the domain replaced.
        "samrusani@users-noreply.github.com",
        "samrusani@users.noreply.github.org",
        "noreply@githu6.com",
        "cursoragent@cursor.example",
        # Two addresses in one field.
        f"{OWNER_NOREPLY}, {BAD_EMAIL}",
        f"{OWNER_NOREPLY}@{BAD_EMAIL}",
    ),
)
def test_allowlist_rejects_other_addresses(email: str) -> None:
    assert not commit_authors.email_allowed(email)


def test_exact_allowlist_is_only_the_listed_addresses() -> None:
    """The allowlist is these five addresses, all lower-case, none a domain.

    Mutation: add an address, add a bare domain, or restore the noreply
    domain rule. This test fails. The lower-case check matters because the
    comparison lower-cases the commit's address: an entry with a capital
    letter could never match.
    """

    assert commit_authors.EXACT_ALLOWLIST == frozenset(ALLOWED_ADDRESSES)
    assert len(ALLOWED_ADDRESSES) == 5
    for entry in commit_authors.EXACT_ALLOWLIST:
        assert entry == entry.strip().lower()
        assert entry.count("@") == 1
        assert entry.split("@", 1)[0], entry
    assert not hasattr(commit_authors, "GITHUB_NOREPLY_DOMAIN")


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
    """The workflow checks origin/<base>..head, including after a retarget."""

    workflow = (REPO_ROOT / ".github/workflows/commit-author-check.yml").read_text(encoding="utf-8")
    trigger = workflow.split("permissions:", 1)[0]
    pull_request = trigger.split("pull_request:", 1)[1]
    checkout = workflow.split("uses: actions/checkout@", 1)[1].split("- name: Reject", 1)[0]

    assert "\n  pull_request:\n" in trigger
    assert "\n  push:\n" not in workflow
    assert "contents: read" in workflow
    assert "fetch-depth: 0" in checkout
    assert "\n          persist-credentials: false\n" in checkout
    assert "scripts/check_commit_authors.py" in workflow
    for activity in ("opened", "reopened", "synchronize", "edited"):
        assert f"\n      - {activity}\n" in pull_request
    assert '--base "origin/${{ github.base_ref }}"' in workflow
    assert '--head "${{ github.event.pull_request.head.sha }}"' in workflow
    assert "github.event.pull_request.base.sha" not in workflow


def test_noreply_comment_records_the_web_merge_committer() -> None:
    source = (REPO_ROOT / "scripts/check_commit_authors.py").read_text(encoding="utf-8")

    assert "noreply@github.com is GitHub's own noreply identity." in source
    assert "as the committer on web merges." in source
    assert 'history author "GitHub"' not in source
