from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_projects import (
    PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE,
    ProjectAutomationRequest,
    VNextProjectService,
    VNextProjectTerminalConsistencyError,
    VNextProjectValidationError,
)


class InMemoryVNextProjectStore:
    def __init__(self) -> None:
        self.projects: dict[str, dict[str, object]] = {}
        self.sources: list[dict[str, object]] = []
        self.memories: dict[str, dict[str, object]] = {}
        self.open_loops: dict[str, dict[str, object]] = {}
        self.artifacts: dict[str, dict[str, object]] = {}
        self.revisions: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.cleared_embedding_ids: list[str] = []
        self.fact_key_updates: list[tuple[str, str | None]] = []
        self.artifact_lock_calls: list[str] = []
        self.project_update_event_lookup_calls: list[tuple[str, str]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, object]]:
        return [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]

    def list_project_update_events(
        self,
        *,
        artifact_id: str,
        candidate_memory_id: str,
    ) -> list[dict[str, object]]:
        self.project_update_event_lookup_calls.append((artifact_id, candidate_memory_id))
        event_types = {
            "project.update_candidate_created",
            "project.update_candidate_accepted",
            "project.update_candidate_rejected",
        }
        rows: list[dict[str, object]] = []
        for event in self.events:
            if event.get("event_type") not in event_types:
                continue
            payload_value = event.get("payload_json")
            payload = payload_value if isinstance(payload_value, dict) else {}
            if (
                (event.get("target_type") == "artifact" and str(event.get("target_id") or "") == artifact_id)
                or (event.get("target_type") == "memory" and str(event.get("target_id") or "") == candidate_memory_id)
                or str(payload.get("artifact_id") or "") == artifact_id
                or str(payload.get("candidate_memory_id") or "") == candidate_memory_id
                or str(payload.get("memory_id") or "") == candidate_memory_id
            ):
                rows.append(event)
        return rows

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        """Mirror narrow dependency reads for this fixture's stored rows."""

        from alicebot_api.vnext_derived_labels import identifier

        rows = {
            "source": self.sources,
            "memory": list(self.memories.values()),
            "open_loop": list(self.open_loops.values()),
            "artifact": list(self.artifacts.values()),
            "project": list(self.projects.values()),
        }.get(kind, [])
        wanted = {identifier(value) for value in ids}
        fields = ("id", "user_id", "domain", "sensitivity", "metadata_json", "value", "project_id", "source_id", "memory_id", "status", "memory_type", "artifact_type")
        return [
            {field: row[field] for field in fields if field in row}
            for row in rows if identifier(row.get("id")) in wanted
        ]

    def create_artifact(self, artifact: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**artifact, "id": f"artifact-{len(self.artifacts) + 1}"}
        self.artifacts[str(row["id"])] = row
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        self.artifact_lock_calls.append(artifact_id)
        return self.artifacts.get(artifact_id)

    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        metadata_json: dict[str, object] | None = None,
        **_kwargs,
    ) -> dict[str, object] | None:
        artifact = self.artifacts[artifact_id]
        if expected_status is not None and artifact.get("status") != expected_status:
            return None
        artifact["status"] = status
        if metadata_json is not None:
            metadata = artifact.setdefault("metadata_json", {})
            assert isinstance(metadata, dict)
            metadata.update(metadata_json)
        return artifact

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories[str(row["id"])] = row
        return row

    def update_memory(self, *, memory_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        memory = self.memories[memory_id]
        memory.update(patch)
        return memory

    def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
        return self.memories.get(memory_id)

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return self.memories.get(memory_id)

    def clear_memory_embedding(self, *, memory_id: str) -> dict[str, object] | None:
        memory = self.memories.get(memory_id)
        if memory is None:
            return None
        self.cleared_embedding_ids.append(memory_id)
        memory["embedding"] = None
        metadata = memory.get("metadata_json")
        if isinstance(metadata, dict):
            metadata.pop("_alice_embedding", None)
        return memory

    def update_memory_fact_keys(
        self,
        *,
        memory_id: str,
        fact_keys: str | None,
    ) -> dict[str, object] | None:
        memory = self.memories.get(memory_id)
        if memory is None:
            return None
        self.fact_key_updates.append((memory_id, fact_keys))
        memory["fact_keys"] = fact_keys
        return memory

    def append_revision(self, revision: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**revision, "id": f"revision-{len(self.revisions) + 1}"}
        self.revisions.append(row)
        return row

    def list_revisions(self, memory_id: str) -> list[dict[str, object]]:
        return [revision for revision in self.revisions if revision.get("memory_id") == memory_id]

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return self.projects.get(project_id)

    def get_project_for_update(self, project_id: str) -> dict[str, object] | None:
        return self.projects.get(project_id)

    def list_projects(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        rows = [row for row in self.projects.values() if status is None or row.get("status") == status]
        return _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def update_project(self, *, project_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        project = self.projects[project_id]
        project.update(patch)
        return project

    def create_open_loop(self, loop: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**loop, "id": f"loop-{len(self.open_loops) + 1}", "status": loop.get("status", "open")}
        self.open_loops[str(row["id"])] = row
        return row

    def get_open_loop(self, loop_id: str) -> dict[str, object] | None:
        return self.open_loops.get(loop_id)

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        project_id: str | None = None,
        person_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        rows = [
            row
            for row in self.open_loops.values()
            if (status is None or row.get("status") == status)
            and (project_id is None or row.get("project_id") == project_id)
            and (person_id is None or row.get("person_id") == person_id)
        ]
        return _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def update_open_loop(self, *, loop_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        loop = self.open_loops[loop_id]
        loop.update(patch)
        return loop

    def update_open_loop_status(
        self,
        *,
        loop_id: str,
        status: str,
        resolution_note: str | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        loop = self.open_loops[loop_id]
        loop["status"] = status
        if resolution_note is not None:
            loop["resolution_note"] = resolution_note
        return loop

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        scope_projects: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        del query
        rows = _filter_rows(self.sources, domains=domains, sensitivity_allowed=sensitivity_allowed)
        if scope_projects:
            rows = [
                row
                for row in rows
                if set(row.get("metadata_json", {}).get("project_scope", [])) & set(scope_projects)  # type: ignore[union-attr]
            ]
        return rows[:limit]

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        projects: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        del query
        rows = _filter_rows(list(self.memories.values()), domains=domains, sensitivity_allowed=sensitivity_allowed)
        if projects:
            rows = [
                row
                for row in rows
                if set(row.get("metadata_json", {}).get("project_scope", [])) & set(projects)  # type: ignore[union-attr]
            ]
        return rows[:limit]

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        scope_projects: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        rows = [
            row for row in self.artifacts.values() if artifact_type is None or row.get("artifact_type") == artifact_type
        ]
        rows = _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)
        if scope_projects:
            rows = [
                row
                for row in rows
                if set(row.get("metadata_json", {}).get("project_scope", [])) & set(scope_projects)  # type: ignore[union-attr]
            ]
        return rows[:limit]

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: tuple[str, ...] | None = None,
    ) -> dict[str, object] | None:
        for row in self.artifacts.values():
            metadata = row.get("metadata_json")
            if row.get("artifact_type") != artifact_type or not isinstance(metadata, dict):
                continue
            if metadata.get("workflow") != workflow:
                continue
            if metadata.get("automation_digest") != digest and metadata.get("consolidation_digest") != digest:
                continue
            if scope_projects and not set(metadata.get("project_scope", [])) & set(scope_projects):
                continue
            return row
        return None

    def find_open_loop_by_automation_digest(
        self,
        *,
        digest: str,
        project_id: str | None = None,
        person_id: str | None = None,
    ) -> dict[str, object] | None:
        for row in self.open_loops.values():
            metadata = row.get("metadata_json")
            if not isinstance(metadata, dict) or metadata.get("automation_digest") != digest:
                continue
            if project_id is not None and row.get("project_id") != project_id:
                continue
            if person_id is not None and row.get("person_id") != person_id:
                continue
            return row
        return None


def _filter_rows(
    rows: list[dict[str, object]],
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in rows:
        domain = row.get("domain")
        sensitivity = row.get("sensitivity")
        if domains is not None and isinstance(domain, str) and domain not in domains and domain != "unknown":
            continue
        if sensitivity_allowed is not None and isinstance(sensitivity, str) and sensitivity not in sensitivity_allowed:
            continue
        output.append(row)
    return output


def _seed_store() -> InMemoryVNextProjectStore:
    store = InMemoryVNextProjectStore()
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 seed complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Alice project note",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T09:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_scope": ["project-1"],
                "raw_text": (
                    "Project: Alice vNext now needs project auto-update review.\n"
                    "TODO: validate project dashboard Owner: Jordan\n"
                    "Waiting on: UI decision Owner: Designer"
                ),
            },
        }
    )
    store.memories["memory-existing"] = {
        "id": "memory-existing",
        "memory_type": "project_state",
        "canonical_text": "Alice vNext has project automation scope.",
        "status": "active",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"project_scope": ["project-1"]},
    }
    return store


