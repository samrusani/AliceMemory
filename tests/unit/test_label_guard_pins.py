"""Pins for request-guard conditions that no other test reaches.

Each condition below is correct today. Removing any one of them lets a
restricted caller get a row, a count or a label that the canonical kernel
refuses, while the rest of the suite stays green. Every test here is the one
that fails when its condition goes. scripts/derived_label_mutations.json holds
the exact edit for each, replayed by scripts/verify_derived_label_mutations.py.

Nothing here depends on set or hash order: rows are settled in an explicit
order and every answer is compared with the kernel, never with a constant.
"""
from __future__ import annotations

import dataclasses
from dataclasses import replace
import sys
from uuid import UUID

import pytest

from alicebot_api import vnext_label_guard as guard_module
from alicebot_api.vnext_derived_labels import HOP_BOUND, identifier, settle_labels, with_derived_from
from alicebot_api.vnext_label_guard import (
    LabelGuard,
    _RequestLabels,
    invalidate_read_labels,
    label_read_scope,
    request_row_cache,
)

USER = "label-guard"
CEILING = ("public", "internal", "private", "unknown")
P1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
P2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def uid(number: int) -> str:
    return str(UUID(int=number))


def source(number, *, sensitivity="public", scope=(), metadata=None):
    return {"kind": "source", "id": uid(number), "domain": "project", "sensitivity": sensitivity,
            "metadata_json": {"project_scope": list(scope), **(metadata or {})}}


def memory(number, metadata):
    return {"kind": "memory", "id": uid(number), "domain": "project", "sensitivity": "public",
            "status": "active", "metadata_json": metadata}


class Rows:
    """A store that only serves the narrow label reader."""

    def __init__(self, rows):
        self.rows = rows
        self.index = {}
        for row in rows:
            self.index.setdefault((row["kind"], identifier(row["id"])), []).append(row)

    def read_label_rows(self, kind, ids):
        return [dict(row) for item in ids for row in self.index.get((kind, item), ())]


class CountedRows(Rows):
    """A store whose complete population is enumerated, as a count needs."""

    def __init__(self, rows, roots):
        super().__init__(rows)
        self.roots = roots

    def iter_label_rows(self, kind, **kwargs):
        yield self.roots


class NativeRows(CountedRows):
    """A store of canonical UUID rows: the only kind that may use a reduced proof."""

    label_count_canonical_unique_ids = True

    def count_original_label_statuses(self, *args, **kwargs):
        return {}


def kernel(rows):
    """The canonical answer for every stored row, under the bounds the guard uses now."""

    result = settle_labels([{**row, "user_id": USER} for row in rows], on_cycle="unverified",
                           max_hops=guard_module.HOP_BOUND, max_nodes=guard_module.NODE_BOUND)
    return lambda row: result.by_stored(row["kind"], str(row["id"]), user_id=USER)


def shown(effective):
    meta = effective["metadata_json"]
    return (effective["domain"], effective["sensitivity"], tuple(meta["project_scope"]),
            tuple(meta["project_floor"]), effective["unverified"])


def kernel_shown(label):
    if label.unverified:
        return label.domain, "regulated", (), (), True
    return label.domain, label.sensitivity, label.project_scope, label.project_floor, False


def chain(length):
    """One public source, a copy of it, then each memory consolidating the one before."""

    rows = [source(1)]
    for i in range(length):
        meta = ({"source_id": rows[0]["id"]} if i == 0
                else {"consolidation": {"cluster_member_ids": [rows[-1]["id"]]}})
        rows.append(memory(1000 + i, meta))
    return rows


def shortcut_rows():
    """Three roots with one dependency signature that the direct settle refuses and the kernel verifies.

    Each chain member also names the source, so every row is at most two hops
    from it by the shortest path, while the longest path through the chain
    exceeds a hop bound of 3. The direct settle measures the longest path and
    gives up; the kernel measures the shortest and verifies.
    """

    origin = source(1, scope=("P1",))
    members = []
    for i in range(3):
        if i == 0:
            meta = {"source_id": origin["id"], "project_scope": ["P1"]}
        else:
            meta = with_derived_from(
                {"consolidation": {"cluster_member_ids": [members[-1]["id"]]}, "project_scope": ["P1"]},
                {"sources": [origin], "memories": [members[-1]]},
            )
        members.append(memory(10 + i, meta))
    roots = [memory(20 + i, with_derived_from(
        {"consolidation": {"cluster_member_ids": [m["id"] for m in members]}, "project_scope": ["P1"]},
        {"memories": members})) for i in range(3)]
    return [origin, *members, *roots], roots


