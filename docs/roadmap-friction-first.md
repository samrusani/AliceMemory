# Friction-first roadmap

Written 2026-07-29. Supersedes the feature-ordered plan for the next phase.

## The thesis, and the evidence for it

`thedotmack/claude-mem` has **90,631 stars**. Alice has **2**. Verified against
the GitHub API on 2026-07-29, not from a screenshot.

claude-mem is a year old, carries 429 open issues, and is on any reasonable
engineering measure less rigorous than Alice: no published benchmark receipt,
no adversarial review record, no release attestation chain. Alice has hybrid
retrieval with reciprocal rank fusion, a LongMemEval receipt with disclosed
trade-offs, eight adversarial rounds on the promotion policy alone, and a
release gate that caught four real defects in a single day.

None of that produced adoption. The difference is that claude-mem starts in
about thirty seconds and Alice needs PostgreSQL.

**So the next phase is ordered by friction removed, not features added.** A
better memory layer nobody can start is worth less than a rougher one they can.

## Licensing constraint, read this before opening their source

claude-mem is **Apache-2.0**. Alice is **MIT**. Apache-2.0 code cannot be
copied into Alice and kept MIT: the patent grant and NOTICE obligations travel
with the code, and including it would make Alice mixed-license, which
undercuts the clean-MIT position.

Ideas are not copyrightable. Read it, understand the pattern, reimplement.
Do not paste. If a specific file is ever genuinely worth vendoring, that is a
deliberate licensing decision to take explicitly, not a shortcut.

---

## Track 1: make the first run that already exists actually work

**Corrected 2026-08-14.** The original version of this track was wrong on the
facts, and the correction matters more than the original claim.

It said the problem was the absence of a single-command installer, and proposed
building `uvx alice-memory init`. **That installer already exists and works.**
`uvx alice-memory mcp --data-dir ~/.alice` pulls 23 packages, starts in about
seven seconds, and serves 11 MCP tools with no Postgres and no checkout.

The real problem is one level deeper and considerably worse.

**Problem.** The documented zero-setup path installs correctly and then
**returns nothing you write to it, permanently.** Verified against published
v0.15.2: `alice_memory_commit` lands as `status: review_required`,
`alice_recall` returns `count: 0` for every query, `alice_context_pack` reports
"No matching memory was selected", `alice_memory_review` can list the queue but
has no action parameter so it cannot approve, and `ALICE_MEMORY_PERSONA` has no
effect on this path at all. There is no approval route on the MCP surface or in
the local CLI.

The cause is deliberate and the security reasoning behind it is correct: MCP
over stdio cannot authenticate a human, so `mcp/memories.py` honestly reports
`owner_verified=False`, and promotion requires owner or authenticated-agent
standing. The local path can mint no key, so every write is a candidate
forever.

**Target.** A memory written through the documented quickstart is retrievable.

Work:

1. Give the local path a real identity: mint a per-installation agent key on
   first run, store it in the data directory at `0600`, register it in the
   local store, and use it. The trust boundary on this path is the filesystem,
   not the transport, because anyone who can run the server can already open
   the SQLite file directly.
2. Add an approve and reject action to the local surface. Required, not
   optional: the hard floor will still escalate some writes even once promotion
   works, and today nothing can clear them.
3. Make `ALICE_MEMORY_PERSONA` and `ALICE_MEMORY_ESCALATION_FILTERS` take
   effect here with the same semantics as the Postgres path.
4. Fix the docs, which currently promise a working memory and do not mention
   that nothing is retrievable.
5. Measure honestly: time from a bare machine to first successful recall,
   written down, even if it is not thirty seconds.

**Acceptance.** From nothing: `uvx alice-memory mcp`, commit one ordinary
memory, recall it, `count >= 1` with matching text. That is the test that fails
today.

**Why this was not caught earlier:** every test exercises the Postgres path or
calls the service layer directly. Nothing drives the shipped MCP binary end to
end from a clean machine. The gap was in what we tested, not in what we knew.