def test_project_update_candidate_creates_reviewable_artifact_and_candidate_memory() -> None:
    store = _seed_store()

    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )

    assert artifact["artifact_type"] == "project_update"
    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["project_id"] == "project-1"
    assert artifact["metadata_json"]["candidate_memory_id"] == "memory-2"
    assert "Project Update Candidate - Alice vNext" in artifact["content_markdown"]
    assert store.memories["memory-2"]["status"] == "candidate"
    assert store.events[-1]["event_type"] == "project.update_candidate_created"


def test_project_update_candidate_model_backed_mode_is_review_only_and_source_grounded() -> None:
    store = _seed_store()

    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None,
            project_id="project-1",
            domains=("project",),
            generation_mode="model_backed",
            model_route_mode="local_only",
        )
    )

    assert artifact["status"] == "needs_review"
    assert store.memories["memory-2"]["status"] == "candidate"
    assert artifact["metadata_json"]["workflow_type"] == "project_update_scan"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert artifact["prompt_hash"].startswith("sha256:")
    assert "## Facts" in artifact["content_markdown"]
    assert "## Open Questions" in artifact["content_markdown"]
    assert "source:source-1" in artifact["content_markdown"]


def test_project_automation_and_dashboard_never_mix_same_domain_projects() -> None:
    store = _seed_store()
    store.projects["project-2"] = {
        **store.projects["project-1"],
        "id": "project-2",
        "name": "Hermes",
        "slug": "hermes",
    }
    store.sources.append(
        {
            "id": "source-project-2",
            "title": "Hermes private plan",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_scope": ["project-2"],
                "raw_text": "Project: Hermes secret launch date.",
            },
        }
    )
    store.memories["memory-project-2"] = {
        "id": "memory-project-2",
        "memory_type": "project_state",
        "canonical_text": "Hermes secret launch date.",
        "status": "active",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"project_scope": ["project-2"]},
    }
    store.artifacts["artifact-project-2"] = {
        "id": "artifact-project-2",
        "artifact_type": "project_update",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"project_scope": ["project-2"]},
    }

    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    dashboard = VNextProjectService(store).project_dashboard(project_id="project-1")

    assert "source-project-2" not in artifact["metadata_json"]["source_ids"]
    assert "memory-project-2" not in artifact["metadata_json"]["memory_ids"]
    assert all(row["id"] != "memory-project-2" for row in dashboard["memories"])
    assert all(row["id"] != "artifact-project-2" for row in dashboard["artifacts"])


def test_project_automation_resolves_source_envelope_before_filter_digest_and_loop_scope() -> None:
    store = _seed_store()
    store.sources = [
        {
            "id": "source-empty",
            "title": "Empty canonical source",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": "TODO: ignore stale source",
                "metadata_json": {"project_scope": []},
            },
        },
        {
            "id": "source-real",
            "title": "Real canonical source",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": "TODO: ship canonical source",
                "metadata_json": {"project_scope": ["project-1"]},
            },
        },
    ]

    def unscoped_sources(**kwargs) -> list[dict[str, object]]:
        return store.sources[: int(kwargs.get("limit", 8))]

    store.search_sources = unscoped_sources  # type: ignore[method-assign]
    service = VNextProjectService(store)
    request = ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))

    artifact = service.generate_project_update_candidate(request)
    source_metadata = store.sources[1]["metadata_json"]
    assert isinstance(source_metadata, dict)
    source_metadata["project_id"] = "different-stale-alias"
    replay = service.generate_project_update_candidate(request)
    loops = service.extract_open_loops(request)

    assert artifact["metadata_json"]["source_ids"] == ["source-real"]
    assert replay["id"] == artifact["id"]
    assert [loop["source_id"] for loop in loops] == ["source-real"]
    assert loops[0]["metadata_json"]["project_scope"] == ["project-1"]


