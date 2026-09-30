from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

import alicebot_api.memory as memory_module
from alicebot_api.contracts import (
    MemoryCandidateInput,
    OpenLoopCandidateInput,
    OpenLoopCreateInput,
    OpenLoopStatusUpdateInput,
)
from alicebot_api.memory import (
    MemoryAdmissionValidationError,
    MemoryReviewNotFoundError,
    OpenLoopNotFoundError,
    OpenLoopValidationError,
    admit_memory_candidate,
    create_open_loop_record,
    create_memory_review_label_record,
    get_open_loop_record,
    get_memory_evaluation_summary,
    get_memory_hygiene_dashboard_summary,
    get_memory_quality_gate_summary,
    get_memory_trust_dashboard_summary,
    get_memory_review_record,
    list_open_loop_records,
    list_memory_review_queue_records,
    list_memory_review_label_records,
    list_memory_review_records,
    list_memory_revision_review_records,
    update_open_loop_status_record,
)


class MemoryStoreStub:
    def __init__(self) -> None:
        self.base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
        self.events: dict[UUID, dict[str, object]] = {}
        self.threads: dict[UUID, dict[str, object]] = {}
        self.memories: dict[tuple[str, str], dict[str, object]] = {}
        self.revisions: list[dict[str, object]] = []
        self.open_loops: list[dict[str, object]] = []
        self.allowed_profiles: dict[str, dict[str, str]] = {
            "assistant_default": {
                "id": "assistant_default",
                "name": "Assistant Default",
                "description": "Default profile",
            },
            "coach_default": {
                "id": "coach_default",
                "name": "Coach Default",
                "description": "Coach profile",
            },
        }

    def list_events_by_ids(self, event_ids: list[UUID]) -> list[dict[str, object]]:
        return [self.events[event_id] for event_id in event_ids if event_id in self.events]

    def get_thread_optional(self, thread_id: UUID) -> dict[str, object] | None:
        return self.threads.get(thread_id)

    def get_agent_profile_optional(self, profile_id: str) -> dict[str, str] | None:
        return self.allowed_profiles.get(profile_id)

    def _find_memory_by_id(self, memory_id: UUID) -> dict[str, object] | None:
        for memory in self.memories.values():
            if memory["id"] == memory_id:
                return memory
        return None

    def get_memory_by_key(self, memory_key: str) -> dict[str, object] | None:
        for memory in self.memories.values():
            if memory["memory_key"] == memory_key:
                return memory
        return None

    def get_memory_by_key_and_profile(
        self,
        *,
        memory_key: str,
        agent_profile_id: str,
    ) -> dict[str, object] | None:
        return self.memories.get((memory_key, agent_profile_id))

    def create_memory(
        self,
        *,
        memory_key: str,
        value,
        status: str,
        source_event_ids: list[str],
        memory_type: str = "preference",
        confidence: float | None = None,
        salience: float | None = None,
        confirmation_status: str = "unconfirmed",
        trust_class: str = "deterministic",
        promotion_eligibility: str = "promotable",
        evidence_count: int | None = None,
        independent_source_count: int | None = None,
        extracted_by_model: str | None = None,
        trust_reason: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        last_confirmed_at: datetime | None = None,
        agent_profile_id: str = "assistant_default",
    ) -> dict[str, object]:
        memory = {
            "id": uuid4(),
            "user_id": uuid4(),
            "agent_profile_id": agent_profile_id,
            "memory_key": memory_key,
            "value": value,
            "status": status,
            "source_event_ids": source_event_ids,
            "memory_type": memory_type,
            "confidence": confidence,
            "salience": salience,
            "confirmation_status": confirmation_status,
            "trust_class": trust_class,
            "promotion_eligibility": promotion_eligibility,
            "evidence_count": evidence_count,
            "independent_source_count": independent_source_count,
            "extracted_by_model": extracted_by_model,
            "trust_reason": trust_reason,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "last_confirmed_at": last_confirmed_at,
            "created_at": self.base_time,
            "updated_at": self.base_time,
            "deleted_at": None,
        }
        self.memories[(memory_key, agent_profile_id)] = memory
        return memory

    def update_memory(
        self,
        *,
        memory_id: UUID,
        value,
        status: str,
        source_event_ids: list[str],
        memory_type: str = "preference",
        confidence: float | None = None,
        salience: float | None = None,
        confirmation_status: str = "unconfirmed",
        trust_class: str = "deterministic",
        promotion_eligibility: str = "promotable",
        evidence_count: int | None = None,
        independent_source_count: int | None = None,
        extracted_by_model: str | None = None,
        trust_reason: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> dict[str, object]:
        existing_memory = self._find_memory_by_id(memory_id)
        assert existing_memory is not None
        updated_at = self.base_time + timedelta(minutes=len(self.revisions) + 1)
        updated_memory = {
            **existing_memory,
            "value": value,
            "status": status,
            "source_event_ids": source_event_ids,
            "memory_type": memory_type,
            "confidence": confidence,
            "salience": salience,
            "confirmation_status": confirmation_status,
            "trust_class": trust_class,
            "promotion_eligibility": promotion_eligibility,
            "evidence_count": evidence_count,
            "independent_source_count": independent_source_count,
            "extracted_by_model": extracted_by_model,
            "trust_reason": trust_reason,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "last_confirmed_at": last_confirmed_at,
            "updated_at": updated_at,
            "deleted_at": updated_at if status == "deleted" else None,
        }
        self.memories[(updated_memory["memory_key"], updated_memory["agent_profile_id"])] = updated_memory
        return updated_memory

    def append_memory_revision(
        self,
        *,
        memory_id: UUID,
        action: str,
        memory_key: str,
        previous_value,
        new_value,
        source_event_ids: list[str],
        candidate: dict[str, object],
    ) -> dict[str, object]:
        existing_memory = self._find_memory_by_id(memory_id)
        revision = {
            "id": uuid4(),
            "user_id": existing_memory["user_id"] if existing_memory is not None else uuid4(),
            "memory_id": memory_id,
            "sequence_no": len(self.revisions) + 1,
            "action": action,
            "memory_key": memory_key,
            "previous_value": previous_value,
            "new_value": new_value,
            "source_event_ids": source_event_ids,
            "candidate": candidate,
            "created_at": self.base_time + timedelta(minutes=len(self.revisions) + 1),
        }
        self.revisions.append(revision)
        return revision

    def create_open_loop(
        self,
        *,
        memory_id: UUID | None,
        title: str,
        status: str,
        opened_at: datetime | None,
        due_at: datetime | None,
        resolved_at: datetime | None,
        resolution_note: str | None,
    ) -> dict[str, object]:
        memory = None if memory_id is None else self._find_memory_by_id(memory_id)
        created = {
            "id": uuid4(),
            "user_id": memory["user_id"] if memory is not None else uuid4(),
            "memory_id": memory_id,
            "title": title,
            "status": status,
            "opened_at": self.base_time if opened_at is None else opened_at,
            "due_at": due_at,
            "resolved_at": resolved_at,
            "resolution_note": resolution_note,
            "created_at": self.base_time,
            "updated_at": self.base_time,
        }
        self.open_loops.append(created)
        return created


def seed_event(store: MemoryStoreStub, *, agent_profile_id: str = "assistant_default") -> UUID:
    event_id = uuid4()
    thread_id = uuid4()
    store.threads[thread_id] = {
        "id": thread_id,
        "agent_profile_id": agent_profile_id,
    }
    store.events[event_id] = {
        "id": event_id,
        "thread_id": thread_id,
        "sequence_no": 1,
        "kind": "message.user",
        "payload": {"text": "evidence"},
        "created_at": store.base_time,
    }
    return event_id


def test_admit_memory_candidate_defaults_to_noop_when_value_is_missing() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value=None,
            source_event_ids=(event_id,),
        ),
    )

    assert decision.action == "NOOP"
    assert decision.reason == "candidate_value_missing"
    assert decision.memory is None
    assert decision.revision is None


