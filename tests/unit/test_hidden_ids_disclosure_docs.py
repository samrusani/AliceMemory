"""The pages say that a readable row can list the id of a row the key cannot read, and what that does and does not give.

The sentences are pinned by phrase. The behaviour is tested in ``tests/integration/test_row_ids_grant_no_access_postgres.py``,
``tests/integration/test_hidden_ids_in_report_metadata_postgres.py`` and ``tests/unit/test_row_ids_grant_no_access_sqlite.py``.

Mutations, each one alone: delete ``not a way of meeting the requirement to filter hidden ids`` from the security note;
delete ``They grant no access`` from the security note; delete ``is answered exactly as for a missing id`` from the security
note; delete the sentence that names the graph neighborhood route from the security note; delete ``withholding them is
tracked for v0.21.0`` from the known limitations; delete ``The full-text stage does refill until its limit is met.`` from
the security note; delete ``where it raised a server error`` from the changelog; delete ``return them for a derived
memory`` from the tool reference; delete ``holds `project_id` to the caller's project binding`` from the security note;
delete ``Ids are not the only thing a report keeps`` (or the sentence that names a redacted memory's text) from the
security note, or ``a report keeps the words it was made with`` from the known limitations, or ``A report also keeps the words it was made with`` from the tool reference,
or ``keeps the title, text or quote of a row`` from the changelog; delete the sentence about an archived or redacted
memory from the security note, the changelog or the tool reference; delete the sentence about the memory audit route, the `capture_content_hash` of a
memory or the scheduler's run records from the security note, the known limitations, the tool reference or the changelog; delete a sentence of the
paragraph on the event feeds from the security note, or the changelog entry for the event feed.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
IDS = f"{MARK} rows a key can read may list in their metadata the ids of rows it cannot read"
KEPT = f"{MARK} a report keeps the words it was made with"
VECTOR = f"{MARK} the vector stage of recall and of the context pack does not yet refill after hiding rows"
BELIEF = f"{MARK} belief review and graph edge review"
EXACT = f"{MARK} the doors that act on one row by id answer a row above"
LOOP_PROJECT = f"{MARK} `POST /v0/vnext/open-loops` holds `project_id`"
AUDIT = f"{MARK} the memory audit route lists the redacted and archived members of a derived memory"
EVENTS = f"{MARK} the event feeds follow the rows they name"
EVENT_FEED = f"{MARK} the workspace event feed and event count follow the rows an event names"


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
    # The memory audit route and the fields a memory keeps from its source.
    assert "The memory audit route (`GET /v0/vnext/memories/{id}/audit`) returns the same lists for a readable derived memory as stored" in paragraph
    for field in ("consolidation.cluster_member_ids", "derived_from.memories", "source_refs", "value.rollup.member_ids"):
        assert f"`{field}`" in paragraph, field
    assert "the same fields in its revisions (`previous_value` and `new_value`), and the same fields in the `changes` of its events" in paragraph
    assert "so the ids of members that were redacted or archived stand there too" in paragraph
    assert "A memory made from a captured source also keeps the `source_id`, the `source_event_ids` (the source and its chunk) and the `capture_content_hash` it was made with after the source is archived." in paragraph
    assert "The hash is the SHA-256 of the source's whole captured text with its project scope." in paragraph
    assert "It is not the text, but it tells a caller who holds a text whether it is the one the source had." in paragraph
    assert "The detail mode of `alice_memory_review` and the memory audit return these three fields, and the workspace returns them in its event feed, in the `changes` that the events of the memory copied from its metadata" in paragraph
    assert "`alice_explain` returns them while the source can be read and answers the memory of an archived source as unavailable." in paragraph
    assert "where it also names an archived source by the `target_id` of the source's own events" in paragraph
    assert "The scheduler's run records, in the workspace (`scheduler.recent_runs` and `scheduler.last_success_by_workflow`) and in the dogfooding view (`last_successful_scheduler_run`), carry the `artifact_id` of the report a run made, also after that report was made confidential." in paragraph
    assert "The memory tools return none of them" not in paragraph  # the detail modes of two tools do return them
    assert "The ids are random UUIDs" in paragraph
    assert "They reveal that the row exists and how a report links to it. They grant no access." in paragraph
    assert "is answered exactly as for a missing id: the same status and body, or the same tool error, and no stored row changes." in paragraph
    assert "is tracked for v0.21.0" in paragraph
    assert "a report keeps the words of a source that was archived later" not in paragraph  # it moved into its own sentences
    # The report keeps more than ids, and the note says what: the printed lines, the quoted fields, the roll-up card and
    # the digest of a consolidation member, and that a redaction does not reach the reports that copied the memory.
    assert "Ids are not the only thing a report keeps." in paragraph
    assert "the words of a row that was archived or redacted after the report was made stay in it" in paragraph
    assert "The `content_markdown` of a daily brief, a weekly synthesis, an open-loop review and a project update prints the lines of the rows it used." in paragraph
    assert "The `explanation` of a connection holds the title and shared terms of a source, the `quote_new` of a contradiction holds its text, and the `suggested_current_state` of a project update holds its claim." in paragraph
    assert "A roll-up card keeps the text, label and amounts of every memory it rolled up (`value.rollup.instances`)" in paragraph
    assert "a consolidation candidate keeps the 16-character digest of each member's title, text, summary and value as it was when the candidate was made (`member_snapshots`)" in paragraph
    assert "Redacting a memory scrubs the memory, its revisions, its events, its quoted provenance and the project update artifacts coupled to it, and does not rewrite the reports and cards that printed or copied it, so a redacted memory's text can still be read in them." in paragraph
    assert "withholding these words for a caller with limits is tracked for v0.21.0 with the ids" in paragraph


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
    assert (
        "An archived or redacted memory is read by no door but redact, and it is outside the limits of every key that has any, so a "
        "refused redact of one is answered and written exactly as a redact of a missing id is: HTTP 404 or the tool's not-found "
        "error, and no policy event and no agent record."
    ) in paragraph
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


def test_the_security_note_says_the_event_feeds_follow_the_rows_they_name() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), EVENTS)
    assert "The workspace `recent_events` and `agent_activity`, the context tree and the dogfooding view show an event, and the workspace event count counts it" in paragraph
    assert "only when the caller may read every labelled row the event names" in paragraph
    assert "the row it is about, the two ends of a graph edge it is about (an entity end has no label), and the ids in its payload" in paragraph
    for field in ("source_id", "source_ids", "memory_id", "candidate_memory_ids", "artifact_id", "belief_id", "project_id"):
        assert f"`{field}`" in paragraph, field
    assert "A chunk of a source has no label, so its `source_chunk.created` event takes the label of the source its payload names" in paragraph
    assert "a chunk event that names no source, or one that is not stored, is not shown to a caller with limits" in paragraph
    assert "An edge with an end of another kind, or an end that is not stored, cannot be shown to be readable and is not shown to a caller with limits either." in paragraph
    assert "Until this change every caller was shown these events, one per chunk, with the id of a source above the ceiling of the key, and the event count included them." in paragraph
    assert "The owner and an unbound admin key are not limited and still see them." in paragraph
    assert "The ids of rows without a label (an entity, a task, a scheduler run, a provenance link, a revision) are not judged" in paragraph
    assert "the `changes` that a `memory.updated` or `project.updated` event copies from a row list the same ids as the row" in paragraph
    assert "a second test lists every event type the code writes and fails when a new target type or a new id field is not classified" in paragraph


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
    audit = _paragraph(page, f"- {AUDIT}")
    assert "and a memory made from a captured source keeps its `source_id`, `source_event_ids` and `capture_content_hash` (the SHA-256 of the whole captured text) after the source is archived" in audit
    assert "which the detail mode of `alice_memory_review`, the memory audit and the event feed of the workspace return" in audit
    assert "the scheduler's run records in the workspace also keep the `artifact_id` of a report made confidential since" in audit
    assert "withholding these is tracked for v0.21.0" in audit
    assert "(see the [draft security note](../release/derived-labels-security-note-draft.md))" in audit
    assert "a known exception, not a way of meeting the requirement to filter hidden ids" in paragraph
    assert "withholding them is tracked for v0.21.0" in paragraph
    assert "(see the [draft security note](../release/derived-labels-security-note-draft.md))" in paragraph

    kept = _paragraph(page, f"- {KEPT}")
    assert ", so the title, text or quote of a row that was archived or redacted after the report was made stays in the report's text" in kept
    assert "in a roll-up card and as the digest of a consolidation member, and redacting a memory does not rewrite the reports that printed it" in kept
    assert "a key that can read the report reads those words, and hiding them from a caller with limits is tracked for v0.21.0" in kept
    assert "(see the [draft security note](../release/derived-labels-security-note-draft.md))" in kept
    vector = _paragraph(page, f"- {VECTOR}")
    assert "a restricted caller can get fewer results than exist, down to none" in vector
    assert "tracked for v0.21.0" in vector


def test_the_changelog_has_one_entry_for_each() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    for start, phrases in (
        (IDS, ("and the notes now say so", "any key whose limits admit the report", "they grant no access", "is answered as a missing id is, with the same status and body or the same tool error", "This is a known exception, not a way of meeting the requirement to filter hidden ids", "is tracked for v0.21.0", "Three answers show a little more", "it also keeps the words and values it was made with, so a report keeps the title, text or quote of a row that was archived or redacted after the report was made", "redacting a memory does not rewrite the reports that printed it", "The memory audit route (`GET /v0/vnext/memories/{id}/audit`) returns the same lists for a readable derived memory", "so the ids of redacted and archived members stand there too", "keeps its `source_id`, `source_event_ids` and `capture_content_hash` (the SHA-256 of the source's whole captured text) after the source is archived", "also carry the `artifact_id` of the report a run made")),
        (EXACT, ("repeated the row's domain, sensitivity and project scope", "carried the id of a project outside its binding", "writes what a call on a missing id writes, which is nothing", "no policy event and no agent record", "A row the key may read keeps its refusals", "superseding memory was not found", "An archived or redacted memory is read by no door but redact, and it is outside the limits of every key that has any, so a refused redact of one is answered and written exactly as a redact of a missing id is")),
        (LOOP_PROJECT, ("a database error (HTTP 500)", "vNext project was not found", "write nothing")),
        (EVENT_FEED, ("a `source_chunk.created` event for every chunk of a source above its sensitivity ceiling", "with the id of the source in `payload_json.source_id`", "and counted each one in `event_count`", "a chunk event that names no source is not shown to a key with limits", "the two ends of a graph edge an event is about", "The context tree and the dogfooding view read the same rule", "The owner and an unbound admin key are unchanged", "an event whose payload names an artifact, a belief or a project is not shown to a key with limits", "are not judged")),
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
    assert "`GET /v0/vnext/memories/{id}/audit` returns the same lists for a readable derived memory, in the memory, its revisions and the `changes` of its events, the ids of redacted and archived members included." in ids
    assert "A memory made from a captured source also keeps its `source_id`, `source_event_ids` and `capture_content_hash` (the SHA-256 of the source's whole captured text) after the source is archived" in ids
    assert "the detail mode of `alice_memory_review` and the memory audit return them, as does the event feed of the workspace; `alice_explain` returns them while the source can be read." in ids
    assert "The scheduler's run records in the workspace and the dogfooding view carry the `artifact_id` of the report a run made, also after the report was made confidential." in ids
    assert "It grants no access" in ids
    assert "is answered as a missing id is, with the same status and body or the same tool error, and nothing is changed" in ids
    assert "Three answers show a little more and are named in the release security note" in ids
    assert "This is a known exception to the rule that a hidden id is not shown, tracked for v0.21.0." in ids
    assert "A report also keeps the words it was made with, so the title, text or quote of a row that was archived or redacted after the report was made stays in the report's text" in ids
    assert "and redacting a memory does not rewrite the reports that printed it" in ids
    exact = _paragraph(tools, f"{MARK} the doors that act on one row by id answer a row above the key's limits as a row that does not exist.")
    assert "Until now these doors answered HTTP 403 with the policy decision" in exact
    assert "The call now writes what a call on a missing id writes, which is nothing: no policy event and no agent record" in exact
    assert "A row the key may read keeps its refusals." in exact
    assert "An archived or redacted memory is read by no door but redact, and it is outside the limits of every key that has any" in exact
    loop = _paragraph(tools, f"{MARK} `POST /v0/vnext/open-loops` holds `project_id` to the key's project binding.")
    assert "vNext project was not found" in loop
