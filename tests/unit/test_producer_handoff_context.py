"""Independent controls for group floors and optional report context."""
from copy import deepcopy
from uuid import uuid4
import pytest

from alicebot_api.vnext_consolidation import _ClusteringOutcome
from alicebot_api.vnext_rollups import RollupOptions, VNextRollupService
from tests.unit.test_group_scope_sqlite import seed_members
from tests.unit.test_consolidation_report_names_sources import SourceReadingStore, _run, _cluster_citing, _source


def test_rollup_partitions_same_scope_members_by_their_different_floors():
    store = SourceReadingStore()
    first = seed_members(store)
    rows = []
    for floor in ("alpha", "beta"):
        for member in first:
            row = deepcopy(member)
            row["id"] = str(uuid4())
            row["metadata_json"].update(project_scope=[], project_floor=[floor], source_artifact_id=str(uuid4()))
            rows.append(row)
    groups, _, gate, _, _ = VNextRollupService(store)._group_members(
        rows, options=RollupOptions(), exclude_member_id_sets=[])
    assert gate["partition_count"] == 2
    assert groups
    for group in groups:
        assert len({tuple(row["metadata_json"]["project_floor"]) for row in group.members}) == 1


def test_locked_consolidation_context_drops_shared_events_and_ratings(monkeypatch):
    store = SourceReadingStore()
    store.events = [{"id": str(uuid4()), "event_type": "memory.updated", "sensitivity": "internal",
                     "metadata_json": {"project_scope": scope}} for scope in (["alpha"], ["alpha", "beta"])]
    store.list_events = lambda **kwargs: list(store.events)
    store.list_artifact_quality_ratings = lambda **kwargs: [
        {"id": str(uuid4()), "sensitivity": "internal", "metadata_json": {"project_scope": scope}}
        for scope in (["alpha"], ["alpha", "beta"])]
    from alicebot_api.vnext_consolidation import VNextConsolidationService
    monkeypatch.setattr(VNextConsolidationService, "_cluster_memories", lambda *args, **kwargs: _ClusteringOutcome())
    artifact = _run(store, {}, propose_rollups=False,
                    agent_identity={"project_scope_locked": True, "project_scope": ["alpha"]})
    assert "0 artifacts, 1 events, 1 ratings" in artifact["content_markdown"]


@pytest.mark.parametrize("template", ["source:{}", "See {} for notes", "https://example.test/sources/{}"])
def test_locked_consolidation_copies_only_admitted_source_references(template):
    store = SourceReadingStore()
    own = _source(store, metadata_json={"project_scope": ["alpha"]})
    hidden = _source(store, metadata_json={"project_scope": ["beta"]})
    mapping, members = _cluster_citing(store, lambda index: [f"source:{own['id']}", template.format(hidden['id'])])
    for row in store.memories:
        row["metadata_json"]["project_scope"] = ["alpha"]
    before = deepcopy(store.memories)
    artifact = _run(store, mapping, propose_rollups=False,
                    agent_identity={"project_scope_locked": True, "project_scope": ["alpha"]})
    assert str(own["id"]) in str(artifact)
    assert str(hidden["id"]) not in str(artifact)
    for row in store.memories[len(before):]:
        assert str(hidden["id"]) not in str(row)
    assert store.memories[:len(before)] == before


def test_locked_consolidation_preserves_external_urls_with_unknown_incidental_ids():
    store = SourceReadingStore()
    external = "https://example.test/tasks/" + str(uuid4())
    mapping, members = _cluster_citing(store, lambda index: [external])
    for row in store.memories:
        row["metadata_json"]["project_scope"] = ["alpha"]
    artifact = _run(store, mapping, propose_rollups=False,
                    agent_identity={"project_scope_locked": True, "project_scope": ["alpha"]})
    assert external in artifact["metadata_json"]["source_refs"]
