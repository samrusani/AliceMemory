"""Seeding and measurement core of the scale benchmark.

Corpus seeding writes through the store's public surface (``create_source``
/ ``create_memory`` / ``create_provenance_link`` / ``create_entity`` +
``mentions`` edges), i.e. the same rows the capture pipeline would produce,
but WITHOUT re-running per-memory candidate extraction and entity-linking
lookups -- direct entity/edge seeding is orders of magnitude cheaper at 100k
and produces the same substrate shape. Embeddings are attached on write for
every memory via ``update_memory_embedding`` using the deterministic local
embedding (see ``eval/scale/vectors.py``), so the vector stage is measured
at every scale.

Measured operations run the product services (retrieval, capture, commit,
scheduler sweep, consolidation) against the seeded store. Write operations
COMMIT inside the timed region, mirroring one-transaction-per-command CLI /
MCP usage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import itertools
import statistics
import time
from typing import Callable
from uuid import uuid4

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_consolidation import (
    MAX_EMBEDDED_MEMORIES_HARD_CAP,
    MemoryConsolidationRequest,
    VNextConsolidationService,
)
from alicebot_api.vnext_embeddings import (
    memory_embedding_text,
    signed_memory_embedding_update,
)
from alicebot_api.vnext_entity_names import normalize_entity_name
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService

from scale import corpus
from scale.backends import BENCH_USER_ID, BackendSession
from scale.vectors import DeterministicEmbeddingProvider
from alicebot_api.vnext_source_fence import SourceReadFence

SEED_COMMIT_EVERY = 1000


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ArtifactSinkStore:
    """Store proxy that absorbs ``create_artifact`` for the SQLite on-ramp.

    ``SQLiteVNextStore`` has no artifact surface, so the staleness sweep and
    the consolidation pass (which both end by persisting a report artifact)
    cannot complete on SQLite as-is. The proxy forwards every store call and
    keeps artifacts in memory, letting the benchmark time the sweep/cluster
    work itself. Documented caveat: on SQLite the measured figure excludes
    artifact persistence (one small INSERT on Postgres).
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.artifacts: list[dict[str, object]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def create_artifact(self, artifact: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        record = {
            "status": "needs_review",
            **artifact,
            "id": str(uuid4()),
            "created_at": _utc_now_iso(),
        }
        self.artifacts.append(record)
        return record


def _create_edge(store: object, edge: dict[str, object]) -> None:
    """Duck-typed edge write, mirroring EntityLinkingService._create_edge."""
    create = getattr(store, "create_edge", None)
    if not callable(create):
        create = getattr(store, "create_graph_edge")
    create(edge, actor_type="system")


@dataclass(slots=True)
class IngestResult:
    scale: int
    entity_count: int
    source_count: int
    memory_count: int
    edge_count: int
    provenance_count: int
    entities_seconds: float
    sources_seconds: float
    memories_seconds: float
    total_seconds: float
    hot_entity_id: str = ""
    mid_entity_id: str = ""

    def to_record(self) -> dict[str, object]:
        return {
            "memories": self.memory_count,
            "sources": self.source_count,
            "entities": self.entity_count,
            "edges": self.edge_count,
            "provenance_links": self.provenance_count,
            "entities_seconds": round(self.entities_seconds, 2),
            "sources_seconds": round(self.sources_seconds, 2),
            "memories_seconds": round(self.memories_seconds, 2),
            "total_seconds": round(self.total_seconds, 2),
            "memories_per_second": round(self.memory_count / self.memories_seconds, 1)
            if self.memories_seconds > 0
            else None,
            "embed_on_write": True,
        }


def seed_corpus(session: BackendSession, scale: int, *, seed: int = corpus.SEED_DEFAULT) -> IngestResult:
    store = session.store
    started = time.monotonic()
    now_iso = _utc_now_iso()

    entities = corpus.build_entities(scale, seed=seed)
    entity_ids: list[str] = []
    for spec in entities:
        row = store.create_entity(
            {
                "entity_type": spec.entity_type,
                "name": spec.name,
                "normalized_name": normalize_entity_name(spec.name),
                "first_observed_at": now_iso,
                "last_observed_at": now_iso,
                "mention_count": 1,
                "metadata_json": {"created_by": "scale_benchmark"},
            },
            actor_type="system",
        )
        entity_ids.append(str(row["id"]))
    session.commit()
    entities_done = time.monotonic()

    sources = corpus.build_sources(scale, seed=seed)
    source_ids: list[str] = []
    for source_spec in sources:
        row = store.create_source(
            {
                "source_type": "manual_text",
                "title": source_spec.title,
                "content_hash": source_spec.content_hash,
                "domain": source_spec.domain,
                "sensitivity": source_spec.sensitivity,
                "connector_name": "scale_benchmark",
                "external_id": f"scale-{seed}-{scale}-{source_spec.index}",
                "metadata_json": {"benchmark": "scale", "index": source_spec.index},
            },
            actor_type="system",
        )
        source_ids.append(str(row["id"]))
        if (source_spec.index + 1) % SEED_COMMIT_EVERY == 0:
            session.commit()
    session.commit()
    sources_done = time.monotonic()

    edge_count = 0
    provenance_count = 0
    memory_count = 0
    embedding_provider = DeterministicEmbeddingProvider()
    for spec in corpus.iter_memories(scale, seed=seed, entities=entities):
        memory = store.create_memory(
            {
                "memory_key": spec.memory_key,
                "value": {"benchmark": "scale", "index": spec.index},
                "status": spec.status,
                "memory_type": spec.memory_type,
                "confidence": spec.confidence,
                "salience": spec.salience,
                "valid_to": spec.valid_to,
                "last_confirmed_at": spec.last_confirmed_at,
                "title": spec.title,
                "canonical_text": spec.canonical_text,
                "domain": spec.domain,
                "sensitivity": spec.sensitivity,
                "metadata_json": {
                    "benchmark": "scale",
                    "source_id": source_ids[spec.source_index],
                },
            },
            actor_type="system",
        )
        memory_id = str(memory["id"])
        memory_count += 1
        store.update_memory_embedding(
            **signed_memory_embedding_update(
                memory,
                embedding_provider.embed_text(memory_embedding_text(memory)),
                provider=embedding_provider,
            )
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory_id,
                "source_id": source_ids[spec.source_index],
                "evidence_role": "supports",
                "confidence": spec.confidence,
            },
            actor_type="system",
        )
        provenance_count += 1
        for entity_index in spec.entity_indices:
            _create_edge(
                store,
                {
                    "from_type": "memory",
                    "from_id": memory_id,
                    "to_type": "entity",
                    "to_id": entity_ids[entity_index],
                    "edge_type": "mentions",
                    "confidence": 0.8,
                    "observed_at": now_iso,
                    "explanation": "scale benchmark seeded mention",
                    "metadata_json": {"benchmark": "scale"},
                },
            )
            edge_count += 1
        if memory_count % SEED_COMMIT_EVERY == 0:
            session.commit()
    session.commit()
    # Refresh planner statistics after bulk ingest: a fresh container's
    # autovacuum has not caught up, so without ANALYZE the benchmark measures
    # pathological plans no steady-state deployment would see. SQLite's
    # ANALYZE is cheap and applied for symmetry.
    try:
        session.raw_execute("ANALYZE")
        session.commit()
    except Exception:
        pass
    finished = time.monotonic()

    return IngestResult(
        scale=scale,
        entity_count=len(entity_ids),
        source_count=len(source_ids),
        memory_count=memory_count,
        edge_count=edge_count,
        provenance_count=provenance_count,
        entities_seconds=entities_done - started,
        sources_seconds=sources_done - entities_done,
        memories_seconds=finished - sources_done,
        total_seconds=finished - started,
        hot_entity_id=entity_ids[0],
        mid_entity_id=entity_ids[1],
    )


@dataclass(slots=True)
class OpResult:
    name: str
    samples_ms: list[float]
    warmup: int
    notes: dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        ordered = sorted(self.samples_ms)
        n = len(ordered)

        def rank(q: float) -> float:
            return ordered[min(n - 1, max(0, round(q * (n - 1))))]

        return {
            "iterations": n,
            "warmup_iterations": self.warmup,
            "p50_ms": round(rank(0.50), 3),
            "p95_ms": round(rank(0.95), 3),
            "mean_ms": round(statistics.fmean(ordered), 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
            "total_seconds": round(sum(ordered) / 1000.0, 2),
            "notes": self.notes,
        }


def _measure(
    name: str,
    fn: Callable[[], object],
    *,
    target_iterations: int,
    min_iterations: int,
    time_budget_seconds: float,
    warmup: int,
    notes: dict[str, object] | None = None,
) -> OpResult:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    window_started = time.monotonic()
    while len(samples) < target_iterations:
        began = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - began) * 1000.0)
        if len(samples) >= min_iterations and (time.monotonic() - window_started) > time_budget_seconds:
            break
    result = OpResult(name=name, samples_ms=samples, warmup=warmup, notes=dict(notes or {}))
    if len(samples) < target_iterations:
        result.notes["iterations_capped_by_time_budget_seconds"] = time_budget_seconds
    return result


