from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from alicebot_api.continuity_objects import serialize_continuity_lifecycle_state_from_record
from alicebot_api.credential_floor import refuse_credential_activation, withhold_credential_text
from alicebot_api.continuity_recall import query_continuity_recall
from alicebot_api.contracts import (
    CONTINUITY_DAILY_BRIEF_ASSEMBLY_VERSION_V0,
    CONTINUITY_OPEN_LOOP_ITEM_ORDER,
    CONTINUITY_OPEN_LOOP_REVIEW_ACTIONS,
    CONTINUITY_WEEKLY_REVIEW_ASSEMBLY_VERSION_V0,
    MAX_CONTINUITY_DAILY_BRIEF_LIMIT,
    MAX_CONTINUITY_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RECALL_LIMIT,
    MAX_CONTINUITY_WEEKLY_REVIEW_LIMIT,
    ContinuityDailyBriefRecord,
    ContinuityDailyBriefRequestInput,
    ContinuityDailyBriefResponse,
    ContinuityOpenLoopDashboardQueryInput,
    ContinuityOpenLoopDashboardRecord,
    ContinuityOpenLoopDashboardResponse,
    ContinuityOpenLoopPosture,
    ContinuityOpenLoopReviewActionInput,
    ContinuityOpenLoopReviewActionResponse,
    ContinuityOpenLoopSection,
    ContinuityRecallQueryInput,
    ContinuityRecallResultRecord,
    ContinuityRecallScopeFilters,
    ContinuityResumptionEmptyState,
    ContinuityResumptionSingleSection,
    ContinuityReviewObjectRecord,
    ContinuityWeeklyReviewRecord,
    ContinuityWeeklyReviewRequestInput,
    ContinuityWeeklyReviewResponse,
    isoformat_or_none,
)
from alicebot_api.store import (
    ContinuityCorrectionEventRow,
    ContinuityObjectRow,
    ContinuityStore,
    JsonObject,
)


class ContinuityOpenLoopValidationError(ValueError):
    """Raised when an open-loop continuity request is invalid."""


class ContinuityOpenLoopNotFoundError(LookupError):
    """Raised when the selected continuity object is not visible in scope."""


@dataclass(frozen=True, slots=True)
class _LifecycleTransition:
    correction_action: str
    lifecycle_outcome: str


_OPEN_LOOP_OBJECT_TYPES = {"WaitingFor", "Blocker", "NextAction"}
_OPEN_LOOP_POSTURES: tuple[ContinuityOpenLoopPosture, ...] = (
    "waiting_for",
    "blocker",
    "stale",
    "next_action",
)
_EMPTY_MESSAGES: dict[ContinuityOpenLoopPosture, str] = {
    "waiting_for": "No waiting-for items in the requested scope.",
    "blocker": "No blocker items in the requested scope.",
    "stale": "No stale items in the requested scope.",
    "next_action": "No next-action items in the requested scope.",
}
_DAILY_WAITING_EMPTY = "No waiting-for highlights for today in the requested scope."
_DAILY_BLOCKER_EMPTY = "No blocker highlights for today in the requested scope."
_DAILY_STALE_EMPTY = "No stale items for today in the requested scope."
_DAILY_NEXT_EMPTY = "No next suggested action in the requested scope."

_REVIEW_ACTION_TRANSITIONS: dict[str, _LifecycleTransition] = {
    "done": _LifecycleTransition(correction_action="edit", lifecycle_outcome="completed"),
    "deferred": _LifecycleTransition(correction_action="mark_stale", lifecycle_outcome="stale"),
    "still_blocked": _LifecycleTransition(correction_action="confirm", lifecycle_outcome="active"),
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


def _serialize_review_object(record: ContinuityObjectRow) -> ContinuityReviewObjectRecord:
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
    }


