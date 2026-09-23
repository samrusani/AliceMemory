# Agent Integration Pack

Agents should use Alice as a durable, private, provenance-aware, reviewable memory layer.

## Connect your agent in 5 minutes

1. Point your MCP-capable agent at the Alice MCP server:

```json
{
  "mcpServers": {
    "alice": {
      "command": "/ABSOLUTE/PATH/TO/AliceMemory/.venv/bin/python",
      "args": ["-m", "alicebot_api.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/TO/AliceMemory",
      "env": {
        "DATABASE_URL": "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot",
        "ALICEBOT_AUTH_USER_ID": "00000000-0000-0000-0000-000000000001"
      }
    }
  }
}
```

   No Postgres yet? `alice-memory mcp --data-dir ~/.alice` serves the same
   default three tools against a local SQLite file. No `DATABASE_URL` needed.

2. (Recommended on Postgres) Bind the server to an agent identity: create
   a key with `alicebot agent keys create` and set `ALICE_AGENT_API_KEY`
   in the MCP server env (details under [Authentication](#authentication)).
   Key creation requires Postgres — in SQLite on-ramp mode, skip this step
   and leave `ALICE_AGENT_API_KEY` unset; payload identity is honored and
   audited as `unauthenticated_local`.
3. Drop one of the skill blocks into your agent's instructions:
   [hermes-skill.md](hermes-skill.md) for personal assistants,
   [openclaw-skill.md](openclaw-skill.md) for coding agents.
4. Ask the agent something it should need memory for. Its write verb is
   `alice_memory_commit`. Its read verbs are `alice_recall` and
   `alice_resume`.

## The default loop

Remember, recall, continue:

1. **`alice_memory_commit`** — record a fact as durable, immediately
   recallable memory, including when the user has not asked.
2. **`alice_recall`** — search memory and imported sources.
3. **`alice_resume`** — pick work back up: last decision, next action,
   open loops, recent changes.

`alice_capture` and `alice_context_pack` are on the full surface
(`ALICE_MCP_FULL_TOOLS=1`). Capture stores a source; its passages come
back from `alice_recall` under `sources`, as material to read and quote
rather than as facts Alice asserts. Candidates stay unsearchable until a
reviewer promotes them. Import is a source. Commit is a fact. Print the
`receipt` field after a capture or commit so the user sees what was stored.
Do not tell the user they must clear a review queue before a note is usable.

Lifecycle tools (`alice_memory_manage`, review, correct) are also
full-surface. Finishing a `confirmation_required` write is not: it happens
on `alice_memory_commit` itself (see
[Explicit Memory Commits](#explicit-memory-commits)).

Respect domain and sensitivity policy on every call, and use `/vnext` for
review, audit, undo, correction, forget, and troubleshooting.

### Which context depth to request

`alice_context_pack` is full-surface (`ALICE_MCP_FULL_TOOLS=1`). The
context-pack request accepts `context_depth` (default `low`). Every
tier is deterministic retrieval and packing — no tier performs LLM
synthesis or summarization. Pick the cheapest tier that answers the
question class:

| `context_depth` | Question class | What runs |
| --- | --- | --- |
| `minimal` | Single-fact lookups, quick pre-flight checks, "do I know X at all?" | Full-text stage only (no vector, no graph hop), at most 4 memories, no sources, no contradictions, no typed sections, no recent changes. The cheapest useful call. |
| `low` (default) | Normal task context before acting | Hybrid full-text + vector + entity-graph retrieval, sources, open loops, supporting evidence; contradiction check only for strategic query shapes (status, synthesis, contradiction, agent-context queries). |
| `medium` | Reviews, plans, status reports, anything you will assert to the user | Everything `low` does, plus the contradiction check forced on for every query type. That contradictions default is the only difference between `low` and `medium`. |
| `high` | Audits, conflicting-history questions, resuming long-running work | Everything `medium` does, plus compact supersession chain notes (`supersession_context`) for packed memories that supersede or are superseded by other revisions, and the resolved `entities` list whenever the query matched entities. |

Explicit `include_sources` / `include_contradictions` flags always override
the tier default — the caller wins (e.g. `minimal` plus
`include_sources: true` returns sources). The tier is echoed back as
`context_depth` on the pack and in the retrieval trace, and skipped stages
report honest statuses such as `disabled: context_depth=minimal`.

### Budget strategies and the allocation report

When the request sets `max_tokens`, a greedy packer drops lowest-priority
items to fit. `budget_strategy` (default `balanced`) controls the packing
order — never what was retrieved or ranked:

| `budget_strategy` | Packs first | Reach for it when |
| --- | --- | --- |
| `balanced` (default) | memories, then open loops, sources, evidence quotes, contradictions | General use. |
| `facts_first` | Same section order; `semantic`/`decision`/`preference` memories boosted to the front of the memories list | Durable facts and decisions matter more than narrative under a tight budget. |
| `recent_first` | Same section order; memories ordered newest-first before fused rank | "What changed" and freshness-sensitive tasks. |
| `contradictions_first` | Contradiction records before everything else | Verification and consistency checks — contradictions survive even when the memories themselves get dropped. |
| `sources_first` | Sources before memories | Citation-heavy, evidence-first workflows. |

The pack's `budget` report shows where the content budget went and also
reports `serialized_token_estimate`. `allocation` covers memories, loops,
sources, evidence, contradictions, recent changes, supersession context,
entities, grounding, derived values, and item annotations; the report's
`excluded_sections` names diagnostic and duplicate navigation views outside
the unique-content budget. `max_tokens` is therefore not a transport cap.

`context_depth` and `budget_strategy` are fields on the context-pack
request across the service surfaces; the matching `alice_context_pack` MCP
tool arguments land in the same release — check the server's `tools/list`
response (the source of truth for input schemas) before passing them, and
keep tool payloads generic otherwise.

The compact MCP result reports `serialized_token_estimate` for that exact
compact tool payload. When the compiler also supplied complete-envelope
accounting, the MCP result preserves it separately as
`full_pack_serialized_token_estimate` (and
`full_pack_excluded_token_estimate`) rather than presenting it as the size of
the smaller response. Temporal `derived_values` are retained because source
event times can contribute to them and compact memory rows alone cannot
reconstruct those values.

### Retrieval configuration (env)

Two optional provider seams upgrade retrieval quality when configured in
the server environment (MCP server env, HTTP server env, or shell for the
CLI). Both are OFF by default, degrade honestly, and disclose themselves in
the pack's retrieval trace; unset means the corresponding stage does not
run at all and makes zero network calls.

| Env vars | Stage | When unset | When set |
| --- | --- | --- | --- |
| `ALICE_EMBEDDINGS_BASE_URL`, `ALICE_EMBEDDINGS_MODEL`, optional `ALICE_EMBEDDINGS_API_KEY` | Vector search (hybrid recall) | Full-text-only retrieval; trace `vector_stage` says `disabled: no embedding provider configured`. | Query and memory embeddings via any OpenAI-compatible `/embeddings` endpoint (Ollama, LM Studio, vLLM, OpenAI); vector results join rank fusion. |
| `ALICE_RERANKER_BASE_URL`, `ALICE_RERANKER_MODEL`, optional `ALICE_RERANKER_API_KEY` | Rerank (precision) | Dormant: fused order stands, zero provider calls, no `reranker` trace stage — packs are byte-identical to the fusion-only path. | Provider-side listwise relevance scoring of the fused candidate head (up to 48 memories + 24 sources per pack) via any OpenAI-compatible `/chat/completions` endpoint, reordering candidates before slots and token budget are spent. |

Vectors are stamped with provider, model, content digest, and signature
version. After upgrading from an unsigned-vector release or changing models,
run `alicebot vnext memories backfill-embeddings` for PostgreSQL or
`alice-memory reindex-embeddings` for the local SQLite on-ramp.

Reranker guarantees, in the order they matter:

- **Reorders, never shrinks.** The same number of memory/source slots is
  filled after reranking; `max_items` and the token-budget packer still
  decide what survives. Domain/sensitivity-excluded items are never
  re-admitted regardless of score.
- **Fail-open.** If the scoring endpoint errors or returns garbage, the
  pack keeps the fused order and the trace records the failure
  (`fail_open: …`) — retrieval never breaks because a reranker is down.
- **Disclosed.** When configured, the trace carries
  `stages.reranker` with the provider, model, prompt sha, candidates
  scored, whether anything actually reordered, latency, and token usage.
  The scoring prompt is a frozen, generic relevance prompt (no query-type
  or benchmark vocabulary), sha-pinned in the test suite.
- **`minimal` depth skips it** (honest `disabled: context_depth=minimal`
  status) to keep the cheapest-useful-call promise; equal scores resolve
  through the same content-stable tie-break as fusion.

The full verb contract — remember, recall, correct, confirm, undo, forget,
audit — with outcomes and audit guarantees per verb is documented in the
[Memory Operations Protocol](../memory-operations-protocol.md).

## Agent Identity Fields

```json
{
  "agent_id": "openclaw",
  "agent_type": "coding_agent",
  "agent_run_id": "run-2026-05-12-001",
  "task_id": "alice-public-alpha",
  "project_scope": ["Alice"],
  "permission_profile": "project_scoped_agent"
}
```

Permission profiles:

- `read_only_agent`: context lookup only
- `project_scoped_agent`: project context, project outputs, explicit project-domain memory commits, and review-only proposals
- `trusted_local_agent`: broader local assistant context, still policy-filtered
- `memory_proposal_agent`: proposal-focused agent
- `admin_agent`: scheduler and administrative actions

## Authentication

Custom agents calling the HTTP API authenticate with per-agent API keys. Create one per agent:

```bash
alicebot agent keys create --agent-id openclaw --profile project_scoped_agent --label "OpenClaw laptop"
```

The raw key (`alice_sk_...`) is printed exactly once; only its sha256 hash is stored. Put it in an owner-only temporary curl config for HTTP calls. Curl receives only the config path in its process arguments; never paste raw keys into scripts or docs:

```bash
ALICE_AGENT_API_KEY="<paste the key printed by 'agent keys create'>"
agent_curl_config="$(mktemp "${TMPDIR:-/tmp}/alice-agent-curl.XXXXXX")"
chmod 600 "$agent_curl_config"
trap 'rm -f "$agent_curl_config"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "$ALICE_AGENT_API_KEY" >"$agent_curl_config"

curl --config "$agent_curl_config" \
  -X POST http://127.0.0.1:8000/v0/vnext/memories/commit \
  -H "Content-Type: application/json" \
  -d '{"user_id": "00000000-0000-0000-0000-000000000001", "title": "Preferred planning format", "canonical_text": "The user prefers concise daily planning summaries."}'
```

The body field is `canonical_text` — the same field name as the
`alice_memory_commit` MCP schema.

Rules:

- With a valid key, `agent_id` and `permission_profile` come from the key record, not the payload. A payload may claim a lower profile (downgrade), but claiming a different `agent_id` or a higher profile is rejected with `403` and logged as `agent.key_escalation_rejected`.
- Fresh local installs keep working without keys: while a user has no active keys, keyless local human and agent calls remain available; self-asserted agents are audited with `auth: "unauthenticated_local"`.
- The moment at least one active key exists, every protected `/v0/vnext` request requires `Authorization: Bearer alice_sk_...`, including requests that omit agent identity fields. Bare keys are rejected.
- Manage keys with `alicebot agent keys list` (prefixes only, never hashes) and `alicebot agent keys revoke <key-prefix-or-id>`.
- MCP servers bind a key by setting `ALICE_AGENT_API_KEY` in the server env. Key-bound MCP runs fail closed to the enabled core set (three by default, eleven with `ALICE_MCP_FULL_TOOLS=1`): legacy tools are neither listed nor callable, even if `ALICE_MCP_LEGACY_TOOLS=1` is also set.

### HTTP error contract

Exceptions handled by the HTTP API use a stable structured envelope and keep
diagnostic details in server logs:

```json
{
  "detail": {
    "code": "invalid_request",
    "message": "The request is invalid"
  }
}
```

Clients should branch on `detail.code`, not on the human-readable message.
The public families are `authentication_failed`, `forbidden`,
`invalid_request`, `not_found`, `conflict`, `upstream_failure`, and
`internal_error`. Deliberate static route errors and FastAPI validation errors
retain their documented string or array `detail` variants; all variants remain
under the same top-level `detail` key and are described by the OpenAPI schema.

## Scopes

Memories carry four scopes. `user_id` is the hard tenancy boundary (RLS);
the rest are first-class columns filled by the agentic write path and
filterable on the read path:

- **`project_id`** — set on commit when the effective project scope is
  singular (from the request's `project_scope`, falling back to the
  identity's). `alice_recall` and `alice_context_pack` filter on it via
  `projects`; the read path also falls back to legacy
  `metadata_json.project_id` during the transition.
- **`created_by_agent_id`** — the *authenticated* agent that wrote the
  memory. Filter with the optional `created_by_agents` array on
  `alice_recall` / `alice_context_pack` (e.g. `["openclaw"]`).
- **`run_id`** — the writing agent's `agent_run_id`. Deliberately
  metadata-plus-filter only: there is no session entity or foreign key
  behind it, so runs cost nothing to mint and old runs need no cleanup.
  Store-level searches accept an optional `run_id` filter.

Project binding closes the trust gap between a key and the payload's
self-asserted `project_scope`:

```bash
alicebot agent keys create --agent-id openclaw --profile project_scoped_agent \
  --project-scope Alice
```

When a key carries `--project-scope`, the resolved identity's project scope
comes from the key record. Omitting a project filter inherits the complete
binding; an explicit filter may narrow to a subset but never widen it.
Lifecycle operations authorize the persisted target's project rather than
trusting a project supplied by the caller. On the core MCP surface, recall,
context packs, explanation expansion, open-loop listing, and resume lookup
enforce the effective key-bound project set; target-specific event reads
follow only after the backing row is admitted. The legacy
`alice_vnext_context_tree` tool is not part of that core surface and remains
disabled on key-bound servers. When legacy tools are enabled for a keyless
local server, its five resource groups (projects, memories, sources, open
loops, and artifacts) plus its separate event group use the caller's effective
project set. Violations are blocked and audited with
`project_scope_binding_violation`. Keys issued without `--project-scope` keep
the prior behavior: the payload's explicit project scope is honored.

`read_only_agent` cannot write. `memory_proposal_agent` can submit a
review-only proposal but cannot approve, correct, forget, redact, or otherwise
mutate an existing resource.

## Explicit Memory Commits

When the user says "remember this", commit it through `alice_memory_commit`
on the core MCP surface (or `POST /v0/vnext/memories/commit` over HTTP,
`alicebot vnext memories commit` on the CLI). Use the same verb when the
agent learns something worth keeping and the user has not asked: an explicit
instruction is one reason to commit, not a precondition. The commit is
policy-checked
and returns one of four outcomes: `committed`, `confirmation_required`,
`review_required`, or `rejected`. It is never a silent write.

A `confirmation_required` write is not stored yet. The agent asks the user,
then calls `alice_memory_commit` again with only the returned
`confirmation_id` and `confirmation_action` (`confirm` or `reject`), plus its
identity fields and an optional `rationale`. That works on the default three
tools. It runs the same service call as `alice_memory_manage` action
`confirm`, with the same policy check, project fence and audit trail. The
project fence binds a key-bound scope; a keyless server trusts whatever
`project_scope` the caller declares. It also refuses to let an agent
confirm a pending write above that agent's sensitivity ceiling; the agent
may still reject it. Alice cannot tell whether the user was asked. The
revision, the policy events and the `agent.memory_confirmed` or
`agent.memory_confirmation_rejected` event name the key's `agent_id` when
`ALICE_AGENT_API_KEY` is set, and the declared, unverified `agent_id` on a
keyless server; the `memory.updated` and `memory_revision.created` events
carry no `actor_id`. A keyless call without an `agent_id` names no agent
on any row (`actor_type: user`). Other follow-up lifecycle verbs (`undo`,
`forget`) live on `alice_memory_manage`, which is full-surface.

Identity requirements:

- Direct human calls (Claude Desktop, an IDE, the CLI as yourself) need no
  identity fields — omit them and the commit runs as the local operator
  (`permission_profile: user_or_system`). No agent profile gates apply,
  but the shared safety checks (secret screening, explicit intent, source
  review, confidence thresholds) still do.
- Agent integrations declare identity: pass `agent_id` and `agent_type`
  (plus the other identity fields above as needed), or bind a key.
- Only `trusted_local_agent` and `admin_agent` profiles can commit outside
  the `project` domain; `project_scoped_agent` commits are limited to
  project-domain memories within their project scope;
  `memory_proposal_agent` and `read_only_agent` callers are routed to
  review or rejected.
- On HTTP, once any agent API key exists for the user, commits that declare
  an agent identity without a key are rejected with `401`; callers must
  send `Authorization: Bearer alice_sk_...`.
- On MCP, set `ALICE_AGENT_API_KEY` in the server environment to bind the
  server to a key; without it the MCP server runs as local operator
  tooling and payload identity is honored (audited as
  `unauthenticated_local`).
- When a key is in play, the key record — not the payload — determines
  `agent_id` and `permission_profile`; claiming a different agent or a
  higher profile is rejected and logged.
- Keys cannot be minted in SQLite on-ramp mode — `alicebot agent keys
  create` requires Postgres. Leave `ALICE_AGENT_API_KEY` unset there:
  identity is honored and audited as `unauthenticated_local`. If it is
  set anyway, enforcement fails closed and every write is rejected.

## CLI Example

```bash
alicebot context-pack "Alice public preview sprint context" --domain project --project Alice

alicebot vnext agents ingest-output \
  --agent-id openclaw \
  --agent-type coding_agent \
  --agent-run-id run-2026-05-12-001 \
  --project-scope Alice \
  --permission-profile project_scoped_agent \
  --title "OpenClaw sprint summary" \
  --output-type sprint_summary \
  --domain project \
  --sensitivity private \
  --propose-memory \
  "Decision: Alice public preview agents use scoped context packs and review-only memory proposals."

alicebot vnext memories commit \
  --agent-id openclaw \
  --agent-type coding_agent \
  --agent-run-id run-2026-05-12-001 \
  --project-scope Alice \
  --permission-profile project_scoped_agent \
  --title "Release gate decision" \
  --text "Alice public preview release gates require doctor, smokes, evals, and git diff checks before merge." \
  --domain project \
  --sensitivity private \
  --confidence 0.94
```

## Smoke

```bash
alicebot vnext smoke agent-integration-pack
alicebot vnext smoke agentic-memory-commit
```

The smokes verify scoped context, output ingestion, explicit trusted memory commits, inline confirmation, review gating, undo/correction/forget, no direct database mutation, event logging, restricted-domain policy blocking, and `/vnext` agent activity visibility.
