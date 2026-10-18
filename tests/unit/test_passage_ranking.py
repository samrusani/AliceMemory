"""Passage ranking for ``alice_recall`` (``ALICE_SEARCH_QUALITY=passage``).

Document mode stays the default. Passage mode ranks chunks across documents,
keeps at most ``PASSAGES_PER_SOURCE`` of each, and drops a row the caller may
not read before that row can take a slot. The pack and the brief keep calling
``SourceRanking.document()``.

Each test names the edit that must make it fail.
"""

from __future__ import annotations

import ast
import io
import json
import random
import string
from collections import Counter
from pathlib import Path

import pytest

from alicebot_api.mcp.retrieval import recall_source_ranking
from alicebot_api.mcp.types import MCPToolError
from alicebot_api.source_ranking import PASSAGES_PER_SOURCE, SEARCH_QUALITY_ENV, SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_retrieval import (
    SOURCE_PASSAGE_CANDIDATE_FLOOR,
    SOURCE_PASSAGE_SCAN_CAPPED,
    SOURCE_PASSAGE_SCAN_COMPLETE,
    SOURCE_PASSAGE_SCAN_MAX_ROWS,
    VNextRetrievalService,
    VNextRetrievalValidationError,
)

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
SENSITIVITY = ["public", "internal", "private", "unknown"]


def _token() -> str:
    """A throwaway token built at run time. Not a live secret."""

    rnd = random.Random("passage-ranking")
    return "gh" + "p_" + "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(36))


def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_AGENT_API_KEY",
        SEARCH_QUALITY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")


def _context(tmp_path: Path):
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID), database


def _store(database: Path):
    return sqlite_user_connection(database, USER_ID)


def _source(store: SQLiteVNextStore, name: str, **extra: object) -> str:
    row = store.create_source(
        {
            "source_type": "note",
            "title": extra.pop("title", name),
            "content_hash": f"hash-{name}",
            "captured_at": extra.pop("captured_at", "2026-06-01T00:00:00Z"),
            "domain": extra.pop("domain", "project"),
            "sensitivity": extra.pop("sensitivity", "public"),
            "metadata_json": extra.pop("metadata_json", {}),
            **extra,
        }
    )
    return str(row["id"])


def _chunk(store: SQLiteVNextStore, source_id: str, index: int, text: str) -> dict[str, object]:
    return store.create_source_chunk(
        {"source_id": source_id, "chunk_index": index, "text": text, "metadata_json": {}}
    )


def _service(store: SQLiteVNextStore) -> VNextRetrievalService:
    return VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)


def _passages(store: SQLiteVNextStore, query: str, **overrides: object):
    arguments: dict[str, object] = {
        "query": query,
        "domains": [],
        "sensitivity_allowed": SENSITIVITY,
        "limit": 8,
        "scope": None,
        "ranking": SourceRanking.passage(),
        "winning_memories": [],
    }
    arguments.update(overrides)
    return _service(store).search_source_excerpts(**arguments)  # type: ignore[arg-type]


def _blob(payload: object) -> str:
    return json.dumps(payload, default=str)


def _door_call(context, name: str, arguments: dict[str, object]) -> dict[str, object]:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _stdio_call(context, name: str, arguments: dict[str, object]) -> dict[str, object]:
    from alicebot_api.mcp_server import MCPServer

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    ]
    stdin = b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    stdout = io.BytesIO()
    assert MCPServer(context=context, input_stream=io.BytesIO(stdin), output_stream=stdout).run() == 0
    responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    response = next(item for item in responses if item.get("id") == 2)
    result = response["result"]
    assert result.get("isError") is not True, result
    assert len(result["content"]) == 1
    return json.loads(result["content"][0]["text"])


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


