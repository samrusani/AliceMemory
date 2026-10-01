from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterator

import pytest

import alicebot_api.vnext_evals as vnext_evals_module
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_evals import (
    CORRECTION_SUPPRESSION_SUITE_KEY,
    DECISION_MEMORY_KEY_PREFIX,
    DECISION_RECOVERY_SUITE_KEY,
    PROVENANCE_EXPLANATION_SUITE_KEY,
    RETRIEVAL_QUALITY_SUITE_KEY,
    SUBSET_LEXICAL_OVERLAP,
    SUBSET_PARAPHRASE,
    VNEXT_BENCHMARK_EXPECTED_COUNTS,
    VNEXT_EVAL_DATABASE_URL_ENV,
    VNEXT_EVAL_DEFAULT_USER_ID,
    VNEXT_EVAL_FIXED_VALID_FROM,
    VNEXT_EVAL_FIXED_VALID_TO,
    VNEXT_EVAL_MEMORY_KEY_PREFIX,
    VNEXT_EVAL_REFERENCE_TIME,
    VNEXT_EVAL_SUITE_ORDER,
    eval_token_overlap,
    generate_correction_suppression_corpus,
    generate_decision_recovery_corpus,
    generate_graph_hop_corpus,
    generate_provenance_explanation_corpus,
    generate_vnext_benchmark_corpus,
    latency_percentile,
    recall_at_k,
    reciprocal_rank,
    retrieval_request_supports_memory_types,
    run_correction_suppression_eval,
    run_decision_recovery_eval,
    run_provenance_explanation_eval,
    run_retrieval_quality_eval,
    run_vnext_evals,
    seed_retrieval_corpus,
    write_vnext_benchmark_corpus,
    write_vnext_eval_report,
)
from alicebot_api.vnext_retrieval import (
    VNextRetrievalRequest,
    VNextRetrievalService,
    classify_query,
    reciprocal_rank_fusion,
)
from alicebot_api.vnext_temporal_query import parse_temporal_anchor

MEMORY_QUALITY_SUITE_KEYS = (
    CORRECTION_SUPPRESSION_SUITE_KEY,
    DECISION_RECOVERY_SUITE_KEY,
    PROVENANCE_EXPLANATION_SUITE_KEY,
)


def test_eval_case_failure_evidence_is_static_and_omits_exception_details() -> None:
    sentinel = "UNIQUE_EVAL_EXCEPTION_SENTINEL"

    record = vnext_evals_module._error_case("case-1", RuntimeError(sentinel))

    assert record["evidence"] == {
        "error_code": "eval_case_failed",
        "error_message": "The evaluation case could not be completed",
    }
    assert sentinel not in json.dumps(record)
    source = Path(vnext_evals_module.__file__).read_text(encoding="utf-8")
    assert '"error_type": type(exc).__name__' not in source
    assert '"error_message": str(exc)' not in source


@pytest.fixture(autouse=True)
def _clear_eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VNEXT_EVAL_DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)


def _memory_lookup(corpus: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(memory["memory_key"]): memory for memory in corpus["memories"]}


def _perfect_retrieval_fn(corpus: dict[str, object]):
    expected_by_query = {str(query["query"]): str(query["expected_memory_key"]) for query in corpus["queries"]}

    def _retrieve(query: str, *, limit: int) -> dict[str, object]:
        return {
            "ranked_memory_keys": [expected_by_query[query]],
            "vector_stage": "enabled",
            "vector_candidate_count": 1,
        }

    return _retrieve


def _hopeless_retrieval_fn(query: str, *, limit: int) -> dict[str, object]:
    return {
        "ranked_memory_keys": [],
        "vector_stage": "enabled",
        "vector_candidate_count": 1,
    }


def _fts_only_retrieval_fn(corpus: dict[str, object]):
    """Mimic a no-embedding-provider run: lexical hits land, paraphrases miss.

    The ``vector_stage`` marker matches production's disabled label so the
    suite classifies the run as ``fts_only``.
    """
    expected_by_query = {
        str(query["query"]): (
            str(query["expected_memory_key"]),
            str(query.get("subset", SUBSET_LEXICAL_OVERLAP)),
        )
        for query in corpus["queries"]
    }

    def _retrieve(query: str, *, limit: int) -> dict[str, object]:
        expected_key, subset = expected_by_query[query]
        ranked = [expected_key] if subset == SUBSET_LEXICAL_OVERLAP else []
        return {
            "ranked_memory_keys": ranked,
            "vector_stage": "disabled: no embedding provider configured",
        }

    return _retrieve


# --------------------------------------------------------------------------
# Corpus properties
# --------------------------------------------------------------------------


def test_benchmark_corpus_is_deterministic_and_meets_size_floor() -> None:
    corpus = generate_vnext_benchmark_corpus()

    assert corpus == generate_vnext_benchmark_corpus()
    assert corpus["schema_version"] == "vnext_eval_corpus_v1"
    assert corpus["counts"] == VNEXT_BENCHMARK_EXPECTED_COUNTS
    assert len(corpus["memories"]) >= 200
    assert len(corpus["queries"]) >= 40

    memory_keys = [str(memory["memory_key"]) for memory in corpus["memories"]]
    assert len(memory_keys) == len(set(memory_keys))
    assert all(key.startswith(VNEXT_EVAL_MEMORY_KEY_PREFIX) for key in memory_keys)
    lookup = set(memory_keys)
    assert all(str(query["expected_memory_key"]) in lookup for query in corpus["queries"])

    query_count = len(corpus["queries"])
    paraphrase_count = sum(1 for query in corpus["queries"] if query["subset"] == SUBSET_PARAPHRASE)
    assert 0.20 <= paraphrase_count / query_count <= 0.40


def test_benchmark_queries_do_not_infer_a_domain_that_excludes_their_target() -> None:
    corpus = generate_vnext_benchmark_corpus()
    memories = _memory_lookup(corpus)

    for query in corpus["queries"]:
        target = memories[str(query["expected_memory_key"])]
        inferred_domains = classify_query(
            VNextRetrievalRequest(query=str(query["query"]))
        )["domains"]
        assert not inferred_domains or str(target["domain"]) in inferred_domains, (
            f"{query['query_key']} inferred {inferred_domains}, excluding "
            f"its {target['domain']} target"
        )


def test_queries_are_phrased_differently_from_their_target_memories() -> None:
    corpus = generate_vnext_benchmark_corpus()
    memories = _memory_lookup(corpus)

    for query in corpus["queries"]:
        target = memories[str(query["expected_memory_key"])]
        target_text = f"{target['canonical_text']} {target['title']}"
        overlap = eval_token_overlap(str(query["query"]), target_text)
        assert str(query["query"]).casefold() != str(target["canonical_text"]).casefold()
        if query["subset"] == SUBSET_PARAPHRASE:
            # Pure paraphrases: near-zero verbatim vocabulary overlap, so
            # lexical search alone should struggle on this subset.
            assert overlap < 0.40, f"{query['query_key']} overlaps too much ({overlap:.2f})"
        else:
            # Reworded but vocabulary-sharing: FTS should still cope.
            assert 0.50 <= overlap <= 0.90, f"{query['query_key']} outside overlap band ({overlap:.2f})"


# --------------------------------------------------------------------------
# Metric math
# --------------------------------------------------------------------------


