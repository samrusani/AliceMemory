from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import sqlite3
from uuid import uuid4

import numpy as np
import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_consolidation import (
    MAX_EMBEDDED_MEMORIES_HARD_CAP,
    MAX_PAIRWISE_COMPARISONS,
    MAX_SIMILARITY_MATRIX_BYTES,
    SIMILARITY_BLOCK_ROWS,
    MemoryConsolidationRequest,
    VNextConsolidationService,
    VNextConsolidationValidationError,
)
from alicebot_api.vnext_embeddings import VNextEmbeddingProviderError, memory_embedding_text
from alicebot_api.vnext_repositories import JsonObject


# -- fakes ---------------------------------------------------------------------


class FakeConsolidationStore:
    """In-memory store mirroring the surface the service needs.

    Deliberately does NOT implement list_artifacts / list_artifact_quality_ratings
    so the optional-surface guards are exercised on every test.
    """

    def __init__(self) -> None:
        self.memories: list[dict] = []
        self.artifacts: list[dict] = []
        self.events: list[dict] = []
        self.embeddings: dict[str, list[float]] = {}
        self.list_memory_calls: list[dict[str, object]] = []
        self._clock = datetime(2026, 7, 1, tzinfo=UTC)

    def _next_timestamp(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.isoformat()

    def append_event(self, event: JsonObject) -> JsonObject:
        self.events.append(dict(event))
        return dict(event)

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
        row = {"id": str(uuid4()), **artifact}
        self.artifacts.append(row)
        return dict(row)

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject:
        timestamp = self._next_timestamp()
        row = {"id": str(uuid4()), "created_at": timestamp, "updated_at": timestamp, **memory}
        self.memories.append(row)
        return dict(row)

    def list_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        projects=None,
        limit: int | None = None,
    ) -> list[JsonObject]:
        self.list_memory_calls.append(
            {
                "status": status,
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
                "projects": projects,
                "limit": limit,
            }
        )
        rows = [row for row in self.memories if status is None or row.get("status") == status]
        if domains:
            rows = [row for row in rows if row.get("domain") in {*domains, "unknown"}]
        if sensitivity_allowed is not None:
            rows = [row for row in rows if row.get("sensitivity", "unknown") in sensitivity_allowed]
        if projects:
            rows = [row for row in rows if row.get("project_id") in set(projects)]
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows]

    def count_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        projects=None,
    ) -> int:
        rows = [row for row in self.memories if status is None or row.get("status") == status]
        if domains:
            rows = [row for row in rows if row.get("domain") in {*domains, "unknown"}]
        if sensitivity_allowed is not None:
            rows = [row for row in rows if row.get("sensitivity", "unknown") in sensitivity_allowed]
        if projects:
            rows = [row for row in rows if row.get("project_id") in set(projects)]
        return len(rows)

    def list_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        limit: int,
        projects=(),
    ) -> list[JsonObject]:
        rows = [
            row
            for row in self.memories
            if row.get("status") in {"active", "accepted"}
            and (not domains or row.get("domain") in {*domains, "unknown"})
            and row.get("sensitivity", "unknown") in sensitivity_allowed
            and not (
                isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == excluded_candidate_kind
            )
            and (not projects or row.get("project_id") in set(projects))
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))), reverse=True)
        return [dict(row) for row in rows[:limit]]

    def list_pending_rollup_candidates(
        self,
        *,
        rollup_digests: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
        projects=(),
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        for digest in sorted(set(rollup_digests)):
            matches = [
                row
                for row in self.memories
                if row.get("status") == "candidate"
                and (not domains or row.get("domain") in {*domains, "unknown"})
                and row.get("sensitivity", "unknown") in sensitivity_allowed
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_digest") == digest
                and (not projects or row.get("project_id") in set(projects))
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
        projects=(),
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        for rollup_key in sorted(set(rollup_keys)):
            matches = [
                row
                for row in self.memories
                if row.get("status") in {"active", "accepted"}
                and (not domains or row.get("domain") in {*domains, "unknown"})
                and row.get("sensitivity", "unknown") in sensitivity_allowed
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_key") == rollup_key
                and (not projects or row.get("project_id") in set(projects))
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

    def list_events(self, **kwargs) -> list[JsonObject]:
        return [dict(event) for event in self.events]

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


class MappedEmbeddingProvider:
    provider = "test_embeddings"
    model = "test-embed-3"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.batch_calls = 0
        self.batch_texts: list[str] = []

    def embed_text(self, text: str) -> list[float]:
        return list(self.mapping[text])

    def embed_batch(self, texts) -> list[list[float]]:
        self.batch_calls += 1
        self.batch_texts.extend(str(text) for text in texts)
        return [self.embed_text(text) for text in texts]


class StubMergeModelProvider:
    provider = "stub_model"
    model = "stub-merge-1"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def chat(self, *, prompt: str, temperature: float) -> str:
        self.prompts.append(prompt)
        return self.response

    def summarize(self, *, text: str) -> str:
        return text[:100]

    def structured_extract(self, *, text: str, schema_name: str) -> JsonObject:
        return {"schema": schema_name}

    def classify(self, *, text: str, labels) -> str:
        return labels[0]

    def embed(self, *, text: str) -> list[float]:
        return [0.0]


NEAR_DUP_VECTORS = (
    [1.0, 0.0, 0.0],
    [0.98, 0.09, 0.0],
    [0.97, 0.12, 0.0],
)
DISTINCT_VECTORS = (
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
    [0.7, -0.7, 0.0],
)


def _seed_memory(
    store,
    mapping: dict[str, list[float]],
    *,
    vector: list[float],
    title: str,
    canonical_text: str,
    memory_type: str = "semantic",
    source_event_ids: list[str] | None = None,
    source_id: str | None = None,
    project_scope: tuple[str, ...] = (),
    with_embedding: bool = True,
) -> JsonObject:
    row = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": canonical_text},
            "status": "active",
            "memory_type": memory_type,
            "title": title,
            "canonical_text": canonical_text,
            "summary": canonical_text[:80],
            "domain": "project",
            "sensitivity": "internal",
            "source_event_ids": source_event_ids or [],
            "project_id": project_scope[0] if len(project_scope) == 1 else None,
            "metadata_json": {
                "source_refs": [f"source:{source_id}"] if source_id is not None else [],
                "project_scope": list(project_scope),
            },
        }
    )
    mapping[memory_embedding_text(row)] = list(vector)
    if with_embedding:
        store.update_memory_embedding(memory_id=str(row["id"]), vector=vector)
    return row


def _seed_six_memories(store, mapping, **near_dup_kwargs) -> tuple[list[JsonObject], list[JsonObject]]:
    near_dups = [
        _seed_memory(
            store,
            mapping,
            vector=NEAR_DUP_VECTORS[index],
            title=f"Coffee preference v{index}",
            canonical_text=text,
            **near_dup_kwargs,
        )
        for index, text in enumerate(
            (
                "Sam prefers oat milk lattes in the morning",
                "Sam prefers oat milk lattes every morning before standup",
                "Sam usually orders an oat milk latte in the mornings",
            )
        )
    ]
    distinct = [
        _seed_memory(
            store,
            mapping,
            vector=DISTINCT_VECTORS[index],
            title=f"Distinct fact {index}",
            canonical_text=text,
        )
        for index, text in enumerate(
            (
                "The API deploys from the main branch on Fridays",
                "Northwind Capital quarterly review happens in October",
                "The staging database is reset every Sunday night",
            )
        )
    ]
    return near_dups, distinct


def _consolidation_candidates(store) -> list[JsonObject]:
    return [
        row
        for row in store.list_memories(status="candidate")
        if isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == "memory_consolidation"
    ]


def _service(store, mapping) -> VNextConsolidationService:
    return VNextConsolidationService(store, embedding_provider=MappedEmbeddingProvider(mapping))


# -- validation ------------------------------------------------------------------


def test_invalid_similarity_threshold_is_rejected() -> None:
    service = VNextConsolidationService(FakeConsolidationStore(), embedding_provider=None)
    with pytest.raises(VNextConsolidationValidationError):
        service.generate_memory_consolidation(MemoryConsolidationRequest(similarity_threshold=1.5))
    with pytest.raises(VNextConsolidationValidationError):
        service.generate_memory_consolidation(MemoryConsolidationRequest(similarity_threshold=0.0))


def test_invalid_metadata_option_overrides_are_rejected() -> None:
    service = VNextConsolidationService(FakeConsolidationStore(), embedding_provider=None)
    with pytest.raises(VNextConsolidationValidationError):
        service.generate_memory_consolidation(
            MemoryConsolidationRequest(metadata_json={"consolidation_options": {"similarity_threshold": "high"}})
        )
    with pytest.raises(VNextConsolidationValidationError):
        service.generate_memory_consolidation(
            MemoryConsolidationRequest(metadata_json={"consolidation_options": {"max_embedded_memories": 50000}})
        )


def test_max_embedded_memories_request_field_cannot_exceed_hard_cap() -> None:
    service = VNextConsolidationService(FakeConsolidationStore(), embedding_provider=None)
    with pytest.raises(VNextConsolidationValidationError):
        service.generate_memory_consolidation(
            MemoryConsolidationRequest(max_embedded_memories=MAX_EMBEDDED_MEMORIES_HARD_CAP + 1)
        )


def test_resource_guards_cover_the_entire_allowed_corpus() -> None:
    assert MAX_SIMILARITY_MATRIX_BYTES == MAX_EMBEDDED_MEMORIES_HARD_CAP**2 * 4
    assert MAX_PAIRWISE_COMPARISONS == (
        MAX_EMBEDDED_MEMORIES_HARD_CAP * (MAX_EMBEDDED_MEMORIES_HARD_CAP - 1) // 2
    )


# -- deterministic clustering ------------------------------------------------------


def test_near_duplicates_produce_one_dedup_candidate_with_correct_members() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    near_dups, distinct = _seed_six_memories(store, mapping)
    provider = MappedEmbeddingProvider(mapping)
    artifact = VNextConsolidationService(store, embedding_provider=provider).generate_memory_consolidation(
        MemoryConsolidationRequest()
    )

    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    candidate = candidates[0]
    consolidation = candidate["metadata_json"]["consolidation"]
    expected_member_ids = sorted(str(row["id"]) for row in near_dups)
    assert consolidation["cluster_member_ids"] == expected_member_ids
    assert consolidation["proposal_kind"] == "dedup"
    assert candidate["status"] == "candidate"
    assert candidate["trust_class"] == "deterministic"

    # Survivor is the longest canonical text; the dedup candidate copies it verbatim.
    survivor = max(near_dups, key=lambda row: (len(row["canonical_text"]), str(row["id"])))
    assert consolidation["survivor_memory_id"] == str(survivor["id"])
    assert candidate["canonical_text"] == survivor["canonical_text"]
    assert sorted(consolidation["proposed_supersede"]) == expected_member_ids
    assert consolidation["similarity_stats"]["min"] >= 0.88
    assert consolidation["reviewer_instructions"]

    # Provenance: candidate links back to every member.
    refs = candidate["metadata_json"]["source_refs"]
    for member_id in expected_member_ids:
        assert f"memory:{member_id}" in refs

    # Review-first guarantee: no member was superseded or mutated.
    for row in store.list_memories(status="active"):
        assert row["status"] == "active"

    metadata = artifact["metadata_json"]
    assert metadata["input_counts"]["clusters"] == 1
    assert metadata["input_counts"]["proposals"] == 1
    assert metadata["candidate_memory_ids"] == [str(candidate["id"])]
    assert (
        metadata["consolidation"]["embedding_access"]
        == "exact_id_presence_read_then_provider_reembed_reused_by_rollups"
    )
    assert metadata["consolidation"]["clustering"] == "complete_link_all_pairs_at_threshold"
    resource_guard = metadata["consolidation"]["resource_guard"]
    assert resource_guard["matrix_dtype"] == "float32"
    assert resource_guard["matrix_materialization"] is False
    assert resource_guard["peak_similarity_block_bytes"] == 6 * 6 * 4
    assert resource_guard["matrix_bytes_hard_cap"] == MAX_SIMILARITY_MATRIX_BYTES
    assert resource_guard["pairwise_comparisons"] == 15
    assert resource_guard["pairwise_comparisons_hard_cap"] == MAX_PAIRWISE_COMPARISONS
    assert resource_guard["pair_index_materialization"] is False
    assert "## Near-Duplicate Clusters" in artifact["content_markdown"]
    assert "## Merge / Dedup Proposals" in artifact["content_markdown"]
    assert "## Skipped / Bounds" in artifact["content_markdown"]
    assert "Review this consolidation candidate before promoting" not in artifact["content_markdown"]
    assert any(event["event_type"] == "memory.consolidation.generated" for event in store.events)
    # distinct memories must not appear in the cluster
    for row in distinct:
        assert str(row["id"]) not in consolidation["cluster_member_ids"]


def test_consolidation_never_admits_a_single_link_bridge_chain() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    rows = [
        _seed_memory(
            store,
            mapping,
            vector=vector,
            title=f"Chain {index}",
            canonical_text=f"Bridge chain memory {index}",
        )
        for index, vector in enumerate(
            (
                [1.0, 0.0],
                [0.7071, 0.7071],
                [0.0, 1.0],
            )
        )
    ]

    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(
            similarity_threshold=0.70,
            create_candidate_memories=False,
            propose_rollups=False,
        )
    )

    memberships = artifact["metadata_json"]["consolidation"]["cluster_membership"]
    assert len(memberships) == 1
    assert len(memberships[0]) == 2
    assert set(memberships[0]) < {str(row["id"]) for row in rows}
    proposal = artifact["metadata_json"]["consolidation"]["proposals"][0]
    assert proposal["similarity_stats"]["min"] >= 0.70
    assert proposal["similarity_stats"]["cohesion"] == "complete_link_all_pairs_at_threshold"