def test_direct_project_workflows_are_idempotent_for_unchanged_evidence() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    request = ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))

    first_artifact = service.generate_project_update_candidate(request)
    first_loops = service.extract_open_loops(request)
    for index in range(150):
        store.artifacts[f"decoy-artifact-{index}"] = {
            "id": f"decoy-artifact-{index}",
            "artifact_type": "project_update",
            "metadata_json": {
                "workflow": "project_auto_update",
                "project_scope": ["project-1"],
                "automation_digest": f"decoy-{index}",
            },
        }
        store.open_loops[f"decoy-loop-{index}"] = {
            "id": f"decoy-loop-{index}",
            "project_id": "project-1",
            "metadata_json": {"automation_digest": f"decoy-{index}"},
        }
    second_artifact = service.generate_project_update_candidate(request)
    second_loops = service.extract_open_loops(request)

    assert second_artifact["id"] == first_artifact["id"]
    assert len([row for row in store.artifacts.values() if row.get("id") == first_artifact["id"]]) == 1
    assert len([row for row in store.memories.values() if row.get("status") == "candidate"]) == 1
    assert [row["id"] for row in second_loops] == [row["id"] for row in first_loops]
    assert len([row for row in store.open_loops.values() if row.get("title")]) == 2


def test_a_candidate_loop_description_never_holds_the_id_of_its_source() -> None:
    """A candidate loop says which source it came from in its ``description``. For a source with no title the text used
    to fall back to the id of the source, so the id of a source the reader of the loop may not read (the loop's
    ``source_id`` column is withheld from that reader, and so is the id in ``metadata_json``) was still shown in the free
    text, to a project key after the source was deleted or reclassified. The text now says the source has no title. A
    source with a title is named as before. The loop still carries the id in its ``source_id`` column, the digest that
    dedupes the loop does not read the description, and a second run returns the loop it made the first time.

    Mutation: put the id back as the fallback, in ``_open_loop_candidates`` (``_title(source)`` with no ``fallback``).
    """

    store = _seed_store()
    untitled = str(uuid4())
    store.sources.append(
        {
            "id": untitled,
            "source_type": "manual_text",
            "captured_at": "2026-05-10T09:30:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"project_scope": ["project-1"], "raw_text": "TODO: file the kiln report"},
        }
    )
    service = VNextProjectService(store)
    request = ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))

    first = service.extract_open_loops(request)
    descriptions: dict[str, list[object]] = {}
    for loop in first:
        descriptions.setdefault(str(loop["source_id"]), []).append(loop["description"])
    assert sorted(descriptions["source-1"]) == [
        "Candidate task discovered from source Alice project note.",
        "Candidate waiting_on_person discovered from source Alice project note.",
    ]
    assert descriptions[untitled] == ["Candidate task discovered from a source with no title."]
    assert set(descriptions) == {"source-1", untitled}
    for loop in first:
        assert untitled not in json.dumps({key: loop.get(key) for key in ("title", "description", "resolution_note")})
    second = service.extract_open_loops(request)
    assert [row["id"] for row in second] == [row["id"] for row in first]
    assert len(store.open_loops) == len(first)


def test_project_update_digest_changes_when_behavior_config_changes() -> None:
    store = _seed_store()
    service = VNextProjectService(store)

    first = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",), max_items=8)
    )
    second = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",), max_items=7)
    )

    assert second["id"] != first["id"]
    assert second["metadata_json"]["automation_digest"] != first["metadata_json"]["automation_digest"]


def test_accepting_project_update_updates_project_promotes_memory_and_appends_revision() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )

    reviewed = service.review_project_update(
        artifact_id=str(artifact["id"]),
        action="edit",
        edited_current_state="Alice vNext project automation is under review.",
        actor_type="agent",
        actor_id="reviewer-agent",
        trace_id="trace-review",
        run_id="run-review",
    )

    assert reviewed["status"] == "accepted"
    assert store.artifact_lock_calls == [str(artifact["id"])]
    assert store.projects["project-1"]["current_state"] == "Alice vNext project automation is under review."
    accepted_memory = store.memories["memory-2"]
    assert accepted_memory["status"] == "active"
    assert accepted_memory["canonical_text"] == "Alice vNext project automation is under review."
    assert accepted_memory["summary"] == "Alice vNext project automation is under review."
    assert accepted_memory["confirmation_status"] == "confirmed"
    assert accepted_memory["value"]["suggested_current_state"] == ("Alice vNext project automation is under review.")
    assert accepted_memory["metadata_json"]["candidate"] is False
    assert accepted_memory["metadata_json"]["review_status"] == "accepted"
    assert reviewed["metadata_json"]["accepted_current_state"] == ("Alice vNext project automation is under review.")
    assert store.revisions[0]["memory_id"] == "memory-2"
    assert store.revisions[0]["memory_key"] == store.memories["memory-2"]["memory_key"]
    assert store.revisions[0]["revision_type"] == "edited"
    assert store.revisions[0]["actor_type"] == "agent"
    assert store.revisions[0]["actor_id"] == "reviewer-agent"
    assert store.events[-1]["event_type"] == "project.update_candidate_accepted"
    assert store.events[-1]["actor_type"] == "agent"
    assert store.events[-1]["actor_id"] == "reviewer-agent"
    assert store.events[-1]["trace_id"] == "trace-review"
    assert store.events[-1]["run_id"] == "run-review"


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_central_artifact_review_dispatches_project_updates_to_coupled_lifecycle(action: str) -> None:
    store = _seed_store()
    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])

    result = dispatch_vnext_artifact_review(
        store,
        artifact_id=str(artifact["id"]),
        action=action,
        actor_type="user",
        actor_id="reviewer-user",
    )

    expected_status = "accepted" if action == "accept" else "rejected"
    expected_memory_status = "active" if action == "accept" else "rejected"
    assert result.artifact["status"] == expected_status
    assert store.memories[candidate_memory_id]["status"] == expected_memory_status
    if action == "accept":
        assert store.projects["project-1"]["current_state"] == metadata["suggested_current_state"]
    else:
        assert store.projects["project-1"]["current_state"] == "Sprint 7 seed complete."
    assert store.events[-1]["actor_type"] == "user"
    assert store.events[-1]["actor_id"] == "reviewer-user"


