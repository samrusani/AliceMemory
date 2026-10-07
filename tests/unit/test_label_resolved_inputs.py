"""Dependency-order reuse agrees with the canonical kernel, including fallbacks."""
from copy import deepcopy
from dataclasses import replace
import json
import random
from uuid import UUID

import pytest

from alicebot_api.vnext_derived_labels import identifier, settle_labels, with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard, invalidate_read_labels, label_read_scope


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def read_label_rows(self, kind, ids):
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in ids]


def labels(row):
    meta = row["metadata_json"]
    return row["domain"], row["sensitivity"], tuple(meta["project_scope"]), tuple(meta["project_floor"]), row["unverified"]


def expected(label):
    return label.domain, "regulated" if label.unverified else label.sensitivity, (() if label.unverified else label.project_scope), (() if label.unverified else label.project_floor), label.unverified


def test_random_mixed_deep_inputs_match_full_kernel_and_keep_distinct_domains():
    rng = random.Random(20261007)
    rows = [{"kind": "source", "id": str(UUID(int=i + 1)), "domain": ("health", "legal", "project")[i % 3],
             "sensitivity": ("public", "confidential")[i % 2], "metadata_json": {"project_scope": ["P" + str(i % 3)]}}
            for i in range(18)]
    for i in range(180):
        parents = rng.sample(rows, min(len(rows), rng.randint(1, 3)))
        by_kind = {kind + "s": [row for row in parents if row["kind"] == kind] for kind in ("source", "memory")}
        meta = with_derived_from({"project_scope": ["P0", "P1", "P2"], "observation_index": i}, by_kind)
        if i % 4 == 1:
            meta["workflow"] = "project_auto_update"
        elif i % 4 == 2:
            meta["candidate_kind"] = "memory_consolidation"
        elif i % 4 == 3:
            meta["discovered_by"] = "vnext_weekly_synthesis"
        rows.append({"kind": "memory", "id": str(UUID(int=100 + i)), "domain": "project", "sensitivity": "public", "metadata_json": meta})
    canonical = settle_labels([{**row, "user_id": "label-guard"} for row in rows], on_cycle="unverified", max_nodes=5000, max_hops=32)
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        guard.admit_rows("memory", rows[18:])
        for row, label in zip(rows[18:], canonical.rows[18:], strict=True):
            assert labels(guard.effective_row("memory", row)) == expected(label)
        # Caller admission still uses its own ceiling/domain after settlement.
        public = replace(guard, sensitivity_allowed=("public",), domains=("project",))
        visible = public.admit_rows("memory", rows[18:])
        assert all(labels(guard.effective_row("memory", row))[0:2] == ("project", "public") for row in visible)


def test_parent_label_multiplicity_changes_restricted_domain_selection():
    sources = [{"kind": "source", "id": str(UUID(int=i + 1)), "domain": domain, "sensitivity": "public", "metadata_json": {}}
               for i, domain in enumerate(("health", "health", "legal", "legal"))]
    roots = [{"kind": "memory", "id": str(UUID(int=100 + i)), "domain": "project", "sensitivity": "public",
              "metadata_json": with_derived_from({}, {"sources": [sources[j] for j in indexes]})}
             for i, indexes in enumerate(((0, 1, 2), (0, 2, 3)))]
    store = Rows(sources)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        assert [guard.effective_row("memory", root)["domain"] for root in roots] == ["health", "legal"]


def test_dependency_frontier_batches_many_independent_origins():
    sources = [{"kind": "source", "id": str(UUID(int=i + 1)), "domain": "project", "sensitivity": "confidential" if i % 2 else "public", "metadata_json": {}}
               for i in range(30)]
    parents = [{"kind": "memory", "id": str(UUID(int=i + 100)), "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": source["id"]}}
               for i, source in enumerate(sources)]
    roots = [{"kind": "memory", "id": str(UUID(int=i + 200)), "domain": "project", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [parents[i % 30]["id"]]}}}
             for i in range(60)]

    class CountedRows(Rows):
        calls = 0

        def read_label_rows(self, kind, ids):
            self.calls += 1
            return super().read_label_rows(kind, ids)

    store = CountedRows(sources + parents)
    kept = LabelGuard(store, active=True, sensitivity_allowed=("public",)).admit_rows("memory", roots)
    assert {row["id"] for row in kept} == {row["id"] for row in roots[::2]}
    assert store.calls <= 2


