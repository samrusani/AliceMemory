from __future__ import annotations

from datetime import datetime
from typing import (
    Annotated,
    Literal,
)
from uuid import UUID
from fastapi import Query
from fastapi.encoders import jsonable_encoder
from pydantic import (
    ConfigDict,
    Field,
    model_validator,
)
from fastapi.responses import JSONResponse
import psycopg
from psycopg.rows import dict_row
from alicebot_api.http_json_types import BoundedJsonObject, BoundedJsonValue
from alicebot_api.compiler import (
    compile_and_persist_trace,
    compile_resumption_brief,
)
from alicebot_api.config import get_settings
from alicebot_api.public_errors import public_exception_response
from alicebot_api.contracts import (
    AGENT_PROFILE_LIST_ORDER,
    AgentProfileListResponse,
    AgentProfileListSummary,
    ArtifactScopedSemanticArtifactChunkRetrievalInput,
    CompileContextArtifactScopedSemanticArtifactRetrievalInput,
    CompileContextArtifactScopedArtifactRetrievalInput,
    CompileContextTaskScopedArtifactRetrievalInput,
    CompileContextTaskScopedSemanticArtifactRetrievalInput,
    ConsentStatus,
    ConsentUpsertInput,
    CompileContextSemanticRetrievalInput,
    DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    DEFAULT_AGENT_PROFILE_ID,
    DEFAULT_MAX_EVENTS,
    DEFAULT_MAX_ENTITY_EDGES,
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_MEMORIES,
    DEFAULT_MEMORY_REVIEW_LIMIT,
    DEFAULT_MEMORY_REVIEW_QUEUE_PRIORITY_MODE,
    DEFAULT_OPEN_LOOP_LIMIT,
    DEFAULT_RESUMPTION_BRIEF_EVENT_LIMIT,
    DEFAULT_RESUMPTION_BRIEF_MEMORY_LIMIT,
    DEFAULT_RESUMPTION_BRIEF_OPEN_LOOP_LIMIT,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
    MAX_MEMORY_REVIEW_LIMIT,
    MAX_OPEN_LOOP_LIMIT,
    MAX_RESUMPTION_BRIEF_EVENT_LIMIT,
    MAX_RESUMPTION_BRIEF_MEMORY_LIMIT,
    MAX_RESUMPTION_BRIEF_OPEN_LOOP_LIMIT,
    MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    MAX_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
    ContextCompilerLimits,
    MemoryHygieneDashboardResponse,
    MemoryTrustDashboardResponse,
    ThreadHealthDashboardResponse,
    EmbeddingConfigStatus,
    EmbeddingConfigCreateInput,
    EntityEdgeCreateInput,
    EntityCreateInput,
    EntityType,
    ExplicitCommitmentExtractionRequestInput,
    ExplicitPreferenceExtractionRequestInput,
    ExplicitSignalCaptureRequestInput,
    MemoryCandidateInput,
    OpenLoopCandidateInput,
    MemoryEmbeddingUpsertInput,
    THREAD_EVENT_LIST_ORDER,
    THREAD_LIST_ORDER,
    THREAD_SESSION_LIST_ORDER,
    MemoryReviewLabelValue,
    MemoryReviewQueuePriorityMode,
    MemoryReviewStatusFilter,
    OpenLoopStatusFilter,
    OpenLoopCreateInput,
    OpenLoopStatusUpdateInput,
    PolicyCreateInput,
    PolicyEffect,
    PolicyEvaluationRequestInput,
    SemanticMemoryRetrievalRequestInput,
    TaskArtifactChunkEmbeddingUpsertInput,
    ArtifactScopedArtifactChunkRetrievalInput,
    TaskArtifactIngestInput,
    ThreadCreateInput,
    ThreadCreateResponse,
    ThreadDetailResponse,
    ThreadEventListResponse,
    ThreadEventListSummary,
    ThreadEventRecord,
    ThreadListResponse,
    ThreadListSummary,
    ThreadRecord,
    ResumptionBriefRequestInput,
    ResumptionBriefResponse,
    ThreadSessionListResponse,
    ThreadSessionListSummary,
    ThreadSessionRecord,
)
from alicebot_api.phase3_profiles import (
    get_agent_profile as get_registered_agent_profile,
    list_agent_profile_ids as list_registered_agent_profile_ids,
    list_agent_profiles as list_registered_agent_profiles,
)
from alicebot_api.artifacts import (
    TaskArtifactChunkRetrievalValidationError,
    TaskArtifactNotFoundError,
    TaskArtifactValidationError,
    get_task_artifact_record,
    ingest_task_artifact_record,
    list_task_artifact_chunk_records,
    list_task_artifact_records,
    retrieve_artifact_scoped_artifact_chunk_records,
)
from alicebot_api.db import user_connection
from alicebot_api.tasks import TaskNotFoundError
from alicebot_api.workspaces import TaskWorkspaceNotFoundError
from alicebot_api.embedding import (
    EmbeddingConfigValidationError,
    MemoryEmbeddingNotFoundError,
    MemoryEmbeddingValidationError,
    TaskArtifactChunkEmbeddingNotFoundError,
    TaskArtifactChunkEmbeddingValidationError,
    create_embedding_config_record,
    get_memory_embedding_record,
    get_task_artifact_chunk_embedding_record,
    list_embedding_config_records,
    list_memory_embedding_records,
    list_task_artifact_chunk_embedding_records_for_artifact,
    list_task_artifact_chunk_embedding_records_for_chunk,
    upsert_task_artifact_chunk_embedding_record,
    upsert_memory_embedding_record,
)
from alicebot_api.entity import (
    EntityNotFoundError,
    EntityValidationError,
    create_entity_record,
    get_entity_record,
    list_entity_records,
)
from alicebot_api.entity_edge import (
    EntityEdgeValidationError,
    create_entity_edge_record,
    list_entity_edge_records,
)
from alicebot_api.explicit_preferences import (
    ExplicitPreferenceExtractionValidationError,
    extract_and_admit_explicit_preferences,
)
from alicebot_api.explicit_commitments import (
    ExplicitCommitmentExtractionValidationError,
    extract_and_admit_explicit_commitments,
)
from alicebot_api.explicit_signal_capture import (
    ExplicitSignalCaptureValidationError,
    extract_and_admit_explicit_signals,
)
from alicebot_api.conversation_health import get_thread_health_dashboard
from alicebot_api.memory import (
    MemoryAdmissionValidationError,
    MemoryReviewNotFoundError,
    OpenLoopNotFoundError,
    OpenLoopValidationError,
    admit_memory_candidate,
    create_open_loop_record,
    create_memory_review_label_record,
    get_open_loop_record,
    get_memory_evaluation_summary,
    get_memory_hygiene_dashboard_summary,
    get_memory_quality_gate_summary,
    get_memory_trust_dashboard_summary,
    get_memory_review_record,
    list_open_loop_records,
    list_memory_review_queue_records,
    list_memory_review_label_records,
    list_memory_review_records,
    list_memory_revision_review_records,
    update_open_loop_status_record,
)
from alicebot_api.policy import (
    PolicyEvaluationValidationError,
    PolicyNotFoundError,
    PolicyValidationError,
    create_policy_record,
    evaluate_policy_request,
    get_policy_record,
    list_consent_records,
    list_policy_records,
    upsert_consent_record,
)
from alicebot_api.semantic_retrieval import (
    SemanticArtifactChunkRetrievalValidationError,
    SemanticMemoryRetrievalValidationError,
    retrieve_artifact_scoped_semantic_artifact_chunk_records,
    retrieve_semantic_memory_records,
)
from alicebot_api.store import (
    ContinuityStore,
    ContinuityStoreInvariantError,
    EventRow,
    SessionRow,
    ThreadRow,
)
from alicebot_api.traces import (
    TraceNotFoundError,
    get_trace_record,
    list_trace_event_records,
    list_trace_records,
)
from fastapi import APIRouter

