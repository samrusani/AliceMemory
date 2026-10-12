from __future__ import annotations

from datetime import datetime

from typing import (
    Callable,
    Literal,
)

from uuid import UUID

from fastapi import (
    APIRouter,
    Query,
)

from fastapi.encoders import jsonable_encoder

from pydantic import (
    ConfigDict,
    Field,
    model_validator,
)

from fastapi.responses import JSONResponse

from alicebot_api.http_json_types import BoundedJsonObject
from alicebot_api.config import get_settings

from alicebot_api.public_errors import public_exception_response

from alicebot_api.routers._api_shared import _json_object

from alicebot_api.routers._vnext_shared import BaseModel

from alicebot_api.contracts import (
    ApprovalApproveInput,
    ApprovalRejectInput,
    ApprovalRequestCreateInput,
    DEFAULT_CALENDAR_EVENT_LIST_LIMIT,
    MAX_CALENDAR_EVENT_LIST_LIMIT,
    MAX_TASK_BRIEF_TOKEN_BUDGET,
    TaskBriefComparisonResponse,
    TaskBriefCompileRequestInput,
    TaskBriefResponse,
    ExecutionBudgetCreateInput,
    ExecutionBudgetDeactivateInput,
    ExecutionBudgetSupersedeInput,
    CalendarAccountConnectInput,
    CalendarEventListInput,
    CalendarEventIngestInput,
    GmailAccountConnectInput,
    GmailMessageIngestInput,
    TOOL_METADATA_VERSION_V0,
    ApprovalStatus,
    ProxyExecutionStatus,
    ToolAllowlistEvaluationRequestInput,
    ProxyExecutionRequestInput,
    TaskArtifactRegisterInput,
    TaskScopedSemanticArtifactChunkRetrievalInput,
    TaskScopedArtifactChunkRetrievalInput,
    TaskStepKind,
    TaskStepLineageInput,
    TaskStepNextCreateInput,
    TaskStepOutcomeSnapshot,
    TaskStepStatus,
    TaskStepTransitionInput,
    TaskRunCancelInput,
    TaskRunCreateInput,
    TaskRunPauseInput,
    TaskRunResumeInput,
    TaskRunTickInput,
    TaskWorkspaceCreateInput,
    ToolRoutingDecision,
    ToolRoutingRequestInput,
    ToolRoutingRequestRecord,
    ToolCreateInput,
)

from alicebot_api.artifacts import (
    TaskArtifactAlreadyExistsError,
    TaskArtifactChunkRetrievalValidationError,
    TaskArtifactValidationError,
    register_task_artifact_record,
    retrieve_task_scoped_artifact_chunk_records,
)

from alicebot_api.approvals import (
    ApprovalNotFoundError,
    ApprovalResolutionConflictError,
    approve_approval_record,
    get_approval_record,
    list_approval_records,
    reject_approval_record,
    submit_approval_request,
)

from alicebot_api.db import user_connection

from alicebot_api.executions import (
    ToolExecutionNotFoundError,
    get_tool_execution_record,
    list_tool_execution_records,
)

from alicebot_api.tasks import (
    TaskNotFoundError,
    TaskStepApprovalLinkageError,
    TaskStepExecutionLinkageError,
    TaskStepLifecycleBoundaryError,
    TaskStepSequenceError,
    TaskStepNotFoundError,
    TaskStepTransitionError,
    create_next_task_step_record,
    get_task_record,
    get_task_step_record,
    list_task_records,
    list_task_step_records,
    transition_task_step_record,
)

from alicebot_api.task_runs import (
    TaskRunNotFoundError,
    TaskRunTransitionError,
    TaskRunValidationError,
    cancel_task_run_record,
    create_task_run_record,
    get_task_run_record,
    list_task_run_records,
    pause_task_run_record,
    resume_task_run_record,
    tick_task_run_record,
)

from alicebot_api.workspaces import (
    TaskWorkspaceAlreadyExistsError,
    TaskWorkspaceNotFoundError,
    TaskWorkspaceProvisioningError,
    create_task_workspace_record,
    get_task_workspace_record,
    list_task_workspace_records,
)

from alicebot_api.execution_budgets import (
    ExecutionBudgetLifecycleError,
    ExecutionBudgetNotFoundError,
    ExecutionBudgetValidationError,
    create_execution_budget_record,
    deactivate_execution_budget_record,
    get_execution_budget_record,
    list_execution_budget_records,
    supersede_execution_budget_record,
)

