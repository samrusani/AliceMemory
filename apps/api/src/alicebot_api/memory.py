from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from typing import TypedDict, cast
from uuid import UUID

import psycopg

from alicebot_api.continuity_open_loops import compile_continuity_weekly_review
from alicebot_api.credential_floor import refuse_credential_material
from alicebot_api.contracts import (
    AdmissionDecisionOutput,
    AdmissionAction,
    DEFAULT_AGENT_PROFILE_ID,
    DEFAULT_MEMORY_CONFIRMATION_STATUS,
    DEFAULT_MEMORY_PROMOTION_ELIGIBILITY,
    DEFAULT_MEMORY_REVIEW_QUEUE_PRIORITY_MODE,
    DEFAULT_MEMORY_TRUST_CLASS,
    DEFAULT_MEMORY_TYPE,
    DEFAULT_MEMORY_REVIEW_LIMIT,
    DEFAULT_OPEN_LOOP_LIMIT,
    MEMORY_QUALITY_HIGH_RISK_CONFIDENCE_THRESHOLD,
    MEMORY_QUALITY_MIN_ADJUDICATED_SAMPLE,
    MEMORY_QUALITY_PRECISION_TARGET,
    MEMORY_CONFIRMATION_STATUSES,
    MEMORY_PROMOTION_ELIGIBILITIES,
    OPEN_LOOP_REVIEW_ORDER,
    OPEN_LOOP_STATUSES,
    MEMORY_REVIEW_LABEL_ORDER,
    MEMORY_REVIEW_LABEL_VALUES,
    MEMORY_REVIEW_QUEUE_ORDER_BY_PRIORITY_MODE,
    MEMORY_REVIEW_QUEUE_PRIORITY_MODES,
    MEMORY_REVISION_REVIEW_ORDER,
    MEMORY_REVIEW_ORDER,
    MEMORY_TYPES,
    MEMORY_TRUST_CLASSES,
    MemoryCandidateInput,
    MemoryConfirmationStatus,
    MemoryEvaluationSummary,
    MemoryEvaluationSummaryResponse,
    MemoryDuplicateGroupRecord,
    MemoryHygieneDashboardResponse,
    MemoryHygieneDashboardSummary,
    MemoryHygieneFocusKind,
    MemoryHygieneFocusRecord,
    MemoryHygienePosture,
    MemoryReviewLabelCounts,
    MemoryReviewLabelCreateResponse,
    MemoryReviewLabelListResponse,
    MemoryReviewLabelRecord,
    MemoryReviewLabelSummary,
    MemoryReviewLabelValue,
    MemoryReviewQueuePriorityMode,
    MemoryReviewQueueItem,
    MemoryReviewQueueResponse,
    MemoryReviewQueueSummary,
    MemoryQualityGateComputationCounts,
    MemoryQualityGateResponse,
    MemoryQualityReviewAction,
    MemoryQualityGateStatus,
    MemoryQualityGateSummary,
    MemoryTrustCorrectionFreshnessSummary,
    MemoryPromotionEligibility,
    MemoryStatus,
    MemoryTrustClass,
    MemoryType,
    MemoryTrustDashboardResponse,
    MemoryTrustDashboardSummary,
    MemoryReviewQueuePressureSummary,
    MemoryTrustQueueAgingSummary,
    MemoryTrustQueuePostureSummary,
    MemoryTrustRecommendedReview,
    MemoryRevisionReviewListResponse,
    MemoryRevisionReviewListSummary,
    MemoryRevisionReviewRecord,
    MemoryReviewDetailResponse,
    MemoryReviewListResponse,
    MemoryReviewListSummary,
    MemoryReviewRecord,
    MemoryReviewStatusFilter,
    OpenLoopCreateInput,
    OpenLoopCreateResponse,
    OpenLoopDetailResponse,
    OpenLoopListResponse,
    OpenLoopListSummary,
    OpenLoopRecord,
    OpenLoopStatusFilter,
    OpenLoopStatus,
    OpenLoopStatusUpdateInput,
    OpenLoopStatusUpdateResponse,
    PersistedMemoryRecord,
    PersistedMemoryRevisionRecord,
    ContinuityWeeklyReviewRequestInput,
    isoformat_or_none,
)
from alicebot_api.retrieval_evaluation import get_retrieval_evaluation_summary
from alicebot_api.store import (
    ContinuityStore,
    EventRow,
    JsonObject,
    LabelCountRow,
    MemoryReviewLabelRow,
    MemoryRevisionRow,
    MemoryRow,
    OpenLoopRow,
)


class MemoryAdmissionValidationError(ValueError):
    """Raised when an admission request fails explicit candidate validation."""


class MemoryReviewNotFoundError(LookupError):
    """Raised when a requested memory is not visible inside the current user scope."""


class OpenLoopValidationError(ValueError):
    """Raised when an open-loop request fails explicit lifecycle validation."""


class OpenLoopNotFoundError(LookupError):
    """Raised when a requested open loop is not visible inside the current user scope."""


class _ResolvedMemoryMetadata(TypedDict):
    memory_type: MemoryType
    confidence: float | None
    salience: float | None
    confirmation_status: MemoryConfirmationStatus
    trust_class: MemoryTrustClass
    promotion_eligibility: MemoryPromotionEligibility
    evidence_count: int | None
    independent_source_count: int | None
    extracted_by_model: str | None
    trust_reason: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    last_confirmed_at: datetime | None


_MEMORY_REVIEW_LABEL_ORDER: tuple[MemoryReviewLabelValue, ...] = (
    "correct",
    "incorrect",
    "outdated",
    "insufficient_evidence",
)


def _memory_status(value: str) -> MemoryStatus:
    if value == "active":
        return "active"
    if value == "deleted":
        return "deleted"
    raise ValueError(f"unsupported memory status: {value}")


def _admission_action(value: str) -> AdmissionAction:
    if value == "NOOP":
        return "NOOP"
    if value == "ADD":
        return "ADD"
    if value == "UPDATE":
        return "UPDATE"
    if value == "DELETE":
        return "DELETE"
    raise ValueError(f"unsupported memory revision action: {value}")


def _memory_review_label(value: str) -> MemoryReviewLabelValue:
    if value == "correct":
        return "correct"
    if value == "incorrect":
        return "incorrect"
    if value == "outdated":
        return "outdated"
    if value == "insufficient_evidence":
        return "insufficient_evidence"
    raise ValueError(f"unsupported memory review label: {value}")


def _open_loop_status(value: str) -> OpenLoopStatus:
    if value == "open":
        return "open"
    if value == "resolved":
        return "resolved"
    if value == "dismissed":
        return "dismissed"
    raise ValueError(f"unsupported open-loop status: {value}")


def _add_typed_memory_metadata(
    payload: PersistedMemoryRecord | MemoryReviewRecord | MemoryReviewQueueItem,
    memory: MemoryRow,
) -> None:
    if "memory_type" in memory:
        payload["memory_type"] = cast(MemoryType, memory["memory_type"])
    if "confidence" in memory:
        payload["confidence"] = memory["confidence"]
    if "salience" in memory:
        payload["salience"] = memory["salience"]
    if "confirmation_status" in memory:
        payload["confirmation_status"] = cast(
            MemoryConfirmationStatus,
            memory["confirmation_status"],
        )
    if "trust_class" in memory:
        payload["trust_class"] = cast(MemoryTrustClass, memory["trust_class"])
    if "promotion_eligibility" in memory:
        payload["promotion_eligibility"] = cast(
            MemoryPromotionEligibility,
            memory["promotion_eligibility"],
        )
    if "evidence_count" in memory:
        payload["evidence_count"] = memory["evidence_count"]
    if "independent_source_count" in memory:
        payload["independent_source_count"] = memory["independent_source_count"]
    if "extracted_by_model" in memory:
        payload["extracted_by_model"] = memory["extracted_by_model"]
    if "trust_reason" in memory:
        payload["trust_reason"] = memory["trust_reason"]
    if "valid_from" in memory:
        payload["valid_from"] = isoformat_or_none(memory["valid_from"])
    if "valid_to" in memory:
        payload["valid_to"] = isoformat_or_none(memory["valid_to"])
    if "last_confirmed_at" in memory:
        payload["last_confirmed_at"] = isoformat_or_none(memory["last_confirmed_at"])

