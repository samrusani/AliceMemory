"""Passage ranking on PostgreSQL.

Asserts fences, the cap, duplicate collapse, a planted passage, and the capped
scan. It does not assert the order of equal-looking rows: PostgreSQL ranks with
``ts_rank`` and SQLite with bm25.
"""

from __future__ import annotations

import functools
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


def test_unset_is_document_mode_on_postgres_for_every_caller(
    migrated_database_urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passage mode is the default on SQLite only. On PostgreSQL an unset variable is document mode for the owner,
    an unbound admin key and a key with limits. ``passage`` keeps today's behaviour, including the limited-key rule.

    Mutation: name ``PostgresVNextStore`` in ``PASSAGE_BY_DEFAULT_STORES``, or make the default passage mode for
    every store (the unset rows become passage mode), or drop the limited-key rule (the last row does).
    """

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "unset-default@example.invalid", "Unset default")
        store = PostgresVNextStore(conn)
        visible = store.create_source(
            {
                "source_type": "note",
                "title": "Inventory",
                "content_hash": "sha256:unset-default",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        for index, place in enumerate(("first", "second", "third")):
            store.create_source_chunk(
                {"source_id": str(visible["id"]), "chunk_index": index, "text": f"brass lantern on the {place} hook"}
            )
        _record, limited_key = create_agent_key(
            store, user_id=user_id, agent_id="reader", permission_profile="trusted_local_agent"
        )
        _record, admin_key = create_agent_key(
            store, user_id=user_id, agent_id="admin", permission_profile="admin_agent"
        )
    context = MCPRuntimeContext(database_url=migrated_database_urls["app"], user_id=user_id)

    seen: list[tuple[object, object]] = []
    original = VNextRetrievalService.search_source_excerpts

    def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        entries, stage = original(self, **kwargs)
        seen.append((kwargs.get("ranking"), stage.get("source")))
        return entries, stage

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", wrapped)

    def recall(caller: str, setting: str | None) -> tuple[object, dict[str, object]]:
        if setting is None:
            monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
        else:
            monkeypatch.setenv(SEARCH_QUALITY_ENV, setting)
        keys = {"owner": None, "admin": admin_key, "limited": limited_key}
        if keys[caller] is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", str(keys[caller]))
        seen.clear()
        payload = call_mcp_tool(context, name="alice_recall", arguments={"query": "brass lantern"})
        assert len(seen) == 1
        return seen[0][0], payload

    for caller in ("owner", "admin", "limited"):
        for setting in (None, "off"):
            ranking, payload = recall(caller, setting)
            assert ranking == SourceRanking.document(), (caller, setting)
            assert payload["source_count"] == 1, (caller, setting)
            assert all("chunk_index" not in row for row in payload["sources"]), (caller, setting)

    expected_for = {
        "owner": SourceRanking.passage(),
        "admin": SourceRanking.passage(),
        "limited": SourceRanking.document(),
    }
    for caller, expected in expected_for.items():
        ranking, payload = recall(caller, "passage")
        assert ranking == expected, caller
        has_passages = all("chunk_index" in row for row in payload["sources"])
        assert has_passages is (expected == SourceRanking.passage()), caller
        assert payload["source_count"] == (3 if expected == SourceRanking.passage() else 1), caller


LONG_QUESTION = "Who keeps the only spare key to the brass cabinet where the almanac is stored, and where does she hang it?"
KEY_CHUNKS = (
    "Marta Quill holds the only spare key to the brass cabinet",
    "She hangs it on the hook behind the kitchen clock",
    "The cabinet holds the lamp glass and the spare wicks",
)


class _Spy:
    """The store with every ``search_source_chunks`` call recorded. ``wraps`` keeps the signature visible."""

    def __init__(self, store: PostgresVNextStore) -> None:
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


def _seed_keys(store: PostgresVNextStore, *, sensitivity: str = "public", suffix: str = "keys") -> str:
    source = store.create_source(
        {
            "source_type": "note",
            "title": "Station log",
            "content_hash": f"sha256:{suffix}",
            "captured_at": "2026-06-01T00:00:00Z",
            "domain": "project",
            "sensitivity": sensitivity,
        }
    )
    for index, text in enumerate(KEY_CHUNKS):
        store.create_source_chunk({"source_id": str(source["id"]), "chunk_index": index, "text": text})
    return str(source["id"])


def test_a_long_question_gets_passages_from_the_or_query(
    migrated_database_urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strict query holds "almanac" and matches no chunk. The OR query finds the key holder's chunks.

    Document mode answers the same question with one entry per document and no ``chunk_index``, as before.
    Mutation: remove the fallback from the passage stage, run it without ``match_any``, or drop the label suffix.
    """

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "orpass@example.invalid", "Or pass")
        store = PostgresVNextStore(conn)
        keys = _seed_keys(store)
        tides = store.create_source(
            {
                "source_type": "note",
                "title": "Tides",
                "content_hash": "sha256:tides",
                "captured_at": "2026-06-02T00:00:00Z",
                "domain": "project",
                "sensitivity": "public",
            }
        )
        store.create_source_chunk(
            {"source_id": str(tides["id"]), "chunk_index": 0, "text": "Tide tables are posted on the north wall"}
        )
        passages, stage = _passages(store, LONG_QUESTION)
        strict, strict_stage = _passages(store, "spare key cabinet")
        document, document_stage = _passages(store, LONG_QUESTION, ranking=SourceRanking.document())

    assert {str(row["id"]) for row in passages} == {keys}
    assert sorted(row["chunk_index"] for row in passages) == [0, 1, 2]
    assert any("spare key to the brass cabinet" in row["excerpt"] for row in passages)
    assert stage["chunk_fts_source"] == "postgres_fts_or_fallback"
    assert stage["passages_per_source"] == 3
    assert strict and all("chunk_index" in row for row in strict)
    assert strict_stage["chunk_fts_source"] == "postgres_fts"
    assert [str(row["id"]) for row in document] == [keys]
    assert "chunk_index" not in document[0]
    assert document_stage["chunk_fts_source"] == "postgres_fts_or_fallback"


