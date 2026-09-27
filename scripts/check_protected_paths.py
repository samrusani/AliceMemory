#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


@dataclass(frozen=True)
class ProtectedArea:
    label: str
    patterns: tuple[str, ...]


PROTECTED_AREAS: tuple[ProtectedArea, ...] = (
    ProtectedArea(
        label="memory schema",
        patterns=(
            "apps/api/alembic/versions/*.py",
            "apps/api/src/alicebot_api/store.py",
            "apps/api/src/alicebot_api/legacy_store/*.py",
            "apps/api/src/alicebot_api/vnext_store.py",
            "apps/api/src/alicebot_api/sqlite_store.py",
            "apps/api/src/alicebot_api/vnext_stores/*.py",
            "apps/api/src/alicebot_api/vnext_stores/postgres/*.py",
            "apps/api/src/alicebot_api/vnext_stores/sqlite/*.py",
            "apps/api/src/alicebot_api/db.py",
            "apps/api/src/alicebot_api/memory.py",
            "apps/api/src/alicebot_api/contracts.py",
            "apps/api/src/alicebot_api/_contracts/*.py",
        ),
    ),
    ProtectedArea(
        label="evidence pipeline",
        patterns=(
            "apps/api/src/alicebot_api/artifacts.py",
            "apps/api/src/alicebot_api/compiler.py",
            "apps/api/src/alicebot_api/continuity_capture.py",
            "apps/api/src/alicebot_api/continuity_evidence.py",
            "apps/api/src/alicebot_api/continuity_explainability.py",
            "apps/api/src/alicebot_api/continuity_review.py",
            "apps/api/src/alicebot_api/importers/*.py",
        ),
    ),
    ProtectedArea(
        label="trust rules",
        patterns=(
            "apps/api/src/alicebot_api/contracts.py",
            "apps/api/src/alicebot_api/_contracts/*.py",
            "apps/api/src/alicebot_api/memory.py",
            "apps/api/src/alicebot_api/trusted_fact_promotions.py",
        ),
    ),
    ProtectedArea(
        label="promotion logic",
        patterns=(
            "apps/api/src/alicebot_api/memory.py",
            "apps/api/src/alicebot_api/trusted_fact_promotions.py",
        ),
    ),
    ProtectedArea(
        label="continuity APIs",
        patterns=(
            "apps/api/src/alicebot_api/cli.py",
            "apps/api/src/alicebot_api/cli/*.py",
            "apps/api/src/alicebot_api/contracts.py",
            "apps/api/src/alicebot_api/_contracts/*.py",
            "apps/api/src/alicebot_api/main.py",
            "apps/api/src/alicebot_api/routers/*.py",
            "apps/api/src/alicebot_api/mcp_server.py",
            "apps/api/src/alicebot_api/mcp_tools.py",
            "apps/api/src/alicebot_api/mcp/*.py",
            "apps/web/lib/api.ts",
        ),
    ),
)

REQUIRED_NARRATIVE_SECTIONS: tuple[str, ...] = (
    "compatibility impact",
    "migration / rollout",
    "operator action",
    "validation",
    "rollback",
)

PLACEHOLDER_VALUES = {
    "",
    "-",
    "n/a",
    "na",
    "none",
    "not applicable",
    "same as above",
    "tbd",
    "todo",
    "pending",
}