def test_similarity_work_uses_float32_blocks_instead_of_a_dense_matrix() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    member_count = SIMILARITY_BLOCK_ROWS + 12
    for index in range(member_count):
        _seed_memory(
            store,
            mapping,
            vector=[1.0, 0.0, 0.0, 0.0],
            title=f"Memory {index}",
            canonical_text=f"Repeated but versioned memory {index}",
        )

    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(
            max_embedded_memories=member_count,
            create_candidate_memories=False,
            propose_rollups=False,
        )
    )

    guard = artifact["metadata_json"]["consolidation"]["resource_guard"]
    assert guard["matrix_dtype"] == "float32"
    assert guard["matrix_materialization"] is False
    assert guard["similarity_block_rows"] == SIMILARITY_BLOCK_ROWS
    assert guard["peak_similarity_block_bytes"] == SIMILARITY_BLOCK_ROWS * member_count * 4
    assert guard["peak_similarity_block_bytes"] < member_count * member_count * 4


def test_rerun_with_same_input_set_creates_no_duplicate_candidate() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    service = _service(store, mapping)
    first = service.generate_memory_consolidation(MemoryConsolidationRequest())
    second = service.generate_memory_consolidation(MemoryConsolidationRequest())

    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    assert second["metadata_json"]["candidate_memory_ids"] == first["metadata_json"]["candidate_memory_ids"]
    proposals = second["metadata_json"]["consolidation"]["proposals"]
    assert proposals[0]["candidate_state"] == "existing"
    assert second["metadata_json"]["consolidation_digest"] == first["metadata_json"]["consolidation_digest"]


