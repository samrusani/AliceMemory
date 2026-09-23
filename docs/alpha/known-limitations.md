# Public Alpha Known Limitations

This alpha is intentionally limited.

- local setup is still technical
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

SQLite mode (`alice-memory mcp`) is the trial/single-agent path and carries extra boundaries:

- core MCP tools only (11 as of this release); optional long-tail memory tools
  require `ALICE_MCP_LEGACY_TOOLS=1` and remain Postgres-only
- no web console review — review runs through `alice_memory_review` / `alice_memory_correct`
- no scheduler
- agent API keys cannot be created (`alicebot agent keys create` requires Postgres); leave `ALICE_AGENT_API_KEY` unset — agent identity is still honored and audited as `unauthenticated_local`, while a set key fails closed and rejects every write
- one user per local database file
- no automatic migration to Postgres; `alice-memory export` creates a versioned, integrity-checked local backup and `alice-memory import` restores it into another local database (portable ids and timestamps preserved). `--quarantine` removes the credential from the named memory and from the records derived from it, and reports any other copies it finds. The named memory is stored as `rejected`. It is not a PostgreSQL import. A Postgres URL passed as `--db` is refused.
- embedding vectors are not exported: after `alice-memory import`, memories are keyword-searchable (FTS) immediately; configure `ALICE_EMBEDDINGS_*` and run `alice-memory reindex-embeddings` to restore vector search
- import never overwrites existing rows: `--mode skip` accepts an existing id only when every portable field is identical; divergent collisions abort, and `--mode fail` aborts on any collision
- users, agent identities/API keys, embedding vectors, and soft-deleted rows are not portable; fact keys and entity relationship history are included, while nullable references to omitted rows are cleared and graph edges with omitted known endpoints are excluded so the portable set remains foreign-key closed
- exports contain plaintext memory and source content: protect and encrypt copies that leave the managed owner-only local directory

See [Backup and restore](backup-and-restore.md) before upgrading or moving a store.

Do not describe this alpha as hosted SaaS, production-ready, or automatic memory autopilot.