def test_from_environment_maps_the_three_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset and ``off`` are document mode. ``passage`` uses the named cap. ``on`` is refused later.

    Mutation: treat unset as passage, or map ``passage`` to document mode.
    """

    monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
    assert SourceRanking.from_environment() == SourceRanking.document()
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "off"}) == SourceRanking.document()
    passage = SourceRanking.from_environment({SEARCH_QUALITY_ENV: "passage"})
    assert passage == SourceRanking.passage()
    assert passage.passages_per_source == PASSAGES_PER_SOURCE == 3
    turned_on = SourceRanking.from_environment({SEARCH_QUALITY_ENV: "on"})
    assert turned_on.lexical == "improved"
    with pytest.raises(ValueError):
        SourceRanking.from_environment({SEARCH_QUALITY_ENV: "sideways"})


def test_on_is_refused_before_the_store_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ALICE_SEARCH_QUALITY=on`` does not run. Mutation: accept ``lexical='improved'``."""

    class _Store:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(name)

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_RERANKER_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_RERANKER_MODEL", raising=False)
    service = VNextRetrievalService(_Store(), embedding_provider=None, reranker_provider=None)  # type: ignore[arg-type]
    ranking = SourceRanking.from_environment({SEARCH_QUALITY_ENV: "on"})
    with pytest.raises(VNextRetrievalValidationError):
        service.search_source_excerpts(
            query="kettle",
            domains=[],
            sensitivity_allowed=SENSITIVITY,
            limit=8,
            scope=None,
            ranking=ranking,
        )


def test_a_limited_postgres_key_stays_on_document_mode_when_passage_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge reads the store kind and the identity. Mutation: return ``from_environment()`` first.

    A later change can make passage the SQLite default by editing ``recall_source_ranking``
    only. This test is the line that must stay: a limited key on PostgreSQL is document mode.
    """

    class PostgresVNextStore:
        pass

    class SQLiteVNextStore:
        pass

    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    limited = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_key")
    admin = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_key")
    assert recall_source_ranking(PostgresVNextStore(), limited) == SourceRanking.document()
    assert recall_source_ranking(PostgresVNextStore(), None) == SourceRanking.passage()
    assert recall_source_ranking(PostgresVNextStore(), admin) == SourceRanking.passage()
    assert recall_source_ranking(SQLiteVNextStore(), limited) == SourceRanking.passage()
    monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
    assert recall_source_ranking(SQLiteVNextStore(), None) == SourceRanking.document()


def test_recall_with_on_set_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool refuses ``on`` instead of answering in document mode. Mutation: catch the error and continue."""

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "on")
    context, _database = _context(tmp_path)
    with pytest.raises(MCPToolError):
        _door_call(context, "alice_recall", {"query": "kettle"})


# --------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------


def test_passage_mode_returns_several_chunks_and_document_mode_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One document, four distinct chunks. Passage mode keeps three and repeats the id.

    Mutation: skip the per-source cap, or collapse passage mode back to one chunk.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "lamp", title="Inventory")
        for index, place in enumerate(("north", "south", "east", "west")):
            _chunk(store, source_id, index, f"brass lantern on the {place} pier")
        passages, stage = _passages(store, "brass lantern")
        document, _document_stage = _passages(store, "brass lantern", ranking=SourceRanking.document())

    assert stage["passages_per_source"] == 3
    assert stage["credential_dropped"] == 0
    assert stage["chunk_scan"] == "complete"
    assert [row["id"] for row in passages] == [source_id, source_id, source_id]
    assert len({row["chunk_index"] for row in passages}) == 3
    assert all(isinstance(row["chunk_index"], int) for row in passages)
    assert "chunk_index" not in document[0]
    assert [row["id"] for row in document] == [source_id]
    assert all(row["excerpt_kind"] == "imported_source_material" for row in passages)


def test_a_planted_middle_chunk_is_the_excerpt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The excerpt is that chunk's own text, not the document's first chunk.

    Mutation: always window chunk 0.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "glass", title="Inventory")
        _chunk(store, source_id, 0, "introduction to the station log")
        _chunk(store, source_id, 1, "the spare lamp glass is wrapped in linen")
        passages, _stage = _passages(store, "spare lamp glass")
    assert len(passages) == 1
    assert passages[0]["chunk_index"] == 1
    assert "spare lamp glass" in passages[0]["excerpt"]
    assert "introduction" not in passages[0]["excerpt"]


