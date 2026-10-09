"""The PostgreSQL extra-rows fixture holds the artifact shapes of a vault with daily and weekly reports.

Mutations: name no weekly candidates, drop the derived_from record of a daily brief, change the
counts of either kind, let the insert floor raise the artifacts, or drop the unique metadata.
"""
from __future__ import annotations

from uuid import UUID

from alicebot_api.vnext_derived_labels import dependencies_of, has_implicit_weekly_inputs, is_derived
from alicebot_api.vnext_label_repair import label_gap_counts

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.performance import test_label_round3_read_budget as budgets
from tests.unit.test_round3_budget_fixture_shapes import assert_unique_metadata


def test_extra_rows_artifacts_have_the_daily_and_weekly_shapes(label_harness):
    h = label_harness
    with h.store() as store:
        budgets.seed_grid(store, postgres=True, case="extra-rows", count=400, source_count=24, provision_keys=False)
        artifacts = store.conn.execute("SELECT * FROM generated_artifacts").fetchall()
        memories = {str(row["id"]): row for row in store.conn.execute("SELECT * FROM memories").fetchall()}
        sources = {str(row["id"]): row for row in store.conn.execute("SELECT * FROM sources").fetchall()}
        gap = label_gap_counts(store)
    daily = [row for row in artifacts if row["artifact_type"] == "daily_brief"]
    weekly = [row for row in artifacts if row["artifact_type"] == "weekly_synthesis"]
    assert (len(daily), len(weekly)) == (250, 250)
    notes = set()
    for row in artifacts:
        metadata = row["metadata_json"]
        assert (row["sensitivity"], row["domain"], row["status"]) == ("public", "project", "draft")
        assert metadata["project_scope"] == [] and metadata["project_floor"] == []
        assert is_derived("artifact", row)
        assert_unique_metadata(metadata)
        notes.add(metadata["capture_note"])
    assert len(notes) == 500
    seen_memory_counts, seen_source_counts = set(), set()
    for row in daily:
        record = row["metadata_json"]["derived_from"]
        assert row["metadata_json"]["workflow"] == "daily_brief"
        assert 1 <= len(record["sources"]) <= 3 and len(record["memories"]) <= 3
        assert set(record["sources"]) <= set(sources) and set(record["memories"]) <= set(memories)
        assert record["counts"] == {"sources": len(record["sources"]), "memories": len(record["memories"]),
                                    "open_loops": 0, "artifacts": 0, "beliefs": 0}
        seen_source_counts.add(len(record["sources"]))
        seen_memory_counts.add(len(record["memories"]))
        assert not has_implicit_weekly_inputs("artifact", row)
        assert dependencies_of("artifact", row) == {("source", item) for item in record["sources"]} | {("memory", item) for item in record["memories"]}
    assert seen_source_counts == {1, 2, 3} and seen_memory_counts == {0, 1, 2, 3}
    for row in weekly:
        metadata = row["metadata_json"]
        summary = metadata["input_summary"]
        assert metadata["workflow"] == "weekly_synthesis" and "derived_from" not in metadata
        assert 1 <= len(summary["source_ids"]) <= 2 and set(summary["source_ids"]) <= set(sources)
        assert len(metadata["candidate_memory_ids"]) == 2
        for candidate in metadata["candidate_memory_ids"]:
            UUID(candidate)
            assert memories[candidate]["metadata_json"]["discovered_by"] == "vnext_weekly_synthesis"
        assert has_implicit_weekly_inputs("artifact", row)
    # Stored below their inputs: the insert floor is not applied, so an unrepaired fixture has a gap.
    assert gap[0] > 0
    hidden = {key for key, row in sources.items() if row["sensitivity"] == "confidential"}
    assert any(set(row["metadata_json"]["derived_from"]["sources"]) & hidden for row in daily)
