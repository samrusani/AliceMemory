# Public Alpha Known Limitations

This alpha is intentionally limited.

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
  `ALICE_MEMORY_PERSONA`, and is only ever available to a writer whose
  identity was established by an issued agent key; a compromised key can
  write durable memory, and `alicebot vnext memories quarantine` is the
  command-line-only sweep for that case
- passive memory capture is structured and English-biased; general conversation is not guaranteed to become memory
- `/vnext` is the operator console, not the main agent interface
- after any active agent key exists, the full `/vnext` console requires a dedicated unbound `admin_agent` key entered again for each mounted browser session; `trusted_local_agent` is not full admin-review parity
- generic thread, approval, task, and trace histories are client-bounded, but their list endpoints do not yet provide cursor pagination
- team accounts, billing, cloud sync, mobile app, and hosted deployment are out of scope

SQLite mode (`alice-memory install`, `alice-memory mcp`) is the default single-user path and carries extra boundaries:

- the default three MCP tools, or all eleven core tools with
  `ALICE_MCP_FULL_TOOLS=1`; optional long-tail memory tools need
  `ALICE_MCP_LEGACY_TOOLS=1` and Postgres
- no web console review — review runs through `alice_memory_review` / `alice_memory_correct`
- no scheduler
- agent API keys cannot be created (`alicebot agent keys create` requires Postgres); leave `ALICE_AGENT_API_KEY` unset — agent identity is still honored and audited as `unauthenticated_local`, while a set key fails closed and rejects every write
- one user per local database file
- no automatic migration to Postgres; `alice-memory export` creates a versioned, integrity-checked local backup and `alice-memory import` restores it into another local database (portable ids and timestamps preserved). A plain import refuses a backup that holds credential material in a memory row (`import_credential_material`, exit 1, nothing written). `alice-memory export` lists the offenders on stderr and exits 0. Other records come across unchanged. Unreleased (on main, not in v0.19.0): import lists each non-memory record that holds credential-shaped text on its receipt, and `alice-memory doctor` reads source chunk text. Import restores those records and does not refuse them. `--quarantine` removes the credential from the named memory and from the records derived from it, and reports any other copies it finds. The named memory is stored as `rejected`. It is not a PostgreSQL import. A Postgres URL passed as `--db` is refused on every `alice-memory` subcommand, including `install --dry-run`, with exit 2 and `sqlite_db_path_required`.
- tool failures over stdio return a generic code and no detail: `tool_not_found`, `tool_request_failed`, or `tool_execution_failed`. Unreleased (on main, not in v0.19.0): a recall or context pack query the source search cannot take is the one exception. It returns `invalid_request` and a message that names the limit (next item).
- In v0.19.0, `alice_recall` and `alice_context_pack` return a tool error (`tool_execution_failed`, no detail) for a query with about 991 or more distinct search terms, because SQLite refuses the search (`Expression tree is too large`). A search term is an ASCII word of two or more characters that is not a stopword, and a repeated word counts once. That is roughly 30,000 bytes of ordinary prose. The session brief bounds such a string, so a long newest fact, open loop, explicit query or source title no longer empties it. These two tools did not, and v0.18.0 fails the same way. With a captured source in the vault, a query over about 50,000 bytes also returns that error (SQLite `LIKE or GLOB pattern too complex`), in both versions. Unreleased (on main, not in v0.19.0): both tools refuse such a query before they search, with `invalid_request` and a message that names the limit, for example `query has 1000 distinct search terms; the limit is 499. Use a shorter query.` The query is never cut to fit. The limits are 499 distinct search terms and 40,000 UTF-8 bytes, counted raw and again after case folding, the same the session brief uses. They sit at about half of what SQLite takes, so a query of 500 to 990 distinct terms, or of 40,001 to about 50,000 bytes, which v0.19.0 took, is refused too. So is a query of any size over 40,000 bytes that v0.19.0 took because the search read no source row, which is a vault with no captured source or filters that exclude every source. A context pack with `include_sources` false or `context_depth` `minimal` runs no source search and still takes a longer query. The limit applies to the SQLite vault only. `alice_resume` and `alice_recent_decisions` are not changed: a query of about 50,000 bytes or more still returns `tool_execution_failed` there once the vault holds a stored decision. Keep the query short.
- a relative `ALICE_MEMORY_DATA_DIR` is not checked: the session-start hook still creates a vault under the current directory for it, including the literal text `${HOME}/.alice`. `alice-memory mcp` and the hook refuse a relative `--data-dir`, and the hook refuses a relative Claude Code plugin `data_dir` option, but only the hook reads this variable. Set it to an absolute path.
- embedding vectors are not exported: after `alice-memory import`, memories are keyword-searchable (FTS) immediately; configure `ALICE_EMBEDDINGS_*` and run `alice-memory reindex-embeddings` to restore vector search
- import never overwrites existing rows: `--mode skip` accepts an existing id only when every portable field is identical; divergent collisions abort, and `--mode fail` aborts on any collision
- users, agent identities/API keys, embedding vectors, and soft-deleted rows are not portable; fact keys and entity relationship history are included, while nullable references to omitted rows are cleared and graph edges with omitted known endpoints are excluded so the portable set remains foreign-key closed
- exports contain plaintext memory and source content: protect and encrypt copies that leave the managed owner-only local directory

See [Backup and restore](backup-and-restore.md) before upgrading or moving a store.

Do not describe this alpha as hosted SaaS, production-ready, or automatic memory autopilot.