Full spec for the external build team:
`~/Documents/alice-local-identity-build-spec.md`.

---

## Track 2: capture that does not need to be chosen

**Problem.** Alice records a memory when an agent decides to call a memory
tool. Tiered promotion removed the approval step, which was the right fix, but
the agent still has to initiate. If it does not think to remember, nothing is
remembered.

**Pattern to reimplement.** claude-mem hooks the session lifecycle:
SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd. Capture becomes
ambient rather than deliberate.

Work:

1. A Claude Code plugin exposing the same lifecycle points, writing through the
   existing commit path so the promotion policy, the hard floor and the
   credential guard all still apply. Ambient capture must not become a way
   around the write gate.
2. A capture policy for what is worth keeping, since hooking everything is how
   a memory store fills with noise. This is where Alice's existing capture
   rules and staleness handling should earn their keep.
3. Provenance on ambient writes, so a memory captured by a hook is
   distinguishable from one the user asked for. `write_provenance` already
   exists; extend rather than duplicate it.

**Acceptance.** A session produces useful memories without the user or the
agent explicitly asking, and every ambient write is refusable by the same
floor that governs deliberate ones.

---

## Track 3: progressive disclosure and token accounting

**Problem.** Context packs return content. There is no cheap layer to filter
against before paying for detail.

**Pattern to reimplement.** A three-layer read: a search index, then a
timeline or summary layer, then full detail. claude-mem claims roughly a ten
times token saving from filtering before fetching. Treat that number as their
claim rather than as measured fact until Alice has its own.

Work:

1. Add a cheap discovery layer over the existing retrieval, returning
   identifiers and one-line summaries rather than full memories.
2. Report token cost as a first-class field on retrieval responses, so a caller
   can see what a pack costs before and after.
3. Measure the saving on Alice's own corpus and publish the number with its
   method, the way the LongMemEval receipt is published. If it is 3x rather
   than 10x, publish 3x.

**Acceptance.** A measured, reproducible token saving on Alice's own eval
corpus, with the harness committed.

---

## Track 4: dogfooding, running in parallel throughout

Not sequenced after the others, because none of them depend on it and it is the
only source of first-run truth Alice does not currently have.

Load the wiki, use it for a week, keep the journal line:

    date | what I asked or captured | hit/miss/partial | latency feel | correction needed?

Misses and corrections are the valuable rows. Two weeks of honest lines decides
what track 1 and track 2 actually need, as opposed to what seems obvious now.

---

## Deliberately not in this phase

**Enterprise readiness.** God modules, contract drift, multi-user, SSO, audit
export. All real, all large, none of them convert until an enterprise
conversation exists. Building for a buyer who has not appeared is the expensive
form of guessing.

**Phase 6 counting.** Still parked. It needs typed rows and SQL aggregation,
not better extraction.

**Another benchmark run.** 79.4% with a disclosed trade-off is already a better
artifact than a bigger number without a receipt. A higher score does not fix
2 stars.

> Note added 2026-10-01: the harness behind that number showed the reader model
> each session's id, and in that dataset every evidence session id starts with
> `answer_`. The effect is unmeasured, so the number may be overstated by an
> unknown amount. See the README Benchmark section.

---

## The writing, which is not optional

Two pieces, independent of all four tracks, aimed at the actual bottleneck:

1. **The governance design point.** Held-out corpora with thresholds fixed
   before the work exists. A membership diff requiring every moved rule to be
   listed. A tag that could not be deleted to cover a mistake, and the version
   number burned instead. This is the differentiator, and nobody knows it
   exists.
2. **Benchmark comparability.** Why a single-run receipt with disclosed
   trade-offs and a committed reproduction script is worth more than a headline
   number, and how to read the memory-benchmark claims currently in circulation.

These are what a prospective client reads. The engineering is not the
constraint.