def run_operations(
    session: BackendSession,
    ingest: IngestResult,
    *,
    seed: int = corpus.SEED_DEFAULT,
    iterations: int = 50,
    time_budget_seconds: float = 45.0,
    heavy_time_budget_seconds: float = 90.0,
) -> dict[str, dict[str, object]]:
    store = session.store
    provider = DeterministicEmbeddingProvider()
    results: dict[str, dict[str, object]] = {}
    counter = itertools.count()

    # 1. alice-recall-shaped compile_context_pack over a rotating query mix
    #    (lexical, domain-inflected, and entity-resolving queries).
    retrieval = VNextRetrievalService(store, embedding_provider=provider)
    last_pack: dict[str, object] = {}

    def run_recall() -> None:
        nonlocal last_pack
        index = next(counter)
        request = VNextRetrievalRequest(
            query=corpus.RECALL_QUERIES[index % len(corpus.RECALL_QUERIES)],
            max_items=8,
            include_sources=True,
            actor_type="system",
        )
        last_pack = retrieval.compile_context_pack(request, source_fence=SourceReadFence.unfenced())

    recall = _measure(
        "recall_context_pack", run_recall,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
        notes={"queries": len(corpus.RECALL_QUERIES)},
    )
    # Stage status snapshot from a known entity-resolving query.
    entity_pack = retrieval.compile_context_pack(
        VNextRetrievalRequest(query=corpus.RECALL_QUERIES[2], max_items=8, include_sources=True, actor_type="system"),
        source_fence=SourceReadFence.unfenced()
    )
    entity_trace = entity_pack.get("trace") if isinstance(entity_pack.get("trace"), dict) else {}
    stages = entity_trace.get("stages") if isinstance(entity_trace.get("stages"), dict) else {}
    recall.notes["stages_entity_query"] = {
        name: (stage.get("status") or stage.get("source"), stage.get("candidate_count"))
        for name, stage in stages.items()
        if isinstance(stage, dict)
    }
    session.commit()
    results["recall_context_pack"] = recall.to_record()

    # 2. capture_text single (chunking, candidate extraction, entity linking,
    #    provenance, commit). Capture embeds nothing: it writes candidates, and
    #    a candidate is not embedded until it is accepted.
    capture_service = VNextCaptureService(store, actor_type="user")
    capture_counter = itertools.count()

    def run_capture() -> None:
        title, text = corpus.capture_text_for_iteration(next(capture_counter), seed=seed)
        capture_service.capture_text(text, title=title, domain="professional", sensitivity="internal")
        session.commit()

    results["capture_text_single"] = _measure(
        "capture_text_single", run_capture,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
    ).to_record()

    # 3. governed agent memory commit (trusted local agent profile takes the
    #    direct-commit branch of the write policy; unique idempotency keys).
    commit_service = VNextMemoryCommitService(store)
    commit_identity = AgentIdentity(
        agent_id="scale-benchmark-agent",
        agent_type="workflow_agent",
        permission_profile="trusted_local_agent",
    )
    commit_counter = itertools.count()
    last_commit: dict[str, object] = {}

    def run_commit() -> None:
        nonlocal last_commit
        payload = corpus.commit_payload_for_iteration(next(commit_counter), seed=seed)
        request = MemoryCommitRequest(user_id=str(BENCH_USER_ID), **payload)
        last_commit = commit_service.commit(identity=commit_identity, request=request)
        session.commit()

    commit_result = _measure(
        "memory_commit", run_commit,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
    )
    commit_result.notes["write_mode_last"] = last_commit.get("write_mode")
    commit_result.notes["status_last"] = last_commit.get("status")
    results["memory_commit"] = commit_result.to_record()

    # 4. review-queue list, the MCP alice_memories list shape:
    #    fetch-all-then-slice (mcp_tools._handle: list_memories()[:limit]).
    queue_len = 0

    def run_review_queue() -> None:
        nonlocal queue_len
        queue_len = len(store.list_memories(status="candidate")[:20])

    review = _measure(
        "review_queue_list", run_review_queue,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
    )
    review.notes["returned_rows_last"] = queue_len
    results["review_queue_list"] = review.to_record()

    # 5. entity resolution lookup (the graph stage's one round-trip).
    lookup_names = tuple(
        normalize_entity_name(name)
        for name in (
            corpus.HOT_ENTITY_NAME,
            corpus.MID_ENTITY_NAME,
            "Sara Lindqvist",
            "Quantia Analytics",
            "completely unknown entity",
        )
    )
    found = 0

    def run_entity_lookup() -> None:
        nonlocal found
        found = len(store.find_entities_by_names(lookup_names))

    entity_result = _measure(
        "entity_find_by_names", run_entity_lookup,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
        notes={"lookup_names": len(lookup_names)},
    )
    entity_result.notes["entities_matched_last"] = found
    results["entity_find_by_names"] = entity_result.to_record()

    # 6. graph one-hop for the hot entity, exactly as the retrieval graph
    #    stage walks it (both edge directions, no limit).
    hot_id = ingest.hot_entity_id
    hot_edges = 0

    def run_one_hop() -> None:
        nonlocal hot_edges
        hot_edges = len(store.list_edges(to_id=hot_id)) + len(store.list_edges(from_id=hot_id))

    hop = _measure(
        "graph_one_hop_hot_entity", run_one_hop,
        target_iterations=iterations, min_iterations=5,
        time_budget_seconds=time_budget_seconds, warmup=3,
    )
    hop.notes["hot_entity_edges"] = hot_edges
    results["graph_one_hop_hot_entity"] = hop.to_record()

    # 7. staleness sweep single pass. SQLite gets the artifact sink proxy
    #    (no artifact surface on the on-ramp store); the measured pass calls
    #    the sweep directly (scheduler-run bookkeeping rows excluded on both
    #    backends so the code path is identical). One unmeasured first pass
    #    clears the seeded expired backlog; measured passes are steady-state.
    sweep_store = store if session.backend == "postgres" else ArtifactSinkStore(store)
    scheduler = VNextSchedulerService(sweep_store)
    sweep_request = SchedulerRunRequest(workflow_type="staleness_sweep")
    sweep_metadata = {"generated_by": "scale_benchmark"}

    first_pass_began = time.perf_counter()
    scheduler._run_staleness_sweep(sweep_request, metadata=sweep_metadata)  # noqa: SLF001
    session.commit()
    first_pass_ms = (time.perf_counter() - first_pass_began) * 1000.0

    def run_sweep() -> None:
        scheduler._run_staleness_sweep(sweep_request, metadata=sweep_metadata)  # noqa: SLF001
        session.commit()

    sweep = _measure(
        "staleness_sweep_pass", run_sweep,
        target_iterations=iterations, min_iterations=3,
        time_budget_seconds=heavy_time_budget_seconds, warmup=1,
        notes={
            "first_pass_ms": round(first_pass_ms, 1),
            "artifact_persisted": session.backend == "postgres",
        },
    )
    results["staleness_sweep_pass"] = sweep.to_record()

    # 8. consolidation clustering pass (embedding clustering -> merge
    #    proposals). Hard-capped at the most recent bounded active corpus
    #    (MAX_EMBEDDED_MEMORIES_HARD_CAP); the store applies the cap before
    #    materialization and clustering uses bounded float32 row blocks.
    cons_store = store if session.backend == "postgres" else ArtifactSinkStore(store)
    consolidation = VNextConsolidationService(cons_store, embedding_provider=provider)
    cons_request = MemoryConsolidationRequest()
    cons_notes: dict[str, object] = {
        "embedded_memory_hard_cap": MAX_EMBEDDED_MEMORIES_HARD_CAP,
        "artifact_persisted": session.backend == "postgres",
    }
    last_payload: dict[str, object] = {}

    def run_consolidation() -> None:
        nonlocal last_payload
        last_payload = consolidation.generate_memory_consolidation(cons_request)
        session.commit()

    try:
        run_consolidation()  # unmeasured warm pass creates candidates; replays after
    except Exception as exc:  # noqa: BLE001 - known product bug on Postgres
        # At alembic head, generated_artifacts_type_check (migration
        # 20260510_0067) does not allow artifact_type='memory_consolidation'
        # (written by vnext_consolidation.generate_memory_consolidation), so
        # the pass crashes on any live Postgres store. Fall back to the
        # artifact sink so the clustering work is still measured, and record
        # the bug in the results.
        session.rollback()
        cons_store = ArtifactSinkStore(store)
        consolidation = VNextConsolidationService(cons_store, embedding_provider=provider)
        cons_notes["artifact_persisted"] = False
        cons_notes["artifact_write_bug"] = (
            f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]} -- "
            "generated_artifacts_type_check lacks 'memory_consolidation'"
        )
        run_consolidation()
    cons = _measure(
        "consolidation_pass", run_consolidation,
        target_iterations=iterations, min_iterations=3,
        time_budget_seconds=heavy_time_budget_seconds, warmup=0,
        notes=cons_notes,
    )
    for key in ("cluster_count", "embedded_count", "active_count", "skipped"):
        if key in last_payload:
            cons.notes[f"{key}_last"] = last_payload[key]
    results["consolidation_pass"] = cons.to_record()

    return results


__all__ = [
    "ArtifactSinkStore",
    "IngestResult",
    "OpResult",
    "run_operations",
    "seed_corpus",
]
