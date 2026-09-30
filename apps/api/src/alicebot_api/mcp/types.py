"""Shared MCP constants, errors, JSON boundary helpers, and runtime types."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from alicebot_api.store import JsonObject, JsonValue
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_projects import OPEN_LOOP_ACTIONS


_REVIEW_STATUS_CHOICES = (
    "pending_review",
    "correction_ready",
    "active",
    "stale",
    "superseded",
    "deleted",
    "all",
)
_REVIEW_STATUS_ALIASES = {
    "pending": "pending_review",
}
_REVIEW_APPLY_ACTION_CHOICES = (
    "approve",
    "edit-and-approve",
    "reject",
    "supersede-existing",
)
_REVIEW_APPLY_ACTION_ALIASES = {
    "edit_and_approve": "edit-and-approve",
    "supersede_existing": "supersede-existing",
}

_PROVENANCE_EVIDENCE_ROLES = (
    "supports",
    "contradicts",
    "mentions",
    "inferred_from",
    "quoted_from",
    "summarizes",
    "background",
)
_REVIEW_APPLY_TO_CORRECTION_ACTION = {
    "approve": "confirm",
    "edit-and-approve": "edit",
    "reject": "delete",
    "supersede-existing": "supersede",
}
_DEFAULT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
_RECALL_DEFAULT_LIMIT = 8
_RECALL_MAX_LIMIT = 50
_OPEN_LOOP_TOOL_ACTIONS = ("list", *sorted(OPEN_LOOP_ACTIONS))
_PREFETCH_CONTEXT_ASSEMBLY_VERSION_V0 = "alice_prefetch_context_v0"
_MODEL_GENERATION_MODES = ("deterministic", "model_backed")
_MODEL_ROUTE_MODES = ("local_only", "cloud_allowed", "cloud_requires_approval", "model_disabled")
_MODEL_GENERATION_SCHEMA_PROPERTIES: dict[str, object] = {
    "generation_mode": {"type": "string", "enum": list(_MODEL_GENERATION_MODES)},
    "model_route_mode": {"type": "string", "enum": list(_MODEL_ROUTE_MODES)},
    "model_provider": {"type": "string"},
    "model": {"type": "string"},
    "model_temperature": {"type": "number", "minimum": 0.0, "maximum": 2.0},
    "allow_cloud_private": {"type": "boolean"},
}


class MCPToolError(ValueError):
    """Raised when MCP tool input or execution fails."""


class MCPInvalidRequestError(MCPToolError):
    """A refused request whose reason is safe to tell the client.

    Every other ``MCPToolError`` answers ``tool_request_failed`` with one
    static message, because its text can carry caller input or internals. This
    one answers ``invalid_request`` with ``public_message``. Build it only from
    fixed words and counts, never from request text. It is still an
    ``MCPToolError``, so code that catches that keeps working.
    """

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class MCPToolNotFoundError(LookupError):
    """Raised when an MCP tool name is not supported."""


def _json_value(value: object) -> JsonValue:
    """Normalize an internal result at the MCP JSON boundary.

    Service-layer TypedDicts intentionally retain precise field types and are
    not treated by mypy as invariant ``dict[str, JsonValue]`` values.  Rebuild
    the value recursively here instead of scattering unchecked casts through
    handlers.
    """
    normalized = json_safe(value)
    if normalized is None or isinstance(normalized, (str, int, float, bool)):
        return normalized
    if isinstance(normalized, list):
        return [_json_value(child) for child in normalized]
    if isinstance(normalized, dict):
        return {str(key): _json_value(child) for key, child in normalized.items()}
    raise MCPToolError(f"MCP result contains unsupported JSON value {type(normalized).__name__}")


def _json_object(value: object) -> JsonObject:
    """Normalize and validate a top-level MCP result object."""
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise MCPToolError("MCP result must be a JSON object")
    return normalized


@dataclass(frozen=True, slots=True)
class MCPRuntimeContext:
    database_url: str
    user_id: UUID
    # Core-tool dispatch resolves the configured agent key exactly once before
    # invoking any handler.  Handlers reuse this authenticated identity rather
    # than opening a second key-verification transaction.
    agent_identity: AgentIdentity | None = None
    agent_identity_resolved: bool = False
