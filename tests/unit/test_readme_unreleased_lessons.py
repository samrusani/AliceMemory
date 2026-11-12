"""Keep the README's release distinction while lessons are unreleased."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def _check_status(readme: str, changelog: str) -> None:
    unreleased = re.search(r"(?ms)^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", changelog)
    if unreleased is None:
        return
    entries = re.findall(r"(?ms)^- (.*?)(?=^- |\Z)", unreleased.group(1))
    if not any("alice_lessons" in entry for entry in entries):
        return
    status = re.search(r"(?ms)^## Status[ \t]*\n(.*?)(?=^## |\Z)", readme)
    assert status is not None
    assert (
        "The README and documentation describe main, where `alice_lessons` and the `lesson` block of "
        "`alice_memory_commit` are available but are not in v0.23.1."
    ) in status.group(1)
    assert "The [Unreleased section of the changelog](CHANGELOG.md#unreleased) lists all changes on main since v0.23.1." in status.group(1)


def test_readme_status_explains_unreleased_lessons() -> None:
    """Mutation: delete the sentence distinguishing main from v0.23.1 in README's Status section."""
    _check_status((ROOT / "README.md").read_text(), (ROOT / "CHANGELOG.md").read_text())
    # A released entry must not require the note, even when it remains in the changelog.
    _check_status("", "## Unreleased\n\n## v0.23.2\n\n- `alice_lessons` is available.\n")
