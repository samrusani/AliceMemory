from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field

from alicebot_api.config import Settings, get_settings
from alicebot_api.continuity_brief import (
    ContinuityBriefValidationError,
    compile_continuity_brief,
)
from alicebot_api.continuity_capture import (
    ContinuityCaptureNotFoundError,
    ContinuityCaptureValidationError,
    capture_continuity_candidates,
    capture_continuity_input,
    commit_continuity_captures,
    get_continuity_capture_detail,
    list_continuity_capture_inbox,
)
from alicebot_api.continuity_contradictions import (
    ContinuityContradictionNotFoundError,
    ContinuityContradictionValidationError,
    get_contradiction_case,
    list_contradiction_cases,
    resolve_contradiction_case,
    sync_contradictions,
)
from alicebot_api.continuity_evidence import (
    ContinuityEvidenceNotFoundError,
    build_continuity_explain,
    get_continuity_artifact_detail,
)
from alicebot_api.continuity_lifecycle import (
    ContinuityLifecycleNotFoundError,
    ContinuityLifecycleValidationError,
    get_continuity_lifecycle_state,
    list_continuity_lifecycle_state,
)
from alicebot_api.continuity_objects import ContinuityObjectValidationError
from alicebot_api.continuity_open_loops import (
    ContinuityOpenLoopNotFoundError,
    ContinuityOpenLoopValidationError,
    apply_continuity_open_loop_review_action,
    compile_continuity_daily_brief,
    compile_continuity_open_loop_dashboard,
    compile_continuity_weekly_review,
)
from alicebot_api.continuity_recall import (
    ContinuityRecallValidationError,
    RetrievalTraceNotFoundError,
    get_retrieval_trace,
    list_retrieval_runs,
    query_continuity_recall,
)
from alicebot_api.continuity_resumption import (
    ContinuityResumptionValidationError,
    compile_continuity_resumption_brief,
)
from alicebot_api.continuity_review import (
    ContinuityReviewNotFoundError,
    ContinuityReviewValidationError,
    apply_continuity_correction,
    get_continuity_review_detail,
    list_continuity_review_queue,
)
from alicebot_api.continuity_trust import list_trust_signals
from alicebot_api.contracts import (
    DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    DEFAULT_CONTINUITY_CAPTURE_LIMIT,
    DEFAULT_CONTINUITY_DAILY_BRIEF_LIMIT,
    DEFAULT_CONTINUITY_LIFECYCLE_LIMIT,
    DEFAULT_CONTINUITY_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RECALL_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    DEFAULT_CONTINUITY_REVIEW_LIMIT,
    DEFAULT_CONTINUITY_WEEKLY_REVIEW_LIMIT,
    DEFAULT_RETRIEVAL_RUN_LIST_LIMIT,
    DEFAULT_TEMPORAL_TIMELINE_LIMIT,
    DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
    MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    MAX_CONTINUITY_CAPTURE_LIMIT,
    MAX_CONTINUITY_DAILY_BRIEF_LIMIT,
    MAX_CONTINUITY_LIFECYCLE_LIMIT,
    MAX_CONTINUITY_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RECALL_LIMIT,
    MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_CONTINUITY_REVIEW_LIMIT,
    MAX_CONTINUITY_WEEKLY_REVIEW_LIMIT,
    MAX_RETRIEVAL_RUN_LIST_LIMIT,
    MAX_TEMPORAL_TIMELINE_LIMIT,
    MAX_TRUSTED_FACT_PROMOTION_LIMIT,
    ContinuityArtifactDetailResponse,
    ContinuityBriefRequestInput,
    ContinuityBriefResponse,
    ContinuityCaptureCandidatesInput,
    ContinuityCaptureCommitInput,
    ContinuityCaptureCreateInput,
    ContinuityCaptureExplicitSignal,
    ContinuityCorrectionInput,
    ContinuityDailyBriefRequestInput,
    ContinuityDailyBriefResponse,
    ContinuityExplainResponse,
    ContinuityLifecycleDetailResponse,
    ContinuityLifecycleListResponse,
    ContinuityLifecycleQueryInput,
    ContinuityOpenLoopDashboardQueryInput,
    ContinuityOpenLoopDashboardResponse,
    ContinuityOpenLoopReviewActionInput,
    ContinuityOpenLoopReviewActionResponse,
    ContinuityRecallQueryInput,
    ContinuityRecallResponse,
    ContinuityResumptionBriefRequestInput,
    ContinuityResumptionBriefResponse,
    ContinuityReviewDetailResponse,
    ContinuityReviewQueueQueryInput,
    ContinuityReviewQueueResponse,
    ContinuityWeeklyReviewRequestInput,
    ContinuityWeeklyReviewResponse,
    ContradictionCaseDetailResponse,
    ContradictionCaseListQueryInput,
    ContradictionCaseListResponse,
    ContradictionResolveInput,
    ContradictionResolveResponse,
    ContradictionSyncInput,
    ContradictionSyncResponse,
    MemoryOperationCommitInput,
    MemoryOperationGenerateInput,
    MemoryOperationListInput,
    PublicEvalRunDetailResponse,
    PublicEvalRunListResponse,
    PublicEvalSuiteDefinitionListResponse,
    RetrievalEvaluationResponse,
    RetrievalRunListResponse,
    RetrievalTraceResponse,
    TemporalExplainQueryInput,
    TemporalExplainResponse,
    TemporalStateAtQueryInput,
    TemporalStateAtResponse,
    TemporalTimelineQueryInput,
    TemporalTimelineResponse,
    TrustSignalListQueryInput,
    TrustSignalListResponse,
    TrustedFactPatternExplainResponse,
    TrustedFactPatternListQueryInput,
    TrustedFactPatternListResponse,
    TrustedFactPlaybookExplainResponse,
    TrustedFactPlaybookListQueryInput,
    TrustedFactPlaybookListResponse,
)
from alicebot_api.db import user_connection
from alicebot_api.memory_mutations import (
    MemoryMutationValidationError,
    commit_memory_operations,
    generate_memory_operation_candidates,
    list_memory_operation_candidates,
    list_memory_operations,
)
from alicebot_api.public_errors import public_exception_response
from alicebot_api.public_evals import (
    get_public_eval_run,
    list_public_eval_runs,
    list_public_eval_suites,
    run_public_evals,
)
from alicebot_api.retrieval_evaluation import get_retrieval_evaluation_summary
from alicebot_api.store import ContinuityStore
from alicebot_api.write_bounds import MAX_CAPTURE_COMMIT_CANDIDATES
from alicebot_api.task_briefing import TaskBriefValidationError
from alicebot_api.temporal_state import (
    TemporalStateNotFoundError,
    TemporalStateValidationError,
    get_temporal_explain,
    get_temporal_state_at,
    get_temporal_timeline,
)
from alicebot_api.trusted_fact_promotions import (
    TrustedFactPromotionNotFoundError,
    get_trusted_fact_pattern,
    get_trusted_fact_playbook,
    list_trusted_fact_patterns,
    list_trusted_fact_playbooks,
)

