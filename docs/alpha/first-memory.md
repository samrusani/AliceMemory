# First Memory Guide

Use this guide when Alice is installed correctly but a tester says, "I do not see any memories."

Alice is not automatic chat-history memory. The current public preview has several memory paths, and they have different trust and review behavior.

The default MCP surface is three tools (see [mcp-tools.md](mcp-tools.md)): `alice_memory_commit` to write, `alice_recall` to read back, and `alice_resume` to pick work up. `alice_explain` is on the full surface (`ALICE_MCP_FULL_TOOLS=1`).

## What To Expect

| Path | Best for | Result |
| --- | --- | --- |
| `alice_memory_commit` | explicit user-directed "remember/save this" instructions | active memory, confirmation, review, or rejection through Alice policy |
| `alice_capture` | agent summaries, documents, notes, or inferred durable facts | source-backed candidate memory awaiting review |
| `alicebot vnext sources capture-text ...` | raw evidence capture from text, files, browser clips, or connectors | source evidence plus deterministic candidate memories when extractable |
| Hermes provider `sync_turn` | post-turn bridge capture for structured turns | best-effort capture candidates, not general conversation memory |
| legacy explicit signal capture | simple preferences and commitments | deterministic memory/open-loop admission for supported phrases |

Normal conversation is not guaranteed to become memory. Agents should use the explicit commit/capture tools when the user clearly asks Alice to remember something.

## Fastest First Memory (any MCP client)

From a human client (Claude Desktop, an IDE) connected to the Alice MCP server, call `alice_memory_commit` with just a title and the memory text — no identity fields needed for direct human use:

```json
{
  "title": "Preferred planning format",
  "canonical_text": "The user prefers concise daily planning summaries with decisions, blockers, and next actions."
}
```

Alice returns one of:

- `committed`
- `confirmation_required` (nothing is stored yet; an agent must ask the user first, showing them the proposed text, then finish it by calling `alice_memory_commit` again with the returned `confirmation_id`, `confirmation_action` set to `confirm` or `reject` from the user's answer, the same identity fields if the write carried any, and no memory fields. Alice cannot tell whether anyone was asked, so an agent must never answer for the user.)
- `review_required`
- `rejected`

Now prove it round-trips:

1. Call `alice_recall` with `{"query": "planning summaries"}`. The memory comes back with its fused-rank score.
2. Call `alice_resume`. The last decision should be the fact you just committed.
3. On the full surface (`ALICE_MCP_FULL_TOOLS=1`), `alice_explain` with the returned `memory_id` shows where the memory came from, its revisions, and why it can be trusted.

Do not write directly to Postgres or SQLite, and do not bypass Alice policy.

## First Agent Memory Commit

Agent integrations declare identity on the same tool. Example payload:

```json
{
  "agent_id": "hermes",
  "agent_type": "personal_assistant",
  "permission_profile": "trusted_local_agent",
  "title": "Preferred planning format",
  "canonical_text": "The user prefers concise daily planning summaries with decisions, blockers, and next actions.",
  "domain": "personal",
  "sensitivity": "private",
  "confidence": 0.93,
  "source_type": "direct_user_instruction"
}
```

The same four outcomes apply; `read_only_agent` callers cannot write. Profile semantics are in [agent-integration.md](agent-integration.md).

## Fastest First Trusted Memory (full stack)

On the Postgres stack, run the shipped smoke test:

```bash
alicebot vnext smoke agentic-memory-commit
```

If `alicebot` is not on your shell path:

```bash
./.venv/bin/alicebot vnext smoke agentic-memory-commit
```

Expected result:

- direct trusted commit works for allowed explicit memory
- sensitive or ambiguous memory is routed to confirmation or review
- undo, correction, forget, and audit gates are exercised

## First Manual Source Capture

Capture a simple source that should produce reviewable candidate memory:

```bash
alicebot vnext sources capture-text "Fact: The user prefers concise daily planning summaries." --domain personal --sensitivity private
```

Then open `/vnext` and check:

- Inbox for the captured source
- Memory Review for candidate memory
- Trace for source-to-candidate provenance

This path creates reviewable evidence first. It does not silently promote trusted memory. Over MCP, `alice_capture` is the same path.

## Structured Phrases That Capture Better

The deterministic paths are intentionally narrow. These phrases are good first-run tests:

```text
I prefer concise summaries
I like short daily planning briefs
remember to follow up with the investor tomorrow
remind me to review the launch checklist
Fact: The user prefers concise daily planning summaries.
Decision: Use Alice as the continuity layer for this agent.
Preference: Keep daily briefs short.
Todo: Confirm the launch checklist owner.
Next action: Send the partner follow-up.
```

Unstructured chat, slang, and non-English natural-language memory requests may not match the deterministic capture rules. Use explicit MCP commit/capture tools for those cases.

## Hermes Provider Notes

The Hermes memory provider adds recall, prefetch, resumption briefs, open-loop lookup, and optional post-turn capture. It does not make every Hermes conversation an automatic trusted memory.

Check the provider config:

```json
{
  "bridge_mode": "assist",
  "sync_turn_capture_enabled": true
}
```

Important behavior:

- `sync_turn_capture_enabled: false` disables post-turn capture.
- `bridge_mode: manual` disables bridge capture behavior.
- post-turn capture is structured and policy-bound.
- trusted memory still requires explicit commit policy, confirmation, or review.

For Hermes, the recommended setup is provider plus MCP: use the provider for always-on continuity context, and MCP for explicit `alice_memory_commit` calls.

## Troubleshooting Checklist

If no memory appears:

1. Call `alice_memory_commit` with just `title` + `canonical_text` from your MCP client, then `alice_recall` for it.
2. On the full stack, run `alicebot vnext smoke agentic-memory-commit`.
3. For agent integrations, confirm the agent is using a permission profile that can commit or propose memory (`read_only_agent` cannot).
4. Check `/vnext` Memory Review (or `alice_memory_review` in SQLite mode) before assuming nothing was captured.
5. Check Trace (or `alice_explain`) for captured source evidence and candidate links.
6. For Hermes, verify `sync_turn_capture_enabled` and avoid `bridge_mode: manual` if post-turn capture is expected.
7. Test with one of the structured English phrases above.

If that works, Alice memory is functioning. The missing piece is agent prompting/integration, not the memory store.

---

Footnote: earlier previews taught this flow through the legacy `alice_vnext_commit_memory` / `alice_vnext_ingest_agent_output` tools. Those remain available only on a deliberately keyless Postgres server behind `ALICE_MCP_LEGACY_TOOLS=1`; key-bound and new integrations use the core tools above.
