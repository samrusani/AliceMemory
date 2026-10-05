from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from uuid import uuid4

import anyio
import pytest
from starlette.requests import Request
from starlette.routing import Match

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.routers import _vnext_automation as vnext_automation
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_projects as vnext_projects_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.routers import workspaces as workspaces_router
from alicebot_api.routers import _vnext_shared as vnext_shared
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_memory_version import memory_version_snapshot
from alicebot_api.vnext_projects import VNextProjectService
from alicebot_api.vnext_project_scope import memory_project_scope


class FakeVNextStore:
    def __init__(self, _conn) -> None:
        self.sources: dict[str, dict[str, object]] = {}
        self.source_by_hash: dict[str, dict[str, object]] = {}
        self.chunks: list[dict[str, object]] = []
        self.memories: list[dict[str, object]] = []
        self.open_loops: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.provenance_links: list[dict[str, object]] = []
        self.tasks: list[dict[str, object]] = []
        self.artifacts: dict[str, dict[str, object]] = {}
        self.quality_ratings: list[dict[str, object]] = []
        self.edges: dict[str, dict[str, object]] = {}
        self.beliefs: dict[str, dict[str, object]] = {}
        self.projects: dict[str, dict[str, object]] = {}
        self.agent_identities: dict[str, dict[str, object]] = {}
        self.agent_api_keys: list[dict[str, object]] = []
        self.browser_clip_capabilities: dict[str, dict[str, object]] = {}
        self.revisions: list[dict[str, object]] = []
        self.graph_locked = False
        self.labels_locked = False
        self.labels_exclusive = False

    def lock_graph_mutation(self) -> None:
        self.graph_locked = True

    def lock_label_writes(self, *, exclusive: bool = False) -> None:
        self.labels_locked = True
        self.labels_exclusive |= exclusive

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        collection = {"source": self.sources.values(), "memory": self.memories, "open_loop": self.open_loops,
                      "artifact": self.artifacts.values(), "belief": self.beliefs.values(), "project": self.projects.values()}.get(kind, [])
        return [dict(row) for row in collection if str(row.get("id")) in ids]

    def _fetch_all(self, query: str, _params: tuple[object, ...]) -> list[dict[str, object]]:
        # The label dependant walker performs its exact canonical reference filter after this prefilter.
        for table, kind in (("memories", "memory"), ("open_loops", "open_loop"), ("generated_artifacts", "artifact"), ("projects", "project")):
            if f"FROM {table}" in query:
                collection = {"memory": self.memories, "open_loop": self.open_loops, "artifact": self.artifacts.values(), "project": self.projects.values()}[kind]
                return [{**row, "kind": kind} for row in collection]
        raise AssertionError(query)

    def create_browser_clip_capability(
        self,
        *,
        capability_hash: str,
        origin: str,
        ttl_seconds: int,
    ) -> dict[str, object]:
        row = {
            "id": str(uuid4()),
            "origin": origin,
            "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            "consumed_at": None,
        }
        self.browser_clip_capabilities[capability_hash] = row
        return dict(row)

    def consume_browser_clip_capability(
        self,
        *,
        capability_hash: str,
        origin: str,
    ) -> dict[str, object] | None:
        row = self.browser_clip_capabilities.get(capability_hash)
        if row is None or row["origin"] != origin or row["consumed_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
            return None
        row["consumed_at"] = datetime.now(UTC)
        return dict(row)

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def get_source_by_content_hash(self, content_hash: str) -> dict[str, object] | None:
        return self.source_by_hash.get(content_hash)

    def create_source(self, source: dict[str, object], **_kwargs) -> dict[str, object]:
        source_id = str(uuid4())
        row = {**source, "id": source_id}
        self.sources[source_id] = row
        self.source_by_hash[str(source["content_hash"])] = row
        return row

    def get_source(self, source_id: str) -> dict[str, object] | None:
        source = self.sources.get(source_id)
        if source is not None and source.get("deleted_at") is None:
            return source
        return None

    def list_sources(self, **kwargs) -> list[dict[str, object]]:
        return list(self.sources.values())[: kwargs.get("limit", 20)]

    def get_sources_by_ids(self, source_ids: list[str]) -> list[dict[str, object]]:
        return [self.sources[source_id] for source_id in source_ids if source_id in self.sources]

    def update_source(self, *, source_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        source = self.sources[source_id]
        source.update(patch)
        return source

    def delete_source(self, *, source_id: str, **_kwargs) -> dict[str, object]:
        source = self.sources[source_id]
        source["deleted_at"] = "now"
        return source

    def create_source_chunk(self, chunk: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**chunk, "id": f"chunk-{len(self.chunks) + 1}"}
        self.chunks.append(row)
        return row

    def list_source_chunks(self, source_id: str, *, limit: int = 500) -> list[dict[str, object]]:
        return [chunk for chunk in self.chunks if chunk.get("source_id") == source_id][:limit]

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories.append(row)
        return row

    def list_memories(self, *, status: str | None = None) -> list[dict[str, object]]:
        return [memory for memory in self.memories if status is None or memory.get("status") == status]

    def list_memories_referencing_source(self, *, source_id: str, limit: int = 500) -> list[dict[str, object]]:
        return [memory for memory in self.memories if vnext_shared._vnext_row_references_source(memory, source_id)][
            :limit
        ]

    def update_memory(self, *, memory_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        for memory in self.memories:
            if memory["id"] == memory_id:
                memory.update(patch)
                return memory
        raise AssertionError(memory_id)

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        for memory in self.memories:
            if str(memory["id"]) == str(memory_id):
                return memory
        return None

    def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
        return self.get_memory(memory_id)

    def get_memory_for_redaction(self, memory_id: str) -> dict[str, object] | None:
        return self.get_memory(memory_id)

    def lock_project_update_artifacts_for_redaction(self, memory_id: str) -> list[dict[str, object]]:
        return sorted(
            [
                artifact
                for artifact in self.artifacts.values()
                if isinstance(artifact.get("metadata_json"), dict)
                and artifact["metadata_json"].get("candidate_memory_id") == memory_id
            ],
            key=lambda artifact: str(artifact.get("id")),
        )

    def redact_memory_bundle(
        self,
        *,
        memory_id: str,
        project_update_artifacts: list[dict[str, object]],
        actor_type: str = "user",
    ) -> dict[str, object]:
        memory = self.get_memory(memory_id)
        assert memory is not None, memory_id
        redacted_at = (
            str(memory.get("metadata_json", {}).get("redacted_at") or "now")
            if isinstance(memory.get("metadata_json"), dict)
            else "now"
        )
        metadata = memory.get("metadata_json")
        structural = {
            key: metadata[key]
            for key in (
                "project_id",
                "project_scope",
                "superseded_by",
                "supersedes",
                "run_id",
                "agent_id",
                "created_by_agent_id",
            )
            if isinstance(metadata, dict) and key in metadata
        }
        desired_memory = {
            "memory_key": f"redacted.{memory_id}",
            "title": None if memory.get("title") is None else "[REDACTED]",
            "canonical_text": "[REDACTED]",
            "summary": None if memory.get("summary") is None else "[REDACTED]",
            "trust_reason": None if memory.get("trust_reason") is None else "[REDACTED]",
            "value": {"redacted": True},
            "source_event_ids": [],
            "metadata_json": {**structural, "redacted": True, "redacted_at": redacted_at},
            "commit_digest": None,
            "confirmation_id": None,
            "status": "archived",
            "deleted_at": memory.get("deleted_at") or "now",
        }
        memory_changed = any(memory.get(key) != value for key, value in desired_memory.items())
        memory.update(desired_memory)
        redacted_revisions = 0
        for revision in self.revisions:
            if str(revision.get("memory_id")) != str(memory_id):
                continue
            desired_revision = {
                "memory_key": f"redacted.{memory_id}",
                "source_event_ids": [],
                "previous_value": None if revision.get("previous_value") is None else {"redacted": True},
                "new_value": None if revision.get("new_value") is None else {"redacted": True},
                "candidate": {"redacted": True},
                "text_before": None if revision.get("text_before") is None else "[REDACTED]",
                "text_after": "[REDACTED]",
                "reason": None if revision.get("reason") is None else "[REDACTED]",
                "metadata_json": {"redacted": True},
            }
            if any(revision.get(key) != value for key, value in desired_revision.items()):
                revision.update(desired_revision)
                redacted_revisions += 1
        coupled_artifact_ids = [str(artifact["id"]) for artifact in project_update_artifacts]
        changed_artifact_ids: list[str] = []
        for artifact in project_update_artifacts:
            artifact_id = str(artifact["id"])
            old_metadata = artifact["metadata_json"]
            assert isinstance(old_metadata, dict)
            desired_artifact = {
                "title": "[REDACTED]",
                "content_markdown": "[REDACTED]",
                "prompt_hash": None,
                "model_info_json": {"redacted": True},
                "metadata_json": {
                    "redacted": True,
                    "redacted_at": redacted_at,
                    "workflow": "project_auto_update",
                    "project_id": old_metadata["project_id"],
                    "project_scope": [old_metadata["project_id"]],
                    "candidate_memory_id": memory_id,
                    "review_action": old_metadata["review_action"],
                },
            }
            if any(artifact.get(key) != value for key, value in desired_artifact.items()):
                artifact.update(desired_artifact)
                changed_artifact_ids.append(artifact_id)

        redacted_quality_ratings = 0
        for rating in self.quality_ratings:
            if str(rating.get("artifact_id")) not in coupled_artifact_ids:
                continue
            desired_rating = {
                "missed_context": None if rating.get("missed_context") is None else "[REDACTED]",
                "comments": None if rating.get("comments") is None else "[REDACTED]",
                "metadata_json": {"redacted": True},
            }
            if any(rating.get(key) != value for key, value in desired_rating.items()):
                rating.update(desired_rating)
                redacted_quality_ratings += 1

        redacted_provenance_links = 0
        for link in self.provenance_links:
            coupled = (link.get("target_type") == "memory" and str(link.get("target_id")) == memory_id) or (
                link.get("target_type") == "artifact" and str(link.get("target_id")) in coupled_artifact_ids
            )
            if coupled and link.get("quote") not in {None, "[REDACTED]"}:
                link["quote"] = "[REDACTED]"
                redacted_provenance_links += 1

        redacted_events = 0
        for event in self.events:
            payload = event.get("payload_json")
            coupled = str(event.get("target_id")) in {memory_id, *coupled_artifact_ids} or (
                isinstance(payload, dict)
                and any(
                    str(payload.get(key)) in {memory_id, *coupled_artifact_ids}
                    for key in ("memory_id", "candidate_memory_id", "artifact_id")
                )
            )
            if coupled:
                desired_payload = {
                    "redacted": True,
                    "memory_id": memory_id,
                    "event_type": event.get("event_type"),
                }
                if event.get("payload_json") != desired_payload or event.get("integrity_hash") is not None:
                    event["payload_json"] = desired_payload
                    event["integrity_hash"] = None
                    redacted_events += 1

        changed = bool(
            memory_changed
            or changed_artifact_ids
            or redacted_quality_ratings
            or redacted_provenance_links
            or redacted_revisions
            or redacted_events
        )
        if changed:
            self.append_event(
                {
                    "event_type": "memory.redacted",
                    "actor_type": actor_type,
                    "target_type": "memory",
                    "target_id": memory_id,
                    "payload_json": {
                        "redacted": True,
                        "memory_id": memory_id,
                        "event_type": "memory.redacted",
                    },
                    "integrity_hash": None,
                }
            )
        return {
            "memory": memory,
            "redacted_revisions": redacted_revisions,
            "redacted_events": redacted_events,
            "redacted_artifacts": len(changed_artifact_ids),
            "redacted_artifact_ids": changed_artifact_ids,
            "redacted_quality_ratings": redacted_quality_ratings,
            "redacted_provenance_links": redacted_provenance_links,
            "idempotent_replay": not changed,
        }

    def redact_memory_content(self, *, memory_id: str, actor_type: str = "user") -> dict[str, object]:
        memory = self.get_memory(memory_id)
        assert memory is not None, memory_id
        memory.update(
            {
                "title": "[REDACTED]",
                "canonical_text": "[REDACTED]",
                "summary": "[REDACTED]",
                "value": {"redacted": True},
                "metadata_json": {"redacted": True},
                "status": "archived",
            }
        )
        self.append_event(
            {
                "event_type": "memory.redacted",
                "actor_type": actor_type,
                "target_type": "memory",
                "target_id": memory_id,
                "payload_json": {"operation": "redact_memory_content"},
            }
        )
        return memory

    def redact_memory_revisions(self, *, memory_id: str, actor_type: str = "user") -> dict[str, object]:
        redacted = 0
        for revision in self.revisions:
            if str(revision.get("memory_id")) == str(memory_id):
                revision.update({"text_before": "[REDACTED]", "text_after": "[REDACTED]", "reason": "[REDACTED]"})
                redacted += 1
        self.append_event(
            {
                "event_type": "memory.redacted",
                "actor_type": actor_type,
                "target_type": "memory",
                "target_id": memory_id,
                "payload_json": {"operation": "redact_memory_revisions", "redacted_revisions": redacted},
            }
        )
        return {"memory_id": memory_id, "redacted_revisions": redacted}

    def redact_memory_events(self, *, memory_id: str, actor_type: str = "user") -> dict[str, object]:
        redacted = 0
        for event in self.events:
            if str(event.get("target_id")) == str(memory_id):
                event["payload_json"] = {"redacted": True, "memory_id": memory_id}
                redacted += 1
        self.append_event(
            {
                "event_type": "memory.redacted",
                "actor_type": actor_type,
                "target_type": "memory",
                "target_id": memory_id,
                "payload_json": {"operation": "redact_memory_events", "redacted_events": redacted},
            }
        )
        return {"memory_id": memory_id, "redacted_events": redacted}

    def append_revision(self, revision: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**revision, "id": f"revision-{len(self.revisions) + 1}"}
        self.revisions.append(row)
        return row

    def list_revisions(self, memory_id: str) -> list[dict[str, object]]:
        return [revision for revision in self.revisions if revision.get("memory_id") == memory_id]

    def create_provenance_link(self, link: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**link, "id": f"provenance-{len(self.provenance_links) + 1}"}
        self.provenance_links.append(row)
        return row

    def create_open_loop(self, loop: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**loop, "id": f"loop-{len(self.open_loops) + 1}", "status": loop.get("status", "open")}
        self.open_loops.append(row)
        return row

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del query, domains, sensitivity_allowed
        return self.memories[:limit]

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del query, domains, sensitivity_allowed
        return list(self.sources.values())[:limit]

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
        del domains, sensitivity_allowed
        rows = [
            row
            for row in self.open_loops
            if (status is None or row.get("status") == status)
            and (project_id is None or row.get("project_id") == project_id)
            and (person_id is None or row.get("person_id") == person_id)
        ]
        return rows[:limit]

    def list_open_loops_referencing_source(self, *, source_id: str, limit: int = 500) -> list[dict[str, object]]:
        return [
            open_loop
            for open_loop in self.open_loops
            if vnext_shared._vnext_row_references_source(open_loop, source_id)
        ][:limit]

    def get_open_loop(self, loop_id: str) -> dict[str, object] | None:
        for loop in self.open_loops:
            if loop["id"] == loop_id:
                return loop
        return None

    def update_open_loop(self, *, loop_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        loop = self.get_open_loop(loop_id)
        if loop is None:
            raise AssertionError(loop_id)
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
        loop = self.update_open_loop(loop_id=loop_id, patch={"status": status})
        if resolution_note is not None:
            loop["resolution_note"] = resolution_note
        return loop

    def list_provenance_links(self, *, target_type: str, target_id: str) -> list[dict[str, object]]:
        return [
            link
            for link in self.provenance_links
            if link.get("target_type") == target_type and link.get("target_id") == target_id
        ]

    def create_task(self, task: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**task, "id": str(uuid4()), "status": task.get("status", "pending")}
        self.tasks.append(row)
        return row

    def claim_next_task(self) -> dict[str, object] | None:
        for task in self.tasks:
            if task.get("status") == "pending":
                task["status"] = "running"
                return task
        return None

    def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        details: dict[str, object] | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        for task in self.tasks:
            if task.get("id") == task_id:
                task["status"] = status
                if details is not None:
                    task.update(details)
                return task
        raise AssertionError(task_id)

    def create_artifact(self, artifact: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**artifact, "id": str(uuid4())}
        self.artifacts[str(row["id"])] = row
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 4,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [
            row for row in self.artifacts.values() if artifact_type is None or row.get("artifact_type") == artifact_type
        ]
        return rows[:limit]

    def list_artifacts_referencing_source(self, *, source_id: str, limit: int = 500) -> list[dict[str, object]]:
        return [
            artifact
            for artifact in self.artifacts.values()
            if vnext_shared._vnext_row_references_source(artifact, source_id)
        ][:limit]

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

    def create_artifact_quality_rating(self, rating: dict[str, object], **_kwargs) -> dict[str, object]:
        reviewer_id = rating.get("reviewer_id")
        if reviewer_id is not None:
            for row in self.quality_ratings:
                if (
                    row.get("artifact_id") == rating.get("artifact_id")
                    and row.get("reviewer_id") == reviewer_id
                ):
                    row.update(rating)
                    return row
        row = {**rating, "id": f"quality-{len(self.quality_ratings) + 1}"}
        self.quality_ratings.append(row)
        return row

    def list_artifact_quality_ratings(
        self,
        *,
        artifact_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        rows = [row for row in self.quality_ratings if artifact_id is None or row.get("artifact_id") == artifact_id]
        return rows[:limit]

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return self.projects.get(project_id)

    def get_project_for_update(self, project_id: str) -> dict[str, object] | None:
        return self.get_project(project_id)

    def list_projects(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [row for row in self.projects.values() if status is None or row.get("status") == status]
        return rows[:limit]

    def update_project(self, *, project_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        project = self.projects[project_id]
        project.update(patch)
        return project

    def create_edge(self, edge: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        del actor_type
        row = {**edge, "id": f"edge-{len(self.edges) + 1}"}
        self.edges[str(row["id"])] = row
        return row

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

    def create_belief(self, belief: dict[str, object]) -> dict[str, object]:
        row = {**belief, "id": f"belief-{len(self.beliefs) + 1}"}
        self.beliefs[str(row["id"])] = row
        return row

    def get_belief(self, belief_id: str) -> dict[str, object] | None:
        return self.beliefs.get(belief_id)

    def list_beliefs(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [row for row in self.beliefs.values() if status is None or row.get("status") == status]
        return rows[:limit]

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

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        rows = [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]
        return rows[:limit] if limit is not None else rows

    def list_project_update_events(
        self,
        *,
        artifact_id: str,
        candidate_memory_id: str,
    ) -> list[dict[str, object]]:
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
            linked = (
                (event.get("target_type") == "artifact" and event.get("target_id") == artifact_id)
                or (event.get("target_type") == "memory" and event.get("target_id") == candidate_memory_id)
                or payload.get("artifact_id") == artifact_id
                or payload.get("candidate_memory_id") == candidate_memory_id
                or payload.get("memory_id") == candidate_memory_id
            )
            if linked:
                rows.append(event)
        return rows

    def list_events_for_source_trace(
        self,
        *,
        source_id: str,
        memory_ids: list[str] | tuple[str, ...] = (),
        artifact_ids: list[str] | tuple[str, ...] = (),
        open_loop_ids: list[str] | tuple[str, ...] = (),
        limit: int = 500,
    ) -> list[dict[str, object]]:
        return [
            event
            for event in self.events
            if vnext_shared._vnext_event_references(
                event,
                source_id=source_id,
                memory_ids=set(memory_ids),
                artifact_ids=set(artifact_ids),
                open_loop_ids=set(open_loop_ids),
            )
        ][:limit]

    def list_agent_events(self, *, agent_id: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        return [
            event
            for event in self.events
            if event.get("actor_type") == "agent" and (agent_id is None or event.get("actor_id") == agent_id)
        ][:limit]

    def list_agent_policy_artifacts(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        return [
            artifact
            for artifact in self.artifacts.values()
            if vnext_shared._vnext_metadata(artifact).get("generated_by") == "agent"
            and (agent_id is None or vnext_shared._vnext_metadata(artifact).get("agent_id") == agent_id)
        ][:limit]

    def list_agent_policy_memories(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        return [
            memory
            for memory in self.memories
            if vnext_shared._vnext_metadata(memory).get("agent_id") is not None
            and (agent_id is None or vnext_shared._vnext_metadata(memory).get("agent_id") == agent_id)
        ][:limit]

    def upsert_agent_identity(self, identity: dict[str, object], **_kwargs) -> dict[str, object]:
        self.agent_identities[str(identity["agent_id"])] = identity
        return identity

    def create_agent_api_key(self, key: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **key,
            "id": str(uuid4()),
            "created_at": "now",
            "revoked_at": None,
            "last_used_at": None,
        }
        self.agent_api_keys.append(row)
        return row

    def get_agent_api_key_by_hash(self, key_hash: str) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row.get("key_hash") == key_hash:
                return row
        return None

    def list_agent_api_keys(self, *, limit: int = 50) -> list[dict[str, object]]:
        return self.agent_api_keys[:limit]

    def revoke_agent_api_key(self, *, key_id: str, **_kwargs) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row["id"] == key_id and row.get("revoked_at") is None:
                row["revoked_at"] = "now"
                return row
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, object]:
        for row in self.agent_api_keys:
            if row["id"] == key_id:
                row["last_used_at"] = "now"
                return row
        raise AssertionError(key_id)

    def count_active_agent_api_keys(self) -> int:
        return len([row for row in self.agent_api_keys if row.get("revoked_at") is None])

    def list_scheduler_runs(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def connector_storage_status(self) -> dict[str, object]:
        return {
            "connector_settings_exists": True,
            "connector_state_exists": True,
            "artifact_quality_ratings_exists": True,
            "scheduler_workflows_exists": True,
            "scheduler_runs_exists": True,
            "pgvector_version": "0.8.0",
            "migration_revision": "test",
        }

    def list_connector_settings(self) -> list[dict[str, object]]:
        return [
            {
                "connector_name": name,
                "enabled": False,
                "configured": True,
                "default_domain": "project",
                "default_sensitivity": "private",
                "sync_mode": "manual",
                "poll_interval_seconds": None,
                "secret_ref": None,
                "validation_errors_json": [],
                "metadata_json": {"config_json": {}},
            }
            for name in ("telegram", "local_folder", "browser_clipper", "agent_output")
        ]

    def list_connector_states(self) -> list[dict[str, object]]:
        return [
            {
                "connector_name": name,
                "cursor_type": "sync_cursor",
                "cursor_value": None,
                "state_json": {},
                "items_seen": 0,
                "items_captured": 0,
                "items_deduped": 0,
                "items_failed": 0,
            }
            for name in ("telegram", "local_folder", "browser_clipper", "agent_output")
        ]


def _install_fake_vnext_store(monkeypatch, store: FakeVNextStore) -> None:
    from alicebot_api import vnext_label_writes
    monkeypatch.setattr(vnext_label_writes, "acquire_exclusive_label_lock", lambda target: target.lock_label_writes(exclusive=True))
    monkeypatch.setattr(
        vnext_label_writes, "held_label_locks",
        lambda target: (target.graph_locked, target.labels_locked, target.labels_exclusive),
    )

    @contextmanager
    def fake_user_connection(database_url, current_user_id):
        assert database_url == "postgresql://db"
        assert current_user_id is not None
        yield object()

    for module in (
        main_module,
        vnext_memories_router,
        vnext_projects_router,
        vnext_retrieval_router,
        vnext_review_router,
        workspaces_router,
    ):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url="postgresql://db"))
        monkeypatch.setattr(module, "user_connection", fake_user_connection)
        monkeypatch.setattr(module, "PostgresVNextStore", lambda _conn: store)


def _invoke_vnext_request(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
    authorization: str | None = None,
    content_type: str = "application/json",
    origin: str | None = None,
) -> tuple[int, dict[str, object]]:
    messages: list[dict[str, object]] = []
    body = b"" if payload is None else json.dumps(payload).encode()
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    # A keyless request must name this machine in Host (DB-005), as every real client does.
    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", content_type.encode())]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body)


@pytest.mark.parametrize("content_type", ("application/json", "text/plain;charset=UTF-8"))
@pytest.mark.parametrize(
    "payload_factory",
    (
        lambda user_id, capability: {
            "user_id": user_id,
            "capture_capability": capability,
        },
        lambda user_id, _capability: {
            "user_id": user_id,
            "url": "https://example.test/article",
            "capture_capability": f"alice_clip_{'S' * 191}",
        },
        lambda user_id, capability: {
            "user_id": user_id,
            "url": {"unexpected": "object"},
            "capture_capability": capability,
        },
    ),
)
def test_browser_clip_validation_errors_never_echo_capability(
    monkeypatch,
    content_type: str,
    payload_factory,
) -> None:
    _install_fake_vnext_store(monkeypatch, FakeVNextStore(None))
    capability = f"alice_clip_{'S' * 43}"
    payload = payload_factory(str(uuid4()), capability)

    status, response_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/connectors/browser-clipper/capture",
        payload=payload,
        content_type=content_type,
        origin="https://example.test",
    )

    serialized_response = json.dumps(response_payload, sort_keys=True)
    assert status == 422
    assert response_payload == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body"],
                "msg": "Input validation failed",
            }
        ]
    }
    assert "alice_clip_" not in serialized_response
    assert "S" * 43 not in serialized_response


def test_vnext_source_http_rejects_invalid_domain_and_sensitivity_enums(monkeypatch) -> None:
    _install_fake_vnext_store(monkeypatch, FakeVNextStore(None))
    user_id = str(uuid4())
    invalid_domain_status, invalid_domain_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/sources",
        payload={
            "user_id": user_id,
            "raw_text": "Fact: Invalid enum values never reach persistence.",
            "domain": "not-a-domain",
        },
    )
    invalid_sensitivity_status, invalid_sensitivity_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/sources",
        payload={
            "user_id": user_id,
            "raw_text": "Fact: Invalid enum values never reach persistence.",
            "sensitivity": "top-secret-ish",
        },
    )

    assert invalid_domain_status == 422
    assert invalid_sensitivity_status == 422
    assert invalid_domain_payload["detail"][0]["loc"][-1] == "domain"
    assert invalid_sensitivity_payload["detail"][0]["loc"][-1] == "sensitivity"


def test_vnext_http_auth_gate_covers_query_and_json_routes(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    # Fresh local installs keep their explicit keyless compatibility path.
    assert _invoke_vnext_request("GET", "/v0/vnext/projects", query={"user_id": str(user_id)})[0] == 200

    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    assert _invoke_vnext_request("GET", "/v0/vnext/projects", query={"user_id": str(user_id)})[0] == 401
    assert (
        _invoke_vnext_request(
            "GET",
            "/v0/vnext/projects",
            query={"user_id": str(user_id)},
            authorization=raw_key,
        )[0]
        == 401
    )
    assert (
        _invoke_vnext_request(
            "GET",
            "/v0/vnext/projects",
            query={"user_id": str(user_id)},
            authorization="Bearer alice_sk_invalid",
        )[0]
        == 401
    )
    assert (
        _invoke_vnext_request(
            "GET",
            "/v0/vnext/projects",
            query={"user_id": str(user_id)},
            authorization=f"Bearer {raw_key}",
        )[0]
        == 200
    )

    created_status, _created_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/sources",
        authorization=f"Bearer {raw_key}",
        payload={
            "user_id": str(user_id),
            "raw_text": "Fact: the centralized gate preserves JSON bodies.",
            "domain": "project",
            "sensitivity": "internal",
        },
    )
    assert created_status == 201
    assert store.sources
    assert _invoke_vnext_request("GET", "/v0/vnext/projects")[0] == 400

    _scoped_record, scoped_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="project-reader",
        permission_profile="read_only_agent",
        project_scope="project-a",
    )
    scoped_status, scoped_payload = _invoke_vnext_request(
        "GET",
        "/v0/vnext/projects",
        query={"user_id": str(user_id)},
        authorization=f"Bearer {scoped_key}",
    )
    assert scoped_status == 403
    assert "trusted_or_admin_agent_required_for_operator_route" in scoped_payload["policy_decision"]["reasons"]
    assert "unbound_operator_key_required" in scoped_payload["policy_decision"]["reasons"]

    _reader_record, reader_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="unbound-reader",
        permission_profile="read_only_agent",
    )
    mutation_status, mutation_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/doctor/run",
        authorization=f"Bearer {reader_key}",
        payload={"user_id": str(user_id)},
    )
    assert mutation_status == 403
    assert "trusted_or_admin_agent_required_for_operator_route" in mutation_payload["policy_decision"]["reasons"]

    # Every central operator-console route requires an unbound trusted/admin
    # key once keys exist. Target-aware routes retain their local policy.
    assert (
        _invoke_vnext_request(
            "GET",
            "/v0/vnext/connectors",
            query={"user_id": str(user_id)},
            authorization=f"Bearer {raw_key}",
        )[0]
        == 200
    )
    assert (
        _invoke_vnext_request(
            "GET",
            "/v0/vnext/artifacts",
            query={"user_id": str(user_id)},
            authorization=f"Bearer {raw_key}",
        )[0]
        == 200
    )
    assert (
        _invoke_vnext_request(
            "POST",
            "/v0/vnext/queue/process-next",
            authorization=f"Bearer {raw_key}",
            payload={"user_id": str(user_id)},
        )[0]
        == 200
    )

    _project_operator_record, project_operator_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="unbound-project-operator",
        permission_profile="project_scoped_agent",
    )
    brain_status, brain_payload = _invoke_vnext_request(
        "PUT",
        "/v0/vnext/settings/brain-charter",
        authorization=f"Bearer {project_operator_key}",
        payload={"user_id": str(user_id)},
    )
    assert brain_status == 403
    assert "trusted_or_admin_agent_required_for_operator_route" in brain_payload["policy_decision"]["reasons"]

    _bound_operator_record, bound_operator_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="bound-trusted-operator",
        permission_profile="trusted_local_agent",
        project_scope="project-a",
    )
    bound_status, bound_payload = _invoke_vnext_request(
        "GET",
        "/v0/vnext/connectors",
        query={"user_id": str(user_id)},
        authorization=f"Bearer {bound_operator_key}",
    )
    assert bound_status == 403
    assert "unbound_operator_key_required" in bound_payload["policy_decision"]["reasons"]