from alicebot_api.routers._api_shared import (
    LOGGER,
    _json_object,
    _request_client_identifier,
    _resolve_authenticated_v1_user_id,
)
from alicebot_api.routers._vnext_shared import BaseModel


capture_router = APIRouter()
operations_router = APIRouter()


class ContinuityCaptureRequest(BaseModel):
    user_id: UUID
    raw_content: str = Field(min_length=1, max_length=4000)
    explicit_signal: ContinuityCaptureExplicitSignal | None = None


class ContinuityCaptureCandidatesRequest(BaseModel):
    user_id: UUID
    user_content: str = Field(default="", max_length=4000)
    assistant_content: str = Field(default="", max_length=4000)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_kind: str = Field(default="sync_turn", min_length=1, max_length=80)


class ContinuityCaptureCommitRequest(BaseModel):
    user_id: UUID
    mode: str = Field(default="assist", min_length=1, max_length=20)
    # The service also bounds each candidate by serialized size (write_bounds).
    candidates: list[dict[str, object]] = Field(default_factory=list, max_length=MAX_CAPTURE_COMMIT_CANDIDATES)
    sync_fingerprint: str | None = Field(default=None, min_length=1, max_length=200)
    source_kind: str = Field(default="sync_turn", min_length=1, max_length=80)


class MemoryOperationGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_content: str = Field(default="", max_length=4000)
    assistant_content: str = Field(default="", max_length=4000)
    mode: str = Field(default="assist", min_length=1, max_length=20)
    sync_fingerprint: str | None = Field(default=None, min_length=1, max_length=200)
    source_kind: str = Field(default="sync_turn", min_length=1, max_length=80)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: UUID | None = None
    task_id: UUID | None = None
    project: str | None = Field(default=None, min_length=1, max_length=200)
    person: str | None = Field(default=None, min_length=1, max_length=200)
    target_continuity_object_id: UUID | None = None


class MemoryOperationCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[UUID] = Field(default_factory=list)
    sync_fingerprint: str | None = Field(default=None, min_length=1, max_length=200)
    include_review_required: bool = False


class ContinuityBriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_type: str = Field(default="general", min_length=1, max_length=40)
    query: str | None = Field(default=None, min_length=1, max_length=4000)
    thread_id: UUID | None = None
    task_id: UUID | None = None
    project: str | None = Field(default=None, min_length=1, max_length=200)
    person: str | None = Field(default=None, min_length=1, max_length=200)
    since: datetime | None = None
    until: datetime | None = None
    max_relevant_facts: int = Field(
        default=DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    )
    max_recent_changes: int = Field(
        default=DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    )
    max_open_loops: int = Field(
        default=DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    )
    max_conflicts: int = Field(
        default=DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    )
    max_timeline_highlights: int = Field(
        default=DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    )
    include_non_promotable_facts: bool = False


class ContinuityCorrectionRequest(BaseModel):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    body: dict[str, object] | None = None
    provenance: dict[str, object] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    replacement_title: str | None = Field(default=None, min_length=1, max_length=280)
    replacement_body: dict[str, object] | None = None
    replacement_provenance: dict[str, object] | None = None
    replacement_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ContradictionDetectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    continuity_object_id: UUID | None = None
    limit: int = Field(default=DEFAULT_CONTINUITY_REVIEW_LIMIT, ge=1, le=MAX_CONTINUITY_REVIEW_LIMIT)


class ContradictionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=60)
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class ContinuityOpenLoopReviewActionRequest(BaseModel):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, min_length=1, max_length=500)


def _allow_raw_evidence_debug_access(settings: Settings) -> bool:
    return settings.app_env in {"development", "test"}


def _audit_raw_evidence_access(
    *,
    request: Request,
    settings: Settings,
    route: str,
    user_id: UUID,
) -> None:
    LOGGER.info(
        "raw evidence content requested route=%s user_id=%s client=%s",
        route,
        user_id,
        _request_client_identifier(request, settings),
    )


