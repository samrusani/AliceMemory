"""SQLite redacts a reviewed copy after an undo and leaves an artifact event that holds only the memory id.

SQLite has no artifact table, so nothing on SQLite promotes an artifact and no route writes an event aimed at one. The
store keeps the same append-only trigger as PostgreSQL, though, and the same redaction statement, so these tests write
the event by hand and check that the two stores treat it alike: an event aimed at an artifact that holds only the id of
the memory is left as it is, and an event aimed at an artifact that holds anything more is not.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.mcp_tools import redact_memory_flow
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_stores.memory_lifecycle_common import is_redacted_memory


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.invalid", "Copy")
    yield SQLiteVNextStore(conn, user_id)
    conn.close()


def holders(conn: sqlite3.Connection, needle: str) -> set[str]:
    """Every table, search index included, that holds the text in any column of any row."""
    found = set()
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for row in conn.execute(f'SELECT * FROM "{table}"'):  # table names come from sqlite_master
            if needle in repr(row):
                found.add(table)
                break
    return found


def tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        repr(conn.execute(f"SELECT * FROM {table}").fetchall())
        for table in ("memories", "memory_revisions", "event_log", "provenance_links")
    )


def reviewed_copy(store: SQLiteVNextStore, text: str) -> dict:
    """A copy of a retired memory that a review made active, as a promotion makes one."""
    parent = store.create_memory(
        {
            "memory_key": f"parent.{uuid4()}", "canonical_text": "Parent", "status": "superseded", "domain": "project",
            "sensitivity": "public", "metadata_json": {"project_scope": []},
        }
    )
    return store.create_memory(
        {
            "memory_key": f"copy.{uuid4()}", "title": text, "canonical_text": text, "summary": text, "value": {"text": text},
            "status": "active", "memory_type": "semantic", "domain": "project", "sensitivity": "public",
            "metadata_json": with_derived_from({"promotion_reviewed": True, "project_scope": []}, {"memories": [parent]}),
        }
    )


def promotion_record(store: SQLiteVNextStore, memory_id: str, **extra: str) -> dict:
    return append_event(
        store,
        event_type="artifact.promoted_to_memory",
        actor_type="agent",
        target_type="artifact",
        target_id=str(uuid4()),
        payload={"memory_id": memory_id, **extra},
    )


def test_a_reviewed_copy_is_redacted_after_an_undo(store):
    sentinel, why = f"ZQXTEXT{uuid4().hex[:12]}", f"ZQXWHY{uuid4().hex[:12]}"
    memory = reviewed_copy(store, f"Copy note {sentinel}")
    VNextMemoryCommitService(store).undo(identity=None, memory_id=str(memory["id"]), reason=why)
    assert holders(store.conn, sentinel) and holders(store.conn, why)

    result = redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")

    assert result["status"] == "redacted"
    assert holders(store.conn, sentinel) == set()
    assert holders(store.conn, why) == set()
    assert is_redacted_memory(store.get_memory_for_redaction(str(memory["id"])))


def test_an_artifact_event_that_holds_only_the_memory_id_is_left_and_the_redaction_completes(store):
    sentinel = f"ZQXTEXT{uuid4().hex[:12]}"
    memory = reviewed_copy(store, f"Copy note {sentinel}")
    record = promotion_record(store, str(memory["id"]))
    VNextMemoryCommitService(store).undo(identity=None, memory_id=str(memory["id"]), reason="Undone.")

    first = redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")

    assert first["idempotent_replay"] is False
    assert holders(store.conn, sentinel) == set()
    cursor = store.conn.execute("SELECT * FROM event_log WHERE id = ?", (record["id"],))
    kept = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
    assert kept["payload_json"].replace(" ", "") == f'{{"memory_id":"{memory["id"]}"}}'
    assert kept["integrity_hash"] == record["integrity_hash"]
    # The replay check reads the left record as part of an exact bundle, or every replay would pass for a new redaction.
    assert store.memory_redaction_bundle_is_exact(str(memory["id"]), []) is True
    frozen = tables(store.conn)

    second = redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")

    assert second["idempotent_replay"] is True
    assert second["redacted_events"] == 0
    assert tables(store.conn) == frozen


def test_the_trigger_still_refuses_to_rewrite_the_artifact_event(store):
    memory = reviewed_copy(store, "Copy note")
    record = promotion_record(store, str(memory["id"]))
    store.conn.execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="event_log is append-only"):
            store.conn.execute(
                "UPDATE event_log SET payload_json = json_object('redacted', json('true'), 'memory_id', ?, "
                "'event_type', event_type), integrity_hash = NULL WHERE id = ?",
                (str(memory["id"]), record["id"]),
            )
    finally:
        store.conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")


def test_an_artifact_event_that_holds_more_than_the_id_is_not_left_behind(store):
    memory = reviewed_copy(store, "Copy note")
    note = f"ZQXNOTE{uuid4().hex[:12]}"
    promotion_record(store, str(memory["id"]), note=note)

    with pytest.raises(sqlite3.IntegrityError, match="event_log is append-only"):
        redact_memory_flow(store, memory_id=str(memory["id"]), reason="Operator erasure")

    assert "event_log" in holders(store.conn, note)
    assert not is_redacted_memory(store.get_memory_for_redaction(str(memory["id"])))
