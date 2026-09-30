#!/usr/bin/env python3
"""Fail a pull request whose commits use an email outside the allowlist.

The range is the commits that would merge (`base..head`), not the history of
the base branch. Author email and committer email are both read. Every
allowed address is listed in full. No domain is allowed as a whole, and a
personal mailbox is never an allowlist entry.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Exact addresses, compared after trimming and lower-casing because GitHub
# reads an address without regard to case. Each one is the owner's GitHub
# noreply address, a GitHub identity, a bot, or a role account. A misspelled
# noreply address is not one of them and fails, which is why no domain is
# allowed as a whole.
# samrusani@users.noreply.github.com is the owner's GitHub noreply address.
# 14844597+samrusani@users.noreply.github.com is the same address in the
# id form GitHub uses for commits made on github.com (web edits and
# suggestions); 14844597 is the owner's GitHub account id.
# noreply@github.com is GitHub's own noreply identity. History records it
# as the committer on web merges. The committer name is GitHub.
# cursoragent@cursor.com is the Cursor agent role address. It is this
# environment's git user.email and it already appears in history. Open
# pull requests from the paused external team still carry it.
# 49699333+dependabot[bot]@users.noreply.github.com is Dependabot. History
# records it as the author of its pull request commits.
# 41898282+github-actions[bot]@users.noreply.github.com is the standard
# address of the github-actions bot. It does not appear in this repository's
# history. It is listed so a workflow that commits as that bot is not
# rejected.
EXACT_ALLOWLIST = frozenset(
    {
        "samrusani@users.noreply.github.com",
        "14844597+samrusani@users.noreply.github.com",
        "noreply@github.com",
        "cursoragent@cursor.com",
        "49699333+dependabot[bot]@users.noreply.github.com",
        "41898282+github-actions[bot]@users.noreply.github.com",
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
    return email.strip().lower() in EXACT_ALLOWLIST


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