def test_vnext_route_inventory_fails_closed_without_route_local_policy() -> None:
    routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    registered = {
        (method, str(route.path))
        for route in routes
        if str(getattr(route, "path", "")).startswith("/v0/vnext")
        for method in (getattr(route, "methods", None) or set())
        if method != "OPTIONS"
    }
    assert not (main_module._VNEXT_ROUTE_LOCAL_POLICY & main_module._VNEXT_CENTRAL_OPERATOR_ROUTES)
    assert (main_module._VNEXT_ROUTE_LOCAL_POLICY | main_module._VNEXT_CENTRAL_OPERATOR_ROUTES) == registered
    assert len(registered) == 72

    project_bound = main_module.AgentIdentity(
        agent_id="project-reader",
        permission_profile="read_only_agent",
        project_scope=("project-a",),
        project_scope_locked=True,
    )
    read_only = main_module.AgentIdentity(
        agent_id="reader",
        permission_profile="read_only_agent",
    )
    proposal_only = main_module.AgentIdentity(
        agent_id="proposer",
        permission_profile="memory_proposal_agent",
    )
    project_scoped = main_module.AgentIdentity(
        agent_id="project-operator",
        permission_profile="project_scoped_agent",
    )
    trusted = main_module.AgentIdentity(
        agent_id="trusted-operator",
        permission_profile="trusted_local_agent",
    )
    admin = main_module.AgentIdentity(
        agent_id="admin-operator",
        permission_profile="admin_agent",
    )
    bound_trusted = main_module.AgentIdentity(
        agent_id="bound-trusted",
        permission_profile="trusted_local_agent",
        project_scope=("project-a",),
        project_scope_locked=True,
    )
    for method, path in main_module._VNEXT_CENTRAL_OPERATOR_ROUTES:
        for identity in (project_bound, read_only, proposal_only, project_scoped, bound_trusted):
            decision = main_module._vnext_central_route_policy(
                identity=identity,
                method=method,
                route_path=path,
            )
            assert decision is not None and decision.decision == "blocked", (
                identity.permission_profile,
                method,
                path,
            )
        for identity in (trusted, admin):
            decision = main_module._vnext_central_route_policy(
                identity=identity,
                method=method,
                route_path=path,
            )
            assert decision is not None and decision.decision == "allowed", (
                identity.permission_profile,
                method,
                path,
            )
        assert (
            main_module._vnext_central_route_policy(
                identity=None,
                method=method,
                route_path=path,
            )
            is None
        )

    unknown = main_module._vnext_central_route_policy(
        identity=admin,
        method="GET",
        route_path="/v0/vnext/new-unclassified-route",
    )
    assert unknown is not None and unknown.decision == "blocked"
    assert unknown.reasons == ("vnext_route_not_classified",)


