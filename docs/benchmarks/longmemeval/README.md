# Alice on LongMemEval

**Product path.** 81.2% is the privileged `store_chunks` harness on
`v0.12.0`, measured 2026-07-18/19. `pack_excerpts` is the product-path
mode and is not yet scored. The receipt below is unchanged.

> **Known issue: session id label.** In this dataset the id of every evidence
> session starts with `answer_` (948 of 948), and no filler session id does
> (0 of 22,919). The harness put that id in the header above each excerpt and
> in the first line of each session's text, so the model answering the
> question could see which sessions held the evidence. The same label code was
> in the harness for the 2026-07-05 run (64.6%), the 2026-07-07 run (79.4%)
> and the v0.12.0 runs (81.2%), so all three published numbers carry it. The
> 2026-07-07 run file has no session id field, but 37 reader answers in it
> name an `answer_` session id. The three replication runs record the
> retrieved session ids in `retrieval.provenance.source_session_ids`, and in
> 29 to 32 answers per run the model named such a session id in its reasoning.
> We have not measured the effect, and on abstention questions its direction
> is not obvious. The next run hides the ids with a keyed hash, scores the
> product path, and is compared question by question with these three runs.
> Until then, read these numbers as what the old harness produced, not as
> what Alice does for a user. The evidence files in this folder are not
> edited.

**Replicated accuracy: 81.2% mean over three independent full runs
(80.8% / 81.0% / 81.8%; 404–409 of 500) on LongMemEval_s** — measured
2026-07-18/19 on the published v0.12.0 code from the immutable release tag,
with the same configuration as the historical run: gpt-4o reader
(chain-of-thought, 24,000-char context budget, 16 items),
gpt-4o-2024-08-06 judge with the benchmark's official protocol,
text-embedding-3-small embed-on-write. Every run finished 500/500 scored
with zero errors. Per-question evidence for all three runs is in
[replication-v0.12.0-2026-07-19/](replication-v0.12.0-2026-07-19/).

Replication notes, disclosed: transient OpenAI embedding-batch timeouts
during ingest were cured by re-running the affected question supersets at
lower concurrency before scoring was accepted (the same recovery the
historical run used); one batch failure in run 1's cleanup pass could not be
attributed to a question and is bounded to at most one partially-embedded
ingest. Per-type accuracy is stable across runs (multi-session, the weakest
category: 63.2/63.9/63.9%); the 30-question abstention subset is noisy
across runs (76.7/90.0/83.3%) and should not be quoted to a single decimal.

## Historical single run (superseded as the headline, retained as evidence)

**Historical single-run accuracy: 79.4% (397/500) on LongMemEval_s** — run
2026-07-07 with
GPT-4o generation and the benchmark's official judge protocol, up from the
prior published run's 64.6% (323/500). Paired on the same 500 questions:
+96 gained, −22 lost, net +74 (McNemar exact two-sided p = 3.26e-12). All 500
questions answered and judged; the complete per-question evidence is in
[per-question-results-2026-07-07.jsonl](per-question-results-2026-07-07.jsonl),
the aggregate report in [report-2026-07-07.json](report-2026-07-07.json), and
the question-by-question flip analysis in
[paired-comparison-2026-07-07.txt](paired-comparison-2026-07-07.txt).

This is a dated benchmark receipt, not a v0.10.2 release measurement, a
repeated-run estimate, or a claim that every current deployment will reproduce
79.4%.

Non-abstention subset: **79.8%** (375/470). Abstention subset: 73.3% (22/30)
— a regression from the baseline's 25/30, disclosed in its own section below.

## Results by question type (paired against the 2026-07-05 baseline)

| Question type | n | Baseline | 2026-07-07 | Net flips |
|---|---|---|---|---|
| single-session-user | 70 | 90.0% | 97.1% | +5 |
| single-session-assistant | 56 | 75.0% | 92.9% | +10 |
| knowledge-update | 78 | 74.4% | 89.7% | +12 |
| temporal-reasoning | 133 | 61.7% | 81.2% | +26 |
| single-session-preference | 30 | 60.0% | 70.0% | +3 |
| multi-session | 133 | 45.1% | 58.6% | +18 |
| **Overall** | **500** | **64.6%** | **79.4%** | **+74** |

Every category improved. Multi-session synthesis (58.6%) remains the weakest
category and the top roadmap item.

## Configuration: what differs between the two runs

Shared by both runs: `gpt-4o` generation at temperature 0,
`gpt-4o-2024-08-06` judging with the benchmark's official per-type judge
prompts (ported verbatim), `text-embedding-3-small` embeddings, dataset
`longmemeval_s_cleaned.json` (sha256 prefix `d6f21ea9d60a0d56`, the
post-2025/09 cleaned release).