def _serialize_correction_event(record: ContinuityCorrectionEventRow):
    return {
        "id": str(record["id"]),
        "continuity_object_id": str(record["continuity_object_id"]),
        "action": record["action"],
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


def _open_loop_posture(item: ContinuityRecallResultRecord) -> ContinuityOpenLoopPosture | None:
    if item["object_type"] not in _OPEN_LOOP_OBJECT_TYPES:
        return None

    if item["status"] == "stale":
        return "stale"

    if item["status"] != "active":
        return None

    if item["object_type"] == "WaitingFor":
        return "waiting_for"
    if item["object_type"] == "Blocker":
        return "blocker"
    if item["object_type"] == "NextAction":
        return "next_action"

    return None


def _recency_sort_key(item: ContinuityRecallResultRecord) -> tuple[str, str]:
    return (item["created_at"], item["id"])


def _empty_state(*, is_empty: bool, message: str) -> ContinuityResumptionEmptyState:
    return {
        "is_empty": is_empty,
        "message": message,
    }


def _build_section(
    *,
    all_items: list[ContinuityRecallResultRecord],
    limit: int,
    empty_message: str,
) -> ContinuityOpenLoopSection:
    items = all_items[:limit] if limit > 0 else []
    return {
        "items": items,
        "summary": {
            "limit": limit,
            "returned_count": len(items),
            "total_count": len(all_items),
            "order": list(CONTINUITY_OPEN_LOOP_ITEM_ORDER),
        },
        "empty_state": _empty_state(
            is_empty=len(items) == 0,
            message=empty_message,
        ),
    }


def _validate_limit(limit: int, *, max_limit: int) -> None:
    if limit < 0 or limit > max_limit:
        raise ContinuityOpenLoopValidationError(f"limit must be between 0 and {max_limit}")


def _is_offset_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _validate_time_window(*, since: datetime | None, until: datetime | None) -> None:
    if since is None or until is None:
        return

    if _is_offset_aware(since) != _is_offset_aware(until):
        raise ContinuityOpenLoopValidationError(
            "since and until must both include timezone offsets or both omit timezone offsets"
        )

    try:
        if until < since:
            raise ContinuityOpenLoopValidationError("until must be greater than or equal to since")
    except TypeError as exc:
        raise ContinuityOpenLoopValidationError(
            "since and until must both include timezone offsets or both omit timezone offsets"
        ) from exc


def _group_open_loops(
    recall_items: list[ContinuityRecallResultRecord],
) -> dict[ContinuityOpenLoopPosture, list[ContinuityRecallResultRecord]]:
    grouped: dict[ContinuityOpenLoopPosture, list[ContinuityRecallResultRecord]] = {
        "waiting_for": [],
        "blocker": [],
        "stale": [],
        "next_action": [],
    }

    for item in recall_items:
        item_posture = _open_loop_posture(item)
        if item_posture is None:
            continue
        grouped[item_posture].append(item)

    for posture in _OPEN_LOOP_POSTURES:
        grouped[posture].sort(key=_recency_sort_key, reverse=True)

    return grouped


def _correction_recurrence_count(
    store: ContinuityStore,
    *,
    grouped: dict[ContinuityOpenLoopPosture, list[ContinuityRecallResultRecord]],
) -> int:
    object_ids: set[UUID] = set()
    for posture in _OPEN_LOOP_POSTURES:
        for item in grouped[posture]:
            try:
                object_ids.add(UUID(item["id"]))
            except (TypeError, ValueError):
                continue

    recurrence_count = 0
    for continuity_object_id in sorted(object_ids, key=str):
        correction_events = store.list_continuity_correction_events(
            continuity_object_id=continuity_object_id,
            limit=2,
        )
        if len(correction_events) >= 2:
            recurrence_count += 1

    return recurrence_count


def _load_grouped_open_loop_candidates(
    store: ContinuityStore,
    *,
    user_id: UUID,
    query: str | None,
    thread_id: UUID | None,
    task_id: UUID | None,
    project: str | None,
    person: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[
    ContinuityRecallScopeFilters,
    dict[ContinuityOpenLoopPosture, list[ContinuityRecallResultRecord]],
]:
    recall_payload = query_continuity_recall(
        store,
        user_id=user_id,
        request=ContinuityRecallQueryInput(
            query=query,
            thread_id=thread_id,
            task_id=task_id,
            project=project,
            person=person,
            since=since,
            until=until,
            limit=MAX_CONTINUITY_RECALL_LIMIT,
        ),
        apply_limit=False,
    )

    return recall_payload["summary"]["filters"], _group_open_loops(recall_payload["items"])


def compile_continuity_open_loop_dashboard(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityOpenLoopDashboardQueryInput,
) -> ContinuityOpenLoopDashboardResponse:
    _validate_limit(request.limit, max_limit=MAX_CONTINUITY_OPEN_LOOP_LIMIT)
    _validate_time_window(since=request.since, until=request.until)

    scope, grouped = _load_grouped_open_loop_candidates(
        store,
        user_id=user_id,
        query=request.query,
        thread_id=request.thread_id,
        task_id=request.task_id,
        project=request.project,
        person=request.person,
        since=request.since,
        until=request.until,
    )

    dashboard: ContinuityOpenLoopDashboardRecord = {
        "scope": scope,
        "waiting_for": _build_section(
            all_items=grouped["waiting_for"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["waiting_for"],
        ),
        "blocker": _build_section(
            all_items=grouped["blocker"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["blocker"],
        ),
        "stale": _build_section(
            all_items=grouped["stale"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["stale"],
        ),
        "next_action": _build_section(
            all_items=grouped["next_action"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["next_action"],
        ),
        "summary": {
            "limit": request.limit,
            "total_count": sum(len(grouped[posture]) for posture in _OPEN_LOOP_POSTURES),
            "posture_order": list(_OPEN_LOOP_POSTURES),
            "item_order": list(CONTINUITY_OPEN_LOOP_ITEM_ORDER),
        },
        "sources": ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
    }

    return {"dashboard": dashboard}


def compile_continuity_daily_brief(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityDailyBriefRequestInput,
) -> ContinuityDailyBriefResponse:
    _validate_limit(request.limit, max_limit=MAX_CONTINUITY_DAILY_BRIEF_LIMIT)
    _validate_time_window(since=request.since, until=request.until)

    scope, grouped = _load_grouped_open_loop_candidates(
        store,
        user_id=user_id,
        query=request.query,
        thread_id=request.thread_id,
        task_id=request.task_id,
        project=request.project,
        person=request.person,
        since=request.since,
        until=request.until,
    )

    next_item = grouped["next_action"][0] if request.limit > 0 and grouped["next_action"] else None
    next_section: ContinuityResumptionSingleSection = {
        "item": next_item,
        "empty_state": _empty_state(
            is_empty=next_item is None,
            message=_DAILY_NEXT_EMPTY,
        ),
    }

    brief: ContinuityDailyBriefRecord = {
        "assembly_version": CONTINUITY_DAILY_BRIEF_ASSEMBLY_VERSION_V0,
        "scope": scope,
        "waiting_for_highlights": _build_section(
            all_items=grouped["waiting_for"],
            limit=request.limit,
            empty_message=_DAILY_WAITING_EMPTY,
        ),
        "blocker_highlights": _build_section(
            all_items=grouped["blocker"],
            limit=request.limit,
            empty_message=_DAILY_BLOCKER_EMPTY,
        ),
        "stale_items": _build_section(
            all_items=grouped["stale"],
            limit=request.limit,
            empty_message=_DAILY_STALE_EMPTY,
        ),
        "next_suggested_action": next_section,
        "sources": ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
    }

    return {"brief": brief}


def compile_continuity_weekly_review(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityWeeklyReviewRequestInput,
) -> ContinuityWeeklyReviewResponse:
    _validate_limit(request.limit, max_limit=MAX_CONTINUITY_WEEKLY_REVIEW_LIMIT)
    _validate_time_window(since=request.since, until=request.until)

    scope, grouped = _load_grouped_open_loop_candidates(
        store,
        user_id=user_id,
        query=request.query,
        thread_id=request.thread_id,
        task_id=request.task_id,
        project=request.project,
        person=request.person,
        since=request.since,
        until=request.until,
    )
    correction_recurrence_count = _correction_recurrence_count(
        store,
        grouped=grouped,
    )
    freshness_drift_count = len(grouped["stale"])

    review: ContinuityWeeklyReviewRecord = {
        "assembly_version": CONTINUITY_WEEKLY_REVIEW_ASSEMBLY_VERSION_V0,
        "scope": scope,
        "rollup": {
            "total_count": sum(len(grouped[posture]) for posture in _OPEN_LOOP_POSTURES),
            "waiting_for_count": len(grouped["waiting_for"]),
            "blocker_count": len(grouped["blocker"]),
            "stale_count": len(grouped["stale"]),
            "correction_recurrence_count": correction_recurrence_count,
            "freshness_drift_count": freshness_drift_count,
            "next_action_count": len(grouped["next_action"]),
            "posture_order": list(_OPEN_LOOP_POSTURES),
        },
        "waiting_for": _build_section(
            all_items=grouped["waiting_for"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["waiting_for"],
        ),
        "blocker": _build_section(
            all_items=grouped["blocker"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["blocker"],
        ),
        "stale": _build_section(
            all_items=grouped["stale"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["stale"],
        ),
        "next_action": _build_section(
            all_items=grouped["next_action"],
            limit=request.limit,
            empty_message=_EMPTY_MESSAGES["next_action"],
        ),
        "sources": ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
    }

    return {"review": review}


def apply_continuity_open_loop_review_action(
    store: ContinuityStore,
    *,
    user_id: UUID,
    continuity_object_id: UUID,
    request: ContinuityOpenLoopReviewActionInput,
) -> ContinuityOpenLoopReviewActionResponse:
    del user_id

    action = request.action
    if action not in CONTINUITY_OPEN_LOOP_REVIEW_ACTIONS:
        allowed = ", ".join(CONTINUITY_OPEN_LOOP_REVIEW_ACTIONS)
        raise ContinuityOpenLoopValidationError(f"action must be one of: {allowed}")

    note = _normalize_optional_text(request.note)
    if note is not None and len(note) > 500:
        raise ContinuityOpenLoopValidationError("note must be 500 characters or fewer")

    current = store.get_continuity_object_optional(continuity_object_id)
    if current is None:
        raise ContinuityOpenLoopNotFoundError(f"continuity object {continuity_object_id} was not found")

    if current["object_type"] not in _OPEN_LOOP_OBJECT_TYPES:
        allowed_types = ", ".join(sorted(_OPEN_LOOP_OBJECT_TYPES))
        raise ContinuityOpenLoopValidationError(
            f"review action requires one of object types: {allowed_types}"
        )

    if current["status"] in {"superseded", "deleted", "cancelled"}:
        raise ContinuityOpenLoopValidationError(
            f"review action cannot be applied when status is {current['status']}"
        )

    transition = _REVIEW_ACTION_TRANSITIONS[action]
    # S4.4 round 3 (P2 item 8). still_blocked moves the object to active, so
    # it takes the shared activation check over the object's text first. The
    # note is stored on the correction event whatever the action, so a note
    # carrying credential material is withheld there, as every other review
    # surface does (owner ruling C6), and the response says so.
    if transition.lifecycle_outcome == "active":
        refuse_credential_activation(
            current["title"], current["body"], None, error=ContinuityOpenLoopValidationError
        )
    note, rationale_withheld = withhold_credential_text(note)
    next_last_confirmed_at = current["last_confirmed_at"]
    if action == "still_blocked":
        next_last_confirmed_at = _utcnow()

    before_snapshot = _snapshot(current)
    after_snapshot: JsonObject = {
        **before_snapshot,
        "status": transition.lifecycle_outcome,
        "last_confirmed_at": isoformat_or_none(next_last_confirmed_at),
    }

    correction_event = store.create_continuity_correction_event(
        continuity_object_id=continuity_object_id,
        action=transition.correction_action,
        reason=note,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        payload={
            "review_action": action,
            "note": note,
            "mapped_correction_action": transition.correction_action,
            "lifecycle_outcome": transition.lifecycle_outcome,
        },
    )

    updated = store.update_continuity_object_optional(
        continuity_object_id=continuity_object_id,
        status=transition.lifecycle_outcome,
        is_preserved=current["is_preserved"],
        is_searchable=current["is_searchable"],
        is_promotable=current["is_promotable"],
        title=current["title"],
        body=current["body"],
        provenance=current["provenance"],
        confidence=float(current["confidence"]),
        last_confirmed_at=next_last_confirmed_at,
        supersedes_object_id=current["supersedes_object_id"],
        superseded_by_object_id=current["superseded_by_object_id"],
    )
    if updated is None:
        raise ContinuityOpenLoopNotFoundError(f"continuity object {continuity_object_id} was not found")

    response: dict[str, object] = {
        "continuity_object": _serialize_review_object(updated),
        "correction_event": _serialize_correction_event(correction_event),
        "review_action": action,
        "lifecycle_outcome": transition.lifecycle_outcome,
        # The response contract class is frozen by the contracts split test,
        # so the flag rides on it rather than widening it here.
        "rationale_withheld": rationale_withheld,
    }
    return cast(ContinuityOpenLoopReviewActionResponse, response)


def build_default_continuity_open_loop_query() -> ContinuityOpenLoopDashboardQueryInput:
    return ContinuityOpenLoopDashboardQueryInput()


__all__ = [
    "ContinuityOpenLoopNotFoundError",
    "ContinuityOpenLoopValidationError",
    "apply_continuity_open_loop_review_action",
    "build_default_continuity_open_loop_query",
    "compile_continuity_daily_brief",
    "compile_continuity_open_loop_dashboard",
    "compile_continuity_weekly_review",
]