def test_central_artifact_review_does_not_treat_project_update_promote_as_generic_promotion() -> None:
    store = _seed_store()
    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])
    project_before = deepcopy(store.projects["project-1"])
    memory_before = deepcopy(store.memories[candidate_memory_id])

    with pytest.raises(VNextProjectValidationError, match="accept, edit, or reject"):
        dispatch_vnext_artifact_review(store, artifact_id=str(artifact["id"]), action="promote")

    assert store.projects["project-1"] == project_before
    assert store.memories[candidate_memory_id] == memory_before
    assert store.artifacts[str(artifact["id"])]["status"] == "needs_review"


def test_central_artifact_review_dispatch_propagates_candidate_supersession_guard() -> None:
    store = _seed_store()
    artifact = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact_metadata = artifact["metadata_json"]
    assert isinstance(artifact_metadata, dict)
    candidate_memory_id = str(artifact_metadata["candidate_memory_id"])
    candidate = store.memories[candidate_memory_id]
    candidate["superseded_by"] = "replacement-memory"

    with pytest.raises(VNextProjectValidationError, match="already been superseded"):
        dispatch_vnext_artifact_review(
            store,
            artifact_id=str(artifact["id"]),
            action="accept",
        )

    assert store.artifacts[str(artifact["id"])]["status"] == "needs_review"
    assert store.projects["project-1"]["current_state"] == "Sprint 7 seed complete."
    assert store.memories[candidate_memory_id]["status"] == "candidate"


def test_project_update_scope_linkage_compares_canonical_project_identity() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact_metadata = artifact["metadata_json"]
    assert isinstance(artifact_metadata, dict)
    candidate = store.memories[str(artifact_metadata["candidate_memory_id"])]
    candidate_metadata = candidate["metadata_json"]
    assert isinstance(candidate_metadata, dict)
    artifact_metadata["project_scope"] = [" PROJECT-1 "]
    candidate_metadata["project_scope"] = ["project-1", "PROJECT-1"]

    reviewed = service.review_project_update(artifact_id=str(artifact["id"]), action="accept")

    assert reviewed["status"] == "accepted"


@pytest.mark.parametrize("action", ["accept", "edit", "reject"])
@pytest.mark.parametrize(
    "marker",
    [
        "column_pointer",
        "legacy_metadata_pointer",
        "review_lifecycle",
        "consolidation_lifecycle",
        "lifecycle_history",
    ],
)
def test_project_update_review_rejects_superseded_candidate_markers_without_mutation(
    action: str,
    marker: str,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact_id = str(artifact["id"])
    artifact_metadata = artifact["metadata_json"]
    assert isinstance(artifact_metadata, dict)
    candidate_memory_id = str(artifact_metadata["candidate_memory_id"])
    candidate = store.memories[candidate_memory_id]
    candidate_metadata = candidate["metadata_json"]
    assert isinstance(candidate_metadata, dict)

    if marker == "column_pointer":
        candidate["superseded_by"] = "replacement-memory"
    elif marker == "legacy_metadata_pointer":
        candidate_metadata["superseded_by"] = "replacement-memory"
    elif marker == "review_lifecycle":
        candidate_metadata["agentic_memory"] = {"lifecycle_status": "review_superseded"}
    elif marker == "consolidation_lifecycle":
        candidate_metadata["agentic_memory"] = {"lifecycle_status": "superseded_by_consolidation"}
    elif marker == "lifecycle_history":
        candidate_metadata["agentic_memory"] = {
            "lifecycle_status": "pending_dashboard_review",
            "lifecycle_history": [{"status": "review_superseded"}],
        }
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(marker)

    project_before = deepcopy(store.projects["project-1"])
    artifact_before = deepcopy(artifact)
    candidate_before = deepcopy(candidate)
    revisions_before = deepcopy(store.revisions)
    events_before = deepcopy(store.events)

    with pytest.raises(VNextProjectValidationError, match="already been superseded"):
        service.review_project_update(
            artifact_id=artifact_id,
            action=action,
            edited_current_state="This edit must never be applied." if action == "edit" else None,
        )

    assert store.projects["project-1"] == project_before
    assert store.artifacts[artifact_id] == artifact_before
    assert store.memories[candidate_memory_id] == candidate_before
    assert store.revisions == revisions_before
    assert store.events == events_before


@pytest.mark.parametrize(
    "mismatch",
    [
        "artifact_type",
        "artifact_workflow",
        "candidate_status",
        "candidate_memory_type",
        "candidate_workflow",
        "candidate_marker",
        "candidate_digest",
        "candidate_root_project",
        "candidate_metadata_project",
        "candidate_value_project",
        "artifact_scope",
        "artifact_scope_empty",
        "candidate_scope",
        "candidate_scope_empty",
    ],
)
def test_project_update_review_rejects_every_candidate_linkage_mismatch_without_mutation(mismatch: str) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact_id = str(artifact["id"])
    artifact_metadata = artifact["metadata_json"]
    assert isinstance(artifact_metadata, dict)
    candidate_memory_id = str(artifact_metadata["candidate_memory_id"])
    candidate = store.memories[candidate_memory_id]
    candidate_metadata = candidate["metadata_json"]
    candidate_value = candidate["value"]
    assert isinstance(candidate_metadata, dict)
    assert isinstance(candidate_value, dict)

    if mismatch == "artifact_type":
        artifact["artifact_type"] = "daily_brief"
    elif mismatch == "artifact_workflow":
        artifact_metadata["workflow"] = "other_workflow"
    elif mismatch == "candidate_status":
        candidate["status"] = "rejected"
    elif mismatch == "candidate_memory_type":
        candidate["memory_type"] = "semantic"
    elif mismatch == "candidate_workflow":
        candidate_metadata["workflow"] = "other_workflow"
    elif mismatch == "candidate_marker":
        candidate_metadata["candidate"] = False
    elif mismatch == "candidate_digest":
        candidate_metadata["automation_digest"] = "sha256:different"
    elif mismatch == "candidate_root_project":
        candidate["project_id"] = "project-2"
    elif mismatch == "candidate_metadata_project":
        candidate_metadata["project_id"] = "project-2"
    elif mismatch == "candidate_value_project":
        candidate_value["project_id"] = "project-2"
    elif mismatch == "artifact_scope":
        artifact_metadata["project_scope"] = ["project-2"]
    elif mismatch == "artifact_scope_empty":
        artifact_metadata["project_scope"] = []
    elif mismatch == "candidate_scope":
        candidate_metadata["project_scope"] = ["project-2"]
    elif mismatch == "candidate_scope_empty":
        candidate_metadata["project_scope"] = []
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(mismatch)

    project_before = deepcopy(store.projects["project-1"])
    artifact_before = deepcopy(artifact)
    candidate_before = deepcopy(candidate)
    revisions_before = deepcopy(store.revisions)
    events_before = deepcopy(store.events)

    with pytest.raises(VNextProjectValidationError):
        service.review_project_update(artifact_id=artifact_id, action="accept")

    assert store.projects["project-1"] == project_before
    assert store.artifacts[artifact_id] == artifact_before
    assert store.memories[candidate_memory_id] == candidate_before
    assert store.revisions == revisions_before
    assert store.events == events_before


def test_project_update_review_refreshes_content_derived_indexes() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    memory_id = str(artifact["metadata_json"]["candidate_memory_id"])
    memory = store.memories[memory_id]
    memory["embedding"] = [0.1, 0.2]
    metadata = memory.setdefault("metadata_json", {})
    assert isinstance(metadata, dict)
    metadata["_alice_embedding"] = {"content_sha256": "stale"}

    service.review_project_update(
        artifact_id=str(artifact["id"]),
        action="edit",
        edited_current_state="Alice Bike-a-Thon raised $5,000 for the release fundraiser.",
    )

    assert store.cleared_embedding_ids == [memory_id]
    assert memory["embedding"] is None
    refreshed_metadata = memory["metadata_json"]
    assert isinstance(refreshed_metadata, dict)
    assert "_alice_embedding" not in refreshed_metadata
    assert store.fact_key_updates[-1][0] == memory_id
    assert "dollars" in str(memory["fact_keys"])


def test_project_update_review_can_defer_embedding_until_after_commit() -> None:
    store = _seed_store()
    service = VNextProjectService(store, defer_embeddings=True)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )

    service.review_project_update(
        artifact_id=str(artifact["id"]),
        action="edit",
        edited_current_state="Deferred project-state embedding.",
    )

    assert len(service.deferred_embedding_inputs) == 1
    deferred = service.deferred_embedding_inputs[0]
    assert deferred.memory_id == artifact["metadata_json"]["candidate_memory_id"]
    assert deferred.canonical_text == "Deferred project-state embedding."


