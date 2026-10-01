# The Alice LongMemEval Honesty Kit

Benchmark numbers for memory systems are notoriously hard to compare:
ingestion, retrieval, prompts, judge models, and dataset revisions all vary
between published claims, and the differences are rarely disclosed. This
document is the antidote for **our** numbers: every knob, hash, and prompt
needed to check them, our negative results treated as first-class findings,
and a judge-free metric anyone can replay against our committed evidence for
free. If a claim in our README cannot be traced to a receipt here or to a
committed evidence file, treat it as unproven.

## 1. The judge protocol, exactly

Everything the judge sees and does is the benchmark's own, ported verbatim
and byte-frozen:

- **Judge model:** `gpt-4o-2024-08-06` (the paper's judge model).
- **Call shape:** one user message, `temperature=0`, `max_tokens=10`.
- **Label extraction:** a response is correct iff `"yes"` appears in the
  lowercased judge reply.
- **Prompts:** the five per-type templates (default, temporal-reasoning,
  knowledge-update, preference, abstention) are verbatim ports of
  `get_anscheck_prompt` in `src/evaluation/evaluate_qa.py` of the official
  LongMemEval repo. They live in
  [`eval/longmemeval/judge.py`](../../../eval/longmemeval/judge.py), and the
  reading templates in `eval/longmemeval/runner.py`.
- **Byte-freeze:** `test_official_templates_byte_frozen` in
  [`eval/longmemeval/test_harness.py`](../../../eval/longmemeval/test_harness.py)
  pins the sha256 of every official template. Any edit to a judge or reading
  prompt fails CI. What we control is pack **content** — the context block
  placed in the template's history slot — never the instruction text.

Abstention questions (`question_id` ending `_abs`) always use the dedicated
abstention template, with the dataset's `answer` field as the explanation.

## 2. Dataset revision

All published numbers use the post-2025/09 cleaned release:

- File: `longmemeval_s_cleaned.json` (500 questions).
- sha256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
  — verified on fetch by `eval/longmemeval/fetch.py`, and recorded per run
  as `dataset_sha256_prefix` in every report and checkpoint fingerprint.

## 3. Config fingerprints of every published run

Historical checkpoint rows carry a `fingerprint_digest` over most run
configuration (models, temperature, budgets, reading style, dataset hash,
question subset). The historical format did **not** cover the source tree or
reranker configuration. Starting with the v0.9.2 harness, fingerprints also
record the source commit, a digest of any tracked diff, embedding endpoint,
reranker model and prompt digest. These new fields do not retroactively make
older checkpoints more complete.

| Run | Result | Digest | Evidence |
|---|---|---|---|
| 2026-07-05 full run (baseline) | 64.6% (323/500) | `798c10822f50a667` | [per-question-results.jsonl](per-question-results.jsonl), [report.json](report.json) |
| 2026-07-07 full run (earlier headline, superseded) | 79.4% (397/500) | `954b203d34e9b96d` | [per-question-results-2026-07-07.jsonl](per-question-results-2026-07-07.jsonl), [report-2026-07-07.json](report-2026-07-07.json) |
| Stage-1 slice, retrieval-only change | net +7 on 172 q | `348ad3edf2524b24` | [uplift-evidence/stage1-runA-retrieval-only-checkpoint.jsonl](uplift-evidence/stage1-runA-retrieval-only-checkpoint.jsonl) |
| Stage-1 slice, full config | net +18 on 172 q (64.5%→75.0%) | `60d226be0161cc45` | [uplift-evidence/stage1-runB-full-config-checkpoint.jsonl](uplift-evidence/stage1-runB-full-config-checkpoint.jsonl) |
| 2026-07-18/19 replication, three full runs (published headline) | 81.2% mean (80.8% / 81.0% / 81.8%; 404-409 of 500) | `c5a7dbe1416cc43d` | [replication-v0.12.0-2026-07-19/](replication-v0.12.0-2026-07-19/) |

Checkpoint files are append-only evidence: quota-outage retries are retained,
and aggregation dedupes by `question_id` keeping the last row. v0.9.2 resume
fails closed when completed rows have a different fingerprint; the older
harness only warned and could produce mixed reports. The 79.4% result is a
**single run** and is no longer the headline (the three-run replication above
is); the prior campaign's
three-run variance band on a fixed config was 63.0–64.6, so treat any
single-run delta under ~2 points as noise.

## 4. Negative results, first class

Things we built or measured that did **not** work, with the numbers:

- **Session ids carried the evidence label.** In the dataset, every evidence
  session id starts with `answer_` and no filler session id does, and the
  harness showed session ids to the reader model. The effect is unmeasured.
  All three published numbers (64.6%, 79.4% and 81.2%, over five full runs)
  carry it. An anonymised rerun is planned and will replace them. See the
  known-issue note at the top of the [benchmark README](README.md).
- **Entity-graph retrieval did not move the benchmark.** A graph stage that
  provably lifts entity-name recall from 0.0 to 1.0 in our own
  `graph_hop_retrieval` eval left multi-session flat (45.1% → 42.1%, within
  noise). LongMemEval's multi-session questions aggregate everyday facts
  with no named entities to hop through.
- **Chain-of-thought reading regressed abstention.** The +74 net of the
  2026-07-07 run includes a −3 on the 30-question abstention subset
  (25/30 → 22/30): CoT makes the model more willing to answer when memory
  lacks the fact. Disclosed, not netted away.
- **More retrieval experiments did not improve this development slice.** On
  a fixed 172-question slice, an unscored FTS-only probe measured 86.6%
  all-evidence-session coverage and a vector probe measured 95.3%. The scored
  historical baseline already used vectors, so those coverage numbers and
  the answer deltas are not a paired causal experiment. Seven committed
  candidate checkpoints range from -14 to +3 net; none is statistically
  significant. This does not establish a retrieval ceiling.
- **Round-5 pack/retrieval variants were flat to sharply negative.** Four
  scored arms on the paired 172-question slice landed at net −14, −11, 0,
  and +3. Working notes label the −14 and −11 checkpoints as differing in
  roll-up-card acceptance, but their retained contexts match on only 30 of
  171 scored questions and the historical fingerprints omitted reranker
  configuration. They therefore do **not** isolate a reranker effect or a
  separate −3 card effect. The net-0 arm was recorded as a vectors-only
  control, but these component labels remain historical working
  interpretations rather than independently replayable causal evidence.
  **Every** arm lost
  knowledge-update net flips (−9, −8, −4, −3), and their measured
  stale-pick rates (see §5) were 25.0%, 31.3%, 12.5%, 12.5% against the
  published run's 3.4%. The four-arm stale-pick table is a retained working
  artifact, not fully reproduced by the four-row committed baseline file in
  §5; it must not be presented as an independently replayable release claim.

## 5. The stale-pick metric (judge-free)

Knowledge-update questions plant an update chain: an old value in an early
session, the current value later; the gold answer is the current value. The
dominant *measured* failure mode on this category is answering with the old
value even when both are retrieved. `eval/longmemeval/stale_pick.py`
measures exactly that with no LLM and no judge:

- **Chain extraction (dataset side):** gold values are parsed from the
  dataset answer; same-shape values (durations, numbers, money, weekdays,
  dates, entities) found in the question's `has_answer` evidence turns that
  do not normalize-match the gold are the chain's stale values.
- **Classification (run side):** scan the hypothesis's sentences from the
  end; the first sentence containing a chain-relevant value decides —
  **GOLD-VALUE** (gold present; gold wins same-sentence ties, matching the
  official judge's rule that mentioning the previous value is fine when the
  updated answer is given), **STALE-VALUE** (superseded value, no gold), or
  **OTHER**.
- **Rate:** STALE-VALUE / questions with an extractable chain. Chains are
  extractable for 59 of the 72 non-abstention knowledge-update questions
  (the rest — yes/no answers and untyped location phrases — are reported as
  `no_chain`, never silently dropped).
- **Validation against the official judge:** on the two full published runs
  the metric's GOLD/not-GOLD split agrees with the LLM judge on 94.9% and
  96.6% of classified questions. Broader working-arm results need a complete
  committed replay manifest before they are release evidence.

Baseline table over the committed runs (replayable offline from this repo;
also committed as
[stale-pick-baseline-2026-07-10.json](stale-pick-baseline-2026-07-10.json)):

| Run | classified | gold | stale | other | stale-pick rate |
|---|---|---|---|---|---|
| 2026-07-05 baseline (64.6%) | 59 | 46 | 9 | 4 | **15.3%** |
| 2026-07-07 published (79.4%) | 59 | 54 | 2 | 3 | **3.4%** |
| stage-1 runA (retrieval-only) | 16 | 13 | 2 | 1 | 12.5% |
| stage-1 runB (full config) | 16 | 15 | 1 | 0 | 6.3% |

This is the free regression signal any future currency/supersession work is
judged against: a pack-side change that helps must push the stale rate
toward the published 3.4%, not just move the headline.

Replay it yourself (offline, deterministic, byte-stable output):

```bash
PYTHONPATH=eval python -m longmemeval.stale_pick \
  --dataset eval/longmemeval/data/longmemeval_s_cleaned.json \
  --checkpoint docs/benchmarks/longmemeval/per-question-results.jsonl --label 2026-07-05-baseline-64.6 \
  --checkpoint docs/benchmarks/longmemeval/per-question-results-2026-07-07.jsonl --label 2026-07-07-published-79.4 \
  --checkpoint docs/benchmarks/longmemeval/uplift-evidence/stage1-runA-retrieval-only-checkpoint.jsonl --label stage1-runA-retrieval-only \
  --checkpoint docs/benchmarks/longmemeval/uplift-evidence/stage1-runB-full-config-checkpoint.jsonl --label stage1-runB-full-config
```

Boundary note: the metric reads dataset labels (`question_type`, `answer`,
`has_answer`) — that is eval tooling running post-hoc over completed
checkpoints. Nothing in `stale_pick.py` is importable from the product
answer path; `test_stale_pick_module_is_posthoc_only` pins that.

## 6. Reproduction pledge

- **Full run:** the exact commands, environment variables, and models are in
  the [README's Reproduce section](README.md#reproduce). Expected variance:
  ±2 points on the 500-question headline for a fixed config (empirical
  three-run band 63.0–64.6 on the old config); paired deltas should be
  assessed with `eval/longmemeval/compare_runs.py` (exact McNemar), not by
  comparing headlines.
- **Free replays:** the stale-pick table above and the coverage probes
  require no API keys and re-derive from committed files; identical inputs
  produce byte-identical outputs.
- **Standing commitments:** evidence files are append-only; judge and
  reading templates stay byte-frozen; config widenings are disclosed in the
  run fingerprint; negative results stay in this document. If a future run
  cannot honor one of these, the run does not get published.


## Protocol-sensitivity working artifacts

Two JSON summaries record an earlier replay of the published answers through
alternative graders. The repository does not contain the replay script,
model/prompt manifest, source-answer digest, or raw grader responses needed
to audit those summaries independently. They are listed for transparency,
but are not release evidence and support no protocol-robustness claim.

| Grading protocol | Score |
|---|---|
| Official: `gpt-4o-2024-08-06`, the benchmark's question-specific prompts | **79.4%** (397/500) |
| Grader-model swap: GPT-4.1, official prompts | 76.8% (384/500) |
| Generic lenient prompt ("conveys the right information"), GPT-4.1 | 79.2% (396/500) |

The official 79.4% row remains supported by its per-question checkpoint and
report. The other two rows should be re-generated with complete receipts
before they are interpreted.