def _serialize_memory(memory: MemoryRow) -> PersistedMemoryRecord:
    payload: PersistedMemoryRecord = {
        "id": str(memory["id"]),
        "user_id": str(memory["user_id"]),
        "memory_key": memory["memory_key"],
        "value": memory["value"],
        "status": _memory_status(memory["status"]),
        "source_event_ids": memory["source_event_ids"],
        "created_at": memory["created_at"].isoformat(),
        "updated_at": memory["updated_at"].isoformat(),
        "deleted_at": isoformat_or_none(memory["deleted_at"]),
    }
    _add_typed_memory_metadata(payload, memory)
    return payload


def _serialize_memory_revision(revision: MemoryRevisionRow) -> PersistedMemoryRevisionRecord:
    return {
        "id": str(revision["id"]),
        "user_id": str(revision["user_id"]),
        "memory_id": str(revision["memory_id"]),
        "sequence_no": revision["sequence_no"],
        "action": _admission_action(revision["action"]),
        "memory_key": revision["memory_key"],
        "previous_value": revision["previous_value"],
        "new_value": revision["new_value"],
        "source_event_ids": revision["source_event_ids"],
        "candidate": revision["candidate"],
        "created_at": revision["created_at"].isoformat(),
    }


def _serialize_memory_review(memory: MemoryRow) -> MemoryReviewRecord:
    payload: MemoryReviewRecord = {
        "id": str(memory["id"]),
        "memory_key": memory["memory_key"],
        "value": memory["value"],
        "status": _memory_status(memory["status"]),
        "source_event_ids": memory["source_event_ids"],
        "created_at": memory["created_at"].isoformat(),
        "updated_at": memory["updated_at"].isoformat(),
        "deleted_at": isoformat_or_none(memory["deleted_at"]),
    }
    _add_typed_memory_metadata(payload, memory)
    return payload


def _is_stale_truth_memory(memory: MemoryRow) -> bool:
    if memory.get("confirmation_status") == "contested":
        return True
    return memory.get("valid_to") is not None


def _is_high_risk_memory(memory: MemoryRow) -> bool:
    if memory.get("promotion_eligibility") == "not_promotable":
        return True
    if _is_stale_truth_memory(memory):
        return True
    if memory.get("confirmation_status") != "confirmed":
        return True
    confidence = memory.get("confidence")
    if confidence is None:
        return True
    return confidence < MEMORY_QUALITY_HIGH_RISK_CONFIDENCE_THRESHOLD


def _high_risk_confidence_priority(memory: MemoryRow) -> float:
    confidence = memory.get("confidence")
    if confidence is None:
        return 2.0
    if confidence < MEMORY_QUALITY_HIGH_RISK_CONFIDENCE_THRESHOLD:
        return 1.0 - confidence
    return 0.0


def _stale_truth_priority(memory: MemoryRow) -> float:
    valid_to = memory.get("valid_to")
    if valid_to is None:
        return float("-inf")
    return -valid_to.timestamp()


def _order_review_queue_memories(
    memories: list[MemoryRow],
    *,
    priority_mode: MemoryReviewQueuePriorityMode,
) -> list[MemoryRow]:
    if priority_mode == "oldest_first":
        return sorted(
            memories,
            key=lambda memory: (memory["updated_at"], memory["created_at"], str(memory["id"])),
        )

    if priority_mode == "high_risk_first":
        return sorted(
            memories,
            key=lambda memory: (
                _is_high_risk_memory(memory),
                _high_risk_confidence_priority(memory),
                memory["updated_at"],
                memory["created_at"],
                str(memory["id"]),
            ),
            reverse=True,
        )

    if priority_mode == "stale_truth_first":
        return sorted(
            memories,
            key=lambda memory: (
                _is_stale_truth_memory(memory),
                _stale_truth_priority(memory),
                memory["updated_at"],
                memory["created_at"],
                str(memory["id"]),
            ),
            reverse=True,
        )

    return sorted(
        memories,
        key=lambda memory: (memory["updated_at"], memory["created_at"], str(memory["id"])),
        reverse=True,
    )


def _review_queue_priority_reason(
    *,
    priority_mode: MemoryReviewQueuePriorityMode,
    is_high_risk: bool,
    is_stale_truth: bool,
    is_promotable: bool,
) -> str:
    if not is_promotable:
        if priority_mode == "oldest_first":
            return "oldest_not_promotable"
        if priority_mode == "recent_first":
            return "recent_not_promotable"
        if priority_mode == "stale_truth_first" and is_stale_truth:
            return "stale_truth_not_promotable"
        return "high_risk_not_promotable"

    if priority_mode == "high_risk_first":
        if is_high_risk and is_stale_truth:
            return "high_risk_stale_truth"
        if is_high_risk:
            return "high_risk"
        if is_stale_truth:
            return "stale_truth"
        return "recent_backlog"

    if priority_mode == "stale_truth_first":
        if is_stale_truth and is_high_risk:
            return "stale_truth_high_risk"
        if is_stale_truth:
            return "stale_truth"
        if is_high_risk:
            return "high_risk"
        return "recent_backlog"

    if priority_mode == "oldest_first":
        return "oldest_first"

    return "recent_first"


def _serialize_memory_review_queue_item(
    memory: MemoryRow,
    *,
    priority_mode: MemoryReviewQueuePriorityMode,
) -> MemoryReviewQueueItem:
    is_high_risk = _is_high_risk_memory(memory)
    is_stale_truth = _is_stale_truth_memory(memory)
    is_promotable = memory.get("promotion_eligibility") != "not_promotable"
    payload: MemoryReviewQueueItem = {
        "id": str(memory["id"]),
        "memory_key": memory["memory_key"],
        "value": memory["value"],
        "status": "active",
        "source_event_ids": memory["source_event_ids"],
        "is_high_risk": is_high_risk,
        "is_stale_truth": is_stale_truth,
        "is_promotable": is_promotable,
        "queue_priority_mode": priority_mode,
        "priority_reason": _review_queue_priority_reason(
            priority_mode=priority_mode,
            is_high_risk=is_high_risk,
            is_stale_truth=is_stale_truth,
            is_promotable=is_promotable,
        ),
        "created_at": memory["created_at"].isoformat(),
        "updated_at": memory["updated_at"].isoformat(),
    }
    if memory["status"] != "active":
        raise ValueError(f"review queue memory {memory['id']} is not active")
    _add_typed_memory_metadata(payload, memory)
    return payload


def _serialize_memory_revision_review(revision: MemoryRevisionRow) -> MemoryRevisionReviewRecord:
    return {
        "id": str(revision["id"]),
        "memory_id": str(revision["memory_id"]),
        "sequence_no": revision["sequence_no"],
        "action": _admission_action(revision["action"]),
        "memory_key": revision["memory_key"],
        "previous_value": revision["previous_value"],
        "new_value": revision["new_value"],
        "source_event_ids": revision["source_event_ids"],
        "created_at": revision["created_at"].isoformat(),
    }


def _serialize_memory_review_label(label: MemoryReviewLabelRow) -> MemoryReviewLabelRecord:
    return {
        "id": str(label["id"]),
        "memory_id": str(label["memory_id"]),
        "reviewer_user_id": str(label["user_id"]),
        "label": _memory_review_label(label["label"]),
        "note": label["note"],
        "created_at": label["created_at"].isoformat(),
    }


def _empty_memory_review_label_counts() -> MemoryReviewLabelCounts:
    return {
        "correct": 0,
        "incorrect": 0,
        "outdated": 0,
        "insufficient_evidence": 0,
    }


def _summarize_memory_review_label_counts(rows: list[LabelCountRow]) -> MemoryReviewLabelCounts:
    counts = _empty_memory_review_label_counts()
    for row in rows:
        label = row["label"]
        if label == "correct":
            counts["correct"] = row["count"]
        elif label == "incorrect":
            counts["incorrect"] = row["count"]
        elif label == "outdated":
            counts["outdated"] = row["count"]
        elif label == "insufficient_evidence":
            counts["insufficient_evidence"] = row["count"]
    return counts


def _memory_review_label_total(counts: MemoryReviewLabelCounts) -> int:
    return (
        counts["correct"]
        + counts["incorrect"]
        + counts["outdated"]
        + counts["insufficient_evidence"]
    )


