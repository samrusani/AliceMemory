# Current State

## Snapshot

- `v0.21.1` is the latest published release. It is available from PyPI and
  GitHub, its record is immutable, and exact artifact digests are in
  `docs/release/v0.21.1-checksums.txt`. `v0.21.0` is the immediately prior
  published release.
- Earlier releases whose headlines still get referenced: `v0.13.1` shipped the
  Phase 4 core-roadmap work as **Replicated benchmark, faster SQLite at scale,
  reference integrations**, and `v0.12.0` shipped the Phase 3 structural
  refactor as **Structure only. Zero behavior change.** Records for both remain
  under `docs/release/`.
- Six tags exist that were never published: `v0.9.3`, a withdrawn security
  candidate; `v0.10.0`, which failed its release gate; `v0.13.0`, superseded by
  `v0.13.1`; `v0.15.0`; `v0.19.1`, superseded by `v0.19.2`; and `v0.20.1`,
  superseded by `v0.20.2`. None has a PyPI
  artifact or a published GitHub Release. `v0.10.1` has a GitHub Release with no
  files and no PyPI artifact, because its publish failed; `v0.10.2` superseded
  it.
- LongMemEval_s is 81.2%, a mean of three runs on `v0.12.0` `store_chunks`.
  It is not the product path and not a measurement of the current release.
  Known issue: the harness showed the reader model each session's id, and in
  this dataset every evidence session id starts with `answer_`. The effect is
  unmeasured, so the number may be overstated by an unknown amount. See
  `docs/benchmarks/longmemeval/README.md`.
- Alice remains public-alpha, pre-1.0, local-first, single-user, and self-hosted.
- The MCP registry on main is 12-core/65-legacy/77-total. The default handshake
  stays at three tools. `ALICE_MCP_FULL_TOOLS=1` exposes all twelve core tools.

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
  by
  `docs/handoff/2026-07-18-v0.12.0-phase3-structural-refactor/BUILD_REPORT.md`.
  The builder matrix ran before the version cut; its locally produced artifacts
  were verification inputs only and were never uploaded anywhere.
- No security or cybersecurity audit was performed in Phase 3.

## Release Boundary

`v0.21.1` is tagged, published, and immutable. Its authoritative records are:

- `docs/release/v0.21.1-release-notes.md`
- `docs/release/v0.21.1-checksums.txt`

`v0.21.0` is the immediately prior published release; its records are
`docs/release/v0.21.0-release-notes.md` and `docs/release/v0.21.0-checksums.txt`.

Every earlier release remains published and immutable, with its own
`docs/release/vX.Y.Z-release-notes.md` and `vX.Y.Z-checksums.txt`. That includes
`v0.13.1`, `v0.12.0` and `v0.11.1`, which are referenced elsewhere in this
document.

Six tags exist that were never published and never will be, because stable
tags are immutable and the numbers are retired rather than reused: `v0.9.3`, a
withdrawn security candidate; `v0.10.0`, whose protected release gate failed;
`v0.13.0`, superseded by `v0.13.1`; `v0.15.0`, whose commit carried a
release-gate step that could not run on a CI runner; `v0.19.1`, whose publish run failed at the
draft readback because a release script imported the package in a job that does
not install it; and `v0.20.1`, whose publish run reached its first job's time
limit while it reran the unit suite, before anything was built. Neither of the
last two has a PyPI artifact or a published GitHub Release.

## What `v0.22.0` Targets

`v0.22.0` is the current release candidate. It is not published.

It takes the fixes and additions on `main` after `v0.21.1`. The agent output route
refuses a request that its agent policy blocks, and the chunks of imported and
captured notes can be embedded and used to rank passages, as an opt-in that is off by
default. It adds no tool and no migration: PostgreSQL stays at revision
`20261011_0097`. It adds five settings, the value `vectors` of `ALICE_SEARCH_QUALITY`
and the flags `--sources` and `--drop` of `alice-memory reindex-embeddings`.