def test_admit_memory_candidate_rejects_missing_source_events() -> None:
    store = MemoryStoreStub()

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="source_event_ids must all reference existing events owned by the user",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.tea",
                value={"likes": True},
                source_event_ids=(uuid4(),),
            ),
        )


def test_admit_memory_candidate_rejects_empty_source_event_ids() -> None:
    store = MemoryStoreStub()

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="source_event_ids must include at least one existing event owned by the user",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.tea",
                value={"likes": True},
                source_event_ids=(),
            ),
        )


def test_admit_memory_candidate_rejects_invalid_memory_type() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="memory_type must be one of:",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.tea",
                value={"likes": True},
                source_event_ids=(event_id,),
                memory_type="not_a_valid_type",
            ),
        )


def test_admit_memory_candidate_defaults_llm_single_source_to_not_promotable() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.fact.weather",
            value={"summary": "Rain tomorrow"},
            source_event_ids=(event_id,),
            trust_class="llm_single_source",
        ),
    )

    assert decision.action == "ADD"
    assert decision.memory is not None
    assert decision.memory["trust_class"] == "llm_single_source"
    assert decision.memory["promotion_eligibility"] == "not_promotable"


def test_admit_memory_candidate_refuses_a_secret_name_in_the_value() -> None:
    """The value mapping is read with its keys. The value alone is not a secret.

    Passing candidate.value through string_values stores this row.
    """

    from alicebot_api.credential_floor import carries_credential_material

    opaque = "Xq9mZt2L" + "xP9wKc4BVq7m"
    assert not carries_credential_material(opaque)
    store = MemoryStoreStub()
    event_id = seed_event(store)

    with pytest.raises(MemoryAdmissionValidationError, match="credential material"):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.note.billing",
                value={"note": "keep", "api_key": opaque},
                source_event_ids=(event_id,),
            ),
        )

    assert store.memories == {}


def test_admit_memory_candidate_keeps_a_routing_session_key() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.note.route",
            value={"session_key": "agent:main:telegram:dm:4471", "note": "short replies"},
            source_event_ids=(event_id,),
        ),
    )

    assert decision.action == "ADD"
    assert decision.memory is not None
    assert decision.memory["value"]["session_key"] == "agent:main:telegram:dm:4471"


def test_admit_memory_candidate_adds_new_memory_with_first_revision() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=(event_id,),
        ),
    )

    assert decision.action == "ADD"
    assert decision.reason == "source_backed_add"
    assert decision.memory is not None
    assert decision.memory["memory_key"] == "user.preference.coffee"
    assert decision.memory["status"] == "active"
    assert decision.revision is not None
    assert decision.revision["sequence_no"] == 1
    assert decision.revision["action"] == "ADD"
    assert decision.revision["new_value"] == {"likes": "oat milk"}


def test_admit_memory_candidate_creates_open_loop_when_requested() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=(event_id,),
            open_loop=OpenLoopCandidateInput(
                title="Confirm this preference before next reorder",
                due_at=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
            ),
        ),
    )

    assert decision.action == "ADD"
    assert decision.open_loop is not None
    assert decision.open_loop["memory_id"] == decision.memory["id"]
    assert decision.open_loop["title"] == "Confirm this preference before next reorder"
    assert decision.open_loop["status"] == "open"
    assert decision.open_loop["due_at"] == "2026-03-20T09:00:00+00:00"


def test_admit_memory_candidate_updates_existing_memory_and_appends_revision() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)
    created = store.create_memory(
        memory_key="user.preference.coffee",
        value={"likes": "black"},
        status="active",
        source_event_ids=[str(event_id)],
    )
    store.append_memory_revision(
        memory_id=created["id"],
        action="ADD",
        memory_key="user.preference.coffee",
        previous_value=None,
        new_value={"likes": "black"},
        source_event_ids=[str(event_id)],
        candidate={
            "memory_key": "user.preference.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(event_id)],
            "delete_requested": False,
        },
    )

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=(event_id,),
        ),
    )

    assert decision.action == "UPDATE"
    assert decision.reason == "source_backed_update"
    assert decision.memory is not None
    assert decision.memory["value"] == {"likes": "oat milk"}
    assert decision.revision is not None
    assert decision.revision["sequence_no"] == 2
    assert decision.revision["previous_value"] == {"likes": "black"}
    assert decision.revision["new_value"] == {"likes": "oat milk"}


def test_admit_memory_candidate_marks_memory_deleted_and_appends_revision() -> None:
    store = MemoryStoreStub()
    event_id = seed_event(store)
    created = store.create_memory(
        memory_key="user.preference.coffee",
        value={"likes": "black"},
        status="active",
        source_event_ids=[str(event_id)],
    )

    decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value=None,
            source_event_ids=(event_id,),
            delete_requested=True,
        ),
    )

    assert decision.action == "DELETE"
    assert decision.reason == "source_backed_delete"
    assert decision.memory is not None
    assert UUID(decision.memory["id"]) == created["id"]
    assert decision.memory["status"] == "deleted"
    assert decision.revision is not None
    assert decision.revision["sequence_no"] == 1
    assert decision.revision["action"] == "DELETE"
    assert decision.revision["new_value"] is None


