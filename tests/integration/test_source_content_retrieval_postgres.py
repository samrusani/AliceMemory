"""Content-aware source retrieval against a real migrated Postgres store.

The rank-1 LongMemEval failure: ``search_sources`` was content-blind
(title/uri/metadata LIKE ordered by ``captured_at DESC``), so the source
that actually contains the answer lost to whatever was captured most
recently. Unit tests cover the SQLite FTS5 mirror; only a live-Postgres
run exercises the ``search_tsv`` generated column + GIN index from
migration ``20260707_0081``, the ``websearch_to_tsquery``/``to_tsquery``
passes of ``search_source_chunks``, and the fused sources stage on top
of them.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_source_fence import SourceReadFence


def _seed_early_answer_and_late_decoys(store: PostgresVNextStore) -> dict[str, object]:
    """Answer text lives in the EARLIEST-captured session; decoys are newer."""
    early = store.create_source(
        {
            "source_type": "chat_session",
            "title": "Chat about the golden retriever",
            "content_hash": "sha256:early",
            "captured_at": "2026-01-05T00:00:00Z",
        }
    )
    store.create_source_chunk(
        {
            "source_id": str(early["id"]),
            "chunk_index": 0,
            "text": "[USER]: I adopted a golden retriever puppy named Biscuit last weekend.",
        }
    )
    for index in range(6):
        decoy = store.create_source(
            {
                "source_type": "chat_session",
                "title": f"Golden hour photo walk {index}",
                "content_hash": f"sha256:decoy-{index}",
                "captured_at": f"2026-06-0{index + 1}T00:00:00Z",
            }
        )
        store.create_source_chunk(
            {
                "source_id": str(decoy["id"]),
                "chunk_index": 0,
                "text": "We compared camera lenses and tripods for the evening shoot.",
            }
        )
    return early


def test_source_content_beats_recency_on_postgres(migrated_database_urls, monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "chunk-fts@example.invalid", "Chunk FTS")
        store = PostgresVNextStore(conn)
        early = _seed_early_answer_and_late_decoys(store)

        # Control: the content-blind lexical list alone ranks every newer
        # decoy above the session that contains the answer.
        lexical = store.search_sources(query="golden retriever Biscuit")
        assert str(lexical[0]["id"]) != str(early["id"])
        assert str(lexical[-1]["id"]) == str(early["id"])

        # The chunk search finds the answer chunk with its parent source.
        chunk_rows = store.search_source_chunks(query="golden retriever Biscuit")
        assert [str(row["source_id"]) for row in chunk_rows] == [str(early["id"])]
        assert "fts_score" in chunk_rows[0]

        # End to end: RRF over chunk-content + provenance + title/recency
        # puts the answer session first despite six newer decoys.
        pack = VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query="golden retriever Biscuit"),
            source_fence=SourceReadFence.unfenced()
        )
        assert str(pack["sources"][0]["id"]) == str(early["id"])
        stage = pack["trace"]["stages"]["sources"]
        assert stage["source"] == "rrf(chunk_fts+provenance+title_recency)"
        assert stage["chunk_fts"] == 1
        assert stage["title_recency"] == 7
        assert stage["chunk_fts_source"] == "postgres_fts"


def test_source_chunk_or_fallback_recovers_natural_language_questions_on_postgres(
    migrated_database_urls, monkeypatch
) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "chunk-fallback@example.invalid", "Chunk Fallback")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "chat_session",
                "title": "Alice public announcement thread",
                "content_hash": "sha256:announcement",
                "captured_at": "2026-01-05T00:00:00Z",
            }
        )
        store.create_source_chunk(
            {
                "source_id": str(source["id"]),
                "chunk_index": 0,
                "text": "[USER]: The Alice public announcement goes out Monday after the pre-launch audit passes.",
            }
        )

        # Strict AND semantics miss: "go" stems apart from "goes", so
        # websearch_to_tsquery demands a lexeme the chunk never contains.
        question = "When does the Alice public announcement go out?"
        assert store.search_source_chunks(query=question) == []
        rows = store.search_source_chunks(query=question, match_any=True)
        assert [str(row["source_id"]) for row in rows] == [str(source["id"])]

        # End to end: the fused stage retries once with OR semantics and
        # the trace reports the relaxed chunk pass honestly.
        pack = VNextRetrievalService(store).compile_context_pack(VNextRetrievalRequest(query=question), source_fence=SourceReadFence.unfenced())
        assert str(pack["sources"][0]["id"]) == str(source["id"])
        assert pack["trace"]["stages"]["sources"]["chunk_fts_source"] == "postgres_fts_or_fallback"


def test_search_source_chunks_is_safe_against_tsquery_metacharacters_on_postgres(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "chunk-hostile@example.invalid", "Chunk Hostile")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "manual_text",
                "title": "Deployment notes",
                "content_hash": "sha256:hostile",
            }
        )
        chunk = store.create_source_chunk(
            {"source_id": str(source["id"]), "chunk_index": 0, "text": "deployment notes with NEAR misses"}
        )

        hostile_queries = [
            "deployment & !notes",
            "monday | (",
            "deployment:* <-> go",
            "'; drop table source_chunks; --",
            "!!(deployment)",
        ]
        for hostile in hostile_queries:
            rows = store.search_source_chunks(query=hostile, match_any=True)  # must not raise
            assert isinstance(rows, list)

        # Metacharacter-only queries sanitize to no tokens and return no rows.
        assert store.search_source_chunks(query="&|!():*<->", match_any=True) == []

        # The operators are stripped, not honored: '!notes' cannot negate.
        rows = store.search_source_chunks(query="deployment & !notes", match_any=True)
        assert [str(row["id"]) for row in rows] == [str(chunk["id"])]


def test_provenance_fusion_pulls_evidence_source_on_postgres(migrated_database_urls, monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "chunk-prov@example.invalid", "Chunk Prov")
        store = PostgresVNextStore(conn)
        evidence = store.create_source(
            {
                "source_type": "document",
                "title": "Import 9f3a4c",
                "content_hash": "sha256:evidence",
                "captured_at": "2026-01-05T00:00:00Z",
            }
        )
        store.create_source_chunk(
            {"source_id": str(evidence["id"]), "chunk_index": 0, "text": "lorem ipsum dolor sit amet"}
        )
        memory = store.create_memory(
            {
                "memory_key": "preference.board-deck",
                "memory_type": "preference",
                "title": "Board deck style",
                "canonical_text": "Sam prefers the quarterly board deck in dark mode.",
                "status": "active",
                "value": {"text": "dark mode board deck"},
            }
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": str(memory["id"]),
                "source_id": str(evidence["id"]),
                "quote": "quarterly board deck in dark mode",
                "evidence_role": "quoted_from",
            }
        )

        # The evidence source has zero lexical overlap with the query
        # anywhere the lexical or chunk passes look; only the winning
        # memory's provenance can pull it into the pack.
        pack = VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query="quarterly board deck dark mode"),
            source_fence=SourceReadFence.unfenced()
        )
        assert [str(item["id"]) for item in pack["relevant_memories"]] == [str(memory["id"])]
        assert str(evidence["id"]) in {str(item["id"]) for item in pack["sources"]}
        assert pack["trace"]["stages"]["sources"]["provenance"] == 1
