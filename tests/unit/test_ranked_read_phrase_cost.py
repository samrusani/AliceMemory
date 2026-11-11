"""Phrase ranks stay exact without a quadratic vocabulary join."""

from functools import wraps
from collections import Counter
from uuid import uuid4

import pytest

from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_stores.sqlite import readable_rank
from tests.unit.test_ranked_read_hidden_rows import USER, call, mint, vault as vault


LONG_TEXT = " ".join(f"token{i:02}" for i in range(33))


def _plant(store, stage, start, stop):
    for project in ("alpha", "beta"):
        labels = {"domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [project]}}
        for i in range(start, stop):
            text = f"weekly routine {LONG_TEXT} record {i}"
            if stage == "memory":
                store.create_memory({"memory_key": f"{project}.{i}", "title": "Record", "canonical_text": text,
                                     "value": {"text": text}, "memory_type": "procedure", "status": "active",
                                     "confidence": 1, "project_id": project, **labels})
            else:
                source = store.create_source({"source_type": "note", "title": "Record", "content_hash": str(uuid4()), **labels})
                store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": 0, "text": text})


@pytest.mark.parametrize("stage", ("memory", "source"))
@pytest.mark.parametrize("query", ('"weekly routine"', '"' + LONG_TEXT + '"'))
def test_phrase_vm_work_grows_below_quadratically(vault, monkeypatch, stage, query):
    """Guard: phrase joins use indexed statement-local postings, including long phrases."""
    key = mint(vault, "project_scoped_agent")
    method = "search_memories_fts" if stage == "memory" else "search_source_chunks"
    original = getattr(SQLiteVNextStore, method)
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    @wraps(original)
    def measured(self, **kwargs):
        self.conn.set_progress_handler(progress, 100)
        try:
            return original(self, **kwargs)
        finally:
            self.conn.set_progress_handler(None, 0)

    monkeypatch.setattr(SQLiteVNextStore, method, measured)
    costs = []
    start = 0
    for size in (100, 400):
        with sqlite_user_connection(vault, USER) as conn:
            _plant(SQLiteVNextStore(conn, USER), stage, start, size)
        steps = 0
        answer = call(vault, monkeypatch, key, "alice_recall", {"query": query, "limit": 5, "include_sources": stage == "source"})
        assert len(answer["results" if stage == "memory" else "sources"]) == 5
        costs.append(steps)
        start = size
    assert costs[0] > 0 and costs[1] <= 6 * costs[0], costs


@pytest.mark.parametrize("stage", ("memory", "source"))
def test_empty_materialization_hint_keeps_keyed_answers(vault, monkeypatch, stage):
    """Guard: every readable-rank CTE uses the version-gated shared hint."""
    with sqlite_user_connection(vault, USER) as conn:
        _plant(SQLiteVNextStore(conn, USER), stage, 0, 3)
    key = mint(vault, "project_scoped_agent")
    args = {"query": '"weekly routine"', "include_sources": stage == "source", "limit": 5}
    before = call(vault, monkeypatch, key, "alice_recall", args)
    assert len(before["results" if stage == "memory" else "sources"]) == 3
    statements = []
    original = SQLiteVNextStore._execute

    @wraps(original)
    def capture(self, query, params=()):
        statements.append(query)
        return original(self, query, params)

    monkeypatch.setattr(SQLiteVNextStore, "_execute", capture)
    monkeypatch.setattr(readable_rank, "CTE_MATERIALIZED_HINT", "", raising=False)
    assert call(vault, monkeypatch, key, "alice_recall", args) == before
    ranked = [sql for sql in statements if "rank_hits" in sql]
    assert ranked and all("MATERIALIZED" not in sql for sql in ranked)


@pytest.mark.parametrize("stage", ("memory", "source"))
def test_phrase_postings_are_collected_once_inside_the_population(vault, monkeypatch, stage):
    """Guard: each distinct token is collected once, after the readable-population fence."""
    with sqlite_user_connection(vault, USER) as conn:
        _plant(SQLiteVNextStore(conn, USER), stage, 0, 3)
        if stage == "memory":
            readable = {row["rowid"] for row in conn.execute("SELECT rowid FROM memories WHERE project_id='alpha'")}
        else:
            readable = {row["rowid"] for row in conn.execute(
                "SELECT c.rowid FROM source_chunks c JOIN sources s ON s.id=c.source_id "
                "WHERE json_extract(s.metadata_json,'$.project_scope[0]')='alpha'")}
    key = mint(vault, "project_scoped_agent")
    batches = []
    installed = set()
    original = SQLiteVNextStore._execute
    vocabulary = "alice_memory_terms" if stage == "memory" else "alice_chunk_terms"

    def record(doc, term, col, offset):
        batches[-1].append((doc, term, col, offset))
        return doc

    @wraps(original)
    def capture(self, query, params=()):
        if f"CROSS JOIN temp.{vocabulary} posting" in query:
            if id(self.conn) not in installed:
                self.conn.create_function("capture_posting", 4, record)
                installed.add(id(self.conn))
            batches.append([])
            query = query.replace("posting.term,posting.doc,posting.col,posting.offset",
                                  "posting.term,capture_posting(posting.doc,posting.term,posting.col,posting.offset) AS doc,posting.col,posting.offset")
        return original(self, query, params)

    monkeypatch.setattr(SQLiteVNextStore, "_execute", capture)
    answer = call(vault, monkeypatch, key, "alice_recall", {
        "query": 'weekly_routine "weekly routine"', "limit": 5, "include_sources": stage == "source",
    })
    assert len(answer["results" if stage == "memory" else "sources"]) == 3
    assert batches and any(batches)
    for batch in batches:
        assert {posting[0] for posting in batch} <= readable
        assert all(count == 1 for count in Counter(batch).values())


@pytest.mark.parametrize("stage", ("memory", "source"))
@pytest.mark.parametrize("query", ('"weekly weekly"', '"weekly weekly" "weekly weekly" routine', "weekly_weekly"))
def test_overlapping_phrase_scores_equal_native_bm25(vault, monkeypatch, stage, query):
    """Guard: postings keep column, offset and repeated phrase contributions exactly."""
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for i, text in enumerate(("weekly weekly weekly routine", "weekly weekly routine " * 4, "weekly routine " * 30)):
            labels = {"domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["alpha"]}}
            if stage == "memory":
                store.create_memory({"memory_key": f"overlap.{i}", "title": "weekly weekly" if i == 2 else "Record",
                                     "canonical_text": text, "value": {"text": text}, "memory_type": "procedure",
                                     "status": "active", "confidence": 1, "project_id": "alpha", **labels})
            else:
                source = store.create_source({"source_type": "note", "title": "Record", "content_hash": str(uuid4()), **labels})
                store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": 0, "text": text})
    observed = {}
    method = "search_memories_fts" if stage == "memory" else "search_source_chunks"
    original = getattr(SQLiteVNextStore, method)

    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        observed.update({str(row["id"]): row["fts_score"] for row in rows})
        return rows

    monkeypatch.setattr(SQLiteVNextStore, method, capture)
    args = {"query": query, "limit": 10, "include_sources": stage == "source"}
    owner = call(vault, monkeypatch, None, "alice_recall", args)
    scores = dict(observed)
    observed.clear()
    key = mint(vault, "project_scoped_agent")
    answer = call(vault, monkeypatch, key, "alice_recall", args)
    assert observed == pytest.approx(scores, rel=1e-12, abs=1e-15)
    section = "results" if stage == "memory" else "sources"
    assert len(owner[section]) >= 2
    assert [row["id"] for row in answer[section]] == [row["id"] for row in owner[section]]
