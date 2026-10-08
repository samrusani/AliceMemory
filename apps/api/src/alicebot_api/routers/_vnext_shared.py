from __future__ import annotations

from dataclasses import replace
from typing import Literal, Mapping
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field

from alicebot_api.public_errors import public_exception_response
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentIdentityValidationError,
    AgentPolicyBlockedError,
    PolicyDecision,
    PromotionCandidate,
    PromotionSettings,
    append_policy_events,
    evaluate_agent_policy,
    resource_project_scope,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    agent_key_from_authorization,
    resolve_protected_agent_identity,
)
from alicebot_api.vnext_label_guard import (
    LabelGuard,
    apply_unverified_rule,
    effective_row_for_fence,
    outside_caller_limits,
    policy_labels,
)
from alicebot_api.vnext_project_scope import source_project_scope
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.vnext_queue import VNextQueueNotFoundError
from alicebot_api.vnext_store import PostgresVNextStore, is_redacted_project_update_artifact


class BaseModel(PydanticBaseModel):
    """Fail-closed request model shared by all HTTP body contracts."""

    model_config = ConfigDict(extra="forbid")


class VNextAgentIdentityRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=120)
    agent_type: str = Field(default="unknown", min_length=1, max_length=80)
    agent_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    task_id: str | None = Field(default=None, min_length=1, max_length=160)
    project_scope: list[str] = Field(default_factory=list)
    permission_profile: str | None = Field(default=None, min_length=1, max_length=80)


class VNextAgentRequest(BaseModel):
    agent: VNextAgentIdentityRequest | None = None
    agent_identity: VNextAgentIdentityRequest | None = None
    agent_id: str | None = Field(default=None, min_length=1, max_length=120)
    agent_type: str | None = Field(default=None, min_length=1, max_length=80)
    agent_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    task_id: str | None = Field(default=None, min_length=1, max_length=160)
    project_scope: list[str] = Field(default_factory=list)
    permission_profile: str | None = Field(default=None, min_length=1, max_length=80)
    trace_id: str | None = Field(default=None, min_length=1, max_length=160)


VNextDomain = Literal[
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
]
VNextSensitivity = Literal[
    "public",
    "internal",
    "private",
    "confidential",
    "highly_sensitive",
    "sacred",
    "regulated",
    "unknown",
]


def _vnext_public_error_response(*, status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _vnext_string_list(mapping: dict[str, object], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if not isinstance(value, list):
        return ()
    output: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if stripped:
            output.append(stripped)
    return tuple(output)


def _vnext_int(mapping: dict[str, object], key: str, default: int) -> int:
    value = mapping.get(key)
    return value if isinstance(value, int) else default


def _vnext_metadata(row: dict[str, object] | None) -> dict[str, object]:
    if row is None:
        return {}
    value = row.get("metadata_json")
    return value if isinstance(value, dict) else {}


def _vnext_payload(row: dict[str, object]) -> dict[str, object]:
    value = row.get("payload_json")
    return value if isinstance(value, dict) else {}


def _vnext_ref_values(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.strip():
            refs.append(value.strip())
    elif isinstance(value, dict):
        for key in ("source_id", "id", "ref", "source_ref"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)):
                refs.append(str(candidate))
        for nested_key in ("source_ids", "source_refs", "sources"):
            refs.extend(_vnext_ref_values(value.get(nested_key)))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_vnext_ref_values(item))
    return refs


def _vnext_ref_matches_source(value: object, source_id: str) -> bool:
    normalized = str(source_id)
    return any(ref == normalized or ref == f"source:{normalized}" for ref in _vnext_ref_values(value))


def _vnext_row_references_source(row: dict[str, object], source_id: str) -> bool:
    if str(row.get("source_id") or "") == str(source_id):
        return True
    metadata = _vnext_metadata(row)
    for key in ("source_id", "source_ids", "source_ref", "source_refs", "source_references", "selected_source_ids"):
        if _vnext_ref_matches_source(metadata.get(key), source_id):
            return True
    return _vnext_ref_matches_source(row.get("source_event_ids"), source_id)


def _vnext_event_references(
    event: dict[str, object],
    *,
    source_id: str,
    memory_ids: set[str],
    artifact_ids: set[str],
    open_loop_ids: set[str],
) -> bool:
    target_type = str(event.get("target_type") or "")
    target_id = str(event.get("target_id") or "")
    if target_type == "source" and target_id == source_id:
        return True
    if target_type == "memory" and target_id in memory_ids:
        return True
    if target_type == "artifact" and target_id in artifact_ids:
        return True
    if target_type == "open_loop" and target_id in open_loop_ids:
        return True
    payload = _vnext_payload(event)
    return any(
        _vnext_ref_matches_source(payload.get(key), source_id)
        for key in ("source_id", "source_ids", "source_ref", "source_refs", "source_references", "selected_source_ids")
    )


_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT = 500


def _vnext_bounded_trace_rows(
    rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], bool]:
    limit = _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
    return rows[:limit], len(rows) <= limit


