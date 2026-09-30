# Active Sprint Packet

<!-- alice-sprint-scope: phase-3-complete -->

## Objective

Alice v0.12.0 Phase 3 structural refactor.

Relocate the oversized HTTP, store, contract, MCP, and CLI carriers along
existing domain seams while preserving behavior, signatures, generated SQL,
route and tool registries, entrypoints, and compatibility imports.

**Structure only. Zero behavior change.**

## Carrier

- Branch: `codex/v0120-phase3-structural-refactor`
- Base commit: `f342d45dabe127acca6231f29830ff11d98a340e`
- Base tree: `1d84e26597aecfcd9ad894c6d0b86fbe9a66dfd6`
- Target release: `v0.12.0`
- Latest published release: `v0.11.1`
- Governed package and web version sources: intentionally held at `0.11.1`
  until the release engineer cuts `v0.12.0`.
- State: implementation and the bounded builder matrix are complete in the
  uncommitted carrier. Independent increment reviews returned GO with no
  remaining P0-P3 findings. The independent final verdict is owned only by the
  handoff's `REVIEW_REPORT.md`; exact-SHA external release verification remains
  release-engineer work.
- Correction (2026-09-27): the reviews called independent above were internal adversarial review, not an independent or external audit.

## Completed structure moves

- `main.py` is a thin app assembly and middleware carrier; handlers live in
  domain routers with conditional legacy mounting unchanged.
- PostgreSQL and SQLite vNext store seams correspond file-for-file; the legacy
  store is split by surviving domain behind its stable facade.
- Pure contracts are split by domain behind the stable `contracts.py` facade.
- MCP registry/dispatch and tool implementations live in `mcp/` behind the
  stable `mcp_tools.py` facade.
- CLI parser/dispatch and commands live in `cli/`; the existing console
  entrypoints and compatibility namespace remain stable.
- Response-hygiene and coverage gates follow the moved carriers. The default-
  surface PostgreSQL smoke now fails if every selected test is skipped.

## Boundaries

- No behavior, logic, signature, SQL-text, migration, schema, dependency, or
  performance change belongs to this carrier.
- No version bump, stage, commit, push, tag, GitHub Release, package upload, or
  repository-setting mutation has been performed.
- Published v0.10.x/v0.11.x records and earlier handoffs remain immutable.
- Phase 4 is out of scope for this packet.
- No cybersecurity audit was performed.
- Text extraction happens outside Alice.
  Alice does not perform OCR or transcription.

## Exit condition

Hand the uncommitted, receipt-bound Phase 3 carrier to the release engineer.
They must verify the handoff, commit and merge through the protected flow, bump
both governed version sources to `0.12.0` in the release cut, run all required
checks on the exact release SHA, finalize the pending release notes and
checksums, and only then tag or publish.

Stop after the Phase 3 handoff; do not begin Phase 4.
