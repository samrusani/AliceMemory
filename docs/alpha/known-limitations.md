# Public Alpha Known Limitations

This alpha is intentionally limited. This page lists what is limited now, in short, with a link to the full explanation where there is one. What an earlier release did, and what a later release fixed, is in the [CHANGELOG](../../CHANGELOG.md) and the [release notes](../release/v0.20.0-release-notes.md). Behaviour that is on main and not in v0.20.0 is marked `Unreleased (on main, not in v0.20.0):`.

- the Postgres stack setup is still technical; the SQLite install is one command
- no hosted cloud
- no production SLA
- no managed Gmail OAuth consent/account-linking flow; manual operator-token
  storage exists
- no managed Calendar OAuth consent/account-linking flow; manual operator-token
  storage exists
- no live email polling
- no live calendar polling
- no Telegram polling, delivery, or channel transport
- OCR execution is not packaged
- PDF OCR is not packaged
- voice transcription execution is not packaged
- browser clipper remains a bookmarklet/MVP path: the trusted console must issue a new short-lived, origin-bound, one-time bookmarklet for each clip, and the user must verify opaque submissions in the Inbox
- scheduler is local
- model providers require user configuration
- secrets fallback is alpha-grade unless OS or managed secret provider is configured
- automatic memory promotion is off unless a deployment opts in with
  `ALICE_MEMORY_PERSONA` (see
  [memory promotion personas](../memory/promotion-personas.md)), and is only
  ever available to a writer whose identity was established by an issued agent
  key; a compromised key can write durable memory, and
  `alicebot vnext memories quarantine` is the command-line-only sweep for that
  case
- passive memory capture is structured and English-biased; general conversation is not guaranteed to become memory
- the Markdown and ChatGPT imports read each file whole into memory (a ChatGPT export takes about 6 to 9 times its size), so a file over 16 MiB (Markdown) or 512 MiB (ChatGPT export) is refused before it is read, with `import_file_too_large`, and `--max-file-mib N` raises the limit; a folder is not limited as a whole. `alicebot vnext sources capture-file`, `alicebot vnext connectors browser-clipper capture --file` and `alicebot vnext agents ingest-output --file` take the same 16 MiB limit and `--max-file-mib N`. See [docs/integrations/importers.md](../integrations/importers.md)
- `/vnext` is the operator console, not the main agent interface
- after any active agent key exists, the full `/vnext` console requires a dedicated unbound `admin_agent` key entered again for each mounted browser session; `trusted_local_agent` is not full admin-review parity
- generic thread, approval, task, and trace histories are client-bounded, but their list endpoints do not yet provide cursor pagination
- team accounts, billing, cloud sync, mobile app, and hosted deployment are out of scope

SQLite mode (`alice-memory install`, `alice-memory mcp`) is the default single-user path and carries extra boundaries:

