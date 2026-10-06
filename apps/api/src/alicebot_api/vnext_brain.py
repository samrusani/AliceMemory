from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import inspect
import json
import re
from typing import Callable, Protocol, Sequence, cast
from uuid import UUID

from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_model_intelligence import (
    ModelBackedArtifact,
    ModelBackedRequest,
    ModelRoutingRequest,
    build_model_backed_artifact,
    resolve_model_route,
)
from alicebot_api.vnext_project_scope import (
    normalize_project_scope,
    project_scope_identity,
    project_scopes_overlap,
    source_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_temporal_query import parse_event_datetime


DEFAULT_BRAIN_LIMIT = 8
DEFAULT_ARTIFACT_LIMIT = 4
MAX_LEGACY_WINDOW_SCAN_ROWS = 16_384
DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
SENSITIVITY_RANK = {
    "public": 1,
    "internal": 2,
    "unknown": 2,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}


class VNextBrainValidationError(ValueError):
    """Raised when a vNext brain workflow request is invalid."""


class VNextBrainStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_open_loop(self, loop: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_BRAIN_LIMIT,
    ) -> list[JsonObject]: ...

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_BRAIN_LIMIT,
    ) -> list[JsonObject]: ...

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_BRAIN_LIMIT,
    ) -> list[JsonObject]: ...

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = DEFAULT_ARTIFACT_LIMIT,
    ) -> list[JsonObject]: ...

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

    def upsert_open_loop_by_automation_digest(
        self,
        loop: JsonObject,
        *,
        digest: str,
        actor_type: str = "system",
    ) -> JsonObject: ...

    def upsert_memory_by_key(
        self,
        memory: JsonObject,
        *,
        actor_type: str = "system",
    ) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class BrainArtifactRequest:
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = DEFAULT_SENSITIVITY_ALLOWED
    generated_for: str | None = None
    source_limit: int = DEFAULT_BRAIN_LIMIT
    memory_limit: int = DEFAULT_BRAIN_LIMIT
    open_loop_limit: int = DEFAULT_BRAIN_LIMIT
    artifact_limit: int = DEFAULT_ARTIFACT_LIMIT
    discover_open_loops: bool = True
    create_candidate_memories: bool = True
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


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _iso_week_label(day: date) -> str:
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def _parse_generated_for(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise VNextBrainValidationError("generated_for must be an ISO date in YYYY-MM-DD format") from exc


def _validate_request(request: BrainArtifactRequest) -> None:
    if not request.sensitivity_allowed:
        raise VNextBrainValidationError("sensitivity_allowed must not be empty")
    if request.generation_mode not in {"deterministic", "model_backed"}:
        raise VNextBrainValidationError("generation_mode must be deterministic or model_backed")
    for field_name in ("source_limit", "memory_limit", "open_loop_limit", "artifact_limit"):
        value = getattr(request, field_name)
        if value < 1 or value > 50:
            raise VNextBrainValidationError(f"{field_name} must be between 1 and 50")
    if request.model_temperature < 0.0 or request.model_temperature > 2.0:
        raise VNextBrainValidationError("model_temperature must be between 0.0 and 2.0")
    _parse_generated_for(request.generated_for)


def _allowed_domains(request: BrainArtifactRequest) -> list[str] | None:
    return list(request.domains) if request.domains else None


def _allowed_sensitivity(request: BrainArtifactRequest) -> list[str]:
    return list(request.sensitivity_allowed)


def _report_window(day: date, *, days: int) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=days)


def _weekly_report_window(day: date) -> tuple[datetime, datetime]:
    return _report_window(day - timedelta(days=day.weekday()), days=7)


def _row_event_time(row: JsonObject, *, kind: str) -> datetime | None:
    keys_by_kind = {
        "source": ("source_created_at", "captured_at"),
        "memory": ("valid_from", "last_seen_at", "updated_at", "first_seen_at", "created_at"),
        "open_loop": ("opened_at", "updated_at", "created_at"),
        "artifact": ("created_at", "generated_at"),
    }
    metadata = row.get("metadata_json")
    if kind == "source" and isinstance(metadata, dict):
        for key in ("session_date", "event_date", "date"):
            parsed = parse_event_datetime(metadata.get(key))
            if parsed is not None:
                return parsed
    if kind == "artifact" and isinstance(metadata, dict):
        generated_for = metadata.get("generated_for")
        if isinstance(generated_for, str):
            try:
                return datetime.combine(date.fromisoformat(generated_for), time.min, tzinfo=UTC)
            except ValueError:
                pass
    for key in keys_by_kind[kind]:
        parsed = parse_event_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _matches_report_scope(
    row: JsonObject,
    *,
    kind: str,
    projects: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    all_of: tuple[str, ...] | None = None,
) -> bool:
    if all_of is not None:
        from alicebot_api.vnext_derived_labels import input_admitted

        if not input_admitted(kind, row, all_of):
            return False
    elif projects:
        row_scope = source_project_scope(row) if kind == "source" else resource_project_scope(row)
        if not project_scopes_overlap(row_scope, projects):
            return False
    event_time = _row_event_time(row, kind=kind)
    return event_time is not None and window_start <= event_time < window_end


