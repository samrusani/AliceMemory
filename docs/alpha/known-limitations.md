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
- no automatic migration to Postgres; `alice-memory export` creates a versioned, integrity-checked local backup and `alice-memory import` restores it into another local database (portable ids and timestamps preserved). A plain import refuses a backup that holds credential material in a memory row (`import_credential_material`, exit 1, nothing written). `alice-memory export` lists the offenders on stderr and exits 0. Other records come across unchanged. From v0.19.1, import lists each record column that holds credential-shaped text and that it does not refuse on its receipt (every non-memory record, and the memory columns the credential check does not read), and `alice-memory doctor` reads source chunk text. Import restores those records and does not refuse them. The Postgres doctor (`alicebot vnext doctor`) reads source rows and `raw_text` only, not chunk text, and is not changed. `--quarantine` removes the credential from the named memory and from the records derived from it, and reports any other copies it finds. The named memory is stored as `rejected`. It is not a PostgreSQL import. A Postgres URL passed as `--db` is refused on every `alice-memory` subcommand, including `install --dry-run`, with exit 2 and `sqlite_db_path_required`.
- tool failures over stdio return a generic code and no detail: `tool_not_found`, `tool_request_failed`, or `tool_execution_failed`. From v0.19.1, a recall or context pack query the source search cannot take is the one exception. It returns `invalid_request` and a message that names the limit (next item).
- In v0.19.0, `alice_recall` and `alice_context_pack` return a tool error (`tool_execution_failed`, no detail) for a query with about 991 or more distinct search terms, because SQLite refuses the search (`Expression tree is too large`). A search term is an ASCII word of two or more characters that is not a stopword, and a repeated word counts once. That is roughly 30,000 bytes of ordinary prose. The session brief bounds such a string, so a long newest fact, open loop, explicit query or source title no longer empties it. These two tools did not, and v0.18.0 fails the same way. With a captured source in the vault, a query over about 50,000 bytes also returns that error (SQLite `LIKE or GLOB pattern too complex`), in both versions. From v0.19.1, both tools refuse such a query before they search, with `invalid_request` and a message that names the limit, for example `query has 1000 distinct search terms; the limit is 499. Use a shorter query.` The query is never cut to fit. The limits are 499 distinct search terms and 40,000 UTF-8 bytes, counted raw and again after case folding, the same the session brief uses. They sit at about half of what SQLite takes, so a query of 500 to 990 distinct terms, or of 40,001 to about 50,000 bytes, which v0.19.0 took, is refused too. So is a query of any size over 40,000 bytes that v0.19.0 took because the search read no source row, which is a vault with no captured source or filters that exclude every source. A context pack with `include_sources` false or `context_depth` `minimal` runs no source search and still takes a longer query. The limit applies to the SQLite vault only. `alice_resume` and `alice_recent_decisions` are not changed: a query of about 50,000 bytes or more still returns `tool_execution_failed` from `alice_resume` once the vault holds an active memory of any type, and from `alice_recent_decisions` once it holds a stored decision. A query inside the limit can still take several seconds on a vault with thousands of sources: 3.7 seconds at 499 distinct terms, against 0.30 seconds for two words, on a synthetic vault of 4,000 captured sources. Keep the query short.
- in v0.19.0 a relative `ALICE_MEMORY_DATA_DIR` is not checked: the session-start hook still creates a vault under the current directory for it, including the literal text `${HOME}/.alice`. `alice-memory mcp` and the hook refuse a relative `--data-dir`, and the hook refuses a relative Claude Code plugin `data_dir` option, but only the hook reads this variable. Set it to an absolute path. From v0.19.1, the hook refuses a non-empty value of the variable that is not absolute after `~` expansion, with the same one line it prints for `--data-dir`, exits 0, and creates nothing. An empty value is the same as unset.
- embedding vectors are not exported: after `alice-memory import`, memories are keyword-searchable (FTS) immediately; configure `ALICE_EMBEDDINGS_*` and run `alice-memory reindex-embeddings` to restore vector search
- import never overwrites existing rows: `--mode skip` accepts an existing id only when every portable field is identical; divergent collisions abort, and `--mode fail` aborts on any collision
- users, agent identities/API keys, embedding vectors, and soft-deleted rows are not portable; fact keys and entity relationship history are included, while nullable references to omitted rows are cleared and graph edges with omitted known endpoints are excluded so the portable set remains foreign-key closed
- exports contain plaintext memory and source content: protect and encrypt copies that leave the managed owner-only local directory

Open items from the internal security review of v0.19.0. They are not fixed in v0.19.1, and the [v0.19.1 release notes](../release/v0.19.1-release-notes.md) give the detail:

- the Postgres stack's HTTP API parses a JSON request body of any size before it authenticates, and it does not check the `Host` header of a keyless loopback request
- the local-folder scan reads each matching file whole with no size limit, and it can read a file that is swapped for a link between its containment check and its read
- calls to a configured provider, embeddings, reranker or fact-key endpoint use the standard library opener, which follows redirects; the provider helper and the embeddings client were shown to send the `Authorization` header on to the target, and the embeddings endpoint is also used on SQLite
- a memory id copied into a stored memory's `metadata_json` is returned without the read fence by an `alice_context_pack` call with `debug: true`, to the keyless owner under the default sensitivity ceiling and to a read-only key
- confirming a pending write with `alice_memory_commit` is not a human gate: the confirm step relies on the agent asking the user

Also open in v0.19.1, with the detail in the release notes:

- the context pack drops `validity.superseded` together with a `superseded_by` pointer to a row the caller cannot read, where recall keeps it; this needs a row whose status is still active
- a backup column that is itself a JSON text too deep for the decoder fails import with the generic `alice_memory_failed`, not `restore_failed`, and nothing is written
- the server answers a `POST /v0/continuity/captures/candidates` body that carries a lone surrogate with HTTP 500, so a Hermes turn that carries one is not saved
- the Claude Code marketplace file pins the v0.19.0 tag commit until a change after this release moves it, so the marketplace install runs v0.19.0 code until then

See [Backup and restore](backup-and-restore.md) before upgrading or moving a store.

Do not describe this alpha as hosted SaaS, production-ready, or automatic memory autopilot.
