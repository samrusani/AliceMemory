"""Hidden rows must not affect ranked reads made through real keyed tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
from functools import wraps
from contextlib import contextmanager
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key

USER = "00000000-0000-0000-0000-000000000001"
QUERY = "luminousmigration amberrebase"
TEXTS = ("luminousmigration luminousmigration shared words", "amberrebase shared words")
PROFILES = ("project_scoped_agent", "read_only_agent", "admin_agent")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    for key in ("ALICE_AGENT_API_KEY", "ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_RERANKER_BASE_URL", "ALICE_RERANKER_MODEL", "ALICE_SEARCH_QUALITY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    db = tmp_path / "memory.db"
    bootstrap_database(db, user_id=USER, user_email="fixture@example.invalid")
    return db


def mint(db, profile, project="alpha"):
    with sqlite_user_connection(db, USER) as conn:
        _, key = create_agent_key(SQLiteVNextStore(conn, USER), user_id=USER, agent_id="reader-" + profile, permission_profile=profile, project_scope=project)
    return key


def call(db, monkeypatch, key, tool, args):
    if key is None:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    answer = call_mcp_tool(MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=USER), name=tool, arguments=args)
    return json.loads(json.dumps(answer, default=str))


def visible(db, monkeypatch, stage, key=None):
    ids = []
    for text in TEXTS:
        if stage == "memory":
            result = call(db, monkeypatch, key, "alice_memory_commit", {"title": "Migration note", "canonical_text": text, "memory_type": "procedure", "domain": "project", "sensitivity": "public", "project_scope": ["alpha"]})
            assert result["status"] == "committed"
            ids.append(result["memory"]["id"])
        else:
            result = call(db, monkeypatch, key, "alice_capture", {"title": "Captured note", "raw_text": text, "domain": "project", "sensitivity": "public", "project_scope": ["alpha"]})
            ids.append(result["source_id"])
    return ids


def add_hidden(store, stage, hiding, count=500):
    """Bulk fixture writes; the readers always enter through their real door."""
    project = "beta" if hiding == "project" else "alpha"
    sensitivity = "confidential" if hiding == "sensitivity" else "public"
    for i in range(count):
        text = f"luminousmigration hidden material {i}"
        labels = {"domain": "project", "sensitivity": sensitivity, "metadata_json": {"project_scope": [project]}}
        if stage == "memory":
            store.create_memory({"memory_key": str(uuid4()), "memory_type": "procedure", "title": "Hidden note", "canonical_text": text, "value": {"text": text}, "status": "active", "confidence": 1.0, "project_id": project, **labels})
        else:
            source = store.create_source({"source_type": "note", "title": "Hidden capture", "content_hash": str(uuid4()), **labels})
            store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": 0, "text": text})


def normalized(answer):
    # These two fields identify each pack request, not the selected data.
    return {key: value for key, value in answer.items() if key not in {"context_pack_id", "trace_id"}}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("hiding", ("project", "sensitivity"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_recall_is_equal_when_hidden_rows_are_added(vault, monkeypatch, profile, hiding, stage):
    """Guard: keyed memory and passage stages use only readable corpus statistics."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, profile)
    args = {"query": QUERY, "limit": 5, "include_sources": stage == "source"}
    if profile == "admin_agent" and hiding == "sensitivity":
        args["sensitivity_allowed"] = ["public", "internal", "private", "unknown"]
    before = call(vault, monkeypatch, key, "alice_recall", args)
    section = "results" if stage == "memory" else "sources"
    assert {row["id"] for row in before[section]} == set(ids)
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, hiding)
    assert call(vault, monkeypatch, key, "alice_recall", args) == before


