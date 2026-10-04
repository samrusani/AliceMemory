"""Frozen handoff READMEs carry one dated correction about the reviews they name, and nothing else changed.

Several handoff folders under ``docs/handoff`` call the project's own review
passes "independent". On 2026-10-04 each of the nine dated folders gained one
correction line in its ``README.md``, saying the reviews were internal adversarial
review and automated security scanning and that no one outside the project has
audited the code. The folders stay frozen otherwise.

The facts live in ``frozen_handoff_support.py``. The guards that hold the folders
immutable accept that line through ``correction_only_problems`` and
``handoff_drift_problems``. This file checks the nine READMEs and proves the
helpers on made-up diffs.

Mutations that must fail this file, each alone:

* delete the line from one README, reword it, change its date, add a second copy
  or move it away from the title: the README test for that folder fails;
* let ``correction_only_problems`` accept any ``Correction (date)`` line, a line
  that is not the whole text, a removed line, a change to another file or more
  than two empty lines: the matrix test for that case fails;
* let ``handoff_drift_problems`` accept a change to another file of the folder,
  or a README rewrite: the drift test for that case fails.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.unit.frozen_handoff_support import (
    CORRECTED_FOLDERS,
    CORRECTION,
    README_PATHS,
    correction_only_problems,
    handoff_drift_problems,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_ROOT = REPO_ROOT / "docs/handoff"
FOLDER = "2026-07-24-v0.14.0-deployment-guide-fixes"
README = f"docs/handoff/{FOLDER}/README.md"
REPORT = f"docs/handoff/{FOLDER}/BUILD_REPORT.md"


def test_the_correction_says_what_the_reviews_were_and_who_has_not_audited() -> None:
    assert CORRECTION == (
        "> **Correction (2026-10-04):** the reviews named here were internal adversarial review"
        " and automated security scanning. No one outside the project has audited the code."
    )
    assert chr(0x2014) not in CORRECTION and chr(0x2013) not in CORRECTION
    assert "independently audited" not in CORRECTION.casefold()


def test_the_corrected_folders_exist_and_each_has_a_readme_path() -> None:
    assert len(set(CORRECTED_FOLDERS)) == len(CORRECTED_FOLDERS) == len(README_PATHS) == 9
    for folder in CORRECTED_FOLDERS:
        assert (HANDOFF_ROOT / folder).is_dir(), folder
    assert not (HANDOFF_ROOT / "history" / "README.md").exists()


@pytest.mark.parametrize("folder", CORRECTED_FOLDERS)
def test_each_frozen_handoff_readme_holds_the_correction_once_right_after_its_title(folder: str) -> None:
    """Mutation: delete, reword or redate the line, add a second copy, or move it below the first section."""

    text = (HANDOFF_ROOT / folder / "README.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("# "), folder
    assert lines[1:4] == ["", CORRECTION, ""], folder
    assert text.count(CORRECTION) == 1, folder
    assert text.count("Correction (2026-10-04)") == 1, folder



def test_the_v0104_repair_batch_history_holds_the_correction_once_right_after_its_title() -> None:
    """``docs/handoff/history`` has no README, so its one dated record that calls a review independent,
    ``v0.10.4-repair-batches.md``, carries the same correction line right after its title. The file is not a
    frozen-folder byte guard target; its required markers are held by ``check_control_doc_truth.py``.

    Mutation: delete, reword or redate the line, or add a second copy.
    """

    text = (HANDOFF_ROOT / "history" / "v0.10.4-repair-batches.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# v0.10.4 Repair-Batch History"
    assert lines[1:4] == ["", CORRECTION, ""]
    assert text.count(CORRECTION) == 1
    assert text.count("Correction (2026-10-04)") == 1

def _diff(*body: str, header: tuple[str, ...] | None = None) -> str:
    head = (
        header
        if header is not None
        else (
            f"diff --git a/{README} b/{README}",
            "index 1111111..2222222 100644",
            f"--- a/{README}",
            f"+++ b/{README}",
        )
    )
    return "\n".join((*head, *body)) + "\n"


def test_the_real_insertion_shape_is_accepted() -> None:
    assert correction_only_problems(README, _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+")) == []
    assert correction_only_problems(README, _diff("@@ -2,0 +3,2 @@", "+", f"+{CORRECTION}")) == []
    assert correction_only_problems(README, "") == []


_REJECTED = (
    (
        "another date",
        README,
        _diff("@@ -2,0 +3,2 @@", "+" + CORRECTION.replace("2026-10-04", "2026-10-05"), "+"),
        "not the dated correction",
    ),
    (
        "a reworded sentence",
        README,
        _diff("@@ -2,0 +3,2 @@", "+" + CORRECTION.replace("internal", "independent"), "+"),
        "not the dated correction",
    ),
    (
        "the generic correction shape with other text",
        README,
        _diff("@@ -2,0 +3,2 @@", "+> **Correction (2026-10-04):** something else entirely.", "+"),
        "not the dated correction",
    ),
    (
        "a second copy",
        README,
        _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+", "@@ -9,0 +11,2 @@", f"+{CORRECTION}", "+"),
        "added 2 times",
    ),
    (
        "an extra sentence",
        README,
        _diff("@@ -2,0 +3,3 @@", f"+{CORRECTION}", "+", "+An extra sentence."),
        "not the dated correction",
    ),
    (
        "a whitespace-only line",
        README,
        _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+ "),
        "not the dated correction",
    ),
    (
        "three empty lines",
        README,
        _diff("@@ -2,0 +3,4 @@", f"+{CORRECTION}", "+", "+", "+"),
        "at most two",
    ),
    (
        "a removed line",
        README,
        _diff("@@ -4 +3,0 @@", "-This directory is the handoff.", "@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+"),
        "removed or rewritten",
    ),
    (
        "a removed line that starts with dashes",
        README,
        _diff("@@ -4 +4,0 @@", "--- a table rule", f"+{CORRECTION}"),
        "removed or rewritten",
    ),
    (
        "a rewritten line",
        README,
        _diff("@@ -4 +4 @@", "-old wording", "+new wording"),
        "removed or rewritten",
    ),
    (
        "a missing final newline",
        README,
        _diff("@@ -9 +9 @@", f"+{CORRECTION}", "\\ No newline at end of file"),
        "unexpected diff line",
    ),
    (
        "a mode change",
        README,
        _diff(
            "@@ -2,0 +3,2 @@",
            f"+{CORRECTION}",
            "+",
            header=(
                f"diff --git a/{README} b/{README}",
                "old mode 100644",
                "new mode 100755",
                "index 1111111..2222222",
                f"--- a/{README}",
                f"+++ b/{README}",
            ),
        ),
        "unexpected diff header",
    ),
    ("another file of the folder", REPORT, _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+"), "only the README"),
    (
        "a folder outside the nine",
        "docs/handoff/2026-08-01-new/README.md",
        _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+"),
        "only the README",
    ),
    (
        "a release note",
        "docs/release/v0.10.0-release-notes.md",
        _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+"),
        "only the README",
    ),
)


@pytest.mark.parametrize(("label", "path", "diff", "fragment"), _REJECTED, ids=[case[0] for case in _REJECTED])
def test_anything_but_the_whole_correction_in_a_corrected_readme_is_reported(
    label: str, path: str, diff: str, fragment: str
) -> None:
    """Mutation: loosen the matcher so this case passes."""

    problems = correction_only_problems(path, diff)
    assert problems, label
    assert any(fragment in problem for problem in problems), (label, problems)


class _FakeGit:
    """Answers the three git calls ``handoff_drift_problems`` makes, from canned changed paths and diffs."""

    def __init__(self, changed: list[str], readme_diff: str = "", quiet_status: int | None = None) -> None:
        self.changed = changed
        self.readme_diff = readme_diff
        self.quiet_status = quiet_status
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        self.calls.append(arguments)
        if "--quiet" in arguments:
            status = self.quiet_status if self.quiet_status is not None else int(bool(self.changed))
            return subprocess.CompletedProcess(arguments, status, b"", b"")
        if "--name-only" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "".join(f"{p}\n" for p in self.changed).encode(), b"")
        assert "-U0" in arguments
        return subprocess.CompletedProcess(arguments, 0, self.readme_diff.encode(), b"")


def _drift(git: _FakeGit) -> list[str]:
    return handoff_drift_problems(git, "carrier", "HEAD", f"docs/handoff/{FOLDER}")


def test_a_handoff_folder_is_unchanged_or_only_its_readme_gained_the_correction() -> None:
    """Mutation: skip the changed-files check, or skip the line check on the README."""

    assert _drift(_FakeGit([])) == []
    good = _diff("@@ -2,0 +3,2 @@", f"+{CORRECTION}", "+")
    assert _drift(_FakeGit([README], good)) == []

    rewritten = _diff("@@ -4 +4 @@", "-old wording", "+new wording")
    assert any("removed or rewritten" in p for p in _drift(_FakeGit([README], rewritten)))

    other = _drift(_FakeGit([REPORT]))
    assert other and "only" in other[0] and REPORT in other[0]
    both = _drift(_FakeGit([README, REPORT], good))
    assert both and REPORT in both[0]
    # The README of another folder is not this folder's README.
    assert _drift(_FakeGit(["docs/handoff/other/README.md"], good))
    # Git reports a change but names no file: not an accepted state.
    assert _drift(_FakeGit([], quiet_status=1))
