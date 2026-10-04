# Local Command Walkthrough

## Requirements and status

- Support status: legacy walkthrough of the Postgres stack. It is not part of the default SQLite install. The scripts it runs are kept working: CI runs the integration tests of the Phase 9 evaluation against Postgres. New integrations use the core MCP tools.
- Backend: Postgres from `docker compose`, the repo's `.venv` from `make setup`, and the API on port 8000 for the health check (see [Full stack](../../README.md#full-stack-postgres--review-console)). The OpenClaw demo and the evaluation script write to that database. The SQLite `alice-memory` vault does not take part.
- Settings: none beyond `DATABASE_URL`, which the scripts default to the local Docker Postgres. No MCP tool is involved, so `ALICE_MCP_LEGACY_TOOLS` plays no part.

This page provides one reproducible command walkthrough using only shipped local paths.

## Scenario

1. Start local runtime.
2. Verify health.
3. Run one-command OpenClaw demo (`before -> import -> replay -> after`).
4. Generate evaluation report.

## Commands

```bash
docker compose up -d
./scripts/migrate.sh
./scripts/load_sample_data.sh
APP_RELOAD=false ./scripts/api_dev.sh
```

```bash
curl -sS http://127.0.0.1:8000/healthz
./scripts/use_alice_with_openclaw.sh
```

```bash
EVAL_USER_ID="$(./.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
EVAL_USER_EMAIL="phase9-eval-${EVAL_USER_ID}@example.com"
./scripts/run_phase9_eval.sh --user-id "${EVAL_USER_ID}" --user-email "${EVAL_USER_EMAIL}" --display-name "Phase9 Eval" --report-path eval/reports/phase9_eval_latest.json
```

## Expected Outcomes

- API health check returns `status=ok`.
- OpenClaw demo shows before/after recall-resume value and idempotent replay.
- imported provenance includes source label `OpenClaw` and `source_kind=openclaw_import`.
- evaluation harness writes report JSON with summary status and metrics.

## Notes

Use a unique `--user-email` and `--user-id` if re-running import/eval flows in the same database and you need a fresh user scope.
