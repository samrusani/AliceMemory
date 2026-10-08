"""Semantic (embedding) grouping tier for roll-up cards.

Round-4 measured the lexical ceiling: aggregation questions whose evidence
shares NO anchor token ("How many kitchen items did I replace or fix?" =
faucet/toaster/shelves) can never form a lexical-topic group. The semantic
tier clusters the rows the lexical/entity passes leave unclaimed by cosine
similarity over their embeddings — mock deterministic vectors here, the
same provider seam embed-on-write uses in production.

Everything in this file runs keyless: no env, no network, no API keys.
The provider is a mapping fixture and the vectors are hand-built.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import numpy as np
import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_derived_labels import identifier
from alicebot_api.vnext_embeddings import memory_embedding_text
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_rollups import (
    ROLLUP_CANDIDATE_KIND,
    SEMANTIC_MIN_MEAN_SIMILARITY,
    SEMANTIC_MIN_PAIRWISE_SIMILARITY,
    VNextRollupService,
    _topic_tokens,
)
from alicebot_api.vnext_source_fence import SourceReadFence


# -- fakes -----------------------------------------------------------------------


class SemanticFakeStore:
    """FakeRollupStore plus the two optional surfaces the semantic tier
    reads: ``update_memory_embedding`` (embed-on-write stand-in) and
    ``search_memories_vector`` (the stored-embeddings read probe)."""

    def __init__(self) -> None:
        self.memories: list[dict] = []
        self.embeddings: dict[str, list[float]] = {}
        self._counter = 0

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject:
        self._counter += 1
        row = {
            "id": memory.get("id") or str(uuid4()),
            "created_at": f"2026-07-01T00:{self._counter:02d}:00Z",
            "updated_at": f"2026-07-01T00:{self._counter:02d}:00Z",
            **{key: value for key, value in memory.items() if key != "id"},
        }
        self.memories.append(row)
        return dict(row)

    def list_memories(self, *, status: str | None = None) -> list[JsonObject]:
        return [dict(row) for row in self.memories if status is None or row.get("status") == status]

    def read_label_rows(self, kind: str, ids) -> list[JsonObject]:
        """Expose recorded inputs so an existing card's label can be verified."""
        if kind != "memory":
            return []
        wanted = {identifier(item) for item in ids}
        return [dict(row) for row in self.memories if identifier(row.get("id")) in wanted]

    @staticmethod
    def _in_scope(
        row: JsonObject,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
    ) -> bool:
        domain = str(row.get("domain") or "unknown")
        sensitivity = str(row.get("sensitivity") or "unknown")
        return (not domains or domain in {*domains, "unknown"}) and sensitivity in sensitivity_allowed

    def list_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        rows = [
            dict(row)
            for row in self.memories
            if row.get("status") in {"active", "accepted"}
            and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
            and not (
                isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == excluded_candidate_kind
            )
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))), reverse=True)
        return rows[:limit]

    def count_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
    ) -> int:
        return len(
            [
                row
                for row in self.memories
                if row.get("status") in {"active", "accepted"}
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and not (
                    isinstance(row.get("metadata_json"), dict)
                    and row["metadata_json"].get("candidate_kind") == excluded_candidate_kind
                )
            ]
        )

    def list_pending_rollup_candidates(
        self,
        *,
        rollup_digests: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        for digest in sorted(set(rollup_digests)):
            matches = [
                row
                for row in self.memories
                if row.get("status") == "candidate"
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_digest") == digest
            ]
            if matches:
                selected.append(
                    dict(
                        max(
                            matches,
                            key=lambda row: (
                                str(row.get("updated_at") or ""),
                                str(row.get("created_at") or ""),
                                str(row.get("id")),
                            ),
                        )
                    )
                )
        return selected[:limit]

    def list_accepted_rollup_cards(
        self,
        *,
        rollup_keys: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        for rollup_key in sorted(set(rollup_keys)):
            matches = [
                row
                for row in self.memories
                if row.get("status") in {"active", "accepted"}
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_key") == rollup_key
            ]
            if matches:
                active = [row for row in matches if row.get("status") == "active"]
                selected.append(
                    dict(
                        max(
                            active or matches,
                            key=lambda row: (
                                str(row.get("updated_at") or ""),
                                str(row.get("created_at") or ""),
                                str(row.get("id")),
                            ),
                        )
                    )
                )
        return selected[:limit]

    def update_memory_embedding(self, *, memory_id: str, vector: list[float]) -> JsonObject:
        self.embeddings[str(memory_id)] = [float(value) for value in vector]
        return {"id": str(memory_id)}

    def list_memory_ids_with_embeddings(self, ids) -> set[str]:
        return {str(value) for value in ids if str(value) in self.embeddings}

    def search_memories_vector(
        self,
        *,
        query_vector: list[float],
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 50,
        **kwargs,
    ) -> list[JsonObject]:
        query = np.asarray(query_vector, dtype=float)
        rows: list[JsonObject] = []
        for memory in self.memories:
            if memory.get("status") not in {"active", "accepted"}:
                continue
            stored = self.embeddings.get(str(memory["id"]))
            if stored is None:
                continue
            vector = np.asarray(stored, dtype=float)
            width = max(query.size, vector.size)
            padded_query = np.zeros(width)
            padded_query[: query.size] = query
            padded_vector = np.zeros(width)
            padded_vector[: vector.size] = vector
            denominator = float(np.linalg.norm(padded_query) * np.linalg.norm(padded_vector))
            similarity = float(padded_query @ padded_vector) / denominator if denominator else 0.0
            row = dict(memory)
            row["vector_distance"] = 1.0 - similarity
            rows.append(row)
        rows.sort(key=lambda row: (row["vector_distance"], str(row["id"])))
        return rows[:limit]


class PlainFakeStore(SemanticFakeStore):
    """Same rows/embeddings, but WITHOUT the embedding-presence read surface."""

    list_memory_ids_with_embeddings = None  # type: ignore[assignment]


class MappedEmbeddingProvider:
    provider = "test_embeddings"
    model = "test-embed-3"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.batch_calls = 0

    def embed_text(self, text: str) -> list[float]:
        return list(self.mapping[text])

    def embed_batch(self, texts) -> list[list[float]]:
        self.batch_calls += 1
        return [self.embed_text(text) for text in texts]


# -- fixtures ---------------------------------------------------------------------

# The round-4 measured miss shape (question gpt4_ab202e7f, "How many
# kitchen items did I replace or fix?"): three replaced kitchen items that
# share NO anchor token across all three texts, so no lexical-topic group
# can ever form. Each carries an amount and its own session (aggregation
# signal), and "kitchen" spans two of the three (dominant noun, but below
# the support >= 3 an anchor needs).
KITCHEN_SPECS = (
    ("kitchen-1", "Swapped the leaky kitchen faucet for $120", "2026-03-02"),
    ("kitchen-2", "The toaster in the kitchen died; its replacement cost $45", "2026-03-11"),
    ("kitchen-3", "Hung floating shelves over the counter, $60 in brackets", "2026-03-19"),
)

# Shared topic direction + per-item unique direction: pairwise cosine
# 0.81 / 0.9864 ~= 0.821 inside the topic (comfortably above the 0.80
# sweep point, below near-duplicate territory), ~0 against the unrelated
# rows.
KITCHEN_VECTORS = (
    [0.9, 0.42, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    [0.9, 0.0, 0.42, 0.0, 0.0, 0.0, 0.0, 0.0],
    [0.9, 0.0, 0.0, 0.42, 0.0, 0.0, 0.0, 0.0],
)

UNRELATED_SPECS = (
    ("other-1", "The staging database resets on Sunday nights", None),
    ("other-2", "Passport renewal appointment is confirmed", None),
    ("other-3", "The maple sapling doubled its height", None),
)
UNRELATED_VECTORS = (
    [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
)


def _seed(
    store,
    mapping: dict[str, list[float]],
    specs,
    vectors,
    *,
    ids: dict[str, str] | None = None,
    with_embedding: bool = True,
) -> dict[str, JsonObject]:
    rows: dict[str, JsonObject] = {}
    for (key, text, session_date), vector in zip(specs, vectors, strict=True):
        metadata: JsonObject = {}
        if session_date is not None:
            metadata["session_date"] = session_date
        memory: JsonObject = {
            "memory_key": f"memory.{key}",
            "value": {"text": text},
            "status": "active",
            "memory_type": "episode",
            "title": text[:80],
            "canonical_text": text,
            "summary": text[:80],
            "domain": "personal",
            "sensitivity": "internal",
            "metadata_json": metadata,
        }
        if ids is not None:
            memory["id"] = ids[key]
        row = store.create_memory(memory)
        mapping[memory_embedding_text(row)] = list(vector)
        if with_embedding:
            store.update_memory_embedding(memory_id=str(row["id"]), vector=list(vector))
        rows[key] = row
    return rows


def _rollup_candidates(store) -> list[JsonObject]:
    return [
        row
        for row in store.list_memories(status="candidate")
        if isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == ROLLUP_CANDIDATE_KIND
    ]


def _semantic_proposals(outcome) -> list[JsonObject]:
    return [proposal for proposal in outcome.proposals if proposal["group_kind"] == "semantic"]


# -- the round-4 measured miss: anchor-less topical clusters ------------------------


def test_kitchen_specs_share_no_anchor_token() -> None:
    """Fixture sanity: the lexical passes CANNOT group these members —
    no stopword-filtered stem spans all three texts, and none reaches the
    support >= 3 a lexical anchor needs."""
    token_sets = [_topic_tokens(text) for _, text, _ in KITCHEN_SPECS]
    assert set.intersection(*token_sets) == set()
    support: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            support[token] = support.get(token, 0) + 1
    assert max(support.values()) < 3


def test_anchorless_kitchen_items_form_exactly_one_gated_topical_card() -> None:
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    kitchen = _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS)
    unrelated = _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    provider = MappedEmbeddingProvider(mapping)

    outcome = VNextRollupService(store, embedding_provider=provider).propose_rollups()

    # Dormant lexical passes propose nothing here; the semantic tier
    # produces EXACTLY ONE gated topical card.
    semantic = _semantic_proposals(outcome)
    assert len(outcome.proposals) == 1
    assert len(semantic) == 1
    proposal = semantic[0]
    assert proposal["label"].casefold() == "kitchen"
    assert proposal["rollup_key"] == "semantic:kitchen"
    kitchen_ids = {str(row["id"]) for row in kitchen.values()}
    unrelated_ids = {str(row["id"]) for row in unrelated.values()}
    assert set(proposal["member_ids"]) == kitchen_ids
    assert set(proposal["member_ids"]) & unrelated_ids == set()

    # The gate held: >= 3 members, real aggregation signal, semantic
    # coherence measured and disclosed.
    aggregation = proposal["aggregation"]
    assert aggregation["distinct_values"] >= 3
    assert aggregation["distinct_sessions"] == 3
    assert aggregation["semantic_mean_similarity"] == pytest.approx(0.821, abs=0.01)
    assert aggregation["semantic_mean_similarity"] >= SEMANTIC_MIN_MEAN_SIMILARITY
    assert aggregation["semantic_min_similarity"] == pytest.approx(0.821, abs=0.01)
    assert aggregation["semantic_min_similarity"] >= SEMANTIC_MIN_PAIRWISE_SIMILARITY

    # The card: review-gated candidate, unit-bearing title, every
    # instance's amount and date present, members stay untouched.
    candidates = _rollup_candidates(store)
    assert len(candidates) == 1
    card = candidates[0]
    assert card["status"] == "candidate"
    assert card["metadata_json"]["review_required"] is True
    assert card["value"]["rollup"]["group_kind"] == "semantic"
    assert "kitchen" in card["title"]
    assert "amounts in $" in card["title"]
    for amount in ("$120", "$45", "$60"):
        assert amount in card["canonical_text"]
    for day in ("2026-03-02", "2026-03-11", "2026-03-19"):
        assert day in card["canonical_text"]
    for word in ("faucet", "toaster", "shelves"):
        assert word in card["canonical_text"]
    consolidation = card["metadata_json"]["consolidation"]
    assert consolidation["proposal_kind"] == "rollup"
    assert consolidation["proposed_supersede"] == []

    # Full disclosure: the tier, its sweep, and the chosen threshold are
    # all in the outcome metadata.
    metadata = outcome.to_metadata()
    assert metadata["grouping"] == "deterministic_entity_and_lexical_topic_plus_semantic_embedding"
    record = metadata["semantic_grouping"]
    assert record["embedding_access"] == "exact_id_presence_read_then_reuse_or_provider_reembed"
    assert record["provider"] == "test_embeddings"
    assert record["matrix_dtype"] == "float32"
    assert record["matrix_materialization"] is False
    assert record["peak_similarity_block_bytes"] == 6 * 6 * 4
    assert record["embedded_rows"] == 6
    assert record["clusters_formed"] == 1
    assert record["groups_admitted"] == 1
    # Identical partitions across the sweep tie on the criterion; the tie
    # breaks toward the HIGHEST (most conservative) threshold that still
    # holds the cluster together.
    assert record["chosen_threshold"] == pytest.approx(0.80)
    assert record["mean_silhouette"] is not None and record["mean_silhouette"] > 0
    assert any(entry["threshold"] == 0.85 and entry["usable_components"] == 0 for entry in record["threshold_sweep"])


def test_semantic_tier_runs_after_lexical_and_only_on_unclaimed_members() -> None:
    """Members a lexical pass claims never re-enter the semantic tier,
    even when their vectors are tight; the lexical group is unchanged
    from the dormant run."""
    game_specs = (
        ("game-1", "I played The Last of Us Part II for 30 hours", "2023-05-10"),
        ("game-2", "I played Hollow Knight for 25 hours", "2023-06-02"),
        ("game-3", "I played Stardew Valley for 85 hours", "2023-06-20"),
    )
    game_vectors = (
        [0.9, 0.45, 0.0, 0.0],
        [0.9, 0.0, 0.45, 0.0],
        [0.9, 0.0, 0.0, 0.45],
    )
    fixed_ids = {key: str(uuid4()) for key, _, _ in game_specs}

    def _build(provider_on: bool):
        store = SemanticFakeStore()
        mapping: dict[str, list[float]] = {}
        _seed(store, mapping, game_specs, game_vectors, ids=fixed_ids)
        provider = MappedEmbeddingProvider(mapping) if provider_on else None
        return store, VNextRollupService(store, embedding_provider=provider)

    store_dormant, service_dormant = _build(provider_on=False)
    dormant = service_dormant.propose_rollups()
    store_semantic, service_semantic = _build(provider_on=True)
    active = service_semantic.propose_rollups()

    assert len(dormant.proposals) == 1
    assert len(active.proposals) == 1
    assert _semantic_proposals(active) == []
    # The lexical group is untouched by the tier: same key, digest, label.
    for key in ("rollup_key", "rollup_digest", "label", "group_kind"):
        assert active.proposals[0][key] == dormant.proposals[0][key]
    # The tier ran, saw zero unclaimed rows, and disclosed that.
    assert active.semantic is not None
    assert active.semantic["ungrouped_rows"] == 0
    assert "fewer_ungrouped_rows_than_min_members" in active.semantic["skipped"]


# -- gate enforcement on semantic groups ---------------------------------------------


def test_semantic_cluster_without_aggregation_signal_is_dropped() -> None:
    """Tight vectors alone are not a card: three anchor-less members with
    no amounts, no dates, and one shared day aggregate nothing."""
    specs = (
        ("flat-1", "The garden hose reel sits by the gate", "2026-03-02"),
        ("flat-2", "A watering can waits under the porch light", "2026-03-02"),
        ("flat-3", "The sprinkler head points at the garden fence", "2026-03-02"),
    )
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, specs, KITCHEN_VECTORS)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    outcome = VNextRollupService(store, embedding_provider=MappedEmbeddingProvider(mapping)).propose_rollups()

    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.quality_gate["dropped_by_reason"]["no_aggregation_signal"] >= 1
    assert outcome.semantic is not None and outcome.semantic["groups_admitted"] == 0


def test_semantic_chain_below_similarity_floor_is_dropped() -> None:
    """Single-linkage can chain loosely related rows through a middle row;
    the mean-pairwise-similarity floor (the semantic substitute for label
    coherence) rejects the chained cluster."""
    specs = (
        ("chain-1", "Toured the harbor museum for $12", "2026-04-01"),
        ("chain-2", "The harbor ferry ticket cost $8", "2026-04-08"),
        ("chain-3", "Bought a museum guidebook for $9", "2026-04-15"),
    )
    # sim(1,2) = sim(2,3) ~= 0.707, sim(1,3) = 0 -> mean ~= 0.471 < floor.
    chain_vectors = (
        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.7071, 0.7071, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, specs, chain_vectors)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    outcome = VNextRollupService(store, embedding_provider=MappedEmbeddingProvider(mapping)).propose_rollups()

    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.semantic is not None
    assert outcome.semantic["clustering"] == "complete_link_all_pairs_at_threshold"
    assert "no_usable_clusters_at_any_threshold" in outcome.semantic["skipped"]


def test_semantic_cluster_without_dominant_noun_is_dropped() -> None:
    """A cluster where no noun spans even two members has no recognizable
    topic; it is dropped with its own disclosed reason."""
    specs = (
        ("solo-1", "Refilled the propane tank for $30", "2026-05-01"),
        ("solo-2", "Patched the trampoline mat for $22", "2026-05-08"),
        ("solo-3", "Sharpened the mower blade for $15", "2026-05-15"),
    )
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, specs, KITCHEN_VECTORS)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    outcome = VNextRollupService(store, embedding_provider=MappedEmbeddingProvider(mapping)).propose_rollups()

    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.quality_gate["dropped_by_reason"]["semantic_no_dominant_label"] >= 1


def test_near_duplicate_similarity_cluster_is_left_to_dedup() -> None:
    """Clusters at near-duplicate cosine belong to the consolidation
    dedup/merge pipeline, not to a roll-up card. The paraphrases below
    share no anchor token, so only the semantic tier sees them — and its
    near-duplicate guard hands them off."""
    specs = (
        ("dup-1", "Sam prefers oat milk lattes in the morning", "2026-06-01"),
        ("dup-2", "Every dawn begins with that same creamy espresso drink", "2026-06-08"),
        ("dup-3", "A plant-based breve is his daily sunrise ritual", "2026-06-15"),
    )
    dup_vectors = (
        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.99, 0.14, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.99, 0.0, 0.14, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, specs, dup_vectors)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    outcome = VNextRollupService(store, embedding_provider=MappedEmbeddingProvider(mapping)).propose_rollups()

    assert _semantic_proposals(outcome) == []
    assert any(reason.startswith("semantic_near_duplicate_left_to_dedup") for reason in outcome.skipped)


# -- dormancy: byte identity without a provider --------------------------------------


ROUND4_METADATA_KEYS = [
    "grouping",
    "options",
    "groupable_memories",
    "groupable_memories_total",
    "groupable_memories_total_exact",
    "bounded",
    "groups",
    "proposals",
    "skipped",
    "quality_gate",
]


def test_dormant_without_provider_is_byte_identical(monkeypatch) -> None:
    """No embedding provider -> the tier does not exist observably: the
    outcome metadata carries exactly the round-4 keys (no semantic keys,
    no skip lines, round-4 grouping string), even when the STORE holds
    stored embeddings. Ambient (env-unset) and explicit-None construction
    produce byte-identical metadata and identical cards."""
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    fixed_ids = {key: str(uuid4()) for key, _, _ in KITCHEN_SPECS}

    def _run(embedding_provider):
        store = SemanticFakeStore()
        mapping: dict[str, list[float]] = {}
        _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS, ids=fixed_ids)
        outcome = VNextRollupService(store, embedding_provider=embedding_provider).propose_rollups()
        return outcome, _rollup_candidates(store)

    ambient_outcome, ambient_cards = _run("ambient")
    explicit_outcome, explicit_cards = _run(None)

    ambient_json = json.dumps(ambient_outcome.to_metadata(), sort_keys=True, default=str)
    explicit_json = json.dumps(explicit_outcome.to_metadata(), sort_keys=True, default=str)
    assert ambient_json == explicit_json
    assert "semantic" not in ambient_json
    assert list(ambient_outcome.to_metadata().keys()) == ROUND4_METADATA_KEYS
    assert ambient_outcome.to_metadata()["grouping"] == "deterministic_entity_and_lexical_topic"
    assert ambient_outcome.semantic is None
    assert ambient_cards == [] and explicit_cards == []
    # The dormant path never touches the vector surface either.
    assert not any("semantic" in reason for reason in ambient_outcome.skipped)


