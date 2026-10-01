from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from longmemeval import count_probe as count_probe_module
from longmemeval.count_probe import (
    EXIT_RUN_FAILURES,
    EXIT_STRATUM_FAILURES,
    DEFAULT_AUDIT_MANIFEST,
    DEFAULT_SLICE,
    STRATUM_CADENCE_ABSTENTION,
    STRATUM_CADENCE_ANSWERABLE,
    STRATUM_CANDIDATE_COUNT,
    STRATUM_NUMERIC_SAFE_NON_EMISSION,
    STRATUM_REJECTED_GATE_WIDENING,
    default_output_path,
    expected_audit_manifest,
    exit_code_for_summary,
    probe_row,
    probe_question,
    summarize_rows,
)
from longmemeval.dataset import SYNTHETIC_FIXTURE_PATH, LongMemEvalQuestion, load_dataset
from longmemeval.session_labels import SESSION_LABEL_MODE_ANONYMISED, SESSION_LABEL_MODE_RAW


def _question(question_id: str, question: str, answer: str = "3") -> LongMemEvalQuestion:
    return LongMemEvalQuestion(
        question_id=question_id,
        question_type="multi-session",
        question=question,
        answer=answer,
        question_date="2025/01/01 (Wed) 00:00",
        haystack_session_ids=(),
        haystack_dates=(),
        haystack_sessions=(),
        answer_session_ids=("session-1",),
    )


def _candidate(count: int = 3) -> dict[str, object]:
    return {
        "count": count,
        "unit": "deduplicated_memory_candidate_groups",
        "basis": "bounded_scoped_fts_candidates",
        "query_basis": "context_pack_query",
        "matching_criteria": "searchable scoped memories matched by the reported FTS mode",
        "deduplication": "source_chunk_then_source_then_memory_id",
        "fts_source": "sqlite_fts",
        "rows_examined": count,
        "rollup_cards_excluded": 1,
        "candidate_cap": 96,
        "scope_filtered": False,
        "candidate_prefix_exhausted": True,
        "more_candidate_groups_may_exist": False,
        "is_answer": False,
        "supports_numeric_sum": False,
    }


def _count_card(count: int = 3) -> dict[str, object]:
    member_ids = [f"memory-{index}" for index in range(1, count + 1)]
    return {
        "id": "rollup-1",
        "title": f"Roll-up: bike service ({count} instances in total)",
        "canonical_text": f"bike service - {count} instances: receipts",
        "metadata_json": {
            "candidate_kind": "memory_rollup",
            "consolidation": {
                "proposal_kind": "rollup",
                "cluster_member_ids": member_ids,
                "accepted": {"reviewed": True},
                "rollup": {"topic_label": "bike service"},
            },
        },
        "value": {
            "rollup": {
                "topic_label": "bike service",
                "member_ids": member_ids,
                "member_count": count,
                "grouping_input_truncated": False,
                "grouping_input_total_exact": True,
                "grouping_input_count": count,
                "grouping_input_total": count,
                "instances_truncated": False,
            }
        },
    }


def _pack(
    *,
    candidate: dict[str, object] | None = None,
    selected_rollup: bool = False,
    unexpected_aggregation: bool = False,
) -> dict[str, object]:
    pack: dict[str, object] = {
        "relevant_memories": [],
        "trace": {"vector_stage": "disabled", "stages": {"coverage_mode": {}}},
    }
    if candidate is not None:
        pack["trace"]["stages"]["coverage_mode"]["candidate_instance_count"] = deepcopy(  # type: ignore[index]
            candidate
        )
    if selected_rollup:
        assert candidate is not None
        pack["relevant_memories"] = [_count_card(int(candidate["count"]))]
    if unexpected_aggregation:
        pack["aggregation"] = {"verified_instance_count": candidate["count"] if candidate else 0}
    return pack


def _row(question: LongMemEvalQuestion, pack: dict[str, object]) -> dict[str, object]:
    return probe_row(
        question,
        pack,
        reused_store=True,
        accept_rollups=False,
        ingest_seconds=None,
        retrieval_seconds=0.01,
    )