def test_partial_prefetch_keeps_per_origin_floors_and_refreshes_after_writes(monkeypatch):
    # The combined frontier exceeds this bound; every individual graph fits.
    monkeypatch.setattr("alicebot_api.vnext_label_guard.NODE_BOUND", 3)
    sources = [{"kind": "source", "id": str(UUID(int=i + 1)), "domain": "project",
                "sensitivity": "confidential" if i == 1 else "public", "metadata_json": {}}
               for i in range(3)]
    parents = [{"kind": "memory", "id": str(UUID(int=i + 100)), "domain": "project", "sensitivity": "public",
                "metadata_json": {"source_id": source["id"]}} for i, source in enumerate(sources)]
    roots = [{"kind": "memory", "id": str(UUID(int=i + 200)), "domain": "project", "sensitivity": "public",
              "metadata_json": {"consolidation": {"cluster_member_ids": [parent["id"]]}}}
             for i, parent in enumerate(parents)]
    store = Rows(sources + parents)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        for _ in range(2):
            assert [row["id"] for row in guard.admit_rows("memory", roots)] == [roots[0]["id"], roots[2]["id"]]
        sources[0]["sensitivity"] = "confidential"
        invalidate_read_labels(store)
        assert [row["id"] for row in guard.admit_rows("memory", roots)] == [roots[2]["id"]]


def test_repeated_admission_keeps_caller_filters_projections_and_write_refresh():
    source = {"kind": "source", "id": "source", "domain": "project", "sensitivity": "public",
              "metadata_json": {"project_scope": ["P1"]}}
    root = {"kind": "memory", "id": "root", "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": "source", "project_scope": ["P1"]}}
    store = Rows([source])
    with label_read_scope(store):
        public = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        for _ in range(2):
            assert public.admit_rows("memory", [root]) == [root]
            assert replace(public, domains=("health",)).admit_rows("memory", [root]) == []
            assert replace(public, projects=("P2",), all_of=("P2",)).admit_rows("memory", [root]) == []
            assert replace(public, projects=("P1",), all_of=("P1",)).admit_rows("memory", [root]) == [root]
        # The same ID with a different stored label is a different projection.
        assert public.admit_rows("memory", [{**root, "sensitivity": "confidential"}]) == []
        source["sensitivity"] = "confidential"
        invalidate_read_labels(store)
        assert public.admit_rows("memory", [root]) == []
        assert replace(public, sensitivity_allowed=("public", "confidential")).admit_rows("memory", [root]) == [root]


