"""List doors, operator screens, and event readers use the effective label."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from alicebot_api.mcp.retrieval import _resume_event_honours_policy_fence
from alicebot_api.routers import vnext_review
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_contradictions import VNextContradictionService
from alicebot_api.vnext_label_guard import apply_sensitivity_ceiling
from alicebot_api.vnext_projects import VNextProjectService
from alicebot_api.vnext_retrieval import VNextRetrievalService

SOURCE_ID = "11111111-1111-1111-1111-111111111111"
MEMORY_ID = "33333333-3333-3333-3333-333333333333"
ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
USER_ID = UUID("44444444-4444-4444-4444-444444444444")
SECRET = "SENTINEL confidential fact"


def _source() -> dict[str, object]:
    return {"id": SOURCE_ID, "domain": "health", "sensitivity": "confidential", "metadata_json": {}}


def _derived_memory() -> dict[str, object]:
    return {
        "id": MEMORY_ID,
        "title": SECRET,
        "canonical_text": SECRET,
        "status": "active",
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": {
            "source_id": SOURCE_ID,
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


class _LabelStore:
    def __init__(self) -> None:
        self.memory = _derived_memory()
        self.artifact = {
            "id": ARTIFACT_ID,
            "title": SECRET,
            "artifact_type": "daily_brief",
            "domain": "project",
            "sensitivity": "confidential",
            "metadata_json": {},
        }
        self.project = {
            "id": "project-1",
            "name": SECRET,
            "domain": "project",
            "sensitivity": "confidential",
            "current_state": SECRET,
            "metadata_json": {},
        }
        self.belief = {
            "id": "belief-1",
            "memory_id": MEMORY_ID,
            "claim": SECRET,
            "quote_belief": SECRET,
            "status": "active",
        }

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        if kind == "source" and SOURCE_ID in ids:
            return [_source()]
        if kind == "memory" and MEMORY_ID in ids:
            return [self.memory]
        return []

    def search_memories_fts(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [self.memory]

    def list_artifacts(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [self.artifact]

    def get_project(self, project_id: str) -> dict[str, object] | None:
        if project_id == self.project["id"]:
            return self.project
        return None

    def search_memories(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return []

    def list_open_loops(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return []

    def get_belief(self, belief_id: str) -> dict[str, object] | None:
        if belief_id == self.belief["id"]:
            return self.belief
        return None

    def list_events(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return []

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        if memory_id == MEMORY_ID:
            return self.memory
        return None

    def get_open_loop(self, loop_id: str) -> dict[str, object] | None:
        del loop_id
        return None


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(agent_id="reader", agent_type="unknown", permission_profile=profile)


def test_fts_stage_drops_a_public_copy_of_a_confidential_source() -> None:
    store = _LabelStore()
    rows, _status = VNextRetrievalService(store)._memory_fts_rows(
        query="sentinel",
        domains=["project"],
        sensitivity_allowed=["public", "internal", "private", "unknown"],
        limit=10,
    )
    assert rows == []
    assert SECRET not in str(rows)


def test_operator_screens_hide_a_confidential_row_from_a_trusted_key() -> None:
    store = _LabelStore()
    trusted = _identity("trusted_local_agent")
    admin = _identity("admin_agent")
    visible_to_trusted = apply_sensitivity_ceiling(store, kind="artifact", rows=[store.artifact], identity=trusted)
    visible_to_admin = apply_sensitivity_ceiling(store, kind="artifact", rows=[store.artifact], identity=admin)
    visible_to_owner = apply_sensitivity_ceiling(store, kind="artifact", rows=[store.artifact], identity=None)
    assert visible_to_trusted == []
    assert [row["id"] for row in visible_to_admin] == [ARTIFACT_ID]
    assert [row["id"] for row in visible_to_owner] == [ARTIFACT_ID]
    assert SECRET not in str(visible_to_trusted)

    for identity in (trusted,):
        try:
            VNextProjectService(store).project_dashboard(project_id="project-1", identity=identity)
        except Exception as exc:
            assert "project-1" not in str(exc) or exc.__class__.__name__ == "VNextProjectValidationError"
            assert SECRET not in str(exc)
        else:
            raise AssertionError("a trusted key saw the confidential project row")
    owner_dashboard = VNextProjectService(store).project_dashboard(project_id="project-1", identity=None)
    admin_dashboard = VNextProjectService(store).project_dashboard(project_id="project-1", identity=admin)
    assert owner_dashboard["project"]["name"] == SECRET
    assert admin_dashboard["counts"] == {"memories": 0, "open_loops": 0, "artifacts": 0}

    try:
        VNextContradictionService(store).belief_state(
            belief_id="belief-1",
            sensitivity_allowed=("public", "internal", "private", "unknown"),
        )
    except Exception as exc:
        assert SECRET not in str(exc)
    else:
        raise AssertionError("a trusted ceiling saw the belief")
    owner_belief = VNextContradictionService(store).belief_state(belief_id="belief-1", sensitivity_allowed=None)
    admin_belief = VNextContradictionService(store).belief_state(belief_id="belief-1", sensitivity_allowed=None)
    assert owner_belief["current"]["claim"] == SECRET
    assert admin_belief["belief_id"] == "belief-1"


def test_artifact_list_route_counts_only_rows_the_caller_may_read(monkeypatch) -> None:
    store = _LabelStore()

    @contextmanager
    def _connection(*args: object, **kwargs: object):
        del args, kwargs
        yield object()

    def _resolve(store_arg: object, *, user_id: object, raw_key: object, payload: object) -> AgentIdentity | None:
        del store_arg, user_id, payload
        if raw_key is None:
            return None
        if raw_key == "trusted":
            return _identity("trusted_local_agent")
        if raw_key == "admin":
            return _identity("admin_agent")
        raise AssertionError(raw_key)

    monkeypatch.setattr(vnext_review, "user_connection", lambda *args, **kwargs: _connection())
    monkeypatch.setattr(vnext_review, "PostgresVNextStore", lambda conn: store)
    monkeypatch.setattr(vnext_review, "resolve_protected_agent_identity", _resolve)
    monkeypatch.setattr(vnext_review, "agent_key_from_authorization", lambda value: value)

    trusted = vnext_review.list_vnext_artifacts(USER_ID, authorization="trusted")
    admin = vnext_review.list_vnext_artifacts(USER_ID, authorization="admin")
    owner = vnext_review.list_vnext_artifacts(USER_ID, authorization=None)
    trusted_body = trusted.body.decode()
    admin_body = admin.body.decode()
    owner_body = owner.body.decode()
    assert ARTIFACT_ID not in trusted_body
    assert SECRET not in trusted_body
    assert '"count":0' in trusted_body or '"count": 0' in trusted_body
    assert ARTIFACT_ID in admin_body
    assert ARTIFACT_ID in owner_body
    assert '"count":1' in admin_body or '"count": 1' in admin_body
    assert '"count":1' in owner_body or '"count": 1' in owner_body


def test_a_labels_raised_event_is_hidden_when_the_target_is_effectively_confidential() -> None:
    store = _LabelStore()
    event = {
        "target_type": "memory",
        "target_id": MEMORY_ID,
        "event_type": "memory.labels_raised",
        "payload_json": {"cause": "repair_v3"},
    }
    assert (
        _resume_event_honours_policy_fence(
            store,
            event,
            effective_domains=("project",),
            effective_sensitivity_allowed=("public", "internal", "private", "unknown"),
            exclude_global_domains=frozenset(),
        )
        is False
    )
    assert (
        _resume_event_honours_policy_fence(
            store,
            event,
            effective_domains=("project", "health"),
            effective_sensitivity_allowed=(
                "public",
                "internal",
                "private",
                "unknown",
                "confidential",
                "highly_sensitive",
                "sacred",
                "regulated",
            ),
            exclude_global_domains=frozenset(),
        )
        is True
    )
