"""Reduced native admission has an independent complete-label oracle."""
from copy import deepcopy
from dataclasses import replace
from itertools import product
import json
import random
from types import SimpleNamespace
from uuid import UUID

import pytest

from alicebot_api import vnext_label_guard as guards
from alicebot_api.vnext_derived_labels import SENSITIVITY_RANK, SettledLabel, identifier, settle_labels, with_derived_from
from alicebot_api.vnext_label_writes import label_savepoint_rolled_back, takes_label_lock


class NativeRows:
    label_count_canonical_unique_ids = True

    def __init__(self, rows, roots):
        self.rows, self.roots = rows, roots
        self.conn = SimpleNamespace()
        self.locks = 0

    def read_label_rows(self, kind, ids):
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in ids]

    def count_original_label_statuses(self, *args, **kwargs):
        return {}

    def iter_label_rows(self, kind, **kwargs):
        yield self.roots

    def lock_label_writes(self, **kwargs):
        self.locks += 1

    @takes_label_lock
    def update_sensitivity(self, source, sensitivity):
        source["sensitivity"] = sensitivity


def source_and_root(sensitivity="public"):
    source = {"kind": "source", "id": UUID(int=1), "user_id": "label-guard", "domain": "project",
              "sensitivity": sensitivity, "metadata_json": {"project_scope": [], "project_floor": []}}
    hidden = {**deepcopy(source), "id": UUID(int=3), "sensitivity": "confidential"}
    root = {"kind": "memory", "id": UUID(int=0xABCDEF), "user_id": "label-guard", "domain": "project",
            "sensitivity": "public", "status": "active", "value": {},
            "metadata_json": {"source_id": str(source["id"]), "project_scope": [], "project_floor": []}}
    return NativeRows([source, hidden, root], [root]), source, hidden, root


def mixed_rows():
    rng = random.Random(20261007)
    sources = [{"kind": "source", "id": UUID(int=i + 1), "user_id": "label-guard", "domain": ("health", "legal", "project")[i % 3],
                "sensitivity": "confidential" if i % 2 else "public",
                "metadata_json": {"project_scope": [], "project_floor": []}} for i in range(8)]
    roots = []
    for i in range(32):
        parents = rng.sample(sources + roots, 2)
        metadata = with_derived_from({"project_scope": [], "project_floor": [], "observation_index": i},
            {"sources": [row for row in parents if row["kind"] == "source"],
             "memories": [row for row in parents if row["kind"] == "memory"]})
        metadata[("workflow", "candidate_kind", "discovered_by")[i % 3]] = (
            "project_auto_update", "memory_consolidation", "vnext_weekly_synthesis")[i % 3]
        roots.append({"kind": "memory", "id": UUID(int=100 + i), "user_id": "label-guard", "domain": "project",
                      "sensitivity": "public", "status": "active", "value": {}, "metadata_json": metadata})
    return sources, roots


def full_labels(rows):
    return settle_labels([{**row, "user_id": "label-guard"} for row in rows], on_cycle="unverified",
                         max_nodes=guards.NODE_BOUND, max_hops=guards.HOP_BOUND)


def assert_only_full_labels(state):
    assert all(isinstance(value, SettledLabel) for value in state.labels.values())
    assert all(isinstance(entry[1], SettledLabel) for entry in state.native_labels.values())


def test_native_rank_dag_matches_complete_kernel_and_keeps_full_projections():
    sources, roots = mixed_rows()
    expected = full_labels(sources + roots)
    by_id = {label.normalized_id: label for label in expected.rows if label.kind == "memory"}
    visible = {row["id"] for row in roots if not by_id[str(row["id"])].unverified and by_id[str(row["id"])].sensitivity == "public"}
    store = NativeRows(sources + roots, roots)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == ({"active": len(visible)} if visible else {})
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == visible
        state = guard._state()
        assert len(state.rank_origins) == len(roots)
        assert not state.labels and not state.native_labels
        for row in roots:
            label = by_id[str(row["id"])]
            assert state.rank_origins[("memory", str(row["id"]))][1] == SENSITIVITY_RANK[label.sensitivity]
            effective = guard.effective_row("memory", row)
            assert (effective["domain"], effective["sensitivity"], tuple(effective["project_scope"]),
                    tuple(effective["project_floor"]), effective["unverified"]) == (
                label.domain, label.sensitivity, label.project_scope, label.project_floor, label.unverified)
        assert_only_full_labels(state)


