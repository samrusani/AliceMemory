"""The pages say what the refill does from v0.21.0, mark it as not in v0.20.2, and give its real boundary.

The refill is new in v0.21.0. v0.20.2 does not have it, and the section of ``CURRENT_STATE.md`` that describes what
v0.20.2 shipped keeps v0.20.2's open item, that the vector stage of recall does not refill after hiding rows. The refill
is described in a bullet of the v0.21.0 section of ``CURRENT_STATE.md``, in the changelog, the known limitations (marked
``From v0.21.0,``) and the security note.

The boundary is stated as the page size gives it. A key with limits is read once, at 128 rows, and the page is full while
the hidden rows in front of it number at most 128 minus the rows the list is asked for. ``alice_recall`` asks its vector
stage for twice its limit, so at limit 5 the bound is 118 hidden rows, which ``test_label_refill_doors.py`` pins at the edge.

Mutations, each one alone: delete ``and the vector stage of recall does not refill after hiding rows.`` from the
v0.20.2 section of ``CURRENT_STATE.md``; delete the bullet of the refill from the v0.21.0 section there; move the
sentence about the refill into the ``Still open`` bullet of the v0.20.2 section; replace ``128 minus the rows the list is asked
for`` with ``128`` in any of the four pages; delete the sentence that names 118 hidden rows from the changelog, the
current state or the security note.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "From v0.21.0,"
BOUND = "while the hidden rows in front of it number at most 128 minus the rows the list is asked for"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _bullets(section: str) -> list[str]:
    return [" ".join(bullet.split()) for bullet in section.split("\n- ")]


def test_current_state_keeps_the_v0202_open_item_and_names_the_refill_in_the_v0210_section() -> None:
    text = _text("CURRENT_STATE.md")
    section = text.split("## What `v0.20.2` Shipped", 1)[1].split("\n## ", 1)[0]
    refilling = [bullet for bullet in _bullets(section) if "refill" in bullet]
    assert len(refilling) == 1, refilling
    open_item = refilling[0]
    assert open_item.startswith("Still open, listed in the release notes:")
    assert open_item.endswith("and the vector stage of recall does not refill after hiding rows.")
    assert open_item.count("refill") == 1
    assert "judging at most 128" not in section
    shipped = text.split("## What `v0.21.0` Shipped", 1)[1].split("\n## ", 1)[0]
    refilled = [bullet for bullet in _bullets(shipped) if "refill" in bullet]
    assert len(refilled) == 1, refilled
    new = refilled[0]
    assert new.startswith("The vector stage of recall and of the context pack, the time stage of the context pack")
    assert "the context tree, the workspace and the project list refill after hiding rows for a key with limits" in new
    assert BOUND in new
    assert "118 for `alice_recall` at limit 5, which asks its vector stage for ten rows" in new
    assert "The owner and an unbound admin key are read as before." in new


def test_the_changelog_entry_of_the_refill_states_the_real_boundary() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    refill = [entry for entry in entries if entry.startswith("The vector stage of `alice_recall` and of the context pack")]
    assert len(refill) == 1
    entry = refill[0]
    assert BOUND in entry
    assert "`alice_recall` asks its vector stage for twice its limit, so at limit 5 that is 118 hidden rows" in entry
    assert "from 119 the stage returns fewer candidates (nine at 119, one at 127, none from 128)" in entry
    assert "Past that bound the page is short, which says only that hidden rows fill the window." in entry
    assert "While the hidden rows stay inside those 128" not in entry
    assert entry.endswith("No migration is required.")


def test_the_known_limitations_entry_of_the_refill_states_the_v0202_limit_and_the_real_boundary() -> None:
    """The bullet says what v0.20.2 does in one sentence and what v0.21.0 does in a sentence marked ``From v0.21.0,``.

    Mutations, each one alone: delete ``v0.20.2 does not refill a page after it hides rows``, delete the marker, delete
    ``128 minus the rows the list is asked for`` or the sentence that names 118 hidden rows from the bullet.
    """

    page = _text("docs/alpha/known-limitations.md")
    entries = [line.removeprefix("- ") for line in page.splitlines() if line.startswith("- ")]
    refill = [entry for entry in entries if "v0.20.2 does not refill a page after it hides rows" in entry]
    assert len(refill) == 1
    entry = refill[0]
    shipped, marker, new = entry.partition(MARK)
    assert marker
    assert "the vector stage of recall and of the context pack and the time stage of the context pack can return fewer results than exist, down to none" in shipped
    assert "the context tree, the workspace and the project list can return short pages" in shipped
    assert "these refill, and a page is short when the hidden rows in front of it number more than 128 minus the rows the list is asked for (118 for `alice_recall` at limit 5)" in new


def test_the_security_note_keeps_the_v0202_limit_and_gives_the_refill_its_own_marked_paragraph() -> None:
    note = _text("docs/release/derived-labels-security-note-draft.md")
    paragraphs = [paragraph for paragraph in note.split("\n\n") if "refill" in paragraph]
    shipped = [p for p in paragraphs if p.startswith("From v0.20.2, the vector stage of recall and of the context pack does not yet refill after hiding rows.")]
    assert len(shipped) == 1
    assert "Refilling the vector stage is tracked for v0.21.0." in shipped[0]
    assert "They now refill" not in note
    refilled = [p for p in paragraphs if p.startswith(f"{MARK} the vector stage of recall and of the context pack")]
    assert len(refilled) == 1
    paragraph = refilled[0]
    assert BOUND in paragraph
    assert "so at limit 5 it asks for ten rows: the answer and the trace are the same up to 118 hidden rows in front of the page" in paragraph
    assert "the stage returns nine candidates at 119, one at 127 and none from 128" in paragraph
    assert "the bound of each is 128 minus that number" in paragraph
    assert "Past the bound the page is short, which says only that hidden rows fill the window, and it can be empty." in paragraph
    assert "Nothing the caller may not read is shown." in paragraph
    assert "v0.20.2 did not refill these stages or lists." in paragraph
    # The paragraph on the bounded reads of v0.20.2 keeps what v0.20.2 does for the two stages.
    resume = [p for p in note.split("\n\n") if p.startswith("From v0.20.2, `alice_resume`, the open loops of the context pack")]
    assert len(resume) == 1
    assert "the vector stage and the temporal-anchor stage of the context pack take their nearest rows first and drop the ones a key may not read afterwards" in resume[0]
