"""Roll-up cards: review-gated pre-aggregation of same-topic memories.

A roll-up card is ONE canonical memory whose text lists every same-topic
instance ("played — 5 instances: The Last of Us Part II (30 hours,
2023-05-10); ..."), so an aggregation-phrased recall ("summarize / count
everything about X") can hit a single card instead of needing every
instance to win a context-pack slot.

Position in the pipeline
------------------------
The roll-up pass rides the existing ``memory_consolidation`` scheduled
workflow (``VNextConsolidationService.generate_memory_consolidation``
invokes ``VNextRollupService.propose_rollups``). Nothing here runs on the
memory commit/capture path: a new memory arriving in a rolled-up topic
costs O(1) at commit time and is only picked up by the next scheduled
consolidation run, which then proposes a roll-up *revision*.

Review gate (Alice's brand, preserved)
--------------------------------------
Roll-up cards are created as ``status="candidate"`` memories carrying the
same ``metadata_json.consolidation`` block the near-duplicate proposals
use, so the existing review surface and
``VNextMemoryCommitService.accept_consolidation_candidate`` handle them
unchanged:

- ``proposal_kind`` is ``"rollup"`` and ``proposed_supersede`` is EMPTY
  for a first proposal: accepting promotes the card to a first-class
  ``active`` memory (embedding attached, entities linked, FTS/vector
  indexed like any memory) and supersedes NOTHING — members stay active
  and individually recallable. Member linkage lives in
  ``cluster_member_ids`` and per-instance records in
  ``value.rollup.instances`` (memory id, label, date, amounts, role).
- A *revision* proposal (new members arrived in an accepted roll-up's
  topic) sets ``proposed_supersede=[previous_card_id]``: accepting the
  revision retires only the outdated CARD, never the member memories.
- Candidates are invisible to retrieval (``search_memories*`` only match
  active/accepted) and to trusted-fact auto-promotion (which requires
  ``status == "active"``), so nothing auto-promotes past human review.

Deterministic grouping (no LLM required)
----------------------------------------
Two documented rules, both pure functions of the member rows:

- same-entity: members whose text mentions the same entity (via the
  deterministic ``vnext_entities`` extraction substrate, alias-folded
  through one bulk ``find_entities_by_names`` call when the store has an
  entity surface);
- lexical-topic: members sharing an anchor content token (stopword-
  filtered, lightly plural-folded) with support >= ``min_members``.

Semantic grouping tier (embeddings, dormant without a provider)
---------------------------------------------------------------
Aggregation topics whose instances share NO anchor token ("kitchen items
replaced" = faucet/toaster/shelves) are invisible to the lexical passes.
When an embedding provider is configured (the same ``vnext_embeddings``
env seam embed-on-write uses) AND the store carries stored vectors, a
third grouping pass runs over the members the entity/lexical passes left
unclaimed:

- embedding access reads exact presence by selected row ID BEFORE provider
  work, reuses compact vectors already derived by consolidation, and
  re-derives only present cache misses from ``memory_embedding_text``;
- remaining rows are agglomerated by cohesive all-pairs cosine admission at
  one threshold chosen from a conservative sweep
  (``SEMANTIC_SWEEP_THRESHOLDS``) by a silhouette-style internal
  criterion — deterministic given fixed vectors, ties break toward the
  HIGHER (more conservative) threshold;
- every semantic cluster passes the SAME group-utility gate as the
  lexical groups (>= 3 members plus an aggregation signal), with one
  documented substitution: group coherence requires mean- and
  minimum-pairwise-similarity floors instead of the label-stem
  majority test, because an anchor-less topical label cannot cover a
  majority of member texts by construction. Values/amounts for the
  aggregation signal are counted across the whole member texts (there is
  no shared label stem to be proximate to). Clusters at near-duplicate
  similarity are left to the dedup/merge pipeline;
- the card label is the dominant noun across the member texts (member
  support, then occurrences, then alphabetical — existing token
  machinery; verbs, closed-class words, and store-generic stems never
  label), and it must span at least two members; labels then run the
  same structural hygiene as every other card label.

Without a configured provider the tier is DORMANT and this module's
outputs are byte-identical to the lexical/entity-only behavior — no
metadata keys, no skip lines, no behavior change (guarded by tests).
When the tier runs, every decision (sweep scores, chosen threshold,
cluster counts, per-reason skips) is disclosed in the outcome metadata
and the consolidation report.

Groups whose member texts are near-identical (high mean pairwise token
Jaccard) are left to the near-duplicate dedup/merge pipeline — a roll-up
aggregates DISTINCT instances of one topic, it does not dedupe copies.

Label hygiene and the group-utility gate
----------------------------------------
A group only becomes a proposal if a human would recognize its card as a
topic that aggregates something. Everything below is deterministic and
store-local (measured from the grouped rows, never from a benchmark
label or an ambient corpus):

- structural label hygiene: pronoun/contraction labels ("I'm", "I've"),
  single letters, and labels made only of function words never label a
  card; a label's head token must be content-bearing — closed-class
  heads (adverbs/prepositions/conjunctions: "even", "still", "right",
  "towards") and light-verb heads without a noun ("add", "used",
  "incorporate") never head a card, and any other bare verb head needs
  a value-based aggregation signal (>= 2 distinct label-proximate
  amounts: "bought $120/$450" aggregates purchases; a bare verb with
  only session spread is plumbing). Entity labels that are broken
  subspans of a longer title ("Us Part II" inside "The Last of Us Part
  II") are repaired by expanding them to the dominant full span found
  in the member texts. Instance labels inside the card body get the
  same subspan repair and pronoun/fragment filtering, falling back to
  a neutral dominant-noun label so the value+date line is never lost.
- frequency-derived generic anchors: a stem that appears in a large
  fraction of the store's *sessions* is conversational plumbing ("need",
  "great", "help"), not a topic, no matter the language. The threshold
  is measured per store (session dispersion), so nothing here hardcodes
  an English vocabulary beyond the small structural lists above, and
  small stores (below the stats floor) skip the frequency test entirely.
- group-utility gate: a proposal needs >= 3 members AND an aggregation
  signal (>= 2 distinct instance values — amounts or in-text dates — or
  >= 3 distinct sessions), a label whose content words appear in a
  majority of member texts, and a label that is specific for the store.
  Groups failing the gate are DROPPED (they do not claim members and do
  not consume ``max_rollups`` slots), with one aggregate skip line
  documenting the counts per reason.
- ranking: surviving groups are proposed best-first by aggregation
  utility (distinct values x distinct sessions x label specificity), so
  the ``max_rollups`` cap keeps the strongest topics, not the first.

The optional model seam (``generation_mode="model_backed"`` through the
existing routing seam in ``vnext_model_intelligence``) only refines the
card's one-line summary; the deterministic instance list is always
present, complete, and correct on its own, and refusals fall back to the
deterministic card. No API key, endpoint, or model call is required for
any behavior in this module.

Storage: no new tables or columns. Cards reuse the memories table
(candidate rows + ``value``/``metadata_json`` JSON) and the existing
``memory_consolidation`` artifact/report, so no migration is needed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from inspect import Parameter, signature
import json
import logging
import re
from typing import Protocol

import numpy as np

from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_embeddings import (
    MAX_EMBEDDINGS_BATCH_SIZE,
    EmbeddingProvider,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    get_embedding_provider,
    memory_embedding_text,
)
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_entities import extract_entity_candidates
from alicebot_api.vnext_model_intelligence import (
    NON_SYNTHESIZING_PROVIDERS,
    BrainModelProvider,
    VNextModelIntelligenceError,
    provider_for_route,
)
from alicebot_api.vnext_memory_version import (
    memory_matches_snapshot,
    memory_version_snapshot,
)
from alicebot_api.vnext_project_scope import project_scope_identity, refuse_global_marker
from alicebot_api.vnext_recall_visibility import (
    drop_expired_memories,
    memory_window_is_open,
)
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_store import FTS_QUERY_STOPWORDS


logger = logging.getLogger(__name__)

ROLLUP_CANDIDATE_KIND = "memory_rollup"
ROLLUP_PROPOSAL_KIND = "rollup"
ROLLUP_SUMMARY_WORKFLOW = "rollup_summary"

DEFAULT_ROLLUP_MIN_MEMBERS = 3
DEFAULT_MAX_ROLLUPS = 8
DEFAULT_MAX_INSTANCES_PER_CARD = 40
MAX_GROUPABLE_MEMORIES_HARD_CAP = 5000

# Groups larger than this are skipped outright: a "topic" shared by
# hundreds of memories is a vocabulary artifact, not an aggregation
# subject, and its member list would bloat the candidate's metadata.
MAX_ROLLUP_GROUP_MEMBERS = 500

# Pairwise Jaccard for the duplicate-group guard is computed over at most
# this many members (deterministic prefix of the sorted group) so a huge
# group cannot turn the guard quadratic.
JACCARD_SAMPLE_MEMBERS = 20

# Mean pairwise token-set Jaccard at or above this marks a group as a
# near-duplicate cluster (same sentence restated), which the dedup/merge
# pipeline owns; roll-ups only aggregate distinct instances.
DUPLICATE_GROUP_JACCARD_THRESHOLD = 0.75

# Minimum token overlap between a model-refined summary and the member
# texts; below it the summary is refused as ungrounded (mirrors the
# consolidation-merge grounding guard).
ROLLUP_SUMMARY_GROUNDING_MIN_OVERLAP = 0.5

# -- group-utility gate thresholds ---------------------------------------------

# A roll-up aggregates; below three instances there is nothing to
# aggregate, whatever ``min_members`` is configured to.
MIN_AGGREGATION_MEMBERS = 3

# Aggregation signal: the group carries at least this many distinct
# instance values (currency/unit amounts or in-text dates) ...
MIN_DISTINCT_INSTANCE_VALUES = 2
# ... OR spans at least this many distinct sessions/days.
MIN_DISTINCT_SESSIONS = 3

# Every content word of a card's label must appear in at least this
# fraction of the member texts — the label has to describe the group.
LABEL_COHERENCE_MIN_FRACTION = 0.5

# -- semantic (embedding) tier thresholds ---------------------------------------

# Conservative cosine-similarity sweep for the semantic tier's
# cohesive all-pairs admission. One threshold is chosen per run by the
# silhouette-style criterion below; ties break toward the HIGHER
# threshold. The band deliberately stops below near-duplicate territory
# (the consolidation pass clusters near-duplicates at 0.88).
SEMANTIC_SWEEP_THRESHOLDS = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85)

# Coherence gate for semantic groups: mean and minimum pairwise cosine
# similarities must reach these floors. This REPLACES
# the label-stem majority test, which anchor-less groups cannot pass by
# construction — every other gate condition is shared with lexical groups.
SEMANTIC_MIN_MEAN_SIMILARITY = 0.60
SEMANTIC_MIN_PAIRWISE_SIMILARITY = 0.60

# Clusters whose mean pairwise similarity reaches near-duplicate territory
# belong to the consolidation dedup/merge pipeline (its default
# similarity_threshold is 0.88), not to a roll-up card.
SEMANTIC_NEAR_DUPLICATE_SIMILARITY = 0.88

# The semantic tier considers at most this many recent unclaimed rows and
# keeps temporary similarity work to fixed-height float32 blocks.
MAX_SEMANTIC_TIER_MEMBERS = 2000
SEMANTIC_SIMILARITY_BLOCK_ROWS = 128

# Frequency-derived generic-anchor detection (store-local, no hardcoded
# vocabulary): a stem that appears in at least this fraction of the
# store's distinct sessions is conversational plumbing, not a topic.
# Calibrated on real conversational stores, where plumbing stems
# ("need", "great", "help", "i'm") disperse across 32%-84% of sessions
# while genuine topics ("workshops", "games", "plants") stay under ~30%
# even when their raw memory counts are higher.
GENERIC_ANCHOR_SESSION_DISPERSION = 0.30

# Dispersion statistics are meaningless on tiny corpora: below these
# floors no stem is ever frequency-blocked (structural hygiene still
# applies). Small personal stores therefore keep full grouping power.
GENERIC_ANCHOR_MIN_ROWS = 30
GENERIC_ANCHOR_MIN_SESSIONS = 10

# Topic tokens that are conversational plumbing rather than topics; the
# speaker tags cover LongMemEval-style "[USER]: ..." transcripts, and
# "ai" covers assistant self-reference ("As an AI ...") — in a store
# built from assistant chats that token labels boilerplate, not a topic
# (specific AI subjects still surface through their own entity/anchor
# labels: product names, "machine learning", model names, ...).
_TOPIC_TOKEN_BLOCKLIST = frozenset(
    {
        "user", "assistant", "alice", "memory", "memories", "session",
        "chat", "remember", "today", "yesterday", "tomorrow", "really",
        "recently", "think", "thanks", "thank", "please", "would", "like",
        "want", "wanted", "going", "got", "get", "also", "one", "two",
        "new", "time", "day", "lot", "bit", "yes", "yeah", "okay", "ok", "ai",
    }
)

# Closed-class contraction heads: a label token like "i'm"/"you'd" whose
# head is one of these is a pronoun contraction, never a topic. Structural
# (grammar, not vocabulary), so it applies at any store size.
_PRONOUN_CONTRACTION_HEADS = frozenset(
    {"i", "you", "he", "she", "it", "we", "they", "that", "there", "this",
     "who", "what", "let", "here"}
)

# Closed-class label heads: adverbs, prepositions, conjunctions, and
# discourse particles that slip past ``FTS_QUERY_STOPWORDS`` (which only
# covers query plumbing). Function words are a finite class — unlike
# topic vocabulary this list can be curated once — and none of them can
# name what a card aggregates ("Roll-up: even — 15 instances"), however
# strong the group's value signal is. Structural, so it applies at any
# store size.
_CLOSED_CLASS_LABEL_HEADS = frozenset(
    {
        # adverbs / discourse particles
        "even", "still", "right", "already", "almost", "always", "anyway",
        "aside", "away", "back", "certainly", "currently", "definitely",
        "especially", "eventually", "finally", "furthermore", "generally",
        "however", "indeed", "instead", "later", "maybe", "meanwhile",
        "moreover", "never", "often", "otherwise", "perhaps", "probably",
        "quite", "rather", "somehow", "sometimes", "somewhat", "soon",
        "specifically", "together", "typically", "usually",
        # prepositions not already in the query stopword list
        "across", "along", "among", "amongst", "around", "behind", "beneath",
        "beside", "besides", "beyond", "despite", "except", "inside", "near",
        "onto", "outside", "past", "since", "throughout", "till", "toward",
        "towards", "underneath", "until", "unto", "upon", "via", "within",
        "without",
        # conjunctions
        "although", "though", "unless", "whereas", "whether", "yet",
    }
)

# Light / grammaticalized verb forms: verbs so bleached ("add", "used",
# "incorporate", "got", "made") that a card headed by one aggregates
# nothing a human would recognize, EVEN when its instances carry amounts
# ("Roll-up: add — 16 instances, amounts in minutes" was cooking-step
# plumbing, not an aggregation topic). Like the closed-class list this is
# a small curated set of function-word-adjacent forms, not an open topic
# vocabulary. A light-verb head is only acceptable when a noun in the
# label carries the topic ("add" alone never; "decided meridian" fine).
_LIGHT_VERB_FORMS = frozenset(
    {
        "add", "adds", "use", "uses", "used", "incorporate", "incorporates",
        "get", "gets", "gotten", "make", "makes", "made", "take", "takes",
        "took", "taken", "put", "puts", "give", "gives", "gave", "given",
        "go", "goes", "went", "gone", "come", "comes", "came", "keep",
        "keeps", "kept", "bring", "brings", "brought", "need", "needs",
        "know", "knows", "knew", "known", "say", "says", "said", "tell",
        "tells", "told", "see", "sees", "seen", "find", "finds",
        "think", "thinks", "thought",
    }
)

# Transaction verbs whose bare form only means something as an
# aggregation over amounts: "bought $120 / $450" aggregates purchases,
# but "bought" with nothing to sum is plumbing. Irregular pasts listed
# explicitly because the ``-ed`` morphology test below cannot see them.
# Deliberately NOT a general verb list: contentful activity verbs
# ("flew", "hiked" via -ed) name a topic by themselves and are handled
# by the same value test only when morphology already marks them.
_TRANSACTION_VERB_FORMS = frozenset(
    {"bought", "sold", "paid", "spent"}
)


def _is_verb_form(token: str) -> bool:
    """Verb-shaped label token, detected without NLP: curated light-verb
    and transaction-verb forms plus the regular past/participle ``-ed``
    morphology (``-eed`` nouns like "speed"/"seed" excluded). ``-ing``
    forms are deliberately NOT verb-shaped: as label tokens gerunds act
    as nouns ("reading", "playing")."""
    if token in _LIGHT_VERB_FORMS or token in _TRANSACTION_VERB_FORMS:
        return True
    return len(token) >= 4 and token.endswith("ed") and not token.endswith("eed")

# Lowercase connectors that may sit INSIDE a capitalized title span
# ("The Last of Us Part II", "Lord of the Rings"); used when repairing
# entity labels that extraction truncated at such a connector.
_TITLE_CONNECTOR_TOKENS = frozenset(
    {"of", "the", "and", "for", "de", "la", "le", "du", "da", "di", "del",
     "van", "von", "&"}
)

# Month names count as in-text date values for the aggregation-signal
# test; only capitalized surfaces are counted so modal "may" stays out.
_MONTH_TOKENS = frozenset(
    {"january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"}
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")
_SURFACE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")
# Word tokens with positions, for the label-repair span walk.
_WORD_WITH_POS_RE = re.compile(r"[A-Za-z0-9][\w'’&-]*")
# Numeric calendar dates ("2023-05-10", "05/10", "5/10/2023").
_DATE_MENTION_RE = re.compile(
    r"(?<![\d/-])(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)(?![\d/-])"
)
# Sentence-ish boundaries for label-proximate value counting.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n;•|]+")
# Quantities worth carrying per instance: currency-prefixed numbers and
# numbers followed by a small unit vocabulary. Bare numbers are too noisy.
_AMOUNT_RE = re.compile(
    r"(?<![\w.])("
    r"[$€£]\s?\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:hours?|hrs?|h\b|minutes?|mins?|days?|weeks?|months?|years?|"
    r"km|kilometers?|miles?|mi\b|kg|kilograms?|lbs?|pounds?|dollars?|usd|eur|gbp|percent|%|times)"
    r")",
    re.IGNORECASE,
)
_SPEAKER_TAG_RE = re.compile(r"^\s*\[(USER|ASSISTANT)\]\s*:?", re.IGNORECASE)


class VNextRollupValidationError(ValueError):
    """Raised when roll-up options are invalid."""


class VNextRollupStore(Protocol):
    """Store surface the roll-up pass needs. ``find_entities_by_names`` is
    optional (checked via ``getattr``); without it entity grouping falls
    back to the raw extracted names (no alias folding). Exact embedding
    presence is discovered through ``list_memory_ids_with_embeddings`` when
    the semantic tier is active."""

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def list_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        limit: int,
        projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def list_pending_rollup_candidates(
        self,
        *,
        rollup_digests: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
        projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def list_accepted_rollup_cards(
        self,
        *,
        rollup_keys: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
        projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def count_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        projects: tuple[str, ...] = (),
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RollupOptions:
    """Knobs for the roll-up pass, overridable via
    ``metadata_json["rollup_options"]`` on the consolidation request."""

    min_members: int = DEFAULT_ROLLUP_MIN_MEMBERS
    max_rollups: int = DEFAULT_MAX_ROLLUPS
    max_instances_per_card: int = DEFAULT_MAX_INSTANCES_PER_CARD
    max_groupable_memories: int = MAX_GROUPABLE_MEMORIES_HARD_CAP

    @classmethod
    def from_metadata(cls, metadata_json: object) -> RollupOptions:
        overrides = metadata_json.get("rollup_options") if isinstance(metadata_json, dict) else None
        overrides = overrides if isinstance(overrides, dict) else {}

        def _int(key: str, default: int) -> int:
            value = overrides.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise VNextRollupValidationError(f"rollup_options.{key} must be an integer")
            return value

        options = cls(
            min_members=_int("min_members", DEFAULT_ROLLUP_MIN_MEMBERS),
            max_rollups=_int("max_rollups", DEFAULT_MAX_ROLLUPS),
            max_instances_per_card=_int("max_instances_per_card", DEFAULT_MAX_INSTANCES_PER_CARD),
            max_groupable_memories=_int("max_groupable_memories", MAX_GROUPABLE_MEMORIES_HARD_CAP),
        )
        if not (2 <= options.min_members <= 100):
            raise VNextRollupValidationError("rollup_options.min_members must be between 2 and 100")
        if not (1 <= options.max_rollups <= 50):
            raise VNextRollupValidationError("rollup_options.max_rollups must be between 1 and 50")
        if not (options.min_members <= options.max_instances_per_card <= 500):
            raise VNextRollupValidationError(
                "rollup_options.max_instances_per_card must be between min_members and 500"
            )
        if not (2 <= options.max_groupable_memories <= MAX_GROUPABLE_MEMORIES_HARD_CAP):
            raise VNextRollupValidationError(
                f"rollup_options.max_groupable_memories must be between 2 and {MAX_GROUPABLE_MEMORIES_HARD_CAP}"
            )
        return options

    def to_record(self) -> JsonObject:
        return {
            "min_members": self.min_members,
            "max_rollups": self.max_rollups,
            "max_instances_per_card": self.max_instances_per_card,
            "max_groupable_memories": self.max_groupable_memories,
        }


@dataclass(frozen=True, slots=True)
class _RollupGroup:
    rollup_key: str
    group_kind: str  # "entity" | "topic" | "semantic"
    label: str
    members: tuple[JsonObject, ...]
    utility: _GroupUtility
    # Runner-up surface forms of the label stems ("plants" next to
    # "plant"); rendered on the card head so exact-token retrieval matches
    # whichever inflection the query uses.
    label_variants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PreparedRollupGroup:
    """Deterministic group identity prepared before bounded state reads."""

    group: _RollupGroup
    member_ids: tuple[str, ...]
    current_member_snapshots: tuple[JsonObject, ...]
    rollup_digest: str
    group_record: JsonObject


@dataclass(slots=True)
class RollupOutcome:
    """JSON-safe result of one roll-up pass, embedded in the consolidation
    artifact's metadata and rendered as a report section."""

    options: JsonObject = field(default_factory=dict)
    groups: list[JsonObject] = field(default_factory=list)
    # Internal evidence for the parent report label, never serialized. Every row
    # the pass writes into the report is listed here once: the members of the
    # groups it proposes, the members of the groups a skip line names by key, and
    # the cards an earlier pass made for the same topic. The report is read behind
    # a label taken over these rows, so a row left out is a row the label ignores.
    input_rows: list[JsonObject] = field(default_factory=list)
    _input_ids: set[str] = field(default_factory=set, repr=False)
    proposals: list[JsonObject] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    groupable_count: int = 0
    groupable_total_count: int = 0
    groupable_total_exact: bool = True
    bounded: bool = False
    quality_gate: JsonObject = field(default_factory=dict)
    # Disclosure record of the semantic embedding tier; None when the tier
    # is dormant (no embedding provider), keeping the metadata below
    # byte-identical to the lexical/entity-only shape.
    semantic: JsonObject | None = None

    def name_rows(self, rows: Iterable[JsonObject]) -> None:
        """Record that the report names ``rows``. A row counts once, whatever number of lines name it."""

        for row in rows:
            if row.get("id") is None:
                # A row with no id cannot be told from another, so it is kept rather than merged into one.
                self.input_rows.append(row)
                continue
            row_id = str(row["id"])
            if row_id not in self._input_ids:
                self._input_ids.add(row_id)
                self.input_rows.append(row)

    def to_metadata(self) -> JsonObject:
        metadata: JsonObject = {
            "grouping": "deterministic_entity_and_lexical_topic",
            "options": dict(self.options),
            "groupable_memories": self.groupable_count,
            "groupable_memories_total": self.groupable_total_count,
            "groupable_memories_total_exact": self.groupable_total_exact,
            "bounded": self.bounded,
            "groups": list(self.groups),
            "proposals": list(self.proposals),
            "skipped": list(self.skipped),
            "quality_gate": dict(self.quality_gate),
        }
        if self.semantic is not None:
            metadata["grouping"] = "deterministic_entity_and_lexical_topic_plus_semantic_embedding"
            metadata["semantic_grouping"] = dict(self.semantic)
        return metadata

    def markdown_lines(self) -> list[str]:
        lines: list[str] = []
        for proposal in self.proposals:
            proposed_supersede = proposal.get("proposed_supersede")
            supersede_items = proposed_supersede if isinstance(proposed_supersede, list) else []
            supersede = ", ".join(str(item) for item in supersede_items) or "none"
            lines.append(
                f"- `{proposal['rollup_digest']}` roll-up proposal "
                f"({proposal['candidate_state']}, candidate: {proposal['candidate_memory_id']}) - "
                f"topic: {proposal['label']} ({proposal['group_kind']}); "
                f"{proposal['instance_count']} instances; members stay active; "
                f"proposed supersede after acceptance: {supersede}"
                + (
                    f"; revises accepted roll-up {proposal['revises_memory_id']}"
                    if proposal.get("revises_memory_id")
                    else ""
                )
            )
        if not lines:
            lines = ["- No roll-up proposals were created this run."]
        if self.semantic is not None:
            lines.append(
                "- Semantic tier (embeddings): "
                f"{self.semantic.get('groups_admitted', 0)} group(s) admitted from "
                f"{self.semantic.get('clusters_formed', 0)} cluster(s); chosen threshold "
                f"{self.semantic.get('chosen_threshold')} (mean silhouette "
                f"{self.semantic.get('mean_silhouette')})."
            )
        for reason in self.skipped:
            lines.append(f"- Skipped: {reason}")
        return lines