- [v0.22.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.22.0-release-notes.md)

`v0.22.0` has these changes. `v0.21.1` does not.

- `POST /v0/vnext/agents/ingest-output` answers HTTP 403 and writes no source, chunk,
  artifact, provenance link or memory when the agent policy blocks the request, as
  `POST /v0/vnext/sources` does. It answers HTTP 400, not HTTP 500, for credential
  material. A call with no key that names an agent and declares no profile is held
  as a read-only agent and is refused.
- With `ALICE_SOURCE_EMBEDDINGS=on`, the chunks of imported and captured notes are
  embedded after the import or capture commits, only to an endpoint on this machine.
  The vectors are kept in a SQLite table that is made when the first one is stored.
- `ALICE_SEARCH_QUALITY=vectors` ranks passages with those vectors on the SQLite
  store. Both settings are off by default, and no measurement of the vectors is
  published.
- The bench scripts, the scripted walkthrough and the web console changes are not
  part of the PyPI package.
- Still open, listed in the release notes: remote embeddings endpoints are not
  supported for source text, deleting a vector is logical deletion, and the rows
  that `v0.21.1` wrote for blocked agent output requests stay.

## What `v0.21.1` Shipped

`v0.21.1` is the latest published release and remains the install, checksum,
and baseline reference.

It is a patch release on `v0.21.0` and shipped two changes from `main`. The
credential check no longer reads a short product code or a variable name as a
credential, and it reads a token right after an underscore. The web console
moves to Next.js 16.4.1 for two advisories. It adds no tool, command, setting or
migration: PostgreSQL stays at revision `20261011_0097`.

- [v0.21.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.21.1-release-notes.md)

`v0.21.1` has these changes. `v0.21.0` does not.

