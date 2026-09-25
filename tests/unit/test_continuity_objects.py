from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.continuity_objects import (
    ContinuityObjectValidationError,
    create_continuity_object_record,
    get_continuity_object_for_capture_event,
    list_continuity_objects_for_capture_events,
)
from alicebot_api.credential_floor import credential_verdict
from alicebot_api.legacy_credential_check import commit_door_secret_verdict


class ContinuityObjectStoreStub:
    def __init__(self) -> None:
        self.created_payloads: list[dict[str, object]] = []
        self.rows_by_capture_event: dict[UUID, dict[str, object]] = {}
        self.base_time = datetime(2026, 3, 29, 9, 0, tzinfo=UTC)

    def create_continuity_object(
        self,
        *,
        capture_event_id: UUID,
        object_type: str,
        status: str,
        title: str,
        body,
        provenance,
        confidence: float,
        is_preserved: bool = True,
        is_searchable: bool = True,
        is_promotable: bool = True,
    ):
        created = {
            "id": uuid4(),
            "user_id": uuid4(),
            "capture_event_id": capture_event_id,
            "object_type": object_type,
            "status": status,
            "is_preserved": is_preserved,
            "is_searchable": is_searchable,
            "is_promotable": is_promotable,
            "title": title,
            "body": body,
            "provenance": provenance,
            "confidence": confidence,
            "created_at": self.base_time,
            "updated_at": self.base_time,
        }
        self.created_payloads.append(created)
        self.rows_by_capture_event[capture_event_id] = created
        return created

    def get_continuity_object_by_capture_event_optional(self, capture_event_id: UUID):
        return self.rows_by_capture_event.get(capture_event_id)

    def list_continuity_objects_for_capture_events(self, capture_event_ids: list[UUID]):
        return [
            self.rows_by_capture_event[capture_event_id]
            for capture_event_id in capture_event_ids
            if capture_event_id in self.rows_by_capture_event
        ]


def test_create_continuity_object_record_serializes_created_row() -> None:
    store = ContinuityObjectStoreStub()
    capture_event_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    user_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    payload = create_continuity_object_record(
        store,  # type: ignore[arg-type]
        user_id=user_id,
        capture_event_id=capture_event_id,
        object_type="Decision",
        title="Decision: Use bounded intake",
        body={"decision_text": "Use bounded intake"},
        provenance={"capture_event_id": str(capture_event_id)},
        confidence=1.0,
    )

    assert payload == {
        "id": payload["id"],
        "capture_event_id": str(capture_event_id),
        "object_type": "Decision",
        "status": "active",
        "lifecycle": {
            "is_preserved": True,
            "preservation_status": "preserved",
            "is_searchable": True,
            "searchability_status": "searchable",
            "is_promotable": True,
            "promotion_status": "promotable",
        },
        "title": "Decision: Use bounded intake",
        "body": {"decision_text": "Use bounded intake"},
        "provenance": {"capture_event_id": str(capture_event_id)},
        "confidence": 1.0,
        "created_at": "2026-03-29T09:00:00+00:00",
        "updated_at": "2026-03-29T09:00:00+00:00",
    }


def test_create_continuity_object_record_rejects_invalid_object_type() -> None:
    store = ContinuityObjectStoreStub()

    with pytest.raises(
        ContinuityObjectValidationError,
        match="object_type must be one of",
    ):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            capture_event_id=uuid4(),
            object_type="Task",
            title="Task: Call supplier",
            body={"action_text": "Call supplier"},
            provenance={"capture_event_id": "event-1"},
            confidence=1.0,
        )


def test_create_continuity_object_record_rejects_empty_title_and_invalid_confidence() -> None:
    store = ContinuityObjectStoreStub()

    with pytest.raises(ContinuityObjectValidationError, match="title must not be empty"):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            capture_event_id=uuid4(),
            object_type="Note",
            title="   ",
            body={"body": "note"},
            provenance={"capture_event_id": "event-1"},
            confidence=1.0,
        )

    with pytest.raises(
        ContinuityObjectValidationError,
        match="confidence must be between 0.0 and 1.0",
    ):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            capture_event_id=uuid4(),
            object_type="Note",
            title="Valid title",
            body={"body": "note"},
            provenance={"capture_event_id": "event-1"},
            confidence=1.01,
        )


