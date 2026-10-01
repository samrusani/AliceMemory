# Current State

## Snapshot

- `v0.19.0` is the latest published release. It is available from PyPI and
  GitHub, its record is immutable, and exact artifact digests are in
  `docs/release/v0.19.0-checksums.txt`. `v0.18.0` is the immediately prior
  published release.
- Earlier releases whose headlines still get referenced: `v0.13.1` shipped the
  Phase 4 core-roadmap work as **Replicated benchmark, faster SQLite at scale,
  reference integrations**, and `v0.12.0` shipped the Phase 3 structural
  refactor as **Structure only. Zero behavior change.** Records for both remain
  under `docs/release/`.
- Two tags exist that were never published: `v0.13.0`, superseded by `v0.13.1`,
  and `v0.15.0`. Neither has a GitHub Release or a PyPI artifact.
- LongMemEval_s is 81.2%, a mean of three runs on `v0.12.0` `store_chunks`.
  It is not the product path and not a measurement of the current release.
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

## v0.12.0 Verification Posture

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

`v0.19.0` is tagged, published, and immutable. Its authoritative records are:

- `docs/release/v0.19.0-release-notes.md`
- `docs/release/v0.19.0-checksums.txt`

`v0.18.0` is the immediately prior published release; its records are
`docs/release/v0.18.0-release-notes.md` and `docs/release/v0.18.0-checksums.txt`.

Every earlier release remains published and immutable, with its own
`docs/release/vX.Y.Z-release-notes.md` and `vX.Y.Z-checksums.txt`. That includes
`v0.13.1`, `v0.12.0` and `v0.11.1`, which are referenced elsewhere in this
document.

Two tags exist that were never published and never will be, because stable tags
are immutable and the numbers are retired rather than reused: `v0.13.0`,
superseded by `v0.13.1`, and `v0.15.0`, whose commit carried a release-gate step
that could not run on a CI runner.

## What `v0.19.1` Targets

`v0.19.1` is the current release candidate. It is not published.

It takes the fixes on `main` since `v0.19.0`. Most come from an internal
security review of `v0.19.0`. It adds no tool or command and changes no schema. The
Hermes provider keeps a turn's user and assistant text apart, the `/v1` memory
operations commit applies only a user turn with an explicit prefix, backup
import restores a stored key claim as unverified and lists credential-shaped
text it does not refuse, and the session-start hook no longer goes blank for a
note that contains protocol text. Recall and the context pack leave out memory
ids the caller cannot read, and refuse a query the SQLite source search cannot
take.

- [v0.19.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.19.1-release-notes.md)

`v0.19.1` has these changes. `v0.19.0` does not.

- `alice-memory import` restores a stored claim that an agent API key wrote a
  row as `auth: imported_claim`, keeps the original as `claimed_auth`, and
  prints `provenance claims restored as unverified: N`. Recall compares `auth`
  exactly. Import also lists credential-shaped text in records it does not
  refuse, and `alice-memory doctor` reads source chunk text. In `v0.19.0` a
  restored row can read `verified_by_key` with no key behind it, and a token
  that sits only in a chunk prints `flagged sources: 0`.
- `POST /v1/memory/operations/commit` applies without review only a user turn
  that matched an explicit prefix at confidence 0.9 or more, as the
  `/v0/continuity` capture commit does. The rest is `review_required`. In
  `v0.19.0` the policy reads no role.
- Hermes provider 0.5.2 sends the user text and the assistant text of a turn as
  separate fields. In `v0.19.0` the provider, 0.5.1, splits a joined turn back
  into roles by line. The provider is not in the wheel.
- The session-start hook shows the brief when a stored note contains `jsonrpc`
  or `Content-Length:`, and refuses a relative `ALICE_MEMORY_DATA_DIR`. In
  `v0.19.0` the first prints `{}` and the second creates a vault under the
  current directory.
- `alice_recall` and `alice_context_pack` leave out a memory id the caller
  cannot read, in the correction label, a pack's `supersedes` and
  `superseded_by`, `validity`, `recent_changes` and `supersession_context`. In
  `v0.19.0` they name it. Ids copied into a stored memory's `metadata_json` are
  not fenced.
- `alice_recall` and `alice_context_pack` refuse a query of more than 499
  distinct search terms or over 40,000 UTF-8 bytes with `invalid_request`. In
  `v0.19.0` a query of about 991 distinct terms or more answers
  `tool_execution_failed`.
- `alice_memory_review` is read-only, so six core tools are read-only, two are
  non-destructive and three are destructive. `alice_memory_commit` with
  `confirmation_id` updates the one pending row it names, and by the approval
  rule the code records it does not prompt in Codex's default mode.
- The eval uses a fixed reference time, so its numbers no longer depend on the
  day it runs. The commit author check accepts exact addresses only.
- Not fixed, and listed in the release notes: the Postgres HTTP API reads a body
  before it checks credentials and does not check the Host header, provider
  clients follow redirects, and the local-folder scan has no size limit and can
  read a file swapped for a link.

## What `v0.19.0` Shipped

`v0.19.0` is the latest published release and remains the install, checksum,
and baseline reference.

It shipped the work on `main` after `v0.18.0`. Codex is a new opt-in install
host that writes an MCP entry and a SessionStart hook. The Claude Code plugin
directory is in the repo. The session brief is cut to fit Claude Code's size
cap, leaves out superseded facts, and no longer fails on a long newest fact,
open loop, query or source title. Every MCP tool declares hints, and a
relative or empty `--data-dir` is refused. There is no schema change.

