"""The one dated correction the frozen handoff READMEs carry, and the check that nothing else changed.

The folders under ``docs/handoff`` are frozen records. Several of them call the
project's own review passes "independent", which a reader can take to mean
someone outside the project looked at the code. On 2026-10-04 each README gained
one dated correction line saying what the reviews were. That is the only edit a
frozen handoff may gain. The guards that keep these folders immutable use
``correction_only_problems`` to accept exactly that line in ``README.md`` and
nothing else.

The line is matched whole, not by the looser ``Correction (date)`` shape the
release-note guard accepts, so a different sentence, a second copy or a changed
date is a failure.
"""

from __future__ import annotations

from collections.abc import Callable
import subprocess

# The nine dated handoff folders that existed on 2026-10-04. Each README holds
# the correction once. ``docs/handoff/history`` is not one of them: it has no
# README and is a pointer page, not a handoff.
CORRECTED_FOLDERS = (
    "2026-07-13-v0.10-audit-remediation",
    "2026-07-13-v0.10.2-post-release-remediation",
    "2026-07-14-v0.10.4-remediation",
    "2026-07-15-v0.11.0-phase1-periphery-cut",
    "2026-07-16-v0.11.1-phase2-debt-sweep",
    "2026-07-18-v0.12.0-phase3-structural-refactor",
    "2026-07-19-v0.13.0-phase4-sprint4-synthesis",
    "2026-07-21-v0.14.0-phase5-enterprise-track",
    "2026-07-24-v0.14.0-deployment-guide-fixes",
)

CORRECTION = (
    "> **Correction (2026-10-04):** the reviews named here were internal adversarial review"
    " and automated security scanning. No one outside the project has audited the code."
)

README_PATHS = tuple(f"docs/handoff/{folder}/README.md" for folder in CORRECTED_FOLDERS)

_FILE_HEADERS = ("diff --git ", "index ", "--- ", "+++ ")


def correction_only_problems(path: str, diff_text: str) -> list[str]:
    """Why a unified diff of ``path`` is more than the one dated correction, else an empty list.

    ``diff_text`` is the output of ``git diff -U0`` for that one path. The path
    must be the README of a corrected folder. Every added line must be empty (at
    most two, the spacing around the correction) or the correction itself, the
    correction may be added at most once, and no line may be removed. Only the
    lines before the first hunk are read as file headers, so a removed line that
    starts with dashes is still a removal. Mode changes, renames and a missing
    final newline are problems too, because none of them is an added line.
    """

    if path not in README_PATHS:
        return [f"{path}: only the README of a corrected handoff folder may change"]
    problems: list[str] = []
    added = 0
    blanks = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            if not line.startswith(_FILE_HEADERS):
                problems.append(f"{path}: unexpected diff header: {line!r}")
        elif line.startswith("-"):
            problems.append(f"{path}: a line was removed or rewritten: {line!r}")
        elif line.startswith("+"):
            body = line[1:]
            if body == CORRECTION:
                added += 1
            elif body == "":
                blanks += 1
            else:
                problems.append(f"{path}: an added line is not the dated correction: {body!r}")
        else:
            problems.append(f"{path}: unexpected diff line: {line!r}")
    if added > 1:
        problems.append(f"{path}: the correction was added {added} times")
    if blanks > 2:
        problems.append(f"{path}: {blanks} empty lines were added, at most two are allowed")
    return problems


def handoff_drift_problems(
    git: Callable[..., subprocess.CompletedProcess[bytes]],
    carrier: str,
    head: str,
    handoff_rel: str,
) -> list[str]:
    """Why a handoff folder differs between ``carrier`` and ``head`` by more than the dated correction.

    An empty list means the folder is byte for byte the same, or its README gained
    only the dated correction and nothing else under the folder changed. ``git`` runs
    git in the checkout and returns the finished process, with ``check=False``
    accepted. The receipt-bound reports are compared by the callers, byte for byte.
    """

    unchanged = git("diff", "--quiet", carrier, head, "--", handoff_rel, check=False)
    if unchanged.returncode == 0:
        return []
    names = git("diff", "--name-only", carrier, head, "--", handoff_rel, check=False).stdout.decode().splitlines()
    readme = f"{handoff_rel}/README.md"
    if names != [readme]:
        return [f"files changed under {handoff_rel}: {names}, and only {readme} may gain the dated correction"]
    body = git("diff", "-U0", carrier, head, "--", readme, check=False).stdout.decode()
    return correction_only_problems(readme, body)
