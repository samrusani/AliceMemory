from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alicebot_api.vnext_scheduler import (
    DEFAULT_STALENESS_WINDOW_DAYS,
    STALENESS_REVIEW_MEMORY_TYPES,
    WORKFLOW_TYPES,
    SchedulerRunRequest,
    VNextSchedulerService,
    VNextSchedulerValidationError,
    _row_matches_projects,
    _workflow_digest,
    compute_next_run_at,
    default_schedule,
    validate_schedule,
)


def test_scheduler_project_matching_uses_canonical_scope_identity() -> None:
    row = {
        "metadata_json": {
            "project_scope": [" Beta Project ", "ALICE", "alice"],
            "agentic_memory": {"project_scope": ["stale-project"]},
        }
    }

    assert _row_matches_projects(row, (" beta   project ",)) is True
    assert _row_matches_projects(row, ("Alice",)) is True
    assert _row_matches_projects(row, ("stale-project",)) is False


def test_scheduler_logical_digest_excludes_volatile_agent_run_id() -> None:
    base = {
        "workflow": "daily_brief",
        "behavior": {
            "agent_identity": {
                "agent_id": "hermes",
                "agent_run_id": "run-a",
                "permission_profile": "trusted_local_agent",
            }
        },
    }
    replay = {
        **base,
        "behavior": {
            "agent_identity": {
                **base["behavior"]["agent_identity"],
                "agent_run_id": "run-b",
            }
        },
    }

    assert _workflow_digest(base) == _workflow_digest(replay)


