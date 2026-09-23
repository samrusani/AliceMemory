#!/usr/bin/env python3
"""Fail-closed metadata, Git, and distribution checks for public releases."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import Parser
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


ROOT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DESCRIPTION_RELATIVE_PATH = "docs/pypi-description.md"
_PACKAGE_DESCRIPTION_VERSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_PACKAGE_DESCRIPTION_STATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "latest-version claim",
        re.compile(r"\blatest\s+(?:published\s+)?release\b", re.IGNORECASE),
    ),
    ("candidate state", re.compile(r"\bcandidate\b", re.IGNORECASE)),
    (
        "release-gating state",
        re.compile(r"\brelease[ -]?gating\b", re.IGNORECASE),
    ),
    (
        "publication state",
        re.compile(r"\b(?:unpublished|publication\s+pending)\b", re.IGNORECASE),
    ),
)
SEMANTIC_EVAL_ATTESTATION_SCHEMA_VERSION = "alice_semantic_eval_attestation_v1"
EMBEDDING_SIGNATURE_IDENTITY_SCHEMA_VERSION = "alice_embedding_signature_identity_v1"
REQUIRED_EMBEDDING_SIGNATURE_VERSION = 2
RELEASE_DOCUMENT_STATE_SCHEMA_VERSION = "alice_release_document_state_v1"
_RELEASE_DOCUMENT_STATE_PATTERN = re.compile(
    r"<!-- alice-release-state: (?P<payload>\{.*\}) -->",
)
SEMANTIC_EVAL_REQUIRED_SUITES = (
    "retrieval_quality",
    "correction_suppression",
    "decision_recovery",
    "provenance_explanation",
    "entity_resolution",
    "graph_hop_retrieval",
)
_CREDENTIAL_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "client_secret",
    "connection_string",
    "credential",
    "database_url",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "access_token",
    "secret",
    "session_key",
    "token",
)
_CREDENTIAL_VALUE_PATTERN = re.compile(
    r"(?i)(?:"
    r"bearer\s+\S+|"
    r"\bsk-[a-z0-9_-]{8,}|"
    r"\bhf_[a-z0-9]{16,}|"
    r"\bgh[pousr]_[a-z0-9]{20,}|"
    r"\bxox(?:a|b|p|r|s)-[a-z0-9-]{10,}|"
    r"\bAIza[0-9A-Za-z_-]{20,}|"
    r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token)"
    r"\s*[:=]\s*[^\s,;]{4,}|"
    r"postgres(?:ql)?://[^/\s]*@|"
    r"https?://[^/\s]*:[^@\s]*@"
    r")"
)
_RAW_URL_PATTERN = re.compile(r"(?i)\bhttps?://")
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
_ENDPOINT_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{16}")
_REPORT_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_GRAPH_WINNER_STAGE_KEYS = frozenset({"fts", "vector", "graph", "temporal_anchor"})
_RETRIEVAL_EVIDENCE_LIMIT = 5
_RETRIEVAL_RECALL_LIMIT = 10
# Cases display five keys, but reciprocal rank is computed over all ten retrieved keys.
_OFF_WINDOW_RECIPROCAL_RANKS = frozenset(
    (
        0.0,
        *(
            1.0 / rank
            for rank in range(_RETRIEVAL_EVIDENCE_LIMIT + 1, _RETRIEVAL_RECALL_LIMIT + 1)
        ),
    )
)

SEMANTIC_EVAL_CANONICAL_TARGETS: dict[str, dict[str, dict[str, object]]] = {
    "retrieval_quality": {
        "lexical_overlap_recall_at_5": {"minimum": 0.80},
        "lexical_overlap_mrr": {"minimum": 0.60},
        "paraphrase_recall_at_5": {
            "minimum": 0.70,
            "enforced_only_when_vector_stage_enabled": True,
        },
    },
    "correction_suppression": {
        "pre_correction_visibility": {"minimum": 1.0},
        "suppression_rate": {"minimum": 1.0},
        "replacement_recall_at_5": {"minimum": 0.80},
        "audit_completeness": {"minimum": 1.0},
    },
    "decision_recovery": {
        "decision_recall_at_5": {"minimum": 0.80},
        "filtered_decision_recall_at_5": {
            "minimum": 0.80,
            "enforced_only_when_memory_types_filter_available": True,
        },
    },
    "provenance_explanation": {
        "explain_completeness_rate": {"minimum": 1.0},
        "orphan_provenance_count": {"maximum": 0},
    },
    "entity_resolution": {
        "resolution_rate": {"minimum": 0.90},
        "noise_entity_count": {"maximum": 0},
        "mention_accuracy": {"minimum": 1.0},
        "alias_growth_rate": {"minimum": 1.0},
    },
    "graph_hop_retrieval": {
        "graph_recall_at_5": {"minimum": 0.80},
        "graph_lift": {"minimum": 0.31},
    },
}

SEMANTIC_EVAL_CANONICAL_CASE_KEYS: dict[str, tuple[str, ...]] = {
    "retrieval_quality": tuple(
        [f"lexical-{index:03d}" for index in range(1, 33)]
        + [f"paraphrase-{index:03d}" for index in range(1, 17)]
    ),
    "correction_suppression": tuple(
        f"correction-{index:03d}" for index in range(1, 7)
    ),
    "decision_recovery": tuple(
        f"decision-query-{index:03d}" for index in range(1, 11)
    ),
    "provenance_explanation": tuple(
        f"provenance-{index:03d}" for index in range(1, 7)
    ),
    "entity_resolution": ("person-sami", "org-meridian", "org-alice-core"),
    "graph_hop_retrieval": (
        "hop-meridian",
        "hop-northwind",
        "hop-aurora",
        "hop-halcyon",
        "hop-verdant",
    ),
}

SEMANTIC_EVAL_CANONICAL_CORPUS_DIGESTS = {
    "retrieval_quality": "sha256:3838f99bf58674e7af1a8f42eefe3a0247d5bee8604d0e3dd359c3e68282439d",
    "correction_suppression": "sha256:96ec73e5ee4105d924663a9c7f2360c731a0c83546ef761359ef8870eb83c6c3",
    "decision_recovery": "sha256:c09a71a13b935a874d85321d9f334a1250f2dce693de2a2da0d73729ee6095a1",
    "provenance_explanation": "sha256:35ca3444929f1f883c341ec422d182abd8feb32a2e1bded14b7d4bef218b5ff1",
    # 2026-09-23: re-pinned after the corpus replaced the owner's real name with
    # "Jane Doe" (public-repo hygiene). Same cases, same expected outcomes.
    "entity_resolution": "sha256:68aac32a78d98fbb12048f7ba7be731c791a2abc18035f9b0c4f70380b96808f",
    "graph_hop_retrieval": "sha256:4eda30203183a55ef8326ddaf33cc62974b158bbdc0601ebc541f6614b2de605",
}

_REPORT_TOP_LEVEL_KEYS = {
    "schema_version",
    "generated_at",
    "suite",
    "status",
    "release_gate",
    "embedding_signature",
    "targets",
    "corpus",
    "skipped_suites",
    "summary",
    "suites",
    "report_digest",
}
_SUMMARY_KEYS = {
    "status",
    "suite_count",
    "executed_suite_count",
    "skipped_suite_count",
    "case_count",
    "passed_case_count",
    "failed_case_count",
    "pass_rate",
    "suite_order",
}
_SUITE_KEYS = {"suite_key", "title", "status", "targets", "metrics", "cases"}
_CASE_KEYS: dict[str, set[str]] = {
    suite_key: {"case_key", "status", "metrics", "evidence"}
    for suite_key in SEMANTIC_EVAL_REQUIRED_SUITES
}
_CASE_KEYS["retrieval_quality"].add("subset")
_CASE_KEYS["provenance_explanation"].add("checks")

_SUITE_METRIC_KEYS: dict[str, set[str]] = {
    "retrieval_quality": {
        "backend",
        "query_count",
        "recall_at_1",
        "recall_at_5",
        "mrr",
        "latency_ms",
        "vector_stages",
        "vector_candidate_count",
        "vector_query_count",
        "vector_queries_with_candidates",
        "vector_stage_participated",
        "retrieval_mode",
        "paraphrase_targets_enforced",
        "release_gate",
        "subsets",
        "target_checks",
        "seeding",
    },
    "correction_suppression": {
        "backend",
        "case_count",
        "distractor_count",
        "pre_correction_visibility",
        "suppression_rate",
        "replacement_recall_at_5",
        "replacement_mrr",
        "audit_completeness",
        "corpus_digest",
        "target_checks",
    },
    "decision_recovery": {
        "backend",
        "query_count",
        "decision_count",
        "distractor_count",
        "decision_recall_at_1",
        "decision_recall_at_5",
        "decision_mrr",
        "memory_types_filter",
        "corpus_digest",
        "filtered_decision_recall_at_5",
        "filtered_decision_mrr",
        "target_checks",
    },
    "provenance_explanation": {
        "backend",
        "audited_memory_count",
        "corrected_memory_count",
        "explain_completeness_rate",
        "provenance_link_count",
        "orphan_provenance_count",
        "corpus_digest",
        "target_checks",
    },
    "entity_resolution": {
        "backend",
        "group_count",
        "entity_count",
        "resolution_rate",
        "mention_accuracy",
        "alias_growth_rate",
        "noise_entity_count",
        "noise_entities",
        "corpus_digest",
        "target_checks",
    },
    "graph_hop_retrieval": {
        "backend",
        "group_count",
        "graph_recall_at_5",
        "fts_only_recall_at_5",
        "graph_lift",
        "winner_graph_rank_rate",
        "control_mechanism",
        "corpus_digest",
        "target_checks",
    },
}

_CASE_METRIC_KEYS: dict[str, set[str]] = {
    "retrieval_quality": {"recall_at_1", "recall_at_5", "reciprocal_rank", "latency_ms"},
    "correction_suppression": {
        "pre_correction_visible",
        "suppressed",
        "replacement_recall_at_5",
        "replacement_reciprocal_rank",
        "audit_complete",
    },
    "decision_recovery": {
        "recall_at_1",
        "recall_at_5",
        "reciprocal_rank",
        "filtered_recall_at_5",
        "filtered_reciprocal_rank",
    },
    "provenance_explanation": {"explain_complete"},
    "entity_resolution": {"resolved", "mention_count"},
    "graph_hop_retrieval": {
        "graph_recall_at_5",
        "fts_recall_at_5",
        "winner_has_graph_rank",
    },
}

_CASE_EVIDENCE_KEYS: dict[str, set[str]] = {
    "retrieval_quality": {
        "query",
        "expected_memory_key",
        "top_memory_keys",
        "vector_stage",
        "vector_candidate_count",
    },
    "correction_suppression": {
        "query",
        "original_memory_key",
        "replacement_memory_key",
        "rejected_memory_key",
        "pre_correction_top_keys",
        "post_correction_top_keys",
        "old_probe_top_keys",
        "reject_probe_top_keys",
        "superseded_revision_reason",
        "commit_flow_notes",
    },
    "decision_recovery": {
        "query",
        "expected_memory_key",
        "top_memory_keys",
        "filtered_top_memory_keys",
    },
    "provenance_explanation": {
        "memory_id",
        "commit_status",
        "revision_types",
        "revision_reasons",
        "event_types",
        "provenance_link_count",
        "resolved_link_count",
        "orphan_link_count",
    },
    "entity_resolution": {"canonical_normalized", "entity_id", "aliases"},
    "graph_hop_retrieval": {
        "query",
        "expected_memory_key",
        "graph_top_keys",
        "fts_top_keys",
        "winner_stage_ranks",
        "control_graph_stage",
    },
}

_ATTESTATION_KEYS = {
    "schema_version",
    "source_sha",
    "report_file",
    "report_sha256",
    "report_digest",
    "generated_at",
    "suite",
    "status",
    "embedding_signature",
    "backend",
    "retrieval_mode",
    "vector_candidate_count",
    "vector_stage_participated",
    "paraphrase_recall_at_5",
    "credentials_included",
}


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    distribution_name: str
    version: str
    web_version: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def _payload_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _credential_material_paths(payload: object, *, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}"
            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if any(fragment in normalized_key for fragment in _CREDENTIAL_KEY_FRAGMENTS):
                issues.append(child_path)
            issues.extend(_credential_material_paths(value, path=child_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            issues.extend(_credential_material_paths(value, path=f"{path}[{index}]"))
    elif isinstance(payload, str) and _CREDENTIAL_VALUE_PATTERN.search(payload):
        issues.append(path)
    return issues


def _validate_embedding_signature_identity(
    value: object,
    *,
    context: str,
) -> list[str]:
    """Validate the non-secret identity of the vectors used by an eval."""
    if not isinstance(value, dict):
        return [f"{context} embedding_signature must be a JSON object"]
    required_keys = {
        "schema_version",
        "signature_version",
        "provider",
        "provider_fingerprint",
        "model",
        "model_fingerprint",
        "endpoint_fingerprint",
    }
    issues: list[str] = []
    if set(value) != required_keys:
        issues.append(
            f"{context} embedding_signature must contain exactly: "
            + ", ".join(sorted(required_keys))
        )
    if value.get("schema_version") != EMBEDDING_SIGNATURE_IDENTITY_SCHEMA_VERSION:
        issues.append(f"{context} embedding_signature has an unsupported schema_version")
    signature_version = value.get("signature_version")
    if (
        not isinstance(signature_version, int)
        or isinstance(signature_version, bool)
        or signature_version != REQUIRED_EMBEDDING_SIGNATURE_VERSION
    ):
        issues.append(
            f"{context} embedding_signature signature_version must be "
            f"{REQUIRED_EMBEDDING_SIGNATURE_VERSION}"
        )

    for key, maximum_length in (("provider", 128), ("model", 512)):
        label = value.get(key)
        if (
            not isinstance(label, str)
            or label.strip() != label
            or not label
            or len(label) > maximum_length
        ):
            issues.append(f"{context} embedding_signature {key} must be a nonempty label")
            continue
        if _RAW_URL_PATTERN.search(label) or any(ord(character) < 32 for character in label):
            issues.append(
                f"{context} embedding_signature {key} must not contain a raw URL or control characters"
            )
        fingerprint = value.get(f"{key}_fingerprint")
        expected_fingerprint = sha256(label.encode("utf-8")).hexdigest()
        if (
            not isinstance(fingerprint, str)
            or _SHA256_HEX_PATTERN.fullmatch(fingerprint) is None
            or fingerprint != expected_fingerprint
        ):
            issues.append(
                f"{context} embedding_signature {key}_fingerprint does not match {key}"
            )

    endpoint_fingerprint = value.get("endpoint_fingerprint")
    if (
        not isinstance(endpoint_fingerprint, str)
        or _ENDPOINT_FINGERPRINT_PATTERN.fullmatch(endpoint_fingerprint) is None
    ):
        issues.append(
            f"{context} embedding_signature endpoint_fingerprint must be 16 lowercase hex characters"
        )
    return issues


def _summary_int_issue(
    summary: dict[str, object],
    *,
    key: str,
    expected: int,
) -> str | None:
    value = summary.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        return f"semantic eval report summary {key} does not match derived value {expected}"
    return None


def _exact_key_issues(
    value: object,
    *,
    expected: set[str],
    context: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{context} must be a JSON object"]
    if set(value) == expected:
        return []
    missing = sorted(expected - set(value))
    unexpected = sorted(set(value) - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unexpected:
        details.append("unexpected " + ", ".join(unexpected))
    return [f"{context} must contain exactly the canonical fields ({'; '.join(details)})"]


def _matches_canonical_value(value: object, canonical: object) -> bool:
    """Compare JSON-shaped values without Python's bool/number coercion."""
    if isinstance(canonical, dict):
        return (
            isinstance(value, dict)
            and value.keys() == canonical.keys()
            and all(
                _matches_canonical_value(value[key], canonical_value)
                for key, canonical_value in canonical.items()
            )
        )
    if isinstance(canonical, list):
        return (
            isinstance(value, list)
            and len(value) == len(canonical)
            and all(
                _matches_canonical_value(item, canonical_item)
                for item, canonical_item in zip(value, canonical, strict=True)
            )
        )
    if isinstance(canonical, bool):
        return isinstance(value, bool) and value is canonical
    if isinstance(canonical, int):
        return isinstance(value, int) and not isinstance(value, bool) and value == canonical
    if isinstance(canonical, float):
        return isinstance(value, float) and value == canonical
    if canonical is None:
        return value is None
    return isinstance(value, type(canonical)) and value == canonical


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and value.strip() == value and bool(value)


