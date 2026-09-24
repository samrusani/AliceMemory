"""Import a document, then read it back through the tools an agent actually calls.

The defect these tests pin, found 2026-08-17 by inspecting a real 226-document
vault import: capture was write-only. A user could import their notes and then
retrieve nothing from them by any route the MCP surface offered.

Three independent causes, each sufficient on its own:

1. A packed source carried ``metadata_json``, and capture stores the entire
   document in there as evidence. ``estimate_item_tokens`` JSON-dumps the item
   to price it, so a 235KB source was charged roughly 59k tokens against a 50k
   ceiling, was rejected, and *latched* the pack's truncation flag, dropping
   every later section too. Above roughly 30k characters sources vanished, and
   no setting could raise the ceiling far enough.
2. The MCP compaction emitted only id/type/title/date/domain/sensitivity, so a
   source that did survive the budget reached the agent as a bibliography entry.
   Retrieval ranked the right chunks and then threw the text away.
3. ``alice_recall`` searched memories only. Since capture stores candidates and
   candidates are unsearchable by design, the natural agent sequence (import,
   then recall) answered ``count=0`` for content the store held.

The benchmark could not see any of this: the LongMemEval harness renders
excerpts itself by calling ``list_source_chunks`` directly, a capability the
shipped tools never exposed. So 81.2% measured a retrieval path no user had.

Each test below is one of those causes. They assert on what an agent receives,
not on internals, because every one of these defects was invisible from inside
the component that caused it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

USER_ID = "00000000-0000-0000-0000-000000000001"

QUOTE = "Discipline is the art of not betraying yourself."

# Shaped from the real vault. The "## Related" block is the trap: it repeats
# every query term inside wikilink paths, so a naive `term in text` scorer
# ranks it above the note that actually contains the sentence.
QUOTE_NOTE = f"""---
Added: 2026-07-28
Tags: #quote #discipline #habits
---

# "{QUOTE[:-1]}"

**Theme:** Discipline and Self-Mastery
**Author:** Unknown

> {QUOTE}

## Why it resonates

Keeping a promise to yourself is the whole of it.

## Related

