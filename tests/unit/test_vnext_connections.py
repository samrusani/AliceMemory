from __future__ import annotations

import pytest

from alicebot_api.vnext_connections import (
    ConnectionFinderRequest,
    VNextConnectionService,
    VNextConnectionValidationError,
)


class InMemoryVNextConnectionStore:
    def __init__(self) -> None:
        self.sources: list[dict[str, object]] = []
        self.memories: list[dict[str, object]] = []
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

    def update_edge_status(self, *, edge_id: str, status: str) -> dict[str, object]:
        edge = self.edges[edge_id]
        metadata = edge.get("metadata_json")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update({"status": status, "candidate": status != "accepted"})
        edge["metadata_json"] = metadata
        if status == "rejected":
            edge["valid_to"] = "now"
        return edge

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[dict[str, object]]:
        return [
            edge
            for edge in self.edges.values()
            if (from_id is None or edge.get("from_id") == from_id)
            and (to_id is None or edge.get("to_id") == to_id)
            and edge.get("valid_to") is None
        ]

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


def _seed_store() -> InMemoryVNextConnectionStore:
    store = InMemoryVNextConnectionStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Queue retrieval pattern note",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "raw_text": "The queue and retrieval pattern both need provenance trace review.",
                "source_chunk_ids": ["chunk-1"],
            },
        }
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "semantic",
            "canonical_text": "Retrieval provenance trace review improves queue artifacts.",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    store.memories.append(
        {
            "id": "belief-1",
            "memory_type": "belief",
            "canonical_text": "Provenance trace review should guide retrieval quality.",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    return store


def test_connection_report_creates_candidate_edges_artifact_and_logs_each_edge() -> None:
    store = _seed_store()

    artifact = VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None, domains=("project",), max_connections=2)
    )

    assert artifact["artifact_type"] == "connection_report"
    assert artifact["status"] == "needs_review"
    assert artifact["sensitivity"] == "private"
    assert len(artifact["metadata_json"]["candidate_edge_ids"]) == 2
    assert "## Candidate Connections" in artifact["content_markdown"]
    assert "[edge for edge" not in artifact["content_markdown"]
    first_edge = store.edges["edge-1"]
    assert first_edge["from_type"] == "source"
    assert first_edge["to_type"] == "memory"
    assert first_edge["metadata_json"]["status"] == "candidate"
    assert first_edge["metadata_json"]["connection"]["confidence"] >= 0.6
    assert "chunk-1" in first_edge["metadata_json"]["connection"]["provenance"]
    assert [event["event_type"] for event in store.events].count("connection.candidate_edge_logged") == 2
    assert store.events[-1]["event_type"] == "artifact.generated"


def test_connection_report_filters_sensitivity_and_can_auto_accept_high_confidence_edges() -> None:
    store = _seed_store()
    store.memories.append(
        {
            "id": "secret-memory",
            "memory_type": "semantic",
            "canonical_text": "Retrieval provenance trace secret review.",
            "domain": "project",
            "sensitivity": "highly_sensitive",
        }
    )

    artifact = VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None,
            domains=("project",),
            sensitivity_allowed=("public", "private"),
            max_connections=3,
            auto_accept_threshold=0.6,
        )
    )

    assert "secret-memory" not in artifact["content_markdown"]
    assert store.edges["edge-1"]["metadata_json"]["status"] == "accepted"
    assert store.edges["edge-1"]["metadata_json"]["candidate"] is False


def test_connection_report_enforces_project_scope_before_candidate_limits() -> None:
    store = InMemoryVNextConnectionStore()
    for project in ("project-a", "project-b"):
        store.sources.append(
            {
                "id": f"source-{project}",
                "source_type": "manual_text",
                "title": "Queue retrieval pattern note",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {
                    "project_scope": [project],
                    "raw_text": "Queue retrieval provenance trace review.",
                },
            }
        )
        store.memories.append(
            {
                "id": f"memory-{project}",
                "memory_type": "semantic",
                "canonical_text": "Queue retrieval provenance trace review.",
                "domain": "project",
                "sensitivity": "private",
                "project_id": project,
            }
        )

    artifact = VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None,
            domains=("project",),
            projects=("project-a",),
            max_connections=2,
        )
    )

    assert artifact["metadata_json"]["source_ids"] == ["source-project-a"]
    assert artifact["metadata_json"]["memory_ids"] == ["memory-project-a"]
    assert artifact["metadata_json"]["project_scope"] == ["project-a"]
    assert all(edge["from_id"] == "source-project-a" for edge in store.edges.values())
    assert all(edge["to_id"] == "memory-project-a" for edge in store.edges.values())


