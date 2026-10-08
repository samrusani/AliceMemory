from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import re
from typing import Callable, Protocol, Sequence, cast

from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_model_intelligence import (
    ModelBackedRequest,
    ModelRoutingRequest,
    build_model_backed_artifact,
    resolve_model_route,
)
from alicebot_api.vnext_project_scope import project_scopes_overlap, source_project_scope
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_workflow_idempotency import logical_workflow_digest


DEFAULT_CONNECTION_LIMIT = 8
MAX_LEGACY_PROJECT_SCOPE_ROWS = 16_384
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
CONNECTION_TO_EDGE_TYPE = {
    "same_problem": "same_problem",
    "same_principle": "same_principle",
    "cross_domain_pattern": "cross_domain_pattern",
    "contradiction": "contradicts",
    "supporting_evidence": "supports",
    "weak_signal": "similar_to",
    "recurring_theme": "same_principle",
    "forgotten_relevant_note": "old_idea_now_relevant",
    "belief_reinforcement": "belief_reinforcement",
    "belief_challenge": "belief_challenge",
    "old_idea_now_relevant": "old_idea_now_relevant",
}
EDGE_REVIEW_ACTIONS = {
    "review": "reviewed",
    "accept": "accepted",
    "reject": "rejected",
}
STOPWORDS = {
    "about",
    "after",
    "again",
    "alice",
    "because",
    "before",
    "being",
    "brief",
    "could",
    "from",
    "have",
    "into",
    "note",
    "project",
    "should",
    "source",
    "that",
    "this",
    "with",
}


class VNextConnectionValidationError(ValueError):
    """Raised when a vNext connection workflow request is invalid."""


class VNextConnectionStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def create_artifact(self, artifact: JsonObject) -> JsonObject: ...

    def create_edge(self, edge: JsonObject) -> JsonObject: ...

    def find_artifact_by_workflow_digest(
        self,
        *,
        artifact_type: str,
        workflow: str,
        digest: str,
        scope_projects: Sequence[str] | None = None,
    ) -> JsonObject | None: ...

    def upsert_artifact_by_workflow_digest(
        self,
        artifact: JsonObject,
        *,
        workflow: str,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def upsert_edge_by_idempotency_digest(
        self,
        edge: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def update_edge_status(self, *, edge_id: str, status: str) -> JsonObject: ...

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[JsonObject]: ...

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_CONNECTION_LIMIT,
    ) -> list[JsonObject]: ...

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_CONNECTION_LIMIT,
    ) -> list[JsonObject]: ...


@dataclass(frozen=True, slots=True)
class ConnectionFinderRequest:
    query: str = ""
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    max_connections: int = DEFAULT_CONNECTION_LIMIT
    auto_accept_threshold: float | None = None
    generated_by: str = "system"
    actor_id: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    agent_identity: JsonObject | None = field(kw_only=True)
    policy_decision: JsonObject | None = None
    metadata_json: JsonObject = field(default_factory=dict)
    generation_mode: str = "deterministic"
    model_route_mode: str | None = None
    model_provider: str | None = None
    model: str | None = None
    model_temperature: float = 0.2
    allow_cloud_private: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionCandidate:
    source: JsonObject
    memory: JsonObject
    connection_type: str
    explanation: str
    why_it_matters: str
    confidence: float
    shared_terms: tuple[str, ...]
    provenance: tuple[str, ...]

    def to_record(self) -> JsonObject:
        return {
            "source_item": f"source:{self.source.get('id')}",
            "connected_item": f"memory:{self.memory.get('id')}",
            "connection_type": self.connection_type,
            "explanation": self.explanation,
            "why_it_matters": self.why_it_matters,
            "confidence": self.confidence,
            "provenance": list(self.provenance),
            "shared_terms": list(self.shared_terms),
        }


