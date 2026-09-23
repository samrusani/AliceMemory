from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import cast
from uuid import UUID, uuid4

from alicebot_api.continuity_capture import capture_continuity_candidates
from alicebot_api.credential_floor import refuse_credential_material
from alicebot_api.continuity_objects import (
    create_continuity_object_record,
    default_continuity_promotable,
    default_continuity_searchable,
    serialize_continuity_lifecycle_state_from_record,
)
from alicebot_api.continuity_review import apply_continuity_correction
from alicebot_api.contracts import (
    CONTINUITY_CAPTURE_COMMIT_MODES,
    CONTINUITY_CAPTURE_ASSIST_AUTOSAVE_TYPES,
    CONTINUITY_CAPTURE_REVIEW_REQUIRED_TYPES,
    MEMORY_OPERATION_POLICY_ACTIONS,
    MEMORY_OPERATION_STATUSES,
    MEMORY_OPERATION_TYPES,
    ContinuityCaptureCandidatesInput,
    ContinuityCorrectionInput,
    ContinuityObjectType,
    ContinuityObjectRecord,
    ContinuityReviewObjectRecord,
    MemoryOperationCandidateGenerateResponse,
    MemoryOperationCandidateGenerateSummary,
    MemoryOperationCandidateListResponse,
    MemoryOperationCandidateRecord,
    MemoryOperationCommitInput,
    MemoryOperationCommitResponse,
    MemoryOperationCommitSummary,
    MemoryOperationGenerateInput,
    MemoryOperationListInput,
    MemoryOperationListResponse,
    MemoryOperationRecord,
    MemoryOperationPolicyAction,
    MemoryOperationStatus,
    MemoryOperationType,
)
from alicebot_api.store import (
    ContinuityObjectRow,
    ContinuityStore,
    JsonObject,
    MemoryOperationCandidateRow,
    MemoryOperationRow,
)


class MemoryMutationValidationError(ValueError):
    """Raised when a memory mutation request is invalid."""


