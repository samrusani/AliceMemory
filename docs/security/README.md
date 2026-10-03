# Security Review Evidence

This directory is Alice's durable security-evidence package. It preserves the
Stage A preparation, the Stage B review history, the owner's Phase 5.1.c
disposition, and the remaining proof gaps without turning team-authored
evidence into an external audit or certification.

## Status

- Stage A threat model and evidence package: **prepared, and carried by the
  v0.14.0 release, published on 2026-07-24. The receipts and counts in this
  folder describe the Stage A carrier as it stood on 2026-07-22; the
  repository tests are the current check**.
- Phase 5.1.c owner disposition: **accepted** at the claim bar "automated
  security scanning and internal adversarial review, findings triaged and
  fixed."
- Stage B history: **retained as provenance, not evidence of an external
  assessment**. No external auditor is required under the owner's disposition,
  and no external audit occurred.
- Security certification or assurance claim: **none**.
- Version change or release approval: **not granted by these documents**.

> **Correction (2026-10-04):** the lines in this folder that say a receipt is
> superseded, the repaired carrier needs fresh evidence, or the 5.4 real-host
> receipt and committed-SHA CI are still open describe 2026-07-22. v0.14.0 was
> published on 2026-07-24 after the owner recorded the real-host receipt (29 of
> 29 checks), so neither gate is open for that release. The public security claim is: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code.

The Stage A baseline is `main` at
`c9d24243920a694eaf00ad595da392a1478710dd`. The browser-clipper remediation
adds one HTTP operation to that baseline. The superseded carrier reproduced 183
default HTTP operations, including 71 `/v0/vnext` operations, 232 HTTP
operations with legacy surfaces enabled, and 11 core MCP tools. Route, OpenAPI,
legacy-gating, and flag-off MCP closure tests reproduced those values on that
carrier; the repaired carrier must reproduce them again. The base counts were
182, 70, 231, and 11.

## Evidence Language

- **Implemented control** means the named code path exists.
- **Test evidence** means a named repository test exercises the claim; it still
  has to pass on the exact review commit.
- **Carrier acceptance** means a final-carrier CI or local result is required.
- **Proof gap** means the team does not claim closure.
- **Owner action** is outside the builder team's authority.

## Package Index

- [Threat model](threat-model.md) — assets, actors, trust boundaries, data flows,
  abuse cases, controls, and residual risks.
- [Stage A evidence ledger](stage-a-evidence.md) — scope, source provenance,
  acceptance rules, and known gaps.
- [Authentication and authorization](auth-authorization.md) — HTTP, MCP, agent
  key, project-scope, RLS, SQLite, and legacy-gating boundaries.
- [Input validation](input-validation.md) — request models, SQL/JSON/FTS
  construction, import paths, and the two directory-import residuals.
- [Secrets and redaction](secrets-redaction.md) — key storage, provider secrets,
  public errors, logging evidence, and RAM-hygiene limits.
- [Dependency posture](dependency-posture.md) — pinning, advisory tooling,
  automated scans, and the Python audit gap.
- [Review disposition and retained brief](external-review-brief.md) — the
  original owner-owned Stage B scope, the accepted claim boundary, optional
  future review guidance, and current status.

## Historical Scan: Input, Not Sign-off

A repository-wide deep scan targeted
`2c372417e1d07d072265d2efdfbdace04f8bfcbb`, not the Phase 5 base or final
carrier. Its report contains 41 canonical findings represented by 156 report
instances: 24 Medium and 132 Low, no High or Critical, all with Medium
confidence. Coverage was partial and the report explicitly retained runtime and
deployment proof gaps.

The scan artifacts remain with the release engineer. They are historical review
input only: the counts must not be presented as a current-head finding count,
clean bill of health, external assessment, or proof that untested paths are
safe. Three confirmed issues from that cycle were merged in PRs #310, #311, and
#312; the browser-clipper credential-context remediation belongs to the Phase 5
carrier and must pass its final acceptance tests. The owner's Stage B
disposition accepts automated security scanning plus internal adversarial
review; it does not turn the historical scan into an exact-candidate external
assessment or an independently audited claim.
