# CLI Integration

The shipped CLI surface runs against the same local runtime used by API and MCP.

## Entrypoints

```bash
./.venv/bin/python -m alicebot_api --help
alicebot --help
```

`alicebot` is available after editable install (`pip install -e '.[dev]'`).

## User Scope

- default user scope comes from `ALICEBOT_AUTH_USER_ID`
- fallback default if unset: `00000000-0000-0000-0000-000000000001`

## Core Commands

```bash
alice brief --brief-type general --query local-first
./.venv/bin/python -m alicebot_api status
./.venv/bin/python -m alicebot_api capture "Decision: Keep Alice local-first for verification." --explicit-signal decision
./.venv/bin/python -m alicebot_api recall --query local-first --limit 5
./.venv/bin/python -m alicebot_api resume --max-recent-changes 5 --max-open-loops 5
./.venv/bin/python -m alicebot_api brief --brief-type general --query local-first
./.venv/bin/python -m alicebot_api open-loops
./.venv/bin/python -m alicebot_api state-at <entity_id> --at 2026-03-12T09:45:00+00:00
./.venv/bin/python -m alicebot_api timeline <entity_id> --limit 20
./.venv/bin/python -m alicebot_api explain --entity-id <entity_id> --at 2026-03-12T09:45:00+00:00
```

`brief` is the default external-agent continuity entrypoint. It assembles relevant facts, recent changes, open loops, conflicts, timeline highlights, provenance, trust posture, and a next suggested action in one call.

## Agent API Keys

Agent HTTP calls authenticate with per-agent API keys:

```bash
alicebot agent keys create --agent-id openclaw --profile project_scoped_agent --label "openclaw laptop"
alicebot agent keys list
alicebot agent keys revoke <key-prefix-or-id>
```

The raw key is printed exactly once at creation and stored hashed. Agents send it as `Authorization: Bearer $ALICE_AGENT_API_KEY`; MCP binds a key through the `ALICE_AGENT_API_KEY` environment variable. The key record overrides any payload-supplied agent identity, payloads may only downgrade the granted permission profile, and keyless agent calls work only while zero active keys exist.

## vNext Agentic Commands

```bash
alicebot context-pack "Alice sprint context" --domain project
alicebot vnext agents propose-memory --agent-id openclaw --permission-profile project_scoped_agent --project-scope Alice --title "Candidate memory" --canonical-text "Alice should keep agent proposals review-only." --domain project --sensitivity private
alicebot vnext scheduler status
alicebot vnext scheduler daemon start --foreground
alicebot vnext scheduler daemon status
alicebot vnext scheduler daemon stop
alicebot vnext scheduler run-now daily_brief --agent-id hermes --permission-profile trusted_local_agent --project-scope Alice --domain project --generation-mode model_backed --model-route-mode local_only
alicebot vnext scheduler run-due --agent-id hermes --permission-profile trusted_local_agent
alicebot vnext scheduler runs
alicebot vnext scheduler failures
alicebot vnext scheduler pause --agent-id hermes --permission-profile trusted_local_agent
alicebot vnext scheduler resume --agent-id hermes --permission-profile trusted_local_agent
alicebot vnext quality rate <artifact_id> --usefulness 5 --accuracy 5 --source-grounding 5 --novel-connections 4 --actionability 4 --hallucination-risk 1 --verbosity right_sized --comments "Grounded and useful."
alicebot vnext quality export --limit 50
alicebot vnext agents policy-telemetry
alicebot vnext connectors list
alicebot vnext connectors status
alicebot vnext connectors health
alicebot vnext connectors local-folder add-path ~/Notes/Alice --extension .md --extension .txt
alicebot vnext connectors local-folder sync
alicebot vnext connectors local-folder watch --once
alicebot vnext connectors browser-clipper capture --url https://example.test/page --selected-text "Fact: browser clips are reviewable." --capture-token "$ALICE_BROWSER_CLIP_TOKEN"
alicebot vnext agents ingest-output --agent-id openclaw --agent-type coding_agent --title "Sprint summary" --propose-memory "Decision: agent output stays review-only."
alicebot vnext dogfooding dashboard
alicebot vnext migrations status
alicebot vnext doctor --fix-safe
alicebot vnext quality insight <artifact_id> --useful-insight yes
alicebot vnext smoke agentic-scheduler
alicebot vnext smoke local-runtime
alicebot vnext smoke model-backed
alicebot vnext smoke live-capture-connectors
alicebot vnext smoke capture-to-brief
alicebot vnext smoke connector-hardening
alicebot vnext smoke secret-redaction
alicebot vnext smoke dogfood-doctor
alicebot vnext smoke operator-console
alicebot vnext smoke agent-integration-pack
alicebot vnext alpha check
alicebot vnext demo load --reset
```

