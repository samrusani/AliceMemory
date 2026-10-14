"""MCP handler for alice_what_changed."""

from __future__ import annotations

from collections.abc import Mapping

from alicebot_api.vnext_label_guard import label_read_scope
from alicebot_api.vnext_what_changed import what_changed

from .runtime import _open_vnext_store
from .shared import MCPRuntimeContext, _agent_identity_from_arguments, _json_object, _parse_required_text
from .types import JsonObject


def _handle_alice_what_changed(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    topic = _parse_required_text(arguments, "topic")
    identity = _agent_identity_from_arguments(context, arguments)
    with _open_vnext_store(context) as store:
        with label_read_scope(store):
            return _json_object(
                what_changed(store, user_id=context.user_id, identity=identity, topic=topic)
            )
