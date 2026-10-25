"""Passage mode retries with the OR query when the strict query finds no passage.

Document mode has always done this: a strict chunk query that finds nothing is retried once with ``match_any=True``
when the query holds two or more tokens that ``fts_fallback_tokens`` keeps, and the stage label ends in
``_or_fallback``. Passage mode ran the strict query only. A long question usually has a word that no chunk holds, so
its strict query matched nothing, passage mode returned no passage entry (none carried ``chunk_index``), and every
slot went to the provenance, title and temporal lists.

The tests here run the passage stage on a real SQLite vault, on stores that answer the strict and the OR query
separately, and through ``alice_recall``. Each test names the edit that must make it fail, and the cases
``passage-or-fallback-*`` of ``scripts/derived_label_mutations.json`` replay four of them. The PostgreSQL side is in
``tests/integration/test_passage_ranking_postgres.py``.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from alicebot_api.source_ranking import PASSAGES_PER_SOURCE, SEARCH_QUALITY_ENV, SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_retrieval import (
    SOURCE_PASSAGE_SCAN_COMPLETE,
    VNextRetrievalService,
    _ResolvedRetrievalScope,
)
from tests.unit.test_passage_ranking import (
    OTHER_USER_ID,
    SENSITIVITY,
    USER_ID,
    _blob,
    _chunk,
    _context,
    _door_call,
    _passages,
    _quiet,
    _source,
    _stdio_call,
    _store,
    _token,
)

LONG_QUESTION = "Who keeps the only spare key to the brass cabinet where the almanac is stored, and where does she hang it?"
SHORT_QUERY = "spare key cabinet"
OR_PASS = "sqlite_fts_or_fallback"
STRICT_PASS = "sqlite_fts"
KEY_CHUNKS = (
    "Marta Quill holds the only spare key to the brass cabinet",
    "She hangs it on the hook behind the kitchen clock",
    "The cabinet holds the lamp glass and the spare wicks",
)


def _seed_keys(store: SQLiteVNextStore) -> str:
    """One document that holds the answer, and one that holds none of the words."""

    keys = _source(store, "keys", title="Station log")
    for index, text in enumerate(KEY_CHUNKS):
        _chunk(store, keys, index, text)
    tides = _source(store, "tides", title="Tides")
    _chunk(store, tides, 0, "Tide tables are posted on the north wall")
    return keys


class _Spy:
    """The SQLite store with every ``search_source_chunks`` call recorded. ``wraps`` keeps the signature visible."""

    def __init__(self, store: SQLiteVNextStore) -> None:
        self._store = store
        self.calls: list[dict[str, object]] = []
        real = store.search_source_chunks

        @functools.wraps(real)
        def search_source_chunks(**kwargs: object):  # type: ignore[no-untyped-def]
            self.calls.append(dict(kwargs))
            return real(**kwargs)

        self.search_source_chunks = search_source_chunks

    def __getattr__(self, name: str) -> object:
        return getattr(self._store, name)

    @property
    def or_calls(self) -> list[dict[str, object]]:
        return [call for call in self.calls if call.get("match_any") is True]

    @property
    def strict_calls(self) -> list[dict[str, object]]:
        return [call for call in self.calls if "match_any" not in call]


def _run(store: object, query: str, ranking: SourceRanking, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "query": query,
        "domains": [],
        "sensitivity_allowed": SENSITIVITY,
        "limit": 8,
        "scope": None,
        "ranking": ranking,
        "winning_memories": [],
    }
    arguments.update(overrides)
    service = VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)  # type: ignore[arg-type]
    return service.search_source_excerpts(**arguments)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# A store that answers the strict and the OR query separately
# --------------------------------------------------------------------------


def _chunk_row(source_id: str, index: int, text: str, score: float) -> dict[str, object]:
    return {"id": f"{source_id}-{index}", "source_id": source_id, "chunk_index": index, "text": text, "fts_score": score}


def _source_row(source_id: str, *, sensitivity: str = "public", captured_at: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {
        "id": source_id,
        "title": "Inventory",
        "domain": "project",
        "sensitivity": sensitivity,
        "captured_at": captured_at,
        "metadata_json": {},
    }


class _ChunkStore:
    """Ignores every filter, as a legacy adapter may, and returns the strict rows or the OR rows it was given."""

    fts_stage_source = "fake_fts"

    def __init__(
        self,
        *,
        strict: list[dict[str, object]],
        relaxed: list[dict[str, object]],
        sources: list[dict[str, object]],
    ) -> None:
        self.strict = strict
        self.relaxed = relaxed
        self.sources = {str(row["id"]): row for row in sources}
        self.calls: list[dict[str, object]] = []

    def search_source_chunks(self, *, limit: int, match_any: bool = False, **_kwargs: object) -> list[dict[str, object]]:
        self.calls.append({"limit": limit, "match_any": match_any})
        return list((self.relaxed if match_any else self.strict)[:limit])

    def get_sources_by_ids(self, ids: object) -> list[dict[str, object]]:
        return [self.sources[str(item)] for item in ids if str(item) in self.sources]  # type: ignore[attr-defined]

    def search_sources(self, **_kwargs: object) -> list[dict[str, object]]:
        return []


# --------------------------------------------------------------------------
# The defect and the fix
# --------------------------------------------------------------------------


def test_a_long_question_gets_passages_from_the_or_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strict query holds "almanac" and matches no chunk. The OR query finds the key holder's chunks.

    Document mode answers the same question with one entry per document and no ``chunk_index``, as before.
    Mutation: remove the fallback from the passage stage, or run it without ``match_any``.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        keys = _seed_keys(store)
        passages, stage = _passages(store, LONG_QUESTION)
        document, document_stage = _passages(store, LONG_QUESTION, ranking=SourceRanking.document())

    assert [row["id"] for row in passages] == [keys, keys, keys]
    assert sorted(row["chunk_index"] for row in passages) == [0, 1, 2]
    assert all(row["excerpt_kind"] == "imported_source_material" for row in passages)
    assert any("spare key to the brass cabinet" in row["excerpt"] for row in passages)
    assert stage["chunk_fts"] == 3
    assert stage["passages_per_source"] == PASSAGES_PER_SOURCE == 3
    assert [row["id"] for row in document] == [keys]
    assert "chunk_index" not in document[0]
    assert document_stage["chunk_fts_source"] == OR_PASS


def test_the_stage_label_discloses_the_or_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result from the OR query says so, as document mode does. A strict result keeps the plain label.

    Mutation: drop the ``_or_fallback`` suffix, or append it to every pass.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        _seed_keys(store)
        _long, long_stage = _passages(store, LONG_QUESTION)
        _short, short_stage = _passages(store, SHORT_QUERY)
        _document, document_stage = _passages(store, LONG_QUESTION, ranking=SourceRanking.document())
    assert long_stage["chunk_fts_source"] == OR_PASS == document_stage["chunk_fts_source"]
    assert short_stage["chunk_fts_source"] == STRICT_PASS


def test_the_or_query_runs_once_and_only_after_an_empty_strict_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strict match is not retried. A miss is retried once, with the same limit and filters.

    Passage mode makes the same two calls document mode makes. Mutation: always run the OR query, or run it twice.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        _seed_keys(store)
        spy = _Spy(store)
        _run(spy, SHORT_QUERY, SourceRanking.passage())
        strict_hit = [dict(call) for call in spy.calls]

        spy.calls.clear()
        _run(spy, LONG_QUESTION, SourceRanking.passage())
        passage_calls = [dict(call) for call in spy.calls]

        spy.calls.clear()
        _run(spy, LONG_QUESTION, SourceRanking.document())
        document_calls = [dict(call) for call in spy.calls]

    assert [call.get("match_any") for call in strict_hit] == [None]
    assert [call.get("match_any") for call in passage_calls] == [None, True]
    assert [call.get("match_any") for call in document_calls] == [None, True]
    same_but_the_flag = {key: value for key, value in passage_calls[1].items() if key != "match_any"}
    assert same_but_the_flag == passage_calls[0]
    assert passage_calls[0]["query"] == LONG_QUESTION


@pytest.mark.parametrize("ranking", (SourceRanking.passage(), SourceRanking.document()), ids=("passage", "document"))
@pytest.mark.parametrize(
    "query",
    ("almanac", "what is the almanac", "the, an: almanac!"),
    ids=("one-word", "stopwords-around-one-word", "punctuation-around-one-word"),
)
def test_a_query_with_fewer_than_two_fallback_tokens_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ranking: SourceRanking, query: str
) -> None:
    """One word that no chunk holds is a miss. It is not retried, in either mode. One more word is retried.

    Mutation: let the passage stage retry a one-token query, count stopwords as tokens, or never retry.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        _seed_keys(store)
        spy = _Spy(store)
        rows, stage = _run(spy, query, ranking)
        assert len(spy.strict_calls) == 1
        assert not spy.or_calls
        assert rows == []
        assert stage["chunk_fts_source"] == STRICT_PASS

        spy.calls.clear()
        _run(spy, query + " spare", ranking)
        assert len(spy.strict_calls) == 1
        assert len(spy.or_calls) == 1