def _supports_parameters(method: object, names: Sequence[str]) -> bool:
    if not callable(method):
        return False
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return all(name in parameters for name in names)


def _windowed_rows(
    method: Callable[..., list[JsonObject]],
    *,
    kwargs: dict[str, object],
    kind: str,
    projects: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    limit: int,
    store_scope_kwargs: dict[str, object] | None = None,
    store_scope_complete: bool = False,
    all_of: tuple[str, ...] | None = None,
) -> list[JsonObject]:
    scope_kwargs = store_scope_kwargs or {}

    def select(rows: Sequence[JsonObject]) -> list[JsonObject]:
        selected: list[JsonObject] = []
        seen: set[str] = set()
        for row in rows:
            identity = str(row.get("id") or repr(sorted(row.items())))
            if identity in seen:
                continue
            seen.add(identity)
            if _matches_report_scope(
                row,
                kind=kind,
                projects=projects,
                window_start=window_start,
                window_end=window_end,
                all_of=all_of,
            ):
                selected.append(_compact_row(row))
        return selected

    if store_scope_complete:
        return select(method(limit=limit, **kwargs, **scope_kwargs))[:limit]

    fetch_limit = max(limit, 200)
    previous_count = -1
    while True:
        raw = list(method(limit=fetch_limit, **kwargs, **scope_kwargs))
        selected = select(raw)
        if len(selected) >= limit or len(raw) < fetch_limit:
            return selected[:limit]
        unique_count = len({str(row.get("id") or repr(sorted(row.items()))) for row in raw})
        if unique_count <= previous_count:
            raise VNextBrainValidationError("legacy brain store returned a non-progressing report prefix")
        if fetch_limit >= MAX_LEGACY_WINDOW_SCAN_ROWS:
            raise VNextBrainValidationError("legacy brain store could not prove complete report time window")
        previous_count = unique_count
        fetch_limit = min(fetch_limit * 2, MAX_LEGACY_WINDOW_SCAN_ROWS)


def _compact_row(row: JsonObject) -> JsonObject:
    return {key: value for key, value in row.items() if key != "deleted_at"}


def _title(row: JsonObject, fallback: str) -> str:
    value = row.get("title")
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    return fallback


