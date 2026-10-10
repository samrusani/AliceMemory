"""The pages say what a saved quote of a memory does, what a loop its caller wrote does, and what the owner reads after a redaction.

Unreleased (on main, not in v0.20.0). Three statements are pinned here, each on the pages that carry it:

* the quote a commit saved of a memory is withheld from a caller who may not read that memory (the security note, the tool
  reference and the changelog), with the refs it is read in, the doors that apply it, who keeps the quote and what is not judged;
* an open loop made over a memory with ``POST /v0/vnext/open-loops`` keeps the title and description its caller typed after the
  memory is redacted (the security note, the known limitations page and the tool reference);
* after a memory is redacted the owner and an unbound admin key read the rows it contained by id, in the artifact and project
  lists, in recent commits and in the dashboard, but not through recall, the context pack or the default context tree, and a
  request naming every sensitivity lists them (the same three pages).

Mutations, each one alone: delete the saved-quote sentence from the security note, the tool reference or the changelog; delete
the loop sentence or the context pack sentence from any of the three pages that carry it; change a key in the list of reference
keys on the tool reference, or in ``MEMORY_REFERENCE_KEYS`` in ``vnext_source_fence.py``; restore the sentence that said the
memory audit route returns what was stored; change ``a bare id in an entry of source_refs`` to say it names a memory.
"""
from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.vnext_source_fence import MEMORY_REFERENCE_KEYS

ROOT = Path(__file__).resolve().parents[2]
PAGES = {
    "security note": "docs/release/derived-labels-security-note-draft.md",
    "known limitations": "docs/alpha/known-limitations.md",
    "tool reference": "docs/alpha/mcp-tools.md",
    "changelog": "CHANGELOG.md",
}
MARK = "Unreleased (on main, not in v0.20.0):"


def _flat(name: str) -> str:
    return " ".join((ROOT / PAGES[name]).read_text(encoding="utf-8").split()).lower()


QUOTE_SENTENCE = {
    "security note": (
        "the quote a commit saved of a memory is withheld from a caller who may not read that memory, as the quote saved of a "
        "source already was"
    ),
    "changelog": (
        "the quote a commit saved of a memory is withheld from a caller who may not read that memory, as the quote saved of a "
        "source already was"
    ),
    "tool reference": "a quote saved of a memory follows that memory as a quote saved of a source follows its source",
}
SHAPES = (
    '`{"memory_id": "<id>", "quote": "..."}`',
    '`"memory:<id>"` followed by `{"quote": "..."}`',
)
KEEPS = {
    "security note": "the owner and an unbound `admin_agent` key have no limits and keep every quote",
    "tool reference": "the owner and an unbound `admin_agent` key have no limits and keep every quote",
    "changelog": "the owner and an unbound admin key keep the quote",
}
NOT_JUDGED = (
    "other text an agent writes in a ref (a `note`, `text` or `excerpt` field), a bare id in an entry of `source_refs` (it names "
    "a source and not a memory), and `alice_explain` for a call that declares a profile with no agent key, which was never held "
    "to it"
)
LOOP_SENTENCE = (
    "an open loop made with `post /v0/vnext/open-loops` over a memory keeps the title and description its caller typed, so words "
    "of the memory that the caller put there stay readable to every key that reads the loop after the memory is redacted"
)
LOOP_BULLET = (
    "an open loop made over a memory with `post /v0/vnext/open-loops` keeps the title and description its caller typed, so words "
    "of the memory typed there stay readable to every key that reads the loop after the memory is redacted"
)
LOOP_TOOLS = (
    "an open loop made over the memory with `post /v0/vnext/open-loops` is a different row: its title and description are text its "
    "caller typed and not a copy the system made, so they stay readable to every key that reads the loop after the memory is "
    "redacted, and the loop's `memory_id` reads `null`"
)
OWNER_READS = (
    "the owner and an unbound `admin_agent` key read the rows contained by it by id, in the artifact list and the project list, in "
    "the recent commits list and in the project dashboard, but not through recall, the context pack or the default context tree, "
    "because the default sensitivity filter of those three reads an unverified row as regulated"
)
OWNER_READS_BULLET = (
    "the owner and an unbound admin key read the rows contained by it by id, in the artifact and project lists, in recent commits "
    "and in the dashboard, but not through recall, the context pack or the default context tree, which read an unverified row as "
    "regulated; a request that names every sensitivity lists them"
)
OWNER_READS_TOOLS = (
    "the owner and an unbound admin key read the rows contained by it by id, in the artifact list and the project list, in the "
    "recent commits list and in the project dashboard, but not through recall, the context pack or the default context tree, "
    "because the default sensitivity filter of those three reads an unverified row as regulated"
)
NAMES_EVERY = (
    "a request that names every sensitivity (the `sensitivity_allowed` option of the context pack, the `sensitivity_allowed` query "
    "of the context tree) lists them. recall takes no sensitivity argument, so it never lists them"
)
RETIRED = (
    "the operator routes `get /v0/vnext/memories/{id}/audit`, `get /v0/vnext/memories/recent-commits` and "
    "`get /v0/vnext/sources/{id}`, which only the owner",
)


