# Hermes Dogfood On Ubuntu

This guide connects Hermes to Alice on the same headless Ubuntu host. Alice remains local-first and review-first: Hermes can request scoped context, submit outputs, propose memory, and create open loops, but its submitted outputs and proposals are review-only. An explicit `alice_memory_commit` follows the commit policy, and for the `trusted_local_agent` profile it can become active memory at once.

## Same-Host Endpoints

```yaml
alice:
  api_url: http://127.0.0.1:8000
  mcp_command: ~/alicebot/.venv/bin/python -m alicebot_api.mcp_server
  mcp_server_name: alice
```

Recommended Hermes identity:

```yaml
agent_id: hermes
agent_type: personal_assistant
permission_profile: trusted_local_agent
project_scope:
  - Alice
```

Use `trusted_local_agent` only for a local operator-owned Hermes runtime on the same host. For narrower project work, use `project_scoped_agent`.

## Memory Provider Capture Config

For the Alice Hermes memory provider, `bridge_mode: auto` or `bridge_mode: assist`
now enables post-turn `sync_turn` capture when `sync_turn_capture_enabled` is
omitted. Keep the flag explicit in production dogfood configs so the posture is
obvious:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "user_id": "00000000-0000-0000-0000-000000000001",
  "bridge_mode": "auto",
  "sync_turn_capture_enabled": true
}
```

Set `sync_turn_capture_enabled` to `false` when Hermes should use Alice recall
and prefetch without committing post-turn capture candidates.

## MCP Config Example

```yaml
mcp_servers:
  alice:
    command: /home/alice/alicebot/.venv/bin/python
    args: ["-m", "alicebot_api.mcp_server"]
    cwd: /home/alice/alicebot
    env:
      DATABASE_URL: postgresql://alicebot_app:<redacted>@127.0.0.1:5432/alicebot
      ALICEBOT_AUTH_USER_ID: "00000000-0000-0000-0000-000000000001"
      ALICE_MCP_LEGACY_TOOLS: "1"
```

Do not paste real database passwords into shared logs. Keep this config readable only by the Hermes/Alice operator.

This example is a deliberately keyless, same-host compatibility setup because
the recipes below call legacy `alice_vnext_*` tools. Do not add
`ALICE_AGENT_API_KEY` to this server: key-bound MCP hides and rejects the
legacy surface even when the flag is set. For the recommended authenticated
deployment, follow the [Hermes bridge operator guide](../integrations/hermes-bridge-operator-guide.md)
and use the default three tools in [mcp-tools.md](mcp-tools.md).

## API Request Shape

Hermes should include identity metadata when it asks Alice for context or submits output:

```json
{
  "agent_id": "hermes",
  "agent_type": "personal_assistant",
  "agent_run_id": "hermes-dogfood-001",
  "task_id": "first-alice-context-test",
  "permission_profile": "trusted_local_agent",
  "project_scope": ["Alice"]
}
```

## First Hermes Test Task

Ask Hermes:

```text
Use Alice to retrieve project context for the headless Ubuntu alpha install. Summarize the next three operator actions, then submit your output back to Alice as a reviewable project update and propose one memory only if it is durable and source-grounded.
```

Expected Alice behavior:

- Hermes requests a project-scoped context pack.
- Hermes submits a source/artifact back to Alice.
- Hermes proposes memory as a candidate only.
- `/vnext` Agent Activity shows the Hermes run.
- `/vnext` Inbox or Memory Review shows the proposal.
- The submitted output and the proposal create nothing active without review. This setup is keyless, so Hermes's identity is declared and no issued key backs it, and Alice never auto-promotes a write from a declared identity, whatever `ALICE_MEMORY_PERSONA` says. A deployment that opts in and issues an agent key follows [memory promotion personas](../memory/promotion-personas.md).

## Context Pack Recipe

Use MCP where available:

```json
{
  "tool": "alice_vnext_context_pack",
  "arguments": {
    "query": "headless Ubuntu Alice install Hermes dogfood",
    "domains": ["project"],
    "projects": ["Alice"],
    "sensitivity_allowed": ["public", "internal", "private", "unknown"],
    "max_items": 8,
    "agent_identity": {
      "agent_id": "hermes",
      "agent_type": "personal_assistant",
      "agent_run_id": "hermes-dogfood-001",
      "task_id": "first-alice-context-test",
      "permission_profile": "trusted_local_agent",
      "project_scope": ["Alice"]
    }
  }
}
```

## Submit Output And Propose Memory

```json
{
  "tool": "alice_vnext_ingest_agent_output",
  "arguments": {
    "agent_id": "hermes",
    "agent_type": "personal_assistant",
    "agent_run_id": "hermes-dogfood-001",
    "task_id": "first-alice-context-test",
    "project_scope": ["Alice"],
    "permission_profile": "trusted_local_agent",
    "title": "Hermes headless Ubuntu dogfood summary",
    "content": "Decision: Alice should be accessed over SSH tunnel first on headless Ubuntu. Next actions: verify services, run alpha check, inspect /vnext review queues.",
    "output_type": "project_update",
    "domain": "project",
    "sensitivity": "private",
    "propose_memory": true
  }
}
```

If the output includes uncertainty or one-off observations, set `propose_memory` to `false` and create an open loop instead.

## Policy-Boundary Test

Use this policy-boundary test to prove a project-scoped Hermes identity cannot expand a request into restricted personal domains. Run it with `permission_profile: project_scoped_agent` in the request identity. The `trusted_local_agent` profile shown above reads every domain, so under it Alice does not block or filter family or health on domain.

Ask Hermes to request private family, health, or spiritual context while using the Alice project scope:

```text
Request Alice context for family and health memories while scoped only to the Alice project.
```

Expected result with `project_scoped_agent`:

- Alice blocks or filters the restricted-domain request.
- A policy event is recorded.
- `/vnext` Agent Activity shows the block/filter.
- Hermes should continue with project-scoped context only.

Run the shipped smoke:

```bash
~/alicebot/.venv/bin/alicebot vnext smoke agent-integration-pack
```

Run the headless package smoke:

```bash
~/alicebot/.venv/bin/alicebot vnext smoke headless-ubuntu
```

## Verify In `/vnext`

Tunnel from your laptop:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@ubuntu-box
```

Open:

```text
http://127.0.0.1:3000/vnext
```

Review:

- Agent Activity for `hermes`
- Memory Review for candidate proposals
- Generated artifacts for submitted outputs
- Open Loops for unresolved follow-ups
- Trace view for source-to-artifact provenance

## CLI Fallback

```bash
~/alicebot/.venv/bin/alicebot vnext agents policy-telemetry
~/alicebot/.venv/bin/alicebot context-pack "Hermes dogfood" --domain project --sensitivity-allowed private
~/alicebot/.venv/bin/alicebot vnext doctor --fix-safe --ci
```
