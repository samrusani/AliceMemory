"""The pages say what a saved quote of a memory does, what a loop its caller wrote does, and what the owner reads after a redaction.

Unreleased (on main, not in v0.20.0). Three statements are pinned here, each on the pages that carry it:

* the quote a commit saved of a memory is withheld from a caller who may not read that memory (the security note, the tool
  reference and the changelog), with the refs it is read in, the ids no marker covers, the doors and feeds of events that apply it,
  who keeps the quote and what is not judged;
* an open loop made over a memory with ``POST /v0/vnext/open-loops`` keeps the title and description its caller typed after the
  memory is redacted (the security note, the known limitations page and the tool reference);
* after a memory is redacted the owner and an unbound admin key read the rows it contained by id, in the artifact and project
  lists, in recent commits and in the dashboard, but not through recall, the context pack or the default context tree, and a
  request naming every sensitivity lists them, in recall as in the pack (the same three pages);
* the update candidate the project update scan writes for a project that is itself contained is not contained (the security note).

Mutations, each one alone: delete the saved-quote sentence from the security note, the tool reference or the changelog; restore the
sentence that said the name of a field is kept as written; delete the sentence about the names of the fields from any of the three
pages; change a key in the list of source keys on the tool reference or the security note; delete
the loop sentence or the context pack sentence from any of the three pages that carry it; change a key in the list of reference
keys on the tool reference, or in ``MEMORY_REFERENCE_KEYS`` in ``vnext_source_fence.py``; restore the sentence that said the
memory audit route returns what was stored; restore the sentence that said an id with no marker names no memory; restore the
sentence that said recall takes no sensitivity argument; delete the sentence about the project update candidate; delete the
paragraph about the other rows, or its sentence about the doors, from the tool reference or the security note.
"""
from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.vnext_source_fence import MEMORY_REFERENCE_KEYS, PRODUCT_REF_KEYS

