"""Passage ranking on PostgreSQL.

Asserts fences, the cap, duplicate collapse, a planted passage, and the capped
scan. It does not assert the order of equal-looking rows: PostgreSQL ranks with
``ts_rank`` and SQLite with bm25.
"""

from __future__ import annotations

import io
import json
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.mcp_server import MCPServer
from alicebot_api.source_ranking import SEARCH_QUALITY_ENV, SourceRanking
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_retrieval import (
    SOURCE_PASSAGE_SCAN_CAPPED,
    SOURCE_PASSAGE_SCAN_MAX_ROWS,
    VNextRetrievalService,
)
from alicebot_api.vnext_store import PostgresVNextStore


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


def _open(migrated_database_urls, user_id):
    return user_connection(migrated_database_urls["app"], user_id)


def _service(store: PostgresVNextStore) -> VNextRetrievalService:
    return VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)


def _passages(store: PostgresVNextStore, query: str, **overrides: object):
    arguments: dict[str, object] = {
        "query": query,
        "domains": [],
        "sensitivity_allowed": ["public", "internal", "private", "unknown"],
        "limit": 8,
        "scope": None,
        "ranking": SourceRanking.passage(),
    }
    arguments.update(overrides)
    return _service(store).search_source_excerpts(**arguments)  # type: ignore[arg-type]


def _stdio(context: MCPRuntimeContext, arguments: dict[str, object]) -> dict[str, object]:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "alice_recall", "arguments": arguments}},
    ]
    stdin = b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    stdout = io.BytesIO()
    assert MCPServer(context=context, input_stream=io.BytesIO(stdin), output_stream=stdout).run() == 0
    responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    result = next(item for item in responses if item.get("id") == 2)["result"]
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def test_a_planted_passage_is_found_and_the_cap_and_duplicates_hold(
    migrated_database_urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: skip the cap, or keep the earlier duplicate."""

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "passage@example.invalid", "Passage")
        store = PostgresVNextStore(conn)
        planted = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:planted",
                "captured_at": "2026-06-01T00:00:00Z",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        store.create_source_chunk(
            {"source_id": str(planted["id"]), "chunk_index": 0, "text": "introduction to the station log"}
        )
        store.create_source_chunk(
            {
                "source_id": str(planted["id"]),
                "chunk_index": 1,
                "text": "the spare lamp glass is wrapped in linen",
            }
        )
        many = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:many",
                "captured_at": "2026-06-02T00:00:00Z",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        for index, place in enumerate(("north", "south", "east", "west")):
            store.create_source_chunk(
                {
                    "source_id": str(many["id"]),
                    "chunk_index": index,
                    "text": f"copper bell on the {place} pier",
                }
            )
        older = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:older",
                "captured_at": "2026-01-01T00:00:00Z",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        newer = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:newer",
                "captured_at": "2026-08-01T00:00:00Z",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        phrase = "velvet ribbon hangs above the chart table"
        store.create_source_chunk({"source_id": str(older["id"]), "chunk_index": 0, "text": phrase.upper()})
        store.create_source_chunk(
            {"source_id": str(newer["id"]), "chunk_index": 0, "text": "  Velvet   ribbon hangs\nabove the chart table  "}
        )
        hidden = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:hidden",
                "captured_at": "2026-06-03T00:00:00Z",
                "domain": "project",
                "sensitivity": "confidential",
            }
        )
        store.create_source_chunk(
            {"source_id": str(hidden["id"]), "chunk_index": 0, "text": "copper bell on the confidential pier"}
        )

        found, _stage = _passages(store, "spare lamp glass")
        capped, _stage = _passages(store, "copper bell")
        copies, _stage = _passages(store, "velvet ribbon chart")

    assert len(found) == 1
    assert found[0]["chunk_index"] == 1
    assert "spare lamp glass" in found[0]["excerpt"]
    assert [str(row["id"]) for row in capped].count(str(many["id"])) == 3
    assert str(hidden["id"]) not in {str(row["id"]) for row in capped}
    assert "confidential" not in json.dumps(capped, default=str)
    assert [str(row["id"]) for row in copies] == [str(newer["id"])]


def test_the_scan_caps_at_2048_rows(migrated_database_urls, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full prefix at the ceiling records ``scan capped`` and does not raise.

    Mutation: raise ``VNextRetrievalCompletenessError`` from the passage stage.
    """

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "cap@example.invalid", "Cap")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:cap",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        for index in range(SOURCE_PASSAGE_SCAN_MAX_ROWS):
            store.create_source_chunk(
                {
                    "source_id": str(source["id"]),
                    "chunk_index": index,
                    "text": f"brass lantern serial {index} on the pier",
                }
            )
        passages, stage = _passages(store, "brass lantern", limit=600)
    assert stage["chunk_scan"] == SOURCE_PASSAGE_SCAN_CAPPED
    assert passages
    assert len(passages) <= 3
    assert {str(row["id"]) for row in passages} == {str(source["id"])}


def test_both_doors_hide_a_confidential_source(migrated_database_urls, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool and the stdio server agree, and a limited key stays on document mode.

    Mutation: ignore the store kind in ``recall_source_ranking``.
    """

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "doors@example.invalid", "Doors")
        store = PostgresVNextStore(conn)
        visible = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:visible",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        store.create_source_chunk(
            {"source_id": str(visible["id"]), "chunk_index": 0, "text": "brass lantern on the public hook"}
        )
        store.create_source_chunk(
            {"source_id": str(visible["id"]), "chunk_index": 1, "text": "brass lantern on the second public hook"}
        )
        hidden = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:hidden-door",
                "domain": "project",
                "sensitivity": "confidential",
            }
        )
        store.create_source_chunk(
            {"source_id": str(hidden["id"]), "chunk_index": 0, "text": "brass lantern on the confidential hook"}
        )
        _record, raw_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="reader",
            permission_profile="trusted_local_agent",
        )
    context = MCPRuntimeContext(database_url=migrated_database_urls["app"], user_id=user_id)
    arguments = {"query": "brass lantern", "sensitivity_allowed": ["public"]}
    via_call = call_mcp_tool(context, name="alice_recall", arguments=arguments)
    via_stdio = _stdio(context, arguments)
    assert via_call["sources"] == via_stdio["sources"]
    assert via_call["source_count"] == 2
    assert str(hidden["id"]) not in json.dumps(via_call, default=str)

    seen: list[object] = []
    original = VNextRetrievalService.search_source_excerpts

    def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs.get("ranking"))
        return original(self, **kwargs)

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", wrapped)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw_key)
    call_mcp_tool(context, name="alice_recall", arguments=arguments)
    assert seen == [SourceRanking.document()]