class InMemorySchedulerStore:
    def __init__(self, *, fail_artifact_create: bool = False) -> None:
        self.fail_artifact_create = fail_artifact_create
        self.events: list[dict[str, object]] = []
        self.workflows: dict[str, dict[str, object]] = {}
        self.runs: dict[str, dict[str, object]] = {}
        self.artifacts: dict[str, dict[str, object]] = {}
        self.sources: list[dict[str, object]] = [
            {
                "id": "source-1",
                "source_type": "manual_text",
                "title": "Daily scheduler note",
                "domain": "project",
                "sensitivity": "private",
                "captured_at": "2026-05-10T08:00:00Z",
                "metadata_json": {"raw_text": "TODO: review scheduled daily brief"},
            }
        ]
        self.memories: list[dict[str, object]] = [
            {
                "id": "memory-1",
                "memory_type": "project_state",
                "memory_key": "project.scheduler.reviewable_artifacts",
                "canonical_text": "Scheduler generated artifacts stay reviewable.",
                "status": "active",
                "domain": "project",
                "sensitivity": "private",
            }
        ]
        self.open_loops: list[dict[str, object]] = []
        self.projects: list[dict[str, object]] = []
        self.revisions: list[dict[str, object]] = []
        self.locked_workflows: set[str] = set()

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def upsert_scheduler_workflow(self, workflow: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        workflow_type = str(workflow["workflow_type"])
        row = {
            "id": f"workflow-{workflow_type}",
            "workflow_type": workflow_type,
            "enabled": bool(workflow.get("enabled", False)),
            "paused": bool(workflow.get("paused", False)),
            "schedule_json": workflow.get("schedule_json", {"kind": "manual"}),
            "timezone": workflow.get("timezone", "UTC"),
            "next_run_at": workflow.get("next_run_at"),
            "metadata_json": workflow.get("metadata_json", {}),
            "last_run_id": None,
            "last_run_at": None,
            "last_result": None,
            "last_error": None,
        }
        self.workflows[workflow_type] = row
        self.append_event({"event_type": "scheduler.workflow_upserted", "actor_type": actor_type})
        return row

    def update_scheduler_workflow(
        self,
        *,
        workflow_type: str,
        patch: dict[str, object],
        actor_type: str = "system",
    ) -> dict[str, object]:
        row = self.workflows[workflow_type]
        row.update(
            {
                key: value
                for key, value in patch.items()
                if value is not None or key in {"last_error", "next_run_at"}
            }
        )
        self.append_event({"event_type": "scheduler.workflow_updated", "actor_type": actor_type})
        return row

    def get_scheduler_workflow(self, workflow_type: str) -> dict[str, object] | None:
        return self.workflows.get(workflow_type)

    def list_scheduler_workflows(self) -> list[dict[str, object]]:
        return list(self.workflows.values())

    def create_scheduler_run(self, run: dict[str, object], *, actor_type: str = "scheduler") -> dict[str, object]:
        row = {
            **run,
            "id": f"run-{len(self.runs) + 1}",
            "started_at": "2026-05-10T08:00:00Z",
            "finished_at": None,
            "artifact_id": None,
            "error_message": None,
        }
        self.runs[str(row["id"])] = row
        self.append_event(
            {
                "event_type": "scheduler.run_started",
                "actor_type": actor_type,
                "run_id": row["id"],
                "trace_id": row["trace_id"],
            }
        )
        return row

    def update_scheduler_run(
        self,
        *,
        run_id: str,
        patch: dict[str, object],
        actor_type: str = "scheduler",
    ) -> dict[str, object]:
        row = self.runs[run_id]
        row.update(patch)
        if row["status"] in {"succeeded", "failed"}:
            row["finished_at"] = "2026-05-10T08:01:00Z"
        event_type = "scheduler.run_succeeded" if row["status"] == "succeeded" else "scheduler.run_failed"
        self.append_event(
            {
                "event_type": event_type,
                "actor_type": actor_type,
                "run_id": row["id"],
                "trace_id": row["trace_id"],
            }
        )
        return row

    def list_scheduler_runs(self, *, workflow_type: str | None = None, limit: int = 20) -> list[dict[str, object]]:
        rows = [
            row
            for row in self.runs.values()
            if workflow_type is None or row["workflow_type"] == workflow_type
        ]
        return rows[:limit]

    def try_scheduler_workflow_lock(self, workflow_type: str) -> bool:
        return workflow_type not in self.locked_workflows

    def create_artifact(self, artifact: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        if self.fail_artifact_create:
            raise RuntimeError("artifact store unavailable")
        row = {**artifact, "id": f"artifact-{len(self.artifacts) + 1}"}
        self.artifacts[str(row["id"])] = row
        self.append_event({"event_type": "artifact.created", "actor_type": actor_type, "target_id": row["id"]})
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        """The stored report by id, as the real store reads it. A run for a caller with limits reads its report again."""

        return self.artifacts.get(str(artifact_id))

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: tuple[str, ...] | None = None,
    ) -> dict[str, object] | None:
        for row in self.artifacts.values():
            metadata = row.get("metadata_json")
            if row.get("artifact_type") != artifact_type or not isinstance(metadata, dict):
                continue
            if metadata.get("workflow") != workflow:
                continue
            if metadata.get("automation_digest") != digest and metadata.get("consolidation_digest") != digest:
                continue
            if scope_projects and not set(metadata.get("project_scope", [])) & set(scope_projects):
                continue
            return row
        return None

    def create_memory(self, memory: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories.append(row)
        self.append_event({"event_type": "memory.created", "actor_type": actor_type, "target_id": row["id"]})
        return row

    def update_memory(self, *, memory_id: str, patch: dict[str, object], actor_type: str = "system") -> dict[str, object]:
        for memory in self.memories:
            if memory["id"] == memory_id:
                memory.update(patch)
                self.append_event({"event_type": "memory.updated", "actor_type": actor_type, "target_id": memory_id})
                return memory
        raise KeyError(memory_id)

    def append_revision(self, revision: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        row = {**revision, "id": f"revision-{len(self.revisions) + 1}"}
        self.revisions.append(row)
        return row

    def list_memories(self, *, status: str | None = None) -> list[dict[str, object]]:
        return [memory for memory in self.memories if status is None or memory.get("status") == status]

    def list_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        limit: int,
    ) -> list[dict[str, object]]:
        rows = [
            memory
            for memory in self.memories
            if memory.get("status") in {"active", "accepted"}
            and (not domains or memory.get("domain") in {*domains, "unknown"})
            and memory.get("sensitivity", "unknown") in sensitivity_allowed
            and not (
                isinstance(memory.get("metadata_json"), dict)
                and memory["metadata_json"].get("candidate_kind") == excluded_candidate_kind
            )
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))), reverse=True)
        return [dict(row) for row in rows[:limit]]

    def list_pending_rollup_candidates(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_accepted_rollup_cards(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def search_sources(self, **kwargs) -> list[dict[str, object]]:
        return self.sources[: kwargs.get("limit", 8)]

    def search_memories(self, **kwargs) -> list[dict[str, object]]:
        return self.memories[: kwargs.get("limit", 8)]

    def list_open_loops(self, **kwargs) -> list[dict[str, object]]:
        rows = list(self.open_loops)
        scope_projects = tuple(kwargs.get("scope_projects") or ())
        if scope_projects:
            rows = [row for row in rows if row.get("project_id") in scope_projects]
        return rows[: kwargs.get("limit", 8)]

    def list_artifacts(self, **kwargs) -> list[dict[str, object]]:
        return list(self.artifacts.values())[: kwargs.get("limit", 8)]

    def list_projects(self, **kwargs) -> list[dict[str, object]]:
        return self.projects[: kwargs.get("limit", 8)]

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return next((project for project in self.projects if project.get("id") == project_id), None)

    def list_beliefs(self, **kwargs) -> list[dict[str, object]]:
        return [] if kwargs else []

    def list_events(self, **kwargs) -> list[dict[str, object]]:
        limit = kwargs.get("limit")
        rows = list(reversed(self.events))
        return rows[:limit] if isinstance(limit, int) else rows

    def list_artifact_quality_ratings(self, **kwargs) -> list[dict[str, object]]:
        return [
            {
                "id": "rating-1",
                "artifact_id": "artifact-seeded",
                "usefulness": 4,
                "source_grounding": 5,
                "metadata_json": {},
            }
        ][: kwargs.get("limit", 20)]


def test_schedule_validation_and_next_run_are_deterministic() -> None:
    schedule = validate_schedule(
        "daily_brief",
        {"kind": "daily", "time_of_day": "08:30", "days_of_week": ["monday", "wednesday"]},
    )
    next_run = compute_next_run_at(
        workflow_type="daily_brief",
        enabled=True,
        paused=False,
        schedule_json=schedule,
        timezone="UTC",
        now=datetime(2026, 5, 11, 8, 0, tzinfo=UTC),
    )

    assert schedule == {"kind": "daily", "time_of_day": "08:30", "days_of_week": ["monday", "wednesday"]}
    assert next_run == "2026-05-11T08:30:00+00:00"
    with pytest.raises(VNextSchedulerValidationError, match="time_of_day"):
        validate_schedule("daily_brief", {"kind": "daily", "time_of_day": "bad"})


def test_scheduler_defaults_are_disabled_and_run_now_creates_reviewable_artifact_only() -> None:
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)

    status = service.status()
    configured = service.configure_workflow(
        workflow_type="daily_brief",
        enabled=True,
        schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
        timezone="UTC",
    )
    result = service.run_now(
        SchedulerRunRequest(
            workflow_type="daily_brief",
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            generated_for="2026-05-11",
        )
    )

    assert status["disabled_by_default"] is True
    assert status["enabled_count"] == 0
    assert configured["enabled"] is True
    assert result["run"]["status"] == "succeeded"
    assert result["artifact"]["status"] == "needs_review"
    assert result["artifact"]["generated_by"] == "scheduler"
    assert result["artifact"]["metadata_json"]["scheduler_run_id"] == result["run"]["id"]
    assert [memory["status"] for memory in store.memories] == ["active"]
    assert "scheduler.run_started" in [event["event_type"] for event in store.events]
    assert "scheduler.run_succeeded" in [event["event_type"] for event in store.events]
    assert "scheduler.artifact_created" in [event["event_type"] for event in store.events]


def test_scheduler_run_due_executes_due_enabled_workflows_and_advances_next_run() -> None:
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)
    service.configure_workflow(
        workflow_type="daily_brief",
        enabled=True,
        schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
        timezone="UTC",
    )
    store.workflows["daily_brief"]["next_run_at"] = "2026-05-11T08:00:00+00:00"

    result = service.run_due_workflows(now=datetime(2026, 5, 11, 8, 5, tzinfo=UTC))

    assert result["due_count"] == 1
    assert result["runs"][0]["workflow_type"] == "daily_brief"
    assert result["runs"][0]["run"]["status"] == "succeeded"
    assert result["runs"][0]["artifact"]["status"] == "needs_review"
    assert store.workflows["daily_brief"]["next_run_at"] != "2026-05-11T08:00:00+00:00"
    assert "scheduler.due_scan" in [event["event_type"] for event in store.events]