def test_get_and_list_continuity_objects_for_capture_events_use_capture_event_scope() -> None:
    store = ContinuityObjectStoreStub()
    capture_event_id = uuid4()

    created = create_continuity_object_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        capture_event_id=capture_event_id,
        object_type="MemoryFact",
        title="Memory Fact: prefers tea",
        body={"fact_text": "prefers tea"},
        provenance={"capture_event_id": str(capture_event_id)},
        confidence=0.95,
    )

    fetched = get_continuity_object_for_capture_event(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        capture_event_id=capture_event_id,
    )
    missing = get_continuity_object_for_capture_event(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        capture_event_id=uuid4(),
    )
    listed = list_continuity_objects_for_capture_events(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        capture_event_ids=[capture_event_id, uuid4()],
    )

    assert fetched == created
    assert missing is None
    assert listed == {
        str(capture_event_id): created,
    }


def test_create_continuity_object_record_refuses_a_legacy_assignment_the_floor_stores() -> None:
    """A PASSWORD_DB assignment is stored when only the floor runs.

    On a throwaway Postgres, before this check, create_continuity_object_record
    stored the value. Mutation: drop the commit_door_secret_verdict call, or
    drop commit_gate_refuses inside that helper. The stub records the row
    and this test fails.
    """

    value = "Ab" + "12" + "cd" + "EF"
    secret = "PASSWORD" + "_DB=" + value
    title = f"decision: {secret}"
    body = {"decision_text": secret}
    assert credential_verdict(title, body) is None

    store = ContinuityObjectStoreStub()
    with pytest.raises(ContinuityObjectValidationError, match="credential material"):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            capture_event_id=uuid4(),
            object_type="Decision",
            title=title,
            body=body,
            provenance={"source_kind": "probe"},
            confidence=0.98,
        )
    assert store.created_payloads == []
    assert secret not in str(store.rows_by_capture_event)


def test_quoted_ordinary_note_is_stored_from_string_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quoted ordinary note is stored. A JSON dump of the body is refused.

    The prose rule counts a backslash. json.dumps turns the quotes around
    "rotated" into backslashes, so the dump refuses a note the commit door
    accepts. That refusal used to abort every later candidate in the turn.
    Mutation: pass json.dumps(body) to the commit door. This call raises
    and the test fails. Mutation: stop calling commit_door_secret_verdict.
    The recorded text is missing and the test fails.
    """

    note = 'the password was "rotated" by ops'
    seen: list[str] = []
    real = commit_door_secret_verdict

    def spy(*args: object, **kwargs: object) -> object:
        if len(args) > 1:
            seen.append(str(args[1]))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("alicebot_api.continuity_objects.commit_door_secret_verdict", spy)
    body = {"decision_text": note, "raw_content": note}
    assert credential_verdict("Decision: " + note, body) is None
    assert commit_door_secret_verdict("Decision: " + note, note) is None

    store = ContinuityObjectStoreStub()
    created = create_continuity_object_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        capture_event_id=uuid4(),
        object_type="Decision",
        title="Decision: " + note,
        body=body,
        provenance={"source_kind": "probe"},
        confidence=0.98,
    )

    assert created["title"] == "Decision: " + note
    assert seen and note in seen[0]
    assert '\\"' not in seen[0]
    assert store.created_payloads


def test_legacy_docs_name_both_doors_and_the_string_value_check() -> None:
    """The docstring, the plan, and the changelog name both doors.

    Mutation: drop the continuity bullet, or restore the sentence that the
    continuity door checks only the floor. This test fails.
    """

    from alicebot_api import legacy_credential_check

    doc = legacy_credential_check.__doc__ or ""
    assert "run_local_vault_sleep" in doc
    assert "create_continuity_object_record" in doc
    assert "commit_door_secret_verdict" in doc
    assert "string values" in doc
    root = Path(__file__).resolve().parents[2]
    plan = " ".join((root / "docs/plans/sprint-5-lifecycle.md").read_text(encoding="utf-8").split())
    changelog = " ".join((root / "CHANGELOG.md").read_text(encoding="utf-8").split())
    assert "Today that door checks only the floor" not in plan
    assert "commit_door_secret_verdict" in plan
    assert "body's string values" in plan
    assert "commit_door_secret_verdict" in changelog
    assert "body's string values" in changelog
    assert "JSON dump" in changelog
