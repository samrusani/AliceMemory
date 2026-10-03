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


def test_the_known_limitations_keep_v0192_and_mark_the_fix() -> None:
    """Both entries keep the v0.19.2 sentence and add the marked fix after it.

    Mutations: delete the v0.19.2 half of either entry; delete the marker; say the
    fix is in v0.19.2.
    """
    text = _read("docs/alpha/known-limitations.md")
    assert (
        "in v0.19.2 a memory id copied into a stored memory's `metadata_json` is returned without the read fence by an "
        "`alice_context_pack` call with `debug: true`"
    ) in text
    assert (
        "to the keyless owner under the default sensitivity ceiling and to a read-only key. From v0.20.0 it is fixed."
    ) in text
    assert (
        "where recall keeps it; this needs a row whose status is still active. From v0.20.0 it is fixed. The pack "
        "keeps `validity.superseded: true` and names no id, as recall does"
    ) in text
    assert "Only a 36-character UUID in `metadata_json` is looked for" in text


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
