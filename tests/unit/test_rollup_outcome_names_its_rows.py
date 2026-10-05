"""``RollupOutcome.input_rows`` lists every row the roll-up pass writes into its report.

The consolidation report is read behind a domain and a sensitivity label taken over these rows. A row the report
names but that is missing here is a row the label does not cover. The report names a row in three ways:

* a proposed group lists its members (ids in the metadata, the topic on the card);
* a group the pass did not propose still puts its key in a skip line (``topic:zorblax``, ``entity:...``,
  ``semantic:cluster-<member id>``), and the key is made from the text or the id of its members;
* a card an earlier pass made for the same topic is named by id (pending, accepted, expired or held).

Each test builds a corpus whose rows are confidential or private, shows that the report prints the text or the id,
and checks that the rows are in ``input_rows``. A test that does not see the key in the output fails first, so none
of them can pass by building a corpus that leaves the key out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import alicebot_api.vnext_rollups as vnext_rollups
from alicebot_api.vnext_rollups import ROLLUP_CANDIDATE_KIND, RollupOutcome, VNextRollupService
from tests.unit.test_vnext_rollups import FakeRollupStore, _seed_game_memories, _seed_texts
from tests.unit.test_vnext_rollups_semantic import (
    SemanticFakeStore,
    MappedEmbeddingProvider,
    KITCHEN_VECTORS,
    UNRELATED_SPECS,
    UNRELATED_VECTORS,
    _seed,
)


def _shown(outcome: RollupOutcome) -> str:
    """Everything the consolidation report prints from the roll-up pass: its lines and its metadata."""

    return "\n".join(outcome.markdown_lines()) + "\n" + json.dumps(outcome.to_metadata(), default=str)


def _named(outcome: RollupOutcome) -> set[str]:
    return {str(row["id"]) for row in outcome.input_rows}


def _ids(rows) -> set[str]:
    return {str(row["id"]) for row in rows}


ALLOWED = ["public", "internal", "private", "confidential", "unknown"]


def _propose(store, **kwargs) -> RollupOutcome:
    kwargs.setdefault("sensitivity_allowed", ALLOWED)
    service_kwargs = {"embedding_provider": kwargs.pop("embedding_provider")} if "embedding_provider" in kwargs else {}
    return VNextRollupService(store, **service_kwargs).propose_rollups(**kwargs)


def _add(store, text: str, *, sensitivity: str = "confidential", domain: str = "personal", **fields) -> dict:
    return store.create_memory(
        {
            "memory_key": f"memory.{uuid4().hex}",
            "value": {"text": text},
            "status": "active",
            "memory_type": "episode",
            "title": text[:80],
            "canonical_text": text,
            "summary": text[:80],
            "domain": domain,
            "sensitivity": sensitivity,
            "metadata_json": {},
            **fields,
        }
    )


def _set_all(store, **patch) -> None:
    for row in store.memories:
        row.update(patch)


# -- the members of a proposed group -------------------------------------------------


def test_members_of_a_proposed_group_are_named() -> None:
    store = FakeRollupStore()
    members = _seed_game_memories(store)
    outcome = _propose(store)

    assert len(outcome.proposals) == 1
    assert _named(outcome) == _ids(members.values())


# -- skip lines that put a key made from member text in the report -----------------------


def test_members_of_a_group_too_large_to_propose_are_named(monkeypatch) -> None:
    monkeypatch.setattr(vnext_rollups, "MAX_ROLLUP_GROUP_MEMBERS", 3)
    store = FakeRollupStore()
    rows = [_add(store, f"zorblax account recovery phrase variant{number}") for number in range(4)]
    outcome = _propose(store)

    assert outcome.proposals == []
    assert any(reason.startswith("group_too_large: topic:zorblax") for reason in outcome.skipped)
    assert "zorblax" in _shown(outcome)
    assert _named(outcome) == _ids(rows)


def test_members_of_a_near_duplicate_group_left_to_dedup_are_named() -> None:
    store = FakeRollupStore()
    rows = [_add(store, "Zorblax account recovery phrase is kept offline") for _ in range(3)]
    outcome = _propose(store)

    assert outcome.proposals == []
    assert any(reason.startswith("near_duplicate_group_left_to_dedup: topic:zorblax") for reason in outcome.skipped)
    assert "zorblax" in _shown(outcome)
    assert _named(outcome) == _ids(rows)


def test_members_named_by_a_skip_line_are_named_in_every_project_partition() -> None:
    """Grouping runs once for each project scope, and each scope's skip lines name their own members."""

    store = FakeRollupStore()
    first = [_add(store, "Zorblax account recovery phrase is kept offline", project_id="project-a") for _ in range(3)]
    second = [_add(store, "Quimble vault combination is kept offline", project_id="project-b") for _ in range(3)]
    outcome = _propose(store)

    lines = [reason for reason in outcome.skipped if "near_duplicate_group_left_to_dedup" in reason]
    assert any(": topic:zorblax" in line for line in lines) and any(": topic:quimble" in line for line in lines)
    assert all(line.startswith("scope:") for line in lines)
    assert _named(outcome) == _ids(first) | _ids(second)


