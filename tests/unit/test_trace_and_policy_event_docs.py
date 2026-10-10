"""The notes say what the source trace and the policy event of a continuity object show a caller with limits.

The security note and the changelog each carry one paragraph for the source trace and one for the policy event of an explain
of a continuity object. This test fails when a sentence that states the rule, the callers it leaves alone or the limit that
stays is taken out or changed.

Mutations: take one of the pinned sentences out of the note or the changelog.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
NOTE_TRACE = f"{MARK} the source trace follows the rows its events name."
NOTE_CONTINUITY = f"{MARK} the policy event of an explain of a continuity object is not shown to a caller with limits."
LOG_TRACE = f"{MARK} the source trace follows the rows its events name."
LOG_CONTINUITY = f"{MARK} the event feeds do not show a key with limits the policy event of an explain of a continuity object."


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _paragraph(text: str, start: str) -> str:
    assert text.count(start) == 1, start
    return start + text.split(start, 1)[1].split("\n", 1)[0]


def test_the_security_note_states_the_rule_of_the_source_trace() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), NOTE_TRACE)
    for phrase in (
        "`GET /v0/vnext/traces/sources/{id}` and the trace that `POST /v0/vnext/sources/{id}/review` embeds show an event, to a caller with limits, only when the caller may read every labelled row the event names",
        "the row it is about and the ids in its payload",
        "the correction of a memory (`memory.reviewed`) named the memory that replaced it in `replacement_memory_id` after that memory was made confidential",
        "The events that target a row the trace does not list are still left out",
        "`summary.event_count` counts the events shown",
        "the owner and an unbound admin key keep every event of the source",
        "a profile handed to the loader inside the process that the gate would refuse is answered as a missing source",
        "the `superseded_by` that a `memory.updated` event copies in its `changes`",
        "A caller with limits is read the newest 2,000 events that the trace asks for, the reach of the event feeds, and no further",
        "`sampling.collection_complete.events` and `sampling.trace_complete` are `false`, and the events older than the reach are not listed",
        "The owner and an unbound admin key are read as before, 501 events at a time",
    ):
        assert phrase in paragraph, phrase


def test_the_security_note_states_what_is_known_about_the_policy_event_of_a_continuity_object() -> None:
    paragraph = _paragraph(_text("docs/release/derived-labels-security-note-draft.md"), NOTE_CONTINUITY)
    for phrase in (
        "records a `policy.decision` event with the target type `continuity_object` and the id of the object, and no other path writes one",
        "A call with no key, and a call that declares a permission profile without a key, never reach the recording",
        "a key-bound caller whose policy would not allow the object is stopped before anything is recorded and leaves what a call on an id that is not stored leaves",
        "so no caller leaves an event that names an object it may not read",
        "the domain and sensitivity the decision judged (`requested_domains` and `requested_sensitivity_allowed`, and the project scope)",
        "it cannot show that a reader with narrower limits than the writer may read the object",
        "the workspace `recent_events` and `agent_activity`, the context tree, the dogfooding view and the policy telemetry, and counted in the workspace `event_count`",
        "the owner and an unbound admin key still see it",
        "the tree filters by a selection of sensitivities and domains, the owner's default selection leaves out the confidential levels, and a selection is not a limit",
        "leaves out an event about a continuity object only for a caller that has limits, whatever it selected; the owner and an unbound admin key keep the event under every selection",
        "The route, the tool and the command line each tell the tree whether the caller has limits, and a tree built by code that does not say is built for a caller with limits",
        "A key therefore does not read its own explains of continuity objects in its telemetry",
        "Judging the event by the labels of the object, which would show a key its own explains, needs the guard to read the legacy store and is not done",
        "it fails when a target type is neither judged, nor listed as unlabelled, nor listed as withheld here, or when a type is written through an expression it cannot resolve and is not listed with the rows it can be",
    ):
        assert phrase in paragraph, phrase


def test_the_changelog_has_one_entry_for_each() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    for start, phrases in (
        (LOG_TRACE.removeprefix("- "), (
            "the correction of a memory (`memory.reviewed`) named the memory that replaced it in `payload_json.replacement_memory_id` even after that memory was made confidential",
            "only when the key may read every labelled row the event names",
            "The filter on the target stays",
            "The owner and an unbound admin key are unchanged and keep every event of the source",
            "one handed to it inside the process is answered as a missing source",
            "A key with limits is read the newest 2,000 events that the trace asks for, as the event feeds are",
        )),
        (LOG_CONTINUITY.removeprefix("- "), (
            "so no caller left an event that names an object it may not read",
            "with the id of the object and its domain and sensitivity",
            "it is not shown to a key with limits, in the feeds or in the workspace `event_count`, and the owner and an unbound admin key still see it",
            "the owner's own selection of sensitivities, which by default leaves out the confidential levels, is not a limit: the owner and an unbound admin key keep the event under every selection",
            "now reads a target type through the functions that hand it on",
            "a target type written through an expression it cannot resolve must be listed with the rows it can be",
        )),
    ):
        matches = [entry for entry in entries if entry.startswith(start)]
        assert len(matches) == 1, start
        for phrase in phrases:
            assert phrase in matches[0], (start, phrase)
        assert matches[0].endswith("No migration is required.")
