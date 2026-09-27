from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_BASE = "f342d45dabe127acca6231f29830ff11d98a340e"
_CORRECTION_LINE = re.compile(r"> \*\*Correction \(\d{4}-\d{2}-\d{2}\):\*\* \S")
_HANDOFF = _ROOT / "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor"
_HEADLINE = "Structure only. Zero behavior change."
_EXCLUSIONS = (
    "coverage.json",
    "uv.lock",
    "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/BUILD_REPORT.md",
    "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/REVIEW_REPORT.md",
)


def _is_ignored(relative_path: str) -> bool:
    result = subprocess.run(
        (
            "git",
            "-C",
            str(_ROOT),
            "check-ignore",
            "--no-index",
            "--quiet",
            relative_path,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def test_phase3_handoff_builder_files_exist_and_pin_structure_only_headline() -> None:
    filenames = (
        "README.md",
        "SURFACE_INVENTORY.md",
        "FIX_MATRIX.md",
        "ENGINEER_HANDOFF.md",
        "BUILD_REPORT.md",
    )

    for filename in filenames:
        text = (_HANDOFF / filename).read_text(encoding="utf-8")
        assert _HEADLINE in text, filename

    for relative_path in (
        "CURRENT_STATE.md",
        ".ai/handoff/CURRENT_STATE.md",
        "docs/release/v0.12.0-release-notes.md",
    ):
        document = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "3,804 unit tests" in document, relative_path
        assert "3,793 unit tests" not in document, relative_path


def test_phase3_versions_were_cut_to_0120_by_the_release_engineer() -> None:
    # The builder carrier held both governed version sources at the published
    # 0.11.1 baseline (the frozen handoff README still records that delivery
    # state); the release engineer cut them to 0.12.0 at release time.
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    web = json.loads((_ROOT / "apps/web/package.json").read_text(encoding="utf-8"))

    # The 0.12.0 cut is historical fact (frozen handoff README below); the
    # live pin moved on with later release cuts. The durable invariant is
    # that both governed version sources always agree.
    assert project["version"] == web["version"]
    assert "version sources intentionally remain `0.11.1`" in (
        _HANDOFF / "README.md"
    ).read_text(encoding="utf-8")


def test_v0120_release_notes_are_published_with_recorded_checksum_receipt() -> None:
    notes = (_ROOT / "docs/release/v0.12.0-release-notes.md").read_text(encoding="utf-8")

    assert notes.splitlines()[0] == "# Alice v0.12.0 Release Notes"
    assert (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"0.12.0","publication_status":"published","checksums_status":"recorded"} -->'
        in notes
    )
    assert _HEADLINE in notes
    assert "verified identical to the published `v0.11.1`" in notes
    checksums = (_ROOT / "docs/release/v0.12.0-checksums.txt").read_text(encoding="utf-8")
    assert (
        "66c050235d57e65969bd81c2b930e212ec674019196c3b3cb0dd84f0183e0d70"
        "  alice_memory-0.12.0-py3-none-any.whl"
    ) in checksums
    assert (
        "1e862de80c2e5e9a47ac5b8e9c636fa39ecfed19974a35e2cf3e3f5841927c6d"
        "  alice_memory-0.12.0.tar.gz"
    ) in checksums


def test_phase3_current_state_is_exact_mirror_and_phase4_is_out_of_scope() -> None:
    current = (_ROOT / "CURRENT_STATE.md").read_bytes()
    mirror = (_ROOT / ".ai/handoff/CURRENT_STATE.md").read_bytes()
    sprint = (_ROOT / ".ai/active/SPRINT_PACKET.md").read_text(encoding="utf-8")

    assert current == mirror
    assert _HEADLINE.encode() in current
    assert "<!-- alice-sprint-scope: phase-3-complete -->" in sprint
    assert "Phase 4 is out of scope for this packet." in sprint
    assert "Phase 4 is active" not in sprint
    assert "Phase 4 work is underway" not in sprint


def test_phase3_handoff_reports_are_unignored_without_weakening_generic_policy() -> None:
    prefix = "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor"

    for filename in ("BUILD_REPORT.md", "REVIEW_REPORT.md"):
        assert not _is_ignored(f"{prefix}/{filename}")
        assert _is_ignored(filename)


def test_phase3_carrier_receipt_exclusions_are_exact_and_review_safe() -> None:
    build_report = (_HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    engineer_handoff = (_HANDOFF / "ENGINEER_HANDOFF.md").read_text(encoding="utf-8")

    exclusion_block = build_report.split("Configured exclusions are exactly:", 1)[1].split(
        "```text\n", 1
    )[1].split("\n```", 1)[0]
    observed = tuple(line for line in exclusion_block.splitlines() if line)
    assert observed == _EXCLUSIONS
    assert "reviewer-authored `REVIEW_REPORT.md`" in build_report
    assert "Any edit outside those four exact exclusions invalidates the receipt" in engineer_handoff


def test_phase3_carrier_does_not_edit_immutable_v010_v011_records() -> None:
    base_present = subprocess.run(
        ("git", "-C", str(_ROOT), "cat-file", "-e", f"{_BASE}^{{commit}}"),
        check=False,
        capture_output=True,
    )
    if base_present.returncode != 0:
        pytest.skip(
            "phase 3 base commit is not present in this checkout (shallow CI "
            "clone); the immutable-records guard runs in full clones"
        )
    result = subprocess.run(
        (
            "git",
            "-C",
            str(_ROOT),
            "diff",
            "--name-only",
            _BASE,
            "--",
            "docs/release/v0.10*",
            "docs/release/v0.11*",
            "docs/handoff/2026-07-13-v0.10-audit-remediation",
            "docs/handoff/2026-07-14-v0.10.4-remediation",
            "docs/handoff/2026-07-15-v0.11.0-phase1-periphery-cut",
            "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    # The only edit allowed is an added, dated correction of old wording,
    # such as an audit a release note called external. Nothing may be
    # removed or rewritten, and the handoff folders stay untouched.
    for path in result.stdout.splitlines():
        assert path.startswith(("docs/release/v0.10", "docs/release/v0.11")), path
        diff = subprocess.run(
            ("git", "-C", str(_ROOT), "diff", "-U0", _BASE, "--", path),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for line in diff.splitlines():
            if line.startswith(("---", "+++", "@@", "diff ", "index ")):
                continue
            assert not line.startswith("-"), (path, line)
            if line.startswith("+"):
                assert line == "+" or _CORRECTION_LINE.match(line[1:]), (path, line)


def test_phase3_untracked_whitespace_check_accepts_clean_diff_only(tmp_path: Path) -> None:
    engineer_handoff = (_HANDOFF / "ENGINEER_HANDOFF.md").read_text(encoding="utf-8")
    assert 'candidate_check="$(git diff --no-index --check /dev/null "$candidate_path" 2>&1)"' in engineer_handoff
    assert '[ "$candidate_status" -gt 1 ] || [ -n "$candidate_check" ]' in engineer_handoff

    fixtures = {
        "clean.txt": (b"clean\n", True),
        "trailing-space.txt": (b"not clean \n", False),
        "blank-eof.txt": (b"not clean\n\n", False),
    }
    for filename, (payload, expected_clean) in fixtures.items():
        path = tmp_path / filename
        path.write_bytes(payload)
        result = subprocess.run(
            ("git", "diff", "--no-index", "--check", "/dev/null", str(path)),
            check=False,
            capture_output=True,
            text=True,
        )
        diagnostics = result.stdout + result.stderr
        observed_clean = result.returncode <= 1 and diagnostics == ""
        assert observed_clean is expected_clean, filename


def test_phase3_included_docs_do_not_predict_live_final_review_state() -> None:
    relative_paths = (
        ".ai/active/SPRINT_PACKET.md",
        "CURRENT_STATE.md",
        ".ai/handoff/CURRENT_STATE.md",
        "README.md",
        "ROADMAP.md",
        "ARCHITECTURE.md",
        "PRODUCT_BRIEF.md",
        "CHANGELOG.md",
        "docs/vnext/README.md",
        "docs/release/v0.12.0-release-notes.md",
        "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/README.md",
        "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/SURFACE_INVENTORY.md",
        "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/FIX_MATRIX.md",
        "docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/ENGINEER_HANDOFF.md",
    )
    stale_review_state = re.compile(
        r"(?:final (?:handoff |independent )?review.{0,100}(?:pending|future|remain)|"
        r"future reviewer-authored|future review report)",
        flags=re.IGNORECASE,
    )

    for relative_path in relative_paths:
        normalized = " ".join((_ROOT / relative_path).read_text(encoding="utf-8").split())
        assert stale_review_state.search(normalized) is None, relative_path
