"""Mechanical MCP synthesis carrier."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from alicebot_api.project_view import ProjectView
from alicebot_api.store import JsonObject
from alicebot_api.vnext_brain import (
    BrainArtifactRequest,
    VNextBrainService,
)
from alicebot_api.vnext_connections import (
    ConnectionFinderRequest,
    VNextConnectionService,
)
from alicebot_api.vnext_contradictions import (
    ContradictionFinderRequest,
    VNextContradictionService,
)
from alicebot_api.vnext_label_guard import readable_artifact
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject

from .shared import (
    MCPRuntimeContext,
    _agent_identity_from_arguments,
    _json_object,
    _mcp_agent_policy_preflight,
    _parse_bool,
    _parse_int,
    _canonical_row_id_argument,
    _parse_model_generation_kwargs,
    _parse_optional_float,
    _parse_optional_text,
    _parse_required_text,
    _parse_string_list,
    _vnext_store_context,
)
from .types import MCPReferenceNotFoundError


def _brain_artifact_request_from_arguments(arguments: Mapping[str, object]) -> BrainArtifactRequest:
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    return BrainArtifactRequest(agent_identity=None,
        domains=_parse_string_list(arguments, "domains"),
        projects=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        sensitivity_allowed=sensitivity_allowed,
        generated_for=_parse_optional_text(arguments, "generated_for"),
        source_limit=_parse_int(arguments, key="source_limit", default=8, minimum=1, maximum=50),
        memory_limit=_parse_int(arguments, key="memory_limit", default=8, minimum=1, maximum=50),
        open_loop_limit=_parse_int(arguments, key="open_loop_limit", default=8, minimum=1, maximum=50),
        artifact_limit=_parse_int(arguments, key="artifact_limit", default=4, minimum=1, maximum=50),
        discover_open_loops=_parse_bool(arguments, key="discover_open_loops", default=True),
        create_candidate_memories=_parse_bool(arguments, key="create_candidate_memories", default=True),
        **_parse_model_generation_kwargs(arguments),
    )


def _generated_artifact_answer(
    context: MCPRuntimeContext, arguments: Mapping[str, object], make: Callable[[Any], VNextJsonObject]
) -> JsonObject:
    """Run ``make(store)`` and answer with its report only when the caller may read that report now.

    The report is stored whoever asked for it and the owner reads it. A report this caller may not read (it was made from
    a row above the caller's limits, or it is one the run found again that the owner raised) is answered as a missing
    artifact is, and nothing about it is echoed.
    """

    identity = _agent_identity_from_arguments(context, arguments)
    with _vnext_store_context(context) as store:
        artifact = readable_artifact(store, identity, make(store))
    if artifact is None:
        raise MCPReferenceNotFoundError("the artifact was not found")
    return _json_object(artifact)


def _handle_alice_generate_daily_brief(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    request = _authorized_brain_request(context, arguments)
    return _generated_artifact_answer(
        context, arguments, lambda store: VNextBrainService(store).generate_daily_brief(request)
    )


def _handle_alice_generate_weekly_synthesis(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    request = _authorized_brain_request(context, arguments)
    return _generated_artifact_answer(
        context, arguments, lambda store: VNextBrainService(store).generate_weekly_synthesis(request)
    )


def _authorized_brain_request(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> BrainArtifactRequest:
    request = _brain_artifact_request_from_arguments(arguments)
    decision = _mcp_agent_policy_preflight(
        context, arguments, action="artifact.generate", domains=request.domains,
        sensitivity_allowed=request.sensitivity_allowed, project_scope=request.projects,
        project_view=ProjectView.unscoped(),
    )
    identity = _agent_identity_from_arguments(context, arguments)
    return replace(request, agent_identity=identity.to_record() if identity is not None else None,
                   domains=decision.effective_domains, projects=decision.effective_project_scope,
                   sensitivity_allowed=decision.effective_sensitivity_allowed)


def _connection_request_from_arguments(arguments: Mapping[str, object]) -> ConnectionFinderRequest:
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    auto_accept_threshold = _parse_optional_float(arguments, "auto_accept_threshold")
    return ConnectionFinderRequest(agent_identity=None,
        query=_parse_optional_text(arguments, "query") or "",
        domains=_parse_string_list(arguments, "domains"),
        projects=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        sensitivity_allowed=sensitivity_allowed,
        max_connections=_parse_int(arguments, key="max_connections", default=8, minimum=1, maximum=50),
        auto_accept_threshold=auto_accept_threshold,
        **_parse_model_generation_kwargs(arguments),
    )


def _handle_alice_generate_connections(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    request = _connection_request_from_arguments(arguments)
    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="artifact.generate",
        domains=request.domains,
        sensitivity_allowed=request.sensitivity_allowed,
        project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        project_view=ProjectView.unscoped(),
    )
    identity = _agent_identity_from_arguments(context, arguments)
    request = ConnectionFinderRequest(agent_identity=identity.to_record() if identity is not None else None,
        query=request.query,
        domains=decision.effective_domains,
        projects=decision.effective_project_scope,
        sensitivity_allowed=decision.effective_sensitivity_allowed,
        max_connections=request.max_connections,
        auto_accept_threshold=request.auto_accept_threshold,
        generation_mode=request.generation_mode,
        model_route_mode=request.model_route_mode,
        model_provider=request.model_provider,
        model=request.model,
        model_temperature=request.model_temperature,
        allow_cloud_private=request.allow_cloud_private,
    )
    return _generated_artifact_answer(
        context, arguments, lambda store: VNextConnectionService(store).generate_connection_report(request)
    )


def _handle_alice_graph_edge_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        # An edge that is missing, and an id that is not one in any accepted
        # spelling, answer not_found alike, before the write path runs: the
        # update of a row that is not there used to fail the whole call.
        edge_id = _canonical_row_id_argument(arguments, "edge_id", store=store, not_found=lambda _raw: MCPReferenceNotFoundError(f"edge {_raw} was not found"))
        lookup_edge = getattr(store, "get_edge", None)
        if lookup_edge is not None and lookup_edge(edge_id) is None:
            raise MCPReferenceNotFoundError(f"edge {edge_id} was not found")
        return _json_object(
            VNextConnectionService(store).review_edge(
                edge_id=edge_id,
                action=_parse_required_text(arguments, "action"),
            )
        )


def _handle_alice_graph_neighborhood(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        return _json_object(
            VNextConnectionService(store).graph_neighborhood(
                target_id=_parse_required_text(arguments, "target_id"),
            )
        )


def _contradiction_request_from_arguments(arguments: Mapping[str, object]) -> ContradictionFinderRequest:
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    return ContradictionFinderRequest(agent_identity=None,
        query=_parse_optional_text(arguments, "query") or "",
        domains=_parse_string_list(arguments, "domains"),
        projects=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        sensitivity_allowed=sensitivity_allowed,
        max_contradictions=_parse_int(arguments, key="max_contradictions", default=8, minimum=1, maximum=50),
        **_parse_model_generation_kwargs(arguments),
    )


def _handle_alice_generate_contradictions(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    request = _contradiction_request_from_arguments(arguments)
    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="artifact.generate",
        domains=request.domains,
        sensitivity_allowed=request.sensitivity_allowed,
        project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        project_view=ProjectView.unscoped(),
    )
    identity = _agent_identity_from_arguments(context, arguments)
    request = ContradictionFinderRequest(agent_identity=identity.to_record() if identity is not None else None,
        query=request.query,
        domains=decision.effective_domains,
        projects=decision.effective_project_scope,
        sensitivity_allowed=decision.effective_sensitivity_allowed,
        max_contradictions=request.max_contradictions,
        generation_mode=request.generation_mode,
        model_route_mode=request.model_route_mode,
        model_provider=request.model_provider,
        model=request.model,
        model_temperature=request.model_temperature,
        allow_cloud_private=request.allow_cloud_private,
    )
    return _generated_artifact_answer(
        context, arguments, lambda store: VNextContradictionService(store).generate_contradiction_report(request)
    )


def _handle_alice_belief_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        # A belief that is missing, and an id that is not one in any accepted
        # spelling, answer not_found alike, before the write path runs: the
        # review of a row that is not there used to fail the whole call.
        belief_id = _canonical_row_id_argument(arguments, "belief_id", store=store, not_found=lambda _raw: MCPReferenceNotFoundError(f"belief {_raw} was not found"))
        lookup_belief = getattr(store, "get_belief", None)
        if lookup_belief is not None and lookup_belief(belief_id) is None:
            raise MCPReferenceNotFoundError(f"belief {belief_id} was not found")
        return _json_object(
            VNextContradictionService(store).review_belief(
                belief_id=belief_id,
                action=_parse_required_text(arguments, "action"),
                confidence=_parse_optional_float(arguments, "confidence"),
                superseded_by=_parse_optional_text(arguments, "superseded_by"),
            )
        )


def _handle_alice_belief_state(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        return _json_object(
            VNextContradictionService(store).belief_state(
                belief_id=_canonical_row_id_argument(arguments, "belief_id", store=store, not_found=lambda _raw: MCPReferenceNotFoundError(f"belief {_raw} was not found")),
            )
        )
