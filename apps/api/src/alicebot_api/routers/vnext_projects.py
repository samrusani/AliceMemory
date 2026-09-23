from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from alicebot_api.config import get_settings
from alicebot_api.db import user_connection
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers._vnext_automation import (
    VNextProjectAutomationRequest,
    _vnext_model_generation_options,
    _vnext_project_automation_request,
)
from alicebot_api.routers._vnext_shared import (
    VNextAgentRequest,
    VNextDomain,
    VNextSensitivity,
    _vnext_agent_actor,
    _vnext_agent_auth_error_response,
    _vnext_agent_identity,
    _vnext_authenticated_agent_identity,
    _vnext_permission_response,
    _vnext_policy_checked,
    _vnext_public_error_response,
    _vnext_string_list,
)
from alicebot_api.vnext_agent_control import (
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    agent_metadata,
    summarize_agent_policy_telemetry,
)
from alicebot_api.vnext_agent_keys import AgentKeyAuthenticationError
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_projects import (
    VNextProjectService,
    VNextProjectValidationError,
)
from alicebot_api.vnext_scheduler import (
    SchedulerRunRequest,
    VNextSchedulerService,
    VNextSchedulerValidationError,
    validate_schedule,
)
from alicebot_api.vnext_scheduler_runtime import (
    daemon_status,
    run_due_workflows_durable,
    run_now_durable,
)
from alicebot_api.vnext_store import PostgresVNextStore


project_core_router = APIRouter()
project_operations_router = APIRouter()


class VNextProjectCreateRequest(VNextAgentRequest):
    user_id: UUID
    name: str = Field(min_length=1, max_length=280)
    slug: str | None = Field(default=None, min_length=1, max_length=280)
    status: str = Field(default="active", min_length=1, max_length=40)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    current_state: str | None = Field(default=None, min_length=1, max_length=4000)
    domain: VNextDomain = "project"
    sensitivity: VNextSensitivity = "private"

class VNextOpenLoopReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    due_at: str | None = Field(default=None, min_length=1, max_length=120)
    priority: str | None = Field(default=None, min_length=1, max_length=80)
    resolution_note: str | None = Field(default=None, min_length=1, max_length=4000)

class VNextOpenLoopCreateRequest(VNextAgentRequest):
    user_id: UUID
    title: str = Field(min_length=1, max_length=280)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    due_at: str | None = Field(default=None, min_length=1, max_length=120)
    priority: str = Field(default="normal", min_length=1, max_length=80)
    memory_id: str | None = Field(default=None, min_length=1, max_length=120)
    project_id: str | None = Field(default=None, min_length=1, max_length=120)
    source_id: str | None = Field(default=None, min_length=1, max_length=120)
    domain: VNextDomain = "unknown"
    sensitivity: VNextSensitivity = "unknown"

class VNextBrainCharterUpsertRequest(VNextAgentRequest):
    user_id: UUID
    content_markdown: str = Field(min_length=1, max_length=200_000)
    owner_json: dict[str, object] = Field(default_factory=dict)
    memory_philosophy_json: dict[str, object] = Field(default_factory=dict)
    life_domains_json: dict[str, object] = Field(default_factory=dict)
    active_projects_json: list[object] = Field(default_factory=list)
    communication_style_json: dict[str, object] = Field(default_factory=dict)
    priorities_json: dict[str, object] = Field(default_factory=dict)
    autonomous_rules_json: list[object] = Field(default_factory=list)
    quality_standard_json: list[object] = Field(default_factory=list)
    sensitivity: VNextSensitivity = "private"

class VNextSchedulerWorkflowPatchRequest(VNextAgentRequest):
    user_id: UUID
    enabled: bool | None = None
    paused: bool | None = None
    schedule_json: dict[str, object] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=120)
    model_options: dict[str, object] = Field(default_factory=dict)

class VNextSchedulerRunNowRequest(VNextAgentRequest):
    user_id: UUID
    scope: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)

