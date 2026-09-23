"""Mechanical MCP review carrier."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import (
    Protocol,
    cast,
)
from uuid import (
    UUID,
    uuid4,
)
from alicebot_api.continuity_contradictions import (
    get_contradiction_case,
    list_contradiction_cases,
    resolve_contradiction_case,
    sync_contradictions,
)
from alicebot_api.continuity_review import (
    apply_continuity_correction,
    get_continuity_review_detail,
    list_continuity_review_queue,
)
from alicebot_api.continuity_trust import list_trust_signals
from alicebot_api.credential_floor import (
    refuse_credential_activation,
    refuse_credential_material,
    stored_text_fields,
    string_values,
    withhold_credential_text,
)
from alicebot_api.write_bounds import MAX_CORRECTION_FIELD_CHARS, first_oversized
from alicebot_api.contracts import (
    CONTINUITY_REVIEW_QUEUE_ORDER,
    DEFAULT_CONTINUITY_REVIEW_LIMIT,
    MAX_CONTINUITY_REVIEW_LIMIT,
    ContradictionResolutionAction,
    ContradictionStatus,
    ContradictionCaseListQueryInput,
    ContradictionResolveInput,
    ContradictionSyncInput,
    ContinuityCorrectionAction,
    ContinuityCorrectionInput,
    ContinuityReviewQueueQueryInput,
    ContinuityReviewStatusFilter,
    TrustSignalState,
    TrustSignalType,
    TrustSignalListQueryInput,
)
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import (
    AgentPolicyBlockedError,
    PolicyDecision,
    resource_project_scope,
)
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_lifecycle import (
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_SUPERSEDE,
    LifecycleTransitionError,
    resolve_transition,
)
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    VNEXT_DOMAINS,
    is_pending_consolidation_candidate,
)
from alicebot_api.vnext_project_update_guard import (
    PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
    is_pending_project_update_memory,
)
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject
from alicebot_api.vnext_json import json_safe

from .retrieval_shared import (
    _compact_vnext_memory,
    _provenance_count,
    _resource_matches_project_scope,
    _utc_now_iso_text,
)
from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _PROVENANCE_EVIDENCE_ROLES,
    _REVIEW_STATUS_ALIASES,
    _REVIEW_STATUS_CHOICES,
    _agent_identity_from_arguments,
    _json_object,
    _mcp_agent_policy_preflight,
    _parse_int,
    _parse_optional_float,
    _parse_optional_json_object,
    _parse_optional_text,
    _parse_optional_uuid,
    _parse_required_text,
    _parse_required_uuid,
    _parse_review_item_id,
    _parse_review_status,
    _parse_string_list,
    _persist_vnext_deferred_embedding_inputs,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _resolve_review_apply_action,
    _store_context,
    _vnext_store_context,
)


def _vnext_memory_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    requested_domains = _parse_string_list(arguments, "domains")
    requested_sensitivity = _parse_string_list(arguments, "sensitivity_allowed")
    requested_projects = _parse_string_list(arguments, "projects")
    review_item_id = _parse_review_item_id(arguments, required=False)
    if review_item_id is not None:
        memory_id = str(review_item_id)
        blocked_decision: PolicyDecision | None = None
        payload: dict[str, object] | None = None
        with _vnext_store_context(context) as store:
            memory = store.get_memory(memory_id)
            if memory is None:
                raise MCPToolError(f"memory {memory_id} was not found")
            target_domain = str(memory.get("domain") or "unknown")
            target_sensitivity = str(memory.get("sensitivity") or "unknown")
            target_projects = resource_project_scope(memory)
            _actor_type, _actor_id, decision = _policy_checked(
                store,
                identity=identity,
                action="review_items.lookup",
                domains=(target_domain,),
                sensitivity_allowed=(target_sensitivity,),
                project_scope=target_projects,
                require_explicit_project_scope=True,
            )
            if decision.decision == "blocked":
                blocked_decision = decision
            elif (
                target_domain not in decision.effective_domains
                or target_sensitivity not in decision.effective_sensitivity_allowed
                or (requested_domains and target_domain not in requested_domains)
                or (requested_sensitivity and target_sensitivity not in requested_sensitivity)
                or (requested_projects and not _resource_matches_project_scope(memory, requested_projects))
            ):
                raise MCPToolError("memory review item is outside the effective review filters")
            else:
                payload = {
                    "mode": "vnext_detail",
                    "review": {
                        "memory": memory,
                        "revisions": store.list_revisions(memory_id),
                        "provenance_links": store.list_provenance_links(target_type="memory", target_id=memory_id),
                    },
                }
        if blocked_decision is not None:
            _raise_mcp_policy_blocked(blocked_decision)
        if payload is None:
            raise MCPToolError("vNext memory review did not complete")
        return _json_object(payload)

    raw_status = arguments.get("status", "correction_ready")
    if not isinstance(raw_status, str):
        raise MCPToolError("status must be a string")
    normalized_status = raw_status.strip()
    normalized_status = _REVIEW_STATUS_ALIASES.get(normalized_status, normalized_status)
    if normalized_status not in _REVIEW_STATUS_CHOICES:
        allowed = ", ".join(_REVIEW_STATUS_CHOICES)
        raise MCPToolError(f"status must be one of: {allowed}")
    limit = _parse_int(
        arguments,
        key="limit",
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        minimum=1,
        maximum=MAX_CONTINUITY_REVIEW_LIMIT,
    )

    if normalized_status in {"pending_review", "correction_ready"}:
        vnext_status: str | None = "candidate"
    elif normalized_status == "active":
        vnext_status = "active"
    elif normalized_status == "all":
        vnext_status = None
    else:
        return {
            "items": [],
            "count": 0,
            "mode": "vnext_candidates",
            "note": (
                f"status '{normalized_status}' has no canonical vNext equivalent; "
                "use pending_review, correction_ready, active, or all"
            ),
        }

    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="review_items.lookup",
        domains=requested_domains or tuple(VNEXT_DOMAINS),
        sensitivity_allowed=requested_sensitivity or ("public", "internal", "private", "highly_sensitive", "unknown"),
        project_scope=requested_projects,
    )
    with _vnext_store_context(context) as store:
        rows = [
            row
            for row in store.list_memories(status=vnext_status)
            if _resource_matches_project_scope(row, decision.effective_project_scope)
            and str(row.get("domain") or "unknown") in decision.effective_domains
            and str(row.get("sensitivity") or "unknown") in decision.effective_sensitivity_allowed
        ][:limit]
        items = [_compact_vnext_memory(row, provenance_count=_provenance_count(store, row.get("id"))) for row in rows]
    return _json_object({"items": items, "count": len(items), "mode": "vnext_candidates"})


def _canonical_text_from_body(body: Mapping[str, object]) -> str:
    text = body.get("text")
    if isinstance(text, str) and text.strip() != "":
        return text.strip()
    return _canonical_json_dumps(body)


def _canonical_json_dumps(value: object) -> str:
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validated_review_provenance(
    store: object,
    provenance: Mapping[str, object],
    *,
    fallback_confidence: float | None,
) -> JsonObject:
    """Resolve one user-owned source reference before a review mutates anything.

    Both backends scope ``get_source`` and ``list_source_chunks`` to the acting
    user (Postgres through RLS, SQLite through the store's ``user_id``).  The
    returned normalized object is therefore safe to persist as metadata and to
    use for the provenance link.  Validation happens before activation or
    replacement creation so an invalid source, chunk, role, or confidence
    leaves the reviewed candidate unchanged.
    """
    raw_source_id = provenance.get("source_id")
    if not isinstance(raw_source_id, str) or raw_source_id.strip() == "":
        raise MCPToolError("provenance.source_id is required and must be a UUID string")
    source_id = raw_source_id.strip()
    try:
        UUID(source_id)
    except ValueError as exc:
        raise MCPToolError("provenance.source_id must be a valid UUID") from exc

    get_source = getattr(store, "get_source", None)
    if not callable(get_source) or get_source(source_id) is None:
        raise MCPToolError(f"provenance source {source_id} was not found in the current user scope")

    raw_chunk_id = provenance.get("source_chunk_id")
    source_chunk_id: str | None = None
    if raw_chunk_id is not None:
        if not isinstance(raw_chunk_id, str) or raw_chunk_id.strip() == "":
            raise MCPToolError("provenance.source_chunk_id must be a UUID string")
        source_chunk_id = raw_chunk_id.strip()
        try:
            UUID(source_chunk_id)
        except ValueError as exc:
            raise MCPToolError("provenance.source_chunk_id must be a valid UUID") from exc
        list_source_chunks = getattr(store, "list_source_chunks", None)
        if not callable(list_source_chunks):
            raise MCPToolError("the current store cannot validate provenance source chunks")
        owned_chunk_ids = {str(row.get("id")) for row in list_source_chunks(source_id) if isinstance(row, Mapping)}
        if source_chunk_id not in owned_chunk_ids:
            raise MCPToolError(
                f"provenance source chunk {source_chunk_id} does not belong to source "
                f"{source_id} in the current user scope"
            )

    raw_role = provenance.get("evidence_role", "supports")
    if not isinstance(raw_role, str) or raw_role not in _PROVENANCE_EVIDENCE_ROLES:
        raise MCPToolError("provenance.evidence_role must be one of: " + ", ".join(_PROVENANCE_EVIDENCE_ROLES))

    raw_confidence = provenance.get("confidence", fallback_confidence if fallback_confidence is not None else 0.5)
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise MCPToolError("provenance.confidence must be a number between 0 and 1")
    confidence = float(raw_confidence)
    if confidence < 0.0 or confidence > 1.0:
        raise MCPToolError("provenance.confidence must be between 0 and 1")

    raw_quote = provenance.get("quote")
    if raw_quote is not None and not isinstance(raw_quote, str):
        raise MCPToolError("provenance.quote must be a string")

    return {
        "source_id": source_id,
        "source_chunk_id": source_chunk_id,
        "evidence_role": raw_role,
        "confidence": confidence,
        "quote": raw_quote,
    }


def _accepted_review_metadata(
    memory: Mapping[str, object], *, confirmed_at: str, extra: Mapping[str, object] | None = None
) -> JsonObject:
    metadata = (
        dict(cast(Mapping[str, object], memory.get("metadata_json")))
        if isinstance(memory.get("metadata_json"), Mapping)
        else {}
    )
    metadata["review_required"] = False
    agentic_raw = metadata.get("agentic_memory")
    if isinstance(agentic_raw, Mapping):
        agentic = dict(agentic_raw)
        # Dashboard approval is also a terminal answer to any pending inline
        # confirmation carried by the row. Leaving that nested flag pending
        # makes the metadata claim a second decision is still possible after
        # the row has already been accepted.
        confirmation_raw = agentic.get("confirmation")
        if isinstance(confirmation_raw, Mapping) and confirmation_raw.get("status") == "pending":
            confirmation = dict(confirmation_raw)
            confirmation.update({"status": "confirmed", "confirmed_at": confirmed_at})
            agentic["confirmation"] = confirmation
        agentic.update(
            {
                "status": "committed",
                "write_mode": "commit",
                "lifecycle_status": "dashboard_review_accepted",
                "confirmed_at": confirmed_at,
                "requires_dashboard_review": False,
            }
        )
        metadata["agentic_memory"] = agentic
    if extra is not None:
        metadata.update(extra)
    return _json_object(metadata)


def _retired_review_metadata(memory: Mapping[str, object], *, outcome: str) -> JsonObject:
    """Metadata for a review rejection/supersession, closing any pending flag.

    A row proposed via inline confirmation carries a nested
    ``agentic_memory.confirmation`` flag. When a review retires the row, that
    flag must not be left ``pending`` -- otherwise a later confirm() would try
    to reactivate a rejected/superseded row. confirm() independently verifies
    the row's lifecycle status, but clearing the flag here keeps the audit
    record honest and closes the hole at its source.
    """
    metadata = (
        dict(cast(Mapping[str, object], memory.get("metadata_json")))
        if isinstance(memory.get("metadata_json"), Mapping)
        else {}
    )
    agentic_raw = metadata.get("agentic_memory")
    if isinstance(agentic_raw, Mapping):
        agentic = dict(agentic_raw)
        confirmation_raw = agentic.get("confirmation")
        if isinstance(confirmation_raw, Mapping) and confirmation_raw.get("status") == "pending":
            confirmation = dict(confirmation_raw)
            confirmation["status"] = outcome
            agentic["confirmation"] = confirmation
        agentic["lifecycle_status"] = f"review_{outcome}"
        agentic["requires_dashboard_review"] = False
        metadata["agentic_memory"] = agentic
    return _json_object(metadata)


class _RevisionStore(Protocol):
    def append_revision(
        self,
        revision: VNextJsonObject,
        *,
        actor_type: str = "system",
    ) -> VNextJsonObject: ...


def _vnext_review_revision(
    store: _RevisionStore,
    *,
    previous: Mapping[str, object],
    updated: Mapping[str, object],
    revision_type: str,
    action_label: str,
    reason: str | None,
    metadata: JsonObject | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> None:
    store.append_revision(
        {
            "memory_id": str(updated["id"]),
            "memory_key": str(updated["memory_key"]),
            "previous_value": previous.get("value"),
            "new_value": updated.get("value"),
            "source_event_ids": updated.get("source_event_ids"),
            "revision_type": revision_type,
            "action": f"memory_correct_{action_label}",
            "text_before": previous.get("canonical_text"),
            "text_after": str(updated.get("canonical_text") or ""),
            "reason": reason or f"alice_memory_correct action: {action_label}",
            "actor_type": actor_type,
            "actor_id": actor_id,
            "metadata_json": {"action": action_label, **(metadata or {})},
        },
        actor_type=actor_type,
    )


def _refuse_oversized_correction(**fields: object) -> None:
    """Bound a correction's mappings before the credential floor reads them."""

    oversized = first_oversized(fields, MAX_CORRECTION_FIELD_CHARS)
    if oversized is not None:
        raise MCPToolError(f"{oversized} must serialize to {MAX_CORRECTION_FIELD_CHARS} characters or fewer")


def _vnext_memory_correct(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    identity = _agent_identity_from_arguments(context, arguments)
    requested_action = _parse_required_text(arguments, "action")
    resolved_action = _resolve_review_apply_action(requested_action, allow_legacy=True)
    if resolved_action not in {"confirm", "edit", "delete", "supersede"}:
        raise MCPToolError(
            f"action '{requested_action}' is not supported by canonical vNext review; "
            "use approve, edit-and-approve, reject, or supersede-existing"
        )
    memory_id = str(cast(UUID, _parse_review_item_id(arguments, required=True)))
    reason = _parse_optional_text(arguments, "reason")
    now_iso = _utc_now_iso_text()
    actor_type = "agent" if identity is not None else "user"
    actor_id = identity.agent_id if identity is not None else None

    blocked_decision: PolicyDecision | None = None
    consolidation_acceptance: VNextJsonObject | None = None
    consolidation_deferred_inputs: tuple[DeferredMemoryEmbedding, ...] = ()
    with _vnext_store_context(context) as store:
        # Preliminary policy/orientation read only. The mutation transaction
        # below re-authorizes after taking its locks; taking a row lock here
        # would invert graph -> row ordering for consolidation acceptance.
        target = store.get_memory(memory_id)
        if target is None:
            raise MCPToolError(f"memory {memory_id} was not found")
        _checked_actor_type, _checked_actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="memory.review",
            domains=(str(target.get("domain") or "unknown"),),
            sensitivity_allowed=(str(target.get("sensitivity") or "unknown"),),
            project_scope=resource_project_scope(target),
            require_explicit_project_scope=True,
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        elif is_pending_consolidation_candidate(target):
            if resolved_action == "edit":
                raise MCPToolError(
                    "pending consolidation candidates cannot be edit-and-approved; "
                    "regenerate the candidate or approve it unchanged"
                )
            if resolved_action == "confirm":
                try:
                    consolidation_service = VNextMemoryCommitService(store, defer_embeddings=True)
                    consolidation_acceptance = consolidation_service.accept_consolidation_candidate(
                        memory_id,
                        reason=reason or "Approved through alice_memory_correct.",
                        identity=identity,
                    )
                    consolidation_deferred_inputs = consolidation_service.deferred_embedding_inputs
                except AgentPolicyBlockedError as exc:
                    blocked_decision = exc.decision
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if consolidation_acceptance is not None:
        _persist_vnext_deferred_embedding_inputs(
            context,
            consolidation_deferred_inputs,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return _json_object(
            {
                "review_action": {
                    "requested_action": requested_action,
                    "resolved_action": resolved_action,
                    "memory_id": memory_id,
                },
                "memory": consolidation_acceptance["memory"],
                "replacement_object": None,
                "consolidation_acceptance": consolidation_acceptance,
                "mode": "vnext",
            }
        )

    replacement_object: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        memory_service = VNextMemoryCommitService(store, defer_embeddings=True)
        # Every review action shares one graph -> row lock order.  Provenance
        # approval activates a memory too, so it must not be a row-first
        # exception to the lifecycle mutation boundary.
        memory_service.lock_supersession_graph()
        get_memory_for_update = getattr(store, "get_memory_for_update", None)
        memory = get_memory_for_update(memory_id) if callable(get_memory_for_update) else store.get_memory(memory_id)
        if memory is None:
            raise MCPToolError(f"memory {memory_id} was not found")
        # Re-authorize the row after acquiring its mutation lock. The earlier
        # check commits a durable policy audit event; this second check closes
        # the gap where a target could be reassigned between authorization and
        # update.
        _locked_actor_type, _locked_actor_id, locked_decision = _policy_checked(
            store,
            identity=identity,
            action="memory.review",
            domains=(str(memory.get("domain") or "unknown"),),
            sensitivity_allowed=(str(memory.get("sensitivity") or "unknown"),),
            project_scope=resource_project_scope(memory),
            require_explicit_project_scope=True,
        )
        if locked_decision.decision == "blocked":
            _raise_mcp_policy_blocked(locked_decision)
        # Route the retired-status guard through the central transition table so
        # a review cannot approve/reject/supersede an already-retired row.
        _review_operation = {
            "confirm": REVIEW_APPROVE,
            "edit": REVIEW_APPROVE,
            "delete": REVIEW_REJECT,
            "supersede": REVIEW_SUPERSEDE,
        }[resolved_action]
        try:
            resolve_transition(_review_operation, str(memory.get("status") or ""))
        except LifecycleTransitionError as exc:
            raise MCPToolError(f"memory {memory_id} cannot be reviewed from status '{memory.get('status')}'") from exc
        if is_pending_consolidation_candidate(memory):
            raise MCPToolError("memory became a pending consolidation candidate during review; retry the approval")
        if is_pending_project_update_memory(memory):
            raise MCPToolError(PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE)
        event_payload: VNextJsonObject = {
            "requested_action": requested_action,
            "resolved_action": resolved_action,
            "reason": reason,
        }

        rationale_withheld = False
        if resolved_action == "confirm":
            # Approve is an activation (ruling C2): the shared check reads the
            # row as it will become searchable, and the reason persisted with
            # it in the revision and the event.
            refuse_credential_activation(
                memory.get("title"),
                memory.get("canonical_text"),
                memory.get("summary"),
                reason,
                error=MCPToolError,
            )
        elif resolved_action == "delete":
            # A reject always completes (ruling C6): a reason carrying
            # credential material is stored as a fixed placeholder.
            reason, rationale_withheld = withhold_credential_text(reason)
            event_payload["reason"] = reason
        if resolved_action == "confirm":
            updated = store.update_memory(
                memory_id=memory_id,
                patch={
                    "status": "active",
                    "confirmation_status": "confirmed",
                    "last_confirmed_at": now_iso,
                    "last_reviewed_at": now_iso,
                    "metadata_json": _accepted_review_metadata(memory, confirmed_at=now_iso),
                },
                actor_type=actor_type,
            )
            _vnext_review_revision(
                store,
                previous=memory,
                updated=updated,
                revision_type="promoted",
                action_label="approve",
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            memory_service.refresh_memory_derived_state(updated, identity=identity, stage="mcp_review_approve")
        elif resolved_action == "delete":
            updated = store.update_memory(
                memory_id=memory_id,
                patch={
                    "status": "rejected",
                    "last_reviewed_at": now_iso,
                    "metadata_json": _retired_review_metadata(memory, outcome="rejected"),
                },
                actor_type=actor_type,
            )
            _vnext_review_revision(
                store,
                previous=memory,
                updated=updated,
                revision_type="rejected",
                action_label="reject",
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        elif resolved_action == "edit":
            title = _parse_optional_text(arguments, "title")
            body = _parse_optional_json_object(arguments, "body")
            provenance = _parse_optional_json_object(arguments, "provenance")
            confidence = _parse_optional_float(arguments, "confidence")
            if title is None and body is None and provenance is None and confidence is None:
                raise MCPToolError("edit-and-approve requires at least one of title, body, provenance, or confidence")
            validated_provenance = (
                _validated_review_provenance(
                    store,
                    provenance,
                    fallback_confidence=confidence,
                )
                if provenance is not None
                else None
            )
            patch: VNextJsonObject = {
                "status": "active",
                "confirmation_status": "confirmed",
                "last_confirmed_at": now_iso,
                "last_reviewed_at": now_iso,
                "metadata_json": _accepted_review_metadata(
                    memory,
                    confirmed_at=now_iso,
                    extra={"provenance": validated_provenance} if validated_provenance is not None else None,
                ),
            }
            if title is not None:
                patch["title"] = title
            if body is not None:
                canonical_text = _canonical_text_from_body(body)
                patch["value"] = body
                patch["canonical_text"] = canonical_text
                patch["summary"] = (
                    canonical_text if len(canonical_text) <= 280 else canonical_text[:277].rstrip() + "..."
                )
                if title is None:
                    patch["title"] = (
                        canonical_text if len(canonical_text) <= 120 else canonical_text[:117].rstrip() + "..."
                    )
            if confidence is not None:
                patch["confidence"] = confidence
            # The credential floor, on the row as it will be stored. This
            # handler writes the row itself, so it calls the shared check
            # here. A title-only edit is read against the stored text; derived
            # previews of the text are left out, and provenance is read by
            # value only.
            _refuse_oversized_correction(body=body, provenance=provenance)
            refuse_credential_material(
                *stored_text_fields(
                    patch.get("title", memory.get("title")),
                    patch.get("canonical_text", memory.get("canonical_text")),
                    patch.get("summary", memory.get("summary")),
                ),
                body,
                string_values(provenance),
                reason,
                error=MCPToolError,
            )
            updated = store.update_memory(memory_id=memory_id, patch=patch, actor_type=actor_type)
            if validated_provenance is not None:
                store.create_provenance_link(
                    {
                        "target_type": "memory",
                        "target_id": memory_id,
                        "source_id": validated_provenance["source_id"],
                        "source_chunk_id": validated_provenance["source_chunk_id"],
                        "quote": validated_provenance["quote"] or str(updated.get("canonical_text") or ""),
                        "evidence_role": validated_provenance["evidence_role"],
                        "confidence": validated_provenance["confidence"],
                    },
                    actor_type=actor_type,
                )
            _vnext_review_revision(
                store,
                previous=memory,
                updated=updated,
                revision_type="edited",
                action_label="edit-and-approve",
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            memory_service.refresh_memory_derived_state(updated, identity=identity, stage="mcp_review_edit_and_approve")
        else:  # supersede
            replacement_title = _parse_optional_text(arguments, "replacement_title")
            replacement_body = _parse_optional_json_object(arguments, "replacement_body")
            replacement_provenance = _parse_optional_json_object(arguments, "replacement_provenance")
            replacement_confidence = _parse_optional_float(arguments, "replacement_confidence")
            if replacement_title is None and replacement_body is None:
                raise MCPToolError("supersede-existing requires replacement_title or replacement_body")
            canonical_text = (
                _canonical_text_from_body(replacement_body)
                if replacement_body is not None
                else cast(str, replacement_title)
            )
            validated_replacement_provenance = (
                _validated_review_provenance(
                    store,
                    replacement_provenance,
                    fallback_confidence=replacement_confidence,
                )
                if replacement_provenance is not None
                else None
            )
            # The supersession pointer is a first-class column
            # (memories.supersedes / memories.superseded_by, migration
            # 20260704_0077); the metadata_json copies below stay for
            # backward compatibility.
            replacement_metadata = _accepted_review_metadata(
                memory,
                confirmed_at=now_iso,
                extra={"supersedes": memory_id, "correction_reason": reason},
            )
            if validated_replacement_provenance is not None:
                replacement_metadata["replacement_provenance"] = validated_replacement_provenance
            # The credential floor, on the new row as it will be stored. The
            # superseded row keeps its text, so it needs no second call.
            _refuse_oversized_correction(
                replacement_body=replacement_body, replacement_provenance=replacement_provenance
            )
            refuse_credential_material(
                *stored_text_fields(replacement_title or canonical_text[:120], canonical_text, canonical_text[:280]),
                replacement_body,
                string_values(replacement_provenance),
                reason,
                error=MCPToolError,
            )
            replacement_object = store.create_memory(
                {
                    "memory_key": f"vnext.correction.supersede.{uuid4().hex[:16]}",
                    "value": replacement_body if replacement_body is not None else {"text": canonical_text},
                    "status": "active",
                    "supersedes": memory_id,
                    "project_id": memory.get("project_id"),
                    "created_by_agent_id": actor_id or memory.get("created_by_agent_id"),
                    "run_id": identity.agent_run_id if identity is not None else memory.get("run_id"),
                    "memory_type": memory.get("memory_type") or "semantic",
                    "confidence": replacement_confidence,
                    "title": replacement_title or canonical_text[:120],
                    "canonical_text": canonical_text,
                    "summary": canonical_text[:280],
                    "domain": memory.get("domain") or "unknown",
                    "sensitivity": memory.get("sensitivity") or "unknown",
                    "last_reviewed_at": now_iso,
                    "last_confirmed_at": now_iso,
                    "confirmation_status": "confirmed",
                    "metadata_json": replacement_metadata,
                },
                actor_type=actor_type,
            )
            replacement_id = str(replacement_object["id"])
            memory_service.require_valid_supersession_successor(replacement_object)
            if validated_replacement_provenance is not None:
                store.create_provenance_link(
                    {
                        "target_type": "memory",
                        "target_id": replacement_id,
                        "source_id": validated_replacement_provenance["source_id"],
                        "source_chunk_id": validated_replacement_provenance["source_chunk_id"],
                        "quote": validated_replacement_provenance["quote"] or canonical_text,
                        "evidence_role": validated_replacement_provenance["evidence_role"],
                        "confidence": validated_replacement_provenance["confidence"],
                    },
                    actor_type=actor_type,
                )
            existing_metadata = _retired_review_metadata(memory, outcome="superseded")
            updated = store.update_memory(
                memory_id=memory_id,
                patch={
                    "status": "superseded",
                    "superseded_by": replacement_id,
                    "last_reviewed_at": now_iso,
                    "metadata_json": {**existing_metadata, "superseded_by": replacement_id},
                },
                actor_type=actor_type,
            )
            _vnext_review_revision(
                store,
                previous=memory,
                updated=updated,
                revision_type="superseded",
                action_label="supersede-existing",
                reason=reason,
                metadata={"superseded_by": replacement_id},
                actor_type=actor_type,
                actor_id=actor_id,
            )
            store.append_revision(
                {
                    "memory_id": replacement_id,
                    "memory_key": str(replacement_object["memory_key"]),
                    "new_value": replacement_object.get("value"),
                    "source_event_ids": replacement_object.get("source_event_ids"),
                    "revision_type": "created",
                    "action": "memory_correct_supersede-existing",
                    "text_after": canonical_text,
                    "reason": reason or "alice_memory_correct action: supersede-existing",
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "metadata_json": {"action": "supersede-existing", "supersedes": memory_id},
                },
                actor_type=actor_type,
            )
            memory_service.refresh_memory_derived_state(
                replacement_object,
                identity=identity,
                stage="mcp_review_supersede_replacement",
            )
            event_payload["replacement_memory_id"] = replacement_id

        append_event(
            store,
            event_type="memory.reviewed",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=memory_id,
            payload=event_payload,
        )

    _persist_vnext_deferred_embedding_inputs(
        context,
        memory_service.deferred_embedding_inputs,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return _json_object(
        {
            "review_action": {
                "requested_action": requested_action,
                "resolved_action": resolved_action,
                "memory_id": memory_id,
            },
            "memory": updated,
            "replacement_object": replacement_object,
            "mode": "vnext",
            **({"rationale_withheld": rationale_withheld} if resolved_action == "delete" else {}),
        }
    )


def _review_queue_payload(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
    *,
    default_status: str,
) -> JsonObject:
    continuity_object_id = _parse_review_item_id(arguments, required=False)
    if continuity_object_id is not None:
        with _store_context(context) as store:
            detail_payload = get_continuity_review_detail(
                store,
                user_id=context.user_id,
                continuity_object_id=continuity_object_id,
            )
        return _json_object(
            {
                "mode": "detail",
                "review": detail_payload["review"],
            }
        )

    status = _parse_review_status(arguments, default=default_status)
    limit = _parse_int(
        arguments,
        key="limit",
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        minimum=1,
        maximum=MAX_CONTINUITY_REVIEW_LIMIT,
    )

    with _store_context(context) as store:
        queue_payload = list_continuity_review_queue(
            store,
            user_id=context.user_id,
            request=ContinuityReviewQueueQueryInput(
                status=cast("ContinuityReviewStatusFilter", status),
                limit=limit,
            ),
        )
    summary_payload: dict[str, object] = dict(queue_payload["summary"])
    summary_payload["order"] = list(CONTINUITY_REVIEW_QUEUE_ORDER)
    return _json_object(
        {
            "mode": "queue",
            "items": queue_payload["items"],
            "summary": summary_payload,
        }
    )


def _handle_alice_review_queue(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    return _review_queue_payload(
        context,
        arguments,
        default_status="pending_review",
    )


def _handle_alice_memory_review(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    return _vnext_memory_review(context, arguments)


def _review_apply_payload(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
    *,
    allow_legacy_actions: bool,
    include_action_resolution: bool,
) -> JsonObject:
    requested_action = _parse_required_text(arguments, "action")
    resolved_action = _resolve_review_apply_action(
        requested_action,
        allow_legacy=allow_legacy_actions,
    )
    continuity_object_id = cast(UUID, _parse_review_item_id(arguments, required=True))

    with _store_context(context) as store:
        payload = apply_continuity_correction(
            store,
            user_id=context.user_id,
            continuity_object_id=continuity_object_id,
            request=ContinuityCorrectionInput(
                action=cast("ContinuityCorrectionAction", resolved_action),
                reason=_parse_optional_text(arguments, "reason"),
                title=_parse_optional_text(arguments, "title"),
                body=_parse_optional_json_object(arguments, "body"),
                provenance=_parse_optional_json_object(arguments, "provenance"),
                confidence=_parse_optional_float(arguments, "confidence"),
                replacement_title=_parse_optional_text(arguments, "replacement_title"),
                replacement_body=_parse_optional_json_object(arguments, "replacement_body"),
                replacement_provenance=_parse_optional_json_object(arguments, "replacement_provenance"),
                replacement_confidence=_parse_optional_float(arguments, "replacement_confidence"),
            ),
        )

    if not include_action_resolution:
        return _json_object(payload)
    return _json_object(
        {
            "review_action": {
                "requested_action": requested_action,
                "resolved_action": resolved_action,
            },
            **payload,
        }
    )


def _handle_alice_review_apply(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    return _review_apply_payload(
        context,
        arguments,
        allow_legacy_actions=True,
        include_action_resolution=True,
    )


def _handle_alice_memory_correct(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    return _vnext_memory_correct(context, arguments)


def _handle_alice_contradictions_detect(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    limit = _parse_int(
        arguments,
        key="limit",
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        minimum=1,
        maximum=MAX_CONTINUITY_REVIEW_LIMIT,
    )
    with _store_context(context) as store:
        return _json_object(
            sync_contradictions(
                store,
                user_id=context.user_id,
                request=ContradictionSyncInput(
                    continuity_object_id=_parse_optional_uuid(arguments, "continuity_object_id"),
                    limit=limit,
                ),
            ),
        )


def _handle_alice_contradictions_list(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    contradiction_case_id = _parse_optional_uuid(arguments, "contradiction_case_id")
    if contradiction_case_id is not None:
        with _store_context(context) as store:
            return _json_object(
                get_contradiction_case(
                    store,
                    user_id=context.user_id,
                    contradiction_case_id=contradiction_case_id,
                )
            )
    limit = _parse_int(
        arguments,
        key="limit",
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        minimum=1,
        maximum=MAX_CONTINUITY_REVIEW_LIMIT,
    )
    raw_status = _parse_optional_text(arguments, "status") or "open"
    with _store_context(context) as store:
        return _json_object(
            list_contradiction_cases(
                store,
                user_id=context.user_id,
                request=ContradictionCaseListQueryInput(
                    status=cast("ContradictionStatus", raw_status),
                    limit=limit,
                    continuity_object_id=_parse_optional_uuid(arguments, "continuity_object_id"),
                ),
            ),
        )


def _handle_alice_contradictions_resolve(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    contradiction_case_id = _parse_required_uuid(arguments, "contradiction_case_id")
    action = _parse_required_text(arguments, "action")
    with _store_context(context) as store:
        return _json_object(
            resolve_contradiction_case(
                store,
                user_id=context.user_id,
                contradiction_case_id=contradiction_case_id,
                request=ContradictionResolveInput(
                    action=cast("ContradictionResolutionAction", action),
                    note=_parse_optional_text(arguments, "note"),
                ),
            ),
        )


def _handle_alice_trust_signals(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    limit = _parse_int(
        arguments,
        key="limit",
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        minimum=1,
        maximum=MAX_CONTINUITY_REVIEW_LIMIT,
    )
    with _store_context(context) as store:
        return _json_object(
            list_trust_signals(
                store,
                user_id=context.user_id,
                request=TrustSignalListQueryInput(
                    limit=limit,
                    continuity_object_id=_parse_optional_uuid(arguments, "continuity_object_id"),
                    signal_state=cast(
                        "TrustSignalState",
                        _parse_optional_text(arguments, "signal_state") or "active",
                    ),
                    signal_type=cast("TrustSignalType | None", _parse_optional_text(arguments, "signal_type")),
                ),
            ),
        )
