"""The protocol page and the changelog say that redact completes for a promoted copy and what it leaves.

Mutations, each one alone: delete the marker from the paragraph; delete ``it holds no text``; delete the sentence that
says the artifact is not rewritten. Each fails the first test. Delete the changelog entry, or the sentence that says an
artifact event that holds more than the id still stops the redaction. Each fails the second.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"


def flat(text: str) -> str:
    return " ".join(text.split())


def test_the_protocol_page_says_what_redact_leaves_of_a_promoted_copy() -> None:
    page = flat((ROOT / "docs/memory-operations-protocol.md").read_text(encoding="utf-8"))
    section = page[page.index("## redact") :]
    start = section.index(MARK + " redact completes for a memory made by promoting a generated artifact")
    paragraph = section[start : start + 900]

    assert "In v0.20.0 it failed with a server error and scrubbed nothing." in paragraph
    assert "its whole payload is the id of the new memory, it holds no text" in paragraph
    assert "an event aimed at an artifact only for a coupled project update" in paragraph
    assert "The artifact is not rewritten either, so the text a promoted copy was made from stays in it" in paragraph


def test_the_changelog_says_redact_completes_for_a_promoted_copy_and_what_it_leaves() -> None:
    changelog = flat((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    entry = changelog[changelog.index(MARK + " redacting a memory that a promotion made now completes.") :][:2600]

    assert "In v0.19.2, in v0.20.0 and on main until now" in entry
    assert "the append-only trigger is unchanged" in entry
    assert "An artifact event that holds anything more still stops the redaction, so no text is left behind." in entry
