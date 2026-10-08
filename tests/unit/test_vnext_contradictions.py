from __future__ import annotations

import pytest

from alicebot_api.vnext_contradictions import (
    ContradictionFinderRequest,
    VNextContradictionService,
    VNextContradictionValidationError,
)


class InMemoryVNextContradictionStore:
    def __init__(self) -> None:
        self.sources: list[dict[str, object]] = []
        self.memories: list[dict[str, object]] = []
        self.beliefs: dict[str, dict[str, object]] = {}
        self.edges: dict[str, dict[str, object]] = {}
        self.artifacts: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def create_artifact(self, artifact: dict[str, object]) -> dict[str, object]:
        row = {**artifact, "id": f"artifact-{len(self.artifacts) + 1}"}
        self.artifacts[str(row["id"])] = row
        return row

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects=None,
    ) -> dict[str, object] | None:
        del scope_projects
        for row in self.artifacts.values():
            metadata = row.get("metadata_json")
            if (
                row.get("artifact_type") == artifact_type
                and isinstance(metadata, dict)
                and metadata.get("workflow") == workflow
                and metadata.get("idempotency_digest") == digest
            ):
                return row
        return None

    def upsert_artifact_by_workflow_digest(
        self,
        artifact: dict[str, object],
        *,
        workflow: str,
        digest: str,
        actor_type: str = "system",
    ) -> dict[str, object]:
        del actor_type
        existing = self.find_artifact_by_workflow_digest(
            artifact_type=str(artifact["artifact_type"]),
            workflow=workflow,
            digest=digest,
        )
        if existing is not None:
            return existing
        metadata = dict(artifact.get("metadata_json") or {})
        metadata.update({"workflow": workflow, "idempotency_digest": digest})
        return self.create_artifact({**artifact, "metadata_json": metadata})

    def create_edge(self, edge: dict[str, object]) -> dict[str, object]:
        row = {**edge, "id": f"edge-{len(self.edges) + 1}"}
        self.edges[str(row["id"])] = row
        return row

    def upsert_edge_by_idempotency_digest(
        self,
        edge: dict[str, object],
        *,
        digest: str,
        actor_type: str = "system",
    ) -> dict[str, object]:
        del actor_type
        for row in self.edges.values():
            metadata = row.get("metadata_json")
            if isinstance(metadata, dict) and metadata.get("idempotency_digest") == digest:
                return row
        metadata = dict(edge.get("metadata_json") or {})
        metadata["idempotency_digest"] = digest
        return self.create_edge({**edge, "metadata_json": metadata})

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del query
        return _filter_rows(self.sources, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del query
        return _filter_rows(self.memories, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def list_beliefs(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        rows = [row for row in self.beliefs.values() if status is None or row.get("status") == status]
        return _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def get_memories_by_ids(self, memory_ids: tuple[str, ...]) -> list[dict[str, object]]:
        wanted = set(memory_ids)
        return [row for row in self.memories if str(row.get("id")) in wanted]

    def get_belief(self, belief_id: str) -> dict[str, object] | None:
        return self.beliefs.get(belief_id)

    def update_belief_status(
        self,
        *,
        belief_id: str,
        status: str,
        confidence: float | None = None,
        superseded_by: str | None = None,
    ) -> dict[str, object]:
        belief = self.beliefs[belief_id]
        belief["status"] = status
        if confidence is not None:
            belief["confidence"] = confidence
        if superseded_by is not None:
            belief["superseded_by"] = superseded_by
        self.append_event(
            {
                "event_type": "belief.updated",
                "target_type": "belief",
                "target_id": belief_id,
                "payload_json": {"status": status},
            }
        )
        return belief

    def list_events(self, *, target_type: str | None = None, target_id: str | None = None) -> list[dict[str, object]]:
        return [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]


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


def _seed_store() -> InMemoryVNextContradictionStore:
    store = InMemoryVNextContradictionStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Artifact policy note",
            "content_hash": "sha256:abc",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"raw_text": "Alice should not auto-promote generated artifacts into memory."},
        }
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "decision",
            "canonical_text": "Alice should not auto-promote generated artifacts into memory.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    store.beliefs["belief-1"] = {
        "id": "belief-1",
        "memory_id": "belief-memory-1",
        "claim": "Alice should auto-promote generated artifacts into memory.",
        "status": "active",
        "confidence": 0.8,
        "domain": "project",
        "sensitivity": "private",
        "memory_type": "belief",
    }
    return store


