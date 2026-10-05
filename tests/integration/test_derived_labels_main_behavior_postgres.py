"""Execute the original stale-label behaviors even before the new kernel exists."""

from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore


def test_source_relabel_reaches_an_existing_report_on_main(migrated_database_urls):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id, f"main-behavior-{user_id}@example.invalid", "Synthetic main behavior"
        )
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        source = store.create_source(
            {"source_type": "note", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public"}
        )
        report = store.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Synthetic report",
                "content_markdown": "Synthetic report",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"workflow": "daily_brief", "source_refs": [str(source["id"])]},
            }
        )
        lock = getattr(store, "lock_label_writes", None)
        if callable(lock):
            lock(exclusive=True)
        store.update_source(source_id=str(source["id"]), patch={"domain": "health", "sensitivity": "confidential"})
        updated = store.get_artifact(str(report["id"]))
        assert updated["domain"] == "health"
        assert updated["sensitivity"] == "confidential"


def test_insert_floor_reads_current_source_labels_on_main(migrated_database_urls):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"main-floor-{user_id}@example.invalid", "Synthetic main floor")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {"source_type": "note", "content_hash": str(uuid4()), "domain": "health", "sensitivity": "confidential"}
        )
        report = store.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Synthetic report",
                "content_markdown": "Synthetic report",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"workflow": "daily_brief", "source_refs": [str(source["id"])]},
            }
        )
        assert report["domain"] == "health"
        assert report["sensitivity"] == "confidential"
