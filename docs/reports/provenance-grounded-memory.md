# Provenance-grounded memory: a measured result and its limits

Sami Rusani and the Alice project · 2026-07-11

> **Note added 2026-10-01.** The 79.4% result below has a known issue. In
> LongMemEval_s every evidence session id starts with `answer_`, and the
> harness showed the reader model each session's id, so the model could see
> which sessions held the evidence. The effect is unmeasured, so the 79.4%
> and the 64.6% before it may be overstated by an unknown amount. See the
> Benchmark section of the README. The text below is unchanged.

## Abstract

Alice is a local-first memory service for AI agents. Its published
LongMemEval_s run scored **79.4% (397/500)** on 2026-07-07. The paired
comparison with the preceding 64.6% run contains 96 gains and 22 losses
(net +74; exact McNemar p = 3.26e-12). The result is supported by committed
per-question rows, but it is one run and has not been replicated.

We subsequently ran exploratory retrieval, pack-format, and reader tests on
a fixed 172-question slice. Those experiments are useful engineering
evidence, but they do not form one controlled causal experiment: the 86.6%
FTS-only coverage probe was not scored, the scored baseline already used
vectors, and some arms have incomplete provenance. This report therefore
keeps the verified historical result and labels the later work narrowly.

## 1. Verified published result

The benchmark is the cleaned LongMemEval_s release: 500 questions over
synthetic conversation histories. Alice generated answers with the official
chain-of-thought reading template and used the benchmark's question-specific
judge prompts with `gpt-4o-2024-08-06`. The 2026-07-07 configuration used 16
items and a 24,000-character context budget.

| Question type | n | 2026-07-05 | 2026-07-07 |
|---|---:|---:|---:|
| single-session-user | 70 | 90.0% | 97.1% |
| single-session-assistant | 56 | 75.0% | 92.9% |
| knowledge-update | 78 | 74.4% | 89.7% |
| temporal-reasoning | 133 | 61.7% | 81.2% |
| single-session-preference | 30 | 60.0% | 70.0% |
| multi-session | 133 | 45.1% | 58.6% |
| **Overall** | **500** | **64.6% (323/500)** | **79.4% (397/500)** |

Receipts:

- [2026-07-07 per-question rows](../benchmarks/longmemeval/per-question-results-2026-07-07.jsonl)
- [2026-07-07 aggregate report](../benchmarks/longmemeval/report-2026-07-07.json)
- [paired comparison](../benchmarks/longmemeval/paired-comparison-2026-07-07.txt)
- [benchmark protocol and reproduction notes](../benchmarks/longmemeval/README.md)

The improvement combined a source-retrieval bug fix with a disclosed
configuration change. It is not evidence that one isolated feature caused
the entire gain. The chain-of-thought configuration also regressed the
30-question abstention subset from 25/30 to 22/30.

## 2. Exploratory 172-question campaign

The follow-up slice intentionally over-represents difficult categories. The
published run scores 127/172 (73.8%) on it. Seven candidate checkpoints and
their paired comparisons are committed:

| Candidate arm | Compared | Candidate | Net | Exact p |
|---|---:|---:|---:|---:|
| vectors + reranker + roll-up cards | 171 | 112/171 | -14 | 0.054 |
| vectors + reranker | 172 | 116/172 | -11 | 0.144 |
| vectors only | 172 | 127/172 | 0 | 1.000 |
| vectors + GPT-4.1 reader | 172 | 130/172 | +3 | 0.690 |
| round-6 prose packs | 172 | 124/172 | -3 | 0.690 |
| round-6 JSON packs | 172 | 127/172 | 0 | 1.000 |
| o4-mini reader working arm | 172 | 129/172 | +2 | 0.845 |

The [comparison files](../benchmarks/longmemeval/saturation-evidence/)
support those arithmetic claims. They do **not** support the stronger claim
that raising retrieval coverage from 86.6% to 95.3% left answer accuracy
unchanged in a paired experiment. The 86.6% result is an unscored FTS-only
probe; the scored baseline already had vectors. The correct conclusion is:

> On this selected slice, none of seven exploratory candidate checkpoints
> produced a statistically significant improvement over the historical
> published answers.

That result does not establish a system ceiling or prove that the reader is
the binding constraint.

### Coverage probes

The offline probe checks whether every dataset-labelled evidence **session**
appears in the retrieved pack. It does not prove that the answer-bearing
turn is visible within a selected excerpt, and abstention questions do not
have evidence-session labels.

| Retrieval configuration | all-evidence session coverage |
|---|---:|
| FTS + graph, no vectors | 86.6% |
| vectors enabled | 95.3% |
| vectors + reranker | 95.3% |

These are retrieval diagnostics, not answer scores.

### Negative-result boundaries

- The reranker arms were negative on this slice, including a large
  knowledge-update regression. This is evidence against the tested
  configuration, not against every possible reranker.
- The loose roll-up configuration produced low-quality mixed-topic cards
  and the accepted-card arm scored -14 net. The committed checkpoint records
  **1,368 proposals and 1,368 harness acceptances**, with zero recorded
  rejections. It therefore cannot support the earlier claim that the product
  review gate rejected 1,376 cards.
- The o4-mini checkpoint is retained as a working artifact. Its contexts are
  not byte-identical to the published run, and the source shaping change used
  for that arm was not on the current main lineage. It is not a frozen-pack
  replication.
- The two rejudge JSON summaries do not include the script, model/prompt
  manifest, source-answer digest, or raw responses needed for independent
  replay. This report does not use them to make a protocol-robustness claim.

## 3. What the architecture provides

Alice stores typed, revisioned memories with source provenance, explicit
review state, corrections, and supersession. Agents use it through MCP,
HTTP, or CLI. Those controls matter independently of a QA benchmark: a
durable memory write can influence future agent behavior, so writes need
authorization and review while reads need traceable evidence.

The release-hardening work for v0.9.2 adds regression coverage for
project-bound agent authorization, persisted-target lifecycle authorization,
derived-index coherence, and safe backup/restore. Those properties should be
evaluated through their security and correctness tests, not inferred from the
79.4% benchmark.

## 4. Scale evidence

The committed single-machine scale envelope measured SQLite memory commits
at 2.3-2.4 ms from 1k through 100k memories after removing a Python O(N)
idempotency scan. The corresponding pre-fix 100k result was 3.5 seconds,
about **1,460x** slower than the fixed measurement. SQLite recall with
embeddings reached 2.25 seconds at 100k; Postgres recall was about 394 ms.

These are synthetic, single-connection p50 measurements, not a concurrent
load test. See the [scale envelope](../benchmarks/scale/README.md) for the
full matrix and caveats.

## 5. Reproduction and evidence rules

Fetch and verify the dataset, then run the production capture and retrieval
pipeline through the harness:

```bash
python eval/longmemeval/fetch.py --variant s
python scripts/run_longmemeval.py --variant s --workers 3 \
  --cot --max-items 16 --context-char-budget 24000
python eval/longmemeval/compare_runs.py BASELINE.jsonl CANDIDATE.jsonl
```

New harness fingerprints include model and retrieval-provider configuration,
source commit and tracked-diff identity, question subset, budgets, pack
format, and dataset digest. Resume now rejects mismatched fingerprints.
Store reuse requires a marker tied to question content, dataset, embedding
configuration, Alice version, and ingestion-code digest.

## 6. Limitations

- The 79.4% headline is a single historical run and is not a v0.9.2
  release-candidate result.
- Multi-session remains the weakest published category at 58.6%.
- LongMemEval measures QA recall over synthetic histories, not multi-week
  agent behavior, authorization, correction safety, or review governance.
- The exploratory slice was repeatedly used during development, so its
  p-values are descriptive rather than confirmatory.
- No new paid run is claimed by this revision. It corrects the narrative to
  match evidence already committed to the repository.

If a statement cannot be traced to a committed receipt, treat it as
unproven. The [honesty kit](../benchmarks/longmemeval/HONESTY-KIT.md) records
the protocol and remaining evidence boundaries.