def test_store_without_embedding_presence_read_discloses_and_adds_nothing() -> None:
    """Provider configured but the store has no embedding-presence read
    surface: the tier discloses the skip and proposes nothing semantic."""
    store = PlainFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS)
    outcome = VNextRollupService(store, embedding_provider=MappedEmbeddingProvider(mapping)).propose_rollups()

    assert _semantic_proposals(outcome) == []
    assert outcome.semantic is not None
    assert "store_lacks_embedding_presence_read" in outcome.semantic["skipped"]
    assert any(
        "semantic_tier: store_lacks_embedding_presence_read" in reason for reason in outcome.skipped
    )


def test_rows_without_stored_embeddings_never_join_a_cluster() -> None:
    """The probe is the read surface for stored embeddings: rows without
    one (embed-on-write missed them) cannot join a semantic cluster."""
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    rows = _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS)
    # Drop one stored vector: only two embedded rows remain (< 3).
    del store.embeddings[str(rows["kitchen-3"]["id"])]
    provider = MappedEmbeddingProvider(mapping)
    outcome = VNextRollupService(store, embedding_provider=provider).propose_rollups()

    assert outcome.proposals == []
    assert outcome.semantic is not None
    assert outcome.semantic["embedded_rows"] == 2
    assert "fewer_embedded_rows_than_min_members" in outcome.semantic["skipped"]
    # Presence is checked before provider work, so an ineligible third row
    # never consumes embedding quota (and this early exit consumes none).
    assert provider.batch_calls == 0


