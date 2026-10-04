# Task-Adaptive Briefing (Legacy Compatibility)

## Requirements and status

- Support status: legacy surface. Its MCP tools are off the default handshake, and the HTTP routes and the CLI command do not exist unless the flag below is set. The surface is deprecated for removal before `1.0`, and until then it is kept working: CI runs its tests against Postgres with the flag set. New integrations use the core tools.
- Backend: Postgres, for the CLI, the HTTP API and the MCP tools alike. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The three MCP tools below read the continuity store, which exists only on Postgres: on the SQLite `alice-memory` vault they are listed but their calls fail. See [Full stack](../../README.md#full-stack-postgres--review-console).
- Settings: `ALICE_LEGACY_SURFACES=1` at process start for the HTTP routes and the CLI command. The three MCP tools need `ALICE_LEGACY_SURFACES=1` and `ALICE_MCP_LEGACY_TOOLS=1` in the server environment, on a keyless server. A server bound with `ALICE_AGENT_API_KEY` never lists or accepts them.

Task briefs compile deterministic, explainable context packs for `user_recall`,
`resume`, `worker_subtask`, and `agent_handoff`. The feature is not part of the
default surface.

## Mount contract

- HTTP and CLI task-brief adapters require `ALICE_LEGACY_SURFACES=1` at process
  start.
- MCP `alice_task_brief`, `alice_task_brief_show`, and
  `alice_task_brief_compare` require both `ALICE_LEGACY_SURFACES=1` and
  `ALICE_MCP_LEGACY_TOOLS=1` on a keyless local server.
- Key-bound MCP never exposes these tools.
- This compatibility surface is deprecated for removal before `1.0`.

## Deterministic contract

Each brief records its mode, explicit strategy, token budget, section selection
rules, truncation counts, and a deterministic digest. Token budget resolves from
an explicit request first, then the mode default adjusted by the requested
`balanced`, `compact`, or `detailed` strategy.

Model-pack fields and workspace model-pack binding behavior were removed in
v0.11. The briefing layer still reads the surviving retrieval/resumption
services and does not replace the canonical memory system.

## Compatibility routes

- `POST /v0/task-briefs/compile`
- `GET /v0/task-briefs/{task_brief_id}`
- `POST /v0/task-briefs/compare`

These routes are absent from default OpenAPI and return `404` unless the legacy
surface flag is enabled before application construction.
