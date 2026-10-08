"""Stored labels, staged restore and crash recovery through real key readers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
from uuid import UUID

import pytest

from alicebot_api import sqlite_schema, vnext_label_repair as repair
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, main as onramp_main, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_derived_domain_fence import USER

SENTINEL = "Violet confidential recovery sentinel"


def old_vault(path, monkeypatch, *, copies=1, cross_project=False, domain="health"):
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
        bootstrap_database(path, user_id=USER, user_email="fixture@example.invalid")
        with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            source = store.create_source(
                {
                    "source_type": "note",
                    "title": "Synthetic input",
                    "content_hash": "sha256:violet",
                    "domain": domain,
                    "sensitivity": "internal" if cross_project else "confidential",
                    "metadata_json": {} if cross_project else {"project_scope": ["beta"]},
                }
            )
            ids = []
            for index in range(copies):
                row = store.create_memory(
                    {
                        "memory_key": f"copy.{index}",
                        "canonical_text": SENTINEL,
                        "status": "active",
                        "domain": "project" if cross_project else "unknown",
                        "sensitivity": "public",
                        "project_id": "alpha",
                        "metadata_json": {"source_id": source["id"], "project_scope": ["alpha"]},
                    }
                )
                ids.append(str(row["id"]))
            if cross_project:
                a = store.create_memory(
                    {
                        "memory_key": "alpha.input",
                        "canonical_text": "Violet alpha input",
                        "status": "active",
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": {"project_scope": ["alpha"]},
                    }
                )
                b = store.create_memory(
                    {
                        "memory_key": "beta.input",
                        "canonical_text": "Violet beta input",
                        "status": "active",
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": {"project_scope": ["beta"]},
                    }
                )
                for key, marker in (
                    ("rollup", {"rollup": {"member_ids": [a["id"], b["id"]]}}),
                    ("consolidation", {"consolidation": {"cluster_member_ids": [a["id"], b["id"]]}}),
                ):
                    row = store.create_memory(
                        {
                            "memory_key": key,
                            "canonical_text": SENTINEL,
                            "status": "active",
                            "domain": "project",
                            "sensitivity": "internal",
                            "value": {"rollup": {"member_ids": [a["id"], b["id"]]}} if key == "rollup" else {},
                            "metadata_json": {
                                **marker,
                                "candidate_kind": "memory_rollup" if key == "rollup" else "memory_consolidation",
                                "project_scope": ["alpha", "beta"],
                            },
                        }
                    )
                    ids.append(str(row["id"]))
    return ids


def export_old(path, backup, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
        assert onramp_main(["export", "--db", str(path), "--user-id", USER, "--out", str(backup)]) == 0


def read_stored_columns(path, ids):
    with sqlite3.connect(path) as conn:
        rows = [
            conn.execute(
                "SELECT domain, sensitivity, project_id, metadata_json FROM memories WHERE id=?", (row_id,)
            ).fetchone()
            for row_id in ids
        ]
        events = conn.execute("SELECT payload_json FROM event_log WHERE event_type='memory.labels_raised'").fetchall()
        state = conn.execute("SELECT value FROM alice_schema_state WHERE key=?", (repair.REPAIR_STATE_KEY,)).fetchone()
    return [(row[0], row[1], row[2], json.loads(row[3])) for row in rows], events, state


def restricted_reads(path, ids, monkeypatch, *, guard_off=False, project=None):
    with sqlite_user_connection(path, USER) as conn:
        _, raw = create_agent_key(
            SQLiteVNextStore(conn, USER),
            user_id=USER,
            agent_id="recovery-reader",
            permission_profile="read_only_agent",
            project_scope=project,
        )
    with monkeypatch.context() as patch:
        patch.setenv("ALICE_AGENT_API_KEY", raw)
        patch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        patch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        if guard_off:
            patch.setattr(LabelGuard, "effective_row", lambda self, kind, row: row)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
        for name, args in (("alice_recall", {"query": "Violet"}), ("alice_context_pack", {"query": "Violet"})):
            result = call_mcp_tool(context, name=name, arguments=args)
            assert SENTINEL not in json.dumps(result, default=str), (name, guard_off, result)
            assert not any(row_id in json.dumps(result, default=str) for row_id in ids)
        for row_id in ids:
            with pytest.raises(MCPToolError, match="requested explanation is unavailable"):
                call_mcp_tool(context, name="alice_explain", arguments={"memory_id": row_id})


@pytest.mark.parametrize("upgraded", (False, True), ids=("fresh", "already_v3"))
def test_old_backup_is_repaired_before_publication(tmp_path, monkeypatch, upgraded):
    old, target, backup = tmp_path / "old.db", tmp_path / "target.db", tmp_path / "old.jsonl"
    ids = old_vault(old, monkeypatch)
    export_old(old, backup, monkeypatch)
    assert read_stored_columns(old, ids)[0][0][1] == "public"
    if upgraded:
        bootstrap_database(target, user_id=USER, user_email="fixture@example.invalid")
        assert read_stored_columns(target, [])[2] is not None
    assert onramp_main(["import", "--db", str(target), "--user-id", USER, "--in", str(backup)]) == 0
    rows, events, state = read_stored_columns(target, ids)
    assert rows[0][:3] == ("health", "confidential", None)
    assert rows[0][3]["project_scope"] == []
    assert rows[0][3]["project_floor"] == ["beta"]
    assert len(events) == 1 and state is not None
    assert SENTINEL not in json.dumps(events)
    restricted_reads(target, ids, monkeypatch, guard_off=True)
    restricted_reads(target, ids, monkeypatch)


def test_old_backup_with_cross_project_aggregates(tmp_path, monkeypatch):
    old, target, backup = tmp_path / "old.db", tmp_path / "target.db", tmp_path / "old.jsonl"
    ids = old_vault(old, monkeypatch, cross_project=True, domain="project")
    export_old(old, backup, monkeypatch)
    assert onramp_main(["import", "--db", str(target), "--user-id", USER, "--in", str(backup)]) == 0
    rows, events, _ = read_stored_columns(target, ids)
    assert all(row[2] is None and row[3]["project_scope"] == [] for row in rows)
    assert rows[0][3]["project_floor"] == []
    assert all(row[3]["project_floor"] == ["alpha", "beta"] for row in rows[1:])
    assert len(events) == 3
    restricted_reads(target, ids, monkeypatch, guard_off=True, project="alpha")
    restricted_reads(target, ids, monkeypatch, project="alpha")


def test_explicit_repair_ignores_completion_stamp(tmp_path, monkeypatch, capsys):
    path = tmp_path / "stamped.db"
    ids = old_vault(path, monkeypatch, domain="project")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO alice_schema_state VALUES (?, '1')", (repair.REPAIR_STATE_KEY,))
    assert onramp_main(["labels", "repair", "--db", str(path), "--user-id", USER]) == 0
    assert "labels repair updated 1" in capsys.readouterr().out
    rows, events, state = read_stored_columns(path, ids)
    assert rows[0][1] == "confidential", "explicit repair must revisit a stamped vault"
    assert len(events) == 1
    assert onramp_main(["labels", "repair", "--db", str(path), "--user-id", USER]) == 0
    assert "labels repair updated 0" in capsys.readouterr().out
    assert read_stored_columns(path, ids) == (rows, events, state)
    restricted_reads(path, ids, monkeypatch, guard_off=True)
    restricted_reads(path, ids, monkeypatch)


def test_physical_copy_is_repaired_on_first_open(tmp_path, monkeypatch):
    old, target = tmp_path / "old.db", tmp_path / "copied.db"
    ids = old_vault(old, monkeypatch, domain="project")
    shutil.copy2(old, target)
    assert read_stored_columns(target, ids)[0][0][1] == "public"
    with sqlite_user_connection(target, USER):
        pass
    assert read_stored_columns(target, ids)[0][0][1] == "confidential"
    restricted_reads(target, ids, monkeypatch, guard_off=True)
    restricted_reads(target, ids, monkeypatch)


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("memories", "sources", "event_log", "alice_schema_state")
        }


def assert_recovered(path, ids, monkeypatch):
    with sqlite_user_connection(path, USER):
        pass
    rows, _, state = read_stored_columns(path, ids)
    assert all(row[1] == "confidential" for row in rows) and state is not None
    restricted_reads(path, ids, monkeypatch, guard_off=True)
    restricted_reads(path, ids, monkeypatch)


def test_interrupted_sqlite_repair_rolls_back_and_backup_repeats(tmp_path, monkeypatch):
    path, backup = tmp_path / "interrupted.db", tmp_path / "preupgrade.db"
    ids = old_vault(path, monkeypatch, copies=3, domain="project")
    shutil.copy2(path, backup)
    before = snapshot(path)
    original = repair.require_changed
    calls = []

    def fail_after_writes(*args):
        original(*args)
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("injected after second label write")

    with sqlite3.connect(path) as conn, monkeypatch.context() as patch:
        patch.setattr(repair, "require_changed", fail_after_writes)
        with pytest.raises(RuntimeError, match="after second"):
            repair.relabel_labels_sqlite(conn, restoring=True)
    assert len(calls) == 2 and snapshot(path) == before
    assert_recovered(path, ids, monkeypatch)
    repeated = tmp_path / "restored_preupgrade.db"
    shutil.copy2(backup, repeated)
    assert snapshot(repeated) == before
    assert_recovered(repeated, ids, monkeypatch)


def test_killed_sqlite_repair_rolls_back_and_backup_repeats(tmp_path, monkeypatch):
    path, backup = tmp_path / "killed.db", tmp_path / "preupgrade.db"
    ids = old_vault(path, monkeypatch, copies=3, domain="project")
    shutil.copy2(path, backup)
    before = snapshot(path)
    program = """
