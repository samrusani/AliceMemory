"""Exact doors judge a derived row by the labels of its inputs."""

from __future__ import annotations

import pytest

from alicebot_api.routers._vnext_shared import _vnext_authorized_artifact
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.vnext_label_guard import LabelGuard, apply_unverified_rule
from alicebot_api.vnext_source_fence import (
    MemoryRefNotFoundError,
    SourceReadFence,
    resolve_attachable_memory_id,
)


ALPHA = "prj_" + "a" * 16
SOURCE_ID = "11111111-1111-1111-1111-111111111111"
ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
MEMORY_ID = "33333333-3333-3333-3333-333333333333"


class _LabelStore:
    def __init__(self) -> None:
        self.source = {
            "id": SOURCE_ID,
            "user_id": "label-guard",
            "domain": "health",
            "sensitivity": "confidential",
            "metadata_json": {"project_scope": [ALPHA]},
        }
        self.artifact = {
            "id": ARTIFACT_ID,
            "domain": "unknown",
            "sensitivity": "public",
            "content_markdown": "secret health note",
            "metadata_json": {
                "workflow": "daily_brief",
                "project_scope": [ALPHA],
                "derived_from": {
                    "v": 1,
                    "sources": [SOURCE_ID],
                    "memories": [],
                    "open_loops": [],
                    "artifacts": [],
                    "beliefs": [],
                    "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                },
            },
        }
        self.memory = {
            "id": MEMORY_ID,
            "domain": "unknown",
            "sensitivity": "public",
            "canonical_text": "copied",
            "metadata_json": {
                "source_id": SOURCE_ID,
                "project_scope": [ALPHA],
                "derived_from": {
                    "v": 1,
                    "sources": [SOURCE_ID],
                    "memories": [],
                    "open_loops": [],
                    "artifacts": [],
                    "beliefs": [],
                    "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                },
            },
        }
        self.events: list[dict[str, object]] = []

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        if kind == "source" and SOURCE_ID in ids:
            return [self.source]
        return []

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifact if artifact_id == ARTIFACT_ID else None

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return self.memory if memory_id == MEMORY_ID else None

    def upsert_agent_identity(self, *_args: object, **_kwargs: object) -> None:
        return None

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


def _trusted() -> AgentIdentity:
    return AgentIdentity(agent_id="trusted-key", permission_profile="trusted_local_agent")


def _locked_admin() -> AgentIdentity:
    return AgentIdentity(
        agent_id="bound-admin",
        permission_profile="admin_agent",
        project_scope=(ALPHA,),
        project_scope_locked=True,
    )


def test_an_exact_artifact_door_uses_the_input_label() -> None:
    store = _LabelStore()
    with pytest.raises(AgentPolicyBlockedError):
        _vnext_authorized_artifact(
            store=store,  # type: ignore[arg-type]
            identity=_trusted(),
            artifact_id=ARTIFACT_ID,
            action="artifact.read",
            for_update=False,
        )


def test_the_owner_guard_does_not_read() -> None:
    store = _LabelStore()
    guard = LabelGuard.for_fence(store, SourceReadFence.for_identity(None))
    assert guard.active is False
    assert guard.effective_row("artifact", store.artifact) is store.artifact


def test_a_locked_key_is_refused_an_unverified_row() -> None:
    store = _LabelStore()
    store.artifact["metadata_json"] = {
        "workflow": "daily_brief",
        "project_scope": [ALPHA],
        "derived_from": {
            "v": 1,
            "sources": ["99999999-9999-9999-9999-999999999999"],
            "memories": [],
            "open_loops": [],
            "artifacts": [],
            "beliefs": [],
            "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
        },
    }
    identity = _locked_admin()
    guard = LabelGuard.for_fence(store, SourceReadFence.for_identity(identity))
    effective = guard.effective_row("artifact", store.artifact)
    assert isinstance(effective, dict)
    assert effective["unverified"] is True
    from alicebot_api.vnext_agent_control import PolicyDecision

    blocked = apply_unverified_rule(
        PolicyDecision(decision="allowed", action="artifact.read", permission_profile="admin_agent"),
        effective,
        identity,
    )
    assert blocked.decision == "blocked"
    assert "derived_labels_unverified" in blocked.reasons


def test_explain_refuses_a_public_copy_of_a_confidential_source() -> None:
    from alicebot_api.mcp.evidence_artifacts import (
        _ExplainAuthorizationError,
        _authorize_explain_resource,
    )

    store = _LabelStore()
    with pytest.raises(_ExplainAuthorizationError):
        _authorize_explain_resource(
            store,
            identity=_trusted(),
            resource=store.memory,
            project_scope=(ALPHA,),
            target_type="memory",
            target_id=MEMORY_ID,
        )


def test_the_legacy_artifact_authorizer_uses_the_input_label() -> None:
    from alicebot_api.mcp.evidence_artifacts import _authorize_vnext_artifact_target

    store = _LabelStore()
    _artifact, _actor_type, _actor_id, decision = _authorize_vnext_artifact_target(
        store,  # type: ignore[arg-type]
        identity=_trusted(),
        artifact_id=ARTIFACT_ID,
        action="artifact.lookup",
        for_update=False,
    )
    assert decision.decision == "blocked"


def test_a_memory_review_decision_uses_the_input_label() -> None:
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    store = _LabelStore()
    decision = VNextMemoryCommitService(store)._write_policy_decision(  # noqa: SLF001
        identity=_trusted(),
        action="memory.review",
        memory=store.memory,
    )
    assert decision.decision == "blocked"


def test_bound_admin_review_uses_the_effective_input_project_floor() -> None:
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    store = _LabelStore()
    store.source["metadata_json"]["project_scope"] = ["prj_" + "b" * 16]
    identity = _locked_admin()
    service = VNextMemoryCommitService(store)
    assert service._write_policy_decision(identity=identity, action="memory.review", memory=store.memory).decision == "blocked"
    store.source["metadata_json"]["project_scope"] = [ALPHA]
    assert service._write_policy_decision(identity=identity, action="memory.review", memory=store.memory).decision != "blocked"


def test_an_open_loop_update_uses_the_input_label() -> None:
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    store = _LabelStore()
    loop = {
        "id": "44444444-4444-4444-4444-444444444444",
        "title": "loop",
        "domain": "unknown",
        "sensitivity": "public",
        "metadata_json": {
            "discovered_by": "vnext_daily_brief",
            "source_id": SOURCE_ID,
            "project_scope": [ALPHA],
            "derived_from": {
                "v": 1,
                "sources": [SOURCE_ID],
                "memories": [],
                "open_loops": [],
                "artifacts": [],
                "beliefs": [],
                "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
            },
        },
    }
    with pytest.raises(AgentPolicyBlockedError):
        VNextMemoryCommitService(store).authorize_memory_action(
            identity=_trusted(),
            action="open_loop.update",
            memory=loop,
            target_type="open_loop",
        )


def test_a_cited_memory_is_judged_by_its_source() -> None:
    store = _LabelStore()
    with pytest.raises(MemoryRefNotFoundError):
        resolve_attachable_memory_id(store, MEMORY_ID, fence=SourceReadFence.for_identity(_trusted()))


from uuid import UUID

def provenance(sources=(), memories=()):
    refs = {'sources': list(sources), 'memories': list(memories), 'open_loops': [], 'artifacts': [], 'beliefs': []}
    return {'v': 1, **refs, 'counts': {k: len(v) for k, v in refs.items()}}


class ArtifactStore:
    def __init__(self, n):
        self.sources = [dict(id=str(UUID(int=i + 1)), domain='project', sensitivity='public', metadata_json={}) for i in range(n)]
        self.artifact = dict(id=str(UUID(int=10000)), artifact_type='daily_brief', domain='project', sensitivity='public', content_markdown='Public report', metadata_json={'workflow': 'daily_brief', 'derived_from': provenance(sources=[s['id'] for s in self.sources])})
    def read_label_rows(self, kind, ids):
        return [s for s in self.sources if s['id'] in ids] if kind == 'source' else []
    def get_artifact(self, artifact_id):
        return self.artifact
    def upsert_agent_identity(self, *args, **kwargs):
        pass
    def append_event(self, event):
        return event


@pytest.mark.parametrize('n', [31, 32, 100])
def test_public_report_fanout_remains_readable(n):
    store = ArtifactStore(n)
    result = _vnext_authorized_artifact(store=store, identity=AgentIdentity(agent_id='trusted-key', permission_profile='trusted_local_agent'), artifact_id=store.artifact['id'], action='artifact.read', for_update=False)
    assert result is not None


def test_cached_guard_keeps_each_roots_independent_budget() -> None:
    store = ArtifactStore(100)
    guard = LabelGuard(store, active=True)
    assert guard.effective_row("artifact", store.artifact)["unverified"] is False
    assert guard.effective_row("artifact", store.artifact)["unverified"] is False


def test_belief_dependency_loads_backing_memory_ancestry() -> None:
    store = _LabelStore()
    belief_id = str(UUID(int=55))
    belief = {"id": belief_id, "memory_id": MEMORY_ID, "domain": "unknown", "sensitivity": "public", "metadata_json": {}}
    artifact = dict(store.artifact)
    artifact["metadata_json"] = {"workflow": "daily_brief", "derived_from": provenance()}
    artifact["metadata_json"]["derived_from"]["beliefs"] = [belief_id]
    artifact["metadata_json"]["derived_from"]["counts"]["beliefs"] = 1
    def reader(kind, ids):
        rows = {"source": [store.source], "memory": [store.memory], "belief": [belief]}.get(kind, [])
        return [row for row in rows if row["id"] in ids]
    store.read_label_rows = reader
    effective = LabelGuard(store, active=True).effective_row("artifact", artifact)
    assert effective["unverified"] is False
    assert effective["domain"] == "health"
    assert effective["sensitivity"] == "confidential"


def test_depth_boundary_and_cache_are_independent_of_width() -> None:
    from alicebot_api.vnext_derived_labels import HOP_BOUND
    # Extracted memories recursively reference memories through consolidation markers.
    class Chain:
        def __init__(self, length):
            self.rows = [{"id": str(i), "domain": "project", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [str(i+1)]}}} for i in range(length)]
            self.rows.append({"id": str(length), "domain": "project", "sensitivity": "public", "metadata_json": {}})
        def read_label_rows(self, kind, ids):
            return [row for row in self.rows if row["id"] in ids] if kind == "memory" else []
    within = Chain(HOP_BOUND)
    guard = LabelGuard(within, active=True)
    assert guard.effective_row("memory", within.rows[0])["unverified"] is False
    assert guard.effective_row("memory", within.rows[0])["unverified"] is False
    beyond = Chain(HOP_BOUND+1)
    assert LabelGuard(beyond, active=True).effective_row("memory", beyond.rows[0])["unverified"] is True


def test_ambiguous_source_alias_blocks_locked_admin() -> None:
    store = _LabelStore()
    store.source["metadata_json"] = {"project_scope": ["prj_" + "b" * 16]}
    twin = {**store.source, "id": "{" + SOURCE_ID + "}", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}}
    store.read_label_rows = lambda kind, ids: [store.source, twin] if kind == "source" else []
    effective = LabelGuard(store, active=True).effective_row("artifact", store.artifact)
    assert effective["unverified"] is True
    identity = AgentIdentity(agent_id="locked-admin", permission_profile="admin", project_scope=(ALPHA,), project_scope_locked=True)
    with pytest.raises(AgentPolicyBlockedError):
        _vnext_authorized_artifact(store=store, identity=identity, artifact_id=ARTIFACT_ID, action="artifact.read", for_update=False)
