# Reference Agent Examples

These examples show the narrow default adoption path for a generic external agent: call Alice one time through `POST /v1/continuity/brief`, then use the returned bundle inside your runtime.

The examples use a keyless loopback install. On main (unreleased), installs with
active keys serve `/v1` only to unbound `admin_agent` keys. Other keys use
`/v0/vnext` or MCP.

## Files

- Python: `docs/examples/generic_python_agent.py`
- TypeScript: `docs/examples/generic_typescript_agent.ts`

Both examples:

- call `POST /v1/continuity/brief`
- accept `ALICE_API_BASE_URL` and `ALICE_USER_ID`
- optionally accept `ALICE_THREAD_ID`, `ALICE_QUERY`, and `ALICE_BRIEF_TYPE`
- print a compact JSON summary that a host runtime can consume directly

## Runtime Notes

- the Python example runs with the repository virtualenv shown below
- the TypeScript example assumes a Node runtime that supports `--experimental-strip-types`

## Run The Python Example

```bash
ALICE_API_BASE_URL="http://127.0.0.1:8000" \
ALICE_USER_ID="00000000-0000-0000-0000-000000000001" \
./.venv/bin/python docs/examples/generic_python_agent.py
```

## Run The TypeScript Example

```bash
ALICE_API_BASE_URL="http://127.0.0.1:8000" \
ALICE_USER_ID="00000000-0000-0000-0000-000000000001" \
node --experimental-strip-types docs/examples/generic_typescript_agent.ts
```

## Reproducible Demo

Run both examples against the shipped response contract demo server:

```bash
./.venv/bin/python scripts/run_reference_agent_examples_demo.py
```

The demo serves a shared canonical continuity-brief fixture from `fixtures/reference_integrations/continuity_brief_agent_handoff_v1.json` so the example output stays tied to a checked-in contract shape instead of an ad hoc inline payload.

Expected JSON output:

- `status` = `pass`
- `python_example.returncode` = `0`
- `typescript_example.returncode` = `0`
- both example outputs include the same `brief_type`, `summary`, and `next_suggested_action`

## When To Use Something Else

- use the MCP surface instead of HTTP when your host runtime is already speaking MCP: the core `alice_resume`/`alice_context_pack` tools cover most continuity lookups; `alice_brief` is available only on a deliberately keyless local legacy server with `ALICE_MCP_LEGACY_TOOLS=1`
- use `docs/integrations/hermes.md` when Hermes owns orchestration
- use `docs/integrations/openclaw.md` when imported OpenClaw memory is part of the adoption path