from alicebot_api.gmail import (
    GmailAccountAlreadyExistsError,
    GmailCredentialInvalidError,
    GmailCredentialNotFoundError,
    GmailCredentialPersistenceError,
    GmailCredentialRefreshError,
    GmailCredentialValidationError,
    GmailAccountNotFoundError,
    GmailMessageFetchError,
    GmailMessageNotFoundError,
    GmailMessageUnsupportedError,
    create_gmail_account_record,
    get_gmail_account_record,
    ingest_gmail_message_record,
    list_gmail_account_records,
)

from alicebot_api.calendar import (
    CalendarAccountAlreadyExistsError,
    CalendarAccountNotFoundError,
    CalendarCredentialInvalidError,
    CalendarCredentialNotFoundError,
    CalendarCredentialPersistenceError,
    CalendarCredentialValidationError,
    CalendarEventFetchError,
    CalendarEventListValidationError,
    CalendarEventNotFoundError,
    CalendarEventUnsupportedError,
    create_calendar_account_record,
    get_calendar_account_record,
    ingest_calendar_event_record,
    list_calendar_account_records,
    list_calendar_event_records,
)

from alicebot_api.calendar_secret_manager import build_calendar_secret_manager

from alicebot_api.gmail_secret_manager import build_gmail_secret_manager

from alicebot_api.continuity_recall import ContinuityRecallValidationError

from alicebot_api.continuity_resumption import ContinuityResumptionValidationError

from alicebot_api.tools import (
    ToolAllowlistValidationError,
    ToolNotFoundError,
    ToolRoutingValidationError,
    ToolValidationError,
    create_tool_record,
    evaluate_tool_allowlist,
    get_tool_record,
    list_tool_records,
    route_tool_invocation,
)

from alicebot_api.semantic_retrieval import (
    SemanticArtifactChunkRetrievalValidationError,
    retrieve_task_scoped_semantic_artifact_chunk_records,
)

from alicebot_api.task_briefing import (
    TaskBriefNotFoundError,
    TaskBriefValidationError,
    compare_task_briefs,
    compile_and_persist_task_brief,
    get_persisted_task_brief,
)

from alicebot_api.store import ContinuityStore
from alicebot_api.routers import memories_legacy


core_router = APIRouter()
task_artifact_retrieval_router = APIRouter()
task_artifact_semantic_router = APIRouter()
operations_router = APIRouter()
task_brief_router = APIRouter()


RetrieveSemanticArtifactChunksRequest = memories_legacy.RetrieveSemanticArtifactChunksRequest


class CreateToolRequest(BaseModel):
    user_id: UUID
    tool_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=100)
    metadata_version: Literal["tool_metadata_v0"] = TOOL_METADATA_VERSION_V0
    active: bool = True
    tags: list[str] = Field(default_factory=list)
    action_hints: list[str] = Field(default_factory=list, min_length=1)
    scope_hints: list[str] = Field(default_factory=list, min_length=1)
    domain_hints: list[str] = Field(default_factory=list)
    risk_hints: list[str] = Field(default_factory=list)
    metadata: BoundedJsonObject = Field(default_factory=dict)


class EvaluateToolAllowlistRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    risk_hint: str | None = Field(default=None, min_length=1, max_length=100)
    attributes: BoundedJsonObject = Field(default_factory=dict)


class RouteToolRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    tool_id: UUID
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    risk_hint: str | None = Field(default=None, min_length=1, max_length=100)
    attributes: BoundedJsonObject = Field(default_factory=dict)


class CreateApprovalRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    tool_id: UUID
    task_run_id: UUID | None = None
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    risk_hint: str | None = Field(default=None, min_length=1, max_length=100)
    attributes: BoundedJsonObject = Field(default_factory=dict)


class ResolveApprovalRequest(BaseModel):
    user_id: UUID


class ExecuteApprovedProxyRequest(BaseModel):
    user_id: UUID
    task_run_id: UUID | None = None


class ConnectGmailAccountRequest(BaseModel):
    user_id: UUID
    provider_account_id: str = Field(min_length=1, max_length=320)
    email_address: str = Field(min_length=1, max_length=320)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    scope: Literal["https://www.googleapis.com/auth/gmail.readonly"] = "https://www.googleapis.com/auth/gmail.readonly"
    access_token: str = Field(min_length=1, max_length=8000)
    refresh_token: str | None = Field(default=None, min_length=1, max_length=8000)
    client_id: str | None = Field(default=None, min_length=1, max_length=2000)
    client_secret: str | None = Field(default=None, min_length=1, max_length=8000)
    access_token_expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_refresh_bundle(self) -> ConnectGmailAccountRequest:
        refresh_bundle = (
            self.refresh_token,
            self.client_id,
            self.client_secret,
            self.access_token_expires_at,
        )
        if all(value is None for value in refresh_bundle):
            return self
        if any(value is None for value in refresh_bundle):
            raise ValueError(
                "gmail refresh credentials must include refresh_token, client_id, "
                "client_secret, and access_token_expires_at"
            )
        return self