def test_exact_duplicates_keep_the_source_captured_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same text, two sources. The later capture wins, and an equal time breaks by id.

    Mutation: keep the first row in rank order, or hash the raw text so case survives.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    phrase = "brass lantern hangs above the chart table"
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        # Equal scores are reordered by text length, so the older source's chunk is
        # padded until it ranks first. Collapse must still keep the source captured last.
        newer = _source(store, "newer", title="Inventory", captured_at="2026-08-01T00:00:00Z")
        _chunk(store, newer, 0, "  Brass   lantern hangs\nabove the chart table  ")
        older = _source(store, "older", title="Inventory", captured_at="2026-01-01T00:00:00Z")
        _chunk(store, older, 0, phrase.upper() + (" " * 80))
        passages, _stage = _passages(store, "brass lantern chart")
    assert [row["id"] for row in passages] == [newer]


def test_a_hidden_duplicate_cannot_push_out_the_visible_passage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confidential copy is dropped before the duplicate collapse. Mutation: collapse first."""

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    phrase = "brass lantern hangs above the chart table"
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        visible = _source(store, "visible", title="Inventory", captured_at="2026-01-01T00:00:00Z")
        hidden = _source(
            store,
            "hidden",
            title="Inventory",
            captured_at="2026-08-01T00:00:00Z",
            sensitivity="confidential",
        )
        _chunk(store, visible, 0, phrase)
        _chunk(store, hidden, 0, phrase)
        passages, _stage = _passages(store, "brass lantern chart", sensitivity_allowed=["public"])
    assert [row["id"] for row in passages] == [visible]
    assert hidden not in _blob(passages)


def test_a_hidden_row_the_chunk_search_returned_is_dropped_before_the_duplicate_collapse() -> None:
    """The store hands back a later confidential copy of the same text. It must not win the hash.

    Mutation: collapse duplicates before ``_resolve_sources`` / ``_allowed``.
    """

    phrase = "brass lantern hangs above the chart table"

    class _Store:
        fts_stage_source = "sqlite_fts"

        def search_source_chunks(self, **_kwargs: object) -> list[dict[str, object]]:
            return [
                {
                    "id": "chunk-hidden",
                    "source_id": "hidden",
                    "chunk_index": 0,
                    "text": phrase,
                    "fts_score": 10.0,
                },
                {
                    "id": "chunk-visible",
                    "source_id": "visible",
                    "chunk_index": 0,
                    "text": phrase,
                    "fts_score": 1.0,
                },
            ]

        def get_sources_by_ids(self, source_ids: object) -> list[dict[str, object]]:
            wanted = set(source_ids)  # type: ignore[arg-type]
            rows = []
            if "hidden" in wanted:
                rows.append(
                    {
                        "id": "hidden",
                        "title": "Inventory",
                        "domain": "project",
                        "sensitivity": "confidential",
                        "captured_at": "2026-08-01T00:00:00Z",
                        "metadata_json": {},
                    }
                )
            if "visible" in wanted:
                rows.append(
                    {
                        "id": "visible",
                        "title": "Inventory",
                        "domain": "project",
                        "sensitivity": "public",
                        "captured_at": "2026-01-01T00:00:00Z",
                        "metadata_json": {},
                    }
                )
            return rows

        def search_sources(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

    service = VNextRetrievalService(_Store(), embedding_provider=None, reranker_provider=None)  # type: ignore[arg-type]
    passages, _stage = service.search_source_excerpts(
        query="brass lantern chart",
        domains=[],
        sensitivity_allowed=["public"],
        limit=8,
        scope=None,
        ranking=SourceRanking.passage(),
    )
    assert [row["id"] for row in passages] == ["visible"]


def test_a_credential_passage_is_dropped_and_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flagged chunk does not come back and does not spend a cap slot.

    Mutation: skip ``credential_verdict``, or count the drop and still return the text.
    """

    _quiet(monkeypatch)
    secret = _token()
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "mixed", title="Inventory")
        _chunk(store, source_id, 0, f"brass lantern token {secret}")
        _chunk(store, source_id, 1, "brass lantern on the hook behind the clock")
        passages, stage = _passages(store, "brass lantern")
    assert stage["credential_dropped"] == 1
    assert secret not in _blob(passages)
    assert any("hook behind the clock" in row["excerpt"] for row in passages)


