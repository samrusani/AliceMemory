from __future__ import annotations

from dataclasses import replace
from datetime import UTC
import ipaddress
import json
from typing import Any, Awaitable, Callable, Literal, TypedDict
from uuid import UUID
from fastapi import (
    FastAPI,
    Header,
    Request,
    Response,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match
from urllib.parse import parse_qsl, urlencode

from alicebot_api import __version__
from alicebot_api.surface_flags import legacy_surfaces_enabled
from alicebot_api.config import Settings, get_settings
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers import (
    continuity,
    legacy_gated,
    memories_legacy,
    vnext_memories,
    vnext_projects,
    vnext_retrieval,
    vnext_review,
)
from alicebot_api.routers._api_shared import (
    AUTH_USER_HEADER as AUTH_USER_HEADER,
    LOGGER as LOGGER,
    _json_object as _json_object,
    _json_value as _json_value,
    _request_client_identifier,
    _resolve_authenticated_user_id,
    _resolve_authenticated_v1_user_id,
)
from alicebot_api.routers._vnext_shared import (
    VNextAgentIdentityRequest,
    VNextAgentRequest,
    VNextDomain,
    VNextSensitivity,
    _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT,
    _vnext_agent_actor,
    _vnext_agent_auth_error_response,
    _vnext_agent_identity,
    _vnext_agent_record,
    _vnext_authenticated_agent_identity,
    _vnext_authorized_artifact,
    _vnext_bounded_trace_rows,
    _vnext_event_references,
    _vnext_exact_resource_policy,
    _vnext_load_source_trace,
    _vnext_metadata,
    _vnext_payload,
    _vnext_permission_response,
    _vnext_policy_checked,
    _vnext_public_error_response,
    _vnext_ref_matches_source,
    _vnext_ref_values,
    _vnext_row_references_source,
    _vnext_source_chunks,
    _vnext_string_list,
)
from alicebot_api.contracts import (
    AgentProfileListResponse,
    ContinuityArtifactDetailResponse,
    ContinuityBriefResponse,
    ContradictionCaseDetailResponse,
    ContradictionCaseListResponse,
    ContradictionResolveResponse,
    ContradictionSyncResponse,
    ContinuityExplainResponse,
    ContinuityLifecycleDetailResponse,
    ContinuityLifecycleListResponse,
    ContinuityDailyBriefResponse,
    ContinuityOpenLoopDashboardResponse,
    ContinuityOpenLoopReviewActionResponse,
    ContinuityRecallResponse,
    ContinuityReviewDetailResponse,
    ContinuityReviewQueueResponse,
    ContinuityResumptionBriefResponse,
    TaskBriefComparisonResponse,
    TaskBriefResponse,
    TemporalExplainResponse,
    TemporalStateAtResponse,
    TemporalTimelineResponse,
    TrustedFactPatternExplainResponse,
    TrustedFactPatternListResponse,
    TrustedFactPlaybookExplainResponse,
    TrustedFactPlaybookListResponse,
    ContinuityWeeklyReviewResponse,
    MemoryHygieneDashboardResponse,
    MemoryTrustDashboardResponse,
    RetrievalEvaluationResponse,
    RetrievalRunListResponse,
    RetrievalTraceResponse,
    ThreadHealthDashboardResponse,
    TrustSignalListResponse,
    PublicEvalRunDetailResponse,
    PublicEvalRunListResponse,
    PublicEvalSuiteDefinitionListResponse,
    ThreadCreateResponse,
    ThreadDetailResponse,
    ThreadEventListResponse,
    ThreadListResponse,
    ResumptionBriefResponse,
    ThreadSessionListResponse,
)
from alicebot_api.db import ping_database, user_connection
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    PolicyDecision,
    append_policy_events,
    evaluate_agent_policy,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    agent_key_from_authorization,
    resolve_protected_agent_identity,
)
from alicebot_api.vnext_project_scope import source_project_scope
from alicebot_api.vnext_project_update_guard import (
    PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
    is_pending_project_update_memory,
)
from alicebot_api.vnext_capture import VNextCaptureService, VNextCaptureValidationError
from alicebot_api.vnext_connectors import VNextConnectorValidationError, list_connector_definitions, scan_local_folder
from alicebot_api.vnext_context_tree import (
    ContextTreeRequest,
    VNextContextTreeService,
    VNextContextTreeStore,
    VNextContextTreeValidationError,
)
from alicebot_api.mcp_tools import redact_memory_flow
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitValidationError,
    is_pending_consolidation_candidate,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService, VNextRetrievalValidationError
from alicebot_api.vnext_store import PostgresVNextStore, is_redacted_project_update_artifact
from alicebot_api.openapi_operation_contracts import (
    OPENAPI_INTENTIONALLY_POLYMORPHIC_OPERATIONS,
    OPENAPI_OPEN_RESPONSE_OPERATIONS,
    OPENAPI_OPERATION_RESPONSE_SCHEMAS,
    OPENAPI_SOURCE_VERIFIED_OPERATIONS,
)


def _openapi_tag_for_path(path: str) -> str:
    if path in {"/healthz", "/readyz", "/version"}:
        return "Operations"
    if path.startswith("/v0/vnext"):
        return "vNext memory"
    if path.startswith("/v0"):
        return "Continuity v0"
    if path.startswith("/v1/providers") or path.startswith("/v1/workspaces") or path.startswith("/v1/runtime"):
        return "Providers"
    return "Alice API"


