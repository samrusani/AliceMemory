"""Regressions from the October 6 derived-label review."""
from uuid import UUID

import pytest

from alicebot_api.vnext_derived_labels import MARKER_KEYS, dependency_record, is_derived, row_class, with_derived_from
from alicebot_api.vnext_label_repair import classify_stored_labels, plan_label_repairs

SID = "abcdef01-2345-6789-abcd-ef0123456789"


@pytest.mark.parametrize("kind", ["memory", "open_loop", "artifact"])
@pytest.mark.parametrize("key", ["source_id", "source_artifact_id", "artifact_id"])
def test_uuid_objects_in_server_records_are_dependencies(kind, key):
    meta = {"discovered_by": "vnext_daily_brief", "workflow": "daily_brief", "derived_from": {}, key: UUID(SID)}
    row = {"id": "copy", "source_id": UUID(SID), "metadata_json": meta}
    assert is_derived(kind, row)
    deps, problem = dependency_record(kind, row)
    assert not problem
    assert ("source" if key == "source_id" else "artifact", SID) in deps


def test_open_loop_derived_from_is_a_sufficient_marker():
    row = {"id": "loop", "metadata_json": with_derived_from({}, {"sources": [{"id": UUID(SID)}]})}
    assert is_derived("open_loop", row)
    assert dependency_record("open_loop", row) == (frozenset({("source", SID)}), "")


@pytest.mark.parametrize("kind", ["memory", "artifact", "open_loop", "project", "source", "belief"])
@pytest.mark.parametrize("shape", [[], {}, 12, None])
def test_planner_is_total_over_malformed_marker_shapes(kind, shape):
    for key in MARKER_KEYS:
        row = {"kind": kind, "id": "row", "user_id": "user", "domain": "project", "sensitivity": "public",
               "metadata_json": {key: shape}}
        row_class(kind, row)
        table = {"artifact": "generated_artifacts", "memory": "memories", "open_loop": "open_loops", "project": "projects", "source": "sources", "belief": "beliefs"}[kind]
        plan_label_repairs({table: [row]})
        classify_stored_labels({table: [row]})


def test_free_text_source_refs_are_not_database_identifiers():
    row = {"metadata_json": {"candidate_kind": "memory_consolidation", "source_refs": ["meeting-notes"]}}
    assert dependency_record("memory", row) == (frozenset(), "")
