"""The SQLite v3 open pass raises a derived row to its inputs."""

from __future__ import annotations

import json
import logging

from alicebot_api import sqlite_schema
from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError
from alicebot_api.vnext_label_repair import REPAIR_STATE_KEY
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_derived_domain_fence import USER

SOURCE_TEXT = "A confidential observation"


def _vault_with_a_public_copy(path, monkeypatch) -> str:
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
        bootstrap_database(path, user_id=USER, user_email="local@alice")
        with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            source = store.create_source(
                {
                    "source_type": "note",
                    "title": "Clinic note",
                    "content_hash": "sha256:clinic",
                    "domain": "project",
                    "sensitivity": "confidential",
                    "metadata_json": {},
                }
            )
            memory = store.create_memory(
                {
                    "memory_key": "copy",
                    "canonical_text": SOURCE_TEXT,
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"source_id": source["id"]},
                }
            )
            return str(memory["id"])


def test_open_raises_a_public_copy_of_a_confidential_source(tmp_path, monkeypatch) -> None:
    path = tmp_path / "vault.db"
    memory_id = _vault_with_a_public_copy(path, monkeypatch)
    with sqlite_user_connection(path, USER) as conn:
        row = conn.execute("SELECT domain, sensitivity FROM memories WHERE id = ?", (memory_id,)).fetchone()
        event = conn.execute(
            "SELECT payload_json FROM event_log WHERE event_type = 'memory.labels_raised' AND target_id = ?",
            (memory_id,),
        ).fetchone()
    sensitivity = row["sensitivity"] if isinstance(row, dict) else row[1]
    raw_event = event["payload_json"] if isinstance(event, dict) else event[0]
    assert sensitivity == "confidential"
    payload = json.loads(raw_event)
    assert payload["cause"] == "repair_v3"
    assert "title" not in json.dumps(payload)
    assert SOURCE_TEXT not in json.dumps(payload)


def test_a_vault_that_cannot_be_repaired_still_opens(tmp_path, monkeypatch, caplog) -> None:
    path = tmp_path / "vault.db"
    _vault_with_a_public_copy(path, monkeypatch)
    with sqlite_user_connection(path, USER) as conn:
        conn.execute("DELETE FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,))

    def _fail(conn, *, restoring: bool = False) -> None:
        del conn, restoring
        raise DerivedDomainRepairError("cycle")

    monkeypatch.setattr("alicebot_api.vnext_label_repair.relabel_labels_sqlite", _fail)
    with caplog.at_level(logging.WARNING, logger="alicebot_api.sqlite_schema"):
        with sqlite_user_connection(path, USER) as conn:
            stamped = conn.execute(
                "SELECT value FROM alice_schema_state WHERE key = ?",
                (REPAIR_STATE_KEY,),
            ).fetchone()
            conn.execute("SELECT 1")
    assert stamped is None
    assert "alice-memory: label repair did not run" in caplog.text
