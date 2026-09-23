"""MCP argument parsers and deterministic rendering helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypedDict, cast
from uuid import UUID

from alicebot_api.recall_framing import frame_rendered_block, quote_stored_note
from alicebot_api.contracts import (
    CONTINUITY_BRIEF_TYPE_ORDER,
    CONTINUITY_CORRECTION_ACTIONS,
    DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    DEFAULT_TASK_BRIEF_TOKEN_BUDGET,
    MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_TASK_BRIEF_TOKEN_BUDGET,
    ContinuityBriefRequestInput,
    ContinuityRecallQueryInput,
    TaskBriefCompileRequestInput,
    TaskBriefingStrategy,
)
from alicebot_api.store import JsonObject
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_memory_commit import VNEXT_MEMORY_TYPES
from alicebot_api.vnext_retrieval import (
    BUDGET_STRATEGIES,
    BUDGET_STRATEGY_BALANCED,
    CONTEXT_DEPTHS,
    CONTEXT_DEPTH_LOW,
)

from .types import (
    MCPToolError,
    _MODEL_GENERATION_MODES,
    _MODEL_ROUTE_MODES,
    _REVIEW_APPLY_ACTION_ALIASES,
    _REVIEW_APPLY_ACTION_CHOICES,
    _REVIEW_APPLY_TO_CORRECTION_ACTION,
    _REVIEW_STATUS_ALIASES,
    _REVIEW_STATUS_CHOICES,
)


def _normalize_arguments(arguments: object) -> Mapping[str, object]:
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise MCPToolError("tool arguments must be a JSON object")
    return arguments


def _parse_optional_text(arguments: Mapping[str, object], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPToolError(f"{key} must be a string")
    normalized = " ".join(value.split()).strip()
    if normalized == "":
        return None
    return normalized


def _parse_required_text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise MCPToolError(f"{key} is required and must be a string")
    normalized = " ".join(value.split()).strip()
    if normalized == "":
        raise MCPToolError(f"{key} must not be empty")
    return normalized


def _parse_required_document_text(arguments: Mapping[str, object], key: str) -> str:
    """Parse a field whose internal line structure is part of its meaning.

    ``_parse_required_text`` collapses every run of whitespace to a single space.
    That is right for a title, an id or an enum, and destructive for a document.

    Found 2026-08-16 against the published v0.15.5 by capturing a markdown vault
    through ``alice_capture`` and reading the rows back out of SQLite: a file with
    17 newlines was stored with 0. ``chunk_text`` splits on blank lines, so a
    flattened document is a single paragraph and gets cut on word count at
    ``max_chars`` instead, straddling unrelated notes. That is what made an
    imported quotes library unsearchable by its own quotes.

    It also broke the tool's stated contract: ``alice_capture`` promises text is
    "kept verbatim with provenance".

    Line endings are normalised and the ends are trimmed, exactly as
    ``vnext_capture.normalize_text`` does. Nothing inside is touched, so
    indentation, code blocks and list structure survive.
    """

    value = arguments.get(key)
    if not isinstance(value, str):
        raise MCPToolError(f"{key} is required and must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized == "":
        raise MCPToolError(f"{key} must not be empty")
    return normalized


def _parse_optional_uuid(arguments: Mapping[str, object], key: str) -> UUID | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPToolError(f"{key} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise MCPToolError(f"{key} must be a valid UUID") from exc


def _parse_required_uuid(arguments: Mapping[str, object], key: str) -> UUID:
    value = _parse_optional_uuid(arguments, key)
    if value is None:
        raise MCPToolError(f"{key} is required and must be a UUID string")
    return value


def _parse_optional_datetime(arguments: Mapping[str, object], key: str) -> datetime | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPToolError(f"{key} must be an ISO-8601 datetime string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MCPToolError(f"{key} must be an ISO-8601 datetime string") from exc


def _parse_int(
    arguments: Mapping[str, object],
    *,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool):
        raise MCPToolError(f"{key} must be an integer")

    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            raise MCPToolError(f"{key} must be an integer")
        try:
            parsed = int(stripped)
        except ValueError as exc:
            raise MCPToolError(f"{key} must be an integer") from exc
    else:
        raise MCPToolError(f"{key} must be an integer")

    if parsed < minimum or parsed > maximum:
        raise MCPToolError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _parse_optional_json_object(arguments: Mapping[str, object], key: str) -> JsonObject | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MCPToolError(f"{key} must be a JSON object")
    return value


def _parse_string_list(arguments: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = arguments.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = " ".join(value.split()).strip()
        return (normalized,) if normalized else ()
    if not isinstance(value, list):
        raise MCPToolError(f"{key} must be a string array")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MCPToolError(f"{key} must be a string array")
        normalized = " ".join(item.split()).strip()
        if normalized:
            output.append(normalized)
    return tuple(output)


def _parse_memory_types(arguments: Mapping[str, object], *, key: str = "memory_types") -> tuple[str, ...]:
    """Parse an optional typed-memory filter, validating against the canonical enum."""
    values = _parse_string_list(arguments, key)
    invalid = sorted({value for value in values if value not in VNEXT_MEMORY_TYPES})
    if invalid:
        raise MCPToolError(
            f"{key} contains unsupported values: {', '.join(invalid)}; "
            f"allowed values are: {', '.join(VNEXT_MEMORY_TYPES)}"
        )
    return values


class _RetrievalFilterKwargs(TypedDict, total=False):
    memory_types: tuple[str, ...]
    projects: tuple[str, ...]
    created_by_agent_ids: tuple[str, ...]
    scope_thread_id: str
    scope_task_id: str
    scope_people: tuple[str, ...]
    scope_window_start: datetime
    scope_window_end: datetime


def _retrieval_filter_kwargs(arguments: Mapping[str, object]) -> _RetrievalFilterKwargs:
    """Optional typed/scoped retrieval filters, passed through only when set.

    The distributed Hermes contract historically exposed singular
    thread/task/project/person and absolute time bounds. Keep those as hard
    ranked-store predicates alongside the newer array filters; when a filter
    is not requested the argument is omitted and service defaults apply.
    """
    kwargs: _RetrievalFilterKwargs = {}
    memory_types = _parse_memory_types(arguments)
    if memory_types:
        kwargs["memory_types"] = memory_types
    projects = list(_parse_string_list(arguments, "projects"))
    singular_project = _parse_optional_text(arguments, "project")
    if singular_project is not None:
        projects.append(singular_project)
    projects = list(dict.fromkeys(projects))
    if projects:
        kwargs["projects"] = tuple(projects)
    created_by_agents = _parse_string_list(arguments, "created_by_agents")
    if created_by_agents:
        kwargs["created_by_agent_ids"] = created_by_agents
    thread_id = _parse_optional_uuid(arguments, "thread_id")
    if thread_id is not None:
        kwargs["scope_thread_id"] = str(thread_id)
    task_id = _parse_optional_uuid(arguments, "task_id")
    if task_id is not None:
        kwargs["scope_task_id"] = str(task_id)
    people = list(_parse_string_list(arguments, "people"))
    singular_person = _parse_optional_text(arguments, "person")
    if singular_person is not None:
        people.append(singular_person)
    if people:
        kwargs["scope_people"] = tuple(dict.fromkeys(person.casefold() for person in people))
    since = _parse_optional_datetime(arguments, "since")
    until = _parse_optional_datetime(arguments, "until")
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if since is not None and until is not None and since > until:
        raise MCPToolError("since must be at or before until")
    if since is not None:
        kwargs["scope_window_start"] = since
    if until is not None:
        kwargs["scope_window_end"] = until
    return kwargs


def _parse_task_brief_request(
    arguments: Mapping[str, object], *, mode_key: str = "mode"
) -> TaskBriefCompileRequestInput:
    mode_value = arguments.get(mode_key)
    if not isinstance(mode_value, str):
        raise MCPToolError(f"{mode_key} is required and must be a string")
    normalized_mode = mode_value.strip()
    if normalized_mode == "":
        raise MCPToolError(f"{mode_key} must not be empty")
    token_budget = arguments.get("token_budget")
    parsed_token_budget: int | None
    if token_budget is None:
        parsed_token_budget = None
    else:
        parsed_token_budget = _parse_int(
            arguments,
            key="token_budget",
            default=DEFAULT_TASK_BRIEF_TOKEN_BUDGET,
            minimum=1,
            maximum=MAX_TASK_BRIEF_TOKEN_BUDGET,
        )
    return TaskBriefCompileRequestInput(
        mode=normalized_mode,  # type: ignore[arg-type]
        query=_parse_optional_text(arguments, "query"),
        thread_id=_parse_optional_uuid(arguments, "thread_id"),
        task_id=_parse_optional_uuid(arguments, "task_id"),
        project=_parse_optional_text(arguments, "project"),
        person=_parse_optional_text(arguments, "person"),
        since=_parse_optional_datetime(arguments, "since"),
        until=_parse_optional_datetime(arguments, "until"),
        include_non_promotable_facts=_parse_bool(
            arguments,
            key="include_non_promotable_facts",
            default=False,
        ),
        provider_strategy=_parse_optional_text(arguments, "provider_strategy"),
        briefing_strategy=cast(
            TaskBriefingStrategy | None,
            _parse_optional_text(arguments, "briefing_strategy"),
        ),
        token_budget=parsed_token_budget,
    )


def _parse_continuity_brief_request(arguments: Mapping[str, object]) -> ContinuityBriefRequestInput:
    brief_type_value = arguments.get("brief_type", "general")
    if not isinstance(brief_type_value, str) or brief_type_value.strip() == "":
        raise MCPToolError("brief_type must be a string")
    brief_type = brief_type_value.strip()
    if brief_type not in CONTINUITY_BRIEF_TYPE_ORDER:
        raise MCPToolError("brief_type must be one of: " + ", ".join(CONTINUITY_BRIEF_TYPE_ORDER))
    return ContinuityBriefRequestInput(
        brief_type=brief_type,  # type: ignore[arg-type]
        query=_parse_optional_text(arguments, "query"),
        thread_id=_parse_optional_uuid(arguments, "thread_id"),
        task_id=_parse_optional_uuid(arguments, "task_id"),
        project=_parse_optional_text(arguments, "project"),
        person=_parse_optional_text(arguments, "person"),
        since=_parse_optional_datetime(arguments, "since"),
        until=_parse_optional_datetime(arguments, "until"),
        max_relevant_facts=_parse_int(
            arguments,
            key="max_relevant_facts",
            default=DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
        ),
        max_recent_changes=_parse_int(
            arguments,
            key="max_recent_changes",
            default=DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        ),
        max_open_loops=_parse_int(
            arguments,
            key="max_open_loops",
            default=DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        ),
        max_conflicts=_parse_int(
            arguments,
            key="max_conflicts",
            default=DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
        ),
        max_timeline_highlights=_parse_int(
            arguments,
            key="max_timeline_highlights",
            default=DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
        ),
        include_non_promotable_facts=_parse_bool(
            arguments,
            key="include_non_promotable_facts",
            default=False,
        ),
    )


def _parse_optional_float(arguments: Mapping[str, object], key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise MCPToolError(f"{key} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise MCPToolError(f"{key} must be a number") from exc
    raise MCPToolError(f"{key} must be a number")


def _parse_bool(arguments: Mapping[str, object], *, key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise MCPToolError(f"{key} must be a boolean")


def _parse_optional_bool(arguments: Mapping[str, object], *, key: str) -> bool | None:
    """Tri-state boolean: absent (or null) means "caller did not specify".

    Retrieval flags such as ``include_sources`` treat None as "let the
    context_depth tier decide", so absence must stay distinguishable from an
    explicit false.
    """
    if arguments.get(key) is None:
        return None
    return _parse_bool(arguments, key=key)


def _parse_context_pack_tuning(arguments: Mapping[str, object]) -> tuple[str, str]:
    """Validated (context_depth, budget_strategy) pair with tier defaults."""
    depth = _parse_optional_text(arguments, "context_depth") or CONTEXT_DEPTH_LOW
    if depth not in CONTEXT_DEPTHS:
        raise MCPToolError(f"context_depth must be one of: {', '.join(CONTEXT_DEPTHS)}")
    strategy = _parse_optional_text(arguments, "budget_strategy") or BUDGET_STRATEGY_BALANCED
    if strategy not in BUDGET_STRATEGIES:
        raise MCPToolError(f"budget_strategy must be one of: {', '.join(BUDGET_STRATEGIES)}")
    return depth, strategy


class _ModelGenerationKwargs(TypedDict):
    generation_mode: str
    model_route_mode: str | None
    model_provider: str | None
    model: str | None
    model_temperature: float
    allow_cloud_private: bool


def _parse_model_generation_kwargs(arguments: Mapping[str, object]) -> _ModelGenerationKwargs:
    generation_mode = _parse_optional_text(arguments, "generation_mode") or "deterministic"
    if generation_mode not in _MODEL_GENERATION_MODES:
        raise MCPToolError("generation_mode must be deterministic or model_backed")
    route_mode = _parse_optional_text(arguments, "model_route_mode")
    if route_mode is not None and route_mode not in _MODEL_ROUTE_MODES:
        raise MCPToolError(
            "model_route_mode must be local_only, cloud_allowed, cloud_requires_approval, or model_disabled"
        )
    temperature = _parse_optional_float(arguments, "model_temperature")
    if temperature is None:
        temperature = 0.2
    if temperature < 0.0 or temperature > 2.0:
        raise MCPToolError("model_temperature must be between 0.0 and 2.0")
    return {
        "generation_mode": generation_mode,
        "model_route_mode": route_mode,
        "model_provider": _parse_optional_text(arguments, "model_provider"),
        "model": _parse_optional_text(arguments, "model"),
        "model_temperature": temperature,
        "allow_cloud_private": _parse_bool(arguments, key="allow_cloud_private", default=False),
    }


def _parse_review_status(
    arguments: Mapping[str, object],
    *,
    default: str,
) -> str:
    raw_status = arguments.get("status", default)
    if not isinstance(raw_status, str):
        raise MCPToolError("status must be a string")
    normalized = raw_status.strip()
    if normalized in _REVIEW_STATUS_ALIASES:
        normalized = _REVIEW_STATUS_ALIASES[normalized]
    if normalized not in _REVIEW_STATUS_CHOICES:
        allowed = ", ".join(_REVIEW_STATUS_CHOICES)
        raise MCPToolError(f"status must be one of: {allowed}")
    if normalized == "pending_review":
        return "stale"
    return normalized


def _parse_review_item_id(arguments: Mapping[str, object], *, required: bool) -> UUID | None:
    review_item_id = _parse_optional_uuid(arguments, "review_item_id")
    continuity_object_id = _parse_optional_uuid(arguments, "continuity_object_id")
    if review_item_id is not None and continuity_object_id is not None and review_item_id != continuity_object_id:
        raise MCPToolError("review_item_id and continuity_object_id must match when both are provided")
    resolved = review_item_id or continuity_object_id
    if required and resolved is None:
        raise MCPToolError("review_item_id or continuity_object_id is required and must be a UUID string")
    return resolved


def _resolve_review_apply_action(raw_action: str, *, allow_legacy: bool) -> str:
    normalized = raw_action.strip()
    if normalized in _REVIEW_APPLY_ACTION_ALIASES:
        normalized = _REVIEW_APPLY_ACTION_ALIASES[normalized]
    mapped = _REVIEW_APPLY_TO_CORRECTION_ACTION.get(normalized)
    if mapped is not None:
        return mapped
    if allow_legacy and normalized in CONTINUITY_CORRECTION_ACTIONS:
        return normalized
    # Advertise only the schema enum; legacy action names are still accepted
    # above when allow_legacy is set, but are not part of the public surface.
    raise MCPToolError(f"action must be one of: {', '.join(_REVIEW_APPLY_ACTION_CHOICES)}")


def _build_recall_query(arguments: Mapping[str, object], *, limit: int) -> ContinuityRecallQueryInput:
    return ContinuityRecallQueryInput(
        query=_parse_optional_text(arguments, "query"),
        thread_id=_parse_optional_uuid(arguments, "thread_id"),
        task_id=_parse_optional_uuid(arguments, "task_id"),
        project=_parse_optional_text(arguments, "project"),
        person=_parse_optional_text(arguments, "person"),
        since=_parse_optional_datetime(arguments, "since"),
        until=_parse_optional_datetime(arguments, "until"),
        limit=limit,
    )


def _canonicalize_json(value: object) -> object:
    value = json_safe(value)
    if isinstance(value, dict):
        return {key: _canonicalize_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize_json(item) for item in value]
    return value


def _recency_sort_key(item: Mapping[str, object]) -> tuple[str, str]:
    created_at = str(item.get("created_at", ""))
    item_id = str(item.get("id", ""))
    return created_at, item_id


def _extract_prefetch_single_title(section: object) -> str:
    if not isinstance(section, Mapping):
        return ""
    item = section.get("item")
    if not isinstance(item, Mapping):
        return ""
    title = item.get("title")
    if not isinstance(title, str):
        return ""
    return title.strip()


def _extract_prefetch_titles(section: object, *, limit: int) -> list[str]:
    if not isinstance(section, Mapping):
        return []
    items = section.get("items")
    if not isinstance(items, list):
        return []

    titles: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        if not isinstance(title, str):
            continue
        normalized = title.strip()
        if normalized == "":
            continue
        titles.append(normalized)
        if len(titles) >= limit:
            break
    return titles


def _render_prefetch_context_text(
    *,
    brief: Mapping[str, object],
    open_loops_limit: int,
    recent_changes_limit: int,
) -> str:
    lines: list[str] = ["## Alice Continuity Prefetch"]

    last_decision = _extract_prefetch_single_title(brief.get("last_decision"))
    if last_decision:
        lines.append(f"- Last decision: {quote_stored_note(last_decision)}")

    next_action = _extract_prefetch_single_title(brief.get("next_action"))
    if next_action:
        lines.append(f"- Next action: {quote_stored_note(next_action)}")

    open_loop_titles = _extract_prefetch_titles(brief.get("open_loops"), limit=open_loops_limit)
    if open_loop_titles:
        lines.append("- Open loops:")
        lines.extend([f"  - {quote_stored_note(title)}" for title in open_loop_titles])

    recent_change_titles = _extract_prefetch_titles(brief.get("recent_changes"), limit=recent_changes_limit)
    if recent_change_titles:
        lines.append("- Recent changes:")
        lines.extend([f"  - {quote_stored_note(title)}" for title in recent_change_titles])

    if len(lines) == 1:
        return ""
    return frame_rendered_block("\n".join(lines))
