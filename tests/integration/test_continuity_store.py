from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from alicebot_api.db import set_current_user, user_connection
from alicebot_api.store import ContinuityStore


def test_thread_session_and_event_persistence(migrated_database_urls):
    user_id = uuid4()

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        user = store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Starter thread")
        session = store.create_session(thread["id"])
        first_event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "hello"},
        )
        second_event = store.append_event(
            thread["id"],
            session["id"],
            "message.assistant",
            {"text": "hi"},
        )
        events = store.list_thread_events(thread["id"])

    assert user["id"] == user_id
    assert session["thread_id"] == thread["id"]
    assert [first_event["sequence_no"], second_event["sequence_no"]] == [1, 2]
    assert [event["kind"] for event in events] == ["message.user", "message.assistant"]
    assert events[0]["payload"]["text"] == "hello"

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE events SET kind = 'message.mutated' WHERE id = %s",
                    (first_event["id"],),
                )


def test_event_deletes_are_rejected_at_database_level(migrated_database_urls):
    user_id = uuid4()

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Delete-protected thread")
        session = store.create_session(thread["id"])
        event = store.append_event(thread["id"], session["id"], "message.user", {"text": "keep"})

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM events WHERE id = %s", (event["id"],))


def test_continuity_rls_blocks_cross_user_access(migrated_database_urls):
    owner_id = uuid4()
    intruder_id = uuid4()

    with user_connection(migrated_database_urls["app"], owner_id) as owner_conn:
        owner_store = ContinuityStore(owner_conn)
        owner_store.create_user(owner_id, "owner@example.com", "Owner")
        thread = owner_store.create_thread("Private thread")
        session = owner_store.create_session(thread["id"])
        owner_store.append_event(thread["id"], session["id"], "message.user", {"text": "secret"})

    with user_connection(migrated_database_urls["app"], intruder_id) as intruder_conn:
        intruder_store = ContinuityStore(intruder_conn)
        intruder_store.create_user(intruder_id, "intruder@example.com", "Intruder")

        with intruder_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM users WHERE id = %s", (owner_id,))
            user_count_row = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS count FROM threads WHERE id = %s", (thread["id"],))
            thread_count_row = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS count FROM sessions WHERE id = %s", (session["id"],))
            session_count_row = cur.fetchone()

        visible_events = intruder_store.list_thread_events(thread["id"])

        assert user_count_row["count"] == 0
        assert thread_count_row["count"] == 0
        assert session_count_row["count"] == 0
        assert visible_events == []

        with pytest.raises(psycopg.Error):
            intruder_store.append_event(
                thread["id"],
                None,
                "message.user",
                {"text": "tamper"},
            )


def test_runtime_role_is_insert_select_only_for_continuity_tables(migrated_database_urls):
    with psycopg.connect(migrated_database_urls["app"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  has_table_privilege(current_user, 'users', 'SELECT'),
                  has_table_privilege(current_user, 'users', 'INSERT'),
                  has_table_privilege(current_user, 'users', 'UPDATE'),
                  has_table_privilege(current_user, 'threads', 'UPDATE'),
                  has_table_privilege(current_user, 'sessions', 'UPDATE'),
                  has_table_privilege(current_user, 'events', 'UPDATE'),
                  has_table_privilege(current_user, 'events', 'DELETE'),
                  has_table_privilege(current_user, 'traces', 'SELECT'),
                  has_table_privilege(current_user, 'traces', 'INSERT'),
                  has_table_privilege(current_user, 'traces', 'UPDATE'),
                  has_table_privilege(current_user, 'trace_events', 'SELECT'),
                  has_table_privilege(current_user, 'trace_events', 'INSERT'),
                  has_table_privilege(current_user, 'trace_events', 'UPDATE'),
                  has_table_privilege(current_user, 'trace_events', 'DELETE'),
                  has_table_privilege(current_user, 'consents', 'SELECT'),
                  has_table_privilege(current_user, 'consents', 'INSERT'),
                  has_table_privilege(current_user, 'consents', 'UPDATE'),
                  has_table_privilege(current_user, 'consents', 'DELETE'),
                  has_table_privilege(current_user, 'policies', 'SELECT'),
                  has_table_privilege(current_user, 'policies', 'INSERT'),
                  has_table_privilege(current_user, 'policies', 'UPDATE'),
                  has_table_privilege(current_user, 'policies', 'DELETE'),
                  has_table_privilege(current_user, 'tools', 'SELECT'),
                  has_table_privilege(current_user, 'tools', 'INSERT'),
                  has_table_privilege(current_user, 'tools', 'UPDATE'),
                  has_table_privilege(current_user, 'tools', 'DELETE')
                """
            )
            (
                users_select,
                users_insert,
                users_update,
                threads_update,
                sessions_update,
                events_update,
                events_delete,
                traces_select,
                traces_insert,
                traces_update,
                trace_events_select,
                trace_events_insert,
                trace_events_update,
                trace_events_delete,
                consents_select,
                consents_insert,
                consents_update,
                consents_delete,
                policies_select,
                policies_insert,
                policies_update,
                policies_delete,
                tools_select,
                tools_insert,
                tools_update,
                tools_delete,
            ) = cur.fetchone()

    assert users_select is True
    assert users_insert is True
    assert users_update is False
    assert threads_update is False
    assert sessions_update is False
    assert events_update is False
    assert events_delete is False
    assert traces_select is True
    assert traces_insert is True
    assert traces_update is False
    assert trace_events_select is True
    assert trace_events_insert is True
    assert trace_events_update is False
    assert trace_events_delete is False
    assert consents_select is True
    assert consents_insert is True
    assert consents_update is True
    assert consents_delete is False
    assert policies_select is True
    assert policies_insert is True
    assert policies_update is False
    assert policies_delete is False
    assert tools_select is True
    assert tools_insert is True
    assert tools_update is False
    assert tools_delete is False


def test_concurrent_event_appends_keep_monotonic_sequence_numbers(migrated_database_urls):
    user_id = uuid4()

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Concurrent thread")
        session = store.create_session(thread["id"])

    with (
        psycopg.connect(migrated_database_urls["app"], row_factory=dict_row) as first_conn,
        psycopg.connect(migrated_database_urls["app"], row_factory=dict_row) as second_conn,
    ):
        set_current_user(first_conn, user_id)
        set_current_user(second_conn, user_id)

        first_store = ContinuityStore(first_conn)
        second_store = ContinuityStore(second_conn)
        first_event = first_store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "first"},
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                second_store.append_event,
                thread["id"],
                session["id"],
                "message.assistant",
                {"text": "second"},
            )

            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)

            first_conn.commit()
            second_event = future.result(timeout=5)

        second_conn.commit()

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        events = store.list_thread_events(thread["id"])

    assert [first_event["sequence_no"], second_event["sequence_no"]] == [1, 2]
    assert [event["sequence_no"] for event in events] == [1, 2]
