# Rules

These are the standing rules for building Alice. They replace the earlier phase/sprint gate rules.

## Product Focus
- The customer is the agent developer. Every feature must make it easier for an agent to store, retrieve, resume, or explain memory through API, CLI, or MCP.
- Human-facing knowledge tooling is a possible later product on top of the agent surface, not the current target.
- The review console exists as the trust boundary for durable memory; treat review-governed writes as a feature, not friction.

## One Memory System
- The vNext store is the canonical memory system. Legacy memory surfaces are deprecated and must not gain new features.
- Do not add a second memory model, a parallel store, or provider-specific forks of continuity semantics.
- Continuity semantics must not fork by provider. Providers may change capability, latency, and quirks — never the object model, provenance contracts, or trust semantics.

## No Fake Intelligence
- No hash-based or otherwise fake embeddings. Vectors come from a real embedding model or the feature degrades explicitly (full-text only, with a visible trace note).
- No template output presented as model output. Deterministic output must be labeled deterministic.
- No eval that does not execute production code. Evals must run the real retrieval/commit pipeline end to end, not a simulation of it.
- Commit evidence only from exact commands and stable fixtures, never from inferred pass states.

## Evals
- Evals measure the real pipeline: real store, real retrieval fusion, real policy engine.
- A change that affects retrieval or memory-commit behavior needs eval results from the production code path before it merges.

## Interfaces
- The core MCP surface stays small. New tools need a reason the existing eleven cannot cover. The default handshake exposes three of them. Retained long-tail memory tools require `ALICE_MCP_LEGACY_TOOLS=1`; exactly the three task-brief tools additionally require `ALICE_LEGACY_SURFACES=1`. Every compatibility tool is limited to a keyless local-operator deployment; key-bound MCP is core-only and fails closed.
- Every new tool, endpoint, or CLI command ships with parameter descriptions. No undocumented parameters.
- Agent access to the HTTP API is authenticated with per-agent API keys.

## Durable Invariants
- Keep credentials, tokens, and secret references out of logs, docs, and outward-facing errors.
- Alice is local-first and single-workspace by default. Do not add hosted identity,
  tenancy, or control-plane tables to the current product. If a future hosted
  offering is separately approved, it needs its own threat model, row-level
  isolation contract, migration plan, and regression suite before it may share
  this repository's runtime.
- Provenance is preserved: memory writes, corrections, and supersessions keep their links to source evidence and review history.
- Migrations are additive-first: no destructive schema change without an explicit, documented migration and rollback path.
- Keep local filesystem paths, workstation usernames, and machine-specific identifiers out of committed docs and reports.
- Keep consequential side effects approval-bounded.

## Docs
- Docs describe shipped behavior. Aspirational features belong in ROADMAP.md, clearly marked as not shipped.
- Never claim a hosted service, published SDKs, automatic conversation capture, or OCR/transcription execution unless it actually ships.
- Keep CURRENT_STATE.md factual and short; keep ROADMAP.md future-facing and short.
- Historical build and repair ledgers belong under `docs/handoff/`, not in the
  live roadmap or current-state summary.
