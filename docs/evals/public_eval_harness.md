# Public Eval Harness

`P12-S4` turns Alice quality into a reproducible local eval surface.

## Scope

The public harness measures the shipped Phase 12 continuity behaviors that this sprint is allowed to inspect:

- recall quality
- resumption quality
- correction behavior
- contradiction handling
- open-loop usefulness

It does not reopen retrieval, mutation, or contradiction implementations. It runs fixture-backed cases against the shipped surfaces and records the result.

## Canonical Inputs

- Fixture catalog: `eval/fixtures/public_eval_suites.json`
- Current branch baseline report artifact: `eval/baselines/public_eval_harness_v1.json`

The fixture catalog is the current branch contract for suite definitions, case ordering, and expectations used by the harness.
`evals suites` reads directly from that checked-in catalog. `evals run` syncs the persisted suite/case tables to the current catalog before storing the run and result rows, so renamed or removed catalog entries do not survive as hidden runtime state.

## Surfaces

- CLI:
  - `python -m alicebot_api evals suites`
  - `python -m alicebot_api evals run --report-path eval/baselines/public_eval_harness_v1.json`
  - `python -m alicebot_api evals runs --limit 10`
  - `python -m alicebot_api evals show <eval_run_id>`
- API (the local `/v1` surface):
  - `GET /v1/evals/suites`
  - `POST /v1/evals/runs`
  - `GET /v1/evals/runs`
  - `GET /v1/evals/runs/{eval_run_id}`

## Report Format

The current branch JSON report contains:

- `schema_version`
- `fixture_schema_version`
- `fixture_source_path`
- `summary`
- `suites`

The report intentionally excludes run-specific timestamps and ids so the checked-in baseline stays stable across repeated local runs. Persisted run records keep ids, timestamps, and the report digest separately in the database.

## Metrics

- Recall:
  suite pass rate across the public retrieval fixture corpus, with per-case precision and lift details.
- Resumption:
  expected last decision, next action, open loops, and recent changes from fixture state.
- Correction:
  expected lifecycle mutation and replacement-object creation for review actions.
- Contradiction:
  expected open-case count, contradiction kind, and active trust-signal posture.
- Open loop:
  expected posture grouping and deterministic item ordering.

## Interpreting The Baseline

The baseline report is evidence, not aspiration.

- A passing suite means the current shipped behavior matches the fixture expectations checked into the catalog.
- A low case score can still appear inside a passing suite when the fixture is recorded as a coverage snapshot instead of a strict gate.
- Unknown `suite_key` filters fail fast instead of silently falling back to a partial run.
- Any fixture or expectation change should regenerate the baseline report in the same change.

## Reproducing The Baseline

Run the harness with a migrated local database and a valid Alice user id:

```bash
python -m alicebot_api \
  --database-url "$DATABASE_URL" \
  --user-id "$ALICEBOT_USER_ID" \
  evals run \
  --report-path eval/baselines/public_eval_harness_v1.json
```

That command uses the checked-in fixture catalog and emits the canonical report artifact used by the sprint verification.