def test_recall_at_k_and_reciprocal_rank_math() -> None:
    ranked = ["m-2", "m-7", "m-1", "m-9"]

    assert recall_at_k(ranked, "m-2", 1) == 1.0
    assert recall_at_k(ranked, "m-1", 1) == 0.0
    assert recall_at_k(ranked, "m-1", 5) == 1.0
    assert recall_at_k(ranked, "missing", 5) == 0.0
    assert reciprocal_rank(ranked, "m-2") == 1.0
    assert reciprocal_rank(ranked, "m-1") == pytest.approx(1.0 / 3.0)
    assert reciprocal_rank(ranked, "missing") == 0.0
    with pytest.raises(ValueError):
        recall_at_k(ranked, "m-2", 0)


def test_latency_percentile_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

    assert latency_percentile(values, 50) == 50.0
    assert latency_percentile(values, 95) == 100.0
    assert latency_percentile([42.0], 95) == 42.0
    assert latency_percentile([], 50) == 0.0
    with pytest.raises(ValueError):
        latency_percentile(values, 0)


def test_retrieval_quality_metrics_against_known_rankings() -> None:
    corpus = {
        "queries": [
            {"query_key": "q-1", "query": "alpha", "expected_memory_key": "m-1", "subset": SUBSET_LEXICAL_OVERLAP},
            {"query_key": "q-2", "query": "beta", "expected_memory_key": "m-2", "subset": SUBSET_LEXICAL_OVERLAP},
            {"query_key": "q-3", "query": "gamma", "expected_memory_key": "m-3", "subset": SUBSET_PARAPHRASE},
        ]
    }
    rankings = {
        "alpha": ["m-1", "m-9"],  # rank 1
        "beta": ["m-9", "m-8", "m-2"],  # rank 3
        "gamma": ["m-9", "m-8", "m-7"],  # not found
    }

    def fake_retrieval(query: str, *, limit: int) -> dict[str, object]:
        return {
            "ranked_memory_keys": rankings[query],
            "vector_stage": "enabled",
            "vector_candidate_count": len(rankings[query]),
        }

    suite = run_retrieval_quality_eval(None, retrieval_fn=fake_retrieval, corpus=corpus)

    assert suite["status"] == "fail"  # paraphrase recall 0.0 < 0.70 with vector enabled
    assert suite["metrics"]["query_count"] == 3
    assert suite["metrics"]["recall_at_1"] == pytest.approx(1.0 / 3.0)
    assert suite["metrics"]["recall_at_5"] == pytest.approx(2.0 / 3.0)
    assert suite["metrics"]["mrr"] == pytest.approx((1.0 + 1.0 / 3.0 + 0.0) / 3.0)
    assert suite["metrics"]["retrieval_mode"] == "hybrid"
    assert suite["metrics"]["subsets"][SUBSET_LEXICAL_OVERLAP]["recall_at_5"] == pytest.approx(1.0)
    assert suite["metrics"]["subsets"][SUBSET_PARAPHRASE]["recall_at_5"] == 0.0
    assert suite["metrics"]["target_checks"]["paraphrase_recall_at_5"] == "fail"
    case_statuses = {case["case_key"]: case["status"] for case in suite["cases"]}
    assert case_statuses == {"q-1": "pass", "q-2": "pass", "q-3": "fail"}
    assert all(case["metrics"]["latency_ms"] >= 0.0 for case in suite["cases"])


def test_retrieval_quality_matches_reciprocal_rank_fusion_ordering() -> None:
    # A paraphrase target missed by FTS but ranked first by the vector stage
    # must land at the top after RRF, exactly as the production fusion does.
    fts_rows = [{"id": "m-lex"}, {"id": "m-noise"}]
    vector_rows = [{"id": "m-para"}, {"id": "m-lex"}]
    fused = reciprocal_rank_fusion({"fts": fts_rows, "vector": vector_rows})
    fused_ids = [str(item["id"]) for item, _score, _stages in fused]
    assert fused_ids[0] == "m-lex"  # appears in both stages
    assert "m-para" in fused_ids[:2]

    corpus = {
        "queries": [
            {"query_key": "q-1", "query": "fused", "expected_memory_key": "m-para", "subset": SUBSET_PARAPHRASE},
        ]
    }

    def fused_retrieval(query: str, *, limit: int) -> dict[str, object]:
        return {
            "ranked_memory_keys": fused_ids[:limit],
            "vector_stage": "enabled",
            "vector_candidate_count": len(vector_rows),
        }

    suite = run_retrieval_quality_eval(None, retrieval_fn=fused_retrieval, corpus=corpus)
    case = suite["cases"][0]

    assert case["metrics"]["recall_at_5"] == 1.0
    assert case["metrics"]["reciprocal_rank"] == pytest.approx(1.0 / (fused_ids.index("m-para") + 1))


def test_paraphrase_target_not_enforced_when_vector_stage_degraded() -> None:
    corpus = {
        "queries": [
            {"query_key": "q-1", "query": "alpha", "expected_memory_key": "m-1", "subset": SUBSET_LEXICAL_OVERLAP},
            {"query_key": "q-2", "query": "gamma", "expected_memory_key": "m-3", "subset": SUBSET_PARAPHRASE},
        ]
    }

    def fts_only_retrieval(query: str, *, limit: int) -> dict[str, object]:
        ranked = ["m-1"] if query == "alpha" else []
        return {"ranked_memory_keys": ranked, "vector_stage": "disabled: no embedding provider configured"}

    suite = run_retrieval_quality_eval(None, retrieval_fn=fts_only_retrieval, corpus=corpus)

    assert suite["metrics"]["retrieval_mode"] == "fts_only"
    assert suite["metrics"]["paraphrase_targets_enforced"] is False
    assert "paraphrase_recall_at_5" not in suite["metrics"]["target_checks"]
    # The degraded paraphrase numbers are still reported, not hidden.
    assert suite["metrics"]["subsets"][SUBSET_PARAPHRASE]["recall_at_5"] == 0.0
    assert suite["status"] == "pass"  # lexical subset met its target


def test_release_gate_fts_only_is_not_reported_as_unqualified_pass() -> None:
    # Reproduction for audit P1 #8: without the vector stage the
    # release-designated eval must NOT masquerade as a full "pass". The
    # dev-facing run stays "pass" (lexical targets met), but the canonical
    # release gate downgrades to "pass_fts_only" because paraphrase/semantic
    # quality was never measured.
    corpus = {
        "queries": [
            {"query_key": "q-1", "query": "alpha", "expected_memory_key": "m-1", "subset": SUBSET_LEXICAL_OVERLAP},
            {"query_key": "q-2", "query": "gamma", "expected_memory_key": "m-3", "subset": SUBSET_PARAPHRASE},
        ]
    }
    retrieval_fn = _fts_only_retrieval_fn(corpus)

    dev = run_retrieval_quality_eval(None, retrieval_fn=retrieval_fn, corpus=corpus)
    gated = run_retrieval_quality_eval(None, retrieval_fn=retrieval_fn, corpus=corpus, release_gate=True)

    # Dev-facing behavior is preserved: an fts_only lexical-only run is a pass.
    assert dev["status"] == "pass"
    # The canonical release gate refuses to call it an unqualified pass.
    assert gated["status"] != "pass"
    assert gated["status"] == "pass_fts_only"
    assert gated["metrics"]["paraphrase_targets_enforced"] is False
    # Per-case fields are preserved.
    case_statuses = {case["case_key"]: case["status"] for case in gated["cases"]}
    assert case_statuses == {"q-1": "pass", "q-2": "fail"}