- [v0.19.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.19.0-release-notes.md)

`v0.19.0` has these changes. `v0.18.0` does not.

- `alice-memory install --host codex` writes an `alice` entry into Codex's
  `config.toml`, edited as text, and a SessionStart hook into `hooks.json`.
  Codex is opt-in and the default hosts are unchanged. Install never writes
  trust, so Codex skips the hook until you trust it once. When `config.toml`
  already defines hooks, install prints the hook, exits 1, and uses the error
  code `install_hook_by_hand`. `v0.18.0` has no Codex host.
- `plugins/alice-memory` holds a Claude Code plugin. Its version and both
  command pins equal the package version. Install skips when the plugin is
  enabled and install has not written Claude Code entries, and refuses when
  both exist. The tag has no marketplace file. `main` has
  `.claude-plugin/marketplace.json`, named `alicememory`, which pins the plugin
  to the `v0.19.0` tag commit, so the plugin installs from that marketplace.
  `v0.18.0` has no plugin.
- The session brief cuts a long note to at most 1,500 characters, stays under
  9,500 characters, and leaves out a superseded fact and a source line whose
  captured sentence was corrected later. A long newest fact, open loop,
  explicit query or source title no longer empties it, including one with
  more than about 990 distinct words. `alice_recall` and
  `alice_context_pack` are not changed and still return a tool error for a
  query with about 991 or more distinct search terms, or, with a captured
  source in the vault, over about 50,000 bytes. In `v0.18.0` a note
  over the token budget was dropped, a brief could pass 15,000 characters,
  and a fact with more than about 990 distinct words, or over about 50,000
  bytes with a source in the vault, made the hook print `{}`.
- Every MCP tool sets `openWorldHint` to false. Five tools are read-only, two
  are non-destructive, and four are destructive. `v0.18.0` tools declare no
  hints. From `v0.19.1`, `alice_memory_review` is
  read-only, so with the full tool set six tools are read-only and three are
  destructive.
- `alice-memory mcp` refuses an empty or relative `--data-dir` with exit 2,
  and the session-start hook prints one line and exits 0 for a relative
  `--data-dir` or plugin `data_dir`. A relative `ALICE_MEMORY_DATA_DIR` is
  not checked. `v0.18.0` creates the vault under the current directory.
  From `v0.19.1`, the hook refuses a relative
  `ALICE_MEMORY_DATA_DIR` the same way.
- `alice-memory install --host hermes` keeps a comment in the `alice` block
  when nothing needs to change, and refuses when a change is needed. In
  `v0.18.0` the re-run drops the comment.
- Folder-import receipt items name the file, a withheld ChatGPT title is
  counted, `alice_capture` refuses a low-entropy AKIA-shaped key, and both
  imports withhold it. In `v0.18.0` capture stores that key.

## What `v0.18.0` Shipped

`v0.18.0` is the immediately prior published release.

It shipped the work on `main` after `v0.17.0`. OpenCode is a new opt-in
install host. Every capture path refuses credential material, resume reads
only active memories, and `alice-memory import-markdown` and
`import-chatgpt` bring notes and chat exports into SQLite with a per-line
credential filter. Install receipts escape control characters, and the
credential check reads provenance and imported values with their keys.
There is no schema change.

- [v0.18.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.18.0-release-notes.md)

`v0.18.0` has these changes. `v0.17.0` does not.

- `alice-memory install --host opencode` writes an OpenCode MCP entry to
  `opencode.json` or `opencode.jsonc`. OpenCode is opt-in and the default
  hosts are unchanged. `v0.17.0` and earlier have no OpenCode host.
- `POST /v0/continuity/captures` runs the credential check on the text before
  it stores anything, and returns 400 when the check refuses. In `v0.17.0`
  this route checks for credentials only when the capture derives a continuity
  object, so a capture left in triage is stored unchecked.
- Provenance and the import `value` column are read with their keys, so a
  secret name over a secret-shaped value is refused. In `v0.17.0` they are
  read by value only, so a password or API key under a secret name is stored
  unless the value identifies itself.
- Continuity capture auto-save, in assist and auto mode, saves only a user
  turn that matches an explicit prefix; the rest are queued. A new continuity
  object gets the same credential check as memory commit. In `v0.17.0`, assist
  saves any explicit match at confidence 0.9 or more from either role, auto
  saves at 0.85 or more, and only the credential floor runs, so
  `PASSWORD_DB=<value>` is stored. That rule is the `/v0/continuity` capture
  commit. The `/v1` memory operations commit reads no role in `v0.19.0`:
  assist applies an explicit match of an allowed type at 0.9 or more from
  either role, and auto applies any match of an allowed type at 0.9 or more.
  From `v0.19.1`, it applies only a user turn that
  matches an explicit prefix, as the capture commit does.
- A JSON write under `/v0` that names the user only in the
  `X-AliceBot-User-Id` header reaches the route with that user in the body. In
  `v0.17.0`, `POST /v0/continuity/captures/candidates` answers that request
  with 422, so Hermes memory provider turn capture fails.
- A blocked idempotent replay of `POST /v0/vnext/memories/commit` returns 403
  when the stored memory matches the request and 400 when it does not. The
  policy rows are kept, except after a lost insert race. In `v0.17.0` the same
  replay is a server error and the policy rows roll back.

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

`v0.17.0` is an earlier published release.

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

`v0.16.0` is an earlier published release.

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