def test_an_unused_reserve_goes_back_to_the_next_passage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eight passages, no document-list filler. The two reserve slots are passages too.

    Mutation: stop after ``limit - limit // 4`` passages.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        ids = []
        for index in range(8):
            source_id = _source(store, f"note-{index}", title="Inventory")
            ids.append(source_id)
            _chunk(store, source_id, 0, f"brass lantern number {index} on its own hook")
        passages, _stage = _passages(store, "brass lantern")
    assert len(passages) == 8
    assert {row["id"] for row in passages} == set(ids)


def test_a_stale_quote_labels_only_the_passage_that_contains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correction labels are per entry. Mutation: label every entry of the source."""

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "quoted", title="Inventory")
        _chunk(store, source_id, 0, "the north pier lamp is green")
        _chunk(store, source_id, 1, "An older sentence about the north pier lamp")
        memory_id = str(
            store.create_memory(
                {
                    "memory_key": "lamp.current",
                    "memory_type": "decision",
                    "title": "lamp",
                    "canonical_text": "The lamp glass was replaced.",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {},
                    "value": {"text": "replaced"},
                }
            )["id"]
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory_id,
                "source_id": source_id,
                "evidence_role": "quoted_from",
                "quote": "An older sentence",
            }
        )
        passages, _stage = _passages(store, "north pier lamp")
    by_index = {row["chunk_index"]: row for row in passages}
    assert "derived_memory_corrected" not in by_index[0]
    assert by_index[1]["derived_memory_corrected"] is True
    assert by_index[1]["current_memory_id"] == memory_id


def test_the_memory_visible_fence_withholds_the_id_on_that_entry_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who may not read the memory still sees the stale flag, and not the id.

    Mutation: skip ``memory_visible`` inside ``_current_memory_id``.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source_id = _source(store, "quoted", title="Inventory")
        _chunk(store, source_id, 0, "the north pier lamp is green")
        _chunk(store, source_id, 1, "An older sentence about the north pier lamp")
        memory_id = str(
            store.create_memory(
                {
                    "memory_key": "lamp.hidden",
                    "memory_type": "decision",
                    "title": "lamp",
                    "canonical_text": "The lamp glass was replaced.",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "confidential",
                    "metadata_json": {},
                    "value": {"text": "replaced"},
                }
            )["id"]
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory_id,
                "source_id": source_id,
                "evidence_role": "quoted_from",
                "quote": "An older sentence",
            }
        )
        hidden, _stage = _passages(store, "north pier lamp", sensitivity_allowed=["public"])
        shown, _stage = _passages(store, "north pier lamp", sensitivity_allowed=["public", "confidential"])
    hidden_quote = next(row for row in hidden if row["chunk_index"] == 1)
    shown_quote = next(row for row in shown if row["chunk_index"] == 1)
    assert hidden_quote["derived_memory_corrected"] is True
    assert "current_memory_id" not in hidden_quote
    assert memory_id not in _blob(hidden)
    assert shown_quote["current_memory_id"] == memory_id


def test_the_scan_stops_at_the_ceiling_and_does_not_raise() -> None:
    """A full prefix at 2048 rows, with nothing resolved, records ``scan capped``.

    Mutation: let ``VNextRetrievalCompletenessError`` propagate, or keep deepening past the ceiling.
    """

    class _Store:
        fts_stage_source = "sqlite_fts"

        def __init__(self) -> None:
            self.limits: list[int] = []

        def search_source_chunks(self, *, limit: int, **_kwargs: object) -> list[dict[str, object]]:
            self.limits.append(limit)
            return [
                {
                    "id": f"chunk-{index}",
                    "source_id": f"missing-{index}",
                    "chunk_index": index,
                    "text": "brass lantern on the north pier",
                    "fts_score": 1.0,
                }
                for index in range(limit)
            ]

        def get_sources_by_ids(self, _ids: object) -> list[dict[str, object]]:
            return []

        def search_sources(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

    store = _Store()
    service = VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)  # type: ignore[arg-type]
    passages, stage = service.search_source_excerpts(
        query="brass lantern",
        domains=[],
        sensitivity_allowed=SENSITIVITY,
        limit=8,
        scope=None,
        ranking=SourceRanking.passage(),
    )
    assert passages == []
    assert stage["chunk_scan"] == SOURCE_PASSAGE_SCAN_CAPPED
    assert store.limits[0] == max(8 * 4, SOURCE_PASSAGE_CANDIDATE_FLOOR)
    assert store.limits[-1] == SOURCE_PASSAGE_SCAN_MAX_ROWS
    assert SOURCE_PASSAGE_SCAN_MAX_ROWS == 2048


def test_the_scan_deepens_past_one_documents_cap() -> None:
    """Eighty chunks of one document do not finish a request for eight passages.

    Mutation: stop the scan once the raw visible prefix fills, before the per-source cap.
    """

    class _Store:
        fts_stage_source = "sqlite_fts"

        def __init__(self) -> None:
            self.limits: list[int] = []

        def search_source_chunks(self, *, limit: int, **_kwargs: object) -> list[dict[str, object]]:
            self.limits.append(limit)
            rows: list[dict[str, object]] = []
            for index in range(80):
                rows.append(
                    {
                        "id": f"a-{index}",
                        "source_id": "doc-a",
                        "chunk_index": index,
                        "text": f"brass lantern note {index}",
                        "fts_score": 100 - index,
                    }
                )
            for index in range(6):
                rows.append(
                    {
                        "id": f"other-{index}",
                        "source_id": f"doc-{index}",
                        "chunk_index": 0,
                        "text": f"brass lantern from document {index}",
                        "fts_score": 10 - index,
                    }
                )
            return rows[:limit]

        def get_sources_by_ids(self, ids: object) -> list[dict[str, object]]:
            return [
                {"id": source_id, "title": source_id, "captured_at": "2026-01-01T00:00:00Z", "domain": "project", "sensitivity": "public"}
                for source_id in ids
            ]

        def search_sources(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

    store = _Store()
    service = VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)  # type: ignore[arg-type]
    passages, stage = service.search_source_excerpts(
        query="brass lantern",
        domains=[],
        sensitivity_allowed=SENSITIVITY,
        limit=8,
        scope=None,
        ranking=SourceRanking.passage(),
    )
    assert len(passages) == 8
    assert len({row.get("source_id", row.get("id")) for row in passages}) > 1
    assert stage["chunk_scan"] == SOURCE_PASSAGE_SCAN_COMPLETE
    assert store.limits[-1] < SOURCE_PASSAGE_SCAN_MAX_ROWS


def test_hidden_rows_under_the_ceiling_do_not_change_the_visible_passages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same answer with and without confidential rows that match the query.

    Mutation: resolve parents after the cap, so a hidden row spends a slot.
    """

    _quiet(monkeypatch)
    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        visible = _source(store, "visible", title="Inventory")
        _chunk(store, visible, 0, "brass lantern on the public hook")
        before, _stage = _passages(store, "brass lantern", sensitivity_allowed=["public"])
        hidden = _source(store, "hidden", title="Inventory", sensitivity="confidential")
        _chunk(store, hidden, 0, "brass lantern on the confidential hook")
        deleted = _source(store, "deleted", title="Inventory")
        _chunk(store, deleted, 0, "brass lantern on the deleted hook")
        conn.execute(
            "UPDATE sources SET deleted_at = ? WHERE id = ?",
            ("2026-07-01T00:00:00Z", deleted),
        )
        ensure_sqlite_user(conn, OTHER_USER_ID, "other@alice")
    with sqlite_user_connection(database, OTHER_USER_ID) as conn:
        other = SQLiteVNextStore(conn, OTHER_USER_ID)
        foreign = _source(other, "foreign", title="Inventory")
        _chunk(other, foreign, 0, "brass lantern on the foreign hook")
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        after, _stage = _passages(store, "brass lantern", sensitivity_allowed=["public"])
    assert before == after
    blob = _blob(after)
    assert "confidential hook" not in blob
    assert "deleted hook" not in blob
    assert "foreign hook" not in blob
    assert hidden not in blob and deleted not in blob and foreign not in blob