def test_rejecting_project_update_logs_rejection_without_updating_project() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )

    reviewed = service.review_project_update(artifact_id=str(artifact["id"]), action="reject")

    assert reviewed["status"] == "rejected"
    assert store.projects["project-1"]["current_state"] == "Sprint 7 seed complete."
    assert store.memories["memory-2"]["status"] == "rejected"
    assert store.revisions[-1]["revision_type"] == "rejected"
    assert store.events[-1]["event_type"] == "project.update_candidate_rejected"

    assert service.review_project_update(artifact_id=str(artifact["id"]), action="reject") == reviewed
    with pytest.raises(VNextProjectValidationError, match="cannot be accepted"):
        service.review_project_update(artifact_id=str(artifact["id"]), action="accept")


@pytest.mark.parametrize("memory_key", [None, "", " \t\n"])
def test_rejecting_project_update_requires_memory_key_before_any_mutation(memory_key: object) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact_metadata = artifact["metadata_json"]
    assert isinstance(artifact_metadata, dict)
    candidate_memory_id = str(artifact_metadata["candidate_memory_id"])
    store.memories[candidate_memory_id]["memory_key"] = memory_key
    state_before = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    with pytest.raises(VNextProjectValidationError, match="^candidate memory is missing memory_key$"):
        service.review_project_update(artifact_id=str(artifact["id"]), action="reject")

    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_terminal_project_update_replay_uses_one_coupled_event_lookup(action: str) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action=action)
    metadata = terminal["metadata_json"]
    assert isinstance(metadata, dict)
    artifact_id = str(terminal["id"])
    candidate_memory_id = str(metadata["candidate_memory_id"])
    assert store.project_update_event_lookup_calls == []

    assert service.review_project_update(artifact_id=artifact_id, action=action) == terminal

    assert store.project_update_event_lookup_calls == [(artifact_id, candidate_memory_id)]