SCOPES = ([], ["P1"], ["P2"], ["P1", "P2"], ["P1", "P1", " p2 "])


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("shape", [list, tuple], ids=["list", "tuple"])
def test_scoped_graphs_publish_the_ranks_of_the_complete_kernel(shape, seed):
    """A project scope or floor never changes a sensitivity, so a scoped graph keeps its reduced proof.

    A scope or floor held as a tuple is a canonical shape too: the complete kernel reads it like a
    list. No store hands one back (stored JSON gives lists), but a row built in process can carry one.

    Mutations: refuse a non-empty scope or floor again (a vault with project-scoped sources has them),
    or accept only a list (the tuple cases).
    """
    rng = random.Random(seed)
    sources, roots = mixed_rows()
    for row in (*sources, *roots):
        row["metadata_json"]["project_scope"] = shape(rng.choice(SCOPES))
        row["metadata_json"]["project_floor"] = shape(rng.choice(SCOPES))
        if shape is tuple:
            row["project_scope"] = row["metadata_json"]["project_scope"]
            row["project_floor"] = row["metadata_json"]["project_floor"]
    expected = full_labels(sources + roots)
    by_id = {label.normalized_id: label for label in expected.rows if label.kind == "memory"}
    assert any(label.project_scope or label.project_floor for label in by_id.values())
    visible = {row["id"] for row in roots if not by_id[str(row["id"])].unverified and by_id[str(row["id"])].sensitivity == "public"}
    store = NativeRows(sources + roots, roots)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == ({"active": len(visible)} if visible else {})
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == visible
        state = guard._state()
        assert len(state.rank_origins) == len(roots)
        assert not state.labels and not state.native_labels
        for row in roots:
            assert state.rank_origins[("memory", str(row["id"]))][1] == SENSITIVITY_RANK[by_id[str(row["id"])].sensitivity]
        assert_only_full_labels(state)


CEILINGS = [tuple(value for value, rank in SENSITIVITY_RANK.items() if rank <= high) for high in range(1, 7)]
CEILINGS += [("public", "internal"), ("public", "unknown"), ("public", "confidential"),
             ("public", "internal", "unknown", "private", "confidential", "highly_sensitive", "sacred"),
             ("public", "internal", "unknown", "private", "confidential", "highly_sensitive", "regulated"),
             ("invalid",), ()]


@pytest.mark.parametrize("sensitivity,allowed", list(product((*SENSITIVITY_RANK, "invalid"), CEILINGS)))
def test_all_rank_boundaries_and_split_allowances_match_full_labels(sensitivity, allowed):
    store, source, _hidden, root = source_and_root(sensitivity)
    label = full_labels(store.rows).by_stored("memory", str(root["id"]), user_id="label-guard")
    expected = not allowed or label.sensitivity in allowed
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=allowed)
        assert guard.readable_status_counts("memory") == ({"active": 1} if expected else {})
        assert bool(guard.admit_rows("memory", [root])) == expected
        if guard._rank_ceiling() is None or source["sensitivity"] == "invalid":
            assert not guard._state().rank_origins
        assert_only_full_labels(guard._state())


@pytest.mark.parametrize("variant", ("missing", "malformed-counts", "malformed-marker", "floor-null", "scope-null",
    "scope-string", "floor-mapping", "legacy-alias", "cycle", "alias", "implicit-weekly", "implicit-weekly-json",
    "node-bound", "hop-bound"))
