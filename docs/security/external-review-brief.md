# Phase 5.1.c Review Disposition and Retained Brief

## Ownership And Status

Phase 5.1.c is owner-owned. Its original Stage B plan contemplated an external
reviewer, and the scope below is retained as historical provenance. The owner's
authoritative disposition now accepts Phase 5.1.c at the claim bar "automated
security scanning under OpenAI Trusted Access on the repository, plus internal
adversarial review."

Current status: **owner disposition accepted at that claim bar**. No external
auditor is required, no external audit occurred, and no exact-review-commit
receipt from an external reviewer exists. Stage A and Stage B evidence must not
be described as an independent audit, security certification, clean bill of
health, or proof that every documented gap is closed. The 5.4 real-host receipt
and green committed-SHA CI remain separate release gates.

## Retained Stage B Review Target

If the owner voluntarily commissions a future external review, the reviewer
should receive:

- the exact immutable release-candidate commit and package/container hashes;
- the supported local-first and keyed single-tenant deployment profiles;
- [the threat model](threat-model.md) and [Stage A ledger](stage-a-evidence.md);
- the historical scan artifacts for commit
  `2c372417e1d07d072265d2efdfbdace04f8bfcbb`, explicitly labeled partial and
  historical;
- instructions to run PostgreSQL and SQLite postures, default and gated HTTP
  surfaces, the 11-tool core MCP server, browser clipper, scheduler, importers,
  providers, exports, and redaction; and
- the private reporting path described in the repository root `SECURITY.md`.

This retained target is not a current release requirement. If used later, do
not ask the reviewer to infer the target from `main`, a branch name, or an
unpublished version string. Their report must name the commit and deployment
configuration they actually assessed.

## Retained Adversarial Questions

1. Does the keyless-local assumption remain contained to loopback/owner trust,
   and does keyed mode close every vNext HTTP and core MCP path?
2. Can a key or payload escalate actor, permission profile, project scope,
   sensitivity, review authority, or PostgreSQL user context?
3. Can a visited page reuse, transfer, race, or leak a browser-clipper
   capability, or cause a capture after capability rejection?
4. Can hostile SQL/JSON/FTS input change query structure or bypass user/project/
   lifecycle filters?
5. Can hostile imported files escape roots, substitute content after evidence
   capture, exhaust a worker, or create provenance that does not match parsed
   bytes?
6. Can agent keys, provider/connector credentials, database URLs, or sensitive
   user content reach public errors, logs, events, traces, process arguments,
   archives, or package artifacts?
7. Are provider URL/redirect/DNS and response-size controls adequate for the
   documented deployment, and what residual availability risks are acceptable?
   From v0.20.0, provider calls go through one function
   that follows no redirect, and the provider helpers, Gmail and Calendar dial
   only an address the outbound policy allows. The embeddings, reranker, fact-key
   and brain clients do not check the address, a request carried by a proxy is not
   checked, and a provider response is still read whole.
8. Do PostgreSQL RLS, SQLite filesystem isolation, true redaction, backup/export,
   and legacy gating match the documentation under adversarial use?

Any future reviewer should explicitly revisit the deferred directory-import
symlink/TOCTOU issue, Python dependency-audit gap, settings cache footgun,
redaction RAM lifetime, and the historical scan's unresolved availability and
provider-network hypotheses.

## Optional Future External-Review Deliverables

These deliverables apply only if the owner later chooses to commission an
external review; they are not required to close the current Phase 5.1.c owner
disposition.

- Executive verdict scoped to the exact commit and deployment assumptions.
- Findings with severity, confidence, source-to-sink proof, reproduction, impact,
  and a falsifiable remediation test.
- Reviewed-surface and unreviewed-surface inventories.
- Clear disposition of Stage A known gaps and historical scan hypotheses.
- Separate code-security and deployment/operational recommendations.
- A retest receipt for each fixed High/Critical and any other release blocker.

Any future High or Critical finding would open a fix-and-retest window.
Medium/Low items would require an explicit owner disposition; silence would not
be acceptance.

## Optional Future Stand-up Path

A future reviewer should be able to reach a working local environment in under
one hour by following:

1. [`docs/alpha/headless-ubuntu-install.md`](../alpha/headless-ubuntu-install.md)
   for role-separated PostgreSQL, or the
   [alpha quickstart](../alpha/quickstart.md) for the SQLite on-ramp;
2. `make setup`, migrations where applicable, and `alicebot vnext doctor
   --fix-safe --ci`;
3. the focused commands in [the Stage A ledger](stage-a-evidence.md); and
4. the repository's full release matrix for the exact carrier.

If those instructions fail on a clean supported host, record the failure as an
environment/reproducibility gap before beginning substantive review.

## Receipt Boundary

The current owner disposition does not require an external-review receipt. If a
future external review is commissioned, its non-secret receipt should contain
the reviewer or organization identity, exact commit, dates, surfaces, methods,
report location, severity rollup, open blockers, remediation commits, and
retest status. Do not commit exploit details or live secrets to the public
repository; use a private GitHub security advisory for active vulnerabilities.