def _validate_request(request: ConnectionFinderRequest) -> None:
    if request.max_connections < 1 or request.max_connections > 50:
        raise VNextConnectionValidationError("max_connections must be between 1 and 50")
    if not request.sensitivity_allowed:
        raise VNextConnectionValidationError("sensitivity_allowed must not be empty")
    if request.generation_mode not in {"deterministic", "model_backed"}:
        raise VNextConnectionValidationError("generation_mode must be deterministic or model_backed")
    if request.model_temperature < 0.0 or request.model_temperature > 2.0:
        raise VNextConnectionValidationError("model_temperature must be between 0.0 and 2.0")
    if request.auto_accept_threshold is not None and (
        request.auto_accept_threshold < 0.0 or request.auto_accept_threshold > 1.0
    ):
        raise VNextConnectionValidationError("auto_accept_threshold must be between 0.0 and 1.0")


def _supports_parameter(method: object, name: str) -> bool:
    if not callable(method):
        return False
    try:
        return name in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def _matches_projects(
    row: JsonObject, projects: tuple[str, ...], *, source_row: bool, all_of: tuple[str, ...] | None = None
) -> bool:
    if all_of is not None:
        from alicebot_api.vnext_derived_labels import input_admitted

        return input_admitted("source" if source_row else "memory", row, all_of)
    if not projects:
        return True
    row_scope = source_project_scope(row) if source_row else resource_project_scope(row)
    return project_scopes_overlap(row_scope, projects)


def _project_scoped_search(
    method: Callable[..., list[JsonObject]],
    *,
    kwargs: dict[str, object],
    projects: tuple[str, ...],
    project_parameter: str,
    limit: int,
    source_rows: bool = False,
    all_of: tuple[str, ...] | None = None,
) -> list[JsonObject]:
    if not projects and all_of is None:
        return list(method(limit=limit, **kwargs))
    if _supports_parameter(method, project_parameter):
        fetch_limit = limit
        while True:
            rows = method(limit=fetch_limit, **kwargs, **{project_parameter: projects})
            selected = [row for row in rows if _matches_projects(row, projects, source_row=source_rows, all_of=all_of)]
            if all_of is None or len(selected) >= limit or len(rows) < fetch_limit:
                return selected[:limit]
            if fetch_limit >= MAX_LEGACY_PROJECT_SCOPE_ROWS:
                raise VNextConnectionValidationError("locked input selection could not prove complete project scope")
            fetch_limit = min(fetch_limit * 2, MAX_LEGACY_PROJECT_SCOPE_ROWS)
    rows = list(method(limit=MAX_LEGACY_PROJECT_SCOPE_ROWS + 1, **kwargs))
    if len(rows) > MAX_LEGACY_PROJECT_SCOPE_ROWS:
        raise VNextConnectionValidationError("legacy connection store could not prove complete project scope")
    return [row for row in rows if _matches_projects(row, projects, source_row=source_rows, all_of=all_of)][:limit]


def _record_text(row: JsonObject) -> str:
    parts: list[str] = []
    for key in ("title", "canonical_text", "summary", "memory_key", "source_type"):
        value = row.get(key)
        if isinstance(value, str):
            parts.append(value)
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        for key in ("raw_text", "relative_path", "filename"):
            value = metadata.get(key)
            if isinstance(value, str):
                parts.append(value)
    value = row.get("value")
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, (str, int, float, bool)):
                parts.append(str(child))
    return " ".join(parts)


def _terms(row: JsonObject) -> set[str]:
    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", _record_text(row))
        if token.casefold() not in STOPWORDS
    }
    return terms


def _title(row: JsonObject) -> str:
    title = row.get("title")
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())
    text = row.get("canonical_text") or row.get("summary") or row.get("memory_key") or row.get("id")
    return " ".join(str(text).split())


def _observed_at(source: JsonObject) -> tuple[object, str]:
    """Event time for an edge derived from ``source``.

    Prefers the source's own event time (``source_created_at``), falls back
    to ingestion time (``captured_at``), and finally to now. Returns the
    timestamp plus which field supplied it, so the fallback is recorded in
    the edge metadata.
    """
    for key in ("source_created_at", "captured_at"):
        value = source.get(key)
        if value is not None and value != "":
            return value, key
    return datetime.now(UTC).isoformat().replace("+00:00", "Z"), "now"


