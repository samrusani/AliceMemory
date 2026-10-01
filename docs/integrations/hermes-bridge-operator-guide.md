# Hermes Bridge Operator Guide

This is the canonical operator guide for the Hermes bridge bundled with the
Alice `v0.9.x` line. The embedded Hermes provider plugin keeps its own `0.5.2`
integration-contract version in `plugin.yaml` (`0.5.1` in v0.18.0 and v0.19.0).
Unreleased (on main, not in v0.19.2): `0.5.3`. These numbers are not the Alice
application release and do not imply `v1.0.0` guarantees.

Recommended deployment shape: **provider plus MCP**.

- Provider handles always-on prefetch and post-turn lifecycle hooks.
- MCP handles explicit deep workflows (review, correction, explainability, targeted recall).

Use MCP-only as a fallback when provider install is temporarily blocked.

## Integration Modes

| Mode | Status | When to use | Tradeoff |
|---|---|---|---|
| Provider + MCP | Recommended | Default production/dev setup | Full bridge behavior with explicit deep actions |
| MCP-only | Fallback | Provider plugin cannot be installed yet | No provider lifecycle hooks or automatic prefetch/capture |

## Config Examples (`~/.hermes/config.yaml`)

- Recommended mode: `docs/integrations/examples/hermes-config.provider-plus-mcp.yaml`
- Fallback mode: `docs/integrations/examples/hermes-config.mcp-only.yaml`

Both examples turn on the full eleven-tool core surface with
`ALICE_MCP_FULL_TOOLS=1` and use its canonical
`alice_memory_review` / `alice_memory_correct` review flow. For a key-bound
Hermes server, set `ALICE_AGENT_API_KEY` to an issued key; do not enable the
legacy flag, because authenticated MCP runs deliberately hide and reject the
partially authorized legacy surface (see `docs/integrations/mcp.md`).

### Recommended snippet (provider + MCP)

```yaml
memory:
  provider: alice

mcp_servers:
  alice_core:
    command: "/path/to/alicebot/.venv/bin/python"
    args: ["-m", "alicebot_api.mcp_server"]
```

### Fallback snippet (MCP-only)

```yaml
memory:
  provider: builtin

mcp_servers:
  alice_core:
    command: "/path/to/alicebot/.venv/bin/python"
    args: ["-m", "alicebot_api.mcp_server"]
```

## One-Command Local Demo

Run the bridge demo command from this repository:

```bash
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

Expected result:

- `status` is `pass`
- `recommended_path` is `provider_plus_mcp`
- `fallback_path` is `mcp_only`
- provider smoke and MCP smoke steps both return `0`

## Validation Commands

Run these directly when you need independent evidence:

```bash
./.venv/bin/python scripts/run_hermes_memory_provider_smoke.py
./.venv/bin/python scripts/run_hermes_mcp_smoke.py
```

## MCP-Only to Provider+MCP Migration

1. Keep your existing MCP block unchanged.
2. Install the Alice provider plugin:

```bash
./scripts/install_hermes_alice_memory_provider.py
```

3. Run Hermes memory setup and select `alice`:

```bash
hermes memory setup
```

4. Set `memory.provider` to `alice` in `config.yaml`.
5. Re-run the one-command demo:

```bash
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

6. Keep MCP enabled for explicit review/correction/explain workflows.

## Related Docs

- `docs/integrations/hermes-provider-plus-mcp-why.md`
- `docs/integrations/hermes-memory-provider.md`
- `docs/integrations/hermes.md`
- `docs/integrations/hermes-skill-pack.md`
- `docs/release/v0.9.2-release-notes.md`
- `RELEASING.md`
