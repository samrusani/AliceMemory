# Hybrid Retrieval Tracing

The Phase 12 /v0/continuity retrieval now runs as an explicit hybrid pipeline instead of a single opaque ranking pass.

## Retrieval stages

- lexical: BM25-style token overlap across continuity titles, body fields, and provenance
- semantic: exact-query and semantic similarity scoring
- entity_edge: direct entity matches plus graph-expanded neighbor matches
- temporal: recency blended with requested time-window overlap
- trust: trust class, confirmation, provenance quality, and supersession posture

## Ranking behavior

- Hybrid retrieval is the default recall path of this pipeline. The core MCP `alice_recall` runs a different, vNext pipeline (see [MCP tools](../alpha/mcp-tools.md)).
- Existing non-debug recall and resumption payloads stay compatible.
- Stale or superseded candidates remain eligible for inspection, but trust-aware reranking lowers their chance of outranking current truth.

## Debug visibility

- `GET /v0/continuity/recall?debug=true` returns inline stage scores, inclusion state, and exclusion reasons.
- `GET /v0/continuity/resumption-brief?debug=true` returns the same retrieval trace under `debug.retrieval`.
- `GET /v0/continuity/retrieval-runs` lists recent persisted runs.
- `GET /v0/continuity/retrieval-runs/{retrieval_run_id}` returns one stored trace.
- CLI adds `recall --debug` and `resume --debug`.
- MCP adds `alice_recall_debug`, `alice_resume_debug`, and `alice_retrieval_trace`.
  These are legacy MCP tools. They are listed only with `ALICE_MCP_LEGACY_TOOLS=1` on a keyless local server, never on a server bound with `ALICE_AGENT_API_KEY`, and they run only on Postgres: on SQLite they are listed but their calls fail. See [Legacy tool surface](../alpha/mcp-tools.md#legacy-tool-surface).

## Persistence and retention

- Each retrieval run is stored in `retrieval_runs`.
- Per-candidate stage scores, selection state, ordering metadata, and exclusion reasons are stored in `retrieval_candidates`.
- Retrieval traces use an operator-configurable retention window via the `retention_until` timestamp.
- The current default retention is 14 days, controlled by `RETRIEVAL_TRACE_RETENTION_DAYS`.
