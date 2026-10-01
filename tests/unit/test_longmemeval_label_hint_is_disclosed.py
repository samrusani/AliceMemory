"""The LongMemEval session id label hint stays disclosed, and the counts the docs give match the committed evidence.

In LongMemEval_s the id of every evidence session starts with ``answer_`` and
no filler session id does. The harness put that id in the header above each
excerpt and in the first line of each session's text, so the reader model could
see which sessions held the evidence. The effect is unmeasured. The README, the benchmark README,
the honesty kit and every other markdown page that states 81.2%, 79.4% or 64.6%
say so, and the published release notes that state a number carry one dated
correction.

What is pinned here, and what is not:

* The two counts the docs give that the committed per-question files can
  prove are recomputed from those files: how many questions list a retrieved
  ``answer_`` session in each replication run, how many reader answers name an
  ``answer_`` session id in each run and in the 2026-07-07 file. The ids come
  in three shapes (``answer_`` and eight hex characters, ``answer_sharegpt_``
  and ``answer_ultrachat_``), so the count matches ``answer_`` followed by any
  id character. The dataset
  counts (948 of 948 evidence ids, 0 of 22,919 filler ids) need
  ``longmemeval_s_cleaned.json``, which is not in the repo, so they are not
  checked here. They were checked once by hand and are in the pull request.
* Any tracked markdown page that writes 81.2%, 79.4% or 64.6% as a result has
  to carry the disclosure. The pages are the ones ``git ls-files`` lists, so a
  checkout that holds nested agent worktrees or local notes is not scanned, and
  a test fails if the scan stops finding the pages it must find.

Mutations that must fail this file, each alone:

* delete the "Known issue with this number." paragraph from README.md, or move
  it above the first LongMemEval paragraph: the README test fails;
* change "495 of 500" or "29 to 32" in README.md, or "37 reader answers" in the
  benchmark README, to another number: the evidence test fails;
* write "title" back into the label sentence of either README, or delete the
  sentence "The earlier 64.6% and 79.4% runs used the same harness code." from
  the README: the README test or the evidence test fails;
* delete the known-issue note from PRODUCT_BRIEF.md, or add "81.2%" to a
  markdown page that does not mention ``answer_``: the scan test fails;
* walk the whole directory again instead of the tracked files: the nested
  checkout case fails;
* delete the CHANGELOG entry: the changelog test fails;
* delete the dated correction from a published release note, or date it
  another day: the release note test fails for that note;
* put the old "four published" count back into the honesty kit, delete its
  replication row, or call the 79.4% run the headline there again: the honesty
  kit test fails.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
BENCH_DIR = REPO_ROOT / "docs" / "benchmarks" / "longmemeval"
BENCH_README = BENCH_DIR / "README.md"
HONESTY_KIT = BENCH_DIR / "HONESTY-KIT.md"
REPLICATION_DIR = BENCH_DIR / "replication-v0.12.0-2026-07-19"
RUN_20260707 = BENCH_DIR / "per-question-results-2026-07-07.jsonl"

# The reader's answer names a session id when it writes answer_ and an id
# character, however it formats the rest ("session answer_x", "[Session answer_x").
# The ids are answer_ and eight hex characters, answer_sharegpt_ and answer_ultrachat_
# followed by the rest. Matching only the hex form missed about a quarter of the answers.
NAMED_SESSION_ID = re.compile(r"answer_[0-9A-Za-z]")

# Published notes that state 81.2%, 79.4% or 64.6% as a result, each with
# exactly one dated correction paragraph about the label.
CORRECTED_NOTES = (
    "docs/release/v0.8.0-release-notes.md",
    "docs/release/v0.9.2-release-notes.md",
    "docs/release/v0.13.1-release-notes.md",
    "docs/release/v0.15.7-release-notes.md",
    "docs/release/v0.16.0-release-notes.md",
)
CORRECTION_OPENING = re.compile(r"^\W*Correction\W+2026-10-01\b")

# Markdown pages the scan must find. If the walk stops finding one, the scan
# would pass because it looked at nothing.
MUST_BE_SCANNED = (
    "README.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "CURRENT_STATE.md",
    ".ai/handoff/CURRENT_STATE.md",
    "ARCHITECTURE.md",
    "docs/benchmarks/longmemeval/README.md",
    "docs/benchmarks/longmemeval/HONESTY-KIT.md",
    "docs/reports/provenance-grounded-memory.md",
    "docs/roadmap-friction-first.md",
    *CORRECTED_NOTES,
)
# CHANGELOG.md keeps its history as written, so it is not scanned. Its
# Unreleased entry states the issue.
SCAN_EXEMPT = frozenset({"CHANGELOG.md"})
# A percentage written as a result. The lookbehind keeps 79.4009% (coverage) out.
STATED_NUMBER = re.compile(r"(?<![\d.])(?:81\.2|79\.4|64\.6)%")


def _paragraphs(text: str) -> list[str]:
    """Blocks of text split on blank lines, quote markers removed, each joined onto one line."""

    blocks: list[list[str]] = [[]]
    for line in text.splitlines():
        stripped = re.sub(r"^[\s>]+", "", line)
        if stripped:
            blocks[-1].append(stripped)
        else:
            blocks.append([])
    return [" ".join(block) for block in blocks if block]


def _section(text: str, heading: str) -> str:
    """The ``## heading`` section of a markdown file, up to the next ``## `` heading."""

    lines = text.splitlines()
    start = lines.index(f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end])


