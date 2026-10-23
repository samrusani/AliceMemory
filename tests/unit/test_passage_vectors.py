"""``ALICE_SEARCH_QUALITY=vectors``: passage mode with the vectors of the chunks.

The vectors are scripted (see ``passage_vector_support``): a test names the cosine between the query and a chunk and the
stage does the rest. What is under test is what the stage does with a ranking, never how good a model's ranking is.

The tests here are the four checks of a new read path. **Fences**: a row the caller may not read cannot take a slot, move
a count or change a status, and a key with limits gets the same bytes whether such a row is hidden or missing.
**Cost**: the scan and the mask are bounded by what the caller may read and by what the result needs. **Id shapes**: no
chunk id and no new id appears in what a caller is shown. **Caches**: the resident copy follows scrub, supersede, a model
change and a write from another process, and it never reads the table row by row instead.

Each test names, in its docstring, the change to the code that must fail it. The cases ``passage-vectors-*`` of
``scripts/derived_label_mutations.json`` replay the ones that guard the fence, the reserved slots, the order inside a
document and the OR decision.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api.mcp.retrieval import (
    VECTORS_STORES,
    recall_source_ranking,
    recall_vector_note,
)
from alicebot_api.source_chunk_vector_search import (
    BREAKER_SECONDS,
    STATUS_BREAKER_OPEN,
    STATUS_ENABLED,
    STATUS_NONE,
    STATUS_OFF,
    STATUS_QUERY_FAILED,
    STATUS_STALE,
    breaker_is_open,
    discovery_depth,
    fuse_within_source,
    reserved_discovery_slots,
    reset_breaker,
)
from alicebot_api.source_embedding_policy import SOURCE_EMBEDDINGS_ENV, SOURCE_EMBEDDINGS_REMOTE_ENV
from alicebot_api.source_ranking import SOURCE_RANKING_MODES, SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import STAGE_DISABLED_MINIMAL, VNextRetrievalService
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import (
    CACHE_MAX_MB_ENV,
    chunk_vector_view,
    clear_registry,
    entry_counters,
)
from tests.unit.passage_vector_support import (
    MODEL,
    OPEN,
    QUERY,
    SEARCH_QUALITY_ENV,
    USER_ID,
    ScriptedProvider,
    ScriptedServer,
    add_note,
    embed_vault,
    fixed_id,
    hit_text,
    make_vault,
    mcp_context,
    plain_text,
    quiet_environment,
)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    quiet_environment(monkeypatch)


def _service(store: SQLiteVNextStore, provider: ScriptedProvider | None) -> VNextRetrievalService:
    return VNextRetrievalService(store, embedding_provider=provider, reranker_provider=None)


def _excerpts(
    database: Path,
    ranking: SourceRanking,
    provider: ScriptedProvider | None,
    *,
    query: str = QUERY,
    limit: int = 8,
    sensitivity: list[str] | None = None,
    domains: list[str] | None = None,
    scope=None,  # noqa: ANN001
):
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        return _service(store, provider).search_source_excerpts(
            query=query,
            domains=domains or [],
            sensitivity_allowed=sensitivity or OPEN,
            limit=limit,
            scope=scope,
            ranking=ranking,
            winning_memories=[],
        )


def _shown(entries) -> list[tuple[str, int | None]]:  # noqa: ANN001
    return [(str(entry.get("title")), entry.get("chunk_index")) for entry in entries]


def _rows(database: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(database)) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


def _blob(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


# -- the switch ---------------------------------------------------------------------------------------------------------


def test_vectors_is_its_own_mode_and_the_switch_maps_it() -> None:
    """Recorded as ``vectors``, never as ``passage``. Mutation: return ``cls.passage()`` for the value ``vectors``."""

    assert "vectors" in SOURCE_RANKING_MODES
    vectors = SourceRanking.vectors()
    assert vectors.mode == "vectors" and vectors != SourceRanking.passage() and vectors != SourceRanking.document()
    assert vectors.passages_per_source == SourceRanking.passage().passages_per_source == 3
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: " Vectors "}) == vectors
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "passage"}) == SourceRanking.passage()
    with pytest.raises(ValueError, match="vectors"):
        SourceRanking.from_environment({SEARCH_QUALITY_ENV: "sideways"})


class _NamedStore:
    """A store known only by its class name, which is all the edge reads."""


def _store_named(name: str):
    return type(name, (_NamedStore,), {})()


def test_the_edge_runs_vectors_on_sqlite_and_answers_as_passage_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite gets ``vectors``; PostgreSQL answers as ``passage`` would, or ``document`` for a key with limits.

    Mutation: let ``recall_source_ranking`` return ``vectors`` for a store that is not in ``VECTORS_STORES``.
    """

    monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
    assert VECTORS_STORES == frozenset({"SQLiteVNextStore"})
    assert recall_source_ranking(_store_named("SQLiteVNextStore"), None).mode == "vectors"
    assert recall_source_ranking(_store_named("PostgresVNextStore"), None) == SourceRanking.passage()
    limited = AgentIdentity(agent_id="limited", agent_type="test", permission_profile="read_only_agent", project_scope=("acme",))
    assert recall_source_ranking(_store_named("PostgresVNextStore"), limited) == SourceRanking.document()
    monkeypatch.delenv(SEARCH_QUALITY_ENV)
    assert recall_source_ranking(_store_named("SQLiteVNextStore"), None) == SourceRanking.passage()
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    assert recall_source_ranking(_store_named("SQLiteVNextStore"), None) == SourceRanking.passage()
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "off")
    assert recall_source_ranking(_store_named("SQLiteVNextStore"), None) == SourceRanking.document()