@pytest.mark.parametrize("variant", ["alias", "cycle", "missing", "malformed", "hop-bound", "node-bound", "implicit-weekly", "implicit-weekly-json"])
def test_unproved_inputs_keep_the_complete_graph_fallback(monkeypatch, variant):
    import alicebot_api.vnext_label_guard as module
    source = {"kind": "source", "id": "source", "domain": "health", "sensitivity": "confidential", "metadata_json": {}}
    parent = {"kind": "memory", "id": "parent", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": "source"}}
    root = {"kind": "memory", "id": "root", "domain": "project", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": ["parent"]}}}
    rows = [source, parent]
    if variant == "alias":
        source["id"] = str(UUID(int=1))
        parent["metadata_json"]["source_id"] = source["id"]
        rows.append({**deepcopy(source), "id": "urn:uuid:" + source["id"]})
    elif variant == "cycle":
        parent["metadata_json"] = {"consolidation": {"cluster_member_ids": ["root"]}}
        rows.append(root)
    elif variant == "missing":
        rows = []
    elif variant == "malformed":
        root["metadata_json"]["candidate_kind"] = []
    elif variant == "hop-bound":
        monkeypatch.setattr(module, "HOP_BOUND", 1)
    elif variant == "node-bound":
        monkeypatch.setattr(module, "NODE_BOUND", 2)
    else:
        artifact = {"kind": "artifact", "id": "artifact", "artifact_type": "weekly_synthesis", "domain": "project", "sensitivity": "public",
                    "metadata_json": {"candidate_memory_ids": ["root"], "input_summary": {"source_ids": ["source"]}}}
        root["metadata_json"] = {"discovered_by": "vnext_weekly_synthesis", "source_artifact_id": "artifact"}
        rows = [source, artifact]
        if variant == "implicit-weekly-json":
            artifact["metadata_json"] = json.dumps(artifact["metadata_json"])
    store = Rows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        if variant in ("hop-bound", "node-bound"):
            assert guard._settled_inputs("memory", parent, frozenset()) is not None
        assert guard._settled_inputs("memory", root, frozenset()) is None
        actual = guard.effective_row("memory", root)
    # Same collector, same canonical kernel bounds, with optimization disabled.
    monkeypatch.setattr(module.LabelGuard, "_settled_inputs", lambda *args: None)
    assert labels(actual) == labels(LabelGuard(store, active=True).effective_row("memory", root))


@pytest.mark.parametrize("variant", ["mixed", "deep", "missing", "malformed", "cycle", "alias", "implicit-weekly"])
def test_native_bulk_count_matches_independent_root_admission(variant):
    sources = [{"kind": "source", "id": UUID(int=i + 1), "domain": "project",
                "sensitivity": "confidential" if i % 2 else "public", "metadata_json": {"project_scope": ["P1"]}}
               for i in range(8)]
    roots = []
    for i in range(32):
        parents = roots[max(0, i - 2):i] if variant == "deep" and i else [sources[i % 8]]
        meta = with_derived_from({"observation_index": i}, {"sources": [p for p in parents if p["kind"] == "source"],
            "memories": [p for p in parents if p["kind"] == "memory"]})
        if i % 4 == 1:
            meta["workflow"] = "project_auto_update"
        elif i % 4 == 2:
            meta["candidate_kind"] = "memory_consolidation"
        elif i % 4 == 3:
            meta["discovered_by"] = "vnext_weekly_synthesis"
        roots.append({"kind": "memory", "id": UUID(int=100 + i), "domain": "project", "sensitivity": "public",
                      "status": "active", "metadata_json": meta})
    rows = sources + roots
    if variant == "missing":
        rows = roots
    elif variant == "malformed":
        roots[0]["metadata_json"]["derived_from"]["counts"]["sources"] += 1
    elif variant == "cycle":
        roots[0]["metadata_json"] = with_derived_from({}, {"memories": [roots[1]]})
        roots[1]["metadata_json"] = with_derived_from({}, {"memories": [roots[0]], "sources": [sources[1]]})
    elif variant == "alias":
        rows.append({**deepcopy(sources[0]), "id": "urn:uuid:" + str(sources[0]["id"])})
    elif variant == "implicit-weekly":
        artifact = {"kind": "artifact", "id": UUID(int=300), "domain": "project", "sensitivity": "public",
                    "artifact_type": "weekly_synthesis", "metadata_json": {"candidate_memory_ids": [str(roots[0]["id"])],
                        "input_summary": {"source_ids": [str(sources[1]["id"])]}}}
        rows.append(artifact)
        roots[0]["metadata_json"] = {"discovered_by": "vnext_weekly_synthesis", "source_artifact_id": str(artifact["id"])}

    class NativeRows(Rows):
        label_count_canonical_unique_ids = True

        def count_original_label_statuses(self, *args, **kwargs):
            return {}

        def iter_label_rows(self, kind, **kwargs):
            yield roots

    independent = LabelGuard(Rows(rows), active=True, sensitivity_allowed=("public",))
    expected_ids = {row["id"] for row in independent.admit_rows("memory", roots)}
    store = NativeRows(rows)
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == ({"active": len(expected_ids)} if expected_ids else {})
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == expected_ids
        for row in roots:
            assert labels(guard.effective_row("memory", row)) == labels(independent.effective_row("memory", row))


def test_cached_unverified_result_still_uses_regulated_admission():
    root = {"kind": "memory", "id": "root", "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": "missing"}}
    store = Rows([])
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.effective_row("memory", root)["unverified"] is True
        assert guard.admit_rows("memory", [root]) == []


def test_native_bulk_count_does_not_reuse_a_distinct_projection():
    source = {"kind": "source", "id": UUID(int=1), "domain": "project", "sensitivity": "public", "metadata_json": {}}
    root = {"kind": "memory", "id": UUID(int=2), "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": str(source["id"])}}
    store = Rows([source])
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        state = guard._state()
        state.nodes[("memory", str(root["id"]))] = [{**root, "sensitivity": "confidential"}]
        guard._settle_native_count_batch("memory", [root])
        assert guard._key("memory", root) not in state.labels
        assert labels(guard.effective_row("memory", root))[1] == "public"
