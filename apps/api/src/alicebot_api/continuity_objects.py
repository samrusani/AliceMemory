from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from alicebot_api.continuity_contradictions import sync_contradiction_state_for_objects
from alicebot_api.contracts import (
    CONTINUITY_OBJECT_TYPES,
    ContinuityLifecycleStateRecord,
    ContinuityObjectRecord,
    ContinuityObjectType,
)
from alicebot_api.credential_floor import (
    CREDENTIAL_MATERIAL_REFUSED_MESSAGE,
    EXPANSION_REFUSED_MESSAGE,
    VERDICT_EXPANSION,
    refuse_credential_material,
    string_values,
)
from alicebot_api.legacy_credential_check import commit_door_secret_verdict
from alicebot_api.store import ContinuityObjectRow, ContinuityStore, JsonObject


class ContinuityObjectValidationError(ValueError):
    """Raised when a continuity object request is invalid."""


def default_continuity_searchable(object_type: str) -> bool:
    return object_type != "Note"


def default_continuity_promotable(object_type: str) -> bool:
    return object_type in {"Decision", "Commitment", "WaitingFor", "Blocker", "NextAction"}


def serialize_continuity_lifecycle_state(
    *,
    is_preserved: bool,
    is_searchable: bool,
    is_promotable: bool,
) -> ContinuityLifecycleStateRecord:
    return {
        "is_preserved": is_preserved,
        "preservation_status": "preserved" if is_preserved else "not_preserved",
        "is_searchable": is_searchable,
        "searchability_status": "searchable" if is_searchable else "not_searchable",
        "is_promotable": is_promotable,
        "promotion_status": "promotable" if is_promotable else "not_promotable",
    }


def serialize_continuity_lifecycle_state_from_record(
    record: Mapping[str, object],
) -> ContinuityLifecycleStateRecord:
    object_type = str(record["object_type"])
    return serialize_continuity_lifecycle_state(
        is_preserved=bool(record.get("is_preserved", True)),
        is_searchable=bool(record.get("is_searchable", default_continuity_searchable(object_type))),
        is_promotable=bool(record.get("is_promotable", default_continuity_promotable(object_type))),
    )


def _serialize_continuity_object(record: ContinuityObjectRow) -> ContinuityObjectRecord:
    return {
        "id": str(record["id"]),
        "capture_event_id": str(record["capture_event_id"]),
        "object_type": cast(ContinuityObjectType, record["object_type"]),
        "status": record["status"],
        "lifecycle": serialize_continuity_lifecycle_state_from_record(record),
        "title": record["title"],
        "body": record["body"],
        "provenance": record["provenance"],
        "confidence": record["confidence"],
        "created_at": record["created_at"].isoformat(),
        "updated_at": record["updated_at"].isoformat(),
    }


def _validate_object_type(object_type: str) -> None:
    if object_type not in CONTINUITY_OBJECT_TYPES:
        allowed = ", ".join(CONTINUITY_OBJECT_TYPES)
        raise ContinuityObjectValidationError(
            f"object_type must be one of: {allowed}"
        )


def _validate_title(title: str) -> None:
    normalized = title.strip()
    if not normalized:
        raise ContinuityObjectValidationError("title must not be empty")
    if len(normalized) > 280:
        raise ContinuityObjectValidationError("title must be 280 characters or fewer")


def _validate_confidence(confidence: float) -> None:
    if confidence < 0.0 or confidence > 1.0:
        raise ContinuityObjectValidationError("confidence must be between 0.0 and 1.0")


def _commit_door_text(value: object) -> str:
    """The field's string values, in reading order.

    The commit door reads those strings. A JSON dump of the body is not
    that text: a quote becomes a backslash, and the prose rule counts the
    backslash. The same dump also hides a quoted assignment.
    """

    if isinstance(value, str):
        return value
    return "\n".join(string_values(value))


def create_continuity_object_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    capture_event_id: UUID,
    object_type: str,
    title: str,
    body: JsonObject,
    provenance: JsonObject,
    confidence: float,
    status: str = "active",
    is_preserved: bool = True,
    is_searchable: bool | None = None,
    is_promotable: bool | None = None,
) -> ContinuityObjectRecord:
    del user_id

    _validate_object_type(object_type)
    _validate_title(title)
    _validate_confidence(confidence)
    # The credential floor. Every legacy path that creates a continuity
    # object comes through here: /v1 memory operation commit (ADD), capture
    # commit, and explicit-signal capture. Until 2026-09-22 none of them
    # consulted it. Raising rolls back the caller's transaction, including
    # the capture event written just before this call. Provenance is
    # read with its keys. The commit door then reads the body's string
    # values and the provenance values, not a JSON dump.
    refuse_credential_material(title, body, provenance, error=ContinuityObjectValidationError)
    # commit_door_secret_verdict runs the floor, then commit_gate_refuses.
    # The gate sees the body's string values, the same text the commit door
    # would read, not a JSON dump.
    verdict = commit_door_secret_verdict(
        title,
        _commit_door_text(body),
        None,
        _commit_door_text(provenance) or None,
        (),
    )
    if verdict == VERDICT_EXPANSION:
        raise ContinuityObjectValidationError(EXPANSION_REFUSED_MESSAGE)
    if verdict is not None:
        raise ContinuityObjectValidationError(CREDENTIAL_MATERIAL_REFUSED_MESSAGE)
    resolved_is_searchable = (
        default_continuity_searchable(object_type)
        if is_searchable is None
        else is_searchable
    )
    resolved_is_promotable = (
        default_continuity_promotable(object_type)
        if is_promotable is None
        else is_promotable
    )

    row = store.create_continuity_object(
        capture_event_id=capture_event_id,
        object_type=object_type,
        status=status,
        is_preserved=is_preserved,
        is_searchable=resolved_is_searchable,
        is_promotable=resolved_is_promotable,
        title=title.strip(),
        body=body,
        provenance=provenance,
        confidence=confidence,
    )
    sync_contradiction_state_for_objects(
        store,
        continuity_object_ids=[row["id"]],
    )
    return _serialize_continuity_object(row)


def get_continuity_object_for_capture_event(
    store: ContinuityStore,
    *,
    user_id: UUID,
    capture_event_id: UUID,
) -> ContinuityObjectRecord | None:
    del user_id

    row = store.get_continuity_object_by_capture_event_optional(capture_event_id)
    if row is None:
        return None
    return _serialize_continuity_object(row)


def list_continuity_objects_for_capture_events(
    store: ContinuityStore,
    *,
    user_id: UUID,
    capture_event_ids: list[UUID],
) -> dict[str, ContinuityObjectRecord]:
    del user_id

    rows = store.list_continuity_objects_for_capture_events(capture_event_ids)
    serialized = [_serialize_continuity_object(row) for row in rows]
    return {item["capture_event_id"]: item for item in serialized}