def test_exact_report_idempotency_survives_decoys_and_tracks_behavior_config() -> None:
    class ExactArtifactStore(FakeConsolidationStore):
        def list_artifacts(self, **kwargs) -> list[JsonObject]:
            rows = list(self.artifacts)
            artifact_type = kwargs.get("artifact_type")
            if artifact_type is not None:
                rows = [row for row in rows if row.get("artifact_type") == artifact_type]
            return [dict(row) for row in rows[: int(kwargs.get("limit", 8))]]

        def find_artifact_by_workflow_digest(
            self,
            *,
            artifact_type: str,
            workflow: str,
            digest: str,
            scope_projects=None,
        ) -> JsonObject | None:
            del scope_projects
            for row in self.artifacts:
                metadata = row.get("metadata_json")
                if (
                    row.get("artifact_type") == artifact_type
                    and isinstance(metadata, dict)
                    and metadata.get("workflow") == workflow
                    and metadata.get("consolidation_digest") == digest
                ):
                    return dict(row)
            return None

    store = ExactArtifactStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    for index in range(150):
        store.artifacts.append(
            {
                "id": f"decoy-{index}",
                "artifact_type": "memory_consolidation",
                "metadata_json": {
                    "workflow": "memory_consolidation",
                    "consolidation_digest": f"decoy-{index}",
                },
            }
        )
    service = _service(store, mapping)
    request = MemoryConsolidationRequest(propose_rollups=False, max_clusters=20)

    first = service.generate_memory_consolidation(request)
    candidate_count = len(_consolidation_candidates(store))
    second = service.generate_memory_consolidation(request)
    changed = service.generate_memory_consolidation(
        MemoryConsolidationRequest(propose_rollups=False, max_clusters=19)
    )

    assert second["id"] == first["id"]
    assert len(_consolidation_candidates(store)) == candidate_count
    assert changed["id"] != first["id"]
    assert changed["metadata_json"]["consolidation_digest"] != first["metadata_json"]["consolidation_digest"]


