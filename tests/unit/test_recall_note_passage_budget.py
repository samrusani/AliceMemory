"""The recall edge's note passage budget: passage and vectors mode default to
ten note passages when the caller passes no limit; memory results stay at
eight; an explicit limit is honoured in both stages.

Guards, each killed by a mutation case in the manifest:
- the named constant ``_RECALL_DEFAULT_NOTE_PASSAGE_BUDGET`` (``mcp/types.py``)
  is read at the call site of the sources stage; setting it back to 8
  shrinks the default sources stage;
- ``caller_set_limit`` (``mcp/retrieval.py``) separates an unset limit from an
  explicit one; ignoring it returns 10 note passages to a caller who asked
  for 5;
- document mode and the minimal depth keep the caller's (capped) limit: the
  budget applies only in passage and vectors mode at a non-minimal depth;
- the source fence (``scope=recall_scope`` on the same call) keeps a source
  the caller may not read out of the ten.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

USER_ID = "00000000-0000-0000-0000-000000000001"

FILE_ONE = "# Lantern plan\n\n" + "\n\n".join(
    f"Paragraph {index} of the lantern plan records the tok{index:02d}lantern detail for the archive."
    for index in range(1, 16)
) + "\n"
FILE_TWO = "# Brass plan\n\n" + "\n\n".join(
    f"Paragraph {index} of the brass plan records the tok{index:02d}brass detail for the workshop."
    for index in range(1, 6)
) + "\n"

ACME_NOTE = "# Acme sprint\n\nThe deployment cadence for acme is weekly, every Thursday afternoon.\n"
OUTSIDE_NOTE = "# Other project\n\nThe cadence for the other project is daily, every morning.\n"


def _context(tmp_path: Path, monkeypatch):
    from alicebot_api.mcp.registry import call_mcp_tool  # noqa: F401
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    monkeypatch.delenv("ALICE_SEARCH_QUALITY", raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _import_passages(tmp_path: Path) -> Path:
    """Fifteen one-paragraph files: one passage each, so the sources stage's
    cap is what the counts show."""
    folder = tmp_path / "notes"
    folder.mkdir()
    for index in range(1, 15):
        (folder / f"lantern{index:02d}.md").write_text(
            f"# Lantern {index}\n\nParagraph {index} of the lantern plan records the tok{index:02d}lantern detail for the archive.\n",
            encoding="utf-8",
        )
    (folder / "brass.md").write_text(
        "# Brass\n\nParagraph 1 of the brass plan records the tok01brass detail for the workshop.\n",
        encoding="utf-8",
    )
    return folder


def _seed_import(context, folder: Path) -> None:
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.vnext_capture import VNextCaptureService

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        return VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)


def _recall(context, **arguments):
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name="alice_recall", arguments={"query": "lantern paragraph archive", **arguments})


def _mint(context, *, profile: str, project: str) -> str:
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        _record, raw = create_agent_key(
            SQLiteVNextStore(conn, USER_ID),
            user_id=USER_ID,
            agent_id=f"{profile}-{project}",
            permission_profile=profile,
            project_scope=project,
        )
    return raw


def _seed_memories(context, count=10) -> None:
    """Committed memories matching the recall query, so the memory stage's
    cap is what the answer's count shows."""
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_label_writes import without_insert_floor

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with without_insert_floor():
            for index in range(count):
                store.create_memory({
                    "memory_key": f"lantern{index:02d}",
                    "canonical_text": f"alpha beta lantern note {index}",
                    "title": f"lantern {index}",
                    "memory_type": "decision",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {},
                })


def test_default_passage_recall_returns_ten_note_passages(tmp_path: Path, monkeypatch) -> None:
    """Guard: the named budget constant read at the sources stage, and the
    memory results' cap of eight."""

    context = _context(tmp_path, monkeypatch)
    _seed_import(context, _import_passages(tmp_path))
    _seed_memories(context)
    answer = _recall(context)
    assert answer["source_count"] == 10, answer["source_count"]
    assert answer["count"] == 8, answer["count"]


