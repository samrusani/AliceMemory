"""The pages say what redacting a memory does to the reports made from it: contained now, removed later.

Redacting a memory restricts the reports and derived rows built from it to the owner and an unbound admin key until they are
regenerated without it. That is temporary access containment and not removal: the text stays in those reports, and full
removal is planned for v0.21.0. Archiving is not redaction and restricts nothing. The security note, the known limitations
page, the tool reference and the CHANGELOG say so in the same words, and this file pins them, so the pages and the behaviour
change together.

Mutations, each one alone: delete the sentence that says redaction restricts reports from the security note, the known
limitations page, the tool reference or the changelog; replace ``not removal`` with ``removal``; delete ``planned for
v0.21.0`` from any of the four; delete the archived sentence from any of the four; delete ``input_redacted`` from the tool
reference; delete the graph edge sentence, the project end sentence or the belief review sentence from the tool reference, the
security note or the changelog; restore a sentence that calls the graph an exception from any page; delete the belief sentence from the security note,
the tool reference or the changelog; delete the sentence that says the review route also refuses an edge whose end is an
artifact, an open loop or a project from the security note, the tool reference or the changelog; delete the sentence that says
who reads the words of an archived row and of a redacted memory from any of the four; restore a sentence that says the graph
edge explanations stay outside the ceiling, that the neighborhood returns the edges of any id, or that three answers show more
than a missing id, in any page; delete the dated update from the v0.20.0 notes; delete the cross-reference from the redact section of the tool reference or of the
protocol; delete the doctor instruction
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
# The sentences each page must hold, with the words of the page around them.
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
# The graph and the belief review route are contained like the reports, on the three pages that describe the doors.
EDGE = (
    "a graph edge has no label and keeps the explanation it was made with, which can hold the title of a memory redacted since, "
    "so `get /v0/vnext/graph/neighborhood/{target_id}` lists and `post /v0/vnext/graph/edges/{edge_id}/review` changes an edge "
    "for a key with limits only when that key may read every labelled row the edge joins and none of them is redacted"
)
# A project end is named by the id of its row or by any identifier the caller typed. Only a row that exists can hide an edge.
PROJECT_END = (
    "a project end that names the project by a name or by an id that finds no project row hides nothing, as an entity end hides "
    "nothing; a project row that exists and is not readable still hides the edge"
)
BELIEF_REVIEW = (
    "`post /v0/vnext/beliefs/{belief_id}/review` is refused to a key that may not read the belief, with the answer of a missing "
    "belief, and a refused review changes nothing"
)
# The review route checks four kinds of end, so it refuses an edge with an end of another kind to a key with limits.
REVIEW_KINDS = (
    "the review route checks the memory, source, belief and entity ends only, so it also refuses an edge whose end is an "
    "artifact, an open loop or a project to a key with limits, as it refuses an edge whose end no longer exists, even when "
    "that key may read the row and the neighborhood lists the edge"
)
# Who reads the words a report kept. An archived row restricts nothing, so a report keeps its words for every key that reads
# the report. A redacted memory contains the reports that recorded it.
ARCHIVED_WORDS = {
    "security note": "a key reads the words of an archived row as it read them in the report",
    "known limitations": "the words of a memory redacted after the report was made stay in it too, but then only the owner and an unbound admin key read the report",
    "tool reference": "the words of a memory that was redacted after the report was made stay in the report as well, but a report that recorded a redacted memory is read by the owner and an unbound admin key only",
    "changelog": "the words of a memory that was redacted after the report was made stay in the report as well, but a report that recorded a redacted memory is read by the owner and an unbound admin key only",
}
REDACTED_WORDS = (
    "the words of a redacted memory are not read that way: a report that recorded a redacted memory is read by the owner and an "
    "unbound admin key only"
)
# Sentences an earlier version of the pages held, which the behaviour no longer matches.
RETIRED = (
    "the one place that still returns the words",
    "the graph edges are the exception",
    "graph edge explanations also keep it",
    "events follow the rows it may read",
    "no total, preview, snippet, quote, title or event payload",
    # The graph neighborhood lists only the edges whose ends the key may read, so these no longer hold.
    "graph edge explanations remain outside that ceiling",
    "graph neighborhood still returns edge explanations outside that ceiling",
    "the graph neighborhood route returns the edges of any id with their explanations",
    "the graph neighborhood route still returns the edges of any id with their explanations",
    "it applies the operator gate and no label fence",
    "keeps the limit named above",
    "the graph-edge limit named",
    "three answers show a little more",
    "the three answers the security note lists",
    # A redacted memory contains the reports that recorded it, so a key that can read a report does not read its words.
    "so a redacted memory's text can still be read in them",
    "redacting a memory does not rewrite the reports that printed it",
    "hiding them from a caller with limits is tracked for v0.21.0",
    "withholding these words for a caller with limits is tracked for v0.21.0 with the ids",
    "the ids of redacted and archived members",
    "the ids of members that were redacted or archived",
)


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


def test_the_tool_reference_names_the_reason_the_doors_the_regeneration_and_the_legacy_tools() -> None:
    text = _flat(PAGES["tool reference"])
    assert "`input_redacted`" in text
    assert "a derived row that recorded a redacted row as an input is now unverified" in text
    assert "and so is every row built from it: a report of reports, a promoted copy, a weekly that names it" in text
    for door in ("the artifact list, get and trace routes", "the memory audit", "`alice_explain`", "the context pack", "the workspace", "the dogfooding page", "the project dashboard"):
        assert door in text, door
    assert "regenerating restores access, and only when the redacted text is no longer included" in text
    assert "the old report stays restricted until it is deleted" in text
    assert "no command deletes a report today: archiving a report keeps its row and its text, so an archived report stays restricted" in text
    assert "redacting a memory needs no relabel pass" in text
    assert "the recommended fix of the check is to regenerate or delete those reports, and not `alicebot vnext labels repair`" in text
    assert "(#redacted-memories-and-the-graph)" in text
    assert "no text, title, preview, snippet or quote of a hidden report is shown" in text
    assert "the only tools a key can call are the core tools" in text
    assert "`alice_graph_neighborhood`, `alice_graph_edge_review`, `alice_belief_review` and `alice_belief_state` are the mcp twins" in text


def test_three_pages_say_a_graph_edge_and_a_belief_review_are_judged_by_the_rows_they_join() -> None:
    for name in ("security note", "tool reference", "changelog"):
        text = _flat(PAGES[name])
        assert EDGE in text, name
        assert BELIEF_REVIEW in text, name
        assert "any other edge answers as a missing edge does (the review answers 404), and a refused review changes nothing" in text, name
        assert PROJECT_END in text, name


def test_no_page_still_calls_the_graph_an_exception_or_claims_what_the_doors_do_not_do() -> None:
    for name, path in PAGES.items():
        text = _flat(path)
        for sentence in RETIRED:
            assert sentence not in text, (name, sentence)


def test_every_page_says_who_reads_the_words_of_an_archived_row_and_of_a_redacted_memory() -> None:
    """Archived: kept and read as before by every key that reads the report. Redacted: kept, and contained."""
    for name, sentence in ARCHIVED_WORDS.items():
        assert sentence in _flat(PAGES[name]), name
    assert REDACTED_WORDS in _flat(PAGES["security note"])
    assert "that is containment and not removal, and removing the words is planned for v0.21.0" in _flat(PAGES["security note"])
    kept = _flat(PAGES["known limitations"])
    assert "the title, text or quote of a row that was archived after the report was made stays in the report's text" in kept
    assert "and a key that can read the report reads those words" in kept
    tools = _flat(PAGES["tool reference"])
    assert "the title, text or quote of a row that was archived after the report was made stays in the report's text" in tools
    assert "and a key that can read the report reads those words" in tools


def test_the_pages_say_what_the_graph_routes_do_now() -> None:
    """The neighborhood lists only the edges whose ends the key may read, on every page that once said it did not."""
    assert "it now lists only the edges whose ends the key may read" in _flat(PAGES["security note"])
    assert "graph neighborhood returned edge explanations outside that ceiling in v0.20.0 and baseline main 48873b03" in _flat(PAGES["security note"])
    assert "and the graph neighborhood lists only the edges whose ends the key may read" in _flat(PAGES["known limitations"])
    assert "the graph neighborhood and the edge review route apply the same ceiling to the rows an edge joins" in _flat(PAGES["changelog"])
    assert "the rule that fences it is under [graph edges and beliefs](#redacted-memories-and-the-graph)" in _flat(PAGES["tool reference"])
    notes = _flat("docs/release/v0.20.0-release-notes.md")
    assert "update (2026-10-09)" in notes
    assert "on main, not in v0.20.0, it lists only the edges whose ends the key may read, and the graph edge review route applies the same rule" in notes
    for name in ("security note", "tool reference", "changelog"):
        assert REVIEW_KINDS in _flat(PAGES[name]), name


def test_the_older_redact_sections_point_to_the_reports_that_kept_the_words() -> None:
    tools = _flat(PAGES["tool reference"])
    assert "it does not rewrite the reports and cards made from the memory before: they keep the words they copied" in tools
    assert "(unreleased, see [redacted memories](#redacted-memories))" in tools
    protocol = _flat("docs/memory-operations-protocol.md")
    assert "redact does not rewrite the reports and derived rows made from the memory before" in protocol
    assert "temporary access containment, not removal" in protocol and "full removal is planned for v0.21.0" in protocol
    assert "archiving a memory restricts nothing" in protocol
    assert "[redacted memories](alpha/mcp-tools.md#redacted-memories)" in protocol


def test_the_security_note_and_the_changelog_name_the_doctor_instruction() -> None:
    note = _flat(PAGES["security note"])
    assert "count these rows as unverified and say to regenerate or delete the reports built from a redacted memory" in note
    assert "because `labels repair` cannot fix them" in note
    log = _flat(PAGES["changelog"])
    assert "count these rows as unverified and say to regenerate or delete the reports built from a redacted memory" in log
    assert "because `labels repair` cannot fix them" in log
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