# --- M2.1: depth > HOP_BOUND for a cached parent --------------------------------


@pytest.mark.parametrize("warm", ["bottom-up", "mid-chain"])
def test_cached_parent_cannot_carry_a_row_past_the_hop_bound(warm):
    rows = chain(HOP_BOUND + 4)
    memories = rows[1:]
    truth = kernel(rows)
    inside, outside = memories[:HOP_BOUND], memories[HOP_BOUND:]
    assert [truth(row).unverified for row in inside] == [False] * HOP_BOUND
    assert [truth(row).unverified for row in outside] == [True] * 4
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        if warm == "bottom-up":
            # Every parent is cached before its child, so the trail never grows
            # past one row and cannot be what refuses the deep rows.
            for row in memories[:-1]:
                settled = guard._settled_inputs("memory", row, frozenset())
                assert (settled is not None) == (not truth(row).unverified)
        else:
            warmed = memories[: HOP_BOUND // 2]
            assert guard.admit_rows("memory", warmed) == warmed
        root = memories[-1]
        assert guard._settled_inputs("memory", root, frozenset()) is None
        assert guard.admit_rows("memory", [root]) == []
        assert shown(guard.effective_row("memory", root)) == kernel_shown(truth(root))
        assert kernel_shown(truth(root))[4] is True
        assert guard.admit_rows("memory", inside) == inside
        assert guard.admit_rows("memory", outside) == []


@pytest.mark.parametrize("warm", [True, False])
def test_cached_parents_cannot_carry_a_row_past_the_node_bound(monkeypatch, warm):
    monkeypatch.setattr(guard_module, "NODE_BOUND", 4)
    left, right = source(1), source(2)
    first = memory(10, {"source_id": left["id"]})
    second = memory(11, {"source_id": right["id"]})
    top = memory(20, {"consolidation": {"cluster_member_ids": [first["id"], second["id"]]}})
    rows = [left, right, first, second, top]
    truth = kernel(rows)
    assert [truth(row).unverified for row in (first, second, top)] == [False, False, True]
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        if warm:
            assert guard._settled_inputs("memory", first, frozenset()) is not None
            assert guard._settled_inputs("memory", second, frozenset()) is not None
        # Five distinct rows in the closure against a bound of four.
        assert guard._settled_inputs("memory", top, frozenset()) is None
        assert guard.admit_rows("memory", [top]) == []
        assert shown(guard.effective_row("memory", top)) == kernel_shown(truth(top))
        assert guard.admit_rows("memory", [first, second]) == [first, second]


# --- M2.3: parent carries_scope in the settled-inputs memo key ------------------


@pytest.mark.parametrize("order", ["scrubbed-first", "global-first"])
def test_memo_does_not_merge_a_scrubbed_source_with_a_global_one(order):
    scrubbed = source(1, metadata={"scrubbed": True})
    plain = source(2)
    from_scrubbed = memory(10, {"source_id": scrubbed["id"], "project_scope": ["P1"]})
    from_plain = memory(11, {"source_id": plain["id"], "project_scope": ["P1"]})
    rows = [scrubbed, plain, from_scrubbed, from_plain]
    truth = kernel(rows)
    # A scrubbed parent carries no scope, so its copy keeps its own. A global
    # parent empties the scope of its copy. The stored labels are equal.
    assert kernel_shown(truth(from_scrubbed))[2] == ("P1",)
    assert kernel_shown(truth(from_plain))[2] == ()
    pair = [from_scrubbed, from_plain] if order == "scrubbed-first" else [from_plain, from_scrubbed]
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        for row in pair:
            assert shown(guard.effective_row("memory", row)) == kernel_shown(truth(row))
        assert replace(guard, projects=("P1",)).admit_rows("memory", pair) == [from_scrubbed]


# --- M2.6: _rank_ceiling is for an unscoped guard of its own store --------------


def native_store():
    origin = {"kind": "source", "id": UUID(int=1), "domain": "project", "sensitivity": "public",
              "metadata_json": {"project_scope": [], "project_floor": []}}
    root = {"kind": "memory", "id": UUID(int=2), "domain": "project", "sensitivity": "public", "status": "active",
            "value": {}, "metadata_json": {"source_id": str(origin["id"]), "project_scope": [], "project_floor": []}}
    return NativeRows([origin, root], [root]), root


@pytest.mark.parametrize("limits", [{"projects": (P1,)}, {"all_of": (P1,)}], ids=["projects", "all-of"])
def test_project_bound_guard_never_takes_the_reduced_rank_proof(limits):
    store, root = native_store()
    with label_read_scope(store):
        counter = LabelGuard(store, active=True, sensitivity_allowed=CEILING)
        assert counter._rank_ceiling() is not None
        assert counter.readable_status_counts("memory") == {"active": 1}
        assert counter._state().rank_origins, "no reduced proof was registered; this test proves nothing"
        bound = LabelGuard(store, active=True, sensitivity_allowed=CEILING, **limits)
        # The bound flag decides before any registered proof is consulted.
        assert bound._rank_ceiling() is None
        # The graph is global, so a guard bound to a project must not read it.
        assert bound.admit_rows("memory", [root]) == []
        assert bound.readable_status_counts("memory") == {}
        assert counter.admit_rows("memory", [root]) == [root]


def test_a_guard_outside_its_store_scope_has_no_rank_ceiling():
    scoped, _root = native_store()
    other, _other_root = native_store()
    with label_read_scope(scoped):
        assert LabelGuard(scoped, active=True, sensitivity_allowed=CEILING)._rank_ceiling() is not None
        assert LabelGuard(other, active=True, sensitivity_allowed=CEILING)._rank_ceiling() is None


# --- M2.6: no admission grant crosses caller filters ----------------------------

# Each pair is (limits that admit the row, limits that refuse it). They differ in one term.
GRANT_CASES = {
    "domains": (dict(domains=("project",)), dict(domains=("health",))),
    "sensitivity": (dict(sensitivity_allowed=("public",)), dict(sensitivity_allowed=("internal",))),
    "projects": (dict(projects=("P1",)), dict(projects=("P2",))),
    "all-of": (dict(projects=("P1",)), dict(projects=("P1",), all_of=("P2",))),
    "all-of-alone": (dict(all_of=("P1",)), dict(all_of=("P2",))),
}


def scoped_copy():
    origin = source(1, scope=("P1",))
    root = memory(10, {"source_id": origin["id"], "project_scope": ["P1"]})
    return [origin, root], root


@pytest.mark.parametrize("first", ["admitting", "refusing"])
@pytest.mark.parametrize("term", sorted(GRANT_CASES))
def test_a_stored_row_admission_stays_with_the_filters_that_earned_it(term, first):
    rows, root = scoped_copy()
    admitting, refusing = GRANT_CASES[term]
    # Alone, outside any scope, each guard gives the kernel's answer for its filters.
    assert LabelGuard(Rows(rows), active=True, **admitting).admit_rows("memory", [root]) == [root]
    assert LabelGuard(Rows(rows), active=True, **refusing).admit_rows("memory", [root]) == []
    store = Rows(rows)
    with label_read_scope(store):
        allow = (LabelGuard(store, active=True, **admitting), [root])
        refuse = (LabelGuard(store, active=True, **refusing), [])
        for guard, expected in ([allow, refuse] if first == "admitting" else [refuse, allow]) * 2:
            assert guard.admit_rows("memory", [root]) == expected


@pytest.mark.parametrize("first", ["admitting", "refusing"])
@pytest.mark.parametrize("term", sorted(GRANT_CASES))
def test_a_shared_source_admission_stays_with_the_filters_that_earned_it(monkeypatch, term, first):
    monkeypatch.setattr(guard_module, "HOP_BOUND", 3)
    rows, roots = shortcut_rows()
    truth = kernel(rows)
    assert [truth(root).unverified for root in roots] == [False] * 3
    admitting, refusing = GRANT_CASES[term]
    store = Rows(rows)
    with label_read_scope(store):
        allow = LabelGuard(store, active=True, **admitting)
        refuse = LabelGuard(store, active=True, **refusing)
        assert allow._settled_inputs("memory", roots[0], frozenset()) is None, "the direct settle must refuse these rows"
        if first == "admitting":
            # The second root of one signature stores a grant that the third may reuse.
            assert allow.admit_rows("memory", roots[:2]) == roots[:2]
            assert allow._state().source_admission, "no shared grant was stored; this test proves nothing"
            assert refuse.admit_rows("memory", roots[2:]) == []
        else:
            assert refuse.admit_rows("memory", roots[:2]) == []
            assert refuse._state().source_admission, "no shared grant was stored; this test proves nothing"
            assert allow.admit_rows("memory", roots[2:]) == roots[2:]


# --- M2.7: the request caches belong to the store that opened the scope ---------


def twin_stores():
    """Two tenants that hold the same ids; one source is hidden from a public ceiling in the second."""

    def rows(sensitivity):
        origin = source(1, sensitivity=sensitivity)
        return [origin, memory(10, {"source_id": origin["id"]})]

    first, second = rows("public"), rows("confidential")
    return Rows(first), Rows(second), first, second


def test_request_row_cache_belongs_to_the_store_that_opened_the_scope():
    first, second, _first_rows, _second_rows = twin_stores()
    assert request_row_cache(first, "namespace") is None
    with label_read_scope(first):
        cache = request_row_cache(first, "namespace")
        assert cache == {}
        cache["row"] = "tenant one"
        assert request_row_cache(second, "namespace") is None
        assert request_row_cache(first, "namespace") is cache
    assert request_row_cache(first, "namespace") is None


def test_guard_state_belongs_to_the_store_that_opened_the_scope():
    first, second, first_rows, second_rows = twin_stores()
    with label_read_scope(first):
        mine = LabelGuard(first, active=True, sensitivity_allowed=("public",))
        theirs = LabelGuard(second, active=True, sensitivity_allowed=("public",))
        assert mine.admit_rows("memory", [first_rows[1]]) == [first_rows[1]]
        assert theirs._state() is not mine._state()
        # Same ids, different labels: the other tenant's source is hidden from this ceiling.
        assert theirs.admit_rows("memory", [second_rows[1]]) == []
        assert shown(theirs.effective_row("memory", second_rows[1]))[1] == "confidential"
        assert theirs._state().nodes[("source", uid(1))][0]["sensitivity"] == "confidential"
        assert mine._state().nodes[("source", uid(1))][0]["sensitivity"] == "public"


def test_a_guard_outside_its_store_scope_leaves_the_scope_identity_map_alone():
    first, second, _first_rows, second_rows = twin_stores()
    with label_read_scope(first):
        scope_state = LabelGuard(first, active=True)._state()
        theirs = LabelGuard(second, active=True, sensitivity_allowed=("public",))
        assert theirs.admit_rows("memory", [second_rows[1]]) == []
        assert not scope_state.row_keys
        assert not scope_state.labels and not scope_state.nodes


def test_a_second_store_gets_its_own_scope_and_its_own_invalidation():
    first, second, _first_rows, second_rows = twin_stores()
    with label_read_scope(first):
        with label_read_scope(second):
            theirs = LabelGuard(second, active=True, sensitivity_allowed=("public",))
            assert theirs.admit_rows("memory", [second_rows[1]]) == []
            second_rows[0]["sensitivity"] = "public"
            invalidate_read_labels(second)
            assert theirs.admit_rows("memory", [second_rows[1]]) == [second_rows[1]]


def test_two_sqlite_stores_under_one_scope_never_share_cached_rows(tmp_path):
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    users = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
    paths = [tmp_path / "tenant-one.db", tmp_path / "tenant-two.db"]
    for path, user in zip(paths, users, strict=True):
        bootstrap_database(path, user_id=user, user_email="synthetic@example.invalid")
    with sqlite_user_connection(paths[0], users[0]) as first_conn, sqlite_user_connection(paths[1], users[1]) as second_conn:
        first, second = SQLiteVNextStore(first_conn, users[0]), SQLiteVNextStore(second_conn, users[1])
        private = first.create_memory({"memory_key": "tenant-one-note", "canonical_text": "tenant one observation",
                                       "status": "active", "domain": "project", "sensitivity": "public",
                                       "metadata_json": {}})
        with label_read_scope(first):
            assert VNextRetrievalService(first)._memories_by_ids([private["id"]], effective=False)
            assert request_row_cache(first, "retrieval_memories")
            assert request_row_cache(second, "retrieval_memories") is None
            assert VNextRetrievalService(second)._memories_by_ids([private["id"]], effective=False) == {}
            assert second.read_label_rows("memory", [private["id"]]) == []


def test_a_native_label_settled_for_one_store_is_never_reused_for_another():
    first, first_root = native_store()
    other_source = {**first.rows[0], "sensitivity": "confidential"}
    other_root = {**first_root}
    second = NativeRows([other_source, other_root], [other_root])
    # A domain filter keeps the count off the reduced rank proof, so it records full labels.
    limits = {"sensitivity_allowed": ("public",), "domains": ("project",)}
    with label_read_scope(first):
        assert LabelGuard(first, active=True, **limits).readable_status_counts("memory") == {"active": 1}
        assert LabelGuard(first, active=True)._state().native_labels, "no native label was recorded"
        # Same row, same stored label, but this store's source is hidden from the caller.
        assert LabelGuard(second, active=True, **limits).admit_rows("memory", [other_root]) == []


def test_rank_batch_refuses_a_row_that_differs_from_the_loaded_projection():
    store, root = native_store()
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=CEILING)
        state = guard._state()
        guard._prefetch_inputs("memory", [root])
        state.nodes[("memory", str(root["id"]))] = [{**root, "sensitivity": "confidential"}]
        assert guard._settle_native_rank_batch("memory", [root]) is False
        assert not state.rank_origins and not state.rank_rows