def test_members_of_a_group_covered_by_a_near_duplicate_cluster_are_named() -> None:
    store = FakeRollupStore()
    members = _seed_game_memories(store)
    outcome = _propose(store, exclude_member_id_sets=[_ids(members.values())])

    assert outcome.proposals == []
    assert any(reason.startswith("covered_by_near_duplicate_cluster") for reason in outcome.skipped)
    assert _named(outcome) == _ids(members.values())


def test_members_of_a_group_the_quality_gate_dropped_are_named() -> None:
    store = FakeRollupStore()
    # Three rows share the topic token and one day, with no amount and no date to aggregate: the group fails the gate.
    texts = (
        "the zorblax hose reel sits by the gate",
        "a zorblax watering can waits under the porch light",
        "the zorblax sprinkler head points at the garden fence",
    )
    rows = [_add(store, text, metadata_json={"session_date": "2026-03-02"}) for text in texts]
    outcome = _propose(store)

    assert outcome.proposals == []
    gate_lines = [reason for reason in outcome.skipped if reason.startswith("quality_gate_dropped")]
    assert gate_lines and "topic:zorblax" in gate_lines[0]
    assert _ids(rows) <= _named(outcome)


def test_members_of_a_junk_label_group_are_named() -> None:
    store = FakeRollupStore()
    _seed_texts(
        store,
        (
            ("I'm excited about the violet harbor", None),
            ("I'm tired after the granite summit", None),
            ("I'm curious about the copper lantern", None),
        ),
    )
    _set_all(store, sensitivity="confidential")
    outcome = _propose(store)

    assert outcome.proposals == []
    gate_lines = [reason for reason in outcome.skipped if reason.startswith("quality_gate_dropped")]
    assert gate_lines and "label_without_content_words" in gate_lines[0]
    assert _named(outcome) == _ids(store.memories)


def test_members_of_a_generic_anchor_the_store_dropped_are_named() -> None:
    store = FakeRollupStore()
    filler = tuple(
        (f"need marker{index:02d}a marker{index:02d}b", f"2023-03-{(index % 12) + 1:02d}") for index in range(36)
    )
    hikes = tuple(
        (f"Hiked {distance} km along the {name} trail", f"2023-04-{day:02d}")
        for distance, name, day in ((5, "juniper", 2), (8, "basalt", 9), (11, "willow", 16), (13, "mesa", 23))
    )
    _seed_texts(store, filler + hikes)
    _set_all(store, sensitivity="private")
    outcome = _propose(store)

    gate_lines = [reason for reason in outcome.skipped if reason.startswith("quality_gate_dropped")]
    assert gate_lines and "topic:need (anchor_generic_for_store)" in gate_lines[0]
    filler_ids = {str(row["id"]) for row in store.memories if str(row["canonical_text"]).startswith("need ")}
    assert len(filler_ids) == 36
    assert filler_ids <= _named(outcome)


