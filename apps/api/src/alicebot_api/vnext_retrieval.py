"""Context Pack retrieval service (Context API v2).

Everything in this module is deterministic: ranking is reciprocal rank
fusion over store-provided stages, the token budget is a greedy packer,
budget strategies only reorder which sections/items are offered to that
packer, and depth tiers only switch stages and sections on or off. NO
depth tier, strategy, or section performs LLM synthesis, summarization,
or any other model call — the pack is a pure function of stored rows
plus the request (house no-fake-intelligence rule). Equal-score ordering
ties resolve through a content-stable cascade (``content_stable_tiebreak``,
disclosed in the trace as ``fusion.tie_break``) so re-ingesting the same
content — new uuids, same rows — reproduces the same pack composition;
the id remains the final total-order key.

One disclosed, opt-in exception to the no-model-call rule: the reranker
stage (``vnext_reranker``), provider-side listwise RELEVANCE SCORING —
never synthesis — over the fused candidate pools, between fusion and the
budget packer. It is dormant (zero calls, fused order stands, packs
byte-identical) unless ``ALICE_RERANKER_BASE_URL``/``ALICE_RERANKER_MODEL``
are configured, fails open to fused order on any provider failure, and is
disclosed in the trace under ``stages.reranker`` whenever configured.

Budget strategies (``VNextRetrievalRequest.budget_strategy``) change the
greedy packer's section order; ``recent_first`` and ``facts_first``
additionally reorder the memories list before packing. When
``budget_strategy`` is the default ``balanced``, ``classify_pack_view``
picks a loops / facts / sources view from the query and packs that
section first. An explicit strategy other than ``balanced`` still wins.
Loops-first order is internal; it is not a public ``BUDGET_STRATEGIES``
value. Depth tiers
(``VNextRetrievalRequest.context_depth``) trade cost for coverage:
``minimal`` (FTS only, no sources/contradictions/typed sections/recent
changes, max_items capped), ``low`` (today's default hybrid behavior),
``medium`` (low with the contradictions stage forced on for every query
type), and ``high`` (medium plus supersession chain notes). Explicit
``include_sources``/``include_contradictions`` flags always override the
tier default — the caller wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import inspect
import json
import logging
import re
from typing import (
    Callable,
    Collection,
    Mapping,
    MutableMapping,
    NamedTuple,
    NotRequired,
    Protocol,
    Sequence,
    TypeVar,
    TypedDict,
    cast,
)
from uuid import uuid4

# Read-only reuse of the contradiction-detection machinery that backs
# VNextContradictionService. compile_context_pack must not mutate state,
# so it calls the pure candidate finder directly instead of
# generate_contradiction_report (which persists edges/artifacts/events).
from alicebot_api import vnext_contradictions

# Coverage mode (aggregation-shaped queries): pure detection, clause
# decomposition, source near-duplicate demotion, and accepted roll-up card
# promotion helpers. Dormant unless detect_aggregation_intent fires on the
# query surface — see the marked "coverage mode" blocks in
# compile_context_pack.
from alicebot_api import vnext_coverage_query

# Currency chains (read-time same-slot update chains): pure grouping of
# the packed memories by derived fact key + supersession edges + event
# dates, so a stale value renders labeled SUPERSEDED below its CURRENT
# replacement. Dormant — memories untouched, no trace stage, packs
# byte-identical — unless the pack actually contains a confirmable
# same-slot group; see the marked "currency chains" blocks in
# compile_context_pack.
from alicebot_api import vnext_currency

# Reranker (disclosed precision stage): provider-side listwise relevance
# scoring between fusion and the budget packer. Dormant — zero provider
# calls, fused order stands, no trace stage — unless the ALICE_RERANKER_*
# env vars are configured or a provider is injected; see the marked
# "reranker" blocks in VNextRetrievalService.
from alicebot_api import vnext_reranker
from alicebot_api.vnext_reranker import RerankProvider, get_reranker_provider
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.vnext_entity_names import normalize_entity_name
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_VERSION,
    EmbeddingProvider,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    endpoint_fingerprint,
    get_embedding_provider,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_grounding import compute_query_grounding
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_label_guard import LabelGuard, admit_loaded
from alicebot_api.vnext_lifecycle import RETIRED_STATUSES
from alicebot_api.vnext_promotion_policy import memory_write_provenance
from alicebot_api.vnext_project_scope import (
    is_global_scope,
    project_floor_shape,
    project_scope_identity,
    project_scopes_overlap,
    resolve_project_scope,
    source_project_scope,
)
from alicebot_api.vnext_ranking import (
    CONTENT_EVENT_METADATA_KEYS,
    TIE_BREAK_CONTENT_STABLE,
    content_stable_event_time as _tiebreak_event_time,
    content_stable_tiebreak,
)
from alicebot_api.vnext_recall_visibility import MEMORY_SEARCHABLE_STATUSES
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from alicebot_api.vnext_store import fts_fallback_tokens
from alicebot_api.vnext_temporal_query import (
    TemporalAnchor,
    derived_timeline_lines,
    parse_event_datetime,
    parse_temporal_anchor,
)


logger = logging.getLogger(__name__)

class QueryInterpretation(TypedDict):
    query: str
    query_type: str
    pack_view: NotRequired[str]
    terms: list[str]
    domains: list[str]
    inferred_domains: list[str]
    projects: list[str]
    people: list[str]
    memory_types: list[str]
    time_window: str | None
    sensitivity_allowed: list[str]
    requires_sources: bool
    requires_contradictions: bool
    requires_raw_evidence: bool


StageSourceT = TypeVar("StageSourceT")


DEFAULT_CONTEXT_PACK_LIMIT = 8
MAX_CONTEXT_PACK_ITEMS = 50
MAX_CONTEXT_PACK_TOKENS = 50_000
# One provenance hop after the first FTS hit. A handful of extra committed
# facts, priced with estimate_item_tokens. Do not raise the pack ceiling to
# make room for this hop.
PROVENANCE_EXPAND_ONCE_MAX_TOKENS = 640
MAX_CONTEXT_SCOPE_VALUES = 50
MAX_TIME_WINDOW_DAYS = 3_650
DEFAULT_SOURCE_LIMIT = 8
DEFAULT_OPEN_LOOP_LIMIT = 8
DEFAULT_RECENT_CHANGES_LIMIT = 5
SCOPED_ROW_OVERFETCH_LIMIT = 200
# Legacy stores expose only ``limit`` (no cursor/offset and no server-side
# scope predicate).  Compatibility deepening therefore needs a finite proof
# boundary: bundled stores never use this ceiling because they apply scope in
# SQL, while an adapter that cannot prove exhaustion before the boundary fails
# closed instead of doubling forever or silently returning an incomplete pack.
LEGACY_SCOPED_SCAN_MAX_ROWS = 16_384
# ``recent_changes`` is an advisory section of a pack, and its events are
# fenced after the store's own LIMIT, so a run of events about memories the
# caller may not read has to be scanned past. The scan stops at this many rows
# and the section keeps what it found, because a short list is safe and a
# failed pack call is not. It sits well under the ceiling above so a vault
# that is mostly hidden costs a bounded amount per pack call.
RECENT_CHANGES_SCAN_MAX_ROWS = 2_048
# Memory ids go to the store in batches of this size. SQLite builds older than
# 3.32 take at most 999 bound variables in one statement.
MEMORY_ID_LOOKUP_BATCH_SIZE = 500
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
STRATEGIC_QUERY_TYPES = {"strategic_synthesis", "contradiction_check", "project_status", "agent_context"}
RRF_K = 60
VECTOR_STAGE_ENABLED = "enabled"
VECTOR_STAGE_DISABLED_NO_PROVIDER = "disabled: no embedding provider configured"
VECTOR_STAGE_DISABLED_NO_STORE_SUPPORT = "disabled: store does not support vector search"
VECTOR_STAGE_DISABLED_QUERY_EMBEDDING_FAILED = "disabled: query_embedding_failed"
# Crude token estimate: ~4 characters of serialized JSON per token. Used by
# the greedy context-pack budget packer; precision is not required, only a
# stable, monotone proxy for payload size.
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4
# A memory whose last_confirmed_at is older than this many days gets a
# "staleness" note attached in the context pack so agents can weigh it.
STALENESS_NOTE_AFTER_DAYS = 90
# valid_to values in year 9999+ are the far-future stand-in for "no expiry"
# (vnext_memory_commit.VALID_TO_UNBOUNDED_SENTINEL, written when a
# COALESCE-style update_memory cannot patch valid_to back to NULL; keep the
# two in sync). An unbounded window carries no validity signal, so pack
# "validity" annotations omit it.
VALID_TO_UNBOUNDED_YEAR = 9999
# Trace exclusion_reason for items selected by ranking but dropped by the
# token budget packer.
EXCLUSION_REASON_TOKEN_BUDGET = "token_budget"
# -- budget strategies ---------------------------------------------------------
# A strategy changes only the ORDER in which sections are offered to the
# greedy packer (and, for recent_first/facts_first, the order of the
# memories list itself). It never changes what was retrieved or ranked.
BUDGET_STRATEGY_BALANCED = "balanced"
BUDGET_STRATEGY_FACTS_FIRST = "facts_first"
BUDGET_STRATEGY_RECENT_FIRST = "recent_first"
BUDGET_STRATEGY_CONTRADICTIONS_FIRST = "contradictions_first"
BUDGET_STRATEGY_SOURCES_FIRST = "sources_first"
BUDGET_STRATEGIES = (
    BUDGET_STRATEGY_BALANCED,
    BUDGET_STRATEGY_FACTS_FIRST,
    BUDGET_STRATEGY_RECENT_FIRST,
    BUDGET_STRATEGY_CONTRADICTIONS_FIRST,
    BUDGET_STRATEGY_SOURCES_FIRST,
)
# Section names double as allocation-report keys and pack section keys.
SECTION_RELEVANT_MEMORIES = "relevant_memories"
SECTION_OPEN_LOOPS = "open_loops"
SECTION_SOURCES = "sources"
SECTION_SUPPORTING_EVIDENCE = "supporting_evidence"
SECTION_CONTRADICTING_EVIDENCE = "contradicting_evidence"
SECTION_ENTITIES = "entities"
SECTION_RECENT_CHANGES = "recent_changes"
SECTION_SUPERSESSION_CONTEXT = "supersession_context"
SECTION_ITEM_ANNOTATIONS = "item_annotations"
SECTION_GROUNDING = "grounding"
SECTION_DERIVED_VALUES = "derived_values"
SUPPLEMENTAL_BUDGET_SECTIONS = (
    SECTION_ITEM_ANNOTATIONS,
    SECTION_ENTITIES,
    SECTION_RECENT_CHANGES,
    SECTION_SUPERSESSION_CONTEXT,
    SECTION_GROUNDING,
    SECTION_DERIVED_VALUES,
)
BUDGETED_ITEM_ANNOTATION_KEYS = ("staleness", "validity", "currency", "event_time")
# These keys are useful request/diagnostic/navigation metadata, not retrieved
# content. ``max_tokens`` budgets every content-bearing section above; the
# final report separately estimates the complete serialized envelope and
# names these exclusions so callers never mistake it for a transport cap.
BUDGET_EXCLUDED_SECTIONS = (
    "context_pack_id",
    "query_interpretation",
    "current_known_state",
    "relevant_beliefs",
    "decisions",
    "procedures",
    "missing_information",
    "warnings",
    "budget",
    "context_depth",
    "trace_id",
    "trace",
    "agent_identity",
    "policy_decision",
)
# Invariant: supporting_evidence always packs after relevant_memories
# because provenance quotes are derived from the packed memories. The
# contradicting_evidence section derives from the memories packed so far;
# under contradictions_first it packs before memories, so it derives from
# the ranking-selected (pre-budget) memories instead — the trade a caller
# opts into by prioritizing contradictions over the memories themselves.
_BALANCED_SECTION_ORDER = (
    SECTION_RELEVANT_MEMORIES,
    SECTION_OPEN_LOOPS,
    SECTION_SOURCES,
    SECTION_SUPPORTING_EVIDENCE,
    SECTION_CONTRADICTING_EVIDENCE,
)
BUDGET_STRATEGY_SECTION_ORDERS: dict[str, tuple[str, ...]] = {
    BUDGET_STRATEGY_BALANCED: _BALANCED_SECTION_ORDER,
    BUDGET_STRATEGY_FACTS_FIRST: _BALANCED_SECTION_ORDER,
    BUDGET_STRATEGY_RECENT_FIRST: _BALANCED_SECTION_ORDER,
    BUDGET_STRATEGY_CONTRADICTIONS_FIRST: (
        SECTION_CONTRADICTING_EVIDENCE,
        SECTION_RELEVANT_MEMORIES,
        SECTION_OPEN_LOOPS,
        SECTION_SOURCES,
        SECTION_SUPPORTING_EVIDENCE,
    ),
    BUDGET_STRATEGY_SOURCES_FIRST: (
        SECTION_SOURCES,
        SECTION_RELEVANT_MEMORIES,
        SECTION_OPEN_LOOPS,
        SECTION_SUPPORTING_EVIDENCE,
        SECTION_CONTRADICTING_EVIDENCE,
    ),
}
# Query-conditioned views used when budget_strategy stays at default
# balanced. Not a public MCP enum; do not add these to BUDGET_STRATEGIES.
PACK_VIEW_LOOPS = "loops"
PACK_VIEW_FACTS = "facts"
PACK_VIEW_SOURCES = "sources"
PACK_VIEWS = (PACK_VIEW_LOOPS, PACK_VIEW_FACTS, PACK_VIEW_SOURCES)
# classify_pack_view reads these word-bounded phrases. Bare "open" is not
# a loops cue, so "open source" is not that view.
PACK_VIEW_LOOP_CUES = (
    "what's open",
    "what is open",
    "still open",
    "open loop",
    "open loops",
    "todo",
    "todos",
    "waiting",
    "blocked",
    "unresolved",
)
PACK_VIEW_SOURCE_CUES = (
    "what did i write",
    "wrote about",
    "written about",
    "write",
    "draft",
    "compose",
    "quote",
    "source",
    "evidence",
)
_LOOPS_FIRST_SECTION_ORDER = (
    SECTION_OPEN_LOOPS,
    SECTION_RELEVANT_MEMORIES,
    SECTION_SOURCES,
    SECTION_SUPPORTING_EVIDENCE,
    SECTION_CONTRADICTING_EVIDENCE,
)
# facts_first boosts these memory_types to the front of the memories list
# (stable within each partition, so fused rank still breaks ties).
FACTS_FIRST_MEMORY_TYPES = frozenset({"semantic", "decision", "preference"})
# -- depth tiers ---------------------------------------------------------------
CONTEXT_DEPTH_MINIMAL = "minimal"
CONTEXT_DEPTH_LOW = "low"
CONTEXT_DEPTH_MEDIUM = "medium"
CONTEXT_DEPTH_HIGH = "high"
CONTEXT_DEPTHS = (
    CONTEXT_DEPTH_MINIMAL,
    CONTEXT_DEPTH_LOW,
    CONTEXT_DEPTH_MEDIUM,
    CONTEXT_DEPTH_HIGH,
)
# minimal caps max_items at min(CONTEXT_DEPTH_MINIMAL_MAX_ITEMS, requested).
CONTEXT_DEPTH_MINIMAL_MAX_ITEMS = 4
# Honest stage status when a tier (not a store limitation) skips a stage.
STAGE_DISABLED_MINIMAL = "disabled: context_depth=minimal"
# Honest stage status when the caller explicitly turned sources off.
SOURCES_STAGE_DISABLED_BY_FLAG = "disabled: include_sources=false"
# -- source stage ----------------------------------------------------------
# The sources section is RRF fusion over up to three ranked lists (keys
# below double as stage_ranks keys in the trace): sources ranked by their
# best chunk-level content hit, provenance sources of the winning memory
# hits in fused rank order, and the legacy title/recency lexical list.
SOURCE_STAGE_CHUNK_FTS = "chunk_fts"
SOURCE_STAGE_PROVENANCE = "provenance"
SOURCE_STAGE_TITLE_RECENCY = "title_recency"
# Honest chunk-list status for stores without search_source_chunks (or
# without the get_source resolver the chunk/provenance lists need).
SOURCE_CHUNK_STAGE_DISABLED_NO_STORE_SUPPORT = "disabled: store does not support source-chunk search"
# Chunk rows fetched per source slot: several chunks of one source may
# outrank the best chunk of another, so the chunk pass over-fetches
# before deduplicating down to distinct parent sources.
SOURCE_CHUNK_CANDIDATE_MULTIPLIER = 4
# Supersession chain notes (high tier): walk supersedes/superseded_by
# pointers at most this many hops in each direction, with a cycle guard.
SUPERSESSION_CHAIN_HOP_LIMIT = 5
SUPERSESSION_STAGE_ENABLED = "enabled"
CONTRADICTIONS_STAGE_ENABLED = "enabled"
CONTRADICTIONS_STAGE_NOT_REQUESTED = "disabled: not requested"
CONTRADICTIONS_STAGE_NO_STORE_SUPPORT = "disabled: store does not support beliefs"
# Entity-hop graph stage: query text -> resolved entities -> memories
# connected to those entities via mentions/about edges. Joins RRF as a third
# ranked list ("graph") next to fts/vector, so a memory with zero lexical
# overlap with the query can still surface through a shared entity.
GRAPH_STAGE_ENABLED = "enabled"
GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH = "disabled: no entity match"
GRAPH_STAGE_DISABLED_NO_STORE_SUPPORT = "disabled: store does not support entities"
# At most this many resolved entities seed the hop; find_entities_by_names
# orders by mention_count DESC, so these are the most-established matches.
GRAPH_ENTITY_MATCH_LIMIT = 5
# Edge types that connect a memory node to an entity node in graph_edges.
MEMORY_ENTITY_EDGE_TYPES = ("mentions", "about")
# Mirror of the stores' _MEMORY_SEARCHABLE_STATUSES_SQL ('active',
# 'accepted'): get_memory does not enforce the searchable-status discipline
# the search_* SQL bakes in, so the graph stage re-applies it in Python. The
# tuple is defined in vnext_recall_visibility, which the embedding door reads
# too, and is re-exported here under its old name.
# Temporal-anchor stage: when parse_temporal_anchor finds a date-bearing
# phrase in the query ("in March 2023", "two months ago"), memories whose
# event window intersects the parsed [start, end) window join RRF as one
# more ranked list ("temporal_anchor") next to fts/vector/graph — never a
# hard filter, so a wrong parse cannot evict lexical/vector/graph hits.
# The stage record (and the list) exist only when an anchor parses; the
# anchor keys on generic query text alone.
TEMPORAL_STAGE_ENABLED = "enabled"
TEMPORAL_STAGE_DISABLED_NO_STORE_SUPPORT = "disabled: store does not support time-window search"
# The same anchor window re-ranks the fused sources stage: candidates the
# chunk/provenance/lexical lists already surfaced whose event date falls
# inside the window form a fourth ranked list (a rank boost, never a new
# recall path). A source's event date is its source_created_at, then the
# first parseable connector-stamped metadata date below, then captured_at
# (write time, the least honest fallback) — mirroring the capture
# service's source_created_at-then-captured_at event-time convention.
SOURCE_STAGE_TEMPORAL = "temporal_anchor"
SOURCE_EVENT_METADATA_KEYS = CONTENT_EVENT_METADATA_KEYS
# Tiny stopword set for entity-name candidate generation: n-grams whose
# first or last token is one of these never name an entity on their own.
ENTITY_NAME_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "about", "at", "be", "by", "did", "do",
        "does", "for", "from", "how", "i", "in", "is", "it", "me", "my",
        "of", "on", "or", "our", "should", "that", "the", "this", "to",
        "was", "we", "were", "what", "when", "where", "which", "who",
        "with", "your",
    }
)
# Bound on candidate names per query so the single find_entities_by_names
# round-trip stays small even for long queries.
ENTITY_NAME_CANDIDATE_LIMIT = 64


class VNextRetrievalValidationError(ValueError):
    """Raised when a vNext retrieval request is invalid."""


class VNextRetrievalCompletenessError(RuntimeError):
    """Raised when a legacy adapter cannot prove scoped recall completeness."""


def _require_document_ranking(ranking: object) -> None:
    """Refuse every source ranking the source stage cannot honour yet.

    Only ``SourceRanking.document()`` has a code path, so any other value is
    refused here and never accepted and ignored. A caller that writes a passage
    ranking before the passage stage exists learns it at the call, not from a
    result that looks like the ranking it asked for. The passage slice replaces
    this refusal with the stage.
    """

    if not isinstance(ranking, SourceRanking):
        raise VNextRetrievalValidationError("ranking must be a SourceRanking value")
    if ranking != SourceRanking.document():
        raise VNextRetrievalValidationError(
            "this build ranks sources by document only; pass SourceRanking.document()"
        )


class VNextRetrievalStore(Protocol):
    """Minimum store surface for context-pack retrieval.

    Stores may additionally expose ``search_memories_fts`` and
    ``search_memories_vector`` (see ``PostgresVNextStore``); the service
    detects them at runtime and degrades to ``search_memories`` otherwise.
    ``search_memories_fts`` implementations should accept
    ``match_any: bool = False`` (the OR-fallback pass for multi-word
    queries the strict AND pass missed); stores that predate the kwarg
    simply never get the fallback.
    The same applies to ``list_events`` (recent_changes section),
    ``list_beliefs`` (contradicting_evidence section), the entity
    substrate ``find_entities_by_names``/``get_memory`` (entity-hop graph
    stage), ``search_source_chunks``/``get_source`` (the chunk-content
    and provenance lists of the fused sources stage), and
    ``search_memories_by_time`` (the temporal-anchor stage for
    date-bearing queries): stores without them yield empty sections / an
    honest disabled stage status instead of failing.

    ``memory_types``/``projects``/``created_by_agent_ids``/``run_id`` are
    only forwarded to the store when the request sets them, so minimal
    stores that predate those keyword arguments keep working for unfiltered
    requests. Bundled ranked searches also accept the optional
    ``scope_people``/``scope_person_memory_ids``/``scope_window_*`` predicate
    arguments so filtering happens before LIMIT. Third-party adapters without
    them retain a complete (uncapped) compatibility scan. Optional bulk
    ``list_memory_entity_edges``/``get_memories_by_ids``/
    ``list_provenance_links_for_targets``/``get_sources_by_ids`` methods avoid
    graph and evidence N+1 reads; legacy single-row methods remain supported.
    """

    def append_event(self, event: JsonObject) -> JsonObject: ...

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_CONTEXT_PACK_LIMIT,
        memory_types: tuple[str, ...] = (),
        projects: tuple[str, ...] = (),
        created_by_agent_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        include_expired: bool = False,
    ) -> list[JsonObject]: ...

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_SOURCE_LIMIT,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> list[JsonObject]: ...

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_OPEN_LOOP_LIMIT,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> list[JsonObject]: ...

    def list_provenance_links(self, *, target_type: str, target_id: str) -> list[JsonObject]: ...

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[JsonObject]: ...


@dataclass(frozen=True, slots=True)
class VNextRetrievalRequest:
    query: str
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()
    # Scope filters over the first-class memory columns: restrict retrieval
    # to memories written by these agents / this agent run. filter_run_id is
    # distinct from ``run_id`` below, which attributes the retrieval *event*
    # to the caller's run in the event log.
    created_by_agent_ids: tuple[str, ...] = ()
    filter_run_id: str | None = None
    time_window: str = "all"
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    # None means "let the depth tier decide" (sources/contradictions on for
    # low/medium/high, off for minimal; contradictions additionally gated to
    # strategic query types at low). An explicit True/False always wins over
    # the tier default.
    include_sources: bool | None = None
    include_contradictions: bool | None = None
    max_items: int = DEFAULT_CONTEXT_PACK_LIMIT
    # None means "no token budget": nothing is dropped, but the pack still
    # reports its token estimate. When set, the greedy packer enforces it.
    max_tokens: int | None = None
    # How the greedy packer spends max_tokens; see BUDGET_STRATEGIES.
    budget_strategy: str = BUDGET_STRATEGY_BALANCED
    # Cost/coverage tier; see CONTEXT_DEPTHS. "low" is today's default
    # hybrid behavior. No tier performs LLM synthesis.
    context_depth: str = CONTEXT_DEPTH_LOW
    actor_type: str = "system"
    actor_id: str | None = None
    agent_identity: JsonObject | None = None
    policy_decision: JsonObject | None = None
    trace_id: str | None = None
    run_id: str | None = None
    # The caller's "now" for resolving relative temporal phrases in the
    # query ("last week", "two months ago"). None means the service uses
    # the current UTC time; parsing itself never reads the wall clock.
    reference_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    item: JsonObject
    target_type: str
    rank: int
    rrf_score: float
    stage_ranks: dict[str, int]
    selected: bool
    exclusion_reason: str | None = None

    def to_trace_record(self) -> JsonObject:
        return {
            "target_type": self.target_type,
            "target_id": str(self.item.get("id")),
            "rank": self.rank,
            "rrf_score": round(self.rrf_score, 6),
            "stage_ranks": dict(self.stage_ranks),
            "selected": self.selected,
            "exclusion_reason": self.exclusion_reason,
        }


def normalize_query(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    if normalized == "":
        raise VNextRetrievalValidationError("query must not be empty")
    return normalized


def query_terms(query: str) -> list[str]:
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", normalize_query(query))]
    stopwords = {"about", "what", "when", "where", "which", "with", "from", "this", "that", "should", "could"}
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in stopwords or term in seen:
            continue
        deduped.append(term)
        seen.add(term)
    return deduped


def entity_name_candidates(query: str) -> list[str]:
    """Candidate normalized entity names generated from the query text.

    Unigrams, bigrams, and trigrams over the ``normalize_entity_name`` form
    of the query, skipping any n-gram whose first or last token is a
    stopword (a stopword can sit inside a longer name, e.g. "bank of
    america"). Deduplicated, order-preserving, and bounded to
    ``ENTITY_NAME_CANDIDATE_LIMIT`` so the resolution lookup stays one
    small round-trip.
    """
    tokens = normalize_entity_name(query).split()
    candidates: list[str] = []
    seen: set[str] = set()
    for size in (1, 2, 3):
        for start in range(len(tokens) - size + 1):
            gram = tokens[start : start + size]
            if gram[0] in ENTITY_NAME_STOPWORDS or gram[-1] in ENTITY_NAME_STOPWORDS:
                continue
            name = " ".join(gram)
            if name in seen:
                continue
            seen.add(name)
            candidates.append(name)
            if len(candidates) >= ENTITY_NAME_CANDIDATE_LIMIT:
                return candidates
    return candidates


def _contains_any(query: str, words: tuple[str, ...]) -> bool:
    lowered = query.casefold()
    return any(word in lowered for word in words)


def _contains_domain_cue(query: str, cue: str) -> bool:
    """Match a domain cue as words, never as a substring of another word."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(cue)}(?![A-Za-z0-9_])", query) is not None