def test_admit_memory_candidate_scopes_upserts_by_derived_agent_profile() -> None:
    store = MemoryStoreStub()
    assistant_event_id = seed_event(store, agent_profile_id="assistant_default")
    coach_event_id = seed_event(store, agent_profile_id="coach_default")

    assistant_decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "black"},
            source_event_ids=(assistant_event_id,),
        ),
    )
    coach_add_decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=(coach_event_id,),
        ),
    )
    coach_update_decision = admit_memory_candidate(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        candidate=MemoryCandidateInput(
            memory_key="user.preference.coffee",
            value={"likes": "macchiato"},
            source_event_ids=(coach_event_id,),
        ),
    )

    assert assistant_decision.action == "ADD"
    assert coach_add_decision.action == "ADD"
    assert coach_update_decision.action == "UPDATE"
    assert assistant_decision.memory is not None
    assert coach_add_decision.memory is not None
    assert coach_update_decision.memory is not None
    assert assistant_decision.memory["id"] != coach_add_decision.memory["id"]
    assert coach_update_decision.memory["id"] == coach_add_decision.memory["id"]
    assert assistant_decision.memory["value"] == {"likes": "black"}
    assert coach_update_decision.memory["value"] == {"likes": "macchiato"}


def test_admit_memory_candidate_rejects_mixed_profile_source_events() -> None:
    store = MemoryStoreStub()
    assistant_event_id = seed_event(store, agent_profile_id="assistant_default")
    coach_event_id = seed_event(store, agent_profile_id="coach_default")

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="source_event_ids must all belong to threads with the same agent_profile_id",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "black"},
                source_event_ids=(assistant_event_id, coach_event_id),
            ),
        )


def test_admit_memory_candidate_rejects_explicit_agent_profile_mismatch() -> None:
    store = MemoryStoreStub()
    assistant_event_id = seed_event(store, agent_profile_id="assistant_default")

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="agent_profile_id must match the profile resolved from source_event_ids",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "black"},
                source_event_ids=(assistant_event_id,),
                agent_profile_id="coach_default",
            ),
        )


def test_admit_memory_candidate_rejects_unknown_agent_profile_id() -> None:
    store = MemoryStoreStub()
    assistant_event_id = seed_event(store, agent_profile_id="assistant_default")

    with pytest.raises(
        MemoryAdmissionValidationError,
        match="agent_profile_id must reference an existing profile: unknown_profile",
    ):
        admit_memory_candidate(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "black"},
                source_event_ids=(assistant_event_id,),
                agent_profile_id="unknown_profile",
            ),
        )


class MemoryReviewStoreStub:
    def __init__(self) -> None:
        self.memories: list[dict[str, object]] = []
        self.revisions: dict[UUID, list[dict[str, object]]] = {}
        self.labels: dict[UUID, list[dict[str, object]]] = {}
        self.open_contradiction_count = 0

    def count_memories(self, *, status: str | None = None) -> int:
        return len(self._filtered_memories(status))

    def list_review_memories(self, *, status: str | None = None, limit: int) -> list[dict[str, object]]:
        return self._review_sorted_memories(self._filtered_memories(status))[:limit]

    def count_unlabeled_review_memories(self) -> int:
        return len(
            [memory for memory in self.memories if memory["status"] == "active" and not self.labels.get(memory["id"])]
        )

    def list_unlabeled_review_memories(self, *, limit: int | None = None) -> list[dict[str, object]]:
        items = self._review_sorted_memories(
            [
                memory
                for memory in self.memories
                if memory["status"] == "active" and not self.labels.get(memory["id"])
            ]
        )
        if limit is None:
            return items
        return items[:limit]

    def get_memory_optional(self, memory_id: UUID) -> dict[str, object] | None:
        for memory in self.memories:
            if memory["id"] == memory_id:
                return memory
        return None

    def count_memory_revisions(self, memory_id: UUID) -> int:
        return len(self.revisions.get(memory_id, []))

    def list_memory_revisions(self, memory_id: UUID, *, limit: int | None = None) -> list[dict[str, object]]:
        revisions = self.revisions.get(memory_id, [])
        if limit is None:
            return revisions
        return revisions[:limit]

    def create_memory_review_label(
        self,
        *,
        memory_id: UUID,
        label: str,
        note: str | None,
    ) -> dict[str, object]:
        memory = self.get_memory_optional(memory_id)
        created = {
            "id": uuid4(),
            "user_id": uuid4() if memory is None else memory["user_id"],
            "memory_id": memory_id,
            "label": label,
            "note": note,
            "created_at": datetime(2026, 3, 11, 13, len(self.labels.get(memory_id, [])), tzinfo=UTC),
        }
        self.labels.setdefault(memory_id, []).append(created)
        return created

    def list_memory_review_labels(self, memory_id: UUID) -> list[dict[str, object]]:
        return list(self.labels.get(memory_id, []))

    def list_memory_review_label_counts(self, memory_id: UUID) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        for label in self.labels.get(memory_id, []):
            label_name = label["label"]
            counts[label_name] = counts.get(label_name, 0) + 1
        return [{"label": label, "count": count} for label, count in sorted(counts.items())]

    def count_labeled_memories(self) -> int:
        return len([memory for memory in self.memories if self.labels.get(memory["id"])])

    def count_unlabeled_memories(self) -> int:
        return len([memory for memory in self.memories if not self.labels.get(memory["id"])])

    def list_all_memory_review_label_counts(self) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        for labels in self.labels.values():
            for label in labels:
                label_name = label["label"]
                counts[label_name] = counts.get(label_name, 0) + 1
        return [{"label": label, "count": count} for label, count in sorted(counts.items())]

    def list_active_memory_review_label_counts(self) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        for memory in self.memories:
            if memory["status"] != "active":
                continue
            for label in self.labels.get(memory["id"], []):
                label_name = label["label"]
                counts[label_name] = counts.get(label_name, 0) + 1
        return [{"label": label, "count": count} for label, count in sorted(counts.items())]

    def list_continuity_recall_candidates(self) -> list[dict[str, object]]:
        return []

    def list_continuity_correction_events(
        self,
        *,
        continuity_object_id: UUID,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        del continuity_object_id
        del limit
        return []

    def count_contradiction_cases(self, *, statuses: list[str]) -> int:
        assert statuses == ["open"]
        return self.open_contradiction_count

    def _filtered_memories(self, status: str | None) -> list[dict[str, object]]:
        if status is None:
            return list(self.memories)
        return [memory for memory in self.memories if memory["status"] == status]

    def _review_sorted_memories(self, memories: list[dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            memories,
            key=lambda memory: (memory["updated_at"], memory["created_at"], memory["id"]),
            reverse=True,
        )


class OpenLoopStoreStub:
    def __init__(self) -> None:
        self.base_time = datetime(2026, 3, 23, 12, 0, tzinfo=UTC)
        self.memories: dict[UUID, dict[str, object]] = {}
        self.open_loops: dict[UUID, dict[str, object]] = {}

    def get_memory_optional(self, memory_id: UUID) -> dict[str, object] | None:
        return self.memories.get(memory_id)

    def count_open_loops(self, *, status: str | None = None) -> int:
        if status is None:
            return len(self.open_loops)
        return len([item for item in self.open_loops.values() if item["status"] == status])

    def list_open_loops(self, *, status: str | None = None, limit: int | None = None) -> list[dict[str, object]]:
        items = list(self.open_loops.values())
        if status is not None:
            items = [item for item in items if item["status"] == status]
        ordered = sorted(
            items,
            key=lambda item: (item["opened_at"], item["created_at"], item["id"]),
            reverse=True,
        )
        if limit is None:
            return ordered
        return ordered[:limit]

    def create_open_loop(
        self,
        *,
        memory_id: UUID | None,
        title: str,
        status: str,
        opened_at: datetime | None,
        due_at: datetime | None,
        resolved_at: datetime | None,
        resolution_note: str | None,
    ) -> dict[str, object]:
        open_loop_id = uuid4()
        loop = {
            "id": open_loop_id,
            "user_id": uuid4(),
            "memory_id": memory_id,
            "title": title,
            "status": status,
            "opened_at": self.base_time if opened_at is None else opened_at,
            "due_at": due_at,
            "resolved_at": resolved_at,
            "resolution_note": resolution_note,
            "created_at": self.base_time,
            "updated_at": self.base_time,
        }
        self.open_loops[open_loop_id] = loop
        return loop

    def get_open_loop_optional(self, open_loop_id: UUID) -> dict[str, object] | None:
        return self.open_loops.get(open_loop_id)

    def update_open_loop_status_optional(
        self,
        *,
        open_loop_id: UUID,
        status: str,
        resolved_at: datetime | None,
        resolution_note: str | None,
    ) -> dict[str, object] | None:
        existing = self.open_loops.get(open_loop_id)
        if existing is None:
            return None
        updated = {
            **existing,
            "status": status,
            "resolved_at": self.base_time + timedelta(minutes=5) if resolved_at is None else resolved_at,
            "resolution_note": resolution_note,
            "updated_at": self.base_time + timedelta(minutes=5),
        }
        self.open_loops[open_loop_id] = updated
        return updated


def test_open_loop_records_support_create_list_get_and_status_transition() -> None:
    store = OpenLoopStoreStub()
    memory_id = uuid4()
    store.memories[memory_id] = {"id": memory_id}

    created = create_open_loop_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        open_loop=OpenLoopCreateInput(
            memory_id=memory_id,
            title="Follow up with merchant confirmation",
            due_at=datetime(2026, 3, 27, 10, 0, tzinfo=UTC),
        ),
    )
    open_loop_id = UUID(created["open_loop"]["id"])

    listed = list_open_loop_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        status="open",
        limit=10,
    )
    detail = get_open_loop_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        open_loop_id=open_loop_id,
    )
    updated = update_open_loop_status_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        open_loop_id=open_loop_id,
        request=OpenLoopStatusUpdateInput(
            status="resolved",
            resolution_note="Resolved in latest review pass.",
        ),
    )

    assert created["open_loop"]["status"] == "open"
    assert listed["summary"] == {
        "status": "open",
        "limit": 10,
        "returned_count": 1,
        "total_count": 1,
        "has_more": False,
        "order": ["opened_at_desc", "created_at_desc", "id_desc"],
    }
    assert detail["open_loop"]["id"] == str(open_loop_id)
    assert updated["open_loop"]["status"] == "resolved"
    assert updated["open_loop"]["resolution_note"] == "Resolved in latest review pass."
    assert updated["open_loop"]["resolved_at"] is not None


def test_open_loop_records_support_dismissed_transition_with_audit_fields() -> None:
    store = OpenLoopStoreStub()
    memory_id = uuid4()
    store.memories[memory_id] = {"id": memory_id}

    created = create_open_loop_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        open_loop=OpenLoopCreateInput(
            memory_id=memory_id,
            title="Dismiss after confirming no further action is needed",
            due_at=None,
        ),
    )

    dismissed = update_open_loop_status_record(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        open_loop_id=UUID(created["open_loop"]["id"]),
        request=OpenLoopStatusUpdateInput(
            status="dismissed",
            resolution_note="No action required after review.",
        ),
    )

    assert dismissed["open_loop"]["status"] == "dismissed"
    assert dismissed["open_loop"]["resolved_at"] is not None
    assert dismissed["open_loop"]["resolution_note"] == "No action required after review."


def test_open_loop_status_update_rejects_invalid_status_and_missing_records() -> None:
    store = OpenLoopStoreStub()
    loop_id = uuid4()
    store.open_loops[loop_id] = {
        "id": loop_id,
        "user_id": uuid4(),
        "memory_id": None,
        "title": "Investigate",
        "status": "open",
        "opened_at": store.base_time,
        "due_at": None,
        "resolved_at": None,
        "resolution_note": None,
        "created_at": store.base_time,
        "updated_at": store.base_time,
    }

    with pytest.raises(OpenLoopValidationError, match="status must be one of"):
        update_open_loop_status_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            open_loop_id=loop_id,
            request=OpenLoopStatusUpdateInput(status="invalid"),  # type: ignore[arg-type]
        )

    with pytest.raises(OpenLoopNotFoundError, match="was not found"):
        get_open_loop_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            open_loop_id=uuid4(),
        )


def test_list_memory_review_records_returns_summary_and_stable_shape() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    deleted_time = base_time + timedelta(minutes=1)
    active_time = base_time + timedelta(minutes=2)
    deleted_id = uuid4()
    active_id = uuid4()
    store.memories = [
        {
            "id": active_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time,
            "updated_at": active_time,
            "deleted_at": None,
        },
        {
            "id": deleted_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.tea",
            "value": {"likes": "green"},
            "status": "deleted",
            "source_event_ids": ["event-1"],
            "created_at": base_time,
            "updated_at": deleted_time,
            "deleted_at": deleted_time,
        },
    ]

    payload = list_memory_review_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        status="all",
        limit=1,
    )

    assert payload == {
        "items": [
            {
                "id": str(active_id),
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["event-2"],
                "created_at": "2026-03-11T12:00:00+00:00",
                "updated_at": "2026-03-11T12:02:00+00:00",
                "deleted_at": None,
            }
        ],
        "summary": {
            "status": "all",
            "limit": 1,
            "returned_count": 1,
            "total_count": 2,
            "has_more": True,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }


def test_get_memory_review_record_raises_not_found_for_inaccessible_memory() -> None:
    store = MemoryReviewStoreStub()

    with pytest.raises(MemoryReviewNotFoundError, match="was not found"):
        get_memory_review_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            memory_id=uuid4(),
        )