@pytest.mark.parametrize(
    ("concrete_path", "template_path"),
    (
        (
            f"/v0/vnext/traces/sources/{uuid4()}",
            "/v0/vnext/traces/sources/{source_id}",
        ),
        (
            f"/v0/vnext/traces/artifacts/{uuid4()}",
            "/v0/vnext/traces/artifacts/{artifact_id}",
        ),
    ),
)
def test_parameterized_retrieval_routes_keep_policy_template_paths(
    concrete_path: str,
    template_path: str,
) -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": concrete_path,
            "raw_path": concrete_path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )

    matched_path = main_module._matched_vnext_route_path(request)

    assert matched_path == template_path
    assert ("GET", matched_path) in main_module._VNEXT_ROUTE_LOCAL_POLICY or (
        "GET",
        matched_path,
    ) in main_module._VNEXT_CENTRAL_OPERATOR_ROUTES
    assert (
        main_module._vnext_central_route_policy(
            identity=None,
            method="GET",
            route_path=matched_path,
        )
        is None
    )


def test_vnext_memories_router_partitions_preserve_global_route_sequence() -> None:
    partition_manifests = (
        (vnext_memories_router.source_create_router, [("POST", "/v0/vnext/sources")]),
        (
            vnext_memories_router.connectors_router,
            [
                ("GET", "/v0/vnext/connectors"),
                ("GET", "/v0/vnext/connectors/health"),
                ("GET", "/v0/vnext/connectors/{connector_name}/status"),
                ("PATCH", "/v0/vnext/connectors/{connector_name}/config"),
                ("POST", "/v0/vnext/connectors/{connector_name}/sync"),
                ("POST", "/v0/vnext/connectors/telegram/sync"),
                ("POST", "/v0/vnext/connectors/local-folder/sync"),
                ("POST", "/v0/vnext/connectors/browser-clipper/capture"),
                ("POST", "/v0/vnext/connectors/browser-clipper/capabilities"),
                ("POST", "/v0/vnext/agents/ingest-output"),
                ("GET", "/v0/vnext/dogfooding"),
                ("GET", "/v0/vnext/doctor"),
                ("POST", "/v0/vnext/doctor/run"),
            ],
        ),
        (
            vnext_memories_router.source_review_router,
            [
                ("GET", "/v0/vnext/sources/{source_id}"),
                ("POST", "/v0/vnext/sources/{source_id}/regenerate"),
                ("POST", "/v0/vnext/sources/{source_id}/review"),
            ],
        ),
        (
            vnext_memories_router.source_delete_router,
            [("DELETE", "/v0/vnext/sources/{source_id}")],
        ),
        (
            vnext_memories_router.memory_router,
            [
                ("POST", "/v0/vnext/memories/{memory_id}/review"),
                ("POST", "/v0/vnext/memory-proposals"),
                ("POST", "/v0/vnext/memories/commit"),
                ("POST", "/v0/vnext/memories/confirm"),
                ("POST", "/v0/vnext/memories/undo"),
                ("POST", "/v0/vnext/memories/correct"),
                ("POST", "/v0/vnext/memories/forget"),
                ("POST", "/v0/vnext/memories/expire"),
                ("POST", "/v0/vnext/memories/unexpire"),
                ("POST", "/v0/vnext/memories/accept-consolidation"),
                ("POST", "/v0/vnext/memories/redact"),
                ("GET", "/v0/vnext/memories/recent-commits"),
                ("GET", "/v0/vnext/memories/{memory_id}/audit"),
            ],
        ),
    )
    for router, expected in partition_manifests:
        observed = [
            (method, str(route.path))
            for route in router.routes
            for method in sorted(getattr(route, "methods", None) or set())
        ]
        assert observed == expected
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.vnext_memories"}

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    expected_slice = [
        ("GET", "/v0/vnext/workspace"),
        *partition_manifests[0][1],
        ("POST", "/v0/vnext/projects"),
        ("GET", "/v0/vnext/projects"),
        *partition_manifests[1][1],
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"),
        *partition_manifests[2][1],
        ("GET", "/v0/vnext/traces/sources/{source_id}"),
        ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"),
        *partition_manifests[3][1],
        ("POST", "/v0/vnext/context-packs"),
        ("GET", "/v0/vnext/context-tree"),
        *partition_manifests[4][1],
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
    ]
    start = effective_pairs.index(expected_slice[0])

    assert effective_pairs[start : start + len(expected_slice)] == expected_slice


def test_vnext_review_router_partitions_preserve_global_route_sequence() -> None:
    feedback_manifest = [
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"),
    ]
    review_manifest = [
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"),
        ("POST", "/v0/vnext/artifacts/generate/connections"),
        ("POST", "/v0/vnext/artifacts/generate/contradictions"),
        ("POST", "/v0/vnext/queue/tasks"),
        ("POST", "/v0/vnext/queue/process-next"),
        ("GET", "/v0/vnext/artifacts"),
        ("GET", "/v0/vnext/artifacts/{artifact_id}"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/review"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"),
        ("GET", "/v0/vnext/quality-evals"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/export"),
        ("POST", "/v0/vnext/graph/edges/{edge_id}/review"),
        ("GET", "/v0/vnext/graph/neighborhood/{target_id}"),
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"),
        ("GET", "/v0/vnext/beliefs/{belief_id}/state"),
        ("POST", "/v0/vnext/projects/update-candidates"),
        ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"),
    ]
    for router, expected in (
        (vnext_review_router.insight_feedback_router, feedback_manifest),
        (vnext_review_router.review_router, review_manifest),
    ):
        observed = [
            (method, str(route.path))
            for route in router.routes
            for method in sorted(getattr(route, "methods", None) or set())
        ]
        assert observed == expected
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.vnext_review"}

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    feedback_slice = [
        ("POST", "/v0/vnext/doctor/run"),
        *feedback_manifest,
        ("GET", "/v0/vnext/sources/{source_id}"),
    ]
    feedback_start = effective_pairs.index(feedback_slice[0])
    assert effective_pairs[feedback_start : feedback_start + len(feedback_slice)] == feedback_slice

    review_slice = [
        ("GET", "/v0/vnext/memories/{memory_id}/audit"),
        *review_manifest,
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"),
    ]
    review_start = effective_pairs.index(review_slice[0])
    assert effective_pairs[review_start : review_start + len(review_slice)] == review_slice


def test_vnext_projects_router_partitions_preserve_global_route_sequence() -> None:
    project_core_manifest = [
        ("POST", "/v0/vnext/projects"),
        ("GET", "/v0/vnext/projects"),
    ]
    project_operations_manifest = [
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"),
        ("POST", "/v0/vnext/open-loops"),
        ("GET", "/v0/vnext/settings/brain-charter"),
        ("PUT", "/v0/vnext/settings/brain-charter"),
        ("GET", "/v0/vnext/scheduler/status"),
        ("GET", "/v0/vnext/scheduler/runs"),
        ("GET", "/v0/vnext/scheduler/failures"),
        ("GET", "/v0/vnext/agents/policy-telemetry"),
        ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"),
        ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"),
        ("POST", "/v0/vnext/scheduler/run-due"),
        ("POST", "/v0/vnext/scheduler/pause"),
        ("POST", "/v0/vnext/scheduler/resume"),
        ("POST", "/v0/vnext/open-loops/extract"),
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"),
    ]
    for router, expected in (
        (vnext_projects_router.project_core_router, project_core_manifest),
        (
            vnext_projects_router.project_operations_router,
            project_operations_manifest,
        ),
    ):
        observed = [
            (method, str(route.path))
            for route in router.routes
            for method in sorted(getattr(route, "methods", None) or set())
        ]
        assert observed == expected
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.vnext_projects"}

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    project_core_slice = [
        ("POST", "/v0/vnext/sources"),
        *project_core_manifest,
        ("GET", "/v0/vnext/connectors"),
    ]
    project_core_start = effective_pairs.index(project_core_slice[0])
    assert effective_pairs[project_core_start : project_core_start + len(project_core_slice)] == project_core_slice

    project_operations_slice = [
        ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"),
        *project_operations_manifest,
        ("POST", "/v0/continuity/captures/candidates"),
    ]
    project_operations_start = effective_pairs.index(project_operations_slice[0])
    assert (
        effective_pairs[project_operations_start : project_operations_start + len(project_operations_slice)]
        == project_operations_slice
    )


def test_continuity_router_partitions_preserve_global_route_sequence() -> None:
    capture_manifest = [("POST", "/v0/continuity/captures")]
    operations_manifest = [
        ("POST", "/v0/continuity/captures/candidates"),
        ("POST", "/v0/continuity/captures/commit"),
        ("POST", "/v1/memory/operations/candidates/generate"),
        ("GET", "/v1/memory/operations/candidates"),
        ("POST", "/v1/memory/operations/commit"),
        ("GET", "/v1/memory/operations"),
        ("GET", "/v0/continuity/captures"),
        ("GET", "/v0/continuity/captures/{capture_event_id}"),
        ("GET", "/v0/admin/debug/continuity/lifecycle"),
        ("GET", "/v0/admin/debug/continuity/lifecycle/{continuity_object_id}"),
        ("GET", "/v0/continuity/review-queue"),
        ("GET", "/v0/continuity/review-queue/{continuity_object_id}"),
        ("GET", "/v0/continuity/explain/{continuity_object_id}"),
        ("POST", "/v1/contradictions/detect"),
        ("GET", "/v1/contradictions/cases"),
        ("GET", "/v1/contradictions/cases/{contradiction_case_id}"),
        ("POST", "/v1/contradictions/cases/{contradiction_case_id}/resolve"),
        ("GET", "/v1/trust/signals"),
        ("GET", "/v0/state-at"),
        ("GET", "/v0/timeline"),
        ("GET", "/v0/explain"),
        ("GET", "/v0/patterns"),
        ("GET", "/v0/patterns/{pattern_id}"),
        ("GET", "/v0/playbooks"),
        ("GET", "/v0/playbooks/{playbook_id}"),
        ("GET", "/v0/admin/debug/continuity/artifacts/{artifact_id}"),
        ("POST", "/v0/continuity/review-queue/{continuity_object_id}/corrections"),
        ("GET", "/v0/continuity/open-loops"),
        ("GET", "/v0/continuity/daily-brief"),
        ("GET", "/v0/continuity/weekly-review"),
        ("POST", "/v0/continuity/open-loops/{continuity_object_id}/review-action"),
        ("GET", "/v0/continuity/recall"),
        ("GET", "/v0/continuity/retrieval-runs"),
        ("GET", "/v0/continuity/retrieval-runs/{retrieval_run_id}"),
        ("GET", "/v0/continuity/retrieval-evaluation"),
        ("GET", "/v1/evals/suites"),
        ("POST", "/v1/evals/runs"),
        ("GET", "/v1/evals/runs"),
        ("GET", "/v1/evals/runs/{eval_run_id}"),
        ("GET", "/v0/continuity/resumption-brief"),
        ("POST", "/v1/continuity/brief"),
    ]
    for router, expected in (
        (continuity_router.capture_router, capture_manifest),
        (continuity_router.operations_router, operations_manifest),
    ):
        observed = [
            (method, str(route.path))
            for route in router.routes
            for method in sorted(getattr(route, "methods", None) or set())
        ]
        assert observed == expected
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.continuity"}

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    capture_slice = [
        ("POST", "/v0/memories/capture-explicit-signals"),
        *capture_manifest,
        ("GET", "/v0/vnext/workspace"),
    ]
    capture_start = effective_pairs.index(capture_slice[0])
    assert effective_pairs[capture_start : capture_start + len(capture_slice)] == capture_slice

    operations_slice = [
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"),
        *operations_manifest,
    ]
    operations_start = effective_pairs.index(operations_slice[0])
    assert effective_pairs[operations_start : operations_start + len(operations_slice)] == operations_slice

    main_source = Path(main_module.__file__).read_text(encoding="utf-8")
    assert (
        main_source.index("app.include_router(vnext_projects.project_operations_router)")
        < main_source.index("app.include_router(continuity.operations_router)")
        < main_source.index("app.include_router(legacy_gated.task_brief_router)")
    )


def test_memories_legacy_router_partitions_preserve_global_route_sequence() -> None:
    core_manifest = [
        ("GET", "/v0/agent-profiles"),
        ("POST", "/v0/context/compile"),
        ("POST", "/v0/threads"),
        ("GET", "/v0/threads"),
        ("GET", "/v0/threads/health-dashboard"),
        ("GET", "/v0/threads/{thread_id}"),
        ("GET", "/v0/threads/{thread_id}/sessions"),
        ("GET", "/v0/threads/{thread_id}/events"),
        ("GET", "/v0/threads/{thread_id}/resumption-brief"),
        ("GET", "/v0/traces"),
        ("GET", "/v0/traces/{trace_id}"),
        ("GET", "/v0/traces/{trace_id}/events"),
        ("POST", "/v0/memories/admit"),
        ("GET", "/v0/open-loops"),
        ("GET", "/v0/open-loops/{open_loop_id}"),
        ("POST", "/v0/open-loops"),
        ("POST", "/v0/open-loops/{open_loop_id}/status"),
        ("POST", "/v0/consents"),
        ("GET", "/v0/consents"),
        ("POST", "/v0/policies"),
        ("GET", "/v0/policies"),
        ("GET", "/v0/policies/{policy_id}"),
        ("POST", "/v0/policies/evaluate"),
    ]
    task_artifact_manifest = [
        ("GET", "/v0/task-artifacts"),
        ("GET", "/v0/task-artifacts/{task_artifact_id}"),
        ("POST", "/v0/task-artifacts/{task_artifact_id}/ingest"),
        ("GET", "/v0/task-artifacts/{task_artifact_id}/chunks"),
    ]
    task_artifact_retrieval_manifest = [
        ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/retrieve"),
    ]
    task_artifact_semantic_manifest = [
        ("POST", "/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval"),
    ]
    signals_manifest = [
        ("POST", "/v0/memories/extract-explicit-preferences"),
        ("POST", "/v0/open-loops/extract-explicit-commitments"),
        ("POST", "/v0/memories/capture-explicit-signals"),
    ]
    memory_manifest = [
        ("GET", "/v0/memories"),
        ("GET", "/v0/memories/review-queue"),
        ("GET", "/v0/memories/quality-gate"),
        ("GET", "/v0/memories/trust-dashboard"),
        ("GET", "/v0/memories/hygiene-dashboard"),
        ("GET", "/v0/memories/evaluation-summary"),
        ("POST", "/v0/memories/semantic-retrieval"),
        ("GET", "/v0/memories/{memory_id}"),
        ("GET", "/v0/memories/{memory_id}/revisions"),
        ("POST", "/v0/memories/{memory_id}/labels"),
        ("GET", "/v0/memories/{memory_id}/labels"),
        ("POST", "/v0/embedding-configs"),
        ("GET", "/v0/embedding-configs"),
        ("POST", "/v0/memory-embeddings"),
        ("POST", "/v0/task-artifact-chunk-embeddings"),
        ("GET", "/v0/memories/{memory_id}/embeddings"),
        ("GET", "/v0/task-artifacts/{task_artifact_id}/chunk-embeddings"),
        ("GET", "/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings"),
        ("GET", "/v0/memory-embeddings/{memory_embedding_id}"),
        ("GET", "/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}"),
        ("POST", "/v0/entities"),
        ("POST", "/v0/entity-edges"),
        ("GET", "/v0/entities"),
        ("GET", "/v0/entities/{entity_id}/edges"),
        ("GET", "/v0/entities/{entity_id}"),
    ]
    partition_manifests = (
        (memories_legacy_router.core_router, core_manifest),
        (memories_legacy_router.task_artifact_router, task_artifact_manifest),
        (
            memories_legacy_router.task_artifact_retrieval_router,
            task_artifact_retrieval_manifest,
        ),
        (
            memories_legacy_router.task_artifact_semantic_router,
            task_artifact_semantic_manifest,
        ),
        (memories_legacy_router.signals_router, signals_manifest),
        (memories_legacy_router.memory_router, memory_manifest),
    )
    for router, expected in partition_manifests:
        observed = [
            (method, str(route.path))
            for route in router.routes
            for method in sorted(getattr(route, "methods", None) or set())
        ]
        assert observed == expected
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.memories_legacy"}

    assert legacy_gated_router.RetrieveArtifactChunksRequest is memories_legacy_router.RetrieveArtifactChunksRequest
    assert (
        legacy_gated_router.RetrieveSemanticArtifactChunksRequest
        is memories_legacy_router.RetrieveSemanticArtifactChunksRequest
    )

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    default_front_slice = [
        ("GET", "/healthz"),
        *core_manifest,
        *task_artifact_manifest,
        *task_artifact_retrieval_manifest,
        *task_artifact_semantic_manifest,
        *signals_manifest,
        ("POST", "/v0/continuity/captures"),
    ]
    front_start = effective_pairs.index(default_front_slice[0])
    assert effective_pairs[front_start : front_start + len(default_front_slice)] == default_front_slice

    default_memory_slice = [
        ("POST", "/v1/continuity/brief"),
        *memory_manifest,
        ("POST", "/v1/workspaces/bootstrap"),
    ]
    memory_start = effective_pairs.index(default_memory_slice[0])
    assert effective_pairs[memory_start : memory_start + len(default_memory_slice)] == default_memory_slice

    main_source = Path(main_module.__file__).read_text(encoding="utf-8")
    ordered_anchors = (
        '@app.get("/healthz")',
        "app.include_router(memories_legacy.core_router)",
        "app.include_router(legacy_gated.core_router)",
        "app.include_router(memories_legacy.task_artifact_router)",
        "app.include_router(legacy_gated.task_artifact_retrieval_router)",
        "app.include_router(memories_legacy.task_artifact_retrieval_router)",
        "app.include_router(legacy_gated.task_artifact_semantic_router)",
        "app.include_router(memories_legacy.task_artifact_semantic_router)",
        "app.include_router(legacy_gated.operations_router)",
        "app.include_router(memories_legacy.signals_router)",
        "app.include_router(continuity.capture_router)",
        "app.include_router(workspaces.core_router)",
        "app.include_router(vnext_memories.source_create_router)",
        "app.include_router(legacy_gated.task_brief_router)",
        "app.include_router(memories_legacy.memory_router)",
        "app.include_router(workspaces.bootstrap_router)",
        "app.include_router(providers.router)",
    )
    anchor_positions = [main_source.index(anchor) for anchor in ordered_anchors]
    assert anchor_positions == sorted(anchor_positions)


@pytest.mark.parametrize(
    "concrete_path",
    (
        "/v0/vnext/connectors/telegram/sync",
        "/v0/vnext/connectors/local-folder/sync",
    ),
)
def test_vnext_connector_literal_sync_paths_preserve_generic_first_match(
    concrete_path: str,
) -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": concrete_path,
            "raw_path": concrete_path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )
    full_match_paths: list[str] = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        route_contexts = effective_route_contexts() if callable(effective_route_contexts) else (route,)
        for route_context in route_contexts:
            match, _child_scope = route_context.matches(request.scope)
            if match is Match.FULL:
                full_match_paths.append(str(route_context.path))

    assert full_match_paths[:2] == [
        "/v0/vnext/connectors/{connector_name}/sync",
        concrete_path,
    ]
    assert main_module._matched_vnext_route_path(request) == "/v0/vnext/connectors/{connector_name}/sync"


