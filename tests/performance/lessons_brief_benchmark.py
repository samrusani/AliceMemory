"""Compare fresh-process keyed briefs on two committed revisions.

Run from a clean, dedicated worktree. The five Python source files used by the
brief are switched between the named commits and restored in a finally block.
All fixture databases and sample files stay under this worktree's .venv.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from tests.unit.test_lessons_read import LESSON, Vault

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "apps/api/src/alicebot_api/session_briefing.py",
    "apps/api/src/alicebot_api/mcp/lessons.py",
    "apps/api/src/alicebot_api/mcp/registry.py",
    "apps/api/src/alicebot_api/vnext_stores/sqlite/lessons.py",
    "apps/api/src/alicebot_api/vnext_stores/postgres/lessons.py",
)
SHAPES = ("none", "published", "waiting", "planted", "mismatch")


def seed(path: Path, shape: str) -> tuple[str, str]:
    vault = Vault(path)
    reader = vault.key("reader", "read_only_agent", "alpha")
    lesson = {
        "text": "Applies when: rebasing a feature branch Do instead: Inspect both sides. What happened: A conflict hid a change.",
        "intent": "explicit_remember",
        "lesson": LESSON,
    }
    rows, revisions = [], []
    for number in range(20_000):
        project = "alpha" if number < 10_000 else "beta"
        kind = shape if project == "alpha" and number < 5_000 else "none"
        mid, key = str(uuid4()), f"bench.{number}"
        value = json.dumps(lesson if kind != "none" else {"text": f"Ordinary note {number}"})
        rows.append((mid, str(vault.user), key, value, "candidate" if kind == "waiting" else "active", "[]",
                     "procedure" if kind != "none" else "preference", f"note {number}",
                     lesson["text"] if kind != "none" else f"Ordinary note {number}", "project", "internal",
                     json.dumps({"project_scope": [project]}), project))
        if kind in {"published", "planted", "mismatch"}:
            prior = lesson if kind == "published" else (
                {"text": "Ordinary fact"} if kind == "planted" else
                {"lesson": {**LESSON, "do_instead": "A different check."}}
            )
            revisions.append((str(uuid4()), str(vault.user), mid, 1, "agentic_memory_commit", key, json.dumps(prior),
                              "[]", "{}", 1, "created", lesson["text"], "user"))
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO memories (id,user_id,memory_key,value,status,source_event_ids,memory_type,title,canonical_text,domain,sensitivity,metadata_json,project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO memory_revisions (id,user_id,memory_id,sequence_no,action,memory_key,new_value,source_event_ids,candidate,revision_number,revision_type,text_after,actor_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            revisions,
        )
    return str(vault.user), reader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--samples", type=int, default=40)
    args = parser.parse_args()
    if args.samples < 40:
        parser.error("at least 40 samples are required")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        parser.error("the worktree must be clean")
    root = Path(tempfile.mkdtemp(prefix="lessons-brief-bench-", dir=ROOT / ".venv"))
    versions = {
        side: {name: subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT) for name in FILES}
        for side, revision in (("base", args.base), ("head", "HEAD"))
    }
    original = {name: (ROOT / name).read_bytes() for name in FILES}
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for shape in SHAPES:
            path = root / f"{shape}.db"
            user, reader = seed(path, shape)
            env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE")}
            env.update(PYTHONPATH=os.pathsep.join((str(ROOT / "apps/api/src"), str(ROOT))),
                       ALICE_AGENT_API_KEY=reader, PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(path), "--user-id", user]
            measurements: dict[str, list[float]] = {"base": [], "head": []}
            for sample in range(args.samples + 1):
                for side in (("base", "head") if sample % 2 == 0 else ("head", "base")):
                    for name in FILES:
                        (ROOT / name).write_bytes(versions[side][name])
                    started = time.perf_counter()
                    subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, check=True)
                    if sample:
                        measurements[side].append(time.perf_counter() - started)
            results = {}
            for side, values in measurements.items():
                ordered = sorted(values)
                results[side] = {"median": statistics.median(values), "p95": ordered[math.ceil(len(values) * .95) - 1]}
            (root / f"{shape}.json").write_text(json.dumps({"samples": measurements, "results": results}, indent=2) + "\n")
            print(json.dumps({"shape": shape, "samples": args.samples, **results}), flush=True)
    finally:
        for name, data in original.items():
            (ROOT / name).write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