def test_the_edge_says_why_vectors_will_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: return no note for a store that keeps no chunk vectors; drop the minimal depth note."""

    sqlite_store, postgres_store = _store_named("SQLiteVNextStore"), _store_named("PostgresVNextStore")
    assert recall_vector_note(sqlite_store, "medium") is None
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    assert recall_vector_note(postgres_store, "medium") is None  # vectors was not asked for
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
    assert recall_vector_note(sqlite_store, "medium") is None  # the stage reports its own status
    assert recall_vector_note(sqlite_store, "minimal") == STAGE_DISABLED_MINIMAL
    assert recall_vector_note(postgres_store, "medium") == "disabled: store does not support chunk vectors"


# -- the order inside a document --------------------------------------------------------------------------------------------


def test_fusion_inside_a_document_blends_the_two_ranks_and_breaks_ties_by_the_lexical_rank() -> None:
    """Reciprocal rank fusion at k 60 over the chunks of one document.

    Mutation: sort by the vector rank alone; sort by the lexical rank alone; break ties by chunk id first.
    """

    lexical = ["h1", "h2", "h3", "h4", "h5", "h6"]
    vector = ["h6", "h5", "h4", "h1", "h2", "h3"]
    fused = fuse_within_source(lexical, vector)
    assert fused[:3] == ["h1", "h6", "h2"]
    # A chunk the words did not find is a candidate by its vector rank alone, and a chunk both lists rank well beats it.
    assert fuse_within_source(["a"], ["x", "a"]) == ["a", "x"]
    assert fuse_within_source([], ["x", "y"]) == ["x", "y"]
    assert fuse_within_source(["a", "b"], []) == ["a", "b"]
    assert fuse_within_source([], []) == []
    # Two lists that agree keep their order, and equal blended scores fall to the lexical rank, not to the id.
    assert fuse_within_source(["z", "y"], ["y", "z"]) == ["z", "y"]


def _lexical_vault(tmp_path: Path, cosines: dict[str, float]):
    """One document of six chunks that hold the query word 6 down to 1 times, so the words rank them h0..h5."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text(f"tagh{index}", 6 - index) for index in range(6)])
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    return database, provider


def test_the_vectors_choose_which_passages_of_a_document_fill_its_slots(tmp_path: Path) -> None:
    """The document keeps its three slots; the fused order fills them.

    Mutation: leave the within-document order out (``row = chunk``), so the lexical order stands.
    """

    cosines = {"tagh5": 0.99, "tagh4": 0.98, "tagh3": 0.97, "tagh0": 0.5, "tagh1": 0.4, "tagh2": 0.3}
    database, provider = _lexical_vault(tmp_path, cosines)
    passage, _ = _excerpts(database, SourceRanking.passage(), provider)
    vectors, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert _shown(passage) == [("Almanac", 0), ("Almanac", 1), ("Almanac", 2)]
    assert _shown(vectors) == [("Almanac", 0), ("Almanac", 5), ("Almanac", 1)]
    assert record["source"] == "vectors" and record["vector"]["status"] == STATUS_ENABLED  # type: ignore[index]
    assert record["vector"]["reordered_slots"] == 2  # type: ignore[index]


def test_a_chunk_with_no_word_match_enters_a_document_only_when_the_document_has_room_for_it(tmp_path: Path) -> None:
    """The slots are the number of passages passage mode gave the document: three, and then the words come first.

    A chunk both lists rank well beats one only the vectors rank. Mutation: give every document more slots than it had.
    """

    cosines = {"tagv": 0.999}
    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text(f"tagh{index}", 6 - index) for index in range(6)] + [plain_text("tagv")])
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    passage, _ = _excerpts(database, SourceRanking.passage(), provider)
    vectors, _ = _excerpts(database, SourceRanking.vectors(), provider)
    assert len(vectors) == len(passage) == 3
    assert ("Almanac", 6) not in _shown(vectors)


# -- the documents only the vectors found -----------------------------------------------------------------------------------


def _discovery_vault(tmp_path: Path, *, discovered: int = 3, lexical: int = 3):
    database = make_vault(tmp_path)
    cosines: dict[str, float] = {}
    for index in range(lexical):
        add_note(database, f"Lexical{index}", [hit_text(f"taglex{index}x{chunk}", 6 - chunk) for chunk in range(3)],
                 captured_at=f"2026-06-0{index + 1}T00:00:00Z")
        for chunk in range(3):
            cosines[f"taglex{index}x{chunk}"] = 0.5 - 0.1 * chunk  # the vectors agree with the words
    for index in range(discovered):
        add_note(database, f"Semantic{index}", [plain_text(f"tagsem{index}")])
        cosines[f"tagsem{index}"] = 0.95 - index * 0.05
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    return database, provider


def test_a_third_of_the_head_is_held_for_documents_only_the_vectors_found(tmp_path: Path) -> None:
    """Two of six head slots at the default limit of 8, best vector first.

    Mutation: remove the reserved slots (``discovery_slots = 0``).
    """

    database, provider = _discovery_vault(tmp_path)
    passage, _ = _excerpts(database, SourceRanking.passage(), provider, limit=8)
    vectors, record = _excerpts(database, SourceRanking.vectors(), provider, limit=8)
    titles = [title for title, _index in _shown(vectors)]
    assert [title for title, _ in _shown(passage)].count("Lexical0") == 3
    assert _shown(vectors)[:4] == _shown(passage)[:4] and all(t.startswith("Lexical") for t in titles[:4])
    assert titles[4:6] == ["Semantic0", "Semantic1"]
    assert len(vectors) == 8
    assert record["vector"]["discovered_in_head"] == 2  # type: ignore[index]


def test_the_reserved_share_scales_with_the_limit() -> None:
    """Mutation: reserve one slot whatever the limit; reserve none below limit 8."""

    assert [reserved_discovery_slots(head) for head in (0, 1, 2, 3, 4, 6, 9, 15)] == [0, 0, 0, 1, 1, 2, 3, 5]
    assert discovery_depth(8) == 32 and discovery_depth(1) == 32 and discovery_depth(20) == 80 and discovery_depth(10_000) == 2048


