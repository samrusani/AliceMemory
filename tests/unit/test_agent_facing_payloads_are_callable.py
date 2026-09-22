"""Every JSON payload we hand an agent must survive the server's own validator.

Agents copy examples. That is what examples are for. So an example payload that the server
rejects is not a typo, it is a broken instruction shipped to a machine that will follow it.

Found 2026-08-15 by executing the OpenClaw skill pack against the published v0.15.4 artifact
over stdio. Nine payloads across four files were rejected outright, because the skill packs and
their recipe docs named the CORE eleven-tool surface while still showing payloads shaped for
the legacy `alice_vnext_*` tools. The clearest case: every documented "explicit commit" carried
`intent: "explicit_remember"`, which exists only on `alice_vnext_commit_memory`. Removing that
one key was the difference between a hard failure and `status=committed`.

This guard runs each documented payload through
`_validate_mcp_arguments_against_advertised_schema`, the same function the server calls before
any handler runs, against the tool the document presents it as being for. It deliberately
validates against tools on the DEFAULT surface: a payload that only works once an operator
sets ALICE_MCP_LEGACY_TOOLS=1 is not something to hand an agent unqualified.

Adding a JSON block to one of these files without classifying it fails
`test_every_documented_payload_is_classified`. That is the point: someone must say which tool a
new example is for, and then it gets checked.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS
from alicebot_api.mcp.registry import _validate_mcp_arguments_against_advertised_schema
from alicebot_api.mcp_tools import MCPToolError


REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_TOOL_NAMES = frozenset(str(tool["name"]) for tool in _CORE_TOOL_DEFINITIONS)

# Not an MCP tool payload. Host configuration, error shapes, and prose fragments.
NOT_A_TOOL_PAYLOAD = "n/a"

# (relative path, block index) -> tool the document presents the payload as being for.
# Block index counts ```json fences in file order, from zero.
DOCUMENTED_PAYLOADS: dict[tuple[str, int], str] = {
    ("agent-skills/hermes/alice-memory/SKILL.md", 0): "alice_memory_commit",
    ("agent-skills/hermes/alice-memory/SKILL.md", 1): "alice_memory_commit",
    ("agent-skills/hermes/alice-memory/SKILL.md", 2): "alice_memory_commit",  # finishes block 1
    ("agent-skills/hermes/alice-memory/SKILL.md", 3): "alice_memory_commit",
    ("agent-skills/hermes/alice-memory/SKILL.md", 4): "alice_memory_commit",
    ("agent-skills/openclaw/alice-project-memory/SKILL.md", 0): "alice_memory_commit",
    ("agent-skills/openclaw/alice-project-memory/SKILL.md", 1): "alice_capture",
    ("agent-skills/openclaw/alice-project-memory/SKILL.md", 2): "alice_memory_commit",
    ("docs/alpha/hermes-skill.md", 0): "alice_memory_commit",
    ("docs/alpha/hermes-skill.md", 1): "alice_memory_commit",
    ("docs/alpha/hermes-skill.md", 2): "alice_memory_commit",
    ("docs/alpha/hermes-skill.md", 3): "alice_memory_commit",  # finishes block 2
    ("docs/alpha/hermes-skill.md", 4): "alice_memory_commit",
    ("docs/alpha/openclaw-skill.md", 0): "alice_memory_commit",
    ("docs/alpha/openclaw-skill.md", 1): "alice_context_pack",
    ("docs/alpha/openclaw-skill.md", 2): "alice_capture",
    ("docs/alpha/openclaw-skill.md", 3): "alice_memory_commit",
    ("docs/alpha/mcp-tools.md", 0): NOT_A_TOOL_PAYLOAD,
    ("docs/alpha/mcp-tools.md", 1): NOT_A_TOOL_PAYLOAD,
    ("docs/alpha/mcp-tools.md", 2): NOT_A_TOOL_PAYLOAD,  # response fragment, not a request
    ("docs/alpha/agent-integration.md", 0): NOT_A_TOOL_PAYLOAD,
    ("docs/alpha/agent-integration.md", 1): "alice_memory_commit",
    ("docs/alpha/agent-integration.md", 2): NOT_A_TOOL_PAYLOAD,
    ("docs/alpha/memory-proposal-recipes.md", 0): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 1): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 2): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 3): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 4): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 5): "alice_memory_commit",
    ("docs/alpha/memory-proposal-recipes.md", 6): "alice_memory_commit",
    **{("docs/alpha/context-pack-recipes.md", i): "alice_context_pack" for i in range(11)},
}

AGENT_FACING_FILES = sorted({path for path, _ in DOCUMENTED_PAYLOADS})

# The identity block every skill opens with is a fragment, not a whole call. It is checked for
# accepted properties but not for required ones, which the surrounding prose supplies.
IDENTITY_FRAGMENTS = {
    ("agent-skills/hermes/alice-memory/SKILL.md", 0),
    ("agent-skills/openclaw/alice-project-memory/SKILL.md", 0),
    ("docs/alpha/hermes-skill.md", 0),
    ("docs/alpha/openclaw-skill.md", 0),
    ("docs/alpha/agent-integration.md", 1),
}


def _json_blocks(relative_path: str) -> list[tuple[int, object]]:
    body = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    blocks: list[tuple[int, object]] = []
    for index, raw in enumerate(re.findall(r"```json\n(.*?)```", body, re.S)):
        try:
            blocks.append((index, json.loads(raw)))
        except json.JSONDecodeError:
            blocks.append((index, None))
    return blocks


def _cases() -> list[tuple[str, int, str, dict]]:
    cases = []
    for relative_path in AGENT_FACING_FILES:
        for index, payload in _json_blocks(relative_path):
            tool = DOCUMENTED_PAYLOADS.get((relative_path, index))
            if tool in (None, NOT_A_TOOL_PAYLOAD) or not isinstance(payload, dict):
                continue
            cases.append((relative_path, index, tool, payload))
    return cases


@pytest.mark.parametrize("relative_path", AGENT_FACING_FILES)
def test_every_documented_payload_is_classified(relative_path: str) -> None:
    """A new example must be named before it can be checked."""

    for index, payload in _json_blocks(relative_path):
        assert (relative_path, index) in DOCUMENTED_PAYLOADS, (
            f"{relative_path} json block {index} is unclassified. Add it to "
            f"DOCUMENTED_PAYLOADS naming the tool it targets, or {NOT_A_TOOL_PAYLOAD!r} if it "
            f"is not an MCP tool payload. Keys: {sorted(payload) if isinstance(payload, dict) else payload}"
        )


def test_classification_table_has_no_stale_entries() -> None:
    """Deleting a block must not leave a phantom entry that silently checks nothing."""

    for relative_path in AGENT_FACING_FILES:
        indices = {index for index, _ in _json_blocks(relative_path)}
        declared = {index for path, index in DOCUMENTED_PAYLOADS if path == relative_path}
        stale = declared - indices
        assert not stale, f"{relative_path} declares blocks that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize(
    ("relative_path", "index", "tool", "payload"),
    _cases(),
    ids=lambda value: f"{value}" if isinstance(value, (str, int)) else "",
)
def test_documented_payload_targets_a_default_surface_tool(
    relative_path: str, index: int, tool: str, payload: dict
) -> None:
    assert tool in CORE_TOOL_NAMES, (
        f"{relative_path} block {index} targets {tool!r}, which is not on the default surface. "
        "Agent-facing examples must run on a server started with no extra environment; a "
        "legacy tool needs ALICE_MCP_LEGACY_TOOLS=1 and cannot be shown unqualified."
    )


@pytest.mark.parametrize(
    ("relative_path", "index", "tool", "payload"),
    _cases(),
    ids=lambda value: f"{value}" if isinstance(value, (str, int)) else "",
)
def test_documented_payload_passes_the_servers_own_validator(
    relative_path: str, index: int, tool: str, payload: dict
) -> None:
    try:
        _validate_mcp_arguments_against_advertised_schema(tool, payload)
    except MCPToolError as exc:  # pragma: no cover - the message is the whole point
        pytest.fail(
            f"{relative_path} json block {index} would be rejected by {tool}: {exc}\n"
            f"payload keys: {sorted(payload)}"
        )


@pytest.mark.parametrize(
    ("relative_path", "index", "tool", "payload"),
    [case for case in _cases() if (case[0], case[1]) not in IDENTITY_FRAGMENTS],
    ids=lambda value: f"{value}" if isinstance(value, (str, int)) else "",
)
def test_documented_payload_carries_every_required_property(
    relative_path: str, index: int, tool: str, payload: dict
) -> None:
    from alicebot_api.mcp.registry import _TOOL_DEFINITIONS_BY_NAME

    schema = (_TOOL_DEFINITIONS_BY_NAME[tool].get("inputSchema") or {})
    missing = set(schema.get("required") or []) - set(payload)
    assert not missing, (
        f"{relative_path} json block {index} omits required {sorted(missing)} for {tool}. "
        "An agent copying this example gets a hard failure."
    )


@pytest.mark.parametrize(
    ("relative_path", "index", "tool", "payload"),
    [
        case
        for case in _cases()
        if case[2] == "alice_memory_commit" and (case[0], case[1]) not in IDENTITY_FRAGMENTS
    ],
    ids=lambda value: f"{value}" if isinstance(value, (str, int)) else "",
)
def test_documented_commit_payload_is_a_whole_write_or_a_whole_confirmation(
    relative_path: str, index: int, tool: str, payload: dict
) -> None:
    """Added 2026-09-22 with the D8 fix.

    alice_memory_commit no longer lists title and canonical_text as required,
    because a confirmation call carries neither. The handler still refuses a
    new write without them, so the check above stopped covering commit
    examples. This puts it back, and checks the confirmation shape too.
    """

    if "confirmation_id" in payload:
        assert payload.get("confirmation_action") in ("confirm", "reject"), (
            f"{relative_path} json block {index} sends confirmation_id without "
            "confirmation_action; the handler refuses it"
        )
        mixed = {"title", "canonical_text", "confidence", "domain", "sensitivity"} & set(payload)
        assert not mixed, (
            f"{relative_path} json block {index} mixes a confirmation with write fields "
            f"{sorted(mixed)}; the handler refuses it"
        )
        return
    missing = {"title", "canonical_text"} - set(payload)
    assert not missing, (
        f"{relative_path} json block {index} omits {sorted(missing)}, which a new "
        "alice_memory_commit write needs. An agent copying this example gets a hard failure."
    )


def test_the_commit_shape_check_sees_both_kinds_of_payload() -> None:
    """Guards the guard: the check above must be exercising at least one
    documented confirmation and at least one documented new write."""

    commit_cases = [case for case in _cases() if case[2] == "alice_memory_commit"]
    assert any("confirmation_id" in case[3] for case in commit_cases)
    assert any("canonical_text" in case[3] for case in commit_cases)