def test_without_embedding_provider_clustering_is_skipped_review_only() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    service = VNextConsolidationService(store, embedding_provider=None)
    artifact = service.generate_memory_consolidation(MemoryConsolidationRequest())

    assert _consolidation_candidates(store) == []
    assert "no_embedding_provider_configured" in artifact["metadata_json"]["consolidation"]["skipped"]
    assert "no_embedding_provider_configured" in artifact["content_markdown"]


def test_embedding_presence_failure_uses_static_skip_reason() -> None:
    sentinel = "UNIQUE_CONSOLIDATION_PRESENCE_EXCEPTION_SENTINEL"

    class FailingPresenceStore(FakeConsolidationStore):
        def list_memory_ids_with_embeddings(self, ids) -> set[str]:
            raise RuntimeError(sentinel)

    store = FailingPresenceStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)

    artifact = VNextConsolidationService(
        store, embedding_provider=MappedEmbeddingProvider(mapping)
    ).generate_memory_consolidation(MemoryConsolidationRequest())

    skipped = artifact["metadata_json"]["consolidation"]["skipped"]
    assert "embedding_presence_read_failed" in skipped
    assert sentinel not in str(artifact)


def test_embedding_provider_failure_uses_static_skip_reason() -> None:
    sentinel = "UNIQUE_CONSOLIDATION_PROVIDER_EXCEPTION_SENTINEL"

    class FailingProvider(MappedEmbeddingProvider):
        def embed_batch(self, texts) -> list[list[float]]:
            raise VNextEmbeddingProviderError(sentinel)

    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)

    artifact = VNextConsolidationService(
        store, embedding_provider=FailingProvider(mapping)
    ).generate_memory_consolidation(MemoryConsolidationRequest())

    skipped = artifact["metadata_json"]["consolidation"]["skipped"]
    assert "embedding_provider_failed" in skipped
    assert sentinel not in str(artifact)


def test_embedded_rows_are_counted_even_when_the_ann_probe_misses_them() -> None:
    # Audit 2 P1 #5: embedding presence must come from an exact-ID read, not a
    # global ANN probe. Here the probe surfaces nothing (older, unrelated
    # neighbors dominated the top-K), yet every selected near-duplicate has a
    # stored embedding and must still cluster.
    class AnnBlindStore(FakeConsolidationStore):
        def search_memories_vector(self, **kwargs):  # type: ignore[override]
            return []

    store = AnnBlindStore()
    mapping: dict[str, list[float]] = {}
    near_dups, _distinct = _seed_six_memories(store, mapping)
    provider = MappedEmbeddingProvider(mapping)
    artifact = VNextConsolidationService(store, embedding_provider=provider).generate_memory_consolidation(
        MemoryConsolidationRequest()
    )

    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    assert candidates[0]["metadata_json"]["consolidation"]["cluster_member_ids"] == sorted(
        str(row["id"]) for row in near_dups
    )
    assert artifact["metadata_json"]["input_counts"]["embedded_memories"] == 6


