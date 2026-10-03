"""Live PostgreSQL regressions for complete retrieval scope predicates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_embeddings import signed_memory_embedding_update
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_source_fence import SourceReadFence


def _create_user(app_url: str, user_id) -> None:
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"retrieval-{user_id}@example.invalid",
            "Retrieval scope",
        )


class _TextEmbedding3SmallStub:
    """1536-dim provider double with the production release-gate identity."""

    provider = "openai_compatible"
    model = "text-embedding-3-small"
    base_url = "https://api.openai.com/v1"

    def embed_text(self, text: str) -> list[float]:
        del text
        return [1.0, *([0.0] * 1535)]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]


def test_ambiguous_business_money_query_keeps_signed_professional_vector_candidates(
    migrated_database_urls,
) -> None:
    """Regression for v0.10.0 semantic gate run 29235844891.

    The exact paraphrase-015 query used to infer the hard ``personal`` domain
    from the word ``money``.  The benchmark fact is professional, so both FTS
    and vector SQL correctly returned no rows before HNSW ranking.  Exercise
    the production classifier, signed-vector contract, endpoint match, and
    PostgreSQL/pgvector path together at the real 1536-column width.
    """
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    _create_user(app_url, user_id)
    provider = _TextEmbedding3SmallStub()
    query = "where did the advertising money move away from adwords"

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": "vnext-eval/retrieval/paraphrase-015",
                "value": {
                    "text": (
                        "Marketing shifted the campaign budget from paid search "
                        "to podcast sponsorships."
                    )
                },
                "status": "active",
                "memory_type": "semantic",
                "title": "Campaign budget shift",
                "canonical_text": (
                    "Marketing shifted the campaign budget from paid search "
                    "to podcast sponsorships."
                ),
                "domain": "professional",
                "sensitivity": "internal",
            }
        )
        store.update_memory_embedding(
            **signed_memory_embedding_update(
                memory,
                provider.embed_text(str(memory["canonical_text"])),
                provider=provider,
            )
        )
        service = VNextRetrievalService(store, embedding_provider=provider)

        pack = service.compile_context_pack(
            VNextRetrievalRequest(
                query=query,
                max_items=10,
                include_sources=False,
                include_contradictions=False,
                actor_type="system",
            ),
            source_fence=SourceReadFence.unfenced()
        )
        explicitly_personal = service.compile_context_pack(
            VNextRetrievalRequest(
                query=query,
                domains=("personal",),
                max_items=10,
                include_sources=False,
                include_contradictions=False,
                actor_type="system",
            ),
            source_fence=SourceReadFence.unfenced()
        )

    assert pack["query_interpretation"]["domains"] == []
    assert pack["trace"]["vector_stage"] == "enabled"
    assert pack["trace"]["stages"]["vector"]["candidate_count"] >= 1
    assert [row["memory_key"] for row in pack["relevant_memories"]] == [
        "vnext-eval/retrieval/paraphrase-015"
    ]
    assert explicitly_personal["query_interpretation"]["domains"] == ["personal"]
    assert explicitly_personal["trace"]["stages"]["vector"]["candidate_count"] == 0


def test_people_and_time_predicates_apply_before_limit_beyond_rank_4000(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    _create_user(app_url, user_id)
    with user_connection(app_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (
                  user_id, memory_key, value, status, source_event_ids,
                  memory_type, title, canonical_text, summary,
                  domain, sensitivity, confirmation_status,
                  first_seen_at, last_seen_at, created_at, updated_at,
                  metadata_json
                )
                SELECT
                  %s, 'scope.decoy.' || ordinal, '{}'::jsonb, 'active', '[]'::jsonb,
                  'semantic', 'Scope needle', 'scope needle decoy', 'scope needle',
                  'professional', 'internal', 'confirmed',
                  now(), now(), now(), now(),
                  '{"people":["alex"]}'::jsonb
                FROM generate_series(1, 4001) AS ordinal
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO memories (
                  user_id, memory_key, value, status, source_event_ids,
                  memory_type, title, canonical_text, summary,
                  domain, sensitivity, confirmation_status,
                  first_seen_at, last_seen_at, created_at, updated_at,
                  metadata_json
                ) VALUES (
                  %s, 'scope.target', '{}'::jsonb, 'active', '[]'::jsonb,
                  'semantic', 'Scope needle', 'scope needle target', 'scope needle',
                  'professional', 'internal', 'confirmed',
                  '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z',
                  '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z',
                  '{"people":["sam"]}'::jsonb
                )
                RETURNING id
                """,
                (user_id,),
            )
            target_id = str(cur.fetchone()["id"])
        store = PostgresVNextStore(conn)
        unfiltered = store.search_memories_fts(query="scope needle", limit=4000)
        assert target_id not in {str(row["id"]) for row in unfiltered}
        scoped = store.search_memories_fts(
            query="scope needle",
            limit=1,
            scope_people=("sam",),
            scope_window_end="2020-12-31T23:59:59Z",
        )
    assert [str(row["id"]) for row in scoped] == [target_id]