@capture_router.post("/v0/continuity/captures")
def create_continuity_capture(request: ContinuityCaptureRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = capture_continuity_input(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ContinuityCaptureCreateInput(
                    raw_content=request.raw_content,
                    explicit_signal=request.explicit_signal,
                ),
            )
    except (ContinuityCaptureValidationError, ContinuityObjectValidationError) as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/continuity/captures/candidates")
def create_continuity_capture_candidates(request: ContinuityCaptureCandidatesRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = capture_continuity_candidates(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ContinuityCaptureCandidatesInput(
                    user_content=request.user_content,
                    assistant_content=request.assistant_content,
                    session_id=request.session_id,
                    source_kind=request.source_kind,
                ),
            )
    except ContinuityCaptureValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/continuity/captures/commit")
def commit_continuity_capture_candidates(request: ContinuityCaptureCommitRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = commit_continuity_captures(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ContinuityCaptureCommitInput(
                    mode=request.mode,  # type: ignore[arg-type]
                    candidates=[_json_object(candidate) for candidate in request.candidates],
                    sync_fingerprint=request.sync_fingerprint,
                    source_kind=request.source_kind,
                ),
            )
    except (ContinuityCaptureValidationError, ContinuityObjectValidationError) as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v1/memory/operations/candidates/generate")
def generate_memory_operation_candidates_endpoint(
    http_request: Request,
    request: MemoryOperationGenerateRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, http_request)
        with user_connection(settings.database_url, user_id) as conn:
            payload = generate_memory_operation_candidates(
                ContinuityStore(conn),
                user_id=user_id,
                request=MemoryOperationGenerateInput(
                    user_content=request.user_content,
                    assistant_content=request.assistant_content,
                    mode=request.mode,  # type: ignore[arg-type]
                    sync_fingerprint=request.sync_fingerprint,
                    source_kind=request.source_kind,
                    session_id=request.session_id,
                    thread_id=request.thread_id,
                    task_id=request.task_id,
                    project=request.project,
                    person=request.person,
                    target_continuity_object_id=request.target_continuity_object_id,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryMutationValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityCaptureValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v1/memory/operations/candidates")
def list_memory_operation_candidates_endpoint(
    request: Request,
    limit: int = Query(default=DEFAULT_CONTINUITY_CAPTURE_LIMIT, ge=1, le=100),
    policy_action: str | None = Query(default=None, min_length=1, max_length=40),
    operation_type: str | None = Query(default=None, min_length=1, max_length=40),
    sync_fingerprint: str | None = Query(default=None, min_length=1, max_length=200),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_memory_operation_candidates(
                ContinuityStore(conn),
                user_id=user_id,
                request=MemoryOperationListInput(
                    limit=limit,
                    policy_action=policy_action,  # type: ignore[arg-type]
                    operation_type=operation_type,  # type: ignore[arg-type]
                    sync_fingerprint=sync_fingerprint,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryMutationValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v1/memory/operations/commit")
def commit_memory_operations_endpoint(
    http_request: Request,
    request: MemoryOperationCommitRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, http_request)
        with user_connection(settings.database_url, user_id) as conn:
            payload = commit_memory_operations(
                ContinuityStore(conn),
                user_id=user_id,
                request=MemoryOperationCommitInput(
                    candidate_ids=request.candidate_ids,
                    sync_fingerprint=request.sync_fingerprint,
                    include_review_required=request.include_review_required,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryMutationValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v1/memory/operations")
def list_memory_operations_endpoint(
    request: Request,
    limit: int = Query(default=DEFAULT_CONTINUITY_CAPTURE_LIMIT, ge=1, le=100),
    sync_fingerprint: str | None = Query(default=None, min_length=1, max_length=200),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_memory_operations(
                ContinuityStore(conn),
                user_id=user_id,
                request=MemoryOperationListInput(
                    limit=limit,
                    sync_fingerprint=sync_fingerprint,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryMutationValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/captures")
def list_continuity_captures(
    user_id: UUID,
    limit: int = Query(default=DEFAULT_CONTINUITY_CAPTURE_LIMIT, ge=1, le=MAX_CONTINUITY_CAPTURE_LIMIT),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_continuity_capture_inbox(
            ContinuityStore(conn),
            user_id=user_id,
            limit=limit,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/captures/{capture_event_id}")
def get_continuity_capture(capture_event_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_continuity_capture_detail(
                ContinuityStore(conn),
                user_id=user_id,
                capture_event_id=capture_event_id,
            )
    except ContinuityCaptureNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/admin/debug/continuity/lifecycle")
def list_continuity_lifecycle_endpoint(
    user_id: UUID,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_LIFECYCLE_LIMIT,
        ge=1,
        le=MAX_CONTINUITY_LIFECYCLE_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityLifecycleListResponse = list_continuity_lifecycle_state(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityLifecycleQueryInput(limit=limit),
            )
    except ContinuityLifecycleValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/admin/debug/continuity/lifecycle/{continuity_object_id}")
def get_continuity_lifecycle_endpoint(
    continuity_object_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityLifecycleDetailResponse = get_continuity_lifecycle_state(
                ContinuityStore(conn),
                user_id=user_id,
                continuity_object_id=continuity_object_id,
            )
    except ContinuityLifecycleNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/review-queue")
def list_continuity_review_queue_endpoint(
    user_id: UUID,
    status: str = Query(default="correction_ready", min_length=1, max_length=40),
    limit: int = Query(
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        ge=1,
        le=MAX_CONTINUITY_REVIEW_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityReviewQueueResponse = list_continuity_review_queue(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityReviewQueueQueryInput(
                    status=status,  # type: ignore[arg-type]
                    limit=limit,
                ),
            )
    except ContinuityReviewValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/review-queue/{continuity_object_id}")
def get_continuity_review_detail_endpoint(
    continuity_object_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityReviewDetailResponse = get_continuity_review_detail(
                ContinuityStore(conn),
                user_id=user_id,
                continuity_object_id=continuity_object_id,
            )
    except ContinuityReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/explain/{continuity_object_id}")
def get_continuity_explain_endpoint(
    continuity_object_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityExplainResponse = build_continuity_explain(
                ContinuityStore(conn),
                user_id=user_id,
                continuity_object_id=continuity_object_id,
                include_raw_content=False,
            )
    except ContinuityEvidenceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v1/contradictions/detect")
def detect_contradictions_endpoint(
    http_request: Request,
    request: ContradictionDetectRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, http_request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContradictionSyncResponse = sync_contradictions(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContradictionSyncInput(
                    continuity_object_id=request.continuity_object_id,
                    limit=request.limit,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityContradictionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v1/contradictions/cases")
def list_contradiction_cases_endpoint(
    request: Request,
    status: str = Query(default="open", min_length=1, max_length=40),
    continuity_object_id: UUID | None = None,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        ge=1,
        le=MAX_CONTINUITY_REVIEW_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContradictionCaseListResponse = list_contradiction_cases(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContradictionCaseListQueryInput(
                    status=status,  # type: ignore[arg-type]
                    limit=limit,
                    continuity_object_id=continuity_object_id,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityContradictionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v1/contradictions/cases/{contradiction_case_id}")
def get_contradiction_case_endpoint(
    contradiction_case_id: UUID,
    request: Request,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContradictionCaseDetailResponse = get_contradiction_case(
                ContinuityStore(conn),
                user_id=user_id,
                contradiction_case_id=contradiction_case_id,
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityContradictionNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.post("/v1/contradictions/cases/{contradiction_case_id}/resolve")
def resolve_contradiction_case_endpoint(
    contradiction_case_id: UUID,
    http_request: Request,
    request: ContradictionResolveRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, http_request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContradictionResolveResponse = resolve_contradiction_case(
                ContinuityStore(conn),
                user_id=user_id,
                contradiction_case_id=contradiction_case_id,
                request=ContradictionResolveInput(
                    action=request.action,  # type: ignore[arg-type]
                    note=request.note,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityContradictionValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityContradictionNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v1/trust/signals")
def list_trust_signals_endpoint(
    request: Request,
    continuity_object_id: UUID | None = None,
    signal_state: str = Query(default="active", min_length=1, max_length=40),
    signal_type: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        ge=1,
        le=MAX_CONTINUITY_REVIEW_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: TrustSignalListResponse = list_trust_signals(
                ContinuityStore(conn),
                user_id=user_id,
                request=TrustSignalListQueryInput(
                    limit=limit,
                    continuity_object_id=continuity_object_id,
                    signal_state=signal_state,  # type: ignore[arg-type]
                    signal_type=signal_type,  # type: ignore[arg-type]
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/state-at")
def get_temporal_state_at_endpoint(
    entity_id: UUID,
    user_id: UUID,
    at: datetime | None = None,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: TemporalStateAtResponse = get_temporal_state_at(
                ContinuityStore(conn),
                user_id=user_id,
                request=TemporalStateAtQueryInput(
                    entity_id=entity_id,
                    at=at,
                ),
            )
    except (TemporalStateNotFoundError, TemporalStateValidationError) as exc:
        status_code = 404 if isinstance(exc, TemporalStateNotFoundError) else 400
        return public_exception_response(exc, status_code=status_code)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/timeline")
def get_temporal_timeline_endpoint(
    entity_id: UUID,
    user_id: UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=DEFAULT_TEMPORAL_TIMELINE_LIMIT, ge=1, le=MAX_TEMPORAL_TIMELINE_LIMIT),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: TemporalTimelineResponse = get_temporal_timeline(
                ContinuityStore(conn),
                user_id=user_id,
                request=TemporalTimelineQueryInput(
                    entity_id=entity_id,
                    since=since,
                    until=until,
                    limit=limit,
                ),
            )
    except (TemporalStateNotFoundError, TemporalStateValidationError) as exc:
        status_code = 404 if isinstance(exc, TemporalStateNotFoundError) else 400
        return public_exception_response(exc, status_code=status_code)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/explain")
def get_temporal_explain_endpoint(
    entity_id: UUID,
    user_id: UUID,
    at: datetime | None = None,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: TemporalExplainResponse = get_temporal_explain(
                ContinuityStore(conn),
                user_id=user_id,
                request=TemporalExplainQueryInput(
                    entity_id=entity_id,
                    at=at,
                ),
            )
    except (TemporalStateNotFoundError, TemporalStateValidationError) as exc:
        status_code = 404 if isinstance(exc, TemporalStateNotFoundError) else 400
        return public_exception_response(exc, status_code=status_code)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/patterns")
def list_trusted_fact_patterns_endpoint(
    user_id: UUID,
    limit: int = Query(
        default=DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
        ge=1,
        le=MAX_TRUSTED_FACT_PROMOTION_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: TrustedFactPatternListResponse = list_trusted_fact_patterns(
            ContinuityStore(conn),
            user_id=user_id,
            request=TrustedFactPatternListQueryInput(limit=limit),
        )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/patterns/{pattern_id}")
def get_trusted_fact_pattern_endpoint(
    pattern_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: TrustedFactPatternExplainResponse = get_trusted_fact_pattern(
                ContinuityStore(conn),
                user_id=user_id,
                pattern_id=pattern_id,
            )
    except TrustedFactPromotionNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/playbooks")
def list_trusted_fact_playbooks_endpoint(
    user_id: UUID,
    limit: int = Query(
        default=DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
        ge=1,
        le=MAX_TRUSTED_FACT_PROMOTION_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: TrustedFactPlaybookListResponse = list_trusted_fact_playbooks(
            ContinuityStore(conn),
            user_id=user_id,
            request=TrustedFactPlaybookListQueryInput(limit=limit),
        )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/playbooks/{playbook_id}")
def get_trusted_fact_playbook_endpoint(
    playbook_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: TrustedFactPlaybookExplainResponse = get_trusted_fact_playbook(
                ContinuityStore(conn),
                user_id=user_id,
                playbook_id=playbook_id,
            )
    except TrustedFactPromotionNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@operations_router.get("/v0/admin/debug/continuity/artifacts/{artifact_id}")
def get_continuity_artifact_detail_endpoint(
    request: Request,
    artifact_id: UUID,
    user_id: UUID,
    include_raw_content: bool = Query(default=False),
) -> JSONResponse:
    settings = get_settings()
    if include_raw_content and not _allow_raw_evidence_debug_access(settings):
        return JSONResponse(
            status_code=403,
            content={"detail": "raw evidence content access is restricted to development/test"},
        )

    if include_raw_content:
        _audit_raw_evidence_access(
            request=request,
            settings=settings,
            route="/v0/admin/debug/continuity/artifacts/{artifact_id}",
            user_id=user_id,
        )

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityArtifactDetailResponse = get_continuity_artifact_detail(
                ContinuityStore(conn),
                user_id=user_id,
                artifact_id=artifact_id,
                include_raw_content=include_raw_content,
            )
    except ContinuityEvidenceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/continuity/review-queue/{continuity_object_id}/corrections")
def apply_continuity_correction_endpoint(
    continuity_object_id: UUID,
    request: ContinuityCorrectionRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = apply_continuity_correction(
                ContinuityStore(conn),
                user_id=request.user_id,
                continuity_object_id=continuity_object_id,
                request=ContinuityCorrectionInput(
                    action=request.action,  # type: ignore[arg-type]
                    reason=request.reason,
                    title=request.title,
                    body=request.body,  # type: ignore[arg-type]
                    provenance=request.provenance,  # type: ignore[arg-type]
                    confidence=request.confidence,
                    replacement_title=request.replacement_title,
                    replacement_body=request.replacement_body,  # type: ignore[arg-type]
                    replacement_provenance=request.replacement_provenance,  # type: ignore[arg-type]
                    replacement_confidence=request.replacement_confidence,
                ),
            )
    except ContinuityReviewValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/open-loops")
def get_continuity_open_loop_dashboard(
    user_id: UUID,
    query_text: str | None = Query(default=None, alias="query", min_length=1, max_length=4000),
    thread_id: UUID | None = None,
    task_id: UUID | None = None,
    project: str | None = Query(default=None, min_length=1, max_length=200),
    person: str | None = Query(default=None, min_length=1, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_OPEN_LOOP_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_OPEN_LOOP_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityOpenLoopDashboardResponse = compile_continuity_open_loop_dashboard(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityOpenLoopDashboardQueryInput(
                    query=query_text,
                    thread_id=thread_id,
                    task_id=task_id,
                    project=project,
                    person=person,
                    since=since,
                    until=until,
                    limit=limit,
                ),
            )
    except ContinuityOpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/daily-brief")
def get_continuity_daily_brief(
    user_id: UUID,
    query_text: str | None = Query(default=None, alias="query", min_length=1, max_length=4000),
    thread_id: UUID | None = None,
    task_id: UUID | None = None,
    project: str | None = Query(default=None, min_length=1, max_length=200),
    person: str | None = Query(default=None, min_length=1, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_DAILY_BRIEF_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_DAILY_BRIEF_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityDailyBriefResponse = compile_continuity_daily_brief(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityDailyBriefRequestInput(
                    query=query_text,
                    thread_id=thread_id,
                    task_id=task_id,
                    project=project,
                    person=person,
                    since=since,
                    until=until,
                    limit=limit,
                ),
            )
    except ContinuityOpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/weekly-review")
def get_continuity_weekly_review(
    user_id: UUID,
    query_text: str | None = Query(default=None, alias="query", min_length=1, max_length=4000),
    thread_id: UUID | None = None,
    task_id: UUID | None = None,
    project: str | None = Query(default=None, min_length=1, max_length=200),
    person: str | None = Query(default=None, min_length=1, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_WEEKLY_REVIEW_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_WEEKLY_REVIEW_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityWeeklyReviewResponse = compile_continuity_weekly_review(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityWeeklyReviewRequestInput(
                    query=query_text,
                    thread_id=thread_id,
                    task_id=task_id,
                    project=project,
                    person=person,
                    since=since,
                    until=until,
                    limit=limit,
                ),
            )
    except ContinuityOpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v0/continuity/open-loops/{continuity_object_id}/review-action")
def apply_continuity_open_loop_review_action_endpoint(
    continuity_object_id: UUID,
    request: ContinuityOpenLoopReviewActionRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload: ContinuityOpenLoopReviewActionResponse = apply_continuity_open_loop_review_action(
                ContinuityStore(conn),
                user_id=request.user_id,
                continuity_object_id=continuity_object_id,
                request=ContinuityOpenLoopReviewActionInput(
                    action=request.action,  # type: ignore[arg-type]
                    note=request.note,
                ),
            )
    except ContinuityOpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityOpenLoopNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/recall")
def list_continuity_recall(
    user_id: UUID,
    query_text: str | None = Query(default=None, alias="query", min_length=1, max_length=4000),
    thread_id: UUID | None = None,
    task_id: UUID | None = None,
    project: str | None = Query(default=None, min_length=1, max_length=200),
    person: str | None = Query(default=None, min_length=1, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(
        default=DEFAULT_CONTINUITY_RECALL_LIMIT,
        ge=1,
        le=MAX_CONTINUITY_RECALL_LIMIT,
    ),
    debug: bool = False,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityRecallResponse = query_continuity_recall(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityRecallQueryInput(
                    query=query_text,
                    thread_id=thread_id,
                    task_id=task_id,
                    project=project,
                    person=person,
                    since=since,
                    until=until,
                    limit=limit,
                    debug=debug,
                ),
            )
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/retrieval-runs")
def get_continuity_retrieval_runs(
    user_id: UUID,
    limit: int = Query(
        default=DEFAULT_RETRIEVAL_RUN_LIST_LIMIT,
        ge=1,
        le=MAX_RETRIEVAL_RUN_LIST_LIMIT,
    ),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: RetrievalRunListResponse = list_retrieval_runs(
                ContinuityStore(conn),
                user_id=user_id,
                limit=limit,
            )
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/retrieval-runs/{retrieval_run_id}")
def get_continuity_retrieval_trace(
    retrieval_run_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: RetrievalTraceResponse = get_retrieval_trace(
                ContinuityStore(conn),
                user_id=user_id,
                retrieval_run_id=retrieval_run_id,
            )
    except RetrievalTraceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/retrieval-evaluation")
def get_continuity_retrieval_evaluation(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: RetrievalEvaluationResponse = get_retrieval_evaluation_summary(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v1/evals/suites")
def get_public_eval_suites(request: Request) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: PublicEvalSuiteDefinitionListResponse = list_public_eval_suites(
                ContinuityStore(conn),
                user_id=user_id,
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v1/evals/runs")
def create_public_eval_run(
    request: Request,
    suite_key: list[str] | None = Query(default=None),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: PublicEvalRunDetailResponse = run_public_evals(
                ContinuityStore(conn),
                user_id=user_id,
                suite_keys=suite_key,
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v1/evals/runs")
def get_public_eval_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: PublicEvalRunListResponse = list_public_eval_runs(
                ContinuityStore(conn),
                user_id=user_id,
                limit=limit,
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v1/evals/runs/{eval_run_id}")
def get_public_eval_run_detail(
    eval_run_id: UUID,
    request: Request,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: PublicEvalRunDetailResponse = get_public_eval_run(
                ContinuityStore(conn),
                user_id=user_id,
                eval_run_id=eval_run_id,
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.get("/v0/continuity/resumption-brief")
def get_continuity_resumption_brief(
    user_id: UUID,
    query_text: str | None = Query(default=None, alias="query", min_length=1, max_length=4000),
    thread_id: UUID | None = None,
    task_id: UUID | None = None,
    project: str | None = Query(default=None, min_length=1, max_length=200),
    person: str | None = Query(default=None, min_length=1, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    max_recent_changes: int = Query(
        default=DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    ),
    max_open_loops: int = Query(
        default=DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        ge=0,
        le=MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    ),
    include_non_promotable_facts: bool = False,
    debug: bool = False,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityResumptionBriefResponse = compile_continuity_resumption_brief(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityResumptionBriefRequestInput(
                    query=query_text,
                    thread_id=thread_id,
                    task_id=task_id,
                    project=project,
                    person=person,
                    since=since,
                    until=until,
                    max_recent_changes=max_recent_changes,
                    max_open_loops=max_open_loops,
                    include_non_promotable_facts=include_non_promotable_facts,
                    debug=debug,
                ),
            )
    except ContinuityResumptionValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except ContinuityRecallValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@operations_router.post("/v1/continuity/brief")
def post_continuity_brief(
    http_request: Request,
    request: ContinuityBriefRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, http_request)
        with user_connection(settings.database_url, user_id) as conn:
            payload: ContinuityBriefResponse = compile_continuity_brief(
                ContinuityStore(conn),
                user_id=user_id,
                request=ContinuityBriefRequestInput(
                    brief_type=request.brief_type,  # type: ignore[arg-type]
                    query=request.query,
                    thread_id=request.thread_id,
                    task_id=request.task_id,
                    project=request.project,
                    person=request.person,
                    since=request.since,
                    until=request.until,
                    max_relevant_facts=request.max_relevant_facts,
                    max_recent_changes=request.max_recent_changes,
                    max_open_loops=request.max_open_loops,
                    max_conflicts=request.max_conflicts,
                    max_timeline_highlights=request.max_timeline_highlights,
                    include_non_promotable_facts=request.include_non_promotable_facts,
                ),
            )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except (
        ContinuityBriefValidationError,
        ContinuityRecallValidationError,
        ContinuityResumptionValidationError,
        TaskBriefValidationError,
    ) as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )
