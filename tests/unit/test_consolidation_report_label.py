"""A consolidation report is labelled over every row it names.

``GET /v0/vnext/artifacts/{id}`` decides by the stored domain and sensitivity alone, so the label must be at least
as strict as every row whose text, topic or id the report prints. These tests run the real service and read the
printed report and its metadata as the judge: if a marker taken from a restricted row, or the id of one, is in the
output, the stored label has to be that row's label or stricter.
"""

from __future__ import annotations

import json
from itertools import product
from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, RESTRICTED_DOMAINS
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from tests.unit.test_vnext_consolidation import (
    FakeConsolidationStore,
    MappedEmbeddingProvider,
    _seed_memory,
    _seed_six_memories,
)
from tests.unit.test_vnext_rollups import _seed_game_memories, _seed_texts

RANK = {
    "public": 1,
    "internal": 2,
    "unknown": 2,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}
EVERYTHING = list(ALL_SENSITIVITY)


@pytest.fixture(autouse=True)
def _no_embedding_provider(monkeypatch):
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)


def _run(store, **request) -> dict:
    request.setdefault("agent_identity", None)
    request.setdefault("sensitivity_allowed", EVERYTHING)
    return VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(
        MemoryConsolidationRequest(**request)
    )


def _printed(artifact: dict) -> str:
    return artifact["content_markdown"] + "\n" + json.dumps(artifact["metadata_json"], default=str)


def _relabel(store, **patch) -> None:
    for row in store.memories:
        row.update(patch)


@pytest.mark.parametrize("sensitivity", ALL_SENSITIVITY)
def test_a_rollup_only_report_takes_the_label_of_its_inputs(sensitivity: str) -> None:
    store = FakeConsolidationStore()
    members = _seed_game_memories(store)
    _relabel(store, sensitivity=sensitivity)
    artifact = _run(store)

    assert artifact["metadata_json"]["consolidation"]["cluster_membership"] == []
    assert len(artifact["metadata_json"]["rollups"]["proposals"]) == 1
    assert "hours played" in artifact["content_markdown"]
    assert {str(row["id"]) for row in members.values()} <= set(artifact["metadata_json"]["rollups"]["groups"][0]["member_ids"])
    assert artifact["sensitivity"] == sensitivity


def test_one_restricted_roll_up_input_among_public_ones_labels_the_whole_report() -> None:
    store = FakeConsolidationStore()
    _seed_game_memories(store)
    _relabel(store, sensitivity="public")
    store.memories[2]["sensitivity"] = "confidential"
    artifact = _run(store)

    assert len(artifact["metadata_json"]["rollups"]["proposals"]) == 1
    assert artifact["sensitivity"] == "confidential"


def test_cluster_rows_and_roll_up_rows_are_labelled_together() -> None:
    """The near-duplicate cluster is internal and the roll-up inputs are confidential: the report is confidential,
    and the other way round the cluster's label still holds."""

    for cluster_sensitivity, rollup_sensitivity, expected in (
        ("internal", "confidential", "confidential"),
        ("confidential", "internal", "confidential"),
        ("private", "private", "private"),
    ):
        store = FakeConsolidationStore()
        mapping: dict[str, list[float]] = {}
        near_dups, _distinct = _seed_six_memories(store, mapping)
        cluster_ids = {str(row["id"]) for row in near_dups}
        game_rows = _seed_game_memories(store)
        for row in store.memories:
            row["sensitivity"] = cluster_sensitivity if str(row["id"]) in cluster_ids else rollup_sensitivity
        artifact = VNextConsolidationService(
            store, embedding_provider=MappedEmbeddingProvider(mapping)
        ).generate_memory_consolidation(MemoryConsolidationRequest(agent_identity=None, sensitivity_allowed=EVERYTHING))

        assert artifact["metadata_json"]["consolidation"]["cluster_membership"], "the run must have a cluster"
        assert len(artifact["metadata_json"]["rollups"]["proposals"]) == 1, "and a roll-up card"
        rollup_member_ids = set(artifact["metadata_json"]["rollups"]["groups"][0]["member_ids"])
        assert rollup_member_ids == {str(row["id"]) for row in game_rows.values()}
        assert artifact["sensitivity"] == expected, (cluster_sensitivity, rollup_sensitivity)


def test_an_unrestricted_rollup_only_report_keeps_a_label_that_reads_the_same() -> None:
    """Internal inputs: the stored label is ``internal``, which every profile reads as it read ``unknown``."""

    store = FakeConsolidationStore()
    _seed_game_memories(store)
    artifact = _run(store)

    assert (artifact["domain"], artifact["sensitivity"]) == ("unknown", "internal")


# -- the printed report is the judge --------------------------------------------------------


def _texts(store, texts, **fields) -> list[dict]:
    rows = []
    for text in texts:
        rows.append(
            store.create_memory(
                {
                    "memory_key": f"memory.{uuid4().hex}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "episode",
                    "title": text[:80],
                    "canonical_text": text,
                    "summary": text[:80],
                    "domain": "personal",
                    "sensitivity": "confidential",
                    "metadata_json": {"session_date": "2026-03-02"},
                    **fields,
                }
            )
        )
    return rows