- [[themes/discipline-and-self-mastery/discipline-is-the-art-of-not-betraying-yourself]]
- [[themes/discipline-and-self-mastery/discipline-is-not-betraying-yourself-again]]
- [[themes/discipline-and-self-mastery/the-art-of-discipline-and-not-betraying]]
- [[themes/discipline-and-self-mastery/betraying-yourself-is-the-opposite-of-discipline]]
"""


def _fresh_context(tmp_path: Path):
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _capture(context, raw_text: str, *, title: str = "Vault") -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": raw_text,
            "title": title,
            "domain": "personal",
            "sensitivity": "private",
        },
    )


def _recall(context, query: str) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name="alice_recall", arguments={"query": query})


def _pack(context, query: str) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name="alice_context_pack", arguments={"query": query})


# --------------------------------------------------------------------------
# Cause 3: recall never looked at sources at all.
# --------------------------------------------------------------------------


def test_recall_returns_the_imported_document_it_just_captured(tmp_path: Path) -> None:
    """Import, then recall. The sequence every agent performs first."""

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    payload = _recall(context, QUOTE)
    sources = payload.get("sources") or []

    assert sources, (
        "alice_recall returned nothing for a document captured moments earlier. "
        "Capture is write-only again."
    )
    assert payload["source_count"] == len(sources)
    assert any(QUOTE in (source.get("excerpt") or "") for source in sources), (
        "a source came back but its excerpt does not contain the sentence that was searched for"
    )


def test_include_sources_false_omits_sources(tmp_path: Path) -> None:
    """The opt-out has to work, or it is a lie in the schema.

    An agent that only wants asserted facts must be able to say so and get
    no sources and no source_count. The framing sentence is still once on
    the result. Untested until review pointed it out: the flag was written,
    documented, and never exercised.
    """

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    payload = _recall(context, QUOTE)
    assert payload.get("sources"), "the fixture must retrieve sources for the opt-out to mean anything"

    from alicebot_api.mcp.registry import call_mcp_tool

    opted_out = call_mcp_tool(
        context,
        name="alice_recall",
        arguments={"query": QUOTE, "include_sources": False},
    )

    assert "sources" not in opted_out
    assert "source_count" not in opted_out
    assert set(opted_out) == {"framing", "query", "results", "count"}


def test_include_sources_is_declared_in_the_schema(tmp_path: Path) -> None:
    """`additionalProperties: false` means an undeclared argument is rejected.

    So the flag working in the handler is only half of it. If the schema does
    not declare it, an agent passing it gets an error instead of the documented
    behaviour, and a schema-driven client never learns the option exists.
    """

    from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS

    recall = next(tool for tool in _CORE_TOOL_DEFINITIONS if tool["name"] == "alice_recall")
    schema = recall["inputSchema"]

    assert schema["additionalProperties"] is False, (
        "this test's premise changed; an undeclared argument is no longer rejected"
    )
    assert "include_sources" in schema["properties"]
    assert schema["properties"]["include_sources"]["type"] == "boolean"


def test_recall_keeps_asserted_facts_and_read_only_material_apart(tmp_path: Path) -> None:
    """The labelling is the safety property, not decoration.

    Source text is material to read and quote. It must never arrive looking like
    a fact Alice stands behind, and capture must not have promoted anything.
    """

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    payload = _recall(context, QUOTE)

    assert payload["count"] == 0, (
        "capture promoted a memory to searchable. Candidates must stay unsearchable "
        "until a reviewer promotes them; this path must not have become a way around that."
    )
    for source in payload["sources"]:
        assert source.get("excerpt_kind") == "imported_source_material"
    assert "sources" in payload and "results" in payload, "the two channels were merged"


# --------------------------------------------------------------------------
# Cause 1: the duplicated document blew the token budget.
# --------------------------------------------------------------------------


def test_a_large_import_does_not_truncate_the_context_pack(tmp_path: Path) -> None:
    """The latching failure: one fat source used to drop every later section.

    Sized past the point where the old code broke. The document is stored once
    as chunks and once more inside metadata_json; pricing the second copy is
    what blew a 50k ceiling on a single item.
    """

    context = _fresh_context(tmp_path)
    big = "\n\n".join(
        f"## Note {index}\n\n> {QUOTE}\n\nSome supporting prose for note {index}."
        for index in range(400)
    )
    assert len(big) > 30_000, "fixture is smaller than the size where sources used to disappear"
    _capture(context, big, title="Big vault")

    pack = _pack(context, QUOTE)

    assert pack.get("sources"), "a large import packs zero sources, the budget defect is back"
    budget = pack.get("budget") or {}
    assert budget.get("truncated") is not True, (
        "one oversized source latched the pack's truncation flag again"
    )


def _compile_pack_uncompacted(tmp_path: Path, raw_text: str, query: str) -> dict:
    """Compile through the service, below the MCP layer.

    This distinction is the whole point of the test that follows. MCP compaction
    keeps an allowlist of six fields, so ``metadata_json`` can never appear in a
    tool payload whether or not anything removed it. Asserting up there proves
    nothing: the first version of this test passed with the fix reverted.

    The token budget runs *below* compaction, on the service's own pack, and
    that is where a duplicated document costs 59k tokens.
    """

    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_capture import VNextCaptureService
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        VNextCaptureService(store, actor_type="user_or_system").capture_text(
            raw_text, title="Vault", domain="personal", sensitivity="private"
        )
        return VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query=query)
        )


def test_a_packed_source_never_carries_the_whole_document(tmp_path: Path) -> None:
    """The mechanism, pinned where it lives, so a regression is diagnosable.

    Review caught the first fix dropping ``metadata_json`` wholesale, which
    silently un-dated every imported source because ``_source_event_time`` reads
    ``session_date`` out of it. Only the duplicated document may be removed, so
    this asserts both halves: the document is gone, the rest of the blob stays.
    """

    from alicebot_api.vnext_retrieval import SOURCE_EXCERPT_MAX_CHARS, estimate_item_tokens

    pack = _compile_pack_uncompacted(tmp_path, QUOTE_NOTE, QUOTE)
    sources = pack.get("sources") or []
    assert sources, "the service packed no sources at all"

    for source in sources:
        metadata = source.get("metadata_json")
        if isinstance(metadata, dict):
            assert "raw_text" not in metadata, (
                "the packed source still carries the whole document, which is what "
                "priced a single item past the entire pack budget"
            )
        # The cost, not just the shape: pricing is what actually broke.
        assert estimate_item_tokens(source) < 5_000, (
            "a packed source is expensive enough to threaten the budget again"
        )
        excerpt = source.get("excerpt")
        if excerpt:
            assert len(excerpt) <= SOURCE_EXCERPT_MAX_CHARS


def test_an_imported_source_keeps_its_own_date_instead_of_todays(tmp_path: Path) -> None:
    """The regression review caught: metadata_json must survive, minus the document.

    Rewritten 2026-08-17 after review showed the first version could not fail.
    That version asserted ``_source_event_time(...) is not None`` on an
    MCP-compacted source. Compaction keeps ``captured_at`` and drops
    ``metadata_json`` regardless, and ``_source_event_time`` falls back to
    ``captured_at`` last, which capture always sets. So it returned a datetime
    even with the whole blob stripped: the second vacuous test in this file, and
    the same sandwich as the first.

    The property is not "has a date". It is "keeps its OWN date". Losing
    ``metadata_json`` dates every imported historical document as today, which
    silently corrupts the derived timeline, and that is invisible to any
    not-None check. So this asserts below compaction, on a source whose only
    honest date lives in metadata, and pins the value rather than its presence.
    """

    from alicebot_api.vnext_retrieval import (
        SOURCE_EVENT_METADATA_KEYS,
        VNextRetrievalService,
        _source_event_time,
    )

    metadata_date_key = SOURCE_EVENT_METADATA_KEYS[0]
    imported = {
        "id": "source-1",
        "title": "A note written years ago",
        # The whole document, duplicated into metadata as capture does. This is
        # the field that must go, and the only one.
        "metadata_json": {
            "raw_text": QUOTE_NOTE * 200,
            metadata_date_key: "2019-03-04T00:00:00+00:00",
        },
        # Ingest write time. Present, parseable, and the wrong answer.
        "captured_at": "2026-08-17T12:00:00+00:00",
        "source_created_at": None,
    }

    service = VNextRetrievalService.__new__(VNextRetrievalService)
    service._winning_chunk_text = {}

    class _NoChunkListing:
        """No chunk capability, so the no-winner fallback stays out of the way.
        This test is about the date, not about the excerpt."""

    service.store = _NoChunkListing()  # type: ignore[assignment]
    packed = service._packable_source(imported, query=QUOTE)

    stamped = _source_event_time(packed)

    assert stamped is not None, "the packed source lost its date entirely"
    assert stamped.year == 2019, (
        f"the imported source was dated {stamped.isoformat()} instead of 2019. "
        "metadata_json was stripped past raw_text, so every imported document "
        "now claims it was written on the day it was imported."
    )
    # And the document itself is still gone, which is the other half.
    assert "raw_text" not in packed.get("metadata_json", {})


# --------------------------------------------------------------------------
# Cause 2: the agent received a bibliography entry with no text.
# --------------------------------------------------------------------------


def test_the_mcp_surface_delivers_the_excerpt_not_just_a_citation(tmp_path: Path) -> None:
    """Compaction runs between retrieval and the agent, and used to drop the text."""

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    pack = _pack(context, QUOTE)
    sources = pack.get("sources") or []
    assert sources, "no sources in the pack at all"

    with_text = [source for source in sources if source.get("excerpt")]
    assert with_text, (
        "every packed source arrived as a citation with no text. The compaction "
        "field list dropped the excerpt again."
    )
    for source in with_text:
        assert source["excerpt_kind"] == "imported_source_material"


# --------------------------------------------------------------------------
# The windowing case review asked for by name.
# --------------------------------------------------------------------------


def test_the_excerpt_shows_the_quote_not_the_related_links_block(tmp_path: Path) -> None:
    """A wikilink block repeating every query term must not win the window.

    The first draft re-derived a "best" chunk here with ``term in text`` and
    picked the ``## Related`` block, because the query terms all appear inside
    its URLs. Retrieval had already ranked a winner; the fix keeps it and scores
    lines by token overlap rather than substring.
    """

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    payload = _recall(context, QUOTE)
    sources = payload.get("sources") or []
    assert sources

    excerpt = sources[0].get("excerpt") or ""
    assert QUOTE in excerpt, f"the excerpt does not contain the quote. Got: {excerpt[:200]!r}"

    link_lines = [line for line in excerpt.splitlines() if line.strip().startswith("- [[")]
    quote_lines = [line for line in excerpt.splitlines() if QUOTE in line]
    assert quote_lines, "no line of the excerpt carries the sentence"
    assert len(link_lines) < len(excerpt.splitlines()), (
        "the excerpt is nothing but the Related links block"
    )


def test_every_ranked_source_reaches_the_agent_carrying_text(tmp_path: Path) -> None:
    """A source good enough to rank must not arrive as a bare citation.

    Only the chunk-FTS stage records a winning chunk. Sources that enter through
    the provenance or title/recency lists have none, and measured on 55 ranked
    sources from real LongMemEval questions that was 56% of them: ranked,
    packed, and empty. Coverage went 44% -> 100% when the fallback landed.
    """

    context = _fresh_context(tmp_path)
    # Several documents, so the pack fills from more than the one lexical hit.
    _capture(context, QUOTE_NOTE, title="Discipline")
    for index in range(6):
        _capture(
            context,
            f"# Note {index}\n\nA separate note about habits and standards, number {index}.\n",
            title=f"Note {index}",
        )

    pack = _pack(context, QUOTE)
    sources = pack.get("sources") or []
    assert sources, "no sources ranked at all"

    without_text = [source for source in sources if not source.get("excerpt")]
    assert not without_text, (
        f"{len(without_text)} of {len(sources)} ranked sources reached the agent "
        "with no text. The fallback for sources the chunk-FTS stage never ranked is gone."
    )


def test_the_fallback_still_scores_lines_not_substrings(tmp_path: Path) -> None:
    """The fallback must not reintroduce the scorer review rejected.

    Re-deriving a best chunk is only safe here because there is no ranking to
    discard. It is still only safe if it scores token overlap over lines. The
    substring form picks the wikilink block every time.
    """

    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")

    class _ChunkStore:
        """Two chunks: one is the sentence, one is the link block that beats it
        on substring matching and loses on token overlap."""

        def list_source_chunks(self, source_id: str) -> list[dict[str, object]]:
            return [
                {
                    "text": "## Related\n"
                    "- [[discipline-is-the-art-of-not-betraying-yourself]]\n"
                    "- [[the-art-of-discipline-and-not-betraying-yourself]]\n",
                    "chunk_index": 0,
                },
                {"text": f"> {QUOTE}\n", "chunk_index": 1},
            ]

    with sqlite_user_connection(database, USER_ID) as connection:
        service = VNextRetrievalService(SQLiteVNextStore(connection, USER_ID))
        service.store = _ChunkStore()  # type: ignore[assignment]
        winner = service._best_chunk_without_a_winner("source-1", query=QUOTE)

    assert winner is not None
    assert QUOTE in winner, (
        f"the fallback picked the Related links block over the sentence: {winner!r}"
    )


POINTER_LINES = (
    "- [[discipline-is-the-art-of-not-betraying-yourself]]",
    "* [[discipline-is-the-art-of-not-betraying-yourself]]",
    "+ [[discipline-is-the-art-of-not-betraying-yourself]]",
    "1. [[discipline-is-the-art-of-not-betraying-yourself]]",
    "12) [[discipline-is-the-art-of-not-betraying-yourself]]",
    "1.[[discipline-is-the-art-of-not-betraying-yourself]]",
    "- [ ] [[discipline-is-the-art-of-not-betraying-yourself]]",
    "- [x] [[discipline-is-the-art-of-not-betraying-yourself]]",
    "![[discipline-is-the-art-of-not-betraying-yourself]]",
    "- [[discipline-is-the-art]] [[of-not-betraying-yourself]]",
    "- [[discipline-is-the-art]], [[of-not-betraying-yourself]]",
    "- [Discipline is the art of not betraying yourself](notes/discipline.md)",
    "- [Discipline][discipline-is-the-art-of-not-betraying-yourself]",
    "[discipline]: https://example.com/discipline-is-the-art",
    "- [D](notes/discipline_(draft).md)",
    "• [[discipline-is-the-art-of-not-betraying-yourself]]",
    "  - [[discipline-is-the-art-of-not-betraying-yourself]]",
    "> - [[discipline-is-the-art-of-not-betraying-yourself]]",
    "![alt text](images/discipline.png)",
    "https://example.com/discipline-is-the-art-of-not-betraying-yourself",
)

READABLE_LINES = (
    "> Discipline is the art of not betraying yourself.",
    "Discipline is the art of not betraying yourself. See [[discipline]].",
    "- [[discipline]] is the art of not betraying yourself.",
    "1. Discipline is the art of not betraying yourself.",
    "2. The capacity to be alone is the capacity to love.",
    "- [ ] Ship the retrieval fix before Friday.",
    "See https://example.com/x for the full argument about discipline.",
    "I read [the note](notes/d.md) yesterday and it changed how I work.",
    "## Related",
    "---",
)


@pytest.mark.parametrize("line", POINTER_LINES)
def test_a_line_that_is_only_a_pointer_scores_zero(line: str) -> None:
    """Every list shape a real vault writes, not just the one I first tested.

    The first version of this guard used a character class for the marker
    prefix, which cannot express "1." or "- [ ]" as a unit. So an ORDERED
    "## Related" list still beat the quote it linked to, in exactly the way the
    unordered one used to. Obsidian writes ordered lists, "![[embed]]"
    transclusions and several links per row, and this branch exists to make an
    Obsidian vault readable.
    """

    from alicebot_api.vnext_retrieval import _line_overlap_score, _tokens

    query_tokens = _tokens(QUOTE)

    assert _line_overlap_score(line, query_tokens) == 0, (
        f"{line!r} is a pointer, not readable content, but it scored. It can "
        "out-rank the sentence it points at, because a wikilink slug tokenises "
        "to the same words as the sentence."
    )


@pytest.mark.parametrize("line", READABLE_LINES)
def test_readable_lines_are_never_silenced(line: str) -> None:
    """The other half. Over-zealous matching would hide real content.

    A marker with no link after it ("- [ ] Ship the fix") is prose. A sentence
    that merely mentions a link is prose. Zeroing those would be a worse bug
    than the one being fixed, because it removes text rather than reordering it.
    """

    from alicebot_api.vnext_retrieval import _LINK_ONLY_LINE

    assert not _LINK_ONLY_LINE.match(line), f"{line!r} is readable and was treated as a pointer"


@pytest.mark.parametrize(
    "marker", ("-", "*", "+", "1.", "2)", "- [ ]", "!", "•")
)
def test_a_related_block_never_wins_whatever_marker_it_uses(
    tmp_path: Path, marker: str
) -> None:
    """The end-to-end version, parametrised over marker shapes.

    Review noted the original fixture hardcoded "- [[", so it proved the fix for
    exactly one of the forms a vault writes.
    """

    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")

    slug = "discipline-is-the-art-of-not-betraying-yourself"
    prefix = "" if marker == "!" else f"{marker} "

    class _ChunkStore:
        def list_source_chunks(self, source_id: str) -> list[dict[str, object]]:
            return [
                {
                    "text": "## Related\n"
                    f"{prefix}{'!' if marker == '!' else ''}[[{slug}]]\n"
                    f"{prefix}{'!' if marker == '!' else ''}[[the-art-of-{slug}]]\n",
                    "chunk_index": 0,
                },
                {"text": f"> {QUOTE}\n", "chunk_index": 1},
            ]

    with sqlite_user_connection(database, USER_ID) as connection:
        service = VNextRetrievalService(SQLiteVNextStore(connection, USER_ID))
        service.store = _ChunkStore()  # type: ignore[assignment]
        winner = service._best_chunk_without_a_winner("source-1", query=QUOTE)

    assert winner is not None
    assert QUOTE in winner, (
        f"with marker {marker!r} the Related block beat the sentence: {winner!r}"
    )


_FILLER = [f"Some unrelated prose line number {index} padding out the chunk." for index in range(40)]
_ANCHOR = f"{QUOTE} Said plainly, and at some length, right here."


@pytest.mark.parametrize("position", (0, 1, 20, 39, 40))
def test_the_excerpt_always_contains_the_line_it_was_selected_for(position: int) -> None:
    """The window used to drop its own anchor.

    Growth is bidirectional but truncation was head-anchored: join lines lo..hi,
    then cut with ``window[:max_chars]``. Whenever the best-matching line sat
    near the END of the chunk, the excerpt kept the padding above it and cut the
    anchor out. Reproduced on a 2,783-character chunk at a 300-character budget,
    where the excerpt was entirely filler.

    An excerpt that drops the line it was chosen for is worse than no excerpt,
    because it reads like an answer.
    """

    from alicebot_api.vnext_retrieval import _query_anchored_window

    lines = [*_FILLER[:position], _ANCHOR, *_FILLER[position:]]
    chunk = "\n".join(lines)
    assert len(chunk) > 300, "the fixture must exceed the budget or nothing is windowed"

    excerpt = _query_anchored_window(chunk, query=QUOTE, max_chars=300)

    assert _ANCHOR in excerpt, (
        f"anchor at line {position} was cut out of its own excerpt. Got: {excerpt[:120]!r}"
    )
    assert len(excerpt) <= 300 + 1, "the window overshot its budget"


def test_a_single_line_longer_than_the_budget_is_still_trimmed_to_it() -> None:
    from alicebot_api.vnext_retrieval import _query_anchored_window

    one_line = f"{QUOTE} " + ("padding words that go on and on " * 60)

    excerpt = _query_anchored_window(one_line, query=QUOTE, max_chars=300)

    assert len(excerpt) <= 301
    assert excerpt.startswith("Discipline")


@pytest.mark.parametrize(
    ("label", "text"),
    (
        ("japanese", "これは日本語の非常に長い行です。" * 60),
        ("url", "see https://example.com/" + ("a" * 2000)),
        ("no-space", "x" * 2000),
    ),
)
def test_text_without_spaces_still_yields_a_usable_excerpt(label: str, text: str) -> None:
    """The word-boundary trim needed a floor.

    ``rsplit(" ", 1)`` keeps only what precedes the LAST space in the slice.
    Japanese and Chinese lines have no spaces at all, and a line opening with a
    short word before a long unbroken token has its last space near the start.
    Both collapsed the excerpt to a handful of characters while reporting a
    1,200-character budget.
    """

    from alicebot_api.vnext_retrieval import _query_anchored_window

    excerpt = _query_anchored_window(text + "\nz", query="日本語 example", max_chars=300)

    assert len(excerpt) > 300 * 0.5, (
        f"{label}: the excerpt collapsed to {len(excerpt)} characters of a 300 budget"
    )
    assert len(excerpt) <= 301


def test_the_fallback_scan_cap_survives_malformed_rows() -> None:
    """The cap check sat below two `continue`s, so rows that skipped never hit it.

    A source whose rows are mostly empty or malformed therefore scanned however
    many the store returned, which on SQLite is all of them.
    """

    from alicebot_api.vnext_retrieval import (
        SOURCE_FALLBACK_CHUNK_SCAN_LIMIT,
        VNextRetrievalService,
    )

    from collections.abc import Mapping as MappingABC

    examined: list[int] = []

    class _CountingRow(MappingABC):
        """Records that the loop body actually reached this row.

        Counting rows the store BUILT would prove nothing: the loop is what the
        cap bounds, and asserting on the return value proves nothing either,
        since a document of blank chunks returns None whether the cap fires or
        not. That was the first version of this test, and it passed with the
        cap moved back below the `continue`s.
        """

        def __init__(self, index: int) -> None:
            self._index = index
            self._data = {"text": "   ", "chunk_index": index}

        def __getitem__(self, key: str) -> object:
            examined.append(self._index)
            return self._data[key]

        def get(self, key: str, default: object = None) -> object:
            examined.append(self._index)
            return self._data.get(key, default)

        def __iter__(self):
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    class _MostlyEmptyStore:
        def list_source_chunks(self, source_id: str) -> list[_CountingRow]:
            return [_CountingRow(index) for index in range(500)]

    service = VNextRetrievalService.__new__(VNextRetrievalService)
    service._winning_chunk_text = {}
    service.store = _MostlyEmptyStore()  # type: ignore[assignment]

    assert service._best_chunk_without_a_winner("source-1", query=QUOTE) is None
    assert len(set(examined)) <= SOURCE_FALLBACK_CHUNK_SCAN_LIMIT, (
        f"the loop examined {len(set(examined))} rows against a cap of "
        f"{SOURCE_FALLBACK_CHUNK_SCAN_LIMIT}. Rows that hit a `continue` are "
        "skipping the cap check again."
    )


def test_the_scan_bound_is_pushed_into_stores_that_accept_one() -> None:
    """Capping the Python loop does not stop the store materialising every row."""

    from alicebot_api.vnext_retrieval import (
        SOURCE_FALLBACK_CHUNK_SCAN_LIMIT,
        VNextRetrievalService,
    )

    received: dict[str, object] = {}

    class _LimitAwareStore:
        def list_source_chunks(self, source_id: str, *, limit: int = 500) -> list[dict]:
            received["limit"] = limit
            return [{"text": f"> {QUOTE}", "chunk_index": 0}]

    service = VNextRetrievalService.__new__(VNextRetrievalService)
    service._winning_chunk_text = {}
    service.store = _LimitAwareStore()  # type: ignore[assignment]
    service._best_chunk_without_a_winner("source-1", query=QUOTE)

    assert received["limit"] == SOURCE_FALLBACK_CHUNK_SCAN_LIMIT


def test_a_store_without_a_limit_parameter_still_works() -> None:
    """SQLite's reader takes no limit. Probing must not break it."""

    from alicebot_api.vnext_retrieval import VNextRetrievalService

    class _NoLimitStore:
        def list_source_chunks(self, source_id: str) -> list[dict]:
            return [{"text": f"> {QUOTE}", "chunk_index": 0}]

    service = VNextRetrievalService.__new__(VNextRetrievalService)
    service._winning_chunk_text = {}
    service.store = _NoLimitStore()  # type: ignore[assignment]

    assert service._best_chunk_without_a_winner("source-1", query=QUOTE) is not None


