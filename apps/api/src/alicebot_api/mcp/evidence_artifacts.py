"""Mechanical MCP evidence artifacts carrier."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID
from alicebot_api.continuity_evidence import (
    build_continuity_explain,
    get_continuity_artifact_detail,
)
from alicebot_api.contracts import TemporalExplainQueryInput
from alicebot_api.config import get_settings
from alicebot_api.recall_framing import frame_disclosed_tree, memory_writer
from alicebot_api.store import JsonObject
from alicebot_api.temporal_state import get_temporal_explain
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    PolicyDecision,
    resource_project_scope,
)
from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
)
from alicebot_api.vnext_project_scope import (
    resolve_project_scope,
    source_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject
from alicebot_api.vnext_retrieval import MEMORY_ENTITY_EDGE_TYPES
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_store import PostgresVNextStore

from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _agent_identity_from_arguments,
    _is_sqlite_backend,
    _json_object,
    _parse_bool,
    _parse_int,
    _parse_optional_datetime,
    _parse_optional_text,
    _parse_optional_uuid,
    _parse_required_text,
    _parse_required_uuid,
    _persist_vnext_deferred_embedding_inputs,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _store_context,
    _vnext_store_context,
)


def _authorize_continuity_explain_target(
    context: MCPRuntimeContext,
    *,
    identity: AgentIdentity,
    continuity_object_id: UUID,
) -> None:
    with _store_context(context) as store:
        continuity_object = store.get_continuity_object_optional(continuity_object_id)
    if not isinstance(continuity_object, Mapping):
        raise _ExplainAuthorizationError()
    scope = _continuity_object_project_scope(continuity_object)
    classified = {
        **continuity_object,
        "domain": _continuity_object_classification(continuity_object, "domain"),
        "sensitivity": _continuity_object_classification(continuity_object, "sensitivity"),
    }
    with _vnext_store_context(context) as policy_store:
        _authorize_explain_resource(
            policy_store,
            identity=identity,
            resource=classified,
            project_scope=scope,
            target_type="continuity_object",
            target_id=str(continuity_object_id),
        )


def _authorize_entity_explain_target(
    context: MCPRuntimeContext,
    *,
    identity: AgentIdentity,
    entity_id: UUID,
) -> None:
    with _store_context(context) as store:
        entity = store.get_entity_optional(entity_id)
        edges = store.list_entity_edges_for_entity(entity_id) if entity is not None else []
    if not isinstance(entity, Mapping):
        raise _ExplainAuthorizationError()

    memory_ids: list[str] = []

    def add_memory_ids(value: object) -> None:
        if not isinstance(value, list):
            raise _ExplainAuthorizationError()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise _ExplainAuthorizationError()
            memory_ids.append(item)

    add_memory_ids(entity.get("source_memory_ids"))
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise _ExplainAuthorizationError()
        add_memory_ids(edge.get("source_memory_ids"))
    memory_ids = list(dict.fromkeys(memory_ids))
    if not memory_ids:
        raise _ExplainAuthorizationError()

    with _vnext_store_context(context) as policy_store:
        for memory_id in memory_ids:
            memory = policy_store.get_memory(memory_id)
            if not isinstance(memory, Mapping):
                raise _ExplainAuthorizationError()
            _authorize_explain_resource(
                policy_store,
                identity=identity,
                resource=memory,
                project_scope=_backing_memory_project_scope(memory),
                target_type="memory",
                target_id=memory_id,
            )


def _handle_alice_explain(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    memory_id = _parse_optional_text(arguments, "memory_id")
    continuity_object_id = _parse_optional_uuid(arguments, "continuity_object_id")
    entity_id = _parse_optional_uuid(arguments, "entity_id")
    provided = [value for value in (memory_id, continuity_object_id, entity_id) if value is not None]
    if len(provided) > 1:
        raise MCPToolError("alice_explain accepts exactly one of memory_id, continuity_object_id, or entity_id")
    if memory_id is not None:
        return _handle_alice_vnext_memory_audit(context, arguments)
    if _is_sqlite_backend(context):
        raise MCPToolError(
            "alice_explain with entity_id or continuity_object_id is available on the Postgres "
            "backend; pass memory_id on the SQLite on-ramp"
        )
    identity = _agent_identity_from_arguments(context, arguments)
    if entity_id is not None:
        if _is_key_bound_explain(identity):
            assert identity is not None
            try:
                _authorize_entity_explain_target(context, identity=identity, entity_id=entity_id)
            except _ExplainAuthorizationError as exc:
                _raise_explain_authorization_error(identity, exc)
        try:
            with _store_context(context) as store:
                return _json_object(
                    get_temporal_explain(
                        store,
                        user_id=context.user_id,
                        request=TemporalExplainQueryInput(
                            entity_id=entity_id,
                            at=_parse_optional_datetime(arguments, "at"),
                        ),
                    ),
                )
        except LookupError:
            if _is_key_bound_explain(identity):
                raise MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE) from None
            raise
    if continuity_object_id is None:
        raise MCPToolError("alice_explain requires memory_id, continuity_object_id, or entity_id")

    include_raw_content = _parse_bool(arguments, key="include_raw_content", default=False)
    if include_raw_content and get_settings().app_env not in {"development", "test"}:
        raise MCPToolError("include_raw_content is restricted to development/test environments")

    if _is_key_bound_explain(identity):
        assert identity is not None
        try:
            _authorize_continuity_explain_target(
                context,
                identity=identity,
                continuity_object_id=continuity_object_id,
            )
        except _ExplainAuthorizationError as exc:
            _raise_explain_authorization_error(identity, exc)
    try:
        with _store_context(context) as store:
            return _json_object(
                build_continuity_explain(
                    store,
                    user_id=context.user_id,
                    continuity_object_id=continuity_object_id,
                    include_raw_content=include_raw_content,
                )
            )
    except LookupError:
        if _is_key_bound_explain(identity):
            raise MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE) from None
        raise


def _handle_alice_artifact_inspect(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    include_raw_content = _parse_bool(arguments, key="include_raw_content", default=False)
    if include_raw_content and get_settings().app_env not in {"development", "test"}:
        raise MCPToolError("include_raw_content is restricted to development/test environments")

    with _store_context(context) as store:
        return _json_object(
            get_continuity_artifact_detail(
                store,
                user_id=context.user_id,
                artifact_id=_parse_required_uuid(arguments, "artifact_id"),
                include_raw_content=include_raw_content,
            )
        )


_MEMORY_TIMELINE_MAX_ENTRIES = 50


_EXPLAIN_UNAVAILABLE_MESSAGE = "requested explanation is unavailable"


class _ExplainAuthorizationError(RuntimeError):
    """Internal stop signal for a fully-authorized explain expansion."""

    def __init__(self, decision: PolicyDecision | None = None) -> None:
        super().__init__(_EXPLAIN_UNAVAILABLE_MESSAGE)
        self.decision = decision


def _is_key_bound_explain(identity: AgentIdentity | None) -> bool:
    return identity is not None and identity.auth == "agent_api_key"


def _raise_explain_authorization_error(
    identity: AgentIdentity | None,
    error: _ExplainAuthorizationError,
) -> None:
    if _is_key_bound_explain(identity):
        raise MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)
    if error.decision is not None:
        _raise_mcp_policy_blocked(error.decision)
    raise MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)


def _authorize_explain_resource(
    store: object,
    *,
    identity: AgentIdentity | None,
    resource: Mapping[str, object],
    project_scope: tuple[str, ...],
    target_type: str,
    target_id: str,
) -> None:
    """Require an unfiltered policy decision for one expanded resource."""

    _actor_type, _actor_id, decision = _policy_checked(
        store,  # type: ignore[arg-type]
        identity=identity,
        action="memory.audit",
        domains=(str(resource.get("domain") or "unknown"),),
        sensitivity_allowed=(str(resource.get("sensitivity") or "unknown"),),
        project_scope=project_scope,
        require_explicit_project_scope=True,
        target_type=target_type,
        target_id=target_id,
    )
    # ``allowed_with_filtering`` is not sufficient for an explain response:
    # the downstream services expand related rows and do not accept filters.
    if decision.decision != "allowed":
        raise _ExplainAuthorizationError(decision)


def _backing_memory_project_scope(memory: Mapping[str, object]) -> tuple[str, ...]:
    """Resolve scope from a full memory row, then its legacy value payload."""

    row_scope = resolve_project_scope(memory)
    if row_scope.present or row_scope.values:
        return row_scope.values
    value = memory.get("value")
    if isinstance(value, Mapping):
        return resolve_project_scope(value).values
    return ()


def _continuity_object_project_scope(row: Mapping[str, object]) -> tuple[str, ...]:
    """Resolve legacy continuity scope with provenance precedence over body."""

    provenance = row.get("provenance")
    body = row.get("body")
    envelope: dict[str, object] = {
        "metadata_json": dict(provenance) if isinstance(provenance, Mapping) else {},
        "scope_json": dict(body) if isinstance(body, Mapping) else {},
    }
    return resolve_project_scope(envelope).values


def _continuity_object_classification(row: Mapping[str, object], key: str) -> str:
    for container_key in ("provenance", "body"):
        container = row.get(container_key)
        if isinstance(container, Mapping):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unknown"


def _authorize_memory_audit_provenance(
    store: object,
    *,
    identity: AgentIdentity | None,
    provenance_links: object,
) -> set[str]:
    """Authorize every persisted source disclosed by a memory audit."""

    # Keyless local operator calls retain their historical tolerance for old
    # or incomplete provenance rows. Authenticated callers fail closed.
    if not _is_key_bound_explain(identity):
        return set()
    if not isinstance(provenance_links, list):
        raise _ExplainAuthorizationError()
    links = provenance_links
    authorized_source_ids: set[str] = set()
    get_source = getattr(store, "get_source", None)
    if not callable(get_source):
        raise _ExplainAuthorizationError()
    for link in links:
        if not isinstance(link, Mapping):
            raise _ExplainAuthorizationError()
        source_id_value = link.get("source_id")
        # A chunk id cannot be authorized without its persisted parent source.
        if source_id_value is None or str(source_id_value).strip() == "":
            raise _ExplainAuthorizationError()
        source_id = str(source_id_value)
        source = get_source(source_id)
        if not isinstance(source, Mapping):
            raise _ExplainAuthorizationError()
        source_chunk_id = link.get("source_chunk_id")
        if source_chunk_id is not None and str(source_chunk_id).strip():
            list_source_chunks = getattr(store, "list_source_chunks", None)
            if not callable(list_source_chunks):
                raise _ExplainAuthorizationError()
            parent_chunks = list_source_chunks(source_id)
            if not any(
                isinstance(chunk, Mapping) and str(chunk.get("id") or "") == str(source_chunk_id)
                for chunk in parent_chunks
            ):
                raise _ExplainAuthorizationError()
        _authorize_explain_resource(
            store,
            identity=identity,
            resource=source,
            project_scope=source_project_scope(source),
            target_type="source",
            target_id=source_id,
        )
        authorized_source_ids.add(source_id)
    return authorized_source_ids


def _entity_backing_is_fully_authorized(
    store: object,
    *,
    identity: AgentIdentity,
    entity_id: str,
) -> bool:
    """Whether every active memory/source edge backing an entity is visible."""

    list_edges = getattr(store, "list_edges", None)
    if not callable(list_edges):
        return False
    edges = [*list_edges(from_id=entity_id), *list_edges(to_id=entity_id)]
    backing_seen = False
    try:
        for edge in edges:
            if not isinstance(edge, Mapping):
                return False
            for type_key, id_key in (("from_type", "from_id"), ("to_type", "to_id")):
                resource_type = str(edge.get(type_key) or "")
                resource_id = str(edge.get(id_key) or "")
                if not resource_id or resource_type == "entity":
                    continue
                if resource_type == "memory":
                    backing_seen = True
                    memory = store.get_memory(resource_id)  # type: ignore[attr-defined]
                    if not isinstance(memory, Mapping):
                        return False
                    _authorize_explain_resource(
                        store,
                        identity=identity,
                        resource=memory,
                        project_scope=_backing_memory_project_scope(memory),
                        target_type="memory",
                        target_id=resource_id,
                    )
                elif resource_type == "source":
                    backing_seen = True
                    source = store.get_source(resource_id)  # type: ignore[attr-defined]
                    if not isinstance(source, Mapping):
                        return False
                    _authorize_explain_resource(
                        store,
                        identity=identity,
                        resource=source,
                        project_scope=source_project_scope(source),
                        target_type="source",
                        target_id=resource_id,
                    )
        return backing_seen
    except _ExplainAuthorizationError:
        return False


def _authorized_memory_audit_entity_ids(
    store: object,
    *,
    identity: AgentIdentity | None,
    chain: object,
) -> set[str] | None:
    """Filter optional linked-entity annotations through all their backings."""

    if not _is_key_bound_explain(identity):
        return None
    assert identity is not None
    nodes = chain if isinstance(chain, list) else []
    candidate_ids: list[str] = []
    for node in nodes:
        if isinstance(node, Mapping):
            candidate_ids.extend(_memory_linked_entity_ids(store, str(node.get("id") or "")))
    return {
        entity_id
        for entity_id in dict.fromkeys(candidate_ids)
        if _entity_backing_is_fully_authorized(store, identity=identity, entity_id=entity_id)
    }


def _timeline_sort_key(value: object) -> str:
    """ISO-8601 strings (and datetimes rendered by json_safe) sort lexically;
    entries without a usable timestamp sort last, keeping insertion order."""
    rendered = json_safe(value)
    if isinstance(rendered, str) and rendered.strip():
        return rendered.replace("Z", "+00:00")
    return "9999"


def _memory_linked_entity_ids(store: object, memory_id: str) -> list[str]:
    """Return de-duplicated entity ids connected to one memory."""

    list_edges = getattr(store, "list_edges")
    entity_ids: list[str] = []
    edge_sides = (
        (list_edges(from_id=memory_id), "memory", "entity", "to_id"),
        (list_edges(to_id=memory_id), "entity", "memory", "from_id"),
    )
    for edges, from_type, to_type, entity_key in edge_sides:
        for edge in edges:
            if edge.get("edge_type") not in MEMORY_ENTITY_EDGE_TYPES:
                continue
            if str(edge.get("from_type")) != from_type or str(edge.get("to_type")) != to_type:
                continue
            entity_ids.append(str(edge.get(entity_key)))
    return list(dict.fromkeys(entity_ids))


def _memory_linked_entities(
    store: object,
    memory_id: str,
    *,
    allowed_entity_ids: set[str] | None = None,
) -> list[VNextJsonObject]:
    """Entities connected to one memory via mentions/about graph edges.

    Walks both edge directions (memory -> entity and entity -> memory) and
    resolves each linked entity to a compact record. Callers must check
    store support (list_edges + get_entity) first.
    """
    get_entity = getattr(store, "get_entity")
    entities: list[VNextJsonObject] = []
    for entity_id in _memory_linked_entity_ids(store, memory_id):
        if allowed_entity_ids is not None and entity_id not in allowed_entity_ids:
            continue
        row = get_entity(entity_id)
        if row is None:
            continue
        entities.append(
            {
                "id": str(row.get("id")),
                "name": row.get("name"),
                "entity_type": row.get("entity_type"),
                "mention_count": row.get("mention_count"),
            }
        )
    return entities


def _memory_evolution_timeline(
    chain: list[VNextJsonObject], revisions: list[Mapping[str, object]]
) -> list[VNextJsonObject]:
    """Merge the supersession chain and revision history into one story.

    One chronological list of ``{at, kind, memory_id, summary}`` entries
    answering "how did this belief evolve": the oldest chain node is the
    creation, each later chain node is a replacement (``superseded_by``),
    and the audited memory's revisions fill in corrections and edits.
    Cycle safety comes from the chain itself (the commit service walks
    pointers with a visited set and a depth bound); the merged list is
    additionally capped at ``_MEMORY_TIMELINE_MAX_ENTRIES``.
    """
    entries: list[VNextJsonObject] = []
    for index, node in enumerate(chain):
        title = node.get("title")
        summary = str(title) if isinstance(title, str) and title.strip() else str(node.get("id"))
        entries.append(
            {
                "at": json_safe(node.get("created_at")),
                "kind": "created" if index == 0 else "superseded_by",
                "memory_id": str(node.get("id")),
                "summary": summary if index == 0 else f"Replaced by: {summary}",
            }
        )
    chain_has_successor = any(node.get("relation") == "successor" for node in chain)
    for revision in revisions:
        revision_type = str(revision.get("revision_type") or "")
        if revision_type == "created":
            continue  # the chain already carries the creation entry
        if revision_type == "corrected":
            kind = "corrected"
        elif revision_type == "superseded":
            if chain_has_successor:
                # The successor chain node already tells this part of the
                # story; a second superseded_by entry would just be noise.
                continue
            kind = "superseded_by"
        else:
            kind = "revised"
        reason = revision.get("reason")
        summary = str(reason) if isinstance(reason, str) and reason.strip() else revision_type
        entries.append(
            {
                "at": json_safe(revision.get("created_at")),
                "kind": kind,
                "memory_id": str(revision.get("memory_id")),
                "summary": summary,
            }
        )
    entries.sort(key=lambda entry: _timeline_sort_key(entry.get("at")))
    # Over the bound, keep the most recent entries: they answer "where did
    # this belief end up" better than ancient intermediate edits.
    return entries[-_MEMORY_TIMELINE_MAX_ENTRIES:]


def _extend_memory_audit(
    store: object,
    payload: VNextJsonObject,
    *,
    allowed_entity_ids: set[str] | None = None,
) -> VNextJsonObject:
    """Add entity links and the evolution timeline to an audit payload.

    Chain nodes gain an ``entities`` list (via mentions/about edges) when
    the store has the entity substrate; stores without it keep the plain
    chain. The ``timeline`` field is always added on the memory_id branch.
    """
    chain_value = payload.get("supersession_chain")
    chain = [node for node in chain_value if isinstance(node, dict)] if isinstance(chain_value, list) else []
    revisions_value = payload.get("revisions")
    revisions = (
        [row for row in revisions_value if isinstance(row, Mapping)] if isinstance(revisions_value, list) else []
    )
    supports_entities = callable(getattr(store, "list_edges", None)) and callable(getattr(store, "get_entity", None))
    if supports_entities:
        for node in chain:
            node["entities"] = _memory_linked_entities(
                store,
                str(node.get("id")),
                allowed_entity_ids=allowed_entity_ids,
            )
    payload["timeline"] = _memory_evolution_timeline(chain, revisions)
    return payload


def _handle_alice_vnext_memory_audit(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    payload: VNextJsonObject | None = None
    authorization_error: _ExplainAuthorizationError | None = None
    validation_error: VNextMemoryCommitValidationError | None = None
    with _vnext_store_context(context) as store:
        memory_id = _parse_required_text(arguments, "memory_id")
        try:
            audit = VNextMemoryCommitService(store).audit(
                memory_id=memory_id,
                authorize_memory=lambda memory: _authorize_explain_resource(
                    store,
                    identity=identity,
                    resource=memory,
                    project_scope=resource_project_scope(memory),
                    target_type="memory",
                    target_id=str(memory.get("id") or ""),
                ),
            )
            _authorize_memory_audit_provenance(
                store,
                identity=identity,
                provenance_links=audit.get("provenance_links"),
            )
            allowed_entity_ids = _authorized_memory_audit_entity_ids(
                store,
                identity=identity,
                chain=audit.get("supersession_chain"),
            )
            extended = _extend_memory_audit(
                store,
                audit,
                allowed_entity_ids=allowed_entity_ids,
            )
            # Frame the stored notes a model reads. Timeline summaries and
            # event payloads stay the audit record Alice wrote.
            payload = dict(extended)
            memory = extended.get("memory")
            framed_memory = frame_disclosed_tree(memory)
            if isinstance(memory, Mapping) and isinstance(framed_memory, dict):
                framed_memory["writer"] = memory_writer(store, memory)
            payload["memory"] = framed_memory
            payload["revisions"] = frame_disclosed_tree(extended.get("revisions"))
            payload["provenance_links"] = frame_disclosed_tree(extended.get("provenance_links"))
            payload["supersession_chain"] = frame_disclosed_tree(extended.get("supersession_chain"))
        except _ExplainAuthorizationError as exc:
            authorization_error = exc
        except VNextMemoryCommitValidationError as exc:
            validation_error = exc
    if authorization_error is not None:
        _raise_explain_authorization_error(identity, authorization_error)
    if validation_error is not None:
        if _is_key_bound_explain(identity):
            raise MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)
        raise validation_error
    if payload is None:
        raise MCPToolError("vNext memory audit did not complete")
    return _json_object(payload)


def _handle_alice_vnext_review_items(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    with _vnext_store_context(context) as store:
        items = [
            row
            for row in store.list_memories(status=None)
            if str(row.get("status")) in {"candidate", "needs_review", "private_only"}
        ][: _parse_int(arguments, key="limit", default=20, minimum=1, maximum=100)]
    return _json_object({"items": items, "count": len(items)})


def _authorize_vnext_artifact_target(
    store: PostgresVNextStore,
    *,
    identity: AgentIdentity | None,
    artifact_id: str,
    action: str,
    for_update: bool,
) -> tuple[VNextJsonObject, str, str | None, PolicyDecision]:
    """Authorize one persisted artifact before returning or mutating it."""

    artifact = store.get_artifact_for_update(artifact_id) if for_update else store.get_artifact(artifact_id)
    if artifact is None:
        raise MCPToolError(f"artifact {artifact_id} was not found")

    actor_type, actor_id, raw_decision = _policy_checked(
        store,
        identity=identity,
        action=action,
        domains=(str(artifact.get("domain") or "unknown"),),
        sensitivity_allowed=(str(artifact.get("sensitivity") or "unknown"),),
        project_scope=resource_project_scope(artifact),
        require_explicit_project_scope=True,
        require_unfiltered_target=True,
        target_type="artifact",
        target_id=artifact_id,
    )
    return artifact, actor_type, actor_id, raw_decision


def _handle_alice_vnext_artifact_get(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    artifact_id = _parse_required_text(arguments, "artifact_id")
    blocked_decision: PolicyDecision | None = None
    artifact: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        target, _actor_type, _actor_id, decision = _authorize_vnext_artifact_target(
            store,
            identity=identity,
            artifact_id=artifact_id,
            action="artifact.lookup",
            for_update=False,
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            artifact = target
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if artifact is None:
        raise MCPToolError("vNext artifact lookup did not complete")
    return _json_object(artifact)


def _handle_alice_vnext_artifact_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
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
            result = dispatch_vnext_artifact_review(
                store,
                artifact_id=artifact_id,
                action=_parse_required_text(arguments, "action"),
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
            )
            payload = result.artifact
            deferred_embedding_inputs = result.deferred_embedding_inputs
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext artifact review did not complete")
    _persist_vnext_deferred_embedding_inputs(
        context,
        deferred_embedding_inputs,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )
    return _json_object(payload)