def test_the_or_pass_applies_the_same_fences(migrated_database_urls, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confidential source and a source of another domain match the OR query. The answer does not move.

    Mutation: build the OR pass without the sensitivity or domain arguments, or skip the parent check on it.
    """

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "orfence@example.invalid", "Or fence")
        store = PostgresVNextStore(conn)
        keys = _seed_keys(store)
        before, before_stage = _passages(store, LONG_QUESTION, sensitivity_allowed=["public"], domains=["project"])
        hidden = _seed_keys(store, sensitivity="confidential", suffix="hidden-keys")
        health = store.create_source(
            {
                "source_type": "note",
                "title": "Station log",
                "content_hash": "sha256:health-keys",
                "captured_at": "2026-06-03T00:00:00Z",
                "domain": "health",
                "sensitivity": "public",
            }
        )
        store.create_source_chunk(
            {
                "source_id": str(health["id"]),
                "chunk_index": 0,
                "text": "the spare key to the brass cabinet is in the health drawer",
            }
        )
        after, after_stage = _passages(store, LONG_QUESTION, sensitivity_allowed=["public"], domains=["project"])
    assert before and {str(row["id"]) for row in before} == {keys}
    assert {row["chunk_index"] for row in before} == {row["chunk_index"] for row in after}
    assert {str(row["id"]) for row in after} == {keys}
    assert before_stage["chunk_fts_source"] == after_stage["chunk_fts_source"] == "postgres_fts_or_fallback"
    assert before_stage["chunk_fts"] == after_stage["chunk_fts"]
    blob = json.dumps(after, default=str)
    assert hidden not in blob and str(health["id"]) not in blob
    assert "health drawer" not in blob


def test_a_query_with_fewer_than_two_fallback_tokens_does_not_fall_back(
    migrated_database_urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One word that no chunk holds is not retried. One more word is.

    Mutation: let the passage stage retry a one-token query, or never retry.
    """

    _quiet(monkeypatch)
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "ortoken@example.invalid", "Or token")
        store = PostgresVNextStore(conn)
        keys = _seed_keys(store)
        spy = _Spy(store)
        rows, stage = _passages(spy, "what is the almanac")  # type: ignore[arg-type]
        assert rows == []
        assert stage["chunk_fts_source"] == "postgres_fts"
        assert not spy.or_calls
        spy.calls.clear()
        two, two_stage = _passages(spy, "what is the almanac spare")  # type: ignore[arg-type]
        assert len(spy.or_calls) == 1
    assert {str(row["id"]) for row in two} == {keys}
    assert two_stage["chunk_fts_source"] == "postgres_fts_or_fallback"


def test_alice_recall_returns_passages_for_a_long_question(
    migrated_database_urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect as an agent met it: ``sources`` held no ``chunk_index`` for a long question.

    Mutation: remove the fallback from the passage stage.
    """

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    user_id = uuid4()
    with _open(migrated_database_urls, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "ordoor@example.invalid", "Or door")
        keys = _seed_keys(PostgresVNextStore(conn))
    context = MCPRuntimeContext(database_url=migrated_database_urls["app"], user_id=user_id)
    arguments = {"query": LONG_QUESTION}
    via_call = call_mcp_tool(context, name="alice_recall", arguments=arguments)
    via_stdio = _stdio(context, arguments)
    assert via_call["sources"] == via_stdio["sources"]
    assert {str(row["id"]) for row in via_call["sources"]} == {keys}
    assert sorted(row["chunk_index"] for row in via_call["sources"]) == [0, 1, 2]
    monkeypatch.delenv(SEARCH_QUALITY_ENV)
    off = call_mcp_tool(context, name="alice_recall", arguments=arguments)
    assert [str(row["id"]) for row in off["sources"]] == [keys]
    assert "chunk_index" not in off["sources"][0]