ROOT = Path(__file__).resolve().parents[2]
PAGES = {
    "security note": "docs/release/derived-labels-security-note-draft.md",
    "known limitations": "docs/alpha/known-limitations.md",
    "tool reference": "docs/alpha/mcp-tools.md",
    "changelog": "CHANGELOG.md",
    "importers": "docs/integrations/importers.md",
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
    "not judged: the text of an entry beside a refused ref that names a stored source or memory the caller may read, which keeps "
    "the strings under the fields the product writes, a `quote` or a string beside a `source_id` (the reader cannot tell whose "
    "words they are; every other field name goes from it with its value); words a writer typed in a field that is not a ref, such as the rationale and the "
    "contradiction refs of a commit or the comments of a rating; what the writer chose to encode in the shape of what stays (the "
    "length of a list, the order of its entries, a number or a boolean under a field the product writes); and `alice_explain` and "
    "the legacy `alice_vnext_memory_audit` tool for a call that declares a profile with no agent key, which were never held to it"
)
COMPANION = (
    "it takes the text of an entry beside such a ref that names no source and no memory the caller may read, as in `\"memory:<id>\"` "
    "followed by `{\"quote\": \"...\"}`, by a bare string, by a `text` field or by a nested list, and the same beside a refused "
    "source. an entry names a row when a reference position says so (`source_id`, `memory_id`, a `source:` or `memory:` ref) and the "
    "row is stored and readable by the caller, and only then does it keep its text; an id under `chunk_id`, in a sentence or in a "
    "quote, and an id that names no stored row, name nothing, so adding one to an entry keeps no quote"
)
COMPANION_NAMES = (
    "the words of a dropped name join the words withheld from the memory's links, and the entry beside a refused ref is held to "
    "the same rule on names whether or not it names a readable row: one that names nothing readable loses its text as well, and "
    "one that names a stored source or memory the caller may read keeps its text and loses every name the product does not "
    "write, with its value"
)
QUOTE_STRUCTURE = (
    "a quote or an excerpt that is an object or a list is read like the rest of the ref, and an id typed in the text of a quote or "
    "of the commit's excerpt (`memory:<id> words`) takes the quote when it names a stored memory the caller may not read"
)
ROWS_PARAGRAPH = (
    "the same rule holds the other rows that keep a structure their writer chose. a source made by the agent-output ingest keeps "
    "the `source_refs` it was sent in its `metadata_json` and again in `metadata_json.raw_payload`"
)
ROWS_DOORS = (
    "`get /v0/vnext/sources/{id}`, the review and delete answers of a source, `get /v0/vnext/traces/sources/{id}`, "
    "`get /v0/vnext/traces/artifacts/{id}`, `post /v0/vnext/context-packs`, `get /v0/vnext/workspace` (its sources and the "
    "traceability items built from them) and `get /v0/vnext/connectors/{name}/status` now show a key with limits both copies of "
    "the refs with the ids and a `null` quote, and drop an entry that names a source the key may not read; the stored row is not "
    "changed"
)
ROWS_OTHERS = (
    "the metadata of a quality rating (the artifact trace, `get /v0/vnext/quality-evals` and the workspace) and the allowed "
    "sources and scope of a queued task (the workspace) are held to the same rule"
)
ROWS_DERIVED = (
    "the artifact a queue worker makes prints the scope and the allowed sources of its task, and the report of a consolidation run "
    "copies the refs of its members, so each is now derived from the memories and sources those structures name, in any spelling: "
    "redacting a memory takes the artifact out of every route of a key with limits, and a report whose refs name a confidential "
    "memory is labelled over it. a report or an artifact made by an earlier release keeps what it printed"
)
ROWS_CLI = (
    "`alicebot vnext memories audit` with an agent key now authorizes the memory and its replacement chain as the audit route does "
    "and holds the result to the reader; until now it returned the audit as stored to a key that reached it"
)
CHANGELOG_ROWS = (
    "an entry beside such a ref that names nothing the caller may read is held to the same rule (a bare string and a `text` field go "
    "with the `quote`), one that names a stored source or memory the caller may read keeps its text and loses the names the "
    "product does not write with their values, and so are the refs a source made by the agent-output ingest keeps (the source routes, the source trace, the "
    "artifact trace, the context pack, the workspace and the connector status), the metadata of a quality rating and the allowed "
    "sources and scope of a queued task; the report of a consolidation run and the artifact a queue worker makes are derived from "
    "the memories their refs and tasks name, and `alicebot vnext memories audit` with an agent key applies the reader"
)
STRINGS = (
    "a refused memory takes the strings of every entry that names it: the `quote` and the `conversation_excerpt` become `null`, "
    "and so does any other text in the entry (a sentence that has the id in it, or a string in a `memory_id`, `source_id`, `id` or "
    "`ref` field that holds more than an id), while the entry keeps its ids, its numbers and its booleans under the fields the "
    "product writes; an id here is a string made of references and nothing else (`<id>`, `memory:<id>`, a list of "
    "them), and a `#` and what follows it after a reference is dropped, so `memory:<id>#chunk-1` reads `memory:<id>`"
)
FIELD_NAMES = (
    "the names of the fields are withheld with their values. a name holds words whatever its value is, so the entry keeps a field "
    "only when its name is one the product writes"
)
FIELD_NAMES_GOES = (
    "and drops every other field with its value, at any depth, inside json text and whatever the value is (`null`, a number, a "
    "boolean, a string, an object): a `text`, `excerpt`, `snippet`, `note` or `page` field goes whole, and so does an id that sits "
    "under a name such as `origin`, `ref_id` or `evidence`"
)
FIELD_NAMES_WRITE_SIDE = (
    "the commit route, the memory proposal and the agent-output ingest still accept any name in an object ref, so the reader is the "
    "fence for new rows and for the rows stored by earlier releases"
)
FIELD_NAMES_CHANGELOG = (
    "the names of the fields are withheld with their values: the entry keeps a field only when its name is one the product writes"
)
COST = (
    "a ref that lists tens of thousands of ids is read in several passes and its ids are looked up in slices of 500, so each page "
    "that returns the commit takes longer in proportion to the size of the ref, bounded by the body limit: about eight times as long "
    "at 100,000 ids on sqlite, against a few milliseconds more for a page of ordinary commits"
)
WORKSPACE_DOORS = (
    "the workspace (its review memories, recent commits, inline confirmations, recent events, agent activity and the source "
    "traces it embeds), the project dashboard, the source trace routes with their events"
)
WORKSPACE_DOORS_NOTE = (
    "the workspace (its review memories, recent commits, inline confirmations, recent events, agent activity and the source "
    "traces it embeds), the project dashboard and the source trace routes with their events now apply the reader"
)
UNMARKED_TOOLS = (
    "every other id in the ref, outside the text of a `quote` or `conversation_excerpt`, is looked up as well, because it may "
    "name a memory: a sentence such as `memory <id>` or `see memory: <id>`, a url, an id under `memory`, `origin`, `ref_id`, "
    "`parent_memory_id` or `supersedes`. the caller is refused the quote when such an id names a memory the store holds a row "
    "for, a redacted or archived one included, that the caller may not read. an id that names no memory row is left alone, "
    "because it may be a source, a chunk or a session, and so is the id of a memory that was removed from the database, which no "
    "door of the product does"
)
UNMARKED_NOTE = (
    "and every other id in the ref, outside the text of a `quote` or `conversation_excerpt`, is looked up as well because it may "
    "name a memory (`memory <id>`, a url, an id under `memory`, `origin` or `supersedes`): the caller is refused the quote when "
    "such an id names a memory the store holds a row for, a redacted or archived one included, that the caller may not read, and "
    "an id that names no memory row is left alone because it may be a source, a chunk or a session"
)
EVENTS_TOOLS = (
    "a commit that is confirmed or edited appends an event whose payload holds the refs and the quote the commit was sent with, "
    "and the three feeds of events (the recent events and the agent activity of the workspace, and the events of a source trace) "
    "hold that payload to the same rule as the events of one memory in its audit; the agent activity lists the events an agent "
    "key caused, so a commit that an agent key confirms is in it as well as among the recent events"
)
EVENTS_NOTE = (
    "(a commit that is confirmed or edited appends an event whose payload holds the refs and the quote it was sent with, and the "
    "three feeds of events, the recent events and the agent activity of the workspace and the events of a source trace, hold that "
    "payload to the same rule as the events of one memory in its audit; an agent key that confirms a commit appends the same "
    "event, which the agent activity lists)"
)
PROJECT_CANDIDATE = (
    "the update candidate that the project update scan writes for a project that is itself contained is not contained. a project "
    "is contained when its state was copied from a memory that is then redacted. the scan takes the newest active project, and "
    "the candidate it writes records the sources and memories of the project as its inputs and not the project"
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
    "a request that names every sensitivity lists them: the `sensitivity_allowed` argument of `alice_recall` and of the context "
    "pack, and the `sensitivity_allowed` query of the context tree"
)
RETIRED_RECALL = "recall takes no sensitivity argument"
RETIRED_UNMARKED = "an id with no marker outside those keys names no memory"
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
        UNMARKED_TOOLS,
        STRINGS,
        EVENTS_TOOLS,
        "the recent commits route and the legacy tool `alice_vnext_recent_memory_commits`, the memory audit route",
        WORKSPACE_DOORS,
        "a row that holds no words to withhold (no quote, no excerpt, no `#` fragment, no string beside an id and no field whose "
        "name the product does not write) costs no lookup",
        NOT_JUDGED,
        FIELD_NAMES,
        FIELD_NAMES_GOES,
        FIELD_NAMES_WRITE_SIDE,
        COST,
        COMPANION,
        COMPANION_NAMES,
        QUOTE_STRUCTURE,
    ):
        assert sentence in text, sentence
    note = _flat("security note")
    for sentence in (
        NOT_JUDGED, STRINGS, FIELD_NAMES, FIELD_NAMES_GOES, FIELD_NAMES_WRITE_SIDE, UNMARKED_NOTE, EVENTS_NOTE, COST,
        WORKSPACE_DOORS_NOTE, COMPANION, COMPANION_NAMES, QUOTE_STRUCTURE,
    ):
        assert sentence in note, sentence