def test_retrieval_router_mount_preserves_original_route_sequence() -> None:
    moved_paths = [
        "/v0/vnext/traces/sources/{source_id}",
        "/v0/vnext/traces/artifacts/{artifact_id}",
        "/v0/vnext/context-packs",
        "/v0/vnext/context-tree",
    ]
    moved_routes = [
        *vnext_retrieval_router.trace_router.routes,
        *vnext_retrieval_router.context_router.routes,
    ]
    assert [route.path for route in moved_routes] == moved_paths
    assert {route.endpoint.__module__ for route in moved_routes} == {"alicebot_api.routers.vnext_retrieval"}

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    expected_sequence = [
        ("GET", "/v0/vnext/traces/sources/{source_id}"),
        ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"),
        ("DELETE", "/v0/vnext/sources/{source_id}"),
        ("POST", "/v0/vnext/context-packs"),
        ("GET", "/v0/vnext/context-tree"),
    ]
    relevant_pairs = set(expected_sequence)
    observed_sequence = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
        if (method, str(getattr(route, "path", ""))) in relevant_pairs
    ]

    assert observed_sequence == expected_sequence


def test_create_vnext_source_endpoint_captures_text(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: vNext source API preserves provenance.",
            title="API capture",
            domain="project",
            sensitivity="private",
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert payload["status"] == "imported"
    assert payload["candidate_memory_count"] == 1
    assert list(store.sources.values())[0]["domain"] == "project"
    assert store.memories[0]["canonical_text"] == "vNext source API preserves provenance."


def _sqlite_vnext_store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "capture-scope@example.com")
    return SQLiteVNextStore(conn, user_id)


def test_create_vnext_source_threads_project_scope_into_captured_memory(monkeypatch) -> None:
    # Audit P1 #4: the HTTP handler validates the request's project scope but,
    # before the fix, dropped it on the way into capture -- the memory persisted
    # with an empty scope, so the owning project's filtered recall found nothing
    # while unscoped recall found it. Uses the real SQLite store so the recall
    # filter (search_memories_fts projects clause) is exercised end to end.
    store = _sqlite_vnext_store()
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=uuid4(),
            raw_text="Decision: The Helios launch ships behind a staged rollout flag.",
            title="Helios launch decision",
            domain="project",
            sensitivity="internal",
            project_scope=["  Project-Helios  ", "project-helios"],
        )
    )
    assert response.status_code == 201
    assert json.loads(response.body)["status"] == "imported"

    candidates = store.list_memories(status="candidate")
    assert candidates, "capture must promote at least one candidate memory"
    assert memory_project_scope(candidates[0]) == ("Project-Helios",)
    for memory in candidates:
        store.update_memory(memory_id=str(memory["id"]), patch={"status": "active"}, actor_type="system")

    owning = store.search_memories_fts(query="Helios staged rollout", projects=("project-helios",), limit=10)
    other = store.search_memories_fts(query="Helios staged rollout", projects=("project-decoy",), limit=10)
    unscoped = store.search_memories_fts(query="Helios staged rollout", limit=10)

    assert len(owning) == 1, "the owning project's filtered recall must retrieve the captured memory"
    assert len(other) == 0, "another project must not see the captured memory"
    assert len(unscoped) == 1


def test_vnext_connector_endpoints_list_and_sync_payloads(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    list_response = vnext_memories_router.list_vnext_connectors(user_id=user_id)
    sync_response = vnext_memories_router.sync_vnext_connector(
        "browser_clipper",
        vnext_memories_router.VNextConnectorSyncRequest(
            user_id=user_id,
            items=[
                {
                    "external_id": "clip-1",
                    "cursor": "1",
                    "title": "API connector clip",
                    "url": "https://example.test/api-clip",
                    "text": "Fact: API connector sync preserves raw evidence.",
                }
            ],
            default_domain="learning",
            default_sensitivity="private",
        ),
    )

    list_payload = json.loads(list_response.body)
    sync_payload = json.loads(sync_response.body)
    assert list_response.status_code == 200
    assert "browser_clipper" in list_payload["order"]
    assert sync_response.status_code == 201
    assert sync_payload["status"] == "ok"
    assert sync_payload["sync_cursor"] == "1"
    source = next(iter(store.sources.values()))
    assert source["connector_name"] == "browser_clipper"
    assert source["metadata_json"]["raw_payload"]["external_id"] == "clip-1"
    assert store.events[-1]["event_type"] == "connector.sync_completed"


def test_get_vnext_source_endpoint_returns_404_for_missing_source(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    missing_source_id = uuid4()

    response = vnext_memories_router.get_vnext_source(missing_source_id, user_id=uuid4())

    assert response.status_code == 404
    assert f"vNext source {missing_source_id} was not found" in json.loads(response.body)["detail"]


def test_delete_vnext_source_endpoint_soft_deletes_source(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {"id": source_id, "deleted_at": None}
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_memories_router.delete_vnext_source(source_id=uuid4(), user_id=uuid4())
    assert response.status_code == 404

    response = vnext_memories_router.delete_vnext_source(source_id=main_module.UUID(source_id), user_id=uuid4())

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["id"] == source_id
    assert payload["deleted_at"] == "now"


def test_vnext_source_review_trace_and_doctor_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    artifact_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "source_type": "manual_text",
        "title": "Operator console source",
        "content_hash": "sha256:source",
        "captured_at": "2026-05-12T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {
            "raw_text": "Fact: source review persists.",
            "project_id": "project-old",
            "project_scope": ["project-old"],
        },
    }
    store.chunks.append(
        {"id": "chunk-1", "source_id": source_id, "chunk_index": 0, "text": "Fact: source review persists."}
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_key": "memory.operator.source",
            "memory_type": "semantic",
            "canonical_text": "Source review persists.",
            "status": "candidate",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"source_id": source_id},
        }
    )
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "daily_brief",
        "title": "Daily",
        "content_markdown": "# Daily",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"source_refs": [f"source:{source_id}"]},
    }
    store.open_loops.append({"id": "loop-1", "title": "Review source", "source_id": source_id, "status": "open"})
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    review_response = vnext_memories_router.review_vnext_source(
        main_module.UUID(source_id),
        vnext_memories_router.VNextSourceReviewRequest(
            user_id=user_id,
            action="assign_project",
            title="Reviewed source",
            domain="project",
            sensitivity="private",
            project_id="project-1",
            review_note="Reviewed from test.",
        ),
    )
    trace_response = vnext_retrieval_router.get_vnext_source_trace(main_module.UUID(source_id), user_id=user_id)
    artifact_trace_response = vnext_retrieval_router.get_vnext_artifact_trace(
        main_module.UUID(artifact_id), user_id=user_id
    )
    doctor_response = vnext_memories_router.run_vnext_doctor(
        vnext_memories_router.VNextDoctorRunRequest(user_id=user_id, fix_safe=False, ci=True)
    )

    review_payload = json.loads(review_response.body)
    old_scope_response = vnext_retrieval_router.create_vnext_context_pack(
        vnext_retrieval_router.VNextContextPackRequest(
            user_id=user_id,
            query="source review persists",
            scope={"projects": ["project-old"]},
            options={"include_sources": True},
        )
    )
    new_scope_response = vnext_retrieval_router.create_vnext_context_pack(
        vnext_retrieval_router.VNextContextPackRequest(
            user_id=user_id,
            query="source review persists",
            scope={"projects": ["project-1"]},
            options={"include_sources": True},
        )
    )
    old_scope_payload = json.loads(old_scope_response.body)
    new_scope_payload = json.loads(new_scope_response.body)
    trace_payload = json.loads(trace_response.body)
    artifact_trace_payload = json.loads(artifact_trace_response.body)
    doctor_payload = json.loads(doctor_response.body)

    assert review_response.status_code == 200
    assert review_payload["source"]["title"] == "Reviewed source"
    assert review_payload["source"]["metadata_json"]["project_id"] == "project-1"
    assert review_payload["source"]["metadata_json"]["project_scope"] == ["project-1"]
    assert old_scope_response.status_code == 201
    assert old_scope_payload["sources"] == []
    assert new_scope_response.status_code == 201
    assert [row["id"] for row in new_scope_payload["sources"]] == [source_id]
    assert review_payload["trace"]["summary"]["candidate_memory_count"] == 1
    assert trace_response.status_code == 200
    assert trace_payload["summary"]["chunk_count"] == 1
    assert trace_payload["summary"]["artifact_count"] == 1
    assert artifact_trace_response.status_code == 200
    assert artifact_trace_payload["summary"]["source_count"] == 1
    assert doctor_response.status_code == 200
    assert doctor_payload["blocking_failure_count"] == 0
    assert any(check["name"] == "migrations" for check in doctor_payload["checks"])


def test_vnext_agent_policy_telemetry_scopes_supporting_rows_to_requested_agent(monkeypatch) -> None:
    store = FakeVNextStore(None)
    store.events.extend(
        [
            {
                "id": "event-hermes",
                "event_type": "agent.memory_proposed",
                "actor_type": "agent",
                "actor_id": "hermes",
                "target_id": "memory-hermes",
                "payload_json": {},
            },
            {
                "id": "event-other",
                "event_type": "agent.memory_proposed",
                "actor_type": "agent",
                "actor_id": "other",
                "target_id": "memory-other",
                "payload_json": {},
            },
        ]
    )
    store.memories.extend(
        [
            {"id": "memory-hermes", "metadata_json": {"agent_id": "hermes"}},
            {"id": "memory-other", "metadata_json": {"agent_id": "other"}},
        ]
    )
    store.artifacts.update(
        {
            "artifact-hermes": {
                "id": "artifact-hermes",
                "metadata_json": {"generated_by": "agent", "agent_id": "hermes"},
            },
            "artifact-other": {
                "id": "artifact-other",
                "metadata_json": {"generated_by": "agent", "agent_id": "other"},
            },
        }
    )
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_projects_router.get_vnext_agent_policy_telemetry(
        user_id=uuid4(),
        agent_id="hermes",
        limit=200,
    )

    assert response.status_code == 200
    summary = json.loads(response.body)["summary"]
    assert summary["total_agent_events"] == 1
    assert summary["memory_proposals_by_agent"] == [{"agent_id": "hermes", "count": 1}]
    assert summary["artifact_generation_by_agent"] == [{"agent_id": "hermes", "count": 1}]


def test_vnext_source_trace_caps_every_collection_and_reports_truncation(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {},
    }
    for index in range(vnext_shared._VNEXT_SOURCE_TRACE_COLLECTION_LIMIT + 1):
        store.chunks.append({"id": f"chunk-{index}", "source_id": source_id, "chunk_index": index})
        store.memories.append(
            {
                "id": f"memory-{index}",
                "source_id": source_id,
                "metadata_json": {},
            }
        )
        store.artifacts[f"artifact-{index}"] = {
            "id": f"artifact-{index}",
            "source_id": source_id,
            "metadata_json": {},
        }
        store.open_loops.append(
            {
                "id": f"loop-{index}",
                "source_id": source_id,
                "metadata_json": {},
            }
        )
        store.events.append(
            {
                "id": f"event-{index}",
                "target_type": "source",
                "target_id": source_id,
                "payload_json": {},
            }
        )
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_retrieval_router.get_vnext_source_trace(main_module.UUID(source_id), user_id=uuid4())

    payload = json.loads(response.body)
    assert response.status_code == 200
    for key in ("chunks", "candidate_memories", "artifacts", "open_loops", "events"):
        assert len(payload[key]) == vnext_shared._VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
    assert payload["sampling"]["trace_complete"] is False
    assert payload["sampling"]["memory_history_complete"] is False
    assert set(payload["sampling"]["truncated_collections"]) == {
        "chunks",
        "candidate_memories",
        "artifacts",
        "open_loops",
        "events",
    }


def test_vnext_agent_policy_telemetry_clamps_direct_call_limit(monkeypatch) -> None:
    store = FakeVNextStore(None)
    observed_limits: list[int] = []

    def capture_limit(*, agent_id: str | None = None, limit: int = 200):
        del agent_id
        observed_limits.append(limit)
        return []

    store.list_agent_events = capture_limit  # type: ignore[method-assign]
    store.list_agent_policy_artifacts = capture_limit  # type: ignore[method-assign]
    store.list_agent_policy_memories = capture_limit  # type: ignore[method-assign]
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_projects_router.get_vnext_agent_policy_telemetry(
        user_id=uuid4(),
        limit=10_000,
    )

    assert response.status_code == 200
    assert observed_limits == [200, 200, 200]