def test_two_fallback_tokens_are_enough_to_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary of the rule above. "almanac" alone is a miss, "almanac spare" runs the OR query.

    Mutation: require three tokens.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        keys = _seed_keys(store)
        passages, stage = _passages(store, "almanac spare")
    assert {row["id"] for row in passages} == {keys}
    assert all("chunk_index" in row for row in passages)
    assert stage["chunk_fts_source"] == OR_PASS


@pytest.mark.parametrize("door", ("call", "stdio"))
def test_alice_recall_returns_passages_for_a_long_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    """The defect as an agent met it: ``sources`` held no ``chunk_index`` for a long question.

    Mutation: remove the fallback from the passage stage.
    """

    _quiet(monkeypatch)
    context, database = _context(tmp_path)
    with _store(database) as conn:
        keys = _seed_keys(SQLiteVNextStore(conn, USER_ID))
    arguments = {"query": LONG_QUESTION}
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    payload = _door_call(context, "alice_recall", arguments) if door == "call" else _stdio_call(context, "alice_recall", arguments)
    sources = payload["sources"]
    assert payload["source_count"] == len(sources)
    assert {row["id"] for row in sources} == {keys}
    assert sorted(row["chunk_index"] for row in sources) == [0, 1, 2]
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "off")
    off = _door_call(context, "alice_recall", arguments)
    assert [row["id"] for row in off["sources"]] == [keys]
    assert "chunk_index" not in off["sources"][0]


