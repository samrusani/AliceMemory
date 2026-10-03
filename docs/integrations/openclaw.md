# OpenClaw Reference Integration

Use OpenClaw when you need to import existing workspace memory into Alice and then consume that imported state through the normal Alice continuity surfaces.

This path is import plus augmentation, not a second runtime.

- OpenClaw data is imported once into Alice continuity objects.
- Imported items keep explicit `OpenClaw` provenance.
- After import, use the Alice brief, recall and resume CLI commands and `POST /v1/continuity/brief`. The core MCP tools `alice_recall` and `alice_resume` do not read imported OpenClaw items: they read vNext memories and sources, and the import writes the continuity store. To recall Markdown notes over MCP, import the folder as a source; see [Import for MCP Recall](importers.md#import-for-mcp-recall).

To connect OpenClaw to Alice over MCP, run `alice-memory install` (OpenClaw is one of the four default hosts); see [Install with alice-memory](../alpha/quickstart.md#install-with-alice-memory). The instruction block for OpenClaw is in [OpenClaw skill](../alpha/openclaw-skill.md).

## One-Command Demo

Run the end-to-end import, replay, recall, and resume demo:

```bash
./scripts/use_alice_with_openclaw.sh
```

Expected JSON output:

- `status` = `pass`
- `before.recall_returned_count` = `0` for the generated demo user
- `import.first.status` = `ok`
- `import.second.status` = `noop`
- `after.recall_source_labels` includes `OpenClaw`
- `after.resume_last_decision_source_label` = `OpenClaw`
- `after.resume_next_action_source_label` = `OpenClaw`
- `checks` values are all `true`

## What Import Changes

Import augments Alice continuity retrieval. It does not replace:

- one-call continuity
- provider registration

After import, these surfaces will include OpenClaw-backed continuity when it is relevant to the request:

- API: `POST /v1/continuity/brief`
- CLI: `alice brief`, `alice recall`, `alice resume`
- MCP: only the legacy `alice_brief`, on a deliberately keyless local server with `ALICE_MCP_LEGACY_TOOLS=1`. The core `alice_recall` and `alice_resume` do not return these items.

## Import Commands

Import the file-contract fixture:

```bash
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_v1.json
```

Import the directory-contract fixture:

```bash
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_dir_v1
```

## Before And After

Before import, scoped recall and resume usually return no OpenClaw-backed continuity for that user.

After import:

- CLI recall returns imported continuity objects with `source_kind=openclaw_import`
- CLI resume returns last-decision and next-action items with `source_label=OpenClaw`
- one-call continuity can include those imported objects in the same response bundle as native Alice continuity

## Replay And Dedupe Contract

- dedupe keys are deterministic for stable payloads
- replaying the same source for the same user does not create duplicates
- repeated replay returns `status=noop`
- duplicate skips are counted in `skipped_duplicates`

## Pairing With Generic Agents

OpenClaw is usually paired with the generic API integration path.

Typical flow:

1. Import OpenClaw data into Alice.
2. Query Alice through `POST /v1/continuity/brief` or the CLI. Over MCP only the legacy `alice_brief` (deliberately keyless local server, `ALICE_MCP_LEGACY_TOOLS=1`) reads the imported items.
3. Let the agent act on Alice output while preserving OpenClaw provenance in the response.

Generic starter examples:

- `docs/examples/generic_python_agent.py`
- `docs/examples/generic_typescript_agent.ts`

## Fixture Sources

- file fixture: `fixtures/openclaw/workspace_v1.json`
- directory fixture: `fixtures/openclaw/workspace_dir_v1/`

## Related Docs

- `docs/integrations/importers.md`
- `docs/integrations/one-call-continuity.md`
- `docs/integrations/reference-paths.md`
