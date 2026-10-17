"""The docs say what v0.20.2 does about the four expiry and embedding gaps, with v0.20.0 as the comparison.

v0.20.0 is released, so its notes stay as they are. The documents that describe the release mark
what v0.20.2 changes with ``From v0.20.2,``, and the changelog has one entry under
``## v0.20.2`` that states the v0.20.0 behaviour beside each change.

Every test names the edit that must fail it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "From v0.20.2,"
ENTRY_START = "The four expiry and embedding gaps that the v0.20.0 notes list as known limitations are closed."
NOTES = "docs/release/v0.20.0-release-notes.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _released() -> str:
    changelog = _changelog()
    return changelog[changelog.index("## v0.20.2") : changelog.index("## v0.20.0")]


def _entry() -> str:
    entries = [item for item in _released().split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_has_one_entry_under_v0202_and_none_in_an_older_release() -> None:
    """One entry, in the v0.20.2 section.

    Mutations, each one alone: move the entry under the v0.20.0 heading, or add a second entry that starts
    the same way.
    """

    assert _changelog().count(ENTRY_START) == 1
    assert ENTRY_START in _released()
    assert _entry().startswith(ENTRY_START)


def test_the_entry_states_the_v0200_behaviour_beside_each_change() -> None:
    """Each of the four gaps has what v0.20.0 did, and the entry says what it leaves alone.

    Mutations, each one alone: delete the sentence that lists the three doors in v0.20.0, the one that gives
    the 3 and 2 of the decision vault, the one that gives the 6 and 4 of the embeddings vault, the one that
    says the promoted memory had no vector, or the sentence that leaves ``alice_context_pack`` unchanged.
    """

    entry = _entry()
    for needle in (
        "`alice_recent_decisions` listed 3 in v0.20.0 and lists 2",
        "`alice_resume` named the closed decision as `last_decision` and names the next one",
        "`alice-memory brief` had 3 fact lines and has 2",
        "consolidation sent 6 texts to the endpoint in v0.20.0 and sends 4",
        "In v0.20.0 the three doors listed an expired memory",
        "`list_accepted_rollup_cards` had no expiry test",
        "a promoted artifact's memory had no vector until the next `alice-memory reindex-embeddings`",
        "`expired_card_members_unchanged`",
        "`already_covered_by_accepted`",
        "This does not change `alice_context_pack`",
        "a user of a hosted endpoint should check that provider's retention terms",
        "a live-database test that CI runs",
    ):
        assert needle in entry, needle
    assert "\u2014" not in entry and "\u2013" not in entry


def test_the_known_limitations_list_what_stays_open_and_name_what_v0202_closed() -> None:
    """The open list keeps what stays open, and the closing paragraph names the four closed gaps, with v0.19.2 as the comparison.

    The three bullets that listed the gaps as open are gone from the open list: the gaps are fixed. One bullet
    keeps what is left (the context pack and the text already sent). The closing paragraph of the page names the four
    gaps as closed, and keeps the v0.19.2 comparison: recall and the context pack never returned an expired memory.

    Mutations, each one alone: put one of the three open bullets back, delete the sentence that says the context pack is
    unchanged, or delete a gap from the closing paragraph.
    """

    limitations = _read("docs/alpha/known-limitations.md")
    section = limitations[limitations.index("Open in v0.20.2, with the detail") :]
    assert "- the session brief, `alice_resume` and `alice_recent_decisions` list memories by status" not in section
    assert "- consolidation and the roll-up semantic tier embed, for clustering" not in section
    assert "- promoting a reviewed artifact into a memory does not embed the new memory" not in section
    bullet = section[section.index("- `alice_context_pack` is not changed by the expiry fix") : section.index("- the `max_tokens` budget")]
    assert "its `recent_changes` still names the id of an expired memory through its creation and expire events" in bullet
    assert "text that v0.20.0 and earlier sent to an embeddings endpoint" in bullet.lower()
    closing = limitations[limitations.index("What v0.20.0 listed as open and v0.20.2 closed") :]
    for gap in (
        "the session brief, `alice_resume`, `alice_recent_decisions` and the SessionStart hook showing a memory whose `valid_to` has passed",
        "while recall and the context pack do not return it, as in v0.19.2",
        "consolidation and the roll-up semantic tier sending the text of such a memory to the embeddings endpoint",
        "the roll-up pass counting an expired card as the accepted card for its topic",
        "artifact promotion leaving the new memory without a vector",
    ):
        assert gap in closing, gap


def test_the_v0200_notes_are_unchanged_and_carry_no_main_only_marker() -> None:
    """The shipped v0.20.0 notes keep listing the four gaps as open, with no main-only text in them.

    Mutations, each one alone: add the ``Unreleased`` marker to the notes, or delete one of the four
    limitation headings.
    """

    notes = _read(NOTES)
    assert "Unreleased (on main" not in notes
    for heading in (
        "**The session brief, `alice_resume` and `alice_recent_decisions` still show an expired active memory.**",
        "**Consolidation and the roll-up semantic tier can send an expired memory's text to the embeddings endpoint.**",
        "**The roll-up pass reads accepted roll-up cards without checking `valid_to`.**",
        "**Promoting a reviewed artifact into a memory does not embed it.**",
    ):
        assert heading in notes, heading


def test_the_control_documents_and_the_protocol_say_what_v0202_changes() -> None:
    """``CURRENT_STATE.md`` carries the line, and the protocol doc says which doors leave an expired memory out.

    Mutations, each one alone: delete the line from ``CURRENT_STATE.md``, or delete the sentence from the
    protocol doc.
    """

    state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
    flat = _flat(state)
    assert "- The brief, `alice_resume` and `alice_recent_decisions` leave out an expired memory" in flat
    assert "artifact promotion embeds the memory it makes" in flat
    protocol = _read("docs/memory-operations-protocol.md")
    assert (
        "In v0.20.0, `alice_resume`, `alice_recent_decisions` and the session brief still listed an expired memory"
    ) in protocol
    assert f"{MARK} all of them leave it out, with the test recall uses." in protocol
