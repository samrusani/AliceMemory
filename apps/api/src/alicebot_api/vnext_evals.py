"""vNext eval harness.

Unlike the previous revision of this module -- which scored a synthetic
fixture generator against itself and could never fail -- every suite here
either executes real production code or reports ``status: "skipped"`` with a
reason. Nothing in this module fabricates a pass.

Suites:

- ``retrieval_quality``: seeds a deterministic paraphrase corpus through a
  real store write path and runs every query through the production hybrid
  retrieval pipeline (``VNextRetrievalService`` -- FTS + vector KNN fused
  with reciprocal-rank fusion). Reports recall@1 / recall@5 / MRR,
  per-query latency (p50/p95), which backend ran, and whether the vector
  stage was active or the run degraded to FTS-only. Requires a live store
  (``ALICEBOT_EVAL_DATABASE_URL`` or an injected store handle); without
  one the suite is skipped, never passed.

- ``correction_suppression``: seeds memories, then runs real correction
  flows through ``VNextMemoryCommitService`` (supersede A with B via the
  undo path; reject C via the inline-confirmation review path) and asserts
  through the production retrieval pipeline that superseded and rejected
  memories stop surfacing, that the replacement ranks, and that the audit
  trail on the superseded memory records the supersession.

- ``decision_recovery``: seeds ``memory_type='decision'`` rows among
  mixed-type distractors and measures recall@5 / MRR for decision-intent
  query phrasings through the production pipeline. When the retrieval
  request grows a ``memory_types`` filter parameter, a filtered variant is
  measured and reported alongside the unfiltered numbers.

- ``provenance_explanation``: commits memories through
  ``VNextMemoryCommitService`` with real source rows and provenance refs,
  corrects a subset, then audits every one (``VNextMemoryCommitService.audit``)
  asserting reasoned revisions, resolvable provenance links, the commit
  event in the trail, and corrections reflected in the audit.

Two backends are supported through ``ALICEBOT_EVAL_DATABASE_URL``:

- a Postgres URL runs ``PostgresVNextStore`` (Postgres FTS + pgvector);
- ``sqlite:///<path>`` or ``sqlite:///:memory:`` runs ``SQLiteVNextStore``
  (FTS5 + numpy cosine) -- the zero-infrastructure on-ramp backend. This
  is also how CI executes the live suite without external services.

Either way the report labels the backend in the suite metrics, so a
side-by-side comparison is just two runs with different URLs.

All seeded rows are written inside a rolled-back transaction (Postgres
``force_rollback``; an explicit ``BEGIN``/``ROLLBACK`` for SQLite), so a
live run leaves no data behind.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from typing import cast
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from alicebot_api.db import set_current_user
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_embeddings import (
    MAX_EMBEDDINGS_BATCH_SIZE,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    get_embedding_provider,
    memory_embedding_text,
    signed_memory_embedding_update,
)
from alicebot_api.vnext_memory_commit import (
    MemoryCommitRequest,
    VNextMemoryCommitService,
)
from alicebot_api.vnext_retrieval import (
    VECTOR_STAGE_ENABLED,
    VNextRetrievalRequest,
    VNextRetrievalService,
    VNextRetrievalStore,
)
from alicebot_api.vnext_store import PostgresVNextStore

logger = logging.getLogger(__name__)
EVAL_CASE_ERROR_CODE = "eval_case_failed"
EVAL_CASE_ERROR_MESSAGE = "The evaluation case could not be completed"
EMBEDDING_PROVIDER_FAILED_NOTE = "embedding failed, continuing FTS-only: embedding_provider_failed"
MEMORY_TYPES_FILTER_INCOMPATIBLE_NOTE = (
    "memory_types filter detected but incompatible: invalid_filter_signature"
)
VNEXT_EVAL_LIVE_STORE_UNAVAILABLE_REASON = "live store unavailable"
VNEXT_EVAL_UNSUPPORTED_SQLITE_URL_REASON = (
    "unsupported sqlite eval URL (expected sqlite:///<path> or sqlite:///:memory:)"
)

JsonObject = dict[str, object]
RetrievalFn = Callable[..., JsonObject]

VNEXT_EVAL_CORPUS_SCHEMA_VERSION = "vnext_eval_corpus_v1"
VNEXT_EVAL_REPORT_SCHEMA_VERSION = "vnext_eval_report_v1"
EMBEDDING_SIGNATURE_IDENTITY_SCHEMA_VERSION = "alice_embedding_signature_identity_v1"
VNEXT_EVAL_CORPUS_SOURCE_PATH = "eval/fixtures/vnext_benchmark_corpus.json"
VNEXT_EVAL_REPORT_PATH = "eval/reports/vnext_eval_latest.json"
VNEXT_EVAL_DATABASE_URL_ENV = "ALICEBOT_EVAL_DATABASE_URL"
VNEXT_EVAL_USER_ID_ENV = "ALICEBOT_AUTH_USER_ID"
VNEXT_EVAL_DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
VNEXT_EVAL_MEMORY_KEY_PREFIX = "vnext-eval/retrieval/"

RETRIEVAL_QUALITY_SUITE_KEY = "retrieval_quality"
CORRECTION_SUPPRESSION_SUITE_KEY = "correction_suppression"
DECISION_RECOVERY_SUITE_KEY = "decision_recovery"
PROVENANCE_EXPLANATION_SUITE_KEY = "provenance_explanation"
ENTITY_RESOLUTION_SUITE_KEY = "entity_resolution"
GRAPH_HOP_RETRIEVAL_SUITE_KEY = "graph_hop_retrieval"

RETRIEVAL_QUALITY_TITLE = "Retrieval quality (production hybrid pipeline)"

VNEXT_EVAL_SUITE_ORDER = (
    RETRIEVAL_QUALITY_SUITE_KEY,
    CORRECTION_SUPPRESSION_SUITE_KEY,
    DECISION_RECOVERY_SUITE_KEY,
    PROVENANCE_EXPLANATION_SUITE_KEY,
    ENTITY_RESOLUTION_SUITE_KEY,
    GRAPH_HOP_RETRIEVAL_SUITE_KEY,
)

VNEXT_EVAL_SQLITE_URL_PREFIX = "sqlite:///"

RETRIEVAL_QUALITY_RECALL_LIMIT = 10
VNEXT_EVAL_LIVE_STORE_SKIP_REASON = (
    "requires live store: pass a store handle or set "
    f"{VNEXT_EVAL_DATABASE_URL_ENV} to a migrated Postgres URL "
    "or a sqlite:///<path> URL (sqlite:///:memory: works)"
)
RETRIEVAL_QUALITY_SKIP_REASON = VNEXT_EVAL_LIVE_STORE_SKIP_REASON

SUBSET_LEXICAL_OVERLAP = "lexical_overlap"
SUBSET_PARAPHRASE = "paraphrase"

RETRIEVAL_QUALITY_TARGETS: JsonObject = {
    "lexical_overlap_recall_at_5": {"minimum": 0.80},
    "lexical_overlap_mrr": {"minimum": 0.60},
    "paraphrase_recall_at_5": {
        "minimum": 0.70,
        "enforced_only_when_vector_stage_enabled": True,
    },
}

CORRECTION_SUPPRESSION_TARGETS: JsonObject = {
    "pre_correction_visibility": {"minimum": 1.0},
    "suppression_rate": {"minimum": 1.0},
    "replacement_recall_at_5": {"minimum": 0.80},
    "audit_completeness": {"minimum": 1.0},
}

DECISION_RECOVERY_TARGETS: JsonObject = {
    "decision_recall_at_5": {"minimum": 0.80},
    "filtered_decision_recall_at_5": {
        "minimum": 0.80,
        "enforced_only_when_memory_types_filter_available": True,
    },
}

PROVENANCE_EXPLANATION_TARGETS: JsonObject = {
    "explain_completeness_rate": {"minimum": 1.0},
    "orphan_provenance_count": {"maximum": 0},
}

VNEXT_ACCEPTANCE_TARGETS: JsonObject = {
    RETRIEVAL_QUALITY_SUITE_KEY: deepcopy(RETRIEVAL_QUALITY_TARGETS),
    CORRECTION_SUPPRESSION_SUITE_KEY: deepcopy(CORRECTION_SUPPRESSION_TARGETS),
    DECISION_RECOVERY_SUITE_KEY: deepcopy(DECISION_RECOVERY_TARGETS),
    PROVENANCE_EXPLANATION_SUITE_KEY: deepcopy(PROVENANCE_EXPLANATION_TARGETS),
}

# Fixed validity window for directly-seeded rows. A sibling workstream is
# adding staleness demotion (status "stale" / expired ``valid_to`` filtering)
# to the search SQL; seeded corpora pin explicit, far-future validity and an
# explicit "active" status so that change cannot silently demote eval rows.
VNEXT_EVAL_FIXED_VALID_FROM = "2026-01-01T00:00:00Z"
VNEXT_EVAL_FIXED_VALID_TO = "2099-12-31T23:59:59Z"

VNEXT_EVAL_AGENT_ID = "vnext-eval-harness"

# Field the sibling retrieval workstream may add to VNextRetrievalRequest.
# Detected at runtime, never assumed.
MEMORY_TYPES_FILTER_FIELD = "memory_types"


# --------------------------------------------------------------------------
# Deterministic paraphrase corpus
#
# All content is derived from index arithmetic over hand-authored template
# pairs; there is no randomness at runtime. Every eval query is phrased
# differently from its target memory. The lexical-overlap subset rewords the
# fact while keeping most content vocabulary (FTS should handle it); the
# paraphrase subset restates the fact with near-zero shared vocabulary
# (FTS alone should struggle; the vector stage should recover it).
# --------------------------------------------------------------------------

_PROJECTS = (
    "Aurora",
    "Basilisk",
    "Cascade",
    "Dorado",
    "Ember",
    "Foxtrot",
    "Granite",
    "Harbor",
    "Icarus",
    "Juniper",
    "Kestrel",
    "Lumen",
)
_PEOPLE = (
    "Priya Nair",
    "Marcus Webb",
    "Elena Vasquez",
    "Tomas Lindqvist",
    "Ingrid Bauer",
    "Jamal Carter",
    "Sofia Marchetti",
    "Devon Blake",
    "Anaya Iyer",
    "Viktor Petrov",
    "Maren Holt",
    "Osei Mensah",
)
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# Each lexical-overlap template is expanded for several projects. The query
# rewords the memory (word order, inflection, dropped words) but introduces
# no new content lexeme, so Postgres FTS with stemming can still match while
# exact-token overlap stays in roughly the 0.6-0.85 band.
_LEXICAL_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "{first_name} {last_name} approved the {project} launch budget of {amount}k for {month}.",
        "what launch budget did {first_name} approve for {project}",
        "{project} launch budget",
    ),
    (
        "The {project} weekly sync moved to {weekday} mornings at 9am.",
        "when did the {project} weekly sync move to mornings",
        "{project} weekly sync",
    ),
    (
        "The {project} beta rollout is blocked on the payments integration review.",
        "what is blocking the {project} beta rollout",
        "{project} beta rollout",
    ),
    (
        "{first_name} {last_name} owns the {project} incident postmortem due on {weekday}.",
        "who owns the incident postmortems for {project}",
        "{project} incident postmortem",
    ),
    (
        "The {project} contract renewal with {first_name} {last_name} closes at {amount}k annual value.",
        "what annual value does the {project} contract renewal close at",
        "{project} contract renewal",
    ),
    (
        "The {project} error budget burned sixty percent after the deploy freeze lifted.",
        "how did the {project} error budget burn after the deploy freeze lift",
        "{project} error budget",
    ),
    (
        "{first_name} {last_name} scheduled the {project} architecture review for {month} 12.",
        "when did {first_name} schedule the {project} architecture review",
        "{project} architecture review",
    ),
    (
        "The {project} data migration finished with zero rows lost in {month}.",
        "did the {project} data migration finish with zero rows",
        "{project} data migration",
    ),
)
_LEXICAL_VARIANTS_PER_TEMPLATE = 4

# Hand-authored pure-paraphrase pairs: the query restates the fact with
# synonyms and reworded intent, sharing (near) zero content tokens with the
# memory. Each pair is a unique topic so the expected answer is unambiguous.
_PARAPHRASE_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "Q3 board pack is due Thursday, September 24.",
        "when is the quarterly board deck deadline",
        "Board pack due date",
    ),
    (
        "Sales offsite moved to the Lisbon office in early November.",
        "where will the revenue team gathering happen this fall",
        "Sales offsite location",
    ),
    (
        "New hires must complete security training within two weeks of their start date.",
        "how long do recent joiners have to finish the infosec course",
        "Security training window",
    ),
    (
        "The postgres upgrade to version 17 is planned for the first weekend of October.",
        "when are we bumping the database to the next major release",
        "Postgres upgrade timing",
    ),
    (
        "Customer churn dropped to four percent after the onboarding revamp.",
        "how did retention numbers change once the signup flow was redone",
        "Churn after onboarding revamp",
    ),
    (
        "Legal approved the updated data processing agreement for European customers.",
        "did counsel sign off on the new DPA for EU clients",
        "DPA approval",
    ),
    (
        "The mobile team froze feature work until the crash rate falls below one percent.",
        "why did app development pause new functionality",
        "Mobile feature freeze",
    ),
    (
        "The investor update goes out every Friday at noon.",
        "what day do backers receive the recurring status email",
        "Investor update cadence",
    ),
    (
        "Server costs doubled after the analytics pipeline moved to real-time processing.",
        "what happened to infrastructure spend when streaming was enabled",
        "Analytics cost increase",
    ),
    (
        "The design system migration finishes at the end of March.",
        "when does the UI component library switchover wrap up",
        "Design system migration",
    ),
    (
        "Support tickets about billing spiked after the pricing page redesign.",
        "which customer complaints increased following the new price layout",
        "Billing ticket spike",
    ),
    (
        "The hiring freeze exempts backend engineers and security roles.",
        "which positions can still be recruited during the headcount pause",
        "Hiring freeze exemptions",
    ),
    (
        "Beta invites are limited to fifty users weekly during the pilot.",
        "how many people can join the early access program each week",
        "Beta invite cap",
    ),
    (
        "The annual compliance audit starts on the second Monday of January.",
        "when do the yearly regulatory reviewers begin their inspection",
        "Compliance audit start",
    ),
    (
        "Marketing shifted the campaign budget from paid search to podcast sponsorships.",
        "where did the advertising money move away from adwords",
        "Campaign budget shift",
    ),
    (
        "The on-call rotation now includes the data platform engineers.",
        "who was recently added to the pager schedule",
        "On-call rotation change",
    ),
)

# Distractor memories share entities and business vocabulary with the query
# set but answer none of the queries.
_DISTRACTOR_TEMPLATES: tuple[str, ...] = (
    "{first_name} {last_name} joined the {project} steering committee in {month}.",
    "{project} standup notes are archived in the shared drive every Friday.",
    "Expense reports for {project} travel must be filed within thirty days.",
    "{first_name} {last_name} presented the {project} quarterly metrics at the all-hands.",
    "The {project} staging environment refreshes nightly at 2am.",
    "{first_name} {last_name} rotated onto {project} on-call for the {month} cycle.",
    "{project} documentation moved to the new wiki space.",
    "Vendor invoices for {project} route through {first_name} {last_name} for signoff.",
    "The {project} test suite runs on every merge to main.",
    "{first_name} {last_name} drafted the {project} hiring plan for two senior roles.",
    "{project} customer feedback is triaged every Tuesday afternoon.",
    "The {project} demo environment password rotates monthly.",
    "{first_name} {last_name} flagged a licensing question on the {project} dependency audit.",
    "{project} release notes are published the first Monday of each month.",
)
_DISTRACTOR_VARIANTS_PER_TEMPLATE = 12

VNEXT_BENCHMARK_EXPECTED_COUNTS: JsonObject = {
    "memories": len(_LEXICAL_TEMPLATES) * _LEXICAL_VARIANTS_PER_TEMPLATE
    + len(_PARAPHRASE_PAIRS)
    + len(_DISTRACTOR_TEMPLATES) * _DISTRACTOR_VARIANTS_PER_TEMPLATE,
    "queries": len(_LEXICAL_TEMPLATES) * _LEXICAL_VARIANTS_PER_TEMPLATE + len(_PARAPHRASE_PAIRS),
    "lexical_overlap_queries": len(_LEXICAL_TEMPLATES) * _LEXICAL_VARIANTS_PER_TEMPLATE,
    "paraphrase_queries": len(_PARAPHRASE_PAIRS),
    "distractor_memories": len(_DISTRACTOR_TEMPLATES) * _DISTRACTOR_VARIANTS_PER_TEMPLATE,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _default_corpus_path() -> Path:
    return _repo_root() / VNEXT_EVAL_CORPUS_SOURCE_PATH


def _default_report_path() -> Path:
    return _repo_root() / VNEXT_EVAL_REPORT_PATH


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _report_digest_payload(report: Mapping[str, object]) -> JsonObject:
    """Return the canonical, time-independent semantic report payload.

    ``generated_at`` is transport metadata rather than eval evidence, and the
    digest cannot include itself. Every other report field is deliberately
    bound: corpus identity, declared targets, ordered suites, per-case
    metrics/evidence, skipped-suite state, summary, and embedding identity.
    Keeping this projection exhaustive makes newly added semantic fields fail
    verification until the producer and release checker agree on them.
    """
    return {
        key: deepcopy(value)
        for key, value in report.items()
        if key not in {"generated_at", "report_digest"}
    }


def semantic_eval_report_digest(report: Mapping[str, object]) -> str:
    """Hash a semantic report using the canonical ``sha256:<hex>`` form."""
    return _hash_payload(_report_digest_payload(report))


def _memory_key(kind: str, index: int) -> str:
    return f"{VNEXT_EVAL_MEMORY_KEY_PREFIX}{kind}-{index:03d}"


def _template_fields(template_index: int, variant_index: int) -> dict[str, str]:
    project = _PROJECTS[(template_index * _LEXICAL_VARIANTS_PER_TEMPLATE + variant_index * 5) % len(_PROJECTS)]
    person = _PEOPLE[(template_index * 5 + variant_index * 3 + 1) % len(_PEOPLE)]
    first_name, last_name = person.split(" ", 1)
    return {
        "project": project,
        "first_name": first_name,
        "last_name": last_name,
        "month": _MONTHS[(template_index * 3 + variant_index) % len(_MONTHS)],
        "weekday": _WEEKDAYS[(template_index + variant_index) % len(_WEEKDAYS)],
        "amount": str(40 + ((template_index * 7 + variant_index * 11) % 50) * 5),
    }


def _corpus_memory(*, memory_key: str, title: str, canonical_text: str, role: str) -> JsonObject:
    return {
        "memory_key": memory_key,
        "title": title,
        "canonical_text": canonical_text,
        "domain": "professional",
        "sensitivity": "internal",
        "memory_type": "semantic",
        "status": "active",
        "role": role,
    }


def generate_vnext_benchmark_corpus() -> JsonObject:
    """Build the deterministic retrieval-quality corpus (no runtime random)."""
    memories: list[JsonObject] = []
    queries: list[JsonObject] = []

    lexical_index = 0
    for template_index, (memory_template, query_template, title_template) in enumerate(_LEXICAL_TEMPLATES):
        for variant_index in range(_LEXICAL_VARIANTS_PER_TEMPLATE):
            lexical_index += 1
            fields = _template_fields(template_index, variant_index)
            memory_key = _memory_key("lexical", lexical_index)
            memories.append(
                _corpus_memory(
                    memory_key=memory_key,
                    title=title_template.format(**fields),
                    canonical_text=memory_template.format(**fields),
                    role="target",
                )
            )
            queries.append(
                {
                    "query_key": f"lexical-{lexical_index:03d}",
                    "query": query_template.format(**fields),
                    "expected_memory_key": memory_key,
                    "subset": SUBSET_LEXICAL_OVERLAP,
                }
            )

    for pair_index, (memory_text, query_text, title) in enumerate(_PARAPHRASE_PAIRS, start=1):
        memory_key = _memory_key("paraphrase", pair_index)
        memories.append(
            _corpus_memory(
                memory_key=memory_key,
                title=title,
                canonical_text=memory_text,
                role="target",
            )
        )
        queries.append(
            {
                "query_key": f"paraphrase-{pair_index:03d}",
                "query": query_text,
                "expected_memory_key": memory_key,
                "subset": SUBSET_PARAPHRASE,
            }
        )

    distractor_index = 0
    for template_index, template in enumerate(_DISTRACTOR_TEMPLATES):
        for variant_index in range(_DISTRACTOR_VARIANTS_PER_TEMPLATE):
            distractor_index += 1
            project = _PROJECTS[variant_index % len(_PROJECTS)]
            person = _PEOPLE[(variant_index + template_index) % len(_PEOPLE)]
            first_name, last_name = person.split(" ", 1)
            fields = {
                "project": project,
                "first_name": first_name,
                "last_name": last_name,
                "month": _MONTHS[(template_index + variant_index * 2) % len(_MONTHS)],
            }
            memories.append(
                _corpus_memory(
                    memory_key=_memory_key("distractor", distractor_index),
                    title=f"{project} operational note {distractor_index:03d}",
                    canonical_text=template.format(**fields),
                    role="distractor",
                )
            )

    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": RETRIEVAL_QUALITY_SUITE_KEY,
        "counts": {
            "memories": len(memories),
            "queries": len(queries),
            "lexical_overlap_queries": sum(1 for query in queries if query["subset"] == SUBSET_LEXICAL_OVERLAP),
            "paraphrase_queries": sum(1 for query in queries if query["subset"] == SUBSET_PARAPHRASE),
            "distractor_memories": sum(1 for memory in memories if memory["role"] == "distractor"),
        },
        "memories": memories,
        "queries": queries,
    }
    corpus["corpus_digest"] = _hash_payload({"memories": memories, "queries": queries})
    return corpus


def load_vnext_benchmark_corpus(corpus_path: str | Path | None = None) -> JsonObject:
    path = Path(corpus_path) if corpus_path is not None else _default_corpus_path()
    if not path.exists():
        return generate_vnext_benchmark_corpus()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("vNext eval corpus must be a JSON object")
    if payload.get("schema_version") != VNEXT_EVAL_CORPUS_SCHEMA_VERSION:
        raise ValueError("unexpected vNext eval corpus schema version")
    return cast(JsonObject, payload)


def write_vnext_benchmark_corpus(corpus_path: str | Path | None = None) -> Path:
    path = Path(corpus_path) if corpus_path is not None else _default_corpus_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    corpus = generate_vnext_benchmark_corpus()
    path.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.resolve()


# --------------------------------------------------------------------------
# Metric math (pure functions, unit-tested against known rankings)
# --------------------------------------------------------------------------

_EVAL_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "during",
        "each",
        "for",
        "from",
        "had",
        "has",
        "have",
        "here",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "many",
        "much",
        "next",
        "no",
        "not",
        "now",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "per",
        "should",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "those",
        "to",
        "too",
        "under",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
    }
)


def eval_content_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if len(token) > 1 and token not in _EVAL_TOKEN_STOPWORDS
    }


def eval_token_overlap(query: str, memory_text: str) -> float:
    """Fraction of the query's content tokens that appear verbatim in the memory."""
    query_tokens = eval_content_tokens(query)
    if not query_tokens:
        return 0.0
    return len(query_tokens & eval_content_tokens(memory_text)) / len(query_tokens)


