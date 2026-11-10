"""Reproduce how unrelated project rows affect SQLite recall ranking."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from pytest import MonkeyPatch
from tests.unit.test_lessons_read import LESSON, Vault


def main():
    with tempfile.TemporaryDirectory(prefix="recall-ranking-") as folder:
        base = Path(folder)
        patch = MonkeyPatch()
        try:
            patch.setenv("ALICE_PROJECT_DIR", folder)
            vault = Vault(base / "source.db")
            admin = vault.key("publisher", "admin_agent")
            reader = vault.key("reader", "project_scoped_agent", "alpha")
            for text in ("luminousmigration", "amberrebase"):
                vault.commit(
                    patch,
                    admin,
                    "publisher",
                    lesson={**LESSON, "applies_when": text},
                    project_scope=["alpha"],
                )
            args = {
                "query": "amberrebase luminousmigration",
                "agent_id": "reader",
                "include_sources": False,
                "limit": 10,
            }
            before = vault.call(patch, reader, "alice_recall", args)["results"]
            assert len(before) == 2
            target = base / "with-hidden.db"
            with sqlite3.connect(vault.path) as source, sqlite3.connect(target) as destination:
                source.backup(destination)
            with sqlite3.connect(target) as conn:
                conn.row_factory = sqlite3.Row
                memory = dict(conn.execute("select * from memories where id = ?", (before[0]["id"],)).fetchone())
                revision = dict(
                    conn.execute("select * from memory_revisions where memory_id = ?", (before[0]["id"],)).fetchone()
                )
                columns = list(memory)
                revision_columns = list(revision)
                memory_sql = (
                    "insert into memories (" + ",".join(columns) + ") values (" + ",".join("?" for _ in columns) + ")"
                )
                revision_sql = (
                    "insert into memory_revisions ("
                    + ",".join(revision_columns)
                    + ") values ("
                    + ",".join("?" for _ in revision_columns)
                    + ")"
                )
                for i in range(500):
                    row = dict(memory)
                    row["id"] = str(uuid4())
                    row["memory_key"] = f"hidden.{i}"
                    row["project_id"] = "beta"
                    metadata = json.loads(row["metadata_json"])
                    metadata["project_scope"] = ["beta"]
                    metadata["agentic_memory"]["project_scope"] = ["beta"]
                    row["metadata_json"] = json.dumps(metadata)
                    rev = dict(revision)
                    rev["id"] = str(uuid4())
                    rev["memory_id"] = row["id"]
                    rev["memory_key"] = row["memory_key"]
                    conn.execute(memory_sql, tuple(row[column] for column in columns))
                    conn.execute(revision_sql, tuple(rev[column] for column in revision_columns))
                conn.commit()
            other = Vault.__new__(Vault)
            other.path, other.user = target, vault.user
            after = other.call(patch, reader, "alice_recall", args)["results"]
            before_ids = [item["id"] for item in before]
            after_ids = [item["id"] for item in after]
            assert set(before_ids) == set(after_ids) and before_ids != after_ids
            print("same two visible memories; adding 500 hidden project rows reversed their recall order")
        finally:
            patch.undo()


if __name__ == "__main__":
    main()