def test_unsupported_graphs_use_full_kernel_without_publishing_ranks(monkeypatch, variant):
    sources, roots = mixed_rows()
    rows = sources + roots
    if variant == "missing":
        rows = roots
    elif variant == "malformed-counts":
        roots[0]["metadata_json"]["derived_from"]["counts"]["sources"] += 1
    elif variant == "malformed-marker":
        roots[0]["metadata_json"]["candidate_kind"] = []
    elif variant == "floor-null":
        roots[0]["metadata_json"]["project_floor"] = None
    elif variant == "scope-null":
        roots[0]["metadata_json"]["project_scope"] = None
    elif variant == "scope-string":
        sources[0]["metadata_json"]["project_scope"] = "P1"
    elif variant == "floor-mapping":
        roots[0]["metadata_json"]["project_floor"] = {"P1": True}
    elif variant == "legacy-alias":
        sources[0]["metadata_json"]["project_id"] = "P1"
    elif variant == "cycle":
        roots[0]["metadata_json"] = with_derived_from({}, {"memories": [roots[1]]})
        roots[1]["metadata_json"] = with_derived_from({}, {"memories": [roots[0]], "sources": [sources[1]]})
    elif variant == "alias":
        rows.append({**deepcopy(sources[0]), "id": "urn:uuid:" + str(sources[0]["id"])})
    elif variant.startswith("implicit-weekly"):
        artifact = {"kind": "artifact", "id": UUID(int=400), "user_id": "label-guard", "domain": "project",
                    "sensitivity": "public", "artifact_type": "weekly_synthesis",
                    "metadata_json": {"candidate_memory_ids": [str(roots[0]["id"])],
                                      "input_summary": {"source_ids": [str(sources[1]["id"])]}}}
        roots[0]["metadata_json"] = {"discovered_by": "vnext_weekly_synthesis", "source_artifact_id": str(artifact["id"])}
        if variant.endswith("-json"):
            artifact["metadata_json"] = json.dumps(artifact["metadata_json"])
        rows.append(artifact)
    elif variant == "node-bound":
        monkeypatch.setattr(guards, "NODE_BOUND", 3)
    elif variant == "hop-bound":
        monkeypatch.setattr(guards, "HOP_BOUND", 1)
    expected = full_labels(rows)
    labels = {label.normalized_id: label for label in expected.rows if label.kind == "memory"}
    visible = {row["id"] for row in roots if not labels[str(row["id"])].unverified and labels[str(row["id"])].sensitivity == "public"}
    full_calls = []
    def counted_full(*args, **kwargs):
        full_calls.append(True)
        return settle_labels(*args, **kwargs)
    monkeypatch.setattr(guards, "settle_labels", counted_full)
    store = NativeRows(rows, roots)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == ({"active": len(visible)} if visible else {})
        assert {row["id"] for row in guard.admit_rows("memory", roots)} == visible
        assert not guard._state().rank_origins and not guard._state().rank_rows
        assert full_calls
        assert_only_full_labels(guard._state())


@pytest.mark.parametrize("variant", ("same", "scope-null", "floor-null", "legacy-project", "legacy-scope-json",
    "nested-agent", "metadata-json-string", "missing-scope-floor", "value-hidden-parent", "source-hidden-parent",
    "source-equivalent-uuid-spelling", "row-uuid-alias", "domain-changed", "sensitivity-changed", "missing-user", "count-disagreement"))