- the default three MCP tools, or all eleven core tools with
  `ALICE_MCP_FULL_TOOLS=1`; optional long-tail memory tools need
  `ALICE_MCP_LEGACY_TOOLS=1`, and most of them fail or refuse on SQLite: the
  ones that read the continuity store (for example `alice_brief` and
  `alice_timeline`) and every `alice_vnext_*` tool except the thirteen that
  run, which are the memory-commit family and four reads and captures (the
  list that runs is under
  [Legacy tool surface](mcp-tools.md#legacy-tool-surface))
- no web console review: review runs through `alice_memory_review` / `alice_memory_correct`
- no scheduler
- agent API keys cannot be created (`alicebot agent keys create` requires Postgres); leave `ALICE_AGENT_API_KEY` unset; agent identity is still honored and audited as `unauthenticated_local`, while a set key fails closed and refuses every core tool call, reads included
- one user per local database file
- no automatic migration to Postgres: `alice-memory export` creates a versioned, integrity-checked local backup and `alice-memory import` restores it into another local database, with portable ids and timestamps preserved. A plain import refuses a backup that holds credential material in a memory row (`import_credential_material`, exit 1, nothing written), and a Postgres URL passed as `--db` is refused on every `alice-memory` subcommand, including `install --dry-run`, with exit 2 and `sqlite_db_path_required`. See [Backup and restore](backup-and-restore.md) for what import lists, restores and refuses
- tool failures over stdio return a generic code and no detail: `tool_not_found`, `tool_request_failed`, or `tool_execution_failed`, except a query over a size limit, which returns `invalid_request` and names the limit. Unreleased (on main, not in v0.20.0): three more codes, `not_permitted`, `not_found` and `precondition_failed`, tell a refusal from a failure, and a rejected argument answers `invalid_request` (see the error codes table in [docs/alpha/mcp-tools.md](mcp-tools.md#error-codes))
- `alice_recall` and `alice_context_pack` refuse a query of more than 499 distinct search terms or 40,000 UTF-8 bytes, counted raw and again after case folding, and `alice_resume` and `alice_recent_decisions` refuse one over 40,000 UTF-8 bytes, each with `invalid_request` and a message that names the limit; the query is never cut to fit, and the limit applies to the SQLite vault only. A query inside the limit can still take several seconds on a vault with thousands of sources, so keep the query short. See [Size bounds](mcp-tools.md#size-bounds)
- embedding vectors are not exported: after `alice-memory import`, memories are keyword-searchable (FTS) immediately; configure `ALICE_EMBEDDINGS_*` and run `alice-memory reindex-embeddings` to restore vector search
- import never overwrites existing rows: `--mode skip` accepts an existing id only when every portable field is identical; divergent collisions abort, and `--mode fail` aborts on any collision. A column the file row does not carry is not compared, and a column the file gives with a value still is, so a row that really differs is refused
- users, agent identities/API keys, embedding vectors, and soft-deleted rows are not portable; fact keys and entity relationship history are included, while nullable references to omitted rows are cleared and graph edges with omitted known endpoints are excluded so the portable set remains foreign-key closed
- exports contain plaintext memory and source content: protect and encrypt copies that leave the managed owner-only local directory

Limits that remain in the Postgres stack and its outbound calls, with the detail in the [threat model](../security/threat-model.md) and the [v0.20.0 release notes](../release/v0.20.0-release-notes.md):

- the HTTP API refuses a request body over 4 MiB (32 MiB for the connector sync routes) with HTTP 413, a JSON body nested more than 256 levels deep with HTTP 422 (DB-006), and a keyless request whose `Host` or `Origin` is not this machine's (DB-005). The cap is no rate limit, a request with an agent key is not checked for `Host` and `Origin`, the legacy `/v0` routes apply the rule to every request, and the rule was checked with raw requests and in process, not from a real browser; a name listed in `ALICEBOT_ALLOWED_HOSTS` is trusted as this machine (see the [threat model](../security/threat-model.md))
- the local-folder scan opens the watched folder, each directory below it and the file one at a time, each relative to the one before and without following a link, so a file or directory swapped for a link is skipped and counted in `refused_count`. It reads at most 2 MiB of a file, stops at 10,000 files or 64 MiB in all and lists at most 100,000 directory entries, and sets `truncated` when a limit stopped it, but it still reads a hard link planted inside the watched folder to a file elsewhere, as the importers do (see the [threat model](../security/threat-model.md) and the [v0.20.0 release notes](../release/v0.20.0-release-notes.md))
- no call to a configured provider, embeddings, reranker or fact-key endpoint follows a redirect (a provider that redirects must be configured with its final URL), and the provider helpers, Gmail and Calendar dial only an address the outbound policy allows (DB-009). The embeddings, reranker, fact-key and brain clients can still reach a loopback or private address, a request carried by a proxy is not held to the address rule, and a provider response is read whole
- the Postgres doctor (`alicebot vnext doctor`) reads source rows and `raw_text` only, not chunk text, so a credential that sits only in a source chunk is not found there, where `alice-memory doctor` on SQLite reads chunk text (see [Backup and restore](backup-and-restore.md))
- `alice_context_pack` removes the id of a memory the caller cannot read from the `metadata_json` of the memories it returns, in every section that holds them. Only a 36-character UUID in `metadata_json` is looked for: an id in another column of a stored row or in another spelling is not
- confirming a pending write with `alice_memory_commit` is not a human gate: the confirm step relies on the agent asking the user. Who may confirm or reject, and what a refused caller gets, is in [Confirm and reject rules](../memory-operations-protocol.md#confirm-and-reject-rules)

Open in v0.20.0, with the detail in the [v0.20.0 release notes](../release/v0.20.0-release-notes.md):

- the session brief, `alice_resume` and `alice_recent_decisions` list memories by status and do not check `valid_to`, so they still show an active memory that `alice_memory_manage` with `action: expire` closed, while recall and the context pack do not return it, as in v0.19.2. Unreleased (on main, not in v0.20.0): all three, and the SessionStart hook that injects the brief, leave out a memory whose `valid_to` has passed, with the test recall uses and before any limit; `alice_context_pack` is not changed: its `recent_changes` still names the id of an expired memory through its creation and expire events
- consolidation and the roll-up semantic tier embed, for clustering, memories that already hold a vector and do not check `valid_to`, so they can send the text of an expired memory to the embeddings endpoint; the roll-up pass also reads accepted roll-up cards (`list_accepted_rollup_cards`) without checking `valid_to`. Unreleased (on main, not in v0.20.0): both read only memories and cards whose `valid_to` has not passed, and a group whose only card expired is reported as `expired_card_members_unchanged` until its members change; text that v0.20.0 and earlier sent cannot be taken back from the endpoint
- promoting a reviewed artifact into a memory does not embed the new memory; it has no vector until the next `alice-memory reindex-embeddings` or `alicebot vnext memories backfill-embeddings`. Unreleased (on main, not in v0.20.0): promotion embeds the new memory after the commit, once, through the same door as every other write, and a promotion that conflicts with another reviewer sends nothing
- the `max_tokens` budget of the context pack prices the full stored row, and the agent receives a compact row, so a budget leaves room unused and can return nothing for a memory that would fit in compact form
- a vault that already holds JSON text nested about 1,000 levels or more (a v0.19.0 or v0.19.2 import could store it) cannot be exported: `alice-memory export` ends with `export_failed` after one line that names the table and the column, for example `alice-memory: memory_revisions column previous_value is nested too deeply for export to write`. Import refuses such text with `restore_failed`, see [Backup and restore](backup-and-restore.md)
- the Postgres HTTP commit routes take up to 20,000 characters of memory text, and `alice_memory_commit` on SQLite has no length limit (a 2,000,000-character memory was stored whole). Unreleased (on main, not in v0.20.0): `alice_memory_commit` refuses a memory whose text is over 20,000 characters, on SQLite and on any other store the tool runs on, with `invalid_request` and nothing saved; the legacy tools `alice_vnext_correct_memory` and `alice_vnext_propose_memory` (off unless `ALICE_MCP_LEGACY_TOOLS=1`) still take text of any length on SQLite
- in v0.20.0 a key bound to one project can attach a source it cannot read through `alice_memory_commit` `source_refs`, the `provenance` of `alice_memory_correct` and, over HTTP on Postgres, `POST /v0/vnext/open-loops`, and a saved quote stays readable after its source is reclassified. Unreleased (on main, not in v0.20.0): those doors answer `not_found` (404 over HTTP) for a source or memory the caller may not read and the readers of a saved quote and of an open loop ask the reader's own fence again, but a link that passes the fence of an `admin_agent` writer still makes `alice_explain` of that memory fail for the keys of a project with a lower ceiling (see [Cited sources](mcp-tools.md#cited-sources) and [Saved quotes](mcp-tools.md#saved-quotes))
- the cited-source fence does not cover everything yet. Unreleased (on main, not in v0.20.0): memory proposals and the agent-output ingest store their `source_refs` as given, the owner dependency trace lists a memory by the first id of a multi-id ref only, `provenance_count` still counts a withheld link, and a memory or open loop saved before the fix keeps its link or id, so `alice_explain` still fails for such a memory; the rest is under [Cited sources](mcp-tools.md#cited-sources), [Saved quotes](mcp-tools.md#saved-quotes) and in the [CHANGELOG](../../CHANGELOG.md)

What v0.19.0 and v0.19.2 limited and v0.20.0 fixed or narrowed (request body size, the `Host` check, provider redirects, local-folder reads, the memory id fence, deep backup JSON, the lone surrogate turn, the query size bounds and the data directory variable) is recorded in the [v0.19.2 release notes](../release/v0.19.2-release-notes.md), the [v0.20.0 release notes](../release/v0.20.0-release-notes.md) and the [CHANGELOG](../../CHANGELOG.md).

See [Backup and restore](backup-and-restore.md) before upgrading or moving a store.

## Derived rows retain restricted input domains

Unreleased (on main, not in v0.20.0): a derived memory or report with restricted-domain inputs keeps the most frequent restricted input label, with alphabetical ties. An explicit request domain cannot override it. With no restricted inputs, each producer retains its prior selection. This covers briefs, weekly synthesis and its candidates, roll-ups, consolidation, connection and contradiction reports, staleness reports, open-loop reviews and project updates. Consolidation reports include their roll-up inputs. New staleness reports also inherit the highest sensitivity of the memories whose titles they include.

Unreleased (on main, not in v0.20.0): the stored-row repair resolves recorded input IDs within the same user and revisits downstream rows until their labels settle, including promoted artifact copies identified by `value.kind`, `value.artifact_id` or `metadata_json.source_artifact_id`. Already restricted labels can change to the settled restricted label; they never become unrestricted. A bounded nonsettling graph aborts the upgrade or restore instead of publishing intermediate labels. Each changed memory or artifact gets one audit event with its id and old/new domains. Redacted rows and rows without resolvable recorded inputs are left alone; text is never used to guess an input.

Unreleased (on main, not in v0.20.0): SQLite repairs stored memories on upgrade and repairs the complete staged graph during `alice-memory import`, before publication, for fresh and already-upgraded destinations. Repeating `--mode skip` recognizes a logged domain repair while still refusing other differing fields. The native SQLite schema has no generated-artifact table and its portable backup carries no artifacts, so a promoted copy whose only input is a missing artifact cannot be repaired from that reference. The shared repair follows artifact rows when that table is present. PostgreSQL migration `20261004_0095` repairs both memories and artifacts, including promoted copies and alternate UUID spellings. Run it before serving requests; it transactionally relaxes and restores FORCE RLS for the documented table-owning NOSUPERUSER NOBYPASSRLS role. Downgrade retains repaired labels.

Unreleased (on main, not in v0.20.0): owner, trusted and admin callers that explicitly filter by an unrelated domain stop receiving a derived row after its label changes from `unknown` to a restricted domain; `unknown` previously matched every domain filter. New weekly candidate memories also store `input_summary`, which explain returns. These are intentional differences for unrestricted readers. Domain fields and explain policy-event labels/hashes reflect the new label; additive metadata changes context-pack token estimates. Repair audit events are additional history. The repair does not add missing historical input summaries or repair historical sensitivity values.

Unreleased (on main, not in v0.20.0): project scope is unchanged. Roll-ups group by exact normalized scope, but brain reports and weekly candidates can combine scopes; a reader matching one scope can potentially read a summary of other scopes. This domain repair does not resolve that separate scope question. In v0.20.0, mixed inputs could produce `unknown` or a less restricted request/project label; the repair covers only copies with recorded inputs that still resolve.

Do not describe this alpha as hosted SaaS, production-ready, or automatic memory autopilot.
