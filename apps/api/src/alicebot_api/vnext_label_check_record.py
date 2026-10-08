"""The recorded result of the last full derived-labels check on PostgreSQL.

A full check settles every derived row, so a workspace page cannot afford one
on each request. The owner commands that run a full check (``labels check``,
``labels repair`` and ``alicebot vnext doctor``) append one ``labels.checked``
event. The workspace reads the newest event, shows its result and time, and
says when the label inputs differ from the ones that check read.

The record is an explicit event in the audit log, not a cache. Nothing here
decides who may read a row. Every guarded read still settles labels itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from alicebot_api.vnext_event_log import build_event_log_record

LABEL_CHECK_EVENT = "labels.checked"
LABEL_CHECK_CAUSES = frozenset({"labels_check", "labels_repair", "doctor"})

# Each select names the columns the label planner reads (INPUT_SELECTS_V3).
# A timestamp is reduced to "is it set" so the text of a row does not depend
# on the session time zone. A unit test keeps this list equal to the planner's.
FINGERPRINT_SELECTS = {
    "sources": "SELECT id, user_id, domain, sensitivity, metadata_json, deleted_at IS NOT NULL AS deleted FROM sources",
    "memories": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, value, project_id, "
        "deleted_at IS NOT NULL AS deleted FROM memories"
    ),
    "open_loops": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, project_id, source_id, memory_id FROM open_loops"
    ),
    "generated_artifacts": (
        "SELECT id, user_id, domain, sensitivity, metadata_json, artifact_type FROM generated_artifacts"
    ),
    "projects": "SELECT id, user_id, domain, sensitivity, metadata_json FROM projects",
    "beliefs": "SELECT id, user_id, memory_id FROM beliefs",
}


def label_input_fingerprint(conn: Any) -> str:
    """A digest of every row the label planner reads, for the current identity.

    Per table: the row count and the sum of a 64-bit hash of each row's text.
    The sum does not depend on row order. Two equal digests mean the planner
    would read the same inputs, so a recorded result still describes them.
    """

    parts: dict[str, list[str]] = {}
    for table, select in FINGERPRINT_SELECTS.items():
        row = conn.execute(
            f"SELECT count(*) AS rows, COALESCE(sum(hashtextextended(t::text, 0)), 0) AS digest FROM ({select}) AS t"
        ).fetchone()
        count, digest = (row["rows"], row["digest"]) if isinstance(row, Mapping) else (row[0], row[1])
        parts[table] = [str(int(count)), str(Decimal(digest))]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def record_label_check(store: Any, *, below: int, unverified: int, fingerprint: str, cause: str) -> None:
    """Append the result of one full check. The caller owns the transaction."""

    if cause not in LABEL_CHECK_CAUSES:
        raise ValueError("unknown label check cause")
    store.append_event(
        build_event_log_record(
            event_type=LABEL_CHECK_EVENT,
            actor_type="system",
            payload={
                "below_inputs": int(below),
                "unverified": int(unverified),
                "fingerprint": str(fingerprint),
                "cause": cause,
            },
        )
    )


@dataclass(frozen=True, slots=True)
class RecordedLabelCheck:
    """The newest recorded check, or the fact that it cannot be read."""

    readable: bool
    checked_at: str = ""
    below_inputs: int = 0
    unverified: int = 0
    cause: str = ""
    fingerprint: str = ""


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


def recorded_label_check(store: Any) -> RecordedLabelCheck | None:
    """The newest ``labels.checked`` event of the current identity, or None."""

    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT occurred_at, payload_json FROM event_log WHERE event_type = %s "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (LABEL_CHECK_EVENT,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    occurred_at, payload = (row["occurred_at"], row["payload_json"]) if isinstance(row, Mapping) else (row[0], row[1])
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    below = _count(payload.get("below_inputs")) if isinstance(payload, Mapping) else None
    unverified = _count(payload.get("unverified")) if isinstance(payload, Mapping) else None
    fingerprint = payload.get("fingerprint") if isinstance(payload, Mapping) else None
    cause = payload.get("cause") if isinstance(payload, Mapping) else None
    if below is None or unverified is None or not isinstance(fingerprint, str) or cause not in LABEL_CHECK_CAUSES:
        return RecordedLabelCheck(readable=False, checked_at=_timestamp(occurred_at))
    return RecordedLabelCheck(
        readable=True,
        checked_at=_timestamp(occurred_at),
        below_inputs=below,
        unverified=unverified,
        cause=str(cause),
        fingerprint=fingerprint,
    )


__all__ = [
    "FINGERPRINT_SELECTS",
    "LABEL_CHECK_CAUSES",
    "LABEL_CHECK_EVENT",
    "RecordedLabelCheck",
    "label_input_fingerprint",
    "record_label_check",
    "recorded_label_check",
]
