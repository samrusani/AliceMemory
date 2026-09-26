from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from alicebot_api.continuity_capture import (
    ContinuityCaptureNotFoundError,
    ContinuityCaptureValidationError,
    capture_continuity_candidates,
    capture_continuity_input,
    commit_continuity_captures,
    get_continuity_capture_detail,
    list_continuity_capture_inbox,
)
from alicebot_api.continuity_objects import ContinuityObjectValidationError
from alicebot_api.credential_floor import credential_verdict
from alicebot_api.legacy_credential_check import commit_door_secret_verdict
from alicebot_api.contracts import (
    ContinuityCaptureCandidatesInput,
    ContinuityCaptureCommitInput,
    ContinuityCaptureCreateInput,
)


class ContinuityCaptureStoreStub:
    def __init__(self) -> None:
        self.user_id = UUID("11111111-1111-4111-8111-111111111111")
        self.base_time = datetime(2026, 3, 29, 9, 30, tzinfo=UTC)
        self.capture_events: dict[UUID, dict[str, object]] = {}
        self.capture_event_order: list[UUID] = []
        self.objects_by_capture_event: dict[UUID, dict[str, object]] = {}
        self.objects_by_commit_fingerprint: dict[tuple[str, str], dict[str, object]] = {}

    def create_continuity_capture_event(
        self,
        *,
        raw_content: str,
        explicit_signal: str | None,
        admission_posture: str,
        admission_reason: str,
    ):
        capture_event_id = uuid4()
        row = {
            "id": capture_event_id,
            "user_id": self.user_id,
            "raw_content": raw_content,
            "explicit_signal": explicit_signal,
            "admission_posture": admission_posture,
            "admission_reason": admission_reason,
            "created_at": self.base_time,
        }
        self.capture_events[capture_event_id] = row
        self.capture_event_order.insert(0, capture_event_id)
        return row

    def get_continuity_capture_event_optional(self, capture_event_id: UUID):
        return self.capture_events.get(capture_event_id)

    def list_continuity_capture_events(self, *, limit: int):
        return [
            self.capture_events[capture_event_id]
            for capture_event_id in self.capture_event_order[:limit]
        ]

    def count_continuity_capture_events(self) -> int:
        return len(self.capture_events)

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
        row = {
            "id": uuid4(),
            "user_id": self.user_id,
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
        self.objects_by_capture_event[capture_event_id] = row
        sync_fingerprint = str(provenance.get("sync_fingerprint", ""))
        candidate_id = str(provenance.get("candidate_id", ""))
        if sync_fingerprint and candidate_id:
            self.objects_by_commit_fingerprint[(sync_fingerprint, candidate_id)] = row
        return row

    def get_continuity_object_by_capture_event_optional(self, capture_event_id: UUID):
        return self.objects_by_capture_event.get(capture_event_id)

    def get_continuity_object_by_commit_fingerprint_optional(
        self,
        *,
        sync_fingerprint: str,
        candidate_id: str,
    ):
        return self.objects_by_commit_fingerprint.get((sync_fingerprint, candidate_id))

    def list_continuity_objects_for_capture_events(self, capture_event_ids: list[UUID]):
        return [
            self.objects_by_capture_event[capture_event_id]
            for capture_event_id in capture_event_ids
            if capture_event_id in self.objects_by_capture_event
        ]


def test_capture_continuity_input_maps_explicit_signal_deterministically() -> None:
    store = ContinuityCaptureStoreStub()

    payload = capture_continuity_input(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCreateInput(
            raw_content="Call supplier before noon",
            explicit_signal="task",
        ),
    )

    capture = payload["capture"]
    assert capture["capture_event"]["admission_posture"] == "DERIVED"
    assert capture["capture_event"]["admission_reason"] == "explicit_signal_task"
    assert capture["derived_object"] is not None
    assert capture["derived_object"]["object_type"] == "NextAction"
    assert capture["derived_object"]["body"]["action_text"] == "Call supplier before noon"
    assert capture["derived_object"]["provenance"]["capture_event_id"] == capture["capture_event"]["id"]


def test_capture_continuity_input_uses_high_confidence_prefix_when_signal_is_missing() -> None:
    store = ContinuityCaptureStoreStub()

    payload = capture_continuity_input(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCreateInput(raw_content="Decision: ship conservative admission"),
    )

    assert payload["capture"]["capture_event"]["admission_posture"] == "DERIVED"
    assert payload["capture"]["capture_event"]["admission_reason"] == "high_confidence_prefix_decision"
    assert payload["capture"]["derived_object"]["object_type"] == "Decision"
    assert payload["capture"]["derived_object"]["body"]["decision_text"] == "ship conservative admission"
    assert payload["capture"]["derived_object"]["confidence"] == 0.95


def test_capture_continuity_input_defaults_to_triage_for_ambiguous_input() -> None:
    store = ContinuityCaptureStoreStub()

    payload = capture_continuity_input(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCreateInput(raw_content="Need to think about this sometime"),
    )

    assert payload["capture"]["capture_event"] == {
        "id": payload["capture"]["capture_event"]["id"],
        "raw_content": "Need to think about this sometime",
        "explicit_signal": None,
        "admission_posture": "TRIAGE",
        "admission_reason": "ambiguous_capture_requires_triage",
        "created_at": "2026-03-29T09:30:00+00:00",
    }
    assert payload["capture"]["derived_object"] is None


def test_create_continuity_capture_refuses_before_any_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /v0/continuity/captures refuses a token and an expanding text before insert.

    The check lives in capture_continuity_input and the route maps the error to
    400. A spy store records create_continuity_capture_event. Three mutations
    must fail this test: deleting the check (the token post returns 201),
    letting the expansion verdict pass (the expanding text returns 201), and
    moving the check to after the insert (the refusal still returns 400, and
    the spy has a row).
    """

    from contextlib import contextmanager
    import json

    from alicebot_api.config import Settings
    from alicebot_api.routers import continuity as continuity_router
    from alicebot_api.routers.continuity import ContinuityCaptureRequest, create_continuity_capture

    created: list[RecordingCaptureStore] = []

    class RecordingCaptureStore(ContinuityCaptureStoreStub):
        def __init__(self, conn: object) -> None:
            super().__init__()
            self.inserts: list[str] = []
            created.append(self)

        def create_continuity_capture_event(self, **kwargs: object):
            raw_content = kwargs["raw_content"]
            assert isinstance(raw_content, str)
            self.inserts.append(raw_content)
            return super().create_continuity_capture_event(
                raw_content=raw_content,
                explicit_signal=kwargs["explicit_signal"] if isinstance(kwargs["explicit_signal"], str) else None,
                admission_posture=str(kwargs["admission_posture"]),
                admission_reason=str(kwargs["admission_reason"]),
            )

    @contextmanager
    def fake_connection(database_url: str, user_id: UUID):
        del database_url, user_id
        yield object()

    monkeypatch.setattr(continuity_router, "user_connection", fake_connection)
    monkeypatch.setattr(continuity_router, "ContinuityStore", RecordingCaptureStore)
    monkeypatch.setattr(continuity_router, "get_settings", lambda: Settings())

    user_id = UUID("11111111-1111-4111-8111-111111111111")
    token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    expansion = "\uFDFA" * 100

    def post(raw_content: str) -> tuple[int, dict[str, object], RecordingCaptureStore]:
        created.clear()
        response = create_continuity_capture(
            ContinuityCaptureRequest(user_id=user_id, raw_content=raw_content)
        )
        body = json.loads(response.body)
        assert isinstance(body, dict)
        assert len(created) == 1
        return response.status_code, body, created[0]

    status, body, store = post(f"Hermes note: rotate to {token}")
    assert status == 400
    assert body["detail"]["code"] == "invalid_request"
    assert store.inserts == []
    assert token not in json.dumps(body)

    status, body, store = post(expansion)
    assert status == 400
    assert body["detail"]["code"] == "invalid_request"
    assert store.inserts == []
    assert expansion not in json.dumps(body)

    ordinary = "Need to think about this sometime"
    status, body, store = post(ordinary)
    assert status == 201
    assert store.inserts == [ordinary]
    assert body["capture"]["capture_event"]["raw_content"] == ordinary


def test_continuity_capture_list_and_detail_preserve_triage_visibility() -> None:
    store = ContinuityCaptureStoreStub()

    triage_payload = capture_continuity_input(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCreateInput(raw_content="Uncertain note without prefix"),
    )
    derived_payload = capture_continuity_input(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCreateInput(raw_content="task: send invoice"),
    )

    inbox = list_continuity_capture_inbox(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        limit=20,
    )

    assert inbox["summary"] == {
        "limit": 20,
        "returned_count": 2,
        "total_count": 2,
        "derived_count": 1,
        "triage_count": 1,
        "order": ["created_at_desc", "id_desc"],
    }
    assert inbox["items"][0]["capture_event"]["id"] == derived_payload["capture"]["capture_event"]["id"]
    assert inbox["items"][1]["capture_event"]["id"] == triage_payload["capture"]["capture_event"]["id"]

    detail = get_continuity_capture_detail(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        capture_event_id=UUID(derived_payload["capture"]["capture_event"]["id"]),
    )
    assert detail["capture"]["derived_object"]["object_type"] == "NextAction"


def test_continuity_capture_validation_and_not_found_contracts() -> None:
    store = ContinuityCaptureStoreStub()

    with pytest.raises(ContinuityCaptureValidationError, match="raw_content must not be empty"):
        capture_continuity_input(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            request=ContinuityCaptureCreateInput(raw_content="   "),
        )

    with pytest.raises(ContinuityCaptureValidationError, match="explicit_signal must be one of"):
        capture_continuity_input(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            request=ContinuityCaptureCreateInput(
                raw_content="Call supplier",
                explicit_signal="unknown_signal",  # type: ignore[arg-type]
            ),
        )

    with pytest.raises(
        ContinuityCaptureNotFoundError,
        match="continuity capture event .* was not found",
    ):
        get_continuity_capture_detail(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            capture_event_id=uuid4(),
        )


def test_capture_candidates_extracts_explicit_decision_and_correction_from_turn_pair() -> None:
    store = ContinuityCaptureStoreStub()

    payload = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="Decision: ship B2 this week",
            assistant_content="Correction: use assist mode as default",
        ),
    )

    assert payload["summary"]["candidate_count"] == 2
    assert payload["summary"]["explicit_count"] == 2
    assert {item["candidate_type"] for item in payload["candidates"]} == {"decision", "correction"}
    by_role = {item["source_role"]: item for item in payload["candidates"]}
    assert by_role["user"]["proposed_action"] == "auto_save_candidate"
    assert by_role["assistant"]["proposed_action"] == "queue_for_review"


def test_capture_candidates_returns_no_op_for_ack_only_turns() -> None:
    store = ContinuityCaptureStoreStub()

    payload = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="thanks",
            assistant_content="ok",
        ),
    )

    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["no_op_count"] == 1
    assert payload["candidates"][0]["candidate_type"] == "no_op"


def test_commit_captures_assist_mode_auto_saves_explicit_decisions_and_routes_notes_to_review() -> None:
    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="Decision: keep release freeze",
            assistant_content="Note: broad summary for later",
        ),
    )["candidates"]

    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="assist",
            sync_fingerprint="sync:assist-001",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )

    commits = payload["commits"]
    assert len(commits) == 2
    decision_commit = next(item for item in commits if item["candidate_type"] == "decision")
    note_commit = next(item for item in commits if item["candidate_type"] == "note")

    assert decision_commit["decision"] == "auto_saved"
    assert decision_commit["continuity_object"] is not None
    assert decision_commit["continuity_object"]["status"] == "active"
    assert note_commit["decision"] == "queued_for_review"
    assert note_commit["continuity_object"] is not None
    assert note_commit["continuity_object"]["status"] == "stale"

    assert payload["summary"] == {
        "mode": "assist",
        "candidate_count": 2,
        "auto_saved_count": 1,
        "review_queued_count": 1,
        "noop_count": 0,
        "duplicate_noop_count": 0,
        "auto_saved_types": ["decision"],
        "review_queued_types": ["note"],
    }


def test_commit_captures_manual_mode_routes_explicit_items_to_review() -> None:
    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="Decision: defer launch by one week",
            assistant_content="",
        ),
    )["candidates"]

    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="manual",
            sync_fingerprint="sync:manual-001",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )

    assert payload["commits"][0]["decision"] == "queued_for_review"
    assert payload["commits"][0]["continuity_object"]["status"] == "stale"
    assert payload["summary"]["auto_saved_count"] == 0
    assert payload["summary"]["review_queued_count"] == 1


def test_commit_captures_auto_mode_queues_assistant_and_regex_hits() -> None:
    """Auto mode no longer saves an assistant hit or a regex hit.

    Mutation: restore auto_mode_allowlist_high_confidence for confidence
    at or above 0.85. The assistant candidate is auto-saved and this test fails.
    """

    store = ContinuityCaptureStoreStub()
    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="auto",
            sync_fingerprint="sync:auto-001",
            candidates=[
                {
                    "candidate_type": "waiting_for",
                    "object_type": "WaitingFor",
                    "normalized_text": "waiting on release approval",
                    "confidence": 0.86,
                    "explicit": False,
                    "source_role": "assistant",
                    "admission_reason": "derived_waiting_for",
                    "evidence_snippet": "waiting on release approval",
                },
                {
                    "candidate_type": "decision",
                    "object_type": "Decision",
                    "normalized_text": "we decided to keep the billing store on Postgres",
                    "confidence": 0.9,
                    "explicit": True,
                    "source_role": "user",
                    "admission_reason": "explicit_phrase_decision",
                    "evidence_snippet": "we decided",
                },
            ],
        ),
    )

    assert [item["decision"] for item in payload["commits"]] == ["queued_for_review", "queued_for_review"]
    assert payload["summary"]["auto_saved_count"] == 0
    assert payload["summary"]["review_queued_count"] == 2


def test_commit_captures_auto_mode_saves_a_user_prefix_rule() -> None:
    """A user who types an explicit prefix is saved in auto mode.

    Mutation: queue every auto-mode candidate. This test fails.
    """

    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="decision: use Postgres for billing",
            assistant_content="Decision: the assistant also picked Postgres",
        ),
    )["candidates"]
    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="auto",
            sync_fingerprint="sync:auto-prefix",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )
    by_role = {item["candidate_id"]: item for item in payload["commits"]}
    user = next(item for item in candidates if item["source_role"] == "user")
    assistant = next(item for item in candidates if item["source_role"] == "assistant")
    assert by_role[user["candidate_id"]]["decision"] == "auto_saved"
    assert by_role[user["candidate_id"]]["reason"] == "user_explicit_prefix_rule"
    assert by_role[assistant["candidate_id"]]["decision"] == "queued_for_review"


def test_commit_captures_auto_mode_routes_below_threshold_candidates_to_review() -> None:
    store = ContinuityCaptureStoreStub()
    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="auto",
            sync_fingerprint="sync:auto-002",
            candidates=[
                {
                    "candidate_type": "decision",
                    "object_type": "Decision",
                    "normalized_text": "ship the bridge now",
                    "confidence": 0.84,
                    "explicit": True,
                    "source_role": "user",
                    "admission_reason": "explicit_prefix_decision",
                    "evidence_snippet": "ship the bridge now",
                }
            ],
        ),
    )

    assert payload["commits"][0]["decision"] == "queued_for_review"
    assert payload["commits"][0]["continuity_object"] is not None
    assert payload["commits"][0]["continuity_object"]["status"] == "stale"
    assert payload["summary"]["auto_saved_count"] == 0
    assert payload["summary"]["review_queued_count"] == 1
    assert payload["summary"]["review_queued_types"] == ["decision"]


def test_commit_captures_rejects_non_boolean_explicit_values() -> None:
    store = ContinuityCaptureStoreStub()

    with pytest.raises(ContinuityCaptureValidationError, match="explicit must be a boolean"):
        commit_continuity_captures(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            request=ContinuityCaptureCommitInput(
                mode="assist",
                sync_fingerprint="sync:explicit-validation-001",
                candidates=[
                    {
                        "candidate_type": "decision",
                        "object_type": "Decision",
                        "normalized_text": "ship after review",
                        "confidence": 0.95,
                        "explicit": "true",
                        "source_role": "user",
                        "admission_reason": "explicit_prefix_decision",
                        "evidence_snippet": "ship after review",
                    }
                ],
            ),
        )


def test_commit_captures_is_idempotent_for_repeated_sync_attempts() -> None:
    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="Decision: freeze scope for bridge sprint",
            assistant_content="",
        ),
    )["candidates"]

    first = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="assist",
            sync_fingerprint="sync:repeat-001",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )
    second = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="assist",
            sync_fingerprint="sync:repeat-001",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )

    assert first["summary"]["auto_saved_count"] == 1
    assert second["summary"]["auto_saved_count"] == 0
    assert second["summary"]["duplicate_noop_count"] == 1
    assert second["commits"][0]["decision"] == "duplicate_noop"


def test_commit_captures_assist_mode_queues_user_regex_and_assistant_prefix() -> None:
    """Assist mode queues a user regex hit and an assistant prefix hit.

    Mutation: auto-save every explicit allowlisted candidate at confidence
    0.9, including an assistant prefix and a regex hit. Both are auto-saved
    and this test fails.
    """

    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="we decided to keep the billing store on Postgres",
            assistant_content="decision: the assistant also picked Postgres",
        ),
    )["candidates"]
    by_role = {item["source_role"]: item for item in candidates}
    assert by_role["user"]["admission_reason"] == "explicit_phrase_decision"
    assert by_role["user"]["explicit"] is True
    assert by_role["assistant"]["admission_reason"] == "explicit_prefix_decision"
    assert by_role["assistant"]["explicit"] is True

    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="assist",
            sync_fingerprint="sync:assist-queue",
            candidates=candidates,  # type: ignore[arg-type]
        ),
    )

    assert [item["decision"] for item in payload["commits"]] == [
        "queued_for_review",
        "queued_for_review",
    ]
    assert payload["summary"]["auto_saved_count"] == 0
    assert payload["summary"]["review_queued_count"] == 2


def test_quoted_note_does_not_abort_a_later_candidate_in_the_same_turn() -> None:
    """An ordinary quoted note is stored, and so is the next candidate.

    Mutation: check json.dumps of the body. The first candidate raises and
    the second candidate is not stored.
    """

    store = ContinuityCaptureStoreStub()
    quoted = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content='decision: the password was "rotated" by ops',
            assistant_content="",
        ),
    )["candidates"]
    later = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="decision: keep the weekly billing report",
            assistant_content="",
        ),
    )["candidates"]
    assert quoted[0]["proposed_action"] == "auto_save_candidate"
    assert later[0]["proposed_action"] == "auto_save_candidate"

    payload = commit_continuity_captures(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCommitInput(
            mode="assist",
            sync_fingerprint="sync:quoted-note",
            candidates=[*quoted, *later],  # type: ignore[arg-type]
        ),
    )

    assert [item["decision"] for item in payload["commits"]] == ["auto_saved", "auto_saved"]
    assert payload["summary"]["auto_saved_count"] == 2
    assert len(store.objects_by_capture_event) == 2


def test_quoted_assignment_past_the_title_cut_is_not_auto_saved() -> None:
    """A quoted assignment past the 280-character title cut is refused.

    The title cut drops it. A JSON dump of the body hides the quotes, so
    the assignment is missed and the candidate is auto-saved. The floor
    accepts the raw text. Mutation: check the title only, or pass
    json.dumps(body) to the commit door. The row is stored and this test
    fails.
    """

    value = "Ab" + "12" + "cd" + "EF"
    quoted = "PASSWORD" + '_DB="' + value + '"'
    filler = ("ship the weekly billing report " * 20).strip()
    store = ContinuityCaptureStoreStub()
    candidates = capture_continuity_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=ContinuityCaptureCandidatesInput(
            user_content="decision: " + filler + " " + quoted,
            assistant_content="",
        ),
    )["candidates"]
    candidate = candidates[0]
    normalized = candidate["normalized_text"]
    assert candidate["proposed_action"] == "auto_save_candidate"
    assert normalized.find(quoted) >= 280
    assert credential_verdict(normalized) is None
    assert commit_door_secret_verdict("Decision", normalized) is not None

    with pytest.raises(ContinuityObjectValidationError, match="credential material"):
        commit_continuity_captures(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            request=ContinuityCaptureCommitInput(
                mode="assist",
                sync_fingerprint="sync:title-cut",
                candidates=candidates,  # type: ignore[arg-type]
            ),
        )
    assert store.objects_by_capture_event == {}