class IngestGmailMessageRequest(BaseModel):
    user_id: UUID
    task_workspace_id: UUID


class ConnectCalendarAccountRequest(BaseModel):
    user_id: UUID
    provider_account_id: str = Field(min_length=1, max_length=320)
    email_address: str = Field(min_length=1, max_length=320)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    scope: Literal["https://www.googleapis.com/auth/calendar.readonly"] = (
        "https://www.googleapis.com/auth/calendar.readonly"
    )
    access_token: str = Field(min_length=1, max_length=8000)


class IngestCalendarEventRequest(BaseModel):
    user_id: UUID
    task_workspace_id: UUID


class CreateTaskWorkspaceRequest(BaseModel):
    user_id: UUID


class RegisterTaskArtifactRequest(BaseModel):
    user_id: UUID
    local_path: str = Field(min_length=1, max_length=4000)
    media_type_hint: str | None = Field(default=None, min_length=1, max_length=200)


RetrieveArtifactChunksRequest = memories_legacy.RetrieveArtifactChunksRequest


class TaskStepRequestSnapshot(BaseModel):
    thread_id: UUID
    tool_id: UUID
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    risk_hint: str | None = Field(default=None, min_length=1, max_length=100)
    attributes: BoundedJsonObject = Field(default_factory=dict)


class TaskStepOutcomeRequest(BaseModel):
    routing_decision: ToolRoutingDecision
    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    execution_id: UUID | None = None
    execution_status: ProxyExecutionStatus | None = None
    blocked_reason: str | None = Field(default=None, min_length=1, max_length=500)


class TaskStepLineageRequest(BaseModel):
    parent_step_id: UUID
    source_approval_id: UUID | None = None
    source_execution_id: UUID | None = None


class CreateNextTaskStepRequest(BaseModel):
    user_id: UUID
    kind: TaskStepKind = "governed_request"
    status: TaskStepStatus
    request: TaskStepRequestSnapshot
    outcome: TaskStepOutcomeRequest
    lineage: TaskStepLineageRequest


class TransitionTaskStepRequest(BaseModel):
    user_id: UUID
    status: TaskStepStatus
    outcome: TaskStepOutcomeRequest


def _task_step_request_record(
    request: TaskStepRequestSnapshot,
) -> ToolRoutingRequestRecord:
    return {
        "thread_id": str(request.thread_id),
        "tool_id": str(request.tool_id),
        "action": request.action,
        "scope": request.scope,
        "domain_hint": request.domain_hint,
        "risk_hint": request.risk_hint,
        "attributes": _json_object(request.attributes),
    }


def _task_step_outcome_snapshot(
    outcome: TaskStepOutcomeRequest,
) -> TaskStepOutcomeSnapshot:
    return {
        "routing_decision": outcome.routing_decision,
        "approval_id": str(outcome.approval_id) if outcome.approval_id is not None else None,
        "approval_status": outcome.approval_status,
        "execution_id": str(outcome.execution_id) if outcome.execution_id is not None else None,
        "execution_status": outcome.execution_status,
        "blocked_reason": outcome.blocked_reason,
    }


class CreateTaskRunRequest(BaseModel):
    user_id: UUID
    max_ticks: int = Field(default=1, ge=1, le=1_000_000)
    retry_cap: int | None = Field(default=None, ge=1, le=1_000_000)
    checkpoint: BoundedJsonObject = Field(default_factory=dict)


class MutateTaskRunRequest(BaseModel):
    user_id: UUID


class CreateExecutionBudgetRequest(BaseModel):
    user_id: UUID
    agent_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    tool_key: str | None = Field(default=None, min_length=1, max_length=200)
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    max_completed_executions: int = Field(ge=1, le=1000000)
    rolling_window_seconds: int | None = Field(default=None, ge=1)


class DeactivateExecutionBudgetRequest(BaseModel):
    user_id: UUID
    thread_id: UUID


class SupersedeExecutionBudgetRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    max_completed_executions: int = Field(ge=1, le=1000000)


class TaskBriefCompileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["user_recall", "resume", "worker_subtask", "agent_handoff"]
    query: str | None = Field(default=None, min_length=1, max_length=4000)
    thread_id: UUID | None = None
    task_id: UUID | None = None
    project: str | None = Field(default=None, min_length=1, max_length=200)
    person: str | None = Field(default=None, min_length=1, max_length=200)
    since: datetime | None = None
    until: datetime | None = None
    include_non_promotable_facts: bool = False
    provider_strategy: str | None = Field(default=None, min_length=1, max_length=80)
    briefing_strategy: Literal["balanced", "compact", "detailed"] | None = None
    token_budget: int | None = Field(default=None, ge=1, le=MAX_TASK_BRIEF_TOKEN_BUDGET)