def test_list_memory_review_queue_records_returns_only_active_unlabeled_memories_in_stable_order() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    deleted_id = uuid4()
    labeled_id = uuid4()
    newest_unlabeled_id = uuid4()
    older_unlabeled_id = uuid4()
    store.memories = [
        {
            "id": newest_unlabeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-4"],
            "created_at": base_time + timedelta(minutes=3),
            "updated_at": base_time + timedelta(minutes=6),
            "deleted_at": None,
        },
        {
            "id": labeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.snack",
            "value": {"likes": "chips"},
            "status": "active",
            "source_event_ids": ["event-3"],
            "created_at": base_time + timedelta(minutes=2),
            "updated_at": base_time + timedelta(minutes=5),
            "deleted_at": None,
        },
        {
            "id": older_unlabeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.book",
            "value": {"genre": "science fiction"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time + timedelta(minutes=1),
            "updated_at": base_time + timedelta(minutes=4),
            "deleted_at": None,
        },
        {
            "id": deleted_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.tea",
            "value": {"likes": "green"},
            "status": "deleted",
            "source_event_ids": ["event-1"],
            "created_at": base_time,
            "updated_at": base_time + timedelta(minutes=7),
            "deleted_at": base_time + timedelta(minutes=7),
        },
    ]
    store.labels[labeled_id] = [
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": labeled_id,
            "label": "correct",
            "note": "Already reviewed.",
            "created_at": base_time + timedelta(minutes=8),
        }
    ]

    payload = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=2,
    )

    assert payload == {
        "items": [
            {
                "id": str(newest_unlabeled_id),
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["event-4"],
                "is_high_risk": True,
                "is_stale_truth": False,
                "is_promotable": True,
                "queue_priority_mode": "recent_first",
                "priority_reason": "recent_first",
                "created_at": "2026-03-11T12:03:00+00:00",
                "updated_at": "2026-03-11T12:06:00+00:00",
            },
            {
                "id": str(older_unlabeled_id),
                "memory_key": "user.preference.book",
                "value": {"genre": "science fiction"},
                "status": "active",
                "source_event_ids": ["event-2"],
                "is_high_risk": True,
                "is_stale_truth": False,
                "is_promotable": True,
                "queue_priority_mode": "recent_first",
                "priority_reason": "recent_first",
                "created_at": "2026-03-11T12:01:00+00:00",
                "updated_at": "2026-03-11T12:04:00+00:00",
            },
        ],
        "summary": {
            "memory_status": "active",
            "review_state": "unlabeled",
            "priority_mode": "recent_first",
            "available_priority_modes": [
                "oldest_first",
                "recent_first",
                "high_risk_first",
                "stale_truth_first",
            ],
            "limit": 2,
            "returned_count": 2,
            "total_count": 2,
            "has_more": False,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }


def test_list_memory_review_queue_records_marks_not_promotable_fact_as_high_risk() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    memory_id = uuid4()
    store.memories = [
        {
            "id": memory_id,
            "user_id": uuid4(),
            "memory_key": "user.fact.single_source",
            "value": {"claim": "single source"},
            "status": "active",
            "source_event_ids": ["event-1"],
            "trust_class": "llm_single_source",
            "promotion_eligibility": "not_promotable",
            "created_at": base_time,
            "updated_at": base_time,
            "deleted_at": None,
        }
    ]

    payload = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=1,
    )

    assert payload["items"][0]["id"] == str(memory_id)
    assert payload["items"][0]["is_promotable"] is False
    assert payload["items"][0]["is_high_risk"] is True
    assert payload["items"][0]["priority_reason"] == "recent_not_promotable"


def test_list_memory_review_queue_records_supports_all_priority_modes_with_deterministic_order() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    oldest_id = uuid4()
    middle_id = uuid4()
    newest_id = uuid4()
    store.memories = [
        {
            "id": oldest_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.oldest",
            "value": {"value": "oldest"},
            "status": "active",
            "source_event_ids": ["event-1"],
            "confirmation_status": "contested",
            "confidence": 0.9,
            "valid_to": datetime(2026, 3, 1, tzinfo=UTC),
            "created_at": base_time,
            "updated_at": base_time,
            "deleted_at": None,
        },
        {
            "id": middle_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.middle",
            "value": {"value": "middle"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "confirmation_status": "confirmed",
            "confidence": 0.95,
            "created_at": base_time + timedelta(minutes=1),
            "updated_at": base_time + timedelta(minutes=1),
            "deleted_at": None,
        },
        {
            "id": newest_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.newest",
            "value": {"value": "newest"},
            "status": "active",
            "source_event_ids": ["event-3"],
            "confirmation_status": "confirmed",
            "confidence": 0.2,
            "created_at": base_time + timedelta(minutes=2),
            "updated_at": base_time + timedelta(minutes=2),
            "deleted_at": None,
        },
    ]

    oldest_first = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=3,
        priority_mode="oldest_first",
    )
    recent_first = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=3,
        priority_mode="recent_first",
    )
    high_risk_first = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=3,
        priority_mode="high_risk_first",
    )
    stale_truth_first = list_memory_review_queue_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        limit=3,
        priority_mode="stale_truth_first",
    )

    assert [item["id"] for item in oldest_first["items"]] == [
        str(oldest_id),
        str(middle_id),
        str(newest_id),
    ]
    assert [item["id"] for item in recent_first["items"]] == [
        str(newest_id),
        str(middle_id),
        str(oldest_id),
    ]
    assert [item["id"] for item in high_risk_first["items"]] == [
        str(newest_id),
        str(oldest_id),
        str(middle_id),
    ]
    assert [item["id"] for item in stale_truth_first["items"]] == [
        str(oldest_id),
        str(newest_id),
        str(middle_id),
    ]
    assert high_risk_first["summary"]["priority_mode"] == "high_risk_first"
    assert high_risk_first["summary"]["order"] == [
        "is_high_risk_desc",
        "confidence_asc_nulls_first",
        "updated_at_desc",
        "created_at_desc",
        "id_desc",
    ]
    assert stale_truth_first["items"][0]["is_stale_truth"] is True


