"""Capture reuses source inputs while every writer takes a live label lock."""
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from alicebot_api import vnext_label_writes as writes
from alicebot_api.db import set_current_user, user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_store import PostgresVNextStore


def _user(conn, user):
    ContinuityStore(conn).create_user(user, f"batch-{user}@example.test", "Synthetic")


def _source(store, user):
    return store.create_source({"source_type": "note", "title": "Synthetic", "content_hash": str(user), "domain": "project", "sensitivity": "public"})


def _memory(store, key, source):
    return store.create_memory({"memory_key": key, "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": str(source["id"])}})


def test_capture_reuses_only_source_inputs_and_keeps_every_writer_lock(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        calls, reads, identities = [], [], []
        real_lock, real_reader, real_memory = store.lock_label_writes, store.read_label_rows, store.create_memory
        def lock(*, exclusive=False):
            calls.append(exclusive)
            return real_lock(exclusive=exclusive)
        def reader(kind, ids):
            reads.append((kind, ids))
            return real_reader(kind, ids)
        def memory(payload, **kwargs):
            identities.append(conn.execute("SELECT pg_current_xact_id()::text AS tx, app.current_user_id()::text AS tenant").fetchone())
            frame = writes._current_capture_inputs(store)
            assert frame.key[0] == identities[-1]["tx"]
            return real_memory(payload, **kwargs)
        monkeypatch.setattr(store, "lock_label_writes", lock)
        monkeypatch.setattr(store, "read_label_rows", reader)
        monkeypatch.setattr(store, "create_memory", memory)
        result = VNextCaptureService(store).capture_text("\n".join(f"Decision: Synthetic item {i} is assigned to synthetic route {i}." for i in range(200)), domain="project", sensitivity="public")
        assert result.candidate_memory_count == 200
        assert len(calls) == 401  # Source, 200 memories, 200 provenance links.
        assert len(reads) == 1 and reads[0][0] == "source"
        assert len({(row["tx"], row["tenant"]) for row in identities}) == 1
        assert identities[0]["tenant"] == str(user)
        assert writes._current_capture_inputs(store) is None
        assert writes.held_label_locks(store)[1] is True
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 200


def test_source_input_memo_is_keyed_by_transaction_and_rollback_counter(migrated_database_urls):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        source = _source(store, user)
        with writes.capture_label_inputs(store, str(source["id"])):
            _memory(store, "before", source)
            frame = writes._current_capture_inputs(store)
            before = frame.key
            assert frame.rows
            with pytest.raises(ValueError):
                with store.savepoint():
                    store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
                    assert _memory(store, "rolled-back", source)["sensitivity"] == "confidential"
                    raise ValueError("Synthetic rollback")
            assert _memory(store, "after", source)["sensitivity"] == "public"
            after = writes._current_capture_inputs(store).key
            assert after == (before[0], before[1] + 1)
            assert writes.held_label_locks(store)[1] is True
        assert {row["memory_key"] for row in conn.execute("SELECT memory_key FROM memories").fetchall()} == {"before", "after"}


def test_failed_native_savepoint_batch_leaves_no_memo_or_rows(migrated_database_urls):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        with pytest.raises(ValueError):
            with conn.transaction():
                source = _source(store, user)
                with writes.capture_label_inputs(store, str(source["id"])):
                    _memory(store, "failed", source)
                    assert writes.held_label_locks(store)[1] is True
                    raise ValueError("Synthetic failure")
        assert writes._current_capture_inputs(store) is None
        assert writes.held_label_locks(store)[1] is False
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 0
        source = _source(store, user)
        _memory(store, "fresh", source)
        assert writes.held_label_locks(store)[1] is True


def test_source_raise_invalidates_inputs_in_the_same_transaction(migrated_database_urls, monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        store.lock_label_writes(exclusive=True)
        source = _source(store, user)
        with writes.capture_label_inputs(store, str(source["id"])):
            assert _memory(store, "before", source)["sensitivity"] == "public"
            store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
            assert _memory(store, "after", source)["sensitivity"] == "confidential"


def test_strict_mode_still_locks_every_writer_with_source_input_reuse(migrated_database_urls, monkeypatch):
    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        _user(conn, user)
        store = PostgresVNextStore(conn)
        source = _source(store, user)
        count = 0
        real_lock = store.lock_label_writes
        def lock(*, exclusive=False):
            nonlocal count
            count += 1
            return real_lock(exclusive=exclusive)
        monkeypatch.setattr(store, "lock_label_writes", lock)
        assert writes.STRICT_LOCK_ORDER is True
        with writes.capture_label_inputs(store, str(source["id"])):
            _memory(store, "first", source)
            _memory(store, "second", source)
            assert writes.held_label_locks(store)[1] is True
        assert count == 2


def test_one_store_starts_fresh_source_inputs_in_each_transaction(migrated_database_urls):
    user = uuid4()
    keys = []
    with psycopg.connect(migrated_database_urls["app"], row_factory=dict_row) as conn:
        store = PostgresVNextStore(conn)
        for index in range(2):
            set_current_user(conn, user)
            if index == 0:
                _user(conn, user)
            source = _source(store, uuid4())
            with writes.capture_label_inputs(store, str(source["id"])):
                keys.append(writes._current_capture_inputs(store).key)
                with pytest.raises(psycopg.ProgrammingError):
                    conn.commit()
                _memory(store, str(index), source)
            assert writes._current_capture_inputs(store) is None
            conn.commit()
        assert keys[0][0] != keys[1][0]


def test_pipeline_writer_waits_for_live_lock_and_reads_committed_source(migrated_database_urls):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    user = uuid4()
    url = migrated_database_urls["app"]
    with user_connection(url, user) as conn:
        _user(conn, user)
        source = _source(PostgresVNextStore(conn), user)
    queued = Event()
    def writer():
        with user_connection(url, user) as conn:
            store = PostgresVNextStore(conn)
            real_lock = store.lock_label_writes
            def lock(*, exclusive=False):
                real_lock(exclusive=exclusive)
                queued.set()
            store.lock_label_writes = lock
            with writes.capture_label_inputs(store, str(source["id"])):
                return _memory(store, "waited", source)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with user_connection(url, user) as conn:
            store = PostgresVNextStore(conn)
            store.lock_graph_mutation()
            store.lock_label_writes(exclusive=True)
            store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential"})
            future = pool.submit(writer)
            assert queued.wait(3)
            deadline = monotonic() + 3
            while monotonic() < deadline:
                waiting = conn.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND NOT granted AND classid=(hashtext('vnext_labels')::bigint & 4294967295)::oid AND objid=(hashtext(app.current_user_id()::text)::bigint & 4294967295)::oid").fetchone()["n"]
                if waiting:
                    break
                sleep(0.01)
            assert waiting == 1
            assert future.done() is False
            assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 0
        result = future.result(timeout=5)
        assert result["sensitivity"] == "confidential"