def _build_memory_review_label_summary(
    *,
    memory_id: UUID,
    counts: MemoryReviewLabelCounts,
) -> MemoryReviewLabelSummary:
    return {
        "memory_id": str(memory_id),
        "total_count": _memory_review_label_total(counts),
        "counts_by_label": counts,
        "order": list(MEMORY_REVIEW_LABEL_ORDER),
    }


def _normalize_memory_status_filter(status: MemoryReviewStatusFilter) -> str | None:
    if status == "all":
        return None
    return status


def list_memory_review_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
    status: MemoryReviewStatusFilter = "active",
    limit: int = DEFAULT_MEMORY_REVIEW_LIMIT,
) -> MemoryReviewListResponse:
    del user_id

    normalized_status = _normalize_memory_status_filter(status)
    total_count = store.count_memories(status=normalized_status)
    memories = store.list_review_memories(status=normalized_status, limit=limit)
    items = [_serialize_memory_review(memory) for memory in memories]
    summary: MemoryReviewListSummary = {
        "status": status,
        "limit": limit,
        "returned_count": len(items),
        "total_count": total_count,
        "has_more": len(items) < total_count,
        "order": list(MEMORY_REVIEW_ORDER),
    }
    return {
        "items": items,
        "summary": summary,
    }


def list_memory_review_queue_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
    limit: int = DEFAULT_MEMORY_REVIEW_LIMIT,
    priority_mode: MemoryReviewQueuePriorityMode = DEFAULT_MEMORY_REVIEW_QUEUE_PRIORITY_MODE,
) -> MemoryReviewQueueResponse:
    del user_id

    candidate_memories = store.list_unlabeled_review_memories(limit=None)
    ordered_memories = _order_review_queue_memories(
        candidate_memories,
        priority_mode=priority_mode,
    )
    selected_memories = ordered_memories[:limit]
    items = [
        _serialize_memory_review_queue_item(
            memory,
            priority_mode=priority_mode,
        )
        for memory in selected_memories
    ]
    total_count = len(candidate_memories)
    summary: MemoryReviewQueueSummary = {
        "memory_status": "active",
        "review_state": "unlabeled",
        "priority_mode": priority_mode,
        "available_priority_modes": list(MEMORY_REVIEW_QUEUE_PRIORITY_MODES),
        "limit": limit,
        "returned_count": len(items),
        "total_count": total_count,
        "has_more": len(items) < total_count,
        "order": list(MEMORY_REVIEW_QUEUE_ORDER_BY_PRIORITY_MODE[priority_mode]),
    }
    return {
        "items": items,
        "summary": summary,
    }


def get_memory_review_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    memory_id: UUID,
) -> MemoryReviewDetailResponse:
    del user_id

    memory = store.get_memory_optional(memory_id)
    if memory is None:
        raise MemoryReviewNotFoundError(f"memory {memory_id} was not found")

    return {
        "memory": _serialize_memory_review(memory),
    }


def list_memory_revision_review_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
    memory_id: UUID,
    limit: int = DEFAULT_MEMORY_REVIEW_LIMIT,
) -> MemoryRevisionReviewListResponse:
    del user_id

    memory = store.get_memory_optional(memory_id)
    if memory is None:
        raise MemoryReviewNotFoundError(f"memory {memory_id} was not found")

    total_count = store.count_memory_revisions(memory_id)
    revisions = store.list_memory_revisions(memory_id, limit=limit)
    items = [_serialize_memory_revision_review(revision) for revision in revisions]
    summary: MemoryRevisionReviewListSummary = {
        "memory_id": str(memory["id"]),
        "limit": limit,
        "returned_count": len(items),
        "total_count": total_count,
        "has_more": len(items) < total_count,
        "order": list(MEMORY_REVISION_REVIEW_ORDER),
    }
    return {
        "items": items,
        "summary": summary,
    }


def create_memory_review_label_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    memory_id: UUID,
    label: MemoryReviewLabelValue,
    note: str | None,
) -> MemoryReviewLabelCreateResponse:
    del user_id

    memory = store.get_memory_optional(memory_id)
    if memory is None:
        raise MemoryReviewNotFoundError(f"memory {memory_id} was not found")

    created_label = store.create_memory_review_label(
        memory_id=memory_id,
        label=label,
        note=note,
    )
    counts = _summarize_memory_review_label_counts(store.list_memory_review_label_counts(memory_id))
    return {
        "label": _serialize_memory_review_label(created_label),
        "summary": _build_memory_review_label_summary(memory_id=memory_id, counts=counts),
    }


def list_memory_review_label_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
    memory_id: UUID,
) -> MemoryReviewLabelListResponse:
    del user_id

    memory = store.get_memory_optional(memory_id)
    if memory is None:
        raise MemoryReviewNotFoundError(f"memory {memory_id} was not found")

    items = [_serialize_memory_review_label(label) for label in store.list_memory_review_labels(memory_id)]
    counts = _summarize_memory_review_label_counts(store.list_memory_review_label_counts(memory_id))
    return {
        "items": items,
        "summary": _build_memory_review_label_summary(memory_id=memory_id, counts=counts),
    }


