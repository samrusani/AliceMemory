# The Memory Operations Protocol

Agent memory is only trustworthy if every write can be traced, checked, and
taken back. Alice exposes memory as a small set of verbs — the same verbs on
MCP, HTTP, and the CLI — and every one of them runs through the same policy
engine and lands in the same audit trail. Agents request; Alice decides.

Ten verbs cover the lifecycle of a memory:

| Verb | What it does | Status |
|---|---|---|
| [remember](#remember) | Write a memory — explicit commit or source-backed capture | Shipped |
| [recall](#recall) | Search memory with hybrid full-text + vector retrieval | Shipped |
| [correct](#correct) | Fix an existing memory, keeping the old version | Shipped |
| [confirm](#confirm) | Complete a write that policy held for confirmation | Shipped |
| [undo](#undo) | Reverse a commit without erasing its history | Shipped |
| [forget](#forget) | Retire a memory from recall on request | Shipped |
| [audit](#audit) | Explain a memory: sources, revisions, events | Shipped |
| [merge](#merge) | Accept a consolidation candidate; supersede its members | Shipped |
| [expire / unexpire](#expire--unexpire) | Close or reopen a memory's validity window | Shipped |
| [redact](#redact) | Scrub governed memory copies, keeping the audit skeleton and source evidence | Shipped |

All shipped verbs are on the default (core) MCP surface — no
`ALICE_MCP_LEGACY_TOOLS` flag needed — and all of them work in SQLite
on-ramp mode as well as against Postgres.

## The outcome vocabulary

A write is never a silent success or a silent drop. Every `remember` returns
one of four outcomes, decided by the memory commit policy engine:

| Outcome (`status`) | Write mode | Meaning |
|---|---|---|
| `committed` | `commit` | Written as active, trusted memory immediately |
| `confirmation_required` | `confirm_inline` | Held for an inline yes/no — finish with [confirm](#confirm) |
| `review_required` | `propose_review` | Parked as a candidate for human review |
| `rejected` | `reject` | Blocked, with machine-readable `reasons` |

What routes where (from `evaluate_memory_commit_policy`):

- Blocked policy, a read-only caller, or secret-looking content (API keys,
  tokens, passwords) → `rejected`.
- Confidence below 0.5, external source types (email, web pages, generated
  artifacts), non-explicit intent, or bulk source references → `review_required`.
- Confidence between 0.5 and 0.85, sensitive domains (health, family,
  financial, legal, spiritual), or declared contradictions →
  `confirmation_required`.
- An agent (keyed, or keyless with a declared agent identity) committing
  above its sensitivity ceiling is `rejected` with reason
  `sensitivity_above_agent_ceiling` and no pending row, including when the
  checks above would have returned `confirmation_required` or
  `review_required`. The owner (a keyless call with no agent identity), an
  `admin_agent` key, and a keyless call that declares
  `permission_profile: admin_agent` still get `confirmation_required` for
  a confidential write. A keyless server does not verify a declared
  profile. That is keyless owner mode.
- Everything else from a trusted or project-scoped agent → `committed`.

## Audit guarantees

Mutating verbs append audit evidence to revisions and events. Read-only verbs
do not append. These stores are append-only during ordinary lifecycle
operations; exact authorized true redaction is the narrow exception that
rewrites governed content copies to content-free skeletons:

- **Revisions** (`memory_revisions`): each state change records the previous
  and new value, the text before and after, a typed `revision_type`
  (`created`, `corrected`, `promoted`, `rejected`, `superseded`, `archived`,
  …), the reason, and the actor.
- **Events** (`event_log`): each verb emits a typed event
  (`agent.memory_committed`, `agent.memory_confirmed`,
  `agent.memory_corrected`, `agent.memory_undone`, `agent.memory_forgotten`,
  `agent.memory_expired`, `agent.memory_unexpired`,
  `agent.memory_consolidation_accepted`, `memory.redacted`, plus the policy
  decision events), correlated by `trace_id` and the agent's `run_id`.

Committed memories with source references also carry **provenance links** to
those references, so [audit](#audit) can answer "where did this come from?".
An explicit commit may legitimately have no source reference.

## Identity and authentication

Every write verb accepts agent identity fields (`agent_id`, `agent_type`,
`agent_run_id`, `task_id`, `project_scope`, `permission_profile`). Direct
human/local calls (Claude Desktop, the CLI as yourself) may omit them
entirely — the write runs as the local operator. Agent integrations
declare identity, and `read_only_agent` callers cannot write. When
per-agent API keys exist, identity is resolved and enforced from the key
record — on HTTP via `Authorization: Bearer alice_sk_...`, on MCP via
`ALICE_AGENT_API_KEY` in the server environment. Claiming another agent's
id or a higher permission profile is refused and logged. See
[agent-integration.md](alpha/agent-integration.md).

---

## remember

Two paths, one trust boundary:

- **Explicit commit** — the user said "remember this" and the agent writes
  it through policy:
  - MCP: `alice_memory_commit`
  - HTTP: `POST /v0/vnext/memories/commit`
  - CLI: `alicebot vnext memories commit --title ... --text ...`
- **Source-backed capture** — documents, notes, and evidence that become
  trusted memory only after review:
  - MCP: `alice_capture`
  - HTTP: `POST /v0/vnext/sources`
  - CLI: `alicebot vnext agents ingest-output ...` (for agent outputs)

Outcomes: the four-outcome vocabulary above for commits; captures return
`imported` with candidate memories that wait in the review queue.

Audit: a written row gets a `created` revision and an
`agent.memory_committed`, `agent.memory_confirmation_required`, or
`agent.memory_review_required` event. A refusal writes
`agent.memory_commit_rejected` and does not write a memory row, a revision,
or provenance. An agent refusal also writes `policy.decision`. A ceiling
refusal also writes `agent.policy_filtered` when the policy decision is
`allowed_with_filtering`, and `agent.policy_blocked` when that decision is
`blocked`.
Commits accept an `idempotency_key`; retries replay the original result
instead of double-writing.

## recall

Hybrid retrieval: full-text and semantic vector search fused with
reciprocal-rank fusion, filtered by domain, sensitivity, memory type, and
project scope.

- MCP: `alice_recall` (single query) and `alice_context_pack` (task-scoped
  bundle with a `max_tokens` budget and a `token_report` of what was dropped)
- HTTP: `POST /v0/vnext/context-packs`
- CLI: `alicebot context-pack "query" ...`

Both tools accept `context_depth` (`minimal` | `low` | `medium` | `high` —
cost/coverage tier; `minimal` is full-text only) and `budget_strategy`
(`balanced` | `facts_first` | `recent_first` | `contradictions_first` |
`sources_first` — how the token budget is spent). The
`include_sources`/`include_contradictions` flags are tri-state: omitted
means the `context_depth` tier decides; an explicit true/false always wins.

Only searchable statuses (`active`, `accepted`) are returned: candidates,
rejected, superseded, forgotten, and [expired](#expire--unexpire) memories
never leak into results.

## correct

Two correction surfaces, both audited:

- **Review-queue correction** — act on a memory awaiting review:
  - MCP: `alice_memory_correct` with `action` of `approve`,
    `edit-and-approve`, `reject`, or `supersede-existing`
  - HTTP: `POST /v0/vnext/memories/{memory_id}/review` (actions `accept`,
    `edit`, `reject`, `private`, `assign_project`, `promote`)
  - Console: the `/vnext` review queue drives the same endpoint
- **Agentic correction** — rewrite the text of a committed memory:
  - HTTP: `POST /v0/vnext/memories/correct`
  - CLI: `alicebot vnext memories correct <memory_id> --text ...`

Outcome: `committed` with the corrected text active. Audit: a `corrected`
revision storing the text before and after, and an
`agent.memory_corrected` event. The pre-correction text is preserved in the
revision history, and correction history accumulates on the memory record.

## confirm

Completes a write that policy held as `confirmation_required` (the pending
memory is not searchable until confirmed).

- MCP, default three tools: `alice_memory_commit` with only the
  `confirmation_id` from the commit response and `confirmation_action`
  (`confirm` or `reject`), plus identity fields and an optional
  `rationale`. A memory field on that call is refused, and there is no
  edit: to change the text, reject it and commit the corrected text.
- MCP, full surface: `alice_memory_manage` with `action: "confirm"` and the
  `confirmation_id`; pass `canonical_text` to confirm with a correction
- HTTP: `POST /v0/vnext/memories/confirm`
- CLI: `alicebot vnext memories confirm <confirmation_id> [--action confirm|reject|edit]`

Both MCP routes call `VNextMemoryCommitService.confirm` through the same
handler code, so identity, the policy check on the pending row's domain,
sensitivity and project scope, and the audit below are the same. The
project scope check binds a key-bound scope; a keyless server trusts
whatever `project_scope` the caller declares. Both routes use the
service ceiling: a mutation of a target above the caller's sensitivity
ceiling is blocked, including confirm, forget, expire and undo. An agent
commit above that ceiling is rejected with no pending row. The receipt
says this was not saved, do not retry with a lower sensitivity label,
tell the user, and the owner can raise this agent's clearance or store
the memory themselves. The owner (a keyless call with no agent identity),
an `admin_agent` key, and a keyless call that declares
`permission_profile: admin_agent` are not held to that ceiling. A keyless
server does not verify a declared profile. That is keyless owner mode.
Only the author, an `admin_agent` key, or the
owner (a keyless call with no agent identity) can confirm or reject a
pending write. On a keyless install that limit is not protection: the
caller can declare the author's agent_id. The author can still reject
their own pending write above the ceiling. Confirming a row that is not
pending is refused and writes nothing. Neither route can
tell whether the user was asked; the tool description tells the agent to
ask. The revision, the policy events and the `agent.memory_confirmed` or
`agent.memory_confirmation_rejected` event name the caller as
`actor_id`: the key's `agent_id` when `ALICE_AGENT_API_KEY` is set, the
declared and unverified `agent_id` on a keyless server. The
`memory.updated` and `memory_revision.created` events carry no
`actor_id`. A keyless call that declares no `agent_id` is recorded as
`actor_type: user` with no `actor_id` on every row and no policy event.

Outcomes: `committed` (memory becomes active) or `rejected`. A pending
confirmation stays out of recall until it is answered, and nothing
expires it in the background. Only `VNextMemoryCommitService.confirm`
reads the 24 hour `expires_at`: after it, a confirm or reject through
either MCP route above, the HTTP confirm route or the CLI confirm that
passes the policy check resolves the row to `rejected` with reason
`confirmation_expired` instead of acting on it. The review paths do not
read it: `alice_memory_correct` `approve` and a correction through
`POST /v0/vnext/memories/correct` or `alicebot vnext memories correct`
can still make the row active after 24 hours. Audit: a `promoted`
revision (`corrected` when text was edited, `rejected` for a reject or an
expiry) and an `agent.memory_confirmed`,
`agent.memory_confirmation_rejected` or
`agent.memory_confirmation_expired` event.

## undo

Reverses a commit. The memory leaves recall; its history stays.

- MCP: `alice_memory_manage` with `action: "undo"` (`memory_id` optional —
  defaults to the calling agent's most recent commit)
- HTTP: `POST /v0/vnext/memories/undo`
- CLI: `alicebot vnext memories undo [--memory-id ...]`

Outcome: `undone`; the memory's status becomes `superseded`. Audit: a
`superseded` revision and an `agent.memory_undone` event.

## forget

Retires a memory on request — "stop using this."

- MCP: `alice_memory_manage` with `action: "forget"` and `memory_id`
- HTTP: `POST /v0/vnext/memories/forget`
- CLI: `alicebot vnext memories forget <memory_id> [--reason ...]`

Outcome: `forgotten`; the memory's status becomes `superseded`. Audit: an
`archived` revision and an `agent.memory_forgotten` event.

**The honest boundary:** forget is a soft delete plus exclusion. The memory
disappears from recall, context packs, and resume briefs, but its content
remains in the revision history and event log — that is what makes forget
reversible and auditable. When the content itself must go, use
[redact](#redact).

## audit

Explains a memory end to end: the row itself, every revision, every event,
and its provenance links back to sources.

- MCP: `alice_explain` with `memory_id` (also accepts `continuity_object_id`
  or `entity_id` on the Postgres backend)
- HTTP: `GET /v0/vnext/memories/{memory_id}/audit`
- CLI: `alicebot vnext memories audit <memory_id>`

Recent write activity is listable via `GET /v0/vnext/memories/recent-commits`
and `alicebot vnext memories recent`.

## merge

Consolidating near-duplicate memories into one, behind the candidate/review
gate. The consolidation pipeline proposes candidates; **accepting** one is
the merge decision, and acceptance is restricted to a human reviewer or an
admin agent (any other agent profile is blocked with
`human_or_admin_review_required`).

- MCP: `alice_memory_manage` with `action: "accept_consolidation"`,
  `memory_id` (the candidate), and a required `reason`
- HTTP: `POST /v0/vnext/memories/accept-consolidation`
- CLI: `alicebot vnext memories accept-consolidation <memory_id> --reason ...`

Outcome: `accepted` — the candidate is promoted to active and every memory
in the proposal's `proposed_supersede` list is superseded by it (real
`superseded_by` pointer columns, one revision and one event per member).
`dedup` proposals record content lineage with a `supersedes` pointer to the
survivor; `merge` proposals record the full member list in
`metadata_json.merged_from`. Replaying an acceptance is a no-op with a note.
Audit: a `promoted` revision on the accepted row, `superseded` revisions on
the members, and an `agent.memory_consolidation_accepted` event.

## expire / unexpire

Ages out time-bounded facts by closing the memory's validity window —
temporal exclusion, not a lifecycle judgment: the row's status stays
`active`, but recall, context packs, and briefs stop returning it once
`valid_to` passes (the staleness sweep later marks long-expired rows
`stale`). Unexpire reopens the window. Both require a `reason`.

- MCP: `alice_memory_manage` with `action: "expire"` (optional `valid_to`
  ISO-8601 timestamp, default now) or `action: "unexpire"`
- HTTP: `POST /v0/vnext/memories/expire`, `POST /v0/vnext/memories/unexpire`
- CLI: `alicebot vnext memories expire <memory_id> --reason ... [--valid-to ...]`
  and `alicebot vnext memories unexpire <memory_id> --reason ...`

Outcomes: `expired` (with the effective `valid_to`) and `active`.
Unexpiring a memory that has no validity end replays as a no-op with a
note. Superseded and rejected rows cannot be expired or unexpired. Audit:
an `edited` revision plus an `agent.memory_expired` /
`agent.memory_unexpired` event recording the window change and reason.

## redact

Scrubs the governed copies in a memory's lifecycle and any coupled terminal
project-update artifact — for content that must leave memory recall and those
lifecycle records, not just become hidden. Redaction is destructive within
that scope, requires a `reason`, and is restricted to a human operator or an
admin agent. It does not erase Alice source or source-chunk evidence.

- MCP: `alice_memory_manage` with `action: "redact"`, `memory_id`, and
  `reason`
- HTTP: `POST /v0/vnext/memories/redact`
- CLI: `alicebot vnext memories redact <memory_id> --reason ...`

Order of operations: if the memory is still live it goes through the
[forget](#forget) flow first (so the lifecycle trail records why it left
recall), then one transaction expunges content from the memory row and its
coupled revisions, event payloads, and quoted provenance. For a terminal
project-update candidate, the same transaction also scrubs the accepted,
edited, or rejected artifact and its quality-rating prose. The already-applied
project state is intentionally retained: redaction scrubs the governed
memory/artifact copies; it does not undo the reviewed project update.

**Honest semantics:** governed memory/artifact copies are expunged; the audit
skeleton and source evidence are not. Governed text uses the exact
`[REDACTED]` marker, governed non-null free-form JSON uses the exact
`{"redacted":true}` marker, and nullable fields that were null stay null.

- *Expunged*: the memory key is replaced, title/canonical text/summary/trust
  prose and other governed text become `[REDACTED]` when non-null, value and
  governed free-form JSON become `{"redacted":true}`, arbitrary metadata is
  removed, `commit_digest` and `confirmation_id` are cleared, and source-event
  ids, embeddings, and fact keys are cleared. Revision content and reasons,
  quoted provenance, coupled event payload content/integrity hashes, terminal
  artifact text/model/prompt inputs, and quality-rating prose/arbitrary
  metadata are scrubbed to their exact content-free shapes. Null content fields
  remain null.
- *Retained*: ids and identity/link columns; memory type, domain, sensitivity,
  confidence, salience, trust class, promotion eligibility, evidence counts,
  extracted-by-model, validity/seen/review timestamps, `confirmation_status`,
  and `last_confirmed_at`; lifecycle state and timestamps; revision/event types,
  actors, and sequence numbers; terminal artifact status/review linkage; and all
  six numeric rating dimensions (`usefulness`, `accuracy`, `source_grounding`,
  `novel_connections`, `actionability`, `hallucination_risk`), categorical
  `verbosity`, reviewer/id, and timestamp. Alice source rows and source chunks,
  including their evidence content, are also retained: they may support other
  memories and require a separate source hygiene action.
- *Proof*: the row is archived and the `memory.redacted` event trail
  proves the redaction happened and when. Repeating the operation after the
  complete skeleton exists is a write-free idempotent replay.

Works on both backends (Postgres and the SQLite on-ramp). Redaction is not
reversible. SQLite has no generated-artifact or quality-rating subsystem, so
its response reports zero for those coupled counts. Redaction is not a
substitute for source or backup hygiene. Alice source/source-chunk evidence
inside the store is intentionally out of this memory-lifecycle operation's
scope because it may be shared. Upstream providers, prior exports, replicas,
and backups also need their own erasure policy.

---

For the full MCP tool schemas see [docs/alpha/mcp-tools.md](alpha/mcp-tools.md);
for identity, keys, and worked agent examples see
[docs/alpha/agent-integration.md](alpha/agent-integration.md).
