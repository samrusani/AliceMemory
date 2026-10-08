from __future__ import annotations

import pytest

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService, VNextBrainValidationError


class InMemoryVNextBrainStore:
    def __init__(self) -> None:
        self.sources: list[dict[str, object]] = []
        self.memories: list[dict[str, object]] = []
        self.open_loops: list[dict[str, object]] = []
        self.artifacts: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def create_artifact(self, artifact: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**artifact, "id": f"artifact-{len(self.artifacts) + 1}"}
        self.artifacts[str(row["id"])] = row
        return row

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories.append(row)
        return row

    def create_open_loop(self, loop: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**loop, "id": f"loop-{len(self.open_loops) + 1}"}
        self.open_loops.append(row)
        return row

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

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        rows = [row for row in self.open_loops if status is None or row.get("status") == status]
        return _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 4,
    ) -> list[dict[str, object]]:
        rows = [
            row
            for row in self.artifacts.values()
            if artifact_type is None or row.get("artifact_type") == artifact_type
        ]
        return _filter_rows(rows, domains=domains, sensitivity_allowed=sensitivity_allowed)[:limit]


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


def _seed_store() -> InMemoryVNextBrainStore:
    store = InMemoryVNextBrainStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Alice daily note",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T08:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"raw_text": "Fact: Alice needs daily brief.\nTODO: review queue worker output"},
        }
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "project_state",
            "canonical_text": "Alice vNext is building the brain workflows.",
            "status": "active",
            "valid_from": "2026-05-10T09:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    store.open_loops.append(
        {
            "id": "loop-existing",
            "title": "Validate daily brief artifact",
            "status": "open",
            "opened_at": "2026-05-10T10:00:00Z",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    return store


def test_daily_brief_generates_dated_reviewable_artifact_with_sources_and_open_loop_candidates() -> None:
    store = _seed_store()

    artifact = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None, generated_for="2026-05-10", domains=("project",))
    )

    assert artifact["artifact_type"] == "daily_brief"
    assert artifact["title"] == "Daily Brief - 2026-05-10"
    assert artifact["status"] == "needs_review"
    assert artifact["sensitivity"] == "private"
    assert "## 1. Executive Summary" in artifact["content_markdown"]
    assert "## 9. Sources Used" in artifact["content_markdown"]
    assert "[source:source-1]" in artifact["content_markdown"]
    assert store.open_loops[-1]["title"] == "review queue worker output"
    assert store.open_loops[-1]["metadata_json"]["candidate"] is True
    assert store.events[-1]["event_type"] == "artifact.generated"
    assert store.events[-1]["payload_json"]["workflow"] == "daily_brief"
    derived = artifact["metadata_json"]["derived_from"]
    assert derived["counts"]["sources"] == len(derived["sources"]) == 1
    assert derived["sources"] == ["source-1"]
    assert "memory-1" in derived["memories"]
    assert store.open_loops[-1]["metadata_json"]["derived_from"]["sources"] == ["source-1"]


def test_daily_brief_respects_sensitivity_filtering() -> None:
    store = _seed_store()
    store.sources.append(
        {
            "id": "source-secret",
            "title": "Highly sensitive note",
            "content_hash": "sha256:secret",
            "domain": "project",
            "sensitivity": "highly_sensitive",
            "metadata_json": {"raw_text": "TODO: do not show"},
        }
    )

    artifact = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            sensitivity_allowed=("public", "private"),
        )
    )

    assert "source-secret" not in artifact["content_markdown"]
    assert artifact["sensitivity"] == "private"


def test_weekly_synthesis_creates_candidate_memory_without_auto_promotion() -> None:
    store = _seed_store()

    artifact = VNextBrainService(store).generate_weekly_synthesis(
        BrainArtifactRequest(agent_identity=None, generated_for="2026-05-10", domains=("project",))
    )

    assert artifact["artifact_type"] == "weekly_synthesis"
    assert artifact["title"] == "Weekly Synthesis - 2026-W19"
    assert "## 3. Recurring patterns" in artifact["content_markdown"]
    assert "## Project / Person / Concept Links" in artifact["content_markdown"]
    candidate = store.memories[-1]
    assert candidate["memory_type"] == "artifact_summary"
    assert candidate["status"] == "candidate"
    assert candidate["metadata_json"]["candidate"] is True
    assert artifact["metadata_json"]["candidate_memory_ids"] == [candidate["id"]]
    assert store.events[-1]["event_type"] == "artifact.generated"
    assert store.events[-1]["payload_json"]["workflow"] == "weekly_synthesis"


def test_weekly_synthesis_can_skip_candidate_memory_creation() -> None:
    store = _seed_store()

    artifact = VNextBrainService(store).generate_weekly_synthesis(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            create_candidate_memories=False,
        )
    )

    assert artifact["metadata_json"]["candidate_memory_ids"] == []
    assert len(store.memories) == 1


def test_daily_brief_model_backed_mode_stores_provider_metadata_and_review_only_artifact() -> None:
    store = _seed_store()

    artifact = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            generation_mode="model_backed",
            model_route_mode="local_only",
        )
    )

    assert artifact["status"] == "needs_review"
    assert artifact["prompt_hash"].startswith("sha256:")
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert artifact["model_info_json"]["input_context_hash"].startswith("sha256:")
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["metadata_json"]["model_routing"]["route_mode"] == "local_only"
    assert "## Facts" in artifact["content_markdown"]
    assert "## Recommendations" in artifact["content_markdown"]
    assert "## Source References" in artifact["content_markdown"]
    assert "source:source-1" in artifact["content_markdown"]


