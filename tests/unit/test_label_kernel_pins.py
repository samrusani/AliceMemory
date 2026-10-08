"""Kernel conditions that a restricted caller depends on, each pinned by a test that fails when it is removed.

Every test names the change that breaks it in ``scripts/derived_label_mutations.json``.
"""
from __future__ import annotations

from collections import Counter
from uuid import UUID, uuid4

import pytest

from alicebot_api import vnext_label_guard as guards
from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_labels import (
    HOP_BOUND,
    NODE_BOUND,
    identifier,
    settle_labels,
    with_derived_from,
)
from alicebot_api.vnext_label_repair import _load_tables, classify_stored_labels, label_gap_counts, relabel_labels_sqlite
from alicebot_api.vnext_label_writes import without_insert_floor

GUARD_USER = "label-guard"
USER = "11111111-1111-4111-8111-111111111111"


def full_kernel(rows):
    """The canonical settlement with the read bounds, independent of any guard cache."""
    return settle_labels([{**row, "user_id": GUARD_USER} for row in rows], on_cycle="unverified",
                         max_nodes=NODE_BOUND, max_hops=HOP_BOUND)


def kernel_status_counts(rows, roots, allowed):
    """Status counts of the roots a sensitivity ceiling admits, from the full kernel alone."""
    settled = full_kernel(rows)
    counts: Counter[str] = Counter()
    for root in roots:
        label = settled.by_stored("memory", str(root["id"]), user_id=GUARD_USER)
        if not label.unverified and label.sensitivity in allowed:
            counts[str(root["status"])] += 1
    return dict(counts)


class NativeStore:
    """A store with UUID keys and the narrow reads that the complete count path uses."""

    label_count_canonical_unique_ids = True

    def __init__(self, rows, roots):
        self.rows, self.roots = rows, roots

    def read_label_rows(self, kind, ids):
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in ids]

    def count_original_label_statuses(self, *args, **kwargs):
        return {}

    def iter_label_rows(self, kind, **kwargs):
        yield self.roots

    def lock_label_writes(self, **kwargs):
        return None


class PlainStore:
    """The same rows without the native count path, so every root is admitted on its own."""

    def __init__(self, rows):
        self.rows = rows

    def read_label_rows(self, kind, ids):
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in ids]


def source_row(number, sensitivity="public"):
    return {"kind": "source", "id": UUID(int=number), "user_id": GUARD_USER, "domain": "project",
            "sensitivity": sensitivity, "metadata_json": {"project_scope": [], "project_floor": []}}


def memory_row(number, metadata, sensitivity="public"):
    return {"kind": "memory", "id": UUID(int=number), "user_id": GUARD_USER, "domain": "project",
            "sensitivity": sensitivity, "status": "active", "value": {},
            "metadata_json": {"project_scope": [], "project_floor": [], **metadata}}


# The backing memory of a belief is a copy of a hidden source stored below it.
def test_a_belief_input_is_never_proved_by_the_reduced_rank_pass():
    hidden, visible = source_row(1, "confidential"), source_row(2)
    backing = memory_row(10, {"source_id": str(hidden["id"])})
    belief = {"kind": "belief", "id": UUID(int=20), "user_id": GUARD_USER, "domain": "project",
              "sensitivity": "public", "metadata_json": {}, "memory_id": str(backing["id"])}
    report = memory_row(30, with_derived_from({"workflow": "project_auto_update"}, {"beliefs": [belief]}))
    clean = memory_row(40, {"source_id": str(visible["id"])})
    roots = [backing, report, clean]
    rows = [hidden, visible, backing, belief, report, clean]
    allowed = ("public",)
    expected = kernel_status_counts(rows, roots, allowed)
    # The report is read through the belief's backing memory, so only the clean copy is readable.
    assert expected == {"active": 1}
    store = NativeStore(rows, roots)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=allowed)
        assert guard._rank_ceiling() is not None
        assert guard.readable_status_counts("memory") == expected
        state = guard._state()
        assert ("belief", str(belief["id"])) in state.nodes
        assert not state.rank_origins and not state.rank_rows
        assert guard._settle_native_rank_batch("memory", roots) is False
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == {clean["id"]}


# A chain of HOP_BOUND + 1 derived rows. The last row is HOP_BOUND + 1 hops from its original.
def test_the_bulk_count_kernel_keeps_the_hop_bound():
    source = source_row(1)
    chain = []
    previous: dict = source
    for index in range(HOP_BOUND + 1):
        kind_key = "sources" if previous is source else "memories"
        row = memory_row(100 + index, with_derived_from({"workflow": "project_auto_update"}, {kind_key: [previous]}))
        chain.append(row)
        previous = row
    # A scoped row makes the reduced proof refuse the batch, so the bulk kernel settles it.
    scoped = memory_row(500, {"source_id": str(source["id"]), "project_scope": ["alpha"]})
    roots = [*chain, scoped]
    rows = [source, *roots]
    allowed = ("public",)
    independent = guards.LabelGuard(PlainStore(rows), active=True, sensitivity_allowed=allowed)
    expected_ids = {row["id"] for row in independent.admit_rows("memory", roots)}
    # The deepest row is past the bound and unverified, so a restricted caller does not read it.
    assert chain[-1]["id"] not in expected_ids and chain[-2]["id"] in expected_ids
    assert kernel_status_counts(rows, roots, allowed) == {"active": len(expected_ids)}
    store = NativeStore(rows, roots)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=allowed)
        counts = guard.readable_status_counts("memory")
        state = guard._state()
        assert not state.rank_origins
        assert ("memory", str(chain[-2]["id"])) in state.native_labels
        assert counts == {"active": len(expected_ids)}
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == expected_ids