def test_run_vnext_evals_release_gate_downgrades_fts_only_aggregate() -> None:
    corpus = generate_vnext_benchmark_corpus()
    retrieval_fn = _fts_only_retrieval_fn(corpus)

    dev = run_vnext_evals(suite="retrieval_quality", retrieval_fn=retrieval_fn)
    gated = run_vnext_evals(suite="retrieval_quality", retrieval_fn=retrieval_fn, release_gate=True)

    # Dev aggregate keeps the legacy fts_only "pass"; the release aggregate
    # fails because the unmeasured paraphrase cases are not passing evidence.
    assert dev["status"] == "pass"
    assert gated["status"] == "fail"


def test_release_gate_uses_suite_targets_while_preserving_case_misses() -> None:
    corpus = generate_vnext_benchmark_corpus()
    query_contract = {
        str(query["query"]): (
            str(query["query_key"]),
            str(query["expected_memory_key"]),
        )
        for query in corpus["queries"]
    }

    def run_with_misses(missed_case_keys: set[str]) -> dict[str, object]:
        def retrieve(query: str, *, limit: int) -> dict[str, object]:
            del limit
            case_key, expected_key = query_contract[query]
            return {
                "ranked_memory_keys": (
                    ["vnext-eval/retrieval/distractor-001"]
                    if case_key in missed_case_keys
                    else [expected_key]
                ),
                "vector_stage": "enabled",
                "vector_candidate_count": 20,
            }

        return run_vnext_evals(
            suite="retrieval_quality",
            retrieval_fn=retrieve,
            release_gate=True,
        )

    passing = run_with_misses(
        {"paraphrase-004", "paraphrase-011", "paraphrase-016"}
    )
    failing = run_with_misses(
        {
            "paraphrase-004",
            "paraphrase-007",
            "paraphrase-011",
            "paraphrase-015",
            "paraphrase-016",
        }
    )

    passing_suite = passing["suites"][0]
    assert passing_suite["status"] == "pass"
    assert passing_suite["metrics"]["subsets"][SUBSET_PARAPHRASE]["recall_at_5"] == 13 / 16
    assert passing_suite["metrics"]["target_checks"]["paraphrase_recall_at_5"] == "pass"
    assert passing["status"] == "pass"
    assert passing["summary"]["passed_case_count"] == 45
    assert passing["summary"]["failed_case_count"] == 3
    assert passing["summary"]["pass_rate"] == 45 / 48

    failing_suite = failing["suites"][0]
    assert failing_suite["metrics"]["subsets"][SUBSET_PARAPHRASE]["recall_at_5"] == 11 / 16
    assert failing_suite["metrics"]["target_checks"]["paraphrase_recall_at_5"] == "fail"
    assert failing_suite["status"] == "fail"
    assert failing["status"] == "fail"


def test_release_gate_requires_nonzero_vector_candidates() -> None:
    corpus = {
        "queries": [
            {
                "query_key": "q-1",
                "query": "alpha",
                "expected_memory_key": "m-1",
                "subset": SUBSET_LEXICAL_OVERLAP,
            },
            {
                "query_key": "q-2",
                "query": "semantic paraphrase",
                "expected_memory_key": "m-2",
                "subset": SUBSET_PARAPHRASE,
            },
        ]
    }

    def enabled_but_empty(query: str, *, limit: int) -> dict[str, object]:
        del limit
        expected = "m-1" if query == "alpha" else "m-2"
        return {
            "ranked_memory_keys": [expected],
            "vector_stage": "enabled",
            "vector_candidate_count": 0,
        }

    gated = run_retrieval_quality_eval(
        None,
        retrieval_fn=enabled_but_empty,
        corpus=corpus,
        release_gate=True,
    )

    assert gated["status"] == "pass_fts_only"
    assert gated["metrics"]["vector_candidate_count"] == 0
    assert gated["metrics"]["vector_queries_with_candidates"] == 0
    assert gated["metrics"]["vector_stage_participated"] is False


# --------------------------------------------------------------------------
# Skip semantics: no live store means skipped, never a fabricated pass
# --------------------------------------------------------------------------


def test_retrieval_quality_eval_without_store_reports_skipped() -> None:
    suite = run_retrieval_quality_eval(None)

    assert suite["suite_key"] == RETRIEVAL_QUALITY_SUITE_KEY
    assert suite["status"] == "skipped"
    assert "live store" in str(suite["reason"])
    assert suite["cases"] == []


def test_run_vnext_evals_without_live_store_reports_skipped_not_pass() -> None:
    report = run_vnext_evals(suite="all")

    assert report["status"] == "skipped"
    assert report["summary"]["status"] == "skipped"
    assert report["summary"]["executed_suite_count"] == 0
    assert report["summary"]["skipped_suite_count"] == len(VNEXT_EVAL_SUITE_ORDER)
    assert [entry["suite_key"] for entry in report["skipped_suites"]] == list(VNEXT_EVAL_SUITE_ORDER)
    assert all(suite["status"] == "skipped" for suite in report["suites"])


def test_memory_quality_suites_without_store_report_skipped() -> None:
    for runner, suite_key in (
        (run_correction_suppression_eval, CORRECTION_SUPPRESSION_SUITE_KEY),
        (run_decision_recovery_eval, DECISION_RECOVERY_SUITE_KEY),
        (run_provenance_explanation_eval, PROVENANCE_EXPLANATION_SUITE_KEY),
    ):
        suite = runner(None)
        assert suite["suite_key"] == suite_key
        assert suite["status"] == "skipped"
        assert "live store" in str(suite["reason"])
        assert suite["cases"] == []


def test_memory_quality_suite_skips_with_reason_when_store_surface_missing() -> None:
    class _BareStore:
        def create_memory(self, memory, *, actor_type="system"):
            return dict(memory)

    suite = run_provenance_explanation_eval(_BareStore())

    assert suite["status"] == "skipped"
    assert "required surface" in str(suite["reason"])


# --------------------------------------------------------------------------
# Report semantics and shape compatibility
# --------------------------------------------------------------------------


def test_report_passes_only_when_executed_suites_pass() -> None:
    corpus = generate_vnext_benchmark_corpus()

    passing = run_vnext_evals(suite="all", retrieval_fn=_perfect_retrieval_fn(corpus))
    failing = run_vnext_evals(suite="all", retrieval_fn=_hopeless_retrieval_fn)

    # retrieval_fn injection only drives the retrieval-quality suite; the
    # commit-flow suites cannot run without a store and skip honestly.
    assert passing["status"] == "pass"
    assert passing["summary"]["suite_count"] == len(VNEXT_EVAL_SUITE_ORDER)
    assert passing["summary"]["executed_suite_count"] == 1
    assert passing["summary"]["skipped_suite_count"] == len(VNEXT_EVAL_SUITE_ORDER) - 1
    assert passing["summary"]["failed_case_count"] == 0
    assert failing["status"] == "fail"
    assert failing["summary"]["passed_case_count"] == 0
    assert failing["suites"][0]["metrics"]["recall_at_5"] == 0.0


def test_release_gate_fails_when_only_some_requested_suites_execute() -> None:
    corpus = generate_vnext_benchmark_corpus()

    report = run_vnext_evals(
        suite="all",
        retrieval_fn=_perfect_retrieval_fn(corpus),
        release_gate=True,
    )

    assert report["summary"]["executed_suite_count"] == 1
    assert report["summary"]["skipped_suite_count"] == len(VNEXT_EVAL_SUITE_ORDER) - 1
    assert report["status"] == "fail"
    assert report["summary"]["status"] == "fail"


