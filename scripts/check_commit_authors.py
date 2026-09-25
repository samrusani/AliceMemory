#!/usr/bin/env python3
"""Fail a pull request whose commits use an email outside the allowlist.

The range is the commits that would merge (`base..head`), not the history of
the base branch. Author email and committer email are both read. A personal
mailbox is never an allowlist entry.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# GitHub's private noreply form. Any non-empty local part is allowed, which
# covers machine accounts that use this form. History includes
# 49699333+dependabot[bot]@users.noreply.github.com. github-actions does not
# appear in this repository's history, so it has no exact entry; the standard
# github-actions noreply address still matches this domain.
GITHUB_NOREPLY_DOMAIN = "users.noreply.github.com"

# Exact addresses. Each one is a machine or role account.
# noreply@github.com is GitHub's own noreply identity. History records it
# as the committer on web merges. The committer name is GitHub.
# cursoragent@cursor.com is the Cursor agent role address. It is this
# environment's git user.email and it already appears in history.
EXACT_ALLOWLIST = frozenset(
    {
        "noreply@github.com",
        "cursoragent@cursor.com",
    }
)


@dataclass(frozen=True)
class CommitEmails:
    sha: str
    author_email: str
    committer_email: str


@dataclass(frozen=True)
class Violation:
    sha: str
    role: str
    email: str


def email_allowed(email: str) -> bool:
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        return False
    local, domain = normalized.split("@", 1)
    if not local or not domain or any(character.isspace() for character in normalized):
        return False
    if normalized in EXACT_ALLOWLIST:
        return True
    return domain == GITHUB_NOREPLY_DOMAIN


def commits_in_range(repo: Path, base: str, head: str) -> list[CommitEmails]:
    """Commits reachable from head and not from base: the merge range."""

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--format=%H%x1f%ae%x1f%ce",
            f"{base}..{head}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git log failed"
        raise RuntimeError(detail)
    commits: list[CommitEmails] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[0]:
            raise RuntimeError(f"unexpected git log line: {line!r}")
        sha, author_email, committer_email = parts
        commits.append(
            CommitEmails(
                sha=sha,
                author_email=author_email,
                committer_email=committer_email,
            )
        )
    return commits


def find_violations(commits: list[CommitEmails]) -> list[Violation]:
    violations: list[Violation] = []
    for commit in commits:
        if not email_allowed(commit.author_email):
            violations.append(Violation(commit.sha, "author", commit.author_email))
        if not email_allowed(commit.committer_email):
            violations.append(Violation(commit.sha, "committer", commit.committer_email))
    return violations


def format_failure(violations: list[Violation]) -> str:
    lines = ["Commit author check: FAIL"]
    for item in violations:
        lines.append(f"{item.sha} {item.role} {item.email}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Base SHA. Commits already on this side are ignored.")
    parser.add_argument("--head", required=True, help="Head SHA. Commits that would merge are base..head.")
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        commits = commits_in_range(args.repo, args.base, args.head)
    except RuntimeError as exc:
        print(f"Commit author check: FAIL\n{exc}", file=sys.stdout)
        return 1
    violations = find_violations(commits)
    if violations:
        print(format_failure(violations))
        return 1
    print(f"Commit author check: PASS ({len(commits)} commits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