def test_contradiction_report_creates_candidate_edges_and_preserves_beliefs() -> None:
    store = _seed_store()

    artifact = VNextContradictionService(store).generate_contradiction_report(
        ContradictionFinderRequest(agent_identity=None, domains=("project",), max_contradictions=2)
    )

    assert artifact["artifact_type"] == "contradiction_report"
    assert artifact["status"] == "needs_review"
    assert artifact["sensitivity"] == "private"
    assert len(artifact["metadata_json"]["candidate_edge_ids"]) == 2
    assert "New claim:" in artifact["content_markdown"]
    assert "Active belief:" in artifact["content_markdown"]
    assert store.beliefs["belief-1"]["status"] == "active"
    first_edge = store.edges["edge-1"]
    assert first_edge["edge_type"] == "contradicts"
    assert first_edge["metadata_json"]["status"] == "candidate"
    contradiction = first_edge["metadata_json"]["contradiction"]
    assert contradiction["recommended_action"] == "review"
    assert "belief:belief-1" in contradiction["provenance"]
    assert [event["event_type"] for event in store.events].count("contradiction.candidate_edge_logged") == 2
    assert store.events[-1]["event_type"] == "artifact.generated"


def test_contradiction_report_filters_sensitivity_and_distinguishes_nuance() -> None:
    store = _seed_store()
    store.sources.append(
        {
            "id": "source-secret",
            "source_type": "manual_text",
            "title": "Secret policy note",
            "content_hash": "sha256:secret",
            "domain": "project",
            "sensitivity": "highly_sensitive",
            "metadata_json": {"raw_text": "Alice should not auto-promote generated artifacts into memory."},
        }
    )
    store.sources[0]["metadata_json"] = {
        "raw_text": "Alice might not auto-promote generated artifacts into memory when context depends."
    }

    artifact = VNextContradictionService(store).generate_contradiction_report(
        ContradictionFinderRequest(agent_identity=None,
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            max_contradictions=2,
        )
    )

    assert "source-secret" not in artifact["content_markdown"]
    nuanced = [
        contradiction
        for contradiction in artifact["metadata_json"]["contradictions"]
        if contradiction["nuance"] == "possible nuance"
    ]
    assert nuanced[0]["recommended_action"] == "request more info"


def test_contradiction_report_enforces_project_scope_for_inputs_and_beliefs() -> None:
    store = InMemoryVNextContradictionStore()
    for project in ("project-a", "project-b"):
        store.sources.append(
            {
                "id": f"source-{project}",
                "source_type": "manual_text",
                "title": "Artifact policy note",
                "content_hash": f"sha256:{project}",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {
                    "project_scope": [project],
                    "raw_text": "Alice should not auto-promote generated artifacts into memory.",
                },
            }
        )
        store.memories.extend(
            [
                {
                    "id": f"memory-{project}",
                    "memory_type": "decision",
                    "canonical_text": "Alice should not auto-promote generated artifacts into memory.",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "private",
                    "project_id": project,
                },
                {
                    "id": f"belief-memory-{project}",
                    "memory_type": "belief",
                    "canonical_text": "Alice should auto-promote generated artifacts into memory.",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "private",
                    "project_id": project,
                },
            ]
        )
        store.beliefs[f"belief-{project}"] = {
            "id": f"belief-{project}",
            "memory_id": f"belief-memory-{project}",
            "claim": "Alice should auto-promote generated artifacts into memory.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
            "memory_type": "belief",
        }

    artifact = VNextContradictionService(store).generate_contradiction_report(
        ContradictionFinderRequest(agent_identity=None,
            domains=("project",),
            projects=("project-a",),
            max_contradictions=4,
        )
    )

    records = artifact["metadata_json"]["contradictions"]
    assert records
    assert {record["belief_id"] for record in records} == {"belief-project-a"}
    assert all("project-b" not in record["source_item"] for record in records)
    assert artifact["metadata_json"]["project_scope"] == ["project-a"]