@pytest.mark.parametrize("limit,expected", [(4, 1), (8, 2), (12, 3)])
def test_the_head_of_a_larger_limit_holds_more_discovered_documents(tmp_path: Path, limit: int, expected: int) -> None:
    database, provider = _discovery_vault(tmp_path, discovered=6)
    _entries, record = _excerpts(database, SourceRanking.vectors(), provider, limit=limit)
    assert record["vector"]["discovered_in_head"] == expected  # type: ignore[index]


def test_with_no_word_match_at_all_only_the_reserved_share_goes_to_the_documents_the_vectors_found(tmp_path: Path) -> None:
    """The lexical list is empty, and the nearest documents still take only the reserved share.

    A vector search always has neighbours, so documents past the share would pad a sparse result with the nearest
    unrelated passages. Mutation: add the documents past the share to the overflow.
    """

    database = make_vault(tmp_path)
    cosines = {}
    for index in range(5):
        add_note(database, f"Semantic{index}", [plain_text(f"tagsem{index}")])
        cosines[f"tagsem{index}"] = 0.9 - index * 0.05
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    passage, _ = _excerpts(database, SourceRanking.passage(), provider)
    vectors, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert passage == []
    assert [title for title, _ in _shown(vectors)] == ["Semantic0", "Semantic1"]
    assert record["vector"]["discovered_in_head"] == 2  # type: ignore[index]
    assert record["candidate_count"] == 2


def test_the_or_pass_is_decided_on_the_lexical_list_alone(tmp_path: Path) -> None:
    """The vectors always return neighbours, so a decision that read them would never run the OR query.

    Two words that no chunk holds together: the strict query finds nothing, the OR query finds one. The label says the
    OR pass ran, with the vectors on. Mutation: let ``found`` count the vectors (``or ranking.mode == 'vectors'``).
    """

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    provider = ScriptedProvider({"tagsem": 0.9, "taga": 0.1}, query_text="zephyr gale")
    embed_vault(database, provider)
    _entries, passage_record = _excerpts(database, SourceRanking.passage(), provider, query="zephyr gale")
    entries, record = _excerpts(database, SourceRanking.vectors(), provider, query="zephyr gale")
    assert passage_record["chunk_fts_source"] == record["chunk_fts_source"] == "sqlite_fts_or_fallback"
    assert ("Almanac", 0) in _shown(entries) and ("Semantic", 0) in _shown(entries)


# -- when the vectors have nothing to add -------------------------------------------------------------------------------------


def _same_as_passage(passage, vectors) -> None:  # noqa: ANN001
    entries_passage, record_passage = passage
    entries_vectors, record_vectors = vectors
    assert _blob(entries_vectors) == _blob(entries_passage)
    shared = {key: value for key, value in record_vectors.items() if key not in {"source", "vector"}}
    assert shared == {key: value for key, value in record_passage.items() if key != "source"}


def test_an_empty_vector_list_gives_the_output_of_passage_mode_byte_for_byte(tmp_path: Path) -> None:
    """No vector stored, the opt-in off, a provider that fails, a mask that holds nothing, every one the same.

    Mutation: let the vector path touch the lexical rows when the stage is not enabled.
    """

    database = make_vault(tmp_path)
    cosines = {}
    for index in range(4):
        add_note(database, f"Almanac{index}", [hit_text(f"taga{index}x{chunk}", 6 - chunk) for chunk in range(4)])
    provider = ScriptedProvider(cosines)
    passage = _excerpts(database, SourceRanking.passage(), provider)
    assert passage[0]
    # No vector stored at all.
    none_stored = _excerpts(database, SourceRanking.vectors(), provider)
    _same_as_passage(passage, none_stored)
    assert none_stored[1]["vector"]["status"] == STATUS_NONE  # type: ignore[index]
    embed_vault(database, provider)
    # The opt-in off.
    off = _excerpts_with_env(database, provider, {SOURCE_EMBEDDINGS_ENV: None})
    _same_as_passage(passage, off)
    assert off[1]["vector"]["status"] == STATUS_OFF  # type: ignore[index]
    # No provider at all.
    no_provider = _excerpts(database, SourceRanking.vectors(), None)
    _same_as_passage(passage, no_provider)
    # A provider that fails the query.
    failing = ScriptedProvider(cosines, fail_queries=True)
    failed = _excerpts(database, SourceRanking.vectors(), failing)
    _same_as_passage(passage, failed)
    assert failed[1]["vector"]["status"] == STATUS_QUERY_FAILED  # type: ignore[index]
    # Every vector equal to the order the words gave: nothing to add.
    reset_breaker()
    agree = ScriptedProvider({f"taga{index}x{chunk}": 0.9 - 0.1 * chunk for index in range(4) for chunk in range(4)})
    from alicebot_api.source_chunk_embeddings import drop_chunk_vectors, sqlite_store_context

    drop_chunk_vectors(store_context=lambda: sqlite_store_context(database, USER_ID))
    embed_vault(database, agree)
    clear_registry()
    agreed = _excerpts(database, SourceRanking.vectors(), agree)
    assert agreed[1]["vector"]["status"] == STATUS_ENABLED  # type: ignore[index]
    assert agreed[1]["vector"]["reordered_slots"] == 0  # type: ignore[index]
    entries_agreed = [(e["title"], e["chunk_index"]) for e in agreed[0]]
    assert entries_agreed[:3] == [(e["title"], e["chunk_index"]) for e in passage[0]][:3]