def test_scope_domains_and_sensitivity_drop_the_parent_before_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_sources`` and ``_allowed`` run before the cap.

    Mutation: filter domains only in the final compact step.
    """

    _quiet(monkeypatch)
    from alicebot_api.vnext_retrieval import _ResolvedRetrievalScope

    _context_obj, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        project = _source(
            store,
            "project-note",
            title="Inventory",
            metadata_json={"project_scope": ["acme"]},
        )
        _chunk(store, project, 0, "brass lantern in the acme cupboard")
        health = _source(store, "health-note", title="Inventory", domain="health")
        _chunk(store, health, 0, "brass lantern in the health cupboard")
        personal = _source(
            store,
            "personal-note",
            title="Inventory",
            metadata_json={"project_scope": ["personal-notes"]},
        )
        _chunk(store, personal, 0, "brass lantern in the personal cupboard")
        scoped, _stage = _passages(
            store,
            "brass lantern",
            domains=["project"],
            scope=_ResolvedRetrievalScope(
                projects=frozenset({"acme"}),
                people=frozenset(),
                window_start=None,
                window_end=None,
                exclude_global_domains=frozenset(),
            ),
        )
    blob = _blob(scoped)
    assert "acme cupboard" in blob
    assert "health cupboard" not in blob
    assert "personal cupboard" not in blob
    assert health not in blob and personal not in blob


# --------------------------------------------------------------------------
# Both doors
# --------------------------------------------------------------------------


def _seed_doors(database: Path) -> dict[str, str]:
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        visible = _source(store, "visible", title="Inventory")
        _chunk(store, visible, 0, "brass lantern on the public hook")
        _chunk(store, visible, 1, "brass lantern on the second public hook")
        hidden = _source(store, "hidden", title="Inventory", sensitivity="confidential")
        _chunk(store, hidden, 0, "brass lantern on the confidential hook")
        return {"visible": visible, "hidden": hidden}


@pytest.mark.parametrize("door", ("call", "stdio"))
def test_both_recall_doors_hide_the_same_rows_and_repeat_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    """The in-process tool and the stdio server both run the passage stage.

    Mutation: read the switch in only one of the two.
    """

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    context, database = _context(tmp_path)
    ids = _seed_doors(database)
    arguments = {"query": "brass lantern", "sensitivity_allowed": ["public"]}
    payload = _door_call(context, "alice_recall", arguments) if door == "call" else _stdio_call(context, "alice_recall", arguments)
    sources = payload["sources"]
    assert payload["source_count"] == len(sources) == 2
    assert [row["id"] for row in sources] == [ids["visible"], ids["visible"]]
    assert {row["chunk_index"] for row in sources} == {0, 1}
    blob = _blob(payload)
    assert "confidential hook" not in blob
    assert ids["hidden"] not in blob


def test_the_two_doors_agree_and_the_switch_off_has_no_chunk_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset switch: one entry, no ``chunk_index``. Passage: the doors match.

    Mutation: add ``chunk_index`` in document mode.
    """

    _quiet(monkeypatch)
    context, database = _context(tmp_path)
    _seed_doors(database)
    arguments = {"query": "brass lantern", "sensitivity_allowed": ["public"]}
    off = _door_call(context, "alice_recall", arguments)
    assert off["source_count"] == 1
    assert "chunk_index" not in off["sources"][0]
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    via_call = _door_call(context, "alice_recall", arguments)
    via_stdio = _stdio_call(context, "alice_recall", arguments)
    assert via_call["sources"] == via_stdio["sources"]
    assert via_call["source_count"] == 2


