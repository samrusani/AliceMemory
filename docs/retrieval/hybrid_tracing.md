# Hybrid Retrieval Tracing

## Requirements and status

- Support status: legacy surface. Its MCP tools are off the default handshake, and the legacy MCP surface is frozen: it gets no new capabilities. The commands, routes and tools on this page are kept working: CI runs their tests against Postgres. New integrations use the core tools.
- Backend: Postgres, for the CLI, the HTTP API and the MCP tools alike. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The three MCP tools below read the continuity store, which exists only on Postgres: on the SQLite `alice-memory` vault they are listed but their calls fail. See [Full stack](../../README.md#full-stack-postgres--review-console).
- Settings: the MCP tools need `ALICE_MCP_LEGACY_TOOLS=1` in the server environment, on a keyless server. A server bound with `ALICE_AGENT_API_KEY` never lists or accepts them. `RETRIEVAL_TRACE_RETENTION_DAYS` sets how long stored traces are kept. The CLI flags and the HTTP routes need no other flag.

The `/v0/continuity` retrieval runs as an explicit hybrid pipeline, not a single opaque ranking pass.

## Retrieval stages

- lexical: BM25-style token overlap across continuity titles, body fields, and provenance
- semantic: exact-query and semantic similarity scoring
- entity_edge: direct entity matches plus graph-expanded neighbor matches
- temporal: recency blended with requested time-window overlap
- trust: trust class, confirmation, provenance quality, and supersession posture

## Ranking behavior

- Hybrid retrieval is the default recall path of this pipeline. The core MCP `alice_recall` runs a different, vNext pipeline (see [MCP tools](../alpha/mcp-tools.md)).
- Recall and resumption payloads carry the trace only when `debug` is requested.
- Stale or superseded candidates remain eligible for inspection, but trust-aware reranking lowers their chance of outranking current truth.

## Debug visibility

- `GET /v0/continuity/recall?debug=true` returns inline stage scores, inclusion state, and exclusion reasons.
- `GET /v0/continuity/resumption-brief?debug=true` returns the same retrieval trace under `debug.retrieval`.
- `GET /v0/continuity/retrieval-runs` lists recent persisted runs.
- `GET /v0/continuity/retrieval-runs/{retrieval_run_id}` returns one stored trace.
- The CLI takes `recall --debug` and `resume --debug`.
- MCP has `alice_recall_debug`, `alice_resume_debug`, and `alice_retrieval_trace`.
  These are legacy MCP tools. Their requirements are at the top of this page. See also [Legacy tool surface](../alpha/mcp-tools.md#legacy-tool-surface).

## Persistence and retention

- Each retrieval run is stored in `retrieval_runs`.
- Per-candidate stage scores, selection state, ordering metadata, and exclusion reasons are stored in `retrieval_candidates`.
- Retrieval traces use an operator-configurable retention window via the `retention_until` timestamp.
- The current default retention is 14 days, controlled by `RETRIEVAL_TRACE_RETENTION_DAYS`.
