"""MCP readOnlyHint is only for a tool that does not write.

Checked the handlers named for Codex approval. Each one writes when an
agent identity is present, so none of them gets readOnlyHint.

- alice_recall and alice_recent_decisions call _mcp_agent_policy_preflight,
  which upserts the agent identity and appends a policy.decision event.
- alice_resume and alice_context_pack call _policy_checked, which does the
  same write.
- alice_open_loops updates the loop when action is not list, and the list
  path still runs the policy preflight.
- alice_explain authorizes through _policy_checked before it reads the
  audit record.
"""

from __future__ import annotations

from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

_CHECKED = (
    "alice_recall",
    "alice_resume",
    "alice_context_pack",
    "alice_open_loops",
    "alice_recent_decisions",
    "alice_explain",
)


def test_checked_tools_do_not_declare_read_only_hint() -> None:
    tools = {
        str(tool["name"]): tool
        for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS)
    }
    for name in _CHECKED:
        tool = tools[name]
        annotations = tool.get("annotations")
        hint = None
        if isinstance(annotations, dict):
            hint = annotations.get("readOnlyHint")
        assert hint is not True, name