# --------------------------------------------------------------------------
# The OR rows go through the same fences
# --------------------------------------------------------------------------


def test_hidden_rows_do_not_change_what_the_or_query_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confidential, a deleted and a foreign source match the OR query. The answer is the same without them.

    Mutation: resolve parents after the cap on the OR pass, or skip the parent lookup on it.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        keys = _seed_keys(store)
        before, before_stage = _passages(store, LONG_QUESTION, sensitivity_allowed=["public"])
        hidden = _source(store, "hidden", title="Inventory", sensitivity="confidential")
        _chunk(store, hidden, 0, "the spare key to the brass cabinet is in the confidential drawer")
        deleted = _source(store, "deleted", title="Inventory")
        _chunk(store, deleted, 0, "the spare key to the brass cabinet is in the deleted drawer")
        conn.execute("UPDATE sources SET deleted_at = ? WHERE id = ?", ("2026-07-01T00:00:00Z", deleted))
        ensure_sqlite_user(conn, OTHER_USER_ID, "other@alice")
    with sqlite_user_connection(database, OTHER_USER_ID) as conn:
        other = SQLiteVNextStore(conn, OTHER_USER_ID)
        foreign = _source(other, "foreign", title="Inventory")
        _chunk(other, foreign, 0, "the spare key to the brass cabinet is in the foreign drawer")
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        after, after_stage = _passages(store, LONG_QUESTION, sensitivity_allowed=["public"])
    assert before and {row["id"] for row in before} == {keys}
    # bm25 reads the statistics of the whole table, so extra rows can reorder equal-looking chunks. The set of
    # passages and the stage record are what must not move.
    assert {row["chunk_index"]: row for row in before} == {row["chunk_index"]: row for row in after}
    assert before_stage == after_stage
    assert after_stage["chunk_fts_source"] == OR_PASS
    blob = _blob(after)
    assert "confidential drawer" not in blob and "deleted drawer" not in blob and "foreign drawer" not in blob
    assert hidden not in blob and deleted not in blob and foreign not in blob


def test_scope_and_domain_drop_the_parent_of_an_or_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The project scope and the domain list apply to the OR pass as they do to the strict pass.

    Mutation: build the OR pass without the scope or domain arguments.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    scope = _ResolvedRetrievalScope(
        projects=frozenset({"acme"}),
        people=frozenset(),
        window_start=None,
        window_end=None,
        exclude_global_domains=frozenset(),
    )
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        project = _source(store, "project-note", title="Inventory", metadata_json={"project_scope": ["acme"]})
        _chunk(store, project, 0, "brass lantern in the acme cupboard")
        health = _source(store, "health-note", title="Inventory", domain="health")
        _chunk(store, health, 0, "brass lantern in the health cupboard")
        personal = _source(store, "personal-note", title="Inventory", metadata_json={"project_scope": ["personal-notes"]})
        _chunk(store, personal, 0, "brass lantern in the personal cupboard")
        spy = _Spy(store)
        scoped, stage = _passages(spy, "brass lantern almanac", domains=["project"], scope=scope)  # type: ignore[arg-type]
    blob = _blob(scoped)
    assert stage["chunk_fts_source"] == OR_PASS
    assert "acme cupboard" in blob
    assert "health cupboard" not in blob and "personal cupboard" not in blob
    assert health not in blob and personal not in blob
    assert spy.or_calls and spy.or_calls[0]["scope_projects"] == ("acme",)
    assert spy.or_calls[0]["domains"] == ["project"]