def test_project_scoped_scheduler_reads_filter_before_workflow_limits() -> None:
    store = InMemorySchedulerStore()
    store.open_loops = [
        {
            "id": f"loop-b-{index}",
            "title": f"Project B decoy {index}",
            "status": "open",
            "domain": "project",
            "sensitivity": "private",
            "project_id": "project-b",
        }
        for index in range(25)
    ]
    store.open_loops.append(
        {
            "id": "loop-a",
            "title": "Project A target",
            "status": "open",
            "domain": "project",
            "sensitivity": "private",
            "project_id": "project-a",
        }
    )
    store.projects = [
        {
            "id": "project-b",
            "name": "Project B",
            "slug": "project-b",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        },
        {
            "id": "project-a",
            "name": "Project A",
            "slug": "project-a",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        },
    ]
    service = VNextSchedulerService(store)

    loop_result = service.run_now(
        SchedulerRunRequest(
            workflow_type="open_loop_review",
            domains=("project",),
            projects=("project-a",),
        )
    )
    project_result = service.run_now(
        SchedulerRunRequest(
            workflow_type="project_update_scan",
            domains=("project",),
            projects=("project-a",),
        )
    )

    assert loop_result["artifact"]["metadata_json"]["open_loop_ids"] == ["loop-a"]
    assert "Project B decoy" not in loop_result["artifact"]["content_markdown"]
    assert project_result["artifact"]["metadata_json"]["project_id"] == "project-a"
    assert project_result["artifact"]["metadata_json"]["project_scope"] == ["project-a"]


