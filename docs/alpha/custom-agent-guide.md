# Custom Agent Integration Guide

Any third-party agent should follow the same Alice pattern.

1. Identify yourself.
2. Request scoped context, not raw memory.
3. Use Alice context packs before acting.
4. Use the read-only context tree for orientation when the agent needs to navigate projects, memories, sources, artifacts, open loops, and traces before requesting a narrower context pack.
5. Submit important outputs back to Alice.
6. Commit explicit memory only through Alice's memory commit API/MCP/CLI path.
7. Propose memory for inferred, external-source-derived, generated, ambiguous, or lower-confidence facts.
8. Create open loops when work remains.
9. Respect domain and sensitivity policy.
10. Use `/vnext` for review, audit, undo, correction, forget, and troubleshooting.

## API Example

This is the body of `POST /v0/vnext/context-packs`.

```json
{
  "user_id": "00000000-0000-0000-0000-000000000001",
  "agent_identity": {
    "agent_id": "researcher",
    "agent_type": "research_agent",
    "agent_run_id": "research-001",
    "task_id": "market-map",
    "project_scope": ["Alice"],
    "permission_profile": "project_scoped_agent"
  },
  "query": "recent Alice market research decisions",
  "scope": {
    "domains": ["project"]
  },
  "options": {
    "sensitivity_allowed": ["public", "internal", "private", "unknown"],
    "max_items": 8
  }
}
```

## MCP Example

The default MCP surface is three tools (`alice_memory_commit`, `alice_recall`, `alice_resume`; see [mcp-tools.md](mcp-tools.md)). Capture and the pack are on the full surface (`ALICE_MCP_FULL_TOOLS=1`).

Read with `alice_recall` and `alice_resume`. On the full surface, `alice_context_pack` takes flat scope fields (`domains`, `projects`, `sensitivity_allowed`, `max_items`) and `alice_capture` submits an output as a source (the field that carries the text is `raw_text`).

For explicit "remember this" instructions, and for durable facts the agent learns on its own, call `alice_memory_commit`:

Use canonical schema values for persisted labels. For quote saves, use `memory_type=semantic`; use `memory_type=procedure` for repeatable playbooks with steps, applicability, failure modes, and provenance; use `domain=learning` only when a quote collection needs an explicit domain. Avoid invented values like `memory_type=quote`, `domain=quotes`, or `sensitivity=sensitive`.

Do not rely on passive chat transcript capture as the primary memory path. For first-run behavior, supported structured phrases, and troubleshooting, see [first-memory.md](first-memory.md).

```json
{
  "agent_id": "researcher",
  "agent_type": "research_agent",
  "permission_profile": "project_scoped_agent",
  "project_scope": ["Alice"],
  "title": "Research source rule",
  "canonical_text": "Alice project research briefs must separate quoted evidence from model interpretation.",
  "domain": "project",
  "sensitivity": "private",
  "confidence": 0.92,
  "source_type": "direct_user_instruction"
}
```

Alice returns one of four outcomes:

- `committed`: active memory, with event and revision audit trail.
- `confirmation_required`: nothing is stored. Ask the user, then call `alice_memory_commit` again with the returned `confirmation_id`, `confirmation_action` set to `confirm` or `reject` from the user's answer, the same identity fields, and no memory fields.
- `review_required`: the item waits for review, in `/vnext` on the Postgres stack or through `alice_memory_review` and `alice_memory_correct` on the full surface.
- `rejected`: the agent should narrow scope or ask the user instead of retrying broadly.

Repair, on the full surface: `alice_memory_manage` takes `undo`, `forget` and `expire`, `alice_memory_review` lists what waits, and `alice_memory_correct` approves, edits, rejects or supersedes.

The `alice_vnext_*` names, such as `alice_vnext_context_tree` for the read-only context tree, are the frozen legacy surface. They need a keyless server with `ALICE_MCP_LEGACY_TOOLS=1` and are hidden and rejected when `ALICE_AGENT_API_KEY` is set. On the SQLite backend most legacy tools fail, `alice_vnext_context_tree` among them. Of the `alice_vnext_*` tools, only thirteen run, the memory-commit family and four reads and captures; see [Legacy tool surface](mcp-tools.md#legacy-tool-surface) for the list.

## Agent Examples

- Research agent: request research/project context, ingest reports, propose only durable findings.
- Coding agent: request project context, ingest sprint summaries, propose decisions and risks.
- Personal assistant: request personal/professional context only when needed, propose stable preferences.
- Workflow orchestrator: request open-loop context, create new open loops for blocked work.
- Meeting-notes agent: ingest meeting summaries and propose decisions, commitments, and follow-ups.

Review queues:

- Memory Review: candidate memory proposals.
- Generated: agent outputs and generated artifacts.
- Open Loops: unresolved follow-ups.
- Trace: provenance from source to artifact.