def _is_string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_is_nonempty_string(item) for item in value)
    )


def _number_issues(
    value: object,
    *,
    context: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> list[str]:
    if not _is_finite_number(value):
        return [f"{context} must be a finite number"]
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    normalized = float(value)
    if minimum is not None and normalized < minimum:
        return [f"{context} must be >= {minimum}"]
    if maximum is not None and normalized > maximum:
        return [f"{context} must be <= {maximum}"]
    return []


def _integer_issues(
    value: object,
    *,
    context: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return [f"{context} must be an integer"]
    if minimum is not None and value < minimum:
        return [f"{context} must be >= {minimum}"]
    if maximum is not None and value > maximum:
        return [f"{context} must be <= {maximum}"]
    return []


def _string_list_issues(
    value: object,
    *,
    context: str,
    allow_empty: bool,
    maximum_items: int | None = None,
) -> list[str]:
    if not isinstance(value, list):
        return [f"{context} must be a list"]
    if not allow_empty and not value:
        return [f"{context} must be nonempty"]
    if maximum_items is not None and len(value) > maximum_items:
        return [f"{context} must contain at most {maximum_items} items"]
    if any(not _is_nonempty_string(item) for item in value):
        return [f"{context} must contain only nonempty strings"]
    if len(set(value)) != len(value):
        return [f"{context} must not contain duplicate values"]
    return []


def _is_canonical_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo == timezone.utc and parsed.isoformat().endswith("+00:00")


def _mean_case_metric(cases: Sequence[object], key: str) -> float | None:
    values: list[float] = []
    for case in cases:
        metrics = case.get("metrics") if isinstance(case, dict) else None
        value = metrics.get(key) if isinstance(metrics, dict) else None
        if not _is_finite_number(value):
            return None
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        values.append(float(value))
    return sum(values) / len(values) if values else None


def _derived_metric_issue(
    metrics: dict[str, object],
    *,
    key: str,
    expected: float | None,
    context: str,
) -> str | None:
    value = metrics.get(key)
    if expected is None:
        return f"{context} {key} does not match its case-derived value"
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not math.isclose(float(value), expected, abs_tol=1e-12)
    ):
        return f"{context} {key} does not match its case-derived value"
    return None


def _semantic_eval_report_digest(report: dict[str, object]) -> str:
    """Canonical semantic digest; generated_at and the digest itself are metadata."""
    payload = {
        key: value
        for key, value in report.items()
        if key not in {"generated_at", "report_digest"}
    }
    return f"sha256:{_payload_sha256(payload)}"


@lru_cache(maxsize=1)
def _generator_release_contract() -> dict[str, object]:
    """Load canonical query/target linkage from the candidate's generators."""
    from alicebot_api.vnext_evals import canonical_semantic_eval_release_contract

    return canonical_semantic_eval_release_contract()


def _validate_canonical_corpus(corpus: object) -> list[str]:
    issues = _exact_key_issues(
        corpus,
        expected={"schema_version", "corpus_digest", "counts"},
        context="semantic eval report corpus",
    )
    if not isinstance(corpus, dict):
        return issues
    if corpus.get("schema_version") != "vnext_eval_corpus_v1":
        issues.append("semantic eval report corpus has an unsupported schema_version")
    digest = corpus.get("corpus_digest")
    if digest != SEMANTIC_EVAL_CANONICAL_CORPUS_DIGESTS["retrieval_quality"]:
        issues.append("semantic eval report corpus_digest does not match the canonical corpus")
    counts = corpus.get("counts")
    issues.extend(
        _exact_key_issues(
            counts,
            expected={"expected", "actual", "status", "mismatches"},
            context="semantic eval report corpus counts",
        )
    )
    expected_counts = {
        "memories": 216,
        "queries": 48,
        "lexical_overlap_queries": 32,
        "paraphrase_queries": 16,
        "distractor_memories": 168,
    }
    if isinstance(counts, dict):
        if counts.get("expected") != expected_counts or counts.get("actual") != expected_counts:
            issues.append("semantic eval report corpus counts do not match the canonical corpus")
        if counts.get("status") != "pass" or counts.get("mismatches") != {}:
            issues.append("semantic eval report canonical corpus validation did not pass")
    return issues


def _case_contract_issues(
    *,
    suite_key: str,
    case: dict[str, object],
    case_index: int,
    canonical_case: dict[str, object],
) -> list[str]:
    context = f"semantic eval case {suite_key}[{case_index}]"
    issues = _exact_key_issues(
        case,
        expected=_CASE_KEYS[suite_key],
        context=context,
    )
    case_status = case.get("status")
    if case_status not in {"pass", "fail"}:
        issues.append(f"{context} status must be pass or fail")
    metrics = case.get("metrics")
    evidence = case.get("evidence")
    issues.extend(
        _exact_key_issues(
            metrics,
            expected=_CASE_METRIC_KEYS[suite_key],
            context=f"{context} metrics",
        )
    )
    issues.extend(
        _exact_key_issues(
            evidence,
            expected=_CASE_EVIDENCE_KEYS[suite_key],
            context=f"{context} evidence",
        )
    )
    if not isinstance(metrics, dict) or not isinstance(evidence, dict):
        return issues

    case_key = str(case.get("case_key") or "")
    if suite_key == "retrieval_quality":
        expected_subset = canonical_case.get("subset")
        expected_memory_key = canonical_case.get("expected_memory_key")
        if case.get("subset") != expected_subset:
            issues.append(f"{context} subset does not match its canonical case identity")
        issues.extend(
            _number_issues(
                metrics.get("recall_at_5"),
                context=f"{context} recall_at_5",
                minimum=0.0,
                maximum=1.0,
            )
        )
        if metrics.get("recall_at_5") not in (0.0, 1.0):
            issues.append(f"{context} recall_at_5 must be binary")
        issues.extend(
            _number_issues(
                metrics.get("recall_at_1"),
                context=f"{context} recall_at_1",
                minimum=0.0,
                maximum=1.0,
            )
        )
        if metrics.get("recall_at_1") not in (0.0, 1.0):
            issues.append(f"{context} recall_at_1 must be binary")
        reciprocal_rank = metrics.get("reciprocal_rank")
        if (
            not isinstance(reciprocal_rank, (int, float))
            or isinstance(reciprocal_rank, bool)
            or not math.isfinite(float(reciprocal_rank))
            or not 0 <= float(reciprocal_rank) <= 1
        ):
            issues.append(f"{context} reciprocal_rank must be in [0, 1]")
        latency_ms = metrics.get("latency_ms")
        if (
            not isinstance(latency_ms, (int, float))
            or isinstance(latency_ms, bool)
            or not math.isfinite(float(latency_ms))
            or float(latency_ms) < 0
        ):
            issues.append(f"{context} latency_ms must be a finite nonnegative number")
        top_keys = evidence.get("top_memory_keys")
        if evidence.get("query") != canonical_case.get("query"):
            issues.append(f"{context} query does not match its canonical case")
        if evidence.get("expected_memory_key") != expected_memory_key:
            issues.append(f"{context} expected_memory_key is not canonical")
        issues.extend(
            _string_list_issues(
                top_keys,
                context=f"{context} top_memory_keys",
                allow_empty=False,
                maximum_items=_RETRIEVAL_EVIDENCE_LIMIT,
            )
        )
        target_rank = (
            top_keys.index(expected_memory_key) + 1
            if isinstance(top_keys, list) and expected_memory_key in top_keys
            else None
        )
        expected_recall_at_5 = 1.0 if target_rank is not None else 0.0
        expected_recall_at_1 = 1.0 if target_rank == 1 else 0.0
        expected_status = "pass" if target_rank is not None else "fail"
        if metrics.get("recall_at_5") != expected_recall_at_5:
            issues.append(f"{context} recall_at_5 does not match the target rank")
        if metrics.get("recall_at_1") != expected_recall_at_1:
            issues.append(f"{context} recall_at_1 does not match the target rank")
        if target_rank is not None:
            expected_reciprocal_rank = 1.0 / target_rank
            if reciprocal_rank != expected_reciprocal_rank:
                issues.append(f"{context} reciprocal_rank does not match the target rank")
        elif _is_finite_number(reciprocal_rank):
            assert isinstance(reciprocal_rank, (int, float)) and not isinstance(
                reciprocal_rank, bool
            )
            if float(reciprocal_rank) not in _OFF_WINDOW_RECIPROCAL_RANKS:
                issues.append(
                    f"{context} reciprocal_rank must be 0 or the reciprocal of a rank "
                    f"from {_RETRIEVAL_EVIDENCE_LIMIT + 1} through "
                    f"{_RETRIEVAL_RECALL_LIMIT} when the target is absent from "
                    "top_memory_keys"
                )
        if case_status != expected_status:
            issues.append(f"{context} status does not match recall_at_5")
        if evidence.get("vector_stage") != "enabled":
            issues.append(f"{context} did not record an enabled vector stage")
        candidate_count = evidence.get("vector_candidate_count")
        issues.extend(
            _integer_issues(
                candidate_count,
                context=f"{context} vector_candidate_count",
                minimum=1,
            )
        )
    elif suite_key == "correction_suppression":
        for key in (
            "pre_correction_visible",
            "suppressed",
            "replacement_recall_at_5",
            "audit_complete",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(key),
                    context=f"{context} {key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        for key in (
            "pre_correction_visible",
            "suppressed",
            "audit_complete",
        ):
            if metrics.get(key) != 1.0:
                issues.append(f"{context} {key} must be 1.0")
        issues.extend(
            _number_issues(
                metrics.get("replacement_reciprocal_rank"),
                context=f"{context} replacement_reciprocal_rank",
                minimum=0.0,
                maximum=1.0,
            )
        )
        replacement_key = evidence.get("replacement_memory_key")
        original_key = evidence.get("original_memory_key")
        rejected_key = evidence.get("rejected_memory_key")
        pre_keys = evidence.get("pre_correction_top_keys")
        post_keys = evidence.get("post_correction_top_keys")
        old_probe_keys = evidence.get("old_probe_top_keys")
        reject_probe_keys = evidence.get("reject_probe_top_keys")
        if not all(_is_nonempty_string(value) for value in (original_key, replacement_key, rejected_key)):
            issues.append(f"{context} must identify original, replacement, and rejected memories")
        if evidence.get("query") != canonical_case.get("query"):
            issues.append(f"{context} query does not match its canonical case")
        for evidence_key in (
            "original_memory_key",
            "replacement_memory_key",
            "rejected_memory_key",
        ):
            if evidence.get(evidence_key) != canonical_case.get(evidence_key):
                issues.append(f"{context} {evidence_key} is not canonical")
        if (
            all(
                isinstance(value, str)
                for value in (original_key, replacement_key, rejected_key)
            )
            and len({original_key, replacement_key, rejected_key}) != 3
        ):
            issues.append(f"{context} correction memory identities must be distinct")
        if (
            not isinstance(pre_keys, list)
            or not _is_string_list(pre_keys)
            or original_key not in pre_keys
        ):
            issues.append(f"{context} pre-correction evidence must retrieve the original")
        if not isinstance(old_probe_keys, list) or original_key in old_probe_keys:
            issues.append(f"{context} old-probe evidence must suppress the original")
        if not isinstance(reject_probe_keys, list) or rejected_key in reject_probe_keys:
            issues.append(f"{context} reject-probe evidence must suppress the rejected memory")
        if isinstance(post_keys, list) and (
            original_key in post_keys or rejected_key in post_keys
        ):
            issues.append(f"{context} post-correction evidence contains suppressed memories")
        expected_pre_visible = 1.0 if isinstance(pre_keys, list) and original_key in pre_keys else 0.0
        expected_suppressed = (
            1.0
            if isinstance(post_keys, list)
            and original_key not in post_keys
            and rejected_key not in post_keys
            else 0.0
        )
        expected_replacement_recall = (
            1.0 if isinstance(post_keys, list) and replacement_key in post_keys else 0.0
        )
        expected_replacement_rr = (
            1.0 / (post_keys.index(replacement_key) + 1)
            if isinstance(post_keys, list) and replacement_key in post_keys
            else 0.0
        )
        for metric_key, expected_value in (
            ("pre_correction_visible", expected_pre_visible),
            ("suppressed", expected_suppressed),
            ("replacement_recall_at_5", expected_replacement_recall),
            ("replacement_reciprocal_rank", expected_replacement_rr),
        ):
            if metrics.get(metric_key) != expected_value:
                issues.append(f"{context} {metric_key} does not match its evidence")
        for evidence_key, evidence_value in (
            ("pre_correction_top_keys", pre_keys),
            ("post_correction_top_keys", post_keys),
            ("old_probe_top_keys", old_probe_keys),
            ("reject_probe_top_keys", reject_probe_keys),
        ):
            issues.extend(
                _string_list_issues(
                    evidence_value,
                    context=f"{context} {evidence_key}",
                    allow_empty=True,
                    maximum_items=5,
                )
            )
        if evidence.get("commit_flow_notes") != []:
            issues.append(f"{context} commit_flow_notes must be empty")
        expected_reason = f"superseded_by:{replacement_key}"
        if evidence.get("superseded_revision_reason") != expected_reason:
            issues.append(f"{context} must include the canonical supersession reason")
        expected_status = (
            "pass"
            if all(
                metrics.get(key) == 1.0
                for key in (
                    "pre_correction_visible",
                    "suppressed",
                    "replacement_recall_at_5",
                    "audit_complete",
                )
            )
            else "fail"
        )
        if case_status != expected_status:
            issues.append(f"{context} status does not match correction metrics")
    elif suite_key == "decision_recovery":
        expected_memory_key = canonical_case.get("expected_memory_key")
        for metric_key in (
            "recall_at_1",
            "recall_at_5",
            "reciprocal_rank",
            "filtered_recall_at_5",
            "filtered_reciprocal_rank",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        for binary_key in ("recall_at_1", "recall_at_5", "filtered_recall_at_5"):
            if metrics.get(binary_key) not in (0.0, 1.0):
                issues.append(f"{context} {binary_key} must be binary")
        if evidence.get("query") != canonical_case.get("query"):
            issues.append(f"{context} query does not match its canonical case")
        if evidence.get("expected_memory_key") != expected_memory_key:
            issues.append(f"{context} expected_memory_key is not canonical")
        for key in ("top_memory_keys", "filtered_top_memory_keys"):
            ranked = evidence.get(key)
            issues.extend(
                _string_list_issues(
                    ranked,
                    context=f"{context} {key}",
                    allow_empty=True,
                    maximum_items=5,
                )
            )
        for evidence_key, recall_key, reciprocal_key in (
            ("top_memory_keys", "recall_at_5", "reciprocal_rank"),
            (
                "filtered_top_memory_keys",
                "filtered_recall_at_5",
                "filtered_reciprocal_rank",
            ),
        ):
            ranked = evidence.get(evidence_key)
            rank = (
                ranked.index(expected_memory_key) + 1
                if isinstance(ranked, list) and expected_memory_key in ranked
                else None
            )
            expected_recall = 1.0 if rank is not None else 0.0
            expected_reciprocal = 1.0 / rank if rank is not None else 0.0
            if metrics.get(recall_key) != expected_recall:
                issues.append(f"{context} {recall_key} does not match its target rank")
            if metrics.get(reciprocal_key) != expected_reciprocal:
                issues.append(f"{context} {reciprocal_key} does not match its target rank")
        unfiltered = evidence.get("top_memory_keys")
        unfiltered_rank = (
            unfiltered.index(expected_memory_key) + 1
            if isinstance(unfiltered, list) and expected_memory_key in unfiltered
            else None
        )
        expected_recall_at_1 = 1.0 if unfiltered_rank == 1 else 0.0
        if metrics.get("recall_at_1") != expected_recall_at_1:
            issues.append(f"{context} recall_at_1 does not match its target rank")
        expected_status = "pass" if unfiltered_rank is not None else "fail"
        if case_status != expected_status:
            issues.append(f"{context} status does not match recall_at_5")
    elif suite_key == "provenance_explanation":
        checks = case.get("checks")
        required_checks = {
            "committed",
            "has_commit_event",
            "has_reasoned_revision",
            "provenance_resolves",
        }
        if canonical_case.get("corrected") is True:
            required_checks.add("correction_reflected")
        issues.extend(
            _exact_key_issues(
                checks,
                expected=required_checks,
                context=f"{context} checks",
            )
        )
        if isinstance(checks, dict) and checks != {key: "pass" for key in sorted(required_checks)}:
            issues.append(f"{context} checks must all contain the canonical pass verdict")
        if metrics.get("explain_complete") != 1.0:
            issues.append(f"{context} explain_complete must be 1.0")
        issues.extend(
            _number_issues(
                metrics.get("explain_complete"),
                context=f"{context} explain_complete",
                minimum=0.0,
                maximum=1.0,
            )
        )
        if evidence.get("commit_status") != "committed":
            issues.append(f"{context} commit_status must be committed")
        memory_id = evidence.get("memory_id")
        if not isinstance(memory_id, str) or _UUID_PATTERN.fullmatch(memory_id) is None:
            issues.append(f"{context} memory_id must be a canonical UUID")
        event_types = evidence.get("event_types")
        revision_types = evidence.get("revision_types")
        revision_reasons = evidence.get("revision_reasons")
        if (
            not isinstance(event_types, list)
            or not _is_string_list(event_types)
            or "agent.memory_committed" not in event_types
        ):
            issues.append(f"{context} must include the memory commit event")
        if not _is_string_list(revision_types):
            issues.append(f"{context} must include revision types")
        if not _is_string_list(revision_reasons):
            issues.append(f"{context} must include reasoned revisions")
        for evidence_key, evidence_value in (
            ("event_types", event_types),
            ("revision_types", revision_types),
            ("revision_reasons", revision_reasons),
        ):
            issues.extend(
                _string_list_issues(
                    evidence_value,
                    context=f"{context} {evidence_key}",
                    allow_empty=False,
                )
            )
        if isinstance(revision_types, list) and isinstance(revision_reasons, list):
            if len(revision_types) != len(revision_reasons):
                issues.append(f"{context} revision types and reasons must align")
            expected_revision_types = (
                ["created", "corrected"]
                if canonical_case.get("corrected") is True
                else ["created"]
            )
            if revision_types != expected_revision_types:
                issues.append(f"{context} revision types do not match its canonical case")
        if canonical_case.get("corrected") is True and (
            not isinstance(event_types, list)
            or "agent.memory_corrected" not in event_types
        ):
            issues.append(f"{context} corrected case must include the correction event")
        if canonical_case.get("corrected") is not True and (
            isinstance(event_types, list) and "agent.memory_corrected" in event_types
        ):
            issues.append(f"{context} uncorrected case must not include a correction event")
        expected_event_types = (
            [
                "agent.memory_committed",
                "agent.memory_corrected",
                "memory.created",
                "memory.updated",
                "memory_revision.created",
                "policy.decision",
                "provenance_link.created",
            ]
            if canonical_case.get("corrected") is True
            else [
                "agent.memory_committed",
                "memory.created",
                "memory_revision.created",
                "provenance_link.created",
            ]
        )
        if event_types != expected_event_types:
            issues.append(f"{context} event types do not match its canonical case")
        if evidence.get("orphan_link_count") != 0:
            issues.append(f"{context} must not contain orphan provenance links")
        resolved_count = evidence.get("resolved_link_count")
        issues.extend(
            _integer_issues(
                resolved_count,
                context=f"{context} resolved_link_count",
                minimum=1,
            )
        )
        issues.extend(
            _integer_issues(
                evidence.get("orphan_link_count"),
                context=f"{context} orphan_link_count",
                minimum=0,
            )
        )
        link_count = evidence.get("provenance_link_count")
        if (
            not isinstance(link_count, int)
            or isinstance(link_count, bool)
            or link_count < 1
            or resolved_count != link_count
        ):
            issues.append(f"{context} provenance links must all resolve")
        expected_status = "pass" if metrics.get("explain_complete") == 1.0 else "fail"
        if case_status != expected_status:
            issues.append(f"{context} status does not match explain_complete")
    elif suite_key == "entity_resolution":
        if metrics.get("resolved") != 1.0:
            issues.append(f"{context} resolved must be 1.0")
        issues.extend(
            _number_issues(
                metrics.get("resolved"),
                context=f"{context} resolved",
                minimum=0.0,
                maximum=1.0,
            )
        )
        mention_count = metrics.get("mention_count")
        if not isinstance(mention_count, int) or isinstance(mention_count, bool) or mention_count < 2:
            issues.append(f"{context} mention_count must be at least 2")
        if evidence.get("canonical_normalized") != canonical_case.get("canonical_normalized"):
            issues.append(f"{context} canonical entity identity does not match")
        entity_id = evidence.get("entity_id")
        if not isinstance(entity_id, str) or _UUID_PATTERN.fullmatch(entity_id) is None:
            issues.append(f"{context} entity_id must be a canonical UUID")
        aliases = evidence.get("aliases")
        issues.extend(
            _string_list_issues(
                aliases,
                context=f"{context} aliases",
                allow_empty=True,
            )
        )
        expected_alias = canonical_case.get("expected_alias")
        expected_aliases = [expected_alias] if expected_alias is not None else []
        if aliases != expected_aliases:
            issues.append(f"{context} aliases do not match its canonical case")
        expected_status = (
            "pass"
            if metrics.get("resolved") == 1.0
            and isinstance(mention_count, int)
            and not isinstance(mention_count, bool)
            and mention_count >= 2
            and aliases == expected_aliases
            else "fail"
        )
        if case_status != expected_status:
            issues.append(f"{context} status does not match entity-resolution evidence")
    elif suite_key == "graph_hop_retrieval":
        expected_memory_key = canonical_case.get("expected_memory_key")
        for metric_key in (
            "graph_recall_at_5",
            "fts_recall_at_5",
            "winner_has_graph_rank",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
            if metrics.get(metric_key) not in (0.0, 1.0):
                issues.append(f"{context} {metric_key} must be binary")
        graph_keys = evidence.get("graph_top_keys")
        if evidence.get("query") != canonical_case.get("query"):
            issues.append(f"{context} query does not match its canonical case")
        if evidence.get("expected_memory_key") != expected_memory_key:
            issues.append(f"{context} expected_memory_key is not canonical")
        for evidence_key in ("graph_top_keys", "fts_top_keys"):
            issues.extend(
                _string_list_issues(
                    evidence.get(evidence_key),
                    context=f"{context} {evidence_key}",
                    allow_empty=True,
                    maximum_items=5,
                )
            )
        fts_keys = evidence.get("fts_top_keys")
        expected_graph_recall = (
            1.0 if isinstance(graph_keys, list) and expected_memory_key in graph_keys else 0.0
        )
        expected_fts_recall = (
            1.0 if isinstance(fts_keys, list) and expected_memory_key in fts_keys else 0.0
        )
        if metrics.get("graph_recall_at_5") != expected_graph_recall:
            issues.append(f"{context} graph_recall_at_5 does not match its evidence")
        if metrics.get("fts_recall_at_5") != expected_fts_recall:
            issues.append(f"{context} fts_recall_at_5 does not match its evidence")
        winner_ranks = evidence.get("winner_stage_ranks")
        if not isinstance(winner_ranks, dict):
            issues.append(f"{context} winner_stage_ranks must be an object")
        else:
            unsupported_stages = set(winner_ranks) - _GRAPH_WINNER_STAGE_KEYS
            if unsupported_stages:
                issues.append(
                    f"{context} winner_stage_ranks contains unsupported stage keys: "
                    + ", ".join(sorted(str(stage) for stage in unsupported_stages))
                )
            for stage, rank in winner_ranks.items():
                if not _is_nonempty_string(stage):
                    issues.append(f"{context} winner_stage_ranks keys must be nonempty strings")
                issues.extend(
                    _integer_issues(
                        rank,
                        context=f"{context} winner_stage_ranks.{stage}",
                        minimum=1,
                    )
                )
        expected_winner_has_graph_rank = (
            1.0 if isinstance(winner_ranks, dict) and "graph" in winner_ranks else 0.0
        )
        if metrics.get("winner_has_graph_rank") != expected_winner_has_graph_rank:
            issues.append(f"{context} winner_has_graph_rank does not match its evidence")
        expected_status = (
            "pass"
            if expected_graph_recall == 1.0 and expected_winner_has_graph_rank == 1.0
            else "fail"
        )
        if case_status != expected_status:
            issues.append(f"{context} status does not match graph evidence")
        control = evidence.get("control_graph_stage")
        issues.extend(
            _exact_key_issues(
                control,
                expected={"status", "matched_entities", "candidate_count"},
                context=f"{context} control_graph_stage",
            )
        )
        if isinstance(control, dict):
            if control.get("status") != "disabled: store does not support entities":
                issues.append(f"{context} must include a disabled graph-control stage")
            if control.get("matched_entities") != []:
                issues.append(f"{context} graph control must match no entities")
            candidate_count = control.get("candidate_count")
            if (
                not isinstance(candidate_count, int)
                or isinstance(candidate_count, bool)
                or candidate_count != 0
            ):
                issues.append(f"{context} graph control candidate_count must be 0")

    if not _is_nonempty_string(evidence.get("query")) and "query" in _CASE_EVIDENCE_KEYS[suite_key]:
        issues.append(f"{context} evidence query must be nonempty")
    return issues


def _suite_contract_issues(suite: dict[str, object], *, index: int) -> list[str]:
    suite_key = str(suite.get("suite_key") or "")
    context = f"semantic eval suite {suite_key or index!r}"
    issues = _exact_key_issues(suite, expected=_SUITE_KEYS, context=context)
    if suite_key not in SEMANTIC_EVAL_REQUIRED_SUITES:
        return issues
    release_contract = _generator_release_contract()
    suite_contract_value = release_contract.get(suite_key)
    suite_contract = (
        suite_contract_value if isinstance(suite_contract_value, dict) else {}
    )
    if suite.get("title") != suite_contract.get("title"):
        issues.append(f"{context} title does not match the canonical suite title")
    if not _matches_canonical_value(
        suite.get("targets"),
        SEMANTIC_EVAL_CANONICAL_TARGETS[suite_key],
    ):
        issues.append(f"{context} targets do not match the canonical acceptance targets")
    metrics = suite.get("metrics")
    issues.extend(
        _exact_key_issues(
            metrics,
            expected=_SUITE_METRIC_KEYS[suite_key],
            context=f"{context} metrics",
        )
    )
    if not isinstance(metrics, dict):
        return issues
    if metrics.get("backend") != "postgres":
        issues.append(f"{context} release evidence must run against Postgres")
    expected_checks = {
        key: "pass" for key in sorted(SEMANTIC_EVAL_CANONICAL_TARGETS[suite_key])
    }
    if metrics.get("target_checks") != expected_checks:
        issues.append(f"{context} target_checks must contain exactly the canonical pass checks")
    target_values: dict[str, object]
    if suite_key == "retrieval_quality":
        subsets = metrics.get("subsets")
        lexical = subsets.get("lexical_overlap") if isinstance(subsets, dict) else None
        paraphrase = subsets.get("paraphrase") if isinstance(subsets, dict) else None
        target_values = {
            "lexical_overlap_recall_at_5": (
                lexical.get("recall_at_5") if isinstance(lexical, dict) else None
            ),
            "lexical_overlap_mrr": lexical.get("mrr") if isinstance(lexical, dict) else None,
            "paraphrase_recall_at_5": (
                paraphrase.get("recall_at_5") if isinstance(paraphrase, dict) else None
            ),
        }
    else:
        target_values = {
            target_key: metrics.get(target_key)
            for target_key in SEMANTIC_EVAL_CANONICAL_TARGETS[suite_key]
        }
    for target_key, target_spec in SEMANTIC_EVAL_CANONICAL_TARGETS[suite_key].items():
        target_value = target_values.get(target_key)
        if not _is_finite_number(target_value):
            issues.append(f"{context} target metric {target_key} must be a finite number")
            continue
        assert isinstance(target_value, (int, float)) and not isinstance(target_value, bool)
        minimum = target_spec.get("minimum")
        maximum = target_spec.get("maximum")
        if (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and float(target_value) < float(minimum)
        ):
            issues.append(f"{context} target metric {target_key} is below its canonical minimum")
        if (
            isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and float(target_value) > float(maximum)
        ):
            issues.append(f"{context} target metric {target_key} is above its canonical maximum")
    corpus_digest = metrics.get("corpus_digest")
    if (
        suite_key != "retrieval_quality"
        and corpus_digest != SEMANTIC_EVAL_CANONICAL_CORPUS_DIGESTS[suite_key]
    ):
        issues.append(f"{context} corpus_digest does not match its canonical corpus")

    cases = suite.get("cases")
    if not isinstance(cases, list):
        return issues
    canonical_cases_value = suite_contract.get("cases")
    canonical_cases = (
        canonical_cases_value if isinstance(canonical_cases_value, list) else []
    )
    case_keys = [str(case.get("case_key") or "") for case in cases if isinstance(case, dict)]
    if tuple(case_keys) != SEMANTIC_EVAL_CANONICAL_CASE_KEYS[suite_key] or len(case_keys) != len(cases):
        issues.append(f"{context} does not contain the exact ordered canonical case identities")
    for case_index, case in enumerate(cases):
        if isinstance(case, dict):
            canonical_case_value = (
                canonical_cases[case_index]
                if case_index < len(canonical_cases)
                else {}
            )
            canonical_case = (
                canonical_case_value
                if isinstance(canonical_case_value, dict)
                else {}
            )
            issues.extend(
                _case_contract_issues(
                    suite_key=suite_key,
                    case=case,
                    case_index=case_index,
                    canonical_case=canonical_case,
                )
            )

    expected_case_count = len(SEMANTIC_EVAL_CANONICAL_CASE_KEYS[suite_key])
    count_key = {
        "retrieval_quality": "query_count",
        "correction_suppression": "case_count",
        "decision_recovery": "query_count",
        "provenance_explanation": "audited_memory_count",
        "entity_resolution": "group_count",
        "graph_hop_retrieval": "group_count",
    }[suite_key]
    if metrics.get(count_key) != expected_case_count:
        issues.append(f"{context} {count_key} does not match the canonical case count")

    if suite_key == "retrieval_quality":
        for metric_key in ("recall_at_1", "recall_at_5", "mrr"):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        for suite_key_name, case_key_name in (
            ("recall_at_1", "recall_at_1"),
            ("recall_at_5", "recall_at_5"),
            ("mrr", "reciprocal_rank"),
        ):
            issue = _derived_metric_issue(
                metrics,
                key=suite_key_name,
                expected=_mean_case_metric(cases, case_key_name),
                context=context,
            )
            if issue is not None:
                issues.append(issue)
        latency = metrics.get("latency_ms")
        issues.extend(
            _exact_key_issues(
                latency,
                expected={"p50", "p95", "max"},
                context=f"{context} latency_ms",
            )
        )
        if isinstance(latency, dict):
            for latency_key in ("p50", "p95", "max"):
                issues.extend(
                    _number_issues(
                        latency.get(latency_key),
                        context=f"{context} latency_ms.{latency_key}",
                        minimum=0.0,
                    )
                )
            p50 = latency.get("p50")
            p95 = latency.get("p95")
            maximum = latency.get("max")
            if all(_is_finite_number(value) for value in (p50, p95, maximum)):
                assert isinstance(p50, (int, float)) and not isinstance(p50, bool)
                assert isinstance(p95, (int, float)) and not isinstance(p95, bool)
                assert isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
                if not float(p50) <= float(p95) <= float(maximum):
                    issues.append(f"{context} latency percentiles must be monotonic")
        if metrics.get("retrieval_mode") != "hybrid":
            issues.append("semantic release evidence did not use hybrid retrieval")
        if metrics.get("vector_stage_participated") is not True:
            issues.append("semantic release evidence has no vector-stage participation")
        if metrics.get("paraphrase_targets_enforced") is not True:
            issues.append("semantic release evidence did not enforce paraphrase targets")
        if metrics.get("release_gate") is not True or metrics.get("vector_stages") != ["enabled"]:
            issues.append("semantic retrieval metrics do not describe a canonical release-gate run")
        if metrics.get("vector_query_count") != 48 or metrics.get("vector_queries_with_candidates") != 48:
            issues.append("semantic release evidence must record vector candidates for all 48 queries")
        vector_candidate_total = 0
        vector_candidate_total_valid = True
        for case in cases:
            evidence = case.get("evidence") if isinstance(case, dict) else None
            candidate_count = (
                evidence.get("vector_candidate_count")
                if isinstance(evidence, dict)
                else None
            )
            if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
                vector_candidate_total_valid = False
                break
            vector_candidate_total += candidate_count
        if (
            not vector_candidate_total_valid
            or metrics.get("vector_candidate_count") != vector_candidate_total
        ):
            issues.append("semantic retrieval vector_candidate_count does not match cases")
        subsets = metrics.get("subsets")
        if not isinstance(subsets, dict) or set(subsets) != {"lexical_overlap", "paraphrase"}:
            issues.append("semantic retrieval subsets must be exactly lexical_overlap and paraphrase")
        else:
            for subset_key, query_count in (("lexical_overlap", 32), ("paraphrase", 16)):
                subset = subsets.get(subset_key)
                issues.extend(
                    _exact_key_issues(
                        subset,
                        expected={"query_count", "recall_at_1", "recall_at_5", "mrr"},
                        context=f"semantic retrieval subset {subset_key}",
                    )
                )
                if isinstance(subset, dict) and subset.get("query_count") != query_count:
                    issues.append(f"semantic retrieval subset {subset_key} query_count is not canonical")
                if isinstance(subset, dict):
                    for metric_key in ("recall_at_1", "recall_at_5", "mrr"):
                        issues.extend(
                            _number_issues(
                                subset.get(metric_key),
                                context=f"semantic retrieval subset {subset_key} {metric_key}",
                                minimum=0.0,
                                maximum=1.0,
                            )
                        )
                    subset_cases = [
                        case
                        for case in cases
                        if isinstance(case, dict) and case.get("subset") == subset_key
                    ]
                    for subset_metric_key, case_metric_key in (
                        ("recall_at_1", "recall_at_1"),
                        ("recall_at_5", "recall_at_5"),
                        ("mrr", "reciprocal_rank"),
                    ):
                        issue = _derived_metric_issue(
                            subset,
                            key=subset_metric_key,
                            expected=_mean_case_metric(subset_cases, case_metric_key),
                            context=f"semantic retrieval subset {subset_key}",
                        )
                        if issue is not None:
                            issues.append(issue)
        seeding = metrics.get("seeding")
        issues.extend(
            _exact_key_issues(
                seeding,
                expected={
                    "seeded_memory_count",
                    "embedded_memory_count",
                    "embedding_signature",
                    "embedding_note",
                },
                context="semantic retrieval seeding",
            )
        )
        if isinstance(seeding, dict):
            if seeding.get("seeded_memory_count") != 216 or seeding.get("embedded_memory_count") != 216:
                issues.append("semantic release evidence must embed all 216 canonical memories")
            if seeding.get("embedding_signature") is None:
                issues.append("semantic retrieval seeding must include its embedding signature")
            signature = seeding.get("embedding_signature")
            provider = signature.get("provider") if isinstance(signature, dict) else None
            model = signature.get("model") if isinstance(signature, dict) else None
            if seeding.get("embedding_note") != f"embedded via {provider}/{model}":
                issues.append("semantic retrieval seeding embedding_note is not canonical")
    elif suite_key == "correction_suppression":
        if metrics.get("distractor_count") != 8:
            issues.append(f"{context} distractor_count is not canonical")
        for metric_key in (
            "pre_correction_visibility",
            "suppression_rate",
            "replacement_recall_at_5",
            "replacement_mrr",
            "audit_completeness",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        for suite_metric_key, case_metric_key in (
            ("pre_correction_visibility", "pre_correction_visible"),
            ("suppression_rate", "suppressed"),
            ("replacement_recall_at_5", "replacement_recall_at_5"),
            ("replacement_mrr", "replacement_reciprocal_rank"),
            ("audit_completeness", "audit_complete"),
        ):
            issue = _derived_metric_issue(
                metrics,
                key=suite_metric_key,
                expected=_mean_case_metric(cases, case_metric_key),
                context=context,
            )
            if issue is not None:
                issues.append(issue)
    elif suite_key == "decision_recovery":
        if metrics.get("decision_count") != 10 or metrics.get("distractor_count") != 30:
            issues.append(f"{context} decision/distractor counts are not canonical")
        for metric_key in (
            "decision_recall_at_1",
            "decision_recall_at_5",
            "decision_mrr",
            "filtered_decision_recall_at_5",
            "filtered_decision_mrr",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        for suite_metric_key, case_metric_key in (
            ("decision_recall_at_1", "recall_at_1"),
            ("decision_recall_at_5", "recall_at_5"),
            ("decision_mrr", "reciprocal_rank"),
            ("filtered_decision_recall_at_5", "filtered_recall_at_5"),
            ("filtered_decision_mrr", "filtered_reciprocal_rank"),
        ):
            issue = _derived_metric_issue(
                metrics,
                key=suite_metric_key,
                expected=_mean_case_metric(cases, case_metric_key),
                context=context,
            )
            if issue is not None:
                issues.append(issue)
        filter_info = metrics.get("memory_types_filter")
        issues.extend(
            _exact_key_issues(
                filter_info,
                expected={"available", "note"},
                context=f"{context} memory_types_filter",
            )
        )
        if isinstance(filter_info, dict):
            if filter_info.get("available") is not True:
                issues.append(f"{context} must exercise the decision memory_types filter")
            expected_filter_note = (
                "filtered via VNextRetrievalRequest.memory_types=('decision',)"
            )
            if filter_info.get("note") != expected_filter_note:
                issues.append(f"{context} memory_types_filter note is not canonical")
    elif suite_key == "provenance_explanation":
        if metrics.get("corrected_memory_count") != 2 or metrics.get("orphan_provenance_count") != 0:
            issues.append(f"{context} correction/provenance counts are not canonical")
        for metric_key in (
            "audited_memory_count",
            "corrected_memory_count",
            "provenance_link_count",
            "orphan_provenance_count",
        ):
            issues.extend(
                _integer_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0,
                )
            )
        issues.extend(
            _number_issues(
                metrics.get("explain_completeness_rate"),
                context=f"{context} explain_completeness_rate",
                minimum=0.0,
                maximum=1.0,
            )
        )
        issue = _derived_metric_issue(
            metrics,
            key="explain_completeness_rate",
            expected=_mean_case_metric(cases, "explain_complete"),
            context=context,
        )
        if issue is not None:
            issues.append(issue)
        resolved_total = 0
        orphan_total = 0
        evidence_counts_valid = True
        for case in cases:
            evidence = case.get("evidence") if isinstance(case, dict) else None
            resolved = evidence.get("resolved_link_count") if isinstance(evidence, dict) else None
            orphan = evidence.get("orphan_link_count") if isinstance(evidence, dict) else None
            if (
                not isinstance(resolved, int)
                or isinstance(resolved, bool)
                or not isinstance(orphan, int)
                or isinstance(orphan, bool)
            ):
                evidence_counts_valid = False
                break
            resolved_total += resolved
            orphan_total += orphan
        if (
            not evidence_counts_valid
            or metrics.get("provenance_link_count") != resolved_total + orphan_total
            or metrics.get("orphan_provenance_count") != orphan_total
        ):
            issues.append(f"{context} provenance aggregates do not match case evidence")
        memory_ids = [
            evidence.get("memory_id")
            for case in cases
            if isinstance(case, dict)
            and isinstance((evidence := case.get("evidence")), dict)
        ]
        if (
            len(memory_ids) != len(cases)
            or not all(isinstance(memory_id, str) for memory_id in memory_ids)
            or len(set(memory_ids)) != len(memory_ids)
        ):
            issues.append(f"{context} memory IDs must be present and distinct")
    elif suite_key == "entity_resolution":
        if metrics.get("noise_entity_count") != 0 or metrics.get("noise_entities") != []:
            issues.append(f"{context} must contain no entity-resolution noise")
        for metric_key in ("group_count", "entity_count", "noise_entity_count"):
            issues.extend(
                _integer_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0,
                )
            )
        for metric_key in ("resolution_rate", "mention_accuracy", "alias_growth_rate"):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        if metrics.get("entity_count") != 3:
            issues.append(f"{context} entity_count is not canonical")
        entity_ids = [
            evidence.get("entity_id")
            for case in cases
            if isinstance(case, dict)
            and isinstance((evidence := case.get("evidence")), dict)
        ]
        if (
            len(entity_ids) != len(cases)
            or not all(isinstance(entity_id, str) for entity_id in entity_ids)
            or len(set(entity_ids)) != len(entity_ids)
        ):
            issues.append(f"{context} entity IDs must be present and distinct")
        resolution_rate = _mean_case_metric(cases, "resolved")
        mention_accuracy = (
            sum(
                1
                for case in cases
                if isinstance(case, dict)
                and isinstance(case.get("metrics"), dict)
                and isinstance(case["metrics"].get("mention_count"), int)
                and not isinstance(case["metrics"].get("mention_count"), bool)
                and case["metrics"]["mention_count"] >= 2
            )
            / len(cases)
            if cases
            else 0.0
        )
        alias_case_indexes = [
            case_index
            for case_index, canonical_case in enumerate(canonical_cases)
            if isinstance(canonical_case, dict)
            and canonical_case.get("expected_alias") is not None
        ]
        alias_growth_rate = (
            sum(
                1
                for case_index in alias_case_indexes
                if case_index < len(cases)
                and isinstance(cases[case_index], dict)
                and isinstance(cases[case_index].get("evidence"), dict)
                and canonical_cases[case_index].get("expected_alias")
                in cases[case_index]["evidence"].get("aliases", [])
            )
            / len(alias_case_indexes)
            if alias_case_indexes
            else 1.0
        )
        for metric_key, expected_value in (
            ("resolution_rate", resolution_rate),
            ("mention_accuracy", mention_accuracy),
            ("alias_growth_rate", alias_growth_rate),
        ):
            issue = _derived_metric_issue(
                metrics,
                key=metric_key,
                expected=expected_value,
                context=context,
            )
            if issue is not None:
                issues.append(issue)
    elif suite_key == "graph_hop_retrieval":
        issues.extend(
            _integer_issues(
                metrics.get("group_count"),
                context=f"{context} group_count",
                minimum=0,
            )
        )
        for metric_key in (
            "graph_recall_at_5",
            "fts_only_recall_at_5",
            "winner_graph_rank_rate",
        ):
            issues.extend(
                _number_issues(
                    metrics.get(metric_key),
                    context=f"{context} {metric_key}",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        issues.extend(
            _number_issues(
                metrics.get("graph_lift"),
                context=f"{context} graph_lift",
                minimum=-1.0,
                maximum=1.0,
            )
        )
        for suite_metric_key, case_metric_key in (
            ("graph_recall_at_5", "graph_recall_at_5"),
            ("fts_only_recall_at_5", "fts_recall_at_5"),
            ("winner_graph_rank_rate", "winner_has_graph_rank"),
        ):
            issue = _derived_metric_issue(
                metrics,
                key=suite_metric_key,
                expected=_mean_case_metric(cases, case_metric_key),
                context=context,
            )
            if issue is not None:
                issues.append(issue)
        graph_recall = metrics.get("graph_recall_at_5")
        fts_recall = metrics.get("fts_only_recall_at_5")
        graph_lift = metrics.get("graph_lift")
        if all(_is_finite_number(value) for value in (graph_recall, fts_recall, graph_lift)):
            assert isinstance(graph_recall, (int, float)) and not isinstance(graph_recall, bool)
            assert isinstance(fts_recall, (int, float)) and not isinstance(fts_recall, bool)
            assert isinstance(graph_lift, (int, float)) and not isinstance(graph_lift, bool)
            if not math.isclose(
                float(graph_lift),
                float(graph_recall) - float(fts_recall),
                abs_tol=1e-12,
            ):
                issues.append(f"{context} graph_lift does not match recall delta")
        expected_control = "duck-type wrapper hiding find_entities_by_names/list_edges"
        if metrics.get("control_mechanism") != expected_control:
            issues.append(f"{context} control_mechanism is not canonical")
    return issues


def validate_semantic_eval_report(report: object) -> list[str]:
    """Validate that release evidence measured real vector participation."""
    if not isinstance(report, dict):
        return ["semantic eval report must be a JSON object"]
    issues = _exact_key_issues(
        report,
        expected=_REPORT_TOP_LEVEL_KEYS,
        context="semantic eval report",
    )
    if report.get("schema_version") != "vnext_eval_report_v1":
        issues.append("semantic eval report has an unsupported schema_version")
    if report.get("release_gate") is not True:
        issues.append("semantic eval report was not produced in release-gate mode")
    if report.get("suite") != "all":
        issues.append("semantic eval report must cover suite=all")
    report_status = report.get("status")
    if report_status != "pass":
        issues.append(f"semantic eval report status is not pass: {report_status!r}")
    if not _is_canonical_utc_timestamp(report.get("generated_at")):
        issues.append("semantic eval report generated_at must be a valid UTC timestamp")
    if not _matches_canonical_value(report.get("targets"), SEMANTIC_EVAL_CANONICAL_TARGETS):
        issues.append(
            "semantic eval report targets must be nonempty and match the canonical acceptance targets"
        )
    issues.extend(_validate_canonical_corpus(report.get("corpus")))

    embedding_signature = report.get("embedding_signature")
    issues.extend(
        _validate_embedding_signature_identity(
            embedding_signature,
            context="semantic eval report",
        )
    )

    summary_value = report.get("summary")
    summary = summary_value if isinstance(summary_value, dict) else None
    if summary is None:
        issues.append("semantic eval report is missing summary")
    else:
        issues.extend(
            _exact_key_issues(
                summary,
                expected=_SUMMARY_KEYS,
                context="semantic eval report summary",
            )
        )

    suites = report.get("suites")
    retrieval: dict[str, object] | None = None
    suite_keys: list[str] = []
    suite_statuses: list[object] = []
    skipped_suite_keys: list[str] = []
    case_count = 0
    passed_case_count = 0
    all_suites_have_cases = True
    all_target_checks_pass = True
    if isinstance(suites, list):
        for index, suite in enumerate(suites):
            if not isinstance(suite, dict):
                issues.append(f"semantic eval report suite at index {index} must be an object")
                suite_keys.append("")
                suite_statuses.append(None)
                all_suites_have_cases = False
                all_target_checks_pass = False
                continue
            suite_key = str(suite.get("suite_key") or "")
            suite_status = suite.get("status")
            suite_keys.append(suite_key)
            suite_statuses.append(suite_status)
            issues.extend(_suite_contract_issues(suite, index=index))
            if suite_status == "skipped":
                skipped_suite_keys.append(suite_key)
            if suite_status != "pass":
                issues.append(f"semantic eval suite {suite_key or index!r} did not pass")

            suite_cases = suite.get("cases")
            if not isinstance(suite_cases, list) or not suite_cases:
                issues.append(
                    f"semantic eval suite {suite_key or index!r} must contain nonempty cases"
                )
                all_suites_have_cases = False
            else:
                case_count += len(suite_cases)
                for case in suite_cases:
                    if isinstance(case, dict) and case.get("status") == "pass":
                        passed_case_count += 1

            metrics = suite.get("metrics")
            target_checks = metrics.get("target_checks") if isinstance(metrics, dict) else None
            if not isinstance(target_checks, dict) or not target_checks:
                all_target_checks_pass = False
                issues.append(
                    f"semantic eval suite {suite_key or index!r} is missing target checks"
                )
            elif any(value != "pass" for value in target_checks.values()):
                all_target_checks_pass = False
                issues.append(
                    f"semantic eval suite {suite_key or index!r} contains a failed target check"
                )

            if suite_key == "retrieval_quality":
                retrieval = suite

        if tuple(suite_keys) != SEMANTIC_EVAL_REQUIRED_SUITES:
            issues.append("semantic eval report does not contain the canonical suite order")
    else:
        issues.append("semantic eval report is missing suites")
        suites = []
        all_suites_have_cases = False
        all_target_checks_pass = False

    suite_count = len(suites)
    skipped_suite_count = sum(status == "skipped" for status in suite_statuses)
    executed_suite_count = suite_count - skipped_suite_count
    failed_case_count = case_count - passed_case_count
    pass_rate = passed_case_count / case_count if case_count else 0.0
    derived_status = (
        "pass"
        if tuple(suite_keys) == SEMANTIC_EVAL_REQUIRED_SUITES
        and all(status == "pass" for status in suite_statuses)
        and all_suites_have_cases
        and all_target_checks_pass
        else "fail"
    )

    if summary is not None:
        for key, expected in (
            ("suite_count", suite_count),
            ("executed_suite_count", executed_suite_count),
            ("skipped_suite_count", skipped_suite_count),
            ("case_count", case_count),
            ("passed_case_count", passed_case_count),
            ("failed_case_count", failed_case_count),
        ):
            issue = _summary_int_issue(summary, key=key, expected=expected)
            if issue is not None:
                issues.append(issue)
        if summary.get("suite_order") != suite_keys:
            issues.append("semantic eval report summary suite_order does not match suites")
        reported_pass_rate = summary.get("pass_rate")
        if (
            not isinstance(reported_pass_rate, (int, float))
            or isinstance(reported_pass_rate, bool)
            or not math.isfinite(float(reported_pass_rate))
            or not math.isclose(float(reported_pass_rate), pass_rate, abs_tol=1e-12)
        ):
            issues.append("semantic eval report summary pass_rate does not match cases")
        if summary.get("status") != report_status:
            issues.append("semantic eval report summary status does not match report status")
        if summary.get("status") != derived_status:
            issues.append("semantic eval report summary status does not match derived status")
    if report_status != derived_status:
        issues.append("semantic eval report status does not match derived suite/case verdict")

    skipped_suites = report.get("skipped_suites")
    if not isinstance(skipped_suites, list):
        issues.append("semantic eval report skipped_suites must be a list")
    else:
        reported_skipped_keys = [
            str(item.get("suite_key") or "")
            for item in skipped_suites
            if isinstance(item, dict)
        ]
        if len(reported_skipped_keys) != len(skipped_suites) or reported_skipped_keys != skipped_suite_keys:
            issues.append("semantic eval report skipped_suites does not match suite statuses")
    if executed_suite_count != suite_count or skipped_suite_count != 0:
        issues.append("semantic eval report must execute every suite without skips")

    if not isinstance(retrieval, dict):
        issues.append("semantic eval report is missing retrieval_quality")
    else:
        if retrieval.get("status") != "pass":
            issues.append("semantic retrieval suite did not pass")
        metrics = retrieval.get("metrics")
        if not isinstance(metrics, dict):
            issues.append("semantic retrieval suite is missing metrics")
        else:
            if metrics.get("backend") != "postgres":
                issues.append("semantic release evidence must run against Postgres + pgvector")
            if metrics.get("retrieval_mode") != "hybrid":
                issues.append("semantic release evidence did not use hybrid retrieval")
            if metrics.get("vector_stage_participated") is not True:
                issues.append("semantic release evidence has no vector-stage participation")
            candidate_count = metrics.get("vector_candidate_count")
            if not isinstance(candidate_count, int) or candidate_count <= 0:
                issues.append("semantic release evidence vector_candidate_count must be > 0")
            if metrics.get("paraphrase_targets_enforced") is not True:
                issues.append("semantic release evidence did not enforce paraphrase targets")
            seeding = metrics.get("seeding")
            if (
                not isinstance(seeding, dict)
                or seeding.get("embedding_signature") != embedding_signature
            ):
                issues.append(
                    "semantic retrieval seeding embedding_signature does not match the report"
                )

    credential_paths = _credential_material_paths(report)
    if credential_paths:
        issues.append(
            "semantic eval report contains credential-like material at: "
            + ", ".join(credential_paths)
        )

    report_digest = report.get("report_digest")
    if not isinstance(report_digest, str) or _REPORT_DIGEST_PATTERN.fullmatch(report_digest) is None:
        issues.append("semantic eval report_digest must be sha256:<64 lowercase hex>")
    if report_digest != _semantic_eval_report_digest(report):
        issues.append("semantic eval report_digest does not match its structured content")
    return issues


def write_semantic_eval_attestation(
    *,
    report_path: Path,
    attestation_path: Path,
    source_sha: str,
) -> Path:
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("semantic eval attestation source_sha must be 40 lowercase hex characters")
    if report_path.resolve().parent != attestation_path.resolve().parent:
        raise ValueError("semantic eval report and attestation must share one artifact directory")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = validate_semantic_eval_report(report)
    if issues:
        raise ValueError("invalid semantic eval report: " + "; ".join(issues))
    retrieval = next(
        suite for suite in report["suites"] if suite["suite_key"] == "retrieval_quality"
    )
    metrics = retrieval["metrics"]
    subsets = metrics["subsets"]
    paraphrase = subsets["paraphrase"]
    attestation = {
        "schema_version": SEMANTIC_EVAL_ATTESTATION_SCHEMA_VERSION,
        "source_sha": source_sha,
        "report_file": report_path.name,
        "report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "report_digest": report["report_digest"],
        "generated_at": report["generated_at"],
        "suite": report["suite"],
        "status": report["status"],
        "embedding_signature": report["embedding_signature"],
        "backend": metrics["backend"],
        "retrieval_mode": metrics["retrieval_mode"],
        "vector_candidate_count": metrics["vector_candidate_count"],
        "vector_stage_participated": metrics["vector_stage_participated"],
        "paraphrase_recall_at_5": paraphrase["recall_at_5"],
        "credentials_included": False,
    }
    attestation_path.parent.mkdir(parents=True, exist_ok=True)
    attestation_path.write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return attestation_path


def validate_semantic_eval_attestation(
    *,
    attestation_path: Path,
    expected_sha: str,
) -> list[str]:
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"could not read semantic eval attestation: {exc}"]
    if not isinstance(attestation, dict):
        return ["semantic eval attestation must be a JSON object"]
    issues = _exact_key_issues(
        attestation,
        expected=_ATTESTATION_KEYS,
        context="semantic eval attestation",
    )
    if attestation.get("schema_version") != SEMANTIC_EVAL_ATTESTATION_SCHEMA_VERSION:
        issues.append("semantic eval attestation has an unsupported schema_version")
    if attestation.get("source_sha") != expected_sha:
        issues.append(
            "semantic eval attestation source_sha does not match the exact release SHA"
        )
    if attestation.get("credentials_included") is not False:
        issues.append("semantic eval attestation must explicitly exclude credentials")
    report_digest = attestation.get("report_digest")
    if not isinstance(report_digest, str) or _REPORT_DIGEST_PATTERN.fullmatch(report_digest) is None:
        issues.append("semantic eval attestation report_digest must be sha256:<64 lowercase hex>")
    report_sha256 = attestation.get("report_sha256")
    if not isinstance(report_sha256, str) or _SHA256_HEX_PATTERN.fullmatch(report_sha256) is None:
        issues.append("semantic eval attestation report_sha256 must be 64 lowercase hex characters")
    issues.extend(
        _integer_issues(
            attestation.get("vector_candidate_count"),
            context="semantic eval attestation vector_candidate_count",
            minimum=1,
        )
    )
    vector_stage_participated = attestation.get("vector_stage_participated")
    if not isinstance(vector_stage_participated, bool):
        issues.append(
            "semantic eval attestation vector_stage_participated must be a boolean"
        )
    elif vector_stage_participated is not True:
        issues.append(
            "semantic eval attestation vector_stage_participated must be true"
        )
    paraphrase_recall_at_5 = attestation.get("paraphrase_recall_at_5")
    if (
        not isinstance(paraphrase_recall_at_5, float)
        or not math.isfinite(paraphrase_recall_at_5)
    ):
        issues.append(
            "semantic eval attestation paraphrase_recall_at_5 must be a finite float"
        )
    elif not 0.0 <= paraphrase_recall_at_5 <= 1.0:
        issues.append(
            "semantic eval attestation paraphrase_recall_at_5 must be between 0.0 and 1.0"
        )
    attestation_embedding_signature = attestation.get("embedding_signature")
    issues.extend(
        _validate_embedding_signature_identity(
            attestation_embedding_signature,
            context="semantic eval attestation",
        )
    )
    credential_paths = _credential_material_paths(attestation)
    # `credentials_included: false` is a declaration, not credential material.
    credential_paths = [path for path in credential_paths if path != "$.credentials_included"]
    if credential_paths:
        issues.append(
            "semantic eval attestation contains credential-like material at: "
            + ", ".join(credential_paths)
        )

    report_file = attestation.get("report_file")
    if not isinstance(report_file, str) or Path(report_file).name != report_file:
        issues.append("semantic eval attestation report_file must be a sibling filename")
        return issues
    report_path = attestation_path.parent / report_file
    try:
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"could not read attested semantic eval report: {exc}")
        return issues
    if sha256(report_bytes).hexdigest() != attestation.get("report_sha256"):
        issues.append("semantic eval report SHA-256 does not match its attestation")
    issues.extend(validate_semantic_eval_report(report))
    if isinstance(report, dict) and not _matches_canonical_value(
        attestation.get("report_digest"),
        report.get("report_digest"),
    ):
        issues.append("semantic eval report_digest does not match its attestation")
    if isinstance(report, dict):
        if not _matches_canonical_value(
            attestation_embedding_signature,
            report.get("embedding_signature"),
        ):
            issues.append(
                "semantic eval attestation embedding_signature does not match its report"
            )
        for key in ("generated_at", "suite", "status"):
            if not _matches_canonical_value(attestation.get(key), report.get(key)):
                issues.append(f"semantic eval attestation {key} does not match its report")
        suites = report.get("suites")
        retrieval = next(
            (
                suite
                for suite in suites
                if isinstance(suite, dict)
                and suite.get("suite_key") == "retrieval_quality"
            ),
            None,
        ) if isinstance(suites, list) else None
        metrics = retrieval.get("metrics") if isinstance(retrieval, dict) else None
        if isinstance(metrics, dict):
            for key in (
                "backend",
                "retrieval_mode",
                "vector_candidate_count",
                "vector_stage_participated",
            ):
                if not _matches_canonical_value(
                    attestation.get(key),
                    metrics.get(key),
                ):
                    issues.append(
                        f"semantic eval attestation {key} does not match its report"
                    )
            subsets = metrics.get("subsets")
            paraphrase = (
                subsets.get("paraphrase") if isinstance(subsets, dict) else None
            )
            if (
                not isinstance(paraphrase, dict)
                or not _matches_canonical_value(
                    attestation.get("paraphrase_recall_at_5"),
                    paraphrase.get("recall_at_5"),
                )
            ):
                issues.append(
                    "semantic eval attestation paraphrase_recall_at_5 does not match its report"
                )
    return issues


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def read_release_metadata(root_dir: Path = ROOT_DIR) -> ReleaseMetadata:
    pyproject = _read_toml(root_dir / "pyproject.toml")
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    distribution_name = str(project.get("name", ""))
    version = str(project.get("version", ""))

    web_payload = json.loads((root_dir / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    if not isinstance(web_payload, dict):
        raise ValueError("apps/web/package.json must contain an object")
    return ReleaseMetadata(
        distribution_name=distribution_name,
        version=version,
        web_version=str(web_payload.get("version", "")),
    )


def validate_package_description_source(root_dir: Path) -> tuple[str | None, list[str]]:
    """Validate the evergreen long-description source used by package metadata."""

    issues: list[str] = []
    pyproject = _read_toml(root_dir / "pyproject.toml")
    project = pyproject.get("project")
    configured_readme = project.get("readme") if isinstance(project, dict) else None
    if configured_readme != PACKAGE_DESCRIPTION_RELATIVE_PATH:
        issues.append(
            "pyproject.toml project.readme must point to the evergreen "
            f"{PACKAGE_DESCRIPTION_RELATIVE_PATH!r}"
        )

    description_path = root_dir / PACKAGE_DESCRIPTION_RELATIVE_PATH
    try:
        description = description_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(
            f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: could not read package description: {exc}"
        )
        return None, issues

    if _PACKAGE_DESCRIPTION_VERSION_PATTERN.search(description):
        issues.append(
            f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: contains a version literal"
        )
    for label, pattern in _PACKAGE_DESCRIPTION_STATE_PATTERNS:
        if pattern.search(description):
            issues.append(
                f"{PACKAGE_DESCRIPTION_RELATIVE_PATH}: contains {label} language"
            )
    return description, issues


def _app_constructor_uses_distribution_version(api_source: str) -> bool:
    """Return whether the exported top-level ``app`` receives ``__version__``.

    The concrete constructor may be FastAPI or a project subclass. Checking the
    AST keeps the release contract semantic instead of coupling it to one
    formatting style or constructor name.
    """
    module = ast.parse(api_source)
    app_value: ast.expr | None = None
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "app"
            for target in statement.targets
        ):
            app_value = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "app"
        ):
            app_value = statement.value
    if not isinstance(app_value, ast.Call):
        return False
    return any(
        keyword.arg == "version"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "__version__"
        for keyword in app_value.keywords
    )


def _package_version_uses_distribution_metadata(package_source: str) -> bool:
    """Return whether top-level package initialization reads alice-memory metadata.

    This is intentionally an AST check rather than an import: release validation
    must not execute package code from an arbitrary checkout. Import aliases and
    quote/formatting choices are accepted, while comments, strings, functions,
    and unrelated lookalike calls cannot satisfy the contract.
    """
    module = ast.parse(package_source)
    direct_version_functions: set[str] = set()
    metadata_modules: set[str] = set()
    importlib_modules: set[str] = set()
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom):
            if statement.module == "importlib.metadata":
                direct_version_functions.update(
                    alias.asname or alias.name
                    for alias in statement.names
                    if alias.name == "version"
                )
            elif statement.module == "importlib":
                metadata_modules.update(
                    alias.asname or alias.name
                    for alias in statement.names
                    if alias.name == "metadata"
                )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "importlib.metadata":
                    if alias.asname:
                        metadata_modules.add(alias.asname)
                    else:
                        importlib_modules.add("importlib")

    def is_metadata_version_call(value: ast.expr | None) -> bool:
        if not isinstance(value, ast.Call) or not value.args:
            return False
        distribution = value.args[0]
        if not (
            isinstance(distribution, ast.Constant)
            and distribution.value == "alice-memory"
        ):
            return False
        function = value.func
        if isinstance(function, ast.Name):
            return function.id in direct_version_functions
        if not isinstance(function, ast.Attribute) or function.attr != "version":
            return False
        owner = function.value
        if isinstance(owner, ast.Name):
            return owner.id in metadata_modules
        return (
            isinstance(owner, ast.Attribute)
            and owner.attr == "metadata"
            and isinstance(owner.value, ast.Name)
            and owner.value.id in importlib_modules
        )

    def block_version_state(statements: list[ast.stmt]) -> bool | None:
        state: bool | None = None
        for statement in statements:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in statement.targets
            ):
                state = is_metadata_version_call(statement.value)
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "__version__"
            ):
                state = is_metadata_version_call(statement.value)
            elif isinstance(statement, ast.Try):
                try_state = block_version_state(statement.body)
                if try_state is not None:
                    state = try_state
        return state

    return block_version_state(module.body) is True


