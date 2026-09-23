# OpenClaw Alice Project Memory Skill

Use this instruction block in OpenClaw when Alice is available.

Note: the structured ingestion and memory-commit MCP payloads below target the legacy `alice_vnext_*` tool surface. They require a deliberately keyless local server with `ALICE_MCP_LEGACY_TOOLS=1`; a server bound with `ALICE_AGENT_API_KEY` hides and rejects them. New authenticated integrations should use the default three tools in [mcp-tools.md](mcp-tools.md): `alice_memory_commit`, `alice_recall`, `alice_resume`. Capture and the pack are on the full surface (`ALICE_MCP_FULL_TOOLS=1`).

```text
You are OpenClaw. Use Alice as the project-scoped memory and continuity layer.

Default loop: remember, recall, continue.
1. Identify as OpenClaw.
2. Call alice_memory_commit whenever you learn a durable project fact worth keeping, including when the user has not asked you to remember it. Domain must be project.
3. Call alice_recall to search project memory and imported sources.
4. Call alice_resume to pick work back up: last decision, next action, open loops, recent changes.
5. alice_capture and alice_context_pack are full-surface. Use them only when the server lists them. Capture stores a source; its passages come back from alice_recall under sources, as material to read and quote rather than as facts Alice asserts. Candidates stay unsearchable until a reviewer promotes them. Import is a source. Commit is a fact. Print the receipt field after a capture or commit so the user sees what was stored. Do not tell the user they must clear a review queue before a note is usable.
6. Do not access or write non-project personal domains.
7. If alice_memory_commit returns confirmation_required, nothing is stored yet. Ask the user, showing them the proposed text, then call alice_memory_commit again with the confirmation_id, confirmation_action "confirm" or "reject" from their answer, and your identity fields, and no memory fields. Never answer for the user.
```

`context_depth` (and `budget_strategy` for `max_tokens` packing) are
fields on the context-pack request. The matching `alice_context_pack` MCP
tool is full-surface. Trust the server's `tools/list` schema and omit them
if not listed.

Default identity:

```json
{
  "agent_id": "openclaw",
  "agent_type": "coding_agent",
  "permission_profile": "project_scoped_agent",
  "project_scope": ["Alice"]
}
```

Allowed direct commit domain: `project`.

Context/read domains may include `project`, `professional`, and `system` when policy allows.

Restricted by default: `personal`, `family`, `health`, `spiritual`, `legal`, `financial`, `regulated`.

Project context recipe. On the full-surface `alice_context_pack` the scope fields are flat, not nested
under `scope` and `options`:

```json
{
  "query": "current sprint decisions, architecture constraints, open loops",
  "domains": ["project"],
  "projects": ["Alice"],
  "sensitivity_allowed": ["public", "internal", "private", "unknown"],
  "max_items": 10
}
```

Sprint output, submitted through full-surface `alice_capture` when the server lists it. The text
itself is searchable from the next call onward and comes back under `sources`, as material to
read and quote rather than as facts Alice asserts. Candidates stay unsearchable until a
reviewer promotes them. The field carrying the text is `raw_text`:

```json
{
  "agent_id": "openclaw",
  "agent_type": "coding_agent",
  "agent_run_id": "openclaw-sprint-001",
  "task_id": "public-alpha-packaging",
  "project_scope": ["Alice"],
  "title": "OpenClaw sprint summary",
  "raw_text": "Decision: Public alpha agents use scoped context packs and review-only memory proposals.",
  "domain": "project",
  "sensitivity": "private"
}
```

Project memory commit:

```json
{
  "agent_id": "openclaw",
  "agent_type": "coding_agent",
  "permission_profile": "project_scoped_agent",
  "project_scope": ["Alice"],
  "title": "Release gate decision",
  "canonical_text": "Alice public preview release gates require doctor, smokes, evals, and git diff checks before merge.",
  "domain": "project",
  "sensitivity": "private",
  "confidence": 0.94,
  "source_type": "direct_user_instruction"
}
```

A new write needs `title` and `canonical_text`. Every other property must appear in
the server's `tools/list` schema for the tool you are calling; an unrecognised property is
rejected outright rather than ignored. In particular `intent` exists only on the legacy
`alice_vnext_commit_memory` tool and is **not** accepted by `alice_memory_commit`. A
`project_scoped_agent` must send `domain: "project"`, or the commit is rejected.

If Alice returns `confirmation_required` (for example at a confidence between 0.5 and 0.85),
nothing is stored yet. Ask the user, showing them the proposed text, then call
`alice_memory_commit` again with only the returned `confirmation_id`, `confirmation_action`
set to `confirm` or `reject` from their answer, and your identity fields. Never answer for the
user; Alice cannot tell whether you asked. There is no edit on this call: to change the text,
reject it and commit the corrected text. After 24 hours it can no longer be confirmed on
`alice_memory_commit`: the next confirm or reject there that passes the policy check resolves
it to `rejected`. A reviewer using `alice_memory_correct` `approve` on the full surface, which
does not check the 24 hours, can still approve it. A key bound to another project can neither
confirm nor reject it; a keyless server trusts whatever `project_scope` the call declares, so
this fence needs a project-bound key. A `project_scoped_agent`
cannot confirm a pending write above its sensitivity ceiling, only reject it, so keep project
facts at `private` or below.

If Alice returns `review_required`, leave the item. Do not tell the user to clear a review
queue. If Alice returns `rejected`, do not retry outside the `project` domain. Use
`alice_memory_manage` with an `action` of `undo`, `forget` or `expire` for repairs when that
tool is listed; never write directly to the database.

Do propose memory for:

- accepted architecture decisions
- durable project direction
- unresolved release risks
- post-sprint state changes

Do not propose memory for:

- raw logs
- temporary implementation chatter
- duplicated source text
- private personal context outside the project scope
