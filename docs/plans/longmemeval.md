# Plan: LongMemEval Harness (`scripts/run_longmemeval.py`)

Status: implemented on this branch (`memory-frontier`). Everything lives in
new files (`eval/longmemeval/`, `scripts/run_longmemeval.py`, this page);
no existing module was touched. What remains for a scored number is exactly
two things: fetch the dataset and point the harness at a chat endpoint.

## Why

LongMemEval (Wu et al., ICLR 2025) is the long-term-memory benchmark the
memory-vendor ecosystem quotes: Zep reports 63.8% with gpt-4o-mini (71.2%
with gpt-4o) on LongMemEval_S, mem0's 2026 blogs claim 90–94%, the paper's
own full-context GPT-4o baseline is 60.6% (87.0% oracle). Alice claims to be
the continuity layer for AI agents; this harness measures that claim with
the benchmark's official protocol against Alice's *real* write and read
paths, not a synthetic fixture.

## Dataset: source of record (verified 2026-07-04)

- Paper: <https://arxiv.org/abs/2410.10813> (ICLR 2025). Code:
  <https://github.com/xiaowu0162/LongMemEval>.
- Data: the official README now points at the HuggingFace repo
  **<https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned>** (MIT).
  The history sessions were cleaned in 2025/09, so the `s`/`m` files are
  renamed with a `_cleaned` suffix:
  - `longmemeval_s_cleaned.json` — 277,383,467 bytes, sha256
    `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
  - `longmemeval_m_cleaned.json` — 2,737,100,077 bytes, sha256
    `9d79e5524794a2e6900a3aa9cb7d9152c5a3e8319c9a87c25494ba1eacee495f`
  - `longmemeval_oracle.json` — 15,388,478 bytes, sha256
    `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`
    (byte-identical to the pre-cleanup oracle file)
  Checksums are the LFS oids from the HF tree API
  (<https://huggingface.co/api/datasets/xiaowu0162/longmemeval-cleaned/tree/main>);
  `fetch.py` verifies them. The HF `datasets` loader does *not* work on
  these files (mixed str/int `answer` column — 468 strings, 32 ints), and it
  is not installed here anyway; the fetcher streams the raw JSON with
  `urllib`.
- Format: each file is a JSON array of 500 instances with
  `question_id`, `question_type`, `question`, `answer`, `question_date`,
  `haystack_dates`, `haystack_session_ids`, `haystack_sessions`,
  `answer_session_ids`. Sessions are lists of
  `{"role": "user"|"assistant", "content": ...}` turns; evidence turns add
  `has_answer: true`. Timestamps look like `2023/04/10 (Mon) 23:07`.
  A `question_id` ending `_abs` is an abstention question (30 of 500) whose
  `answer` field holds the judge-facing explanation.
- Question types (counts in the 500): single-session-user 70,
  single-session-assistant 56, single-session-preference 30,
  multi-session 133, temporal-reasoning 133, knowledge-update 78.
- Scale: `_s` ≈ 40–50 sessions (~115k tokens) per question; `_m` ≈ 500
  sessions (~1.5M tokens); `oracle` = evidence sessions only.
- Metric: QA accuracy via an LLM judge (`evaluate_qa.py`), judge model
  `gpt-4o-2024-08-06`, `temperature=0`, `max_tokens=10`, correct iff
  `"yes" in reply.lower()`. The per-type judge prompts are ported verbatim
  into `eval/longmemeval/judge.py`.

## What the harness does

Per question (`eval/longmemeval/adapter.py`):

1. **Isolated store** — one SQLite file per question under a scratch dir
   (`sqlite_user_connection` + `SQLiteVNextStore`; schema bootstrap, event
   log, FTS5 triggers — the same zero-infrastructure on-ramp the product
   ships). No services, questions parallelize freely.
2. **Ingest through the real capture path** — each haystack session is
   rendered speaker-tagged (`[USER]: ...` / `[ASSISTANT]: ...`, one
   paragraph per turn, session id + date in the header, title, and
   metadata) and fed to `VNextCaptureService.capture_source`: sources,
   chunks (turn-boundary chunking via the paragraph splitter), provenance
   links, and candidate memories from the extraction heuristics, which
   are not embedded while they are candidates. Candidates are
   then promoted to `active` via the store's review-accept patch
   (`update_memory(status="active")`) because Alice's search stages only
   see active/accepted memories — the harness plays the "user accepted the
   capture review" role that a human plays in the product.
3. **Retrieve through the real read path** —
   `VNextRetrievalService.compile_context_pack` with the benchmark question
   as the query: hybrid FTS5 + vector KNN + reciprocal-rank fusion (or
   FTS-only without an embedding provider). The pack is rendered into a
   compact context block: memory facts (with session dates) first, then
   chunk excerpts of the retrieved source sessions ranked by query-term
   overlap, under a character budget (default 12,000 chars ≈ 3k tokens).
4. **Answer** — the official LongMemEval reading prompt (verbatim template,
   `Current Date` = `question_date`, `temperature=0`, `max_tokens=500`;
   `--cot` switches to the official chain-of-thought variant at 800), with
   the history slot filled by Alice's context block instead of the full
   haystack. Any OpenAI-compatible `/chat/completions` endpoint works.
5. **Judge** — the official per-type judge prompts and label rule
   (`eval/longmemeval/judge.py`); abstention questions use the dedicated
   abstention template regardless of type.

The runner (`eval/longmemeval/runner.py`, entry
`scripts/run_longmemeval.py`) drives a small thread pool, checkpoints one
JSONL record per question (so `--resume` continues long runs and retries
only errors), and writes a report with overall + per-type accuracy, the
abstention split, retrieval stats (context chars/≈tokens, retrieval-latency
p50/p95, ingest time, vector-stage share), and a config fingerprint
(alicebot version, dataset sha256 prefix, models, embeddings on/off,
budgets) whose digest is stamped on every record — resuming under a changed
config warns loudly.

## How to run

```bash
# 1. Fetch the dataset (gitignored; ~277 MB for _s, sha256-verified):
.venv/bin/python eval/longmemeval/fetch.py --variant s