_UPGRADE_OVERVIEW_RE = re.compile(
    r"^## Upgrade Overview\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_SUBSECTION_RE = re.compile(
    r"^### (?P<title>.+?)\s*$\n(?P<body>.*?)(?=^###\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_CHECKBOX_RE = re.compile(r"^- \[(?P<checked>[ xX])\] (?P<label>.+?)\s*$", re.MULTILINE)


def _normalize_heading(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _matches_pattern(path: str, pattern: str) -> bool:
    return PurePosixPath(path).match(pattern)


def categorize_files(changed_files: Iterable[str]) -> dict[str, list[str]]:
    matched: dict[str, list[str]] = {}
    for raw_path in changed_files:
        path = raw_path.strip()
        if not path:
            continue
        for area in PROTECTED_AREAS:
            if any(_matches_pattern(path, pattern) for pattern in area.patterns):
                matched.setdefault(area.label, []).append(path)
    return matched


def _strip_comments(value: str) -> str:
    return re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()


def _has_meaningful_content(value: str) -> bool:
    cleaned = _strip_comments(value)
    collapsed = " ".join(cleaned.split()).strip(" -")
    if not collapsed:
        return False
    if collapsed.lower() in PLACEHOLDER_VALUES:
        return False
    return len(re.sub(r"[^a-zA-Z0-9]+", "", collapsed)) >= 8


def extract_upgrade_sections(pr_body: str) -> dict[str, str]:
    match = _UPGRADE_OVERVIEW_RE.search(pr_body)
    if match is None:
        return {}

    overview_body = match.group("body").strip()
    sections: dict[str, str] = {}
    for subsection in _SUBSECTION_RE.finditer(overview_body):
        title = _normalize_heading(subsection.group("title"))
        sections[title] = subsection.group("body").strip()
    return sections


def parse_checked_areas(section_body: str) -> set[str]:
    checked: set[str] = set()
    for match in _CHECKBOX_RE.finditer(section_body):
        if match.group("checked").lower() == "x":
            checked.add(_normalize_heading(match.group("label")))
    return checked


def validate_upgrade_overview(pr_body: str, touched_areas: dict[str, list[str]]) -> list[str]:
    if not touched_areas:
        return []

    sections = extract_upgrade_sections(pr_body)
    if not sections:
        return ["Protected paths were modified, but the PR body is missing the required `## Upgrade Overview` section."]

    errors: list[str] = []
    protected_area_section = sections.get("protected areas")
    if protected_area_section is None:
        errors.append("`### Protected Areas` is missing from `## Upgrade Overview`.")
    else:
        checked_areas = parse_checked_areas(protected_area_section)
        normalized_touched_labels = {_normalize_heading(area_label): area_label for area_label in touched_areas}
        missing_checked = sorted(
            (
                normalized_touched_labels[normalized_label]
                for normalized_label in set(normalized_touched_labels) - checked_areas
            ),
            key=str.lower,
        )
        if missing_checked:
            errors.append(
                "The checked protected areas do not cover the touched categories: " + ", ".join(missing_checked) + "."
            )

    for section_name in REQUIRED_NARRATIVE_SECTIONS:
        section_body = sections.get(section_name)
        if section_body is None:
            errors.append(f"`### {section_name.title()}` is missing from `## Upgrade Overview`.")
            continue
        if not _has_meaningful_content(section_body):
            errors.append(f"`### {section_name.title()}` must contain explicit upgrade notes.")

    return errors


def changed_files_between(base_sha: str, head_sha: str) -> list[str]:
    command = ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def load_pull_request_event(event_path: str) -> tuple[str, str, str]:
    with open(event_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("GitHub event payload does not contain a pull_request object.")

    base_sha = pull_request["base"]["sha"]
    head_sha = pull_request["head"]["sha"]
    body = pull_request.get("body") or ""
    return base_sha, head_sha, body


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail CI when protected path changes are missing explicit upgrade metadata."
    )
    parser.add_argument(
        "--event-path",
        required=True,
        help="Path to the GitHub event payload JSON.",
    )
    args = parser.parse_args()

    try:
        base_sha, head_sha, pr_body = load_pull_request_event(args.event_path)
        changed_files = changed_files_between(base_sha, head_sha)
    except Exception as exc:
        print(f"Unable to evaluate protected-path guardrails: {exc}")
        return 1

    touched_areas = categorize_files(changed_files)
    if not touched_areas:
        print("No protected paths changed; upgrade metadata is not required.")
        return 0

    errors = validate_upgrade_overview(pr_body, touched_areas)
    if not errors:
        print("Protected-path upgrade metadata is present.")
        for label, files in sorted(touched_areas.items()):
            print(f"- {label}: {', '.join(sorted(files))}")
        return 0

    print("Protected-path guardrail failure:")
    for label, files in sorted(touched_areas.items()):
        print(f"- touched {label}: {', '.join(sorted(files))}")
    for error in errors:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