def test_get_memory_quality_gate_summary_returns_canonical_status_transitions() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    def build_active_memory(memory_id: UUID, index: int) -> dict[str, object]:
        return {
            "id": memory_id,
            "user_id": uuid4(),
            "memory_key": f"user.preference.item_{index}",
            "value": {"index": index},
            "status": "active",
            "source_event_ids": [f"event-{index}"],
            "confirmation_status": "confirmed",
            "confidence": 0.95,
            "valid_to": None,
            "created_at": base_time + timedelta(minutes=index),
            "updated_at": base_time + timedelta(minutes=index),
            "deleted_at": None,
        }

    memory_ids = [uuid4() for _ in range(11)]
    store.memories = [build_active_memory(memory_id, index) for index, memory_id in enumerate(memory_ids)]

    def assign_labels(*, correct: int, incorrect: int, outdated_memory_ids: set[UUID] | None = None) -> None:
        store.labels = {}
        outdated_memory_ids = outdated_memory_ids or set()
        cursor = 0
        for _ in range(correct):
            memory_id = memory_ids[cursor]
            store.labels.setdefault(memory_id, []).append(
                {
                    "id": uuid4(),
                    "user_id": uuid4(),
                    "memory_id": memory_id,
                    "label": "correct",
                    "note": None,
                    "created_at": base_time + timedelta(hours=1, minutes=cursor),
                }
            )
            cursor += 1
        for _ in range(incorrect):
            memory_id = memory_ids[cursor]
            store.labels.setdefault(memory_id, []).append(
                {
                    "id": uuid4(),
                    "user_id": uuid4(),
                    "memory_id": memory_id,
                    "label": "incorrect",
                    "note": None,
                    "created_at": base_time + timedelta(hours=2, minutes=cursor),
                }
            )
            cursor += 1
        for memory_id in outdated_memory_ids:
            store.labels.setdefault(memory_id, []).append(
                {
                    "id": uuid4(),
                    "user_id": uuid4(),
                    "memory_id": memory_id,
                    "label": "outdated",
                    "note": "Superseded.",
                    "created_at": base_time + timedelta(hours=3),
                }
            )

    assign_labels(correct=1, incorrect=0)
    insufficient = get_memory_quality_gate_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    assert insufficient["summary"]["status"] == "insufficient_sample"
    assert insufficient["summary"]["adjudicated_sample_count"] == 1
    assert insufficient["summary"]["remaining_to_minimum_sample"] == 9

    assign_labels(correct=7, incorrect=3)
    degraded_precision = get_memory_quality_gate_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    assert degraded_precision["summary"]["status"] == "degraded"
    assert degraded_precision["summary"]["precision"] == 0.7

    assign_labels(correct=10, incorrect=0, outdated_memory_ids={memory_ids[0]})
    degraded_conflict = get_memory_quality_gate_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    assert degraded_conflict["summary"]["status"] == "degraded"
    assert degraded_conflict["summary"]["superseded_active_conflict_count"] == 1

    assign_labels(correct=10, incorrect=0)
    needs_review_memory = next(memory for memory in store.memories if memory["id"] == memory_ids[10])
    needs_review_memory["confirmation_status"] = "unconfirmed"
    needs_review = get_memory_quality_gate_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    assert needs_review["summary"]["status"] == "needs_review"
    assert needs_review["summary"]["high_risk_memory_count"] >= 1

    assign_labels(correct=11, incorrect=0)
    needs_review_memory["confirmation_status"] = "confirmed"
    healthy = get_memory_quality_gate_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    assert healthy["summary"]["status"] == "healthy"
    assert healthy["summary"]["precision"] == 1.0
    assert healthy["summary"]["high_risk_memory_count"] == 0
    assert healthy["summary"]["stale_truth_count"] == 0
    assert healthy["summary"]["superseded_active_conflict_count"] == 0


def test_get_memory_trust_dashboard_summary_is_deterministic_and_uses_canonical_components() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    labeled_memory_ids = [uuid4() for _ in range(10)]
    for index, memory_id in enumerate(labeled_memory_ids):
        store.memories.append(
            {
                "id": memory_id,
                "user_id": uuid4(),
                "memory_key": f"user.preference.reviewed_{index}",
                "value": {"index": index},
                "status": "active",
                "source_event_ids": [f"event-reviewed-{index}"],
                "confirmation_status": "confirmed",
                "confidence": 0.95,
                "valid_to": None,
                "created_at": base_time + timedelta(minutes=index),
                "updated_at": base_time + timedelta(minutes=index),
                "deleted_at": None,
            }
        )
        store.labels[memory_id] = [
            {
                "id": uuid4(),
                "user_id": uuid4(),
                "memory_id": memory_id,
                "label": "correct",
                "note": None,
                "created_at": base_time + timedelta(hours=1, minutes=index),
            }
        ]

    high_risk_memory_id = uuid4()
    stale_truth_memory_id = uuid4()
    store.memories.extend(
        [
            {
                "id": high_risk_memory_id,
                "user_id": uuid4(),
                "memory_key": "user.preference.high_risk",
                "value": {"state": "high_risk"},
                "status": "active",
                "source_event_ids": ["event-high-risk"],
                "confirmation_status": "unconfirmed",
                "confidence": 0.2,
                "valid_to": None,
                "created_at": base_time + timedelta(hours=2),
                "updated_at": base_time + timedelta(hours=2),
                "deleted_at": None,
            },
            {
                "id": stale_truth_memory_id,
                "user_id": uuid4(),
                "memory_key": "user.preference.stale_truth",
                "value": {"state": "stale_truth"},
                "status": "active",
                "source_event_ids": ["event-stale-truth"],
                "confirmation_status": "contested",
                "confidence": 0.95,
                "valid_to": base_time - timedelta(days=1),
                "created_at": base_time,
                "updated_at": base_time,
                "deleted_at": None,
            },
        ]
    )

    first = get_memory_trust_dashboard_summary(store, user_id=uuid4())  # type: ignore[arg-type]
    second = get_memory_trust_dashboard_summary(store, user_id=uuid4())  # type: ignore[arg-type]

    assert first == second
    assert first["dashboard"]["quality_gate"]["status"] == "needs_review"
    assert first["dashboard"]["queue_posture"]["total_count"] == 2
    assert first["dashboard"]["queue_posture"]["high_risk_count"] == 2
    assert first["dashboard"]["queue_posture"]["stale_truth_count"] == 1
    assert first["dashboard"]["retrieval_quality"]["fixture_count"] == 7
    assert first["dashboard"]["correction_freshness"] == {
        "total_open_loop_count": 0,
        "stale_open_loop_count": 0,
        "correction_recurrence_count": 0,
        "freshness_drift_count": 0,
    }
    assert first["dashboard"]["recommended_review"]["priority_mode"] == "high_risk_first"
    assert first["dashboard"]["recommended_review"]["action"] == "review_high_risk_queue"


