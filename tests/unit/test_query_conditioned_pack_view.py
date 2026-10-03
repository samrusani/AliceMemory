"""Query-conditioned pack view: loops, facts, or sources from the query.

``classify_pack_view`` reads ``PACK_VIEW_LOOP_CUES`` then
``PACK_VIEW_SOURCE_CUES``. Default ``balanced`` packs that section first.
An explicit ``budget_strategy`` still wins. Import stays a source.
Commit stays a fact.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.session_briefing import compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import (
    PACK_VIEW_FACTS,
    PACK_VIEW_LOOP_CUES,
    PACK_VIEW_LOOPS,
    PACK_VIEW_SOURCE_CUES,
    PACK_VIEW_SOURCES,
    VNextRetrievalRequest,
    VNextRetrievalService,
    classify_pack_view,
    estimate_item_tokens,
)
from alicebot_api.project_view import ProjectView

from tests.unit.test_vnext_retrieval import InMemoryVNextRetrievalStore, _memory_row
from alicebot_api.vnext_source_fence import SourceReadFence

USER_ID = "00000000-0000-0000-0000-000000000001"
CANARY = "indigo-lighthouse-42"
OPEN_QUERY = "what's open?"
OPEN_LOOPS_QUERY = "what are the open loops?"
TODOS_QUERY = "what are my todos?"
CURLY_OPEN_QUERY = "what\u2019s open?"
WRITE_QUERY = f"what did I write about {CANARY}?"
CANDIDATE_TEXT = "Do not trust the unreviewed harbour rumour."
FAT_PADDING = "harbour-board-padding " * 50
UNSCOPED_FENCES = {
    "effective_domains": (),
    "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"),
    "effective_project_scope": (),
    # No project view on purpose: the library prints no project line or status line.
    "project_view": ProjectView.unscoped(),
    "exclude_global_domains": frozenset(),
}


def _clear_env(monkeypatch) -> None:
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    _clear_env(monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _fat_text(label: str) -> str:
    return f"{label} {CANARY} stays open on the harbour board. {FAT_PADDING}"


def _view_store() -> InMemoryVNextRetrievalStore:
    fact_text = _fat_text("Committed fact")
    loop_text = _fat_text("Open loop")
    source_text = _fat_text("Imported note I wrote")
    return InMemoryVNextRetrievalStore(
        memories=[_memory_row("memory-fat", fact_text)],
        sources=[
            {
                "id": "source-fat",
                "source_type": "manual_text",
                "title": f"Written note about {CANARY}",
                "content_hash": "sha256:pack-view",
                "domain": "project",
                "sensitivity": "private",
            }
        ],
        source_chunks=[
            {
                "id": "chunk-fat",
                "source_id": "source-fat",
                "chunk_index": 0,
                "text": source_text,
            }
        ],
        open_loops=[
            {
                "id": "loop-fat",
                "title": loop_text,
                "status": "open",
                "domain": "project",
                "sensitivity": "private",
            }
        ],
    )


def _compile_view_pack(
    query: str,
    *,
    max_tokens: int | None = None,
    budget_strategy: str = "balanced",
    projects: tuple[str, ...] = (),
    store: InMemoryVNextRetrievalStore | None = None,
) -> dict[str, object]:
    return VNextRetrievalService(store or _view_store()).compile_context_pack(
        VNextRetrievalRequest(
            query=query,
            domains=("project",),
            projects=projects,
            max_tokens=max_tokens,
            budget_strategy=budget_strategy,
        ),
        source_fence=SourceReadFence.unfenced()
    )


def _tight_one_section_budget(probe: dict[str, object]) -> int:
    memories = probe["relevant_memories"]
    loops = probe["open_loops"]
    sources = probe["sources"]
    assert memories and loops and sources, "fixture lost a section; later asserts are vacuous"
    costs = (
        estimate_item_tokens(memories[0]),
        estimate_item_tokens(loops[0]),
        estimate_item_tokens(sources[0]),
    )
    tight = max(costs)
    two_smallest = sum(sorted(costs)[:2])
    assert tight < two_smallest, f"packed costs {costs} cannot isolate one section"
    return tight


def test_classify_pack_view_reads_the_named_word_bounded_cues() -> None:
    """classify_pack_view reads PACK_VIEW_LOOP_CUES then PACK_VIEW_SOURCE_CUES.

    Fails if the helper drops ``what's open`` / ``what did I write``, or
    if bare ``open`` becomes the loops view.
    """

    assert "what's open" in PACK_VIEW_LOOP_CUES
    assert "what is open" in PACK_VIEW_LOOP_CUES
    assert "still open" in PACK_VIEW_LOOP_CUES
    assert "open loops" in PACK_VIEW_LOOP_CUES
    assert "todos" in PACK_VIEW_LOOP_CUES
    assert "what did i write" in PACK_VIEW_SOURCE_CUES
    assert "wrote about" in PACK_VIEW_SOURCE_CUES
    assert "written about" in PACK_VIEW_SOURCE_CUES
    assert classify_pack_view(OPEN_QUERY) == PACK_VIEW_LOOPS
    assert classify_pack_view(OPEN_LOOPS_QUERY) == PACK_VIEW_LOOPS
    assert classify_pack_view(TODOS_QUERY) == PACK_VIEW_LOOPS
    assert classify_pack_view(CURLY_OPEN_QUERY) == PACK_VIEW_LOOPS
    assert classify_pack_view("what is open") == PACK_VIEW_LOOPS
    assert classify_pack_view("still open") == PACK_VIEW_LOOPS
    assert classify_pack_view("waiting on legal") == PACK_VIEW_LOOPS
    assert classify_pack_view(WRITE_QUERY) == PACK_VIEW_SOURCES
    assert classify_pack_view("wrote about the harbour") == PACK_VIEW_SOURCES
    assert classify_pack_view("the hangar is open") == PACK_VIEW_FACTS
    assert classify_pack_view("open source") != PACK_VIEW_LOOPS
    assert classify_pack_view("todoist inbox") != PACK_VIEW_LOOPS
    assert classify_pack_view("where do I live") == PACK_VIEW_FACTS


def test_whats_open_packs_the_loop_under_a_tight_budget() -> None:
    """Tight budget, fat fact / source / loop: ``what's open?`` keeps the loop.

    The fact is not the surviving section. Mutation: skip
    ``classify_pack_view`` so default ``balanced`` always packs memories
    first. This test fails.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(OPEN_QUERY, max_tokens=tight)

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_LOOPS
    assert pack["trace"]["budget_strategy"] == "balanced"
    assert [row["id"] for row in pack["open_loops"]] == ["loop-fat"]
    assert pack["relevant_memories"] == []


