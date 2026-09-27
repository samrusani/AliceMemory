# Phase 12 Closeout Packet

> Historical: kept for reference; not current guidance.

This runbook is the source-of-truth closeout packet for the accepted Phase 12 baseline through `P12-S5`.
The latest published tag remains `v0.2.0`; this packet prepares the completed Phase 12 boundary for a `v0.3.2` release decision.

## Required Phase 12 Go/No-Go Commands

Run these commands from repo root in order and retain outputs verbatim in the evidence bundle:

1. `python3 scripts/check_control_doc_truth.py`
2. `./.venv/bin/python -m pytest tests/unit/test_control_doc_truth.py -q`
3. `./.venv/bin/python -m pytest tests/unit tests/integration -q`
4. `pnpm --dir apps/web test`
5. `./.venv/bin/python scripts/run_hermes_memory_provider_smoke.py`
6. `./.venv/bin/python scripts/run_hermes_mcp_smoke.py`
7. `./.venv/bin/python scripts/run_hermes_bridge_demo.py`
8. `./.venv/bin/python -m alicebot_api --database-url "$DATABASE_URL" --user-id "$ALICEBOT_USER_ID" evals run --report-path eval/baselines/public_eval_harness_v1.json`

## Required PASS Evidence Bundle

- command transcript for all required go/no-go commands
- final statuses showing control-doc truth PASS, control-doc unit test PASS, Python suites PASS, web tests PASS, and Hermes smoke/demo PASS
- links to current sprint reports at repo root:
  - `docs/archive/process/BUILD_REPORT.md`
  - `docs/archive/process/REVIEW_REPORT.md`
- release-target docs for `v0.3.2`:
  - `docs/release/v0.3.2-release-checklist.md`
  - `docs/release/v0.3.2-tag-plan.md`
  - `docs/runbooks/v0.3.2-public-release-runbook.md`
- Phase 12 summary:
  - `docs/archive/process/phase12-closeout-summary.md`

## Explicit Deferred Scope Entering Next Phase

The following remain outside this closeout decision:

- post-Phase-12 feature work
- graph database migration
- marketplace or enterprise/compliance expansion
- new channels or vertical products
- `v1.0.0` compatibility/support guarantees

## Closeout Checklist

- Canonical docs describe Phase 12 as complete through `P12-S5`.
- The documented release target is `v0.3.2`.
- Docs do not falsely claim that the `v0.3.2` tag already exists.
- This closeout packet and the Phase 12 summary exist and remain referenced by control-doc truth.
- Control-doc truth guardrail and unit test pass.
