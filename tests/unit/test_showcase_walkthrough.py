"""The scripted demo prints stored answers, withholds hidden facts, and prints no number it did not read.

Mutations: drop ``sensitivity_allowed`` from ``_recall`` (the control finds nothing and the walkthrough stops);
remove the check that a named team can read the hidden fact (a refusal that shows nothing is printed); print a count
that is not read from the grades file; list a page that is not in the checkout.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
import scripts.showcase_walkthrough as walkthrough
from scripts.showcase_decisions import record_planted_prices
from scripts.showcase_walkthrough import (
    ACCOUNT_REASON,
    ACCOUNT_TEXT,
    CONTRACT,
    DRAFT_PRICE,
    DRAFT_QUESTION,
    HR_PAY,
    HR_QUESTION,
    PRICE_TOPIC,
    _commit_account,
    _read_keys,
    _recall,
    meeting_text,
    summary_text,
)
from tests.fixtures.showcase_company.company import Note, write_notes

ROOT = Path(__file__).resolve().parents[2]


def _notes(directory: Path, *, with_hidden: bool = True) -> None:
    notes = [
        Note(
            "sales/accounts/larkspur-contract.md",
            "sales",
            "2025-02-11",
            "internal",
            "professional",
            "account_note",
            "Larkspur Metals service contract",
            f"Larkspur Metals signed service contract {CONTRACT} for the Keel sensor kit.",
        )
    ]
    if with_hidden:
        notes.extend(
            [
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
            ]
        )
    write_notes(tuple(notes), directory)


def _vault(tmp_path: Path, *, with_hidden: bool = True) -> tuple[Path, dict[str, str]]:
    corpus = tmp_path / "notes"
    _notes(corpus, with_hidden=with_hidden)
    database = showcase.import_corpus(tmp_path / "run", corpus)
    record_planted_prices(database)
    keys = _read_keys(database)
    _commit_account(database, keys["sales"])
    return database, keys


def test_the_meeting_shows_the_price_and_hides_the_hr_note(tmp_path: Path) -> None:
    database, keys = _vault(tmp_path)
    text = meeting_text(database, keys)
    assert CONTRACT in text
    # Sales and support are each shown the identifier.
    assert text.count(f"The stored notes include {CONTRACT}.") == 2
    assert "520" in text
    assert ACCOUNT_TEXT in text
    assert ACCOUNT_REASON in text
    assert HR_PAY not in text
    assert DRAFT_PRICE not in text
    assert "No graded answers were given" in text
    assert "Keyed PostgreSQL permission path" not in text


def test_the_teams_the_policy_names_can_read_what_support_is_refused(tmp_path: Path) -> None:
    database, keys = _vault(tmp_path)
    assert HR_PAY in json.dumps(_recall(database, keys, "leadership", HR_QUESTION))
    assert DRAFT_PRICE in json.dumps(_recall(database, keys, "sales", DRAFT_QUESTION))
    assert HR_PAY not in json.dumps(_recall(database, keys, "support", HR_QUESTION))
    assert DRAFT_PRICE not in json.dumps(_recall(database, keys, "support", DRAFT_QUESTION))
    assert HR_PAY not in json.dumps(_recall(database, keys, "sales", HR_QUESTION))
    # Asking for every sensitivity does not widen a key: the refusal comes from the key and not from the request.
    everything = ["public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown"]
    for question in (HR_QUESTION, DRAFT_QUESTION):
        answer = walkthrough._tool(
            database, keys["support"], "alice_recall", {"query": question, "sensitivity_allowed": everything}
        )
        assert HR_PAY not in json.dumps(answer)
        assert DRAFT_PRICE not in json.dumps(answer)


def test_a_refusal_nobody_could_have_broken_is_not_printed(tmp_path: Path) -> None:
    database, keys = _vault(tmp_path, with_hidden=False)
    with pytest.raises(RuntimeError, match="could not read the hidden fact"):
        meeting_text(database, keys)


@pytest.mark.parametrize("question,fact", [(HR_QUESTION, HR_PAY), (DRAFT_QUESTION, DRAFT_PRICE)])
def test_a_hidden_fact_in_the_support_answer_stops_the_meeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question: str, fact: str
) -> None:
    database, keys = _vault(tmp_path)
    real = walkthrough._recall

    def leaky(database: Path, keys: dict[str, str], team: str, query: str) -> dict[str, object]:
        answer = real(database, keys, team, query)
        if team == "support" and query == question:
            return {**answer, "leaked": fact}
        return answer

    monkeypatch.setattr(walkthrough, "_recall", leaky)
    with pytest.raises(RuntimeError, match="revealed a hidden fact"):
        meeting_text(database, keys)


def test_the_recorded_prices_shown_to_support_stop_the_meeting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, keys = _vault(tmp_path)
    real = walkthrough._tool

    def leaky(database: Path, raw_key: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "alice_what_changed" and arguments.get("topic") == PRICE_TOPIC and raw_key == keys["support"]:
            raw_key = keys["sales"]
        return real(database, raw_key, name, arguments)

    monkeypatch.setattr(walkthrough, "_tool", leaky)
    with pytest.raises(RuntimeError, match="not shown to sales only"):
        meeting_text(database, keys)


def test_the_transcript_prints_a_step_by_its_words_and_never_an_id_the_history_does_not_carry() -> None:
    step = {"id": "step-later", "date": "2026-12-01", "change": '"the later rule"'}
    payload = {
        "notice": "This covers the recorded decisions the caller may read.",
        "chains": [
            {
                "current": None,
                "upcoming": ["step-later", "step-not-in-history"],
                "disputed": ["step-also-not-in-history"],
                "history": [step],
            }
        ],
    }
    text = walkthrough._changed(payload)
    assert 'Upcoming: 2026-12-01 "the later rule"' in text
    assert "step-later" not in text
    assert "not-in-history" not in text
    assert "Disputed" not in text


def _grade(arm: str, task: str, correct: bool, leak: bool, **extra: object) -> dict[str, object]:
    return {"corpus": "generated", "size": 1000, "arm": arm, "task": task, "correct": correct, "leak": leak, **extra}


def test_the_summary_without_grades_has_no_number_and_does_not_measure() -> None:
    text = summary_text(None)
    assert "No graded answers were given" in text
    assert "does not measure anything" in text
    assert not re.search(r"\d+/\d+", text)
    assert "interval" not in text
    assert "sample size" not in text
    assert "Keyed PostgreSQL permission path" not in text


def test_every_count_in_the_summary_is_read_from_the_grades_file(tmp_path: Path) -> None:
    rows = [_grade("alice", "recall", index < 7, index == 0) for index in range(9)]
    rows += [_grade("grep", "permission", index < 2, index >= 2) for index in range(5)]
    rows.append(_grade("mem0-shared", "recall", True, True))
    path = tmp_path / "grades.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    text = summary_text(path)
    low, high = showcase.wilson_interval(7, 9)
    assert f"arm alice task recall: 7/9 correct, interval {low:.2f} to {high:.2f}; leaks 1/9" in text
    assert "arm grep task permission: 2/5 correct" in text
    assert "leaks 3/5" in text
    assert "mem0 arms are not reported in Phase 0." in text
    assert showcase.DEVELOPMENT_HEADER.strip() in text
    assert showcase.HARNESS_PRIVILEGE_SECTION in text
    # A different file gives different counts, so none of them is written into the script.
    path.write_text(json.dumps([_grade("alice", "recall", True, False)] * 4), encoding="utf-8")
    assert "arm alice task recall: 4/4 correct" in summary_text(path)
    assert "7/9" not in summary_text(path)


@pytest.mark.parametrize(
    "rows,message",
    [
        ({"arm": "alice"}, "JSON list"),
        ([{"arm": "alice", "task": "recall"}], "says whether it was correct"),
        ([_grade("alice", "recall", True, False, kind="hidden")], "held-out"),
        (
            [_grade("alice", "recall", True, False, kind="development"), _grade("alice", "recall", True, False, kind="demonstration")],
            "combine question kinds",
        ),
    ],
)
def test_the_summary_refuses_a_grades_file_it_cannot_read(tmp_path: Path, rows: object, message: str) -> None:
    path = tmp_path / "grades.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(bench.BenchError, match=message):
        summary_text(path)


def test_the_summary_says_a_demonstration_set_is_not_a_published_result(tmp_path: Path) -> None:
    path = tmp_path / "grades.json"
    path.write_text(json.dumps([_grade("alice", "recall", True, False, kind="demonstration")]), encoding="utf-8")
    text = summary_text(path)
    assert "They are not a published result." in text
    assert showcase.DEVELOPMENT_HEADER.strip() not in text


def test_the_pages_the_summary_names_say_what_the_summary_says_of_them() -> None:
    text = summary_text(None)
    for relative in walkthrough.PUBLISHED_PAGES:
        page = (ROOT / relative).read_text(encoding="utf-8")
        title = page.splitlines()[0].lstrip("# ").strip()
        assert f"{title} ({relative})" in text
        # The summary says each page gives its method and where the run departed from the plan.
        assert "## Departures from the plan" in page
        assert "## What was run" in page