_OPENAPI_EXACT_RESPONSE_CONTRACTS: dict[tuple[str, str], tuple[str, object]] = {
    ("GET", "/v0/agent-profiles"): ("AgentProfileListResponse", AgentProfileListResponse),
    ("POST", "/v0/threads"): ("ThreadCreateResponse", ThreadCreateResponse),
    ("GET", "/v0/threads"): ("ThreadListResponse", ThreadListResponse),
    ("GET", "/v0/threads/health-dashboard"): ("ThreadHealthDashboardResponse", ThreadHealthDashboardResponse),
    ("GET", "/v0/threads/{thread_id}"): ("ThreadDetailResponse", ThreadDetailResponse),
    ("GET", "/v0/threads/{thread_id}/sessions"): ("ThreadSessionListResponse", ThreadSessionListResponse),
    ("GET", "/v0/threads/{thread_id}/events"): ("ThreadEventListResponse", ThreadEventListResponse),
    ("GET", "/v0/threads/{thread_id}/resumption-brief"): ("ResumptionBriefResponse", ResumptionBriefResponse),
    ("GET", "/v0/admin/debug/continuity/lifecycle"): (
        "ContinuityLifecycleListResponse",
        ContinuityLifecycleListResponse,
    ),
    ("GET", "/v0/admin/debug/continuity/lifecycle/{continuity_object_id}"): (
        "ContinuityLifecycleDetailResponse",
        ContinuityLifecycleDetailResponse,
    ),
    ("GET", "/v0/continuity/review-queue"): ("ContinuityReviewQueueResponse", ContinuityReviewQueueResponse),
    ("GET", "/v0/continuity/review-queue/{continuity_object_id}"): (
        "ContinuityReviewDetailResponse",
        ContinuityReviewDetailResponse,
    ),
    ("GET", "/v0/continuity/explain/{continuity_object_id}"): (
        "ContinuityExplainResponse",
        ContinuityExplainResponse,
    ),
    ("POST", "/v1/contradictions/detect"): ("ContradictionSyncResponse", ContradictionSyncResponse),
    ("GET", "/v1/contradictions/cases"): ("ContradictionCaseListResponse", ContradictionCaseListResponse),
    ("GET", "/v1/contradictions/cases/{contradiction_case_id}"): (
        "ContradictionCaseDetailResponse",
        ContradictionCaseDetailResponse,
    ),
    ("POST", "/v1/contradictions/cases/{contradiction_case_id}/resolve"): (
        "ContradictionResolveResponse",
        ContradictionResolveResponse,
    ),
    ("GET", "/v1/trust/signals"): ("TrustSignalListResponse", TrustSignalListResponse),
    ("GET", "/v0/state-at"): ("TemporalStateAtResponse", TemporalStateAtResponse),
    ("GET", "/v0/timeline"): ("TemporalTimelineResponse", TemporalTimelineResponse),
    ("GET", "/v0/explain"): ("TemporalExplainResponse", TemporalExplainResponse),
    ("GET", "/v0/patterns"): ("TrustedFactPatternListResponse", TrustedFactPatternListResponse),
    ("GET", "/v0/patterns/{pattern_id}"): ("TrustedFactPatternExplainResponse", TrustedFactPatternExplainResponse),
    ("GET", "/v0/playbooks"): ("TrustedFactPlaybookListResponse", TrustedFactPlaybookListResponse),
    ("GET", "/v0/playbooks/{playbook_id}"): (
        "TrustedFactPlaybookExplainResponse",
        TrustedFactPlaybookExplainResponse,
    ),
    ("GET", "/v0/admin/debug/continuity/artifacts/{artifact_id}"): (
        "ContinuityArtifactDetailResponse",
        ContinuityArtifactDetailResponse,
    ),
    ("GET", "/v0/continuity/open-loops"): (
        "ContinuityOpenLoopDashboardResponse",
        ContinuityOpenLoopDashboardResponse,
    ),
    ("GET", "/v0/continuity/daily-brief"): ("ContinuityDailyBriefResponse", ContinuityDailyBriefResponse),
    ("GET", "/v0/continuity/weekly-review"): (
        "ContinuityWeeklyReviewResponse",
        ContinuityWeeklyReviewResponse,
    ),
    ("POST", "/v0/continuity/open-loops/{continuity_object_id}/review-action"): (
        "ContinuityOpenLoopReviewActionResponse",
        ContinuityOpenLoopReviewActionResponse,
    ),
    ("GET", "/v0/continuity/recall"): ("ContinuityRecallResponse", ContinuityRecallResponse),
    ("GET", "/v0/continuity/retrieval-runs"): ("RetrievalRunListResponse", RetrievalRunListResponse),
    ("GET", "/v0/continuity/retrieval-runs/{retrieval_run_id}"): (
        "RetrievalTraceResponse",
        RetrievalTraceResponse,
    ),
    ("GET", "/v0/continuity/retrieval-evaluation"): (
        "RetrievalEvaluationResponse",
        RetrievalEvaluationResponse,
    ),
    ("GET", "/v1/evals/suites"): ("PublicEvalSuiteDefinitionListResponse", PublicEvalSuiteDefinitionListResponse),
    ("POST", "/v1/evals/runs"): ("PublicEvalRunDetailResponse", PublicEvalRunDetailResponse),
    ("GET", "/v1/evals/runs"): ("PublicEvalRunListResponse", PublicEvalRunListResponse),
    ("GET", "/v1/evals/runs/{eval_run_id}"): ("PublicEvalRunDetailResponse", PublicEvalRunDetailResponse),
    ("GET", "/v0/continuity/resumption-brief"): (
        "ContinuityResumptionBriefResponse",
        ContinuityResumptionBriefResponse,
    ),
    ("POST", "/v1/continuity/brief"): ("ContinuityBriefResponse", ContinuityBriefResponse),
    ("POST", "/v0/task-briefs/compile"): ("TaskBriefResponse", TaskBriefResponse),
    ("POST", "/v0/task-briefs/compare"): ("TaskBriefComparisonResponse", TaskBriefComparisonResponse),
    ("GET", "/v0/memories/trust-dashboard"): ("MemoryTrustDashboardResponse", MemoryTrustDashboardResponse),
    ("GET", "/v0/memories/hygiene-dashboard"): ("MemoryHygieneDashboardResponse", MemoryHygieneDashboardResponse),
}


_OPENAPI_CREATED_ONLY_OPERATIONS = {
    ("POST", "/v0/threads"),
    ("POST", "/v0/open-loops"),
    ("POST", "/v0/policies"),
    ("POST", "/v0/tools"),
    ("POST", "/v0/tasks/{task_id}/runs"),
    ("POST", "/v0/gmail-accounts"),
    ("POST", "/v0/calendar-accounts"),
    ("POST", "/v0/tasks/{task_id}/workspace"),
    ("POST", "/v0/task-workspaces/{task_workspace_id}/artifacts"),
    ("POST", "/v0/tasks/{task_id}/steps"),
    ("POST", "/v0/execution-budgets"),
    ("POST", "/v0/continuity/captures"),
    ("POST", "/v0/vnext/sources"),
    ("POST", "/v0/vnext/projects"),
    ("POST", "/v0/vnext/connectors/telegram/sync"),
    ("POST", "/v0/vnext/connectors/local-folder/sync"),
    ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"),
    ("POST", "/v0/vnext/connectors/browser-clipper/capture"),
    ("POST", "/v0/vnext/agents/ingest-output"),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"),
    ("POST", "/v0/vnext/context-packs"),
    ("POST", "/v0/vnext/memory-proposals"),
    ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
    ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"),
    ("POST", "/v0/vnext/artifacts/generate/connections"),
    ("POST", "/v0/vnext/artifacts/generate/contradictions"),
    ("POST", "/v0/vnext/queue/tasks"),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"),
    ("POST", "/v0/vnext/projects/update-candidates"),
    ("POST", "/v0/vnext/open-loops"),
    ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"),
    ("POST", "/v0/vnext/scheduler/run-due"),
    ("POST", "/v0/vnext/open-loops/extract"),
    ("POST", "/v0/task-briefs/compile"),
    ("POST", "/v0/memories/{memory_id}/labels"),
    ("POST", "/v0/embedding-configs"),
    ("POST", "/v0/memory-embeddings"),
    ("POST", "/v0/task-artifact-chunk-embeddings"),
    ("POST", "/v0/entities"),
    ("POST", "/v0/entity-edges"),
    ("POST", "/v1/providers"),
    ("POST", "/v1/providers/ollama/register"),
    ("POST", "/v1/providers/llamacpp/register"),
    ("POST", "/v1/providers/vllm/register"),
    ("POST", "/v1/providers/azure/register"),
}


