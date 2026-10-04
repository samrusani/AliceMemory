# Automated Memory Operations

## Requirements and status

- Support status: legacy surface. Its MCP tools are off the default handshake, and the legacy MCP surface is frozen: it gets no new capabilities. The commands, routes and tools on this page are kept working: CI runs their tests against Postgres. New integrations use the core tools.
- Backend: Postgres, for the CLI, the HTTP API and the MCP tools alike. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The four MCP tools below read the continuity store, which exists only on Postgres: on the SQLite `alice-memory` vault they are listed but their calls fail. See [Full stack](../../README.md#full-stack-postgres--review-console).
- Settings: the MCP tools need `ALICE_MCP_LEGACY_TOOLS=1` in the server environment, on a keyless server. A server bound with `ALICE_AGENT_API_KEY` never lists or accepts them. The CLI commands and the HTTP routes need no flag.

## Scope

The mutation layer is an explicit step for post-turn continuity handling. The flow separates:

- candidate generation
- operation classification
- policy gating
- deterministic commit application
- candidate-to-operation audit inspection

The shipped mutation operation types are:

- `ADD`
- `UPDATE`
- `SUPERSEDE`
- `DELETE`
- `NOOP`

## Policy

`DELETE` goes through the existing continuity correction path as a logical tombstone.

Policy decisions are stored on each mutation candidate:

- `auto_apply`
- `review_required`
- `skip`

The default gate is conservative:

- low-confidence candidates route to `review_required`
- `DELETE` routes to `review_required`
- `NOOP` routes to `skip`
- in v0.19.0, `assist` mode can `auto_apply` an explicit candidate of an allowed type at confidence 0.9 or more, and `auto` mode any candidate of an allowed type at 0.9 or more, from the user or the assistant alike

From v0.19.2, `assist` and `auto` modes can `auto_apply` only a candidate that came from the user, matched an explicit prefix (`decision:`, `preference:`, `commitment:` and the other prefixes in the capture rules) and has confidence 0.9 or more. This is the rule the `/v0/continuity` capture commit applies, and both call one function, `user_prefix_autosave`. A candidate from the assistant, or a phrase match from either role, is `review_required`. `manual` mode queues every candidate. Commit skips a `review_required` candidate unless the request sets `include_review_required`, and it checks the rule again on a row that was stored as `auto_apply` before v0.19.2. Commit does not rewrite that row: list output and a replayed generate keep showing `auto_apply` for it until it is committed, and a list filtered to `review_required` does not include it. The role is the request field that carried the text, so a caller that puts text in `user_content` is still taken at its word.

## Storage

Two audit tables back the flow:

- `memory_operation_candidates`
- `memory_operations`

The candidate row stores the classified operation, policy decision, scope, target snapshot, and sync fingerprint. The operation row stores the applied outcome, before/after snapshots, and correction linkage when the mutation was applied through continuity corrections.

## Surfaces

### API

Endpoints:

- `POST /v1/memory/operations/candidates/generate`
- `GET /v1/memory/operations/candidates`
- `POST /v1/memory/operations/commit`
- `GET /v1/memory/operations`

### CLI

Commands:

- `alicebot mutations generate`
- `alicebot mutations candidates`
- `alicebot mutations commit`
- `alicebot mutations operations`

### MCP

These are legacy MCP tools. Their requirements are at the top of this page. See also [Legacy tool surface](../alpha/mcp-tools.md#legacy-tool-surface).

Tools:

- `alice_memory_mutations_generate`
- `alice_memory_mutations_list_candidates`
- `alice_memory_mutations_commit`
- `alice_memory_mutations_list_operations`

## Matching Rules

The classifier uses scoped continuity objects and deterministic text matching:

- exact current match -> `NOOP`
- changed fact or explicit correction against a current scoped object -> `SUPERSEDE` or `UPDATE`
- destructive correction phrases against a matched object -> `DELETE`
- unmatched actionable content -> `ADD`

This keeps explicit corrections out of silent overwrite paths and records the resulting mutation decision before any apply step.