def test_the_hop_follows_the_first_passage_when_no_memory_names_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No memory hit names a source, so the hop uses the first source entry.

    Document mode's first entry is the title match. Passage mode's first entry
    is the best chunk, which is a different document. ``results`` follow that
    entry. Mutation: always hop from the title-list winner.
    """

    _quiet(monkeypatch)
    context, database = _context(tmp_path)
    with _store(database) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        titled = _source(store, "titled", title="kettle shelf notes", captured_at="2020-01-01T00:00:00Z")
        _chunk(store, titled, 0, "room inventory list")
        chunked = _source(store, "chunked", title="Inventory", captured_at="2026-08-01T00:00:00Z")
        _chunk(store, chunked, 0, "the kettle is stored on the second shelf")
        memory_titled = str(
            store.create_memory(
                {
                    "memory_key": "tag.titled",
                    "memory_type": "decision",
                    "title": "tag",
                    "canonical_text": "alpha tag only",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {},
                    "value": {"text": "alpha"},
                }
            )["id"]
        )
        memory_chunked = str(
            store.create_memory(
                {
                    "memory_key": "tag.chunked",
                    "memory_type": "decision",
                    "title": "tag",
                    "canonical_text": "beta tag only",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {},
                    "value": {"text": "beta"},
                }
            )["id"]
        )
        store.create_provenance_link(
            {"target_type": "memory", "target_id": memory_titled, "source_id": titled, "evidence_role": "supports"}
        )
        store.create_provenance_link(
            {"target_type": "memory", "target_id": memory_chunked, "source_id": chunked, "evidence_role": "supports"}
        )
    off = _door_call(context, "alice_recall", {"query": "kettle shelf"})
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    passage = _door_call(context, "alice_recall", {"query": "kettle shelf"})
    off_ids = [row["id"] for row in off["results"]]
    passage_ids = [row["id"] for row in passage["results"]]
    assert off["sources"][0]["id"] == titled
    assert passage["sources"][0]["id"] == chunked
    assert memory_titled in off_ids and memory_chunked not in off_ids
    assert memory_chunked in passage_ids and memory_titled not in passage_ids


# --------------------------------------------------------------------------
# list_source_chunks has no fence of its own
# --------------------------------------------------------------------------


_LIST_SOURCE_CHUNKS = {
    ("apps/api/src/alicebot_api/mcp/evidence_artifacts.py", "_authorize_memory_audit_provenance"): (
        2,
        "explain checks a cited chunk belongs to a source the fence already judged",
    ),
    ("apps/api/src/alicebot_api/mcp/review.py", "_validated_review_provenance"): (
        2,
        "review checks a chunk id belongs to a source the fence already admitted",
    ),
    ("apps/api/src/alicebot_api/routers/_vnext_shared.py", "_vnext_source_chunks"): (
        1,
        "HTTP chunk list for a source the route already fenced",
    ),
    ("apps/api/src/alicebot_api/session_briefing.py", "_source_query_hint"): (
        1,
        "the brief uses the title, and reads a chunk only when the title is empty",
    ),
    ("apps/api/src/alicebot_api/vault_demo.py", "receipt_for_imported_source"): (
        1,
        "the receipt for an import the owner just wrote",
    ),
    ("apps/api/src/alicebot_api/vault_sleep.py", "_first_chunk_text"): (
        1,
        "sleep proposals read the first chunk of a source the owner imported",
    ),
    ("apps/api/src/alicebot_api/vnext_coverage_query.py", "text_for"): (
        1,
        "coverage signatures for the pack, not a recall result",
    ),
    ("apps/api/src/alicebot_api/vnext_retrieval.py", "_best_chunk_without_a_winner"): (
        1,
        "document-mode excerpt when the chunk stage ranked no winner; passage entries do not use it",
    ),
    ("apps/api/src/alicebot_api/vnext_retrieval.py", "compile_context_pack"): (
        1,
        "the pack passes the listing into the coverage helper and stays on document mode",
    ),
    ("eval/longmemeval/adapter.py", "_source_chunks_for"): (
        1,
        "the benchmark harness reads store chunks; no MCP tool does",
    ),
}


class _ChunkCalls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: list[str] = []

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        hit = name == "list_source_chunks"
        if name == "getattr":
            hit = any(isinstance(arg, ast.Constant) and arg.value == "list_source_chunks" for arg in node.args)
        if hit:
            self.hits.append(self.stack[-1] if self.stack else "<module>")
        self.generic_visit(node)


def test_list_source_chunks_has_no_new_caller() -> None:
    """Every call site is named, with why it may list chunks. Passage mode adds none.

    Mutation: call ``list_source_chunks`` from ``_passage_excerpts`` or ``_passage_entry``.
    """

    root = Path(__file__).resolve().parents[2]
    found: dict[tuple[str, str], int] = {}
    for base in ("apps/api/src", "eval", "scripts"):
        for path in sorted((root / base).rglob("*.py")):
            if path.name.startswith("test_"):
                continue
            relative = path.relative_to(root).as_posix()
            visitor = _ChunkCalls()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
            counts = Counter(visitor.hits)
            for function, count in counts.items():
                found[(relative, function)] = count
    assert found.keys() == _LIST_SOURCE_CHUNKS.keys()
    for key, (count, reason) in _LIST_SOURCE_CHUNKS.items():
        assert found[key] == count, key
        assert reason
