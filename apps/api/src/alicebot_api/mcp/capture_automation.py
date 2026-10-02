"""Mechanical MCP capture automation carrier."""

from __future__ import annotations

from collections.abc import Mapping
from alicebot_api.project_view import ProjectView
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import (
    PolicyDecision,
    agent_metadata,
    append_policy_events,
)
from alicebot_api.vnext_brain import (
    BrainArtifactRequest,
    VNextBrainService,
)
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_connectors import VNextConnectorService
from alicebot_api.vnext_queue import (
    QueueTaskRequest,
    VNextQueueService,
)
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject

from .scheduler import _handle_alice_vnext_scheduler_run_now
from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _agent_identity_from_arguments,
    _json_object,
    _parse_bool,
    _parse_int,
    _parse_model_generation_kwargs,
    _parse_optional_text,
    _parse_required_document_text,
    _parse_required_text,
    _parse_string_list,
    _persist_vnext_deferred_embedding_inputs,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _vnext_store_context,
)
from .synthesis import (
    _handle_alice_generate_connections,
    _handle_alice_generate_contradictions,
)


def _handle_alice_vnext_capture(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    domain = _parse_optional_text(arguments, "domain") or "unknown"
    sensitivity = _parse_optional_text(arguments, "sensitivity") or "unknown"
    blocked_decision: PolicyDecision | None = None
    capture_result = None
    capture_actor_type = "user"
    capture_actor_id: str | None = None
    capture_trace_id: str | None = None
    with _vnext_store_context(context) as store:
        actor_type, actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="source.capture",
            domains=(domain,),
            sensitivity_allowed=(sensitivity,),
            project_scope=_parse_string_list(arguments, "project_scope"),
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            capture_actor_type = actor_type
            capture_actor_id = actor_id
            capture_trace_id = _parse_optional_text(arguments, "trace_id") or decision.trace_id
            capture_result = VNextCaptureService(
                store,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=capture_trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
                agent_identity=identity.to_record() if identity is not None else None,
                policy_decision=decision.to_record(),
                defer_embeddings=True,
            ).capture_text(
                _parse_required_document_text(arguments, "raw_text"),
                title=_parse_optional_text(arguments, "title"),
                domain=domain,
                sensitivity=sensitivity,
                # Thread the validated effective project scope into capture so
                # a project-scoped agent's memory is retrievable by that
                # project's filtered recall (audit P1 #4).
                project_scope=decision.effective_project_scope,
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if capture_result is None:
        raise MCPToolError("vNext source capture did not complete")
    _persist_vnext_deferred_embedding_inputs(
        context,
        capture_result.deferred_embedding_inputs,
        actor_type=capture_actor_type,
        actor_id=capture_actor_id,
        trace_id=capture_trace_id,
    )
    return _json_object(capture_result.to_record())


def _handle_alice_vnext_ingest_agent_output(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    if identity is None:
        raise MCPToolError("agent_id is required for alice_vnext_ingest_agent_output")
    domain = _parse_optional_text(arguments, "domain") or "project"
    sensitivity = _parse_optional_text(arguments, "sensitivity") or "private"
    blocked_decision: PolicyDecision | None = None
    ingest_result = None
    decision: PolicyDecision | None = None
    with _vnext_store_context(context) as store:
        _actor_type, _actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="source.capture",
            domains=(domain,),
            sensitivity_allowed=(sensitivity,),
            project_scope=_parse_string_list(arguments, "project_scope"),
            write_policy="proposal_only" if _parse_bool(arguments, key="propose_memory", default=False) else None,
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            ingest_result = VNextConnectorService(store, defer_embeddings=True).ingest_agent_output(
                {
                    "agent_id": identity.agent_id,
                    "agent_type": identity.agent_type,
                    "agent_run_id": identity.agent_run_id,
                    "task_id": identity.task_id,
                    "project_scope": list(decision.effective_project_scope),
                    "title": _parse_required_text(arguments, "title"),
                    "content": _parse_required_text(arguments, "content"),
                    "output_type": _parse_optional_text(arguments, "output_type") or "general",
                    "domain": domain,
                    "sensitivity": sensitivity,
                    "source_refs": list(_parse_string_list(arguments, "source_refs")),
                    "rationale": _parse_optional_text(arguments, "rationale"),
                    "propose_memory": _parse_bool(arguments, key="propose_memory", default=False),
                },
                policy_decision=decision.to_record(),
            )
            append_policy_events(
                store, identity=identity, decision=decision, target_type="connector", target_id="agent_output"
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if ingest_result is None or decision is None:
        raise MCPToolError("agent output ingestion did not complete")
    _persist_vnext_deferred_embedding_inputs(
        context,
        ingest_result.deferred_embedding_inputs,
        actor_type="agent",
        actor_id=identity.agent_id,
        trace_id=decision.trace_id,
    )
    return _json_object(ingest_result.to_record())


def _handle_alice_vnext_queue_task(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    domain = _parse_optional_text(arguments, "domain") or "unknown"
    sensitivity = _parse_optional_text(arguments, "sensitivity") or "unknown"
    write_policy = _parse_optional_text(arguments, "write_policy") or "proposal_only"
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="queue_task.create",
            domains=(domain,),
            sensitivity_allowed=(sensitivity,),
            project_scope=_parse_string_list(arguments, "project_scope"),
            write_policy=write_policy,
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            payload = VNextQueueService(store).enqueue_task(
                QueueTaskRequest(
                    title=_parse_required_text(arguments, "title"),
                    task_type=_parse_required_text(arguments, "task_type"),
                    instructions=_parse_required_text(arguments, "instructions"),
                    requested_by=identity.agent_id if identity is not None else "mcp",
                    domain=domain,
                    sensitivity=sensitivity,
                    write_policy=write_policy,
                    scope_json={"project_scope": list(_parse_string_list(arguments, "project_scope"))},
                    actor_type=actor_type,
                    actor_id=actor_id,
                    trace_id=_parse_optional_text(arguments, "trace_id") or decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                )
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext queue task did not complete")
    return _json_object(payload)


def _handle_alice_vnext_generate_artifact(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    workflow_type = _parse_optional_text(arguments, "workflow_type") or "daily_brief"
    if workflow_type in {"connection_report", "connections"}:
        return _handle_alice_generate_connections(context, arguments)
    if workflow_type in {"contradiction_report", "contradictions"}:
        return _handle_alice_generate_contradictions(context, arguments)
    if workflow_type in {"open_loop_review", "project_update_scan", "memory_consolidation"}:
        scheduler_arguments = dict(arguments)
        scheduler_arguments["workflow_type"] = workflow_type
        return _handle_alice_vnext_scheduler_run_now(context, scheduler_arguments)
    if workflow_type not in {"daily_brief", "weekly_synthesis"}:
        raise MCPToolError(
            "workflow_type must be daily_brief, weekly_synthesis, connection_report, "
            "contradiction_report, open_loop_review, project_update_scan, or memory_consolidation"
        )
    identity = _agent_identity_from_arguments(context, arguments)
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    generation_kwargs = _parse_model_generation_kwargs(arguments)
    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="artifact.generate",
            domains=_parse_string_list(arguments, "domains"),
            sensitivity_allowed=sensitivity_allowed,
            project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
            project_view=ProjectView.unscoped(),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            request = BrainArtifactRequest(
                domains=decision.effective_domains,
                projects=decision.effective_project_scope,
                sensitivity_allowed=decision.effective_sensitivity_allowed,
                generated_for=_parse_optional_text(arguments, "generated_for"),
                source_limit=_parse_int(arguments, key="source_limit", default=8, minimum=1, maximum=50),
                memory_limit=_parse_int(arguments, key="memory_limit", default=8, minimum=1, maximum=50),
                open_loop_limit=_parse_int(arguments, key="open_loop_limit", default=8, minimum=1, maximum=50),
                artifact_limit=_parse_int(arguments, key="artifact_limit", default=4, minimum=1, maximum=50),
                discover_open_loops=_parse_bool(arguments, key="discover_open_loops", default=True),
                create_candidate_memories=_parse_bool(arguments, key="create_candidate_memories", default=True),
                generated_by=actor_type,
                actor_id=actor_id,
                trace_id=_parse_optional_text(arguments, "trace_id") or decision.trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
                agent_identity=identity.to_record() if identity is not None else None,
                policy_decision=decision.to_record(),
                metadata_json=agent_metadata(identity, decision),
                **generation_kwargs,
            )
            service = VNextBrainService(store)
            payload = (
                service.generate_weekly_synthesis(request)
                if workflow_type == "weekly_synthesis"
                else service.generate_daily_brief(request)
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext artifact generation did not complete")
    return _json_object(payload)