def _last_row_per_question(path: Path) -> dict[str, dict]:
    """Rows scored ok, one per question id, the last row winning, as the harness aggregates."""

    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["question_id"]] = row
    return {key: row for key, row in rows.items() if row.get("status") == "ok"}


def _named_ids(rows: dict[str, dict]) -> int:
    return sum(1 for row in rows.values() if NAMED_SESSION_ID.search(row.get("hypothesis") or ""))


def _labelled_questions(rows: dict[str, dict]) -> int:
    count = 0
    for row in rows.values():
        provenance = (row.get("retrieval") or {}).get("provenance") or {}
        if any(str(i).startswith("answer_") for i in provenance.get("source_session_ids") or []):
            count += 1
    return count


def _tracked_markdown_paths(root: Path) -> list[str]:
    """Relative paths of the markdown files git tracks under ``root``, in sorted order.

    A directory walk would read nested agent worktrees (``.claude/worktrees``),
    editor folders and local notes, so the test would pass in a clean CI checkout
    and fail in a working checkout. Tracked files are what ships.
    """

    listed = subprocess.run(
        ("git", "-C", str(root), "ls-files", "-z", "--", "*.md"),
        capture_output=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr.decode("utf-8", errors="replace")
    return sorted(item.decode("utf-8") for item in listed.stdout.split(b"\0") if item)


def _markdown_files_stating_a_number(root: Path) -> dict[str, str]:
    """Relative path to text, for every tracked markdown page that writes 81.2%, 79.4% or 64.6%."""

    found: dict[str, str] = {}
    for relative in _tracked_markdown_paths(root):
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if STATED_NUMBER.search(text):
            found[relative] = text
    return found


def _pages_without_the_pointer(pages: dict[str, str]) -> list[str]:
    return sorted(
        name for name, text in pages.items() if name not in SCAN_EXEMPT and "answer_" not in text
    )


# --- README ------------------------------------------------------------------


def test_readme_second_benchmark_paragraph_discloses_the_label_hint() -> None:
    """Mutation: delete the paragraph, move it above the first LongMemEval paragraph,
    write "title" back into the label sentence, or delete the 64.6% and 79.4% sentence.

    The first LongMemEval paragraph stays first so the lead guard in
    test_readme_does_not_lead_with_privileged_lme.py still reads store_chunks
    and v0.12.0 there. This one is the second.
    """

    paragraphs = _paragraphs(_section(README.read_text(encoding="utf-8"), "Benchmark"))
    lme = [p for p in paragraphs if "LongMemEval" in p]
    assert lme[0].startswith("A LongMemEval_s receipt of 81.2% mean"), lme[0][:80]
    assert lme[1].startswith("Known issue with this number."), lme[1][:80]
    second = lme[1]
    for needle in (
        "`answer_`",
        "in the header above each excerpt and in the first line of each session's text",
        "a retrieved session carried that label on 495 of 500 questions",
        "29 to 32 answers per run",
        "unknown amount",
        "81.2%",
        "The earlier 64.6% and 79.4% runs used the same harness code.",
        "`pack_excerpts`",
        "replace these numbers rather than sit beside them",
    ):
        assert needle in second, needle


# --- the counts match the committed evidence ------------------------------------


def test_stated_counts_match_the_committed_per_question_files() -> None:
    """Mutation: change 495, 29 to 32 or 37 in a doc, or tighten the pattern to hex ids so it under-counts."""

    labelled: list[int] = []
    named: list[int] = []
    for run in (1, 2, 3):
        rows = _last_row_per_question(REPLICATION_DIR / f"per-question-results-run{run}.jsonl")
        assert len(rows) == 500
        labelled.append(_labelled_questions(rows))
        named.append(_named_ids(rows))
    assert labelled == [495, 495, 495]
    assert (min(named), max(named)) == (29, 32), named
    rows_0707 = _last_row_per_question(RUN_20260707)
    assert len(rows_0707) == 500
    assert _named_ids(rows_0707) == 37
    assert not any(
        "session_id" in key for row in rows_0707.values() for key in {**row, **(row.get("retrieval") or {})}
    ), "the 2026-07-07 file was said to have no session id field"

    readme = README.read_text(encoding="utf-8")
    assert "495 of 500" in readme
    assert f"{min(named)} to {max(named)} answers per run" in readme
    bench = " ".join(_paragraphs(BENCH_README.read_text(encoding="utf-8")))
    assert "37 reader answers" in bench
    assert f"in {min(named)} to {max(named)} answers per run" in bench
    assert "in the header above each excerpt and in the first line of each session's text" in bench


def test_the_named_session_id_pattern_counts_what_it_says() -> None:
    """Mutation: loosen the pattern to match any answer_ text, tighten it to eight hex characters,
    or tighten it to the bracketed header only."""

    assert NAMED_SESSION_ID.search("From session answer_f6168136 on 2023-05-28")
    assert NAMED_SESSION_ID.search("**Session answer_f6168136 | 2023/05/28**")
    assert NAMED_SESSION_ID.search("[Session answer_f6168136 | 2023/05/28 | excerpt 1]")
    assert NAMED_SESSION_ID.search("**Session answer_ultrachat_348449 | 2023/05/23 (Tue) 07:14 | excerpt 1**")
    assert NAMED_SESSION_ID.search("- **Session answer_sharegpt_SS141vi_0 | 2023/05/28 (Sun) 09:46**")
    assert not NAMED_SESSION_ID.search("the answer_ prefix")
    assert not NAMED_SESSION_ID.search("a short answer is yes")


# --- every page that states a number carries the pointer --------------------------


def test_every_markdown_page_that_states_a_hint_affected_number_carries_the_disclosure() -> None:
    """Mutation: delete a known-issue note (for example in PRODUCT_BRIEF.md), or add 81.2% to a page without answer_."""

    pages = _markdown_files_stating_a_number(REPO_ROOT)
    missing_from_walk = [name for name in MUST_BE_SCANNED if name not in pages]
    assert missing_from_walk == [], f"the scan no longer finds: {missing_from_walk}"
    assert _pages_without_the_pointer(pages) == []


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(("git", "-C", str(root), *arguments), capture_output=True, check=True)


def test_the_scan_names_a_page_with_the_number_and_no_pointer(tmp_path: Path) -> None:
    """Mutation: make the scan skip a tracked page, or let a page without answer_ pass."""

    _git(tmp_path, "init", "--quiet")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bad.md").write_text("The result is 81.2% on the benchmark.\n", encoding="utf-8")
    (tmp_path / "docs" / "ok.md").write_text("It was 79.4%. The `answer_` ids leak.\n", encoding="utf-8")
    (tmp_path / "docs" / "coverage.md").write_text("Coverage is 79.4009% overall.\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("History says 79.4%.\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "docs", "CHANGELOG.md")
    pages = _markdown_files_stating_a_number(tmp_path)
    assert sorted(pages) == ["CHANGELOG.md", "docs/bad.md", "docs/ok.md"]
    assert _pages_without_the_pointer(pages) == ["docs/bad.md"]


def test_the_scan_does_not_read_untracked_pages_in_a_working_checkout(tmp_path: Path) -> None:
    """Mutation: walk the whole directory again instead of listing the tracked files.

    A working checkout holds nested agent worktrees under ``.claude/worktrees``,
    an editor folder, an ignored ``wiki/`` and loose notes. None of it ships, and
    a page there that states 81.2% must not fail the test.
    """

    _git(tmp_path, "init", "--quiet")
    (tmp_path / "README.md").write_text("It was 81.2%. The `answer_` ids leak.\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "README.md")
    for untracked in (
        ".claude/worktrees/agent-x/NOTES.md",
        ".claude/worktrees/agent-x/docs/more.md",
        ".cursor/rules/notes.md",
        "wiki/internal.md",
        "scratch.md",
    ):
        path = tmp_path / untracked
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("81.2% with no pointer.\n", encoding="utf-8")
    pages = _markdown_files_stating_a_number(tmp_path)
    assert sorted(pages) == ["README.md"]
    assert _pages_without_the_pointer(pages) == []


def test_a_tracked_page_that_is_deleted_from_the_working_tree_is_not_read(tmp_path: Path) -> None:
    """Mutation: read every listed path without checking that the file still exists."""

    _git(tmp_path, "init", "--quiet")
    (tmp_path / "gone.md").write_text("81.2% with no pointer.\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "gone.md")
    (tmp_path / "gone.md").unlink()
    assert _markdown_files_stating_a_number(tmp_path) == {}


def test_the_changelog_names_the_label_hint() -> None:
    """Mutation: delete the CHANGELOG entry, or take `answer_` out of it.

    The entry is found by its opening words wherever it sits, so it keeps
    passing after a release moves it from Unreleased under a version heading.
    """

    entries = [
        line
        for line in (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("- The LongMemEval numbers (64.6%, 79.4% and 81.2%) now carry a published known issue.")
    ]
    assert len(entries) == 1
    assert "`answer_`" in entries[0]
    assert "unknown amount" in entries[0]


# --- published release notes ---------------------------------------------------


@pytest.mark.parametrize("path", CORRECTED_NOTES)
def test_a_published_release_note_that_states_a_number_holds_one_dated_correction(path: str) -> None:
    """Mutation: delete the 2026-10-01 correction from the note, date it another day, or add a second."""

    paragraphs = _paragraphs((REPO_ROOT / path).read_text(encoding="utf-8"))
    corrections = [p for p in paragraphs if CORRECTION_OPENING.match(p)]
    assert len(corrections) == 1, corrections
    correction = corrections[0]
    assert correction.startswith("**Correction (2026-10-01):**"), correction[:60]
    assert "`answer_`" in correction
    assert "known issue" in correction
    assert "unknown amount" in correction
    assert "Benchmark section of the README" in correction


# --- the honesty kit -----------------------------------------------------------


def test_honesty_kit_names_the_hint_with_the_right_counts_and_lists_the_replication() -> None:
    """Mutation: write 'four published' again, drop the replication row, or call the 79.4% run the headline."""

    kit = HONESTY_KIT.read_text(encoding="utf-8")
    negative = _section(kit, "4. Negative results, first class")
    bullets = [b for b in re.split(r"\n(?=- )", negative) if b.startswith("- **Session ids carried")]
    assert len(bullets) == 1
    bullet = " ".join(bullets[0].split())
    assert "three published numbers (64.6%, 79.4% and 81.2%, over five full runs)" in bullet
    assert "four published" not in kit
    assert "`answer_`" in bullet
    assert "unmeasured" in bullet

    table = _section(kit, "3. Config fingerprints of every published run")
    replication = [line for line in table.splitlines() if "`c5a7dbe1416cc43d`" in line]
    assert len(replication) == 1
    assert "81.2%" in replication[0]
    assert "replication-v0.12.0-2026-07-19/" in replication[0]
    # The 79.4% run is the earlier headline now, in the table and in the prose under it.
    assert "2026-07-07 full run (published)" not in kit
    assert "79.4% headline" not in kit
    row_0707 = [line for line in table.splitlines() if "`954b203d34e9b96d`" in line]
    assert len(row_0707) == 1
    assert "superseded" in row_0707[0]
