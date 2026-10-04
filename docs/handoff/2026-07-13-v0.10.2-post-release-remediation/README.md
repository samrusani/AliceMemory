# Alice v0.10.2 Post-Release Audit Remediation Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

This directory is the main-engineer handoff for the non-security audit and
remediation performed after the published `v0.10.2` release. It includes the
builder corrections requested by the independent review passes, the final
reviewer's approval, and the complete local verification rerun on the
resulting code-frozen tree.

- `FIX_MATRIX.md` maps the independent review findings to the implemented
  correction and verification evidence.
- `BUILD_REPORT.md` records the builder lanes and local release-matrix results.
- `ENGINEER_HANDOFF.md` contains the review, commit, migration, and release
  procedure.
- `REVIEW_REPORT.md` is owned by the independent reviewer and records the
  authoritative **APPROVE** verdict for the frozen local patch.

The control-tower builders did not commit, push, merge, tag, dispatch a
credentialed workflow, modify live repository controls, or publish a package.
The current remediation is a dirty working tree based on branch
`codex/v0102-audit-remediation` at `767ede0eafe01e0971cf1ae1c432f961fd5b4578`.
It must not be described as part of immutable `v0.10.2`.

Publication remains blocked until the engineer creates a clean candidate
commit, all required checks run on that exact SHA, live repository controls
are read back and attested, and the protected configured PostgreSQL/pgvector
semantic gate passes for the same SHA.