def test_project_scoped_staleness_sweep_never_mutates_out_of_scope_decoys() -> None:
    class ScopedStalenessStore(InMemorySchedulerStore):
        def list_memories_for_staleness_sweep(
            self,
            *,
            reference_time,
            confirmation_before,
            review_memory_types,
            limit: int,
            projects=None,
        ) -> list[dict[str, object]]:
            del reference_time, confirmation_before, review_memory_types
            allowed = set(projects or ())
            rows = [
                memory
                for memory in self.memories
                if memory.get("status") == "active"
                and (not allowed or memory.get("project_id") in allowed)
            ]
            return rows[:limit]

    store = ScopedStalenessStore()
    store.memories = [
        {
            "id": f"memory-b-{index}",
            "memory_type": "project_state",
            "status": "active",
            "project_id": "project-b",
            "valid_to": "2026-01-01T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        }
        for index in range(600)
    ]
    store.memories.append(
        {
            "id": "memory-a",
            "memory_type": "project_state",
            "status": "active",
            "project_id": "project-a",
            "valid_to": "2026-01-01T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        }
    )

    result = VNextSchedulerService(store).run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            projects=("project-a",),
            options={
                "reference_time": "2026-07-01T00:00:00Z",
                "staleness_memory_limit": 1,
            },
        )
    )

    assert result["artifact"]["metadata_json"]["stale_marked_memory_ids"] == ["memory-a"]
    assert all(memory["status"] == "active" for memory in store.memories if memory.get("project_id") == "project-b")


def test_scheduler_due_scan_can_run_model_backed_workflow_from_metadata_options() -> None:
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)
    service.configure_workflow(
        workflow_type="daily_brief",
        enabled=True,
        schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
        timezone="UTC",
        metadata_json={
            "model_options": {
                "generation_mode": "model_backed",
                "model_route_mode": "local_only",
                "model_provider": "deterministic_local",
            }
        },
    )
    store.workflows["daily_brief"]["next_run_at"] = "2026-05-11T08:00:00+00:00"

    result = service.run_due_workflows(now=datetime(2026, 5, 11, 8, 5, tzinfo=UTC))

    artifact = result["runs"][0]["artifact"]
    assert result["due_count"] == 1
    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["metadata_json"]["model_routing"]["route_mode"] == "local_only"
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert "## Source References" in artifact["content_markdown"]


def test_scheduler_run_due_skips_workflow_when_lock_is_not_acquired() -> None:
    store = InMemorySchedulerStore()
    store.locked_workflows.add("daily_brief")
    service = VNextSchedulerService(store)
    service.configure_workflow(
        workflow_type="daily_brief",
        enabled=True,
        schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
        timezone="UTC",
    )
    store.workflows["daily_brief"]["next_run_at"] = "2026-05-11T08:00:00+00:00"

    result = service.run_due_workflows(now=datetime(2026, 5, 11, 8, 5, tzinfo=UTC))

    assert result["due_count"] == 0
    assert not store.runs
    assert "scheduler.workflow_lock_skipped" in [event["event_type"] for event in store.events]


