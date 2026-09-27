---
name: alice-explain-provenance
description: Explain why an answer is trustworthy by referencing Alice continuity provenance and correction state from MCP tool outputs.
version: 1.0.0
author: Alice
license: MIT
metadata:
  hermes:
    tags: [alice, continuity, provenance, explainability, mcp]
    related_skills: [alice-continuity-recall, alice-correction-loop]
---

# Alice Explain Provenance

Legacy pack. The supported Hermes pack is `agent-skills/hermes/alice-memory`.
This skill needs the full tool surface: set `ALICE_MCP_FULL_TOOLS=1` in the
Alice MCP server env. On the default three tools, only the `alice_recall` step
works.

## Goal

Provide evidence-backed explanations for Alice-based answers.

## Trigger Cues

Use this skill when the user asks:
- why are you saying this
- what source supports this
- can you show evidence or provenance

## Required MCP Tools

- `mcp_<alice_server>_alice_context_pack`
- Optional: `mcp_<alice_server>_alice_recall`

`<alice_server>` is the key under `mcp_servers`. It is `alice` when
`alice-memory install --host hermes` wrote it and `alice_core` in the example
configs.

## Workflow

1. Start from `alice_context_pack` with the claim as `query`. It takes no `thread_id`.
2. If needed, run a focused `alice_recall` query for missing evidence.
3. Explain answer claims by citing returned continuity object IDs and provenance fields.
4. If provenance is thin, state uncertainty and propose the next validating step.

## Tool Call Templates

```text
mcp_<alice_server>_alice_context_pack({"query":"<claim>","include_sources":true,"max_items":10})
```

```text
mcp_<alice_server>_alice_recall({"thread_id":"<uuid>","query":"<claim>","limit":5})
```

## Output Contract

Always include:
- claim
- supporting object IDs
- provenance summary from returned records
- confidence posture (`high`, `medium`, `low`) based on evidence quality