def test_three_pages_say_the_quote_saved_of_a_memory_follows_the_memory() -> None:
    for name, sentence in QUOTE_SENTENCE.items():
        text = _flat(name)
        assert sentence in text, name
        for shape in SHAPES:
            assert shape in text, (name, shape)
        assert KEEPS[name] in text, name
        assert "`null`" in text, name


def test_the_pages_say_the_quote_follows_the_memory_in_an_unreleased_paragraph() -> None:
    for name in QUOTE_SENTENCE:
        text = (ROOT / PAGES[name]).read_text(encoding="utf-8")
        paragraphs = [
            item for item in re.split(r"\n\s*\n|\n(?=- )", text) if QUOTE_SENTENCE[name] in " ".join(item.split()).lower()
        ]
        assert len(paragraphs) == 1, name
        assert MARK in " ".join(paragraphs[0].split()), name


def test_the_tool_reference_lists_the_reference_keys_the_reader_takes_and_what_it_does_not_judge() -> None:
    text = _flat("tool reference")
    named = re.search(r"under (`memory_id`.*?) at any depth", text)
    assert named is not None
    keys = set(re.findall(r"`([a-z_]+)`", named.group(1)))
    assert keys == set(MEMORY_REFERENCE_KEYS)
    for sentence in (
        "a memory is named by a `memory:` prefix in any case and any number, by an `alice://memories/<id>` url",
        "an id with no marker outside those keys names no memory",
        "the recent commits route and the legacy tool `alice_vnext_recent_memory_commits`, the memory audit route",
        "the workspace (its review memories, recent commits, inline confirmations and the source traces it embeds), the project dashboard",
        "a row that holds no quote costs no lookup",
        NOT_JUDGED,
    ):
        assert sentence in text, sentence
    assert NOT_JUDGED in _flat("security note")


def test_no_current_page_still_says_the_memory_audit_route_returns_what_was_stored() -> None:
    """The changelog keeps the entry of the release that said it, as a dated record; the pages that describe main do not."""

    for name in ("security note", "known limitations", "tool reference"):
        for sentence in RETIRED:
            assert sentence not in _flat(name), (name, sentence)


def test_three_pages_say_a_loop_its_caller_wrote_over_a_memory_stays_readable() -> None:
    assert LOOP_SENTENCE in _flat("security note")
    assert LOOP_BULLET in _flat("known limitations")
    assert LOOP_TOOLS in _flat("tool reference")
    assert "a loop that a producer made from the memory records it as an input, is a copy, and is contained with the reports" in _flat("security note")
    assert "a loop that a producer made from the memory records it as an input, is a copy, and is contained with the reports" in _flat("tool reference")
    assert "a loop that a producer made from the memory is contained with the reports" in _flat("known limitations")


def test_three_pages_say_what_the_owner_reads_after_a_redaction() -> None:
    assert OWNER_READS in _flat("security note")
    assert OWNER_READS_BULLET in _flat("known limitations")
    assert OWNER_READS_TOOLS in _flat("tool reference")
    for name in ("security note", "tool reference"):
        assert NAMES_EVERY in _flat(name), name
    assert "a request that names every sensitivity lists them" in _flat("known limitations")


def test_the_known_limitations_bullets_are_one_bullet_each_with_a_link_to_the_explanation() -> None:
    text = (ROOT / PAGES["known limitations"]).read_text(encoding="utf-8")
    for sentence in (LOOP_BULLET, OWNER_READS_BULLET):
        bullets = [item for item in re.split(r"\n(?=- )", text) if sentence in " ".join(item.split()).lower()]
        assert len(bullets) == 1
        assert "(mcp-tools.md#redacted-memories)" in bullets[0]
        assert bullets[0].startswith("- " + MARK)


def test_the_changelog_names_the_doors_and_says_no_migration_is_needed() -> None:
    text = _flat("changelog")
    for door in ("the recent commits list and legacy tool", "the memory audit", "`alice_explain`", "`alice_memory_review` detail", "the workspace"):
        assert door in text, door
    assert (
        "the audit route, `alice_explain`, the workspace, the project dashboard and the source traces apply the saved-quote reader "
        "for the first time"
    ) in text
    assert "see [saved quotes](docs/alpha/mcp-tools.md#saved-quotes)" in text