def test_release_gate_derives_failure_from_case_and_target_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vnext_evals_module,
        "run_retrieval_quality_eval",
        lambda *_args, **_kwargs: {
            "suite_key": RETRIEVAL_QUALITY_SUITE_KEY,
            "status": "pass",
            "metrics": {"target_checks": {"recall_at_5": "fail"}},
            "cases": [{"case_key": "adversarial", "status": "fail", "metrics": {}}],
        },
    )

    report = run_vnext_evals(
        suite="retrieval_quality",
        retrieval_fn=lambda *_args, **_kwargs: {},
        release_gate=True,
    )

    assert report["status"] == "fail"
    assert report["summary"]["status"] == "fail"
    assert report["summary"]["case_count"] == 1
    assert report["summary"]["passed_case_count"] == 0
    assert report["summary"]["failed_case_count"] == 1
    assert report["summary"]["pass_rate"] == 0.0


def test_report_keeps_top_level_shape_for_cli_seam() -> None:
    report = run_vnext_evals(suite="all")

    # cli.py serializes the report as-is; these keys are the stable contract.
    for key in (
        "schema_version",
        "generated_at",
        "suite",
        "status",
        "embedding_signature",
        "targets",
        "suites",
        "summary",
    ):
        assert key in report
    assert report["suite"] == "all"
    assert isinstance(report["suites"], list)
    assert isinstance(report["targets"], dict)
    assert report["summary"]["suite_order"] == list(VNEXT_EVAL_SUITE_ORDER)


def test_generated_at_is_real_time_not_hardcoded() -> None:
    fixed = datetime(2026, 7, 4, 12, 30, 0, tzinfo=timezone.utc)

    stamped = run_vnext_evals(suite="all", now_fn=lambda: fixed)
    defaulted = run_vnext_evals(suite="all")

    assert stamped["generated_at"] == "2026-07-04T12:30:00Z"
    assert defaulted["generated_at"] != "2026-05-11T00:00:00Z"
    parsed = datetime.fromisoformat(str(defaulted["generated_at"]).replace("Z", "+00:00"))
    assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 60
    # Wall-clock time must not leak into the digest.
    assert stamped["report_digest"] == defaulted["report_digest"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(stamped["report_digest"]))


def test_report_digest_binds_all_semantic_evidence_but_not_generated_at() -> None:
    report = run_vnext_evals(suite="all")
    original_digest = report["report_digest"]

    report["generated_at"] = "2099-01-01T00:00:00Z"
    assert vnext_evals_module.semantic_eval_report_digest(report) == original_digest

    report["targets"][RETRIEVAL_QUALITY_SUITE_KEY][
        "lexical_overlap_recall_at_5"
    ]["minimum"] = 0.0
    assert vnext_evals_module.semantic_eval_report_digest(report) != original_digest


def test_vnext_eval_rejects_unknown_suite() -> None:
    with pytest.raises(ValueError, match="unknown vNext eval suite"):
        run_vnext_evals(suite="recall")


# --------------------------------------------------------------------------
# Seeding writes through the real store surface
# --------------------------------------------------------------------------


class RecordingStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def create_memory(self, memory: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        row = dict(memory)
        row["id"] = f"row-{len(self.created):03d}"
        self.created.append(row)
        return row


def test_seed_retrieval_corpus_writes_active_memories_via_store() -> None:
    corpus = generate_vnext_benchmark_corpus()
    store = RecordingStore()

    seeding = seed_retrieval_corpus(store, corpus)

    assert seeding["seeded_memory_count"] == len(corpus["memories"])
    assert seeding["embedded_memory_count"] == 0  # no provider configured
    assert "vector stage inactive" in str(seeding["embedding_note"])
    assert all(row["status"] == "active" for row in store.created)
    assert all(str(row["memory_key"]).startswith(VNEXT_EVAL_MEMORY_KEY_PREFIX) for row in store.created)
    assert all(row["canonical_text"] for row in store.created)


def test_seed_retrieval_corpus_reports_static_embedding_failure_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "UNIQUE_EVAL_EMBEDDING_EXCEPTION_SENTINEL"

    class EmbeddingStore(RecordingStore):
        def update_memory_embedding(self, **update: object) -> dict[str, object]:
            return {"id": update["memory_id"]}

    class FailingProvider:
        provider = "configured-test"
        model = "semantic-v1"

        def embed_batch(self, texts: object) -> list[list[float]]:
            raise vnext_evals_module.VNextEmbeddingProviderError(sentinel)

    monkeypatch.setattr(vnext_evals_module, "get_embedding_provider", lambda: FailingProvider())

    seeding = seed_retrieval_corpus(
        EmbeddingStore(),
        {
            "memories": [
                {
                    "memory_key": "vnext-eval/retrieval/failure-1",
                    "canonical_text": "Embedding failure report fixture.",
                }
            ]
        },
    )

    assert seeding["embedding_note"] == vnext_evals_module.EMBEDDING_PROVIDER_FAILED_NOTE
    assert sentinel not in json.dumps(seeding)


def test_seed_retrieval_corpus_persists_complete_signed_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmbeddingStore(RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.updates: list[dict[str, object]] = []

        def update_memory_embedding(self, **update: object) -> dict[str, object]:
            self.updates.append(update)
            return {"id": update["memory_id"]}

    class Provider:
        provider = "configured-test"
        model = "semantic-v1"
        base_url = "HTTPS://Embed.Example:443/Case/V1"

        def embed_batch(self, texts):
            return [[0.5, 0.25] for _text in texts]

    store = EmbeddingStore()
    corpus = {
        "memories": [
            {
                "memory_key": "vnext-eval/retrieval/signed-1",
                "canonical_text": "A product-usable signed vector.",
                "title": "Signed vector",
            }
        ]
    }
    monkeypatch.setattr(vnext_evals_module, "get_embedding_provider", lambda: Provider())

    seeding = seed_retrieval_corpus(store, corpus)

    assert seeding["embedded_memory_count"] == 1
    assert len(store.updates) == 1
    update = store.updates[0]
    assert update["signature_version"] == 2
    assert update["provider"] == "configured-test"
    assert update["model"] == "semantic-v1"
    assert update["endpoint"]
    assert update["content_sha256"]
    assert len(update["vector"]) == 1536
    assert seeding["embedding_signature"] == {
        "schema_version": "alice_embedding_signature_identity_v1",
        "signature_version": 2,
        "provider": "configured-test",
        "provider_fingerprint": vnext_evals_module.sha256(b"configured-test").hexdigest(),
        "model": "semantic-v1",
        "model_fingerprint": vnext_evals_module.sha256(b"semantic-v1").hexdigest(),
        "endpoint_fingerprint": update["endpoint"],
    }
    assert "Embed.Example" not in json.dumps(seeding["embedding_signature"])


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------


def test_backend_label_reported_for_injected_runs() -> None:
    corpus = {
        "queries": [
            {"query_key": "q-1", "query": "alpha", "expected_memory_key": "m-1", "subset": SUBSET_LEXICAL_OVERLAP},
        ]
    }

    def retrieval(query: str, *, limit: int) -> dict[str, object]:
        return {
            "ranked_memory_keys": ["m-1"],
            "vector_stage": "enabled",
            "vector_candidate_count": 1,
        }

    suite = run_retrieval_quality_eval(None, retrieval_fn=retrieval, corpus=corpus)

    assert suite["metrics"]["backend"] == "injected"


# --------------------------------------------------------------------------
# Live sqlite backend: the full production pipeline runs with no services.
# This is the CI-runnable live path -- seeding, FTS5 retrieval, RRF fusion,
# and rollback all execute for real against sqlite:///:memory: or a file.
# --------------------------------------------------------------------------


def _assert_live_sqlite_suite_shape(report: dict[str, object]) -> dict[str, object]:
    assert report["status"] == "pass"
    assert report["summary"]["executed_suite_count"] == 1
    assert report["summary"]["skipped_suite_count"] == 0
    suite = report["suites"][0]
    metrics = suite["metrics"]

    assert suite["status"] == "pass"
    assert metrics["backend"] == "sqlite"
    # No embedding provider in unit tests: FTS5-only, degraded honestly.
    assert metrics["retrieval_mode"] == "fts_only"
    assert metrics["paraphrase_targets_enforced"] is False
    assert "paraphrase_recall_at_5" not in metrics["target_checks"]

    # FTS5 (porter + stopword-filtered MATCH) must recover every reworded
    # lexical query. Pure paraphrases share almost no vocabulary, so
    # without an embedding provider only the OR-fallback's occasional
    # shared-token hits land: recall stays low and honestly reported --
    # never fabricated up to the hybrid targets.
    assert metrics["subsets"][SUBSET_LEXICAL_OVERLAP]["recall_at_5"] == 1.0
    assert metrics["target_checks"]["lexical_overlap_recall_at_5"] == "pass"
    assert metrics["target_checks"]["lexical_overlap_mrr"] == "pass"
    assert 0.0 < metrics["subsets"][SUBSET_PARAPHRASE]["recall_at_5"] < 0.5

    seeding = metrics["seeding"]
    assert seeding["seeded_memory_count"] == VNEXT_BENCHMARK_EXPECTED_COUNTS["memories"]
    assert seeding["embedded_memory_count"] == 0
    assert "vector stage inactive" in str(seeding["embedding_note"])
    return metrics


def test_live_suite_runs_against_sqlite_memory_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite="retrieval_quality")

    metrics = _assert_live_sqlite_suite_shape(report)
    assert metrics["query_count"] == VNEXT_BENCHMARK_EXPECTED_COUNTS["queries"]


def test_live_sqlite_file_run_persists_no_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "eval.db"
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, f"sqlite:///{db_path}")

    report = run_vnext_evals(suite="retrieval_quality")

    _assert_live_sqlite_suite_shape(report)
    # The rollback must leave zero rows behind -- including the FTS5
    # shadow tables written by the external-content sync triggers.
    conn = sqlite3.connect(str(db_path))
    try:
        for table in ("users", "memories", "event_log"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        fts_hits = conn.execute(
            "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'launch'"
        ).fetchone()[0]
        assert fts_hits == 0
    finally:
        conn.close()


def test_live_sqlite_file_run_is_repeatable_on_the_same_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bootstrap is idempotent and each run rolls back, so re-running against
    # the same file must not hit duplicate-key errors or skew metrics.
    db_path = tmp_path / "eval.db"
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, f"sqlite:///{db_path}")

    first = run_vnext_evals(suite="retrieval_quality")
    second = run_vnext_evals(suite="retrieval_quality")

    _assert_live_sqlite_suite_shape(first)
    _assert_live_sqlite_suite_shape(second)
    assert first["summary"] == second["summary"]
    assert first["targets"] == second["targets"]
    assert [case["status"] for case in first["suites"][0]["cases"]] == [
        case["status"] for case in second["suites"][0]["cases"]
    ]
    # The exact evidence digest intentionally binds measured latency, so two
    # otherwise equivalent live runs need not share one digest.
    assert first["report_digest"] == vnext_evals_module.semantic_eval_report_digest(first)
    assert second["report_digest"] == vnext_evals_module.semantic_eval_report_digest(second)


