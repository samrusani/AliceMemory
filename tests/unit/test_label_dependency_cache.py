"""Dependency reuse preserves canonical kernel outcomes and request boundaries."""
from copy import deepcopy
import json
from dataclasses import replace
from uuid import UUID

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_label_guard import LabelGuard, invalidate_read_labels, label_read_scope
from alicebot_api.vnext_derived_labels import identifier, with_derived_from

SOURCE = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
ROOT = "33333333-3333-4333-8333-333333333333"


def row(row_id, metadata=None, **extra):
    return {"id": row_id, "domain": "project", "sensitivity": "public", "metadata_json": metadata or {}, **extra}


class Store:
    def __init__(self, rows):
        self.rows = rows

    def read_label_rows(self, kind, ids):
        return [value for name, value in self.rows if name == kind and identifier(value["id"]) in ids]


def stamp(kind, *ids):
    return with_derived_from({}, {kind: [{"id": value} for value in ids]})["derived_from"]


def effective(guard, kind, value):
    result = guard.effective_row(kind, value)
    return tuple(result.get(key) for key in ("domain", "sensitivity", "project_scope", "project_floor", "unverified"))


def test_unique_metadata_and_text_share_one_settlement_but_never_admission(monkeypatch):
    import alicebot_api.vnext_label_guard as module
    source = row(SOURCE, {"project_scope": ["alpha"]}, sensitivity="confidential")
    store = Store([("source", source)])
    calls = []
    original = module.settle_labels
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "settle_labels", counted)
    with label_read_scope(store):
        restricted = LabelGuard.for_filters(store, (), ("public",))
        copies = [row(str(UUID(int=i + 10)), {"source_id": SOURCE, "observation": i}, canonical_text=str(i)) for i in range(30)]
        assert restricted.admit_rows("memory", copies) == []
        assert len(calls) == 1
        broad = replace(restricted, sensitivity_allowed=ALL_SENSITIVITY)
        assert broad.admit_rows("memory", copies) == copies
        assert len(calls) == 1
        source["sensitivity"] = "public"
        invalidate_read_labels(store)
        assert restricted.admit_rows("memory", copies) == copies
        assert len(calls) == 2
    source["sensitivity"] = "confidential"
    with label_read_scope(store):
        assert restricted.admit_rows("memory", copies) == []
        assert len(calls) == 3


@pytest.mark.parametrize("variant", ["self", "ancestor", "alias", "belief", "malformed", "missing", "floor", "class"])
def test_reused_signature_agrees_with_fresh_kernel_for_boundary_variants(variant):
    source = row(SOURCE, sensitivity="confidential")
    parent = row(PARENT, {"source_id": SOURCE})
    first = row(ROOT, {"source_id": SOURCE, "observation": 1})
    second = row("44444444-4444-4444-8444-444444444444", {"source_id": SOURCE, "observation": 2})
    rows = [("source", source), ("memory", parent)]
    if variant == "self":
        first["metadata_json"] = second["metadata_json"] = {"derived_from": stamp("memories", second["id"])}
    elif variant in {"ancestor", "alias"}:
        first["metadata_json"] = second["metadata_json"] = {"derived_from": stamp("memories", PARENT)}
        second["id"] = "urn:uuid:" + PARENT if variant == "alias" else PARENT
    elif variant == "belief":
        belief = row("55555555-5555-4555-8555-555555555555", memory_id=second["id"])
        first["metadata_json"] = second["metadata_json"] = {"derived_from": stamp("beliefs", belief["id"])}
        rows.extend([("belief", belief), ("memory", row(second["id"], sensitivity="confidential"))])
    elif variant == "malformed":
        second["metadata_json"]["derived_from"] = {"sources": [SOURCE], "counts": {"sources": 99}}
    elif variant == "missing":
        second["metadata_json"]["source_id"] = "missing-source"
    elif variant == "floor":
        second["metadata_json"]["project_floor"] = ["beta"]
    elif variant == "class":
        second["metadata_json"]["workflow"] = "project_auto_update"
    if variant == "self":
        rows.append(("memory", row(second["id"], sensitivity="confidential")))
    store = Store(rows)
    guard = LabelGuard(store, active=True)
    with label_read_scope(store):
        assert effective(guard, "memory", first)[-1] is False
        reused = effective(guard, "memory", second)
    fresh = effective(LabelGuard(store, active=True), "memory", deepcopy(second))
    assert reused == fresh


def test_implicit_weekly_candidate_parent_is_not_shared_between_candidate_ids():
    weekly = row("66666666-6666-4666-8666-666666666666",
                 {"candidate_memory_ids": [ROOT], "derived_from": stamp("sources", SOURCE),
                  "input_summary": {"source_ids": [SOURCE], "memory_ids": [], "open_loop_ids": [], "artifact_ids": [],
                                    "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0}}},
                 artifact_type="weekly_synthesis")
    first = row(ROOT, {"discovered_by": "vnext_weekly_synthesis", "source_artifact_id": weekly["id"]})
    second = row(PARENT, deepcopy(first["metadata_json"]))
    store = Store([("artifact", weekly), ("source", row(SOURCE, sensitivity="confidential"))])
    guard = LabelGuard(store, active=True)
    with label_read_scope(store):
        effective(guard, "memory", first)
        reused = effective(guard, "memory", second)
    assert reused == effective(LabelGuard(store, active=True), "memory", second)


@pytest.mark.parametrize("change", ["nested_parent", "encoded_ref", "counts", "nested_scope", "floor_presence", "value_parent", "encoded_scope"])
def test_parsing_memo_preserves_non_incidental_fields(change):
    source = row(SOURCE, {"project_scope": ["alpha"]})
    hidden = row(PARENT, {"project_scope": ["beta"]}, sensitivity="confidential")
    first = row(ROOT, {"source_id": SOURCE})
    second = row("44444444-4444-4444-8444-444444444444", deepcopy(first["metadata_json"]))
    if change == "nested_parent":
        second["metadata_json"]["evidence"] = {"source_id": PARENT}
    elif change == "encoded_ref":
        second["metadata_json"]["source_refs"] = ['{"source_id":"' + PARENT + '"}']
    elif change == "counts":
        second["metadata_json"]["input_counts"] = {"sources": 99}
        second["metadata_json"]["workflow"] = "project_auto_update"
    elif change == "nested_scope":
        second["metadata_json"]["agentic_memory"] = {"project_scope": ["beta"]}
    elif change == "floor_presence":
        first["metadata_json"]["project_floor"] = second["metadata_json"]["project_floor"] = ["beta"]
        second["project_floor"] = None
    elif change == "encoded_scope":
        first["metadata_json"]["project_scope"] = ["alpha"]
        second["metadata_json"] = json.dumps(first["metadata_json"])
    elif change == "value_parent":
        second["value"] = {"source_id": PARENT}
    store = Store([("source", source), ("source", hidden)])
    with label_read_scope(store):
        guard = LabelGuard(store, active=True)
        effective(guard, "memory", first)
        reused = effective(guard, "memory", second)
    assert reused == effective(LabelGuard(store, active=True), "memory", second)
