# AliceBot evals

This page documents what each eval in this repository actually measures, what
requires a live database, and which historical numbers should *not* be read
as benchmarks.

## Continuity-task eval

CI runs a tiny fixture vault through capture, commit, session brief,
`alice_recall`, and `alice_resume`. Three named tasks must pass:

- `quote_imported_line`: the next session can quote the imported line
  as a source
- `resume_last_decision`: `alice_resume` surfaces the last committed
  decision
- `list_open_loop`: the session brief and `alice_resume` list the open
  loop

The receipt is per-task pass/fail and a count (`3/3`). This is not a
LongMemEval score. Import stays a source. Commit stays a fact.

Module: `alicebot_api.continuity_task_eval`. Tests live in
`tests/unit/test_continuity_task_eval.py` and run with the unit suite.
This gate is not part of `VNEXT_EVAL_SUITE_ORDER`.

## vNext eval harness (`alicebot eval ...`)

Implemented in `apps/api/src/alicebot_api/vnext_evals.py`. The harness was
rewritten to measure the real system: every suite either executes production
code or reports `status: "skipped"` with a reason. The overall report status
is `pass` only when every *executed* suite passed; skipped suites are listed
under `skipped_suites` and never counted as a pass. If nothing could execute,
the report status is `skipped`.

> The previous revision of this harness ran 12 suites ("recall", "temporal",
> "privacy", "prompt_injection", "context_efficiency", ...) that scored a
> synthetic fixture generator against its own invariants — no production
> retrieval, policy, or sanitization code was ever invoked, and several
> metrics (e.g. `tool_write_executed: False`) were hard-coded so they could
> not fail. Those suites and their always-green reports were deleted. Any
> historical `vnext_eval_latest.json` produced by that harness (schema
> `vnext_eval_report_v0`, generated_at `2026-05-11T00:00:00Z`) is meaningless
> and should be discarded.

Six suites (`VNEXT_EVAL_SUITE_ORDER`): `retrieval_quality`,
`correction_suppression`, `decision_recovery`, `provenance_explanation`,
`entity_resolution`, `graph_hop_retrieval`.
`--suite all` runs every one; each suite key is also individually
addressable via `--suite <key>`. All six run against either backend and
each opens (and rolls back) its own transaction, so suites stay isolated
from one another and leave no rows behind.

### Suite: `retrieval_quality`

Executes the production retrieval pipeline end-to-end against either
backend — Postgres (`PostgresVNextStore`) or the zero-infrastructure
SQLite on-ramp (`SQLiteVNextStore`), selected by the URL in
`ALICEBOT_EVAL_DATABASE_URL`:

1. Seeds a deterministic 216-memory corpus through the real store
   `create_memory` write path (inside a rolled-back transaction — nothing
   persists after the run on either backend).
2. If an embedding provider is configured (`ALICE_EMBEDDINGS_BASE_URL` +
   `ALICE_EMBEDDINGS_MODEL`), embeds every memory through the real provider
   and writes vectors into the store (pgvector column on Postgres, float32
   blob on SQLite).
3. Runs 48 queries through `VNextRetrievalService.compile_context_pack` —
   the same hybrid FTS + vector KNN + reciprocal-rank-fusion path the
   product uses (Postgres FTS + pgvector, or SQLite FTS5 + numpy cosine) —
   and scores the fused ranking.

The report labels which backend ran (`metrics.backend`:
`"postgres"` / `"sqlite"`).

Every query is phrased differently from its target memory:

- **`lexical_overlap` subset (32 queries)** — reworded questions that keep
  most of the memory's vocabulary (word order changes, inflection changes,
  dropped words). Verbatim token overlap with the target is in the ~0.6–0.85
  band. The FTS stage (Postgres FTS or SQLite FTS5) should handle these on
  its own.