def _service_with_chunks(tmp_path: Path, chunks: list[dict], *, winner: str | None = None):
    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")

    class _ChunkStore:
        def list_source_chunks(self, source_id: str) -> list[dict]:
            return chunks

    with sqlite_user_connection(database, USER_ID) as connection:
        service = VNextRetrievalService(SQLiteVNextStore(connection, USER_ID))
        service.store = _ChunkStore()  # type: ignore[assignment]
        if winner is not None:
            service._winning_chunk_text["source-1"] = winner
        return service._packable_source({"id": "source-1"}, query=QUOTE)


def test_a_winning_chunk_of_pure_links_does_not_become_the_excerpt(
    tmp_path: Path,
) -> None:
    """The fourth door, and the last one I could find.

    ``_query_anchored_window`` returns a chunk verbatim when it already fits the
    budget. That short-circuit skips line scoring entirely, so a SHORT chunk of
    nothing but wikilinks reached the agent intact even though every line in it
    scores zero. Fixing the scorer never touched this path, because the scorer
    is not called.

    Reproduced: a 93-character "## Related" chunk came back verbatim as a list
    of paths, for a query whose answer was one chunk away.
    """

    packed = _service_with_chunks(
        tmp_path,
        [
            {"text": "## Related\n- [[discipline-is-the-art]]\n", "chunk_index": 0},
            {"text": f"> {QUOTE}\n", "chunk_index": 1},
        ],
        winner="## Related\n- [[discipline-is-the-art-of-not-betraying-yourself]]\n",
    )

    excerpt = packed.get("excerpt") or ""
    assert QUOTE in excerpt, (
        f"the agent received navigation instead of text: {excerpt!r}. A chunk "
        "short enough to skip windowing bypassed the link rule entirely."
    )


