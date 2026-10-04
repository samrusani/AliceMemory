# Public Eval Harness

## Requirements and status

- Support status: legacy surface of the Postgres stack. It is not part of the default SQLite install. It is kept working: CI runs `evals run` against Postgres and compares the report with the checked-in baseline. New integrations use the core MCP tools.
- Backend: Postgres. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The harness needs a migrated database and a valid Alice user id (see [Full stack](../../README.md#full-stack-postgres--review-console)). `evals run` also writes its run and result rows to that database.
- Settings: none. This page names no MCP tool, so `ALICE_MCP_LEGACY_TOOLS` plays no part, and no `ALICE_LEGACY_SURFACES` flag is needed.

The public harness is a reproducible local eval surface for Alice's quality.

## Scope

The public harness measures these continuity behaviors:

- recall quality
- resumption quality
- correction behavior
- contradiction handling
- open-loop usefulness

It does not change retrieval, mutation, or contradiction behavior. It runs fixture-backed cases against the shipped surfaces and records the result.

## Canonical Inputs

- Fixture catalog: `eval/fixtures/public_eval_suites.json`
- Baseline report artifact: `eval/baselines/public_eval_harness_v1.json`

The fixture catalog is the contract for suite definitions, case ordering, and expectations used by the harness.
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

The JSON report contains:

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

That command uses the checked-in fixture catalog and emits the canonical report artifact.
