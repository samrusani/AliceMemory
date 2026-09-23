from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
from typing import Protocol

from alicebot_api.credential_floor import refuse_credential_material
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_project_update_guard import is_project_update_artifact
from alicebot_api.vnext_repositories import JsonObject

DEFAULT_VNEXT_ARTIFACT_EXPORT_ROOT = Path("/tmp/alicebot-vnext-artifact-exports")
QUEUE_TASK_PROCESSING_ERROR_CODE = "queue_task_processing_failed"
QUEUE_TASK_PROCESSING_ERROR_MESSAGE = "Queue task processing failed"
logger = logging.getLogger(__name__)

# Artifact review is a lifecycle, not an arbitrary status setter.  Keeping the
# complete transition table in one place makes terminal-state behavior
# reviewable and prevents a promoted memory source from later being presented
# as rejected or archived while its trusted memory remains active.
ARTIFACT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"reviewed", "accepted", "rejected", "archived", "promoted_to_memory"}),
    "needs_review": frozenset({"reviewed", "accepted", "rejected", "archived", "promoted_to_memory"}),
    "reviewed": frozenset({"accepted", "rejected", "archived", "promoted_to_memory"}),
    "accepted": frozenset({"rejected", "archived", "promoted_to_memory"}),
    "rejected": frozenset({"archived"}),
    "superseded": frozenset({"archived"}),
    "archived": frozenset(),
    "promoted_to_memory": frozenset(),
}


class VNextQueueValidationError(ValueError):
    """Raised when a vNext queue or artifact operation is invalid."""


class VNextQueueNotFoundError(VNextQueueValidationError):
    """Raised when a vNext queue artifact or task cannot be found."""


class VNextQueueStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def list_events(self, *, target_type: str | None = None, target_id: str | None = None) -> list[JsonObject]: ...

    def create_task(self, task: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def claim_next_task(self) -> JsonObject | None: ...

    def update_task_status(self, *, task_id: str, status: str, details: JsonObject | None = None) -> JsonObject: ...

    def create_artifact(self, artifact: JsonObject) -> JsonObject: ...

    def get_artifact(self, artifact_id: str) -> JsonObject | None: ...

    def get_artifact_for_update(self, artifact_id: str) -> JsonObject | None: ...

    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        actor_type: str = "system",
    ) -> JsonObject | None: ...

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class QueueTaskRequest:
    title: str
    task_type: str
    instructions: str
    requested_by: str = "user"
    scope_json: JsonObject = field(default_factory=dict)
    allowed_sources_json: list[object] = field(default_factory=list)
    domain: str = "unknown"
    sensitivity: str = "unknown"
    write_policy: str = "proposal_only"
    scheduled_for: str | None = None
    metadata_json: JsonObject = field(default_factory=dict)
    actor_type: str = "system"
    actor_id: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    agent_identity: JsonObject | None = None
    policy_decision: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class QueueProcessResult:
    status: str
    task_id: str | None = None
    artifact_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_record(self) -> JsonObject:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _normalize_required_text(value: str, *, field_name: str) -> str:
    normalized = " ".join(value.split()).strip()
    if normalized == "":
        raise VNextQueueValidationError(f"{field_name} must not be empty")
    return normalized


def _artifact_type_for_task(task_type: str) -> str:
    if task_type == "draft":
        return "draft"
    if task_type == "create_context_pack":
        return "context_pack"
    if task_type in {"research", "analyze", "compare", "summarize"}:
        return "research_brief"
    return "queue_result"


def _artifact_status_for_policy(write_policy: str) -> str:
    if write_policy == "auto_generate_artifact":
        return "reviewed"
    return "draft"