import sqlite3, sys, time
from alicebot_api import vnext_label_repair as repair
original = repair.require_changed
calls = 0
def pause(*args):
    global calls
    original(*args)
    calls += 1
    if calls == 2:
        print('second write reached', flush=True)
        time.sleep(30)
repair.require_changed = pause
with sqlite3.connect(sys.argv[1]) as conn:
    repair.relabel_labels_sqlite(conn, restoring=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0], "child never reached its writes"
        assert process.stdout.readline().strip() == "second write reached"
        process.kill()
        process.wait(timeout=10)
        assert snapshot(path) == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert_recovered(path, ids, monkeypatch)
    repeated = tmp_path / "restored_preupgrade.db"
    shutil.copy2(backup, repeated)
    assert snapshot(repeated) == before
    assert_recovered(repeated, ids, monkeypatch)


def test_repeating_skip_composes_v2_v3_legacy_scope_and_open_loop(tmp_path, monkeypatch):
    old, target, backup = tmp_path / "old.db", tmp_path / "target.db", tmp_path / "old.jsonl"
    ids = old_vault(old, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
        with without_insert_floor(), sqlite_user_connection(old, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]
            legacy = store.create_memory(
                {
                    "memory_key": "legacy.project",
                    "canonical_text": SENTINEL,
                    "domain": "unknown",
                    "sensitivity": "public",
                    "status": "active",
                    "project_id": "alpha",
                    "metadata_json": {"source_id": source_id},
                }
            )
            loop = store.create_open_loop(
                {
                    "title": SENTINEL,
                    "domain": "unknown",
                    "sensitivity": "public",
                    "source_id": source_id,
                    "metadata_json": {"source_id": source_id, "discovered_by": "vnext_daily_brief"},
                }
            )
            ids.append(str(legacy["id"]))
            v2_input = store.create_memory(
                {
                    "memory_key": "v2.input",
                    "canonical_text": "Violet private original",
                    "domain": "health",
                    "sensitivity": "confidential",
                    "status": "active",
                }
            )
            aggregate = store.create_memory(
                {
                    "memory_key": "v2.aggregate",
                    "canonical_text": SENTINEL,
                    "domain": "unknown",
                    "sensitivity": "public",
                    "status": "active",
                    "metadata_json": {"consolidation": {"cluster_member_ids": [v2_input["id"]]}},
                }
            )
            ids.append(str(aggregate["id"]))
    export_old(old, backup, monkeypatch)
    argv = ["import", "--db", str(target), "--user-id", USER, "--in", str(backup)]
    assert onramp_main(argv) == 0
    before = snapshot(target)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT domain,sensitivity FROM open_loops WHERE id=?", (loop["id"],)).fetchone() == (
            "health",
            "confidential",
        )
        assert (
            conn.execute("SELECT count(*) FROM event_log WHERE event_type='memory.domain_relabelled'").fetchone()[0]
            >= 1
        )
    assert onramp_main([*argv, "--mode", "skip"]) == 0
    assert snapshot(target) == before, "repeat skip must change no stored row, event, or stamp"
    restricted_reads(target, ids, monkeypatch, guard_off=True)
    restricted_reads(target, ids, monkeypatch)