def test_embedding_presence_is_read_before_any_provider_call() -> None:
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS, with_embedding=False)
    provider = MappedEmbeddingProvider(mapping)

    outcome = VNextRollupService(store, embedding_provider=provider).propose_rollups()

    assert provider.batch_calls == 0
    assert outcome.semantic is not None
    assert outcome.semantic["embedded_rows"] == 0
    assert "fewer_embedded_rows_than_min_members" in outcome.semantic["skipped"]


# -- determinism ----------------------------------------------------------------------


def test_semantic_grouping_is_deterministic_across_insertion_order_and_reruns() -> None:
    fixed_ids = {key: str(uuid4()) for key, _, _ in (*KITCHEN_SPECS, *UNRELATED_SPECS)}

    def _build(order: tuple[int, ...]):
        store = SemanticFakeStore()
        mapping: dict[str, list[float]] = {}
        all_specs = (*KITCHEN_SPECS, *UNRELATED_SPECS)
        all_vectors = (*KITCHEN_VECTORS, *UNRELATED_VECTORS)
        specs = tuple(all_specs[index] for index in order)
        vectors = tuple(all_vectors[index] for index in order)
        _seed(store, mapping, specs, vectors, ids=fixed_ids)
        return store, MappedEmbeddingProvider(mapping)

    store_a, provider_a = _build(order=(0, 1, 2, 3, 4, 5))
    store_b, provider_b = _build(order=(5, 2, 4, 0, 3, 1))
    service_a = VNextRollupService(store_a, embedding_provider=provider_a)
    VNextRollupService(store_b, embedding_provider=provider_b).propose_rollups()
    service_a.propose_rollups()

    card_a = _rollup_candidates(store_a)[0]
    card_b = _rollup_candidates(store_b)[0]
    assert card_a["metadata_json"]["rollup_digest"] == card_b["metadata_json"]["rollup_digest"]
    assert card_a["metadata_json"]["rollup_key"] == card_b["metadata_json"]["rollup_key"] == "semantic:kitchen"
    assert card_a["canonical_text"] == card_b["canonical_text"]
    assert card_a["title"] == card_b["title"]

    # Double run: the pending candidate is recognized, nothing new written.
    second = service_a.propose_rollups()
    assert second.proposals == []
    assert len(_rollup_candidates(store_a)) == 1
    assert any(group["state"] == "existing_candidate" for group in second.groups)


