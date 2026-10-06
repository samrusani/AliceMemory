"""Filtering precedes limits, and locked readers refill crowded store pages."""
import json
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_derived_labels_producers_postgres import seed_grid, wire_database
from tests.integration.test_memory_mutations_api import invoke_request


@pytest.mark.parametrize("producer", ["daily-brief", "connections", "contradictions"])
def test_bound_producer_uses_admitted_memories_behind_newer_shared_rows(migrated_database_urls, monkeypatch, producer):
    url = migrated_database_urls["app"]
    wire_database(monkeypatch, url)
    user, alpha, beta, _, key, *_ = seed_grid(url)
    own = []
    selected = []
    if producer != "daily-brief":
        from alicebot_api import vnext_connections, vnext_contradictions
        module = vnext_connections if producer == "connections" else vnext_contradictions
        original = module._find_candidates
        def observe(**kwargs):
            selected.extend(str(row["id"]) for row in kwargs.get("memories", kwargs.get("new_items", [])))
            return original(**kwargs)
        monkeypatch.setattr(module, "_find_candidates", observe)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        for shared, count in ((False, 3), (True, 30)):
            for index in range(count):
                row = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Atlas synthetic launch evidence " + str(index),
                    "status": "active", "domain": "project", "sensitivity": "public", "memory_type": "episode",
                    "metadata_json": {"project_scope": [alpha, beta] if shared else [alpha]}})
                conn.execute("UPDATE memories SET created_at=%s, updated_at=%s, first_seen_at=%s, last_seen_at=%s WHERE id=%s", (
                    "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z",
                    "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z",
                    "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z",
                    "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z", row["id"]))
                if not shared:
                    own.append(str(row["id"]))
    status, body = invoke_request("POST", "/v0/vnext/artifacts/generate/" + producer,
        payload={"user_id": str(user), "scope": {"projects": [alpha]}, "options": {
            "generated_for": "2026-10-05", "memory_limit": 3, "max_connections": 2, "max_contradictions": 2, "discover_open_loops": False}},
        headers={"authorization": "Bearer " + key})
    assert status == 201, body
    printed_inputs = body["metadata_json"]["derived_from"]["memories"] if producer == "daily-brief" else selected
    assert set(own) <= set(printed_inputs), (own, printed_inputs)


def test_locked_daily_refills_sources_and_loops(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    wire_database(monkeypatch, url)
    user, alpha, beta, _, key, *_ = seed_grid(url)
    own = {"sources": [], "open_loops": []}
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        for shared, count in ((False, 3), (True, 30)):
            for index in range(count):
                scope = [alpha, beta] if shared else [alpha]
                source = store.create_source({"source_type": "note", "title": "Synthetic refill " + str(index),
                    "content_hash": str(uuid4()), "captured_at": "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z",
                    "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": scope, "raw_text": "Synthetic refill evidence"}})
                loop = store.create_open_loop({"title": "Synthetic refill " + str(index), "domain": "project", "sensitivity": "public",
                    "metadata_json": {"project_scope": scope}})
                conn.execute("UPDATE open_loops SET created_at=%s, opened_at=%s WHERE id=%s", ("2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z", "2026-10-05T11:00:00Z" if shared else "2026-10-05T10:00:00Z", loop["id"]))
                if not shared:
                    own["sources"].append(str(source["id"]))
                    own["open_loops"].append(str(loop["id"]))
    status, body = invoke_request("POST", "/v0/vnext/artifacts/generate/daily-brief",
        payload={"user_id": str(user), "scope": {"projects": [alpha]}, "options": {
            "generated_for": "2026-10-05", "source_limit": 5, "open_loop_limit": 5, "discover_open_loops": False}},
        headers={"authorization": "Bearer " + key})
    assert status == 201, body
    record = body["metadata_json"]["derived_from"]
    for kind, ids in own.items():
        assert set(ids) <= set(record[kind]), json.dumps(record)