def test_unsupported_sqlite_url_reports_skipped_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # sqlite3.connect("") would create a throwaway temp database; a malformed
    # URL must skip with a reason instead of silently "passing" against it.
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite://missing-slash.db")

    report = run_vnext_evals(suite="retrieval_quality")

    assert report["status"] == "skipped"
    assert report["summary"]["executed_suite_count"] == 0
    assert "unsupported sqlite eval URL" in str(report["skipped_suites"][0]["reason"])
    assert "missing-slash.db" not in json.dumps(report)


def test_live_store_skip_reason_is_static_and_omits_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "UNIQUE_EVAL_CONNECTION_EXCEPTION_SENTINEL"
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    @contextmanager
    def unavailable_store(_database_url: str):
        raise sqlite3.OperationalError(sentinel)
        yield  # pragma: no cover

    monkeypatch.setattr(vnext_evals_module, "_ephemeral_sqlite_eval_store", unavailable_store)

    result = vnext_evals_module._run_suite_against_live_store(
        run_with_store=lambda *_args, **_kwargs: {"status": "pass"},
        skipped=lambda reason: {"status": "skipped", "reason": reason},
    )

    assert result == {"status": "skipped", "reason": "live store unavailable"}
    assert sentinel not in json.dumps(result)


def test_corpus_and_report_writers_round_trip(tmp_path: Path) -> None:
    corpus_path = tmp_path / "vnext_corpus.json"
    report_path = tmp_path / "vnext_report.json"

    written_corpus_path = write_vnext_benchmark_corpus(corpus_path)
    report = run_vnext_evals(suite="all", corpus_path=written_corpus_path)
    written_report_path = write_vnext_eval_report(report=report, report_path=report_path)

    assert written_corpus_path == corpus_path.resolve()
    assert json.loads(corpus_path.read_text(encoding="utf-8"))["counts"] == VNEXT_BENCHMARK_EXPECTED_COUNTS
    assert written_report_path == report_path.resolve()
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


# --------------------------------------------------------------------------
# Memory-quality suites: suite order and deterministic corpora
# --------------------------------------------------------------------------


def test_suite_order_contains_all_registered_suites() -> None:
    assert VNEXT_EVAL_SUITE_ORDER == (
        RETRIEVAL_QUALITY_SUITE_KEY,
        CORRECTION_SUPPRESSION_SUITE_KEY,
        DECISION_RECOVERY_SUITE_KEY,
        PROVENANCE_EXPLANATION_SUITE_KEY,
        ENTITY_RESOLUTION_SUITE_KEY,
        GRAPH_HOP_RETRIEVAL_SUITE_KEY,
    )
    # Each new key is individually dispatchable (the CLI passes --suite through).
    for suite_key in MEMORY_QUALITY_SUITE_KEYS:
        report = run_vnext_evals(suite=suite_key)
        assert report["suite"] == suite_key
        assert [suite["suite_key"] for suite in report["suites"]] == [suite_key]


def test_memory_quality_corpora_are_deterministic() -> None:
    for generator, expected_kind in (
        (generate_correction_suppression_corpus, CORRECTION_SUPPRESSION_SUITE_KEY),
        (generate_decision_recovery_corpus, DECISION_RECOVERY_SUITE_KEY),
        (generate_provenance_explanation_corpus, PROVENANCE_EXPLANATION_SUITE_KEY),
    ):
        corpus = generator()
        assert corpus == generator()
        assert corpus["kind"] == expected_kind
        assert str(corpus["corpus_digest"]).startswith("sha256:")


