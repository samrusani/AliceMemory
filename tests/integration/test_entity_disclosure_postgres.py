"""The entity read fence against real PostgreSQL rows and graph SQL."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, evaluate_agent_policy
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_retrieval import VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore


def test_entity_names_and_counts_follow_the_postgres_read_fence(migrated_database_urls):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "entity-fence@example.invalid", "Entity fence")
        store = PostgresVNextStore(conn)
        _, raw = create_agent_key(
            store, user_id=user, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha"
        )
        identity = resolve_agent_identity(store, user_id=user, raw_key=raw, payload={})
        decision = evaluate_agent_policy(identity=identity, action="memory.recall", sensitivity_allowed=ALL_SENSITIVITY)
        for name, domain, project in [
            ("Meridian", "project", "alpha"),
            ("Cedar", "health", "alpha"),
            ("Briar", "project", "beta"),
        ]:
            entity = store.create_entity({"name": name, "entity_type": "person", "mention_count": 900})
            memory = store.create_memory(
                {
                    "memory_key": name,
                    "canonical_text": name + " observation",
                    "status": "active",
                    "domain": domain,
                    "sensitivity": "public",
                    "project_id": project,
                    "metadata_json": {"project_scope": [project]},
                }
            )
            store.create_edge(
                {
                    "from_type": "memory",
                    "from_id": memory["id"],
                    "to_type": "entity",
                    "to_id": entity["id"],
                    "edge_type": "mentions",
                }
            )
        service = VNextRetrievalService(store)
        _, _, entities = service._memory_graph_rows(
            entity_read_fenced=True,
            query="Meridian Cedar Briar",
            domains=list(decision.effective_domains),
            sensitivity_allowed=list(decision.effective_sensitivity_allowed),
            projects=decision.effective_project_scope,
            limit=10,
        )
        assert [row["name"] for row in entities] == ["Meridian"]
        assert "mention_count" not in entities[0]
        _, _, owner = service._memory_graph_rows(
            entity_read_fenced=False,
            query="Meridian Cedar Briar",
            domains=[],
            sensitivity_allowed=list(ALL_SENSITIVITY),
            limit=10,
        )
        assert {row["name"] for row in owner} == {"Meridian", "Cedar", "Briar"}
        assert all(row["mention_count"] == 900 for row in owner)


@pytest.mark.parametrize(
    "case", ("visible", "reverse", "deleted_source", "expired_edge", "person", "since", "until", "project")
)
def test_source_only_entity_admission_and_lifecycle(migrated_database_urls, case):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "source-entity@example.invalid", "Source entity")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Neutral document",
                "content_hash": "f" * 64,
                "domain": "project",
                "sensitivity": "public",
                "source_created_at": "2026-10-04T00:00:00Z",
                "metadata_json": {"project_scope": ["alpha"], "people": ["dana"]},
            }
        )
        entity = store.create_entity({"name": "Meridian", "entity_type": "person", "mention_count": 900})
        edge = {
            "from_type": "source",
            "from_id": source["id"],
            "to_type": "entity",
            "to_id": entity["id"],
            "edge_type": "mentions",
        }
        if case == "reverse":
            edge = {
                "from_type": "entity",
                "from_id": entity["id"],
                "to_type": "source",
                "to_id": source["id"],
                "edge_type": "mentions",
            }
        if case == "expired_edge":
            edge.update(valid_from="2026-09-01T00:00:00Z", valid_to="2026-10-01T00:00:00Z")
        store.create_edge(edge)
        if case == "deleted_source":
            conn.execute("UPDATE sources SET deleted_at = now() WHERE id = %s", (source["id"],))
        filters = {"projects": ("alpha",), "scope_people": ("dana",)}
        if case == "project":
            filters["projects"] = ("beta",)
        if case == "person":
            filters["scope_people"] = ("alex",)
        if case == "since":
            filters["scope_window_start"] = datetime(2026, 10, 5, tzinfo=UTC)
        if case == "until":
            filters["scope_window_end"] = datetime(2026, 10, 3, tzinfo=UTC)
        rows, _status, entities = VNextRetrievalService(store)._memory_graph_rows(
            query="Meridian",
            domains=["project"],
            sensitivity_allowed=["public"],
            limit=10,
            entity_read_fenced=True,
            **filters,
        )
        assert rows == []
        assert bool(entities) is (case in ("visible", "reverse"))
        assert all("mention_count" not in row for row in entities)


@pytest.mark.parametrize("case", ("visible", "candidate", "expired", "deleted"))
def test_entity_memory_lifecycle_exclusions(migrated_database_urls, case):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "memory-entity@example.invalid", "Memory entity")
        store = PostgresVNextStore(conn)
        payload = {
            "memory_key": "fixture",
            "canonical_text": "Neutral observation",
            "status": "candidate" if case == "candidate" else "active",
            "domain": "project",
            "sensitivity": "public",
        }
        if case == "expired":
            payload.update(valid_from="2020-01-01T00:00:00Z", valid_to="2021-01-01T00:00:00Z")
        memory = store.create_memory(payload)
        entity = store.create_entity({"name": "Meridian", "entity_type": "person"})
        store.create_edge(
            {
                "from_type": "memory",
                "from_id": memory["id"],
                "to_type": "entity",
                "to_id": entity["id"],
                "edge_type": "mentions",
            }
        )
        if case == "deleted":
            conn.execute("UPDATE memories SET deleted_at = now() WHERE id = %s", (memory["id"],))
        rows, _status, entities = VNextRetrievalService(store)._memory_graph_rows(
            query="Meridian", domains=["project"], sensitivity_allowed=["public"], limit=10, entity_read_fenced=True
        )
        assert bool(rows) is bool(entities) is (case == "visible")