- **`paraphrase` subset (16 queries)** — pure paraphrases (synonyms, reworded
  intent, e.g. memory "Q3 board pack is due Thursday..." vs. query "when is
  the quarterly board deck deadline") with near-zero verbatim overlap
  (< 0.4, mostly 0.0). FTS alone is expected to struggle here; the vector
  stage is what should recover them.

Reported metrics: recall@1, recall@5, MRR (overall and per subset), per-query
latency with p50/p95/max (wall-clock around the full context-pack compile),
`retrieval_mode` (`hybrid` / `fts_only` / `mixed`), and seeding/embedding
counts.

Enforced targets (`RETRIEVAL_QUALITY_TARGETS`):

| Target | Threshold | Enforced |
| --- | --- | --- |
| `lexical_overlap_recall_at_5` | >= 0.80 | always |
| `lexical_overlap_mrr` | >= 0.60 | always |
| `paraphrase_recall_at_5` | >= 0.70 | only when the vector stage ran (`retrieval_mode: hybrid`) |

When no embedding provider is configured the run degrades to FTS-only: the
paraphrase numbers are still measured and reported (expect ~0.0 recall — that
is the honest signal, not a bug), but the paraphrase target is not enforced.
The report always says which mode ran (`paraphrase_targets_enforced`).

The corpus is derived from hand-authored template pairs expanded by index
arithmetic — fully deterministic, no randomness at runtime. `alicebot eval
seed` writes it to `eval/fixtures/vnext_benchmark_corpus.json` if you want to
inspect it; the harness generates it in memory otherwise.

### Suite: `correction_suppression`

A regression gate over the correction lifecycle. An empirical probe showed
the production flows already behave correctly; this suite locks that in so
a future retrieval/store change cannot silently resurface corrected or
rejected memories.

Per case (6 deterministic triplets on topics disjoint from the
retrieval-quality corpus, plus 8 distractors):

1. Commit stale fact **A** through the real `VNextMemoryCommitService`
   (trusted agent identity, explicit intent, high confidence — the real
   auto-commit path) and verify through the production retrieval pipeline
   that A actually surfaces (`pre_correction_visibility` — this keeps the
   suppression claim non-vacuous).
2. Commit replacement **B**, then supersede A via the service undo path
   with a reason referencing B (`superseded_by:<B>`).
3. Commit **C** at medium confidence — which lands on the real
   inline-confirmation review path — and reject it through
   `VNextMemoryCommitService.confirm(action="reject")`.
4. Probe the production pipeline with the correction query plus targeted
   probes for A's stale detail and C's detail, and audit A and C via
   `VNextMemoryCommitService.audit`.

Metrics and targets:

| Metric | Meaning | Target |
| --- | --- | --- |
| `pre_correction_visibility` | A ranked in top-5 before correction | >= 1.0 |
| `suppression_rate` | A and C absent from *all* probe results | >= 1.0 |
| `replacement_recall_at_5` | B in top-5 for the correction query | >= 0.80 |
| `audit_completeness` | A's audit shows a reasoned `superseded` revision referencing B + the `agent.memory_undone` event; C's shows the `rejected` revision + rejection event | >= 1.0 |

### Suite: `decision_recovery`

Seeds 10 `memory_type='decision'` rows among 30 mixed-type distractors
(semantic / preference / episode / procedure / commitment / project_state /
routine — several are confusables that share the decision's topic
vocabulary, three also contain the "decided" stem) and runs decision-intent
query phrasings ("what did we decide about ...") through the production
pipeline.

Metrics and targets:

| Metric | Target |
| --- | --- |
| `decision_recall_at_5` (unfiltered, lexical-overlap phrasings) | >= 0.80 |
| `decision_recall_at_1`, `decision_mrr` | reported |
| `filtered_decision_recall_at_5` (with `memory_types=['decision']`) | >= 0.80, enforced only when the filter parameter exists |

The `memory_types` retrieval filter is **feature-detected at runtime**
(`retrieval_request_supports_memory_types()`): when
`VNextRetrievalRequest` has the parameter, the suite measures and reports
both the unfiltered and filtered variants; when it does not, it reports
unfiltered numbers plus an explicit TODO note in
`metrics.memory_types_filter.note`. (At the time of writing the parameter
has landed, so live runs report both.)

### Suite: `provenance_explanation`

Creates real source rows, commits 6 memories through
`VNextMemoryCommitService` with `source_refs` pointing at them (which
creates real provenance links), corrects 2 of them through the service
correction path, then audits every one via
`VNextMemoryCommitService.audit` and asserts concrete content:

- at least one revision with a non-empty reason,
- at least one provenance link whose `source_id` resolves to a real source
  row (unresolvable links are counted as orphans),
- the `agent.memory_committed` event in the event trail,
- for corrected memories: a `corrected` revision whose `text_after` matches
  the corrected text, the `agent.memory_corrected` event, and the
  correction recorded in the agentic metadata.

Metrics and targets:

| Metric | Target |
| --- | --- |
| `explain_completeness_rate` | >= 1.0 |
| `orphan_provenance_count` | <= 0 |

### Suite: `entity_resolution` (Sprint D)

Drives surface variants of the same entity through the REAL capture
pipeline (`VNextCaptureService.capture_text`) — e.g. "Jane Doe" and
"Dr Jane Doe" across separate sources — plus blocklist-noise probes
(repeated weekday/month capitals engineered to clear the repeat-threshold
rule so ONLY the blocklist stops them). Asserts variants canonicalize to
one entity row, mention counts match the capturing sources, honorific
variants grow the alias list, and zero noise entities exist afterward.

| Metric | Target |
| --- | --- |
| `resolution_rate` | >= 0.90 |
| `noise_entity_count` | <= 0 |
| `mention_accuracy` | >= 1.0 |
| `alias_growth_rate` | >= 1.0 |

### Suite: `graph_hop_retrieval` (Sprint D — the multi-session mechanism)

Entities are established through real capture; each truth-group's target
memory shares NO content words with its query (the entity name is the only
path); the memory→entity association is corpus ground truth. The same
production retrieval then runs twice — normally, and against a duck-type
wrapper hiding `find_entities_by_names`/`list_edges` so the graph stage
reports itself disabled. The difference is the measured contribution of
entity-hop retrieval, and every winning candidate must carry a `graph`
stage rank in the trace.

| Metric | Target |
| --- | --- |
| `graph_recall_at_5` | >= 0.80 |
| `graph_lift` (graph − fts-only) | >= 0.31 |

`fts_only_recall_at_5` is reported as the honest control (expected near
zero by corpus construction).

### How the new suites stay honest

- Every suite runs real production code (`VNextMemoryCommitService`,
  `VNextRetrievalService`) against a live store, inside the same
  rolled-back transaction discipline as `retrieval_quality`; without a
  live store they report `status: "skipped"` with a reason — never a
  fabricated pass.
- Corpora are hand-authored constants (digest-hashed, zero runtime
  randomness); directly-seeded rows pin explicit `status: "active"` and a
  fixed far-future `valid_to`, so the staleness-demotion work landing in
  the search SQL cannot silently demote eval rows.
- The unit tests prove each suite can genuinely fail by breaking one
  production behavior at a time through a delegating store wrapper
  (dropped status transitions → suppression fails; blind search → decision
  recall fails; missing revisions / unresolvable sources → provenance
  fails).

### Running against SQLite (no services)

The fastest live run needs nothing but the repo checkout — no Docker, no
Postgres:

```bash
ALICEBOT_EVAL_DATABASE_URL="sqlite:///:memory:" alicebot eval run --suite all
# or a single suite:
ALICEBOT_EVAL_DATABASE_URL="sqlite:///:memory:" alicebot eval run --suite correction_suppression
```

A file path (`sqlite:///eval.db`) works too and behaves the same: the
harness bootstraps the schema, seeds and queries inside one explicit
transaction, and rolls it back — zero rows persist afterwards (a file-backed
run leaves only the empty schema; the FTS5 index writes roll back with the
transaction, which is unit-tested). Embeddings work the same way as on
Postgres: configure `ALICE_EMBEDDINGS_BASE_URL` + `ALICE_EMBEDDINGS_MODEL`
to activate the vector stage; without them the run is FTS5-only and the
paraphrase target is reported but not enforced.

### Comparing backends

There is no special orchestration — run the suite twice with different URLs
and compare the two reports; each is labelled via `metrics.backend`:

```bash
ALICEBOT_EVAL_DATABASE_URL="postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot" \
  alicebot eval run --suite retrieval_quality --report-path eval/reports/vnext_eval_postgres.json
ALICEBOT_EVAL_DATABASE_URL="sqlite:///:memory:" \
  alicebot eval run --suite retrieval_quality --report-path eval/reports/vnext_eval_sqlite.json
```

### Running against a local Postgres

The suite needs a live, migrated Postgres (it is *skipped*, never faked,
without one):

```bash
# 1. Start the local stack (pgvector image + role bootstrap):
docker compose up -d postgres

# 2. Migrate (admin URL):
alembic upgrade head   # or your usual migration entrypoint

# 3. Point the eval at the app-role URL and run:
export ALICEBOT_EVAL_DATABASE_URL="postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot"
# optional, enables the vector stage / paraphrase target:
export ALICE_EMBEDDINGS_BASE_URL="http://localhost:11434/v1"   # any OpenAI-compatible /embeddings
export ALICE_EMBEDDINGS_MODEL="nomic-embed-text"

alicebot eval run --suite all --report-path eval/reports/vnext_eval_latest.json
```

Notes:

- `ALICEBOT_EVAL_DATABASE_URL` is a deliberate opt-in, separate from
  `DATABASE_URL`, so unit tests and unrelated CLI use never write eval
  corpora into a working database by accident. All seeded rows (memories,
  the eval user, event-log entries) are rolled back at the end of the run
  regardless.
- `ALICEBOT_AUTH_USER_ID` selects the acting user id if you need a specific
  one; otherwise a fixed default is used.
- Programmatic use: `run_retrieval_quality_eval(store)`,
  `run_correction_suppression_eval(store)`, `run_decision_recovery_eval(store)`
  and `run_provenance_explanation_eval(store)` accept any live store handle
  directly; `run_vnext_evals(...)` also accepts `store=` / `retrieval_fn=`
  injection (`retrieval_fn` only drives `retrieval_quality` — the
  commit-flow suites need a real store and skip honestly without one).
- Valid `--suite` values: `all`, `retrieval_quality`,
  `correction_suppression`, `decision_recovery`, `provenance_explanation`,
  `entity_resolution`, `graph_hop_retrieval`.
  Anything else raises `unknown vNext eval suite`. (The `--suite` help
  string in `cli.py` may lag this list; `VNEXT_EVAL_SUITE_ORDER` in
  `vnext_evals.py` is the source of truth.)

### What the unit tests cover (and don't)

`tests/unit/test_vnext_evals.py` validates the metric math (recall@k, MRR,
percentiles, RRF-ordering consistency) against fake retrieval functions with
known rankings, the corpus paraphrase properties (token-overlap bands), the
skip semantics, and the report shape. Since the SQLite backend landed, the
unit tests **also execute the full live pipeline** against
`sqlite:///:memory:` and a temp file — real seeding, FTS5 retrieval, RRF
fusion, and rollback — so the live eval path runs in CI with no external
services. They assert lexical recall@5 == 1.0 on FTS5 alone and paraphrase
recall == 0.0 without embeddings (the honest degraded number).

For the memory-quality suites the unit tests additionally execute the full
commit/review/audit flows live against SQLite (all suites end-to-end
via `--suite all` semantics, plus a file-backed run asserting zero residual
rows across `memories`, `sources`, `memory_revisions`, `provenance_links`,
`event_log`), and prove genuine failability by breaking one production
behavior at a time through delegating store wrappers.

What unit tests still cannot tell you is how the Postgres backend or a
configured embedding provider performs — for that, run the CLI against the
real thing as above.

## LongMemEval retrieval-coverage probe (`eval/longmemeval/coverage_probe.py`)

A free, keyless gate for retrieval changes. It replays the LongMemEval
harness's ingest + retrieval pipeline only (the same machinery as
`runner.py --dry-run` — no chat model, no judge) and checks, per question,
whether the *sessions* Alice retrieved include the dataset's ground-truth
evidence sessions (`answer_session_ids`; evidence turns also carry
`has_answer: true`). Retrieved sessions are collected from the context
pack's `sources` plus the provenance (`metadata_json.source_id`) of its
`relevant_memories`.

