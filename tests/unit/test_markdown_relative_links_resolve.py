"""Every relative link in every tracked Markdown file resolves.

Main has no broken relative link in any tracked Markdown file. A page that links a file that does not exist, or a
section anchor that no heading produces, sends the reader nowhere, so the check covers every tracked file and fails
on the first broken link it names.

Mutations that must fail this file, each alone: change one relative link in docs/overview.md to a file that does
not exist, or point a link with an anchor at a file whose headings do not include that anchor.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:")


def _tracked_markdown_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    files = [ROOT / relative for relative in listed]
    assert len(files) >= 100, "the list of tracked Markdown files is empty or short"
    return files


def _github_anchor(heading: str) -> str:
    """The anchor GitHub produces for a heading: lowercase, punctuation dropped, spaces as hyphens."""

    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _headings(text: str) -> set[str]:
    anchors: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"#{1,6} (.+?)\s*#*$", line)
        if match:
            anchors.add(_github_anchor(match.group(1)))
    return anchors


def _without_fenced_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def test_every_relative_link_in_every_tracked_markdown_file_resolves() -> None:
    """Mutation: change one relative link in docs/overview.md to a file that does not exist, or point a
    link with an anchor at a file whose headings do not include that anchor. This test fails.
    """

    broken: list[str] = []
    for path in _tracked_markdown_files():
        text = _without_fenced_blocks(path.read_text(encoding="utf-8"))
        anchors = _headings(path.read_text(encoding="utf-8"))
        for match in _LINK.finditer(text):
            target = match.group(1)
            if target.startswith("#") or _SCHEME.match(target):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
                continue
            if anchor and resolved.suffix == ".md" and anchor not in _headings(resolved.read_text(encoding="utf-8")):
                broken.append(f"{path.relative_to(ROOT)} -> {target} (no heading for the anchor)")
    assert broken == [], broken
