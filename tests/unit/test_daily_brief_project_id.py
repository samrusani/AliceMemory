"""Candidate open loops keep a free-form project name out of project_id."""

from __future__ import annotations

from uuid import UUID

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService, _stored_open_loop_project_id


class _Store:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def create_open_loop(self, payload: dict, *, actor_type: str = "system") -> dict:
        self.payloads.append(payload)
        return {"id": "loop", **payload}


def _source(projects: list[str]) -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "title": "Note",
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {"project_scope": projects},
    }


def test_a_free_form_name_is_not_a_stored_project_id() -> None:
    """Mutation: return ``project_scope[0]`` for a one-item scope. ``Alice`` is then the project id."""

    assert _stored_open_loop_project_id(("Alice",)) is None
    assert _stored_open_loop_project_id(("prj_0123456789abcdef",)) is None
    assert _stored_open_loop_project_id(("Alice", "Bob")) is None
    canonical = "11111111-1111-1111-1111-111111111111"
    assert _stored_open_loop_project_id((canonical.upper(),)) == canonical
    assert _stored_open_loop_project_id((str(UUID(canonical)),)) == canonical

    store = _Store()
    VNextBrainService(store)._create_candidate_open_loops(
        BrainArtifactRequest(),
        [("publish the note", _source(["Alice"]))],
        workflow_digest="digest",
    )
    payload = store.payloads[0]
    assert payload["project_id"] is None
    assert payload["metadata_json"]["project_scope"] == ["Alice"]
