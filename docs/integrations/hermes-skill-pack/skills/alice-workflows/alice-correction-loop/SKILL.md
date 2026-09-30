---
name: alice-correction-loop
description: Run deterministic correction workflows with Alice MCP review and correction tools, then verify that future outputs reflect the update.
version: 1.0.0
author: Alice
license: MIT
metadata:
  hermes:
    tags: [alice, continuity, correction, review, mcp]
    related_skills: [alice-explain-provenance, alice-open-loop-review]
---

# Alice Correction Loop

Legacy pack. The supported Hermes pack is `agent-skills/hermes/alice-memory`.
This skill needs the full tool surface: set `ALICE_MCP_FULL_TOOLS=1` in the
Alice MCP server env. On the default three tools, only the `alice_recall` and
`alice_resume` verification steps work.

## Goal

Apply corrections through Alice review tools and confirm that recall/resumption behavior updates accordingly.

## Trigger Cues

Use this skill when the user asks:
- this is outdated or wrong
- correct this memory
- supersede or mark stale

## Required MCP Tools

- `mcp_<alice_server>_alice_memory_review`
- `mcp_<alice_server>_alice_memory_correct`
- Verification: `mcp_<alice_server>_alice_recall` or `mcp_<alice_server>_alice_resume`

`<alice_server>` is the key under `mcp_servers`. It is `alice` when
`alice-memory install --host hermes` wrote it and `alice_core` in the example
configs.

## Workflow

1. Fetch review queue or detail with `alice_memory_review`.
2. Select correction action:
   - `approve`: keep the memory as it is.
   - `edit-and-approve`: fix it with `title` or `body`, then approve it.
   - `reject`: the memory is wrong or stale and has no replacement.
   - `supersede-existing`: replace it with `replacement_title` and `replacement_body`.
3. Apply correction with `alice_memory_correct`.
4. Re-run `alice_recall` or `alice_resume` to verify behavior changed.
5. Report both the correction action and the observed post-correction result.

## Tool Call Templates

```text
mcp_<alice_server>_alice_memory_review({"status":"correction_ready","limit":10})
```

```text
mcp_<alice_server>_alice_memory_correct({"continuity_object_id":"<uuid>","action":"supersede-existing","replacement_title":"<title>","replacement_body":{"text":"<text>"},"reason":"<reason>"})
```

## Output Contract

Always include:
- corrected object ID
- action applied
- reason
- post-correction verification result
