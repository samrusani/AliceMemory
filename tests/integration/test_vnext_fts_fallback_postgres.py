"""Regression: the FTS OR-fallback against a real migrated Postgres store.

With no embeddings configured (the default first-hour setup) recall is
pure full-text, and ``websearch_to_tsquery('english', ...)`` ANDs every
non-stopword term -- so a natural-language question returned zero rows
against a memory a keyword query finds instantly. Unit tests cover the
SQLite FTS5 path; only a live-Postgres run exercises the ``to_tsquery``
OR expression, its stemming, and its lexeme sanitization.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_source_fence import SourceReadFence

QUESTION = "When does the Alice public announcement go out?"


def _commit_announcement_decision(store: PostgresVNextStore) -> dict[str, object]:
    return store.create_memory(
        {
            "memory_key": "decision.alice-announcement",
            "memory_type": "decision",
            "title": "Alice public announcement timing",
            "canonical_text": (
                "Decision: Alice public announcement goes out Monday after the pre-launch audit passes."
            ),
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
            "value": {"text": "Alice public announcement goes out Monday."},
        }
    )


def test_natural_language_question_falls_back_to_or_matching(migrated_database_urls, monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "fts-fallback@example.invalid", "FTS Fallback")
        store = PostgresVNextStore(conn)
        memory = _commit_announcement_decision(store)

        # Strict AND semantics miss: "go" stems apart from "goes", so
        # websearch_to_tsquery demands a lexeme the row never contains.
        assert store.search_memories_fts(query=QUESTION) == []
        rows = store.search_memories_fts(query=QUESTION, match_any=True)
        assert [str(row["id"]) for row in rows] == [str(memory["id"])]

        # End to end: the retrieval service retries with OR semantics and
        # the trace reports the relaxed pass honestly.
        pack = VNextRetrievalService(store).compile_context_pack(VNextRetrievalRequest(query=QUESTION), source_fence=SourceReadFence.unfenced())
        assert [str(item["id"]) for item in pack["relevant_memories"]] == [str(memory["id"])]
        assert pack["trace"]["stages"]["fts"] == {
            "source": "postgres_fts_or_fallback",
            "candidate_count": 1,
        }

        # A query the AND pass already satisfies never uses the fallback.
        pack = VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query="Alice announcement"),
            source_fence=SourceReadFence.unfenced()
        )
        assert [str(item["id"]) for item in pack["relevant_memories"]] == [str(memory["id"])]
        assert pack["trace"]["stages"]["fts"] == {"source": "postgres_fts", "candidate_count": 1}


def test_match_any_is_safe_against_tsquery_metacharacters(migrated_database_urls) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "fts-hostile@example.invalid", "FTS Hostile")
        store = PostgresVNextStore(conn)
        memory = _commit_announcement_decision(store)

        hostile_queries = [
            "announcement & !audit",
            "monday | (",
            "announcement:* <-> go",
            "'; drop table memories; --",
            "!!(announcement)",
        ]
        for hostile in hostile_queries:
            rows = store.search_memories_fts(query=hostile, match_any=True)  # must not raise
            assert isinstance(rows, list)

        # Metacharacter-only queries sanitize to no tokens and return no rows.
        assert store.search_memories_fts(query="&|!():*<->", match_any=True) == []

        # The operators are stripped, not honored: '!audit' cannot negate.
        rows = store.search_memories_fts(query="announcement & !audit", match_any=True)
        assert [str(row["id"]) for row in rows] == [str(memory["id"])]
