# Roadmap

## Baseline (Not Roadmap Work)

- `v0.16.0` is the latest published release. Its immutable release record is
  `docs/release/v0.16.0-release-notes.md`, with artifact digests in
  `docs/release/v0.16.0-checksums.txt`. (The `v0.13.0` tag was never
  published; superseded.)
- `v0.11.0` shipped the Phase 1 periphery cut; `v0.11.1` shipped the Phase 2
  debt sweep. Their tags, release records, and published artifacts are not
  changed by the Phase 3 carrier.
- `v0.12.0` shipped the Phase 3 structural refactor with **Structure only.
  Zero behavior change.** `v0.13.1` shipped the Phase 4 core roadmap:
  three-run benchmark replication on a pinned manifest, SQLite vector scale
  with the resident cache, CI-smoked reference integrations, and the
  multi-session synthesis measurement (its records live in
  `docs/release/v0.13.1-release-notes.md`).
- LongMemEval_s has a `v0.12.0` `store_chunks` receipt of 81.2% mean over
  three independent full runs (80.8 / 81.0 / 81.8), measured 2026-07-18/19,
  with per-question evidence for every run committed. That harness is not
  the product path. `pack_excerpts` is the product-path mode and is not
  yet a published score. The earlier **79.4% (397/500)** single run from
  2026-07-07 is retained as evidence.
- Detailed v0.10.4 repair-batch chronology is historical evidence under
  `docs/handoff/history/`; it is not current roadmap work.

## Next

`v0.14.0` (Phase 5 enterprise track) is released; the next phase begins here.
Phases 1 through 5 are shipped and recorded in their release notes. Of the
former roadmap list, benchmark replication, multi-session synthesis
measurement, reference integrations, SQLite vector scale, and the enterprise
evidence base (real-host single-tenant deployment contract executed end to
end, least-privilege operations, encrypted off-host backups with a tested
restore, recorded security disposition) shipped in Phases 4 and 5.

1. **Dogfood the agent interface daily.** Still open, no build phase can
   deliver it, and it now gates the counting decision below. Exercise the
   default three-tool MCP core and equivalent HTTP/CLI flows against a live
   deployment; measure correction quality, review burden, project-scope
   usability, and end-to-end latency.
2. **Counting substrate (Phase 6): attempted, and parked.** Multi-session
   accuracy is the largest measured weakness on LongMemEval, and Phase 4
   forensics attribute the dominant failures to undercounting. Phase 4 also
   proved that no query-time mechanism over the then-current store could
   earn a count. Phase 6 built the substrate change: one reviewed occurrence
   unit per countable event, established at write time, with
   provenance-aware deduplication and honest ranges. The work is preserved
   unmerged on `codex/phase6-counting-substrate` and is **parked, not
   abandoned**.

   What it achieved: a correct, conservative substrate with write-time
   occurrence identity, provable sums with provenance, review gating, and no
   reachable exact answer. Occurrence extraction from ordinary English went
   from near zero to a small nonzero count, the query side parses ordinary
   count questions or refuses cleanly, and relative date windows resolve.
   Adversarial review found and removed a mechanism that would have reported
   confidently wrong counts.

   Why it is parked: the deterministic extractor recovers too few events from
   real conversational prose to reach the acceptance target, and the query
   and write stages are each short of it independently. The remaining gap is
   semantic event extraction from arbitrary English, which pattern rules are
   poorly suited to. The unused lever is model assistance at capture time
   under the existing review-gated proposal pattern, which the design has
   always permitted and which no increment has attempted; the house rule
   barring model calls in pack compilation is unaffected. Any next attempt
   should take that route and should be measured against real usage as well
   as the benchmark, which is why dogfooding now precedes it.
3. **Scale end-to-end recall past the FTS wall.** The vector stage stays
   interactive at 100k memories (resident cache; published in
   `docs/benchmarks/scale/`). The remaining wall at very large corpora is
   FTS/source-chunk search; SQLite end-to-end recall at 100k is ~1.8s
   against ~0.4s on Postgres.
4. **Road to `1.0`.** Remaining before a `1.0` claim: sustained dogfooding
   evidence, the filed deployment-guide hygiene follow-ups, and one final
   product-scoped review of the post-cut surface.

## Explicit Non-Goals For Now

- Hosted-service, multi-tenant control-plane, or SLA commitments.
- Managed OAuth consent/account-linking or automatic account polling.
- Telegram or other channel transports.
- A public bundled chat/response product, chief-of-staff product, or model-pack
  catalog. Internal response jobs remain limited to retained provider
  invocation.
- A consumer knowledge-management product.
- OCR or transcription execution; Alice accepts text extracted elsewhere.
- Re-expanding the default MCP surface beyond the eleven core tools.

`v0.16.0` is the latest published release and remains the install, checksum,
and baseline reference.

`v0.17.0` is the current release candidate. It is not published.