def _validate_choice(value: str, *, field_name: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise VNextRetrievalValidationError(
            f"{field_name} must be one of: {', '.join(choices)} (got {value!r})"
        )


def _resolve_section_flags(request: VNextRetrievalRequest, *, query_type: str) -> tuple[bool, bool]:
    """Resolve (requires_sources, requires_contradictions) — caller wins.

    Explicit ``include_sources``/``include_contradictions`` always take
    precedence. When unset (None), the depth tier decides: minimal turns
    both off; low keeps today's defaults (sources on, contradictions only
    for strategic query types); medium/high force contradictions on for
    every query type — that gate is the only low/medium difference.
    """
    depth = request.context_depth
    if request.include_sources is not None:
        requires_sources = request.include_sources
    else:
        requires_sources = depth != CONTEXT_DEPTH_MINIMAL
    if request.include_contradictions is not None:
        requires_contradictions = request.include_contradictions
    elif depth == CONTEXT_DEPTH_MINIMAL:
        requires_contradictions = False
    elif depth in (CONTEXT_DEPTH_MEDIUM, CONTEXT_DEPTH_HIGH):
        requires_contradictions = True
    else:
        requires_contradictions = query_type in STRATEGIC_QUERY_TYPES
    return requires_sources, requires_contradictions


def classify_pack_view(query: str) -> str:
    """Pick loops / facts / sources from word-bounded query cues.

    Reads ``PACK_VIEW_LOOP_CUES`` first, then ``PACK_VIEW_SOURCE_CUES``.
    A curly apostrophe (U+2019) folds to ASCII ``'`` before matching.
    Bare ``open`` is not a loops cue. When neither list hits, the view is
    facts, which keeps today's balanced section order.
    """

    lowered = normalize_query(query).casefold().replace("\u2019", "'")
    if any(_contains_domain_cue(lowered, cue) for cue in PACK_VIEW_LOOP_CUES):
        return PACK_VIEW_LOOPS
    if any(_contains_domain_cue(lowered, cue) for cue in PACK_VIEW_SOURCE_CUES):
        return PACK_VIEW_SOURCES
    return PACK_VIEW_FACTS


def budget_section_order(*, budget_strategy: str, pack_view: str) -> tuple[str, ...]:
    """Section offer order for the greedy packer.

    An explicit ``budget_strategy`` other than ``balanced`` still wins.
    Default ``balanced`` follows ``pack_view``. Loops-first order is
    internal; it is not a ``BUDGET_STRATEGIES`` value.
    """

    if budget_strategy != BUDGET_STRATEGY_BALANCED:
        return BUDGET_STRATEGY_SECTION_ORDERS[budget_strategy]
    if pack_view == PACK_VIEW_LOOPS:
        return _LOOPS_FIRST_SECTION_ORDER
    if pack_view == PACK_VIEW_SOURCES:
        return BUDGET_STRATEGY_SECTION_ORDERS[BUDGET_STRATEGY_SOURCES_FIRST]
    return _BALANCED_SECTION_ORDER


def classify_query(request: VNextRetrievalRequest) -> QueryInterpretation:
    _validate_choice(request.context_depth, field_name="context_depth", choices=CONTEXT_DEPTHS)
    query = normalize_query(request.query)
    lowered = query.casefold()

    if _contains_any(lowered, ("contradict", "conflict", "inconsistent", "disagree")):
        query_type = "contradiction_check"
    elif _contains_any(lowered, ("open loop", "todo", "waiting", "blocked", "unresolved")):
        query_type = "open_loop_review"
    elif _contains_any(lowered, ("project", "status", "roadmap", "milestone", "next step")):
        query_type = "project_status"
    elif _contains_any(lowered, ("who is", "person", "people", "relationship")):
        query_type = "people_context"
    elif _contains_any(lowered, ("when", "timeline", "history", "changed", "since")):
        query_type = "temporal_recall"
    elif _contains_any(lowered, ("draft", "write", "compose")):
        query_type = "draft_generation"
    elif _contains_any(lowered, ("agent", "context pack", "handoff", "resume")):
        query_type = "agent_context"
    elif query.startswith('"') and query.endswith('"'):
        query_type = "exact_recall"
    else:
        query_type = "strategic_synthesis"

    # Caller-supplied domains are authorization/retrieval predicates. Domain
    # inference is necessarily heuristic, so disclose it separately and never
    # turn it into a destructive store filter.
    domains = list(request.domains)
    inferred_domains = _infer_domains(lowered)
    sensitivity_allowed = list(request.sensitivity_allowed) or list(DEFAULT_SENSITIVITY_ALLOWED)
    requires_sources, requires_contradictions = _resolve_section_flags(request, query_type=query_type)
    pack_view = classify_pack_view(query)
    interpretation: QueryInterpretation = {
        "query": query,
        "query_type": query_type,
        "terms": query_terms(query),
        "domains": domains,
        "inferred_domains": inferred_domains,
        "projects": list(request.projects),
        "people": list(request.people),
        "memory_types": list(request.memory_types),
        "time_window": request.time_window,
        "sensitivity_allowed": sensitivity_allowed,
        "requires_sources": requires_sources,
        "requires_contradictions": requires_contradictions,
        "requires_raw_evidence": _contains_any(lowered, ("quote", "source", "evidence", "prove", "where did")),
    }
    # Absent on the default facts envelope so ordinary packs stay
    # byte-identical. Disclose only when the query picked loops or sources.
    if pack_view != PACK_VIEW_FACTS:
        interpretation["pack_view"] = pack_view
    return interpretation


def _infer_domains(lowered_query: str) -> list[str]:
    domains: list[str] = []
    if any(
        _contains_domain_cue(lowered_query, cue)
        for cue in ("alice", "project", "roadmap", "sprint", "build")
    ):
        domains.extend(["project", "professional"])
    if any(
        _contains_domain_cue(lowered_query, cue)
        for cue in ("family", "health", "spiritual", "legal")
    ):
        domains.append("personal")
    return list(dict.fromkeys(domains))


def _allowed(item: JsonObject, *, domains: list[str], sensitivity_allowed: list[str]) -> str | None:
    item_domain = item.get("domain")
    item_sensitivity = item.get("sensitivity")
    if domains and isinstance(item_domain, str) and item_domain not in domains and item_domain != "unknown":
        return "domain_filtered"
    if isinstance(item_sensitivity, str) and item_sensitivity not in sensitivity_allowed:
        return "sensitivity_filtered"
    return None


_TIME_WINDOW_PATTERN = re.compile(r"^(?P<days>[1-9][0-9]{0,3})d$", re.IGNORECASE)
_PROJECT_SCOPE_KEYS = ("project_id", "project", "projects", "project_scope")
_PEOPLE_SCOPE_KEYS = ("person_id", "person_ids", "person", "people", "people_ids")
_SCOPE_EVENT_KEYS = (
    "valid_from",
    "source_created_at",
    "occurred_at",
    "opened_at",
    "last_seen_at",
    "updated_at",
    "first_seen_at",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class _ResolvedRetrievalScope:
    projects: frozenset[str]
    people: frozenset[str]
    window_start: datetime | None
    window_end: datetime | None
    #: Domains whose GLOBAL rows (a scope that holds no Alice project id) this
    #: read leaves out, before fusion, so a held-back row never spends a rank
    #: (spec 6.4 item 5). Required, with no default: a site that builds a scope
    #: states the choice, and the empty set is written where it is meant. The
    #: brief passes ``SENSITIVE_DOMAINS`` for the project view.
    exclude_global_domains: frozenset[str]

    @property
    def active(self) -> bool:
        return bool(
            self.projects
            or self.people
            or self.window_start is not None
            or self.window_end is not None
            or self.exclude_global_domains
        )


def _normalized_scope_values(values: Sequence[str], *, field_name: str) -> frozenset[str]:
    if len(values) > MAX_CONTEXT_SCOPE_VALUES:
        raise VNextRetrievalValidationError(
            f"{field_name} is limited to {MAX_CONTEXT_SCOPE_VALUES} values"
        )
    normalized = frozenset(value.strip().casefold() for value in values if value.strip())
    if any(len(value) > 200 for value in normalized):
        raise VNextRetrievalValidationError(f"{field_name} values must be at most 200 characters")
    return normalized


def _resolve_retrieval_scope(request: VNextRetrievalRequest) -> _ResolvedRetrievalScope:
    if len(request.projects) > MAX_CONTEXT_SCOPE_VALUES:
        raise VNextRetrievalValidationError(
            f"projects is limited to {MAX_CONTEXT_SCOPE_VALUES} values"
        )
    projects = frozenset(project_scope_identity(request.projects))
    if any(len(value) > 200 for value in projects):
        raise VNextRetrievalValidationError("projects values must be at most 200 characters")
    people = _normalized_scope_values(request.people, field_name="people")
    raw_window = request.time_window.strip().casefold()
    if raw_window == "all":
        return _ResolvedRetrievalScope(
            projects=projects,
            people=people,
            window_start=None,
            window_end=None,
            # An explicit search: every global row the caller may read is returned.
            exclude_global_domains=frozenset(),
        )
    match = _TIME_WINDOW_PATTERN.fullmatch(raw_window)
    if match is None:
        raise VNextRetrievalValidationError(
            "time_window must be 'all' or a positive day window such as '7d' or '30d'"
        )
    days = int(match.group("days"))
    if days > MAX_TIME_WINDOW_DAYS:
        raise VNextRetrievalValidationError(
            f"time_window must not exceed {MAX_TIME_WINDOW_DAYS}d"
        )
    window_end = parse_event_datetime(
        request.reference_time if request.reference_time is not None else datetime.now(UTC)
    )
    if window_end is None:  # defensive: request.reference_time is typed datetime
        raise VNextRetrievalValidationError("reference_time must be a valid datetime")
    return _ResolvedRetrievalScope(
        projects=projects,
        people=people,
        window_start=window_end - timedelta(days=days),
        window_end=window_end,
        exclude_global_domains=frozenset(),
    )


def _scope_values_from(value: object) -> set[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return {stripped.casefold()} if stripped else set()
    if isinstance(value, Mapping):
        values: set[str] = set()
        for nested in value.values():
            values.update(_scope_values_from(nested))
        return values
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = set()
        for nested in value:
            values.update(_scope_values_from(nested))
        return values
    return set()


def _row_scope_values(row: Mapping[str, object], keys: Sequence[str]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        values.update(_scope_values_from(row.get(key)))
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        for key in keys:
            values.update(_scope_values_from(metadata.get(key)))
    return values


def _row_project_scope_values(row: Mapping[str, object]) -> set[str]:
    """Resolve project scope without widening canonical rows through stale metadata.

    New memory rows expose ``project_scope`` at the top level.  Legacy rows may
    instead carry a singular ``project_id`` or metadata-only scope.  Once a
    higher-priority representation is present, lower-priority values must not
    widen access to a project the resource no longer belongs to.
    """
    return set(resolve_project_scope(row).identity)


def _source_project_scope_values(row: Mapping[str, object]) -> set[str]:
    """Resolve a persisted source envelope without widening stale aliases."""

    return set(project_scope_identity(source_project_scope(row)))


def _row_scope_event_time(row: Mapping[str, object]) -> datetime | None:
    for key in _SCOPE_EVENT_KEYS:
        parsed = parse_event_datetime(row.get(key))
        if parsed is not None:
            return parsed
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        for key in (*SOURCE_EVENT_METADATA_KEYS, "occurred_at", "opened_at"):
            parsed = parse_event_datetime(metadata.get(key))
            if parsed is not None:
                return parsed
    return parse_event_datetime(row.get("captured_at"))


def _row_floor(row: Mapping[str, object]) -> tuple[str, ...]:
    shape, floor = project_floor_shape(row)
    return floor if shape == "list" else ()


def _project_scope_meets(
    row_scope: set[str],
    requested: Collection[str],
    *,
    floor: Collection[str] = (),
) -> bool:
    """Does a row's resolved project scope meet the requested tuple?

    The tuple may hold the reserved global marker (spec 6.1): it asks for a row
    whose scope holds no Alice project id. The one predicate in
    ``vnext_project_scope`` decides, so a request without the marker keeps the
    plain intersection it always had. On that global branch the row's floor
    must also sit inside the view.
    """

    return project_scopes_overlap(
        tuple(sorted(row_scope)),
        tuple(sorted(requested)),
        floor=tuple(floor),
    )


def _is_held_back_global(
    row: Mapping[str, object],
    row_scope: set[str],
    excluded_domains: Collection[str],
) -> bool:
    """A global row (no Alice project id in its scope) whose stored domain is held back.

    The test is on the row: a project's own row in the same domain is not held back,
    and a global row in any other domain is not either.
    """

    return row.get("domain") in excluded_domains and is_global_scope(tuple(sorted(row_scope)))


def _row_matches_scope(
    row: Mapping[str, object],
    scope: _ResolvedRetrievalScope,
    *,
    person_linked_memory_ids: frozenset[str] = frozenset(),
    source_scope_envelope: bool = False,
) -> bool:
    project_scope = (
        _source_project_scope_values(row)
        if source_scope_envelope
        else _row_project_scope_values(row)
    )
    if scope.projects and not _project_scope_meets(
        project_scope,
        scope.projects,
        floor=_row_floor(row),
    ):
        return False
    if scope.exclude_global_domains and _is_held_back_global(
        row, project_scope, scope.exclude_global_domains
    ):
        return False
    if scope.people:
        direct_people = _row_scope_values(row, _PEOPLE_SCOPE_KEYS)
        linked = str(row.get("id")) in person_linked_memory_ids
        if not linked and not (direct_people & scope.people):
            return False
    if scope.window_start is not None or scope.window_end is not None:
        event_time = _row_scope_event_time(row)
        if event_time is None or (scope.window_start is not None and event_time < scope.window_start):
            return False
        if scope.window_end is not None and event_time > scope.window_end:
            return False
    return True


def _filter_rows_for_scope(
    rows: Sequence[JsonObject],
    scope: _ResolvedRetrievalScope,
    *,
    person_linked_memory_ids: frozenset[str] = frozenset(),
    source_scope_envelope: bool = False,
) -> list[JsonObject]:
    if not scope.active:
        return list(rows)
    return [
        row
        for row in rows
        if _row_matches_scope(
            row,
            scope,
            person_linked_memory_ids=person_linked_memory_ids,
            source_scope_envelope=source_scope_envelope,
        )
    ]


def _retrieval_row_identity(row: Mapping[str, object]) -> str:
    """Stable identity for compatibility-scan dedupe/progress detection."""
    row_id = row.get("id")
    if row_id not in (None, ""):
        return f"id:{row_id}"
    return "row:" + json.dumps(json_safe(row), sort_keys=True, separators=(",", ":"))


def _dedupe_retrieval_rows(rows: Sequence[JsonObject]) -> list[JsonObject]:
    deduped: list[JsonObject] = []
    seen: set[str] = set()
    for row in rows:
        identity = _retrieval_row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(row)
    return deduped


def _fetch_filtered_prefix(
    fetch: "Callable[[int], tuple[list[JsonObject], StageSourceT]]",
    *,
    select_rows: "Callable[[Sequence[JsonObject]], list[JsonObject]]",
    target: int,
    predicate_applied_before_limit: bool = False,
    initial_limit: int | None = None,
    max_rows: int | None = None,
) -> tuple[list[JsonObject], StageSourceT]:
    """Fetch/select a ranked prefix with finite legacy compatibility deepening.

    ``fetch(limit)`` returns ``(rows, source)`` ranked best-first. Bundled
    stores apply the complete predicate before ranking/limiting and need one
    bounded query. Older adapters retain only the legacy top-N API, so grow the
    requested prefix until enough rows survive or the adapter proves
    exhaustion. The compatibility path is explicitly finite: rows are
    deduplicated, repeated/non-growing prefixes fail closed, and an adapter
    that still returns a full prefix at ``LEGACY_SCOPED_SCAN_MAX_ROWS`` raises
    ``VNextRetrievalCompletenessError`` instead of doubling forever or
    returning a false-negative pack. ``max_rows`` lowers that ceiling for a
    caller that would rather stop early; it never raises it.
    """
    if predicate_applied_before_limit:
        rows, source = fetch(target)
        return _dedupe_retrieval_rows(select_rows(_dedupe_retrieval_rows(rows))), source
    scan_ceiling = (
        LEGACY_SCOPED_SCAN_MAX_ROWS if max_rows is None else min(max_rows, LEGACY_SCOPED_SCAN_MAX_ROWS)
    )
    limit = min(
        scan_ceiling,
        max(target, initial_limit or SCOPED_ROW_OVERFETCH_LIMIT),
    )
    previous_unique_count = -1
    while True:
        raw_rows, source = fetch(limit)
        rows = _dedupe_retrieval_rows(raw_rows)
        filtered = _dedupe_retrieval_rows(select_rows(rows))
        if len(filtered) >= target or len(raw_rows) < limit:
            return filtered, source
        if len(rows) <= previous_unique_count:
            raise VNextRetrievalCompletenessError(
                "legacy scoped retrieval adapter returned a repeated or non-progressing prefix"
            )
        if limit >= scan_ceiling:
            raise VNextRetrievalCompletenessError(
                "legacy scoped retrieval adapter did not prove exhaustion within "
                f"{scan_ceiling} rows"
            )
        previous_unique_count = len(rows)
        limit = min(limit * 2, scan_ceiling)


def _fetch_scope_filtered(
    fetch: "Callable[[int], tuple[list[JsonObject], StageSourceT]]",
    *,
    scope: _ResolvedRetrievalScope,
    person_linked_memory_ids: frozenset[str],
    target: int,
    store_scope_complete: bool = False,
    source_scope_envelope: bool = False,
) -> tuple[list[JsonObject], StageSourceT]:
    """Fetch a ranked list and apply the resolved scope before selection."""
    if not scope.active:
        rows, source = fetch(target)
        return _dedupe_retrieval_rows(rows), source
    return _fetch_filtered_prefix(
        fetch,
        select_rows=lambda rows: _filter_rows_for_scope(
            rows,
            scope,
            person_linked_memory_ids=person_linked_memory_ids,
            source_scope_envelope=source_scope_envelope,
        ),
        target=target,
        predicate_applied_before_limit=store_scope_complete,
    )


_STORE_SCOPE_PARAMETERS = frozenset(
    {
        "scope_thread_id",
        "scope_task_id",
        "scope_people",
        "scope_person_memory_ids",
        "scope_window_start",
        "scope_window_end",
    }
)

_RESOURCE_SCOPE_PARAMETERS = frozenset(
    {
        "scope_projects",
        "scope_people",
        "scope_window_start",
        "scope_window_end",
    }
)


def _supports_store_scope_predicate(method: object) -> bool:
    """Whether a duck-typed search method accepts the complete scope filter."""
    if not callable(method):
        return False
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    names = {parameter.name for parameter in parameters}
    # **kwargs alone is not evidence that an adapter actually applies the
    # predicate; treating an ignore-only shim as complete would reintroduce the
    # top-N correctness cliff. Require the four explicit capability names.
    return _STORE_SCOPE_PARAMETERS <= names


def _supports_resource_scope_predicate(method: object) -> bool:
    """Whether source/open-loop reads apply the complete scope in-store."""
    if not callable(method):
        return False
    try:
        names = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return False
    return _RESOURCE_SCOPE_PARAMETERS <= names


def _supports_explicit_parameters(method: object, names: Sequence[str]) -> bool:
    """Return true only when a duck-typed method declares every capability."""
    if not callable(method):
        return False
    try:
        parameters = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return False
    return set(names) <= parameters


_GRAPH_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _compact_entity(entity: JsonObject, *, include_count: bool = True) -> JsonObject:
    compact = {
        "id": str(entity.get("id")),
        "name": entity.get("name"),
        "entity_type": entity.get("entity_type"),
    }
    if include_count:
        compact["mention_count"] = entity.get("mention_count")
    return compact


def _graph_memory_admissible(
    row: JsonObject,
    *,
    now: datetime,
    domains: list[str],
    sensitivity_allowed: list[str],
    memory_types: tuple[str, ...],
    projects: tuple[str, ...],
    created_by_agent_ids: tuple[str, ...],
    run_id: str | None,
    scope_thread_id: str | None = None,
    scope_task_id: str | None = None,
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
) -> bool:
    """Apply the search stages' row discipline to a hop-sourced memory row.

    ``get_memory`` returns any non-deleted row, but the fts/vector stages
    only ever see searchable-status, unexpired rows that pass the request's
    scope filters. The graph stage must not smuggle in rows the other
    stages would never return, so it re-applies the same rules here.
    """
    if str(row.get("status")) not in MEMORY_SEARCHABLE_STATUSES:
        return False
    valid_to = _parse_timestamp(row.get("valid_to"))
    if valid_to is not None and valid_to < now:
        return False
    if _allowed(row, domains=domains, sensitivity_allowed=sensitivity_allowed) is not None:
        return False
    if memory_types and row.get("memory_type") not in memory_types:
        return False
    if projects:
        if not _project_scope_meets(
            _row_project_scope_values(row),
            projects,
            floor=_row_floor(row),
        ):
            return False
    if created_by_agent_ids and row.get("created_by_agent_id") not in created_by_agent_ids:
        return False
    if run_id is not None and row.get("run_id") != run_id:
        return False
    if scope_thread_id is not None and scope_thread_id.casefold() not in _row_scope_values(
        row, ("thread_id",)
    ):
        return False
    if scope_task_id is not None and scope_task_id.casefold() not in _row_scope_values(
        row, ("task_id",)
    ):
        return False
    if scope_people:
        requested_people = {person.strip().casefold() for person in scope_people if person.strip()}
        if (
            str(row.get("id")) not in scope_person_memory_ids
            and not (_row_scope_values(row, _PEOPLE_SCOPE_KEYS) & requested_people)
        ):
            return False
    if scope_window_start is not None or scope_window_end is not None:
        event_time = _row_scope_event_time(row)
        if event_time is None:
            return False
        if scope_window_start is not None and event_time < scope_window_start:
            return False
        if scope_window_end is not None and event_time > scope_window_end:
            return False
    return True


def _stabilize_scored_rows(
    rows: Sequence[JsonObject],
    *,
    score_key: str = "fts_score",
    descending: bool = True,
) -> list[JsonObject]:
    """Reorder equal-score runs of a scored stage list content-stably.

    The stores' FTS stages order by ``fts_score DESC`` and then write-clock
    columns with the row id as the last key, so rows with EQUAL scores
    (identical term statistics — restated facts, near-duplicate turns) can
    arrive in ingest-dependent order: their relative rank re-rolls per
    ingest even though the content is identical. The Postgres vector stage
    is worse — ``ORDER BY embedding_vector <=> query`` alone, so equal
    distances get undefined database order within a single store. Distinct
    scores keep the store's order exactly (``descending`` says which way the
    stage ranks); equal scores fall through the content-stable cascade with
    the id as the final total-order key. Lists where any row lacks a
    numeric score are returned unchanged — an unknown store contract keeps
    its own order.
    """
    if not all(isinstance(row.get(score_key), (int, float)) for row in rows):
        return list(rows)
    sign = -1.0 if descending else 1.0
    return sorted(
        rows,
        key=lambda row: (
            sign * float(row[score_key]),  # type: ignore[arg-type]
            *content_stable_tiebreak(row),
            str(row.get("id")),
        ),
    )


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[JsonObject]],
    *,
    k: int = RRF_K,
) -> list[tuple[JsonObject, float, dict[str, int]]]:
    """Fuse per-stage ranked result lists with Reciprocal Rank Fusion.

    Each item scores ``sum(1 / (k + rank))`` over the stages it appears in.
    Returns ``(item, rrf_score, stage_ranks)`` tuples ordered by descending
    score; equal scores fall through the content-stable cascade
    (``content_stable_tiebreak``) with the id as the final total-order key.
    """
    if k < 1:
        raise VNextRetrievalValidationError("reciprocal rank fusion k must be at least 1")
    items: dict[str, JsonObject] = {}
    scores: dict[str, float] = {}
    stage_ranks: dict[str, dict[str, int]] = {}
    for stage, rows in ranked_lists.items():
        for rank, row in enumerate(rows, start=1):
            item_id = str(row.get("id"))
            if item_id not in items:
                items[item_id] = row
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
            stage_ranks.setdefault(item_id, {})[stage] = rank
    ordered_ids = sorted(
        items,
        key=lambda item_id: (-scores[item_id], *content_stable_tiebreak(items[item_id]), item_id),
    )
    return [(items[item_id], scores[item_id], stage_ranks[item_id]) for item_id in ordered_ids]


def _fused_candidates(
    ranked_lists: Mapping[str, Sequence[JsonObject]],
    *,
    target_type: str,
    domains: list[str],
    sensitivity_allowed: list[str],
    limit: int,
) -> list[RetrievalCandidate]:
    candidates: list[RetrievalCandidate] = []
    selected_count = 0
    for rank, (item, rrf_score, stage_ranks) in enumerate(reciprocal_rank_fusion(ranked_lists), start=1):
        exclusion_reason = _allowed(item, domains=domains, sensitivity_allowed=sensitivity_allowed)
        selected = exclusion_reason is None and selected_count < limit
        if selected:
            selected_count += 1
        elif exclusion_reason is None:
            exclusion_reason = "trimmed_by_limit"
        candidates.append(
            RetrievalCandidate(
                item=item,
                target_type=target_type,
                rank=rank,
                rrf_score=rrf_score,
                stage_ranks=stage_ranks,
                selected=selected,
                exclusion_reason=exclusion_reason,
            )
        )
    return candidates


def _compact_item(item: JsonObject) -> JsonObject:
    return {key: value for key, value in item.items() if key != "deleted_at"}


# A packed source carries an excerpt of its best-matching chunk, and never the
# whole document.
#
# Before 2026-08-17 the pack emitted the source row as-is. Two consequences, both
# measured on a real 226-document vault import:
#
# 1. ``metadata_json`` holds the entire captured document (``vnext_capture``
#    writes ``raw_text`` there for evidence). ``estimate_item_tokens`` JSON-dumps
#    the item, so one 235KB source was charged ~59k tokens against a 50k ceiling,
#    was rejected, and latched the truncation flag so every later section was
#    dropped too. Sources vanished from the pack entirely above ~30k characters.
#
# 2. Even under the ceiling, the MCP layer emitted only id/type/title/date, so an
#    agent received a bibliography entry and never the text. Retrieval ranked the
#    right chunks and then threw them away.
#
# The chunk text is what the agent actually needs, and it is what the LongMemEval
# harness has always rendered for itself by reading ``list_source_chunks``
# directly. That is why the benchmark scored a capability the shipped tool did
# not offer.
SOURCE_EXCERPT_MAX_CHARS = 1_200
# How many chunks of one source the fallback reader considers when the chunk-FTS
# stage ranked no winner for it. Pushed into the store as a LIMIT where the
# backend accepts one, and enforced in the loop either way. The head of a
# document is where its title and summary live, so the cap costs little.
SOURCE_FALLBACK_CHUNK_SCAN_LIMIT = 24
# Least fraction of the budget a word-boundary trim may leave before the blunt
# character cut is preferred instead.
_WORD_TRIM_FLOOR = 0.6
# The mark a cut line carries. One character, appended where text was removed.
_CUT_MARKER = "\u2026"


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"\w+", text.lower()) if len(token) > 2}


