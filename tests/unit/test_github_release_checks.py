from __future__ import annotations

from pathlib import Path
import re

import yaml

import scripts.check_github_release_checks as release_checks


REPO_ROOT = Path(__file__).resolve().parents[2]


def _workflow_job_display_names(path: Path, *, skip_job_ids: frozenset[str] = frozenset()) -> set[str]:
    """Read job-level names and expand the matrix keys a name uses.

    A matrix key takes its values from the key's own list or, when the key only appears in
    ``include`` entries, from those entries.
    """
    jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
    names: set[str] = set()
    for job_id, job in jobs.items():
        raw_name = job.get("name")
        if job_id in skip_job_ids or not isinstance(raw_name, str):
            continue
        matrix_keys = re.findall(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", raw_name)
        matrix = job.get("strategy", {}).get("matrix", {})
        expanded = {raw_name}
        for matrix_key in matrix_keys:
            values = matrix.get(matrix_key)
            if values is None:
                values = [entry[matrix_key] for entry in matrix.get("include", []) if matrix_key in entry]
            assert values, matrix_key
            expanded = {
                name.replace(f"${{{{ matrix.{matrix_key} }}}}", str(value))
                for name in expanded
                for value in values
            }
        names.update(expanded)
    return names


def _jobs_the_required_summary_covers(path: Path, summary_name: str) -> frozenset[str]:
    """Ids of the jobs whose result the job named ``summary_name`` requires to be a success."""
    jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
    summaries = [job for job in jobs.values() if job.get("name") == summary_name]
    assert len(summaries) == 1, summary_name
    needs = summaries[0].get("needs", [])
    return frozenset([needs] if isinstance(needs, str) else needs)


def test_exact_sha_check_gate_requires_every_success() -> None:
    runs = [
        {"name": name, "conclusion": "success"}
        for name in release_checks.REQUIRED_CHECKS
    ]

    assert release_checks.validate_check_runs(runs) == []


def test_exact_sha_check_gate_reports_missing_and_failed() -> None:
    runs = [
        {"name": name, "conclusion": "success"}
        for name in release_checks.REQUIRED_CHECKS[2:]
    ]
    runs.append(
        {"name": release_checks.REQUIRED_CHECKS[0], "conclusion": "failure"}
    )

    issues = release_checks.validate_check_runs(runs)

    assert any("did not succeed" in issue for issue in issues)
    assert any(release_checks.REQUIRED_CHECKS[1] in issue for issue in issues)


def test_exact_sha_check_gate_uses_latest_rerun_only() -> None:
    required = release_checks.REQUIRED_CHECKS[0]
    successful_latest = [
        {"name": name, "id": 10, "conclusion": "success"}
        for name in release_checks.REQUIRED_CHECKS
    ]
    successful_latest.append({"name": required, "id": 9, "conclusion": "failure"})
    assert release_checks.validate_check_runs(successful_latest) == []

    failed_latest = [
        {"name": name, "id": 10, "conclusion": "success"}
        for name in release_checks.REQUIRED_CHECKS
    ]
    failed_latest.append({"name": required, "id": 11, "conclusion": "failure"})

    issues = release_checks.validate_check_runs(failed_latest)
    assert any(required in issue and "latest" in issue for issue in issues)


def test_required_checks_match_actual_workflow_job_display_names() -> None:
    tests_path = REPO_ROOT / ".github/workflows/tests.yml"
    summary = "Unit tests + live eval battery (SQLite)"
    # The unit shards, the eval job and the coverage job are not required one by one.
    # The summary that keeps the required name needs each of them and fails unless
    # every one succeeded, so the required name stands for all of them.
    covered_by_summary = _jobs_the_required_summary_covers(tests_path, summary)
    tests_jobs = _workflow_job_display_names(tests_path, skip_job_ids=covered_by_summary)
    semantic_jobs = _workflow_job_display_names(
        REPO_ROOT / ".github/workflows/semantic-release-gate.yml"
    )
    externally_managed_jobs = {
        "Secrets Scan (Gitleaks)",
        "CodeQL (python)",
        "CodeQL (javascript)",
    }

    assert summary in tests_jobs
    assert set(release_checks.REQUIRED_CHECKS) == (
        tests_jobs | semantic_jobs | externally_managed_jobs
    )
    # A job left out of the required set only because the summary covers it must
    # not also be required, and must not carry a required name.
    covered_names = _workflow_job_display_names(tests_path) - tests_jobs
    assert covered_names
    assert not covered_names & set(release_checks.REQUIRED_CHECKS)
    assert not covered_names & set(release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)


def test_default_surface_smoke_is_required_for_prs_and_exact_sha_release() -> None:
    context = "Default surface integration smoke (Postgres)"

    assert context in release_checks.REQUIRED_CHECKS
    assert context in release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS


def _active_main_ruleset(
    *contexts: str,
    strict: bool = True,
    includes: tuple[str, ...] = ("~DEFAULT_BRANCH",),
    excludes: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": list(includes), "exclude": list(excludes)}
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": strict,
                    "required_status_checks": [
                        {"context": context} for context in contexts
                    ],
                },
            }
        ],
    }


def test_branch_ruleset_check_accepts_current_strict_contexts() -> None:
    ruleset = _active_main_ruleset(
        *release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS
    )

    assert release_checks.validate_branch_rulesets([ruleset]) == []


def test_branch_ruleset_check_rejects_renamed_missing_and_nonstrict_contexts() -> None:
    contexts = list(release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)
    contexts.remove("Web tests, types, accessibility, and budgets")
    contexts.append("Web tests, lint, build")

    issues = release_checks.validate_branch_rulesets(
        [_active_main_ruleset(*contexts, strict=False)]
    )

    assert any("not strict" in issue for issue in issues)
    assert any("missing current required check" in issue for issue in issues)


def test_branch_ruleset_check_allows_additional_organization_contexts() -> None:
    ruleset = _active_main_ruleset(
        *release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS,
        "Organization release policy",
    )

    assert release_checks.validate_branch_rulesets([ruleset]) == []


def test_branch_ruleset_check_matches_and_excludes_ref_patterns() -> None:
    wildcard = _active_main_ruleset(
        *release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS,
        includes=("refs/heads/m*",),
    )
    excluded = _active_main_ruleset(
        *release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS,
        includes=("refs/heads/*",),
        excludes=("refs/heads/m*",),
    )

    assert release_checks.validate_branch_rulesets([wildcard]) == []
    assert release_checks.validate_branch_rulesets([excluded]) == [
        "no active strict status-check ruleset applies to main"
    ]


def test_branch_ruleset_check_ignores_inactive_or_other_branch_rulesets() -> None:
    inactive = _active_main_ruleset(*release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)
    inactive["enforcement"] = "disabled"
    other = _active_main_ruleset(*release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)
    other["conditions"] = {
        "ref_name": {"include": ["refs/heads/develop"], "exclude": []}
    }

    issues = release_checks.validate_branch_rulesets([inactive, other])

    assert issues == ["no active strict status-check ruleset applies to main"]
