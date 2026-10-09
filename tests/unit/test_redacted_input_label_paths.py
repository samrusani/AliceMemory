"""Every path that decides a derived row's label agrees about a row built from a redacted row.

A derived row that recorded a redacted row as an input kept the words it copied from it, so it is unverified, and so is every
row built from it. Four paths decide a label: the bulk kernel, the per-origin guard (with the memos it shares inside one
request), the reduced rank proof with the bulk count, and the SQL prefilters. Random graphs put a redacted row at every
depth, and an oracle that states the sentence in plain recursion judges all four.

Mutations that this file must fail (the manifest replays each one):

* the kernel stops reading a redacted input (``redacted_input`` never set);
* the per-origin guard settles a row from a redacted parent (the parent check in ``_settled_inputs``);
* the reduced rank proof stops counting a row with a redacted input at the top rank;
* the redacted flag leaves the dependency signature, so a memo made for a redacted row serves a row that is not;
* the SQL prefilter rejects a row it should read (a redacted input is taken for a hidden one when it is not).
"""
from __future__ import annotations

from types import SimpleNamespace
import random
from uuid import UUID

import pytest

from alicebot_api import vnext_label_guard as guards
from alicebot_api.vnext_derived_labels import (
    SENSITIVITY_RANK,
    SettledLabel,
    identifier,
    is_derived,
    settle_labels,
    settle_verified_inputs,
)
from alicebot_api.vnext_label_sql import hidden_memory_input_sql
from tests.unit.redacted_graph_support import CEILINGS, REGULATED, USER, Graph, random_graph

SEEDS = range(60)


class GraphStore:
    """The reads a native (UUID keyed) store gives the guard, over the rows of one graph."""

    label_count_canonical_unique_ids = True

    def __init__(self, graph: Graph) -> None:
        self.rows = [{**node.row, "kind": node.kind} for node in graph.nodes]
        self.conn = SimpleNamespace()

    def read_label_rows(self, kind, ids):
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in ids]

    def count_original_label_statuses(self, kind, *, domains=(), sensitivity_allowed=(), **_kwargs):
        counts: dict[str, int] = {}
        for row in self.rows:
            if row["kind"] != kind or is_derived(kind, row):
                continue
            if sensitivity_allowed and row["sensitivity"] not in sensitivity_allowed:
                continue
            counts[str(row.get("status", "active"))] = counts.get(str(row.get("status", "active")), 0) + 1
        return counts

    def iter_label_rows(self, kind, derived_only=False, **_kwargs):
        rows = [row for row in self.rows if row["kind"] == kind and (not derived_only or is_derived(kind, row))]
        yield [dict(row) for row in rows]

    def lock_label_writes(self, **_kwargs):
        pass


def first_memory_input_is_redacted(graph: Graph) -> set[str]:
    """Memories that are not redacted and list a redacted memory first: the case the SQL hint can prove."""
    found = set()
    for node in graph.nodes:
        if node.kind != "memory" or node.redacted:
            continue
        meta = node.row["metadata_json"]
        members = (meta.get("consolidation") or {}).get("cluster_member_ids") or (meta.get("derived_from") or {}).get("memories") or []
        if not members:
            continue
        by_id = {str(item.row["id"]): item for item in graph.nodes}
        first = by_id.get(members[0])
        if first is not None and first.kind == "memory" and first.redacted:
            found.add(identifier(node.row["id"]))
    return found


def _nodes(graph: Graph) -> list[dict]:
    return [{**node.row, "kind": node.kind, "user_id": USER} for node in graph.nodes]


def _expected(graph: Graph):
    contained, ranks = graph.contained(), graph.ranks()
    return contained, ranks


@pytest.mark.parametrize("seed", SEEDS)
def test_bulk_kernel_marks_exactly_the_rows_built_from_a_redacted_row(seed):
    graph = random_graph(seed)
    contained, ranks = _expected(graph)
    settled = settle_labels(_nodes(graph), on_cycle="unverified", max_hops=guards.HOP_BOUND, max_nodes=guards.NODE_BOUND)
    for node, label in zip(graph.nodes, settled.rows, strict=True):
        assert label.unverified is contained[node.key], (seed, node.kind, node.key)
        if contained[node.key]:
            assert label.reason in {"input_redacted", "dependency_unverified"}
            if any(item.redacted for item in node.inputs):
                assert label.reason == "input_redacted"
            assert label.carries_scope is False
        else:
            assert SENSITIVITY_RANK[label.sensitivity] == ranks[node.key], (seed, node.kind)