def test_correction_corpus_probes_are_lexically_bound_to_their_targets() -> None:
    corpus = generate_correction_suppression_corpus()
    cases = corpus["cases"]

    assert len(cases) >= 5
    for case in cases:
        # The main query must be able to surface both A and B (AND-semantics
        # FTS): the query's content tokens appear in each text. The overlap
        # measure is verbatim while FTS stems (ship/ships), so allow a small
        # inflection gap.
        for text_key in ("original_text", "replacement_text"):
            overlap = eval_token_overlap(str(case["query"]), str(case[text_key]))
            assert overlap >= 0.7, f"{case['case_key']}: query does not cover {text_key} ({overlap:.2f})"
        # The old-fact probe must be satisfiable only by A: at least one of
        # its content tokens is missing from B's text.
        old_probe_overlap_with_b = eval_token_overlap(str(case["old_probe"]), str(case["replacement_text"]))
        assert old_probe_overlap_with_b < 1.0, f"{case['case_key']}: old probe cannot discriminate A from B"


def test_decision_corpus_mixes_memory_types_and_covers_queries() -> None:
    corpus = generate_decision_recovery_corpus()

    decision_types = {str(row["memory_type"]) for row in corpus["decisions"]}
    distractor_types = {str(row["memory_type"]) for row in corpus["distractors"]}
    assert decision_types == {"decision"}
    assert len(distractor_types) >= 4  # genuinely mixed-type distractor pool
    assert "decision" not in distractor_types
    assert len(corpus["distractors"]) >= 2 * len(corpus["decisions"])

    lookup = {str(row["memory_key"]): row for row in corpus["decisions"]}
    for query in corpus["queries"]:
        target = lookup[str(query["expected_memory_key"])]
        overlap = eval_token_overlap(str(query["query"]), str(target["canonical_text"]))
        # Decision-intent phrasing shares vocabulary but is not verbatim.
        assert str(query["query"]).casefold() != str(target["canonical_text"]).casefold()
        assert overlap >= 0.5, f"{query['query_key']} shares too little vocabulary ({overlap:.2f})"


# --------------------------------------------------------------------------
# Live sqlite execution of the memory-quality suites (full production code:
# commit service, review paths, retrieval pipeline, rollback -- no mocks).
# --------------------------------------------------------------------------

_LIVE_USER_ID = VNEXT_EVAL_DEFAULT_USER_ID


@contextmanager
def _live_sqlite_store() -> Iterator[SQLiteVNextStore]:
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    bootstrap_sqlite_schema(conn)
    conn.execute("BEGIN")
    ensure_sqlite_user(conn, _LIVE_USER_ID, "vnext-eval-test@example.invalid", "vNext Eval Test")
    try:
        yield SQLiteVNextStore(conn, _LIVE_USER_ID)
    finally:
        conn.rollback()
        conn.close()


class _OverrideStore:
    """Delegating wrapper that breaks selected store methods for failure tests."""

    def __init__(self, inner: object, **overrides: object) -> None:
        self._inner = inner
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        overrides = self.__dict__["_overrides"]
        if name in overrides:
            return overrides[name]
        return getattr(self.__dict__["_inner"], name)


def test_live_correction_suppression_locks_in_regression_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite=CORRECTION_SUPPRESSION_SUITE_KEY)

    assert report["status"] == "pass"
    suite = report["suites"][0]
    metrics = suite["metrics"]
    assert suite["status"] == "pass"
    assert metrics["backend"] == "sqlite"
    assert metrics["pre_correction_visibility"] == 1.0  # non-vacuous: A ranked before correction
    assert metrics["suppression_rate"] == 1.0
    assert metrics["replacement_recall_at_5"] == 1.0
    assert metrics["audit_completeness"] == 1.0
    for case in suite["cases"]:
        evidence = case["evidence"]
        assert case["status"] == "pass"
        # A surfaced pre-correction, then vanished from every probe.
        assert evidence["original_memory_key"] in evidence["pre_correction_top_keys"]
        assert evidence["original_memory_key"] not in evidence["post_correction_top_keys"]
        assert evidence["original_memory_key"] not in evidence["old_probe_top_keys"]
        assert evidence["rejected_memory_key"] not in evidence["post_correction_top_keys"]
        assert evidence["rejected_memory_key"] not in evidence["reject_probe_top_keys"]
        # The supersession on A points at its replacement.
        assert evidence["replacement_memory_key"] in str(evidence["superseded_revision_reason"])


def test_live_decision_recovery_measures_recall_and_filter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite=DECISION_RECOVERY_SUITE_KEY)

    assert report["status"] == "pass"
    suite = report["suites"][0]
    metrics = suite["metrics"]
    assert suite["status"] == "pass"
    assert metrics["backend"] == "sqlite"
    assert metrics["decision_recall_at_5"] >= 0.8
    assert metrics["target_checks"]["decision_recall_at_5"] == "pass"
    filter_state = metrics["memory_types_filter"]
    if retrieval_request_supports_memory_types():
        # The sibling workstream's filter parameter has landed: both the
        # unfiltered and filtered variants must be measured and reported.
        assert filter_state["available"] is True
        assert metrics["filtered_decision_recall_at_5"] >= 0.8
        assert metrics["target_checks"]["filtered_decision_recall_at_5"] == "pass"
    else:
        assert filter_state["available"] is False
        assert "TODO" in str(filter_state["note"])
        assert "filtered_decision_recall_at_5" not in metrics["target_checks"]


def test_decision_recovery_reports_static_incompatible_filter_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "UNIQUE_EVAL_FILTER_EXCEPTION_SENTINEL"

    def incompatible_filter(*_args: object, **_kwargs: object) -> object:
        raise TypeError(sentinel)

    monkeypatch.setattr(vnext_evals_module, "filtered_retrieval_fn", incompatible_filter)

    with _live_sqlite_store() as store:
        suite = run_decision_recovery_eval(store)

    filter_state = suite["metrics"]["memory_types_filter"]
    assert filter_state == {
        "available": False,
        "note": vnext_evals_module.MEMORY_TYPES_FILTER_INCOMPATIBLE_NOTE,
    }
    assert sentinel not in json.dumps(suite)


def test_live_provenance_explanation_audits_real_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite=PROVENANCE_EXPLANATION_SUITE_KEY)

    assert report["status"] == "pass"
    suite = report["suites"][0]
    metrics = suite["metrics"]
    assert suite["status"] == "pass"
    assert metrics["backend"] == "sqlite"
    assert metrics["explain_completeness_rate"] == 1.0
    assert metrics["orphan_provenance_count"] == 0
    assert metrics["provenance_link_count"] >= metrics["audited_memory_count"]
    assert metrics["corrected_memory_count"] >= 1
    corrected_cases = [case for case in suite["cases"] if "correction_reflected" in case["checks"]]
    assert len(corrected_cases) == metrics["corrected_memory_count"]
    for case in corrected_cases:
        assert case["checks"]["correction_reflected"] == "pass"
        assert "corrected" in case["evidence"]["revision_types"]
        assert "agent.memory_corrected" in case["evidence"]["event_types"]
    for case in suite["cases"]:
        assert "agent.memory_committed" in case["evidence"]["event_types"]
        assert any(reason.strip() for reason in case["evidence"]["revision_reasons"])