@pytest.mark.parametrize("stage", ("memory", "source"))
def test_context_pack_is_equal_when_hidden_rows_are_added(vault, monkeypatch, stage):
    """Guard: pack memory and document stages pass the caller's rank fence."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, "project_scoped_agent")
    args = {"query": QUERY, "include_sources": True}
    before = normalized(call(vault, monkeypatch, key, "alice_context_pack", args))
    section = "memories" if stage == "memory" else "sources"
    assert {row["id"] for row in before[section]} == set(ids)
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, "project")
    assert normalized(call(vault, monkeypatch, key, "alice_context_pack", args)) == before


def test_recall_document_mode_is_equal_when_hidden_rows_are_added(vault, monkeypatch):
    """Guard: document-mode recall passes the same rank fence as passage mode."""
    ids = visible(vault, monkeypatch, "source")
    key = mint(vault, "project_scoped_agent")
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "off")
    args = {"query": QUERY, "include_sources": True, "limit": 5}
    before = call(vault, monkeypatch, key, "alice_recall", args)
    assert {row["id"] for row in before["sources"]} == set(ids)
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), "source", "project")
    assert call(vault, monkeypatch, key, "alice_recall", args) == before


def test_brief_is_equal_when_hidden_rows_are_added(vault, monkeypatch):
    """Guard: the keyed brief passes its identity's rank fence to the document stage."""
    visible(vault, monkeypatch, "source")
    key = mint(vault, "project_scoped_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    command = [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault), "--query", QUERY, "--scope", "all"]
    before = subprocess.check_output(command, text=True, env=os.environ)
    assert "luminousmigration" in before and "amberrebase" in before
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), "source", "project")
    assert subprocess.check_output(command, text=True, env=os.environ) == before