def test_the_graphs_reach_every_case():
    """The seeds put a redacted row under a direct input, under a chain, and leave rows clear."""
    direct = chain = clear = redacted_derived = 0
    for seed in SEEDS:
        graph = random_graph(seed)
        contained = graph.contained()
        for node in graph.nodes:
            if contained[node.key]:
                if any(item.redacted for item in node.inputs):
                    direct += 1
                else:
                    chain += 1
            elif not node.redacted and node.inputs:
                clear += 1
        redacted_derived += sum(1 for node in graph.nodes if node.redacted and node.row["metadata_json"].get("redacted") and node.row.get("status") == "archived" and node.kind == "memory")
    assert direct > 50 and chain > 50 and clear > 50 and redacted_derived > 50, (direct, chain, clear, redacted_derived)


def test_the_seeds_put_a_redacted_row_at_every_depth_and_a_contained_row_above_it():
    """A redacted row stands as an original and as a derived row redacted later, at depth 0, 1, 2 and 3 or more."""
    redacted_depths: set[int] = set()
    contained_above: set[int] = set()
    for seed in SEEDS:
        graph = random_graph(seed)
        contained = graph.contained()
        for node in graph.nodes:
            if node.redacted:
                redacted_depths.add(min(node.depth, 3))
            if contained[node.key]:
                contained_above.add(min(node.depth, 4))
    assert redacted_depths == {0, 1, 2, 3}, redacted_depths
    assert {1, 2, 3, 4} <= contained_above, contained_above


def _label_of(effective):
    return bool(effective["unverified"]), SENSITIVITY_RANK[str(effective["sensitivity"])]


@pytest.mark.parametrize("seed", SEEDS)
def test_per_origin_guard_gives_the_bulk_kernels_label_in_a_shared_scope_and_alone(seed):
    graph = random_graph(seed)
    contained, ranks = _expected(graph)
    derived = [node for node in graph.nodes if is_derived(node.kind, node.row)]
    order = list(derived)
    random.Random(seed).shuffle(order)
    store = GraphStore(graph)
    shared: dict = {}
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=CEILINGS[-1])
        for node in order:
            shared[node.key] = _label_of(guard.effective_row(node.kind, dict(next(r for r in store.rows if r["id"] == node.row["id"]))))
    for node in derived:
        expected = (contained[node.key], REGULATED if contained[node.key] else ranks[node.key])
        assert shared[node.key] == expected, (seed, node.kind, "shared scope", shared[node.key], expected)
        with guards.label_read_scope(store):
            alone = guards.LabelGuard(store, active=True, sensitivity_allowed=CEILINGS[-1])
            row = dict(next(r for r in store.rows if r["id"] == node.row["id"]))
            assert _label_of(alone.effective_row(node.kind, row)) == expected, (seed, node.kind, "alone")


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("domains", [(), ("project",)], ids=["rank-proof", "bulk-kernel"])
def test_counts_and_admission_follow_the_oracle_on_both_bulk_paths(seed, domains):
    graph = random_graph(seed)
    for ceiling in CEILINGS:
        hidden = graph.hidden_from(ceiling)
        for kind in ("memory", "artifact"):
            wanted = [node for node in graph.nodes if node.kind == kind and node.key not in hidden]
            expected_counts: dict[str, int] = {}
            for node in wanted:
                status = str(node.row.get("status", "active"))
                expected_counts[status] = expected_counts.get(status, 0) + 1
            store = GraphStore(graph)
            with guards.label_read_scope(store):
                guard = guards.LabelGuard(store, active=True, sensitivity_allowed=ceiling, domains=domains)
                counts = guard.readable_status_counts(kind)
                if kind == "artifact":
                    # artifacts carry no status here, so only the total is comparable
                    assert sum(counts.values()) == len(wanted), (seed, ceiling, kind, counts)
                else:
                    assert counts == expected_counts, (seed, ceiling, kind, counts, expected_counts)
            store = GraphStore(graph)
            with guards.label_read_scope(store):
                guard = guards.LabelGuard(store, active=True, sensitivity_allowed=ceiling, domains=domains)
                rows = [dict(row) for row in store.rows if row["kind"] == kind]
                admitted = {str(row["id"]) for row in guard.admit_rows(kind, rows)}
                assert admitted == {str(node.row["id"]) for node in wanted}, (seed, ceiling, kind)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_reduced_proof_never_admits_what_the_kernel_hides(seed):
    """With the whole population loaded the reduced proof is used, and its rank for a contained row is the top rank."""
    graph = random_graph(seed)
    contained, ranks = _expected(graph)
    store = GraphStore(graph)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=CEILINGS[-1])
        guard.readable_status_counts("memory")
        state = guard._state()
        for (kind, node_id), (_row, rank) in state.rank_origins.items():
            assert rank == (REGULATED if contained[(kind, node_id)] else ranks[(kind, node_id)]), (seed, kind)
        for key in state.labels.values():
            assert isinstance(key, SettledLabel)


