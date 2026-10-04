# Alice v0.11.0 Phase 1 Periphery-Cut Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

This directory is the control-tower handoff for Phase 1 of the post-v0.10.4
work brief. The implementation is based on `main` at
`8520f29d3812aa95a75d192fdaf897e5d099a29a` and targets the unpublished
`0.11.0` candidate.

The tree is intentionally uncommitted. No files were staged, no commit or tag
was created, and nothing was pushed or published. The immutable v0.10.2,
v0.10.3, and v0.10.4 release records and migrations were not edited. Phase 2
correctness work was not started.

## Package contents

- `SURFACE_INVENTORY.md` — the required pre-edit enumeration and disposition
  across HTTP, MCP, CLI, scheduler, web, PostgreSQL, and SQLite, with factual
  implementation reconciliations discovered during collection.
- `FIX_MATRIX.md` — the Phase 1 implementation boundary and fail-on-old proof.
- `BUILD_REPORT.md` — commands, counts, coverage, package hashes, and the
  twice-reproduced frozen-tree receipt.
- `ENGINEER_HANDOFF.md` — review order, high-risk checks, and next actions.
- `REVIEW_REPORT.md` — reserved for the independent reviewer. The builder does
  not create or pre-authorize it.

## Delivered boundary

The Phase 1 sign-off deviations are now dispositioned without opening Phase 2:

- **D1 ratified:** the allowlisted ingest-only Telegram connector remains on
  the default surface.
- **D2 accepted:** v0.11.0 retains the flag-on full integration posture. A
  flag-off default-surface integration smoke job is a required Phase 2 CI
  deliverable and is not implemented in this carrier.
- **D3 closed:** core-only production startup and the adjacent environment
  validator no longer require dead S3 credentials. The healthcheck retains an
  object-storage status marker but does not echo the dormant S3 endpoint.
  Fail-on-old tests cover both production configuration paths and health
  response absence.

- Default HTTP is reduced to 182 core and adjacent operations. Exactly 49
  compatibility operations mount only when `ALICE_LEGACY_SURFACES=1`, yielding
  231 operations. Sixty-three deleted operations never remount.
- Default MCP exposes exactly eleven tools. The MCP-only legacy posture exposes
  73; both flags expose 76; agent-key-bound MCP remains eleven only.
- Telegram channel transport, hosted administration/identity/workspaces,
  design-partner launch, chief-of-staff, model-pack, and public bundled-chat
  surfaces are removed. vNext Telegram remains only as caller-supplied,
  allowlisted raw-source ingestion with no polling, delivery, token, or secret
  configuration.
- The sole local workspace creator is `POST /v1/workspaces/bootstrap`.
  Provider endpoints require prior bootstrap and local identity through
  `X-AliceBot-User-Id` or the configured local identity.
- Internal response-generation jobs remain only because
  `/v1/runtime/invoke` reuses their durable idempotency machinery. The public
  `/v0/responses` product endpoint and its acceptance chain are retired.
- The web shell defaults to seven retained views. Four compatibility views are
  server-gated by the exact legacy flag; deleted pages and API clients are
  absent. The five emptied route directories were also removed from the
  filesystem.
- Historical database tables and migrations remain immutable and inert.
  PostgreSQL carries the existing provider/local-workspace boundary; shared
  memory, retrieval, artifact, MCP, continuity, and Telegram raw-source
  behavior retains PostgreSQL/SQLite proof.
- The first independent review's twelve bounded Phase 1 findings are repaired:
  web/packaging Telegram residue, exact physical/public deletion guards,
  neutral reference vocabulary, dead limiter residue, stale web copy and page
  inventory, active-control history, lazy proxy loading, and stale positive
  test routes. Re-review then found that the canonical reference fixture still
  had incomplete nested action/trust records and nullable optional scope keys.
  The fixture now validates recursively against the complete
  `ContinuityBriefResponse` TypedDict graph, with systematic missing/extra-key
  mutations for every populated record. The final receipt in
  `BUILD_REPORT.md` supersedes both earlier digests; a fresh independent review
  is still required.

## Builder status

The implementation and builder verification are frozen on the superseding
receipt in `BUILD_REPORT.md`. Passing builder gates are evidence of
implementation readiness, not independent approval or publication readiness.
The existing reviewer report is bound to the prior receipt; independent review
of this exact superseding receipt is the next gate.
