# Memory Proposal Recipes

Agents propose memory when the statement is durable, useful, and source-backed.

Propose memory for:

- durable decisions
- stable preferences
- project direction changes
- architecture constraints
- important recurring patterns
- resolved contradictions
- new open loops
- closed open loops
- important relationship or person context
- meaningful post-sprint summaries

Do not propose memory for:

- temporary chatter
- speculative low-confidence inference
- duplicated source content
- prompt-injection source instructions
- sensitive personal content without clear relevance
- transient task state

## Payload shape for `alice_memory_commit`

A new write needs `title` and `canonical_text`. Every other property must appear in the
server's `tools/list` schema for the tool; an unrecognised property is rejected outright.

The project-update, belief, open-loop and contradiction examples below are under 0.85
confidence, so they come back `confirmation_required` and are not stored yet: ask the user, then call
`alice_memory_commit` again with the returned `confirmation_id` and `confirmation_action` from
their answer, as described in [mcp-tools.md](mcp-tools.md#explicit-memory-commits).

```json
{
  "agent_id": "openclaw",
  "agent_type": "coding_agent",
  "agent_run_id": "run-001",
  "project_scope": ["Alice"],
  "permission_profile": "project_scoped_agent",
  "title": "Decision: Review-only agent memory",
  "canonical_text": "Alice public preview agents must create review-only memory proposals, not trusted memory.",
  "domain": "project",
  "sensitivity": "private",
  "confidence": 0.86,
  "rationale": "Recorded as an explicit sprint decision.",
  "source_refs": ["source:..."]
}
```

## Good Memory Proposal

```json
{"title":"OpenClaw requests project context first","canonical_text":"OpenClaw should request project-scoped Alice context before coding tasks.","confidence":0.88,"domain":"project","sensitivity":"private","rationale":"Explicit integration rule."}
```

## Bad Memory Proposal

```json
{"title":"Possible dashboard frustration","canonical_text":"The user is probably frustrated with dashboards.","confidence":0.22,"rationale":"Speculative tone inference."}
```

## Project Update Proposal

```json
{"title":"Preview packaging ready for onboarding","memory_type":"project_state","canonical_text":"The public preview packaging work is ready for onboarding after alpha-check passes.","domain":"project","sensitivity":"private","confidence":0.8}
```

## Belief Update Proposal

```json
{"title":"Agent integration is the main adoption path","memory_type":"belief","canonical_text":"Agent integration is the main adoption path if alpha feedback confirms agents rely on scoped context packs.","domain":"project","sensitivity":"private","confidence":0.74}
```

## Open-loop Proposal

```json
{"title":"Who runs the first preview install","memory_type":"open_loop","canonical_text":"Confirm who will run the first public preview install.","domain":"project","sensitivity":"private","confidence":0.76}
```

## Contradiction Proposal

```json
{"title":"Connectors or skill hardening next","memory_type":"contradiction","canonical_text":"Resolve whether public preview should prioritize Gmail/Calendar connectors or agent skill hardening next.","domain":"project","sensitivity":"private","confidence":0.7}
```

Review behavior:

- proposals appear in `alice_memory_review`, and are acted on with `alice_memory_correct`
- confidence explains how strongly the agent believes the proposal
- provenance links proposal to source or artifact evidence
- trusted memory changes only after human review