_REPLACEABLE_OBJECT_TYPES = {"MemoryFact", "Decision", "Commitment", "WaitingFor", "Blocker", "NextAction"}
_DELETE_PATTERNS = (
    "delete",
    "remove",
    "forget this",
    "ignore this",
    "no longer true",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def _serialize_review_object(record: ContinuityObjectRow) -> ContinuityReviewObjectRecord:
    return {
        "id": str(record["id"]),
        "capture_event_id": str(record["capture_event_id"]),
        "object_type": cast(ContinuityObjectType, record["object_type"]),
        "status": cast(str, record["status"]),
        "lifecycle": serialize_continuity_lifecycle_state_from_record(record),
        "title": str(record["title"]),
        "body": cast(JsonObject, record["body"]),
        "provenance": cast(JsonObject, record["provenance"]),
        "confidence": float(record["confidence"]),
        "last_confirmed_at": (
            None if record["last_confirmed_at"] is None else record["last_confirmed_at"].isoformat()
        ),
        "supersedes_object_id": (
            None if record["supersedes_object_id"] is None else str(record["supersedes_object_id"])
        ),
        "superseded_by_object_id": (
            None if record["superseded_by_object_id"] is None else str(record["superseded_by_object_id"])
        ),
        "created_at": record["created_at"].isoformat(),
        "updated_at": record["updated_at"].isoformat(),
    }


def _snapshot(record: ContinuityObjectRow | None) -> JsonObject:
    if record is None:
        return {}
    return {
        "id": str(record["id"]),
        "capture_event_id": str(record["capture_event_id"]),
        "object_type": str(record["object_type"]),
        "status": str(record["status"]),
        "is_preserved": bool(record["is_preserved"]),
        "is_searchable": bool(record["is_searchable"]),
        "is_promotable": bool(record["is_promotable"]),
        "title": str(record["title"]),
        "body": cast(JsonObject, record["body"]),
        "provenance": cast(JsonObject, record["provenance"]),
        "confidence": float(record["confidence"]),
        "last_confirmed_at": (
            None if record["last_confirmed_at"] is None else record["last_confirmed_at"].isoformat()
        ),
        "supersedes_object_id": (
            None if record["supersedes_object_id"] is None else str(record["supersedes_object_id"])
        ),
        "superseded_by_object_id": (
            None if record["superseded_by_object_id"] is None else str(record["superseded_by_object_id"])
        ),
    }


def _serialize_memory_operation_candidate(row: MemoryOperationCandidateRow) -> MemoryOperationCandidateRecord:
    return {
        "id": str(row["id"]),
        "sync_fingerprint": str(row["sync_fingerprint"]),
        "source_kind": str(row["source_kind"]),
        "source_candidate_id": str(row["source_candidate_id"]),
        "source_candidate_type": str(row["source_candidate_type"]),
        "candidate_payload": cast(JsonObject, row["candidate_payload"]),
        "source_scope": cast(JsonObject, row["source_scope"]),
        "operation_type": cast(MemoryOperationType, row["operation_type"]),
        "operation_reason": str(row["operation_reason"]),
        "policy_action": cast(MemoryOperationPolicyAction, row["policy_action"]),
        "policy_reason": str(row["policy_reason"]),
        "target_continuity_object_id": (
            None if row["target_continuity_object_id"] is None else str(row["target_continuity_object_id"])
        ),
        "target_snapshot": cast(JsonObject, row["target_snapshot"]),
        "applied_operation_id": None if row["applied_operation_id"] is None else str(row["applied_operation_id"]),
        "created_at": row["created_at"].isoformat(),
        "applied_at": None if row["applied_at"] is None else row["applied_at"].isoformat(),
    }


def _serialize_memory_operation(row: MemoryOperationRow) -> MemoryOperationRecord:
    return {
        "id": str(row["id"]),
        "candidate_id": str(row["candidate_id"]),
        "operation_type": cast(MemoryOperationType, row["operation_type"]),
        "status": cast(MemoryOperationStatus, row["status"]),
        "sync_fingerprint": str(row["sync_fingerprint"]),
        "target_continuity_object_id": (
            None if row["target_continuity_object_id"] is None else str(row["target_continuity_object_id"])
        ),
        "resulting_continuity_object_id": (
            None if row["resulting_continuity_object_id"] is None else str(row["resulting_continuity_object_id"])
        ),
        "correction_event_id": None if row["correction_event_id"] is None else str(row["correction_event_id"]),
        "before_snapshot": cast(JsonObject, row["before_snapshot"]),
        "after_snapshot": cast(JsonObject, row["after_snapshot"]),
        "details": cast(JsonObject, row["details"]),
        "created_at": row["created_at"].isoformat(),
    }


def _title_for_object_type(object_type: str, normalized_text: str) -> str:
    prefix_by_type = {
        "Decision": "Decision",
        "Commitment": "Commitment",
        "WaitingFor": "Waiting For",
        "Blocker": "Blocker",
        "NextAction": "Next Action",
        "MemoryFact": "Memory Fact",
        "Note": "Note",
    }
    prefix = prefix_by_type.get(object_type, "Note")
    return _truncate(f"{prefix}: {normalized_text}", max_length=280)


def _body_for_object_type(
    *,
    object_type: str,
    normalized_text: str,
    raw_content: str,
    explicit_signal: str | None,
) -> JsonObject:
    key_by_type = {
        "Note": "body",
        "MemoryFact": "fact_text",
        "Decision": "decision_text",
        "Commitment": "commitment_text",
        "WaitingFor": "waiting_for_text",
        "Blocker": "blocking_reason",
        "NextAction": "action_text",
    }
    payload: JsonObject = {
        key_by_type.get(object_type, "body"): normalized_text,
        "raw_content": raw_content,
    }
    payload["explicit_signal"] = explicit_signal
    return payload


def _object_type_for_candidate(candidate_type: str, candidate_object_type: object) -> str:
    if isinstance(candidate_object_type, str) and candidate_object_type != "":
        return candidate_object_type
    mapping = {
        "decision": "Decision",
        "commitment": "Commitment",
        "waiting_for": "WaitingFor",
        "blocker": "Blocker",
        "preference": "MemoryFact",
        "correction": "Note",
        "note": "Note",
        "no_op": "Note",
    }
    return mapping.get(candidate_type, "Note")


def _explicit_signal_for_candidate(candidate_type: str) -> str | None:
    mapping = {
        "decision": "decision",
        "commitment": "commitment",
        "waiting_for": "waiting_for",
        "blocker": "blocker",
        "preference": "remember_this",
        "note": "note",
    }
    return mapping.get(candidate_type)


def _candidate_scope(request: MemoryOperationGenerateInput) -> JsonObject:
    return {
        "session_id": request.session_id,
        "thread_id": None if request.thread_id is None else str(request.thread_id),
        "task_id": None if request.task_id is None else str(request.task_id),
        "project": request.project,
        "person": request.person,
        "target_continuity_object_id": (
            None if request.target_continuity_object_id is None else str(request.target_continuity_object_id)
        ),
    }


def _comparison_text(record: ContinuityObjectRow) -> str:
    body = cast(JsonObject, record["body"])
    object_type = str(record["object_type"])
    for key in (
        "decision_text",
        "commitment_text",
        "waiting_for_text",
        "blocking_reason",
        "action_text",
        "fact_text",
        "body",
        "raw_content",
    ):
        value = body.get(key)
        if isinstance(value, str) and value.strip() != "":
            return _normalize_text(value)
    return _normalize_text(str(record["title"]))


def _subject_key(text: str) -> str | None:
    normalized = _normalize_text(text).casefold()
    for marker in (" is ", " = ", ":"):
        if marker in normalized:
            head = normalized.split(marker, 1)[0].strip()
            return head or None
    return None


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.casefold()) if len(token) >= 3}


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = _tokenize(left)
    right_tokens = _tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def _scope_matches(record: ContinuityObjectRow, scope: JsonObject) -> bool:
    provenance = cast(JsonObject, record["provenance"])
    for key in ("thread_id", "task_id", "project", "person"):
        expected = scope.get(key)
        if expected is None:
            continue
        if provenance.get(key) != expected:
            return False
    return True