def get_memory_evaluation_summary(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> MemoryEvaluationSummaryResponse:
    del user_id

    total_memory_count = store.count_memories()
    active_memory_count = store.count_memories(status="active")
    deleted_memory_count = store.count_memories(status="deleted")
    labeled_memory_count = store.count_labeled_memories()
    unlabeled_memory_count = store.count_unlabeled_memories()
    label_row_counts = _summarize_memory_review_label_counts(store.list_all_memory_review_label_counts())
    summary: MemoryEvaluationSummary = {
        "total_memory_count": total_memory_count,
        "active_memory_count": active_memory_count,
        "deleted_memory_count": deleted_memory_count,
        "labeled_memory_count": labeled_memory_count,
        "unlabeled_memory_count": unlabeled_memory_count,
        "total_label_row_count": _memory_review_label_total(label_row_counts),
        "label_row_counts_by_value": label_row_counts,
        "label_value_order": list(_MEMORY_REVIEW_LABEL_ORDER),
    }
    return {
        "summary": summary,
    }


def _calculate_memory_precision(*, correct_count: int, incorrect_count: int) -> float | None:
    denominator = correct_count + incorrect_count
    if denominator == 0:
        return None
    return correct_count / denominator


def _queue_age_hours(*, anchor_updated_at: datetime, updated_at: datetime) -> float:
    age_hours = (anchor_updated_at - updated_at).total_seconds() / 3600.0
    return max(0.0, age_hours)


def _summarize_queue_aging(queue_memories: list[MemoryRow]) -> MemoryTrustQueueAgingSummary:
    if not queue_memories:
        return {
            "anchor_updated_at": None,
            "newest_updated_at": None,
            "oldest_updated_at": None,
            "backlog_span_hours": 0.0,
            "fresh_within_24h_count": 0,
            "aging_24h_to_72h_count": 0,
            "stale_over_72h_count": 0,
        }

    newest_updated_at = max(memory["updated_at"] for memory in queue_memories)
    oldest_updated_at = min(memory["updated_at"] for memory in queue_memories)
    fresh_count = 0
    aging_count = 0
    stale_count = 0

    for memory in queue_memories:
        age_hours = _queue_age_hours(
            anchor_updated_at=newest_updated_at,
            updated_at=memory["updated_at"],
        )
        if age_hours <= 24.0:
            fresh_count += 1
        elif age_hours <= 72.0:
            aging_count += 1
        else:
            stale_count += 1

    backlog_span_hours = max(
        0.0,
        (newest_updated_at - oldest_updated_at).total_seconds() / 3600.0,
    )
    return {
        "anchor_updated_at": newest_updated_at.isoformat(),
        "newest_updated_at": newest_updated_at.isoformat(),
        "oldest_updated_at": oldest_updated_at.isoformat(),
        "backlog_span_hours": round(backlog_span_hours, 6),
        "fresh_within_24h_count": fresh_count,
        "aging_24h_to_72h_count": aging_count,
        "stale_over_72h_count": stale_count,
    }


def _summarize_queue_posture(
    *,
    queue_memories: list[MemoryRow],
    priority_mode: MemoryReviewQueuePriorityMode,
) -> MemoryTrustQueuePostureSummary:
    high_risk_count = 0
    stale_truth_count = 0
    priority_reason_counts: dict[str, int] = {}

    for memory in queue_memories:
        is_high_risk = _is_high_risk_memory(memory)
        is_stale_truth = _is_stale_truth_memory(memory)
        if is_high_risk:
            high_risk_count += 1
        if is_stale_truth:
            stale_truth_count += 1

        reason = _review_queue_priority_reason(
            priority_mode=priority_mode,
            is_high_risk=is_high_risk,
            is_stale_truth=is_stale_truth,
            is_promotable=memory.get("promotion_eligibility") != "not_promotable",
        )
        priority_reason_counts[reason] = priority_reason_counts.get(reason, 0) + 1

    return {
        "priority_mode": priority_mode,
        "total_count": len(queue_memories),
        "high_risk_count": high_risk_count,
        "stale_truth_count": stale_truth_count,
        "priority_reason_counts": {
            reason: priority_reason_counts[reason] for reason in sorted(priority_reason_counts)
        },
        "order": list(MEMORY_REVIEW_QUEUE_ORDER_BY_PRIORITY_MODE[priority_mode]),
        "aging": _summarize_queue_aging(queue_memories),
    }


def _determine_recommended_review(
    *,
    quality_gate: MemoryQualityGateSummary,
    queue_posture: MemoryTrustQueuePostureSummary,
    correction_freshness: MemoryTrustCorrectionFreshnessSummary,
) -> MemoryTrustRecommendedReview:
    action: MemoryQualityReviewAction
    priority_mode: MemoryReviewQueuePriorityMode
    reason: str

    if quality_gate["remaining_to_minimum_sample"] > 0:
        action = "adjudicate_minimum_sample"
        priority_mode = "recent_first"
        reason = (
            "Adjudicated sample is below minimum threshold; prioritize recent backlog "
            "to reach quality-gate sample sufficiency."
        )
    elif queue_posture["high_risk_count"] > 0:
        action = "review_high_risk_queue"
        priority_mode = "high_risk_first"
        reason = "High-risk unlabeled memories are present; triage those before lower-risk backlog."
    elif queue_posture["stale_truth_count"] > 0:
        action = "review_stale_truth_queue"
        priority_mode = "stale_truth_first"
        reason = "Stale-truth unlabeled memories are present; resolve stale truth before newer backlog."
    elif queue_posture["total_count"] > 0:
        action = "drain_unlabeled_queue"
        priority_mode = "oldest_first"
        reason = "Unlabeled backlog remains; drain oldest-first for deterministic queue hygiene."
    elif correction_freshness["correction_recurrence_count"] > 0:
        action = "investigate_correction_recurrence"
        priority_mode = "recent_first"
        reason = (
            "Queue is clear but recurring correction patterns are present; inspect recent corrections "
            "for repeated quality misses."
        )
    elif correction_freshness["freshness_drift_count"] > 0:
        action = "remediate_freshness_drift"
        priority_mode = "stale_truth_first"
        reason = "Queue is clear but freshness drift is present; prioritize stale truth remediation."
    else:
        action = "monitor_quality_posture"
        priority_mode = "recent_first"
        reason = "Quality posture is stable; continue deterministic monitoring with recent-first review."

    return {
        "priority_mode": priority_mode,
        "action": action,
        "reason": reason,
    }


def _is_missing_continuity_table_error(exc: psycopg.errors.UndefinedTable) -> bool:
    message = str(exc)
    return (
        "continuity_objects" in message
        or "continuity_capture_events" in message
        or "continuity_correction_events" in message
    )


def _summarize_correction_freshness(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> MemoryTrustCorrectionFreshnessSummary:
    try:
        weekly_rollup = compile_continuity_weekly_review(
            store,
            user_id=user_id,
            request=ContinuityWeeklyReviewRequestInput(),
        )["review"]["rollup"]
    except psycopg.errors.UndefinedTable as exc:
        if not _is_missing_continuity_table_error(exc):
            raise
        return {
            "total_open_loop_count": 0,
            "stale_open_loop_count": 0,
            "correction_recurrence_count": 0,
            "freshness_drift_count": 0,
        }

    return {
        "total_open_loop_count": weekly_rollup["total_count"],
        "stale_open_loop_count": weekly_rollup["stale_count"],
        "correction_recurrence_count": weekly_rollup["correction_recurrence_count"],
        "freshness_drift_count": weekly_rollup["freshness_drift_count"],
    }


def _determine_memory_quality_gate_status(
    *,
    adjudicated_sample_count: int,
    minimum_adjudicated_sample: int,
    precision: float | None,
    precision_target: float,
    unlabeled_memory_count: int,
    high_risk_memory_count: int,
    stale_truth_count: int,
    superseded_active_conflict_count: int,
) -> MemoryQualityGateStatus:
    if adjudicated_sample_count < minimum_adjudicated_sample:
        return "insufficient_sample"

    if precision is None or precision < precision_target:
        return "degraded"

    if superseded_active_conflict_count > 0:
        return "degraded"

    if unlabeled_memory_count > 0 or high_risk_memory_count > 0 or stale_truth_count > 0:
        return "needs_review"

    return "healthy"


def _count_superseded_active_conflicts(
    store: ContinuityStore,
    *,
    active_memories: list[MemoryRow],
) -> int:
    conflicted_count = 0
    for memory in active_memories:
        counts = _summarize_memory_review_label_counts(
            store.list_memory_review_label_counts(memory["id"])
        )
        if counts["outdated"] > 0:
            conflicted_count += 1
    return conflicted_count


def get_memory_quality_gate_summary(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> MemoryQualityGateResponse:
    del user_id

    active_memory_count = store.count_memories(status="active")
    active_memories = (
        []
        if active_memory_count == 0
        else store.list_review_memories(status="active", limit=active_memory_count)
    )
    unlabeled_memory_count = store.count_unlabeled_review_memories()
    high_risk_memory_count = sum(1 for memory in active_memories if _is_high_risk_memory(memory))
    stale_truth_count = sum(1 for memory in active_memories if _is_stale_truth_memory(memory))
    active_label_counts = _summarize_memory_review_label_counts(
        store.list_active_memory_review_label_counts()
    )
    adjudicated_correct_count = active_label_counts["correct"]
    adjudicated_incorrect_count = active_label_counts["incorrect"]
    adjudicated_sample_count = adjudicated_correct_count + adjudicated_incorrect_count
    precision = _calculate_memory_precision(
        correct_count=adjudicated_correct_count,
        incorrect_count=adjudicated_incorrect_count,
    )
    minimum_adjudicated_sample = MEMORY_QUALITY_MIN_ADJUDICATED_SAMPLE
    precision_target = MEMORY_QUALITY_PRECISION_TARGET
    remaining_to_minimum_sample = max(0, minimum_adjudicated_sample - adjudicated_sample_count)
    labeled_active_memory_count = max(0, active_memory_count - unlabeled_memory_count)
    superseded_active_conflict_count = _count_superseded_active_conflicts(
        store,
        active_memories=active_memories,
    )

    counts: MemoryQualityGateComputationCounts = {
        "active_memory_count": active_memory_count,
        "labeled_active_memory_count": labeled_active_memory_count,
        "adjudicated_correct_count": adjudicated_correct_count,
        "adjudicated_incorrect_count": adjudicated_incorrect_count,
        "outdated_label_count": active_label_counts["outdated"],
        "insufficient_evidence_label_count": active_label_counts["insufficient_evidence"],
    }
    summary: MemoryQualityGateSummary = {
        "status": _determine_memory_quality_gate_status(
            adjudicated_sample_count=adjudicated_sample_count,
            minimum_adjudicated_sample=minimum_adjudicated_sample,
            precision=precision,
            precision_target=precision_target,
            unlabeled_memory_count=unlabeled_memory_count,
            high_risk_memory_count=high_risk_memory_count,
            stale_truth_count=stale_truth_count,
            superseded_active_conflict_count=superseded_active_conflict_count,
        ),
        "precision": precision,
        "precision_target": precision_target,
        "adjudicated_sample_count": adjudicated_sample_count,
        "minimum_adjudicated_sample": minimum_adjudicated_sample,
        "remaining_to_minimum_sample": remaining_to_minimum_sample,
        "unlabeled_memory_count": unlabeled_memory_count,
        "high_risk_memory_count": high_risk_memory_count,
        "stale_truth_count": stale_truth_count,
        "superseded_active_conflict_count": superseded_active_conflict_count,
        "counts": counts,
    }
    return {
        "summary": summary,
    }


def get_memory_trust_dashboard_summary(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> MemoryTrustDashboardResponse:
    quality_gate_summary = get_memory_quality_gate_summary(
        store,
        user_id=user_id,
    )["summary"]

    queue_priority_mode = DEFAULT_MEMORY_REVIEW_QUEUE_PRIORITY_MODE
    unlabeled_queue_count = store.count_unlabeled_review_memories()
    queue_candidates = (
        []
        if unlabeled_queue_count == 0
        else store.list_unlabeled_review_memories(limit=None)
    )
    queue_memories = _order_review_queue_memories(
        queue_candidates,
        priority_mode=queue_priority_mode,
    )
    queue_posture = _summarize_queue_posture(
        queue_memories=queue_memories,
        priority_mode=queue_priority_mode,
    )

    retrieval_quality_summary = get_retrieval_evaluation_summary(
        store,
        user_id=user_id,
    )["summary"]
    correction_freshness = _summarize_correction_freshness(
        store,
        user_id=user_id,
    )
    recommended_review = _determine_recommended_review(
        quality_gate=quality_gate_summary,
        queue_posture=queue_posture,
        correction_freshness=correction_freshness,
    )

    dashboard: MemoryTrustDashboardSummary = {
        "quality_gate": quality_gate_summary,
        "queue_posture": queue_posture,
        "retrieval_quality": retrieval_quality_summary,
        "correction_freshness": correction_freshness,
        "recommended_review": recommended_review,
        "sources": [
            "memories",
            "memory_review_labels",
            "continuity_recall",
            "continuity_correction_events",
            "retrieval_evaluation_fixtures",
        ],
    }
    return {"dashboard": dashboard}


def _normalize_duplicate_value(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        return repr(value)


def _memory_duplicate_groups(active_memories: list[MemoryRow]) -> list[MemoryDuplicateGroupRecord]:
    groups: dict[tuple[str, str], list[MemoryRow]] = defaultdict(list)
    for memory in active_memories:
        groups[(memory["memory_type"], _normalize_duplicate_value(memory["value"]))].append(memory)

    duplicate_groups: list[MemoryDuplicateGroupRecord] = []
    for (memory_type, normalized_value), grouped_memories in groups.items():
        if len(grouped_memories) < 2:
            continue
        ordered = sorted(
            grouped_memories,
            key=lambda memory: (memory["updated_at"], memory["created_at"], str(memory["id"])),
            reverse=True,
        )
        duplicate_groups.append(
            {
                "group_key": f"{memory_type}:{normalized_value}",
                "memory_type": memory_type,
                "normalized_value": normalized_value,
                "count": len(ordered),
                "memory_ids": [str(memory["id"]) for memory in ordered],
                "memory_keys": [memory["memory_key"] for memory in ordered],
                "latest_updated_at": ordered[0]["updated_at"].isoformat(),
            }
        )

    return sorted(
        duplicate_groups,
        key=lambda group: (group["count"], group["latest_updated_at"], group["group_key"]),
        reverse=True,
    )


def _memory_hygiene_focus(
    *,
    kind: MemoryHygieneFocusKind,
    posture: MemoryHygienePosture,
    count: int,
    reason: str,
    action: str,
    sample_ids: list[str],
) -> MemoryHygieneFocusRecord:
    return {
        "kind": kind,
        "posture": posture,
        "count": count,
        "reason": reason,
        "action": action,
        "sample_ids": sample_ids,
    }


def _review_queue_pressure(queue_posture: MemoryTrustQueuePostureSummary) -> MemoryReviewQueuePressureSummary:
    stale_over_72h_count = queue_posture["aging"]["stale_over_72h_count"]
    aging_24h_to_72h_count = queue_posture["aging"]["aging_24h_to_72h_count"]
    total_count = queue_posture["total_count"]

    posture: MemoryHygienePosture
    reason: str
    if stale_over_72h_count > 0 or total_count >= 10:
        posture = "critical"
        reason = "Review backlog contains stale queue items or has grown beyond the bounded operating range."
    elif total_count > 0 or aging_24h_to_72h_count > 0:
        posture = "watch"
        reason = "Review backlog exists and should be drained before it becomes stale."
    else:
        posture = "healthy"
        reason = "Review backlog is clear."

    return {
        "posture": posture,
        "total_count": total_count,
        "stale_over_72h_count": stale_over_72h_count,
        "aging_24h_to_72h_count": aging_24h_to_72h_count,
        "reason": reason,
    }


def _missing_contradiction_tables(exc: psycopg.errors.UndefinedTable) -> bool:
    message = str(exc)
    return (
        "continuity_objects" in message
        or "contradiction_cases" in message
        or "trust_signals" in message
    )


def get_memory_hygiene_dashboard_summary(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> MemoryHygieneDashboardResponse:
    active_memory_count = store.count_memories(status="active")
    active_memories = (
        []
        if active_memory_count == 0
        else store.list_review_memories(status="active", limit=active_memory_count)
    )
    duplicate_groups = _memory_duplicate_groups(active_memories)
    duplicate_memory_count = sum(group["count"] for group in duplicate_groups)
    stale_facts = [memory for memory in active_memories if _is_stale_truth_memory(memory)]
    weak_trust_memories: list[MemoryRow] = []
    for memory in active_memories:
        confidence = memory.get("confidence")
        if (
            memory.get("promotion_eligibility") == "not_promotable"
            or memory.get("trust_class") == "llm_single_source"
            or memory.get("confirmation_status") != "confirmed"
            or confidence is None
            or confidence < MEMORY_QUALITY_HIGH_RISK_CONFIDENCE_THRESHOLD
        ):
            weak_trust_memories.append(memory)

    trust_dashboard = get_memory_trust_dashboard_summary(store, user_id=user_id)["dashboard"]
    review_queue_pressure = _review_queue_pressure(trust_dashboard["queue_posture"])

    unresolved_contradiction_count = 0
    try:
        from alicebot_api.continuity_contradictions import sync_contradiction_state_for_objects

        sync_contradiction_state_for_objects(store, continuity_object_ids=None)
        if hasattr(store, "count_contradiction_cases"):
            unresolved_contradiction_count = store.count_contradiction_cases(statuses=["open"])
    except psycopg.errors.UndefinedTable as exc:
        if not _missing_contradiction_tables(exc):
            raise

    focus: list[MemoryHygieneFocusRecord] = []
    if duplicate_groups:
        focus.append(
            _memory_hygiene_focus(
                kind="duplicates",
                posture="watch",
                count=duplicate_memory_count,
                reason="Multiple active memories share the same normalized value and should be reviewed for consolidation.",
                action="Review duplicate groups and keep one canonical fact per repeated value.",
                sample_ids=duplicate_groups[0]["memory_ids"],
            )
        )
    if stale_facts:
        focus.append(
            _memory_hygiene_focus(
                kind="stale_facts",
                posture="watch",
                count=len(stale_facts),
                reason="Facts are marked contested or bounded by expired truth windows.",
                action="Reconfirm or retire stale facts before they influence recall.",
                sample_ids=[str(memory["id"]) for memory in stale_facts[:5]],
            )
        )
    if unresolved_contradiction_count > 0:
        focus.append(
            _memory_hygiene_focus(
                kind="unresolved_contradictions",
                posture="critical",
                count=unresolved_contradiction_count,
                reason="Open contradiction cases still penalize trust and recall quality.",
                action="Resolve contradiction cases before relying on those facts in continuity surfaces.",
                sample_ids=[],
            )
        )
    if weak_trust_memories:
        focus.append(
            _memory_hygiene_focus(
                kind="weak_trust",
                posture="watch",
                count=len(weak_trust_memories),
                reason="Some active memories still rely on low-confidence or non-promotable evidence.",
                action="Add corroboration or downgrade these memories before reuse.",
                sample_ids=[str(memory["id"]) for memory in weak_trust_memories[:5]],
            )
        )
    if review_queue_pressure["total_count"] > 0:
        focus.append(
            _memory_hygiene_focus(
                kind="review_queue_pressure",
                posture=review_queue_pressure["posture"],
                count=review_queue_pressure["total_count"],
                reason=review_queue_pressure["reason"],
                action="Drain the unlabeled review queue in the recommended priority mode.",
                sample_ids=[],
            )
        )

    if unresolved_contradiction_count > 0 or review_queue_pressure["posture"] == "critical":
        posture: MemoryHygienePosture = "critical"
        reason = "Contradiction or queue pressure currently blocks a healthy memory posture."
    elif focus:
        posture = "watch"
        reason = "Memory hygiene issues are visible and should be handled before they accumulate."
    else:
        posture = "healthy"
        reason = "No duplicate, stale, contradiction, weak-trust, or queue-pressure issue is currently visible."

    dashboard: MemoryHygieneDashboardSummary = {
        "posture": posture,
        "reason": reason,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_memory_count": duplicate_memory_count,
        "stale_fact_count": len(stale_facts),
        "unresolved_contradiction_count": unresolved_contradiction_count,
        "weak_trust_count": len(weak_trust_memories),
        "review_queue_pressure": review_queue_pressure,
        "duplicate_groups": duplicate_groups,
        "focus": focus,
        "sources": [
            "memories",
            "memory_review_labels",
            "contradiction_cases",
            "trust_signals",
            "continuity_recall",
        ],
    }
    return {"dashboard": dashboard}


def _serialize_open_loop(open_loop: OpenLoopRow) -> OpenLoopRecord:
    return {
        "id": str(open_loop["id"]),
        "memory_id": None if open_loop["memory_id"] is None else str(open_loop["memory_id"]),
        "title": open_loop["title"],
        "status": _open_loop_status(open_loop["status"]),
        "opened_at": open_loop["opened_at"].isoformat(),
        "due_at": isoformat_or_none(open_loop["due_at"]),
        "resolved_at": isoformat_or_none(open_loop["resolved_at"]),
        "resolution_note": open_loop["resolution_note"],
        "created_at": open_loop["created_at"].isoformat(),
        "updated_at": open_loop["updated_at"].isoformat(),
    }


def _normalize_open_loop_status_filter(status: OpenLoopStatusFilter) -> str | None:
    if status == "all":
        return None
    return status


def _normalize_open_loop_title(
    title: str,
    *,
    error_prefix: str,
    error_type: type[ValueError],
) -> str:
    normalized = title.strip()
    if not normalized:
        raise error_type(f"{error_prefix} must be a non-empty string")
    if len(normalized) > 280:
        raise error_type(f"{error_prefix} must be 280 characters or fewer")
    return normalized


def _normalize_open_loop_resolution_note(note: str | None) -> str | None:
    if note is None:
        return None
    normalized = note.strip()
    if not normalized:
        raise OpenLoopValidationError("resolution_note must be a non-empty string when provided")
    if len(normalized) > 2000:
        raise OpenLoopValidationError("resolution_note must be 2000 characters or fewer")
    return normalized


def _validate_open_loop_status(status: str) -> str:
    if status not in OPEN_LOOP_STATUSES:
        allowed_values = ", ".join(OPEN_LOOP_STATUSES)
        raise OpenLoopValidationError(f"status must be one of: {allowed_values}")
    return status


def list_open_loop_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
    status: OpenLoopStatusFilter = "open",
    limit: int = DEFAULT_OPEN_LOOP_LIMIT,
) -> OpenLoopListResponse:
    del user_id

    normalized_status = _normalize_open_loop_status_filter(status)
    total_count = store.count_open_loops(status=normalized_status)
    open_loops = store.list_open_loops(status=normalized_status, limit=limit)
    items = [_serialize_open_loop(open_loop) for open_loop in open_loops]
    summary: OpenLoopListSummary = {
        "status": status,
        "limit": limit,
        "returned_count": len(items),
        "total_count": total_count,
        "has_more": len(items) < total_count,
        "order": list(OPEN_LOOP_REVIEW_ORDER),
    }
    return {
        "items": items,
        "summary": summary,
    }


def get_open_loop_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    open_loop_id: UUID,
) -> OpenLoopDetailResponse:
    del user_id

    open_loop = store.get_open_loop_optional(open_loop_id)
    if open_loop is None:
        raise OpenLoopNotFoundError(f"open loop {open_loop_id} was not found")
    return {
        "open_loop": _serialize_open_loop(open_loop),
    }


def create_open_loop_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    open_loop: OpenLoopCreateInput,
) -> OpenLoopCreateResponse:
    del user_id

    if open_loop.memory_id is not None:
        memory = store.get_memory_optional(open_loop.memory_id)
        if memory is None:
            raise OpenLoopValidationError(
                "memory_id must reference an existing memory owned by the user"
            )

    created = store.create_open_loop(
        memory_id=open_loop.memory_id,
        title=_normalize_open_loop_title(
            open_loop.title,
            error_prefix="title",
            error_type=OpenLoopValidationError,
        ),
        status="open",
        opened_at=None,
        due_at=open_loop.due_at,
        resolved_at=None,
        resolution_note=None,
    )
    return {
        "open_loop": _serialize_open_loop(created),
    }


def update_open_loop_status_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    open_loop_id: UUID,
    request: OpenLoopStatusUpdateInput,
) -> OpenLoopStatusUpdateResponse:
    del user_id

    existing = store.get_open_loop_optional(open_loop_id)
    if existing is None:
        raise OpenLoopNotFoundError(f"open loop {open_loop_id} was not found")

    normalized_status = _validate_open_loop_status(request.status)
    if normalized_status == "open":
        raise OpenLoopValidationError("status transition must be resolved or dismissed")
    if existing["status"] != "open":
        raise OpenLoopValidationError("open loop status can only transition from open")

    updated = store.update_open_loop_status_optional(
        open_loop_id=open_loop_id,
        status=normalized_status,
        resolved_at=None,
        resolution_note=_normalize_open_loop_resolution_note(request.resolution_note),
    )
    if updated is None:
        raise OpenLoopNotFoundError(f"open loop {open_loop_id} was not found")

    return {
        "open_loop": _serialize_open_loop(updated),
    }


def _dedupe_source_event_ids(source_event_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
    deduped: list[UUID] = []
    seen: set[UUID] = set()
    for source_event_id in source_event_ids:
        if source_event_id in seen:
            continue
        seen.add(source_event_id)
        deduped.append(source_event_id)
    return tuple(deduped)


def _resolve_agent_profile_id_from_source_events(
    store: ContinuityStore,
    *,
    source_events: list[EventRow],
) -> str:
    thread_ids = sorted({event["thread_id"] for event in source_events}, key=str)
    if not thread_ids:
        return DEFAULT_AGENT_PROFILE_ID

    resolved_profile_ids: set[str] = set()
    for thread_id in thread_ids:
        thread = store.get_thread_optional(thread_id)
        if thread is None:
            raise MemoryAdmissionValidationError(
                f"source_event thread {thread_id} was not found"
            )
        resolved_profile_ids.add(str(thread.get("agent_profile_id", DEFAULT_AGENT_PROFILE_ID)))

    if len(resolved_profile_ids) > 1:
        raise MemoryAdmissionValidationError(
            "source_event_ids must all belong to threads with the same agent_profile_id"
        )
    return next(iter(resolved_profile_ids))


def _resolve_memory_agent_profile_id(
    store: ContinuityStore,
    *,
    candidate: MemoryCandidateInput,
    derived_agent_profile_id: str,
) -> str:
    candidate_profile_id = candidate.agent_profile_id
    resolved_profile_id = (
        derived_agent_profile_id if candidate_profile_id is None else candidate_profile_id
    )
    if store.get_agent_profile_optional(resolved_profile_id) is None:
        raise MemoryAdmissionValidationError(
            f"agent_profile_id must reference an existing profile: {resolved_profile_id}"
        )
    if candidate_profile_id is not None and candidate_profile_id != derived_agent_profile_id:
        raise MemoryAdmissionValidationError(
            "agent_profile_id must match the profile resolved from source_event_ids"
        )
    return resolved_profile_id


def _validate_source_events(
    store: ContinuityStore,
    source_event_ids: tuple[UUID, ...],
) -> tuple[list[str], str]:
    normalized_event_ids = _dedupe_source_event_ids(source_event_ids)
    if not normalized_event_ids:
        raise MemoryAdmissionValidationError(
            "source_event_ids must include at least one existing event owned by the user"
        )
    source_events = store.list_events_by_ids(list(normalized_event_ids))
    found_event_ids = {event["id"] for event in source_events}
    missing_event_ids = [
        str(source_event_id)
        for source_event_id in normalized_event_ids
        if source_event_id not in found_event_ids
    ]
    if missing_event_ids:
        raise MemoryAdmissionValidationError(
            "source_event_ids must all reference existing events owned by the user: "
            + ", ".join(missing_event_ids)
        )
    derived_profile_id = _resolve_agent_profile_id_from_source_events(
        store,
        source_events=source_events,
    )
    return [str(source_event_id) for source_event_id in normalized_event_ids], derived_profile_id


def _candidate_payload(
    candidate: MemoryCandidateInput,
    *,
    resolved_agent_profile_id: str,
) -> JsonObject:
    payload = candidate.as_payload()
    payload["agent_profile_id"] = resolved_agent_profile_id
    return payload


def _create_open_loop_for_memory(
    store: ContinuityStore,
    *,
    candidate: MemoryCandidateInput,
    memory: MemoryRow,
) -> OpenLoopRecord | None:
    if candidate.open_loop is None:
        return None

    created = store.create_open_loop(
        memory_id=memory["id"],
        title=_normalize_open_loop_title(
            candidate.open_loop.title,
            error_prefix="open_loop.title",
            error_type=MemoryAdmissionValidationError,
        ),
        status="open",
        opened_at=None,
        due_at=candidate.open_loop.due_at,
        resolved_at=None,
        resolution_note=None,
    )
    return _serialize_open_loop(created)


def _validate_memory_type(memory_type: str | None) -> MemoryType | None:
    if memory_type is None:
        return None
    if memory_type not in MEMORY_TYPES:
        allowed_values = ", ".join(MEMORY_TYPES)
        raise MemoryAdmissionValidationError(f"memory_type must be one of: {allowed_values}")
    return cast(MemoryType, memory_type)


def _validate_confirmation_status(
    confirmation_status: str | None,
) -> MemoryConfirmationStatus | None:
    if confirmation_status is None:
        return None
    if confirmation_status not in MEMORY_CONFIRMATION_STATUSES:
        allowed_values = ", ".join(MEMORY_CONFIRMATION_STATUSES)
        raise MemoryAdmissionValidationError(
            f"confirmation_status must be one of: {allowed_values}"
        )
    return cast(MemoryConfirmationStatus, confirmation_status)


def _validate_trust_class(trust_class: str | None) -> MemoryTrustClass | None:
    if trust_class is None:
        return None
    if trust_class not in MEMORY_TRUST_CLASSES:
        allowed_values = ", ".join(MEMORY_TRUST_CLASSES)
        raise MemoryAdmissionValidationError(f"trust_class must be one of: {allowed_values}")
    return cast(MemoryTrustClass, trust_class)


def _validate_promotion_eligibility(
    promotion_eligibility: str | None,
) -> MemoryPromotionEligibility | None:
    if promotion_eligibility is None:
        return None
    if promotion_eligibility not in MEMORY_PROMOTION_ELIGIBILITIES:
        allowed_values = ", ".join(MEMORY_PROMOTION_ELIGIBILITIES)
        raise MemoryAdmissionValidationError(
            f"promotion_eligibility must be one of: {allowed_values}"
        )
    return cast(MemoryPromotionEligibility, promotion_eligibility)


def _default_promotion_eligibility_for_trust_class(
    trust_class: MemoryTrustClass,
) -> MemoryPromotionEligibility:
    if trust_class == "llm_single_source":
        return "not_promotable"
    return DEFAULT_MEMORY_PROMOTION_ELIGIBILITY


def _validate_score(name: str, score: float | None) -> float | None:
    if score is None:
        return None
    normalized = float(score)
    if normalized < 0.0 or normalized > 1.0:
        raise MemoryAdmissionValidationError(f"{name} must be between 0.0 and 1.0")
    return normalized


def _validate_temporal_range(valid_from: datetime | None, valid_to: datetime | None) -> None:
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        raise MemoryAdmissionValidationError("valid_to must be greater than or equal to valid_from")


def _validate_count(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if normalized < 0:
        raise MemoryAdmissionValidationError(f"{name} must be greater than or equal to 0")
    return normalized


def _normalize_optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    if normalized == "":
        raise MemoryAdmissionValidationError(f"{name} must not be empty")
    return normalized


def _resolve_memory_typed_metadata(
    *,
    existing_memory: MemoryRow | None,
    candidate: MemoryCandidateInput,
) -> _ResolvedMemoryMetadata:
    memory_type = _validate_memory_type(candidate.memory_type)
    confirmation_status = _validate_confirmation_status(candidate.confirmation_status)
    trust_class = _validate_trust_class(candidate.trust_class)
    promotion_eligibility = _validate_promotion_eligibility(candidate.promotion_eligibility)
    confidence = _validate_score("confidence", candidate.confidence)
    salience = _validate_score("salience", candidate.salience)
    evidence_count = _validate_count("evidence_count", candidate.evidence_count)
    independent_source_count = _validate_count(
        "independent_source_count",
        candidate.independent_source_count,
    )
    extracted_by_model = _normalize_optional_text("extracted_by_model", candidate.extracted_by_model)
    trust_reason = _normalize_optional_text("trust_reason", candidate.trust_reason)
    _validate_temporal_range(candidate.valid_from, candidate.valid_to)

    if existing_memory is None:
        resolved_trust_class = trust_class or DEFAULT_MEMORY_TRUST_CLASS
        return {
            "memory_type": memory_type or DEFAULT_MEMORY_TYPE,
            "confidence": confidence,
            "salience": salience,
            "confirmation_status": confirmation_status or DEFAULT_MEMORY_CONFIRMATION_STATUS,
            "trust_class": resolved_trust_class,
            "promotion_eligibility": (
                promotion_eligibility
                if promotion_eligibility is not None
                else _default_promotion_eligibility_for_trust_class(resolved_trust_class)
            ),
            "evidence_count": evidence_count,
            "independent_source_count": independent_source_count,
            "extracted_by_model": extracted_by_model,
            "trust_reason": trust_reason,
            "valid_from": candidate.valid_from,
            "valid_to": candidate.valid_to,
            "last_confirmed_at": candidate.last_confirmed_at,
        }

    existing_trust_class = _validate_trust_class(
        existing_memory.get("trust_class", DEFAULT_MEMORY_TRUST_CLASS)
    ) or DEFAULT_MEMORY_TRUST_CLASS
    resolved_trust_class = trust_class if trust_class is not None else existing_trust_class
    resolved_promotion_eligibility: MemoryPromotionEligibility
    if promotion_eligibility is not None:
        resolved_promotion_eligibility = promotion_eligibility
    elif trust_class is not None:
        resolved_promotion_eligibility = _default_promotion_eligibility_for_trust_class(
            resolved_trust_class
        )
    else:
        resolved_promotion_eligibility = (
            _validate_promotion_eligibility(existing_memory.get("promotion_eligibility"))
            or _default_promotion_eligibility_for_trust_class(resolved_trust_class)
        )

    return {
        "memory_type": (
            memory_type
            if memory_type is not None
            else _validate_memory_type(existing_memory.get("memory_type"))
            or DEFAULT_MEMORY_TYPE
        ),
        "confidence": confidence if confidence is not None else existing_memory.get("confidence"),
        "salience": salience if salience is not None else existing_memory.get("salience"),
        "confirmation_status": (
            confirmation_status
            if confirmation_status is not None
            else _validate_confirmation_status(existing_memory.get("confirmation_status"))
            or DEFAULT_MEMORY_CONFIRMATION_STATUS
        ),
        "trust_class": resolved_trust_class,
        "promotion_eligibility": resolved_promotion_eligibility,
        "evidence_count": (
            evidence_count if evidence_count is not None else existing_memory.get("evidence_count")
        ),
        "independent_source_count": (
            independent_source_count
            if independent_source_count is not None
            else existing_memory.get("independent_source_count")
        ),
        "extracted_by_model": (
            extracted_by_model
            if extracted_by_model is not None
            else existing_memory.get("extracted_by_model")
        ),
        "trust_reason": trust_reason if trust_reason is not None else existing_memory.get("trust_reason"),
        "valid_from": candidate.valid_from if candidate.valid_from is not None else existing_memory.get("valid_from"),
        "valid_to": candidate.valid_to if candidate.valid_to is not None else existing_memory.get("valid_to"),
        "last_confirmed_at": (
            candidate.last_confirmed_at
            if candidate.last_confirmed_at is not None
            else existing_memory.get("last_confirmed_at")
        ),
    }


def admit_memory_candidate(
    store: ContinuityStore,
    *,
    user_id: UUID,
    candidate: MemoryCandidateInput,
) -> AdmissionDecisionOutput:
    del user_id

    # The credential floor, before any branch (owner ruling R1, 2026-09-23).
    # All four legacy admit routes land here, and each branch writes: ADD and
    # UPDATE write the value, and even the NOOP-unchanged branch writes the
    # candidate's open-loop title. Until this change none of them checked.
    open_loop_title = candidate.open_loop.title if candidate.open_loop is not None else None
    refuse_credential_material(
        candidate.memory_key,
        candidate.value,
        open_loop_title,
        error=MemoryAdmissionValidationError,
    )

    source_event_ids, derived_agent_profile_id = _validate_source_events(
        store,
        candidate.source_event_ids,
    )
    agent_profile_id = _resolve_memory_agent_profile_id(
        store,
        candidate=candidate,
        derived_agent_profile_id=derived_agent_profile_id,
    )
    existing_memory = store.get_memory_by_key_and_profile(
        memory_key=candidate.memory_key,
        agent_profile_id=agent_profile_id,
    )
    resolved_metadata = _resolve_memory_typed_metadata(
        existing_memory=existing_memory,
        candidate=candidate,
    )

    noop_decision = AdmissionDecisionOutput(
        action="NOOP",
        reason="candidate_default_noop",
        memory=None,
        revision=None,
    )

    if candidate.delete_requested:
        if existing_memory is None or existing_memory["status"] == "deleted":
            return AdmissionDecisionOutput(
                action=noop_decision.action,
                reason="memory_not_found_for_delete",
                memory=None if existing_memory is None else _serialize_memory(existing_memory),
                revision=None,
            )

        memory = store.update_memory(
            memory_id=existing_memory["id"],
            value=existing_memory["value"],
            status="deleted",
            source_event_ids=source_event_ids,
            memory_type=resolved_metadata["memory_type"],
            confidence=resolved_metadata["confidence"],
            salience=resolved_metadata["salience"],
            confirmation_status=resolved_metadata["confirmation_status"],
            trust_class=resolved_metadata["trust_class"],
            promotion_eligibility=resolved_metadata["promotion_eligibility"],
            evidence_count=resolved_metadata["evidence_count"],
            independent_source_count=resolved_metadata["independent_source_count"],
            extracted_by_model=resolved_metadata["extracted_by_model"],
            trust_reason=resolved_metadata["trust_reason"],
            valid_from=resolved_metadata["valid_from"],
            valid_to=resolved_metadata["valid_to"],
            last_confirmed_at=resolved_metadata["last_confirmed_at"],
        )
        revision = store.append_memory_revision(
            memory_id=memory["id"],
            action="DELETE",
            memory_key=memory["memory_key"],
            previous_value=existing_memory["value"],
            new_value=None,
            source_event_ids=source_event_ids,
            candidate=_candidate_payload(
                candidate,
                resolved_agent_profile_id=agent_profile_id,
            ),
        )
        return AdmissionDecisionOutput(
            action="DELETE",
            reason="source_backed_delete",
            memory=_serialize_memory(memory),
            revision=_serialize_memory_revision(revision),
        )

    if candidate.value is None:
        return AdmissionDecisionOutput(
            action=noop_decision.action,
            reason="candidate_value_missing",
            memory=None if existing_memory is None else _serialize_memory(existing_memory),
            revision=None,
        )

    if existing_memory is None:
        memory = store.create_memory(
            memory_key=candidate.memory_key,
            value=candidate.value,
            status="active",
            source_event_ids=source_event_ids,
            memory_type=resolved_metadata["memory_type"],
            confidence=resolved_metadata["confidence"],
            salience=resolved_metadata["salience"],
            confirmation_status=resolved_metadata["confirmation_status"],
            trust_class=resolved_metadata["trust_class"],
            promotion_eligibility=resolved_metadata["promotion_eligibility"],
            evidence_count=resolved_metadata["evidence_count"],
            independent_source_count=resolved_metadata["independent_source_count"],
            extracted_by_model=resolved_metadata["extracted_by_model"],
            trust_reason=resolved_metadata["trust_reason"],
            valid_from=resolved_metadata["valid_from"],
            valid_to=resolved_metadata["valid_to"],
            last_confirmed_at=resolved_metadata["last_confirmed_at"],
            agent_profile_id=agent_profile_id,
        )
        revision = store.append_memory_revision(
            memory_id=memory["id"],
            action="ADD",
            memory_key=memory["memory_key"],
            previous_value=None,
            new_value=candidate.value,
            source_event_ids=source_event_ids,
            candidate=_candidate_payload(
                candidate,
                resolved_agent_profile_id=agent_profile_id,
            ),
        )
        return AdmissionDecisionOutput(
            action="ADD",
            reason="source_backed_add",
            memory=_serialize_memory(memory),
            revision=_serialize_memory_revision(revision),
            open_loop=_create_open_loop_for_memory(
                store,
                candidate=candidate,
                memory=memory,
            ),
        )

    metadata_changed = any(
        (
            existing_memory.get("memory_type") != resolved_metadata["memory_type"],
            existing_memory.get("confidence") != resolved_metadata["confidence"],
            existing_memory.get("salience") != resolved_metadata["salience"],
            existing_memory.get("confirmation_status")
            != resolved_metadata["confirmation_status"],
            existing_memory.get("trust_class") != resolved_metadata["trust_class"],
            existing_memory.get("promotion_eligibility")
            != resolved_metadata["promotion_eligibility"],
            existing_memory.get("evidence_count") != resolved_metadata["evidence_count"],
            existing_memory.get("independent_source_count")
            != resolved_metadata["independent_source_count"],
            existing_memory.get("extracted_by_model")
            != resolved_metadata["extracted_by_model"],
            existing_memory.get("trust_reason") != resolved_metadata["trust_reason"],
            existing_memory.get("valid_from") != resolved_metadata["valid_from"],
            existing_memory.get("valid_to") != resolved_metadata["valid_to"],
            existing_memory.get("last_confirmed_at")
            != resolved_metadata["last_confirmed_at"],
        )
    )

    if existing_memory["status"] == "active" and existing_memory["value"] == candidate.value and not metadata_changed:
        return AdmissionDecisionOutput(
            action=noop_decision.action,
            reason="memory_unchanged",
            memory=_serialize_memory(existing_memory),
            revision=None,
            open_loop=_create_open_loop_for_memory(
                store,
                candidate=candidate,
                memory=existing_memory,
            ),
        )

    memory = store.update_memory(
        memory_id=existing_memory["id"],
        value=candidate.value,
        status="active",
        source_event_ids=source_event_ids,
        memory_type=resolved_metadata["memory_type"],
        confidence=resolved_metadata["confidence"],
        salience=resolved_metadata["salience"],
        confirmation_status=resolved_metadata["confirmation_status"],
        trust_class=resolved_metadata["trust_class"],
        promotion_eligibility=resolved_metadata["promotion_eligibility"],
        evidence_count=resolved_metadata["evidence_count"],
        independent_source_count=resolved_metadata["independent_source_count"],
        extracted_by_model=resolved_metadata["extracted_by_model"],
        trust_reason=resolved_metadata["trust_reason"],
        valid_from=resolved_metadata["valid_from"],
        valid_to=resolved_metadata["valid_to"],
        last_confirmed_at=resolved_metadata["last_confirmed_at"],
    )
    revision = store.append_memory_revision(
        memory_id=memory["id"],
        action="UPDATE",
        memory_key=memory["memory_key"],
        previous_value=existing_memory["value"],
        new_value=candidate.value,
        source_event_ids=source_event_ids,
        candidate=_candidate_payload(
            candidate,
            resolved_agent_profile_id=agent_profile_id,
        ),
    )
    return AdmissionDecisionOutput(
        action="UPDATE",
        reason="source_backed_update",
        memory=_serialize_memory(memory),
        revision=_serialize_memory_revision(revision),
        open_loop=_create_open_loop_for_memory(
            store,
            candidate=candidate,
            memory=memory,
        ),
    )