def test_default_vectors_recall_returns_ten_note_passages(tmp_path: Path, monkeypatch) -> None:
    """The same budget in vectors mode (the mode the edge keeps on a store
    that carries vectors, downgraded to passage only at the minimal depth)."""

    context = _context(tmp_path, monkeypatch)
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "vectors")
    _seed_import(context, _import_passages(tmp_path))
    _seed_memories(context)
    answer = _recall(context)
    assert answer["source_count"] == 10, answer["source_count"]
    assert answer["count"] == 8, answer["count"]


@pytest.mark.parametrize("limit", (5, 8, 12))
def test_an_explicit_limit_is_honoured_in_both_stages(tmp_path: Path, monkeypatch, limit) -> None:
    """Guard: ``caller_set_limit`` — an explicit limit is used for both stages
    and never exceeded, even when it equals the memory default (8) or the
    note passage budget (10)."""

    context = _context(tmp_path, monkeypatch)
    _seed_import(context, _import_passages(tmp_path))
    answer = _recall(context, limit=limit)
    assert answer["source_count"] == min(limit, 15), answer["source_count"]
    assert answer["count"] <= limit, answer["count"]


def test_document_mode_keeps_the_memory_limit(tmp_path: Path, monkeypatch) -> None:
    """Guard: the budget applies only in passage and vectors mode; document
    mode keeps the caller's (default eight) limit for its one-passage-per-
    document ranking."""

    context = _context(tmp_path, monkeypatch)
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "off")
    _seed_import(context, _import_passages(tmp_path))
    answer = _recall(context)
    assert answer["source_count"] == 8, answer["source_count"]
    assert answer["count"] <= 8, answer["count"]


def test_minimal_depth_keeps_todays_cap(tmp_path: Path, monkeypatch) -> None:
    """Guard: the budget is not applied at the minimal depth; the tier cap
    (four items) holds for both stages, as it does today."""

    context = _context(tmp_path, monkeypatch)
    _seed_import(context, _import_passages(tmp_path))
    answer = _recall(context, context_depth="minimal")
    assert answer["source_count"] <= 4, answer["source_count"]
    assert answer["count"] <= 4, answer["count"]


def test_the_context_pack_ranks_by_document_as_before(tmp_path: Path, monkeypatch) -> None:
    """Guard: the pack is unchanged — one source entry per document, whatever
    the recall edge's budget does."""

    context = _context(tmp_path, monkeypatch)
    _seed_import(context, _import_passages(tmp_path))
    pack = call_pack(context, "lantern paragraph archive")
    sources = pack.get("sources") or []
    ids = {str(source.get("source_id") or source.get("id")) for source in sources}
    # One entry per document: fifteen passages exist and the pack still
    # shows one entry per document, up to its own low-depth budget.
    assert len(sources) == len(ids), (len(sources), len(ids))
    assert len(sources) == 8, len(sources)


def call_pack(context, query):
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name="alice_context_pack", arguments={"query": query})


def test_a_bound_key_stays_inside_its_fence_at_the_budget(tmp_path: Path, monkeypatch) -> None:
    """Guard: the source fence on the same call. A project-scoped key's
    default recall in passage mode returns note passages from readable
    sources only, at the budget, and a source outside the key's projects
    stays out however many passages the readable ones hold."""

    from alicebot_api.mcp.registry import call_mcp_tool

    context = _context(tmp_path, monkeypatch)
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": ACME_NOTE, "title": "Acme sprint", "domain": "project", "sensitivity": "private", "project_scope": ["acme"]},
    )
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": OUTSIDE_NOTE, "title": "Other cadence", "domain": "project", "sensitivity": "private", "project_scope": ["other"]},
    )
    key = _mint(context, profile="project_scoped_agent", project="acme")
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    monkeypatch.delenv("ALICE_SEARCH_QUALITY", raising=False)
    answer = call_mcp_tool(context, name="alice_recall", arguments={"query": "cadence deployment"})
    titles = {str(source.get("title")) for source in (answer.get("sources") or [])}
    assert "Other cadence" not in titles, titles
    assert answer["source_count"] <= 10, answer["source_count"]
    assert answer["source_count"] >= 1, answer


