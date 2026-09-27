# Phase 9 Public Release Runbook

> Historical: kept for reference; not current guidance.

This runbook executes `P9-S38` release readiness for the first public tag.

## Objective

Cut a credible public release from shipped local functionality with reproducible verification evidence.

## Scope Guard

Do not add features during this runbook. Only launch docs/release readiness adjustments are allowed.

## Step 1: Start Runtime

```bash
docker compose up -d
./scripts/migrate.sh
./scripts/load_sample_data.sh
```

## Step 2: Start API and Verify Health

```bash
APP_RELOAD=false ./scripts/api_dev.sh
```

In separate terminal:

```bash
curl -sS http://127.0.0.1:8000/healthz
```

## Step 3: Verify Test and Eval Gates

```bash
./.venv/bin/python -m pytest tests/unit tests/integration
pnpm --dir apps/web test
EVAL_USER_ID="$(./.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
EVAL_USER_EMAIL="phase9-eval-${EVAL_USER_ID}@example.com"
./scripts/run_phase9_eval.sh --user-id "${EVAL_USER_ID}" --user-email "${EVAL_USER_EMAIL}" --display-name "Phase9 Eval" --report-path eval/reports/phase9_eval_latest.json
```

## Step 4: Verify Documentation Surfaces

- `README.md`
- `docs/quickstart/local-setup-and-first-result.md`
- `docs/integrations/cli.md`
- `docs/integrations/mcp.md`
- `docs/integrations/importers.md`
- `docs/release/v0.1.0-release-checklist.md`
- `docs/release/v0.1.0-tag-plan.md`

Ensure all commands and claims are executable and evidence-backed.

## Step 5: Record Sprint Reports

- update `docs/archive/process/BUILD_REPORT.md` with command evidence and outcomes
- update `docs/archive/process/REVIEW_REPORT.md` with sprint review state

## Step 6: Execute Release Checklist

Complete all checks in `docs/release/v0.1.0-release-checklist.md`.

## Step 7: Tag Gate

After checklist pass + reviewer `PASS` + explicit maintainer approval, follow:

- `docs/release/v0.1.0-tag-plan.md`

## Failure Handling

- If a required command fails, stop tag prep.
- Fix only launch/doc/release-readiness issues inside `P9-S38` scope.
- Re-run failed gates and update `docs/archive/process/BUILD_REPORT.md` before retry.