_OPENAPI_CONDITIONAL_SUCCESS_OPERATIONS: dict[tuple[str, str], tuple[int, ...]] = {
    ("POST", "/v0/consents"): (200, 201),
    ("POST", "/v0/vnext/memories/commit"): (200, 201),
    ("POST", "/v0/vnext/connectors/{connector_name}/sync"): (201, 207),
    ("POST", "/v1/runtime/invoke"): (200, 202),
}


LEGACY_HTTP_OPERATION_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v0/tools"),
        ("GET", "/v0/tools"),
        ("GET", "/v0/tools/{tool_id}"),
        ("POST", "/v0/tools/allowlist/evaluate"),
        ("POST", "/v0/tools/route"),
        ("POST", "/v0/approvals/requests"),
        ("GET", "/v0/approvals"),
        ("GET", "/v0/approvals/{approval_id}"),
        ("POST", "/v0/approvals/{approval_id}/approve"),
        ("POST", "/v0/approvals/{approval_id}/reject"),
        ("POST", "/v0/approvals/{approval_id}/execute"),
        ("GET", "/v0/tasks"),
        ("GET", "/v0/tasks/{task_id}"),
        ("POST", "/v0/tasks/{task_id}/runs"),
        ("GET", "/v0/tasks/{task_id}/runs"),
        ("POST", "/v0/tasks/{task_id}/workspace"),
        ("GET", "/v0/tasks/{task_id}/steps"),
        ("POST", "/v0/tasks/{task_id}/artifact-chunks/retrieve"),
        ("POST", "/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval"),
        ("POST", "/v0/tasks/{task_id}/steps"),
        ("GET", "/v0/task-runs/{task_run_id}"),
        ("POST", "/v0/task-runs/{task_run_id}/tick"),
        ("POST", "/v0/task-runs/{task_run_id}/pause"),
        ("POST", "/v0/task-runs/{task_run_id}/resume"),
        ("POST", "/v0/task-runs/{task_run_id}/cancel"),
        ("GET", "/v0/task-workspaces"),
        ("GET", "/v0/task-workspaces/{task_workspace_id}"),
        ("POST", "/v0/task-workspaces/{task_workspace_id}/artifacts"),
        ("GET", "/v0/task-steps/{task_step_id}"),
        ("POST", "/v0/task-steps/{task_step_id}/transition"),
        ("POST", "/v0/execution-budgets"),
        ("GET", "/v0/execution-budgets"),
        ("GET", "/v0/execution-budgets/{execution_budget_id}"),
        ("POST", "/v0/execution-budgets/{execution_budget_id}/deactivate"),
        ("POST", "/v0/execution-budgets/{execution_budget_id}/supersede"),
        ("GET", "/v0/tool-executions"),
        ("GET", "/v0/tool-executions/{execution_id}"),
        ("POST", "/v0/task-briefs/compile"),
        ("GET", "/v0/task-briefs/{task_brief_id}"),
        ("POST", "/v0/task-briefs/compare"),
        ("POST", "/v0/gmail-accounts"),
        ("GET", "/v0/gmail-accounts"),
        ("GET", "/v0/gmail-accounts/{gmail_account_id}"),
        ("POST", "/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest"),
        ("POST", "/v0/calendar-accounts"),
        ("GET", "/v0/calendar-accounts"),
        ("GET", "/v0/calendar-accounts/{calendar_account_id}"),
        ("GET", "/v0/calendar-accounts/{calendar_account_id}/events"),
        ("POST", "/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest"),
    }
)
LEGACY_SURFACES_ENABLED = legacy_surfaces_enabled()


def _openapi_live_operation_keys(schema: dict[str, Any]) -> set[tuple[str, str]]:
    operation_keys: set[tuple[str, str]] = set()
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict):
                operation_keys.add((method.upper(), path))
    return operation_keys