def test_a_hidden_or_row_is_dropped_before_the_duplicate_collapse() -> None:
    """The store hands back a later confidential copy of the same text on the OR pass. It must not win the hash.

    The store ignores the sensitivity list, so only ``_allowed`` can drop the row. Mutation: skip ``_allowed`` on the
    rows of the OR pass.
    """

    phrase = "brass lantern hangs above the chart table"
    store = _ChunkStore(
        strict=[],
        relaxed=[_chunk_row("hidden", 0, phrase, 10.0), _chunk_row("visible", 0, phrase, 1.0)],
        sources=[
            _source_row("hidden", sensitivity="confidential", captured_at="2026-08-01T00:00:00Z"),
            _source_row("visible"),
        ],
    )
    passages, stage = _run(store, "brass lantern chart almanac", SourceRanking.passage(), sensitivity_allowed=["public"])
    assert [row["id"] for row in passages] == ["visible"]
    assert stage["chunk_fts_source"] == "fake_fts_or_fallback"
    assert "hidden" not in _blob(passages)


def test_an_or_row_of_a_missing_parent_is_dropped() -> None:
    """A chunk whose parent does not come back (deleted, or another user's) takes no slot on the OR pass.

    Mutation: keep a chunk row whose parent the lookup did not return.
    """

    store = _ChunkStore(
        strict=[],
        relaxed=[_chunk_row("gone", 0, "brass lantern in the gone cupboard", 10.0), _chunk_row("here", 0, "brass lantern in the here cupboard", 1.0)],
        sources=[_source_row("here")],
    )
    passages, _stage = _run(store, "brass lantern almanac", SourceRanking.passage())
    assert [row["id"] for row in passages] == ["here"]


@pytest.mark.parametrize("hidden_by", ("sensitivity", "missing parent"))
def test_a_strict_row_the_caller_may_not_read_does_not_stop_the_or_query(hidden_by: str) -> None:
    """The decision is made on the rows the caller may read. The answer equals the one without the hidden row.

    The strict query returns one chunk, and its parent is above the caller's ceiling or does not come back. If
    that raw row stopped the OR query, the caller would get no passage and the plain label where a vault without
    the row gives the visible passage and ``_or_fallback``, and the difference would tell it that a row it may not
    read matches every word. Mutation: decide on the raw strict rows, or run the OR query whenever the strict
    query is not full.
    """

    hidden_row = _chunk_row("hidden", 0, "brass lantern almanac in the hidden cupboard", 10.0)
    visible_row = _chunk_row("visible", 0, "brass lantern in the visible cupboard", 1.0)
    sources = [_source_row("visible")]
    if hidden_by == "sensitivity":
        sources.append(_source_row("hidden", sensitivity="confidential"))
    with_row = _ChunkStore(strict=[hidden_row], relaxed=[hidden_row, visible_row], sources=sources)
    without_row = _ChunkStore(strict=[], relaxed=[visible_row], sources=[_source_row("visible")])
    query = "brass lantern almanac"
    shown, shown_stage = _run(with_row, query, SourceRanking.passage(), sensitivity_allowed=["public"])
    baseline, baseline_stage = _run(without_row, query, SourceRanking.passage(), sensitivity_allowed=["public"])
    assert [row["id"] for row in shown] == ["visible"]
    assert shown == baseline
    assert shown_stage == baseline_stage
    assert shown_stage["chunk_fts_source"] == "fake_fts_or_fallback"
    assert [call["match_any"] for call in with_row.calls] == [False, True]

    # Control: when the strict row is one the caller may read, it is the answer and the OR query stays off.
    readable = _ChunkStore(strict=[visible_row], relaxed=[hidden_row, visible_row], sources=sources)
    answer, answer_stage = _run(readable, query, SourceRanking.passage(), sensitivity_allowed=["public"])
    assert [row["id"] for row in answer] == ["visible"]
    assert answer_stage["chunk_fts_source"] == "fake_fts"
    assert [call["match_any"] for call in readable.calls] == [False]


# --------------------------------------------------------------------------
# The OR rows feed the same pipeline
# --------------------------------------------------------------------------