def test_a_dropped_group_beyond_the_six_examples_is_not_named() -> None:
    """Only the groups a skip line shows are named, so the label does not rise for rows the report never prints."""

    store = FakeRollupStore()
    words = ("zorblax", "quimble", "fenwick", "glorbit", "hamsel", "juniva", "kestrel", "lumbar")
    # Eight groups of three, each sharing one topic word and nothing to aggregate, so each fails the gate.
    # Only the first six are printed in the skip line.
    for group, word in enumerate(words):
        for member in range(3):
            _add(
                store,
                f"{word} {word[:3]}{'abc'[member]}{'pqrstuvw'[group]}zz",
                metadata_json={"session_date": "2026-03-02"},
            )
    outcome = _propose(store)

    gate_lines = [reason for reason in outcome.skipped if reason.startswith("quality_gate_dropped")]
    assert gate_lines
    printed = gate_lines[0].split("e.g. ", 1)[1].split("; ")
    assert len(printed) == 6
    named_texts = {str(row["canonical_text"]) for row in outcome.input_rows}
    shown_words = [word for word in words if any(f"topic:{word}" in entry for entry in printed)]
    named_words = [word for word in words if any(text.startswith(word) for text in named_texts)]
    assert len(shown_words) == 6
    assert named_words == shown_words


def test_the_member_whose_id_a_dropped_semantic_cluster_prints_is_named() -> None:
    specs = (
        ("solo-1", "Refilled the propane tank for $30", "2026-05-01"),
        ("solo-2", "Patched the trampoline mat for $22", "2026-05-08"),
        ("solo-3", "Sharpened the mower blade for $15", "2026-05-15"),
    )
    store = SemanticFakeStore()
    mapping: dict[str, list[float]] = {}
    rows = _seed(store, mapping, specs, KITCHEN_VECTORS)
    _seed(store, mapping, UNRELATED_SPECS, UNRELATED_VECTORS)
    _set_all(store, sensitivity="confidential")
    outcome = _propose(store, embedding_provider=MappedEmbeddingProvider(mapping))

    assert outcome.quality_gate["dropped_by_reason"]["semantic_no_dominant_label"] >= 1
    printed_id = min(_ids(rows.values()))
    assert f"semantic:cluster-{printed_id}" in _shown(outcome)
    assert printed_id in _named(outcome)


def test_members_of_a_semantic_cluster_whose_label_collided_are_named(monkeypatch) -> None:
    """Two clusters that settle on one dominant label: the second prints ``semantic:<label>`` and is left out."""

    store = FakeRollupStore()
    first = [_add(store, f"alpha{number} bravo{number} charlie{number}") for number in range(3)]
    second = [_add(store, f"delta{number} echo{number} foxtrot{number}") for number in range(3)]

    def two_clusters(self, remaining, **kwargs):
        record = {"clusters_formed": 2, "groups_admitted": 0, "skipped": []}
        return [
            (tuple(sorted(first, key=lambda row: str(row["id"]))), 0.8, 0.7),
            (tuple(sorted(second, key=lambda row: str(row["id"]))), 0.8, 0.7),
        ], record

    monkeypatch.setattr(VNextRollupService, "_semantic_clusters", two_clusters)
    monkeypatch.setattr(vnext_rollups, "_dominant_noun_phrase", lambda members, stats: ("kitchen items", ("kitchen",)))
    outcome = _propose(store, embedding_provider=MappedEmbeddingProvider({}))

    collision = [reason for reason in outcome.skipped if reason.startswith("semantic_label_collision")]
    assert collision and "semantic:kitchen items" in collision[0]
    assert _ids(second) <= _named(outcome)


def test_names_are_not_repeated_when_a_row_is_in_two_groups() -> None:
    """The domain of a report is the most frequent restricted label of the rows it names, so a row counts once."""

    store = FakeRollupStore()
    rows = [_add(store, "Zorblax account recovery phrase is kept offline") for _ in range(3)]
    outcome = _propose(store)

    assert len(outcome.input_rows) == len(rows)
    assert len(_named(outcome)) == len(rows)