# --- minor 5: the bulk count never runs on a graph the per-root path would walk differently ---


def per_root_admission(rows, roots, **limits):
    return LabelGuard(Rows(rows), active=True, **limits).admit_rows("memory", roots)


def test_bulk_count_leaves_implicit_weekly_artifacts_to_the_per_root_path(monkeypatch):
    monkeypatch.setattr(guard_module, "HOP_BOUND", 2)
    empty = {"project_scope": [], "project_floor": []}
    origin = {"kind": "source", "id": UUID(int=1), "domain": "project", "sensitivity": "public",
              "metadata_json": dict(empty)}
    artifact = {"kind": "artifact", "id": UUID(int=2), "artifact_type": "weekly_synthesis", "domain": "project",
                "sensitivity": "public", "metadata_json": {
                    "candidate_memory_ids": [uid(3)], "input_summary": {"source_ids": [uid(1)]}, **empty}}
    candidate = {"kind": "memory", "id": UUID(int=3), "domain": "project", "sensitivity": "public",
                 "status": "active", "value": {}, "metadata_json": {
                     "discovered_by": "vnext_weekly_synthesis", "source_artifact_id": uid(2), **empty}}
    # Three hops from the source by the recorded inputs, one fewer by the weekly rule.
    root = {"kind": "memory", "id": UUID(int=4), "domain": "project", "sensitivity": "public",
            "status": "active", "value": {}, "metadata_json": {
                "consolidation": {"cluster_member_ids": [uid(3)]}, **empty}}
    rows, roots = [origin, artifact, candidate, root], [candidate, root]
    limits = {"sensitivity_allowed": ("public",)}
    assert per_root_admission(rows, roots, **limits) == [candidate]
    store = NativeRows(rows, roots)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, **limits)
        assert guard.readable_status_counts("memory") == {"active": 1}
        assert guard.admit_rows("memory", roots) == [candidate]


