"""Weekly candidates: one pass finds the marker of every named candidate.

The label kernel used to scan every node once per named weekly candidate, which
is quadratic on a vault with hundreds of weekly reports. These tests keep the
earlier scan as the oracle and compare it with the single pass on random graphs.

Mutations: scan every node once per named weekly candidate again (the large-graph
test), or let the last row with a key answer for it instead of the first.
"""
from __future__ import annotations

from collections.abc import Mapping
import random
from uuid import UUID

import pytest

from alicebot_api import vnext_derived_labels as derived
from alicebot_api.vnext_derived_labels import SettledLabel, _node_label, _strings, canon_kind, with_derived_from


def old_weekly_parent_deps(labels, own, nodes):
    """The per-candidate scan this replaced, kept as the oracle."""
    artifacts = [
        (label, derived._metadata(row))
        for label, row in nodes
        if label.kind == "artifact" and isinstance(derived._metadata(row).get("input_summary"), Mapping)
    ]
    for label, meta in artifacts:
        for candidate in _strings(meta.get("candidate_memory_ids")):
            candidate_key = ("memory", label.user_id, candidate)
            candidate_label = labels.get(candidate_key)
            if candidate_label is None or candidate_label.row_class != "aggregate":
                continue
            found = None
            for node_label, row in nodes:
                if node_label.key == candidate_key:
                    found = derived._metadata(row).get("discovered_by")
                    break
            if found != "vnext_weekly_synthesis":
                continue
            own.setdefault(candidate_key, set()).update(own.get(label.key, set()))


def weekly_world(seed: int, *, duplicate: bool):
    rng = random.Random(seed)
    sources = [{"kind": "source", "id": str(UUID(int=i + 1)), "user_id": "u", "domain": "project",
                "sensitivity": "public", "metadata_json": {}} for i in range(6)]
    memories = []
    for i in range(14):
        marker = rng.choice(["vnext_weekly_synthesis", "vnext_weekly_synthesis", "other", None])
        meta = {"observation": i}
        if marker:
            meta["discovered_by"] = marker
        memories.append({"kind": "memory", "id": str(UUID(int=100 + i)), "user_id": "u", "domain": "project",
                         "sensitivity": "public", "status": "candidate", "value": {}, "metadata_json": meta})
    if duplicate:
        twin = dict(memories[0])
        twin["metadata_json"] = {"observation": 99, "discovered_by": "other"}
        memories.insert(rng.randrange(len(memories)), twin)
    artifacts = []
    for i in range(10):
        picked = rng.sample(memories, rng.randrange(0, 4))
        named = [row["id"] for row in picked] + (["missing-" + str(i)] if i % 4 == 0 else [])
        inputs = rng.sample(sources, rng.randrange(1, 3))
        meta = with_derived_from({"workflow": "weekly_synthesis", "candidate_memory_ids": named,
                                  "input_summary": {"source_ids": [row["id"] for row in inputs]}}, {"sources": inputs})
        artifacts.append({"kind": "artifact", "id": str(UUID(int=500 + i)), "user_id": "u", "domain": "project",
                          "sensitivity": "public", "artifact_type": "weekly_synthesis", "metadata_json": meta})
    rows = sources + memories + artifacts
    rng.shuffle(rows)
    return rows


def run_both(seed, duplicate):
    rows = weekly_world(seed, duplicate=duplicate)
    prepared = [(_node_label(canon_kind(row["kind"]), row), row) for row in rows]
    labels = {label.key: label for label, _row in prepared}
    base = {label.key: {("source", label.user_id, label.stored_id)} for label, _row in prepared if label.derived}
    expected = {key: set(value) for key, value in base.items()}
    actual = {key: set(value) for key, value in base.items()}
    old_weekly_parent_deps(labels, expected, prepared)
    derived._weekly_parent_deps(labels, actual, prepared)
    return base, expected, actual


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("seed", range(30))
def test_single_pass_weekly_inputs_equal_the_per_candidate_scan(seed, duplicate):
    _base, expected, actual = run_both(seed, duplicate)
    assert actual == expected


@pytest.mark.parametrize("duplicate", [False, True])
def test_the_weekly_worlds_really_merge_artifact_inputs_into_candidates(duplicate):
    merged = 0
    for seed in range(30):
        base, _expected, actual = run_both(seed, duplicate)
        merged += sum(1 for key in actual if actual[key] != base[key])
    assert merged >= 30, merged


def key_reads(monkeypatch, run):
    """How many node keys `run` reads."""
    reads = 0
    original = SettledLabel.key

    def counting(self):
        nonlocal reads
        reads += 1
        return original.fget(self)

    with monkeypatch.context() as patch:
        patch.setattr(SettledLabel, "key", property(counting))
        run()
    return reads


def test_a_scan_of_a_large_graph_visits_each_node_once_not_once_per_candidate(monkeypatch):
    """Named candidates behind 2,000 other rows must not cost one scan of the rows each.

    The filler comes first on purpose. The earlier scan stopped at the first row that
    carried a key, so filler placed after the world rows would never be read and the
    quadratic cost would not show.
    """
    rows = weekly_world(2, duplicate=False)
    filler = [{"kind": "source", "id": str(UUID(int=10_000 + i)), "user_id": "u", "domain": "project",
               "sensitivity": "public", "metadata_json": {}} for i in range(2000)]
    prepared = [(_node_label(canon_kind(row["kind"]), row), row) for row in filler + rows]
    labels = {label.key: label for label, _row in prepared}
    base = {label.key: {("source", label.user_id, label.stored_id)} for label, _row in prepared if label.derived}
    expected = {key: set(value) for key, value in base.items()}
    actual = {key: set(value) for key, value in base.items()}
    bound = 3 * len(prepared)

    old_reads = key_reads(monkeypatch, lambda: old_weekly_parent_deps(labels, expected, prepared))
    new_reads = key_reads(monkeypatch, lambda: derived._weekly_parent_deps(labels, actual, prepared))

    assert actual == expected
    assert any(actual[key] != base[key] for key in actual)
    assert old_reads > bound, (old_reads, bound)  # this graph does expose the per-candidate scan
    assert new_reads <= bound, (new_reads, bound)