def recall_at_k(ranked_ids: Sequence[str], expected_id: str, k: int) -> float:
    if k < 1:
        raise ValueError("recall@k requires k >= 1")
    return 1.0 if expected_id in list(ranked_ids)[:k] else 0.0


def reciprocal_rank(ranked_ids: Sequence[str], expected_id: str) -> float:
    for rank, ranked_id in enumerate(ranked_ids, start=1):
        if ranked_id == expected_id:
            return 1.0 / rank
    return 0.0


def latency_percentile(values: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentile; deterministic and dependency-free."""
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[rank - 1]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------
# Retrieval-quality suite: production pipeline execution
# --------------------------------------------------------------------------


def seed_retrieval_corpus(store: object, corpus: JsonObject) -> JsonObject:
    """Write the corpus memories through the real store write path.

    Embeds through the configured embedding provider when one is available so
    the vector stage participates; embedding failure degrades to FTS-only and
    is reported, never hidden.
    """
    create_memory = getattr(store, "create_memory", None)
    if not callable(create_memory):
        raise ValueError("retrieval-quality eval store must expose create_memory")
    memories = [cast(JsonObject, item) for item in cast(list[object], corpus.get("memories", [])) if isinstance(item, dict)]
    created_rows: list[JsonObject] = []
    for memory in memories:
        created_rows.append(
            cast(
                JsonObject,
                create_memory(
                    {
                        "memory_key": memory["memory_key"],
                        "value": {"text": memory["canonical_text"]},
                        "status": memory.get("status", "active"),
                        "memory_type": memory.get("memory_type", "semantic"),
                        "title": memory.get("title"),
                        "canonical_text": memory["canonical_text"],
                        "domain": memory.get("domain", "professional"),
                        "sensitivity": memory.get("sensitivity", "internal"),
                    }
                ),
            )
        )

    embedded_count = 0
    embedding_signature: JsonObject | None = None
    embedding_note = "no embedding provider configured; vector stage inactive"
    provider = get_embedding_provider()
    update_memory_embedding = getattr(store, "update_memory_embedding", None)
    if provider is not None and callable(update_memory_embedding):
        try:
            for batch_start in range(0, len(created_rows), MAX_EMBEDDINGS_BATCH_SIZE):
                batch = created_rows[batch_start : batch_start + MAX_EMBEDDINGS_BATCH_SIZE]
                vectors = provider.embed_batch([memory_embedding_text(row) for row in batch])
                for row, vector in zip(batch, vectors, strict=True):
                    signed_update = signed_memory_embedding_update(row, vector, provider=provider)
                    update_memory_embedding(**signed_update)
                    if embedding_signature is None:
                        embedding_signature = {
                            "schema_version": EMBEDDING_SIGNATURE_IDENTITY_SCHEMA_VERSION,
                            "signature_version": signed_update["signature_version"],
                            "provider": signed_update["provider"],
                            "provider_fingerprint": sha256(
                                signed_update["provider"].encode("utf-8")
                            ).hexdigest(),
                            "model": signed_update["model"],
                            "model_fingerprint": sha256(
                                signed_update["model"].encode("utf-8")
                            ).hexdigest(),
                            "endpoint_fingerprint": signed_update["endpoint"],
                        }
                    embedded_count += 1
            embedding_note = f"embedded via {provider.provider}/{provider.model}"
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            logger.warning(
                "Retrieval evaluation corpus embedding failed error_code=embedding_provider_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            embedding_note = EMBEDDING_PROVIDER_FAILED_NOTE
    elif provider is not None:
        embedding_note = "store does not support update_memory_embedding; vector stage inactive"

    return {
        "seeded_memory_count": len(created_rows),
        "embedded_memory_count": embedded_count,
        "embedding_signature": embedding_signature,
        "embedding_note": embedding_note,
    }


def production_retrieval_fn(store: object) -> RetrievalFn:
    """Per-query closure over the real hybrid retrieval pipeline."""
    service = VNextRetrievalService(cast(VNextRetrievalStore, store))

    def _retrieve(query: str, *, limit: int) -> JsonObject:
        pack = service.compile_context_pack(
            VNextRetrievalRequest(
                query=query,
                max_items=limit,
                include_sources=False,
                include_contradictions=False,
                actor_type="system",
            )
        )
        relevant = cast(list[JsonObject], pack.get("relevant_memories", []))
        trace = cast(JsonObject, pack.get("trace", {}))
        stages = trace.get("stages")
        vector_stage = (
            cast(Mapping[str, object], stages).get("vector")
            if isinstance(stages, Mapping)
            else None
        )
        return {
            "ranked_memory_keys": [str(item["memory_key"]) for item in relevant if item.get("memory_key")],
            "vector_stage": trace.get("vector_stage", "unknown"),
            "vector_candidate_count": (
                cast(Mapping[str, object], vector_stage).get("candidate_count", 0)
                if isinstance(vector_stage, Mapping)
                else 0
            ),
        }

    return _retrieve


def _skipped_retrieval_suite(reason: str) -> JsonObject:
    return {
        "suite_key": RETRIEVAL_QUALITY_SUITE_KEY,
        "title": RETRIEVAL_QUALITY_TITLE,
        "status": "skipped",
        "reason": reason,
        "targets": deepcopy(RETRIEVAL_QUALITY_TARGETS),
        "metrics": {"query_count": 0},
        "cases": [],
    }


def _eval_backend_label(store: object | None, backend: str | None) -> str:
    """Name the backend a suite ran against, for the report."""
    if backend is not None:
        return backend
    if isinstance(store, SQLiteVNextStore):
        return "sqlite"
    if isinstance(store, PostgresVNextStore):
        return "postgres"
    return "injected"


def run_retrieval_quality_eval(
    store: object | None,
    *,
    retrieval_fn: RetrievalFn | None = None,
    corpus: JsonObject | None = None,
    recall_limit: int = RETRIEVAL_QUALITY_RECALL_LIMIT,
    backend: str | None = None,
    release_gate: bool = False,
) -> JsonObject:
    """Execute the retrieval-quality suite against the production pipeline.

    With a live ``store`` handle the corpus is seeded through the real write
    path and every query runs through ``VNextRetrievalService``. A
    ``retrieval_fn`` may be injected for metric-math testing. With neither,
    the suite reports ``status: "skipped"`` -- never a fabricated pass.

    ``backend`` labels the run in the metrics ("postgres" / "sqlite");
    when omitted it is inferred from the store type, and injected
    ``retrieval_fn`` runs are labelled "injected".

    ``release_gate`` marks a canonical/release-designated run. In that mode a
    run that never exercised the vector stage (so the paraphrase/semantic
    target was not enforced) is reported as ``"pass_fts_only"`` instead of an
    unqualified ``"pass"`` -- the release gate cannot be green without
    measuring semantic retrieval quality. Dev-facing runs (the default) keep
    their existing ``"pass"`` semantics.
    """
    resolved_corpus = corpus if corpus is not None else generate_vnext_benchmark_corpus()
    queries = [
        cast(JsonObject, item)
        for item in cast(list[object], resolved_corpus.get("queries", []))
        if isinstance(item, dict)
    ]
    if not queries:
        raise ValueError("retrieval-quality corpus must include queries")

    seeding: JsonObject | None = None
    if retrieval_fn is None:
        if store is None:
            return _skipped_retrieval_suite(RETRIEVAL_QUALITY_SKIP_REASON)
        seeding = seed_retrieval_corpus(store, resolved_corpus)
        retrieval_fn = production_retrieval_fn(store)

    cases: list[JsonObject] = []
    latencies_ms: list[float] = []
    vector_stages: set[str] = set()
    vector_candidate_counts: list[int] = []
    for query in queries:
        query_text = str(query["query"])
        expected_key = str(query["expected_memory_key"])
        subset = str(query.get("subset", SUBSET_LEXICAL_OVERLAP))
        started = perf_counter()
        result = retrieval_fn(query_text, limit=recall_limit)
        latency_ms = (perf_counter() - started) * 1000.0
        latencies_ms.append(latency_ms)
        ranked_keys = [str(key) for key in cast(list[object], result.get("ranked_memory_keys", []))]
        vector_stages.add(str(result.get("vector_stage", "unknown")))
        candidate_count = result.get("vector_candidate_count", 0)
        vector_candidate_count = (
            candidate_count
            if isinstance(candidate_count, int)
            and not isinstance(candidate_count, bool)
            and candidate_count > 0
            else 0
        )
        vector_candidate_counts.append(vector_candidate_count)
        case_recall_at_1 = recall_at_k(ranked_keys, expected_key, 1)
        case_recall_at_5 = recall_at_k(ranked_keys, expected_key, 5)
        case_reciprocal_rank = reciprocal_rank(ranked_keys, expected_key)
        cases.append(
            {
                "case_key": str(query["query_key"]),
                "subset": subset,
                "status": "pass" if case_recall_at_5 == 1.0 else "fail",
                "metrics": {
                    "recall_at_1": case_recall_at_1,
                    "recall_at_5": case_recall_at_5,
                    "reciprocal_rank": case_reciprocal_rank,
                    "latency_ms": round(latency_ms, 3),
                },
                "evidence": {
                    "query": query_text,
                    "expected_memory_key": expected_key,
                    "top_memory_keys": ranked_keys[:5],
                    "vector_stage": result.get("vector_stage", "unknown"),
                    "vector_candidate_count": vector_candidate_count,
                },
            }
        )

    vector_stage_enabled = vector_stages == {VECTOR_STAGE_ENABLED}
    vector_candidate_count = sum(vector_candidate_counts)
    vector_queries_with_candidates = sum(
        1 for candidate_count in vector_candidate_counts if candidate_count > 0
    )
    vector_stage_participated = (
        vector_stage_enabled
        and bool(vector_candidate_counts)
        and vector_queries_with_candidates == len(vector_candidate_counts)
    )
    if vector_stage_enabled:
        retrieval_mode = "hybrid"
    elif VECTOR_STAGE_ENABLED in vector_stages:
        retrieval_mode = "mixed"
    else:
        retrieval_mode = "fts_only"

    def _subset_metrics(subset: str) -> JsonObject:
        subset_cases = [case for case in cases if case["subset"] == subset]
        metrics = [cast(JsonObject, case["metrics"]) for case in subset_cases]
        return {
            "query_count": len(subset_cases),
            "recall_at_1": _mean([cast(float, metric["recall_at_1"]) for metric in metrics]),
            "recall_at_5": _mean([cast(float, metric["recall_at_5"]) for metric in metrics]),
            "mrr": _mean([cast(float, metric["reciprocal_rank"]) for metric in metrics]),
        }

    lexical_metrics = _subset_metrics(SUBSET_LEXICAL_OVERLAP)
    paraphrase_metrics = _subset_metrics(SUBSET_PARAPHRASE)
    all_metrics = [cast(JsonObject, case["metrics"]) for case in cases]

    lexical_recall_target = cast(dict[str, float], RETRIEVAL_QUALITY_TARGETS["lexical_overlap_recall_at_5"])["minimum"]
    lexical_mrr_target = cast(dict[str, float], RETRIEVAL_QUALITY_TARGETS["lexical_overlap_mrr"])["minimum"]
    paraphrase_recall_target = cast(dict[str, float], RETRIEVAL_QUALITY_TARGETS["paraphrase_recall_at_5"])["minimum"]

    checks: dict[str, bool] = {
        "lexical_overlap_recall_at_5": cast(float, lexical_metrics["recall_at_5"]) >= lexical_recall_target,
        "lexical_overlap_mrr": cast(float, lexical_metrics["mrr"]) >= lexical_mrr_target,
    }
    # The paraphrase subset is designed so FTS alone struggles; the target is
    # only enforced when the vector stage actually ran. In FTS-only mode the
    # paraphrase numbers are still reported, honestly, as degraded coverage.
    if vector_stage_enabled:
        checks["paraphrase_recall_at_5"] = cast(float, paraphrase_metrics["recall_at_5"]) >= paraphrase_recall_target

    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "query_count": len(cases),
        "recall_at_1": _mean([cast(float, metric["recall_at_1"]) for metric in all_metrics]),
        "recall_at_5": _mean([cast(float, metric["recall_at_5"]) for metric in all_metrics]),
        "mrr": _mean([cast(float, metric["reciprocal_rank"]) for metric in all_metrics]),
        "latency_ms": {
            "p50": round(latency_percentile(latencies_ms, 50), 3),
            "p95": round(latency_percentile(latencies_ms, 95), 3),
            "max": round(max(latencies_ms), 3),
        },
        "vector_stages": sorted(vector_stages),
        "vector_candidate_count": vector_candidate_count,
        "vector_query_count": len(vector_candidate_counts),
        "vector_queries_with_candidates": vector_queries_with_candidates,
        "vector_stage_participated": vector_stage_participated,
        "retrieval_mode": retrieval_mode,
        "paraphrase_targets_enforced": vector_stage_enabled,
        "release_gate": release_gate,
        "subsets": {
            SUBSET_LEXICAL_OVERLAP: lexical_metrics,
            SUBSET_PARAPHRASE: paraphrase_metrics,
        },
        "target_checks": {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())},
    }
    if seeding is not None:
        metrics["seeding"] = seeding

    if all(checks.values()):
        # A release-designated run that never exercised the vector stage did not
        # measure paraphrase/semantic quality, so it cannot claim a full pass.
        status = "pass_fts_only" if (release_gate and not vector_stage_participated) else "pass"
    else:
        status = "fail"

    return {
        "suite_key": RETRIEVAL_QUALITY_SUITE_KEY,
        "title": RETRIEVAL_QUALITY_TITLE,
        "status": status,
        "targets": deepcopy(RETRIEVAL_QUALITY_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


def _eval_user_id() -> UUID:
    return UUID(os.environ.get(VNEXT_EVAL_USER_ID_ENV, "").strip() or VNEXT_EVAL_DEFAULT_USER_ID)


@contextmanager
def _ephemeral_eval_store(database_url: str) -> Iterator[PostgresVNextStore]:
    """Live store scoped to a forced-rollback transaction: no data persists."""
    user_id = _eval_user_id()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            set_current_user(conn, user_id)
            with conn.cursor() as cur:
                # Seeded memories require an acting user row; it rolls back
                # with everything else.
                cur.execute(
                    "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (user_id, f"vnext-eval+{user_id}@example.invalid", "vNext Eval"),
                )
            yield PostgresVNextStore(conn)


@contextmanager
def _ephemeral_sqlite_eval_store(database_url: str) -> Iterator[SQLiteVNextStore]:
    """Live SQLite store scoped to one rolled-back transaction.

    Mirrors the Postgres ``force_rollback`` approach: the schema bootstrap
    runs in autocommit (a file-backed run leaves empty tables behind, no
    rows), then the eval user row, every seeded memory, and all event-log
    writes happen inside a single explicit ``BEGIN`` that is rolled back
    before the connection closes. The FTS5 index is maintained by triggers
    inside that same transaction, so its shadow-table writes roll back with
    it (covered by a unit test that reopens the file and probes MATCH).
    """
    user_id = str(_eval_user_id())
    path = database_url.removeprefix(VNEXT_EVAL_SQLITE_URL_PREFIX)
    conn = sqlite3.connect(path)
    conn.isolation_level = None  # explicit transaction control below
    conn.row_factory = sqlite3.Row
    try:
        bootstrap_sqlite_schema(conn)  # autocommit DDL, idempotent
        conn.execute("BEGIN")
        # Seeded memories require an acting user row; it rolls back with
        # everything else.
        ensure_sqlite_user(conn, user_id, f"vnext-eval+{user_id}@example.invalid", "vNext Eval")
        yield SQLiteVNextStore(conn, user_id)
    finally:
        conn.rollback()
        conn.close()


def _run_suite_against_live_store(
    *,
    run_with_store: Callable[..., JsonObject],
    skipped: Callable[[str], JsonObject],
) -> JsonObject:
    """Resolve ``ALICEBOT_EVAL_DATABASE_URL`` into a rolled-back live store.

    ``run_with_store(store, backend=...)`` executes the suite; every
    non-executable path funnels into ``skipped(reason)`` -- never a
    fabricated pass. Each call opens (and rolls back) its own transaction,
    so suites stay isolated from one another.
    """
    database_url = os.environ.get(VNEXT_EVAL_DATABASE_URL_ENV, "").strip()
    if database_url == "":
        return skipped(VNEXT_EVAL_LIVE_STORE_SKIP_REASON)
    if database_url.startswith("sqlite:"):
        if not database_url.startswith(VNEXT_EVAL_SQLITE_URL_PREFIX) or database_url == VNEXT_EVAL_SQLITE_URL_PREFIX:
            return skipped(VNEXT_EVAL_UNSUPPORTED_SQLITE_URL_REASON)
        try:
            with _ephemeral_sqlite_eval_store(database_url) as live_store:
                return run_with_store(live_store, backend="sqlite")
        except sqlite3.Error as exc:
            logger.error(
                "SQLite evaluation store was unavailable",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return skipped(VNEXT_EVAL_LIVE_STORE_UNAVAILABLE_REASON)
    try:
        with _ephemeral_eval_store(database_url) as live_store:
            return run_with_store(live_store, backend="postgres")
    except psycopg.Error as exc:
        logger.error(
            "Postgres evaluation store was unavailable",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return skipped(VNEXT_EVAL_LIVE_STORE_UNAVAILABLE_REASON)


def _run_retrieval_quality_suite(
    *,
    corpus: JsonObject,
    store: object | None,
    retrieval_fn: RetrievalFn | None,
    release_gate: bool = False,
) -> JsonObject:
    if store is not None or retrieval_fn is not None:
        return run_retrieval_quality_eval(
            store, retrieval_fn=retrieval_fn, corpus=corpus, release_gate=release_gate
        )

    def _run(live_store: object, *, backend: str | None = None) -> JsonObject:
        return run_retrieval_quality_eval(
            live_store, corpus=corpus, backend=backend, release_gate=release_gate
        )

    return _run_suite_against_live_store(run_with_store=_run, skipped=_skipped_retrieval_suite)


# --------------------------------------------------------------------------
# Shared memory-quality suite machinery
#
# The three suites below drive the REAL commit/review service
# (``VNextMemoryCommitService``) and the REAL retrieval pipeline. Sibling
# workstreams are actively changing the retrieval/store/commit modules, so
# everything here is written defensively:
#
# - store surfaces are duck-type checked up front (missing surface => an
#   explicit skip reason, and the live unit tests assert "pass", so a
#   silently skipping suite still fails CI);
# - per-case execution catches exceptions and records them as failing
#   cases with evidence instead of aborting the whole report;
# - directly-seeded rows pin explicit status/validity values so the
#   sibling's staleness-demotion change cannot demote fresh eval rows;
# - the ``memory_types`` retrieval filter is feature-detected at runtime.
# --------------------------------------------------------------------------


def _skipped_suite(suite_key: str, title: str, targets: JsonObject, reason: str) -> JsonObject:
    return {
        "suite_key": suite_key,
        "title": title,
        "status": "skipped",
        "reason": reason,
        "targets": deepcopy(targets),
        "metrics": {"case_count": 0},
        "cases": [],
    }


def _target_checks(metrics: JsonObject, targets: JsonObject, *, skip_keys: set[str] | None = None) -> dict[str, bool]:
    """Evaluate ``minimum`` / ``maximum`` targets against computed metrics."""
    checks: dict[str, bool] = {}
    for key, spec in targets.items():
        if skip_keys is not None and key in skip_keys:
            continue
        if not isinstance(spec, Mapping):
            continue
        value = metrics.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            checks[key] = False
            continue
        passed = True
        minimum = spec.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            passed = False
        maximum = spec.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            passed = False
        checks[key] = passed
    return checks


def _missing_store_surface(store: object, required: Sequence[str]) -> list[str]:
    return [name for name in required if not callable(getattr(store, name, None))]


def _eval_agent_identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id=VNEXT_EVAL_AGENT_ID,
        agent_type="workflow_agent",
        permission_profile="trusted_local_agent",
        auth="unauthenticated_local",
    )


def _eval_commit_service(store: object) -> VNextMemoryCommitService:
    # The service is duck-typed over the store surface; both
    # PostgresVNextStore and SQLiteVNextStore satisfy it.
    return VNextMemoryCommitService(cast(PostgresVNextStore, store))


def _commit_active_memory(
    service: VNextMemoryCommitService,
    *,
    title: str,
    text: str,
    memory_type: str = "semantic",
    confidence: float = 0.95,
    idempotency_key: str | None = None,
    source_refs: tuple[object, ...] = (),
    rationale: str | None = None,
) -> JsonObject:
    """Commit through the real service; returns the raw service response."""
    request = MemoryCommitRequest(
        user_id=str(_eval_user_id()),
        title=title,
        canonical_text=text,
        memory_type=memory_type,
        domain="professional",
        sensitivity="internal",
        confidence=confidence,
        intent="explicit_remember",
        source_type="direct_user_instruction",
        source_refs=source_refs,
        rationale=rationale,
        idempotency_key=idempotency_key,
    )
    return service.commit(identity=_eval_agent_identity(), request=request)


def _seed_direct_memory(
    store: object,
    *,
    memory_key: str,
    title: str,
    text: str,
    memory_type: str = "semantic",
) -> JsonObject:
    """Seed one row through the real ``create_memory`` write path.

    Pins explicit non-expired validity and an explicit active status so the
    sibling staleness-demotion change cannot flake the suites.
    """
    create_memory = cast(Callable[..., JsonObject], getattr(store, "create_memory"))
    return create_memory(
        {
            "memory_key": memory_key,
            "value": {"text": text},
            "status": "active",
            "confirmation_status": "confirmed",
            "memory_type": memory_type,
            "title": title,
            "canonical_text": text,
            "domain": "professional",
            "sensitivity": "internal",
            "valid_from": VNEXT_EVAL_FIXED_VALID_FROM,
            "valid_to": VNEXT_EVAL_FIXED_VALID_TO,
        }
    )


def retrieval_request_supports_memory_types() -> bool:
    """True when the sibling's ``memory_types`` filter parameter has landed."""
    return any(field.name == MEMORY_TYPES_FILTER_FIELD for field in dataclass_fields(VNextRetrievalRequest))


def filtered_retrieval_fn(store: object, memory_types: tuple[str, ...]) -> RetrievalFn | None:
    """Retrieval closure with the ``memory_types`` filter applied, when available."""
    if not retrieval_request_supports_memory_types():
        return None
    service = VNextRetrievalService(cast(VNextRetrievalStore, store))

    def _retrieve(query: str, *, limit: int) -> JsonObject:
        request_kwargs: dict[str, object] = {
            "query": query,
            "max_items": limit,
            "include_sources": False,
            "include_contradictions": False,
            "actor_type": "system",
            MEMORY_TYPES_FILTER_FIELD: memory_types,
        }
        pack = service.compile_context_pack(VNextRetrievalRequest(**request_kwargs))  # type: ignore[arg-type]
        relevant = cast(list[JsonObject], pack.get("relevant_memories", []))
        trace = cast(JsonObject, pack.get("trace", {}))
        return {
            "ranked_memory_keys": [str(item["memory_key"]) for item in relevant if item.get("memory_key")],
            "vector_stage": trace.get("vector_stage", "unknown"),
        }

    return _retrieve


# --------------------------------------------------------------------------
# Correction-suppression suite
#
# Deterministic triplets on topics disjoint from the retrieval-quality
# corpus. Per case: commit A (stale fact), verify it surfaces, commit B
# (replacement), supersede A referencing B, commit C at medium confidence
# (inline-confirmation review path) and reject it, then assert through the
# production pipeline that A and C are gone, B ranks, and A's audit trail
# records the supersession.
# --------------------------------------------------------------------------

CORRECTION_SUPPRESSION_TITLE = "Correction suppression (commit service + production retrieval)"
CORRECTION_MEMORY_KEY_PREFIX = "vnext-eval/correction/"

_CORRECTION_CASES: tuple[dict[str, str], ...] = (
    {
        "case_key": "correction-001",
        "topic": "meridian-launch-window",
        "original_title": "Meridian launch window",
        "original_text": "The Meridian launch window is set for March 14 at 9am.",
        "replacement_title": "Meridian launch window (corrected)",
        "replacement_text": "The Meridian launch window moved to April 2 at 9am after the slip.",
        "rejected_title": "Meridian launch window rumor",
        "rejected_text": "The Meridian launch window may slip to June pending vendor review.",
        "query": "when is the Meridian launch window",
        "old_probe": "Meridian launch window March 14",
        "reject_probe": "Meridian launch window vendor review",
    },
    {
        "case_key": "correction-002",
        "topic": "nimbus-oncall-owner",
        "original_title": "Nimbus on-call owner",
        "original_text": "Priya Nair owns the Nimbus on-call rotation for the winter cycle.",
        "replacement_title": "Nimbus on-call owner (corrected)",
        "replacement_text": "Devon Blake owns the Nimbus on-call rotation after the winter handoff.",
        "rejected_title": "Nimbus on-call rumor",
        "rejected_text": "Marcus Webb may take the Nimbus on-call rotation next quarter.",
        "query": "who owns the Nimbus on-call rotation",
        "old_probe": "Priya Nair Nimbus on-call rotation",
        "reject_probe": "Nimbus on-call rotation next quarter",
    },
    {
        "case_key": "correction-003",
        "topic": "oriole-budget",
        "original_title": "Oriole platform budget",
        "original_text": "The Oriole platform budget is approved at 120k for this fiscal year.",
        "replacement_title": "Oriole platform budget (corrected)",
        "replacement_text": "The Oriole platform budget was revised to 95k for this fiscal year.",
        "rejected_title": "Oriole budget speculation",
        "rejected_text": "The Oriole platform budget could rise to 150k if headcount doubles.",
        "query": "what is the Oriole platform budget for the fiscal year",
        "old_probe": "Oriole platform budget 120k",
        "reject_probe": "Oriole platform budget 150k headcount",
    },
    {
        "case_key": "correction-004",
        "topic": "quill-standup-time",
        "original_title": "Quill design standup",
        "original_text": "The Quill design standup happens on Tuesdays at 10am.",
        "replacement_title": "Quill design standup (corrected)",
        "replacement_text": "The Quill design standup moved to Thursdays at 2pm.",
        "rejected_title": "Quill standup speculation",
        "rejected_text": "The Quill design standup might merge with the platform sync.",
        "query": "when is the Quill design standup",
        "old_probe": "Quill design standup Tuesdays",
        "reject_probe": "Quill design standup platform sync",
    },
    {
        "case_key": "correction-005",
        "topic": "sable-vendor-contract",
        "original_title": "Sable data vendor contract",
        "original_text": "The Sable data vendor contract renews with Northwind in September.",
        "replacement_title": "Sable data vendor contract (corrected)",
        "replacement_text": "The Sable data vendor contract switches to Contoso in September.",
        "rejected_title": "Sable vendor speculation",
        "rejected_text": "The Sable data vendor contract may add a second regional supplier.",
        "query": "who is the Sable data vendor contract with in September",
        "old_probe": "Sable data vendor Northwind",
        "reject_probe": "Sable data vendor regional supplier",
    },
    {
        "case_key": "correction-006",
        "topic": "tundra-release-cadence",
        "original_title": "Tundra release cadence",
        "original_text": "The Tundra release train ships every two weeks.",
        "replacement_title": "Tundra release cadence (corrected)",
        "replacement_text": "The Tundra release train ships weekly after the automation upgrade.",
        "rejected_title": "Tundra release speculation",
        "rejected_text": "The Tundra release train might pause during the audit freeze.",
        "query": "when does the Tundra release train ship",
        "old_probe": "Tundra release train two weeks",
        "reject_probe": "Tundra release train audit freeze",
    },
)

_CORRECTION_DISTRACTORS: tuple[tuple[str, str], ...] = (
    ("Meridian retro notes", "Meridian sprint retro notes are archived in the shared drive."),
    ("Nimbus staging refresh", "The Nimbus staging environment refreshes nightly at 2am."),
    ("Oriole feedback triage", "Oriole customer feedback is triaged on Wednesdays."),
    ("Quill docs move", "The Quill component library docs moved to the new wiki."),
    ("Sable metrics recap", "Sable quarterly metrics were presented at the all-hands."),
    ("Tundra dependency audit", "The Tundra dependency audit flagged two licensing questions."),
    ("Willow onboarding step", "The Willow onboarding checklist gained a security step."),
    ("Halcyon demo password", "The Halcyon demo environment password rotates monthly."),
)

_CORRECTION_REQUIRED_STORE_SURFACE = (
    "create_memory",
    "update_memory",
    "get_memory",
    "append_revision",
    "list_revisions",
    "list_events",
    "list_provenance_links",
    "append_event",
    "list_memories",
    "search_memories",
    "search_sources",
    "list_open_loops",
    "upsert_agent_identity",
)


def generate_correction_suppression_corpus() -> JsonObject:
    cases = [dict(case) for case in _CORRECTION_CASES]
    distractors = [
        {
            "memory_key": f"{CORRECTION_MEMORY_KEY_PREFIX}distractor-{index:03d}",
            "title": title,
            "canonical_text": text,
        }
        for index, (title, text) in enumerate(_CORRECTION_DISTRACTORS, start=1)
    ]
    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": CORRECTION_SUPPRESSION_SUITE_KEY,
        "counts": {"cases": len(cases), "distractors": len(distractors)},
        "cases": cases,
        "distractors": distractors,
    }
    corpus["corpus_digest"] = _hash_payload({"cases": cases, "distractors": distractors})
    return corpus


