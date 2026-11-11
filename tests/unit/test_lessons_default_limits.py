"""The brief's bounded lesson scan and conservative publication prefilter."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import alicebot_api.mcp.lessons as lessons
from alicebot_api.onramp import main as onramp_main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_stores.sqlite.lessons import stream_lesson_memories
from tests.unit.test_lessons_read import LESSON, Vault
from tests.unit.test_lessons_default_hidden import _brief, _insert_rows


def _insert_candidates(vault, count, *, valid_first=0, valid_tail=False):
    rows, revisions = [], []
    current = {"text": "rebasing a feature branch", "lesson": LESSON}
    for number in range(count):
        mid, key = str(uuid4()), f"candidate.{number}"
        valid = number >= count - valid_first or (valid_tail and number == 0)
        prior = current if valid else {"text": "rebasing", "lesson": {**LESSON, "do_instead": "A different check."}}
        timestamp = f"2026-01-01T00:00:{number // 1000:02d}.{number % 1000:03d}Z"
        rows.append((mid, str(vault.user), key, json.dumps(current), "active", "[]", "procedure", "rebasing",
                     "rebasing a feature branch", "project", "internal", json.dumps({"project_scope": ["alpha"]}), "alpha", timestamp))
        revisions.append((str(uuid4()), str(vault.user), mid, 1, "agentic_memory_commit", key,
                          json.dumps(prior), "[]", "{}", 1, "created", "rebasing", "user"))
    with sqlite3.connect(vault.path) as conn:
        conn.executemany(
            "INSERT INTO memories (id,user_id,memory_key,value,status,source_event_ids,memory_type,title,canonical_text,domain,sensitivity,metadata_json,project_id,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        conn.executemany(
            "INSERT INTO memory_revisions (id,user_id,memory_id,sequence_no,action,memory_key,new_value,source_event_ids,candidate,revision_number,revision_type,text_after,actor_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", revisions,
        )


@pytest.mark.parametrize("count", [300, 5000])
def test_one_brief_reads_at_most_256_candidates(tmp_path, monkeypatch, capsys, count):
    """Guard: raise the 256-candidate limit to 100000 and the exact-check count exceeds 256."""
    vault = Vault(tmp_path / "vault.db")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_candidates(vault, count, valid_tail=True)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    checked, revision_ids = [], []
    original_check = lessons._publication
    original_revisions = SQLiteVNextStore.stream_lesson_revisions

    def check(row, revisions):
        checked.append(str(row["id"]))
        return original_check(row, revisions)

    def revisions(store, ids):
        revision_ids.extend(ids)
        return original_revisions(store, ids)

    monkeypatch.setattr(lessons, "_publication", check)
    monkeypatch.setattr(SQLiteVNextStore, "stream_lesson_revisions", revisions)
    assert onramp_main(["brief", "--db", str(vault.path), "--user-id", str(vault.user)]) == 0
    text = capsys.readouterr().out
    assert "Shared lessons may apply. Call alice_lessons with your task before you start." in text
    assert len(checked) == len(revision_ids) == 256


@pytest.mark.parametrize("valid,opening", [
    (0, "Shared lessons may apply."),
    (1, "Shared lessons: at least 1."),
    (20, "Shared lessons: at least 20."),
    (21, "Shared lessons: more than 20."),
])
def test_limited_scan_reports_only_what_it_established(tmp_path, monkeypatch, capsys, valid, opening):
    """Guard: drop the may-apply sentence and the zero-result case changes."""
    vault = Vault(tmp_path / "vault.db")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_candidates(vault, 300, valid_first=valid, valid_tail=True)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    assert onramp_main(["brief", "--db", str(vault.path), "--user-id", str(vault.user)]) == 0
    assert f"{opening} Call alice_lessons with your task before you start." in capsys.readouterr().out
    result = vault.call(monkeypatch, reader, "alice_lessons", {"task": "rebasing", "limit": 10})
    assert result["count"] == min(valid + 1, 10)


def test_exactly_256_exhausted_candidates_do_not_imply_a_lesson(tmp_path, monkeypatch, capsys):
    """Guard: mark an exhausted scan incomplete at 256 and a false notice appears."""
    vault = Vault(tmp_path / "vault.db")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_candidates(vault, 256)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    assert onramp_main(["brief", "--db", str(vault.path), "--user-id", str(vault.user)]) == 0
    assert "Shared lessons" not in capsys.readouterr().out


def test_sql_prefilter_avoids_revision_reads_for_planted_blocks(tmp_path, monkeypatch, capsys):
    """Guard: remove the SQL EXISTS prefilter and planted blocks cause revision reads."""
    vault = Vault(tmp_path / "vault.db")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_rows(vault, "planted")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    seen = []
    original = SQLiteVNextStore.stream_lesson_revisions

    def revisions(store, ids):
        seen.extend(ids)
        return original(store, ids)

    monkeypatch.setattr(SQLiteVNextStore, "stream_lesson_revisions", revisions)
    assert onramp_main(["brief", "--db", str(vault.path), "--user-id", str(vault.user)]) == 0
    assert "Shared lessons" not in capsys.readouterr().out
    assert seen == []


def test_import_preserves_candidates_that_fail_the_exact_check(tmp_path, monkeypatch):
    """Guard: ignore the revision's lesson value and imported mismatches become published."""
    source = Vault(tmp_path / "source.db")
    _insert_candidates(source, 2)
    target, exported = tmp_path / "restored.db", tmp_path / "backup.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE")}
    env["PYTHONPATH"] = os.pathsep.join((str(Path.cwd() / "apps/api/src"), str(Path.cwd())))
    command = [sys.executable, "-m", "alicebot_api.onramp"]
    subprocess.run([*command, "export", "--db", str(source.path), "--user-id", str(source.user), "--out", str(exported)],
                   env=env, check=True, capture_output=True, text=True)
    subprocess.run([*command, "import", "--db", str(target), "--user-id", str(source.user), "--in", str(exported)],
                   env=env, check=True, capture_output=True, text=True)
    restored = Vault.__new__(Vault)
    restored.path, restored.user = target, source.user
    with sqlite_user_connection(target, source.user) as conn:
        store = SQLiteVNextStore(conn, source.user)
        candidates = list(stream_lesson_memories(store, domains=[], sensitivity=list(ALL_SENSITIVITY), projects=(), lazy_metadata=True))
    assert len(candidates) == 2
    assert restored.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["count"] == 0