def _excerpts_with_env(database: Path, provider: ScriptedProvider, changes: dict[str, str | None]):
    import os

    held = {name: os.environ.get(name) for name in changes}
    try:
        for name, value in changes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return _excerpts(database, SourceRanking.vectors(), provider)
    finally:
        for name, value in held.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_query_is_embedded_once_per_recall_with_the_model_prefix(tmp_path: Path) -> None:
    """One request for the query, with the family's query prefix. Mutation: embed the query once per document."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3), hit_text("tagb", 2), hit_text("tagc", 1)])
    add_note(database, "Rota", [hit_text("tagr", 3), hit_text("tags", 2)])
    provider = ScriptedProvider({}, model="nomic-embed-text:v1.5", query_text="search_query: zephyr")
    embed_vault(database, provider)
    _excerpts(database, SourceRanking.vectors(), provider)
    assert provider.queries == ["search_query: zephyr"]
    assert all(text.startswith("search_document: ") for batch in provider.batches for text in batch)


def test_a_query_with_credential_material_is_not_sent(tmp_path: Path) -> None:
    """The query is egress too. Mutation: leave the credential check out of ``_open``."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zC9bD1fG3hJ5"
    provider = ScriptedProvider({}, query_text=f"zephyr {token}")
    embed_vault(database, provider)
    _entries, record = _excerpts(database, SourceRanking.vectors(), provider, query=f"zephyr {token}")
    assert provider.queries == []
    assert record["vector"]["status"] == "skipped: query not sent (credential material)"  # type: ignore[index]