def test_reloaded_projections_require_supported_shape_and_canonical_signature(variant):
    store, source, hidden, root = source_and_root()
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == {"active": 1}
        reloaded = deepcopy(root)
        if variant == "scope-null":
            reloaded["metadata_json"]["project_scope"] = None
        elif variant == "floor-null":
            reloaded["project_floor"] = None
        elif variant == "legacy-project":
            reloaded["project_id"] = "P1"
        elif variant == "legacy-scope-json":
            reloaded["scope_json"] = {"project_scope": []}
        elif variant == "nested-agent":
            reloaded["metadata_json"]["agentic_memory"] = {"project_scope": []}
        elif variant == "metadata-json-string":
            reloaded["metadata_json"] = json.dumps(reloaded["metadata_json"])
        elif variant == "missing-scope-floor":
            reloaded["metadata_json"].pop("project_scope")
            reloaded["metadata_json"].pop("project_floor")
        elif variant == "value-hidden-parent":
            reloaded["value"] = {"source_id": str(hidden["id"])}
        elif variant == "source-hidden-parent":
            reloaded["metadata_json"]["source_id"] = str(hidden["id"])
        elif variant == "source-equivalent-uuid-spelling":
            reloaded["metadata_json"]["source_id"] = "SOURCE:" + str(source["id"]).upper()
        elif variant == "row-uuid-alias":
            reloaded["id"] = "urn:uuid:" + str(root["id"])
        elif variant == "domain-changed":
            reloaded["domain"] = "health"
        elif variant == "sensitivity-changed":
            reloaded["sensitivity"] = "confidential"
        elif variant == "missing-user":
            reloaded.pop("user_id")
        elif variant == "count-disagreement":
            reloaded["metadata_json"] = with_derived_from(reloaded["metadata_json"], {"sources": [source]})
            reloaded["metadata_json"]["derived_from"]["counts"]["sources"] += 1
        rank = guard._native_rank_for_projection("memory", reloaded)
        if variant not in ("same", "missing-scope-floor", "source-equivalent-uuid-spelling"):
            assert rank is None
        for allowed in (("public",), ("public", "internal", "unknown", "private"), ("public", "confidential")):
            independent = guards.LabelGuard(NativeRows(store.rows, [reloaded]), active=True, sensitivity_allowed=allowed)
            expected = bool(independent.admit_rows("memory", [reloaded]))
            assert bool(replace(guard, sensitivity_allowed=allowed).admit_rows("memory", [reloaded])) == expected
        assert_only_full_labels(guard._state())


@pytest.mark.parametrize("source_ref", ("canonical", "prefixed", "encoded"))
def test_source_kind_and_source_grammar_stay_canonical(source_ref):
    store, source, hidden, root = source_and_root()
    hidden["id"] = UUID("123e4567-e89b-42d3-a456-426614174000")
    source["metadata_json"]["source_id"] = str(UUID(int=9999))
    hidden["metadata_json"]["source_id"] = str(UUID(int=9999))
    ref = str(hidden["id"])
    if source_ref == "prefixed":
        ref = "SOURCE:" + ref.upper()
    elif source_ref == "encoded":
        escaped = "".join("\\u%04x" % ord(character) for character in ref)
        ref = '{"source_id":"' + escaped + '"}'
    root["metadata_json"] = {"source_refs": [ref], "derived_from": {"v": 1, "sources": [], "counts": {}},
                             "project_scope": [], "project_floor": []}
    label = full_labels(store.rows).by_stored("memory", str(root["id"]), user_id="label-guard")
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == {}
        assert label.sensitivity == "confidential" and not label.unverified
        assert len(guard._state().rank_origins) == 1
        assert guard.admit_rows("memory", [root]) == []


def test_event_spelling_filter_grants_and_writer_rollback_invalidation():
    store, source, _hidden, root = source_and_root()
    canonical = {"target_type": "memory", "target_id": str(root["id"]), "event_type": "memory.created"}
    alias = {**canonical, "target_id": str(root["id"]).upper()}
    source_event = {"target_type": "source", "target_id": str(source["id"]), "event_type": "source.created"}
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == {"active": 1}
        assert guard.admit_events([canonical, alias]) == [canonical]
        assert guard.admit_events([source_event]) == [source_event]
        assert replace(guard, domains=("health",)).admit_events([canonical]) == []
        assert replace(guard, projects=("P1",), all_of=("P1",)).admit_events([canonical]) == []
        assert replace(guard, sensitivity_allowed=("public", "internal", "unknown", "private")).admit_events([canonical]) == [canonical]
        assert len(guard._state().rank_admission) == 2
        store.update_sensitivity(source, "confidential")
        assert store.locks == 1
        assert not guard._state().rank_rows and not guard._state().rank_origins and not guard._state().rank_admission
        assert guard.admit_events([canonical]) == []
        source["sensitivity"] = "public"
        label_savepoint_rolled_back(store)
        assert store.conn._alice_label_rollback_counter == 1
        assert not guard._state().rank_rows and not guard._state().rank_origins and not guard._state().rank_admission
        assert guard.readable_status_counts("memory") == {"active": 1}
        assert guard.admit_events([canonical]) == [canonical]
        assert_only_full_labels(guard._state())


