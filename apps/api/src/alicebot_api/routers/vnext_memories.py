from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from alicebot_api.browser_clip_capabilities import (
    BrowserClipCapabilityValidationError,
    consume_browser_clip_capability,
    issue_browser_clip_capability,
)
from alicebot_api.config import get_settings
from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import redact_memory_flow
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers._vnext_embeddings import (
    _persist_vnext_deferred_embeddings,
)
from alicebot_api.routers._vnext_shared import (
    BaseModel,
    VNextAgentRequest,
    VNextDomain,
    VNextSensitivity,
    _vnext_agent_actor,
    _vnext_agent_auth_error_response,
    _vnext_agent_identity,
    _vnext_agent_record,
    _vnext_authenticated_agent_identity,
    _vnext_load_source_trace,
    _vnext_metadata,
    _vnext_permission_response,
    _vnext_policy_checked,
    _vnext_public_error_response,
)
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_agent_control import (
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    agent_metadata,
    append_policy_events,
    append_promotion_event,
    resource_project_scope,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
)
from alicebot_api.vnext_capture import (
    VNextCaptureService,
    VNextCaptureValidationError,
)
from alicebot_api.vnext_connectors import (
    VNextConnectorService,
    VNextConnectorValidationError,
    list_connector_definitions,
    scan_local_folder,
)
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
    _brain_charter_row,
    agent_api_keys_provisioned,
    is_pending_consolidation_candidate,
    load_promotion_settings,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_promotion_policy import promotion_candidate_for_proposal
from alicebot_api.vnext_project_update_guard import (
    PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
    is_pending_project_update_memory,
)
from alicebot_api.vnext_store import PostgresVNextStore


source_create_router = APIRouter()
connectors_router = APIRouter()
source_review_router = APIRouter()
source_delete_router = APIRouter()
memory_router = APIRouter()


class VNextSourceCaptureRequest(VNextAgentRequest):
    user_id: UUID
    raw_text: str = Field(min_length=1, max_length=200_000)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    domain: VNextDomain = "unknown"
    sensitivity: VNextSensitivity = "unknown"


class VNextSourceReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    domain: VNextDomain | None = None
    sensitivity: VNextSensitivity | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=120)
    review_note: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextConnectorSyncRequest(VNextAgentRequest):
    user_id: UUID
    items: list[dict[str, object]] = Field(default_factory=list)
    default_domain: VNextDomain | None = None
    default_sensitivity: VNextSensitivity | None = None


class VNextConnectorConfigRequest(VNextAgentRequest):
    user_id: UUID
    enabled: bool | None = None
    default_domain: VNextDomain | None = None
    default_sensitivity: VNextSensitivity | None = None
    secret_ref: str | None = Field(default=None, min_length=1, max_length=240)
    sync_mode: str | None = Field(default=None, min_length=1, max_length=40)
    poll_interval_seconds: int | None = Field(default=None, ge=1, le=86_400)
    config_json: dict[str, object] = Field(default_factory=dict)


class VNextTelegramSyncRequest(VNextAgentRequest):
    user_id: UUID
    updates: list[dict[str, object]] = Field(min_length=1)
    allowed_chat_ids: list[str] = Field(min_length=1)
    default_domain: VNextDomain | None = None
    default_sensitivity: VNextSensitivity | None = None


class VNextLocalFolderSyncRequest(VNextAgentRequest):
    user_id: UUID
    paths: list[str] = Field(default_factory=list)
    recursive: bool = True
    extensions: list[str] = Field(default_factory=lambda: [".md", ".txt"])
    ignore_patterns: list[str] = Field(default_factory=list)
    default_domain: VNextDomain | None = None
    default_sensitivity: VNextSensitivity | None = None


class VNextBrowserClipperCaptureRequest(VNextAgentRequest):
    user_id: UUID
    url: str = Field(min_length=1, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    selected_text: str | None = Field(default=None, min_length=1, max_length=200_000)
    page_text: str | None = Field(default=None, min_length=1, max_length=500_000)
    user_note: str | None = Field(default=None, min_length=1, max_length=20_000)
    capture_token: str | None = Field(default=None, min_length=1, max_length=500)
    capture_capability: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        json_schema_extra={"writeOnly": True},
    )
    captured_at: str | None = Field(default=None, min_length=1, max_length=120)
    domain: VNextDomain = "professional"
    sensitivity: VNextSensitivity = "private"


class VNextBrowserClipperCapabilityRequest(VNextAgentRequest):
    user_id: UUID
    origin: str = Field(min_length=1, max_length=2048)


class VNextAgentOutputIngestRequest(VNextAgentRequest):
    user_id: UUID
    agent_id: str = Field(min_length=1, max_length=160)
    agent_type: str = Field(default="unknown", min_length=1, max_length=80)
    agent_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    task_id: str | None = Field(default=None, min_length=1, max_length=160)
    project_scope: list[str] = Field(default_factory=list)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=500_000)
    output_type: str = Field(default="general", min_length=1, max_length=80)
    domain: VNextDomain = "project"
    sensitivity: VNextSensitivity = "private"
    source_refs: list[object] = Field(default_factory=list)
    rationale: str | None = Field(default=None, min_length=1, max_length=4000)
    propose_memory: bool = False


