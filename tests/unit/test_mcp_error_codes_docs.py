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


def test_the_section_says_what_an_id_tells_a_caller() -> None:
    """The section says ``alice_explain`` stays uniform and that every other tool answers the difference.

    The second half is a ruling of the second review of PR 528, so it is pinned in words: a key bound to one project
    can learn that an id it holds exists in another. The paragraph is read inside the section, never page-wide.

    The HTTP clause is exact about the codes. The routes tell a refusal (403) from a missing id, but a missing id is 404
    only on the review, redact and audit routes and 400 on the others, so the text says that and not "403 and 404 the
    same way", which the first version of this page said and which was true of three routes out of ten.

    Mutations, each one alone: change ``gets `tool_request_failed` from `alice_explain``` to ``gets `not_found` from
    `alice_explain```; delete the sentence that says a key bound to one project can learn that an id exists in
    another project; change ```not_permitted` from `alice_memory_review` by id`` to ```not_found` from
    `alice_memory_review` by id``; delete the clause that names the HTTP routes; change ``400 from the others`` to
    ``404 from the others``. Each fails this test.
    """

    section = _flat(_error_codes_section())

    assert (
        "A caller that authenticates with an agent key gets `tool_request_failed` from `alice_explain` whether "
        "the target is missing or unreadable"
    ) in section
    assert (
        "an id that the key's project scope refuses answers `not_permitted` from `alice_memory_review` by id, "
        "`alice_memory_correct` and `alice_memory_manage`, and an id that does not exist answers `not_found`"
    ) in section
    assert "a key bound to one project can learn that an id it already holds exists in another project" in section
    assert (
        "the HTTP memory routes also tell a refusal (403) from a missing id (404 from the review, redact and audit "
        "routes, 400 from the others)"
    ) in section
    assert "the HTTP memory routes answer 403 and 404 the same way" not in section


def test_the_section_states_the_rule_for_a_refused_caller_and_a_deleted_row() -> None:
    """The paragraph ``What an id tells a caller`` says plainly what a refused caller hears for a live and a deleted row.

    The tower ruled that a refused caller hears ``not_permitted`` for a live row and ``not_found`` for an archived or
    redacted row, the same as for an id the vault never held. A reader who holds an id can see the answer change when
    the row is deleted, so the paragraph says so and says what that does and does not tell the caller. The sentences are
    read inside the paragraph that starts ``What an id tells a caller.``, so a copy of them elsewhere does not count.

    Mutations, each one alone: change ```not_permitted` for a live row`` to ```not_found` for a live row``; change
    ```not_found` for an archived or redacted row`` to ```not_permitted` for an archived or redacted row``; delete the
    sentence that says ``alice_memory_manage`` with ``action: redact`` is included; delete the sentence about a caller
    that holds the id seeing the answer change; delete the sentence that says a caller with an id it never saw cannot
    tell a deleted row from one that never existed. Each fails this test.
    """

    section = _flat(_error_codes_section())
    start = section.index("What an id tells a caller.")
    end = section.index("Authorization comes before state.", start)
    paragraph = section[start:end]

    assert (
        "A refused caller hears `not_permitted` for a live row and `not_found` for an archived or redacted row, "
        "the same as for an id the vault never held."
    ) in paragraph
    assert "`alice_memory_manage` with `action: redact` included" in paragraph
    assert (
        "an id a caller holds in another project answers `not_permitted` while the row is live, and `not_found` once "
        "the row is archived or redacted"
    ) in paragraph
    assert "A caller that asks with an id it has never seen cannot tell a deleted row from one that never existed." in paragraph
    assert "A caller that held the id can see the answer change, and learns only that the row is gone." in paragraph


def test_the_section_says_authorization_comes_before_state() -> None:
    """The rule of the follow-up to PR 528, in words: the policy is asked before the state of the row.

    A reader who builds an agent on these codes needs to know that ``precondition_failed`` never reaches a caller the
    policy refuses, and that a deleted row is ``not_found`` to such a caller on every verb, redact included. The
    paragraph is read inside the section, never page-wide.

    Mutations, each one alone: delete the paragraph that starts ``Authorization comes before state``; change
    ``gets `not_permitted` whatever state the row is in`` to ``gets `precondition_failed` when the row is in a
    state``; delete the sentence that says ``precondition_failed`` only reaches a caller the policy allows; delete the
    sentence about archived and redacted rows or the clause that names ``redact``. Each fails this test.
    """

    section = _flat(_error_codes_section())

    assert "Authorization comes before state." in section
    assert "gets `not_permitted` whatever state the row is in" in section
    assert "`precondition_failed` only ever reaches a caller the policy allows" in section
    assert "The same holds for the memory named in `superseded_by` and for the HTTP routes" in section
    assert (
        "A memory that has been archived or redacted is gone from the API, and every verb answers a refused caller "
        "`not_found` for it"
    ) in section
    assert "`alice_memory_manage` with `action: redact`, the one verb that reads such a row on purpose" in section