def test_a_belief_is_read_only_when_its_backing_memory_is_not_redacted():
    """A belief keeps the claim it copied from its memory: redacting the memory leaves it, so the belief is contained with it."""
    plain = {"kind": "memory", "id": UUID(int=0xB0001), "user_id": USER, "domain": "project", "sensitivity": "public", "status": "active",
             "value": {}, "metadata_json": {"project_scope": [], "project_floor": []}}
    redacted = {**plain, "id": UUID(int=0xB0002), "status": "archived", "metadata_json": {"redacted": True, "redacted_at": "2026-10-09T00:00:00Z"}}
    beliefs = [{"id": UUID(int=0xB0010 + index), "memory_id": str(row["id"]), "claim": "claim"} for index, row in enumerate((plain, redacted))]
    store = SimpleNamespace(rows=[plain, redacted], conn=SimpleNamespace(), label_count_canonical_unique_ids=True)
    store.read_label_rows = lambda kind, ids: [dict(row) for row in store.rows if row["kind"] == kind and identifier(row["id"]) in ids]
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=CEILINGS[-1])
        assert [row["id"] for row in guard.admit_beliefs(beliefs)] == [beliefs[0]["id"]]
        unlimited = guards.LabelGuard(store, active=False)
        assert [row["id"] for row in unlimited.admit_beliefs(beliefs)] == [row["id"] for row in beliefs]


def test_the_seeds_give_the_sql_partition_rows_to_reject():
    assert sum(len(first_memory_input_is_redacted(random_graph(seed, artifacts=False))) for seed in range(25)) > 15


def test_the_redacted_flag_is_part_of_the_dependency_signature_so_no_memo_crosses_it():
    """Two originals that differ only in the flag must not share a memo entry, in any order."""
    from alicebot_api.vnext_derived_labels import dependency_label_signature

    plain = {"id": "00000000-0000-0000-0000-000000000001", "user_id": USER, "domain": "project", "sensitivity": "public",
             "metadata_json": {"project_scope": [], "project_floor": []}}
    redacted = {**plain, "id": "00000000-0000-0000-0000-000000000002", "metadata_json": {"redacted": True, "project_scope": [], "project_floor": []}}
    assert dependency_label_signature("memory", plain) != dependency_label_signature("memory", redacted)


def test_a_verified_label_cannot_be_settled_from_a_redacted_parent():
    plain = {"id": "00000000-0000-0000-0000-000000000001", "user_id": USER, "domain": "project", "sensitivity": "public",
             "metadata_json": {"project_scope": [], "project_floor": []}}
    redacted = {**plain, "id": "00000000-0000-0000-0000-000000000002", "metadata_json": {"redacted": True}}
    child = {**plain, "id": "00000000-0000-0000-0000-000000000003",
             "metadata_json": {"consolidation": {"cluster_member_ids": [redacted["id"]]}, "project_scope": [], "project_floor": [],
                               "derived_from": {"v": 1, "sources": [], "memories": [redacted["id"]], "open_loops": [],
                                                "artifacts": [], "beliefs": [],
                                                "counts": {"sources": 0, "memories": 1, "open_loops": 0, "artifacts": 0, "beliefs": 0}}}}
    result = settle_labels([{**redacted, "kind": "memory"}, {**child, "kind": "memory"}], on_cycle="unverified")
    parent_label, child_label = result.rows
    assert parent_label.redacted and not parent_label.unverified
    assert child_label.unverified and child_label.reason == "input_redacted"
    with pytest.raises(ValueError):
        settle_verified_inputs("memory", {**child, "user_id": USER}, [parent_label])