def test_scheduler_empty_due_poll_does_not_grow_append_only_event_log() -> None:
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)
    service.ensure_default_workflows()
    before = len(store.events)

    result = service.run_due_workflows(now=datetime(2026, 5, 11, 8, 5, tzinfo=UTC))

    assert result["due_count"] == 0
    assert len(store.events) == before


@pytest.mark.parametrize(
    ("workflow_type", "artifact_type"),
    [
        ("connection_report", "connection_report"),
        ("contradiction_report", "contradiction_report"),
        ("open_loop_review", "open_loop_report"),
        ("project_update_scan", "project_update"),
        ("memory_consolidation", "memory_consolidation"),
    ],
)
def test_remaining_scheduler_workflows_create_reviewable_artifacts(workflow_type: str, artifact_type: str) -> None:
    store = InMemorySchedulerStore()
    store.open_loops.append(
        {
            "id": "loop-1",
            "title": "Review scheduler output",
            "status": "open",
            "description": "Confirm non-primary workflows produce reviewable artifacts.",
            "source_id": "source-1",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    service = VNextSchedulerService(store)

    result = service.run_now(
        SchedulerRunRequest(
            workflow_type=workflow_type,
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            generated_for="2026-05-11",
        )
    )

    artifact = result["artifact"]
    metadata = artifact["metadata_json"]
    assert result["run"]["status"] == "succeeded"
    assert artifact["artifact_type"] == artifact_type
    assert artifact["status"] == "needs_review"
    assert artifact["generated_by"] == "scheduler"
    assert metadata["workflow_type"] == workflow_type
    assert metadata["scheduler_run_id"] == result["run"]["id"]
    assert metadata["trace_id"] == result["run"]["trace_id"]
    assert "source_refs" in metadata
    assert metadata["review_status"] == "needs_review"


def test_memory_consolidation_without_embeddings_is_review_only_and_creates_no_placeholder() -> None:
    # The consolidation rebuild replaced the fixed-text placeholder candidate:
    # without an embedding provider (or a vector-search-capable store) the run
    # emits a review-only report with an explicit skip reason and writes no
    # candidate memories at all.
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)

    first = service.run_now(
        SchedulerRunRequest(
            workflow_type="memory_consolidation",
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            generated_for="2026-05-11",
        )
    )
    second = service.run_now(
        SchedulerRunRequest(
            workflow_type="memory_consolidation",
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            generated_for="2026-05-11",
        )
    )

    candidates = [memory for memory in store.memories if memory.get("status") == "candidate"]
    active = [memory for memory in store.memories if memory.get("status") == "active"]
    assert first["artifact"]["artifact_type"] == "memory_consolidation"
    assert first["artifact"]["status"] == "needs_review"
    assert candidates == []
    assert first["artifact"]["metadata_json"]["candidate_memory_ids"] == []
    assert first["artifact"]["metadata_json"]["consolidation"]["skipped"]
    assert second["artifact"]["metadata_json"]["consolidation"]["skipped"]
    assert (
        second["artifact"]["metadata_json"]["consolidation_digest"]
        == first["artifact"]["metadata_json"]["consolidation_digest"]
    )
    assert [memory["id"] for memory in active] == ["memory-1"]
    assert "## Skipped / Bounds" in first["artifact"]["content_markdown"]
    assert "memory.consolidation.generated" in [event["event_type"] for event in store.events]


@pytest.mark.parametrize(
    "workflow_type",
    ["connection_report", "contradiction_report", "open_loop_review", "project_update_scan", "memory_consolidation"],
)
def test_remaining_scheduler_workflows_support_model_backed_mode(workflow_type: str) -> None:
    store = InMemorySchedulerStore()
    store.open_loops.append(
        {
            "id": "loop-1",
            "title": "Review scheduler output",
            "status": "open",
            "description": "Confirm model-backed scheduled workflows are review-only.",
            "source_id": "source-1",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    service = VNextSchedulerService(store)

    result = service.run_now(
        SchedulerRunRequest(
            workflow_type=workflow_type,
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            generated_for="2026-05-11",
            options={
                "generation_mode": "model_backed",
                "model_route_mode": "local_only",
                "model_provider": "deterministic_local",
            },
        )
    )

    artifact = result["artifact"]
    assert result["run"]["status"] == "succeeded"
    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert "## Facts" in artifact["content_markdown"]


def test_scheduler_pause_clears_stale_next_run() -> None:
    store = InMemorySchedulerStore()
    service = VNextSchedulerService(store)
    service.configure_workflow(
        workflow_type="daily_brief",
        enabled=True,
        schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
        timezone="UTC",
    )

    paused = service.configure_workflow(workflow_type="daily_brief", paused=True)

    assert paused["paused"] is True
    assert paused["next_run_at"] is None


def test_scheduler_failure_marks_run_failed_without_raising() -> None:
    store = InMemorySchedulerStore(fail_artifact_create=True)
    service = VNextSchedulerService(store)

    result = service.run_now(SchedulerRunRequest(workflow_type="project_update_scan", domains=("project",)))

    assert result["artifact"] is None
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_message"] == "Scheduler workflow execution failed"
    assert result["run"]["metadata_json"]["error_code"] == "scheduler_workflow_failed"
    assert store.workflows["project_update_scan"]["last_result"] == "failed"
    assert store.workflows["project_update_scan"]["last_error"] == "Scheduler workflow execution failed"
    assert "artifact store unavailable" not in str(result)


def test_staleness_sweep_is_a_registered_workflow_with_daily_default_schedule() -> None:
    assert "staleness_sweep" in WORKFLOW_TYPES
    assert DEFAULT_STALENESS_WINDOW_DAYS == 180
    assert STALENESS_REVIEW_MEMORY_TYPES == ("open_loop", "commitment", "project_state")

    schedule = default_schedule("staleness_sweep")
    assert schedule["kind"] == "daily"
    assert len(schedule["days_of_week"]) == 7

    normalized = validate_schedule("staleness_sweep", {"kind": "daily", "time_of_day": "04:15"})
    assert normalized["kind"] == "daily"
    assert normalized["time_of_day"] == "04:15"

    next_run = compute_next_run_at(
        workflow_type="staleness_sweep",
        enabled=True,
        paused=False,
        schedule_json=default_schedule("staleness_sweep"),
        timezone="UTC",
        now=datetime(2026, 7, 4, 1, 0, tzinfo=UTC),
    )
    assert next_run == "2026-07-04T03:30:00+00:00"


def _staleness_store() -> InMemorySchedulerStore:
    store = InMemorySchedulerStore()
    store.memories = [
        {
            "id": "memory-expired",
            "memory_type": "semantic",
            "memory_key": "vnext.capture.semantic.expired",
            "canonical_text": "Conference badge pickup closes June 1.",
            "status": "active",
            "valid_to": "2026-06-01T00:00:00Z",
            "last_confirmed_at": "2026-06-30T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"existing": True},
        },
        {
            "id": "memory-old-open-loop",
            "memory_type": "open_loop",
            "memory_key": "vnext.capture.open_loop.old",
            "canonical_text": "Follow up with the design partner.",
            "status": "active",
            "valid_to": None,
            "last_confirmed_at": "2025-11-01T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        },
        {
            "id": "memory-old-preference",
            "memory_type": "preference",
            "memory_key": "vnext.capture.preference.durable",
            "canonical_text": "Sam prefers dark roast coffee.",
            "status": "active",
            "valid_to": None,
            "last_confirmed_at": "2024-01-01T00:00:00Z",
            "domain": "personal",
            "sensitivity": "private",
        },
        {
            "id": "memory-fresh-project-state",
            "memory_type": "project_state",
            "memory_key": "vnext.capture.project_state.fresh",
            "canonical_text": "Retrieval rebuild shipped last sprint.",
            "status": "active",
            "valid_to": None,
            "last_confirmed_at": "2026-06-20T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        },
        {
            "id": "memory-expired-candidate",
            "memory_type": "semantic",
            "memory_key": "vnext.capture.semantic.candidate",
            "canonical_text": "Candidate rows stay in the review queue.",
            "status": "candidate",
            "valid_to": "2026-01-01T00:00:00Z",
            "last_confirmed_at": None,
            "domain": "project",
            "sensitivity": "private",
        },
    ]
    return store


def test_staleness_sweep_marks_expired_and_unconfirmed_working_state_memories() -> None:
    store = _staleness_store()
    service = VNextSchedulerService(store)

    result = service.run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            generated_for="2026-07-04",
            options={"reference_time": "2026-07-04T03:30:00Z"},
        )
    )

    by_id = {str(memory["id"]): memory for memory in store.memories}
    assert result["run"]["status"] == "succeeded"
    assert by_id["memory-expired"]["status"] == "stale"
    assert by_id["memory-old-open-loop"]["status"] == "stale"
    # Durable types are exempt from the confirmation-age rule.
    assert by_id["memory-old-preference"]["status"] == "active"
    # Recently confirmed working state stays active.
    assert by_id["memory-fresh-project-state"]["status"] == "active"
    # Only active rows are swept; candidates stay in the review queue.
    assert by_id["memory-expired-candidate"]["status"] == "candidate"
    # Review-first: nothing is deleted.
    assert len(store.memories) == 5
    assert by_id["memory-expired"]["metadata_json"]["existing"] is True
    assert by_id["memory-expired"]["metadata_json"]["staleness"]["reason"] == "valid_to_expired"
    assert by_id["memory-old-open-loop"]["metadata_json"]["staleness"]["reason"] == "confirmation_window_elapsed"

    artifact = result["artifact"]
    assert artifact["artifact_type"] == "system_report"
    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["stale_marked_memory_ids"] == ["memory-expired", "memory-old-open-loop"]
    assert artifact["metadata_json"]["staleness_window_days"] == 180
    assert artifact["metadata_json"]["input_counts"] == {
        "scanned": 4,
        "expired_marked": 1,
        "unconfirmed_marked": 1,
    }
    assert artifact["metadata_json"]["review_policy"] == "marks_stale_never_deletes"

    stale_events = [event for event in store.events if event.get("event_type") == "memory.stale_marked"]
    assert len(stale_events) == 2
    assert {event["target_id"] for event in stale_events} == {"memory-expired", "memory-old-open-loop"}

    sweep_revisions = [revision for revision in store.revisions if revision.get("action") == "staleness_sweep_mark"]
    assert len(sweep_revisions) == 2
    for revision in sweep_revisions:
        # REVISION_TYPES has no 'stale_marked'; the sweep notes the intent.
        assert revision["revision_type"] == "edited"
        assert revision["metadata_json"]["requested_revision_type"] == "stale_marked"
        assert revision["reason"].startswith("stale_marked:")


def test_staleness_sweep_is_idempotent_across_runs() -> None:
    store = _staleness_store()
    service = VNextSchedulerService(store)
    options = {"reference_time": "2026-07-04T03:30:00Z"}

    first = service.run_now(SchedulerRunRequest(workflow_type="staleness_sweep", options=options))
    second = service.run_now(SchedulerRunRequest(workflow_type="staleness_sweep", options=options))

    assert first["run"]["status"] == "succeeded"
    assert second["run"]["status"] == "succeeded"
    assert second["artifact"]["metadata_json"]["stale_marked_memory_ids"] == []
    assert second["artifact"]["metadata_json"]["input_counts"]["expired_marked"] == 0
    assert second["artifact"]["metadata_json"]["input_counts"]["unconfirmed_marked"] == 0
    stale_events = [event for event in store.events if event.get("event_type") == "memory.stale_marked"]
    assert len(stale_events) == 2
    assert len([revision for revision in store.revisions if revision.get("action") == "staleness_sweep_mark"]) == 2


def test_staleness_sweep_window_is_configurable_via_options() -> None:
    store = _staleness_store()
    service = VNextSchedulerService(store)

    result = service.run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            options={"reference_time": "2026-07-04T03:30:00Z", "staleness_window_days": 10},
        )
    )

    by_id = {str(memory["id"]): memory for memory in store.memories}
    # With a 10-day window even the recently confirmed project state ages out.
    assert by_id["memory-fresh-project-state"]["status"] == "stale"
    # Durable preference remains exempt regardless of the window.
    assert by_id["memory-old-preference"]["status"] == "active"
    assert result["artifact"]["metadata_json"]["staleness_window_days"] == 10


def test_staleness_sweep_skips_memories_without_freshness_signal() -> None:
    store = InMemorySchedulerStore()
    store.memories = [
        {
            "id": "memory-no-signal",
            "memory_type": "open_loop",
            "memory_key": "vnext.capture.open_loop.nosignal",
            "canonical_text": "Loop without any timestamps.",
            "status": "active",
            "valid_to": None,
            "last_confirmed_at": None,
            "domain": "project",
            "sensitivity": "private",
        }
    ]
    service = VNextSchedulerService(store)

    result = service.run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            options={"reference_time": "2026-07-04T03:30:00Z"},
        )
    )

    assert store.memories[0]["status"] == "active"
    assert result["artifact"]["metadata_json"]["stale_marked_memory_ids"] == []
