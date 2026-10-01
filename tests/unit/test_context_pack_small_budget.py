"""A small token budget must not empty the context pack.

Through v0.19.2 the packer latched on the first item that did not fit and
dropped every item after it. One large item ranked first therefore emptied the
whole pack. Measured on a synthetic vault of four questions at a 500-token
budget, three of the four packs were empty, and one of them stayed empty at
4000 tokens because the memory ranked first cost more than that.

The rules these tests pin:

1. An item that does not fit is skipped, and packing continues with the items
   behind it.
2. When nothing fits whole, the first item that can be made to fit is cut to
   the budget, and the cut ends in the one-character marker the pack already
   uses for a trimmed line.
3. The pack is never empty while some ranked item can be cut to fit.
4. Packed items keep the order the ranking gave them.
5. ``token_estimate`` never exceeds ``max_tokens``.

Every test names the change that must fail it. Each of those changes was made
and the test was seen to fail before the test was kept.
"""

from __future__ import annotations

from pathlib import Path
import random

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import (
    BUDGET_STRATEGIES,
    VNextRetrievalRequest,
    VNextRetrievalService,
    estimate_item_tokens,
)

from tests.unit.test_vnext_retrieval import InMemoryVNextRetrievalStore, _memory_row

USER_ID = "00000000-0000-0000-0000-000000000001"
CUT_MARKER = "…"
QUERY = "Alice budget probe"

PRIMARY_SECTIONS = ("relevant_memories", "open_loops", "sources")