def _memory_text(row: JsonObject) -> str:
    for key in ("canonical_text", "summary", "title", "memory_key"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    value = row.get("value")
    if isinstance(value, dict):
        text = " ".join(str(child) for child in value.values() if isinstance(child, (str, int, float, bool)))
        if text.strip():
            return " ".join(text.split())
    return str(row.get("id", "memory"))


def _source_text(row: JsonObject) -> str:
    metadata = row.get("metadata_json")
    if isinstance(metadata, dict):
        raw_text = metadata.get("raw_text")
        if isinstance(raw_text, str):
            return raw_text
    return _title(row, str(row.get("source_type", "source")))


def _source_ref(row: JsonObject) -> str:
    return f"[source:{row.get('id')}]"


def _memory_ref(row: JsonObject) -> str:
    return f"[memory:{row.get('id')}]"


def _artifact_ref(row: JsonObject) -> str:
    return f"[artifact:{row.get('id')}]"


def _highest_sensitivity(rows: list[JsonObject]) -> str:
    sensitivities = [str(row.get("sensitivity", "unknown")) for row in rows]
    if not sensitivities:
        return "unknown"
    return max(sensitivities, key=lambda value: SENSITIVITY_RANK.get(value, SENSITIVITY_RANK["unknown"]))


def _artifact_domain(request: BrainArtifactRequest, rows: list[JsonObject]) -> str:
    if len(request.domains) == 1:
        return derived_domain(rows, fallback=request.domains[0])
    domains = {row.get("domain") for row in rows if isinstance(row.get("domain"), str)}
    if len(domains) == 1:
        return str(next(iter(domains)))
    return derived_domain(rows, fallback="unknown")


def _section(title: str, lines: list[str]) -> str:
    body = lines or ["- No matching input was available."]
    return "\n".join([f"## {title}", *body])


def _input_summary(
    *,
    sources: list[JsonObject],
    memories: list[JsonObject],
    open_loops: list[JsonObject],
    artifacts: list[JsonObject],
) -> JsonObject:
    return {
        "source_ids": [str(row.get("id")) for row in sources],
        "memory_ids": [str(row.get("id")) for row in memories],
        "open_loop_ids": [str(row.get("id")) for row in open_loops],
        "artifact_ids": [str(row.get("id")) for row in artifacts],
        "counts": {
            "sources": len(sources),
            "memories": len(memories),
            "open_loops": len(open_loops),
            "artifacts": len(artifacts),
        },
    }


def _source_refs(rows: list[JsonObject]) -> list[str]:
    refs = [f"source:{row.get('id')}" for row in rows if row.get("id") is not None]
    return list(dict.fromkeys(refs))


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_project_scope(values: Sequence[object]) -> tuple[str, ...]:
    return normalize_project_scope(values)


def _stored_open_loop_project_id(project_scope: Sequence[str]) -> str | None:
    """Return a project id only when the single scope entry is a uuid.

    ``open_loops.project_id`` is a uuid column. A free-form name stays in
    ``metadata_json.project_scope``.
    """

    if len(project_scope) != 1:
        return None
    try:
        return str(UUID(str(project_scope[0])))
    except (ValueError, AttributeError, TypeError):
        return None


def _metadata_json(row: JsonObject) -> JsonObject:
    value = row.get("metadata_json")
    return value if isinstance(value, dict) else {}


def _brain_charter(store: VNextBrainStore) -> JsonObject | None:
    getter = getattr(store, "get_brain_charter", None)
    if not callable(getter):
        return None
    charter = cast(Callable[[], object], getter)()
    return charter if isinstance(charter, dict) else None


def _candidate_open_loop_titles(sources: list[JsonObject]) -> list[tuple[str, JsonObject]]:
    candidates: list[tuple[str, JsonObject]] = []
    pattern = re.compile(r"^\s*(?:todo|follow up|waiting on|question|ask)\s*:?\s*(.+)$", re.IGNORECASE)
    for source in sources:
        for line in _source_text(source).splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            title = " ".join(match.group(1).split())
            if title:
                candidates.append((title[:240], source))
    return candidates[:5]


class VNextBrainService:
    def __init__(self, store: VNextBrainStore) -> None:
        self.store = store

    def generate_daily_brief(self, request: BrainArtifactRequest | None = None) -> JsonObject:
        request = request or BrainArtifactRequest(agent_identity=None, )
        _validate_request(request)
        day = _parse_generated_for(request.generated_for)
        window_start, window_end = _report_window(day, days=1)
        sources, memories, open_loops, artifacts = self._load_inputs(
            request,
            window_start=window_start,
            window_end=window_end,
        )
        open_loops = [row for row in open_loops if _metadata_json(row).get("discovered_by") != "vnext_daily_brief"]
        artifacts = [
            row
            for row in artifacts
            if not (
                _metadata_json(row).get("workflow") == "daily_brief"
                and _metadata_json(row).get("generated_for") == day.isoformat()
            )
        ]
        candidate_specs = _candidate_open_loop_titles(sources)
        workflow_digest = _digest_payload(
            {
                "workflow": "daily_brief",
                "generated_for": day.isoformat(),
                "scope": {
                    "domains": request.domains,
                    "projects": project_scope_identity(request.projects),
                    "sensitivity_allowed": request.sensitivity_allowed,
                },
                "limits": {
                    "sources": request.source_limit,
                    "memories": request.memory_limit,
                    "open_loops": request.open_loop_limit,
                    "artifacts": request.artifact_limit,
                },
                "behavior": {
                    "discover_open_loops": request.discover_open_loops,
                    "generation_mode": request.generation_mode,
                    "model_route_mode": request.model_route_mode,
                    "model_provider": request.model_provider,
                    "model": request.model,
                    "model_temperature": request.model_temperature,
                    "allow_cloud_private": request.allow_cloud_private,
                    "generated_by": request.generated_by,
                    "agent_identity": request.agent_identity,
                    "brain_charter": _brain_charter(self.store),
                },
                "inputs": {
                    "sources": sources,
                    "memories": memories,
                    "open_loops": open_loops,
                    "artifacts": artifacts,
                    "candidate_open_loops": [
                        {"title": title, "source_id": source.get("id")} for title, source in candidate_specs
                    ],
                },
            }
        )
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing = cast(Callable[..., JsonObject | None], find_existing)(
                artifact_type="daily_brief",
                workflow="daily_brief",
                digest=workflow_digest,
                scope_projects=request.projects or None,
            )
            if existing is not None:
                return existing
        candidate_open_loops = self._create_candidate_open_loops(
            request,
            candidate_specs,
            workflow_digest=workflow_digest,
        )
        all_rows = [*sources, *memories, *open_loops, *artifacts]
        content = self._daily_markdown(
            generated_for=day.isoformat(),
            sources=sources,
            memories=memories,
            open_loops=[*open_loops, *candidate_open_loops],
            artifacts=artifacts,
        )
        source_refs = _source_refs(sources)
        report_project_scope = _canonical_project_scope(
            request.projects
            or tuple(project for row in sources for project in source_project_scope(row))
            + tuple(project for row in [*memories, *open_loops] for project in resource_project_scope(row))
        )
        metadata = {
            **request.metadata_json,
            "workflow": "daily_brief",
            "generated_by": request.generated_by,
            "agent_identity": request.agent_identity,
            "agent_id": request.agent_identity.get("agent_id") if isinstance(request.agent_identity, dict) else None,
            "agent_run_id": request.agent_identity.get("agent_run_id")
            if isinstance(request.agent_identity, dict)
            else None,
            "scheduler_run_id": request.run_id if request.generated_by == "scheduler" else None,
            "trace_id": request.trace_id,
            "policy_decision": request.policy_decision,
            "generated_for": day.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "project_scope": list(report_project_scope),
            "source_refs": source_refs,
            "input_summary": _input_summary(
                sources=sources,
                memories=memories,
                open_loops=open_loops,
                artifacts=artifacts,
            ),
            "candidate_open_loop_ids": [str(row.get("id")) for row in candidate_open_loops],
            "generation_mode": request.generation_mode,
            "workflow_digest": workflow_digest,
        }
        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if request.generation_mode == "model_backed":
            model_artifact = self._model_backed_artifact(
                request=request,
                workflow_type="daily_brief",
                title=f"Daily Brief - {day.isoformat()}",
                deterministic_markdown=content,
                context_rows=all_rows,
                source_refs=source_refs,
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            metadata = {**metadata, **model_artifact.metadata}
        metadata = with_derived_from(
            metadata,
            {"sources": sources, "memories": memories, "open_loops": open_loops, "artifacts": artifacts},
        )
        artifact_payload: JsonObject = {
            "artifact_type": "daily_brief",
            "title": f"Daily Brief - {day.isoformat()}",
            "content_markdown": content,
            "status": "needs_review",
            "domain": _artifact_domain(request, all_rows),
            "sensitivity": _highest_sensitivity(all_rows),
            "generated_by": request.generated_by if request.generated_by != "system" else "vnext_daily_brief",
            "prompt_hash": prompt_hash,
            "model_info_json": model_info_json,
            "metadata_json": metadata,
        }
        upsert_artifact = getattr(self.store, "upsert_artifact_by_workflow_digest", None)
        if callable(upsert_artifact):
            artifact = cast(Callable[..., JsonObject], upsert_artifact)(
                artifact_payload,
                workflow="daily_brief",
                digest=workflow_digest,
                actor_type=request.generated_by,
            )
        else:
            artifact = self.store.create_artifact(
                artifact_payload,
                actor_type=request.generated_by,
            )
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
                "workflow": "daily_brief",
                "generated_for": day.isoformat(),
                "artifact_type": "daily_brief",
                "candidate_open_loop_count": len(candidate_open_loops),
                "agent_identity": request.agent_identity,
                "policy_decision": request.policy_decision,
                "generation_mode": request.generation_mode,
            },
        )
        if request.generated_by == "agent" and request.actor_id is not None:
            append_event(
                self.store,
                event_type="agent.artifact_generated",
                actor_type="agent",
                actor_id=request.actor_id,
                target_type="artifact",
                target_id=str(artifact["id"]),
                trace_id=request.trace_id,
                run_id=request.run_id,
                payload={"workflow": "daily_brief", "agent_identity": request.agent_identity},
            )
        return artifact

    def generate_weekly_synthesis(self, request: BrainArtifactRequest | None = None) -> JsonObject:
        request = request or BrainArtifactRequest(agent_identity=None, )
        _validate_request(request)
        day = _parse_generated_for(request.generated_for)
        week_label = _iso_week_label(day)
        window_start, window_end = _weekly_report_window(day)
        sources, memories, open_loops, artifacts = self._load_inputs(
            request,
            window_start=window_start,
            window_end=window_end,
        )
        memories = [row for row in memories if _metadata_json(row).get("discovered_by") != "vnext_weekly_synthesis"]
        artifacts = [
            row
            for row in artifacts
            if not (
                _metadata_json(row).get("workflow") == "weekly_synthesis"
                and _metadata_json(row).get("week") == week_label
            )
        ]
        workflow_digest = _digest_payload(
            {
                "workflow": "weekly_synthesis",
                "generated_for": day.isoformat(),
                "week": week_label,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "scope": {
                    "domains": request.domains,
                    "projects": project_scope_identity(request.projects),
                    "sensitivity_allowed": request.sensitivity_allowed,
                },
                "limits": {
                    "sources": request.source_limit,
                    "memories": request.memory_limit,
                    "open_loops": request.open_loop_limit,
                    "artifacts": request.artifact_limit,
                },
                "behavior": {
                    "create_candidate_memories": request.create_candidate_memories,
                    "generation_mode": request.generation_mode,
                    "model_route_mode": request.model_route_mode,
                    "model_provider": request.model_provider,
                    "model": request.model,
                    "model_temperature": request.model_temperature,
                    "allow_cloud_private": request.allow_cloud_private,
                    "generated_by": request.generated_by,
                    "agent_identity": request.agent_identity,
                    "brain_charter": _brain_charter(self.store),
                },
                "inputs": {
                    "sources": sources,
                    "memories": memories,
                    "open_loops": open_loops,
                    "artifacts": artifacts,
                },
            }
        )
        find_existing = getattr(self.store, "find_artifact_by_workflow_digest", None)
        if callable(find_existing):
            existing = cast(Callable[..., JsonObject | None], find_existing)(
                artifact_type="weekly_synthesis",
                workflow="weekly_synthesis",
                digest=workflow_digest,
                scope_projects=request.projects or None,
            )
            if existing is not None:
                return existing
        candidate_memories = self._create_weekly_candidate_memories(
            request,
            sources,
            memories,
            open_loops,
            workflow_digest=workflow_digest,
        )
        all_rows = [*sources, *memories, *open_loops, *artifacts]
        content = self._weekly_markdown(
            week_label=week_label,
            sources=sources,
            memories=memories,
            open_loops=open_loops,
            artifacts=artifacts,
            candidate_memories=candidate_memories,
        )
        source_refs = _source_refs(sources)
        report_project_scope = _canonical_project_scope(
            request.projects
            or tuple(project for row in sources for project in source_project_scope(row))
            + tuple(project for row in [*memories, *open_loops] for project in resource_project_scope(row))
        )
        metadata = {
            **request.metadata_json,
            "workflow": "weekly_synthesis",
            "generated_by": request.generated_by,
            "agent_identity": request.agent_identity,
            "agent_id": request.agent_identity.get("agent_id") if isinstance(request.agent_identity, dict) else None,
            "agent_run_id": request.agent_identity.get("agent_run_id")
            if isinstance(request.agent_identity, dict)
            else None,
            "scheduler_run_id": request.run_id if request.generated_by == "scheduler" else None,
            "trace_id": request.trace_id,
            "policy_decision": request.policy_decision,
            "generated_for": day.isoformat(),
            "week": week_label,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "project_scope": list(report_project_scope),
            "source_refs": source_refs,
            "input_summary": _input_summary(
                sources=sources,
                memories=memories,
                open_loops=open_loops,
                artifacts=artifacts,
            ),
            "candidate_memory_ids": [str(row.get("id")) for row in candidate_memories],
            "generation_mode": request.generation_mode,
            "workflow_digest": workflow_digest,
        }
        prompt_hash: str | None = None
        model_info_json: JsonObject | None = None
        if request.generation_mode == "model_backed":
            model_artifact = self._model_backed_artifact(
                request=request,
                workflow_type="weekly_synthesis",
                title=f"Weekly Synthesis - {week_label}",
                deterministic_markdown=content,
                context_rows=all_rows,
                source_refs=source_refs,
            )
            content = model_artifact.content_markdown
            prompt_hash = model_artifact.prompt_hash
            model_info_json = model_artifact.model_info
            metadata = {**metadata, **model_artifact.metadata}
        metadata = with_derived_from(
            metadata,
            {"sources": sources, "memories": memories, "open_loops": open_loops, "artifacts": artifacts},
        )
        artifact_payload: JsonObject = {
            "artifact_type": "weekly_synthesis",
            "title": f"Weekly Synthesis - {week_label}",
            "content_markdown": content,
            "status": "needs_review",
            "domain": _artifact_domain(request, all_rows),
            "sensitivity": _highest_sensitivity(all_rows),
            "generated_by": request.generated_by if request.generated_by != "system" else "vnext_weekly_synthesis",
            "prompt_hash": prompt_hash,
            "model_info_json": model_info_json,
            "metadata_json": metadata,
        }
        upsert_artifact = getattr(
            self.store,
            "upsert_artifact_by_workflow_digest",
            None,
        )
        if callable(upsert_artifact):
            artifact = cast(Callable[..., JsonObject], upsert_artifact)(
                artifact_payload,
                workflow="weekly_synthesis",
                digest=workflow_digest,
                actor_type=request.generated_by,
            )
        else:
            artifact = self.store.create_artifact(
                artifact_payload,
                actor_type=request.generated_by,
            )
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
                "workflow": "weekly_synthesis",
                "generated_for": day.isoformat(),
                "week": week_label,
                "artifact_type": "weekly_synthesis",
                "candidate_memory_count": len(candidate_memories),
                "agent_identity": request.agent_identity,
                "policy_decision": request.policy_decision,
                "generation_mode": request.generation_mode,
            },
        )
        if request.generated_by == "agent" and request.actor_id is not None:
            append_event(
                self.store,
                event_type="agent.artifact_generated",
                actor_type="agent",
                actor_id=request.actor_id,
                target_type="artifact",
                target_id=str(artifact["id"]),
                trace_id=request.trace_id,
                run_id=request.run_id,
                payload={"workflow": "weekly_synthesis", "agent_identity": request.agent_identity},
            )
        return artifact

    def _model_backed_artifact(
        self,
        *,
        request: BrainArtifactRequest,
        workflow_type: str,
        title: str,
        deterministic_markdown: str,
        context_rows: list[JsonObject],
        source_refs: list[str],
    ) -> ModelBackedArtifact:
        route = resolve_model_route(
            ModelRoutingRequest(
                workflow_type=workflow_type,
                generation_mode=request.generation_mode,
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
        return build_model_backed_artifact(
            ModelBackedRequest(
                workflow_type=workflow_type,
                title=title,
                deterministic_markdown=deterministic_markdown,
                context_rows=tuple(context_rows),
                source_refs=tuple(source_refs),
                trace_id=request.trace_id,
                route=route,
                temperature=request.model_temperature,
                config={
                    "agent_id": request.actor_id,
                    "generated_by": request.generated_by,
                },
            )
        )

    def _load_inputs(
        self,
        request: BrainArtifactRequest,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[JsonObject], list[JsonObject], list[JsonObject], list[JsonObject]]:
        domains = _allowed_domains(request)
        sensitivity_allowed = _allowed_sensitivity(request)
        from alicebot_api.vnext_derived_labels import locked_projects

        all_of = locked_projects(request.agent_identity, request.projects)
        inclusive_window_end = window_end - timedelta(microseconds=1)
        source_scope_names = ("scope_projects", "scope_window_start", "scope_window_end")
        source_scope_supported = _supports_parameters(self.store.search_sources, source_scope_names)
        sources = _windowed_rows(
            self.store.search_sources,
            kwargs={
                "query": "",
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            kind="source",
            projects=request.projects,
            window_start=window_start,
            window_end=window_end,
            limit=request.source_limit,
            store_scope_kwargs={
                "scope_projects": request.projects,
                "scope_window_start": window_start,
                "scope_window_end": inclusive_window_end,
            }
            if source_scope_supported
            else None,
            store_scope_complete=source_scope_supported and all_of is None,
            all_of=all_of,
        )
        memory_store_scope: dict[str, object] | None = (
            {"projects": request.projects} if _supports_parameters(self.store.search_memories, ("projects",)) else None
        )
        memories = _windowed_rows(
            self.store.search_memories,
            kwargs={
                "query": "",
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            kind="memory",
            projects=request.projects,
            window_start=window_start,
            window_end=window_end,
            limit=request.memory_limit,
            store_scope_kwargs=memory_store_scope,
            all_of=all_of,
        )
        open_loop_scope_names = ("scope_projects", "scope_window_start", "scope_window_end")
        open_loop_scope_supported = _supports_parameters(
            self.store.list_open_loops,
            open_loop_scope_names,
        )
        open_loops = _windowed_rows(
            self.store.list_open_loops,
            kwargs={
                "status": "open",
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            kind="open_loop",
            projects=request.projects,
            window_start=window_start,
            window_end=window_end,
            limit=request.open_loop_limit,
            store_scope_kwargs={
                "scope_projects": request.projects,
                "scope_window_start": window_start,
                "scope_window_end": inclusive_window_end,
            }
            if open_loop_scope_supported
            else None,
            store_scope_complete=open_loop_scope_supported and all_of is None,
            all_of=all_of,
        )
        artifact_store_scope: dict[str, object] | None = (
            {"scope_projects": request.projects}
            if _supports_parameters(self.store.list_artifacts, ("scope_projects",))
            else None
        )
        artifacts = _windowed_rows(
            self.store.list_artifacts,
            kwargs={
                "artifact_type": None,
                "domains": domains,
                "sensitivity_allowed": sensitivity_allowed,
            },
            kind="artifact",
            projects=request.projects,
            window_start=window_start,
            window_end=window_end,
            limit=request.artifact_limit,
            store_scope_kwargs=artifact_store_scope,
            all_of=all_of,
        )
        from alicebot_api.vnext_label_guard import admit_loaded

        sources = admit_loaded(
            self.store, kind="source", rows=sources, domains=domains, sensitivity_allowed=sensitivity_allowed, projects=request.projects,
            all_of=all_of,
        )
        memories = admit_loaded(
            self.store, kind="memory", rows=memories, domains=domains, sensitivity_allowed=sensitivity_allowed, projects=request.projects,
            all_of=all_of,
        )
        open_loops = admit_loaded(
            self.store, kind="open_loop", rows=open_loops, domains=domains, sensitivity_allowed=sensitivity_allowed, projects=request.projects,
            all_of=all_of,
        )
        artifacts = admit_loaded(
            self.store, kind="artifact", rows=artifacts, domains=domains, sensitivity_allowed=sensitivity_allowed, projects=request.projects,
            all_of=all_of,
        )
        return sources, memories, open_loops, artifacts

    def _create_candidate_open_loops(
        self,
        request: BrainArtifactRequest,
        candidates: list[tuple[str, JsonObject]],
        *,
        workflow_digest: str,
    ) -> list[JsonObject]:
        if not request.discover_open_loops:
            return []
        created: list[JsonObject] = []
        upsert_open_loop = getattr(self.store, "upsert_open_loop_by_automation_digest", None)
        for title, source in candidates:
            source_scope = _canonical_project_scope(source_project_scope(source))
            project_scope = source_scope or _canonical_project_scope(request.projects)
            automation_digest = _digest_payload(
                {
                    "workflow_digest": workflow_digest,
                    "source_id": source.get("id"),
                    "title": title,
                    "project_scope": project_scope_identity(project_scope),
                }
            )
            loop_payload: JsonObject = {
                "title": title,
                "description": f"Candidate open loop discovered in {_title(source, 'source')}.",
                "status": "open",
                "priority": "normal",
                "source_id": str(source["id"]),
                "project_id": _stored_open_loop_project_id(project_scope),
                "domain": source.get("domain", "unknown"),
                "sensitivity": source.get("sensitivity", "unknown"),
                "metadata_json": {
                    "candidate": True,
                    "discovered_by": "vnext_daily_brief",
                    "source_id": str(source["id"]),
                    "project_scope": list(project_scope),
                    "project_floor": list(source_project_scope(source)),
                    "automation_digest": automation_digest,
                    "workflow_digest": workflow_digest,
                },
            }
            loop_payload["metadata_json"] = with_derived_from(
                cast(dict, loop_payload["metadata_json"]),
                {"sources": [source]},
            )
            if callable(upsert_open_loop):
                loop = cast(Callable[..., JsonObject], upsert_open_loop)(
                    loop_payload,
                    digest=automation_digest,
                    actor_type=request.generated_by,
                )
            else:
                loop = self.store.create_open_loop(
                    loop_payload,
                    actor_type=request.generated_by,
                )
            created.append(loop)
        return created

    def _create_weekly_candidate_memories(
        self,
        request: BrainArtifactRequest,
        sources: list[JsonObject],
        memories: list[JsonObject],
        open_loops: list[JsonObject],
        *,
        workflow_digest: str,
    ) -> list[JsonObject]:
        if not request.create_candidate_memories or not (sources or memories or open_loops):
            return []
        insight = self._weekly_pattern_line(sources=sources, memories=memories, open_loops=open_loops)
        project_scope = _canonical_project_scope(request.projects) or tuple(
            dict.fromkeys(
                (
                    *(project for row in sources for project in source_project_scope(row)),
                    *(project for row in [*memories, *open_loops] for project in resource_project_scope(row)),
                )
            )
        )
        memory_payload: JsonObject = {
            "memory_type": "artifact_summary",
            "memory_key": f"weekly_synthesis.{workflow_digest}",
            "value": {"insight": insight},
            "status": "candidate",
            "confidence": 0.6,
            "canonical_text": insight,
            "summary": insight,
            "domain": _artifact_domain(request, [*sources, *memories, *open_loops]),
            "sensitivity": _highest_sensitivity([*sources, *memories, *open_loops]),
            "metadata_json": {
                "candidate": True,
                "discovered_by": "vnext_weekly_synthesis",
                "input_summary": _input_summary(sources=sources, memories=memories, open_loops=open_loops, artifacts=[]),
                "generated_by": request.generated_by,
                "agent_identity": request.agent_identity,
                "scheduler_run_id": request.run_id if request.generated_by == "scheduler" else None,
                "trace_id": request.trace_id,
                "project_scope": list(project_scope),
                "workflow_digest": workflow_digest,
            },
        }
        memory_payload["metadata_json"] = with_derived_from(
            cast(dict, memory_payload["metadata_json"]),
            {"sources": sources, "memories": memories, "open_loops": open_loops},
        )
        upsert_memory = getattr(self.store, "upsert_memory_by_key", None)
        if callable(upsert_memory):
            memory = cast(Callable[..., JsonObject], upsert_memory)(
                memory_payload,
                actor_type=request.generated_by,
            )
        else:
            memory = self.store.create_memory(
                memory_payload,
                actor_type=request.generated_by,
            )
        return [memory]

    def _daily_markdown(
        self,
        *,
        generated_for: str,
        sources: list[JsonObject],
        memories: list[JsonObject],
        open_loops: list[JsonObject],
        artifacts: list[JsonObject],
    ) -> str:
        source_lines = [
            f"- Fact: {_title(source, 'Untitled source')} was captured for review. {_source_ref(source)}"
            for source in sources[:3]
        ]
        memory_lines = [f"- Fact: {_memory_text(memory)} {_memory_ref(memory)}" for memory in memories[:3]]
        loop_lines = [f"- Action: {_title(loop, 'Open loop')} [open_loop:{loop.get('id')}]" for loop in open_loops[:5]]
        connection_lines = self._connection_lines(sources=sources, memories=memories, artifacts=artifacts)
        sources_used = [f"- {_source_ref(source)} {_title(source, 'Untitled source')}" for source in sources] or [
            "- No source records were available."
        ]
        return "\n\n".join(
            [
                f"# Daily Brief - {generated_for}",
                _section("1. Executive Summary", source_lines or memory_lines),
                _section("2. Project Status", memory_lines),
                _section("3. Open Loops", loop_lines),
                _section("4. New Connections", connection_lines),
                _section(
                    "5. Contradictions / Tensions",
                    ["- Inference: No contradiction scan is enabled in this Sprint 5 scaffold."],
                ),
                _section("6. Emerging Pattern", [f"- Inference: {self._daily_pattern_line(sources, memories)}"]),
                _section("7. Suggested Focus", self._suggested_focus_lines(open_loops=open_loops, memories=memories)),
                _section("8. People to Follow Up With", ["- Inference: No person-specific follow-up was detected."]),
                _section("9. Sources Used", sources_used),
            ]
        )

    def _weekly_markdown(
        self,
        *,
        week_label: str,
        sources: list[JsonObject],
        memories: list[JsonObject],
        open_loops: list[JsonObject],
        artifacts: list[JsonObject],
        candidate_memories: list[JsonObject],
    ) -> str:
        moved = [f"- Fact: {_memory_text(memory)} {_memory_ref(memory)}" for memory in memories[:5]] or [
            f"- Fact: {_title(source, 'Untitled source')} entered the evidence archive. {_source_ref(source)}"
            for source in sources[:5]
        ]
        blocked = [
            f"- Action: {_title(loop, 'Open loop')} remains open. [open_loop:{loop.get('id')}]"
            for loop in open_loops[:5]
        ]
        links = self._weekly_link_lines(sources=sources, memories=memories, artifacts=artifacts)
        candidate_lines = [
            f"- Candidate memory: {_memory_text(memory)} {_memory_ref(memory)}" for memory in candidate_memories
        ] or ["- No candidate memory was created because no meaningful weekly input was available."]
        sources_used = [f"- {_source_ref(source)} {_title(source, 'Untitled source')}" for source in sources] or [
            "- No source records were available."
        ]
        return "\n\n".join(
            [
                f"# Weekly Synthesis - {week_label}",
                _section("1. What moved forward", moved),
                _section("2. What did not move", blocked or ["- Inference: No unresolved open loops were selected."]),
                _section(
                    "3. Recurring patterns",
                    [
                        f"- Inference: {self._weekly_pattern_line(sources=sources, memories=memories, open_loops=open_loops)}"
                    ],
                ),
                _section(
                    "4. Contradictions or changed assumptions",
                    ["- Inference: No contradiction scan is enabled in this Sprint 5 scaffold."],
                ),
                _section("5. Emerging thesis", candidate_lines),
                _section(
                    "6. Highest-leverage next actions",
                    self._suggested_focus_lines(open_loops=open_loops, memories=memories),
                ),
                _section(
                    "7. What to stop doing / thinking about",
                    ["- Inference: Stop treating generated synthesis as durable memory until reviewed."],
                ),
                _section("Project / Person / Concept Links", links),
                _section("8. Sources Used", sources_used),
            ]
        )

    @staticmethod
    def _connection_lines(
        *,
        sources: list[JsonObject],
        memories: list[JsonObject],
        artifacts: list[JsonObject],
    ) -> list[str]:
        lines: list[str] = []
        if sources and memories:
            lines.append(
                f"- Inference: {_title(sources[0], 'Source')} may relate to {_memory_text(memories[0])}. "
                f"{_source_ref(sources[0])} {_memory_ref(memories[0])}"
            )
        if artifacts and sources:
            lines.append(
                f"- Inference: {_title(artifacts[0], 'Artifact')} should be reviewed against new source "
                f"{_title(sources[0], 'Source')}. {_artifact_ref(artifacts[0])} {_source_ref(sources[0])}"
            )
        return lines or ["- Inference: No non-obvious connection was detected from the selected inputs."]

    @staticmethod
    def _weekly_link_lines(
        *,
        sources: list[JsonObject],
        memories: list[JsonObject],
        artifacts: list[JsonObject],
    ) -> list[str]:
        rows = [*memories, *sources, *artifacts]
        labels: list[str] = []
        for row in rows[:3]:
            if "canonical_text" in row:
                labels.append(f"- Link: {_memory_text(row)} {_memory_ref(row)}")
            elif "content_hash" in row:
                labels.append(f"- Link: {_title(row, 'Source')} {_source_ref(row)}")
            else:
                labels.append(f"- Link: {_title(row, 'Artifact')} {_artifact_ref(row)}")
        if labels:
            return labels
        return ["- No project/person/concept links were available from the selected inputs."]

    @staticmethod
    def _daily_pattern_line(sources: list[JsonObject], memories: list[JsonObject]) -> str:
        if len(sources) + len(memories) >= 2:
            return "Recent inputs cluster around the selected domain and should be reviewed together."
        return "No strong pattern found from the selected daily inputs."

    @staticmethod
    def _weekly_pattern_line(
        *,
        sources: list[JsonObject],
        memories: list[JsonObject],
        open_loops: list[JsonObject],
    ) -> str:
        if len(open_loops) >= 2:
            return "Multiple open loops remained active across the weekly window."
        if len(sources) + len(memories) >= 2:
            return "Evidence and memories repeated across the weekly window."
        return "No strong pattern found from the selected weekly inputs."

    @staticmethod
    def _suggested_focus_lines(*, open_loops: list[JsonObject], memories: list[JsonObject]) -> list[str]:
        if open_loops:
            return [f"- Action: Resolve or clarify {_title(open_loops[0], 'the highest-priority open loop')}."]
        if memories:
            return [f"- Action: Review {_memory_text(memories[0])} for promotion or follow-up."]
        return ["- Action: Capture more evidence before making a planning decision."]


__all__ = [
    "BrainArtifactRequest",
    "DEFAULT_ARTIFACT_LIMIT",
    "DEFAULT_BRAIN_LIMIT",
    "DEFAULT_SENSITIVITY_ALLOWED",
    "VNextBrainService",
    "VNextBrainStore",
    "VNextBrainValidationError",
]