class VNextMemoryReviewRequest(VNextAgentRequest):
    user_id: UUID
    action: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    canonical_text: str | None = Field(default=None, min_length=1, max_length=4000)
    summary: str | None = Field(default=None, min_length=1, max_length=4000)
    domain: VNextDomain | None = None
    sensitivity: VNextSensitivity | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextMemoryProposalRequest(VNextAgentRequest):
    user_id: UUID
    proposal_type: str = Field(default="candidate_memory", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=280)
    canonical_text: str = Field(min_length=1, max_length=20_000)
    source_refs: list[object] = Field(default_factory=list)
    domain: VNextDomain = "unknown"
    sensitivity: VNextSensitivity = "unknown"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str | None = Field(default=None, min_length=1, max_length=4000)
    review_required: bool = True


class VNextMemoryCommitRequest(VNextAgentRequest):
    user_id: UUID
    intent: str = Field(default="explicit_remember", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=280)
    canonical_text: str = Field(min_length=1, max_length=20_000)
    memory_type: str = Field(default="semantic", min_length=1, max_length=80)
    domain: VNextDomain = "unknown"
    sensitivity: VNextSensitivity = "unknown"
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    source_type: str = Field(default="direct_user_instruction", min_length=1, max_length=120)
    source_refs: list[object] = Field(default_factory=list)
    conversation_excerpt: str | None = Field(default=None, min_length=1, max_length=4000)
    rationale: str | None = Field(default=None, min_length=1, max_length=4000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    contradiction_refs: list[str] = Field(default_factory=list)


class VNextMemoryConfirmRequest(VNextAgentRequest):
    user_id: UUID
    confirmation_id: str = Field(min_length=1, max_length=160)
    action: str = Field(default="confirm", min_length=1, max_length=40)
    canonical_text: str | None = Field(default=None, min_length=1, max_length=20_000)
    rationale: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextMemoryUndoRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextMemoryCorrectRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    canonical_text: str = Field(min_length=1, max_length=20_000)
    reason: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextMemoryForgetRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    reason: str | None = Field(default=None, min_length=1, max_length=4000)


class VNextMemoryExpireRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    valid_to: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4000)


class VNextMemoryUnexpireRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    reason: str = Field(min_length=1, max_length=4000)


class VNextMemoryAcceptConsolidationRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    reason: str = Field(min_length=1, max_length=4000)


class VNextMemoryRedactRequest(VNextAgentRequest):
    user_id: UUID
    memory_id: UUID
    reason: str = Field(min_length=1, max_length=4000)


class VNextDoctorRunRequest(BaseModel):
    user_id: UUID
    fix_safe: bool = False
    ci: bool = True


def _vnext_terminal_review_metadata(
    existing_metadata: dict[str, object],
    *,
    outcome: Literal["confirmed", "rejected"],
    terminal_at: str,
) -> dict[str, object]:
    """Close nested review/confirmation state with the outer row decision."""
    metadata: dict[str, object] = {**existing_metadata, "review_required": False}
    agentic_raw = metadata.get("agentic_memory")
    if not isinstance(agentic_raw, dict):
        return metadata

    agentic: dict[str, object] = {**agentic_raw}
    confirmation_raw = agentic.get("confirmation")
    if isinstance(confirmation_raw, dict) and confirmation_raw.get("status") == "pending":
        timestamp_key = "confirmed_at" if outcome == "confirmed" else "rejected_at"
        agentic["confirmation"] = {
            **confirmation_raw,
            "status": outcome,
            timestamp_key: terminal_at,
        }

    if outcome == "confirmed":
        agentic.update(
            {
                "status": "committed",
                "write_mode": "commit",
                "lifecycle_status": "dashboard_review_accepted",
                "confirmed_at": terminal_at,
                "requires_dashboard_review": False,
            }
        )
    else:
        agentic.update(
            {
                "status": "rejected",
                "lifecycle_status": "review_rejected",
                "requires_dashboard_review": False,
            }
        )
    metadata["agentic_memory"] = agentic
    return metadata


@source_create_router.post("/v0/vnext/sources")
def create_vnext_source(
    request: VNextSourceCaptureRequest,
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
                action="source.capture",
                domains=(request.domain,),
                sensitivity_allowed=(request.sensitivity,),
                project_scope=tuple(request.project_scope),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, actor_id = _vnext_agent_actor(identity, fallback="user")
            capture_result = VNextCaptureService(
                store,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=request.trace_id or decision.trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
                agent_identity=identity.to_record() if identity is not None else None,
                policy_decision=decision.to_record(),
                defer_embeddings=True,
            ).capture_text(
                request.raw_text,
                title=request.title,
                domain=request.domain,
                sensitivity=request.sensitivity,
                project_scope=decision.effective_project_scope,
            )
            payload = capture_result.to_record()
            if identity is not None:
                append_policy_events(
                    store,
                    identity=identity,
                    decision=decision,
                    target_type="source",
                    target_id=str(payload.get("source_id")),
                )
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=capture_result,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=request.trace_id or decision.trace_id,
        )
    except VNextCaptureValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext source capture request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@connectors_router.get("/v0/vnext/connectors")
