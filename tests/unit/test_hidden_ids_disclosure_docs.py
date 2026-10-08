"""The pages say that a readable row can list the id of a row the key cannot read, and what that does and does not give.

The sentences are pinned by phrase. The behaviour is tested in ``tests/integration/test_row_ids_grant_no_access_postgres.py``,
``tests/integration/test_hidden_ids_in_report_metadata_postgres.py`` and ``tests/unit/test_row_ids_grant_no_access_sqlite.py``.

Mutations, each one alone: delete ``not a way of meeting the requirement to filter hidden ids`` from the security note;
delete ``They grant no access`` from the security note; delete ``withholding them is tracked for v0.21.0`` from the
known limitations; delete ``The full-text stage does refill until its limit is met.`` from the security note; delete
``where it raised a server error`` from the changelog.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
IDS = f"{MARK} rows a key can read may list in their metadata the ids of rows it cannot read"
VECTOR = f"{MARK} the vector stage of recall and of the context pack does not yet refill after hiding rows"
BELIEF = f"{MARK} belief review and graph edge review"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _paragraph(text: str, start: str) -> str:
    assert text.count(start) == 1, start
    return text.split(start, 1)[1].split("\n", 1)[0]


def test_the_security_note_states_the_hidden_id_exception() -> None:
    note = _text("docs/release/derived-labels-security-note-draft.md")
    paragraph = _paragraph(note, IDS)
    assert "and that stays so until v0.21.0" in paragraph
    assert "This is a known exception, not a way of meeting the requirement to filter hidden ids." in paragraph
    for field in ("candidate_memory_ids", "derived_from", "input_summary", "source_ids", "memory_ids", "source_refs", "stale_marked_memory_ids", "content_markdown"):
        assert f"`{field}`" in paragraph, field
    assert "The artifact get and artifact trace routes return these lists as stored to any key whose limits admit the report" in paragraph
    assert "a read-only, project-scoped or project-bound key included" in paragraph
    assert "The artifact list, source trace, workspace and project dashboard are operator routes" in paragraph
    assert "the owner, an unbound admin key and an unbound trusted key" in paragraph
    assert "The memory tools return none of them." in paragraph
    assert "The ids are random UUIDs" in paragraph
    assert "They reveal that the row exists and how a report links to it. They grant no access." in paragraph
    assert "can repeat its domain, sensitivity and project scope" in paragraph
    assert "Neither answer shows the row's title, text, summary, raw text, chunk or claim, and neither changes a stored row." in paragraph
    assert "is tracked for v0.21.0" in paragraph
    assert "a report keeps the words of a source that was archived later" in paragraph


def test_the_security_note_states_the_vector_stage_limit() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), VECTOR)
    assert "a restricted caller can get fewer results than exist, down to none" in paragraph
    assert "Nothing the caller may not read is shown." in paragraph
    assert "The full-text stage does refill until its limit is met." in paragraph
    assert "tracked for v0.21.0" in paragraph


def test_the_security_note_covers_belief_and_edge_review() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), BELIEF)
    assert "`POST /v0/vnext/beliefs/{id}/review`" in paragraph and "`POST /v0/vnext/graph/edges/{id}/review`" in paragraph
    assert "The exposure covers disclosure and unauthorized modification." in paragraph
    assert "The route code is identical in v0.19.2 and v0.20.0, so it is not a regression of this set." in paragraph
    assert "retire, challenge, reinforce or supersede the belief, and accept or reject the edge" in paragraph
    assert "answer the same HTTP 404 they give for a missing id" in paragraph
    assert "Read-only, proposal and project-bound keys keep the HTTP 403 of the operator gate." in paragraph


def test_the_known_limitations_page_lists_both_limits() -> None:
    page = _text("docs/alpha/known-limitations.md")
    paragraph = _paragraph(page, f"- {IDS}")
    assert "such as the candidate memory a report made or a source archived since" in paragraph
    assert "The ids reveal that the row exists and grant no access" in paragraph
    assert "a known exception, not a way of meeting the requirement to filter hidden ids" in paragraph
    assert "withholding them is tracked for v0.21.0" in paragraph
    assert "(see the [draft security note](../release/derived-labels-security-note-draft.md))" in paragraph
    vector = _paragraph(page, f"- {VECTOR}")
    assert "a restricted caller can get fewer results than exist, down to none" in vector
    assert "tracked for v0.21.0" in vector


def test_the_changelog_has_one_entry_for_each() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    for start, phrases in (
        (IDS, ("and the notes now say so", "any key whose limits admit the report", "they grant no access", "This is a known exception, not a way of meeting the requirement to filter hidden ids", "is tracked for v0.21.0")),
        (VECTOR, ("fewer results than exist, down to none", "Tracked for v0.21.0")),
        (BELIEF + " apply the caller's limits", ("In v0.19.2, in v0.20.0 and on main until now", "change the stored row", "where it raised a server error")),
    ):
        matches = [entry for entry in entries if entry.startswith(start)]
        assert len(matches) == 1, start
        for phrase in phrases:
            assert phrase in matches[0], (start, phrase)
        assert matches[0].endswith("No migration is required.")


def test_the_tool_reference_states_both() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    belief = _paragraph(tools, f"{MARK} `POST /v0/vnext/beliefs/{{id}}/review` and `POST /v0/vnext/graph/edges/{{id}}/review` apply the same operator gate and the caller's read fence.")
    assert "a belief it would be replaced by is held to the same rule" in belief
    assert "Anything else answers HTTP 404 with the body of a missing row" in belief
    ids = _paragraph(tools, f"{MARK} a row a key can read may list in its metadata the id of a row the key cannot read.")
    assert "any key whose limits admit the report" in ids
    assert "The memory tools return none." in ids
    assert "It grants no access" in ids
    assert "This is a known exception to the rule that a hidden id is not shown, tracked for v0.21.0." in ids
