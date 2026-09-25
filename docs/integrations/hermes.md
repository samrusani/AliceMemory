# Hermes Reference Integration

Hermes is the reference path when another agent runtime owns orchestration and Alice supplies continuity, recall, resumption, and review workflows.

Recommended deployment shape: `provider_plus_mcp`.

- Provider gives Hermes always-on prefetch plus post-turn capture hooks.
- MCP gives Hermes the default three tools (`alice_memory_commit`,
  `alice_recall`, `alice_resume`). The other eight core tools are behind
  `ALICE_MCP_FULL_TOOLS=1`. The deeper legacy
  surface is limited to deliberately keyless, same-host operator compatibility;
  a server bound with `ALICE_AGENT_API_KEY` hides and rejects every legacy tool.
- MCP-only remains available when provider install is blocked.

## What Stays Stable

Hermes does not create a second Alice runtime contract.

- one-call continuity stays `POST /v1/continuity/brief` and `alice brief`.
  MCP `alice_brief` is a legacy, keyless-local compatibility path; key-bound
  integrations use the core `alice_recall` / `alice_resume` tools instead.
- provider registration and runtime shaping stay on the shipped Alice provider surface

Use these docs when you need the underlying Alice runtime controls:

- `docs/integrations/one-call-continuity.md`
- `docs/integrations/phase14-provider-configuration.md`

## Choose A Mode

| Mode | Use it when | What you get |
|---|---|---|
| Provider + MCP | default Hermes deployment | prefetch, post-turn lifecycle hooks, plus full Alice tool access |
| MCP-only | provider plugin install is blocked | explicit Alice tools with no provider lifecycle hooks |
| Provider + MCP + skill pack | you want stronger prompting and workflow policy | recommended runtime shape plus Hermes-side policy guidance |

## Recommended Setup

1. Install the Alice Hermes memory provider:

```bash
./scripts/install_hermes_alice_memory_provider.py
```

2. Configure Hermes memory for `alice` and keep Alice attached through `mcp_servers`.

3. Validate the full bridge path:

```bash
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

Expected JSON output:

- `status` = `pass`
- `recommended_path` = `provider_plus_mcp`
- provider smoke returns `structural.bridge_status.ready = true`
- MCP smoke validates recall, open-loop, capture, and review flows

## Minimal Config Shape

```yaml
memory:
  provider: alice

mcp_servers:
  alice_core:
    command: "/path/to/alice/.venv/bin/python"
    args: ["-m", "alicebot_api.mcp_server"]
    env:
      DATABASE_URL: "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot"
      ALICEBOT_AUTH_USER_ID: "00000000-0000-0000-0000-000000000001"
      PYTHONPATH: "/path/to/alice/apps/api/src:/path/to/alice/workers"
      # Recommended authenticated posture:
      ALICE_AGENT_API_KEY: "<load from a protected secret provider>"
```

Use the full operator examples for production config details:

- `docs/integrations/examples/hermes-config.provider-plus-mcp.yaml`
- `docs/integrations/examples/hermes-config.mcp-only.yaml`

## One-Call Continuity Inside Hermes

For generic continuity lookups on a key-bound server, use the core
`alice_recall` and `alice_resume` tools. Use `POST /v1/continuity/brief` when
the HTTP one-call contract is preferable.

The legacy MCP `alice_brief`, `alice_review_queue`, and `alice_review_apply`
tools remain available only for a deliberately keyless same-host operator
server with `ALICE_MCP_LEGACY_TOOLS=1`; do not combine that compatibility
mode with `ALICE_AGENT_API_KEY`. The core review path is:

- `alice_recall` (core) for ranked facts only
- `alice_resume` (core) for a structured resumption brief
- `alice_memory_review` and `alice_memory_correct` (core, `ALICE_MCP_FULL_TOOLS=1`) for explicit review and correction

## Provider Guidance

Hermes should treat Alice provider decisions as Alice runtime configuration,
not Hermes configuration.

- register or update provider connections through the Alice provider endpoints documented in `docs/integrations/phase14-provider-configuration.md`
- keep Hermes focused on when to call Alice, not on reproducing Alice runtime policy

## Fallback Path

If provider install is not available yet, keep:

- `memory.provider: builtin`
- the Alice `mcp_servers` block
- the same `alice_brief` / `alice_recall` / review tool usage

Then validate with:

```bash
hermes mcp test alice_core
./.venv/bin/python scripts/run_hermes_mcp_smoke.py
```

## Re-running install

`alice-memory install --host hermes` keeps a documented Alice env value on
`mcp_servers.alice` when that value is a one-line plain, single-quoted, or
double-quoted scalar, with no anchor, alias, tag, or block scalar. The keys
are `ALICE_MCP_FULL_TOOLS`, `ALICE_MCP_LEGACY_TOOLS`, `ALICE_AGENT_API_KEY`,
`ALICE_LEGACY_SURFACES`, `ALICE_EMBEDDINGS_BASE_URL`,
`ALICE_EMBEDDINGS_MODEL`, and `ALICE_EMBEDDINGS_API_KEY`. The name and the
scalar text stay as written. The receipt lists the kept keys and masks
printed values the same way as the other hosts, so `ALICE_AGENT_API_KEY`
and `ALICE_EMBEDDINGS_API_KEY` are not printed. Any other key install did
not write still refuses the file. Install refuses while those keys are
present.
Edit the alice entry by hand. A documented key whose value is an anchor, an
alias, a tag, or a block scalar is refused the same way.

## Related Docs

- `docs/integrations/hermes-bridge-operator-guide.md`
- `docs/integrations/hermes-memory-provider.md`
- `docs/integrations/hermes-skill-pack.md`
- `docs/integrations/reference-paths.md`