def list_vnext_connectors(user_id: UUID) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        service = VNextConnectorService(PostgresVNextStore(conn))
        definitions = list_connector_definitions()
        payload = {
            "items": [
                {
                    **definition.to_record(),
                    "config": service.get_config(definition.name),
                    "health": service.connector_health(definition.name),
                }
                for definition in definitions
            ],
            "count": len(definitions),
            "order": [definition.name for definition in definitions],
        }
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.get("/v0/vnext/connectors/health")
def get_vnext_connectors_health(user_id: UUID) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        payload = VNextConnectorService(PostgresVNextStore(conn)).connector_health_all()
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.get("/v0/vnext/connectors/{connector_name}/status")
def get_vnext_connector_status(connector_name: str, user_id: UUID) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            service = VNextConnectorService(store)
            sources = [
                source for source in store.list_sources(limit=50) if source.get("connector_name") == connector_name
            ]
            failures = [
                event
                for event in store.list_events(target_type="connector", target_id=connector_name, limit=50)
                if event.get("event_type") in {"connector.item_failed", "connector.sync_failed"}
            ]
            payload = {
                "config": service.get_config(connector_name),
                "health": service.connector_health(connector_name),
                "recent_captures": sources[:10],
                "recent_failures": failures[:10],
            }
    except VNextConnectorValidationError:
        return _vnext_public_error_response(status_code=404, detail="vNext connector was not found")
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.patch("/v0/vnext/connectors/{connector_name}/config")
def update_vnext_connector_config(connector_name: str, request: VNextConnectorConfigRequest) -> JSONResponse:
    settings = get_settings()
    if connector_name == "telegram" and (
        request.secret_ref is not None
        or request.poll_interval_seconds is not None
        or request.sync_mode not in {None, "on_demand"}
    ):
        return _vnext_public_error_response(
            status_code=400,
            detail="Telegram source ingestion is on-demand and does not accept polling or secret configuration",
        )
    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            payload = VNextConnectorService(PostgresVNextStore(conn)).update_config(
                connector_name,
                enabled=request.enabled,
                default_domain=request.default_domain,
                default_sensitivity=request.default_sensitivity,
                secret_ref=request.secret_ref,
                sync_mode=request.sync_mode,
                poll_interval_seconds=request.poll_interval_seconds,
                config_json=request.config_json,
            )
    except VNextConnectorValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext connector config request is invalid")
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.post("/v0/vnext/connectors/{connector_name}/sync")
def sync_vnext_connector(connector_name: str, request: VNextConnectorSyncRequest) -> JSONResponse:
    settings = get_settings()
    if connector_name == "telegram":
        return _vnext_public_error_response(
            status_code=400,
            detail="use /v0/vnext/connectors/telegram/sync for allowlist-aware Telegram ingestion",
        )

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            result = VNextConnectorService(PostgresVNextStore(conn), defer_embeddings=True).sync_items(
                connector_name,
                request.items,
                default_domain=request.default_domain,
                default_sensitivity=request.default_sensitivity,
            )
            payload = result.to_record()
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=result,
        )
    except VNextConnectorValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext connector sync request is invalid")

    status_code = 201
    if payload["status"] == "partial":
        status_code = 207
    elif payload["status"] == "failed":
        status_code = 400
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


@connectors_router.post("/v0/vnext/connectors/telegram/sync")
def sync_vnext_telegram_connector(request: VNextTelegramSyncRequest) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            service = VNextConnectorService(PostgresVNextStore(conn), defer_embeddings=True)
            result = service.sync_telegram_updates(
                request.updates,
                allowed_chat_ids=request.allowed_chat_ids,
                default_domain=request.default_domain,
                default_sensitivity=request.default_sensitivity,
            )
            payload = result.to_record()
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=result,
        )
    except VNextConnectorValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext Telegram sync request is invalid")
    return JSONResponse(
        status_code=201 if payload["status"] in {"ok", "partial"} else 400, content=jsonable_encoder(payload)
    )


@connectors_router.post("/v0/vnext/connectors/local-folder/sync")
def sync_vnext_local_folder_connector(request: VNextLocalFolderSyncRequest) -> JSONResponse:
    settings = get_settings()
    try:
        paths = list(request.paths)
        with user_connection(settings.database_url, request.user_id) as conn:
            if not paths:
                config = VNextConnectorService(PostgresVNextStore(conn)).get_config("local_folder")
                config_json_value = config.get("config_json")
                config_json: dict[str, object] = config_json_value if isinstance(config_json_value, dict) else {}
                configured_paths_value = config_json.get("paths")
                configured_paths = configured_paths_value if isinstance(configured_paths_value, list) else []
                paths = [str(path) for path in configured_paths if isinstance(path, str)]

        # File traversal and reads are intentionally outside the transaction so
        # slow or remote mounts cannot monopolize a pooled database connection.
        scan = scan_local_folder(
            paths,
            recursive=request.recursive,
            extensions=request.extensions,
            ignore_patterns=request.ignore_patterns,
        )
        with user_connection(settings.database_url, request.user_id) as conn:
            result = VNextConnectorService(PostgresVNextStore(conn), defer_embeddings=True).sync_local_folder_scan(
                scan,
                default_domain=request.default_domain,
                default_sensitivity=request.default_sensitivity,
            )
            payload = result.to_record()
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=result,
        )
    except VNextConnectorValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext local folder sync request is invalid")
    return JSONResponse(
        status_code=201 if payload["status"] in {"ok", "partial", "duplicate"} else 400,
        content=jsonable_encoder(payload),
    )


@connectors_router.post("/v0/vnext/connectors/browser-clipper/capture")
def capture_vnext_browser_clip(
    request: VNextBrowserClipperCaptureRequest,
    origin: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            capability_authorized = request.capture_capability is not None
            if capability_authorized:
                if request.capture_token is not None:
                    raise BrowserClipCapabilityValidationError(
                        "browser clip credentials are mutually exclusive"
                    )
                consume_browser_clip_capability(
                    store,
                    capability=request.capture_capability or "",
                    capture_url=request.url,
                    request_origin=origin if isinstance(origin, str) else None,
                )
            result = VNextConnectorService(store, defer_embeddings=True).capture_browser_clip(
                request.model_dump(mode="json"),
                default_domain=request.domain,
                default_sensitivity=request.sensitivity,
                capability_authorized=capability_authorized,
            )
            payload = result.to_record()
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=result,
        )
    except (BrowserClipCapabilityValidationError, VNextConnectorValidationError):
        return _vnext_public_error_response(status_code=400, detail="vNext browser clip capture request is invalid")
    return JSONResponse(
        status_code=201 if payload["status"] in {"ok", "partial", "duplicate"} else 400,
        content=jsonable_encoder(payload),
    )