def validate_metadata(root_dir: Path = ROOT_DIR) -> tuple[ReleaseMetadata, list[str]]:
    metadata = read_release_metadata(root_dir)
    issues: list[str] = []
    _description, description_issues = validate_package_description_source(root_dir)
    issues.extend(description_issues)
    if metadata.distribution_name != "alice-memory":
        issues.append(f"unexpected distribution name: {metadata.distribution_name!r}")
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", metadata.version):
        issues.append(f"release version must be stable SemVer, got {metadata.version!r}")
    if metadata.web_version != metadata.version:
        issues.append(
            "apps/web/package.json version does not match pyproject.toml: "
            f"{metadata.web_version!r} != {metadata.version!r}"
        )

    api_source = (root_dir / "apps" / "api" / "src" / "alicebot_api" / "main.py").read_text(encoding="utf-8")
    try:
        app_uses_distribution_version = _app_constructor_uses_distribution_version(
            api_source
        )
    except SyntaxError:
        app_uses_distribution_version = False
    if not app_uses_distribution_version:
        issues.append("FastAPI application version is not sourced from alicebot_api.__version__")
    package_init = (
        root_dir / "apps" / "api" / "src" / "alicebot_api" / "__init__.py"
    ).read_text(encoding="utf-8")
    try:
        package_version_uses_metadata = _package_version_uses_distribution_metadata(
            package_init
        )
    except SyntaxError:
        package_version_uses_metadata = False
    if not package_version_uses_metadata:
        issues.append("alicebot_api.__version__ is not sourced from installed distribution metadata")
    return metadata, issues


