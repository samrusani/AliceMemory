from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from alicebot_api.continuity_contradictions import sync_contradiction_state_for_objects
from alicebot_api.continuity_explainability import build_continuity_item_explanation
from alicebot_api.continuity_objects import serialize_continuity_lifecycle_state_from_record
from alicebot_api.contracts import (
    CONTINUITY_CORRECTION_ACTIONS,
    CONTINUITY_REVIEW_QUEUE_ORDER,
    DEFAULT_CONTINUITY_REVIEW_LIMIT,
    MAX_CONTINUITY_REVIEW_LIMIT,
    ContinuityCorrectionApplyResponse,
    ContinuityCorrectionEventRecord,
    ContinuityCorrectionInput,
    ContinuityReviewDetail,
    ContinuityReviewDetailResponse,
    ContinuityReviewObjectRecord,
    ContinuityReviewQueueQueryInput,
    ContinuityReviewQueueResponse,
    ContinuityReviewStatusFilter,
    ContinuitySupersessionChain,
    isoformat_or_none,
)
from alicebot_api.credential_floor import (
    TEXT_WITHHELD_PLACEHOLDER,
    carries_credential_material,
    refuse_credential_activation,
    refuse_credential_material,
    withhold_credential_text,
)
from alicebot_api.write_bounds import MAX_CORRECTION_FIELD_CHARS, MAX_CORRECTION_TITLE_CHARS, first_oversized
from alicebot_api.store import (
    ContinuityCorrectionEventRow,
    ContinuityObjectRow,
    ContinuityStore,
    JsonObject,
)


class ContinuityReviewValidationError(ValueError):
    """Raised when a continuity review request is invalid."""


class ContinuityReviewNotFoundError(LookupError):
    """Raised when a continuity object is not visible in scope."""


