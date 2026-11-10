"""Hidden rows must not affect ranked reads made through real keyed tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    """Guard: the fenced memory and passage stages request row-only full-text ranks."""
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


@pytest.mark.parametrize("profile", (None, "admin_agent"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_unrestricted_recall_keeps_bm25_order(vault, monkeypatch, profile, stage):
    """Guard: row-only rank is selected only for an identity with read limits."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, profile, project=None) if profile else None
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, "project")
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": stage == "source", "projects": ["alpha"], "limit": 5})
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
def test_fenced_rank_weights_each_searchable_column(vault, monkeypatch, column, repeats, competitor):
    """Guard: title/body weights are 2:1, then summary .5, key 1 and fact keys .25."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        for mid in ids:
            conn.execute("UPDATE memories SET title='Neutral', canonical_text='Neutral', summary=NULL, fact_keys=NULL WHERE id=?", (mid,))
        # The column names are the fixed parameter table, never user input.
        conn.execute(f"UPDATE memories SET {column}=? WHERE id=?", ("luminousmigration " * repeats, ids[0]))
        for name, text in competitor.items():
            conn.execute(f"UPDATE memories SET {name}=? WHERE id=?", (text, ids[1]))
    key = mint(vault, "project_scoped_agent")
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": False})
    assert [row["id"] for row in result["results"]] == ids


@pytest.mark.parametrize("profile", (None, "admin_agent"))
@pytest.mark.parametrize("stage", ("memory", "source"))
def test_unrestricted_context_pack_keeps_bm25_order(vault, monkeypatch, profile, stage):
    """Guard: a pack's request scope does not activate the limited-caller score."""
    ids = visible(vault, monkeypatch, stage)
    key = mint(vault, profile, project=None) if profile else None
    with sqlite_user_connection(vault, USER) as conn:
        add_hidden(SQLiteVNextStore(conn, USER), stage, "project")
    result = call(vault, monkeypatch, key, "alice_context_pack", {"query": QUERY, "include_sources": True, "projects": ["alpha"]})
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
    """Guard: subtract the original column length from its highlighted length."""
    ids = visible(vault, monkeypatch, "memory")
    with sqlite_user_connection(vault, USER) as conn:
        conn.execute("UPDATE memories SET canonical_text=canonical_text || ? WHERE id=?", (" irrelevant" * 100, ids[1]))
    key = mint(vault, "project_scoped_agent")
    result = call(vault, monkeypatch, key, "alice_recall", {"query": QUERY, "include_sources": False})
    assert [row["id"] for row in result["results"]] == ids


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
