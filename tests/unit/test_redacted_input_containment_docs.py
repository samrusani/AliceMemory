"""The pages say what redacting a memory does to the reports made from it: contained now, removed later.

Redacting a memory restricts the reports and derived rows built from it to the owner and an unbound admin key until they are
regenerated without it. That is temporary access containment and not removal: the text stays in those reports, and full
removal is planned for v0.21.0. Archiving is not redaction and restricts nothing. The security note, the known limitations
page, the tool reference and the CHANGELOG say so in the same words, and this file pins them, so the pages and the behaviour
change together.

Mutations, each one alone: delete the sentence that says redaction restricts reports from the security note, the known
limitations page, the tool reference or the changelog; replace ``not removal`` with ``removal``; delete ``planned for
v0.21.0`` from any of the four; delete the archived sentence from any of the four; delete ``input_redacted`` from the tool
reference; delete the graph edge sentence from the tool reference or the security note; delete the belief sentence from the security note, the tool reference or the changelog; delete the doctor instruction
(``regenerate or delete``) from the tool reference or the changelog; change the doctor line in the code so that it no
longer says it, or the reason name in the code.
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PAGES = {
    "security note": "docs/release/derived-labels-security-note-draft.md",
    "known limitations": "docs/alpha/known-limitations.md",
    "tool reference": "docs/alpha/mcp-tools.md",
    "changelog": "CHANGELOG.md",
}
MARK = "Unreleased (on main, not in v0.20.0):"
# The owner's sentences. Each page holds each of them, with the words of the page around it.
RESTRICTS = (
    "redacting a memory now restricts reports and derived rows built from it to the owner and an unbound admin key until "
    "they are regenerated without it"
)
CONTAINMENT = (
    "this is temporary access containment, not removal: the text stays in those reports and the owner and an unbound admin "
    "key can still read it"
)
REMOVAL = "full removal of redacted text from reports is planned for v0.21.0"
ARCHIVED = "archiving keeps reports' historical content and does not restrict them"


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split()).lower()


def test_every_page_says_redaction_restricts_reports_until_they_are_regenerated() -> None:
    for name, path in PAGES.items():
        text = _flat(path)
        assert RESTRICTS in text, name
        assert text.count(RESTRICTS) >= 1


def test_every_page_says_this_is_containment_and_not_removal_and_when_removal_comes() -> None:
    for name, path in PAGES.items():
        text = _flat(path)
        assert CONTAINMENT in text, name
        assert REMOVAL in text, name


def test_every_page_says_archiving_keeps_the_historical_content() -> None:
    for name, path in PAGES.items():
        assert ARCHIVED in _flat(path), name


def test_each_sentence_stands_in_an_unreleased_paragraph_and_not_beside_a_release_that_has_it() -> None:
    """The sentences describe main, not v0.20.0, and no release note of a tag says them."""
    for name, path in PAGES.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        paragraphs = [item for item in re.split(r"\n\s*\n|\n(?=- )", text) if RESTRICTS in " ".join(item.split()).lower()]
        assert paragraphs, name
        assert all(MARK in " ".join(item.split()) for item in paragraphs), name
    for release in sorted((ROOT / "docs/release").glob("v*-release-notes.md")):
        assert RESTRICTS not in " ".join(release.read_text(encoding="utf-8").split()).lower(), release.name


def test_the_tool_reference_names_the_reason_the_doors_the_regeneration_and_the_graph_exception() -> None:
    text = _flat(PAGES["tool reference"])
    assert "`input_redacted`" in text
    assert "a derived row that recorded a redacted row as an input is now unverified" in text
    assert "and so is every row built from it: a report of reports, a promoted copy, a weekly that names it" in text
    for door in ("the artifact list, get and trace routes", "the memory audit", "`alice_explain`", "the context pack", "the workspace", "the dogfooding page", "the project dashboard"):
        assert door in text, door
    assert "regenerating restores access, and only when the redacted text is no longer included" in text
    assert "the old report stays restricted until it is deleted" in text
    assert "redacting a memory needs no relabel pass" in text
    assert "`get /v0/vnext/graph/neighborhood/{target_id}` and `alice_graph_neighborhood` return it to the owner and to an unbound admin or trusted key" in text
    assert "(#redacted-memories-and-the-graph)" in text


def test_the_security_note_and_the_changelog_name_the_graph_exception_and_the_doctor_instruction() -> None:
    note = _flat(PAGES["security note"])
    assert "the graph edges are the exception: they have no label" in note
    assert "that predates this change and is tracked for v0.21.0" in note
    assert "count these rows as unverified and say to regenerate or delete the reports built from a redacted memory" in note
    assert "because `labels repair` cannot fix them" in note
    log = _flat(PAGES["changelog"])
    assert "count these rows as unverified and say to regenerate or delete the reports built from a redacted memory" in log
    assert "because `labels repair` cannot fix them" in log
    assert "the edges of the connection graph are the one place that still returns the words" in log
    assert "redacting a memory needs no relabel pass, because the read check decides" in log


def test_three_pages_say_a_belief_of_a_redacted_memory_is_contained_with_the_reports() -> None:
    belief = (
        "a belief keeps the claim it copied from its memory, so a belief whose backing memory is redacted is read as a derived "
        "row with a redacted input is: by the owner and an unbound admin key only"
    )
    for name in ("security note", "tool reference", "changelog"):
        assert belief in _flat(PAGES[name]), name


def test_the_known_limitations_bullet_is_one_bullet_with_a_link_to_the_explanation() -> None:
    text = (ROOT / PAGES["known limitations"]).read_text(encoding="utf-8")
    bullets = [item for item in re.split(r"\n(?=- )", text) if RESTRICTS in " ".join(item.split()).lower()]
    assert len(bullets) == 1
    assert "(mcp-tools.md#redacted-memories)" in bullets[0]
    assert "(../release/derived-labels-security-note-draft.md)" in bullets[0]


def test_the_doctor_line_in_the_code_says_what_the_pages_say() -> None:
    from alicebot_api.vnext_label_repair import REDACTED_INPUT_ADVICE, REDACTED_INPUT_REASON

    assert REDACTED_INPUT_REASON == "input_redacted"
    advice = REDACTED_INPUT_ADVICE.lower()
    assert "regenerate or delete those reports" in advice
    assert "labels repair cannot fix them" in advice
    assert "only the owner and an unbound admin key read them" in advice
    for path in ("apps/api/src/alicebot_api/vnext_doctor.py", "apps/api/src/alicebot_api/vault_doctor.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "REDACTED_INPUT_ADVICE" in source and "built from a redacted memory" in source, path
