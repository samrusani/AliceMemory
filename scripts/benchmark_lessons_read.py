"""Measure paired MCP reads on a synthetic SQLite vault."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def client_class():
    spec = importlib.util.spec_from_file_location("mcp_quickstart", ROOT / "docs/examples/mcp_quickstart.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MCPClient


def launch(db, env, user_id=None):
    command = [sys.executable, "-m", "alicebot_api.onramp", "mcp", "--db", str(db)]
    if user_id is not None:
        command.extend(("--user-id", str(user_id)))
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = client_class()(process)
    client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lesson-benchmark", "version": "1"},
        },
    )
    client.notify("notifications/initialized")
    return process, client


def populate(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        template = dict(conn.execute("select * from memories limit 1").fetchone())
        revision = dict(conn.execute("select * from memory_revisions limit 1").fetchone())
        metadata = json.loads(template["metadata_json"])
        metadata["project_scope"] = ["project-0"]
        metadata["agentic_memory"]["project_scope"] = ["project-0"]
        template["project_id"] = "project-0"
        template["metadata_json"] = json.dumps(metadata)
        conn.execute(
            "update memories set project_id=?, metadata_json=? where id=?",
            (template["project_id"], template["metadata_json"], template["id"]),
        )
        columns = list(template)
        revision_columns = list(revision)
        memory_sql = "insert into memories (" + ",".join(columns) + ") values (" + ",".join("?" for _ in columns) + ")"
        revision_sql = (
            "insert into memory_revisions ("
            + ",".join(revision_columns)
            + ") values ("
            + ",".join("?" for _ in revision_columns)
            + ")"
        )
        memory_rows = []
        revision_rows = []
        for i in range(9999):
            row = copy.copy(template)
            row["id"] = str(uuid4())
            row["memory_key"] = "bench." + str(i)
            row["project_id"] = "project-" + str(i % 5)
            meta = json.loads(row["metadata_json"])
            meta["project_scope"] = [row["project_id"]]
            meta["agentic_memory"]["project_scope"] = [row["project_id"]]
            row["metadata_json"] = json.dumps(meta)
            value = json.loads(row["value"])
            if i < 4999:
                value["lesson"]["what_happened"] = "A conflict hid a change on branch " + str(i)
                value["text"] = (
                    "Applies when: rebasing feature branch Do instead: Inspect both sides. What happened: A conflict hid a change on branch "
                    + str(i)
                )
                row["canonical_text"] = value["text"]
                row["summary"] = value["text"][:280]
            else:
                value.pop("lesson", None)
                value["text"] = "Plain procedure " + str(i)
                row["canonical_text"] = value["text"]
                row["title"] = value["text"]
                row["summary"] = value["text"]
            row["value"] = json.dumps(value)
            memory_rows.append(tuple(row[column] for column in columns))
            if i < 4999:
                rev = copy.copy(revision)
                rev["id"] = str(uuid4())
                rev["memory_id"] = row["id"]
                rev["memory_key"] = row["memory_key"]
                rev["new_value"] = row["value"]
                rev["text_after"] = row["canonical_text"]
                revision_rows.append(tuple(rev[column] for column in revision_columns))
        conn.executemany(memory_sql, memory_rows)
        conn.executemany(revision_sql, revision_rows)
        conn.commit()
        assert (
            conn.execute("select count(*) from memories where json_type(value, '$.lesson') = 'object'").fetchone()[0]
            == 5000
        )
        assert (
            conn.execute("select count(*) from memories where json_type(value, '$.lesson') is null").fetchone()[0]
            == 5000
        )
        assert conn.execute("select count(distinct project_id) from memories").fetchone()[0] == 5


def main():
    with tempfile.TemporaryDirectory(prefix="lesson-benchmark-") as folder:
        db = Path(folder) / "vault.db"
        env = dict(os.environ, ALICE_MCP_FULL_TOOLS="1", ALICE_PROJECT_DIR=folder)
        for key in (
            "ALICE_AGENT_API_KEY",
            "ALICE_EMBEDDINGS_BASE_URL",
            "ALICE_EMBEDDINGS_MODEL",
            "ALICE_EMBEDDINGS_API_KEY",
        ):
            env.pop(key, None)
        process, client = launch(db, env)
        try:
            result = client.call_tool(
                "alice_memory_commit",
                {
                    "lesson": {
                        "applies_when": "rebasing feature branch",
                        "what_happened": "A conflict hid a change.",
                        "do_instead": "Inspect both sides.",
                        "paths": ["src/*.py"],
                    }
                },
            )
            assert result["status"] == "committed"
        finally:
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=20)
        populate(db)
        process, client = launch(db, env)
        samples = {"lessons": [], "recall": []}
        calls = {
            "lessons": (
                "alice_lessons",
                {"task": "rebasing feature branch", "paths": ["src/a.py", "docs/a.md"], "limit": 5},
            ),
            "recall": ("alice_recall", {"query": "rebasing feature branch", "limit": 5, "include_sources": False}),
        }
        try:
            for i in range(41):
                for name in ("lessons", "recall") if i % 2 == 0 else ("recall", "lessons"):
                    tool, args = calls[name]
                    start = time.perf_counter()
                    result = client.call_tool(tool, args)
                    elapsed = time.perf_counter() - start
                    assert result["count"] == 5
                    if i:
                        samples[name].append(elapsed)
        finally:
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=20)
        p95 = {name: sorted(values)[37] for name, values in samples.items()}
        print(
            json.dumps(
                {
                    "samples": 40,
                    "p95_seconds": p95,
                    "median_seconds": {name: statistics.median(values) for name, values in samples.items()},
                },
                sort_keys=True,
            )
        )
        assert p95["lessons"] <= p95["recall"] + 0.2
        if "--compare-main" in sys.argv:
            source_dir = Path(folder) / "main"
            source_dir.mkdir()
            archive = Path(folder) / "main.tar"
            with archive.open("wb") as output:
                subprocess.run(["git", "archive", "origin/main"], cwd=ROOT, stdout=output, check=True)
            with tarfile.open(archive) as source:
                source.extractall(source_dir, filter="data")
            main_env = dict(env, PYTHONPATH=str(source_dir / "apps/api/src"))
            main_process, main_client = launch(db, main_env)
            head_process, head_client = launch(db, env)
            clients = {"main": main_client, "head": head_client}
            recall_samples: dict[str, list[float]] = {"main": [], "head": []}
            try:
                for i in range(41):
                    for name in ("main", "head") if i % 2 == 0 else ("head", "main"):
                        start = time.perf_counter()
                        result = clients[name].call_tool(*calls["recall"])
                        elapsed = time.perf_counter() - start
                        assert result["count"] == 5
                        if i:
                            recall_samples[name].append(elapsed)
            finally:
                for current_process in (main_process, head_process):
                    assert current_process.stdin is not None
                    current_process.stdin.close()
                    current_process.wait(timeout=20)
            medians = {name: statistics.median(values) for name, values in recall_samples.items()}
            print(json.dumps({"recall_samples": 40, "recall_median_seconds": medians}, sort_keys=True))
            assert medians["head"] <= medians["main"] * 1.05


if __name__ == "__main__":
    main()