def test_probe_separates_trace_mechanism_from_reader_sufficiency() -> None:
    question = _question("00ca467f", "How many bike services did I record?")
    row = _row(question, _pack(candidate=_candidate()))

    assert row["hand_audited_stratum"] == STRATUM_CANDIDATE_COUNT
    assert row["trace_candidate_count_present"] is True
    assert row["trace_candidate_disclosure_is_non_oracle"] is True
    assert row["reader_aggregation_present"] is False
    assert row["mechanism_expectation_met"] is True
    assert row["answer_sufficiency"] == "trace_candidate_statistic_only_not_an_answer"


def test_probe_never_treats_selected_frequency_rollup_as_answer_sufficient() -> None:
    question = _question(
        "00ca467f",
        "How many times did I score goals?",
        # Each of the three member memories can say "twice"; member_count=3
        # therefore does not establish the six queried occurrences.
        "6",
    )
    row = _row(
        question,
        _pack(candidate=_candidate(), selected_rollup=True),
    )

    assert row["selected_count_bearing_rollups"] == [
        {"memory_id": "rollup-1", "member_count": 3}
    ]
    assert row["reader_aggregation_present"] is False
    assert row["reader_aggregation_contract_valid"] is False
    assert row["reader_verified_rollup"] is None
    assert row["reader_verified_rollup_matches_dev_gold"] is False
    assert row["answer_sufficiency"] == "selected_unverified_count_rollup_not_an_answer"


def test_probe_rejects_any_unexpected_reader_aggregation_surface() -> None:
    question = _question("00ca467f", "How many times did I service my bike?")
    row = _row(
        question,
        _pack(candidate=_candidate(), unexpected_aggregation=True),
    )

    assert row["reader_aggregation_present"] is True
    assert row["reader_aggregation_contract_valid"] is False
    assert row["answer_sufficiency"] == "unexpected_reader_aggregation_not_an_answer"


def test_probe_safe_non_emission_strata_cover_numeric_rejected_and_cadence() -> None:
    cases = (
        (
            _question("10d9b85a", "How many days did the trip last?"),
            STRATUM_NUMERIC_SAFE_NON_EMISSION,
            "numeric_value",
        ),
        (
            _question("4adc0475", "What is the total number of goals and assists I have?"),
            STRATUM_REJECTED_GATE_WIDENING,
            None,
        ),
        (
            _question("945e3d21", "How often did I service my bike?", "Three times a week"),
            STRATUM_CADENCE_ANSWERABLE,
            "cadence",
        ),
        (
            _question("2698e78f_abs", "How often did I service my bike?", "No evidence"),
            STRATUM_CADENCE_ABSTENTION,
            "cadence",
        ),
    )

    for question, stratum, sub_intent in cases:
        row = _row(question, _pack())
        assert row["hand_audited_stratum"] == stratum
        assert row["detector_sub_intent"] == sub_intent
        assert row["trace_candidate_count_present"] is False
        assert row["reader_aggregation_present"] is False
        assert row["mechanism_expectation_met"] is True
        assert row["safety_expectation_met"] is True
    abstention = _row(cases[-1][0], _pack())
    assert abstention["safe_abstention_non_answer"] is True


def test_probe_summary_and_exit_fail_when_answer_sufficiency_is_zero() -> None:
    question = _question("00ca467f", "How many bike services did I record?")
    trace_only = _row(question, _pack(candidate=_candidate()))
    no_go = summarize_rows([trace_only])

    assert no_go["overall"]["mechanism_expectations_failed"] == 0  # type: ignore[index]
    assert no_go["overall"]["answer_sufficiency_checks"] == 1  # type: ignore[index]
    assert no_go["overall"]["answer_sufficient_via_verified_rollup"] == 0  # type: ignore[index]
    assert exit_code_for_summary(no_go, has_errors=False) == EXIT_STRATUM_FAILURES

    selected_card_row = _row(
        _question("00ca467f", "How many times did I service my bike?"),
        _pack(candidate=_candidate(), selected_rollup=True),
    )
    selected_card = summarize_rows([selected_card_row])
    # A partial custom probe cannot green the release gate by omitting every
    # safety stratum.
    assert exit_code_for_summary(selected_card, has_errors=False) == EXIT_STRATUM_FAILURES
    numeric = _row(
        _question("10d9b85a", "How many days did the trip last?"),
        _pack(),
    )
    complete_custom = summarize_rows([selected_card_row, numeric])
    # A selected count-bearing card remains measurement only because members
    # have no reviewed one-unit-per-member invariant.
    assert exit_code_for_summary(complete_custom, has_errors=False) == EXIT_STRATUM_FAILURES
    partial = summarize_rows(
        [
            selected_card_row,
            trace_only,
            numeric,
        ]
    )
    assert partial["overall"]["answer_sufficient_via_verified_rollup"] == 0  # type: ignore[index]
    assert partial["overall"]["answer_sufficiency_checks"] == 2  # type: ignore[index]
    assert exit_code_for_summary(partial, has_errors=False) == EXIT_STRATUM_FAILURES
    assert exit_code_for_summary(complete_custom, has_errors=True) == EXIT_RUN_FAILURES