def test_vnext_artifact_trace_loads_exact_referenced_source_and_events_before_limit(monkeypatch) -> None:
    store = FakeVNextStore(None)
    for index in range(101):
        decoy_id = str(uuid4())
        store.sources[decoy_id] = {
            "id": decoy_id,
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {},
        }
        store.events.append(
            {
                "id": f"decoy-event-{index}",
                "event_type": "source.updated",
                "target_type": "source",
                "target_id": decoy_id,
                "payload_json": {},
            }
        )
    source_id = str(uuid4())
    artifact_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": "Exact provenance target"},
    }
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "daily_brief",
        "title": "Exact trace",
        "content_markdown": "# Exact trace",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"source_refs": [f"source:{source_id}"]},
    }
    store.events.append(
        {
            "id": "artifact-event",
            "event_type": "artifact.generated",
            "target_type": "artifact",
            "target_id": artifact_id,
            "payload_json": {},
        }
    )
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_retrieval_router.get_vnext_artifact_trace(
        main_module.UUID(artifact_id),
        user_id=uuid4(),
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert [source["id"] for source in payload["sources"]] == [source_id]
    assert [event["id"] for event in payload["events"]] == ["artifact-event"]


def test_vnext_artifact_trace_authorizes_sources_from_complete_persisted_scope_envelope(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    empty_source_id = str(uuid4())
    real_source_id = str(uuid4())
    artifact_id = str(uuid4())
    for source_id, canonical_scope in ((empty_source_id, []), (real_source_id, ["real"])):
        store.sources[source_id] = {
            "id": source_id,
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_id": "stale",
                "raw_text": source_id,
                "metadata_json": {"project_scope": canonical_scope},
            },
        }
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "manual_note",
        "title": "Scoped trace",
        "content_markdown": "# Scoped trace",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {
            "project_scope": ["real"],
            "source_refs": [f"source:{empty_source_id}", f"source:{real_source_id}"],
        },
    }
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="real-reader",
        permission_profile="project_scoped_agent",
        project_scope="real",
    )

    response = vnext_retrieval_router.get_vnext_artifact_trace(
        main_module.UUID(artifact_id),
        user_id=user_id,
        authorization=f"Bearer {raw_key}",
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert [source["id"] for source in payload["sources"]] == [real_source_id]


def test_create_vnext_context_pack_endpoint_returns_structured_pack(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "source_type": "manual_text",
        "title": "Alice context source",
        "content_hash": "sha256:abc",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
    }
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "semantic",
            "canonical_text": "Alice context packs include sources.",
            "status": "active",
            "confidence": 0.9,
            "domain": "project",
            "sensitivity": "private",
            "first_seen_at": "2026-05-10T00:00:00Z",
            "last_seen_at": "2026-05-10T00:00:00Z",
        }
    )
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_retrieval_router.create_vnext_context_pack(
        vnext_retrieval_router.VNextContextPackRequest(
            user_id=uuid4(),
            query="Alice context sources",
            scope={"domains": ["project"]},
            options={"sensitivity_allowed": ["public", "private"], "max_items": 4},
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert payload["relevant_memories"][0]["id"] == "memory-1"
    assert payload["sources"][0]["id"] == source_id
    assert payload["trace_id"] == payload["trace"]["trace_id"]
    assert store.events[-1]["event_type"] == "retrieval.context_pack_compiled"


def test_create_vnext_context_pack_endpoint_keeps_uncorroborated_count_trace_only(monkeypatch) -> None:
    store = FakeVNextStore(None)
    for index, bike in enumerate(("commuter", "touring"), start=1):
        store.memories.append(
            {
                "id": f"memory-bike-{index}",
                "memory_type": "semantic",
                "canonical_text": f"I serviced the {bike} bike in March.",
                "status": "active",
                "confidence": 0.9,
                "domain": "personal",
                "sensitivity": "private",
                "metadata_json": {
                    "source_id": f"source-bike-{index}",
                    "source_chunk_id": f"chunk-bike-{index}",
                },
            }
        )
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_retrieval_router.create_vnext_context_pack(
        vnext_retrieval_router.VNextContextPackRequest(
            user_id=uuid4(),
            query="How many bikes did I service?",
            scope={"domains": ["personal"]},
            options={"sensitivity_allowed": ["private"], "max_items": 4},
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert "aggregation" not in payload
    trace_count = payload["trace"]["stages"]["coverage_mode"]["candidate_instance_count"]
    assert trace_count["count"] == 2
    assert trace_count["is_answer"] is False
    assert trace_count["supports_numeric_sum"] is False
    response_contract = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[
        ("POST", "/v0/vnext/context-packs")
    ][1]
    assert "aggregation" not in response_contract["required"]
    assert "aggregation" not in response_contract["properties"]


def test_vnext_brain_artifact_generation_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "source_type": "manual_text",
        "title": "Alice daily API note",
        "content_hash": "sha256:abc",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": "TODO: validate daily API endpoint"},
    }
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "project_state",
            "canonical_text": "Alice vNext API generates brain artifacts.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    request = vnext_review_router.VNextBrainArtifactGenerateRequest(
        user_id=user_id,
        scope={"domains": ["project"]},
        options={"generated_for": "2026-05-10", "sensitivity_allowed": ["public", "private"]},
    )

    daily_response = vnext_review_router.generate_vnext_daily_brief(request)
    weekly_response = vnext_review_router.generate_vnext_weekly_synthesis(request)

    daily_payload = json.loads(daily_response.body)
    weekly_payload = json.loads(weekly_response.body)
    daily_contract = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[
        ("POST", "/v0/vnext/artifacts/generate/daily-brief")
    ][1]
    weekly_contract = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis")
    ][1]
    assert daily_response.status_code == 201
    assert set(daily_payload) == set(daily_contract["properties"]) - {
        "created_at",
        "promoted_at",
        "reviewed_at",
        "user_id",
    }
    assert daily_payload["artifact_type"] == "daily_brief"
    assert daily_payload["metadata_json"]["candidate_open_loop_ids"] == ["loop-1"]
    assert weekly_response.status_code == 201
    assert set(weekly_payload) == set(weekly_contract["properties"]) - {
        "created_at",
        "promoted_at",
        "reviewed_at",
        "user_id",
    }
    assert weekly_payload["artifact_type"] == "weekly_synthesis"
    assert weekly_payload["metadata_json"]["candidate_memory_ids"] == ["memory-2"]
    assert store.events[-1]["event_type"] == "artifact.generated"


def test_vnext_connection_and_graph_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "source_type": "manual_text",
        "title": "Queue retrieval pattern note",
        "content_hash": "sha256:abc",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": "Queue retrieval provenance trace review."},
    }
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "semantic",
            "canonical_text": "Retrieval provenance trace review improves queue artifacts.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    generate_response = vnext_review_router.generate_vnext_connection_report(
        vnext_review_router.VNextConnectionReportGenerateRequest(
            user_id=user_id,
            scope={"domains": ["project"]},
            options={"max_connections": 1},
        )
    )
    review_response = vnext_review_router.review_vnext_graph_edge(
        "edge-1",
        vnext_review_router.VNextGraphEdgeReviewRequest(user_id=user_id, action="accept"),
    )
    neighborhood_response = vnext_review_router.get_vnext_graph_neighborhood(source_id, user_id=user_id)

    generate_payload = json.loads(generate_response.body)
    review_payload = json.loads(review_response.body)
    neighborhood_payload = json.loads(neighborhood_response.body)
    assert generate_response.status_code == 201
    assert set(generate_payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/vnext/artifacts/generate/connections")][1][
            "properties"
        ]
    ) - {"created_at", "promoted_at", "reviewed_at", "user_id"}
    assert generate_payload["artifact_type"] == "connection_report"
    assert generate_payload["metadata_json"]["candidate_edge_ids"] == ["edge-1"]
    assert review_response.status_code == 200
    assert review_payload["metadata_json"]["status"] == "accepted"
    assert neighborhood_response.status_code == 200
    assert neighborhood_payload["edge_count"] == 1
    assert neighborhood_payload["from_edges"][0]["id"] == "edge-1"


def test_vnext_contradiction_and_belief_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    source_id = str(uuid4())
    store.sources[source_id] = {
        "id": source_id,
        "source_type": "manual_text",
        "title": "Artifact policy note",
        "content_hash": "sha256:abc",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"raw_text": "Alice should not auto-promote generated artifacts into memory."},
    }
    store.beliefs["belief-1"] = {
        "id": "belief-1",
        "memory_id": "memory-belief-1",
        "claim": "Alice should auto-promote generated artifacts into memory.",
        "status": "active",
        "confidence": 0.8,
        "domain": "project",
        "sensitivity": "private",
        "memory_type": "belief",
    }
    store.memories.append({"id": "memory-belief-1", "domain": "project", "sensitivity": "private", "canonical_text": "Alice should auto-promote generated artifacts into memory.", "status": "active", "metadata_json": {}})
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    generate_response = vnext_review_router.generate_vnext_contradiction_report(
        vnext_review_router.VNextContradictionReportGenerateRequest(
            user_id=user_id,
            scope={"domains": ["project"]},
            options={"max_contradictions": 1},
        )
    )
    review_response = vnext_review_router.review_vnext_belief(
        "belief-1",
        vnext_review_router.VNextBeliefReviewRequest(user_id=user_id, action="challenge", confidence=0.25),
    )
    state_response = vnext_review_router.get_vnext_belief_state("belief-1", user_id=user_id)

    generate_payload = json.loads(generate_response.body)
    review_payload = json.loads(review_response.body)
    state_payload = json.loads(state_response.body)
    assert generate_response.status_code == 201
    assert set(generate_payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/vnext/artifacts/generate/contradictions")][1][
            "properties"
        ]
    ) - {"created_at", "promoted_at", "reviewed_at", "user_id"}
    assert generate_payload["artifact_type"] == "contradiction_report"
    assert generate_payload["metadata_json"]["candidate_edge_ids"] == ["edge-1"]
    assert review_response.status_code == 200
    assert review_payload["status"] == "challenged"
    assert review_payload["confidence"] == 0.25
    assert state_response.status_code == 200
    assert state_payload["current"]["status"] == "challenged"
    assert "challenged" in state_payload["previous_statuses"]


def test_vnext_project_and_open_loop_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources[str(uuid4())] = {
        "id": "source-1",
        "source_type": "manual_text",
        "title": "Alice project note",
        "content_hash": "sha256:abc",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {
            "project_scope": ["project-1"],
            "raw_text": "Project: Alice vNext needs project automation.\nTODO: validate dashboard Owner: Jordan",
        },
    }
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    request = vnext_automation.VNextProjectAutomationRequest(
        user_id=user_id,
        scope={"domains": ["project"], "project_id": "project-1"},
        options={"sensitivity_allowed": ["public", "private"]},
    )

    update_response = vnext_review_router.generate_vnext_project_update_candidate(request)
    update_payload = json.loads(update_response.body)
    extract_response = vnext_projects_router.extract_vnext_open_loops(request)
    review_update_response = vnext_review_router.review_vnext_project_update_candidate(
        update_payload["id"],
        vnext_review_router.VNextProjectUpdateReviewRequest(
            user_id=user_id,
            action="edit",
            edited_current_state="Project automation reviewed.",
        ),
    )
    review_loop_response = vnext_projects_router.review_vnext_open_loop(
        "loop-1",
        vnext_projects_router.VNextOpenLoopReviewRequest(
            user_id=user_id,
            action="snooze",
            due_at="2026-05-12T09:00:00Z",
        ),
    )
    dashboard_response = vnext_projects_router.get_vnext_project_dashboard("project-1", user_id=user_id)

    extract_payload = json.loads(extract_response.body)
    review_update_payload = json.loads(review_update_response.body)
    review_loop_payload = json.loads(review_loop_response.body)
    dashboard_payload = json.loads(dashboard_response.body)
    assert update_response.status_code == 201
    assert set(update_payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/vnext/projects/update-candidates")][1][
            "properties"
        ]
    ) - {"created_at", "promoted_at", "reviewed_at", "user_id"}
    assert update_payload["artifact_type"] == "project_update"
    assert update_payload["metadata_json"]["candidate_memory_id"] == "memory-1"
    assert extract_response.status_code == 201
    assert extract_payload["created_count"] == 1
    assert extract_payload["open_loops"][0]["metadata_json"]["owner"] == "Jordan"
    assert review_update_response.status_code == 200
    assert set(review_update_payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[
            ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review")
        ][1]["properties"]
    ) - {"created_at", "promoted_at", "reviewed_at", "user_id"}
    assert review_update_payload["status"] == "accepted"
    assert store.projects["project-1"]["current_state"] == "Project automation reviewed."
    assert review_loop_response.status_code == 200
    assert review_loop_payload["due_at"] == "2026-05-12T09:00:00Z"
    assert dashboard_response.status_code == 200
    assert dashboard_payload["counts"]["open_loops"] == 1


def test_project_automation_uses_canonical_project_scope() -> None:
    converted = vnext_automation._vnext_project_automation_request(
        vnext_automation.VNextProjectAutomationRequest(
            user_id=uuid4(),
            scope={"domains": ["project"]},
            project_scope=["project-canonical"],
        )
    )

    assert converted.project_id == "project-canonical"


def test_project_automation_rejects_ambiguous_canonical_project_scope() -> None:
    with pytest.raises(ValueError, match="requires one project_id"):
        vnext_automation._vnext_project_automation_request(
            vnext_automation.VNextProjectAutomationRequest(
                user_id=uuid4(),
                scope={"domains": ["project"]},
                project_scope=["project-a", "project-b"],
            )
        )


def test_project_automation_endpoints_map_ambiguous_and_mismatched_scope_to_400(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    ambiguous = vnext_review_router.generate_vnext_project_update_candidate(
        vnext_automation.VNextProjectAutomationRequest(
            user_id=user_id,
            scope={"domains": ["project"]},
            project_scope=["project-a", "project-b"],
        )
    )
    mismatched = vnext_projects_router.extract_vnext_open_loops(
        vnext_automation.VNextProjectAutomationRequest(
            user_id=user_id,
            scope={"domains": ["project"]},
            project_scope=["project-a"],
            options={"project_id": "project-b"},
        )
    )

    assert ambiguous.status_code == 400
    assert json.loads(ambiguous.body) == {"detail": "vNext project update request is invalid"}
    assert mismatched.status_code == 400
    assert json.loads(mismatched.body) == {"detail": "vNext open-loop extraction request is invalid"}


def test_vnext_queue_and_artifact_endpoints(monkeypatch, tmp_path) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    create_response = vnext_review_router.create_vnext_queue_task(
        vnext_review_router.VNextQueueTaskCreateRequest(
            user_id=user_id,
            title="Draft launch note",
            task_type="draft",
            instructions="Write from approved sources.",
            domain="project",
            sensitivity="private",
            scope_json={"project": "alice"},
            allowed_sources_json=["source-1"],
        )
    )

    create_payload = json.loads(create_response.body)
    assert create_response.status_code == 201
    assert create_payload["status"] == "pending"
    assert create_payload["requested_by"] == "api"
    assert store.events[-1]["event_type"] == "queue.task_enqueued"

    process_response = vnext_review_router.process_next_vnext_queue_task(
        vnext_review_router.VNextQueueProcessNextRequest(user_id=user_id)
    )

    process_payload = json.loads(process_response.body)
    artifact_id = process_payload["artifact_id"]
    assert process_response.status_code == 200
    assert process_payload["status"] == "completed"
    assert store.tasks[0]["status"] == "completed"
    assert store.tasks[0]["output_artifact_id"] == artifact_id
    assert store.artifacts[artifact_id]["content_markdown"].startswith("# Draft launch note")

    get_response = vnext_review_router.get_vnext_artifact(main_module.UUID(artifact_id), user_id=user_id)
    assert get_response.status_code == 200
    assert json.loads(get_response.body)["id"] == artifact_id

    review_response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action="accept"),
    )
    assert review_response.status_code == 200
    assert json.loads(review_response.body)["status"] == "accepted"

    quality_response = vnext_review_router.rate_vnext_artifact_quality(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactQualityRatingRequest(
            user_id=user_id,
            reviewer_id=str(user_id),
            usefulness=4,
            accuracy=5,
            source_grounding=5,
            novel_connections=3,
            actionability=4,
            hallucination_risk=1,
            verbosity="right_sized",
            comments="Useful and grounded.",
        ),
    )
    quality_payload = json.loads(quality_response.body)
    export_quality_response = vnext_review_router.list_vnext_quality_evals(
        user_id=user_id,
        artifact_id=main_module.UUID(artifact_id),
        limit=10,
    )
    export_quality_payload = json.loads(export_quality_response.body)

    assert quality_response.status_code == 201
    assert quality_payload["artifact_id"] == artifact_id
    assert quality_payload["usefulness"] == 4
    assert export_quality_response.status_code == 200
    assert export_quality_payload["count"] == 1
    assert export_quality_payload["items"][0]["artifact_id"] == artifact_id

    export_response = vnext_review_router.export_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactExportRequest(user_id=user_id, output_dir=str(tmp_path)),
    )
    export_payload = json.loads(export_response.body)
    output_path = Path(export_payload["output_path"])
    assert export_response.status_code == 200
    assert output_path.name.startswith("artifact-")
    assert output_path.suffix == ".md"
    assert output_path.read_text(encoding="utf-8").startswith("# Draft launch note")


def test_vnext_artifact_review_endpoint_maps_validation_errors(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact_id = str(uuid4())
    store.artifacts[artifact_id] = {"id": artifact_id, "title": "Draft", "content_markdown": "# Draft"}

    invalid_response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action="ship"),
    )
    missing_response = vnext_review_router.review_vnext_artifact(
        uuid4(),
        vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action="accept"),
    )

    assert invalid_response.status_code == 400
    assert missing_response.status_code == 404


def test_generic_artifact_review_dispatches_with_authenticated_reviewer_attribution(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact_id = str(uuid4())
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "daily_brief",
        "title": "Review me",
        "content_markdown": "# Review me",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {},
    }

    response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(
            user_id=user_id,
            action="accept",
            agent=main_module.VNextAgentIdentityRequest(
                agent_id="artifact-reviewer",
                agent_type="coding_agent",
                agent_run_id="artifact-review-run-1",
                permission_profile="admin_agent",
            ),
            trace_id="artifact-review-trace-1",
        ),
    )

    assert response.status_code == 200
    review_event = next(event for event in store.events if event.get("event_type") == "artifact.reviewed")
    assert review_event["actor_type"] == "agent"
    assert review_event["actor_id"] == "artifact-reviewer"
    assert review_event["trace_id"] == "artifact-review-trace-1"
    assert review_event["run_id"] == "artifact-review-run-1"


