"""Actual RLS-scoped PostgreSQL write-floor and belief ancestry controls."""
from uuid import uuid4
import pytest
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore


@pytest.mark.parametrize("domain,sensitivity", [("project", "public"), ("health", "confidential")])
def test_pg_copy_and_summary_keep_tenant_and_ancestry(migrated_database_urls, domain, sensitivity):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"labels-{user_id}@example.invalid", "Labels")
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "synthetic", "content_hash": str(uuid4()), "domain": domain, "sensitivity": sensitivity})
        copy = store.create_memory({"memory_key": "copy", "canonical_text": "copy", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"source_id": str(source["id"])}})
        summary = store.create_memory({"memory_key": "summary", "canonical_text": "summary", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [str(copy["id"])]}}})
        assert copy["sensitivity"] == sensitivity
        assert summary["sensitivity"] == sensitivity
        if domain == "health":
            assert summary["domain"] == domain
        belief_id = uuid4()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO beliefs(id,user_id,memory_id,claim) VALUES (%s,%s,%s,%s)", (belief_id,user_id,copy["id"],"synthetic"))
        belief = store.read_label_rows("belief", [str(belief_id)])[0]
        assert belief["sensitivity"] == sensitivity
        assert str(belief["memory_id"]) == str(copy["id"])

        record = {"v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [str(belief_id)], "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 1}}
        artifact = store.create_artifact({"artifact_type": "daily_brief", "title": "synthetic", "content_markdown": "synthetic", "domain": "unknown", "sensitivity": "public", "metadata_json": {"workflow": "daily_brief", "derived_from": record}})
        assert artifact["sensitivity"] == sensitivity
        if domain == "health":
            assert artifact["domain"] == domain


def test_checked_project_review_moves_scope_and_propagates_labels(migrated_database_urls, monkeypatch):
    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_memories as router
    from alicebot_api.vnext_agent_keys import create_agent_key
    from uuid import UUID
    user_id = uuid4()
    alpha, beta = "prj_" + "a" * 16, "prj_" + "b" * 16
    app_url = migrated_database_urls["app"]
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"review-labels-{user_id}@example.invalid", "Labels")
        store = PostgresVNextStore(conn)
        original = store.create_memory({"memory_key": "original", "canonical_text": "original", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [alpha]}})
        summary = store.create_memory({"memory_key": "summary", "canonical_text": "summary", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [str(original["id"])]}, "project_scope": [alpha]}})
        _key, raw_key = create_agent_key(store, user_id=user_id, agent_id="alpha-only", permission_profile="admin_agent", project_scope=alpha)
        _admin, admin_key = create_agent_key(store, user_id=user_id, agent_id="unbound-admin", permission_profile="admin_agent")
    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url=app_url))
    request = router.VNextMemoryReviewRequest(user_id=user_id, action="assign_project", project_id=beta, domain="health", sensitivity="confidential")
    denied = router.review_vnext_memory(UUID(str(original["id"])), request, authorization=f"Bearer {raw_key}")
    assert denied.status_code == 403
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_memory(str(original["id"]))["metadata_json"]["project_scope"] == [alpha]
        assert store.get_memory(str(summary["id"]))["sensitivity"] == "public"
    moved = router.review_vnext_memory(UUID(str(original["id"])), request, authorization=f"Bearer {admin_key}")
    assert moved.status_code == 200
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        original_after = store.get_memory(str(original["id"]))
        summary_after = store.get_memory(str(summary["id"]))
        assert original_after["metadata_json"]["project_scope"] == [beta]
        assert summary_after["domain"] == "health"
        assert summary_after["sensitivity"] == "confidential"
        assert beta in summary_after["metadata_json"]["project_floor"]


def test_a_relabel_traverses_the_belief_backing_memory(migrated_database_urls):
    user_id = uuid4()
    url = migrated_database_urls["app"]
    with user_connection(url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"belief-relabel-{user_id}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "synthetic", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public"})
        copy = store.create_memory({"memory_key": "copy", "canonical_text": "synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": str(source["id"])}})
        belief = store.create_belief({"memory_id": str(copy["id"]), "claim": "Synthetic belief"})
        report = store.create_artifact({"artifact_type": "contradiction_report", "title": "Synthetic", "content_markdown": "Synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"belief_ids": [str(belief["id"])]}})
    other_user = uuid4()
    with user_connection(url, other_user) as conn:
        ContinuityStore(conn).create_user(other_user, f"other-belief-{other_user}@example.test", "Synthetic")
        other_store = PostgresVNextStore(conn)
        other_memory = other_store.create_memory({"memory_key": "other", "canonical_text": "synthetic", "domain": "project", "sensitivity": "public"})
        other_store.create_belief({"memory_id": str(other_memory["id"]), "claim": "Other synthetic belief"})
    with user_connection(url, user_id) as conn:
        store = PostgresVNextStore(conn)
        alias = "{" + str(copy["id"]).upper() + "}"
        assert store.list_belief_ids_for_memories([alias, str(other_memory["id"])]) == [str(belief["id"])]
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        store.update_source(source_id=str(source["id"]), patch={"sensitivity": "regulated"})
    with user_connection(url, user_id) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_memory(str(copy["id"]))["sensitivity"] == "regulated"
        assert store.get_artifact(str(report["id"]))["sensitivity"] == "regulated"


def test_postgres_owner_edit_is_clamped_in_response_event_and_storage(migrated_database_urls, monkeypatch):
    import json
    from uuid import UUID
    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_memories as router
    from alicebot_api.vnext_label_writes import without_insert_floor

    user_id = uuid4()
    url = migrated_database_urls["app"]
    with user_connection(url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-clamp-{user_id}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        source = store.create_source({"source_type": "note", "title": "Synthetic private input", "content_hash": str(user_id), "domain": "health", "sensitivity": "confidential"})
        with without_insert_floor():
            memory = store.create_memory({"memory_key": "stale-copy", "canonical_text": "Synthetic private observation", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": str(source["id"])}})
        assert memory["sensitivity"] == "public"
    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url=url))
    response = router.review_vnext_memory(UUID(str(memory["id"])), router.VNextMemoryReviewRequest(user_id=user_id, action="edit", domain="project", sensitivity="public"), authorization=None)
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["label_floor_applied"] is True
    assert payload["memory"]["domain"] == "health"
    assert payload["memory"]["sensitivity"] == "confidential"
    with user_connection(url, user_id) as conn:
        store = PostgresVNextStore(conn)
        stored = store.get_memory(str(memory["id"]))
        assert stored["domain"] == "health" and stored["sensitivity"] == "confidential"
        assert stored["metadata_json"]["source_id"] == str(source["id"])
        event = conn.execute("SELECT payload_json FROM event_log WHERE event_type='memory.labels_raised' AND target_id=%s", (str(memory["id"]),)).fetchone()
        assert event["payload_json"]["cause"] == "floor_clamped"
        encoded = json.dumps(event["payload_json"])
        assert "Synthetic private input" not in encoded
        assert "Synthetic private observation" not in encoded
