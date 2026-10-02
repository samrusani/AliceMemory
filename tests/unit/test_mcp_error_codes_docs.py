"""The error codes the docs list are the codes the server sends, and main-only text says it is main-only.

Unreleased (on main, not in v0.20.0). v0.20.0 is released and these pages describe it, so the three new codes and the
wider use of ``invalid_request`` carry ``Unreleased (on main, not in v0.20.0):`` until the release PR converts the
markers. The CHANGELOG entry states the v0.20.0 answer for each case, as the release rule for behaviour changes asks.
"""

from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.mcp.types import MCP_CODED_ERROR_CODES

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
# The codes that existed before this change and are still sent.
OLD_CODES = frozenset({"tool_request_failed", "tool_execution_failed", "tool_not_found"})


def _flat(text: str) -> str:
    return " ".join(text.split())


def _error_codes_section() -> str:
    page = (ROOT / "docs/alpha/mcp-tools.md").read_text(encoding="utf-8")
    assert page.count("\n## Error codes\n") == 1
    start = page.index("\n## Error codes\n")
    end = page.index("\n## ", start + 1)
    return page[start:end]


def test_the_table_lists_exactly_the_codes_the_server_can_send() -> None:
    """Every row is a code the server sends, and every code the server sends has a row.

    Mutations: rename a code in the table; delete a row; add a row for a code the server does not send; add a fifth
    code to ``MCP_CODED_ERROR_CODES`` in ``mcp/types.py``. Each fails this test.
    """

    section = _error_codes_section()
    rows = re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.M)

    assert len(rows) == len(set(rows))
    assert set(rows) == MCP_CODED_ERROR_CODES | OLD_CODES


def test_the_section_is_marked_as_main_only_once_and_states_the_v0200_answer() -> None:
    """One marker, and the sentence that says what v0.20.0 answered for the cases the new codes cover.

    The section sits on a page that otherwise describes v0.20.0, so a reader needs the marker to know the three new
    codes are not in the release. The marker is counted inside the section, never page-wide, because other changes
    add marked bullets to the same page.

    Mutations, each one alone: delete the marker from the section; add a second one; delete the sentence that says
    v0.20.0 answered ``tool_request_failed``. Each fails this test.
    """

    section = _flat(_error_codes_section())

    assert section.count(MARK) == 1
    assert section.index(MARK) < section.index("| Code |")
    assert "v0.20.0 answered `tool_request_failed` for every case these four cover" in section


def test_the_table_says_what_an_agent_should_do_for_each_new_code() -> None:
    """The three new codes and ``invalid_request`` each have a row with a reaction, so the table is usable.

    Mutations: empty the last column of a row; swap the reaction of ``not_permitted`` (do not retry) with the one of
    ``invalid_request`` (fix the call and retry). Each fails this test.
    """

    rows = {
        match.group(1): [cell.strip() for cell in match.group(2).split("|")]
        for match in re.finditer(r"^\| `([a-z_]+)` \| (.+) \|$", _error_codes_section(), flags=re.M)
    }

    assert rows["invalid_request"][-1].startswith("Fix the call and retry")
    assert rows["not_permitted"][-1].startswith("Do not retry")
    assert rows["not_found"][-1].startswith("Check the id")
    assert rows["precondition_failed"][-1].startswith("Change the state first")
    for code in MCP_CODED_ERROR_CODES:
        assert all(cell for cell in rows[code]), code


def test_the_pages_that_describe_the_old_answer_carry_the_marker_in_the_right_place() -> None:
    """Each page that says a refusal comes back as ``tool_request_failed`` also says, after one marker, what main does.

    The marker is counted in the sentence or bullet that holds the old text, so a page that gains another marked
    bullet does not break it.

    Mutations, each one alone: delete the marker from one of the five places; move the new-code sentence before the
    marker; delete the ``not_permitted`` clause after the marker. Each fails its own check.
    """

    places = (
        (
            "docs/alpha/mcp-tools.md",
            "Over the stdio server, a refused confirm or reject",
            "come back as `not_permitted`",
        ),
        (
            "docs/alpha/mcp-tools.md",
            "At the MCP wire boundary, tool failures are deliberately stable",
            "`not_permitted`",
        ),
        (
            "docs/alpha/mcp-tools.md",
            "Over stdio, a blocked read or confirm returns `tool_request_failed`",
            "returns `not_permitted`",
        ),
        ("docs/integrations/mcp.md", "tool failure codes are `tool_not_found`", "`not_permitted`"),
        (
            "docs/memory-operations-protocol.md",
            "Over the stdio server, a refused confirm or reject",
            "come back as `not_permitted`",
        ),
        ("docs/alpha/known-limitations.md", "tool failures over stdio return a generic code", "`not_permitted`"),
    )
    for relative, anchor, new_text in places:
        page = _flat((ROOT / relative).read_text(encoding="utf-8"))
        start = page.index(anchor)
        following = page[start : start + 2500]
        before_marker, marker, after_marker = following.partition(MARK)
        assert marker, (relative, anchor)
        assert "tool_request_failed" in before_marker or "generic code" in before_marker, (relative, anchor)
        assert new_text in after_marker.split("\n")[0][:900], (relative, anchor)
        assert "not_permitted" not in before_marker, (relative, anchor)


def test_the_changelog_has_one_unreleased_entry_that_states_the_v0200_code() -> None:
    """One Unreleased entry names the new codes and says v0.20.0 answered ``tool_request_failed`` for each case.

    Mutations, each one alone: delete the sentence ``v0.20.0 answered `tool_request_failed` for each of these
    cases``; move the entry under the v0.20.0 heading; add a second entry that names ``not_permitted``. Each fails
    this test.
    """

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## ")[1]
    assert unreleased.startswith("Unreleased")
    entries = [entry for entry in unreleased.split("\n- ") if "`not_permitted`" in entry]

    assert len(entries) == 1
    entry = _flat(entries[0])
    for code in ("`not_permitted`", "`not_found`", "`precondition_failed`", "`invalid_request`"):
        assert code in entry
    assert "v0.20.0 answered `tool_request_failed` for each of these cases" in entry
    assert "`alice_memory_correct`" in entry and "`human_or_admin_review_required`" in entry
    assert "`not_permitted`" not in changelog.split("\n## v0.20.0")[1].split("\n## v0.19.2")[0]