def test_weekly_synthesis_model_backed_mode_keeps_candidate_memory_reviewable() -> None:
    store = _seed_store()

    artifact = VNextBrainService(store).generate_weekly_synthesis(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            generation_mode="model_backed",
            model_route_mode="local_only",
        )
    )

    assert artifact["status"] == "needs_review"
    assert artifact["metadata_json"]["generation_mode"] == "model_backed"
    assert artifact["model_info_json"]["provider"] == "deterministic_local"
    assert store.memories[-1]["status"] == "candidate"
    assert "## Uncertainties" in artifact["content_markdown"]


def test_daily_and_weekly_reports_use_true_half_open_time_windows() -> None:
    store = InMemoryVNextBrainStore()
    for source_id, captured_at in (
        ("before-week", "2026-05-03T23:59:59Z"),
        ("week-start", "2026-05-04T00:00:00Z"),
        ("daily", "2026-05-10T12:00:00Z"),
        ("week-end", "2026-05-11T00:00:00Z"),
    ):
        store.sources.append(
            {
                "id": source_id,
                "title": source_id,
                "captured_at": captured_at,
                "domain": "project",
                "sensitivity": "private",
            }
        )

    daily = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            discover_open_loops=False,
        )
    )
    weekly = VNextBrainService(store).generate_weekly_synthesis(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            create_candidate_memories=False,
        )
    )

    assert daily["metadata_json"]["input_summary"]["source_ids"] == ["daily"]
    assert weekly["metadata_json"]["input_summary"]["source_ids"] == [
        "week-start",
        "daily",
    ]
    assert daily["metadata_json"]["window_end"] == "2026-05-11T00:00:00+00:00"


def test_daily_brief_applies_project_scope_before_bounded_legacy_limit() -> None:
    store = InMemoryVNextBrainStore()
    for index in range(201):
        store.sources.append(
            {
                "id": f"project-b-{index}",
                "title": "Project B decoy",
                "captured_at": "2026-05-10T12:00:00Z",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_scope": ["project-b"]},
            }
        )
    store.sources.append(
        {
            "id": "project-a-source",
            "title": "Project A source",
            "captured_at": "2026-05-10T12:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_scope": ["project-a"],
                "raw_text": "TODO: ship project A",
            },
        }
    )

    artifact = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            domains=("project",),
            projects=("project-a",),
            source_limit=1,
        )
    )

    assert artifact["metadata_json"]["input_summary"]["source_ids"] == ["project-a-source"]
    assert artifact["metadata_json"]["project_scope"] == ["project-a"]
    assert store.open_loops[-1]["metadata_json"]["project_scope"] == ["project-a"]


def test_brain_source_scope_uses_complete_persisted_envelope_for_filtering_and_derivation() -> None:
    def source(source_id: str, canonical_scope: list[str]) -> dict[str, object]:
        return {
            "id": source_id,
            "title": source_id,
            "captured_at": "2026-05-10T12:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": f"TODO: review {source_id}",
                "metadata_json": {"project_scope": canonical_scope},
            },
        }

    scoped_store = InMemoryVNextBrainStore()
    scoped_store.sources.extend((source("empty-source", []), source("real-source", ["real"])))
    real = VNextBrainService(scoped_store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            projects=("real",),
            discover_open_loops=False,
        )
    )
    stale = VNextBrainService(scoped_store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None,
            generated_for="2026-05-10",
            projects=("stale",),
            discover_open_loops=False,
        )
    )

    assert real["metadata_json"]["input_summary"]["source_ids"] == ["real-source"]
    assert stale["metadata_json"]["input_summary"]["source_ids"] == []

    daily_store = InMemoryVNextBrainStore()
    daily_store.sources.extend((source("empty-source", []), source("real-source", ["real"])))
    daily = VNextBrainService(daily_store).generate_daily_brief(
        BrainArtifactRequest(agent_identity=None, generated_for="2026-05-10")
    )
    loops_by_source = {row["source_id"]: row for row in daily_store.open_loops}

    assert daily["metadata_json"]["project_scope"] == ["real"]
    assert loops_by_source["empty-source"]["metadata_json"]["project_scope"] == []
    assert loops_by_source["real-source"]["metadata_json"]["project_scope"] == ["real"]

    weekly_store = InMemoryVNextBrainStore()
    weekly_store.sources.extend((source("empty-source", []), source("real-source", ["real"])))
    weekly = VNextBrainService(weekly_store).generate_weekly_synthesis(
        BrainArtifactRequest(agent_identity=None, generated_for="2026-05-10")
    )

    assert weekly["metadata_json"]["project_scope"] == ["real"]
    assert weekly_store.memories[-1]["metadata_json"]["project_scope"] == ["real"]


def test_brain_request_validation_rejects_bad_dates_and_limits() -> None:
    service = VNextBrainService(InMemoryVNextBrainStore())

    with pytest.raises(VNextBrainValidationError, match="generated_for"):
        service.generate_daily_brief(BrainArtifactRequest(agent_identity=None, generated_for="10-05-2026"))

    with pytest.raises(VNextBrainValidationError, match="source_limit"):
        service.generate_weekly_synthesis(BrainArtifactRequest(agent_identity=None, source_limit=0))