The vNext agent arguments are `--agent-id`, `--agent-type`, `--agent-run-id`, `--agent-task-id`, `--project-scope`, and `--permission-profile`. Agent-originated scheduler and memory-proposal commands are policy checked, logged, and kept review-only where they create memory or generated artifacts.

Model-backed generation arguments are available on daily brief, weekly synthesis, connection report, contradiction report, project update candidate, and scheduler run-now commands: `--generation-mode`, `--model-route-mode`, `--model-provider`, `--model`, `--model-temperature`, and `--allow-cloud-private`. Private and highly sensitive scopes remain local-only or disabled unless explicitly configured.

Live capture connector commands preserve the same trust model as manual capture: raw source text is archived, domain/sensitivity defaults are explicit, source material is treated as untrusted, agent output produces review-only artifacts/proposals, and capture-to-brief promotion still requires human review. Connector settings and state now persist outside the event log, while settings/state changes still write audit events. Secret values are never printed; the CLI stores or resolves only `secret_ref` values.

`alicebot vnext alpha check` is the public-preview readiness gate. It summarizes migrations, doctor, scheduler posture, connector settings/state storage, core vNext smokes, agent integration pack smoke, and the eval command expected for release evidence.

`alicebot vnext demo load --reset` loads the safe synthetic public-preview dataset from `fixtures/vnext/demo_dataset.json`; `alicebot vnext demo reset` archives rows from that dataset.

The `operator-console` smoke is the broadest local go/no-go check for daily `/vnext` operation. It verifies source review, memory review, artifact review/rating, source-backed open-loop creation, scheduler run-now artifact creation, connector health visibility, doctor readiness, event logging, and source-to-brief traceability.

## Temporal History Commands

- `state-at` reconstructs entity facts plus effective edges at a prior time
- `timeline` returns chronological entity history from fact revisions and edge lifecycle rows
- `explain --entity-id` adds trust, provenance, and supersession-chain detail for the reconstructed state

## Review and Correction Commands

```bash
./.venv/bin/python -m alicebot_api review queue --status correction_ready --limit 20
./.venv/bin/python -m alicebot_api review show <continuity_object_id>
./.venv/bin/python -m alicebot_api review apply <continuity_object_id> --action supersede --replacement-title "Decision: Updated title" --replacement-body-json '{"decision_text":"Updated title"}' --replacement-provenance-json '{"thread_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}' --replacement-confidence 0.97
```

## Determinism Contract

- output format is deterministic for stable automated validation
- `alice`/`alicebot` and `alice-memory` write argument, boundary-validation,
  and unhandled execution failures to stderr as one compact JSON line:
  `{"error":{"code":"...","message":"..."}}`; messages are static and
  never contain exception text, exception types, tracebacks, paths, record
  identifiers, or rejected input
- handled `EvalGateFailure`, `EmbeddingBackfillFailure`, and
  `PartialCommandFailure` cases emit their structured failure report on stdout
  and return nonzero; they are not part of the one-line stderr guarantee
- `alice-memory` preserves distinct committed-state codes so automation can
  distinguish a rollback-safe failure from a restore that already committed:
  `restore_failed`, `restore_committed_hardening_failed`,
  `restore_committed_summary_failed`, and
  `restore_committed_hardening_and_summary_failed`
- other SQLite boundary codes distinguish invalid input, source/path conflicts,
  snapshot/validation failures, export failures, provider configuration, and
  embedding-batch failures; failed batch/result objects expose an
  `error_code` with a static companion message
- provenance snippets remain visible in recall/resume responses
- correction flow updates future recall/resume results
- vNext scheduler, connector-hardening, secret-redaction, doctor, and model-backed smoke output is JSON and fails nonzero if any gate fails

See tests:

- `tests/integration/test_mcp_cli_parity.py`
- `tests/integration/test_mcp_server.py`
- `tests/integration/test_temporal_state_mcp_cli.py`
- `docs/integrations/one-call-continuity.md`

## SQLite Markdown replacement

Unreleased (on main, not in v0.20.0):

```bash
alice-memory import-markdown --from PATH [--supersede | --no-supersede] [--dry-run] [--allow-looser-classification]
```

`--no-supersede` is the default. `--supersede` replaces earlier Markdown imports
of the resolved path in one transaction per file. `--dry-run` reports the
planned result and rolls back database writes. `--allow-looser-classification`
allows a lower sensitivity or a changed domain during replacement, never a
project-scope change. Omitted `--domain` and `--sensitivity` keep a known path's
labels; new files default to `unknown`. See [importer details](importers.md#replacement-on-re-import)
for duplicate outcomes, receipts, derived-state cleanup and recovery limits.
These flags are absent from the Postgres `alice vnext sources` commands.
