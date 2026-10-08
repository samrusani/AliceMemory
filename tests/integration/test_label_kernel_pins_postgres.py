"""Real-store pins for the label kernel: shared memo entries, belief inputs and the hop bound."""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from alicebot_api.cli import labels
from alicebot_api.vnext_agent_control import VNEXT_DOMAINS
from alicebot_api.vnext_derived_labels import HOP_BOUND, with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_repair import classify_stored_labels, label_gap_counts, load_postgres_label_tables, plan_label_repairs
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.integration.derived_labels_postgres_support import label_harness


def create_copy(store, source, number, *, domain="project", sensitivity="public"):
    with without_insert_floor():
        return store.create_memory({
            "id": str(UUID(int=number)), "memory_key": str(uuid4()), "canonical_text": "Pinned observation",
            "status": "active", "domain": domain, "sensitivity": sensitivity,
            "metadata_json": {"source_id": str(source["id"])},
        })


def stored_labels(store, rows):
    return {str(row["id"]): (store.get_memory(str(row["id"]))["domain"], store.get_memory(str(row["id"]))["sensitivity"])
            for row in rows}


def run_repair(harness):
    return labels._run_vnext_labels_repair(SimpleNamespace(database_url=harness.urls["app"], user_id=harness.user_id), None)


@pytest.mark.parametrize("restricted_first", [True, False])
@pytest.mark.parametrize("field", ["domain", "sensitivity"])
def test_repair_never_lowers_or_raises_a_stored_label_that_equal_rows_share(label_harness, field, restricted_first):
    harness = label_harness
    source = harness.source()
    restricted = {"domain": "health"} if field == "domain" else {"sensitivity": "confidential"}
    ordinary = {"domain": "project"} if field == "domain" else {"sensitivity": "public"}
    low, high = (1, 2) if restricted_first else (2, 1)
    with harness.store() as store:
        keeps = create_copy(store, source, low, **restricted)
        stays = create_copy(store, source, high, **ordinary)
        before = stored_labels(store, [keeps, stays])
        assert before[str(keeps["id"])] != before[str(stays["id"])]
        tables = load_postgres_label_tables(store.conn)
        assert plan_label_repairs(tables) == []
        assert classify_stored_labels(tables) == ([], {})
        assert label_gap_counts(store) == (0, 0)
    assert run_repair(harness) == "labels repair updated 0"
    with harness.store() as store:
        assert stored_labels(store, [keeps, stays]) == before


def test_repair_raises_only_the_row_below_its_input_among_equal_rows(label_harness):
    harness = label_harness
    hidden = harness.source(sensitivity="confidential")
    with harness.store() as store:
        low = create_copy(store, hidden, 1, sensitivity="public")
        kept = create_copy(store, hidden, 2, sensitivity="regulated")
    assert run_repair(harness) == "labels repair updated 1"
    with harness.store() as store:
        assert stored_labels(store, [low, kept]) == {
            str(low["id"]): ("project", "confidential"),
            str(kept["id"]): ("project", "regulated"),
        }


def readable_memory_counts(harness, key):
    status, body, _ = harness.request("GET", "/v0/vnext/workspace", key=key)
    assert status == 200, body
    return body["summary"]["memory_status_counts"]


def independent_counts(store, allowed):
    """Each root through the complete effective label, with no reduced proof and no bulk pass."""
    guard = LabelGuard(store, active=True, domains=tuple(VNEXT_DOMAINS), sensitivity_allowed=allowed)
    with label_read_scope(store):
        rows = [row for batch in store.iter_label_rows("memory") for row in batch]
        counts: dict[str, int] = {}
        for row in guard.admit_rows("memory", rows):
            counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
        return counts


def test_a_belief_input_is_not_proved_by_the_reduced_rank_pass_on_postgres(label_harness):
    harness = label_harness
    hidden, visible = harness.source(sensitivity="confidential"), harness.source()
    with harness.store() as store, without_insert_floor():
        backing = create_copy(store, hidden, 10)
        belief = store.create_belief({"memory_id": str(backing["id"]), "claim": "Synthetic belief"})
        report = store.create_memory({
            "id": str(UUID(int=30)), "memory_key": str(uuid4()), "canonical_text": "Report over the belief",
            "status": "active", "domain": "project", "sensitivity": "public",
            "metadata_json": with_derived_from({"workflow": "project_auto_update"}, {"beliefs": [belief]}),
        })
        clean = create_copy(store, visible, 40)
    key = harness.key("trusted_local_agent")
    with harness.store() as store:
        allowed = ("public", "internal", "unknown", "private")
        expected = independent_counts(store, allowed)
        assert expected == {"active": 1}
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=allowed)
            assert guard._rank_ceiling() is not None
            assert guard.readable_status_counts("memory") == expected
            assert not guard._state().rank_origins
            assert {str(row["id"]) for row in guard.admit_rows("memory", [report, backing, clean])} == {str(clean["id"])}
    assert readable_memory_counts(harness, key) == expected


def test_the_bulk_count_kernel_keeps_the_hop_bound_on_postgres(label_harness):
    harness = label_harness
    source = harness.source()
    with harness.store() as store, without_insert_floor():
        previous, chain = source, []
        for index in range(HOP_BOUND + 1):
            link = {"sources" if previous is source else "memories": [previous]}
            previous = store.create_memory({
                "id": str(UUID(int=100 + index)), "memory_key": str(uuid4()), "canonical_text": f"Chain link {index}",
                "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": with_derived_from({"workflow": "project_auto_update"}, link),
            })
            chain.append(previous)
        # A scoped row makes the reduced proof refuse the batch, so the bulk kernel settles it.
        store.create_memory({
            "id": str(UUID(int=500)), "memory_key": str(uuid4()), "canonical_text": "Scoped copy", "status": "active",
            "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": str(source["id"]), "project_scope": ["alpha"]},
        })
    key = harness.key("trusted_local_agent")
    with harness.store() as store:
        allowed = ("public", "internal", "unknown", "private")
        expected = independent_counts(store, allowed)
        # The last link is HOP_BOUND + 1 hops from its original, so it is unverified and unreadable.
        assert expected == {"active": HOP_BOUND + 1}
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=allowed)
            assert guard.readable_status_counts("memory") == expected
            assert not guard._state().rank_origins
            assert ("memory", str(chain[-2]["id"])) in guard._state().native_labels
            assert ("memory", str(chain[-1]["id"])) not in guard._state().native_labels
    assert readable_memory_counts(harness, key) == expected