def _vnext_source_chunks(
    store: PostgresVNextStore,
    source_id: str,
) -> tuple[list[dict[str, object]], bool]:
    if not hasattr(store, "list_source_chunks"):
        return [], True
    rows = list(
        store.list_source_chunks(
            source_id,
            limit=_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT + 1,
        )
    )
    return _vnext_bounded_trace_rows(rows)


def _vnext_source_trace(
    *,
    store: PostgresVNextStore,
    source: dict[str, object],
    memories: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    open_loops: list[dict[str, object]],
    events: list[dict[str, object]],
    memory_scope: str = "complete",
    collection_completeness: dict[str, bool] | None = None,
) -> dict[str, object]:
    source_id = str(source["id"])
    related_memories = [memory for memory in memories if _vnext_row_references_source(memory, source_id)]
    related_artifacts = [artifact for artifact in artifacts if _vnext_row_references_source(artifact, source_id)]
    related_open_loops = [loop for loop in open_loops if _vnext_row_references_source(loop, source_id)]
    memory_ids = {str(memory["id"]) for memory in related_memories}
    artifact_ids = {str(artifact["id"]) for artifact in related_artifacts}
    open_loop_ids = {str(loop["id"]) for loop in related_open_loops}
    related_events = [
        event
        for event in events
        if _vnext_event_references(
            event,
            source_id=source_id,
            memory_ids=memory_ids,
            artifact_ids=artifact_ids,
            open_loop_ids=open_loop_ids,
        )
    ]
    trace_id = next((str(event.get("trace_id")) for event in related_events if event.get("trace_id")), None)
    chunks, chunks_complete = _vnext_source_chunks(store, source_id)
    default_complete = memory_scope == "complete"
    completeness = {
        "chunks": chunks_complete,
        "candidate_memories": default_complete,
        "artifacts": default_complete,
        "open_loops": default_complete,
        "events": default_complete,
    }
    if collection_completeness is not None:
        completeness.update(collection_completeness)
        completeness["chunks"] = chunks_complete
    truncated_collections = [
        collection_name for collection_name, is_complete in completeness.items() if not is_complete
    ]
    return {
        "trace_id": trace_id or f"source:{source_id}",
        "trace_kind": "capture_to_brief",
        "source": source,
        "chunks": chunks,
        "candidate_memories": related_memories,
        "artifacts": related_artifacts,
        "open_loops": related_open_loops,
        "events": related_events,
        "sampling": {
            "memory_scope": memory_scope,
            "collection_limit": _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT,
            "collection_complete": completeness,
            "truncated_collections": truncated_collections,
            "trace_complete": len(truncated_collections) == 0,
            "memory_history_complete": completeness["candidate_memories"],
        },
        "summary": {
            "source_id": source_id,
            "chunk_count": len(chunks),
            "candidate_memory_count": len(related_memories),
            "artifact_count": len(related_artifacts),
            "open_loop_count": len(related_open_loops),
            "event_count": len(related_events),
        },
    }


def _vnext_readable_trace_rows(store, kind, fetch, identity, *, admit=None):
    """Deepen the prefix until readable truncation can be answered."""

    from alicebot_api.vnext_label_guard import apply_sensitivity_ceiling

    limit = _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
    prefix = limit + 1
    while True:
        fetched = list(fetch(prefix))
        admitted = list(admit(fetched)) if admit is not None else apply_sensitivity_ceiling(store, kind=kind, rows=fetched, identity=identity)
        if len(admitted) > limit or len(fetched) < prefix:
            return _vnext_bounded_trace_rows(admitted)
        prefix *= 2


