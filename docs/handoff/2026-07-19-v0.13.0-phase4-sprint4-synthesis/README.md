# Alice Phase 4 Sprint 4 Multi-Session Synthesis Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

## Verdict

The bounded implementation carrier is ready for independent code review, but
**Sprint 4.2 benchmark closure is NO-GO**. The final safe design records a
bounded count-candidate statistic in retrieval trace only. It exposes no count
to readers, MCP, or OpenAPI, and 0/14 audited occurrence-count rows are
answer-sufficient in both deterministic development arms.

The uncommitted, unstaged carrier is based on
`dc60924f6c5486b8ba2b82dcecf22378bf043319` (tree
`2f1a6ababbd70ebc0106f750dc1b34d4b85fd5ca`) on branch
`codex/v0130-phase4-sprint4-synthesis`. Governed version sources remain
`0.12.0`; the release engineer owns any later v0.13.0 integration and
version cut.

## Delivered boundary

- Count detection distinguishes discrete cardinality, occurrence frequency,
  cadence, and numeric-value questions. `how often` is cadence, not a total.
- Cardinality/frequency queries receive a provenance-deduplicated FTS
  candidate-group statistic under `trace.stages.coverage_mode`. It is
  explicitly marked `is_answer=false` and `supports_numeric_sum=false`.
- The statistic consumes at most its disclosed positive `candidate_cap`;
  legacy adapters returning more rows cannot make `rows_examined` or
  `count` exceed that bound.
- A memory row or roll-up member is not proven to equal one queried unit. The
  attempted reader aggregation and aggressive count-card promotion were
  therefore rejected and removed. Existing generic card-promotion behavior is
  preserved; no new count-specific elevation remains.
- No top-level `aggregation` field exists. MCP and OpenAPI remain unchanged.
  Candidate counts are trace-only even when an accepted count-bearing card is
  naturally selected.
- Cadence recognition preserves detector-off store calls, pool depth,
  diversity, and ranking.
- The keyless count probe anchors relative time to the question date, gives
  roll-ups-off/on mode-specific output names, rejects empty/partial audit
  manifests, and exits nonzero while answer sufficiency is 0/14. The evidence
  commands pass separate work directories for the two modes.

## Package contents

- `FIX_MATRIX.md` records what shipped and what measurement rejected.
- `BUILD_REPORT.md` records final hashes, test evidence, the external scale
  lane readback, and the explicit carrier receipt.
- `ENGINEER_HANDOFF.md` gives review order, invariants, exact-path staging,
  and remaining release-engineer work.
- `REVIEW_REPORT.md` is reserved for the independent reviewer.

No model or paid call, full benchmark, held-out run, commit, stage, push, tag,
package build, publication, or repository-setting mutation was performed.
Immutable release records, governed versions, `docs/benchmarks/`, and the
release-engineer-owned SQLite scale file were not edited by this carrier.
