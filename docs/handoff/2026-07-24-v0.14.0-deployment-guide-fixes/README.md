# v0.14.0 Deployment Guide Fixes Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

## Verdict boundary

- **Code carrier:** independent review **GO**.
- **Release:** **NO-GO** until the committed-SHA workflow matrix is green.
- **Owner real-host evidence:** accepted on 2026-07-24 with 29 of 29 checks
  passing. The static configuration smoke intentionally continues to report
  `owner_real_host_deployment_receipt` as a proof gap because it cannot inspect
  an external host.
- **Versions:** both governed sources remain `0.13.1`. This carrier does not
  perform the `0.14.0` cut, tag, publication, or external readback.

This package repairs the five defects found when the owner ran the
single-tenant deployment guide on Ubuntu 24.04 with PostgreSQL 16 and pgvector.
It also transitions the integrated Phase 5 truth guard and repairs the
candidate-versus-published control-document assertion that blocked a truthful
release cut.

## Delivered decisions

1. Identity provisioning uses the brief's option (a). One RLS-aware
   `scripts/seed_local_user.py` helper is shared by the installer and the manual
   guide. It sets `app.current_user_id` transaction-locally on the same
   connection as the `users` upsert. The bootstrap API keeps its existing
   unknown-user 404 boundary.
2. Next.js `.env*.local` files are ignored while
   `apps/web/.env.local.example` remains tracked. The validator treats the
   documented rendered environment file as expected deployment state rather
   than carrier drift.
3. Physical backups use a dedicated non-superuser `alicebot_backup` role with
   `BYPASSRLS` and read-only grants. A separate `alicebot_drill` role holds
   `CREATEDB` only for disposable restore databases. The docs state the
   cluster-wide resource tradeoff of that privilege.
4. New archives omit comments and restore also ignores comments, so extension
   ownership does not block a least-privilege drill. Bounded, scrubbed
   subprocess diagnostics go to stderr and never enter the sanitized receipt.
5. Persistent deployment configuration and CA material now live under
   `/etc/alicebot`; the docs, examples, validators, tests, and smoke contract
   move together.
6. The roadmap truth assertion now follows the structured published-release
   record instead of the pending governed version.
7. Full-history CI provisions root, admin, application, backup, and lifecycle
   roles with exact authority checks, then runs the empty-user bootstrap and
   both-backend ops drill under the documented least-privilege split.

## Evidence boundary

The local and isolated PostgreSQL 16 matrix and independent review are green,
including forced-RLS
identity seeding, migrations through `20260721_0094`, physical destroy and
restore, recall, embedding-signature continuity, and cleanup of disposable
databases. The committed carrier still needs the pull-request and main
workflows because local success does not certify a future commit SHA.

The supported security claim remains: automated security scanning and internal
adversarial review, findings triaged and fixed. This package does not claim an
independent audit, third-party audit, penetration test, or security
certification.

## Package contents

- `FIX_MATRIX.md` maps every brief item to the implementation and proof.
- `ENGINEER_HANDOFF.md` gives the exact verification, commit, PR, rollback, and
  operator sequence.
- `BUILD_REPORT.md` records the builder matrix and explicit carrier receipt.
- `REVIEW_REPORT.md` records the independent control-tower review GO and is
  excluded from the carrier receipt loop.

The receipt binds the explicit sorted path list to base commit
`b383f6e69896717dfb60b887747e304c33f70d5b` and base tree
`faec22103b6bdee8650513f0c4c6aa28b7e5b912`. Any pre-commit edit to a
receipt-listed path requires a new receipt and review. After integration, the
guard freezes this handoff package while allowing later reviewed source
changes.