@connectors_router.post("/v0/vnext/connectors/browser-clipper/capabilities")
def create_vnext_browser_clip_capability(
    request: VNextBrowserClipperCapabilityRequest,
) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            issued = issue_browser_clip_capability(
                PostgresVNextStore(conn),
                origin=request.origin,
            )
            payload = issued.to_record()
    except BrowserClipCapabilityValidationError:
        return _vnext_public_error_response(
            status_code=400,
            detail="vNext browser clip capability request is invalid",
        )
    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@connectors_router.post("/v0/vnext/agents/ingest-output")
def ingest_vnext_agent_output(
    request: VNextAgentOutputIngestRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            _vnext_agent_record(store, identity)
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="source.capture",
                domains=(request.domain,),
                sensitivity_allowed=(request.sensitivity,),
                project_scope=tuple(request.project_scope),
                target_type="connector",
                target_id="agent_output",
                write_policy="proposal_only" if request.propose_memory else None,
            )
            ingest_payload = request.model_dump(mode="json")
            ingest_payload["project_scope"] = list(decision.effective_project_scope)
            result = VNextConnectorService(store, defer_embeddings=True).ingest_agent_output(
                ingest_payload,
                policy_decision=decision.to_record(),
            )
            payload = result.to_record()
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=result,
            actor_type="agent",
            actor_id=identity.agent_id if identity is not None else None,
            trace_id=decision.trace_id,
        )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    except (AgentIdentityValidationError, VNextConnectorValidationError):
        return _vnext_public_error_response(status_code=400, detail="vNext agent output ingest request is invalid")
    return JSONResponse(status_code=201, content=jsonable_encoder(payload))


@connectors_router.get("/v0/vnext/dogfooding")
def get_vnext_dogfooding_dashboard(user_id: UUID) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        payload = VNextDogfoodingService(PostgresVNextStore(conn)).dashboard()
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.get("/v0/vnext/doctor")
def get_vnext_doctor(user_id: UUID, ci: bool = True) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        payload = VNextDoctorService(PostgresVNextStore(conn)).run(ci=ci)
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@connectors_router.post("/v0/vnext/doctor/run")
def run_vnext_doctor(request: VNextDoctorRunRequest) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, request.user_id) as conn:
        payload = VNextDoctorService(PostgresVNextStore(conn)).run(fix_safe=request.fix_safe, ci=request.ci)
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@source_review_router.get("/v0/vnext/sources/{source_id}")
def get_vnext_source(source_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        payload = PostgresVNextStore(conn).get_source(str(source_id))

    if payload is None:
        return JSONResponse(status_code=404, content={"detail": f"vNext source {source_id} was not found"})

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@source_review_router.post("/v0/vnext/sources/{source_id}/review")
def review_vnext_source(source_id: UUID, request: VNextSourceReviewRequest) -> JSONResponse:
    settings = get_settings()
    action = request.action.strip().casefold()
    if action not in {"review", "update", "assign_project", "archive"}:
        return _vnext_public_error_response(status_code=400, detail="vNext source review action is invalid")

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            existing = store.get_source(str(source_id))
            if existing is None:
                return _vnext_public_error_response(status_code=404, detail="vNext source was not found")
            if action == "archive":
                archived = store.delete_source(source_id=str(source_id), actor_type="user")
                append_event(
                    store,
                    event_type="source.archived",
                    actor_type="user",
                    target_type="source",
                    target_id=str(source_id),
                    payload={"action": action, "review_note": request.review_note},
                )
                trace = _vnext_load_source_trace(
                    store=store,
                    source=archived,
                )
                return JSONResponse(
                    status_code=200,
                    content=jsonable_encoder({"source": archived, "archived": True, "trace": trace}),
                )

            if action == "assign_project" and request.project_id is None:
                return _vnext_public_error_response(status_code=400, detail="project_id is required")

            metadata = {
                **_vnext_metadata(existing),
                "review_status": "reviewed" if action == "review" else "updated",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "review_note": request.review_note,
                "updated_from": "vnext_workspace",
            }
            if request.project_id is not None:
                metadata["project_id"] = request.project_id
                if action == "assign_project":
                    # project_scope is the canonical, overlap-aware scope used by
                    # retrieval.  Replace it together with the singular legacy
                    # pointer so a reassignment cannot leave the source readable
                    # through its previous project.
                    metadata["project_scope"] = [request.project_id]
            patch: dict[str, object] = {"metadata_json": metadata}
            if request.title is not None:
                patch["title"] = request.title
            if request.domain is not None:
                patch["domain"] = request.domain
            if request.sensitivity is not None:
                patch["sensitivity"] = request.sensitivity
            updated = store.update_source(source_id=str(source_id), patch=patch, actor_type="user")
            if action == "assign_project":
                store.create_edge(
                    {
                        "from_type": "source",
                        "from_id": str(source_id),
                        "to_type": "project",
                        "to_id": request.project_id,
                        "edge_type": "belongs_to_project",
                        "confidence": 1.0,
                        "explanation": "Assigned from live /vnext source review.",
                        "created_by": "user",
                        "metadata_json": {"review_action": action},
                    },
                    actor_type="user",
                )
            append_event(
                store,
                event_type={
                    "review": "source.reviewed",
                    "update": "source.updated_from_workspace",
                    "assign_project": "source.assigned_project",
                }[action],
                actor_type="user",
                target_type="source",
                target_id=str(source_id),
                payload={"action": action, "project_id": request.project_id, "review_note": request.review_note},
            )
            trace = _vnext_load_source_trace(
                store=store,
                source=updated,
            )
    except ContinuityStoreInvariantError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=200, content=jsonable_encoder({"source": updated, "archived": False, "trace": trace})
    )


