from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_project_scope import (
    normalize_project_scope,
    project_scope_identity,
    project_scopes_overlap,
    source_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject


DEFAULT_CONTEXT_TREE_LIMIT = 12
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")


class VNextContextTreeValidationError(ValueError):
    """Raised when context-tree inputs are invalid."""


class VNextContextTreeStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def list_projects(self, **kwargs) -> list[JsonObject]: ...

    def search_sources(self, **kwargs) -> list[JsonObject]: ...

    def search_memories(self, **kwargs) -> list[JsonObject]: ...

    def list_open_loops(self, **kwargs) -> list[JsonObject]: ...

    def list_artifacts(self, **kwargs) -> list[JsonObject]: ...

    def list_events(self, **kwargs) -> list[JsonObject]: ...


@dataclass(frozen=True, slots=True)
class ContextTreeRequest:
    query: str = ""
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    limit: int = DEFAULT_CONTEXT_TREE_LIMIT
    include_events: bool = True
    generated_by: str = "system"
    agent_identity: JsonObject | None = None
    policy_decision: JsonObject | None = None
    trace_id: str | None = None


def _validate_request(request: ContextTreeRequest) -> None:
    if request.limit < 1 or request.limit > 50:
        raise VNextContextTreeValidationError("limit must be between 1 and 50")
    if not request.sensitivity_allowed:
        raise VNextContextTreeValidationError("sensitivity_allowed must not be empty")


def _domains(request: ContextTreeRequest) -> list[str] | None:
    return list(request.domains) if request.domains else None


def _sensitivity(request: ContextTreeRequest) -> list[str]:
    return list(request.sensitivity_allowed)


def _project_scope(request: ContextTreeRequest) -> tuple[str, ...]:
    return normalize_project_scope(request.projects)


def _row_id(row: JsonObject) -> str:
    value = row.get("id")
    if value is None:
        return ""
    row_id = str(value)
    return row_id if row_id.strip() else ""


def _project_is_in_scope(row: JsonObject, projects: tuple[str, ...]) -> bool:
    if not projects:
        return True
    row_id = _row_id(row)
    candidates = project_scope_identity(
        (
            row_id,
            row.get("slug"),
            row.get("name"),
        )
    )
    return bool(set(project_scope_identity(projects)).intersection(candidates))


def _resource_is_in_scope(
    row: JsonObject,
    projects: tuple[str, ...],
    *,
    source: bool = False,
) -> bool:
    if not projects:
        return True
    row_scope = source_project_scope(row) if source else resource_project_scope(row)
    return project_scopes_overlap(row_scope, projects)


def _admitted_rows(
    rows: list[JsonObject],
    projects: tuple[str, ...],
    *,
    project: bool = False,
    source: bool = False,
) -> list[JsonObject]:
    if not projects:
        return rows
    admitted: list[JsonObject] = []
    for row in rows:
        if not _row_id(row):
            continue
        if project:
            allowed = _project_is_in_scope(row, projects)
        else:
            allowed = _resource_is_in_scope(row, projects, source=source)
        if allowed:
            admitted.append(row)
    return admitted


def _event_sort_key(event: JsonObject) -> tuple[str, str]:
    return (str(event.get("occurred_at") or ""), _row_id(event))


def _node(
    *,
    node_id: str,
    label: str,
    node_type: str,
    ref: str | None = None,
    children: list[JsonObject] | None = None,
    metadata: JsonObject | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "id": node_id,
        "label": label,
        "type": node_type,
        "ref": ref,
        "children": children or [],
        "metadata": metadata or {},
    }
    payload["child_count"] = len(payload["children"]) if isinstance(payload["children"], list) else 0
    return payload


def _label(row: JsonObject, *keys: str, fallback: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:180]
    return fallback


def _tree_event_visible(store: object, event: JsonObject, domains: list[str], sensitivity: list[str], projects: tuple[str, ...]) -> bool:
    from alicebot_api.vnext_label_guard import admit_loaded

    kind = str(event.get("target_type") or "")
    target_id = event.get("target_id")
    getters = {
        "memory": "get_memory",
        "open_loop": "get_open_loop",
        "artifact": "get_artifact",
        "project": "get_project",
        "source": "get_source",
    }
    getter_name = getters.get(kind)
    if getter_name is None or not isinstance(target_id, str) or target_id == "":
        return True
    getter = getattr(store, getter_name, None)
    if not callable(getter):
        return True
    row = getter(target_id)
    if not isinstance(row, dict) and not hasattr(row, "get"):
        return True
    return bool(
        admit_loaded(store, kind=kind, rows=[row], domains=domains, sensitivity_allowed=sensitivity, projects=projects)
    )


def _row_node(prefix: str, row: JsonObject, *, label_keys: tuple[str, ...], fallback: str) -> JsonObject:
    row_id = str(row.get("id", "unknown"))
    return _node(
        node_id=f"{prefix}:{row_id}",
        label=_label(row, *label_keys, fallback=fallback),
        node_type=prefix,
        ref=f"{prefix}:{row_id}",
        metadata={
            key: row.get(key)
            for key in (
                "domain",
                "sensitivity",
                "status",
                "memory_type",
                "artifact_type",
                "source_type",
                "updated_at",
                "created_at",
                "captured_at",
            )
            if row.get(key) is not None
        },
    )


class VNextContextTreeService:
    def __init__(self, store: VNextContextTreeStore) -> None:
        self.store = store

    def _scoped_events(
        self,
        *,
        projects: list[JsonObject],
        memories: list[JsonObject],
        sources: list[JsonObject],
        open_loops: list[JsonObject],
        artifacts: list[JsonObject],
        limit: int,
    ) -> list[JsonObject]:
        events: list[JsonObject] = []
        targets = (
            ("project", projects),
            ("memory", memories),
            ("source", sources),
            ("open_loop", open_loops),
            ("artifact", artifacts),
        )
        for target_type, rows in targets:
            for row in rows:
                target_id = _row_id(row)
                if not target_id:
                    continue
                target_events = self.store.list_events(
                    target_type=target_type,
                    target_id=target_id,
                    limit=limit,
                )
                for event in target_events:
                    if not _row_id(event):
                        continue
                    if event.get("target_type") != target_type:
                        continue
                    if str(event.get("target_id") or "") != target_id:
                        continue
                    events.append(event)

        events.sort(key=_event_sort_key, reverse=True)
        unique_events: list[JsonObject] = []
        seen_event_ids: set[str] = set()
        for event in events:
            event_id = _row_id(event)
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            unique_events.append(event)
            if len(unique_events) == limit:
                break
        return unique_events

    def build_tree(self, request: ContextTreeRequest | None = None) -> JsonObject:
        request = request or ContextTreeRequest()
        _validate_request(request)
        trace_id = request.trace_id or str(uuid4())
        domains = _domains(request)
        project_scope = _project_scope(request)
        sensitivity = _sensitivity(request)
        query = request.query or "project decision preference open loop artifact"
        scoped_filter = {"scope_projects": project_scope} if project_scope else {}
        memory_scoped_filter = {"projects": project_scope} if project_scope else {}

        projects = self.store.list_projects(
            status="active",
            domains=domains,
            sensitivity_allowed=sensitivity,
            limit=request.limit,
            **scoped_filter,
        )
        memories = self.store.search_memories(
            query=query,
            domains=domains,
            sensitivity_allowed=sensitivity,
            limit=request.limit,
            **memory_scoped_filter,
        )
        sources = self.store.search_sources(
            query=query,
            domains=domains,
            sensitivity_allowed=sensitivity,
            limit=request.limit,
            **scoped_filter,
        )
        open_loops = self.store.list_open_loops(
            status="open",
            domains=domains,
            sensitivity_allowed=sensitivity,
            limit=request.limit,
            **scoped_filter,
        )
        artifacts = self.store.list_artifacts(
            domains=domains,
            sensitivity_allowed=sensitivity,
            limit=request.limit,
            **scoped_filter,
        )
        projects = _admitted_rows(projects, project_scope, project=True)
        memories = _admitted_rows(memories, project_scope)
        sources = _admitted_rows(sources, project_scope, source=True)
        open_loops = _admitted_rows(open_loops, project_scope)
        artifacts = _admitted_rows(artifacts, project_scope)
        from alicebot_api.vnext_label_guard import admit_loaded

        projects = admit_loaded(
            self.store, kind="project", rows=projects, domains=domains, sensitivity_allowed=sensitivity, projects=project_scope
        )
        memories = admit_loaded(
            self.store, kind="memory", rows=memories, domains=domains, sensitivity_allowed=sensitivity, projects=project_scope
        )
        sources = admit_loaded(
            self.store, kind="source", rows=sources, domains=domains, sensitivity_allowed=sensitivity, projects=project_scope
        )
        open_loops = admit_loaded(
            self.store,
            kind="open_loop",
            rows=open_loops,
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=project_scope,
        )
        artifacts = admit_loaded(
            self.store,
            kind="artifact",
            rows=artifacts,
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=project_scope,
        )
        if not request.include_events:
            events = []
        elif project_scope:
            events = self._scoped_events(
                projects=projects,
                memories=memories,
                sources=sources,
                open_loops=open_loops,
                artifacts=artifacts,
                limit=request.limit,
            )
        else:
            events = [
                event
                for event in self.store.list_events(limit=request.limit)
                if _tree_event_visible(self.store, event, domains, sensitivity, project_scope)
            ]

        roots = [
            _node(
                node_id="root:projects",
                label="Projects",
                node_type="group",
                children=[
                    _row_node("project", row, label_keys=("name", "title", "slug"), fallback="Project")
                    for row in projects
                ],
            ),
            _node(
                node_id="root:memories",
                label="Memories",
                node_type="group",
                children=[
                    _row_node("memory", row, label_keys=("title", "canonical_text", "memory_key"), fallback="Memory")
                    for row in memories
                ],
            ),
            _node(
                node_id="root:sources",
                label="Sources",
                node_type="group",
                children=[
                    _row_node("source", row, label_keys=("title", "source_type"), fallback="Source") for row in sources
                ],
            ),
            _node(
                node_id="root:open_loops",
                label="Open Loops",
                node_type="group",
                children=[
                    _row_node("open_loop", row, label_keys=("title", "description"), fallback="Open loop")
                    for row in open_loops
                ],
            ),
            _node(
                node_id="root:artifacts",
                label="Artifacts",
                node_type="group",
                children=[
                    _row_node("artifact", row, label_keys=("title", "artifact_type"), fallback="Artifact")
                    for row in artifacts
                ],
            ),
            _node(
                node_id="root:events",
                label="Recent Events",
                node_type="group",
                children=[_row_node("event", row, label_keys=("event_type",), fallback="Event") for row in events],
            ),
        ]
        summary = {
            "projects": len(projects),
            "memories": len(memories),
            "sources": len(sources),
            "open_loops": len(open_loops),
            "artifacts": len(artifacts),
            "events": len(events),
        }
        payload = {
            "schema_version": "vnext_context_tree_v0",
            "generated_at": datetime.now(UTC).isoformat(),
            "trace_id": trace_id,
            "query": request.query,
            "read_only": True,
            "summary": summary,
            "roots": roots,
            "agent_identity": request.agent_identity,
            "policy_decision": request.policy_decision,
        }
        append_event(
            self.store,
            event_type="context_tree.generated",
            actor_type=request.generated_by,
            target_type="context_tree",
            target_id=trace_id,
            trace_id=trace_id,
            payload={
                "summary": summary,
                "read_only": True,
                "project_scope": list(project_scope),
            },
        )
        return payload


__all__ = [
    "ContextTreeRequest",
    "VNextContextTreeService",
    "VNextContextTreeStore",
    "VNextContextTreeValidationError",
]
