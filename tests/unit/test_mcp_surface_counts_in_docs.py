"""The tool counts the living docs state are the counts the registry lists.

The default three, the eleven core tools, the legacy long tail and the totals under each flag combination are written
by hand into `docs/integrations/mcp.md` and into `CURRENT_STATE.md` (and its mirror). Nothing derived them from the
registry, so a new tool definition or a moved tool would leave every page agreeing with itself and wrong.

This guard lists the tools the way a server does, under each flag combination, and reads the figures out of the pages.

Mutations, each one alone: add a twelfth core tool definition to `_CORE_TOOL_DEFINITIONS`; change `62` to `63` in
`docs/integrations/mcp.md`; delete `alice_explain` from the list in `ARCHITECTURE.md` while the name stays elsewhere
on that page; delete the `alice_explain` bullet under "The full core surface" in `docs/alpha/mcp-tools.md` while the
name stays elsewhere on that page; change `76-total` to `75-total` in `CURRENT_STATE.md`. Each fails one of the tests
below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alicebot_api.mcp.registry import (
    _CORE_TOOL_DEFINITIONS,
    _DEFAULT_CORE_TOOL_ORDER,
    _LEGACY_TOOL_DEFINITIONS,
    list_mcp_tools,
)

ROOT = Path(__file__).resolve().parents[2]

_FLAGS = ("ALICE_AGENT_API_KEY", "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES")
_NUMBER_WORDS = {3: "three", 11: "eleven"}


def _word(number: int) -> str:
    """The word the pages use for 3 and 11, else the digits, so a new count fails on the page text, not on a lookup."""

    return _NUMBER_WORDS.get(number, str(number))


# Pages that name every core tool the default three do not include.
_PAGES_THAT_NAME_THE_OTHER_CORE_TOOLS = (
    "README.md",
    "ARCHITECTURE.md",
    "docs/integrations/mcp.md",
    "docs/alpha/mcp-tools.md",
)


def _flat(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def _listed(monkeypatch: pytest.MonkeyPatch, **flags: str) -> int:
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in flags.items():
        monkeypatch.setenv(name, value)
    return len(list_mcp_tools())


def _counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    return {
        "default": _listed(monkeypatch),
        "full": _listed(monkeypatch, ALICE_MCP_FULL_TOOLS="1"),
        "legacy": _listed(monkeypatch, ALICE_MCP_LEGACY_TOOLS="1"),
        "full_legacy": _listed(monkeypatch, ALICE_MCP_FULL_TOOLS="1", ALICE_MCP_LEGACY_TOOLS="1"),
        "legacy_surfaces": _listed(monkeypatch, ALICE_MCP_LEGACY_TOOLS="1", ALICE_LEGACY_SURFACES="1"),
        "all_three": _listed(
            monkeypatch,
            ALICE_MCP_FULL_TOOLS="1",
            ALICE_MCP_LEGACY_TOOLS="1",
            ALICE_LEGACY_SURFACES="1",
        ),
    }


def test_the_counts_in_the_guide_are_the_counts_the_registry_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _counts(monkeypatch)
    core = len(_CORE_TOOL_DEFINITIONS)
    default = len(_DEFAULT_CORE_TOOL_ORDER)

    # The registry itself agrees with the arithmetic the pages rely on.
    assert counts["default"] == default
    assert counts["full"] == core
    legacy_listed = counts["legacy"] - default
    assert counts["full_legacy"] == core + legacy_listed
    assert counts["all_three"] == core + len(_LEGACY_TOOL_DEFINITIONS)

    guide = _flat("docs/integrations/mcp.md")
    assert f"append {legacy_listed} retained long-tail memory tools" in guide
    assert (
        f"{legacy_listed} retained legacy memory tools are listed alongside whatever core set is enabled "
        f"({counts['legacy']} with the default {_word(default)}, {counts['full_legacy']} with the full "
        f"{_word(core)})"
    ) in guide
    assert f"the counts are {counts['legacy_surfaces']} and {counts['all_three']}" in guide


@pytest.mark.parametrize("relative", ("CURRENT_STATE.md", ".ai/handoff/CURRENT_STATE.md"))
def test_the_state_file_states_the_core_legacy_and_total_counts(monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    counts = _counts(monkeypatch)
    core = len(_CORE_TOOL_DEFINITIONS)
    legacy = len(_LEGACY_TOOL_DEFINITIONS)

    assert core + legacy == counts["all_three"]
    assert f"{core}-core/{legacy}-legacy/{counts['all_three']}-total" in _flat(relative)


def _the_part_that_lists_the_tools(relative: str, names: list[str]) -> tuple[str, str]:
    """The text that has to name every tool, and how a name must sit in it.

    A name that appears somewhere else on a page does not prove the list on that page is whole, so the check reads one
    place. `mcp-tools.md` lists one bullet per tool under its full core surface heading. Every other page lists the
    tools in a single paragraph, so the paragraph that names the most of them is the list.
    """

    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "docs/alpha/mcp-tools.md":
        start = text.index("\n## The full core surface\n")
        end = text.index("\n## ", start + 1)
        return text[start:end], "bullet"
    paragraphs = re.split(r"\n\s*\n", text)
    return max(paragraphs, key=lambda block: sum(name in block for name in names)), "name"


@pytest.mark.parametrize("relative", _PAGES_THAT_NAME_THE_OTHER_CORE_TOOLS)
def test_the_pages_that_list_the_core_tools_name_every_tool_the_default_three_leave_out(relative: str) -> None:
    others = sorted(
        str(tool["name"]) for tool in _CORE_TOOL_DEFINITIONS if str(tool["name"]) not in _DEFAULT_CORE_TOOL_ORDER
    )
    assert others, "every core tool is on the default surface, so this guard checks nothing"

    scope, shape = _the_part_that_lists_the_tools(relative, others)
    missing = [name for name in others if (f"- `{name}`" if shape == "bullet" else name) not in scope]
    assert missing == [], f"{relative} does not list {missing} where it lists the core tools"
