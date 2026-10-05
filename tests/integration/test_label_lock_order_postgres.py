"""Live advisory grants enforce S before L and survive savepoint rollback."""
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_label_writes import LabelLockOrderError, held_label_locks


def _user(url, user):
    with user_connection(url, user) as conn:
        ContinuityStore(conn).create_user(user, f"locks-{user}@example.test", "Synthetic")


def test_real_pg_strict_s_after_l_is_refused(migrated_database_urls):
    user = uuid4()
    url = migrated_database_urls["app"]
    _user(url, user)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        store.lock_label_writes()
        assert held_label_locks(store) == (False, True, False)
        with pytest.raises(LabelLockOrderError, match="graph lock must precede"):
            store.lock_graph_mutation()


def test_real_pg_savepoint_rollback_releases_live_grants(migrated_database_urls):
    user = uuid4()
    url = migrated_database_urls["app"]
    _user(url, user)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        with pytest.raises(ValueError, match="rollback"):
            with conn.transaction():
                store.lock_graph_mutation()
                store.lock_label_writes(exclusive=True)
                assert held_label_locks(store) == (True, True, True)
                raise ValueError("rollback")
        assert held_label_locks(store) == (False, False, False)
        store.lock_graph_mutation()
        store.lock_label_writes()
        assert held_label_locks(store) == (True, True, False)


def test_real_pg_changing_hook_needs_exclusive_before_the_update(migrated_database_urls):
    user = uuid4()
    url = migrated_database_urls["app"]
    _user(url, user)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        row = store.create_memory({"memory_key": "original", "canonical_text": "synthetic", "domain": "project", "sensitivity": "public"})
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        with pytest.raises(LabelLockOrderError, match="exclusive label lock"):
            store.update_memory(memory_id=str(row["id"]), patch={"sensitivity": "confidential"})
        assert store.get_memory(str(row["id"]))["sensitivity"] == "public"
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        assert store.update_memory(memory_id=str(row["id"]), patch={"sensitivity": "confidential"})["sensitivity"] == "confidential"
