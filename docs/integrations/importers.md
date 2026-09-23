# Importer Integration

Alice ships three importer paths.

## Shipped Importers

- OpenClaw: `openclaw_import`
- Markdown: `markdown_import`
- ChatGPT export: `chatgpt_import`

## Canonical Loader Commands

```bash
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_v1.json
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_dir_v1
./scripts/load_markdown_sample_data.sh --source fixtures/importers/markdown/workspace_v1.md
./scripts/load_chatgpt_sample_data.sh --source fixtures/importers/chatgpt/workspace_v1.json
```

## OpenClaw One-Command Demo

```bash
./scripts/use_alice_with_openclaw.sh
```

See [docs/integrations/openclaw.md](openclaw.md) for end-to-end before/after output and replay expectations.

## Importer Behavior Contract

- imported records are queryable through normal recall and resume
- provenance remains explicit with importer-specific `source_kind`
- dedupe posture is deterministic per source payload
- replaying the same fixture returns noop duplicate skips
- an item that holds credential material is skipped, and the import continues
- the receipt field `skipped_credentials` is how many items were skipped
- `skipped_credential_items` names each skipped item by id or line and does not include the matched text

## Verification Example

```bash
./.venv/bin/python -m alicebot_api recall --query "MCP tool surface" --limit 5
./.venv/bin/python -m alicebot_api resume --max-recent-changes 5 --max-open-loops 5
```

## Evaluation Harness

```bash
EVAL_USER_ID="$(./.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
EVAL_USER_EMAIL="phase9-eval-${EVAL_USER_ID}@example.com"
./scripts/run_phase9_eval.sh --user-id "${EVAL_USER_ID}" --user-email "${EVAL_USER_EMAIL}" --display-name "Phase9 Eval" --report-path eval/reports/phase9_eval_latest.json
```

Evidence paths:

- `eval/baselines/phase9_s37_baseline.json`
- `eval/reports/phase9_eval_latest.json`

## Scope Guard

No additional importer families are currently shipped.