def test_rows_with_no_id_are_not_merged_into_one() -> None:
    """A store row always has an id, but a row that has none must not hide another behind it."""

    outcome = RollupOutcome()
    outcome.name_rows([{"sensitivity": "public"}, {"sensitivity": "confidential"}, {"id": "a", "sensitivity": "private"}])
    outcome.name_rows([{"id": "a", "sensitivity": "private"}])

    assert [row["sensitivity"] for row in outcome.input_rows] == ["public", "confidential", "private"]


# -- cards an earlier pass made for the same topic -------------------------------------


class KeyedRollupStore(FakeRollupStore):
    """FakeRollupStore with the by-key read the expired-card and held-card checks use."""

    def get_memory_by_key(self, *, memory_key: str, include_deleted: bool = False):
        for row in self.memories:
            if row.get("memory_key") == memory_key:
                return dict(row)
        return None


def _card_for_the_games(store: KeyedRollupStore) -> dict:
    members = _seed_game_memories(store)
    _propose(store)
    (card,) = [row for row in store.memories if row["metadata_json"].get("candidate_kind") == ROLLUP_CANDIDATE_KIND]
    assert card["sensitivity"] == "internal" and {row["sensitivity"] for row in members.values()} == {"internal"}
    return card


def _raise_card(card: dict, **patch) -> None:
    card.update({"sensitivity": "private", **patch})


def _second_pass(store: KeyedRollupStore) -> RollupOutcome:
    return _propose(store, sensitivity_allowed=["internal", "private"])


def test_a_pending_card_the_report_names_is_named() -> None:
    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    _raise_card(card)
    outcome = _second_pass(store)

    assert [group["state"] for group in outcome.groups] == ["existing_candidate"]
    assert str(card["id"]) in _shown(outcome)
    assert str(card["id"]) in _named(outcome)


def test_an_accepted_card_the_report_names_is_named() -> None:
    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    _raise_card(card, status="active")
    outcome = _second_pass(store)

    assert [group["state"] for group in outcome.groups] == ["already_covered_by_accepted"]
    assert str(card["id"]) in _shown(outcome)
    assert str(card["id"]) in _named(outcome)


def test_the_accepted_card_a_revision_retires_is_named() -> None:
    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    _raise_card(card, status="active")
    _add(store, "I played Tetris for 5 hours", sensitivity="internal", metadata_json={"session_date": "2023-07-09"})
    outcome = _second_pass(store)

    assert [proposal["candidate_state"] for proposal in outcome.proposals] == ["revision_proposed"]
    assert str(card["id"]) in _shown(outcome)
    assert str(card["id"]) in _named(outcome)


def test_an_expired_card_the_report_names_is_named() -> None:
    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    _raise_card(card, status="active", valid_to=past)
    outcome = _second_pass(store)

    assert [group["state"] for group in outcome.groups] == ["expired_card_members_unchanged"]
    assert str(card["id"]) in _shown(outcome)
    assert str(card["id"]) in _named(outcome)


def test_a_held_card_the_report_names_is_named() -> None:
    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    _raise_card(card, status="rejected")
    outcome = _second_pass(store)

    assert [group["state"] for group in outcome.groups] == ["existing_card_members_unchanged"]
    assert str(card["id"]) in _shown(outcome)
    assert str(card["id"]) in _named(outcome)


def test_a_card_the_pass_may_not_name_is_not_named() -> None:
    """A card above the sensitivity ceiling of the pass is not named in the report, so it does not raise the label."""

    store = KeyedRollupStore()
    card = _card_for_the_games(store)
    _raise_card(card, status="rejected", sensitivity="confidential")
    outcome = _second_pass(store)

    assert [group["state"] for group in outcome.groups] == ["digest_key_held_by_another_row"]
    assert str(card["id"]) not in _shown(outcome)
    assert str(card["id"]) not in _named(outcome)
