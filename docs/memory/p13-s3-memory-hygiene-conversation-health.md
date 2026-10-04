# Memory Hygiene and Conversation Health

## Requirements and status

- Support status: legacy surface of the Postgres stack. It is not part of the default SQLite install. It is kept working: CI runs its unit and web tests. New integrations use the core MCP tools.
- Backend: Postgres. The two HTTP routes belong to the HTTP API, which runs only on Postgres, and the two web panels read those routes, so they need the API on port 8000 and the web console (see [Full stack](../../README.md#full-stack-postgres--review-console)). `alicebot status` refuses a SQLite URL.
- Settings: none beyond the Postgres setup. This page names no MCP tool, so `ALICE_MCP_LEGACY_TOOLS` plays no part.

Two bounded visibility surfaces cover memory hygiene and conversation health. They aggregate existing data and have no storage of their own.

## Shipped Surfaces

- `GET /v0/memories/hygiene-dashboard`
- `GET /v0/threads/health-dashboard`
- web memory hygiene panel in the memory review workspace
- web thread health panel in the continuity workspace
- CLI `alicebot status` extensions for hygiene and thread-health posture

## Memory Hygiene Coverage

The hygiene dashboard makes the following states visible in one summary:

- duplicate active memories grouped by normalized value and memory type
- stale facts derived from contested memories or bounded truth windows
- unresolved contradiction count from open contradiction cases
- weak-trust memory count from low-confidence, unconfirmed, or non-promotable facts
- review queue pressure derived from unlabeled queue size and queue aging

Each nonzero category ships with an explicit action string so the operator can decide what to inspect next.

## Conversation Health Coverage

The thread-health dashboard classifies threads across three dimensions:

- recent threads: last visible activity within 24 hours
- stale threads: last visible activity older than 72 hours
- risky threads: thread risk score at or above 2

Current risk score model:

- `+2` for any unresolved contradiction on a thread-scoped continuity object
- `+1` for any stale thread-scoped open-loop item
- `+1` for any active weak-inference trust signal on a thread-scoped continuity object
- `+1` when an active session remains open after more than 24 hours of inactivity

Overall thread posture is:

- `critical` when any risky thread is visible
- `watch` when stale or watch threads are visible without a risky thread
- `healthy` otherwise

## Scope Notes

- The dashboards add no connector, runtime, persistence, or retrieval substrate.
- The dashboard builders aggregate existing thread, event, continuity, contradiction, trust-signal, and review-queue data.
- Thread health is visible through both the API and the web panel.

## Verification

- unit tests for memory hygiene aggregation
- unit tests for thread-health aggregation
- page tests for the memory and continuity workspace panels
