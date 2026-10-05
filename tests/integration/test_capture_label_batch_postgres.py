"""A capture batch reuses only a live transaction's shared advisory grant."""
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from alicebot_api import vnext_label_writes as writes
from alicebot_api.db import set_current_user, user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_store import PostgresVNextStore


def _store(url):
    user = uuid4()
    return user, user_connection(url, user)


def _user(conn, user):
    ContinuityStore(conn).create_user(user, f"batch-{user}@example.test", "Synthetic")


def _memory(store, key, source=None):
    return store.create_memory({"memory_key": key, "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": str(source["id"])} if source else {}})


def test_capture_reuses_only_the_shared_grant_in_production_mode(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user, context = _store(migrated_database_urls["app"])
    with context as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        count = 0
        real_lock = store.lock_label_writes
        def counted_lock(*, exclusive=False):
            nonlocal count
            count += 1
            return real_lock(exclusive=exclusive)
        monkeypatch.setattr(store, "lock_label_writes", counted_lock)
        result = VNextCaptureService(store).capture_text("\n".join(f"Decision: Synthetic item {i} is assigned to synthetic route {i}." for i in range(200)), domain="project", sensitivity="public")
        assert result.candidate_memory_count == 200
        # Source creation precedes the batch, and its candidate writes share
        # one further grant. All insert floors still read their current inputs.
        assert count == 2
        assert writes._current_write_batch(store) is None
        assert writes.held_label_locks(store)[1] is True
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 200


def test_batch_grant_is_keyed_by_transaction_and_savepoint_rollback(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user, context = _store(migrated_database_urls["app"])
    with context as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        calls = []
        real_lock = store.lock_label_writes
        def counted_lock(*, exclusive=False):
            calls.append(exclusive)
            return real_lock(exclusive=exclusive)
        monkeypatch.setattr(store, "lock_label_writes", counted_lock)
        with writes.label_write_batch(store):
            frame = writes._current_write_batch(store)
            before = frame.key
            assert before[0] == str(conn.execute("SELECT pg_current_xact_id()::text AS id").fetchone()["id"])
            try:
                with store.savepoint():
                    _memory(store, "rolled-back")
                    raise ValueError("Synthetic rollback")
            except ValueError:
                pass
            _memory(store, "after-rollback")
            after = writes._current_write_batch(store).key
            assert after[0] == before[0] and after[1] == before[1] + 1
            assert calls == [False, False]
            assert writes.held_label_locks(store)[1] is True
        assert conn.execute("SELECT memory_key FROM memories").fetchone()["memory_key"] == "after-rollback"


def test_a_failed_batch_leaves_no_grant_memo_or_rows(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user, context = _store(migrated_database_urls["app"])
    with context as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        with pytest.raises(ValueError):
            with store.savepoint():
                with writes.label_write_batch(store):
                    _memory(store, "failed")
                    assert writes.held_label_locks(store)[1] is True
                    raise ValueError("Synthetic failure")
        assert writes._current_write_batch(store) is None
        assert writes.held_label_locks(store)[1] is False
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 0
        _memory(store, "fresh")
        assert writes.held_label_locks(store)[1] is True


def test_strict_mode_does_not_reuse_the_batch_grant(migrated_database_urls, monkeypatch):
    user, context = _store(migrated_database_urls["app"])
    with context as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        count = 0
        real_lock = store.lock_label_writes
        def counted_lock(*, exclusive=False):
            nonlocal count
            count += 1
            return real_lock(exclusive=exclusive)
        monkeypatch.setattr(store, "lock_label_writes", counted_lock)
        assert writes.STRICT_LOCK_ORDER is True
        with writes.label_write_batch(store):
            _memory(store, "first")
            _memory(store, "second")
            assert writes.held_label_locks(store)[1] is True
        assert count == 3


def test_batch_has_no_stale_input_label_cache_after_a_same_transaction_raise(migrated_database_urls):
    user, context = _store(migrated_database_urls["app"])
    with context as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        source = store.create_source({"source_type": "note", "title": "Synthetic", "content_hash": str(user), "domain": "project", "sensitivity": "public"})
        with writes.label_write_batch(store):
            first = _memory(store, "before-raise", source)
            assert first["sensitivity"] == "public"
            store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
            second = _memory(store, "after-raise", source)
            assert second["sensitivity"] == "confidential"


def test_one_store_reacquires_a_batch_grant_in_a_new_transaction(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user = uuid4()
    keys = []
    with psycopg.connect(migrated_database_urls["app"], row_factory=dict_row) as conn:
        store = PostgresVNextStore(conn)
        for index in range(2):
            set_current_user(conn, user)
            if index == 0:
                _user(conn, user)
            with writes.label_write_batch(store):
                keys.append(writes._current_write_batch(store).key)
                with pytest.raises(psycopg.ProgrammingError):
                    conn.commit()
                _memory(store, str(index))
                assert writes.held_label_locks(store)[1] is True
            assert writes._current_write_batch(store) is None
            conn.commit()
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert keys[0][0] != keys[1][0]