@pytest.mark.parametrize("seed", range(25))
def test_a_sqlite_store_gives_the_oracle_s_answer_through_the_guard_and_its_sql_prefilter(tmp_path, seed):
    """The real SQLite store, its readers and its SQL partition agree with the oracle on the same random graph.

    The SQL partition may leave a hidden row to the kernel. It must never reject a row the kernel reads, and it must reject a
    row whose first listed memory is redacted, before the limit applies. The guard reads a graph edge between each memory and the
    next: it keeps the edge when neither end is hidden from the ceiling or redacted.
    """
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_label_writes import without_insert_floor

    user = "11111111-1111-4111-8111-111111111111"
    graph = random_graph(seed, artifacts=False)
    path = tmp_path / "paths.db"
    bootstrap_database(path, user_id=user, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, user) as conn, without_insert_floor():
        store = SQLiteVNextStore(conn, user)
        for node in graph.nodes:
            if node.kind == "source":
                store.create_source({"id": str(node.row["id"]), "source_type": "note", "title": "s", "content_hash": str(node.row["id"]),
                                     "domain": "project", "sensitivity": node.row["sensitivity"], "metadata_json": node.row["metadata_json"]})
            else:
                store.create_memory({"id": str(node.row["id"]), "memory_key": str(node.row["id"]), "canonical_text": "text", "status": "active",
                                     "domain": "project", "sensitivity": node.row["sensitivity"],
                                     "metadata_json": node.row["metadata_json"], "value": node.row.get("value") or {}})
        sure = first_memory_input_is_redacted(graph)
        # One edge joins each memory to the next, so every memory stands at the end of two edges.
        memories = [node for node in graph.nodes if node.kind == "memory"]
        edges = {}
        for index, node in enumerate(memories if len(memories) > 1 else []):
            other = memories[(index + 1) % len(memories)]
            made = store.create_graph_edge(
                {"from_type": "memory", "from_id": str(node.row["id"]), "to_type": "memory", "to_id": str(other.row["id"]),
                 "edge_type": "mentions", "confidence": 0.5, "explanation": "joins two memories", "created_by": "test"}
            )
            edges[str(made["id"])] = (node, other)
        for ceiling in CEILINGS:
            hidden = graph.hidden_from(ceiling)
            with guards.label_read_scope(store):
                guard = guards.LabelGuard.for_filters(store, (), ceiling)
                admitted_edges = {row["id"] for row in guard.admit_edges(store.list_edges())}
            # An edge is read when neither memory at its ends is hidden from the ceiling or redacted: it keeps both titles.
            wanted_edges = {
                edge_id for edge_id, ends in edges.items()
                if not any(end.key in hidden or end.redacted for end in ends)
            }
            assert admitted_edges == wanted_edges, (seed, ceiling, admitted_edges ^ wanted_edges)
            hidden_memories = {identifier(key[1]) for key in hidden if key[0] == "memory"}
            sql = hidden_memory_input_sql(ceiling, sqlite=True)
            rejected = {identifier(row["id"]) for row in conn.execute(f"SELECT m.id FROM memories m WHERE NOT ({sql})").fetchall()}  # nosec B608 - closed kernel constants
            assert rejected <= hidden_memories, (seed, ceiling, rejected - hidden_memories)
            assert sure <= rejected, (seed, ceiling, sure - rejected)
            wanted = [node for node in graph.nodes if node.kind == "memory" and node.key not in hidden]
            for domains in ((), ("project",)):
                with guards.label_read_scope(store):
                    guard = guards.LabelGuard.for_filters(store, domains, ceiling)
                    expected_counts = {"active": len(wanted)} if wanted else {}
                    assert guard.readable_status_counts("memory") == expected_counts, (seed, ceiling, domains)
                with guards.label_read_scope(store):
                    guard = guards.LabelGuard.for_filters(store, domains, ceiling)
                    rows = [row for batch in store.iter_label_rows("memory") for row in batch]
                    admitted = {identifier(row["id"]) for row in guard.admit_rows("memory", rows)}
                    assert admitted == {identifier(node.row["id"]) for node in wanted}, (seed, ceiling, domains)
