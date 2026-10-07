"""A full doctor scan keeps the canonical labels and guarded-read cache contract."""
from __future__ import annotations

import json

import pytest

from alicebot_api import vnext_derived_labels as kernel, vnext_label_repair as repair
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError


def row(kind, stored, **fields):
    return {"kind": kind, "id": stored, "user_id": "u", "domain": "project", "sensitivity": "public",
            "metadata_json": {}, **fields}


def source():
    return row("source", "s", domain="health", sensitivity="confidential", metadata_json={"project_scope": ["P1"]})


def report(stored, parents, **fields):
    return row("artifact", stored, artifact_type="daily_brief",
               metadata_json={"workflow": "daily_brief", "derived_from": {"artifacts": parents,
                              "counts": {"artifacts": len(parents)}}}, **fields)


def oscillating_ring():
    return [report("r0", ["r1"], domain="health"), report("r1", ["r2"], domain="legal"),
            report("r2", ["r0"], domain="health")]


def cases():
    copy = row("memory", "r", metadata_json={"source_id": "s", "project_scope": ["P1", "P2"], "project_floor": ["P2"]})
    yield pytest.param([source(), copy], {}, "memory", "r", True, None, "confidential", id="copy-floor")
    for metadata, reason in (({"source_id": "missing"}, "missing_dependency"),
                             ({"source_id": "s", "candidate_kind": []}, "malformed_marker"),
                             ({"source_id": "s", "project_floor": "P1"}, "malformed_floor"),
                             ({"derived_from": {"sources": ["s"], "counts": {"sources": 2}}}, "counts_disagree"),
                             ({"derived_from": {"sources": ["s"], "counts": {"sources": True}}}, "malformed")):
        yield pytest.param([source(), row("memory", "r", metadata_json=metadata)], {},
                           "memory", "r", True, reason, "public", id=reason)
    yield pytest.param([source(), copy], {"unavailable_kinds": ["source"]},
                       "memory", "r", True, "missing_table", "public", id="missing-table")
    alias = "550e8400-e29b-41d4-a716-446655440000"
    yield pytest.param([source(), {**copy, "id": alias}, {**copy, "id": alias.upper()}], {},
                       "memory", alias, True, "ambiguous_identity", "public", id="uuid-alias")
    backing = row("memory", "backing", metadata_json={"source_id": "s"})
    belief_report = row("artifact", "r", artifact_type="daily_brief",
                        metadata_json={"derived_from": {"beliefs": ["b"], "counts": {"beliefs": 1}}})
    yield pytest.param([source(), backing, row("belief", "b", memory_id="backing"), belief_report], {},
                       "artifact", "r", True, None, "confidential", id="belief")
    weekly = row("memory", "r", metadata_json={"discovered_by": "vnext_weekly_synthesis"})
    parent = row("artifact", "weekly", artifact_type="weekly_synthesis", metadata_json={
        "workflow": "weekly_synthesis", "candidate_memory_ids": ["r"],
        "input_summary": {"source_ids": ["s"], "memory_ids": [], "open_loop_ids": [], "artifact_ids": [],
                          "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0}}})
    yield pytest.param([source(), weekly, parent], {}, "memory", "r", True, None, "confidential", id="weekly-parent")
    yield pytest.param([source(), row("memory", "r", value={"kind": "memory_consolidation", "source_id": "s"})], {},
                       "memory", "r", False, None, "public", id="value-only-marker")
    yield pytest.param([source(), row("memory", "r", value=json.dumps({"source_id": "s"}),
                                      metadata_json={"consolidation": {}})], {},
                       "memory", "r", True, None, "confidential", id="encoded-value-input")
    yield pytest.param([source(), row("memory", "r", metadata_json=json.dumps({"source_id": "s"}))], {},
                       "memory", "r", True, None, "confidential", id="encoded-metadata")
    yield pytest.param([source(), row("memory", "r", user_id="other", metadata_json={"source_id": "s"})], {},
                       "memory", "r", True, "missing_dependency", "public", id="tenant-separation")
    yield pytest.param([source(), copy], {"max_nodes": 1}, "memory", "r", True,
                       "bound_exceeded", "public", id="node-bound")
    tail = row("artifact", "tail", artifact_type="daily_brief", metadata_json={"source_id": "s"})
    yield pytest.param([source(), tail, report("mid", ["tail"]), report("r", ["mid"])], {"max_hops": 1},
                       "artifact", "r", True, "bound_exceeded", "public", id="hop-bound")
    yield pytest.param(oscillating_ring(), {}, "artifact", "r0", True, "cycle_unsettled", "public", id="cycle")


@pytest.mark.parametrize("nodes,options,kind,stored,derived,reason,sensitivity", list(cases()))
def test_one_pass_preserves_every_label_field_and_classification(monkeypatch, nodes, options, kind, stored,
                                                               derived, reason, sensitivity):
    with kernel.label_metadata_cache({}):
        cached = kernel.settle_labels(nodes, on_cycle="unverified", **options)
    with kernel.label_metadata_cache({}):
        single = kernel.settle_labels(nodes, on_cycle="unverified", one_pass=True, **options)
    assert single.rows == cached.rows
    label = single.by_stored(kind, stored)
    assert (label.derived, label.reason, label.sensitivity) == (derived, reason, sensitivity)
    assert label.unverified is (reason is not None)
    if label.stored_id == "r" and label.stored_floor == ("P2",) and reason is None:
        assert (label.project_scope, label.project_floor, label.domain) == (("P1",), ("P1", "P2"), "health")
    tables = {}
    table_names = {"source": "sources", "memory": "memories", "artifact": "generated_artifacts", "belief": "beliefs"}
    for node in nodes:
        tables.setdefault(table_names[node["kind"]], []).append(node)
    actual = repair.classify_stored_labels(tables)

    def cached_settle(inputs, **kwargs):
        return kernel.settle_labels(inputs, **{**kwargs, "one_pass": False})

    monkeypatch.setattr(repair, "settle_labels", cached_settle)
    assert actual == repair.classify_stored_labels(tables)


@pytest.mark.parametrize("one_pass", (False, True))
def test_both_modes_keep_the_cycle_repair_abort(one_pass):
    with pytest.raises(DerivedDomainRepairError, match="did not settle"):
        kernel.settle_labels(oscillating_ring(), one_pass=one_pass)


def test_default_settlement_reuses_parsing_and_rechecks_a_changed_value(monkeypatch):
    root = row("memory", "r", metadata_json={"source_id": "s"})
    original = kernel._derived_dependency_record
    parsed = []

    def traced(name, item):
        parsed.append(item["id"])
        return original(name, item)

    monkeypatch.setattr(kernel, "_derived_dependency_record", traced)
    with kernel.label_metadata_cache({}):
        assert kernel.dependency_record("memory", root) == (frozenset({("source", "s")}), "")
        for _ in range(2):
            assert kernel.settle_labels([source(), root]).by_stored("memory", "r").sensitivity == "confidential"
        assert parsed == ["r"]
        root["value"] = {"source_id": "missing"}
        assert kernel.settle_labels([source(), root]).by_stored("memory", "r").reason == "missing_dependency"
        assert parsed == ["r", "r"]


def test_only_full_classification_opts_into_one_pass(monkeypatch):
    tables = {"sources": [source()], "memories": [row("memory", "r", metadata_json={"source_id": "s"})]}
    original = repair.settle_labels
    options = []

    def traced(nodes, **kwargs):
        options.append(kwargs.get("one_pass", False))
        return original(nodes, **kwargs)

    monkeypatch.setattr(repair, "settle_labels", traced)
    below, unverified = repair.classify_stored_labels(tables)
    planned = repair.plan_label_repairs(tables)
    assert options == [True, False]
    assert below == planned
    assert len(below) == 1 and below[0][2] == "r" and unverified == {}


@pytest.mark.parametrize("max_hops,max_nodes,exceeded", ((0, None, {"r", "mid"}), (1, None, {"r"}),
                                                        (2, None, set()), (None, 0, {"r", "mid"}),
                                                        (None, 1, {"r", "mid"}), (None, 2, {"r"}),
                                                        (None, 3, set())))
def test_shared_bounds_keep_original_selection_order_and_existing_problems(max_hops, max_nodes, exceeded):
    nodes = [source(), row("memory", "mid", metadata_json={"source_id": "s"}),
             row("memory", "r", metadata_json={"consolidation": {"cluster_member_ids": ["mid"]}}),
             row("memory", "skip", metadata_json={"source_id": "missing"})]
    labels = {label.key: label for node in nodes for label in [kernel._node_label(node["kind"], node)]}
    key = lambda stored: ("source" if stored == "s" else "memory", "u", stored)
    resolved = {key("mid"): {key("s")}, key("r"): {key("mid")}, key("skip"): {key("missing")}}
    initial = {key("skip"): "missing_dependency"}
    wrapped, shared = dict(initial), dict(initial)
    kernel._mark_bounds(labels, resolved, wrapped, max_hops=max_hops, max_nodes=max_nodes)
    kernel._mark_dependency_bounds((origin for origin, label in labels.items() if label.derived), resolved, shared,
                                   max_hops=max_hops, max_nodes=max_nodes)
    expected = {**initial, **{key(stored): "bound_exceeded" for stored in exceeded}}
    assert wrapped == shared == expected
    assert list(wrapped) == list(shared)
    assert key("s") not in wrapped
