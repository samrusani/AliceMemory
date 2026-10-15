"""The synthetic company generator writes a seeded firm and no one else's.

Quenby Fieldworks is fictional. These tests drive the generator and the script
that regenerates a folder. They need no network and no model.

Mutation: change HR_FOLDER_SENSITIVITY from confidential to internal. HR notes
would then be readable by a support key, and test_hr_notes_are_confidential fails.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import subprocess
import sys

from tests.fixtures.showcase_company.company import (
    ABSENT,
    COMPANY_NAME,
    DEFAULT_SEED,
    DRAFT_AUDIENCE,
    DRAFT_DOMAIN,
    DRAFT_SENSITIVITY,
    END,
    HR_FOLDER_DOMAIN,
    HR_FOLDER_SENSITIVITY,
    START,
    anchors,
    generate,
    validate,
    write_notes,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_showcase_company.py"


def test_the_company_name_is_fictional_and_the_two_sizes_repeat() -> None:
    assert COMPANY_NAME == "Quenby Fieldworks"
    small = generate(1000)
    again = generate(1000)
    large = generate(10000)
    assert [note.markdown() for note in small] == [note.markdown() for note in again]
    assert len(small) == 1000
    assert len(large) == 10000
    assert validate(small) == []
    assert validate(large) == []


def test_a_different_seed_changes_filler_and_not_a_planted_contract() -> None:
    first = {note.relative_path: note.markdown() for note in generate(1000, seed=DEFAULT_SEED)}
    second = {note.relative_path: note.markdown() for note in generate(1000, seed=DEFAULT_SEED + 1)}
    planted = "sales/accounts/larkspur-contract.md"
    assert first[planted] == second[planted]
    assert first != second


def test_every_note_carries_a_team_a_date_a_sensitivity_and_a_domain() -> None:
    notes = generate(1000)
    dates = []
    for note in notes:
        text = note.markdown()
        assert f"team: {note.team}\n" in text
        assert f"date: {note.on}\n" in text
        assert f"sensitivity: {note.sensitivity}\n" in text
        assert f"domain: {note.domain}\n" in text
        dates.append(date.fromisoformat(note.on))
    assert min(dates) == START
    assert max(dates) == END
    assert (END - START).days >= 500


def test_hr_notes_are_confidential() -> None:
    """HR notes use the HR folder label: confidential, stored as legal.

    Mutation: set HR_FOLDER_SENSITIVITY to internal.
    """

    assert HR_FOLDER_SENSITIVITY == "confidential"
    assert HR_FOLDER_DOMAIN == "legal"
    notes = [note for note in generate(1000) if note.kind == "hr_note" or note.relative_path.startswith("hr/")]
    assert len(notes) == 40
    assert notes
    for note in notes:
        assert note.team == "hr"
        assert note.sensitivity == "confidential"
        assert note.domain == "legal"
        assert "sensitivity: confidential\n" in note.markdown()


def test_pricing_drafts_are_for_sales_and_leadership_only() -> None:
    notes = [note for note in generate(1000) if note.kind == "pricing_draft"]
    assert len(notes) == 20
    for note in notes:
        assert note.sensitivity == DRAFT_SENSITIVITY == "private"
        assert note.domain == DRAFT_DOMAIN == "financial"
        assert note.audience == DRAFT_AUDIENCE == ("leadership", "sales")
        assert "support" not in note.audience
        text = note.markdown()
        assert "audience: leadership, sales\n" in text
        assert "sensitivity: private\n" in text


def test_planted_facts_cover_every_bench_task_and_abstentions_are_absent() -> None:
    notes = generate(10000)
    texts = {note.relative_path: note.markdown() for note in notes}
    tasks = {anchor.task for anchor in anchors()}
    assert tasks >= {"recall", "change", "connection", "contradiction", "corroboration", "permission"}
    for anchor in anchors():
        found = sorted(path for path, text in texts.items() if anchor.text in text)
        assert found == sorted(anchor.paths)
    blob = "\n".join(texts.values())
    for absent in ABSENT:
        assert absent not in blob
    # A decision is reversed, a customer changes status, two notes disagree,
    # and a support note plus a product note are both required.
    assert "replaces the list price decision of 2 June 2025" in texts["pricing/changes/keel-520.md"]
    assert "Bramble Dock status prospect" in texts["sales/accounts/bramble-prospect.md"]
    assert "Bramble Dock status closed" in texts["support/resolutions/bramble-closed.md"]
    assert "adding a second yard" in texts["sales/calls/bramble-yard.md"]
    assert "not adding a yard" in texts["support/resolutions/bramble-closed.md"]
    assert "Ticket QF-441 reports cold-start failures" in texts["support/tickets/qf-441.md"]
    assert "because of ticket QF-441" in texts["product/decisions/operating-floor.md"]
    assert "cold-start failures" not in texts["product/decisions/operating-floor.md"]


def test_a_folder_the_generator_does_not_own_is_refused(tmp_path: Path) -> None:
    """A hand-written note in a folder with no ownership marker is left alone.

    Mutation: change the refuse check ``if any(root.iterdir()) and not owned``
    so it does not refuse.
    """

    folder = tmp_path / "foreign"
    folder.mkdir()
    handwritten = folder / "handwritten.md"
    handwritten.write_text("keep this note\n", encoding="utf-8")
    try:
        write_notes([], folder)
    except ValueError as exc:
        assert "no generator ownership marker" in str(exc)
    else:
        raise AssertionError("a foreign folder was overwritten")
    assert handwritten.read_text(encoding="utf-8") == "keep this note\n"
    assert not (folder / ".showcase_company_owner").exists()
    assert list(folder.rglob("*.md")) == [handwritten]


def test_a_handwritten_note_in_an_owned_folder_is_kept(tmp_path: Path) -> None:
    folder = tmp_path / "owned"
    notes = [note for note in generate(1000) if note.relative_path == "sales/accounts/span-open.md"]
    write_notes(notes, folder)
    handwritten = folder / "handwritten.md"
    handwritten.write_text("keep this note\n", encoding="utf-8")
    write_notes(notes, folder)
    assert handwritten.read_text(encoding="utf-8") == "keep this note\n"
    assert (folder / "sales" / "accounts" / "span-open.md").is_file()
    assert (folder / ".showcase_company_owner").read_text(encoding="utf-8").startswith("showcase company generator owns")


def test_a_note_the_generator_wrote_and_no_longer_produces_is_removed(tmp_path: Path) -> None:
    folder = tmp_path / "owned"
    first = [note for note in generate(1000) if note.relative_path == "sales/accounts/span-open.md"]
    second_source = next(note for note in generate(1000) if note.relative_path == "product/meetings/span-close.md")
    write_notes(first, folder)
    write_notes([second_source], folder)
    assert not (folder / "sales" / "accounts" / "span-open.md").exists()
    assert (folder / "product" / "meetings" / "span-close.md").is_file()


def test_the_script_writes_the_folder_the_library_builds(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--size", "1000", "--out", str(tmp_path / "company")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    written = sorted((tmp_path / "company").rglob("*.md"))
    assert len(written) == 1000
    sample = tmp_path / "company" / "sales" / "accounts" / "larkspur-contract.md"
    library = next(note for note in generate(1000) if note.relative_path == "sales/accounts/larkspur-contract.md")
    assert sample.read_text(encoding="utf-8") == library.markdown()