def test_generic_artifact_review_preserves_human_reviewer_attribution(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact_id = str(uuid4())
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "daily_brief",
        "title": "Human review",
        "content_markdown": "# Human review",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {},
    }

    response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(
            user_id=user_id,
            action="accept",
            trace_id="human-review-trace-1",
        ),
    )

    assert response.status_code == 200
    review_event = next(event for event in store.events if event.get("event_type") == "artifact.reviewed")
    assert review_event["actor_type"] == "user"
    assert review_event["actor_id"] == str(user_id)
    assert review_event["trace_id"] == "human-review-trace-1"
    assert review_event["run_id"] is None


def test_generic_artifact_review_preserves_applied_project_update_state(monkeypatch) -> None:
    store = FakeVNextStore(None)
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources["source-1"] = {
        "id": "source-1",
        "source_type": "manual_text",
        "title": "Alice project note",
        "content_hash": "sha256:project-review-guard",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {
            "project_scope": ["project-1"],
            "raw_text": "Alice vNext is ready for the public release candidate.",
        },
    }
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact = VNextProjectService(store).generate_project_update_candidate(
        vnext_automation.ProjectAutomationRequest(project_id="project-1", domains=("project",))
    )
    artifact_id = str(artifact["id"])
    candidate_memory_id = str(artifact["metadata_json"]["candidate_memory_id"])
    expected_state = str(artifact["metadata_json"]["suggested_current_state"])

    accepted_response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action="accept"),
    )
    rejected_response = vnext_review_router.review_vnext_artifact(
        main_module.UUID(artifact_id),
        vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action="reject"),
    )

    assert accepted_response.status_code == 200
    assert json.loads(accepted_response.body)["status"] == "accepted"
    assert rejected_response.status_code == 400
    assert store.artifacts[artifact_id]["status"] == "accepted"
    assert store.projects["project-1"]["current_state"] == expected_state
    assert store.get_memory(candidate_memory_id)["status"] == "active"
    assert not any(event.get("event_type") == "project.update_candidate_rejected" for event in store.events)


def _http_project_update_review_fixture() -> tuple[FakeVNextStore, dict[str, object]]:
    store = FakeVNextStore(None)
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources["source-1"] = {
        "id": "source-1",
        "source_type": "manual_text",
        "title": "Alice project note",
        "content_hash": "sha256:terminal-consistency-http",
        "captured_at": "2026-05-10T00:00:00Z",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {
            "project_scope": ["project-1"],
            "raw_text": "Alice vNext is ready for terminal consistency review.",
        },
    }
    artifact = VNextProjectService(store).generate_project_update_candidate(
        vnext_automation.ProjectAutomationRequest(project_id="project-1", domains=("project",))
    )
    return store, artifact


def _review_project_update_over_http(
    *,
    adapter: str,
    artifact_id: str,
    user_id: main_module.UUID,
    action: str,
) -> main_module.JSONResponse:
    if adapter == "generic":
        return vnext_review_router.review_vnext_artifact(
            main_module.UUID(artifact_id),
            vnext_review_router.VNextArtifactReviewRequest(user_id=user_id, action=action),
        )
    return vnext_review_router.review_vnext_project_update_candidate(
        artifact_id,
        vnext_review_router.VNextProjectUpdateReviewRequest(user_id=user_id, action=action),
    )


def _apply_supported_http_memory_lifecycle(
    store: FakeVNextStore,
    *,
    artifact: dict[str, object],
    operation: str,
) -> None:
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    memory_id = str(metadata["candidate_memory_id"])
    service = VNextMemoryCommitService(store)
    if operation == "correct":
        service.correct(
            identity=None,
            memory_id=memory_id,
            canonical_text="Later corrected HTTP project-update memory.",
            reason="Exercise a supported post-review correction.",
        )
    elif operation == "undo":
        service.undo(
            identity=None,
            memory_id=memory_id,
            reason="Exercise a supported post-review undo.",
        )
    else:
        service.forget(
            identity=None,
            memory_id=memory_id,
            reason="Exercise a supported post-review forget.",
        )


def _accept_later_http_project_update(store: FakeVNextStore, *, first_artifact_id: str) -> None:
    service = VNextProjectService(store)
    later = service.generate_project_update_candidate(
        vnext_automation.ProjectAutomationRequest(project_id="project-1", domains=("project",))
    )
    assert later["id"] != first_artifact_id
    service.review_project_update(
        artifact_id=str(later["id"]),
        action="edit",
        edited_current_state="Later accepted HTTP project state B.",
    )


def _append_conflicting_http_project_update_decision(
    store: FakeVNextStore,
    *,
    artifact: dict[str, object],
    conflict: str,
) -> None:
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    artifact_id = str(artifact["id"])
    candidate_memory_id = str(metadata["candidate_memory_id"])
    project_id = str(metadata["project_id"])
    review_event = next(
        event for event in store.events if event.get("event_type") == f"project.update_candidate_{artifact['status']}"
    )
    event_type: str
    target_type: str
    target_id: str
    payload: dict[str, object]
    if conflict == "accepted_plus_rejected":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {"project_id": project_id, "source_ids": list(metadata["source_ids"])}
    elif conflict == "candidate_linked_accepted_wrong_action":
        event_type = "project.update_candidate_accepted"
        target_type = "project"
        target_id = project_id
        payload = {"candidate_memory_id": candidate_memory_id, "action": "reject"}
    elif conflict == "rejected_plus_conflicting_rejection":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {"project_id": project_id, "source_ids": ["conflicting-source"]}
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(conflict)
    store.append_event(
        build_event_log_record(
            event_type=event_type,
            actor_type=str(review_event["actor_type"]),
            actor_id=str(review_event["actor_id"]) if review_event.get("actor_id") is not None else None,
            target_type=target_type,
            target_id=target_id,
            trace_id=str(review_event["trace_id"]) if review_event.get("trace_id") is not None else None,
            run_id=str(review_event["run_id"]) if review_event.get("run_id") is not None else None,
            payload=payload,
        )
    )


def _redact_and_clone_http_project_update_terminal(
    store: FakeVNextStore,
    *,
    terminal: dict[str, object],
) -> str:
    metadata = terminal["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])
    for revision in store.revisions:
        if (
            str(revision.get("memory_id") or "") == candidate_memory_id
            and revision.get("action") == "project_update_review"
        ):
            revision.update(
                {
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
        ):
            continue
        event["payload_json"] = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": event["event_type"],
        }
        event["integrity_hash"] = None
    clone_id = str(uuid4())
    clone = deepcopy(terminal)
    clone["id"] = clone_id
    store.artifacts[clone_id] = clone
    return clone_id


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize(
    ("forced_status", "retry_action"),
    [("accepted", "accept"), ("rejected", "reject")],
)
def test_http_project_update_review_rejects_forced_terminal_status_without_mutation(
    monkeypatch,
    adapter: str,
    forced_status: str,
    retry_action: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    artifact["status"] = forced_status
    artifact_id = str(artifact["id"])
    state_before = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    response = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=uuid4(),
        action=retry_action,
    )

    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": vnext_review_router.PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE}
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
def test_http_project_update_review_rejects_terminal_clone_after_true_redaction_without_mutation(
    monkeypatch,
    adapter: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    original_id = str(artifact["id"])
    accepted = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=original_id,
        user_id=user_id,
        action="accept",
    )
    assert accepted.status_code == 200
    clone_id = _redact_and_clone_http_project_update_terminal(store, terminal=artifact)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    response = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=clone_id,
        user_id=user_id,
        action="accept",
    )

    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": vnext_review_router.PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE}
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize("action", ["accept", "reject"])
def test_http_project_update_review_keeps_consistent_terminal_outcomes_idempotent(
    monkeypatch,
    adapter: str,
    action: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    artifact_id = str(artifact["id"])
    user_id = uuid4()
    first = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action=action,
    )
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action=action,
    )

    assert first.status_code == second.status_code == 200
    assert json.loads(first.body) == json.loads(second.body)
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize(
    ("action", "conflict"),
    [
        ("accept", "accepted_plus_rejected"),
        ("accept", "candidate_linked_accepted_wrong_action"),
        ("reject", "rejected_plus_conflicting_rejection"),
    ],
)
def test_http_project_update_terminal_replay_rejects_every_coupled_competing_decision(
    monkeypatch,
    adapter: str,
    action: str,
    conflict: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    artifact_id = str(artifact["id"])
    user_id = uuid4()
    first = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action=action,
    )
    assert first.status_code == 200
    _append_conflicting_http_project_update_decision(store, artifact=artifact, conflict=conflict)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action=action,
    )

    assert second.status_code == 409
    assert json.loads(second.body) == {"detail": vnext_review_router.PROJECT_UPDATE_TERMINAL_CONSISTENCY_MESSAGE}
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize("operation", ["correct", "undo", "forget"])
def test_http_accepted_project_update_replay_survives_supported_memory_lifecycle(
    monkeypatch,
    adapter: str,
    operation: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    artifact_id = str(artifact["id"])
    user_id = uuid4()
    first = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action="accept",
    )
    _apply_supported_http_memory_lifecycle(store, artifact=artifact, operation=operation)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action="accept",
    )

    assert first.status_code == second.status_code == 200
    assert json.loads(first.body) == json.loads(second.body)
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
def test_http_accepted_project_update_replay_preserves_a_genuine_later_project_update(
    monkeypatch,
    adapter: str,
) -> None:
    store, artifact = _http_project_update_review_fixture()
    _install_fake_vnext_store(monkeypatch, store)
    artifact_id = str(artifact["id"])
    user_id = uuid4()
    first = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action="accept",
    )
    _accept_later_http_project_update(store, first_artifact_id=artifact_id)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second = _review_project_update_over_http(
        adapter=adapter,
        artifact_id=artifact_id,
        user_id=user_id,
        action="accept",
    )

    assert first.status_code == second.status_code == 200
    assert json.loads(first.body) == json.loads(second.body)
    assert store.projects["project-1"]["current_state"] == "Later accepted HTTP project state B."
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


def test_live_capture_connector_api_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    config_response = vnext_memories_router.update_vnext_connector_config(
        "telegram",
        vnext_memories_router.VNextConnectorConfigRequest(
            user_id=user_id,
            enabled=True,
            sync_mode="on_demand",
            config_json={"allowed_chat_ids": ["999001"]},
        ),
    )
    telegram_response = vnext_memories_router.sync_vnext_telegram_connector(
        vnext_memories_router.VNextTelegramSyncRequest(
            user_id=user_id,
            allowed_chat_ids=["999001"],
            updates=[
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "date": 1_778_400_000,
                        "chat": {"id": 999001},
                        "from": {"id": 1001, "username": "jordan"},
                        "text": "Fact: API Telegram capture works.",
                    },
                }
            ],
        )
    )
    browser_response = vnext_memories_router.capture_vnext_browser_clip(
        vnext_memories_router.VNextBrowserClipperCaptureRequest(
            user_id=user_id,
            url="https://example.test/clip",
            title="Clip",
            selected_text="Fact: Browser API clip works.",
            user_note="Remember: keep this reviewable.",
        )
    )
    health_response = vnext_memories_router.get_vnext_connectors_health(user_id=user_id)

    assert config_response.status_code == 200
    assert telegram_response.status_code == 201
    assert browser_response.status_code == 201
    assert json.loads(telegram_response.body)["imported_count"] == 1
    assert json.loads(browser_response.body)["imported_count"] == 1
    health_payload = json.loads(health_response.body)
    assert health_payload["count"] >= 4
    assert any(item["connector_name"] == "telegram" for item in health_payload["items"])


def test_browser_clip_capability_is_one_time_origin_bound_and_bypasses_only_capture_auth(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _key_record, raw_agent_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="browser-clip-issuer",
        permission_profile="trusted_local_agent",
    )
    issue_payload = {"user_id": str(user_id), "origin": "https://example.test"}

    assert (
        _invoke_vnext_request(
            "POST",
            "/v0/vnext/connectors/browser-clipper/capabilities",
            payload=issue_payload,
        )[0]
        == 401
    )
    issued_status, issued_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/connectors/browser-clipper/capabilities",
        payload=issue_payload,
        authorization=f"Bearer {raw_agent_key}",
    )
    assert issued_status == 201
    capability = str(issued_payload["capability"])
    assert capability.startswith("alice_clip_")
    assert capability not in repr(store.browser_clip_capabilities)

    clip_payload = {
        "user_id": str(user_id),
        "url": "https://example.test/article",
        "selected_text": "Fact: one-time browser capabilities are narrow.",
        "capture_capability": capability,
    }
    captured_status, captured_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/connectors/browser-clipper/capture",
        payload=clip_payload,
        content_type="text/plain;charset=UTF-8",
        origin="https://example.test",
    )
    replay_status, _replay_payload = _invoke_vnext_request(
        "POST",
        "/v0/vnext/connectors/browser-clipper/capture",
        payload=clip_payload,
        content_type="text/plain;charset=UTF-8",
        origin="https://example.test",
    )

    assert captured_status == 201, captured_payload
    assert captured_payload["imported_count"] == 1
    assert replay_status == 400
    assert len(store.sources) == 1
    assert capability not in json.dumps(store.sources, default=str)
    assert capability not in json.dumps(store.events, default=str)


