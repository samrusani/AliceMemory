"""A locked key's report inputs use the exact project test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pytest

from alicebot_api.vnext_brain import _matches_report_scope
from alicebot_api.vnext_derived_labels import input_admitted, locked_projects, stamp_derived_from

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def test_input_admitted_requires_every_project() -> None:
    alpha = {"metadata_json": {"project_scope": [ALPHA]}}
    shared = {"metadata_json": {"project_scope": [ALPHA, BETA]}}
    global_row = {"metadata_json": {}}
    assert input_admitted("memory", alpha, (ALPHA,)) is True
    assert input_admitted("memory", shared, (ALPHA,)) is False
    assert input_admitted("memory", global_row, (ALPHA,)) is False
    assert input_admitted("memory", alpha, (ALPHA, BETA)) is True


def test_locked_projects_uses_the_binding_when_the_request_is_empty() -> None:
    identity = {"project_scope_locked": True, "project_scope": [ALPHA]}
    assert locked_projects(identity, ()) == (ALPHA,)
    assert locked_projects(identity, (ALPHA,)) == (ALPHA,)
    assert locked_projects({"project_scope_locked": False}, (ALPHA,)) is None
    assert locked_projects(None, (ALPHA,)) is None


def test_a_locked_brief_scope_rejects_a_shared_row() -> None:
    start = datetime(2026, 10, 5, tzinfo=UTC)
    end = start + timedelta(days=1)
    shared = {
        "id": "m",
        "metadata_json": {"project_scope": [ALPHA, BETA]},
        "created_at": start.isoformat(),
    }
    assert _matches_report_scope(
        shared, kind="memory", projects=(ALPHA,), window_start=start, window_end=end
    ) is True
    assert _matches_report_scope(
        shared, kind="memory", projects=(ALPHA,), window_start=start, window_end=end, all_of=(ALPHA,)
    ) is False


@pytest.mark.parametrize("producer", ["brain", "connections", "contradictions"])
def test_empty_projects_still_apply_the_locked_input_gate(producer):
    from alicebot_api import vnext_connections, vnext_contradictions
    row = {"id": "beta", "metadata_json": {"project_scope": [BETA]}, "captured_at": "2026-10-06T09:00:00Z"}
    identity = {"project_scope_locked": True, "project_scope": [ALPHA]}
    all_of = locked_projects(identity, ())
    if producer == "brain":
        start = datetime(2026, 10, 6, tzinfo=UTC)
        assert not _matches_report_scope(row, kind="source", projects=(), all_of=all_of,
                                         window_start=start, window_end=start + timedelta(days=1))
    else:
        module = vnext_connections if producer == "connections" else vnext_contradictions
        assert not module._matches_projects(row, (), source_row=True, all_of=all_of)


def test_stamp_derived_from_counts_match_the_lists() -> None:
    payload: dict[str, object] = {"metadata_json": {"workflow": "daily_brief"}}
    stamp_derived_from(
        payload,
        {
            "sources": [{"id": "s"}],
            "memories": [{"id": "m"}],
            "open_loops": [],
            "artifacts": [],
            "beliefs": [],
        },
    )
    record = payload["metadata_json"]["derived_from"]
    assert record["sources"] == ["s"]
    assert record["counts"]["sources"] == 1
    assert record["counts"]["memories"] == 1


from uuid import UUID
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.routers._vnext_shared import _vnext_authorized_artifact
from alicebot_api.vnext_queue import VNextQueueNotFoundError

def provenance(sources=(), memories=()):
    refs = {'sources': list(sources), 'memories': list(memories), 'open_loops': [], 'artifacts': [], 'beliefs': []}
    return {'v': 1, **refs, 'counts': {k: len(v) for k, v in refs.items()}}


@pytest.mark.parametrize('source_project', [ALPHA, BETA])
def test_locked_weekly_producer_rejects_effective_out_of_scope_artifact(source_project):
    from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
    from tests.unit.test_vnext_brain import InMemoryVNextBrainStore
    from alicebot_api.vnext_label_guard import LabelGuard
    store = InMemoryVNextBrainStore()
    source = {'id': str(UUID(int=500)), 'domain': 'project', 'sensitivity': 'public', 'metadata_json': {'project_scope': [source_project]}}
    artifact = {'id': str(UUID(int=501)), 'artifact_type': 'connection_report', 'title': 'SENTINEL BETA PRIVATE PROJECT', 'domain': 'project', 'sensitivity': 'public', 'created_at': '2026-05-10T09:00:00Z', 'content_markdown': 'Synthetic restricted project title', 'metadata_json': {'workflow': 'connections', 'project_scope': [ALPHA], 'derived_from': provenance(sources=[source['id']])}}
    store.artifacts[artifact['id']] = artifact
    store.get_artifact = lambda artifact_id: store.artifacts.get(artifact_id)
    store.read_label_rows = lambda kind, ids: [source] if kind == 'source' and source['id'] in ids else []
    # Exact read denies the same input for the same locked identity.
    identity = AgentIdentity(agent_id='alpha-key', permission_profile='trusted_local_agent', project_scope=(ALPHA,), project_scope_locked=True)
    store.upsert_agent_identity = lambda *args, **kwargs: None
    if source_project == BETA:
        # The artifact is out of the key's scope through its input, so the key may not read it and the exact door
        # answers as it answers an artifact that does not exist.
        with pytest.raises(VNextQueueNotFoundError):
            _vnext_authorized_artifact(store=store, identity=identity, artifact_id=artifact['id'], action='artifact.read', for_update=False)
    else:
        _vnext_authorized_artifact(store=store, identity=identity, artifact_id=artifact['id'], action='artifact.read', for_update=False)
    effective = LabelGuard(store=store, active=True).effective_row('artifact', artifact)
    assert source_project in effective['metadata_json']['project_floor']
    generated = VNextBrainService(store).generate_weekly_synthesis(BrainArtifactRequest(generated_for='2026-05-10', domains=('project',), projects=(ALPHA,), agent_identity={'agent_id': 'alpha-key', 'permission_profile': 'trusted_local_agent', 'project_scope': [ALPHA], 'project_scope_locked': True}, discover_open_loops=False, create_candidate_memories=False))
    assert ('SENTINEL BETA PRIVATE PROJECT' in generated['content_markdown']) == (source_project == ALPHA)


def test_effective_all_of_keeps_ordinary_overlap_and_returns_originals() -> None:
    from alicebot_api.vnext_label_guard import admit_loaded
    class Store:
        def read_label_rows(self, kind, ids):
            return [{"id": "s", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [BETA]}}] if kind == "source" else []
    row = {"id": "a", "domain": "project", "sensitivity": "public", "metadata_json": {"workflow": "daily_brief", "project_scope": [ALPHA], "derived_from": provenance(sources=["s"])}}
    kwargs = dict(kind="artifact", rows=[row], domains=("project",), sensitivity_allowed=("public",), projects=(ALPHA,))
    assert admit_loaded(Store(), **kwargs) == [row]
    assert admit_loaded(Store(), **kwargs, all_of=(ALPHA,)) == []
    kept = admit_loaded(Store(), **kwargs, all_of=(ALPHA, BETA))
    assert kept[0] is row