from alicebot_api.routers._api_shared import _json_object, _json_value
from alicebot_api.routers._vnext_shared import BaseModel

core_router = APIRouter()
task_artifact_router = APIRouter()
task_artifact_retrieval_router = APIRouter()
task_artifact_semantic_router = APIRouter()
signals_router = APIRouter()
memory_router = APIRouter()

class CompileContextSemanticRequest(BaseModel):
    embedding_config_id: UUID
    query_vector: list[float] = Field(min_length=1, max_length=20000)
    limit: int = Field(
        default=DEFAULT_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
    )

class CompileContextTaskScopedArtifactRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["task"]
    task_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    )

class CompileContextArtifactScopedArtifactRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["artifact"]
    task_artifact_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    )

CompileContextArtifactRetrievalRequest = Annotated[
    CompileContextTaskScopedArtifactRetrievalRequest | CompileContextArtifactScopedArtifactRetrievalRequest,
    Field(discriminator="kind"),
]

class CompileContextTaskScopedSemanticArtifactRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["task"]
    task_id: UUID
    embedding_config_id: UUID
    query_vector: list[float] = Field(min_length=1, max_length=20000)
    limit: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    )

class CompileContextArtifactScopedSemanticArtifactRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["artifact"]
    task_artifact_id: UUID
    embedding_config_id: UUID
    query_vector: list[float] = Field(min_length=1, max_length=20000)
    limit: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    )

CompileContextSemanticArtifactRetrievalRequest = Annotated[
    CompileContextTaskScopedSemanticArtifactRetrievalRequest
    | CompileContextArtifactScopedSemanticArtifactRetrievalRequest,
    Field(discriminator="kind"),
]

class CompileContextRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    max_sessions: int = Field(default=DEFAULT_MAX_SESSIONS, ge=0, le=25)
    max_events: int = Field(default=DEFAULT_MAX_EVENTS, ge=0, le=200)
    max_memories: int = Field(default=DEFAULT_MAX_MEMORIES, ge=0, le=50)
    max_entities: int = Field(default=DEFAULT_MAX_ENTITIES, ge=0, le=50)
    max_entity_edges: int = Field(default=DEFAULT_MAX_ENTITY_EDGES, ge=0, le=100)
    semantic: CompileContextSemanticRequest | None = None
    artifact_retrieval: CompileContextArtifactRetrievalRequest | None = None
    semantic_artifact_retrieval: CompileContextSemanticArtifactRetrievalRequest | None = None

