"""Mechanical MCP projects carrier."""

from __future__ import annotations

from collections.abc import Mapping
from alicebot_api.project_view import ProjectView
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import (
    PolicyDecision,
    agent_metadata,
)
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_projects import (
    ProjectAutomationRequest,
    VNextProjectService,
)
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject

from .evidence_artifacts import _authorize_vnext_artifact_target
from .retrieval_shared import _resource_matches_project_scope
from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _agent_identity_from_arguments,
    _json_object,
    _mcp_agent_policy_preflight,
    _parse_int,
    _parse_model_generation_kwargs,
    _parse_optional_text,
    _parse_required_text,
    _parse_string_list,
    _persist_vnext_deferred_embedding_inputs,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _vnext_store_context,
)


def _project_request_from_arguments(arguments: Mapping[str, object]) -> ProjectAutomationRequest:
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    return ProjectAutomationRequest(
        domains=_parse_string_list(arguments, "domains"),
        sensitivity_allowed=sensitivity_allowed,
        project_id=_parse_optional_text(arguments, "project_id"),
        person_id=_parse_optional_text(arguments, "person_id"),
        max_items=_parse_int(arguments, key="max_items", default=8, minimum=1, maximum=50),
        **_parse_model_generation_kwargs(arguments),
    )


def _handle_alice_project_update_candidate(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    request = _project_request_from_arguments(arguments)
    identity = _agent_identity_from_arguments(context, arguments)
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="artifact.generate",
            domains=request.domains,
            sensitivity_allowed=request.sensitivity_allowed,
            project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
            workflow_type="project_update_scan",
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            payload = VNextProjectService(store).generate_project_update_candidate(
                ProjectAutomationRequest(
                    domains=decision.effective_domains,
                    sensitivity_allowed=decision.effective_sensitivity_allowed,
                    project_id=request.project_id,
                    person_id=request.person_id,
                    max_items=request.max_items,
                    generated_by=actor_type,
                    actor_id=actor_id,
                    trace_id=_parse_optional_text(arguments, "trace_id") or decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                    metadata_json=agent_metadata(identity, decision),
                    generation_mode=request.generation_mode,
                    model_route_mode=request.model_route_mode,
                    model_provider=request.model_provider,
                    model=request.model,
                    model_temperature=request.model_temperature,
                    allow_cloud_private=request.allow_cloud_private,
                )
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext project update candidate generation did not complete")
    return _json_object(payload)


def _handle_alice_project_update_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    artifact_id = _parse_required_text(arguments, "artifact_id")
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()
    actor_type = "system"
    actor_id: str | None = None
    trace_id: str | None = None
    with _vnext_store_context(context) as store:
        _target, actor_type, actor_id, decision = _authorize_vnext_artifact_target(
            store,
            identity=identity,
            artifact_id=artifact_id,
            action="artifact.review",
            for_update=True,
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            if identity is None:
                actor_type = "user"
                actor_id = str(context.user_id)
            trace_id = _parse_optional_text(arguments, "trace_id") or decision.trace_id
            service = VNextProjectService(store, defer_embeddings=True)
            payload = service.review_project_update(
                artifact_id=artifact_id,
                action=_parse_required_text(arguments, "action"),
                edited_current_state=_parse_optional_text(arguments, "edited_current_state"),
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
            )
            deferred_embedding_inputs = service.deferred_embedding_inputs
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext project update review did not complete")
    _persist_vnext_deferred_embedding_inputs(
        context,
        deferred_embedding_inputs,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )
    return _json_object(payload)


def _handle_alice_project_dashboard(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="project.dashboard",
        sensitivity_allowed=sensitivity_allowed,
        project_scope=_parse_string_list(arguments, "project_scope"),
        project_view=ProjectView.unscoped(),
    )
    with _vnext_store_context(context) as store:
        return _json_object(
            VNextProjectService(store).project_dashboard(
                project_id=_parse_required_text(arguments, "project_id"),
                sensitivity_allowed=decision.effective_sensitivity_allowed,
            )
        )


def _handle_alice_open_loop_extract(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        loops = VNextProjectService(store).extract_open_loops(_project_request_from_arguments(arguments))
    return _json_object({"open_loops": loops, "created_count": len(loops)})


def _handle_alice_open_loop_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        return _json_object(
            VNextProjectService(store).review_open_loop(
                loop_id=_parse_required_text(arguments, "loop_id"),
                action=_parse_required_text(arguments, "action"),
                title=_parse_optional_text(arguments, "title"),
                description=_parse_optional_text(arguments, "description"),
                due_at=_parse_optional_text(arguments, "due_at"),
                priority=_parse_optional_text(arguments, "priority"),
                resolution_note=_parse_optional_text(arguments, "resolution_note"),
            )
        )


def _handle_alice_vnext_open_loops(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    status = _parse_optional_text(arguments, "status") or "open"
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="open_loop.lookup",
        domains=_parse_string_list(arguments, "domains"),
        sensitivity_allowed=sensitivity_allowed,
        project_scope=_parse_string_list(arguments, "project_scope"),
        project_view=ProjectView.unscoped(),
    )
    limit = _parse_int(arguments, key="limit", default=20, minimum=1, maximum=100)
    with _vnext_store_context(context) as store:
        loops = store.list_open_loops(
            status=status if status != "all" else None,
            domains=list(decision.effective_domains) or None,
            sensitivity_allowed=list(decision.effective_sensitivity_allowed),
            scope_projects=decision.effective_project_scope,
            limit=limit,
        )
        if decision.effective_project_scope:
            loops = [loop for loop in loops if _resource_matches_project_scope(loop, decision.effective_project_scope)]
        loops = loops[:limit]
    return _json_object({"items": loops, "count": len(loops)})
