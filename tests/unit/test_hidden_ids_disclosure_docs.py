"""The pages say that a readable row can list the id of a row the key cannot read, and what that does and does not give.

The sentences are pinned by phrase. The behaviour is tested in ``tests/integration/test_row_ids_grant_no_access_postgres.py``,
``tests/integration/test_hidden_ids_in_report_metadata_postgres.py`` and ``tests/unit/test_row_ids_grant_no_access_sqlite.py``.

Mutations, each one alone: delete ``not a way of meeting the requirement to filter hidden ids`` from the security note;
delete ``They grant no access`` from the security note; delete ``is answered exactly as for a missing id`` from the security
note; delete the sentence that names the graph neighborhood route from the security note; delete ``withholding them is
tracked for v0.21.0`` from the known limitations; delete ``The full-text stage does refill until its limit is met.`` from
the security note; delete ``where it raised a server error`` from the changelog; delete ``return them for a derived
memory`` from the tool reference; delete ``holds `project_id` to the caller's project binding`` from the security note.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
IDS = f"{MARK} rows a key can read may list in their metadata the ids of rows it cannot read"
VECTOR = f"{MARK} the vector stage of recall and of the context pack does not yet refill after hiding rows"
BELIEF = f"{MARK} belief review and graph edge review"
EXACT = f"{MARK} the doors that act on one row by id answer a row above"
LOOP_PROJECT = f"{MARK} `POST /v0/vnext/open-loops` holds `project_id`"


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
    assert "a 16-character digest of its title, text, summary and value (`member_snapshots`)" in paragraph
    assert "none of these is the member's text" in paragraph
    assert "The artifact get and artifact trace routes return these lists as stored to any key whose limits admit the report" in paragraph
    assert "a read-only, project-scoped or project-bound key included" in paragraph
    assert "The artifact list, source trace, workspace and project dashboard are operator routes" in paragraph
    assert "the owner, an unbound admin key and an unbound trusted key" in paragraph
    assert "The detail modes of `alice_explain` and `alice_memory_review` return them for a derived memory" in paragraph
    assert "the list modes of the other memory tools return none of them" in paragraph
    assert "The memory tools return none of them" not in paragraph  # the detail modes of two tools do return them
    assert "The ids are random UUIDs" in paragraph
    assert "They reveal that the row exists and how a report links to it. They grant no access." in paragraph
    assert "is answered exactly as for a missing id: the same status and body, or the same tool error, and no stored row changes." in paragraph
    assert "is tracked for v0.21.0" in paragraph
    assert "a report keeps the words of a source that was archived later" in paragraph


def test_the_security_note_names_the_three_answers_that_show_more_than_a_missing_id() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), IDS)
    assert "Three answers show a little more, and they belong to this exception." in paragraph
    assert "The recall, context-pack, resume and recent-decisions tools take the id as query text and show nothing of the row" in paragraph
    assert (
        "The graph neighborhood route returns the edges of any id with their explanations, which hold the titles and shared terms "
        "of the rows an edge joins"
    ) in paragraph
    assert "a memory commit whose `source_refs` names an id under a prefix other than `source:`" in paragraph
    assert "which tells a key whether an id it already holds names such a source and nothing more" in paragraph
    # The old claim that the refusal is allowed to repeat labels is gone: no door answers a hidden id with a policy decision.
    assert "can repeat its domain, sensitivity and project scope" not in paragraph
    assert "Neither answer shows" not in paragraph


def test_the_security_note_says_the_exact_doors_answer_a_hidden_row_as_a_missing_one() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), EXACT)
    assert "answered HTTP 403 with that decision (a tool answered with the policy error)" in paragraph
    assert "carried the id of a project outside its binding" in paragraph
    for door in ("artifact get, trace, review, feedback, quality-rating and export routes", "memory audit, review, correct, expire, forget, redact, undo, unexpire and accept-consolidation routes", "`alice_open_loops` (close, edit, snooze, reopen)"):
        assert door in paragraph, door
    assert "and the call writes what a call on a missing id writes, which is nothing" in paragraph
    assert "That includes the policy events and the agent record: a key reads them back in its own policy telemetry" in paragraph
    assert "The `alice_explain` tool stops at the same point for each row it would expand" in paragraph
    assert "A refusal for a row the key may read is recorded and answered as before, and it repeats only labels the key can read." in paragraph
    assert "The owner and an unbound admin key are unchanged." in paragraph
    assert "so a refused confirm keeps its refusal" in paragraph
    assert "(the event log and the agent records included), with those of a missing id, and read the key's own telemetry before and after" in paragraph


def test_the_security_note_says_the_open_loop_route_holds_the_project_to_the_binding() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), LOOP_PROJECT)
    assert paragraph.startswith(" to the caller's project binding.")  # the pinned start holds "holds `project_id`"
    assert "a key bound to one project could file an open loop under a project outside its binding" in paragraph
    assert "a database error (HTTP 500)" in paragraph
    assert "A project outside the binding, a project that does not exist and an id that is not well formed" in paragraph
    assert "write nothing" in paragraph


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
    assert "and an id that is not a well-formed id, now answers HTTP 404 for every caller" in paragraph
    assert "Read-only, proposal and project-bound keys keep the HTTP 403 of the operator gate." in paragraph


def test_the_known_limitations_page_lists_both_limits() -> None:
    page = _text("docs/alpha/known-limitations.md")
    paragraph = _paragraph(page, f"- {IDS}")
    assert "such as the candidate memory a report made or a source archived since" in paragraph
    assert "The ids grant no access and show that the row exists" in paragraph
    assert "a call that names one is answered as a missing id is answered, except for the three answers the security note lists" in paragraph
    assert "which can repeat the row's labels" not in paragraph
    assert "a known exception, not a way of meeting the requirement to filter hidden ids" in paragraph
    assert "withholding them is tracked for v0.21.0" in paragraph
    assert "(see the [draft security note](../release/derived-labels-security-note-draft.md))" in paragraph
    vector = _paragraph(page, f"- {VECTOR}")
    assert "a restricted caller can get fewer results than exist, down to none" in vector
    assert "tracked for v0.21.0" in vector


def test_the_changelog_has_one_entry_for_each() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    for start, phrases in (
        (IDS, ("and the notes now say so", "any key whose limits admit the report", "they grant no access", "is answered as a missing id is, with the same status and body or the same tool error", "This is a known exception, not a way of meeting the requirement to filter hidden ids", "is tracked for v0.21.0", "Three answers show a little more")),
        (EXACT, ("repeated the row's domain, sensitivity and project scope", "carried the id of a project outside its binding", "writes what a call on a missing id writes, which is nothing", "no policy event and no agent record", "A row the key may read keeps its refusals", "superseding memory was not found")),
        (LOOP_PROJECT, ("a database error (HTTP 500)", "vNext project was not found", "write nothing")),
        (VECTOR, ("fewer results than exist, down to none", "Tracked for v0.21.0")),
        (BELIEF + " apply the caller's limits", ("In v0.19.2, in v0.20.0 and on main until now", "change the stored row", "where it raised a server error", "an id that is not a well-formed id")),
    ):
        matches = [entry for entry in entries if entry.startswith(start)]
        assert len(matches) == 1, start
        for phrase in phrases:
            assert phrase in matches[0], (start, phrase)
        assert matches[0].endswith("No migration is required.")
    ids_entry = next(entry for entry in entries if entry.startswith(IDS))
    assert "can repeat its domain, sensitivity and project scope" not in ids_entry
    assert "the memory tools return none" not in ids_entry


def test_the_tool_reference_states_both() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    belief = _paragraph(tools, f"{MARK} `POST /v0/vnext/beliefs/{{id}}/review` and `POST /v0/vnext/graph/edges/{{id}}/review` apply the same operator gate and the caller's read fence.")
    assert "a belief it would be replaced by is held to the same rule" in belief
    assert "Anything else answers HTTP 404 with the body of a missing row" in belief
    assert "and an id that is not well formed, answers the same for every caller" in belief
    ids = _paragraph(tools, f"{MARK} a row a key can read may list in its metadata the id of a row the key cannot read.")
    assert "any key whose limits admit the report" in ids
    assert "The detail modes of `alice_explain` and `alice_memory_review` return them for a derived memory" in ids
    assert "The memory tools return none." not in ids
    assert "It grants no access" in ids
    assert "is answered as a missing id is, with the same status and body or the same tool error, and nothing is changed" in ids
    assert "Three answers show a little more and are named in the release security note" in ids
    assert "This is a known exception to the rule that a hidden id is not shown, tracked for v0.21.0." in ids
    exact = _paragraph(tools, f"{MARK} the doors that act on one row by id answer a row above the key's limits as a row that does not exist.")
    assert "Until now these doors answered HTTP 403 with the policy decision" in exact
    assert "The call now writes what a call on a missing id writes, which is nothing: no policy event and no agent record" in exact
    assert "A row the key may read keeps its refusals." in exact
    loop = _paragraph(tools, f"{MARK} `POST /v0/vnext/open-loops` holds `project_id` to the key's project binding.")
    assert "vNext project was not found" in loop
