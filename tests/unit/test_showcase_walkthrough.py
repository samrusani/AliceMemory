"""The scripted demo prints stored answers and withholds hidden facts."""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.showcase_company.company import Note, write_notes
import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
from scripts.showcase_decisions import record_planted_prices
from scripts.showcase_walkthrough import (
    ACCOUNT_TEXT,
    CONTRACT,
    DRAFT_PRICE,
    HR_PAY,
    _commit_account,
    _read_keys,
    meeting_text,
)


def _notes(directory: Path) -> None:
    write_notes(
        (
            Note(
                "sales/accounts/larkspur-contract.md",
                "sales",
                "2025-02-11",
                "internal",
                "professional",
                "account_note",
                "Larkspur Metals service contract",
                f"Larkspur Metals signed service contract {CONTRACT} for the Keel sensor kit.",
            ),
            Note(
                "hr/notes/mara-quen-pay.md",
                "hr",
                "2025-04-02",
                "confidential",
                "legal",
                "hr_note",
                "Pay record",
                f"Mara Quen compensation is {HR_PAY} ledger units. This pay record is an HR note.",
            ),
            Note(
                "pricing/drafts/keel-910.md",
                "sales",
                "2025-11-01",
                "private",
                "financial",
                "pricing_draft",
                "Keel sensor kit draft price",
                f"Draft list price {DRAFT_PRICE} for the Keel sensor kit.",
            ),
        ),
        directory,
    )


def test_the_meeting_shows_the_price_and_hides_the_hr_note(tmp_path: Path) -> None:
    corpus = tmp_path / "notes"
    _notes(corpus)
    run = tmp_path / "run"
    database = showcase.import_corpus(run, corpus)
    record_planted_prices(database)
    keys = _read_keys(database)
    _commit_account(database, keys["sales"])
    text = meeting_text(database, keys, run)
    assert CONTRACT in text
    assert "520" in text
    assert ACCOUNT_TEXT in text
    assert HR_PAY not in text
    assert DRAFT_PRICE not in text
    assert "No graded answers are recorded" in text
    assert "prepare_demo_product" not in Path("scripts/alice_bench.py").read_text(encoding="utf-8")
    assert bench.DEFAULT_USER_ID
