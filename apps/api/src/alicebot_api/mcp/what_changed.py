"""MCP handler for alice_what_changed."""

from __future__ import annotations

from collections.abc import Mapping

from alicebot_api.project_view import ProjectView
from alicebot_api.recall_framing import with_result_framing
from alicebot_api.source_search_limits import SourceSearchQueryTooLarge
from alicebot_api.vnext_what_changed import what_changed

from .policy import _mcp_agent_policy_preflight
from .runtime import _open_vnext_store
from .shared import MCPRuntimeContext, _agent_identity_from_arguments, _json_object, _parse_required_text
from .types import JsonObject, MCPArgumentError


def _handle_alice_what_changed(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    topic = _parse_required_text(arguments, "topic")
    _mcp_agent_policy_preflight(
        context,
        arguments,
        action="recent_decisions.lookup",
        project_view=ProjectView.unscoped(),
    )
    identity = _agent_identity_from_arguments(context, arguments)
    try:
        with _open_vnext_store(context) as store:
            payload = what_changed(store, user_id=context.user_id, identity=identity, topic=topic)
    except SourceSearchQueryTooLarge as exc:
        raise MCPArgumentError(exc.public_message) from exc
    return _json_object(with_result_framing(payload))
