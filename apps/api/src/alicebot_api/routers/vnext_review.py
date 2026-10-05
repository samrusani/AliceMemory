from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from alicebot_api.config import get_settings
from alicebot_api.db import user_connection
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers._vnext_automation import (
    VNextProjectAutomationRequest,
    _vnext_bool,
    _vnext_float,
    _vnext_model_generation_options,
    _vnext_project_automation_request,
)
from alicebot_api.routers._vnext_embeddings import (
    _persist_vnext_deferred_embeddings,
)
from alicebot_api.routers._vnext_shared import (
    VNextAgentRequest,
    VNextDomain,
    VNextSensitivity,
    _vnext_agent_actor,
    _vnext_agent_auth_error_response,
    _vnext_agent_identity,
    _vnext_authenticated_agent_identity,
    _vnext_authorized_artifact,
    _vnext_int,
    _vnext_permission_response,
    _vnext_policy_checked,
    _vnext_public_error_response,
    _vnext_string_list,
)
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    PolicyDecision,
    agent_metadata,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    agent_key_from_authorization,
    resolve_protected_agent_identity,
)
from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
from alicebot_api.vnext_brain import (
    BrainArtifactRequest,
    VNextBrainService,
    VNextBrainValidationError,
)
from alicebot_api.vnext_connections import (
    ConnectionFinderRequest,
    VNextConnectionService,
    VNextConnectionValidationError,
)
from alicebot_api.vnext_contradictions import (
    ContradictionFinderRequest,
    VNextContradictionService,
    VNextContradictionValidationError,
)
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_projects import (
    PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE,
    VNextProjectService,
    VNextProjectTerminalConsistencyError,
    VNextProjectValidationError,
)
from alicebot_api.vnext_queue import (
    QueueTaskRequest,
    VNextQueueNotFoundError,
    VNextQueueService,
    VNextQueueValidationError,
)
from alicebot_api.vnext_store import PostgresVNextStore


insight_feedback_router = APIRouter()
review_router = APIRouter()


class VNextArtifactInsightFeedbackRequest(VNextAgentRequest):
    user_id: UUID
    useful_insight: str = Field(min_length=1, max_length=20)
    surfaced_missed: str | None = Field(default=None, min_length=1, max_length=20)
    comments: str | None = Field(default=None, min_length=1, max_length=4000)

class VNextBrainArtifactGenerateRequest(VNextAgentRequest):
    user_id: UUID
    scope: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)

class VNextConnectionReportGenerateRequest(VNextAgentRequest):
    user_id: UUID
    query: str = Field(default="", max_length=4000)
    scope: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)

class VNextContradictionReportGenerateRequest(VNextAgentRequest):
    user_id: UUID
    query: str = Field(default="", max_length=4000)
    scope: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)

class VNextProjectUpdateReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    edited_current_state: str | None = Field(default=None, min_length=1, max_length=4000)

class VNextQueueTaskCreateRequest(VNextAgentRequest):
    user_id: UUID
    title: str = Field(min_length=1, max_length=280)
    task_type: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=20_000)
    domain: VNextDomain = "unknown"
    sensitivity: VNextSensitivity = "unknown"
    write_policy: str = Field(default="proposal_only", min_length=1, max_length=80)
    scope_json: dict[str, object] = Field(default_factory=dict)
    allowed_sources_json: list[object] = Field(default_factory=list)

class VNextQueueProcessNextRequest(VNextAgentRequest):
    user_id: UUID

class VNextArtifactReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)

class VNextArtifactQualityRatingRequest(VNextAgentRequest):
    user_id: UUID
    reviewer_id: str | None = Field(default=None, min_length=1, max_length=120)
    usefulness: int | None = Field(default=None, ge=1, le=5)
    accuracy: int | None = Field(default=None, ge=1, le=5)
    source_grounding: int | None = Field(default=None, ge=1, le=5)
    novel_connections: int | None = Field(default=None, ge=1, le=5)
    actionability: int | None = Field(default=None, ge=1, le=5)
    hallucination_risk: int | None = Field(default=None, ge=1, le=5)
    verbosity: str = Field(default="unknown", min_length=1, max_length=40)
    missed_context: str | None = Field(default=None, min_length=1, max_length=4000)
    comments: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata_json: dict[str, object] = Field(default_factory=dict)

class VNextGraphEdgeReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)

class VNextBeliefReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    superseded_by: str | None = Field(default=None, min_length=1, max_length=120)

class VNextArtifactExportRequest(VNextAgentRequest):
    user_id: UUID
    output_dir: str = Field(min_length=1, max_length=1000)