# A line that is nothing but list markers and link syntax: "- [[a-b-c]]",
# "1. [text](url)", "![[embed]]", "- [[a]] [[b]]". Stripping those leaves no
# prose behind, so the line is a pointer and must not out-score the sentence it
# points at.
#
# The marker prefix is a group of ALTERNATIVES, not one character class. The
# first version used the class [\s>*+\-]*, which cannot express "1." or a task
# checkbox as a unit, so an ordered "## Related" list scored full token overlap
# and won exactly the way an unordered one used to. Obsidian vaults, which is
# what this branch exists to make readable, write both forms, plus "![[embed]]"
# transclusions and several links on one row.
#
# Alternatives rather than a class also keeps the guard narrow: "- [ ] Ship the
# fix" has a marker and no link, so it is still readable prose and still scores.
_LINK_ONLY_LINE = re.compile(
    r"""^
    (?:\s | > | [*+\-\u2022\u2013\u2014] | \d+[.)] | \[[ xX]\])*
    (?:
        !?\[\[[^\]]*\]\]
      | !?\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)
      | !?\[[^\]]*\]\[[^\]]*\]
      | \[[^\]]*\]:\s*\S+
      | <?https?://\S+>?
    )
    (?:[\s,.;|]+(?:
        !?\[\[[^\]]*\]\]
      | !?\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)
      | !?\[[^\]]*\]\[[^\]]*\]
      | <?https?://\S+>?
    ))*
    [\s,.;!?]*$""",
    re.VERBOSE,
)


# A line carrying no content of its own: a markdown heading, a horizontal
# rule, or a table separator.
_LABEL_ONLY_LINE = re.compile(r"^(?:#{1,6}\s.*|[-*_=]{3,}|\|?[\s:|-]+\|?)$")


def _has_readable_line(text: str) -> bool:
    """Does this chunk contain anything a person could read, or only pointers?

    ``_query_anchored_window`` returns a chunk verbatim when it already fits the
    budget, which skips line scoring entirely. So a SHORT chunk of nothing but
    wikilinks reached the agent intact, as a list of paths, even though every
    one of its lines scores zero. That is the Related-block defect surviving in
    the one case the line scorer never gets to see.
    """

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or _LINK_ONLY_LINE.match(line):
            continue
        # A bare heading or rule is a label for content, not the content. A
        # "## Related" block is exactly a label plus pointers, and counting the
        # label as readable is what let that block through the first time.
        if _LABEL_ONLY_LINE.match(stripped):
            continue
        return True
    return False


def _line_overlap_score(line: str, query_tokens: set[str]) -> int:
    """How well one line answers the query, for excerpt windowing.

    Link-only lines score zero even when they match every query token. A
    wikilink is usually the slug of the sentence it points at, so
    "- [[discipline-is-the-art-of-not-betraying-yourself]]" tokenises to exactly
    the same words as the sentence itself and ties with it. Ties went to the
    earlier chunk, so a "## Related" block beat the quote it linked to and the
    agent received a list of paths instead of the text it asked for.

    Token overlap alone cannot separate those two lines, because at token level
    they are the same. What separates them is that one is readable and the other
    is a pointer.
    """

    if not line.strip() or _LINK_ONLY_LINE.match(line):
        return 0
    return len(query_tokens & _tokens(line))


def _call_with_optional_limit(
    method: Callable[..., object], *args: object, limit: int
) -> Sequence[object]:
    """Call a store method with ``limit=`` only when its signature accepts it.

    Store backends differ: the Postgres reader takes a limit, the SQLite one
    does not, and test fakes take whatever they were written with. Probing
    beats a try/except on TypeError, which would also swallow a real TypeError
    raised inside the method.
    """

    try:
        accepts = "limit" in inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        accepts = False
    rows = method(*args, limit=limit) if accepts else method(*args)
    return rows if isinstance(rows, Sequence) else ()


def _trimmed_to_budget(text: str, max_chars: int) -> str:
    """Cut one over-long line to budget, preferring a word boundary.

    The word-boundary preference has a floor. ``rsplit(" ", 1)`` keeps only what
    precedes the LAST space in the slice, which is fine for spaced prose and
    destructive for anything else: a Japanese or Chinese line has no spaces at
    all, and a line that opens with a short word followed by a long unbroken
    token (a URL, a base64 blob) has its last space near the start. Both
    collapse the excerpt to a few characters. When the tidy cut would throw away
    most of the budget, take the blunt one instead.
    """

    if len(text) <= max_chars:
        return text
    sliced = text[:max_chars]
    tidy = sliced.rsplit(" ", 1)[0]
    if len(tidy) >= max_chars * _WORD_TRIM_FLOOR:
        return tidy + _CUT_MARKER
    return sliced + _CUT_MARKER


def _query_anchored_window(chunk: str, *, query: str, max_chars: int) -> str:
    """Window a chunk around the line with the most query-token overlap.

    Scoring is per LINE and by token-set overlap, not ``term in chunk``. The
    substring form scored a whole "## Related" block full of wikilink paths
    above the line actually containing the sentence, because the query terms all
    appear inside the URLs. Overlap on tokenised lines picks the sentence.
    """

    chunk = chunk.strip()
    if len(chunk) <= max_chars:
        return chunk

    query_tokens = _tokens(query)
    lines = chunk.split("\n")
    best_index, best_score = 0, -1
    for index, line in enumerate(lines):
        score = _line_overlap_score(line, query_tokens)
        if score > best_score:
            best_score, best_index = score, index

    # The anchor line goes in FIRST and is never trimmed away by growth.
    #
    # This used to grow outward, join lo..hi, then cut with window[:max_chars].
    # Growth is bidirectional and truncation was head-anchored, so whenever the
    # anchor sat near the end of the chunk the excerpt kept the padding above it
    # and cut the anchor itself out. Reproduced on a 2,783-character chunk at a
    # 300-character budget: the excerpt was entirely filler and did not contain
    # the line it had been selected for. An excerpt that drops its own reason
    # for existing is worse than no excerpt, because it looks like an answer.
    anchor = lines[best_index]
    if len(anchor) >= max_chars:
        return _trimmed_to_budget(anchor, max_chars)

    lo = hi = best_index
    size = len(anchor)
    while lo > 0 or hi < len(lines) - 1:
        grow_up = lo > 0 and (
            hi == len(lines) - 1 or len(lines[lo - 1]) <= len(lines[hi + 1])
        )
        candidate = lines[lo - 1] if grow_up else lines[hi + 1] if hi < len(lines) - 1 else None
        if candidate is None:
            break
        # Only take the neighbour if it FITS. Stopping short of the budget is
        # correct; overshooting and cutting afterwards is what lost the anchor.
        if size + len(candidate) + 1 > max_chars:
            break
        if grow_up:
            lo -= 1
        else:
            hi += 1
        size += len(candidate) + 1

    return "\n".join(lines[lo : hi + 1]).strip()