def test_bulk_count_leaves_beliefs_to_the_per_root_path(monkeypatch):
    monkeypatch.setattr(guard_module, "HOP_BOUND", 3)
    empty = {"project_scope": [], "project_floor": []}

    def memory_row(number, metadata, domain="project"):
        return {"kind": "memory", "id": UUID(int=number), "domain": domain, "sensitivity": "public",
                "status": "active", "value": {}, "metadata_json": {**metadata, **empty}}

    origin = {"kind": "source", "id": UUID(int=2), "domain": "project", "sensitivity": "public",
              "metadata_json": dict(empty)}
    belief = {"kind": "belief", "id": UUID(int=9), "memory_id": uid(7), "domain": "project",
              "sensitivity": "public", "metadata_json": {}}
    # 4 names a belief whose backing memory 7 consolidates 4, 5 and 6: a loop through the belief.
    four = memory_row(4, with_derived_from({"consolidation": {"cluster_member_ids": []}},
                                           {"sources": [origin], "beliefs": [belief]}))
    five = memory_row(5, {"consolidation": {"cluster_member_ids": [uid(4)]}})
    six = memory_row(6, {"consolidation": {"cluster_member_ids": [uid(5)]}})
    seven = memory_row(7, {"consolidation": {"cluster_member_ids": [uid(6), uid(5), uid(4)]}}, domain="health")
    rows, roots = [origin, belief, four, five, six, seven], [four, five, six, seven]
    limits = {"sensitivity_allowed": ("public", "private")}
    expected = per_root_admission(rows, roots, **limits)
    assert 0 < len(expected) < len(roots), "the per-root answer must refuse some rows; this test proves nothing"
    store = NativeRows(rows, roots)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, **limits)
        assert guard.readable_status_counts("memory") == {"active": len(expected)}
        assert guard.admit_rows("memory", roots) == expected