def _select_target_record(
    store: ContinuityStore,
    *,
    candidate_payload: JsonObject,
    scope: JsonObject,
    target_continuity_object_id: UUID | None,
) -> ContinuityObjectRow | None:
    if target_continuity_object_id is not None:
        return store.get_continuity_object_optional(target_continuity_object_id)

    normalized_text = _normalize_text(cast(str, candidate_payload.get("normalized_text")))
    candidate_type = str(candidate_payload.get("candidate_type", ""))
    candidate_object_type = _object_type_for_candidate(candidate_type, candidate_payload.get("object_type"))
    target_subject = _subject_key(normalized_text)
    target_candidates: list[tuple[float, ContinuityObjectRow]] = []

    for row in store.list_continuity_recall_candidates():
        if row["status"] not in {"active", "stale"}:
            continue
        continuity_row = store.get_continuity_object_optional(row["id"])
        if continuity_row is None:
            continue
        if not _scope_matches(continuity_row, scope):
            continue
        if candidate_type != "correction" and str(continuity_row["object_type"]) != candidate_object_type:
            continue

        existing_text = _comparison_text(continuity_row)
        overlap = _lexical_overlap(normalized_text, existing_text)
        if normalized_text.casefold() == existing_text.casefold():
            return continuity_row
        if target_subject is not None and target_subject == _subject_key(existing_text):
            overlap = max(overlap, 0.85)
        target_candidates.append((overlap, continuity_row))

    if not target_candidates:
        return None

    target_candidates.sort(key=lambda item: (item[0], item[1]["updated_at"], item[1]["created_at"]), reverse=True)
    best_score, best_row = target_candidates[0]
    if candidate_type == "correction" and best_score >= 0.2:
        return best_row
    if best_score >= 0.5:
        return best_row
    return None


