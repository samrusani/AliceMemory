# Current State

## Snapshot

- `v0.17.0` is the latest published release. It is available from PyPI and
  GitHub, its record is immutable, and exact artifact digests are in
  `docs/release/v0.17.0-checksums.txt`. `v0.16.0` is the immediately prior
  published release.
- Earlier releases whose headlines still get referenced: `v0.13.1` shipped the
  Phase 4 core-roadmap work as **Replicated benchmark, faster SQLite at scale,
  reference integrations**, and `v0.12.0` shipped the Phase 3 structural
  refactor as **Structure only. Zero behavior change.** Records for both remain
  under `docs/release/`.
- Two tags exist that were never published: `v0.13.0`, superseded by `v0.13.1`,
  and `v0.15.0`. Neither has a GitHub Release or a PyPI artifact.
- Phase 3 implementation and the bounded builder matrix completed on
  `codex/v0120-phase3-structural-refactor`, based on
  `f342d45dabe127acca6231f29830ff11d98a340e`. Each code increment received an
  independent GO with no remaining P0-P3 finding. The independent final verdict
  is owned only by the handoff's `REVIEW_REPORT.md`; the exact-SHA external
  release gates passed on the release commit.
- Both governed version sources were cut to `0.12.0` by the release engineer
  after verifying the handoff.
- The historical LongMemEval_s result is **79.4% (397/500)** from one run on
  2026-07-07. It is not a repeated estimate or a measurement of this release.
- Alice remains public-alpha, pre-1.0, local-first, single-user, and self-hosted.

## What `v0.11.1` Shipped

`v0.11.1` shipped the bounded Phase 2 debt sweep on the post-periphery-cut
product surface. Its immutable release notes and checksums remain the
authoritative description; Phase 3 does not rewrite that history.

- Stable HTTP, MCP, CLI, and onramp failures, plus the migrated provider,
  response, scheduler, evaluation, doctor, and connector diagnostics, keep
  static public vocabularies. Intentional legacy-on `proxy_execution.py`
  business-result reasons remain dynamic.
- The `list_memories(query=...)` and
  `list_resume_memory_events(query=...)` legs keep their ASCII case-insensitive
  literal-filter contract; generic `alice_recall` retrieval remains separate.
- Coupled true redaction scrubs the governed memory/project-update graph but
  retains shared source/source-chunk evidence for separate source hygiene and
  does not roll back accepted project state.

## What `v0.12.0` Shipped

- A thin `alicebot_api.main:app` assembly module with the HTTP handlers moved
  into domain routers. Default and gated OpenAPI registries remain exactly 182
  and 231 operations, a delta of 49.
- Corresponding PostgreSQL and SQLite vNext store seams, a surviving-domain
  legacy-store split, and stable store facades. Generated SQL text and store
  protocols are unchanged.
- Domain contract modules behind the stable `contracts.py` facade.
- Per-domain MCP implementations behind the stable `mcp_tools.py` facade,
  preserving the 11-core/65-legacy/76-total registry and flag behavior.
- Per-domain CLI command modules behind the stable `alicebot_api.cli` import,
  preserving both `alice` and `alicebot` entrypoints; `alice-memory` remains
  routed through the onramp entrypoint.
- Response-hygiene and coverage enforcement that follows moved modules rather
  than shrinking onto a facade. The 296 public-response call inventory and the
  router aggregate coverage floor remain pinned.
- No route, tool, command, schema, migration, dependency, or runtime behavior
  change.

## Verification Posture

- Final code-carrier evidence passed 3,804 unit tests with 80.3777897% package
  coverage. Router coverage was 3,604/5,373 statements, 67.0761%, above the
  45% floor.
- PostgreSQL 16 plus pgvector passed 399 legacy-on integration tests with one
  expected skip. The separate flag-off smoke passed one executed test with the
  nonzero-test guard enabled.
- LongMemEval passed 127 tests; the checked evidence replay passed seven arms;
  the focused vector/retrieval lane passed two tests.
- The web carrier passed 217 unit tests, core and vNext coverage floors,
  typecheck, lint, build, bundle budgets, and the 17+1+1+1 browser matrix.
- Final-carrier package reproduction and installed-artifact evidence is owned
  by the Phase 3 `BUILD_REPORT.md`. The builder matrix ran before the version
  cut; its locally produced artifacts were verification inputs only and were
  never uploaded anywhere.
- No security or cybersecurity audit was performed in Phase 3.

## Release Boundary

`v0.17.0` is tagged, published, and immutable. Its authoritative records are:

- `docs/release/v0.17.0-release-notes.md`
- `docs/release/v0.17.0-checksums.txt`

`v0.16.0` is the immediately prior published release; its records are
`docs/release/v0.16.0-release-notes.md` and `docs/release/v0.16.0-checksums.txt`.

Every earlier release remains published and immutable, with its own
`docs/release/vX.Y.Z-release-notes.md` and `vX.Y.Z-checksums.txt`. That includes
`v0.13.1`, `v0.12.0` and `v0.11.1`, which are referenced elsewhere in this
document.

Two tags exist that were never published and never will be, because stable tags
are immutable and the numbers are retired rather than reused: `v0.13.0`,
superseded by `v0.13.1`, and `v0.15.0`, whose commit carried a release-gate step
that could not run on a CI runner.