def test_a_remote_endpoint_is_not_asked_for_a_query_without_its_own_opt_in(tmp_path: Path, monkeypatch) -> None:
    """Mutation: leave the posture check out of ``_open``."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    local = ScriptedProvider({"taga": 0.9})
    embed_vault(database, local)
    remote = ScriptedProvider({"taga": 0.9}, base_url="http://embeddings.example.test/v1")
    _entries, record = _excerpts(database, SourceRanking.vectors(), remote)
    assert remote.queries == [] and record["vector"]["status"] == "disabled: remote endpoint not allowed"  # type: ignore[index]
    monkeypatch.setenv(SOURCE_EMBEDDINGS_REMOTE_ENV, "on")
    _entries, record = _excerpts(database, SourceRanking.vectors(), remote)
    # Allowed now, but the vectors in the vault were made at another endpoint: they are not this endpoint's.
    assert remote.queries == [QUERY] and record["vector"]["status"] == STATUS_STALE  # type: ignore[index]


def test_the_circuit_breaker_skips_the_stage_after_a_failure_and_closes_again(tmp_path: Path, monkeypatch) -> None:
    """After a failed query embedding the endpoint is not asked again for BREAKER_SECONDS.

    Mutation: leave out ``trip_breaker``; never close the breaker.
    """

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    good = ScriptedProvider({"taga": 0.9})
    embed_vault(database, good)
    failing = ScriptedProvider({"taga": 0.9}, fail_queries=True)
    first = _excerpts(database, SourceRanking.vectors(), failing)
    second = _excerpts(database, SourceRanking.vectors(), failing)
    assert first[1]["vector"]["status"] == STATUS_QUERY_FAILED  # type: ignore[index]
    assert second[1]["vector"]["status"] == STATUS_BREAKER_OPEN  # type: ignore[index]
    assert failing.queries == [QUERY]  # the second recall never asked
    assert breaker_is_open(failing)
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + BREAKER_SECONDS + 1)
    assert not breaker_is_open(failing)
    again = _excerpts(database, SourceRanking.vectors(), failing)
    assert again[1]["vector"]["status"] == STATUS_QUERY_FAILED and failing.queries == [QUERY, QUERY]  # type: ignore[index]


def test_any_error_in_the_vector_path_is_caught_and_the_result_is_passage_modes(tmp_path: Path, monkeypatch) -> None:
    """The stage never fails a recall, whether it breaks while opening or while ranking.

    Mutation: let an exception out of ``ChunkVectorSession.__enter__`` or out of ``compute``.
    """

    from alicebot_api.vnext_stores.sqlite import chunk_vector_cache as cache
    import alicebot_api.source_chunk_vector_search as search

    database = make_vault(tmp_path)
    for index in range(3):
        add_note(database, f"Almanac{index}", [hit_text(f"taga{index}x{chunk}", 6 - chunk) for chunk in range(4)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    provider = ScriptedProvider({"tagsem": 0.99})
    embed_vault(database, provider)
    passage = _excerpts(database, SourceRanking.passage(), provider)

    def broken_view(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the cache broke")

    monkeypatch.setattr(search, "chunk_vector_view", broken_view)
    opened = _excerpts(database, SourceRanking.vectors(), provider)
    _same_as_passage(passage, opened)
    assert opened[1]["vector"]["status"] == search.STATUS_ERROR  # type: ignore[index]
    monkeypatch.undo()
    quiet_environment(monkeypatch)

    def broken_scan(self, mask, query_vector, count):  # noqa: ANN001, ANN202
        raise RuntimeError("the scan broke")

    monkeypatch.setattr(cache.ChunkVectorView, "top_chunks", broken_scan)
    ranked = _excerpts(database, SourceRanking.vectors(), provider)
    _same_as_passage(passage, ranked)
    assert ranked[1]["vector"]["status"] == search.STATUS_ERROR  # type: ignore[index]


def test_the_postgresql_store_modules_do_not_know_the_vector_stage() -> None:
    """PostgreSQL behaviour is unchanged: nothing it runs names the chunk vectors or the shared SQLite predicate.

    Mutation: import a chunk vector module from ``vnext_store.py`` or from a PostgreSQL store module.
    """

    root = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
    paths = [root / "vnext_store.py", *sorted((root / "vnext_stores/postgres").glob("*.py"))]
    assert len(paths) > 5
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in ("chunk_vector", "source_chunk_embeddings", "source_chunk_vector_search", "source_readable"):
            assert token not in text, (path.name, token)


# -- the fence ----------------------------------------------------------------------------------------------------------------


def test_a_document_the_caller_may_not_read_takes_no_slot_and_moves_no_count(tmp_path: Path) -> None:
    """The mask is built from the readable sources before anything is ranked.

    Mutation: build the mask from every source (``view.mask(view._entry.source_ids)``).
    """

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)], sensitivity="public")
    add_note(database, "Visible", [plain_text("tagvis")], sensitivity="public")
    add_note(database, "Secret", [plain_text("tagsec")], sensitivity="confidential")
    provider = ScriptedProvider({"tagsec": 0.999, "tagvis": 0.6, "taga": 0.1})
    embed_vault(database, provider)
    entries, record = _excerpts(database, SourceRanking.vectors(), provider, sensitivity=["public"])
    assert "Secret" not in [title for title, _ in _shown(entries)] and ("Visible", 0) in _shown(entries)
    assert record["vector"]["readable_chunks"] == 2  # type: ignore[index]
    everything, everything_record = _excerpts(database, SourceRanking.vectors(), provider, sensitivity=[*OPEN, "confidential"])
    assert ("Secret", 0) in _shown(everything) and everything_record["vector"]["readable_chunks"] == 3  # type: ignore[index]


def _mirror_vaults(tmp_path: Path, *, hidden_by: str):
    """Two vaults that differ only by one row the caller may not read: present in the first, missing in the second."""

    names = {"with": tmp_path / "with", "without": tmp_path / "without"}
    cosines = {"tagsec": 0.999, "tagvis": 0.7, "taga": 0.2, "tagb": 0.3, "tagc": 0.4}
    vaults = {}
    for label, root in names.items():
        database = make_vault(root)
        add_note(database, "Almanac", [hit_text("taga", 4), hit_text("tagb", 3), hit_text("tagc", 2)],
                 metadata={"project_scope": ["acme"]})
        add_note(database, "Visible", [plain_text("tagvis")], metadata={"project_scope": ["acme"]})
        if label == "with":
            if hidden_by == "sensitivity":
                add_note(database, "Secret", [hit_text("tagsec", 5), plain_text("tagsec2")], sensitivity="confidential",
                         metadata={"project_scope": ["acme"]})
            elif hidden_by == "project":
                add_note(database, "Secret", [hit_text("tagsec", 5), plain_text("tagsec2")], metadata={"project_scope": ["other"]})
            elif hidden_by == "domain":
                add_note(database, "Secret", [hit_text("tagsec", 5), plain_text("tagsec2")], domain="health",
                         metadata={"project_scope": ["acme"]})
        provider = ScriptedProvider({**cosines, "tagsec2": 0.998})
        embed_vault(database, provider)
        vaults[label] = (database, provider)
    return vaults


@pytest.mark.parametrize("hidden_by", ["sensitivity", "project", "domain"])
def test_a_key_with_limits_gets_the_same_bytes_whether_a_row_is_hidden_or_missing(
    tmp_path: Path, hidden_by: str
) -> None:
    """Through ``alice_recall`` with ``debug``, in the debug record too. Mutation: build the mask from every source."""

    from alicebot_api.mcp.registry import call_mcp_tool

    vaults = _mirror_vaults(tmp_path, hidden_by=hidden_by)
    arguments = {
        "query": QUERY,
        "debug": True,
        "limit": 8,
        "sensitivity_allowed": ["public", "internal"],
        "project_scope": ["acme"],
        "domains": ["project"],
    }
    answers = {}
    for label, (database, provider) in vaults.items():
        with ScriptedServer(provider.cosines) as server:
            import os

            os.environ[EMBEDDINGS_BASE_URL_ENV] = server.base_url
            os.environ[EMBEDDINGS_MODEL_ENV] = MODEL
            os.environ[SEARCH_QUALITY_ENV] = "vectors"
            try:
                # The vault holds vectors made by the scripted provider at LOCAL_URL; this endpoint is another, so
                # embed again through the server so that the signature is the one the recall uses.
                from alicebot_api.source_chunk_embeddings import backfill_source_chunks, sqlite_store_context

                receipt = backfill_source_chunks(
                    store_context=lambda db=database: sqlite_store_context(db, USER_ID), environ={SOURCE_EMBEDDINGS_ENV: "on"}
                )
                assert receipt.pending == 0
                answers[label] = _blob(call_mcp_tool(mcp_context(database), name="alice_recall", arguments=arguments))
            finally:
                for name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, SEARCH_QUALITY_ENV):
                    os.environ.pop(name, None)
    assert answers["with"] == answers["without"]
    assert "Secret" not in answers["with"] and '"vector"' in answers["with"]
    assert '"status": "enabled"' in json.dumps(json.loads(answers["with"]), indent=1).replace('"status":"enabled"', '"status": "enabled"')


def test_the_status_is_the_same_when_only_a_hidden_row_has_vectors(tmp_path: Path) -> None:
    """``none embedded`` is judged after the mask. Mutation: judge it on all the rows of the table."""

    outputs = {}
    for label in ("with", "without"):
        database = make_vault(tmp_path / label)
        add_note(database, "Almanac", [hit_text("taga", 3)])
        if label == "with":
            add_note(database, "Secret", [hit_text("tagsec", 3)], sensitivity="confidential")
            provider = ScriptedProvider({"tagsec": 0.9})
            # Only the hidden source is embedded.
            assert _rows(database, "SELECT COUNT(*) FROM sources WHERE id = ?", (fixed_id("source", "Secret"),)) == [(1,)]
            from alicebot_api.source_chunk_embeddings import embed_new_sources, sqlite_store_context

            embed_new_sources([fixed_id("source", "Secret")], store_context=lambda db=database: sqlite_store_context(db, USER_ID),
                              environ={SOURCE_EMBEDDINGS_ENV: "on"}, provider=provider)
        else:
            provider = ScriptedProvider({})
        outputs[label] = _excerpts(database, SourceRanking.vectors(), provider, sensitivity=["public"])
    assert outputs["with"][1]["vector"]["status"] == STATUS_NONE  # type: ignore[index]
    assert _blob(outputs["with"]) == _blob(outputs["without"])


def test_the_stale_status_is_judged_after_the_mask(tmp_path: Path) -> None:
    """Vectors of another model exist only on a hidden source: for this caller there are none embedded at all."""

    outputs = {}
    for label in ("with", "without"):
        database = make_vault(tmp_path / label)
        add_note(database, "Almanac", [hit_text("taga", 3)])
        if label == "with":
            add_note(database, "Secret", [hit_text("tagsec", 3)], sensitivity="confidential")
            from alicebot_api.source_chunk_embeddings import embed_new_sources, sqlite_store_context

            embed_new_sources([fixed_id("source", "Secret")], store_context=lambda db=database: sqlite_store_context(db, USER_ID),
                              environ={SOURCE_EMBEDDINGS_ENV: "on"}, provider=ScriptedProvider({}, model="older-model"))
        outputs[label] = _excerpts(database, SourceRanking.vectors(), ScriptedProvider({}), sensitivity=["public"])
    assert outputs["with"][1]["vector"]["status"] == STATUS_NONE  # type: ignore[index]
    assert _blob(outputs["with"]) == _blob(outputs["without"])


# -- cost ---------------------------------------------------------------------------------------------------------------------


def test_the_scan_scores_only_the_rows_the_caller_may_read(tmp_path: Path, monkeypatch) -> None:
    """A project-scoped caller costs the rows of its sources, not the vault. Mutation: score every row, then mask."""

    from alicebot_api.vnext_stores.sqlite import chunk_vector_cache as cache

    database = make_vault(tmp_path)
    cosines = {}
    for index in range(40):
        scope = ["acme"] if index < 4 else ["other"]
        add_note(database, f"Note{index}", [plain_text(f"tagn{index}x{chunk}") for chunk in range(5)], metadata={"project_scope": scope})
        for chunk in range(5):
            cosines[f"tagn{index}x{chunk}"] = 0.5
    add_note(database, "Lex", [hit_text("taglex", 3)], metadata={"project_scope": ["acme"]})
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    scored: list[int] = []
    real = cache.ChunkVectorView._scores

    def spy(self, mask, positions, query, norm):  # noqa: ANN001, ANN202
        scored.append(int(positions.size))
        return real(self, mask, positions, query, norm)

    monkeypatch.setattr(cache.ChunkVectorView, "_scores", spy)
    from alicebot_api.vnext_retrieval import _ResolvedRetrievalScope

    scope = _ResolvedRetrievalScope(projects=frozenset({"acme"}), people=frozenset(), window_start=None, window_end=None,
                                    exclude_global_domains=frozenset())
    _excerpts(database, SourceRanking.vectors(), provider, scope=scope)
    total_rows = 40 * 5 + 1
    readable_rows = 4 * 5 + 1
    assert scored and scored[0] == readable_rows < total_rows
    assert sum(scored) <= readable_rows * 2  # the global scan, then the ranks inside the few documents the words found


def test_the_work_of_a_recall_does_not_grow_with_the_number_of_documents(tmp_path: Path) -> None:
    """The statements one recall runs are bounded by the result, not by the vault. Mutation: one read for each document."""

    counts = {}
    for size in (10, 60):
        database = make_vault(tmp_path / str(size))
        cosines = {}
        for index in range(size):
            add_note(database, f"Note{index}", [hit_text(f"tagn{index}", 2) if index % 3 == 0 else plain_text(f"tagn{index}")])
            cosines[f"tagn{index}"] = 0.3 + (index % 7) / 20
        provider = ScriptedProvider(cosines)
        embed_vault(database, provider)
        _excerpts(database, SourceRanking.vectors(), provider)  # build the resident copy
        statements: list[str] = []
        with sqlite_user_connection(database, USER_ID) as conn:
            conn.set_trace_callback(statements.append)
            _service(SQLiteVNextStore(conn, USER_ID), provider).search_source_excerpts(
                query=QUERY, domains=[], sensitivity_allowed=OPEN, limit=8, scope=None,
                ranking=SourceRanking.vectors(), winning_memories=[])
        counts[size] = len([s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))])
    assert counts[60] <= counts[10] + 12, counts


# -- id shapes ------------------------------------------------------------------------------------------------------------------


UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_no_chunk_id_and_no_new_id_is_in_what_a_caller_is_shown(tmp_path: Path, monkeypatch) -> None:
    """The ids in a recall answer are those of the documents returned, in passage mode and in vectors mode alike.

    Mutation: put a chunk id in the vector record or in an entry.
    """

    from alicebot_api.mcp.registry import call_mcp_tool

    database = make_vault(tmp_path)
    cosines = {"tagsem": 0.95, "taga": 0.2, "tagb": 0.3}
    add_note(database, "Almanac", [hit_text("taga", 3), hit_text("tagb", 2)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    with ScriptedServer(cosines) as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, MODEL)
        from alicebot_api.source_chunk_embeddings import backfill_source_chunks, sqlite_store_context

        backfill_source_chunks(store_context=lambda: sqlite_store_context(database, USER_ID), environ={SOURCE_EMBEDDINGS_ENV: "on"})
        monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
        passage = _blob(call_mcp_tool(mcp_context(database), name="alice_recall", arguments={"query": QUERY, "debug": True}))
        monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
        vectors = _blob(call_mcp_tool(mcp_context(database), name="alice_recall", arguments={"query": QUERY, "debug": True}))
    chunk_ids = {row[0] for row in _rows(database, "SELECT id FROM source_chunks")}
    source_ids = {fixed_id("source", "Almanac"), fixed_id("source", "Semantic")}
    for payload in (passage, vectors):
        assert not (set(UUID_PATTERN.findall(payload)) & chunk_ids)
        assert set(UUID_PATTERN.findall(payload)) <= source_ids
    assert fixed_id("source", "Semantic") in vectors and fixed_id("source", "Semantic") not in passage


def test_the_debug_record_of_the_source_stage_shows_only_counts_and_a_status(tmp_path: Path, monkeypatch) -> None:
    """``alice_recall`` debug gains the source stage record, with the fields that cannot count a row the caller may not read.

    Mutation: pass the whole stage record through (it counts provenance sources before the fence).
    """

    from alicebot_api.mcp.registry import call_mcp_tool

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    provider = ScriptedProvider({"taga": 0.5})
    embed_vault(database, provider)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    payload = call_mcp_tool(mcp_context(database), name="alice_recall", arguments={"query": QUERY, "debug": True})
    stage = payload["retrieval"]["stages"]["sources"]  # type: ignore[index]
    assert set(stage) == {"source", "candidate_count", "chunk_fts_source", "passages_per_source"}
    assert stage["source"] == "passage"
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, "http://127.0.0.1:9/v1")
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, MODEL)
    payload = call_mcp_tool(mcp_context(database), name="alice_recall", arguments={"query": QUERY, "debug": True})
    stage = payload["retrieval"]["stages"]["sources"]  # type: ignore[index]
    assert stage["source"] == "vectors" and set(stage["vector"]) == {
        "status", "readable_chunks", "stale_chunks", "discovered_in_head", "reordered_slots", "filler_picks"}


def test_minimal_depth_runs_no_vector_stage_and_says_so(tmp_path: Path, monkeypatch) -> None:
    """Mutation: let the minimal depth run the vector stage."""

    from alicebot_api.mcp.registry import call_mcp_tool

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    cosines = {"tagsem": 0.99}
    with ScriptedServer(cosines) as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, MODEL)
        from alicebot_api.source_chunk_embeddings import backfill_source_chunks, sqlite_store_context

        backfill_source_chunks(store_context=lambda: sqlite_store_context(database, USER_ID), environ={SOURCE_EMBEDDINGS_ENV: "on"})
        monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
        server.texts.clear()
        minimal = call_mcp_tool(mcp_context(database), name="alice_recall",
                                arguments={"query": QUERY, "debug": True, "context_depth": "minimal"})
        assert server.texts == []  # nothing was sent for the query
        monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
        passage = call_mcp_tool(mcp_context(database), name="alice_recall",
                                arguments={"query": QUERY, "debug": True, "context_depth": "minimal"})
    assert minimal["retrieval"]["stages"]["sources"]["vector"]["status"] == STAGE_DISABLED_MINIMAL  # type: ignore[index]
    assert minimal["sources"] == passage["sources"] and "Semantic" not in _blob(minimal["sources"])


# -- the resident copy -------------------------------------------------------------------------------------------------------------


def _counters(database: Path) -> dict[str, int]:
    import os

    return entry_counters(os.path.realpath(str(database)), USER_ID)


def test_a_new_vector_is_appended_without_a_rebuild_and_found(tmp_path: Path) -> None:
    """Rows that were added are found by their row number. Mutation: rebuild on every new row; never look for new rows."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    cosines = {"tagsem": 0.99, "taga": 0.1}
    provider = ScriptedProvider(cosines)
    embed_vault(database, provider)
    first, _ = _excerpts(database, SourceRanking.vectors(), provider)
    assert [t for t, _ in _shown(first)] == ["Almanac"]
    assert _counters(database)["rebuilds"] == 1
    add_note(database, "Semantic", [plain_text("tagsem")])
    embed_vault(database, provider)
    second, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert ("Semantic", 0) in _shown(second)
    counters = _counters(database)
    assert counters["rebuilds"] == 1 and counters["appends"] == 1 and counters["rows"] == 2
    assert record["vector"]["readable_chunks"] == 2  # type: ignore[index]


