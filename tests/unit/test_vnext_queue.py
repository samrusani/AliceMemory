from __future__ import annotations

import errno
from pathlib import Path

import pytest

from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService, VNextQueueValidationError, _write_keyed_export


class InMemoryVNextQueueStore:
    def __init__(self, *, fail_artifact_create: bool = False) -> None:
        self.tasks: list[dict[str, object]] = []
        self.artifacts: dict[str, dict[str, object]] = {}
        self.memories: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []
        self.artifact_lock_calls: list[str] = []
        self.fail_artifact_create = fail_artifact_create

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, object]]:
        return [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories[str(row["id"])] = row
        return row

    def create_task(self, task: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **task,
            "id": f"task-{len(self.tasks) + 1}",
            "status": task.get("status", "pending"),
        }
        self.tasks.append(row)
        return row

    def claim_next_task(self) -> dict[str, object] | None:
        for task in self.tasks:
            if task["status"] == "pending":
                task["status"] = "running"
                return task
        return None

    def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        details: dict[str, object] | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        for task in self.tasks:
            if task["id"] != task_id:
                continue
            task["status"] = status
            if details:
                task.update(details)
            return task
        raise AssertionError(f"missing task {task_id}")

    def create_artifact(self, artifact: dict[str, object], **_kwargs) -> dict[str, object]:
        if self.fail_artifact_create:
            raise RuntimeError("artifact renderer failed")
        row = {
            **artifact,
            "id": f"artifact-{len(self.artifacts) + 1}",
        }
        self.artifacts[str(row["id"])] = row
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        self.artifact_lock_calls.append(artifact_id)
        return self.artifacts.get(artifact_id)

    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        **_kwargs,
    ) -> dict[str, object] | None:
        artifact = self.artifacts[artifact_id]
        if expected_status is not None and str(artifact.get("status") or "draft") != expected_status:
            return None
        artifact["status"] = status
        return artifact


def test_enqueue_task_creates_pending_task_and_logs_event() -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)

    task = service.enqueue_task(
        QueueTaskRequest(
            title="Synthesize Alice retrieval",
            task_type="synthesize",
            instructions="Summarize the retrieval state.",
            domain="project",
            sensitivity="private",
        )
    )

    assert task["id"] == "task-1"
    assert task["status"] == "pending"
    assert task["domain"] == "project"
    assert store.events[-1]["event_type"] == "queue.task_enqueued"
    assert store.events[-1]["target_id"] == "task-1"


def test_worker_processes_next_task_and_creates_artifact() -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    service.enqueue_task(
        QueueTaskRequest(
            title="Draft launch note",
            task_type="draft",
            instructions="Draft a launch note.",
            write_policy="auto_generate_artifact",
        )
    )

    result = service.process_next_task()

    assert result.status == "completed"
    assert result.task_id == "task-1"
    assert result.artifact_id == "artifact-1"
    assert store.tasks[0]["status"] == "completed"
    assert store.tasks[0]["output_artifact_id"] == "artifact-1"
    artifact = store.artifacts["artifact-1"]
    assert artifact["artifact_type"] == "draft"
    assert artifact["status"] == "reviewed"
    assert "# Draft launch note" in artifact["content_markdown"]
    assert store.events[-1]["event_type"] == "queue.task_completed"


def test_worker_records_failed_task_with_useful_error() -> None:
    store = InMemoryVNextQueueStore(fail_artifact_create=True)
    service = VNextQueueService(store)
    service.enqueue_task(
        QueueTaskRequest(
            title="Research failure mode",
            task_type="research",
            instructions="This should fail.",
        )
    )

    result = service.process_next_task()

    assert result.status == "failed"
    assert result.task_id == "task-1"
    assert result.error_code == "queue_task_processing_failed"
    assert result.error_message == "Queue task processing failed"
    assert store.tasks[0]["status"] == "failed"
    assert store.tasks[0]["error_code"] == "queue_task_processing_failed"
    assert store.tasks[0]["error_message"] == "Queue task processing failed"
    assert store.events[-1]["event_type"] == "queue.task_failed"
    assert store.events[-1]["payload_json"]["error_code"] == "queue_task_processing_failed"
    assert store.events[-1]["payload_json"]["error_message"] == "Queue task processing failed"
    assert "artifact renderer failed" not in str(result.to_record())
    assert "artifact renderer failed" not in str(store.events[-1]["payload_json"])