def _vnext_load_source_trace(
    *,
    store: PostgresVNextStore,
    source: dict[str, object],
    identity: object | None = None,
) -> dict[str, object] | None:
    """Load one bounded source trace and disclose per-collection truncation.

    Returns None when the caller's sensitivity ceiling hides the source, so
    the response carries neither its title nor its id.
    """

    from alicebot_api.vnext_agent_control import AgentIdentity
    from alicebot_api.vnext_label_guard import apply_sensitivity_ceiling
    from alicebot_api.vnext_open_loop_references import withhold_unreadable_references

    caller = identity if isinstance(identity, AgentIdentity) else None
    if not apply_sensitivity_ceiling(store, kind="source", rows=[source], identity=caller):
        return None
    source_id = str(source["id"])
    memories, memories_complete = _vnext_readable_trace_rows(
        store, "memory", lambda limit: store.list_memories_referencing_source(source_id=source_id, limit=limit), caller
    )
    artifacts, artifacts_complete = _vnext_readable_trace_rows(
        store, "artifact", lambda limit: store.list_artifacts_referencing_source(source_id=source_id, limit=limit), caller
    )
    open_loops, open_loops_complete = _vnext_readable_trace_rows(
        store, "open_loop", lambda limit: store.list_open_loops_referencing_source(source_id=source_id, limit=limit), caller
    )
    open_loops = withhold_unreadable_references(store, open_loops, fence=SourceReadFence.for_identity(caller))
    kept_ids = {str(row.get("id")) for row in (*memories, *artifacts, *open_loops)}
    kept_ids.add(source_id)
    events, direct_events_complete = _vnext_readable_trace_rows(
        store, "event", lambda limit: store.list_events_for_source_trace(
            source_id=source_id,
            memory_ids=[str(memory["id"]) for memory in memories],
            artifact_ids=[str(artifact["id"]) for artifact in artifacts],
            open_loop_ids=[str(open_loop["id"]) for open_loop in open_loops],
            limit=limit,
        ), caller,
        admit=(lambda rows: [event for event in rows if str(event.get("target_id") or "") in kept_ids])
        if SourceReadFence.for_identity(caller).entity_read_fenced else None,
    )
    events_complete = direct_events_complete and memories_complete and artifacts_complete and open_loops_complete
    return _vnext_source_trace(
        store=store,
        source=source,
        memories=memories,
        artifacts=artifacts,
        open_loops=open_loops,
        events=events,
        collection_completeness={
            "candidate_memories": memories_complete,
            "artifacts": artifacts_complete,
            "open_loops": open_loops_complete,
            "events": events_complete,
        },
    )


def _vnext_normalized_agent_payload(request: VNextAgentRequest) -> dict[str, object]:
    payload = request.model_dump(mode="json")
    if payload.get("agent_identity") is None and isinstance(payload.get("agent"), dict):
        payload["agent_identity"] = payload["agent"]
    nested_request = request.agent_identity or request.agent
    nested = payload.get("agent_identity")
    if nested_request is None or not isinstance(nested, Mapping):
        return payload
    for field in ("agent_id", "agent_type"):
        if field not in request.model_fields_set or field not in nested_request.model_fields_set:
            continue
        top_level = payload.get(field)
        nested_value = nested.get(field)
        normalized_top_level = " ".join(top_level.split()).strip() if isinstance(top_level, str) else top_level
        normalized_nested_value = (
            " ".join(nested_value.split()).strip() if isinstance(nested_value, str) else nested_value
        )
        if (
            normalized_top_level is not None
            and normalized_nested_value is not None
            and normalized_top_level != normalized_nested_value
        ):
            raise AgentIdentityValidationError(
                f"agent_identity conflicts with top-level {field}"
            )
    return payload


def _vnext_agent_identity(request: VNextAgentRequest) -> AgentIdentity | None:
    payload = _vnext_normalized_agent_payload(request)
    return AgentIdentity.from_payload(payload)


