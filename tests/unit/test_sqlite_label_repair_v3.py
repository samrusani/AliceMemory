"""The SQLite v3 open pass raises a derived row to its inputs."""

from __future__ import annotations

import hashlib
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


def test_skip_accepts_a_recorded_project_scope_and_floor(tmp_path, monkeypatch, capsys) -> None:
    """A file that still has the scope and floor from before the repair is skipped, and the stored labels stay.

    Mutation: drop the project_scope and project_floor rewrite in ``_import_records``. The second import then
    refuses the row.
    """

    from alicebot_api.onramp import _export_line
    from alicebot_api.onramp import main as onramp_main

    path = tmp_path / "vault.db"
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
                    "metadata_json": {"project_scope": ["beta"]},
                }
            )
            memory = store.create_memory(
                {
                    "memory_key": "copy",
                    "canonical_text": SOURCE_TEXT,
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"source_id": source["id"], "project_scope": ["alpha"], "project_floor": []},
                }
            )
            memory_id = str(memory["id"])
    with sqlite_user_connection(path, USER) as conn:
        row = conn.execute("SELECT sensitivity, metadata_json FROM memories WHERE id = ?", (memory_id,)).fetchone()
        event = conn.execute(
            "SELECT payload_json FROM event_log WHERE event_type = 'memory.labels_raised' AND target_id = ?",
            (memory_id,),
        ).fetchone()
    stored_meta = json.loads(row["metadata_json"] if isinstance(row, dict) else row[1])
    payload = json.loads(event["payload_json"] if isinstance(event, dict) else event[0])
    previous = payload["previous"]
    assert stored_meta.get("project_floor") != previous.get("project_floor")
    export_path = tmp_path / "vault.jsonl"
    assert onramp_main(["export", "--db", str(path), "--user-id", USER, "--out", str(export_path)]) == 0
    digest = hashlib.sha256()
    lines = []
    for line in export_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        record_type = record.get("record_type")
        body = record.get("record")
        if record_type in {"export_header", "export_footer"}:
            lines.append((record_type, body))
            continue
        if record_type == "memory" and isinstance(body, dict) and body.get("id") == memory_id:
            body["sensitivity"] = previous["sensitivity"]
            metadata = body.get("metadata_json") or {}
            metadata["project_scope"] = previous["project_scope"]
            metadata["project_floor"] = previous["project_floor"]
            body["metadata_json"] = metadata
        canonical = _export_line(str(record_type), body) + "\n"
        digest.update(canonical.encode("utf-8"))
        lines.append((record_type, canonical))
    rewritten = []
    for record_type, body in lines:
        if record_type == "export_footer" and isinstance(body, dict):
            body["sha256"] = digest.hexdigest()
            rewritten.append(_export_line(record_type, body) + "\n")
        elif record_type == "export_header":
            rewritten.append(_export_line(record_type, body) + "\n")
        else:
            rewritten.append(body)
    export_path.write_text("".join(rewritten), encoding="utf-8")
    capsys.readouterr()
    code = onramp_main(["import", "--db", str(path), "--user-id", USER, "--in", str(export_path), "--mode", "skip"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    with sqlite_user_connection(path, USER) as conn:
        row = conn.execute("SELECT sensitivity, metadata_json FROM memories WHERE id = ?", (memory_id,)).fetchone()
    sensitivity = row["sensitivity"] if isinstance(row, dict) else row[0]
    metadata = json.loads(row["metadata_json"] if isinstance(row, dict) else row[1])
    assert sensitivity == "confidential"
    assert metadata.get("project_floor") == stored_meta.get("project_floor")
    assert metadata.get("project_scope") == stored_meta.get("project_scope")


def test_labels_check_names_the_rows_the_next_open_would_raise(tmp_path, monkeypatch, capsys) -> None:
    from alicebot_api.onramp import main as onramp_main

    path = tmp_path / "vault.db"
    _vault_with_a_public_copy(path, monkeypatch)
    code = onramp_main(["labels", "check", "--db", str(path), "--user-id", USER])
    output = capsys.readouterr().out
    assert code == 1
    assert "next open would raise" in output


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
