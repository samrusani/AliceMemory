"""Roll-up cards: deterministic grouping, review gate, revisions, recall.

The live-SQLite tests exercise the full loop the product runs: the
consolidation workflow proposes a roll-up candidate, a reviewer accepts it
through the memory commit service, and the accepted card becomes an
ordinary FTS-indexed memory that wins aggregation-phrased recall while
every member stays individually recallable.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from alicebot_api.vnext_embeddings import VNextEmbeddingProviderError
from alicebot_api.vnext_model_intelligence import VNextModelIntelligenceError
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_rollups import (
    ROLLUP_CANDIDATE_KIND,
    RollupOptions,
    VNextRollupService,
    VNextRollupValidationError,
    _CorpusStats,
    _instance_label,
    _instance_record,
    _label_junk_reason,
)


# -- fakes ---------------------------------------------------------------------


class FakeRollupStore:
    """Minimal store surface for the pure grouping/proposal tests.

    Deliberately lacks ``find_entities_by_names`` so the optional entity
    surface guard is exercised (extraction-only fallback).
    """

    def __init__(self) -> None:
        self.memories: list[dict] = []
        self._counter = 0
        self.rollup_read_calls: list[tuple[str, JsonObject]] = []

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject:
        self._counter += 1
        row = {
            "id": memory.get("id") or str(uuid4()),
            "created_at": f"2026-07-01T00:{self._counter:02d}:00Z",
            "updated_at": f"2026-07-01T00:{self._counter:02d}:00Z",
            **{key: value for key, value in memory.items() if key != "id"},
        }
        self.memories.append(row)
        return dict(row)

    def list_memories(self, *, status: str | None = None) -> list[JsonObject]:
        return [dict(row) for row in self.memories if status is None or row.get("status") == status]

    @staticmethod
    def _in_scope(
        row: JsonObject,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
    ) -> bool:
        domain = str(row.get("domain") or "unknown")
        sensitivity = str(row.get("sensitivity") or "unknown")
        return (not domains or domain in {*domains, "unknown"}) and sensitivity in sensitivity_allowed

    def list_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        self.rollup_read_calls.append(
            (
                "inputs",
                {
                    "domains": domains,
                    "sensitivity_allowed": sensitivity_allowed,
                    "excluded_candidate_kind": excluded_candidate_kind,
                    "limit": limit,
                },
            )
        )
        rows = [
            dict(row)
            for row in self.memories
            if row.get("status") in {"active", "accepted"}
            and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
            and not (
                isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == excluded_candidate_kind
            )
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))), reverse=True)
        return rows[:limit]

    def count_rollup_input_memories(
        self,
        *,
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        excluded_candidate_kind: str,
    ) -> int:
        return len(
            [
                row
                for row in self.memories
                if row.get("status") in {"active", "accepted"}
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and not (
                    isinstance(row.get("metadata_json"), dict)
                    and row["metadata_json"].get("candidate_kind") == excluded_candidate_kind
                )
            ]
        )

    def list_pending_rollup_candidates(
        self,
        *,
        rollup_digests: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        self.rollup_read_calls.append(
            (
                "pending",
                {
                    "rollup_digests": rollup_digests,
                    "domains": domains,
                    "sensitivity_allowed": sensitivity_allowed,
                    "candidate_kind": candidate_kind,
                    "limit": limit,
                },
            )
        )
        selected: list[JsonObject] = []
        for digest in sorted(set(rollup_digests)):
            matches = [
                row
                for row in self.memories
                if row.get("status") == "candidate"
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_digest") == digest
            ]
            if matches:
                selected.append(
                    dict(
                        max(
                            matches,
                            key=lambda row: (
                                str(row.get("updated_at") or ""),
                                str(row.get("created_at") or ""),
                                str(row.get("id")),
                            ),
                        )
                    )
                )
        return selected[:limit]

    def list_accepted_rollup_cards(
        self,
        *,
        rollup_keys: tuple[str, ...],
        domains: list[str] | None,
        sensitivity_allowed: list[str],
        candidate_kind: str,
        limit: int,
    ) -> list[JsonObject]:
        self.rollup_read_calls.append(
            (
                "accepted",
                {
                    "rollup_keys": rollup_keys,
                    "domains": domains,
                    "sensitivity_allowed": sensitivity_allowed,
                    "candidate_kind": candidate_kind,
                    "limit": limit,
                },
            )
        )
        selected: list[JsonObject] = []
        for rollup_key in sorted(set(rollup_keys)):
            matches = [
                row
                for row in self.memories
                if row.get("status") in {"active", "accepted"}
                and self._in_scope(row, domains=domains, sensitivity_allowed=sensitivity_allowed)
                and isinstance(row.get("metadata_json"), dict)
                and row["metadata_json"].get("candidate_kind") == candidate_kind
                and row["metadata_json"].get("rollup_key") == rollup_key
            ]
            if matches:
                active = [row for row in matches if row.get("status") == "active"]
                selected.append(
                    dict(
                        max(
                            active or matches,
                            key=lambda row: (
                                str(row.get("updated_at") or ""),
                                str(row.get("created_at") or ""),
                                str(row.get("id")),
                            ),
                        )
                    )
                )
        return selected[:limit]


GAME_SPECS = (
    ("game-1", "I played The Last of Us Part II for 30 hours", "2023-05-10", "USER"),
    ("game-2", "I played Assassin's Creed Odyssey for 70 hours", "2023-05-18", None),
    ("game-3", "I played Hollow Knight for 25 hours", "2023-06-02", None),
    ("game-4", "I played Stardew Valley for 85 hours", "2023-06-20", None),
    ("game-5", "I played Celeste for 10 hours", "2023-07-01", None),
)


def _seed_game_memories(store, *, ids: dict[str, str] | None = None, order: tuple[int, ...] | None = None) -> dict[str, JsonObject]:
    rows: dict[str, JsonObject] = {}
    indices = order if order is not None else tuple(range(len(GAME_SPECS)))
    for index in indices:
        key, text, session_date, speaker = GAME_SPECS[index]
        metadata: JsonObject = {"session_date": session_date}
        if speaker is not None:
            metadata["speaker"] = speaker
        memory: JsonObject = {
            "memory_key": f"memory.{key}",
            "value": {"text": text},
            "status": "active",
            "memory_type": "episode",
            "title": text[:80],
            "canonical_text": text,
            "summary": text[:80],
            "domain": "personal",
            "sensitivity": "internal",
            "metadata_json": metadata,
        }
        if ids is not None:
            memory["id"] = ids[key]
        rows[key] = store.create_memory(memory)
    return rows


def _rollup_candidates(store) -> list[JsonObject]:
    return [
        row
        for row in store.list_memories(status="candidate")
        if isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == ROLLUP_CANDIDATE_KIND
    ]


# -- options validation ------------------------------------------------------------


def test_invalid_rollup_options_are_rejected() -> None:
    with pytest.raises(VNextRollupValidationError):
        RollupOptions.from_metadata({"rollup_options": {"min_members": 1}})
    with pytest.raises(VNextRollupValidationError):
        RollupOptions.from_metadata({"rollup_options": {"min_members": "three"}})
    with pytest.raises(VNextRollupValidationError):
        RollupOptions.from_metadata({"rollup_options": {"max_rollups": 0}})
    with pytest.raises(VNextRollupValidationError):
        RollupOptions.from_metadata({"rollup_options": {"max_groupable_memories": 50000}})


def test_options_defaults_and_overrides_parse() -> None:
    assert RollupOptions.from_metadata(None) == RollupOptions()
    parsed = RollupOptions.from_metadata({"rollup_options": {"min_members": 4, "max_rollups": 3}})
    assert parsed.min_members == 4
    assert parsed.max_rollups == 3


# -- deterministic grouping -----------------------------------------------------------


def test_same_topic_instances_produce_one_rollup_card() -> None:
    store = FakeRollupStore()
    members = _seed_game_memories(store)
    outcome = VNextRollupService(store).propose_rollups()

    candidates = _rollup_candidates(store)
    assert len(candidates) == 1
    card = candidates[0]
    consolidation = card["metadata_json"]["consolidation"]
    assert consolidation["proposal_kind"] == "rollup"
    assert sorted(consolidation["cluster_member_ids"]) == sorted(str(row["id"]) for row in members.values())
    snapshots = consolidation["member_snapshots"]
    assert {snapshot["id"] for snapshot in snapshots} == {
        str(row["id"]) for row in members.values()
    }
    assert all(snapshot["status"] == "active" for snapshot in snapshots)
    assert all(len(snapshot["content_digest"]) == 16 for snapshot in snapshots)
    # First proposal supersedes NOTHING: members stay individually recallable.
    assert consolidation["proposed_supersede"] == []
    assert consolidation["survivor_memory_id"] is None
    assert card["status"] == "candidate"
    assert card["trust_class"] == "deterministic"
    assert card["metadata_json"]["review_required"] is True

    rollup = card["value"]["rollup"]
    assert rollup["member_count"] == 5
    assert rollup["group_kind"] == "topic"
    assert len(rollup["instances"]) == 5

    # Card content: every instance keeps its amount and date, in date order.
    text = card["canonical_text"]
    for amount in ("30 hours", "70 hours", "25 hours", "85 hours", "10 hours"):
        assert amount in text
    for day in ("2023-05-10", "2023-05-18", "2023-06-02", "2023-06-20", "2023-07-01"):
        assert day in text
    for name in ("Assassin's Creed Odyssey", "Hollow Knight", "Stardew Valley"):
        assert name in text
    assert text.index("2023-05-10") < text.index("2023-05-18") < text.index("2023-07-01")
    # The topic label carries the group's shared words.
    assert "hours" in card["title"]
    assert "played" in card["title"]

    # USER provenance carried when the member metadata has it.
    first = next(inst for inst in rollup["instances"] if inst["date"] == "2023-05-10")
    assert first["role"] == "USER"
    assert all("role" not in inst for inst in rollup["instances"] if inst["date"] != "2023-05-10")

    assert outcome.proposals[0]["candidate_state"] == "created"
    assert outcome.proposals[0]["instance_count"] == 5


def test_grouping_is_deterministic_across_insertion_order() -> None:
    fixed_ids = {key: str(uuid4()) for key, _, _, _ in GAME_SPECS}
    store_a, store_b = FakeRollupStore(), FakeRollupStore()
    _seed_game_memories(store_a, ids=fixed_ids)
    _seed_game_memories(store_b, ids=fixed_ids, order=(4, 2, 0, 3, 1))

    VNextRollupService(store_a).propose_rollups()
    VNextRollupService(store_b).propose_rollups()
    card_a = _rollup_candidates(store_a)[0]
    card_b = _rollup_candidates(store_b)[0]

    assert card_a["metadata_json"]["rollup_digest"] == card_b["metadata_json"]["rollup_digest"]
    assert card_a["metadata_json"]["rollup_key"] == card_b["metadata_json"]["rollup_key"]
    assert card_a["canonical_text"] == card_b["canonical_text"]
    assert card_a["title"] == card_b["title"]


def test_rerun_creates_no_duplicate_candidate() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    service = VNextRollupService(store)
    first = service.propose_rollups()
    second = service.propose_rollups()

    assert len(_rollup_candidates(store)) == 1
    assert second.proposals == []
    assert second.candidate_ids == first.candidate_ids
    assert any(group["state"] == "existing_candidate" for group in second.groups)


def test_same_entity_memories_group_into_entity_rollup() -> None:
    store = FakeRollupStore()
    for index, text in enumerate(
        (
            "Northwind Capital closed the seed round in March",
            "Quarterly review with Northwind Capital moved to October",
            "Northwind Capital is hiring two analysts this fall",
        )
    ):
        store.create_memory(
            {
                "memory_key": f"memory.entity-{index}",
                "value": {"text": text},
                "status": "active",
                "memory_type": "project_fact",
                "title": text[:80],
                "canonical_text": text,
                "summary": text[:80],
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {},
            }
        )
    VNextRollupService(store).propose_rollups()
    candidates = _rollup_candidates(store)
    assert len(candidates) == 1
    card = candidates[0]
    assert card["value"]["rollup"]["group_kind"] == "entity"
    assert card["metadata_json"]["rollup_key"] == "entity:northwind capital"
    assert "Northwind Capital" in card["title"]
    assert card["value"]["rollup"]["member_count"] == 3
    assert card["memory_type"] == "project_fact"


def test_near_identical_texts_are_left_to_dedup() -> None:
    store = FakeRollupStore()
    for index in range(3):
        store.create_memory(
            {
                "memory_key": f"memory.dup-{index}",
                "value": {"text": "The launch window moves to March after the review."},
                "status": "active",
                "memory_type": "semantic",
                "title": "Launch window",
                "canonical_text": "The launch window moves to March after the review.",
                "summary": "Launch window",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {},
            }
        )
    outcome = VNextRollupService(store).propose_rollups()
    assert _rollup_candidates(store) == []
    assert any("near_duplicate_group_left_to_dedup" in reason for reason in outcome.skipped)


def test_groups_covered_by_near_duplicate_clusters_are_skipped() -> None:
    store = FakeRollupStore()
    members = _seed_game_memories(store)
    member_ids = {str(row["id"]) for row in members.values()}
    outcome = VNextRollupService(store).propose_rollups(exclude_member_id_sets=[member_ids])
    assert _rollup_candidates(store) == []
    assert any("covered_by_near_duplicate_cluster" in reason for reason in outcome.skipped)


def test_candidate_and_rollup_card_rows_never_rejoin_grouping() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    service = VNextRollupService(store)
    service.propose_rollups()
    # Flip the card to active as an accepted rollup would be, then rerun:
    # the card itself must not become a member of a new group.
    card = _rollup_candidates(store)[0]
    for row in store.memories:
        if row["id"] == card["id"]:
            row["status"] = "active"
    second = service.propose_rollups()
    assert second.proposals == []
    covered = [group for group in second.groups if group["state"] == "already_covered_by_accepted"]
    assert covered and covered[0]["accepted_memory_id"] == str(card["id"])


def _seed_entity_group(store) -> list[JsonObject]:
    """Five distinct memories that all mention one entity (one entity group)."""
    texts = (
        "Northwind Capital closed the seed round in March",
        "Northwind Capital moved the quarterly review to October",
        "Northwind Capital is hiring two analysts this fall",
        "Northwind Capital signed a new lease near the harbor",
        "Northwind Capital published its annual letter in December",
    )
    rows: list[JsonObject] = []
    for index, text in enumerate(texts):
        rows.append(
            store.create_memory(
                {
                    "memory_key": f"memory.northwind-{index}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "project_fact",
                    "title": text[:80],
                    "canonical_text": text,
                    "summary": text[:80],
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {},
                }
            )
        )
    return rows


def test_rollup_membership_beyond_display_cap_is_full_and_stable() -> None:
    # Audit 2 P1 #6: a group larger than max_instances_per_card must persist its
    # FULL membership as authoritative, not the truncated display set. Before the
    # fix, the accepted card stored only the displayed subset, so the recomputed
    # full set never matched and a revision was re-proposed on every run (and
    # collided on the digest-keyed memory_key). Here 5 members with a display cap
    # of 3 exercise the same truncation the 40/500 defaults hit at scale.
    store = FakeRollupStore()
    members = _seed_entity_group(store)
    options = RollupOptions(max_instances_per_card=3)
    service = VNextRollupService(store)
    service.propose_rollups(options=options)

    candidates = _rollup_candidates(store)
    assert len(candidates) == 1
    card = candidates[0]
    consolidation = card["metadata_json"]["consolidation"]
    assert len(consolidation["cluster_member_ids"]) == 5
    assert sorted(consolidation["cluster_member_ids"]) == sorted(str(row["id"]) for row in members)
    assert {snap["id"] for snap in consolidation["member_snapshots"]} == {str(row["id"]) for row in members}
    assert card["value"]["rollup"]["member_count"] == 5
    # Display instances stay bounded to the per-card cap.
    assert len(card["value"]["rollup"]["instances"]) == 3
    assert card["value"]["rollup"]["displayed_instance_count"] == 3
    assert card["value"]["rollup"]["instances_truncated"] is True
    assert "5 instances total (showing 3)" in card["title"]
    assert "5 instances in the authoritative group; showing 3" in card["canonical_text"]
    assert "3 instances in total" not in card["canonical_text"]

    # Accept it (a rollup card flips to active) and rerun: a stable group larger
    # than the display cap must NOT re-propose a revision.
    for row in store.memories:
        if row["id"] == card["id"]:
            row["status"] = "active"
    second = service.propose_rollups(options=options)
    assert second.proposals == []
    covered = [group for group in second.groups if group["state"] == "already_covered_by_accepted"]
    assert covered and covered[0]["accepted_memory_id"] == str(card["id"])


def test_create_candidate_memories_false_reports_without_writes() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    outcome = VNextRollupService(store).propose_rollups(create_candidate_memories=False)
    assert _rollup_candidates(store) == []
    assert len(outcome.proposals) == 1
    assert outcome.proposals[0]["candidate_memory_id"] is None


def test_rollups_partition_same_topic_members_by_exact_project_scope() -> None:
    store = FakeRollupStore()
    for project_id in ("project-a", "project-b"):
        for index, (_key, text, session_date, _speaker) in enumerate(GAME_SPECS[:3]):
            store.create_memory(
                {
                    "memory_key": f"memory.{project_id}.{index}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "episode",
                    "title": text,
                    "canonical_text": text,
                    "summary": text,
                    "domain": "project",
                    "sensitivity": "internal",
                    "project_id": project_id,
                    "metadata_json": {
                        "session_date": session_date,
                        "project_scope": [project_id],
                    },
                }
            )

    outcome = VNextRollupService(store).propose_rollups(
        options=RollupOptions(min_members=3)
    )

    assert len(outcome.proposals) == 2
    candidates = _rollup_candidates(store)
    assert len(candidates) == 2
    assert {tuple(row["metadata_json"]["project_scope"]) for row in candidates} == {
        ("project-a",),
        ("project-b",),
    }
    assert len({row["metadata_json"]["rollup_key"] for row in candidates}) == 2
    for candidate in candidates:
        assert len(candidate["metadata_json"]["consolidation"]["cluster_member_ids"]) == 3


def test_max_rollups_bound_is_reported() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    for index, text in enumerate(
        (
            "Ran 5 km along the river on Tuesday",
            "Ran 8 km before work with the club",
            "Ran 12 km on the trail loop this weekend",
        )
    ):
        store.create_memory(
            {
                "memory_key": f"memory.run-{index}",
                "value": {"text": text},
                "status": "active",
                "memory_type": "episode",
                "title": text[:80],
                "canonical_text": text,
                "summary": text[:80],
                "domain": "personal",
                "sensitivity": "internal",
                "metadata_json": {},
            }
        )
    outcome = VNextRollupService(store).propose_rollups(
        options=RollupOptions.from_metadata({"rollup_options": {"max_rollups": 1}})
    )
    assert len(outcome.proposals) == 1
    assert any("rollup_bound" in reason for reason in outcome.skipped)


def test_rollup_reads_apply_deterministic_database_bounds_before_grouping() -> None:
    class BoundedOnlyStore(FakeRollupStore):
        def list_memories(self, *, status: str | None = None) -> list[JsonObject]:
            raise AssertionError("the roll-up service must not use the unbounded generic read")

    store = BoundedOnlyStore()
    members = _seed_game_memories(store)
    options = RollupOptions(max_groupable_memories=3)

    outcome = VNextRollupService(store).propose_rollups(
        domains=["personal"],
        sensitivity_allowed=["internal"],
        options=options,
    )

    assert outcome.bounded is True
    assert outcome.groupable_count == 3
    assert outcome.groupable_total_count == 5
    assert outcome.groupable_total_exact is True
    assert any("of 5 in scope" in reason for reason in outcome.skipped)
    bounded_card = next(
        row
        for row in store.memories
        if isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == ROLLUP_CANDIDATE_KIND
    )
    assert bounded_card["value"]["rollup"]["grouping_input_truncated"] is True
    assert bounded_card["value"]["rollup"]["grouping_input_total"] == 5
    assert "3 matched instances in a truncated grouping input" in bounded_card["canonical_text"]
    assert "instances in total" not in bounded_card["canonical_text"]
    expected_member_ids = {
        str(members[key]["id"])
        for key in ("game-3", "game-4", "game-5")
    }
    assert set(outcome.proposals[0]["member_ids"]) == expected_member_ids

    input_call = store.rollup_read_calls[0]
    assert input_call == (
        "inputs",
        {
            "domains": ["personal"],
            "sensitivity_allowed": ["internal"],
            "excluded_candidate_kind": ROLLUP_CANDIDATE_KIND,
            # One sentinel row proves that the configured cap was reached.
            "limit": 4,
        },
    )
    pending_call = next(call for call in store.rollup_read_calls if call[0] == "pending")
    accepted_call = next(call for call in store.rollup_read_calls if call[0] == "accepted")
    assert pending_call[1]["limit"] == len(pending_call[1]["rollup_digests"]) == 1
    assert accepted_call[1]["limit"] == len(accepted_call[1]["rollup_keys"]) == 1
    assert pending_call[1]["domains"] == accepted_call[1]["domains"] == ["personal"]
    assert (
        pending_call[1]["sensitivity_allowed"]
        == accepted_call[1]["sensitivity_allowed"]
        == ["internal"]
    )


# -- label hygiene & group-utility gate ------------------------------------------------


def _seed_texts(store, specs: tuple[tuple[str, str | None], ...], *, prefix: str = "memory.gate") -> None:
    for index, (text, session_date) in enumerate(specs):
        metadata: JsonObject = {}
        if session_date is not None:
            metadata["session_date"] = session_date
        store.create_memory(
            {
                "memory_key": f"{prefix}-{index}",
                "value": {"text": text},
                "status": "active",
                "memory_type": "episode",
                "title": text[:80],
                "canonical_text": text,
                "summary": text[:80],
                "domain": "personal",
                "sensitivity": "internal",
                "metadata_json": metadata,
            }
        )


JUNK_TRIO = (
    ("I'm excited about the violet harbor", None),
    ("I'm tired after the granite summit", None),
    ("I'm curious about the copper lantern", None),
)


def test_pronoun_contraction_labels_never_head_cards() -> None:
    """Members sharing only \"I'm\" produce no card: the label has no
    content words, so the group is dropped instead of proposed."""
    store = FakeRollupStore()
    _seed_texts(store, JUNK_TRIO)
    outcome = VNextRollupService(store).propose_rollups()
    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    gate_lines = [reason for reason in outcome.skipped if reason.startswith("quality_gate_dropped")]
    assert gate_lines and "label_without_content_words" in gate_lines[0]
    assert outcome.quality_gate["dropped_by_reason"]["label_without_content_words"] >= 1


def test_frequency_generic_anchor_is_not_a_topic() -> None:
    """Store-measured generic anchors: a stem dispersed across most
    sessions ('need') cannot anchor a topic, while a concentrated topic
    ('hiked') still can — no hardcoded vocabulary involved."""
    store = FakeRollupStore()
    filler = tuple(
        (f"need marker{index:02d}a marker{index:02d}b", f"2023-03-{(index % 12) + 1:02d}")
        for index in range(36)
    )
    hikes = tuple(
        (f"Hiked {distance} km along the {name} trail", f"2023-04-{day:02d}")
        for distance, name, day in (
            (5, "juniper", 2),
            (8, "basalt", 9),
            (11, "willow", 16),
            (13, "mesa", 23),
        )
    )
    _seed_texts(store, filler + hikes)
    outcome = VNextRollupService(store).propose_rollups()

    assert outcome.quality_gate["anchor_stats_enabled"] is True
    assert len(outcome.proposals) == 1
    proposal = outcome.proposals[0]
    assert "hiked" in proposal["label"].casefold()
    assert proposal["aggregation"]["distinct_values"] >= 4
    assert proposal["aggregation"]["distinct_sessions"] >= 4
    # The plumbing anchor was dropped BEFORE claiming members, in the
    # aggregate skip line, not proposed and not one-line-per-anchor.
    assert outcome.quality_gate["dropped_by_reason"]["anchor_generic_for_store"] >= 1
    assert not any("need" == proposal["label"].casefold() for proposal in outcome.proposals)


def test_broken_subspan_entity_label_is_repaired() -> None:
    """Extraction truncates 'The Last of Us Part II' to 'Us Part II' (the
    lowercase connector splits the span); the card label is repaired to the
    dominant full span while the grouping key stays stable."""
    store = FakeRollupStore()
    _seed_texts(
        store,
        (
            ("I finished The Last of Us Part II after 30 hours", "2023-05-10"),
            ("Replaying The Last of Us Part II took 12 hours", "2023-05-18"),
            ("The Last of Us Part II photo mode ate 3 hours", "2023-06-02"),
            ("Speedran The Last of Us Part II in 9 hours", "2023-06-11"),
        ),
    )
    outcome = VNextRollupService(store).propose_rollups()
    assert len(outcome.proposals) == 1
    proposal = outcome.proposals[0]
    assert proposal["rollup_key"] == "entity:us part ii"  # grouping unchanged
    assert proposal["label"] == "The Last of Us Part II"  # display repaired
    card = _rollup_candidates(store)[0]
    assert "The Last of Us Part II" in card["title"]


def test_groups_without_aggregation_signal_are_dropped() -> None:
    """Three same-entity mentions with no amounts, dates, or session spread
    aggregate nothing; the group is dropped, not proposed."""
    store = FakeRollupStore()
    _seed_texts(
        store,
        (
            ("Quartz Peak has a nice lodge", None),
            ("The lodge at Quartz Peak seems busy", None),
            ("Thinking about the Quartz Peak lodge", None),
        ),
    )
    outcome = VNextRollupService(store).propose_rollups()
    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.quality_gate["dropped_by_reason"]["no_aggregation_signal"] >= 1


def test_ranking_prefers_higher_aggregation_utility() -> None:
    """max_rollups keeps the strongest aggregation, not the first-formed
    group: five games across five sessions beat three ferry receipts in one."""
    store = FakeRollupStore()
    _seed_game_memories(store)
    _seed_texts(
        store,
        (
            ("Paid $40 for the ferry pass", None),
            ("Paid $25 for the ferry pass upgrade", None),
            ("Paid $40 ferry pass renewal fee", None),
        ),
    )
    outcome = VNextRollupService(store).propose_rollups(
        options=RollupOptions.from_metadata({"rollup_options": {"max_rollups": 1}})
    )
    assert len(outcome.proposals) == 1
    label = outcome.proposals[0]["label"].casefold()
    assert "played" in label or "hours" in label
    assert any("rollup_bound" in reason for reason in outcome.skipped)


def test_title_reads_like_a_topic_with_dominant_unit() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    outcome = VNextRollupService(store).propose_rollups()
    card = _rollup_candidates(store)[0]
    assert "amounts in hours" in card["title"]
    aggregation = outcome.proposals[0]["aggregation"]
    assert aggregation["distinct_values"] == 5
    assert aggregation["distinct_sessions"] == 5
    assert aggregation["score"] > 0
    assert aggregation["label_coherence"] == 1.0


# -- label heads: closed-class words and bare verbs --------------------------------


LABEL_HEAD_CASES = (
    # Closed-class heads are junk no matter how strong the value signal:
    # the verifier's residue cards ("even — 15 instances, amounts in $")
    # all carried amounts.
    ("even", 5, "label_head_closed_class"),
    ("still", 5, "label_head_closed_class"),
    ("right", 5, "label_head_closed_class"),
    ("towards", 5, "label_head_closed_class"),
    ("typically", 5, "label_head_closed_class"),
    # Light-verb heads are junk without a noun, amounts or not: light
    # verbs attract incidental quantities ("add ... for 10 minutes").
    ("add", 5, "label_head_light_verb"),
    ("used", 5, "label_head_light_verb"),
    ("incorporate", 5, "label_head_light_verb"),
    # Other bare verb heads are acceptable exactly when the group
    # aggregates values ("bought $120/$450" aggregates purchases; a bare
    # verb with only session spread is plumbing).
    ("bought", 3, None),
    ("bought", 0, "label_bare_verb_without_values"),
    ("hiked", 2, None),
    ("hiked", 0, "label_bare_verb_without_values"),
    # A verb the deterministic machinery cannot mark (irregular past,
    # not curated) stays content-bearing.
    ("flew", 0, None),
    # A noun anywhere in the label carries the topic.
    ("miles ran", 0, None),
    ("decided meridian", 0, None),
    ("finished reading", 0, None),  # gerunds act as nouns
    ("hours played", 0, None),
    # Plain noun labels are never head-junk.
    ("workshops", 0, None),
    ("model kits", 0, None),
)


@pytest.mark.parametrize(
    "label,amounts,expected", LABEL_HEAD_CASES, ids=[f"{c[0]}-{c[1]}" for c in LABEL_HEAD_CASES]
)
def test_label_head_junk_rules(label, amounts, expected) -> None:
    """Table-driven head hygiene: a card label's head token must be
    content-bearing (no closed-class words, no light verbs without a
    noun, no bare verbs without a value-based aggregation signal)."""
    stats = _CorpusStats({})  # below the stats floor: structural rules only
    assert _label_junk_reason(label, stats, label_amount_count=amounts) == expected


def test_closed_class_head_never_labels_card_even_with_amounts() -> None:
    """Members sharing only a preposition ('towards') with dollar amounts
    in every text: real aggregation signal, but a closed-class head can
    never name a topic — the group is dropped, not proposed."""
    store = FakeRollupStore()
    _seed_texts(
        store,
        (
            ("Saved $40 towards a harbor kayak", "2026-03-02"),
            ("Chipped $25 towards the granite lodge", "2026-03-09"),
            ("Banked $60 towards the copper lantern", "2026-03-16"),
        ),
    )
    outcome = VNextRollupService(store).propose_rollups()
    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.quality_gate["dropped_by_reason"]["label_head_closed_class"] >= 1


def test_light_verb_head_never_labels_card_even_with_amounts() -> None:
    """Members sharing only a light verb ('used') with amounts: light
    verbs attract incidental quantities, so the value signal does not
    rescue the head — the group is dropped."""
    store = FakeRollupStore()
    _seed_texts(
        store,
        (
            ("Used the harbor pass, $12 fare", "2026-03-02"),
            ("Used the granite entrance, $15 fee", "2026-03-09"),
            ("Used the copper gate, $18 toll", "2026-03-16"),
        ),
    )
    outcome = VNextRollupService(store).propose_rollups()
    assert outcome.proposals == []
    assert _rollup_candidates(store) == []
    assert outcome.quality_gate["dropped_by_reason"]["label_head_light_verb"] >= 1


def test_bare_transaction_verb_needs_value_signal() -> None:
    """Both sides of the bare-verb rule on real stores: 'bought' with
    only session spread is plumbing (dropped); 'bought' aggregating
    distinct prices is a purchases card (proposed)."""
    plain = FakeRollupStore()
    _seed_texts(
        plain,
        (
            ("Bought a harbor kayak pass", "2026-03-02"),
            ("Bought the granite lodge voucher", "2026-03-09"),
            ("Bought a copper lantern kit", "2026-03-16"),
        ),
    )
    without_values = VNextRollupService(plain).propose_rollups()
    assert without_values.proposals == []
    assert (
        without_values.quality_gate["dropped_by_reason"]["label_bare_verb_without_values"] >= 1
    )

    priced = FakeRollupStore()
    _seed_texts(
        priced,
        (
            ("Bought a harbor kayak pass for $40", "2026-03-02"),
            ("Bought the granite lodge voucher for $25", "2026-03-09"),
            ("Bought a copper lantern kit for $60", "2026-03-16"),
        ),
    )
    with_values = VNextRollupService(priced).propose_rollups()
    assert len(with_values.proposals) == 1
    proposal = with_values.proposals[0]
    assert "bought" in proposal["label"].casefold()
    assert proposal["aggregation"]["distinct_amounts"] >= 2


# -- instance-line hygiene ---------------------------------------------------------


def test_instance_labels_get_subspan_repair() -> None:
    """The card-label subspan repair also applies inside instance lines:
    'Us Part II' renders as 'The Last of Us Part II' next to its value
    and date."""
    store = FakeRollupStore()
    _seed_game_memories(store)
    VNextRollupService(store).propose_rollups()
    card = _rollup_candidates(store)[0]
    labels = [instance["label"] for instance in card["value"]["rollup"]["instances"]]
    assert "The Last of Us Part II" in labels
    assert "Us Part II" not in labels
    assert "The Last of Us Part II (30 hours; 2023-05-10" in card["canonical_text"]


def test_fragment_instance_label_filters_to_neutral_noun() -> None:
    """An instance whose extracted label is a pronoun/closed-class
    fragment ('Since I'm') keeps its value+date line under a neutral
    content-bearing label instead."""
    row: JsonObject = {
        "id": "frag-1",
        "title": "Since I'm in Denver, the pottery workshop cost $40",
        "canonical_text": "Since I'm in Denver, the pottery workshop cost $40",
        "metadata_json": {"session_date": "2026-03-02"},
    }
    label = _instance_label(row)
    assert label
    lowered = label.casefold()
    assert not lowered.startswith("since")
    assert "i'm" not in lowered
    record = _instance_record(row)
    assert record["label"] == label
    assert record["amounts"] == ["$40"]  # the value line survives the filter
    assert record["date"] == "2026-03-02"


def test_pronoun_contraction_instance_label_is_filtered() -> None:
    """A bare pronoun-contraction candidate ('I'm') never labels an
    instance line."""
    row: JsonObject = {
        "id": "frag-2",
        "title": "I'm logging 3 hours at the granite summit",
        "canonical_text": "I'm logging 3 hours at the granite summit",
        "metadata_json": {},
    }
    label = _instance_label(row)
    assert label
    assert not label.casefold().startswith(("i'", "i’"))


PRODUCT_STORE_FIXTURES = (
    (
        "workouts",
        (
            ("Ran 3 miles around Lakeview loop", "2026-03-02"),
            ("Ran 5 miles along the river path", "2026-03-09"),
            ("Ran 4 miles at the track", "2026-03-16"),
            ("Ran 6 miles on the canyon trail", "2026-03-23"),
        )
        + JUNK_TRIO,
        ("mile", "ran"),
    ),
    (
        "project_decisions",
        (
            ("Decided to adopt the Meridian rollout plan for launch", "2026-01-05"),
            ("Decided to delay the Meridian rollout by two weeks", "2026-01-12"),
            ("Decided the Meridian rollout needs a canary stage", "2026-01-19"),
        ),
        ("meridian", "rollout", "decided"),
    ),
    (
        "shopping",
        (
            ("Bought a Herman Miller chair for $450", "2026-02-01"),
            ("Bought new running shoes for $120", "2026-02-08"),
            ("Bought a standing desk mat for $60", "2026-02-15"),
        ),
        ("bought",),
    ),
    (
        "reading_list",
        (
            ("Finished reading Project Hail Mary in March", "2026-03-05"),
            ("Finished reading The Martian in April", "2026-04-02"),
            ("Finished reading Recursion in May", "2026-05-07"),
        ),
        ("reading", "finished"),
    ),
    (
        "travel",
        (
            ("Flew to Lisbon for the spring conference", "2026-04-10"),
            ("Flew to Oslo for the design retreat", "2026-05-20"),
            ("Flew to Kyoto for the autumn workshop", "2026-10-15"),
        ),
        ("flew",),
    ),
)


@pytest.mark.parametrize("name,specs,expected_label_words", PRODUCT_STORE_FIXTURES, ids=[f[0] for f in PRODUCT_STORE_FIXTURES])
def test_product_shaped_stores_produce_topical_cards(name, specs, expected_label_words) -> None:
    """Non-benchmark product stores: every proposed card passes the utility
    gate and its label reads like a topic (contains the fixture's topical
    words, never a pronoun contraction or function-word label)."""
    store = FakeRollupStore()
    _seed_texts(store, specs, prefix=f"memory.{name}")
    outcome = VNextRollupService(store).propose_rollups()

    assert outcome.proposals, f"{name}: expected at least one topical card"
    labels = [proposal["label"] for proposal in outcome.proposals]
    assert any(
        any(word in label.casefold() for word in expected_label_words) for label in labels
    ), f"{name}: no topical label in {labels}"
    for proposal in outcome.proposals:
        label = proposal["label"].casefold()
        # Structural hygiene: no pronoun-contraction or single-letter labels.
        assert not label.startswith(("i'", "you'", "it'", "that'", "there'"))
        assert len(label) >= 2
        aggregation = proposal["aggregation"]
        assert (
            aggregation["distinct_values"] >= 2 or aggregation["distinct_sessions"] >= 3
        ), f"{name}: proposal without aggregation signal: {proposal}"
        assert aggregation["label_coherence"] >= 0.5


# -- model refinement (existing provider seam, deterministic fallback) ---------------


class StubRoute:
    approval_required = False
    route_mode = "cloud_allowed"
    policy_mode = "test"

    def to_record(self) -> JsonObject:
        return {"route_mode": self.route_mode}


class StubSummaryProvider:
    provider = "stub_model"
    model = "stub-rollup-1"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def chat(self, *, prompt: str, temperature: float) -> str:
        self.prompts.append(prompt)
        return self.response


def test_model_backed_summary_is_grounded_and_disclosed() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    stub = StubSummaryProvider(
        '{"summary": "Played Hollow Knight, Stardew Valley, Celeste and more, logging 30 hours to 85 hours each."}'
    )
    VNextRollupService(store, merge_provider=stub).propose_rollups(
        generation_mode="model_backed", route=StubRoute()
    )
    card = _rollup_candidates(store)[0]
    consolidation = card["metadata_json"]["consolidation"]
    assert "Summary: Played Hollow Knight" in card["canonical_text"]
    # The deterministic instance list is still fully present.
    for amount in ("30 hours", "70 hours", "25 hours", "85 hours", "10 hours"):
        assert amount in card["canonical_text"]
    assert card["trust_class"] == "llm_single_source"
    assert consolidation["model_provenance"]["provider"] == "stub_model"
    assert consolidation["model_provenance"]["prompt_hash"].startswith("sha256:")
    assert consolidation["merge_refusal"] is None
    assert stub.prompts and "[UNTRUSTED_CONTEXT_JSON]" in stub.prompts[0]


def test_model_backed_summary_provider_failure_uses_static_refusal_reason() -> None:
    sentinel = "UNIQUE_ROLLUP_MODEL_EXCEPTION_SENTINEL"

    class FailingSummaryProvider(StubSummaryProvider):
        def chat(self, *, prompt: str, temperature: float) -> str:
            raise VNextModelIntelligenceError(sentinel)

    store = FakeRollupStore()
    _seed_game_memories(store)
    VNextRollupService(store, merge_provider=FailingSummaryProvider("unused")).propose_rollups(
        generation_mode="model_backed", route=StubRoute()
    )

    cards = _rollup_candidates(store)
    assert cards
    assert all(
        card["metadata_json"]["consolidation"]["merge_refusal"] == "provider_error"
        for card in cards
    )
    assert sentinel not in str(cards)


def test_semantic_rollup_failures_use_static_skip_reasons() -> None:
    presence_sentinel = "UNIQUE_ROLLUP_PRESENCE_EXCEPTION_SENTINEL"
    provider_sentinel = "UNIQUE_ROLLUP_EMBEDDING_EXCEPTION_SENTINEL"
    rows = [
        {
            "id": f"memory-{index}",
            "title": f"Unrelated {index}",
            "canonical_text": f"Distinct semantic fixture {index}",
            "created_at": f"2026-07-01T00:0{index}:00Z",
        }
        for index in range(3)
    ]

    class PresenceFailureStore(FakeRollupStore):
        def list_memory_ids_with_embeddings(self, ids: list[str]) -> set[str]:
            raise RuntimeError(presence_sentinel)

    class ProviderFailureStore(FakeRollupStore):
        def list_memory_ids_with_embeddings(self, ids: list[str]) -> set[str]:
            return set(ids)

    class Provider:
        provider = "test"
        model = "test"

        def embed_batch(self, texts: object) -> list[list[float]]:
            raise VNextEmbeddingProviderError(provider_sentinel)

    presence_service = VNextRollupService(PresenceFailureStore(), embedding_provider=Provider())
    _clusters, presence_record = presence_service._semantic_clusters(
        rows, options=RollupOptions(), domains=None, sensitivity_allowed=None
    )
    provider_service = VNextRollupService(ProviderFailureStore(), embedding_provider=Provider())
    _clusters, provider_record = provider_service._semantic_clusters(
        rows, options=RollupOptions(), domains=None, sensitivity_allowed=None
    )

    assert presence_record["skipped"] == ["embedding_presence_read_failed"]
    assert provider_record["skipped"] == ["embedding_provider_failed"]
    assert presence_sentinel not in str(presence_record)
    assert provider_sentinel not in str(provider_record)


def test_model_summary_is_not_allowed_to_describe_a_truncated_group() -> None:
    store = FakeRollupStore()
    _seed_entity_group(store)
    stub = StubSummaryProvider('{"summary": "All five investments were successful."}')

    outcome = VNextRollupService(store, merge_provider=stub).propose_rollups(
        options=RollupOptions(max_instances_per_card=3),
        generation_mode="model_backed",
        route=StubRoute(),
    )

    assert stub.prompts == []
    assert outcome.proposals[0]["instance_count"] == 5
    assert outcome.proposals[0]["displayed_instance_count"] == 3
    assert outcome.proposals[0]["instances_truncated"] is True
    assert outcome.proposals[0]["merge_refusal"] == "model_summary_skipped_for_truncated_instances"
    card = _rollup_candidates(store)[0]
    assert "Summary:" not in card["canonical_text"]
    assert card["trust_class"] == "deterministic"


def test_ungrounded_model_summary_falls_back_to_deterministic_card() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    stub = StubSummaryProvider('{"summary": "Definitely a champion speedrunner sponsored by Redwood Beverages."}')
    VNextRollupService(store, merge_provider=stub).propose_rollups(
        generation_mode="model_backed", route=StubRoute()
    )
    card = _rollup_candidates(store)[0]
    assert "Summary:" not in card["canonical_text"]
    assert card["trust_class"] == "deterministic"
    refusal = card["metadata_json"]["consolidation"]["merge_refusal"]
    assert refusal is not None and refusal.startswith("ungrounded_model_output")


def test_deterministic_mode_never_calls_a_model() -> None:
    store = FakeRollupStore()
    _seed_game_memories(store)
    stub = StubSummaryProvider('{"summary": "should never be requested"}')
    VNextRollupService(store, merge_provider=stub).propose_rollups(generation_mode="deterministic")
    assert stub.prompts == []
    card = _rollup_candidates(store)[0]
    assert card["metadata_json"]["consolidation"]["model_provenance"] is None


# -- live sqlite: review gate, acceptance, recall, revisions ---------------------------


def _live_store() -> tuple[sqlite3.Connection, SQLiteVNextStore]:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "rollups@example.com", "Rollup User")
    return conn, SQLiteVNextStore(conn, user_id)


def _seed_live_game_memories(store: SQLiteVNextStore) -> dict[str, JsonObject]:
    return _seed_game_memories(store)


def test_sqlite_rollup_reads_are_exact_deduplicated_and_bounded() -> None:
    conn, store = _live_store()

    def create_row(name: str, *, status: str, metadata: JsonObject) -> JsonObject:
        return store.create_memory(
            {
                "memory_key": f"memory.{name}",
                "value": {"text": name},
                "status": status,
                "memory_type": "semantic",
                "title": name,
                "canonical_text": name,
                "summary": name,
                "domain": "personal",
                "sensitivity": "internal",
                "metadata_json": metadata,
            }
        )

    ordinary_active = create_row("ordinary-active", status="active", metadata={})
    ordinary_accepted = create_row("ordinary-accepted", status="accepted", metadata={})
    create_row(
        "excluded-card",
        status="active",
        metadata={"candidate_kind": ROLLUP_CANDIDATE_KIND, "rollup_key": "topic:excluded"},
    )

    inputs = store.list_rollup_input_memories(
        domains=["personal"],
        sensitivity_allowed=["internal"],
        excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=1,
    )
    expected_newest = max(
        (ordinary_active, ordinary_accepted),
        key=lambda row: (str(row["created_at"]), str(row["id"])),
    )
    assert [str(row["id"]) for row in inputs] == [str(expected_newest["id"])]
    assert store.count_rollup_input_memories(
        domains=["personal"],
        sensitivity_allowed=["internal"],
        excluded_candidate_kind=ROLLUP_CANDIDATE_KIND,
    ) == 2
    assert store.count_memories(
        status="active",
        domains=["personal"],
        sensitivity_allowed=["internal"],
    ) == 2

    older_pending = create_row(
        "pending-old",
        status="candidate",
        metadata={
            "candidate_kind": ROLLUP_CANDIDATE_KIND,
            "rollup_digest": "digest-exact",
        },
    )
    newer_pending = create_row(
        "pending-new",
        status="candidate",
        metadata={
            "candidate_kind": ROLLUP_CANDIDATE_KIND,
            "rollup_digest": "digest-exact",
        },
    )
    create_row(
        "pending-unrelated",
        status="candidate",
        metadata={
            "candidate_kind": ROLLUP_CANDIDATE_KIND,
            "rollup_digest": "digest-unrelated",
        },
    )
    pending = store.list_pending_rollup_candidates(
        rollup_digests=("digest-exact", "digest-exact"),
        domains=["personal"],
        sensitivity_allowed=["internal"],
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=99,
    )
    expected_pending = max(
        (older_pending, newer_pending),
        key=lambda row: (str(row["updated_at"]), str(row["created_at"]), str(row["id"])),
    )
    assert [str(row["id"]) for row in pending] == [str(expected_pending["id"])]

    active_card = create_row(
        "card-active",
        status="active",
        metadata={
            "candidate_kind": ROLLUP_CANDIDATE_KIND,
            "rollup_key": "topic:exact",
        },
    )
    create_row(
        "card-accepted-newer",
        status="accepted",
        metadata={
            "candidate_kind": ROLLUP_CANDIDATE_KIND,
            "rollup_key": "topic:exact",
        },
    )
    cards = store.list_accepted_rollup_cards(
        rollup_keys=("topic:exact", "topic:exact"),
        domains=["personal"],
        sensitivity_allowed=["internal"],
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=99,
    )
    assert [str(row["id"]) for row in cards] == [str(active_card["id"])]
    conn.close()


def _compile_pack(store: SQLiteVNextStore, query: str) -> JsonObject:
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService

    return VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query=query, max_items=8, actor_type="system")
    )


AGGREGATION_QUERY = "How many hours did I play in total?"


def test_unaccepted_rollup_candidate_never_appears_in_packs(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()
    _seed_live_game_memories(store)
    VNextRollupService(store).propose_rollups()
    candidate_id = str(_rollup_candidates(store)[0]["id"])

    pack = _compile_pack(store, AGGREGATION_QUERY)
    pack_ids = {str(memory.get("id")) for memory in pack.get("relevant_memories") or []}
    assert candidate_id not in pack_ids
    conn.close()


@pytest.mark.parametrize("mutation", ("corrected", "forgotten", "redacted", "superseded"))
def test_member_mutation_invalidates_pending_rollup_and_prevents_publication(
    monkeypatch,
    mutation: str,
) -> None:
    """Every destructive/content mutation closes the pending review lane."""
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()
    members = _seed_live_game_memories(store)
    VNextRollupService(store).propose_rollups()
    candidate_id = str(_rollup_candidates(store)[0]["id"])
    target_id = str(members["game-5"]["id"])
    commit = VNextMemoryCommitService(store)

    if mutation == "corrected":
        commit.correct(
            identity=None,
            memory_id=target_id,
            canonical_text="I played Celeste for 12 hours on 2023-07-01.",
            reason="Correct the recorded duration.",
        )
    elif mutation == "forgotten":
        commit.forget(identity=None, memory_id=target_id, reason="Forget this game session.")
    elif mutation == "redacted":
        commit.forget(identity=None, memory_id=target_id, reason="Redact this game session.")
        store.redact_memory_content(memory_id=target_id, actor_type="user")
    else:
        replacement = store.create_memory(
            {
                "memory_key": "memory.game-5-replacement",
                "value": {"text": "I played Celeste for 12 hours"},
                "status": "active",
                "memory_type": "episode",
                "title": "Corrected Celeste duration",
                "canonical_text": "I played Celeste for 12 hours",
                "domain": "personal",
                "sensitivity": "internal",
            }
        )
        commit.undo(
            identity=None,
            memory_id=target_id,
            superseded_by_memory_id=str(replacement["id"]),
            reason="Superseded by the corrected game session.",
        )

    invalidated = store.get_memory(candidate_id)
    assert invalidated["status"] == "stale"
    assert invalidated["promotion_eligibility"] == "not_promotable"
    assert invalidated["metadata_json"]["review_required"] is False
    invalidation = invalidated["metadata_json"]["consolidation"]["invalidated"]
    assert invalidation["member_id"] == target_id
    assert any(
        revision["action"] == "memory_consolidation_candidate_invalidated"
        for revision in store.list_revisions(candidate_id)
    )
    assert any(
        event["event_type"] == "agent.memory_consolidation_candidate_invalidated"
        for event in store.list_events(target_type="memory", target_id=candidate_id)
    )
    with pytest.raises(VNextMemoryCommitValidationError, match="candidate is stale"):
        commit.accept_consolidation_candidate(candidate_id, reason="Attempt stale acceptance.")

    # Acceptance performed no derived publication or unrelated retirement.
    assert store.get_memory(candidate_id)["status"] == "stale"
    for key, row in members.items():
        current = store.get_memory(str(row["id"]))
        if key == "game-5" and mutation == "redacted":
            assert current is None
            continue
        assert current is not None
        if key != "game-5" or mutation == "corrected":
            assert current["status"] == "active"
        assert str(current.get("superseded_by") or "") != candidate_id
    conn.close()


def test_accepted_rollup_card_wins_recall_for_aggregation_query(monkeypatch) -> None:
    """The full product loop on live SQLite: propose -> accept -> recall."""
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()
    members = _seed_live_game_memories(store)
    VNextRollupService(store).propose_rollups()
    candidate_id = str(_rollup_candidates(store)[0]["id"])

    result = VNextMemoryCommitService(store).accept_consolidation_candidate(
        candidate_id, reason="Reviewed the games roll-up."
    )
    assert result["status"] == "accepted"
    assert result["proposal_kind"] == "rollup"
    # Superseding nothing is the roll-up contract.
    assert result["superseded_member_ids"] == []
    accepted = store.get_memory(candidate_id)
    assert accepted["status"] == "active"
    assert accepted["supersedes"] is None
    for row in members.values():
        member = store.get_memory(str(row["id"]))
        assert member["status"] == "active"
        assert member["superseded_by"] is None

    pack = _compile_pack(store, AGGREGATION_QUERY)
    ranked_ids = [str(memory.get("id")) for memory in pack.get("relevant_memories") or []]
    assert candidate_id in ranked_ids
    # The card outranks every individual member: one canonical hit instead
    # of needing all five instances to win pack slots.
    member_ids = {str(row["id"]) for row in members.values()}
    card_rank = ranked_ids.index(candidate_id)
    for member_id in member_ids:
        if member_id in ranked_ids:
            assert card_rank < ranked_ids.index(member_id)

    # Members remain individually recallable by their own content.
    member_pack = _compile_pack(store, "How long did I play Hollow Knight?")
    member_pack_ids = {str(memory.get("id")) for memory in member_pack.get("relevant_memories") or []}
    assert str(members["game-3"]["id"]) in member_pack_ids
    conn.close()


def test_new_member_triggers_revision_that_retires_only_the_old_card(monkeypatch) -> None:
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()
    members = _seed_live_game_memories(store)
    service = VNextRollupService(store)
    service.propose_rollups()
    first_card_id = str(_rollup_candidates(store)[0]["id"])
    commit = VNextMemoryCommitService(store)
    commit.accept_consolidation_candidate(first_card_id, reason="Initial games roll-up.")

    # A new same-topic memory arrives (on the commit path this costs O(1);
    # only the next scheduled pass reacts).
    new_member = store.create_memory(
        {
            "memory_key": "memory.game-6",
            "value": {"text": "I played Hades for 40 hours"},
            "status": "active",
            "memory_type": "episode",
            "title": "I played Hades for 40 hours",
            "canonical_text": "I played Hades for 40 hours",
            "summary": "I played Hades for 40 hours",
            "domain": "personal",
            "sensitivity": "internal",
            "metadata_json": {"session_date": "2023-07-15"},
        }
    )

    outcome = service.propose_rollups()
    assert len(outcome.proposals) == 1
    revision = outcome.proposals[0]
    assert revision["candidate_state"] == "revision_proposed"
    assert revision["revises_memory_id"] == first_card_id
    assert revision["proposed_supersede"] == [first_card_id]
    revision_row = store.get_memory(str(revision["candidate_memory_id"]))
    assert revision_row["status"] == "candidate"  # not silent mutation
    assert str(new_member["id"]) in revision_row["metadata_json"]["consolidation"]["cluster_member_ids"]
    revision_consolidation = revision_row["metadata_json"]["consolidation"]
    assert {snapshot["id"] for snapshot in revision_consolidation["member_snapshots"]} == {
        *revision_consolidation["cluster_member_ids"],
        first_card_id,
    }
    assert "40 hours" in revision_row["canonical_text"]
    # The old card is untouched until a reviewer accepts the revision.
    assert store.get_memory(first_card_id)["status"] == "active"

    accepted = commit.accept_consolidation_candidate(
        str(revision["candidate_memory_id"]), reason="Roll-up now includes Hades."
    )
    assert accepted["superseded_member_ids"] == [first_card_id]
    assert store.get_memory(first_card_id)["status"] == "superseded"
    for row in (*members.values(), new_member):
        assert store.get_memory(str(row["id"]))["status"] == "active"

    # Idempotent follow-up: the accepted revision covers the topic.
    third = service.propose_rollups()
    assert third.proposals == []
    assert any(group["state"] == "already_covered_by_accepted" for group in third.groups)
    conn.close()


def test_corrected_member_versions_propose_reviewed_rollup_revision(monkeypatch) -> None:
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()
    members = _seed_live_game_memories(store)
    rollups = VNextRollupService(store)
    first = rollups.propose_rollups()
    first_card_id = str(first.proposals[0]["candidate_memory_id"])
    commit = VNextMemoryCommitService(store)
    commit.accept_consolidation_candidate(first_card_id, reason="Accept initial games roll-up.")

    commit.correct(
        identity=None,
        memory_id=str(members["game-1"]["id"]),
        canonical_text="I played The Last of Us Part II for 35 hours",
        reason="Correct the duration without changing the topic.",
    )
    revised = rollups.propose_rollups()

    assert len(revised.proposals) == 1
    proposal = revised.proposals[0]
    assert proposal["candidate_state"] == "revision_proposed"
    assert proposal["revises_memory_id"] == first_card_id
    assert proposal["rollup_digest"] != first.proposals[0]["rollup_digest"]
    revision_row = store.get_memory(str(proposal["candidate_memory_id"]))
    assert "35 hours" in revision_row["canonical_text"]
    assert store.get_memory(first_card_id)["status"] == "active"
    conn.close()


# -- consolidation workflow integration ------------------------------------------------


def test_consolidation_run_proposes_rollups_and_stays_review_only(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()

    class ArtifactShim:
        def __init__(self, inner: SQLiteVNextStore) -> None:
            self._inner = inner
            self.artifacts: list[JsonObject] = []

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
            row = {"id": str(uuid4()), **artifact}
            self.artifacts.append(row)
            return dict(row)

    shim = ArtifactShim(store)
    members = _seed_live_game_memories(store)
    artifact = VNextConsolidationService(shim, embedding_provider=None).generate_memory_consolidation(
        MemoryConsolidationRequest()
    )

    candidates = _rollup_candidates(store)
    assert len(candidates) == 1
    rollups_metadata = artifact["metadata_json"]["rollups"]
    assert rollups_metadata["enabled"] is True
    assert rollups_metadata["grouping"] == "deterministic_entity_and_lexical_topic"
    assert len(rollups_metadata["proposals"]) == 1
    assert artifact["metadata_json"]["input_counts"]["rollup_proposals"] == 1
    assert str(candidates[0]["id"]) in artifact["metadata_json"]["candidate_memory_ids"]
    assert "## Roll-up Proposals" in artifact["content_markdown"]
    assert "members stay active" in artifact["content_markdown"]
    # Review-only: nothing about the members changed.
    for row in members.values():
        assert store.get_memory(str(row["id"]))["status"] == "active"
    conn.close()


def test_consolidation_run_with_rollups_disabled_creates_none(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    conn, store = _live_store()

    class ArtifactShim:
        def __init__(self, inner: SQLiteVNextStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject:
            return {"id": str(uuid4()), **artifact}

    _seed_live_game_memories(store)
    artifact = VNextConsolidationService(ArtifactShim(store), embedding_provider=None).generate_memory_consolidation(
        MemoryConsolidationRequest(propose_rollups=False)
    )
    assert _rollup_candidates(store) == []
    assert artifact["metadata_json"]["rollups"] == {"enabled": False}
    assert "propose_rollups=false" in artifact["content_markdown"]
    conn.close()


# -- commit-path guard -------------------------------------------------------------------


def test_commit_path_issues_identical_queries_with_and_without_rollups(monkeypatch) -> None:
    """Roll-ups must add zero per-commit work: the SQL statement sequence
    of a memory commit is byte-identical whether the database holds no
    roll-up cards or an accepted card over many members."""
    import re

    from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)

    timestamp_re = re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.+]+Z?")
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    hash_re = re.compile(r"\b[0-9a-f]{16,64}\b")

    def _mask(statement: str) -> str:
        masked = timestamp_re.sub("<TS>", statement)
        masked = uuid_re.sub("<UUID>", masked)
        return hash_re.sub("<HASH>", masked)

    def _commit_statements(seed_rollups: bool) -> list[str]:
        conn, store = _live_store()
        if seed_rollups:
            _seed_live_game_memories(store)
            VNextRollupService(store).propose_rollups()
            candidate_id = str(_rollup_candidates(store)[0]["id"])
            VNextMemoryCommitService(store).accept_consolidation_candidate(
                candidate_id, reason="Accepted games roll-up."
            )
        statements: list[str] = []

        def _trace(statement: str) -> None:
            flattened = " ".join(statement.split())
            # sqlite prefixes internal statements (FTS5 segment writes,
            # trigger bodies) with "--"; those vary with index size and are
            # not application queries. Timestamp/uuid/hash literals differ
            # between runs by nature; masking them keeps the comparison
            # about application statement SHAPE and COUNT.
            if flattened.startswith("--"):
                return
            statements.append(_mask(flattened))

        conn.set_trace_callback(_trace)
        VNextMemoryCommitService(store).commit(
            identity=None,
            request=MemoryCommitRequest(
                user_id=str(store.user_id),
                title="grocery budget note",
                canonical_text="the weekly grocery budget stays around forty dollars",
                idempotency_key="commit-timing-probe",
            ),
        )
        conn.set_trace_callback(None)
        conn.close()
        return statements

    without_rollups = _commit_statements(seed_rollups=False)
    with_rollups = _commit_statements(seed_rollups=True)
    assert without_rollups, "commit issued no SQL; trace hook is broken"
    assert without_rollups == with_rollups


def test_commit_and_capture_never_invoke_the_rollup_pass(monkeypatch) -> None:
    """Belt and braces for the O(1) commit-path guarantee: even if a store
    holds roll-up state, committing and capturing must never call into the
    roll-up service (which only the scheduled consolidation workflow runs)."""
    from alicebot_api import vnext_rollups
    from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService
    from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)

    def _explode(self, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("rollup pass ran on the commit/capture path")

    monkeypatch.setattr(vnext_rollups.VNextRollupService, "propose_rollups", _explode)

    conn, store = _live_store()
    result = VNextMemoryCommitService(store).commit(
        identity=None,
        request=MemoryCommitRequest(
            user_id=str(store.user_id),
            title="capture guard note",
            canonical_text="the capture guard note stays plain and lowercase",
        ),
    )
    assert result["status"] in {"committed", "needs_confirmation", "proposed"}
    capture = VNextCaptureService(store, actor_type="system")
    capture.capture_source(
        SourceCaptureInput(
            source_type="note",
            title="plain note",
            raw_text="remember: the standup moved to nine thirty on wednesdays",
        )
    )
    conn.close()