def test_what_are_the_open_loops_packs_the_loop_under_a_tight_budget() -> None:
    """Same fixture: ``what are the open loops?`` keeps the loop.

    Mutation: drop ``open loops`` from ``PACK_VIEW_LOOP_CUES``. This test
    fails. Singular ``open loop`` does not match the plural phrase.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(OPEN_LOOPS_QUERY, max_tokens=tight)

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_LOOPS
    assert pack["trace"]["budget_strategy"] == "balanced"
    assert [row["id"] for row in pack["open_loops"]] == ["loop-fat"]
    assert pack["relevant_memories"] == []


def test_what_are_my_todos_packs_the_loop_under_a_tight_budget() -> None:
    """Same fixture: ``what are my todos?`` keeps the loop.

    Mutation: drop ``todos`` from ``PACK_VIEW_LOOP_CUES``. This test
    fails. Word-bounded ``todo`` does not match ``todos``.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(TODOS_QUERY, max_tokens=tight)

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_LOOPS
    assert pack["trace"]["budget_strategy"] == "balanced"
    assert [row["id"] for row in pack["open_loops"]] == ["loop-fat"]
    assert pack["relevant_memories"] == []


def test_curly_apostrophe_whats_open_packs_the_loop_under_a_tight_budget() -> None:
    """A U+2019 apostrophe in ``what’s open?`` still keeps the loop.

    Fails if ``classify_pack_view`` matches the ASCII cue without folding
    the curly mark first.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(CURLY_OPEN_QUERY, max_tokens=tight)

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_LOOPS
    assert pack["trace"]["budget_strategy"] == "balanced"
    assert [row["id"] for row in pack["open_loops"]] == ["loop-fat"]
    assert pack["relevant_memories"] == []


def test_what_did_i_write_packs_the_excerpt_under_a_tight_budget() -> None:
    """Same fixture: ``what did I write about indigo-lighthouse-42?`` keeps the excerpt.

    Mutation: skip the sources view in ``classify_pack_view``. This test
    fails.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(WRITE_QUERY, max_tokens=tight)

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_SOURCES
    assert pack["trace"]["budget_strategy"] == "balanced"
    assert [row["id"] for row in pack["sources"]] == ["source-fat"]
    assert any(CANARY in str(row.get("excerpt") or "") for row in pack["sources"])
    assert pack["relevant_memories"] == []


def test_explicit_facts_first_wins_over_an_open_loop_query() -> None:
    """Caller ``budget_strategy=facts_first`` still prefers facts.

    Fails if an open-loop query overrides an explicit strategy.
    """

    probe = _compile_view_pack(CANARY)
    tight = _tight_one_section_budget(probe)

    pack = _compile_view_pack(
        OPEN_QUERY,
        max_tokens=tight,
        budget_strategy="facts_first",
    )

    assert pack["query_interpretation"]["pack_view"] == PACK_VIEW_LOOPS
    assert pack["trace"]["budget_strategy"] == "facts_first"
    assert [row["id"] for row in pack["relevant_memories"]] == ["memory-fat"]
    assert pack["open_loops"] == []


