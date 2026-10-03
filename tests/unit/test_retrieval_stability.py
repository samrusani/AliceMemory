"""Retrieval ordering stability: churn-hardening regression pins.

Loss forensics on identical-config LongMemEval runs measured a handful of
pure-churn answer flips per run: equal-score ordering decisions fell
through directly to the row id — a uuid minted at ingest — so re-ingesting
the SAME content re-rolled every near-equal coin flip (bistable packs
under the slot budget). These tests pin the fix at three levels:

* unit — the ``content_stable_tiebreak`` cascade itself, its use in
  ``reciprocal_rank_fusion``, and the equal-score stage-list
  stabilization (``fts_score`` / ``vector_distance`` runs);
* same store — repeated ``compile_context_pack`` calls return identical
  packs modulo the per-call pack/trace uuids;
* two seeds — the same content captured into two fresh SQLite stores
  (different uuids, different write clocks) compiles packs with identical
  content projections (session labels, memory texts), including under a
  constructed exact RRF tie between two sources.

The id remains the FINAL total-order key everywhere: rows whose content
keys are byte-identical still order deterministically within one store.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from alicebot_api.source_ranking import SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService
from alicebot_api.vnext_retrieval import (
    SOURCE_STAGE_TEMPORAL,
    TIE_BREAK_CONTENT_STABLE,
    VNextRetrievalRequest,
    VNextRetrievalService,
    content_stable_tiebreak,
    reciprocal_rank_fusion,
)
from alicebot_api.vnext_retrieval import _stabilize_scored_rows  # tie-break internals under test
from alicebot_api.vnext_temporal_query import TemporalAnchor
from alicebot_api.vnext_source_fence import SourceReadFence


USER_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _clear_embedding_env(monkeypatch) -> None:
    """Pin these tests to the fusion layer rather than to ambient config.

    These assertions are about tie-break stability, and one of them guards
    that the corpus really produces an exact fused tie so the stability it
    proves is not trivially true. A configured embedding provider adds a
    vector stage that separates those scores, so the guard fires and the test
    reports a corpus problem it does not have. Nine sibling test modules
    already isolate the same way; this one did not, so it passed everywhere
    except the release gate, which is the one place a provider is required.
    """

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)


# -- cascade unit tests ---------------------------------------------------------


def test_cascade_prefers_older_content_event_date() -> None:
    older = {"id": "zzz", "canonical_text": "short", "valid_from": "2023-01-05T00:00:00Z"}
    newer = {"id": "aaa", "canonical_text": "much much longer text", "valid_from": "2023-09-01T00:00:00Z"}
    # Older event date wins even though the newer row has the longer text
    # and the lexicographically smaller id.
    assert sorted([newer, older], key=content_stable_tiebreak) == [older, newer]


def test_cascade_reads_source_created_at_and_metadata_session_date() -> None:
    by_column = {"id": "b", "title": "x", "source_created_at": "2023-03-01T00:00:00Z"}
    by_metadata = {"id": "a", "title": "x", "metadata_json": {"session_date": "2023/04/02 (Sun) 10:00"}}
    assert sorted([by_metadata, by_column], key=content_stable_tiebreak) == [by_column, by_metadata]


def test_cascade_puts_undated_rows_after_dated_rows() -> None:
    dated = {"id": "zzz", "canonical_text": "t", "valid_from": "2024-12-31T00:00:00Z"}
    undated = {"id": "aaa", "canonical_text": "a very long text indeed"}
    assert sorted([undated, dated], key=content_stable_tiebreak) == [dated, undated]


def test_cascade_prefers_longer_then_lexicographic_text() -> None:
    longer = {"id": "zzz", "canonical_text": "abcdef"}
    shorter_b = {"id": "yyy", "canonical_text": "abc"}
    shorter_a = {"id": "xxx", "canonical_text": "abb"}
    assert sorted([shorter_b, shorter_a, longer], key=content_stable_tiebreak) == [longer, shorter_a, shorter_b]


def test_cascade_fingerprint_distinguishes_identical_text() -> None:
    from_capture_one = {
        "id": "zzz",
        "canonical_text": "same text",
        "metadata_json": {"capture_content_hash": "aaaa1111", "source_chunk_index": 2},
    }
    from_capture_two = {
        "id": "aaa",
        "canonical_text": "same text",
        "metadata_json": {"capture_content_hash": "bbbb2222", "source_chunk_index": 0},
    }
    # Identical text from different captures orders by the capture digest
    # (content), not by the uuid.
    assert sorted([from_capture_two, from_capture_one], key=content_stable_tiebreak) == [
        from_capture_one,
        from_capture_two,
    ]
    source_one = {"id": "zzz", "title": "same title", "content_hash": "aaaa"}
    source_two = {"id": "aaa", "title": "same title", "content_hash": "bbbb"}
    assert sorted([source_two, source_one], key=content_stable_tiebreak) == [source_one, source_two]


# -- reciprocal rank fusion ties -----------------------------------------------


def test_rrf_equal_scores_resolve_by_content_not_id() -> None:
    # Exact fused tie: each row is rank 1 of its own stage. The content
    # winner (older session date) carries the LOSING id on purpose.
    content_winner = {"id": "zzz", "title": "x", "metadata_json": {"session_date": "2023/01/01 (Sun) 00:00"}}
    content_loser = {"id": "aaa", "title": "x", "metadata_json": {"session_date": "2023/06/01 (Thu) 00:00"}}
    fused = reciprocal_rank_fusion({"chunk_fts": [content_winner], "title_recency": [content_loser]}, k=60)
    assert [item["id"] for item, _score, _ranks in fused] == ["zzz", "aaa"]
    scores = [score for _item, score, _ranks in fused]
    assert scores[0] == scores[1]  # guard: the constructed tie is exact


def test_rrf_identical_content_ties_fall_back_to_id() -> None:
    # Byte-identical content: the id keeps the total order deterministic.
    fused = reciprocal_rank_fusion(
        {"fts": [{"id": "b", "title": "same"}], "vector": [{"id": "a", "title": "same"}]},
        k=60,
    )
    assert [item["id"] for item, _score, _ranks in fused] == ["a", "b"]


# -- scored stage-list stabilization ---------------------------------------------


def test_stabilize_reorders_equal_scores_content_first_and_keeps_distinct_scores() -> None:
    top = {"id": "m3", "canonical_text": "clearly the best match", "fts_score": 9.0}
    tied_newer = {"id": "a-first", "canonical_text": "note", "valid_from": "2024-05-01T00:00:00Z", "fts_score": 4.0}
    tied_older = {"id": "z-last", "canonical_text": "note", "valid_from": "2022-05-01T00:00:00Z", "fts_score": 4.0}
    rows = [top, tied_newer, tied_older]
    stabilized = _stabilize_scored_rows(rows)
    # Distinct score keeps its store rank; the equal-score pair reorders to
    # the older-dated row first despite its losing id.
    assert [row["id"] for row in stabilized] == ["m3", "z-last", "a-first"]


def test_stabilize_supports_ascending_scores_for_vector_distance() -> None:
    near = {"id": "m1", "canonical_text": "text", "vector_distance": 0.1}
    tied_long = {"id": "z", "canonical_text": "longer text wins", "vector_distance": 0.5}
    tied_short = {"id": "a", "canonical_text": "short", "vector_distance": 0.5}
    stabilized = _stabilize_scored_rows(
        [near, tied_short, tied_long], score_key="vector_distance", descending=False
    )
    assert [row["id"] for row in stabilized] == ["m1", "z", "a"]


def test_stabilize_leaves_unscored_lists_untouched() -> None:
    rows = [{"id": "b", "canonical_text": "y"}, {"id": "a", "canonical_text": "zzzz"}]
    assert _stabilize_scored_rows(rows) == rows  # no fts_score: store order kept


# -- graph and temporal stage ties -----------------------------------------------


class _GraphTieStore:
    """Minimal entity-hop surface: two memories tied on every timestamp."""

    def __init__(self) -> None:
        observed = "2024-01-01T00:00:00Z"
        self.entities = [{"id": "entity-1", "name": "meridian", "entity_type": "project", "mention_count": 3}]
        self.edges = [
            {"from_type": "memory", "to_type": "entity", "from_id": memory_id, "to_id": "entity-1",
             "edge_type": "mentions", "observed_at": observed}
            for memory_id in ("zzz-memory", "aaa-memory")
        ]
        shared = {
            "status": "active",
            "domain": "project",
            "sensitivity": "internal",
            "updated_at": "2024-01-02T00:00:00Z",
            "created_at": "2024-01-02T00:00:00Z",
        }
        self.memories = {
            # The content winner (older valid_from) carries the losing id.
            "zzz-memory": {"id": "zzz-memory", "canonical_text": "meridian kickoff",
                           "valid_from": "2023-01-01T00:00:00Z", **shared},
            "aaa-memory": {"id": "aaa-memory", "canonical_text": "meridian kickoff",
                           "valid_from": "2023-08-01T00:00:00Z", **shared},
        }

    def find_entities_by_names(self, names: tuple[str, ...]) -> list[dict[str, object]]:
        return list(self.entities)

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[dict[str, object]]:
        if to_id is not None:
            return [edge for edge in self.edges if edge["to_id"] == to_id]
        return []

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return self.memories.get(memory_id)


def test_graph_stage_timestamp_ties_resolve_by_content_not_id() -> None:
    store = _GraphTieStore()
    rows, stage, _entities = VNextRetrievalService(store, embedding_provider=None)._memory_graph_rows(
        query="meridian",
        domains=[],
        sensitivity_allowed=["public", "internal", "private", "unknown"],
        limit=8,
    )
    assert stage == "enabled"
    assert [row["id"] for row in rows] == ["zzz-memory", "aaa-memory"]


class _TemporalTieStore:
    """Sources-only surface: two same-day sources tie on window distance."""

    def __init__(self) -> None:
        self.sources = [
            # Store order deliberately leads with the content LOSER (newer
            # within the same day is not distinguishable here: identical
            # event date, so the longer title must win, not the id).
            {"id": "aaa-source", "title": "short", "domain": "unknown", "sensitivity": "internal",
             "metadata_json": {"session_date": "2023/05/10 (Wed) 00:00"}},
            {"id": "zzz-source", "title": "short but longer", "domain": "unknown", "sensitivity": "internal",
             "metadata_json": {"session_date": "2023/05/10 (Wed) 00:00"}},
        ]

    def search_sources(self, *, query: str, domains=None, sensitivity_allowed=None, limit: int = 8):
        return list(self.sources)[:limit]

    def list_provenance_links(self, *, target_type: str, target_id: str):
        return []


def test_temporal_source_boost_distance_ties_resolve_by_content() -> None:
    store = _TemporalTieStore()
    service = VNextRetrievalService(store, embedding_provider=None)
    anchor = TemporalAnchor(
        window_start=datetime(2023, 5, 1, tzinfo=UTC),
        window_end=datetime(2023, 6, 1, tzinfo=UTC),
        parsed_from="in May 2023",
    )
    ranked_lists, _record = service._source_stage_lists(
        query="what happened",
        domains=[],
        sensitivity_allowed=["public", "internal", "private", "unknown"],
        limit=8,
        winning_memories=[],
        ranking=SourceRanking.document(),
        anchor=anchor,
    )
    temporal = ranked_lists[SOURCE_STAGE_TEMPORAL]
    # Equal distance from the window center: the longer-titled source wins
    # (content cascade), not the lexicographically smaller uuid.
    assert [row["id"] for row in temporal] == ["zzz-source", "aaa-source"]


# -- same-store and two-seed pack stability ---------------------------------------

_SESSIONS = (
    # (session_id, date, turns) — chat-shaped content captured through the
    # REAL capture service. Crafted so retrieval fuses multiple ranked
    # lists: session text carries the query terms (chunk/memory FTS) and
    # one source title carries them too (title_recency lexical list),
    # yielding an exact cross-list RRF tie between two sources.
    (
        "session-cycling",
        "2023/05/20 (Sat) 02:21",
        "[USER]: My favorite bicycle is a blue Brompton folding bike.\n\n"
        "[ASSISTANT]: Noted! Folding bikes are great for commuting.",
    ),
    (
        "session-trip",
        "2023/05/21 (Sun) 09:00",
        "[USER]: The bicycle trip to Ghent is planned for late September.\n\n"
        "[ASSISTANT]: A bicycle trip in autumn sounds lovely.",
    ),
    (
        "session-unrelated",
        "2023/06/02 (Fri) 12:00",
        "[USER]: My tomato plants are finally ripening this week.\n\n"
        "[ASSISTANT]: Fresh tomatoes are the best part of summer.",
    ),
)

_QUERY = "What bicycle does the user ride?"


def _ingest_synthetic_store(db_path: Path) -> None:
    with sqlite_user_connection(db_path, USER_ID) as conn:
        ensure_sqlite_user(conn, USER_ID, "stability@alice.local", "Stability Tests")
        store = SQLiteVNextStore(conn, USER_ID)
        capture = VNextCaptureService(store, actor_type="system")
        for session_id, date, text in _SESSIONS:
            capture.capture_source(
                SourceCaptureInput(
                    source_type="chat_session",
                    title=f"Chat session {session_id} on {date}",
                    raw_text=f"Chat session {session_id} on {date}.\n\n{text}",
                    connector_name="stability-test",
                    external_id=f"stability/{session_id}",
                    domain="unknown",
                    sensitivity="internal",
                    metadata_json={"session_id": session_id, "session_date": date},
                )
            )
        for memory in store.list_memories(status="candidate"):
            store.update_memory(memory_id=str(memory["id"]), patch={"status": "active"}, actor_type="system")


def _compile_pack(db_path: Path) -> dict[str, object]:
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        service = VNextRetrievalService(store)
        return service.compile_context_pack(
            VNextRetrievalRequest(query=_QUERY, max_items=8, include_sources=True, actor_type="system"),
            source_fence=SourceReadFence.unfenced()
        )


def _normalized_pack(pack: dict[str, object]) -> str:
    """Canonical JSON with the per-call uuids and clock-derived note removed."""
    scrubbed = json.loads(json.dumps(pack, sort_keys=True, default=str))
    scrubbed.pop("context_pack_id", None)
    scrubbed.pop("trace_id", None)
    trace = scrubbed.get("trace") or {}
    trace.pop("trace_id", None)
    return json.dumps(scrubbed, sort_keys=True)


def _content_projection(pack: dict[str, object]) -> dict[str, object]:
    """uuid-free composition: session labels and memory texts, in pack order."""
    sources = pack.get("sources") or []
    memories = pack.get("relevant_memories") or []
    return {
        "source_sessions": [
            (source.get("metadata_json") or {}).get("session_id") for source in sources
        ],
        "memory_texts": [memory.get("canonical_text") for memory in memories],
        "fusion_tie_break": (pack.get("trace") or {}).get("fusion", {}).get("tie_break"),
    }


def test_same_store_repeated_packs_are_identical_modulo_pack_uuids(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite3"
    _ingest_synthetic_store(db_path)
    first = _compile_pack(db_path)
    second = _compile_pack(db_path)
    assert _normalized_pack(first) == _normalized_pack(second)


def test_two_seed_ingest_compiles_identical_pack_composition(tmp_path: Path) -> None:
    seed_a = tmp_path / "seed_a.sqlite3"
    seed_b = tmp_path / "seed_b.sqlite3"
    _ingest_synthetic_store(seed_a)
    _ingest_synthetic_store(seed_b)

    pack_a = _compile_pack(seed_a)
    pack_b = _compile_pack(seed_b)

    projection_a = _content_projection(pack_a)
    projection_b = _content_projection(pack_b)
    assert projection_a == projection_b
    assert projection_a["fusion_tie_break"] == TIE_BREAK_CONTENT_STABLE
    # The synthetic corpus retrieves real content on both sides.
    assert projection_a["source_sessions"], "expected pack sources for the synthetic corpus"
    assert projection_a["memory_texts"], "expected pack memories for the synthetic corpus"

    # Guard: the corpus really does produce at least one exact fused tie
    # among selected source candidates, so this test would have been a
    # coin flip under the old id tie-break rather than trivially stable.
    trace_scores = [
        record["rrf_score"]
        for record in (pack_a.get("trace") or {}).get("selected", [])
        if record.get("target_type") == "source"
    ]
    assert len(trace_scores) != len(set(trace_scores)), "expected an exact fused source tie"