def test_artifact_review_actions_map_to_expected_statuses() -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {"id": "artifact-1", "title": "Artifact", "content_markdown": "# Artifact"}

    reviewed = service.review_artifact(
        artifact_id="artifact-1",
        action="accept",
        actor_type="agent",
        actor_id="reviewer-agent",
        trace_id="trace-review",
        run_id="run-review",
    )

    assert reviewed["status"] == "accepted"
    assert store.events[-1]["event_type"] == "artifact.reviewed"
    assert store.events[-1]["actor_type"] == "agent"
    assert store.events[-1]["actor_id"] == "reviewer-agent"
    assert store.events[-1]["trace_id"] == "trace-review"
    assert store.events[-1]["run_id"] == "run-review"
    assert store.events[-1]["payload_json"]["action"] == "accept"

    with pytest.raises(VNextQueueValidationError, match="artifact review action"):
        service.review_artifact(artifact_id="artifact-1", action="invalid")


@pytest.mark.parametrize("action", ["accept", "reject", "promote"])
def test_generic_queue_review_cannot_mutate_project_update_artifacts(action: str) -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "artifact_type": "project_update",
        "title": "Coupled project update",
        "content_markdown": "# Project update",
        "status": "needs_review",
        # Artifact type alone owns the coupled lifecycle; a damaged/missing
        # workflow marker must not turn into a generic-review side door.
        "metadata_json": {},
    }

    with pytest.raises(VNextQueueValidationError, match="coupled project-update lifecycle"):
        service.review_artifact(artifact_id="artifact-1", action=action)

    assert store.artifacts["artifact-1"]["status"] == "needs_review"
    assert store.memories == {}
    assert store.events == []


def test_artifact_promote_creates_and_returns_a_real_memory_target_idempotently() -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "title": "Release findings",
        "content_markdown": "# Release findings\n\nShip the scoped fix.",
        "status": "accepted",
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {"project_scope": ["project-a"]},
    }

    promoted = service.review_artifact(artifact_id="artifact-1", action="promote")
    replay = service.review_artifact(artifact_id="artifact-1", action="promote")

    assert promoted["status"] == "promoted_to_memory"
    assert promoted["promoted_memory_id"] == "memory-1"
    assert replay["promoted_memory_id"] == "memory-1"
    assert len(store.memories) == 1
    assert store.artifact_lock_calls == ["artifact-1", "artifact-1"]
    assert store.memories["memory-1"]["status"] == "active"
    assert store.memories["memory-1"]["metadata_json"]["project_scope"] == ["project-a"]


@pytest.mark.parametrize("action", ["reject", "archive"])
def test_promoted_artifact_cannot_be_rejected_or_archived(action: str) -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "title": "Promoted artifact",
        "content_markdown": "# Promoted artifact",
        "status": "promoted_to_memory",
    }

    with pytest.raises(VNextQueueValidationError, match="not allowed"):
        service.review_artifact(artifact_id="artifact-1", action=action)

    assert store.artifacts["artifact-1"]["status"] == "promoted_to_memory"
    assert store.artifact_lock_calls == ["artifact-1"]


@pytest.mark.parametrize("status", ["rejected", "superseded", "archived"])
def test_artifact_promote_rejects_terminal_statuses_without_creating_memory(
    status: str,
) -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "title": "Closed artifact",
        "content_markdown": "# Closed artifact",
        "status": status,
    }

    with pytest.raises(VNextQueueValidationError, match="terminal status"):
        service.review_artifact(artifact_id="artifact-1", action="promote")

    assert store.artifact_lock_calls == ["artifact-1"]
    assert store.memories == {}
    assert store.artifacts["artifact-1"]["status"] == status


def test_export_artifact_markdown_writes_file_and_logs_event(tmp_path: Path) -> None:
    store = InMemoryVNextQueueStore()
    service = VNextQueueService(store)
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "title": "Alice Queue Result",
        "content_markdown": "# Alice Queue Result\n\nDone.",
    }

    output_path = service.export_artifact_markdown(artifact_id="artifact-1", output_dir=tmp_path)

    assert output_path.parent == tmp_path.resolve()
    assert output_path.name.startswith("artifact-")
    assert output_path.suffix == ".md"
    assert output_path.read_text(encoding="utf-8") == "# Alice Queue Result\n\nDone."
    assert store.events[-1]["event_type"] == "artifact.exported"
    assert store.events[-1]["payload_json"]["output_path"] == str(output_path)


def test_keyed_export_writer_refuses_a_directory_link(tmp_path: Path) -> None:
    """Guard: the writer refuses to follow a directory link when opening the destination."""
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    link = root / "linked"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError) as refusal:
        _write_keyed_export(link / "artifact.md", "Synthetic export\n")

    assert refusal.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert list(target.iterdir()) == []


def test_enqueue_task_rejects_empty_required_fields() -> None:
    service = VNextQueueService(InMemoryVNextQueueStore())

    with pytest.raises(VNextQueueValidationError, match="instructions must not be empty"):
        service.enqueue_task(QueueTaskRequest(title="Task", task_type="draft", instructions=" "))
