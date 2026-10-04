# Plan: Memory Frontier — Quality + Context Engineering

Status: planned. Grounded against the actual codebase (post `v0.7.0`) by an
eight-area code audit on 2026-07-04. Inspired by external 2026 research input
(MAG taxonomy survey, CoMem, AgeMem, Mem0/Zep architectures), filtered to what
Alice can honestly build next.

Status update, 2026-10-04: historical plan. `v0.8.0`, the memory-frontier release, shipped from it; see `docs/release/v0.8.0-release-notes.md` for what shipped.

## The audit's one-line conclusion

Across all eight proposed areas the pattern is identical: **schema
over-delivers, behavior under-delivers.** The enums, columns, and gates
mostly exist; almost nothing reads them. The risk to avoid is "enum theater"
— adding more vocabulary before making the existing vocabulary do work.

## Grounded status per area

| Area | Reality today | Size |
|---|---|---|
| Taxonomy / procedural | 25-type enum already supersets the proposal (incl. `procedure`, `episode`); zero type-conditioned behavior anywhere — no filter, no ranking, no sections, no auto-classification | M |
| Multi-scope | Two real scopes (user hard; domain/sensitivity soft). `project_scope` is policy theater; MCP `projects` param accepted and silently dropped | L |
| Staleness / decay | `valid_from/valid_to/last_confirmed_at` columns exist, never written or read on the vNext path; no `stale` status; no sweep; ranking has zero temporal input | M |
| Context compiler v2 | Real hybrid retrieval + provenance; but `max_tokens` accepted and ignored, three sections hardcoded empty, rows duplicated 3× in packs; superseded/rejected exclusion already works | M–L |
| Consolidation | Still a manual-only placeholder (one fixed-text candidate per run); no merging, no scheduling; embedding substrate from the retrieval rebuild is the reusable asset | L |
| Temporal graph | Legacy bi-temporal replay engine works but is triple-gated (Postgres-only, legacy flag, entities nothing creates); vNext edge event-time columns are dead | M slice / XL parity |
| Ops contract | 6 of 8 verbs real and converging on one service (commit/confirm/undo/correct/forget/audit); `merge`/`expire` missing; the whole agentic write protocol is legacy-flag-gated off the core MCP surface | M |
| Memory-quality evals | Harness is honest and reusable; correction-suppression already passes empirically; 3 of 6 proposed suites buildable now, 3 blocked on staleness/consolidation features | M |

## Sequencing

### Sprint A — Make existing vocabulary work (mostly S/M)
1. **Truth fixes**: enforce `max_tokens` with greedy budget packing; honor or
   reject the `projects` retrieval param; populate contradictions via the
   existing `VNextContradictionService` or delete the dead sections; fix docs
   overstating procedural/temporal claims.
2. **Type-conditioned retrieval**: `memory_type` filter through the six search
   methods and `alice_recall`/`alice_context_pack`; a procedures section in
   packs copying the shipped beliefs/decisions pattern; capture rules for
   `Procedure:`/`Playbook:` prefixes.
3. **Staleness v1**: demote/filter `valid_to < now` in search; add `stale`
   status; refresh `last_confirmed_at` on confirm; scheduler expiry sweep.
4. **Three memory-quality eval suites** on existing plumbing:
   correction-suppression, decision recovery, provenance explanation.

### Sprint B — Scopes + the write protocol (M/L)
5. **Real scopes**: `project_id`, `created_by_agent_id`, `run_id` columns with
   metadata backfill; filters threaded through search + compiler mirroring the
   domains pattern; bind `project_scope` to agent API keys (closes the
   self-declared-scope trust gap).
6. **Memory Operations Protocol**: promote the agentic write verbs
   (commit/confirm/undo/correct/forget) onto the core MCP surface; publish the
   protocol doc with `merge`/`expire` marked planned; decide honest `forget`
   semantics (soft-delete today; offer true redaction).

### Sprint C — Consolidation MVP + temporal slice (L)
7. **Consolidation that merges**: nightly-schedulable embedding-KNN clustering
   over active memories → LLM merge proposal → existing candidate/review gate;
   repeated-preference detection as counting heuristics first. Requires a
   configured embedding endpoint (dogfooding prerequisite).
8. **Temporal cheap slice**: populate edge event-time at creation
   (`source_created_at` fallback `captured_at`); as-of reads; `superseded_by`
   pointers on memories; a history answer on the core surface.

### Sprint D — Temporal Graph Memory + Entity Resolution (planned; the multi-session play)
Sprints A–C shipped the temporal substrate (event-time on edges, first-class
supersession pointers, as-of queries, supersession chains in `alice_explain`).
Sprint D makes the graph *load-bearing for retrieval* — the mechanism aimed
directly at the weakest LongMemEval category (multi-session synthesis, 45.1%):

1. **Entity extraction & canonicalization** — deterministic-first (capitalized
   spans, handles, alias tables); optional LLM-backed extraction through the
   same review gate. Nothing invents entities silently. Link the `person`
   memory type to the people/entities substrate.
2. **Graph traversal as a retrieval stage** — entity-hop expansion fused into
   the existing hybrid ranking (never replacing it): question mentions X →
   X's edges → connected memories join the candidate pool.
3. **Belief/thesis evolution timeline** — built on the supersession-chain
   primitive; "how did this thesis form, what supported it, what contradicted
   it, what changed."
4. **Relationship history** — stop overwriting `relationship_type` in place;
   record `relationship_changed_at` transitions.
5. **Graph quality evals** — a suite that can fail, plus the decisive test:
   re-run LongMemEval and watch the multi-session score move.

Non-goals: adopting Zep/Graphiti as a dependency (borrow the insight — Alice
stays the governed memory OS); graph-only retrieval (the hybrid stack always
fuses; graph must earn its rank contribution).

## Deliberately deferred
- RL-trained memory policies (AgeMem-style) — need dogfooding telemetry first.
- `app_scope`/`source_scope` — weakest motivation of the proposed scopes.
- Full Zep/Graphiti-parity bi-temporal graph — XL; the M slice above covers
  the high-value questions.
- AMP wire-format adoption — watch, don't chase.

## Relationship to the standing roadmap
Dogfooding with real embeddings (roadmap #1) is a hard prerequisite for
Sprint C consolidation and calibrates the paraphrase eval target. LongMemEval
(roadmap #3) lands best after Sprint A: temporal reasoning is its core
difficulty, and staleness v1 + compiler v2 directly move that score.