def node(kind, name, *, domain="project", sensitivity="public", **metadata):
    return {"kind": kind, "id": name, "user_id": "u", "domain": domain, "sensitivity": sensitivity,
            "status": "active", "metadata_json": metadata}


def stamp(*, sources=(), memories=()):
    """A canonical derived_from record naming the given ids."""
    return {"v": 1, "sources": list(sources), "memories": list(memories), "open_loops": [], "artifacts": [], "beliefs": [],
            "counts": {"sources": len(sources), "memories": len(memories), "open_loops": 0, "artifacts": 0, "beliefs": 0}}


def settled_by_name(rows):
    result = settle_labels(rows, on_cycle="raise")
    return {label.stored_id: label for label in result.rows}


@pytest.mark.parametrize("restricted_first", [True, False])
def test_equal_rows_with_different_stored_domains_keep_their_own_domain(restricted_first):
    source = node("source", "s")
    names = ("a", "b") if restricted_first else ("b", "a")
    health = node("memory", names[0], domain="health", source_id="s")
    plain = node("memory", names[1], domain="project", source_id="s")
    labels = settled_by_name([source, health, plain])
    assert labels[names[0]].domain == "health"
    assert labels[names[1]].domain == "project"


@pytest.mark.parametrize("restricted_first", [True, False])
def test_equal_rows_with_different_stored_sensitivities_keep_their_own_sensitivity(restricted_first):
    source = node("source", "s")
    names = ("a", "b") if restricted_first else ("b", "a")
    secret = node("memory", names[0], sensitivity="confidential", source_id="s")
    plain = node("memory", names[1], sensitivity="public", source_id="s")
    labels = settled_by_name([source, secret, plain])
    assert labels[names[0]].sensitivity == "confidential"
    assert labels[names[1]].sensitivity == "public"


@pytest.mark.parametrize("alpha_first", [True, False])
def test_equal_copies_of_sources_with_different_scopes_keep_their_own_scope_and_floor(alpha_first):
    alpha = node("source", "s-alpha", project_scope=["alpha"])
    beta = node("source", "s-beta", project_scope=["beta"])
    names = ("a", "b") if alpha_first else ("b", "a")
    from_alpha = node("memory", names[0], source_id="s-alpha", project_scope=["alpha", "beta"])
    from_beta = node("memory", names[1], source_id="s-beta", project_scope=["alpha", "beta"])
    labels = settled_by_name([alpha, beta, from_alpha, from_beta])
    assert (labels[names[0]].project_scope, labels[names[0]].project_floor) == (("alpha",), ("alpha",))
    assert (labels[names[1]].project_scope, labels[names[1]].project_floor) == (("beta",), ("beta",))


@pytest.mark.parametrize("scrubbed_first", [True, False])
def test_a_scrubbed_source_and_a_global_source_give_their_copies_different_scopes(scrubbed_first):
    scrubbed = node("source", "s-scrubbed", scrubbed=True)
    everywhere = node("source", "s-global")
    names = ("a", "b") if scrubbed_first else ("b", "a")
    # A scrubbed source carries no scope, so its copy keeps the stored scope. A global source widens it.
    from_scrubbed = node("memory", names[0], source_id="s-scrubbed", project_scope=["alpha"])
    from_global = node("memory", names[1], source_id="s-global", project_scope=["alpha"])
    labels = settled_by_name([scrubbed, everywhere, from_scrubbed, from_global])
    assert labels[names[0]].project_scope == ("alpha",)
    assert labels[names[1]].project_scope == ()


@pytest.mark.parametrize("floored_first", [True, False])
def test_equal_reports_over_parents_with_different_floors_keep_their_own_floor(floored_first):
    source = node("source", "s")
    floored = node("memory", "p-floored", source_id="s", project_floor=["alpha"])
    bare = node("memory", "p-bare", source_id="s", project_floor=[])
    names = ("a", "b") if floored_first else ("b", "a")
    over_floored = node("memory", names[0], workflow="project_auto_update", derived_from=stamp(memories=["p-floored"]))
    over_bare = node("memory", names[1], workflow="project_auto_update", derived_from=stamp(memories=["p-bare"]))
    labels = settled_by_name([source, floored, bare, over_floored, over_bare])
    assert labels[names[0]].project_floor == ("alpha",)
    assert labels[names[1]].project_floor == ()