def test_memories_without_stored_embeddings_are_excluded() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    # Third near-dup never got a stored embedding: cluster is only the first two.
    kept = [
        _seed_memory(store, mapping, vector=NEAR_DUP_VECTORS[0], title="A", canonical_text="Sam prefers oat milk lattes"),
        _seed_memory(
            store, mapping, vector=NEAR_DUP_VECTORS[1], title="B", canonical_text="Sam prefers oat milk lattes daily"
        ),
    ]
    _seed_memory(
        store,
        mapping,
        vector=NEAR_DUP_VECTORS[2],
        title="C",
        canonical_text="Sam usually orders oat milk lattes",
        with_embedding=False,
    )
    provider = MappedEmbeddingProvider(mapping)
    artifact = VNextConsolidationService(store, embedding_provider=provider).generate_memory_consolidation(
        MemoryConsolidationRequest()
    )
    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    assert candidates[0]["metadata_json"]["consolidation"]["cluster_member_ids"] == sorted(
        str(row["id"]) for row in kept
    )
    assert artifact["metadata_json"]["input_counts"]["embedded_memories"] == 2
    assert len(provider.batch_texts) == 2
    assert all("usually orders" not in text for text in provider.batch_texts)


def test_cap_bound_is_applied_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    with caplog.at_level(logging.INFO, logger="alicebot_api.vnext_consolidation"):
        artifact = _service(store, mapping).generate_memory_consolidation(
            MemoryConsolidationRequest(max_embedded_memories=2)
        )
    assert artifact["metadata_json"]["consolidation"]["bounded"] is True
    assert artifact["metadata_json"]["input_counts"]["active_memories"] == 6
    assert artifact["metadata_json"]["input_counts"]["active_memories_exact"] is True
    assert any("bounded" in record.message for record in caplog.records)
    assert "bounded" in artifact["content_markdown"]
    clustering_calls = [
        call
        for call in store.list_memory_calls
        if call["status"] in {"active", "accepted"} and call["limit"] is not None
    ]
    assert [call["limit"] for call in clustering_calls[:2]] == [3, 3]


def test_clustering_does_not_materialize_triangle_or_pair_index_lists(monkeypatch) -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)

    def _forbidden(*args, **kwargs):
        raise AssertionError("dense triangle and global pair indexes must not be materialized")

    monkeypatch.setattr(np, "triu", _forbidden)
    monkeypatch.setattr(np, "where", _forbidden)
    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(propose_rollups=False)
    )

    assert artifact["metadata_json"]["input_counts"]["clusters"] == 1
    assert artifact["metadata_json"]["consolidation"]["resource_guard"][
        "pair_index_materialization"
    ] is False


def test_threshold_override_via_metadata_json_options() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(metadata_json={"consolidation_options": {"similarity_threshold": 0.9999}})
    )
    assert _consolidation_candidates(store) == []
    assert artifact["metadata_json"]["consolidation"]["similarity_threshold"] == 0.9999


def test_near_duplicate_clusters_are_partitioned_by_exact_project_scope() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    for project_scope in (("project-a",), ("project-b",)):
        for index in range(2):
            _seed_memory(
                store,
                mapping,
                vector=NEAR_DUP_VECTORS[index],
                title=f"{project_scope[0]} note {index}",
                canonical_text=f"Shared release wording variant {index}",
                project_scope=project_scope,
            )

    VNextConsolidationService(
        store,
        embedding_provider=MappedEmbeddingProvider(mapping),
    ).generate_memory_consolidation(
        MemoryConsolidationRequest(min_cluster_size=2, propose_rollups=False)
    )

    candidates = _consolidation_candidates(store)
    assert len(candidates) == 2
    assert {tuple(row["metadata_json"]["project_scope"]) for row in candidates} == {
        ("project-a",),
        ("project-b",),
    }
    for candidate in candidates:
        member_ids = candidate["metadata_json"]["consolidation"]["cluster_member_ids"]
        member_scopes = {
            tuple(row["metadata_json"]["project_scope"])
            for row in store.memories
            if str(row["id"]) in member_ids
        }
        assert member_scopes == {tuple(candidate["metadata_json"]["project_scope"])}


def test_project_scoped_consolidation_filters_decoys_before_corpus_limit() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    for index in range(25):
        _seed_memory(
            store,
            mapping,
            vector=DISTINCT_VECTORS[index % len(DISTINCT_VECTORS)],
            title=f"Project B decoy {index}",
            canonical_text=f"Unrelated project B state {index}",
            project_scope=("project-b",),
        )
    target_ids = {
        str(
            _seed_memory(
                store,
                mapping,
                vector=NEAR_DUP_VECTORS[index],
                title=f"Project A target {index}",
                canonical_text=f"Project A release readiness wording {index}",
                project_scope=("project-a",),
            )["id"]
        )
        for index in range(2)
    }

    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(
            projects=("project-a",),
            max_embedded_memories=2,
            min_cluster_size=2,
            propose_rollups=False,
        )
    )

    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    assert set(candidates[0]["metadata_json"]["consolidation"]["cluster_member_ids"]) == target_ids
    assert candidates[0]["metadata_json"]["project_scope"] == ["project-a"]
    assert artifact["metadata_json"]["project_scope"] == ["project-a"]


# -- reinforced preferences --------------------------------------------------------


