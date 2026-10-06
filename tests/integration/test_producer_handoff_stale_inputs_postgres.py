"""Real producers floor UUID-bearing snapshots after a committed source move."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_derived_labels import is_derived
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_source_regeneration import regenerate_source_inputs
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness, today


@pytest.mark.parametrize("producer", ["brief", "project", "regenerate"])
def test_real_producer_payloads_and_stale_uuid_source_floor(label_harness, monkeypatch, producer):
    h = label_harness
    alpha, beta = str(uuid4()), str(uuid4())
    with h.store() as store:
        for project, name in ((alpha, "Alpha"), (beta, "Beta")):
            store.create_project({"id": project, "name": name, "slug": name.lower()})
    source = h.source(scope=(alpha,), text="I prefer synthetic blue ink.\nTODO: Review synthetic draft")
    assert isinstance(source["id"], UUID)
    with h.store() as store:
        store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": 0,
                                   "text": source["metadata_json"]["raw_text"]})
        stale = deepcopy(source)
        if producer == "brief":
            from datetime import UTC, datetime, timedelta
            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            inputs = deepcopy(VNextBrainService(store)._load_inputs(
                BrainArtifactRequest(agent_identity=None, generated_for=today()),
                window_start=start, window_end=start + timedelta(days=1)))
    from alicebot_api.routers import vnext_memories
    moved = vnext_memories.review_vnext_source(source["id"], vnext_memories.VNextSourceReviewRequest(
        user_id=h.user_id, action="assign_project", sensitivity="confidential", domain="health",
        project_id=beta, confirm_label_hide=True))
    assert moved.status_code == 200, moved.body
    with h.store() as store:
        assert store.get_source(str(source["id"]))["metadata_json"]["project_scope"] == [beta]
    observed = []
    for kind, method in (("memory", "create_memory"), ("open_loop", "create_open_loop"), ("artifact", "create_artifact")):
        original = getattr(PostgresVNextStore, method)
        def spy(self, payload, *args, _kind=kind, _original=original, **kwargs):
            assert is_derived(_kind, payload), (_kind, payload)
            observed.append(_kind)
            return _original(self, payload, *args, **kwargs)
        monkeypatch.setattr(PostgresVNextStore, method, spy)
    with h.store() as store:
        if producer == "brief":
            monkeypatch.setattr(VNextBrainService, "_load_inputs", lambda *_args, **_kwargs: deepcopy(inputs))
            VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(
                agent_identity=None, generated_for=today(), discover_open_loops=True))
        elif producer == "project":
            monkeypatch.setattr(store, "search_sources", lambda **_kwargs: [deepcopy(stale)])
            VNextProjectService(store).extract_open_loops(ProjectAutomationRequest(agent_identity=None, project_id=alpha))
        else:
            regenerate_source_inputs(store, stale)
        assert "open_loop" in observed
        loops = store.list_open_loops(status=None, sensitivity_allowed=["confidential"], limit=50)
        assert loops
        for loop in loops:
            assert loop["sensitivity"] == "confidential"
            assert loop["domain"] == "health"
            assert set(loop["metadata_json"]["project_floor"]) == {alpha, beta}, (producer, alpha, beta, loop)
            assert str(source["id"]) in loop["metadata_json"]["derived_from"]["sources"]
        assert label_gap_counts(store) == (0, 0)