- A token marker (`ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `glpat-`, `npm_`,
  `sk-`, `xoxb-`, `xoxp-`, `xoxa-`, `xoxs-`) counts only when a floor of token
  characters follows it. A code such as `SK-2291` and a name such as
  `npm_config_cache` are not read as credentials, so `alice_capture` accepts the
  text and `alice-memory import-markdown` keeps the line and the file.
- A token right after an underscore (`_ghs_` and a token, `TOKEN_npm_` and 36
  characters) is read at the commit gate, `alice_capture`, both imports and the
  promotion credential floor. A marker glued directly onto a letter or a digit is
  not read, as in `v0.21.0`.
- The web console uses Next.js 16.4.1 and `eslint-config-next` 16.4.1. The PyPI
  package does not include the console.
- Still open, listed in the release notes: `alice-memory doctor` reads sources
  and not the text of a memory, and the floors of `github_pat_`, `glpat-`,
  `npm_`, `sk-` and the Slack tokens are not a published minimum.

## What `v0.21.0` Shipped

`v0.21.0` is the immediately prior published release.

It shipped the fixes and additions on `main` after `v0.20.2`. Passage ranking
becomes the default of `alice_recall` on SQLite, a key with limits is no longer
shown the ids of rows it may not read and gets a full page when hidden rows sit
in front of it, and the doors that write `source_refs` refuse entries that no
producer writes. It adds the tool `alice_what_changed` to the full tool
surface, and the route `POST /v0/vnext/changes`. It adds no migration:
PostgreSQL stays at revision `20261011_0097`.

- [v0.21.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.21.0-release-notes.md)

`v0.21.0` has these changes. `v0.20.2` does not.

- On SQLite, `alice_recall` runs passage mode by default: up to three passages
  from one document, ranked across documents. `ALICE_SEARCH_QUALITY=off` restores
  the `v0.20.2` ranking, and a value other than `off` or `passage` makes the
  tool answer `tool_request_failed`. PostgreSQL keeps document mode.
- A key with limits is not shown the id of a memory, a source, an artifact or an
  open loop it may not read, in any field at any depth of an answer, in any
  spelling and behind the prefixes `memory:`, `source:`, `artifact:` and
  `open_loop:`. A count beside a list that lost an id is recomputed or dropped.
  The owner and an unbound admin key keep every id.
- The vector stage of recall and of the context pack, the time stage of the
  context pack, the context tree, the workspace and the project list refill
  after hiding rows for a key with limits. A page is read once, at 128 rows
  (`READABLE_SCAN_CAP`), and is full while the hidden rows in front of it number
  at most 128 minus the rows the list is asked for: 118 for `alice_recall` at
  limit 5, which asks its vector stage for ten rows. The owner and an unbound
  admin key are read as before.
- The memory commit, the memory proposal and the agent-output ingest routes, the
  sync of the `agent_output` connector, their MCP tools and the `--source-ref`
  flag refuse a `source_refs` entry that is an object, a number, a boolean, null,
  a nested array, a blank string or the text of a JSON object or array.
- `alice_what_changed` and `POST /v0/vnext/changes` answer the recorded decision
  history of one topic as chains of current, upcoming, disputed and past steps.
  The full tool surface holds twelve core tools. `alice_memory_correct` with
  `replacement_body.decision_text` stores that text.
- A killed `alice-memory import` no longer leaves its working copy in the data
  directory: the next import into that vault and the next open of an existing
  vault remove it.
- Still open, listed in the release notes: an id inside free text is not removed,
  and redaction restricts reports and does not remove the words from them, which
  is planned with no release set.

## What `v0.20.2` Shipped

`v0.20.2` is an earlier published release.

It shipped the fixes and additions on `main` after `v0.20.0`. Most are security
fixes: a derived summary, report or copy follows the labels of its inputs, and
the operator routes, the event feeds, the doors that act on one row by id and
the readers of saved quotes and of open-loop ids apply the caller's limits. It
adds no MCP tool. It adds commands (`alice-memory project`, `alice-memory
labels`, `alicebot vnext labels`, the SQLite `sources` commands and
`import-markdown --supersede`) and three settings, `ALICE_PROJECT_DIR`,
`ALICE_PROJECT_SCOPING` and `ALICE_MCP_COMMIT_RESULT`. Per-project scoping and
the compact commit result are off by default. PostgreSQL migrations
`20261004_0095`, `20261005_0096` and `20261011_0097` repair stored labels, and a
SQLite vault is repaired on its next open.

- [v0.20.2 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.20.2-release-notes.md)

`v0.20.2` has these changes. `v0.20.0` does not.

- A derived row is stored at least as strict as its inputs and follows them when
  they are relabelled. `alice-memory labels check` and `alicebot vnext labels
  check` list the rows that are below their inputs or unverified, and `labels
  repair` raises them. After the upgrade, a derived row whose inputs are
  confidential is labelled confidential and a derived row with no record is
  unverified. Either one leaves the owner's default recall, pack and resume, as
  any confidential row did in `v0.20.0`.
- Thirteen operator routes, source review and delete, belief and graph edge
  review, open-loop filing and extraction, the project update candidate, the
  daily brief, the answer of a run that makes a report and the doors that act on
  one row by id apply the caller's limits. A row above the key's limits answers
  as a row that does not exist. The workspace and source trace event feeds,
  saved quotes and the ids an open loop keeps follow the rows they name.
- Redacting a memory restricts the reports and derived rows built from it to the
  owner and an unbound admin key until they are regenerated without it. This is
  containment, not removal.
- A refusal over the MCP tools says what kind it was: `not_permitted`,
  `not_found` or `precondition_failed`. A request that names no domain is held to
  the caller's profile. A key cannot be bound to the project name `~global`.
- `POST /v0/vnext/projects` answers HTTP 409 for a slug in use,
  `POST /v0/vnext/queue/process-next` works on PostgreSQL, and redacting a
  memory made by promoting a report completes.
- SQLite gains `sources list`, `sources delete`, `sources prune --superseded` and
  `import-markdown --supersede`, and a Markdown or ChatGPT import that fails part
  way leaves no half-built source.
- The brief, `alice_resume` and `alice_recent_decisions` leave out an expired
  memory, consolidation and roll-ups neither send its text nor group it, an
  expired roll-up card is not the accepted card for its topic, and artifact
  promotion embeds the memory it makes.
- `alice_memory_commit` refuses a memory over 20,000 characters, and JSON a
  caller hands a write door nests at most 64 levels and holds no number that is
  not finite.
- Still open, listed in the release notes: the `max_tokens` budget prices the
  full stored row, a vector made from a cut text represents only its head,
  removal of redacted text from reports is planned, with no release set, and
  the vector stage of recall does not refill after hiding rows.

## What `v0.20.0` Shipped

`v0.20.0` is an earlier published release.

It shipped the fixes on `main` after `v0.19.2`. Most come from the internal
security review of `v0.19.0`, whose findings DB-005, DB-006, DB-008, DB-009,
DB-010 and DB-011 `v0.19.2` left open. It adds no tool or command and changes
no schema. It adds the `--max-file-mib` option and four settings:
`ALICEBOT_ALLOWED_HOSTS`, `ALICEBOT_MAX_REQUEST_BODY_BYTES`,
`ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES` and `ALICE_EMBEDDINGS_MAX_INPUT_CHARS`.

- [v0.20.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.20.0-release-notes.md)

`v0.20.0` has these changes. `v0.19.2` does not.

- The Postgres HTTP API refuses a request body over 4 MiB with HTTP 413 and a
  body nested more than 256 levels with HTTP 422, and a keyless request whose
  `Host` or `Origin` does not name this machine gets HTTP 401. No provider,
  embeddings, reranker, fact-key, brain, Gmail or Calendar client follows a
  redirect. In `v0.19.2` none of these is limited or checked.
- A request body with a lone surrogate, or one that is not UTF-8 and has no
  JSON content type, gets HTTP 422 where `v0.19.2` answers HTTP 500. Hermes
  provider 0.5.3 replaces a lone surrogate with U+FFFD, so the turn is saved.
  The provider is not in the wheel.
- The local-folder connector reads each file through an open descriptor beneath
  the watched folder, stops at fixed limits and lets one bad file fail alone.
  The canary and archive maintenance workflows hold no write permission in the
  job that installs packages.
- Alice sends the text of a memory to the embeddings endpoint only when recall
  can return it, cuts it to `ALICE_EMBEDDINGS_MAX_INPUT_CHARS` (default 8000),
  and isolates one memory the endpoint refuses. `alice-memory doctor` prints
  `memories without a current vector`. In `v0.19.2` every memory with no current
  vector is sent, whatever its status.
- A context pack skips an item that does not fit and cuts the first item when
  none fits. `alice_resume` and `alice_recent_decisions` refuse a query over
  40,000 UTF-8 bytes with `invalid_request`. Recall and the pack read the
  memories that reference packed sources in one lookup.
- The Markdown and ChatGPT imports, `capture-file`, and the browser clipper and
  agent output file options refuse a file over 16 MiB (512 MiB for a ChatGPT
  export) with `import_file_too_large`. A ChatGPT conversation of any length
  imports, and one that cannot be read is named while the others import.
- `alice-memory import` refuses deeply nested JSON with `restore_failed` and a
  line that names the file line, the table and the column, and `--mode skip`
  accepts a legacy row. In `v0.19.2` the refusal is generic and skip stops.
- Recall and the pack name no id for a retired, expired or looped successor, and
  the pack removes the id of a memory the caller cannot read from `metadata_json`.
- LongMemEval harness 1.1 hides session ids from the reader. The published
  numbers carry a known issue and no number changed.
- Still open, among the known limitations in the release notes: five limits of
  the security fixes (DB-005, DB-006, DB-007, DB-009 and DB-010), the session
  brief, `alice_resume` and `alice_recent_decisions` show an expired active
  memory, consolidation and roll-ups can embed one, artifact promotion leaves a
  memory without a vector until reindex, and the `max_tokens` budget prices the
  full stored row.

## What `v0.19.2` Shipped

`v0.19.2` is an earlier published release.

It shipped the fixes on `main` after `v0.19.0`. Most come from an internal
security review of `v0.19.0`. It adds no tool or command and changes no schema. The
Hermes provider keeps a turn's user and assistant text apart, the `/v1` memory
operations commit applies only a user turn with an explicit prefix, backup
import restores a stored key claim as unverified and lists credential-shaped
text it does not refuse, and the session-start hook no longer goes blank for a
note that contains protocol text. Recall and the context pack leave out memory
ids the caller cannot read, and refuse a query the SQLite source search cannot
take.

It also shipped the fix for the release script that failed the publish run of
the never-published `v0.19.1`: the draft readback, finalize, resume and recovery
jobs run it without the package installed, and it no longer imports the package.

- [v0.19.2 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.19.2-release-notes.md)

`v0.19.2` has these changes. `v0.19.0` does not.

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

`v0.19.0` is an earlier published release.

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
  both exist. The `v0.19.0` tag has no marketplace file. `main` has
  `.claude-plugin/marketplace.json`, named `alicememory`, which pins the plugin
  to the `v0.21.1` tag commit, so the plugin installs from that marketplace.
  The `v0.19.2` tag has the file too, still pinned to the `v0.19.0` tag
  commit, the `v0.20.0` tag has it still pinned to the `v0.19.2` tag commit,
  the `v0.20.2` tag has it still pinned to the `v0.20.0` tag commit, the
  `v0.21.0` tag has it still pinned to the `v0.20.2` tag commit, and the
  `v0.21.1` tag has it still pinned to the `v0.21.0` tag commit.
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
  hints. From `v0.19.2`, `alice_memory_review` is
  read-only, so with the full tool set six tools are read-only and three are
  destructive.
- `alice-memory mcp` refuses an empty or relative `--data-dir` with exit 2,
  and the session-start hook prints one line and exits 0 for a relative
  `--data-dir` or plugin `data_dir`. A relative `ALICE_MEMORY_DATA_DIR` is
  not checked. `v0.18.0` creates the vault under the current directory.
  From `v0.19.2`, the hook refuses a relative
  `ALICE_MEMORY_DATA_DIR` the same way.
- `alice-memory install --host hermes` keeps a comment in the `alice` block
  when nothing needs to change, and refuses when a change is needed. In
  `v0.18.0` the re-run drops the comment.
- Folder-import receipt items name the file, a withheld ChatGPT title is
  counted, `alice_capture` refuses a low-entropy AKIA-shaped key, and both
  imports withhold it. In `v0.18.0` capture stores that key.

## What `v0.18.0` Shipped

`v0.18.0` is an earlier published release.

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
  From `v0.19.2`, it applies only a user turn that
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
  published `v0.12.0` code) committed as per-question evidence. It carries the
  session id label issue described in the Snapshot section.
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
(`alice_memory_commit`, `alice_recall`, `alice_resume`). The tools
outside that handshake need `ALICE_MCP_FULL_TOOLS=1`.

Present-tense recall prefers the current fact. Recall hops once
through provenance. The pack picks a loops / facts / sources view
from the query. `alice-memory sleep` writes up to eight proposals and
does not create a memory.

Import stays a source. Commit stays a fact. Candidates stay
unsearchable as memories. `count=0` after capture is still the
design. 81.2% stays a `v0.12.0` `store_chunks` receipt, with the session id
label issue described in the Snapshot section. `pack_excerpts` is named and
not scored.

## The `v0.15.0` tag was never published

The commit it points at carried a release-gate step that could not run on a CI
runner, so the gate could never pass from that tag, and repository rules
correctly refuse both deletion and update of stable tags. No GitHub Release or
PyPI artifact was ever created for it.