class TaskBriefCompileRequest(TaskBriefCompileSpec):
    user_id: UUID


class TaskBriefCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    primary: TaskBriefCompileSpec
    secondary: TaskBriefCompileSpec


@core_router.post("/v0/tools")
def create_tool(request: CreateToolRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_tool_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                tool=ToolCreateInput(
                    tool_key=request.tool_key,
                    name=request.name,
                    description=request.description,
                    version=request.version,
                    metadata_version=request.metadata_version,
                    active=request.active,
                    tags=tuple(request.tags),
                    action_hints=tuple(request.action_hints),
                    scope_hints=tuple(request.scope_hints),
                    domain_hints=tuple(request.domain_hints),
                    risk_hints=tuple(request.risk_hints),
                    metadata=_json_object(request.metadata),
                ),
            )
    except ToolValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/tools")
def list_tools(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_tool_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/tools/allowlist/evaluate")
def evaluate_tools_allowlist(request: EvaluateToolAllowlistRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = evaluate_tool_allowlist(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ToolAllowlistEvaluationRequestInput(
                    thread_id=request.thread_id,
                    action=request.action,
                    scope=request.scope,
                    domain_hint=request.domain_hint,
                    risk_hint=request.risk_hint,
                    attributes=_json_object(request.attributes),
                ),
            )
    except ToolAllowlistValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/tools/route")
def route_tool(request: RouteToolRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = route_tool_invocation(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ToolRoutingRequestInput(
                    thread_id=request.thread_id,
                    tool_id=request.tool_id,
                    action=request.action,
                    scope=request.scope,
                    domain_hint=request.domain_hint,
                    risk_hint=request.risk_hint,
                    attributes=_json_object(request.attributes),
                ),
            )
    except ToolRoutingValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/approvals/requests")
def create_approval_request(request: CreateApprovalRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = submit_approval_request(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ApprovalRequestCreateInput(
                    thread_id=request.thread_id,
                    tool_id=request.tool_id,
                    task_run_id=request.task_run_id,
                    action=request.action,
                    scope=request.scope,
                    domain_hint=request.domain_hint,
                    risk_hint=request.risk_hint,
                    attributes=_json_object(request.attributes),
                ),
            )
    except ToolRoutingValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/approvals")
def list_approvals(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_approval_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/approvals/{approval_id}")
def get_approval(approval_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_approval_record(
                ContinuityStore(conn),
                user_id=user_id,
                approval_id=approval_id,
            )
    except ApprovalNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/approvals/{approval_id}/approve")
def approve_approval(approval_id: UUID, request: ResolveApprovalRequest) -> JSONResponse:
    settings = get_settings()
    resolution_error: (
        ApprovalResolutionConflictError | TaskStepApprovalLinkageError | TaskStepLifecycleBoundaryError | None
    ) = None

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            try:
                payload = approve_approval_record(
                    ContinuityStore(conn),
                    user_id=request.user_id,
                    request=ApprovalApproveInput(approval_id=approval_id),
                )
            except (
                ApprovalResolutionConflictError,
                TaskStepApprovalLinkageError,
                TaskStepLifecycleBoundaryError,
            ) as exc:
                resolution_error = exc
                payload = None
    except ApprovalNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    if resolution_error is not None:
        return public_exception_response(resolution_error, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/approvals/{approval_id}/reject")
def reject_approval(approval_id: UUID, request: ResolveApprovalRequest) -> JSONResponse:
    settings = get_settings()
    resolution_error: (
        ApprovalResolutionConflictError | TaskStepApprovalLinkageError | TaskStepLifecycleBoundaryError | None
    ) = None

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            try:
                payload = reject_approval_record(
                    ContinuityStore(conn),
                    user_id=request.user_id,
                    request=ApprovalRejectInput(approval_id=approval_id),
                )
            except (
                ApprovalResolutionConflictError,
                TaskStepApprovalLinkageError,
                TaskStepLifecycleBoundaryError,
            ) as exc:
                resolution_error = exc
                payload = None
    except ApprovalNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    if resolution_error is not None:
        return public_exception_response(resolution_error, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/approvals/{approval_id}/execute")
def execute_approved_proxy(approval_id: UUID, request: ExecuteApprovedProxyRequest) -> JSONResponse:
    from alicebot_api import proxy_execution

    settings = get_settings()
    execution_error: Exception | None = None

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            try:
                payload = proxy_execution.execute_approved_proxy_request(
                    ContinuityStore(conn),
                    user_id=request.user_id,
                    request=ProxyExecutionRequestInput(
                        approval_id=approval_id,
                        task_run_id=request.task_run_id,
                    ),
                )
            except (
                proxy_execution.ProxyExecutionApprovalStateError,
                proxy_execution.ProxyExecutionHandlerNotFoundError,
                proxy_execution.ProxyExecutionIdempotencyError,
                TaskStepApprovalLinkageError,
                TaskStepExecutionLinkageError,
            ) as exc:
                execution_error = exc
                payload = None
    except ApprovalNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    if execution_error is not None:
        return public_exception_response(execution_error, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/tasks")
def list_tasks(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_task_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/tasks/{task_id}")
def get_task(task_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_id=task_id,
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/tasks/{task_id}/runs")
def create_task_run(task_id: UUID, request: CreateTaskRunRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_task_run_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskRunCreateInput(
                    task_id=task_id,
                    max_ticks=request.max_ticks,
                    retry_cap=request.retry_cap,
                    checkpoint=_json_object(request.checkpoint),
                ),
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskRunValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/tasks/{task_id}/runs")
def list_task_runs(task_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_task_run_records(
                ContinuityStore(conn),
                user_id=user_id,
                task_id=task_id,
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/task-runs/{task_run_id}")
def get_task_run(task_run_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_run_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_run_id=task_run_id,
            )
    except TaskRunNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


def _mutate_task_run(
    *,
    task_run_id: UUID,
    request: MutateTaskRunRequest,
    mutation_handler: Callable[..., object],
    mutation_input_model: type[TaskRunTickInput]
    | type[TaskRunPauseInput]
    | type[TaskRunResumeInput]
    | type[TaskRunCancelInput],
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = mutation_handler(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=mutation_input_model(task_run_id=task_run_id),
            )
    except TaskRunValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except TaskRunNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskRunTransitionError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/task-runs/{task_run_id}/tick")
def tick_task_run(task_run_id: UUID, request: MutateTaskRunRequest) -> JSONResponse:
    return _mutate_task_run(
        task_run_id=task_run_id,
        request=request,
        mutation_handler=tick_task_run_record,
        mutation_input_model=TaskRunTickInput,
    )


@core_router.post("/v0/task-runs/{task_run_id}/pause")
def pause_task_run(task_run_id: UUID, request: MutateTaskRunRequest) -> JSONResponse:
    return _mutate_task_run(
        task_run_id=task_run_id,
        request=request,
        mutation_handler=pause_task_run_record,
        mutation_input_model=TaskRunPauseInput,
    )


@core_router.post("/v0/task-runs/{task_run_id}/resume")
def resume_task_run(task_run_id: UUID, request: MutateTaskRunRequest) -> JSONResponse:
    return _mutate_task_run(
        task_run_id=task_run_id,
        request=request,
        mutation_handler=resume_task_run_record,
        mutation_input_model=TaskRunResumeInput,
    )


@core_router.post("/v0/task-runs/{task_run_id}/cancel")
def cancel_task_run(task_run_id: UUID, request: MutateTaskRunRequest) -> JSONResponse:
    return _mutate_task_run(
        task_run_id=task_run_id,
        request=request,
        mutation_handler=cancel_task_run_record,
        mutation_input_model=TaskRunCancelInput,
    )


@core_router.post("/v0/gmail-accounts")
def connect_gmail_account(request: ConnectGmailAccountRequest) -> JSONResponse:
    settings = get_settings()
    secret_manager = build_gmail_secret_manager(settings.gmail_secret_manager_url)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_gmail_account_record(
                ContinuityStore(conn),
                secret_manager,
                user_id=request.user_id,
                request=GmailAccountConnectInput(
                    provider_account_id=request.provider_account_id,
                    email_address=request.email_address,
                    display_name=request.display_name,
                    scope=request.scope,
                    access_token=request.access_token,
                    refresh_token=request.refresh_token,
                    client_id=request.client_id,
                    client_secret=request.client_secret,
                    access_token_expires_at=request.access_token_expires_at,
                ),
            )
    except GmailCredentialValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except GmailCredentialPersistenceError as exc:
        return public_exception_response(exc, status_code=409)
    except GmailAccountAlreadyExistsError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/gmail-accounts")
def list_gmail_accounts(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_gmail_account_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/gmail-accounts/{gmail_account_id}")
def get_gmail_account(gmail_account_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_gmail_account_record(
                ContinuityStore(conn),
                user_id=user_id,
                gmail_account_id=gmail_account_id,
            )
    except GmailAccountNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest")
def ingest_gmail_message(
    gmail_account_id: UUID,
    provider_message_id: str,
    request: IngestGmailMessageRequest,
) -> JSONResponse:
    settings = get_settings()
    secret_manager = build_gmail_secret_manager(settings.gmail_secret_manager_url)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = ingest_gmail_message_record(
                ContinuityStore(conn),
                secret_manager,
                user_id=request.user_id,
                request=GmailMessageIngestInput(
                    gmail_account_id=gmail_account_id,
                    task_workspace_id=request.task_workspace_id,
                    provider_message_id=provider_message_id,
                ),
            )
    except GmailAccountNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskWorkspaceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except GmailMessageNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except GmailMessageUnsupportedError as exc:
        return public_exception_response(exc, status_code=400)
    except (
        GmailCredentialNotFoundError,
        GmailCredentialInvalidError,
        GmailCredentialPersistenceError,
    ) as exc:
        return public_exception_response(exc, status_code=409)
    except TaskArtifactValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except (GmailMessageFetchError, GmailCredentialRefreshError) as exc:
        return public_exception_response(exc, status_code=502)
    except TaskArtifactAlreadyExistsError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/calendar-accounts")
def connect_calendar_account(request: ConnectCalendarAccountRequest) -> JSONResponse:
    settings = get_settings()
    secret_manager = build_calendar_secret_manager(settings.calendar_secret_manager_url)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_calendar_account_record(
                ContinuityStore(conn),
                secret_manager,
                user_id=request.user_id,
                request=CalendarAccountConnectInput(
                    provider_account_id=request.provider_account_id,
                    email_address=request.email_address,
                    display_name=request.display_name,
                    scope=request.scope,
                    access_token=request.access_token,
                ),
            )
    except CalendarCredentialValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except CalendarCredentialPersistenceError as exc:
        return public_exception_response(exc, status_code=409)
    except CalendarAccountAlreadyExistsError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/calendar-accounts")
def list_calendar_accounts(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_calendar_account_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/calendar-accounts/{calendar_account_id}")
def get_calendar_account(calendar_account_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_calendar_account_record(
                ContinuityStore(conn),
                user_id=user_id,
                calendar_account_id=calendar_account_id,
            )
    except CalendarAccountNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/calendar-accounts/{calendar_account_id}/events")
def list_calendar_events(
    calendar_account_id: UUID,
    user_id: UUID,
    limit: int = Query(default=DEFAULT_CALENDAR_EVENT_LIST_LIMIT, ge=1, le=MAX_CALENDAR_EVENT_LIST_LIMIT),
    time_min: datetime | None = Query(default=None),
    time_max: datetime | None = Query(default=None),
) -> JSONResponse:
    settings = get_settings()
    secret_manager = build_calendar_secret_manager(settings.calendar_secret_manager_url)

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_calendar_event_records(
                ContinuityStore(conn),
                secret_manager,
                user_id=user_id,
                request=CalendarEventListInput(
                    calendar_account_id=calendar_account_id,
                    limit=limit,
                    time_min=time_min,
                    time_max=time_max,
                ),
            )
    except CalendarAccountNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except (
        CalendarCredentialNotFoundError,
        CalendarCredentialInvalidError,
        CalendarCredentialPersistenceError,
    ) as exc:
        return public_exception_response(exc, status_code=409)
    except CalendarEventListValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except CalendarEventFetchError as exc:
        return public_exception_response(exc, status_code=502)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest")
def ingest_calendar_event(
    calendar_account_id: UUID,
    provider_event_id: str,
    request: IngestCalendarEventRequest,
) -> JSONResponse:
    settings = get_settings()
    secret_manager = build_calendar_secret_manager(settings.calendar_secret_manager_url)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = ingest_calendar_event_record(
                ContinuityStore(conn),
                secret_manager,
                user_id=request.user_id,
                request=CalendarEventIngestInput(
                    calendar_account_id=calendar_account_id,
                    task_workspace_id=request.task_workspace_id,
                    provider_event_id=provider_event_id,
                ),
            )
    except CalendarAccountNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskWorkspaceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except CalendarEventNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except CalendarEventUnsupportedError as exc:
        return public_exception_response(exc, status_code=400)
    except (
        CalendarCredentialNotFoundError,
        CalendarCredentialInvalidError,
        CalendarCredentialPersistenceError,
    ) as exc:
        return public_exception_response(exc, status_code=409)
    except TaskArtifactValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except CalendarEventFetchError as exc:
        return public_exception_response(exc, status_code=502)
    except TaskArtifactAlreadyExistsError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/tasks/{task_id}/workspace")
def create_task_workspace(task_id: UUID, request: CreateTaskWorkspaceRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_task_workspace_record(
                ContinuityStore(conn),
                settings=settings,
                user_id=request.user_id,
                request=TaskWorkspaceCreateInput(
                    task_id=task_id,
                    status="active",
                ),
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except (TaskWorkspaceAlreadyExistsError, TaskWorkspaceProvisioningError) as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/task-workspaces")
def list_task_workspaces(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_task_workspace_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/task-workspaces/{task_workspace_id}")
def get_task_workspace(task_workspace_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_workspace_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_workspace_id=task_workspace_id,
            )
    except TaskWorkspaceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/tasks/{task_id}/steps")
def list_task_steps(task_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_task_step_records(
                ContinuityStore(conn),
                user_id=user_id,
                task_id=task_id,
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.get("/v0/task-steps/{task_step_id}")
def get_task_step(task_step_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_step_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_step_id=task_step_id,
            )
    except TaskStepNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@core_router.post("/v0/task-workspaces/{task_workspace_id}/artifacts")
def register_task_artifact(
    task_workspace_id: UUID,
    request: RegisterTaskArtifactRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = register_task_artifact_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskArtifactRegisterInput(
                    task_workspace_id=task_workspace_id,
                    local_path=request.local_path,
                    media_type_hint=request.media_type_hint,
                ),
            )
    except TaskWorkspaceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskArtifactValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except TaskArtifactAlreadyExistsError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@task_artifact_retrieval_router.post("/v0/tasks/{task_id}/artifact-chunks/retrieve")
def retrieve_task_artifact_chunks(
    task_id: UUID,
    request: RetrieveArtifactChunksRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = retrieve_task_scoped_artifact_chunk_records(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskScopedArtifactChunkRetrievalInput(
                    task_id=task_id,
                    query=request.query,
                ),
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@task_artifact_semantic_router.post("/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval")
def retrieve_semantic_task_artifact_chunks(
    task_id: UUID,
    request: RetrieveSemanticArtifactChunksRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = retrieve_task_scoped_semantic_artifact_chunk_records(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskScopedSemanticArtifactChunkRetrievalInput(
                    task_id=task_id,
                    embedding_config_id=request.embedding_config_id,
                    query_vector=tuple(request.query_vector),
                    limit=request.limit,
                ),
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except SemanticArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/tasks/{task_id}/steps")
def create_next_task_step(task_id: UUID, request: CreateNextTaskStepRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_next_task_step_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskStepNextCreateInput(
                    task_id=task_id,
                    kind=request.kind,
                    status=request.status,
                    request=_task_step_request_record(request.request),
                    outcome=_task_step_outcome_snapshot(request.outcome),
                    lineage=TaskStepLineageInput(
                        parent_step_id=request.lineage.parent_step_id,
                        source_approval_id=request.lineage.source_approval_id,
                        source_execution_id=request.lineage.source_execution_id,
                    ),
                ),
            )
    except TaskNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskStepSequenceError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/task-steps/{task_step_id}/transition")
def transition_task_step(task_step_id: UUID, request: TransitionTaskStepRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = transition_task_step_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskStepTransitionInput(
                    task_step_id=task_step_id,
                    status=request.status,
                    outcome=_task_step_outcome_snapshot(request.outcome),
                ),
            )
    except TaskStepNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskStepTransitionError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/execution-budgets")
def create_execution_budget(request: CreateExecutionBudgetRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_execution_budget_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ExecutionBudgetCreateInput(
                    agent_profile_id=request.agent_profile_id,
                    tool_key=request.tool_key,
                    domain_hint=request.domain_hint,
                    max_completed_executions=request.max_completed_executions,
                    rolling_window_seconds=request.rolling_window_seconds,
                ),
            )
    except ExecutionBudgetValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/execution-budgets")
def list_execution_budgets(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_execution_budget_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/execution-budgets/{execution_budget_id}")
def get_execution_budget(execution_budget_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_execution_budget_record(
                ContinuityStore(conn),
                user_id=user_id,
                execution_budget_id=execution_budget_id,
            )
    except ExecutionBudgetNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/execution-budgets/{execution_budget_id}/deactivate")
def deactivate_execution_budget(
    execution_budget_id: UUID,
    request: DeactivateExecutionBudgetRequest,
) -> JSONResponse:
    settings = get_settings()
    lifecycle_error: ExecutionBudgetLifecycleError | None = None

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            try:
                payload = deactivate_execution_budget_record(
                    ContinuityStore(conn),
                    user_id=request.user_id,
                    request=ExecutionBudgetDeactivateInput(
                        thread_id=request.thread_id,
                        execution_budget_id=execution_budget_id,
                    ),
                )
            except ExecutionBudgetLifecycleError as exc:
                lifecycle_error = exc
                payload = None
    except ExecutionBudgetValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ExecutionBudgetNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    if lifecycle_error is not None:
        return public_exception_response(lifecycle_error, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/execution-budgets/{execution_budget_id}/supersede")
def supersede_execution_budget(
    execution_budget_id: UUID,
    request: SupersedeExecutionBudgetRequest,
) -> JSONResponse:
    settings = get_settings()
    lifecycle_error: ExecutionBudgetLifecycleError | None = None

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            try:
                payload = supersede_execution_budget_record(
                    ContinuityStore(conn),
                    user_id=request.user_id,
                    request=ExecutionBudgetSupersedeInput(
                        thread_id=request.thread_id,
                        execution_budget_id=execution_budget_id,
                        max_completed_executions=request.max_completed_executions,
                    ),
                )
            except ExecutionBudgetLifecycleError as exc:
                lifecycle_error = exc
                payload = None
    except ExecutionBudgetValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ExecutionBudgetNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    if lifecycle_error is not None:
        return public_exception_response(lifecycle_error, status_code=409)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/tool-executions")
def list_tool_executions(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_tool_execution_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/tool-executions/{execution_id}")
def get_tool_execution(execution_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_tool_execution_record(
                ContinuityStore(conn),
                user_id=user_id,
                execution_id=execution_id,
            )
    except ToolExecutionNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/tools/{tool_id}")
def get_tool(tool_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_tool_record(
                ContinuityStore(conn),
                user_id=user_id,
                tool_id=tool_id,
            )
    except ToolNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@task_brief_router.post("/v0/task-briefs/compile")
def post_v0_task_brief_compile(body: TaskBriefCompileRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, body.user_id) as conn:
            payload: TaskBriefResponse = compile_and_persist_task_brief(
                ContinuityStore(conn),
                user_id=body.user_id,
                request=TaskBriefCompileRequestInput(
                    mode=body.mode,
                    query=body.query,
                    thread_id=body.thread_id,
                    task_id=body.task_id,
                    project=body.project,
                    person=body.person,
                    since=body.since,
                    until=body.until,
                    include_non_promotable_facts=body.include_non_promotable_facts,
                    provider_strategy=body.provider_strategy,
                    briefing_strategy=body.briefing_strategy,
                    token_budget=body.token_budget,
                ),
            )
    except (
        TaskBriefValidationError,
        ContinuityRecallValidationError,
        ContinuityResumptionValidationError,
    ) as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@task_brief_router.get("/v0/task-briefs/{task_brief_id}")
def get_v0_task_brief(task_brief_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_persisted_task_brief(
                ContinuityStore(conn),
                task_brief_id=task_brief_id,
            )
    except TaskBriefNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@task_brief_router.post("/v0/task-briefs/compare")
def post_v0_task_brief_compare(body: TaskBriefCompareRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, body.user_id) as conn:
            payload: TaskBriefComparisonResponse = compare_task_briefs(
                ContinuityStore(conn),
                user_id=body.user_id,
                primary_request=TaskBriefCompileRequestInput(
                    mode=body.primary.mode,
                    query=body.primary.query,
                    thread_id=body.primary.thread_id,
                    task_id=body.primary.task_id,
                    project=body.primary.project,
                    person=body.primary.person,
                    since=body.primary.since,
                    until=body.primary.until,
                    include_non_promotable_facts=body.primary.include_non_promotable_facts,
                    provider_strategy=body.primary.provider_strategy,
                    briefing_strategy=body.primary.briefing_strategy,
                    token_budget=body.primary.token_budget,
                ),
                secondary_request=TaskBriefCompileRequestInput(
                    mode=body.secondary.mode,
                    query=body.secondary.query,
                    thread_id=body.secondary.thread_id,
                    task_id=body.secondary.task_id,
                    project=body.secondary.project,
                    person=body.secondary.person,
                    since=body.secondary.since,
                    until=body.secondary.until,
                    include_non_promotable_facts=body.secondary.include_non_promotable_facts,
                    provider_strategy=body.secondary.provider_strategy,
                    briefing_strategy=body.secondary.briefing_strategy,
                    token_budget=body.secondary.token_budget,
                ),
            )
    except (
        TaskBriefValidationError,
        ContinuityRecallValidationError,
        ContinuityResumptionValidationError,
    ) as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )
