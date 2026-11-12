"""Compare fresh-process keyed briefs on two committed revisions.

Compare committed trees in a temporary directory outside the checkout.
Uncommitted changes are not measured.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import tarfile
import time
from uuid import uuid4

sys.dont_write_bytecode = True

from tests.unit.test_lessons_read import LESSON, Vault  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHAPES = ("none", "published", "waiting", "planted", "mismatch")


def seed(path: Path, shape: str, rows_count: int = 20_000) -> tuple[str, str]:
    vault = Vault(path)
    reader = vault.key("reader", "read_only_agent", "alpha")
    lesson = {
        "text": "Applies when: rebasing a feature branch Do instead: Inspect both sides. What happened: A conflict hid a change.",
        "intent": "explicit_remember",
        "lesson": LESSON,
    }
    rows, revisions = [], []
    for number in range(rows_count):
        project = "alpha" if number < rows_count // 2 else "beta"
        kind = shape if project == "alpha" and number < rows_count // 4 else "none"
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


def extract_tree(revision: str, destination: Path) -> None:
    """Extract a complete commit without registering a worktree or changing the index."""
    destination.mkdir()
    archive = destination.parent / (destination.name + ".tar")
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", revision], cwd=ROOT, stdout=output, check=True)
    try:
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")
    finally:
        archive.unlink()


class SideError(Exception):
    def __init__(self, side: str, error: str):
        self.side = side
        super().__init__(error)


def run_sample(tree: Path, path: Path, user: str, reader: str) -> tuple[float, str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE")}
    # Use the same fixture folder for project detection on both sides when scoping is on.
    env.update(PYTHONPATH=os.pathsep.join((str(tree / "apps/api/src"), str(tree))),
               ALICE_AGENT_API_KEY=reader, ALICE_PROJECT_DIR=str(path.parent), PYTHONDONTWRITEBYTECODE="1")
    probe = (
        "import json,runpy,sys,alicebot_api; "
        "print(json.dumps({'package':alicebot_api.__file__}),flush=True); "
        "sys.argv=['alicebot_api.onramp','brief','--db',sys.argv[1],'--user-id',sys.argv[2]]; "
        "runpy.run_module('alicebot_api.onramp',run_name='__main__')"
    )
    started = time.perf_counter()
    process = subprocess.run([sys.executable, "-c", probe, str(path), user], cwd=tree,
                             env=env, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise RuntimeError(process.stderr or process.stdout or f"exit {process.returncode}")
    first, _, output = process.stdout.partition("\n")
    try:
        package = Path(json.loads(first)["package"]).resolve()
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("measured process did not report its package path: " + first) from exc
    if not package.is_relative_to(tree.resolve()):
        raise RuntimeError(f"package imported outside measured tree: {package}")
    return elapsed, str(package.parent), output


def run_benchmark(trees: dict[str, Path], root: Path, samples: int = 40,
                  rows_count: int = 20_000, shapes: tuple[str, ...] = SHAPES) -> list[dict]:
    results = []
    for shape in shapes:
        path = root / f"{shape}.db"
        user, reader = seed(path, shape, rows_count)
        measurements: dict[str, list[float]] = {"base": [], "head": []}
        imports: dict[str, str] = {}
        for sample in range(samples + 1):
            for side in (("base", "head") if sample % 2 == 0 else ("head", "base")):
                try:
                    elapsed, package, _ = run_sample(trees[side], path, user, reader)
                except Exception as exc:
                    raise SideError(side, str(exc)) from exc
                if side in imports and imports[side] != package:
                    raise SideError(side, "package import path changed between samples")
                imports[side] = package
                if sample:
                    measurements[side].append(elapsed)
        summary = {}
        for side, values in measurements.items():
            ordered = sorted(values)
            summary[side] = {"median": statistics.median(values),
                             "p95": ordered[math.ceil(len(values) * .95) - 1]}
        results.append({"shape": shape, "samples": samples, "imports": imports,
                        "measurements": measurements, "results": summary})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.samples < 40:
        parser.error("at least 40 samples are required")
    work_dir = (args.work_dir or Path(tempfile.gettempdir())).resolve()
    if work_dir.is_relative_to(ROOT):
        parser.error("work directory must be outside the checkout")
    revisions = {}
    for side, ref in (("base", args.base), ("head", args.head)):
        try:
            revisions[side] = subprocess.check_output(
                ["git", "rev-parse", "--verify", ref + "^{commit}"], cwd=ROOT, text=True,
                stderr=subprocess.PIPE).strip()
        except subprocess.CalledProcessError as exc:
            print(json.dumps({"side": side, "error": exc.stderr.strip()}), file=sys.stderr)
            return 1
    root = Path(tempfile.mkdtemp(prefix="lessons-brief-bench-", dir=work_dir)).resolve()
    trees = {side: root / side for side in ("base", "head")}
    context = {"revisions": revisions, "imports": {side: str(tree / "apps/api/src/alicebot_api")
                                                   for side, tree in trees.items()}}
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
            print(json.dumps({**context, "notice": "Uncommitted changes are not measured."}), flush=True)
        for side in ("base", "head"):
            try:
                extract_tree(revisions[side], trees[side])
            except Exception as exc:
                raise SideError(side, str(exc)) from exc
        results = run_benchmark(trees, root, args.samples)
        for result in results:
            record = {**result, "revisions": revisions}
            (root / (result["shape"] + ".json")).write_text(json.dumps(record, indent=2) + "\n")
            print(json.dumps({key: value for key, value in record.items() if key != "measurements"}), flush=True)
        return 0
    except SideError as exc:
        print(json.dumps({**context, "side": exc.side, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({**context, "error": "interrupted"}), file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(json.dumps({**context, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        if not args.keep:
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