def test_two_pages_say_which_other_rows_are_held_to_the_rule_in_an_unreleased_paragraph_of_their_own() -> None:
    """The refs a source made by the agent-output ingest keeps, the metadata of a quality rating, the allowed sources and scope of a
    queued task, the artifact a queue worker makes, the report of a consolidation run and the command line audit are held to the
    caller by the same rule. The tool reference and the security note say so in one paragraph each, with the doors, what is derived
    and what the stored row keeps, and the changelog says it in the entry of the saved quote.

    Mutations: delete the paragraph from either page; delete the sentence about the doors; restore the sentence that said the
    source route returns what was stored; delete the clause of the changelog entry.
    """

    for name in ("tool reference", "security note"):
        text = _flat(name)
        for sentence in (ROWS_PARAGRAPH, ROWS_DOORS, ROWS_OTHERS, ROWS_DERIVED, ROWS_CLI):
            assert sentence in text, (name, sentence)
        paragraphs = [
            item
            for item in re.split(r"\n\s*\n|\n(?=- )", (ROOT / PAGES[name]).read_text(encoding="utf-8"))
            if ROWS_PARAGRAPH in " ".join(item.split()).lower()
        ]
        assert len(paragraphs) == 1, name
        assert MARK in " ".join(paragraphs[0].split()), name
        assert "returns what was stored subject to the route's current read fence" not in text, name
    assert CHANGELOG_ROWS in _flat("changelog")