def test_live_all_suites_execute_against_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite="all")

    assert report["status"] == "pass"
    assert report["summary"]["executed_suite_count"] == len(VNEXT_EVAL_SUITE_ORDER)
    assert report["summary"]["skipped_suite_count"] == 0
    assert [suite["suite_key"] for suite in report["suites"]] == list(VNEXT_EVAL_SUITE_ORDER)
    assert all(suite["status"] == "pass" for suite in report["suites"])


def test_live_all_suites_file_run_persists_no_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "eval_all.db"
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, f"sqlite:///{db_path}")

    report = run_vnext_evals(suite="all")

    assert report["status"] == "pass"
    conn = sqlite3.connect(str(db_path))
    try:
        for table in ("users", "memories", "event_log", "sources", "memory_revisions", "provenance_links"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        conn.close()


def test_seeded_decision_rows_pin_explicit_status_and_validity() -> None:
    # A sibling workstream is adding staleness demotion to the search SQL;
    # seeded rows must carry explicit active status and far-future validity
    # so that change cannot silently demote them.
    with _live_sqlite_store() as store:
        suite = run_decision_recovery_eval(store)

        assert suite["status"] == "pass"
        assert suite["metrics"]["backend"] == "sqlite"  # injected stores are still labelled
        seeded = [
            row
            for row in store.list_memories(status=None)
            if str(row["memory_key"]).startswith(DECISION_MEMORY_KEY_PREFIX)
        ]
        assert seeded
        assert all(row["status"] == "active" for row in seeded)
        assert all(str(row["valid_to"]).startswith(VNEXT_EVAL_FIXED_VALID_TO[:10]) for row in seeded)


# --------------------------------------------------------------------------
# The suites can genuinely FAIL: break one production behavior at a time
# through a delegating store wrapper and watch the right metric collapse.
# --------------------------------------------------------------------------


def test_correction_suppression_fails_when_status_transitions_are_lost() -> None:
    # Simulates a store regression where supersede/reject no longer demote
    # the memory status: the stale memory keeps surfacing and the audit
    # trail no longer shows a superseded row.
    with _live_sqlite_store() as store:
        def update_memory_dropping_status(*, memory_id: str, patch: dict, actor_type: str = "system") -> dict:
            stripped = {key: value for key, value in patch.items() if key != "status"}
            if not stripped:
                return store.get_memory(memory_id)
            return store.update_memory(memory_id=memory_id, patch=stripped, actor_type=actor_type)

        broken = _OverrideStore(store, update_memory=update_memory_dropping_status)
        suite = run_correction_suppression_eval(broken)

        assert suite["status"] == "fail"
        assert suite["metrics"]["suppression_rate"] < 1.0
        assert suite["metrics"]["target_checks"]["suppression_rate"] == "fail"
        assert suite["metrics"]["audit_completeness"] < 1.0


def test_decision_recovery_fails_when_retrieval_goes_blind() -> None:
    with _live_sqlite_store() as store:
        broken = _OverrideStore(
            store,
            search_memories_fts=lambda **_kwargs: [],
            search_memories=lambda **_kwargs: [],
        )
        suite = run_decision_recovery_eval(broken)

        assert suite["status"] == "fail"
        assert suite["metrics"]["decision_recall_at_5"] == 0.0
        assert suite["metrics"]["target_checks"]["decision_recall_at_5"] == "fail"


def test_provenance_explanation_fails_when_revisions_disappear() -> None:
    with _live_sqlite_store() as store:
        broken = _OverrideStore(store, list_revisions=lambda memory_id: [])
        suite = run_provenance_explanation_eval(broken)

        assert suite["status"] == "fail"
        assert suite["metrics"]["explain_completeness_rate"] == 0.0
        assert suite["metrics"]["target_checks"]["explain_completeness_rate"] == "fail"


def test_provenance_explanation_fails_on_orphaned_provenance_links() -> None:
    with _live_sqlite_store() as store:
        broken = _OverrideStore(store, get_source=lambda source_id: None)
        suite = run_provenance_explanation_eval(broken)

        assert suite["status"] == "fail"
        assert suite["metrics"]["orphan_provenance_count"] > 0
        assert suite["metrics"]["target_checks"]["orphan_provenance_count"] == "fail"


# ---------------------------------------------------------------------------
# Sprint D suites: entity_resolution and graph_hop_retrieval
# ---------------------------------------------------------------------------

from alicebot_api.vnext_evals import (  # noqa: E402
    ENTITY_RESOLUTION_SUITE_KEY,
    GRAPH_HOP_RETRIEVAL_SUITE_KEY,
    run_entity_resolution_eval,
    run_graph_hop_retrieval_eval,
)


def test_suite_order_includes_sprint_d_suites() -> None:
    assert ENTITY_RESOLUTION_SUITE_KEY in VNEXT_EVAL_SUITE_ORDER
    assert GRAPH_HOP_RETRIEVAL_SUITE_KEY in VNEXT_EVAL_SUITE_ORDER


def test_sprint_d_suites_skip_without_live_store() -> None:
    for runner, key in (
        (run_entity_resolution_eval, ENTITY_RESOLUTION_SUITE_KEY),
        (run_graph_hop_retrieval_eval, GRAPH_HOP_RETRIEVAL_SUITE_KEY),
    ):
        suite = runner(None)
        assert suite["suite_key"] == key
        assert suite["status"] == "skipped"
        assert suite["reason"]


def test_live_entity_resolution_passes_through_real_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite=ENTITY_RESOLUTION_SUITE_KEY)

    assert report["status"] == "pass"
    suite = report["suites"][0]
    metrics = suite["metrics"]
    assert metrics["resolution_rate"] == 1.0
    assert metrics["noise_entity_count"] == 0
    assert metrics["alias_growth_rate"] == 1.0
    assert metrics["mention_accuracy"] == 1.0
    assert metrics["backend"] == "sqlite"


def test_entity_resolution_fails_when_blocklist_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api import vnext_entities

    monkeypatch.setattr(vnext_entities, "ENTITY_EXTRACTION_BLOCKLIST", frozenset())
    with _live_sqlite_store() as store:
        suite = run_entity_resolution_eval(store)

    assert suite["metrics"]["noise_entity_count"] > 0
    assert suite["metrics"]["target_checks"]["noise_entity_count"] == "fail"
    assert suite["status"] == "fail"


def test_live_graph_hop_retrieval_measures_the_multi_session_mechanism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")

    report = run_vnext_evals(suite=GRAPH_HOP_RETRIEVAL_SUITE_KEY)

    assert report["status"] == "pass"
    suite = report["suites"][0]
    metrics = suite["metrics"]
    assert metrics["graph_recall_at_5"] == 1.0
    assert metrics["fts_only_recall_at_5"] == 0.0
    assert metrics["graph_lift"] == 1.0
    assert metrics["winner_graph_rank_rate"] == 1.0
    for case in suite["cases"]:
        assert case["status"] == "pass"
        assert "graph" in case["evidence"]["winner_stage_ranks"]
        control_stage = case["evidence"]["control_graph_stage"]
        assert str(control_stage.get("status", "")).startswith("disabled")