class CreateThreadRequest(BaseModel):
    user_id: UUID
    title: str = Field(min_length=1, max_length=200)
    agent_profile_id: str | None = Field(default=None, min_length=1, max_length=100)

class AdmitMemoryOpenLoopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=280)
    due_at: datetime | None = None

class AdmitMemoryRequest(BaseModel):
    user_id: UUID
    memory_key: str = Field(min_length=1, max_length=200)
    value: BoundedJsonValue | None = None
    source_event_ids: list[UUID] = Field(min_length=1)
    agent_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    delete_requested: bool = False
    memory_type: str | None = Field(default=None, min_length=1, max_length=100)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    salience: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmation_status: str | None = Field(default=None, min_length=1, max_length=100)
    trust_class: str | None = Field(default=None, min_length=1, max_length=100)
    promotion_eligibility: str | None = Field(default=None, min_length=1, max_length=100)
    evidence_count: int | None = Field(default=None, ge=0)
    independent_source_count: int | None = Field(default=None, ge=0)
    extracted_by_model: str | None = Field(default=None, min_length=1, max_length=200)
    trust_reason: str | None = Field(default=None, min_length=1, max_length=500)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    last_confirmed_at: datetime | None = None
    open_loop: AdmitMemoryOpenLoopRequest | None = None

    @model_validator(mode="after")
    def validate_temporal_range(self) -> "AdmitMemoryRequest":
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be greater than or equal to valid_from")
        return self

class ExtractExplicitPreferencesRequest(BaseModel):
    user_id: UUID
    source_event_id: UUID

class ExtractExplicitCommitmentsRequest(BaseModel):
    user_id: UUID
    source_event_id: UUID

class CaptureExplicitSignalsRequest(BaseModel):
    user_id: UUID
    source_event_id: UUID

class CreateMemoryReviewLabelRequest(BaseModel):
    user_id: UUID
    label: MemoryReviewLabelValue
    note: str | None = Field(default=None, min_length=1, max_length=280)

class CreateOpenLoopRequest(BaseModel):
    user_id: UUID
    memory_id: UUID | None = None
    title: str = Field(min_length=1, max_length=280)
    due_at: datetime | None = None

class UpdateOpenLoopStatusRequest(BaseModel):
    user_id: UUID
    status: str = Field(min_length=1, max_length=100)
    resolution_note: str | None = Field(default=None, min_length=1, max_length=2000)

class CreateEntityRequest(BaseModel):
    user_id: UUID
    entity_type: EntityType
    name: str = Field(min_length=1, max_length=200)
    source_memory_ids: list[UUID] = Field(min_length=1)

class CreateEntityEdgeRequest(BaseModel):
    user_id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str = Field(min_length=1, max_length=100)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_memory_ids: list[UUID] = Field(min_length=1)

class CreateEmbeddingConfigRequest(BaseModel):
    user_id: UUID
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    dimensions: int = Field(ge=1, le=20000)
    status: EmbeddingConfigStatus = "active"
    metadata: BoundedJsonObject = Field(default_factory=dict)

class UpsertMemoryEmbeddingRequest(BaseModel):
    user_id: UUID
    memory_id: UUID
    embedding_config_id: UUID
    vector: list[float] = Field(min_length=1, max_length=20000)

class UpsertTaskArtifactChunkEmbeddingRequest(BaseModel):
    user_id: UUID
    task_artifact_chunk_id: UUID
    embedding_config_id: UUID
    vector: list[float] = Field(min_length=1, max_length=20000)

class RetrieveSemanticMemoriesRequest(BaseModel):
    user_id: UUID
    embedding_config_id: UUID
    query_vector: list[float] = Field(min_length=1, max_length=20000)
    limit: int = Field(
        default=DEFAULT_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_SEMANTIC_MEMORY_RETRIEVAL_LIMIT,
    )

class RetrieveSemanticArtifactChunksRequest(BaseModel):
    user_id: UUID
    embedding_config_id: UUID
    query_vector: list[float] = Field(min_length=1, max_length=20000)
    limit: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_RETRIEVAL_LIMIT,
    )

class UpsertConsentRequest(BaseModel):
    user_id: UUID
    consent_key: str = Field(min_length=1, max_length=200)
    status: ConsentStatus
    metadata: BoundedJsonObject = Field(default_factory=dict)

class CreatePolicyRequest(BaseModel):
    user_id: UUID
    name: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    effect: PolicyEffect
    priority: int = Field(ge=0, le=1000000)
    active: bool = True
    conditions: BoundedJsonObject = Field(default_factory=dict)
    required_consents: list[str] = Field(default_factory=list)
    agent_profile_id: str | None = Field(default=None, min_length=1, max_length=100)

class EvaluatePolicyRequest(BaseModel):
    user_id: UUID
    thread_id: UUID
    action: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    attributes: BoundedJsonObject = Field(default_factory=dict)

class IngestTaskArtifactRequest(BaseModel):
    user_id: UUID

class RetrieveArtifactChunksRequest(BaseModel):
    user_id: UUID
    query: str = Field(min_length=1, max_length=1000)

