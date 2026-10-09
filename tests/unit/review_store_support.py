"""A SQLite store with the readers and writers of a graph edge and a belief that only PostgreSQL has, and a vault to use it on.

The review services and the helpers of the review routes read an edge and a belief through ``get_edge``, ``get_belief``,
``update_edge_status`` and ``update_belief_status``. The SQLite store has none of them, so this adapter keeps the edges and
beliefs a test adds in memory, records every write, and hands everything else to the SQLite store.
"""
from __future__ import annotations

from uuid import uuid4

from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_redacted_input_containment_sqlite import ALPHA

CEILING = ("public", "internal", "private", "unknown")


class ReviewStore:
    """The SQLite store, with the readers and writers of an edge and a belief that only PostgreSQL has. Writes are recorded."""

    def __init__(self, store: SQLiteVNextStore) -> None:
        self._store = store
        self.edges: dict[str, dict] = {}
        self.beliefs: dict[str, dict] = {}
        self.writes: list[tuple[str, str, str]] = []

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def add_edge(self, source_id: str, to_type: str, to_id: str) -> str:
        row = self._store.create_graph_edge(
            {"from_type": "source", "from_id": source_id, "to_type": to_type, "to_id": str(to_id), "edge_type": "mentions",
             "confidence": 0.5, "explanation": "an edge", "created_by": "test", "metadata_json": {"status": "candidate"}}
        )
        self.edges[str(row["id"])] = dict(row)
        return str(row["id"])

    def add_belief(self, memory_id: str) -> str:
        belief_id = str(uuid4())
        self.beliefs[belief_id] = {"id": belief_id, "memory_id": str(memory_id), "claim": "a claim", "status": "active"}
        return belief_id

    def get_edge(self, edge_id: str):
        return self.edges.get(str(edge_id))

    def get_belief(self, belief_id: str):
        return self.beliefs.get(str(belief_id))

    def update_edge_status(self, *, edge_id: str, status: str, actor_type: str = "system"):
        self.writes.append(("edge", edge_id, status))
        return {**self.edges[edge_id], "metadata_json": {"status": status}}

    def update_belief_status(self, *, belief_id: str, status: str, confidence=None, superseded_by=None):
        self.writes.append(("belief", belief_id, status))
        return {**self.beliefs[belief_id], "status": status}


def event_types(store: ReviewStore, target_type: str, target_id: str) -> list[str]:
    return [str(event["event_type"]) for event in store.list_events(target_type=target_type, target_id=target_id)]


def world(vault):
    """A vault with a clear memory, a confidential one, a redacted one and a copy built from the redacted one."""
    vault.redact()
    conn_context = sqlite_user_connection(vault.path, vault.user)
    conn = conn_context.__enter__()
    store = SQLiteVNextStore(conn, vault.user)
    confidential = store.create_memory(
        {"memory_key": "alpha.confidential", "memory_type": "episode", "title": "CONFSECRET", "canonical_text": "CONFSECRET",
         "status": "active", "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}}
    )
    ends = {
        "clear": str(vault.clear["id"]),
        "redacted": vault.redacted,
        "contained copy": str(vault.chain["id"]),
        "above the ceiling": str(confidential["id"]),
        "missing": str(uuid4()),
    }
    return conn_context, ReviewStore(store), ends