class AliceFastAPI(FastAPI):
    """FastAPI app with concrete success contracts for every JSON route."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        schema = get_openapi(
            title=self.title,
            version=self.version,
            description=self.description,
            routes=self.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        live_operation_keys = _openapi_live_operation_keys(schema)
        registered_operation_keys = set(_OPENAPI_EXACT_RESPONSE_CONTRACTS) | set(OPENAPI_OPERATION_RESPONSE_SCHEMAS)
        expected_live_operation_keys = (
            registered_operation_keys
            if LEGACY_SURFACES_ENABLED
            else registered_operation_keys - LEGACY_HTTP_OPERATION_KEYS
        )
        if live_operation_keys != expected_live_operation_keys:
            missing = sorted(live_operation_keys - expected_live_operation_keys)
            extra = sorted(expected_live_operation_keys - live_operation_keys)
            raise RuntimeError(
                f"OpenAPI success-contract registry drifted from live routes; missing={missing}, extra={extra}"
            )
        if not LEGACY_HTTP_OPERATION_KEYS <= registered_operation_keys:
            missing_legacy = sorted(LEGACY_HTTP_OPERATION_KEYS - registered_operation_keys)
            raise RuntimeError(f"legacy OpenAPI operation inventory is incomplete: {missing_legacy}")
        live_registry = {
            operation_key: contract
            for operation_key, contract in OPENAPI_OPERATION_RESPONSE_SCHEMAS.items()
            if operation_key in live_operation_keys
        }
        live_exact_contracts = {
            operation_key: contract
            for operation_key, contract in _OPENAPI_EXACT_RESPONSE_CONTRACTS.items()
            if operation_key in live_operation_keys
        }
        component_names = [component_name for component_name, _component_schema in live_registry.values()]
        if len(component_names) != len(set(component_names)):
            raise RuntimeError("OpenAPI per-operation success component names must be unique")
        non_closed_operation_keys = {
            operation_key
            for operation_key, (_component_name, component_schema) in live_registry.items()
            if component_schema.get("additionalProperties") is not False
        }
        closed_operation_keys = set(live_registry) - non_closed_operation_keys
        expected_closed_operation_keys = set(OPENAPI_SOURCE_VERIFIED_OPERATIONS) & live_operation_keys
        if closed_operation_keys != expected_closed_operation_keys:
            raise RuntimeError("OpenAPI source-verified operation inventory drifted from closed schemas")
        expected_open_operation_keys = set(OPENAPI_OPEN_RESPONSE_OPERATIONS) & live_operation_keys
        if non_closed_operation_keys != expected_open_operation_keys:
            raise RuntimeError("OpenAPI open operation inventory drifted from permissive schemas")
        if not set(OPENAPI_INTENTIONALLY_POLYMORPHIC_OPERATIONS) <= non_closed_operation_keys:
            raise RuntimeError("OpenAPI polymorphic operations must use permissive schemas")
        if any(not justification.strip() for justification in OPENAPI_INTENTIONALLY_POLYMORPHIC_OPERATIONS.values()):
            raise RuntimeError("OpenAPI polymorphic operations require individual justifications")
        for component_name, component_schema in live_registry.values():
            components[component_name] = component_schema
        for component_name, contract in live_exact_contracts.values():
            contract_schema = TypeAdapter(contract).json_schema(
                ref_template="#/components/schemas/{model}",
            )
            definitions = contract_schema.pop("$defs", {})
            if isinstance(definitions, dict):
                for definition_name, definition in definitions.items():
                    components.setdefault(definition_name, definition)
            contract_schema["additionalProperties"] = False
            components[component_name] = contract_schema
        components["APIErrorDetail"] = {
            "title": "Stable API error detail",
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        }
        components["APIErrorResponse"] = {
            "title": "API error response",
            "type": "object",
            "required": ["detail"],
            "properties": {
                "detail": {
                    "description": "Static detail, stable structured error, or validation errors.",
                    "oneOf": [
                        {"type": "string"},
                        {"$ref": "#/components/schemas/APIErrorDetail"},
                        {"type": "array", "items": {}},
                    ],
                }
            },
        }
        tag_descriptions = {
            "Operations": "Health, readiness, and build identity.",
            "vNext memory": "Agentic memory, retrieval, project, and scheduler workflows.",
            "Continuity v0": "Deterministic continuity and memory APIs.",
            "Providers": "Model-provider discovery, configuration, and invocation.",
            "Alice API": "Local-first agent interface and continuity operations.",
        }
        schema["tags"] = [{"name": name, "description": description} for name, description in tag_descriptions.items()]
        for path, path_item in schema.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                operation_key = (method.upper(), path)
                operation.setdefault("tags", [_openapi_tag_for_path(path)])
                summary = operation.get("summary")
                operation.setdefault(
                    "description",
                    f"{summary}." if isinstance(summary, str) and summary else "AliceBot API operation.",
                )
                if operation_key == ("POST", "/v0/vnext/connectors/browser-clipper/capture"):
                    request_body = operation.get("requestBody")
                    if not isinstance(request_body, dict):  # pragma: no cover - FastAPI contract guard
                        raise RuntimeError("browser clip capture request body is missing from OpenAPI")
                    request_content = request_body.get("content")
                    if not isinstance(request_content, dict):  # pragma: no cover - FastAPI contract guard
                        raise RuntimeError("browser clip capture request content is missing from OpenAPI")
                    request_content["text/plain"] = {
                        "schema": {
                            "type": "string",
                            "contentMediaType": "application/json",
                            "description": (
                                "JSON-encoded VNextBrowserClipperCaptureRequest used by the "
                                "CORS-safelisted one-time bookmarklet transport."
                            ),
                        }
                    }
                responses = operation.get("responses")
                if not isinstance(responses, dict):
                    continue
                expected_statuses: tuple[int, ...] | None = None
                if operation_key in _OPENAPI_CREATED_ONLY_OPERATIONS:
                    expected_statuses = (201,)
                elif operation_key in _OPENAPI_CONDITIONAL_SUCCESS_OPERATIONS:
                    expected_statuses = _OPENAPI_CONDITIONAL_SUCCESS_OPERATIONS[operation_key]
                if expected_statuses is not None:
                    for status_code in tuple(responses):
                        if str(status_code).startswith("2") and int(status_code) not in expected_statuses:
                            responses.pop(status_code)
                    for status_code in expected_statuses:
                        responses.setdefault(str(status_code), {"description": "Successful response"})

                exact_contract = _OPENAPI_EXACT_RESPONSE_CONTRACTS.get(operation_key)
                if exact_contract is not None:
                    schema_name = exact_contract[0]
                else:
                    operation_contract = live_registry.get(operation_key)
                    if operation_contract is None:  # pragma: no cover - inventory fence above
                        raise RuntimeError(f"OpenAPI operation {operation_key!r} has no success contract")
                    schema_name = operation_contract[0]
                for status_code, response in responses.items():
                    if not str(status_code).startswith("2") or not isinstance(response, dict):
                        continue
                    content = response.setdefault("content", {})
                    if isinstance(content, dict):
                        json_content = content.setdefault("application/json", {})
                        if isinstance(json_content, dict):
                            json_content["schema"] = {"$ref": f"#/components/schemas/{schema_name}"}
                if path == "/healthz":
                    responses["503"] = {
                        "description": "Service is degraded or unavailable",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/HealthcheckSuccessResponse"}}
                        },
                    }
                responses.setdefault(
                    "default",
                    {
                        "description": "Error response",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/APIErrorResponse"}}},
                    },
                )
        self.openapi_schema = schema
        return self.openapi_schema


app = AliceFastAPI(
    title="AliceBot API",
    version=__version__,
    description="AliceBot local-first continuity, retrieval, and agentic-memory API.",
)


@app.exception_handler(RequestValidationError)
async def _alice_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    """Keep one-time browser credentials out of framework validation bodies."""

    if (
        request.method.upper() == "POST"
        and request.url.path == "/v0/vnext/connectors/browser-clipper/capture"
    ):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body"],
                        "msg": "Input validation failed",
                    }
                ]
            },
        )
    return await request_validation_exception_handler(request, exc)


from alicebot_api.routers import providers  # noqa: E402
from alicebot_api.routers.providers import redact_url_credentials  # noqa: E402
from alicebot_api.routers import workspaces  # noqa: E402

HealthStatus = Literal["ok", "degraded"]
ServiceStatus = Literal["ok", "unreachable", "not_checked"]


class DatabaseServicePayload(TypedDict):
    status: Literal["ok", "unreachable"]


class RedisServicePayload(TypedDict):
    status: Literal["not_checked"]
    url: str


class ObjectStorageServicePayload(TypedDict):
    status: Literal["not_checked"]


class HealthServicesPayload(TypedDict):
    database: DatabaseServicePayload
    redis: RedisServicePayload
    object_storage: ObjectStorageServicePayload


class HealthcheckPayload(TypedDict):
    status: HealthStatus
    environment: str
    services: HealthServicesPayload


def _rewrite_user_id_query_param(request: Request, authenticated_user_id: UUID) -> None:
    raw_query = request.scope.get("query_string", b"")
    query_items = parse_qsl(raw_query.decode("utf-8"), keep_blank_values=True)
    expected_user_id = str(authenticated_user_id)
    for key, value in query_items:
        if key == "user_id" and value != expected_user_id:
            raise ValueError("query user_id does not match authenticated user")
    rewritten_items = [(key, value) for key, value in query_items if key != "user_id"]
    rewritten_items.append(("user_id", expected_user_id))
    request.scope["query_string"] = urlencode(rewritten_items, doseq=True).encode("utf-8")


async def _rewrite_user_id_json_body(request: Request, authenticated_user_id: UUID) -> Request:
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return request

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return request

    raw_body = await request.body()
    if raw_body == b"":
        return request

    try:
        parsed_body = json.loads(raw_body)
    except json.JSONDecodeError:
        return request

    if not isinstance(parsed_body, dict):
        return request

    expected_user_id = str(authenticated_user_id)
    existing_user_id = parsed_body.get("user_id")
    if existing_user_id is not None and str(existing_user_id) != expected_user_id:
        raise ValueError("request user_id does not match authenticated user")
    parsed_body["user_id"] = expected_user_id
    rewritten_body = json.dumps(parsed_body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    # BaseHTTPMiddleware.call_next ignores a replacement Request and replays
    # this request's cached body. Write the rewritten bytes into that cache,
    # as the browser-clip path does, or the route validates the original body
    # and a header-only client gets 422.
    request._body = rewritten_body  # type: ignore[attr-defined]

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": rewritten_body,
            "more_body": False,
        }

    return Request(request.scope, receive)


_VNEXT_ROUTE_LOCAL_POLICY = frozenset(
    {
        ("POST", "/v0/vnext/sources"),
        ("POST", "/v0/vnext/agents/ingest-output"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"),
        ("GET", "/v0/vnext/artifacts/{artifact_id}"),
        ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/export"),
        ("POST", "/v0/vnext/context-packs"),
        ("POST", "/v0/vnext/memories/{memory_id}/review"),
        ("POST", "/v0/vnext/memory-proposals"),
        ("POST", "/v0/vnext/memories/commit"),
        ("POST", "/v0/vnext/memories/confirm"),
        ("POST", "/v0/vnext/memories/undo"),
        ("POST", "/v0/vnext/memories/correct"),
        ("POST", "/v0/vnext/memories/forget"),
        ("POST", "/v0/vnext/memories/expire"),
        ("POST", "/v0/vnext/memories/unexpire"),
        ("POST", "/v0/vnext/memories/accept-consolidation"),
        ("POST", "/v0/vnext/memories/redact"),
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"),
        ("POST", "/v0/vnext/artifacts/generate/connections"),
        ("POST", "/v0/vnext/artifacts/generate/contradictions"),
        ("POST", "/v0/vnext/queue/tasks"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/review"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"),
        ("POST", "/v0/vnext/projects/update-candidates"),
        ("POST", "/v0/vnext/open-loops"),
        ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"),
        ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"),
        ("POST", "/v0/vnext/scheduler/run-due"),
        ("POST", "/v0/vnext/scheduler/pause"),
        ("POST", "/v0/vnext/scheduler/resume"),
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"),
    }
)

# Routes without a target/scope-specific policy are the operator-console
# surface.  Keep this inventory explicit: adding a route without classifying
# it must fail closed at runtime and fail the route-inventory regression.
_VNEXT_CENTRAL_OPERATOR_ROUTES = frozenset(
    {
        ("DELETE", "/v0/vnext/sources/{source_id}"),
        ("GET", "/v0/vnext/agents/policy-telemetry"),
        ("GET", "/v0/vnext/artifacts"),
        ("GET", "/v0/vnext/beliefs/{belief_id}/state"),
        ("GET", "/v0/vnext/connectors"),
        ("GET", "/v0/vnext/connectors/health"),
        ("GET", "/v0/vnext/connectors/{connector_name}/status"),
        ("GET", "/v0/vnext/context-tree"),
        ("GET", "/v0/vnext/doctor"),
        ("GET", "/v0/vnext/dogfooding"),
        ("GET", "/v0/vnext/graph/neighborhood/{target_id}"),
        ("GET", "/v0/vnext/memories/recent-commits"),
        ("GET", "/v0/vnext/memories/{memory_id}/audit"),
        ("GET", "/v0/vnext/projects"),
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"),
        ("GET", "/v0/vnext/quality-evals"),
        ("GET", "/v0/vnext/scheduler/failures"),
        ("GET", "/v0/vnext/scheduler/runs"),
        ("GET", "/v0/vnext/scheduler/status"),
        ("GET", "/v0/vnext/settings/brain-charter"),
        ("GET", "/v0/vnext/sources/{source_id}"),
        ("GET", "/v0/vnext/traces/sources/{source_id}"),
        ("GET", "/v0/vnext/workspace"),
        ("PATCH", "/v0/vnext/connectors/{connector_name}/config"),
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"),
        ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"),
        ("POST", "/v0/vnext/connectors/browser-clipper/capture"),
        ("POST", "/v0/vnext/connectors/local-folder/sync"),
        ("POST", "/v0/vnext/connectors/telegram/sync"),
        ("POST", "/v0/vnext/connectors/{connector_name}/sync"),
        ("POST", "/v0/vnext/doctor/run"),
        ("POST", "/v0/vnext/graph/edges/{edge_id}/review"),
        ("POST", "/v0/vnext/open-loops/extract"),
        ("POST", "/v0/vnext/projects"),
        ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"),
        ("POST", "/v0/vnext/queue/process-next"),
        ("POST", "/v0/vnext/sources/{source_id}/review"),
        ("PUT", "/v0/vnext/settings/brain-charter"),
    }
)

_BROWSER_CLIP_SIMPLE_CAPTURE_PATH = "/v0/vnext/connectors/browser-clipper/capture"
_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES = 3_000_000


async def _prepare_browser_clip_simple_request(
    request: Request,
) -> tuple[Request, dict[str, object] | None]:
    """Translate the clipper's CORS-safelisted body into the JSON contract.

    A bookmarklet executes inside an untrusted visited page. Its one-time
    capability is therefore sent as a simple ``text/plain`` request without
    reusable credentials or custom headers. Keep this exception confined to
    the exact capture route and bound the body before parsing it.
    """

    if request.method.upper() != "POST" or request.url.path != _BROWSER_CLIP_SIMPLE_CAPTURE_PATH:
        return request, None
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if media_type != "text/plain":
        return request, None

    raw_content_length = request.headers.get("content-length")
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except ValueError as exc:
            raise ValueError("browser clip request content length is invalid") from exc
        if content_length < 0 or content_length > _BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES:
            raise ValueError("browser clip request body is too large")

    buffered_body = bytearray()
    async for chunk in request.stream():
        if len(buffered_body) + len(chunk) > _BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES:
            raise ValueError("browser clip request body is too large")
        buffered_body.extend(chunk)
    raw_body = bytes(buffered_body)
    if len(raw_body) > _BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES:  # pragma: no cover - accumulator fence
        raise ValueError("browser clip request body is too large")
    # BaseHTTPMiddleware captured this original request's wrapped receiver
    # before dispatch. Populate its replay cache only after bounded streaming;
    # otherwise the downstream app sees an empty body.
    request._body = raw_body  # type: ignore[attr-defined]
    try:
        parsed_body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("browser clip request body is invalid") from exc
    if not isinstance(parsed_body, dict):
        raise ValueError("browser clip request body must be an object")
    capability = parsed_body.get("capture_capability")
    if not isinstance(capability, str) or not capability.strip():
        raise ValueError("browser clip simple requests require a capability")

    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.lower() != b"content-type"
    ]
    headers.append((b"content-type", b"application/json"))
    # BaseHTTPMiddleware dispatches the downstream app with the original ASGI
    # scope, so mutate that shared scope in place before handing it a replayable
    # request body.
    request.scope["headers"] = headers

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": raw_body,
            "more_body": False,
        }

    return Request(request.scope, receive), parsed_body


def _matched_vnext_route_path(request: Request) -> str:
    for route in app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        route_contexts = effective_route_contexts() if callable(effective_route_contexts) else (route,)
        for route_context in route_contexts:
            match, _child_scope = route_context.matches(request.scope)
            if match is Match.FULL:
                return str(getattr(route_context, "path", None) or request.url.path)
    return request.url.path


def _vnext_central_route_policy(
    *,
    identity: AgentIdentity | None,
    method: str,
    route_path: str,
) -> PolicyDecision | None:
    """Authorize one classified local-policy or central-operator route."""

    route_key = (method.upper(), route_path)
    if route_key in _VNEXT_ROUTE_LOCAL_POLICY:
        return None
    if route_key not in _VNEXT_CENTRAL_OPERATOR_ROUTES:
        return PolicyDecision(
            decision="blocked",
            action="http.route.unclassified",
            permission_profile=(identity.permission_profile if identity is not None else "user_or_system"),
            reasons=("vnext_route_not_classified",),
        )
    if identity is None:
        # Zero-key local installs retain their explicit human/operator path.
        return None
    return evaluate_agent_policy(identity=identity, action="http.operator.access")


def _resolve_vnext_http_auth(
    *,
    settings: Settings,
    user_id: UUID,
    raw_key: str | None,
    payload: dict[str, object],
    method: str,
    route_path: str,
) -> tuple[AgentIdentity | None, PolicyDecision | None]:
    """Run protected-route database authentication off the event-loop thread."""

    with user_connection(settings.database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        identity = resolve_protected_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload=payload,
        )
        route_decision = _vnext_central_route_policy(
            identity=identity,
            method=method,
            route_path=route_path,
        )
        if route_decision is not None and route_decision.decision == "blocked":
            append_policy_events(
                store,
                identity=identity,
                decision=route_decision,
                target_type="http_route",
                target_id=route_path,
            )
    return identity, route_decision


async def _vnext_protected_http_auth(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Authenticate the complete protected ``/v0/vnext`` route surface."""

    if not request.url.path.startswith("/v0/vnext"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    try:
        request, simple_capture_payload = await _prepare_browser_clip_simple_request(request)
    except ValueError:
        return _vnext_public_error_response(
            status_code=400,
            detail="vNext browser clip capture request is invalid",
        )

    payload: dict[str, object] = {}
    if simple_capture_payload is not None:
        payload = simple_capture_payload
    elif request.method not in {"GET", "HEAD", "OPTIONS"}:
        try:
            candidate = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            candidate = None
        if isinstance(candidate, dict):
            payload = candidate

    query_user_id = request.query_params.get("user_id")
    body_user_id = payload.get("user_id")
    if query_user_id is not None and body_user_id is not None and str(body_user_id) != query_user_id:
        return _vnext_public_error_response(
            status_code=400,
            detail="vNext user_id values do not match",
        )
    raw_user_id = body_user_id if body_user_id is not None else query_user_id
    if raw_user_id is None:
        return _vnext_public_error_response(
            status_code=400,
            detail="protected vNext requests require user_id",
        )
    try:
        user_id = UUID(str(raw_user_id))
    except ValueError:
        return _vnext_public_error_response(status_code=400, detail="vNext user_id is invalid")

    try:
        settings = get_settings()
        route_path = _matched_vnext_route_path(request)
        capability_capture = (
            request.method.upper() == "POST"
            and route_path == _BROWSER_CLIP_SIMPLE_CAPTURE_PATH
            and isinstance(payload.get("capture_capability"), str)
            and bool(str(payload["capture_capability"]).strip())
        )
        raw_key = agent_key_from_authorization(request.headers.get("authorization"))
        if raw_key is None and not capability_capture and _keyless_request_is_off_loopback(request, settings):
            return _authentication_failed_response("keyless vNext requests are restricted to loopback clients")
        if capability_capture:
            # The capability is the narrow credential for this endpoint. Its
            # hash/origin/user/expiry/consumption checks run atomically in the
            # handler's capture transaction.
            identity = None
            route_decision = _vnext_central_route_policy(
                identity=None,
                method=request.method,
                route_path=route_path,
            )
        else:
            identity, route_decision = await run_in_threadpool(
                _resolve_vnext_http_auth,
                settings=settings,
                user_id=user_id,
                raw_key=raw_key,
                payload=payload,
                method=request.method,
                route_path=route_path,
            )
        if route_decision is not None and route_decision.decision == "blocked":
            return _vnext_permission_response(route_decision)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    request.state.vnext_agent_identity = identity
    return await call_next(request)


app.middleware("http")(_vnext_protected_http_auth)


def build_healthcheck_payload(settings: Settings, database_ok: bool) -> HealthcheckPayload:
    status: HealthStatus = "ok" if database_ok else "degraded"
    database_status: Literal["ok", "unreachable"] = "ok" if database_ok else "unreachable"

    return {
        "status": status,
        "environment": settings.app_env,
        "services": {
            "database": {
                "status": database_status,
            },
            "redis": {
                "status": "not_checked",
                "url": redact_url_credentials(settings.redis_url),
            },
            "object_storage": {
                "status": "not_checked",
            },
        },
    }


def _request_client_is_loopback(request: Request, settings: Settings) -> bool:
    client_identifier = _request_client_identifier(request, settings)
    try:
        client_ip = ipaddress.ip_address(client_identifier)
    except ValueError:
        return client_identifier in {"localhost", "localhost.localdomain"}
    return client_ip.is_loopback


def _keyless_request_is_off_loopback(request: Request, settings: Settings) -> bool:
    """Report whether a keyless request must be refused before dispatch.

    Unconditional by design. Deriving this from ``APP_ENV`` or ``APP_HOST``
    would trust settings that can disagree with the real bind: a process
    started outside ``local_server`` on ``0.0.0.0`` still reports the default
    loopback ``APP_HOST``. The peer address is the only fact that cannot lie,
    and ``_request_client_is_loopback`` reads ``X-Forwarded-For`` only when the
    peer is a configured trusted proxy.
    """

    return not _request_client_is_loopback(request, settings)


def _authentication_failed_response(reason: str) -> JSONResponse:
    """Return the repository's one stable 401 body for a refused request."""

    return public_exception_response(
        AgentKeyAuthenticationError(reason, status_code=401),
        status_code=401,
    )


def _is_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


async def _v1_request_payload(request: Request) -> dict[str, object]:
    """Read the JSON body an agent-key claim could be hiding in."""

    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return {}
    if "application/json" not in request.headers.get("content-type", "").casefold():
        return {}
    try:
        candidate = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return candidate if isinstance(candidate, dict) else {}


def _v1_request_claims_other_user(
    request: Request,
    payload: dict[str, object],
    authenticated_user_id: UUID,
) -> bool:
    """Report whether the request body or query claims a different user.

    ``/v1`` handlers take their user from the server-side binding, never from
    the payload. This keeps that true by construction: a payload that names
    another user is refused rather than quietly ignored.
    """

    expected_user_id = str(authenticated_user_id)
    query_user_id = request.query_params.get("user_id")
    if query_user_id is not None and query_user_id.strip() != expected_user_id:
        return True
    body_user_id = payload.get("user_id")
    return body_user_id is not None and str(body_user_id).strip() != expected_user_id


def _resolve_v1_http_auth(
    *,
    settings: Settings,
    user_id: UUID,
    raw_key: str | None,
    payload: dict[str, object],
) -> AgentIdentity | None:
    """Run ``/v1`` agent-key authentication off the event-loop thread."""

    with user_connection(settings.database_url, user_id) as conn:
        return resolve_protected_agent_identity(
            PostgresVNextStore(conn),
            user_id=user_id,
            raw_key=raw_key,
            payload=payload,
        )


async def enforce_v1_agent_authentication(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Authenticate the complete ``/v1`` surface before any handler runs.

    ``/v1`` routes resolve their user from ``ALICEBOT_AUTH_USER_ID`` or the
    identity header, which binds the data but proves nothing about the caller.
    Once the bound user has provisioned an agent API key, every ``/v1`` request
    must present it. While no key exists the surface stays keyless for local
    callers only.

    This authenticates and does not authorize. No handler reads the resolved
    identity, so ``/v1`` enforces no permission profile and records no agent on
    the rows it writes: any valid key can do anything any other valid key can,
    and a revoked key's ``/v1`` writes are not reachable by an agent-keyed
    quarantine sweep. Keep ``/v1`` loopback-only, as
    ``docs/deployment/single-tenant-self-hosted.md`` instructs.
    """

    if not _is_v1_path(request.url.path):
        return await call_next(request)
    if request.method.upper() == "OPTIONS":
        return await call_next(request)

    settings = get_settings()
    raw_key = agent_key_from_authorization(request.headers.get("authorization"))
    if raw_key is None and _keyless_request_is_off_loopback(request, settings):
        return _authentication_failed_response("keyless /v1 requests are restricted to loopback clients")

    try:
        user_id = _resolve_authenticated_v1_user_id(settings, request)
    except ValueError:
        # No bound user means no privilege to grant. The route handler owns the
        # stable "local identity is required" contract for that case.
        return await call_next(request)

    payload = await _v1_request_payload(request)
    if _v1_request_claims_other_user(request, payload, user_id):
        return _authentication_failed_response("request user_id does not match the authenticated user")

    try:
        identity = await run_in_threadpool(
            _resolve_v1_http_auth,
            settings=settings,
            user_id=user_id,
            raw_key=raw_key,
            payload=payload,
        )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    request.state.v1_agent_identity = identity
    return await call_next(request)


# Registered after the vNext middleware and before the security-posture
# middleware, so a refused /v1 request still leaves with the standard CORS and
# security headers.
app.middleware("http")(enforce_v1_agent_authentication)


def _append_vary_header(response: Response, value: str) -> None:
    existing = response.headers.get("Vary", "")
    values = [item.strip() for item in existing.split(",") if item.strip() != ""]
    if value not in values:
        values.append(value)
    response.headers["Vary"] = ", ".join(values)


def _cors_origin_allowed(origin: str, allowed_origins: tuple[str, ...]) -> bool:
    if len(allowed_origins) == 0:
        return False
    if "*" in allowed_origins:
        return True
    return origin in allowed_origins


def _resolve_cors_allow_origin_value(settings: Settings, origin: str) -> str:
    if "*" in settings.cors_allowed_origins and not settings.cors_allow_credentials:
        return "*"
    return origin


def _apply_cors_headers(
    *,
    response: Response,
    settings: Settings,
    origin: str,
    preflight: bool,
) -> None:
    allow_origin = _resolve_cors_allow_origin_value(settings, origin)
    response.headers["Access-Control-Allow-Origin"] = allow_origin
    if allow_origin != "*":
        _append_vary_header(response, "Origin")
    if settings.cors_allow_credentials:
        response.headers["Access-Control-Allow-Credentials"] = "true"

    if not preflight:
        return

    response.headers["Access-Control-Allow-Methods"] = ", ".join(settings.cors_allowed_methods)
    response.headers["Access-Control-Allow-Headers"] = ", ".join(settings.cors_allowed_headers)
    response.headers["Access-Control-Max-Age"] = str(settings.cors_preflight_max_age_seconds)
    _append_vary_header(response, "Access-Control-Request-Method")
    _append_vary_header(response, "Access-Control-Request-Headers")


def _apply_security_headers(*, response: Response, settings: Settings, request: Request) -> None:
    if not settings.security_headers_enabled:
        return

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
            "microphone=(), payment=(), usb=()"
        ),
    )

    if request.url.scheme != "https" or settings.app_env in {"development", "test"}:
        return

    hsts_value = f"max-age={settings.security_headers_hsts_max_age_seconds}"
    if settings.security_headers_hsts_include_subdomains:
        hsts_value += "; includeSubDomains"
    response.headers.setdefault("Strict-Transport-Security", hsts_value)


@app.middleware("http")
async def apply_http_security_posture(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    settings = get_settings()
    origin = request.headers.get("origin", "").strip()
    is_preflight = (
        request.method.upper() == "OPTIONS" and request.headers.get("access-control-request-method", "").strip() != ""
    )
    response: Response

    if is_preflight:
        if origin == "" or not _cors_origin_allowed(origin, settings.cors_allowed_origins):
            response = JSONResponse(status_code=403, content={"detail": "CORS origin is not allowed"})
            _apply_security_headers(response=response, settings=settings, request=request)
            return response
        response = Response(status_code=204)
        _apply_cors_headers(response=response, settings=settings, origin=origin, preflight=True)
        _apply_security_headers(response=response, settings=settings, request=request)
        return response

    response = await call_next(request)
    if origin != "" and _cors_origin_allowed(origin, settings.cors_allowed_origins):
        _apply_cors_headers(response=response, settings=settings, origin=origin, preflight=False)
    _apply_security_headers(response=response, settings=settings, request=request)
    return response


@app.middleware("http")
async def enforce_authenticated_user_identity(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if not request.url.path.startswith("/v0/"):
        return await call_next(request)

    settings = get_settings()

    if settings.app_env not in {"development", "test"} and not (
        request.url.path == "/v0/vnext" or request.url.path.startswith("/v0/vnext/")
    ):
        if not settings.legacy_v0_enabled_outside_dev:
            return JSONResponse(
                status_code=404,
                content={"detail": "legacy v0 API is disabled outside development and test"},
            )
        if not _request_client_is_loopback(request, settings):
            return JSONResponse(
                status_code=403,
                content={"detail": "legacy v0 API is restricted to loopback clients"},
            )

    try:
        authenticated_user_id = _resolve_authenticated_user_id(settings, request)
        if authenticated_user_id is not None:
            request.scope.setdefault("state", {})["authenticated_user_id"] = str(authenticated_user_id)
            _rewrite_user_id_query_param(request, authenticated_user_id)
            request = await _rewrite_user_id_json_body(request, authenticated_user_id)
    except ValueError as exc:
        return public_exception_response(exc, status_code=401)

    return await call_next(request)


@app.get("/healthz")
def healthcheck() -> JSONResponse:
    settings = get_settings()
    database_ok = ping_database(
        settings.database_url,
        settings.healthcheck_timeout_seconds,
    )
    payload = build_healthcheck_payload(settings, database_ok)
    status_code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content=payload,
    )


app.include_router(memories_legacy.core_router)


if LEGACY_SURFACES_ENABLED:
    app.include_router(legacy_gated.core_router)


app.include_router(memories_legacy.task_artifact_router)


if LEGACY_SURFACES_ENABLED:
    app.include_router(legacy_gated.task_artifact_retrieval_router)


app.include_router(memories_legacy.task_artifact_retrieval_router)


if LEGACY_SURFACES_ENABLED:
    app.include_router(legacy_gated.task_artifact_semantic_router)


app.include_router(memories_legacy.task_artifact_semantic_router)


if LEGACY_SURFACES_ENABLED:
    app.include_router(legacy_gated.operations_router)


app.include_router(memories_legacy.signals_router)


app.include_router(continuity.capture_router)


app.include_router(workspaces.core_router)


app.include_router(vnext_memories.source_create_router)


app.include_router(vnext_projects.project_core_router)


app.include_router(vnext_memories.connectors_router)


app.include_router(vnext_review.insight_feedback_router)


app.include_router(vnext_memories.source_review_router)


app.include_router(vnext_retrieval.trace_router)


app.include_router(vnext_memories.source_delete_router)


app.include_router(vnext_retrieval.context_router)


app.include_router(vnext_memories.memory_router)


app.include_router(vnext_review.review_router)


app.include_router(vnext_projects.project_operations_router)


app.include_router(continuity.operations_router)


if LEGACY_SURFACES_ENABLED:
    app.include_router(legacy_gated.task_brief_router)


app.include_router(memories_legacy.memory_router)


app.include_router(workspaces.bootstrap_router)


app.include_router(providers.router)


def _apply_legacy_surface_mount_policy() -> None:
    if LEGACY_SURFACES_ENABLED:
        return

    retained_routes = []
    for route in app.router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not isinstance(methods, set):
            retained_routes.append(route)
            continue
        operation_keys = {(method, path) for method in methods if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}}
        gated_keys = operation_keys & LEGACY_HTTP_OPERATION_KEYS
        if gated_keys:
            if operation_keys != gated_keys:  # pragma: no cover - one-operation route invariant
                raise RuntimeError(f"legacy surface route mixes gated and retained methods: {path}")
            continue
        retained_routes.append(route)
    app.router.routes[:] = retained_routes


_apply_legacy_surface_mount_policy()
