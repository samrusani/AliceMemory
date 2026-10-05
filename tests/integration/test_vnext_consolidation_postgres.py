"""Regression: consolidation's artifact write against a real migrated store.

The memory_consolidation artifact type was missing from
``generated_artifacts_type_check`` until migration 20260706_0080 — unit
tests use fakes and the SQLite on-ramp has no generated_artifacts table,
so only a live-Postgres run of the real service catches vocabulary drift
between the code and the CHECK constraints.

The roll-up test below is the same class of guard for roll-up cards: the
candidate row, its JSON value/metadata, the acceptance transition, and the
FTS visibility of the accepted card all run against the real migrated
schema and its CHECK constraints.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from alicebot_api.vnext_embeddings import pad_embedding_vector
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import lock_label_fixture


def test_generate_memory_consolidation_persists_its_artifact(migrated_database_urls) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "consolidation@example.invalid", "Consolidation")
        store = PostgresVNextStore(conn)
        for index in range(3):
            memory = store.create_memory(
                {
                    "memory_key": f"consolidation-regression-{index}",
                    "memory_type": "semantic",
                    "title": f"Launch window fact {index}",
                    "canonical_text": "The launch window moves to March after the review.",
                    "status": "active",
                    "value": {"text": "The launch window moves to March after the review."},
                }
            )
            store.update_memory_embedding(
                memory_id=str(memory["id"]),
                vector=pad_embedding_vector([0.5, 0.1, 0.2]),
            )

        artifact = VNextConsolidationService(store).generate_memory_consolidation()

        assert artifact.get("artifact_type") == "memory_consolidation"
        assert artifact.get("status") == "needs_review"
        stored = store.get_artifact(str(artifact["id"]))
        assert stored is not None
        assert stored["artifact_type"] == "memory_consolidation"


def test_rollup_candidate_round_trips_and_acceptance_promotes_it(
    migrated_database_urls, monkeypatch
) -> None:
    """Happy path on real Postgres: the consolidation run proposes a roll-up
    card, acceptance promotes it without superseding any member, and the
    accepted card is FTS-visible while candidates never were."""
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "rollups@example.invalid", "Rollups")
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        members = []
        for index, (text, day) in enumerate(
            (
                ("I played Hollow Knight for 25 hours", "2023-06-02"),
                ("I played Stardew Valley for 85 hours", "2023-06-20"),
                ("I played Celeste for 10 hours", "2023-07-01"),
            )
        ):
            members.append(
                store.create_memory(
                    {
                        "memory_key": f"rollup-regression-{index}",
                        "memory_type": "episode",
                        "title": text,
                        "canonical_text": text,
                        "summary": text,
                        "status": "active",
                        "value": {"text": text},
                        "domain": "personal",
                        "sensitivity": "internal",
                        "metadata_json": {"session_date": day},
                    }
                )
            )

        artifact = VNextConsolidationService(store).generate_memory_consolidation(
            MemoryConsolidationRequest()
        )
        rollups = artifact["metadata_json"]["rollups"]
        assert rollups["enabled"] is True
        assert len(rollups["proposals"]) == 1
        candidate_id = str(rollups["proposals"][0]["candidate_memory_id"])

        candidate = store.get_memory(candidate_id)
        assert candidate is not None
        assert candidate["status"] == "candidate"
        consolidation = candidate["metadata_json"]["consolidation"]
        assert consolidation["proposal_kind"] == "rollup"
        assert sorted(consolidation["cluster_member_ids"]) == sorted(str(row["id"]) for row in members)
        assert consolidation["proposed_supersede"] == []
        assert len(candidate["value"]["rollup"]["instances"]) == 3
        # Review gate on the real store: the candidate is not searchable.
        found = store.search_memories_fts(query="hours played in total", match_any=True, limit=10)
        assert candidate_id not in {str(row["id"]) for row in found}

        result = VNextMemoryCommitService(store).accept_consolidation_candidate(
            candidate_id, reason="Reviewed the games roll-up on Postgres."
        )
        assert result["status"] == "accepted"
        assert result["superseded_member_ids"] == []
        accepted = store.get_memory(candidate_id)
        assert accepted["status"] == "active"
        for row in members:
            member = store.get_memory(str(row["id"]))
            assert member["status"] == "active"
            assert member["superseded_by"] is None
        # The accepted card is an ordinary FTS-indexed memory now.
        found = store.search_memories_fts(query="hours played in total", match_any=True, limit=10)
        assert candidate_id in {str(row["id"]) for row in found}
