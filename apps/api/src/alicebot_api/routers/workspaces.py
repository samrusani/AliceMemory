from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import psycopg
from psycopg.rows import dict_row

from alicebot_api.config import get_settings
from alicebot_api.db import user_connection
from alicebot_api.vnext_label_guard import label_read_request
from alicebot_api.local_workspace import (
    ensure_local_workspace,
    get_local_workspace,
    serialize_local_workspace,
)
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers._api_shared import _resolve_authenticated_v1_user_id
from alicebot_api.routers._vnext_shared import _vnext_agent_auth_error_response, _vnext_int, _vnext_source_trace
from alicebot_api.routers.providers import (
    _discover_provider_capability,
    _persist_discovered_provider_capability,
    _seed_workspace_provider_configs,
)
from alicebot_api.vnext_agent_control import AgentIdentity, summarize_agent_policy_telemetry
from alicebot_api.vnext_agent_keys import AgentKeyAuthenticationError, agent_key_from_authorization, resolve_protected_agent_identity
from alicebot_api.vnext_connectors import VNextConnectorService
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_projects import VNextProjectService, VNextProjectValidationError
from alicebot_api.vnext_scheduler import VNextSchedulerService
from alicebot_api.vnext_scheduler_runtime import daemon_status
from alicebot_api.vnext_store import PostgresVNextStore


core_router = APIRouter()
bootstrap_router = APIRouter()


