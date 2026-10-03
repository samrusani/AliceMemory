---
name: alice-open-loop-review
description: Use Alice open-loop MCP tools to review unresolved commitments and prioritize concrete next actions.
version: 1.0.0
author: Alice
license: MIT
metadata:
  hermes:
    tags: [alice, continuity, open-loops, review, mcp]
    related_skills: [alice-resumption, alice-correction-loop]
---

# Alice Open-Loop Review

Legacy pack. The supported Hermes pack is `agent-skills/hermes/alice-memory`.
This skill needs the full tool surface: set `ALICE_MCP_FULL_TOOLS=1` in the
Alice MCP server env. On the default three tools, `alice_open_loops` is not
available.

## Goal

Turn open loops into a prioritized action queue grounded in Alice continuity state.

## Trigger Cues

Use this skill when the user asks:
- what is still open
- what is blocked or waiting
- what should I do next from unresolved items

## Required MCP Tools

- `mcp_<alice_server>_alice_open_loops`
- Optional: `mcp_<alice_server>_alice_recent_changes` (keyless-local legacy tool; unavailable when the Alice MCP server is bound with `ALICE_AGENT_API_KEY`)

`<alice_server>` is the key under `mcp_servers`. It is `alice` when
`alice-memory install --host hermes` wrote it and `alice_core` in the example
configs.

## Workflow

1. Call `alice_open_loops` with `action` set to `list`. It takes no `thread_id`.
2. Keep output grouped by posture:
   - `waiting_for`
   - `blocker`
   - `stale`
   - `next_action`
3. Surface top priorities first (blocked and overdue items before low-risk follow-ups).
4. Convert each group into explicit next steps.

## Tool Call Template

```text
mcp_<alice_server>_alice_open_loops({"action":"list","limit":10})
```

## Output Contract

Always include:
- grouped open-loop summary
- top 3 priority actions
- per-item rationale tied to returned loop posture
