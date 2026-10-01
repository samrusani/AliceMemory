# P12-S2 Automated Memory Operations

## Scope

This sprint adds an explicit mutation layer for post-turn continuity handling. The new flow separates:

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

Current branch behavior routes `DELETE` through the existing continuity correction path as a logical tombstone. Control Tower still owns whether that remains the settled product contract for Phase 12.

Policy decisions are stored on each mutation candidate:

- `auto_apply`
- `review_required`
- `skip`

The default gate is conservative:

- low-confidence candidates route to `review_required`
- `DELETE` routes to `review_required`
- `NOOP` routes to `skip`
- in v0.19.0, `assist` mode can `auto_apply` an explicit candidate of an allowed type at confidence 0.9 or more, and `auto` mode any candidate of an allowed type at 0.9 or more, from the user or the assistant alike

Unreleased (on main, not in v0.19.0): `assist` and `auto` modes can `auto_apply` only a candidate that came from the user, matched an explicit prefix (`decision:`, `preference:`, `commitment:` and the other prefixes in the capture rules) and has confidence 0.9 or more. This is the rule the `/v0/continuity` capture commit applies, and both call one function, `user_prefix_autosave`. A candidate from the assistant, or a phrase match from either role, is `review_required`. `manual` mode queues every candidate. Commit skips a `review_required` candidate unless the request sets `include_review_required`, and it checks the rule again on a row that was stored as `auto_apply` before this change. Commit does not rewrite that row: list output and a replayed generate keep showing `auto_apply` for it until it is committed, and a list filtered to `review_required` does not include it. The role is the request field that carried the text, so a caller that puts text in `user_content` is still taken at its word.

## Storage

Two new audit tables back the flow:

- `memory_operation_candidates`
- `memory_operations`

The candidate row stores the classified operation, policy decision, scope, target snapshot, and sync fingerprint. The operation row stores the applied outcome, before/after snapshots, and correction linkage when the mutation was applied through continuity corrections.

## Surfaces

### API

Current branch endpoints, pending Control Tower confirmation of the final Phase 12 API shape:

- `POST /v1/memory/operations/candidates/generate`
- `GET /v1/memory/operations/candidates`
- `POST /v1/memory/operations/commit`
- `GET /v1/memory/operations`

### CLI

New commands:

- `alicebot mutations generate`
- `alicebot mutations candidates`
- `alicebot mutations commit`
- `alicebot mutations operations`

### MCP

New tools:

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