## What `v0.14.0` Shipped

The Phase 5 enterprise track: the single-tenant self-hosted deployment
contract executed on a real public host with an owner deployment receipt,
least-privilege operations proven under a non-superuser admin role, executed
backup and restore evidence on both backends, the one-time origin-bound
browser-clip capability, and the five deployment-guide fixes surfaced by the
first real-host execution. Scope details are in the dated handoff packages and
the `v0.14.0` release notes.

## What `v0.13.1` Shipped

- The replicated LongMemEval_s baseline (81.2% mean over three runs on the
  published `v0.12.0` code) committed as per-question evidence.
- SQLite vector-scale work: bit-identical vectorized scan, resident vector
  cache with transactional stamp invalidation (vector stage 385-465ms warm
  at 100k inside 754MB peak / 760MB steady, 1024MB default cap,
  off-switch), one additive bootstrap table, Postgres unchanged.
- Two CI-smoked reference integrations: the MCP quickstart and the OpenAI
  Agents SDK function-tool example with real per-agent key auth.
- Trace-only count-intent diagnostics behind the aggregation gate
  (is_answer=false); the multi-session benchmark-closure NO-GO is recorded
  and no synthesis uplift is claimed.
- The deferred mcp-tools.md legacy-alias wording correction.
- No MCP registry, OpenAPI, HTTP route, dependency, or Postgres schema
  change.

## Product Boundaries

- No hosted service, multi-tenant control plane, or SLA.
- No managed OAuth consent/account-linking or automatic account polling.
- No Telegram or other channel transport in the current runtime.
- No public bundled chat/response product, chief-of-staff product, or model
  packs. Internal response jobs support retained provider invocation only.
- No silent capture from arbitrary conversations.
- No OCR or transcription execution; Alice only ingests extracted text.
- Durable agent writes remain policy-checked, provenance-linked, and reviewable.
- Coupled true redaction scrubs governed content copies in the memory/project-
  update graph; it does not undo accepted project state or erase shared source
  evidence, upstream systems, exports, backups, or external logs.

## What `v0.15.6` Shipped

It fixes one defect: `alice_capture` flattened `raw_text` before chunking, so a
document with 17 newlines was stored with 0 and chunking, which splits on blank
lines, saw a single paragraph. A whole-vault note import therefore produced
memories spanning unrelated notes.

**`v0.15.5` shipped a chunker fix for this same symptom and it was inert**, since
the flattening happened upstream of the chunker and left it no boundaries to act
on. Confirmed against the published artifacts: capturing the same vault gives
`chunk_count` of 1 on both v0.15.4 and v0.15.5, and 3 on v0.15.6.

Introduced in `v0.12.0`, so it survived every release between.

Re-import notes on `v0.15.6`. Candidates extracted earlier came from flattened
text and should be deleted rather than approved.

## What `v0.15.7` Shipped

Imported documents are readable as sources: `alice_context_pack` and
`alice_recall` return excerpts labelled
`excerpt_kind: imported_source_material`. Candidates stay unsearchable
as memories. `count=0` after capture is still the design.

An oversized paragraph splits on its own lines before falling back to
words, so a long list is not stored as flattened word-count slices.
Already-captured content is not re-chunked.

`alice_resume` and `alice_recent_decisions` apply the policy fence they
already computed. Four other read paths of the same class are left
alone and are not default tools.

If you imported on `v0.15.6`, upgrade is enough for readability. If you
imported on `v0.15.5` or earlier, delete those candidates and import
again.

## What `v0.17.0` Shipped

`v0.17.0` is the latest published release and remains the install, checksum,
and baseline reference.

It shipped the work on `main` after `v0.16.0`. Install writes the Claude Code
session hook in the shape Claude Code reads, edits only Alice's entry in the
Hermes config and keeps its documented env keys, and works without uv. One
credential check covers the memory write paths, and the importers and the
sleep pass skip credential material. Agent commits above the sensitivity
ceiling are refused. Recalled notes are JSON-quoted under one framing
sentence, with a writer label on each. `alice-memory sleep-proposals` lists
sleep proposals. There is no schema change.

- [v0.17.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.17.0-release-notes.md)

## What `v0.16.0` Shipped

`v0.16.0` is the immediately prior published release.

The default loop is on the wheel: `alice-memory install`, `demo --vault`,
`doctor`, `brief`, write receipts, and a three-tool MCP handshake
(`alice_memory_commit`, `alice_recall`, `alice_resume`). The other
eight core tools need `ALICE_MCP_FULL_TOOLS=1`.

Present-tense recall prefers the current fact. Recall hops once
through provenance. The pack picks a loops / facts / sources view
from the query. `alice-memory sleep` writes up to eight proposals and
does not create a memory.

Import stays a source. Commit stays a fact. Candidates stay
unsearchable as memories. `count=0` after capture is still the
design. 81.2% stays a `v0.12.0` `store_chunks` receipt.
`pack_excerpts` is named and not scored.

## The `v0.15.0` tag was never published

The commit it points at carried a release-gate step that could not run on a CI
runner, so the gate could never pass from that tag, and repository rules
correctly refuse both deletion and update of stable tags. No GitHub Release or
PyPI artifact was ever created for it.