@source_delete_router.delete("/v0/vnext/sources/{source_id}")
def delete_vnext_source(source_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()

    with user_connection(settings.database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        existing = store.get_source(str(source_id))
        if existing is None:
            return JSONResponse(status_code=404, content={"detail": f"vNext source {source_id} was not found"})
        payload = store.delete_source(source_id=str(source_id))

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(payload),
    )


@memory_router.post("/v0/vnext/memories/{memory_id}/review")
def review_vnext_memory(
    memory_id: UUID,
    request: VNextMemoryReviewRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    action = request.action.strip().casefold()
    if action not in {"accept", "edit", "reject", "private", "assign_project", "promote"}:
        return _vnext_public_error_response(status_code=400, detail="vNext memory review action is invalid")

    try:
        _vnext_agent_identity(request)
        with user_connection(settings.database_url, request.user_id) as conn:
            auth_store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                auth_store,
                request,
                user_id=request.user_id,
                authorization=authorization,
            )
            target = auth_store.get_memory(str(memory_id))
            if target is None:
                return _vnext_public_error_response(status_code=404, detail="vNext memory was not found")
            target_scope = resource_project_scope(target)
            if action == "assign_project" and request.project_id is not None:
                target_scope = tuple(dict.fromkeys((*target_scope, request.project_id)))
            decision = _vnext_policy_checked(
                store=auth_store,
                identity=identity,
                action="memory.review",
                domains=(str(target.get("domain") or "unknown"),),
                sensitivity_allowed=(str(target.get("sensitivity") or "unknown"),),
                project_scope=target_scope,
                target_type="memory",
                target_id=str(memory_id),
                require_explicit_project_scope=True,
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)

    actor_type, actor_id = _vnext_agent_actor(identity, fallback="user")

    # Consolidation acceptance has its own graph-wide locking protocol. Run it
    # in a complete primary transaction, then perform optional embedding work
    # only after that transaction has committed.
    if is_pending_consolidation_candidate(target) and action in {"accept", "promote"}:
        if action == "edit" or any(
            value is not None
            for value in (
                request.title,
                request.canonical_text,
                request.summary,
                request.domain,
                request.sensitivity,
                request.project_id,
            )
        ):
            return _vnext_public_error_response(
                status_code=400,
                detail=(
                    "pending consolidation candidates cannot be edited during approval; "
                    "regenerate the candidate or accept it unchanged"
                ),
            )
        try:
            with user_connection(settings.database_url, request.user_id) as conn:
                consolidation_service = VNextMemoryCommitService(
                    PostgresVNextStore(conn),
                    defer_embeddings=True,
                )
                # Preserve the route-level graph boundary before the service
                # reacquires it and locks the candidate/member rows.
                consolidation_service.lock_supersession_graph()
                acceptance = consolidation_service.accept_consolidation_candidate(
                    str(memory_id),
                    reason=request.reason or "Approved through vNext memory review.",
                    identity=identity,
                )
        except AgentPolicyBlockedError as exc:
            return _vnext_permission_response(exc.decision)
        except VNextMemoryCommitValidationError as exc:
            return public_exception_response(exc, status_code=400)
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=consolidation_service,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder(
                {
                    "memory": acceptance["memory"],
                    "consolidation_acceptance": acceptance,
                }
            ),
        )

    with user_connection(settings.database_url, request.user_id) as conn:
        store = PostgresVNextStore(conn)
        memory_service = VNextMemoryCommitService(store, defer_embeddings=True)
        # Review can promote a consolidation candidate or mutate a member
        # referenced by pending derived work. Establish the shared per-user
        # graph boundary before the route takes any candidate/member row lock;
        # delegated service calls may safely reacquire the transaction lock.
        memory_service.lock_supersession_graph()
        preview = store.get_memory(str(memory_id))
        if preview is None:
            return _vnext_public_error_response(status_code=404, detail="vNext memory was not found")
        # Delegate consolidation approval before this adapter takes a row lock.
        # The service reacquires the already-held transaction advisory lock
        # (non-blocking/re-entrant) and then owns all candidate/member locks.
        if is_pending_consolidation_candidate(preview):
            if action == "edit" or any(
                value is not None
                for value in (
                    request.title,
                    request.canonical_text,
                    request.summary,
                    request.domain,
                    request.sensitivity,
                    request.project_id,
                )
            ):
                return _vnext_public_error_response(
                    status_code=400,
                    detail=(
                        "pending consolidation candidates cannot be edited during approval; "
                        "regenerate the candidate or accept it unchanged"
                    ),
                )
            if action in {"accept", "promote"}:
                return _vnext_public_error_response(
                    status_code=409,
                    detail="vNext memory became a consolidation candidate during review; retry the approval",
                )
        get_memory_for_update = getattr(store, "get_memory_for_update", None)
        existing = (
            get_memory_for_update(str(memory_id))
            if callable(get_memory_for_update)
            else store.get_memory(str(memory_id))
        )
        if existing is None:
            return _vnext_public_error_response(status_code=404, detail="vNext memory was not found")
        # Re-authorize the locked record so a concurrent reassignment cannot
        # move it outside the bound agent project between the first check and
        # this mutation.
        locked_scope = resource_project_scope(existing)
        if action == "assign_project" and request.project_id is not None:
            locked_scope = tuple(dict.fromkeys((*locked_scope, request.project_id)))
        locked_decision = _vnext_policy_checked(
            store=store,
            identity=identity,
            action="memory.review",
            domains=(str(existing.get("domain") or "unknown"),),
            sensitivity_allowed=(str(existing.get("sensitivity") or "unknown"),),
            project_scope=locked_scope,
            target_type="memory",
            target_id=str(memory_id),
            require_explicit_project_scope=True,
        )
        if locked_decision.decision == "blocked":
            return _vnext_permission_response(locked_decision)
        if str(existing.get("status") or "") in {"archived", "rejected", "superseded"}:
            return _vnext_public_error_response(
                status_code=409,
                detail=f"vNext memory cannot be reviewed from status '{existing.get('status')}'",
            )
        if is_pending_consolidation_candidate(existing):
            if action == "edit" or any(
                value is not None
                for value in (
                    request.title,
                    request.canonical_text,
                    request.summary,
                    request.domain,
                    request.sensitivity,
                    request.project_id,
                )
            ):
                return _vnext_public_error_response(
                    status_code=400,
                    detail=(
                        "pending consolidation candidates cannot be edited during approval; "
                        "regenerate the candidate or accept it unchanged"
                    ),
                )
            if action in {"accept", "promote"}:
                return _vnext_public_error_response(
                    status_code=409,
                    detail="vNext memory became a consolidation candidate during review; retry the approval",
                )
        if is_pending_project_update_memory(existing):
            return _vnext_public_error_response(
                status_code=409,
                detail=PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
            )

        existing_metadata_value = existing.get("metadata_json")
        existing_metadata: dict[str, object] = (
            existing_metadata_value if isinstance(existing_metadata_value, dict) else {}
        )
        reviewed_at = datetime.now(UTC).isoformat()
        patch: dict[str, object] = {
            "last_reviewed_at": reviewed_at,
        }
        revision_type = "edited"
        if action == "accept":
            patch["status"] = "active"
            revision_type = "promoted"
        elif action == "reject":
            patch["status"] = "rejected"
            patch["metadata_json"] = _vnext_terminal_review_metadata(
                existing_metadata,
                outcome="rejected",
                terminal_at=reviewed_at,
            )
            revision_type = "rejected"
        elif action == "private":
            patch["status"] = "private_only"
            patch["sensitivity"] = "private"
        elif action == "promote":
            patch["status"] = "active"
            patch["confirmation_status"] = "confirmed"
            revision_type = "promoted"
        elif action == "assign_project":
            if request.project_id is None:
                return _vnext_public_error_response(status_code=400, detail="project_id is required")
            # Keep every current scope representation in the same UPDATE.  A
            # metadata-only project_id write leaves an older project_scope in
            # place, and canonical retrieval correctly gives that array
            # precedence over the legacy singular fallback.
            patch["project_id"] = request.project_id
            patch["metadata_json"] = {
                **existing_metadata,
                "project_id": request.project_id,
                "project_scope": [request.project_id],
                "assigned_from": "vnext_workspace",
            }
        else:
            patch["status"] = "active"

        if action in {"accept", "edit", "promote"}:
            patch.update(
                {
                    "confirmation_status": "confirmed",
                    "last_confirmed_at": reviewed_at,
                    "metadata_json": _vnext_terminal_review_metadata(
                        existing_metadata,
                        outcome="confirmed",
                        terminal_at=reviewed_at,
                    ),
                }
            )

        if request.title is not None:
            patch["title"] = request.title
        if request.canonical_text is not None:
            patch["canonical_text"] = request.canonical_text
            existing_value = existing.get("value")
            patch["value"] = {
                **(existing_value if isinstance(existing_value, dict) else {}),
                "text": request.canonical_text,
            }
            # Capture-generated title/summary are denormalized views of the
            # canonical text.  Editing only the body must not leave those
            # user-visible fields describing the pre-edit value.
            if request.title is None:
                patch["title"] = (
                    request.canonical_text
                    if len(request.canonical_text) <= 120
                    else request.canonical_text[:117].rstrip() + "..."
                )
            if request.summary is None:
                patch["summary"] = (
                    request.canonical_text
                    if len(request.canonical_text) <= 280
                    else request.canonical_text[:277].rstrip() + "..."
                )
        if request.summary is not None:
            patch["summary"] = request.summary
        if request.domain is not None:
            patch["domain"] = request.domain
        if request.sensitivity is not None:
            patch["sensitivity"] = request.sensitivity

        updated = store.update_memory(memory_id=str(memory_id), patch=patch, actor_type=actor_type)
        if action in ("accept", "edit", "promote"):
            memory_service.refresh_memory_derived_state(
                updated,
                identity=identity,
                stage=f"http_review_{action}",
            )
        if action == "assign_project" and request.project_id is not None:
            store.create_edge(
                {
                    "from_type": "memory",
                    "from_id": str(memory_id),
                    "to_type": "project",
                    "to_id": request.project_id,
                    "edge_type": "belongs_to_project",
                    "confidence": 1.0,
                    "explanation": "Assigned from live /vnext memory review.",
                    "created_by": "user",
                    "metadata_json": {"review_action": action},
                },
                actor_type=actor_type,
            )
        store.append_revision(
            {
                "memory_id": str(memory_id),
                "memory_key": str(updated["memory_key"]),
                "previous_value": existing.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": revision_type,
                "action": f"memory_review_{action}",
                "text_before": existing.get("canonical_text"),
                "text_after": str(updated.get("canonical_text", "")),
                "reason": request.reason or f"vNext workspace memory review action: {action}",
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"action": action, "project_id": request.project_id},
            },
            actor_type=actor_type,
        )
        review_event = {
            "accept": "review.item_accepted",
            "promote": "review.item_accepted",
            "reject": "review.item_rejected",
            "edit": "review.item_edited",
            "private": "review.item_edited",
            "assign_project": "review.item_edited",
        }[action]
        append_event(
            store,
            event_type=review_event,
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory_id),
            payload={"action": action, "project_id": request.project_id},
        )

    _persist_vnext_deferred_embeddings(
        database_url=settings.database_url,
        user_id=request.user_id,
        result=memory_service,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return JSONResponse(status_code=200, content=jsonable_encoder({"memory": updated}))


def _vnext_memory_type_for_proposal(proposal_type: str) -> str:
    mapping = {
        "candidate_memory": "semantic",
        "project_update": "project_state",
        "open_loop": "open_loop",
        "belief_update": "belief",
        "contradiction": "contradiction",
        "graph_edge": "semantic",
        "artifact_summary": "artifact_summary",
        "decision": "decision",
        "recent_change": "semantic",
    }
    return mapping.get(proposal_type, "semantic")


@memory_router.post("/v0/vnext/memory-proposals")
def create_vnext_memory_proposal(
    request: VNextMemoryProposalRequest,
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
            if identity is None:
                return _vnext_public_error_response(
                    status_code=400, detail="agent identity is required for memory proposals"
                )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="memory.propose",
                domains=(request.domain,),
                sensitivity_allowed=(request.sensitivity,),
                project_scope=tuple(request.project_scope),
                promotion_settings=load_promotion_settings(brain_charter=_brain_charter_row(store)),
                promotion_candidate=promotion_candidate_for_proposal(
                    canonical_text=request.canonical_text,
                    title=request.title,
                    domain=request.domain,
                    sensitivity=request.sensitivity,
                    source_type=getattr(request, "source_type", None) or "trusted_agent",
                    source_refs=request.source_refs,
                ),
                owner_verified=agent_api_keys_provisioned(store),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            # A promoted proposal is a live memory, not a review item. With no
            # persona configured review_required stays True and this stays the
            # candidate row it always was.
            review_required = decision.review_required
            proposal_id = str(uuid4())
            metadata = {
                "proposal_id": proposal_id,
                "proposal_type": request.proposal_type,
                "source_refs": request.source_refs,
                "project_scope": list(decision.effective_project_scope),
                "rationale": request.rationale,
                "review_required": review_required,
                **agent_metadata(identity, decision),
            }
            memory = store.create_memory(
                {
                    "memory_type": _vnext_memory_type_for_proposal(request.proposal_type),
                    "memory_key": f"agent_proposal.{request.proposal_type}.{proposal_id}",
                    "value": {
                        "proposal_type": request.proposal_type,
                        "text": request.canonical_text,
                        "source_refs": request.source_refs,
                        "rationale": request.rationale,
                    },
                    "status": "candidate" if review_required else "active",
                    "project_id": (
                        decision.effective_project_scope[0] if len(decision.effective_project_scope) == 1 else None
                    ),
                    "confidence": request.confidence,
                    "title": request.title,
                    "canonical_text": request.canonical_text,
                    "summary": request.canonical_text[:280],
                    "domain": request.domain,
                    "sensitivity": request.sensitivity,
                    "metadata_json": metadata,
                },
                actor_type="agent",
            )
            store.append_revision(
                {
                    "memory_id": str(memory["id"]),
                    "memory_key": str(memory["memory_key"]),
                    "new_value": memory.get("value"),
                    "revision_type": "created",
                    "action": "agent_memory_proposal",
                    "text_after": request.canonical_text,
                    "reason": request.rationale or "Agent proposed memory for human review.",
                    "actor_type": "agent",
                    "actor_id": identity.agent_id,
                    "metadata_json": metadata,
                },
                actor_type="agent",
            )
            append_event(
                store,
                event_type="agent.memory_proposed",
                actor_type="agent",
                actor_id=identity.agent_id,
                target_type="memory",
                target_id=str(memory["id"]),
                trace_id=request.trace_id or decision.trace_id,
                run_id=identity.agent_run_id,
                payload={
                    "proposal_type": request.proposal_type,
                    "agent_identity": identity.to_record(),
                    "policy_decision": decision.to_record(),
                },
            )
            if review_required:
                append_event(
                    store,
                    event_type="review.item_created",
                    actor_type="agent",
                    actor_id=identity.agent_id,
                    target_type="memory",
                    target_id=str(memory["id"]),
                    trace_id=request.trace_id or decision.trace_id,
                    run_id=identity.agent_run_id,
                    payload={"review_required": True, "proposal_type": request.proposal_type},
                )
            append_promotion_event(
                store,
                identity=identity,
                decision=decision,
                target_type="memory",
                target_id=str(memory["id"]),
                trace_id=request.trace_id or decision.trace_id,
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "proposal": memory,
                "policy_decision": decision.to_record(),
                "review_required": review_required,
            }
        ),
    )


