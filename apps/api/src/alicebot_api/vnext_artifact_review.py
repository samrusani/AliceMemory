from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_project_update_guard import is_project_update_artifact
from alicebot_api.vnext_projects import VNextProjectService, VNextProjectStore
from alicebot_api.vnext_queue import VNextQueueNotFoundError, VNextQueueService, VNextQueueStore
from alicebot_api.vnext_repositories import JsonObject


class VNextArtifactReviewDispatchStore(Protocol):
    def get_artifact_for_update(self, artifact_id: str) -> JsonObject | None: ...


@dataclass(frozen=True, slots=True)
class VNextArtifactReviewDispatchResult:
    artifact: JsonObject
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()


def dispatch_vnext_artifact_review(
    store: VNextArtifactReviewDispatchStore,
    *,
    artifact_id: str,
    action: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
) -> VNextArtifactReviewDispatchResult:
    """Route every artifact review through its owning lifecycle service.

    The dispatcher always takes the artifact lock for classification. The
    selected service then reacquires the same transaction-local lock before
    mutating it, so no caller can route from a stale or forged preloaded row.
    """

    lock_graph = getattr(store, "lock_graph_mutation", None)
    if callable(lock_graph):
        lock_graph()
        lock_artifact_review_labels(store, artifact_id=artifact_id, action=action)
    target = store.get_artifact_for_update(artifact_id)
    if target is None:
        raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
    if is_project_update_artifact(target):
        service = VNextProjectService(cast(VNextProjectStore, store), defer_embeddings=True)
        reviewed = service.review_project_update(
            artifact_id=artifact_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
            run_id=run_id,
        )
        return VNextArtifactReviewDispatchResult(
            artifact=reviewed,
            deferred_embedding_inputs=service.deferred_embedding_inputs,
        )
    queue_service = VNextQueueService(cast(VNextQueueStore, store), defer_embeddings=True)
    reviewed = queue_service.review_artifact(
        artifact_id=artifact_id,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
        run_id=run_id,
    )
    return VNextArtifactReviewDispatchResult(
        artifact=reviewed,
        deferred_embedding_inputs=queue_service.deferred_embedding_inputs,
    )


def lock_artifact_review_labels(store, *, artifact_id: str, action: str) -> None:
    """Project acceptance changes labels; ordinary artifact review does not."""
    from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock

    if not callable(getattr(store, "lock_label_writes", None)):
        return
    getter = getattr(store, "get_artifact", None)
    target = getter(artifact_id) if callable(getter) else None
    # Older adapters expose only the locking read. Until that read establishes
    # the type, acceptance may change a project label and needs exclusivity.
    if (target is None or is_project_update_artifact(target)) and action in {"accept", "edit", "promote"}:
        acquire_exclusive_label_lock(store)
    else:
        lock = getattr(store, "lock_label_writes", None)
        if callable(lock):
            lock()


__all__ = [
    "VNextArtifactReviewDispatchResult",
    "VNextArtifactReviewDispatchStore",
    "dispatch_vnext_artifact_review",
]
