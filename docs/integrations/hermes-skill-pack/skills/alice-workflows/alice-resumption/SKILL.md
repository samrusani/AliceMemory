---
name: alice-resumption
description: Build deterministic continuation briefs with Alice MCP resumption tools when users want to pick up interrupted work.
version: 1.0.0
author: Alice
license: MIT
metadata:
  hermes:
    tags: [alice, continuity, resume, mcp]
    related_skills: [alice-continuity-recall, alice-open-loop-review]
---

# Alice Resumption

Legacy pack. The supported Hermes pack is `agent-skills/hermes/alice-memory`.
This skill needs the full tool surface: set `ALICE_MCP_FULL_TOOLS=1` in the
Alice MCP server env. On the default three tools, only the `alice_resume` step
works.

## Goal

Resume work from deterministic continuity state instead of reconstructing history manually.

## Trigger Cues

Use this skill when the user asks:
- continue where we left off
- give me a restart brief
- summarize decisions, next action, blockers

## Required MCP Tools

- `mcp_<alice_server>_alice_resume`
- Optional: `mcp_<alice_server>_alice_context_pack`

`<alice_server>` is the key under `mcp_servers`. It is `alice` when
`alice-memory install --host hermes` wrote it and `alice_core` in the example
configs.

## Workflow

1. Call `alice_resume` with scoped filters and bounded limits.
2. If broader context is needed, call `alice_context_pack`.
3. Prioritize these sections in your answer:
   - last decision
   - next action
   - open loops
   - recent changes
4. Keep the final brief actionable and short.

## Tool Call Templates

```text
mcp_<alice_server>_alice_resume({"thread_id":"<uuid>","max_recent_changes":5,"max_open_loops":5})
```

```text
mcp_<alice_server>_alice_context_pack({"query":"<topic>","max_items":10})
```

## Output Contract

Always include:
- `last_decision`
- `next_action`
- `blockers_or_waiting_for`
- `recent_changes`
- explicit note when `thread_id` or scope is missing