def _classify_operation(
    *,
    candidate_payload: JsonObject,
    target_record: ContinuityObjectRow | None,
) -> tuple[MemoryOperationType, str]:
    candidate_type = str(candidate_payload.get("candidate_type", ""))
    normalized_text = _normalize_text(cast(str, candidate_payload.get("normalized_text")))

    if candidate_type == "no_op":
        return "NOOP", "ack_or_irrelevant_turn"

    if target_record is None:
        return "ADD", "no_current_target_match"

    existing_text = _comparison_text(target_record)
    if normalized_text.casefold() == existing_text.casefold():
        return "NOOP", "exact_current_match"

    lowered_text = normalized_text.casefold()
    if candidate_type == "correction":
        if any(marker in lowered_text for marker in _DELETE_PATTERNS):
            return "DELETE", "explicit_correction_delete_request"
        if str(target_record["object_type"]) in _REPLACEABLE_OBJECT_TYPES:
            return "SUPERSEDE", "explicit_correction_replaces_current_fact"
        return "UPDATE", "explicit_correction_updates_current_note"

    candidate_subject = _subject_key(normalized_text)
    existing_subject = _subject_key(existing_text)
    if candidate_subject is not None and candidate_subject == existing_subject:
        if str(target_record["object_type"]) in _REPLACEABLE_OBJECT_TYPES:
            return "SUPERSEDE", "subject_key_match_changed_fact"
        return "UPDATE", "subject_key_match_changed_note"

    if _lexical_overlap(normalized_text, existing_text) >= 0.72:
        return "UPDATE", "high_overlap_refinement"

    return "ADD", "distinct_candidate_requires_add"


def _resolve_policy_action(
    *,
    candidate_payload: JsonObject,
    operation_type: MemoryOperationType,
    mode: str,
) -> tuple[MemoryOperationPolicyAction, str]:
    if operation_type == "NOOP":
        return "skip", "noop_candidate"

    if operation_type == "DELETE":
        return "review_required", "destructive_operations_require_review"

    candidate_type = str(candidate_payload.get("candidate_type", ""))
    explicit = bool(candidate_payload.get("explicit", False))
    raw_confidence = candidate_payload.get("confidence", 0.0)
    confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, (str, int, float))
        else 0.0
    )

    if confidence < 0.9:
        return "review_required", "low_confidence_requires_review"
    if candidate_type in CONTINUITY_CAPTURE_REVIEW_REQUIRED_TYPES:
        return "review_required", "candidate_type_requires_review"
    if mode == "manual":
        return "review_required", "manual_mode_requires_review"
    if mode == "assist":
        if explicit and candidate_type in CONTINUITY_CAPTURE_ASSIST_AUTOSAVE_TYPES:
            return "auto_apply", "assist_mode_allowlist_explicit_high_confidence"
        return "review_required", "assist_mode_review_gate"
    if mode == "auto":
        if candidate_type in CONTINUITY_CAPTURE_ASSIST_AUTOSAVE_TYPES:
            return "auto_apply", "auto_mode_allowlist_high_confidence"
        return "review_required", "auto_mode_review_gate"
    return "review_required", "unknown_mode_review_gate"


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > 100:
        raise MemoryMutationValidationError("limit must be between 1 and 100")


def _validate_mode(mode: str) -> str:
    normalized = _normalize_text(mode).lower()
    if normalized not in CONTINUITY_CAPTURE_COMMIT_MODES:
        allowed = ", ".join(CONTINUITY_CAPTURE_COMMIT_MODES)
        raise MemoryMutationValidationError(f"mode must be one of: {allowed}")
    return normalized


