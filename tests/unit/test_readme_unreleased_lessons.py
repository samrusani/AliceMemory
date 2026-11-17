"""Keep the README free of the retired unreleased lessons status note."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_has_no_unreleased_lessons_status_note() -> None:
    """Mutation: restore the status note saying lessons are not in v0.23.1."""
    readme = (ROOT / "README.md").read_text()
    assert "The README and documentation describe main, where `alice_lessons`" not in readme