def _vnext_brain_artifact_request(
    request: VNextBrainArtifactGenerateRequest,
    *,
    identity: AgentIdentity | None = None,
    decision: PolicyDecision | None = None,
) -> BrainArtifactRequest:
    scope = request.scope
    options = request.options
    generated_for = options.get("generated_for") or scope.get("generated_for")
    actor_type, actor_id = _vnext_agent_actor(identity, fallback="system")
    return BrainArtifactRequest(
        domains=decision.effective_domains if decision is not None else _vnext_string_list(scope, "domains"),
        projects=decision.effective_project_scope
        if decision is not None
        else tuple(request.project_scope) or _vnext_string_list(scope, "projects"),
        sensitivity_allowed=decision.effective_sensitivity_allowed
        if decision is not None
        else _vnext_string_list(options, "sensitivity_allowed") or ("public", "internal", "private", "unknown"),
        generated_for=str(generated_for) if isinstance(generated_for, str) else None,
        source_limit=_vnext_int(options, "source_limit", 8),
        memory_limit=_vnext_int(options, "memory_limit", 8),
        open_loop_limit=_vnext_int(options, "open_loop_limit", 8),
        artifact_limit=_vnext_int(options, "artifact_limit", 4),
        discover_open_loops=_vnext_bool(options, "discover_open_loops", True),
        create_candidate_memories=_vnext_bool(options, "create_candidate_memories", True),
        generated_by=actor_type,
        actor_id=actor_id,
        trace_id=request.trace_id,
        run_id=identity.agent_run_id if identity is not None else None,
        agent_identity=identity.to_record() if identity is not None else None,
        policy_decision=decision.to_record() if decision is not None else None,
        metadata_json=agent_metadata(identity, decision),
        **_vnext_model_generation_options(options),
    )

def _vnext_connection_request(
    request: VNextConnectionReportGenerateRequest,
    *,
    identity: AgentIdentity | None = None,
    decision: PolicyDecision | None = None,
) -> ConnectionFinderRequest:
    options = request.options
    actor_type, actor_id = _vnext_agent_actor(identity, fallback="system")
    return ConnectionFinderRequest(
        query=request.query,
        domains=decision.effective_domains if decision is not None else _vnext_string_list(request.scope, "domains"),
        projects=decision.effective_project_scope
        if decision is not None
        else tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
        sensitivity_allowed=decision.effective_sensitivity_allowed
        if decision is not None
        else _vnext_string_list(options, "sensitivity_allowed") or ("public", "internal", "private", "unknown"),
        max_connections=_vnext_int(options, "max_connections", 8),
        auto_accept_threshold=_vnext_float(options, "auto_accept_threshold"),
        generated_by=actor_type,
        actor_id=actor_id,
        trace_id=request.trace_id,
        run_id=identity.agent_run_id if identity is not None else None,
        agent_identity=identity.to_record() if identity is not None else None,
        policy_decision=decision.to_record() if decision is not None else None,
        metadata_json=agent_metadata(identity, decision),
        **_vnext_model_generation_options(options),
    )

def _vnext_contradiction_request(
    request: VNextContradictionReportGenerateRequest,
    *,
    identity: AgentIdentity | None = None,
    decision: PolicyDecision | None = None,
) -> ContradictionFinderRequest:
    options = request.options
    actor_type, actor_id = _vnext_agent_actor(identity, fallback="system")
    return ContradictionFinderRequest(
        query=request.query,
        domains=decision.effective_domains if decision is not None else _vnext_string_list(request.scope, "domains"),
        projects=decision.effective_project_scope
        if decision is not None
        else tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
        sensitivity_allowed=decision.effective_sensitivity_allowed
        if decision is not None
        else _vnext_string_list(options, "sensitivity_allowed") or ("public", "internal", "private", "unknown"),
        max_contradictions=_vnext_int(options, "max_contradictions", 8),
        generated_by=actor_type,
        actor_id=actor_id,
        trace_id=request.trace_id,
        run_id=identity.agent_run_id if identity is not None else None,
        agent_identity=identity.to_record() if identity is not None else None,
        policy_decision=decision.to_record() if decision is not None else None,
        metadata_json=agent_metadata(identity, decision),
        **_vnext_model_generation_options(options),
    )