def test_contradiction_report_source_filter_honors_embedded_canonical_envelope() -> None:
    store = _seed_store()
    store.sources = [
        {
            "id": "source-empty",
            "source_type": "manual_text",
            "title": "Empty canonical source",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": "Alice should not auto-promote generated artifacts into memory.",
                "metadata_json": {"project_scope": []},
            },
        },
        {
            "id": "source-real",
            "source_type": "manual_text",
            "title": "Real canonical source",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": "Alice should not auto-promote generated artifacts into memory.",
                "metadata_json": {"project_scope": ["real"]},
            },
        },
    ]

    artifact = VNextContradictionService(store).generate_contradiction_report(
        ContradictionFinderRequest(agent_identity=None, projects=("real",), max_contradictions=2)
    )

    assert artifact["metadata_json"]["source_ids"] == ["source-real"]


def test_contradiction_report_model_backed_mode_records_source_grounded_metadata() -> None:
    store = _seed_store()

    artifact = VNextContradictionService(store).generate_contradiction_report(
        ContradictionFinderRequest(agent_identity=None,
            domains=("project",),
            max_contradictions=2,
            generation_mode="model_backed",
            model_route_mode="local_only",
        )
    )

    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["metadata_json"]["candidate_edge_ids"] == ["edge-1", "edge-2"]
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert artifact["prompt_hash"].startswith("sha256:")
    assert "## Facts" in artifact["content_markdown"]
    assert "## Contradictions Considered" in artifact["content_markdown"]
    assert "source:source-1" in artifact["content_markdown"]


def test_contradiction_report_logical_retry_replays_artifact_and_edges() -> None:
    store = _seed_store()
    service = VNextContradictionService(store)

    results = [
        service.generate_contradiction_report(
            ContradictionFinderRequest(
                domains=("project",),
                max_contradictions=2,
                generated_by="agent",
                actor_id="hermes",
                trace_id=f"trace-{attempt}",
                run_id=f"run-{attempt}",
                agent_identity={
                    "agent_id": "hermes",
                    "agent_run_id": f"agent-run-{attempt}",
                },
            )
        )
        for attempt in ("a", "b")
    ]

    assert results[0]["id"] == results[1]["id"]
    assert len(store.artifacts) == 1
    assert len(store.edges) == 2
    assert results[0]["metadata_json"]["workflow_digest"] == results[1]["metadata_json"]["workflow_digest"]


def test_belief_review_and_state_history() -> None:
    store = _seed_store()
    service = VNextContradictionService(store)

    challenged = service.review_belief(belief_id="belief-1", action="challenge", confidence=0.35)
    state = service.belief_state(belief_id="belief-1")

    assert challenged["status"] == "challenged"
    assert challenged["confidence"] == 0.35
    assert state["current"]["status"] == "challenged"
    assert "challenged" in state["previous_statuses"]
    assert store.events[-1]["event_type"] == "belief.challenged"

    superseded = service.review_belief(belief_id="belief-1", action="supersede", superseded_by="belief-2")
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == "belief-2"


def test_contradiction_validation_errors() -> None:
    service = VNextContradictionService(InMemoryVNextContradictionStore())

    with pytest.raises(VNextContradictionValidationError, match="max_contradictions"):
        service.generate_contradiction_report(ContradictionFinderRequest(agent_identity=None, max_contradictions=0))

    with pytest.raises(VNextContradictionValidationError, match="belief review action"):
        service.review_belief(belief_id="belief-1", action="delete")

    with pytest.raises(VNextContradictionValidationError, match="was not found"):
        service.belief_state(belief_id="missing")