def _serialize_thread(thread: ThreadRow) -> ThreadRecord:
    agent_profile_id = _thread_agent_profile_id(thread)
    return {
        "id": str(thread["id"]),
        "title": thread["title"],
        "agent_profile_id": agent_profile_id,
        "created_at": thread["created_at"].isoformat(),
        "updated_at": thread["updated_at"].isoformat(),
    }

def _thread_agent_profile_id(thread: ThreadRow) -> str:
    return str(thread.get("agent_profile_id", DEFAULT_AGENT_PROFILE_ID))

def _serialize_thread_session(session: SessionRow) -> ThreadSessionRecord:
    return {
        "id": str(session["id"]),
        "thread_id": str(session["thread_id"]),
        "status": session["status"],
        "started_at": None if session["started_at"] is None else session["started_at"].isoformat(),
        "ended_at": None if session["ended_at"] is None else session["ended_at"].isoformat(),
        "created_at": session["created_at"].isoformat(),
    }

def _serialize_thread_event(event: EventRow) -> ThreadEventRecord:
    return {
        "id": str(event["id"]),
        "thread_id": str(event["thread_id"]),
        "session_id": None if event["session_id"] is None else str(event["session_id"]),
        "sequence_no": event["sequence_no"],
        "kind": event["kind"],
        "payload": event["payload"],
        "created_at": event["created_at"].isoformat(),
    }

@core_router.get("/v0/agent-profiles")
def list_agent_profiles() -> JSONResponse:
    settings = get_settings()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        items = list_registered_agent_profiles(ContinuityStore(conn))
    summary: AgentProfileListSummary = {
        "total_count": len(items),
        "order": list(AGENT_PROFILE_LIST_ORDER),
    }
    payload: AgentProfileListResponse = {
        "items": items,
        "summary": summary,
    }
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/context/compile")
def compile_context(request: CompileContextRequest) -> JSONResponse:
    settings = get_settings()
    artifact_retrieval: (
        CompileContextTaskScopedArtifactRetrievalInput | CompileContextArtifactScopedArtifactRetrievalInput | None
    ) = None
    semantic_artifact_retrieval: (
        CompileContextTaskScopedSemanticArtifactRetrievalInput
        | CompileContextArtifactScopedSemanticArtifactRetrievalInput
        | None
    ) = None
    if isinstance(request.artifact_retrieval, CompileContextTaskScopedArtifactRetrievalRequest):
        artifact_retrieval = CompileContextTaskScopedArtifactRetrievalInput(
            task_id=request.artifact_retrieval.task_id,
            query=request.artifact_retrieval.query,
            limit=request.artifact_retrieval.limit,
        )
    elif isinstance(
        request.artifact_retrieval,
        CompileContextArtifactScopedArtifactRetrievalRequest,
    ):
        artifact_retrieval = CompileContextArtifactScopedArtifactRetrievalInput(
            task_artifact_id=request.artifact_retrieval.task_artifact_id,
            query=request.artifact_retrieval.query,
            limit=request.artifact_retrieval.limit,
        )
    if isinstance(
        request.semantic_artifact_retrieval,
        CompileContextTaskScopedSemanticArtifactRetrievalRequest,
    ):
        semantic_artifact_retrieval = CompileContextTaskScopedSemanticArtifactRetrievalInput(
            task_id=request.semantic_artifact_retrieval.task_id,
            embedding_config_id=request.semantic_artifact_retrieval.embedding_config_id,
            query_vector=tuple(request.semantic_artifact_retrieval.query_vector),
            limit=request.semantic_artifact_retrieval.limit,
        )
    elif isinstance(
        request.semantic_artifact_retrieval,
        CompileContextArtifactScopedSemanticArtifactRetrievalRequest,
    ):
        semantic_artifact_retrieval = CompileContextArtifactScopedSemanticArtifactRetrievalInput(
            task_artifact_id=request.semantic_artifact_retrieval.task_artifact_id,
            embedding_config_id=request.semantic_artifact_retrieval.embedding_config_id,
            query_vector=tuple(request.semantic_artifact_retrieval.query_vector),
            limit=request.semantic_artifact_retrieval.limit,
        )

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = ContinuityStore(conn)
            thread = store.get_thread(request.thread_id)
            result = compile_and_persist_trace(
                store,
                user_id=request.user_id,
                thread_id=request.thread_id,
                limits=ContextCompilerLimits(
                    max_sessions=request.max_sessions,
                    max_events=request.max_events,
                    max_memories=request.max_memories,
                    max_entities=request.max_entities,
                    max_entity_edges=request.max_entity_edges,
                ),
                semantic_retrieval=(
                    None
                    if request.semantic is None
                    else CompileContextSemanticRetrievalInput(
                        embedding_config_id=request.semantic.embedding_config_id,
                        query_vector=tuple(request.semantic.query_vector),
                        limit=request.semantic.limit,
                    )
                ),
                artifact_retrieval=artifact_retrieval,
                semantic_artifact_retrieval=semantic_artifact_retrieval,
            )
    except TaskArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except SemanticArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except SemanticMemoryRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except (TaskNotFoundError, TaskArtifactNotFoundError) as exc:
        return public_exception_response(exc, status_code=404)
    except ContinuityStoreInvariantError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "trace_id": result.trace_id,
                "trace_event_count": result.trace_event_count,
                "context_pack": result.context_pack,
                "metadata": {"agent_profile_id": _thread_agent_profile_id(thread)},
            }
        ),
    )

