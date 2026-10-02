# MCP Integration

Alice's MCP server exposes three tools by default (`alice_memory_commit`,
`alice_recall`, `alice_resume`). The other eight core tools become listed
and callable when `ALICE_MCP_FULL_TOOLS=1`. The legacy long-tail surface
is available behind a separate environment flag.

## Entrypoints

```bash
./.venv/bin/python -m alicebot_api.mcp_server --help
./.venv/bin/python -m alicebot_api.mcp_server
alicebot-mcp --help
alicebot-mcp
```

`alicebot-mcp` is available after editable install.

## Runtime Scope

MCP uses the same local runtime scope as the CLI:

- `DATABASE_URL`
- `ALICEBOT_AUTH_USER_ID`

With a `sqlite:///` `DATABASE_URL` (or the packaged
`alice-memory mcp --data-dir ~/.alice`), the server bootstraps the user
row automatically on first start.

Optional:

- `ALICE_EMBEDDINGS_BASE_URL`, `ALICE_EMBEDDINGS_MODEL`,
  `ALICE_EMBEDDINGS_API_KEY` — enable semantic vector search in
  `alice_recall` and `alice_context_pack` (full-text-only without them)
- `ALICE_EMBEDDINGS_MAX_INPUT_CHARS` (from v0.20.0): the most characters of
  one memory's text, or of one recall query, sent to
  the embeddings endpoint. Longer text is cut to this many characters, and the
  vector of a cut memory is labelled as made from a cut text. A whole number
  from 256 to 1000000; the default is 8000, which fits models that take about
  8,000 tokens. A model with a smaller window needs a lower value, for example
  1500 for one that takes 512 tokens. A memory whose embedded text (its title,
  text and summary) is over 8,000 characters is embedded from its first 8,000 on
  a model that could take more, so raise it for long memories on a large-window
  model. Changing it makes `alice-memory
  reindex-embeddings` re-embed only the memories whose embedded text changes.
  In v0.19.2 there is no cap.
- `ALICE_MCP_FULL_TOOLS=1` — advertise all eleven core tools and accept
  calls to the eight that are hidden by default
- `ALICE_MCP_LEGACY_TOOLS=1` — append 62 retained long-tail memory tools to
  whatever core set is enabled, only for an unbound local-operator server;
  ignored when `ALICE_AGENT_API_KEY` is set
- `ALICE_LEGACY_SURFACES=1` — additionally expose the three task-brief tools.
  Both flags are read at process start (routes are mounted at import time), so
  changing them requires restarting the server
  when the MCP legacy flag is also set; this mount-time compatibility flag is
  deprecated for removal before `1.0`

## Default Tool Surface

- `alice_memory_commit` — explicit policy-checked memory write with commit / confirmation / review / reject outcomes
- `alice_recall` — hybrid full-text + vector search with fused ranking; hard
  pre-limit scopes support `thread_id`, `task_id`, `project`/`projects`,
  `person`/`people`, and absolute `since`/`until` bounds
- `alice_resume` — resumption brief for a project, person, or thread

`ALICE_MCP_FULL_TOOLS=1` also advertises `alice_capture`, `alice_context_pack`,
`alice_open_loops`, `alice_recent_decisions`, `alice_memory_review`,
`alice_memory_correct`, `alice_memory_manage`, and `alice_explain`.

Full schemas with per-parameter descriptions come from `tools/list`.
Details and examples: [docs/alpha/mcp-tools.md](../alpha/mcp-tools.md).

## Legacy Tool Surface

With `ALICE_MCP_LEGACY_TOOLS=1`, 62 retained legacy memory tools are listed
alongside whatever core set is enabled (65 with the default three, 73 with
the full eleven). With the task-brief flag as well, the counts are 68 and
76. The legacy surface requires Postgres: on the SQLite backend the legacy
tools are listed but their calls fail. It also requires `ALICE_AGENT_API_KEY`
to be unset. Key-bound servers list and accept only the enabled core set.
The long tail covers briefs, timeline, state-at-time, capture pipelines,
queue/graph/belief/scheduler controls, provider runtime tools, and the
`alice_vnext_*` agentic control-plane contract, including
`alice_vnext_ingest_agent_output` (agent-output capture as untrusted source
evidence) and `alice_vnext_commit_memory` (explicit policy-checked memory
writes with commit / confirmation / review / reject outcomes).

Permanently deleted hosted, Telegram-channel, chat, chief-of-staff, and model-
pack tools are absent under every flag combination. The retained legacy surface
is frozen: new capabilities land on the core tools.

For first-run memory expectations and a deterministic way to prove memory
is working, see [../alpha/first-memory.md](../alpha/first-memory.md).
Normal chat is not guaranteed to become trusted memory; explicit memory
instructions should use an explicit commit or capture call.

## Example: Claude Desktop MCP Config

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

## Hermes

For Hermes Agent-specific setup, prompts, and troubleshooting:

- `docs/integrations/hermes-bridge-operator-guide.md` (recommended provider+MCP path)
- `docs/integrations/hermes.md`
- `docs/integrations/hermes-memory-provider.md`
- `docs/integrations/hermes-skill-pack.md`

Recommended bridge deployment shape:

- provider plus MCP is the default operator path
- MCP-only remains available as fallback when provider install is blocked

One-command bridge demo:

```bash
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

## Contract Guardrails

- the default tool set is intentionally small and stable
- failed `tools/call` responses keep the MCP result shape (`isError: true`)
  and put exactly one compact JSON object in `content[0].text`:
  `{"error":{"code":"...","message":"..."}}`
- tool failure codes are `tool_not_found`, `tool_request_failed`, and
  `tool_execution_failed`; their messages are static, while exception details
  are written only to server logs. From v0.19.2, `invalid_request` is a fourth code, used when `alice_recall` or
  `alice_context_pack` gets a query the SQLite source search cannot take. Its
  message names the limit and holds only counts, never the query. From v0.20.0,
  `alice_resume` and `alice_recent_decisions` use it for
  a query over 40,000 UTF-8 bytes.
- JSON-RPC framing errors use the standard static messages `Parse error`,
  `Invalid Request`, `Invalid params`, and `Method not found`; request data and
  parser exception text are never copied into the wire response
- responses are compact by default; diagnostic traces are opt-in via the
  `debug` parameter on read tools
- agent-proposed memory requires review; nothing an agent submits becomes
  trusted memory without passing the commit policy engine
- agent-output ingestion treats agent text as untrusted source evidence

See tests:

- `tests/unit/test_mcp.py`
- `tests/integration/test_mcp_server.py`
- `tests/integration/test_temporal_state_mcp_cli.py`
- `tests/integration/test_openclaw_mcp_integration.py`