def test_preference_cluster_spanning_three_sources_is_reported_review_only() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    for index, text in enumerate(
        (
            "Sam prefers oat milk lattes in the morning",
            "Sam prefers oat milk lattes every morning before standup",
            "Sam usually orders an oat milk latte in the mornings",
        )
    ):
        _seed_memory(
            store,
            mapping,
            vector=NEAR_DUP_VECTORS[index],
            title=f"Coffee preference v{index}",
            canonical_text=text,
            memory_type="preference",
            source_event_ids=[f"event-{index}"],
            source_id=f"source-{index}",
        )
    artifact = _service(store, mapping).generate_memory_consolidation(MemoryConsolidationRequest())
    reinforced = artifact["metadata_json"]["consolidation"]["reinforced_preferences"]
    assert len(reinforced) == 1
    assert reinforced[0]["distinct_source_count"] >= 3
    assert "Reinforced Preferences" in artifact["content_markdown"]
    assert "reinforced preference" in artifact["content_markdown"]
    # Review-only: the note changed no memory rows beyond the one candidate.
    assert len(_consolidation_candidates(store)) == 1
    for row in store.list_memories(status="active"):
        assert row.get("confidence") is None or row["memory_type"] == "preference"


def test_distinct_event_ids_do_not_count_as_independent_sources() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    for index in range(3):
        _seed_memory(
            store,
            mapping,
            vector=NEAR_DUP_VECTORS[index],
            title=f"Preference {index}",
            canonical_text="Sam prefers oat milk lattes every morning",
            memory_type="preference",
            source_event_ids=[f"event-{index}"],
        )

    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(propose_rollups=False)
    )

    assert artifact["metadata_json"]["consolidation"]["reinforced_preferences"] == []


def test_non_preference_cluster_is_not_reported_as_reinforced() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)  # memory_type semantic
    artifact = _service(store, mapping).generate_memory_consolidation(MemoryConsolidationRequest())
    assert artifact["metadata_json"]["consolidation"]["reinforced_preferences"] == []


# -- model-backed proposals ---------------------------------------------------------


def test_model_backed_merge_uses_stub_provider_and_records_provenance() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    near_dups, _ = _seed_six_memories(store, mapping)
    stub = StubMergeModelProvider(
        '{"title": "Morning oat milk latte preference", '
        '"canonical_text": "Sam prefers oat milk lattes every morning, usually before standup."}'
    )
    service = VNextConsolidationService(
        store,
        embedding_provider=MappedEmbeddingProvider(mapping),
        merge_provider=stub,
    )
    artifact = service.generate_memory_consolidation(
        MemoryConsolidationRequest(
            generation_mode="model_backed",
            model_route_mode="cloud_allowed",
            model_provider="mock",
            sensitivity_allowed=("public", "internal"),
        )
    )
    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    candidate = candidates[0]
    consolidation = candidate["metadata_json"]["consolidation"]
    assert consolidation["proposal_kind"] == "merge"
    assert candidate["canonical_text"] == "Sam prefers oat milk lattes every morning, usually before standup."
    assert candidate["trust_class"] == "llm_single_source"
    assert consolidation["model_provenance"]["provider"] == "stub_model"
    assert consolidation["model_provenance"]["prompt_hash"].startswith("sha256:")
    # merge proposes superseding every member (after human acceptance only)
    assert sorted(consolidation["proposed_supersede"]) == sorted(str(row["id"]) for row in near_dups)
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert stub.prompts and "cluster_members" in stub.prompts[0]


def test_model_backed_with_deterministic_route_falls_back_to_dedup() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    near_dups, _ = _seed_six_memories(store, mapping)
    service = _service(store, mapping)
    service.generate_memory_consolidation(
        MemoryConsolidationRequest(generation_mode="model_backed", sensitivity_allowed=("public", "internal"))
    )
    candidates = _consolidation_candidates(store)
    assert len(candidates) == 1
    consolidation = candidates[0]["metadata_json"]["consolidation"]
    assert consolidation["proposal_kind"] == "dedup"
    assert consolidation["merge_refusal"] == "deterministic_provider_refuses_merge_synthesis"
    survivor = max(near_dups, key=lambda row: (len(row["canonical_text"]), str(row["id"])))
    assert candidates[0]["canonical_text"] == survivor["canonical_text"]


def test_model_backed_approval_required_route_fails_before_any_writes() -> None:
    from alicebot_api.vnext_model_intelligence import VNextModelIntelligenceError

    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    memory_count = len(store.memories)
    with pytest.raises(VNextModelIntelligenceError):
        _service(store, mapping).generate_memory_consolidation(
            MemoryConsolidationRequest(
                generation_mode="model_backed",
                model_route_mode="cloud_requires_approval",
                sensitivity_allowed=("public", "internal"),
            )
        )
    assert len(store.memories) == memory_count
    assert store.artifacts == []