def test_get_memory_trust_dashboard_summary_handles_missing_continuity_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryReviewStoreStub()

    def _raise_missing_continuity_table(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        del kwargs
        raise psycopg.errors.UndefinedTable('relation "continuity_objects" does not exist')

    monkeypatch.setattr(memory_module, "compile_continuity_weekly_review", _raise_missing_continuity_table)

    payload = get_memory_trust_dashboard_summary(store, user_id=uuid4())  # type: ignore[arg-type]

    assert payload["dashboard"]["correction_freshness"] == {
        "total_open_loop_count": 0,
        "stale_open_loop_count": 0,
        "correction_recurrence_count": 0,
        "freshness_drift_count": 0,
    }


def test_get_memory_hygiene_dashboard_summary_surfaces_duplicates_stale_and_queue_pressure() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    duplicate_a = uuid4()
    duplicate_b = uuid4()
    stale_memory = uuid4()
    weak_memory = uuid4()
    store.memories = [
        {
            "id": duplicate_a,
            "user_id": uuid4(),
            "memory_key": "user.preference.store",
            "value": {"merchant": "Fixture Store"},
            "status": "active",
            "source_event_ids": ["event-1"],
            "memory_type": "preference",
            "confirmation_status": "confirmed",
            "confidence": 0.95,
            "promotion_eligibility": "promotable",
            "trust_class": "deterministic",
            "valid_to": None,
            "created_at": base_time,
            "updated_at": base_time + timedelta(hours=1),
            "deleted_at": None,
        },
        {
            "id": duplicate_b,
            "user_id": uuid4(),
            "memory_key": "user.preference.backup_store",
            "value": {"merchant": "Fixture Store"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "memory_type": "preference",
            "confirmation_status": "confirmed",
            "confidence": 0.91,
            "promotion_eligibility": "promotable",
            "trust_class": "deterministic",
            "valid_to": None,
            "created_at": base_time,
            "updated_at": base_time + timedelta(hours=2),
            "deleted_at": None,
        },
        {
            "id": stale_memory,
            "user_id": uuid4(),
            "memory_key": "user.fact.trip_window",
            "value": {"status": "expired"},
            "status": "active",
            "source_event_ids": ["event-3"],
            "memory_type": "fact",
            "confirmation_status": "contested",
            "confidence": 0.88,
            "promotion_eligibility": "promotable",
            "trust_class": "deterministic",
            "valid_to": base_time - timedelta(days=1),
            "created_at": base_time,
            "updated_at": base_time + timedelta(hours=3),
            "deleted_at": None,
        },
        {
            "id": weak_memory,
            "user_id": uuid4(),
            "memory_key": "user.fact.single_source",
            "value": {"status": "tentative"},
            "status": "active",
            "source_event_ids": ["event-4"],
            "memory_type": "fact",
            "confirmation_status": "unconfirmed",
            "confidence": 0.4,
            "promotion_eligibility": "not_promotable",
            "trust_class": "llm_single_source",
            "valid_to": None,
            "created_at": base_time,
            "updated_at": base_time + timedelta(hours=4),
            "deleted_at": None,
        },
    ]
    store.open_contradiction_count = 1

    payload = get_memory_hygiene_dashboard_summary(store, user_id=uuid4())  # type: ignore[arg-type]

    assert payload["dashboard"]["posture"] == "critical"
    assert payload["dashboard"]["duplicate_group_count"] == 1
    assert payload["dashboard"]["duplicate_memory_count"] == 2
    assert payload["dashboard"]["stale_fact_count"] == 1
    assert payload["dashboard"]["unresolved_contradiction_count"] == 1
    assert payload["dashboard"]["weak_trust_count"] == 2
    assert payload["dashboard"]["review_queue_pressure"]["total_count"] == 4
    assert payload["dashboard"]["focus"][0]["kind"] == "duplicates"
    assert payload["dashboard"]["duplicate_groups"][0]["memory_keys"] == [
        "user.preference.backup_store",
        "user.preference.store",
    ]


def test_list_memory_revision_review_records_returns_deterministic_revision_order() -> None:
    store = MemoryReviewStoreStub()
    memory_id = uuid4()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    store.memories = [
        {
            "id": memory_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time,
            "updated_at": base_time + timedelta(minutes=2),
            "deleted_at": None,
        }
    ]
    store.revisions[memory_id] = [
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": memory_id,
            "sequence_no": 1,
            "action": "ADD",
            "memory_key": "user.preference.coffee",
            "previous_value": None,
            "new_value": {"likes": "black"},
            "source_event_ids": ["event-1"],
            "candidate": {"memory_key": "user.preference.coffee"},
            "created_at": base_time,
        },
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": memory_id,
            "sequence_no": 2,
            "action": "UPDATE",
            "memory_key": "user.preference.coffee",
            "previous_value": {"likes": "black"},
            "new_value": {"likes": "oat milk"},
            "source_event_ids": ["event-2"],
            "candidate": {"memory_key": "user.preference.coffee"},
            "created_at": base_time + timedelta(minutes=1),
        },
    ]

    payload = list_memory_revision_review_records(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        memory_id=memory_id,
        limit=10,
    )

    assert payload == {
        "items": [
            {
                "id": str(store.revisions[memory_id][0]["id"]),
                "memory_id": str(memory_id),
                "sequence_no": 1,
                "action": "ADD",
                "memory_key": "user.preference.coffee",
                "previous_value": None,
                "new_value": {"likes": "black"},
                "source_event_ids": ["event-1"],
                "created_at": "2026-03-11T12:00:00+00:00",
            },
            {
                "id": str(store.revisions[memory_id][1]["id"]),
                "memory_id": str(memory_id),
                "sequence_no": 2,
                "action": "UPDATE",
                "memory_key": "user.preference.coffee",
                "previous_value": {"likes": "black"},
                "new_value": {"likes": "oat milk"},
                "source_event_ids": ["event-2"],
                "created_at": "2026-03-11T12:01:00+00:00",
            },
        ],
        "summary": {
            "memory_id": str(memory_id),
            "limit": 10,
            "returned_count": 2,
            "total_count": 2,
            "has_more": False,
            "order": ["sequence_no_asc"],
        },
    }


