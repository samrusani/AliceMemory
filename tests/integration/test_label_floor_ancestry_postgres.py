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