def test_create_candidate_memories_false_lists_proposals_without_writes() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(create_candidate_memories=False)
    )
    assert _consolidation_candidates(store) == []
    proposals = artifact["metadata_json"]["consolidation"]["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["candidate_memory_id"] is None


def test_rollup_pass_skips_groups_covered_by_near_duplicate_clusters() -> None:
    """The latte trio clusters as near-duplicates, so the roll-up pass must
    leave it to the dedup proposal instead of double-proposing a card."""
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    artifact = _service(store, mapping).generate_memory_consolidation(MemoryConsolidationRequest())

    rollups = artifact["metadata_json"]["rollups"]
    assert rollups["enabled"] is True
    assert rollups["proposals"] == []
    assert any("covered_by_near_duplicate_cluster" in reason for reason in rollups["skipped"])
    rollup_candidates = [
        row
        for row in store.list_memories(status="candidate")
        if isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == "memory_rollup"
    ]
    assert rollup_candidates == []
    assert "## Roll-up Proposals" in artifact["content_markdown"]


def test_rollup_quality_gate_drops_junk_groups_and_is_disclosed() -> None:
    """Pronoun-contraction groups never reach the review console through the
    scheduled consolidation workflow; the artifact discloses the gate."""
    store = FakeConsolidationStore()
    specs = (
        ("I played The Last of Us Part II for 30 hours", "2023-05-10"),
        ("I played Assassin's Creed Odyssey for 70 hours", "2023-05-18"),
        ("I played Hollow Knight for 25 hours", "2023-06-02"),
        ("I played Stardew Valley for 85 hours", "2023-06-20"),
        ("I'm excited about the violet harbor", None),
        ("I'm tired after the granite summit", None),
        ("I'm curious about the copper lantern", None),
    )
    for index, (text, session_date) in enumerate(specs):
        metadata: JsonObject = {"session_date": session_date} if session_date else {}
        store.create_memory(
            {
                "memory_key": f"memory.gate-{index}",
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
        )
    artifact = VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(
        MemoryConsolidationRequest()
    )

    rollups = artifact["metadata_json"]["rollups"]
    assert rollups["enabled"] is True
    labels = [proposal["label"].casefold() for proposal in rollups["proposals"]]
    assert labels and all(not label.startswith("i'") for label in labels)
    assert rollups["quality_gate"]["dropped_group_count"] >= 1
    assert "label_without_content_words" in rollups["quality_gate"]["dropped_by_reason"]
    assert any(reason.startswith("quality_gate_dropped") for reason in rollups["skipped"])


def test_propose_rollups_false_discloses_disabled_state() -> None:
    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    artifact = _service(store, mapping).generate_memory_consolidation(
        MemoryConsolidationRequest(propose_rollups=False)
    )
    assert artifact["metadata_json"]["rollups"] == {"enabled": False}
    assert artifact["metadata_json"]["input_counts"]["rollup_proposals"] == 0
    assert "propose_rollups=false" in artifact["content_markdown"]


def test_invalid_rollup_options_fail_before_any_writes() -> None:
    from alicebot_api.vnext_rollups import VNextRollupValidationError

    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    _seed_six_memories(store, mapping)
    memory_count = len(store.memories)
    with pytest.raises(VNextRollupValidationError):
        _service(store, mapping).generate_memory_consolidation(
            MemoryConsolidationRequest(metadata_json={"rollup_options": {"min_members": 1}})
        )
    assert len(store.memories) == memory_count
    assert store.artifacts == []


def test_scheduler_call_shape_still_constructs() -> None:
    # Mirrors the kwargs vnext_scheduler passes; must keep constructing unchanged.
    request = MemoryConsolidationRequest(
        domains=("project",),
        sensitivity_allowed=("public", "internal"),
        generated_for="2026-07-04",
        source_limit=12,
        memory_limit=12,
        artifact_limit=8,
        event_limit=30,
        rating_limit=20,
        create_candidate_memories=True,
        generated_by="scheduler",
        trace_id="trace-1",
        run_id="run-1",
        agent_identity=None,
        policy_decision=None,
        metadata_json={"workflow": "memory_consolidation"},
        generation_mode="deterministic",
        model_route_mode=None,
        model_provider=None,
        model=None,
        model_temperature=0.2,
        allow_cloud_private=False,
    )
    store = FakeConsolidationStore()
    artifact = VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(request)
    assert artifact["artifact_type"] == "memory_consolidation"
    assert artifact["status"] == "needs_review"
    assert artifact["generated_by"] == "scheduler"


# -- live sqlite smoke ----------------------------------------------------------------


class SQLiteArtifactShim:
    """SQLiteVNextStore has no artifact surface yet; delegate everything else."""

    def __init__(self, store: SQLiteVNextStore) -> None:
        self._store = store
        self.artifacts: list[JsonObject] = []

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
        row = {"id": str(uuid4()), **artifact}
        self.artifacts.append(row)
        return dict(row)


def test_live_sqlite_smoke_clusters_near_duplicates_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "smoke@example.com", "Smoke User")
    sqlite_store = SQLiteVNextStore(conn, user_id)
    store = SQLiteArtifactShim(sqlite_store)

    mapping: dict[str, list[float]] = {}
    specs = [
        ("Oat milk latte", "Sam prefers oat milk lattes in the morning", NEAR_DUP_VECTORS[0]),
        ("Oat milk latte routine", "Sam prefers oat milk lattes every morning before standup", NEAR_DUP_VECTORS[1]),
        ("Morning latte", "Sam usually orders an oat milk latte in the mornings", NEAR_DUP_VECTORS[2]),
        ("Deploy day", "The API deploys from the main branch on Fridays", DISTINCT_VECTORS[0]),
        ("Quarterly review", "Northwind Capital quarterly review happens in October", DISTINCT_VECTORS[1]),
        ("Staging reset", "The staging database is reset every Sunday night", DISTINCT_VECTORS[2]),
    ]
    rows: list[JsonObject] = []
    for title, canonical_text, vector in specs:
        row = sqlite_store.create_memory(
            {
                "memory_key": f"memory.{uuid4()}",
                "value": {"text": canonical_text},
                "status": "active",
                "memory_type": "semantic",
                "title": title,
                "canonical_text": canonical_text,
                "summary": canonical_text[:80],
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        # Deterministic fake vectors through the real embed-on-write surface.
        assert sqlite_store.update_memory_embedding(memory_id=str(row["id"]), vector=vector) is not None
        mapping[memory_embedding_text(row)] = list(vector)
        rows.append(row)
    near_dup_ids = sorted(str(row["id"]) for row in rows[:3])

    service = VNextConsolidationService(store, embedding_provider=MappedEmbeddingProvider(mapping))
    artifact = service.generate_memory_consolidation(MemoryConsolidationRequest())

    candidates = _consolidation_candidates(sqlite_store)
    assert len(candidates) == 1
    consolidation = candidates[0]["metadata_json"]["consolidation"]
    assert consolidation["cluster_member_ids"] == near_dup_ids
    assert consolidation["proposal_kind"] in {"dedup", "merge"}
    assert consolidation["proposal_kind"] == "dedup"  # deterministic run: no model
    assert candidates[0]["status"] == "candidate"
    assert artifact["metadata_json"]["input_counts"]["embedded_memories"] == 6
    assert artifact["metadata_json"]["input_counts"]["clusters"] == 1
    # Stored and re-derived vectors agree: probe self-distance is ~0.
    self_distance = artifact["metadata_json"]["consolidation"]["probe_self_distance"]
    assert self_distance is not None and self_distance < 1e-6

    # Idempotent rerun: same input set, no duplicate candidate.
    second = service.generate_memory_consolidation(MemoryConsolidationRequest())
    assert len(_consolidation_candidates(sqlite_store)) == 1
    assert second["metadata_json"]["candidate_memory_ids"] == artifact["metadata_json"]["candidate_memory_ids"]
    assert second["metadata_json"]["consolidation"]["proposals"][0]["candidate_state"] == "existing"

    # Review-first: the three members are still active in the real store.
    active_ids = {str(row["id"]) for row in sqlite_store.list_memories(status="active")}
    for member_id in near_dup_ids:
        assert member_id in active_ids
    conn.close()


def test_accepting_the_dedup_candidate_executes_supersessions_on_live_sqlite(monkeypatch) -> None:
    """Full merge loop: consolidation proposes, acceptance executes.

    The consolidation run stays review-only; accepting the candidate through
    the memory commit service promotes it and supersedes exactly the
    proposed members, and the survivor pointer follows the dedup rule.
    """
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "accept@example.com", "Accept User")
    sqlite_store = SQLiteVNextStore(conn, user_id)
    store = SQLiteArtifactShim(sqlite_store)

    mapping: dict[str, list[float]] = {}
    rows: list[JsonObject] = []
    for index, text in enumerate(
        (
            "Sam prefers oat milk lattes in the morning",
            "Sam prefers oat milk lattes every morning before standup",
            "Sam usually orders an oat milk latte in the mornings",
        )
    ):
        row = sqlite_store.create_memory(
            {
                "memory_key": f"memory.{uuid4()}",
                "value": {"text": text},
                "status": "active",
                "memory_type": "semantic",
                "title": f"Latte {index}",
                "canonical_text": text,
                "summary": text[:80],
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        assert sqlite_store.update_memory_embedding(memory_id=str(row["id"]), vector=NEAR_DUP_VECTORS[index]) is not None
        mapping[memory_embedding_text(row)] = list(NEAR_DUP_VECTORS[index])
        rows.append(row)

    service = VNextConsolidationService(store, embedding_provider=MappedEmbeddingProvider(mapping))
    service.generate_memory_consolidation(MemoryConsolidationRequest())
    candidates = _consolidation_candidates(sqlite_store)
    assert len(candidates) == 1
    candidate_id = str(candidates[0]["id"])
    consolidation = candidates[0]["metadata_json"]["consolidation"]
    assert consolidation["proposal_kind"] == "dedup"
    assert any("accept_consolidation_candidate" in line for line in consolidation["reviewer_instructions"])
    survivor_id = str(consolidation["survivor_memory_id"])
    proposed = sorted(str(member) for member in consolidation["proposed_supersede"])

    commit_service = VNextMemoryCommitService(sqlite_store)
    result = commit_service.accept_consolidation_candidate(candidate_id, reason="Reviewed the dedup cluster.")

    assert result["status"] == "accepted"
    assert sorted(result["superseded_member_ids"]) == proposed
    accepted = sqlite_store.get_memory(candidate_id)
    assert accepted["status"] == "active"
    assert str(accepted["supersedes"]) == survivor_id
    for member_id in proposed:
        member = sqlite_store.get_memory(member_id)
        assert member["status"] == "superseded"
        assert str(member["superseded_by"]) == candidate_id
    # The accepted candidate is now the single active representative,
    # including for the member whose text it canonically descends from.
    assert sqlite_store.get_memory(survivor_id)["status"] == "superseded"
    # Replay is a no-op.
    replay = commit_service.accept_consolidation_candidate(candidate_id, reason="Accept again.")
    assert replay["idempotent_replay"] is True
    conn.close()