def test_session_brief_follows_the_query_view_and_none_stays_facts_first(
    tmp_path: Path, monkeypatch
) -> None:
    """``what's open?`` lists ``**open loop**`` before ``**fact**``.

    ``query=None`` still lists facts first. Fails if the brief ignores
    ``classify_pack_view`` when ``query`` is set, or if the no-query path
    starts using the picker.
    """

    _context(tmp_path, monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        store.create_memory(
            {
                "memory_key": "decision.harbour.board",
                "memory_type": "decision",
                "title": "Harbour board",
                "canonical_text": f"Keep {CANARY} on the public board.",
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "project_scope": ["harbour"],
                "metadata_json": {"project_scope": ["harbour"]},
                "value": {"text": f"Keep {CANARY} on the public board."},
            }
        )
        store.create_open_loop(
            {
                "title": f"Follow the {CANARY} night watch",
                "status": "open",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"project_scope": ["harbour"]},
            }
        )

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        open_brief = compile_session_brief(store, **UNSCOPED_FENCES, query=OPEN_QUERY)
        none_brief = compile_session_brief(store, **UNSCOPED_FENCES, query=None)

    open_lines = open_brief.splitlines()
    none_lines = none_brief.splitlines()
    open_loop_lines = [line for line in open_lines if line.startswith("**open loop**:")]
    open_fact_lines = [line for line in open_lines if line.startswith("**fact**:")]
    none_loop_lines = [line for line in none_lines if line.startswith("**open loop**:")]
    none_fact_lines = [line for line in none_lines if line.startswith("**fact**:")]
    assert open_loop_lines and open_fact_lines, open_brief
    assert open_lines.index(open_loop_lines[0]) < open_lines.index(open_fact_lines[0])
    assert none_loop_lines and none_fact_lines, none_brief
    assert none_lines.index(none_fact_lines[0]) < none_lines.index(none_loop_lines[0])


def test_out_of_scope_loop_or_source_is_not_admitted() -> None:
    """Fence: an other-project loop or source stays out of a scoped pack.

    Unscoped compile must still see the other-project rows, or the scoped
    asserts prove nothing. Fails if ``projects`` is dropped on the pack
    request, or if the view picker admits before the fence.
    """

    store = InMemoryVNextRetrievalStore(
        memories=[
            _memory_row(
                "memory-acme",
                f"Acme fact about {CANARY} stays open.",
                project_id="acme",
            ),
            _memory_row(
                "memory-other",
                f"Other fact about {CANARY} stays open.",
                project_id="other",
            ),
        ],
        sources=[
            {
                "id": "source-acme",
                "title": f"Acme note about {CANARY}",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_id": "acme"},
            },
            {
                "id": "source-other",
                "title": f"Other note about {CANARY}",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_id": "other"},
            },
        ],
        open_loops=[
            {
                "id": "loop-acme",
                "title": f"Follow acme {CANARY}",
                "status": "open",
                "domain": "project",
                "sensitivity": "private",
                "project_id": "acme",
            },
            {
                "id": "loop-other",
                "title": f"Follow other {CANARY}",
                "status": "open",
                "domain": "project",
                "sensitivity": "private",
                "project_id": "other",
            },
        ],
    )

    unscoped = _compile_view_pack(OPEN_QUERY, store=store)
    assert {row["id"] for row in unscoped["open_loops"]} == {"loop-acme", "loop-other"}
    assert {row["id"] for row in unscoped["sources"]} == {"source-acme", "source-other"}

    scoped = _compile_view_pack(OPEN_QUERY, projects=("acme",), store=store)
    assert [row["id"] for row in scoped["open_loops"]] == ["loop-acme"]
    assert [row["id"] for row in scoped["sources"]] == ["source-acme"]
    assert "loop-other" not in {row["id"] for row in scoped["open_loops"]}
    assert "source-other" not in {row["id"] for row in scoped["sources"]}


def test_a_capture_candidate_stays_unsearchable_as_a_memory(
    tmp_path: Path, monkeypatch
) -> None:
    """A candidate is not packed or briefed as a fact.

    Fails if the pack or brief lists ``status=candidate`` rows as
    memories. Import stays a source. Commit stays a fact.
    """

    _context(tmp_path, monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        store.create_memory(
            {
                "memory_key": "decision.harbour.rumour",
                "memory_type": "decision",
                "title": "Harbour rumour",
                "canonical_text": CANDIDATE_TEXT,
                "status": "candidate",
                "domain": "project",
                "sensitivity": "public",
                "project_scope": ["harbour"],
                "metadata_json": {"project_scope": ["harbour"]},
                "value": {"text": CANDIDATE_TEXT},
            }
        )
        candidates = store.list_memories(status="candidate")
        committed = store.list_memories(status=None, statuses=("active", "accepted"))

    assert any(
        CANDIDATE_TEXT in str(row.get("canonical_text") or "") for row in candidates
    ), "fixture created no candidate; the no-promote assert is vacuous"
    assert not any(CANDIDATE_TEXT in str(row.get("canonical_text") or "") for row in committed)

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        pack = VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query=WRITE_QUERY),
            source_fence=SourceReadFence.unfenced()
        )
        brief = compile_session_brief(store, **UNSCOPED_FENCES, query=WRITE_QUERY)

    memory_blob = "\n".join(str(row.get("canonical_text") or "") for row in pack["relevant_memories"])
    assert CANDIDATE_TEXT not in memory_blob
    assert CANDIDATE_TEXT not in brief
    assert not any(
        line.startswith("**fact**:") and CANDIDATE_TEXT in line for line in brief.splitlines()
    )
