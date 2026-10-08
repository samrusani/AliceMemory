#!/usr/bin/env python3
"""Require successful GitHub check runs on the exact release commit."""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
import re
from typing import Any
from urllib.request import Request, urlopen


# Protected-path metadata is enforced on the PR head and is not copied onto a
# merge commit. These are the exact-SHA push checks plus the new release gates.
REQUIRED_CHECKS = (
    "Secrets Scan (Gitleaks)",
    "CodeQL (python)",
    "CodeQL (javascript)",
    "Unit tests + live eval battery (SQLite)",
    "Python correctness lint, types, and release truth",
    "Installed wheel + sdist contract",
    "Python 3.13 install smoke",
    "Python 3.14 install smoke",
    "Integration tests (Postgres + pgvector, role separation)",
    "Read-budget maximum workload smoke (Postgres)",
    "Default surface integration smoke (Postgres)",
    "Web tests, types, accessibility, and budgets",
    "Semantic eval attestation (exact SHA)",
)

# These checks run on pull requests and collectively protect main. The semantic
# attestation is intentionally absent: it is dispatched against the accepted
# exact SHA after merge and is enforced by the publication workflow instead.
BRANCH_PROTECTION_REQUIRED_CHECKS = (
    "Secrets Scan (Gitleaks)",
    "CodeQL (python)",
    "CodeQL (javascript)",
    "Unit tests + live eval battery (SQLite)",
    "Python correctness lint, types, and release truth",
    "Installed wheel + sdist contract",
    "Python 3.13 install smoke",
    "Python 3.14 install smoke",
    "Integration tests (Postgres + pgvector, role separation)",
    "Default surface integration smoke (Postgres)",
    "Web tests, types, accessibility, and budgets",
    "Protected Path Upgrade Guardrails",
)


def validate_check_runs(check_runs: list[dict[str, Any]]) -> list[str]:
    runs_by_name: dict[str, list[tuple[int, str]]] = {}
    for check_run in check_runs:
        name = str(check_run.get("name", ""))
        conclusion = str(check_run.get("conclusion", ""))
        run_id = check_run.get("id")
        # GitHub check-run ids are monotonically increasing. Using the newest
        # run prevents an older successful attempt from masking a later failed
        # rerun on the same release commit.
        sortable_id = run_id if isinstance(run_id, int) else -1
        runs_by_name.setdefault(name, []).append((sortable_id, conclusion))

    issues: list[str] = []
    for required in REQUIRED_CHECKS:
        runs = runs_by_name.get(required, [])
        if not runs:
            issues.append(f"missing required exact-SHA check: {required}")
            continue
        _latest_id, latest_conclusion = max(runs, key=lambda item: item[0])
        if latest_conclusion != "success":
            issues.append(
                "latest required exact-SHA check did not succeed: "
                f"{required} ({latest_conclusion or 'pending'})"
            )
    return issues


def fetch_check_runs(*, repository: str, sha: str, token: str) -> list[dict[str, Any]]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError(f"invalid GitHub repository: {repository!r}")
    if re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
        raise ValueError(f"invalid commit SHA: {sha!r}")
    url = f"https://api.github.com/repos/{repository}/commits/{sha}/check-runs?per_page=100"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "alice-release-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - validated GitHub origin
        payload = json.load(response)
    check_runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(check_runs, list):
        raise ValueError("GitHub check-runs response is malformed")
    return [item for item in check_runs if isinstance(item, dict)]


def validate_branch_rulesets(rulesets: list[dict[str, Any]]) -> list[str]:
    """Require active main rules to include every release-critical CI context."""
    contexts: set[str] = set()
    status_rule_count = 0
    issues: list[str] = []
    for ruleset in rulesets:
        if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
            continue
        conditions = ruleset.get("conditions")
        ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
        includes = ref_name.get("include") if isinstance(ref_name, dict) else None
        excludes = ref_name.get("exclude") if isinstance(ref_name, dict) else None
        include_values = {str(value) for value in includes} if isinstance(includes, list) else set()
        exclude_values = {str(value) for value in excludes} if isinstance(excludes, list) else set()
        main_ref = "refs/heads/main"

        def matches_main(pattern: str) -> bool:
            return pattern in {"~ALL", "~DEFAULT_BRANCH"} or fnmatchcase(
                main_ref, pattern
            )

        applies_to_main = any(matches_main(value) for value in include_values) and not any(
            matches_main(value) for value in exclude_values
        )
        if not applies_to_main:
            continue
        rules = ruleset.get("rules")
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            status_rule_count += 1
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                issues.append("active main ruleset has malformed required_status_checks parameters")
                continue
            if parameters.get("strict_required_status_checks_policy") is not True:
                issues.append("active main required status checks are not strict")
            required = parameters.get("required_status_checks")
            if not isinstance(required, list):
                issues.append("active main ruleset has no required_status_checks list")
                continue
            for item in required:
                context = item.get("context") if isinstance(item, dict) else None
                if isinstance(context, str) and context:
                    contexts.add(context)
                else:
                    issues.append("active main ruleset contains a malformed check context")

    if status_rule_count == 0:
        issues.append("no active strict status-check ruleset applies to main")
        return issues
    expected = set(BRANCH_PROTECTION_REQUIRED_CHECKS)
    for missing in sorted(expected - contexts):
        issues.append(f"main ruleset is missing current required check: {missing}")
    return issues


def _fetch_github_json(*, url: str, token: str) -> object:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "alice-release-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - validated GitHub origin
        return json.load(response)


def fetch_branch_rulesets(*, repository: str, token: str) -> list[dict[str, Any]]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError(f"invalid GitHub repository: {repository!r}")
    base = f"https://api.github.com/repos/{repository}"
    summaries = _fetch_github_json(
        url=f"{base}/rulesets?includes_parents=true&per_page=100", token=token
    )
    if not isinstance(summaries, list):
        raise ValueError("GitHub rulesets response is malformed")
    details: list[dict[str, Any]] = []
    for summary in summaries:
        ruleset_id = summary.get("id") if isinstance(summary, dict) else None
        if not isinstance(ruleset_id, int):
            raise ValueError("GitHub rulesets response contains a malformed id")
        detail = _fetch_github_json(
            url=f"{base}/rulesets/{ruleset_id}?includes_parents=true", token=token
        )
        if not isinstance(detail, dict):
            raise ValueError("GitHub ruleset detail response is malformed")
        details.append(detail)
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--check-rulesets",
        action="store_true",
        help="Also require live active main rules to include all release-critical checks.",
    )
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        parser.error("GITHUB_TOKEN is required")

    issues = validate_check_runs(
        fetch_check_runs(repository=args.repo, sha=args.sha, token=token)
    )
    if args.check_rulesets:
        issues.extend(
            validate_branch_rulesets(
                fetch_branch_rulesets(repository=args.repo, token=token)
            )
        )
    if issues:
        print("Exact-SHA GitHub checks: FAIL")
        for issue in issues:
            print(f" - {issue}")
        return 1
    print(
        "Exact-SHA GitHub checks and branch rulesets: PASS"
        if args.check_rulesets
        else "Exact-SHA GitHub checks: PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
