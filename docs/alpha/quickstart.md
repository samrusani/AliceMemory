# Quickstart

This is the canonical local setup walkthrough for Alice. Other quickstart pages point here.

## Zero-Infrastructure Trial (SQLite)

To try Alice before setting up the full stack, run the MCP server against a single local SQLite file. The `uvx` line needs [uv](https://docs.astral.sh/uv/), which fetches Python for you; the `pip` line needs Python 3.12+. No Docker, Node or Postgres. Straight from PyPI:

```bash
uvx alice-memory mcp --data-dir ~/.alice
# or: pip install alice-memory && alice-memory mcp --data-dir ~/.alice
```

Working from a repo checkout instead? Install into a virtualenv first (a bare `pip install -e .` fails on PEP 668-managed systems):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
alice-memory mcp --data-dir ~/.alice
```

This is the trial and single-agent path: the default three MCP tools for one user (`alice_memory_commit`, `alice_recall`, `alice_resume`). Capture, the pack, and review are on the full surface (`ALICE_MCP_FULL_TOOLS=1`). No review console, scheduler, or legacy surfaces. See [known limitations](known-limitations.md). The Postgres setup below remains the full experience.

## Requirements

- Python 3.12+
- Node 20+
- pnpm
- Docker Desktop or compatible Docker engine
- Git

## Setup

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
make setup
make migrate
make doctor
```

Expected success:

- Python dependencies install into `.venv`
- `.env`, `.env.lite`, and `apps/web/.env.local` are created from the checked-in examples when missing
- web dependencies install under `apps/web`
- Docker services start
- migrations finish
- doctor returns `pass` or a warning without blocking failures

If port 5432 is already taken by another local Postgres, stop it or change the mapped port before `make migrate` (see the comments in `.env.example`).

## Start Alice

```bash
make dev
```

This runs the API on port 8000 and the web review console on port 3000.

For day-to-day use without the development file watcher, `make runtime` builds the web app once and serves it with lower idle CPU. Use `make dev` when editing the web UI.

Open:

```text
http://localhost:3000/vnext
```

Local live use needs explicit browser/API settings. Keep both frontend origins in the API CORS allowlist and keep the browser API URL pointed at localhost:

```dotenv
CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000
NEXT_PUBLIC_ALICEBOT_USER_ID=00000000-0000-0000-0000-000000000001
```

Use the same user id as `ALICEBOT_AUTH_USER_ID`.

### Authenticate the review console after provisioning a key

The local API remains keyless only until the first active agent key exists. After that point,
every `/v0/vnext` request requires Bearer authentication, including requests from the review
console. Create a dedicated, unbound operator key (omit any project binding):

```bash
alicebot agent keys create --agent-id vnext-operator --profile admin_agent --label "Local review console"
```

The raw `alice_sk_...` value is printed once. Open `http://localhost:3000/vnext`, paste it into
**Unbound admin_agent API key**, and select **Use key for this session**. The browser holds the key
only in memory for the mounted console, clears it when the field is edited, cleared, or unmounted,
and forwards it only to loopback `/v0/vnext` requests. It is not loaded from an environment
variable or stored in local storage, a URL, logs, or errors. `trusted_local_agent` is insufficient
for the full human/admin review surface.

The browser-clipper bookmarklet deliberately cannot receive or prompt for this key because it runs
inside the visited page. From the authenticated Alice console, enter the page URL and choose
**Issue and copy one-time bookmarklet**. Alice binds a short-lived, single-use capability to that
page origin; the reusable agent key and any configured `capture_token` remain in trusted clients.
Prepare a fresh bookmarklet for every clip and verify the result in the Inbox.

## Configure Embeddings (Recommended)

Semantic search uses any OpenAI-compatible embeddings endpoint (Ollama, LM Studio, OpenAI). Set in `.env`:

```dotenv
ALICE_EMBEDDINGS_BASE_URL=http://localhost:11434/v1
ALICE_EMBEDDINGS_MODEL=nomic-embed-text
ALICE_EMBEDDINGS_API_KEY=
```

Without an embedding endpoint, search runs full-text only and the retrieval trace says so explicitly.

## First Smoke

```bash
alicebot vnext smoke operator-console
alicebot vnext smoke local-cors
alicebot vnext smoke agent-integration-pack
alicebot vnext smoke agentic-memory-commit
alicebot vnext alpha check
```

If `alicebot` is not on your shell path, use:

```bash
./.venv/bin/alicebot vnext alpha check
```

## First Memory

If Alice starts correctly but no memory appears after normal chat, follow the [first memory guide](first-memory.md).

Short version:

- use `alice_memory_commit` for explicit "remember/save this" requests — policy-checked, never a silent write
- use the core `alice_capture` MCP tool to submit new information as source-backed, reviewable memory
- use `alicebot vnext sources capture-text "Fact: ..."` for source-backed candidate memory
- do not expect arbitrary conversation to become trusted memory automatically

## First Daily Brief

Capture source evidence and generate a brief:

```bash
alicebot vnext sources capture-text "TODO: confirm launch checklist owner" --domain project --sensitivity private
alicebot daily-brief --generate --domain project
```

The generated artifact appears in the review console under Generated, with provenance back to the captured source.

## Load Safe Demo Data

```bash
alicebot vnext demo load --reset
```

Expected success:

- synthetic sources appear in the Inbox
- candidate memories appear in Memory Review
- generated artifacts appear in Generated
- Agent Activity shows demo agent activity and a restricted-domain policy block
- Trace shows source-to-artifact provenance

Reset the demo:

```bash
alicebot vnext demo reset
```

## Optional: Local Capture Connectors

Local, review-only capture paths that need no managed OAuth or account syncing:

```bash
# scan a local Markdown/text folder
alicebot vnext connectors local-folder add-path ~/Notes/Alice --extension .md --extension .txt
alicebot vnext connectors local-folder sync

# check connector health
alicebot vnext connectors health
```

All connector output lands as reviewable source evidence, never as automatic
trusted memory. See the [dogfooding guide](dogfooding-guide.md) for local-folder
and browser-clip capture.

## Verify Your Setup

The core checks used before release:

```bash
./.venv/bin/python -m pytest tests/unit -q
pnpm --dir apps/web test
pnpm --dir apps/web lint
pnpm --dir apps/web build
python3 scripts/check_control_doc_truth.py
git diff --check
```

## Next Steps

- Connect an agent: [agent integration](agent-integration.md) and [MCP tools](mcp-tools.md)
- Headless server install: [headless Ubuntu install](headless-ubuntu-install.md)
- What is intentionally not included: [known limitations](known-limitations.md)