```bash
# full 500 questions, FTS-only (ALICE_EMBEDDINGS_* is scrubbed by default):
.venv/bin/python eval/longmemeval/coverage_probe.py

# slices: --limit N, --question-ids ids.txt (one id per line), --max-items K
# opt-in vector stage (needs an embedding provider): --with-vectors
```

Outputs one JSONL row per question (`any_coverage` = at least one evidence
session retrieved, `all_coverage` = every evidence session retrieved,
`n_evidence`/`n_hit`, missed session ids) plus a `.summary.json` and a
printed any%/all% table per question type. Questions with no evidence ids
are reported with `null` coverage and excluded from the percentages
(abstention questions in the real dataset *do* carry evidence ids and are
scored; rows carry `is_abstention` for slicing).

Per-question SQLite stores live under `--work-dir` (default
`eval/longmemeval/work/coverage`, runner-compatible naming) and are kept
with an `*.ingested.json` marker, so re-probing after a retrieval change
skips ingest entirely and takes seconds instead of minutes. Same inputs
produce the same numbers: ingest and FTS retrieval are deterministic, and
the probe never reads `question_type` (or any benchmark label) on the
retrieval path — labels are used for reporting only.

## Legacy baselines in `eval/baselines/` — read with caution

These files are historical snapshots, **not benchmarks**:

- **`public_eval_harness_v1.json`** / **`eval/fixtures/public_eval_suites.json`**
  (the `alicebot evals ...` public harness, `public_evals.py`): the recall
  suite is 7 hand-authored fixtures with only 0–3 candidate memories each —
  far too small to measure retrieval quality. One case
  (`entity_edge_expansion_recovers_related_owner`) scores **0.0** yet is
  counted as `pass` because the fixture sets
  `require_expected_top_result: false`. The headline "pass rate 1.0 /
  average score 0.857" therefore includes a case that found nothing.
- **`retrieval_eval_hybrid_v2.json`**: 6 fixtures with 1–3 candidates each;
  `precision_at_1_mean: 1.0` over a candidate pool this small says nothing
  about ranking at realistic corpus sizes.
- **`phase9_s37_baseline.json`**: an importer/dedupe smoke snapshot, not a
  quality eval.

None of these numbers should be quoted as retrieval quality. For an actual
measurement, run the `retrieval_quality` suite above against a live store
with embeddings configured.