| Knob | 2026-07-05 baseline | 2026-07-07 |
|---|---|---|
| Reading template | official standard template | official chain-of-thought template (`reading_style=cot`) |
| Context-pack `max_items` | 8 | 16 |
| Context character budget | 12,000 | 24,000 |
| Sources retrieval stage | title/metadata match (content-blind — see bug fix below) | RRF fusion over chunk-level FTS + memory provenance + title/recency |
| Excerpt packing | budget-order fill | best-chunk-per-source guarantee, session-timestamp order |
| Config digest | `798c10822f50a667` | `954b203d34e9b96d` |

Both reading templates are the benchmark's own; no prompt of ours was
substituted anywhere in the generation or judging path.

## Known trade-off: abstention regressed

The abstention subset — the 30 questions whose correct answer is "I don't
know" — went from **25/30 to 22/30 (−3)**. The chain-of-thought reading
style makes the model more willing to answer when the memory lacks the fact.
This is the known trade-off of `reading_style=cot`, stated here plainly
rather than netted away: the +74 overall already includes this −3.

## What changed between the runs

Items 1–3 are product code changes with no benchmark-specific logic; items 4–5
are run-configuration choices, disclosed here and recorded in the committed
report config:

1. **Bug fix — source search was content-blind.** The sources stage matched
   only titles and metadata, with a broken stopword list, so it effectively
   returned the most-recent sessions regardless of the query. It is now RRF
   fusion over chunk-level full-text hits, the provenance of winning
   memories, and title/recency signals.
2. **Excerpt packing** now guarantees each retrieved source its best chunk
   before spending the remaining budget, and renders excerpts in
   session-timestamp order.
3. **FTS OR-fallback** when strict AND matching finds nothing.
4. **Disclosed config widening**: 16 items / 24k-char context (was 8 / 12k).
5. **The official chain-of-thought reading template** (was the official
   standard template).

Two cheaper measurements preceded the full run:

- An offline evidence-coverage replay (free, FTS-only) of the retrieval
  changes alone lifted all-evidence coverage from 79.0% to 84.6% overall,
  and multi-session from 62.4% to 74.4% (rows and summaries committed under
  [uplift-evidence/](uplift-evidence/)).
- A stage-1 validation slice (172 fixed questions, paired): the
  retrieval-only change was net +7; the full configuration was net +18
  (64.5% → 75.0% on the slice, p = 0.0029). Both slice checkpoints and
  their paired comparisons are committed under
  [uplift-evidence/](uplift-evidence/).

## What was measured

The full Alice pipeline, per question, in an isolated store:

1. Every haystack session is ingested through Alice's real capture service
   (chunking, candidate extraction, provenance) into a fresh SQLite store,
   with embed-on-write via `text-embedding-3-small`.
2. At answer time, Alice's production retrieval (`compile_context_pack`:
   SQLite FTS5 + vector cosine fused with reciprocal-rank fusion, budget
   enforced) builds the context block.
3. `gpt-4o` (temperature 0) answers from that context using the benchmark's
   official reading templates.
4. `gpt-4o-2024-08-06` judges with the benchmark's official per-type judge
   prompts (ported verbatim).

## Methodology notes

- **Quota outage and resume.** The run hit an API quota outage mid-run: the
  account ran dry at 2026-07-07T01:56 after 201 questions (a smaller number
  of transient `RemoteDisconnected` connection drops also occurred earlier
  in the run and were retried the same way). It was topped up
  and the run resumed with `--resume`; resume passes only re-ran questions
  without a completed answer. The checkpoint file therefore holds 892 rows
  for 500 questions — error-retry entries are retained deliberately (the
  file is append-only evidence) and deduplication is by `question_id`,
  keeping the last row per question. The aggregate report counts unique
  `question_id`s.
- **This is a single run.** The prior campaign established a three-run
  variance band of 63.0–64.6 on the old configuration; treat single-run
  deltas of a point or two as noise. The paired +74 (p = 3.26e-12) is far
  outside that band, but the 79.4% headline itself has not yet been
  replicated.

## Reproduce

```bash
python eval/longmemeval/fetch.py --variant s
export ALICE_LME_MODEL_BASE_URL=https://api.openai.com/v1 \
       ALICE_LME_MODEL=gpt-4o \
       ALICE_LME_MODEL_API_KEY=... \
       ALICE_LME_JUDGE_MODEL=gpt-4o-2024-08-06 \
       ALICE_EMBEDDINGS_BASE_URL=https://api.openai.com/v1 \
       ALICE_EMBEDDINGS_MODEL=text-embedding-3-small \
       ALICE_EMBEDDINGS_API_KEY=...
python scripts/run_longmemeval.py --variant s --workers 3 --resume \
       --cot --max-items 16 --context-char-budget 24000
```

