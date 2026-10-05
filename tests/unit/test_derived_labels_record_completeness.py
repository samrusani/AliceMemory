"""Missing legacy lists cannot pass a producer's positive completeness count."""

from __future__ import annotations

import pytest

from alicebot_api.vnext_derived_labels import settle_labels


def legacy_report(workflow, lists, counts):
    metadata = {"workflow": workflow}
    if workflow in {"daily_brief", "weekly_synthesis"}:
        metadata["input_summary"] = {**lists, "counts": counts}
    else:
        metadata.update(lists)
        metadata["input_counts"] = counts
    return {"kind": "artifact", "id": "report", "domain": "project", "sensitivity": "public", "metadata_json": metadata}


@pytest.mark.parametrize("workflow", ("daily_brief", "weekly_synthesis", "connection_finder", "contradiction_finder"))
@pytest.mark.parametrize("lists,counts", (({}, {"sources": 1}), ({"source_ids": []}, {"sources": "1"}),
                                       ({"source_ids": []}, {"sources": True}), ({"source_ids": []}, {"sources": -1}),
                                       ({"source_ids": []}, [])))
def test_a_missing_or_malformed_legacy_completeness_record_is_unverified(workflow, lists, counts):
    row = legacy_report(workflow, lists, counts)
    label = settle_labels([row]).by_stored("artifact", "report")
    assert label.unverified is True
    assert label.reason == "legacy_counts"


@pytest.mark.parametrize("workflow", ("daily_brief", "weekly_synthesis", "connection_finder", "contradiction_finder"))
@pytest.mark.parametrize("lists,counts", (({}, {"sources": 0}), ({"source_ids": []}, {"sources": 0}),
                                       ({"source_ids": []}, {})))
def test_a_legitimate_empty_legacy_record_is_verified(workflow, lists, counts):
    row = legacy_report(workflow, lists, counts)
    assert settle_labels([row]).by_stored("artifact", "report").unverified is False