def test_the_confirm_paragraph_says_the_scope_is_checked_before_the_pending_check() -> None:
    """The confirm paragraph says, after one marker, that scope, profile and ceiling now come before the pending check.

    v0.20.0 checked only who may resolve the write first, so the sentence is main-only and says what v0.20.0 did.

    Mutations, each one alone: delete the marker from the sentence; delete ``checked before the pending check too``;
    delete the sentence that says only the check of who may resolve the write came first in v0.20.0. Each fails.
    """

    page = _flat((ROOT / "docs/alpha/mcp-tools.md").read_text(encoding="utf-8"))
    anchor = "Confirming a row that is not pending is refused and writes nothing."
    start = page.index(anchor) + len(anchor)
    sentence_block = page[start : start + 520]

    assert sentence_block.lstrip().startswith(MARK), sentence_block
    assert "checked before the pending check too" in sentence_block
    assert "In v0.20.0 only the check of who may resolve the write came first." in sentence_block


def test_the_protocol_page_says_redact_asks_the_policy_before_the_state() -> None:
    """The redact section of the protocol page says, after one marker, that the policy comes before the state of the row.

    The behaviour change is unreleased and visible over HTTP (404 instead of 403 for a redacted row), so the page needs
    the marker and the v0.20.0 answer.

    Mutations, each one alone: delete the marker from the paragraph; delete ``redact asks the policy before it reads
    the state of the row``; delete the sentence that says what v0.20.0 did; change ``404 over HTTP`` to ``403 over
    HTTP``; delete the sentence that says the refusal is recorded in the audit trail. Each fails this test.
    """

    page = _flat((ROOT / "docs/memory-operations-protocol.md").read_text(encoding="utf-8"))
    section = page[page.index("## redact") :]
    start = section.index(MARK)
    paragraph = section[start : start + 800]

    assert section.count(MARK) == 1
    assert "redact asks the policy before it reads the state of the row" in paragraph
    assert "(`not_found` over stdio, 404 over HTTP)" in paragraph
    assert "In v0.20.0 such a caller was refused (403 over HTTP) for the row" in paragraph
    assert "The refusal is recorded in the audit trail whichever answer the caller hears." in paragraph


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

    The entry also carries the follow-up fixes, so the one entry stays the whole story of the feature: the policy is
    asked before the state of the row, a missing entity is ``not_found``, and a PostgreSQL foreign key failure is
    ``precondition_failed``.

    Mutations, each one alone: delete the sentence ``v0.20.0 answered `tool_request_failed` for each of these
    cases``; move the entry under the v0.20.0 heading; add a second entry that names ``not_permitted``; change
    ``still gets one uniform `tool_request_failed``` to ``still gets one uniform `not_found```; change ``nine`` to
    ``eight``; delete the sentence about the other tools that take an id; delete ``A refusal is decided before the
    state of the row``; change ``400 from the others`` to ``404 from the others``; delete the sentence about
    PostgreSQL foreign key failures or the one about ``alice_explain`` with an ``entity_id``; delete the sentence that
    says a refused redact of an archived or redacted row is still recorded. Each fails this test.
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
    assert "A key-bound caller of `alice_explain` still gets one uniform `tool_request_failed`" in entry
    assert (
        "tells a key-bound caller an id its project scope refuses (`not_permitted`) from one that does not exist "
        "(`not_found`)"
    ) in entry
    assert "the nine rejected review arguments of the Postgres parity test" in entry
    assert "A refusal is decided before the state of the row." in entry
    assert (
        "a refused redact of an archived row leaves the same `agent.policy_blocked` audit row as in v0.20.0, and a "
        "refused redact of a redacted row now leaves one too, where v0.20.0 recorded none for a refused replay"
    ) in entry
    assert "`precondition_failed` reaches only a caller the policy allows" in entry
    assert "(404 from the review, redact and audit routes, 400 from the others)" in entry
    assert "the HTTP memory routes already do with 403 and 404" not in entry
    assert "`alice_explain` with an `entity_id`, `alice_state_at` and `alice_timeline` answer `not_found`" in entry
    assert "A PostgreSQL foreign key failure, a write that names a row the vault does not hold, answers `precondition_failed`" in entry
    assert "`not_permitted`" not in changelog.split("\n## v0.20.0")[1].split("\n## v0.19.2")[0]
