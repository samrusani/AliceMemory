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


def test_a_cited_memory_is_judged_by_its_source() -> None:
    store = _LabelStore()
    with pytest.raises(MemoryRefNotFoundError):
        resolve_attachable_memory_id(store, MEMORY_ID, fence=SourceReadFence.for_identity(_trusted()))
