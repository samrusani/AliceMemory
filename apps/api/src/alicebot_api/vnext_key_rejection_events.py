"""Keep a bounded key-refusal record after the refused transaction rolls back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import sqlite3
import unicodedata
from typing import Mapping
from uuid import UUID

from alicebot_api.credential_floor import carries_credential_material
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_repositories import JsonObject

logger = logging.getLogger(__name__)
CLAIM_VALUE_LIMIT = 128
CLAIM_LIST_LIMIT = 8
REJECTION_WINDOW_SECONDS = 60
WITHHELD = "[withheld]"
EVENT_TYPE = "agent.key_escalation_rejected"


@dataclass(frozen=True)
class KeyRejection:
    key_id: str
    payload: JsonObject


def _clean(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    # Check the complete value before truncation, and its control-free form too.
    clean = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"})
    if carries_credential_material(text) or carries_credential_material(clean):
        return WITHHELD
    return clean[:CLAIM_VALUE_LIMIT]


def rejection_notice(record: Mapping[str, object], reason: str, claimed: JsonObject) -> KeyRejection:
    payload: JsonObject = {
        "reason": reason,
        "key_prefix": _clean(record.get("key_prefix") or ""),
        "agent_id": _clean(record.get("agent_id") or ""),
        "granted_permission_profile": _clean(record.get("permission_profile") or ""),
    }
    for key, value in claimed.items():
        if isinstance(value, (list, tuple)):
            payload[key] = [_clean(item) for item in value[:CLAIM_LIST_LIMIT]]
        else:
            payload[key] = _clean(value)
    return KeyRejection(key_id=str(record["id"]), payload=payload)


def _notice(error: BaseException) -> KeyRejection | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        notice = getattr(current, "key_rejection", None)
        if isinstance(notice, KeyRejection):
            return notice
        current = current.__cause__ or current.__context__
    return None


def _append_once(store: object, notice: KeyRejection) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=REJECTION_WINDOW_SECONDS)
    recent = store.list_events(  # type: ignore[attr-defined]
        target_type="agent_api_key", target_id=notice.key_id, occurred_at_start=cutoff
    )
    if any(event.get("event_type") == EVENT_TYPE and event.get("payload_json") == notice.payload for event in recent):
        return
    # This is a system audit record, not an action by the key's agent.
    append_event(store, event_type=EVENT_TYPE, actor_type="system", target_type="agent_api_key",  # type: ignore[arg-type]
                 target_id=notice.key_id, payload=notice.payload)


def persist_after_rollback(conn: object, user_id: UUID | str, error: BaseException) -> None:
    """Best effort on the same connection after rollback; never replace the original refusal."""
    notice = _notice(error)
    if notice is None:
        return
    try:
        if isinstance(conn, sqlite3.Connection):
            from alicebot_api.sqlite_store import SQLiteVNextStore

            conn.execute("BEGIN IMMEDIATE")
            _append_once(SQLiteVNextStore(conn, user_id), notice)
            conn.commit()
        else:
            from alicebot_api.db import set_current_user
            from alicebot_api.vnext_store import PostgresVNextStore

            # label state: none, the refused request's store has ended; this transaction writes only its audit event.
            with conn.transaction():  # type: ignore[attr-defined]
                set_current_user(conn, UUID(str(user_id)))  # type: ignore[arg-type]
                # Serialize retries for this key even when two requests are refused together.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(user_id) + ":" + notice.key_id,))  # type: ignore[attr-defined]
                _append_once(PostgresVNextStore(conn), notice)  # type: ignore[arg-type]
    except Exception as exc:
        if isinstance(conn, sqlite3.Connection):
            try:
                # label state: none, the refused request's store has ended; this rollback abandons only the audit write.
                conn.rollback()
            except Exception as rollback_exc:
                logger.warning("could not roll back the failed agent key refusal record: %s", type(rollback_exc).__name__)
        logger.warning("could not record an agent key identity refusal: %s", type(exc).__name__)