def _error_case(case_key: str, exc: Exception) -> JsonObject:
    logger.error(
        "correction-suppression evaluation case failed case_key=%s error_code=%s",
        case_key,
        EVAL_CASE_ERROR_CODE,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return {
        "case_key": case_key,
        "status": "fail",
        "metrics": {
            "pre_correction_visible": 0.0,
            "suppressed": 0.0,
            "replacement_recall_at_5": 0.0,
            "replacement_reciprocal_rank": 0.0,
            "audit_complete": 0.0,
        },
        "evidence": {
            "error_code": EVAL_CASE_ERROR_CODE,
            "error_message": EVAL_CASE_ERROR_MESSAGE,
        },
    }


def _run_correction_case(
    case: JsonObject,
    *,
    service: VNextMemoryCommitService,
    retrieve: RetrievalFn,
) -> JsonObject:
    case_key = str(case["case_key"])
    query = str(case["query"])
    identity = _eval_agent_identity()

    result_a = _commit_active_memory(
        service,
        title=str(case["original_title"]),
        text=str(case["original_text"]),
        idempotency_key=f"vnext-eval/correction/{case_key}/original",
    )
    memory_a = cast(JsonObject, result_a.get("memory") or {})
    a_key = str(memory_a.get("memory_key") or "")
    a_id = str(memory_a.get("id") or "")
    commit_notes: list[str] = []
    if result_a.get("status") != "committed" or not a_id:
        commit_notes.append(f"original commit did not land active: {result_a.get('status')}")

    pre_ranked = [str(key) for key in cast(list[object], retrieve(query, limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"])]
    pre_visible = recall_at_k(pre_ranked, a_key, 5) if a_key else 0.0

    result_b = _commit_active_memory(
        service,
        title=str(case["replacement_title"]),
        text=str(case["replacement_text"]),
        idempotency_key=f"vnext-eval/correction/{case_key}/replacement",
        rationale=f"Replaces stale memory {a_key}.",
    )
    memory_b = cast(JsonObject, result_b.get("memory") or {})
    b_key = str(memory_b.get("memory_key") or "")
    if result_b.get("status") != "committed" or not b_key:
        commit_notes.append(f"replacement commit did not land active: {result_b.get('status')}")

    supersede_reason = f"superseded_by:{b_key}"
    if a_id:
        service.undo(identity=identity, memory_id=a_id, reason=supersede_reason)

    result_c = _commit_active_memory(
        service,
        title=str(case["rejected_title"]),
        text=str(case["rejected_text"]),
        confidence=0.70,  # medium confidence => inline-confirmation review path
        idempotency_key=f"vnext-eval/correction/{case_key}/rejected",
    )
    memory_c = cast(JsonObject, result_c.get("memory") or {})
    c_key = str(memory_c.get("memory_key") or "")
    c_id = str(memory_c.get("id") or "")
    confirmation_id = result_c.get("confirmation_id")
    rejected_via_review = False
    if result_c.get("status") == "confirmation_required" and isinstance(confirmation_id, str):
        rejection = service.confirm(
            identity=identity,
            confirmation_id=confirmation_id,
            action="reject",
            rationale="Unverified speculation rejected during eval review.",
        )
        rejected_via_review = rejection.get("status") == "rejected"
    if not rejected_via_review:
        commit_notes.append(f"review rejection path unavailable: status={result_c.get('status')}")

    # -- production retrieval probes ------------------------------------
    post_ranked = [str(key) for key in cast(list[object], retrieve(query, limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"])]
    old_probe_ranked = [
        str(key)
        for key in cast(list[object], retrieve(str(case["old_probe"]), limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"])
    ]
    reject_probe_ranked = [
        str(key)
        for key in cast(
            list[object], retrieve(str(case["reject_probe"]), limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"]
        )
    ]
    a_absent = bool(a_key) and a_key not in post_ranked and a_key not in old_probe_ranked
    c_absent = bool(c_key) and c_key not in post_ranked and c_key not in reject_probe_ranked
    suppressed = 1.0 if (a_absent and c_absent) else 0.0
    replacement_recall = recall_at_k(post_ranked, b_key, 5) if b_key else 0.0
    replacement_rr = reciprocal_rank(post_ranked, b_key) if b_key else 0.0

    # -- audit trail ------------------------------------------------------
    audit_ok_a = False
    superseded_reason_seen: str | None = None
    if a_id:
        audit_a = service.audit(memory_id=a_id)
        revisions_a = cast(list[JsonObject], audit_a.get("revisions", []))
        superseded_revisions = [
            revision
            for revision in revisions_a
            if str(revision.get("revision_type")) == "superseded" and str(revision.get("reason") or "").strip()
        ]
        if superseded_revisions:
            superseded_reason_seen = str(superseded_revisions[-1].get("reason"))
        event_types_a = {str(event.get("event_type")) for event in cast(list[JsonObject], audit_a.get("events", []))}
        audit_ok_a = (
            bool(superseded_revisions)
            and any(b_key and b_key in str(revision.get("reason")) for revision in superseded_revisions)
            and "agent.memory_undone" in event_types_a
            and str(cast(JsonObject, audit_a.get("memory") or {}).get("status")) == "superseded"
        )
    audit_ok_c = False
    if c_id:
        audit_c = service.audit(memory_id=c_id)
        revisions_c = cast(list[JsonObject], audit_c.get("revisions", []))
        event_types_c = {str(event.get("event_type")) for event in cast(list[JsonObject], audit_c.get("events", []))}
        audit_ok_c = (
            any(
                str(revision.get("revision_type")) == "rejected" and str(revision.get("reason") or "").strip()
                for revision in revisions_c
            )
            and "agent.memory_confirmation_rejected" in event_types_c
            and str(cast(JsonObject, audit_c.get("memory") or {}).get("status")) == "rejected"
        )
    audit_complete = 1.0 if (audit_ok_a and audit_ok_c) else 0.0

    passed = pre_visible == 1.0 and suppressed == 1.0 and replacement_recall == 1.0 and audit_complete == 1.0
    return {
        "case_key": case_key,
        "status": "pass" if passed else "fail",
        "metrics": {
            "pre_correction_visible": pre_visible,
            "suppressed": suppressed,
            "replacement_recall_at_5": replacement_recall,
            "replacement_reciprocal_rank": replacement_rr,
            "audit_complete": audit_complete,
        },
        "evidence": {
            "query": query,
            "original_memory_key": a_key,
            "replacement_memory_key": b_key,
            "rejected_memory_key": c_key,
            "pre_correction_top_keys": pre_ranked[:5],
            "post_correction_top_keys": post_ranked[:5],
            "old_probe_top_keys": old_probe_ranked[:5],
            "reject_probe_top_keys": reject_probe_ranked[:5],
            "superseded_revision_reason": superseded_reason_seen,
            "commit_flow_notes": commit_notes,
        },
    }


def run_correction_suppression_eval(
    store: object | None,
    *,
    corpus: JsonObject | None = None,
    backend: str | None = None,
) -> JsonObject:
    """Execute the correction-suppression suite against a live store."""
    if store is None:
        return _skipped_suite(
            CORRECTION_SUPPRESSION_SUITE_KEY,
            CORRECTION_SUPPRESSION_TITLE,
            CORRECTION_SUPPRESSION_TARGETS,
            VNEXT_EVAL_LIVE_STORE_SKIP_REASON,
        )
    missing = _missing_store_surface(store, _CORRECTION_REQUIRED_STORE_SURFACE)
    if missing:
        return _skipped_suite(
            CORRECTION_SUPPRESSION_SUITE_KEY,
            CORRECTION_SUPPRESSION_TITLE,
            CORRECTION_SUPPRESSION_TARGETS,
            f"store does not expose required surface: {', '.join(missing)}",
        )
    resolved_corpus = corpus if corpus is not None else generate_correction_suppression_corpus()
    service = _eval_commit_service(store)
    retrieve = production_retrieval_fn(store)

    for distractor in cast(list[JsonObject], resolved_corpus.get("distractors", [])):
        _seed_direct_memory(
            store,
            memory_key=str(distractor["memory_key"]),
            title=str(distractor["title"]),
            text=str(distractor["canonical_text"]),
        )

    cases: list[JsonObject] = []
    for case in cast(list[JsonObject], resolved_corpus.get("cases", [])):
        try:
            cases.append(_run_correction_case(case, service=service, retrieve=retrieve))
        except Exception as exc:  # defensive: sibling churn must not abort the report
            cases.append(_error_case(str(case.get("case_key", "unknown")), exc))

    case_metrics = [cast(JsonObject, case["metrics"]) for case in cases]
    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "case_count": len(cases),
        "distractor_count": len(cast(list[object], resolved_corpus.get("distractors", []))),
        "pre_correction_visibility": _mean([cast(float, metric["pre_correction_visible"]) for metric in case_metrics]),
        "suppression_rate": _mean([cast(float, metric["suppressed"]) for metric in case_metrics]),
        "replacement_recall_at_5": _mean([cast(float, metric["replacement_recall_at_5"]) for metric in case_metrics]),
        "replacement_mrr": _mean([cast(float, metric["replacement_reciprocal_rank"]) for metric in case_metrics]),
        "audit_completeness": _mean([cast(float, metric["audit_complete"]) for metric in case_metrics]),
        "corpus_digest": resolved_corpus.get("corpus_digest"),
    }
    checks = _target_checks(metrics, CORRECTION_SUPPRESSION_TARGETS)
    metrics["target_checks"] = {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())}
    return {
        "suite_key": CORRECTION_SUPPRESSION_SUITE_KEY,
        "title": CORRECTION_SUPPRESSION_TITLE,
        "status": "pass" if cases and all(checks.values()) else "fail",
        "targets": deepcopy(CORRECTION_SUPPRESSION_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


# --------------------------------------------------------------------------
# Decision-recovery suite
# --------------------------------------------------------------------------

DECISION_RECOVERY_TITLE = "Decision recovery (decision memories among mixed-type distractors)"
DECISION_MEMORY_KEY_PREFIX = "vnext-eval/decision/"

_DECISION_CASES: tuple[tuple[str, str, str], ...] = (
    (
        "Meridian ledger storage decision",
        "We decided Meridian will use Postgres over DynamoDB for ledger storage.",
        "what did we decide about Meridian ledger storage",
    ),
    (
        "Nimbus hiring decision",
        "We decided to freeze Nimbus hiring until the second quarter.",
        "what did we decide about Nimbus hiring",
    ),
    (
        "Oriole feature flag decision",
        "We decided Oriole ships behind a feature flag for enterprise tenants.",
        "what did we decide about the Oriole feature flag",
    ),
    (
        "Quill legacy tablet decision",
        "We decided the Quill mobile app drops support for the legacy tablet build.",
        "what did we decide about Quill legacy tablet support",
    ),
    (
        "Sable branching decision",
        "We decided Sable adopts trunk based development with nightly release branches.",
        "what did we decide about Sable trunk based development",
    ),
    (
        "Tundra pricing decision",
        "We decided the Tundra pricing tier caps at nine seats for starter plans.",
        "what did we decide about the Tundra pricing tier caps",
    ),
    (
        "Verdant on-premise decision",
        "We decided Verdant keeps the on-premise offering for regulated customers.",
        "what did we decide about the Verdant on-premise offering",
    ),
    (
        "Willow import pipeline decision",
        "We decided Willow sunsets the legacy import pipeline at the end of March.",
        "what did we decide about the Willow import pipeline",
    ),
    (
        "Halcyon analytics decision",
        "We decided Halcyon moves its analytics workload to the batch cluster.",
        "what did we decide about the Halcyon analytics workload",
    ),
    (
        "Ironwood infrastructure decision",
        "We decided Ironwood standardizes on Terraform for infrastructure changes.",
        "what did we decide about Ironwood Terraform infrastructure",
    ),
)

# Confusable distractors intentionally share topic vocabulary -- three also
# contain the "decided" stem -- so ranking regressions have somewhere to go.
_DECISION_CONFUSABLES: tuple[tuple[str, str, str], ...] = (
    ("episode", "Meridian ledger retro", "The Meridian team revisited what was decided about ledger storage during the spring retro."),
    ("semantic", "Meridian ledger costs", "Meridian ledger storage costs rose eleven percent after the migration."),
    ("episode", "Sable branching debate", "The Sable guild debated what was decided about trunk based development at the summit."),
    ("semantic", "Sable release note", "Sable nightly release branches were flaky during the infrastructure freeze."),
    ("episode", "Halcyon analytics review", "The Halcyon leads reviewed what was decided about the analytics workload in the QBR."),
    ("project_state", "Halcyon batch status", "The Halcyon batch cluster migration is sixty percent complete."),
    ("semantic", "Nimbus hiring note", "Nimbus hiring managers keep a shared interview loop template."),
    ("preference", "Oriole flag preference", "The Oriole team prefers gradual feature flag rollouts over big launches."),
    ("semantic", "Quill tablet metrics", "The Quill legacy tablet build still serves four percent of sessions."),
    ("commitment", "Tundra pricing follow-up", "Finance committed to revisit Tundra pricing tier caps after the pilot."),
    ("project_state", "Verdant deployment state", "The Verdant on-premise installer passed certification last week."),
    ("routine", "Willow import checks", "Willow import pipeline health checks run every morning at 6am."),
)

_DECISION_DISTRACTOR_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("semantic", "{project} weekly metrics are archived to the shared drive."),
    ("routine", "The {project} staging cluster refreshes nightly at 3am."),
    ("procedure", "{project} support tickets are triaged every morning."),
    ("semantic", "The {project} runbook moved to the new wiki space."),
    ("procedure", "{project} invoices route through finance for signoff."),
    ("project_state", "The {project} beta cohort grew by forty accounts."),
)
_DECISION_DISTRACTOR_PROJECTS = ("Meridian", "Sable", "Halcyon")

_DECISION_REQUIRED_STORE_SURFACE = (
    "create_memory",
    "append_event",
    "search_memories",
    "search_sources",
    "list_open_loops",
    "list_provenance_links",
)


def generate_decision_recovery_corpus() -> JsonObject:
    decisions: list[JsonObject] = []
    queries: list[JsonObject] = []
    for index, (title, text, query) in enumerate(_DECISION_CASES, start=1):
        memory_key = f"{DECISION_MEMORY_KEY_PREFIX}decision-{index:03d}"
        decisions.append(
            {
                "memory_key": memory_key,
                "memory_type": "decision",
                "title": title,
                "canonical_text": text,
            }
        )
        queries.append(
            {
                "query_key": f"decision-query-{index:03d}",
                "query": query,
                "expected_memory_key": memory_key,
            }
        )

    distractors: list[JsonObject] = []
    for index, (memory_type, title, text) in enumerate(_DECISION_CONFUSABLES, start=1):
        distractors.append(
            {
                "memory_key": f"{DECISION_MEMORY_KEY_PREFIX}confusable-{index:03d}",
                "memory_type": memory_type,
                "title": title,
                "canonical_text": text,
            }
        )
    counter = 0
    for template_index, (memory_type, template) in enumerate(_DECISION_DISTRACTOR_TEMPLATES):
        for project in _DECISION_DISTRACTOR_PROJECTS:
            counter += 1
            distractors.append(
                {
                    "memory_key": f"{DECISION_MEMORY_KEY_PREFIX}distractor-{counter:03d}",
                    "memory_type": memory_type,
                    "title": f"{project} operational note {counter:03d}",
                    "canonical_text": template.format(project=project),
                }
            )

    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": DECISION_RECOVERY_SUITE_KEY,
        "counts": {
            "decisions": len(decisions),
            "distractors": len(distractors),
            "queries": len(queries),
        },
        "decisions": decisions,
        "distractors": distractors,
        "queries": queries,
    }
    corpus["corpus_digest"] = _hash_payload({"decisions": decisions, "distractors": distractors, "queries": queries})
    return corpus


def run_decision_recovery_eval(
    store: object | None,
    *,
    corpus: JsonObject | None = None,
    backend: str | None = None,
) -> JsonObject:
    """Execute the decision-recovery suite against a live store."""
    if store is None:
        return _skipped_suite(
            DECISION_RECOVERY_SUITE_KEY,
            DECISION_RECOVERY_TITLE,
            DECISION_RECOVERY_TARGETS,
            VNEXT_EVAL_LIVE_STORE_SKIP_REASON,
        )
    missing = _missing_store_surface(store, _DECISION_REQUIRED_STORE_SURFACE)
    if missing:
        return _skipped_suite(
            DECISION_RECOVERY_SUITE_KEY,
            DECISION_RECOVERY_TITLE,
            DECISION_RECOVERY_TARGETS,
            f"store does not expose required surface: {', '.join(missing)}",
        )
    resolved_corpus = corpus if corpus is not None else generate_decision_recovery_corpus()

    for row in (
        *cast(list[JsonObject], resolved_corpus.get("decisions", [])),
        *cast(list[JsonObject], resolved_corpus.get("distractors", [])),
    ):
        _seed_direct_memory(
            store,
            memory_key=str(row["memory_key"]),
            title=str(row["title"]),
            text=str(row["canonical_text"]),
            memory_type=str(row.get("memory_type", "semantic")),
        )

    retrieve = production_retrieval_fn(store)
    filtered_retrieve: RetrievalFn | None = None
    filter_note = (
        "memory_types filter parameter not present on VNextRetrievalRequest; "
        "TODO: add the filtered variant when the sibling retrieval workstream lands it"
    )
    if retrieval_request_supports_memory_types():
        try:
            filtered_retrieve = filtered_retrieval_fn(store, ("decision",))
            filter_note = "filtered via VNextRetrievalRequest.memory_types=('decision',)"
        except TypeError as exc:  # sibling landed an incompatible signature
            logger.warning(
                "Decision-recovery memory-types filter was incompatible "
                "error_code=invalid_filter_signature",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            filtered_retrieve = None
            filter_note = MEMORY_TYPES_FILTER_INCOMPATIBLE_NOTE

    cases: list[JsonObject] = []
    for query in cast(list[JsonObject], resolved_corpus.get("queries", [])):
        query_text = str(query["query"])
        expected_key = str(query["expected_memory_key"])
        ranked = [
            str(key)
            for key in cast(list[object], retrieve(query_text, limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"])
        ]
        case_metrics: JsonObject = {
            "recall_at_1": recall_at_k(ranked, expected_key, 1),
            "recall_at_5": recall_at_k(ranked, expected_key, 5),
            "reciprocal_rank": reciprocal_rank(ranked, expected_key),
        }
        evidence: JsonObject = {
            "query": query_text,
            "expected_memory_key": expected_key,
            "top_memory_keys": ranked[:5],
        }
        if filtered_retrieve is not None:
            filtered_ranked = [
                str(key)
                for key in cast(
                    list[object], filtered_retrieve(query_text, limit=RETRIEVAL_QUALITY_RECALL_LIMIT)["ranked_memory_keys"]
                )
            ]
            case_metrics["filtered_recall_at_5"] = recall_at_k(filtered_ranked, expected_key, 5)
            case_metrics["filtered_reciprocal_rank"] = reciprocal_rank(filtered_ranked, expected_key)
            evidence["filtered_top_memory_keys"] = filtered_ranked[:5]
        cases.append(
            {
                "case_key": str(query["query_key"]),
                "status": "pass" if case_metrics["recall_at_5"] == 1.0 else "fail",
                "metrics": case_metrics,
                "evidence": evidence,
            }
        )

    case_metrics_list = [cast(JsonObject, case["metrics"]) for case in cases]
    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "query_count": len(cases),
        "decision_count": len(cast(list[object], resolved_corpus.get("decisions", []))),
        "distractor_count": len(cast(list[object], resolved_corpus.get("distractors", []))),
        "decision_recall_at_1": _mean([cast(float, metric["recall_at_1"]) for metric in case_metrics_list]),
        "decision_recall_at_5": _mean([cast(float, metric["recall_at_5"]) for metric in case_metrics_list]),
        "decision_mrr": _mean([cast(float, metric["reciprocal_rank"]) for metric in case_metrics_list]),
        "memory_types_filter": {
            "available": filtered_retrieve is not None,
            "note": filter_note,
        },
        "corpus_digest": resolved_corpus.get("corpus_digest"),
    }
    skip_target_keys: set[str] = set()
    if filtered_retrieve is not None:
        metrics["filtered_decision_recall_at_5"] = _mean(
            [cast(float, metric.get("filtered_recall_at_5", 0.0)) for metric in case_metrics_list]
        )
        metrics["filtered_decision_mrr"] = _mean(
            [cast(float, metric.get("filtered_reciprocal_rank", 0.0)) for metric in case_metrics_list]
        )
    else:
        # Filter unavailable: the filtered target is reported but not enforced.
        skip_target_keys.add("filtered_decision_recall_at_5")
    checks = _target_checks(metrics, DECISION_RECOVERY_TARGETS, skip_keys=skip_target_keys)
    metrics["target_checks"] = {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())}
    return {
        "suite_key": DECISION_RECOVERY_SUITE_KEY,
        "title": DECISION_RECOVERY_TITLE,
        "status": "pass" if cases and all(checks.values()) else "fail",
        "targets": deepcopy(DECISION_RECOVERY_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


# --------------------------------------------------------------------------
# Provenance-explanation suite
# --------------------------------------------------------------------------

PROVENANCE_EXPLANATION_TITLE = "Provenance explanation (commit service audit path)"
PROVENANCE_MEMORY_KEY_PREFIX = "vnext-eval/provenance/"

_PROVENANCE_SOURCES: tuple[dict[str, str], ...] = (
    {
        "source_key": "prov-source-001",
        "source_type": "document",
        "title": "Verdant planning notes",
        "content_hash": "sha256:vnext-eval-provenance-0001",
    },
    {
        "source_key": "prov-source-002",
        "source_type": "email",
        "title": "Willow vendor thread",
        "content_hash": "sha256:vnext-eval-provenance-0002",
    },
)

_PROVENANCE_MEMORIES: tuple[dict[str, object], ...] = (
    {
        "case_key": "provenance-001",
        "title": "Verdant pilot pricing",
        "text": "Verdant pilot pricing is locked at 40 dollars per seat.",
        "memory_type": "decision",
        "source_key": "prov-source-001",
        "excerpt": "pricing locked at 40 dollars per seat for the pilot",
        "corrected_text": "Verdant pilot pricing is locked at 35 dollars per seat after the discount review.",
        "correction_reason": "Pilot discount approved by finance.",
    },
    {
        "case_key": "provenance-002",
        "title": "Willow CDN renewal",
        "text": "Willow renews the CDN contract with Fastly in November.",
        "memory_type": "semantic",
        "source_key": "prov-source-002",
        "excerpt": "the CDN renewal thread settled on November",
        "corrected_text": "Willow renews the CDN contract with Cloudflare in November.",
        "correction_reason": "Vendor switched after the outage postmortem.",
    },
    {
        "case_key": "provenance-003",
        "title": "Halcyon migration runbook",
        "text": "The Halcyon migration runbook requires a dry run before each cutover.",
        "memory_type": "procedure",
        "source_key": "prov-source-001",
        "excerpt": "dry run required before each cutover",
    },
    {
        "case_key": "provenance-004",
        "title": "Ironwood incident reviews",
        "text": "Ironwood incident reviews happen within three business days.",
        "memory_type": "routine",
        "source_key": "prov-source-002",
        "excerpt": "reviews within three business days",
    },
    {
        "case_key": "provenance-005",
        "title": "Quill accessibility audit",
        "text": "The Quill accessibility audit closes at the end of the quarter.",
        "memory_type": "commitment",
        "source_key": "prov-source-001",
        "excerpt": "audit closes at the end of the quarter",
    },
    {
        "case_key": "provenance-006",
        "title": "Sable dashboard refresh",
        "text": "Sable analytics dashboards refresh hourly during business hours.",
        "memory_type": "semantic",
        "source_key": "prov-source-002",
        "excerpt": "dashboards refresh hourly",
    },
)

_PROVENANCE_REQUIRED_STORE_SURFACE = (
    "create_memory",
    "update_memory",
    "get_memory",
    "create_source",
    "get_source",
    "create_provenance_link",
    "list_provenance_links",
    "append_revision",
    "list_revisions",
    "list_events",
    "append_event",
    "list_memories",
    "upsert_agent_identity",
)


def generate_provenance_explanation_corpus() -> JsonObject:
    sources = [dict(source) for source in _PROVENANCE_SOURCES]
    memories = [dict(memory) for memory in _PROVENANCE_MEMORIES]
    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": PROVENANCE_EXPLANATION_SUITE_KEY,
        "counts": {
            "sources": len(sources),
            "memories": len(memories),
            "corrected_memories": sum(1 for memory in memories if memory.get("corrected_text")),
        },
        "sources": sources,
        "memories": memories,
    }
    corpus["corpus_digest"] = _hash_payload({"sources": sources, "memories": memories})
    return corpus


def _audit_provenance_case(
    case: JsonObject,
    *,
    store: object,
    service: VNextMemoryCommitService,
    source_ids: Mapping[str, str],
) -> tuple[JsonObject, int, int]:
    """Commit + optionally correct one memory, then audit it.

    Returns ``(case_record, resolved_link_count, orphan_link_count)``.
    """
    case_key = str(case["case_key"])
    source_id = source_ids.get(str(case.get("source_key")), "")
    corrected_text = case.get("corrected_text")

    result = _commit_active_memory(
        service,
        title=str(case["title"]),
        text=str(case["text"]),
        memory_type=str(case.get("memory_type", "semantic")),
        idempotency_key=f"vnext-eval/provenance/{case_key}",
        source_refs=(f"source:{source_id}",) if source_id else (),
    )
    memory = cast(JsonObject, result.get("memory") or {})
    memory_id = str(memory.get("id") or "")
    committed = result.get("status") == "committed" and bool(memory_id)

    correction_applied = False
    if committed and isinstance(corrected_text, str) and corrected_text:
        service.correct(
            identity=_eval_agent_identity(),
            memory_id=memory_id,
            canonical_text=corrected_text,
            reason=str(case.get("correction_reason") or "Eval correction."),
        )
        correction_applied = True

    checks: dict[str, bool] = {"committed": committed}
    resolved_links = 0
    orphan_links = 0
    evidence: JsonObject = {"memory_id": memory_id, "commit_status": result.get("status")}
    if committed:
        audit = service.audit(memory_id=memory_id)
        revisions = cast(list[JsonObject], audit.get("revisions", []))
        events = cast(list[JsonObject], audit.get("events", []))
        links = cast(list[JsonObject], audit.get("provenance_links", []))
        event_types = {str(event.get("event_type")) for event in events}
        get_source = cast(Callable[[str], object], getattr(store, "get_source"))
        for link in links:
            link_source_id = str(link.get("source_id") or "")
            if link_source_id and get_source(link_source_id) is not None:
                resolved_links += 1
            else:
                orphan_links += 1

        checks["has_reasoned_revision"] = any(str(revision.get("reason") or "").strip() for revision in revisions)
        checks["has_commit_event"] = "agent.memory_committed" in event_types
        checks["provenance_resolves"] = len(links) >= 1 and orphan_links == 0
        if correction_applied:
            corrected_revisions = [
                revision
                for revision in revisions
                if str(revision.get("revision_type")) == "corrected"
                and str(revision.get("text_after") or "") == str(corrected_text)
            ]
            metadata = cast(JsonObject, cast(JsonObject, audit.get("memory") or {}).get("metadata_json") or {})
            agentic = cast(JsonObject, metadata.get("agentic_memory") or {})
            corrections_meta = agentic.get("corrections")
            checks["correction_reflected"] = (
                bool(corrected_revisions)
                and "agent.memory_corrected" in event_types
                and isinstance(corrections_meta, list)
                and len(corrections_meta) >= 1
            )
        evidence.update(
            {
                "revision_types": [str(revision.get("revision_type")) for revision in revisions],
                "revision_reasons": [str(revision.get("reason") or "") for revision in revisions],
                "event_types": sorted(event_types),
                "provenance_link_count": len(links),
                "resolved_link_count": resolved_links,
                "orphan_link_count": orphan_links,
            }
        )
    explain_complete = all(checks.values())
    return (
        {
            "case_key": case_key,
            "status": "pass" if explain_complete else "fail",
            "metrics": {"explain_complete": 1.0 if explain_complete else 0.0},
            "checks": {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())},
            "evidence": evidence,
        },
        resolved_links,
        orphan_links,
    )


def run_provenance_explanation_eval(
    store: object | None,
    *,
    corpus: JsonObject | None = None,
    backend: str | None = None,
) -> JsonObject:
    """Execute the provenance-explanation suite against a live store."""
    if store is None:
        return _skipped_suite(
            PROVENANCE_EXPLANATION_SUITE_KEY,
            PROVENANCE_EXPLANATION_TITLE,
            PROVENANCE_EXPLANATION_TARGETS,
            VNEXT_EVAL_LIVE_STORE_SKIP_REASON,
        )
    missing = _missing_store_surface(store, _PROVENANCE_REQUIRED_STORE_SURFACE)
    if missing:
        return _skipped_suite(
            PROVENANCE_EXPLANATION_SUITE_KEY,
            PROVENANCE_EXPLANATION_TITLE,
            PROVENANCE_EXPLANATION_TARGETS,
            f"store does not expose required surface: {', '.join(missing)}",
        )
    resolved_corpus = corpus if corpus is not None else generate_provenance_explanation_corpus()
    service = _eval_commit_service(store)

    create_source = cast(Callable[..., JsonObject], getattr(store, "create_source"))
    source_ids: dict[str, str] = {}
    for source in cast(list[JsonObject], resolved_corpus.get("sources", [])):
        row = create_source(
            {
                "source_type": source["source_type"],
                "title": source["title"],
                "content_hash": source["content_hash"],
                "domain": "professional",
                "sensitivity": "internal",
            }
        )
        source_ids[str(source["source_key"])] = str(row["id"])

    cases: list[JsonObject] = []
    total_resolved = 0
    total_orphans = 0
    for case in cast(list[JsonObject], resolved_corpus.get("memories", [])):
        try:
            case_record, resolved_links, orphan_links = _audit_provenance_case(
                case, store=store, service=service, source_ids=source_ids
            )
        except Exception as exc:  # defensive: sibling churn must not abort the report
            case_key = str(case.get("case_key", "unknown"))
            logger.error(
                "provenance evaluation case failed case_key=%s error_code=%s",
                case_key,
                EVAL_CASE_ERROR_CODE,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            case_record = {
                "case_key": case_key,
                "status": "fail",
                "metrics": {"explain_complete": 0.0},
                "checks": {},
                "evidence": {
                    "error_code": EVAL_CASE_ERROR_CODE,
                    "error_message": EVAL_CASE_ERROR_MESSAGE,
                },
            }
            resolved_links = 0
            orphan_links = 0
        cases.append(case_record)
        total_resolved += resolved_links
        total_orphans += orphan_links

    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "audited_memory_count": len(cases),
        "corrected_memory_count": sum(
            1 for case in cast(list[JsonObject], resolved_corpus.get("memories", [])) if case.get("corrected_text")
        ),
        "explain_completeness_rate": _mean(
            [cast(float, cast(JsonObject, case["metrics"])["explain_complete"]) for case in cases]
        ),
        "provenance_link_count": total_resolved + total_orphans,
        "orphan_provenance_count": total_orphans,
        "corpus_digest": resolved_corpus.get("corpus_digest"),
    }
    checks = _target_checks(metrics, PROVENANCE_EXPLANATION_TARGETS)
    metrics["target_checks"] = {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())}
    return {
        "suite_key": PROVENANCE_EXPLANATION_SUITE_KEY,
        "title": PROVENANCE_EXPLANATION_TITLE,
        "status": "pass" if cases and all(checks.values()) else "fail",
        "targets": deepcopy(PROVENANCE_EXPLANATION_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


# --------------------------------------------------------------------------
# Entity-resolution suite (Sprint D)
# --------------------------------------------------------------------------

ENTITY_RESOLUTION_TITLE = "Entity resolution (capture pipeline extraction + canonicalization)"

_ENTITY_RESOLUTION_GROUPS: tuple[JsonObject, ...] = (
    {
        "group_key": "person-sami",
        "canonical_name": "Jane Doe",
        "entity_type": "person",
        "expected_alias": "dr jane doe",
        "source_texts": (
            "Met with Jane Doe about the fund strategy and follow-ups.",
            "Dr Jane Doe confirmed the allocation timeline yesterday.",
        ),
    },
    {
        "group_key": "org-meridian",
        "canonical_name": "Meridian Capital",
        "entity_type": "organization",
        "expected_alias": None,
        "source_texts": (
            "Meridian Capital opened the data room for diligence.",
            "The diligence call with Meridian Capital ran long again.",
        ),
    },
    {
        "group_key": "org-alice-core",
        "canonical_name": "Alice Core",
        "entity_type": None,
        "expected_alias": None,
        "source_texts": (
            "Alice Core stores every revision with provenance links.",
            "We profiled Alice Core under heavier workloads today.",
        ),
    },
)

# Blocklist-noise probes: capitalized tokens the extractor must NOT turn into
# entities (weekday, month). Each noise token repeats with a mid-sentence
# occurrence so it clears the repeat-threshold rule and is stopped ONLY by
# the blocklist — which is exactly what makes the suite able to fail when
# the blocklist regresses. Kept free of other capitalized spans.
_ENTITY_RESOLUTION_NOISE_TEXTS: tuple[str, ...] = (
    "Monday standup ran long, so we moved Monday planning to the afternoon.",
    "The review closed in January because January carried the audit window.",
)

ENTITY_RESOLUTION_TARGETS: JsonObject = {
    "resolution_rate": {"minimum": 0.90},
    "noise_entity_count": {"maximum": 0},
    "mention_accuracy": {"minimum": 1.0},
    "alias_growth_rate": {"minimum": 1.0},
}

_ENTITY_RESOLUTION_REQUIRED_STORE_SURFACE = (
    "create_source",
    "create_memory",
    "find_entities_by_names",
    "list_entities",
)


def generate_entity_resolution_corpus() -> JsonObject:
    groups = [deepcopy(group) for group in _ENTITY_RESOLUTION_GROUPS]
    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": ENTITY_RESOLUTION_SUITE_KEY,
        "groups": groups,
        "noise_texts": list(_ENTITY_RESOLUTION_NOISE_TEXTS),
        "counts": {
            "groups": len(groups),
            "source_texts": sum(len(cast(tuple, g["source_texts"])) for g in groups)
            + len(_ENTITY_RESOLUTION_NOISE_TEXTS),
        },
    }
    corpus["corpus_digest"] = _hash_payload(
        {"groups": groups, "noise_texts": list(_ENTITY_RESOLUTION_NOISE_TEXTS)}
    )
    return corpus


def run_entity_resolution_eval(
    store: object | None,
    *,
    corpus: JsonObject | None = None,
    backend: str | None = None,
) -> JsonObject:
    """Execute entity extraction/canonicalization against the REAL capture path.

    Every source text goes through ``VNextCaptureService.capture_text``; the
    suite then asserts that surface variants of the same entity resolved to a
    single canonical row, that blocklist noise created zero entities, that
    mention counts equal the number of capturing sources, and that honorific
    variants grew the alias list.
    """
    from alicebot_api.vnext_capture import VNextCaptureService
    from alicebot_api.vnext_entity_names import normalize_entity_name

    if store is None:
        return _skipped_suite(
            ENTITY_RESOLUTION_SUITE_KEY,
            ENTITY_RESOLUTION_TITLE,
            ENTITY_RESOLUTION_TARGETS,
            VNEXT_EVAL_LIVE_STORE_SKIP_REASON,
        )
    missing = _missing_store_surface(store, _ENTITY_RESOLUTION_REQUIRED_STORE_SURFACE)
    if missing:
        return _skipped_suite(
            ENTITY_RESOLUTION_SUITE_KEY,
            ENTITY_RESOLUTION_TITLE,
            ENTITY_RESOLUTION_TARGETS,
            f"store does not expose required surface: {', '.join(missing)}",
        )
    resolved_corpus = corpus if corpus is not None else generate_entity_resolution_corpus()

    capture = VNextCaptureService(cast("PostgresVNextStore", store))
    for group in cast(list[JsonObject], resolved_corpus["groups"]):
        for text in cast(Sequence[str], group["source_texts"]):
            capture.capture_text(str(text), domain="professional", sensitivity="internal")
    for text in cast(Sequence[str], resolved_corpus["noise_texts"]):
        capture.capture_text(str(text), domain="professional", sensitivity="internal")

    entities = cast(list[JsonObject], store.list_entities(limit=200))  # type: ignore[attr-defined]
    by_normalized: dict[str, JsonObject] = {}
    for entity in entities:
        by_normalized[str(entity["normalized_name"])] = entity

    expected_normalized: set[str] = set()
    cases: list[JsonObject] = []
    resolved_groups = 0
    mention_correct = 0
    alias_expected = 0
    alias_grown = 0
    for group in cast(list[JsonObject], resolved_corpus["groups"]):
        canonical = normalize_entity_name(str(group["canonical_name"]))
        expected_normalized.add(canonical)
        source_texts = cast(Sequence[str], group["source_texts"])
        matches = cast(
            list[JsonObject],
            store.find_entities_by_names((canonical,)),  # type: ignore[attr-defined]
        )
        matched_entity: JsonObject | None = matches[0] if matches else None
        distinct_rows = {str(m["id"]) for m in matches}
        group_resolved = matched_entity is not None and len(distinct_rows) == 1
        if group_resolved:
            resolved_groups += 1
        mentions_ok = matched_entity is not None and int(
            cast(int, matched_entity.get("mention_count", 0))
        ) >= len(source_texts)
        if mentions_ok:
            mention_correct += 1
        alias_ok = True
        expected_alias = group.get("expected_alias")
        if expected_alias is not None:
            alias_expected += 1
            aliases = [
                str(alias)
                for alias in cast(list[object], (matched_entity or {}).get("aliases", []))
            ]
            alias_ok = str(expected_alias) in aliases
            if alias_ok:
                alias_grown += 1
            expected_normalized.add(str(expected_alias))
        cases.append(
            {
                "case_key": str(group["group_key"]),
                "status": "pass" if (group_resolved and mentions_ok and alias_ok) else "fail",
                "metrics": {
                    "resolved": 1.0 if group_resolved else 0.0,
                    "mention_count": int(
                        cast(int, (matched_entity or {}).get("mention_count", 0))
                    ),
                },
                "evidence": {
                    "canonical_normalized": canonical,
                    "entity_id": str(matched_entity["id"]) if matched_entity else None,
                    "aliases": (matched_entity or {}).get("aliases", []),
                },
            }
        )

    noise_entities = [
        {"normalized_name": name, "entity_type": row.get("entity_type")}
        for name, row in sorted(by_normalized.items())
        if name not in expected_normalized
    ]

    group_count = len(cast(list[object], resolved_corpus["groups"]))
    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "group_count": group_count,
        "entity_count": len(entities),
        "resolution_rate": (resolved_groups / group_count) if group_count else 0.0,
        "mention_accuracy": (mention_correct / group_count) if group_count else 0.0,
        "alias_growth_rate": (alias_grown / alias_expected) if alias_expected else 1.0,
        "noise_entity_count": len(noise_entities),
        "noise_entities": noise_entities,
        "corpus_digest": resolved_corpus.get("corpus_digest"),
    }
    checks = _target_checks(metrics, ENTITY_RESOLUTION_TARGETS)
    metrics["target_checks"] = {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())}
    return {
        "suite_key": ENTITY_RESOLUTION_SUITE_KEY,
        "title": ENTITY_RESOLUTION_TITLE,
        "status": "pass" if cases and all(checks.values()) else "fail",
        "targets": deepcopy(ENTITY_RESOLUTION_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


# --------------------------------------------------------------------------
# Graph-hop retrieval suite (Sprint D — the multi-session mechanism measure)
# --------------------------------------------------------------------------

GRAPH_HOP_RETRIEVAL_TITLE = "Graph-hop retrieval (entity-connected memories beyond lexical reach)"
GRAPH_HOP_MEMORY_KEY_PREFIX = "vnext-eval/graph-hop/"

# Each truth group: the entity is established through REAL capture (sources
# mentioning it), the target memory is lexically DISJOINT from the query, and
# the memory->entity 'mentions' edge is seeded as corpus ground truth (in
# production the edge arises when an entity-bearing memory is accepted; the
# association itself is what retrieval is being measured against).
_GRAPH_HOP_GROUPS: tuple[JsonObject, ...] = (
    {
        "group_key": "hop-meridian",
        "entity_name": "Meridian Capital",
        "entity_sources": (
            "Meridian Capital opened the data room for the round.",
            "Call notes: Meridian Capital wants weekly updates.",
        ),
        "memory_title": "Diligence blocker",
        "memory_text": "Legal review is blocking the third-quarter close.",
        "query": "what is happening with Meridian Capital",
    },
    {
        "group_key": "hop-northwind",
        "entity_name": "Northwind Labs",
        "entity_sources": (
            "Northwind Labs shipped their sensor firmware beta.",
            "Northwind Labs asked for the integration checklist.",
        ),
        "memory_title": "Partnership decision",
        "memory_text": "The pilot agreement was signed after the demo succeeded.",
        "query": "latest on Northwind Labs",
    },
    {
        "group_key": "hop-aurora",
        "entity_name": "Project Aurora",
        "entity_sources": (
            "Project Aurora kickoff covered scope and staffing.",
            "Project Aurora retro flagged the vendor dependency.",
        ),
        "memory_title": "Budget change",
        "memory_text": "Spending approval moved to a monthly cadence.",
        "query": "Project Aurora status",
    },
    {
        "group_key": "hop-halcyon",
        "entity_name": "Halcyon Group",
        "entity_sources": (
            "Halcyon Group introduced their platform team.",
            "Halcyon Group requested the security addendum.",
        ),
        "memory_title": "Contract state",
        "memory_text": "Redlines were returned and the signature packet is ready.",
        "query": "where do we stand with Halcyon Group",
    },
    {
        "group_key": "hop-verdant",
        "entity_name": "Verdant Systems",
        "entity_sources": (
            "Verdant Systems published the migration schedule.",
            "Verdant Systems confirmed the sandbox credentials.",
        ),
        "memory_title": "Rollout risk",
        "memory_text": "The cutover depends on freezing schema changes first.",
        "query": "any news about Verdant Systems",
    },
)

GRAPH_HOP_RETRIEVAL_TARGETS: JsonObject = {
    "graph_recall_at_5": {"minimum": 0.80},
    "graph_lift": {"minimum": 0.31},
}

_GRAPH_HOP_REQUIRED_STORE_SURFACE = (
    "create_source",
    "create_memory",
    "find_entities_by_names",
    "list_entities",
    "list_edges",
)


class _GraphlessStore:
    """Duck-type wrapper hiding the entity/edge surface.

    The production graph stage feature-detects ``find_entities_by_names`` and
    ``list_edges``; hiding them yields the honest FTS-only control run for
    the same seeded corpus.
    """

    _HIDDEN = {"find_entities_by_names", "list_edges", "list_edges_as_of"}

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        if name in self._HIDDEN:
            raise AttributeError(name)
        return getattr(self._inner, name)


def generate_graph_hop_corpus() -> JsonObject:
    groups = [deepcopy(group) for group in _GRAPH_HOP_GROUPS]
    corpus: JsonObject = {
        "schema_version": VNEXT_EVAL_CORPUS_SCHEMA_VERSION,
        "kind": GRAPH_HOP_RETRIEVAL_SUITE_KEY,
        "groups": groups,
        "counts": {"groups": len(groups)},
    }
    corpus["corpus_digest"] = _hash_payload({"groups": groups})
    return corpus


def _graph_hop_retrieval_fn(store: object) -> Callable[[str], JsonObject]:
    service = VNextRetrievalService(cast(VNextRetrievalStore, store))

    def _retrieve(query: str) -> JsonObject:
        pack = service.compile_context_pack(
            VNextRetrievalRequest(
                query=query,
                max_items=RETRIEVAL_QUALITY_RECALL_LIMIT,
                include_sources=False,
                include_contradictions=False,
                actor_type="system",
            )
        )
        relevant = cast(list[JsonObject], pack.get("relevant_memories", []))
        trace = cast(JsonObject, pack.get("trace", {}))
        selected = cast(list[JsonObject], trace.get("selected", []))
        stage_ranks_by_id: dict[str, JsonObject] = {}
        for item in selected:
            target_id = str(item.get("target_id") or "")
            if target_id:
                stage_ranks_by_id[target_id] = cast(JsonObject, item.get("stage_ranks", {}))
        graph_stage = cast(JsonObject, trace.get("stages", {})).get("graph", {})
        return {
            "ranked_memory_keys": [str(m["memory_key"]) for m in relevant if m.get("memory_key")],
            "stage_ranks_by_id": stage_ranks_by_id,
            "graph_stage": graph_stage,
        }

    return _retrieve


def run_graph_hop_retrieval_eval(
    store: object | None,
    *,
    corpus: JsonObject | None = None,
    backend: str | None = None,
) -> JsonObject:
    """Measure entity-hop retrieval against its honest FTS-only control.

    Entities are established through the real capture pipeline; target
    memories share no content words with their queries; the memory->entity
    association is corpus ground truth. The same production retrieval runs
    twice — once normally, once against a wrapper hiding the entity/edge
    surface — and the lift between the two is the multi-session mechanism's
    measured contribution.
    """
    from alicebot_api.vnext_capture import VNextCaptureService
    from alicebot_api.vnext_entities import EntityLinkingService
    from alicebot_api.vnext_entity_names import normalize_entity_name

    if store is None:
        return _skipped_suite(
            GRAPH_HOP_RETRIEVAL_SUITE_KEY,
            GRAPH_HOP_RETRIEVAL_TITLE,
            GRAPH_HOP_RETRIEVAL_TARGETS,
            VNEXT_EVAL_LIVE_STORE_SKIP_REASON,
        )
    missing = _missing_store_surface(store, _GRAPH_HOP_REQUIRED_STORE_SURFACE)
    if missing:
        return _skipped_suite(
            GRAPH_HOP_RETRIEVAL_SUITE_KEY,
            GRAPH_HOP_RETRIEVAL_TITLE,
            GRAPH_HOP_RETRIEVAL_TARGETS,
            f"store does not expose required surface: {', '.join(missing)}",
        )
    resolved_corpus = corpus if corpus is not None else generate_graph_hop_corpus()

    capture = VNextCaptureService(cast("PostgresVNextStore", store))
    linker = EntityLinkingService(store, actor_type="system", actor_id=None, trace_id=None)
    for index, group in enumerate(cast(list[JsonObject], resolved_corpus["groups"]), start=1):
        for text in cast(Sequence[str], group["entity_sources"]):
            capture.capture_text(str(text), domain="professional", sensitivity="internal")
        memory_key = f"{GRAPH_HOP_MEMORY_KEY_PREFIX}{index:03d}"
        group["memory_key"] = memory_key
        memory = _seed_direct_memory(
            store,
            memory_key=memory_key,
            title=str(group["memory_title"]),
            text=str(group["memory_text"]),
            memory_type="semantic",
        )
        group["memory_id"] = str(memory["id"])
        # Ground-truth association: link the accepted memory to the captured
        # entity by name (same call the acceptance paths make), which creates
        # the memory->entity mentions edge without requiring the entity name
        # inside the memory text.
        linker.link_entities_for_memory(
            memory_id=str(memory["id"]),
            text=str(group["entity_name"]),
            observed_at=memory.get("created_at"),
        )
        group["normalized_entity_name"] = normalize_entity_name(str(group["entity_name"]))

    retrieve_graph = _graph_hop_retrieval_fn(store)
    retrieve_fts = _graph_hop_retrieval_fn(_GraphlessStore(store))

    cases: list[JsonObject] = []
    for group in cast(list[JsonObject], resolved_corpus["groups"]):
        query = str(group["query"])
        expected_key = str(group["memory_key"])
        graph_result = retrieve_graph(query)
        fts_result = retrieve_fts(query)
        graph_ranked = cast(list[str], graph_result["ranked_memory_keys"])
        fts_ranked = cast(list[str], fts_result["ranked_memory_keys"])
        winner_stage_ranks_value = cast(JsonObject, graph_result["stage_ranks_by_id"]).get(
            str(group.get("memory_id", "")), {}
        )
        winner_stage_ranks = (
            winner_stage_ranks_value if isinstance(winner_stage_ranks_value, dict) else {}
        )
        case_metrics: JsonObject = {
            "graph_recall_at_5": recall_at_k(graph_ranked, expected_key, 5),
            "fts_recall_at_5": recall_at_k(fts_ranked, expected_key, 5),
            "winner_has_graph_rank": 1.0 if "graph" in winner_stage_ranks else 0.0,
        }
        cases.append(
            {
                "case_key": str(group["group_key"]),
                "status": "pass"
                if case_metrics["graph_recall_at_5"] == 1.0 and case_metrics["winner_has_graph_rank"] == 1.0
                else "fail",
                "metrics": case_metrics,
                "evidence": {
                    "query": query,
                    "expected_memory_key": expected_key,
                    "graph_top_keys": graph_ranked[:5],
                    "fts_top_keys": fts_ranked[:5],
                    "winner_stage_ranks": winner_stage_ranks,
                    "control_graph_stage": fts_result["graph_stage"],
                },
            }
        )

    case_metrics_list = [cast(JsonObject, case["metrics"]) for case in cases]
    graph_recall = _mean([cast(float, m["graph_recall_at_5"]) for m in case_metrics_list])
    fts_recall = _mean([cast(float, m["fts_recall_at_5"]) for m in case_metrics_list])
    metrics: JsonObject = {
        "backend": _eval_backend_label(store, backend),
        "group_count": len(cases),
        "graph_recall_at_5": graph_recall,
        "fts_only_recall_at_5": fts_recall,
        "graph_lift": graph_recall - fts_recall,
        "winner_graph_rank_rate": _mean(
            [cast(float, m["winner_has_graph_rank"]) for m in case_metrics_list]
        ),
        "control_mechanism": "duck-type wrapper hiding find_entities_by_names/list_edges",
        "corpus_digest": resolved_corpus.get("corpus_digest"),
    }
    checks = _target_checks(metrics, GRAPH_HOP_RETRIEVAL_TARGETS)
    metrics["target_checks"] = {key: ("pass" if passed else "fail") for key, passed in sorted(checks.items())}
    return {
        "suite_key": GRAPH_HOP_RETRIEVAL_SUITE_KEY,
        "title": GRAPH_HOP_RETRIEVAL_TITLE,
        "status": "pass" if cases and all(checks.values()) else "fail",
        "targets": deepcopy(GRAPH_HOP_RETRIEVAL_TARGETS),
        "metrics": metrics,
        "cases": cases,
    }


_MEMORY_QUALITY_SUITE_RUNNERS: dict[str, Callable[..., JsonObject]] = {
    CORRECTION_SUPPRESSION_SUITE_KEY: run_correction_suppression_eval,
    DECISION_RECOVERY_SUITE_KEY: run_decision_recovery_eval,
    PROVENANCE_EXPLANATION_SUITE_KEY: run_provenance_explanation_eval,
    ENTITY_RESOLUTION_SUITE_KEY: run_entity_resolution_eval,
    GRAPH_HOP_RETRIEVAL_SUITE_KEY: run_graph_hop_retrieval_eval,
}

# The Sprint D suite targets are defined adjacent to their suites (above),
# after the acceptance-targets map near the top of the module was built.
VNEXT_ACCEPTANCE_TARGETS[ENTITY_RESOLUTION_SUITE_KEY] = deepcopy(ENTITY_RESOLUTION_TARGETS)
VNEXT_ACCEPTANCE_TARGETS[GRAPH_HOP_RETRIEVAL_SUITE_KEY] = deepcopy(GRAPH_HOP_RETRIEVAL_TARGETS)


def canonical_semantic_eval_release_contract() -> JsonObject:
    """Derive the release verifier's case linkage from canonical generators.

    This intentionally returns only non-secret, deterministic identities. The
    release checker uses it to bind each case key to the exact query, expected
    target, title, and correction/entity semantics produced by this candidate.
    """
    from alicebot_api.vnext_entity_names import normalize_entity_name

    retrieval_corpus = generate_vnext_benchmark_corpus()
    correction_corpus = generate_correction_suppression_corpus()
    decision_corpus = generate_decision_recovery_corpus()
    provenance_corpus = generate_provenance_explanation_corpus()
    entity_corpus = generate_entity_resolution_corpus()
    graph_corpus = generate_graph_hop_corpus()

    return {
        RETRIEVAL_QUALITY_SUITE_KEY: {
            "title": RETRIEVAL_QUALITY_TITLE,
            "cases": [
                {
                    "case_key": row["query_key"],
                    "query": row["query"],
                    "expected_memory_key": row["expected_memory_key"],
                    "subset": row["subset"],
                }
                for row in cast(list[JsonObject], retrieval_corpus["queries"])
            ],
        },
        CORRECTION_SUPPRESSION_SUITE_KEY: {
            "title": CORRECTION_SUPPRESSION_TITLE,
            "cases": [
                {
                    "case_key": row["case_key"],
                    "query": row["query"],
                    "original_memory_key": (
                        "agentic_memory.semantic.vnext-eval/correction/"
                        f"{row['case_key']}/original"
                    ),
                    "replacement_memory_key": (
                        "agentic_memory.semantic.vnext-eval/correction/"
                        f"{row['case_key']}/replacement"
                    ),
                    "rejected_memory_key": (
                        "agentic_memory.semantic.vnext-eval/correction/"
                        f"{row['case_key']}/rejected"
                    ),
                }
                for row in cast(list[JsonObject], correction_corpus["cases"])
            ],
        },
        DECISION_RECOVERY_SUITE_KEY: {
            "title": DECISION_RECOVERY_TITLE,
            "cases": [
                {
                    "case_key": row["query_key"],
                    "query": row["query"],
                    "expected_memory_key": row["expected_memory_key"],
                }
                for row in cast(list[JsonObject], decision_corpus["queries"])
            ],
        },
        PROVENANCE_EXPLANATION_SUITE_KEY: {
            "title": PROVENANCE_EXPLANATION_TITLE,
            "cases": [
                {
                    "case_key": row["case_key"],
                    "corrected": bool(row.get("corrected_text")),
                }
                for row in cast(list[JsonObject], provenance_corpus["memories"])
            ],
        },
        ENTITY_RESOLUTION_SUITE_KEY: {
            "title": ENTITY_RESOLUTION_TITLE,
            "cases": [
                {
                    "case_key": row["group_key"],
                    "canonical_normalized": normalize_entity_name(
                        str(row["canonical_name"])
                    ),
                    "expected_alias": row.get("expected_alias"),
                }
                for row in cast(list[JsonObject], entity_corpus["groups"])
            ],
        },
        GRAPH_HOP_RETRIEVAL_SUITE_KEY: {
            "title": GRAPH_HOP_RETRIEVAL_TITLE,
            "cases": [
                {
                    "case_key": row["group_key"],
                    "query": row["query"],
                    "expected_memory_key": f"{GRAPH_HOP_MEMORY_KEY_PREFIX}{index:03d}",
                }
                for index, row in enumerate(
                    cast(list[JsonObject], graph_corpus["groups"]),
                    start=1,
                )
            ],
        },
    }


def _run_memory_quality_suite(suite_key: str, *, store: object | None) -> JsonObject:
    runner = _MEMORY_QUALITY_SUITE_RUNNERS[suite_key]
    if store is not None:
        return runner(store)

    def _run(live_store: object, *, backend: str | None = None) -> JsonObject:
        return runner(live_store, backend=backend)

    return _run_suite_against_live_store(run_with_store=_run, skipped=lambda reason: runner(None) | {"reason": reason})


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------


def _resolve_suite_keys(suite: str | None) -> tuple[str, ...]:
    requested = "all" if suite is None else suite.strip().lower()
    if requested == "all":
        return VNEXT_EVAL_SUITE_ORDER
    if requested not in VNEXT_EVAL_SUITE_ORDER:
        raise ValueError(f"unknown vNext eval suite: {suite}")
    return (requested,)


def _validate_corpus_counts(corpus: JsonObject) -> JsonObject:
    actual_counts = cast(JsonObject, corpus.get("counts", {}))
    mismatches = {
        key: {"expected": expected, "actual": actual_counts.get(key)}
        for key, expected in VNEXT_BENCHMARK_EXPECTED_COUNTS.items()
        if actual_counts.get(key) != expected
    }
    return {
        "expected": deepcopy(VNEXT_BENCHMARK_EXPECTED_COUNTS),
        "actual": actual_counts,
        "status": "pass" if not mismatches else "fail",
        "mismatches": mismatches,
    }


def _generated_at(now_fn: Callable[[], datetime] | None) -> str:
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_vnext_evals(
    *,
    suite: str | None = "all",
    corpus_path: str | Path | None = None,
    store: object | None = None,
    retrieval_fn: RetrievalFn | None = None,
    now_fn: Callable[[], datetime] | None = None,
    release_gate: bool = False,
) -> JsonObject:
    """Run the vNext eval suites and assemble an honest report.

    Overall ``status`` is ``"pass"`` only when every *executed* suite passed;
    skipped suites are listed separately and never counted as a pass. When no
    suite could execute at all the report status is ``"skipped"``. A release
    gate is stricter: any skipped requested suite makes the aggregate fail.

    ``release_gate`` marks a canonical/release-designated run. In that mode the
    retrieval-quality suite refuses to report an unqualified ``"pass"`` when the
    vector/paraphrase gate never ran (it reports ``"pass_fts_only"``). The
    aggregate fails unless every requested suite executes with nonempty cases
    and passing target checks, so the release gate can never be green without
    measuring semantic retrieval quality. Individual case misses remain in the
    report as diagnostics; the suite's declared aggregate targets determine
    its verdict.
    """
    corpus = load_vnext_benchmark_corpus(corpus_path)
    if corpus.get("schema_version") != VNEXT_EVAL_CORPUS_SCHEMA_VERSION:
        raise ValueError("unexpected vNext eval corpus schema version")
    corpus_validation = _validate_corpus_counts(corpus)
    requested_suite = "all" if suite is None else suite.strip().lower()
    suite_keys = _resolve_suite_keys(suite)

    suites: list[JsonObject] = []
    for suite_key in suite_keys:
        if suite_key == RETRIEVAL_QUALITY_SUITE_KEY:
            suites.append(
                _run_retrieval_quality_suite(
                    corpus=corpus, store=store, retrieval_fn=retrieval_fn, release_gate=release_gate
                )
            )
        elif suite_key in _MEMORY_QUALITY_SUITE_RUNNERS:
            suites.append(_run_memory_quality_suite(suite_key, store=store))

    executed_suites = [suite_report for suite_report in suites if suite_report["status"] != "skipped"]
    skipped_suites = [suite_report for suite_report in suites if suite_report["status"] == "skipped"]
    executed_statuses = [suite_report["status"] for suite_report in executed_suites]
    release_cases_present = all(
        isinstance(suite_report.get("cases"), list)
        and bool(cast(list[object], suite_report["cases"]))
        for suite_report in executed_suites
    )
    release_target_checks_pass = all(
        isinstance(suite_report.get("metrics"), dict)
        and isinstance(cast(JsonObject, suite_report["metrics"]).get("target_checks"), dict)
        and bool(cast(JsonObject, suite_report["metrics"])["target_checks"])
        and all(
            value == "pass"
            for value in cast(
                JsonObject,
                cast(JsonObject, suite_report["metrics"])["target_checks"],
            ).values()
        )
        for suite_report in executed_suites
    )
    release_suites_pass = all(
        suite_report.get("status") == "pass" for suite_report in executed_suites
    )
    if (
        corpus_validation["status"] != "pass"
        or (
            release_gate
            and (
                bool(skipped_suites)
                or not release_cases_present
                or not release_target_checks_pass
                or not release_suites_pass
            )
        )
    ):
        status = "fail"
    elif not executed_suites:
        status = "skipped"
    elif all(suite_status == "pass" for suite_status in executed_statuses):
        status = "pass"
    elif all(suite_status in {"pass", "pass_fts_only"} for suite_status in executed_statuses):
        # No hard failures, but at least one suite could not measure semantic
        # quality (fts_only under the release gate): not an unqualified pass.
        status = "pass_fts_only"
    else:
        status = "fail"

    case_count = sum(len(cast(list[JsonObject], suite_report["cases"])) for suite_report in executed_suites)
    passed_case_count = sum(
        1
        for suite_report in executed_suites
        for case in cast(list[JsonObject], suite_report["cases"])
        if case.get("status") == "pass"
    )
    retrieval_suite = next(
        (
            suite_report
            for suite_report in suites
            if suite_report.get("suite_key") == RETRIEVAL_QUALITY_SUITE_KEY
        ),
        None,
    )
    retrieval_metrics = (
        cast(JsonObject, retrieval_suite.get("metrics"))
        if isinstance(retrieval_suite, dict)
        and isinstance(retrieval_suite.get("metrics"), dict)
        else {}
    )
    seeding = retrieval_metrics.get("seeding")
    embedding_signature = (
        cast(JsonObject, seeding.get("embedding_signature"))
        if isinstance(seeding, dict)
        and isinstance(seeding.get("embedding_signature"), dict)
        else None
    )
    report: JsonObject = {
        "schema_version": VNEXT_EVAL_REPORT_SCHEMA_VERSION,
        "generated_at": _generated_at(now_fn),
        "suite": requested_suite,
        "status": status,
        "release_gate": release_gate,
        "embedding_signature": deepcopy(embedding_signature),
        "targets": deepcopy(VNEXT_ACCEPTANCE_TARGETS),
        "corpus": {
            "schema_version": corpus.get("schema_version"),
            "corpus_digest": corpus.get("corpus_digest"),
            "counts": corpus_validation,
        },
        "skipped_suites": [
            {"suite_key": suite_report["suite_key"], "reason": suite_report.get("reason")}
            for suite_report in skipped_suites
        ],
        "summary": {
            "status": status,
            "suite_count": len(suites),
            "executed_suite_count": len(executed_suites),
            "skipped_suite_count": len(skipped_suites),
            "case_count": case_count,
            "passed_case_count": passed_case_count,
            "failed_case_count": case_count - passed_case_count,
            "pass_rate": passed_case_count / max(case_count, 1),
            "suite_order": list(suite_keys),
        },
        "suites": suites,
    }
    # Wall-clock time is intentionally excluded, but every semantic field is
    # included. This is separate from the attestation's byte-level file hash.
    report["report_digest"] = semantic_eval_report_digest(report)
    return report


def write_vnext_eval_report(
    *,
    report: JsonObject,
    report_path: str | Path | None = None,
) -> Path:
    path = Path(report_path) if report_path is not None else _default_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.resolve()


__all__ = [
    "CORRECTION_MEMORY_KEY_PREFIX",
    "CORRECTION_SUPPRESSION_SUITE_KEY",
    "CORRECTION_SUPPRESSION_TARGETS",
    "DECISION_MEMORY_KEY_PREFIX",
    "DECISION_RECOVERY_SUITE_KEY",
    "DECISION_RECOVERY_TARGETS",
    "ENTITY_RESOLUTION_SUITE_KEY",
    "ENTITY_RESOLUTION_TARGETS",
    "GRAPH_HOP_MEMORY_KEY_PREFIX",
    "GRAPH_HOP_RETRIEVAL_SUITE_KEY",
    "GRAPH_HOP_RETRIEVAL_TARGETS",
    "generate_entity_resolution_corpus",
    "generate_graph_hop_corpus",
    "run_entity_resolution_eval",
    "run_graph_hop_retrieval_eval",
    "MEMORY_TYPES_FILTER_FIELD",
    "PROVENANCE_EXPLANATION_SUITE_KEY",
    "PROVENANCE_EXPLANATION_TARGETS",
    "PROVENANCE_MEMORY_KEY_PREFIX",
    "RETRIEVAL_QUALITY_RECALL_LIMIT",
    "RETRIEVAL_QUALITY_SUITE_KEY",
    "RETRIEVAL_QUALITY_TITLE",
    "RETRIEVAL_QUALITY_TARGETS",
    "SUBSET_LEXICAL_OVERLAP",
    "SUBSET_PARAPHRASE",
    "VNEXT_ACCEPTANCE_TARGETS",
    "VNEXT_BENCHMARK_EXPECTED_COUNTS",
    "VNEXT_EVAL_CORPUS_SCHEMA_VERSION",
    "VNEXT_EVAL_DATABASE_URL_ENV",
    "VNEXT_EVAL_FIXED_VALID_FROM",
    "VNEXT_EVAL_FIXED_VALID_TO",
    "VNEXT_EVAL_LIVE_STORE_SKIP_REASON",
    "VNEXT_EVAL_MEMORY_KEY_PREFIX",
    "VNEXT_EVAL_REPORT_SCHEMA_VERSION",
    "VNEXT_EVAL_SQLITE_URL_PREFIX",
    "VNEXT_EVAL_SUITE_ORDER",
    "eval_content_tokens",
    "eval_token_overlap",
    "filtered_retrieval_fn",
    "canonical_semantic_eval_release_contract",
    "generate_correction_suppression_corpus",
    "generate_decision_recovery_corpus",
    "generate_provenance_explanation_corpus",
    "generate_vnext_benchmark_corpus",
    "latency_percentile",
    "load_vnext_benchmark_corpus",
    "production_retrieval_fn",
    "recall_at_k",
    "reciprocal_rank",
    "retrieval_request_supports_memory_types",
    "run_correction_suppression_eval",
    "run_decision_recovery_eval",
    "run_provenance_explanation_eval",
    "run_retrieval_quality_eval",
    "run_vnext_evals",
    "seed_retrieval_corpus",
    "semantic_eval_report_digest",
    "write_vnext_benchmark_corpus",
    "write_vnext_eval_report",
]
