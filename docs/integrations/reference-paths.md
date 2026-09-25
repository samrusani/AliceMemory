# Reference Integration Paths

This page is the path-selection guide for external builders adopting Alice on
top of the latest published `v0.17.0` baseline. Later changes on `main` are
unreleased until they receive a release
identity and publication evidence.

## Default Recommendation

Start with the narrowest path that solves the integration need:

| Need | Default path | Demo or example |
|---|---|---|
| Generic external agent needs continuity in one call | `POST /v1/continuity/brief`; key-bound MCP integrations use core `alice_recall` / `alice_resume` (`alice_brief` is keyless-local legacy compatibility only) | `docs/examples/reference-agent-examples.md` |
| MCP client (Claude Desktop, Claude Code, any stdio MCP host) needs zero-setup local memory | `uvx alice-memory mcp` serving the default three tools over stdio | `docs/examples/mcp_quickstart.py` |
| Agent-framework tooling (OpenAI Agents SDK style function tools) with per-agent API keys | HTTP function tools over `/v0/vnext` with `Authorization: Bearer alice_sk_...` | `docs/examples/openai_agents_sdk_tool.py` |
| Hermes owns orchestration and Alice supplies continuity workflows | provider plus MCP | `./.venv/bin/python scripts/run_hermes_bridge_demo.py` |
| Existing OpenClaw workspace data must become queryable in Alice | import, then use normal brief/recall/resume surfaces | `./scripts/use_alice_with_openclaw.sh` |
| Alice must target a non-default runtime provider | supporting Alice-side configuration for the paths above | `docs/integrations/phase14-provider-configuration.md` |

The three major adoption paths are Generic Agent, Hermes, and OpenClaw. Provider
controls support those paths; they are not presented as a fourth standalone
demo path.

## Path Details

### Generic Agent

Use this when you are integrating Alice into a Python or TypeScript agent without adopting a framework-specific bridge.

- prefer one-call continuity first
- use `alice_recall` or `alice_resume` only when your agent truly needs narrower output
- examples: `docs/examples/generic_python_agent.py` and `docs/examples/generic_typescript_agent.ts`
- reproducible demo: `./.venv/bin/python scripts/run_reference_agent_examples_demo.py`

### MCP Quickstart

Use this when the integrating agent is an MCP client and memory should live in
a local SQLite file with no server to operate.

- entrypoint: `uvx alice-memory mcp` (or `alice-memory mcp` from an install)
- runnable example: `docs/examples/mcp_quickstart.py` spawns the packaged
  server, verifies the eleven-core-tool surface over live `tools/list`, and
  round-trips a capture, commit, and recall over stdio
- CI smoke: `tests/integration/test_mcp_quickstart.py`
- client configuration snippets: `docs/integrations/mcp.md`

### Agent-Framework Function Tools

Use this when a framework such as the OpenAI Agents SDK owns the loop and
Alice supplies memory tools authenticated with per-agent API keys.

- runnable example: `docs/examples/openai_agents_sdk_tool.py` defines
  capture/recall functions in the SDK's function-tool shape (no SDK
  dependency) calling `/v0/vnext` with `Authorization: Bearer alice_sk_...`
- key management: `alicebot agent keys create --agent-id <id> --profile <profile>`
  with server-side permission profiles; see `docs/alpha/agent-integration.md`
- CI smoke: `tests/integration/test_openai_agents_sdk_tool.py` exercises a
  real key end to end, including tampered-key rejection and the
  `read_only_agent` write refusal

### Hermes

Use Hermes when another runtime owns planning and execution, and Alice should stay focused on continuity plus review workflows.

- default recommendation: provider plus MCP
- fallback: MCP-only
- docs: `docs/integrations/hermes.md`
- reproducible demo: `./.venv/bin/python scripts/run_hermes_bridge_demo.py`

### OpenClaw

Use OpenClaw when the main requirement is importing existing workspace memory into Alice and then querying it through the normal Alice surfaces.

- imported data augments Alice continuity objects with explicit `OpenClaw` provenance
- after import, keep using the same brief, recall, resume, CLI, and MCP paths
- docs: `docs/integrations/openclaw.md`
- reproducible demo: `./scripts/use_alice_with_openclaw.sh`

### Provider Controls

Use the provider docs when Alice itself owns runtime selection.

- provider registration and capability discovery: `docs/integrations/phase14-provider-configuration.md`
- keep these controls in Alice rather than cloning them into Hermes or importer flows
- treat these controls as supporting configuration for the three major adoption paths above, not as a separate reference integration path

## Scope Guard

These paths package the shipped Alice surface. They do not introduce a second
continuity contract or provider substrate.
