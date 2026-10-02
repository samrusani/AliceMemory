"""The docs say what main does about the gaps the v0.20 wave-2 reviews found, with v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as they are. The documents that describe the
latest release mark what main changed with ``Unreleased (on main, not in v0.19.2):``,
and the changelog has one entry under the Unreleased heading that states the
v0.19.2 behaviour beside each change.

Every test names the edit that must fail it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.19.2):"
ENTRY_START = "Gaps that the reviews of the v0.20 wave-2 changes found are closed"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _entry() -> str:
    changelog = _changelog()
    unreleased = changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]
    entries = [item for item in unreleased.split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_has_one_entry_under_unreleased_and_none_above_v0192() -> None:
    """One entry, in the Unreleased section.

    Mutations, each one alone: move the entry under the v0.19.2 heading, or add a second
    entry that starts the same way.
    """
    assert _changelog().count(ENTRY_START) == 1
    assert _entry().startswith(ENTRY_START)


def test_the_entry_states_the_v0192_memory_id_behaviour() -> None:
    """The expired successor and the loop, each with what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says v0.19.2 named an id in both
    cases, or the sentence that says the status stays active after expire, or the one that
    names the loop.
    """
    entry = _entry()
    assert "`alice_memory_manage` with `action: expire` sets `valid_to` and leaves the status `active`" in entry
    assert "although `alice_recall` and `alice_context_pack` do not return it" in entry
    assert "was answered with the id of the memory the walk came back to, which is a superseded memory." in entry
    assert "In v0.19.2 both cases name an id." in entry


def test_the_pointer_residue_entry_no_longer_says_an_expired_successor_is_named() -> None:
    """The entry for the pointer residue change said an expired successor is still named. It is corrected.

    Mutation: put the sentence back.
    """
    changelog = _flat(_changelog())
    assert "a successor closed with `expire` is still named" not in changelog
    assert "A deleted successor already named no id. Second," in changelog


def test_the_tools_doc_marks_the_expired_successor_and_the_loop() -> None:
    """``current_memory_id`` is left off for a closed validity window and for a loop, and v0.19.2 is stated.

    Mutations: delete the marker, or the sentence that says what v0.19.2 names.
    """
    tools = _read("docs/alpha/mcp-tools.md")
    assert (
        MARK + " `current_memory_id` is also left off when the memory it would name has a validity window that has closed"
    ) in tools
    assert "`alice_memory_manage` with `action: expire` sets while the status stays `active`" in tools
    assert "A chain that loops back to a memory it already passed names no id." in tools
    assert "In v0.19.2 the id of the expired memory is named, and so is the id of the memory the loop came back to." in tools