def generate_memory_operation_candidates(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: MemoryOperationGenerateInput,
) -> MemoryOperationCandidateGenerateResponse:
    mode = _validate_mode(request.mode)
    extracted = capture_continuity_candidates(
        store,
        user_id=user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content=request.user_content,
            assistant_content=request.assistant_content,
            session_id=request.session_id,
            source_kind=request.source_kind,
        ),
    )

    scope = _candidate_scope(request)
    items: list[MemoryOperationCandidateRecord] = []

    for candidate in extracted["candidates"]:
        source_candidate_id = str(candidate["candidate_id"])
        sync_fingerprint = _normalize_text(request.sync_fingerprint) or f"mutation:{source_candidate_id}"
        existing = store.get_memory_operation_candidate_by_sync_source_optional(
            sync_fingerprint=sync_fingerprint,
            source_candidate_id=source_candidate_id,
        )
        if existing is not None:
            items.append(_serialize_memory_operation_candidate(existing))
            continue

        target_record = _select_target_record(
            store,
            candidate_payload=cast(JsonObject, candidate),
            scope=scope,
            target_continuity_object_id=request.target_continuity_object_id,
        )
        operation_type, operation_reason = _classify_operation(
            candidate_payload=cast(JsonObject, candidate),
            target_record=target_record,
        )
        policy_action, policy_reason = _resolve_policy_action(
            candidate_payload=cast(JsonObject, candidate),
            operation_type=operation_type,
            mode=mode,
        )
        # The credential floor, before the candidate row persists the text.
        # Until 2026-09-22 this path never consulted it, so a GitHub token in
        # "Decision: ..." became a stored candidate and, on commit, an active
        # Decision read back through the brief and recall. Raising here rolls
        # the whole request back.
        refuse_credential_material(
            candidate["normalized_text"],
            candidate["evidence_snippet"],
            error=MemoryMutationValidationError,
        )
        created = store.create_memory_operation_candidate(
            sync_fingerprint=sync_fingerprint,
            source_kind=request.source_kind,
            source_candidate_id=source_candidate_id,
            source_candidate_type=str(candidate["candidate_type"]),
            candidate_payload=cast(JsonObject, candidate),
            source_scope=scope,
            operation_type=operation_type,
            operation_reason=operation_reason,
            policy_action=policy_action,
            policy_reason=policy_reason,
            target_continuity_object_id=None if target_record is None else target_record["id"],
            target_snapshot=_snapshot(target_record),
        )
        items.append(_serialize_memory_operation_candidate(created))

    summary: MemoryOperationCandidateGenerateSummary = {
        "candidate_count": len(items),
        "auto_apply_count": sum(1 for item in items if item["policy_action"] == "auto_apply"),
        "review_required_count": sum(1 for item in items if item["policy_action"] == "review_required"),
        "noop_count": sum(1 for item in items if item["operation_type"] == "NOOP"),
        "operation_types": sorted({item["operation_type"] for item in items}),
    }
    return {"items": items, "summary": summary}


def list_memory_operation_candidates(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: MemoryOperationListInput,
) -> MemoryOperationCandidateListResponse:
    del user_id
    _validate_limit(request.limit)
    if request.policy_action is not None and request.policy_action not in MEMORY_OPERATION_POLICY_ACTIONS:
        raise MemoryMutationValidationError("policy_action must be auto_apply, review_required, or skip")
    if request.operation_type is not None and request.operation_type not in MEMORY_OPERATION_TYPES:
        raise MemoryMutationValidationError("operation_type must be ADD, UPDATE, SUPERSEDE, DELETE, or NOOP")

    rows = store.list_memory_operation_candidates(
        limit=request.limit,
        policy_action=request.policy_action,
        operation_type=request.operation_type,
        sync_fingerprint=_normalize_text(request.sync_fingerprint) or None,
    )
    total_count = store.count_memory_operation_candidates(
        policy_action=request.policy_action,
        operation_type=request.operation_type,
        sync_fingerprint=_normalize_text(request.sync_fingerprint) or None,
    )
    return {
        "items": [_serialize_memory_operation_candidate(row) for row in rows],
        "summary": {
            "limit": request.limit,
            "returned_count": len(rows),
            "total_count": total_count,
            "policy_action": request.policy_action,
            "operation_type": request.operation_type,
            "sync_fingerprint": _normalize_text(request.sync_fingerprint) or None,
        },
    }


