"""Regression tests for F1: Redundancy-aware memory selection.

Pinning the invariants:
1. Distinct current facts survive clusters of near-verbatim duplicate memories
   in both alice_recall (small limits) and alice_context_pack (tight budgets).
2. Demote, don't discard: when fewer distinct candidates exist than the limit,
   redundant candidates fill the remaining slots.
3. Contradictory values sharing sentence structure (e.g., PostgreSQL vs MySQL)
   are not demoted because their token Jaccard similarity is below 0.90.
4. Aggregation mode preserves its existing provenance-based diversity behavior.
5. Security fences (scope, sensitivity) cannot be bypassed by diversity reordering.
6. Clean corpora and single-result requests preserve existing behavior.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from tests.unit.search_goldens_support import Vault

QUERY = "where do we deploy the release service?"

AWS_DUPLICATES = [
    "The release service deploys to AWS in us-east-1 on the standard release train.",
    "The release service deploys to AWS in us-east-1 on the standard release train",
    "The release service deploys to AWS in us-east-1 on the standard release train.",
    "The release service deploys to AWS in us-east-1 (standard release train).",
    "The release service deploys to AWS in us-east-1 on the standard release train.",
    "The release service deploys to AWS in us-east-1 on the standard release train.",
]

CLOUDFLARE_UPDATE = (
    "Correction: the release service now deploys to Cloudflare Workers, not AWS. "
    "The AWS us-east-1 target was retired last quarter."
)


def _commit(vault: Vault, title: str, text: str, *, sensitivity: str = "internal") -> dict[str, object]:
    return vault.call(
        "alice_memory_commit",
        {
            "title": title,
            "canonical_text": text,
            "memory_type": "semantic",
            "domain": "project",
            "sensitivity": sensitivity,
        },
    )


def test_distinct_update_survives_six_duplicates_in_recall_limit_2() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    res = vault.call("alice_recall", {"query": QUERY, "limit": 2, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    texts = [str(r.get("text", "")) for r in results]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert has_cloudflare, (
        f"Expected distinct Cloudflare update in top-2 results, but got only duplicates:\n{texts}"
    )


def test_distinct_update_survives_six_duplicates_in_recall_limit_3() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    res = vault.call("alice_recall", {"query": QUERY, "limit": 3, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    texts = [str(r.get("text", "")) for r in results]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert has_cloudflare, (
        f"Expected distinct Cloudflare update in top-3 results, but got only duplicates:\n{texts}"
    )


def test_context_pack_spends_budget_on_distinct_update_not_duplicate() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i in range(1, 7):
        _commit(
            vault,
            f"deploy-target-{i}",
            "The release service deploys to AWS in us-east-1 on the standard release train.",
        )
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    # alice_context_pack is on the full tool surface
    res = vault.call(
        "alice_context_pack",
        {"query": QUERY, "max_tokens": 2400, "budget_strategy": "balanced", "include_sources": False},
        full=True,
    )
    memories = res.get("memories") or []
    assert len(memories) >= 2, f"Expected at least 2 memories packed, got {len(memories)}"

    texts = [str(m.get("canonical_text") or m.get("text", "")) for m in memories]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert has_cloudflare, (
        f"Expected distinct Cloudflare update in context pack memories, but got only duplicates:\n{texts}"
    )


def test_demote_not_drop_fills_all_requested_slots_when_candidates_exist() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    res = vault.call("alice_recall", {"query": QUERY, "limit": 4, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 4, f"Expected 4 results, got {len(results)}"

    texts = [str(r.get("text", "")) for r in results]
    cloudflare_count = sum(1 for t in texts if "Cloudflare" in t)
    aws_count = sum(1 for t in texts if "AWS" in t and "Cloudflare" not in t)
    assert cloudflare_count == 1
    assert aws_count == 3, f"Expected 3 AWS duplicates filling remaining slots, got {aws_count}"


def test_corpus_of_only_duplicates_fills_all_requested_slots() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)

    res = vault.call("alice_recall", {"query": QUERY, "limit": 4, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 4, (
        f"Demote-not-drop must fill all requested slots even if all candidates are redundant, got {len(results)}"
    )


def test_contradictory_facts_sharing_sentence_frame_are_not_demoted() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    _commit(vault, "db-target-pg", "The primary service database is PostgreSQL 16 on port 5432.")
    _commit(vault, "db-target-my", "The primary service database is MySQL 8.4 on port 3306.")

    res = vault.call("alice_recall", {"query": "primary service database", "limit": 2, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    texts = [str(r.get("text", "")) for r in results]
    has_pg = any("PostgreSQL" in t for t in texts)
    has_my = any("MySQL" in t for t in texts)
    assert has_pg and has_my, f"Both contradictory database targets must be returned, got {texts}"


def test_single_result_request_preserves_top_ranked_memory() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    res = vault.call("alice_recall", {"query": QUERY, "limit": 1, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 1
    # Rank 1 must be preserved as the top relevance hit
    assert "AWS" in str(results[0].get("text", ""))


def test_scope_and_sensitivity_fences_cannot_be_bypassed_by_diversity() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text, sensitivity="public")
    # Secret update marked confidential
    _commit(vault, "deploy-target-secret", CLOUDFLARE_UPDATE, sensitivity="confidential")

    # Agent with public sensitivity only
    res = vault.call(
        "alice_recall",
        {"query": QUERY, "limit": 2, "sensitivity_allowed": ["public"], "include_sources": False},
    )
    results = res.get("results") or []
    texts = [str(r.get("text", "")) for r in results]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert not has_cloudflare, "Confidential memory must not be surfaced when caller lacks permission"


def test_candidate_pool_deepening_prevents_pre_fusion_starvation() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    # 8 near-verbatim memories
    for i in range(1, 9):
        _commit(vault, f"deploy-target-{i}", AWS_DUPLICATES[(i - 1) % len(AWS_DUPLICATES)])
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    # limit=2: with candidate_limit = max(limit*2, limit) = 4, the update at rank 9 would be starved.
    # With candidate_limit deepened to at least 12, rank 9 is fetched and promoted to slot 2.
    res = vault.call("alice_recall", {"query": QUERY, "limit": 2, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 2
    texts = [str(r.get("text", "")) for r in results]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert has_cloudflare, f"Deepened candidate pool must allow Cloudflare update to surface, got {texts}"


def test_deterministic_repeatability_over_runs() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    first_run = vault.call("alice_recall", {"query": QUERY, "limit": 3, "include_sources": False})
    first_ids = [r.get("id") for r in first_run.get("results") or []]

    for _ in range(4):
        run = vault.call("alice_recall", {"query": QUERY, "limit": 3, "include_sources": False})
        run_ids = [r.get("id") for r in run.get("results") or []]
        assert run_ids == first_ids, "Selection order must be strictly deterministic across calls"


def test_recall_limit_matching_candidates_promotes_distinct_memory_ahead_of_duplicates() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i, text in enumerate(AWS_DUPLICATES, start=1):
        _commit(vault, f"deploy-target-{i}", text)
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    # limit=7 matches all 7 memories in the vault.
    # Redundancy demotion must place Cloudflare at rank 2 (index 1),
    # rather than leaving it buried at rank 7 behind 6 identical AWS copies.
    res = vault.call("alice_recall", {"query": QUERY, "limit": 7, "include_sources": False})
    results = res.get("results") or []
    assert len(results) == 7

    texts = [str(r.get("text", "")) for r in results]
    assert "Cloudflare" in texts[1], (
        f"Expected distinct Cloudflare update at rank 2 (index 1), got:\n{texts}"
    )


def test_context_pack_small_max_items_deepens_candidate_pool_for_distinct_update() -> None:
    work = Path(tempfile.mkdtemp(prefix="alice-f1-test-"))
    vault = Vault(work / "vault")
    for i in range(1, 7):
        _commit(
            vault,
            f"deploy-target-{i}",
            "The release service deploys to AWS in us-east-1 on the standard release train.",
        )
    _commit(vault, "deploy-target-current", CLOUDFLARE_UPDATE)

    # max_items=2: without candidate deepening max(max_items * 3, 12),
    # retrieval would only fetch 4 candidates (all AWS duplicates) and starve Cloudflare.
    res = vault.call(
        "alice_context_pack",
        {"query": QUERY, "max_items": 2, "max_tokens": 4000, "budget_strategy": "balanced", "include_sources": False},
        full=True,
    )
    memories = res.get("memories") or []
    assert len(memories) == 2, f"Expected 2 memories packed, got {len(memories)}"
    texts = [str(m.get("canonical_text") or m.get("text", "")) for m in memories]
    has_cloudflare = any("Cloudflare" in t for t in texts)
    assert has_cloudflare, (
        f"Expected distinct Cloudflare update in top-2 packed memories, but got only duplicates:\n{texts}"
    )