# -- consolidation workflow integration ------------------------------------------------


class ConsolidationShimStore(SemanticFakeStore):
    """SemanticFakeStore plus the artifact/event surface the consolidation
    service needs."""

    def __init__(self) -> None:
        super().__init__()
        self.artifacts: list[dict] = []
        self.events: list[dict] = []

    def append_event(self, event: JsonObject) -> JsonObject:
        self.events.append(dict(event))
        return dict(event)

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
        row = {"id": str(uuid4()), **artifact}
        self.artifacts.append(row)
        return dict(row)

    def list_events(self, **kwargs) -> list[JsonObject]:
        return [dict(event) for event in self.events]


def test_consolidation_workflow_runs_semantic_tier_and_discloses() -> None:
    """Full scheduled-workflow shape: near-duplicates go to the dedup
    proposal, the anchor-less kitchen trio becomes one semantic roll-up
    card, and the artifact metadata discloses the tier end to end."""
    from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService

    store = ConsolidationShimStore()
    mapping: dict[str, list[float]] = {}
    dup_specs = (
        ("dup-1", "Sam prefers oat milk lattes in the morning", None),
        ("dup-2", "Sam prefers oat milk lattes every morning before standup", None),
        ("dup-3", "Sam usually orders an oat milk latte in the mornings", None),
    )
    dup_vectors = (
        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.98, 0.09, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.97, 0.12, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    kitchen_vectors = tuple([0.0, 0.0, 0.0, 0.0, *vector[:4]] for vector in KITCHEN_VECTORS)
    _seed(store, mapping, dup_specs, dup_vectors)
    _seed(store, mapping, KITCHEN_SPECS, kitchen_vectors)

    artifact = VNextConsolidationService(
        store, embedding_provider=(provider := MappedEmbeddingProvider(mapping))
    ).generate_memory_consolidation(MemoryConsolidationRequest(agent_identity=None, ))

    rollups_metadata = artifact["metadata_json"]["rollups"]
    assert rollups_metadata["enabled"] is True
    assert rollups_metadata["grouping"] == "deterministic_entity_and_lexical_topic_plus_semantic_embedding"
    assert rollups_metadata["semantic_grouping"]["groups_admitted"] == 1
    assert rollups_metadata["semantic_grouping"]["reused_embedding_rows"] == 6
    assert rollups_metadata["semantic_grouping"]["provider_embedded_rows"] == 0
    # One consolidation batch; rollups consume the cache and make no second
    # provider call for the same selected memories.
    assert provider.batch_calls == 1
    semantic = [p for p in rollups_metadata["proposals"] if p["group_kind"] == "semantic"]
    assert len(semantic) == 1
    assert semantic[0]["label"].casefold() == "kitchen"
    # The near-duplicate cluster went to the dedup pipeline, not to a card.
    consolidation_metadata = artifact["metadata_json"]["consolidation"]
    assert len(consolidation_metadata["cluster_membership"]) == 1
    assert "Semantic tier (embeddings)" in artifact["content_markdown"]

    # Review-only: members untouched; the only writes are candidates.
    for row in store.memories:
        if row["metadata_json"].get("candidate_kind"):
            continue
        assert row["status"] == "active"


def test_consolidation_without_provider_keeps_rollups_dormant() -> None:
    """embedding_provider=None flows through the wiring: clustering skips
    AND the semantic tier stays dormant with round-4 metadata."""
    from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService

    store = ConsolidationShimStore()
    mapping: dict[str, list[float]] = {}
    _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS)
    artifact = VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(
        MemoryConsolidationRequest(agent_identity=None, )
    )
    rollups_metadata = artifact["metadata_json"]["rollups"]
    assert rollups_metadata["enabled"] is True
    assert rollups_metadata["grouping"] == "deterministic_entity_and_lexical_topic"
    assert "semantic_grouping" not in rollups_metadata