def test_connection_report_source_filter_honors_embedded_canonical_envelope() -> None:
    store = InMemoryVNextConnectionStore()
    for source_id, canonical_scope in (("source-empty", []), ("source-real", ["real"])):
        store.sources.append(
            {
                "id": source_id,
                "source_type": "manual_text",
                "title": "Queue retrieval pattern note",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {
                    "project_id": "stale",
                    "raw_text": "Queue retrieval provenance trace review.",
                    "metadata_json": {"project_scope": canonical_scope},
                },
            }
        )
    store.memories.append(
        {
            "id": "memory-real",
            "memory_type": "semantic",
            "canonical_text": "Queue retrieval provenance trace review.",
            "domain": "project",
            "sensitivity": "private",
            "project_id": "real",
        }
    )

    artifact = VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None, projects=("real",), max_connections=2)
    )

    assert artifact["metadata_json"]["source_ids"] == ["source-real"]
    assert artifact["metadata_json"]["memory_ids"] == ["memory-real"]
    assert {edge["from_id"] for edge in store.edges.values()} == {"source-real"}


def test_connection_report_model_backed_mode_preserves_candidate_edges_and_metadata() -> None:
    store = _seed_store()

    artifact = VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None,
            domains=("project",),
            max_connections=2,
            generation_mode="model_backed",
            model_route_mode="local_only",
        )
    )

    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["metadata_json"]["candidate_edge_ids"] == ["edge-1", "edge-2"]
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert artifact["prompt_hash"].startswith("sha256:")
    assert "## Inferences" in artifact["content_markdown"]
    assert "## Source References" in artifact["content_markdown"]
    assert "source:source-1" in artifact["content_markdown"]


def test_connection_report_logical_retry_replays_artifact_and_edges() -> None:
    store = _seed_store()
    service = VNextConnectionService(store)

    results = [
        service.generate_connection_report(
            ConnectionFinderRequest(
                domains=("project",),
                max_connections=2,
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


def test_connection_edge_review_and_graph_neighborhood() -> None:
    store = _seed_store()
    service = VNextConnectionService(store)
    service.generate_connection_report(ConnectionFinderRequest(agent_identity=None, domains=("project",), max_connections=1))

    accepted = service.review_edge(edge_id="edge-1", action="accept")
    neighborhood = service.graph_neighborhood(target_id="source-1")

    assert accepted["metadata_json"]["status"] == "accepted"
    assert accepted["metadata_json"]["candidate"] is False
    assert store.events[-1]["event_type"] == "graph_edge.reviewed"
    assert neighborhood["edge_count"] == 1
    assert neighborhood["from_edges"][0]["id"] == "edge-1"

    rejected = service.review_edge(edge_id="edge-1", action="reject")
    assert rejected["metadata_json"]["status"] == "rejected"
    assert service.graph_neighborhood(target_id="source-1")["edge_count"] == 0


def test_connection_request_validation() -> None:
    service = VNextConnectionService(InMemoryVNextConnectionStore())

    with pytest.raises(VNextConnectionValidationError, match="max_connections"):
        service.generate_connection_report(ConnectionFinderRequest(agent_identity=None, max_connections=0))

    with pytest.raises(VNextConnectionValidationError, match="edge review action"):
        service.review_edge(edge_id="edge-1", action="ship")


def test_connection_edges_carry_event_time_from_the_source() -> None:
    store = _seed_store()
    store.sources[0]["source_created_at"] = "2026-06-01T09:30:00Z"
    store.sources[0]["captured_at"] = "2026-07-01T00:00:00Z"

    VNextConnectionService(store).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None, domains=("project",), max_connections=2)
    )

    edge = store.edges["edge-1"]
    # observed_at is the source's own event time, not ingestion time, and
    # valid_from starts the validity interval at the same instant.
    assert edge["observed_at"] == "2026-06-01T09:30:00Z"
    assert edge["valid_from"] == "2026-06-01T09:30:00Z"
    assert edge["metadata_json"]["observed_at_source"] == "source_created_at"


def test_connection_edges_fall_back_to_captured_at_then_now_for_event_time() -> None:
    captured_only = _seed_store()
    captured_only.sources[0]["captured_at"] = "2026-07-01T00:00:00Z"
    VNextConnectionService(captured_only).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None, domains=("project",), max_connections=1)
    )
    edge = captured_only.edges["edge-1"]
    assert edge["observed_at"] == "2026-07-01T00:00:00Z"
    assert edge["valid_from"] == "2026-07-01T00:00:00Z"
    assert edge["metadata_json"]["observed_at_source"] == "captured_at"

    # A source with no timestamps at all: write time stands in, and the
    # fallback is noted on the edge so as-of readers can tell them apart.
    bare = _seed_store()
    VNextConnectionService(bare).generate_connection_report(
        ConnectionFinderRequest(agent_identity=None, domains=("project",), max_connections=1)
    )
    edge = bare.edges["edge-1"]
    assert isinstance(edge["observed_at"], str) and edge["observed_at"].endswith("Z")
    assert edge["valid_from"] == edge["observed_at"]
    assert edge["metadata_json"]["observed_at_source"] == "now"
