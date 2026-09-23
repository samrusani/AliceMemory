from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from alicebot_api.db import user_connection
from alicebot_api.recall_framing import annotate_http_context_pack
from alicebot_api.config import get_settings
from alicebot_api.public_errors import public_exception_response
from alicebot_api.routers._vnext_shared import (
    BaseModel,
    VNextAgentRequest,
    _vnext_agent_actor,
    _vnext_agent_auth_error_response,
    _vnext_agent_identity,
    _vnext_authenticated_agent_identity,
    _vnext_authorized_artifact,
    _vnext_exact_resource_policy,
    _vnext_int,
    _vnext_load_source_trace,
    _vnext_metadata,
    _vnext_permission_response,
    _vnext_policy_checked,
    _vnext_public_error_response,
    _vnext_ref_values,
    _vnext_string_list,
)
from alicebot_api.vnext_agent_control import (
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    append_policy_events,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    agent_key_from_authorization,
    resolve_protected_agent_identity,
)
from alicebot_api.vnext_context_tree import (
    ContextTreeRequest,
    VNextContextTreeService,
    VNextContextTreeStore,
    VNextContextTreeValidationError,
)
from alicebot_api.vnext_queue import VNextQueueNotFoundError
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService, VNextRetrievalValidationError
from alicebot_api.vnext_store import PostgresVNextStore


trace_router = APIRouter()
context_router = APIRouter()


class VNextContextPackRequest(VNextAgentRequest):
    user_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    scope: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)


def _vnext_optional_bool(mapping: dict[str, object], key: str) -> bool | None:
    """Tri-state option: absent (or non-boolean) means "caller did not say".

    Retrieval flags such as ``include_sources`` treat None as "let the
    context_depth tier decide", so absence must stay distinguishable from an
    explicit false.
    """
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