# -- live sqlite: the accepted semantic card serves aggregation recall -----------------


def test_accepted_semantic_card_enters_aggregation_recall(monkeypatch) -> None:
    """The full product loop on live SQLite with mock vectors: propose ->
    review-accept -> the card is a first-class memory that surfaces for
    the aggregation query the members individually under-serve."""
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "semantic@example.com", "Semantic User")
    store = SQLiteVNextStore(conn, user_id)

    mapping: dict[str, list[float]] = {}
    members = _seed(store, mapping, KITCHEN_SPECS, KITCHEN_VECTORS)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    provider = MappedEmbeddingProvider(mapping)

    outcome = VNextRollupService(store, embedding_provider=provider).propose_rollups()
    assert len(_semantic_proposals(outcome)) == 1
    candidate_id = str(_rollup_candidates(store)[0]["id"])

    result = VNextMemoryCommitService(store).accept_consolidation_candidate(
        candidate_id, reason="Reviewed the kitchen items roll-up."
    )
    assert result["status"] == "accepted"
    assert result["superseded_member_ids"] == []
    for row in members.values():
        assert store.get_memory(str(row["id"]))["status"] == "active"

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(
            query="How many kitchen items did I replace or fix?", max_items=8, actor_type="system"
        ),
        source_fence=SourceReadFence.unfenced()
    )
    ranked_ids = [str(memory.get("id")) for memory in pack.get("relevant_memories") or []]
    assert candidate_id in ranked_ids
    conn.close()