def _vnext_status_counts(rows: list[dict[str, object]], *, field: str = "status") -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get(field, "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _workspace_event_visible(store: PostgresVNextStore, event: dict[str, object], sensitivity: list[str]) -> bool:
    """An event stays when its target row is inside the workspace sensitivity list."""

    kind = str(event.get("target_type") or "")
    target_id = event.get("target_id")
    getters = {
        "memory": "get_memory",
        "open_loop": "get_open_loop",
        "artifact": "get_artifact",
        "project": "get_project",
        "source": "get_source",
    }
    getter_name = getters.get(kind)
    if getter_name is None or not isinstance(target_id, str) or target_id == "":
        return True
    getter = getattr(store, getter_name, None)
    if not callable(getter):
        return True
    row = getter(target_id)
    if not isinstance(row, Mapping):
        return True
    return bool(_workspace_rows(store, kind, [row], sensitivity))


def _workspace_rows(store: PostgresVNextStore, kind: str, rows: Sequence[Mapping[str, object]], sensitivity: list[str]):
    from alicebot_api.vnext_label_guard import admit_loaded

    return admit_loaded(
        store,
        kind=kind,
        rows=rows,
        domains=(),
        sensitivity_allowed=sensitivity,
        projects=(),
    )


@label_read_request
def _vnext_workspace_payload(store: PostgresVNextStore, *, identity: AgentIdentity | None = None) -> dict[str, object]:
    from alicebot_api.vnext_label_guard import LabelGuard, sensitivity_ceiling
    from alicebot_api.vnext_open_loop_references import withhold_unreadable_references
    from alicebot_api.vnext_source_fence import SourceReadFence

    sensitivity_allowed = ["public", "internal", "private", "unknown"]
    ceiling = sensitivity_ceiling(identity)
    if ceiling is not None:
        sensitivity_allowed = [value for value in sensitivity_allowed if value in ceiling]
    requested_projects = identity.project_scope if identity is not None else ()
    all_of = requested_projects if identity is not None and identity.project_scope_locked else None
    guard = LabelGuard.for_filters(store, (), sensitivity_allowed, requested_projects, all_of=all_of)
    unfenced = not SourceReadFence.for_identity(identity).entity_read_fenced
    if unfenced:
        guard = LabelGuard(store=store, active=False)
    review_statuses = ["candidate", "needs_review", "private_only", "accepted", "rejected"]
    fetched_sources = store.list_sources(sensitivity_allowed=sensitivity_allowed, limit=20)
    sources = guard.admit_rows("source", fetched_sources)
    source_count = store.count_sources() if unfenced else sum(guard.readable_status_counts("source").values())
    list_memories_by_statuses = getattr(store, "list_memories_by_statuses", None)
    if callable(list_memories_by_statuses):
        fetched_memories = list_memories_by_statuses(
            statuses=review_statuses,
            sensitivity_allowed=sensitivity_allowed,
            limit=30,
        )
    else:  # Compatibility for external/test stores implementing the older protocol.
        fetched_memories = [
            memory for memory in store.list_memories(status=None) if str(memory.get("status")) in set(review_statuses)
        ][:30]
    review_memories = guard.admit_rows("memory", fetched_memories)
    memory_status_counts = store.count_memories_by_status(sensitivity_allowed=sensitivity_allowed) if unfenced else guard.readable_status_counts("memory")
    review_memory_total = sum(memory_status_counts.get(status, 0) for status in review_statuses)
    fetched_artifacts = store.list_artifacts(sensitivity_allowed=sensitivity_allowed, limit=30)
    artifacts = guard.admit_rows("artifact", fetched_artifacts)
    artifact_status_counts = store.count_artifacts_by_status() if unfenced else guard.readable_status_counts("artifact")
    artifact_count = sum(artifact_status_counts.values())
    quality_evals = guard.admit_related_rows(store.list_artifact_quality_ratings(limit=50), kind="artifact", field="artifact_id")
    quality_eval_count = store.count_artifact_quality_ratings() if unfenced else sum(
        len(guard.admit_related_rows(batch, kind="artifact", field="artifact_id"))
        for batch in store.iter_label_ratings()
    )
    fetched_projects = store.list_projects(status=None, sensitivity_allowed=sensitivity_allowed, limit=20)
    projects = guard.admit_rows("project", fetched_projects)
    project_count = store.count_projects() if unfenced else sum(guard.readable_status_counts("project").values())
    fetched_loops = store.list_open_loops(status=None, sensitivity_allowed=sensitivity_allowed, limit=30)
    open_loops = guard.admit_rows("open_loop", fetched_loops)
    open_loops = withhold_unreadable_references(store, open_loops, fence=SourceReadFence.for_identity(identity))
    open_loop_status_counts = store.count_open_loops_by_status() if unfenced else guard.readable_status_counts("open_loop")
    open_loop_count = open_loop_status_counts.get("open", 0)
    people = store.list_people(sensitivity_allowed=sensitivity_allowed, limit=12)
    fetched_beliefs = store.list_beliefs(status=None, sensitivity_allowed=sensitivity_allowed, limit=12)
    beliefs = guard.admit_beliefs(fetched_beliefs)
    tasks = store.list_tasks(status=None, limit=12)
    if getattr(type(store), "label_count_input_prefilter", False):
        fetched_events = store.list_events(limit=20, reject_sensitivity_allowed=guard.sensitivity_allowed)
    else:
        fetched_events = store.list_events(limit=20)
    recent_events = guard.admit_events(fetched_events)
    event_count = store.count_events() if unfenced else guard.readable_event_count()
    agent_identities = store.list_agent_identities(limit=20)
    agent_count = store.count_agent_identities()
    agent_events = guard.admit_events(store.list_agent_events(limit=50))
    list_recent_agentic_commits = getattr(store, "list_recent_agentic_commits", None)
    list_pending_inline_confirmations = getattr(store, "list_pending_inline_confirmations", None)
    memory_commit_service = VNextMemoryCommitService(store)
    recent_memory_commits = (
        list_recent_agentic_commits(limit=20)
        if callable(list_recent_agentic_commits)
        else memory_commit_service.recent_commits(limit=20)["recent_commits"]
    )
    inline_confirmations = (
        list_pending_inline_confirmations(limit=20)
        if callable(list_pending_inline_confirmations)
        else memory_commit_service.inline_confirmations(limit=20)
    )
    recent_memory_commits = guard.admit_rows("memory", recent_memory_commits if isinstance(recent_memory_commits, list) else [])
    inline_confirmations = guard.admit_rows("memory", inline_confirmations)
    scheduler_status = VNextSchedulerService(store).status()
    scheduler_status = {**scheduler_status, "daemon": daemon_status()}
    connector_health = VNextConnectorService(store).connector_health_all()
    dogfooding = VNextDogfoodingService(store).dashboard() if unfenced else VNextDogfoodingService(store).dashboard(sensitivity_allowed=tuple(sensitivity_allowed), label_guard=guard)
    doctor = VNextDoctorService(store).run(ci=True, include_content_diagnostics=unfenced)
    policy_telemetry = summarize_agent_policy_telemetry(
        agent_events=agent_events,
        artifacts=artifacts,
        memories=review_memories,
    )
    project_service = VNextProjectService(store)
    project_dashboards: list[dict[str, object]] = []
    for project in projects[:5]:
        try:
            project_dashboards.append(project_service.project_dashboard(project_id=str(project["id"]), identity=identity))
        except VNextProjectValidationError:
            continue
    trace_items = [
        _vnext_source_trace(
            store=store,
            source=source,
            memories=review_memories,
            artifacts=artifacts,
            open_loops=open_loops,
            events=recent_events,
            memory_scope="bounded_workspace_review_sample",
        )
        for source in sources[:8]
    ]
    return {
        "mode": "live",
        "summary": {
            "source_count": source_count,
            "candidate_memory_count": memory_status_counts.get("candidate", 0),
            "review_memory_count": review_memory_total,
            "artifact_count": artifact_count,
            "open_loop_count": open_loop_count,
            "project_count": project_count,
            "event_count": event_count,
            "agent_count": agent_count,
            "scheduler_enabled_count": _vnext_int(scheduler_status, "enabled_count", 0),
            "memory_status_counts": memory_status_counts,
            "artifact_status_counts": artifact_status_counts,
            "quality_eval_count": quality_eval_count,
            "open_loop_status_counts": open_loop_status_counts,
        },
        "sources": sources,
        "review_memories": review_memories,
        "samples": {
            "sources": {
                "returned_count": len(sources),
                "total_count": source_count,
                "limit": 20,
                "has_more": source_count > len(sources),
            },
            "review_memories": {
                "returned_count": len(review_memories),
                "total_count": review_memory_total,
                "limit": 30,
                "has_more": review_memory_total > len(review_memories),
            },
            "recent_events": {
                "returned_count": len(recent_events),
                "total_count": event_count,
                "limit": 20,
                "has_more": event_count > len(recent_events),
            },
            "artifacts": {
                "returned_count": len(artifacts),
                "total_count": artifact_count,
                "limit": 30,
                "has_more": artifact_count > len(artifacts),
            },
            "quality_evals": {
                "returned_count": len(quality_evals),
                "total_count": quality_eval_count,
                "limit": 50,
                "has_more": quality_eval_count > len(quality_evals),
            },
            "projects": {
                "returned_count": len(projects),
                "total_count": project_count,
                "limit": 20,
                "has_more": project_count > len(projects),
            },
            "open_loops": {
                "returned_count": len(open_loops),
                "total_count": sum(open_loop_status_counts.values()),
                "limit": 30,
                "has_more": sum(open_loop_status_counts.values()) > len(open_loops),
            },
            "agent_identities": {
                "returned_count": len(agent_identities),
                "total_count": agent_count,
                "limit": 20,
                "has_more": agent_count > len(agent_identities),
            },
        },
        "artifacts": artifacts,
        "quality_evals": quality_evals,
        "connector_health": connector_health,
        "dogfooding": dogfooding,
        "doctor": doctor,
        "traceability": {
            "items": trace_items,
            "count": len(trace_items),
            "order": [str(trace.get("trace_id")) for trace in trace_items],
        },
        "projects": projects,
        "project_dashboards": project_dashboards,
        "open_loops": open_loops,
        "people": people,
        "beliefs": beliefs,
        "tasks": tasks,
        "recent_events": recent_events,
        "agent_activity": {
            "agents": agent_identities,
            "recent_events": agent_events,
            "policy_blocks": [
                event
                for event in agent_events
                if event.get("event_type") in {"agent.policy_blocked", "agent.policy_filtered"}
            ],
            "generated_artifacts": [
                artifact
                for artifact in artifacts
                if isinstance((artifact_metadata := artifact.get("metadata_json")), dict)
                and artifact_metadata.get("generated_by") == "agent"
            ],
            "pending_review_items": [
                memory
                for memory in review_memories
                if isinstance((memory_metadata := memory.get("metadata_json")), dict)
                and memory_metadata.get("agent_id") is not None
            ],
            "recent_commits": recent_memory_commits,
            "inline_confirmations": inline_confirmations,
        },
        "policy_telemetry": policy_telemetry,
        "scheduler": scheduler_status,
        "brain_charter": store.get_brain_charter(),
    }


@core_router.get("/v0/vnext/workspace")
def get_vnext_workspace(user_id: UUID, authorization: str | None = Header(default=None)) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            # The project dashboards read artifacts through a long scope expression. Its estimated
            # cost is above PostgreSQL's JIT threshold, so each such read spent about a quarter of
            # a second compiling a query that runs in two milliseconds. Compilation never pays for
            # these short reads. The setting ends with this transaction.
            conn.execute("SET LOCAL jit = off")
            store = PostgresVNextStore(conn)
            identity = resolve_protected_agent_identity(
                store, user_id=user_id, raw_key=agent_key_from_authorization(authorization), payload={},
            )
            payload = _vnext_workspace_payload(store, identity=identity)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@bootstrap_router.post("/v1/workspaces/bootstrap")
def bootstrap_v1_workspace(request: Request) -> JSONResponse:
    settings = get_settings()
    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                context = ensure_local_workspace(conn, user_account_id=user_account_id)
        workspace_id = context["workspace"]["id"]
        seeded_providers = _seed_workspace_provider_configs(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=workspace_id,
        )
        for provider in seeded_providers:
            discovery = _discover_provider_capability(provider=provider, settings=settings)
            _persist_discovered_provider_capability(
                settings=settings,
                user_account_id=user_account_id,
                workspace_id=workspace_id,
                provider=provider,
                outcome=discovery,
            )
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "workspace": serialize_local_workspace(context["workspace"]),
                "bootstrap": {
                    "workspace_id": str(workspace_id),
                    "status": "ready",
                    "bootstrapped_at": (
                        None
                        if context["workspace"]["bootstrapped_at"] is None
                        else context["workspace"]["bootstrapped_at"].isoformat()
                    ),
                },
                "seeded_provider_count": len(seeded_providers),
            }
        ),
    )


@bootstrap_router.get("/v1/workspaces/bootstrap/status")
def get_v1_workspace_bootstrap_status(request: Request) -> JSONResponse:
    settings = get_settings()
    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                context = get_local_workspace(conn, user_account_id=user_account_id)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    if context is None:
        return public_exception_response(
            LookupError("local workspace is not bootstrapped; POST /v1/workspaces/bootstrap first"),
            status_code=404,
        )
    workspace = context["workspace"]
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "workspace": serialize_local_workspace(workspace),
                "bootstrap": {
                    "workspace_id": str(workspace["id"]),
                    "status": workspace["bootstrap_status"],
                    "bootstrapped_at": (
                        None if workspace["bootstrapped_at"] is None else workspace["bootstrapped_at"].isoformat()
                    ),
                },
            }
        ),
    )