def _vnext_authenticated_agent_identity(
    store: PostgresVNextStore,
    request: VNextAgentRequest,
    *,
    user_id: UUID,
    authorization: str | None,
) -> AgentIdentity | None:
    payload = _vnext_normalized_agent_payload(request)
    return resolve_protected_agent_identity(
        store,
        user_id=user_id,
        raw_key=agent_key_from_authorization(authorization),
        payload=payload,
    )


def _vnext_agent_auth_error_response(exc: AgentKeyAuthenticationError) -> JSONResponse:
    return public_exception_response(exc, status_code=exc.status_code)


def _vnext_permission_response(decision: PolicyDecision) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=jsonable_encoder(
            {
                "detail": "agent policy blocked this action",
                "policy_decision": decision.to_record(),
            }
        ),
    )


def _vnext_agent_actor(identity: AgentIdentity | None, *, fallback: str = "user") -> tuple[str, str | None]:
    if identity is None:
        return fallback, None
    return identity.actor_type, identity.agent_id


def _vnext_agent_record(store: PostgresVNextStore, identity: AgentIdentity | None) -> None:
    if identity is None:
        return
    store.upsert_agent_identity(
        {
            "agent_id": identity.agent_id,
            "agent_type": identity.agent_type,
            "permission_profile": identity.permission_profile,
            "project_scope_json": list(identity.project_scope),
            "metadata_json": {
                "last_agent_run_id": identity.agent_run_id,
                "last_task_id": identity.task_id,
            },
        },
        actor_type="agent",
    )


def _vnext_policy_checked(
    *,
    store: PostgresVNextStore,
    identity: AgentIdentity | None,
    action: str,
    domains: tuple[str, ...] = (),
    sensitivity_allowed: tuple[str, ...] = ("public", "internal", "private", "unknown"),
    project_scope: tuple[str, ...] = (),
    project_floor: tuple[str, ...] = (),
    workflow_type: str | None = None,
    write_policy: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    require_explicit_project_scope: bool = False,
    promotion_settings: PromotionSettings | None = None,
    promotion_candidate: PromotionCandidate | None = None,
    owner_verified: bool = False,
) -> PolicyDecision:
    _vnext_agent_record(store, identity)
    decision = evaluate_agent_policy(
        identity=identity,
        action=action,
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        project_scope=project_scope,
        project_floor=project_floor,
        workflow_type=workflow_type,
        write_policy=write_policy,
        require_explicit_project_scope=require_explicit_project_scope,
        promotion_settings=promotion_settings,
        promotion_candidate=promotion_candidate,
        owner_verified=owner_verified,
    )
    append_policy_events(store, identity=identity, decision=decision, target_type=target_type, target_id=target_id)
    return decision


def _vnext_exact_resource_policy(
    *,
    identity: AgentIdentity | None,
    action: str,
    resource: dict[str, object],
    source_resource: bool = False,
) -> PolicyDecision:
    domains, sensitivity_allowed, project_scope, project_floor = policy_labels(resource)
    if source_resource:
        project_scope = source_project_scope(resource)
    decision = evaluate_agent_policy(
        identity=identity,
        action=action,
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        project_scope=project_scope,
        project_floor=project_floor,
        require_explicit_project_scope=bool(identity is not None and identity.project_scope_locked),
    )
    if decision.decision == "allowed_with_filtering":
        decision = replace(
            decision,
            decision="blocked",
            reasons=tuple(dict.fromkeys((*decision.reasons, "exact_target_filtering_not_permitted"))),
        )
    return decision