def estimate_item_tokens(item: JsonObject) -> int:
    """Estimate the token cost of one pack item (chars/4 heuristic)."""
    try:
        text = json.dumps(json_safe(item), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(item)
    chars = len(text)
    return max(1, (chars + TOKEN_ESTIMATE_CHARS_PER_TOKEN - 1) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


# Text a budget cut may shorten: the excerpt a source carries, and the text a
# memory or open loop carries (a memory row repeats it as canonical_text,
# summary and value.text). Ids, scope, provenance, metadata and titles are
# never cut, so a cut item still names what it is and where it came from.
_CUTTABLE_TEXT_KEYS = ("excerpt", "canonical_text", "summary", "description")


def _cuttable_text_lengths(item: JsonObject) -> list[int]:
    lengths: list[int] = []
    for key in _CUTTABLE_TEXT_KEYS:
        text = item.get(key)
        if isinstance(text, str):
            lengths.append(len(text))
    value = item.get("value")
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        lengths.append(len(value["text"]))
    return lengths


def _cut_text(text: str, *, query: str, max_chars: int) -> str:
    """Shorten text to about ``max_chars`` and mark the cut.

    The cut is the one a source excerpt already gets: a window around the line
    that best answers the query, ending in the same one-character marker that
    ``_trimmed_to_budget`` appends. Text that already fits comes back unchanged.
    """

    if len(text) <= max_chars:
        return text
    window = _query_anchored_window(text, query=query, max_chars=max_chars)
    if window == text.strip() or window.endswith(_CUT_MARKER):
        return window
    return window + _CUT_MARKER


def _item_with_text_cut(item: JsonObject, *, query: str, max_chars: int) -> JsonObject:
    cut = dict(item)
    for key in _CUTTABLE_TEXT_KEYS:
        text = cut.get(key)
        if isinstance(text, str):
            cut[key] = _cut_text(text, query=query, max_chars=max_chars)
    value = cut.get("value")
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        cut["value"] = {**value, "text": _cut_text(value["text"], query=query, max_chars=max_chars)}
    return cut


def _fit_item_to_tokens(item: JsonObject, *, query: str, max_tokens: int) -> JsonObject | None:
    """The longest cut of ``item`` that the search finds within ``max_tokens``, or None.

    None means the item cannot be made to fit: with every cuttable text reduced
    to the bare cut marker, the ids, scope and metadata it carries alone cost
    more than the budget. That irreducible cost is the pack's floor for this
    item. A returned item was priced with ``estimate_item_tokens`` before it was
    returned, so it fits whatever the search assumed about how cost grows.
    """

    best = _item_with_text_cut(item, query=query, max_chars=0)
    if estimate_item_tokens(best) > max_tokens:
        return None
    low, high = 0, max(_cuttable_text_lengths(item), default=0)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _item_with_text_cut(item, query=query, max_chars=middle)
        if estimate_item_tokens(candidate) <= max_tokens:
            best, low = candidate, middle
        else:
            high = middle - 1
    return best


class _PackedSections(NamedTuple):
    """What one pass of the greedy packer admitted, section by section."""

    memories: list[JsonObject]
    open_loops: list[JsonObject]
    sources: list[JsonObject]
    supporting_evidence: list[JsonObject]
    contradicting_evidence: list[JsonObject]
    contradictions_stage: str


def _first_item_that_fits_when_cut(
    offers: Mapping[str, Sequence[JsonObject]],
    *,
    section_order: Sequence[str],
    query: str,
    max_tokens: int,
) -> tuple[str, int, JsonObject] | None:
    """The first offered item, in offer order, that a cut can make fit.

    Returns its section, its index in that section, and the cut item. Sections
    the packer derives rather than ranks (evidence quotes, contradiction
    records) are not in ``offers`` and are never cut.
    """

    for section in section_order:
        for index, offered in enumerate(offers.get(section, ())):
            fitted = _fit_item_to_tokens(offered, query=query, max_tokens=max_tokens)
            if fitted is not None:
                return section, index, fitted
    return None


@dataclass(slots=True)
class _TokenBudget:
    """Greedy token-budget packer state.

    Items are offered section by section in the strategy's section order, and
    in rank order within a section. An item that does not fit is skipped and
    the next one is tried, so one large item early in the order cannot take the
    smaller items behind it down with it. Packed items keep their offer order.
    ``truncated`` is set when any offered item was dropped or cut.
    ``allocation`` records the admitted token estimate per section so agents
    can see where the budget went; the values always sum to ``token_estimate``,
    which never exceeds ``token_budget``.

    Through v0.19.2 the first item that did not fit latched ``truncated`` and
    every later item was dropped too, which left a pack empty whenever a large
    item was ranked first.
    """

    token_budget: int | None
    strategy: str = BUDGET_STRATEGY_BALANCED
    token_estimate: int = 0
    truncated: bool = False
    dropped_item_count: int = 0
    cut_item_count: int = 0
    allocation: dict[str, int] = field(default_factory=dict)

    def open_section(self, section: str) -> None:
        """Register a section so the allocation report has stable keys."""
        self.allocation.setdefault(section, 0)

    def admit(self, item: JsonObject, *, section: str) -> bool:
        self.open_section(section)
        cost = estimate_item_tokens(item)
        if self.token_budget is not None and self.token_estimate + cost > self.token_budget:
            self.truncated = True
            self.dropped_item_count += 1
            return False
        self.token_estimate += cost
        self.allocation[section] += cost
        return True

    def to_record(self) -> JsonObject:
        record: JsonObject = {
            "token_budget": self.token_budget,
            "token_estimate": self.token_estimate,
            "truncated": self.truncated,
            "dropped_item_count": self.dropped_item_count,
            "strategy": self.strategy,
            "allocation": dict(self.allocation),
        }
        if self.cut_item_count:
            # Absent unless an item was cut to fit, so every pack that was not
            # cut keeps the report it had.
            record["cut_item_count"] = self.cut_item_count
        return record


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _staleness_note(memory: JsonObject, *, now: datetime) -> JsonObject | None:
    """Note for memories not confirmed within STALENESS_NOTE_AFTER_DAYS days."""
    last_confirmed = _parse_timestamp(memory.get("last_confirmed_at"))
    if last_confirmed is None:
        return None
    age_days = (now - last_confirmed).days
    if age_days <= STALENESS_NOTE_AFTER_DAYS:
        return None
    return {
        "days_since_last_confirmed": age_days,
        "threshold_days": STALENESS_NOTE_AFTER_DAYS,
        "note": f"last confirmed {age_days} days ago (over the {STALENESS_NOTE_AFTER_DAYS}-day threshold)",
    }


def _last_corrected_at(memory: JsonObject) -> datetime | None:
    """Newest ``corrected_at`` in the agentic in-place correction history.

    The agentic correct flow (vnext_memory_commit) appends
    ``{"corrected_at", "reason", "previous_text"}`` entries under
    ``metadata_json.agentic_memory.corrections``; the row's canonical text
    is already the corrected value, so the timestamp tells a reader when
    the current wording took effect.
    """
    metadata = memory.get("metadata_json")
    if not isinstance(metadata, Mapping):
        return None
    agentic = metadata.get("agentic_memory")
    if not isinstance(agentic, Mapping):
        return None
    corrections = agentic.get("corrections")
    if not isinstance(corrections, Sequence) or isinstance(corrections, (str, bytes)):
        return None
    moments = [
        parsed
        for entry in corrections
        if isinstance(entry, Mapping)
        if (parsed := _parse_timestamp(entry.get("corrected_at"))) is not None
    ]
    return max(moments, default=None)


def _flat_stored_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _memory_is_superseded(memory: Mapping[str, object]) -> bool:
    if str(memory.get("status") or "") == "superseded":
        return True
    pointer = memory.get("superseded_by")
    return pointer is not None and str(pointer).strip() != ""


def _quoted_from_links(
    store: object,
    *,
    memory_id: str,
    source_id: str,
    links_by_memory: Mapping[str, Sequence[object]] | None = None,
) -> list[Mapping[str, object]]:
    if links_by_memory is not None:
        raw_links = links_by_memory.get(memory_id, ())
    else:
        list_links = getattr(store, "list_provenance_links", None)
        if not callable(list_links):
            return []
        raw_links = list_links(target_type="memory", target_id=memory_id) or ()
    chosen: list[Mapping[str, object]] = []
    for link in raw_links:
        if not isinstance(link, Mapping):
            continue
        if str(link.get("evidence_role") or "") != "quoted_from":
            continue
        if str(link.get("source_id") or "") != source_id:
            continue
        chosen.append(link)
    return chosen


def _memory_validity_has_closed(memory: Mapping[str, object], *, now: datetime) -> bool:
    """True when the row's validity window closed before ``now``.

    ``alice_memory_manage`` expire sets ``valid_to`` and leaves the status
    alone, so a row closed that way is still ``active``. Recall and the pack
    both leave such a row out (their searches keep only rows with no
    ``valid_to`` or one that has not passed), so a label that named it would
    point at a fact neither of them returns. The far-future unbounded
    ``valid_to`` stand-in is never in the past.
    """

    valid_to = _parse_timestamp(memory.get("valid_to"))
    return valid_to is not None and valid_to < now


def _memory_was_retired(memory: Mapping[str, object]) -> bool:
    """True when the row is retired, whether or not a replacement was named.

    A row whose status is ``superseded`` with no ``superseded_by`` pointer was
    retired without a replacement: ``alice_memory_manage`` forget and undo both
    leave a row like that. A ``rejected`` or ``archived`` row was withdrawn as
    well. A retired row at the end of a supersession chain is not a fact an
    agent should read now. A row that is deleted never gets here: ``get_memory``
    does not return it, so the walk ends there unresolved.
    """

    return str(memory.get("status") or "") in RETIRED_STATUSES


def _current_memory_id(
    store: object,
    memory: Mapping[str, object],
    *,
    memory_visible: Callable[[Mapping[str, object]], bool],
    now: datetime | None = None,
) -> str:
    """Id of the fact an agent should read now, or "" when it cannot be shown.

    An in-place correction keeps the same id. A supersession chain walks
    ``superseded_by`` to the row that is not itself superseded. Every row the
    walk touches, the starting memory included, must pass ``memory_visible``,
    the same fence the caller's memory reads run under. A pointer id is
    itself sensitive metadata, so a hop that is hidden, unresolved, or past
    the depth cap yields "" rather than the last id seen.

    The row the chain ends on must also still be live. When it was forgotten,
    undone, rejected or archived, or deleted, or its validity window has closed
    (``alice_memory_manage`` expire leaves the status ``active``), there is no
    current fact to name, so the answer is "" and not the id of the last row
    read. The caller still labels the passage as corrected. A chain that loops
    back to a row it has already read names nothing: every row on the loop is
    superseded. ``now`` is the clock the validity window is read against.
    """

    clock = now if now is not None else datetime.now(UTC)
    seen: set[str] = set()
    current: Mapping[str, object] = memory
    for _step in range(8):
        memory_id = str(current.get("id") or "")
        if memory_id == "" or memory_id in seen:
            return ""
        if not memory_visible(current):
            return ""
        seen.add(memory_id)
        if not _memory_is_superseded(current):
            if _memory_was_retired(current) or _memory_validity_has_closed(current, now=clock):
                return ""
            return memory_id
        successor_id = str(current.get("superseded_by") or "").strip()
        if successor_id == "":
            # Retired with no replacement (forgotten or undone), so nothing
            # is current.
            return ""
        getter = getattr(store, "get_memory", None)
        if not callable(getter):
            return ""
        successor = getter(successor_id)
        if not isinstance(successor, Mapping):
            return ""
        current = successor
    # The eight-hop walk ended on a row it has not checked yet. It names that
    # row only when the row is visible and ends the chain with a live fact. A
    # row that is still superseded means the chain is longer than the walk, so
    # the current fact is not known.
    last_id = str(current.get("id") or "")
    if (
        last_id == ""
        or _memory_is_superseded(current)
        or _memory_was_retired(current)
        or _memory_validity_has_closed(current, now=clock)
        or not memory_visible(current)
    ):
        return ""
    return last_id


def _quoted_link_is_stale(
    memory: Mapping[str, object],
    link: Mapping[str, object],
    *,
    captured_at: datetime | None,
) -> bool:
    """True when this quoted_from passage is older than the fact.

    A quote that no longer matches canonical text was stored at capture
    and the memory was corrected afterwards. A matching quote on a
    superseded row counts only when that supersession is after the
    source's captured_at. A source captured after the row was already
    superseded is a new import, not this label.
    """

    quote = _flat_stored_text(link.get("quote"))
    canonical = _flat_stored_text(memory.get("canonical_text"))
    if quote and quote != canonical:
        return True
    if not _memory_is_superseded(memory):
        return False
    if captured_at is None:
        return True
    updated_at = _parse_timestamp(memory.get("updated_at"))
    if updated_at is None:
        return True
    return updated_at > captured_at


def _quote_covers_excerpt(quote: str, excerpt: str) -> bool:
    if excerpt == "":
        return True
    if quote == "":
        return False
    return quote in excerpt or excerpt in quote


def _memory_visibility_predicate(
    *,
    domains: list[str],
    sensitivity_allowed: list[str],
    scope: _ResolvedRetrievalScope | None,
    person_linked_memory_ids: frozenset[str] = frozenset(),
) -> Callable[[Mapping[str, object]], bool]:
    """The fence a memory read runs under, as a yes/no for one stored row."""

    def visible(row: Mapping[str, object]) -> bool:
        if _allowed(dict(row), domains=domains, sensitivity_allowed=sensitivity_allowed) is not None:
            return False
        if scope is not None and scope.active:
            return _row_matches_scope(row, scope, person_linked_memory_ids=person_linked_memory_ids)
        return True

    return visible


# A memory id is a UUID in Postgres and in every id the store makes. The scan
# for ids inside ``metadata_json`` only looks for that shape, because a
# Postgres lookup of any other text as a uuid raises.
_MEMORY_ID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_WHOLE_MEMORY_ID_TEXT = re.compile(r"(?:memory:)?" + _MEMORY_ID_TEXT.pattern)
_MEMORY_ID_WITHHELD = "(id withheld)"
# Metadata the product writes is a few levels deep, and a restore refuses more
# than 256. Recursing that far on text a v0.19.0 store let through would raise
# before it finished, so the scan stops here and drops what is below.
_METADATA_ID_SCAN_MAX_DEPTH = 64
_SCRUB_DROPPED = object()


def _collect_memory_id_texts(value: object, found: set[str], *, depth: int) -> bool:
    """Add every UUID-shaped substring of the strings and keys in ``value``, lowercased.

    Returns True when ``value`` nests deeper than ``_METADATA_ID_SCAN_MAX_DEPTH``,
    so some of it was not read.
    """

    if depth > _METADATA_ID_SCAN_MAX_DEPTH:
        return True
    too_deep = False
    if isinstance(value, str):
        found.update(match.lower() for match in _MEMORY_ID_TEXT.findall(value))
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                found.update(match.lower() for match in _MEMORY_ID_TEXT.findall(key))
            too_deep = _collect_memory_id_texts(nested, found, depth=depth + 1) or too_deep
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for nested in value:
            too_deep = _collect_memory_id_texts(nested, found, depth=depth + 1) or too_deep
    return too_deep


def _scrub_memory_id_text(text: str, hidden: frozenset[str]) -> object:
    """``text`` without a hidden id, or ``_SCRUB_DROPPED`` when the text is only that id."""

    if not any(match.lower() in hidden for match in _MEMORY_ID_TEXT.findall(text)):
        return text
    if _WHOLE_MEMORY_ID_TEXT.fullmatch(text.strip()):
        return _SCRUB_DROPPED
    return _MEMORY_ID_TEXT.sub(
        lambda match: _MEMORY_ID_WITHHELD if match.group(0).lower() in hidden else match.group(0),
        text,
    )


def _scrub_memory_ids(value: object, hidden: frozenset[str], *, depth: int) -> object:
    """A copy of ``value`` with every id in ``hidden`` removed, or ``_SCRUB_DROPPED``.

    A string that is only a hidden id is dropped, so its key or list slot goes
    with it. A hidden id inside a longer string is replaced. Below
    ``_METADATA_ID_SCAN_MAX_DEPTH`` nothing is scanned, so the value is dropped.
    """

    if depth > _METADATA_ID_SCAN_MAX_DEPTH:
        return _SCRUB_DROPPED
    if isinstance(value, str):
        return _scrub_memory_id_text(value, hidden)
    if isinstance(value, Mapping):
        output: dict[object, object] = {}
        for key, nested in value.items():
            new_key = _scrub_memory_id_text(key, hidden) if isinstance(key, str) else key
            if new_key is _SCRUB_DROPPED:
                continue
            new_value = _scrub_memory_ids(nested, hidden, depth=depth + 1)
            if new_value is _SCRUB_DROPPED:
                continue
            output[new_key] = new_value
        return output
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = (_scrub_memory_ids(nested, hidden, depth=depth + 1) for nested in value)
        return [item for item in items if item is not _SCRUB_DROPPED]
    return value


def _drop_pointers_outside_fence(
    holders: Sequence[MutableMapping[str, object]],
    pointer_keys: Sequence[str],
    *,
    targets: Mapping[str, Mapping[str, object]],
    memory_visible: Callable[[Mapping[str, object]], bool],
    fail_closed_when_unresolved: bool,
) -> list[tuple[MutableMapping[str, object], str]]:
    """Remove each memory-id pointer whose target the caller may not read.

    A pointer id is itself sensitive metadata, so this is the one place that
    decides whether a pointer may be shown. ``targets`` holds the rows the
    pointers resolved to. A pointer to a row that is in ``targets`` goes when
    ``memory_visible`` says no. A pointer to a row that is not in ``targets``
    names a row this user does not have, which is not a hidden one, because
    the lookup applies no fence: it is kept unless
    ``fail_closed_when_unresolved`` is set, which a scoped read sets.

    Returns each ``(holder, pointer_key)`` it removed, so a caller that derives
    something from the pointer, such as a validity flag, can keep the part that
    is not an id.
    """

    dropped: list[tuple[MutableMapping[str, object], str]] = []
    for holder in holders:
        for pointer_key in pointer_keys:
            pointer = holder.get(pointer_key)
            if not pointer:
                continue
            target = targets.get(str(pointer))
            if target is None:
                if fail_closed_when_unresolved:
                    holder.pop(pointer_key, None)
                    dropped.append((holder, pointer_key))
                continue
            if not memory_visible(target):
                holder.pop(pointer_key, None)
                dropped.append((holder, pointer_key))
    return dropped


# Memories asked for per packed source when labelling a corrected excerpt. The
# label depends on which rows come back, so this stays at the 50 that v0.19.0
# asked for, not the store's 500 default.
_CORRECTION_LABEL_MEMORIES_PER_SOURCE = 50

# Memory ids per provenance-link lookup. One statement per packed request is
# the aim, but a SQLite build from before 3.32 allows only 999 bound
# variables, so a very large pack splits its ids rather than fails.
_CORRECTION_LABEL_LINK_BATCH = 500


def _memories_referencing_sources(
    store: object,
    source_ids: Sequence[str],
    *,
    limit_per_source: int,
) -> dict[str, list[Mapping[str, object]]]:
    """Memories that reference each source: one batched lookup when the store has one.

    The batched method is what keeps a pack of N sources from asking the store
    N times. A store without it (a minimal test double, an older store) gets
    the one-source method once per source, which returns the same rows.
    """

    batched = getattr(store, "list_memories_referencing_sources", None)
    if callable(batched):
        grouped = batched(source_ids, limit_per_source=limit_per_source) or {}
        return {
            source_id: [row for row in grouped.get(source_id, ()) if isinstance(row, Mapping)]
            for source_id in source_ids
        }
    single = getattr(store, "list_memories_referencing_source", None)
    if not callable(single):
        return {}
    return {
        source_id: [
            row
            for row in (single(source_id=source_id, limit=limit_per_source) or ())
            if isinstance(row, Mapping)
        ]
        for source_id in source_ids
    }


def _provenance_links_by_memory(
    store: object,
    memory_ids: Sequence[str],
) -> dict[str, list[object]] | None:
    """Links of every memory in ``memory_ids``, keyed by memory id, or None.

    None means the store has no bulk link lookup, so callers read one memory at
    a time. An empty dict means the lookup ran and found nothing.
    """

    list_bulk = getattr(store, "list_provenance_links_for_targets", None)
    if not callable(list_bulk):
        return None
    links_by_memory: dict[str, list[object]] = {}
    for offset in range(0, len(memory_ids), _CORRECTION_LABEL_LINK_BATCH):
        batch = list(memory_ids[offset : offset + _CORRECTION_LABEL_LINK_BATCH])
        for link in list_bulk(target_type="memory", target_ids=batch) or ():
            if not isinstance(link, Mapping):
                continue
            target_id = str(link.get("target_id") or "")
            links_by_memory.setdefault(target_id, []).append(link)
    return links_by_memory


def annotate_derived_memory_corrections(
    store: object,
    sources: Sequence[MutableMapping[str, object]],
    *,
    memory_visible: Callable[[Mapping[str, object]], bool],
) -> None:
    """Label every packed excerpt whose derived memory was corrected or superseded.

    The batch form of ``annotate_derived_memory_correction``, which is its
    one-source case. It reads what the labels need from the store once for the
    whole pack: one lookup of the memories that reference any of the sources,
    and one lookup of their provenance links. v0.19.0 made both calls once per
    packed source, and each memory lookup scans the memories table.

    The labels are the ones the one-source function sets. ``memory_visible`` is
    required and has no default: it is the fence the caller's memory reads run
    under, applied to every memory the walk to a current fact touches. The
    stale flag stays set whatever the caller may read; only the pointer id is
    withheld.
    """

    sources_by_id: dict[str, list[MutableMapping[str, object]]] = {}
    for source in sources:
        source_id = str(source.get("id") or "")
        if source_id:
            sources_by_id.setdefault(source_id, []).append(source)
    if not sources_by_id:
        return
    memories_by_source = _memories_referencing_sources(
        store,
        list(sources_by_id),
        limit_per_source=_CORRECTION_LABEL_MEMORIES_PER_SOURCE,
    )
    memory_ids_by_source = {
        source_id: [str(memory.get("id") or "") for memory in rows if str(memory.get("id") or "")]
        for source_id, rows in memories_by_source.items()
    }
    all_memory_ids = list(dict.fromkeys(memory_id for ids in memory_ids_by_source.values() for memory_id in ids))
    bulk_links = _provenance_links_by_memory(store, all_memory_ids) if all_memory_ids else None
    for source_id, group in sources_by_id.items():
        memory_rows = memories_by_source.get(source_id) or []
        if not memory_rows:
            continue
        links_by_memory: dict[str, list[object]] = {}
        if bulk_links is not None:
            links_by_memory = {
                memory_id: bulk_links[memory_id]
                for memory_id in memory_ids_by_source[source_id]
                if memory_id in bulk_links
            }
        for source in group:
            _annotate_corrected_source(
                store,
                source,
                memory_rows,
                links_by_memory=links_by_memory,
                memory_visible=memory_visible,
            )


def annotate_derived_memory_correction(
    store: object,
    source: MutableMapping[str, object],
    *,
    memory_visible: Callable[[Mapping[str, object]], bool],
) -> None:
    """Label a packed excerpt whose derived memory was corrected or superseded.

    Sets ``derived_memory_corrected`` when a ``quoted_from`` link on this
    source points at a memory that was corrected or superseded after the
    capture, and the stored quote is the passage in the excerpt. The flag
    stays set whatever the caller may read, because an agent must not quote
    a stale passage as current. ``current_memory_id`` is set only when every
    memory on the walk to the current fact passes ``memory_visible``, the
    fence the caller's memory reads run under. A pointer id is itself
    sensitive metadata, so there is no fallback to an earlier visible hop.
    Ordinary sources gain no keys.

    A pack of several sources calls ``annotate_derived_memory_corrections``
    instead, so the store is asked once for all of them.
    """

    annotate_derived_memory_corrections(store, [source], memory_visible=memory_visible)


def _annotate_corrected_source(
    store: object,
    source: MutableMapping[str, object],
    memory_rows: Sequence[Mapping[str, object]],
    *,
    links_by_memory: Mapping[str, Sequence[object]],
    memory_visible: Callable[[Mapping[str, object]], bool],
) -> None:
    source_id = str(source.get("id") or "")
    captured_at = _parse_timestamp(source.get("captured_at"))
    excerpt = _flat_stored_text(source.get("excerpt"))
    chosen_id: str | None = None
    chosen_in_excerpt = False
    corrected = False
    for memory in memory_rows:
        memory_id = str(memory.get("id") or "")
        if memory_id == "":
            continue
        links = _quoted_from_links(
            store,
            memory_id=memory_id,
            source_id=source_id,
            links_by_memory=links_by_memory if links_by_memory else None,
        )
        for link in links:
            if not _quoted_link_is_stale(memory, link, captured_at=captured_at):
                continue
            quote = _flat_stored_text(link.get("quote"))
            in_excerpt = _quote_covers_excerpt(quote, excerpt)
            if excerpt and not in_excerpt:
                continue
            corrected = True
            current_id = _current_memory_id(store, memory, memory_visible=memory_visible)
            if current_id == "":
                continue
            if chosen_id is None or (in_excerpt and not chosen_in_excerpt):
                chosen_id = current_id
                chosen_in_excerpt = in_excerpt
    if not corrected:
        return
    source["derived_memory_corrected"] = True
    if chosen_id is not None:
        source["current_memory_id"] = chosen_id


def _validity_annotation(
    memory: JsonObject,
    *,
    superseded_by_hint: str | None = None,
    superseded_pointer_withheld: bool = False,
) -> JsonObject | None:
    """Compact validity summary for rows carrying temporal/supersession signal.

    Derived purely from values the row already carries -- the
    ``valid_from``/``valid_to`` window columns, the supersession pointer
    columns from migration 20260704_0077 (plus the row status those flows
    set), and the in-place correction history read by
    ``_last_corrected_at`` -- so annotating costs no extra store queries.
    ``superseded_by_hint`` is a pack-local back-pointer: when a pack-mate's
    ``supersedes`` names this row, the row is annotated as superseded even
    if it never received the ``superseded_by`` column (one-sided patches).
    ``superseded_pointer_withheld`` says the row's ``superseded_by`` pointer was
    removed from this copy of the row because it names a memory the caller may
    not read. The row is still superseded, so ``superseded`` stays true, and
    with no id to show it carries no ``superseded_by_memory_id``.
    Rows without any signal return ``None`` so plain memories keep their
    exact shape, and the far-future unbounded ``valid_to`` sentinel (see
    ``VALID_TO_UNBOUNDED_YEAR``) is treated as no signal.
    """
    validity: JsonObject = {}
    valid_from = _parse_timestamp(memory.get("valid_from"))
    if valid_from is not None:
        validity["valid_from"] = valid_from.isoformat()
    valid_to = _parse_timestamp(memory.get("valid_to"))
    if valid_to is not None and valid_to.year < VALID_TO_UNBOUNDED_YEAR:
        validity["valid_to"] = valid_to.isoformat()
    superseded_by = memory.get("superseded_by") or superseded_by_hint
    if superseded_by or superseded_pointer_withheld or str(memory.get("status")) == "superseded":
        validity["superseded"] = True
    if superseded_by:
        validity["superseded_by_memory_id"] = str(superseded_by)
    if memory.get("supersedes"):
        validity["supersedes_memory_id"] = str(memory.get("supersedes"))
    corrected_at = _last_corrected_at(memory)
    if corrected_at is not None:
        validity["corrected_at"] = corrected_at.isoformat()
    return validity or None


def _memory_recency(memory: JsonObject) -> datetime:
    """Best-effort recency timestamp for recent_first ordering."""
    for key in ("updated_at", "last_seen_at", "created_at", "first_seen_at"):
        parsed = _parse_timestamp(memory.get(key))
        if parsed is not None:
            return parsed
    return _GRAPH_EPOCH


def _order_memories_for_strategy(memories: list[JsonObject], strategy: str) -> list[JsonObject]:
    """Reorder the ranking-selected memories list for the budget strategy.

    recent_first: recency DESC before fused rank (stable sort keeps fused
    order on recency ties). facts_first: memory_types in
    FACTS_FIRST_MEMORY_TYPES boosted to the front, fused order preserved
    inside each partition. Every other strategy keeps the fused order.
    """
    if strategy == BUDGET_STRATEGY_RECENT_FIRST:
        return sorted(memories, key=_memory_recency, reverse=True)
    if strategy == BUDGET_STRATEGY_FACTS_FIRST:
        facts = [item for item in memories if item.get("memory_type") in FACTS_FIRST_MEMORY_TYPES]
        rest = [item for item in memories if item.get("memory_type") not in FACTS_FIRST_MEMORY_TYPES]
        return [*facts, *rest]
    return list(memories)


def _prefer_current_versions(memories: list[JsonObject]) -> tuple[list[JsonObject], int]:
    """Demote-not-drop: move a replacement directly above its superseded ancestor.

    The store search stages already exclude retired rows (status outside
    active/accepted, closed validity windows), so both sides of a
    supersession pair can only co-occur in a pack when the pointer state is
    one-sided -- e.g. an ``update_memory`` patch set ``superseded_by``
    without retiring the row, or only the replacement's ``supersedes``
    pointer exists. Fused (RRF) order is preserved for every other item;
    only the offending (ancestor, replacement) pair is reordered,
    replacement first, and each replacement moves at most once so corrupt
    pointer cycles terminate. Returns the reordered list plus the move
    count reported in the pack trace as ``supersession_reorders``.
    """
    if len(memories) < 2:
        return list(memories), 0
    ids_in_list = {str(memory.get("id")) for memory in memories}
    # ancestor id -> replacement id, from both pointer directions; a row's
    # own superseded_by pointer wins over a pack-mate's supersedes claim.
    successor_of: dict[str, str] = {}
    for memory in memories:
        memory_id = str(memory.get("id"))
        supersedes = memory.get("supersedes")
        if supersedes and str(supersedes) in ids_in_list and str(supersedes) != memory_id:
            successor_of.setdefault(str(supersedes), memory_id)
    for memory in memories:
        memory_id = str(memory.get("id"))
        superseded_by = memory.get("superseded_by")
        if superseded_by and str(superseded_by) in ids_in_list and str(superseded_by) != memory_id:
            successor_of[memory_id] = str(superseded_by)
    if not successor_of:
        return list(memories), 0
    items = list(memories)
    moved: set[str] = set()
    reorders = 0
    index = 0
    while index < len(items):
        ancestor_id = str(items[index].get("id"))
        successor_id = successor_of.get(ancestor_id)
        if successor_id is None or successor_id in moved:
            index += 1
            continue
        successor_index = next(
            (position for position, item in enumerate(items) if str(item.get("id")) == successor_id),
            None,
        )
        if successor_index is None or successor_index <= index:
            index += 1
            continue
        items.insert(index, items.pop(successor_index))
        moved.add(successor_id)
        reorders += 1
        # Stay on this index: the replacement now sits here and may itself
        # be a superseded ancestor of a later pack-mate (chains reorder
        # newest-first in one pass).
    return items, reorders


def _first_fts_named_source_id(
    store: object,
    *,
    fts_memories: Sequence[JsonObject],
    source_excerpts: Sequence[JsonObject],
) -> str | None:
    """First FTS hit that names a source: memory provenance, else excerpt id."""

    list_links = getattr(store, "list_provenance_links", None)
    if callable(list_links):
        for memory in fts_memories:
            memory_id = memory.get("id")
            if memory_id is None or str(memory_id) == "":
                continue
            for link in list_links(target_type="memory", target_id=str(memory_id)):
                source_id = link.get("source_id") if isinstance(link, Mapping) else None
                if source_id is not None and str(source_id) != "":
                    return str(source_id)
    for excerpt in source_excerpts:
        source_id = excerpt.get("id") or excerpt.get("source_id")
        if source_id is not None and str(source_id) != "":
            return str(source_id)
    return None


def expand_provenance_once(
    store: object,
    *,
    fts_memories: Sequence[JsonObject],
    source_excerpts: Sequence[JsonObject],
    already_selected_ids: set[str],
    effective_domains: list[str],
    effective_sensitivity_allowed: list[str],
    effective_project_scope: tuple[str, ...],
    memory_types: tuple[str, ...] = (),
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    scope_thread_id: str | None = None,
    scope_task_id: str | None = None,
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
) -> list[JsonObject]:
    """One source-to-fact hop after the first FTS hit that names a source.

    Calls ``list_memories_referencing_source`` for that one source, keeps
    only ``active`` / ``accepted`` rows that pass the same hop-row filter
    ``_memory_graph_rows`` applies, and admits extras until
    ``PROVENANCE_EXPAND_ONCE_MAX_TOKENS``. Does not walk the siblings'
    sources. Candidates stay out.
    """

    list_refs = getattr(store, "list_memories_referencing_source", None)
    if not callable(list_refs):
        return []
    source_id = _first_fts_named_source_id(
        store,
        fts_memories=fts_memories,
        source_excerpts=source_excerpts,
    )
    if source_id is None:
        return []
    token_cap = PROVENANCE_EXPAND_ONCE_MAX_TOKENS
    now = datetime.now(UTC)
    selected = set(already_selected_ids)
    admitted: list[JsonObject] = []
    used_tokens = 0
    candidates: list[JsonObject] = []
    for row in list_refs(source_id=source_id):
        if not isinstance(row, Mapping):
            continue
        row_id = str(row.get("id") or "")
        if not row_id or row_id in selected:
            continue
        if not _graph_memory_admissible(
            dict(row),
            now=now,
            domains=effective_domains,
            sensitivity_allowed=effective_sensitivity_allowed,
            memory_types=memory_types,
            projects=effective_project_scope,
            created_by_agent_ids=created_by_agent_ids,
            run_id=run_id,
            scope_thread_id=scope_thread_id,
            scope_task_id=scope_task_id,
            scope_people=scope_people,
            scope_person_memory_ids=scope_person_memory_ids,
            scope_window_start=scope_window_start,
            scope_window_end=scope_window_end,
        ):
            continue
        candidates.append(dict(row))
    for item in admit_loaded(
        store,
        kind="memory",
        rows=candidates,
        domains=effective_domains,
        sensitivity_allowed=effective_sensitivity_allowed,
        projects=effective_project_scope,
    ):
        row_id = str(item.get("id") or "")
        cost = estimate_item_tokens(item)
        if used_tokens + cost > token_cap:
            break
        used_tokens += cost
        selected.add(row_id)
        admitted.append(item)
    return admitted


def _memory_title(memory: JsonObject) -> str:
    for key in ("title", "canonical_text", "summary", "memory_key"):
        value = memory.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:120]
    return str(memory.get("id"))


def _with_write_provenance(memory: JsonObject) -> JsonObject:
    """Surface who wrote an ungated memory, on the row the reader sees.

    Absent for every memory that went through review, so a deployment that
    has never auto-promoted emits byte-identical context packs.
    """

    provenance = memory_write_provenance(memory)
    if provenance is None:
        return memory
    return {**memory, "write_provenance": provenance}


def _memory_reference(memory: JsonObject) -> JsonObject:
    reference: JsonObject = {
        "id": str(memory.get("id")),
        "title": _memory_title(memory),
        "memory_type": memory.get("memory_type"),
    }
    # The compact projection is the view a downstream agent is most likely to
    # consume, so a row that was written without a human gate has to be
    # recognisable here too. Absent for every reviewed row, which keeps an
    # unconfigured deployment's packs identical.
    provenance = memory.get("write_provenance") or memory_write_provenance(memory)
    if provenance is not None:
        reference["write_provenance"] = provenance
    return reference


def _source_event_time(source: JsonObject) -> datetime | None:
    """Best-effort event timestamp of a source row, or None.

    Precedence: ``source_created_at`` (the source's own event time, when
    the connector recorded one), then the first parseable
    ``SOURCE_EVENT_METADATA_KEYS`` value in ``metadata_json`` (connectors
    that only stamp dates into metadata, e.g. imported chat sessions),
    then ``captured_at`` (ingest write time — the least honest signal,
    kept last so imported historical sources are not dated "today").
    """
    event = parse_event_datetime(source.get("source_created_at"))
    if event is not None:
        return event
    metadata = source.get("metadata_json")
    if isinstance(metadata, Mapping):
        for key in SOURCE_EVENT_METADATA_KEYS:
            event = parse_event_datetime(metadata.get(key))
            if event is not None:
                return event
    return parse_event_datetime(source.get("captured_at"))


def _optional_search_filters(
    memory_types: tuple[str, ...],
    projects: tuple[str, ...],
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
) -> dict[str, object]:
    """Only forward filter kwargs when set, so minimal stores keep working."""
    filters: dict[str, object] = {}
    if memory_types:
        filters["memory_types"] = tuple(memory_types)
    if projects:
        filters["projects"] = tuple(projects)
    if created_by_agent_ids:
        filters["created_by_agent_ids"] = tuple(created_by_agent_ids)
    if run_id is not None:
        filters["run_id"] = run_id
    return filters


def _apply_budget_exclusions(
    candidates: list[RetrievalCandidate],
    kept_items: list[JsonObject],
) -> list[RetrievalCandidate]:
    """Deselect ranking-selected candidates the token budget dropped."""
    kept_ids = {str(item.get("id")) for item in kept_items}
    updated: list[RetrievalCandidate] = []
    for candidate in candidates:
        if candidate.selected and str(candidate.item.get("id")) not in kept_ids:
            updated.append(replace(candidate, selected=False, exclusion_reason=EXCLUSION_REASON_TOKEN_BUDGET))
        else:
            updated.append(candidate)
    return updated


class VNextRetrievalService:
    def __init__(
        self,
        store: VNextRetrievalStore,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        reranker_provider: RerankProvider | None = None,
    ) -> None:
        self.store = store
        self._winning_chunk_text: dict[str, str] = {}
        self.embedding_provider = embedding_provider if embedding_provider is not None else get_embedding_provider()
        # ---- reranker (disclosed precision stage) begin -------------------
        # None (no env config, no injected provider) keeps the rerank stage
        # dormant: the marked block in compile_context_pack never runs.
        self.reranker_provider = (
            reranker_provider if reranker_provider is not None else get_reranker_provider()
        )
        # ---- reranker (disclosed precision stage) end ---------------------

    def _memory_entity_edges(self, entity_ids: Sequence[str]) -> list[JsonObject]:
        """Resolve active memory/entity edges in one read when supported."""
        normalized_ids = tuple(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
        if not normalized_ids:
            return []
        bulk = getattr(self.store, "list_memory_entity_edges", None)
        if callable(bulk):
            return list(bulk(entity_ids=normalized_ids, edge_types=tuple(MEMORY_ENTITY_EDGE_TYPES)))
        list_edges = getattr(self.store, "list_edges", None)
        if not callable(list_edges):
            return []
        edges: list[JsonObject] = []
        for entity_id in normalized_ids:
            edges.extend(list_edges(to_id=entity_id))
            edges.extend(list_edges(from_id=entity_id))
        return edges

    def _memories_by_ids(
        self,
        memory_ids: Sequence[str],
        *,
        domains: Sequence[str] | None = None,
        sensitivity_allowed: Sequence[str] | None = None,
        projects: Sequence[str] | None = None,
    ) -> dict[str, JsonObject]:
        normalized_ids = tuple(dict.fromkeys(str(memory_id) for memory_id in memory_ids if memory_id))
        if not normalized_ids:
            return {}
        bulk = getattr(self.store, "get_memories_by_ids", None)
        if callable(bulk):
            rows = [
                row
                for start in range(0, len(normalized_ids), MEMORY_ID_LOOKUP_BATCH_SIZE)
                for row in bulk(normalized_ids[start : start + MEMORY_ID_LOOKUP_BATCH_SIZE])
            ]
        else:
            get_memory = getattr(self.store, "get_memory", None)
            rows = (
                [row for memory_id in normalized_ids if (row := get_memory(memory_id)) is not None]
                if callable(get_memory)
                else []
            )
        rows = admit_loaded(
            self.store,
            kind="memory",
            rows=rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        return {str(row.get("id")): row for row in rows}

    def _sources_by_ids(self, source_ids: Sequence[str]) -> dict[str, JsonObject]:
        normalized_ids = tuple(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        if not normalized_ids:
            return {}
        bulk = getattr(self.store, "get_sources_by_ids", None)
        if callable(bulk):
            rows = bulk(normalized_ids)
        else:
            get_source = getattr(self.store, "get_source", None)
            rows = (
                [row for source_id in normalized_ids if (row := get_source(source_id)) is not None]
                if callable(get_source)
                else []
            )
        return {str(row.get("id")): row for row in rows}

    def _provenance_by_target(
        self,
        *,
        target_type: str,
        target_ids: Sequence[str],
    ) -> dict[str, list[JsonObject]]:
        normalized_ids = tuple(dict.fromkeys(str(target_id) for target_id in target_ids if target_id))
        grouped: dict[str, list[JsonObject]] = {target_id: [] for target_id in normalized_ids}
        if not normalized_ids:
            return grouped
        bulk = getattr(self.store, "list_provenance_links_for_targets", None)
        if callable(bulk):
            rows = bulk(target_type=target_type, target_ids=normalized_ids)
            for row in rows:
                grouped.setdefault(str(row.get("target_id")), []).append(row)
            return grouped
        for target_id in normalized_ids:
            grouped[target_id] = list(
                self.store.list_provenance_links(target_type=target_type, target_id=target_id)
            )
        return grouped

    def _person_linked_memory_ids(self, people: frozenset[str]) -> frozenset[str]:
        """Resolve explicit people scope through the entity graph once.

        Structured ``person``/``people`` metadata remains a supported fast
        path, but first-class entity links are the canonical relationship for
        ordinary memories. Stores without that optional substrate degrade to
        metadata-only filtering; they never broaden the requested scope.
        """
        if not people:
            return frozenset()
        find_entities_by_names = getattr(self.store, "find_entities_by_names", None)
        if not callable(find_entities_by_names):
            return frozenset()
        normalized_people = tuple(sorted(normalize_entity_name(person) for person in people))
        entities = list(find_entities_by_names(normalized_people))[:MAX_CONTEXT_SCOPE_VALUES]
        memory_ids: set[str] = set()
        entity_ids = {str(entity.get("id")) for entity in entities}
        for edge in self._memory_entity_edges(tuple(entity_ids)):
            if edge.get("edge_type") not in MEMORY_ENTITY_EDGE_TYPES:
                continue
            if (
                str(edge.get("from_type")) == "memory"
                and str(edge.get("to_type")) == "entity"
                and str(edge.get("to_id")) in entity_ids
            ):
                memory_ids.add(str(edge.get("from_id")))
            elif (
                str(edge.get("from_type")) == "entity"
                and str(edge.get("to_type")) == "memory"
                and str(edge.get("from_id")) in entity_ids
            ):
                memory_ids.add(str(edge.get("to_id")))
        return frozenset(memory_ids)

    def _sanitize_memory_scope_pointers(
        self,
        memories: Sequence[JsonObject],
        *,
        scope: _ResolvedRetrievalScope,
        person_linked_memory_ids: frozenset[str],
        domains: list[str],
        sensitivity_allowed: list[str],
    ) -> frozenset[str]:
        """Remove supersession pointers that would cross the caller's read fence.

        A pointer id is itself sensitive metadata. A pointer goes when its
        target is not readable under the fence the memory stages ran with: the
        domain and sensitivity ceiling, and the project, person and time scope
        when one is active. The ceiling applies with no scope at all, so a
        visible successor does not carry a ``supersedes`` pointer to a
        predecessor the caller's sensitivity ceiling hides.

        A target that does not resolve is a row this user does not have, not a
        hidden one, because the lookup applies no fence. A scoped pack fails
        closed on it. An unscoped pack keeps the pointer, and the high-depth
        ``supersession_context`` shows it as an id-only reference on purpose.

        Only the two pointer columns are cleaned here. The ids copied into a
        row's ``metadata_json`` are cleaned by ``_drop_hidden_memory_ids_from_metadata``.

        Returns the id of every memory whose ``superseded_by`` pointer was
        removed. The caller derives ``validity`` after this runs, and that
        memory is still superseded, so it passes the set on to keep
        ``validity.superseded`` true without the id.
        """
        pointer_ids = [
            str(pointer)
            for memory in memories
            for pointer_key in ("supersedes", "superseded_by")
            if (pointer := memory.get(pointer_key))
        ]
        if not pointer_ids:
            return frozenset()
        dropped = _drop_pointers_outside_fence(
            memories,
            ("supersedes", "superseded_by"),
            targets=self._memories_by_ids(pointer_ids),
            memory_visible=_memory_visibility_predicate(
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                scope=scope,
                person_linked_memory_ids=person_linked_memory_ids,
            ),
            fail_closed_when_unresolved=scope.active,
        )
        return frozenset(
            str(holder["id"]) for holder, pointer_key in dropped if pointer_key == "superseded_by" and holder.get("id")
        )

    def _drop_hidden_memory_ids_from_metadata(
        self,
        memories: Sequence[MutableMapping[str, object]],
        *,
        memory_visible: Callable[[Mapping[str, object]], bool],
    ) -> None:
        """Remove from each row's ``metadata_json`` the id of a memory the caller may not read.

        A memory id is itself sensitive metadata. A stored row can carry the id
        of another memory inside its ``metadata_json``, as the copy of a
        supersession pointer or as a reference one of the write paths left
        there, and the rows of a pack are returned whole in the typed sections
        of ``alice_context_pack`` with ``debug`` set. The pointer columns are
        fenced by ``_sanitize_memory_scope_pointers``. This is the same fence
        for the ids in the JSON.

        Every UUID-shaped string in the metadata, as a value, a list element or
        a key, is looked up once for the whole pack. One that names a memory
        ``memory_visible`` rejects goes. A string that is only that id, with or
        without a ``memory:`` prefix, is removed with its key or list slot.
        An id inside a longer string is replaced by ``(id withheld)``. One that
        names no row at all is left as it is, because it is not a hidden row;
        the same rule the pointer columns follow on an unscoped read.

        ``memory_visible`` is required and has no default, for the same reason
        as on every other reader of the fence: a default would be "no fence"
        for the next caller that forgot it. Metadata nested deeper than
        ``_METADATA_ID_SCAN_MAX_DEPTH`` cannot be scanned, so the part below
        that depth is dropped.
        """

        found_in: list[tuple[MutableMapping[str, object], set[str], bool]] = []
        for memory in memories:
            metadata = memory.get("metadata_json")
            if metadata is None:
                continue
            texts: set[str] = set()
            too_deep = _collect_memory_id_texts(metadata, texts, depth=0)
            if texts or too_deep:
                found_in.append((memory, texts, too_deep))
        if not found_in:
            return
        candidates = sorted(set().union(*(texts for _memory, texts, _too_deep in found_in)))
        targets = self._memories_by_ids(candidates) if candidates else {}
        hidden = frozenset(memory_id.lower() for memory_id, target in targets.items() if not memory_visible(target))
        for memory, texts, too_deep in found_in:
            if texts.isdisjoint(hidden) and not too_deep:
                continue
            scrubbed = _scrub_memory_ids(memory["metadata_json"], hidden, depth=0)
            memory["metadata_json"] = {} if scrubbed is _SCRUB_DROPPED else scrubbed

    def _sanitize_memory_scope_references(
        self,
        memories: Sequence[JsonObject],
        *,
        scope: _ResolvedRetrievalScope,
        person_linked_memory_ids: frozenset[str],
    ) -> None:
        """Remove resource references that cannot be proven inside scope.

        Memory metadata is often copied from capture/provenance rows and is
        rendered verbatim in context packs. Filtering the primary rows and
        evidence sections is therefore insufficient: a scoped memory could
        still expose an out-of-scope source id, chunk id, capture hash, or
        source-derived date through ``metadata_json``. This pass deep-copies
        and validates known reference fields before budgeting or rendering.
        """
        if not scope.active:
            return
        source_cache: dict[str, Mapping[str, object] | None] = {}
        memory_cache: dict[str, Mapping[str, object] | None] = {}

        def _reference_strings(value: object) -> list[str]:
            if isinstance(value, (str, int)):
                normalized = str(value).strip()
                return [normalized] if normalized else []
            if isinstance(value, Mapping):
                refs: list[str] = []
                for key in ("source_id", "id", "ref", "source_ref"):
                    refs.extend(_reference_strings(value.get(key)))
                for key in ("source_ids", "source_refs", "sources"):
                    refs.extend(_reference_strings(value.get(key)))
                return refs
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return [ref for item in value for ref in _reference_strings(item)]
            return []

        all_references = [
            reference
            for memory in memories
            for reference in _reference_strings(memory)
        ]
        memory_reference_ids = [
            reference.removeprefix("memory:")
            for reference in all_references
            if reference.startswith("memory:")
        ]
        source_reference_ids = [
            reference.removeprefix("source:")
            for reference in all_references
            if not reference.startswith("memory:")
        ]
        memory_cache.update(self._memories_by_ids(memory_reference_ids))
        source_cache.update(self._sources_by_ids(source_reference_ids))

        def _reference_allowed(reference: str) -> bool:
            if reference.startswith("memory:"):
                memory_id = reference.removeprefix("memory:")
                target = memory_cache.get(memory_id)
                return target is not None and _row_matches_scope(
                    target,
                    scope,
                    person_linked_memory_ids=person_linked_memory_ids,
                )
            source_id = reference.removeprefix("source:")
            source = source_cache.get(source_id)
            return source is not None and _row_matches_scope(
                source,
                scope,
                source_scope_envelope=True,
            )

        def _value_allowed(value: object) -> bool:
            references = _reference_strings(value)
            return bool(references) and all(_reference_allowed(ref) for ref in references)

        singular_keys = frozenset({"source_id", "source_ref"})
        collection_keys = frozenset(
            {"source_ids", "source_refs", "source_references", "selected_source_ids"}
        )
        source_derived_keys = frozenset(
            {
                "source_created_at",
                "source_chunk_id",
                "source_chunk_index",
                "capture_content_hash",
                *SOURCE_EVENT_METADATA_KEYS,
            }
        )

        def _sanitize_mapping(value: Mapping[str, object]) -> JsonObject:
            output: JsonObject = {}
            invalid_primary_source = False
            for key, nested in value.items():
                if key in singular_keys:
                    if _value_allowed(nested):
                        output[key] = nested
                    elif key == "source_id":
                        invalid_primary_source = True
                    continue
                if key in collection_keys:
                    if isinstance(nested, Sequence) and not isinstance(
                        nested, (str, bytes, bytearray)
                    ):
                        output[key] = [item for item in nested if _value_allowed(item)]
                    elif _value_allowed(nested):
                        output[key] = nested
                    continue
                if isinstance(nested, Mapping):
                    output[key] = _sanitize_mapping(nested)
                elif isinstance(nested, list):
                    output[key] = [
                        _sanitize_mapping(item) if isinstance(item, Mapping) else item
                        for item in nested
                    ]
                else:
                    output[key] = nested
            if invalid_primary_source:
                for key in source_derived_keys:
                    output.pop(key, None)
            return output

        for memory in memories:
            metadata = memory.get("metadata_json")
            metadata_source = metadata.get("source_id") if isinstance(metadata, Mapping) else None
            invalid_metadata_source = bool(metadata_source) and not _value_allowed(metadata_source)
            sanitized = _sanitize_mapping(memory)
            if invalid_metadata_source:
                for key in source_derived_keys:
                    sanitized.pop(key, None)
            memory.clear()
            memory.update(sanitized)

    def _memory_fts_rows(
        self,
        *,
        query: str,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        memory_types: tuple[str, ...] = (),
        projects: tuple[str, ...] = (),
        created_by_agent_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        scope: _ResolvedRetrievalScope | None = None,
        person_linked_memory_ids: frozenset[str] = frozenset(),
        scope_thread_id: str | None = None,
        scope_task_id: str | None = None,
        scope_people: tuple[str, ...] = (),
        scope_person_memory_ids: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> tuple[list[JsonObject], str]:
        filters = _optional_search_filters(memory_types, projects, created_by_agent_ids, run_id)
        scope_filters: dict[str, object] = {}
        search_memories_fts = getattr(self.store, "search_memories_fts", None)
        active_search = search_memories_fts if callable(search_memories_fts) else self.store.search_memories
        effective_people = tuple(sorted(scope.people)) if scope is not None else scope_people
        effective_window_start = scope.window_start if scope is not None else scope_window_start
        effective_window_end = scope.window_end if scope is not None else scope_window_end
        direct_scope_active = bool(
            scope_thread_id
            or scope_task_id
            or effective_people
            or effective_window_start is not None
            or effective_window_end is not None
        )
        if direct_scope_active and _supports_store_scope_predicate(active_search):
            scope_filters = {
                "scope_thread_id": scope_thread_id,
                "scope_task_id": scope_task_id,
                "scope_people": effective_people,
                "scope_person_memory_ids": (
                    scope_person_memory_ids or tuple(sorted(person_linked_memory_ids))
                ),
                "scope_window_start": effective_window_start,
                "scope_window_end": effective_window_end,
            }
        if callable(search_memories_fts):
            rows = search_memories_fts(
                query=query,
                domains=domains or None,
                sensitivity_allowed=sensitivity_allowed,
                limit=limit,
                **filters,
                **scope_filters,
            )
            # Display-only trace label; SQLite stores override it via
            # ``fts_stage_source`` so traces do not claim a Postgres stage.
            fts_source = str(getattr(self.store, "fts_stage_source", "postgres_fts"))
            if not rows and len(fts_fallback_tokens(query)) >= 2:
                # Strict AND semantics found nothing for a multi-word query.
                # With no embeddings configured (the default first-hour
                # setup) FTS is the whole recall path, so a natural-language
                # question would return zero results against memories a
                # keyword query finds instantly. Retry once with OR
                # semantics; the source string keeps the trace honest about
                # the relaxed pass, and fallback rows join RRF fusion
                # exactly like strict FTS rows. Single-token queries skip
                # the retry (OR and AND are identical there), and a strict
                # hit above never reaches this branch.
                try:
                    rows = search_memories_fts(
                        query=query,
                        domains=domains or None,
                        sensitivity_allowed=sensitivity_allowed,
                        limit=limit,
                        match_any=True,
                        **filters,
                        **scope_filters,
                    )
                except TypeError:
                    # Store predates the match_any kwarg; keep the strict
                    # (empty) result rather than guessing.
                    return [], fts_source
                rows = admit_loaded(
                    self.store,
                    kind="memory",
                    rows=rows,
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    projects=projects,
                )
                return _stabilize_scored_rows(rows), f"{fts_source}_or_fallback"
            rows = admit_loaded(
                self.store,
                kind="memory",
                rows=rows,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                projects=projects,
            )
            return _stabilize_scored_rows(rows), fts_source
        legacy_search = cast(
            Callable[..., list[JsonObject]],
            getattr(self.store, "search_memories"),
        )
        rows = legacy_search(
            query=query,
            domains=domains or None,
            sensitivity_allowed=sensitivity_allowed,
            limit=limit,
            **filters,
            **scope_filters,
        )
        rows = admit_loaded(
            self.store,
            kind="memory",
            rows=rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        return list(rows), "store_lexical"

    def _query_embedding(self, query: str) -> tuple[list[float] | None, str]:
        if self.embedding_provider is None:
            return None, VECTOR_STAGE_DISABLED_NO_PROVIDER
        search_memories_vector = getattr(self.store, "search_memories_vector", None)
        if not callable(search_memories_vector):
            return None, VECTOR_STAGE_DISABLED_NO_STORE_SUPPORT
        try:
            return self.embedding_provider.embed_text(query), VECTOR_STAGE_ENABLED
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            logger.warning(
                "Query embedding failed open error_code=query_embedding_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return None, VECTOR_STAGE_DISABLED_QUERY_EMBEDDING_FAILED

    def _memory_vector_rows(
        self,
        *,
        query: str,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        memory_types: tuple[str, ...] = (),
        projects: tuple[str, ...] = (),
        created_by_agent_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        scope: _ResolvedRetrievalScope | None = None,
        person_linked_memory_ids: frozenset[str] = frozenset(),
        query_vector: list[float] | None = None,
        query_embedding_status: str | None = None,
        scope_thread_id: str | None = None,
        scope_task_id: str | None = None,
        scope_people: tuple[str, ...] = (),
        scope_person_memory_ids: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> tuple[list[JsonObject], str]:
        search_memories_vector = getattr(self.store, "search_memories_vector", None)
        if query_embedding_status is None:
            query_vector, query_embedding_status = self._query_embedding(query)
        if query_embedding_status != VECTOR_STAGE_ENABLED or query_vector is None:
            return [], query_embedding_status
        assert self.embedding_provider is not None
        assert callable(search_memories_vector)
        try:
            search_kwargs: dict[str, object] = {
                "query_vector": query_vector,
                "domains": domains or None,
                "sensitivity_allowed": sensitivity_allowed,
                "limit": limit,
                **_optional_search_filters(memory_types, projects, created_by_agent_ids, run_id),
            }
            effective_people = tuple(sorted(scope.people)) if scope is not None else scope_people
            effective_window_start = scope.window_start if scope is not None else scope_window_start
            effective_window_end = scope.window_end if scope is not None else scope_window_end
            direct_scope_active = bool(
                scope_thread_id
                or scope_task_id
                or effective_people
                or effective_window_start is not None
                or effective_window_end is not None
            )
            if direct_scope_active and _supports_store_scope_predicate(search_memories_vector):
                search_kwargs.update(
                    {
                        "scope_thread_id": scope_thread_id,
                        "scope_task_id": scope_task_id,
                        "scope_people": effective_people,
                        "scope_person_memory_ids": (
                            scope_person_memory_ids or tuple(sorted(person_linked_memory_ids))
                        ),
                        "scope_window_start": effective_window_start,
                        "scope_window_end": effective_window_end,
                    }
                )
            try:
                rows = search_memories_vector(
                    **search_kwargs,
                    embedding_provider=self.embedding_provider.provider,
                    embedding_model=self.embedding_provider.model,
                    embedding_endpoint=endpoint_fingerprint(
                        getattr(self.embedding_provider, "base_url", "")
                    ),
                    embedding_signature_version=EMBEDDING_SIGNATURE_VERSION,
                )
            except TypeError as exc:
                # Compatibility for third-party/minimal store adapters. The
                # bundled stores enforce provider/model/version matching.
                if "unexpected keyword argument" not in str(exc):
                    raise
                rows = search_memories_vector(**search_kwargs)
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            logger.warning(
                "Query embedding failed open error_code=query_embedding_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return [], VECTOR_STAGE_DISABLED_QUERY_EMBEDDING_FAILED
        # Ascending stage: smaller distance ranks first. Equal distances
        # (identical texts embed identically) stabilize content-first.
        rows = admit_loaded(
            self.store,
            kind="memory",
            rows=rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        return _stabilize_scored_rows(rows, score_key="vector_distance", descending=False), VECTOR_STAGE_ENABLED

    def _memory_graph_rows(
        self,
        *,
        query: str,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        entity_read_fenced: bool,
        memory_types: tuple[str, ...] = (),
        projects: tuple[str, ...] = (),
        created_by_agent_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        scope_thread_id: str | None = None,
        scope_task_id: str | None = None,
        scope_people: tuple[str, ...] = (),
        scope_person_memory_ids: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> tuple[list[JsonObject], str, list[JsonObject]]:
        """Entity-hop graph stage: ``(rows, stage_status, matched_entities)``.

        Resolution: n-gram candidate names from the query (see
        ``entity_name_candidates``) go through ONE ``find_entities_by_names``
        round-trip; the top ``GRAPH_ENTITY_MATCH_LIMIT`` matches (by
        mention_count, the store's ordering) seed the hop. One hop: edges of
        type mentions/about between those entities and memory nodes, walked
        in both directions. Connected memories are fetched through
        ``get_memory`` and re-filtered with the same status/expiry/scope
        discipline the fts/vector stages honor, then ranked by edge
        observed_at DESC (recency proxy) and memory recency.

        Duck-typed via getattr so legacy/minimal stores without the entity
        substrate degrade to an honest disabled status instead of failing.
        """
        find_entities_by_names = getattr(self.store, "find_entities_by_names", None)
        has_edges = callable(getattr(self.store, "list_memory_entity_edges", None)) or callable(
            getattr(self.store, "list_edges", None)
        )
        has_memories = callable(getattr(self.store, "get_memories_by_ids", None)) or callable(
            getattr(self.store, "get_memory", None)
        )
        if not (callable(find_entities_by_names) and has_edges and has_memories):
            return [], GRAPH_STAGE_DISABLED_NO_STORE_SUPPORT, []
        candidate_names = entity_name_candidates(query)
        entities = list(find_entities_by_names(tuple(candidate_names))) if candidate_names else []
        restricted = entity_read_fenced
        if not restricted:
            entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]
        if not entities:
            return [], GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH, []

        # One hop: newest edge observed_at per connected memory.
        observed_at_by_memory: dict[str, datetime] = {}
        entities_by_memory: dict[str, set[str]] = {}
        entity_ids = {str(entity.get("id")) for entity in entities}
        for edge in self._memory_entity_edges(tuple(entity_ids)):
            if edge.get("edge_type") not in MEMORY_ENTITY_EDGE_TYPES:
                continue
            if (
                str(edge.get("from_type")) == "memory"
                and str(edge.get("to_type")) == "entity"
                and str(edge.get("to_id")) in entity_ids
            ):
                memory_id = str(edge.get("from_id"))
                entity_id = str(edge.get("to_id"))
            elif (
                str(edge.get("from_type")) == "entity"
                and str(edge.get("to_type")) == "memory"
                and str(edge.get("from_id")) in entity_ids
            ):
                memory_id = str(edge.get("to_id"))
                entity_id = str(edge.get("from_id"))
            else:
                continue
            entities_by_memory.setdefault(memory_id, set()).add(entity_id)
            observed_at = _parse_timestamp(edge.get("observed_at")) or _GRAPH_EPOCH
            previous = observed_at_by_memory.get(memory_id)
            if previous is None or observed_at > previous:
                observed_at_by_memory[memory_id] = observed_at

        now = datetime.now(UTC)
        ranked: list[tuple[datetime, datetime, str, JsonObject]] = []
        visible_entity_ids: set[str] = set()
        readable_mentions: dict[str, set[tuple[str, str]]] = {entity_id: set() for entity_id in entity_ids}
        memories_by_id = self._memories_by_ids(
            tuple(observed_at_by_memory),
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        graph_candidates: list[JsonObject] = []
        graph_observed: list[datetime] = []
        for memory_id, observed_at in observed_at_by_memory.items():
            row = memories_by_id.get(memory_id)
            if row is None:
                continue
            if not _graph_memory_admissible(
                row,
                now=now,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                memory_types=memory_types,
                projects=projects,
                created_by_agent_ids=created_by_agent_ids,
                run_id=run_id,
                scope_thread_id=scope_thread_id,
                scope_task_id=scope_task_id,
                scope_people=scope_people,
                scope_person_memory_ids=scope_person_memory_ids,
                scope_window_start=scope_window_start,
                scope_window_end=scope_window_end,
            ):
                continue
            graph_candidates.append(row)
            graph_observed.append(observed_at)
        admitted_graph_ids = {
            str(row.get("id"))
            for row in admit_loaded(
                self.store,
                kind="memory",
                rows=graph_candidates,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                projects=projects,
            )
        }
        for row, observed_at in zip(graph_candidates, graph_observed, strict=True):
            memory_id = str(row.get("id"))
            if memory_id not in admitted_graph_ids:
                continue
            recency = (
                _parse_timestamp(row.get("updated_at"))
                or _parse_timestamp(row.get("created_at"))
                or _GRAPH_EPOCH
            )
            ranked.append((observed_at, recency, memory_id, row))
            visible_entity_ids.update(entities_by_memory[memory_id])
            for entity_id in entities_by_memory[memory_id]:
                readable_mentions[entity_id].add(("memory", memory_id))
        # Deterministic order: edge observed_at DESC, memory recency DESC,
        # then the content-stable cascade with id ASC as the final key (the
        # ascending pre-sort survives the stable reverse timestamp sort).
        ranked.sort(key=lambda entry: (*content_stable_tiebreak(entry[3]), entry[2]))
        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        # A source-only mention is enough, but it must pass this call's labels
        # and scope too. The store returns active edges in both directions.
        list_edges = getattr(self.store, "list_edges", None)
        source_links: dict[str, set[str]] = {}
        if callable(list_edges) and not (memory_types or created_by_agent_ids or run_id or scope_thread_id or scope_task_id):
            for entity_id in entity_ids:
                for edge in [*list_edges(from_id=entity_id), *list_edges(to_id=entity_id)]:
                    if edge.get("edge_type") not in MEMORY_ENTITY_EDGE_TYPES or edge.get("valid_to") is not None:
                        continue
                    if edge.get("from_type") == "source" and edge.get("to_type") == "entity" and str(edge.get("to_id")) == entity_id:
                        source_links.setdefault(str(edge.get("from_id")), set()).add(entity_id)
                    elif edge.get("to_type") == "source" and edge.get("from_type") == "entity" and str(edge.get("from_id")) == entity_id:
                        source_links.setdefault(str(edge.get("to_id")), set()).add(entity_id)
        source_scope = _ResolvedRetrievalScope(
            projects=frozenset(projects), people=frozenset(scope_people),
            window_start=scope_window_start, window_end=scope_window_end,
            exclude_global_domains=frozenset(),
        )
        for source_id, source in self._sources_by_ids(tuple(source_links)).items():
            if (source.get("deleted_at") is None
                    and _allowed(source, domains=domains, sensitivity_allowed=sensitivity_allowed) is None
                    and _row_matches_scope(source, source_scope, source_scope_envelope=True)):
                visible_entity_ids.update(source_links[source_id])
                for entity_id in source_links[source_id]:
                    readable_mentions[entity_id].add(("source", source_id))
        entities = [entity for entity in entities if str(entity.get("id")) in visible_entity_ids]
        if restricted:
            # Count distinct readable linked rows, not stored vault-wide counts
            # or duplicate edges pointing to the same memory or source.
            entities.sort(key=lambda entity: (-len(readable_mentions[str(entity.get("id"))]),
                str(entity.get("name")), str(entity.get("entity_type")), str(entity.get("id"))))
            entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]
            selected_entity_ids = {str(entity.get("id")) for entity in entities}
            ranked = [entry for entry in ranked if entities_by_memory[entry[2]] & selected_entity_ids]
        rows = [entry[3] for entry in ranked[:limit]]
        matched_entities = [_compact_entity(entity, include_count=not restricted) for entity in entities]
        return rows, GRAPH_STAGE_ENABLED if entities else GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH, matched_entities

    def _memory_temporal_rows(
        self,
        *,
        anchor: TemporalAnchor,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        memory_types: tuple[str, ...] = (),
        projects: tuple[str, ...] = (),
        created_by_agent_ids: tuple[str, ...] = (),
        run_id: str | None = None,
    ) -> tuple[list[JsonObject], str]:
        """Temporal-anchor stage: ``(rows, stage_status)``.

        Memories whose event window intersects the parsed anchor window,
        via the store's ``search_memories_by_time`` (proximity-to-center
        order). Duck-typed like the other optional stages: stores without
        the method degrade to an honest disabled status instead of
        failing. The rows join RRF as one more ranked list, so the anchor
        is a ranking signal, never a filter.
        """
        search_memories_by_time = getattr(self.store, "search_memories_by_time", None)
        if not callable(search_memories_by_time):
            return [], TEMPORAL_STAGE_DISABLED_NO_STORE_SUPPORT
        rows = search_memories_by_time(
            window_start=anchor.window_start,
            window_end=anchor.window_end,
            window_center=anchor.window_center,
            domains=domains or None,
            sensitivity_allowed=sensitivity_allowed,
            limit=limit,
            **_optional_search_filters(memory_types, projects, created_by_agent_ids, run_id),
        )
        rows = admit_loaded(
            self.store,
            kind="memory",
            rows=rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        return list(rows), TEMPORAL_STAGE_ENABLED

    def _source_stage_lists(
        self,
        *,
        query: str,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        winning_memories: Sequence[JsonObject],
        ranking: SourceRanking,
        scope: _ResolvedRetrievalScope | None = None,
        anchor: TemporalAnchor | None = None,
    ) -> tuple[dict[str, Sequence[JsonObject]], JsonObject]:
        """Ranked source lists for RRF fusion plus the honest stage record.

        ``ranking`` is REQUIRED and has no default, as ``scope`` is on
        ``search_source_excerpts``: a defaulted ranking is a silent choice for
        the caller that forgets it. Only ``SourceRanking.document()`` has a code
        path today, which is the stage as written below, and any other value is
        refused before a store is touched.

        Up to three lists (see SOURCE_STAGE_* constants):

        - ``chunk_fts``: parent sources of the best chunk-content hits, in
          best-chunk order. When the strict pass finds nothing for a
          multi-token query, retried once with ``match_any=True`` — the
          same one-shot OR-fallback the memory FTS stage uses — and the
          record's ``chunk_fts_source`` label reports the relaxed pass.
        - ``provenance``: sources the winning memories' provenance links
          point at, in fused memory-rank order, so evidence backing a
          retrieved memory surfaces even with zero lexical overlap.
        - ``title_recency``: the legacy ``search_sources`` lexical list
          (title/author/uri/metadata LIKE, recency-ordered).
        - ``temporal_anchor`` (only when ``anchor`` is set): the sources
          the other lists already surfaced whose event date (see
          ``_source_event_time``) falls inside the anchor window, ordered
          by proximity to the window center — a rank boost over existing
          candidates, never a new recall path.

        Lists whose store capability is missing (``search_source_chunks``
        / ``get_source``) are skipped with an honest label instead of
        failing, so minimal stores and test fakes keep working. The stage
        record reports each list's candidate count under its stage key.
        """
        _require_document_ranking(ranking)
        # source id -> the chunk this stage ranked best, for the packed
        # excerpt. Reset per run so a previous query's winner is never reused.
        self._winning_chunk_text = {}
        scope = scope or _ResolvedRetrievalScope(
            projects=frozenset(),
            people=frozenset(),
            window_start=None,
            window_end=None,
            exclude_global_domains=frozenset(),
        )
        has_source_resolver = callable(getattr(self.store, "get_sources_by_ids", None)) or callable(
            getattr(self.store, "get_source", None)
        )

        def _resolve_sources(source_ids: list[str]) -> list[JsonObject]:
            by_id = self._sources_by_ids(source_ids)
            return [
                row
                for source_id in source_ids
                if (row := by_id.get(source_id)) is not None
                and _row_matches_scope(
                    row,
                    scope,
                    source_scope_envelope=True,
                )
            ]

        def _resource_scope_filters(method: object) -> dict[str, object]:
            if not scope.active or not _supports_resource_scope_predicate(method):
                return {}
            return {
                "scope_projects": tuple(sorted(scope.projects)),
                "scope_people": tuple(sorted(scope.people)),
                "scope_window_start": scope.window_start,
                "scope_window_end": scope.window_end,
            }

        ranked_lists: dict[str, Sequence[JsonObject]] = {}

        # (a) Content: sources ranked by their best chunk-FTS hit.
        search_source_chunks = getattr(self.store, "search_source_chunks", None)
        chunk_sources: list[JsonObject] = []
        if callable(search_source_chunks) and has_source_resolver:
            chunk_fts_source = str(getattr(self.store, "fts_stage_source", "postgres_fts"))
            chunk_scope_filters = _resource_scope_filters(search_source_chunks)

            def _chunk_sources_for(rows: Sequence[JsonObject]) -> list[JsonObject]:
                ordered_source_ids: list[str] = []
                seen_source_ids: set[str] = set()
                for row in _stabilize_scored_rows(rows):
                    source_id = row.get("source_id")
                    if source_id is None or str(source_id) in seen_source_ids:
                        continue
                    seen_source_ids.add(str(source_id))
                    ordered_source_ids.append(str(source_id))
                    # Remember the FTS-winning chunk for this source. Rows
                    # arrive best-first, so the first one seen for a source IS
                    # the winner. Re-deriving it later by listing every chunk
                    # and substring-matching promotes link-heavy sections such
                    # as "## Related" over the sentence the user asked for.
                    text = row.get("text")
                    if isinstance(text, str) and text.strip():
                        self._winning_chunk_text.setdefault(str(source_id), text)
                return _resolve_sources(ordered_source_ids)

            def _fetch_chunk_sources(
                *, match_any: bool,
            ) -> list[JsonObject]:
                def _fetch(n: int) -> tuple[list[JsonObject], str]:
                    kwargs: dict[str, object] = {
                        "query": query,
                        "domains": domains or None,
                        "sensitivity_allowed": sensitivity_allowed,
                        "limit": n,
                        **chunk_scope_filters,
                    }
                    if match_any:
                        kwargs["match_any"] = True
                    return list(search_source_chunks(**kwargs)), chunk_fts_source

                # The chunk query ranks chunk rows, while the context pack
                # selects parent sources. A fixed ``limit * N`` prefix lets
                # one long source consume the entire chunk arm. Deepen until
                # enough distinct in-scope parents survive or the store proves
                # exhaustion. Store-side scope predicates still apply before
                # every prefix LIMIT; parent deduplication necessarily happens
                # here and therefore cannot use the one-shot fast path.
                selected, _source = _fetch_filtered_prefix(
                    _fetch,
                    select_rows=_chunk_sources_for,
                    target=limit,
                    initial_limit=limit * SOURCE_CHUNK_CANDIDATE_MULTIPLIER,
                )
                return selected[:limit]

            chunk_sources = _fetch_chunk_sources(match_any=False)
            if not chunk_sources and len(fts_fallback_tokens(query)) >= 2:
                # Same one-shot OR retry as _memory_fts_rows, same honesty
                # rule: the label reports the relaxed pass.
                try:
                    chunk_sources = _fetch_chunk_sources(match_any=True)
                except TypeError:
                    # Store predates the match_any kwarg; keep the strict
                    # (empty) result rather than guessing.
                    chunk_sources = []
                else:
                    chunk_fts_source = f"{chunk_fts_source}_or_fallback"
            ranked_lists[SOURCE_STAGE_CHUNK_FTS] = chunk_sources
        else:
            chunk_fts_source = SOURCE_CHUNK_STAGE_DISABLED_NO_STORE_SUPPORT

        # (b) Provenance of the winning memory hits, in fused rank order.
        provenance_sources: list[JsonObject] = []
        if has_source_resolver:
            provenance_ids: list[str] = []
            seen_provenance: set[str] = set()
            links_by_target = self._provenance_by_target(
                target_type="memory",
                target_ids=[str(memory.get("id")) for memory in winning_memories],
            )
            for memory in winning_memories:
                for link in links_by_target.get(str(memory.get("id")), []):
                    source_id = link.get("source_id")
                    if source_id is None or str(source_id) in seen_provenance:
                        continue
                    seen_provenance.add(str(source_id))
                    provenance_ids.append(str(source_id))
            provenance_sources = _resolve_sources(provenance_ids)[:limit]
            ranked_lists[SOURCE_STAGE_PROVENANCE] = provenance_sources

        # (c) Legacy title/recency lexical list.
        search_sources = cast(Callable[..., list[JsonObject]], self.store.search_sources)
        source_scope_filters = _resource_scope_filters(search_sources)

        def _fetch_sources(n: int) -> tuple[list[JsonObject], str]:
            return list(search_sources(
                query=query,
                domains=domains or None,
                sensitivity_allowed=sensitivity_allowed,
                limit=n,
                **source_scope_filters,
            )), SOURCE_STAGE_TITLE_RECENCY

        lexical_rows, _lexical_source = _fetch_scope_filtered(
            _fetch_sources,
            scope=scope,
            person_linked_memory_ids=frozenset(),
            target=limit,
            store_scope_complete=bool(source_scope_filters),
            source_scope_envelope=True,
        )
        lexical_rows = lexical_rows[:limit]
        ranked_lists[SOURCE_STAGE_TITLE_RECENCY] = lexical_rows

        # (d) Temporal-anchor rank boost: re-rank the candidates the lists
        # above already found by proximity to the anchor window's center.
        # Only sources with a parseable event date inside the window join;
        # everything stays fusion-honest (a wrong window cannot evict the
        # content/provenance/lexical hits, only fail to boost).
        temporal_sources: list[JsonObject] = []
        if anchor is not None:
            center = anchor.window_center
            dated: list[tuple[float, str, JsonObject]] = []
            seen_dated: set[str] = set()
            for rows in (chunk_sources, provenance_sources, lexical_rows):
                for row in rows:
                    source_id = str(row.get("id"))
                    if source_id in seen_dated:
                        continue
                    seen_dated.add(source_id)
                    event = _source_event_time(row)
                    if event is None or not (anchor.window_start <= event < anchor.window_end):
                        continue
                    dated.append((abs((event - center).total_seconds()), source_id, row))
            # Distance ties are the norm here (day-resolution session dates
            # share a window distance), so they fall through the
            # content-stable cascade before the id total-order key.
            dated.sort(key=lambda entry: (entry[0], *content_stable_tiebreak(entry[2]), entry[1]))
            temporal_sources = [row for _distance, _source_id, row in dated]
            ranked_lists[SOURCE_STAGE_TEMPORAL] = temporal_sources

        unique_candidate_ids = {
            str(row.get("id")) for rows in ranked_lists.values() for row in rows
        }
        stage_record: JsonObject = {
            "source": "rrf(" + "+".join(ranked_lists) + ")",
            "candidate_count": len(unique_candidate_ids),
            SOURCE_STAGE_CHUNK_FTS: len(chunk_sources),
            SOURCE_STAGE_PROVENANCE: len(provenance_sources),
            SOURCE_STAGE_TITLE_RECENCY: len(lexical_rows),
            "chunk_fts_source": chunk_fts_source,
        }
        if anchor is not None:
            stage_record[SOURCE_STAGE_TEMPORAL] = len(temporal_sources)
        return ranked_lists, stage_record

    def _best_chunk_without_a_winner(self, source_id: str, *, query: str) -> str | None:
        """An excerpt for a source the chunk-FTS stage never ranked.

        Only ``chunk_fts`` records a winning chunk. Sources that entered the
        pack through the provenance or title/recency lists have none, and
        measured on 55 ranked sources from real LongMemEval questions that was
        56% of them: retrieval judged them relevant and they reached the agent
        as bare citations with no text. That is the same defect as packing no
        sources at all, just narrower.

        Re-deriving here discards nothing, because there is no ranking to
        discard. It still must not repeat the mistake review caught: scoring is
        token overlap over lines, never ``term in text``, which promoted a
        wikilink block over the sentence being searched for.

        Stores without chunk listing degrade to no excerpt rather than failing,
        matching how every other optional stage capability is handled.
        """

        list_chunks = getattr(self.store, "list_source_chunks", None)
        if not callable(list_chunks):
            return None
        try:
            # Push the bound into the store when it accepts one. Without this
            # the cap below bounds only the Python loop: SQLite's
            # list_source_chunks has no LIMIT clause, so a 5,000-chunk document
            # is fully materialised before the first comparison. Postgres
            # defaults to 500. An earlier comment here claimed the cap prevented
            # a table scan; it never did, and saying so was worse than the cost.
            rows = _call_with_optional_limit(
                list_chunks, source_id, limit=SOURCE_FALLBACK_CHUNK_SCAN_LIMIT
            )
        except Exception:  # pragma: no cover - store capability probe
            return None

        query_tokens = _tokens(query)
        best_text: str | None = None
        best_score = -1
        for index, row in enumerate(rows or ()):
            # Checked FIRST. At the bottom of the body it sat after two
            # `continue`s, so a source whose rows were mostly empty or malformed
            # never reached it and the scan ran to whatever the store returned.
            if index >= SOURCE_FALLBACK_CHUNK_SCAN_LIMIT:
                break
            if not isinstance(row, Mapping):
                continue
            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            score = max(
                (_line_overlap_score(line, query_tokens) for line in text.split("\n")),
                default=0,
            )
            # Ties keep the earlier chunk, so a source with no lexical overlap
            # at all yields its opening rather than an arbitrary interior slice.
            if score > best_score:
                best_score, best_text = score, text
        return best_text

    def _packable_sources(
        self,
        items: Sequence[JsonObject],
        *,
        query: str,
        memory_visible: Callable[[Mapping[str, object]], bool],
    ) -> list[JsonObject]:
        """Compact every ranked source for packing, then label them in one pass.

        The correction label needs the memories that reference each source.
        Asking the store once per source made recall and the pack scan the
        memories table once per packed source (v0.19.0 to v0.19.2), so the
        excerpts are built first and the labels are read for all of them in one
        lookup. ``memory_visible`` is required and has no default, for the
        reason ``_packable_source`` gives.
        """

        compacted = [self._compact_source(item, query=query) for item in items]
        annotate_derived_memory_corrections(self.store, compacted, memory_visible=memory_visible)
        return compacted

    def _packable_source(
        self,
        item: JsonObject,
        *,
        query: str,
        memory_visible: Callable[[Mapping[str, object]], bool],
    ) -> JsonObject:
        """One source through ``_packable_sources``. Packs call that for the whole list."""

        return self._packable_sources([item], query=query, memory_visible=memory_visible)[0]

    def _compact_source(self, item: JsonObject, *, query: str) -> JsonObject:
        """Compact a ranked source for packing: drop the document, add an excerpt.

        The excerpt is the chunk the FTS stage already ranked highest for this
        source, windowed around its best-matching line. Re-deriving a "best"
        chunk here would discard the ranking that retrieval just did. The
        correction label is added afterwards by ``_packable_sources``, once the
        excerpt exists, because the label is about the passage the agent reads.
        """

        compacted = _compact_item(item)

        # Drop ONLY the duplicated document, never the whole blob. Temporal
        # precompute reads session_date and friends out of metadata_json via
        # _source_event_time, so stripping it wholesale silently un-dates every
        # imported source and breaks the derived timeline.
        metadata = compacted.get("metadata_json")
        if isinstance(metadata, Mapping):
            compacted["metadata_json"] = {
                key: value for key, value in metadata.items() if key != "raw_text"
            }

        source_id = str(item.get("id"))
        winner = self._winning_chunk_text.get(source_id)
        if winner is not None and not _has_readable_line(winner):
            # The stage ranked a chunk that is pure navigation. Prefer any chunk
            # of this source with real prose in it; an excerpt of link paths
            # tells the agent nothing it can quote. If the whole document is
            # links, keep the winner, because then that IS the document.
            readable = self._best_chunk_without_a_winner(source_id, query=query)
            if readable is not None and _has_readable_line(readable):
                winner = readable
        if winner is None:
            winner = self._best_chunk_without_a_winner(source_id, query=query)
        if winner:
            compacted["excerpt"] = _query_anchored_window(
                winner, query=query, max_chars=SOURCE_EXCERPT_MAX_CHARS
            )
            compacted["excerpt_kind"] = "imported_source_material"
        return compacted

    def memory_visibility(
        self,
        *,
        domains: list[str],
        sensitivity_allowed: list[str],
        scope: _ResolvedRetrievalScope | None,
    ) -> Callable[[Mapping[str, object]], bool]:
        """The fence a caller's memory reads run under, as a yes/no for one row.

        Built from the same filters the memory stages were given. All three are
        required. ``scope=None`` is the honest unscoped query and has to be
        written down at the call site. A person scope is resolved through the
        entity graph here, once, like the memory stages do.
        """

        base = _memory_visibility_predicate(
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            scope=scope,
            person_linked_memory_ids=(
                self._person_linked_memory_ids(scope.people) if scope is not None else frozenset()
            ),
        )
        project_filter = tuple(scope.projects) if scope is not None else ()

        def visible(row: Mapping[str, object]) -> bool:
            if not base(row):
                return False
            return bool(
                admit_loaded(
                    self.store,
                    kind="memory",
                    rows=[row],
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    projects=project_filter,
                )
            )

        return visible

    def fence_validity_memory_ids(
        self,
        validities: Sequence[JsonObject],
        *,
        domains: list[str],
        sensitivity_allowed: list[str],
        scope: _ResolvedRetrievalScope | None,
    ) -> None:
        """Remove the memory ids a ``validity`` annotation names outside the caller's fence.

        ``_validity_annotation`` copies the ``superseded_by`` and ``supersedes``
        pointers of a stored row into ``superseded_by_memory_id`` and
        ``supersedes_memory_id``. A recall result carries the raw row, so its
        pointers have not been through the pack's pointer fence. An id goes when
        the row it names is outside the fence the memory stages ran with. The
        ``superseded`` flag stays, so an agent still does not read the row as
        current; only the id is withheld. A pointer to a row that cannot be found
        is dropped on a scoped read and kept on an unscoped one, as on the pack.
        """

        id_keys = ("superseded_by_memory_id", "supersedes_memory_id")
        pointer_ids = [str(validity[key]) for validity in validities for key in id_keys if validity.get(key)]
        if not pointer_ids:
            return
        _drop_pointers_outside_fence(
            validities,
            id_keys,
            targets=self._memories_by_ids(pointer_ids),
            memory_visible=self.memory_visibility(
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                scope=scope,
            ),
            fail_closed_when_unresolved=scope is not None and scope.active,
        )

    def require_source_query_searchable(self, query: str) -> None:
        """Refuse, before any stage runs, a query the source search cannot take.

        The store owns the limit, because it is the store's SQL that fails: the
        SQLite store exposes ``check_source_search_query`` and raises
        ``SourceSearchQueryTooLarge``; the Postgres store and minimal test
        stores have no such limit and expose nothing, so this does nothing
        for them. Without this the same refusal still comes from
        ``search_sources``, only after the memory, vector and graph stages
        have run. Callers use it only when the source search will run, so a
        pack that turns sources off keeps taking a long query.
        """

        check = getattr(self.store, "check_source_search_query", None)
        if callable(check):
            check(query)

    def search_source_excerpts(
        self,
        *,
        query: str,
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int,
        scope: _ResolvedRetrievalScope | None,
        ranking: SourceRanking,
        winning_memories: Sequence[JsonObject] = (),
    ) -> tuple[list[JsonObject], JsonObject]:
        """Ranked imported source material for a query, with readable excerpts.

        What comes back is material the agent can read and quote, NOT facts it
        should assert: every entry carries ``excerpt_kind`` saying so. Committed
        memories remain the only channel for facts, and nothing here promotes a
        candidate or makes one searchable as a memory.

        ``scope`` is REQUIRED and has no default, deliberately. This method is a
        read path over stored documents, and ``_source_stage_lists`` treats scope
        as an exclusion filter, not a ranking hint: ``_resolve_sources`` drops
        every row that fails ``_row_matches_scope``. The first version of this
        method took no scope at all, so ``alice_recall`` returned excerpts from
        sources a project-scoped agent was never allowed to read, while
        ``alice_context_pack`` filtered the same rows correctly.

        Making the parameter required rather than optional is the point. An
        optional scope defaults to "no fence" and reintroduces that leak the
        moment someone adds a caller and forgets. ``None`` is still accepted,
        because an unscoped owner query is legitimate, but it has to be written
        down at the call site.

        ``ranking`` is required for the same reason and is passed on to
        ``_source_stage_lists``. A caller writes ``SourceRanking.document()``
        where it means the one-entry-per-document ranking v0.20.0 has; any other
        value is refused until a stage exists that honours it.

        NOT the only source reader. ``compile_context_pack`` runs its own
        ``_source_stage_lists`` -> ``_fused_candidates`` -> ``_packable_source``
        sequence, because it interleaves stages this method has no business
        carrying: the coverage/aggregation gate and the instance-diversity pass.
        An earlier version of this docstring claimed the pack routed here, which
        was never true. Saying so plainly matters, because it is the reason both
        paths have to be fenced and tested separately: they share the stage
        builder and the compaction, not the call sequence.

        No ``anchor`` parameter, on purpose. One was added here for symmetry
        with the pack and no caller ever passed it, which is untested surface
        that reads as a supported feature. Recall's ``since``/``until`` become
        part of ``scope``, which fences; a temporal anchor is a rank boost and
        is the pack's own concern. Add it back when something needs it, with a
        test.
        """

        source_lists, stage_record = self._source_stage_lists(
            query=query,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=limit,
            winning_memories=winning_memories,
            ranking=ranking,
            scope=scope,
        )
        candidates = _fused_candidates(
            source_lists,
            target_type="source",
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=limit,
        )
        memory_visible = self.memory_visibility(
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            scope=scope,
        )
        excerpts = self._packable_sources(
            [candidate.item for candidate in candidates if candidate.selected],
            query=query,
            memory_visible=memory_visible,
        )
        return excerpts, stage_record

    def compile_context_pack(self, request: VNextRetrievalRequest, *, source_fence: SourceReadFence) -> JsonObject:
        """Compile one context pack for a caller.

        ``source_fence`` is the caller's read fence over sources, and it has no default. A pack carries the quotes its
        memories saved from their sources (``supporting_evidence``, and the metadata of each memory row), and a source
        can be reclassified or archived after the memory cited it, so the quote of a source the caller may not read
        now is withheld however the pack's own scope was set. The owner passes ``SourceReadFence.unfenced()``, written
        at the call site, and is shown what was stored.
        """
        saved_provenance = SavedProvenanceReader(self.store, fence=source_fence)
        if isinstance(request.max_items, bool) or not isinstance(request.max_items, int):
            raise VNextRetrievalValidationError("max_items must be an integer")
        if request.max_items < 1 or request.max_items > MAX_CONTEXT_PACK_ITEMS:
            raise VNextRetrievalValidationError(
                f"max_items must be between 1 and {MAX_CONTEXT_PACK_ITEMS}"
            )
        if request.max_tokens is not None:
            if isinstance(request.max_tokens, bool) or not isinstance(request.max_tokens, int):
                raise VNextRetrievalValidationError("max_tokens must be an integer when set")
            if request.max_tokens < 1 or request.max_tokens > MAX_CONTEXT_PACK_TOKENS:
                raise VNextRetrievalValidationError(
                    f"max_tokens must be between 1 and {MAX_CONTEXT_PACK_TOKENS} when set"
                )
        _validate_choice(request.budget_strategy, field_name="budget_strategy", choices=BUDGET_STRATEGIES)
        _validate_choice(request.context_depth, field_name="context_depth", choices=CONTEXT_DEPTHS)
        scope = _resolve_retrieval_scope(request)
        person_linked_memory_ids = self._person_linked_memory_ids(scope.people)
        strategy = request.budget_strategy
        depth = request.context_depth
        interpretation = classify_query(request)
        terms = list(interpretation["terms"])
        domains = list(interpretation["domains"])
        sensitivity_allowed = list(interpretation["sensitivity_allowed"])
        sources_enabled = bool(interpretation["requires_sources"])
        if sources_enabled:
            self.require_source_query_searchable(request.query)
        contradictions_requested = bool(interpretation["requires_contradictions"])
        memory_types = tuple(request.memory_types)
        projects = tuple(sorted(scope.projects))
        created_by_agent_ids = tuple(request.created_by_agent_ids)
        filter_run_id = request.filter_run_id
        trace_id = request.trace_id or str(uuid4())
        context_pack_id = str(uuid4())
        max_items = request.max_items
        if depth == CONTEXT_DEPTH_MINIMAL:
            max_items = min(CONTEXT_DEPTH_MINIMAL_MAX_ITEMS, max_items)
        memory_candidate_limit = max(max_items * 2, max_items)
        # Temporal anchor from generic query text only. The reference time
        # for relative phrases is the caller's now (request.reference_time)
        # or the current UTC time; the parser itself never reads the clock.
        anchor = parse_temporal_anchor(
            request.query,
            reference_time=(
                request.reference_time if request.reference_time is not None else datetime.now(UTC)
            ),
        )

        # ---- coverage mode (aggregation intent) begin --------------------
        # Gated by the query surface ONLY (vnext_coverage_query.detect_
        # aggregation_intent); when the gate stays None — every ordinary
        # query — no coverage block in this method runs and the pack is
        # byte-identical to the ungated pipeline. When it fires, the
        # memory candidate POOL deepens here (selection slot counts never
        # change) so the instance-diversity pass below has distinct
        # instances to promote into the slots. The source pool is NOT
        # deepened: measured on the free coverage probe, a deeper source
        # pool lets tail items that appear in two ranked lists outscore
        # single-list evidence and all-coverage regressed. minimal depth
        # keeps its cheapest-useful-call promise: no detection, no
        # coverage work.
        coverage_intent = (
            None
            if depth == CONTEXT_DEPTH_MINIMAL
            else vnext_coverage_query.detect_aggregation_intent(str(interpretation["query"]))
        )
        # "How often" asks for cadence/rate, not an occurrence total. Keep
        # recognition in the trace, but preserve the old ungated candidate
        # pool, store calls, and ranking: repeated similar events are rate
        # evidence and must not be diversity-demoted.
        coverage_selection_enabled = bool(
            coverage_intent is not None
            and coverage_intent.sub_intent != vnext_coverage_query.COUNT_SUB_INTENT_CADENCE
        )
        if coverage_selection_enabled:
            memory_candidate_limit *= vnext_coverage_query.COVERAGE_POOL_MULTIPLIER
        # ---- coverage mode (aggregation intent) end ----------------------

        # Bundled stores apply people/time predicates before ranked LIMIT. The
        # Python pass remains a fail-closed verifier and an uncapped compatibility
        # path for adapters that predate the optional scope parameters.
        scope_target = memory_candidate_limit
        if scope.active:
            memory_candidate_limit = min(
                SCOPED_ROW_OVERFETCH_LIMIT,
                max(memory_candidate_limit, SCOPED_ROW_OVERFETCH_LIMIT),
            )

        fts_rows, fts_source = _fetch_scope_filtered(
            lambda n: self._memory_fts_rows(
                query=request.query,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                limit=n,
                memory_types=memory_types,
                projects=projects,
                created_by_agent_ids=created_by_agent_ids,
                run_id=filter_run_id,
                scope=scope,
                person_linked_memory_ids=person_linked_memory_ids,
            ),
            scope=scope,
            person_linked_memory_ids=person_linked_memory_ids,
            target=scope_target,
            store_scope_complete=_supports_store_scope_predicate(
                getattr(self.store, "search_memories_fts", None)
                or getattr(self.store, "search_memories", None)
            ),
        )
        # Count-intent candidate annotation. This reuses the already-fetched,
        # scoped FTS prefix: no store read, model call, or pool deepening. It
        # is intentionally limited to discrete cardinality/frequency queries;
        # numeric quantities (hours/days/pages/amounts) keep coverage mode but
        # never receive a memory-row count that could be mistaken for a sum.
        coverage_candidate_instance_count: JsonObject | None = None
        if vnext_coverage_query.supports_candidate_instance_count(coverage_intent):
            coverage_candidate_instance_count = vnext_coverage_query.candidate_instance_count_record(
                fts_rows,
                fts_source=fts_source,
                candidate_cap=scope_target,
                scope_filtered=scope.active,
            )
        if depth == CONTEXT_DEPTH_MINIMAL:
            # The cheapest useful call: FTS only. No query embedding, no
            # entity resolution or graph hop; honest tier status instead.
            vector_rows: list[JsonObject] = []
            vector_stage = STAGE_DISABLED_MINIMAL
            graph_rows: list[JsonObject] = []
            graph_stage = STAGE_DISABLED_MINIMAL
            matched_entities: list[JsonObject] = []
        else:
            query_vector, query_embedding_status = self._query_embedding(
                str(interpretation["query"])
            )
            vector_rows, vector_stage = _fetch_scope_filtered(
                lambda n: self._memory_vector_rows(
                    query=str(interpretation["query"]),
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    limit=n,
                    memory_types=memory_types,
                    projects=projects,
                    created_by_agent_ids=created_by_agent_ids,
                    run_id=filter_run_id,
                    scope=scope,
                    person_linked_memory_ids=person_linked_memory_ids,
                    query_vector=query_vector,
                    query_embedding_status=query_embedding_status,
                ),
                scope=scope,
                person_linked_memory_ids=person_linked_memory_ids,
                target=scope_target,
                store_scope_complete=_supports_store_scope_predicate(
                    getattr(self.store, "search_memories_vector", None)
                ),
            )
            graph_rows, graph_stage, matched_entities = self._memory_graph_rows(
                query=" ".join((request.query, *request.people)),
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                limit=memory_candidate_limit,
                entity_read_fenced=source_fence.entity_read_fenced,
                memory_types=memory_types,
                projects=projects,
                created_by_agent_ids=created_by_agent_ids,
                run_id=filter_run_id,
                scope_people=tuple(sorted(scope.people)),
                scope_person_memory_ids=tuple(sorted(person_linked_memory_ids)),
                scope_window_start=scope.window_start,
                scope_window_end=scope.window_end,
            )
            graph_rows = _filter_rows_for_scope(
                graph_rows,
                scope,
                person_linked_memory_ids=person_linked_memory_ids,
            )
            # Entity rows have no project/person/time columns of their own;
            # suppress their display under an explicit scope rather than
            # leaking a name merely because the unscoped resolver matched it.
            if scope.active or not graph_rows:
                matched_entities = []
        # Temporal-anchor stage: only exists when the query carried a
        # parseable date phrase. One more RRF list (never a filter), plus
        # an honest trace record; both are absent when no anchor parses.
        temporal_rows: list[JsonObject] = []
        temporal_stage_record: JsonObject | None = None
        if anchor is not None:
            if depth == CONTEXT_DEPTH_MINIMAL:
                temporal_stage = STAGE_DISABLED_MINIMAL
            else:
                temporal_rows, temporal_stage = _fetch_scope_filtered(
                    lambda n: self._memory_temporal_rows(
                        anchor=anchor,
                        domains=domains,
                        sensitivity_allowed=sensitivity_allowed,
                        limit=n,
                        memory_types=memory_types,
                        projects=projects,
                        created_by_agent_ids=created_by_agent_ids,
                        run_id=filter_run_id,
                    ),
                    scope=scope,
                    person_linked_memory_ids=person_linked_memory_ids,
                    target=scope_target,
                )
            temporal_stage_record = {
                "source": "temporal_anchor",
                "status": temporal_stage,
                "window": [anchor.window_start.isoformat(), anchor.window_end.isoformat()],
                "parsed_from": anchor.parsed_from,
                "candidate_count": len(temporal_rows),
            }
        memory_lists: dict[str, Sequence[JsonObject]] = {"fts": fts_rows}
        if vector_stage == VECTOR_STAGE_ENABLED:
            memory_lists["vector"] = vector_rows
        if graph_stage == GRAPH_STAGE_ENABLED:
            memory_lists["graph"] = graph_rows
        if temporal_stage_record is not None and temporal_stage_record["status"] == TEMPORAL_STAGE_ENABLED:
            memory_lists["temporal_anchor"] = temporal_rows

        # ---- coverage mode (aggregation intent) begin --------------------
        # Multi-clause aggregations ("X and Y", comparative pairs) run
        # capped FTS-only sub-retrievals per clause. The rows do NOT join
        # the RRF score fight (measured on the free coverage probe, naive
        # fused clause lists let generic clause fragments displace evidence
        # — all-coverage regressed); instead they backfill below: clause
        # rows enter the candidate pool right behind the fused winners, so
        # they can only fill slots the diversity pass frees. Dormant unless
        # the intent gate fired above.
        coverage_clauses: list[str] = []
        coverage_clause_lists: dict[str, list[JsonObject]] = {}
        coverage_clause_candidate_count = 0
        if coverage_selection_enabled:
            coverage_clauses = vnext_coverage_query.decompose_clauses(str(interpretation["query"]))
            if len(coverage_clauses) >= 2:
                for clause_index, clause in enumerate(coverage_clauses, start=1):
                    clause_target = min(
                        max_items,
                        vnext_coverage_query.COVERAGE_CLAUSE_FETCH_LIMIT,
                    )
                    def _fetch_clause_rows(
                        fetch_limit: int,
                        clause_query: str = clause,
                    ) -> tuple[list[JsonObject], str]:
                        return self._memory_fts_rows(
                            query=clause_query,
                            domains=domains,
                            sensitivity_allowed=sensitivity_allowed,
                            limit=fetch_limit,
                            memory_types=memory_types,
                            projects=projects,
                            created_by_agent_ids=created_by_agent_ids,
                            run_id=filter_run_id,
                            scope=scope,
                            person_linked_memory_ids=person_linked_memory_ids,
                        )

                    clause_rows, _clause_fts_source = _fetch_scope_filtered(
                        _fetch_clause_rows,
                        scope=scope,
                        person_linked_memory_ids=person_linked_memory_ids,
                        target=clause_target,
                        store_scope_complete=_supports_store_scope_predicate(
                            getattr(self.store, "search_memories_fts", None)
                            or getattr(self.store, "search_memories", None)
                        ),
                    )
                    if clause_rows:
                        coverage_clause_lists[vnext_coverage_query.clause_stage_name(clause_index)] = list(
                            clause_rows
                        )
                        coverage_clause_candidate_count += len(clause_rows)
        # ---- coverage mode (aggregation intent) end ----------------------

        # Memories fuse before the source stage runs: the provenance list
        # of the fused sources stage follows the winning memory hits.
        memory_candidates = _fused_candidates(
            memory_lists,
            target_type="memory",
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=max_items,
        )

        # The provenance list of the fused sources stage follows these
        # winners; captured here, before any coverage-mode reordering, so
        # the source stage sees the same winners with or without coverage.
        provenance_memories = [candidate.item for candidate in memory_candidates if candidate.selected]

        # ---- coverage mode (aggregation intent) begin --------------------
        # (1) Clause backfill: sub-retrieval rows enter the candidate pool
        #     immediately behind the fused winners (unselected), so each
        #     clause's best instances are first in line for any slot the
        #     diversity pass frees — without ever displacing a fused
        #     winner on score.
        # (2) Instance diversity over the memories: re-statements of an
        #     already-kept memory's provenance source are demoted behind
        #     memories from distinct sources, so an aggregation pack
        #     carries every instance instead of one instance restated.
        #     Group-key only — NO text-similarity demotion here, because
        #     two instances of the same recurring fact captured from
        #     different sources legitimately share text and are exactly
        #     what aggregation questions need.
        # The pass reorders pack slots only: provenance_memories above is
        # captured pre-diversity, so the source stage stays decoupled and
        # a demotion can never knock a session out of the pack's source
        # slots. Every baseline-selected memory's source stays represented
        # (first memory per source is never demoted), so the pack's
        # session coverage is a superset of the ungated pack's. Dormant
        # unless the gate fired.
        coverage_memory_demotions = 0
        if coverage_selection_enabled:
            if coverage_clause_lists:
                seen_candidate_ids = {str(candidate.item.get("id")) for candidate in memory_candidates}
                backfill_candidates: list[RetrievalCandidate] = []
                for stage_name, stage_rank, row in vnext_coverage_query.interleave_clause_rows(
                    coverage_clause_lists
                ):
                    row_id = str(row.get("id"))
                    if row_id in seen_candidate_ids:
                        continue
                    if _allowed(row, domains=domains, sensitivity_allowed=sensitivity_allowed) is not None:
                        continue
                    seen_candidate_ids.add(row_id)
                    backfill_candidates.append(
                        RetrievalCandidate(
                            item=row,
                            target_type="memory",
                            rank=0,  # reassigned below
                            rrf_score=0.0,  # honest: not part of RRF fusion
                            stage_ranks={stage_name: stage_rank},
                            selected=False,
                            exclusion_reason="trimmed_by_limit",
                        )
                    )
                if backfill_candidates:
                    winners = [candidate for candidate in memory_candidates if candidate.selected]
                    rest = [candidate for candidate in memory_candidates if not candidate.selected]
                    memory_candidates = [
                        replace(candidate, rank=position)
                        for position, candidate in enumerate(
                            [*winners, *backfill_candidates, *rest], start=1
                        )
                    ]
            # Window spans the whole deepened pool (baseline pool is
            # 2 x slots, coverage deepens it x POOL_MULTIPLIER), so the
            # walk can reach distinct-source instances however deep FTS
            # ranked them; group-key checks are O(1) per candidate.
            memory_candidates, coverage_memory_demotions = vnext_coverage_query.apply_instance_diversity(
                memory_candidates,
                group_key_for=vnext_coverage_query.memory_provenance_group_key,
                limit=max_items,
                consider_multiplier=2 * vnext_coverage_query.COVERAGE_POOL_MULTIPLIER,
            )
        # ---- coverage mode (aggregation intent) end ----------------------

        # ---- coverage mode (roll-up card ranking) begin -------------------
        # An ACCEPTED roll-up card pre-aggregates its member instances, so
        # for an aggregation query the card is the aggregate answer and the
        # members are its receipts — yet RRF ranks the card below its own
        # members (each member matches the query about as well and there
        # are more of them), so the receipts eat the selection slots and
        # the card never packs. When >= COVERAGE_MIN_SLOTTED_MEMBERS of a
        # card's members hold selection slots (the receipts pile-up is
        # real; a lone slotted member is an ordinary hit and, measured on
        # the free probe, promoting on one only spent tail slots), the
        # card is promoted to the best member's rank; members stay in the
        # pool directly below it (demote-not-drop — only the last slot
        # holder loses selection). Runs AFTER the diversity pass so the
        # promoted order is final, and at most
        # COVERAGE_MAX_CARD_PROMOTIONS cards promote per pack. Dormant
        # (memory_candidates untouched, byte-identical pack) unless the
        # intent gate fired above AND an accepted card co-occurs with
        # enough slot-holding members; disclosed as card_promotions on the
        # coverage_mode trace stage.
        coverage_card_promotions = 0
        if coverage_selection_enabled:
            # Preserve the pre-Sprint generic card-promotion posture. The
            # measured count-specific aggressive arm was rejected: neither a
            # candidate row nor a roll-up member is proven to represent one
            # queried unit. Cadence remains recognition-only above.
            memory_candidates, coverage_card_promotions = vnext_coverage_query.promote_rollup_cards(
                memory_candidates
            )
        # ---- coverage mode (roll-up card ranking) end ---------------------

        if sources_enabled:
            source_lists, sources_stage_record = self._source_stage_lists(
                query=request.query,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                limit=max(DEFAULT_SOURCE_LIMIT, max_items),
                winning_memories=provenance_memories,
                # Written here on purpose. The pack ranks sources by document
                # whatever recall does, so a change to recall's ranking never
                # moves a pack.
                ranking=SourceRanking.document(),
                scope=scope,
                anchor=anchor,
            )
        else:
            source_lists = {}
            sources_stage_status = (
                SOURCES_STAGE_DISABLED_BY_FLAG if request.include_sources is False else STAGE_DISABLED_MINIMAL
            )
            sources_stage_record = {"candidate_count": 0, "status": sources_stage_status}
        list_open_loops = cast(Callable[..., list[JsonObject]], self.store.list_open_loops)
        open_loop_scope_filters = (
            {
                "scope_projects": tuple(sorted(scope.projects)),
                "scope_people": tuple(sorted(scope.people)),
                "scope_window_start": scope.window_start,
                "scope_window_end": scope.window_end,
            }
            if scope.active and _supports_resource_scope_predicate(list_open_loops)
            else {}
        )

        def _fetch_open_loops(n: int) -> tuple[list[JsonObject], str]:
            return list(
                list_open_loops(
                    status="open",
                    domains=domains or None,
                    sensitivity_allowed=sensitivity_allowed,
                    limit=n,
                    **open_loop_scope_filters,
                )
            ), "listing"

        open_loop_rows, _open_loop_source = _fetch_scope_filtered(
            _fetch_open_loops,
            scope=scope,
            person_linked_memory_ids=frozenset(),
            target=DEFAULT_OPEN_LOOP_LIMIT,
            store_scope_complete=bool(open_loop_scope_filters),
        )
        open_loop_rows = admit_loaded(
            self.store,
            kind="open_loop",
            rows=open_loop_rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=tuple(scope.projects),
        )
        open_loop_rows = open_loop_rows[:DEFAULT_OPEN_LOOP_LIMIT]

        source_candidates = _fused_candidates(
            source_lists,
            target_type="source",
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=DEFAULT_SOURCE_LIMIT,
        )
        # ---- coverage mode (aggregation intent) begin --------------------
        # Instance-diversity pass over the fused sources: near-verbatim
        # duplicate sources are demoted behind distinct same-topic
        # instances so aggregation questions see every instance instead of
        # the same content repeated. Dormant (coverage_record is None, no
        # trace stage) unless the intent gate fired above.
        coverage_record: JsonObject | None = None
        if coverage_intent is not None:
            coverage_text_for = (
                vnext_coverage_query.source_chunk_text_provider(
                    getattr(self.store, "list_source_chunks", None)
                )
                if coverage_selection_enabled
                else None
            )
            coverage_source_demotions = 0
            if coverage_text_for is not None:
                source_candidates, coverage_source_demotions = vnext_coverage_query.apply_instance_diversity(
                    source_candidates,
                    text_for=coverage_text_for,
                    limit=DEFAULT_SOURCE_LIMIT,
                )
            coverage_record = vnext_coverage_query.coverage_stage_record(
                intent=coverage_intent,
                clause_count=len(coverage_clauses),
                clause_candidate_count=coverage_clause_candidate_count,
                source_diversity_enabled=coverage_text_for is not None,
                memory_demotions=coverage_memory_demotions,
                source_demotions=coverage_source_demotions,
                # roll-up card ranking (see the marked block above).
                card_promotions=coverage_card_promotions,
                candidate_instance_count=coverage_candidate_instance_count,
                diversity_status=(
                    None
                    if coverage_selection_enabled
                    else vnext_coverage_query.DIVERSITY_DISABLED_CADENCE
                ),
            )
        # ---- coverage mode (aggregation intent) end ----------------------

        # ---- reranker (disclosed precision stage) begin -------------------
        # Provider-side listwise relevance scoring over the fused candidate
        # pools — post-fusion, post-coverage, PRE-budget. Dormant unless a
        # reranker endpoint is configured (self.reranker_provider is None):
        # this block then never runs — zero provider calls, fused order
        # stands, no reranker trace stage, packs byte-identical to the
        # fusion-only path. When configured, the top
        # RERANK_MEMORY_CANDIDATE_CAP fused memory candidates and top
        # RERANK_SOURCE_CANDIDATE_CAP fused source candidates are scored
        # with the frozen generic relevance prompt (sha-pinned) and
        # reordered by score; equal scores fall through the content-stable
        # cascade. Slot counts are preserved — the reranked order fills
        # exactly as many selection slots as fusion did, and the token
        # budget packer below still decides what survives max_tokens.
        # Provider failure fails open to fused order (recorded in the stage
        # record). The provenance seeds of the source stage stay the fused
        # winners (captured above), keeping the source stage decoupled from
        # rerank order the same way it is decoupled from coverage order.
        # minimal depth skips the stage (honest disabled record) to keep
        # its cheapest-useful-call promise.
        reranker_record: JsonObject | None = None
        if self.reranker_provider is not None:
            if depth == CONTEXT_DEPTH_MINIMAL:
                reranker_record = vnext_reranker.disabled_stage_record(
                    provider=self.reranker_provider, status=STAGE_DISABLED_MINIMAL
                )
            else:
                rerank_query = str(interpretation["query"])
                memory_candidates, memory_rerank_outcome = vnext_reranker.rerank_fused_candidates(
                    memory_candidates,
                    query=rerank_query,
                    provider=self.reranker_provider,
                    limit=max_items,
                    max_candidates=vnext_reranker.RERANK_MEMORY_CANDIDATE_CAP,
                )
                source_candidates, source_rerank_outcome = vnext_reranker.rerank_fused_candidates(
                    source_candidates,
                    query=rerank_query,
                    provider=self.reranker_provider,
                    limit=DEFAULT_SOURCE_LIMIT,
                    max_candidates=vnext_reranker.RERANK_SOURCE_CANDIDATE_CAP,
                )
                reranker_record = vnext_reranker.reranker_stage_record(
                    provider=self.reranker_provider,
                    memories=memory_rerank_outcome,
                    sources=source_rerank_outcome,
                )
        # ---- reranker (disclosed precision stage) end ---------------------

        open_loop_candidates = _fused_candidates(
            {"listing": open_loop_rows},
            target_type="open_loop",
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=DEFAULT_OPEN_LOOP_LIMIT,
        )

        # The fence the memory stages ran under, built once for everything the
        # pack names that is derived from a memory row.
        pack_memory_visible = _memory_visibility_predicate(
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            scope=scope,
            person_linked_memory_ids=person_linked_memory_ids,
        )
        ranked_memories = [_compact_item(candidate.item) for candidate in memory_candidates if candidate.selected]
        # The rows the pack hands back keep the quotes their memories saved (``metadata_json.provenance`` and the other
        # copies). Those are held to the caller's read fence here, first, while each row still names the sources it
        # cited: the scope pass below removes source refs from the rows of a scoped pack (those of a source the key
        # may read too), and a memory with no provenance link keeps its sources only in those refs, so a row judged
        # after it would show the reader nothing to refuse and keep its quote. The budget prices the rows afterwards,
        # so a withheld quote is not counted in the size of the pack, and a pack for the owner or an authorized caller
        # is byte for byte what it was.
        ranked_memories = cast(list[JsonObject], saved_provenance.memories(ranked_memories))
        superseded_pointer_withheld_ids = self._sanitize_memory_scope_pointers(
            ranked_memories,
            scope=scope,
            person_linked_memory_ids=person_linked_memory_ids,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
        )
        self._drop_hidden_memory_ids_from_metadata(ranked_memories, memory_visible=pack_memory_visible)
        self._sanitize_memory_scope_references(
            ranked_memories,
            scope=scope,
            person_linked_memory_ids=person_linked_memory_ids,
        )
        ordered_memories = _order_memories_for_strategy(ranked_memories, strategy)
        # Current-version preference (demote-not-drop): when a supersession
        # pair leaks into the same pack, the replacement packs directly
        # above its superseded ancestor; every other item keeps its order.
        ordered_memories, supersession_reorders = _prefer_current_versions(ordered_memories)
        ranked_sources = self._packable_sources(
            [candidate.item for candidate in source_candidates if candidate.selected],
            query=request.query,
            memory_visible=pack_memory_visible,
        )
        ranked_open_loops = [_compact_item(candidate.item) for candidate in open_loop_candidates if candidate.selected]

        # Greedy token-budget packing, section by section. Default
        # balanced follows classify_pack_view (loops / facts / sources).
        # An explicit budget_strategy other than balanced still wins.
        # Derived sections (supporting/contradicting evidence) come from
        # the memories packed so far; when a strategy packs contradictions
        # before memories, they derive from the ranking-selected
        # (pre-budget) memories instead.
        contradictions_not_requested_status = (
            STAGE_DISABLED_MINIMAL
            if depth == CONTEXT_DEPTH_MINIMAL and request.include_contradictions is None
            else CONTRADICTIONS_STAGE_NOT_REQUESTED
        )
        section_order = budget_section_order(
            budget_strategy=strategy,
            pack_view=str(interpretation.get("pack_view") or PACK_VIEW_FACTS),
        )
        # Wrapped before any item is priced. Admitting the bare row and
        # emitting the wrapped one made the budget count something smaller
        # than the pack carries, under-counting every promoted row by the
        # whole provenance record.
        memory_offers = [_with_write_provenance(item) for item in ordered_memories]

        def pack_sections(
            budget: _TokenBudget,
            *,
            memories: list[JsonObject],
            open_loops: list[JsonObject],
            sources: list[JsonObject],
        ) -> _PackedSections:
            selected_memories: list[JsonObject] = []
            selected_open_loops: list[JsonObject] = []
            selected_sources: list[JsonObject] = []
            supporting_evidence: list[JsonObject] = []
            contradicting_evidence: list[JsonObject] = []
            contradictions_stage = contradictions_not_requested_status
            memories_packed = False
            for section in section_order:
                budget.open_section(section)
                if section == SECTION_RELEVANT_MEMORIES:
                    selected_memories = [item for item in memories if budget.admit(item, section=section)]
                    memories_packed = True
                elif section == SECTION_OPEN_LOOPS:
                    selected_open_loops = [item for item in open_loops if budget.admit(item, section=section)]
                elif section == SECTION_SOURCES:
                    selected_sources = [item for item in sources if budget.admit(item, section=section)]
                elif section == SECTION_SUPPORTING_EVIDENCE:
                    evidence_base = selected_memories if memories_packed else ordered_memories
                    supporting_evidence = [
                        evidence
                        for evidence in self._supporting_evidence(
                            evidence_base, scope=scope, saved_provenance=saved_provenance
                        )
                        if budget.admit(evidence, section=section)
                    ]
                elif section == SECTION_CONTRADICTING_EVIDENCE:
                    contradiction_base = selected_memories if memories_packed else ordered_memories
                    contradiction_records, contradictions_stage = self._contradicting_evidence(
                        contradiction_base,
                        requested=contradictions_requested,
                        domains=domains,
                        sensitivity_allowed=sensitivity_allowed,
                        scope=scope,
                        person_linked_memory_ids=person_linked_memory_ids,
                        not_requested_status=contradictions_not_requested_status,
                    )
                    contradicting_evidence = [
                        record for record in contradiction_records if budget.admit(record, section=section)
                    ]
            return _PackedSections(
                selected_memories,
                selected_open_loops,
                selected_sources,
                supporting_evidence,
                contradicting_evidence,
                contradictions_stage,
            )

        budget = _TokenBudget(token_budget=request.max_tokens, strategy=strategy)
        packed = pack_sections(
            budget, memories=memory_offers, open_loops=ranked_open_loops, sources=ranked_sources
        )
        if budget.token_budget is not None and budget.token_estimate == 0:
            # Nothing fit whole. Cut the first item that can be made to fit,
            # taking items in offer order, and pack again with that cut item in
            # place of its whole self. Everything else is offered exactly as it
            # was, so the cut item is admitted first and the rest are dropped
            # as before. An item whose ids and metadata alone cost more than
            # the budget cannot be cut small enough, so the next one is tried.
            offers = {
                SECTION_RELEVANT_MEMORIES: list(memory_offers),
                SECTION_OPEN_LOOPS: list(ranked_open_loops),
                SECTION_SOURCES: list(ranked_sources),
            }
            first_fit = _first_item_that_fits_when_cut(
                offers,
                section_order=section_order,
                query=request.query,
                max_tokens=budget.token_budget,
            )
            if first_fit is not None:
                fit_section, fit_index, fitted = first_fit
                offers[fit_section][fit_index] = fitted
                budget = _TokenBudget(token_budget=request.max_tokens, strategy=strategy)
                packed = pack_sections(
                    budget,
                    memories=offers[SECTION_RELEVANT_MEMORIES],
                    open_loops=offers[SECTION_OPEN_LOOPS],
                    sources=offers[SECTION_SOURCES],
                )
                budget.truncated = True
                budget.cut_item_count = 1
        (
            selected_memories,
            selected_open_loops,
            selected_sources,
            supporting_evidence,
            contradicting_evidence,
            contradictions_stage,
        ) = packed
        memory_candidates = _apply_budget_exclusions(memory_candidates, selected_memories)
        open_loop_candidates = _apply_budget_exclusions(open_loop_candidates, selected_open_loops)
        source_candidates = _apply_budget_exclusions(source_candidates, selected_sources)

        now = datetime.now(UTC)
        # Pack-local back-pointers: a pack-mate's supersedes pointer marks
        # its ancestor as superseded even when the ancestor row never
        # received the superseded_by column (one-sided patches). First
        # claim wins, mirroring _prefer_current_versions.
        superseded_by_packmate: dict[str, str] = {}
        for memory in selected_memories:
            supersedes_pointer = memory.get("supersedes")
            if supersedes_pointer:
                superseded_by_packmate.setdefault(str(supersedes_pointer), str(memory.get("id")))
        for memory in selected_memories:
            staleness = _staleness_note(memory, now=now)
            if staleness is not None:
                memory["staleness"] = staleness
            validity = _validity_annotation(
                memory,
                superseded_by_hint=superseded_by_packmate.get(str(memory.get("id"))),
                superseded_pointer_withheld=str(memory.get("id") or "") in superseded_pointer_withheld_ids,
            )
            if validity is not None:
                memory["validity"] = validity

        # ---- currency chains (read-time update chains) begin --------------
        # Same-slot update chains over the PACKED memories: rows sharing a
        # derived fact key, confirmed by supersession edges or same
        # unit/currency-class values plus a shared topic token, regroup
        # into one contiguous block ordered oldest first with the CURRENT
        # value last and every entry annotated (memory["currency"]).
        # Selection, budget, and every non-member row are untouched;
        # ambiguous groups emit no chain and are only counted. Dormant —
        # selected_memories unchanged (same list object), no annotations,
        # no trace stage below — for every pack without a confirmable
        # same-key group, and always at minimal depth (whose
        # cheapest-useful-call promise excludes the provenance-source date
        # lookups this stage may need).
        currency_record: JsonObject | None = None
        if depth != CONTEXT_DEPTH_MINIMAL and len(selected_memories) >= 2:
            currency_source_cache: dict[str, JsonObject | None] = {}
            currency_get_source = getattr(self.store, "get_source", None)

            def _currency_source(source_id: str) -> JsonObject | None:
                if source_id not in currency_source_cache:
                    source = currency_get_source(source_id) if callable(currency_get_source) else None
                    currency_source_cache[source_id] = (
                        cast(JsonObject, source)
                        if isinstance(source, Mapping)
                        and _row_matches_scope(
                            source,
                            scope,
                            source_scope_envelope=True,
                        )
                        else None
                    )
                return currency_source_cache[source_id]

            currency_result = vnext_currency.build_currency_chains(
                selected_memories, source_lookup=_currency_source
            )
            if currency_result.considered:
                selected_memories = vnext_currency.apply_currency_chains(
                    selected_memories, currency_result
                )
                currency_record = vnext_currency.currency_stage_record(currency_result)
        # ---- currency chains (read-time update chains) end ----------------

        supersession_context: list[JsonObject] | None = None
        if depth == CONTEXT_DEPTH_HIGH:
            supersession_context = self._supersession_context(
                selected_memories,
                scope=scope,
                memory_visible=pack_memory_visible,
            )

        if depth == CONTEXT_DEPTH_MINIMAL:
            recent_changes: list[JsonObject] | None = None
            recent_changes_stage_record: JsonObject = {"status": STAGE_DISABLED_MINIMAL, "candidate_count": 0}
        else:
            recent_changes = self._recent_changes(
                scope=scope,
                person_linked_memory_ids=person_linked_memory_ids,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
            )
            recent_changes_stage_record = {"candidate_count": len(recent_changes)}

        # Content-bearing sections added after the primary retrieval rows
        # participate in the same max_tokens budget. Navigation/diagnostic
        # envelope fields stay outside that budget and are named explicitly
        # in the final report below.
        for supplemental_section in SUPPLEMENTAL_BUDGET_SECTIONS:
            budget.open_section(supplemental_section)

        for item in (*selected_memories, *selected_sources):
            for annotation_key in BUDGETED_ITEM_ANNOTATION_KEYS:
                annotation = item.get(annotation_key)
                if annotation is None:
                    continue
                if not budget.admit(
                    {annotation_key: annotation},
                    section=SECTION_ITEM_ANNOTATIONS,
                ):
                    item.pop(annotation_key, None)

        matched_entities = [
            entity
            for entity in matched_entities
            if budget.admit(entity, section=SECTION_ENTITIES)
        ]
        if recent_changes is not None:
            recent_changes_candidate_count = len(recent_changes)
            recent_changes = [
                change
                for change in recent_changes
                if budget.admit(change, section=SECTION_RECENT_CHANGES)
            ]
            if len(recent_changes) != recent_changes_candidate_count:
                recent_changes_stage_record["selected_count"] = len(recent_changes)
        if supersession_context is not None:
            supersession_context = [
                note
                for note in supersession_context
                if budget.admit(note, section=SECTION_SUPERSESSION_CONTEXT)
            ]

        warnings = self._warnings(
            memory_candidates=memory_candidates,
            source_candidates=source_candidates,
            open_loop_candidates=open_loop_candidates,
        )
        all_candidates = [*memory_candidates, *source_candidates, *open_loop_candidates]
        excluded_counts: dict[str, int] = {}
        for candidate in all_candidates:
            if candidate.exclusion_reason is None:
                continue
            excluded_counts[candidate.exclusion_reason] = excluded_counts.get(candidate.exclusion_reason, 0) + 1
        selected_trace = [candidate.to_trace_record() for candidate in all_candidates if candidate.selected]
        trace = {
            "trace_id": trace_id,
            "candidate_count": len(all_candidates),
            "selected_count": len(selected_trace),
            "query_terms": terms,
            "filters": {
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
                "time_window": request.time_window,
                "memory_types": list(memory_types),
                "projects": list(projects),
                "created_by_agent_ids": list(created_by_agent_ids),
                "run_id": filter_run_id,
            },
            "fusion": {
                "algorithm": "reciprocal_rank_fusion",
                "k": RRF_K,
                # Honest disclosure: equal-score ties resolve on row content
                # (see content_stable_tiebreak), no longer on the raw id.
                "tie_break": TIE_BREAK_CONTENT_STABLE,
            },
            "vector_stage": vector_stage,
            "context_depth": depth,
            "budget_strategy": strategy,
            "budget": budget.to_record(),
            "supersession_reorders": supersession_reorders,
            "stages": {
                "fts": {"source": fts_source, "candidate_count": len(fts_rows)},
                "vector": {"status": vector_stage, "candidate_count": len(vector_rows)},
                "graph": {
                    "status": graph_stage,
                    "matched_entities": matched_entities,
                    "candidate_count": len(graph_rows),
                },
                "sources": sources_stage_record,
                "open_loops": {"candidate_count": len(open_loop_rows)},
                "contradictions": {"status": contradictions_stage, "candidate_count": len(contradicting_evidence)},
                "recent_changes": recent_changes_stage_record,
            },
            "selected": selected_trace,
            "excluded_counts": excluded_counts,
        }
        if temporal_stage_record is not None:
            trace["stages"]["temporal_anchor"] = temporal_stage_record  # type: ignore[index]
        if supersession_context is not None:
            trace["stages"]["supersession"] = {  # type: ignore[index]
                "status": SUPERSESSION_STAGE_ENABLED,
                "candidate_count": len(supersession_context),
            }
        if coverage_record is not None:
            # coverage mode (aggregation intent): absent when dormant so
            # ungated traces stay byte-identical.
            trace["stages"][vnext_coverage_query.COVERAGE_STAGE] = coverage_record  # type: ignore[index]
        # ---- reranker (disclosed precision stage) begin -------------------
        if reranker_record is not None:
            # Absent when unconfigured so dormant traces stay byte-identical.
            trace["stages"][vnext_reranker.RERANKER_STAGE] = reranker_record  # type: ignore[index]
        # ---- reranker (disclosed precision stage) end ---------------------
        # ---- currency chains (read-time update chains) begin --------------
        if currency_record is not None:
            # Absent when dormant so ungated traces stay byte-identical.
            trace["stages"][vnext_currency.CURRENCY_STAGE] = currency_record  # type: ignore[index]
        # ---- currency chains (read-time update chains) end ----------------
        pack: JsonObject = {
            "context_pack_id": context_pack_id,
            "query_interpretation": interpretation,
        }
        if matched_entities:
            # WHO the pack is about: compact resolved entities from the
            # graph stage. Only present when the query matched entities.
            pack["entities"] = matched_entities
        pack.update({
            # Compact references only; the full rows appear once, in
            # relevant_memories.
            "current_known_state": [_memory_reference(item) for item in selected_memories],
            "relevant_memories": selected_memories,
        })
        if depth != CONTEXT_DEPTH_MINIMAL:
            # Typed sections are views over relevant_memories; minimal
            # keeps only the memories themselves.
            pack.update({
                "relevant_beliefs": [
                    item for item in selected_memories if item.get("memory_type") in {"belief", "thesis"}
                ],
                "decisions": [item for item in selected_memories if item.get("memory_type") == "decision"],
                "procedures": [
                    item for item in selected_memories if item.get("memory_type") in {"procedure", "routine"}
                ],
            })
        pack.update({
            "open_loops": selected_open_loops,
            "supporting_evidence": supporting_evidence,
            "contradicting_evidence": contradicting_evidence,
        })
        if recent_changes is not None:
            pack["recent_changes"] = recent_changes
        if supersession_context is not None:
            pack["supersession_context"] = supersession_context
        pack.update({
            "missing_information": self._missing_information(
                selected_memories, selected_sources, sources_enabled=sources_enabled
            ),
            "sources": selected_sources,
            "warnings": warnings,
            "budget": budget.to_record(),
            "context_depth": depth,
            "trace_id": trace_id,
            "trace": trace,
            "agent_identity": request.agent_identity,
            "policy_decision": request.policy_decision,
        })
        # -- entity grounding (vnext_grounding integration; single block) ------
        # Pack-level retrieval statistic: salient query entities with ZERO
        # corpus support (entity substrate miss AND one-row FTS probe miss).
        # Read-only, additive, and absent for every ungated query -- packs
        # without unsupported entities are byte-identical to the old path.
        # Skipped at minimal depth to preserve its cheapest-call promise.
        if depth != CONTEXT_DEPTH_MINIMAL and not scope.active:
            try:
                grounding = compute_query_grounding(
                    self.store,
                    request.query,
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    allow_entity_lookup=not source_fence.entity_read_fenced,
                    admitted_entity_names=tuple(str(entity["name"]) for entity in matched_entities),
                    admitted_entity_ids=tuple(str(entity["id"]) for entity in matched_entities),
                )
            except Exception:
                # Final best-effort boundary: operational probe failures must
                # never abort a context pack. BaseException remains visible so
                # cancellation and process-control signals are not swallowed.
                grounding = None
            if grounding is not None and budget.admit(grounding, section=SECTION_GROUNDING):
                pack["grounding"] = grounding
                trace["grounding"] = dict(grounding)
        # -- end entity grounding ----------------------------------------------
        # ---- temporal precompute (machine-readable time + derived values) begin
        # Date questions fail on ARITHMETIC, not recall: the dated evidence
        # is in the pack, but the reader still has to compute deltas,
        # orderings, and spans from raw timestamps. Two additive,
        # deterministic presentation moves over the ALREADY-SELECTED items
        # (selection, ordering, and budget above are untouched):
        #
        # (1) Every selected memory/source with a resolvable event date gets
        #     a machine-readable ISO-8601 ``event_time``. Sources use
        #     ``_source_event_time`` (the temporal stage's event semantic).
        #     Memories use their content-honest signals (``valid_from``,
        #     connector-stamped metadata dates via ``_tiebreak_event_time``)
        #     and then — at non-minimal depth only, keeping minimal's
        #     cheapest-call promise of zero extra store reads — fall back to
        #     their provenance source's event time (``metadata_json.
        #     source_id`` -> ``get_source``, cached per compile, duck-typed
        #     like every optional stage). Deliberately NO write-clock
        #     fallback for memory rows themselves: an imported/replayed
        #     memory must not present ingest day as its event date.
        # (2) When the caller supplied ``reference_time`` (the caller's
        #     "now"; the service NEVER substitutes the wall clock here, so
        #     identical inputs keep producing byte-identical derived
        #     values), a bounded derived-values block precomputes the date
        #     arithmetic for the dated items — delta to the reference,
        #     chronological ordinal, span — every line marked "[derived]"
        #     (``vnext_temporal_query.derived_timeline_lines``, at most
        #     ``DERIVED_TIMELINE_MAX_LINES`` lines). Question-agnostic: it
        #     fires for ANY dated items, never on question shapes. Dormant
        #     (no pack key, no trace stage, byte-identical pack) when
        #     ``reference_time`` is absent.
        temporal_get_source = (
            getattr(self.store, "get_source", None) if depth != CONTEXT_DEPTH_MINIMAL else None
        )
        temporal_source_dates: dict[str, datetime | None] = {}
        temporal_anchored_items = 0
        temporal_dated_events: list[datetime] = []

        def _memory_event_time(memory: JsonObject) -> datetime | None:
            event = _tiebreak_event_time(memory)
            if event is not None or temporal_get_source is None:
                return event
            metadata = memory.get("metadata_json")
            source_id = str(metadata.get("source_id") or "") if isinstance(metadata, Mapping) else ""
            if not source_id:
                return None
            if source_id not in temporal_source_dates:
                source_row = temporal_get_source(source_id)
                temporal_source_dates[source_id] = (
                    _source_event_time(cast(JsonObject, source_row))
                    if isinstance(source_row, Mapping)
                    and _row_matches_scope(
                        source_row,
                        scope,
                        source_scope_envelope=True,
                    )
                    else None
                )
            return temporal_source_dates[source_id]

        for pack_item, event_time in (
            *((item, _memory_event_time(item)) for item in selected_memories),
            *((item, _source_event_time(item)) for item in selected_sources),
        ):
            if event_time is None:
                continue
            temporal_dated_events.append(event_time)
            event_time_text = event_time.isoformat()
            if budget.admit(
                {"event_time": event_time_text},
                section=SECTION_ITEM_ANNOTATIONS,
            ):
                pack_item["event_time"] = event_time_text
                temporal_anchored_items += 1
        if request.reference_time is not None:
            derived_lines = derived_timeline_lines(
                temporal_dated_events,
                reference_time=request.reference_time,
            )
            if derived_lines:
                derived_values: JsonObject = {
                    # parse_event_datetime is the UTC normalizer the stamps
                    # above already trust; reference_time is a datetime, so
                    # this is a pure aware/naive->UTC conversion.
                    "reference_time": parse_event_datetime(request.reference_time).isoformat(),  # type: ignore[union-attr]
                    "lines": derived_lines,
                }
                if budget.admit(derived_values, section=SECTION_DERIVED_VALUES):
                    pack["derived_values"] = derived_values
            trace["stages"]["temporal_precompute"] = {  # type: ignore[index]
                "anchored_items": temporal_anchored_items,
                "derived_lines": len(derived_lines),
            }
        # ---- temporal precompute (machine-readable time + derived values) end

        # Final budget report: max_tokens is an enforced content budget, not
        # a transport-envelope cap. The complete serialized estimate and the
        # exact diagnostic/navigation exclusions make that distinction
        # machine-readable instead of leaving callers to infer it.
        budget_record = budget.to_record()
        budget_record.update(
            {
                "scope": "content_sections",
                "counted_sections": list(budget.allocation),
                "excluded_sections": list(BUDGET_EXCLUDED_SECTIONS),
                "is_transport_cap": False,
            }
        )
        pack["budget"] = budget_record
        trace["budget"] = budget_record
        # The report is itself serialized twice (pack.budget and
        # pack.trace.budget), so adding its own estimate changes the value
        # being estimated. Iterate the two integer fields to a fixed point;
        # otherwise callers see the pre-report size (for example 998) while
        # serializing a larger envelope (for example 1029).
        budget_record["serialized_token_estimate"] = 0
        budget_record["excluded_token_estimate"] = 0
        for _attempt in range(32):
            serialized_token_estimate = estimate_item_tokens(pack)
            excluded_token_estimate = max(
                0,
                serialized_token_estimate - budget.token_estimate,
            )
            if (
                budget_record["serialized_token_estimate"] == serialized_token_estimate
                and budget_record["excluded_token_estimate"] == excluded_token_estimate
            ):
                break
            budget_record["serialized_token_estimate"] = serialized_token_estimate
            budget_record["excluded_token_estimate"] = excluded_token_estimate
        else:  # pragma: no cover - integer digit widths converge in a few iterations
            raise RuntimeError("context-pack budget report did not converge")
        append_event(
            self.store,
            event_type="retrieval.context_pack_compiled",
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            target_type="context_pack",
            target_id=context_pack_id,
            trace_id=trace_id,
            run_id=request.run_id,
            payload={
                "query": request.query,
                "query_type": interpretation["query_type"],
                "candidate_count": trace["candidate_count"],
                "selected_count": trace["selected_count"],
                "vector_stage": vector_stage,
                "graph_stage": graph_stage,
                "context_depth": depth,
                "budget_strategy": strategy,
                "budget": budget.to_record(),
                "warnings": warnings,
                "agent_identity": request.agent_identity,
                "policy_decision": request.policy_decision,
            },
        )
        if request.actor_type == "agent" and request.actor_id is not None:
            append_event(
                self.store,
                event_type="agent.context_pack_requested",
                actor_type="agent",
                actor_id=request.actor_id,
                target_type="context_pack",
                target_id=context_pack_id,
                trace_id=trace_id,
                run_id=request.run_id,
                payload={
                    "query": request.query,
                    "query_type": interpretation["query_type"],
                    "selected_count": trace["selected_count"],
                    "agent_identity": request.agent_identity,
                    "policy_decision": request.policy_decision,
                },
            )
        return pack

    def _contradicting_evidence(
        self,
        memories: list[JsonObject],
        *,
        requested: bool,
        domains: list[str],
        sensitivity_allowed: list[str],
        scope: _ResolvedRetrievalScope,
        person_linked_memory_ids: frozenset[str],
        not_requested_status: str = CONTRADICTIONS_STAGE_NOT_REQUESTED,
    ) -> tuple[list[JsonObject], str]:
        """Contradiction candidates between the selected memories and active beliefs.

        Reuses the pure detection helpers behind VNextContradictionService
        without persisting edges, artifacts, or events (read-only path).
        Stores without ``list_beliefs`` (e.g. the SQLite on-ramp) degrade
        to an empty section with an honest stage status.
        """
        if not requested:
            return [], not_requested_status
        list_beliefs = getattr(self.store, "list_beliefs", None)
        if not callable(list_beliefs):
            return [], CONTRADICTIONS_STAGE_NO_STORE_SUPPORT
        limit = vnext_contradictions.DEFAULT_CONTRADICTION_LIMIT
        new_items = [memory for memory in memories if memory.get("memory_type") not in {"belief", "thesis"}]
        if not new_items:
            return [], CONTRADICTIONS_STAGE_ENABLED
        belief_target = max(limit * 2, limit)
        if scope.active:
            scoped_belief_parameters = (
                "scope_projects",
                "scope_people",
                "scope_person_memory_ids",
                "scope_window_start",
                "scope_window_end",
            )
            if _supports_explicit_parameters(list_beliefs, scoped_belief_parameters):
                beliefs = list(
                    list_beliefs(
                        status="active",
                        domains=domains or None,
                        sensitivity_allowed=sensitivity_allowed,
                        scope_projects=tuple(sorted(scope.projects)),
                        scope_people=tuple(sorted(scope.people)),
                        scope_person_memory_ids=tuple(sorted(person_linked_memory_ids)),
                        scope_window_start=scope.window_start,
                        scope_window_end=scope.window_end,
                        limit=belief_target,
                    )
                )
            else:
                def _select_scoped_beliefs(rows: Sequence[JsonObject]) -> list[JsonObject]:
                    backing_by_id = self._memories_by_ids(
                        [str(row.get("memory_id") or "") for row in rows]
                    )
                    return [
                        belief
                        for belief in rows
                        if (
                            backing := backing_by_id.get(str(belief.get("memory_id") or ""))
                        )
                        is not None
                        and _row_matches_scope(
                            backing,
                            scope,
                            person_linked_memory_ids=person_linked_memory_ids,
                        )
                    ]

                beliefs, _belief_source = _fetch_filtered_prefix(
                    lambda n: (
                        list(
                            list_beliefs(
                                status="active",
                                domains=domains or None,
                                sensitivity_allowed=sensitivity_allowed,
                                limit=n,
                            )
                        ),
                        "listing",
                    ),
                    select_rows=_select_scoped_beliefs,
                    target=belief_target,
                )
        else:
            beliefs = list_beliefs(
                status="active",
                domains=domains or None,
                sensitivity_allowed=sensitivity_allowed,
                limit=belief_target,
            )
        beliefs = LabelGuard.for_filters(
            self.store,
            domains,
            sensitivity_allowed,
            tuple(scope.projects),
        ).admit_beliefs(list(beliefs))
        candidates = vnext_contradictions._find_candidates(  # noqa: SLF001 - deliberate read-only reuse
            new_items=new_items,
            beliefs=list(beliefs),
            limit=limit,
        )
        return [candidate.to_record() for candidate in candidates], CONTRADICTIONS_STAGE_ENABLED

    def _recent_changes(
        self,
        *,
        scope: _ResolvedRetrievalScope,
        person_linked_memory_ids: frozenset[str],
        domains: list[str],
        sensitivity_allowed: list[str],
        limit: int = DEFAULT_RECENT_CHANGES_LIMIT,
    ) -> list[JsonObject]:
        """Most recent ``memory.*`` events from the store event log.

        An entry names the memory it is about in ``target_id``, and a memory id
        is itself sensitive metadata. An event is kept only when its target
        passes the fence the pack's memory stages ran with: the domain and
        sensitivity ceiling, and the project and person scope when one is
        active. The time window bounds the event, not the row it is about, so
        the predicate is built without it. An event whose target cannot be
        found is dropped on a scoped pack and kept on an unscoped one.
        """
        list_memory_events = getattr(self.store, "list_memory_events", None)
        list_events = getattr(self.store, "list_events", None)
        if not callable(list_memory_events) and not callable(list_events):
            return []
        identity_scope = _ResolvedRetrievalScope(
            projects=scope.projects,
            people=scope.people,
            window_start=None,
            window_end=None,
            exclude_global_domains=scope.exclude_global_domains,
        )
        memory_visible = _memory_visibility_predicate(
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            scope=identity_scope,
            person_linked_memory_ids=person_linked_memory_ids,
        )

        def _select_events(rows: Sequence[JsonObject]) -> list[JsonObject]:
            eligible = [
                event
                for event in rows
                if str(event.get("event_type") or "").startswith("memory.")
                and (
                    scope.window_start is None
                    or (
                        (occurred_at := _row_scope_event_time(event)) is not None
                        and occurred_at >= scope.window_start
                        and (scope.window_end is None or occurred_at <= scope.window_end)
                    )
                )
            ]
            targets = self._memories_by_ids(
                [str(event.get("target_id") or "") for event in eligible]
            )
            admitted_targets = {
                str(row.get("id"))
                for row in admit_loaded(
                    self.store,
                    kind="memory",
                    rows=list(targets.values()),
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    projects=tuple(scope.projects),
                )
            }

            def _target_visible(event: JsonObject) -> bool:
                target = targets.get(str(event.get("target_id") or ""))
                if target is None:
                    # No such row is not a hidden row: the lookup applies no
                    # fence. A scoped pack fails closed on it, as it always has.
                    return not identity_scope.active
                if str(target.get("id")) not in admitted_targets:
                    return False
                return memory_visible(target)

            return [event for event in eligible if _target_visible(event)]

        scoped_event_parameters = (
            "event_type_prefix",
            "scope_projects",
            "scope_people",
            "scope_person_memory_ids",
            "scope_window_start",
            "scope_window_end",
        )
        use_scoped_events = _supports_explicit_parameters(list_memory_events, scoped_event_parameters)

        def _fetch_events(row_limit: int) -> tuple[list[JsonObject], str]:
            # The store applies project, person and time scope before its LIMIT.
            # The domain and sensitivity ceiling is applied after it, so the
            # prefix is deepened until enough visible events survive.
            if use_scoped_events:
                scoped_list_memory_events = cast(
                    Callable[..., list[JsonObject]],
                    list_memory_events,
                )
                return (
                    list(
                        scoped_list_memory_events(
                            event_type_prefix="memory.",
                            scope_projects=tuple(sorted(scope.projects)),
                            scope_people=tuple(sorted(scope.people)),
                            scope_person_memory_ids=tuple(sorted(person_linked_memory_ids)),
                            scope_window_start=scope.window_start,
                            scope_window_end=scope.window_end,
                            limit=row_limit,
                        )
                    ),
                    "scoped",
                )
            assert callable(list_events)
            return list(list_events(target_type="memory", limit=row_limit)), "listing"

        selected_so_far: list[JsonObject] = []

        def _select_and_remember(rows: Sequence[JsonObject]) -> list[JsonObject]:
            nonlocal selected_so_far
            selected_so_far = _select_events(rows)
            return selected_so_far

        try:
            events, _event_source = _fetch_filtered_prefix(
                _fetch_events,
                select_rows=_select_and_remember,
                target=limit,
                initial_limit=limit * 4,
                max_rows=RECENT_CHANGES_SCAN_MAX_ROWS,
            )
        except VNextRetrievalCompletenessError:
            # Recent changes only add to a pack, and dropping an entry is the safe
            # direction, so a vault whose newest events are nearly all hidden from
            # this caller gets the visible ones found so far (newest first), not a
            # failed pack. The older visible events past the scan are not listed.
            logger.warning(
                "recent_changes scan ended early (ceiling %d rows) with %d visible events found",
                RECENT_CHANGES_SCAN_MAX_ROWS,
                len(selected_so_far),
            )
            events = _dedupe_retrieval_rows(selected_so_far)
        return [
            {
                "event_id": str(event.get("id")),
                "event_type": str(event.get("event_type") or ""),
                "target_id": event.get("target_id"),
                "occurred_at": event.get("occurred_at"),
                "actor_type": event.get("actor_type"),
            }
            for event in events[:limit]
        ]

    def _supporting_evidence(
        self,
        memories: list[JsonObject],
        *,
        scope: _ResolvedRetrievalScope,
        saved_provenance: SavedProvenanceReader,
    ) -> list[JsonObject]:
        evidence: list[JsonObject] = []
        memory_ids = [str(memory.get("id")) for memory in memories]
        links_by_target = self._provenance_by_target(
            target_type="memory", target_ids=memory_ids
        )
        # The caller's read fence on each linked source, asked now and whatever the pack's scope is: a link keeps the
        # quote it was made with, and the source can have been reclassified or archived since. A link whose source is
        # missing, archived or outside the fence is left out whole, as a link that was never stored would be.
        saved_provenance.judge_links(link for links in links_by_target.values() for link in links)
        sources_by_id = (
            self._sources_by_ids(
                [
                    str(link.get("source_id"))
                    for links in links_by_target.values()
                    for link in links
                    if link.get("source_id")
                ]
            )
            if scope.active
            else {}
        )
        for memory in memories:
            memory_id = str(memory.get("id"))
            for stored_link in links_by_target.get(memory_id, []):
                link = saved_provenance.shown_link(stored_link)
                if link is None:
                    continue
                if scope.active:
                    source_id = str(link.get("source_id") or "")
                    source = sources_by_id.get(source_id)
                    if source is None or not _row_matches_scope(
                        source,
                        scope,
                        source_scope_envelope=True,
                    ):
                        continue
                evidence.append(
                    {
                        "target_type": "memory",
                        "target_id": memory_id,
                        "source_id": link.get("source_id"),
                        "source_chunk_id": link.get("source_chunk_id"),
                        "quote": link.get("quote"),
                        "evidence_role": link.get("evidence_role"),
                        "confidence": link.get("confidence"),
                    }
                )
        return evidence

    @staticmethod
    def _warnings(
        *,
        memory_candidates: list[RetrievalCandidate],
        source_candidates: list[RetrievalCandidate],
        open_loop_candidates: list[RetrievalCandidate],
    ) -> list[str]:
        candidates = [*memory_candidates, *source_candidates, *open_loop_candidates]
        warnings: list[str] = []
        if not any(candidate.selected for candidate in memory_candidates):
            warnings.append("no_relevant_memories_selected")
        if any(candidate.exclusion_reason == "sensitivity_filtered" for candidate in candidates):
            warnings.append("sensitive_items_filtered")
        if any(candidate.exclusion_reason == "domain_filtered" for candidate in candidates):
            warnings.append("domain_items_filtered")
        return warnings

    @staticmethod
    def _missing_information(
        memories: list[JsonObject],
        sources: list[JsonObject],
        *,
        sources_enabled: bool = True,
    ) -> list[JsonObject]:
        missing: list[JsonObject] = []
        if not memories:
            missing.append({"kind": "memory", "reason": "No matching memory was selected."})
        if sources_enabled and not sources:
            missing.append({"kind": "source", "reason": "No matching source was selected."})
        return missing

    def _supersession_context(
        self,
        memories: list[JsonObject],
        *,
        scope: _ResolvedRetrievalScope,
        memory_visible: Callable[[Mapping[str, object]], bool],
    ) -> list[JsonObject]:
        """Compact supersession chain notes (context_depth=high only).

        For each packed memory carrying a ``supersedes`` or
        ``superseded_by`` pointer, walk each direction through
        ``get_memory`` (duck-typed; unresolvable pointers degrade to
        id-only references) up to SUPERSESSION_CHAIN_HOP_LIMIT hops with a
        cycle guard. Deterministic — chain notes quote stored rows only.

        A revision's id and title are named only when the row passes
        ``memory_visible``, the fence the pack's memory stages ran under. A
        hidden revision ends the walk and names nothing, itself or anything
        past it. That is a different outcome from a pointer to no row at all,
        which an unscoped pack still shows as an id-only reference and a scoped
        pack drops. ``memory_visible`` is required and has no default, so a
        caller cannot walk the chain without a fence by leaving it out.
        """
        get_memory = getattr(self.store, "get_memory", None)

        def resolver(memory_id: str) -> JsonObject | None:
            return get_memory(memory_id) if callable(get_memory) else None

        notes: list[JsonObject] = []
        for memory in memories:
            supersedes_pointer = memory.get("supersedes")
            superseded_by_pointer = memory.get("superseded_by")
            if not supersedes_pointer and not superseded_by_pointer:
                continue
            memory_id = str(memory.get("id"))
            newer = (
                self._walk_supersession_chain(
                    str(superseded_by_pointer),
                    pointer_key="superseded_by",
                    resolver=resolver,
                    memory_visible=memory_visible,
                    seen={memory_id},
                    reveal_unresolved=not scope.active,
                )
                if superseded_by_pointer
                else []
            )
            older = (
                self._walk_supersession_chain(
                    str(supersedes_pointer),
                    pointer_key="supersedes",
                    resolver=resolver,
                    memory_visible=memory_visible,
                    seen={memory_id},
                    reveal_unresolved=not scope.active,
                )
                if supersedes_pointer
                else []
            )
            note_parts: list[str] = []
            if newer:
                note_parts.append(f"superseded by {len(newer)} newer revision(s)")
            if older:
                note_parts.append(f"supersedes {len(older)} older revision(s)")
            notes.append(
                {
                    "memory_id": memory_id,
                    "superseded_by": newer,
                    "supersedes": older,
                    "note": "; ".join(note_parts),
                }
            )
        return notes

    @staticmethod
    def _walk_supersession_chain(
        start_id: str,
        *,
        pointer_key: str,
        resolver: Callable[[str], JsonObject | None],
        memory_visible: Callable[[Mapping[str, object]], bool],
        seen: set[str],
        reveal_unresolved: bool = True,
    ) -> list[JsonObject]:
        chain: list[JsonObject] = []
        current: str | None = start_id
        while current and current not in seen and len(chain) < SUPERSESSION_CHAIN_HOP_LIMIT:
            seen.add(current)
            row = resolver(current)
            if row is None:
                # No such row. Not a hidden one: the lookup applies no fence.
                if reveal_unresolved:
                    chain.append({"id": current})
                break
            if not memory_visible(row):
                # A hidden revision is neither named nor titled, and the walk
                # stops here so nothing past it is named either.
                break
            chain.append(
                {
                    "id": current,
                    "title": _memory_title(row),
                    "memory_type": row.get("memory_type"),
                    "status": row.get("status"),
                }
            )
            next_pointer = row.get(pointer_key)
            current = str(next_pointer) if next_pointer else None
        return chain


__all__ = [
    "BUDGET_STRATEGIES",
    "BUDGET_STRATEGY_BALANCED",
    "BUDGET_STRATEGY_CONTRADICTIONS_FIRST",
    "BUDGET_STRATEGY_FACTS_FIRST",
    "BUDGET_STRATEGY_RECENT_FIRST",
    "BUDGET_STRATEGY_SECTION_ORDERS",
    "BUDGET_STRATEGY_SOURCES_FIRST",
    "PACK_VIEWS",
    "PACK_VIEW_FACTS",
    "PACK_VIEW_LOOP_CUES",
    "PACK_VIEW_LOOPS",
    "PACK_VIEW_SOURCE_CUES",
    "PACK_VIEW_SOURCES",
    "CONTEXT_DEPTHS",
    "CONTEXT_DEPTH_HIGH",
    "CONTEXT_DEPTH_LOW",
    "CONTEXT_DEPTH_MEDIUM",
    "CONTEXT_DEPTH_MINIMAL",
    "CONTEXT_DEPTH_MINIMAL_MAX_ITEMS",
    "CONTRADICTIONS_STAGE_ENABLED",
    "CONTRADICTIONS_STAGE_NOT_REQUESTED",
    "CONTRADICTIONS_STAGE_NO_STORE_SUPPORT",
    "DEFAULT_CONTEXT_PACK_LIMIT",
    "DEFAULT_RECENT_CHANGES_LIMIT",
    "DEFAULT_SENSITIVITY_ALLOWED",
    "FACTS_FIRST_MEMORY_TYPES",
    "ENTITY_NAME_CANDIDATE_LIMIT",
    "ENTITY_NAME_STOPWORDS",
    "EXCLUSION_REASON_TOKEN_BUDGET",
    "GRAPH_ENTITY_MATCH_LIMIT",
    "GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH",
    "GRAPH_STAGE_DISABLED_NO_STORE_SUPPORT",
    "GRAPH_STAGE_ENABLED",
    "LEGACY_SCOPED_SCAN_MAX_ROWS",
    "MAX_CONTEXT_PACK_ITEMS",
    "MAX_CONTEXT_PACK_TOKENS",
    "PROVENANCE_EXPAND_ONCE_MAX_TOKENS",
    "MAX_CONTEXT_SCOPE_VALUES",
    "MAX_TIME_WINDOW_DAYS",
    "MEMORY_ENTITY_EDGE_TYPES",
    "MEMORY_SEARCHABLE_STATUSES",
    "RRF_K",
    "SECTION_CONTRADICTING_EVIDENCE",
    "SECTION_OPEN_LOOPS",
    "SECTION_RELEVANT_MEMORIES",
    "SECTION_SOURCES",
    "SECTION_SUPPORTING_EVIDENCE",
    "SOURCES_STAGE_DISABLED_BY_FLAG",
    "SOURCE_CHUNK_CANDIDATE_MULTIPLIER",
    "SOURCE_CHUNK_STAGE_DISABLED_NO_STORE_SUPPORT",
    "SOURCE_EVENT_METADATA_KEYS",
    "SOURCE_STAGE_CHUNK_FTS",
    "SOURCE_STAGE_PROVENANCE",
    "SOURCE_STAGE_TEMPORAL",
    "SOURCE_STAGE_TITLE_RECENCY",
    "STAGE_DISABLED_MINIMAL",
    "STALENESS_NOTE_AFTER_DAYS",
    "SUPERSESSION_CHAIN_HOP_LIMIT",
    "SUPERSESSION_STAGE_ENABLED",
    "TEMPORAL_STAGE_DISABLED_NO_STORE_SUPPORT",
    "TEMPORAL_STAGE_ENABLED",
    "TIE_BREAK_CONTENT_STABLE",
    "TOKEN_ESTIMATE_CHARS_PER_TOKEN",
    "VALID_TO_UNBOUNDED_YEAR",
    "VECTOR_STAGE_DISABLED_NO_PROVIDER",
    "VECTOR_STAGE_DISABLED_NO_STORE_SUPPORT",
    "VECTOR_STAGE_DISABLED_QUERY_EMBEDDING_FAILED",
    "VECTOR_STAGE_ENABLED",
    "VNextRetrievalRequest",
    "VNextRetrievalCompletenessError",
    "VNextRetrievalService",
    "VNextRetrievalStore",
    "VNextRetrievalValidationError",
    "budget_section_order",
    "classify_pack_view",
    "classify_query",
    "content_stable_tiebreak",
    "entity_name_candidates",
    "estimate_item_tokens",
    "expand_provenance_once",
    "normalize_query",
    "query_terms",
    "reciprocal_rank_fusion",
]