def test_probe_exit_rejects_empty_detector_only_and_incomplete_default_manifest() -> None:
    assert exit_code_for_summary(summarize_rows([]), has_errors=False) == EXIT_STRATUM_FAILURES
    detector_only = _row(
        _question("unreviewed", "How many bikes did I service?"),
        _pack(candidate=_candidate()),
    )
    assert (
        exit_code_for_summary(summarize_rows([detector_only]), has_errors=False)
        == EXIT_STRATUM_FAILURES
    )
    selected_card = _row(
        _question("00ca467f", "How many times did I service my bike?"),
        _pack(candidate=_candidate(), selected_rollup=True),
    )
    numeric = _row(
        _question("10d9b85a", "How many days did the trip last?"),
        _pack(),
    )
    incomplete = summarize_rows([selected_card, numeric])
    assert (
        exit_code_for_summary(
            incomplete,
            has_errors=False,
            expected_manifest=DEFAULT_AUDIT_MANIFEST,
        )
        == EXIT_STRATUM_FAILURES
    )
    # ``--limit`` changes row selection, not the default-slice truth target:
    # main() keeps this exact manifest and therefore rejects the partial run.
    assert expected_audit_manifest(Path(DEFAULT_SLICE)) == DEFAULT_AUDIT_MANIFEST


def test_default_outputs_separate_rollup_modes() -> None:
    dataset = Path("longmemeval_s_cleaned.json")

    off = default_output_path(dataset, accept_rollups=False)
    on = default_output_path(dataset, accept_rollups=True)

    assert off.name == "count_probe_longmemeval_s_cleaned_rollups_off.jsonl"
    assert on.name == "count_probe_longmemeval_s_cleaned_rollups_on.jsonl"
    assert off != on


def test_probe_request_uses_the_question_date_as_reference_time(
    monkeypatch,
    tmp_path: Path,
) -> None:
    question = _question("00ca467f", "How many bike services did I record?")
    captured: dict[str, object] = {}

    class FakeRun:
        store = object()

        def ingest(self, *, accept_rollups: bool) -> None:
            captured["accept_rollups"] = accept_rollups

    @contextmanager
    def fake_question_run(_question_value, _db_path, **_choices):
        yield FakeRun()

    class FakeService:
        def __init__(self, _store: object) -> None:
            pass

        def compile_context_pack(self, request):
            captured["request"] = request
            return {"relevant_memories": [], "trace": {"stages": {}}}

    monkeypatch.setattr(count_probe_module, "question_run", fake_question_run)
    monkeypatch.setattr(count_probe_module, "VNextRetrievalService", FakeService)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text("[]\n", encoding="utf-8")

    probe_question(
        question,
        work_dir=tmp_path,
        dataset_path=dataset_path,
        max_items=16,
        accept_rollups=False,
    )

    request = captured["request"]
    assert request.reference_time is not None
    assert request.reference_time.isoformat() == "2025-01-01T00:00:00+00:00"


# -- session label mode ---------------------------------------------------------------------------------


def _scrub_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_RERANKER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _store_mentions(db_path: Path, needle: str) -> bool:
    connection = sqlite3.connect(db_path)
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for name in names:
            for row in connection.execute(f'SELECT * FROM "{name}"'):
                for value in row:
                    text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                    if needle in text:
                        return True
    finally:
        connection.close()
    return False