def test_graph_hop_retrieval_fails_without_entity_links(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.vnext_entities import EntityLinkingService

    monkeypatch.setattr(
        EntityLinkingService,
        "link_entities_for_memory",
        lambda self, **kwargs: [],
    )
    with _live_sqlite_store() as store:
        suite = run_graph_hop_retrieval_eval(store)

    assert suite["metrics"]["graph_recall_at_5"] < 0.8
    assert suite["metrics"]["target_checks"]["graph_recall_at_5"] == "fail"
    assert suite["status"] == "fail"


# --------------------------------------------------------------------------
# Calendar independence: no suite result may depend on the day it runs.
#
# The retrieval service resolves a yearless date in a query ("in September")
# against the request's reference time, and against the wall clock when the
# request has none. From 2026-10-01 that moved correction-005's replacement to
# rank 2 and failed the release-check tests on every branch. These tests run
# the real suites under a process clock set to several dates.
# --------------------------------------------------------------------------

_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Dates picked to straddle the failure: before the corpus epoch, the last day
# the suite happened to pass, the first day it moved, and a distant month.
_PROCESS_CLOCK_STARTS = (
    datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
    datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc),
)

# Modules on the eval path that read the wall clock. The patch below also
# covers any other alicebot_api module that binds the datetime class, and the
# test asserts these are among them so a rename cannot make it vacuous.
_EVAL_CLOCK_MODULES = (
    "alicebot_api.vnext_event_log",
    "alicebot_api.vnext_memory_commit",
    "alicebot_api.vnext_retrieval",
    "alicebot_api.vnext_stores.sqlite.primitives",
)


class _FakeDatetimeMeta(type(datetime)):
    """Keeps ``isinstance(real_datetime, FakeDatetime)`` true for patched modules."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


def _install_process_clock(monkeypatch: pytest.MonkeyPatch, start: datetime) -> list[str]:
    """Make ``datetime.now()`` in every alicebot_api module start at ``start``.

    The clock advances one millisecond per read, so rows written in sequence
    keep the strictly increasing timestamps a real run gives them (a frozen
    clock would turn every recency tiebreak into a coin flip on random ids).
    """
    state = {"next": start}

    def read() -> datetime:
        current = state["next"]
        state["next"] = current + timedelta(milliseconds=1)
        return current

    class FakeDatetime(datetime, metaclass=_FakeDatetimeMeta):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            current = read()
            return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> datetime:
            return read().replace(tzinfo=None)

    patched = [
        name
        for name, module in list(sys.modules.items())
        if name.startswith("alicebot_api") and getattr(module, "datetime", None) is datetime
    ]
    for name in patched:
        monkeypatch.setattr(sys.modules[name], "datetime", FakeDatetime)
    return patched


def _calendar_stable_view(value: object) -> object:
    """A report minus what legitimately differs run to run: timing and random ids."""
    if isinstance(value, dict):
        return {
            key: _calendar_stable_view(child)
            for key, child in value.items()
            if "latency" not in str(key)
            and not (isinstance(child, str) and _UUID_TEXT.match(child))
        }
    if isinstance(value, list):
        return [_calendar_stable_view(child) for child in value]
    return value


def _run_all_suites_at(start: datetime) -> dict[str, object]:
    with pytest.MonkeyPatch.context() as local:
        local.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")
        patched = _install_process_clock(local, start)
        assert set(_EVAL_CLOCK_MODULES) <= set(patched)
        from alicebot_api.vnext_stores.sqlite import primitives

        # The fake clock is really the one the stores stamp rows with.
        assert primitives._utc_now_iso().startswith(start.date().isoformat())
        return run_vnext_evals(suite="all")


def test_suite_results_do_not_depend_on_the_day_they_run() -> None:
    views = {}
    for start in _PROCESS_CLOCK_STARTS:
        report = _run_all_suites_at(start)
        assert report["status"] == "pass", start
        views[start.date().isoformat()] = _calendar_stable_view(report["suites"])

    reference_day, reference_view = next(iter(views.items()))
    for day, view in views.items():
        assert view == reference_view, f"suite results on {day} differ from {reference_day}"

    # The numbers the suite produced on 2026-09-30, pinned so that the
    # clocks agreeing with each other cannot mean they agree on a wrong value.
    correction = next(
        suite
        for suite in reference_view
        if suite["suite_key"] == CORRECTION_SUPPRESSION_SUITE_KEY
    )
    assert {
        key: correction["metrics"][key]
        for key in (
            "pre_correction_visibility",
            "suppression_rate",
            "replacement_recall_at_5",
            "replacement_mrr",
            "audit_completeness",
        )
    } == {
        "pre_correction_visibility": 1.0,
        "suppression_rate": 1.0,
        "replacement_recall_at_5": 1.0,
        "replacement_mrr": 1.0,
        "audit_completeness": 1.0,
    }
    assert [case["metrics"]["replacement_reciprocal_rank"] for case in correction["cases"]] == [1.0] * 6


def test_every_eval_retrieval_request_carries_the_fixed_reference_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")
    seen: list[VNextRetrievalRequest] = []
    original = VNextRetrievalService.compile_context_pack

    def spy(self: VNextRetrievalService, request: VNextRetrievalRequest) -> dict[str, object]:
        seen.append(request)
        return original(self, request)

    monkeypatch.setattr(VNextRetrievalService, "compile_context_pack", spy)

    report = run_vnext_evals(suite="all")

    assert report["status"] == "pass"
    # Every live suite issued retrieval (queries, probes, filtered variants,
    # graph control runs), and none of those requests left the clock to chance.
    assert len(seen) >= 100
    assert {request.reference_time for request in seen} == {VNEXT_EVAL_REFERENCE_TIME}


def _eval_query_texts() -> list[str]:
    retrieval = generate_vnext_benchmark_corpus()
    correction = generate_correction_suppression_corpus()
    decision = generate_decision_recovery_corpus()
    graph = generate_graph_hop_corpus()
    texts = [str(query["query"]) for query in retrieval["queries"]]
    for case in correction["cases"]:
        texts.extend(str(case[key]) for key in ("query", "old_probe", "reject_probe"))
    texts.extend(str(query["query"]) for query in decision["queries"])
    texts.extend(str(group["query"]) for group in graph["groups"])
    return texts


def test_reference_time_is_the_corpus_epoch_and_no_query_window_reaches_it() -> None:
    epoch = datetime.fromisoformat(VNEXT_EVAL_FIXED_VALID_FROM.replace("Z", "+00:00"))
    assert VNEXT_EVAL_REFERENCE_TIME == epoch
    assert VNEXT_EVAL_REFERENCE_TIME.tzinfo is not None

    anchored = {}
    for text in _eval_query_texts():
        anchor = parse_temporal_anchor(text, reference_time=VNEXT_EVAL_REFERENCE_TIME)
        if anchor is None:
            continue
        anchored[text] = anchor
        # An anchor window that reaches the epoch would overlap the seeded
        # rows' validity interval, and its hits would then depend on which
        # day the wall-clock-stamped commit rows were written. Reword the
        # query or pick a window before the epoch.
        assert anchor.window_end <= epoch, f"{text!r} resolves to a window that reaches the corpus epoch"

    # Non-vacuous: the two date-bearing queries in the corpus are still parsed
    # (so this guard sees them) and both land in 2025.
    assert anchored["who is the Sable data vendor contract with in September"].window_start == datetime(
        2025, 9, 1, tzinfo=timezone.utc
    )
    assert anchored["Meridian launch window March 14"].window_start == datetime(
        2025, 3, 14, tzinfo=timezone.utc
    )
    assert len(anchored) == 2