def list_memory_operations(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: MemoryOperationListInput,
) -> MemoryOperationListResponse:
    del user_id
    _validate_limit(request.limit)
    rows = store.list_memory_operations(
        limit=request.limit,
        sync_fingerprint=_normalize_text(request.sync_fingerprint) or None,
    )
    total_count = store.count_memory_operations(
        sync_fingerprint=_normalize_text(request.sync_fingerprint) or None,
    )
    return {
        "items": [_serialize_memory_operation(row) for row in rows],
        "summary": {
            "limit": request.limit,
            "returned_count": len(rows),
            "total_count": total_count,
            "policy_action": None,
            "operation_type": None,
            "sync_fingerprint": _normalize_text(request.sync_fingerprint) or None,
        },
    }


def _replacement_values(
    *,
    candidate_payload: JsonObject,
    target_record: ContinuityObjectRow | None,
    candidate_row: MemoryOperationCandidateRow,
    operation_id: UUID,
) -> tuple[str, JsonObject, JsonObject, float]:
    candidate_type = str(candidate_payload.get("candidate_type", ""))
    candidate_object_type = _object_type_for_candidate(candidate_type, candidate_payload.get("object_type"))
    normalized_text = _normalize_text(cast(str, candidate_payload.get("normalized_text")))
    object_type = candidate_object_type if target_record is None else str(target_record["object_type"])
    title = _title_for_object_type(object_type, normalized_text)
    provenance: JsonObject = {}
    if target_record is not None:
        provenance.update(cast(JsonObject, target_record["provenance"]))
    else:
        source_scope = cast(JsonObject, candidate_row["source_scope"])
        for key in ("session_id", "thread_id", "task_id", "project", "person"):
            value = source_scope.get(key)
            if value is not None:
                provenance[key] = value
    provenance.update(
        {
            "memory_operation_candidate_id": str(candidate_row["id"]),
            "memory_operation_id": str(operation_id),
            "sync_fingerprint": str(candidate_row["sync_fingerprint"]),
            "source_kind": str(candidate_row["source_kind"]),
            "source_candidate_id": str(candidate_row["source_candidate_id"]),
            "source_candidate_type": str(candidate_row["source_candidate_type"]),
        }
    )
    body = _body_for_object_type(
        object_type=object_type,
        normalized_text=normalized_text,
        raw_content=normalized_text,
        explicit_signal=_explicit_signal_for_candidate(candidate_type),
    )
    raw_confidence = candidate_payload.get("confidence", 0.0)
    confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, (str, int, float))
        else 0.0
    )
    return title, body, provenance, confidence


def _apply_add_operation(
    store: ContinuityStore,
    *,
    user_id: UUID,
    candidate_row: MemoryOperationCandidateRow,
    operation_id: UUID,
) -> tuple[UUID, JsonObject]:
    candidate_payload = cast(JsonObject, candidate_row["candidate_payload"])
    candidate_type = str(candidate_payload.get("candidate_type", ""))
    object_type = _object_type_for_candidate(candidate_type, candidate_payload.get("object_type"))
    title, body, provenance, confidence = _replacement_values(
        candidate_payload=candidate_payload,
        target_record=None,
        candidate_row=candidate_row,
        operation_id=operation_id,
    )
    capture_event = store.create_continuity_capture_event(
        raw_content=_normalize_text(cast(str, candidate_payload.get("normalized_text"))),
        explicit_signal=_explicit_signal_for_candidate(candidate_type),
        admission_posture="DERIVED" if bool(candidate_payload.get("explicit", False)) else "TRIAGE",
        admission_reason=_truncate(str(candidate_payload.get("admission_reason", "memory_operation_add")), max_length=200),
    )
    created = create_continuity_object_record(
        store,
        user_id=user_id,
        capture_event_id=capture_event["id"],
        object_type=object_type,
        status="active",
        title=title,
        body=body,
        provenance=provenance,
        confidence=confidence,
        is_searchable=default_continuity_searchable(object_type),
        is_promotable=default_continuity_promotable(object_type),
    )
    return UUID(created["id"]), {
        "capture_event_id": str(capture_event["id"]),
        "continuity_object": cast(JsonObject, created),
    }