# 2. Model-free smoke (also the CI path; skips cleanly if no dataset):
.venv/bin/python scripts/run_longmemeval.py --dry-run

# 3. Scored run (any OpenAI-compatible endpoint):
export ALICE_LME_MODEL_BASE_URL="https://api.openai.com/v1"
export ALICE_LME_MODEL="gpt-4o-mini"
export ALICE_LME_MODEL_API_KEY="sk-..."
# judge defaults to the same endpoint/model; override for the official judge:
export ALICE_LME_JUDGE_MODEL="gpt-4o-2024-08-06"
# optional — activates Alice's vector stage (strongly recommended, see below):
export ALICE_EMBEDDINGS_BASE_URL="http://localhost:11434/v1"
export ALICE_EMBEDDINGS_MODEL="nomic-embed-text"

.venv/bin/python scripts/run_longmemeval.py --variant s --resume --workers 4

# Harness unit + integration tests (not part of the main suite on purpose):
.venv/bin/python -m pytest eval/longmemeval/test_harness.py -q
```

Useful flags: `--limit N` (subset), `--dataset-file` / `--data-dir`
(alternate locations), `--cot` (official chain-of-thought reading prompt),
`--max-items` / `--context-char-budget` (retrieval knobs), `--keep-stores`
(inspect per-question SQLite files), `--report` / `--checkpoint` (paths;
default `eval/longmemeval/results/`).

### Env var contract

| Variable | Required | Meaning |
| --- | --- | --- |
| `ALICE_LME_MODEL_BASE_URL` | scored runs | OpenAI-compatible base URL for answer generation |
| `ALICE_LME_MODEL` | scored runs | answer model name |
| `ALICE_LME_MODEL_API_KEY` | if endpoint needs it | bearer token (never written to reports) |
| `ALICE_LME_JUDGE_BASE_URL` / `ALICE_LME_JUDGE_MODEL` / `ALICE_LME_JUDGE_API_KEY` | optional | judge endpoint; each field falls back to the model value |
| `ALICE_EMBEDDINGS_BASE_URL` / `ALICE_EMBEDDINGS_MODEL` / `ALICE_EMBEDDINGS_API_KEY` | optional | existing Alice envs; enable the vector retrieval stage |
| `ALICE_LME_CONTEXT_CHAR_BUDGET` | optional | rendered context budget (default 12000 chars) |
| `ALICE_LME_MAX_ITEMS` | optional | context-pack `max_items` (default 8) |

## Cost and time for a full `_s` run (500 questions)

- **Ingest**: pure-Python capture of ~500 KB of chat per question; a few
  seconds each, roughly 30–60 minutes total on a laptop. Threads overlap
  model latency well but not this CPU-bound phase (GIL).
- **With embeddings on**: the harness embeds the promoted memories in
  batches at promotion, because capture no longer embeds a candidate
  (Unreleased, on main, not in v0.19.2). Expect a few hundred memories per question, i.e. ~10⁵ calls
  for the full run — use a local embedding server (Ollama/LM Studio) or
  budget hours against a hosted one.
- **Generation + judging** with hosted models: 500 × (one ~3–4k-token
  generation + one ~300-token judge call). At July-2026 OpenAI list prices
  that is roughly **$8–12 with gpt-4o both sides, well under $1 with
  gpt-4o-mini**. Wall clock ~30–60 min at `--workers 4`.
- End-to-end: **~1.5–2.5 hours and ≤ ~$10** for a gpt-4o-scored `_s` run.
- `_m` note: the file is 2.7 GB and `json.load` will want >10 GB RAM;
  ingest is ~10× `_s`. Treat `_m` as an overnight run; `--resume` exists
  for exactly this.

## Honest notes on comparability

- **Published numbers are not apples-to-apples.** Zep's 63.8% used
  gpt-4o-mini as the reader on the *pre-cleanup* dataset; the paper's
  full-context baselines used 2024 models; mem0's 2026 numbers are
  self-reported on the cleaned data with different readers. Any Alice
  number must be reported with this harness's config fingerprint (reader
  model, judge model, embeddings model, budgets, dataset sha256) and
  compared only against runs with the same reader/judge.
- **The judge matters.** The official judge is `gpt-4o-2024-08-06`. The
  harness defaults the judge to your answer model for convenience — that is
  *not* the official protocol; set `ALICE_LME_JUDGE_*` explicitly when the
  number is meant to be quoted.
- **Without embeddings, expect weak memory-stage recall.** Alice's FTS
  MATCH is AND-conjunctive over query terms, so natural-language questions
  ("What breed is the user's dog?") often match zero *memories*; recall
  then rides on the source-excerpt path, which is LIKE/OR-based. The dry
  run demonstrates this honestly (`memory_count: 0`,
  `no_relevant_memories_selected`, yet correct evidence in the excerpts).
  A quotable run should configure `ALICE_EMBEDDINGS_*`.
- **One deliberate deviation from "pure product":** promoting capture
  candidates to `active` in bulk stands in for the product's human review
  step. Without it the memory stages would run empty by design; the
  promotion is store-level (`update_memory`), not a bypass of the write
  path.
- **The context renderer is harness code.** Every LongMemEval integration
  needs a mapping from its system's retrieval output to a reading prompt;
  ours (facts + overlap-ranked chunk excerpts under a budget) is
  deterministic and reported via `context_chars` / `approx_context_tokens`,
  but it is a choice, and changing it changes scores.

## What remains for a scored number

1. `python eval/longmemeval/fetch.py --variant s` (network to HuggingFace).
2. `ALICE_LME_MODEL_*` (+ ideally `ALICE_LME_JUDGE_MODEL=gpt-4o-2024-08-06`
   and `ALICE_EMBEDDINGS_*`) pointed at real endpoints, then
   `scripts/run_longmemeval.py --variant s --resume`.

Nothing else is pending: the ingest → retrieve → answer → judge → aggregate
pipeline, checkpoint/resume, dry-run CI smoke, and the harness test suite
(23 tests, model- and network-free) are all green on this branch.
