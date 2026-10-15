"""What-changed reads the edges the contradiction finder and the edge review write."""

from __future__ import annotations

import json
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_connections import VNextConnectionService
from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_what_changed import what_changed

TODAY = "2026-10-14"
NEGATIVE = "Alice should not auto-promote generated artifacts into memory."
POSITIVE = "Alice should auto-promote generated artifacts into memory."


def test_the_finder_and_the_edge_review_decide_current(migrated_database_urls: dict[str, str]) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "what-changed@example.com", "What Changed")
        store = PostgresVNextStore(conn)
        older = store.create_memory(
            {
                "memory_key": f"decision.{uuid4()}",
                "memory_type": "decision",
                "title": "artifact promotion",
                "canonical_text": NEGATIVE,
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "confirmation_status": "confirmed",
                "valid_from": "2026-01-01T00:00:00Z",
                "value": {"text": NEGATIVE},
                "metadata_json": {},
            }
        )
        newer = store.create_memory(
            {
                "memory_key": f"decision.{uuid4()}",
                "memory_type": "decision",
                "title": "artifact promotion",
                "canonical_text": POSITIVE,
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "confirmation_status": "confirmed",
                "valid_from": "2026-06-01T00:00:00Z",
                "supersedes": str(older["id"]),
                "value": {"text": POSITIVE},
                "metadata_json": {},
            }
        )
        store.update_memory(memory_id=str(older["id"]), patch={"superseded_by": str(newer["id"])})
        belief = store.create_belief({"memory_id": str(newer["id"]), "claim": POSITIVE, "confidence": 0.8})
        artifact = VNextContradictionService(store).generate_contradiction_report(
            ContradictionFinderRequest(agent_identity=None, query="artifacts", domains=("project",))
        )
        edges = store._fetch_all(
            "SELECT id, from_id, to_type, to_id, metadata_json, valid_to FROM graph_edges WHERE user_id = %s AND edge_type = 'contradicts'",
            (str(user_id),),
        )
        assert edges, artifact
        assert any(str(row["to_type"]) == "belief" and str(row["to_id"]) == str(belief["id"]) for row in edges)
        open_answer = what_changed(store, user_id=user_id, identity=None, topic="artifact promotion", today=TODAY)
        chain = open_answer["chains"][0]
        assert chain["current"] is None
        assert set(chain["disputed"]) == {str(older["id"]), str(newer["id"])}
        assert any(step["disagrees_with"] == str(newer["id"]) for step in chain["history"] if step["id"] == str(older["id"]))
        edge_id = str(edges[0]["id"])
        reviewed = VNextConnectionService(store).review_edge(edge_id=edge_id, action="review")
        assert reviewed["metadata_json"]["status"] == "reviewed"
        held = what_changed(store, user_id=user_id, identity=None, topic="artifact promotion", today=TODAY)
        assert held["chains"][0]["current"]["id"] == str(newer["id"])
        rejected = VNextConnectionService(store).review_edge(edge_id=edge_id, action="reject")
        assert rejected["metadata_json"]["status"] == "rejected"
        assert rejected["valid_to"] is not None
        closed = what_changed(store, user_id=user_id, identity=None, topic="artifact promotion", today=TODAY)
        assert closed["chains"][0]["current"]["id"] == str(newer["id"])
        assert closed["chains"][0]["disputed"] == []
        assert all(step["disagrees_with"] is None for step in closed["chains"][0]["history"])
        assert str(belief["id"]) not in json.dumps(closed)