class VNextSchedulerRunDueRequest(VNextAgentRequest):
    user_id: UUID
    limit: int = Field(default=10, ge=1, le=50)

class VNextSchedulerControlRequest(VNextAgentRequest):
    user_id: UUID

def _vnext_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "project"

@project_core_router.post("/v0/vnext/projects")
def create_vnext_project(request: VNextProjectCreateRequest) -> JSONResponse:
    settings = get_settings()
    slug = request.slug or _vnext_slug(request.name)

    with user_connection(settings.database_url, request.user_id) as conn:
        payload = PostgresVNextStore(conn).create_project(
            {
                "name": request.name.strip(),
                "slug": slug,
                "status": request.status,
                "description": request.description,
                "current_state": request.current_state,
                "domain": request.domain,
                "sensitivity": request.sensitivity,
                "metadata_json": {"created_from": "vnext_workspace"},
            },
            actor_type="user",
        )

    return JSONResponse(status_code=201, content=jsonable_encoder({"project": payload}))

@project_core_router.get("/v0/vnext/projects")
def list_vnext_projects(user_id: UUID, status: str | None = "active", limit: int = 20) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = PostgresVNextStore(conn).list_projects(status=status, limit=limit)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder({"items": payload, "count": len(payload), "order": ["updated_at_desc", "id_desc"]}),
    )

@project_operations_router.get("/v0/vnext/projects/{project_id}/dashboard")
def get_vnext_project_dashboard(project_id: str, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = VNextProjectService(PostgresVNextStore(conn)).project_dashboard(project_id=project_id)
    except VNextProjectValidationError:
        return _vnext_public_error_response(status_code=404, detail="vNext project was not found")

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))

@project_operations_router.post("/v0/vnext/open-loops")
def create_vnext_open_loop(
    request: VNextOpenLoopCreateRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="open_loop.create",
                domains=(request.domain,),
                sensitivity_allowed=(request.sensitivity,),
                project_scope=tuple(request.project_scope),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, _actor_id = _vnext_agent_actor(identity, fallback="user")
            payload = store.create_open_loop(
                {
                    "title": request.title.strip(),
                    "description": request.description,
                    "due_at": request.due_at,
                    "priority": request.priority,
                    "memory_id": request.memory_id,
                    "project_id": request.project_id,
                    "source_id": request.source_id,
                    "domain": request.domain,
                    "sensitivity": request.sensitivity,
                    "metadata_json": {
                        "created_from": "vnext_workspace",
                        **agent_metadata(identity, decision),
                    },
                },
                actor_type=actor_type,
            )
            if identity is not None:
                append_event(
                    store,
                    event_type="agent.open_loop_created",
                    actor_type="agent",
                    actor_id=identity.agent_id,
                    target_type="open_loop",
                    target_id=str(payload["id"]),
                    trace_id=request.trace_id or decision.trace_id,
                    run_id=identity.agent_run_id,
                    payload={"agent_identity": identity.to_record(), "policy_decision": decision.to_record()},
                )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(status_code=201, content=jsonable_encoder({"open_loop": payload}))

@project_operations_router.get("/v0/vnext/settings/brain-charter")
def get_vnext_brain_charter(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = PostgresVNextStore(conn).get_brain_charter()

    return JSONResponse(status_code=200, content=jsonable_encoder({"brain_charter": payload}))

@project_operations_router.put("/v0/vnext/settings/brain-charter")
def upsert_vnext_brain_charter(request: VNextBrainCharterUpsertRequest) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, request.user_id) as conn:
        payload = PostgresVNextStore(conn).upsert_brain_charter(
            {
                "content_markdown": request.content_markdown,
                "owner_json": request.owner_json,
                "memory_philosophy_json": request.memory_philosophy_json,
                "life_domains_json": request.life_domains_json,
                "active_projects_json": request.active_projects_json,
                "communication_style_json": request.communication_style_json,
                "priorities_json": request.priorities_json,
                "autonomous_rules_json": request.autonomous_rules_json,
                "quality_standard_json": request.quality_standard_json,
                "sensitivity": request.sensitivity,
            },
            actor_type="user",
        )

    return JSONResponse(status_code=200, content=jsonable_encoder({"brain_charter": payload}))

@project_operations_router.get("/v0/vnext/scheduler/status")
def get_vnext_scheduler_status(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = VNextSchedulerService(PostgresVNextStore(conn)).status()
    payload = {**payload, "daemon": daemon_status()}

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))

