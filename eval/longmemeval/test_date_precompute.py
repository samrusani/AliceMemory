"""Tests for machine-readable dates + the derived-values section (harness side).

Companion to ``test_harness.py`` (same model-free, network-free rules; kept
in its own file so parallel workstreams do not contend on one test module).
Covers the ISO-8601 fact-line date prefix, rendering of the pack's
``derived_values`` block into the context block (bounded, uncharged,
byte-identical when absent), and the end-to-end path through real SQLite
ingest + retrieval including reference_time-absent dormancy.

Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_date_precompute.py -q
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from longmemeval import adapter  # noqa: E402
from longmemeval.dataset import SYNTHETIC_FIXTURE_PATH, load_dataset, parse_question  # noqa: E402
from longmemeval.session_labels import SESSION_LABEL_MODE_RAW  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_model_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        adapter.CONTEXT_CHAR_BUDGET_ENV,
        adapter.MAX_ITEMS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


# -- ISO-8601 fact-line date prefix -------------------------------------------------


def test_iso_fact_date_normalizes_and_keeps_weekday_and_time() -> None:
    assert adapter._iso_fact_date("2023/05/30 (Tue) 02:01") == "2023-05-30 (Tue) 02:01"
    # Zero-padding makes the prefix genuinely ISO-8601.
    assert adapter._iso_fact_date("2023/5/3 (Wed) 09:07") == "2023-05-03 (Wed) 09:07"
    # Already-ISO prefixes are idempotent.
    assert adapter._iso_fact_date("2023-05-30 (Tue) 02:01") == "2023-05-30 (Tue) 02:01"


def test_iso_fact_date_passes_through_undated_or_invalid() -> None:
    assert adapter._iso_fact_date("undated") == "undated"
    assert adapter._iso_fact_date("") == ""
    # Not a real calendar date: never invent one.
    assert adapter._iso_fact_date("2023/15/40 (Xxx) 09:99") == "2023/15/40 (Xxx) 09:99"


# -- context-block rendering ---------------------------------------------------------

_SESSIONS = {
    "src-a": ("session_a", "2023/05/20 (Sat) 14:10"),
    "src-b": ("session_b", "2023/05/01 (Mon) 09:00"),
}


class _StubChunkStore:
    """Just enough store surface for ``_render_context_block``."""

    def __init__(self, chunks_by_source: dict[str, list[str]]) -> None:
        self._chunks_by_source = chunks_by_source

    def list_source_chunks(self, source_id: str) -> list[dict[str, object]]:
        return [
            {"text": text, "chunk_index": index}
            for index, text in enumerate(self._chunks_by_source.get(source_id, []))
        ]

    def get_source(self, source_id: str) -> dict[str, object] | None:
        if source_id not in _SESSIONS:
            return None
        session_id, session_date = _SESSIONS[source_id]
        return {"metadata_json": {"session_id": session_id, "session_date": session_date}}


def _run() -> adapter.QuestionRun:
    question = parse_question(
        {
            "question_id": "q_derived",
            "question_type": "temporal-reasoning",
            "question": "How many days ago did the user adopt the golden retriever puppy?",
            "answer": "26 days",
            "question_date": "2023/06/15 (Thu) 10:00",
            "haystack_dates": ["2023/05/20 (Sat) 14:10"],
            "haystack_session_ids": ["session_a"],
            "haystack_sessions": [[{"role": "user", "content": "hello"}]],
            "answer_session_ids": ["session_a"],
        }
    )
    store = _StubChunkStore(
        {
            "src-a": ["the user adopted the golden retriever puppy from the shelter downtown"],
            "src-b": ["the golden retriever puppy chewed a slipper this morning after breakfast"],
        }
    )
    # Stub stores hold the literal session ids written in this module, so the packing
    # logic is tested in raw mode; labelling is tested in test_session_label_leak_guard.py.
    return adapter.QuestionRun(question, store, session_label_mode=SESSION_LABEL_MODE_RAW)  # type: ignore[arg-type]


_DERIVED_LINES = [
    "[derived] reference date: 2023-06-15 (Thu)",
    "[derived] dated items span 19 days (2 weeks 5 days); 2023-05-01 -> 2023-05-20",
    "[derived] 2023-05-01 (Mon): 45 days (6 weeks 3 days) earlier; 2023-05-01 -> 2023-06-15; day 1 of 2",
    "[derived] 2023-05-20 (Sat): 26 days (3 weeks 5 days) earlier; 2023-05-20 -> 2023-06-15; day 2 of 2",
]


def _pack(**extra: object) -> dict[str, object]:
    return {
        "relevant_memories": [
            {
                "canonical_text": "The user adopted a golden retriever puppy.",
                "metadata_json": {"source_id": "src-a"},
            }
        ],
        "sources": [{"id": "src-a"}, {"id": "src-b"}],
        **extra,
    }


def test_fact_lines_render_iso_dates_while_excerpt_headers_keep_raw_form() -> None:
    block, _count = _run()._render_context_block(_pack(), budget=4_000)
    assert "- [2023-05-20 (Sat) 14:10] The user adopted a golden retriever puppy." in block
    # Excerpt headers are NOT owned by this change: raw connector form stays.
    assert "[Session session_a | 2023/05/20 (Sat) 14:10 | excerpt 1]" in block


def test_derived_section_renders_after_excerpts_and_is_uncharged() -> None:
    run = _run()
    budget = 4_000
    baseline, baseline_count = run._render_context_block(_pack(), budget=budget)
    block, count = run._render_context_block(
        _pack(derived_values={"reference_time": "2023-06-15T10:00:00+00:00", "lines": _DERIVED_LINES}),
        budget=budget,
    )
    # Uncharged and additive: the pre-derived rendering is byte-identical,
    # the section is appended after the excerpts it summarizes.
    assert count == baseline_count
    assert block == baseline + "\n\n\n" + adapter.DERIVED_SECTION_HEADER + "\n" + "\n".join(_DERIVED_LINES)
    assert block.index("### Retrieved chat history excerpts:") < block.index(adapter.DERIVED_SECTION_HEADER)


def test_derived_section_precedes_grounding_notes() -> None:
    run = _run()
    block, _count = run._render_context_block(
        _pack(
            derived_values={"reference_time": "2023-06-15T10:00:00+00:00", "lines": _DERIVED_LINES},
            grounding={"unsupported_entities": ["Marcus Chen"], "checked": 2},
        ),
        budget=4_000,
    )
    assert block.index(adapter.DERIVED_SECTION_HEADER) < block.index('Note: no stored memories mention "Marcus Chen".')
    assert block.rstrip().endswith('Note: no stored memories mention "Marcus Chen".')


def test_derived_section_is_hard_capped_to_whole_lines() -> None:
    run = _run()
    long_lines = [f"[derived] filler line {index:03d} " + "x" * 150 for index in range(64)]
    block, _count = run._render_context_block(
        _pack(derived_values={"reference_time": "2023-06-15T10:00:00+00:00", "lines": long_lines}),
        budget=4_000,
    )
    section = block.split(adapter.DERIVED_SECTION_HEADER, 1)[1]
    rendered = [line for line in section.splitlines() if line.startswith("[derived]")]
    assert 0 < len(rendered) < len(long_lines)
    # Whole lines only, in order, within the cap.
    assert rendered == long_lines[: len(rendered)]
    assert sum(len(line) + 1 for line in rendered) <= adapter.DERIVED_SECTION_MAX_CHARS


def test_absent_or_malformed_derived_values_render_byte_identically() -> None:
    run = _run()
    baseline, _count = run._render_context_block(_pack(), budget=4_000)
    for malformed in ("not-a-dict", {"lines": "not-a-list"}, {"lines": []}, {}):
        block, _count = run._render_context_block(_pack(derived_values=malformed), budget=4_000)
        assert block == baseline
    assert adapter.DERIVED_SECTION_HEADER not in baseline


# -- end to end through real SQLite ingest + retrieval -------------------------------


def test_question_run_renders_derived_date_arithmetic(tmp_path: Path) -> None:
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3") as run:
        run.ingest()
        outcome = run.retrieve(max_items=8, context_char_budget=12_000)
    block = outcome.context_block
    assert adapter.DERIVED_SECTION_HEADER in block
    # question_date 2023/06/01 (Thu) 10:00 is the reference the harness passes.
    assert "[derived] reference date: 2023-06-01 (Thu)" in block
    # The haystack sessions' dates are precomputed against it (memory dates
    # resolve through the provenance source's session_date — the harness
    # promotes memories without stamping them).
    assert re.search(r"\[derived\] 2023-05-20 \(Sat\): 12 days \(1 week 5 days\) earlier; "
                     r"2023-05-20 -> 2023-06-01; day \d of \d", block)
    # Fact lines lead with the ISO form of their session date.
    assert re.search(r"^- \[2023-\d{2}-\d{2} \([A-Z][a-z]{2}\) \d{2}:\d{2}\] ", block, re.MULTILINE)
    # Trace stage disclosed the precompute honestly.
    assert outcome.context_chars == len(block)


def test_question_run_derived_block_dormant_without_parseable_question_date(tmp_path: Path) -> None:
    # An unparseable question_date means no reference_time: the derived
    # block must stay dormant end-to-end (never fall back to wall clock).
    raw = {
        "question_id": "q_undated",
        "question_type": "single-session-user",
        "question": "What breed is the user's dog?",
        "answer": "Golden retriever",
        "question_date": "someday soon",
        "haystack_dates": ["2023/05/20 (Sat) 14:10"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [
            [{"role": "user", "content": "My dog is a golden retriever named Biscuit."}]
        ],
        "answer_session_ids": ["s1"],
    }
    question = parse_question(raw)
    with adapter.question_run(question, tmp_path / "q.sqlite3") as run:
        run.ingest()
        outcome = run.retrieve(max_items=4, context_char_budget=8_000)
    assert adapter.DERIVED_SECTION_HEADER not in outcome.context_block
    assert "[derived]" not in outcome.context_block


def test_question_run_derived_block_is_byte_stable(tmp_path: Path) -> None:
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3") as run:
        run.ingest()
        first = run.retrieve(max_items=8, context_char_budget=12_000)
        second = run.retrieve(max_items=8, context_char_budget=12_000)
    assert first.context_block == second.context_block
    assert first.context_sha256 == second.context_sha256