def _artifact_markdown_for_task(task: JsonObject) -> str:
    title = str(task.get("title", "Queue Task"))
    instructions = str(task.get("instructions", "")).strip()
    task_type = str(task.get("task_type", "synthesize"))
    scope_json = task.get("scope_json") if isinstance(task.get("scope_json"), dict) else {}
    allowed_sources = task.get("allowed_sources_json") if isinstance(task.get("allowed_sources_json"), list) else []
    return "\n".join(
        [
            f"# {title}",
            "",
            f"Task type: {task_type}",
            "",
            "## Instructions",
            instructions or "No instructions were provided.",
            "",
            "## Scope",
            str(scope_json),
            "",
            "## Allowed Sources",
            str(allowed_sources),
            "",
            "## Result",
            "This deterministic vNext artifact was generated by the local queue worker scaffold.",
            "",
        ]
    )


class VNextQueueService:
    def __init__(self, store: VNextQueueStore) -> None:
        self.store = store

    def enqueue_task(self, request: QueueTaskRequest) -> JsonObject:
        title = _normalize_required_text(request.title, field_name="title")
        task_type = _normalize_required_text(request.task_type, field_name="task_type")
        instructions = _normalize_required_text(request.instructions, field_name="instructions")
        task = self.store.create_task(
            {
                "title": title,
                "task_type": task_type,
                "instructions": instructions,
                "requested_by": request.requested_by,
                "scope_json": request.scope_json,
                "allowed_sources_json": request.allowed_sources_json,
                "domain": request.domain,
                "sensitivity": request.sensitivity,
                "write_policy": request.write_policy,
                "scheduled_for": request.scheduled_for,
                "metadata_json": {
                    **request.metadata_json,
                    "agent_identity": request.agent_identity,
                    "agent_id": request.actor_id if request.actor_type == "agent" else None,
                    "policy_decision": request.policy_decision,
                    "trace_id": request.trace_id,
                },
            },
            actor_type=request.actor_type,
        )
        append_event(
            self.store,
            event_type="queue.task_enqueued",
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            target_type="task",
            target_id=str(task["id"]),
            trace_id=request.trace_id,
            run_id=request.run_id,
            payload={"task_type": task_type, "write_policy": request.write_policy},
        )
        if request.actor_type == "agent" and request.actor_id is not None:
            append_event(
                self.store,
                event_type="agent.task_created",
                actor_type="agent",
                actor_id=request.actor_id,
                target_type="task",
                target_id=str(task["id"]),
                trace_id=request.trace_id,
                run_id=request.run_id,
                payload={"task_type": task_type, "agent_identity": request.agent_identity},
            )
        return task

    def process_next_task(self) -> QueueProcessResult:
        task = self.store.claim_next_task()
        if task is None:
            append_event(
                self.store,
                event_type="queue.worker_idle",
                actor_type="system",
                target_type="task_queue",
                payload={"status": "idle"},
            )
            return QueueProcessResult(status="idle")

        task_id = str(task["id"])
        try:
            artifact = self.store.create_artifact(
                {
                    "artifact_type": _artifact_type_for_task(str(task.get("task_type", "synthesize"))),
                    "title": str(task.get("title", "Queue Result")),
                    "content_markdown": _artifact_markdown_for_task(task),
                    "status": _artifact_status_for_policy(str(task.get("write_policy", "proposal_only"))),
                    "domain": str(task.get("domain", "unknown")),
                    "sensitivity": str(task.get("sensitivity", "unknown")),
                    "generated_by": "vnext_queue_worker",
                    "metadata_json": {
                        "task_id": task_id,
                        "task_type": task.get("task_type"),
                    },
                }
            )
            artifact_id = str(artifact["id"])
            self.store.update_task_status(
                task_id=task_id,
                status="completed",
                details={
                    "output_artifact_id": artifact_id,
                    "metadata_json": {"artifact_id": artifact_id},
                },
            )
            append_event(
                self.store,
                event_type="queue.task_completed",
                actor_type="system",
                target_type="task",
                target_id=task_id,
                payload={"artifact_id": artifact_id},
            )
            return QueueProcessResult(status="completed", task_id=task_id, artifact_id=artifact_id)
        except Exception as exc:
            logger.exception(
                "queue task processing failed task_id=%s error_code=%s",
                task_id,
                QUEUE_TASK_PROCESSING_ERROR_CODE,
            )
            self.store.update_task_status(
                task_id=task_id,
                status="failed",
                details={
                    "error_code": QUEUE_TASK_PROCESSING_ERROR_CODE,
                    "error_message": QUEUE_TASK_PROCESSING_ERROR_MESSAGE,
                },
            )
            append_event(
                self.store,
                event_type="queue.task_failed",
                actor_type="system",
                target_type="task",
                target_id=task_id,
                payload={
                    "error_code": QUEUE_TASK_PROCESSING_ERROR_CODE,
                    "error_message": QUEUE_TASK_PROCESSING_ERROR_MESSAGE,
                },
            )
            return QueueProcessResult(
                status="failed",
                task_id=task_id,
                error_code=QUEUE_TASK_PROCESSING_ERROR_CODE,
                error_message=QUEUE_TASK_PROCESSING_ERROR_MESSAGE,
            )

    def review_artifact(
        self,
        *,
        artifact_id: str,
        action: str,
        actor_type: str = "system",
        actor_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> JsonObject:
        action_to_status = {
            "review": "reviewed",
            "accept": "accepted",
            "reject": "rejected",
            "archive": "archived",
        }
        if action == "promote":
            return self._promote_artifact(
                artifact_id=artifact_id,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
                run_id=run_id,
            )
        status = action_to_status.get(action)
        if status is None:
            raise VNextQueueValidationError(
                "artifact review action must be review, accept, reject, promote, or archive"
            )
        artifact = self.store.get_artifact_for_update(artifact_id)
        if artifact is None:
            raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
        if is_project_update_artifact(artifact):
            raise VNextQueueValidationError(
                "project update artifacts must be reviewed through the coupled project-update lifecycle"
            )
        current_status = str(artifact.get("status") or "draft")
        if current_status == status:
            return artifact
        if status not in ARTIFACT_STATUS_TRANSITIONS.get(current_status, frozenset()):
            raise VNextQueueValidationError(
                f"artifact status transition {current_status!r} -> {status!r} is not allowed"
            )
        updated_artifact = self.store.update_artifact_status(
            artifact_id=artifact_id,
            status=status,
            expected_status=current_status,
            actor_type=actor_type,
        )
        if updated_artifact is None:
            raise VNextQueueValidationError("artifact review conflicted with another reviewer")
        append_event(
            self.store,
            event_type="artifact.reviewed",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="artifact",
            target_id=artifact_id,
            trace_id=trace_id,
            run_id=run_id,
            payload={"action": action, "status": status},
        )
        return updated_artifact

    def _promote_artifact(
        self,
        *,
        artifact_id: str,
        actor_type: str,
        actor_id: str | None,
        trace_id: str | None,
        run_id: str | None,
    ) -> JsonObject:
        # Promotion creates a trusted-memory side effect. Lock the artifact for
        # the duration of the caller's transaction so concurrent reviewers
        # cannot both observe the pre-promotion state and create two memories.
        artifact = self.store.get_artifact_for_update(artifact_id)
        if artifact is None:
            raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
        if is_project_update_artifact(artifact):
            raise VNextQueueValidationError(
                "project update artifacts must be reviewed through the coupled project-update lifecycle"
            )
        artifact_status = str(artifact.get("status") or "draft")
        if artifact_status == "promoted_to_memory":
            for event in self.store.list_events(target_type="artifact", target_id=artifact_id):
                payload = event.get("payload_json")
                if (
                    event.get("event_type") == "artifact.promoted_to_memory"
                    and isinstance(payload, dict)
                    and payload.get("memory_id") is not None
                ):
                    return {**artifact, "promoted_memory_id": str(payload["memory_id"])}
            raise VNextQueueValidationError(
                "artifact is marked promoted, but no persisted memory target can be verified"
            )
        if artifact_status in {"rejected", "superseded", "archived"}:
            raise VNextQueueValidationError(f"artifact in terminal status {artifact_status!r} cannot be promoted")
        if "promoted_to_memory" not in ARTIFACT_STATUS_TRANSITIONS.get(artifact_status, frozenset()):
            raise VNextQueueValidationError(
                f"artifact status transition {artifact_status!r} -> 'promoted_to_memory' is not allowed"
            )

        content = str(artifact.get("content_markdown") or "").strip()
        if not content:
            raise VNextQueueValidationError("artifact content must not be empty before promotion")
        title = str(artifact.get("title") or "Promoted artifact")
        # The credential floor, before the memory is created. Promotion makes
        # an active, human_curated, confidence 1.0 memory, and artifact text
        # is caller-influenced through queue-task instructions. Until
        # 2026-09-22 this path never consulted the floor.
        refuse_credential_material(title, content, error=VNextQueueValidationError)
        create_memory = getattr(self.store, "create_memory", None)
        if not callable(create_memory):
            raise VNextQueueValidationError(
                "artifact promotion is unavailable because this store cannot create a memory target"
            )
        scope = resource_project_scope(artifact)
        digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()[:24]
        promoted = create_memory(
            {
                "memory_key": f"artifact.promotion.{digest}",
                "value": {
                    "kind": "promoted_artifact",
                    "artifact_id": artifact_id,
                    "text": content,
                },
                "status": "active",
                "memory_type": "semantic",
                "confirmation_status": "confirmed",
                "confidence": 1.0,
                "trust_class": "human_curated",
                "promotion_eligibility": "promotable",
                "title": title,
                "canonical_text": content,
                "summary": content[:280],
                "domain": str(artifact.get("domain") or "unknown"),
                "sensitivity": str(artifact.get("sensitivity") or "unknown"),
                "project_id": scope[0] if len(scope) == 1 else None,
                "source_event_ids": [],
                "metadata_json": {
                    "source_artifact_id": artifact_id,
                    "project_scope": list(scope),
                    "promotion_reviewed": True,
                },
            },
            actor_type=actor_type,
        )
        memory_id = str(promoted.get("id") or "")
        if not memory_id:
            raise VNextQueueValidationError("artifact promotion did not return a persisted memory target")
        updated_artifact = self.store.update_artifact_status(
            artifact_id=artifact_id,
            status="promoted_to_memory",
            expected_status=artifact_status,
            actor_type=actor_type,
        )
        if updated_artifact is None:
            raise VNextQueueValidationError("artifact promotion conflicted with another reviewer")
        append_event(
            self.store,
            event_type="artifact.promoted_to_memory",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="artifact",
            target_id=artifact_id,
            trace_id=trace_id,
            run_id=run_id,
            payload={"memory_id": memory_id},
        )
        return {**updated_artifact, "promoted_memory_id": memory_id}

    def export_artifact_markdown(self, *, artifact_id: str, output_dir: str | Path) -> Path:
        artifact = self.store.get_artifact(artifact_id)
        if artifact is None:
            raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
        content = str(artifact.get("content_markdown", ""))
        requested_output_dir = str(output_dir)
        target_dir = Path(output_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        artifact_digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()[:24]
        output_path = target_dir / f"artifact-{artifact_digest}.md"
        output_path.write_text(content, encoding="utf-8")
        append_event(
            self.store,
            event_type="artifact.exported",
            actor_type="system",
            target_type="artifact",
            target_id=artifact_id,
            payload={"output_path": str(output_path), "requested_output_dir": requested_output_dir},
        )
        return output_path


__all__ = [
    "DEFAULT_VNEXT_ARTIFACT_EXPORT_ROOT",
    "ARTIFACT_STATUS_TRANSITIONS",
    "QueueProcessResult",
    "QueueTaskRequest",
    "VNextQueueNotFoundError",
    "VNextQueueService",
    "VNextQueueStore",
    "VNextQueueValidationError",
]