def test_repeating_skip_accepts_a_later_supersede_label_raise(tmp_path, monkeypatch):
    old, target, backup = tmp_path / "old.db", tmp_path / "target.db", tmp_path / "memory_backup.jsonl"
    ids = old_vault(old, monkeypatch, domain="project")
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
        with sqlite_user_connection(old, USER) as conn:
            source = dict(conn.execute("SELECT * FROM sources").fetchone())
            source["source_type"], source["connector_name"], source["raw_path"] = (
                "markdown",
                "markdown_folder",
                "fixture.md",
            )
            conn.execute(
                "UPDATE sources SET source_type=?, connector_name=?, raw_path=? WHERE id=?",
                ("markdown", "markdown_folder", "fixture.md", source["id"]),
            )
    export_old(old, backup, monkeypatch)
    # Old unversioned memory-only backups are valid. The source already lives
    # in this destination and is intentionally outside this incremental file.
    records = [json.loads(line) for line in backup.read_text().splitlines()]
    backup.write_text("\n".join(json.dumps(row) for row in records if row["record_type"] == "memory") + "\n")
    bootstrap_database(target, user_id=USER, user_email="fixture@example.invalid")
    with sqlite_user_connection(target, USER) as conn:
        source["metadata_json"] = json.loads(source["metadata_json"])
        SQLiteVNextStore(conn, USER).create_source(source)
    argv = ["import", "--db", str(target), "--user-id", USER, "--in", str(backup)]
    assert onramp_main(argv) == 0
    with sqlite_user_connection(target, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        replacement = store.create_source(
            {**source, "id": None, "dedupe_key": None, "content_hash": "sha256:replacement", "sensitivity": "regulated"}
        )
        store.supersede_source(str(source["id"]), superseded_by=str(replacement["id"]))
        assert store.get_memory(ids[0])["sensitivity"] == "regulated"
        payloads = [
            json.loads(row["payload_json"])
            for row in conn.execute(
                "SELECT payload_json FROM event_log WHERE event_type='memory.labels_raised' AND target_id=?", (ids[0],)
            )
        ]
        assert any(row["cause"] == "input_relabelled" for row in payloads)
    with sqlite_user_connection(target, USER):
        pass
    before = snapshot(target)
    assert onramp_main([*argv, "--mode", "skip"]) == 0
    assert snapshot(target) == before
    restricted_reads(target, ids, monkeypatch, guard_off=True)
    restricted_reads(target, ids, monkeypatch)


def test_unrepairable_open_keeps_guarded_read_available(tmp_path, monkeypatch, caplog):
    path = tmp_path / "unrepairable.db"
    ids = old_vault(path, monkeypatch, domain="project")

    def fail(conn, **kwargs):
        raise repair.DerivedDomainRepairError("synthetic cycle did not settle")

    monkeypatch.setattr(repair, "relabel_labels_sqlite", fail)
    with sqlite_user_connection(path, USER):
        pass
    assert read_stored_columns(path, ids)[2] is None
    assert "alice-memory: label repair did not run" in caplog.text
    restricted_reads(path, ids, monkeypatch)