def _words(topic: str, n_chars: int) -> str:
    """Spaced prose of about ``n_chars`` characters that mentions the topic."""

    sentence = f"The {topic} was reviewed in detail and every step of it was written down. "
    return (sentence * (n_chars // len(sentence) + 1))[:n_chars]


def _compile(
    store: object,
    *,
    max_tokens: int | None,
    query: str = QUERY,
    strategy: str = "balanced",
) -> dict[str, object]:
    return VNextRetrievalService(store).compile_context_pack(  # type: ignore[arg-type]
        VNextRetrievalRequest(
            query=query,
            domains=("project",),
            max_tokens=max_tokens,
            budget_strategy=strategy,
        )
    )


def _packed_items(pack: dict[str, object]) -> list[dict[str, object]]:
    return [item for section in PRIMARY_SECTIONS for item in pack[section]]  # type: ignore[union-attr]


def _memory_store(*texts: str) -> InMemoryVNextRetrievalStore:
    return InMemoryVNextRetrievalStore(
        memories=[_memory_row(f"memory-{index}", text) for index, text in enumerate(texts, start=1)],
        sources=[],
    )


def _source_store(excerpt_texts: list[str]) -> InMemoryVNextRetrievalStore:
    return InMemoryVNextRetrievalStore(
        memories=[],
        sources=[
            {
                "id": f"source-{index}",
                "source_type": "manual_text",
                "title": f"Alice budget probe source {index}",
                "content_hash": f"sha256:probe-{index}",
                "domain": "project",
                "sensitivity": "private",
            }
            for index, _text in enumerate(excerpt_texts, start=1)
        ],
        source_chunks=[
            {"id": f"chunk-{index}", "source_id": f"source-{index}", "chunk_index": 0, "text": text}
            for index, text in enumerate(excerpt_texts, start=1)
        ],
    )


# --------------------------------------------------------------------------
# Rule 1: an item that does not fit is skipped, not fatal.
# --------------------------------------------------------------------------


def test_a_large_first_item_is_skipped_and_the_items_behind_it_are_packed() -> None:
    """The reported defect: the large first item emptied the pack.

    Mutation: restore the latch in ``_TokenBudget.admit`` by adding
    ``self.truncated or`` to its over-budget condition. The large memory is
    rejected, ``truncated`` is set, the two small memories are rejected for
    that reason alone, and the pack comes back empty.
    """

    big = _words("Alice budget probe", 6_000)
    store = _memory_store(big, "Alice budget probe small one.", "Alice budget probe small two.")
    small_cost = sum(
        estimate_item_tokens(_memory_row(f"memory-{index}", text))
        for index, text in ((2, "Alice budget probe small one."), (3, "Alice budget probe small two."))
    )
    assert estimate_item_tokens(_memory_row("memory-1", big)) > small_cost * 4, "fixture lost its large item"

    pack = _compile(store, max_tokens=small_cost)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-2", "memory-3"]
    assert pack["budget"]["dropped_item_count"] == 1
    assert pack["budget"]["truncated"] is True
    assert "cut_item_count" not in pack["budget"], "nothing was cut: smaller items fit whole"
    assert pack["budget"]["token_estimate"] <= small_cost


def test_skipping_an_item_keeps_the_ranking_order_of_the_rest() -> None:
    """Packed items stay in the order the ranking gave them.

    Mutation: sort the offered memories by cost before admitting them, in
    ``pack_sections``. The cheapest memory then packs first and the ids come
    back as memory-4, memory-3, memory-1.
    """

    memories = [
        _memory_row("memory-1", "Alice budget probe " + "mid " * 40),
        _memory_row("memory-2", "Alice budget probe " + _words("big", 4_000)),
        _memory_row("memory-3", "Alice budget probe " + "small " * 8),
        _memory_row("memory-4", "Alice budget probe tiny"),
    ]
    costs = {row["id"]: estimate_item_tokens(row) for row in memories}
    assert costs["memory-1"] > costs["memory-3"] > costs["memory-4"], "fixture sizes must differ"
    budget = costs["memory-1"] + costs["memory-3"] + costs["memory-4"]
    assert costs["memory-2"] > budget

    pack = _compile(InMemoryVNextRetrievalStore(memories=memories, sources=[]), max_tokens=budget)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-1", "memory-3", "memory-4"]


def test_a_skipped_section_item_does_not_stop_the_next_section() -> None:
    """Skipping works across sections too, not only inside one.

    The large memory is skipped, the loop behind it is not offered a latched
    budget, and the source after the loop still packs.

    Mutation: restore the latch (``self.truncated or`` in
    ``_TokenBudget.admit``). The loop and the source both disappear.
    """

    store = InMemoryVNextRetrievalStore(
        memories=[_memory_row("memory-big", _words("Alice budget probe", 6_000))],
        sources=[
            {
                "id": "source-small",
                "source_type": "manual_text",
                "title": "Alice budget probe source",
                "content_hash": "sha256:small",
                "domain": "project",
                "sensitivity": "private",
            }
        ],
        open_loops=[
            {
                "id": "loop-small",
                "title": "Alice budget probe loop",
                "status": "open",
                "domain": "project",
                "sensitivity": "private",
            }
        ],
    )
    unbudgeted = _compile(store, max_tokens=None)
    loop_cost = estimate_item_tokens(unbudgeted["open_loops"][0])
    source_cost = estimate_item_tokens(unbudgeted["sources"][0])

    pack = _compile(store, max_tokens=loop_cost + source_cost)

    assert pack["relevant_memories"] == []
    assert [item["id"] for item in pack["open_loops"]] == ["loop-small"]
    assert [item["id"] for item in pack["sources"]] == ["source-small"]


# --------------------------------------------------------------------------
# Rule 2 and 3: nothing fits whole, so the first item is cut to fit.
# --------------------------------------------------------------------------


def test_a_lone_item_larger_than_the_budget_is_cut_to_fit() -> None:
    """The case the skip alone cannot reach: there is nothing to skip to.

    Mutation: delete the fallback in ``compile_context_pack`` (the block
    guarded by ``budget.token_estimate == 0``). The pack is empty.
    """

    text = _words("Alice budget probe", 8_000)
    store = _memory_store(text)
    whole = estimate_item_tokens(_memory_row("memory-1", text))
    budget = whole // 4

    pack = _compile(store, max_tokens=budget)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-1"]
    cut = pack["relevant_memories"][0]
    assert cut["canonical_text"].endswith(CUT_MARKER)
    assert 0 < len(cut["canonical_text"]) < len(text)
    assert text.startswith(cut["canonical_text"].removesuffix(CUT_MARKER)), "the cut is a head cut of this text"
    assert estimate_item_tokens(cut) <= budget
    assert pack["budget"]["token_estimate"] <= budget
    assert pack["budget"]["truncated"] is True, "a cut item means the pack is not the whole answer"
    assert pack["budget"]["cut_item_count"] == 1
    assert pack["budget"]["dropped_item_count"] == 0, "the item was packed, cut, not dropped"
    assert pack["current_known_state"][0]["id"] == "memory-1"


def test_an_item_one_token_over_the_budget_is_cut_not_admitted() -> None:
    """The budget is a hard limit, to the token.

    Mutation: change ``self.token_estimate + cost > self.token_budget`` to
    ``> self.token_budget + 1`` in ``_TokenBudget.admit``. The whole item is
    then admitted at one token over and ``token_estimate`` exceeds the budget.
    """

    text = _words("Alice budget probe", 2_000)
    whole = estimate_item_tokens(_memory_row("memory-1", text))

    pack = _compile(_memory_store(text), max_tokens=whole - 1)

    assert pack["budget"]["token_estimate"] <= whole - 1
    assert pack["relevant_memories"][0]["canonical_text"].endswith(CUT_MARKER)
    assert pack["budget"]["cut_item_count"] == 1

    exact = _compile(_memory_store(text), max_tokens=whole)
    assert exact["relevant_memories"][0]["canonical_text"] == text, "an item that fits exactly is not cut"
    assert "cut_item_count" not in exact["budget"]


def test_the_cut_keeps_as_much_text_as_the_budget_allows() -> None:
    """The cut keeps nearly all the room the budget leaves, not just a sliver of it.

    Mutation: make ``_fit_item_to_tokens`` return its first candidate, the one
    with every text cut to the bare marker, without searching. The pack is then
    non-empty and fits, and only this assertion notices that it is nearly all
    marker.
    """

    text = _words("Alice budget probe", 8_000)
    store = _memory_store(text)
    budget = estimate_item_tokens(_memory_row("memory-1", text)) // 4

    pack = _compile(store, max_tokens=budget)

    cut = pack["relevant_memories"][0]
    slack = budget - estimate_item_tokens(cut)
    assert 0 <= slack <= 6, f"cut left {slack} tokens of an {budget}-token budget unused"


def test_a_source_excerpt_is_cut_around_the_matching_line_and_marked() -> None:
    """The cut for a source follows the excerpt rule: window on the best line.

    The matching line is in the middle of a long chunk. A head cut would keep
    only filler.

    Mutation: replace ``_query_anchored_window`` in ``_cut_text`` with a head
    slice (``text[:max_chars]``). The excerpt then holds no canary line.
    """

    canary = "Alice budget probe canary line states the decision."
    filler = [f"Filler line {index} about nothing in particular at all." for index in range(40)]
    chunk = "\n".join([*filler[:25], canary, *filler[25:]])
    store = _source_store([chunk])
    unbudgeted = _compile(store, max_tokens=None)
    whole = estimate_item_tokens(unbudgeted["sources"][0])
    floor_item = {**unbudgeted["sources"][0], "excerpt": CUT_MARKER}
    budget = estimate_item_tokens(floor_item) + 40
    assert budget < whole

    pack = _compile(store, max_tokens=budget)

    assert [item["id"] for item in pack["sources"]] == ["source-1"]
    excerpt = pack["sources"][0]["excerpt"]
    assert canary in excerpt
    assert excerpt.endswith(CUT_MARKER)
    assert pack["budget"]["cut_item_count"] == 1
    assert estimate_item_tokens(pack["sources"][0]) <= budget


def test_only_the_first_item_is_cut_when_nothing_fits_whole() -> None:
    """One item is cut, and it is the first one in the ranking.

    Mutation: cut the LAST fittable offer instead of the first in
    ``_first_item_that_fits_when_cut`` (iterate ``reversed`` over each
    section). memory-2 is then the one that comes back.
    """

    first = _words("Alice budget probe first", 6_000)
    second = _words("Alice budget probe second", 6_000)
    store = _memory_store(first, second)
    budget = estimate_item_tokens(_memory_row("memory-1", first)) // 3

    pack = _compile(store, max_tokens=budget)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-1"]
    assert pack["budget"]["cut_item_count"] == 1
    assert pack["budget"]["dropped_item_count"] >= 1, "memory-2 is still dropped, not cut as well"


@pytest.mark.parametrize(
    ("strategy", "query", "expected_id"),
    [
        ("balanced", QUERY, "memory-1"),
        ("facts_first", QUERY, "memory-1"),
        ("contradictions_first", QUERY, "memory-1"),
        ("sources_first", QUERY, "source-1"),
        ("balanced", f"{QUERY} what is still open", "loop-1"),
    ],
    ids=["balanced", "facts_first", "contradictions_first", "sources_first", "loops_view"],
)
def test_the_item_that_is_cut_is_the_first_in_the_strategys_section_order(
    strategy: str, query: str, expected_id: str
) -> None:
    """Which item is cut follows the offer order the strategy and view set.

    A memory, an open loop and a source each need a cut at this budget, so the
    only thing that decides which one comes back is the section order. The
    default order offers memories first, ``sources_first`` offers sources first,
    and a query that asks what is still open reads as the loops view, which
    offers open loops first. ``contradictions_first`` has no ranked item to cut
    in its first section, so it falls through to the memory.

    Mutation: in ``compile_context_pack``, pass the fixed tuple
    ``(SECTION_RELEVANT_MEMORIES, SECTION_OPEN_LOOPS, SECTION_SOURCES)`` as
    ``section_order`` to ``_first_item_that_fits_when_cut`` instead of the
    strategy's ``section_order``. The ``sources_first`` and ``loops_view`` cases
    then come back with memory-1.
    """

    memory = _memory_row("memory-1", _words("Alice budget probe", 6_000))
    loop = {
        "id": "loop-1",
        "title": "Alice budget probe loop",
        "description": _words("Alice budget probe", 6_000),
        "status": "open",
        "domain": "project",
        "sensitivity": "private",
    }
    source = {
        "id": "source-1",
        "source_type": "manual_text",
        "title": "Alice budget probe source",
        "content_hash": "sha256:order",
        "domain": "project",
        "sensitivity": "private",
    }
    chunk = {"id": "chunk-1", "source_id": "source-1", "chunk_index": 0, "text": _words("Alice budget probe", 6_000)}
    store = InMemoryVNextRetrievalStore(
        memories=[memory], sources=[source], open_loops=[loop], source_chunks=[chunk]
    )
    whole = _compile(store, max_tokens=None, query=query, strategy=strategy)
    offered = _packed_items(whole)
    assert {item["id"] for item in offered} == {"memory-1", "loop-1", "source-1"}, "fixture: three sections"
    budget = 200
    assert min(estimate_item_tokens(item) for item in offered) > budget, "fixture: every item needs a cut"
    assert max(estimate_item_tokens(_floor_item(item)) for item in offered) < budget, "fixture: every item can be cut"

    pack = _compile(store, max_tokens=budget, query=query, strategy=strategy)

    assert [item["id"] for item in _packed_items(pack)] == [expected_id]
    assert pack["budget"]["cut_item_count"] == 1
    assert pack["budget"]["token_estimate"] <= budget


def test_nothing_is_cut_while_some_item_fits_whole() -> None:
    """The cut is a last resort. A whole smaller item beats a cut larger one.

    Mutation: run the fallback unconditionally instead of only when
    ``budget.token_estimate == 0``. memory-1 comes back cut and memory-2 is
    lost.
    """

    big = _words("Alice budget probe", 6_000)
    small_text = "Alice budget probe small row."
    store = _memory_store(big, small_text)
    small_cost = estimate_item_tokens(_memory_row("memory-2", small_text))

    pack = _compile(store, max_tokens=small_cost)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-2"]
    assert pack["relevant_memories"][0]["canonical_text"] == small_text
    assert "cut_item_count" not in pack["budget"]


def test_an_item_that_cannot_be_cut_small_enough_yields_to_the_next_one() -> None:
    """The first item's ids and metadata can cost more than the budget.

    memory-1 carries 3,000 characters of metadata that no cut touches, so its
    floor is far above the budget. memory-2 is plain, and its text is long
    enough that it does not fit whole, so only a cut gets it in.

    Mutation: make ``_first_item_that_fits_when_cut`` return None as soon as the
    first offer cannot fit (replace its ``continue``-style scan with a check of
    offer 0 only). The pack is then empty.
    """

    heavy = _memory_row("memory-1", _words("Alice budget probe", 2_000), metadata_json={"note": "x" * 3_000})
    plain_text = _words("Alice budget probe", 2_000)
    plain = _memory_row("memory-2", plain_text)
    heavy_floor = estimate_item_tokens({**heavy, "canonical_text": CUT_MARKER})
    plain_floor = estimate_item_tokens({**plain, "canonical_text": CUT_MARKER})
    budget = plain_floor + 60
    assert heavy_floor > budget, "fixture: the heavy item must be unfittable"
    assert estimate_item_tokens(plain) > budget, "fixture: the plain item must need a cut"

    pack = _compile(InMemoryVNextRetrievalStore(memories=[heavy, plain], sources=[]), max_tokens=budget)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-2"]
    assert pack["relevant_memories"][0]["canonical_text"].endswith(CUT_MARKER)
    assert pack["budget"]["token_estimate"] <= budget


# --------------------------------------------------------------------------
# Every copy of an item's text is cut, so a long one cannot keep the item out.
# --------------------------------------------------------------------------

SHORT_TEXT = "Alice budget probe row."


@pytest.mark.parametrize("long_field", ["canonical_text", "summary", "value.text"])
def test_a_memory_whose_long_text_sits_in_one_field_is_cut_in_that_field(long_field: str) -> None:
    """A memory row repeats its text as canonical_text, summary and value.text.

    A row written by ``alice_memory_commit`` carries all three. Whichever one is
    long must be cut, because an uncut copy keeps the item above the budget
    however short the other two become, and the item is then dropped instead of
    cut. Each case makes one field long and leaves the other two short.

    Mutations, one per case, each of which fails its own case:
    - ``canonical_text`` case: remove ``"canonical_text"`` from
      ``_CUTTABLE_TEXT_KEYS``;
    - ``summary`` case: remove ``"summary"`` from ``_CUTTABLE_TEXT_KEYS``
      (only the ``summary`` case fails);
    - ``value.text`` case: skip the ``value`` branch in ``_item_with_text_cut``
      (only the ``value.text`` case fails).
    In each the long copy is left whole, the item's floor is above the budget,
    and the pack is empty.
    """

    long_text = _words("Alice budget probe", 6_000)
    row = _memory_row(
        "memory-1",
        long_text if long_field == "canonical_text" else SHORT_TEXT,
        summary=long_text if long_field == "summary" else SHORT_TEXT,
        value={"text": long_text if long_field == "value.text" else SHORT_TEXT},
    )
    store = InMemoryVNextRetrievalStore(memories=[row], sources=[])
    floor = estimate_item_tokens(_floor_item(row))
    whole = estimate_item_tokens(row)
    budget = floor + 60
    assert whole > budget * 2, "fixture: the row must need a cut"

    pack = _compile(store, max_tokens=budget)

    assert [item["id"] for item in pack["relevant_memories"]] == ["memory-1"], (
        f"the {long_field} copy was not cut, so the memory was dropped"
    )
    packed = pack["relevant_memories"][0]
    cut_texts = {
        "canonical_text": packed["canonical_text"],
        "summary": packed["summary"],
        "value.text": packed["value"]["text"],
    }
    assert cut_texts[long_field].endswith(CUT_MARKER)
    assert 0 < len(cut_texts[long_field]) < len(long_text)
    for field, text in cut_texts.items():
        if field != long_field:
            assert text == SHORT_TEXT, f"{field} was short and must come back as it was"
    assert estimate_item_tokens(packed) <= budget
    assert pack["budget"]["token_estimate"] <= budget
    assert pack["budget"]["cut_item_count"] == 1


def test_an_open_loop_with_a_long_description_is_cut_in_the_description() -> None:
    """An open loop carries its text in ``description``, which the cut must reach.

    Mutation: remove ``"description"`` from ``_CUTTABLE_TEXT_KEYS``. The loop's
    description is left whole, its floor is above the budget, and the pack is
    empty.
    """

    description = _words("Alice budget probe", 6_000)
    loop = {
        "id": "loop-1",
        "title": "Alice budget probe loop",
        "description": description,
        "status": "open",
        "domain": "project",
        "sensitivity": "private",
    }
    store = InMemoryVNextRetrievalStore(memories=[], sources=[], open_loops=[loop])
    floor = estimate_item_tokens({**loop, "description": CUT_MARKER})
    budget = floor + 60
    assert estimate_item_tokens(loop) > budget * 2, "fixture: the loop must need a cut"

    pack = _compile(store, max_tokens=budget)

    assert [item["id"] for item in pack["open_loops"]] == ["loop-1"], "the description was not cut"
    cut = pack["open_loops"][0]
    assert cut["description"].endswith(CUT_MARKER)
    assert 0 < len(cut["description"]) < len(description)
    assert cut["title"] == loop["title"], "ids and titles are never cut"
    assert estimate_item_tokens(cut) <= budget
    assert pack["budget"]["cut_item_count"] == 1


def test_a_memory_committed_through_the_tool_comes_back_cut_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row shape agents write: ``alice_memory_commit`` then ``alice_context_pack``.

    A committed memory carries its text in canonical_text, summary and
    value.text, plus the write provenance the packer prices before it admits
    anything. At 1000 tokens the whole memory does not fit and nothing else is
    in the vault, so the memory is the one that must be cut.

    Mutation: skip the ``value`` branch in ``_item_with_text_cut``. The
    committed memory's value.text is left whole and the tool returns no memory
    at 800 or 1000 tokens, an empty pack for the most common shape of memory.
    """

    _clear_env(monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": f"{TOPICS['orion']} decision",
            "canonical_text": _words(TOPICS["orion"], 6_000),
            "memory_type": "decision",
            "domain": "personal",
            "sensitivity": "private",
            "confidence": 0.96,
            "rationale": "User said: remember this",
        },
    )
    assert committed["status"] == "committed", committed
    unbudgeted = call_mcp_tool(
        context, name="alice_context_pack", arguments={"query": TOPICS["orion"], "max_tokens": 50_000}
    )
    assert unbudgeted["token_report"]["token_estimate"] > 2_000, "fixture: the memory must not fit whole"
    assert len(unbudgeted["memories"]) == 1

    for budget in (800, 1_000):
        payload = call_mcp_tool(
            context, name="alice_context_pack", arguments={"query": TOPICS["orion"], "max_tokens": budget}
        )

        assert len(payload["memories"]) == 1, f"the committed memory came back empty at {budget} tokens"
        text = payload["memories"][0]["canonical_text"]
        assert text.removesuffix('"').endswith(CUT_MARKER), "the tool presents a stored note in quotes"
        assert len(text) < len(unbudgeted["memories"][0]["canonical_text"])
        assert payload["token_report"]["token_estimate"] <= budget
        assert payload["token_report"]["cut_item_count"] == 1


# --------------------------------------------------------------------------
# The floor: the smallest budget at which one item can be packed.
# --------------------------------------------------------------------------


def test_the_pack_is_empty_just_below_the_floor_and_has_one_item_at_it() -> None:
    """The floor is exact: ids and metadata with the text reduced to the marker.

    Mutation: change ``estimate_item_tokens(best) > max_tokens`` to ``>=`` in
    ``_fit_item_to_tokens``. The pack at the floor is then empty.
    """

    text = _words("Alice budget probe", 4_000)
    row = _memory_row("memory-1", text)
    floor = estimate_item_tokens({**row, "canonical_text": CUT_MARKER})
    store = _memory_store(text)

    below = _compile(store, max_tokens=floor - 1)
    at = _compile(store, max_tokens=floor)

    assert below["relevant_memories"] == []
    assert [item["id"] for item in at["relevant_memories"]] == ["memory-1"]
    assert at["relevant_memories"][0]["canonical_text"].endswith(CUT_MARKER)
    assert at["budget"]["token_estimate"] == floor


# --------------------------------------------------------------------------
# Rule 5, over many shapes: never over budget, never needlessly empty, ordered.
# --------------------------------------------------------------------------


def _floor_item(item: dict[str, object]) -> dict[str, object]:
    """The item with every text the cut may touch reduced to the marker.

    Written out here rather than imported, so the oracle does not share a
    mistake with the code it checks.
    """

    floor = dict(item)
    for key in ("excerpt", "canonical_text", "summary", "description"):
        if isinstance(floor.get(key), str):
            floor[key] = CUT_MARKER
    value = floor.get("value")
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        floor["value"] = {**value, "text": CUT_MARKER}
    return floor


def _random_store(rng: random.Random) -> InMemoryVNextRetrievalStore:
    memories = [
        _memory_row(
            f"memory-{index}",
            "Alice budget probe " + _words("row", rng.choice((5, 40, 300, 1_500, 6_000, 12_000))),
            **({"metadata_json": {"note": "m" * rng.randrange(0, 1_200)}} if rng.random() < 0.3 else {}),
        )
        for index in range(1, rng.randrange(1, 7))
    ]
    texts = [
        "Alice budget probe " + _words("chunk", rng.choice((20, 200, 900, 2_400, 5_000)))
        for _ in range(rng.randrange(0, 4))
    ]
    sources = _source_store(texts)
    return InMemoryVNextRetrievalStore(
        memories=memories, sources=sources.sources, source_chunks=sources.source_chunks
    )


def test_seeded_property_budget_never_exceeded_never_needlessly_empty_order_kept() -> None:
    """Many random item sizes and budgets, one seed, four properties each.

    For every case: the reported estimate is within the budget; the packed
    items, priced again on their own with the annotations the packer adds
    separately taken off, are within the budget; the pack is empty exactly when
    the budget is below the cheapest item's floor; and the packed ids are a
    subsequence of the ranking order the unbudgeted pack gave.

    Mutations, each of which fails this test:
    - ``_TokenBudget.admit`` compares ``> self.token_budget + 1`` (a pack one
      token over budget);
    - ``_fit_item_to_tokens`` accepts a candidate up to three tokens over the
      budget (pass two then rejects it and the pack is empty above the floor);
    - ``pack_sections`` sorts the offered memories by cost (order property);
    - the cut fallback is deleted, or only the first offer is ever tried, or
      the floor check uses ``>=``.
    """

    rng = random.Random(20261001)
    cases = 0
    cut_cases = 0
    multi_item_cases = 0
    for _ in range(300):
        store = _random_store(rng)
        strategy = rng.choice(BUDGET_STRATEGIES)
        unbudgeted = _compile(store, max_tokens=None, strategy=strategy)
        offered = _packed_items(unbudgeted)
        if not offered:
            continue
        costs = [estimate_item_tokens(item) for item in offered]
        floor = min(estimate_item_tokens(_floor_item(item)) for item in offered)
        whole = sum(costs)
        prefix = sum(costs[: rng.randrange(1, len(costs) + 1)])
        budget = rng.choice(
            (
                floor - 1,
                floor,
                floor + 1,
                rng.choice(costs) - 1,
                rng.choice(costs),
                prefix - 1,
                prefix,
                prefix + 1,
                rng.randrange(1, whole + 200),
                rng.randrange(floor, floor + 400),
            )
        )
        if budget < 1:
            continue
        cases += 1

        pack = _compile(store, max_tokens=budget, strategy=strategy)

        assert pack["budget"]["token_estimate"] <= budget
        assert sum(pack["budget"]["allocation"].values()) == pack["budget"]["token_estimate"]
        packed = _packed_items(pack)
        if len(packed) >= 2:
            multi_item_cases += 1
        if pack["budget"].get("cut_item_count"):
            cut_cases += 1
            assert len(packed) == 1
            assert estimate_item_tokens(packed[0]) <= budget
        if budget < floor:
            assert packed == [], f"budget {budget} is below the floor {floor} yet something was packed"
        else:
            assert packed, f"budget {budget} is at or above the floor {floor} yet the pack is empty"
        for section in PRIMARY_SECTIONS:
            ranked_ids = [item["id"] for item in unbudgeted[section]]
            packed_ids = [item["id"] for item in pack[section]]
            remaining = iter(ranked_ids)
            assert all(item_id in remaining for item_id in packed_ids), (
                f"{section}: {packed_ids} is not in the ranking order {ranked_ids}"
            )
        # The unbudgeted pack is produced by the code under test, so it is not an
        # independent witness of the ranking. The store ranks by insertion order,
        # and only facts_first and recent_first reorder memories, so for every
        # other strategy the insertion order is the ranking.
        insertion_order = [f"memory-{index}" for index in range(1, len(store.memories) + 1)]
        if strategy not in ("facts_first", "recent_first"):
            remaining = iter(insertion_order)
            packed_memory_ids = [item["id"] for item in pack["relevant_memories"]]
            assert all(item_id in remaining for item_id in packed_memory_ids), (
                f"memories {packed_memory_ids} are not in store order {insertion_order}"
            )
        source_order = [f"source-{index}" for index in range(1, len(store.sources) + 1)]
        remaining = iter(source_order)
        packed_source_ids = [item["id"] for item in pack["sources"]]
        assert all(item_id in remaining for item_id in packed_source_ids), (
            f"sources {packed_source_ids} are not in store order {source_order}"
        )

    assert cases >= 200, "the generator stopped producing cases and the properties are vacuous"
    assert cut_cases >= 15, "no case exercised the cut path, so its properties are vacuous"
    assert multi_item_cases >= 40, "too few packs held two items for the order property to mean anything"


# --------------------------------------------------------------------------
# The shape of the report, for packs nothing was cut from.
# --------------------------------------------------------------------------


def test_a_pack_nothing_was_cut_from_keeps_its_budget_report_unchanged() -> None:
    """``cut_item_count`` exists only when an item was cut.

    Every pack that did not take the cut path must carry the report it carried
    in v0.19.2, key for key.

    Mutation: have ``_TokenBudget.to_record`` always emit ``cut_item_count``.
    """

    store = _memory_store("Alice budget probe small row.")

    for budget in (None, 10_000):
        pack = _compile(store, max_tokens=budget)
        assert set(pack["budget"]) == {
            "token_budget",
            "token_estimate",
            "truncated",
            "dropped_item_count",
            "strategy",
            "allocation",
            "scope",
            "counted_sections",
            "excluded_sections",
            "is_transport_cap",
            "serialized_token_estimate",
            "excluded_token_estimate",
        }


# --------------------------------------------------------------------------
# End to end on a SQLite vault, through the tool an agent calls.
# --------------------------------------------------------------------------

TOPICS = {
    "orion": "orion migration rollout",
    "harbor": "harbor ledger audit",
    "kestrel": "kestrel vendor contract",
    "birch": "birch garden irrigation",
}


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)


def _vault(tmp_path: Path) -> Path:
    """Four questions. Three have one large item ranked first, with small ones behind.

    The large source is one 2,200-character paragraph, so its excerpt hits the
    1,200-character cap and costs about 630 tokens with its row. The large
    memory is 14,000 characters. The fourth question has only small sources.
    """

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        capture = VNextCaptureService(store, actor_type="user_or_system")
        for key in ("orion", "harbor", "kestrel"):
            words = TOPICS[key]
            capture.capture_text(
                _words(words, 2_200), title=f"{words} full write-up", domain="project", sensitivity="private"
            )
            for index in range(2):
                capture.capture_text(
                    f"Short memo {index}: {words} is settled.",
                    title=f"{words} memo {index}",
                    domain="project",
                    sensitivity="private",
                )
        for index in range(3):
            capture.capture_text(
                f"Short note {index}: {TOPICS['birch']} runs at dawn.",
                title=f"{TOPICS['birch']} note {index}",
                domain="project",
                sensitivity="private",
            )
        for index, text in enumerate((_words(TOPICS["orion"], 14_000), f"We settled the {TOPICS['orion']} plan.")):
            store.create_memory(
                {
                    "memory_key": f"decision.orion.{index}",
                    "memory_type": "decision",
                    "title": f"{TOPICS['orion']} decision {index}",
                    "canonical_text": text,
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "private",
                    "metadata_json": {},
                    "value": {"text": text},
                }
            )
    return database


def _vault_pack(database: Path, query: str, max_tokens: int | None) -> dict[str, object]:
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query=query, max_tokens=max_tokens, max_items=8)
        )


def test_three_of_four_questions_were_empty_at_500_tokens_and_none_are_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction from the probe, on a real SQLite vault.

    For orion, harbor and kestrel the item ranked first costs more than the
    budget, which is the situation that emptied the pack. For birch no item is
    large. Before the fix the first three packs were empty at 500 tokens.

    Every question here has a smaller item that fits whole, so the skip alone
    rescues the pack and the cut fallback has nothing to do. The test asserts
    both halves of that: the pack is not empty, and nothing in it was cut.

    Mutation: restore the latch (``self.truncated or`` in
    ``_TokenBudget.admit``). The pack is then empty after the first pass, the
    cut fallback rescues it with the large item cut, and ``cut_item_count`` is 1
    on a question where small items fit whole. The cut fallback alone hides the
    emptiness, which is why the test also checks that no cut happened and that
    the large first item is not the one packed.
    """

    _clear_env(monkeypatch)
    database = _vault(tmp_path)
    budget = 500

    empty: list[str] = []
    for key, words in TOPICS.items():
        unbudgeted = _vault_pack(database, words, None)
        ranked = [*unbudgeted["relevant_memories"], *unbudgeted["sources"]]
        first_cost = estimate_item_tokens(ranked[0])
        if key != "birch":
            assert first_cost > budget, f"{key}: the item ranked first must be too large for {budget}"

        pack = _vault_pack(database, words, budget)

        assert pack["budget"]["token_estimate"] <= budget, f"{key}: over budget"
        packed_ids = [item["id"] for item in [*pack["relevant_memories"], *pack["sources"]]]
        if not packed_ids:
            empty.append(key)
        assert "cut_item_count" not in pack["budget"], f"{key}: small items fit whole, so nothing is cut"
        if key != "birch":
            assert ranked[0]["id"] not in packed_ids, f"{key}: the large first item is skipped, not packed"
    assert empty == [], f"packs still empty at {budget} tokens: {empty}"


def test_a_vault_with_one_large_source_returns_a_cut_excerpt_through_the_mcp_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What an agent receives at the smallest budget the tool accepts.

    The harbor question has a 1,197-character excerpt first and two tiny memos
    behind it. This test removes the memos by asking a question only the large
    write-up answers, so nothing fits whole and the excerpt is cut. The cut
    reaches the agent with its marker, and the tool's token report says so.

    Mutation: remove ``cut_item_count`` from ``_TOKEN_REPORT_FIELDS`` in
    ``mcp/context.py``. The report then does not mention the cut.
    """

    _clear_env(monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        VNextCaptureService(store, actor_type="user_or_system").capture_text(
            _words(TOPICS["harbor"], 2_200),
            title="harbor ledger audit full write-up",
            domain="project",
            sensitivity="private",
        )
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)

    unbudgeted = call_mcp_tool(
        context, name="alice_context_pack", arguments={"query": TOPICS["harbor"], "max_tokens": 50_000}
    )
    assert unbudgeted["token_report"]["token_estimate"] > 500, "fixture: the source must not fit whole"
    assert "cut_item_count" not in unbudgeted["token_report"]

    payload = call_mcp_tool(
        context, name="alice_context_pack", arguments={"query": TOPICS["harbor"], "max_tokens": 500}
    )

    assert len(payload["sources"]) == 1, "the pack is empty again at the smallest budget the tool accepts"
    excerpt = payload["sources"][0]["excerpt"]
    # The tool presents a stored note in quotes, so the marker sits inside them.
    assert excerpt.removesuffix('"').endswith(CUT_MARKER)
    assert len(excerpt) < len(unbudgeted["sources"][0]["excerpt"])
    report = payload["token_report"]
    assert report["token_budget"] == 500
    assert report["token_estimate"] <= 500
    assert report["cut_item_count"] == 1
    assert report["truncated"] is True
