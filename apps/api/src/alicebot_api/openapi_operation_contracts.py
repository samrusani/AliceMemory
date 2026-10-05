"""Per-operation OpenAPI success response schemas.

This registry deliberately uses literal ``(METHOD, path)`` keys so route coverage can
be checked without executing the application. Named response helpers generate their
schemas from the authoritative ``TypedDict`` contract. Direct service/store returns
use source-audited, closed top-level envelopes. Only the two genuinely polymorphic
async response operations remain open, with closed variants for each known shape.
"""

from __future__ import annotations

from importlib import import_module

from pydantic import TypeAdapter


def _typed_properties(
    *,
    objects: tuple[str, ...] = (),
    nullable_objects: tuple[str, ...] = (),
    object_arrays: tuple[str, ...] = (),
    string_arrays: tuple[str, ...] = (),
    strings: tuple[str, ...] = (),
    nullable_strings: tuple[str, ...] = (),
    integers: tuple[str, ...] = (),
    booleans: tuple[str, ...] = (),
) -> dict[str, dict[str, object]]:
    """Build an explicitly classified per-operation property map."""

    properties: dict[str, dict[str, object]] = {}
    for field in objects:
        properties[field] = {"type": "object", "additionalProperties": True}
    for field in nullable_objects:
        properties[field] = {
            "anyOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "null"},
            ]
        }
    for field in object_arrays:
        properties[field] = {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        }
    for field in string_arrays:
        properties[field] = {"type": "array", "items": {"type": "string"}}
    for field in strings:
        properties[field] = {"type": "string"}
    for field in nullable_strings:
        properties[field] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    for field in integers:
        properties[field] = {"type": "integer"}
    for field in booleans:
        properties[field] = {"type": "boolean"}
    classified_count = sum(
        len(fields)
        for fields in (
            objects,
            nullable_objects,
            object_arrays,
            string_arrays,
            strings,
            nullable_strings,
            integers,
            booleans,
        )
    )
    if len(properties) != classified_count:
        raise RuntimeError("OpenAPI property was assigned more than one JSON type")
    return properties


_ARTIFACT_RESPONSE_FIELDS = (
    "artifact_type",
    "content_markdown",
    "created_at",
    "domain",
    "generated_by",
    "id",
    "metadata_json",
    "model_info_json",
    "prompt_hash",
    "promoted_at",
    "reviewed_at",
    "sensitivity",
    "status",
    "title",
    "user_id",
)

# VNextSchedulerService.status() returns the first twelve fields on every call;
# GET /v0/vnext/scheduler/status adds the daemon snapshot as the final field.
# Keep this inventory literal so a runtime/schema drift is visible in review.
_SCHEDULER_STATUS_RESPONSE_FIELDS = (
    "mode",
    "disabled_by_default",
    "workflows",
    "recent_runs",
    "enabled_count",
    "paused_count",
    "last_failure",
    "recent_failures",
    "last_due_scan",
    "next_due_workflow",
    "currently_running_workflow",
    "last_success_by_workflow",
    "daemon",
)


def _artifact_response_properties() -> dict[str, dict[str, object]]:
    """Describe the artifact object returned directly by generator handlers."""

    properties = _typed_properties(
        objects=("metadata_json", "model_info_json"),
        strings=(
            "artifact_type",
            "content_markdown",
            "domain",
            "generated_by",
            "sensitivity",
            "status",
            "title",
        ),
        nullable_strings=("prompt_hash",),
    )
    properties.update(
        {
            "id": {"type": "string", "format": "uuid"},
            "user_id": {"type": "string", "format": "uuid"},
            "created_at": {"type": "string", "format": "date-time"},
            "reviewed_at": {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]},
            "promoted_at": {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]},
        }
    )
    return properties


def _operation_schema(
    title: str,
    fields: tuple[str, ...],
    *,
    required: tuple[str, ...] | None = None,
    closed: bool = False,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "title": title,
        "type": "object",
        # Unverified fields stay unconstrained until a source-backed entry is
        # applied below. An empty JSON Schema is honest; claiming every unknown
        # scalar/array/null field is an object is not.
        "properties": {field: {} for field in fields},
        "additionalProperties": not closed,
    }
    if required is not None:
        schema["required"] = list(required)
    elif closed:
        schema["required"] = list(fields)
    return schema


