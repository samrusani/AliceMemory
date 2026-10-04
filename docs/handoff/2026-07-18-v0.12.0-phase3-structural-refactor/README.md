# Alice v0.12.0 Phase 3 Structural-Refactor Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

**Structure only. Zero behavior change.**

This directory is the control-tower handoff for the bounded Phase 3 structural
refactor. The uncommitted carrier is based on published v0.11.1 `main` commit
`f342d45dabe127acca6231f29830ff11d98a340e` (tree
`1d84e26597aecfcd9ad894c6d0b86fbe9a66dfd6`) and targets `v0.12.0` on branch
`codex/v0120-phase3-structural-refactor`.

Both governed version sources intentionally remain `0.11.1`. The release
engineer cuts them to `0.12.0` only after verifying the handoff. The tree is
intentionally uncommitted: no file is staged, and no commit, push, tag, GitHub
Release, package upload, or repository-setting mutation was performed.

Published v0.10.x/v0.11.x release notes, checksums, and prior handoffs were not
edited. Phase 4 was not started. No cybersecurity audit was performed.

## Package contents

- `SURFACE_INVENTORY.md` — base monolith sizes, final module disposition,
  stable public facades, and exact contract inventories.
- `FIX_MATRIX.md` — increment-by-increment move and fail-on-old proof.
- `BUILD_REPORT.md` — final local matrix, package-input evidence, protected
  hashes, and the independently reproduced carrier receipt.
- `ENGINEER_HANDOFF.md` — review order, invariants, reproduction commands,
  copy-ready upgrade metadata, and release-engineer-only work.
- `REVIEW_REPORT.md` — reserved for the independent final reviewer. The builder
  does not create or pre-authorize it.

## Delivered boundary

- `main.py` is 1,140 lines of application assembly, middleware, shared
  dependencies, and conditional router mounting. Domain handlers live in the
  `routers/` package. `alicebot_api.main:app` remains stable.
- PostgreSQL and SQLite vNext stores have corresponding seam modules behind
  their existing facades. The surviving legacy store is split by domain.
- Pure contracts, MCP tools, and CLI commands are split by domain behind their
  stable facades and entrypoints.
- Every production Python file is below 4,000 lines; the largest is
  `vnext_retrieval.py` at 3,803 lines.
- The default/gated OpenAPI inventories remain 182/231 operations (delta 49),
  public-response hygiene remains 296 calls, and MCP remains 11 core/65
  legacy/76 total tools.
- The default-surface integration smoke now requires at least one executed
  test, closing the carried all-skipped-green infrastructure gap without
  changing runtime behavior.

## Status

Every code increment received an independent GO with no remaining P0-P3
finding. The final builder evidence and receipt are in `BUILD_REPORT.md`; the
reviewer-owned final verdict belongs only in `REVIEW_REPORT.md`.

Local evidence does not approve publication. A final committed release SHA,
required CI and semantic attestations, version cut, fresh v0.12.0 package
artifacts, checksums, tag, GitHub Release, PyPI publication, and external
readback remain release-engineer work.

## Deferred and out of scope

- `docs/alpha/mcp-tools.md` contains pre-existing alias wording that may imply
  four legacy management verbs share direct handler identity, although only
  the commit path is a direct registry alias and management dispatches
  lifecycle actions. Correcting behavior documentation was deliberately filed
  for a later increment instead of changing claims during a mechanical move.
- Exact-SHA semantic and CodeQL evidence cannot be produced for an uncommitted
  carrier. Those are external release gates, not local claims.
- Any behavior, performance, dependency-major, Phase 4, or security work is
  outside this carrier.
