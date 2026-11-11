# One-Call Continuity

One continuity call is the default integration path for external agents:

- API: `POST /v1/continuity/brief`
- CLI: `alice brief`
- MCP: core `alice_resume`; `alice_context_pack` is full-surface; `alice_brief` is keyless-local legacy compatibility only

This surface composes the shipped recall, resumption, contradiction, and trust
systems into one response bundle so callers do not need tool choreography. The
separately persisted task-brief compatibility surface is not required.

## Defaults

- default `brief_type`: `general`
- timeline highlights: included by default
- `coding_context` and `operator_context` keep the same response shape as other brief types and differ by selection strategy only

## Supported Brief Types

- `general`
- `resume`
- `agent_handoff`
- `coding_context`
- `operator_context`

## Response Bundle

Every one-call brief returns:

- `summary`
- `relevant_facts`
- `recent_changes`
- `open_loops`
- `conflicts`
- `timeline_highlights`
- `next_suggested_action`
- `provenance_bundle`
- `trust_posture`

## API

`POST /v1/continuity/brief` uses the authenticated `v1` API surface.

Example:

```bash
curl -X POST http://localhost:8000/v1/continuity/brief \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "brief_type": "general",
    "thread_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "query": "deploy",
    "max_relevant_facts": 6,
    "max_recent_changes": 5,
    "max_open_loops": 5,
    "max_conflicts": 5,
    "max_timeline_highlights": 5
  }'
```

## CLI

After an editable install (`pip install -e '.[dev]'`), the `alice` entrypoint is available:

```bash
alice brief --brief-type general --query deploy
```

If you are running directly from the repository Python runtime:

```bash
./.venv/bin/python -m alicebot_api brief --brief-type general --query deploy
```

## MCP

The default MCP surface is four tools (see [docs/integrations/mcp.md](mcp.md)); `alice_resume` covers most continuity lookups there. `alice_context_pack` is full-surface. `alice_brief` requires a deliberately keyless local server with `ALICE_MCP_LEGACY_TOOLS=1`; a key-bound server hides and rejects it.

Example:

```json
{
  "name": "alice_brief",
  "arguments": {
    "brief_type": "coding_context",
    "thread_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "query": "deploy"
  }
}
```

## When To Use Other Surfaces

- use `alice_recall` (core) when you only need ranked facts
- use `alice_resume` (core) when you only need a resumption brief
- use `alice_task_brief` only on a deliberately keyless local legacy server with
  both `ALICE_MCP_LEGACY_TOOLS=1` and `ALICE_LEGACY_SURFACES=1` when you
  explicitly want deprecated persisted task-adaptive briefing output