@memory_router.post("/v0/vnext/memories/commit")
def commit_vnext_memory(
    request: VNextMemoryCommitRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        identity = _vnext_agent_identity(request)
        commit_request = memory_commit_request_from_payload(
            request.model_dump(mode="json", exclude={"agent", "agent_identity"}),
            user_id=request.user_id,
        )
    except (AgentIdentityValidationError, VNextMemoryCommitValidationError) as exc:
        return public_exception_response(exc, status_code=400)

    try:
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            # This route resolves identity through
            # resolve_protected_agent_identity, which rejects a keyless
            # request once any key exists. Getting here with no agent
            # identity on a keyed install therefore does mean the
            # authenticated human, and this is the only surface that can say
            # so.
            service = VNextMemoryCommitService(
                store,
                defer_embeddings=True,
                owner_verified=agent_api_keys_provisioned(store),
            )
            payload = service.commit(identity=identity, request=commit_request)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    _persist_vnext_deferred_embeddings(
        database_url=settings.database_url,
        user_id=request.user_id,
        result=service,
        actor_type="agent" if identity is not None else "user",
        actor_id=identity.agent_id if identity is not None else None,
        trace_id=commit_request.trace_id,
    )
    status_code = 201 if payload.get("status") in {"committed", "confirmation_required", "review_required"} else 200
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/confirm")
def confirm_vnext_memory(
    request: VNextMemoryConfirmRequest,
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
            service = VNextMemoryCommitService(store, defer_embeddings=True)
            # Return inside the connection so a refusal's policy event
            # commits. Raising out of user_connection rolls it back.
            try:
                payload = service.confirm(
                    identity=identity,
                    confirmation_id=request.confirmation_id,
                    action=request.action,
                    canonical_text=request.canonical_text,
                    rationale=request.rationale,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    _persist_vnext_deferred_embeddings(
        database_url=settings.database_url,
        user_id=request.user_id,
        result=service,
        actor_type="agent" if identity is not None else "user",
        actor_id=identity.agent_id if identity is not None else None,
    )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/undo")
def undo_vnext_memory(
    request: VNextMemoryUndoRequest,
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
            try:
                payload = VNextMemoryCommitService(store).undo(
                    identity=identity,
                    memory_id=str(request.memory_id) if request.memory_id is not None else None,
                    reason=request.reason,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/correct")
def correct_vnext_memory(
    request: VNextMemoryCorrectRequest,
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
            service = VNextMemoryCommitService(store, defer_embeddings=True)
            try:
                payload = service.correct(
                    identity=identity,
                    memory_id=str(request.memory_id),
                    canonical_text=request.canonical_text,
                    reason=request.reason,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    _persist_vnext_deferred_embeddings(
        database_url=settings.database_url,
        user_id=request.user_id,
        result=service,
        actor_type="agent" if identity is not None else "user",
        actor_id=identity.agent_id if identity is not None else None,
    )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/forget")
def forget_vnext_memory(
    request: VNextMemoryForgetRequest,
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
            try:
                payload = VNextMemoryCommitService(store).forget(
                    identity=identity,
                    memory_id=str(request.memory_id),
                    reason=request.reason,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/expire")
def expire_vnext_memory(
    request: VNextMemoryExpireRequest,
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
            # The commit service policy-checks memory.expire itself (and
            # appends the policy events); returning from inside the store
            # context keeps the blocked-decision audit events committed.
            try:
                payload = VNextMemoryCommitService(store).expire(
                    str(request.memory_id),
                    valid_to=request.valid_to,
                    reason=request.reason,
                    identity=identity,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/unexpire")
def unexpire_vnext_memory(
    request: VNextMemoryUnexpireRequest,
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
            try:
                payload = VNextMemoryCommitService(store).unexpire(
                    str(request.memory_id),
                    reason=request.reason,
                    identity=identity,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/accept-consolidation")
def accept_vnext_memory_consolidation(
    request: VNextMemoryAcceptConsolidationRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    service: VNextMemoryCommitService | None = None
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
            # Acceptance is a review decision: the commit service
            # policy-checks it internally (human or admin agent only).
            try:
                service = VNextMemoryCommitService(store, defer_embeddings=True)
                payload = service.accept_consolidation_candidate(
                    str(request.memory_id),
                    reason=request.reason,
                    identity=identity,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=400)

    if service is not None:
        actor_type, actor_id = _vnext_agent_actor(identity, fallback="user")
        _persist_vnext_deferred_embeddings(
            database_url=settings.database_url,
            user_id=request.user_id,
            result=service,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.post("/v0/vnext/memories/redact")
def redact_vnext_memory(
    request: VNextMemoryRedactRequest,
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
            try:
                payload = redact_memory_flow(
                    store,
                    memory_id=str(request.memory_id),
                    reason=request.reason,
                    identity=identity,
                )
            except AgentPolicyBlockedError as exc:
                return _vnext_permission_response(exc.decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextMemoryCommitValidationError as exc:
        if str(exc) == "memory was not found":
            return _vnext_public_error_response(status_code=404, detail="vNext memory was not found")
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.get("/v0/vnext/memories/recent-commits")
def list_vnext_recent_memory_commits(user_id: UUID, limit: int = Query(default=20, ge=1, le=100)) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        payload = VNextMemoryCommitService(store).recent_commits(limit=limit)
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@memory_router.get("/v0/vnext/memories/{memory_id}/audit")
def get_vnext_memory_audit(memory_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            payload = VNextMemoryCommitService(store).audit(memory_id=str(memory_id))
    except VNextMemoryCommitValidationError as exc:
        return public_exception_response(exc, status_code=404)
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))
