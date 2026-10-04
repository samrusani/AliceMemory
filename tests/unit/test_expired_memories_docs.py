"""The docs say what main does about the four expiry and embedding gaps, with v0.20.0 as the comparison.

v0.20.0 is released, so its notes stay as they are. The documents that describe the latest release mark
what only main does with ``Unreleased (on main, not in v0.20.0):``, and the changelog has one entry under
``## Unreleased`` that states the v0.20.0 behaviour beside each change.

Every test names the edit that must fail it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
ENTRY_START = "The four expiry and embedding gaps that the v0.20.0 notes list as known limitations are closed."
NOTES = "docs/release/v0.20.0-release-notes.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _unreleased() -> str:
    changelog = _changelog()
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.20.0")]


def _entry() -> str:
    entries = [item for item in _unreleased().split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_has_one_entry_under_unreleased_and_none_in_a_release() -> None:
    """One entry, in the Unreleased section.

    Mutations, each one alone: move the entry under the v0.20.0 heading, or add a second entry that starts
    the same way.
    """

    assert _changelog().count(ENTRY_START) == 1
    assert ENTRY_START in _unreleased()
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


def test_the_known_limitations_keep_the_v0200_text_and_mark_what_main_changes() -> None:
    """The three open bullets keep what v0.20.0 does and add the main-only change after the marker.

    Mutations, each one alone: delete the marker from a bullet, delete the v0.20.0 clause before it, or
    delete the sentence that says the context pack is unchanged.
    """

    limitations = _read("docs/alpha/known-limitations.md")
    section = limitations[limitations.index("Open in v0.20.0, with the detail") :]
    brief, consolidation, promotion = (
        section[section.index("- the session brief, `alice_resume`") : section.index("- consolidation and the roll-up")],
        section[section.index("- consolidation and the roll-up") : section.index("- promoting a reviewed artifact")],
        section[section.index("- promoting a reviewed artifact") : section.index("- the `max_tokens` budget")],
    )
    # One marker in each of the three bullets the expiry change marked. The count is of these three bullets and not
    # of the whole section, so a later change that marks another bullet of the section does not break this test.
    assert (brief.count(MARK), consolidation.count(MARK), promotion.count(MARK)) == (1, 1, 1)
    assert "list memories by status and do not check `valid_to`" in brief.split(MARK)[0]
    assert "as in v0.19.2." in brief.split(MARK)[0]
    assert "`alice_context_pack` is not changed" in brief.split(MARK)[1]
    assert "embed, for clustering, memories that already hold a vector and do not check `valid_to`" in (
        consolidation.split(MARK)[0]
    )
    assert "(`list_accepted_rollup_cards`) without checking `valid_to`" in consolidation.split(MARK)[0]
    assert "`expired_card_members_unchanged`" in consolidation.split(MARK)[1]
    assert "does not embed the new memory" in promotion.split(MARK)[0]
    assert "embeds the new memory after the commit, once" in promotion.split(MARK)[1]


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


def test_the_control_documents_and_the_protocol_say_what_main_changes() -> None:
    """``CURRENT_STATE.md`` carries the line, and the protocol doc says which doors leave an expired memory out.

    Mutations, each one alone: delete the line from ``CURRENT_STATE.md``, or delete the sentence from the
    protocol doc.
    """

    state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
    flat = _flat(state)
    assert f"- {MARK} the brief, `alice_resume` and `alice_recent_decisions` leave out an expired memory" in flat
    assert "artifact promotion embeds the memory it makes" in flat
    protocol = _read("docs/memory-operations-protocol.md")
    assert (
        "In v0.20.0, `alice_resume`, `alice_recent_decisions` and the session brief still listed an expired memory"
    ) in protocol
    assert f"{MARK} all of them leave it out, with the test recall uses." in protocol
