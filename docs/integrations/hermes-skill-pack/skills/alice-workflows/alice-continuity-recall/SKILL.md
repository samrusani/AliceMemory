---
name: alice-continuity-recall
description: Use Alice MCP recall tools for continuity-grounded answers with provenance when users ask what was decided, what changed, or what to remember.
version: 1.0.0
author: Alice
license: MIT
metadata:
  hermes:
    tags: [alice, continuity, recall, mcp]
    related_skills: [alice-resumption, alice-explain-provenance]
---

# Alice Continuity Recall

Legacy pack. The supported Hermes pack is `agent-skills/hermes/alice-memory`.
This skill needs the full tool surface: set `ALICE_MCP_FULL_TOOLS=1` in the
Alice MCP server env. On the default three tools, only the `alice_recall` step
works.

## Goal

Produce recall answers from Alice continuity records instead of free-form memory.

## Trigger Cues

Use this skill when the user asks:
- what was decided
- what happened in a thread/project/person scope
- what should be remembered from prior work

## Required MCP Tools

- `mcp_<alice_server>_alice_recall`
- Optional: `mcp_<alice_server>_alice_recent_decisions`

`<alice_server>` is the key under `mcp_servers`. It is `alice` when
`alice-memory install --host hermes` wrote it and `alice_core` in the example
configs.

## Workflow

1. Prefer `alice_recall` over inference-only answers.
2. Use hard scope filters when available (`thread_id`, `task_id`, `project`, `person`, `since`, `until`); Alice applies them before ranked result limits.
3. Keep `limit` bounded (normally `3` to `10`).
4. Return summary plus provenance-backed evidence IDs.

## Tool Call Templates

```text
mcp_<alice_server>_alice_recall({"query":"<topic>","thread_id":"<uuid>","limit":5})
```

```text
mcp_<alice_server>_alice_recent_decisions({"thread_id":"<uuid>","limit":5})
```

## Output Contract

Always include:
- direct answer
- top evidence items (`id`, `title`, `object_type`)
- provenance notes from the returned item fields
- uncertainty note if evidence is weak or absent