def test_browser_clip_capability_response_is_not_cacheable(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)

    response = vnext_memories_router.create_vnext_browser_clip_capability(
        vnext_memories_router.VNextBrowserClipperCapabilityRequest(
            user_id=uuid4(),
            origin="https://example.test",
        )
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_local_folder_external_io_runs_without_database_connection(monkeypatch, tmp_path) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    connection_depth = 0
    scan_depths: list[int] = []

    @contextmanager
    def tracked_user_connection(database_url, current_user_id):
        nonlocal connection_depth
        assert database_url == "postgresql://db"
        assert current_user_id == user_id
        connection_depth += 1
        try:
            yield object()
        finally:
            connection_depth -= 1

    original_scan_local_folder = vnext_memories_router.scan_local_folder

    def tracked_scan_local_folder(*args, **kwargs):
        scan_depths.append(connection_depth)
        return original_scan_local_folder(*args, **kwargs)

    monkeypatch.setattr(vnext_memories_router, "user_connection", tracked_user_connection)
    monkeypatch.setattr(vnext_memories_router, "scan_local_folder", tracked_scan_local_folder)
    watched_file = tmp_path / "release-note.md"
    watched_file.write_text("Fact: local folder scans release the database connection.", encoding="utf-8")

    local_response = vnext_memories_router.sync_vnext_local_folder_connector(
        vnext_memories_router.VNextLocalFolderSyncRequest(
            user_id=user_id,
            paths=[str(tmp_path)],
        )
    )

    assert local_response.status_code == 201
    assert scan_depths == [0]
    assert connection_depth == 0


def test_vnext_agent_endpoint_with_bearer_key_uses_key_identity(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: keyed agents authenticate with per-agent API keys.",
            domain="project",
            sensitivity="private",
            agent_id="openclaw",
            agent_run_id="run-keyed-1",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert response.status_code == 201
    recorded_identity = store.agent_identities["openclaw"]
    assert recorded_identity["permission_profile"] == "project_scoped_agent"
    policy_events = [event for event in store.events if event.get("event_type") == "policy.decision"]
    assert policy_events
    identity_record = policy_events[0]["payload_json"]["agent_identity"]
    assert identity_record["auth"] == "agent_api_key"
    assert identity_record["permission_profile"] == "project_scoped_agent"
    assert store.agent_api_keys[0]["last_used_at"] == "now"


def test_vnext_agent_endpoint_rejects_keyless_agent_call_when_keys_exist(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    create_agent_key(store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent")

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: keyless agent calls are rejected once keys exist.",
            domain="project",
            sensitivity="private",
            agent_id="openclaw",
        )
    )

    assert response.status_code == 401
    detail = json.loads(response.body)["detail"]
    assert detail == {"code": "authentication_failed", "message": "Authentication failed"}
    assert store.sources == {}


def test_vnext_memory_commit_rejects_keyless_agent_call_when_keys_exist(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    create_agent_key(store, user_id=user_id, agent_id="hermes", permission_profile="trusted_local_agent")

    response = vnext_memories_router.commit_vnext_memory(
        vnext_memories_router.VNextMemoryCommitRequest(
            user_id=user_id,
            title="Keyless agent commit",
            canonical_text="Keyless agent commits stay rejected once keys exist.",
            agent_id="hermes",
        )
    )

    assert response.status_code == 401
    assert json.loads(response.body)["detail"] == {
        "code": "authentication_failed",
        "message": "Authentication failed",
    }
    assert store.memories == []


def test_vnext_memory_commit_without_identity_commits_as_direct_user(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    user_id = uuid4()
    response = vnext_memories_router.commit_vnext_memory(
        vnext_memories_router.VNextMemoryCommitRequest(
            user_id=user_id,
            title="Direct user commit",
            canonical_text="Direct human commits need no agent identity.",
            confidence=0.95,
        )
    )

    assert response.status_code == 201
    payload = json.loads(response.body)
    assert payload["status"] == "committed"
    assert payload["write_mode"] == "commit"
    assert payload["policy_decision"]["policy_decision"]["permission_profile"] == "user_or_system"
    assert store.memories[0]["created_by_agent_id"] is None


def test_vnext_agent_endpoint_rejects_payload_profile_escalation(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: escalation attempts are rejected.",
            domain="project",
            sensitivity="private",
            agent_id="openclaw",
            permission_profile="admin_agent",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert response.status_code == 403
    assert store.sources == {}
    assert any(event.get("event_type") == "agent.key_escalation_rejected" for event in store.events)
    assert raw_key not in json.dumps([event for event in store.events], default=str)


def test_vnext_agent_endpoint_rejects_agent_id_mismatch(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: keys are bound to a single agent id.",
            domain="project",
            sensitivity="private",
            agent_id="hermes",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert response.status_code == 403
    assert store.sources == {}


def test_vnext_agent_endpoint_rejects_conflicting_identity_namespaces(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )
    nested_identity = vnext_shared.VNextAgentIdentityRequest(
        agent_id="openclaw",
        agent_type="coding_agent",
        permission_profile="project_scoped_agent",
    )

    rejected = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: conflicting identities must be rejected.",
            domain="project",
            sensitivity="private",
            agent_identity=nested_identity,
            agent_id="hermes",
            agent_type="personal_assistant",
        ),
        authorization=f"Bearer {raw_key}",
    )
    accepted = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: matching identities preserve authenticated provenance.",
            domain="project",
            sensitivity="private",
            agent_identity=nested_identity,
            agent_id="openclaw",
            agent_type="coding_agent",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 201
    assert len(store.sources) == 1
    source = next(iter(store.sources.values()))
    metadata = source["metadata_json"]
    assert isinstance(metadata, dict)
    assert metadata["agent_identity"]["agent_id"] == "openclaw"
    assert metadata["agent_identity"]["agent_type"] == "coding_agent"


def test_vnext_agent_endpoint_without_keys_marks_unauthenticated_local(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    response = vnext_memories_router.create_vnext_source(
        vnext_memories_router.VNextSourceCaptureRequest(
            user_id=user_id,
            raw_text="Fact: fresh installs keep working without keys.",
            domain="project",
            sensitivity="private",
            agent_id="openclaw",
        )
    )

    assert response.status_code == 201
    policy_events = [event for event in store.events if event.get("event_type") == "policy.decision"]
    assert policy_events
    identity_record = policy_events[0]["payload_json"]["agent_identity"]
    assert identity_record["auth"] == "unauthenticated_local"


def _seed_active_memory(store: FakeVNextStore, *, text: str = "The quarterly plan is drafted.") -> str:
    memory_id = str(uuid4())
    store.memories.append(
        {
            "id": memory_id,
            "memory_type": "semantic",
            "memory_key": f"seed.{memory_id}",
            "value": {"text": text},
            "status": "active",
            "confidence": 0.9,
            "title": text[:60],
            "canonical_text": text,
            "summary": text,
            "domain": "professional",
            "sensitivity": "internal",
            "metadata_json": {},
            "valid_to": None,
        }
    )
    return memory_id


def _seed_pending_confirmation(store: FakeVNextStore, *, label: str) -> str:
    memory_id = _seed_active_memory(store, text=f"Pending confirmation for {label}.")
    memory = store.get_memory(memory_id)
    assert memory is not None
    memory.update(
        {
            "status": "needs_review",
            "confirmation_status": "unconfirmed",
            "last_confirmed_at": None,
            "last_reviewed_at": None,
            "metadata_json": {
                "review_required": True,
                "agentic_memory": {
                    "status": "confirmation_required",
                    "write_mode": "confirm_inline",
                    "lifecycle_status": "pending_inline_confirmation",
                    "requires_dashboard_review": True,
                    "confirmation": {
                        "confirmation_id": f"confirmation-{label}",
                        "status": "pending",
                    },
                },
            },
        }
    )
    return memory_id


def test_http_review_edit_synchronizes_title_and_summary(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    memory_id = _seed_active_memory(store, text="The old review text is stale.")
    corrected = "The reviewed memory now carries the corrected canonical text."

    response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(
            user_id=uuid4(),
            action="edit",
            canonical_text=corrected,
        ),
    )

    assert response.status_code == 200
    memory = store.get_memory(memory_id)
    assert memory is not None
    assert memory["canonical_text"] == corrected
    assert memory["title"] == corrected
    assert memory["summary"] == corrected


@pytest.mark.parametrize("marker", ["workflow", "stripped_memory_key"])
def test_generic_http_review_cannot_strand_pending_project_update_candidate(
    monkeypatch,
    marker: str,
) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="Pending project state candidate.")
    memory = store.get_memory(memory_id)
    assert memory is not None
    memory.update(
        {
            "status": "candidate",
            "memory_key": ("general.note" if marker == "workflow" else "  project_update.project-1.current_state  "),
            "metadata_json": {
                "candidate": True,
                **({"workflow": "project_auto_update"} if marker == "workflow" else {}),
            },
        }
    )
    store.projects["project-1"] = {
        "id": "project-1",
        "current_state": "Original state.",
        "status": "active",
    }
    store.artifacts["artifact-1"] = {
        "id": "artifact-1",
        "artifact_type": "project_update",
        "status": "candidate",
        "metadata_json": {"candidate_memory_id": memory_id, "workflow": "project_auto_update"},
    }
    store.revisions.append({"id": "revision-before", "memory_id": memory_id})
    store.events.append(
        {
            "event_type": "project.update_candidate_created",
            "target_type": "artifact",
            "target_id": "artifact-1",
            "payload_json": {"candidate_memory_id": memory_id},
        }
    )
    before = deepcopy(
        {
            "project": store.projects["project-1"],
            "memory": memory,
            "artifact": store.artifacts["artifact-1"],
            "revisions": store.revisions,
            "events": store.events,
        }
    )

    response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, action="reject"),
    )

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "detail": "pending project update candidates must be reviewed through the project update workflow"
    }
    assert {
        "project": store.projects["project-1"],
        "memory": store.get_memory(memory_id),
        "artifact": store.artifacts["artifact-1"],
        "revisions": store.revisions,
        "events": store.events,
    } == before


@pytest.mark.parametrize("action", ["accept", "edit", "promote", "reject"])
def test_http_terminal_reviews_close_nested_confirmation_metadata(
    monkeypatch,
    action: str,
) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_pending_confirmation(store, label=action)
    response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(
            user_id=user_id,
            action=action,
            canonical_text="Edited and confirmed text." if action == "edit" else None,
            reason=f"Exercise HTTP {action} terminal metadata.",
        ),
    )

    assert response.status_code == 200
    memory = store.get_memory(memory_id)
    assert memory is not None
    assert memory["last_reviewed_at"]
    metadata = memory["metadata_json"]
    assert metadata["review_required"] is False
    agentic = metadata["agentic_memory"]
    confirmation = agentic["confirmation"]
    if action == "reject":
        assert memory["status"] == "rejected"
        assert memory["confirmation_status"] == "unconfirmed"
        assert memory["last_confirmed_at"] is None
        assert agentic["status"] == "rejected"
        assert agentic["lifecycle_status"] == "review_rejected"
        assert confirmation["status"] == "rejected"
        assert confirmation["rejected_at"] == memory["last_reviewed_at"]
    else:
        assert memory["status"] == "active"
        assert memory["confirmation_status"] == "confirmed"
        assert memory["last_confirmed_at"]
        assert agentic["status"] == "committed"
        assert agentic["lifecycle_status"] == "dashboard_review_accepted"
        assert confirmation["status"] == "confirmed"
        assert confirmation["confirmed_at"] == memory["last_confirmed_at"]


def test_assign_project_replaces_canonical_memory_scope_used_by_retrieval(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="Release scope reassignment marker.")
    memory = store.get_memory(memory_id)
    assert memory is not None
    memory.update(
        {
            "domain": "project",
            "project_id": "project-old",
            "metadata_json": {
                "project_id": "project-old",
                "project_scope": ["project-old"],
            },
        }
    )

    response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(
            user_id=user_id,
            action="assign_project",
            project_id="project-new",
        ),
    )

    assert response.status_code == 200
    reassigned = store.get_memory(memory_id)
    assert reassigned is not None
    assert reassigned["project_id"] == "project-new"
    assert reassigned["metadata_json"]["project_id"] == "project-new"
    assert reassigned["metadata_json"]["project_scope"] == ["project-new"]

    def scoped_pack(project_id: str) -> dict[str, object]:
        pack_response = vnext_retrieval_router.create_vnext_context_pack(
            vnext_retrieval_router.VNextContextPackRequest(
                user_id=user_id,
                query="release scope reassignment marker",
                scope={"projects": [project_id]},
                options={"include_sources": False},
            )
        )
        assert pack_response.status_code == 201
        return json.loads(pack_response.body)

    assert scoped_pack("project-old")["relevant_memories"] == []
    assert [row["id"] for row in scoped_pack("project-new")["relevant_memories"]] == [memory_id]


def test_vnext_memory_expire_and_unexpire_endpoints(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store)

    expired = vnext_memories_router.expire_vnext_memory(
        vnext_memories_router.VNextMemoryExpireRequest(user_id=user_id, memory_id=memory_id, reason="Window closed")
    )
    assert expired.status_code == 200
    expired_payload = json.loads(expired.body)
    assert expired_payload["status"] == "expired"
    assert expired_payload["valid_to"]
    assert store.get_memory(memory_id)["valid_to"] == expired_payload["valid_to"]
    # Expiry is temporal, not a lifecycle judgment: the row stays active.
    assert store.get_memory(memory_id)["status"] == "active"
    assert any(event.get("event_type") == "agent.memory_expired" for event in store.events)

    unexpired = vnext_memories_router.unexpire_vnext_memory(
        vnext_memories_router.VNextMemoryUnexpireRequest(
            user_id=user_id, memory_id=memory_id, reason="Deadline extended"
        )
    )
    assert unexpired.status_code == 200
    assert json.loads(unexpired.body)["status"] == "active"
    assert store.get_memory(memory_id)["valid_to"] is None
    assert any(event.get("event_type") == "agent.memory_unexpired" for event in store.events)