def commit_memory_operations(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: MemoryOperationCommitInput,
) -> MemoryOperationCommitResponse:
    if not request.candidate_ids and _normalize_text(request.sync_fingerprint) == "":
        raise MemoryMutationValidationError("candidate_ids or sync_fingerprint is required")

    candidate_rows: list[MemoryOperationCandidateRow] = []
    for candidate_id in request.candidate_ids:
        row = store.get_memory_operation_candidate_optional(candidate_id)
        if row is None:
            raise MemoryMutationValidationError(f"memory operation candidate {candidate_id} was not found")
        candidate_rows.append(row)

    if _normalize_text(request.sync_fingerprint) != "":
        sync_rows = store.list_memory_operation_candidates(
            limit=100,
            sync_fingerprint=_normalize_text(request.sync_fingerprint),
        )
        if request.candidate_ids:
            seen_ids = {row["id"] for row in candidate_rows}
            candidate_rows.extend(row for row in sync_rows if row["id"] not in seen_ids)
        else:
            candidate_rows = sync_rows

    operations: list[MemoryOperationRecord] = []
    serialized_candidates: list[MemoryOperationCandidateRecord] = []
    summary: MemoryOperationCommitSummary = {
        "requested_count": len(candidate_rows),
        "applied_count": 0,
        "no_op_count": 0,
        "skipped_count": 0,
        "duplicate_count": 0,
        "operation_types": [],
    }
    operation_types: set[str] = set()

    for candidate_row in candidate_rows:
        if candidate_row["applied_operation_id"] is not None:
            existing_operation = store.get_memory_operation_optional(candidate_row["applied_operation_id"])
            if existing_operation is not None:
                operations.append(_serialize_memory_operation(existing_operation))
            serialized_candidates.append(_serialize_memory_operation_candidate(candidate_row))
            summary["duplicate_count"] += 1
            if existing_operation is not None:
                operation_types.add(str(existing_operation["operation_type"]))
            continue

        if candidate_row["policy_action"] == "review_required" and not request.include_review_required:
            serialized_candidates.append(_serialize_memory_operation_candidate(candidate_row))
            summary["skipped_count"] += 1
            continue

        candidate_payload = cast(JsonObject, candidate_row["candidate_payload"])
        target_record = None
        if candidate_row["target_continuity_object_id"] is not None:
            target_record = store.get_continuity_object_optional(candidate_row["target_continuity_object_id"])

        operation_id = uuid4()

        status = "applied"
        resulting_continuity_object_id: UUID | None = None
        correction_event_id: UUID | None = None
        after_snapshot: JsonObject = {}
        details: JsonObject = {}
        operation_type = str(candidate_row["operation_type"])

        if operation_type == "NOOP":
            status = "no_op"
            after_snapshot = cast(JsonObject, candidate_row["target_snapshot"])
            details = {"reason": str(candidate_row["operation_reason"])}
            summary["no_op_count"] += 1
        elif operation_type == "ADD":
            resulting_continuity_object_id, details = _apply_add_operation(
                store,
                user_id=user_id,
                candidate_row=candidate_row,
                operation_id=operation_id,
            )
            created_row = store.get_continuity_object_optional(resulting_continuity_object_id)
            after_snapshot = _snapshot(created_row)
            summary["applied_count"] += 1
        elif target_record is None:
            status = "skipped"
            after_snapshot = {}
            details = {"reason": "target_continuity_object_missing"}
            summary["skipped_count"] += 1
        elif operation_type == "UPDATE":
            title, body, provenance, confidence = _replacement_values(
                candidate_payload=candidate_payload,
                target_record=target_record,
                candidate_row=candidate_row,
                operation_id=operation_id,
            )
            applied = apply_continuity_correction(
                store,
                user_id=user_id,
                continuity_object_id=target_record["id"],
                request=ContinuityCorrectionInput(
                    action="edit",
                    title=title,
                    body=body,
                    provenance=provenance,
                    confidence=confidence,
                ),
            )
            correction_event_id = UUID(applied["correction_event"]["id"])
            resulting_continuity_object_id = target_record["id"]
            updated_row = store.get_continuity_object_optional(target_record["id"])
            after_snapshot = _snapshot(updated_row)
            details = {
                "continuity_object": cast(JsonObject, applied["continuity_object"]),
            }
            summary["applied_count"] += 1
        elif operation_type == "SUPERSEDE":
            title, body, provenance, confidence = _replacement_values(
                candidate_payload=candidate_payload,
                target_record=target_record,
                candidate_row=candidate_row,
                operation_id=operation_id,
            )
            applied = apply_continuity_correction(
                store,
                user_id=user_id,
                continuity_object_id=target_record["id"],
                request=ContinuityCorrectionInput(
                    action="supersede",
                    reason="Auto-applied memory mutation supersession",
                    replacement_title=title,
                    replacement_body=body,
                    replacement_provenance=provenance,
                    replacement_confidence=confidence,
                ),
            )
            correction_event_id = UUID(applied["correction_event"]["id"])
            replacement_object = cast(ContinuityReviewObjectRecord, applied["replacement_object"])
            resulting_continuity_object_id = UUID(replacement_object["id"])
            original_row = store.get_continuity_object_optional(target_record["id"])
            replacement_row = store.get_continuity_object_optional(resulting_continuity_object_id)
            after_snapshot = {
                "superseded_object": _snapshot(original_row),
                "replacement_object": _snapshot(replacement_row),
            }
            details = {
                "continuity_object": cast(JsonObject, applied["continuity_object"]),
                "replacement_object": cast(JsonObject, replacement_object),
            }
            summary["applied_count"] += 1
        elif operation_type == "DELETE":
            applied = apply_continuity_correction(
                store,
                user_id=user_id,
                continuity_object_id=target_record["id"],
                request=ContinuityCorrectionInput(
                    action="delete",
                    reason="Memory mutation delete candidate approved",
                ),
            )
            correction_event_id = UUID(applied["correction_event"]["id"])
            updated_row = store.get_continuity_object_optional(target_record["id"])
            resulting_continuity_object_id = None
            after_snapshot = _snapshot(updated_row)
            details = {
                "continuity_object": cast(JsonObject, applied["continuity_object"]),
            }
            summary["applied_count"] += 1
        else:
            raise MemoryMutationValidationError(f"unsupported operation_type {operation_type}")

        committed = store.create_memory_operation(
            operation_id=operation_id,
            candidate_id=candidate_row["id"],
            operation_type=operation_type,
            status=status,
            sync_fingerprint=str(candidate_row["sync_fingerprint"]),
            target_continuity_object_id=candidate_row["target_continuity_object_id"],
            resulting_continuity_object_id=resulting_continuity_object_id,
            correction_event_id=correction_event_id,
            before_snapshot=_snapshot(target_record),
            after_snapshot=after_snapshot,
            details=details,
        )
        updated_candidate = store.update_memory_operation_candidate_application(
            candidate_id=candidate_row["id"],
            applied_operation_id=committed["id"],
            applied_at=_utcnow(),
        )
        if updated_candidate is None:
            raise MemoryMutationValidationError(
                f"memory operation candidate {candidate_row['id']} disappeared during commit"
            )
        serialized_candidates.append(_serialize_memory_operation_candidate(updated_candidate))
        operations.append(_serialize_memory_operation(committed))
        operation_types.add(operation_type)

    summary["operation_types"] = sorted(operation_types)
    return {
        "candidates": serialized_candidates,
        "operations": operations,
        "summary": summary,
    }


__all__ = [
    "MemoryMutationValidationError",
    "commit_memory_operations",
    "generate_memory_operation_candidates",
    "list_memory_operation_candidates",
    "list_memory_operations",
]
