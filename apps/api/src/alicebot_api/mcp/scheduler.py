"""Mechanical MCP scheduler carrier."""

from __future__ import annotations

from collections.abc import Mapping
from alicebot_api.project_view import ProjectView
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import PolicyDecision
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject
from alicebot_api.vnext_scheduler import (
    SchedulerRunRequest,
    VNextSchedulerService,
)
from alicebot_api.vnext_scheduler_runtime import (
    run_due_workflows_durable,
    run_now_durable,
)

from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _agent_identity_from_arguments,
    _is_sqlite_backend,
    _json_object,
    _parse_bool,
    _parse_int,
    _parse_model_generation_kwargs,
    _parse_optional_text,
    _parse_required_text,
    _parse_string_list,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _vnext_store_context,
)
from .types import MCPArgumentError, MCPPreconditionFailedError


def _handle_alice_vnext_scheduler_status(context: MCPRuntimeContext, _arguments: Mapping[str, object]) -> JsonObject:
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError("vNext scheduler tools require the Postgres backend")
    with _vnext_store_context(context) as store:
        return _json_object(VNextSchedulerService(store).status())


def _handle_alice_vnext_scheduler_run_now(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError("vNext scheduler tools require the Postgres backend")
    identity = _agent_identity_from_arguments(context, arguments)
    workflow_type = _parse_required_text(arguments, "workflow_type")
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    generation_kwargs = {
        **_parse_model_generation_kwargs(arguments),
        "source_limit": _parse_int(arguments, key="source_limit", default=12, minimum=1, maximum=100),
        "memory_limit": _parse_int(arguments, key="memory_limit", default=12, minimum=1, maximum=100),
        "artifact_limit": _parse_int(arguments, key="artifact_limit", default=8, minimum=1, maximum=100),
        "event_limit": _parse_int(arguments, key="event_limit", default=30, minimum=1, maximum=100),
        "rating_limit": _parse_int(arguments, key="rating_limit", default=20, minimum=1, maximum=100),
        "create_candidate_memories": _parse_bool(arguments, key="create_candidate_memories", default=True),
    }
    blocked_decision: PolicyDecision | None = None
    scheduler_request: SchedulerRunRequest | None = None
    with _vnext_store_context(context) as store:
        _actor_type, _actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="scheduler.run_now",
            domains=_parse_string_list(arguments, "domains"),
            sensitivity_allowed=sensitivity_allowed,
            project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
            workflow_type=workflow_type,
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            scheduler_request = SchedulerRunRequest(
                workflow_type=workflow_type,
                domains=decision.effective_domains,
                projects=decision.effective_project_scope,
                sensitivity_allowed=decision.effective_sensitivity_allowed,
                generated_for=_parse_optional_text(arguments, "generated_for"),
                triggered_by="agent" if identity is not None else "user",
                agent_identity=identity,
                policy_decision=decision,
                options=generation_kwargs,
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if scheduler_request is None:
        raise MCPToolError("vNext scheduler run-now did not complete")
    payload = run_now_durable(
        database_url=context.database_url,
        user_id=context.user_id,
        request=scheduler_request,
    )
    return _json_object(payload)


def _handle_alice_vnext_scheduler_run_due(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError("vNext scheduler tools require the Postgres backend")
    identity = _agent_identity_from_arguments(context, arguments)
    limit_value = arguments.get("limit", 10)
    if not isinstance(limit_value, int):
        raise MCPArgumentError("limit must be an integer")
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    actor_type = "scheduler"
    decision: PolicyDecision | None = None
    with _vnext_store_context(context) as store:
        actor_type, _actor_id, decision = _policy_checked(
            store, identity=identity, action="scheduler.run_due",
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if decision is not None:
        payload = run_due_workflows_durable(
            database_url=context.database_url,
            user_id=context.user_id,
            limit=limit_value,
            triggered_by=actor_type if identity is not None else "scheduler",
            agent_identity=identity,
            policy_decision=decision,
        )
    if payload is None:
        raise MCPToolError("vNext scheduler run-due did not complete")
    return _json_object(payload)


def _handle_alice_vnext_scheduler_pause(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError("vNext scheduler tools require the Postgres backend")
    identity = _agent_identity_from_arguments(context, arguments)
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, _actor_id, decision = _policy_checked(
            store, identity=identity, action="scheduler.pause",
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            payload = VNextSchedulerService(store).pause_all(actor_type=actor_type)
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext scheduler pause did not complete")
    return _json_object(payload)


def _handle_alice_vnext_scheduler_resume(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError("vNext scheduler tools require the Postgres backend")
    identity = _agent_identity_from_arguments(context, arguments)
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, _actor_id, decision = _policy_checked(
            store, identity=identity, action="scheduler.resume",
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            payload = VNextSchedulerService(store).resume_all(actor_type=actor_type)
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext scheduler resume did not complete")
    return _json_object(payload)
