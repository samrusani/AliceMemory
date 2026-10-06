from __future__ import annotations

from collections.abc import Mapping

import pytest

from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService


class ContextTreeStore:
    def __init__(self) -> None:
        self.calls: dict[str, list[dict[str, object]]] = {}
        self.projects: list[dict[str, object]] = [
            {
                "id": "project-real",
                "slug": "REAL",
                "name": "Real Project",
                "status": "active",
            },
            {
                "id": "project-foreign",
                "slug": "foreign",
                "name": "Foreign Project",
                "status": "active",
            },
        ]
        self.memories: list[dict[str, object]] = [
            {
                "id": "memory-real",
                "canonical_text": "Same-project memory",
                "project_scope": [" real "],
            },
            {
                "id": "memory-empty-canonical",
                "canonical_text": "Stale memory alias",
                "project_id": "real",
                "metadata_json": {"project_scope": []},
            },
            {
                "id": "memory-foreign",
                "canonical_text": "Foreign memory",
                "project_scope": ["foreign"],
            },
        ]
        self.sources: list[dict[str, object]] = [
            {
                "id": "source-e1",
                "title": "Same-project E1 source",
                "project_id": "foreign",
                "metadata_json": {"project_scope": ["real"]},
            },
            {
                "id": "source-e0",
                "title": "E0 stale source alias",
                "project_id": "real",
                "metadata_json": {"project_scope": []},
            },
            {
                "id": "source-foreign",
                "title": "Foreign source",
                "metadata_json": {"project_scope": ["foreign"]},
            },
        ]
        self.open_loops: list[dict[str, object]] = [
            {
                "id": "loop-real",
                "title": "Same-project open loop",
                "project_scope": ["real"],
            },
            {
                "id": "loop-foreign",
                "title": "Foreign open loop",
                "project_scope": ["foreign"],
            },
        ]
        self.artifacts: list[dict[str, object]] = [
            {
                "id": "artifact-real",
                "title": "Same-project artifact",
                "project_scope": ["real"],
            },
            {
                "id": "artifact-foreign",
                "title": "Foreign artifact",
                "project_scope": ["foreign"],
            },
        ]
        self.events: list[dict[str, object]] = [
            {
                "id": f"event-foreign-{index:02d}",
                "event_type": f"foreign.event.{index:02d}",
                "target_type": "memory",
                "target_id": "memory-foreign",
                "occurred_at": f"2026-07-14T12:{index:02d}:00Z",
            }
            for index in range(13)
        ]
        self.events.extend(
            [
                {
                    "id": "event-project-real",
                    "event_type": "project.updated",
                    "target_type": "project",
                    "target_id": "project-real",
                    "occurred_at": "2026-07-14T11:05:00Z",
                },
                {
                    "id": "event-memory-real",
                    "event_type": "memory.updated",
                    "target_type": "memory",
                    "target_id": "memory-real",
                    "occurred_at": "2026-07-14T11:04:00Z",
                },
                {
                    "id": "event-source-real",
                    "event_type": "source.updated",
                    "target_type": "source",
                    "target_id": "source-e1",
                    "occurred_at": "2026-07-14T11:03:00Z",
                },
                {
                    "id": "event-loop-real",
                    "event_type": "open_loop.updated",
                    "target_type": "open_loop",
                    "target_id": "loop-real",
                    "occurred_at": "2026-07-14T11:02:00Z",
                },
                {
                    "id": "event-artifact-real",
                    "event_type": "artifact.updated",
                    "target_type": "artifact",
                    "target_id": "artifact-real",
                    "occurred_at": "2026-07-14T11:01:00Z",
                },
                {
                    "id": "event-unknown",
                    "event_type": "unknown.updated",
                    "target_type": "unknown",
                    "target_id": "unknown-real",
                    "occurred_at": "2026-07-14T11:00:00Z",
                },
            ]
        )

    def _record(self, method: str, kwargs: Mapping[str, object]) -> None:
        self.calls.setdefault(method, []).append(dict(kwargs))

    @staticmethod
    def _limited(rows: list[dict[str, object]], kwargs: Mapping[str, object]) -> list[dict[str, object]]:
        limit = kwargs.get("limit")
        return rows[:limit] if isinstance(limit, int) else rows

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        groups = {
            "project": self.projects,
            "memory": self.memories,
            "source": self.sources,
            "open_loop": self.open_loops,
            "artifact": self.artifacts,
        }
        return [row for row in groups.get(kind, []) if str(row.get("id")) in ids]

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def list_projects(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("list_projects", kwargs)
        return self._limited(self.projects, kwargs)

    def search_memories(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("search_memories", kwargs)
        return self._limited(self.memories, kwargs)

    def search_sources(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("search_sources", kwargs)
        return self._limited(self.sources, kwargs)

    def list_open_loops(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("list_open_loops", kwargs)
        return self._limited(self.open_loops, kwargs)

    def list_artifacts(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("list_artifacts", kwargs)
        return self._limited(self.artifacts, kwargs)

    def list_events(self, **kwargs: object) -> list[dict[str, object]]:
        self._record("list_events", kwargs)
        target_type = kwargs.get("target_type")
        target_id = kwargs.get("target_id")
        rows = [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]
        return self._limited(rows, kwargs)


def _children_by_root(payload: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    roots = payload["roots"]
    assert isinstance(roots, list)
    result: dict[str, list[dict[str, object]]] = {}
    for root in roots:
        assert isinstance(root, dict)
        children = root["children"]
        assert isinstance(children, list)
        result[str(root["id"])] = children
    return result


def test_scoped_context_tree_filters_five_resource_groups_and_events_before_rendering() -> None:
    store = ContextTreeStore()

    payload = VNextContextTreeService(store).build_tree(
        ContextTreeRequest(query="same project", projects=(" real ",), limit=12)
    )

    assert payload["summary"] == {
        "projects": 1,
        "memories": 1,
        "sources": 1,
        "open_loops": 1,
        "artifacts": 1,
        "events": 5,
    }
    assert "entities" not in payload["summary"]
    children = _children_by_root(payload)
    assert [child["ref"] for child in children["root:projects"]] == ["project:project-real"]
    assert [child["ref"] for child in children["root:memories"]] == ["memory:memory-real"]
    assert [child["ref"] for child in children["root:sources"]] == ["source:source-e1"]
    assert [child["ref"] for child in children["root:open_loops"]] == ["open_loop:loop-real"]
    assert [child["ref"] for child in children["root:artifacts"]] == ["artifact:artifact-real"]
    assert [child["ref"] for child in children["root:events"]] == [
        "event:event-project-real",
        "event:event-memory-real",
        "event:event-source-real",
        "event:event-loop-real",
        "event:event-artifact-real",
    ]

    assert store.calls["list_projects"][0]["scope_projects"] == ("real",)
    assert store.calls["search_memories"][0]["projects"] == ("real",)
    assert store.calls["search_sources"][0]["scope_projects"] == ("real",)
    assert store.calls["list_open_loops"][0]["scope_projects"] == ("real",)
    assert store.calls["list_artifacts"][0]["scope_projects"] == ("real",)
    assert len(store.calls["list_events"]) == 5
    assert all(call.get("target_type") and call.get("target_id") for call in store.calls["list_events"])

    generated = store.events[-1]
    assert generated["event_type"] == "context_tree.generated"
    assert generated["payload_json"] == {
        "summary": payload["summary"],
        "read_only": True,
        "project_scope": ["real"],
    }


def test_scoped_context_tree_does_not_widen_sources_to_stale_aliases() -> None:
    store = ContextTreeStore()
    store.projects = []
    store.memories = []
    store.sources = store.sources[:2]
    store.open_loops = []
    store.artifacts = []

    payload = VNextContextTreeService(store).build_tree(ContextTreeRequest(projects=("foreign",), include_events=False))

    assert payload["summary"]["sources"] == 0  # type: ignore[index]
    assert _children_by_root(payload)["root:sources"] == []


@pytest.mark.parametrize(
    ("project", "requested_scope"),
    [
        ({"id": "project-by-id", "slug": "other", "name": "Other"}, "PROJECT-BY-ID"),
        ({"id": "project-2", "slug": "Launch\tPlan", "name": "Other"}, " launch plan "),
        ({"id": "project-3", "slug": "other", "name": "Project Atlas"}, "project atlas"),
    ],
)
def test_scoped_context_tree_matches_project_id_slug_or_name(
    project: dict[str, object],
    requested_scope: str,
) -> None:
    store = ContextTreeStore()
    store.projects = [project]
    store.memories = []
    store.sources = []
    store.open_loops = []
    store.artifacts = []

    payload = VNextContextTreeService(store).build_tree(
        ContextTreeRequest(projects=(requested_scope,), include_events=False)
    )

    assert payload["summary"] == {
        "projects": 1,
        "memories": 0,
        "sources": 0,
        "open_loops": 0,
        "artifacts": 0,
        "events": 0,
    }


def test_unscoped_context_tree_keeps_global_event_read_behavior() -> None:
    store = ContextTreeStore()

    payload = VNextContextTreeService(store).build_tree(ContextTreeRequest(limit=2))

    assert payload["summary"] == {
        "projects": 2,
        "memories": 2,
        "sources": 2,
        "open_loops": 2,
        "artifacts": 2,
        "events": 2,
    }
    assert store.calls["list_events"] == [{"limit": 2}]
    assert "scope_projects" not in store.calls["list_projects"][0]
    assert "projects" not in store.calls["search_memories"][0]
    assert "scope_projects" not in store.calls["search_sources"][0]
    assert "scope_projects" not in store.calls["list_open_loops"][0]
    assert "scope_projects" not in store.calls["list_artifacts"][0]
    generated = store.events[-1]
    assert generated["payload_json"]["project_scope"] == []  # type: ignore[index]
