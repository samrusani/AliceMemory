from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
import hashlib
from inspect import Parameter, signature
import json
import logging
from typing import Protocol, TypedDict, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_derived_labels import admit_when_locked, locked_projects, with_derived_from
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    PolicyDecision,
    resource_project_scope,
)
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_connections import ConnectionFinderRequest, VNextConnectionService
from alicebot_api.vnext_consolidation import (
    MemoryConsolidationRequest,
    VNextConsolidationService,
    VNextConsolidationStore,
)
from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_model_intelligence import (
    MODEL_ROUTE_MODES,
    ModelBackedRequest,
    ModelRoutingRequest,
    build_model_backed_artifact,
    resolve_model_route,
)
from alicebot_api.vnext_open_loop_references import sources_named_by_loops, withhold_unreadable_references
from alicebot_api.vnext_projects import (
    ProjectAutomationRequest,
    VNextProjectService,
    VNextProjectStore,
    VNextProjectValidationError,
)
from alicebot_api.vnext_project_scope import (
    normalize_project_scope,
    project_scope_identity,
    refuse_global_marker,
)
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_source_fence import SourceReadFence


WORKFLOW_TYPES = (
    "daily_brief",
    "weekly_synthesis",
    "connection_report",
    "contradiction_report",
    "open_loop_review",
    "project_update_scan",
    "memory_consolidation",
    "staleness_sweep",
)
PRIMARY_WORKFLOWS = ("daily_brief", "weekly_synthesis")
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Workflow types whose default cadence is daily.
DAILY_WORKFLOWS = ("daily_brief", "staleness_sweep")
SCHEDULER_WORKFLOW_ERROR_CODE = "scheduler_workflow_failed"
SCHEDULER_WORKFLOW_ERROR_MESSAGE = "Scheduler workflow execution failed"
logger = logging.getLogger(__name__)

DEFAULT_STALENESS_WINDOW_DAYS = 180
DEFAULT_STALENESS_MEMORY_LIMIT = 500
# Only working-state memory types decay when they go unconfirmed. Stability
# priors differ by type: durable types (preference, semantic, procedure,
# identity/relationship facts, values, ...) stay true without periodic
# re-confirmation — silence does not make "prefers dark roast" or a playbook
# less valid — while open loops, commitments, and project state describe a
# world that moves on without confirmation, so they are the only types the
# confirmation-age rule applies to. Explicit `valid_to` expiry applies to
# every type: a row that declares its own end of validity is stale once that
# time passes.
STALENESS_REVIEW_MEMORY_TYPES = ("open_loop", "commitment", "project_state")
# The sweep only inspects trusted/active rows. Candidates and review items are
# already in a review queue; superseded/rejected/archived rows are already out
# of recall. Sweeping only "active" also makes the sweep idempotent: a row
# marked "stale" is never re-scanned.
STALENESS_SWEEP_STATUSES = ("active",)


class VNextSchedulerValidationError(ValueError):
    """Raised when scheduler configuration or execution input is invalid."""