@pytest.mark.parametrize("profile", (None, "admin_agent", "trusted_local_agent", "read_only_agent"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_unrestricted_recall_keeps_bm25_order(vault, monkeypatch, profile, stage):
    """Guard: row-only rank is selected only for an identity with read limits."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, profile, project=None) if profile == "admin_agent" else None
    declared = {"agent_id": "reader", "permission_profile": profile} if profile in {"trusted_local_agent", "read_only_agent"} else {}
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, "project")
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": stage == "source", "projects": ["alpha"], "limit": 5, **declared})
    section = "results" if stage == "memory" else "sources"
    assert [row["id"] for row in result[section]] == ids[::-1], "bm25 reverses these rows; row-only counts do not"


@pytest.mark.parametrize("expired", (False, True))
def test_what_changed_is_equal_when_hidden_rows_are_added(vault, monkeypatch, expired):
    """Guard: decision-chain starts use the fenced row-only memory stage."""
    for text in TEXTS:
        result = call(vault, monkeypatch, None, "alice_memory_commit", {"title": "Migration decision", "canonical_text": text, "memory_type": "decision", "domain": "project", "sensitivity": "public", "project_scope": ["alpha"]})
        assert result["status"] == "committed"
        if expired:
            with sqlite_user_connection(vault, USER) as conn:
                conn.execute("UPDATE memories SET valid_to='2000-01-01T00:00:00+00:00' WHERE id=?", (result["memory"]["id"],))
    key = mint(vault, "project_scoped_agent")
    # Expired decisions belong to history, never to ordinary recall.
    recall = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": False})
    assert recall["count"] == (0 if expired else 2)
    args = {"topic": QUERY}
    before = call(vault, monkeypatch, key, "alice_what_changed", args)
    assert len(before["chains"]) == 2
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        add_hidden(store, "memory", "project")
        conn.execute("UPDATE memories SET memory_type = 'decision' WHERE project_id = 'beta'")
    assert call(vault, monkeypatch, key, "alice_what_changed", args) == before


@pytest.mark.parametrize(
    "column, repeats, competitor",
    [
        ("title", 1, {"canonical_text": "luminousmigration", "summary": "luminousmigration"}),
        ("canonical_text", 2, {"summary": "luminousmigration " * 3}),
        ("summary", 3, {"canonical_text": "luminousmigration"}),
        ("memory_key", 2, {"canonical_text": "luminousmigration", "summary": "luminousmigration"}),
        ("fact_keys", 5, {"canonical_text": "luminousmigration"}),
    ],
)
@pytest.mark.parametrize("ordinary", (False, True))
def test_fenced_rank_weights_each_searchable_column(vault, monkeypatch, column, repeats, competitor, ordinary):
    """Guard: each native BM25 column weight and each zero-population fallback weight."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        for mid in ids:
            conn.execute("UPDATE memories SET title='Neutral', canonical_text='Neutral', summary=NULL, fact_keys=NULL WHERE id=?", (mid,))
        # The column names are the fixed parameter table, never user input.
        conn.execute(f"UPDATE memories SET {column}=? WHERE id=?", ("luminousmigration " * repeats, ids[0]))
        for name, text in competitor.items():
            conn.execute(f"UPDATE memories SET {name}=? WHERE id=?", (text, ids[1]))
        if not ordinary:
            _derive(SQLiteVNextStore(conn, USER), ids)
        elif column == "fact_keys":
            conn.execute("UPDATE memories SET fact_keys=? WHERE id=?", ("luminousmigration " * 20, ids[0]))
    key = mint(vault, "project_scoped_agent")
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": False})
    assert [row["id"] for row in result["results"]] == ids


@pytest.mark.parametrize("profile", (None, "admin_agent", "trusted_local_agent", "read_only_agent"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_unrestricted_context_pack_keeps_bm25_order(vault, monkeypatch, profile, stage):
    """Guard: a pack's request scope does not activate the limited-caller score."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, profile, project=None) if profile == "admin_agent" else None
    declared = {"agent_id": "reader", "permission_profile": profile} if profile in {"trusted_local_agent", "read_only_agent"} else {}
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, "project")
    result = call(vault, monkeypatch, key, "alice_context_pack", {"query": QUERY, "include_sources": True, "projects": ["alpha"], **declared})
    section = "memories" if stage == "memory" else "sources"
    assert [row["id"] for row in result[section]] == ids[::-1]


def test_session_start_brief_is_equal_when_hidden_rows_are_added(vault, monkeypatch):
    """Guard: the authenticated session-start brief reaches the row-only source stage."""
    visible(vault, monkeypatch, "source")
    call(vault, monkeypatch, None, "alice_memory_commit", {"title": "Current task", "canonical_text": QUERY, "memory_type": "procedure", "domain": "project", "sensitivity": "public", "project_scope": ["alpha"]})
    key = mint(vault, "project_scoped_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    command = [sys.executable, "-m", "alicebot_api.session_start_hook", "--data-dir", str(vault.parent), "--format", "markdown"]
    before = subprocess.check_output(command, input="{}", text=True, env=os.environ)
    assert "luminousmigration luminousmigration" in before and "amberrebase shared" in before
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), "source", "project")
    assert subprocess.check_output(command, input="{}", text=True, env=os.environ) == before


def test_fenced_rank_ignores_unmatched_text_length(vault, monkeypatch):
    """Guard: a vault with no ordinary memory uses the row-alone fallback."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        conn.execute("UPDATE memories SET canonical_text=canonical_text || ? WHERE id=?", (" irrelevant" * 100, ids[1]))
        _derive(SQLiteVNextStore(conn, USER), ids)
    key = mint(vault, "project_scoped_agent")
    statements = []
    original = SQLiteVNextStore._execute
    @wraps(original)
    def capture(self, query, params=()):
        statements.append(query)
        return original(self, query, params)
    monkeypatch.setattr(SQLiteVNextStore, "_execute", capture)
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": False})
    assert [row["id"] for row in result["results"]] == ids
    assert not any("rank_hits" in query and "FROM memories_fts" in query for query in statements)


@pytest.mark.parametrize("hidden_kind", ("stored", "derived"))
def test_what_changed_fences_a_hidden_predecessor(vault, monkeypatch, hidden_kind):
    """Guard: a history walk still judges stored and derived labels after its start was admitted."""
    ids = []
    for text in ("Private predecessor marker", "luminousmigration decision"):
        result = call(vault, monkeypatch, None, "alice_memory_commit", {"title": "Decision", "canonical_text": text, "memory_type": "decision", "domain": "project", "sensitivity": "public", "project_scope": ["alpha"]})
        assert result["status"] == "committed"
        ids.append(result["memory"]["id"])
    with sqlite_user_connection(vault, USER) as conn:
        conn.execute("UPDATE memories SET supersedes=? WHERE id=?", (ids[0], ids[1]))
        conn.execute("UPDATE memories SET superseded_by=? WHERE id=?", (ids[1], ids[0]))
        if hidden_kind == "stored":
            conn.execute("UPDATE memories SET sensitivity='confidential', domain='legal' WHERE id=?", (ids[0],))
        else:
            source = SQLiteVNextStore(conn, USER).create_source({"source_type": "note", "title": "Restricted source", "content_hash": "restricted", "domain": "legal", "sensitivity": "confidential"})
            conn.execute("UPDATE memories SET metadata_json=json_set(metadata_json, '$.source_id', ?) WHERE id=?", (str(source["id"]), ids[0]))
    key = mint(vault, "project_scoped_agent")
    answer = call(vault, monkeypatch, key, "alice_what_changed", {"topic": "luminousmigration"})
    dumped = json.dumps(answer)
    assert ids[1] in dumped and len(answer["chains"]) == 1
    assert ids[0] not in dumped and "Private predecessor marker" not in dumped


def _derive(store, ids, *, hidden=False):
    source = store.create_source({"source_type": "note", "title": "Recorded input", "content_hash": str(uuid4()),
                                  "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["alpha"]}})
    refs = [str(source["id"])]
    if hidden:
        private = store.create_source({"source_type": "note", "title": "Restricted input", "content_hash": str(uuid4()),
                                       "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": ["alpha"]}})
        refs.append(str(private["id"]))
    for mid in ids:
        store.conn.execute("UPDATE memories SET metadata_json=? WHERE id=?", (json.dumps({
            "project_scope": ["alpha"], "derived_from": {"sources": refs, "counts": {"sources": len(refs)}}}), str(mid)))


@pytest.mark.parametrize("stage", ("memory", "source"))
@pytest.mark.parametrize("query", (
    "luminousmigration amberrebase copperdeploy",
    "How should luminousmigration amberrebase copperdeploy proceed when planning changes across unfamiliar modules",
    '"amberrebase amberrebase" copperdeploy',
    '"luminousmigration amberrebase"',
    'luminousmigrations amberrebases copperdeploy',
))
def test_readable_corpus_preserves_owner_order(vault, monkeypatch, stage, query):
    """Guard: exact BM25 rarity, length normalization and overlapping phrase occurrences."""
    texts = ["luminousmigration " * 8 + "amberrebase amberrebase copperdeploy " + "padding " * 130,
             "luminousmigration amberrebase amberrebase amberrebase copperdeploy"]
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for i, text in enumerate(texts + ["luminousmigration filler " + str(i) for i in range(35)] + ["filler amberrebase"]):
            labels = {"domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["alpha"]}}
            if stage == "memory":
                store.create_memory({"memory_key": f"quality.{i}", "title": "amberrebase" if text == "filler amberrebase" else "Migration record", "canonical_text": text,
                                     "value": {"text": text}, "memory_type": "procedure", "status": "active", "confidence": 1,
                                     "project_id": "alpha", **labels})
            else:
                source = store.create_source({"source_type": "note", "title": "Captured record", "content_hash": str(uuid4()), **labels})
                store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": 0, "text": text})
    args = {"query": query, "include_sources": stage == "source", "limit": 10}
    method = "search_memories_fts" if stage == "memory" else "search_source_chunks"
    original = getattr(SQLiteVNextStore, method)
    observed = {}
    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        observed.update({str(r["id"]): r["fts_score"] for r in rows})
        return rows
    monkeypatch.setattr(SQLiteVNextStore, method, capture)
    owner = call(vault, monkeypatch, None, "alice_recall", args)
    owner_scores = dict(observed)
    observed.clear()
    key = mint(vault, "project_scoped_agent")
    bounded = call(vault, monkeypatch, key, "alice_recall", args)
    assert observed == pytest.approx(owner_scores, rel=1e-12, abs=1e-15)
    field = "results" if stage == "memory" else "sources"
    assert len(owner[field]) >= 2
    assert [r["id"] for r in bounded[field]] == [r["id"] for r in owner[field]]


@pytest.mark.parametrize("tool", ("alice_recall", "alice_context_pack", "alice_what_changed"))
def test_derived_hidden_rows_do_not_change_ranked_doors(vault, monkeypatch, tool):
    """Guard: derived rows cannot contribute to ordinary population counts or phrase rarity."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        conn.execute("UPDATE memories SET memory_type='decision' WHERE id IN (?,?)", ids)
    key = mint(vault, "project_scoped_agent")
    def read():
        args = {"topic": QUERY} if tool == "alice_what_changed" else {"query": QUERY}
        return normalized(call(vault, monkeypatch, key, tool, args))
    seen = set()
    original = SQLiteVNextStore.search_memories_fts
    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        seen.update(str(r["id"]) for r in rows)
        return rows
    monkeypatch.setattr(SQLiteVNextStore, "search_memories_fts", capture)
    before = read()
    assert all(mid in json.dumps(before) for mid in ids)
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        hidden_ids = []
        for i in range(100):
            row = store.create_memory({"memory_key": f"derived.hidden.{i}", "memory_type": "decision", "title": "Input copy",
                                       "canonical_text": "luminousmigration " * 3, "value": {"text": "luminousmigration"},
                                       "status": "active", "domain": "project", "sensitivity": "public", "confidence": 1,
                                       "project_id": "alpha", "metadata_json": {"project_scope": ["alpha"]}})
            hidden_ids.append(row["id"])
        _derive(store, hidden_ids, hidden=True)
    assert read() == before
    assert not seen.intersection(str(mid) for mid in hidden_ids)


@pytest.mark.parametrize("caller", ("key", "owner", "admin_agent", "trusted_local_agent", "read_only_agent"))
def test_rank_helpers_are_temporary_and_only_for_keyed_limits(vault, monkeypatch, caller):
    """Guard: helpers belong only to temp on the product read connection and only to keyed limits."""
    visible(vault, monkeypatch, "memory")
    visible(vault, monkeypatch, "source")
    key = mint(vault, "project_scoped_agent") if caller == "key" else mint(vault, "admin_agent", None) if caller == "admin_agent" else None
    declared = {"agent_id": "reader", "permission_profile": caller} if caller in {"trusted_local_agent", "read_only_agent"} else {}
    def schema():
        with sqlite3.connect(vault) as conn:
            return conn.execute("SELECT * FROM sqlite_master ORDER BY type,name").fetchall(), conn.execute("PRAGMA user_version").fetchone()
    before = schema()
    seen = []
    original = SQLiteVNextStore.search_memories_fts
    @wraps(original)
    def capture(self, **kwargs):
        result = original(self, **kwargs)
        seen.extend(row["name"] for row in self.conn.execute("SELECT name FROM temp.sqlite_master WHERE name LIKE 'alice_%'").fetchall())
        return result
    monkeypatch.setattr(SQLiteVNextStore, "search_memories_fts", capture)
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": True, **declared})
    assert result["count"] == 2 and result["source_count"] == 2
    assert schema() == before
    helpers = {"alice_memory_terms", "alice_chunk_terms", "alice_query_text", "alice_query_terms"}
    assert helpers <= set(seen) if caller == "key" else not seen
    with sqlite3.connect(vault) as conn:
        assert conn.execute("SELECT name FROM temp.sqlite_master").fetchall() == []


def test_readable_derived_rows_leave_other_bm25_scores_unchanged(vault, monkeypatch):
    """Guard: readable derived rows are ranked but excluded from N, average length and document frequency."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for i in range(10):
            store.create_memory({"memory_key": f"ordinary.filler.{i}", "canonical_text": "Unrelated ordinary text",
                                 "value": {"text": "Unrelated ordinary text"}, "memory_type": "procedure", "status": "active",
                                 "domain": "project", "sensitivity": "public", "project_id": "alpha",
                                 "metadata_json": {"project_scope": ["alpha"]}})
    key = mint(vault, "project_scoped_agent")
    original = SQLiteVNextStore.search_memories_fts
    observed = {}
    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        observed.update({str(r["id"]): r["fts_score"] for r in rows})
        return rows
    monkeypatch.setattr(SQLiteVNextStore, "search_memories_fts", capture)
    args = {"query": QUERY, "include_sources": False, "limit": 20}
    before = call(vault, monkeypatch, key, "alice_recall", args)
    before_scores = {mid: observed[mid] for mid in ids}
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        added = store.create_memory({"memory_key": "derived.readable", "title": "Input copy", "canonical_text": "luminousmigration " * 200,
                                     "value": {"text": "luminousmigration"}, "memory_type": "procedure", "status": "active",
                                     "domain": "project", "sensitivity": "public", "confidence": 1, "project_id": "alpha",
                                     "metadata_json": {"project_scope": ["alpha"]}})
        _derive(store, [added["id"]])
    after = call(vault, monkeypatch, key, "alice_recall", args)
    assert str(added["id"]) in [r["id"] for r in after["results"]]
    assert {mid: observed[mid] for mid in ids} == before_scores
    assert [r["id"] for r in after["results"] if r["id"] in ids] == [r["id"] for r in before["results"]]


def test_rank_ignores_empty_tokenizer_phrases(vault, monkeypatch):
    """Guard: a syntactically present phrase with no native tokens produces no hits."""
    visible(vault, monkeypatch, "memory")
    key = mint(vault, "project_scoped_agent")
    statements = []
    original = SQLiteVNextStore._execute
    @wraps(original)
    def capture(self, query, params=()):
        statements.append(query)
        return original(self, query, params)
    monkeypatch.setattr(SQLiteVNextStore, "_execute", capture)
    assert call(vault, monkeypatch, key, "alice_recall", {"query": '"___"', "include_sources": True})["count"] == 0
    assert not any("rank_hits" in query for query in statements)


def test_store_refill_keeps_its_limit_after_derived_admission(vault, monkeypatch):
    """Guard: a refilled store page returns at most its requested readable limit."""
    with sqlite_user_connection(vault, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        hidden = []
        for i in range(43):
            text = "luminousmigration " * 20 if i < 3 else "luminousmigration ordinary"
            row = store.create_memory({"memory_key": f"refill.{i}", "memory_type": "procedure", "title": "Record",
                                       "canonical_text": text, "value": {"text": text}, "status": "active", "confidence": 1,
                                       "domain": "project", "sensitivity": "public", "project_id": "alpha",
                                       "metadata_json": {"project_scope": ["alpha"]}})
            if i < 3:
                hidden.append(row["id"])
        _derive(store, hidden, hidden=True)
    key = mint(vault, "project_scoped_agent")
    original = SQLiteVNextStore.search_memories_fts
    sizes = []
    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        sizes.append((len(rows), kwargs["limit"]))
        return rows
    monkeypatch.setattr(SQLiteVNextStore, "search_memories_fts", capture)
    result = call(vault, monkeypatch, key, "alice_recall", {"query": "luminousmigration", "limit": 1, "include_sources": False})
    assert result["count"] == 1
    assert sizes and all(size <= requested for size, requested in sizes)


def test_rank_helpers_allow_repeated_reads_with_an_active_cursor(vault, monkeypatch):
    """Guard: already registered functions are kept on a reused product connection."""
    visible(vault, monkeypatch, "memory")
    key = mint(vault, "project_scoped_agent")
    with sqlite_user_connection(vault, USER) as conn:
        @contextmanager
        def shared_connection(_path, _user):
            yield conn
        monkeypatch.setattr("alicebot_api.mcp.runtime.sqlite_user_connection", shared_connection)
        args = {"query": QUERY, "include_sources": True}
        before = call(vault, monkeypatch, key, "alice_recall", args)
        cursor = conn.execute("SELECT id FROM memories ORDER BY id")
        assert cursor.fetchone() is not None
        try:
            assert call(vault, monkeypatch, key, "alice_recall", args) == before
        finally:
            cursor.close()


@pytest.mark.parametrize("shape", ("repeated", "phrases", "long_phrase"))
def test_long_supported_queries_keep_native_bm25(vault, monkeypatch, shape):
    """Guard: repeated contributions and large phrase lists avoid SQL compound limits."""
    if shape == "repeated":
        query = "luminousmigration " * 600
        text = "luminousmigration luminousmigration"
    elif shape == "long_phrase":
        text = " ".join(f"t{i:02}" for i in range(65))
        query = '"' + text + '"'
    else:
        phrases = [f"t{i:02} t{j:02}" for i in range(25) for j in range(25)][:600]
        query = " ".join('"' + phrase + '"' for phrase in phrases)
        text = " ".join(phrases)
    with sqlite_user_connection(vault, USER) as conn:
        SQLiteVNextStore(conn, USER).create_memory({"memory_key": "long.query", "canonical_text": text,
            "value": {"text": text}, "memory_type": "procedure", "status": "active", "domain": "project",
            "sensitivity": "public", "project_id": "alpha", "metadata_json": {"project_scope": ["alpha"]}})
    scores = {}
    original = SQLiteVNextStore.search_memories_fts
    @wraps(original)
    def capture(self, **kwargs):
        rows = original(self, **kwargs)
        scores.update({str(r["id"]): r["fts_score"] for r in rows})
        return rows
    monkeypatch.setattr(SQLiteVNextStore, "search_memories_fts", capture)
    args = {"query": query, "limit": 1, "include_sources": False}
    owner = call(vault, monkeypatch, None, "alice_recall", args)
    expected = dict(scores)
    key = mint(vault, "project_scoped_agent")
    scores.clear()
    bounded = call(vault, monkeypatch, key, "alice_recall", args)
    assert owner["count"] == bounded["count"] == 1
    assert scores == pytest.approx(expected, rel=1e-12, abs=1e-15)


def test_empty_strict_pass_skips_corpus_scoring(vault, monkeypatch):
    """Guard: an empty native MATCH pass reaches fallback without computing corpus scores."""
    from alicebot_api.vnext_stores.sqlite import readable_rank

    ids = visible(vault, monkeypatch, "memory")
    key = mint(vault, "project_scoped_agent")
    original = readable_rank.rank_sql
    scored = []
    @wraps(original)
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["memory"] and result is not None:
            scored.append(kwargs["expression"])
        return result
    monkeypatch.setattr(readable_rank, "rank_sql", capture)
    answer = call(vault, monkeypatch, key, "alice_recall", {
        "query": QUERY + " unfamiliar", "include_sources": False, "limit": 5,
    })
    assert {row["id"] for row in answer["results"]} == set(ids)
    assert scored and all(" OR " in expression for expression in scored)
