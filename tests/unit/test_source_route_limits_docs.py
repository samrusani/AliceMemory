"""The pages say who may review, update, assign, archive and delete a source by id, and what the advisory covers.

The sentences are pinned by phrase. The behaviour is tested in ``tests/integration/test_source_routes_limits_postgres.py``.

Mutations, each one alone: delete ``a source above its sensitivity ceiling`` from the tool reference; delete the table
row for the owner; delete ``unauthorized modification and unauthorized deletion`` from the security note; delete
``stays refused to every key but an unbound`` from the changelog; delete ``HTTP 403 for every source`` from the tool
reference.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_the_tool_reference_says_who_may_change_or_delete_a_source_by_id() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    paragraph = tools.split(f"{MARK} the routes that review, update, assign, archive or delete one source by id", 1)[1]
    paragraph = paragraph.split("\n\n## ", 1)[0]
    assert "`POST /v0/vnext/sources/{id}/review`" in paragraph
    assert "`DELETE /v0/vnext/sources/{id}`" in paragraph
    assert "| The owner (no agent key) | Every stored source, as before. |" in paragraph
    assert "| `admin_agent` bound to no project | Every stored source, as before. |" in paragraph
    assert "A source above its sensitivity ceiling" in paragraph
    assert "answers HTTP 404 with the body of a missing source" in paragraph
    assert "HTTP 403 for every source, a missing id included" in paragraph
    assert "A refused call changes nothing" in paragraph
    assert "rename it, move it to a project, archive it and delete it" in paragraph
    assert "empty trace" in paragraph
    assert "receives HTTP 403 before the source is looked up" in paragraph
    assert "The SQLite install has no HTTP route for these verbs" in paragraph


def test_the_changelog_has_one_entry_for_the_source_routes() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    matches = [entry for entry in entries if entry.startswith(f"{MARK} source review and delete apply the caller's limits")]
    assert len(matches) == 1
    entry = matches[0]
    assert "v0.19.2" in entry and "v0.20.0" in entry
    assert "rename it, move it to a project, archive it and delete it" in entry
    assert "stays refused to every key but an unbound `admin_agent`" in entry
    assert "same body as the GET" in entry
    assert entry.endswith("No migration is required.")


def test_the_security_note_covers_disclosure_modification_and_deletion() -> None:
    note = _text("docs/release/derived-labels-security-note-draft.md")
    paragraph = note.split(f"{MARK} source review and delete now apply the caller's limits", 1)[1].split("\n", 1)[0]
    assert "disclosure, unauthorized modification and unauthorized deletion" in paragraph
    assert "rename it, assign it to a project, archive it or delete it" in paragraph
    assert "identical in v0.19.2 and v0.20.0, so it is not a regression of this set" in paragraph
    assert "apply the caller's identity to the embedded trace" in paragraph
    assert "Read-only, proposal and project-bound keys keep HTTP 403" in paragraph
    assert "source regeneration stays refused to every key but the owner and an unbound admin" in paragraph