class VNextSchedulerStore(VNextProjectStore, Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def upsert_scheduler_workflow(self, workflow: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def update_scheduler_workflow(
        self,
        *,
        workflow_type: str,
        patch: JsonObject,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def get_scheduler_workflow(self, workflow_type: str) -> JsonObject | None: ...

    def list_scheduler_workflows(self) -> list[JsonObject]: ...

    def create_scheduler_run(self, run: JsonObject, *, actor_type: str = "scheduler") -> JsonObject: ...

    def update_scheduler_run(self, *, run_id: str, patch: JsonObject, actor_type: str = "scheduler") -> JsonObject: ...

    def list_scheduler_runs(self, *, workflow_type: str | None = None, limit: int = 20) -> list[JsonObject]: ...

    def try_scheduler_workflow_lock(self, workflow_type: str) -> bool: ...

    def list_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int | None = None,
    ) -> list[JsonObject]: ...

    def count_memories(
        self,
        *,
        status: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
    ) -> int: ...

    def list_memories_for_staleness_sweep(
        self,
        *,
        reference_time: datetime,
        confirmation_before: datetime,
        review_memory_types: tuple[str, ...],
        limit: int,
        projects: Sequence[str] | None = None,
    ) -> list[JsonObject]: ...

    def create_edge(self, edge: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def upsert_edge_by_idempotency_digest(
        self,
        edge: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[JsonObject]: ...

    def update_edge_status(self, *, edge_id: str, status: str, actor_type: str = "system") -> JsonObject: ...

    def list_beliefs(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[JsonObject]: ...

    def get_belief(self, belief_id: str) -> JsonObject | None: ...

    def update_belief_status(
        self,
        *,
        belief_id: str,
        status: str,
        confidence: float | None = None,
        superseded_by: str | None = None,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def list_artifact_quality_ratings(
        self,
        *,
        artifact_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonObject]: ...

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
    ) -> list[JsonObject]: ...


class _GenerationOptions(TypedDict):
    generation_mode: str
    model_route_mode: str | None
    model_provider: str | None
    model: str | None
    model_temperature: float
    allow_cloud_private: bool


@dataclass(frozen=True, slots=True)
class SchedulerRunRequest:
    workflow_type: str
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = ("public", "internal", "private", "unknown")
    generated_for: str | None = None
    triggered_by: str = "user"
    agent_identity: AgentIdentity | None = None
    policy_decision: PolicyDecision | None = None
    options: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _StagedSchedulerMutation:
    method: str
    payload: JsonObject
    actor_type: str
    options: JsonObject = field(default_factory=dict)


def _remap_staged_value(value: object, id_map: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {str(key): _remap_staged_value(child, id_map) for key, child in value.items()}
    if isinstance(value, list):
        return [_remap_staged_value(child, id_map) for child in value]
    if isinstance(value, tuple):
        return tuple(_remap_staged_value(child, id_map) for child in value)
    if isinstance(value, str):
        remapped = value
        for provisional_id, durable_id in id_map.items():
            if provisional_id != durable_id:
                remapped = remapped.replace(provisional_id, durable_id)
        return remapped
    return value


def _json_object_copy(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise VNextSchedulerValidationError("staged scheduler mutation returned a non-object row")
    return cast(JsonObject, deepcopy(value))


@dataclass(frozen=True, slots=True)
class SchedulerWorkflowPlan:
    """Side-effect-free scheduler result awaiting one atomic publication."""

    artifact: JsonObject
    mutations: tuple[_StagedSchedulerMutation, ...]

    def publish(self, store: VNextSchedulerStore) -> JsonObject:
        """Flush the plan to ``store`` in dependency order.

        Callers must hold the manual-run transaction or a validated scheduler
        claim fence for the entire call. Any exception is intentionally left
        to the surrounding transaction so every staged write rolls back.
        """

        id_map: dict[str, str] = {}
        published_rows: dict[str, JsonObject] = {}
        for mutation in self.mutations:
            payload = cast(JsonObject, _remap_staged_value(deepcopy(mutation.payload), id_map))
            options = cast(JsonObject, _remap_staged_value(deepcopy(mutation.options), id_map))
            provisional_id = str(mutation.payload.get("id") or "").strip() or None
            row = self._publish_mutation(
                store,
                method=mutation.method,
                payload=payload,
                actor_type=mutation.actor_type,
                options=options,
            )
            if provisional_id is not None and row.get("id") is not None:
                durable_id = str(row["id"])
                id_map[provisional_id] = durable_id
                published_rows[provisional_id] = row

        planned_artifact_id = str(self.artifact.get("id") or "").strip()
        if planned_artifact_id in published_rows:
            return published_rows[planned_artifact_id]
        return cast(JsonObject, _remap_staged_value(deepcopy(self.artifact), id_map))

    @staticmethod
    def _publish_mutation(
        store: VNextSchedulerStore,
        *,
        method: str,
        payload: JsonObject,
        actor_type: str,
        options: JsonObject,
    ) -> JsonObject:
        if method == "append_event":
            return store.append_event(payload)
        if method == "create_artifact":
            return store.create_artifact(payload, actor_type=actor_type)
        if method == "upsert_artifact_by_workflow_digest":
            upsert = getattr(store, method, None)
            if callable(upsert):
                return _json_object_copy(
                    upsert(
                        payload,
                        workflow=str(options["workflow"]),
                        digest=str(options["digest"]),
                        actor_type=actor_type,
                    )
                )
            return store.create_artifact(payload, actor_type=actor_type)
        if method == "create_memory":
            return store.create_memory(payload, actor_type=actor_type)
        if method == "upsert_memory_by_key":
            upsert = getattr(store, method, None)
            if callable(upsert):
                return _json_object_copy(upsert(payload, actor_type=actor_type))
            return store.create_memory(payload, actor_type=actor_type)
        if method == "create_open_loop":
            return store.create_open_loop(payload, actor_type=actor_type)
        if method == "upsert_open_loop_by_automation_digest":
            upsert = getattr(store, method, None)
            if callable(upsert):
                return _json_object_copy(upsert(payload, digest=str(options["digest"]), actor_type=actor_type))
            return store.create_open_loop(payload, actor_type=actor_type)
        if method == "create_edge":
            return store.create_edge(payload, actor_type=actor_type)
        if method == "upsert_edge_by_idempotency_digest":
            upsert = getattr(store, method, None)
            if callable(upsert):
                return _json_object_copy(
                    upsert(
                        payload,
                        digest=str(options["digest"]),
                        actor_type=actor_type,
                    )
                )
            return store.create_edge(payload, actor_type=actor_type)
        if method == "update_memory":
            return store.update_memory(
                memory_id=str(options["memory_id"]),
                patch=payload,
                actor_type=actor_type,
            )
        if method == "append_revision":
            return store.append_revision(payload, actor_type=actor_type)
        raise VNextSchedulerValidationError(f"unsupported staged scheduler mutation: {method}")


class _StagedSchedulerStore:
    """Read-through, write-buffering store used during provider work."""

    _WRITE_PREFIXES = (
        "append_",
        "create_",
        "delete_",
        "get_or_create_",
        "mark_",
        "record_",
        "remove_",
        "set_",
        "update_",
        "upsert_",
    )

    def __init__(self, delegate: VNextSchedulerStore) -> None:
        self._delegate = delegate
        self._mutations: list[_StagedSchedulerMutation] = []
        self._memory_keys: dict[tuple[str, str], JsonObject] = {}
        self._open_loop_digests: dict[tuple[str, str | None, str | None], JsonObject] = {}
        self._artifact_digests: dict[tuple[str, str, str], JsonObject] = {}
        self._edge_digests: dict[str, JsonObject] = {}

    def __getattr__(self, name: str) -> object:
        if name.startswith(self._WRITE_PREFIXES):
            raise VNextSchedulerValidationError(f"scheduler staging does not support write method {name}")
        return getattr(self._delegate, name)

    def plan(self, artifact: JsonObject) -> SchedulerWorkflowPlan:
        return SchedulerWorkflowPlan(
            artifact=deepcopy(artifact),
            mutations=tuple(deepcopy(self._mutations)),
        )

    def _stage_create(
        self,
        method: str,
        payload: JsonObject,
        *,
        actor_type: str,
        options: JsonObject | None = None,
    ) -> JsonObject:
        row = deepcopy(payload)
        row["id"] = str(row.get("id") or uuid4())
        self._mutations.append(
            _StagedSchedulerMutation(
                method=method,
                payload=deepcopy(row),
                actor_type=actor_type,
                options=deepcopy(options or {}),
            )
        )
        return row

    def append_event(self, event: JsonObject) -> JsonObject:
        row = deepcopy(event)
        row["id"] = str(row.get("id") or uuid4())
        self._mutations.append(
            _StagedSchedulerMutation(
                method="append_event",
                payload=deepcopy(row),
                actor_type=str(row.get("actor_type") or "scheduler"),
            )
        )
        return row

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
        return self._stage_create("create_artifact", artifact, actor_type=actor_type)

    def upsert_artifact_by_workflow_digest(
        self,
        artifact: JsonObject,
        *,
        workflow: str,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject:
        key = (str(artifact["artifact_type"]), workflow, digest)
        staged = self._artifact_digests.get(key)
        if staged is not None:
            return deepcopy(staged)
        finder = getattr(self._delegate, "find_artifact_by_workflow_digest", None)
        if callable(finder):
            existing = finder(
                artifact_type=key[0],
                workflow=workflow,
                digest=digest,
            )
            if isinstance(existing, dict):
                return existing
        metadata_value = artifact.get("metadata_json")
        metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        metadata.update({"workflow": workflow, "idempotency_digest": digest})
        row = self._stage_create(
            "upsert_artifact_by_workflow_digest",
            {**artifact, "metadata_json": metadata},
            actor_type=actor_type,
            options={"workflow": workflow, "digest": digest},
        )
        self._artifact_digests[key] = row
        return deepcopy(row)

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject:
        return self._stage_create("create_memory", memory, actor_type=actor_type)

    def upsert_memory_by_key(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject:
        memory_key = str(memory.get("memory_key") or "").strip()
        profile_id = str(memory.get("agent_profile_id") or "assistant_default")
        if not memory_key:
            raise VNextSchedulerValidationError("staged memory_key must not be empty")
        key = (profile_id, memory_key)
        staged = self._memory_keys.get(key)
        if staged is not None:
            return deepcopy(staged)
        getter = getattr(self._delegate, "get_memory_by_key", None)
        if callable(getter):
            existing = getter(memory_key=memory_key, agent_profile_id=profile_id)
            if isinstance(existing, dict):
                return existing
        row = self._stage_create("upsert_memory_by_key", memory, actor_type=actor_type)
        self._memory_keys[key] = row
        return deepcopy(row)

    def create_open_loop(self, loop: JsonObject, *, actor_type: str = "system") -> JsonObject:
        return self._stage_create("create_open_loop", loop, actor_type=actor_type)

    def upsert_open_loop_by_automation_digest(
        self,
        loop: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject:
        project_id = str(loop["project_id"]) if loop.get("project_id") is not None else None
        person_id = str(loop["person_id"]) if loop.get("person_id") is not None else None
        key = (digest, project_id, person_id)
        staged = self._open_loop_digests.get(key)
        if staged is not None:
            return deepcopy(staged)
        finder = getattr(self._delegate, "find_open_loop_by_automation_digest", None)
        if callable(finder):
            existing = finder(digest=digest, project_id=project_id, person_id=person_id)
            if isinstance(existing, dict):
                return existing
        metadata_value = loop.get("metadata_json")
        metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        metadata.update({"automation_digest": digest, "idempotency_digest": digest})
        row = self._stage_create(
            "upsert_open_loop_by_automation_digest",
            {**loop, "metadata_json": metadata},
            actor_type=actor_type,
            options={"digest": digest},
        )
        self._open_loop_digests[key] = row
        return deepcopy(row)

    def create_edge(self, edge: JsonObject, *, actor_type: str = "system") -> JsonObject:
        return self._stage_create("create_edge", edge, actor_type=actor_type)

    def upsert_edge_by_idempotency_digest(
        self,
        edge: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject:
        normalized_digest = str(digest).strip()
        if not normalized_digest:
            raise VNextSchedulerValidationError("staged edge digest must not be empty")
        staged = self._edge_digests.get(normalized_digest)
        if staged is not None:
            return deepcopy(staged)
        finder = getattr(self._delegate, "find_edge_by_idempotency_digest", None)
        if callable(finder):
            existing = finder(digest=normalized_digest)
            if isinstance(existing, dict):
                return existing
        metadata_value = edge.get("metadata_json")
        metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        metadata["idempotency_digest"] = normalized_digest
        row = self._stage_create(
            "upsert_edge_by_idempotency_digest",
            {**edge, "metadata_json": metadata},
            actor_type=actor_type,
            options={"digest": normalized_digest},
        )
        self._edge_digests[normalized_digest] = row
        return deepcopy(row)

    def update_memory(
        self,
        *,
        memory_id: str,
        patch: JsonObject,
        actor_type: str = "system",
    ) -> JsonObject:
        getter = getattr(self._delegate, "get_memory", None)
        current = getter(memory_id) if callable(getter) else None
        if not isinstance(current, dict):
            memories = self._delegate.list_memories()
            current = next((row for row in memories if str(row.get("id")) == memory_id), None)
        if not isinstance(current, dict):
            raise VNextSchedulerValidationError("staged memory update target does not exist")
        row = {**deepcopy(current), **deepcopy(patch)}
        self._mutations.append(
            _StagedSchedulerMutation(
                method="update_memory",
                payload=deepcopy(patch),
                actor_type=actor_type,
                options={"memory_id": memory_id},
            )
        )
        return row

    def append_revision(self, revision: JsonObject, *, actor_type: str = "system") -> JsonObject:
        return self._stage_create("append_revision", revision, actor_type=actor_type)


def _normalized_project_scope(values: tuple[str, ...]) -> tuple[str, ...]:
    # Explicit project names only: every scheduler request takes its projects
    # through here, and the reserved global marker would be read as a name no
    # row holds and hide every global memory.
    refuse_global_marker(values, where="scheduler")
    return normalize_project_scope(values)


def _row_matches_projects(
    row: JsonObject,
    projects: tuple[str, ...],
    *,
    project_row: bool = False,
) -> bool:
    if not projects:
        return True
    row_scope = list(resource_project_scope(row))
    if project_row and row.get("id") is not None:
        row_scope.append(str(row["id"]))
    allowed = set(project_scope_identity(projects))
    return bool(allowed.intersection(project_scope_identity(row_scope)))


def _supports_explicit_parameter(method: object, name: str) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )


def _workflow_digest(payload: object) -> str:
    if isinstance(payload, dict):
        payload = {key: _digest_stable_value(value) for key, value in payload.items() if key != "agent_run_id"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _digest_stable_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _digest_stable_value(child) for key, child in value.items() if key != "agent_run_id"}
    if isinstance(value, list):
        return [_digest_stable_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_digest_stable_value(child) for child in value)
    return value


def _logical_agent_identity(identity: AgentIdentity | None) -> JsonObject | None:
    if identity is None:
        return None
    return cast(JsonObject, _digest_stable_value(identity.to_record()))


def default_schedule(workflow_type: str) -> JsonObject:
    if workflow_type == "daily_brief":
        return {"kind": "daily", "time_of_day": "08:00", "days_of_week": list(DAY_NAMES)}
    if workflow_type == "weekly_synthesis":
        return {"kind": "weekly", "day_of_week": "monday", "time_of_day": "09:00"}
    if workflow_type == "staleness_sweep":
        return {"kind": "daily", "time_of_day": "03:30", "days_of_week": list(DAY_NAMES)}
    return {"kind": "manual"}


def _parse_time_of_day(value: object) -> time:
    if not isinstance(value, str):
        raise VNextSchedulerValidationError("time_of_day must be HH:MM")
    try:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError) as exc:
        raise VNextSchedulerValidationError("time_of_day must be HH:MM") from exc


def _day_index(value: object) -> int:
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    if isinstance(value, str):
        normalized = value.casefold().strip()
        if normalized in DAY_NAMES:
            return DAY_NAMES.index(normalized)
    raise VNextSchedulerValidationError("day_of_week must be a weekday name or integer 0-6")


def _option_int(options: JsonObject, key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _option_bool(options: JsonObject, key: str, default: bool) -> bool:
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def validate_schedule(workflow_type: str, schedule_json: JsonObject) -> JsonObject:
    if workflow_type not in WORKFLOW_TYPES:
        raise VNextSchedulerValidationError(f"workflow_type must be one of {', '.join(WORKFLOW_TYPES)}")
    if not isinstance(schedule_json, dict):
        raise VNextSchedulerValidationError("schedule_json must be an object")

    kind = schedule_json.get("kind") or (
        "daily" if workflow_type in DAILY_WORKFLOWS else "weekly" if workflow_type == "weekly_synthesis" else "manual"
    )
    if kind == "manual":
        return {"kind": "manual"}
    if workflow_type in DAILY_WORKFLOWS:
        default_time = "08:00" if workflow_type == "daily_brief" else "03:30"
        when = _parse_time_of_day(schedule_json.get("time_of_day", default_time))
        days = schedule_json.get("days_of_week", list(DAY_NAMES))
        if not isinstance(days, list) or not days:
            raise VNextSchedulerValidationError("days_of_week must be a non-empty list")
        day_values = sorted(dict.fromkeys(_day_index(day) for day in days))
        return {
            "kind": "daily",
            "time_of_day": f"{when.hour:02d}:{when.minute:02d}",
            "days_of_week": [DAY_NAMES[index] for index in day_values],
        }
    if workflow_type == "weekly_synthesis":
        when = _parse_time_of_day(schedule_json.get("time_of_day", "09:00"))
        day = DAY_NAMES[_day_index(schedule_json.get("day_of_week", "monday"))]
        return {"kind": "weekly", "day_of_week": day, "time_of_day": f"{when.hour:02d}:{when.minute:02d}"}
    return {"kind": "manual"}


def compute_next_run_at(
    *,
    workflow_type: str,
    enabled: bool,
    paused: bool,
    schedule_json: JsonObject,
    timezone: str,
    now: datetime | None = None,
) -> str | None:
    if not enabled or paused:
        return None
    schedule = validate_schedule(workflow_type, schedule_json)
    if schedule.get("kind") == "manual":
        return None
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise VNextSchedulerValidationError("timezone must be a valid IANA timezone") from exc
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    run_time = _parse_time_of_day(schedule.get("time_of_day"))
    if schedule["kind"] == "daily":
        days_of_week = schedule.get("days_of_week")
        if not isinstance(days_of_week, list):
            raise VNextSchedulerValidationError("daily schedules must include days_of_week")
        allowed_days = {_day_index(day) for day in days_of_week}
    else:
        allowed_days = {_day_index(schedule["day_of_week"])}
    for offset in range(8):
        candidate_date = (local_now + timedelta(days=offset)).date()
        if candidate_date.weekday() not in allowed_days:
            continue
        candidate = datetime.combine(candidate_date, run_time, tzinfo=zone)
        if candidate > local_now:
            return candidate.astimezone(UTC).isoformat()
    raise VNextSchedulerValidationError("could not compute next scheduler run")


def _memory_timestamp(value: object) -> datetime | None:
    """Parse a memory row timestamp leniently; unparseable values become None."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VNextSchedulerValidationError("next_run_at must be an ISO datetime") from exc
    else:
        raise VNextSchedulerValidationError("next_run_at must be an ISO datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class VNextSchedulerService:
    def __init__(self, store: VNextSchedulerStore) -> None:
        self.store = store

    def ensure_default_workflows(self) -> list[JsonObject]:
        existing = {str(row["workflow_type"]): row for row in self.store.list_scheduler_workflows()}
        workflows: list[JsonObject] = []
        for workflow_type in WORKFLOW_TYPES:
            if workflow_type in existing:
                workflows.append(existing[workflow_type])
                continue
            workflows.append(
                self.store.upsert_scheduler_workflow(
                    {
                        "workflow_type": workflow_type,
                        "enabled": False,
                        "paused": False,
                        "schedule_json": default_schedule(workflow_type),
                        "timezone": "UTC",
                        "next_run_at": None,
                        "metadata_json": {"created_by": "vnext_scheduler_defaults"},
                    }
                )
            )
        return workflows

    def status(self) -> JsonObject:
        workflows = self.ensure_default_workflows()
        runs = self.store.list_scheduler_runs(limit=100)
        recent_events = self.store.list_events(limit=100)
        due_scan_events = [event for event in recent_events if event.get("event_type") == "scheduler.due_scan"]
        failures = [run for run in runs if run.get("status") == "failed"]
        successes = [run for run in runs if run.get("status") == "succeeded"]
        running = next((run for run in runs if run.get("status") == "started"), None)
        next_due_workflow = min(
            (
                workflow
                for workflow in workflows
                if workflow.get("enabled") is True
                and workflow.get("paused") is not True
                and workflow.get("next_run_at") is not None
            ),
            key=lambda workflow: str(workflow.get("next_run_at")),
            default=None,
        )
        return {
            "mode": "local_governed",
            "disabled_by_default": True,
            "workflows": workflows,
            "recent_runs": runs[:20],
            "enabled_count": sum(1 for row in workflows if row.get("enabled") is True),
            "paused_count": sum(1 for row in workflows if row.get("paused") is True),
            "last_failure": failures[0] if failures else None,
            "recent_failures": failures[:10],
            "last_due_scan": due_scan_events[0] if due_scan_events else None,
            "next_due_workflow": next_due_workflow,
            "currently_running_workflow": running,
            "last_success_by_workflow": {
                workflow_type: next((run for run in successes if run.get("workflow_type") == workflow_type), None)
                for workflow_type in WORKFLOW_TYPES
            },
        }

    def configure_workflow(
        self,
        *,
        workflow_type: str,
        enabled: bool | None = None,
        paused: bool | None = None,
        schedule_json: JsonObject | None = None,
        timezone: str | None = None,
        metadata_json: JsonObject | None = None,
        actor_type: str = "user",
    ) -> JsonObject:
        current = self._ensure_workflow(workflow_type)
        next_enabled = bool(current.get("enabled")) if enabled is None else enabled
        next_paused = bool(current.get("paused")) if paused is None else paused
        configured_schedule = schedule_json or current.get("schedule_json")
        schedule_input = (
            configured_schedule if isinstance(configured_schedule, dict) else default_schedule(workflow_type)
        )
        next_schedule = validate_schedule(workflow_type, schedule_input)
        next_timezone = timezone or str(current.get("timezone") or "UTC")
        next_run_at = compute_next_run_at(
            workflow_type=workflow_type,
            enabled=next_enabled,
            paused=next_paused,
            schedule_json=next_schedule,
            timezone=next_timezone,
        )
        row = self.store.update_scheduler_workflow(
            workflow_type=workflow_type,
            patch={
                "enabled": next_enabled,
                "paused": next_paused,
                "schedule_json": next_schedule,
                "timezone": next_timezone,
                "next_run_at": next_run_at,
                "last_error": None,
                **({"metadata_json": metadata_json} if metadata_json is not None else {}),
            },
            actor_type=actor_type,
        )
        event_type = "scheduler.workflow_enabled" if next_enabled else "scheduler.workflow_disabled"
        if paused is True:
            event_type = "scheduler.workflow_paused"
        elif paused is False:
            event_type = "scheduler.workflow_resumed"
        append_event(
            self.store,
            event_type=event_type,
            actor_type=actor_type,
            target_type="scheduler_workflow",
            target_id=str(row["id"]),
            payload={"workflow_type": workflow_type, "enabled": next_enabled, "paused": next_paused},
        )
        return row

    def pause_all(self, *, actor_type: str = "user") -> JsonObject:
        workflows = [
            self.configure_workflow(workflow_type=workflow_type, paused=True, actor_type=actor_type)
            for workflow_type in WORKFLOW_TYPES
        ]
        return {"workflows": workflows, "paused_count": len(workflows)}

    def resume_all(self, *, actor_type: str = "user") -> JsonObject:
        workflows = [
            self.configure_workflow(workflow_type=workflow_type, paused=False, actor_type=actor_type)
            for workflow_type in WORKFLOW_TYPES
        ]
        return {"workflows": workflows, "resumed_count": len(workflows)}

    def run_due_workflows(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
        triggered_by: str = "scheduler",
        agent_identity: AgentIdentity | None = None,
        policy_decision: PolicyDecision | None = None,
    ) -> JsonObject:
        if limit < 1:
            raise VNextSchedulerValidationError("limit must be at least 1")
        checked_at = _coerce_datetime(now or datetime.now(UTC)) or datetime.now(UTC)
        due_runs: list[JsonObject] = []
        for workflow in self.ensure_default_workflows():
            if len(due_runs) >= limit:
                break
            if workflow.get("enabled") is not True or workflow.get("paused") is True:
                continue
            next_run_at = _coerce_datetime(workflow.get("next_run_at"))
            if next_run_at is None or next_run_at > checked_at:
                continue
            workflow_type = str(workflow["workflow_type"])
            if not self.store.try_scheduler_workflow_lock(workflow_type):
                append_event(
                    self.store,
                    event_type="scheduler.workflow_lock_skipped",
                    actor_type=triggered_by,
                    target_type="scheduler_workflow",
                    target_id=str(workflow.get("id")) if workflow.get("id") is not None else None,
                    payload={"workflow_type": workflow_type, "scheduled_for": next_run_at.isoformat()},
                )
                continue
            workflow_metadata_value = workflow.get("metadata_json")
            workflow_metadata: JsonObject = workflow_metadata_value if isinstance(workflow_metadata_value, dict) else {}
            workflow_options_value = workflow_metadata.get("model_options")
            workflow_options: JsonObject = workflow_options_value if isinstance(workflow_options_value, dict) else {}
            result = self.run_now(
                SchedulerRunRequest(
                    workflow_type=workflow_type,
                    domains=(policy_decision.effective_domains if policy_decision is not None else ()),
                    projects=(
                        policy_decision.effective_project_scope
                        if policy_decision is not None
                        else agent_identity.project_scope
                        if agent_identity is not None
                        else ()
                    ),
                    sensitivity_allowed=(
                        policy_decision.effective_sensitivity_allowed
                        if policy_decision is not None
                        else ("public", "internal", "private", "unknown")
                    ),
                    generated_for=self._generated_for(workflow, checked_at),
                    triggered_by=triggered_by,
                    agent_identity=agent_identity,
                    policy_decision=policy_decision,
                    options={
                        **workflow_options,
                        "scheduled_for": next_run_at.isoformat(),
                        "due_scan_checked_at": checked_at.isoformat(),
                    },
                )
            )
            due_runs.append(
                {
                    "workflow_type": workflow_type,
                    "scheduled_for": next_run_at.isoformat(),
                    "run": result["run"],
                    "artifact": result["artifact"],
                }
            )
        # Empty polling is routine daemon behavior, not a durable domain
        # event.  Persisting one row every minute grew the append-only log by
        # 525,600 rows per user/year.  Actual runs and lock skips already emit
        # their own auditable events; the daemon status file carries the most
        # recent empty-poll heartbeat.
        if due_runs:
            append_event(
                self.store,
                event_type="scheduler.due_scan",
                actor_type=triggered_by,
                payload={"checked_at": checked_at.isoformat(), "due_count": len(due_runs), "limit": limit},
            )
        return {"checked_at": checked_at.isoformat(), "due_count": len(due_runs), "runs": due_runs}

    def run_now(self, request: SchedulerRunRequest) -> JsonObject:
        started = self.begin_run(request)
        workflow = cast(JsonObject, started["workflow"])
        run = cast(JsonObject, started["run"])
        try:
            plan = self.prepare_started_workflow(request, run=run)
            return self.publish_started_workflow(
                request,
                workflow=workflow,
                run=run,
                plan=plan,
            )
        except Exception as exc:
            return self.fail_started_workflow(
                request,
                workflow=workflow,
                run=run,
                error=exc,
            )

    def begin_run(self, request: SchedulerRunRequest) -> JsonObject:
        """Persist only the scheduler run envelope before external work."""

        if request.workflow_type not in WORKFLOW_TYPES:
            raise VNextSchedulerValidationError(f"workflow_type must be one of {', '.join(WORKFLOW_TYPES)}")
        projects = _normalized_project_scope(request.projects)
        if len(projects) > 50:
            raise VNextSchedulerValidationError("projects must contain at most 50 values")
        workflow = self._ensure_workflow(request.workflow_type)
        trace_id = str(uuid4())
        actor_type = request.triggered_by
        run = self.store.create_scheduler_run(
            {
                "workflow_id": workflow.get("id"),
                "workflow_type": request.workflow_type,
                "status": "started",
                "triggered_by": request.triggered_by,
                "trace_id": trace_id,
                "policy_decision_json": request.policy_decision.to_record() if request.policy_decision else {},
                "agent_identity_json": request.agent_identity.to_record() if request.agent_identity else {},
                "metadata_json": {
                    "manual_run": request.triggered_by != "scheduler",
                    "options": request.options,
                    "generation_mode": self._generation_mode(request),
                    "project_scope": list(projects),
                },
            },
            actor_type=actor_type,
        )
        return {"workflow": workflow, "run": run}

    def prepare_started_workflow(
        self,
        request: SchedulerRunRequest,
        *,
        run: JsonObject,
    ) -> SchedulerWorkflowPlan:
        """Run reads/provider work while buffering every domain mutation."""

        run_id = str(run.get("id") or "").strip()
        trace_id = str(run.get("trace_id") or "").strip()
        if not run_id or not trace_id or run.get("status") != "started":
            raise VNextSchedulerValidationError("scheduler run is malformed or no longer active")
        staged_store = _StagedSchedulerStore(self.store)
        artifact = VNextSchedulerService(cast(VNextSchedulerStore, staged_store))._run_workflow(
            request,
            scheduler_run_id=run_id,
            trace_id=trace_id,
        )
        return staged_store.plan(artifact)

    def publish_started_workflow(
        self,
        request: SchedulerRunRequest,
        *,
        workflow: JsonObject,
        run: JsonObject,
        plan: SchedulerWorkflowPlan,
    ) -> JsonObject:
        """Atomically publish a prepared workflow in the caller transaction."""

        run_id = str(run["id"])
        trace_id = str(run["trace_id"])
        actor_type = request.triggered_by
        run_metadata_value = run.get("metadata_json")
        run_metadata: JsonObject = dict(run_metadata_value) if isinstance(run_metadata_value, dict) else {}
        artifact = plan.publish(self.store)
        artifact_id = str(artifact["id"])
        updated_run = self.store.update_scheduler_run(
            run_id=run_id,
            patch={
                "status": "succeeded",
                "artifact_id": artifact_id,
                "metadata_json": {**run_metadata, "artifact_id": artifact_id},
            },
            actor_type=actor_type,
        )
        self.store.update_scheduler_workflow(
            workflow_type=request.workflow_type,
            patch={
                "last_run_id": run_id,
                "last_run_at": updated_run.get("finished_at"),
                "last_result": "succeeded",
                "last_error": None,
                "next_run_at": self._next_run_after(workflow),
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type="scheduler.artifact_created",
            actor_type=actor_type,
            target_type="artifact",
            target_id=artifact_id,
            trace_id=trace_id,
            run_id=run_id,
            payload={"workflow_type": request.workflow_type, "scheduler_run_id": run_id},
        )
        return {"run": updated_run, "artifact": artifact}

    def fail_started_workflow(
        self,
        request: SchedulerRunRequest,
        *,
        workflow: JsonObject,
        run: JsonObject,
        error: Exception,
    ) -> JsonObject:
        """Record failure after a staged plan was discarded or rolled back."""

        run_id = str(run["id"])
        logger.error(
            "scheduler workflow failed workflow_type=%s run_id=%s error_code=%s",
            request.workflow_type,
            run_id,
            SCHEDULER_WORKFLOW_ERROR_CODE,
            exc_info=(type(error), error, error.__traceback__),
        )
        run_metadata_value = run.get("metadata_json")
        run_metadata: JsonObject = dict(run_metadata_value) if isinstance(run_metadata_value, dict) else {}
        updated_run = self.store.update_scheduler_run(
            run_id=run_id,
            patch={
                "status": "failed",
                "error_message": SCHEDULER_WORKFLOW_ERROR_MESSAGE,
                "metadata_json": {**run_metadata, "error_code": SCHEDULER_WORKFLOW_ERROR_CODE},
            },
            actor_type=request.triggered_by,
        )
        self.store.update_scheduler_workflow(
            workflow_type=request.workflow_type,
            patch={
                "last_run_id": run_id,
                "last_run_at": updated_run.get("finished_at"),
                "last_result": "failed",
                "last_error": SCHEDULER_WORKFLOW_ERROR_MESSAGE,
                "next_run_at": self._next_run_after(workflow),
            },
            actor_type=request.triggered_by,
        )
        return {"run": updated_run, "artifact": None}

    def execute_claimed_workflow(
        self,
        request: SchedulerRunRequest,
        *,
        run: JsonObject,
    ) -> SchedulerWorkflowPlan:
        """Execute one durably claimed workflow without changing claim state.

        The runtime invokes this through a dedicated autocommit connection, so
        provider latency never holds a database transaction. The returned plan
        has no durable domain side effects; only the later fenced finalize may
        publish it as the scheduler run outcome.
        """

        if request.triggered_by != "scheduler":
            raise VNextSchedulerValidationError("claimed workflows must be scheduler-triggered")
        run_id = str(run.get("id") or "").strip()
        trace_id = str(run.get("trace_id") or "").strip()
        if not run_id or not trace_id or run.get("status") != "started":
            raise VNextSchedulerValidationError("scheduler claim run is malformed or no longer active")
        return self.prepare_started_workflow(request, run=run)

    def claimed_request(
        self,
        *,
        workflow: JsonObject,
        checked_at: datetime,
        scheduled_for: datetime,
        agent_identity: AgentIdentity | None = None,
        policy_decision: PolicyDecision | None = None,
    ) -> SchedulerRunRequest:
        workflow_metadata_value = workflow.get("metadata_json")
        workflow_metadata = workflow_metadata_value if isinstance(workflow_metadata_value, dict) else {}
        model_options_value = workflow_metadata.get("model_options")
        model_options = model_options_value if isinstance(model_options_value, dict) else {}
        return SchedulerRunRequest(
            workflow_type=str(workflow["workflow_type"]),
            domains=policy_decision.effective_domains if policy_decision is not None else (),
            projects=(
                policy_decision.effective_project_scope
                if policy_decision is not None
                else agent_identity.project_scope
                if agent_identity is not None
                else ()
            ),
            sensitivity_allowed=(
                policy_decision.effective_sensitivity_allowed
                if policy_decision is not None
                else ("public", "internal", "private", "unknown")
            ),
            generated_for=self._generated_for(workflow, checked_at),
            triggered_by="scheduler",
            agent_identity=agent_identity,
            policy_decision=policy_decision,
            options={
                **model_options,
                "scheduled_for": scheduled_for.isoformat(),
                "due_scan_checked_at": checked_at.isoformat(),
            },
        )

    def next_run_after_workflow(self, workflow: JsonObject) -> str | None:
        return self._next_run_after(workflow)

    def _ensure_workflow(self, workflow_type: str) -> JsonObject:
        if workflow_type not in WORKFLOW_TYPES:
            raise VNextSchedulerValidationError(f"workflow_type must be one of {', '.join(WORKFLOW_TYPES)}")
        workflow = self.store.get_scheduler_workflow(workflow_type)
        if workflow is not None:
            return workflow
        return self.store.upsert_scheduler_workflow(
            {
                "workflow_type": workflow_type,
                "enabled": False,
                "paused": False,
                "schedule_json": default_schedule(workflow_type),
                "timezone": "UTC",
                "next_run_at": None,
                "metadata_json": {"created_by": "vnext_scheduler_defaults"},
            }
        )

    def _run_workflow(self, request: SchedulerRunRequest, *, scheduler_run_id: str, trace_id: str) -> JsonObject:
        generation_kwargs = self._generation_kwargs(request)
        projects = _normalized_project_scope(request.projects)
        metadata: JsonObject = {
            "generated_by": "scheduler",
            "workflow": request.workflow_type,
            "workflow_type": request.workflow_type,
            "scheduler_run_id": scheduler_run_id,
            "trace_id": trace_id,
            "policy_decision": request.policy_decision.to_record() if request.policy_decision else None,
            "agent_identity": request.agent_identity.to_record() if request.agent_identity else None,
            "review_status": "needs_review",
            "generation_mode": generation_kwargs["generation_mode"],
            "project_scope": list(projects),
        }
        brain_request = BrainArtifactRequest(
            domains=request.domains,
            projects=projects,
            sensitivity_allowed=request.sensitivity_allowed,
            generated_for=request.generated_for,
            discover_open_loops=_option_bool(request.options, "discover_open_loops", False),
            create_candidate_memories=_option_bool(request.options, "create_candidate_memories", False),
            generated_by="scheduler",
            trace_id=trace_id,
            run_id=scheduler_run_id,
            agent_identity=_logical_agent_identity(request.agent_identity),
            metadata_json=metadata,
            **generation_kwargs,
        )
        brain = VNextBrainService(self.store)
        if request.workflow_type == "daily_brief":
            return brain.generate_daily_brief(brain_request)
        if request.workflow_type == "weekly_synthesis":
            return brain.generate_weekly_synthesis(brain_request)
        if request.workflow_type == "connection_report":
            return VNextConnectionService(self.store).generate_connection_report(
                ConnectionFinderRequest(
                    domains=request.domains,
                    projects=projects,
                    sensitivity_allowed=request.sensitivity_allowed,
                    generated_by="scheduler",
                    trace_id=trace_id,
                    run_id=scheduler_run_id,
                    agent_identity=_logical_agent_identity(request.agent_identity),
                    policy_decision=request.policy_decision.to_record() if request.policy_decision else None,
                    metadata_json=metadata,
                    **generation_kwargs,
                )
            )
        if request.workflow_type == "contradiction_report":
            return VNextContradictionService(self.store).generate_contradiction_report(
                ContradictionFinderRequest(
                    domains=request.domains,
                    projects=projects,
                    sensitivity_allowed=request.sensitivity_allowed,
                    generated_by="scheduler",
                    trace_id=trace_id,
                    run_id=scheduler_run_id,
                    agent_identity=_logical_agent_identity(request.agent_identity),
                    policy_decision=request.policy_decision.to_record() if request.policy_decision else None,
                    metadata_json=metadata,
                    **generation_kwargs,
                )
            )
        if request.workflow_type == "staleness_sweep":
            return self._run_staleness_sweep(request, metadata=metadata)
        if request.workflow_type == "open_loop_review":
            return self._generate_open_loop_review_artifact(request, metadata=metadata)
        if request.workflow_type == "project_update_scan":
            return self._generate_project_update_scan_artifact(request, metadata=metadata)
        if request.workflow_type == "memory_consolidation":
            return VNextConsolidationService(cast(VNextConsolidationStore, self.store)).generate_memory_consolidation(
                MemoryConsolidationRequest(
                    domains=request.domains,
                    projects=projects,
                    sensitivity_allowed=request.sensitivity_allowed,
                    generated_for=request.generated_for,
                    source_limit=_option_int(request.options, "source_limit", 12),
                    memory_limit=_option_int(request.options, "memory_limit", 12),
                    artifact_limit=_option_int(request.options, "artifact_limit", 8),
                    event_limit=_option_int(request.options, "event_limit", 30),
                    rating_limit=_option_int(request.options, "rating_limit", 20),
                    create_candidate_memories=_option_bool(request.options, "create_candidate_memories", True),
                    generated_by="scheduler",
                    trace_id=trace_id,
                    run_id=scheduler_run_id,
                    agent_identity=_logical_agent_identity(request.agent_identity),
                    policy_decision=request.policy_decision.to_record() if request.policy_decision else None,
                    metadata_json=metadata,
                    **self._generation_kwargs(request),
                )
            )
        artifact_type = "system_report"
        return self.store.create_artifact(
            {
                "artifact_type": artifact_type,
                "title": f"{request.workflow_type.replace('_', ' ').title()} - {datetime.now(UTC).date().isoformat()}",
                "content_markdown": "\n".join(
                    [
                        f"# {request.workflow_type.replace('_', ' ').title()}",
                        "",
                        "This governed scheduler workflow is configured but has only a deterministic local scaffold in this sprint.",
                    ]
                ),
                "status": "needs_review",
                "domain": request.domains[0] if len(request.domains) == 1 else "unknown",
                "sensitivity": "unknown",
                "generated_by": "scheduler",
                "metadata_json": metadata,
            },
            actor_type="scheduler",
        )

    def _run_staleness_sweep(self, request: SchedulerRunRequest, *, metadata: JsonObject) -> JsonObject:
        """Mark expired or long-unconfirmed memories as stale.

        Review-first semantics: the sweep only transitions ``status`` to
        ``stale`` — it never deletes, archives, or supersedes anything. Stale
        rows stay fully auditable and can be re-confirmed or archived through
        the normal review paths. The sweep is idempotent because it only
        scans ``active`` rows, so a row marked stale is never re-marked.
        """
        now = _memory_timestamp(request.options.get("reference_time")) or datetime.now(UTC)
        window_days = _option_int(request.options, "staleness_window_days", DEFAULT_STALENESS_WINDOW_DAYS)
        if window_days < 1:
            window_days = DEFAULT_STALENESS_WINDOW_DAYS
        mark_limit = _option_int(request.options, "staleness_memory_limit", DEFAULT_STALENESS_MEMORY_LIMIT)
        window_start = now - timedelta(days=window_days)
        projects = _normalized_project_scope(request.projects)

        scanned_count = 0
        expired_marked: list[JsonObject] = []
        unconfirmed_marked: list[JsonObject] = []
        targeted_reader = getattr(self.store, "list_memories_for_staleness_sweep", None)
        if callable(targeted_reader):
            if projects and not _supports_explicit_parameter(targeted_reader, "projects"):
                raise VNextSchedulerValidationError(
                    "project-scoped staleness sweep requires a store reader with explicit projects support"
                )
            memories = targeted_reader(
                reference_time=now,
                confirmation_before=window_start,
                review_memory_types=STALENESS_REVIEW_MEMORY_TYPES,
                limit=mark_limit,
                projects=projects or None,
            )
        else:
            if projects:
                raise VNextSchedulerValidationError("project-scoped staleness sweep requires the targeted store reader")
            # Compatibility for third-party/fake stores. Bundled PostgreSQL
            # and SQLite stores always take the indexed targeted path above.
            memories = [
                memory for status in STALENESS_SWEEP_STATUSES for memory in self.store.list_memories(status=status)
            ]
        out_of_scope = [memory for memory in memories if not _row_matches_projects(memory, projects)]
        if out_of_scope:
            raise VNextSchedulerValidationError(
                "staleness sweep store returned memories outside the requested project scope"
            )
        bound = locked_projects(
            request.agent_identity.to_record() if request.agent_identity is not None else None,
            projects,
        )
        memories = admit_when_locked("memory", memories, bound)
        from alicebot_api.vnext_label_guard import admit_loaded

        memories = admit_loaded(
            self.store,
            kind="memory",
            rows=memories,
            domains=list(request.domains) if request.domains else None,
            sensitivity_allowed=list(request.sensitivity_allowed),
            projects=projects,
            all_of=bound,
        )
        for memory in memories:
            if len(expired_marked) + len(unconfirmed_marked) >= mark_limit:
                break
            scanned_count += 1
            valid_to = _memory_timestamp(memory.get("valid_to"))
            if valid_to is not None and valid_to < now:
                expired_marked.append(
                    self._mark_memory_stale(
                        memory,
                        reason="valid_to_expired",
                        note=f"valid_to {valid_to.isoformat()} passed before {now.isoformat()}",
                        metadata=metadata,
                    )
                )
                continue
            if str(memory.get("memory_type")) not in STALENESS_REVIEW_MEMORY_TYPES:
                continue
            confirmed_at = (
                _memory_timestamp(memory.get("last_confirmed_at"))
                or _memory_timestamp(memory.get("last_seen_at"))
                or _memory_timestamp(memory.get("created_at"))
            )
            if confirmed_at is not None and confirmed_at < window_start:
                unconfirmed_marked.append(
                    self._mark_memory_stale(
                        memory,
                        reason="confirmation_window_elapsed",
                        note=(
                            f"last confirmation {confirmed_at.isoformat()} is older than "
                            f"{window_days} days for working-state type {memory.get('memory_type')}"
                        ),
                        metadata=metadata,
                    )
                )

        marked = [*expired_marked, *unconfirmed_marked]
        generated_for = request.generated_for or now.date().isoformat()
        marked_lines = [
            f"- memory:{row.get('id')} ({row.get('memory_type')}) - {str(row.get('title') or row.get('canonical_text') or 'untitled')[:120]}"
            for row in marked
        ] or ["- No memories crossed an expiry or confirmation-age threshold."]
        content = "\n".join(
            [
                f"# Staleness Sweep - {generated_for}",
                "",
                "## Summary",
                f"- Scanned active memories: {scanned_count}",
                f"- Marked stale (valid_to expired): {len(expired_marked)}",
                f"- Marked stale (unconfirmed working-state, window {window_days} days): {len(unconfirmed_marked)}",
                "",
                "## Marked Memories",
                *marked_lines,
                "",
                "## Review Policy",
                "- The sweep marks memories stale; it never deletes, archives, or supersedes them.",
                "- Durable types (preference, semantic, procedure, facts) are exempt from the confirmation-age rule.",
                "- Stale memories stay auditable and can be re-confirmed or archived through review.",
            ]
        )
        return self.store.create_artifact(
            {
                "artifact_type": "system_report",
                "title": f"Staleness Sweep - {generated_for}",
                "content_markdown": content,
                "status": "needs_review",
                "domain": derived_domain(marked, fallback=request.domains[0] if len(request.domains) == 1 else "unknown"),
                "sensitivity": self._highest_sensitivity(marked),
                "generated_by": "scheduler",
                "metadata_json": with_derived_from(
                    {
                        **metadata,
                        "workflow": "staleness_sweep",
                        "source_refs": [],
                        "stale_marked_memory_ids": [str(row.get("id")) for row in marked],
                        "staleness_window_days": window_days,
                        "input_counts": {
                            "scanned": scanned_count,
                            "expired_marked": len(expired_marked),
                            "unconfirmed_marked": len(unconfirmed_marked),
                        },
                        "review_policy": "marks_stale_never_deletes",
                    },
                    {"memories": marked},
                ),
            },
            actor_type="scheduler",
        )

    def _mark_memory_stale(
        self,
        memory: JsonObject,
        *,
        reason: str,
        note: str,
        metadata: JsonObject,
    ) -> JsonObject:
        memory_metadata_value = memory.get("metadata_json")
        memory_metadata: JsonObject = dict(memory_metadata_value) if isinstance(memory_metadata_value, dict) else {}
        memory_metadata["staleness"] = {
            "marked_by": "staleness_sweep",
            "reason": reason,
            "note": note,
            "scheduler_run_id": metadata.get("scheduler_run_id"),
            "trace_id": metadata.get("trace_id"),
        }
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch={"status": "stale", "metadata_json": memory_metadata},
            actor_type="scheduler",
        )
        # The revision-type vocabulary (REVISION_TYPES, enforced by the
        # Postgres memory_revisions_revision_type_check constraint) has no
        # "stale_marked" value, so the sweep records revision_type="edited"
        # and carries the intended type in the reason/metadata note.
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated.get("memory_key") or ""),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "edited",
                "action": "staleness_sweep_mark",
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": f"stale_marked: {note}",
                "actor_type": "scheduler",
                "actor_id": None,
                "metadata_json": {
                    "requested_revision_type": "stale_marked",
                    "staleness_reason": reason,
                    "workflow_type": "staleness_sweep",
                    "scheduler_run_id": metadata.get("scheduler_run_id"),
                },
            },
            actor_type="scheduler",
        )
        append_event(
            self.store,
            event_type="memory.stale_marked",
            actor_type="scheduler",
            target_type="memory",
            target_id=str(updated["id"]),
            trace_id=str(metadata.get("trace_id")) if metadata.get("trace_id") is not None else None,
            run_id=str(metadata.get("scheduler_run_id")) if metadata.get("scheduler_run_id") is not None else None,
            payload={
                "reason": reason,
                "note": note,
                "memory_type": memory.get("memory_type"),
                "previous_status": memory.get("status"),
            },
        )
        return updated

    def _generate_open_loop_review_artifact(self, request: SchedulerRunRequest, *, metadata: JsonObject) -> JsonObject:
        domains = list(request.domains) if request.domains else None
        projects = _normalized_project_scope(request.projects)
        loops = self.store.list_open_loops(
            status="open",
            domains=domains,
            sensitivity_allowed=list(request.sensitivity_allowed),
            limit=20,
            scope_projects=projects,
        )
        if any(not _row_matches_projects(loop, projects) for loop in loops):
            raise VNextSchedulerValidationError("open-loop store returned rows outside the requested project scope")
        bound = locked_projects(
            request.agent_identity.to_record() if request.agent_identity is not None else None,
            projects,
        )
        loops = admit_when_locked("open_loop", loops, bound)
        from alicebot_api.vnext_label_guard import admit_loaded

        loops = admit_loaded(
            self.store,
            kind="open_loop",
            rows=loops,
            domains=domains,
            sensitivity_allowed=list(request.sensitivity_allowed),
            projects=projects,
            all_of=bound,
        )
        # The report copies the id of each loop's source into its text and its ``source_refs``, and a later reader of
        # the artifact is shown them, so a source the run's own identity may not read is left out.
        loops = withhold_unreadable_references(
            self.store, loops, fence=SourceReadFence.for_identity(request.agent_identity)
        )
        # The ids that are left are printed, and the artifact is read behind the label it is stored with. A source can
        # be stricter than the loop that links it (it was reclassified, or the loop was made by a key that could read
        # it), so the label is taken over those sources as well as the loops.
        linked_sources = sources_named_by_loops(self.store, loops)
        labelled_rows = [*loops, *linked_sources]
        generation_kwargs = self._generation_kwargs(request)
        digest_inputs: JsonObject = {
            "workflow": "open_loop_review",
            "generated_for": request.generated_for,
            "scope": {
                "domains": request.domains,
                "projects": projects,
                "sensitivity_allowed": request.sensitivity_allowed,
            },
            "behavior": {
                "generation": generation_kwargs,
                "agent_identity": request.agent_identity.to_record() if request.agent_identity else None,
                "brain_charter": self._brain_charter(),
            },
            "open_loops": loops,
        }
        if linked_sources:
            # Part of the identity of the run, so a source that was reclassified since the last report makes a new one
            # instead of handing back the report that carries the earlier label. A run that links no source has the
            # digest it always had.
            digest_inputs["linked_sources"] = [
                {"id": str(row.get("id")), "domain": row.get("domain"), "sensitivity": row.get("sensitivity")}
                for row in linked_sources
            ]
        workflow_digest = _workflow_digest(digest_inputs)
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing = find_existing(
                artifact_type="open_loop_report",
                workflow="open_loop_review",
                digest=workflow_digest,
                scope_projects=projects or None,
            )
            if existing is not None:
                return existing
        source_refs = [f"source:{loop.get('source_id')}" for loop in loops if loop.get("source_id") is not None]
        loop_lines = [
            "\n".join(
                [
                    f"### {index}. {loop.get('title', 'Open loop')}",
                    f"- Open loop: open_loop:{loop.get('id')}",
                    f"- Priority: {loop.get('priority', 'normal')}",
                    f"- Due: {loop.get('due_at') or 'not set'}",
                    f"- Source: source:{loop.get('source_id')}"
                    if loop.get("source_id") is not None
                    else "- Source: not linked",
                    f"- Description: {loop.get('description') or 'No description recorded.'}",
                ]
            )
            for index, loop in enumerate(loops, start=1)
        ] or ["- No open loops matched this scheduler scope."]
        content = "\n\n".join(
            [
                f"# Open Loop Review - {request.generated_for or datetime.now(UTC).date().isoformat()}",
                "## Open Items",
                *loop_lines,
                "## Review Policy",
                "- This artifact is review-only and does not close, snooze, or promote any open loop automatically.",
            ]
        )
        enriched_metadata = {
            **metadata,
            "workflow": "open_loop_review",
            "open_loop_ids": [str(loop.get("id")) for loop in loops if loop.get("id") is not None],
            "source_refs": source_refs,
            "input_counts": {"open_loops": len(loops)},
            "workflow_digest": workflow_digest,
        }
        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if self._generation_mode(request) == "model_backed":
            route = resolve_model_route(
                ModelRoutingRequest(
                    workflow_type="open_loop_review",
                    generation_mode="model_backed",
                    domains=request.domains,
                    sensitivity_allowed=request.sensitivity_allowed,
                    agent_identity=request.agent_identity.to_record() if request.agent_identity else None,
                    brain_charter=self._brain_charter(),
                    requested_route_mode=generation_kwargs["model_route_mode"],
                    requested_provider=generation_kwargs["model_provider"],
                    requested_model=generation_kwargs["model"],
                    allow_cloud_private=generation_kwargs["allow_cloud_private"],
                )
            )
            model_artifact = build_model_backed_artifact(
                ModelBackedRequest(
                    workflow_type="open_loop_review",
                    title=f"Open Loop Review - {request.generated_for or datetime.now(UTC).date().isoformat()}",
                    deterministic_markdown=content,
                    context_rows=tuple(loops),
                    source_refs=tuple(source_refs),
                    open_questions=("Which open loop should be closed, snoozed, edited, or escalated first?",),
                    trace_id=str(metadata.get("trace_id")) if metadata.get("trace_id") is not None else None,
                    route=route,
                    temperature=generation_kwargs["model_temperature"],
                    config={"generated_by": "scheduler"},
                )
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            enriched_metadata = {**enriched_metadata, **model_artifact.metadata}
        enriched_metadata = with_derived_from(
            enriched_metadata,
            {"open_loops": loops, "sources": linked_sources},
        )
        artifact_payload: JsonObject = {
            "artifact_type": "open_loop_report",
            "title": f"Open Loop Review - {request.generated_for or datetime.now(UTC).date().isoformat()}",
            "content_markdown": content,
            "status": "needs_review",
            "domain": derived_domain(
                labelled_rows, fallback=request.domains[0] if len(request.domains) == 1 else "unknown"
            ),
            "sensitivity": self._highest_sensitivity(labelled_rows),
            "generated_by": "scheduler",
            "prompt_hash": prompt_hash,
            "model_info_json": model_info_json,
            "metadata_json": enriched_metadata,
        }
        upsert_artifact = getattr(self.store, "upsert_artifact_by_workflow_digest", None)
        if callable(upsert_artifact):
            return upsert_artifact(
                artifact_payload,
                workflow="open_loop_review",
                digest=workflow_digest,
                actor_type="scheduler",
            )
        return self.store.create_artifact(artifact_payload, actor_type="scheduler")

    def _generate_project_update_scan_artifact(
        self, request: SchedulerRunRequest, *, metadata: JsonObject
    ) -> JsonObject:
        requested_projects = _normalized_project_scope(request.projects)
        if requested_projects:
            projects = []
            allowed_domains = set(request.domains)
            allowed_sensitivity = set(request.sensitivity_allowed)
            for project_id in requested_projects:
                project = self.store.get_project(project_id)
                if project is None or project.get("status") != "active":
                    continue
                if not _row_matches_projects(project, requested_projects, project_row=True):
                    continue
                project_domain = str(project.get("domain") or "unknown")
                if allowed_domains and project_domain not in allowed_domains and project_domain != "unknown":
                    continue
                if str(project.get("sensitivity") or "unknown") not in allowed_sensitivity:
                    continue
                projects.append(project)
                break
        else:
            projects = self.store.list_projects(
                status="active",
                domains=list(request.domains) if request.domains else None,
                sensitivity_allowed=list(request.sensitivity_allowed),
                limit=1,
            )
        if projects:
            try:
                policy_decision_value = metadata.get("policy_decision")
                return VNextProjectService(self.store).generate_project_update_candidate(
                    ProjectAutomationRequest(
                        agent_identity=_logical_agent_identity(request.agent_identity),
                        domains=request.domains,
                        sensitivity_allowed=request.sensitivity_allowed,
                        project_id=str(projects[0]["id"]),
                        generated_by="scheduler",
                        trace_id=str(metadata.get("trace_id")) if metadata.get("trace_id") is not None else None,
                        run_id=str(metadata.get("scheduler_run_id"))
                        if metadata.get("scheduler_run_id") is not None
                        else None,
                        policy_decision=policy_decision_value if isinstance(policy_decision_value, dict) else None,
                        metadata_json={
                            **metadata,
                            "workflow_type": "project_update_scan",
                            "review_status": "needs_review",
                        },
                        **self._generation_kwargs(request),
                    )
                )
            except VNextProjectValidationError:
                pass
        content = "\n".join(
            [
                f"# Project Update Scan - {request.generated_for or datetime.now(UTC).date().isoformat()}",
                "",
                "No active project matched this scheduler scope.",
                "",
                "This review artifact records the scan without creating or promoting trusted memory.",
            ]
        )
        enriched_metadata = {
            **metadata,
            "workflow": "project_update_scan",
            "source_refs": [],
            "project_ids": [],
            "input_counts": {"projects": 0},
        }
        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if self._generation_mode(request) == "model_backed":
            generation_kwargs = self._generation_kwargs(request)
            route = resolve_model_route(
                ModelRoutingRequest(
                    workflow_type="project_update_scan",
                    generation_mode="model_backed",
                    domains=request.domains,
                    sensitivity_allowed=request.sensitivity_allowed,
                    agent_identity=request.agent_identity.to_record() if request.agent_identity else None,
                    brain_charter=self._brain_charter(),
                    requested_route_mode=generation_kwargs["model_route_mode"],
                    requested_provider=generation_kwargs["model_provider"],
                    requested_model=generation_kwargs["model"],
                    allow_cloud_private=generation_kwargs["allow_cloud_private"],
                )
            )
            model_artifact = build_model_backed_artifact(
                ModelBackedRequest(
                    workflow_type="project_update_scan",
                    title=f"Project Update Scan - {request.generated_for or datetime.now(UTC).date().isoformat()}",
                    deterministic_markdown=content,
                    context_rows=(),
                    source_refs=(),
                    open_questions=("Which project scope should be checked next?",),
                    trace_id=str(metadata.get("trace_id")) if metadata.get("trace_id") is not None else None,
                    route=route,
                    temperature=generation_kwargs["model_temperature"],
                    config={"generated_by": "scheduler"},
                )
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            enriched_metadata = {**enriched_metadata, **model_artifact.metadata}
        return self.store.create_artifact(
            {
                "artifact_type": "project_update",
                "title": f"Project Update Scan - {request.generated_for or datetime.now(UTC).date().isoformat()}",
                "content_markdown": content,
                "status": "needs_review",
                "domain": request.domains[0] if len(request.domains) == 1 else "project",
                "sensitivity": "unknown",
                "generated_by": "scheduler",
                "prompt_hash": prompt_hash,
                "model_info_json": model_info_json,
                "metadata_json": enriched_metadata,
            },
            actor_type="scheduler",
        )

    def _generation_mode(self, request: SchedulerRunRequest) -> str:
        value = request.options.get("generation_mode")
        return value if value in {"deterministic", "model_backed"} else "deterministic"

    def _generation_kwargs(self, request: SchedulerRunRequest) -> _GenerationOptions:
        options = request.options
        route_mode = options.get("model_route_mode")
        temperature_value = options.get("model_temperature")
        temperature = (
            float(temperature_value)
            if isinstance(temperature_value, (int, float)) and not isinstance(temperature_value, bool)
            else 0.2
        )
        if temperature < 0.0 or temperature > 2.0:
            temperature = 0.2
        return {
            "generation_mode": self._generation_mode(request),
            "model_route_mode": route_mode if isinstance(route_mode, str) and route_mode in MODEL_ROUTE_MODES else None,
            "model_provider": provider if isinstance((provider := options.get("model_provider")), str) else None,
            "model": model if isinstance((model := options.get("model")), str) else None,
            "model_temperature": temperature,
            "allow_cloud_private": bool(options.get("allow_cloud_private"))
            if isinstance(options.get("allow_cloud_private"), bool)
            else False,
        }

    def _brain_charter(self) -> JsonObject | None:
        getter = getattr(self.store, "get_brain_charter", None)
        if not callable(getter):
            return None
        charter = getter()
        return charter if isinstance(charter, dict) else None

    def _next_run_after(self, workflow: JsonObject) -> str | None:
        workflow_type = str(workflow["workflow_type"])
        schedule_value = workflow.get("schedule_json")
        schedule_json = schedule_value if isinstance(schedule_value, dict) else default_schedule(workflow_type)
        return compute_next_run_at(
            workflow_type=workflow_type,
            enabled=bool(workflow.get("enabled")),
            paused=bool(workflow.get("paused")),
            schedule_json=schedule_json,
            timezone=str(workflow.get("timezone") or "UTC"),
        )

    def _generated_for(self, workflow: JsonObject, checked_at: datetime) -> str:
        zone: tzinfo
        try:
            zone = ZoneInfo(str(workflow.get("timezone") or "UTC"))
        except ZoneInfoNotFoundError:
            zone = UTC
        return checked_at.astimezone(zone).date().isoformat()

    @staticmethod
    def _highest_sensitivity(rows: list[JsonObject]) -> str:
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


__all__ = [
    "DEFAULT_STALENESS_MEMORY_LIMIT",
    "DEFAULT_STALENESS_WINDOW_DAYS",
    "PRIMARY_WORKFLOWS",
    "STALENESS_REVIEW_MEMORY_TYPES",
    "STALENESS_SWEEP_STATUSES",
    "SchedulerRunRequest",
    "SchedulerWorkflowPlan",
    "VNextSchedulerService",
    "VNextSchedulerStore",
    "VNextSchedulerValidationError",
    "WORKFLOW_TYPES",
    "compute_next_run_at",
    "default_schedule",
    "validate_schedule",
]