def _vnext_text_option(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _vnext_artifact_trace(
    *,
    artifact: dict[str, object],
    sources: list[dict[str, object]],
    quality_evals: list[dict[str, object]],
    events: list[dict[str, object]],
) -> dict[str, object]:
    artifact_id = str(artifact["id"])
    metadata = _vnext_metadata(artifact)
    source_refs = _vnext_ref_values(metadata.get("source_refs")) + _vnext_ref_values(metadata.get("source_ids"))
    related_sources = [
        source
        for source in sources
        if str(source.get("id")) in source_refs or f"source:{source.get('id')}" in source_refs
    ]
    related_evals = [rating for rating in quality_evals if str(rating.get("artifact_id")) == artifact_id]
    related_events = [
        event
        for event in events
        if str(event.get("target_type") or "") == "artifact" and str(event.get("target_id") or "") == artifact_id
    ]
    return {
        "trace_id": metadata.get("trace_id") or metadata.get("scheduler_run_id") or f"artifact:{artifact_id}",
        "trace_kind": "artifact_review",
        "artifact": artifact,
        "sources": related_sources,
        "quality_evals": related_evals,
        "events": related_events,
        "summary": {
            "artifact_id": artifact_id,
            "source_count": len(related_sources),
            "quality_eval_count": len(related_evals),
            "event_count": len(related_events),
            "scheduler_run_id": metadata.get("scheduler_run_id"),
            "agent_run_id": metadata.get("agent_run_id"),
        },
    }


@trace_router.get("/v0/vnext/traces/sources/{source_id}")
def get_vnext_source_trace(source_id: UUID, user_id: UUID) -> JSONResponse:
    settings = get_settings()
    with user_connection(settings.database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.get_source(str(source_id))
        if source is None:
            return _vnext_public_error_response(status_code=404, detail="vNext source was not found")
        payload = _vnext_load_source_trace(
            store=store,
            source=source,
        )
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@trace_router.get("/v0/vnext/traces/artifacts/{artifact_id}")
def get_vnext_artifact_trace(
    artifact_id: UUID,
    user_id: UUID,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = resolve_protected_agent_identity(
                store,
                user_id=user_id,
                raw_key=agent_key_from_authorization(authorization),
                payload={},
            )
            artifact, _decision = _vnext_authorized_artifact(
                store=store,
                identity=identity,
                artifact_id=str(artifact_id),
                action="artifact.lookup",
                for_update=False,
            )
            metadata = _vnext_metadata(artifact)
            source_refs = _vnext_ref_values(metadata.get("source_refs")) + _vnext_ref_values(metadata.get("source_ids"))
            source_ids: list[str] = []
            for source_ref in source_refs:
                source_id = source_ref.removeprefix("source:")
                try:
                    UUID(source_id)
                except ValueError:
                    continue
                if source_id not in source_ids:
                    source_ids.append(source_id)
            authorized_sources: list[dict[str, object]] = []
            for source in store.get_sources_by_ids(source_ids):
                source_id = str(source.get("id"))
                source_decision = _vnext_exact_resource_policy(
                    identity=identity,
                    action="artifact.lookup",
                    resource=source,
                    source_resource=True,
                )
                append_policy_events(
                    store,
                    identity=identity,
                    decision=source_decision,
                    target_type="source",
                    target_id=source_id,
                )
                if source_decision.decision != "blocked":
                    authorized_sources.append(source)
            payload = _vnext_artifact_trace(
                artifact=artifact,
                sources=authorized_sources,
                quality_evals=store.list_artifact_quality_ratings(artifact_id=str(artifact_id), limit=100),
                events=store.list_events(
                    target_type="artifact",
                    target_id=str(artifact_id),
                    limit=100,
                ),
            )
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except VNextQueueNotFoundError:
        return _vnext_public_error_response(status_code=404, detail="vNext artifact was not found")
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))


@context_router.post("/v0/vnext/context-packs")
def create_vnext_context_pack(
    request: VNextContextPackRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    scope = request.scope
    options = request.options
    try:
        identity = _vnext_agent_identity(request)
    except AgentIdentityValidationError as exc:
        return public_exception_response(exc, status_code=400)

    try:
        requested_domains = _vnext_string_list(scope, "domains")
        requested_sensitivity = _vnext_string_list(options, "sensitivity_allowed") or (
            "public",
            "internal",
            "private",
            "unknown",
        )
        # Only forwarded when the caller sets them, so the retrieval request
        # dataclass stays the source of truth for the tier defaults.
        tuning_kwargs: dict[str, object] = {}
        context_depth = _vnext_text_option(options, "context_depth")
        if context_depth is not None:
            tuning_kwargs["context_depth"] = context_depth
        budget_strategy = _vnext_text_option(options, "budget_strategy")
        if budget_strategy is not None:
            tuning_kwargs["budget_strategy"] = budget_strategy
        retrieval_request = VNextRetrievalRequest(
            query=request.query,
            domains=requested_domains,
            projects=_vnext_string_list(scope, "projects"),
            people=_vnext_string_list(scope, "people"),
            time_window=str(scope.get("time_window", "all")),
            sensitivity_allowed=requested_sensitivity,
            # Tri-state: absent means "let the context_depth tier decide";
            # an explicit true/false always wins.
            include_sources=_vnext_optional_bool(options, "include_sources"),
            include_contradictions=_vnext_optional_bool(options, "include_contradictions"),
            max_items=_vnext_int(options, "max_items", 8),
            max_tokens=_vnext_int(options, "max_tokens", 8000),
            **tuning_kwargs,  # type: ignore[arg-type]
        )
        with user_connection(settings.database_url, request.user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = _vnext_authenticated_agent_identity(
                store, request, user_id=request.user_id, authorization=authorization
            )
            decision = _vnext_policy_checked(
                store=store,
                identity=identity,
                action="context_pack.request",
                domains=requested_domains,
                sensitivity_allowed=requested_sensitivity,
                project_scope=tuple(request.project_scope) or _vnext_string_list(scope, "projects"),
            )
            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            actor_type, actor_id = _vnext_agent_actor(identity, fallback="system")
            payload = annotate_http_context_pack(
                store,
                VNextRetrievalService(store).compile_context_pack(
                VNextRetrievalRequest(
                    query=retrieval_request.query,
                    domains=decision.effective_domains,
                    projects=decision.effective_project_scope,
                    people=retrieval_request.people,
                    time_window=retrieval_request.time_window,
                    sensitivity_allowed=decision.effective_sensitivity_allowed,
                    include_sources=retrieval_request.include_sources,
                    include_contradictions=retrieval_request.include_contradictions,
                    context_depth=retrieval_request.context_depth,
                    budget_strategy=retrieval_request.budget_strategy,
                    max_items=retrieval_request.max_items,
                    max_tokens=retrieval_request.max_tokens,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                    trace_id=request.trace_id or decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                )
                ),
            )
    except VNextRetrievalValidationError:
        return _vnext_public_error_response(status_code=400, detail="vNext context-pack request is invalid")
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(payload),
    )


@context_router.get("/v0/vnext/context-tree")
def get_vnext_context_tree(
    user_id: UUID,
    query: str = "",
    domains: Annotated[list[str] | None, Query()] = None,
    sensitivity_allowed: Annotated[list[str] | None, Query()] = None,
    limit: int = 12,
    include_events: bool = True,
) -> JSONResponse:
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            payload = VNextContextTreeService(cast(VNextContextTreeStore, store)).build_tree(
                ContextTreeRequest(
                    query=query,
                    domains=tuple(domains or ()),
                    sensitivity_allowed=tuple(sensitivity_allowed or ("public", "internal", "private", "unknown")),
                    limit=limit,
                    include_events=include_events,
                    generated_by="user",
                )
            )
    except VNextContextTreeValidationError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(status_code=200, content=jsonable_encoder(payload))