@pytest.mark.parametrize(
    ("forced_status", "retry_action"),
    [("accepted", "accept"), ("rejected", "reject")],
)
def test_project_update_forced_terminal_status_fails_closed_without_mutation(
    forced_status: str,
    retry_action: str,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    artifact["status"] = forced_status
    artifact_id = str(artifact["id"])
    project_before = deepcopy(store.projects)
    memories_before = deepcopy(store.memories)
    artifacts_before = deepcopy(store.artifacts)
    revisions_before = deepcopy(store.revisions)
    events_before = deepcopy(store.events)

    with pytest.raises(VNextProjectTerminalConsistencyError) as excinfo:
        service.review_project_update(artifact_id=artifact_id, action=retry_action)

    assert str(excinfo.value) == PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE
    assert store.projects == project_before
    assert store.memories == memories_before
    assert store.artifacts == artifacts_before
    assert store.revisions == revisions_before
    assert store.events == events_before


@pytest.mark.parametrize(
    ("action", "corruption"),
    [
        ("accept", "artifact_id"),
        ("accept", "artifact_workflow"),
        ("accept", "artifact_project"),
        ("accept", "artifact_linkage"),
        ("accept", "artifact_candidate"),
        ("accept", "artifact_digest"),
        ("accept", "artifact_review_action"),
        ("accept", "artifact_outcome"),
        ("accept", "revision_linkage"),
        ("accept", "revision_action"),
        ("accept", "revision_type"),
        ("accept", "revision_before"),
        ("accept", "revision_after"),
        ("accept", "revision_actor"),
        ("accept", "event_linkage"),
        ("accept", "event_actor"),
        ("accept", "creation_linkage"),
        ("accept", "creation_target"),
        ("accept", "creation_target_type"),
        ("accept", "partial_creation_redaction"),
        ("accept", "redacted_creation_with_integrity_hash"),
        ("accept", "missing_creation_event"),
        ("accept", "competing_creation_target"),
        ("accept", "partial_revision_redaction"),
        ("accept", "partial_event_redaction"),
        ("accept", "redacted_event_with_integrity_hash"),
        ("accept", "extra_wrong_review_revision"),
        ("accept", "duplicate_revision"),
        ("accept", "duplicate_event"),
        ("reject", "artifact_id"),
        ("reject", "artifact_workflow"),
        ("reject", "artifact_project"),
        ("reject", "artifact_linkage"),
        ("reject", "artifact_candidate"),
        ("reject", "artifact_digest"),
        ("reject", "artifact_review_action"),
        ("reject", "artifact_outcome"),
        ("reject", "revision_linkage"),
        ("reject", "revision_action"),
        ("reject", "revision_type"),
        ("reject", "revision_before"),
        ("reject", "revision_after"),
        ("reject", "revision_actor"),
        ("reject", "event_linkage"),
        ("reject", "event_actor"),
        ("reject", "event_source_ids"),
        ("reject", "creation_linkage"),
        ("reject", "creation_target"),
        ("reject", "creation_target_type"),
        ("reject", "partial_creation_redaction"),
        ("reject", "redacted_creation_with_integrity_hash"),
        ("reject", "missing_creation_event"),
        ("reject", "competing_creation_target"),
        ("reject", "partial_revision_redaction"),
        ("reject", "partial_event_redaction"),
        ("reject", "redacted_event_with_integrity_hash"),
        ("reject", "extra_wrong_review_revision"),
        ("reject", "duplicate_revision"),
        ("reject", "duplicate_event"),
    ],
)
def test_project_update_terminal_consistency_requires_every_immutable_evidence_leg_without_mutation(
    action: str,
    corruption: str,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action=action)
    artifact_id = str(terminal["id"])
    terminal_metadata = terminal["metadata_json"]
    assert isinstance(terminal_metadata, dict)
    candidate_memory_id = str(terminal_metadata["candidate_memory_id"])
    review_revision = next(
        revision
        for revision in store.revisions
        if revision.get("memory_id") == candidate_memory_id and revision.get("action") == "project_update_review"
    )
    revision_metadata = review_revision["metadata_json"]
    assert isinstance(revision_metadata, dict)
    review_event = next(
        event for event in store.events if event.get("event_type") == f"project.update_candidate_{terminal['status']}"
    )
    event_payload = review_event["payload_json"]
    assert isinstance(event_payload, dict)
    creation_event = next(
        event for event in store.events if event.get("event_type") == "project.update_candidate_created"
    )
    creation_payload = creation_event["payload_json"]
    assert isinstance(creation_payload, dict)

    if corruption == "artifact_id":
        terminal["id"] = "different-artifact"
    elif corruption == "artifact_workflow":
        terminal_metadata["workflow"] = "different-workflow"
    elif corruption == "artifact_project":
        terminal_metadata["project_id"] = "different-project"
    elif corruption == "artifact_linkage":
        terminal_metadata["candidate_memory_id"] = "missing-candidate-memory"
    elif corruption == "artifact_candidate":
        terminal_metadata["candidate"] = True
    elif corruption == "artifact_digest":
        terminal_metadata["automation_digest"] = ""
    elif corruption == "artifact_review_action":
        terminal_metadata["review_action"] = "edit" if action == "accept" else "accept"
    elif corruption == "artifact_outcome":
        terminal_metadata["review_status"] = "rejected" if action == "accept" else "accepted"
    elif corruption == "revision_linkage":
        revision_metadata["artifact_id"] = "different-artifact"
    elif corruption == "revision_action":
        review_revision["action"] = "different_action"
    elif corruption == "revision_type":
        review_revision["revision_type"] = "rejected" if action == "accept" else "promoted"
    elif corruption == "revision_before":
        review_revision["text_before"] = ""
    elif corruption == "revision_after":
        review_revision["text_after"] = "Different reviewed state."
    elif corruption == "revision_actor":
        review_revision["actor_id"] = "different-reviewer"
    elif corruption == "event_linkage":
        event_payload["artifact_id" if action == "accept" else "project_id"] = "different-link"
    elif corruption == "event_actor":
        review_event["actor_id"] = "different-reviewer"
    elif corruption == "event_source_ids":
        event_payload["source_ids"] = ["different-source"]
    elif corruption == "creation_linkage":
        creation_payload["candidate_memory_id"] = "different-candidate-memory"
    elif corruption == "creation_target":
        creation_event["target_id"] = "different-artifact"
    elif corruption == "creation_target_type":
        creation_event["target_type"] = "memory"
    elif corruption == "partial_creation_redaction":
        creation_event["payload_json"] = {"redacted": True, "memory_id": candidate_memory_id}
        creation_event["integrity_hash"] = None
    elif corruption == "redacted_creation_with_integrity_hash":
        creation_event["payload_json"] = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": "project.update_candidate_created",
        }
        creation_event["integrity_hash"] = "sha256:stale-payload-hash"
    elif corruption == "missing_creation_event":
        store.events.remove(creation_event)
    elif corruption == "competing_creation_target":
        competing_creation = deepcopy(creation_event)
        competing_creation["target_id"] = "different-artifact"
        store.events.append(competing_creation)
    elif corruption == "partial_revision_redaction":
        review_revision["metadata_json"] = {"redacted": True}
        review_revision["text_before"] = "[REDACTED]"
    elif corruption == "partial_event_redaction":
        review_event["payload_json"] = {"redacted": True, "memory_id": candidate_memory_id}
    elif corruption == "redacted_event_with_integrity_hash":
        review_event["payload_json"] = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": f"project.update_candidate_{terminal['status']}",
        }
        review_event["integrity_hash"] = "sha256:stale-payload-hash"
    elif corruption == "extra_wrong_review_revision":
        extra_revision = deepcopy(review_revision)
        extra_revision["revision_type"] = "rejected" if action == "accept" else "promoted"
        store.revisions.append(extra_revision)
    elif corruption == "duplicate_revision":
        store.revisions.append(deepcopy(review_revision))
    elif corruption == "duplicate_event":
        store.events.append(deepcopy(review_event))
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(corruption)

    project_before = deepcopy(store.projects)
    memories_before = deepcopy(store.memories)
    artifacts_before = deepcopy(store.artifacts)
    revisions_before = deepcopy(store.revisions)
    events_before = deepcopy(store.events)

    with pytest.raises(VNextProjectTerminalConsistencyError) as excinfo:
        service.review_project_update(artifact_id=artifact_id, action=action)

    assert str(excinfo.value) == PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE
    assert store.projects == project_before
    assert store.memories == memories_before
    assert store.artifacts == artifacts_before
    assert store.revisions == revisions_before
    assert store.events == events_before


