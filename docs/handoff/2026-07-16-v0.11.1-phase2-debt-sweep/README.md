# Alice v0.11.1 Phase 2 Debt-Sweep Handoff

> **Correction (2026-10-04):** the reviews named here were internal adversarial review and automated security scanning. No one outside the project has audited the code.

This directory is the control-tower handoff for the bounded Phase 2 debt
sweep. The candidate is based on the published v0.11.0 `main` commit
`5f0a92d77d02b0699af3054fced7427929808aa8` (tree
`560bade5b9ad20c659f03f19693288558c706945`) and targets `0.11.1` on branch
`codex/v0111-phase2-debt-sweep`.

The tree is intentionally uncommitted. No file is staged, no commit or tag was
created, and nothing was pushed or published. Existing migrations through
`0090`, immutable v0.10.x/v0.11.0 release records, and the Phase 1 handoff were
not edited. Phase 3 work was not started.

## Package contents

- `SURFACE_INVENTORY.md` — re-measured baseline and the HTTP, MCP, CLI,
  scheduler, web, PostgreSQL, SQLite, workflow, test, and documentation
  disposition for every Phase 2 item.
- `FIX_MATRIX.md` — item-by-item closure of 2.0 through 2.14 and the associated
  fail-on-old proof.
- `BUILD_REPORT.md` — exact-tree verification, coverage, web and advisory
  results, package evidence, and the twice-reproduced carrier receipt.
- `ENGINEER_HANDOFF.md` — review order, high-risk invariants, reproduction
  commands, and release-engineer-only gates.
- `REVIEW_REPORT.md` — reserved for the independent reviewer. The builder does
  not create or pre-authorize it.

## Delivered boundary

- The required CI inventory now has a separate flag-off, role-separated
  PostgreSQL job that proves the 182-operation HTTP and eleven-tool MCP default
  surface through bootstrap, capture, review, recall, resume, and context-pack.
- The post-cut error surface uses stable codes and static messages for public
  HTTP, MCP, CLI, and onramp failures and for the migrated provider, response,
  scheduler, evaluation, doctor, and connector diagnostics. Exception details
  remain in private logs. Intentional legacy-on `proxy_execution.py` business-
  result reasons remain dynamic and are explicitly excluded from this
  diagnostic migration. The local source baseline of 288 direct or delayed
  public exception callsites has zero old-pattern AST violations in the
  candidate; eight additional dynamic 404 branches bring the stable helper-
  call inventory to 296.
  Live CodeQL closure still belongs to the committed-SHA external gate.
- PostgreSQL, SQLite, and the MCP fake now give
  `list_memories(query=...)` and `list_resume_memory_events(query=...)` the
  open-loop ASCII-case-insensitive literal-filter contract used by recent
  decisions and resume. Generic `search_memories`/`alice_recall` retrieval is
  separate and unchanged. Terminal project-update replay uses one target/
  payload-filtered store query rather than full event-log scans.
- Reject validates its mandatory candidate memory key before mutation. Generic
  HTTP, MCP, and CLI review/correct/undo/forget/redact paths cannot strand a
  pending project-update artifact.
- The owner-selected **Option A** true-redaction design is implemented. For a
  terminal project update, the coupled artifact, free-text quality feedback,
  provenance quotes, memory, revisions, and exact events converge on
  content-free skeletons in one transaction. Governed text uses `[REDACTED]`,
  governed non-null free-form JSON uses `{"redacted":true}`, and null content
  stays null. The memory's `commit_digest` and `confirmation_id` are cleared;
  `confirmation_status`, `last_confirmed_at`, non-content classification/
  lifecycle fields, identities, links, all six numeric quality dimensions,
  categorical verbosity, and already-applied project state remain. Source and
  source-chunk evidence is intentionally unchanged because either may support
  other memories.
- Forward migrations `0091` and `0092` add defensive source normalization,
  bounded event lookup, and fail-closed coupled-redaction guards without
  changing migration history.
- Store fakes reject unknown list filters and honor deleted-row parity; coverage
  declares its required feature floor; release-body decoding is type-checked;
  workflow/readme/scheduler shapes have fail-on-old tests.
- `response_generation.py` is reduced from 705 to 610 lines around the retained
  provider-runtime path. Phase 1 already closed the empty-directory and upgrade
  documentation riders. No Alice Redis client or transport survives; only
  configuration, health-reporting, dependency, Docker, and install placeholders
  remain, so the old entrypoint-rate shared-Redis item is obsolete.
- The repository keeps its fail-closed npm bulk-advisory wrapper on pinned Node
  20/pnpm 10.23.0. The carrier takes the bounded pytest, checkout, and CodeQL
  updates and defers broader runtime/toolchain ranges to separate compatibility
  work.

## Builder status

The bounded builder matrix is green at package-input freeze:

- 3,547 unit tests passed; reported total coverage is 79.4009%, with
  `main.py` at 62.9591%. Static checks, control-document truth, Ruff, mypy,
  compileall, and diff hygiene passed.
- PostgreSQL 16.13 with pgvector 0.8.2 passed 399 legacy-on integration tests
  with one documented skip, plus the separate explicit flag-off default-
  surface smoke. LongMemEval passed 127 tests; all six provider-free SQLite
  eval suites passed all 78 cases in FTS-only mode.
- The web carrier passed 217 unit tests, 20 browser tests, typecheck, lint,
  build, budgets, and production/full advisory checks. The final lockfile hash
  after the direct `semver@7.8.0` dependency correction is
  `c4cd48f582508459ca4927539bd5ae7c6976aa99a12201f588bf6a81669d86a3`.
- The isolated health scan reported overall 34.3-34.4, objective 85.8-85.9,
  code-quality 83.1 (82.9 strict), file health 81.1 (80.8 strict), duplication
  99.6, and test health 73.7-74.6. Its range is reported because a basename-
  only test-to-source mapping is nondeterministic. Two unchanged private
  helpers, `main.py::_runtime_provider_config_or_none` (18 lines) and
  `mcp_tools.py::_build_recall_query` (11 lines), are confirmed repository-
  local dead code and remain nonblocking pre-existing follow-up debt outside
  this Phase 2 delta.

At package-input freeze, final package reproduction, the twice-reproduced
superseding carrier receipt, and independent review were still pending. Consult
the final `BUILD_REPORT.md` and reviewer-authored `REVIEW_REPORT.md` for their
outcomes. External exact-SHA CodeQL/real-provider semantic/required-check,
release identity, tag, GitHub Release, and PyPI gates remain release-engineer
work.

No cybersecurity audit was performed; the owner explicitly placed security
review outside this Phase 2 carrier. The 242-alert published-base CodeQL
baseline described in `SURFACE_INVENTORY.md` is a narrow response-hygiene
release measurement, not a security assessment.