def _inline_local_definitions(
    value: object,
    definitions: dict[str, object],
    *,
    resolving: frozenset[str] = frozenset(),
) -> object:
    """Inline Pydantic's local definitions so registry components are self-contained.

    Registry components are inserted into the application OpenAPI document by
    ``AliceFastAPI``.  A raw ``#/$defs/...`` reference would point at the document
    root rather than at the component that owns it, so resolve those references
    before handing the schema to the application.
    """

    if isinstance(value, list):
        return [_inline_local_definitions(item, definitions, resolving=resolving) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        definition = definitions.get(name)
        if definition is None:
            raise RuntimeError(f"OpenAPI response type references unknown definition {name!r}")
        if name in resolving:
            # JSON values are intentionally recursive.  Keep that nested value
            # unconstrained instead of publishing a dangling document-level
            # reference; the response envelope itself remains exact and closed.
            return {}
        return _inline_local_definitions(definition, definitions, resolving=resolving | {name})
    return {
        str(key): _inline_local_definitions(child, definitions, resolving=resolving)
        for key, child in value.items()
        if key != "$defs"
    }


def _schema_from_authoritative_response_type(
    *,
    module_name: str,
    type_name: str,
    title: str,
) -> dict[str, object]:
    """Build a closed schema from the response type returned by the handler helper."""

    response_type = getattr(import_module(module_name), type_name)
    raw_schema = TypeAdapter(response_type).json_schema()
    definitions_value = raw_schema.pop("$defs", {})
    definitions = definitions_value if isinstance(definitions_value, dict) else {}
    schema_value = _inline_local_definitions(raw_schema, definitions)
    if not isinstance(schema_value, dict):  # pragma: no cover - TypeAdapter invariant
        raise RuntimeError(f"OpenAPI response type {module_name}.{type_name} did not produce an object schema")
    schema: dict[str, object] = {str(key): value for key, value in schema_value.items()}
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise RuntimeError(f"OpenAPI response type {module_name}.{type_name} must be an object")
    schema["title"] = title
    schema["additionalProperties"] = False
    return schema


def _closed_source_schema(
    title: str,
    fields: tuple[str, ...],
    *,
    required: tuple[str, ...] | None = None,
    properties: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Describe a source-audited success envelope without guessing nested types."""

    field_properties = properties or {field: {} for field in fields}
    if set(field_properties) != set(fields):
        raise RuntimeError(f"OpenAPI source schema {title} property inventory drifted")
    required_fields = fields if required is None else required
    if not set(required_fields) <= set(fields):
        raise RuntimeError(f"OpenAPI source schema {title} requires an unknown property")
    return {
        "title": title,
        "type": "object",
        "properties": field_properties,
        "required": list(required_fields),
        "additionalProperties": False,
    }


OPENAPI_OPERATION_RESPONSE_SCHEMAS: dict[tuple[str, str], tuple[str, dict[str, object]]] = {
    ("GET", "/healthz"): (
        "HealthcheckSuccessResponse",
        _operation_schema(
            "HealthcheckSuccessResponse",
            (
                "status",
                "environment",
                "services",
            ),
            closed=True,
        ),
    ),
    ("POST", "/v0/context/compile"): (
        "CompileContextSuccessResponse",
        _operation_schema(
            "CompileContextSuccessResponse",
            ("context_pack", "metadata", "trace_event_count", "trace_id"),
            required=("context_pack", "metadata", "trace_event_count", "trace_id"),
            closed=True,
        ),
    ),
    ("GET", "/v0/traces"): (
        "ListTracesSuccessResponse",
        _operation_schema(
            "ListTracesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/traces/{trace_id}"): (
        "GetTraceSuccessResponse",
        _operation_schema(
            "GetTraceSuccessResponse",
            (
                "trace",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/traces/{trace_id}/events"): (
        "ListTraceEventsSuccessResponse",
        _operation_schema(
            "ListTraceEventsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/memories/admit"): (
        "AdmitMemorySuccessResponse",
        _operation_schema(
            "AdmitMemorySuccessResponse",
            ("decision", "memory", "open_loop", "reason", "revision"),
            required=("decision", "memory", "reason", "revision"),
            closed=True,
        ),
    ),
    ("GET", "/v0/open-loops"): (
        "ListOpenLoopsSuccessResponse",
        _operation_schema(
            "ListOpenLoopsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/open-loops/{open_loop_id}"): (
        "GetOpenLoopSuccessResponse",
        _operation_schema(
            "GetOpenLoopSuccessResponse",
            (
                "open_loop",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/open-loops"): (
        "CreateOpenLoopSuccessResponse",
        _operation_schema(
            "CreateOpenLoopSuccessResponse",
            (
                "open_loop",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/open-loops/{open_loop_id}/status"): (
        "UpdateOpenLoopStatusSuccessResponse",
        _operation_schema(
            "UpdateOpenLoopStatusSuccessResponse",
            (
                "open_loop",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/consents"): (
        "UpsertConsentSuccessResponse",
        _operation_schema(
            "UpsertConsentSuccessResponse",
            (
                "consent",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/consents"): (
        "ListConsentsSuccessResponse",
        _operation_schema(
            "ListConsentsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/policies"): (
        "CreatePolicySuccessResponse",
        _operation_schema(
            "CreatePolicySuccessResponse",
            (
                "policy",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/policies"): (
        "ListPoliciesSuccessResponse",
        _operation_schema(
            "ListPoliciesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/policies/{policy_id}"): (
        "GetPolicySuccessResponse",
        _operation_schema(
            "GetPolicySuccessResponse",
            (
                "policy",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/policies/evaluate"): (
        "EvaluatePolicySuccessResponse",
        _operation_schema(
            "EvaluatePolicySuccessResponse",
            (
                "policy",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tools"): (
        "CreateToolSuccessResponse",
        _operation_schema(
            "CreateToolSuccessResponse",
            (
                "tool",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tools"): (
        "ListToolsSuccessResponse",
        _operation_schema(
            "ListToolsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/tools/allowlist/evaluate"): (
        "EvaluateToolsAllowlistSuccessResponse",
        _operation_schema(
            "EvaluateToolsAllowlistSuccessResponse",
            (
                "allowlist",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tools/route"): (
        "RouteToolSuccessResponse",
        _operation_schema(
            "RouteToolSuccessResponse",
            (
                "tool",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/approvals/requests"): (
        "CreateApprovalRequestSuccessResponse",
        _operation_schema(
            "CreateApprovalRequestSuccessResponse",
            (
                "request",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/approvals"): (
        "ListApprovalsSuccessResponse",
        _operation_schema(
            "ListApprovalsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/approvals/{approval_id}"): (
        "GetApprovalSuccessResponse",
        _operation_schema(
            "GetApprovalSuccessResponse",
            (
                "approval",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/approvals/{approval_id}/approve"): (
        "ApproveApprovalSuccessResponse",
        _operation_schema(
            "ApproveApprovalSuccessResponse",
            (
                "approval",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/approvals/{approval_id}/reject"): (
        "RejectApprovalSuccessResponse",
        _operation_schema(
            "RejectApprovalSuccessResponse",
            (
                "approval",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/approvals/{approval_id}/execute"): (
        "ExecuteApprovedProxySuccessResponse",
        _operation_schema(
            "ExecuteApprovedProxySuccessResponse",
            (
                "approval",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tasks"): (
        "ListTasksSuccessResponse",
        _operation_schema(
            "ListTasksSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/tasks/{task_id}"): (
        "GetTaskSuccessResponse",
        _operation_schema(
            "GetTaskSuccessResponse",
            (
                "task",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tasks/{task_id}/runs"): (
        "CreateTaskRunSuccessResponse",
        _operation_schema(
            "CreateTaskRunSuccessResponse",
            (
                "run",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tasks/{task_id}/runs"): (
        "ListTaskRunsSuccessResponse",
        _operation_schema(
            "ListTaskRunsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-runs/{task_run_id}"): (
        "GetTaskRunSuccessResponse",
        _operation_schema(
            "GetTaskRunSuccessResponse",
            (
                "task_run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-runs/{task_run_id}/tick"): (
        "TickTaskRunSuccessResponse",
        _operation_schema(
            "TickTaskRunSuccessResponse",
            (
                "task_run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-runs/{task_run_id}/pause"): (
        "PauseTaskRunSuccessResponse",
        _operation_schema(
            "PauseTaskRunSuccessResponse",
            (
                "task_run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-runs/{task_run_id}/resume"): (
        "ResumeTaskRunSuccessResponse",
        _operation_schema(
            "ResumeTaskRunSuccessResponse",
            (
                "task_run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-runs/{task_run_id}/cancel"): (
        "CancelTaskRunSuccessResponse",
        _operation_schema(
            "CancelTaskRunSuccessResponse",
            (
                "task_run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/gmail-accounts"): (
        "ConnectGmailAccountSuccessResponse",
        _operation_schema(
            "ConnectGmailAccountSuccessResponse",
            (
                "gmail_account",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/gmail-accounts"): (
        "ListGmailAccountsSuccessResponse",
        _operation_schema(
            "ListGmailAccountsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/gmail-accounts/{gmail_account_id}"): (
        "GetGmailAccountSuccessResponse",
        _operation_schema(
            "GetGmailAccountSuccessResponse",
            (
                "gmail_account",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest"): (
        "IngestGmailMessageSuccessResponse",
        _operation_schema(
            "IngestGmailMessageSuccessResponse",
            (
                "ingest",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/calendar-accounts"): (
        "ConnectCalendarAccountSuccessResponse",
        _operation_schema(
            "ConnectCalendarAccountSuccessResponse",
            (
                "calendar_account",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/calendar-accounts"): (
        "ListCalendarAccountsSuccessResponse",
        _operation_schema(
            "ListCalendarAccountsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/calendar-accounts/{calendar_account_id}"): (
        "GetCalendarAccountSuccessResponse",
        _operation_schema(
            "GetCalendarAccountSuccessResponse",
            (
                "calendar_account",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/calendar-accounts/{calendar_account_id}/events"): (
        "ListCalendarEventsSuccessResponse",
        _operation_schema(
            "ListCalendarEventsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest"): (
        "IngestCalendarEventSuccessResponse",
        _operation_schema(
            "IngestCalendarEventSuccessResponse",
            (
                "ingest",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tasks/{task_id}/workspace"): (
        "CreateTaskWorkspaceSuccessResponse",
        _operation_schema(
            "CreateTaskWorkspaceSuccessResponse",
            (
                "workspace",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/task-workspaces"): (
        "ListTaskWorkspacesSuccessResponse",
        _operation_schema(
            "ListTaskWorkspacesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-workspaces/{task_workspace_id}"): (
        "GetTaskWorkspaceSuccessResponse",
        _operation_schema(
            "GetTaskWorkspaceSuccessResponse",
            (
                "task_workspace",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tasks/{task_id}/steps"): (
        "ListTaskStepsSuccessResponse",
        _operation_schema(
            "ListTaskStepsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-steps/{task_step_id}"): (
        "GetTaskStepSuccessResponse",
        _operation_schema(
            "GetTaskStepSuccessResponse",
            (
                "task_step",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-workspaces/{task_workspace_id}/artifacts"): (
        "RegisterTaskArtifactSuccessResponse",
        _operation_schema(
            "RegisterTaskArtifactSuccessResponse",
            (
                "artifact",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/task-artifacts"): (
        "ListTaskArtifactsSuccessResponse",
        _operation_schema(
            "ListTaskArtifactsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-artifacts/{task_artifact_id}"): (
        "GetTaskArtifactSuccessResponse",
        _operation_schema(
            "GetTaskArtifactSuccessResponse",
            (
                "task_artifact",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-artifacts/{task_artifact_id}/ingest"): (
        "IngestTaskArtifactSuccessResponse",
        _operation_schema(
            "IngestTaskArtifactSuccessResponse",
            (
                "ingest",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/task-artifacts/{task_artifact_id}/chunks"): (
        "ListTaskArtifactChunksSuccessResponse",
        _operation_schema(
            "ListTaskArtifactChunksSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/tasks/{task_id}/artifact-chunks/retrieve"): (
        "RetrieveTaskArtifactChunksSuccessResponse",
        _operation_schema(
            "RetrieveTaskArtifactChunksSuccessResponse",
            (
                "retrieve",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/retrieve"): (
        "RetrieveTaskArtifactChunksForArtifactSuccessResponse",
        _operation_schema(
            "RetrieveTaskArtifactChunksForArtifactSuccessResponse",
            (
                "retrieve",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval"): (
        "RetrieveSemanticTaskArtifactChunksSuccessResponse",
        _operation_schema(
            "RetrieveSemanticTaskArtifactChunksSuccessResponse",
            (
                "semantic_retrieval",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval"): (
        "RetrieveSemanticArtifactChunksForArtifactSuccessResponse",
        _operation_schema(
            "RetrieveSemanticArtifactChunksForArtifactSuccessResponse",
            (
                "semantic_retrieval",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/tasks/{task_id}/steps"): (
        "CreateNextTaskStepSuccessResponse",
        _operation_schema(
            "CreateNextTaskStepSuccessResponse",
            (
                "step",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-steps/{task_step_id}/transition"): (
        "TransitionTaskStepSuccessResponse",
        _operation_schema(
            "TransitionTaskStepSuccessResponse",
            (
                "task_step",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/execution-budgets"): (
        "CreateExecutionBudgetSuccessResponse",
        _operation_schema(
            "CreateExecutionBudgetSuccessResponse",
            (
                "execution_budget",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/execution-budgets"): (
        "ListExecutionBudgetsSuccessResponse",
        _operation_schema(
            "ListExecutionBudgetsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/execution-budgets/{execution_budget_id}"): (
        "GetExecutionBudgetSuccessResponse",
        _operation_schema(
            "GetExecutionBudgetSuccessResponse",
            (
                "execution_budget",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/execution-budgets/{execution_budget_id}/deactivate"): (
        "DeactivateExecutionBudgetSuccessResponse",
        _operation_schema(
            "DeactivateExecutionBudgetSuccessResponse",
            (
                "execution_budget",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/execution-budgets/{execution_budget_id}/supersede"): (
        "SupersedeExecutionBudgetSuccessResponse",
        _operation_schema(
            "SupersedeExecutionBudgetSuccessResponse",
            (
                "execution_budget",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tool-executions"): (
        "ListToolExecutionsSuccessResponse",
        _operation_schema(
            "ListToolExecutionsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/tool-executions/{execution_id}"): (
        "GetToolExecutionSuccessResponse",
        _operation_schema(
            "GetToolExecutionSuccessResponse",
            (
                "tool_execution",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/tools/{tool_id}"): (
        "GetToolSuccessResponse",
        _operation_schema(
            "GetToolSuccessResponse",
            (
                "tool",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/memories/extract-explicit-preferences"): (
        "ExtractExplicitPreferencesSuccessResponse",
        _operation_schema(
            "ExtractExplicitPreferencesSuccessResponse",
            ("admissions", "candidates", "summary"),
        ),
    ),
    ("POST", "/v0/open-loops/extract-explicit-commitments"): (
        "ExtractExplicitCommitmentsSuccessResponse",
        _operation_schema(
            "ExtractExplicitCommitmentsSuccessResponse",
            ("admissions", "candidates", "summary"),
        ),
    ),
    ("POST", "/v0/memories/capture-explicit-signals"): (
        "CaptureExplicitSignalsSuccessResponse",
        _operation_schema(
            "CaptureExplicitSignalsSuccessResponse",
            ("commitments", "preferences", "summary"),
        ),
    ),
    ("POST", "/v0/continuity/captures"): (
        "CreateContinuityCaptureSuccessResponse",
        _operation_schema(
            "CreateContinuityCaptureSuccessResponse",
            (
                "capture",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/workspace"): (
        "GetVnextWorkspaceSuccessResponse",
        _operation_schema(
            "GetVnextWorkspaceSuccessResponse",
            (
                "workspace",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/sources"): (
        "CreateVnextSourceSuccessResponse",
        _operation_schema(
            "CreateVnextSourceSuccessResponse",
            (
                "source",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/projects"): (
        "CreateVnextProjectSuccessResponse",
        _operation_schema("CreateVnextProjectSuccessResponse", ("project",), required=("project",), closed=True),
    ),
    ("GET", "/v0/vnext/projects"): (
        "ListVnextProjectsSuccessResponse",
        _operation_schema(
            "ListVnextProjectsSuccessResponse",
            ("count", "items", "order"),
            required=("count", "items", "order"),
            closed=True,
        ),
    ),
    ("GET", "/v0/vnext/connectors"): (
        "ListVnextConnectorsSuccessResponse",
        _operation_schema(
            "ListVnextConnectorsSuccessResponse",
            ("count", "items", "order"),
            required=("count", "items", "order"),
            closed=True,
        ),
    ),
    ("GET", "/v0/vnext/connectors/health"): (
        "GetVnextConnectorsHealthSuccessResponse",
        _operation_schema(
            "GetVnextConnectorsHealthSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/vnext/connectors/{connector_name}/status"): (
        "GetVnextConnectorStatusSuccessResponse",
        _operation_schema(
            "GetVnextConnectorStatusSuccessResponse",
            ("config", "health", "recent_captures", "recent_failures"),
            required=("config", "health", "recent_captures", "recent_failures"),
            closed=True,
        ),
    ),
    ("PATCH", "/v0/vnext/connectors/{connector_name}/config"): (
        "UpdateVnextConnectorConfigSuccessResponse",
        _operation_schema(
            "UpdateVnextConnectorConfigSuccessResponse",
            (
                "config",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/connectors/{connector_name}/sync"): (
        "SyncVnextConnectorSuccessResponse",
        _operation_schema(
            "SyncVnextConnectorSuccessResponse",
            (
                "connector",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/connectors/telegram/sync"): (
        "SyncVnextTelegramConnectorSuccessResponse",
        _operation_schema(
            "SyncVnextTelegramConnectorSuccessResponse",
            (
                "telegram",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/connectors/local-folder/sync"): (
        "SyncVnextLocalFolderConnectorSuccessResponse",
        _operation_schema(
            "SyncVnextLocalFolderConnectorSuccessResponse",
            (
                "local_folder",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"): (
        "CreateVnextBrowserClipCapabilitySuccessResponse",
        _operation_schema(
            "CreateVnextBrowserClipCapabilitySuccessResponse",
            (
                "status",
                "capability",
                "origin",
                "expires_at",
                "one_time",
            ),
        ),
    ),
    ("POST", "/v0/vnext/connectors/browser-clipper/capture"): (
        "CaptureVnextBrowserClipSuccessResponse",
        _operation_schema(
            "CaptureVnextBrowserClipSuccessResponse",
            (
                "browser_clipper",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/agents/ingest-output"): (
        "IngestVnextAgentOutputSuccessResponse",
        _operation_schema(
            "IngestVnextAgentOutputSuccessResponse",
            (
                "ingest_output",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/dogfooding"): (
        "GetVnextDogfoodingDashboardSuccessResponse",
        _operation_schema(
            "GetVnextDogfoodingDashboardSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/vnext/doctor"): (
        "GetVnextDoctorSuccessResponse",
        _operation_schema(
            "GetVnextDoctorSuccessResponse",
            (
                "doctor",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/doctor/run"): (
        "RunVnextDoctorSuccessResponse",
        _operation_schema(
            "RunVnextDoctorSuccessResponse",
            (
                "run",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"): (
        "RecordVnextArtifactInsightFeedbackSuccessResponse",
        _operation_schema(
            "RecordVnextArtifactInsightFeedbackSuccessResponse",
            (
                "insight_feedback",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/sources/{source_id}"): (
        "GetVnextSourceSuccessResponse",
        _operation_schema(
            "GetVnextSourceSuccessResponse",
            (
                "source",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/sources/{source_id}/review"): (
        "ReviewVnextSourceSuccessResponse",
        _operation_schema(
            "ReviewVnextSourceSuccessResponse",
            ("archived", "source", "trace"),
            required=("archived", "source", "trace"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/sources/{source_id}/regenerate"): (
        "RegenerateVnextSourceSuccessResponse",
        _operation_schema("RegenerateVnextSourceSuccessResponse", ("source_id", "memory_ids", "open_loop_ids", "memory_count", "open_loop_count"), closed=True),
    ),
    ("GET", "/v0/vnext/traces/sources/{source_id}"): (
        "GetVnextSourceTraceSuccessResponse",
        _operation_schema(
            "GetVnextSourceTraceSuccessResponse",
            (
                "source",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"): (
        "GetVnextArtifactTraceSuccessResponse",
        _operation_schema(
            "GetVnextArtifactTraceSuccessResponse",
            (
                "artifact",
                "status",
            ),
        ),
    ),
    ("DELETE", "/v0/vnext/sources/{source_id}"): (
        "DeleteVnextSourceSuccessResponse",
        _operation_schema(
            "DeleteVnextSourceSuccessResponse",
            (
                "status",
                "source_id",
            ),
        ),
    ),
    ("POST", "/v0/vnext/context-packs"): (
        "CreateVnextContextPackSuccessResponse",
        _operation_schema(
            "CreateVnextContextPackSuccessResponse",
            (
                "context_pack",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/context-tree"): (
        "GetVnextContextTreeSuccessResponse",
        _operation_schema(
            "GetVnextContextTreeSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/{memory_id}/review"): (
        "ReviewVnextMemorySuccessResponse",
        _operation_schema(
            "ReviewVnextMemorySuccessResponse",
            ("consolidation_acceptance", "memory"),
            required=("memory",),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/memory-proposals"): (
        "CreateVnextMemoryProposalSuccessResponse",
        _operation_schema(
            "CreateVnextMemoryProposalSuccessResponse",
            ("policy_decision", "proposal", "review_required"),
            required=("policy_decision", "proposal", "review_required"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/memories/commit"): (
        "CommitVnextMemorySuccessResponse",
        _operation_schema(
            "CommitVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/confirm"): (
        "ConfirmVnextMemorySuccessResponse",
        _operation_schema(
            "ConfirmVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/undo"): (
        "UndoVnextMemorySuccessResponse",
        _operation_schema(
            "UndoVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/correct"): (
        "CorrectVnextMemorySuccessResponse",
        _operation_schema(
            "CorrectVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/forget"): (
        "ForgetVnextMemorySuccessResponse",
        _operation_schema(
            "ForgetVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/expire"): (
        "ExpireVnextMemorySuccessResponse",
        _operation_schema(
            "ExpireVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/unexpire"): (
        "UnexpireVnextMemorySuccessResponse",
        _operation_schema(
            "UnexpireVnextMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/accept-consolidation"): (
        "AcceptVnextMemoryConsolidationSuccessResponse",
        _operation_schema(
            "AcceptVnextMemoryConsolidationSuccessResponse",
            (
                "accept_consolidation",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/memories/redact"): (
        "RedactVnextMemorySuccessResponse",
        _operation_schema(
            "RedactVnextMemorySuccessResponse",
            (
                "forgotten_first",
                "idempotent_replay",
                "memory",
                "reason",
                "redacted_artifact_ids",
                "redacted_artifacts",
                "redacted_events",
                "redacted_provenance_links",
                "redacted_quality_ratings",
                "redacted_revisions",
                "redaction_marker",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/memories/recent-commits"): (
        "ListVnextRecentMemoryCommitsSuccessResponse",
        _operation_schema(
            "ListVnextRecentMemoryCommitsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/vnext/memories/{memory_id}/audit"): (
        "GetVnextMemoryAuditSuccessResponse",
        _operation_schema(
            "GetVnextMemoryAuditSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/vnext/artifacts/generate/daily-brief"): (
        "GenerateVnextDailyBriefSuccessResponse",
        _operation_schema(
            "GenerateVnextDailyBriefSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"): (
        "GenerateVnextWeeklySynthesisSuccessResponse",
        _operation_schema(
            "GenerateVnextWeeklySynthesisSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("POST", "/v0/vnext/artifacts/generate/connections"): (
        "GenerateVnextConnectionReportSuccessResponse",
        _operation_schema(
            "GenerateVnextConnectionReportSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("POST", "/v0/vnext/artifacts/generate/contradictions"): (
        "GenerateVnextContradictionReportSuccessResponse",
        _operation_schema(
            "GenerateVnextContradictionReportSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("POST", "/v0/vnext/queue/tasks"): (
        "CreateVnextQueueTaskSuccessResponse",
        _operation_schema(
            "CreateVnextQueueTaskSuccessResponse",
            (
                "task",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/queue/process-next"): (
        "ProcessNextVnextQueueTaskSuccessResponse",
        _operation_schema(
            "ProcessNextVnextQueueTaskSuccessResponse",
            (
                "queue",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/artifacts"): (
        "ListVnextArtifactsSuccessResponse",
        _operation_schema(
            "ListVnextArtifactsSuccessResponse",
            ("count", "items", "order"),
            required=("count", "items", "order"),
            closed=True,
        ),
    ),
    ("GET", "/v0/vnext/artifacts/{artifact_id}"): (
        "GetVnextArtifactSuccessResponse",
        _operation_schema(
            "GetVnextArtifactSuccessResponse",
            (
                "artifact",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/review"): (
        "ReviewVnextArtifactSuccessResponse",
        _operation_schema(
            "ReviewVnextArtifactSuccessResponse",
            (
                "artifact",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"): (
        "RateVnextArtifactQualitySuccessResponse",
        _operation_schema(
            "RateVnextArtifactQualitySuccessResponse",
            (
                "quality_rating",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/quality-evals"): (
        "ListVnextQualityEvalsSuccessResponse",
        _operation_schema(
            "ListVnextQualityEvalsSuccessResponse",
            ("count", "export", "items", "order"),
            required=("count", "export", "items", "order"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/export"): (
        "ExportVnextArtifactSuccessResponse",
        _operation_schema(
            "ExportVnextArtifactSuccessResponse",
            ("artifact_id", "output_path"),
            required=("artifact_id", "output_path"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/graph/edges/{edge_id}/review"): (
        "ReviewVnextGraphEdgeSuccessResponse",
        _operation_schema(
            "ReviewVnextGraphEdgeSuccessResponse",
            (
                "edge",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): (
        "GetVnextGraphNeighborhoodSuccessResponse",
        _operation_schema(
            "GetVnextGraphNeighborhoodSuccessResponse",
            (
                "neighborhood",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/beliefs/{belief_id}/review"): (
        "ReviewVnextBeliefSuccessResponse",
        _operation_schema(
            "ReviewVnextBeliefSuccessResponse",
            (
                "belief",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/vnext/beliefs/{belief_id}/state"): (
        "GetVnextBeliefStateSuccessResponse",
        _operation_schema(
            "GetVnextBeliefStateSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/vnext/projects/update-candidates"): (
        "GenerateVnextProjectUpdateCandidateSuccessResponse",
        _operation_schema(
            "GenerateVnextProjectUpdateCandidateSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"): (
        "ReviewVnextProjectUpdateCandidateSuccessResponse",
        _operation_schema(
            "ReviewVnextProjectUpdateCandidateSuccessResponse",
            _ARTIFACT_RESPONSE_FIELDS,
        ),
    ),
    ("GET", "/v0/vnext/projects/{project_id}/dashboard"): (
        "GetVnextProjectDashboardSuccessResponse",
        _operation_schema(
            "GetVnextProjectDashboardSuccessResponse",
            (
                "dashboard",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/open-loops"): (
        "CreateVnextOpenLoopSuccessResponse",
        _operation_schema("CreateVnextOpenLoopSuccessResponse", ("open_loop",), required=("open_loop",), closed=True),
    ),
    ("GET", "/v0/vnext/settings/brain-charter"): (
        "GetVnextBrainCharterSuccessResponse",
        _operation_schema(
            "GetVnextBrainCharterSuccessResponse", ("brain_charter",), required=("brain_charter",), closed=True
        ),
    ),
    ("PUT", "/v0/vnext/settings/brain-charter"): (
        "UpsertVnextBrainCharterSuccessResponse",
        _operation_schema(
            "UpsertVnextBrainCharterSuccessResponse", ("brain_charter",), required=("brain_charter",), closed=True
        ),
    ),
    ("GET", "/v0/vnext/scheduler/status"): (
        "GetVnextSchedulerStatusSuccessResponse",
        _operation_schema(
            "GetVnextSchedulerStatusSuccessResponse",
            _SCHEDULER_STATUS_RESPONSE_FIELDS,
            closed=True,
        ),
    ),
    ("GET", "/v0/vnext/scheduler/runs"): (
        "ListVnextSchedulerRunsSuccessResponse",
        _operation_schema(
            "ListVnextSchedulerRunsSuccessResponse", ("count", "items"), required=("count", "items"), closed=True
        ),
    ),
    ("GET", "/v0/vnext/scheduler/failures"): (
        "ListVnextSchedulerFailuresSuccessResponse",
        _operation_schema(
            "ListVnextSchedulerFailuresSuccessResponse", ("count", "items"), required=("count", "items"), closed=True
        ),
    ),
    ("GET", "/v0/vnext/agents/policy-telemetry"): (
        "GetVnextAgentPolicyTelemetrySuccessResponse",
        _operation_schema(
            "GetVnextAgentPolicyTelemetrySuccessResponse", ("summary",), required=("summary",), closed=True
        ),
    ),
    ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"): (
        "PatchVnextSchedulerWorkflowSuccessResponse",
        _operation_schema(
            "PatchVnextSchedulerWorkflowSuccessResponse",
            ("policy_decision", "workflow"),
            required=("policy_decision", "workflow"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"): (
        "RunVnextSchedulerWorkflowNowSuccessResponse",
        _operation_schema(
            "RunVnextSchedulerWorkflowNowSuccessResponse",
            ("policy_decision",),
            required=("policy_decision",),
            closed=False,
        ),
    ),
    ("POST", "/v0/vnext/scheduler/run-due"): (
        "RunVnextSchedulerDueSuccessResponse",
        _operation_schema(
            "RunVnextSchedulerDueSuccessResponse", ("policy_decision",), required=("policy_decision",), closed=False
        ),
    ),
    ("POST", "/v0/vnext/scheduler/pause"): (
        "PauseVnextSchedulerSuccessResponse",
        _operation_schema(
            "PauseVnextSchedulerSuccessResponse",
            (
                "scheduler",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/scheduler/resume"): (
        "ResumeVnextSchedulerSuccessResponse",
        _operation_schema(
            "ResumeVnextSchedulerSuccessResponse",
            (
                "scheduler",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/vnext/open-loops/extract"): (
        "ExtractVnextOpenLoopsSuccessResponse",
        _operation_schema(
            "ExtractVnextOpenLoopsSuccessResponse",
            ("created_count", "open_loops"),
            required=("created_count", "open_loops"),
            closed=True,
        ),
    ),
    ("POST", "/v0/vnext/open-loops/{loop_id}/review"): (
        "ReviewVnextOpenLoopSuccessResponse",
        _operation_schema(
            "ReviewVnextOpenLoopSuccessResponse",
            (
                "open_loop",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/continuity/captures/candidates"): (
        "CreateContinuityCaptureCandidatesSuccessResponse",
        _operation_schema(
            "CreateContinuityCaptureCandidatesSuccessResponse",
            (
                "candidate",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/continuity/captures/commit"): (
        "CommitContinuityCaptureCandidatesSuccessResponse",
        _operation_schema(
            "CommitContinuityCaptureCandidatesSuccessResponse",
            (
                "capture",
                "status",
            ),
        ),
    ),
    ("POST", "/v1/memory/operations/candidates/generate"): (
        "GenerateMemoryOperationCandidatesEndpointSuccessResponse",
        _operation_schema(
            "GenerateMemoryOperationCandidatesEndpointSuccessResponse",
            (
                "candidate",
                "status",
            ),
        ),
    ),
    ("GET", "/v1/memory/operations/candidates"): (
        "ListMemoryOperationCandidatesEndpointSuccessResponse",
        _operation_schema(
            "ListMemoryOperationCandidatesEndpointSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v1/memory/operations/commit"): (
        "CommitMemoryOperationsEndpointSuccessResponse",
        _operation_schema(
            "CommitMemoryOperationsEndpointSuccessResponse",
            (
                "operation",
                "status",
            ),
        ),
    ),
    ("GET", "/v1/memory/operations"): (
        "ListMemoryOperationsEndpointSuccessResponse",
        _operation_schema(
            "ListMemoryOperationsEndpointSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/continuity/captures"): (
        "ListContinuityCapturesSuccessResponse",
        _operation_schema(
            "ListContinuityCapturesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/continuity/captures/{capture_event_id}"): (
        "GetContinuityCaptureSuccessResponse",
        _operation_schema(
            "GetContinuityCaptureSuccessResponse",
            (
                "capture",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/continuity/review-queue/{continuity_object_id}/corrections"): (
        "ApplyContinuityCorrectionEndpointSuccessResponse",
        _operation_schema(
            "ApplyContinuityCorrectionEndpointSuccessResponse",
            (
                "correction",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/task-briefs/{task_brief_id}"): (
        "GetV0TaskBriefSuccessResponse",
        _operation_schema(
            "GetV0TaskBriefSuccessResponse",
            (
                "task_brief",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories"): (
        "ListMemoriesSuccessResponse",
        _operation_schema(
            "ListMemoriesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/memories/review-queue"): (
        "ListMemoryReviewQueueSuccessResponse",
        _operation_schema(
            "ListMemoryReviewQueueSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/memories/quality-gate"): (
        "GetMemoriesQualityGateSuccessResponse",
        _operation_schema(
            "GetMemoriesQualityGateSuccessResponse",
            (
                "quality_gate",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories/evaluation-summary"): (
        "GetMemoriesEvaluationSummarySuccessResponse",
        _operation_schema(
            "GetMemoriesEvaluationSummarySuccessResponse",
            (
                "evaluation_summary",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/memories/semantic-retrieval"): (
        "RetrieveSemanticMemoriesSuccessResponse",
        _operation_schema(
            "RetrieveSemanticMemoriesSuccessResponse",
            (
                "semantic_retrieval",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories/{memory_id}"): (
        "GetMemorySuccessResponse",
        _operation_schema(
            "GetMemorySuccessResponse",
            (
                "memory",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories/{memory_id}/revisions"): (
        "ListMemoryRevisionsSuccessResponse",
        _operation_schema(
            "ListMemoryRevisionsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/memories/{memory_id}/labels"): (
        "CreateMemoryReviewLabelSuccessResponse",
        _operation_schema(
            "CreateMemoryReviewLabelSuccessResponse",
            (
                "label",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories/{memory_id}/labels"): (
        "ListMemoryReviewLabelsSuccessResponse",
        _operation_schema(
            "ListMemoryReviewLabelsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/embedding-configs"): (
        "CreateEmbeddingConfigSuccessResponse",
        _operation_schema(
            "CreateEmbeddingConfigSuccessResponse",
            (
                "embedding_config",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/embedding-configs"): (
        "ListEmbeddingConfigsSuccessResponse",
        _operation_schema(
            "ListEmbeddingConfigsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("POST", "/v0/memory-embeddings"): (
        "UpsertMemoryEmbeddingSuccessResponse",
        _operation_schema(
            "UpsertMemoryEmbeddingSuccessResponse",
            (
                "memory_embedding",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/task-artifact-chunk-embeddings"): (
        "UpsertTaskArtifactChunkEmbeddingSuccessResponse",
        _operation_schema(
            "UpsertTaskArtifactChunkEmbeddingSuccessResponse",
            (
                "task_artifact_chunk_embedding",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/memories/{memory_id}/embeddings"): (
        "ListMemoryEmbeddingsSuccessResponse",
        _operation_schema(
            "ListMemoryEmbeddingsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-artifacts/{task_artifact_id}/chunk-embeddings"): (
        "ListTaskArtifactChunkEmbeddingsForArtifactSuccessResponse",
        _operation_schema(
            "ListTaskArtifactChunkEmbeddingsForArtifactSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings"): (
        "ListTaskArtifactChunkEmbeddingsSuccessResponse",
        _operation_schema(
            "ListTaskArtifactChunkEmbeddingsSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/memory-embeddings/{memory_embedding_id}"): (
        "GetMemoryEmbeddingSuccessResponse",
        _operation_schema(
            "GetMemoryEmbeddingSuccessResponse",
            (
                "memory_embedding",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}"): (
        "GetTaskArtifactChunkEmbeddingSuccessResponse",
        _operation_schema(
            "GetTaskArtifactChunkEmbeddingSuccessResponse",
            (
                "task_artifact_chunk_embedding",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/entities"): (
        "CreateEntitySuccessResponse",
        _operation_schema(
            "CreateEntitySuccessResponse",
            (
                "entity",
                "status",
            ),
        ),
    ),
    ("POST", "/v0/entity-edges"): (
        "CreateEntityEdgeSuccessResponse",
        _operation_schema(
            "CreateEntityEdgeSuccessResponse",
            (
                "entity_edge",
                "status",
            ),
        ),
    ),
    ("GET", "/v0/entities"): (
        "ListEntitiesSuccessResponse",
        _operation_schema(
            "ListEntitiesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/entities/{entity_id}/edges"): (
        "ListEntityEdgesSuccessResponse",
        _operation_schema(
            "ListEntityEdgesSuccessResponse",
            (
                "items",
                "summary",
            ),
        ),
    ),
    ("GET", "/v0/entities/{entity_id}"): (
        "GetEntitySuccessResponse",
        _operation_schema(
            "GetEntitySuccessResponse",
            (
                "entity",
                "status",
            ),
        ),
    ),
    ("POST", "/v1/workspaces/bootstrap"): (
        "BootstrapV1WorkspaceSuccessResponse",
        _operation_schema(
            "BootstrapV1WorkspaceSuccessResponse",
            ("bootstrap", "seeded_provider_count", "workspace"),
            required=("bootstrap", "seeded_provider_count", "workspace"),
            closed=True,
        ),
    ),
    ("GET", "/v1/workspaces/bootstrap/status"): (
        "GetV1WorkspaceBootstrapStatusSuccessResponse",
        _operation_schema(
            "GetV1WorkspaceBootstrapStatusSuccessResponse",
            ("bootstrap", "workspace"),
            required=("bootstrap", "workspace"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers"): (
        "RegisterV1ProviderSuccessResponse",
        _operation_schema(
            "RegisterV1ProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers/ollama/register"): (
        "RegisterV1OllamaProviderSuccessResponse",
        _operation_schema(
            "RegisterV1OllamaProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers/llamacpp/register"): (
        "RegisterV1LlamacppProviderSuccessResponse",
        _operation_schema(
            "RegisterV1LlamacppProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers/vllm/register"): (
        "RegisterV1VllmProviderSuccessResponse",
        _operation_schema(
            "RegisterV1VllmProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers/azure/register"): (
        "RegisterV1AzureProviderSuccessResponse",
        _operation_schema(
            "RegisterV1AzureProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("GET", "/v1/providers"): (
        "ListV1ProvidersSuccessResponse",
        _operation_schema(
            "ListV1ProvidersSuccessResponse", ("items", "summary"), required=("items", "summary"), closed=True
        ),
    ),
    ("GET", "/v1/providers/{provider_id}"): (
        "GetV1ProviderSuccessResponse",
        _operation_schema(
            "GetV1ProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("PATCH", "/v1/providers/{provider_id}"): (
        "UpdateV1ProviderSuccessResponse",
        _operation_schema(
            "UpdateV1ProviderSuccessResponse",
            ("capabilities", "provider"),
            required=("capabilities", "provider"),
            closed=True,
        ),
    ),
    ("POST", "/v1/providers/test"): (
        "TestV1ProviderSuccessResponse",
        _operation_schema(
            "TestV1ProviderSuccessResponse",
            ("capabilities", "provider", "result"),
            required=("capabilities", "provider", "result"),
            closed=True,
        ),
    ),
    ("POST", "/v1/runtime/invoke"): (
        "InvokeV1RuntimeSuccessResponse",
        _operation_schema(
            "InvokeV1RuntimeSuccessResponse",
            (
                "assistant",
                "detail",
                "metadata",
                "response_job",
                "trace",
            ),
            closed=False,
        ),
    ),
}


# Property types are recorded by operation, never inferred from a field name.
# Closed envelopes cover every declared property; the two asynchronous
# operations also receive explicit types and variant requirements while their
# top-level envelope remains permissive for forward-compatible metadata.
_OPENAPI_EXPLICIT_PROPERTY_SCHEMAS: dict[tuple[str, str], dict[str, dict[str, object]]] = {
    ("GET", "/healthz"): _typed_properties(objects=("services",), strings=("status", "environment")),
    ("POST", "/v0/context/compile"): _typed_properties(
        objects=("context_pack", "metadata"),
        integers=("trace_event_count",),
        strings=("trace_id",),
    ),
    ("POST", "/v0/memories/admit"): _typed_properties(
        objects=("open_loop",),
        nullable_objects=("memory", "revision"),
        strings=("decision", "reason"),
    ),
    ("POST", "/v0/vnext/projects"): _typed_properties(objects=("project",)),
    ("GET", "/v0/vnext/projects"): _typed_properties(
        object_arrays=("items",), string_arrays=("order",), integers=("count",)
    ),
    ("GET", "/v0/vnext/connectors"): _typed_properties(
        object_arrays=("items",), string_arrays=("order",), integers=("count",)
    ),
    ("GET", "/v0/vnext/connectors/{connector_name}/status"): _typed_properties(
        objects=("config", "health"), object_arrays=("recent_captures", "recent_failures")
    ),
    ("POST", "/v0/vnext/sources/{source_id}/review"): _typed_properties(
        objects=("source", "trace"), booleans=("archived",)
    ),
    ("POST", "/v0/vnext/sources/{source_id}/regenerate"): _typed_properties(
        strings=("source_id",), string_arrays=("memory_ids", "open_loop_ids"), integers=("memory_count", "open_loop_count"),
    ),
    ("POST", "/v0/vnext/memories/{memory_id}/review"): _typed_properties(
        objects=("memory",), nullable_objects=("consolidation_acceptance",)
    ),
    ("POST", "/v0/vnext/memory-proposals"): _typed_properties(
        objects=("policy_decision", "proposal"), booleans=("review_required",)
    ),
    ("GET", "/v0/vnext/artifacts"): _typed_properties(
        object_arrays=("items",), string_arrays=("order",), integers=("count",)
    ),
    ("GET", "/v0/vnext/quality-evals"): _typed_properties(
        objects=("export",),
        object_arrays=("items",),
        string_arrays=("order",),
        integers=("count",),
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/export"): _typed_properties(strings=("artifact_id", "output_path")),
    ("POST", "/v0/vnext/open-loops"): _typed_properties(objects=("open_loop",)),
    ("GET", "/v0/vnext/settings/brain-charter"): _typed_properties(objects=("brain_charter",)),
    ("PUT", "/v0/vnext/settings/brain-charter"): _typed_properties(objects=("brain_charter",)),
    ("GET", "/v0/vnext/scheduler/status"): _typed_properties(
        objects=("daemon", "last_success_by_workflow"),
        nullable_objects=("currently_running_workflow", "last_due_scan", "last_failure", "next_due_workflow"),
        object_arrays=("recent_failures", "recent_runs", "workflows"),
        strings=("mode",),
        integers=("enabled_count", "paused_count"),
        booleans=("disabled_by_default",),
    ),
    ("GET", "/v0/vnext/scheduler/runs"): _typed_properties(object_arrays=("items",), integers=("count",)),
    ("GET", "/v0/vnext/scheduler/failures"): _typed_properties(object_arrays=("items",), integers=("count",)),
    ("GET", "/v0/vnext/agents/policy-telemetry"): _typed_properties(objects=("summary",)),
    ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"): _typed_properties(
        objects=("policy_decision", "workflow")
    ),
    ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"): _typed_properties(objects=("policy_decision",)),
    ("POST", "/v0/vnext/scheduler/run-due"): _typed_properties(objects=("policy_decision",)),
    ("POST", "/v0/vnext/open-loops/extract"): _typed_properties(
        object_arrays=("open_loops",), integers=("created_count",)
    ),
    ("POST", "/v1/workspaces/bootstrap"): _typed_properties(
        objects=("bootstrap", "workspace"),
        integers=("seeded_provider_count",),
    ),
    ("GET", "/v1/workspaces/bootstrap/status"): _typed_properties(
        objects=("bootstrap", "workspace"),
    ),
    ("POST", "/v1/providers"): _typed_properties(objects=("capabilities", "provider")),
    ("POST", "/v1/providers/ollama/register"): _typed_properties(objects=("capabilities", "provider")),
    ("POST", "/v1/providers/llamacpp/register"): _typed_properties(objects=("capabilities", "provider")),
    ("POST", "/v1/providers/vllm/register"): _typed_properties(objects=("capabilities", "provider")),
    ("POST", "/v1/providers/azure/register"): _typed_properties(objects=("capabilities", "provider")),
    ("GET", "/v1/providers"): _typed_properties(objects=("summary",), object_arrays=("items",)),
    ("GET", "/v1/providers/{provider_id}"): _typed_properties(objects=("capabilities", "provider")),
    ("PATCH", "/v1/providers/{provider_id}"): _typed_properties(objects=("capabilities", "provider")),
    ("POST", "/v1/providers/test"): _typed_properties(objects=("capabilities", "provider", "result")),
    ("POST", "/v1/runtime/invoke"): _typed_properties(
        objects=("assistant", "detail", "metadata", "response_job", "trace")
    ),
}


# The remaining permissive envelopes are still typed by literal operation
# key. They intentionally keep optional fields/additional properties because
# their full envelope is helper- or variable-backed, but their declared
# properties are not guessed from spelling conventions.
_OPENAPI_EXPLICIT_PROPERTY_SCHEMAS.update(
    {
        ("GET", "/v0/traces"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/traces/{trace_id}"): _typed_properties(objects=("trace",), strings=("status",)),
        ("GET", "/v0/traces/{trace_id}/events"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/open-loops"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/open-loops/{open_loop_id}"): _typed_properties(objects=("open_loop",), strings=("status",)),
        ("POST", "/v0/open-loops"): _typed_properties(objects=("open_loop",), strings=("status",)),
        ("POST", "/v0/open-loops/{open_loop_id}/status"): _typed_properties(
            objects=("open_loop",), strings=("status",)
        ),
        ("POST", "/v0/consents"): _typed_properties(objects=("consent",), strings=("status",)),
        ("GET", "/v0/consents"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v0/policies"): _typed_properties(objects=("policy",), strings=("status",)),
        ("GET", "/v0/policies"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/policies/{policy_id}"): _typed_properties(objects=("policy",), strings=("status",)),
        ("POST", "/v0/policies/evaluate"): _typed_properties(objects=("policy",), strings=("status",)),
        ("POST", "/v0/tools"): _typed_properties(objects=("tool",), strings=("status",)),
        ("GET", "/v0/tools"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v0/tools/allowlist/evaluate"): _typed_properties(objects=("allowlist",), strings=("status",)),
        ("POST", "/v0/tools/route"): _typed_properties(objects=("tool",), strings=("status",)),
        ("POST", "/v0/approvals/requests"): _typed_properties(objects=("request",), strings=("status",)),
        ("GET", "/v0/approvals"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/approvals/{approval_id}"): _typed_properties(objects=("approval",), strings=("status",)),
        ("POST", "/v0/approvals/{approval_id}/approve"): _typed_properties(objects=("approval",), strings=("status",)),
        ("POST", "/v0/approvals/{approval_id}/reject"): _typed_properties(objects=("approval",), strings=("status",)),
        ("POST", "/v0/approvals/{approval_id}/execute"): _typed_properties(objects=("approval",), strings=("status",)),
        ("GET", "/v0/tasks"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/tasks/{task_id}"): _typed_properties(objects=("task",), strings=("status",)),
        ("POST", "/v0/tasks/{task_id}/runs"): _typed_properties(objects=("run",), strings=("status",)),
        ("GET", "/v0/tasks/{task_id}/runs"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/task-runs/{task_run_id}"): _typed_properties(objects=("task_run",), strings=("status",)),
        ("POST", "/v0/task-runs/{task_run_id}/tick"): _typed_properties(objects=("task_run",), strings=("status",)),
        ("POST", "/v0/task-runs/{task_run_id}/pause"): _typed_properties(objects=("task_run",), strings=("status",)),
        ("POST", "/v0/task-runs/{task_run_id}/resume"): _typed_properties(objects=("task_run",), strings=("status",)),
        ("POST", "/v0/task-runs/{task_run_id}/cancel"): _typed_properties(objects=("task_run",), strings=("status",)),
        ("POST", "/v0/gmail-accounts"): _typed_properties(objects=("gmail_account",), strings=("status",)),
        ("GET", "/v0/gmail-accounts"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/gmail-accounts/{gmail_account_id}"): _typed_properties(
            objects=("gmail_account",), strings=("status",)
        ),
        ("POST", "/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest"): _typed_properties(
            objects=("ingest",), strings=("status",)
        ),
        ("POST", "/v0/calendar-accounts"): _typed_properties(objects=("calendar_account",), strings=("status",)),
        ("GET", "/v0/calendar-accounts"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/calendar-accounts/{calendar_account_id}"): _typed_properties(
            objects=("calendar_account",), strings=("status",)
        ),
        ("GET", "/v0/calendar-accounts/{calendar_account_id}/events"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("POST", "/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest"): _typed_properties(
            objects=("ingest",), strings=("status",)
        ),
        ("POST", "/v0/tasks/{task_id}/workspace"): _typed_properties(objects=("workspace",), strings=("status",)),
        ("GET", "/v0/task-workspaces"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/task-workspaces/{task_workspace_id}"): _typed_properties(
            objects=("task_workspace",), strings=("status",)
        ),
        ("GET", "/v0/tasks/{task_id}/steps"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/task-steps/{task_step_id}"): _typed_properties(objects=("task_step",), strings=("status",)),
        ("POST", "/v0/task-workspaces/{task_workspace_id}/artifacts"): _typed_properties(
            objects=("artifact",), strings=("status",)
        ),
        ("GET", "/v0/task-artifacts"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/task-artifacts/{task_artifact_id}"): _typed_properties(
            objects=("task_artifact",), strings=("status",)
        ),
        ("POST", "/v0/task-artifacts/{task_artifact_id}/ingest"): _typed_properties(
            objects=("ingest",), strings=("status",)
        ),
        ("GET", "/v0/task-artifacts/{task_artifact_id}/chunks"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("POST", "/v0/tasks/{task_id}/artifact-chunks/retrieve"): _typed_properties(
            objects=("retrieve",), strings=("status",)
        ),
        ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/retrieve"): _typed_properties(
            objects=("retrieve",), strings=("status",)
        ),
        ("POST", "/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval"): _typed_properties(
            objects=("semantic_retrieval",), strings=("status",)
        ),
        ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval"): _typed_properties(
            objects=("semantic_retrieval",), strings=("status",)
        ),
        ("POST", "/v0/tasks/{task_id}/steps"): _typed_properties(objects=("step",), strings=("status",)),
        ("POST", "/v0/task-steps/{task_step_id}/transition"): _typed_properties(
            objects=("task_step",), strings=("status",)
        ),
        ("POST", "/v0/execution-budgets"): _typed_properties(objects=("execution_budget",), strings=("status",)),
        ("GET", "/v0/execution-budgets"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/execution-budgets/{execution_budget_id}"): _typed_properties(
            objects=("execution_budget",), strings=("status",)
        ),
        ("POST", "/v0/execution-budgets/{execution_budget_id}/deactivate"): _typed_properties(
            objects=("execution_budget",), strings=("status",)
        ),
        ("POST", "/v0/execution-budgets/{execution_budget_id}/supersede"): _typed_properties(
            objects=("execution_budget",), strings=("status",)
        ),
        ("GET", "/v0/tool-executions"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/tool-executions/{execution_id}"): _typed_properties(
            objects=("tool_execution",), strings=("status",)
        ),
        ("GET", "/v0/tools/{tool_id}"): _typed_properties(objects=("tool",), strings=("status",)),
        ("POST", "/v0/memories/extract-explicit-preferences"): _typed_properties(
            objects=("summary",), object_arrays=("admissions", "candidates")
        ),
        ("POST", "/v0/open-loops/extract-explicit-commitments"): _typed_properties(
            objects=("summary",), object_arrays=("admissions", "candidates")
        ),
        ("POST", "/v0/memories/capture-explicit-signals"): _typed_properties(
            objects=("commitments", "preferences", "summary")
        ),
        ("POST", "/v0/continuity/captures"): _typed_properties(objects=("capture",), strings=("status",)),
        ("GET", "/v0/vnext/workspace"): _typed_properties(objects=("workspace",), strings=("status",)),
        ("POST", "/v0/vnext/sources"): _typed_properties(objects=("source",), strings=("status",)),
        ("GET", "/v0/vnext/connectors/health"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("PATCH", "/v0/vnext/connectors/{connector_name}/config"): _typed_properties(
            objects=("config",), strings=("status",)
        ),
        ("POST", "/v0/vnext/connectors/{connector_name}/sync"): _typed_properties(
            objects=("connector",), strings=("status",)
        ),
        ("POST", "/v0/vnext/connectors/telegram/sync"): _typed_properties(objects=("telegram",), strings=("status",)),
    ("POST", "/v0/vnext/connectors/local-folder/sync"): _typed_properties(
        objects=("local_folder",), strings=("status",)
    ),
    ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"): {
        "status": {"type": "string"},
        "capability": {"type": "string", "readOnly": True},
        "origin": {"type": "string", "format": "uri"},
        "expires_at": {"type": "string", "format": "date-time"},
        "one_time": {"type": "boolean"},
    },
    ("POST", "/v0/vnext/connectors/browser-clipper/capture"): _typed_properties(
            objects=("browser_clipper",), strings=("status",)
        ),
        ("POST", "/v0/vnext/agents/ingest-output"): _typed_properties(objects=("ingest_output",), strings=("status",)),
        ("GET", "/v0/vnext/dogfooding"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/vnext/doctor"): _typed_properties(objects=("doctor",), strings=("status",)),
        ("POST", "/v0/vnext/doctor/run"): _typed_properties(objects=("run",), strings=("status",)),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"): _typed_properties(
            objects=("insight_feedback",), strings=("status",)
        ),
        ("GET", "/v0/vnext/sources/{source_id}"): _typed_properties(objects=("source",), strings=("status",)),
        ("GET", "/v0/vnext/traces/sources/{source_id}"): _typed_properties(objects=("source",), strings=("status",)),
        ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"): _typed_properties(
            objects=("artifact",), strings=("status",)
        ),
        ("DELETE", "/v0/vnext/sources/{source_id}"): _typed_properties(strings=("status", "source_id")),
        ("POST", "/v0/vnext/context-packs"): _typed_properties(objects=("context_pack",), strings=("status",)),
        ("GET", "/v0/vnext/context-tree"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v0/vnext/memories/commit"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/confirm"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/undo"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/correct"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/forget"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/expire"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/unexpire"): _typed_properties(objects=("memory",), strings=("status",)),
        ("POST", "/v0/vnext/memories/accept-consolidation"): _typed_properties(
            objects=("accept_consolidation",), strings=("status",)
        ),
        ("POST", "/v0/vnext/memories/redact"): _typed_properties(
            objects=("memory",),
            strings=("status", "redaction_marker", "reason"),
            string_arrays=("redacted_artifact_ids",),
            integers=(
                "redacted_revisions",
                "redacted_events",
                "redacted_artifacts",
                "redacted_quality_ratings",
                "redacted_provenance_links",
            ),
            booleans=("forgotten_first", "idempotent_replay"),
        ),
        ("GET", "/v0/vnext/memories/recent-commits"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/vnext/memories/{memory_id}/audit"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"): _artifact_response_properties(),
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"): _artifact_response_properties(),
        ("POST", "/v0/vnext/artifacts/generate/connections"): _artifact_response_properties(),
        ("POST", "/v0/vnext/artifacts/generate/contradictions"): _artifact_response_properties(),
        ("POST", "/v0/vnext/queue/tasks"): _typed_properties(objects=("task",), strings=("status",)),
        ("POST", "/v0/vnext/queue/process-next"): _typed_properties(objects=("queue",), strings=("status",)),
        ("GET", "/v0/vnext/artifacts/{artifact_id}"): _typed_properties(objects=("artifact",), strings=("status",)),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/review"): _typed_properties(
            objects=("artifact",), strings=("status",)
        ),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"): _typed_properties(
            objects=("quality_rating",), strings=("status",)
        ),
        ("POST", "/v0/vnext/graph/edges/{edge_id}/review"): _typed_properties(objects=("edge",), strings=("status",)),
        ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): _typed_properties(
            objects=("neighborhood",), strings=("status",)
        ),
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"): _typed_properties(objects=("belief",), strings=("status",)),
        ("GET", "/v0/vnext/beliefs/{belief_id}/state"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("POST", "/v0/vnext/projects/update-candidates"): _artifact_response_properties(),
        ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"): _artifact_response_properties(),
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"): _typed_properties(
            objects=("dashboard",), strings=("status",)
        ),
        ("POST", "/v0/vnext/scheduler/pause"): _typed_properties(objects=("scheduler",), strings=("status",)),
        ("POST", "/v0/vnext/scheduler/resume"): _typed_properties(objects=("scheduler",), strings=("status",)),
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"): _typed_properties(
            objects=("open_loop",), strings=("status",)
        ),
        ("POST", "/v0/continuity/captures/candidates"): _typed_properties(objects=("candidate",), strings=("status",)),
        ("POST", "/v0/continuity/captures/commit"): _typed_properties(objects=("capture",), strings=("status",)),
        ("POST", "/v1/memory/operations/candidates/generate"): _typed_properties(
            objects=("candidate",), strings=("status",)
        ),
        ("GET", "/v1/memory/operations/candidates"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v1/memory/operations/commit"): _typed_properties(objects=("operation",), strings=("status",)),
        ("GET", "/v1/memory/operations"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/continuity/captures"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/continuity/captures/{capture_event_id}"): _typed_properties(
            objects=("capture",), strings=("status",)
        ),
        ("POST", "/v0/continuity/review-queue/{continuity_object_id}/corrections"): _typed_properties(
            objects=("correction",), strings=("status",)
        ),
        ("GET", "/v0/task-briefs/{task_brief_id}"): _typed_properties(objects=("task_brief",), strings=("status",)),
        ("GET", "/v0/memories"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/memories/review-queue"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/memories/quality-gate"): _typed_properties(objects=("quality_gate",), strings=("status",)),
        ("GET", "/v0/memories/evaluation-summary"): _typed_properties(
            objects=("evaluation_summary",), strings=("status",)
        ),
        ("POST", "/v0/memories/semantic-retrieval"): _typed_properties(
            objects=("semantic_retrieval",), strings=("status",)
        ),
        ("GET", "/v0/memories/{memory_id}"): _typed_properties(objects=("memory",), strings=("status",)),
        ("GET", "/v0/memories/{memory_id}/revisions"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("POST", "/v0/memories/{memory_id}/labels"): _typed_properties(objects=("label",), strings=("status",)),
        ("GET", "/v0/memories/{memory_id}/labels"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v0/embedding-configs"): _typed_properties(objects=("embedding_config",), strings=("status",)),
        ("GET", "/v0/embedding-configs"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("POST", "/v0/memory-embeddings"): _typed_properties(objects=("memory_embedding",), strings=("status",)),
        ("POST", "/v0/task-artifact-chunk-embeddings"): _typed_properties(
            objects=("task_artifact_chunk_embedding",), strings=("status",)
        ),
        ("GET", "/v0/memories/{memory_id}/embeddings"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("GET", "/v0/task-artifacts/{task_artifact_id}/chunk-embeddings"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("GET", "/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings"): _typed_properties(
            objects=("summary",), object_arrays=("items",)
        ),
        ("GET", "/v0/memory-embeddings/{memory_embedding_id}"): _typed_properties(
            objects=("memory_embedding",), strings=("status",)
        ),
        ("GET", "/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}"): _typed_properties(
            objects=("task_artifact_chunk_embedding",), strings=("status",)
        ),
        ("POST", "/v0/entities"): _typed_properties(objects=("entity",), strings=("status",)),
        ("POST", "/v0/entity-edges"): _typed_properties(objects=("entity_edge",), strings=("status",)),
        ("GET", "/v0/entities"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/entities/{entity_id}/edges"): _typed_properties(objects=("summary",), object_arrays=("items",)),
        ("GET", "/v0/entities/{entity_id}"): _typed_properties(objects=("entity",), strings=("status",)),
    }
)


for _operation_key, _property_schemas in _OPENAPI_EXPLICIT_PROPERTY_SCHEMAS.items():
    if _operation_key not in OPENAPI_OPERATION_RESPONSE_SCHEMAS:
        raise RuntimeError(f"OpenAPI typed property registry has unknown operation {_operation_key!r}")
    _component_name, _component_schema = OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key]
    _declared_properties = _component_schema.get("properties")
    if not isinstance(_declared_properties, dict):  # pragma: no cover - construction invariant
        raise RuntimeError(f"OpenAPI component {_component_name} has no properties")
    if set(_property_schemas) != set(_declared_properties):
        raise RuntimeError(
            f"OpenAPI typed properties drifted for {_operation_key!r}; "
            f"declared={sorted(_declared_properties)}, typed={sorted(_property_schemas)}"
        )
    _component_schema["properties"] = _property_schemas


# These operations return named TypedDict envelopes from their handler helper.
# Generate the public response schema from that same runtime type instead of
# maintaining a second, speculative list of wrapper fields here.
_OPENAPI_CONTRACT_RESPONSE_TYPES: dict[tuple[str, str], str] = {
    ("GET", "/v0/traces"): "TraceReviewListResponse",
    ("GET", "/v0/traces/{trace_id}"): "TraceReviewDetailResponse",
    ("GET", "/v0/traces/{trace_id}/events"): "TraceReviewEventListResponse",
    ("GET", "/v0/open-loops"): "OpenLoopListResponse",
    ("GET", "/v0/open-loops/{open_loop_id}"): "OpenLoopDetailResponse",
    ("POST", "/v0/open-loops"): "OpenLoopCreateResponse",
    ("POST", "/v0/open-loops/{open_loop_id}/status"): "OpenLoopStatusUpdateResponse",
    ("POST", "/v0/consents"): "ConsentUpsertResponse",
    ("GET", "/v0/consents"): "ConsentListResponse",
    ("POST", "/v0/policies"): "PolicyCreateResponse",
    ("GET", "/v0/policies"): "PolicyListResponse",
    ("GET", "/v0/policies/{policy_id}"): "PolicyDetailResponse",
    ("POST", "/v0/policies/evaluate"): "PolicyEvaluationResponse",
    ("POST", "/v0/tools"): "ToolCreateResponse",
    ("GET", "/v0/tools"): "ToolListResponse",
    ("POST", "/v0/tools/allowlist/evaluate"): "ToolAllowlistEvaluationResponse",
    ("POST", "/v0/tools/route"): "ToolRoutingResponse",
    ("POST", "/v0/approvals/requests"): "ApprovalRequestCreateResponse",
    ("GET", "/v0/approvals"): "ApprovalListResponse",
    ("GET", "/v0/approvals/{approval_id}"): "ApprovalDetailResponse",
    ("POST", "/v0/approvals/{approval_id}/approve"): "ApprovalResolutionResponse",
    ("POST", "/v0/approvals/{approval_id}/reject"): "ApprovalResolutionResponse",
    ("POST", "/v0/approvals/{approval_id}/execute"): "ProxyExecutionResponse",
    ("GET", "/v0/tasks"): "TaskListResponse",
    ("GET", "/v0/tasks/{task_id}"): "TaskDetailResponse",
    ("POST", "/v0/tasks/{task_id}/runs"): "TaskRunCreateResponse",
    ("GET", "/v0/tasks/{task_id}/runs"): "TaskRunListResponse",
    ("GET", "/v0/task-runs/{task_run_id}"): "TaskRunDetailResponse",
    ("POST", "/v0/task-runs/{task_run_id}/tick"): "TaskRunMutationResponse",
    ("POST", "/v0/task-runs/{task_run_id}/pause"): "TaskRunMutationResponse",
    ("POST", "/v0/task-runs/{task_run_id}/resume"): "TaskRunMutationResponse",
    ("POST", "/v0/task-runs/{task_run_id}/cancel"): "TaskRunMutationResponse",
    ("POST", "/v0/gmail-accounts"): "GmailAccountConnectResponse",
    ("GET", "/v0/gmail-accounts"): "GmailAccountListResponse",
    ("GET", "/v0/gmail-accounts/{gmail_account_id}"): "GmailAccountDetailResponse",
    ("POST", "/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest"): (
        "GmailMessageIngestionResponse"
    ),
    ("POST", "/v0/calendar-accounts"): "CalendarAccountConnectResponse",
    ("GET", "/v0/calendar-accounts"): "CalendarAccountListResponse",
    ("GET", "/v0/calendar-accounts/{calendar_account_id}"): "CalendarAccountDetailResponse",
    ("GET", "/v0/calendar-accounts/{calendar_account_id}/events"): "CalendarEventListResponse",
    ("POST", "/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest"): (
        "CalendarEventIngestionResponse"
    ),
    ("POST", "/v0/tasks/{task_id}/workspace"): "TaskWorkspaceCreateResponse",
    ("GET", "/v0/task-workspaces"): "TaskWorkspaceListResponse",
    ("GET", "/v0/task-workspaces/{task_workspace_id}"): "TaskWorkspaceDetailResponse",
    ("GET", "/v0/tasks/{task_id}/steps"): "TaskStepListResponse",
    ("GET", "/v0/task-steps/{task_step_id}"): "TaskStepDetailResponse",
    ("POST", "/v0/task-workspaces/{task_workspace_id}/artifacts"): "TaskArtifactCreateResponse",
    ("GET", "/v0/task-artifacts"): "TaskArtifactListResponse",
    ("GET", "/v0/task-artifacts/{task_artifact_id}"): "TaskArtifactDetailResponse",
    ("POST", "/v0/task-artifacts/{task_artifact_id}/ingest"): "TaskArtifactIngestionResponse",
    ("GET", "/v0/task-artifacts/{task_artifact_id}/chunks"): "TaskArtifactChunkListResponse",
    ("POST", "/v0/tasks/{task_id}/artifact-chunks/retrieve"): "TaskArtifactChunkRetrievalResponse",
    ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/retrieve"): "TaskArtifactChunkRetrievalResponse",
    ("POST", "/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval"): ("TaskArtifactChunkSemanticRetrievalResponse"),
    ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval"): (
        "TaskArtifactChunkSemanticRetrievalResponse"
    ),
    ("POST", "/v0/tasks/{task_id}/steps"): "TaskStepNextCreateResponse",
    ("POST", "/v0/task-steps/{task_step_id}/transition"): "TaskStepTransitionResponse",
    ("POST", "/v0/execution-budgets"): "ExecutionBudgetCreateResponse",
    ("GET", "/v0/execution-budgets"): "ExecutionBudgetListResponse",
    ("GET", "/v0/execution-budgets/{execution_budget_id}"): "ExecutionBudgetDetailResponse",
    ("POST", "/v0/execution-budgets/{execution_budget_id}/deactivate"): "ExecutionBudgetDeactivateResponse",
    ("POST", "/v0/execution-budgets/{execution_budget_id}/supersede"): "ExecutionBudgetSupersedeResponse",
    ("GET", "/v0/tool-executions"): "ToolExecutionListResponse",
    ("GET", "/v0/tool-executions/{execution_id}"): "ToolExecutionDetailResponse",
    ("GET", "/v0/tools/{tool_id}"): "ToolDetailResponse",
    ("POST", "/v0/memories/extract-explicit-preferences"): "ExplicitPreferenceExtractionResponse",
    ("POST", "/v0/open-loops/extract-explicit-commitments"): "ExplicitCommitmentExtractionResponse",
    ("POST", "/v0/memories/capture-explicit-signals"): "ExplicitSignalCaptureResponse",
    ("POST", "/v0/continuity/captures"): "ContinuityCaptureCreateResponse",
    ("POST", "/v0/continuity/captures/candidates"): "ContinuityCaptureCandidatesResponse",
    ("POST", "/v0/continuity/captures/commit"): "ContinuityCaptureCommitResponse",
    ("POST", "/v1/memory/operations/candidates/generate"): "MemoryOperationCandidateGenerateResponse",
    ("GET", "/v1/memory/operations/candidates"): "MemoryOperationCandidateListResponse",
    ("POST", "/v1/memory/operations/commit"): "MemoryOperationCommitResponse",
    ("GET", "/v1/memory/operations"): "MemoryOperationListResponse",
    ("GET", "/v0/continuity/captures"): "ContinuityCaptureInboxResponse",
    ("GET", "/v0/continuity/captures/{capture_event_id}"): "ContinuityCaptureDetailResponse",
    ("POST", "/v0/continuity/review-queue/{continuity_object_id}/corrections"): ("ContinuityCorrectionApplyResponse"),
    ("GET", "/v0/task-briefs/{task_brief_id}"): "TaskBriefResponse",
    ("GET", "/v0/memories"): "MemoryReviewListResponse",
    ("GET", "/v0/memories/review-queue"): "MemoryReviewQueueResponse",
    ("GET", "/v0/memories/quality-gate"): "MemoryQualityGateResponse",
    ("GET", "/v0/memories/evaluation-summary"): "MemoryEvaluationSummaryResponse",
    ("POST", "/v0/memories/semantic-retrieval"): "SemanticMemoryRetrievalResponse",
    ("GET", "/v0/memories/{memory_id}"): "MemoryReviewDetailResponse",
    ("GET", "/v0/memories/{memory_id}/revisions"): "MemoryRevisionReviewListResponse",
    ("POST", "/v0/memories/{memory_id}/labels"): "MemoryReviewLabelCreateResponse",
    ("GET", "/v0/memories/{memory_id}/labels"): "MemoryReviewLabelListResponse",
    ("POST", "/v0/embedding-configs"): "EmbeddingConfigCreateResponse",
    ("GET", "/v0/embedding-configs"): "EmbeddingConfigListResponse",
    ("POST", "/v0/memory-embeddings"): "MemoryEmbeddingUpsertResponse",
    ("POST", "/v0/task-artifact-chunk-embeddings"): "TaskArtifactChunkEmbeddingWriteResponse",
    ("GET", "/v0/memories/{memory_id}/embeddings"): "MemoryEmbeddingListResponse",
    ("GET", "/v0/task-artifacts/{task_artifact_id}/chunk-embeddings"): ("TaskArtifactChunkEmbeddingListResponse"),
    ("GET", "/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings"): ("TaskArtifactChunkEmbeddingListResponse"),
    ("GET", "/v0/memory-embeddings/{memory_embedding_id}"): "MemoryEmbeddingDetailResponse",
    ("GET", "/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}"): (
        "TaskArtifactChunkEmbeddingDetailResponse"
    ),
    ("POST", "/v0/entities"): "EntityCreateResponse",
    ("POST", "/v0/entity-edges"): "EntityEdgeCreateResponse",
    ("GET", "/v0/entities"): "EntityListResponse",
    ("GET", "/v0/entities/{entity_id}/edges"): "EntityEdgeListResponse",
    ("GET", "/v0/entities/{entity_id}"): "EntityDetailResponse",
}

_OPENAPI_OTHER_AUTHORITATIVE_RESPONSE_TYPES: dict[tuple[str, str], tuple[str, str]] = {}

for _operation_key, _type_name in _OPENAPI_CONTRACT_RESPONSE_TYPES.items():
    _component_name, _previous_schema = OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key]
    OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key] = (
        _component_name,
        _schema_from_authoritative_response_type(
            module_name="alicebot_api.contracts",
            type_name=_type_name,
            title=_component_name,
        ),
    )

for _operation_key, (_module_name, _type_name) in _OPENAPI_OTHER_AUTHORITATIVE_RESPONSE_TYPES.items():
    _component_name, _previous_schema = OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key]
    OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key] = (
        _component_name,
        _schema_from_authoritative_response_type(
            module_name=_module_name,
            type_name=_type_name,
            title=_component_name,
        ),
    )


_SOURCE_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "source_type",
    "title",
    "author",
    "uri",
    "raw_path",
    "content_hash",
    "dedupe_key",
    "captured_at",
    "source_created_at",
    "source_modified_at",
    "connector_name",
    "external_id",
    "domain",
    "sensitivity",
    "metadata_json",
    "deleted_at",
)
_TASK_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "title",
    "task_type",
    "instructions",
    "status",
    "requested_by",
    "scope_json",
    "allowed_sources_json",
    "domain",
    "sensitivity",
    "write_policy",
    "scheduled_for",
    "started_at",
    "completed_at",
    "failed_at",
    "error_message",
    "output_artifact_id",
    "created_at",
    "updated_at",
    "metadata_json",
)
_GRAPH_EDGE_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "from_type",
    "from_id",
    "to_type",
    "to_id",
    "edge_type",
    "confidence",
    "explanation",
    "created_by",
    "created_at",
    "observed_at",
    "valid_from",
    "valid_to",
    "metadata_json",
)
_BELIEF_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "memory_id",
    "claim",
    "status",
    "confidence",
    "first_seen_at",
    "last_reinforced_at",
    "last_challenged_at",
    "superseded_by",
    "metadata_json",
)
_OPEN_LOOP_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "memory_id",
    "title",
    "status",
    "opened_at",
    "due_at",
    "resolved_at",
    "resolution_note",
    "created_at",
    "updated_at",
    "description",
    "priority",
    "project_id",
    "person_id",
    "source_id",
    "closed_at",
    "domain",
    "sensitivity",
    "metadata_json",
)
_QUALITY_RATING_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "artifact_id",
    "reviewer_id",
    "usefulness",
    "accuracy",
    "source_grounding",
    "novel_connections",
    "actionability",
    "hallucination_risk",
    "verbosity",
    "missed_context",
    "comments",
    "created_at",
    "metadata_json",
)
_EVENT_RESPONSE_FIELDS = (
    "id",
    "user_id",
    "event_type",
    "actor_type",
    "actor_id",
    "target_type",
    "target_id",
    "occurred_at",
    "payload_json",
    "trace_id",
    "run_id",
    "integrity_hash",
)
_CONNECTOR_SYNC_RESPONSE_FIELDS = (
    "status",
    "connector_name",
    "item_count",
    "imported_count",
    "duplicate_count",
    "skipped_count",
    "failed_count",
    "previous_cursor",
    "sync_cursor",
    "error_code",
    "source_ids",
    "failed_external_ids",
    "errors",
)
_CONNECTOR_CONFIG_RESPONSE_FIELDS = (
    "connector_id",
    "connector_name",
    "enabled",
    "configured",
    "secret_ref",
    "secret_configured",
    "default_domain",
    "default_sensitivity",
    "sync_mode",
    "poll_interval_seconds",
    "config_json",
    "validation_errors",
    "created_at",
    "updated_at",
    "last_configured_at",
)

_OPENAPI_SOURCE_AUDITED_RESPONSES: dict[
    tuple[str, str],
    tuple[tuple[str, ...], tuple[str, ...] | None],
] = {
    ("GET", "/v0/vnext/workspace"): (
        (
            "mode",
            "summary",
            "sources",
            "review_memories",
            "samples",
            "artifacts",
            "quality_evals",
            "connector_health",
            "dogfooding",
            "doctor",
            "traceability",
            "projects",
            "project_dashboards",
            "open_loops",
            "people",
            "beliefs",
            "tasks",
            "recent_events",
            "agent_activity",
            "policy_telemetry",
            "scheduler",
            "brain_charter",
        ),
        None,
    ),
    ("POST", "/v0/vnext/sources"): (
        ("status", "source_id", "content_hash", "chunk_count", "candidate_memory_count", "duplicate", "errors"),
        None,
    ),
    ("GET", "/v0/vnext/connectors/health"): (("items", "count", "order"), None),
    ("PATCH", "/v0/vnext/connectors/{connector_name}/config"): (_CONNECTOR_CONFIG_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/connectors/{connector_name}/sync"): (_CONNECTOR_SYNC_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/connectors/telegram/sync"): (_CONNECTOR_SYNC_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/connectors/local-folder/sync"): (_CONNECTOR_SYNC_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"): (
        ("status", "capability", "origin", "expires_at", "one_time"),
        None,
    ),
    ("POST", "/v0/vnext/connectors/browser-clipper/capture"): (_CONNECTOR_SYNC_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/agents/ingest-output"): (
        ("status", "source_id", "artifact_id", "memory_id", "policy_decision"),
        None,
    ),
    ("GET", "/v0/vnext/dogfooding"): (
        (
            "sample_scope",
            "captures_by_connector",
            "captures_today",
            "captures_this_week",
            "capture_trend_by_day",
            "capture_trend_by_week",
            "candidate_memories_created",
            "memory_status_counts",
            "candidate_memory_review_rate",
            "generated_artifacts_created",
            "artifact_status_counts",
            "artifact_quality_average",
            "artifact_quality_rating_count",
            "artifact_rating_trend",
            "daily_brief_review_status",
            "weekly_synthesis_review_status",
            "connections_surfaced",
            "contradictions_surfaced",
            "open_loop_status_counts",
            "open_loops_created",
            "open_loops_closed",
            "agent_context_packs_requested",
            "agent_memory_proposals",
            "policy_blocks_filters",
            "connector_failures",
            "top_failure_causes",
            "scheduler_freshness",
            "agent_activity_summary",
            "policy_block_filter_summary",
            "last_successful_scheduler_run",
            "connector_health",
            "dogfood_readiness",
            "insight_feedback",
        ),
        None,
    ),
    ("GET", "/v0/vnext/doctor"): (
        (
            "status",
            "fix_safe_applied",
            "ci_mode",
            "blocking_failure_count",
            "warning_count",
            "checks",
            "recommended_fixes",
            "migration_status",
            "connector_health",
        ),
        None,
    ),
    ("POST", "/v0/vnext/doctor/run"): (
        (
            "status",
            "fix_safe_applied",
            "ci_mode",
            "blocking_failure_count",
            "warning_count",
            "checks",
            "recommended_fixes",
            "migration_status",
            "connector_health",
        ),
        None,
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"): (_EVENT_RESPONSE_FIELDS, None),
    ("GET", "/v0/vnext/sources/{source_id}"): (_SOURCE_RESPONSE_FIELDS, None),
    ("GET", "/v0/vnext/traces/sources/{source_id}"): (
        (
            "trace_id",
            "trace_kind",
            "source",
            "chunks",
            "candidate_memories",
            "artifacts",
            "open_loops",
            "events",
            "sampling",
            "summary",
        ),
        None,
    ),
    ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"): (
        ("trace_id", "trace_kind", "artifact", "sources", "quality_evals", "events", "summary"),
        None,
    ),
    ("DELETE", "/v0/vnext/sources/{source_id}"): (_SOURCE_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/context-packs"): (
        (
            "context_pack_id",
            "query_interpretation",
            "entities",
            "current_known_state",
            "relevant_memories",
            "relevant_beliefs",
            "decisions",
            "procedures",
            "open_loops",
            "supporting_evidence",
            "contradicting_evidence",
            "recent_changes",
            "supersession_context",
            "missing_information",
            "sources",
            "warnings",
            "budget",
            "context_depth",
            "trace_id",
            "trace",
            "agent_identity",
            "policy_decision",
            "grounding",
            "derived_values",
        ),
        (
            "context_pack_id",
            "query_interpretation",
            "current_known_state",
            "relevant_memories",
            "open_loops",
            "supporting_evidence",
            "contradicting_evidence",
            "missing_information",
            "sources",
            "warnings",
            "budget",
            "context_depth",
            "trace_id",
            "trace",
            "agent_identity",
            "policy_decision",
        ),
    ),
    ("GET", "/v0/vnext/context-tree"): (
        (
            "schema_version",
            "generated_at",
            "trace_id",
            "query",
            "read_only",
            "summary",
            "roots",
            "agent_identity",
            "policy_decision",
        ),
        None,
    ),
    ("POST", "/v0/vnext/memories/commit"): (
        (
            "status",
            "write_mode",
            "reason",
            "reasons",
            "memory",
            "idempotent_replay",
            "confirmation_id",
            "confirmation",
            "proposal_id",
            "policy_decision",
        ),
        ("status", "write_mode", "policy_decision"),
    ),
    ("POST", "/v0/vnext/memories/confirm"): (
        ("status", "write_mode", "confirmation_id", "reason", "memory"),
        ("status", "write_mode", "confirmation_id", "memory"),
    ),
    ("POST", "/v0/vnext/memories/undo"): (("status", "write_mode", "memory"), None),
    ("POST", "/v0/vnext/memories/correct"): (("status", "write_mode", "memory"), None),
    ("POST", "/v0/vnext/memories/forget"): (("status", "write_mode", "memory"), None),
    ("POST", "/v0/vnext/memories/expire"): (("status", "memory", "valid_to", "policy_decision"), None),
    ("POST", "/v0/vnext/memories/unexpire"): (
        ("status", "memory", "policy_decision", "idempotent_replay", "note"),
        ("status", "memory", "policy_decision", "idempotent_replay"),
    ),
    ("POST", "/v0/vnext/memories/accept-consolidation"): (
        (
            "status",
            "memory",
            "proposal_kind",
            "superseded_member_ids",
            "skipped_members",
            "supersedes",
            "policy_decision",
            "idempotent_replay",
            "note",
        ),
        (
            "status",
            "memory",
            "proposal_kind",
            "superseded_member_ids",
            "skipped_members",
            "supersedes",
            "policy_decision",
            "idempotent_replay",
        ),
    ),
    ("POST", "/v0/vnext/memories/redact"): (
        (
            "status",
            "memory",
            "forgotten_first",
            "redacted_revisions",
            "redacted_events",
            "redacted_artifacts",
            "redacted_artifact_ids",
            "redacted_quality_ratings",
            "redacted_provenance_links",
            "idempotent_replay",
            "redaction_marker",
            "reason",
        ),
        None,
    ),
    ("GET", "/v0/vnext/memories/recent-commits"): (("recent_commits", "count"), None),
    ("GET", "/v0/vnext/memories/{memory_id}/audit"): (
        ("memory", "supersession_chain", "revisions", "events", "provenance_links"),
        None,
    ),
    ("POST", "/v0/vnext/queue/tasks"): (_TASK_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/queue/process-next"): (
        ("status", "task_id", "artifact_id", "error_code", "error_message"),
        None,
    ),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"): (_QUALITY_RATING_RESPONSE_FIELDS, None),
    ("POST", "/v0/vnext/graph/edges/{edge_id}/review"): (_GRAPH_EDGE_RESPONSE_FIELDS, None),
    ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): (
        ("target_id", "from_edges", "to_edges", "edge_count"),
        None,
    ),
    ("POST", "/v0/vnext/beliefs/{belief_id}/review"): (_BELIEF_RESPONSE_FIELDS, None),
    ("GET", "/v0/vnext/beliefs/{belief_id}/state"): (
        ("belief_id", "current", "history", "previous_statuses"),
        None,
    ),
    ("GET", "/v0/vnext/projects/{project_id}/dashboard"): (
        ("project", "state", "memories", "open_loops", "artifacts", "counts"),
        None,
    ),
    ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"): (
        ("run", "artifact", "policy_decision"),
        None,
    ),
    ("POST", "/v0/vnext/scheduler/run-due"): (
        ("checked_at", "due_count", "failed_count", "reaped_count", "runs", "policy_decision"),
        None,
    ),
    ("POST", "/v0/vnext/scheduler/pause"): (
        ("workflows", "paused_count", "policy_decision"),
        None,
    ),
    ("POST", "/v0/vnext/scheduler/resume"): (
        ("workflows", "resumed_count", "policy_decision"),
        None,
    ),
    ("POST", "/v0/vnext/open-loops/{loop_id}/review"): (_OPEN_LOOP_RESPONSE_FIELDS, None),
}

_OPENAPI_ARTIFACT_ROW_OPERATIONS = {
    ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
    ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"),
    ("POST", "/v0/vnext/artifacts/generate/connections"),
    ("POST", "/v0/vnext/artifacts/generate/contradictions"),
    ("GET", "/v0/vnext/artifacts/{artifact_id}"),
    ("POST", "/v0/vnext/artifacts/{artifact_id}/review"),
    ("POST", "/v0/vnext/projects/update-candidates"),
    ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"),
}

for _operation_key in _OPENAPI_ARTIFACT_ROW_OPERATIONS:
    _component_name, _previous_schema = OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key]
    OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key] = (
        _component_name,
        _closed_source_schema(
            _component_name,
            _ARTIFACT_RESPONSE_FIELDS,
            properties=_artifact_response_properties(),
        ),
    )

for _operation_key, (_fields, _required) in _OPENAPI_SOURCE_AUDITED_RESPONSES.items():
    _component_name, _previous_schema = OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key]
    _source_properties = (
        _OPENAPI_EXPLICIT_PROPERTY_SCHEMAS[_operation_key]
        if _operation_key == ("POST", "/v0/vnext/connectors/browser-clipper/capabilities")
        else None
    )
    OPENAPI_OPERATION_RESPONSE_SCHEMAS[_operation_key] = (
        _component_name,
        _closed_source_schema(
            _component_name,
            _fields,
            required=_required,
            properties=_source_properties,
        ),
    )


_OPENAPI_POLYMORPHIC_VARIANTS = [
    {
        "type": "object",
        "required": ["assistant", "metadata", "trace"],
        "additionalProperties": False,
        "properties": {
            "assistant": {"type": "object", "additionalProperties": True},
            "metadata": {"type": "object", "additionalProperties": True},
            "trace": {"type": "object", "additionalProperties": True},
        },
    },
    {
        "type": "object",
        "required": ["detail", "response_job"],
        "additionalProperties": False,
        "properties": {
            "detail": {"type": "object", "additionalProperties": True},
            "response_job": {"type": "object", "additionalProperties": True},
        },
    },
]
OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v1/runtime/invoke")][1]["oneOf"] = _OPENAPI_POLYMORPHIC_VARIANTS


# Phase 1 removes these public operations permanently.  Keep the inventory exact
# and fail closed if a deleted operation is accidentally reintroduced into the
# exported response registry. Historical migrations remain intentionally inert.
PERMANENTLY_REMOVED_OPENAPI_OPERATIONS = frozenset(
    {
        ("POST", "/v0/responses"),
        ("GET", "/v0/chief-of-staff"),
        ("POST", "/v0/chief-of-staff/recommendation-outcomes"),
        ("POST", "/v0/chief-of-staff/handoff-review-actions"),
        ("POST", "/v0/chief-of-staff/execution-routing-actions"),
        ("POST", "/v0/chief-of-staff/handoff-outcomes"),
        ("GET", "/v1/model-packs"),
        ("POST", "/v1/model-packs"),
        ("GET", "/v1/model-packs/{pack_id}"),
        ("POST", "/v1/model-packs/{pack_id}/bind"),
        ("GET", "/v1/workspaces/{workspace_id}/model-pack-binding"),
        ("POST", "/v1/auth/magic-link/start"),
        ("POST", "/v1/auth/magic-link/verify"),
        ("POST", "/v1/auth/logout"),
        ("GET", "/v1/auth/session"),
        ("POST", "/v1/workspaces"),
        ("GET", "/v1/workspaces/current"),
        ("POST", "/v1/devices/link/start"),
        ("POST", "/v1/devices/link/confirm"),
        ("GET", "/v1/devices"),
        ("DELETE", "/v1/devices/{device_id}"),
        ("GET", "/v1/preferences"),
        ("PATCH", "/v1/preferences"),
        ("GET", "/v1/admin/hosted/overview"),
        ("GET", "/v1/admin/hosted/design-partners/dashboard"),
        ("GET", "/v1/admin/hosted/design-partners"),
        ("POST", "/v1/admin/hosted/design-partners"),
        ("GET", "/v1/admin/hosted/design-partners/{design_partner_id}"),
        ("PATCH", "/v1/admin/hosted/design-partners/{design_partner_id}"),
        ("POST", "/v1/admin/hosted/design-partners/{design_partner_id}/workspaces"),
        ("POST", "/v1/admin/hosted/design-partners/{design_partner_id}/feedback"),
        ("GET", "/v1/admin/hosted/workspaces"),
        ("GET", "/v1/admin/hosted/delivery-receipts"),
        ("GET", "/v1/admin/hosted/incidents"),
        ("GET", "/v1/admin/hosted/rollout-flags"),
        ("PATCH", "/v1/admin/hosted/rollout-flags"),
        ("GET", "/v1/admin/hosted/analytics"),
        ("GET", "/v1/admin/hosted/rate-limits"),
        ("POST", "/v1/channels/telegram/link/start"),
        ("POST", "/v1/channels/telegram/link/confirm"),
        ("POST", "/v1/channels/telegram/unlink"),
        ("GET", "/v1/channels/telegram/status"),
        ("POST", "/v1/channels/telegram/webhook"),
        ("GET", "/v1/channels/telegram/messages"),
        ("GET", "/v1/channels/telegram/threads"),
        ("POST", "/v1/channels/telegram/messages/{message_id}/dispatch"),
        ("GET", "/v1/channels/telegram/delivery-receipts"),
        ("GET", "/v1/channels/telegram/notification-preferences"),
        ("PATCH", "/v1/channels/telegram/notification-preferences"),
        ("GET", "/v1/channels/telegram/daily-brief"),
        ("POST", "/v1/channels/telegram/daily-brief/deliver"),
        ("GET", "/v1/channels/telegram/open-loop-prompts"),
        ("POST", "/v1/channels/telegram/open-loop-prompts/{prompt_id}/deliver"),
        ("GET", "/v1/channels/telegram/scheduler/jobs"),
        ("POST", "/v1/channels/telegram/messages/{message_id}/handle"),
        ("GET", "/v1/channels/telegram/messages/{message_id}/result"),
        ("GET", "/v1/channels/telegram/recall"),
        ("GET", "/v1/channels/telegram/resume"),
        ("GET", "/v1/channels/telegram/open-loops"),
        ("POST", "/v1/channels/telegram/open-loops/{open_loop_id}/review-action"),
        ("GET", "/v1/channels/telegram/approvals"),
        ("POST", "/v1/channels/telegram/approvals/{approval_id}/approve"),
        ("POST", "/v1/channels/telegram/approvals/{approval_id}/reject"),
    }
)
if len(PERMANENTLY_REMOVED_OPENAPI_OPERATIONS) != 63:
    raise RuntimeError("permanently removed OpenAPI inventory must contain exactly 63 operations")
for _operation_key in PERMANENTLY_REMOVED_OPENAPI_OPERATIONS:
    OPENAPI_OPERATION_RESPONSE_SCHEMAS.pop(_operation_key, None)
    _OPENAPI_EXPLICIT_PROPERTY_SCHEMAS.pop(_operation_key, None)


_closed_operations = {
    operation_key
    for operation_key, (_component_name, schema) in OPENAPI_OPERATION_RESPONSE_SCHEMAS.items()
    if schema.get("additionalProperties") is False
}
_untyped_closed_operations = _closed_operations - set(_OPENAPI_EXPLICIT_PROPERTY_SCHEMAS)
if _untyped_closed_operations:
    raise RuntimeError(
        f"OpenAPI closed operations require explicit property types; missing={sorted(_untyped_closed_operations)}"
    )


OPENAPI_INTENTIONALLY_POLYMORPHIC_OPERATIONS: dict[tuple[str, str], str] = {
    ("POST", "/v1/runtime/invoke"): (
        "Returns a completed invocation for terminal work or an accepted in-progress job."
    ),
}


# These classifications are derived from the literal registry above and are
# exported so OpenAPI generation/tests can fail closed when a route changes
# verification state. Closed schemas are backed by statically visible success
# payloads; open schemas remain deliberately permissive until their helper or
# variable-backed envelopes have equivalent source/test evidence.
OPENAPI_SOURCE_VERIFIED_OPERATIONS = frozenset(
    operation_key
    for operation_key, (_component_name, schema) in OPENAPI_OPERATION_RESPONSE_SCHEMAS.items()
    if schema.get("additionalProperties") is False
)
OPENAPI_OPEN_RESPONSE_OPERATIONS = frozenset(OPENAPI_OPERATION_RESPONSE_SCHEMAS) - (OPENAPI_SOURCE_VERIFIED_OPERATIONS)