def _redact_project_update_terminal_evidence(
    store: InMemoryVNextProjectStore,
    *,
    terminal: dict[str, object],
) -> None:
    metadata = terminal["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])
    artifact_id = str(terminal["id"])
    project_id = str(metadata["project_id"])
    review_action = str(metadata["review_action"])
    terminal.update(
        {
            "title": "[REDACTED]",
            "content_markdown": "[REDACTED]",
            "prompt_hash": None,
            "model_info_json": {"redacted": True},
            "metadata_json": {
                "redacted": True,
                "redacted_at": "2026-07-16T00:00:00Z",
                "workflow": "project_auto_update",
                "project_id": project_id,
                "project_scope": [project_id],
                "candidate_memory_id": candidate_memory_id,
                "review_action": review_action,
            },
        }
    )
    for revision in store.revisions:
        if (
            str(revision.get("memory_id") or "") == candidate_memory_id
            and revision.get("action") == "project_update_review"
        ):
            revision.update(
                {
                    "memory_key": f"redacted.{candidate_memory_id}",
                    "source_event_ids": [],
                    "previous_value": None if revision.get("previous_value") is None else {"redacted": True},
                    "new_value": None if revision.get("new_value") is None else {"redacted": True},
                    "candidate": {"redacted": True},
                    "metadata_json": {"redacted": True},
                    "text_before": "[REDACTED]",
                    "text_after": "[REDACTED]",
                    "reason": "[REDACTED]",
                }
            )
    for event in store.events:
        payload = event.get("payload_json")
        if not isinstance(payload, dict):
            continue
        if (
            str(payload.get("candidate_memory_id") or "") != candidate_memory_id
            and str(payload.get("memory_id") or "") != candidate_memory_id
            and not (event.get("target_type") == "memory" and str(event.get("target_id") or "") == candidate_memory_id)
            and not (event.get("target_type") == "artifact" and str(event.get("target_id") or "") == artifact_id)
            and str(payload.get("artifact_id") or "") != artifact_id
        ):
            continue
        event["payload_json"] = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": event["event_type"],
        }
        event["integrity_hash"] = None


def test_project_update_terminal_replay_rejects_clone_after_authorized_true_redaction_without_mutation() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action="accept")
    _redact_project_update_terminal_evidence(store, terminal=terminal)
    cloned_artifact = deepcopy(terminal)
    cloned_artifact["id"] = "artifact-clone"
    store.artifacts["artifact-clone"] = cloned_artifact
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    with pytest.raises(VNextProjectTerminalConsistencyError) as excinfo:
        service.review_project_update(artifact_id="artifact-clone", action="accept")

    assert str(excinfo.value) == PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("redacted", [False, True])
def test_project_update_terminal_replay_allows_repeated_creation_rows_for_one_artifact_without_mutation(
    redacted: bool,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action="accept")
    creation_event = next(
        event for event in store.events if event.get("event_type") == "project.update_candidate_created"
    )
    repeated_creation = deepcopy(creation_event)
    repeated_creation["id"] = "repeated-creation-event"
    store.events.append(repeated_creation)
    if redacted:
        _redact_project_update_terminal_evidence(store, terminal=terminal)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    assert service.review_project_update(artifact_id=str(artifact["id"]), action="accept") == terminal
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


def _append_conflicting_project_update_decision(
    store: InMemoryVNextProjectStore,
    *,
    terminal: dict[str, object],
    conflict: str,
) -> None:
    metadata = terminal["metadata_json"]
    assert isinstance(metadata, dict)
    artifact_id = str(terminal["id"])
    candidate_memory_id = str(metadata["candidate_memory_id"])
    project_id = str(metadata["project_id"])
    review_event = next(
        event for event in store.events if event.get("event_type") == f"project.update_candidate_{terminal['status']}"
    )
    event_type: str
    target_type: str
    target_id: str
    payload: dict[str, object]
    if conflict == "accepted_plus_rejected":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {
            "project_id": project_id,
            "source_ids": list(metadata["source_ids"]),
        }
    elif conflict == "candidate_linked_accepted_wrong_action":
        event_type = "project.update_candidate_accepted"
        target_type = "project"
        target_id = project_id
        payload = {
            "candidate_memory_id": candidate_memory_id,
            "action": "reject",
        }
    elif conflict == "rejected_plus_conflicting_rejection":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {
            "project_id": project_id,
            "source_ids": ["conflicting-source"],
        }
    elif conflict == "rejected_plus_accepted":
        event_type = "project.update_candidate_accepted"
        target_type = "project"
        target_id = project_id
        payload = {
            "artifact_id": artifact_id,
            "candidate_memory_id": candidate_memory_id,
            "action": "accept",
        }
    elif conflict == "accepted_linked_wrong_actor":
        event_type = "project.update_candidate_accepted"
        target_type = "project"
        target_id = project_id
        payload = {
            "artifact_id": artifact_id,
            "candidate_memory_id": candidate_memory_id,
            "action": "accept",
        }
    elif conflict == "accepted_linked_wrong_target":
        event_type = "project.update_candidate_accepted"
        target_type = "memory"
        target_id = candidate_memory_id
        payload = {
            "artifact_id": artifact_id,
            "candidate_memory_id": candidate_memory_id,
            "action": "accept",
        }
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(conflict)
    actor_type = str(review_event["actor_type"])
    actor_id = review_event.get("actor_id")
    if conflict == "accepted_linked_wrong_actor":
        actor_type = "agent" if actor_type != "agent" else "user"
        actor_id = "conflicting-reviewer"
    store.append_event(
        build_event_log_record(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id is not None else None,
            target_type=target_type,
            target_id=target_id,
            trace_id=str(review_event["trace_id"]) if review_event.get("trace_id") is not None else None,
            run_id=str(review_event["run_id"]) if review_event.get("run_id") is not None else None,
            payload=payload,
        )
    )


