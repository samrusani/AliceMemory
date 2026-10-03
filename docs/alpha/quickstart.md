# Quickstart

This is the canonical local setup walkthrough for Alice. Other quickstart pages point here.

## Install with alice-memory

This is the default setup. Your memory lives in one local SQLite file. The `uvx` line needs [uv](https://docs.astral.sh/uv/), which fetches Python for you. The `pip` line needs Python 3.12 or later. No Docker, Node or Postgres.

```bash
uvx alice-memory install
# or, without uv: pip install alice-memory && alice-memory install
```

What install writes:

- An `alice` MCP entry for Claude Desktop, Claude Code, Cursor and OpenClaw. Other entries in those files are kept. The receipt prints each file's path.
- A SessionStart hook for Claude Code (`~/.claude/settings.json`) and Cursor (`~/.cursor/hooks.json`), so the next session can inject the brief. Claude Desktop and OpenClaw get no hook. Examples and the host config shapes are in [session-start hook examples](../examples/alice-memory-session-start.md).
- For OpenClaw, the receipt also prints an `openclaw mcp add alice ...` line you can run instead.
- Hermes is opt-in. `--host hermes` configures Hermes only, because any `--host` replaces the default set; pass each host you want to write them together. Install then writes only the `mcp_servers.alice` lines in `~/.hermes/config.yaml` and keeps the rest of the file. Hermes gets no hook. A comment inside the alice block stays when there is nothing to change; when there is, install refuses and prints the block, and v0.18.0 dropped the comment. If the file uses YAML the installer does not edit, install changes nothing, prints the lines to add by hand, and exits non-zero.
- OpenCode is opt-in with `--host opencode`, which writes `opencode.json` or `opencode.jsonc`. See [OpenCode](../integrations/opencode.md).
- Codex is opt-in with `--host codex`, which edits `~/.codex/config.toml` as text and writes a SessionStart hook to `~/.codex/hooks.json`. See [Codex](../integrations/codex.md). v0.18.0 has no `--host codex`.
- The Claude Code plugin in `plugins/alice-memory` installs from the `alicememory` marketplace: run `claude plugin marketplace add samrusani/AliceMemory`, then `claude plugin install alice-memory@alicememory`. If git is set to use SSH for GitHub and you have no key there, add `https://github.com/samrusani/AliceMemory.git` instead. If you already have a clone, `claude plugin marketplace add <path to the clone>` works too. Use the plugin or `--host claude-code`, not both. See [Claude Code plugin](../integrations/claude-code-plugin.md). v0.18.0 has no plugin.

The data dir:

- Alice keeps your memory in `memory.db` in the data dir. The MCP server creates it the first time it starts.
- Without `--data-dir`, install keeps the data dir an existing Alice entry uses, else `~/.alice`. Pass `--data-dir` to move it. The hooks follow the entry.
- A re-run keeps keys you added to an existing Alice entry, such as `env` and `timeout`, on the four JSON hosts. On Hermes, install refuses while the entry has keys it did not write.

Backups: before install rewrites an existing host file, it saves a copy in `<data dir>/backups/host-configs/`.

Add `--dry-run` to see the plan first. It prints each path, the Alice entry and the hook, and writes nothing. Values from your existing entry print as `<hidden>` except `command`, `type`, `timeout`, `cwd` and `args`, and except booleans and numbers on top-level keys. From v0.19.0 those print as they are on Claude Desktop, Claude Code, Cursor, OpenClaw and OpenCode; v0.18.0 hid them too. A value inside a map such as `env` stays hidden, whatever its type. In `args`, a URL prints as its scheme and `<hidden>`, and the value after a flag named with key, token, secret or password is hidden. A `hidden:` line lists each masked value.

Without uv: when `uvx` is not on PATH, install writes the absolute path of the installed `alice-memory` script into each new host entry, and of `alice-memory-session-start` into the hooks. If it finds neither a usable `uvx` nor those scripts, it prints a warning.

Install leaves a host file alone when it already has an `alice` entry that install did not write, or one whose `--data-dir` is a relative path. That host is reported, the other hosts are still written, and the exit code is 1.

What install does not do:

- It does not import a vault or create the database.
- It does not start the MCP server or any host.
- It does not write Hermes unless you pass `--host hermes`.
- It does not turn on the full tool surface. The server lists three tools by default: `alice_memory_commit`, `alice_recall` and `alice_resume`. Set `ALICE_MCP_FULL_TOOLS=1` in the entry's `env` for all eleven core tools.

Launcher selection, hook limits on Windows and on pinned entries, and the other install edge cases as of v0.17.0 are in the [v0.17.0 release notes](../release/v0.17.0-release-notes.md#install-and-host-config). That page is a dated record: where this page or a later CHANGELOG section says otherwise, they are current.

## Run the MCP server by hand (SQLite)

You can also run the MCP server yourself against the same SQLite file. This writes no host config. Straight from PyPI:

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

This is the same SQLite path, for one user: three MCP tools by default (`alice_memory_commit`, `alice_recall`, `alice_resume`). Capture, the pack, and review are on the full surface (`ALICE_MCP_FULL_TOOLS=1`). No review console, scheduler, or legacy surfaces. See [known limitations](known-limitations.md). The Postgres setup below adds the `/vnext` review console, capture connectors and the scheduler.

## Sleep proposals (SQLite)

`alice-memory sleep` writes proposals for sources that have no memory yet, oldest first, to `sleep_proposals.jsonl` next to `memory.db`. At most eight proposals count toward the cap at a time. It creates no memory and changes no source or fact. A proposal stops counting once its source has an active or accepted memory; the row stays in the file. A source whose excerpt holds credential material is not proposed.

`alice-memory sleep-proposals` lists the proposals oldest first, framed and JSON-quoted like the session brief, each with the `alice_memory_commit` arguments that accept it, including the source's domain, sensitivity and project scope. It skips a source that already has an active or accepted memory and writes nothing. To accept one, an agent or you calls `alice_memory_commit` with those arguments; the sidecar is not edited.

`alice-memory doctor` prints a `sleep proposals: <n>` line. Both commands take `--data-dir` and `--db`.

Unreleased (on main, not in v0.20.0): `alice-memory sleep-proposals` also takes `--scope` and `--project-dir`; see [Projects](projects.md).

## Requirements

The rest of this page sets up the Postgres stack. It needs:

- Python 3.12+
- Node 20+
- pnpm
- Docker Desktop or compatible Docker engine
- Git

## Setup

The clone checks out `main`, which can be ahead of the latest release. To run v0.20.0, run `git checkout v0.20.0` before `make setup`.

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
- with `ALICE_MCP_FULL_TOOLS=1`, use the `alice_capture` MCP tool to submit new information as source-backed, reviewable memory
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
