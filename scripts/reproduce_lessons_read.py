"""Reproduce lesson publication and restore behavior through the packaged commands."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="lesson-reproduction-") as folder:
        base = Path(folder)
        env = dict(os.environ, ALICE_MCP_FULL_TOOLS="1", ALICE_PROJECT_DIR=folder)
        for name in (
            "ALICE_AGENT_API_KEY",
            "ALICE_EMBEDDINGS_BASE_URL",
            "ALICE_EMBEDDINGS_MODEL",
            "ALICE_EMBEDDINGS_API_KEY",
        ):
            env.pop(name, None)
        cli = [sys.executable, "-m", "alicebot_api.onramp"]
        spec = importlib.util.spec_from_file_location("quickstart", ROOT / "docs/examples/mcp_quickstart.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def run(*args, ok=True):
            result = subprocess.run([*cli, *map(str, args)], cwd=ROOT, env=env, text=True, capture_output=True)
            if ok and result.returncode:
                raise RuntimeError(result.stderr)
            return result

        def key(db, agent, profile):
            return json.loads(
                run(
                    "keys",
                    "create",
                    "--db",
                    db,
                    "--agent-id",
                    agent,
                    "--profile",
                    profile,
                    "--project",
                    "alpha",
                    "--json",
                ).stdout
            )["raw_key"]

        def call(db, token, calls):
            process_env = dict(env)
            if token is not None:
                process_env["ALICE_AGENT_API_KEY"] = token
            process = subprocess.Popen(
                [*cli, "mcp", "--db", str(db)],
                cwd=ROOT,
                env=process_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                client = module.MCPClient(process)
                client.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lesson-reproduction", "version": "1"},
                    },
                )
                client.notify("notifications/initialized")
                return [
                    client.request("tools/call", {"name": name, "arguments": arguments}) for name, arguments in calls
                ]
            finally:
                assert process.stdin is not None
                process.stdin.close()
                process.wait(timeout=20)

        def decoded(result):
            if result.get("isError"):
                raise RuntimeError(result["content"][0]["text"])
            return json.loads(result["content"][0]["text"])

        db = base / "source.db"
        admin = key(db, "admin", "admin_agent")
        writer = key(db, "writer", "project_scoped_agent")
        reader = key(db, "reader", "project_scoped_agent")
        lesson = {
            "applies_when": "rebase my feature branch onto main",
            "what_happened": "A conflict hid the earlier change.",
            "do_instead": "Inspect both revisions before rebasing.",
            "paths": ["src/*.py"],
        }
        proposed = call(
            db, admin, [("alice_memory_commit", {"lesson": lesson, "agent_id": "admin", "agent_type": "coding_agent"})]
        )[0]
        if proposed.get("isError"):
            print("lesson commit unavailable:", json.loads(proposed["content"][0]["text"])["error"]["code"])
            return
        committed = decoded(proposed)
        waiting = decoded(
            call(
                db,
                writer,
                [
                    (
                        "alice_memory_commit",
                        {
                            "lesson": {**lesson, "applies_when": "rebase waiting branch"},
                            "agent_id": "writer",
                            "agent_type": "coding_agent",
                        },
                    )
                ],
            )[0]
        )
        held = decoded(
            call(
                db,
                admin,
                [
                    (
                        "alice_memory_commit",
                        {
                            "lesson": {**lesson, "applies_when": "rebase confirmation branch"},
                            "confidence": 0.7,
                            "agent_id": "admin",
                            "agent_type": "coding_agent",
                        },
                    )
                ],
            )[0]
        )
        identity = {"agent_id": "reader", "agent_type": "coding_agent"}
        answers = [
            decoded(item)
            for item in call(
                db,
                reader,
                [
                    ("alice_recall", {"query": "about to rebase my feature branch onto main", **identity}),
                    ("alice_recall", {"query": "astronomy xylophone octopus", **identity}),
                    ("alice_resume", identity),
                    ("alice_context_pack", {"query": "rebase waiting branch", **identity}),
                ],
            )
        ]
        assert (
            committed["status"] == "committed"
            and waiting["status"] == "review_required"
            and held["status"] == "confirmation_required"
        )
        assert answers[0]["count"] == 1 and answers[1]["count"] == 0
        assert sorted(answers[0]["results"][0]) == [
            "confidence",
            "domain",
            "id",
            "provenance_count",
            "score",
            "status",
            "text",
            "type",
            "writer",
        ]
        assert "rebase waiting branch" not in json.dumps(answers[0]["results"])
        assert "rebase confirmation branch" not in json.dumps(answers[3]["memories"])
        brief = run("brief", "--db", db).stdout
        assert "rebase waiting branch" not in brief and "rebase confirmation branch" not in brief
        call(
            db,
            admin,
            [
                (
                    "alice_memory_manage",
                    {
                        "action": "confirm",
                        "memory_id": held["memory"]["id"],
                        "confirmation_id": held["confirmation_id"],
                        "agent_id": "admin",
                    },
                ),
                (
                    "alice_memory_correct",
                    {"action": "approve", "review_item_id": waiting["memory"]["id"], "agent_id": "admin"},
                ),
            ],
        )
        with sqlite3.connect(db) as conn:
            revisions = conn.execute(
                "select memory_id, action, actor_type, actor_id, created_at from memory_revisions order by created_at"
            ).fetchall()
        actions = [row[1] for row in revisions]
        for action in (
            "agentic_memory_commit",
            "agentic_memory_review_required",
            "agentic_memory_confirmation_required",
            "agentic_memory_confirm_confirm",
            "memory_correct_approve",
        ):
            assert action in actions
        export = base / "full.jsonl"
        run("export", "--db", db, "--out", export)
        restored = base / "restored.db"
        run("import", "--db", restored, "--in", export)
        with sqlite3.connect(restored) as conn:
            assert (
                conn.execute(
                    "select memory_id, action, actor_type, actor_id, created_at from memory_revisions order by created_at"
                ).fetchall()
                == revisions
            )
        lines = export.read_text().splitlines()
        missing = base / "missing.jsonl"
        missing.write_text(
            "\n".join(line for line in lines if json.loads(line)["record_type"] != "memory_revision") + "\n"
        )
        refused = run("import", "--db", base / "refused.db", "--in", missing, ok=False)
        assert refused.returncode and "import_validation_failed" in refused.stderr
        legacy = base / "legacy.jsonl"
        legacy.write_text("\n".join(line for line in lines if json.loads(line)["record_type"] == "memory") + "\n")
        legacy_db = base / "legacy.db"
        run("import", "--db", legacy_db, "--in", legacy)
        with sqlite3.connect(legacy_db) as conn:
            assert (
                conn.execute(
                    "select count(*) from memories where status='active' and json_type(value,'$.lesson')='object'"
                ).fetchone()[0]
                == 3
            )
            assert conn.execute("select count(*) from memory_revisions").fetchone()[0] == 0
        shared_admin = key(db, "shared", "admin_agent")
        shared_limited = key(db, "shared", "project_scoped_agent")
        shared = decoded(
            call(
                db,
                shared_admin,
                [
                    (
                        "alice_memory_commit",
                        {
                            "lesson": {**lesson, "applies_when": "sharedidentity rebase"},
                            "confidence": 0.7,
                            "agent_id": "shared",
                        },
                    )
                ],
            )[0]
        )
        assert shared["status"] == "confirmation_required"
        confirmed_shared = decoded(
            call(
                db,
                shared_limited,
                [
                    (
                        "alice_memory_manage",
                        {
                            "action": "confirm",
                            "memory_id": shared["memory"]["id"],
                            "confirmation_id": shared["confirmation_id"],
                            "agent_id": "shared",
                        },
                    )
                ],
            )[0]
        )
        assert confirmed_shared["memory"]["status"] == "active"
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                "select action, actor_id from memory_revisions where memory_id = ? order by revision_number desc limit 1",
                (shared["memory"]["id"],),
            ).fetchone() == ("agentic_memory_confirm_confirm", "shared")
        print("shared agent id: admin writes, project-scoped key confirms")
        print("statuses: committed, review_required, confirmation_required")
        print("recall fields and waiting visibility: verified")
        print("publication revisions:", ", ".join(actions))
        print(
            "full import revisions preserved; incomplete current export refused; legacy memory-only import has no revisions"
        )


if __name__ == "__main__":
    main()