def validate_release_document_state(
    root_dir: Path,
    *,
    version: str,
    require_finalized: bool,
) -> list[str]:
    if not require_finalized:
        return []

    issues: list[str] = []
    changelog = (root_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    release_heading = re.compile(
        rf"^## v{re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}$",
        flags=re.MULTILINE,
    )
    if release_heading.search(changelog) is None:
        issues.append(
            f"CHANGELOG.md must contain a finalized dated heading for v{version}"
        )
    unreleased_match = re.search(
        r"^## Unreleased\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        changelog,
        flags=re.MULTILINE | re.DOTALL,
    )
    if unreleased_match is None:
        issues.append("CHANGELOG.md must retain an empty Unreleased section")
    elif unreleased_match.group("body").strip():
        issues.append("CHANGELOG.md Unreleased section must be empty for the release tag")

    release_notes_path = root_dir / "docs" / "release" / f"v{version}-release-notes.md"
    try:
        release_notes = release_notes_path.read_text(encoding="utf-8")
    except OSError:
        issues.append(f"release notes are missing: {release_notes_path.relative_to(root_dir)}")
    else:
        expected_title = f"# Alice v{version} Release Notes"
        if not release_notes.startswith(expected_title + "\n"):
            issues.append(f"release notes must start with finalized title: {expected_title}")
        lines = release_notes.splitlines()
        state_lines = [
            (index, line)
            for index, line in enumerate(lines)
            if "alice-release-state" in line
        ]
        state: object = None
        state_match: re.Match[str] | None = None
        if len(state_lines) != 1:
            issues.append(
                "release notes must contain exactly one alice-release-state declaration"
            )
        elif state_lines[0][0] != 1:
            issues.append(
                "release notes alice-release-state must appear immediately below the exact title"
            )
        else:
            state_match = _RELEASE_DOCUMENT_STATE_PATTERN.fullmatch(state_lines[0][1])
            if state_match is None:
                issues.append("release notes alice-release-state declaration is malformed")
        if state_match is not None:
            try:
                state = json.loads(state_match.group("payload"))
            except json.JSONDecodeError as exc:
                issues.append(f"release notes alice-release-state is invalid JSON: {exc}")
        if isinstance(state, dict):
            if state.get("schema_version") != RELEASE_DOCUMENT_STATE_SCHEMA_VERSION:
                issues.append("release notes alice-release-state has an unsupported schema")
            if state.get("version") != version:
                issues.append("release notes alice-release-state version does not match")
            if state.get("publication_status") != "pending":
                issues.append(
                    "release notes alice-release-state publication_status must be pending before tag"
                )
            if state.get("checksums_status") != "pending":
                issues.append(
                    "release notes alice-release-state checksums_status must be pending before tag"
                )
        elif state is not None:
            issues.append("release notes alice-release-state must be a JSON object")

        checksums_name = f"v{version}-checksums.txt"
        checksums_exists = (root_dir / "docs" / "release" / checksums_name).exists()
        if checksums_exists:
            issues.append(
                f"docs/release/{checksums_name} must not exist on the pre-publication tag"
            )
    return issues


def _git(root_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def validate_git_state(
    *,
    root_dir: Path,
    tag: str | None,
    expected_sha: str | None,
    require_main_head: bool,
    require_clean: bool,
) -> list[str]:
    issues: list[str] = []
    head = _git(root_dir, "rev-parse", "HEAD")
    if expected_sha is not None and head != expected_sha:
        issues.append(f"checked-out SHA {head} does not match expected release SHA {expected_sha}")

    if tag is not None:
        tag_ref = f"refs/tags/{tag}"
        try:
            tag_type = _git(root_dir, "cat-file", "-t", tag_ref)
            tag_sha = _git(root_dir, "rev-list", "-n", "1", tag_ref)
        except subprocess.CalledProcessError:
            issues.append(f"release tag does not exist locally: {tag}")
        else:
            if tag_type != "tag":
                issues.append(f"release tag {tag} must be an annotated tag, got {tag_type}")
            if tag_sha != head:
                issues.append(f"release tag {tag} points to {tag_sha}, not checked-out SHA {head}")

    if require_main_head:
        try:
            main_sha = _git(root_dir, "rev-parse", "refs/remotes/origin/main")
        except subprocess.CalledProcessError:
            issues.append("origin/main is unavailable; fetch it before running the release check")
        else:
            if head != main_sha:
                issues.append(f"release SHA {head} is not the exact origin/main head {main_sha}")

    if require_clean and _git(root_dir, "status", "--porcelain"):
        issues.append("working tree is not clean")
    return issues


def _wheel_metadata_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"wheel must contain one METADATA file, found {metadata_names}")
        return archive.read(metadata_names[0]).decode("utf-8")


def _wheel_version(path: Path) -> str:
    metadata_text = _wheel_metadata_text(path)
    for line in metadata_text.splitlines():
        if line.startswith("Version: "):
            return line.removeprefix("Version: ").strip()
    raise ValueError("wheel METADATA is missing Version")


def _sdist_pyproject_version(path: Path) -> str:
    with tarfile.open(path, mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/pyproject.toml")]
        if len(members) != 1:
            raise ValueError(f"sdist must contain one pyproject.toml, found {[m.name for m in members]}")
        handle = archive.extractfile(members[0])
        if handle is None:
            raise ValueError("could not read sdist pyproject.toml")
        payload = tomllib.loads(handle.read().decode("utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("sdist pyproject.toml is missing [project]")
    return str(project.get("version", ""))


def _sdist_pkg_info_text(path: Path) -> str:
    with tarfile.open(path, mode="r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1
        ]
        if len(members) != 1:
            raise ValueError(
                f"sdist must contain one top-level PKG-INFO, found {[m.name for m in members]}"
            )
        handle = archive.extractfile(members[0])
        if handle is None:
            raise ValueError("could not read sdist PKG-INFO")
        return handle.read().decode("utf-8")


def _metadata_description(metadata_text: str) -> str:
    message = Parser(policy=policy.default).parsestr(metadata_text)
    payload = message.get_payload()
    if not isinstance(payload, str):
        raise ValueError("distribution metadata long description is not text")
    return payload


def validate_distributions(
    dist_dir: Path,
    *,
    version: str,
    expected_description: str | None = None,
) -> tuple[list[Path], list[str]]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    artifacts = [*wheels, *sdists]
    issues: list[str] = []
    if len(wheels) != 1:
        issues.append(f"expected exactly one wheel in {dist_dir}, found {[p.name for p in wheels]}")
    if len(sdists) != 1:
        issues.append(f"expected exactly one sdist in {dist_dir}, found {[p.name for p in sdists]}")
    if issues:
        return artifacts, issues

    wheel = wheels[0]
    sdist = sdists[0]
    try:
        if _wheel_version(wheel) != version:
            issues.append(f"wheel metadata version does not match {version}")
        if _sdist_pyproject_version(sdist) != version:
            issues.append(f"sdist metadata version does not match {version}")
        if expected_description is not None:
            expected = expected_description.strip()
            wheel_description = _metadata_description(_wheel_metadata_text(wheel)).strip()
            sdist_description = _metadata_description(_sdist_pkg_info_text(sdist)).strip()
            if wheel_description != expected:
                issues.append(
                    "wheel METADATA long description does not match the evergreen package description"
                )
            if sdist_description != expected:
                issues.append(
                    "sdist PKG-INFO long description does not match the evergreen package description"
                )
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        issues.append(f"could not inspect distributions: {exc}")
        return artifacts, issues

    required_wheel_resources = {
        "alicebot_api/_resources/alembic.ini",
        "alicebot_api/_resources/alembic/env.py",
        "alicebot_api/_resources/eval/public_eval_suites.json",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = sorted(required_wheel_resources - names)
    if missing:
        issues.append(f"wheel is missing runtime resources: {missing}")
    if not any(
        name.startswith("alicebot_api/_resources/alembic/versions/") and name.endswith(".py")
        for name in names
    ):
        issues.append("wheel contains no packaged Alembic revisions")

    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_names = {member.name for member in archive.getmembers()}
    required_sdist_suffixes = (
        "/apps/api/alembic.ini",
        "/apps/api/alembic/env.py",
        "/eval/fixtures/public_eval_suites.json",
        "/setup.py",
    )
    for suffix in required_sdist_suffixes:
        if not any(name.endswith(suffix) for name in sdist_names):
            issues.append(f"sdist is missing build/runtime source: *{suffix}")
    return artifacts, issues


def write_checksums(dist_dir: Path, artifacts: list[Path]) -> Path:
    manifest = dist_dir / "SHA256SUMS"
    lines = [f"{sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(artifacts)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def validate_release_asset_set(
    *, dist_dir: Path, artifacts: Sequence[Path]
) -> list[str]:
    """Require exactly one manifest covering exactly the validated distributions."""

    manifest = dist_dir / "SHA256SUMS"
    expected_names = {path.name for path in artifacts}
    expected_asset_names = {*expected_names, manifest.name}
    try:
        actual_asset_names = {path.name for path in dist_dir.iterdir()}
    except OSError as exc:
        return [f"could not list release assets: {exc}"]
    issues: list[str] = []
    if actual_asset_names != expected_asset_names:
        issues.append(
            "release asset set must contain only the wheel, sdist, and SHA256SUMS: "
            f"expected={sorted(expected_asset_names)} actual={sorted(actual_asset_names)}"
        )
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        issues.append(f"could not read SHA256SUMS: {exc}")
        return issues

    entries: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]*)", line)
        if match is None:
            issues.append(f"SHA256SUMS line {line_number} is malformed")
            continue
        digest, filename = match.groups()
        if filename in entries:
            issues.append(f"SHA256SUMS repeats artifact {filename}")
            continue
        entries[filename] = digest
    if set(entries) != expected_names:
        issues.append(
            "SHA256SUMS file set does not match validated distributions: "
            f"expected={sorted(expected_names)} manifest={sorted(entries)}"
        )
    for artifact in artifacts:
        expected_digest = entries.get(artifact.name)
        if expected_digest is not None and sha256(artifact.read_bytes()).hexdigest() != expected_digest:
            issues.append(f"SHA256SUMS digest does not match {artifact.name}")
    return issues


def compare_distribution_artifacts(
    *, canonical: Sequence[Path], rebuilt: Sequence[Path]
) -> list[str]:
    """Require a deterministic rebuild to reproduce every canonical distribution byte."""

    canonical_by_name = {path.name: path for path in canonical}
    rebuilt_by_name = {path.name: path for path in rebuilt}
    if set(canonical_by_name) != set(rebuilt_by_name):
        return [
            "deterministic rebuild file set does not match canonical distributions: "
            f"canonical={sorted(canonical_by_name)} rebuilt={sorted(rebuilt_by_name)}"
        ]
    issues: list[str] = []
    for filename in sorted(canonical_by_name):
        canonical_digest = sha256(canonical_by_name[filename].read_bytes()).hexdigest()
        rebuilt_digest = sha256(rebuilt_by_name[filename].read_bytes()).hexdigest()
        if canonical_digest != rebuilt_digest:
            issues.append(f"deterministic rebuild does not match canonical artifact {filename}")
    return issues


def pypi_version_exists(distribution_name: str, version: str) -> bool:
    url = f"https://pypi.org/pypi/{distribution_name}/{version}/json"
    request = Request(url, headers={"User-Agent": "alice-release-check/1"})
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed PyPI origin
            return response.status == 200
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def verify_pypi_artifacts(
    *,
    distribution_name: str,
    version: str,
    artifacts: Sequence[Path],
    allow_subset: bool = False,
) -> list[str]:
    """Verify local release artifacts exactly match the files recorded by PyPI."""

    url = f"https://pypi.org/pypi/{distribution_name}/{version}/json"
    request = Request(url, headers={"User-Agent": "alice-release-check/1"})
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed PyPI origin
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return [f"{distribution_name} {version} does not exist on PyPI"]
        return [f"could not read PyPI release metadata: HTTP {exc.code}"]
    except (OSError, URLError, json.JSONDecodeError, TypeError) as exc:
        return [f"could not read PyPI release metadata: {exc}"]

    if not isinstance(payload, dict):
        return ["PyPI release metadata must be a JSON object"]
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return ["PyPI release metadata is missing its file list"]

    published: dict[str, str] = {}
    issues: list[str] = []
    for index, item in enumerate(urls):
        if not isinstance(item, dict):
            issues.append(f"PyPI file entry {index} must be an object")
            continue
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not isinstance(digest, str):
            issues.append(f"PyPI file entry {index} is missing filename or sha256")
            continue
        if filename in published:
            issues.append(f"PyPI release metadata repeats file {filename}")
            continue
        published[filename] = digest

    local = {path.name: sha256(path.read_bytes()).hexdigest() for path in artifacts}
    if allow_subset and not published:
        issues.append("PyPI resume requires at least one already-published verified artifact")
    if allow_subset and set(published) == set(local):
        issues.append(
            "PyPI resume requires a proper partial file set; use finalization recovery when all artifacts exist"
        )
    file_set_matches = (
        set(published).issubset(local) if allow_subset else set(local) == set(published)
    )
    if not file_set_matches:
        issues.append(
            "PyPI file set is not an exact permitted set of verified artifacts: "
            f"local={sorted(local)} published={sorted(published)}"
        )
    for filename in sorted(set(local) & set(published)):
        if local[filename] != published[filename]:
            issues.append(f"PyPI sha256 does not match verified artifact {filename}")
    return issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument("--tag", default=None, help="Release tag, for example v0.9.2.")
    parser.add_argument("--expected-sha", default=None)
    parser.add_argument("--require-main-head", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--check-pypi", action="store_true")
    parser.add_argument(
        "--verify-pypi-artifacts",
        action="store_true",
        help="Require --dist-dir wheel/sdist bytes to exactly match PyPI file digests.",
    )
    parser.add_argument(
        "--verify-pypi-artifact-subset",
        action="store_true",
        help="Allow PyPI to contain an exact verified subset before a protected resume upload.",
    )
    parser.add_argument(
        "--verify-release-assets",
        action="store_true",
        help="Require exactly wheel, sdist, and a matching SHA256SUMS manifest.",
    )
    parser.add_argument(
        "--require-finalized-release-docs",
        action="store_true",
        help="Require a dated changelog section and final release-note title before tagging.",
    )
    parser.add_argument("--dist-dir", type=Path, default=None)
    parser.add_argument(
        "--compare-dist-dir",
        type=Path,
        default=None,
        help="Require a second valid distribution directory to be byte-identical.",
    )
    parser.add_argument("--write-checksums", action="store_true")
    parser.add_argument(
        "--semantic-eval-report",
        type=Path,
        default=None,
        help="Validate a credential-free, fully passing semantic release-gate report.",
    )
    parser.add_argument(
        "--write-semantic-eval-attestation",
        type=Path,
        default=None,
        help="Write exact-SHA structured attestation for --semantic-eval-report.",
    )
    parser.add_argument(
        "--semantic-eval-attestation",
        type=Path,
        default=None,
        help="Validate an exact-SHA semantic report/attestation artifact pair.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root_dir = args.root.resolve()
    metadata, issues = validate_metadata(root_dir)
    package_description, _description_issues = validate_package_description_source(
        root_dir
    )

    if args.tag is not None and args.tag != metadata.tag:
        issues.append(f"release tag {args.tag!r} does not match package version tag {metadata.tag!r}")
    issues.extend(
        validate_release_document_state(
            root_dir,
            version=metadata.version,
            require_finalized=args.require_finalized_release_docs or args.tag is not None,
        )
    )
    issues.extend(
        validate_git_state(
            root_dir=root_dir,
            tag=args.tag,
            expected_sha=args.expected_sha,
            require_main_head=args.require_main_head,
            require_clean=args.require_clean,
        )
    )

    artifacts: list[Path] = []
    if args.dist_dir is not None:
        artifacts, artifact_issues = validate_distributions(
            args.dist_dir.resolve(),
            version=metadata.version,
            expected_description=package_description,
        )
        issues.extend(artifact_issues)
    elif args.write_checksums:
        issues.append("--write-checksums requires --dist-dir")
    if args.compare_dist_dir is not None:
        if args.dist_dir is None:
            issues.append("--compare-dist-dir requires --dist-dir")
        else:
            rebuilt, rebuilt_issues = validate_distributions(
                args.compare_dist_dir.resolve(),
                version=metadata.version,
                expected_description=package_description,
            )
            issues.extend(rebuilt_issues)
            if artifacts and not rebuilt_issues:
                issues.extend(
                    compare_distribution_artifacts(
                        canonical=artifacts, rebuilt=rebuilt
                    )
                )
    if args.verify_pypi_artifacts and args.verify_pypi_artifact_subset:
        issues.append(
            "--verify-pypi-artifacts and --verify-pypi-artifact-subset are mutually exclusive"
        )
    verify_pypi = args.verify_pypi_artifacts or args.verify_pypi_artifact_subset
    if args.verify_release_assets or verify_pypi:
        if args.dist_dir is None:
            issues.append("release asset verification requires --dist-dir")
        elif not artifacts:
            issues.append("release asset verification requires valid wheel and sdist artifacts")
        else:
            issues.extend(
                validate_release_asset_set(
                    dist_dir=args.dist_dir.resolve(), artifacts=artifacts
                )
            )
    if verify_pypi and artifacts:
        issues.extend(
            verify_pypi_artifacts(
                distribution_name=metadata.distribution_name,
                version=metadata.version,
                artifacts=artifacts,
                allow_subset=args.verify_pypi_artifact_subset,
            )
        )

    semantic_report_path = (
        args.semantic_eval_report.resolve()
        if args.semantic_eval_report is not None
        else None
    )
    if semantic_report_path is not None:
        try:
            semantic_report = json.loads(
                semantic_report_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"could not read semantic eval report: {exc}")
        else:
            issues.extend(validate_semantic_eval_report(semantic_report))
    if args.write_semantic_eval_attestation is not None:
        if semantic_report_path is None:
            issues.append(
                "--write-semantic-eval-attestation requires --semantic-eval-report"
            )
        if args.expected_sha is None:
            issues.append("--write-semantic-eval-attestation requires --expected-sha")
    if args.semantic_eval_attestation is not None:
        if args.expected_sha is None:
            issues.append("--semantic-eval-attestation requires --expected-sha")
        else:
            issues.extend(
                validate_semantic_eval_attestation(
                    attestation_path=args.semantic_eval_attestation.resolve(),
                    expected_sha=args.expected_sha,
                )
            )

    if args.check_pypi and pypi_version_exists(metadata.distribution_name, metadata.version):
        issues.append(f"{metadata.distribution_name} {metadata.version} already exists on PyPI")

    if issues:
        print("Release check: FAIL")
        for issue in issues:
            print(f" - {issue}")
        return 1

    if args.write_checksums:
        manifest = write_checksums(args.dist_dir.resolve(), artifacts)
        print(f" - wrote: {manifest}")
    if args.write_semantic_eval_attestation is not None:
        assert semantic_report_path is not None
        assert args.expected_sha is not None
        try:
            attestation = write_semantic_eval_attestation(
                report_path=semantic_report_path,
                attestation_path=args.write_semantic_eval_attestation.resolve(),
                source_sha=args.expected_sha,
            )
        except (OSError, ValueError, KeyError) as exc:
            print("Release check: FAIL")
            print(f" - could not write semantic eval attestation: {exc}")
            return 1
        print(f" - wrote: {attestation}")
    print(f"Release check: PASS ({metadata.distribution_name} {metadata.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