@core_router.post("/v0/threads")
def create_thread(request: CreateThreadRequest) -> JSONResponse:
    settings = get_settings()
    agent_profile_id = request.agent_profile_id if request.agent_profile_id is not None else DEFAULT_AGENT_PROFILE_ID
    thread_input = ThreadCreateInput(
        title=request.title,
        agent_profile_id=agent_profile_id,
    )

    with user_connection(settings.database_url, request.user_id) as conn:
        store = ContinuityStore(conn)
        if get_registered_agent_profile(store, agent_profile_id) is None:
            allowed_agent_profile_ids = list_registered_agent_profile_ids(store)
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "invalid_agent_profile_id",
                        "message": ("agent_profile_id must be one of: " + ", ".join(allowed_agent_profile_ids)),
                        "allowed_agent_profile_ids": allowed_agent_profile_ids,
                    }
                },
            )

        created = store.create_thread(
            thread_input.title,
            thread_input.agent_profile_id,
        )

    payload: ThreadCreateResponse = {"thread": _serialize_thread(created)}
    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads")
def list_threads(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        items = [_serialize_thread(thread) for thread in ContinuityStore(conn).list_threads()]

    summary: ThreadListSummary = {
        "total_count": len(items),
        "order": list(THREAD_LIST_ORDER),
    }
    payload: ThreadListResponse = {
        "items": items,
        "summary": summary,
    }
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads/health-dashboard")
def get_threads_health_dashboard(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: ThreadHealthDashboardResponse = get_thread_health_dashboard(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads/{thread_id}")
def get_thread(thread_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        thread = ContinuityStore(conn).get_thread_optional(thread_id)

    if thread is None:
        return public_exception_response(LookupError(f"thread {thread_id} was not found"), status_code=404)

    payload: ThreadDetailResponse = {"thread": _serialize_thread(thread)}
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads/{thread_id}/sessions")
def list_thread_sessions(thread_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.get_thread_optional(thread_id)
        if thread is None:
            return public_exception_response(LookupError(f"thread {thread_id} was not found"), status_code=404)
        items = [_serialize_thread_session(session) for session in store.list_thread_sessions(thread_id)]

    summary: ThreadSessionListSummary = {
        "thread_id": str(thread["id"]),
        "total_count": len(items),
        "order": list(THREAD_SESSION_LIST_ORDER),
    }
    payload: ThreadSessionListResponse = {
        "items": items,
        "summary": summary,
    }
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads/{thread_id}/events")
def list_thread_events(thread_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.get_thread_optional(thread_id)
        if thread is None:
            return public_exception_response(LookupError(f"thread {thread_id} was not found"), status_code=404)
        items = [_serialize_thread_event(event) for event in store.list_thread_events(thread_id)]

    summary: ThreadEventListSummary = {
        "thread_id": str(thread["id"]),
        "total_count": len(items),
        "order": list(THREAD_EVENT_LIST_ORDER),
    }
    payload: ThreadEventListResponse = {
        "items": items,
        "summary": summary,
    }
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/threads/{thread_id}/resumption-brief")
def get_thread_resumption_brief(
    thread_id: UUID,
    user_id: UUID,
    max_events: Annotated[
        int,
        Query(ge=0, le=MAX_RESUMPTION_BRIEF_EVENT_LIMIT),
    ] = DEFAULT_RESUMPTION_BRIEF_EVENT_LIMIT,
    max_open_loops: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_RESUMPTION_BRIEF_OPEN_LOOP_LIMIT,
        ),
    ] = DEFAULT_RESUMPTION_BRIEF_OPEN_LOOP_LIMIT,
    max_memories: Annotated[
        int,
        Query(ge=0, le=MAX_RESUMPTION_BRIEF_MEMORY_LIMIT),
    ] = DEFAULT_RESUMPTION_BRIEF_MEMORY_LIMIT,
) -> JSONResponse:
    settings = get_settings()
    request = ResumptionBriefRequestInput(
        thread_id=thread_id,
        max_events=max_events,
        max_open_loops=max_open_loops,
        max_memories=max_memories,
    )

    with user_connection(settings.database_url, user_id) as conn:
        store = ContinuityStore(conn)
        thread = store.get_thread_optional(thread_id)
        if thread is None:
            return public_exception_response(LookupError(f"thread {thread_id} was not found"), status_code=404)
        brief = compile_resumption_brief(
            store,
            thread=thread,
            event_limit=request.max_events,
            open_loop_limit=request.max_open_loops,
            memory_limit=request.max_memories,
        )

    payload: ResumptionBriefResponse = {"brief": brief}
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/traces")
def list_traces(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_trace_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/traces/{trace_id}")
def get_trace(trace_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_trace_record(
                ContinuityStore(conn),
                user_id=user_id,
                trace_id=trace_id,
            )
    except TraceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/traces/{trace_id}/events")
def list_trace_events(trace_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_trace_event_records(
                ContinuityStore(conn),
                user_id=user_id,
                trace_id=trace_id,
            )
    except TraceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/memories/admit")
def admit_memory(request: AdmitMemoryRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            decision = admit_memory_candidate(
                ContinuityStore(conn),
                user_id=request.user_id,
                candidate=MemoryCandidateInput(
                    memory_key=request.memory_key,
                    value=_json_value(request.value),
                    source_event_ids=tuple(request.source_event_ids),
                    agent_profile_id=request.agent_profile_id,
                    delete_requested=request.delete_requested,
                    memory_type=request.memory_type,
                    confidence=request.confidence,
                    salience=request.salience,
                    confirmation_status=request.confirmation_status,
                    trust_class=request.trust_class,
                    promotion_eligibility=request.promotion_eligibility,
                    evidence_count=request.evidence_count,
                    independent_source_count=request.independent_source_count,
                    extracted_by_model=request.extracted_by_model,
                    trust_reason=request.trust_reason,
                    valid_from=request.valid_from,
                    valid_to=request.valid_to,
                    last_confirmed_at=request.last_confirmed_at,
                    open_loop=(
                        None
                        if request.open_loop is None
                        else OpenLoopCandidateInput(
                            title=request.open_loop.title,
                            due_at=request.open_loop.due_at,
                        )
                    ),
                ),
            )
    except MemoryAdmissionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    payload: dict[str, object] = {
        "decision": decision.action,
        "reason": decision.reason,
        "memory": decision.memory,
        "revision": decision.revision,
    }
    if decision.open_loop is not None:
        payload["open_loop"] = decision.open_loop

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/open-loops")
def list_open_loops(
    user_id: UUID,
    status: OpenLoopStatusFilter = Query(default="open"),
    limit: int = Query(default=DEFAULT_OPEN_LOOP_LIMIT, ge=1, le=MAX_OPEN_LOOP_LIMIT),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_open_loop_records(
            ContinuityStore(conn),
            user_id=user_id,
            status=status,
            limit=limit,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/open-loops/{open_loop_id}")
def get_open_loop(
    open_loop_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_open_loop_record(
                ContinuityStore(conn),
                user_id=user_id,
                open_loop_id=open_loop_id,
            )
    except OpenLoopNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/open-loops")
def create_open_loop(request: CreateOpenLoopRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_open_loop_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                open_loop=OpenLoopCreateInput(
                    memory_id=request.memory_id,
                    title=request.title,
                    due_at=request.due_at,
                ),
            )
    except OpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/open-loops/{open_loop_id}/status")
def update_open_loop_status(
    open_loop_id: UUID,
    request: UpdateOpenLoopStatusRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = update_open_loop_status_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                open_loop_id=open_loop_id,
                request=OpenLoopStatusUpdateInput(
                    status=request.status,  # type: ignore[arg-type]
                    resolution_note=request.resolution_note,
                ),
            )
    except OpenLoopValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except OpenLoopNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/consents")
def upsert_consent(request: UpsertConsentRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = upsert_consent_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                consent=ConsentUpsertInput(
                    consent_key=request.consent_key,
                    status=request.status,
                    metadata=_json_object(request.metadata),
                ),
            )
    except PolicyValidationError as exc:
        return public_exception_response(exc, status_code=400)

    status_code = 201 if payload["write_mode"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/consents")
def list_consents(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_consent_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/policies")
def create_policy(request: CreatePolicyRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = ContinuityStore(conn)
            if (
                request.agent_profile_id is not None
                and get_registered_agent_profile(store, request.agent_profile_id) is None
            ):
                allowed_agent_profile_ids = list_registered_agent_profile_ids(store)
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_agent_profile_id",
                            "message": ("agent_profile_id must be one of: " + ", ".join(allowed_agent_profile_ids)),
                            "allowed_agent_profile_ids": allowed_agent_profile_ids,
                        }
                    },
                )

            payload = create_policy_record(
                store,
                user_id=request.user_id,
                policy=PolicyCreateInput(
                    name=request.name,
                    action=request.action,
                    scope=request.scope,
                    effect=request.effect,
                    priority=request.priority,
                    active=request.active,
                    conditions=_json_object(request.conditions),
                    required_consents=tuple(request.required_consents),
                    agent_profile_id=request.agent_profile_id,
                ),
            )
    except PolicyValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/policies")
def list_policies(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_policy_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.get("/v0/policies/{policy_id}")
def get_policy(policy_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_policy_record(
                ContinuityStore(conn),
                user_id=user_id,
                policy_id=policy_id,
            )
    except PolicyNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@core_router.post("/v0/policies/evaluate")
def evaluate_policy(request: EvaluatePolicyRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = evaluate_policy_request(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=PolicyEvaluationRequestInput(
                    thread_id=request.thread_id,
                    action=request.action,
                    scope=request.scope,
                    attributes=_json_object(request.attributes),
                ),
            )
    except PolicyEvaluationValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_router.get("/v0/task-artifacts")
def list_task_artifacts(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_task_artifact_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_router.get("/v0/task-artifacts/{task_artifact_id}")
def get_task_artifact(task_artifact_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_artifact_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_artifact_id=task_artifact_id,
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_router.post("/v0/task-artifacts/{task_artifact_id}/ingest")
def ingest_task_artifact(
    task_artifact_id: UUID,
    request: IngestTaskArtifactRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = ingest_task_artifact_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskArtifactIngestInput(task_artifact_id=task_artifact_id),
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskWorkspaceNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskArtifactValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_router.get("/v0/task-artifacts/{task_artifact_id}/chunks")
def list_task_artifact_chunks(task_artifact_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_task_artifact_chunk_records(
                ContinuityStore(conn),
                user_id=user_id,
                task_artifact_id=task_artifact_id,
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_retrieval_router.post("/v0/task-artifacts/{task_artifact_id}/chunks/retrieve")
def retrieve_task_artifact_chunks_for_artifact(
    task_artifact_id: UUID,
    request: RetrieveArtifactChunksRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = retrieve_artifact_scoped_artifact_chunk_records(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ArtifactScopedArtifactChunkRetrievalInput(
                    task_artifact_id=task_artifact_id,
                    query=request.query,
                ),
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except TaskArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@task_artifact_semantic_router.post("/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval")
def retrieve_semantic_artifact_chunks_for_artifact(
    task_artifact_id: UUID,
    request: RetrieveSemanticArtifactChunksRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = retrieve_artifact_scoped_semantic_artifact_chunk_records(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ArtifactScopedSemanticArtifactChunkRetrievalInput(
                    task_artifact_id=task_artifact_id,
                    embedding_config_id=request.embedding_config_id,
                    query_vector=tuple(request.query_vector),
                    limit=request.limit,
                ),
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)
    except SemanticArtifactChunkRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@signals_router.post("/v0/memories/extract-explicit-preferences")
def extract_explicit_preferences(request: ExtractExplicitPreferencesRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = extract_and_admit_explicit_preferences(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ExplicitPreferenceExtractionRequestInput(
                    source_event_id=request.source_event_id,
                ),
            )
    except ExplicitPreferenceExtractionValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryAdmissionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@signals_router.post("/v0/open-loops/extract-explicit-commitments")
def extract_explicit_commitments(request: ExtractExplicitCommitmentsRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = extract_and_admit_explicit_commitments(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ExplicitCommitmentExtractionRequestInput(
                    source_event_id=request.source_event_id,
                ),
            )
    except ExplicitCommitmentExtractionValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryAdmissionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@signals_router.post("/v0/memories/capture-explicit-signals")
def capture_explicit_signals(request: CaptureExplicitSignalsRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = extract_and_admit_explicit_signals(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=ExplicitSignalCaptureRequestInput(
                    source_event_id=request.source_event_id,
                ),
            )
    except ExplicitSignalCaptureValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except MemoryAdmissionValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories")
def list_memories(
    user_id: UUID,
    status: MemoryReviewStatusFilter = Query(default="active"),
    limit: int = Query(default=DEFAULT_MEMORY_REVIEW_LIMIT, ge=1, le=MAX_MEMORY_REVIEW_LIMIT),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_memory_review_records(
            ContinuityStore(conn),
            user_id=user_id,
            status=status,
            limit=limit,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/review-queue")
def list_memory_review_queue(
    user_id: UUID,
    limit: int = Query(default=DEFAULT_MEMORY_REVIEW_LIMIT, ge=1, le=MAX_MEMORY_REVIEW_LIMIT),
    priority_mode: MemoryReviewQueuePriorityMode = Query(default=DEFAULT_MEMORY_REVIEW_QUEUE_PRIORITY_MODE),
) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_memory_review_queue_records(
            ContinuityStore(conn),
            user_id=user_id,
            limit=limit,
            priority_mode=priority_mode,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/quality-gate")
def get_memories_quality_gate(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = get_memory_quality_gate_summary(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/trust-dashboard")
def get_memories_trust_dashboard(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: MemoryTrustDashboardResponse = get_memory_trust_dashboard_summary(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/hygiene-dashboard")
def get_memories_hygiene_dashboard(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload: MemoryHygieneDashboardResponse = get_memory_hygiene_dashboard_summary(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/evaluation-summary")
def get_memories_evaluation_summary(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = get_memory_evaluation_summary(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/memories/semantic-retrieval")
def retrieve_semantic_memories(request: RetrieveSemanticMemoriesRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = retrieve_semantic_memory_records(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=SemanticMemoryRetrievalRequestInput(
                    embedding_config_id=request.embedding_config_id,
                    query_vector=tuple(request.query_vector),
                    limit=request.limit,
                ),
            )
    except SemanticMemoryRetrievalValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/{memory_id}")
def get_memory(
    memory_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_memory_review_record(
                ContinuityStore(conn),
                user_id=user_id,
                memory_id=memory_id,
            )
    except MemoryReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/{memory_id}/revisions")
def list_memory_revisions(
    memory_id: UUID,
    user_id: UUID,
    limit: int = Query(default=DEFAULT_MEMORY_REVIEW_LIMIT, ge=1, le=MAX_MEMORY_REVIEW_LIMIT),
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_memory_revision_review_records(
                ContinuityStore(conn),
                user_id=user_id,
                memory_id=memory_id,
                limit=limit,
            )
    except MemoryReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/memories/{memory_id}/labels")
def create_memory_review_label(
    memory_id: UUID,
    request: CreateMemoryReviewLabelRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_memory_review_label_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                memory_id=memory_id,
                label=request.label,
                note=request.note,
            )
    except MemoryReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/{memory_id}/labels")
def list_memory_review_labels(
    memory_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_memory_review_label_records(
                ContinuityStore(conn),
                user_id=user_id,
                memory_id=memory_id,
            )
    except MemoryReviewNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/embedding-configs")
def create_embedding_config(request: CreateEmbeddingConfigRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_embedding_config_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                config=EmbeddingConfigCreateInput(
                    provider=request.provider,
                    model=request.model,
                    version=request.version,
                    dimensions=request.dimensions,
                    status=request.status,
                    metadata=_json_object(request.metadata),
                ),
            )
    except EmbeddingConfigValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/embedding-configs")
def list_embedding_configs(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_embedding_config_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/memory-embeddings")
def upsert_memory_embedding(request: UpsertMemoryEmbeddingRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = upsert_memory_embedding_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=MemoryEmbeddingUpsertInput(
                    memory_id=request.memory_id,
                    embedding_config_id=request.embedding_config_id,
                    vector=tuple(request.vector),
                ),
            )
    except MemoryEmbeddingValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/task-artifact-chunk-embeddings")
def upsert_task_artifact_chunk_embedding(
    request: UpsertTaskArtifactChunkEmbeddingRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = upsert_task_artifact_chunk_embedding_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                request=TaskArtifactChunkEmbeddingUpsertInput(
                    task_artifact_chunk_id=request.task_artifact_chunk_id,
                    embedding_config_id=request.embedding_config_id,
                    vector=tuple(request.vector),
                ),
            )
    except TaskArtifactChunkEmbeddingValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memories/{memory_id}/embeddings")
def list_memory_embeddings(memory_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_memory_embedding_records(
                ContinuityStore(conn),
                user_id=user_id,
                memory_id=memory_id,
            )
    except MemoryEmbeddingNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/task-artifacts/{task_artifact_id}/chunk-embeddings")
def list_task_artifact_chunk_embeddings_for_artifact(
    task_artifact_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_task_artifact_chunk_embedding_records_for_artifact(
                ContinuityStore(conn),
                user_id=user_id,
                task_artifact_id=task_artifact_id,
            )
    except TaskArtifactNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings")
def list_task_artifact_chunk_embeddings(
    task_artifact_chunk_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_task_artifact_chunk_embedding_records_for_chunk(
                ContinuityStore(conn),
                user_id=user_id,
                task_artifact_chunk_id=task_artifact_chunk_id,
            )
    except TaskArtifactChunkEmbeddingNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/memory-embeddings/{memory_embedding_id}")
def get_memory_embedding(memory_embedding_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_memory_embedding_record(
                ContinuityStore(conn),
                user_id=user_id,
                memory_embedding_id=memory_embedding_id,
            )
    except MemoryEmbeddingNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}")
def get_task_artifact_chunk_embedding(
    task_artifact_chunk_embedding_id: UUID,
    user_id: UUID,
) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_task_artifact_chunk_embedding_record(
                ContinuityStore(conn),
                user_id=user_id,
                task_artifact_chunk_embedding_id=task_artifact_chunk_embedding_id,
            )
    except TaskArtifactChunkEmbeddingNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/entities")
def create_entity(request: CreateEntityRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_entity_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                entity=EntityCreateInput(
                    entity_type=request.entity_type,
                    name=request.name,
                    source_memory_ids=tuple(request.source_memory_ids),
                ),
            )
    except EntityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.post("/v0/entity-edges")
def create_entity_edge(request: CreateEntityEdgeRequest) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = create_entity_edge_record(
                ContinuityStore(conn),
                user_id=request.user_id,
                edge=EntityEdgeCreateInput(
                    from_entity_id=request.from_entity_id,
                    to_entity_id=request.to_entity_id,
                    relationship_type=request.relationship_type,
                    valid_from=request.valid_from,
                    valid_to=request.valid_to,
                    source_memory_ids=tuple(request.source_memory_ids),
                ),
            )
    except EntityEdgeValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/entities")
def list_entities(user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = list_entity_records(
            ContinuityStore(conn),
            user_id=user_id,
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/entities/{entity_id}/edges")
def list_entity_edges(entity_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = list_entity_edge_records(
                ContinuityStore(conn),
                user_id=user_id,
                entity_id=entity_id,
            )
    except EntityNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )

@memory_router.get("/v0/entities/{entity_id}")
def get_entity(entity_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    try:
        with user_connection(settings.database_url, user_id) as conn:
            payload = get_entity_record(
                ContinuityStore(conn),
                user_id=user_id,
                entity_id=entity_id,
            )
    except EntityNotFoundError as exc:
        return public_exception_response(exc, status_code=404)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )
