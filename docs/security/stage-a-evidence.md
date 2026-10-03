# Stage A Evidence Ledger

## Epistemic Status

This is team-authored preparation, not an external or independent audit. It was
started from `main` at
`c9d24243920a694eaf00ad595da392a1478710dd`. The focused Stage A matrix below
was reproduced on the now-superseded pre-commit carrier. Commit `e8d2018`
subsequently failed CI, so receipt `4cf7e08b...` is not shippable and the
repaired carrier needs fresh evidence. A result from the base, the superseded
working tree, or the historical scan does not transfer automatically to a
later tree.

The owner has accepted Phase 5.1.c at the claim bar "automated security
scanning and internal adversarial review, findings triaged and fixed." This
closes the owner disposition at that stated bar; it does not assert that an
external review occurred, that the carrier was independently audited, or that
the open proof gaps below disappeared. Stage B history is retained to make that
distinction auditable.

> **Correction (2026-10-04):** the sentences above that say a receipt is
> superseded and the repaired carrier needs fresh evidence describe 2026-07-22.
> v0.14.0 was published on 2026-07-24 after the owner recorded the real-host
> receipt (29 of 29 checks), so the release no longer waits on fresh Stage A
> receipts. The public security claim is: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code.

## Surface Closure

| Surface | Base | Superseded carrier result | Acceptance evidence |
| --- | ---: | ---: | --- |
| Default HTTP operations | 182 | 183 | `tests/integration/test_default_surface_integration.py` and OpenAPI closure tests |
| `/v0/vnext` operations | 70 | 71 | route-policy inventory in `tests/unit/test_vnext_main.py` |
| HTTP operations with legacy surfaces | 231 | 232 | `tests/unit/test_legacy_gated_router_split.py` |
| Core MCP tools | 11 | 11 | MCP registry/sentinel tests |
| Legacy HTTP delta | 49 | 49 | default/gated exact-set comparison |
| Direct `public_exception_response` calls | 298 | 298 | per-module manifest in `tests/unit/test_public_errors.py` |

The deltas reflect one new browser-clipper capability-issuance route. Focused
closure tests reproduced the exact sets and counts on the superseded carrier.
The repaired carrier and eventual release commit must reproduce them again; no
count may be updated merely to make a gate pass.

## Evidence Areas

| Area | Implemented/test evidence | Open acceptance or proof gap |
| --- | --- | --- |
| Authentication and authorization | Central vNext auth gate, key-bound actor/profile/scope, RLS, central-route classification, default/legacy gating, core MCP sentinels, and an all-71-route ASGI keyless-rejection matrix. | Re-run the exact route inventory and live per-user RLS/key-isolation tests on the release commit. |
| Browser clipper | Carrier replaces reusable page-context token with a short-lived, origin-bound, one-time capability, bounded simple-request transport, sanitized validation, value-based taint redaction, and hash-only persistence. | Focused tests reproduced first use, replay/reload, cross-origin, expiry, tamper, atomic concurrent double redemption, and absence from persisted/error surfaces; repeat them on the release commit and retain browser-policy limitations. |
| Input/injection | Fail-closed vNext models; parameterized store SQL; SQL-shape tests; hostile FTS tests; SQLite portable-import snapshot/alias tests. | Directory importers have an outside-root symlink and archive/parse TOCTOU gap; PostgreSQL hostile FTS coverage should be broadened. |
| Secrets/errors | Agent-key hash verifier, provider secret references, recursive key/value redaction, sanitized provider errors, stable public-error vocabulary and AST manifest. Raw-agent-key log and provider-key non-echo sentinels passed on the superseded carrier. | Re-run on the repaired carrier and release commit; redaction pre-read still leaves transient plaintext in RAM. |
| Dependencies | Exact web package versions and lockfile; production/full npm bulk audits; Dependabot; CodeQL; Gitleaks; SHA-pinned Actions. | No fail-closed Python advisory audit or fully locked Python application graph. |
| Configuration | Production config checks, explicit CORS, loopback defaults, security headers. | `get_settings()` is first-caller-wins per process; document for embedders. |

Corrected 2026-10-04: the "Directory importers have an outside-root symlink and
archive/parse TOCTOU gap" entry in the Input/injection row and the section
"Deferred Finding: Directory Import Source Integrity" are kept as recorded for
the 5.1 carrier. v0.15.2 fixed the symlink escape and the archive/parse
mismatch. Two residuals remain, a hard link and a folder on the path swapped for
a symlink between the listing and the read, and
[input validation](input-validation.md) describes them.

## Reproduction Commands

Run with the repository's supported environment and exact dependency state.
PostgreSQL integration commands require the documented role-separated test
database. Treat skipped security tests as missing evidence, not a pass.

```bash
./.venv/bin/pytest -q \
  tests/unit/test_vnext_agent_keys.py \
  tests/unit/test_stage_a_vnext_auth_surface.py \
  tests/unit/test_vnext_main.py \
  tests/unit/test_mcp.py \
  tests/unit/test_public_errors.py \
  tests/unit/test_browser_clip_capabilities.py \
  tests/unit/test_browser_clip_capability_storage.py \
  tests/unit/test_20260721_0094_browser_clip_capabilities.py \
  tests/unit/test_provider_security.py \
  tests/unit/test_provider_secrets.py \
  tests/unit/test_vnext_secrets.py \
  tests/unit/test_importers.py \
  tests/unit/test_sqlite_onramp.py

./.venv/bin/pytest -q \
  tests/integration/test_default_surface_integration.py \
  tests/integration/test_stage_a_agent_key_isolation.py \
  tests/integration/test_browser_clip_capabilities.py \
  tests/integration/test_http_security_posture.py \
  tests/integration/test_source_content_retrieval_postgres.py \
  tests/integration/test_vnext_fts_fallback_postgres.py

pnpm --dir apps/web test
pnpm --dir apps/web test:browser

(cd apps/web && pnpm test:advisory-audit)
(cd apps/web && node scripts/npm-advisory-audit.mjs --prod --audit-level=high)
(cd apps/web && node scripts/npm-advisory-audit.mjs --audit-level=high)
```

The full release matrix, migration tests, coverage floors, OpenAPI registry
closure, and both integration postures remain required in addition to this
focused set.

## Historical Scan Provenance

The prior deep scan targeted commit
`2c372417e1d07d072265d2efdfbdace04f8bfcbb`. Its 41 canonical findings were
expanded into 156 report instances (24 Medium and 132 Low; no High/Critical),
all Medium-confidence with partial coverage and proof gaps. It is input to this
ledger, not evidence about the final carrier and not an external or independent
review.

## Deferred Finding: Directory Import Source Integrity

The Markdown, ChatGPT, and OpenClaw directory importers enumerate path objects,
archive content, and later reopen the selected source for parsing. They do not
currently reject every symlinked member or guarantee that the parsed bytes are
the bytes whose archive checksum was recorded. A source controlled by another
local actor can therefore escape the selected root or be substituted between
reads.

This is deliberately documented, not fixed, in the 5.1 carrier. Until a later
remediation lands, operators must stage imports in a private, owner-controlled,
non-symlinked directory and stop writers before import. Any future adversarial
review should validate reachability and severity if this issue is revisited,
including Markdown recursive discovery, ChatGPT recursive JSON discovery, and
OpenClaw named/fallback JSON discovery. The owner-accepted Phase 5.1.c
disposition does not silently close this documented gap.

## Stage A Exit Rule

Stage A is ready to hand to the owner only when the final-carrier commands and
full release matrix pass without security-relevant skips, target operation
counts are reproduced, the browser-clipper negative tests pass for both stores,
and every remaining gap is explicitly retained. The owner has recorded the
Stage B disposition at the automated-scan plus internal adversarial-review bar.
Phase 5 still requires green committed-SHA CI and the 5.4 real-host receipt;
these documents grant neither.

> **Correction (2026-10-04):** the last sentence above describes 2026-07-22.
> v0.14.0 was published on 2026-07-24 after the owner recorded the real-host
> receipt (29 of 29 checks), so neither gate is open for that release.
