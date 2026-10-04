#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tomllib


ROOT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DESCRIPTION_RELATIVE_PATH = "docs/pypi-description.md"
_PACKAGE_DESCRIPTION_VERSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_PACKAGE_DESCRIPTION_STATE_PATTERN = re.compile(
    r"\b(?:latest\s+(?:published\s+)?release|candidate|release[ -]?gating|"
    r"unpublished|publication\s+pending)\b",
    flags=re.IGNORECASE,
)
_LATEST_RELEASE_NOTES_DOCS = ("README.md", "docs/vnext/README.md")
_LATEST_CHECKSUM_DOCS = ("ARCHITECTURE.md", "PRODUCT_BRIEF.md", "ROADMAP.md")
# An install line that names a release tag by hand: `git checkout vX.Y.Z`,
# `git clone --branch vX.Y.Z` or `--tag vX.Y.Z`. A tag in any of these forms is
# the version a reader installs, so it must be the latest published release.
_LITERAL_INSTALL_TAG_PATTERN = re.compile(
    r"(?:--tag|--branch|\bgit\s+checkout(?:\s+--detach)?)\s+[\"']?v(?P<version>\d+\.\d+\.\d+)\b"
)
# Where the living docs are. The root-level Markdown files are living docs too.
# Everything else in the repository (code, tests, fixtures) is not documentation.
LIVING_DOC_ROOTS: tuple[str, ...] = ("docs", "agent-skills", "plugins", ".ai", ".github")
# Dated records. They say what was true on their date, quote the install line
# and the tool counts of their own release, and are never rewritten, so no check
# of current behaviour reads them. This is the one list of the deliberately
# historical examples; a path is a file, or a folder and everything under it.
DATED_RECORD_PATHS: tuple[str, ...] = (
    "CHANGELOG.md",  # released sections quote the install line of their release
    "docs/release",  # release notes, tag plans and checklists, frozen at publication
    "docs/handoff",  # build handoffs, frozen when the work shipped
    "docs/archive",  # superseded pages, which say they describe nothing current
    "docs/plans",  # plans, each describing the surface of the day it was written
    "docs/adr",  # decision records
    "docs/reports",  # research reports
    "docs/issues",  # an issue write-up from one install
    "docs/roadmap-friction-first.md",  # a plan dated 2026-07-29
)
_PUBLISHED_FUTURE_STATE_PATTERN = re.compile(
    r"\b(?:will\s+be\s+(?:published|recorded|uploaded|created)|"
    r"after\s+publication|once\s+published)\b",
    flags=re.IGNORECASE,
)
# v0.10.3 is an immutable published baseline whose historical prose cannot be
# rewritten. Enforce publication-neutral notes for every later release.
_FUTURE_STATE_ENFORCEMENT_AFTER = (0, 10, 3)
_HISTORICAL_REMEDIATION_HANDOFF = Path("docs/handoff/2026-07-14-v0.10.4-remediation")
_HISTORICAL_REMEDIATION_MARKERS: dict[str, tuple[str, ...]] = {
    "README.md": (
        "Repair Batch 16 is the current bounded correction",
        "29-codepoint `chr()`-enumerated",
        "Batch 15 is historical, independently approved, and superseded",
    ),
    "BUILD_REPORT.md": (
        "## Historical Builder Repair Batch 16 dirty-tree scope and status",
        "### Historical Repair Batch 16 dirty-tree verification",
        "whitespace finding discovered",
        "Refreeze 17 changes only documentation-truth enforcement",
        "Independent review of the exact frozen Batch 16 carrier: **CHANGES",
    ),
    "ENGINEER_HANDOFF.md": (
        "## Builder Repair Batch 16 review delta",
        "U+001C–U+001F",
        "```md\n## Upgrade Overview",
        "- [x] memory schema",
        "- [x] continuity APIs",
    ),
    "FIX_MATRIX.md": (
        "## Builder Repair Batch 16 closure",
        "Embedding CAS Python-strip parity",
        "Repair Batch 16 is the current bounded correction",
        "Repair Batch 15 was independently approved",
        "Batch 16 review approved production semantics",
        "## Refreeze 17 documentation-truth closure",
        "Batch 15 is historical and superseded",
    ),
    "SURFACE_INVENTORY.md": (
        "## Repair Batch 16 correction appendix",
        "three embedding-CAS SQL consumers",
        "NBSP and U+001C",
    ),
}
_HISTORICAL_FINALIZATION_MARKERS: dict[str, tuple[str, ...]] = {
    "README.md": (
        "Code remediation commit `41641fbfa5dc8198bf47bad8849c828dbb519617`",
        "npm advisory endpoint commit `42b8c2d470a7535ec39d4028c2ef3868dcd4598a`",
        "At control-tower delivery, this documentation correction is intentionally uncommitted",
    ),
    "BUILD_REPORT.md": (
        "## Refreeze 17 exact-code handoff truth",
        "96a7f4d940bcf1154d31d730450e00935ba06341",
        "The final release SHA is deliberately not predicted",
    ),
    "ENGINEER_HANDOFF.md": (
        "Code boundary `42b8c2d470a7535ec39d4028c2ef3868dcd4598a`",
        "At control-tower delivery, the documentation correction itself is intentionally uncommitted",
        "Do not treat either code commit as the future release SHA",
    ),
    "FIX_MATRIX.md": (
        "## Refreeze 17 exact-code receipt",
        "code tree `96a7f4d940bcf1154d31d730450e00935ba06341`",
    ),
    "SURFACE_INVENTORY.md": (
        "Version sources were committed at `0.10.4`",
        "historical pre-edit inventory",
    ),
}
_HISTORICAL_REMEDIATION_FORBIDDEN_MARKERS: dict[str, tuple[str, ...]] = {
    "README.md": (
        "Repair Batch 14 is the current bounded correction",
        "Independent review and release approval remain pending",
        "all six groups",
    ),
    "BUILD_REPORT.md": (
        "## Builder Repair Batch 14 scope and current status",
        "### Current Repair Batch 14 verification",
        "Independent review of the exact fingerprinted Batch 14 carrier: **PENDING**",
        "Repair Batch 15 is the current bounded correction",
        "its six groups",
    ),
    "ENGINEER_HANDOFF.md": (
        "## Builder Repair Batch 14 review deltas",
        "scopes all six groups",
    ),
    "FIX_MATRIX.md": (
        "across the six groups",
        "## Builder Repair Batch 14 closure",
        "Repair Batch 15 is the current bounded correction",
    ),
    "SURFACE_INVENTORY.md": (
        "six memory/source/open-loop/artifact/entity/project groups",
        "## Repair Batch 14 correction appendix",
    ),
}
_HISTORICAL_FINALIZATION_FORBIDDEN_MARKERS: dict[str, tuple[str, ...]] = {
    "README.md": (
        "control-tower handoff for the uncommitted v0.10.4 remediation",
        "The tree is intentionally uncommitted. No version source was bumped",
    ),
    "BUILD_REPORT.md": (
        "Package version intentionally remains `0.10.3` until release-engineer finalization",
        "the package version remains `0.10.3`",
    ),
    "ENGINEER_HANDOFF.md": (
        "Review the uncommitted tree in this order",
        "Before the finalization commit, bump every governed version source to `0.10.4`",
    ),
    "FIX_MATRIX.md": ("v0.10.4 as uncommitted remediation",),
    "SURFACE_INVENTORY.md": (
        "Version sources remain `0.10.3` during this uncommitted remediation",
        "describing the current tree as uncommitted",
    ),
}

