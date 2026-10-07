"""Round-three owner compatibility and explicit scope-clamp contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_repair import label_gap_counts, plan_label_repairs, load_postgres_label_tables
from alicebot_api.routers._vnext_shared import _vnext_load_source_trace
from alicebot_api.routers.workspaces import _vnext_workspace_payload
from tests.integration.derived_labels_postgres_support import label_harness


def test_native_count_prefilter_matches_complete_effective_admission(label_harness):
    from tests.unit.test_label_round3_sqlite_reads import assert_native_count_prefilter_matches_complete_effective_admission
    with label_harness.store() as store:
        assert_native_count_prefilter_matches_complete_effective_admission(store)


@pytest.mark.parametrize("profile", ["owner", "admin_agent"])
@pytest.mark.parametrize("component", ["trace", "workspace"])
def test_unfenced_source_trace_and_workspace_preserve_main(label_harness, profile, component):
    h = label_harness
    sources = [h.source(sensitivity=value) for value in ("public", "internal", "private", "unknown", "confidential")]
    source_id = str(sources[0]["id"])
    with h.store() as store:
        store.create_source_chunk({"source_id": source_id, "chunk_index": 0, "text": "Synthetic chunk"})
        store.append_event(build_event_log_record(event_type="open_loop.extraction_completed", actor_type="system",
                                                 payload={"source_id": source_id}))
        identity = None if profile == "owner" else AgentIdentity(agent_id="parity", permission_profile=profile)
        # Default connector records are created on first load. Warm that
        # legitimate side effect before comparing a stable, identical dataset.
        _vnext_workspace_payload(store, identity=identity)
        trace = _vnext_load_source_trace(store=store, source=store.get_source(source_id), identity=identity)
        expected = store.list_events_for_source_trace(source_id=source_id)
        if component == "trace":
            assert {str(row["id"]) for row in trace["events"]} == {str(row["id"]) for row in expected}
            assert {"source_chunk.created", "open_loop.extraction_completed"} <= {row["event_type"] for row in trace["events"]}
        workspace = _vnext_workspace_payload(store, identity=identity)
        checks = {row["name"]: row for row in workspace["doctor"]["checks"]}
        if component == "workspace":
            assert checks["flagged_sources"]["status"] == "pass"
            assert checks["derived_labels"]["status"] == "pass"
            assert workspace["dogfooding"]["captures_today"] == 5
            assert workspace["summary"]["source_count"] == 5
        # Main keeps its four-value display window, but complete totals and
        # embedded diagnostics are unfiltered for these operators.
        assert len(workspace["sources"]) == 4
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    if main:
        probe = Path(__file__).with_name("round3_parity_probe.py")
        completed = subprocess.run([sys.executable, str(probe), main, h.urls["app"], str(h.user_id), profile, source_id],
                                   text=True, capture_output=True, timeout=120)
        assert completed.returncode == 0, completed.stderr
        baseline = json.loads(completed.stdout.strip().splitlines()[-1])
        assert [str(row["id"]) for row in trace["events"]] == [str(row["id"]) for row in baseline["trace"]["events"]]
        assert workspace["summary"] == baseline["workspace"]["summary"]
        assert workspace["dogfooding"] == baseline["workspace"]["dogfooding"]
        baseline_checks = {row["name"]: row for row in baseline["workspace"]["doctor"]["checks"]}
        assert checks["flagged_sources"] == baseline_checks["flagged_sources"]


@pytest.mark.parametrize("legacy_project", [False, True])
def test_owner_scope_assignment_clamps_to_weekly_floor(label_harness, legacy_project):
    from alicebot_api.routers import vnext_memories as router
    from alicebot_api.vnext_derived_labels import with_derived_from
    h = label_harness
    projects = [str(uuid4()), str(uuid4())]
    sources = [h.source(scope=(project,)) for project in projects]
    with h.store() as store:
        for project in projects:
            store.create_project({"id": project, "name": project, "slug": project})
        metadata = with_derived_from({"discovered_by": "vnext_weekly_synthesis"}, {"sources": sources})
        memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Synthetic weekly summary", "status": "candidate",
                                      "domain": "project", "sensitivity": "public", "metadata_json": metadata})
        assert memory["project_scope"] == []
        assert set(memory["metadata_json"]["project_floor"]) == set(projects)
        assert label_gap_counts(store) == (0, 0)
        if legacy_project:
            store.conn.execute("UPDATE memories SET project_id=%s WHERE id=%s::uuid", (projects[0], str(memory["id"])))
    response = router.review_vnext_memory(UUID(str(memory["id"])),
        router.VNextMemoryReviewRequest(user_id=h.user_id, action="assign_project", project_id=projects[0]), authorization=None)
    assert response.status_code == 200, response.body
    body = json.loads(response.body)
    assert body.get("label_floor_applied") is True
    with h.store() as store:
        updated = store.get_memory(str(memory["id"]))
        assert updated["project_scope"] == []
        assert updated["project_id"] is None
        assert set(updated["metadata_json"]["project_floor"]) == set(projects)
        assert label_gap_counts(store) == (0, 0)
        assert plan_label_repairs(load_postgres_label_tables(store.conn)) == []
        raised = [event for event in store.list_events(limit=100) if event["event_type"] == "memory.labels_raised"
                  and event.get("target_id") == str(memory["id"]) and event["payload_json"].get("cause") == "floor_clamped"]
        assert len(raised) == 1


def test_fenced_workspace_keeps_diagnostics_omitted(label_harness):
    h = label_harness
    h.source()
    with h.store() as store:
        payload = _vnext_workspace_payload(store, identity=AgentIdentity(agent_id="fenced", permission_profile="trusted_local_agent"))
        checks = {row["name"]: row for row in payload["doctor"]["checks"]}
        assert checks["flagged_sources"]["status"] == "skipped"
        assert checks["derived_labels"]["status"] == "skipped"


def test_native_memory_refill_crosses_the_legacy_ceiling(label_harness, monkeypatch):
    from alicebot_api.vnext_retrieval import VNextRetrievalService
    from alicebot_api.vnext_derived_labels import with_derived_from
    from alicebot_api.vnext_label_writes import without_insert_floor
    monkeypatch.setattr("alicebot_api.vnext_retrieval.LEGACY_SCOPED_SCAN_MAX_ROWS", 16)
    h = label_harness
    public, hidden = h.source(), h.source(sensitivity="confidential")
    with h.store() as store, without_insert_floor():
        visible = [store.create_memory({"memory_key": str(uuid4()), "canonical_text": "refill observation", "domain": "project", "sensitivity": "public", "status": "active"}) for _ in range(8)]
        metadata = with_derived_from({"workflow": "project_auto_update"}, {"sources": [public, hidden]})
        metadata["derived_from"]["sources"] = [str(public["id"]), str(hidden["id"])]
        for _ in range(100):
            store.create_memory({"memory_key": str(uuid4()), "title": "refill observation", "canonical_text": "refill observation", "domain": "project", "sensitivity": "public", "status": "active", "metadata_json": metadata})
        rows, _ = VNextRetrievalService(store)._memory_fts_rows(query="refill observation", domains=[], sensitivity_allowed=["public", "internal"], limit=8)
        assert {str(row["id"]) for row in rows} == {str(row["id"]) for row in visible}
