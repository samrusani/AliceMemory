"""Memory consolidation: near-duplicate clustering behind the review gate.

Pipeline (all review-only, nothing is promoted or superseded automatically):

1. Count the user's active/accepted memories exactly, fetch a disclosed
   bounded subset, and cluster near-duplicates by cohesive all-pairs cosine
   admission over their embeddings.
2. Per cluster, build a merge (model-backed) or dedup (deterministic)
   proposal as a *candidate* memory carrying cluster membership, similarity
   stats, provenance, and per-member ``proposed_supersede`` markers.
3. Detect reinforced preferences: preference/routine clusters whose members
   span >= 3 distinct sources or days.
4. Propose roll-up cards (``vnext_rollups``): deterministic same-entity /
   lexical-topic groups become one aggregation card per topic, created as
   review-gated candidates exactly like the merge/dedup proposals. Accepting
   a roll-up supersedes NOTHING (members stay individually recallable);
   accepting a roll-up *revision* retires only the previous card.
5. Emit a ``memory_consolidation`` report artifact with real sections.

Embedding access path
---------------------
Store rows never expose the raw embedding column (``MEMORY_COLUMNS`` excludes
it in both stores). This service first reads exact embedding presence by
selected ID, then re-derives only present rows from ``memory_embedding_text``
through the configured provider. The compact float32 vectors are retained for
the roll-up phase in the same run, avoiding duplicate provider calls. Optional
``search_memories_vector`` use is only a drift diagnostic. Without an embedding
provider, clustering is skipped with an explicit reason in the artifact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from inspect import Parameter, signature
import json
import logging
from typing import Any, Protocol, cast

import numpy as np

from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_derived_labels import (
    admit_when_locked,
    group_scope,
    locked_projects,
    with_derived_from,
)
from alicebot_api.vnext_embeddings import (
    MAX_EMBEDDINGS_BATCH_SIZE,
    EmbeddingProvider,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    get_embedding_provider,
    memory_embedding_text,
)
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_version import memory_version_snapshot
from alicebot_api.vnext_model_intelligence import (
    BrainModelProvider,
    ConsolidationMergeRequest,
    ModelBackedRequest,
    ModelRoutingRequest,
    VNextModelIntelligenceError,
    build_model_backed_artifact,
    generate_consolidation_merge,
    resolve_model_route,
)
from alicebot_api.vnext_open_loop_references import sources_named_by_refs
from alicebot_api.vnext_project_scope import project_scope_identity, refuse_global_marker
from alicebot_api.vnext_recall_visibility import drop_expired_memories
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_rollups import (
    RollupOptions,
    RollupOutcome,
    VNextRollupService,
    VNextRollupStore,
)


logger = logging.getLogger(__name__)

DEFAULT_CONSOLIDATION_LIMIT = 12
DEFAULT_SIMILARITY_THRESHOLD = 0.88
DEFAULT_MIN_CLUSTER_SIZE = 2
DEFAULT_MAX_CLUSTERS = 20
# Clustering is quadratic. At 2,000 rows the float32 similarity matrix is
# 16 MB and there are just under two million unique pairs. Keep both limits
# explicit so a future corpus-cap change cannot silently reintroduce an
# unbounded matrix or giant pair-index allocation.
MAX_EMBEDDED_MEMORIES_HARD_CAP = 2000
MAX_SIMILARITY_MATRIX_BYTES = 16_000_000
MAX_PAIRWISE_COMPARISONS = 1_999_000
SIMILARITY_BLOCK_ROWS = 128
PREFERENCE_MEMORY_TYPES = frozenset({"preference", "routine"})
REINFORCED_PREFERENCE_MIN_DISTINCT = 3
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
SENSITIVITY_RANK = {
    "public": 1,
    "internal": 2,
    "unknown": 2,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}


class VNextConsolidationValidationError(ValueError):
    """Raised when a memory-consolidation request is invalid."""


class VNextConsolidationStore(VNextRollupStore, Protocol):
    """Required store surface. ``search_memories_vector``, ``search_sources``,
    ``list_artifacts`` and ``list_artifact_quality_ratings`` are used when
    present (checked via ``getattr``) so slimmer stores still work. The
    ``include_expired`` keyword of ``list_memories`` and ``count_memories`` is
    passed when the method names it (checked with ``signature``); a store that
    does not is filtered after the read."""

    def append_event(self, event: JsonObject) -> JsonObject: ...

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def list_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        projects: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[JsonObject]: ...

    def list_events(self, **kwargs) -> list[JsonObject]: ...

    def count_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        projects: Sequence[str] | None = None,
    ) -> int: ...

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: Sequence[str] | None = None,
    ) -> JsonObject | None: ...


@dataclass(frozen=True, slots=True)
class MemoryConsolidationRequest:
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    generated_for: str | None = None
    # source_limit/memory_limit/artifact_limit/event_limit/rating_limit are
    # kept for scheduler call-shape compatibility; they bound the report's
    # context scan, not the clustering pass (which uses max_embedded_memories).
    source_limit: int = DEFAULT_CONSOLIDATION_LIMIT
    memory_limit: int = DEFAULT_CONSOLIDATION_LIMIT
    artifact_limit: int = 8
    event_limit: int = 30
    rating_limit: int = 20
    create_candidate_memories: bool = True
    generated_by: str = "system"
    trace_id: str | None = None
    run_id: str | None = None
    agent_identity: JsonObject | None = None
    policy_decision: JsonObject | None = None
    metadata_json: JsonObject = field(default_factory=dict)
    generation_mode: str = "deterministic"
    model_route_mode: str | None = None
    model_provider: str | None = None
    model: str | None = None
    model_temperature: float = 0.2
    allow_cloud_private: bool = False
    # Clustering knobs; also overridable via
    # metadata_json["consolidation_options"] = {"similarity_threshold": ...,
    # "max_embedded_memories": ..., "min_cluster_size": ..., "max_clusters": ...}.
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    max_embedded_memories: int = MAX_EMBEDDED_MEMORIES_HARD_CAP
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE
    max_clusters: int = DEFAULT_MAX_CLUSTERS
    # Roll-up pass toggle; knobs come from metadata_json["rollup_options"]
    # (see vnext_rollups.RollupOptions). Disclosed in the artifact metadata.
    propose_rollups: bool = True


@dataclass(frozen=True, slots=True)
class _ClusteringOptions:
    similarity_threshold: float
    max_embedded_memories: int
    min_cluster_size: int
    max_clusters: int


@dataclass(slots=True)
class _ClusteringOutcome:
    clusters: list[list[JsonObject]] = field(default_factory=list)
    similarity_stats: dict[str, JsonObject] = field(default_factory=dict)
    active_count: int = 0
    embedded_count: int = 0
    bounded: bool = False
    active_count_exact: bool = True
    similarity_matrix_bytes: int = 0
    pairwise_comparisons: int = 0
    corpus_digest: str = ""
    probe_self_distance: float | None = None
    skipped: list[str] = field(default_factory=list)
    embedding_vectors: dict[str, np.ndarray] = field(default_factory=dict, repr=False)


def _validate_request(request: MemoryConsolidationRequest) -> None:
    if not request.sensitivity_allowed:
        raise VNextConsolidationValidationError("sensitivity_allowed must not be empty")
    if request.generation_mode not in {"deterministic", "model_backed"}:
        raise VNextConsolidationValidationError("generation_mode must be deterministic or model_backed")
    for field_name in ("source_limit", "memory_limit", "artifact_limit", "event_limit", "rating_limit"):
        value = getattr(request, field_name)
        if value < 1 or value > 100:
            raise VNextConsolidationValidationError(f"{field_name} must be between 1 and 100")
    if request.model_temperature < 0.0 or request.model_temperature > 2.0:
        raise VNextConsolidationValidationError("model_temperature must be between 0.0 and 2.0")
    if len(request.projects) > 50:
        raise VNextConsolidationValidationError("projects must contain at most 50 values")


def _clustering_options(request: MemoryConsolidationRequest) -> _ClusteringOptions:
    overrides = request.metadata_json.get("consolidation_options") if isinstance(request.metadata_json, dict) else None
    overrides = overrides if isinstance(overrides, dict) else {}

    def _number(key: str, default: float) -> float:
        value = overrides.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise VNextConsolidationValidationError(f"consolidation_options.{key} must be a number")
        return float(value)

    threshold = _number("similarity_threshold", request.similarity_threshold)
    cap = int(_number("max_embedded_memories", request.max_embedded_memories))
    min_cluster = int(_number("min_cluster_size", request.min_cluster_size))
    max_clusters = int(_number("max_clusters", request.max_clusters))
    if not (0.0 < threshold <= 1.0):
        raise VNextConsolidationValidationError("similarity_threshold must be in (0.0, 1.0]")
    if not (2 <= cap <= MAX_EMBEDDED_MEMORIES_HARD_CAP):
        raise VNextConsolidationValidationError(
            f"max_embedded_memories must be between 2 and {MAX_EMBEDDED_MEMORIES_HARD_CAP}"
        )
    if not (2 <= min_cluster <= 50):
        raise VNextConsolidationValidationError("min_cluster_size must be between 2 and 50")
    if not (1 <= max_clusters <= 100):
        raise VNextConsolidationValidationError("max_clusters must be between 1 and 100")
    return _ClusteringOptions(
        similarity_threshold=threshold,
        max_embedded_memories=cap,
        min_cluster_size=min_cluster,
        max_clusters=max_clusters,
    )


def _allowed_domains(request: MemoryConsolidationRequest) -> list[str] | None:
    return list(request.domains) if request.domains else None


def _allowed_sensitivity(request: MemoryConsolidationRequest) -> list[str]:
    return list(request.sensitivity_allowed)


def _allowed_projects(request: MemoryConsolidationRequest) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalized
            for value in request.projects
            if (normalized := " ".join(str(value).split()).strip())
        )
    )


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


def _unexpired_only_kwargs(method: object) -> dict[str, Any]:
    """``include_expired=False`` when the store method takes that keyword, else nothing.

    The bundled stores take it. A narrower adapter does not, and its rows are
    filtered by ``drop_expired_memories`` after the read instead.
    """

    return {"include_expired": False} if _supports_explicit_parameter(method, "include_expired") else {}


def _list_memories_bounded(
    store: VNextConsolidationStore,
    *,
    status: str,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    projects: tuple[str, ...],
    limit: int,
) -> list[JsonObject]:
    """Apply the corpus bound in the database when the store supports it.

    Older third-party store adapters only implement ``status``. Keep those
    adapters working while ensuring the bundled PostgreSQL and SQLite stores
    never materialize an unbounded status list for consolidation.

    A memory whose ``valid_to`` has passed is not part of the corpus: recall
    does not return it, its text is not sent to the embeddings endpoint, and a
    merge that superseded it would bring it back as a new memory. The bundled
    stores leave it out in SQL, before ``LIMIT``. A store that cannot is
    filtered here, so an adapter never sends one.
    """

    list_memories = store.list_memories
    parameters: Mapping[str, Parameter]
    try:
        parameters = signature(list_memories).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_kwargs = any(parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values())
    supports_bounded_scope = supports_kwargs or all(
        field_name in parameters
        for field_name in ("limit", "domains", "sensitivity_allowed")
    )
    if projects and "projects" not in parameters:
        raise VNextConsolidationValidationError(
            "project-scoped consolidation requires list_memories with explicit projects support"
        )
    expiry_kwargs = _unexpired_only_kwargs(list_memories)
    if supports_bounded_scope:
        if projects:
            rows = list_memories(
                status=status,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                projects=projects,
                limit=limit,
                **expiry_kwargs,
            )
        else:
            rows = list_memories(
                status=status,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                limit=limit,
                **expiry_kwargs,
            )
        return drop_expired_memories(
            _scoped_rows(
                rows,
                domains=domains,
                sensitivity_allowed=sensitivity_allowed,
                projects=projects,
            )
        )
    if projects:
        raise VNextConsolidationValidationError(
            "project-scoped consolidation cannot use an unbounded legacy memory reader"
        )
    return drop_expired_memories(
        _scoped_rows(
            list_memories(status=status, **expiry_kwargs),
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=projects,
        )
    )[:limit]


def _highest_sensitivity(rows: list[JsonObject]) -> str:
    sensitivities = [str(row.get("sensitivity", "unknown")) for row in rows]
    if not sensitivities:
        return "unknown"
    return max(sensitivities, key=lambda value: SENSITIVITY_RANK.get(value, SENSITIVITY_RANK["unknown"]))


def _domain(request: MemoryConsolidationRequest, rows: list[JsonObject]) -> str:
    if len(request.domains) == 1:
        return derived_domain(rows, fallback=request.domains[0])
    domains = {row.get("domain") for row in rows if isinstance(row.get("domain"), str)}
    if len(domains) == 1:
        return str(next(iter(domains)))
    return derived_domain(rows, fallback="unknown")


def _text(row: JsonObject) -> str:
    for key in ("canonical_text", "summary", "title", "content_markdown", "memory_key"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        raw_text = metadata.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            return " ".join(raw_text.split())
    return str(row.get("id", "item"))


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256(encoded).hexdigest()[:16]


def _cluster_digest(rows: list[JsonObject]) -> str:
    """A cluster identity changes when any reviewed member changes."""
    snapshots = [
        memory_version_snapshot(row)
        for row in sorted(rows, key=lambda item: str(item.get("id")))
    ]
    return _digest_payload({"member_snapshots": snapshots})


def _scoped_rows(
    rows: list[JsonObject],
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    projects: tuple[str, ...] = (),
) -> list[JsonObject]:
    # Explicit project names only: the reserved global marker of a project view
    # would be read as a name no row holds and hide every global memory.
    refuse_global_marker(projects, where="consolidation")
    allowed_sensitivity = set(sensitivity_allowed)
    scoped: list[JsonObject] = []
    for row in rows:
        sensitivity = str(row.get("sensitivity") or "unknown")
        if sensitivity not in allowed_sensitivity:
            continue
        if domains:
            domain = str(row.get("domain") or "unknown")
            if domain not in domains and domain != "unknown":
                continue
        if projects:
            allowed_projects = set(project_scope_identity(projects))
            if not allowed_projects.intersection(group_scope(row)):
                continue
        scoped.append(row)
    return scoped


def _project_scope_key(row: JsonObject) -> tuple[str, ...]:
    """Exact group scope used for safe consolidation groups.

    Overlap is insufficient here: merging a memory scoped to A+B with one
    scoped only to A would widen B-only information into project A. Candidate
    members must therefore carry the same scope set (including the empty
    global scope). A derived row uses its scope united with its floor, which
    equals the scope for an original row.
    """

    return group_scope(row)


def _shared_project_scope(rows: list[JsonObject]) -> tuple[str, ...]:
    if not rows:
        return ()
    first = resource_project_scope(rows[0])
    first_key = _project_scope_key(rows[0])
    if all(_project_scope_key(row) == first_key for row in rows[1:]):
        return first
    return ()


def _member_source_ids(row: JsonObject) -> set[str]:
    sources: set[str] = set()
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        source_id = metadata.get("source_id")
        if source_id is not None:
            sources.add(f"source:{source_id}")
        refs = metadata.get("source_refs")
        if isinstance(refs, list):
            sources.update(str(ref) for ref in refs if isinstance(ref, str) and ref.startswith("source:"))
    return sources


def _member_day(row: JsonObject) -> str | None:
    for key in ("first_seen_at", "created_at", "last_seen_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _member_source_refs(members: list[JsonObject]) -> list[str]:
    refs: list[str] = []
    for member in members:
        if member.get("id") is not None:
            refs.append(f"memory:{member['id']}")
        metadata = member.get("metadata_json")
        if isinstance(metadata, dict) and isinstance(metadata.get("source_refs"), list):
            refs.extend(str(ref) for ref in metadata["source_refs"] if isinstance(ref, str))
    return list(dict.fromkeys(refs))


def _union_source_event_ids(members: list[JsonObject]) -> list[str]:
    merged: list[str] = []
    for member in members:
        event_ids = member.get("source_event_ids")
        if isinstance(event_ids, list):
            merged.extend(str(item) for item in event_ids if item is not None)
    return list(dict.fromkeys(merged))


def _existing_cluster_candidates(
    store: VNextConsolidationStore,
    *,
    projects: tuple[str, ...],
) -> dict[str, str]:
    """Map cluster consolidation_digest -> existing candidate memory id."""
    existing: dict[str, str] = {}
    if projects and not _supports_explicit_parameter(store.list_memories, "projects"):
        raise VNextConsolidationValidationError(
            "project-scoped consolidation requires candidate lookup with explicit projects support"
        )
    if projects:
        candidate_rows = store.list_memories(status="candidate", projects=projects)
    else:
        candidate_rows = store.list_memories(status="candidate")
    for memory in candidate_rows:
        if projects and not _scoped_rows(
            [memory],
            domains=None,
            sensitivity_allowed=list(DEFAULT_SENSITIVITY_ALLOWED),
            projects=projects,
        ):
            raise VNextConsolidationValidationError(
                "candidate lookup returned memory outside the requested project scope"
            )
        metadata = memory.get("metadata_json")
        if not isinstance(metadata, dict) or memory.get("id") is None:
            continue
        digest = metadata.get("consolidation_digest")
        if isinstance(digest, str) and digest:
            existing.setdefault(digest, str(memory["id"]))
    return existing


def _cohesive_clusters(
    normalized: np.ndarray,
    member_rows: list[JsonObject],
    *,
    threshold: float,
    min_cluster_size: int,
) -> list[list[int]]:
    """Build deterministic complete-link groups without a dense N x N matrix.

    A row joins a group only when it clears ``threshold`` against every
    current member. This prevents the A~B~C single-link chain from turning A
    and C into one destructive dedup proposal when A and C are not similar.
    """

    remaining = sorted(range(len(member_rows)), key=lambda index: str(member_rows[index].get("id")))
    clusters: list[list[int]] = []
    while remaining:
        seed, *candidates = remaining
        group = [seed]
        seed_scope = _project_scope_key(member_rows[seed])
        rejected: list[int] = []
        for candidate in candidates:
            if _project_scope_key(member_rows[candidate]) != seed_scope:
                rejected.append(candidate)
                continue
            clears_group = True
            for start in range(0, len(group), SIMILARITY_BLOCK_ROWS):
                block = group[start : start + SIMILARITY_BLOCK_ROWS]
                similarities = normalized[block] @ normalized[candidate]
                if not bool(np.all(similarities >= threshold)):
                    clears_group = False
                    break
            if clears_group:
                group.append(candidate)
            else:
                rejected.append(candidate)
        if len(group) >= min_cluster_size:
            clusters.append(group)
        remaining = rejected
    clusters.sort(
        key=lambda indices: (
            -len(indices),
            min(str(member_rows[index].get("id")) for index in indices),
        )
    )
    return clusters


def _cohesive_pair_stats(normalized: np.ndarray, indices: list[int]) -> JsonObject:
    """Exact pair stats with O(cluster-size) temporary memory."""

    pair_count = 0
    pair_total = 0.0
    pair_min = float("inf")
    pair_max = float("-inf")
    for position, left in enumerate(indices[:-1]):
        right_indices = indices[position + 1 :]
        values = normalized[right_indices] @ normalized[left]
        if values.size == 0:
            continue
        pair_count += int(values.size)
        pair_total += float(values.sum(dtype=np.float64))
        pair_min = min(pair_min, float(values.min()))
        pair_max = max(pair_max, float(values.max()))
    return {
        "pair_count": pair_count,
        "min": round(pair_min, 4),
        "max": round(pair_max, 4),
        "mean": round(pair_total / pair_count, 4),
        "cohesion": "complete_link_all_pairs_at_threshold",
    }


def _one_row_per_id(rows: list[JsonObject]) -> list[JsonObject]:
    """``rows`` with each id kept once, in order. Every row must come from one table. A row with no id cannot be told
    from another, so it is kept."""

    return list({(str(row["id"]) if row.get("id") is not None else id(row)): row for row in rows}.values())


class VNextConsolidationService:
    def __init__(
        self,
        store: VNextConsolidationStore,
        *,
        embedding_provider: EmbeddingProvider | None | str = "ambient",
        merge_provider: BrainModelProvider | None = None,
    ) -> None:
        self.store = store
        if embedding_provider == "ambient":
            self.embedding_provider: EmbeddingProvider | None = get_embedding_provider()
        else:
            self.embedding_provider = embedding_provider  # type: ignore[assignment]
        self.merge_provider = merge_provider

    # -- clustering ------------------------------------------------------------

    def _cluster_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity: list[str],
        projects: tuple[str, ...],
        options: _ClusteringOptions,
        all_of: tuple[str, ...] | None = None,
    ) -> _ClusteringOutcome:
        outcome = _ClusteringOutcome()
        count_memories = getattr(self.store, "count_memories", None)
        count_is_authoritative = False
        if callable(count_memories) and (
            not projects or _supports_explicit_parameter(count_memories, "projects")
        ):
            try:
                # The count and the list behind it leave out an expired memory
                # the same way, so the corpus the report states is the corpus read.
                count_expiry_kwargs = _unexpired_only_kwargs(count_memories)

                def _count(status: str) -> int:
                    if projects:
                        return int(
                            count_memories(
                                status=status,
                                domains=domains,
                                sensitivity_allowed=sensitivity,
                                projects=projects,
                                **count_expiry_kwargs,
                            )
                        )
                    return int(
                        count_memories(
                            status=status,
                            domains=domains,
                            sensitivity_allowed=sensitivity,
                            **count_expiry_kwargs,
                        )
                    )

                outcome.active_count = sum(
                    _count(status)
                    for status in ("active", "accepted")
                )
                count_is_authoritative = True
            except Exception:  # noqa: BLE001 - legacy adapters may expose a narrower count shape
                pass
        outcome.active_count_exact = count_is_authoritative

        # Fetch one look-ahead row per status. Bundled stores report the exact
        # total above; legacy adapters retain an explicit lower-bound fallback.
        status_query_limit = options.max_embedded_memories + 1
        active_status_rows = _list_memories_bounded(
            self.store,
            status="active",
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
            limit=status_query_limit,
        )
        accepted_status_rows = _list_memories_bounded(
            self.store,
            status="accepted",
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
            limit=status_query_limit,
        )
        active_rows = [
            *active_status_rows,
            *accepted_status_rows,
        ]
        if count_is_authoritative and outcome.active_count < len(active_rows):
            # A concurrent insert between count and bounded read must never
            # make the report claim an impossible exact total.
            count_is_authoritative = False
            outcome.active_count_exact = False
        if not count_is_authoritative:
            outcome.active_count_exact = not (
                len(active_status_rows) == status_query_limit
                or len(accepted_status_rows) == status_query_limit
            )
        active_rows.sort(
            key=lambda row: (str(row.get("updated_at") or row.get("created_at") or ""), str(row.get("id"))),
            reverse=True,
        )
        if not count_is_authoritative:
            outcome.active_count = len(active_rows)
        if (
            len(active_rows) > options.max_embedded_memories
            or (count_is_authoritative and outcome.active_count > options.max_embedded_memories)
        ):
            outcome.bounded = True
            logger.info(
                "memory consolidation bounded to %d most recently updated of %s%d active memories",
                options.max_embedded_memories,
                "at least " if not outcome.active_count_exact else "",
                outcome.active_count,
            )
            active_rows = active_rows[: options.max_embedded_memories]
        if all_of is not None:
            active_rows = admit_when_locked("memory", active_rows, all_of)
            outcome.active_count = len(active_rows)
            outcome.active_count_exact = not outcome.bounded
        from alicebot_api.vnext_label_guard import admit_loaded

        active_rows = admit_loaded(
            self.store,
            kind="memory",
            rows=active_rows,
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
            all_of=all_of,
        )
        outcome.corpus_digest = _digest_payload(
            {
                "memory_versions": [
                    memory_version_snapshot(row)
                    for row in sorted(active_rows, key=lambda item: str(item.get("id")))
                ]
            }
        )
        if not active_rows:
            outcome.skipped.append("no_active_memories_in_scope")
            return outcome
        if self.embedding_provider is None:
            outcome.skipped.append("no_embedding_provider_configured")
            return outcome
        search_memories_vector = getattr(self.store, "search_memories_vector", None)
        list_memory_ids_with_embeddings = getattr(
            self.store, "list_memory_ids_with_embeddings", None
        )
        if not callable(list_memory_ids_with_embeddings):
            outcome.skipped.append("store_lacks_embedding_presence_read")
            return outcome

        embeddable = [(row, memory_embedding_text(row)) for row in active_rows]
        embeddable = [(row, text) for row, text in embeddable if text]
        if not embeddable:
            outcome.skipped.append("no_embeddable_memory_text")
            return outcome

        # Presence MUST be resolved before provider work. Re-embedding rows
        # that have no stored vector wastes provider quota and cannot affect
        # clustering because those rows are ineligible by protocol.
        selected_ids = [str(row.get("id")) for row, _ in embeddable]
        try:
            embedded_ids = set(list_memory_ids_with_embeddings(selected_ids))
        except Exception as exc:  # noqa: BLE001 - store backends raise driver-specific errors
            logger.warning(
                "Consolidation embedding-presence read failed error_code=embedding_presence_read_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            outcome.skipped.append("embedding_presence_read_failed")
            return outcome
        embedded_rows_and_text = [
            (row, text)
            for row, text in embeddable
            if str(row.get("id")) in embedded_ids
        ]
        outcome.embedded_count = len(embedded_rows_and_text)
        if len(embedded_rows_and_text) < options.min_cluster_size:
            outcome.skipped.append("fewer_embedded_memories_than_min_cluster_size")
            return outcome

        derived_vectors: list[np.ndarray] = []
        try:
            for start in range(0, len(embedded_rows_and_text), MAX_EMBEDDINGS_BATCH_SIZE):
                batch = embedded_rows_and_text[start : start + MAX_EMBEDDINGS_BATCH_SIZE]
                batch_vectors = self.embedding_provider.embed_batch([text for _row, text in batch])
                if len(batch_vectors) != len(batch):
                    outcome.skipped.append("embedding_provider_returned_wrong_batch_size")
                    return outcome
                derived_vectors.extend(np.asarray(vector, dtype=np.float32) for vector in batch_vectors)
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            logger.warning(
                "Consolidation embedding provider failed error_code=embedding_provider_failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            outcome.skipped.append("embedding_provider_failed")
            return outcome

        # Best-effort drift diagnostic ONLY (never presence): a single probe
        # seeded by the most-recent selected row's re-derived vector reports how
        # far that row's stored vector drifted. A probe failure or a miss simply
        # leaves probe_self_distance unset.
        probe_row_id = str(embedded_rows_and_text[0][0].get("id"))
        probe_rows: list[JsonObject] = []
        if callable(search_memories_vector) and (
            not projects or _supports_explicit_parameter(search_memories_vector, "projects")
        ):
            try:
                if projects:
                    probe_rows = search_memories_vector(
                        query_vector=derived_vectors[0].tolist(),
                        domains=domains,
                        sensitivity_allowed=sensitivity,
                        projects=projects,
                        limit=options.max_embedded_memories,
                    )
                else:
                    probe_rows = search_memories_vector(
                        query_vector=derived_vectors[0].tolist(),
                        domains=domains,
                        sensitivity_allowed=sensitivity,
                        limit=options.max_embedded_memories,
                    )
            except Exception:  # noqa: BLE001 - diagnostic only; presence already resolved
                probe_rows = []
        for row in probe_rows:
            vector_distance = row.get("vector_distance")
            if str(row.get("id")) == probe_row_id and isinstance(
                vector_distance,
                (int, float),
            ):
                outcome.probe_self_distance = float(vector_distance)
                break

        member_count = len(embedded_rows_and_text)
        outcome.similarity_matrix_bytes = (
            min(member_count, SIMILARITY_BLOCK_ROWS)
            * member_count
            * np.dtype(np.float32).itemsize
        )
        outcome.pairwise_comparisons = member_count * (member_count - 1) // 2
        if (
            outcome.similarity_matrix_bytes > MAX_SIMILARITY_MATRIX_BYTES
            or outcome.pairwise_comparisons > MAX_PAIRWISE_COMPARISONS
        ):
            # Defensive fail-closed guard. ``similarity_matrix_bytes`` is the
            # maximum streamed block, never a dense N x N allocation.
            outcome.skipped.append("similarity_resource_guard_exceeded")
            return outcome

        member_rows = [row for row, _text in embedded_rows_and_text]
        width = max(len(vector) for vector in derived_vectors)
        matrix: np.ndarray = np.zeros((member_count, width), dtype=np.float32)
        for index, vector in enumerate(derived_vectors):
            matrix[index, : len(vector)] = vector
        del derived_vectors, embeddable, embedded_rows_and_text
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0
        matrix /= norms[:, None]
        normalized = matrix
        del norms

        clusters = _cohesive_clusters(
            normalized,
            member_rows,
            threshold=options.similarity_threshold,
            min_cluster_size=options.min_cluster_size,
        )

        for indices in clusters:
            rows = sorted((member_rows[index] for index in indices), key=lambda row: str(row.get("id")))
            digest = _cluster_digest(rows)
            outcome.clusters.append(rows)
            outcome.similarity_stats[digest] = _cohesive_pair_stats(normalized, indices)
        outcome.embedding_vectors = {
            str(row.get("id")): normalized[index].copy()
            for index, row in enumerate(member_rows)
        }
        return outcome

    # -- proposals ---------------------------------------------------------------

    def _build_proposal(
        self,
        *,
        request: MemoryConsolidationRequest,
        members: list[JsonObject],
        cluster_digest: str,
        similarity_stats: JsonObject,
        route,
    ) -> JsonObject:
        member_ids = [str(row.get("id")) for row in members]
        survivor = max(members, key=lambda row: (len(str(row.get("canonical_text") or "")), str(row.get("id"))))
        proposal_kind = "dedup"
        title = f"Duplicate cluster: {_text(survivor)[:120]}"
        canonical_text = _text(survivor)
        model_provenance: JsonObject | None = None
        merge_refusal: str | None = None
        if request.generation_mode == "model_backed" and route is not None:
            merge = generate_consolidation_merge(
                ConsolidationMergeRequest(
                    cluster_members=tuple(members),
                    route=route,
                    temperature=request.model_temperature,
                    trace_id=request.trace_id,
                ),
                provider=self.merge_provider,
            )
            model_provenance = merge.model_provenance
            if merge.merged:
                proposal_kind = "merge"
                title = str(merge.title)
                canonical_text = str(merge.canonical_text)
            else:
                merge_refusal = merge.refusal_reason
        proposed_supersede = member_ids if proposal_kind == "merge" else [
            member_id for member_id in member_ids
        ]
        return {
            "proposal_kind": proposal_kind,
            "cluster_member_ids": member_ids,
            "consolidation_digest": cluster_digest,
            "similarity_stats": similarity_stats,
            "survivor_memory_id": str(survivor.get("id")) if proposal_kind == "dedup" else None,
            "proposed_supersede": proposed_supersede,
            "title": title,
            "canonical_text": canonical_text,
            "model_provenance": model_provenance,
            "merge_refusal": merge_refusal,
            "source_refs": _member_source_refs(members),
            "source_event_ids": _union_source_event_ids(members),
            "member_snapshots": [memory_version_snapshot(row) for row in members],
            "members": members,
        }

    def _create_proposal_candidate(
        self,
        *,
        request: MemoryConsolidationRequest,
        proposal: JsonObject,
    ) -> JsonObject:
        members_value = proposal["members"]
        members = (
            [cast(JsonObject, member) for member in members_value if isinstance(member, dict)]
            if isinstance(members_value, list)
            else []
        )
        member_ids_value = proposal["cluster_member_ids"]
        member_ids = (
            [str(member_id) for member_id in member_ids_value]
            if isinstance(member_ids_value, list)
            else []
        )
        cluster_digest = str(proposal["consolidation_digest"])
        proposal_kind = str(proposal["proposal_kind"])
        memory_types = Counter(
            str(row.get("memory_type")) for row in members if isinstance(row.get("memory_type"), str)
        )
        memory_type = memory_types.most_common(1)[0][0] if memory_types else "semantic"
        proposed_supersede_value = proposal.get("proposed_supersede")
        proposed_supersede = (
            [str(member_id) for member_id in proposed_supersede_value]
            if isinstance(proposed_supersede_value, list)
            else []
        )
        similarity_stats_value = proposal.get("similarity_stats")
        similarity_stats = (
            similarity_stats_value if isinstance(similarity_stats_value, Mapping) else {}
        )
        reviewer_instructions = [
            f"Review candidate memory for cluster {cluster_digest}; accepting it is the promotion decision.",
            "Accepting through the memory commit service (accept_consolidation_candidate) promotes this "
            "candidate and executes the proposed supersessions "
            f"(members proposed for supersession: {', '.join(proposed_supersede) or 'none'}).",
            "Consolidation never supersedes active memories automatically; nothing changes until a reviewer accepts.",
        ]
        project_scope = _shared_project_scope(members)
        return self.store.create_memory(
            {
                "memory_key": f"vnext.consolidation.{cluster_digest}",
                "value": {
                    "kind": "memory_consolidation_proposal",
                    "proposal_kind": proposal_kind,
                    "consolidation_digest": cluster_digest,
                    "cluster_member_ids": member_ids,
                    "text": proposal["canonical_text"],
                },
                "status": "candidate",
                "memory_type": memory_type,
                "confidence": 0.6 if proposal_kind == "merge" else 0.75,
                "trust_class": "llm_single_source" if proposal_kind == "merge" else "deterministic",
                "promotion_eligibility": "promotable",
                "title": proposal["title"],
                "canonical_text": proposal["canonical_text"],
                "summary": (
                    f"{proposal_kind} proposal covering {len(member_ids)} near-duplicate memories "
                    f"(mean cosine {similarity_stats.get('mean')})."
                ),
                "domain": _domain(request, members),
                "sensitivity": _highest_sensitivity(members),
                "project_id": project_scope[0] if len(project_scope) == 1 else None,
                "source_event_ids": proposal["source_event_ids"],
                "metadata_json": with_derived_from(
                    {
                        "candidate_kind": "memory_consolidation",
                        "consolidation_digest": cluster_digest,
                        "source_refs": proposal["source_refs"],
                        "project_scope": list(project_scope),
                        "review_required": True,
                        "consolidation": {
                            "cluster_member_ids": member_ids,
                            "member_snapshots": proposal["member_snapshots"],
                            "similarity_stats": proposal["similarity_stats"],
                            "proposal_kind": proposal_kind,
                            "model_provenance": proposal["model_provenance"],
                            "survivor_memory_id": proposal["survivor_memory_id"],
                            "proposed_supersede": proposal["proposed_supersede"],
                            "merge_refusal": proposal["merge_refusal"],
                            "reviewer_instructions": reviewer_instructions,
                        },
                    },
                    {"memories": members},
                ),
            },
            actor_type=request.generated_by,
        )

    # -- reinforced preferences ----------------------------------------------------

    def _reinforced_preferences(self, clusters: list[list[JsonObject]]) -> list[JsonObject]:
        notes: list[JsonObject] = []
        for members in clusters:
            types = {str(row.get("memory_type") or "") for row in members}
            if not types or not types.issubset(PREFERENCE_MEMORY_TYPES):
                continue
            source_ids: set[str] = set()
            days: set[str] = set()
            for row in members:
                source_ids |= _member_source_ids(row)
                day = _member_day(row)
                if day is not None:
                    days.add(day)
            if (
                len(source_ids) < REINFORCED_PREFERENCE_MIN_DISTINCT
                and len(days) < REINFORCED_PREFERENCE_MIN_DISTINCT
            ):
                continue
            member_ids = [str(row.get("id")) for row in members]
            notes.append(
                {
                    "cluster_member_ids": member_ids,
                    "consolidation_digest": _digest_payload({"cluster_member_ids": sorted(member_ids)}),
                    "distinct_source_count": len(source_ids),
                    "distinct_day_count": len(days),
                    "memory_types": sorted(types),
                    "suggestion": (
                        "Repeated preference observed across independent sources/days; "
                        "consider promotion or a confidence bump after human review."
                    ),
                }
            )
        return notes

    # -- report ---------------------------------------------------------------

    def generate_memory_consolidation(self, request: MemoryConsolidationRequest | None = None) -> JsonObject:
        request = request or MemoryConsolidationRequest()
        _validate_request(request)
        options = _clustering_options(request)
        # Parsed before any write so invalid roll-up options fail the run
        # exactly like invalid clustering options do.
        rollup_options = RollupOptions.from_metadata(request.metadata_json) if request.propose_rollups else None
        domains = _allowed_domains(request)
        sensitivity = _allowed_sensitivity(request)
        projects = _allowed_projects(request)
        all_of = locked_projects(request.agent_identity, projects)

        if projects:
            list_memory_events = getattr(self.store, "list_memory_events", None)
            if callable(list_memory_events) and _supports_explicit_parameter(
                list_memory_events, "scope_projects"
            ):
                events = list_memory_events(
                    scope_projects=projects,
                    limit=request.event_limit,
                )
            else:
                # Event context is optional. Omitting it is safe; reading an
                # unscoped prefix and filtering after LIMIT is not.
                events = []
        else:
            events = self.store.list_events(limit=request.event_limit)
        events = [
            event
            for event in events
            if not str(event.get("event_type") or "").startswith("scheduler.")
            and event.get("event_type")
            not in {"memory.consolidation.generated", "artifact.created"}
        ]
        ratings: list[JsonObject] = []
        list_ratings = getattr(self.store, "list_artifact_quality_ratings", None)
        if callable(list_ratings):
            if projects:
                if _supports_explicit_parameter(list_ratings, "scope_projects"):
                    ratings = list_ratings(
                        limit=request.rating_limit,
                        scope_projects=projects,
                    )
            else:
                ratings = list_ratings(limit=request.rating_limit)
        artifacts: list[JsonObject] = []
        list_artifacts = getattr(self.store, "list_artifacts", None)
        if callable(list_artifacts):
            artifact_kwargs: dict[str, object] = {
                "domains": domains,
                "sensitivity_allowed": sensitivity,
                "limit": request.artifact_limit,
            }
            if projects:
                if not _supports_explicit_parameter(list_artifacts, "scope_projects"):
                    raise VNextConsolidationValidationError(
                        "project-scoped consolidation requires artifact lookup with explicit scope_projects support"
                    )
                artifact_kwargs["scope_projects"] = projects
            artifacts = [
                row
                for row in list_artifacts(**artifact_kwargs)
                if row.get("artifact_type") != "memory_consolidation"
            ]
        if projects:
            events = _scoped_rows(
                events,
                domains=None,
                sensitivity_allowed=sensitivity,
                projects=projects,
            )
            ratings = _scoped_rows(
                ratings,
                domains=None,
                sensitivity_allowed=sensitivity,
                projects=projects,
            )
            artifacts = _scoped_rows(
                artifacts,
                domains=domains,
                sensitivity_allowed=sensitivity,
                projects=projects,
            )
        events = admit_when_locked("memory", events, all_of)
        ratings = admit_when_locked("memory", ratings, all_of)
        artifacts = admit_when_locked("artifact", artifacts, all_of)
        from alicebot_api.vnext_label_guard import admit_loaded
        artifacts = admit_loaded(
            self.store, kind="artifact", rows=artifacts, domains=domains,
            sensitivity_allowed=sensitivity, projects=projects, all_of=all_of,
        )

        clustering = self._cluster_memories(
            domains=domains,
            sensitivity=sensitivity,
            projects=projects,
            options=options,
            all_of=all_of,
        )
        cluster_membership = [
            sorted(str(row.get("id")) for row in members) for members in clustering.clusters
        ]
        brain_charter = self._brain_charter()
        clusters_for_proposals = clustering.clusters[: options.max_clusters]
        # The report copies the ``source_refs`` of each proposed cluster member as stored, and the candidate memories
        # copy them too. The refs are not dropped: they are the provenance. But the report is read behind its label
        # alone, so the label has to be at least as strict as every source they name. The list printed is made here,
        # once, and the rows it names are read here, once, because the run digest and the label both cover them.
        report_source_refs = list(
            dict.fromkeys(ref for members in clusters_for_proposals for ref in _member_source_refs(members))
        )
        named_sources = sources_named_by_refs(self.store, report_source_refs)
        run_digest = _digest_payload(
            {
                "scope": {
                    "domains": request.domains,
                    "projects": projects,
                    "sensitivity_allowed": request.sensitivity_allowed,
                },
                "limits": {
                    "source": request.source_limit,
                    "memory": request.memory_limit,
                    "artifact": request.artifact_limit,
                    "event": request.event_limit,
                    "rating": request.rating_limit,
                },
                "behavior": {
                    "create_candidate_memories": request.create_candidate_memories,
                    "propose_rollups": request.propose_rollups,
                    "clustering": {
                        "similarity_threshold": options.similarity_threshold,
                        "max_embedded_memories": options.max_embedded_memories,
                        "min_cluster_size": options.min_cluster_size,
                        "max_clusters": options.max_clusters,
                    },
                    "rollups": rollup_options.to_record() if rollup_options is not None else None,
                    "generation_mode": request.generation_mode,
                    "model_route_mode": request.model_route_mode,
                    "model_provider": request.model_provider,
                    "model": request.model,
                    "model_temperature": request.model_temperature,
                    "allow_cloud_private": request.allow_cloud_private,
                    "generated_for": request.generated_for,
                    "generated_by": request.generated_by,
                    "agent_identity": request.agent_identity,
                    "brain_charter": brain_charter,
                },
                "corpus_digest": clustering.corpus_digest,
                "cluster_membership": cluster_membership,
                "embedded_count": clustering.embedded_count,
                "context": {
                    "events": _digest_payload(events),
                    "artifacts": _digest_payload(artifacts),
                    "ratings": _digest_payload(ratings),
                },
                # Part of the identity of the run: a source that was reclassified since the last report makes a new
                # one, instead of returning the report that carries the earlier label. A run that names no source
                # has the digest it always had.
                **(
                    {
                        "named_sources": [
                            {"id": str(row.get("id")), "domain": row.get("domain"), "sensitivity": row.get("sensitivity")}
                            for row in named_sources
                        ]
                    }
                    if named_sources
                    else {}
                ),
            }
        )
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing_report = find_existing(
                artifact_type="memory_consolidation",
                workflow="memory_consolidation",
                digest=run_digest,
                scope_projects=projects or None,
            )
            if existing_report is not None:
                return existing_report

        route = None
        if request.generation_mode == "model_backed":
            route = resolve_model_route(
                ModelRoutingRequest(
                    workflow_type="memory_consolidation",
                    generation_mode="model_backed",
                    domains=request.domains,
                    sensitivity_allowed=request.sensitivity_allowed,
                    agent_identity=request.agent_identity,
                    brain_charter=brain_charter,
                    requested_route_mode=request.model_route_mode,
                    requested_provider=request.model_provider,
                    requested_model=request.model,
                    allow_cloud_private=request.allow_cloud_private,
                )
            )
            if route.approval_required or route.route_mode == "model_disabled":
                raise VNextModelIntelligenceError("model-backed generation is not allowed by routing policy")

        existing_candidates = _existing_cluster_candidates(self.store, projects=projects)

        proposals: list[JsonObject] = []
        skipped: list[str] = list(clustering.skipped)
        candidate_ids: list[str] = []
        if len(clustering.clusters) > options.max_clusters:
            skipped.append(
                f"cluster_bound: {len(clustering.clusters) - options.max_clusters} clusters beyond "
                f"max_clusters={options.max_clusters} were not proposed this run"
            )
        for members in clusters_for_proposals:
            member_ids = [str(row.get("id")) for row in members]
            cluster_digest = _cluster_digest(members)
            similarity_stats = clustering.similarity_stats.get(cluster_digest, {})
            proposal = self._build_proposal(
                request=request,
                members=members,
                cluster_digest=cluster_digest,
                similarity_stats=similarity_stats,
                route=route,
            )
            existing_id = existing_candidates.get(cluster_digest)
            if existing_id is not None:
                proposal["candidate_memory_id"] = existing_id
                proposal["candidate_state"] = "existing"
                candidate_ids.append(existing_id)
            elif request.create_candidate_memories:
                candidate = self._create_proposal_candidate(request=request, proposal=proposal)
                proposal["candidate_memory_id"] = str(candidate["id"])
                proposal["candidate_state"] = "created"
                candidate_ids.append(str(candidate["id"]))
            else:
                proposal["candidate_memory_id"] = None
                proposal["candidate_state"] = "not_created (create_candidate_memories=false)"
            proposals.append(proposal)

        reinforced = self._reinforced_preferences(clustering.clusters)

        # Roll-up pass: same-topic aggregation cards, review-gated like the
        # merge/dedup proposals above. Runs only in this scheduled workflow
        # (never on the memory commit path) and never mutates existing rows.
        rollups: RollupOutcome | None = None
        if rollup_options is not None:
            # lme5/semantic-grouping integration: the roll-up pass reuses
            # this service's already-resolved embedding provider so its
            # semantic tier and the clustering above always see the same
            # seam (both dormant when no provider is configured).
            rollups = VNextRollupService(
                self.store,
                merge_provider=self.merge_provider,
                embedding_provider=self.embedding_provider,
                precomputed_embeddings=clustering.embedding_vectors,
            ).propose_rollups(
                domains=domains,
                projects=projects,
                sensitivity_allowed=sensitivity,
                options=rollup_options,
                create_candidate_memories=request.create_candidate_memories,
                generated_by=request.generated_by,
                trace_id=request.trace_id,
                generation_mode=request.generation_mode,
                route=route,
                model_temperature=request.model_temperature,
                agent_identity=request.agent_identity,
                # Near-duplicate clusters belong to the dedup/merge proposals
                # above; the roll-up pass must not re-propose those groups.
                exclude_member_id_sets=[
                    {str(row.get("id")) for row in members} for members in clustering.clusters
                ],
            )
            candidate_ids.extend(rollups.candidate_ids)

        content = self._render_markdown(
            request=request,
            options=options,
            clustering=clustering,
            proposals=proposals,
            reinforced=reinforced,
            rollups=rollups,
            skipped=skipped,
            events_count=len(events),
            artifacts_count=len(artifacts),
            ratings_count=len(ratings),
        )

        proposal_records = [
            {
                key: proposal[key]
                for key in (
                    "proposal_kind",
                    "cluster_member_ids",
                    "consolidation_digest",
                    "similarity_stats",
                    "survivor_memory_id",
                    "proposed_supersede",
                    "model_provenance",
                    "merge_refusal",
                    "candidate_memory_id",
                    "candidate_state",
                    "source_refs",
                )
            }
            for proposal in proposals
        ]
        metadata = {
            **request.metadata_json,
            "workflow": "memory_consolidation",
            "workflow_type": "memory_consolidation",
            "trace_id": request.trace_id,
            "scheduler_run_id": request.run_id,
            "review_status": "needs_review",
            "generation_mode": request.generation_mode,
            "project_scope": list(projects),
            "source_refs": report_source_refs,
            "consolidation_digest": run_digest,
            "candidate_memory_ids": candidate_ids,
            "consolidation": {
                "embedding_access": "exact_id_presence_read_then_provider_reembed_reused_by_rollups",
                "clustering": "complete_link_all_pairs_at_threshold",
                "similarity_threshold": options.similarity_threshold,
                "min_cluster_size": options.min_cluster_size,
                "max_embedded_memories": options.max_embedded_memories,
                "bounded": clustering.bounded,
                "active_memory_count_exact": clustering.active_count_exact,
                "resource_guard": {
                    "matrix_dtype": "float32",
                    "matrix_materialization": False,
                    "similarity_block_rows": SIMILARITY_BLOCK_ROWS,
                    "peak_similarity_block_bytes": clustering.similarity_matrix_bytes,
                    # Compatibility alias; now the actual peak streamed
                    # similarity block, not an N x N allocation.
                    "matrix_bytes": clustering.similarity_matrix_bytes,
                    "matrix_bytes_hard_cap": MAX_SIMILARITY_MATRIX_BYTES,
                    "pairwise_comparisons": clustering.pairwise_comparisons,
                    "pairwise_comparisons_hard_cap": MAX_PAIRWISE_COMPARISONS,
                    "pair_index_materialization": False,
                },
                "probe_self_distance": clustering.probe_self_distance,
                "cluster_membership": cluster_membership,
                "proposals": proposal_records,
                "reinforced_preferences": reinforced,
                "skipped": skipped,
            },
            "rollups": {"enabled": True, **rollups.to_metadata()} if rollups is not None else {"enabled": False},
            "input_counts": {
                "active_memories": clustering.active_count,
                "active_memories_exact": clustering.active_count_exact,
                "embedded_memories": clustering.embedded_count,
                "clusters": len(clustering.clusters),
                "proposals": len(proposals),
                "reinforced_preferences": len(reinforced),
                "rollup_proposals": len(rollups.proposals) if rollups is not None else 0,
                "artifacts": len(artifacts),
                "events": len(events),
                "ratings": len(ratings),
            },
            "policy_decision": request.policy_decision,
            "agent_identity": request.agent_identity,
        }

        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if request.generation_mode == "model_backed" and route is not None:
            context_rows = tuple(row for members in clusters_for_proposals for row in members)[:10]
            model_artifact = build_model_backed_artifact(
                ModelBackedRequest(
                    workflow_type="memory_consolidation",
                    title=self._title(request),
                    deterministic_markdown=content,
                    context_rows=context_rows,
                    source_refs=tuple(report_source_refs),
                    open_questions=("Which consolidation proposal should be promoted, edited, or rejected?",),
                    trace_id=request.trace_id,
                    route=route,
                    temperature=request.model_temperature,
                    config={"generated_by": request.generated_by},
                )
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            metadata = {**metadata, **model_artifact.metadata}

        all_cluster_rows = [row for members in clustering.clusters for row in members]
        metadata = with_derived_from(
            metadata,
            {
                "sources": named_sources,
                "memories": [
                    *all_cluster_rows,
                    *(rollups.input_rows if rollups is not None else []),
                ],
                "artifacts": artifacts,
            },
        )
        # The report is read behind its domain and sensitivity, so both are taken over every row it names: the
        # near-duplicate cluster members, every row the roll-up pass names, and the sources that the refs it prints
        # name (read above, after the run's own fence chose the members the refs are copied from). A run whose only
        # proposals are roll-ups has no cluster, and its label used to come from nothing. A row counts once, because
        # the domain is the most frequent restricted label among the rows, and a cluster member is named by its
        # cluster and again by the roll-up line that leaves its group to the dedup proposal. Memories and sources are
        # counted apart: an id is unique only within its own table, so a source may carry the id of a memory, and
        # one must never stand in for the other's label.
        labelled_rows = [
            *_one_row_per_id([*all_cluster_rows, *(rollups.input_rows if rollups is not None else [])]),
            *_one_row_per_id(named_sources),
        ]
        artifact = self.store.create_artifact(
            {
                "artifact_type": "memory_consolidation",
                "title": self._title(request),
                "content_markdown": content,
                "status": "needs_review",
                "domain": derived_domain(labelled_rows, fallback=_domain(request, all_cluster_rows)),
                "sensitivity": _highest_sensitivity(labelled_rows),
                "generated_by": request.generated_by,
                "prompt_hash": prompt_hash,
                "model_info_json": model_info_json,
                "metadata_json": metadata,
            },
            actor_type=request.generated_by,
        )
        append_event(
            self.store,
            event_type="memory.consolidation.generated",
            actor_type=request.generated_by,
            target_type="artifact",
            target_id=str(artifact["id"]),
            trace_id=request.trace_id,
            run_id=request.run_id,
            payload={
                "consolidation_digest": run_digest,
                "candidate_memory_ids": candidate_ids,
                "rollup_candidate_ids": rollups.candidate_ids if rollups is not None else [],
                "input_counts": metadata["input_counts"],
                "skipped": skipped,
            },
        )
        return artifact

    def _title(self, request: MemoryConsolidationRequest) -> str:
        return f"Memory Consolidation - {request.generated_for or datetime.now(UTC).date().isoformat()}"

    def _render_markdown(
        self,
        *,
        request: MemoryConsolidationRequest,
        options: _ClusteringOptions,
        clustering: _ClusteringOutcome,
        proposals: list[JsonObject],
        reinforced: list[JsonObject],
        rollups: RollupOutcome | None,
        skipped: list[str],
        events_count: int,
        artifacts_count: int,
        ratings_count: int,
    ) -> str:
        cluster_lines: list[str] = []
        for members in clustering.clusters:
            member_ids = [str(row.get("id")) for row in members]
            digest = _cluster_digest(members)
            stats = clustering.similarity_stats.get(digest, {})
            cluster_lines.append(
                f"- Cluster `{digest}` ({len(members)} members, mean cosine {stats.get('mean')}): "
                + "; ".join(f"{_text(row)[:80]} [memory:{row.get('id')}]" for row in members)
            )
        if not cluster_lines:
            cluster_lines = ["- No near-duplicate clusters were found in scope."]

        proposal_lines: list[str] = []
        for proposal in proposals:
            cluster_member_ids_value = proposal.get("cluster_member_ids")
            cluster_member_ids = (
                [str(member_id) for member_id in cluster_member_ids_value]
                if isinstance(cluster_member_ids_value, list)
                else []
            )
            proposed_supersede_value = proposal.get("proposed_supersede")
            proposed_supersede = (
                [str(member_id) for member_id in proposed_supersede_value]
                if isinstance(proposed_supersede_value, list)
                else []
            )
            proposal_lines.append(
                f"- `{proposal['consolidation_digest']}` {proposal['proposal_kind']} proposal "
                f"({proposal['candidate_state']}, candidate: {proposal['candidate_memory_id']}) - "
                f"members: {', '.join(cluster_member_ids)}; "
                f"proposed supersede after acceptance: {', '.join(proposed_supersede) or 'none'}"
                + (f"; merge refused: {proposal['merge_refusal']}" if proposal.get("merge_refusal") else "")
            )
        if not proposal_lines:
            proposal_lines = ["- No merge or dedup proposals were created this run."]

        reinforced_lines = [
            f"- Cluster `{note['consolidation_digest']}` is a reinforced preference: "
            f"{note['distinct_source_count']} distinct sources, {note['distinct_day_count']} distinct days. "
            f"{note['suggestion']}"
            for note in reinforced
        ] or ["- No reinforced preferences were detected."]

        rollup_lines = (
            rollups.markdown_lines()
            if rollups is not None
            else ["- Roll-up proposals were disabled for this run (propose_rollups=false)."]
        )

        skipped_lines = [f"- {reason}" for reason in skipped]
        active_count_label = (
            str(clustering.active_count)
            if clustering.active_count_exact
            else f"at least {clustering.active_count}"
        )
        if clustering.bounded:
            skipped_lines.append(
                f"- Clustering was bounded to the {options.max_embedded_memories} most recently updated "
                f"memories of {active_count_label} in scope."
            )
        if clustering.probe_self_distance is not None and clustering.probe_self_distance > (
            1.0 - options.similarity_threshold
        ):
            skipped_lines.append(
                f"- Warning: stored embeddings may be stale (probe self-distance "
                f"{clustering.probe_self_distance:.4f}); rerun the embeddings backfill."
            )
        if not skipped_lines:
            skipped_lines = ["- Nothing was skipped; no bounds were hit."]

        return "\n".join(
            [
                f"# {self._title(request)}",
                "",
                "## Consolidation Summary",
                f"- Active memories in scope: {active_count_label}",
                f"- Memories with stored embeddings considered: {clustering.embedded_count}",
                f"- Similarity threshold: {options.similarity_threshold} (min cluster size {options.min_cluster_size})",
                f"- Clusters found: {len(clustering.clusters)}",
                f"- Proposals: {len(proposals)}",
                f"- Reinforced preferences: {len(reinforced)}",
                f"- Roll-up proposals: {len(rollups.proposals) if rollups is not None else 0}",
                f"- Context scanned: {artifacts_count} artifacts, {events_count} events, {ratings_count} ratings",
                "",
                "## Near-Duplicate Clusters",
                *cluster_lines,
                "",
                "## Merge / Dedup Proposals",
                *proposal_lines,
                "",
                "## Reinforced Preferences",
                *reinforced_lines,
                "",
                "## Roll-up Proposals",
                *rollup_lines,
                "",
                "## Skipped / Bounds",
                *skipped_lines,
                "",
                "## Review Policy",
                "- This artifact is review-only.",
                "- Proposals are candidate memories; accepting a candidate is the promotion decision.",
                "- Accepting a candidate (memory commit service: accept_consolidation_candidate) promotes it",
                "  and supersedes the members marked `proposed_supersede`; consolidation itself never",
                "  supersedes automatically.",
                "- Roll-up cards propose superseding NOTHING (a revision retires only the previous card);",
                "  members always stay active and individually recallable.",
            ]
        )

    def _brain_charter(self) -> JsonObject | None:
        getter = getattr(self.store, "get_brain_charter", None)
        if not callable(getter):
            return None
        charter = getter()
        return charter if isinstance(charter, dict) else None


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "MAX_EMBEDDED_MEMORIES_HARD_CAP",
    "MAX_PAIRWISE_COMPARISONS",
    "MAX_SIMILARITY_MATRIX_BYTES",
    "SIMILARITY_BLOCK_ROWS",
    "MemoryConsolidationRequest",
    "VNextConsolidationService",
    "VNextConsolidationStore",
    "VNextConsolidationValidationError",
]