# -- small deterministic helpers -----------------------------------------------


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256(encoded).hexdigest()[:16]


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _member_text(row: JsonObject) -> str:
    for key in ("canonical_text", "summary", "title", "memory_key"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    value = row.get("value")
    if isinstance(value, dict):
        raw = value.get("text")
        if isinstance(raw, str) and raw.strip():
            return " ".join(raw.split())
    return str(row.get("id", "item"))


def _project_scope_key(row: JsonObject) -> tuple[str, ...]:
    return project_scope_identity(resource_project_scope(row))


def _shared_project_scope(rows: tuple[JsonObject, ...] | list[JsonObject]) -> tuple[str, ...]:
    if not rows:
        return ()
    first = resource_project_scope(rows[0])
    first_key = _project_scope_key(rows[0])
    if all(_project_scope_key(row) == first_key for row in rows[1:]):
        return first
    return ()


def _light_stem(token: str) -> str:
    """Deliberately crude plural fold used ONLY for grouping keys (never
    displayed): 'games' and 'game' share one topic anchor."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _topic_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text.casefold()):
        # A topic token needs at least one letter: bare numbers and dates
        # ("2023-06-02") are instance data, not aggregation subjects.
        if len(raw) < 3 or not any(char.isalpha() for char in raw):
            continue
        if raw in FTS_QUERY_STOPWORDS or raw in _TOPIC_TOKEN_BLOCKLIST:
            continue
        stemmed = _light_stem(raw)
        if stemmed in FTS_QUERY_STOPWORDS or stemmed in _TOPIC_TOKEN_BLOCKLIST:
            continue
        tokens.add(stemmed)
    return tokens


def _surface_counts(members: tuple[JsonObject, ...], anchor: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for member in members:
        for raw in _SURFACE_TOKEN_RE.findall(_member_text(member)):
            lowered = raw.casefold()
            if _light_stem(lowered) == anchor:
                counts[lowered] += 1
    return counts


def _surface_form(members: tuple[JsonObject, ...], anchor: str) -> str:
    """Most frequent original surface form whose light stem equals
    ``anchor`` (ties break alphabetically) — labels show real words."""
    counts = _surface_counts(members, anchor)
    if not counts:
        return anchor
    return min(counts, key=lambda token: (-counts[token], token))


def _surface_variants(members: tuple[JsonObject, ...], anchors: list[str], label: str) -> tuple[str, ...]:
    """Runner-up surface forms of the label's stems ("plants" next to
    "plant"), so the card also carries the inflection the members — and an
    aggregation query — actually use. At most one variant per stem."""
    label_tokens = set(_TOKEN_RE.findall(label.casefold()))
    variants: list[str] = []
    for anchor in anchors:
        counts = _surface_counts(members, anchor)
        for surface in sorted(counts, key=lambda token: (-counts[token], token)):
            if surface not in label_tokens and surface not in variants:
                variants.append(surface)
                break
    return tuple(variants)


def _mean_pairwise_jaccard(token_sets: list[set[str]]) -> float:
    pairs: list[float] = []
    for index, left in enumerate(token_sets):
        for right in token_sets[index + 1 :]:
            union = left | right
            pairs.append(len(left & right) / len(union) if union else 1.0)
    return sum(pairs) / len(pairs) if pairs else 0.0


# -- semantic tier: clustering math (deterministic given fixed vectors) ----------


def _cohesive_labels(
    normalized: np.ndarray,
    stable_keys: list[str],
    threshold: float,
) -> list[int]:
    """Complete-link labels with streaming all-pairs admission.

    A row joins a group only when it clears the threshold against every
    member already admitted, so bridge chains cannot create a loose group.
    Stable-key ordering makes the deterministic greedy partition independent
    of input order without materializing an N x N similarity matrix.
    """

    remaining = sorted(range(len(stable_keys)), key=stable_keys.__getitem__)
    labels = [-1] * len(stable_keys)
    while remaining:
        seed, *candidates = remaining
        group = [seed]
        rejected: list[int] = []
        for candidate in candidates:
            clears_group = True
            for start in range(0, len(group), SEMANTIC_SIMILARITY_BLOCK_ROWS):
                block = group[start : start + SEMANTIC_SIMILARITY_BLOCK_ROWS]
                similarities = normalized[block] @ normalized[candidate]
                if not bool(np.all(similarities >= threshold)):
                    clears_group = False
                    break
            if clears_group:
                group.append(candidate)
            else:
                rejected.append(candidate)
        label = min(group)
        for index in group:
            labels[index] = label
        remaining = rejected
    return labels


def _mean_silhouette(normalized: np.ndarray, labels: list[int]) -> float | None:
    """Silhouette-style internal criterion over cosine distance
    (``1 - similarity``): per point, ``a`` = mean distance to its own
    cluster, ``b`` = smallest mean distance to another cluster,
    ``s = (b - a) / max(a, b)``; singleton clusters contribute 0 (the
    standard convention). Returns the mean over all points, or None when
    the partition has fewer than two clusters (the criterion is undefined
    there). Similarities are computed in fixed float32 row blocks, so peak
    temporary memory is O(block_rows * N)."""
    unique = sorted(set(labels))
    if len(unique) < 2:
        return None
    count = len(labels)
    members_by_label: dict[int, np.ndarray] = {
        label: np.asarray([index for index, value in enumerate(labels) if value == label], dtype=np.intp)
        for label in unique
    }
    score_total = 0.0
    for start in range(0, count, SEMANTIC_SIMILARITY_BLOCK_ROWS):
        stop = min(count, start + SEMANTIC_SIMILARITY_BLOCK_ROWS)
        similarities = normalized[start:stop] @ normalized.T
        distances = 1.0 - similarities
        for local_index, row_index in enumerate(range(start, stop)):
            own_label = labels[row_index]
            own_members = members_by_label[own_label]
            if len(own_members) == 1:
                continue
            a = float(distances[local_index, own_members].sum(dtype=np.float64)) / (
                len(own_members) - 1
            )
            b = min(
                float(distances[local_index, members].mean(dtype=np.float64))
                for label, members in members_by_label.items()
                if label != own_label
            )
            denominator = max(a, b)
            if denominator > 0.0:
                score_total += (b - a) / denominator
        del similarities, distances
    return score_total / count


def _semantic_pair_stats(normalized: np.ndarray, indices: list[int]) -> tuple[float, float]:
    """Return exact (mean, minimum) cosine with O(cluster-size) scratch."""

    pair_count = 0
    pair_total = 0.0
    pair_min = float("inf")
    for position, left in enumerate(indices[:-1]):
        values = normalized[indices[position + 1 :]] @ normalized[left]
        if values.size == 0:
            continue
        pair_count += int(values.size)
        pair_total += float(values.sum(dtype=np.float64))
        pair_min = min(pair_min, float(values.min()))
    return pair_total / pair_count, pair_min


def _member_date(row: JsonObject) -> str | None:
    """Best-effort per-instance date: explicit content dates first
    (``metadata_json.session_date``, ``value.date``), then row event time."""
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        session_date = metadata.get("session_date")
        if isinstance(session_date, str) and len(session_date) >= 8:
            return session_date[:10]
    value = row.get("value")
    if isinstance(value, dict):
        content_date = value.get("date")
        if isinstance(content_date, str) and len(content_date) >= 8:
            return content_date[:10]
    for key in ("first_seen_at", "created_at", "last_seen_at"):
        stamp = row.get(key)
        if isinstance(stamp, datetime):
            return stamp.date().isoformat()
        if isinstance(stamp, date):
            return stamp.isoformat()
        if isinstance(stamp, str) and len(stamp) >= 10:
            return stamp[:10]
    return None


def _member_role(row: JsonObject) -> str | None:
    """USER/ASSISTANT provenance when the member carries it (metadata keys
    or a leading speaker tag in the text)."""
    metadata = row.get("metadata_json")
    for container in (metadata, row.get("value")):
        if isinstance(container, dict):
            for key in ("speaker", "speaker_role", "role"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().upper()
    for key in ("canonical_text", "title"):
        text = row.get(key)
        if isinstance(text, str):
            match = _SPEAKER_TAG_RE.match(text)
            if match:
                return match.group(1).upper()
    value = row.get("value")
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        match = _SPEAKER_TAG_RE.match(value["text"])
        if match:
            return match.group(1).upper()
    return None


def _member_amounts(text: str, *, limit: int = 4) -> list[str]:
    amounts: list[str] = []
    for match in _AMOUNT_RE.finditer(text):
        surface = " ".join(match.group(1).split())
        if surface not in amounts:
            amounts.append(surface)
        if len(amounts) >= limit:
            break
    return amounts


def _strip_speaker_tag(text: str) -> str:
    return _SPEAKER_TAG_RE.sub("", text, count=1).strip()


# -- label hygiene & the group-utility gate --------------------------------------


def _stats_tokens(text: str) -> set[str]:
    """Stemmed content tokens for corpus statistics and coherence checks.

    Wider than ``_topic_tokens`` (length >= 2, so acronym entity labels
    like "AI" resolve to a measured stem) but the same stopword filter,
    so plumbing stems and topic stems live in one vocabulary."""
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text.casefold()):
        if len(raw) < 2 or not any(char.isalpha() for char in raw):
            continue
        if raw in FTS_QUERY_STOPWORDS or raw in _TOPIC_TOKEN_BLOCKLIST:
            continue
        tokens.add(_light_stem(raw))
    return tokens


def _member_session_key(row: JsonObject) -> str | None:
    """Best-effort per-member occasion key: source/session provenance when
    the store carries it, else the member's content/event date. Only
    DISTINCTNESS matters (the gate and the dispersion statistics count
    occasions); the key never reaches a card."""
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        for key in ("source_id", "session_id", "session_date"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return f"{key}:{value.strip()}"
    member_date = _member_date(row)
    return f"date:{member_date}" if member_date else None


def _member_instance_values(text: str) -> frozenset[str]:
    """Distinct aggregation values carried by one member text: unit/currency
    amounts plus in-text dates (numeric or capitalized month names)."""
    values: set[str] = set()
    for amount in _member_amounts(text, limit=8):
        values.add(" ".join(amount.casefold().split()))
    for match in _DATE_MENTION_RE.finditer(text):
        values.add(match.group(1))
    for token in _SURFACE_TOKEN_RE.findall(text):
        if token[0].isupper() and token.casefold() in _MONTH_TOKENS:
            values.add(token.casefold())
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class _RowProfile:
    stems: frozenset[str]
    session_key: str | None
    # (sentence stems, sentence values, sentence amounts) per sentence, so
    # a group can count the values that sit in the SAME sentence as one of
    # its label stems — a card about hours played aggregates the amounts
    # attached to playing, not every number a long member text happens to
    # mention elsewhere. Amounts (currency/unit quantities) are carried
    # separately from the wider value set (which adds in-text dates)
    # because the bare-verb label rule keys on amounts specifically.
    sentences: tuple[tuple[frozenset[str], frozenset[str], frozenset[str]], ...]


def _row_profiles(rows: list[JsonObject]) -> dict[str, _RowProfile]:
    profiles: dict[str, _RowProfile] = {}
    for row in rows:
        text = _strip_speaker_tag(_member_text(row))
        sentences: list[tuple[frozenset[str], frozenset[str], frozenset[str]]] = []
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            if not sentence.strip():
                continue
            values = _member_instance_values(sentence)
            if values:
                amounts = frozenset(
                    " ".join(amount.casefold().split())
                    for amount in _member_amounts(sentence, limit=8)
                )
                sentences.append((frozenset(_stats_tokens(sentence)), values, amounts))
        profiles[str(row.get("id"))] = _RowProfile(
            stems=frozenset(_stats_tokens(text)),
            session_key=_member_session_key(row),
            sentences=tuple(sentences),
        )
    return profiles


class _CorpusStats:
    """Store-local session-dispersion statistics for anchor stems.

    ``dispersion(stem)`` is the fraction of the store's distinct sessions
    whose memories contain the stem. Conversational plumbing ("need",
    "great", "i'm") disperses across most sessions; genuine topics
    concentrate in a few. Below the row/session floors the statistics are
    disabled and nothing is frequency-blocked."""

    __slots__ = ("enabled", "session_count", "_stem_session_counts")

    def __init__(self, profiles: dict[str, _RowProfile]) -> None:
        stem_sessions: dict[str, set[str]] = {}
        sessions: set[str] = set()
        for profile in profiles.values():
            if profile.session_key is None:
                continue
            sessions.add(profile.session_key)
            for stem in profile.stems:
                stem_sessions.setdefault(stem, set()).add(profile.session_key)
        self.session_count = len(sessions)
        self.enabled = (
            len(profiles) >= GENERIC_ANCHOR_MIN_ROWS
            and self.session_count >= GENERIC_ANCHOR_MIN_SESSIONS
        )
        self._stem_session_counts = (
            {stem: len(keys) for stem, keys in stem_sessions.items()} if self.enabled else {}
        )

    def dispersion(self, stem: str) -> float:
        if not self.enabled or self.session_count == 0:
            return 0.0
        return self._stem_session_counts.get(stem, 0) / self.session_count

    def is_generic(self, stem: str) -> bool:
        return self.dispersion(stem) >= GENERIC_ANCHOR_SESSION_DISPERSION


def _label_content_tokens(label: str) -> list[str]:
    """Casefolded content tokens of a label: pronoun contractions, single
    letters, stopwords, and bare numbers do not count as content."""
    content: list[str] = []
    for token in _TOKEN_RE.findall(label.replace("’", "'").casefold()):
        head = token.split("'", 1)[0]
        if "'" in token and head in _PRONOUN_CONTRACTION_HEADS:
            continue
        if token in _PRONOUN_CONTRACTION_HEADS:
            continue
        if len(token) < 2 or not any(char.isalpha() for char in token):
            continue
        if token in FTS_QUERY_STOPWORDS or token in _TOPIC_TOKEN_BLOCKLIST:
            continue
        content.append(token)
    return content


def _label_junk_reason(
    label: str,
    stats: _CorpusStats,
    *,
    label_amount_count: int | None = None,
) -> str | None:
    """Why the label can never head a card, or None if it is presentable.

    The head (first content token) must be content-bearing:

    - a closed-class head (adverb/preposition/conjunction: "even",
      "still", "right", "towards") never labels a card, whatever the
      group's value signal;
    - a light-verb head ("add", "used", "incorporate") needs a noun in
      the label to carry the topic — amounts do not rescue it, because
      light verbs attract incidental quantities ("add ... 10 minutes");
    - any other verb-shaped head (regular ``-ed`` forms, transaction
      verbs like "bought") without an accompanying noun is acceptable
      only when the group aggregates values: at least
      ``MIN_DISTINCT_INSTANCE_VALUES`` distinct label-proximate amounts
      ("bought $120/$450" aggregates purchases; a bare verb with only
      session-dispersion signal is plumbing). ``label_amount_count`` is
      that measured count; ``None`` (label-only callers with no group in
      hand) skips the value-dependent rule.
    """
    content = _label_content_tokens(label)
    if not content:
        return "label_without_content_words"
    head = content[0]
    if head in _CLOSED_CLASS_LABEL_HEADS:
        return "label_head_closed_class"
    if _is_verb_form(head):
        has_noun = any(
            token not in _CLOSED_CLASS_LABEL_HEADS and not _is_verb_form(token)
            for token in content[1:]
        )
        if not has_noun:
            if head in _LIGHT_VERB_FORMS:
                return "label_head_light_verb"
            if (
                label_amount_count is not None
                and label_amount_count < MIN_DISTINCT_INSTANCE_VALUES
            ):
                return "label_bare_verb_without_values"
    stems = [_light_stem(token) for token in content]
    if stats.enabled and min(stats.dispersion(stem) for stem in stems) >= GENERIC_ANCHOR_SESSION_DISPERSION:
        return "label_generic_for_store"
    return None


def _label_specificity(label: str, stats: _CorpusStats) -> float:
    """1 - session dispersion of the label's most specific content stem
    (1.0 when statistics are disabled or the label has no measured stem)."""
    stems = [_light_stem(token) for token in _label_content_tokens(label)]
    if not stems:
        return 0.0
    return 1.0 - min(stats.dispersion(stem) for stem in stems)


def _natural_surface_order(members: tuple[JsonObject, ...], surfaces: list[str]) -> list[str]:
    """Order a two-word topic label the way the member texts say it
    ("credit card", not "card credit"); ties keep the support order."""
    if len(surfaces) != 2:
        return surfaces
    first, second = surfaces
    forward = backward = 0
    for member in members[:JACCARD_SAMPLE_MEMBERS]:
        text = " ".join(_member_text(member).casefold().split())
        forward += text.count(f"{first} {second}")
        backward += text.count(f"{second} {first}")
    if backward > forward:
        return [second, first]
    return surfaces


def _label_coherence(label: str, member_profiles: list[_RowProfile]) -> float:
    """Minimum member-coverage across the label's content stems: every
    content word of the label must describe most of the group."""
    stems = [_light_stem(token) for token in _label_content_tokens(label)]
    if not stems or not member_profiles:
        return 0.0
    coverage = []
    for stem in stems:
        hits = sum(1 for profile in member_profiles if stem in profile.stems)
        coverage.append(hits / len(member_profiles))
    return min(coverage)


def _expanded_entity_label(label: str, members: tuple[JsonObject, ...]) -> str:
    """Repair entity labels that are broken subspans of a longer title.

    Extraction can truncate "The Last of Us Part II" to "Us Part II" (the
    lowercase connector splits the capitalized span). Walking each label
    occurrence leftward through capitalized words and title connectors in
    the member texts recovers the full span; when a strict majority of
    occurrences sit inside such a longer span, the label becomes the most
    common expansion. Deterministic; bounded by the same member sample the
    duplicate-group guard uses."""
    label_tokens = label.split()
    if not label_tokens:
        return label
    expansions: Counter[str] = Counter()
    occurrences = 0
    for member in members[:JACCARD_SAMPLE_MEMBERS]:
        text = _strip_speaker_tag(_member_text(member))
        tokens = [(match.group(), match.start(), match.end()) for match in _WORD_WITH_POS_RE.finditer(text)]
        for index in range(len(tokens) - len(label_tokens) + 1):
            window = tokens[index : index + len(label_tokens)]
            if [token[0] for token in window] != label_tokens:
                continue
            if any(
                text[window[position][2] : window[position + 1][1]].strip()
                for position in range(len(window) - 1)
            ):
                continue  # punctuation inside the window: not this span
            occurrences += 1
            start = index

            def _title_word(word: str) -> bool:
                # A capitalized word can extend the span, but pronoun
                # contractions ("I'm thinking ...") are sentence subjects,
                # not truncated title material: compare the head before
                # the apostrophe, not just the whole token.
                head = word.casefold().replace("’", "'").split("'", 1)[0]
                return word[0].isupper() and head not in _PRONOUN_CONTRACTION_HEADS

            while start > 0:
                previous = tokens[start - 1]
                if text[previous[2] : tokens[start][1]].strip():
                    break  # sentence punctuation between words: stop
                word = previous[0]
                if _title_word(word):
                    start -= 1
                    continue
                if word.casefold() in _TITLE_CONNECTOR_TOKENS and start - 1 > 0:
                    before = tokens[start - 2]
                    if not text[before[2] : previous[1]].strip() and _title_word(before[0]):
                        start -= 2
                        continue
                break
            if start < index:
                expansions[" ".join(token[0] for token in tokens[start : index + len(label_tokens)])] += 1
    if occurrences and sum(expansions.values()) * 2 > occurrences:
        return min(expansions, key=lambda name: (-expansions[name], name))
    return label


@dataclass(frozen=True, slots=True)
class _GroupUtility:
    distinct_values: int
    # Label-proximate currency/unit amounts only (a subset of
    # ``distinct_values``, which also counts in-text dates); the
    # bare-verb label rule keys on this.
    distinct_amounts: int
    distinct_sessions: int
    label_specificity: float
    label_coherence: float
    score: float
    # Mean pairwise cosine similarity for SEMANTIC groups; None for
    # lexical/entity groups, whose record shape is unchanged.
    semantic_mean_similarity: float | None = None
    semantic_min_similarity: float | None = None

    def to_record(self) -> JsonObject:
        record: JsonObject = {
            "distinct_values": self.distinct_values,
            "distinct_amounts": self.distinct_amounts,
            "distinct_sessions": self.distinct_sessions,
            "label_specificity": round(self.label_specificity, 4),
            "label_coherence": round(self.label_coherence, 4),
            "score": round(self.score, 4),
        }
        if self.semantic_mean_similarity is not None:
            record["semantic_mean_similarity"] = round(self.semantic_mean_similarity, 4)
        if self.semantic_min_similarity is not None:
            record["semantic_min_similarity"] = round(self.semantic_min_similarity, 4)
        return record


def _group_utility(
    label: str,
    members: tuple[JsonObject, ...] | list[JsonObject],
    profiles: dict[str, _RowProfile],
    stats: _CorpusStats,
    *,
    semantic_mean_similarity: float | None = None,
    semantic_min_similarity: float | None = None,
) -> tuple[_GroupUtility, str | None]:
    """(utility, failure_reason). The gate a group must pass to become a
    proposal: an aggregation signal (label-proximate distinct values or
    distinct sessions) plus a coherent, store-specific label.

    Values only count when they share a sentence with one of the label's
    content stems: a card about hours played aggregates the amounts
    attached to playing, not every number its member texts mention.

    Semantic groups (``semantic_mean_similarity`` not None) share every
    threshold but differ in two documented ways: values/amounts count
    across the whole member texts (an anchor-less group has no shared
    label stem to be proximate to), and the coherence gate is the
    mean- and minimum-pairwise-similarity floors
    instead of the label-stem majority test, which anchor-less groups
    cannot pass by construction. ``label_coherence`` is still measured
    and disclosed for semantic groups; it just does not gate them."""
    label_stems = frozenset(_light_stem(token) for token in _label_content_tokens(label))
    member_profiles = [
        profiles[str(member.get("id"))] for member in members if str(member.get("id")) in profiles
    ]
    values: set[str] = set()
    amounts: set[str] = set()
    sessions: set[str] = set()
    for profile in member_profiles:
        for sentence_stems, sentence_values, sentence_amounts in profile.sentences:
            if semantic_mean_similarity is not None or label_stems & sentence_stems:
                values.update(sentence_values)
                amounts.update(sentence_amounts)
        if profile.session_key is not None:
            sessions.add(profile.session_key)
    specificity = _label_specificity(label, stats)
    coherence = _label_coherence(label, member_profiles)
    utility = _GroupUtility(
        distinct_values=len(values),
        distinct_amounts=len(amounts),
        distinct_sessions=len(sessions),
        label_specificity=specificity,
        label_coherence=coherence,
        score=max(1, len(values)) * max(1, len(sessions)) * specificity,
        semantic_mean_similarity=semantic_mean_similarity,
        semantic_min_similarity=semantic_min_similarity,
    )
    if len(members) < MIN_AGGREGATION_MEMBERS:
        return utility, "below_min_aggregation_members"
    if (
        utility.distinct_values < MIN_DISTINCT_INSTANCE_VALUES
        and utility.distinct_sessions < MIN_DISTINCT_SESSIONS
    ):
        return utility, "no_aggregation_signal"
    if semantic_mean_similarity is not None:
        if semantic_mean_similarity < SEMANTIC_MIN_MEAN_SIMILARITY:
            return utility, "semantic_coherence_below_floor"
        if (
            semantic_min_similarity is None
            or semantic_min_similarity < SEMANTIC_MIN_PAIRWISE_SIMILARITY
        ):
            return utility, "semantic_min_pairwise_below_floor"
    elif coherence < LABEL_COHERENCE_MIN_FRACTION:
        return utility, "label_not_coherent_with_members"
    return utility, None


def _instance_label_junk_reason(label: str) -> str | None:
    """Structural-only junk test for instance labels (no store statistics):
    labels with no content words ("I'm", "I've"), closed-class heads, and
    mid-sentence fragment openers — a label whose first token is a pronoun
    contraction or closed-class word ("Since I'm ...", "I'm logging ...")
    is a truncation artifact, not a name. The same filters card labels
    get, minus the group-dependent rules."""
    content = _label_content_tokens(label)
    if not content:
        return "label_without_content_words"
    raw_tokens = _TOKEN_RE.findall(label.replace("’", "'").casefold())
    first = raw_tokens[0] if raw_tokens else ""
    if first in _PRONOUN_CONTRACTION_HEADS or first.split("'", 1)[0] in _PRONOUN_CONTRACTION_HEADS:
        return "label_pronoun_fragment"
    if first in _CLOSED_CLASS_LABEL_HEADS or content[0] in _CLOSED_CLASS_LABEL_HEADS:
        return "label_head_closed_class"
    return None


def _dominant_noun_label(text: str) -> str | None:
    """Neutral display label for an instance whose extracted labels all
    filtered out: the text's most frequent content-bearing, non-verb
    surface token (ties break alphabetically on the casefolded token;
    the first-seen surface form is displayed). Deterministic."""
    counts: Counter[str] = Counter()
    surfaces: dict[str, str] = {}
    for raw in _SURFACE_TOKEN_RE.findall(text):
        token = raw.casefold().replace("’", "'")
        head = token.split("'", 1)[0]
        if "'" in token and head in _PRONOUN_CONTRACTION_HEADS:
            continue
        if token in _PRONOUN_CONTRACTION_HEADS:
            continue
        if len(token) < 3 or not any(char.isalpha() for char in token):
            continue
        if token in FTS_QUERY_STOPWORDS or token in _TOPIC_TOKEN_BLOCKLIST:
            continue
        if token in _CLOSED_CLASS_LABEL_HEADS or _is_verb_form(token):
            continue
        counts[token] += 1
        surfaces.setdefault(token, raw)
    if not counts:
        return None
    best = min(counts, key=lambda token: (-counts[token], token))
    return surfaces[best]


def _dominant_noun_phrase(
    members: tuple[JsonObject, ...],
    stats: _CorpusStats,
) -> tuple[str | None, list[str]]:
    """(label, label_stems) for a semantic group: the dominant noun across
    the member texts, via the same token machinery ``_dominant_noun_label``
    uses per instance (no verbs, closed-class words, pronoun contractions,
    stopwords, or store-generic stems).

    Stems rank by (member support, total occurrences, alphabetical) —
    "kitchen" mentioned by two of three otherwise anchor-less members
    labels the group. A runner-up stem that also spans at least half the
    members joins as a second word (ordered the way the texts say it).
    The top stem must span at least TWO members: a group where every noun
    appears in a single member has no recognizable topic, and returns
    ``(None, [])`` so the caller can drop it (disclosed)."""
    member_stems: list[set[str]] = []
    occurrences: Counter[str] = Counter()
    for member in members:
        text = _strip_speaker_tag(_member_text(member))
        stems: set[str] = set()
        for raw in _SURFACE_TOKEN_RE.findall(text):
            token = raw.casefold().replace("’", "'")
            head = token.split("'", 1)[0]
            if "'" in token and head in _PRONOUN_CONTRACTION_HEADS:
                continue
            if token in _PRONOUN_CONTRACTION_HEADS:
                continue
            if len(token) < 3 or not any(char.isalpha() for char in token):
                continue
            if token in FTS_QUERY_STOPWORDS or token in _TOPIC_TOKEN_BLOCKLIST:
                continue
            if token in _CLOSED_CLASS_LABEL_HEADS or _is_verb_form(token):
                continue
            stem = _light_stem(token)
            if stem in FTS_QUERY_STOPWORDS or stem in _TOPIC_TOKEN_BLOCKLIST:
                continue
            if stats.is_generic(stem):
                continue
            stems.add(stem)
            occurrences[stem] += 1
        member_stems.append(stems)
    support: Counter[str] = Counter()
    for stems in member_stems:
        support.update(stems)
    if not support:
        return None, []
    ranked = sorted(support, key=lambda stem: (-support[stem], -occurrences[stem], stem))
    if support[ranked[0]] < 2:
        return None, []
    chosen = [ranked[0]]
    majority = max(2, (len(members) + 1) // 2)
    for stem in ranked[1:]:
        if support[stem] >= majority:
            chosen.append(stem)
            break
    surfaces = [_surface_form(members, stem) for stem in chosen]
    return " ".join(_natural_surface_order(members, surfaces)), chosen


def _instance_label(row: JsonObject, *, exclude_normalized: str | None = None) -> str:
    """Entity surface when extraction finds a presentable one, else the
    member title, else a neutral noun label, else the truncated text —
    short, deterministic, display-safe.

    Instance labels get the same hygiene the card label gets: broken
    subspans are repaired against the member's own text ("Us Part II" ->
    "The Last of Us Part II") and pronoun-contraction / closed-class-head
    fragments ("I'm", "Since I'm") are filtered; a member whose labels all
    filter out keeps its value+date line under a neutral label derived
    from its dominant noun token.

    ``exclude_normalized`` skips the group's own entity so an entity-group
    card labels each instance by its distinguishing content, not by the
    shared entity name repeated N times."""
    text = _strip_speaker_tag(_member_text(row))
    candidates = [
        candidate
        for candidate in extract_entity_candidates(text)
        if candidate.normalized != exclude_normalized
    ]
    for candidate in sorted(
        candidates,
        key=lambda candidate: (-candidate.confidence, -candidate.occurrences, candidate.name),
    ):
        label = _expanded_entity_label(candidate.name, (row,))
        if _instance_label_junk_reason(label) is None:
            return label
    title = row.get("title")
    if isinstance(title, str) and title.strip():
        cleaned = _strip_speaker_tag(" ".join(title.split()))[:80]
        if cleaned and _instance_label_junk_reason(cleaned) is None:
            return cleaned
    neutral = _dominant_noun_label(text)
    if neutral is not None:
        return neutral
    return text[:80]


def _instance_record(row: JsonObject, *, exclude_normalized: str | None = None) -> JsonObject:
    text = _strip_speaker_tag(_member_text(row))
    record: JsonObject = {
        "memory_id": str(row.get("id")),
        "label": _instance_label(row, exclude_normalized=exclude_normalized),
        "date": _member_date(row),
        "amounts": _member_amounts(text),
        "text": text[:200],
    }
    role = _member_role(row)
    if role is not None:
        record["role"] = role
    return record


def _amount_unit(amount: str) -> str | None:
    """Unit carried by one amount surface: leading currency symbol or the
    trailing unit word ("30 hours" -> "hours", "$40" -> "$")."""
    amount = amount.strip()
    if amount[:1] in "$€£":
        return amount[:1]
    match = re.search(r"([a-z%]+)\s*$", amount.casefold())
    return match.group(1) if match else None


def _dominant_amount_unit(instances: list[JsonObject]) -> str | None:
    """Most common unit across the instances' amounts, when at least two
    instances carry it (ties break alphabetically) — a card that mostly
    lists hours is titled as an hours aggregation."""
    counts: Counter[str] = Counter()
    for instance in instances:
        amounts = instance.get("amounts")
        if not isinstance(amounts, list):
            continue
        units = {unit for amount in amounts if (unit := _amount_unit(str(amount))) is not None}
        for unit in units:
            counts[unit] += 1
    if not counts:
        return None
    unit = min(counts, key=lambda name: (-counts[name], name))
    return unit if counts[unit] >= 2 else None


def _render_instance(instance: JsonObject) -> str:
    details: list[str] = []
    amounts = instance.get("amounts")
    if isinstance(amounts, list) and amounts:
        details.append(", ".join(str(amount) for amount in amounts))
    if instance.get("date"):
        details.append(str(instance["date"]))
    if instance.get("role"):
        details.append(str(instance["role"]))
    label = str(instance["label"])
    return f"{label} ({'; '.join(details)})" if details else label


def _sorted_members(members: list[JsonObject]) -> tuple[JsonObject, ...]:
    return tuple(
        sorted(
            members,
            key=lambda row: (
                _member_date(row) or "9999-99-99",
                str(row.get("created_at") or ""),
                str(row.get("id")),
            ),
        )
    )


def _scoped_rows(
    rows: list[JsonObject],
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    projects: tuple[str, ...] = (),
) -> list[JsonObject]:
    """Domain/sensitivity scoping, mirroring the consolidation service."""
    # Explicit project names only (see the consolidation service).
    refuse_global_marker(projects, where="rollups")
    allowed = set(sensitivity_allowed)
    scoped: list[JsonObject] = []
    for row in rows:
        sensitivity = str(row.get("sensitivity") or "unknown")
        if sensitivity not in allowed:
            continue
        if domains:
            domain = str(row.get("domain") or "unknown")
            if domain not in domains and domain != "unknown":
                continue
        if projects:
            allowed_projects = set(project_scope_identity(projects))
            if not allowed_projects.intersection(
                project_scope_identity(resource_project_scope(row))
            ):
                continue
        scoped.append(row)
    return scoped


def _supports_explicit_parameter(method: object, name: str) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name
        and parameter.kind
        in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )


def _is_rollup_card(row: JsonObject) -> bool:
    metadata = row.get("metadata_json")
    return isinstance(metadata, dict) and metadata.get("candidate_kind") == ROLLUP_CANDIDATE_KIND


def _may_name_card(
    row: JsonObject,
    *,
    rollup_key: str,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    projects: tuple[str, ...],
) -> bool:
    """Whether a row read by key is a roll-up card of this group's key inside the fence of the pass.

    The accepted-card read asks for the roll-up candidate kind, the requested roll-up keys, the domains, the
    sensitivity ceiling and the projects; the reads by digest key apply the same five controls here. Every
    control is a required keyword-only argument, so a caller cannot leave one out.
    """

    if not _is_rollup_card(row):
        return False
    metadata = row.get("metadata_json")
    if not isinstance(metadata, dict) or metadata.get("rollup_key") != rollup_key:
        return False
    return bool(
        _scoped_rows(
            [row],
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
    )


def _highest_sensitivity(rows: tuple[JsonObject, ...]) -> str:
    rank = {
        "public": 1,
        "internal": 2,
        "unknown": 2,
        "private": 3,
        "confidential": 4,
        "highly_sensitive": 5,
        "sacred": 6,
        "regulated": 6,
    }
    sensitivities = [str(row.get("sensitivity", "unknown")) for row in rows]
    if not sensitivities:
        return "unknown"
    return max(sensitivities, key=lambda value: rank.get(value, rank["unknown"]))


def _dominant_domain(rows: tuple[JsonObject, ...]) -> str:
    domains = {row.get("domain") for row in rows if isinstance(row.get("domain"), str)}
    if len(domains) == 1:
        return str(next(iter(domains)))
    return derived_domain(rows, fallback="unknown")


def _dominant_memory_type(rows: tuple[JsonObject, ...]) -> str:
    counts = Counter(str(row.get("memory_type")) for row in rows if isinstance(row.get("memory_type"), str))
    return counts.most_common(1)[0][0] if counts else "semantic"


# -- optional model refinement (existing routing seam, deterministic fallback) --


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(text.casefold())
        if len(token) >= 3 and token not in FTS_QUERY_STOPWORDS
    }


def _grounding_overlap(summary: str, members: tuple[JsonObject, ...]) -> float:
    summary_tokens = _content_tokens(summary)
    if not summary_tokens:
        return 0.0
    member_tokens: set[str] = set()
    for member in members:
        member_tokens |= _content_tokens(_member_text(member))
    if not member_tokens:
        return 0.0
    return len(summary_tokens & member_tokens) / len(summary_tokens)


def _clean_untrusted_line(text: str) -> str:
    cleaned = "".join(char for char in text if char == " " or char.isprintable())
    cleaned = cleaned.replace("```", "").replace("[UNTRUSTED_CONTEXT_JSON]", "")
    return " ".join(cleaned.split())[:400]


def _build_rollup_summary_prompt(context_json: str) -> str:
    return "\n\n".join(
        [
            f"Workflow: {ROLLUP_SUMMARY_WORKFLOW}",
            "The context lists DISTINCT same-topic memory instances (not duplicates).",
            "Write ONE short sentence summarizing the aggregate (counts, totals, span).",
            "Rules: use only facts present in the instances; do not invent names, dates,"
            " numbers, or totals that cannot be read directly from them.",
            'Return strict JSON only, shaped exactly as {"summary": "..."}'
            " with no markdown fences and no additional keys.",
            "[UNTRUSTED_CONTEXT_JSON]",
            context_json,
        ]
    )


def _refine_summary_with_model(
    *,
    group: _RollupGroup,
    instances: list[JsonObject],
    route,
    provider: BrainModelProvider | None,
    temperature: float,
    trace_id: str | None,
) -> tuple[str | None, JsonObject | None, str | None]:
    """(summary, provenance, refusal_reason). Deterministic refusal paths
    mirror ``generate_consolidation_merge``: routing disallows, provider is
    non-synthesizing, output unparseable, or output fails grounding."""
    context_json = json.dumps(
        {"workflow_type": ROLLUP_SUMMARY_WORKFLOW, "topic": group.label, "instances": instances},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    prompt = _build_rollup_summary_prompt(context_json)
    provenance: JsonObject = {
        "workflow_type": ROLLUP_SUMMARY_WORKFLOW,
        "prompt_hash": f"sha256:{sha256(prompt.encode('utf-8')).hexdigest()}",
        "trace_id": trace_id,
        "created_at": _utc_iso(),
        "prompt_injection_guard": "source_content_untrusted_no_tool_execution",
    }
    if route is None or getattr(route, "approval_required", False) or getattr(route, "route_mode", "") == "model_disabled":
        return None, None, "model_route_disallows_synthesis"
    provider = provider or provider_for_route(route)
    provenance = {**provenance, "provider": provider.provider, "model": provider.model, "routing": route.to_record()}
    if provider.provider in NON_SYNTHESIZING_PROVIDERS:
        return None, provenance, "deterministic_provider_refuses_rollup_summary"
    try:
        raw = provider.chat(prompt=prompt, temperature=temperature)
    except VNextModelIntelligenceError as exc:
        logger.warning(
            "Roll-up summary provider failed error_code=provider_error provider=%s model=%s",
            provider.provider,
            provider.model,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return None, provenance, "provider_error"
    try:
        parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
    except json.JSONDecodeError:
        return None, provenance, "unparseable_model_output"
    if not isinstance(parsed, dict):
        return None, provenance, "unparseable_model_output"
    summary = _clean_untrusted_line(str(parsed.get("summary") or ""))
    if not summary:
        return None, provenance, "empty_model_output"
    overlap = _grounding_overlap(summary, group.members)
    if overlap < ROLLUP_SUMMARY_GROUNDING_MIN_OVERLAP:
        return None, provenance, f"ungrounded_model_output: token_overlap={overlap:.2f}"
    return summary, {**provenance, "grounding_token_overlap": round(overlap, 4)}, None


# -- service --------------------------------------------------------------------


class VNextRollupService:
    def __init__(
        self,
        store: VNextRollupStore,
        *,
        merge_provider: BrainModelProvider | None = None,
        embedding_provider: EmbeddingProvider | None | str = "ambient",
        precomputed_embeddings: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.store = store
        self.merge_provider = merge_provider
        # Same ambient resolution the consolidation service uses: the
        # ``vnext_embeddings`` env seam yields None when unconfigured,
        # which keeps the semantic grouping tier fully dormant (no keys,
        # no network, byte-identical lexical/entity behavior).
        if embedding_provider == "ambient":
            self.embedding_provider: EmbeddingProvider | None = get_embedding_provider()
        else:
            self.embedding_provider = embedding_provider  # type: ignore[assignment]
        self.precomputed_embeddings = precomputed_embeddings or {}

    # -- grouping ---------------------------------------------------------------

    def _collect_rows(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        projects: tuple[str, ...],
        options: RollupOptions,
    ) -> tuple[list[JsonObject], bool, int, bool]:
        # Ask for one sentinel row beyond the configured cap. The store
        # applies status, scope, roll-up-card exclusion, deterministic order,
        # and LIMIT in SQL, so neither a large corpus nor existing cards are
        # materialized before the service enforces its bound.
        list_inputs = self.store.list_rollup_input_memories
        if projects and not _supports_explicit_parameter(list_inputs, "projects"):
            raise VNextRollupValidationError(
                "project-scoped roll-ups require input lookup with explicit projects support"
            )
        if projects:
            rows = list_inputs(
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
                limit=options.max_groupable_memories + 1,
                projects=projects,
            )
        else:
            rows = list_inputs(
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
                limit=options.max_groupable_memories + 1,
            )
        count_inputs = getattr(self.store, "count_rollup_input_memories", None)
        total_exact = False
        total_count = len(rows)
        if callable(count_inputs) and (
            not projects or _supports_explicit_parameter(count_inputs, "projects")
        ):
            try:
                if projects:
                    total_count = int(
                        count_inputs(
                            domains=domains,
                            sensitivity_allowed=sensitivity_allowed,
                            excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
                            projects=projects,
                        )
                    )
                else:
                    total_count = int(
                        count_inputs(
                            domains=domains,
                            sensitivity_allowed=sensitivity_allowed,
                            excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
                        )
                    )
                total_exact = total_count >= len(rows)
            except Exception:  # noqa: BLE001 - legacy adapters may expose a narrower method
                pass
        if len(rows) <= options.max_groupable_memories:
            # The sentinel read itself proves the total when it did not fill
            # the cap-plus-one request, even for legacy stores without count.
            total_count = len(rows)
            total_exact = True
        elif not total_exact:
            total_count = max(total_count, len(rows))
        bounded = len(rows) > options.max_groupable_memories
        rows = rows[: options.max_groupable_memories]
        # Defensive parity for non-SQL protocol implementations. Production
        # stores already apply these predicates before LIMIT.
        scoped_rows = _scoped_rows(
            rows,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        if projects and len(scoped_rows) != len(rows):
            raise VNextRollupValidationError(
                "roll-up input lookup returned rows outside the requested project scope"
            )
        rows = scoped_rows
        rows = [row for row in rows if not _is_rollup_card(row)]
        # The same parity for validity: the bundled stores leave an expired
        # memory out in SQL, and every tier below (entity, topic and the
        # semantic tier that sends text to the embeddings endpoint) groups what
        # is left. A store that cannot is filtered here.
        rows = drop_expired_memories(rows)
        # Insertion-order independent: grouping sees one canonical order.
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))))
        return rows, bounded, total_count, total_exact

    def _entity_key_maps(
        self, row_candidates: list[tuple[JsonObject, tuple]]
    ) -> dict[str, tuple[str, str]]:
        """member normalized-entity-name -> (canonical key, display name).

        One bulk ``find_entities_by_names`` call folds aliases onto their
        canonical entity when the store has an entity surface; otherwise
        the extracted normalized name is its own key.
        """
        extracted: dict[str, str] = {}
        for _row, candidates in row_candidates:
            for candidate in candidates:
                extracted.setdefault(candidate.normalized, candidate.name)
        if not extracted:
            return {}
        mapping = {normalized: (normalized, display) for normalized, display in extracted.items()}
        finder = getattr(self.store, "find_entities_by_names", None)
        if callable(finder):
            for entity in finder(tuple(sorted(extracted))):
                canonical = str(entity.get("normalized_name") or "")
                if not canonical:
                    continue
                display = str(entity.get("name") or extracted.get(canonical, canonical))
                names = [canonical]
                aliases = entity.get("aliases")
                if isinstance(aliases, (list, tuple)):
                    names.extend(str(alias) for alias in aliases)
                for name in names:
                    if name in mapping:
                        mapping[name] = (canonical, display)
        return mapping

    def _group_members(
        self,
        rows: list[JsonObject],
        *,
        options: RollupOptions,
        exclude_member_id_sets: list[set[str]],
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
    ) -> tuple[list[_RollupGroup], list[str], JsonObject, JsonObject | None, list[JsonObject]]:
        """Group only within exact project-scope partitions.

        The fifth item is the rows the skip lines name: the members of every group the pass
        looked at and did not propose, when a line prints a key made from them.

        A card scoped to project A must never aggregate a member also visible
        to project B with an A-only member. Exact partitioning is deliberately
        stricter than overlap-based retrieval scope.
        """

        partitions: dict[tuple[str, ...], list[JsonObject]] = {}
        for row in rows:
            partitions.setdefault(_project_scope_key(row), []).append(row)
        if len(partitions) <= 1:
            groups, skipped, gate, semantic, named = self._group_members_same_scope(
                rows,
                options=options,
                exclude_member_id_sets=exclude_member_id_sets,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
            )
            only_scope = next(iter(partitions), ())
            if only_scope:
                scope_digest = _digest({"project_scope": only_scope})
                groups = [
                    replace(group, rollup_key=f"scope:{scope_digest}:{group.rollup_key}")
                    for group in groups
                ]
            return groups, skipped, gate, semantic, named

        all_groups: list[_RollupGroup] = []
        all_skipped: list[str] = []
        all_named: list[JsonObject] = []
        gate_records: list[JsonObject] = []
        semantic_records: list[JsonObject] = []
        for scope_key in sorted(partitions):
            groups, skipped, gate, semantic, named = self._group_members_same_scope(
                partitions[scope_key],
                options=options,
                exclude_member_id_sets=exclude_member_id_sets,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
            )
            all_named.extend(named)
            scope_digest = _digest({"project_scope": scope_key})
            all_groups.extend(
                replace(
                    group,
                    rollup_key=(
                        f"scope:{scope_digest}:{group.rollup_key}"
                        if scope_key
                        else group.rollup_key
                    ),
                )
                for group in groups
            )
            all_skipped.extend(f"scope:{scope_digest}: {reason}" for reason in skipped)
            gate_records.append({"project_scope": list(scope_key), **gate})
            if semantic is not None:
                semantic_records.append({"project_scope": list(scope_key), **semantic})

        all_groups.sort(key=lambda group: (-group.utility.score, -len(group.members), group.rollup_key))

        def _record_int(record: JsonObject, key: str) -> int:
            value = record.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        gate_record: JsonObject = {
            "scope_partitioned": True,
            "partition_count": len(partitions),
            "partitions": gate_records,
            "dropped_group_count": sum(_record_int(record, "dropped_group_count") for record in gate_records),
        }
        semantic_record: JsonObject | None = None
        if semantic_records:
            semantic_record = {
                "scope_partitioned": True,
                "partition_count": len(semantic_records),
                "partitions": semantic_records,
                "clusters_formed": sum(_record_int(record, "clusters_formed") for record in semantic_records),
                "groups_admitted": sum(_record_int(record, "groups_admitted") for record in semantic_records),
                "chosen_threshold": "per_project_scope",
                "mean_silhouette": None,
            }
        return all_groups, all_skipped, gate_record, semantic_record, all_named

    def _group_members_same_scope(
        self,
        rows: list[JsonObject],
        *,
        options: RollupOptions,
        exclude_member_id_sets: list[set[str]],
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
    ) -> tuple[list[_RollupGroup], list[str], JsonObject, JsonObject | None, list[JsonObject]]:
        skipped: list[str] = []
        claimed: set[str] = set()
        groups: list[_RollupGroup] = []
        dropped: Counter[str] = Counter()
        dropped_examples: list[str] = []
        # Rows a skip line names by a key made from their text or their id. A group
        # that is not proposed still prints its key, so its members are named too.
        named: list[JsonObject] = []

        profiles = _row_profiles(rows)
        stats = _CorpusStats(profiles)

        def _drop(key: str, reason: str, members: Iterable[JsonObject]) -> None:
            # Gate-dropped groups do NOT claim members and are reported as
            # one aggregate skip line (not one line per junk anchor). Only the
            # examples the line prints name their members.
            dropped[reason] += 1
            if len(dropped_examples) < 6:
                dropped_examples.append(f"{key} ({reason})")
                named.extend(members)

        def _admit(
            key: str,
            kind: str,
            label: str,
            members: list[JsonObject],
            label_variants: tuple[str, ...] = (),
            semantic_similarity: float | None = None,
            semantic_min_similarity: float | None = None,
        ) -> None:
            if len(members) > MAX_ROLLUP_GROUP_MEMBERS:
                skipped.append(f"group_too_large: {key} (members={len(members)})")
                named.extend(members)
                return
            member_ids = {str(member.get("id")) for member in members}
            # Groups fully covered by a near-duplicate cluster belong to the
            # dedup/merge pipeline; a roll-up aggregates distinct instances.
            if any(member_ids <= excluded for excluded in exclude_member_id_sets):
                skipped.append(f"covered_by_near_duplicate_cluster: {key} (members={len(members)})")
                named.extend(members)
                return
            token_sets = [
                _topic_tokens(_member_text(member)) for member in members[:JACCARD_SAMPLE_MEMBERS]
            ]
            jaccard = _mean_pairwise_jaccard(token_sets)
            if jaccard >= DUPLICATE_GROUP_JACCARD_THRESHOLD:
                skipped.append(
                    f"near_duplicate_group_left_to_dedup: {key} "
                    f"(mean_jaccard={jaccard:.2f}, members={len(members)})"
                )
                named.extend(members)
                return
            # Label hygiene, then the group-utility gate; failing groups are
            # dropped and their members stay available to later groups. The
            # utility is measured first because the bare-verb label rule
            # needs the group's label-proximate amount count.
            utility, gate_reason = _group_utility(
                label,
                members,
                profiles,
                stats,
                semantic_mean_similarity=semantic_similarity,
                semantic_min_similarity=semantic_min_similarity,
            )
            junk_reason = _label_junk_reason(
                label, stats, label_amount_count=utility.distinct_amounts
            )
            if junk_reason is not None:
                _drop(key, junk_reason, members)
                return
            if gate_reason is not None:
                _drop(key, gate_reason, members)
                return
            claimed.update(member_ids)
            groups.append(
                _RollupGroup(
                    rollup_key=key,
                    group_kind=kind,
                    label=label,
                    members=_sorted_members(members),
                    utility=utility,
                    label_variants=label_variants,
                )
            )

        # One extraction pass per row; both grouping passes reuse it.
        row_candidates = [
            (row, extract_entity_candidates(_strip_speaker_tag(_member_text(row)))) for row in rows
        ]

        # Pass 1 — same-entity groups (more precise; they claim members first).
        entity_map = self._entity_key_maps(row_candidates)
        entity_members: dict[str, list[JsonObject]] = {}
        entity_display: dict[str, str] = {}
        for row, candidates in row_candidates:
            seen_keys: set[str] = set()
            for candidate in candidates:
                key, display = entity_map.get(candidate.normalized, (candidate.normalized, candidate.name))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                entity_members.setdefault(key, []).append(row)
                entity_display.setdefault(key, display)
        for key in sorted(entity_members, key=lambda name: (-len(entity_members[name]), name)):
            members = [row for row in entity_members[key] if str(row.get("id")) not in claimed]
            if len(members) < options.min_members:
                continue
            # Repair labels extraction truncated at a lowercase title
            # connector ("Us Part II" -> "The Last of Us Part II").
            label = _expanded_entity_label(entity_display[key], tuple(members))
            _admit(f"entity:{key}", "entity", label, members)

        # Pass 2 — lexical-topic anchors over members no entity group claimed.
        remaining = [row for row in rows if str(row.get("id")) not in claimed]
        anchor_members: dict[str, list[JsonObject]] = {}
        for row in remaining:
            for token in sorted(_topic_tokens(_member_text(row))):
                anchor_members.setdefault(token, []).append(row)
        for anchor in sorted(anchor_members, key=lambda token: (-len(anchor_members[token]), token)):
            if stats.is_generic(anchor):
                # Frequency-derived: an anchor dispersed across most of the
                # store's sessions is plumbing, not a topic. Checked before
                # claiming so real topics keep these members.
                if len(anchor_members[anchor]) >= options.min_members:
                    _drop(f"topic:{anchor}", "anchor_generic_for_store", anchor_members[anchor])
                continue
            members = [row for row in anchor_members[anchor] if str(row.get("id")) not in claimed]
            if len(members) < options.min_members:
                continue
            # Label: up to two topic stems shared by EVERY member (surface
            # forms), so the card carries the words an aggregation query
            # would use ("hours played"), not just the single anchor.
            # Closed-class words never carry a topic, so they are not
            # label material ("hiked trail", never "along hiked").
            shared = {
                stem
                for stem in set.intersection(*(_topic_tokens(_member_text(member)) for member in members))
                if not stats.is_generic(stem) and stem not in _CLOSED_CLASS_LABEL_HEADS
            }
            ordered = sorted(shared, key=lambda token: (-len(anchor_members.get(token, ())), token)) or [anchor]
            surfaces = [_surface_form(tuple(members), stem) for stem in ordered[:2]]
            label = " ".join(_natural_surface_order(tuple(members), surfaces))
            variants = _surface_variants(tuple(members), ordered[:2], label)
            _admit(f"topic:{anchor}", "topic", label, members, label_variants=variants)

        # Pass 3 — semantic embedding tier over members neither lexical
        # pass claimed (topics whose instances share NO anchor token).
        # DORMANT without a configured embedding provider: nothing below
        # runs, and every output of this method stays byte-identical to
        # the lexical/entity-only behavior. When the tier runs, all of it
        # is disclosed in the returned semantic record.
        semantic_record: JsonObject | None = None
        if self.embedding_provider is not None:
            unclaimed = [row for row in rows if str(row.get("id")) not in claimed]
            semantic_clusters, semantic_record = self._semantic_clusters(
                unclaimed,
                options=options,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
            )
            admitted = 0
            seen_semantic_keys: set[str] = set()
            for cluster_members, mean_similarity, min_similarity in semantic_clusters:
                if mean_similarity >= SEMANTIC_NEAR_DUPLICATE_SIMILARITY:
                    skipped.append(
                        "semantic_near_duplicate_left_to_dedup: "
                        f"mean_similarity={mean_similarity:.2f}, members={len(cluster_members)}"
                    )
                    continue
                semantic_label, label_stems = _dominant_noun_phrase(cluster_members, stats)
                if semantic_label is None:
                    # No noun spans even two members: the cluster has no
                    # recognizable topic to put on a card.
                    # The key prints the smallest member id, so that row is the one named.
                    printed = min(cluster_members, key=lambda row: str(row.get("id")))
                    _drop(
                        "semantic:cluster-" + str(printed.get("id")),
                        "semantic_no_dominant_label",
                        (printed,),
                    )
                    continue
                key = f"semantic:{semantic_label.casefold()}"
                if key in seen_semantic_keys:
                    # Two clusters sharing one dominant label in a single
                    # run would collide on the rollup key; keep the first
                    # (larger, by cluster ordering) and disclose the rest.
                    skipped.append(f"semantic_label_collision: {key} (members={len(cluster_members)})")
                    named.extend(cluster_members)
                    continue
                seen_semantic_keys.add(key)
                variants = _surface_variants(cluster_members, label_stems, semantic_label)
                groups_before = len(groups)
                _admit(
                    key,
                    "semantic",
                    semantic_label,
                    list(cluster_members),
                    label_variants=variants,
                    semantic_similarity=mean_similarity,
                    semantic_min_similarity=min_similarity,
                )
                if len(groups) > groups_before:
                    admitted += 1
            semantic_record["groups_admitted"] = admitted
            semantic_skipped = semantic_record.get("skipped")
            if isinstance(semantic_skipped, list):
                for reason in semantic_skipped:
                    skipped.append(f"semantic_tier: {reason}")

        # Rank by aggregation utility so max_rollups keeps the best groups,
        # not the first-admitted ones. Deterministic tie-breaks.
        groups.sort(key=lambda group: (-group.utility.score, -len(group.members), group.rollup_key))

        gate_record: JsonObject = {
            "anchor_stats_enabled": stats.enabled,
            "session_count": stats.session_count,
            "generic_anchor_session_dispersion": GENERIC_ANCHOR_SESSION_DISPERSION,
            "min_aggregation_members": MIN_AGGREGATION_MEMBERS,
            "dropped_group_count": sum(dropped.values()),
            "dropped_by_reason": {reason: dropped[reason] for reason in sorted(dropped)},
        }
        if dropped:
            reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(dropped.items()))
            examples = "; ".join(dropped_examples)
            skipped.append(
                f"quality_gate_dropped: {sum(dropped.values())} group(s) not proposed "
                f"({reasons}); e.g. {examples}"
            )
        return groups, skipped, gate_record, semantic_record, named

    def _semantic_clusters(
        self,
        remaining: list[JsonObject],
        *,
        options: RollupOptions,
        domains: list[str] | None,
        sensitivity_allowed: list[str] | None,
    ) -> tuple[list[tuple[tuple[JsonObject, ...], float, float]], JsonObject]:
        """Embedding clusters over the rows the lexical/entity passes left
        unclaimed: ``(clusters, disclosure_record)`` where each cluster is
        ``(members sorted like every group, mean cosine, minimum cosine)``.

        Exact presence is resolved before provider work. Compact vectors
        already derived by consolidation are reused, and the provider sees
        only present cache misses. Every early exit lands in the record's
        ``skipped`` list."""
        provider = self.embedding_provider
        skipped_reasons: list[str] = []
        threshold_sweep: list[JsonObject] = []
        record: JsonObject = {
            "embedding_access": "exact_id_presence_read_then_reuse_or_provider_reembed",
            "provider": getattr(provider, "provider", None),
            "model": getattr(provider, "model", None),
            "ungrouped_rows": len(remaining),
            "embedded_rows": 0,
            "bounded": False,
            "threshold_sweep": threshold_sweep,
            "chosen_threshold": None,
            "mean_silhouette": None,
            "clustering": "complete_link_all_pairs_at_threshold",
            "min_mean_similarity": SEMANTIC_MIN_MEAN_SIMILARITY,
            "min_pairwise_similarity": SEMANTIC_MIN_PAIRWISE_SIMILARITY,
            "matrix_dtype": "float32",
            "matrix_materialization": False,
            "similarity_block_rows": SEMANTIC_SIMILARITY_BLOCK_ROWS,
            "peak_similarity_block_bytes": 0,
            "reused_embedding_rows": 0,
            "provider_embedded_rows": 0,
            "clusters_formed": 0,
            "groups_admitted": 0,
            "skipped": skipped_reasons,
        }
        if provider is None:  # caller-gated; kept defensive
            skipped_reasons.append("no_embedding_provider_configured")
            return [], record
        usable_min = max(options.min_members, MIN_AGGREGATION_MEMBERS)
        if len(remaining) < usable_min:
            skipped_reasons.append("fewer_ungrouped_rows_than_min_members")
            return [], record
        list_memory_ids_with_embeddings = getattr(
            self.store, "list_memory_ids_with_embeddings", None
        )
        if not callable(list_memory_ids_with_embeddings):
            skipped_reasons.append("store_lacks_embedding_presence_read")
            return [], record
        embeddable = [(row, memory_embedding_text(row)) for row in remaining]
        embeddable = [(row, text) for row, text in embeddable if text]
        if len(embeddable) > MAX_SEMANTIC_TIER_MEMBERS:
            # Rows arrive sorted by (created_at, id); keep the most recent.
            record["bounded"] = True
            embeddable = embeddable[-MAX_SEMANTIC_TIER_MEMBERS:]
        if len(embeddable) < usable_min:
            skipped_reasons.append("fewer_embeddable_rows_than_min_members")
            return [], record

        # Presence comes first: rows without stored vectors never consume a
        # provider call and cannot participate in semantic grouping.
        selected_ids = [str(row.get("id")) for row, _ in embeddable]
        try:
            embedded_ids = set(list_memory_ids_with_embeddings(selected_ids))
        except Exception as exc:  # noqa: BLE001 - store backends raise driver-specific errors
            logger.warning(
                "Roll-up embedding-presence read failed error_code=embedding_presence_read_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            skipped_reasons.append("embedding_presence_read_failed")
            return [], record
        present = [(row, text) for row, text in embeddable if str(row.get("id")) in embedded_ids]
        record["embedded_rows"] = len(present)
        if len(present) < usable_min:
            skipped_reasons.append("fewer_embedded_rows_than_min_members")
            return [], record
        vectors_by_id: dict[str, np.ndarray] = {}
        missing: list[tuple[JsonObject, str]] = []
        for row, text in present:
            member_id = str(row.get("id"))
            cached = self.precomputed_embeddings.get(member_id)
            if cached is not None and np.asarray(cached).size > 0:
                vectors_by_id[member_id] = np.asarray(cached, dtype=np.float32)
            else:
                missing.append((row, text))
        record["reused_embedding_rows"] = len(vectors_by_id)
        try:
            for start in range(0, len(missing), MAX_EMBEDDINGS_BATCH_SIZE):
                batch = missing[start : start + MAX_EMBEDDINGS_BATCH_SIZE]
                batch_vectors = provider.embed_batch([text for _row, text in batch])
                if len(batch_vectors) != len(batch):
                    skipped_reasons.append("embedding_provider_returned_wrong_batch_size")
                    return [], record
                for (row, _text), vector in zip(batch, batch_vectors, strict=True):
                    vectors_by_id[str(row.get("id"))] = np.asarray(vector, dtype=np.float32)
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            logger.warning(
                "Roll-up embedding provider failed error_code=embedding_provider_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            skipped_reasons.append("embedding_provider_failed")
            return [], record
        record["provider_embedded_rows"] = len(missing)
        members = [(row, vectors_by_id[str(row.get("id"))]) for row, _text in present]

        width = max(len(vector) for _, vector in members)
        matrix: np.ndarray = np.zeros((len(members), width), dtype=np.float32)
        for index, (_, member_vector) in enumerate(members):
            matrix[index, : len(member_vector)] = member_vector
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0
        matrix /= norms[:, None]
        normalized = matrix
        record["peak_similarity_block_bytes"] = (
            min(len(members), SEMANTIC_SIMILARITY_BLOCK_ROWS)
            * len(members)
            * np.dtype(np.float32).itemsize
        )

        # Conservative threshold sweep, scored by the silhouette-style
        # criterion; only sweep points that yield at least one usable
        # cluster AND a defined criterion (>= 2 components) compete. Ties
        # keep the HIGHER threshold. Scores are rounded before comparison
        # so insertion order cannot flip a tie through float summation.
        best: tuple[float, float, list[int]] | None = None
        stable_keys = [str(row.get("id")) for row, _vector in members]
        for threshold in SEMANTIC_SWEEP_THRESHOLDS:
            labels = _cohesive_labels(normalized, stable_keys, threshold)
            component_sizes = Counter(labels)
            usable_components = sum(1 for size in component_sizes.values() if size >= usable_min)
            entry: JsonObject = {
                "threshold": threshold,
                "components": len(component_sizes),
                "usable_components": usable_components,
                "mean_silhouette": None,
            }
            if usable_components >= 1:
                score = _mean_silhouette(normalized, labels)
                if score is not None:
                    rounded = round(score, 6)
                    entry["mean_silhouette"] = rounded
                    if best is None or rounded > best[0] or (rounded == best[0] and threshold > best[1]):
                        best = (rounded, threshold, labels)
            threshold_sweep.append(entry)
        if best is None:
            skipped_reasons.append("no_usable_clusters_at_any_threshold")
            return [], record
        score, threshold, labels = best
        record["chosen_threshold"] = threshold
        record["mean_silhouette"] = score

        components: dict[int, list[int]] = {}
        for index, component in enumerate(labels):
            components.setdefault(component, []).append(index)
        clusters = [indices for indices in components.values() if len(indices) >= usable_min]
        # Insertion-order independent ordering: size, then smallest member id.
        clusters.sort(key=lambda indices: (-len(indices), min(str(members[i][0].get("id")) for i in indices)))
        record["clusters_formed"] = len(clusters)

        results: list[tuple[tuple[JsonObject, ...], float, float]] = []
        for indices in clusters:
            mean_similarity, min_similarity = _semantic_pair_stats(normalized, indices)
            results.append(
                (
                    _sorted_members([members[i][0] for i in indices]),
                    mean_similarity,
                    min_similarity,
                )
            )
        return results, record

    # -- accepted / pending state -------------------------------------------------

    def _existing_rollup_state(
        self,
        *,
        rollup_digests: tuple[str, ...],
        rollup_keys: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        projects: tuple[str, ...],
    ) -> tuple[dict[str, JsonObject], dict[str, JsonObject]]:
        """(pending candidate by rollup_digest, accepted card by rollup_key)."""
        pending: dict[str, JsonObject] = {}
        unique_digests = tuple(sorted(set(rollup_digests)))
        pending_reader = self.store.list_pending_rollup_candidates
        accepted_reader = self.store.list_accepted_rollup_cards
        if projects and (
            not _supports_explicit_parameter(pending_reader, "projects")
            or not _supports_explicit_parameter(accepted_reader, "projects")
        ):
            raise VNextRollupValidationError(
                "project-scoped roll-ups require candidate/card lookups with explicit projects support"
            )
        pending_rows = (
            pending_reader(
                rollup_digests=unique_digests,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                candidate_kind=ROLLUP_CANDIDATE_KIND,
                limit=len(unique_digests),
                **({"projects": projects} if projects else {}),
            )
            if unique_digests
            else []
        )
        for row in pending_rows:
            metadata = row.get("metadata_json")
            if not isinstance(metadata, dict) or row.get("id") is None:
                continue
            digest = metadata.get("rollup_digest")
            if isinstance(digest, str) and digest:
                pending.setdefault(digest, row)
        accepted: dict[str, JsonObject] = {}
        unique_keys = tuple(sorted(set(rollup_keys)))
        accepted_rows = (
            accepted_reader(
                rollup_keys=unique_keys,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                candidate_kind=ROLLUP_CANDIDATE_KIND,
                limit=len(unique_keys),
                **({"projects": projects} if projects else {}),
            )
            if unique_keys
            else []
        )
        for row in accepted_rows:
            metadata = row.get("metadata_json")
            if not isinstance(metadata, dict) or metadata.get("candidate_kind") != ROLLUP_CANDIDATE_KIND:
                continue
            # A card whose validity window has closed is not the accepted card
            # for its topic. The bundled stores leave it out in SQL, before they
            # pick one card per key; this is the same parity for any other store.
            if not memory_window_is_open(row):
                continue
            key = metadata.get("rollup_key")
            if isinstance(key, str) and key and key not in accepted:
                accepted[key] = row
        if projects:
            combined = [*pending_rows, *accepted_rows]
            if len(
                _scoped_rows(
                    combined,
                    domains=domains,
                    sensitivity_allowed=sensitivity_allowed,
                    projects=projects,
                )
            ) != len(combined):
                raise VNextRollupValidationError(
                    "roll-up candidate/card lookup returned rows outside the requested project scope"
                )
        return pending, accepted

    def _expired_card_for_digest(
        self,
        rollup_digest: str,
        *,
        rollup_key: str,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        projects: tuple[str, ...],
    ) -> JsonObject | None:
        """The card an earlier pass made for exactly these members, if its validity window has closed.

        A card's ``memory_key`` is derived from its digest, so a proposal for members that have not
        changed since the card was made has the key the card already holds, and creating it would
        collide with that row. While the expired card counted as the accepted card, the group was
        reported as covered and no proposal was tried. Now that an expired card is not the accepted
        card, this keeps an unchanged group from being proposed again under the same key: it is
        reported, and left alone, until its members change. A store with no ``get_memory_by_key``
        reports nothing, as before.

        The status of the card is not tested. Its row holds the key whatever the status, and the
        staleness sweep moves an expired card from ``active`` to ``stale`` while it keeps its
        ``valid_to``, so a test for a searchable status would let the pass raise on the key as soon
        as the sweep had run.

        The read asks the store for soft-deleted rows too (``include_deleted=True``). A card archived
        through ``update_memory`` has ``deleted_at`` set and keeps its ``memory_key``, and the unique
        index counts it, so a read that skipped soft-deleted rows would not see the row that makes the
        create raise.

        This read applies every other control the accepted-card read applies, and the fence is a
        required keyword-only argument so a caller cannot leave one out: the row must be a roll-up
        card, for this group's key, inside the domains, the sensitivity ceiling and the projects of
        the pass. A card outside any of them is not named. The controls are ``_may_name_card``, which
        ``_row_at_digest_key`` applies too.
        """

        getter = getattr(self.store, "get_memory_by_key", None)
        if not callable(getter):
            return None
        row = getter(memory_key=f"vnext.rollup.{rollup_digest}", include_deleted=True)
        if row is None:
            return None
        if memory_window_is_open(row):
            return None
        if not _may_name_card(
            row,
            rollup_key=rollup_key,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        ):
            return None
        return row

    def _row_at_digest_key(
        self,
        rollup_digest: str,
        *,
        rollup_key: str,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        projects: tuple[str, ...],
    ) -> tuple[bool, JsonObject | None]:
        """(held, card): whether any row holds the memory key of this digest, and the card to name for it.

        ``_expired_card_for_digest`` names the card whose validity window has closed. A card can hold the
        key in other ways: a reviewer rejected it, a later revision superseded it, it was archived, the
        staleness sweep marked it stale because nobody confirmed it (that arm leaves ``valid_to`` open), or
        it is a candidate or an accepted card the pass did not pick for this key. The unique index on
        ``(user, profile, memory_key)`` holds the key in every one of those, so a proposal for members that
        have not changed since would raise ``IntegrityError`` on it. The pass calls this after the expired
        card read and reports the group as held back instead of proposing it, until its members change and
        the digest, and with it the key, changes.

        The read asks the store for soft-deleted rows too (``include_deleted=True``): a card archived through
        ``update_memory`` has ``deleted_at`` set and keeps its ``memory_key``, and the unique index counts it,
        so a read that skipped soft-deleted rows would not see the row that makes the create raise.

        ``held`` is true for any row at the key. ``card`` is that row only when the pass may name it, by the
        same controls the expired-card read applies (a roll-up card, for this group's key, inside the
        domains, the sensitivity ceiling and the projects of the pass); a row outside them makes the group
        held back with no id, so the pass does not name a row it is not allowed to show. As in the
        expired-card read, the fence is a required keyword-only argument. A store with no
        ``get_memory_by_key`` holds nothing, as before.
        """

        getter = getattr(self.store, "get_memory_by_key", None)
        if not callable(getter):
            return False, None
        row = getter(memory_key=f"vnext.rollup.{rollup_digest}", include_deleted=True)
        if row is None:
            return False, None
        named = _may_name_card(
            row,
            rollup_key=rollup_key,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
        return True, (row if named else None)

    # -- card assembly --------------------------------------------------------------

    def _render_card(
        self,
        *,
        group: _RollupGroup,
        instances: list[JsonObject],
        model_summary: str | None,
        grouping_input_truncated: bool,
        grouping_input_count: int,
        grouping_input_total: int,
        grouping_input_total_exact: bool,
    ) -> tuple[str, str, str]:
        """(title, canonical_text, summary) — deterministic; the optional
        model summary is appended as a clearly-labelled extra sentence.

        The title reads like a topic: label plus the dominant value unit
        carried by the instances ("hours played — 5 instances, amounts in
        hours"), derived deterministically from the instance amounts."""
        rendered = "; ".join(_render_instance(instance) for instance in instances)
        total_count = len(group.members)
        displayed_count = len(instances)
        truncated = displayed_count < total_count
        unit = _dominant_amount_unit(instances)
        if grouping_input_truncated:
            corpus_total = (
                str(grouping_input_total)
                if grouping_input_total_exact
                else f"at least {grouping_input_total}"
            )
            display = f"; showing {displayed_count}" if truncated else ""
            title = (
                f"Roll-up: {group.label} — {total_count} matched instances in bounded input "
                f"({grouping_input_count} of {corpus_total} memories scanned{display})"
            )
        elif truncated:
            title = (
                f"Roll-up: {group.label} — {total_count} instances total "
                f"(showing {displayed_count})"
            )
        elif unit is not None:
            title = f"Roll-up: {group.label} — {total_count} instances, amounts in {unit}"
        else:
            title = f"Roll-up: {group.label} ({total_count} instances in total)"
        head = group.label
        if group.label_variants:
            head = f"{group.label} (also: {', '.join(group.label_variants)})"
        if grouping_input_truncated:
            corpus_total = (
                str(grouping_input_total)
                if grouping_input_total_exact
                else f"at least {grouping_input_total}"
            )
            display = f"; showing {displayed_count}" if truncated else ""
            canonical_text = (
                f"{head} — {total_count} matched instances in a truncated grouping input "
                f"({grouping_input_count} of {corpus_total} in-scope memories scanned{display}): "
                f"{rendered}."
            )
        elif truncated:
            canonical_text = (
                f"{head} — {total_count} instances in the authoritative group; "
                f"showing {displayed_count}: {rendered}."
            )
        else:
            canonical_text = f"{head} — {total_count} instances in total: {rendered}."
        if model_summary:
            canonical_text = f"{canonical_text} Summary: {model_summary}"
        summary = canonical_text[:280]
        return title, canonical_text, summary

    def _create_rollup_candidate(
        self,
        *,
        group: _RollupGroup,
        instances: list[JsonObject],
        rollup_digest: str,
        title: str,
        canonical_text: str,
        summary: str,
        revises_memory: JsonObject | None,
        proposed_supersede: list[str],
        model_provenance: JsonObject | None,
        merge_refusal: str | None,
        grouping_input_truncated: bool,
        grouping_input_count: int,
        grouping_input_total: int,
        grouping_input_total_exact: bool,
        generated_by: str,
        trace_id: str | None,
    ) -> JsonObject:
        # Authoritative membership is the FULL group, not the truncated display
        # instances. Persisting only the displayed subset (max_instances_per_card)
        # as cluster_member_ids made the stable-identity comparison recompute the
        # full set every run, so any group larger than the display cap never
        # matched its accepted card and re-proposed a revision (and collided on
        # the digest-keyed memory_key) forever (audit 2 P1 #6).
        member_ids = [str(member.get("id")) for member in group.members]
        members_by_id = {str(member.get("id")): member for member in group.members}
        member_snapshots = [
            memory_version_snapshot(members_by_id[member_id])
            for member_id in member_ids
        ]
        revises_memory_id = str(revises_memory.get("id")) if revises_memory is not None else None
        if revises_memory is not None:
            # A revision also depends on the exact accepted card it proposes
            # to retire. Persist that target in the same reviewed-input
            # snapshot set so a concurrent edit/retirement invalidates the
            # proposal before any member can be superseded.
            member_snapshots.append(memory_version_snapshot(revises_memory))
        source_refs = [f"memory:{member_id}" for member_id in member_ids]
        # Provenance parity with the merge/dedup proposals: the card carries
        # the union of its LISTED instances' source events (bounded by the
        # per-card instance cap).
        listed = {str(instance["memory_id"]) for instance in instances}
        source_event_ids: list[str] = []
        for member in group.members:
            if str(member.get("id")) not in listed:
                continue
            events = member.get("source_event_ids")
            if isinstance(events, list):
                source_event_ids.extend(str(item) for item in events if item is not None)
        source_event_ids = list(dict.fromkeys(source_event_ids))
        reviewer_instructions = [
            f"Review roll-up card for topic '{group.label}'; accepting it is the promotion decision.",
            "Accepting through the memory commit service (accept_consolidation_candidate) promotes this "
            "card to a first-class memory. Members are NOT superseded: they stay active and individually "
            "recallable; the card only pre-aggregates them for aggregation-style recall.",
        ]
        if revises_memory_id is not None:
            reviewer_instructions.append(
                f"This is a REVISION of accepted roll-up {revises_memory_id} (new members arrived in its "
                "topic). Accepting supersedes only that previous card, never the member memories."
            )
        reviewer_instructions.append(
            "Roll-ups never promote or supersede anything automatically; nothing changes until a reviewer accepts."
        )
        project_scope = _shared_project_scope(group.members)
        rollup_value: JsonObject = {
            "rollup_key": group.rollup_key,
            "group_kind": group.group_kind,
            "topic_label": group.label,
            "member_count": len(member_ids),
            "displayed_instance_count": len(instances),
            "instances_truncated": len(instances) < len(member_ids),
            "grouping_input_truncated": grouping_input_truncated,
            "grouping_input_count": grouping_input_count,
            "grouping_input_total": grouping_input_total,
            "grouping_input_total_exact": grouping_input_total_exact,
            "member_ids": list(member_ids),
            "instances": instances,
        }
        if revises_memory_id is not None:
            rollup_value["revises_memory_id"] = revises_memory_id
        return self.store.create_memory(
            {
                "memory_key": f"vnext.rollup.{rollup_digest}",
                "value": {"kind": ROLLUP_CANDIDATE_KIND, "text": canonical_text, "rollup": rollup_value},
                "status": "candidate",
                "memory_type": _dominant_memory_type(group.members),
                "confidence": 0.6 if model_provenance and merge_refusal is None else 0.75,
                "trust_class": "llm_single_source" if model_provenance and merge_refusal is None else "deterministic",
                "promotion_eligibility": "promotable",
                "title": title,
                "canonical_text": canonical_text,
                "summary": summary,
                "domain": _dominant_domain(group.members),
                "sensitivity": _highest_sensitivity(group.members),
                "project_id": project_scope[0] if len(project_scope) == 1 else None,
                "source_event_ids": source_event_ids,
                "metadata_json": {
                    "candidate_kind": ROLLUP_CANDIDATE_KIND,
                    "rollup_digest": rollup_digest,
                    "rollup_key": group.rollup_key,
                    "review_required": True,
                    "source_refs": source_refs,
                    "project_scope": list(project_scope),
                    "trace_id": trace_id,
                    # accept_consolidation_candidate compatibility: the
                    # existing review/acceptance path reads this block.
                    "consolidation": {
                        "proposal_kind": ROLLUP_PROPOSAL_KIND,
                        "cluster_member_ids": member_ids,
                        "member_snapshots": member_snapshots,
                        "proposed_supersede": proposed_supersede,
                        "survivor_memory_id": None,
                        "model_provenance": model_provenance,
                        "merge_refusal": merge_refusal,
                        "reviewer_instructions": reviewer_instructions,
                        "rollup": {
                            "rollup_key": group.rollup_key,
                            "group_kind": group.group_kind,
                            "topic_label": group.label,
                            "revises_memory_id": revises_memory_id,
                        },
                    },
                },
            },
            actor_type=generated_by,
        )

    # -- entry point ------------------------------------------------------------------

    def propose_rollups(
        self,
        *,
        domains: list[str] | None = None,
        projects: tuple[str, ...] = (),
        sensitivity_allowed: list[str] | None = None,
        options: RollupOptions | None = None,
        create_candidate_memories: bool = True,
        generated_by: str = "system",
        trace_id: str | None = None,
        generation_mode: str = "deterministic",
        route=None,
        model_temperature: float = 0.2,
        exclude_member_id_sets: list[set[str]] | None = None,
    ) -> RollupOutcome:
        """One review-only roll-up pass over the in-scope memories.

        Never mutates existing memories: the only writes are new
        ``status="candidate"`` roll-up cards (and only when
        ``create_candidate_memories`` is true). ``exclude_member_id_sets``
        carries the caller's near-duplicate cluster memberships so groups
        the dedup/merge pipeline already covers are not re-proposed here.

        When an embedding provider is configured, the semantic grouping
        tier also runs (over members the lexical/entity passes left
        unclaimed) and its full disclosure record lands in
        ``outcome.semantic``; without a provider the tier is dormant and
        the outcome is byte-identical to the lexical/entity-only shape.
        """
        options = options or RollupOptions()
        sensitivity = list(sensitivity_allowed or ("public", "internal", "private", "unknown"))
        outcome = RollupOutcome(options=options.to_record())

        rows, bounded, total_count, total_exact = self._collect_rows(
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
            options=options,
        )
        outcome.groupable_count = len(rows)
        outcome.groupable_total_count = total_count
        outcome.groupable_total_exact = total_exact
        outcome.bounded = bounded
        if bounded:
            total_label = str(total_count) if total_exact else f"at least {total_count}"
            outcome.skipped.append(
                f"grouping bounded to the {options.max_groupable_memories} most recently created "
                f"memories of {total_label} in scope"
            )
        if len(rows) < options.min_members:
            outcome.skipped.append("fewer_memories_than_min_members")
            return outcome

        groups, group_skips, gate_record, semantic_record, skip_named_rows = self._group_members(
            rows,
            options=options,
            exclude_member_id_sets=exclude_member_id_sets or [],
            domains=domains,
            sensitivity_allowed=sensitivity,
        )
        outcome.skipped.extend(group_skips)
        outcome.name_rows(skip_named_rows)
        outcome.quality_gate = gate_record
        outcome.semantic = semantic_record
        if len(groups) > options.max_rollups:
            outcome.skipped.append(
                f"rollup_bound: {len(groups) - options.max_rollups} groups beyond "
                f"max_rollups={options.max_rollups} were not proposed this run"
            )
            groups = groups[: options.max_rollups]

        prepared_groups: list[_PreparedRollupGroup] = []
        for group in groups:
            outcome.name_rows(group.members)
            member_ids = tuple(str(row.get("id")) for row in group.members)
            current_member_snapshots = tuple(
                memory_version_snapshot(row)
                for row in sorted(group.members, key=lambda item: str(item.get("id")))
            )
            rollup_digest = _digest(
                {
                    "rollup_key": group.rollup_key,
                    # The review record below keeps ``updated_at`` for strict
                    # stale detection. Candidate identity only needs stable
                    # content/status versions, so equivalent corpora remain
                    # deterministic across insertion order and timestamps.
                    "member_versions": [
                        {
                            "id": snapshot["id"],
                            "status": snapshot["status"],
                            "content_digest": snapshot["content_digest"],
                        }
                        for snapshot in current_member_snapshots
                    ],
                }
            )
            group_record: JsonObject = {
                "rollup_key": group.rollup_key,
                "group_kind": group.group_kind,
                "label": group.label,
                "member_ids": list(member_ids),
                "rollup_digest": rollup_digest,
                "aggregation": group.utility.to_record(),
            }
            prepared_groups.append(
                _PreparedRollupGroup(
                    group=group,
                    member_ids=member_ids,
                    current_member_snapshots=current_member_snapshots,
                    rollup_digest=rollup_digest,
                    group_record=group_record,
                )
            )

        pending, accepted = self._existing_rollup_state(
            rollup_digests=tuple(item.rollup_digest for item in prepared_groups),
            rollup_keys=tuple(item.group.rollup_key for item in prepared_groups),
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
        )

        for prepared in prepared_groups:
            group = prepared.group
            current_member_ids = list(prepared.member_ids)
            rollup_digest = prepared.rollup_digest
            group_record = prepared.group_record

            accepted_card = accepted.get(group.rollup_key)
            revises_memory_id: str | None = None
            proposed_supersede: list[str] = []
            if accepted_card is not None:
                # The group record or the revision proposal prints the id of this card.
                outcome.name_rows([accepted_card])
                accepted_metadata = accepted_card.get("metadata_json")
                accepted_consolidation = (
                    accepted_metadata.get("consolidation") if isinstance(accepted_metadata, dict) else None
                )
                accepted_members = set()
                if isinstance(accepted_consolidation, dict):
                    accepted_members = {
                        str(member)
                        for member in (accepted_consolidation.get("cluster_member_ids") or [])
                    }
                accepted_snapshot_rows = (
                    accepted_consolidation.get("member_snapshots")
                    if isinstance(accepted_consolidation, dict)
                    else None
                )
                accepted_snapshots = {
                    str(snapshot.get("id")): snapshot
                    for snapshot in accepted_snapshot_rows
                    if isinstance(snapshot, dict) and snapshot.get("id") is not None
                } if isinstance(accepted_snapshot_rows, list) else {}
                current_members_by_id = {str(row.get("id")): row for row in group.members}
                accepted_members_unchanged = (
                    accepted_members == set(current_member_ids)
                    and all(member_id in accepted_snapshots for member_id in current_member_ids)
                    and all(
                        memory_matches_snapshot(current_members_by_id[member_id], accepted_snapshots[member_id])
                        for member_id in current_member_ids
                    )
                )
                if accepted_members_unchanged:
                    group_record["state"] = "already_covered_by_accepted"
                    group_record["accepted_memory_id"] = str(accepted_card.get("id"))
                    outcome.groups.append(group_record)
                    continue
                # Member set changed: propose a revision that retires only
                # the previous CARD on acceptance (never the members).
                revises_memory_id = str(accepted_card.get("id"))
                proposed_supersede = [revises_memory_id]

            if rollup_digest in pending:
                pending_card = pending[rollup_digest]
                outcome.name_rows([pending_card])
                group_record["state"] = "existing_candidate"
                group_record["candidate_memory_id"] = str(pending_card["id"])
                outcome.groups.append(group_record)
                outcome.candidate_ids.append(str(pending_card["id"]))
                continue

            expired_card = self._expired_card_for_digest(
                rollup_digest,
                rollup_key=group.rollup_key,
                domains=domains,
                sensitivity_allowed=sensitivity,
                projects=projects,
            )
            if expired_card is not None:
                outcome.name_rows([expired_card])
                group_record["state"] = "expired_card_members_unchanged"
                group_record["expired_memory_id"] = str(expired_card.get("id"))
                outcome.groups.append(group_record)
                continue

            held, held_card = self._row_at_digest_key(
                rollup_digest,
                rollup_key=group.rollup_key,
                domains=domains,
                sensitivity_allowed=sensitivity,
                projects=projects,
            )
            if held:
                if held_card is not None:
                    outcome.name_rows([held_card])
                    group_record["state"] = "existing_card_members_unchanged"
                    group_record["existing_memory_id"] = str(held_card.get("id"))
                    group_record["existing_status"] = str(held_card.get("status"))
                else:
                    group_record["state"] = "digest_key_held_by_another_row"
                outcome.groups.append(group_record)
                continue

            group_entity = group.rollup_key.removeprefix("entity:") if group.group_kind == "entity" else None
            instances = [
                _instance_record(row, exclude_normalized=group_entity)
                for row in group.members[: options.max_instances_per_card]
            ]
            if len(group.members) > options.max_instances_per_card:
                outcome.skipped.append(
                    f"instance_bound: {group.rollup_key} truncated to "
                    f"{options.max_instances_per_card} of {len(group.members)} instances"
                )

            model_summary: str | None = None
            model_provenance: JsonObject | None = None
            merge_refusal: str | None = None
            if generation_mode == "model_backed":
                if bounded:
                    merge_refusal = "model_summary_skipped_for_truncated_grouping_input"
                elif len(instances) < len(group.members):
                    merge_refusal = "model_summary_skipped_for_truncated_instances"
                else:
                    model_summary, model_provenance, merge_refusal = _refine_summary_with_model(
                        group=group,
                        instances=instances,
                        route=route,
                        provider=self.merge_provider,
                        temperature=model_temperature,
                        trace_id=trace_id,
                    )

            title, canonical_text, summary = self._render_card(
                group=group,
                instances=instances,
                model_summary=model_summary,
                grouping_input_truncated=bounded,
                grouping_input_count=len(rows),
                grouping_input_total=total_count,
                grouping_input_total_exact=total_exact,
            )

            proposal: JsonObject = {
                **group_record,
                "instance_count": len(group.members),
                "displayed_instance_count": len(instances),
                "instances_truncated": len(instances) < len(group.members),
                "grouping_input_truncated": bounded,
                "grouping_input_count": len(rows),
                "grouping_input_total": total_count,
                "grouping_input_total_exact": total_exact,
                "revises_memory_id": revises_memory_id,
                "proposed_supersede": proposed_supersede,
                "model_refined": model_summary is not None,
                "merge_refusal": merge_refusal,
                "source_refs": [f"memory:{member_id}" for member_id in current_member_ids],
            }
            if create_candidate_memories:
                candidate = self._create_rollup_candidate(
                    group=group,
                    instances=instances,
                    rollup_digest=rollup_digest,
                    title=title,
                    canonical_text=canonical_text,
                    summary=summary,
                    revises_memory=accepted_card,
                    proposed_supersede=proposed_supersede,
                    model_provenance=model_provenance,
                    merge_refusal=merge_refusal,
                    grouping_input_truncated=bounded,
                    grouping_input_count=len(rows),
                    grouping_input_total=total_count,
                    grouping_input_total_exact=total_exact,
                    generated_by=generated_by,
                    trace_id=trace_id,
                )
                proposal["candidate_memory_id"] = str(candidate["id"])
                proposal["candidate_state"] = "created" if revises_memory_id is None else "revision_proposed"
                outcome.candidate_ids.append(str(candidate["id"]))
            else:
                proposal["candidate_memory_id"] = None
                proposal["candidate_state"] = "not_created (create_candidate_memories=false)"

            group_record["state"] = proposal["candidate_state"]
            outcome.groups.append(group_record)
            outcome.proposals.append(proposal)

        return outcome


__all__ = [
    "DEFAULT_MAX_ROLLUPS",
    "DEFAULT_ROLLUP_MIN_MEMBERS",
    "DUPLICATE_GROUP_JACCARD_THRESHOLD",
    "GENERIC_ANCHOR_MIN_ROWS",
    "GENERIC_ANCHOR_MIN_SESSIONS",
    "GENERIC_ANCHOR_SESSION_DISPERSION",
    "LABEL_COHERENCE_MIN_FRACTION",
    "MAX_SEMANTIC_TIER_MEMBERS",
    "MIN_AGGREGATION_MEMBERS",
    "MIN_DISTINCT_INSTANCE_VALUES",
    "MIN_DISTINCT_SESSIONS",
    "ROLLUP_CANDIDATE_KIND",
    "ROLLUP_PROPOSAL_KIND",
    "SEMANTIC_MIN_MEAN_SIMILARITY",
    "SEMANTIC_MIN_PAIRWISE_SIMILARITY",
    "SEMANTIC_NEAR_DUPLICATE_SIMILARITY",
    "SEMANTIC_SIMILARITY_BLOCK_ROWS",
    "SEMANTIC_SWEEP_THRESHOLDS",
    "RollupOptions",
    "RollupOutcome",
    "VNextRollupService",
    "VNextRollupStore",
    "VNextRollupValidationError",
]