def test_a_document_that_is_genuinely_all_links_still_yields_its_text(
    tmp_path: Path,
) -> None:
    """The honest limit of the rule above.

    Preferring readable chunks must not mean returning nothing when a document
    really is an index page. Then the links ARE the document, and withholding
    them helps no one.
    """

    only_links = "## Related\n- [[discipline-is-the-art]]\n- [[solitude]]\n"

    packed = _service_with_chunks(
        tmp_path,
        [{"text": only_links, "chunk_index": 0}],
        winner=only_links,
    )

    assert packed.get("excerpt"), "an index-only document lost its excerpt entirely"


def test_the_fallback_degrades_quietly_on_a_store_that_cannot_list_chunks(
    tmp_path: Path,
) -> None:
    """Minimal stores and fakes must keep working, without an excerpt."""

    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")

    class _NoChunkListing:
        pass

    with sqlite_user_connection(database, USER_ID) as connection:
        service = VNextRetrievalService(SQLiteVNextStore(connection, USER_ID))
        service.store = _NoChunkListing()  # type: ignore[assignment]

        assert service._best_chunk_without_a_winner("source-1", query=QUOTE) is None


@pytest.mark.parametrize(
    "query",
    (
        "discipline",
        "not betraying yourself",
        "what did I save about keeping promises to myself",
    ),
)
def test_several_phrasings_all_reach_the_document(tmp_path: Path, query: str) -> None:
    """One query working could be luck. The user does not know the magic words."""

    context = _fresh_context(tmp_path)
    _capture(context, QUOTE_NOTE)

    payload = _recall(context, query)

    assert payload.get("sources"), f"query {query!r} returned no source material"