def _provenance(source: JsonObject, memory: JsonObject) -> tuple[str, ...]:
    output: list[str] = []
    metadata = source.get("metadata_json")
    if isinstance(metadata, dict):
        chunk_ids = metadata.get("source_chunk_ids")
        if isinstance(chunk_ids, list):
            output.extend(str(chunk_id) for chunk_id in chunk_ids if isinstance(chunk_id, str))
    output.append(f"source:{source.get('id')}")
    output.append(f"memory:{memory.get('id')}")
    return tuple(output)


def _connection_type(source: JsonObject, memory: JsonObject, shared_terms: set[str]) -> str:
    source_domain = source.get("domain")
    memory_domain = memory.get("domain")
    memory_type = memory.get("memory_type")
    if memory_type in {"belief", "thesis"}:
        return "belief_reinforcement"
    if isinstance(source_domain, str) and isinstance(memory_domain, str) and source_domain != memory_domain:
        return "cross_domain_pattern"
    if {"blocked", "problem", "failure", "risk"} & shared_terms:
        return "same_problem"
    if {"principle", "pattern", "rule", "standard"} & shared_terms:
        return "same_principle"
    if {"old", "again", "revisit"} & shared_terms:
        return "old_idea_now_relevant"
    return "recurring_theme"


def _confidence(shared_terms: set[str], *, cross_domain: bool, memory_type: object) -> float:
    base = 0.52 + min(len(shared_terms), 5) * 0.07
    if cross_domain:
        base += 0.04
    if memory_type in {"belief", "thesis"}:
        base += 0.05
    return round(min(base, 0.92), 2)