def test_create_memory_review_label_record_returns_created_label_and_summary_counts() -> None:
    store = MemoryReviewStoreStub()
    memory_id = uuid4()
    reviewer_user_id = uuid4()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    store.memories = [
        {
            "id": memory_id,
            "user_id": reviewer_user_id,
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time,
            "updated_at": base_time,
            "deleted_at": None,
        }
    ]
    store.labels[memory_id] = [
        {
            "id": uuid4(),
            "user_id": reviewer_user_id,
            "memory_id": memory_id,
            "label": "correct",
            "note": "Matches the latest cited event.",
            "created_at": datetime(2026, 3, 11, 12, 30, tzinfo=UTC),
        }
    ]

    payload = create_memory_review_label_record(
        store,  # type: ignore[arg-type]
        user_id=reviewer_user_id,
        memory_id=memory_id,
        label="outdated",
        note="Superseded by the newer milk preference.",
    )

    assert payload == {
        "label": {
            "id": payload["label"]["id"],
            "memory_id": str(memory_id),
            "reviewer_user_id": payload["label"]["reviewer_user_id"],
            "label": "outdated",
            "note": "Superseded by the newer milk preference.",
            "created_at": "2026-03-11T13:01:00+00:00",
        },
        "summary": {
            "memory_id": str(memory_id),
            "total_count": 2,
            "counts_by_label": {
                "correct": 1,
                "incorrect": 0,
                "outdated": 1,
                "insufficient_evidence": 0,
            },
            "order": ["created_at_asc", "id_asc"],
        },
    }


def test_create_memory_review_label_record_raises_not_found_for_inaccessible_memory() -> None:
    store = MemoryReviewStoreStub()

    with pytest.raises(MemoryReviewNotFoundError, match="was not found"):
        create_memory_review_label_record(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            memory_id=uuid4(),
            label="correct",
            note=None,
        )


def test_list_memory_review_label_records_returns_deterministic_order_and_zero_filled_counts() -> None:
    store = MemoryReviewStoreStub()
    memory_id = uuid4()
    reviewer_user_id = uuid4()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    store.memories = [
        {
            "id": memory_id,
            "user_id": reviewer_user_id,
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time,
            "updated_at": base_time,
            "deleted_at": None,
        }
    ]
    store.labels[memory_id] = [
        {
            "id": uuid4(),
            "user_id": reviewer_user_id,
            "memory_id": memory_id,
            "label": "incorrect",
            "note": "The source event only mentions tea.",
            "created_at": datetime(2026, 3, 11, 12, 15, tzinfo=UTC),
        },
        {
            "id": uuid4(),
            "user_id": reviewer_user_id,
            "memory_id": memory_id,
            "label": "insufficient_evidence",
            "note": None,
            "created_at": datetime(2026, 3, 11, 12, 16, tzinfo=UTC),
        },
    ]

    payload = list_memory_review_label_records(
        store,  # type: ignore[arg-type]
        user_id=reviewer_user_id,
        memory_id=memory_id,
    )

    assert payload == {
        "items": [
            {
                "id": str(store.labels[memory_id][0]["id"]),
                "memory_id": str(memory_id),
                "reviewer_user_id": str(reviewer_user_id),
                "label": "incorrect",
                "note": "The source event only mentions tea.",
                "created_at": "2026-03-11T12:15:00+00:00",
            },
            {
                "id": str(store.labels[memory_id][1]["id"]),
                "memory_id": str(memory_id),
                "reviewer_user_id": str(reviewer_user_id),
                "label": "insufficient_evidence",
                "note": None,
                "created_at": "2026-03-11T12:16:00+00:00",
            },
        ],
        "summary": {
            "memory_id": str(memory_id),
            "total_count": 2,
            "counts_by_label": {
                "correct": 0,
                "incorrect": 1,
                "outdated": 0,
                "insufficient_evidence": 1,
            },
            "order": ["created_at_asc", "id_asc"],
        },
    }


def test_get_memory_evaluation_summary_returns_explicit_consistent_counts() -> None:
    store = MemoryReviewStoreStub()
    base_time = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    active_labeled_id = uuid4()
    active_unlabeled_id = uuid4()
    deleted_labeled_id = uuid4()
    deleted_unlabeled_id = uuid4()
    store.memories = [
        {
            "id": active_labeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-1"],
            "created_at": base_time,
            "updated_at": base_time,
            "deleted_at": None,
        },
        {
            "id": active_unlabeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.book",
            "value": {"genre": "science fiction"},
            "status": "active",
            "source_event_ids": ["event-2"],
            "created_at": base_time + timedelta(minutes=1),
            "updated_at": base_time + timedelta(minutes=1),
            "deleted_at": None,
        },
        {
            "id": deleted_labeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.snack",
            "value": {"likes": "chips"},
            "status": "deleted",
            "source_event_ids": ["event-3"],
            "created_at": base_time + timedelta(minutes=2),
            "updated_at": base_time + timedelta(minutes=2),
            "deleted_at": base_time + timedelta(minutes=2),
        },
        {
            "id": deleted_unlabeled_id,
            "user_id": uuid4(),
            "memory_key": "user.preference.tea",
            "value": {"likes": "green"},
            "status": "deleted",
            "source_event_ids": ["event-4"],
            "created_at": base_time + timedelta(minutes=3),
            "updated_at": base_time + timedelta(minutes=3),
            "deleted_at": base_time + timedelta(minutes=3),
        },
    ]
    store.labels[active_labeled_id] = [
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": active_labeled_id,
            "label": "correct",
            "note": "Looks right.",
            "created_at": base_time + timedelta(minutes=4),
        },
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": active_labeled_id,
            "label": "insufficient_evidence",
            "note": "Needs another source.",
            "created_at": base_time + timedelta(minutes=5),
        },
    ]
    store.labels[deleted_labeled_id] = [
        {
            "id": uuid4(),
            "user_id": uuid4(),
            "memory_id": deleted_labeled_id,
            "label": "outdated",
            "note": None,
            "created_at": base_time + timedelta(minutes=6),
        }
    ]

    payload = get_memory_evaluation_summary(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
    )

    assert payload == {
        "summary": {
            "total_memory_count": 4,
            "active_memory_count": 2,
            "deleted_memory_count": 2,
            "labeled_memory_count": 2,
            "unlabeled_memory_count": 2,
            "total_label_row_count": 3,
            "label_row_counts_by_value": {
                "correct": 1,
                "incorrect": 0,
                "outdated": 1,
                "insufficient_evidence": 1,
            },
            "label_value_order": [
                "correct",
                "incorrect",
                "outdated",
                "insufficient_evidence",
            ],
        }
    }