def _vnext_authorized_artifact(
    *,
    store: PostgresVNextStore,
    identity: AgentIdentity | None,
    artifact_id: str,
    action: str,
    for_update: bool,
) -> tuple[dict[str, object], PolicyDecision]:
    """Load and authorize a persisted artifact before any content is returned.

    Side-effecting handlers lock the target first so the project/domain/
    sensitivity attributes used for authorization remain stable through the
    mutation.  A single artifact cannot be partially filtered: if policy
    removes its domain or sensitivity, access is denied rather than returning
    the unfiltered row.
    """

    artifact = store.get_artifact_for_update(artifact_id) if for_update else store.get_artifact(artifact_id)
    if artifact is None:
        raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
    # An artifact the caller may not read is a missing artifact, found before its state is read, before a policy decision
    # is built from its labels (a refusal would repeat them) and before anything is recorded: the agent record and the
    # policy events are events a key can read back.
    if outside_caller_limits(store, identity, "artifact", artifact):
        raise VNextQueueNotFoundError(f"artifact {artifact_id} was not found")
    if action == "artifact.feedback" and is_redacted_project_update_artifact(artifact):
        raise ValueError("feedback cannot be added to a redacted artifact")

    _vnext_agent_record(store, identity)
    guard = LabelGuard.for_fence(store, SourceReadFence.for_identity(identity))
    effective = guard.effective_row("artifact", artifact)
    decision = _vnext_exact_resource_policy(
        identity=identity,
        action=action,
        resource=effective if isinstance(effective, dict) else artifact,
    )
    decision = apply_unverified_rule(decision, effective if isinstance(effective, Mapping) else None, identity)
    append_policy_events(
        store,
        identity=identity,
        decision=decision,
        target_type="artifact",
        target_id=artifact_id,
    )
    if decision.decision == "blocked":
        raise AgentPolicyBlockedError(decision)
    return artifact, decision


def _vnext_canonical_id(value: object) -> str | None:
    """The id in its canonical spelling, or None when the value is not a well-formed id.

    A value that is not an id names no row, so a door answers it as it answers an id that does not exist, and a
    database cast never sees it.
    """

    if value is None:
        return None
    try:
        return str(UUID(str(value).strip()))
    except ValueError:
        return None


def _vnext_readable_memory(
    store: PostgresVNextStore, identity: AgentIdentity | None, memory_id: object
) -> dict[str, object] | None:
    """The stored memory when this caller may read it now, otherwise None.

    A memory that is missing, deleted or outside the caller's read fence (its sensitivity, domain, project or an
    unverified label) is the same None for a caller with limits. The owner and an unbound admin key are not limited, so
    for them only a missing memory is None.
    """

    memory = store.get_memory(str(memory_id)) if memory_id is not None else None
    if memory is None:
        return None
    fence = SourceReadFence.for_identity(identity)
    if fence.entity_read_fenced and not fence.admits_memory(effective_row_for_fence(store, identity, "memory", memory)):
        return None
    return memory


def _vnext_readable_belief(
    store: PostgresVNextStore, identity: AgentIdentity | None, belief_id: object
) -> dict[str, object] | None:
    """The stored belief when this caller may read the memory behind it, otherwise None.

    A belief has no labels of its own: it is read through the memory it was made from. A belief whose memory the
    caller may not read is the same None as a belief that does not exist.
    """

    canonical = _vnext_canonical_id(belief_id)
    belief = store.get_belief(canonical) if canonical is not None else None
    if belief is None:
        return None
    if SourceReadFence.for_identity(identity).entity_read_fenced:
        if _vnext_readable_memory(store, identity, belief.get("memory_id")) is None:
            return None
    return belief


def _vnext_readable_edge(
    store: PostgresVNextStore, identity: AgentIdentity | None, edge_id: object
) -> dict[str, object] | None:
    """The stored graph edge when this caller may read both of its ends, otherwise None.

    An edge has no labels of its own, and its explanation is made from the text of the rows it joins, so a caller with
    limits may act on it only when it may read every labelled row at its ends. A source, a memory and a belief are checked
    the way their own doors check them; an entity has no label. An end of any other kind, or an end that no longer
    exists, cannot be shown to be readable and the edge is None for a caller with limits.
    """

    canonical = _vnext_canonical_id(edge_id)
    edge = store.get_edge(canonical) if canonical is not None else None
    if edge is None:
        return None
    if not SourceReadFence.for_identity(identity).entity_read_fenced:
        return edge
    from alicebot_api.routers.vnext_memories import _vnext_readable_source

    for side in ("from", "to"):
        kind, end_id = str(edge.get(f"{side}_type") or ""), edge.get(f"{side}_id")
        if kind == "entity":
            continue
        if kind == "source":
            readable = _vnext_readable_source(store, identity, end_id)  # type: ignore[arg-type]
        elif kind == "memory":
            readable = _vnext_readable_memory(store, identity, end_id)
        elif kind == "belief":
            readable = _vnext_readable_belief(store, identity, end_id)
        else:
            readable = None
        if readable is None:
            return None
    return edge
