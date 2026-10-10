"""Real round-two producer lifecycle regressions."""
from copy import deepcopy
from uuid import uuid4

import pytest

from alicebot_api.routers import vnext_memories
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from tests.integration.derived_labels_postgres_support import label_harness, today


def projects(h):
    ids = [str(uuid4()), str(uuid4())]
    with h.store() as store:
        for identifier, name in zip(ids, ("Round2 Alpha", "Round2 Beta"), strict=True):
            store.create_project({"id": identifier, "name": name, "slug": name.lower().replace(" ", "-")})
    return ids


def move(h, source, project):
    result = vnext_memories.review_vnext_source(source["id"], vnext_memories.VNextSourceReviewRequest(
        user_id=h.user_id, action="assign_project", project_id=project, confirm_label_hide=True))
    assert result.status_code == 200, result.body


@pytest.mark.parametrize("stale", [False, True])
def test_digest_replays_after_project_moves(label_harness, monkeypatch, stale):
    h = label_harness
    alpha, beta = projects(h)
    source = h.source(scope=(alpha,), text="TODO: Round trip task")
    request = ProjectAutomationRequest(agent_identity=None, project_id=alpha)
    with h.store() as store:
        VNextProjectService(store).extract_open_loops(request, guard=LabelGuard.unlimited(store))
        initial = store.list_open_loops(status=None, limit=20)
    assert len(initial) == 1
    move(h, source, beta)
    if not stale:
        move(h, source, alpha)
    with h.store() as store:
        existing = store.get_open_loop(str(initial[0]["id"]))
        assert existing["project_id"] is None
        if stale:
            monkeypatch.setattr(store, "search_sources", lambda **_kwargs: [deepcopy(source)])
        VNextProjectService(store).extract_open_loops(request, guard=LabelGuard.unlimited(store))
        VNextProjectService(store).extract_open_loops(request, guard=LabelGuard.unlimited(store))
        rows = store.list_open_loops(status=None, limit=20)
        assert [str(row["id"]) for row in rows] == [str(initial[0]["id"])]
        assert set(rows[0]["metadata_json"]["project_floor"]) == {alpha, beta}
        assert label_gap_counts(store) == (0, 0)


def test_weekly_candidate_inherits_report_inputs_before_commit(label_harness):
    h = label_harness
    alpha, beta = projects(h)
    source = h.source(scope=(alpha,))
    h.memory(scope=(alpha,))
    with h.store() as store:
        VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(agent_identity=None, projects=(alpha,), generated_for=today()))
    move(h, source, beta)
    with h.store() as store:
        artifact = VNextBrainService(store).generate_weekly_synthesis(BrainArtifactRequest(
            agent_identity=None, projects=(alpha,), generated_for=today(), create_candidate_memories=True))
        candidates = store.read_label_rows("memory", artifact["metadata_json"]["candidate_memory_ids"])
        assert candidates
        assert set(artifact["metadata_json"]["project_floor"]) == {alpha, beta}
        for candidate in candidates:
            assert set(candidate["metadata_json"]["project_floor"]) == {alpha, beta}
        assert label_gap_counts(store) == (0, 0)
