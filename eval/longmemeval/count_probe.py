"""Deterministic LongMemEval count-intent context-pack probe.

This is the keyless Sprint 4 companion to ``coverage_probe.py``. It reuses
the same per-question SQLite stores and compiles the same model-free context
pack, then checks the diagnostic and selected-card surfaces separately:

* a disclosed, bounded FTS candidate-instance statistic in the coverage
  trace; and
* whether an accepted count-bearing roll-up card was selected for measurement.

The probe never treats either signal as an oracle. A memory candidate or
roll-up member is not reviewed as exactly one queried unit; a single memory can
say "twice" or carry several items. After pack compilation the probe compares
diagnostic candidate values with dev-slice gold strictly as measurement. It
does not call a selected card answer-sufficient, and the carrier emits no
reader-facing aggregate field.
Numeric-value questions (hours/days/pages) and cadence questions ("how
often") form explicit safe-non-emission strata: coverage mode may run, but a
memory-row total must stay absent. The cadence abstention fixtures likewise
require recognition plus safe non-emission, not a fabricated answer.

Session ids. Like the runner, the probe stores keyed-hash session labels in
place of the dataset's ids unless ``--raw-session-labels`` is set, and each row
and the summary record the mode (see :mod:`longmemeval.session_labels`), so a
row says what its store held. A kept store is reused only within one mode.

Embeddings and the optional reranker are scrubbed by default, so no model or
paid API call can occur. Run from the repository root:

    .venv/bin/python eval/longmemeval/count_probe.py \
      --dataset-file eval/longmemeval/data/longmemeval_s_cleaned.json \
      --work-dir <scratch dir>/count-probe-work \
      --max-items 16 --workers 8
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import time
from typing import Mapping

_EVAL_DIR = Path(__file__).resolve().parent.parent
if str(_EVAL_DIR) not in sys.path:  # direct execution
    sys.path.insert(0, str(_EVAL_DIR))

from alicebot_api.vnext_coverage_query import (
    AGGREGATION_KIND_COUNT,
    COUNT_SUB_INTENT_CADENCE,
    COUNT_SUB_INTENT_NUMERIC_VALUE,
    count_bearing_rollup_member_count,
    detect_aggregation_intent,
    supports_candidate_instance_count,
)
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_temporal_query import parse_event_datetime

from longmemeval.adapter import max_items_from_env, question_run
from longmemeval.coverage_probe import disable_embeddings_env, disable_reranker_env
from longmemeval.dataset import (
    RESULTS_DIR,
    LongMemEvalDatasetError,
    LongMemEvalQuestion,
    load_dataset,
    resolve_dataset_path,
)
from longmemeval.session_labels import DEFAULT_SESSION_LABEL_MODE, SESSION_LABEL_MODE_RAW
from longmemeval.runner import (
    _FILENAME_SAFE,
    _build_ingest_marker_payload,
    _cleanup_store,
    _sha256_prefix,
)
from alicebot_api.vnext_source_fence import SourceReadFence


COUNT_PROBE_SCHEMA = "longmemeval_count_probe_v2"
DEFAULT_SLICE = Path(__file__).resolve().parent / "slices" / "stage1-150.txt"
DEFAULT_WORK_DIR = Path(__file__).resolve().parent / "work" / "coverage"

EXIT_OK = 0
EXIT_RUN_FAILURES = 1
EXIT_CONFIG_ERROR = 2
EXIT_STRATUM_FAILURES = 3

STRATUM_CANDIDATE_COUNT = "candidate_count_eligible"
STRATUM_NUMERIC_SAFE_NON_EMISSION = "numeric_value_safe_non_emission"
STRATUM_CADENCE_ANSWERABLE = "cadence_answerable_safe_non_emission"
STRATUM_CADENCE_ABSTENTION = "cadence_abstention_safe_non_emission"
STRATUM_REJECTED_GATE_WIDENING = "rejected_gate_widening_safe_non_emission"
STRATUM_DETECTOR_ONLY = "detector_only_unreviewed"
DEFAULT_AUDIT_MANIFEST = {
    "audited": 23,
    "safety_checks": 9,
    "answer_sufficiency_checks": 14,
}

# Hand-audited development strata from the fixed stage-1 slice. These are
# product-mechanism checks, not benchmark-label access during retrieval.
_CARDINALITY_FREQUENCY_IDS = frozenset(
    {
        "00ca467f",
        "0a995998",
        "1a8a66a6",
        "2788b940",
        "2e6d26dc",
        "4f54b7c9",
        "60159905",
        "681a1674",
        "88432d0a",
        "9d25d4e0",
        "a9f6b44c",
        "bf659f65",
        "c2ac3c61",
        "d682f1a2",
    }
)
_NUMERIC_SAFE_IDS = frozenset({"10d9b85a", "5a7937c8", "a08a253f"})
_GATE_RECALL_ANSWERABLE_IDS = frozenset({"945e3d21"})
_GATE_RECALL_ABSTENTION_IDS = frozenset({"2698e78f_abs", "f685340e_abs"})
_REJECTED_GATE_WIDENING_IDS = frozenset({"4adc0475", "8979f9ec", "8e91e7d9"})


def hand_audited_stratum(question_id: str) -> str:
    if question_id in _CARDINALITY_FREQUENCY_IDS:
        return STRATUM_CANDIDATE_COUNT
    if question_id in _NUMERIC_SAFE_IDS:
        return STRATUM_NUMERIC_SAFE_NON_EMISSION
    if question_id in _GATE_RECALL_ANSWERABLE_IDS:
        return STRATUM_CADENCE_ANSWERABLE
    if question_id in _GATE_RECALL_ABSTENTION_IDS:
        return STRATUM_CADENCE_ABSTENTION
    if question_id in _REJECTED_GATE_WIDENING_IDS:
        return STRATUM_REJECTED_GATE_WIDENING
    return STRATUM_DETECTOR_ONLY


def _db_path_for(work_dir: Path, question_id: str) -> Path:
    return work_dir / f"{_FILENAME_SAFE.sub('_', question_id)}.sqlite3"


def _marker_path_for(db_path: Path) -> Path:
    return Path(str(db_path) + ".ingested.json")


def _marker_matches(
    marker_path: Path,
    question: LongMemEvalQuestion,
    dataset_path: Path,
    *,
    accept_rollups: bool,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
) -> bool:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(marker, dict) and marker == _build_ingest_marker_payload(
        question,
        dataset_path=dataset_path,
        accept_rollups=accept_rollups,
        session_label_mode=session_label_mode,
    )


def _trace_candidate_record(pack: Mapping[str, object]) -> dict[str, object] | None:
    trace = pack.get("trace")
    if not isinstance(trace, Mapping):
        return None
    stages = trace.get("stages")
    if not isinstance(stages, Mapping):
        return None
    coverage = stages.get("coverage_mode")
    if not isinstance(coverage, Mapping):
        return None
    candidate = coverage.get("candidate_instance_count")
    if not isinstance(candidate, Mapping):
        return None
    return {str(key): value for key, value in candidate.items()}


def _reader_aggregation_record(pack: Mapping[str, object]) -> dict[str, object] | None:
    aggregation = pack.get("aggregation")
    if not isinstance(aggregation, Mapping):
        return None
    return {str(key): value for key, value in aggregation.items()}


def _selected_count_rollups(pack: Mapping[str, object]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    memories = pack.get("relevant_memories")
    if not isinstance(memories, list):
        return selected
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        count = count_bearing_rollup_member_count(memory)
        if count is None:
            continue
        selected.append({"memory_id": str(memory.get("id") or ""), "member_count": count})
    return selected


def _candidate_disclosure_is_non_oracle(candidate: Mapping[str, object] | None) -> bool:
    return bool(
        candidate is not None
        and candidate.get("is_answer") is False
        and candidate.get("supports_numeric_sum") is False
        and isinstance(candidate.get("matching_criteria"), str)
        and str(candidate.get("matching_criteria")).strip()
    )


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _simple_gold_count(answer: str) -> int | None:
    """Parse only an unambiguous leading scalar for audit comparison."""
    numeric = re.match(r"\s*([0-9]+)\b", answer)
    if numeric is not None:
        return int(numeric.group(1))
    word = re.match(r"\s*([a-z]+)\b", answer.casefold())
    return _NUMBER_WORDS.get(word.group(1)) if word is not None else None


def probe_row(
    question: LongMemEvalQuestion,
    pack: Mapping[str, object],
    *,
    reused_store: bool,
    accept_rollups: bool,
    ingest_seconds: float | None,
    retrieval_seconds: float,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
) -> dict[str, object]:
    """Build one auditable row without treating benchmark gold as retrieval input."""
    intent = detect_aggregation_intent(question.question)
    trace_candidate = _trace_candidate_record(pack)
    reader_aggregation = _reader_aggregation_record(pack)
    count_rollups = _selected_count_rollups(pack)
    stratum = hand_audited_stratum(question.question_id)
    trace_candidate_present = trace_candidate is not None
    reader_aggregation_present = reader_aggregation is not None
    non_oracle = _candidate_disclosure_is_non_oracle(trace_candidate)
    audited_answerable = stratum == STRATUM_CANDIDATE_COUNT
    gold_count = _simple_gold_count(question.answer) if audited_answerable else None
    trace_candidate_value = trace_candidate.get("count") if trace_candidate is not None else None
    trace_candidate_matches_gold = (
        trace_candidate_value == gold_count
        if isinstance(trace_candidate_value, int)
        and not isinstance(trace_candidate_value, bool)
        and gold_count is not None
        else None
    )
    reader_verified_rollup = None
    reader_verified_rollup_matches_gold = False
    safe_non_emission = not trace_candidate_present and not reader_aggregation_present
    safe_abstention_non_answer = bool(
        stratum == STRATUM_CADENCE_ABSTENTION and safe_non_emission
    )

    if stratum == STRATUM_NUMERIC_SAFE_NON_EMISSION:
        mechanism_expectation_met = bool(
            intent is not None
            and intent.sub_intent == COUNT_SUB_INTENT_NUMERIC_VALUE
        )
        safety_expectation_met = safe_non_emission
    elif stratum == STRATUM_REJECTED_GATE_WIDENING:
        mechanism_expectation_met = intent is None
        safety_expectation_met = safe_non_emission
    elif stratum in {STRATUM_CADENCE_ANSWERABLE, STRATUM_CADENCE_ABSTENTION}:
        mechanism_expectation_met = bool(
            intent is not None and intent.sub_intent == COUNT_SUB_INTENT_CADENCE
        )
        safety_expectation_met = safe_non_emission
    elif audited_answerable:
        mechanism_expectation_met = bool(
            supports_candidate_instance_count(intent)
            and trace_candidate_present
            and non_oracle
        )
        safety_expectation_met = None
    else:
        # Detector-only rows are measurement, not silently promoted into the
        # hand-audited release gate.
        mechanism_expectation_met = None
        safety_expectation_met = None

    if not audited_answerable:
        answer_sufficiency = "not_applicable"
    elif reader_aggregation_present:
        answer_sufficiency = "unexpected_reader_aggregation_not_an_answer"
    elif count_rollups:
        answer_sufficiency = "selected_unverified_count_rollup_not_an_answer"
    elif trace_candidate_present:
        answer_sufficiency = "trace_candidate_statistic_only_not_an_answer"
    else:
        answer_sufficiency = "no_answer_sufficient_aggregate"

    trace = pack.get("trace")
    trace_mapping = trace if isinstance(trace, Mapping) else {}
    memories = pack.get("relevant_memories")
    return {
        "schema": COUNT_PROBE_SCHEMA,
        "question_id": question.question_id,
        "question_type": question.question_type,
        "is_abstention": question.is_abstention,
        "question": question.question,
        "gold_answer_for_measurement_only": question.answer,
        "parsed_gold_count_for_measurement_only": gold_count,
        "answer_session_count_for_measurement_only": len(question.answer_session_ids),
        "hand_audited_stratum": stratum,
        "detector_kind": intent.kind if intent is not None else None,
        "detector_trigger": intent.trigger if intent is not None else None,
        "detector_sub_intent": intent.sub_intent if intent is not None else None,
        "trace_candidate_count_present": trace_candidate_present,
        "trace_candidate_count": trace_candidate,
        "trace_candidate_disclosure_is_non_oracle": non_oracle,
        "trace_candidate_count_matches_dev_gold": trace_candidate_matches_gold,
        "reader_aggregation_present": reader_aggregation_present,
        "reader_aggregation": reader_aggregation,
        "reader_aggregation_contract_valid": reader_verified_rollup is not None,
        "selected_count_bearing_rollups": count_rollups,
        "reader_verified_rollup": reader_verified_rollup,
        "reader_verified_rollup_matches_dev_gold": reader_verified_rollup_matches_gold,
        "safe_non_emission": safe_non_emission,
        "safe_abstention_non_answer": safe_abstention_non_answer,
        "mechanism_expectation_met": mechanism_expectation_met,
        "safety_expectation_met": safety_expectation_met,
        "answer_sufficiency": answer_sufficiency,
        "selected_memory_count": len(memories) if isinstance(memories, list) else 0,
        "vector_stage": str(trace_mapping.get("vector_stage", "unknown")),
        "reused_store": reused_store,
        "accept_rollups": accept_rollups,
        "session_label_mode": session_label_mode,
        "ingest_seconds": round(ingest_seconds, 3) if ingest_seconds is not None else None,
        "retrieval_seconds": round(retrieval_seconds, 3),
    }


def probe_question(
    question: LongMemEvalQuestion,
    *,
    work_dir: Path,
    dataset_path: Path,
    max_items: int,
    accept_rollups: bool,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
) -> dict[str, object]:
    db_path = _db_path_for(work_dir, question.question_id)
    marker_path = _marker_path_for(db_path)
    reuse = db_path.is_file() and _marker_matches(
        marker_path,
        question,
        dataset_path,
        accept_rollups=accept_rollups,
        session_label_mode=session_label_mode,
    )
    if not reuse:
        marker_path.unlink(missing_ok=True)
        _cleanup_store(db_path)
    started = time.monotonic()
    ingest_seconds: float | None = None
    with question_run(question, db_path, session_label_mode=session_label_mode) as run:
        if not reuse:
            run.ingest(accept_rollups=accept_rollups)
            ingest_seconds = time.monotonic() - started
        request = VNextRetrievalRequest(
            query=question.question,
            max_items=max_items,
            include_sources=True,
            actor_type="system",
            reference_time=parse_event_datetime(question.question_date),
        )
        retrieval_started = time.monotonic()
        pack = VNextRetrievalService(run.store).compile_context_pack(request, source_fence=SourceReadFence.unfenced())
        retrieval_seconds = time.monotonic() - retrieval_started
    if not reuse:
        marker_path.write_text(
            json.dumps(
                _build_ingest_marker_payload(
                    question,
                    dataset_path=dataset_path,
                    accept_rollups=accept_rollups,
                    session_label_mode=session_label_mode,
                ),
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return probe_row(
        question,
        pack,
        reused_store=reuse,
        accept_rollups=accept_rollups,
        ingest_seconds=ingest_seconds,
        retrieval_seconds=retrieval_seconds,
        session_label_mode=session_label_mode,
    )


def summarize_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    def summarize_bucket(bucket_rows: list[dict[str, object]]) -> dict[str, object]:
        audited = [
            row for row in bucket_rows if row.get("mechanism_expectation_met") is not None
        ]
        safety_checked = [
            row for row in bucket_rows if row.get("safety_expectation_met") is not None
        ]
        candidate_gold_comparisons = [
            row
            for row in bucket_rows
            if row.get("trace_candidate_count_matches_dev_gold") is not None
        ]
        answer_sufficiency_checks = [
            row for row in bucket_rows if row.get("answer_sufficiency") != "not_applicable"
        ]
        return {
            "questions": len(bucket_rows),
            "audited": len(audited),
            "detected": sum(1 for row in bucket_rows if row.get("detector_kind") == AGGREGATION_KIND_COUNT),
            "with_trace_candidate_count": sum(
                1 for row in bucket_rows if row.get("trace_candidate_count_present") is True
            ),
            "with_reader_aggregation": sum(
                1 for row in bucket_rows if row.get("reader_aggregation_present") is True
            ),
            "with_valid_reader_aggregation": sum(
                1
                for row in bucket_rows
                if row.get("reader_aggregation_contract_valid") is True
            ),
            "with_count_bearing_accepted_rollup": sum(
                1 for row in bucket_rows if row.get("selected_count_bearing_rollups")
            ),
            "numeric_safe_non_emissions": sum(
                1
                for row in bucket_rows
                if row.get("hand_audited_stratum") == STRATUM_NUMERIC_SAFE_NON_EMISSION
                and row.get("safety_expectation_met") is True
            ),
            "rejected_gate_widening_safe_non_emissions": sum(
                1
                for row in bucket_rows
                if row.get("hand_audited_stratum") == STRATUM_REJECTED_GATE_WIDENING
                and row.get("safety_expectation_met") is True
            ),
            "safe_abstention_non_answers": sum(
                1 for row in bucket_rows if row.get("safe_abstention_non_answer") is True
            ),
            "cadence_safe_non_emissions": sum(
                1
                for row in bucket_rows
                if row.get("hand_audited_stratum")
                in {STRATUM_CADENCE_ANSWERABLE, STRATUM_CADENCE_ABSTENTION}
                and row.get("safety_expectation_met") is True
            ),
            "mechanism_expectations_met": sum(
                1 for row in audited if row.get("mechanism_expectation_met") is True
            ),
            "mechanism_expectations_failed": sum(
                1 for row in audited if row.get("mechanism_expectation_met") is False
            ),
            "safety_checks": len(safety_checked),
            "safety_expectations_met": sum(
                1 for row in safety_checked if row.get("safety_expectation_met") is True
            ),
            "safety_expectations_failed": sum(
                1 for row in safety_checked if row.get("safety_expectation_met") is False
            ),
            "trace_candidate_counts_compared_to_dev_gold": len(candidate_gold_comparisons),
            "trace_candidate_counts_matching_dev_gold": sum(
                1
                for row in candidate_gold_comparisons
                if row.get("trace_candidate_count_matches_dev_gold") is True
            ),
            "trace_candidate_counts_mismatching_dev_gold": sum(
                1
                for row in candidate_gold_comparisons
                if row.get("trace_candidate_count_matches_dev_gold") is False
            ),
            "answer_sufficiency_checks": len(answer_sufficiency_checks),
            "answer_sufficient_via_verified_rollup": sum(
                1
                for row in bucket_rows
                if row.get("answer_sufficiency")
                == "verified_count_bearing_rollup_matches_dev_gold"
            ),
        }

    by_stratum: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_stratum.setdefault(str(row["hand_audited_stratum"]), []).append(row)
    return {
        "overall": summarize_bucket(rows),
        "by_stratum": {
            stratum: summarize_bucket(bucket_rows)
            for stratum, bucket_rows in sorted(by_stratum.items())
        },
    }


def exit_code_for_summary(
    summary: Mapping[str, object],
    *,
    has_errors: bool,
    expected_manifest: Mapping[str, int] | None = None,
) -> int:
    """Deterministic release-probe exit semantics, isolated for unit tests."""
    if has_errors:
        return EXIT_RUN_FAILURES
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        return EXIT_STRATUM_FAILURES
    required_nonzero = ("audited", "safety_checks", "answer_sufficiency_checks")
    if any(int(overall.get(key, 0)) < 1 for key in required_nonzero):
        return EXIT_STRATUM_FAILURES
    if expected_manifest is not None and any(
        int(overall.get(key, -1)) != expected for key, expected in expected_manifest.items()
    ):
        return EXIT_STRATUM_FAILURES
    if int(overall.get("mechanism_expectations_failed", 0)) or int(
        overall.get("safety_expectations_failed", 0)
    ):
        return EXIT_STRATUM_FAILURES
    checks = int(overall.get("answer_sufficiency_checks", 0))
    sufficient = int(overall.get("answer_sufficient_via_verified_rollup", 0))
    if checks > 0 and sufficient != checks:
        return EXIT_STRATUM_FAILURES
    return EXIT_OK


def _load_question_ids(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[count] cannot read question-id file: {exc}", file=sys.stderr)
        return None
    return [
        line
        for raw_line in text.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]


def default_output_path(
    dataset_path: Path,
    *,
    accept_rollups: bool,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
) -> Path:
    mode = "rollups_on" if accept_rollups else "rollups_off"
    # Raw-label rows get their own default file so they cannot overwrite the
    # anonymised ones, which carry the default name.
    labels = "_raw_labels" if session_label_mode == SESSION_LABEL_MODE_RAW else ""
    return RESULTS_DIR / f"count_probe_{dataset_path.stem}_{mode}{labels}.jsonl"


def expected_audit_manifest(question_ids: Path) -> Mapping[str, int] | None:
    """Return the governed full-slice manifest, including for limited runs."""
    return (
        DEFAULT_AUDIT_MANIFEST
        if question_ids.resolve() == DEFAULT_SLICE.resolve()
        else None
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="count_probe.py",
        description="Keyless context-pack aggregate-information probe for count-intent questions.",
    )
    parser.add_argument("--dataset-file", type=Path, default=None)
    parser.add_argument(
        "--question-ids",
        type=Path,
        default=DEFAULT_SLICE,
        help="one question_id per line (default: fixed stage1-150 slice, comments ignored)",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--accept-rollups",
        action="store_true",
        help="deterministically build/accept roll-up cards during ingest (default: off)",
    )
    parser.add_argument(
        "--raw-session-labels",
        action="store_true",
        help="store the dataset's raw session ids instead of keyed-hash labels (default: labels); "
        "only for reproducing pre-1.1 behaviour. Each row records the mode.",
    )
    parser.add_argument("--with-vectors", action="store_true")
    parser.add_argument("--with-reranker", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.with_vectors:
        disable_embeddings_env()
    if not args.with_reranker:
        disable_reranker_env()

    dataset_path = args.dataset_file if args.dataset_file is not None else resolve_dataset_path("s")
    if dataset_path is None or not dataset_path.is_file():
        print("[count] dataset not found; pass --dataset-file", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        questions = load_dataset(dataset_path)
    except LongMemEvalDatasetError as exc:
        print(f"[count] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    question_ids = _load_question_ids(args.question_ids)
    if question_ids is None:
        return EXIT_CONFIG_ERROR
    wanted = set(question_ids)
    selected = [question for question in questions if question.question_id in wanted]
    missing = wanted - {question.question_id for question in selected}
    if missing:
        print(f"[count] requested ids missing from dataset: {sorted(missing)[:5]}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    # Keep all hand-audited rows, plus detector-positive rows for measurement.
    selected = [
        question
        for question in selected
        if hand_audited_stratum(question.question_id) != STRATUM_DETECTOR_ONLY
        or (
            (intent := detect_aggregation_intent(question.question)) is not None
            and intent.kind == AGGREGATION_KIND_COUNT
        )
    ]
    if args.limit is not None:
        selected = selected[: args.limit]
    if not selected:
        print("[count] no count-intent questions selected", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    max_items = args.max_items if args.max_items is not None else max_items_from_env()
    session_label_mode = SESSION_LABEL_MODE_RAW if args.raw_session_labels else DEFAULT_SESSION_LABEL_MODE
    args.work_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or default_output_path(
        dataset_path,
        accept_rollups=args.accept_rollups,
        session_label_mode=session_label_mode,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[count] dataset={dataset_path.name} questions={len(selected)} max_items={max_items} "
        f"vectors={'ambient' if args.with_vectors else 'disabled'} work_dir={args.work_dir} "
        f"workers={max(1, args.workers)} accept_rollups={args.accept_rollups} labels={session_label_mode}"
    )

    rows_by_id: dict[str, dict[str, object]] = {}
    errors: list[tuple[str, str]] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                probe_question,
                question,
                work_dir=args.work_dir,
                dataset_path=dataset_path,
                max_items=max_items,
                accept_rollups=args.accept_rollups,
                session_label_mode=session_label_mode,
            ): question
            for question in selected
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            question = futures[future]
            try:
                rows_by_id[question.question_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - report every question independently
                errors.append((question.question_id, f"{type(exc).__name__}: {exc}"))
                print(f"[count] {question.question_id} ERROR: {exc}", file=sys.stderr, flush=True)
            if completed % 25 == 0 or completed == len(selected):
                print(f"[count] {completed}/{len(selected)} probed", flush=True)

    rows = [rows_by_id[question.question_id] for question in selected if question.question_id in rows_by_id]
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")

    summary = summarize_rows(rows)
    summary_path = out_path.with_suffix(".summary.json")
    summary_payload = {
        "schema": COUNT_PROBE_SCHEMA + "_summary",
        "dataset_file": dataset_path.name,
        "dataset_sha256_prefix": _sha256_prefix(dataset_path),
        "question_id_file": str(args.question_ids),
        "max_items": max_items,
        "vectors": "ambient" if args.with_vectors else "disabled",
        "accept_rollups": args.accept_rollups,
        "session_label_mode": session_label_mode,
        "questions": len(rows),
        "errors": [{"question_id": question_id, "error": error} for question_id, error in errors],
        **summary,
    }
    summary_path.write_text(
        json.dumps(summary_payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"[count] done in {time.monotonic() - started:.1f}s rows={out_path} summary={summary_path}")
    # A ``--limit`` run over the governed default slice is diagnostic only:
    # keep the full manifest expectation so a partial prefix cannot become a
    # green release receipt merely because every row it happened to include
    # passed. Custom question-id manifests still use the generic non-zero
    # stratum checks above.
    expected_manifest = expected_audit_manifest(args.question_ids)
    return exit_code_for_summary(
        summary,
        has_errors=bool(errors),
        expected_manifest=expected_manifest,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "COUNT_PROBE_SCHEMA",
    "DEFAULT_SLICE",
    "DEFAULT_AUDIT_MANIFEST",
    "DEFAULT_WORK_DIR",
    "EXIT_CONFIG_ERROR",
    "EXIT_OK",
    "EXIT_RUN_FAILURES",
    "EXIT_STRATUM_FAILURES",
    "STRATUM_CADENCE_ABSTENTION",
    "STRATUM_CADENCE_ANSWERABLE",
    "STRATUM_CANDIDATE_COUNT",
    "STRATUM_DETECTOR_ONLY",
    "STRATUM_NUMERIC_SAFE_NON_EMISSION",
    "STRATUM_REJECTED_GATE_WIDENING",
    "build_arg_parser",
    "default_output_path",
    "expected_audit_manifest",
    "exit_code_for_summary",
    "hand_audited_stratum",
    "main",
    "probe_question",
    "probe_row",
    "summarize_rows",
]
