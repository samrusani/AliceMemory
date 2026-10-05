"""A source move previews loss of full project admission before any write."""
from copy import deepcopy
from uuid import uuid4

import pytest

from alicebot_api import vnext_label_writes as writes


def _source(scope):
    return {"id": str(uuid4()), "kind": "source", "user_id": "synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": scope}}


def _report(sources, scope):
    return {"id": str(uuid4()), "kind": "artifact", "user_id": "synthetic", "artifact_type": "daily_brief", "domain": "project", "sensitivity": "public", "metadata_json": {"source_ids": [row["id"] for row in sources], "project_scope": scope}}


class Store:
    def __init__(self, rows):
        self.rows = rows
        self.reads = []
    def read_label_rows(self, kind, ids):
        self.reads.append((kind, ids))
        return [row for row in self.rows if row["kind"] == kind and row["id"] in ids]


def test_stored_scope_unchanged_floor_move_still_counts_hidden_row(monkeypatch):
    source = _source(["alpha"])
    report = _report([source], ["alpha"])
    store = Store([source, report])
    original = deepcopy(store.rows)
    monkeypatch.setattr(writes, "walk_dependants", lambda *_: [report])
    assert writes.count_rows_hidden_by_scope_move(store, source, ["beta"]) == 1
    assert store.rows == original


def test_preview_reads_the_other_parent_and_its_ancestry(monkeypatch):
    moved, other = _source(["alpha"]), _source(["alpha"])
    parent = _report([other], ["alpha"])
    report = _report([moved], ["alpha"])
    report["metadata_json"]["artifact_ids"] = [parent["id"]]
    store = Store([moved, other, parent, report])
    monkeypatch.setattr(writes, "walk_dependants", lambda *_: [report])
    assert writes.count_rows_hidden_by_scope_move(store, moved, ["beta"]) == 1
    assert any(other["id"] in ids for _kind, ids in store.reads)
    assert any(parent["id"] in ids for _kind, ids in store.reads)


def test_preview_does_not_count_a_row_already_unverified(monkeypatch):
    source = _source(["alpha"])
    report = _report([source, _source(["alpha"])], ["alpha"])
    monkeypatch.setattr(writes, "walk_dependants", lambda *_: [report])
    assert writes.count_rows_hidden_by_scope_move(Store([source, report]), source, ["beta"]) == 0