def test_a_memory_re_embed_does_not_rebuild_the_copy(tmp_path: Path) -> None:
    """The copy has its own stamp. Mutation: read ``embedding_stamp`` instead of the chunk stamp."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    provider = ScriptedProvider({"taga": 0.5})
    embed_vault(database, provider)
    _excerpts(database, SourceRanking.vectors(), provider)
    with sqlite_user_connection(database, USER_ID) as conn:
        conn.execute("UPDATE embedding_stamp SET token = 'a-memory-was-re-embedded' WHERE id = 1")
    _excerpts(database, SourceRanking.vectors(), provider)
    assert _counters(database)["rebuilds"] == 1


def test_a_scrub_and_a_replacement_take_their_vectors_out_of_the_copy(tmp_path: Path) -> None:
    """Mutation: leave the stamp trigger out; serve the copy without reading the stamp."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    add_note(database, "Other", [plain_text("tagoth")])
    provider = ScriptedProvider({"tagsem": 0.99, "tagoth": 0.98, "taga": 0.1})
    embed_vault(database, provider)
    before, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert {t for t, _ in _shown(before)} == {"Almanac", "Semantic", "Other"}
    with sqlite_user_connection(database, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).scrub_source(fixed_id("source", "Semantic"))
    after_scrub, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert {t for t, _ in _shown(after_scrub)} == {"Almanac", "Other"}
    assert _counters(database)["rebuilds"] == 2 and record["vector"]["readable_chunks"] == 2  # type: ignore[index]
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (fixed_id("source", "Other"),))
        from alicebot_api.vnext_stores.sqlite.source_chunk_vectors import delete_chunk_vectors

        delete_chunk_vectors(store, source_id=fixed_id("source", "Other"))
    after_replace, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert {t for t, _ in _shown(after_replace)} == {"Almanac"}
    assert _counters(database)["rebuilds"] == 3 and record["vector"]["readable_chunks"] == 1  # type: ignore[index]