_ACTIVE_SPRINT_PACKET = Path(".ai/active/SPRINT_PACKET.md")
_ACTIVE_SPRINT_PACKET_MAX_LINES = 120
_ACTIVE_SPRINT_PACKET_MAX_BYTES = 8_192
_ACTIVE_SPRINT_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "stale live-repair ledger claim",
        re.compile(
            r"(?:\b(?:repair|remediation)\s+(?:batch|ledger)\s+\d+\b|"
            r"\b(?:live|current)\s+(?:repair|remediation)\s+ledger\b|"
            r"\bmandatory\s+repair\s+pass\s+(?:is\s+)?active\b|"
            r"\bcurrent\s+bounded\s+correction\b)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Phase 4 active-work claim",
        re.compile(
            r"(?:\bphase\s*4\b.{0,80}\b(?:active|underway|in\s+progress|"
            r"authorized|current\s+(?:work|sprint))\b|"
            r"\b(?:active|underway|in\s+progress|authorized|current\s+(?:work|sprint))"
            r"\b.{0,80}\bphase\s*4\b)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Alice OCR/transcription execution claim",
        re.compile(
            r"(?:\balice(?:\s+(?:itself|directly))?\s+"
            r"(?:performs?|executes?|runs?|provides?|transcribes?)\s+(?:\w+\s+){0,4}"
            r"(?:ocr|transcription)\b|"
            r"\b(?:ocr|transcription)\b(?:(?!\bnot\b).){0,60}\b"
            r"(?:performed|executed|run|provided)\s+by\s+alice\b)",
            flags=re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ControlDocTruthRule:
    relative_path: str
    required_markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VersionAlignedDocRule:
    relative_path: str
    candidate_pattern_templates: tuple[str, ...] = ()
    published_pattern_templates: tuple[str, ...] = ()


CONTROL_DOC_TRUTH_RULES: tuple[ControlDocTruthRule, ...] = (
    ControlDocTruthRule(
        relative_path=str(_ACTIVE_SPRINT_PACKET),
        required_markers=(
            "<!-- alice-sprint-scope: phase-3-complete -->",
            "Alice v0.12.0 Phase 3 structural refactor.",
            "Branch: `codex/v0120-phase3-structural-refactor`",
            "Structure only. Zero behavior change.",
            "Governed package and web version sources: intentionally held at `0.11.1`",
            "Text extraction happens outside Alice.",
            "Alice does not perform OCR or transcription.",
            "Phase 4 is out of scope for this packet.",
            "Stop after the Phase 3 handoff; do not begin Phase 4.",
        ),
    ),
    ControlDocTruthRule(
        relative_path="README.md",
        required_markers=(
            "**The continuity layer for AI agents.**",
            "make doctor",
            "ALICE_MCP_LEGACY_TOOLS=1",
            "## Status",
        ),
    ),
    ControlDocTruthRule(
        relative_path="ROADMAP.md",
        required_markers=(
            "## Baseline (Not Roadmap Work)",
            "## Next",
            "## Explicit Non-Goals For Now",
        ),
    ),
    ControlDocTruthRule(
        relative_path="RULES.md",
        required_markers=(
            "## No Fake Intelligence",
            "The vNext store is the canonical memory system.",
            "Continuity semantics must not fork by provider.",
        ),
    ),
    ControlDocTruthRule(
        relative_path="CURRENT_STATE.md",
        required_markers=(
            "## Snapshot",
            "## Release Boundary",
            "## Product Boundaries",
        ),
    ),
    ControlDocTruthRule(
        relative_path=".ai/handoff/CURRENT_STATE.md",
        required_markers=(
            "## Snapshot",
            "## Release Boundary",
            "## Product Boundaries",
        ),
    ),
    ControlDocTruthRule(
        relative_path="PRODUCT_BRIEF.md",
        required_markers=(
            "Alice is the continuity layer for AI agents",
            "## Non-Goals (Now)",
        ),
    ),
    ControlDocTruthRule(
        relative_path="ARCHITECTURE.md",
        required_markers=(
            "# Architecture",
            "## Scope Boundary",
            "## Current Architectural Posture",
        ),
    ),
    ControlDocTruthRule(
        relative_path="RELEASING.md",
        required_markers=(
            "# Releasing Alice",
            "ALICE_RELEASE_CONTROLS_ATTESTATION",
            "alice_release_controls_attestation_v1",
            "independent mandatory gates",
            "exact-SHA semantic report/attestation",
            "normal cross-module mypy",
            "core plus vNext per-file coverage",
            "structured `embedding_signature`",
            "nonempty case lists",
        ),
    ),
    ControlDocTruthRule(
        relative_path="docs/vnext/consolidation.md",
        required_markers=(
            "non-null vector presence",
            "ANN/vector-search capability remains optional",
        ),
    ),
    ControlDocTruthRule(
        relative_path="docs/vnext/README.md",
        required_markers=("# Alice vNext", "## Alpha Boundary"),
    ),
    ControlDocTruthRule(
        relative_path="docs/integrations/reference-paths.md",
        required_markers=("# Reference Integration Paths", "## Scope Guard"),
    ),
    ControlDocTruthRule(
        relative_path="docs/alpha/headless-ubuntu-install.md",
        required_markers=("# Headless Ubuntu Install", "## Headless Alpha Check"),
    ),
    ControlDocTruthRule(
        relative_path="docs/archive/planning/2026-04-08-context-compaction/README.md",
        required_markers=(
            "This folder preserves superseded planning and control material removed from the live docs during Context Compaction 01.",
        ),
    ),
    ControlDocTruthRule(
        relative_path="docs/archive/process/README.md",
        required_markers=(
            "historical build-process artifacts",
            "[Phase 2 and Phase 3 closeout history](phase2-phase3-closeout-history.md)",
        ),
    ),
    ControlDocTruthRule(
        relative_path="docs/archive/process/phase2-phase3-closeout-history.md",
        required_markers=(
            "# Phase 2 and Phase 3 Closeout History",
            "Historical record only.",
            "The former active runbooks were retired during the v0.11.0 Phase 1 periphery cut.",
            "This record does not authorize Phase 2 or Phase 3 work.",
            "../../../.ai/active/SPRINT_PACKET.md",
        ),
    ),
    ControlDocTruthRule(
        relative_path="docs/handoff/history/v0.10.4-repair-batches.md",
        required_markers=(
            "# v0.10.4 Repair-Batch History",
            "Entries are historical evidence.",
            "| 16 | Embedding-CAS semantics approved; documentation-truth correction followed |",
            "docs/release/v0.10.4-release-notes.md",
            "docs/release/v0.10.4-checksums.txt",
        ),
    ),
)

VERSION_ALIGNED_DOC_RULES: tuple[VersionAlignedDocRule, ...] = (
    VersionAlignedDocRule(
        relative_path="CURRENT_STATE.md",
        candidate_pattern_templates=(
            r"`v{version}`[^\n]*\bcandidate\b",
            r"^## What `v{version}` (?:Targets|Changes|Adds)$",
        ),
        published_pattern_templates=(
            r"`v{version}`[^\n]*\blatest published release\b",
            r"^## What `v{version}` Shipped$",
        ),
    ),
    VersionAlignedDocRule(
        relative_path=".ai/handoff/CURRENT_STATE.md",
        candidate_pattern_templates=(
            r"`v{version}`[^\n]*\bcandidate\b",
            r"^## What `v{version}` (?:Targets|Changes|Adds)$",
        ),
        published_pattern_templates=(
            r"`v{version}`[^\n]*\blatest published release\b",
            r"^## What `v{version}` Shipped$",
        ),
    ),
    VersionAlignedDocRule(
        relative_path="PRODUCT_BRIEF.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="ROADMAP.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="ARCHITECTURE.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published[^\n]*release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="README.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="RELEASING.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="docs/vnext/README.md",
        candidate_pattern_templates=(r"`v{version}`[^\n]*\bcandidate\b",),
        published_pattern_templates=(r"`v{version}`[^\n]*\blatest published release\b",),
    ),
    VersionAlignedDocRule(
        relative_path="docs/integrations/reference-paths.md",
        published_pattern_templates=(r"latest published `v{version}` baseline",),
    ),
    VersionAlignedDocRule(
        relative_path="docs/alpha/headless-ubuntu-install.md",
        published_pattern_templates=(r"latest published release tag\s+\(`v{version}`\)",),
    ),
)

RELEASE_DOCUMENT_STATE_SCHEMA_VERSION = "alice_release_document_state_v1"
_RELEASE_DOCUMENT_STATE_KEYS = frozenset({"schema_version", "version", "publication_status", "checksums_status"})
_RELEASE_DOCUMENT_STATE_PATTERN = re.compile(r"<!-- alice-release-state: (?P<payload>\{.*\}) -->")
_LATEST_PUBLISHED_VERSION_PATTERN = re.compile(
    r"(?:\blatest\s+published\b(?:(?!\n[ \t]*\n)[\s\S]){0,160}?"
    r"`v(?P<after>\d+\.\d+\.\d+)`|"
    r"`v(?P<before>\d+\.\d+\.\d+)`(?:(?!\n[ \t]*\n)[\s\S]){0,160}?"
    r"\blatest\s+published\b)",
    flags=re.IGNORECASE,
)
_CHECKSUM_RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{64}  [A-Za-z0-9][A-Za-z0-9_.+-]*$", flags=re.MULTILINE)
_STALE_RELEASE_CLOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "CHANGELOG.md",
        re.compile(r"v0\.10\.0[^\n]{0,80}current closure record", re.IGNORECASE),
    ),
    (
        "docs/release/v0.9.4-release-notes.md",
        re.compile(r"^The v0\.10\.0 remediation cycle tracks", re.IGNORECASE | re.MULTILINE),
    ),
)


def _semantic_version_key(version: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match is not None else ""


def _is_dated_record(relative_path: str) -> bool:
    return any(
        relative_path == record or relative_path.startswith(record + "/") for record in DATED_RECORD_PATHS
    )


def living_doc_files(root_dir: Path = ROOT_DIR, *, suffixes: tuple[str, ...] = (".md",)) -> Iterator[Path]:
    """The living documentation files under ``root_dir``, in a stable order.

    A living doc is a file with one of ``suffixes`` that is a root-level file or sits under a folder of
    ``LIVING_DOC_ROOTS``, and is not one of the ``DATED_RECORD_PATHS``.
    """

    found: list[Path] = [path for path in sorted(root_dir.iterdir()) if path.is_file() and path.suffix in suffixes]
    for root_name in LIVING_DOC_ROOTS:
        base = root_dir / root_name
        if not base.is_dir():
            continue
        for directory, directory_names, file_names in os.walk(base):
            directory_names.sort()
            for file_name in sorted(file_names):
                path = Path(directory) / file_name
                if path.suffix in suffixes:
                    found.append(path)
    for path in found:
        if not _is_dated_record(path.relative_to(root_dir).as_posix()):
            yield path


def _validate_package_description(root_dir: Path, project: object) -> list[str]:
    issues: list[str] = []
    configured_readme = project.get("readme") if isinstance(project, dict) else None
    if configured_readme != PACKAGE_DESCRIPTION_RELATIVE_PATH:
        issues.append(
            f"pyproject.toml: project.readme must point to the evergreen {PACKAGE_DESCRIPTION_RELATIVE_PATH!r}"
        )
    path = root_dir / PACKAGE_DESCRIPTION_RELATIVE_PATH
    try:
        description = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        issues.append(f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: missing or unreadable")
        return issues
    if _PACKAGE_DESCRIPTION_VERSION_PATTERN.search(description):
        issues.append(f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: contains a version literal")
    if _PACKAGE_DESCRIPTION_STATE_PATTERN.search(description):
        issues.append(f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: contains release-state language")
    return issues


def _validate_historical_remediation_handoff(root_dir: Path) -> list[str]:
    handoff_dir = root_dir / _HISTORICAL_REMEDIATION_HANDOFF
    if not handoff_dir.is_dir():
        return [f"{_HISTORICAL_REMEDIATION_HANDOFF}: missing directory"]

    issues: list[str] = []
    for relative_path, markers in _HISTORICAL_REMEDIATION_MARKERS.items():
        path = handoff_dir / relative_path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            issues.append(f"{path.relative_to(root_dir)}: missing or unreadable")
            continue
        for marker in markers:
            if marker not in text:
                issues.append(f"{path.relative_to(root_dir)}: missing Repair Batch 16 truth marker {marker!r}")
        normalized_text = " ".join(text.split()).casefold()
        for marker in _HISTORICAL_REMEDIATION_FORBIDDEN_MARKERS.get(relative_path, ()):
            normalized_marker = " ".join(marker.split()).casefold()
            if normalized_marker in normalized_text:
                issues.append(f"{path.relative_to(root_dir)}: contains stale remediation marker {marker!r}")
        for marker in _HISTORICAL_FINALIZATION_MARKERS.get(relative_path, ()):
            normalized_marker = " ".join(marker.split()).casefold()
            if normalized_marker not in normalized_text:
                issues.append(f"{path.relative_to(root_dir)}: missing finalization truth marker {marker!r}")
        for marker in _HISTORICAL_FINALIZATION_FORBIDDEN_MARKERS.get(relative_path, ()):
            normalized_marker = " ".join(marker.split()).casefold()
            if normalized_marker in normalized_text:
                issues.append(f"{path.relative_to(root_dir)}: contains stale finalization marker {marker!r}")
    return issues


def _validate_active_sprint_packet(root_dir: Path) -> list[str]:
    path = root_dir / _ACTIVE_SPRINT_PACKET
    if not path.is_file():
        return []  # The static control-document rule reports absence.

    payload = path.read_bytes()
    issues: list[str] = []
    if len(payload) > _ACTIVE_SPRINT_PACKET_MAX_BYTES:
        issues.append(
            f"{_ACTIVE_SPRINT_PACKET}: exceeds {_ACTIVE_SPRINT_PACKET_MAX_BYTES}-byte control-document limit"
        )

    text = payload.decode("utf-8")
    line_count = len(text.splitlines())
    if line_count > _ACTIVE_SPRINT_PACKET_MAX_LINES:
        issues.append(
            f"{_ACTIVE_SPRINT_PACKET}: exceeds {_ACTIVE_SPRINT_PACKET_MAX_LINES}-line control-document limit"
        )

    normalized_text = " ".join(text.split())
    for label, pattern in _ACTIVE_SPRINT_FORBIDDEN_PATTERNS:
        if pattern.search(normalized_text) is not None:
            issues.append(f"{_ACTIVE_SPRINT_PACKET}: contains {label}")
    return issues


def _has_exact_release_state_keys(state: object) -> bool:
    return isinstance(state, dict) and set(state) == _RELEASE_DOCUMENT_STATE_KEYS


def _latest_structured_published_version(*, root_dir: Path) -> str | None:
    """Resolve publication truth solely from structured release records."""

    release_dir = root_dir / "docs" / "release"
    published: list[tuple[tuple[int, int, int], str]] = []
    for path in release_dir.glob("v*-release-notes.md"):
        version = path.name.removeprefix("v").removesuffix("-release-notes.md")
        key = _semantic_version_key(version)
        if key is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if len(lines) < 2:
            continue
        state_match = _RELEASE_DOCUMENT_STATE_PATTERN.fullmatch(lines[1])
        if state_match is None:
            continue
        try:
            state = json.loads(state_match.group("payload"))
        except json.JSONDecodeError:
            continue
        if (
            _has_exact_release_state_keys(state)
            and state.get("schema_version") == RELEASE_DOCUMENT_STATE_SCHEMA_VERSION
            and state.get("version") == version
            and state.get("publication_status") == "published"
            and state.get("checksums_status") == "recorded"
            and _has_exact_checksum_receipt(
                root_dir / "docs" / "release" / f"v{version}-checksums.txt",
                version=version,
            )
        ):
            published.append((key, version))
    return max(published)[1] if published else None


def _has_exact_checksum_receipt(path: Path, *, version: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    artifact_names = [match.group(0).split("  ", maxsplit=1)[1] for match in _CHECKSUM_RECEIPT_PATTERN.finditer(text)]
    return len(artifact_names) == 2 and set(artifact_names) == {
        f"alice_memory-{version}-py3-none-any.whl",
        f"alice_memory-{version}.tar.gz",
    }


def _read_release_document_mode(*, root_dir: Path, version: str) -> tuple[str | None, list[str]]:
    relative_path = f"docs/release/v{version}-release-notes.md"
    path = root_dir / relative_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, [f"{relative_path}: missing file"]

    expected_title = f"# Alice v{version} Release Notes"
    issues: list[str] = []
    if not lines or lines[0] != expected_title:
        issues.append(f"{relative_path}: missing exact title '{expected_title}'")
    state_lines = [(index, line) for index, line in enumerate(lines) if "alice-release-state" in line]
    if len(state_lines) != 1 or state_lines[0][0] != 1:
        issues.append(f"{relative_path}: alice-release-state must appear exactly once on line 2")
        return None, issues
    match = _RELEASE_DOCUMENT_STATE_PATTERN.fullmatch(state_lines[0][1])
    if match is None:
        issues.append(f"{relative_path}: malformed alice-release-state")
        return None, issues
    try:
        state = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        issues.append(f"{relative_path}: invalid alice-release-state JSON: {exc}")
        return None, issues
    if not isinstance(state, dict):
        issues.append(f"{relative_path}: alice-release-state must be an object")
        return None, issues
    if not _has_exact_release_state_keys(state):
        issues.append(f"{relative_path}: alice-release-state must contain exactly the supported keys")
    if state.get("schema_version") != RELEASE_DOCUMENT_STATE_SCHEMA_VERSION:
        issues.append(f"{relative_path}: unsupported alice-release-state schema")
    if state.get("version") != version:
        issues.append(f"{relative_path}: alice-release-state version does not match")

    publication_status = state.get("publication_status")
    checksums_status = state.get("checksums_status")
    pair = (publication_status, checksums_status)
    if pair == ("pending", "pending"):
        mode = "candidate"
    elif pair == ("published", "recorded"):
        mode = "published"
    else:
        issues.append(f"{relative_path}: unsupported publication/checksum state {pair!r}")
        mode = None

    checksums = root_dir / "docs" / "release" / f"v{version}-checksums.txt"
    if mode == "candidate" and checksums.exists():
        issues.append(f"docs/release/v{version}-checksums.txt: must not exist while publication is pending")
    if mode == "published":
        if not checksums.is_file():
            issues.append(f"docs/release/v{version}-checksums.txt: missing for recorded publication")
        elif not _has_exact_checksum_receipt(checksums, version=version):
            issues.append(
                f"docs/release/v{version}-checksums.txt: must contain exactly the canonical wheel and sdist SHA-256 records"
            )
    return mode, issues


DISALLOWED_MARKERS: tuple[str, ...] = (
    "through Phase 3 Sprint 9",
    "Active Sprint focus is Phase 4 Sprint 14",
    "Gate ownership is canonicalized to Phase 4 runner scripts",
    "Gate ownership is canonicalized to Phase 4 runner script names",
    "Legacy Compatibility Marker",
    "Legacy Compatibility Markers",
    "Phase 9 Sprint Sequence",
    "No active build sprint is open.",
    "Phase 10 planning docs are not defined yet.",
    "Keep this file as an idle-state pointer, not as a fake active sprint.",
    "CTO summary",
    "control tower",
    "design partner",
    "second brain",
    "HF-001",
    "resumes feature work and clears the second audit's P2 backlog",
    "semantic attestation replaces the repository-control attestation",
)


def run_control_doc_truth_check(
    *,
    root_dir: Path = ROOT_DIR,
    rules: tuple[ControlDocTruthRule, ...] = CONTROL_DOC_TRUTH_RULES,
    disallowed_markers: tuple[str, ...] = DISALLOWED_MARKERS,
) -> list[str]:
    issues: list[str] = []
    with (root_dir / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    project = pyproject.get("project")
    version = str(project.get("version", "")) if isinstance(project, dict) else ""
    if not version:
        issues.append("pyproject.toml: missing project.version")
    issues.extend(_validate_package_description(root_dir, project))
    issues.extend(_validate_historical_remediation_handoff(root_dir))
    issues.extend(_validate_active_sprint_packet(root_dir))
    release_mode, release_state_issues = _read_release_document_mode(root_dir=root_dir, version=version)
    issues.extend(release_state_issues)
    latest_published_version = _latest_structured_published_version(root_dir=root_dir)

    for rule in rules:
        doc_path = root_dir / rule.relative_path
        if not doc_path.exists():
            issues.append(f"{rule.relative_path}: missing file")
            continue

        text = doc_path.read_text(encoding="utf-8")
        for marker in rule.required_markers:
            if marker not in text:
                issues.append(f"{rule.relative_path}: missing required marker '{marker}'")

        lowered_text = text.casefold()
        for marker in disallowed_markers:
            if marker.casefold() in lowered_text:
                issues.append(f"{rule.relative_path}: contains disallowed marker '{marker}'")

    for version_rule in VERSION_ALIGNED_DOC_RULES:
        doc_path = root_dir / version_rule.relative_path
        if not doc_path.exists():
            continue  # The static rule already reports required-file absence.
        text = doc_path.read_text(encoding="utf-8")
        pattern_templates = (
            version_rule.published_pattern_templates
            if release_mode == "published"
            else version_rule.candidate_pattern_templates
        )
        for pattern_template in pattern_templates:
            pattern = pattern_template.format(version=re.escape(version))
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is None:
                issues.append(
                    f"{version_rule.relative_path}: missing current-version {release_mode or 'unknown'} marker matching '{pattern_template.format(version=version)}'"
                )
        if release_mode == "published":
            bounded_same_sentence = r"(?:(?!\n[ \t]*\n)[^.!?]){0,200}?"
            contradiction_term = r"(?:candidate|unshipped|unpublished|not[ \t]+published)"
            contradiction = re.compile(
                rf"(?:`v{re.escape(version)}`{bounded_same_sentence}\b{contradiction_term}\b|"
                rf"\b{contradiction_term}\b{bounded_same_sentence}`v{re.escape(version)}`)",
                flags=re.IGNORECASE,
            )
            if contradiction.search(text) is not None:
                issues.append(
                    f"{version_rule.relative_path}: describes published v{version} as unpublished or a candidate"
                )
        for match in _LATEST_PUBLISHED_VERSION_PATTERN.finditer(text):
            stated_version = match.group("after") or match.group("before")
            if latest_published_version is None:
                issues.append(
                    f"{version_rule.relative_path}: names v{stated_version} as latest published "
                    "without a structured published release record"
                )
            elif stated_version != latest_published_version:
                issues.append(
                    f"{version_rule.relative_path}: names v{stated_version} as latest published "
                    f"instead of v{latest_published_version}"
                )

    if latest_published_version is not None:
        expected_notes = f"v{latest_published_version}-release-notes.md"
        for relative_path in _LATEST_RELEASE_NOTES_DOCS:
            path = root_dir / relative_path
            if path.is_file() and expected_notes not in path.read_text(encoding="utf-8"):
                issues.append(f"{relative_path}: latest release-notes link must target {expected_notes}")

        expected_checksums = f"docs/release/v{latest_published_version}-checksums.txt"
        for relative_path in _LATEST_CHECKSUM_DOCS:
            path = root_dir / relative_path
            if path.is_file() and expected_checksums not in path.read_text(encoding="utf-8"):
                issues.append(f"{relative_path}: published checksum pointer must target {expected_checksums}")

        current_state = root_dir / "CURRENT_STATE.md"
        if current_state.is_file():
            release_boundary = _markdown_section(current_state.read_text(encoding="utf-8"), "## Release Boundary")
            if expected_checksums not in release_boundary:
                issues.append(f"CURRENT_STATE.md: Release Boundary checksum pointer must target {expected_checksums}")

        for living_doc in living_doc_files(root_dir):
            for match in _LITERAL_INSTALL_TAG_PATTERN.finditer(living_doc.read_text(encoding="utf-8", errors="replace")):
                if match.group("version") != latest_published_version:
                    issues.append(
                        f"{living_doc.relative_to(root_dir).as_posix()}: literal install tag "
                        f"v{match.group('version')} must match latest published "
                        f"v{latest_published_version}"
                    )

    version_key = _semantic_version_key(version)
    if release_mode == "published" and version_key is not None and version_key > _FUTURE_STATE_ENFORCEMENT_AFTER:
        notes_path = root_dir / "docs" / "release" / f"v{version}-release-notes.md"
        if notes_path.is_file() and _PUBLISHED_FUTURE_STATE_PATTERN.search(notes_path.read_text(encoding="utf-8")):
            issues.append(f"docs/release/v{version}-release-notes.md: published notes contain future-state language")

    for relative_path, stale_pattern in _STALE_RELEASE_CLOSURE_PATTERNS:
        path = root_dir / relative_path
        if path.is_file() and stale_pattern.search(path.read_text(encoding="utf-8")):
            issues.append(f"{relative_path}: contains stale present-tense v0.10.0 closure wording")

    mirror = root_dir / ".ai" / "handoff" / "CURRENT_STATE.md"
    current = root_dir / "CURRENT_STATE.md"
    if mirror.exists() and current.exists() and mirror.read_bytes() != current.read_bytes():
        issues.append(".ai/handoff/CURRENT_STATE.md: must exactly mirror CURRENT_STATE.md")

    return issues


def main() -> int:
    issues = run_control_doc_truth_check()
    if issues:
        print("Control-doc truth check: FAIL")
        for issue in issues:
            print(f" - {issue}")
        return 1

    print("Control-doc truth check: PASS")
    for rule in CONTROL_DOC_TRUTH_RULES:
        print(f" - verified: {rule.relative_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