This runs harness 1.1, which hides the dataset session ids behind keyed-hash labels. The published 81.2%, 79.4% and 64.6% were produced with the ids shown to the reader, so this command does not reproduce them. To rerun the older configurations add `--raw-session-labels` and a new checkpoint; see [REPRODUCTION-NOTES.md](REPRODUCTION-NOTES.md#reproducing-the-older-configurations). That page also describes the harness 1.1 flags.

The 24k-char context roughly doubles the token spend of the baseline
configuration; budget accordingly.

## The honesty kit

Everything needed to audit these numbers is collected in
[HONESTY-KIT.md](HONESTY-KIT.md): the exact judge protocol (model, verbatim
prompts, temperature), dataset revision hashes, the config fingerprint of
every published run, our negative results as first-class findings, and the
judge-free **stale-pick metric** — an offline classifier that replays any
run checkpoint against the dataset's knowledge-update chains and reports how
often the answer picked a superseded value (baseline:
[stale-pick-baseline-2026-07-10.json](stale-pick-baseline-2026-07-10.json)).
No API keys required to check any of it.

## Evidence files

- [per-question-results-2026-07-07.jsonl](per-question-results-2026-07-07.jsonl)
  — 892 rows for 500 questions (quota-outage retries and resume passes
  appended, never rewritten); dedupe by `question_id` keeping the **last**
  row per question.
- [report-2026-07-07.json](report-2026-07-07.json) — aggregate report over
  unique `question_id`s, with the full config fingerprint.
- [paired-comparison-2026-07-07.txt](paired-comparison-2026-07-07.txt) —
  per-type gained/lost flips and the McNemar test against the baseline run.
- [per-question-results.jsonl](per-question-results.jsonl) and
  [report.json](report.json) — the prior 2026-07-05 baseline run's evidence
  (508 rows for 500 questions; same append-only rule).

## Prior baseline: the 2026-07-05 run (64.6%)

**Overall accuracy: 64.6% (323/500)** — run 2026-07-05 at commit `eda49da`
with the same generation/judge/embedding models as above, the official
standard reading template, `max_items=8`, and a 12k-char context budget.

| Question type | Accuracy | Correct |
|---|---|---|
| single-session-user | 90.0% | 63/70 |
| single-session-assistant | 75.0% | 42/56 |
| knowledge-update | 74.4% | 58/78 |
| temporal-reasoning | 61.7% | 82/133 |
| single-session-preference | 60.0% | 18/30 |
| multi-session | 45.1% | 60/133 |
| **Overall** | **64.6%** | **323/500** |

Non-abstention subset: 63.4% (298/470). Abstention subset: 83.3% (25/30).

Honest caveats from that campaign, kept as part of the record:

- **Run-to-run variance is real; treat single-run deltas under ~2 points as
  noise.** Three full 500-question runs were completed on that
  configuration: 64.2% and 64.6% on the quoted release, and 63.0% on a
  later build that added entity-graph retrieval. The spread (63.0–64.6,
  mean ≈ 63.9) is consistent with binomial variance at n=500 under
  non-bit-deterministic temperature-0 inference. We quoted 64.6% because it
  is the run with the complete archived per-question evidence; the honest
  summary of that configuration is "approximately 64%".
- **Negative result, disclosed:** the entity-graph retrieval stage (which
  provably lifts entity-name queries in `graph_hop_retrieval` from 0.0 to
  1.0 recall) did **not** move this benchmark's multi-session category
  (45.1% → 42.1%, within noise). Diagnosis: LongMemEval's multi-session
  questions aggregate everyday facts with no named entities to hop through.
  The mechanism is real; this benchmark's weakest category needs retrieval
  breadth, not graphs.
- **Breadth ablation (multi-session only):** re-running just the 133
  multi-session questions with widened retrieval (16 items, ~6k context
  tokens instead of ~3k) scored **49.2%** — a directional +4–7 point lift
  over the 42.1%/45.1% full-run readings at double the context cost. That
  ablation motivated the retrieval-breadth work that produced the
  2026-07-07 run, and the planned query-shape-aware aggregation mode.
- **Cross-system comparisons are approximate.** Published numbers for other
  systems use their own ingestion, retrieval, prompt, and (sometimes) judge
  variations. The judge model and prompts here are the benchmark's official
  ones.
- **Dataset variant.** These numbers use the cleaned 2025-09 dataset
  release; older reports may use the original files.

## What the breakdown says about Alice

Knowledge-update (89.7%) is consistent with the correction/supersession machinery working as designed (no isolating ablation was run; the wider CoT context is a confound):
when facts change across sessions, Alice's retrieval surfaces the current
truth. Temporal-reasoning (81.2%) benefits from event-time capture plus the
timestamp-ordered excerpt rendering. The clear frontier is still
**multi-session synthesis (58.6%)** — questions whose answers must be
assembled from evidence scattered across many sessions. The retrieval-breadth
fixes moved it from 45.1%, but it remains the weakest category; the
query-shape-aware aggregation mode is the planned next step, and improving
it stays the top benchmark-driven roadmap item.