def test_a_change_of_model_or_endpoint_makes_the_copy_for_the_new_signature(tmp_path: Path) -> None:
    """The vectors of the old model are counted stale, not used. Mutation: compare the vector width only."""

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    old = ScriptedProvider({"tagsem": 0.99}, model="model-one")
    embed_vault(database, old)
    assert _excerpts(database, SourceRanking.vectors(), old)[1]["vector"]["status"] == STATUS_ENABLED  # type: ignore[index]
    new = ScriptedProvider({"tagsem": 0.99}, model="model-two")
    entries, record = _excerpts(database, SourceRanking.vectors(), new)
    assert record["vector"]["status"] == STATUS_STALE and record["vector"]["stale_chunks"] == 2  # type: ignore[index]
    passage, _ = _excerpts(database, SourceRanking.passage(), new)
    assert _blob(entries) == _blob(passage)
    embed_vault(database, new)
    entries, record = _excerpts(database, SourceRanking.vectors(), new)
    assert record["vector"]["status"] == STATUS_ENABLED and record["vector"]["stale_chunks"] == 0  # type: ignore[index]
    assert ("Semantic", 0) in _shown(entries)


def test_a_write_from_another_process_is_seen_by_the_copy_of_this_one(tmp_path: Path, monkeypatch) -> None:
    """The stamp is in the database, so another process that drops the vectors rebuilds this one's copy.

    Mutation: keep the stamp in the process.
    """

    database = make_vault(tmp_path)
    add_note(database, "Almanac", [hit_text("taga", 3)])
    add_note(database, "Semantic", [plain_text("tagsem")])
    provider = ScriptedProvider({"tagsem": 0.99})
    embed_vault(database, provider)
    assert ("Semantic", 0) in _shown(_excerpts(database, SourceRanking.vectors(), provider)[0])
    environment = {key: value for key, value in __import__("os").environ.items() if not key.startswith(("ALICE_", "ALICEBOT_"))}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "apps/api/src")
    completed = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "reindex-embeddings", "--sources", "--drop", "--db", str(database),
         "--user-id", USER_ID],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    entries, record = _excerpts(database, SourceRanking.vectors(), provider)
    assert record["vector"]["status"] == STATUS_NONE and ("Semantic", 0) not in _shown(entries)  # type: ignore[index]
    assert _counters(database)["rebuilds"] == 2