def test_sql_prefilter_keeps_every_publishing_shape_and_ignores_key_order(tmp_path, monkeypatch):
    """Guard: remove memory_review_promote from the SQL prefilter and a tool result is missing."""
    vault = Vault(tmp_path / "vault.db")
    shapes = [("agentic_memory_commit", "created"), ("agentic_memory_confirm_confirm", "promoted"),
              ("memory_correct_approve", "promoted"), ("memory_review_accept", "promoted"),
              ("memory_review_promote", "promoted")]
    _insert_candidates(vault, len(shapes), valid_first=len(shapes))
    with sqlite3.connect(vault.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT memory_id FROM memory_revisions ORDER BY memory_key").fetchall()
        conn.execute("DROP TRIGGER IF EXISTS memory_revisions_append_only_update")
        for index, (row, (action, revision_type)) in enumerate(zip(rows, shapes, strict=True)):
            reversed_block = dict(reversed(list(LESSON.items())))
            stored_block = json.dumps(reversed_block) if index == 0 else reversed_block
            conn.execute("UPDATE memory_revisions SET action=?, revision_type=?, new_value=? WHERE memory_id=?",
                         (action, revision_type, json.dumps({"lesson": stored_block}), row["memory_id"]))
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        candidates = list(stream_lesson_memories(store, domains=[], sensitivity=list(ALL_SENSITIVITY), projects=(), lazy_metadata=True))
    result = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing", "limit": 10})
    assert result["count"] == len(shapes)
    assert {str(row["id"]) for row in candidates} == {str(row["id"]) for row in result["lessons"]}


@pytest.mark.parametrize("kind", ["other_project", "ceiling", "waiting", "rejected", "superseded", "expired", "forgotten", "redacted"])
@pytest.mark.parametrize("valid", [0, 1])
def test_hidden_rows_do_not_change_the_notice_at_the_limit(tmp_path, kind, valid):
    """Guard: count the named hidden kind before the read fence and the notice changes."""
    vault = Vault(tmp_path / "vault.db")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_candidates(vault, 300, valid_first=valid, valid_tail=True)
    before = _brief(vault, reader)
    assert ("Shared lessons may apply." if valid == 0 else "Shared lessons: at least 1.") in before
    _insert_rows(vault, kind, count=5000)
    assert _brief(vault, reader) == before


def test_unreadable_derived_candidates_do_not_use_the_limit(tmp_path, monkeypatch):
    """Guard: charge the 256 limit before the label fence and the readable lesson is missed."""
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("admin", "admin_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    visible = vault.commit(monkeypatch, admin, "admin")["memory"]["id"]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        source = store.create_source({"source_type": "note", "title": "Restricted input", "domain": "project",
                                      "sensitivity": "confidential", "content_hash": "sha256:hidden-input",
                                      "metadata_json": {"project_scope": ["alpha"]}})
    with sqlite3.connect(vault.path) as conn:
        conn.row_factory = sqlite3.Row
        memory = dict(conn.execute("SELECT * FROM memories WHERE id=?", (visible,)).fetchone())
        revision = dict(conn.execute("SELECT * FROM memory_revisions WHERE memory_id=?", (visible,)).fetchone())
        columns, rcolumns = list(memory), list(revision)
        insert_memory = "INSERT INTO memories (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
        insert_revision = "INSERT INTO memory_revisions (" + ",".join(rcolumns) + ") VALUES (" + ",".join("?" for _ in rcolumns) + ")"
        for number in range(300):
            row, rev = dict(memory), dict(revision)
            row["id"], row["memory_key"] = str(uuid4()), f"derived.{number}"
            row["commit_digest"] = None
            row["updated_at"] = "2099-01-01T00:00:00Z"
            metadata = json.loads(row["metadata_json"])
            metadata["derived_from"] = {"sources": [str(source["id"])]}
            row["metadata_json"] = json.dumps(metadata)
            rev["id"], rev["memory_id"], rev["memory_key"] = str(uuid4()), row["id"], row["memory_key"]
            conn.execute(insert_memory, tuple(row[name] for name in columns))
            conn.execute(insert_revision, tuple(rev[name] for name in rcolumns))
    result = _brief(vault, reader)
    assert "Shared lessons: 1. Call alice_lessons with your task before you start." in result