_STATUS_FILTERS: dict[ContinuityReviewStatusFilter, list[str]] = {
    "correction_ready": ["active", "stale"],
    "active": ["active"],
    "stale": ["stale"],
    "superseded": ["superseded"],
    "deleted": ["deleted"],
    "all": ["active", "stale", "superseded", "deleted"],
}
_CORRECTION_ACTION_ALIASES: dict[str, str] = {
    "approve": "confirm",
    "edit-and-approve": "edit",
    "edit_and_approve": "edit",
    "reject": "delete",
    "supersede-existing": "supersede",
    "supersede_existing": "supersede",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    return normalized


def _validate_title(title: str) -> str:
    normalized = " ".join(title.split()).strip()
    if not normalized:
        raise ContinuityReviewValidationError("title must not be empty")
    if len(normalized) > 280:
        raise ContinuityReviewValidationError("title must be 280 characters or fewer")
    return normalized


def _validate_confidence(confidence: float) -> float:
    if confidence < 0.0 or confidence > 1.0:
        raise ContinuityReviewValidationError("confidence must be between 0.0 and 1.0")
    return confidence


def _serialize_review_object(
    store: ContinuityStore,
    record: ContinuityObjectRow,
) -> ContinuityReviewObjectRecord:
    return {
        "id": str(record["id"]),
        "capture_event_id": str(record["capture_event_id"]),
        "object_type": record["object_type"],  # type: ignore[typeddict-item]
        "status": record["status"],
        "lifecycle": serialize_continuity_lifecycle_state_from_record(record),
        "title": record["title"],
        "body": record["body"],
        "provenance": record["provenance"],
        "confidence": float(record["confidence"]),
        "last_confirmed_at": isoformat_or_none(record["last_confirmed_at"]),
        "supersedes_object_id": (
            None if record["supersedes_object_id"] is None else str(record["supersedes_object_id"])
        ),
        "superseded_by_object_id": (
            None if record["superseded_by_object_id"] is None else str(record["superseded_by_object_id"])
        ),
        "created_at": record["created_at"].isoformat(),
        "updated_at": record["updated_at"].isoformat(),
        "explanation": build_continuity_item_explanation(
            store,
            continuity_object_id=record["id"],
            capture_event_id=record["capture_event_id"],
            title=record["title"],
            body=record["body"],
            provenance=record["provenance"],
            status=record["status"],
            confidence=float(record["confidence"]),
            last_confirmed_at=record["last_confirmed_at"],
            supersedes_object_id=record["supersedes_object_id"],
            superseded_by_object_id=record["superseded_by_object_id"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        ),
    }


def _serialize_correction_event(record: ContinuityCorrectionEventRow) -> ContinuityCorrectionEventRecord:
    return {
        "id": str(record["id"]),
        "continuity_object_id": str(record["continuity_object_id"]),
        "action": record["action"],  # type: ignore[typeddict-item]
        "reason": record["reason"],
        "before_snapshot": record["before_snapshot"],
        "after_snapshot": record["after_snapshot"],
        "payload": record["payload"],
        "created_at": record["created_at"].isoformat(),
    }


def _snapshot(record: ContinuityObjectRow) -> JsonObject:
    return {
        "id": str(record["id"]),
        "capture_event_id": str(record["capture_event_id"]),
        "object_type": record["object_type"],
        "status": record["status"],
        "is_preserved": record["is_preserved"],
        "is_searchable": record["is_searchable"],
        "is_promotable": record["is_promotable"],
        "title": record["title"],
        "body": record["body"],
        "provenance": record["provenance"],
        "confidence": float(record["confidence"]),
        "last_confirmed_at": isoformat_or_none(record["last_confirmed_at"]),
        "supersedes_object_id": (
            None if record["supersedes_object_id"] is None else str(record["supersedes_object_id"])
        ),
        "superseded_by_object_id": (
            None if record["superseded_by_object_id"] is None else str(record["superseded_by_object_id"])
        ),
    }


def _lookup_object_or_raise(
    store: ContinuityStore,
    *,
    continuity_object_id: UUID,
) -> ContinuityObjectRow:
    record = store.get_continuity_object_optional(continuity_object_id)
    if record is None:
        raise ContinuityReviewNotFoundError(f"continuity object {continuity_object_id} was not found")
    return record


def _canonicalize_action(action: str) -> str:
    normalized = action.strip()
    if normalized in _CORRECTION_ACTION_ALIASES:
        return _CORRECTION_ACTION_ALIASES[normalized]
    return normalized


def _validate_queue_query(request: ContinuityReviewQueueQueryInput) -> list[str]:
    if request.limit < 1 or request.limit > MAX_CONTINUITY_REVIEW_LIMIT:
        raise ContinuityReviewValidationError(
            f"limit must be between 1 and {MAX_CONTINUITY_REVIEW_LIMIT}"
        )

    if request.status not in _STATUS_FILTERS:
        allowed = ", ".join(_STATUS_FILTERS)
        raise ContinuityReviewValidationError(f"status must be one of: {allowed}")

    return _STATUS_FILTERS[request.status]


def list_continuity_review_queue(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityReviewQueueQueryInput,
) -> ContinuityReviewQueueResponse:
    del user_id

    statuses = _validate_queue_query(request)
    rows = store.list_continuity_review_queue(statuses=statuses, limit=request.limit)
    total_count = store.count_continuity_review_queue(statuses=statuses)
    sync_contradiction_state_for_objects(
        store,
        continuity_object_ids=[row["id"] for row in rows],
    )

    return {
        "items": [_serialize_review_object(store, row) for row in rows],
        "summary": {
            "status": request.status,
            "limit": request.limit,
            "returned_count": len(rows),
            "total_count": total_count,
            "order": list(CONTINUITY_REVIEW_QUEUE_ORDER),
        },
    }


def get_continuity_review_detail(
    store: ContinuityStore,
    *,
    user_id: UUID,
    continuity_object_id: UUID,
) -> ContinuityReviewDetailResponse:
    del user_id

    continuity_object = _lookup_object_or_raise(
        store,
        continuity_object_id=continuity_object_id,
    )
    sync_contradiction_state_for_objects(
        store,
        continuity_object_ids=[continuity_object["id"]],
    )
    continuity_object = _lookup_object_or_raise(
        store,
        continuity_object_id=continuity_object_id,
    )

    supersedes_object = None
    if continuity_object["supersedes_object_id"] is not None:
        supersedes_object = store.get_continuity_object_optional(continuity_object["supersedes_object_id"])

    superseded_by_object = None
    if continuity_object["superseded_by_object_id"] is not None:
        superseded_by_object = store.get_continuity_object_optional(continuity_object["superseded_by_object_id"])

    correction_events = store.list_continuity_correction_events(
        continuity_object_id=continuity_object_id,
        limit=100,
    )

    supersession_chain: ContinuitySupersessionChain = {
        "supersedes": (
            None if supersedes_object is None else _serialize_review_object(store, supersedes_object)
        ),
        "superseded_by": (
            None
            if superseded_by_object is None
            else _serialize_review_object(store, superseded_by_object)
        ),
    }

    detail: ContinuityReviewDetail = {
        "continuity_object": _serialize_review_object(store, continuity_object),
        "correction_events": [_serialize_correction_event(event) for event in correction_events],
        "supersession_chain": supersession_chain,
    }

    return {
        "review": detail,
    }


def _create_correction_event(
    store: ContinuityStore,
    *,
    continuity_object_id: UUID,
    action: str,
    reason: str | None,
    before_snapshot: JsonObject,
    after_snapshot: JsonObject,
    payload: JsonObject,
) -> ContinuityCorrectionEventRow:
    return store.create_continuity_correction_event(
        continuity_object_id=continuity_object_id,
        action=action,
        reason=reason,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        payload=payload,
    )


def _refuse_credential_row(title: object, body: object, provenance: object, reason: object) -> None:
    """One call per object written: title, body (as a mapping), provenance
    with its keys, then the reason that is persisted on the correction event."""

    refuse_credential_material(
        title,
        body,
        provenance,
        reason,
        error=ContinuityReviewValidationError,
    )


def apply_continuity_correction(
    store: ContinuityStore,
    *,
    user_id: UUID,
    continuity_object_id: UUID,
    request: ContinuityCorrectionInput,
) -> ContinuityCorrectionApplyResponse:
    del user_id

    action = _canonicalize_action(request.action)
    if action not in CONTINUITY_CORRECTION_ACTIONS:
        allowed = ", ".join(CONTINUITY_CORRECTION_ACTIONS)
        raise ContinuityReviewValidationError(f"action must be one of: {allowed}")

    reason = _normalize_optional_text(request.reason)
    if reason is not None and len(reason) > 500:
        raise ContinuityReviewValidationError("reason must be 500 characters or fewer")

    # The object is looked up before anything is scanned, so a request for an
    # id that does not exist costs a lookup, not a scan of its payload. Then
    # every caller field is bounded before the credential floor reads it
    # (review finding 8: a 3 MB body cost 44 s of CPU before the lookup).
    current = _lookup_object_or_raise(store, continuity_object_id=continuity_object_id)
    for name, title in (("title", request.title), ("replacement_title", request.replacement_title)):
        if title is not None and len(title) > MAX_CORRECTION_TITLE_CHARS:
            raise ContinuityReviewValidationError(f"{name} must be {MAX_CORRECTION_TITLE_CHARS} characters or fewer")
    oversized = first_oversized(
        {
            "body": request.body,
            "provenance": request.provenance,
            "replacement_body": request.replacement_body,
            "replacement_provenance": request.replacement_provenance,
        },
        MAX_CORRECTION_FIELD_CHARS,
    )
    if oversized is not None:
        raise ContinuityReviewValidationError(
            f"{oversized} must serialize to {MAX_CORRECTION_FIELD_CHARS} characters or fewer"
        )
    before_snapshot = _snapshot(current)

    next_title = current["title"]
    next_body = current["body"]
    next_provenance = current["provenance"]
    next_confidence = float(current["confidence"])
    next_status = current["status"]
    next_last_confirmed_at = current["last_confirmed_at"]
    next_supersedes_object_id = current["supersedes_object_id"]
    next_superseded_by_object_id = current["superseded_by_object_id"]

    if action == "confirm":
        if current["status"] not in {"active", "stale"}:
            raise ContinuityReviewValidationError("confirm requires an active or stale continuity object")
        next_status = "active"
        next_last_confirmed_at = _utcnow()

    elif action == "edit":
        if current["status"] not in {"active", "stale"}:
            raise ContinuityReviewValidationError("edit requires an active or stale continuity object")

        if (
            request.title is None
            and request.body is None
            and request.provenance is None
            and request.confidence is None
        ):
            raise ContinuityReviewValidationError(
                "edit requires at least one of title, body, provenance, or confidence"
            )

        if request.title is not None:
            next_title = _validate_title(request.title)
        if request.body is not None:
            next_body = request.body
        if request.provenance is not None:
            next_provenance = request.provenance
        if request.confidence is not None:
            next_confidence = _validate_confidence(float(request.confidence))

        next_status = "active"
        next_last_confirmed_at = _utcnow()
        # The credential floor, on the object as it will be stored, once per
        # row written. Until 2026-09-22 this path, which /v1 memory operation
        # commit uses for UPDATE, never consulted it. A title-only edit is
        # read against the stored body; provenance by value only.
        _refuse_credential_row(next_title, next_body, next_provenance, reason)

    elif action == "delete":
        if current["status"] not in {"active", "stale"}:
            raise ContinuityReviewValidationError("delete requires an active or stale continuity object")
        next_status = "deleted"

    elif action == "mark_stale":
        if current["status"] != "active":
            raise ContinuityReviewValidationError("mark_stale requires an active continuity object")
        next_status = "stale"

    elif action == "supersede":
        if current["status"] not in {"active", "stale"}:
            raise ContinuityReviewValidationError("supersede requires an active or stale continuity object")

        replacement_title = _validate_title(request.replacement_title or current["title"])
        replacement_body = request.replacement_body if request.replacement_body is not None else current["body"]
        replacement_provenance = (
            request.replacement_provenance
            if request.replacement_provenance is not None
            else current["provenance"]
        )
        replacement_confidence = _validate_confidence(
            float(request.replacement_confidence)
            if request.replacement_confidence is not None
            else float(current["confidence"])
        )

        # The credential floor, on the new object as it will be stored. The
        # superseded object keeps its text, so it needs no second call.
        _refuse_credential_row(replacement_title, replacement_body, replacement_provenance, reason)

        supersede_payload: JsonObject = {
            "replacement_title": replacement_title,
            "replacement_body": replacement_body,
            "replacement_provenance": replacement_provenance,
            "replacement_confidence": replacement_confidence,
        }
        supersede_after_snapshot: JsonObject = {
            **before_snapshot,
            "status": "superseded",
            "superseded_by_object_id": None,
        }

        correction_event = _create_correction_event(
            store,
            continuity_object_id=continuity_object_id,
            action=action,
            reason=reason,
            before_snapshot=before_snapshot,
            after_snapshot=supersede_after_snapshot,
            payload=supersede_payload,
        )

        capture_event = store.create_continuity_capture_event(
            raw_content=replacement_title,
            explicit_signal=None,
            admission_posture="DERIVED",
            admission_reason="correction_supersede",
        )

        replacement_provenance_payload = {
            **replacement_provenance,
            "supersedes_object_id": str(current["id"]),
            "correction_action": "supersede",
            "capture_event_id": str(capture_event["id"]),
            "source_kind": "continuity_correction_event",
        }

        replacement_object = store.create_continuity_object(
            capture_event_id=capture_event["id"],
            object_type=current["object_type"],
            status="active",
            is_preserved=current["is_preserved"],
            is_searchable=current["is_searchable"],
            is_promotable=current["is_promotable"],
            title=replacement_title,
            body=replacement_body,
            provenance=replacement_provenance_payload,
            confidence=replacement_confidence,
            last_confirmed_at=_utcnow(),
            supersedes_object_id=current["id"],
            superseded_by_object_id=None,
        )
        list_evidence = getattr(store, "list_continuity_object_evidence", None)
        create_evidence_link = getattr(store, "create_continuity_object_evidence_link", None)
        if callable(list_evidence) and callable(create_evidence_link):
            for evidence_link in list_evidence(current["id"]):
                create_evidence_link(
                    continuity_object_id=replacement_object["id"],
                    artifact_id=evidence_link["artifact_id"],
                    artifact_copy_id=evidence_link["artifact_copy_id"],
                    artifact_segment_id=evidence_link["artifact_segment_id"],
                    relationship=evidence_link["relationship"],
                )

        updated = store.update_continuity_object_optional(
            continuity_object_id=continuity_object_id,
            status="superseded",
            is_preserved=current["is_preserved"],
            is_searchable=current["is_searchable"],
            is_promotable=current["is_promotable"],
            title=current["title"],
            body=current["body"],
            provenance=current["provenance"],
            confidence=float(current["confidence"]),
            last_confirmed_at=current["last_confirmed_at"],
            supersedes_object_id=current["supersedes_object_id"],
            superseded_by_object_id=replacement_object["id"],
        )
        if updated is None:
            raise ContinuityReviewNotFoundError(f"continuity object {continuity_object_id} was not found")
        sync_contradiction_state_for_objects(
            store,
            continuity_object_ids=[updated["id"], replacement_object["id"]],
        )
        updated = _lookup_object_or_raise(store, continuity_object_id=continuity_object_id)
        replacement_refreshed = store.get_continuity_object_optional(replacement_object["id"])
        if replacement_refreshed is not None:
            replacement_object = replacement_refreshed

        return {
            "continuity_object": _serialize_review_object(store, updated),
            "correction_event": _serialize_correction_event(correction_event),
            "replacement_object": _serialize_review_object(store, replacement_object),
        }

    else:
        raise ContinuityReviewValidationError(f"unsupported continuity correction action: {action}")

    event_payload = request.as_payload()
    rationale_withheld = False
    text_withheld = False
    if action == "confirm":
        # Confirm makes the object current again, so it takes the shared
        # activation check over the object and the reason persisted with it
        # (ruling C2). The request fields confirm ignores are still stored on
        # the correction event, so they are checked as that row.
        refuse_credential_activation(
            current["title"], current["body"], None, reason, error=ContinuityReviewValidationError
        )
        refuse_credential_material(
            request.title,
            request.body,
            request.provenance,
            request.replacement_title,
            request.replacement_body,
            request.replacement_provenance,
            error=ContinuityReviewValidationError,
        )
    elif action in {"delete", "mark_stale"}:
        # A retirement always completes (ruling C6). A reason carrying
        # credential material is stored as a fixed placeholder; so is any
        # ignored request field the event would otherwise store.
        reason, rationale_withheld = withhold_credential_text(reason)
        event_payload["reason"] = reason
        for key in ("title", "body", "provenance", "replacement_title", "replacement_body", "replacement_provenance"):
            value = event_payload.get(key)
            fields = [value]
            if value is not None and carries_credential_material(*fields):
                event_payload[key] = TEXT_WITHHELD_PLACEHOLDER
                text_withheld = True
    else:
        # An edit ignores replacement_* but still persists them on the event.
        refuse_credential_material(
            request.replacement_title,
            request.replacement_body,
            request.replacement_provenance,
            error=ContinuityReviewValidationError,
        )

    after_snapshot: JsonObject = {
        **before_snapshot,
        "status": next_status,
        "title": next_title,
        "body": next_body,
        "provenance": next_provenance,
        "confidence": next_confidence,
        "last_confirmed_at": isoformat_or_none(next_last_confirmed_at),
        "supersedes_object_id": (
            None if next_supersedes_object_id is None else str(next_supersedes_object_id)
        ),
        "superseded_by_object_id": (
            None if next_superseded_by_object_id is None else str(next_superseded_by_object_id)
        ),
    }

    correction_event = _create_correction_event(
        store,
        continuity_object_id=continuity_object_id,
        action=action,
        reason=reason,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        payload=event_payload,
    )

    updated = store.update_continuity_object_optional(
        continuity_object_id=continuity_object_id,
        status=next_status,
        is_preserved=current["is_preserved"],
        is_searchable=current["is_searchable"],
        is_promotable=current["is_promotable"],
        title=next_title,
        body=next_body,
        provenance=next_provenance,
        confidence=next_confidence,
        last_confirmed_at=next_last_confirmed_at,
        supersedes_object_id=next_supersedes_object_id,
        superseded_by_object_id=next_superseded_by_object_id,
    )
    if updated is None:
        raise ContinuityReviewNotFoundError(f"continuity object {continuity_object_id} was not found")
    sync_contradiction_state_for_objects(
        store,
        continuity_object_ids=[updated["id"]],
    )
    updated = _lookup_object_or_raise(store, continuity_object_id=continuity_object_id)

    response: dict[str, object] = {
        "continuity_object": _serialize_review_object(store, updated),
        "correction_event": _serialize_correction_event(correction_event),
        "replacement_object": None,
    }
    if action in {"delete", "mark_stale"}:
        # Ruling C6: a retirement reports whether it withheld anything. The
        # response contract class is frozen by the contracts split test, so
        # the two flags ride on it rather than widening it in this change.
        response["rationale_withheld"] = rationale_withheld
        response["text_withheld"] = text_withheld
    return cast(ContinuityCorrectionApplyResponse, response)


def build_default_continuity_review_query() -> ContinuityReviewQueueQueryInput:
    return ContinuityReviewQueueQueryInput(limit=DEFAULT_CONTINUITY_REVIEW_LIMIT)


__all__ = [
    "ContinuityReviewNotFoundError",
    "ContinuityReviewValidationError",
    "apply_continuity_correction",
    "build_default_continuity_review_query",
    "get_continuity_review_detail",
    "list_continuity_review_queue",
]