def _near_duplicates(store) -> list[dict]:
    return _texts(store, ["Zorblax account recovery phrase is kept offline"] * 3)


def _gate_dropped(store) -> list[dict]:
    return _texts(
        store,
        (
            "the zorblax hose reel sits by the gate",
            "a zorblax watering can waits under the porch light",
            "the zorblax sprinkler head points at the garden fence",
        ),
    )


def _junk(store) -> list[dict]:
    return _texts(
        store,
        (
            "I'm excited about the zorblax harbor",
            "I'm tired after the zorblax summit",
            "I'm curious about the zorblax lantern",
        ),
    )


def _generic_anchor(store) -> list[dict]:
    filler = [f"need zorblax marker{index:02d}a marker{index:02d}b" for index in range(36)]
    rows = _texts(store, filler, sensitivity="private")
    for index, row in enumerate(rows):
        row["metadata_json"] = {"session_date": f"2023-03-{(index % 12) + 1:02d}"}
    return rows


@pytest.mark.parametrize("scenario", (_near_duplicates, _gate_dropped, _junk, _generic_anchor))
def test_a_marker_that_reaches_the_report_has_its_row_label_behind_it(scenario) -> None:
    store = FakeConsolidationStore()
    rows = scenario(store)
    floor = max(RANK[str(row["sensitivity"])] for row in rows)
    artifact = _run(store)

    assert "zorblax" in _printed(artifact).casefold(), "the scenario must print the marker, or it proves nothing"
    assert RANK[artifact["sensitivity"]] >= floor, (scenario.__name__, artifact["sensitivity"])


def test_a_report_that_prints_no_text_of_a_row_does_not_take_its_label() -> None:
    """The label follows what the report names, not everything the run read: a restricted row in a group that no
    line of the report prints leaves the label where the printed rows put it."""

    store = FakeConsolidationStore()
    _seed_game_memories(store)
    _relabel(store, sensitivity="internal")
    # One restricted memory that shares nothing with another row: no group forms and no line names it.
    _texts(store, ["a lone vault combination note"], sensitivity="confidential")
    artifact = _run(store)

    assert "vault" not in _printed(artifact).casefold()
    assert artifact["sensitivity"] == "internal"


# -- the domain follows the same rows ----------------------------------------------------------


def test_the_domain_of_a_report_follows_the_rows_a_skip_line_names() -> None:
    store = FakeConsolidationStore()
    _near_duplicates(store)
    _relabel(store, domain="health", sensitivity="internal")
    artifact = _run(store)

    assert "zorblax" in _printed(artifact).casefold()
    assert "health" in RESTRICTED_DOMAINS
    assert artifact["domain"] == "health"


def test_a_row_the_cluster_and_a_roll_up_line_both_name_counts_once_for_the_domain() -> None:
    """The near-duplicate trio is named by its cluster and again by the roll-up line that leaves its group to dedup.
    Counted twice it would outweigh the five roll-up inputs. The most frequent restricted label of the distinct
    rows is the one the roll-up inputs carry."""

    store = FakeConsolidationStore()
    mapping: dict[str, list[float]] = {}
    near_dups, _distinct = _seed_six_memories(store, mapping)
    cluster_ids = {str(row["id"]) for row in near_dups}
    _seed_game_memories(store)
    for row in store.memories:
        row["domain"] = "health" if str(row["id"]) in cluster_ids else "legal"
    artifact = VNextConsolidationService(
        store, embedding_provider=MappedEmbeddingProvider(mapping)
    ).generate_memory_consolidation(MemoryConsolidationRequest(agent_identity=None, sensitivity_allowed=EVERYTHING))

    assert artifact["metadata_json"]["consolidation"]["cluster_membership"]
    assert any(
        "covered_by_near_duplicate_cluster" in reason for reason in artifact["metadata_json"]["rollups"]["skipped"]
    ), "the roll-up pass must name the cluster rows a second time"
    assert artifact["domain"] == "legal"


@pytest.mark.parametrize("position", range(5))
def test_the_roll_up_card_is_labelled_by_each_member_it_lists(position: int) -> None:
    """The card is a memory the run writes next to its report, and it lists every member by id."""

    store = FakeConsolidationStore()
    members = list(_seed_game_memories(store).values())
    store.memories[position]["sensitivity"] = "confidential"
    _run(store)
    (card,) = [row for row in store.memories if row["status"] == "candidate"]

    assert str(members[position]["id"]) in json.dumps(card["metadata_json"], default=str)
    assert card["sensitivity"] == "confidential"


# -- cards of an earlier pass ----------------------------------------------------------------------


def test_a_card_an_earlier_pass_made_labels_the_report_that_names_it() -> None:
    store = FakeConsolidationStore()
    _seed_game_memories(store)
    first = _run(store, sensitivity_allowed=["internal", "private"])
    assert first["sensitivity"] == "internal"
    (card_id,) = [str(row["id"]) for row in store.memories if row["status"] == "candidate"]
    for row in store.memories:
        if str(row["id"]) == card_id:
            row["sensitivity"] = "private"
    second = _run(store, sensitivity_allowed=["internal", "private"])

    assert card_id in _printed(second)
    assert second["sensitivity"] == "private"