@insight_feedback_router.post("/v0/vnext/artifacts/{artifact_id}/insight-feedback")
def record_vnext_artifact_insight_feedback(
    artifact_id: UUID,
    request: VNextArtifactInsightFeedbackRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            _artifact, _decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.feedback",
                for_update=True,
            )
            actor_type, actor_id = _vnext_agent_actor(identity)
            payload = VNextDogfoodingService(store).record_insight_feedback(
                artifact_id=str(artifact_id),
                useful_insight=request.useful_insight,
                surfaced_missed=request.surfaced_missed,
                comments=request.comments,
                actor_type=actor_type,
                actor_id=actor_id,
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    except ValueError:
        return _vnext_public_error_response(
            status_code=400, detail="vNext artifact insight feedback request is invalid"
        )
    return JSONResponse(status_code=201, content=jsonable_encoder(payload))

@review_router.post("/v0/vnext/artifacts/generate/daily-brief")
def generate_vnext_daily_brief(
    request: VNextBrainArtifactGenerateRequest,
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
            requested_domains = _vnext_string_list(request.scope, "domains")
            requested_sensitivity = _vnext_string_list(request.options, "sensitivity_allowed") or (
                "public",
                "internal",
                "private",
                "unknown",
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="artifact.generate",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            payload = VNextBrainService(store).generate_daily_brief(
                _vnext_brain_artifact_request(request, identity=identity, decision=decision)
            )
    except VNextBrainValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext daily brief request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/artifacts/generate/weekly-synthesis")
def generate_vnext_weekly_synthesis(
    request: VNextBrainArtifactGenerateRequest,
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
            requested_domains = _vnext_string_list(request.scope, "domains")
            requested_sensitivity = _vnext_string_list(request.options, "sensitivity_allowed") or (
                "public",
                "internal",
                "private",
                "unknown",
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="artifact.generate",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            payload = VNextBrainService(store).generate_weekly_synthesis(
                _vnext_brain_artifact_request(request, identity=identity, decision=decision)
            )
    except VNextBrainValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext weekly synthesis request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/artifacts/generate/connections")
def generate_vnext_connection_report(
    request: VNextConnectionReportGenerateRequest,
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
            requested_domains = _vnext_string_list(request.scope, "domains")
            requested_sensitivity = _vnext_string_list(request.options, "sensitivity_allowed") or (
                "public",
                "internal",
                "private",
                "unknown",
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="artifact.generate",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
                workflow_type="connection_report",
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            payload = VNextConnectionService(store).generate_connection_report(
                _vnext_connection_request(request, identity=identity, decision=decision)
            )
    except VNextConnectionValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext connection report request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/artifacts/generate/contradictions")
def generate_vnext_contradiction_report(
    request: VNextContradictionReportGenerateRequest,
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
            requested_domains = _vnext_string_list(request.scope, "domains")
            requested_sensitivity = _vnext_string_list(request.options, "sensitivity_allowed") or (
                "public",
                "internal",
                "private",
                "unknown",
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="artifact.generate",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
                workflow_type="contradiction_report",
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            payload = VNextContradictionService(store).generate_contradiction_report(
                _vnext_contradiction_request(request, identity=identity, decision=decision)
            )
    except VNextContradictionValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext contradiction report request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/queue/tasks")
def create_vnext_queue_task(
    request: VNextQueueTaskCreateRequest,
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
                action="queue_task.create",
                domains=(request.domain,),
                sensitivity_allowed=(request.sensitivity,),
                project_scope=tuple(request.project_scope),
                write_policy=request.write_policy,
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, actor_id = _vnext_agent_actor(identity, fallback="user")
            payload = VNextQueueService(store).enqueue_task(
                QueueTaskRequest(
                    title=request.title,
                    task_type=request.task_type,
                    instructions=request.instructions,
                    requested_by=identity.agent_id if identity is not None else "api",
                    scope_json=request.scope_json,
                    allowed_sources_json=request.allowed_sources_json,
                    domain=request.domain,
                    sensitivity=request.sensitivity,
                    write_policy=request.write_policy,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    trace_id=request.trace_id or decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                )
            )
    except VNextQueueValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext queue task request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/queue/process-next")
def process_next_vnext_queue_task(request: VNextQueueProcessNextRequest) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, request.user_id) as conn:
        payload = VNextQueueService(PostgresVNextStore(conn)).process_next_task().to_record()

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.get("/v0/vnext/artifacts")
def list_vnext_artifacts(
    user_id: UUID,
    artifact_type: str | None = None,
    limit: int = 30,
    project: str | None = None,
) -> JSONResponse:
    settings = get_settings()
    scope_projects = (project,) if isinstance(project, str) and project.strip() else ()

    with user_connection(settings.database_url, user_id) as conn:
        payload = PostgresVNextStore(conn).list_artifacts(
            artifact_type=artifact_type,
            limit=limit,
            scope_projects=scope_projects,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder({"items": payload, "count": len(payload), "order": ["created_at_desc", "id_desc"]}),
    )

@review_router.get("/v0/vnext/artifacts/{artifact_id}")
def get_vnext_artifact(
    artifact_id: UUID,
    user_id: UUID,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = resolve_protected_agent_identity(
                store,
                user_id=user_id,
                raw_key=agent_key_from_authorization(authorization),
                payload={},
            )
            payload, _decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.lookup",
                for_update=False,
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/artifacts/{artifact_id}/review")
def review_vnext_artifact(
    artifact_id: UUID,
    request: VNextArtifactReviewRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    review_result = None
    reviewer_actor_type = "user"
    reviewer_actor_id: str | None = None
    reviewer_trace_id: str | None = request.trace_id
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
            store.lock_graph_mutation()
            from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock

            acquire_exclusive_label_lock(store)
            _artifact, decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.review",
                for_update=True,
            )
            reviewer_actor_type, reviewer_actor_id = _vnext_agent_actor(identity, fallback="user")
            if reviewer_actor_id is None:
                reviewer_actor_id = str(request.user_id)
            reviewer_trace_id = request.trace_id or decision.trace_id
            review_result = dispatch_vnext_artifact_review(
                store,
                artifact_id=str(artifact_id),
                action=request.action,
                actor_type=reviewer_actor_type,
                actor_id=reviewer_actor_id,
                trace_id=reviewer_trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
            )
            payload = review_result.artifact
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except VNextProjectTerminalConsistencyError:
        return _vnext_public_error_response(
            status_code=409,
            detail=PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE,
        )
    except (VNextQueueValidationError, VNextProjectValidationError):
        return _vnext_public_error_response(status_code=400, detail="vNext artifact review request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    if review_result is not None and review_result.deferred_embedding_inputs:
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=review_result,
            actor_type=reviewer_actor_type,
            actor_id=reviewer_actor_id,
            trace_id=reviewer_trace_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/artifacts/{artifact_id}/quality-ratings")
def rate_vnext_artifact_quality(
    artifact_id: UUID,
    request: VNextArtifactQualityRatingRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    verbosity = request.verbosity.strip().casefold()
    if verbosity not in {"too_shallow", "right_sized", "too_verbose", "unknown"}:
        return _vnext_public_error_response(status_code=400, detail="vNext artifact quality verbosity is invalid")
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
            existing, decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.feedback",
                for_update=True,
            )
            actor_type, actor_id = _vnext_agent_actor(identity, fallback="user")
            reviewer_id = actor_id or str(request.user_id)
            if request.reviewer_id is not None and request.reviewer_id != reviewer_id:
                raise AgentKeyAuthenticationError(
                    "reviewer_id must match the authenticated reviewer identity",
                    status_code=403,
                )
            existing_metadata = existing.get("metadata_json")
            payload = store.create_artifact_quality_rating(
                {
                    "artifact_id": str(artifact_id),
                    "reviewer_id": reviewer_id,
                    "usefulness": request.usefulness,
                    "accuracy": request.accuracy,
                    "source_grounding": request.source_grounding,
                    "novel_connections": request.novel_connections,
                    "actionability": request.actionability,
                    "hallucination_risk": request.hallucination_risk,
                    "verbosity": verbosity,
                    "missed_context": request.missed_context,
                    "comments": request.comments,
                    "metadata_json": {
                        **request.metadata_json,
                        "artifact_type": existing.get("artifact_type"),
                        "generation_mode": existing_metadata.get("generation_mode")
                        if isinstance(existing_metadata, dict)
                        else None,
                        "agent_identity": identity.to_record() if identity is not None else None,
                        "policy_decision": decision.to_record(),
                    },
                },
                actor_type=actor_type,
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    except ValueError:
        return _vnext_public_error_response(
            status_code=400,
            detail="vNext artifact quality rating request is invalid",
        )

    return JSONResponse(status_code=201, content=jsonable_encoder(payload))

@review_router.get("/v0/vnext/quality-evals")
def list_vnext_quality_evals(user_id: UUID, artifact_id: UUID | None = None, limit: int = 100) -> JSONResponse:
    settings = get_settings()
    bounded_limit = max(1, min(limit, 200))
    with user_connection(settings.database_url, user_id) as conn:
        rows = PostgresVNextStore(conn).list_artifact_quality_ratings(
            artifact_id=str(artifact_id) if artifact_id is not None else None,
            limit=bounded_limit,
        )
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "items": rows,
                "count": len(rows),
                "order": ["created_at_desc", "id_desc"],
                "export": {
                    "format": "json",
                    "rating_fields": [
                        "usefulness",
                        "accuracy",
                        "source_grounding",
                        "novel_connections",
                        "actionability",
                        "hallucination_risk",
                        "verbosity",
                        "missed_context",
                    ],
                },
            }
        ),
    )

@review_router.post("/v0/vnext/artifacts/{artifact_id}/export")
def export_vnext_artifact(
    artifact_id: UUID,
    request: VNextArtifactExportRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()

    try:
        _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            _artifact, _decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.export",
                for_update=True,
            )
            output_path = VNextQueueService(store).export_artifact_markdown(
                artifact_id=str(artifact_id),
                output_dir=request.output_dir,
            )
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except VNextQueueValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext artifact export request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder({"artifact_id": str(artifact_id), "output_path": str(output_path)}),
    )

@review_router.post("/v0/vnext/graph/edges/{edge_id}/review")
def review_vnext_graph_edge(edge_id: str, request: VNextGraphEdgeReviewRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = VNextConnectionService(PostgresVNextStore(conn)).review_edge(
                edge_id=edge_id,
                action=request.action,
            )
    except VNextConnectionValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext graph edge review request is invalid")

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.get("/v0/vnext/graph/neighborhood/{target_id}")
def get_vnext_graph_neighborhood(target_id: str, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = VNextConnectionService(PostgresVNextStore(conn)).graph_neighborhood(target_id=target_id)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/beliefs/{belief_id}/review")
def review_vnext_belief(belief_id: str, request: VNextBeliefReviewRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = VNextContradictionService(PostgresVNextStore(conn)).review_belief(
                belief_id=belief_id,
                action=request.action,
                confidence=request.confidence,
                superseded_by=request.superseded_by,
            )
    except VNextContradictionValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext belief review request is invalid")

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.get("/v0/vnext/beliefs/{belief_id}/state")
def get_vnext_belief_state(belief_id: str, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = VNextContradictionService(PostgresVNextStore(conn)).belief_state(belief_id=belief_id)
    except VNextContradictionValidationError:
        return _vnext_public_error_response(status_code=404, detail="vNext belief was not found")

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@review_router.post("/v0/vnext/projects/update-candidates")
def generate_vnext_project_update_candidate(
    request: VNextProjectAutomationRequest,
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
            requested_domains = _vnext_string_list(request.scope, "domains")
            requested_sensitivity = _vnext_string_list(request.options, "sensitivity_allowed") or (
                "public",
                "internal",
                "private",
                "unknown",
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="artifact.generate",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(request.scope, "projects"),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            payload = VNextProjectService(store).generate_project_update_candidate(
                _vnext_project_automation_request(request, identity=identity, decision=decision)
            )
    except (ValueError, VNextProjectValidationError):
        return _vnext_public_error_response(status_code=400, detail="vNext project update request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(status_code=201, content=jsonable_encoder(payload))

@review_router.post("/v0/vnext/projects/update-candidates/{artifact_id}/review")
def review_vnext_project_update_candidate(
    artifact_id: str,
    request: VNextProjectUpdateReviewRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    reviewer_actor_type = "user"
    reviewer_actor_id: str | None = None
    reviewer_trace_id: str | None = request.trace_id

    try:
        _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            _artifact, decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=artifact_id,
                action="artifact.review",
                for_update=True,
            )
            reviewer_actor_type, reviewer_actor_id = _vnext_agent_actor(identity, fallback="user")
            if reviewer_actor_id is None:
                reviewer_actor_id = str(request.user_id)
            reviewer_trace_id = request.trace_id or decision.trace_id
            service = VNextProjectService(store, defer_embeddings=True)
            payload = service.review_project_update(
                artifact_id=artifact_id,
                action=request.action,
                edited_current_state=request.edited_current_state,
                actor_type=reviewer_actor_type,
                actor_id=reviewer_actor_id,
                trace_id=reviewer_trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
            )
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except VNextProjectTerminalConsistencyError:
        return _vnext_public_error_response(
            status_code=409,
            detail=PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE,
        )
    except VNextProjectValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext project update review request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    _persist_vnext_deferred_embeddings(
        database_url=settings.database_url,
        user_id=request.user_id,
        result=service,
        actor_type=reviewer_actor_type,
        actor_id=reviewer_actor_id,
        trace_id=reviewer_trace_id,
    )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))
