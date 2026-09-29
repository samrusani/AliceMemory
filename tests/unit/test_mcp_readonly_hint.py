"""readOnlyHint on tools that only read.

A keyed call still appends a policy.decision event when an agent identity is
present. Keyless calls, the usual Codex path, do not. alice_open_loops writes
loop state on every action other than list, and alice_context_pack always
appends retrieval.context_pack_compiled, so neither gets the hint.

Mutation: drop the annotations on the four read tools. This test fails.
"""

from __future__ import annotations

from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV, MCP_LEGACY_TOOLS_ENV

READ_ONLY = (
    "alice_recall",
    "alice_resume",
    "alice_recent_decisions",
    "alice_explain",
)
WRITES = (
    "alice_memory_commit",
    "alice_context_pack",
    "alice_open_loops",
)


def test_tools_list_marks_only_the_read_tools(monkeypatch) -> None:
    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")
    monkeypatch.delenv(MCP_LEGACY_TOOLS_ENV, raising=False)
    by_name = {tool["name"]: tool for tool in list_mcp_tools()}
    for name in READ_ONLY:
        assert by_name[name]["annotations"] == {"readOnlyHint": True}
    for name in WRITES:
        assert "annotations" not in by_name[name]