def test_person_entity_resolution_returns_more_than_200_live_edges(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    _create_user(app_url, user_id)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        entity = store.create_entity(
            {
                "entity_type": "person",
                "name": "Sam",
                "normalized_name": "sam",
            }
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH inserted AS (
                  INSERT INTO memories (
                    user_id, memory_key, value, status, source_event_ids,
                    memory_type, title, canonical_text, domain, sensitivity
                  )
                  SELECT
                    %s, 'person.edge.' || ordinal, '{}'::jsonb, 'active', '[]'::jsonb,
                    'semantic', 'Sam linked memory', 'Sam linked memory ' || ordinal,
                    'professional', 'internal'
                  FROM generate_series(1, 251) AS ordinal
                  RETURNING id
                )
                INSERT INTO graph_edges (
                  user_id, from_type, from_id, to_type, to_id,
                  edge_type, confidence, created_by, metadata_json
                )
                SELECT
                  %s, 'memory', id::text, 'entity', %s,
                  'mentions', 1.0, 'integration-test', '{}'::jsonb
                FROM inserted
                """,
                (user_id, user_id, str(entity["id"])),
            )
        resolved = VNextRetrievalService(store, embedding_provider=None)._person_linked_memory_ids(
            frozenset({"sam"})
        )
    assert len(resolved) == 251


def test_people_and_time_scope_precedes_source_chunk_title_and_loop_limits(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    _create_user(app_url, user_id)
    with user_connection(app_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sources (
                  user_id, source_type, title, content_hash, captured_at,
                  source_created_at, metadata_json
                ) VALUES (
                  %s, 'chat_session', 'Scoped needle source', 'target-hash',
                  '2026-01-01T00:00:00Z', NULL,
                  '{"people":["Sam"],"session_date":"2026-07-08T00:00:00Z"}'::jsonb
                )
                RETURNING id
                """,
                (user_id,),
            )
            target_source_id = str(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO source_chunks (
                  user_id, source_id, chunk_index, text, created_at
                ) VALUES (
                  %s, %s, 0, 'scoped needle evidence', '2026-01-01T00:00:00Z'
                )
                """,
                (user_id, target_source_id),
            )
            cur.execute(
                """
                INSERT INTO open_loops (
                  user_id, title, status, opened_at, created_at, updated_at, metadata_json
                ) VALUES (
                  %s, 'Scoped needle follow-up', 'open',
                  '2026-07-08T00:00:00Z', '2026-01-01T00:00:00Z',
                  '2026-01-01T00:00:00Z', '{"people":["Sam"]}'::jsonb
                )
                RETURNING id
                """,
                (user_id,),
            )
            target_loop_id = str(cur.fetchone()["id"])
            cur.execute(
                """
                WITH inserted_sources AS (
                  INSERT INTO sources (
                    user_id, source_type, title, content_hash, captured_at,
                    source_created_at, metadata_json
                  )
                  SELECT
                    %s, 'chat_session', 'Scoped needle source',
                    'decoy-hash-' || ordinal,
                    '2026-12-31T00:00:00Z'::timestamptz + ordinal * interval '1 second',
                    CASE
                      WHEN ordinal <= 210 THEN '2026-07-09T00:00:00Z'::timestamptz
                      ELSE '2027-01-01T00:00:00Z'::timestamptz
                    END,
                    CASE
                      WHEN ordinal <= 210 THEN '{"people":["Alex"]}'::jsonb
                      ELSE '{"people":["Sam"]}'::jsonb
                    END
                  FROM generate_series(1, 420) AS ordinal
                  RETURNING id, captured_at
                )
                INSERT INTO source_chunks (
                  user_id, source_id, chunk_index, text, created_at
                )
                SELECT %s, id, 0, 'scoped needle evidence', captured_at
                FROM inserted_sources
                """,
                (user_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO open_loops (
                  user_id, title, status, opened_at, created_at, updated_at, metadata_json
                )
                SELECT
                  %s, 'Scoped needle follow-up', 'open',
                  CASE
                    WHEN ordinal <= 210 THEN '2026-07-09T00:00:00Z'::timestamptz
                    ELSE '2027-01-01T00:00:00Z'::timestamptz
                  END,
                  '2026-12-31T00:00:00Z'::timestamptz + ordinal * interval '1 second',
                  '2026-12-31T00:00:00Z'::timestamptz + ordinal * interval '1 second',
                  CASE
                    WHEN ordinal <= 210 THEN '{"people":["Alex"]}'::jsonb
                    ELSE '{"people":["Sam"]}'::jsonb
                  END
                FROM generate_series(1, 420) AS ordinal
                """,
                (user_id,),
            )

        store = PostgresVNextStore(conn)
        window_start = datetime(2026, 7, 3, tzinfo=UTC)
        window_end = datetime(2026, 7, 10, tzinfo=UTC)
        chunks = store.search_source_chunks(
            query="scoped needle",
            limit=1,
            scope_people=("sam",),
            scope_window_start=window_start,
            scope_window_end=window_end,
        )
        sources = store.search_sources(
            query="scoped needle",
            limit=1,
            scope_people=("sam",),
            scope_window_start=window_start,
            scope_window_end=window_end,
        )
        loops = store.list_open_loops(
            limit=1,
            scope_people=("sam",),
            scope_window_start=window_start,
            scope_window_end=window_end,
        )
        assert [str(row["source_id"]) for row in chunks] == [target_source_id]
        assert [str(row["id"]) for row in sources] == [target_source_id]
        assert [str(row["id"]) for row in loops] == [target_loop_id]

        pack = VNextRetrievalService(store, embedding_provider=None).compile_context_pack(
            VNextRetrievalRequest(
                query="scoped needle",
                people=("Sam",),
                time_window="7d",
                reference_time=window_end,
                max_items=1,
            ),
            source_fence=SourceReadFence.unfenced()
        )
    assert [str(row["id"]) for row in pack["sources"]] == [target_source_id]
    assert [str(row["id"]) for row in pack["open_loops"]] == [target_loop_id]