def _find_candidates(
    *,
    sources: list[JsonObject],
    memories: list[JsonObject],
    limit: int,
) -> list[ConnectionCandidate]:
    candidates: list[ConnectionCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for source in sources:
        source_terms = _terms(source)
        if not source_terms:
            continue
        for memory in memories:
            pair = (str(source.get("id")), str(memory.get("id")))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            shared_terms = source_terms & _terms(memory)
            cross_domain = (
                isinstance(source.get("domain"), str)
                and isinstance(memory.get("domain"), str)
                and source.get("domain") != memory.get("domain")
            )
            if len(shared_terms) < 2 and not (cross_domain and shared_terms):
                continue
            connection_type = _connection_type(source, memory, shared_terms)
            confidence = _confidence(
                shared_terms,
                cross_domain=cross_domain,
                memory_type=memory.get("memory_type"),
            )
            candidates.append(
                ConnectionCandidate(
                    source=source,
                    memory=memory,
                    connection_type=connection_type,
                    explanation=(f"{_title(source)} and {_title(memory)} share {', '.join(sorted(shared_terms)[:4])}."),
                    why_it_matters=(
                        "This may help Alice connect new evidence to older context without flattening them into one memory."
                    ),
                    confidence=confidence,
                    shared_terms=tuple(sorted(shared_terms)),
                    provenance=_provenance(source, memory),
                )
            )
    candidates.sort(
        key=lambda candidate: (
            -candidate.confidence,
            candidate.to_record()["source_item"],
            candidate.to_record()["connected_item"],
        )
    )
    return candidates[:limit]


def _report_markdown(connections: list[JsonObject], edge_ids: list[str]) -> str:
    lines = ["# Connection Report", "", "## Candidate Connections"]
    if not connections:
        lines.append("- No high-value candidate connection was detected from the selected inputs.")
    for index, connection in enumerate(connections, start=1):
        provenance = connection["provenance"]
        provenance_items = provenance if isinstance(provenance, list) else []
        lines.extend(
            [
                f"### {index}. {connection['connection_type']}",
                f"- Source item: {connection['source_item']}",
                f"- Connected item: {connection['connected_item']}",
                f"- Confidence: {connection['confidence']}",
                f"- Explanation: {connection['explanation']}",
                f"- Why it matters: {connection['why_it_matters']}",
                f"- Provenance: {', '.join(str(item) for item in provenance_items)}",
                "",
            ]
        )
    lines.extend(["## Candidate Graph Edges", *(f"- graph_edge:{edge_id}" for edge_id in edge_ids)])
    return "\n".join(lines).rstrip() + "\n"


def _brain_charter(store: VNextConnectionStore) -> JsonObject | None:
    getter = getattr(store, "get_brain_charter", None)
    if not callable(getter):
        return None
    charter = getter()
    return charter if isinstance(charter, dict) else None


class VNextConnectionService:
    def __init__(self, store: VNextConnectionStore) -> None:
        self.store = store

    def generate_connection_report(self, request: ConnectionFinderRequest | None = None) -> JsonObject:
        request = request or ConnectionFinderRequest(agent_identity=None, )
        _validate_request(request)
        domains = list(request.domains) if request.domains else None
        sensitivity_allowed = list(request.sensitivity_allowed)
        input_limit = max(request.max_connections * 2, request.max_connections)
        from alicebot_api.vnext_derived_labels import locked_projects

        all_of = locked_projects(request.agent_identity, request.projects)
        sources = _project_scoped_search(
            self.store.search_sources,
            kwargs={
                "query": request.query,
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            projects=request.projects,
            project_parameter="scope_projects",
            limit=input_limit,
            source_rows=True,
            all_of=all_of,
        )
        memories = _project_scoped_search(
            self.store.search_memories,
            kwargs={
                "query": request.query,
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            projects=request.projects,
            project_parameter="projects",
            limit=input_limit,
            all_of=all_of,
        )
        from alicebot_api.vnext_label_guard import admit_loaded

        sources = admit_loaded(
            self.store,
            kind="source",
            rows=sources,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=request.projects,
            all_of=all_of,
        )
        memories = admit_loaded(
            self.store,
            kind="memory",
            rows=memories,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            projects=request.projects,
            all_of=all_of,
        )
        candidates = _find_candidates(
            sources=sources,
            memories=memories,
            limit=request.max_connections,
        )
        workflow_digest = logical_workflow_digest(
            {
                "workflow": "connection_report",
                "scope": {
                    "domains": sorted(request.domains),
                    "projects": sorted(request.projects),
                    "sensitivity_allowed": sorted(request.sensitivity_allowed),
                },
                "request": {
                    "query": request.query,
                    "max_connections": request.max_connections,
                    "auto_accept_threshold": request.auto_accept_threshold,
                    "generated_by": request.generated_by,
                    "actor_id": request.actor_id,
                    "agent_identity": request.agent_identity,
                    "policy_decision": request.policy_decision,
                    "metadata_json": request.metadata_json,
                    "generation_mode": request.generation_mode,
                    "model_route_mode": request.model_route_mode,
                    "model_provider": request.model_provider,
                    "model": request.model,
                    "model_temperature": request.model_temperature,
                    "allow_cloud_private": request.allow_cloud_private,
                    "brain_charter": _brain_charter(self.store),
                },
                "inputs": {"sources": sources, "memories": memories},
                "candidates": [candidate.to_record() for candidate in candidates],
            }
        )
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing = cast(Callable[..., JsonObject | None], find_existing)(
                artifact_type="connection_report",
                workflow="connection_finder",
                digest=workflow_digest,
                scope_projects=request.projects or None,
            )
            if existing is not None:
                return existing
        edge_ids: list[str] = []
        connection_records: list[JsonObject] = []
        for candidate in candidates:
            status = (
                "accepted"
                if (request.auto_accept_threshold is not None and candidate.confidence >= request.auto_accept_threshold)
                else "candidate"
            )
            # Event time: when the connected observation happened (the
            # source's own timestamp), not when this edge was written.
            # valid_from starts the validity interval at the same instant.
            observed_at, observed_at_source = _observed_at(candidate.source)
            connection_record = candidate.to_record()
            edge_digest = logical_workflow_digest(
                {
                    "workflow_digest": workflow_digest,
                    "edge_type": CONNECTION_TO_EDGE_TYPE[candidate.connection_type],
                    "from_type": "source",
                    "from_id": str(candidate.source.get("id")),
                    "to_type": "memory",
                    "to_id": str(candidate.memory.get("id")),
                    "connection": connection_record,
                }
            )
            edge_payload: JsonObject = {
                "from_type": "source",
                "from_id": str(candidate.source.get("id")),
                "to_type": "memory",
                "to_id": str(candidate.memory.get("id")),
                "edge_type": CONNECTION_TO_EDGE_TYPE[candidate.connection_type],
                "confidence": candidate.confidence,
                "explanation": candidate.explanation,
                "created_by": "vnext_connection_finder",
                "observed_at": observed_at,
                "valid_from": observed_at,
                "metadata_json": {
                    "status": status,
                    "connection": connection_record,
                    "candidate": status == "candidate",
                    "observed_at_source": observed_at_source,
                    "workflow": "connection_finder",
                    "workflow_digest": workflow_digest,
                    "edge_digest": edge_digest,
                    "generated_by": request.generated_by,
                    "scheduler_run_id": request.run_id if request.generated_by == "scheduler" else None,
                    "trace_id": request.trace_id,
                    "policy_decision": request.policy_decision,
                    "project_scope": list(request.projects),
                },
            }
            upsert_edge = getattr(self.store, "upsert_edge_by_idempotency_digest", None)
            if callable(upsert_edge):
                edge = cast(Callable[..., JsonObject], upsert_edge)(
                    edge_payload,
                    digest=edge_digest,
                    actor_type=request.generated_by,
                )
            else:
                edge = self.store.create_edge(edge_payload)
            edge_id = str(edge["id"])
            edge_ids.append(edge_id)
            connection_records.append(connection_record)
            append_event(
                self.store,
                event_type="connection.candidate_edge_logged",
                actor_type=request.generated_by,
                actor_id=request.actor_id,
                target_type="graph_edge",
                target_id=edge_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                payload={
                    "connection_type": candidate.connection_type,
                    "edge_type": CONNECTION_TO_EDGE_TYPE[candidate.connection_type],
                    "confidence": candidate.confidence,
                    "status": status,
                    "policy_decision": request.policy_decision,
                },
            )

        source_ids = [str(source.get("id")) for source in sources if source.get("id") is not None]
        memory_ids = [str(memory.get("id")) for memory in memories if memory.get("id") is not None]
        source_refs = [f"source:{source_id}" for source_id in source_ids]
        content = _report_markdown(connection_records, edge_ids)
        metadata = {
            "workflow": "connection_finder",
            "workflow_type": "connection_report",
            "candidate_edge_ids": edge_ids,
            "connections": connection_records,
            "source_ids": source_ids,
            "memory_ids": memory_ids,
            "source_refs": source_refs,
            "input_counts": {"sources": len(sources), "memories": len(memories)},
            "generated_by": request.generated_by,
            "agent_identity": request.agent_identity,
            "agent_id": request.actor_id if request.generated_by == "agent" else None,
            "agent_run_id": request.run_id if request.generated_by == "agent" else None,
            "scheduler_run_id": request.run_id if request.generated_by == "scheduler" else None,
            "trace_id": request.trace_id,
            "policy_decision": request.policy_decision,
            "review_status": "needs_review",
            "project_scope": list(request.projects),
            "generation_mode": request.generation_mode,
            **request.metadata_json,
            "workflow_digest": workflow_digest,
        }
        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if request.generation_mode == "model_backed":
            route = resolve_model_route(
                ModelRoutingRequest(
                    workflow_type="connection_report",
                    generation_mode="model_backed",
                    domains=request.domains,
                    sensitivity_allowed=request.sensitivity_allowed,
                    agent_identity=request.agent_identity,
                    brain_charter=_brain_charter(self.store),
                    requested_route_mode=request.model_route_mode,
                    requested_provider=request.model_provider,
                    requested_model=request.model,
                    allow_cloud_private=request.allow_cloud_private,
                )
            )
            model_artifact = build_model_backed_artifact(
                ModelBackedRequest(
                    workflow_type="connection_report",
                    title="Connection Report",
                    deterministic_markdown=content,
                    context_rows=tuple([*sources, *memories, *connection_records]),
                    source_refs=tuple(source_refs),
                    open_questions=("Which candidate edge is worth accepting into the graph?",),
                    trace_id=request.trace_id,
                    route=route,
                    temperature=request.model_temperature,
                    config={"generated_by": request.generated_by, "agent_id": request.actor_id},
                )
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            metadata = {**metadata, **model_artifact.metadata}
        metadata = with_derived_from(metadata, {"sources": sources, "memories": memories})
        artifact_payload: JsonObject = {
            "artifact_type": "connection_report",
            "title": "Connection Report",
            "content_markdown": content,
            "status": "needs_review",
            "domain": derived_domain([*sources, *memories], fallback=request.domains[0] if len(request.domains) == 1 else "unknown"),
            "sensitivity": self._highest_sensitivity([*sources, *memories]),
            "generated_by": request.generated_by if request.generated_by != "system" else "vnext_connection_finder",
            "prompt_hash": prompt_hash,
            "model_info_json": model_info_json,
            "metadata_json": metadata,
        }
        upsert_artifact = getattr(self.store, "upsert_artifact_by_workflow_digest", None)
        if callable(upsert_artifact):
            artifact = cast(Callable[..., JsonObject], upsert_artifact)(
                artifact_payload,
                workflow="connection_finder",
                digest=workflow_digest,
                actor_type=request.generated_by,
            )
        else:
            artifact = self.store.create_artifact(artifact_payload)
        append_event(
            self.store,
            event_type="artifact.generated",
            actor_type=request.generated_by,
            actor_id=request.actor_id,
            target_type="artifact",
            target_id=str(artifact["id"]),
            trace_id=request.trace_id,
            run_id=request.run_id,
            payload={
                "workflow": "connection_finder",
                "workflow_type": "connection_report",
                "artifact_type": "connection_report",
                "candidate_edge_count": len(edge_ids),
                "policy_decision": request.policy_decision,
                "generation_mode": request.generation_mode,
            },
        )
        return artifact

    def review_edge(self, *, edge_id: str, action: str) -> JsonObject:
        status = EDGE_REVIEW_ACTIONS.get(action)
        if status is None:
            raise VNextConnectionValidationError("edge review action must be review, accept, or reject")
        edge = self.store.update_edge_status(edge_id=edge_id, status=status)
        append_event(
            self.store,
            event_type="graph_edge.reviewed",
            actor_type="system",
            target_type="graph_edge",
            target_id=edge_id,
            payload={"action": action, "status": status},
        )
        return edge

    def graph_neighborhood(self, *, target_id: str) -> JsonObject:
        from_edges = self.store.list_edges(from_id=target_id)
        to_edges = self.store.list_edges(to_id=target_id)
        return {
            "target_id": target_id,
            "from_edges": from_edges,
            "to_edges": to_edges,
            "edge_count": len(from_edges) + len(to_edges),
        }

    @staticmethod
    def _highest_sensitivity(rows: list[JsonObject]) -> str:
        rank = {
            "public": 1,
            "internal": 2,
            "unknown": 2,
            "private": 3,
            "confidential": 4,
            "highly_sensitive": 5,
            "sacred": 6,
            "regulated": 6,
        }
        sensitivities = [str(row.get("sensitivity", "unknown")) for row in rows]
        if not sensitivities:
            return "unknown"
        return max(sensitivities, key=lambda value: rank.get(value, rank["unknown"]))


__all__ = [
    "ConnectionFinderRequest",
    "VNextConnectionService",
    "VNextConnectionStore",
    "VNextConnectionValidationError",
]