# --- M2.8: invalidation clears every request cache -----------------------------


def test_invalidation_empties_every_request_cache_field():
    store = Rows([])
    with label_read_scope(store):
        state = LabelGuard(store, active=True)._state()
        fields = dataclasses.fields(_RequestLabels)
        assert all(isinstance(getattr(state, item.name), dict) for item in fields)
        for item in fields:
            getattr(state, item.name)[("probe", item.name)] = object()
        assert all(getattr(state, item.name) for item in fields)
        invalidate_read_labels(store)
        assert [item.name for item in fields if getattr(state, item.name)] == []


def test_a_cached_status_count_is_dropped_by_invalidation():
    origin = source(1)
    root = memory(10, {"source_id": origin["id"]})
    store = CountedRows([origin, root], [root])
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == {"active": 1}
        origin["sensitivity"] = "confidential"
        invalidate_read_labels(store)
        assert guard.readable_status_counts("memory") == {}


def test_a_shared_dependency_label_is_dropped_by_invalidation(monkeypatch):
    monkeypatch.setattr(guard_module, "HOP_BOUND", 3)
    rows, roots = shortcut_rows()
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.effective_row("memory", roots[0])["sensitivity"] == "public"
        assert guard._state().dependency_labels, "no shared label was stored; this test proves nothing"
        rows[0]["sensitivity"] = "confidential"
        invalidate_read_labels(store)
        # Another root with the same signature must be settled again, not served the old label.
        assert guard.effective_row("memory", roots[1])["sensitivity"] == "confidential"