def test_probe_hides_session_ids_by_default_records_the_mode_and_keeps_stores_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the probe changes label mode without saying so, or reuses a store across modes.

    Mutations: (1) drop ``session_label_mode=session_label_mode`` from the
    ``question_run`` call in ``probe_question`` (a raw run would store hashed
    labels); (2) drop it from ``_marker_matches`` or from the marker write (a raw
    run would reuse the hashed store, or the reverse); (3) drop the row field in
    ``probe_row``. Before this the probe took the new default with no flag and no
    record.
    """
    _scrub_models(monkeypatch)
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    raw_id = question.haystack_session_ids[1]
    assert raw_id.startswith("answer_")
    options = {"work_dir": tmp_path, "dataset_path": SYNTHETIC_FIXTURE_PATH, "max_items": 8, "accept_rollups": False}
    db_path = count_probe_module._db_path_for(tmp_path, question.question_id)

    first = probe_question(question, **options)
    assert first["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED and first["reused_store"] is False
    assert not _store_mentions(db_path, raw_id), "the default probe stored a raw session id"
    assert probe_question(question, **options)["reused_store"] is True

    raw = probe_question(question, session_label_mode=SESSION_LABEL_MODE_RAW, **options)
    assert raw["session_label_mode"] == SESSION_LABEL_MODE_RAW
    assert raw["reused_store"] is False, "a store ingested with hashed labels was reused for a raw-label probe"
    assert _store_mentions(db_path, raw_id), "the raw probe did not store the raw session id"
    assert probe_question(question, session_label_mode=SESSION_LABEL_MODE_RAW, **options)["reused_store"] is True
    back = probe_question(question, **options)
    assert back["reused_store"] is False
    assert not _store_mentions(db_path, raw_id)


def test_probe_flag_reaches_the_rows_the_summary_and_the_default_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if ``--raw-session-labels`` is parsed but not applied, or its rows can overwrite the others.

    Mutations: (1) drop ``session_label_mode=session_label_mode`` from the
    ``pool.submit`` call in ``main``; (2) drop it from the summary payload;
    (3) ignore the mode in ``default_output_path``.
    """
    _scrub_models(monkeypatch)
    source = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    dataset = tmp_path / "dataset.json"
    record = json.loads(SYNTHETIC_FIXTURE_PATH.read_text(encoding="utf-8"))[0]
    record["question_id"] = "count_synth_1"
    record["question"] = "How many bike services did I record?"
    dataset.write_text(json.dumps([record]), encoding="utf-8")
    ids = tmp_path / "ids.txt"
    ids.write_text("count_synth_1\n", encoding="utf-8")
    raw_id = source.haystack_session_ids[1]

    def run(*extra: str) -> tuple[list[dict[str, object]], dict[str, object]]:
        out = tmp_path / f"rows{len(extra)}.jsonl"
        count_probe_module.main(
            [
                "--dataset-file", str(dataset),
                "--question-ids", str(ids),
                "--work-dir", str(tmp_path / "stores"),
                "--out", str(out),
                "--workers", "1",
                *extra,
            ]
        )
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        summary = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
        return rows, summary

    rows, summary = run()
    assert [row["session_label_mode"] for row in rows] == [SESSION_LABEL_MODE_ANONYMISED]
    assert summary["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    assert not _store_mentions(count_probe_module._db_path_for(tmp_path / "stores", "count_synth_1"), raw_id)

    raw_rows, raw_summary = run("--raw-session-labels")
    assert [row["session_label_mode"] for row in raw_rows] == [SESSION_LABEL_MODE_RAW]
    assert raw_summary["session_label_mode"] == SESSION_LABEL_MODE_RAW
    assert raw_rows[0]["reused_store"] is False
    assert _store_mentions(count_probe_module._db_path_for(tmp_path / "stores", "count_synth_1"), raw_id)

    dataset_name = Path("longmemeval_s_cleaned.json")
    default_name = default_output_path(dataset_name, accept_rollups=False)
    raw_name = default_output_path(dataset_name, accept_rollups=False, session_label_mode=SESSION_LABEL_MODE_RAW)
    assert default_name.name == "count_probe_longmemeval_s_cleaned_rollups_off.jsonl"
    assert raw_name != default_name and "raw_labels" in raw_name.name
