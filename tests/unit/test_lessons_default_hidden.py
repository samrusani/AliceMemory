"""The brief keeps its existing facts and does not count inaccessible lessons."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_lessons_read import LESSON, Vault


def _brief(vault, key):
    env = {name: value for name, value in os.environ.items() if not name.startswith("ALICE")}
    env["PYTHONPATH"] = os.pathsep.join((str(Path.cwd() / "apps/api/src"), str(Path.cwd())))
    env["ALICE_AGENT_API_KEY"] = key
    return subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault.path), "--user-id", str(vault.user)],
        env=env, text=True, capture_output=True, check=True,
    ).stdout


def _insert_rows(vault, kind, *, count=300):
    value = json.dumps({"text": "planted ordinary fact", "lesson": LESSON})
    rows, revisions = [], []
    for number in range(count):
        mid, key = str(uuid4()), f"hidden.{kind}.{number}"
        project = "beta" if kind == "other_project" else "alpha"
        status = {"waiting": "candidate", "rejected": "rejected", "superseded": "superseded",
                  "forgotten": "archived", "redacted": "archived"}.get(kind, "active")
        text = "[REDACTED]" if kind == "redacted" else "planted ordinary fact"
        stored = json.dumps({"text": "[REDACTED]"}) if kind == "redacted" else value
        rows.append((mid, str(vault.user), key, stored, status, "[]", "procedure", text, text,
                     "project", "confidential" if kind == "ceiling" else "internal",
                     json.dumps({"project_scope": [project]}), project,
                     "2000-01-01T00:00:00Z" if kind == "expired" else None))
        if kind in {"other_project", "ceiling", "expired", "superseded", "forgotten"}:
            revisions.append((str(uuid4()), str(vault.user), mid, 1, "agentic_memory_commit", key,
                              value, "[]", "{}", 1, "created", text, "user"))
        elif kind == "planted":
            revisions.append((str(uuid4()), str(vault.user), mid, 1, "agentic_memory_commit", key,
                              json.dumps({"text": text}), "[]", "{}", 1, "created", text, "user"))
    with sqlite3.connect(vault.path) as conn:
        conn.executemany(
            "INSERT INTO memories (id,user_id,memory_key,value,status,source_event_ids,memory_type,title,canonical_text,domain,sensitivity,metadata_json,project_id,valid_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO memory_revisions (id,user_id,memory_id,sequence_no,action,memory_key,new_value,source_event_ids,candidate,revision_number,revision_type,text_after,actor_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            revisions,
        )


@pytest.mark.parametrize("kind", ["other_project", "ceiling", "waiting", "rejected", "superseded", "expired", "forgotten", "redacted"])
@pytest.mark.parametrize("published", [False, True])
def test_hidden_rows_leave_the_whole_keyed_brief_unchanged(tmp_path, monkeypatch, kind, published):
    """Guard: admit the named hidden row kind and the whole brief changes."""
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("admin", "admin_agent", "alpha")
    key = vault.key("reader", "read_only_agent", "alpha")
    if published:
        result = vault.commit(monkeypatch, admin, "admin", lesson={**LESSON, "applies_when": "rebasing a published branch"})
        assert result["status"] == "committed"
        with sqlite3.connect(vault.path) as conn:
            conn.execute("UPDATE memories SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (result["memory"]["id"],))
    before = _brief(vault, key)
    _insert_rows(vault, kind)
    assert _brief(vault, key) == before


@pytest.mark.parametrize("published", [False, True])
def test_planted_blocks_do_not_change_facts_or_the_lesson_line(tmp_path, monkeypatch, published):
    """Guard: count an active block without its publishing act as a lesson."""
    vault = Vault(tmp_path / "vault.db")
    admin = vault.key("admin", "admin_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    _insert_rows(vault, "planted")
    with sqlite3.connect(vault.path) as conn:
        conn.execute("UPDATE memories SET value = json_remove(value, '$.lesson')")
    if published:
        result = vault.commit(monkeypatch, admin, "admin", lesson={**LESSON, "applies_when": "rebasing a published branch"})
        assert result["status"] == "committed"
        with sqlite3.connect(vault.path) as conn:
            conn.execute("UPDATE memories SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (result["memory"]["id"],))
    before = _brief(vault, reader)
    if published:
        assert "Shared lessons: 1. Call alice_lessons with your task before you start." in before
    else:
        assert "Shared lessons" not in before
    recalled_before = vault.call(monkeypatch, reader, "alice_recall", {"query": "planted ordinary fact"})
    before_ids = {row["id"] for row in recalled_before["results"]}
    assert before_ids
    with sqlite3.connect(vault.path) as conn:
        conn.execute("UPDATE memories SET value = json_set(value, '$.lesson', json(?)) WHERE memory_key LIKE 'hidden.planted.%'", (json.dumps(LESSON),))
    after = _brief(vault, reader)
    assert after == before
    recalled_after = vault.call(monkeypatch, reader, "alice_recall", {"query": "planted ordinary fact"})
    assert {row["id"] for row in recalled_after["results"]} == before_ids
    answer = vault.call(monkeypatch, reader, "alice_lessons", {"task": "rebasing", "limit": 10})
    assert answer["count"] == (1 if published else 0)