def test_a_key_with_scoped_vectors_gets_readable_passages_only(tmp_path: Path, monkeypatch) -> None:
    """Guard: ``scoped_vectors`` — a key whose reads are fenced, in vectors
    mode, is served from the sources it may read; the budget applies there
    too and the outside source stays out."""

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "vectors")
    context = _context(tmp_path, monkeypatch)
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": ACME_NOTE, "title": "Acme sprint", "domain": "project", "sensitivity": "private", "project_scope": ["acme"]},
    )
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": OUTSIDE_NOTE, "title": "Other cadence", "domain": "project", "sensitivity": "private", "project_scope": ["other"]},
    )
    key = _mint(context, profile="project_scoped_agent", project="acme")
    monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "vectors")
    answer = call_mcp_tool(context, name="alice_recall", arguments={"query": "cadence deployment"})
    titles = {str(source.get("title")) for source in (answer.get("sources") or [])}
    assert "Other cadence" not in titles, titles
    assert answer["source_count"] <= 10, answer["source_count"]
    assert answer["source_count"] >= 1, answer


@pytest.mark.parametrize("sensitivity", ("public", "internal", "private", "unknown"))
def test_a_hidden_source_answers_as_a_missing_one_at_every_status(tmp_path: Path, monkeypatch, sensitivity) -> None:
    """Guard: the fence, not a status-dependent answer. For every status a
    source can carry, the answer with the source present but unreadable is
    the answer with the source gone."""

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    context = _context(tmp_path, monkeypatch)
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": ACME_NOTE, "title": "Acme sprint", "domain": "project", "sensitivity": "private", "project_scope": ["acme"]},
    )
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": OUTSIDE_NOTE,
            "title": "Other cadence",
            "domain": "project",
            "sensitivity": sensitivity,
            "project_scope": ["other"],
        },
    )
    key = _mint(context, profile="project_scoped_agent", project="acme")
    monkeypatch.setenv(AGENT_API_KEY_ENV, key)

    def normalise(value):
        volatile = {"retrieval_run_id", "trace_id", "run_id", "duration_ms", "elapsed_ms", "latency_ms"}
        if isinstance(value, dict):
            return {k: normalise(child) for k, child in value.items() if k not in volatile}
        if isinstance(value, list):
            return [normalise(child) for child in value]
        return value

    hidden = normalise(call_mcp_tool(context, name="alice_recall", arguments={"query": "cadence deployment"}))

    monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    with sqlite_owner_connection(context) as conn:
        conn.execute("DELETE FROM sources WHERE title = 'Other cadence'")
        conn.commit()
    monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    missing = normalise(call_mcp_tool(context, name="alice_recall", arguments={"query": "cadence deployment"}))
    assert hidden == missing, (hidden.get("source_count"), missing.get("source_count"))


def sqlite_owner_connection(context):
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    import sqlite3

    conn = sqlite3.connect(_sqlite_path_from_url(context.database_url))
    return conn


def test_the_bench_records_the_budget_beside_the_pinned_settings() -> None:
    """Guard: ``note_passage_budget`` in the bench. A passage or vectors run
    whose calls sent the query alone records the tool's default note passage
    budget (not the pinned limit of the memory results); a run that sent a
    limit, or ran document mode, records the limit it used. A run's claim
    names the budget through the fingerprint's record."""

    import scripts.alice_bench as bench

    passage = {"arguments": [{}], "limit": 8, "context_depth": "low"}
    assert bench.note_passage_budget(passage, "passage") == 10
    assert bench.note_passage_budget(passage, "vectors") == 10
    assert bench.note_passage_budget(passage, "document") == 8
    explicit = {"arguments": [{"limit": 5}], "limit": 5, "context_depth": "low"}
    assert bench.note_passage_budget(explicit, "passage") == 5
    minimal = {"arguments": [{}], "limit": 4, "context_depth": "minimal"}
    assert bench.note_passage_budget(minimal, "passage") == 4
    assert bench.note_passage_budget(None, "passage") is None