def test_a_shared_admission_is_dropped_by_invalidation(monkeypatch):
    monkeypatch.setattr(guard_module, "HOP_BOUND", 3)
    rows, roots = shortcut_rows()
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.admit_rows("memory", roots[:2]) == roots[:2]
        assert guard._state().source_admission, "no shared grant was stored; this test proves nothing"
        rows[0]["sensitivity"] = "confidential"
        invalidate_read_labels(store)
        assert guard.admit_rows("memory", roots) == []


def test_a_cached_signature_is_dropped_by_invalidation_for_a_reused_row_identity():
    safe, secret = source(1), source(2, sensitivity="confidential")
    row = memory(10, {"source_id": safe["id"]})
    store = Rows([safe, secret])
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        assert guard.effective_row("memory", row)["sensitivity"] == "public"
        # Once the identity map is cleared, a new row can take the address of one
        # that was released, and so get its key. The in-place edit stands for that.
        row["metadata_json"]["source_id"] = secret["id"]
        invalidate_read_labels(store)
        assert guard.effective_row("memory", row)["sensitivity"] == "confidential"


# --- M2.9: len(trail) > HOP_BOUND on the cold path ------------------------------


def test_a_very_long_chain_requested_alone_is_refused_without_recursing():
    length = max(1500, sys.getrecursionlimit() + 500)
    rows = chain(length)
    top = rows[-1]
    truth = kernel(rows)
    assert truth(top).unverified
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard._settled_inputs("memory", top, frozenset()) is None
        assert guard.admit_rows("memory", [top]) == []
        assert shown(guard.effective_row("memory", top)) == kernel_shown(truth(top))