@pytest.mark.parametrize("variant,expected_rank_walks", (("acyclic", 1), ("node-bound", 1), ("cycle", 0)))
def test_cyclic_fallback_skips_the_extra_rank_bound_walk(monkeypatch, variant, expected_rank_walks):
    store, _source, _hidden, root = source_and_root()
    if variant == "cycle":
        second = {**deepcopy(root), "id": UUID(int=100)}
        root["metadata_json"] = with_derived_from({}, {"memories": [second]})
        second["metadata_json"] = with_derived_from({}, {"memories": [root]})
        store.rows.append(second)
        store.roots.append(second)
    elif variant == "node-bound":
        monkeypatch.setattr(guards, "NODE_BOUND", 1)
    labels = full_labels(store.rows)
    visible = sum(not labels.by_stored("memory", str(row["id"])).unverified
                  and labels.by_stored("memory", str(row["id"])).sensitivity == "public" for row in store.roots)
    original = guards._mark_dependency_bounds
    walks = []

    def counted(*args, **kwargs):
        walks.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(guards, "_mark_dependency_bounds", counted)
    with guards.label_read_scope(store):
        guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert guard.readable_status_counts("memory") == ({"active": visible} if visible else {})
        assert len(walks) == expected_rank_walks
        if variant != "acyclic":
            assert not guard._state().rank_origins
        assert_only_full_labels(guard._state())


def test_rank_proofs_require_native_locked_store_and_active_guard():
    store, _source, _hidden, root = source_and_root()
    guard = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
    assert guard._rank_ceiling() is None
    with guards.label_read_scope(store):
        assert replace(guard, active=False)._rank_ceiling() is None
        store.label_count_canonical_unique_ids = False
        # The capability belongs to the store type, not a mutable instance.
        assert guard._rank_ceiling() == 1
        class TextRows(NativeRows):
            label_count_canonical_unique_ids = False
        other = TextRows(store.rows, [root])
        with guards.label_read_scope(other):
            assert guards.LabelGuard(other, active=True, sensitivity_allowed=("public",))._rank_ceiling() is None


def test_same_origin_keeps_counts_and_event_grants_separate_across_rank_ceilings():
    store, _source, _hidden, root = source_and_root("private")
    canonical = {"target_type": "memory", "target_id": str(root["id"]), "event_type": "memory.created"}
    alias = {**canonical, "target_id": str(root["id"]).upper()}
    label = full_labels(store.rows).by_stored("memory", str(root["id"]), user_id="label-guard")
    assert not label.unverified and label.sensitivity == "private"
    with guards.label_read_scope(store):
        public = guards.LabelGuard(store, active=True, sensitivity_allowed=("public",))
        private = replace(public, sensitivity_allowed=("public", "internal", "unknown", "private"))
        for caller in (public, private, public):
            readable = label.sensitivity in caller.sensitivity_allowed
            assert caller.readable_status_counts("memory") == ({"active": 1} if readable else {})
            assert caller.admit_rows("memory", [deepcopy(root)]) == ([root] if readable else [])
            assert caller.admit_events([canonical, alias]) == ([canonical] if readable else [])
        assert len(public._state().rank_origins) == 1
        assert_only_full_labels(public._state())
