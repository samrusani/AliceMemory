"""The docs say what v0.20.0 does about the three memory id pointer defects, with v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as they are and the documents that
describe the latest release mark what v0.20.0 changed with
``From v0.20.0,``. The changelog entry sits under the
v0.20.0 heading and states the v0.19.2 behaviour beside each fix.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "From v0.20.0,"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _entry() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = changelog[changelog.index("## v0.20.0 \u2014 2026-10-02") + len("## v0.20.0 \u2014 2026-10-02") : changelog.index("## v0.19.2")]
    entries = [item for item in released.split("\n- ")[1:] if item.startswith("Three memory id defects")]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_entry_sits_under_v0200_and_states_v0192() -> None:
    """One entry, three fixes, and the v0.19.2 behaviour beside each.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete the
    sentence that says v0.19.2 named the retired memory; delete the sentence that says
    the v0.19.2 pack worked out `validity` after dropping the pointer; delete the
    sentence that says v0.19.2 returned the id in a debug call; delete the sentence
    that says an id that names no memory is kept; delete the extra lookup and timing.
    """
    entry = _entry()
    assert "In v0.19.2 the label named the forgotten, undone or rejected memory, to every caller that could read it." in entry
    assert "A deleted successor already named no id. Second," in entry
    assert (
        "In v0.19.2 the pack dropped the pointer first and worked out `validity` afterwards, so a memory that was still "
        "active and carried such a pointer had no `validity` in the pack, while recall returned `superseded: true`."
    ) in entry
    assert (
        "In v0.19.2 an `alice_context_pack` call with `debug: true` returned it for decision, procedure and belief "
        "memories, to the keyless owner under the default sensitivity ceiling and to a read-only key."
    ) in entry
    assert "An id that names no memory is kept" in entry
    assert "A source id or a chunk id is kept because it names no memory." in entry
    assert "one extra `get_memories_by_ids` call of 16 ids" in entry
    assert "Postgres was not timed." in entry


def test_the_known_limitations_state_what_the_pack_does_now_and_leave_the_fixed_gaps_to_the_records() -> None:
    """The page states what the pack does now and the one thing it does not cover; the fixed gaps are not on it.

    Both gaps are fixed in v0.20.0, so the limitations page lists what is limited now: the pack
    removes the id of a memory the caller cannot read from ``metadata_json``, and looks only for a
    36-character UUID there. What v0.19.2 did is pinned on the dated records: the changelog entry
    (``test_the_changelog_entry_sits_under_v0200_and_states_v0192`` above) holds the debug pack gap for
    decision, procedure and belief memories and the ``validity.superseded`` gap, and the published v0.19.2
    notes still list both as open (``test_the_published_v0192_notes_are_not_changed``).

    Mutations: delete the 36-character UUID sentence or the sentence that says what the pack removes
    from the page; put either v0.19.2 bullet back on the page (``in v0.19.2 a memory id copied into a stored
    memory's `metadata_json` is returned without the read fence``, ``in v0.19.2 the context pack drops
    `validity.superseded` ``).
    """
    text = _read("docs/alpha/known-limitations.md")
    assert (
        "`alice_context_pack` removes the id of a memory the caller cannot read from the `metadata_json` of the "
        "memories it returns, in every section that holds them"
    ) in text
    assert "Only a 36-character UUID in `metadata_json` is looked for" in text
    assert "an id in another column of a stored row or in another spelling is not" in text
    assert "is returned without the read fence by an `alice_context_pack` call" not in text
    assert "the context pack drops `validity.superseded`" not in text


def test_the_threat_model_and_the_tools_doc_mark_what_v0200_changed() -> None:
    """The threat model and the tool descriptions say what v0.19.2 does and what v0.20.0 does.

    Mutations: delete a marker; delete the v0.19.2 comparison that follows it.
    """
    threat = _read("docs/security/threat-model.md")
    assert MARK + " the `metadata_json` place is fenced too" in threat
    assert "The correction label names no id when the memory it would name was forgotten, undone or rejected, where v0.19.2 named it." in threat
    assert "where v0.19.2 gave that memory no `validity` in the pack and recall kept `superseded: true`." in threat
    tools = _read("docs/alpha/mcp-tools.md")
    assert (
        MARK + " `current_memory_id` is also left off when the memory it would name was forgotten, undone or rejected"
    ) in tools
    assert "In v0.19.2 the id of the forgotten, undone or rejected memory is named." in tools
    assert (
        MARK + " a memory whose `superseded_by` pointer names a memory outside that fence keeps `validity.superseded: true` in the pack"
    ) in tools
    assert "In v0.19.2 the pack has no `validity` for it." in tools
    assert "From v0.20.0, the id of a memory the caller cannot read is removed from it." in tools


def test_the_published_v0192_notes_are_not_changed() -> None:
    """The v0.19.2 notes still list both defects as open and carry no Unreleased marker.

    Mutation: edit the notes to say the defects are fixed.
    """
    notes = _read("docs/release/v0.19.2-release-notes.md")
    assert "**Memory ids in `metadata_json` are not fenced.**" in notes
    assert "**The pack can lose `validity.superseded` for a hidden pointer.**" in notes
    assert "Unreleased (on main" not in notes