def test_over_its_cap_the_stage_is_skipped_and_the_table_is_not_read_row_by_row(tmp_path: Path, monkeypatch) -> None:
    """Above the cap the status says so and the result is passage mode's. Mutation: fall back to a scan of the table."""

    database = make_vault(tmp_path)
    for index in range(30):
        add_note(database, f"Note{index}", [hit_text(f"tagn{index}", 2) if index % 5 == 0 else plain_text(f"tagn{index}")])
    provider = ScriptedProvider({f"tagn{index}": 0.5 for index in range(30)})
    embed_vault(database, provider)
    passage = _excerpts(database, SourceRanking.passage(), provider)
    monkeypatch.setenv(CACHE_MAX_MB_ENV, "0")
    statements: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        conn.set_trace_callback(statements.append)
        capped = _service(SQLiteVNextStore(conn, USER_ID), provider).search_source_excerpts(
            query=QUERY, domains=[], sensitivity_allowed=OPEN, limit=8, scope=None, ranking=SourceRanking.vectors(),
            winning_memories=[])
    assert capped[1]["vector"]["status"] == "skipped: resident vector cache over its cap"  # type: ignore[index]
    _same_as_passage(passage, capped)
    assert not [s for s in statements if "embedding" in s.lower() and "source_chunk_embeddings" in s and "SELECT chunk_id" in s]
    assert _counters(database)["rows"] == 0  # nothing stayed resident


_APPEND_PROBE = r"""
import json, resource, sqlite3, sys
import numpy as np
from alicebot_api.sqlite_schema import ensure_source_chunk_vectors_table
from alicebot_api.source_embedding_policy import ChunkSignature
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import chunk_vector_view

database, user, first_rows, more_rows = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
signature = ChunkSignature("p", "m", "e", 1, 1, 0)
rng = np.random.default_rng(7)
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
ensure_source_chunk_vectors_table(conn)

def insert(start, count):
    for low in range(start, start + count, 1000):
        high = min(low + 1000, start + count)
        block = rng.random((high - low, 768), dtype=np.float32)
        conn.executemany(
            "INSERT INTO source_chunk_embeddings (chunk_id, user_id, source_id, dimensions, embedding, provider, model, "
            "endpoint, signature_version, header_version, prefix_version, content_sha256) "
            "VALUES (?, ?, ?, 768, ?, 'p', 'm', 'e', 1, 1, 0, 'x')",
            [(f"c{low + i}", user, f"s{(low + i) % 50}", block[i].tobytes()) for i in range(high - low)],
        )
    conn.commit()

insert(0, first_rows)
conn.close()
with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        built = view.total_rows
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
insert(first_rows, more_rows)
conn.close()
scale = 1 if sys.platform == "darwin" else 1024
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        grown = view.total_rows
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
print(json.dumps({"built": built, "grown": grown, "delta": after - before, "matrix": first_rows * 768 * 4}))
"""


def test_an_append_does_not_hold_the_old_and_the_new_matrix_at_once(tmp_path: Path) -> None:
    """The matrix grows in place, so an append costs about the rows it adds and not a second copy of the matrix.

    Measured as the growth of the peak resident size of a process that builds a copy of 30,000 vectors and then
    appends 8,000 (``tracemalloc`` counts a resize as a second block, so it cannot tell the two apart).
    Mutation: grow the matrix with ``np.concatenate``, which holds the old and the new array at once.
    """

    import os

    database = make_vault(tmp_path)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("ALICE_", "ALICEBOT_"))}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "apps/api/src")
    completed = subprocess.run(
        [sys.executable, "-c", _APPEND_PROBE, str(database), USER_ID, "30000", "8000"],
        capture_output=True, text=True, env=environment, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["built"] == 30000 and result["grown"] == 38000
    assert result["delta"] < 0.6 * result["matrix"], result