def test_vnext_memory_accept_consolidation_endpoint_supersedes_members(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    member_id = _seed_active_memory(store, text="Standup happens in the morning.")
    candidate_id = _seed_active_memory(store, text="Standup happens every morning at 9:30am.")
    candidate = store.get_memory(candidate_id)
    candidate["status"] = "candidate"
    candidate["metadata_json"] = {
        "consolidation": {
            "proposal_kind": "merge",
            "cluster_member_ids": [member_id],
            "proposed_supersede": [member_id],
        },
        "review_required": True,
    }

    response = vnext_memories_router.accept_vnext_memory_consolidation(
        vnext_memories_router.VNextMemoryAcceptConsolidationRequest(
            user_id=user_id, memory_id=candidate_id, reason="Duplicates of one fact"
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "accepted"
    assert payload["superseded_member_ids"] == [member_id]
    assert store.get_memory(candidate_id)["status"] == "active"
    assert store.get_memory(member_id)["status"] == "superseded"
    assert store.get_memory(member_id)["superseded_by"] == candidate_id
    assert any(event.get("event_type") == "agent.memory_consolidation_accepted" for event in store.events)


def test_generic_http_review_delegates_consolidation_acceptance_and_rejects_stale_input(
    monkeypatch,
) -> None:
    class OrderedReviewStore(FakeVNextStore):
        def __init__(self) -> None:
            super().__init__(None)
            self.lock_order: list[str] = []

        def lock_graph_mutation(self) -> None:
            self.lock_order.append("graph")

        def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
            self.lock_order.append(f"row:{memory_id}")
            return self.get_memory(memory_id)

    store = OrderedReviewStore()
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    first_id = _seed_active_memory(store, text="First duplicate fact.")
    second_id = _seed_active_memory(store, text="Second duplicate fact.")

    def seed_candidate() -> str:
        candidate_id = _seed_active_memory(store, text="Canonical merged fact.")
        candidate = store.get_memory(candidate_id)
        candidate["status"] = "candidate"
        candidate["metadata_json"] = {
            "candidate_kind": "memory_consolidation",
            "review_required": True,
            "consolidation": {
                "proposal_kind": "merge",
                "cluster_member_ids": [first_id, second_id],
                "member_snapshots": [
                    memory_version_snapshot(store.get_memory(first_id)),
                    memory_version_snapshot(store.get_memory(second_id)),
                ],
                "proposed_supersede": [first_id, second_id],
            },
        }
        return candidate_id

    candidate_id = seed_candidate()
    edited = vnext_memories_router.review_vnext_memory(
        main_module.UUID(candidate_id),
        vnext_memories_router.VNextMemoryReviewRequest(
            user_id=user_id,
            action="edit",
            canonical_text="Unsafe edited merge.",
        ),
    )
    assert edited.status_code == 400
    assert store.get_memory(candidate_id)["status"] == "candidate"
    assert store.get_memory(first_id)["status"] == "active"

    store.lock_order.clear()
    accepted = vnext_memories_router.review_vnext_memory(
        main_module.UUID(candidate_id),
        vnext_memories_router.VNextMemoryReviewRequest(
            user_id=user_id,
            action="accept",
            reason="Reviewed duplicates.",
        ),
    )
    assert accepted.status_code == 200
    payload = json.loads(accepted.body)
    assert payload["consolidation_acceptance"]["status"] == "accepted"
    assert payload["consolidation_acceptance"]["superseded_member_ids"] == [
        first_id,
        second_id,
    ]
    assert store.get_memory(candidate_id)["metadata_json"]["consolidation"]["accepted"]
    assert store.get_memory(first_id)["superseded_by"] == candidate_id
    assert store.lock_order[0] == "graph"
    assert store.lock_order.count("graph") == 2
    first_row_index = next(index for index, item in enumerate(store.lock_order) if item.startswith("row:"))
    assert max(index for index, item in enumerate(store.lock_order) if item == "graph") < first_row_index

    # A stale generic approval must not partially supersede any member.
    fresh_first = _seed_active_memory(store, text="Fresh first fact.")
    fresh_second = _seed_active_memory(store, text="Fresh second fact.")
    stale_id = _seed_active_memory(store, text="Stale merge candidate.")
    stale = store.get_memory(stale_id)
    stale["status"] = "candidate"
    stale["metadata_json"] = {
        "candidate_kind": "memory_consolidation",
        "review_required": True,
        "consolidation": {
            "proposal_kind": "merge",
            "cluster_member_ids": [fresh_first, fresh_second],
            "member_snapshots": [
                memory_version_snapshot(store.get_memory(fresh_first)),
                memory_version_snapshot(store.get_memory(fresh_second)),
            ],
            "proposed_supersede": [fresh_first, fresh_second],
        },
    }
    store.get_memory(fresh_first)["canonical_text"] = "Changed after proposal."

    rejected = vnext_memories_router.review_vnext_memory(
        main_module.UUID(stale_id),
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, action="accept"),
    )
    assert rejected.status_code == 400
    assert json.loads(rejected.body)["detail"] == {
        "code": "invalid_request",
        "message": "The request is invalid",
    }
    assert store.get_memory(stale_id)["status"] == "candidate"
    assert store.get_memory(fresh_first)["status"] == "active"
    assert store.get_memory(fresh_second)["status"] == "active"


def test_vnext_memory_redact_endpoint_forgets_then_scrubs(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="The secret codename is Kestrel.")

    response = vnext_memories_router.redact_vnext_memory(
        vnext_memories_router.VNextMemoryRedactRequest(user_id=user_id, memory_id=memory_id, reason="Erasure request")
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "redacted"
    assert payload["forgotten_first"] is True
    assert payload["idempotent_replay"] is False
    assert payload["redacted_artifacts"] == 0
    assert payload["redacted_artifact_ids"] == []
    assert payload["redacted_quality_ratings"] == 0
    assert payload["redacted_provenance_links"] == 0
    assert payload["redaction_marker"] == "[REDACTED]"
    memory = store.get_memory(memory_id)
    assert memory["status"] == "archived"
    assert memory["canonical_text"] == "[REDACTED]"
    # Order of operations: the forget transition ran before the scrub, and
    # the memory.redacted trail survives it (earlier trail payloads are
    # themselves scrubbed by the events pass — event types are what remain).
    assert any(revision.get("revision_type") == "archived" for revision in store.revisions)
    redaction_trail = [event for event in store.events if event.get("event_type") == "memory.redacted"]
    assert len(redaction_trail) == 1
    assert redaction_trail[0]["payload_json"] == {
        "redacted": True,
        "memory_id": str(memory_id),
        "event_type": "memory.redacted",
    }

    redacted_at = memory["metadata_json"]["redacted_at"]
    state_after_first = deepcopy(
        (store.memories, store.artifacts, store.quality_ratings, store.provenance_links, store.revisions, store.events)
    )
    replay = vnext_memories_router.redact_vnext_memory(
        vnext_memories_router.VNextMemoryRedactRequest(
            user_id=user_id,
            memory_id=memory_id,
            reason="Repeated erasure request",
        )
    )

    assert replay.status_code == 200
    replay_payload = json.loads(replay.body)
    assert replay_payload["forgotten_first"] is False
    assert replay_payload["idempotent_replay"] is True
    assert replay_payload["redacted_revisions"] == 0
    assert replay_payload["redacted_events"] == 0
    assert replay_payload["redacted_artifacts"] == 0
    assert replay_payload["redacted_artifact_ids"] == []
    assert replay_payload["redacted_quality_ratings"] == 0
    assert replay_payload["redacted_provenance_links"] == 0
    assert store.get_memory(memory_id)["metadata_json"]["redacted_at"] == redacted_at
    assert (
        store.memories,
        store.artifacts,
        store.quality_ratings,
        store.provenance_links,
        store.revisions,
        store.events,
    ) == state_after_first

    missing = vnext_memories_router.redact_vnext_memory(
        vnext_memories_router.VNextMemoryRedactRequest(user_id=user_id, memory_id=uuid4(), reason="Nothing there")
    )
    assert missing.status_code == 404


def test_vnext_memory_redact_endpoint_blocks_non_admin_agents(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store)

    response = vnext_memories_router.redact_vnext_memory(
        vnext_memories_router.VNextMemoryRedactRequest(
            user_id=user_id, memory_id=memory_id, reason="Not allowed", agent_id="hermes"
        )
    )

    assert response.status_code == 403
    assert store.get_memory(memory_id)["status"] == "active"
    blocked_events = [event for event in store.events if event.get("event_type") == "agent.policy_blocked"]
    assert blocked_events
    decision = blocked_events[0]["payload_json"]["policy_decision"]
    assert decision["action"] == "memory.redact"
    assert "human_or_admin_review_required" in decision["reasons"]


def test_vnext_memory_lifecycle_endpoints_share_agent_key_auth(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store)
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="hermes", permission_profile="trusted_local_agent"
    )

    # Keyless agent calls are rejected once keys exist — parity with the
    # other vNext agent endpoints.
    keyless = vnext_memories_router.expire_vnext_memory(
        vnext_memories_router.VNextMemoryExpireRequest(
            user_id=user_id, memory_id=memory_id, reason="Window closed", agent_id="hermes"
        )
    )
    assert keyless.status_code == 401
    assert json.loads(keyless.body)["detail"] == {
        "code": "authentication_failed",
        "message": "Authentication failed",
    }
    assert store.get_memory(memory_id)["valid_to"] is None

    # With the key, the same call succeeds under the key-bound identity.
    keyed = vnext_memories_router.expire_vnext_memory(
        vnext_memories_router.VNextMemoryExpireRequest(
            user_id=user_id, memory_id=memory_id, reason="Window closed", agent_id="hermes"
        ),
        authorization=f"Bearer {raw_key}",
    )
    assert keyed.status_code == 200
    assert store.get_memory(memory_id)["valid_to"] is not None
    policy_events = [event for event in store.events if event.get("event_type") == "policy.decision"]
    assert policy_events
    identity_record = policy_events[-1]["payload_json"]["agent_identity"]
    assert identity_record["auth"] == "agent_api_key"

    keyless_unexpire = vnext_memories_router.unexpire_vnext_memory(
        vnext_memories_router.VNextMemoryUnexpireRequest(
            user_id=user_id, memory_id=memory_id, reason="Extended", agent_id="hermes"
        )
    )
    assert keyless_unexpire.status_code == 401
    keyless_redact = vnext_memories_router.redact_vnext_memory(
        vnext_memories_router.VNextMemoryRedactRequest(
            user_id=user_id, memory_id=memory_id, reason="Erase", agent_id="hermes"
        )
    )
    assert keyless_redact.status_code == 401
    keyless_accept = vnext_memories_router.accept_vnext_memory_consolidation(
        vnext_memories_router.VNextMemoryAcceptConsolidationRequest(
            user_id=user_id, memory_id=memory_id, reason="Merge", agent_id="hermes"
        )
    )
    assert keyless_accept.status_code == 401


def test_http_memory_review_rejects_non_admin_and_out_of_scope_keys(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="Project B review target.")
    store.get_memory(memory_id)["status"] = "candidate"
    store.get_memory(memory_id)["project_id"] = "project-b"

    _reader_record, reader_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="reader",
        permission_profile="read_only_agent",
        project_scope="project-b",
    )
    reader_response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, action="accept"),
        authorization=f"Bearer {reader_key}",
    )
    assert reader_response.status_code == 403
    assert store.get_memory(memory_id)["status"] == "candidate"
    assert "human_or_admin_review_required" in json.loads(reader_response.body)["policy_decision"]["reasons"]

    _admin_record, admin_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="admin",
        permission_profile="admin_agent",
        project_scope="project-a",
    )
    scope_response = vnext_memories_router.review_vnext_memory(
        main_module.UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, action="accept"),
        authorization=f"Bearer {admin_key}",
    )
    assert scope_response.status_code == 403
    assert store.get_memory(memory_id)["status"] == "candidate"
    assert "project_scope_binding_violation" in json.loads(scope_response.body)["policy_decision"]["reasons"]


def test_http_memory_lifecycle_authorizes_the_persisted_target_scope(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="Project B lifecycle target.")
    store.get_memory(memory_id)["project_id"] = "project-b"
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope="project-a",
    )

    response = vnext_memories_router.forget_vnext_memory(
        vnext_memories_router.VNextMemoryForgetRequest(
            user_id=user_id,
            memory_id=memory_id,
            reason="Cross-project attempt.",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert response.status_code == 403
    assert store.get_memory(memory_id)["status"] == "active"
    assert "project_scope_binding_violation" in json.loads(response.body)["policy_decision"]["reasons"]


def test_agent_output_ingest_api_creates_review_only_records(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()

    response = vnext_memories_router.ingest_vnext_agent_output(
        vnext_memories_router.VNextAgentOutputIngestRequest(
            user_id=user_id,
            agent_id="openclaw",
            agent_type="coding_agent",
            permission_profile="project_scoped_agent",
            agent_run_id="run-1",
            project_scope=["Alice"],
            title="Sprint summary",
            content="Decision: API agent output ingestion is review-only.",
            output_type="sprint_summary",
            propose_memory=True,
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert payload["status"] == "imported"
    assert payload["artifact_id"] in store.artifacts
    assert store.artifacts[payload["artifact_id"]]["status"] == "needs_review"
    assert payload["memory_id"] is not None
    assert any(memory["status"] == "candidate" for memory in store.memories)


def test_dogfooding_dashboard_and_insight_feedback_api(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact = store.create_artifact(
        {
            "artifact_type": "daily_brief",
            "title": "Daily",
            "content_markdown": "# Daily",
            "status": "needs_review",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    store.create_artifact_quality_rating(
        {
            "artifact_id": artifact["id"],
            "usefulness": 5,
            "verbosity": "right_sized",
            "metadata_json": {},
        }
    )

    feedback_response = vnext_review_router.record_vnext_artifact_insight_feedback(
        main_module.UUID(str(artifact["id"])),
        vnext_review_router.VNextArtifactInsightFeedbackRequest(
            user_id=user_id, useful_insight="yes", surfaced_missed="yes"
        ),
    )
    dashboard_response = vnext_memories_router.get_vnext_dogfooding_dashboard(user_id=user_id)
    dashboard = json.loads(dashboard_response.body)

    assert feedback_response.status_code == 201
    assert dashboard_response.status_code == 200
    assert dashboard["artifact_quality_rating_count"] == 1
    assert dashboard["insight_feedback"]["useful_yes"] == 1


def test_artifact_quality_rating_rejects_alias_forgery_and_rerates_authenticated_reviewer(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact = store.create_artifact(
        {
            "artifact_type": "daily_brief",
            "title": "Daily",
            "content_markdown": "# Daily",
            "status": "needs_review",
            "domain": "project",
            "sensitivity": "private",
        }
    )
    from alicebot_api.vnext_derived_labels import stamp_derived_from
    stamp_derived_from(artifact, {})
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="reviewer", permission_profile="trusted_local_agent"
    )

    rejected = vnext_review_router.rate_vnext_artifact_quality(
        main_module.UUID(str(artifact["id"])),
        vnext_review_router.VNextArtifactQualityRatingRequest(
            user_id=user_id,
            reviewer_id="forged-reviewer",
            usefulness=5,
            verbosity="right_sized",
        ),
        authorization=f"Bearer {raw_key}",
    )
    first = vnext_review_router.rate_vnext_artifact_quality(
        main_module.UUID(str(artifact["id"])),
        vnext_review_router.VNextArtifactQualityRatingRequest(
            user_id=user_id,
            reviewer_id="reviewer",
            usefulness=5,
            verbosity="right_sized",
        ),
        authorization=f"Bearer {raw_key}",
    )
    second = vnext_review_router.rate_vnext_artifact_quality(
        main_module.UUID(str(artifact["id"])),
        vnext_review_router.VNextArtifactQualityRatingRequest(
            user_id=user_id,
            reviewer_id="reviewer",
            usefulness=2,
            verbosity="too_shallow",
        ),
        authorization=f"Bearer {raw_key}",
    )

    assert rejected.status_code == 403
    assert first.status_code == 201
    assert second.status_code == 201
    assert len(store.quality_ratings) == 1
    assert store.quality_ratings[0]["reviewer_id"] == "reviewer"
    assert store.quality_ratings[0]["usefulness"] == 2
    assert store.quality_ratings[0]["verbosity"] == "too_shallow"


def test_insight_feedback_rejects_exact_redacted_project_update(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = str(uuid4())
    artifact = store.create_artifact(
        {
            "artifact_type": "project_update",
            "title": "[REDACTED]",
            "content_markdown": "[REDACTED]",
            "status": "accepted",
            "domain": "project",
            "sensitivity": "private",
            "prompt_hash": None,
            "model_info_json": {"redacted": True},
            "metadata_json": {
                "redacted": True,
                "redacted_at": "2026-07-16T00:00:00Z",
                "workflow": "project_auto_update",
                "project_id": "project-1",
                "project_scope": ["project-1"],
                "candidate_memory_id": memory_id,
                "review_action": "accept",
            },
        }
    )
    events_before = deepcopy(store.events)

    response = vnext_review_router.record_vnext_artifact_insight_feedback(
        main_module.UUID(str(artifact["id"])),
        vnext_review_router.VNextArtifactInsightFeedbackRequest(
            user_id=user_id, useful_insight="yes", comments="secret"
        ),
    )

    assert response.status_code == 400
    assert store.events == events_before


def test_artifact_routes_authorize_persisted_target_scope_and_profile(monkeypatch) -> None:
    from alicebot_api.vnext_agent_keys import create_agent_key

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    artifact_id = str(uuid4())
    store.artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "daily_brief",
        "title": "Project B private brief",
        "content_markdown": "# Project B\n\nPrivate target content.",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {"project_id": "project-b"},
    }

    from alicebot_api.vnext_derived_labels import stamp_derived_from
    stamp_derived_from(store.artifacts[artifact_id], {})
    _reader_record, reader_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="project-b-reader",
        permission_profile="read_only_agent",
        project_scope="project-b",
    )
    feedback_status, feedback_payload = _invoke_vnext_request(
        "POST",
        f"/v0/vnext/artifacts/{artifact_id}/insight-feedback",
        authorization=f"Bearer {reader_key}",
        payload={"user_id": str(user_id), "useful_insight": "yes"},
    )
    assert feedback_status == 403
    assert "read_only_agent_cannot_write" in feedback_payload["policy_decision"]["reasons"]

    sensitive_source_id = str(uuid4())
    public_artifact_id = str(uuid4())
    store.sources[sensitive_source_id] = {
        "id": sensitive_source_id,
        "domain": "health",
        "sensitivity": "highly_sensitive",
        "metadata_json": {"project_id": "project-b", "raw_text": "VERY SECRET"},
    }
    store.artifacts[public_artifact_id] = {
        "id": public_artifact_id,
        "artifact_type": "daily_brief",
        "title": "Public shell",
        "content_markdown": "# Public shell",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": {
            "project_id": "project-b",
            "source_refs": [f"source:{sensitive_source_id}"],
        },
    }
    derived_status, _derived_payload = _invoke_vnext_request(
        "GET", f"/v0/vnext/traces/artifacts/{public_artifact_id}",
        query={"user_id": str(user_id)}, authorization=f"Bearer {reader_key}",
    )
    assert derived_status == 403
    # A manual original public artifact exercises per-source trace projection separately.
    store.artifacts[public_artifact_id]["artifact_type"] = "manual_note"
    trace_status, trace_payload = _invoke_vnext_request(
        "GET",
        f"/v0/vnext/traces/artifacts/{public_artifact_id}",
        query={"user_id": str(user_id)},
        authorization=f"Bearer {reader_key}",
    )
    assert trace_status == 200
    assert trace_payload["sources"] == []
    assert "VERY SECRET" not in json.dumps(trace_payload)

    _project_a_record, project_a_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="project-a-admin",
        permission_profile="admin_agent",
        project_scope="project-a",
    )
    denied_requests = (
        (
            "GET",
            f"/v0/vnext/artifacts/{artifact_id}",
            {"query": {"user_id": str(user_id)}},
        ),
        (
            "GET",
            f"/v0/vnext/traces/artifacts/{artifact_id}",
            {"query": {"user_id": str(user_id)}},
        ),
        (
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/review",
            {"payload": {"user_id": str(user_id), "action": "accept"}},
        ),
        (
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/quality-ratings",
            {"payload": {"user_id": str(user_id), "verbosity": "right_sized"}},
        ),
        (
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/export",
            {"payload": {"user_id": str(user_id), "output_dir": "/tmp"}},
        ),
    )
    for method, path, kwargs in denied_requests:
        status, payload = _invoke_vnext_request(
            method,
            path,
            authorization=f"Bearer {project_a_key}",
            **kwargs,
        )
        assert status == 403, (method, path, payload)
        assert "project_scope_binding_violation" in payload["policy_decision"]["reasons"]
        assert "content_markdown" not in payload

    _trusted_b_record, trusted_b_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="project-b-trusted",
        permission_profile="trusted_local_agent",
        project_scope="project-b",
    )
    assert (
        _invoke_vnext_request(
            "GET",
            f"/v0/vnext/artifacts/{artifact_id}",
            query={"user_id": str(user_id)},
            authorization=f"Bearer {trusted_b_key}",
        )[0]
        == 200
    )
    assert (
        _invoke_vnext_request(
            "GET",
            f"/v0/vnext/traces/artifacts/{artifact_id}",
            query={"user_id": str(user_id)},
            authorization=f"Bearer {trusted_b_key}",
        )[0]
        == 200
    )
    assert (
        _invoke_vnext_request(
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/insight-feedback",
            authorization=f"Bearer {trusted_b_key}",
            payload={"user_id": str(user_id), "useful_insight": "yes"},
        )[0]
        == 201
    )
    assert (
        _invoke_vnext_request(
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/quality-ratings",
            authorization=f"Bearer {trusted_b_key}",
            payload={"user_id": str(user_id), "verbosity": "right_sized", "usefulness": 5},
        )[0]
        == 201
    )
    assert (
        _invoke_vnext_request(
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/export",
            authorization=f"Bearer {trusted_b_key}",
            payload={"user_id": str(user_id), "output_dir": "/tmp"},
        )[0]
        == 200
    )

    _admin_b_record, admin_b_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="project-b-admin",
        permission_profile="admin_agent",
        project_scope="project-b",
    )
    assert (
        _invoke_vnext_request(
            "POST",
            f"/v0/vnext/artifacts/{artifact_id}/review",
            authorization=f"Bearer {admin_b_key}",
            payload={"user_id": str(user_id), "action": "accept"},
        )[0]
        == 200
    )