def test_the_or_pass_keeps_three_passages_of_one_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four chunks of one document match the OR query. Three come back.

    Mutation: skip the per-source cap on the OR pass.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "lamp", title="Inventory")
        for index, place in enumerate(("north", "south", "east", "west")):
            _chunk(store, source_id, index, f"brass lantern on the {place} pier")
        passages, stage = _passages(store, "brass lantern almanac")
    assert stage["chunk_fts_source"] == OR_PASS
    assert [row["id"] for row in passages] == [source_id] * 3
    assert len({row["chunk_index"] for row in passages}) == 3


def test_the_or_pass_drops_and_counts_a_credential_passage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flagged chunk does not come back on the OR pass and does not spend a slot.

    Mutation: skip ``credential_verdict`` on the OR pass.
    """

    _quiet(monkeypatch)
    secret = _token()
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "mixed", title="Inventory")
        _chunk(store, source_id, 0, f"brass lantern token {secret}")
        _chunk(store, source_id, 1, "brass lantern on the hook behind the clock")
        passages, stage = _passages(store, "brass lantern almanac")
    assert stage["chunk_fts_source"] == OR_PASS
    assert stage["credential_dropped"] == 1
    assert secret not in _blob(passages)
    assert any("hook behind the clock" in row["excerpt"] for row in passages)


def test_the_or_pass_collapses_exact_duplicates_to_the_source_captured_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same text in two sources. The later capture wins on the OR pass too.

    Mutation: skip the duplicate collapse on the OR pass.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    phrase = "brass lantern hangs above the chart table"
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        newer = _source(store, "newer", title="Inventory", captured_at="2026-08-01T00:00:00Z")
        _chunk(store, newer, 0, "  Brass   lantern hangs\nabove the chart table  ")
        older = _source(store, "older", title="Inventory", captured_at="2026-01-01T00:00:00Z")
        _chunk(store, older, 0, phrase.upper() + (" " * 80))
        passages, stage = _passages(store, "brass lantern chart almanac")
    assert stage["chunk_fts_source"] == OR_PASS
    assert [row["id"] for row in passages] == [newer]


def test_the_or_pass_deepens_past_one_documents_cap() -> None:
    """Eighty chunks of one document do not finish a request for eight passages on the OR pass either.

    Mutation: fetch the OR pass once, at the first depth.
    """

    rows = [_chunk_row("doc-a", index, f"brass lantern note {index}", 100 - index) for index in range(80)]
    rows += [_chunk_row(f"doc-{index}", 0, f"brass lantern from document {index}", 10 - index) for index in range(6)]
    store = _ChunkStore(
        strict=[],
        relaxed=rows,
        sources=[_source_row("doc-a")] + [_source_row(f"doc-{index}") for index in range(6)],
    )
    passages, stage = _run(store, "brass lantern almanac", SourceRanking.passage())
    assert len(passages) == 8
    assert len({row["id"] for row in passages}) > 1
    assert stage["chunk_scan"] == SOURCE_PASSAGE_SCAN_COMPLETE
    assert stage["chunk_fts_source"] == "fake_fts_or_fallback"
    assert [call["limit"] for call in store.calls if call["match_any"]] == [32, 64, 128]


# --------------------------------------------------------------------------
# A store that predates match_any
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ranking", (SourceRanking.passage(), SourceRanking.document()), ids=("passage", "document"))
def test_a_store_without_match_any_keeps_the_strict_result(ranking: SourceRanking) -> None:
    """The OR call raises ``TypeError`` on such a store. The strict result stands, with the plain label.

    The retry is attempted once, and its ``TypeError`` is the store's own. Mutation: skip the retry, let the
    ``TypeError`` out of the passage stage, or add the suffix without a result.
    """

    class _OldStore:
        fts_stage_source = "old_fts"

        def __init__(self) -> None:
            self.attempts: list[bool] = []

        def search_source_chunks(self, **kwargs: object) -> list[dict[str, object]]:
            self.attempts.append("match_any" in kwargs)
            return self._strict(**kwargs)  # type: ignore[arg-type]

        def _strict(self, *, query: str, domains: object, sensitivity_allowed: object, limit: int) -> list[dict[str, object]]:
            return []

        def get_sources_by_ids(self, _ids: object) -> list[dict[str, object]]:
            return []

        def search_sources(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

    store = _OldStore()
    rows, stage = _run(store, "brass lantern almanac", ranking)
    assert rows == []
    assert stage["chunk_fts_source"] == "old_fts"
    assert store.attempts == [False, True]
