#!/usr/bin/env python3
"""Scale-envelope benchmark: Alice core-operation latency at 1k/10k/100k.

Seeds a deterministic synthetic corpus into a fresh store per
(backend, scale), then measures p50/p95 latency of the product operations
(recall, capture, commit, review queue, entity lookup, graph hop, staleness
sweep, consolidation). See eval/scale/ for the harness and
docs/benchmarks/scale/README.md for methodology and published results.

Usage (from the repo root):

    .venv/bin/python scripts/run_scale_benchmark.py \
        --scales 1000,10000,100000 --backends sqlite,postgres

The Postgres backend spins up a disposable pgvector/pgvector:pg16 container
on 127.0.0.1:55433 (never the dev compose port) and removes it afterwards.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (_REPO_ROOT / "eval", _REPO_ROOT / "apps" / "api" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scale import corpus  # noqa: E402
from scale.backends import (  # noqa: E402
    PG_DEFAULT_PORT,
    PG_IMAGE,
    PostgresContainer,
    postgres_session,
    sqlite_session,
)
from scale.harness import run_operations, seed_corpus  # noqa: E402
from scale.vectors import MODEL_NAME, stub_embeddings_server  # noqa: E402

DATA_DIR = _REPO_ROOT / "eval" / "scale" / "data"
RESULTS_DIR = _REPO_ROOT / "docs" / "benchmarks" / "scale" / "results"


def _sysctl(name: str) -> str:
    try:
        return subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, cwd=_REPO_ROOT,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def environment_record() -> dict[str, object]:
    record: dict[str, object] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "commit": _git_commit(),
    }
    if sys.platform == "darwin":
        record["cpu"] = _sysctl("machdep.cpu.brand_string")
        memsize = _sysctl("hw.memsize")
        record["ram_gb"] = round(int(memsize) / 2**30) if memsize.isdigit() else memsize
        record["cores"] = _sysctl("hw.ncpu")
    record["postgres_image"] = PG_IMAGE
    record["embedding_model"] = f"{MODEL_NAME} (deterministic local; no API latency included)"
    return record


def _postgres_database_size(container: PostgresContainer, database: str) -> int | None:
    import psycopg

    try:
        with psycopg.connect(container.admin_root_url, autocommit=True) as conn:
            row = conn.execute("SELECT pg_database_size(%s)", (database,)).fetchone()
            return int(row[0]) if row else None
    except psycopg.Error:
        return None


def run_combo(
    *,
    backend: str,
    scale: int,
    args: argparse.Namespace,
    container: PostgresContainer | None,
) -> dict[str, object]:
    seed = args.seed
    combo_started = time.monotonic()
    print(f"[scale-bench] {backend} @ {scale:,} memories: seeding...", flush=True)

    if backend == "sqlite":
        db_path = DATA_DIR / f"scale_{seed}_{scale}.db"
        if db_path.exists():
            db_path.unlink()
        session_ctx = sqlite_session(db_path)
        # Repo-relative, so published results never carry a local home path.
        store_label = db_path.relative_to(_REPO_ROOT).as_posix()
    else:
        assert container is not None
        database = f"scale_{seed}_{scale}"
        container.create_migrated_database(database)
        session_ctx = postgres_session(container, database)
        store_label = f"postgresql://alicebot_app@127.0.0.1:{container.port}/{database}"

    with session_ctx as session:
        ingest = seed_corpus(session, scale, seed=seed)
        print(
            f"[scale-bench]   seeded {ingest.memory_count:,} memories in "
            f"{ingest.total_seconds:.1f}s "
            f"({ingest.memory_count / max(ingest.memories_seconds, 1e-9):.0f} mem/s); measuring...",
            flush=True,
        )
        operations = run_operations(
            session,
            ingest,
            seed=seed,
            iterations=args.iterations,
            time_budget_seconds=args.op_budget_seconds,
            heavy_time_budget_seconds=args.heavy_op_budget_seconds,
        )

    store_size: int | None = None
    if backend == "sqlite":
        store_size = (DATA_DIR / f"scale_{seed}_{scale}.db").stat().st_size
    elif container is not None:
        store_size = _postgres_database_size(container, f"scale_{seed}_{scale}")

    result = {
        "benchmark": "alice-scale-envelope",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "backend": backend,
        "scale": scale,
        "seed": seed,
        "store": store_label,
        "store_size_bytes": store_size,
        "environment": environment_record(),
        "ingest": ingest.to_record(),
        "operations": operations,
        "combo_seconds": round(time.monotonic() - combo_started, 1),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", default="1000,10000,100000")
    parser.add_argument("--backends", default="sqlite,postgres")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--seed", type=int, default=corpus.SEED_DEFAULT)
    parser.add_argument("--pg-port", type=int, default=PG_DEFAULT_PORT)
    parser.add_argument("--op-budget-seconds", type=float, default=45.0)
    parser.add_argument("--heavy-op-budget-seconds", type=float, default=90.0)
    parser.add_argument("--total-budget-minutes", type=float, default=40.0)
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()

    scales = sorted(int(scale.strip()) for scale in args.scales.split(",") if scale.strip())
    backends = [backend.strip() for backend in args.backends.split(",") if backend.strip()]
    for backend in backends:
        if backend not in {"sqlite", "postgres"}:
            parser.error(f"unknown backend: {backend}")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    container: PostgresContainer | None = None
    skipped: list[str] = []
    written: list[Path] = []
    run_started = time.monotonic()

    with stub_embeddings_server() as stub_base_url:
        # Ambient embed-on-write (capture/commit) goes through the real
        # OpenAI-compatible HTTP client against this deterministic stub.
        os.environ["ALICE_EMBEDDINGS_BASE_URL"] = stub_base_url
        os.environ["ALICE_EMBEDDINGS_MODEL"] = MODEL_NAME
        os.environ.pop("ALICE_EMBEDDINGS_API_KEY", None)
        try:
            for scale in scales:
                for backend in backends:
                    elapsed_min = (time.monotonic() - run_started) / 60.0
                    if elapsed_min > args.total_budget_minutes:
                        skipped.append(f"{backend}@{scale} (total budget {args.total_budget_minutes}min exceeded)")
                        continue
                    if backend == "postgres" and container is None:
                        print(f"[scale-bench] starting {PG_IMAGE} on 127.0.0.1:{args.pg_port}...", flush=True)
                        container = PostgresContainer(port=args.pg_port)
                        container.start()
                    result = run_combo(backend=backend, scale=scale, args=args, container=container)
                    out_path = results_dir / f"{backend}-{scale}.json"
                    out_path.write_text(json.dumps(result, indent=2, default=str) + "\n")
                    written.append(out_path)
                    recall = result["operations"]["recall_context_pack"]  # type: ignore[index]
                    print(
                        f"[scale-bench]   done in {result['combo_seconds']}s; "
                        f"recall p50={recall['p50_ms']}ms p95={recall['p95_ms']}ms -> {out_path}",
                        flush=True,
                    )
        finally:
            if container is not None and not args.keep_container:
                container.stop()

    print(f"[scale-bench] wrote {len(written)} result files in {(time.monotonic() - run_started) / 60.0:.1f} min")
    for path in written:
        print(f"  {path}")
    if skipped:
        print("[scale-bench] skipped (budget):")
        for item in skipped:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
