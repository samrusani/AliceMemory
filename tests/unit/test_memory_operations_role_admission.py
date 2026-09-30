"""The /v1 memory operations policy applies the capture admission rule.

Finding DB-001 (related). ``memory_mutations._resolve_policy_action`` read no
role. In ``assist`` mode an explicit candidate of an allowed type at 0.9 or
more returned ``auto_apply``, and in ``auto`` mode any candidate of an allowed
type did, whichever side of the turn carried the text. ``POST
/v1/memory/operations/commit`` applies ``auto_apply`` rows, so the assistant
line ``decision: ship X`` became an active Decision. The continuity capture
commit has applied only a user turn that used an explicit prefix since
v0.18.0 (``user_prefix_autosave``). The two policies now call that one
function.

The server derives the role from which field carried the text
(``user_content`` or ``assistant_content``), so the route tests drive the real
app and read the role back from the stored candidate. Only the Postgres layer
is replaced, by an in-memory store. Reproduced on origin/main before the fix:
every assistant-role and regex-hit case below returned ``auto_apply`` and
created an active object.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import itertools
import json
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api import continuity_capture, memory_mutations
from alicebot_api.config import Settings
from alicebot_api.continuity_capture import _resolve_commit_decision
from alicebot_api.contracts import CONTINUITY_CAPTURE_CANDIDATE_TYPES
from alicebot_api.memory_mutations import _resolve_policy_action
from alicebot_api.routers import continuity as continuity_router

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
GENERATE_PATH = "/v1/memory/operations/candidates/generate"
COMMIT_PATH = "/v1/memory/operations/commit"


class OperationsStore:
    """The store methods the generate and commit path calls, in memory."""

    def __init__(self) -> None:
        self.base_time = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        self.events: dict[UUID, dict[str, Any]] = {}
        self.objects: dict[UUID, dict[str, Any]] = {}
        self.candidates: dict[UUID, dict[str, Any]] = {}
        self.candidates_by_sync_source: dict[tuple[str, str], UUID] = {}
        self.operations: dict[UUID, dict[str, Any]] = {}

    def create_continuity_capture_event(
        self, *, raw_content: str, explicit_signal: str | None, admission_posture: str, admission_reason: str
    ) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "user_id": USER_ID,
            "raw_content": raw_content,
            "explicit_signal": explicit_signal,
            "admission_posture": admission_posture,
            "admission_reason": admission_reason,
            "created_at": self.base_time,
        }
        self.events[row["id"]] = row
        return row

    def create_continuity_object(
        self,
        *,
        capture_event_id: UUID,
        object_type: str,
        status: str,
        title: str,
        body: Any,
        provenance: Any,
        confidence: float,
        is_preserved: bool = True,
        is_searchable: bool = True,
        is_promotable: bool = True,
    ) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "user_id": USER_ID,
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
            "last_confirmed_at": None,
            "supersedes_object_id": None,
            "superseded_by_object_id": None,
            "created_at": self.base_time,
            "updated_at": self.base_time,
        }
        self.objects[row["id"]] = row
        return row

    def get_continuity_object_optional(self, continuity_object_id: UUID) -> dict[str, Any] | None:
        row = self.objects.get(continuity_object_id)
        return None if row is None else dict(row)

    def list_continuity_recall_candidates(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.objects.values():
            event = self.events[row["capture_event_id"]]
            rows.append(
                {
                    **row,
                    "object_created_at": row["created_at"],
                    "object_updated_at": row["updated_at"],
                    "admission_posture": event["admission_posture"],
                    "admission_reason": event["admission_reason"],
                    "explicit_signal": event["explicit_signal"],
                    "capture_created_at": event["created_at"],
                }
            )
        return rows

    def get_memory_operation_candidate_by_sync_source_optional(
        self, *, sync_fingerprint: str, source_candidate_id: str
    ) -> dict[str, Any] | None:
        candidate_id = self.candidates_by_sync_source.get((sync_fingerprint, source_candidate_id))
        return None if candidate_id is None else dict(self.candidates[candidate_id])

    def create_memory_operation_candidate(
        self,
        *,
        sync_fingerprint: str,
        source_kind: str,
        source_candidate_id: str,
        source_candidate_type: str,
        candidate_payload: Any,
        source_scope: Any,
        operation_type: str,
        operation_reason: str,
        policy_action: str,
        policy_reason: str,
        target_continuity_object_id: UUID | None,
        target_snapshot: Any,
    ) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "user_id": USER_ID,
            "sync_fingerprint": sync_fingerprint,
            "source_kind": source_kind,
            "source_candidate_id": source_candidate_id,
            "source_candidate_type": source_candidate_type,
            "candidate_payload": candidate_payload,
            "source_scope": source_scope,
            "operation_type": operation_type,
            "operation_reason": operation_reason,
            "policy_action": policy_action,
            "policy_reason": policy_reason,
            "target_continuity_object_id": target_continuity_object_id,
            "target_snapshot": target_snapshot,
            "applied_operation_id": None,
            "created_at": self.base_time,
            "applied_at": None,
        }
        self.candidates[row["id"]] = row
        self.candidates_by_sync_source[(sync_fingerprint, source_candidate_id)] = row["id"]
        return dict(row)

    def get_memory_operation_candidate_optional(self, candidate_id: UUID) -> dict[str, Any] | None:
        row = self.candidates.get(candidate_id)
        return None if row is None else dict(row)

    def list_memory_operation_candidates(
        self,
        *,
        limit: int,
        policy_action: str | None = None,
        operation_type: str | None = None,
        sync_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.candidates.values()
            if sync_fingerprint is None or row["sync_fingerprint"] == sync_fingerprint
        ]
        return rows[:limit]

    def get_memory_operation_optional(self, operation_id: UUID) -> dict[str, Any] | None:
        row = self.operations.get(operation_id)
        return None if row is None else dict(row)

    def create_memory_operation(
        self,
        *,
        operation_id: UUID,
        candidate_id: UUID,
        operation_type: str,
        status: str,
        sync_fingerprint: str,
        target_continuity_object_id: UUID | None,
        resulting_continuity_object_id: UUID | None,
        correction_event_id: UUID | None,
        before_snapshot: Any,
        after_snapshot: Any,
        details: Any,
    ) -> dict[str, Any]:
        row = {
            "id": operation_id,
            "user_id": USER_ID,
            "candidate_id": candidate_id,
            "operation_type": operation_type,
            "status": status,
            "sync_fingerprint": sync_fingerprint,
            "target_continuity_object_id": target_continuity_object_id,
            "resulting_continuity_object_id": resulting_continuity_object_id,
            "correction_event_id": correction_event_id,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "details": details,
            "created_at": self.base_time,
        }
        self.operations[operation_id] = row
        return dict(row)

    def update_memory_operation_candidate_application(
        self, *, candidate_id: UUID, applied_operation_id: UUID, applied_at: datetime
    ) -> dict[str, Any] | None:
        row = self.candidates.get(candidate_id)
        if row is None:
            return None
        row["applied_operation_id"] = applied_operation_id
        row["applied_at"] = applied_at
        return dict(row)

    def active_titles(self) -> list[str]:
        return [str(row["title"]) for row in self.objects.values() if row["status"] == "active"]


@contextmanager
def _fake_connection(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    yield object()


def _invoke(path: str, payload: dict[str, object]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode()
    messages: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-alicebot-user-id", str(USER_ID).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> OperationsStore:
    """Keyless loopback app, in-memory store. No agent key exists, so /v1 resolves no identity."""

    operations_store = OperationsStore()
    settings = Settings(app_env="test", auth_user_id="", database_url="postgresql://db.example/alice")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(continuity_router, "get_settings", lambda: settings)
    monkeypatch.setattr(continuity_router, "user_connection", _fake_connection)
    monkeypatch.setattr(continuity_router, "ContinuityStore", lambda connection: operations_store)
    monkeypatch.setattr(main_module, "_resolve_v1_http_auth", lambda **kwargs: None)
    return operations_store


def _turn(
    *, user: str, assistant: str, mode: str, fingerprint: str, include_review_required: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    status, generated = _invoke(
        GENERATE_PATH,
        {"user_content": user, "assistant_content": assistant, "mode": mode, "sync_fingerprint": fingerprint},
    )
    assert status == 200, generated
    status, committed = _invoke(
        COMMIT_PATH, {"sync_fingerprint": fingerprint, "include_review_required": include_review_required}
    )
    assert status == 200, committed
    return generated, committed


PREFIX_CASES = (
    ("decision", "decision: use Postgres for billing"),
    ("commitment", "commitment: send the invoice on Friday"),
    ("waiting_for", "waiting for: the design review"),
)


@pytest.mark.parametrize("mode", ["assist", "auto"])
@pytest.mark.parametrize(("candidate_type", "text"), PREFIX_CASES)
def test_user_prefix_candidate_is_applied_in_assist_and_auto_mode(
    store: OperationsStore, mode: str, candidate_type: str, text: str
) -> None:
    """A user who types an explicit prefix is applied without review.

    Mutation: make ``_admitted_for_auto_apply`` return False. This test fails
    on ``policy_action``. Mutation: handle only ``assist`` in
    ``_resolve_policy_action`` (``mode in {"assist"}``). The auto-mode case
    fails.
    """

    generated, committed = _turn(user=text, assistant="Noted.", mode=mode, fingerprint=f"user-{mode}-{candidate_type}")

    (item,) = generated["items"]
    assert item["candidate_payload"]["source_role"] == "user"
    assert item["candidate_payload"]["candidate_type"] == candidate_type
    assert item["policy_action"] == "auto_apply"
    assert item["policy_reason"] == "user_explicit_prefix_rule"
    assert generated["summary"]["auto_apply_count"] == 1
    assert committed["summary"]["applied_count"] == 1
    assert len(store.active_titles()) == 1


@pytest.mark.parametrize("mode", ["assist", "auto"])
@pytest.mark.parametrize(("candidate_type", "text"), PREFIX_CASES)
def test_assistant_prefix_candidate_is_queued_and_never_applied(
    store: OperationsStore, mode: str, candidate_type: str, text: str
) -> None:
    """The DB-001 case: the assistant reply carries the prefixed line.

    The role comes from ``assistant_content`` on the server. Mutation: restore
    the old allowlist (``auto_apply`` for an allowed type, no role read) in
    ``_resolve_policy_action``. The candidate is applied and this test fails.
    Mutation: pass ``source_role="user"`` in ``_admitted_for_auto_apply``.
    Same failure.
    """

    generated, committed = _turn(
        user="What is the plan?", assistant=text, mode=mode, fingerprint=f"assistant-{mode}-{candidate_type}"
    )

    (item,) = generated["items"]
    assert item["candidate_payload"]["source_role"] == "assistant"
    assert item["candidate_payload"]["candidate_type"] == candidate_type
    assert item["policy_action"] == "review_required"
    assert item["policy_reason"] == f"{mode}_mode_review_gate"
    assert generated["summary"]["auto_apply_count"] == 0
    assert committed["summary"]["applied_count"] == 0
    assert committed["summary"]["skipped_count"] == 1
    assert store.active_titles() == []
    assert store.operations == {}


@pytest.mark.parametrize("mode", ["assist", "auto"])
@pytest.mark.parametrize(
    ("user", "assistant", "role"),
    (
        ("I prefer tabs over spaces", "ok", "user"),
        ("hmm", "we decided that the release ships on Friday", "assistant"),
        ("I will send the invoice on Friday", "ok", "user"),
    ),
)
def test_regex_hit_from_either_role_is_queued(
    store: OperationsStore, mode: str, user: str, assistant: str, role: str
) -> None:
    """A phrase match is not an instruction to save, from either role.

    Mutation: drop the ``admission_reason in _PREFIX_ADMISSION_REASONS`` term
    from ``user_prefix_autosave``. The user regex hit is applied and the
    ``applied_count`` assertion fails.
    """

    generated, committed = _turn(user=user, assistant=assistant, mode=mode, fingerprint=f"regex-{mode}-{role}")

    (item,) = generated["items"]
    assert item["candidate_payload"]["source_role"] == role
    assert item["candidate_payload"]["admission_reason"].startswith("explicit_phrase_")
    assert item["policy_action"] == "review_required"
    assert committed["summary"]["applied_count"] == 0
    assert store.active_titles() == []


def test_manual_mode_applies_nothing_for_either_role(store: OperationsStore) -> None:
    generated, committed = _turn(
        user="decision: use Postgres for billing",
        assistant="decision: skip the security review",
        mode="manual",
        fingerprint="manual",
    )

    assert [item["policy_action"] for item in generated["items"]] == ["review_required", "review_required"]
    assert {item["policy_reason"] for item in generated["items"]} == {"manual_mode_requires_review"}
    assert committed["summary"]["applied_count"] == 0
    assert store.active_titles() == []


def test_a_mixed_turn_applies_only_the_user_candidate(store: OperationsStore) -> None:
    """One sync fingerprint, both roles: commit applies the user line and skips the assistant line.

    Mutation: in ``commit_memory_operations`` apply every candidate of the
    fingerprint (skip the ``_effective_policy`` gate). The assistant line is
    applied and ``skipped_count`` is 0.
    """

    generated, committed = _turn(
        user="decision: use Postgres for billing",
        assistant="decision: skip the security review for the billing service",
        mode="assist",
        fingerprint="mixed",
    )

    by_role = {item["candidate_payload"]["source_role"]: item for item in generated["items"]}
    assert by_role["user"]["policy_action"] == "auto_apply"
    assert by_role["assistant"]["policy_action"] == "review_required"
    assert committed["summary"]["applied_count"] == 1
    assert committed["summary"]["skipped_count"] == 1
    assert store.active_titles() == ["Decision: use Postgres for billing"]


def test_the_owner_can_still_approve_a_queued_assistant_candidate(store: OperationsStore) -> None:
    """``include_review_required`` is the explicit approval path and keeps working."""

    generated, committed = _turn(
        user="What is the plan?",
        assistant="decision: ship the release on Friday",
        mode="assist",
        fingerprint="approved",
        include_review_required=True,
    )

    assert generated["items"][0]["policy_action"] == "review_required"
    assert committed["summary"]["applied_count"] == 1
    assert store.active_titles() == ["Decision: ship the release on Friday"]


_MISSING = object()


def _override_id(overrides: dict[str, object]) -> str:
    return ",".join(f"{key}={'missing' if value is _MISSING else value}" for key, value in overrides.items())


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_type": "decision",
        "object_type": "Decision",
        "normalized_text": "use Postgres for billing",
        "confidence": 0.98,
        "explicit": True,
        "source_role": "user",
        "admission_reason": "explicit_prefix_decision",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _MISSING}


@pytest.mark.parametrize("mode", ["assist", "auto"])
def test_policy_applies_a_user_prefix_candidate_at_the_confidence_boundary(mode: str) -> None:
    """0.9 applies and anything below it is queued for low confidence.

    Mutation: change the ``< 0.9`` test in ``_resolve_policy_action`` to
    ``< 0.85``. The 0.8999 row reaches the shared rule, which still queues
    it, but with the review gate reason, so the reason assertion fails.
    Mutation: drop the ``isinstance(raw_confidence, bool)`` test in
    ``_payload_confidence``. ``True`` reads as 1.0 and is applied. Mutation:
    accept a string there. ``"0.98"`` is applied.
    """

    assert _resolve_policy_action(
        candidate_payload=_payload(confidence=0.9), operation_type="ADD", mode=mode
    ) == ("auto_apply", "user_explicit_prefix_rule")
    assert _resolve_policy_action(
        candidate_payload=_payload(confidence=0.8999), operation_type="ADD", mode=mode
    ) == ("review_required", "low_confidence_requires_review")
    for unreadable in (_MISSING, None, True, "0.98", "abc", [0.98]):
        assert _resolve_policy_action(
            candidate_payload=_payload(confidence=unreadable), operation_type="ADD", mode=mode
        ) == ("review_required", "low_confidence_requires_review"), unreadable


@pytest.mark.parametrize("mode", ["assist", "auto"])
@pytest.mark.parametrize(
    "overrides",
    (
        {"source_role": _MISSING},
        {"source_role": ""},
        {"source_role": "assistant"},
        {"source_role": "combined"},
        {"explicit": False},
        {"explicit": _MISSING},
        {"explicit": "true"},
        {"admission_reason": "explicit_phrase_decision"},
        {"admission_reason": "derived_candidate"},
        {"admission_reason": _MISSING},
        {"candidate_type": "no_op"},
    ),
    ids=_override_id,
)
def test_policy_queues_a_candidate_that_fails_any_term_of_the_rule(mode: str, overrides: dict[str, object]) -> None:
    """A missing role is not a user, and every other term of the rule gates on its own.

    Mutation: read the role as ``candidate_payload.get("source_role", "user")``
    in ``_admitted_for_auto_apply``. The missing-role case fails. Mutation:
    pass ``explicit=True`` there. The ``explicit`` cases fail.
    """

    action, reason = _resolve_policy_action(
        candidate_payload=_payload(**overrides), operation_type="ADD", mode=mode
    )

    assert action == "review_required"
    assert reason == f"{mode}_mode_review_gate"


def test_policy_keeps_its_earlier_gates() -> None:
    """NOOP, DELETE, a review-required type and manual mode are decided before the role."""

    assert _resolve_policy_action(candidate_payload=_payload(), operation_type="NOOP", mode="auto") == (
        "skip",
        "noop_candidate",
    )
    assert _resolve_policy_action(candidate_payload=_payload(), operation_type="DELETE", mode="auto") == (
        "review_required",
        "destructive_operations_require_review",
    )
    assert _resolve_policy_action(
        candidate_payload=_payload(candidate_type="note", admission_reason="explicit_prefix_note"),
        operation_type="ADD",
        mode="auto",
    ) == ("review_required", "candidate_type_requires_review")
    assert _resolve_policy_action(candidate_payload=_payload(), operation_type="ADD", mode="manual") == (
        "review_required",
        "manual_mode_requires_review",
    )
    assert _resolve_policy_action(candidate_payload=_payload(), operation_type="ADD", mode="banana") == (
        "review_required",
        "unknown_mode_review_gate",
    )


def test_the_two_policies_share_one_rule_and_agree_on_every_combination() -> None:
    """The capture commit and the /v1 policy auto-apply exactly the same candidates.

    Mutation: copy the rule into ``memory_mutations`` instead of calling
    ``user_prefix_autosave``. The identity assertion fails. Mutation: change
    ``confidence >= 0.9`` inside ``user_prefix_autosave`` to ``>= 0.85``. The
    0.89 rows disagree, because the /v1 policy also checks ``< 0.9`` first.
    """

    assert memory_mutations.user_prefix_autosave is continuity_capture.user_prefix_autosave

    candidate_types = [value for value in CONTINUITY_CAPTURE_CANDIDATE_TYPES if value != "no_op"]
    combinations = itertools.product(
        candidate_types,
        ("user", "assistant", "combined"),
        (True, False),
        (0.89, 0.9, 0.98),
        ("explicit_prefix_decision", "explicit_phrase_decision", "derived_candidate"),
        ("assist", "auto"),
    )
    auto_applied = 0
    for candidate_type, source_role, explicit, confidence, reason, mode in combinations:
        payload = _payload(
            candidate_type=candidate_type,
            source_role=source_role,
            explicit=explicit,
            confidence=confidence,
            admission_reason=reason,
        )
        policy_action, _ = _resolve_policy_action(candidate_payload=payload, operation_type="ADD", mode=mode)
        capture_decision, _, _ = _resolve_commit_decision(candidate=payload, mode=mode)  # type: ignore[arg-type]
        assert (policy_action == "auto_apply") == (capture_decision == "auto_saved"), (
            candidate_type,
            source_role,
            explicit,
            confidence,
            reason,
            mode,
        )
        auto_applied += policy_action == "auto_apply"
    assert auto_applied > 0


def _plant(store: OperationsStore, *, payload: dict[str, object], policy_action: str = "auto_apply") -> UUID:
    row = store.create_memory_operation_candidate(
        sync_fingerprint=f"planted-{uuid4()}",
        source_kind="sync_turn",
        source_candidate_id=str(uuid4()),
        source_candidate_type=str(payload.get("candidate_type", "decision")),
        candidate_payload=payload,
        source_scope={},
        operation_type="ADD",
        operation_reason="no_current_target_match",
        policy_action=policy_action,
        policy_reason="assist_mode_allowlist_explicit_high_confidence",
        target_continuity_object_id=None,
        target_snapshot={},
    )
    return row["id"]


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_role": "assistant"},
        {"source_role": _MISSING},
        {"admission_reason": "explicit_phrase_decision"},
        {"confidence": 0.89},
        {"confidence": True},
        {"explicit": False},
    ),
    ids=_override_id,
)
def test_commit_queues_a_row_stored_as_auto_apply_that_fails_the_rule(
    store: OperationsStore, overrides: dict[str, object]
) -> None:
    """A row stored before the rule was shared is checked again at commit.

    The generate route writes the role, reason and confidence into the row, so
    a row written by v0.19.0 from the assistant reply says ``auto_apply`` and
    carries ``source_role`` assistant. Commit does not trust the stored label.
    Mutation: return the stored policy from ``_effective_policy``. The row is
    applied and this test fails on ``applied_count``. Mutation: change
    ``confidence >= 0.9`` in ``user_prefix_autosave`` to ``>= 0.85``. The 0.89
    case is applied and fails.
    """

    candidate_id = _plant(store, payload=_payload(**overrides))

    status, committed = _invoke(COMMIT_PATH, {"candidate_ids": [str(candidate_id)]})

    assert status == 200, committed
    assert committed["summary"]["applied_count"] == 0
    assert committed["summary"]["skipped_count"] == 1
    assert store.active_titles() == []
    assert store.operations == {}
    (listed,) = committed["candidates"]
    assert listed["policy_action"] == "review_required"
    assert listed["policy_reason"] == "stored_auto_apply_fails_admission_rule"
    assert listed["applied_operation_id"] is None

    status, approved = _invoke(COMMIT_PATH, {"candidate_ids": [str(candidate_id)], "include_review_required": True})
    assert status == 200, approved
    assert approved["summary"]["applied_count"] == 1
    assert len(store.active_titles()) == 1


def test_commit_applies_a_stored_auto_apply_row_that_passes_the_rule(store: OperationsStore) -> None:
    """Control for the stored-row gate: a user prefix row at 0.9 still applies."""

    candidate_id = _plant(store, payload=_payload(confidence=0.9))

    status, committed = _invoke(COMMIT_PATH, {"candidate_ids": [str(candidate_id)]})

    assert status == 200, committed
    assert committed["summary"]["applied_count"] == 1
    assert committed["summary"]["skipped_count"] == 0
    assert store.active_titles() == ["Decision: use Postgres for billing"]


def test_commit_still_skips_a_stored_review_required_row(store: OperationsStore) -> None:
    """The stored label is honoured in the other direction: review stays review."""

    candidate_id = _plant(store, payload=_payload(), policy_action="review_required")

    status, committed = _invoke(COMMIT_PATH, {"candidate_ids": [str(candidate_id)]})

    assert status == 200, committed
    assert committed["summary"]["applied_count"] == 0
    assert committed["summary"]["skipped_count"] == 1
    assert committed["candidates"][0]["policy_reason"] == "assist_mode_allowlist_explicit_high_confidence"
    assert store.active_titles() == []
