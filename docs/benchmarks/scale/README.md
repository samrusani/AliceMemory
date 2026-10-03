# Alice scale envelope: core-operation latency at 1k / 10k / 100k memories

The SQLite recall row was re-measured on 2026-07-19 after the v0.13.0-cycle
vector-scale work (vectorized scan plus resident vector cache). Every other
cell in the tables below, all Postgres cells and the SQLite cells except
recall, is from the 2026-07-05 run, because Postgres code was untouched by
that work. A Postgres re-run on 2026-07-19 on a 3-day-old Docker Postgres gave
non-monotonic latencies (recall p50 335.6, 195.2 and 2135.7 ms at 1k, 10k and
100k); its files stay in results/ and are not used in the tables. Both
measurement dates used the same harness: an
Apple Silicon laptop (single machine, no concurrency), a deterministic
synthetic corpus, and a deterministic in-process embedding provider (so the
vector stage runs at every scale without network calls). Tables report p50
over the operation-specific iteration counts recorded in each raw result
(20–50 after warmup; slow operations stop at a disclosed time budget).
Reproduction command below. Raw results: [results/](results/) holds the
2026-07-19 run of both backends; [results-2026-07-05/](results-2026-07-05/)
holds the run behind every Postgres cell and every SQLite cell except recall;
[results-prefix-archive/](results-prefix-archive/) holds the pre-fix run.

## The two numbers that matter for agents

| Operation | Backend | 1k | 10k | 100k |
|---|---|---|---|---|
| **recall (context pack)** | SQLite (2026-07-19) | 20.1ms | 176.9ms | 1764.4ms |
| | Postgres (2026-07-05) | 22.9ms | 98.4ms | 393.6ms |
| **memory commit** | SQLite (2026-07-05) | 2.3ms | 2.3ms | 2.4ms |
| | Postgres (2026-07-05) | 15.5ms | 17.7ms | 20.1ms |

### Inside SQLite recall at 100k: the vector stage is no longer the wall

The 2026-07-05 note attributed the 100k recall cost to the brute-force
vector scan alone. Direct stage measurement (2026-07-19) corrects that:

- **Vector stage, warm resident cache: 385–465ms** (was ~2.1s stateless);
  cold first query after process start or invalidation: ~1.1–1.7s.
- Resident memory for the cache at 100k: **754MB peak / 760MB steady**
  (~6KB per embedded memory; default cap 1024MB via
  `ALICEBOT_SQLITE_VECTOR_CACHE_MAX_MB`, disable with
  `ALICEBOT_SQLITE_VECTOR_CACHE=off`; results are bit-identical either way,
  and over-cap corpora fall back to the stateless scan automatically).
- The remaining ~1.3s of the 100k end-to-end pack is FTS and source-chunk
  search over the full corpus — the next optimization wall, out of scope for
  the vector-cache work and recorded here so the attribution stays honest.

No raw file for these stage figures is committed.

## Full matrix (p50)

Every cell below is from the 2026-07-05 run ([results-2026-07-05/](results-2026-07-05/)).

| Operation | Backend | 1k | 10k | 100k |
|---|---|---|---|---|
| capture (single text) | SQLite | 1.3ms | 2.7ms | 18.3ms |
| | Postgres | 9.0ms | 9.2ms | 14.3ms |
| review queue list | SQLite | 0.5ms | 6.3ms | 72.0ms |
| | Postgres | 1.7ms | 6.4ms | 69.1ms |
| entity lookup (by names) | SQLite | 0.1ms | 0.2ms | 1.9ms |
| | Postgres | 0.5ms | 0.4ms | 1.2ms |
| graph one-hop (hot entity) | SQLite | 0.3ms | 2.2ms | 34.1ms |
| | Postgres | 1.2ms | 1.7ms | 11.8ms |
| staleness sweep (full pass) | SQLite | 12.9ms | 138.8ms | 1786.6ms |
| | Postgres | 13.3ms | 179.8ms | 1856.5ms |
| consolidation clustering pass | SQLite | 154.4ms | 1373.6ms | 4682.3ms |
| | Postgres | 174.1ms | 1348.4ms | 2684.6ms |

Ingest throughput: SQLite about 1,000 to 1,300 memories/sec at every scale
(2026-07-05; 686 to 979 in the 2026-07-19 run); Postgres about 65 to 137
(2026-07-05; 64 to 108 in the 2026-07-19 run). Postgres is per-row round-trips;
bulk import is not yet optimized.

## What this means in practice

- **SQLite (the `uvx alice-memory` trial path)**: writes are effectively
  instant at any scale, and recall is excellent through ~10k memories
  (~180ms). The vector stage now stays interactive well past the old
  ~20–30k comfort zone (385–465ms warm at 100k via the resident cache, at
  the documented memory cost), but end-to-end recall at very large corpora
  is bounded by FTS/source search (~1.8s at 100k). Practical guidance:
  SQLite is comfortable to ~30–50k memories end-to-end; beyond that move to
  Postgres, run FTS-only, or accept slower packs until the FTS wall is
  addressed.
- **Postgres**: production-viable across the board — ~20ms commits and
  ~400ms recall at 100k memories. Recall's growth (23 → 98 → 394ms) is a
  known optimization candidate, not a wall.
- **Scheduled jobs** are nightly-cadence work, not request-path latency.
  Staleness is a full-corpus scan. Consolidation clustering is worst-case
  quadratic; the current implementation bounds its corpus to 2,000 memories,
  its float32 similarity work to bounded row blocks (about 1 MB at the current
  128 x 2,000 block), and unique comparisons to 1,999,000. These
  historical 2026-07-05 measurements used the then-current implementation and
  should not be read as proof of linear consolidation scaling.

## Found and fixed by this benchmark

The first measurement run caught a real production bug: SQLite memory
commits ran an O(N) Python idempotency scan (18ms at 1k → 222ms at 10k →
3.5s at 100k) because the indexed `get_memory_by_commit_digest` fast path
existed only on Postgres. The fix (store method + partial indexes) took
commits to a flat 2.3ms at every scale. Pre-fix results are archived in
[results-prefix-archive/](results-prefix-archive/) as the before/after
record. The run also added post-ingest `ANALYZE` so Postgres is measured on
realistic query plans rather than stale statistics.

## Caveats

- Single machine, single connection, no concurrent load — this is an
  envelope, not a load test.
- Deterministic synthetic corpus and embeddings; real text and real
  embedding endpoints change absolute numbers (not the shapes).
- Postgres runs in a local Docker container (pgvector/pgvector:pg16) with
  fsync-default settings.

## Reproduce

```bash
.venv/bin/python scripts/run_scale_benchmark.py \
    --scales 1000,10000,100000 --backends sqlite,postgres
```

Roughly 35 minutes total; no network or API keys required.
