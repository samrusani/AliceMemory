from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from alicebot_api.credential_floor import refuse_credential_activation
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_model_intelligence import (
    ModelBackedRequest,
    ModelRoutingRequest,
    build_model_backed_artifact,
    resolve_model_route,
)
from alicebot_api.vnext_project_update_guard import (
    PROJECT_UPDATE_WORKFLOW,
    is_project_update_artifact,
)
from alicebot_api.vnext_project_scope import project_scope_identity, source_project_scope
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_store import (
    REDACTED_JSON_VALUE,
    REDACTION_MARKER,
    PostgresVNextStore,
    is_redacted_project_update_artifact,
)


DEFAULT_PROJECT_LIMIT = 8
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
PROJECT_UPDATE_ACTIONS = {"accept", "edit", "reject"}
OPEN_LOOP_ACTIONS = {"close", "snooze", "edit", "reopen"}

PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE = (
    "project update terminal state is inconsistent with its durable review outcome. "
    "Do not retry or infer the intended decision; inspect the artifact, linked candidate memory, "
    "project, and memory revisions, then repair them from durable audit evidence before retrying."
)

_SUPERSEDED_AGENTIC_LIFECYCLE_STATUSES = frozenset(
    {
        "review_superseded",
        "superseded_by_consolidation",
    }
)


class VNextProjectValidationError(ValueError):
    """Raised when a vNext project or open-loop operation is invalid."""


class VNextProjectTerminalConsistencyError(VNextProjectValidationError):
    """Raised when a terminal project-update artifact has no matching durable outcome."""


class VNextProjectStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def list_project_update_events(
        self,
        *,
        artifact_id: str,
        candidate_memory_id: str,
    ) -> list[JsonObject]: ...

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def get_artifact(self, artifact_id: str) -> JsonObject | None: ...

    def get_artifact_for_update(self, artifact_id: str) -> JsonObject | None: ...

    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        metadata_json: JsonObject | None = None,
        actor_type: str = "system",
    ) -> JsonObject | None: ...

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def update_memory(self, *, memory_id: str, patch: JsonObject, actor_type: str = "system") -> JsonObject: ...

    def get_memory_for_update(self, memory_id: str) -> JsonObject | None: ...

    def append_revision(self, revision: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def list_revisions(self, memory_id: str) -> list[JsonObject]: ...

    def get_project(self, project_id: str) -> JsonObject | None: ...

    def get_project_for_update(self, project_id: str) -> JsonObject | None: ...

    def list_projects(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_PROJECT_LIMIT,
    ) -> list[JsonObject]: ...

    def update_project(self, *, project_id: str, patch: JsonObject, actor_type: str = "system") -> JsonObject: ...

    def create_open_loop(self, loop: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def get_open_loop(self, loop_id: str) -> JsonObject | None: ...

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        project_id: str | None = None,
        person_id: str | None = None,
        limit: int = DEFAULT_PROJECT_LIMIT,
        scope_projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def update_open_loop(self, *, loop_id: str, patch: JsonObject, actor_type: str = "system") -> JsonObject: ...

    def update_open_loop_status(
        self,
        *,
        loop_id: str,
        status: str,
        resolution_note: str | None = None,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_PROJECT_LIMIT,
        scope_projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_PROJECT_LIMIT,
        projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_PROJECT_LIMIT,
        scope_projects: tuple[str, ...] = (),
    ) -> list[JsonObject]: ...

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: Sequence[str] | None = None,
    ) -> JsonObject | None: ...

    def find_open_loop_by_automation_digest(
        self,
        *,
        digest: str,
        project_id: str | None = None,
        person_id: str | None = None,
    ) -> JsonObject | None: ...

    def upsert_artifact_by_workflow_digest(
        self,
        artifact: JsonObject,
        *,
        workflow: str,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def upsert_open_loop_by_automation_digest(
        self,
        loop: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def upsert_memory_by_key(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ProjectAutomationRequest:
    domains: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    project_id: str | None = None
    person_id: str | None = None
    max_items: int = DEFAULT_PROJECT_LIMIT
    generated_by: str = "system"
    actor_id: str | None = None
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


def _validate_request(request: ProjectAutomationRequest) -> None:
    if request.max_items < 1 or request.max_items > 50:
        raise VNextProjectValidationError("max_items must be between 1 and 50")
    if not request.sensitivity_allowed:
        raise VNextProjectValidationError("sensitivity_allowed must not be empty")
    if request.generation_mode not in {"deterministic", "model_backed"}:
        raise VNextProjectValidationError("generation_mode must be deterministic or model_backed")
    if request.model_temperature < 0.0 or request.model_temperature > 2.0:
        raise VNextProjectValidationError("model_temperature must be between 0.0 and 2.0")


def _text(row: JsonObject) -> str:
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        raw_text = metadata.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            return raw_text
    for key in ("title", "canonical_text", "summary", "current_state", "description", "name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return str(row.get("id", "item"))


def _title(row: JsonObject) -> str:
    for key in ("title", "name", "canonical_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return str(row.get("id", "item"))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "project"


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


def _source_ids(rows: list[JsonObject]) -> list[str]:
    return [str(row.get("id")) for row in rows if row.get("id") is not None]


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_in_project(row: JsonObject, project_id: str) -> bool:
    requested = project_scope_identity((project_id,))
    return bool(requested and requested[0] in project_scope_identity(resource_project_scope(row)))


def _is_source_in_project(row: JsonObject, project_id: str) -> bool:
    requested = project_scope_identity((project_id,))
    return bool(requested and requested[0] in project_scope_identity(source_project_scope(row)))


def _project_candidate_supersession_marker(memory: Mapping[str, object]) -> str | None:
    """Return a persisted marker proving this candidate was superseded."""

    if memory.get("superseded_by") is not None:
        return "superseded_by"
    metadata = memory.get("metadata_json")
    if not isinstance(metadata, Mapping):
        return None
    # This is the supported pre-pointer-column representation and remains a
    # compatibility copy on every current supersession write.
    if metadata.get("superseded_by") is not None:
        return "metadata_json.superseded_by"
    agentic = metadata.get("agentic_memory")
    if not isinstance(agentic, Mapping):
        return None
    # These exact values are persisted by dashboard and consolidation
    # supersession paths. Lifecycle history is append-only audit state, so a
    # superseded entry remains contradictory even if a raw write overwrote the
    # current lifecycle marker.
    lifecycle_status = str(agentic.get("lifecycle_status") or "").strip().casefold()
    if lifecycle_status in _SUPERSEDED_AGENTIC_LIFECYCLE_STATUSES:
        return f"metadata_json.agentic_memory.lifecycle_status={lifecycle_status}"
    lifecycle_history = agentic.get("lifecycle_history")
    if isinstance(lifecycle_history, list):
        for entry in lifecycle_history:
            if not isinstance(entry, Mapping):
                continue
            historical_status = str(entry.get("status") or "").strip().casefold()
            if historical_status in _SUPERSEDED_AGENTIC_LIFECYCLE_STATUSES:
                return f"metadata_json.agentic_memory.lifecycle_history={historical_status}"
    return None


def _project_automation_digest(
    *,
    project: JsonObject,
    sources: list[JsonObject],
    memories: list[JsonObject],
    request: ProjectAutomationRequest,
    brain_charter: JsonObject | None,
) -> str:
    return _digest_payload(
        {
            "project": {
                key: project.get(key)
                for key in ("id", "name", "slug", "current_state", "domain", "sensitivity", "status")
            },
            "sources": [
                {
                    "id": row.get("id"),
                    "text": _text(row),
                    "domain": row.get("domain"),
                    "sensitivity": row.get("sensitivity"),
                    "project_scope": project_scope_identity(source_project_scope(row)),
                }
                for row in sources
            ],
            "memories": [
                {
                    "id": row.get("id"),
                    "text": _text(row),
                    "status": row.get("status"),
                    "domain": row.get("domain"),
                    "sensitivity": row.get("sensitivity"),
                    "project_scope": project_scope_identity(resource_project_scope(row)),
                }
                for row in memories
            ],
            "behavior": {
                "domains": request.domains,
                "sensitivity_allowed": request.sensitivity_allowed,
                "max_items": request.max_items,
                "generated_by": request.generated_by,
                "agent_identity": request.agent_identity,
                "generation_mode": request.generation_mode,
                "model_route_mode": request.model_route_mode,
                "model_provider": request.model_provider,
                "model": request.model,
                "model_temperature": request.model_temperature,
                "allow_cloud_private": request.allow_cloud_private,
                "brain_charter": brain_charter,
            },
        }
    )


def _open_loop_digest(candidate: JsonObject, *, project_id: str | None, person_id: str | None) -> str:
    metadata = candidate.get("metadata_json")
    loop_type = metadata.get("loop_type") if isinstance(metadata, dict) else None
    return _digest_payload(
        {
            "source_id": candidate.get("source_id"),
            "loop_type": loop_type,
            "title": candidate.get("title"),
            "project_id": project_id,
            "person_id": person_id,
        }
    )


def _brain_charter(store: VNextProjectStore) -> JsonObject | None:
    getter = getattr(store, "get_brain_charter", None)
    if not callable(getter):
        return None
    charter = getter()
    return charter if isinstance(charter, dict) else None


def _detect_project_change(project: JsonObject, sources: list[JsonObject], memories: list[JsonObject]) -> str:
    name = _title(project)
    for row in [*sources, *memories]:
        text = _text(row)
        for line in text.splitlines() or [text]:
            normalized = " ".join(line.split())
            if not normalized:
                continue
            lowered = normalized.casefold()
            if name.casefold() in lowered or str(project.get("slug", "")).casefold() in lowered:
                return normalized[:500]
            if lowered.startswith(("decision:", "change:", "project:", "now:")):
                return normalized[:500]
    return f"{name} has new evidence that should be reviewed before updating project state."


def _open_loop_candidates(source: JsonObject) -> list[JsonObject]:
    candidates: list[JsonObject] = []
    patterns = (
        ("task", re.compile(r"^\s*(?:todo|task)\s*:?\s*(.+)$", re.IGNORECASE)),
        ("follow_up", re.compile(r"^\s*follow up\s*:?\s*(.+)$", re.IGNORECASE)),
        ("waiting_on_person", re.compile(r"^\s*waiting (?:on|for)\s*:?\s*(.+)$", re.IGNORECASE)),
        ("question", re.compile(r"^\s*(?:question|ask)\s*:?\s*(.+)$", re.IGNORECASE)),
        ("decision_needed", re.compile(r"^\s*decision(?: needed)?\s*:?\s*(.+)$", re.IGNORECASE)),
        ("research_gap", re.compile(r"^\s*research\s*:?\s*(.+)$", re.IGNORECASE)),
        ("project_blocker", re.compile(r"^\s*block(?:ed|er)?\s*:?\s*(.+)$", re.IGNORECASE)),
    )
    for line in _text(source).splitlines():
        for loop_type, pattern in patterns:
            match = pattern.match(line)
            if match is None:
                continue
            title = " ".join(match.group(1).split())
            if not title:
                continue
            owner_match = re.search(r"(?:owner|by)\s*:\s*([A-Za-z0-9 _.-]+)", title, re.IGNORECASE)
            owner = " ".join(owner_match.group(1).split()) if owner_match else None
            candidates.append(
                {
                    "title": title[:240],
                    "description": f"Candidate {loop_type} discovered from source {_title(source)}.",
                    "priority": "high" if loop_type == "project_blocker" else "normal",
                    "source_id": source.get("id"),
                    "domain": source.get("domain", "unknown"),
                    "sensitivity": source.get("sensitivity", "unknown"),
                    "metadata_json": {
                        "candidate": True,
                        "loop_type": loop_type,
                        "owner": owner,
                        "source_captured_at": source.get("captured_at"),
                        "discovered_by": "vnext_project_automation",
                    },
                }
            )
    return candidates


def _project_update_markdown(
    *,
    project: JsonObject,
    change: str,
    suggested_current_state: str,
    sources: list[JsonObject],
    memories: list[JsonObject],
) -> str:
    source_lines = [f"- source:{source.get('id')} {_title(source)}" for source in sources] or ["- No sources selected."]
    memory_lines = [f"- memory:{memory.get('id')} {_title(memory)}" for memory in memories] or [
        "- No memories selected."
    ]
    return "\n".join(
        [
            f"# Project Update Candidate - {_title(project)}",
            "",
            f"Project: {_title(project)}",
            f"Change Detected: {change}",
            "Why It Matters: This may update the project's current state, but it requires review before promotion.",
            "Suggested Updates:",
            "- current_state",
            "",
            f"Suggested Current State: {suggested_current_state}",
            "",
            "Sources:",
            *source_lines,
            "Memories:",
            *memory_lines,
            "Confidence: 0.72",
            "Actions: Accept / Edit / Reject",
            "",
        ]
    )


class VNextProjectService:
    def __init__(self, store: VNextProjectStore, *, defer_embeddings: bool = False) -> None:
        self.store = store
        self._defer_embeddings = defer_embeddings
        self._deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []

    @property
    def deferred_embedding_inputs(self) -> tuple[DeferredMemoryEmbedding, ...]:
        """Immutable embedding snapshots collected for post-commit work."""

        return tuple(self._deferred_embedding_inputs)

    @staticmethod
    def is_project_update_candidate(artifact: JsonObject) -> bool:
        """Return whether this artifact belongs to the coupled project-update workflow."""

        return is_project_update_artifact(artifact)

    def generate_project_update_candidate(self, request: ProjectAutomationRequest | None = None) -> JsonObject:
        request = request or ProjectAutomationRequest()
        _validate_request(request)
        project = self._resolve_project(request)
        domains = list(request.domains) if request.domains else None
        sensitivity_allowed = list(request.sensitivity_allowed)
        sources = self.store.search_sources(
            query=str(project.get("name", "")),
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=request.max_items,
            scope_projects=(str(project["id"]),),
        )
        memories = self.store.search_memories(
            query=str(project.get("name", "")),
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            limit=request.max_items,
            projects=(str(project["id"]),),
        )
        # The bundled stores apply these predicates in SQL. Keep this
        # defensive check at the workflow boundary so legacy adapters cannot
        # widen a project-scoped report by ignoring optional query arguments.
        project_id = str(project["id"])
        sources = [row for row in sources if _is_source_in_project(row, project_id)]
        memories = [
            row for row in memories if _is_in_project(row, project_id) and row.get("status") in {"active", "accepted"}
        ]
        brain_charter = _brain_charter(self.store)
        automation_digest = _project_automation_digest(
            project=project,
            sources=sources,
            memories=memories,
            request=request,
            brain_charter=brain_charter,
        )
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing = find_existing(
                artifact_type="project_update",
                workflow="project_auto_update",
                digest=automation_digest,
                scope_projects=(project_id,),
            )
            if existing is not None:
                return existing
        change = _detect_project_change(project, sources, memories)
        suggested_current_state = change
        upsert_memory = getattr(self.store, "upsert_memory_by_key", None)
        persist_memory = upsert_memory if callable(upsert_memory) else self.store.create_memory
        candidate_memory = persist_memory(
            {
                "memory_type": "project_state",
                "memory_key": f"project_update.{_slug(str(project.get('name', 'project')))}.{automation_digest[:24]}",
                "value": {"project_id": project.get("id"), "suggested_current_state": suggested_current_state},
                "status": "candidate",
                "confidence": 0.72,
                "canonical_text": suggested_current_state,
                "summary": suggested_current_state,
                "domain": project.get("domain", "project"),
                "sensitivity": _highest_sensitivity([project, *sources, *memories]),
                "project_id": project_id,
                "metadata_json": {
                    **request.metadata_json,
                    "candidate": True,
                    "workflow": "project_auto_update",
                    "project_id": project.get("id"),
                    "project_scope": [project_id],
                    "automation_digest": automation_digest,
                    "source_ids": _source_ids(sources),
                    "memory_ids": _source_ids(memories),
                    "generated_by": request.generated_by,
                    "agent_identity": request.agent_identity,
                    "agent_id": request.actor_id if request.generated_by == "agent" else None,
                    "trace_id": request.trace_id,
                    "policy_decision": request.policy_decision,
                    "project_scope": [project_id],
                    "automation_digest": automation_digest,
                },
            },
            actor_type=request.generated_by,
        )
        content, prompt_hash, model_info_json, model_metadata = self._project_update_content(
            request=request,
            project=project,
            change=change,
            suggested_current_state=suggested_current_state,
            sources=sources,
            memories=memories,
        )
        artifact_record: JsonObject = {
            "artifact_type": "project_update",
            "title": f"Project Update Candidate - {_title(project)}",
            "content_markdown": content,
            "status": "needs_review",
            "domain": project.get("domain", "project"),
            "sensitivity": _highest_sensitivity([project, *sources, *memories]),
            "generated_by": request.generated_by if request.generated_by != "system" else "vnext_project_auto_updater",
            "prompt_hash": prompt_hash,
            "model_info_json": model_info_json,
            "metadata_json": {
                **request.metadata_json,
                "workflow": "project_auto_update",
                "workflow_type": "project_update_scan",
                "project_id": project.get("id"),
                "project_scope": [project_id],
                "automation_digest": automation_digest,
                "candidate_memory_id": candidate_memory.get("id"),
                "suggested_current_state": suggested_current_state,
                "source_ids": _source_ids(sources),
                "source_refs": [f"source:{source_id}" for source_id in _source_ids(sources)],
                "memory_ids": _source_ids(memories),
                "generated_by": request.generated_by,
                "agent_identity": request.agent_identity,
                "agent_id": request.actor_id if request.generated_by == "agent" else None,
                "agent_run_id": request.run_id if request.generated_by == "agent" else None,
                "trace_id": request.trace_id,
                "policy_decision": request.policy_decision,
                **model_metadata,
                "project_scope": [project_id],
                "automation_digest": automation_digest,
            },
        }
        upsert_artifact = getattr(self.store, "upsert_artifact_by_workflow_digest", None)
        if callable(upsert_artifact):
            artifact = upsert_artifact(
                artifact_record,
                workflow="project_auto_update",
                digest=automation_digest,
                actor_type=request.generated_by,
            )
        else:
            artifact = self.store.create_artifact(artifact_record, actor_type=request.generated_by)
        append_event(
            self.store,
            event_type="project.update_candidate_created",
            actor_type=request.generated_by,
            actor_id=request.actor_id,
            target_type="artifact",
            target_id=str(artifact["id"]),
            trace_id=request.trace_id,
            run_id=request.run_id,
            payload={
                "project_id": project.get("id"),
                "candidate_memory_id": candidate_memory.get("id"),
                "source_ids": _source_ids(sources),
                "agent_identity": request.agent_identity,
                "policy_decision": request.policy_decision,
                "generation_mode": request.generation_mode,
            },
        )
        return artifact

    def _project_update_content(
        self,
        *,
        request: ProjectAutomationRequest,
        project: JsonObject,
        change: str,
        suggested_current_state: str,
        sources: list[JsonObject],
        memories: list[JsonObject],
    ) -> tuple[str, str | None, JsonObject | None, JsonObject]:
        content = _project_update_markdown(
            project=project,
            change=change,
            suggested_current_state=suggested_current_state,
            sources=sources,
            memories=memories,
        )
        metadata: JsonObject = {"generation_mode": request.generation_mode}
        if request.generation_mode != "model_backed":
            return content, None, None, metadata
        source_refs = [f"source:{source_id}" for source_id in _source_ids(sources)]
        route = resolve_model_route(
            ModelRoutingRequest(
                workflow_type="project_update_scan",
                generation_mode="model_backed",
                domains=request.domains,
                sensitivity_allowed=request.sensitivity_allowed,
                agent_identity=request.agent_identity,
                brain_charter=_brain_charter(self.store),
                requested_route_mode=request.model_route_mode,
                requested_provider=request.model_provider,
                requested_model=request.model,
                allow_cloud_private=request.allow_cloud_private,
            )
        )
        model_artifact = build_model_backed_artifact(
            ModelBackedRequest(
                workflow_type="project_update_scan",
                title=f"Project Update Candidate - {_title(project)}",
                deterministic_markdown=content,
                context_rows=tuple([project, *sources, *memories]),
                source_refs=tuple(source_refs),
                open_questions=("Should this project state update be accepted, edited, or rejected?",),
                trace_id=request.trace_id,
                route=route,
                temperature=request.model_temperature,
                config={"generated_by": request.generated_by, "agent_id": request.actor_id},
            )
        )
        return (
            model_artifact.content_markdown,
            model_artifact.prompt_hash,
            model_artifact.model_info,
            {**metadata, **model_artifact.metadata},
        )

    def extract_open_loops(self, request: ProjectAutomationRequest | None = None) -> list[JsonObject]:
        request = request or ProjectAutomationRequest()
        _validate_request(request)
        domains = list(request.domains) if request.domains else None
        sources = self.store.search_sources(
            query="",
            domains=domains,
            sensitivity_allowed=list(request.sensitivity_allowed),
            limit=request.max_items,
            scope_projects=(request.project_id,) if request.project_id is not None else (),
        )
        if request.project_id is not None:
            sources = [row for row in sources if _is_source_in_project(row, request.project_id)]
        existing_by_digest: dict[str, JsonObject] = {}
        find_existing_loop = getattr(self.store, "find_open_loop_by_automation_digest", None)
        created: list[JsonObject] = []
        for source in sources:
            for candidate in _open_loop_candidates(source):
                source_scope = source_project_scope(source)
                canonical_scope = (request.project_id,) if request.project_id is not None else source_scope
                if len(canonical_scope) == 1:
                    candidate["project_id"] = canonical_scope[0]
                if request.person_id is not None:
                    candidate["person_id"] = request.person_id
                metadata_value = candidate.get("metadata_json")
                candidate_metadata: JsonObject = metadata_value if isinstance(metadata_value, dict) else {}
                automation_digest = _open_loop_digest(
                    candidate,
                    project_id=request.project_id,
                    person_id=request.person_id,
                )
                existing = existing_by_digest.get(automation_digest)
                if existing is None and callable(find_existing_loop):
                    existing = find_existing_loop(
                        digest=automation_digest,
                        project_id=request.project_id,
                        person_id=request.person_id,
                    )
                if existing is not None:
                    existing_by_digest[automation_digest] = existing
                    created.append(existing)
                    continue
                candidate["metadata_json"] = {
                    **candidate_metadata,
                    "project_scope": list(canonical_scope),
                    "automation_digest": automation_digest,
                    "generated_by": request.generated_by,
                    "agent_identity": request.agent_identity,
                    "agent_id": request.actor_id if request.generated_by == "agent" else None,
                    "trace_id": request.trace_id,
                    "policy_decision": request.policy_decision,
                }
                upsert_loop = getattr(self.store, "upsert_open_loop_by_automation_digest", None)
                if callable(upsert_loop):
                    created_loop = upsert_loop(
                        candidate,
                        digest=automation_digest,
                        actor_type=request.generated_by,
                    )
                else:
                    created_loop = self.store.create_open_loop(candidate, actor_type=request.generated_by)
                created.append(created_loop)
                existing_by_digest[automation_digest] = created_loop
        append_event(
            self.store,
            event_type="open_loop.extraction_completed",
            actor_type=request.generated_by,
            actor_id=request.actor_id,
            target_type="open_loop",
            trace_id=request.trace_id,
            run_id=request.run_id,
            payload={"created_count": len(created), "source_ids": _source_ids(sources)},
        )
        return created

    def review_project_update(
        self,
        *,
        artifact_id: str,
        action: str,
        edited_current_state: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> JsonObject:
        if action not in PROJECT_UPDATE_ACTIONS:
            raise VNextProjectValidationError("project update action must be accept, edit, or reject")
        # The artifact is the review decision's serialization point.  Every
        # accept/edit/reject path must inspect and transition the same locked
        # row so stale reviewers cannot split project, memory, and artifact
        # state across contradictory outcomes.
        artifact = self.store.get_artifact_for_update(artifact_id)
        if artifact is None:
            raise VNextProjectValidationError(f"artifact {artifact_id} was not found")
        metadata = artifact.get("metadata_json")
        artifact_status = str(artifact.get("status") or "")
        if artifact_status in {"accepted", "rejected"}:
            self._validate_terminal_project_update(
                artifact_id=artifact_id,
                artifact=artifact,
                terminal_status=artifact_status,
            )
        else:
            if not self.is_project_update_candidate(artifact):
                raise VNextProjectValidationError(f"artifact {artifact_id} is not a project update candidate")
            if artifact.get("artifact_type") != "project_update":
                raise VNextProjectValidationError("project update candidate has an invalid artifact_type")
            if not isinstance(metadata, dict) or metadata.get("workflow") != PROJECT_UPDATE_WORKFLOW:
                raise VNextProjectValidationError("project update candidate has an invalid workflow")
        if artifact_status == "accepted":
            if action in {"accept", "edit"}:
                return artifact
            raise VNextProjectValidationError("accepted project update candidates cannot be rejected")
        if artifact_status == "rejected":
            if action == "reject":
                return artifact
            raise VNextProjectValidationError("rejected project update candidates cannot be accepted or edited")
        if artifact_status != "needs_review":
            raise VNextProjectValidationError("project update candidate is not pending review")
        metadata = cast(JsonObject, metadata)
        candidate_memory_id = str(metadata.get("candidate_memory_id") or "")
        if candidate_memory_id == "":
            raise VNextProjectValidationError("project update candidate is missing candidate_memory_id")
        candidate_memory = self.store.get_memory_for_update(candidate_memory_id)
        if candidate_memory is None:
            raise VNextProjectValidationError("project update candidate memory was not found")
        candidate_metadata_value = candidate_memory.get("metadata_json")
        candidate_metadata: JsonObject = (
            dict(candidate_metadata_value) if isinstance(candidate_metadata_value, dict) else {}
        )
        candidate_value_value = candidate_memory.get("value")
        candidate_value: JsonObject = dict(candidate_value_value) if isinstance(candidate_value_value, dict) else {}
        supersession_marker = _project_candidate_supersession_marker(candidate_memory)
        if supersession_marker is not None:
            raise VNextProjectValidationError(
                f"project update candidate memory has already been superseded ({supersession_marker})"
            )
        if str(candidate_memory.get("status") or "") != "candidate":
            raise VNextProjectValidationError("project update candidate memory is not pending review")
        if candidate_memory.get("memory_type") != "project_state":
            raise VNextProjectValidationError("project update candidate memory has an invalid memory_type")
        if candidate_metadata.get("workflow") != PROJECT_UPDATE_WORKFLOW:
            raise VNextProjectValidationError("project update candidate memory has an invalid workflow linkage")
        if candidate_metadata.get("candidate") is not True:
            raise VNextProjectValidationError("project update candidate memory is not marked as a candidate")
        automation_digest = str(metadata.get("automation_digest") or "").strip()
        if automation_digest == "":
            raise VNextProjectValidationError("project update candidate is missing automation_digest")
        if str(candidate_metadata.get("automation_digest") or "").strip() != automation_digest:
            raise VNextProjectValidationError("project update candidate memory digest linkage does not match")
        project_id = str(metadata.get("project_id") or "").strip()
        if project_id == "":
            raise VNextProjectValidationError("project update candidate is missing project_id")
        if str(candidate_memory.get("project_id") or "").strip() != project_id:
            raise VNextProjectValidationError("project update candidate memory project linkage does not match")
        if str(candidate_metadata.get("project_id") or "").strip() != project_id:
            raise VNextProjectValidationError("project update candidate memory metadata project linkage does not match")
        if str(candidate_value.get("project_id") or "").strip() != project_id:
            raise VNextProjectValidationError("project update candidate memory value project linkage does not match")
        expected_scope_identity = project_scope_identity((project_id,))
        if project_scope_identity(resource_project_scope(artifact)) != expected_scope_identity:
            raise VNextProjectValidationError("project update candidate artifact scope linkage does not match")
        if project_scope_identity(resource_project_scope(candidate_memory)) != expected_scope_identity:
            raise VNextProjectValidationError("project update candidate memory scope linkage does not match")
        if action == "reject":
            memory_key = str(candidate_memory.get("memory_key") or "").strip()
            if memory_key == "":
                raise VNextProjectValidationError("candidate memory is missing memory_key")
            rejected_memory = self.store.update_memory(
                memory_id=candidate_memory_id,
                patch={
                    "status": "rejected",
                    "value": {**candidate_value, "review_status": "rejected"},
                    "metadata_json": {
                        **candidate_metadata,
                        "candidate": False,
                        "review_status": "rejected",
                        "review_action": action,
                        "review_artifact_id": artifact_id,
                    },
                },
                actor_type=actor_type,
            )
            self.store.append_revision(
                {
                    "memory_id": candidate_memory_id,
                    "memory_key": memory_key,
                    "revision_type": "rejected",
                    "action": "project_update_review",
                    "text_before": str(candidate_memory.get("canonical_text") or ""),
                    "text_after": str(rejected_memory.get("canonical_text") or ""),
                    "reason": "Project update candidate rejected by review action.",
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "metadata_json": {"artifact_id": artifact_id, "action": action},
                },
                actor_type=actor_type,
            )
            updated_artifact = self.store.update_artifact_status(
                artifact_id=artifact_id,
                status="rejected",
                expected_status="needs_review",
                metadata_json={
                    "candidate": False,
                    "review_status": "rejected",
                    "review_action": action,
                },
                actor_type=actor_type,
            )
            if updated_artifact is None:
                raise VNextProjectValidationError("project update candidate review conflicted with another reviewer")
            append_event(
                self.store,
                event_type="project.update_candidate_rejected",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="artifact",
                target_id=artifact_id,
                trace_id=trace_id,
                run_id=run_id,
                payload={"project_id": metadata.get("project_id"), "source_ids": metadata.get("source_ids", [])},
            )
            return updated_artifact

        if action == "edit" and (edited_current_state is None or edited_current_state.strip() == ""):
            raise VNextProjectValidationError("edited_current_state is required for edit")
        current_state = (
            edited_current_state.strip()
            if edited_current_state is not None
            else str(metadata.get("suggested_current_state", ""))
        )
        if current_state.strip() == "":
            raise VNextProjectValidationError("project update candidate current state is empty")
        # Accept makes the candidate memory active and writes the state onto
        # the project, so it takes the shared activation check first, in this
        # service's error contract (ruling C2).
        refuse_credential_activation(
            candidate_memory.get("title"),
            current_state,
            current_state,
            error=VNextProjectValidationError,
        )
        if self.store.get_project_for_update(project_id) is None:
            raise VNextProjectValidationError("project update candidate project was not found")
        self.store.update_project(
            project_id=project_id,
            patch={"current_state": current_state},
            actor_type=actor_type,
        )
        updated_memory = self.store.update_memory(
            memory_id=candidate_memory_id,
            patch={
                "status": "active",
                "value": {
                    **candidate_value,
                    "project_id": project_id,
                    "suggested_current_state": current_state,
                    "current_state": current_state,
                    "review_status": "accepted",
                },
                "canonical_text": current_state,
                "summary": current_state,
                "confirmation_status": "confirmed",
                "metadata_json": {
                    **candidate_metadata,
                    "candidate": False,
                    "review_status": "accepted",
                    "review_action": action,
                    "review_artifact_id": artifact_id,
                    "suggested_current_state": current_state,
                },
            },
            actor_type=actor_type,
        )
        memory_service = VNextMemoryCommitService(
            cast(PostgresVNextStore, self.store),
            defer_embeddings=self._defer_embeddings,
        )
        memory_service.refresh_memory_derived_state(
            updated_memory,
            stage="project_update_review",
        )
        self._deferred_embedding_inputs.extend(memory_service.deferred_embedding_inputs)
        memory_key = str(updated_memory.get("memory_key", "")).strip()
        if memory_key == "":
            raise VNextProjectValidationError("candidate memory is missing memory_key")
        self.store.append_revision(
            {
                "memory_id": candidate_memory_id,
                "memory_key": memory_key,
                "revision_type": "edited" if action == "edit" else "promoted",
                "action": "project_update_review",
                "text_before": str(candidate_memory.get("canonical_text") or ""),
                "text_after": current_state,
                "reason": "Project update candidate accepted by review action.",
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"artifact_id": artifact_id, "project_id": project_id, "action": action},
            },
            actor_type=actor_type,
        )
        updated_artifact = self.store.update_artifact_status(
            artifact_id=artifact_id,
            status="accepted",
            expected_status="needs_review",
            metadata_json={
                "candidate": False,
                "review_status": "accepted",
                "review_action": action,
                "suggested_current_state": current_state,
                "accepted_current_state": current_state,
            },
            actor_type=actor_type,
        )
        if updated_artifact is None:
            raise VNextProjectValidationError("project update candidate review conflicted with another reviewer")
        append_event(
            self.store,
            event_type="project.update_candidate_accepted",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="project",
            target_id=project_id,
            trace_id=trace_id,
            run_id=run_id,
            payload={"artifact_id": artifact_id, "candidate_memory_id": candidate_memory_id, "action": action},
        )
        return updated_artifact

    def _validate_terminal_project_update(
        self,
        *,
        artifact_id: str,
        artifact: JsonObject,
        terminal_status: str,
    ) -> None:
        """Prove a terminal decision from immutable persisted review evidence.

        The caller already holds the terminal artifact lock. Current linked
        rows are deliberately not evidence because later project updates,
        corrections, supersession, forget, and retention can legitimately
        change or remove them. The terminal artifact plus its append-only
        review revision and event prove the original coupled outcome.
        """

        def require(condition: bool) -> None:
            if not condition:
                raise VNextProjectTerminalConsistencyError(PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE)

        metadata_value = artifact.get("metadata_json")
        require(isinstance(metadata_value, dict))
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        require(str(artifact.get("id") or "") == artifact_id)
        require(artifact.get("artifact_type") == "project_update")
        require(artifact.get("status") == terminal_status)
        redacted_artifact = is_redacted_project_update_artifact(artifact)
        if metadata.get("redacted") is True:
            # A marker claim is privileged only when every content-bearing
            # artifact field and every retained metadata key match the exact
            # canonical skeleton. Partial/fabricated shapes fail closed.
            require(redacted_artifact)
        require(metadata.get("workflow") == PROJECT_UPDATE_WORKFLOW)
        candidate_memory_id = str(metadata.get("candidate_memory_id") or "").strip()
        project_id = str(metadata.get("project_id") or "").strip()
        automation_digest = str(metadata.get("automation_digest") or "").strip()
        require(bool(candidate_memory_id and project_id))
        if not redacted_artifact:
            require(bool(automation_digest))
            require(metadata.get("candidate") is False)
            require(metadata.get("review_status") == terminal_status)

        review_action = str(metadata.get("review_action") or "").strip()
        allowed_actions = {"accept", "edit"} if terminal_status == "accepted" else {"reject"}
        require(review_action in allowed_actions)
        expected_scope_identity = project_scope_identity((project_id,))
        require(project_scope_identity(resource_project_scope(artifact)) == expected_scope_identity)

        project_update_events = self.store.list_project_update_events(
            artifact_id=artifact_id,
            candidate_memory_id=candidate_memory_id,
        )

        creation_event_type = "project.update_candidate_created"
        creation_artifact_targets: set[str] = set()
        creation_events = [event for event in project_update_events if event.get("event_type") == creation_event_type]
        for event in creation_events:
            payload_value = event.get("payload_json")
            payload = payload_value if isinstance(payload_value, Mapping) else {}

            # Candidate creation is the immutable leg that binds terminal
            # review evidence to the artifact row currently held under lock.
            # A concurrent idempotent upsert can append the same creation event
            # more than once, so validate distinct targets rather than rows.
            require(isinstance(payload_value, Mapping))
            require(event.get("target_type") == "artifact")
            creation_target_id = str(event.get("target_id") or "").strip()
            require(bool(creation_target_id))
            redacted_creation_payload = {
                "redacted": True,
                "memory_id": candidate_memory_id,
                "event_type": creation_event_type,
            }
            if payload.get("redacted") is True:
                require(redacted_artifact)
                require(dict(payload) == redacted_creation_payload)
                require(event.get("integrity_hash") is None)
            else:
                require(not redacted_artifact)
                require(str(payload.get("candidate_memory_id") or "") == candidate_memory_id)
            creation_artifact_targets.add(creation_target_id)

        require(creation_artifact_targets == {artifact_id})

        expected_revision_type = (
            "edited" if review_action == "edit" else "promoted" if review_action == "accept" else "rejected"
        )
        review_revisions = [
            revision
            for revision in self.store.list_revisions(candidate_memory_id)
            if str(revision.get("memory_id") or "") == candidate_memory_id
            and revision.get("action") == "project_update_review"
        ]
        # There is exactly one decision revision per candidate.  Filtering by
        # the expected type first would let a second, contradictory decision
        # revision hide beside the apparently valid one.
        require(len(review_revisions) == 1)
        review_revision = review_revisions[0]
        require(bool(str(review_revision.get("memory_key") or "").strip()))
        require(review_revision.get("revision_type") == expected_revision_type)
        require(bool(str(review_revision.get("actor_type") or "").strip()))
        revision_metadata = review_revision.get("metadata_json")
        require(isinstance(revision_metadata, Mapping))
        revision_metadata = revision_metadata if isinstance(revision_metadata, Mapping) else {}

        redacted_revision = (
            dict(revision_metadata) == {"redacted": True}
            and review_revision.get("memory_key") == f"redacted.{candidate_memory_id}"
            and review_revision.get("source_event_ids") == []
            and (
                review_revision.get("previous_value") is None
                or review_revision.get("previous_value") == REDACTED_JSON_VALUE
            )
            and (review_revision.get("new_value") is None or review_revision.get("new_value") == REDACTED_JSON_VALUE)
            and review_revision.get("candidate") == REDACTED_JSON_VALUE
            and review_revision.get("text_before") == REDACTION_MARKER
            and review_revision.get("text_after") == REDACTION_MARKER
            and review_revision.get("reason") == REDACTION_MARKER
        )
        if revision_metadata.get("redacted") is True:
            # Only the exact skeleton written by the authorized true-redaction
            # path is admissible.  A partially scrubbed or fabricated marker
            # must fail closed instead of weakening the ordinary evidence
            # checks below.
            require(redacted_artifact)
            require(redacted_revision)
        else:
            require(not redacted_artifact)
            require(str(revision_metadata.get("artifact_id") or "") == artifact_id)
            require(revision_metadata.get("action") == review_action)

        if terminal_status == "accepted":
            if not redacted_artifact:
                accepted_state = str(metadata.get("accepted_current_state") or "").strip()
                require(bool(accepted_state))
                require(str(metadata.get("suggested_current_state") or "").strip() == accepted_state)
                require(bool(str(review_revision.get("text_before") or "").strip()))
                require(str(review_revision.get("text_after") or "").strip() == accepted_state)
                require(str(revision_metadata.get("project_id") or "").strip() == project_id)
        else:
            if not redacted_artifact:
                rejected_state = str(metadata.get("suggested_current_state") or "").strip()
                require(bool(rejected_state))
                require(not str(metadata.get("accepted_current_state") or "").strip())
                require(str(review_revision.get("text_before") or "").strip() == rejected_state)
                require(str(review_revision.get("text_after") or "").strip() == rejected_state)

        expected_event_type = f"project.update_candidate_{terminal_status}"
        decision_event_types = {
            "project.update_candidate_accepted",
            "project.update_candidate_rejected",
        }
        coupled_events = [event for event in project_update_events if event.get("event_type") in decision_event_types]

        # First reject every competing decision coupled through any supported
        # artifact/candidate linkage. Filtering by the expected outcome,
        # actor, target, or payload before this cardinality check would let a
        # contradictory durable decision hide beside the apparently valid one.
        require(len(coupled_events) == 1)
        decision_event = coupled_events[0]
        payload_value = decision_event.get("payload_json")
        require(isinstance(payload_value, Mapping))
        payload = payload_value if isinstance(payload_value, Mapping) else {}
        require(decision_event.get("event_type") == expected_event_type)
        require(decision_event.get("actor_type") == review_revision.get("actor_type"))
        require(decision_event.get("actor_id") == review_revision.get("actor_id"))

        redacted_event_payload = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": expected_event_type,
        }
        if terminal_status == "accepted":
            require(decision_event.get("target_type") == "project")
            require(str(decision_event.get("target_id") or "") == project_id)
            accepted_event_payload = {
                "artifact_id": artifact_id,
                "candidate_memory_id": candidate_memory_id,
                "action": review_action,
            }
            if payload.get("redacted") is True:
                # True redaction deliberately retains this exact linkage
                # skeleton while scrubbing every content-bearing field.
                require(redacted_artifact)
                require(dict(payload) == redacted_event_payload)
                require(decision_event.get("integrity_hash") is None)
            else:
                require(not redacted_artifact)
                require(dict(payload) == accepted_event_payload)
        else:
            require(decision_event.get("target_type") == "artifact")
            require(str(decision_event.get("target_id") or "") == artifact_id)
            if payload.get("redacted") is True:
                require(redacted_artifact)
                require(dict(payload) == redacted_event_payload)
                require(decision_event.get("integrity_hash") is None)
            else:
                require(not redacted_artifact)
                artifact_source_ids = metadata.get("source_ids")
                require(isinstance(artifact_source_ids, list))
                rejected_event_payload = {
                    "project_id": project_id,
                    "source_ids": [str(source_id) for source_id in artifact_source_ids]
                    if isinstance(artifact_source_ids, list)
                    else [],
                }
                require(dict(payload) == rejected_event_payload)

    def review_open_loop(
        self,
        *,
        loop_id: str,
        action: str,
        title: str | None = None,
        description: str | None = None,
        due_at: str | None = None,
        priority: str | None = None,
        resolution_note: str | None = None,
    ) -> JsonObject:
        if action not in OPEN_LOOP_ACTIONS:
            raise VNextProjectValidationError("open loop action must be close, snooze, edit, or reopen")
        if self.store.get_open_loop(loop_id) is None:
            raise VNextProjectValidationError(f"open loop {loop_id} was not found")
        if action == "close":
            return self.store.update_open_loop_status(
                loop_id=loop_id, status="resolved", resolution_note=resolution_note
            )
        if action == "reopen":
            return self.store.update_open_loop_status(loop_id=loop_id, status="open")
        patch: JsonObject = {}
        if title is not None:
            patch["title"] = title
        if description is not None:
            patch["description"] = description
        if due_at is not None:
            patch["due_at"] = due_at
        if priority is not None:
            patch["priority"] = priority
        if action == "snooze" and due_at is None:
            raise VNextProjectValidationError("due_at is required for snooze")
        if not patch:
            raise VNextProjectValidationError("at least one editable field is required")
        return self.store.update_open_loop(loop_id=loop_id, patch=patch)

    def project_dashboard(
        self, *, project_id: str, sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    ) -> JsonObject:
        project = self.store.get_project(project_id)
        if project is None:
            raise VNextProjectValidationError(f"project {project_id} was not found")
        domain = str(project.get("domain", "unknown"))
        memories = self.store.search_memories(
            query=str(project.get("name", "")),
            domains=[domain],
            sensitivity_allowed=list(sensitivity_allowed),
            limit=DEFAULT_PROJECT_LIMIT,
            projects=(project_id,),
        )
        memories = [
            row for row in memories if _is_in_project(row, project_id) and row.get("status") in {"active", "accepted"}
        ]
        open_loops = self.store.list_open_loops(
            status="open",
            domains=[domain],
            sensitivity_allowed=list(sensitivity_allowed),
            project_id=project_id,
            limit=DEFAULT_PROJECT_LIMIT,
        )
        artifact_candidates = self.store.list_artifacts(
            artifact_type=None,
            domains=[domain],
            sensitivity_allowed=list(sensitivity_allowed),
            limit=DEFAULT_PROJECT_LIMIT,
            scope_projects=(project_id,),
        )
        artifacts = [row for row in artifact_candidates if _is_in_project(row, project_id)][:DEFAULT_PROJECT_LIMIT]
        return {
            "project": project,
            "state": project.get("current_state"),
            "memories": memories,
            "open_loops": open_loops,
            "artifacts": artifacts,
            "counts": {"memories": len(memories), "open_loops": len(open_loops), "artifacts": len(artifacts)},
        }

    def _resolve_project(self, request: ProjectAutomationRequest) -> JsonObject:
        if request.project_id is not None:
            project = self.store.get_project(request.project_id)
            if project is None:
                raise VNextProjectValidationError(f"project {request.project_id} was not found")
            return project
        projects = self.store.list_projects(
            status="active",
            domains=list(request.domains) if request.domains else None,
            sensitivity_allowed=list(request.sensitivity_allowed),
            limit=1,
        )
        if not projects:
            raise VNextProjectValidationError("no active project was found for update candidate generation")
        return projects[0]


__all__ = [
    "PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE",
    "ProjectAutomationRequest",
    "VNextProjectService",
    "VNextProjectStore",
    "VNextProjectTerminalConsistencyError",
    "VNextProjectValidationError",
]