@project_operations_router.get("/v0/vnext/scheduler/runs")
def list_vnext_scheduler_runs(user_id: UUID, workflow_type: str | None = None, limit: int = 20) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = PostgresVNextStore(conn).list_scheduler_runs(workflow_type=workflow_type, limit=limit)

    return JSONResponse(status_code=200, content=jsonable_encoder({"items": payload, "count": len(payload)}))

@project_operations_router.get("/v0/vnext/scheduler/failures")
def list_vnext_scheduler_failures(user_id: UUID, workflow_type: str | None = None, limit: int = 20) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        runs = [
            run
            for run in PostgresVNextStore(conn).list_scheduler_runs(
                workflow_type=workflow_type,
                limit=max(limit * 4, limit),
            )
            if run.get("status") == "failed"
        ][:limit]

    return JSONResponse(status_code=200, content=jsonable_encoder({"items": runs, "count": len(runs)}))

@project_operations_router.get("/v0/vnext/agents/policy-telemetry")
def get_vnext_agent_policy_telemetry(
    user_id: UUID,
    agent_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> JSONResponse:
    settings = get_settings()
    bounded_limit = min(max(limit, 1), 200)

    with user_connection(settings.database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        payload = summarize_agent_policy_telemetry(
            agent_events=store.list_agent_events(agent_id=agent_id, limit=bounded_limit),
            artifacts=store.list_agent_policy_artifacts(agent_id=agent_id, limit=bounded_limit),
            memories=store.list_agent_policy_memories(agent_id=agent_id, limit=bounded_limit),
        )

    return JSONResponse(status_code=200, content=jsonable_encoder({"summary": payload}))

@project_operations_router.patch("/v0/vnext/scheduler/workflows/{workflow_type}")
def patch_vnext_scheduler_workflow(
    workflow_type: str,
    request: VNextSchedulerWorkflowPatchRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
        if request.schedule_json is not None:
            validate_schedule(workflow_type, request.schedule_json)
    except (AgentIdentityValidationError, VNextSchedulerValidationError) as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="scheduler.configure",
                workflow_type=workflow_type,
                project_scope=tuple(request.project_scope),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, _actor_id = _vnext_agent_actor(identity, fallback="user")
            payload = VNextSchedulerService(store).configure_workflow(
                workflow_type=workflow_type,
                enabled=request.enabled,
                paused=request.paused,
                schedule_json=request.schedule_json,
                timezone=request.timezone,
                metadata_json={"model_options": _vnext_model_generation_options(request.model_options)}
                if request.model_options
                else None,
                actor_type=actor_type,
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextSchedulerValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=200, content=jsonable_encoder({"workflow": payload, "policy_decision": decision.to_record()})
    )

@project_operations_router.post("/v0/vnext/scheduler/workflows/{workflow_type}/run-now")
def run_vnext_scheduler_workflow_now(
    workflow_type: str,
    request: VNextSchedulerRunNowRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)
    scope = request.scope
    options = request.options
    requested_domains = _vnext_string_list(scope, "domains")
    requested_sensitivity = _vnext_string_list(options, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="scheduler.run_now",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(scope, "projects"),
                workflow_type=workflow_type,
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            triggered_by = "agent" if identity is not None else "user"
            scheduler_request = SchedulerRunRequest(
                workflow_type=workflow_type,
                domains=decision.effective_domains,
                projects=decision.effective_project_scope,
                sensitivity_allowed=decision.effective_sensitivity_allowed,
                generated_for=str(options["generated_for"]) if isinstance(options.get("generated_for"), str) else None,
                triggered_by=triggered_by,
                agent_identity=identity,
                policy_decision=decision,
                options=options,
            )
        payload = run_now_durable(
            database_url=settings.database_url,
            user_id=request.user_id,
            request=scheduler_request,
        )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextSchedulerValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(status_code=201, content=jsonable_encoder({**payload, "policy_decision": decision.to_record()}))

@project_operations_router.post("/v0/vnext/scheduler/run-due")
def run_vnext_scheduler_due(
    request: VNextSchedulerRunDueRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    actor_type = "scheduler"
    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="scheduler.run_due",
                project_scope=tuple(request.project_scope),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, _actor_id = _vnext_agent_actor(identity, fallback="scheduler")
        payload = run_due_workflows_durable(
            database_url=settings.database_url,
            user_id=request.user_id,
            limit=request.limit,
            triggered_by=actor_type,
            agent_identity=identity,
            policy_decision=decision,
        )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextSchedulerValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(status_code=201, content=jsonable_encoder({**payload, "policy_decision": decision.to_record()}))

@project_operations_router.post("/v0/vnext/scheduler/pause")
def pause_vnext_scheduler(
    request: VNextSchedulerControlRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    return _vnext_scheduler_global_control(request, action="scheduler.pause", pause=True, authorization=authorization)

@project_operations_router.post("/v0/vnext/scheduler/resume")
def resume_vnext_scheduler(
    request: VNextSchedulerControlRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    return _vnext_scheduler_global_control(request, action="scheduler.resume", pause=False, authorization=authorization)

def _vnext_scheduler_global_control(
    request: VNextSchedulerControlRequest,
    *,
    action: str,
    pause: bool,
    authorization: str | None = None,
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action=action,
                project_scope=tuple(request.project_scope),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, _actor_id = _vnext_agent_actor(identity, fallback="user")
            service = VNextSchedulerService(store)
            payload = service.pause_all(actor_type=actor_type) if pause else service.resume_all(actor_type=actor_type)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextSchedulerValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(status_code=200, content=jsonable_encoder({**payload, "policy_decision": decision.to_record()}))

@project_operations_router.post("/v0/vnext/open-loops/extract")
def extract_vnext_open_loops(request: VNextProjectAutomationRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            loops = VNextProjectService(PostgresVNextStore(conn)).extract_open_loops(
                _vnext_project_automation_request(request)
            )
    except (ValueError, VNextProjectValidationError):
        return _vnext_public_error_response(status_code=400, detail="vNext open-loop extraction request is invalid")

    return JSONResponse(status_code=201, content=jsonable_encoder({"open_loops": loops, "created_count": len(loops)}))

@project_operations_router.post("/v0/vnext/open-loops/{loop_id}/review")
def review_vnext_open_loop(
    loop_id: str,
    request: VNextOpenLoopReviewRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()

    try:
        _vnext_agent_identity(request)
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store,
                request,
                user_id=request.user_id,
                authorization=authorization,
            )
            target = store.get_open_loop(loop_id)
            if target is None:
                return _vnext_public_error_response(status_code=404, detail="vNext open loop was not found")
            # Same ceiling as the MCP open-loop updates. Returning the 403
            # from inside the connection keeps the policy event committed.
            try:
                VNextMemoryCommitService(store).authorize_memory_action(
                    identity=identity,
                    action="open_loop.update",
                    memory=target,
                    target_type="open_loop",
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
            payload = VNextProjectService(store).review_open_loop(
                loop_id=loop_id,
                action=request.action,
                title=request.title,
                description=request.description,
                due_at=request.due_at,
                priority=request.priority,
                resolution_note=request.resolution_note,
            )
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    except VNextProjectValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext open-loop review request is invalid")

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))
