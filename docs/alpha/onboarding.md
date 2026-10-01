# Alpha Onboarding Guide

This alpha is for technical users who can run Alice locally and connect an
existing agent through MCP, HTTP, or CLI. It is not a design-partner or hosted
onboarding program.

## What To Test

- fresh setup from the README
- doctor-first onboarding
- one explicit local capture path
- source and candidate-memory review
- recall, resume, context-pack, and provenance explanation
- correction and memory-management lifecycle
- a core scheduler artifact when that workflow is relevant
- a core MCP/HTTP/CLI agent integration
- restricted-domain and project-scope policy boundaries

## Install

The clone checks out `main`, which can be ahead of the latest release. To run
v0.19.2, run `git checkout v0.19.2` before `make setup`.

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
cp .env.example .env
make setup
make migrate
make doctor
make dev
```

## Connect An Agent

Start the MCP server. It lists three tools by default. Set
`ALICE_MCP_FULL_TOOLS=1` to list all eleven core tools:

```bash
alicebot-mcp
# or, with all eleven core tools:
ALICE_MCP_FULL_TOOLS=1 alicebot-mcp
```

Use [mcp-tools.md](mcp-tools.md), [agent-integration.md](agent-integration.md),
[hermes-skill.md](hermes-skill.md), [openclaw-skill.md](openclaw-skill.md), or
the [custom agent guide](custom-agent-guide.md).

## Capture Explicit Evidence

Local folder:

```bash
alicebot vnext connectors local-folder add-path ~/Notes/Alice --extension .md --extension .txt
alicebot vnext connectors local-folder sync
```

Browser clip:

```bash
alicebot vnext connectors browser-clipper capture \
  --url https://example.test/note \
  --selected-text "Fact: demo source" \
  --domain project \
  --sensitivity private
```

Screenshot and audio workflows must extract text with an external OCR or
transcription tool before submitting the text to Alice.

## Feedback

Include:

- `alicebot vnext doctor --fix-safe --ci`
- the failing command and sanitized output
- a browser-console screenshot only when the review console is involved
- expected versus actual behavior
- whether the failure affects MCP, HTTP, CLI, PostgreSQL, or SQLite

Do not include secrets, private source exports, or customer data.