@pytest.mark.parametrize(
    ("action", "conflict"),
    [
        ("accept", "accepted_plus_rejected"),
        ("accept", "candidate_linked_accepted_wrong_action"),
        ("reject", "rejected_plus_conflicting_rejection"),
        ("reject", "rejected_plus_accepted"),
        ("accept", "accepted_linked_wrong_actor"),
        ("accept", "accepted_linked_wrong_target"),
    ],
)
def test_project_update_terminal_replay_rejects_every_coupled_competing_decision_without_mutation(
    action: str,
    conflict: str,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action=action)
    _append_conflicting_project_update_decision(store, terminal=terminal, conflict=conflict)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    with pytest.raises(VNextProjectTerminalConsistencyError) as excinfo:
        service.review_project_update(artifact_id=str(terminal["id"]), action=action)

    assert str(excinfo.value) == PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("operation", ["correct", "undo", "forget"])
def test_accepted_project_update_terminal_replay_ignores_supported_memory_lifecycle_evolution(
    operation: str,
) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action="accept")
    terminal_metadata = terminal["metadata_json"]
    assert isinstance(terminal_metadata, dict)
    candidate_memory_id = str(terminal_metadata["candidate_memory_id"])
    memory_service = VNextMemoryCommitService(store)  # type: ignore[arg-type]
    if operation == "correct":
        memory_service.correct(
            identity=None,
            memory_id=candidate_memory_id,
            canonical_text="Later corrected project-update memory text.",
            reason="Exercise a supported post-review correction.",
        )
    elif operation == "undo":
        memory_service.undo(
            identity=None,
            memory_id=candidate_memory_id,
            reason="Exercise a supported post-review undo.",
        )
    else:
        memory_service.forget(
            identity=None,
            memory_id=candidate_memory_id,
            reason="Exercise a supported post-review forget.",
        )
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    assert service.review_project_update(artifact_id=str(artifact["id"]), action="accept") == terminal
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


def test_accepted_project_update_replay_survives_a_genuine_later_project_update() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    first = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    accepted_first = service.review_project_update(artifact_id=str(first["id"]), action="accept")
    second = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    assert second["id"] != first["id"]
    service.review_project_update(
        artifact_id=str(second["id"]),
        action="edit",
        edited_current_state="Later accepted project state B.",
    )
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    assert service.review_project_update(artifact_id=str(first["id"]), action="accept") == accepted_first
    assert store.projects["project-1"]["current_state"] == "Later accepted project state B."
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_project_update_consistent_terminal_outcome_remains_idempotent_without_mutation(action: str) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )
    terminal = service.review_project_update(artifact_id=str(artifact["id"]), action=action)
    project_before = deepcopy(store.projects)
    memories_before = deepcopy(store.memories)
    artifacts_before = deepcopy(store.artifacts)
    revisions_before = deepcopy(store.revisions)
    events_before = deepcopy(store.events)

    assert service.review_project_update(artifact_id=str(artifact["id"]), action=action) == terminal
    assert store.projects == project_before
    assert store.memories == memories_before
    assert store.artifacts == artifacts_before
    assert store.revisions == revisions_before
    assert store.events == events_before


def test_accepted_project_update_cannot_later_be_rejected() -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",))
    )

    accepted = service.review_project_update(artifact_id=str(artifact["id"]), action="accept")

    assert service.review_project_update(artifact_id=str(artifact["id"]), action="accept") == accepted
    with pytest.raises(VNextProjectValidationError, match="cannot be rejected"):
        service.review_project_update(artifact_id=str(artifact["id"]), action="reject")


def test_open_loop_extraction_and_review_support_source_owner_and_filters() -> None:
    store = _seed_store()
    service = VNextProjectService(store)

    loops = service.extract_open_loops(ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",)))
    snoozed = service.review_open_loop(loop_id="loop-1", action="snooze", due_at="2026-05-12T09:00:00Z")
    closed = service.review_open_loop(loop_id="loop-2", action="close", resolution_note="Decision captured.")
    dashboard = service.project_dashboard(project_id="project-1")

    assert [loop["metadata_json"]["loop_type"] for loop in loops] == ["task", "waiting_on_person"]
    assert loops[0]["source_id"] == "source-1"
    assert loops[0]["metadata_json"]["source_captured_at"] == "2026-05-10T09:00:00Z"
    assert loops[0]["metadata_json"]["owner"] == "Jordan"
    assert snoozed["due_at"] == "2026-05-12T09:00:00Z"
    assert closed["status"] == "resolved"
    assert dashboard["project"]["id"] == "project-1"
    assert dashboard["counts"]["open_loops"] == 1



@pytest.mark.parametrize("parent_change", ["missing", "restricted"])
def test_dashboard_withholds_loops_after_their_source_becomes_unreadable(parent_change) -> None:
    store = _seed_store()
    service = VNextProjectService(store)
    loops = service.extract_open_loops(ProjectAutomationRequest(agent_identity=None, project_id="project-1", domains=("project",)))
    assert service.project_dashboard(project_id="project-1")["counts"]["open_loops"] == 2
    if parent_change == "missing":
        store.sources.clear()
    else:
        store.sources[0]["domain"] = "health"
        store.sources[0]["sensitivity"] = "regulated"
    dashboard = service.project_dashboard(project_id="project-1")
    assert dashboard["counts"]["open_loops"] == 0
    assert dashboard["open_loops"] == []
    assert all(loop["source_id"] == "source-1" for loop in loops)


def test_project_service_validation_errors() -> None:
    service = VNextProjectService(InMemoryVNextProjectStore())

    with pytest.raises(VNextProjectValidationError, match="max_items"):
        service.extract_open_loops(ProjectAutomationRequest(agent_identity=None, max_items=0))

    with pytest.raises(VNextProjectValidationError, match="no active project"):
        service.generate_project_update_candidate(ProjectAutomationRequest(agent_identity=None, ))

    store = _seed_store()
    service = VNextProjectService(store)
    artifact = service.generate_project_update_candidate(ProjectAutomationRequest(agent_identity=None, project_id="project-1"))
    with pytest.raises(VNextProjectValidationError, match="edited_current_state"):
        service.review_project_update(artifact_id=str(artifact["id"]), action="edit")
