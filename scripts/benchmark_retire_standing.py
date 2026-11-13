"""Interleave accepted SQLite forget and expire timings against a source tree."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def worker(directory: Path) -> None:
    from pytest import MonkeyPatch
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from tests.unit.test_lessons_read import Vault

    logging.disable(logging.CRITICAL)
    os.environ["ALICE_PROJECT_DIR"] = str(directory)
    vault = Vault(directory / "vault.db")
    patch = MonkeyPatch()
    key = vault.key("bench", "project_scoped_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        for index in range(10_000):
            store.create_memory({"memory_key": f"bench-{index}", "canonical_text": f"Project procedure {index}",
                                 "status": "active", "domain": "project", "sensitivity": "internal",
                                 "project_scope": ["alpha"], "project_id": "alpha"})
    ids = {"forget": [], "expire": []}
    for action in ids:
        for index in range(40):
            result = vault.call(patch, key, "alice_memory_commit", {
                "title": f"Procedure {action} {index}", "canonical_text": f"Project note {action} {index}",
                "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"],
            })
            ids[action].append(str(result["memory"]["id"]))
    print("ready", flush=True)
    for line in sys.stdin:
        action, index = line.strip().split()
        start = time.perf_counter_ns()
        result = vault.call(patch, key, "alice_memory_manage", {
            "action": action, "memory_id": ids[action][int(index)], "reason": "Retire it",
        })
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
        print(json.dumps({"ms": elapsed, "status": result["status"]}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, help="A source tree at the comparison revision")
    parser.add_argument("--work-dir", type=Path, default=Path.cwd())
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        worker(args.worker)
        return
    if args.base is None:
        parser.error("--base is required")
    root = Path(__file__).resolve().parents[1]
    base = args.base.resolve()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="retire-timing-", dir=args.work_dir.resolve()) as directory:
        processes = []
        for side, source in (("base", base), ("head", root)):
            work = Path(directory) / side
            work.mkdir()
            env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source / "apps/api/src"), str(source)))}
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(work)],
                                       cwd=source, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            processes.append((side, process))
        try:
            for _, process in processes:
                assert process.stdout is not None
                if process.stdout.readline().strip() != "ready":
                    raise RuntimeError("benchmark worker did not start")
            samples = {side: {"forget": [], "expire": []} for side, _ in processes}
            for action in ("forget", "expire"):
                for index in range(40):
                    for side, process in processes if index % 2 == 0 else processes[::-1]:
                        assert process.stdin is not None and process.stdout is not None
                        process.stdin.write(f"{action} {index}\n")
                        process.stdin.flush()
                        result = json.loads(process.stdout.readline())
                        if result["status"] not in {"forgotten", "expired"}:
                            raise RuntimeError(result)
                        samples[side][action].append(result["ms"])
            for action in ("forget", "expire"):
                before = statistics.median(samples["base"][action])
                after = statistics.median(samples["head"][action])
                print(f"{action}: base {before:.3f} ms, head {after:.3f} ms, {(after / before - 1) * 100:+.2f}%")
        finally:
            for _, process in processes:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait()


if __name__ == "__main__":
    main()