# --------------------------------------------------------------------------
# The capture receipt: what the agent tells its user.
# --------------------------------------------------------------------------


def test_capture_reports_what_became_searchable(tmp_path: Path) -> None:
    """``chunk_count`` and ``candidate_memory_count`` read as two counts of the
    same stored thing. They are not, and the difference is what an agent repeats
    to its user."""

    context = _fresh_context(tmp_path)
    record = _capture(context, QUOTE_NOTE)

    retrieval = record.get("retrieval")
    assert isinstance(retrieval, dict), "capture no longer says what became retrievable"
    assert retrieval["searchable_now"] == "source_material"
    assert retrieval["searchable_chunks"] == record["chunk_count"]
    assert retrieval["awaiting_review"] == record["candidate_memory_count"]
    assert "alice_recall" in retrieval["how"]


def test_capture_does_not_claim_searchability_when_nothing_was_indexed() -> None:
    """The honest empty case, asserted on the record itself."""

    from alicebot_api.vnext_capture import CaptureResult

    record = CaptureResult(status="duplicate", source_id=None, content_hash="x").to_record()

    assert record["retrieval"]["searchable_now"] == "nothing"
    assert record["retrieval"]["searchable_chunks"] == 0
    assert "alice_recall" not in record["retrieval"]["how"]
