"""Exercise repair rollback through the actual SQLite open wrapper."""
import json
import sqlite3

from alicebot_api import vnext_label_repair as repair
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_label_repair import REPAIR_STATE_KEY, plan_label_repairs
from tests.unit.test_sqlite_label_repair_v3 import _vault_with_a_public_copy, USER


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return (conn.execute("SELECT id,domain,sensitivity,metadata_json FROM memories ORDER BY id").fetchall(),
                conn.execute("SELECT id FROM event_log WHERE event_type LIKE '%.labels_raised'").fetchall(),
                conn.execute("SELECT value FROM alice_schema_state WHERE key=?", (REPAIR_STATE_KEY,)).fetchall())


def test_failed_real_open_rolls_back_rows_events_and_stamp(tmp_path, monkeypatch):
    path = tmp_path / "vault.db"
    _vault_with_a_public_copy(path, monkeypatch)
    before = snapshot(path)
    original = repair.require_changed
    calls = 0

    def interrupt(changed, table, row_id):
        nonlocal calls
        original(changed, table, row_id)
        calls += 1
        raise RuntimeError("synthetic interruption after the first label update")

    with monkeypatch.context() as patch:
        patch.setattr(repair, "require_changed", interrupt)
        with sqlite_user_connection(path, USER):
            pass
    assert calls == 1
    assert snapshot(path) == before
    with sqlite_user_connection(path, USER):
        pass
    rows, events, stamp = snapshot(path)
    assert rows[0][2] == "confidential"
    assert len(events) == 1
    assert stamp


def test_unverified_rows_are_not_in_the_repair_plan():
    rows = [{"id": "dangling", "user_id": "user", "domain": "project", "sensitivity": "public",
             "metadata_json": {"source_id": "abcdef01-2345-6789-abcd-ef0123456789"}},
            {"id": "incomplete", "user_id": "user", "domain": "project", "sensitivity": "public",
             "metadata_json": {"candidate_kind": "memory_rollup"}}]
    assert plan_label_repairs({"memories": rows}) == []


def test_malformed_marker_restore_succeeds_without_repair(tmp_path, monkeypatch, capsys):
    from alicebot_api.onramp import main

    path = tmp_path / "vault.db"
    memory_id = _vault_with_a_public_copy(path, monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE memories SET metadata_json=? WHERE id=?", (json.dumps({"candidate_kind": ["memory_rollup"]}), memory_id))
    exported = tmp_path / "backup.jsonl"
    assert main(["export", "--db", str(path), "--user-id", USER, "--out", str(exported)]) == 0
    target = tmp_path / "restored.db"
    assert main(["import", "--db", str(target), "--user-id", USER, "--in", str(exported)]) == 0, capsys.readouterr()
    with sqlite_user_connection(target, USER) as conn:
        tables = repair._load_tables(conn)
        assert plan_label_repairs(tables) == []
        assert repair.classify_stored_labels(tables)[1] == {"malformed_marker": [memory_id]}