def test_the_pages_list_the_field_names_a_withheld_entry_keeps_as_the_reader_does() -> None:
    """The tool reference and the security note name the fields a withheld entry keeps: the memory keys (checked above), the source
    keys, ``id``, ``ref``, ``quote`` and ``conversation_excerpt``. The names they print are the ones in ``PRODUCT_REF_KEYS``, no
    more and no fewer, so a name added to or removed from the reader changes the pages with it.

    Mutation: change a key in the list of source keys on the tool reference or on the security note (the set differs).
    """

    for name in ("tool reference", "security note"):
        text = _flat(name)
        listed = re.search(r"the source keys (.*?), in any case\)", text)
        assert listed is not None, name
        printed = set(re.findall(r"`([a-z_]+)`", listed.group(1)))
        assert printed | set(MEMORY_REFERENCE_KEYS) == set(PRODUCT_REF_KEYS), (name, printed ^ set(PRODUCT_REF_KEYS))
    assert FIELD_NAMES_CHANGELOG in _flat("changelog")


IMPORTERS_QUOTE = (
    "an id inside the text of a `quote` or `conversation_excerpt` field names nothing (a quote that is an object or a list is a "
    "structure, and the ids it holds under a reference field count), and a `memory:` reference names a memory"
)


def test_the_importers_page_says_a_quote_that_is_a_structure_names_the_source_it_holds() -> None:
    """A replacement, ``sources delete`` and ``sources prune`` count a memory as citing a source by the rule the saved-quote reader
    names it by, and that rule reads the ids in a quote that is an object or a list. The importers page says so next to the rule
    for quote text, and no longer says that any id inside a ``quote`` field names nothing.

    Mutation: restore the sentence that said an id inside a ``quote`` or ``conversation_excerpt`` field names nothing.
    """

    text = _flat("importers")
    assert IMPORTERS_QUOTE in text
    assert "an id inside a `quote` or `conversation_excerpt` field names nothing" not in text


def test_no_current_page_still_says_the_memory_audit_route_returns_what_was_stored() -> None:
    """The changelog keeps the entry of the release that said it, as a dated record; the pages that describe main do not."""

    for name in ("security note", "known limitations", "tool reference"):
        for sentence in RETIRED:
            assert sentence not in _flat(name), (name, sentence)


def test_no_current_page_still_says_recall_takes_no_sensitivity_argument_or_that_an_unmarked_id_names_no_memory() -> None:
    """``alice_recall`` has a ``sensitivity_allowed`` argument and lists a contained row when it names every sensitivity, and the
    reader looks up an id that no marker says is a memory. The pages that describe main say neither of the old sentences."""

    for name in ("security note", "known limitations", "tool reference", "changelog"):
        text = _flat(name)
        assert RETIRED_RECALL not in text, name
        assert RETIRED_UNMARKED not in text, name


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


def test_the_security_note_says_the_update_candidate_of_a_contained_project_is_not_contained() -> None:
    """The expected failure ``test_the_update_candidate_of_a_contained_project_is_contained`` says the security note lists the gap.

    Mutation: delete the paragraph from the security note.
    """

    note = _flat("security note")
    assert PROJECT_CANDIDATE in note
    assert "a permanent test marks this as an expected failure" in note
    paragraph = [
        item
        for item in re.split(r"\n\s*\n", (ROOT / PAGES["security note"]).read_text(encoding="utf-8"))
        if PROJECT_CANDIDATE in " ".join(item.split()).lower()
    ]
    assert len(paragraph) == 1 and MARK in " ".join(paragraph[0].split())


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
    assert "the recent events and the agent activity of the workspace and the events of a source trace hold the payload of a confirmed commit" in text
    assert "a `#` fragment after a reference is dropped, so `memory:<id>#chunk-1` reads `memory:<id>`" in text
    assert "an id the reader has no marker for (`memory <id>`, a url, an id under `origin`) is looked up as a possible memory" in text
    assert (
        "the audit route, `alice_explain`, the workspace, the project dashboard and the source traces apply the saved-quote reader "
        "for the first time"
    ) in text
    assert "see [saved quotes](docs/alpha/mcp-tools.md#saved-quotes)" in text
