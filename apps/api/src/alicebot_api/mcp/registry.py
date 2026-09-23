"""Mechanical MCP registry carrier."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import (
    date,
    datetime,
)
from uuid import UUID
from psycopg.errors import CheckViolation
from alicebot_api.continuity_capture import ContinuityCaptureValidationError
from alicebot_api.continuity_brief import ContinuityBriefValidationError
from alicebot_api.continuity_evidence import ContinuityEvidenceNotFoundError
from alicebot_api.continuity_contradictions import (
    ContinuityContradictionNotFoundError,
    ContinuityContradictionValidationError,
)
from alicebot_api.continuity_recall import (
    ContinuityRecallValidationError,
    RetrievalTraceNotFoundError,
)
from alicebot_api.continuity_resumption import ContinuityResumptionValidationError
from alicebot_api.continuity_review import (
    ContinuityReviewNotFoundError,
    ContinuityReviewValidationError,
)
from alicebot_api.memory_mutations import MemoryMutationValidationError
from alicebot_api.store import JsonObject
from alicebot_api.surface_flags import (
    LEGACY_SURFACES_ENV,
    MCP_FULL_TOOLS_ENV,
    MCP_LEGACY_TOOLS_ENV,
    legacy_surfaces_enabled,
    mcp_full_tools_enabled,
    mcp_legacy_tools_enabled,
)
from alicebot_api.temporal_state import TemporalStateValidationError
from alicebot_api.task_briefing import (
    TaskBriefNotFoundError,
    TaskBriefValidationError,
)

from .capture_automation import (
    _handle_alice_vnext_capture,
    _handle_alice_vnext_generate_artifact,
    _handle_alice_vnext_ingest_agent_output,
    _handle_alice_vnext_queue_task,
)
from .capture_mutations import (
    _handle_alice_capture_candidates,
    _handle_alice_commit_captures,
    _handle_alice_memory_mutations_commit,
    _handle_alice_memory_mutations_generate,
    _handle_alice_memory_mutations_list_candidates,
    _handle_alice_memory_mutations_list_operations,
)
from .context import (
    _handle_alice_context_pack,
    _handle_alice_vnext_context_pack,
    _handle_alice_vnext_context_tree,
)
from .definitions import (
    _CORE_TOOL_DEFINITIONS,
    _LEGACY_TOOL_DEFINITIONS,
)
from .evidence_artifacts import (
    _handle_alice_artifact_inspect,
    _handle_alice_explain,
    _handle_alice_vnext_artifact_get,
    _handle_alice_vnext_artifact_review,
    _handle_alice_vnext_memory_audit,
    _handle_alice_vnext_review_items,
)
from .memories import (
    _handle_alice_memory_manage,
    _handle_alice_vnext_commit_memory,
    _handle_alice_vnext_confirm_memory,
    _handle_alice_vnext_correct_memory,
    _handle_alice_vnext_forget_memory,
    _handle_alice_vnext_propose_memory,
    _handle_alice_vnext_recent_memory_commits,
    _handle_alice_vnext_undo_memory,
)
from .projects import (
    _handle_alice_open_loop_extract,
    _handle_alice_open_loop_review,
    _handle_alice_project_dashboard,
    _handle_alice_project_update_candidate,
    _handle_alice_project_update_review,
    _handle_alice_vnext_open_loops,
)
from .retrieval import (
    _handle_alice_brief,
    _handle_alice_open_loops,
    _handle_alice_prefetch_context,
    _handle_alice_recall,
    _handle_alice_recall_debug,
    _handle_alice_recent_changes,
    _handle_alice_recent_decisions,
    _handle_alice_resume,
    _handle_alice_resume_debug,
    _handle_alice_retrieval_trace,
    _handle_alice_state_at,
    _handle_alice_task_brief,
    _handle_alice_task_brief_compare,
    _handle_alice_task_brief_show,
    _handle_alice_timeline,
)
from .review import (
    _handle_alice_contradictions_detect,
    _handle_alice_contradictions_list,
    _handle_alice_contradictions_resolve,
    _handle_alice_memory_correct,
    _handle_alice_memory_review,
    _handle_alice_review_apply,
    _handle_alice_review_queue,
    _handle_alice_trust_signals,
)
from .scheduler import (
    _handle_alice_vnext_scheduler_pause,
    _handle_alice_vnext_scheduler_resume,
    _handle_alice_vnext_scheduler_run_due,
    _handle_alice_vnext_scheduler_run_now,
    _handle_alice_vnext_scheduler_status,
)
from .shared import (
    AGENT_API_KEY_ENV,
    MCPRuntimeContext,
    MCPToolError,
    MCPToolNotFoundError,
    _agent_identity_from_arguments,
    _canonicalize_json,
    _normalize_arguments,
)
from .synthesis import (
    _handle_alice_belief_review,
    _handle_alice_belief_state,
    _handle_alice_generate_connections,
    _handle_alice_generate_contradictions,
    _handle_alice_generate_daily_brief,
    _handle_alice_generate_weekly_synthesis,
    _handle_alice_graph_edge_review,
    _handle_alice_graph_neighborhood,
)

_TOOL_HANDLERS = {
    "alice_capture": _handle_alice_vnext_capture,
    # Core front door for explicit agent writes; same handler as the legacy
    # alice_vnext_commit_memory alias below. Only this name's schema admits
    # confirmation_id, so only this name reaches the confirmation route.
    "alice_memory_commit": _handle_alice_vnext_commit_memory,
    "alice_memory_manage": _handle_alice_memory_manage,
    "alice_capture_candidates": _handle_alice_capture_candidates,
    "alice_commit_captures": _handle_alice_commit_captures,
    "alice_memory_mutations_generate": _handle_alice_memory_mutations_generate,
    "alice_memory_mutations_list_candidates": _handle_alice_memory_mutations_list_candidates,
    "alice_memory_mutations_commit": _handle_alice_memory_mutations_commit,
    "alice_memory_mutations_list_operations": _handle_alice_memory_mutations_list_operations,
    "alice_recall": _handle_alice_recall,
    "alice_recall_debug": _handle_alice_recall_debug,
    "alice_state_at": _handle_alice_state_at,
    "alice_resume": _handle_alice_resume,
    "alice_resume_debug": _handle_alice_resume_debug,
    "alice_brief": _handle_alice_brief,
    "alice_task_brief": _handle_alice_task_brief,
    "alice_task_brief_show": _handle_alice_task_brief_show,
    "alice_task_brief_compare": _handle_alice_task_brief_compare,
    "alice_retrieval_trace": _handle_alice_retrieval_trace,
    "alice_prefetch_context": _handle_alice_prefetch_context,
    "alice_open_loops": _handle_alice_open_loops,
    "alice_recent_decisions": _handle_alice_recent_decisions,
    "alice_recent_changes": _handle_alice_recent_changes,
    "alice_timeline": _handle_alice_timeline,
    "alice_review_queue": _handle_alice_review_queue,
    "alice_review_apply": _handle_alice_review_apply,
    "alice_contradictions_detect": _handle_alice_contradictions_detect,
    "alice_contradictions_list": _handle_alice_contradictions_list,
    "alice_contradictions_resolve": _handle_alice_contradictions_resolve,
    "alice_trust_signals": _handle_alice_trust_signals,
    "alice_memory_review": _handle_alice_memory_review,
    "alice_memory_correct": _handle_alice_memory_correct,
    "alice_explain": _handle_alice_explain,
    "alice_artifact_inspect": _handle_alice_artifact_inspect,
    "alice_context_pack": _handle_alice_context_pack,
    "alice_vnext_context_pack": _handle_alice_vnext_context_pack,
    "alice_vnext_context_tree": _handle_alice_vnext_context_tree,
    "alice_generate_daily_brief": _handle_alice_generate_daily_brief,
    "alice_generate_weekly_synthesis": _handle_alice_generate_weekly_synthesis,
    "alice_generate_connections": _handle_alice_generate_connections,
    "alice_graph_edge_review": _handle_alice_graph_edge_review,
    "alice_graph_neighborhood": _handle_alice_graph_neighborhood,
    "alice_generate_contradictions": _handle_alice_generate_contradictions,
    "alice_belief_review": _handle_alice_belief_review,
    "alice_belief_state": _handle_alice_belief_state,
    "alice_project_update_candidate": _handle_alice_project_update_candidate,
    "alice_project_update_review": _handle_alice_project_update_review,
    "alice_project_dashboard": _handle_alice_project_dashboard,
    "alice_open_loop_extract": _handle_alice_open_loop_extract,
    "alice_open_loop_review": _handle_alice_open_loop_review,
    "alice_vnext_capture": _handle_alice_vnext_capture,
    "alice_vnext_ingest_agent_output": _handle_alice_vnext_ingest_agent_output,
    "alice_vnext_queue_task": _handle_alice_vnext_queue_task,
    "alice_vnext_generate_artifact": _handle_alice_vnext_generate_artifact,
    "alice_vnext_project_dashboard": _handle_alice_project_dashboard,
    "alice_vnext_open_loops": _handle_alice_vnext_open_loops,
    "alice_vnext_recent_decisions": _handle_alice_recent_decisions,
    "alice_vnext_recent_changes": _handle_alice_recent_changes,
    "alice_vnext_find_connections": _handle_alice_generate_connections,
    "alice_vnext_find_contradictions": _handle_alice_generate_contradictions,
    "alice_vnext_propose_memory": _handle_alice_vnext_propose_memory,
    "alice_vnext_commit_memory": _handle_alice_vnext_commit_memory,
    "alice_vnext_confirm_memory": _handle_alice_vnext_confirm_memory,
    "alice_vnext_undo_memory": _handle_alice_vnext_undo_memory,
    "alice_vnext_correct_memory": _handle_alice_vnext_correct_memory,
    "alice_vnext_forget_memory": _handle_alice_vnext_forget_memory,
    "alice_vnext_recent_memory_commits": _handle_alice_vnext_recent_memory_commits,
    "alice_vnext_memory_audit": _handle_alice_vnext_memory_audit,
    "alice_vnext_review_items": _handle_alice_vnext_review_items,
    "alice_vnext_artifact_get": _handle_alice_vnext_artifact_get,
    "alice_vnext_artifact_review": _handle_alice_vnext_artifact_review,
    "alice_vnext_scheduler_status": _handle_alice_vnext_scheduler_status,
    "alice_vnext_scheduler_run_now": _handle_alice_vnext_scheduler_run_now,
    "alice_vnext_scheduler_run_due": _handle_alice_vnext_scheduler_run_due,
    "alice_vnext_scheduler_pause": _handle_alice_vnext_scheduler_pause,
    "alice_vnext_scheduler_resume": _handle_alice_vnext_scheduler_resume,
}


_CORE_TOOL_NAMES = frozenset(str(tool["name"]) for tool in _CORE_TOOL_DEFINITIONS)


_DEFAULT_CORE_TOOL_ORDER = (
    "alice_memory_commit",
    "alice_recall",
    "alice_resume",
)


_DEFAULT_CORE_TOOL_NAMES = frozenset(_DEFAULT_CORE_TOOL_ORDER)


_LEGACY_TOOL_NAMES = frozenset(str(tool["name"]) for tool in _LEGACY_TOOL_DEFINITIONS)


_TASK_BRIEF_TOOL_NAMES = frozenset(
    {
        "alice_task_brief",
        "alice_task_brief_show",
        "alice_task_brief_compare",
    }
)


_TOOL_DEFINITIONS_BY_NAME = {str(tool["name"]): tool for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS)}


def _validate_mcp_arguments_against_advertised_schema(
    name: str,
    arguments: Mapping[str, object],
) -> None:
    """Enforce the advertised JSON-Schema subset before any handler runs."""
    definition = _TOOL_DEFINITIONS_BY_NAME.get(name)
    schema = definition.get("inputSchema") if isinstance(definition, Mapping) else None
    if not isinstance(schema, Mapping):
        return

    def fail(path: str, detail: str) -> None:
        raise MCPToolError(f"tool '{name}' has invalid value at {path}: {detail}")

    def matches_type(value: object, schema_type: str) -> bool:
        if schema_type == "null":
            return value is None
        if schema_type == "object":
            return isinstance(value, Mapping)
        if schema_type == "array":
            return isinstance(value, list)
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            return not isinstance(value, float) or math.isfinite(value)
        return False

    def enum_equal(left: object, right: object) -> bool:
        # Python considers True == 1 and False == 0, while JSON Schema keeps
        # booleans distinct from numbers.
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left is right
        if (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        ):
            left_finite = not isinstance(left, float) or math.isfinite(left)
            right_finite = not isinstance(right, float) or math.isfinite(right)
            return left_finite and right_finite and left == right
        return left == right

    def validate_format(value: str, schema_format: str, path: str) -> None:
        if schema_format == "uuid":
            try:
                parsed_uuid = UUID(value)
            except (AttributeError, ValueError) as exc:
                raise MCPToolError(f"tool '{name}' has invalid value at {path}: must be a UUID string") from exc
            if str(parsed_uuid) != value.casefold():
                fail(path, "must be a canonical UUID string")
            return
        if schema_format == "date-time":
            if (
                re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
                    value,
                )
                is None
            ):
                fail(path, "must be an RFC 3339 date-time with a timezone")
            try:
                parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError as exc:
                raise MCPToolError(
                    f"tool '{name}' has invalid value at {path}: must be a valid RFC 3339 date-time"
                ) from exc
            if parsed_datetime.tzinfo is None:
                fail(path, "must include a timezone")
            return
        if schema_format == "date":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                fail(path, "must be an RFC 3339 full-date (YYYY-MM-DD)")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise MCPToolError(
                    f"tool '{name}' has invalid value at {path}: must be a valid RFC 3339 full-date"
                ) from exc
            return
        fail(path, f"uses unsupported advertised format {schema_format!r}")

    def validate(value: object, candidate_schema: Mapping[str, object], path: str) -> None:
        field_name = path.rsplit(".", 1)[-1]
        raw_type = candidate_schema.get("type")
        schema_types: tuple[str, ...] = ()
        if isinstance(raw_type, str):
            schema_types = (raw_type,)
        elif isinstance(raw_type, list) and all(isinstance(item, str) for item in raw_type):
            schema_types = tuple(raw_type)
        if schema_types and not any(matches_type(value, item) for item in schema_types):
            fail(path, field_name + " must have type " + " or ".join(schema_types))

        raw_enum = candidate_schema.get("enum")
        if isinstance(raw_enum, list) and not any(enum_equal(value, item) for item in raw_enum):
            rendered = ", ".join(json.dumps(item, sort_keys=True) for item in raw_enum)
            fail(path, f"{field_name} must be one of: {rendered}")

        schema_format = candidate_schema.get("format")
        if isinstance(value, str) and isinstance(schema_format, str):
            validate_format(value, schema_format, path)

        pattern = candidate_schema.get("pattern")
        if isinstance(value, str) and isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            fail(path, f"must match pattern {pattern}")

        # Enforced since 2026-09-23 (S4.4 round 2, ruling R4). Before that a
        # maxLength in a schema was advertised and never checked.
        maximum_length = candidate_schema.get("maxLength")
        if isinstance(value, str) and isinstance(maximum_length, int) and len(value) > maximum_length:
            fail(path, f"must be at most {maximum_length} characters")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = candidate_schema.get("minimum")
            maximum = candidate_schema.get("maximum")
            bounded_minimum = (
                float(minimum) if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) else None
            )
            bounded_maximum = (
                float(maximum) if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) else None
            )
            if bounded_minimum is not None and value < bounded_minimum:
                if bounded_maximum is not None:
                    fail(
                        path,
                        f"{field_name} must be between {bounded_minimum:g} and {bounded_maximum:g}",
                    )
                fail(path, f"{field_name} must be greater than or equal to {bounded_minimum:g}")
            if bounded_maximum is not None and value > bounded_maximum:
                if bounded_minimum is not None:
                    fail(
                        path,
                        f"{field_name} must be between {bounded_minimum:g} and {bounded_maximum:g}",
                    )
                fail(path, f"{field_name} must be less than or equal to {bounded_maximum:g}")

        if isinstance(value, list):
            minimum_items = candidate_schema.get("minItems")
            if isinstance(minimum_items, int) and len(value) < minimum_items:
                fail(path, f"must contain at least {minimum_items} items")
            maximum_items = candidate_schema.get("maxItems")
            if isinstance(maximum_items, int) and len(value) > maximum_items:
                fail(path, f"must contain at most {maximum_items} items")

        properties = candidate_schema.get("properties")
        required = candidate_schema.get("required")
        # Top-level handlers already own their established error vocabulary;
        # enforce nested requirements here, where otherwise a permissive
        # object could bypass the advertised contract before the handler sees
        # it.
        if path != "arguments" and isinstance(value, Mapping) and isinstance(required, list):
            missing = sorted(str(key) for key in required if isinstance(key, str) and key not in value)
            if missing:
                location = "" if path == "arguments" else f" at {path}"
                raise MCPToolError(f"tool '{name}' is missing required properties{location}: " + ", ".join(missing))
        minimum_properties = candidate_schema.get("minProperties")
        if isinstance(value, Mapping) and isinstance(minimum_properties, int) and len(value) < minimum_properties:
            location = "" if path == "arguments" else f" at {path}"
            raise MCPToolError(f"tool '{name}' requires at least {minimum_properties} properties{location}")
        maximum_properties = candidate_schema.get("maxProperties")
        if isinstance(value, Mapping) and isinstance(maximum_properties, int) and len(value) > maximum_properties:
            fail(path, f"must contain at most {maximum_properties} properties")
        if isinstance(value, Mapping) and candidate_schema.get("additionalProperties") is False:
            allowed = set(properties) if isinstance(properties, Mapping) else set()
            unknown = sorted(str(key) for key in value if key not in allowed)
            if unknown:
                location = "" if path == "arguments" else f" at {path}"
                raise MCPToolError(
                    f"tool '{name}' does not accept additional properties{location}: " + ", ".join(unknown)
                )
        if isinstance(value, Mapping) and isinstance(properties, Mapping):
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, Mapping):
                    validate(child, child_schema, f"{path}.{key}")
        items = candidate_schema.get("items")
        if isinstance(value, list) and isinstance(items, Mapping):
            for index, child in enumerate(value):
                validate(child, items, f"{path}[{index}]")

    validate(arguments, schema, "arguments")


def _legacy_tools_enabled() -> bool:
    # Legacy handlers do not all enforce persisted-target authorization. Do
    # not expose a partially protected surface when the server is operating
    # as a key-bound agent, even if the opt-in legacy flag is also set.
    if (os.environ.get(AGENT_API_KEY_ENV) or "").strip():
        return False
    return mcp_legacy_tools_enabled()


def _advertised_core_definitions() -> list[dict[str, object]]:
    if mcp_full_tools_enabled():
        return list(_CORE_TOOL_DEFINITIONS)
    by_name = {str(tool["name"]): tool for tool in _CORE_TOOL_DEFINITIONS}
    return [by_name[name] for name in _DEFAULT_CORE_TOOL_ORDER]


def _enabled_core_tool_names() -> frozenset[str]:
    if mcp_full_tools_enabled():
        return _CORE_TOOL_NAMES
    return _DEFAULT_CORE_TOOL_NAMES


def _enabled_tool_definitions() -> list[dict[str, object]]:
    core_definitions = _advertised_core_definitions()
    if _legacy_tools_enabled():
        legacy_definitions = _LEGACY_TOOL_DEFINITIONS
        if not legacy_surfaces_enabled():
            legacy_definitions = [
                definition for definition in legacy_definitions if str(definition["name"]) not in _TASK_BRIEF_TOOL_NAMES
            ]
        return [*core_definitions, *legacy_definitions]
    return core_definitions


def list_mcp_tools() -> list[dict[str, object]]:
    return _canonicalize_json(_enabled_tool_definitions())  # type: ignore[return-value]


def call_mcp_tool(
    context: MCPRuntimeContext,
    *,
    name: str,
    arguments: object,
) -> JsonObject:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise MCPToolNotFoundError(f"unknown tool '{name}'")
    if name in _LEGACY_TOOL_NAMES and (os.environ.get(AGENT_API_KEY_ENV) or "").strip():
        raise MCPToolNotFoundError(
            f"tool '{name}' is part of the legacy MCP surface, which is disabled whenever "
            f"{AGENT_API_KEY_ENV} is configured"
        )
    if name not in _CORE_TOOL_NAMES and not _legacy_tools_enabled():
        raise MCPToolNotFoundError(
            f"tool '{name}' is part of the legacy MCP surface and is currently disabled; "
            f"set {MCP_LEGACY_TOOLS_ENV}=1 in the MCP server environment to enable legacy tools"
        )
    if name in _CORE_TOOL_NAMES and name not in _enabled_core_tool_names():
        raise MCPToolNotFoundError(
            f"tool '{name}' is part of the full MCP surface and is currently disabled; "
            f"set {MCP_FULL_TOOLS_ENV}=1 in the MCP server environment to enable the full tool set"
        )
    if name in _TASK_BRIEF_TOOL_NAMES and not legacy_surfaces_enabled():
        raise MCPToolNotFoundError(
            f"tool '{name}' is part of the legacy task surface and is currently disabled; "
            f"set {LEGACY_SURFACES_ENV}=1 in addition to {MCP_LEGACY_TOOLS_ENV} to enable it"
        )

    parsed_arguments = _normalize_arguments(arguments)
    _validate_mcp_arguments_against_advertised_schema(name, parsed_arguments)
    try:
        if name in _CORE_TOOL_NAMES:
            # Authentication is a property of the MCP boundary, not an
            # optional responsibility of individual handlers.  This makes
            # invalid/revoked keys fail before reads as well as writes.  Core
            # tools always cross this boundary. Key-bound servers fail closed
            # by removing the legacy surface altogether.
            identity = _agent_identity_from_arguments(context, parsed_arguments)
            context = replace(
                context,
                agent_identity=identity,
                agent_identity_resolved=True,
            )
        payload = handler(context, parsed_arguments)
    except (
        ContinuityCaptureValidationError,
        ContinuityRecallValidationError,
        ContinuityBriefValidationError,
        ContinuityResumptionValidationError,
        ContinuityReviewValidationError,
        ContinuityReviewNotFoundError,
        ContinuityContradictionValidationError,
        ContinuityContradictionNotFoundError,
        RetrievalTraceNotFoundError,
        ContinuityEvidenceNotFoundError,
        MemoryMutationValidationError,
        TaskBriefNotFoundError,
        TaskBriefValidationError,
        TemporalStateValidationError,
    ) as exc:
        raise MCPToolError(str(exc)) from exc
    except CheckViolation as exc:
        raise MCPToolError(
            "vNext request violates a persisted schema constraint; use schema-backed enum values "
            "for memory_type, domain, sensitivity, status, and action fields."
        ) from exc
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "CHECK constraint" in message:
            raise MCPToolError(
                "vNext request violates a persisted schema constraint; use schema-backed enum values "
                "for memory_type, domain, sensitivity, status, and action fields."
            ) from exc
        if "FOREIGN KEY constraint failed" in message:
            raise MCPToolError(
                "a row this write references does not exist in the SQLite database (most often the "
                "acting user row); bootstrap it with 'alice-memory init' or verify the referenced ids."
            ) from exc
        raise MCPToolError(message) from exc
    except (TypeError, ValueError) as exc:
        raise MCPToolError(str(exc)) from exc

    return _canonicalize_json(payload)  # type: ignore[return-value]
