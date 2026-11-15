"""Compare accepted context-pack and capture calls on two SQLite source trees."""

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
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    vault = Vault(directory / "vault.db")
    patch = MonkeyPatch()
    key = vault.key("benchmark", "project_scoped_agent", "alpha")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        for index in range(10_000):
            store.create_memory({"memory_key": f"bench-{index}", "canonical_text": f"Project procedure {index}",
                                 "status": "active", "domain": "project", "sensitivity": "internal",
                                 "project_scope": ["alpha"], "project_id": "alpha"})
    print("ready", flush=True)
    for line in sys.stdin:
        action, number = line.split()
        name = "alice_context_pack" if action == "context" else "alice_capture"
        arguments = ({"query": "project procedure", "max_items": 8} if action == "context" else
                     {"raw_text": f"Synthetic benchmark capture number {number}.", "domain": "project",
                      "sensitivity": "private"})
        start = time.perf_counter_ns()
        answer = vault.call(patch, key, name, arguments)
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
        if action == "capture" and answer.get("status") != "imported":
            raise RuntimeError(f"capture was not accepted: {answer}")
        print(json.dumps({"ms": elapsed}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path.cwd())
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        worker(args.worker)
        return
    if args.base is None:
        parser.error("--base is required")
    root = Path(__file__).resolve().parents[1]
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-timing-", dir=args.work_dir.resolve()) as directory:
        processes = []
        for side, source in (("base", args.base.resolve()), ("head", root)):
            work = Path(directory) / side
            work.mkdir()
            env = {name: value for name, value in os.environ.items()
                   if not name.startswith(("ALICE_", "ALICEBOT_"))
                   and not name.endswith(("_API_KEY", "_BASE_URL"))}
            env["PYTHONPATH"] = os.pathsep.join((str(source / "apps/api/src"), str(source)))
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(work)],
                                       cwd=source, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            processes.append((side, process))
        try:
            for _, process in processes:
                assert process.stdout is not None
                if process.stdout.readline().strip() != "ready":
                    raise RuntimeError("benchmark worker did not start")
            samples: dict[str, dict[str, list[float]]] = {side: {"context": [], "capture": []} for side, _ in processes}
            for action in ("context", "capture"):
                for index in range(40):
                    for side, process in processes if index % 2 == 0 else processes[::-1]:
                        assert process.stdin is not None and process.stdout is not None
                        process.stdin.write(f"{action} {index}\n")
                        process.stdin.flush()
                        samples[side][action].append(json.loads(process.stdout.readline())["ms"])
            for action in ("context", "capture"):
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
