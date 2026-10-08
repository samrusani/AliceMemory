"""Actual PostgreSQL encoded dependency and confirmation controls."""
import json
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_derived_labels import dependencies_of
from alicebot_api.vnext_label_writes import walk_dependants
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_source_move_label_preview_postgres import ALPHA, BETA, _snapshot
from tests.unit.test_encoded_label_dependencies import encoded_object


def test_encoded_source_move_previews_without_writes_then_confirms(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user = uuid4()
    with user_connection(url, user) as conn:
        ContinuityStore(conn).create_user(user, f"encoded-{user}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Synthetic", "content_hash": str(user), "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}})
        reference = encoded_object("source_id", str(source["id"]))
        copy = store.create_memory({"memory_key": "encoded", "canonical_text": "Synthetic", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": reference, "project_scope": [ALPHA]}})
        unrelated = store.create_memory({"memory_key": "unrelated", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"note": encoded_object("source_id", str(uuid4()))}})
        assert ("source", str(source["id"])) in dependencies_of("memory", copy)
        assert {str(row["id"]) for row in walk_dependants(store, [str(source["id"])])} == {str(copy["id"])}
    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url=url))
    before = _snapshot(url, user, source)
    request = router.VNextSourceReviewRequest(user_id=user, action="assign_project", project_id=BETA, sensitivity="confidential")
    preview = router.review_vnext_source(UUID(str(source["id"])), request)
    payload = json.loads(preview.body)
    assert preview.status_code == 200 and payload["preview"] is True
    assert payload["derived_rows_hidden_from_project_keys"] == 1
    assert payload["confirm_required"] is True
    assert _snapshot(url, user, source) == before
    confirmed = router.review_vnext_source(UUID(str(source["id"])), request.model_copy(update={"confirm_label_hide": True}))
    assert confirmed.status_code == 200
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        stored = store.get_memory(str(copy["id"]))
        assert stored["sensitivity"] == "confidential"
        assert stored["metadata_json"]["source_id"] == reference
        assert BETA in stored["metadata_json"]["project_floor"]
        assert store.get_memory(str(unrelated["id"]))["sensitivity"] == "public"
    assert _snapshot(url, user, source)["provenance_links"] == before["provenance_links"]


@pytest.mark.parametrize("key,kind", [("source_id", "source"), ("memory_id", "memory"), ("artifact_id", "artifact"), ("belief_ids", "belief")])
def test_encoded_metadata_references_keep_the_canonical_reverse_edge(migrated_database_urls, key, kind):
    user = uuid4()
    root_id = str(uuid4())
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"encoded-keys-{user}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        row = store.create_memory({"memory_key": "encoded-key", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public"})
        metadata = {key: [root_id] if key.endswith("ids") else root_id, "consolidation": {}}
        raw = json.dumps(metadata)
        encoded = "".join("\\u%04x" % ord(char) if char.isalnum() or char == "_" else char for char in raw)
        conn.execute("UPDATE memories SET metadata_json = %s::jsonb WHERE id = %s", (encoded, row["id"]))
        stored = store.get_memory(str(row["id"]))
        assert (kind, root_id) in dependencies_of("memory", stored)
        assert {str(item["id"]) for item in walk_dependants(store, [root_id])} == {str(row["id"])}


@pytest.mark.parametrize("key,kind", [("source_id", "source"), ("artifact_id", "artifact")])
def test_encoded_value_object_keeps_its_canonical_reverse_edge(migrated_database_urls, key, kind):
    user = uuid4()
    root_id = str(uuid4())
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"encoded-value-{user}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        row = store.create_memory({"memory_key": "encoded-value", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public"})
        conn.execute("UPDATE memories SET value = %s, metadata_json = %s WHERE id = %s", (Jsonb(encoded_object(key, root_id)), Jsonb({"consolidation": {}}), row["id"]))
        stored = store.get_memory(str(row["id"]))
        assert (kind, root_id) in dependencies_of("memory", stored)
        assert {str(item["id"]) for item in walk_dependants(store, [root_id])} == {str(row["id"])}


def test_a_candidate_open_loop_direct_source_column_is_a_reverse_edge(migrated_database_urls):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"loop-edge-{user}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        source = store.create_source({"source_type": "note", "title": "Synthetic", "content_hash": str(user), "domain": "project", "sensitivity": "public"})
        loop = store.create_open_loop({"title": "Synthetic", "loop_type": "task", "source_id": str(source["id"]), "domain": "project", "sensitivity": "public", "metadata_json": {"discovered_by": "synthetic"}})
        assert {str(row["id"]) for row in walk_dependants(store, [str(source["id"])])} == {str(loop["id"])}
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
        assert store.get_open_loop(str(loop["id"]))["sensitivity"] == "confidential"