@pytest.mark.parametrize("source_first", [True, False])
def test_a_source_parent_and_a_memory_parent_with_equal_labels_give_different_scopes(source_first):
    source = node("source", "s-global")
    original = node("memory", "m-global")
    names = ("a", "b") if source_first else ("b", "a")
    # Only a source parent can widen a copy. A memory parent with the same labels leaves the stored scope.
    over_source = node("memory", names[0], derived_from=stamp(sources=["s-global"]), project_scope=["alpha"])
    over_memory = node("memory", names[1], derived_from=stamp(memories=["m-global"]), project_scope=["alpha"])
    labels = settled_by_name([source, original, over_source, over_memory])
    assert labels[names[0]].project_scope == ()
    assert labels[names[1]].project_scope == ("alpha",)


def sqlite_vault(tmp_path):
    path = tmp_path / "vault.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    return path


def create_source(store, sensitivity="public"):
    return store.create_source({"source_type": "note", "title": "Input", "content_hash": str(uuid4()),
                                "sensitivity": sensitivity, "domain": "project", "metadata_json": {"project_scope": []}})


def create_copy(store, source, *, number=None, domain="project", sensitivity="public"):
    row = {"memory_key": str(uuid4()), "canonical_text": "pinned observation", "status": "active",
           "domain": domain, "sensitivity": sensitivity, "metadata_json": {"source_id": source["id"]}}
    if number is not None:
        row["id"] = str(UUID(int=number))
    with without_insert_floor():
        return store.create_memory(row)


def memory_labels(conn):
    return {row["id"]: (row["domain"], row["sensitivity"]) for row in conn.execute("SELECT id, domain, sensitivity FROM memories")}


@pytest.mark.parametrize("restricted_first", [True, False])
@pytest.mark.parametrize("field", ["domain", "sensitivity"])
def test_sqlite_repair_never_lowers_or_raises_a_stored_label_that_equal_rows_share(tmp_path, field, restricted_first):
    path = sqlite_vault(tmp_path)
    restricted = {"domain": "health"} if field == "domain" else {"sensitivity": "confidential"}
    ordinary = {"domain": "project"} if field == "domain" else {"sensitivity": "public"}
    low, high = (1, 2) if restricted_first else (2, 1)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = create_source(store)
        keeps = create_copy(store, source, number=low, **restricted)
        stays = create_copy(store, source, number=high, **ordinary)
        before = memory_labels(conn)
        below, unverified = classify_stored_labels(_load_tables(conn))
        assert below == [] and unverified == {}
        assert label_gap_counts(store) == (0, 0)
        assert relabel_labels_sqlite(conn, explicit=True) == 0
        assert memory_labels(conn) == before
        assert before[str(keeps["id"])] != before[str(stays["id"])]


def test_sqlite_repair_raises_only_the_row_below_its_input_among_equal_rows(tmp_path):
    path = sqlite_vault(tmp_path)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        hidden = create_source(store, "confidential")
        low = create_copy(store, hidden, number=1, sensitivity="public")
        kept = create_copy(store, hidden, number=2, sensitivity="regulated")
        assert relabel_labels_sqlite(conn, explicit=True) == 1
        labels = memory_labels(conn)
        assert labels[str(low["id"])] == ("project", "confidential")
        assert labels[str(kept["id"])] == ("project", "regulated")


def all_memories(store):
    return [row for batch in store.iter_label_rows("memory") for row in batch]


def restricted(store):
    return guards.LabelGuard(store, active=True, sensitivity_allowed=("public", "internal"))


def test_sqlite_savepoint_rollback_clears_the_request_admission_cache(tmp_path):
    path = sqlite_vault(tmp_path)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = create_source(store, "confidential")
        memory = create_copy(store, source)
        with guards.label_read_scope(store):
            assert restricted(store).admit_rows("memory", all_memories(store)) == []
            with pytest.raises(RuntimeError, match="abandon"):
                with store.savepoint():
                    store.update_source(source_id=str(source["id"]), patch={"sensitivity": "public"})
                    admitted = restricted(store).admit_rows("memory", all_memories(store))
                    assert [row["id"] for row in admitted] == [memory["id"]]
                    raise RuntimeError("abandon")
            assert store.get_source(str(source["id"]))["sensitivity"] == "confidential"
            assert restricted(store).admit_rows("memory", all_memories(store)) == []


def test_sqlite_savepoint_rollback_clears_the_request_counts(tmp_path):
    path = sqlite_vault(tmp_path)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = create_source(store, "confidential")
        create_copy(store, source)
        with guards.label_read_scope(store):
            assert restricted(store).readable_status_counts("memory") == {}
            with pytest.raises(RuntimeError, match="abandon"):
                with store.savepoint():
                    store.update_source(source_id=str(source["id"]), patch={"sensitivity": "public"})
                    assert restricted(store).readable_status_counts("memory") == {"active": 1}
                    raise RuntimeError("abandon")
            assert store.get_source(str(source["id"]))["sensitivity"] == "confidential"
            assert restricted(store).readable_status_counts("memory") == {}
