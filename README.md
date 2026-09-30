# Alice

<!-- mcp-name: io.github.samrusani/alice-memory -->

**The continuity layer for AI agents.**

```bash
uvx alice-memory install --data-dir ~/.alice
uvx alice-memory demo --vault ~/Notes --data-dir ~/.alice-demo
```

![import then quote](docs/examples/alice-memory-demo.gif)

`alice-memory doctor` prints sources, then candidates.

[![LongMemEval](https://img.shields.io/badge/LongMemEval-v0.12.0%20store__chunks%20receipt-6f42c1)](https://github.com/samrusani/AliceMemory/blob/main/docs/benchmarks/longmemeval/README.md)
![Local-first](https://img.shields.io/badge/local--first-core-0A7B61)
![MCP](https://img.shields.io/badge/MCP-supported-1f6feb)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-2ea043)

Alice is a local-first memory service for AI agents. It lets them resume interrupted work, track open loops, recall decisions with provenance, and improve when corrected. They do not have to re-read transcripts or trust opaque summaries.

Alice is a public alpha with one maintainer, and most of its code is written by AI coding agents ([how it is built](#how-it-is-built)). Read the [known limitations](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/known-limitations.md) before you rely on it.

Agents connect over MCP, HTTP API, or CLI. Humans stay in control. Agent writes land as policy-checked commits or reviewable proposals. On SQLite you approve, correct, or forget memory with the full tool set (`ALICE_MCP_FULL_TOOLS=1`). The Postgres stack adds a local review console.

## How Alice compares

Most agent memory tools, such as mem0, Zep, and Letta, focus on extracting facts from conversations and retrieving them later. That solves recall, and they do it well. Alice focuses on continuity: it stores typed continuity objects (decisions, open loops, resumption briefs) alongside plain memories; source-backed answers trace to the evidence that was supplied; and writes are review-governed, so an agent cannot silently promote a bad extraction into durable truth. Explicit commits may legitimately have no source reference. If you mainly need conversational fact recall, those tools are solid choices. If your agents need to resume work, honor past decisions, and explain why they believe something, that is what Alice is built for.

Alice runs alongside other memory tools. A stack can use a fact-extraction memory for conversational recall and Alice for governed continuity.

## What Alice stores

- **Memories**: typed, revisioned facts with trust classification and, when
  evidence was supplied, provenance links to that source evidence.
- **Decisions**: what was decided, when, and what superseded it.
- **Open loops**: blockers, waiting-fors, and follow-ups that agents can query, create, and close.
- **Resumption briefs**: "here is where work stopped, and what should happen next" for a project or thread.
- **Provenance and audit**: source-backed memories identify their supplied
  sources; reviews and corrections preserve their audit chain. Explicit
  commits may legitimately have no source reference.

Corrections are first-class: when a memory is corrected or superseded, future recall reflects the correction and the explanation chain shows why.

## Quickstart

The fastest path is the packaged runtime from PyPI. Needs [uv](https://docs.astral.sh/uv/), which fetches Python for you. Or Python 3.12+ with pip. No Docker, Node or Postgres.

```bash
uvx alice-memory install --data-dir ~/.alice
# or, without uv: pip install alice-memory && alice-memory install --data-dir ~/.alice
```

That writes Claude Desktop, Claude Code, Cursor, and OpenClaw MCP config. Claude Code and Cursor also get a SessionStart hook so the next session can inject the brief. Hermes is opt-in. `--host hermes` configures Hermes only, because any `--host` replaces the default set. To write those five, pass `--host claude-desktop --host claude-code --host cursor --host openclaw --host hermes`. Without `uvx` on PATH, install writes the path of the installed `alice-memory` scripts instead. It warns if it finds neither `uvx` nor those scripts. On Claude Desktop, Claude Code, Cursor and OpenClaw, a re-run keeps any keys you added to the Alice entry. On Hermes it keeps only the documented Alice env values and refuses while the entry has other keys. A re-run keeps your data dir unless you pass `--data-dir`. It backs up each file before it rewrites it. `--dry-run` prints the plan and writes nothing. The command writes host config. It does not import a vault. The details are in [Install with alice-memory](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/quickstart.md#install-with-alice-memory).

OpenCode is opt-in with `--host opencode`, which writes `opencode.json` or `opencode.jsonc`. See [OpenCode](https://github.com/samrusani/AliceMemory/blob/main/docs/integrations/opencode.md).

OpenClaw can also add the server in one line, which probes before saving:

```bash
openclaw mcp add alice --command uvx --arg alice-memory --arg mcp --arg --data-dir --arg ~/.alice
```

Or paste this into a host that still wants a file. There is no `DATABASE_URL`:

```json
{
  "mcpServers": {
    "alice": {
      "command": "uvx",
      "args": ["alice-memory", "mcp", "--data-dir", "/ABSOLUTE/PATH/TO/.alice"]
    }
  }
}
```

To serve MCP yourself after install:

```bash
uvx alice-memory mcp --data-dir ~/.alice
# or: pip install alice-memory && alice-memory mcp --data-dir ~/.alice
```

OpenClaw prefixes MCP tool names with the server name, so `alice_recall` reaches the model as `alice__alice_recall`.

#### Skill packs

Optional. Packs exist for 2 of the 6 install hosts, Hermes and OpenClaw.
Nothing in this repo measures whether a pack changes what an agent does.
[`agent-skills/hermes/alice-memory`](https://github.com/samrusani/AliceMemory/tree/main/agent-skills/hermes/alice-memory)
and [`agent-skills/openclaw/alice-project-memory`](https://github.com/samrusani/AliceMemory/tree/main/agent-skills/openclaw/alice-project-memory)
are the packs under `agent-skills/`. Copy the directory, not the file:

```bash
cp -R agent-skills/openclaw/alice-project-memory ~/.openclaw/skills/
cp -R agent-skills/hermes/alice-memory ~/.hermes/skills/
```

An older Hermes pack at
`docs/integrations/hermes-skill-pack/skills/alice-workflows/` is legacy. It
uses the manual `alice_core` server name plus full-surface tools.

Both hosts load `<skill-name>/SKILL.md` and read the frontmatter `description` to decide
when the skill applies. A skill grants no tools on its own; it tells an agent how to use
the ones the MCP server already provides.

SQLite mode is the single-agent path and the one most agents should use: it serves the default three tools for one user. Capture, the pack, and review are on the full surface (`ALICE_MCP_FULL_TOOLS=1`). Boundaries are listed in [known limitations](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/known-limitations.md).

> **Install note:** the PyPI package is [`alice-memory`](https://pypi.org/project/alice-memory/). The name `alice-core` on PyPI belongs to an unrelated project.

### Full stack (Postgres + review console)

For Postgres with pgvector, the web review console, and the core memory
scheduler workflows, run from a repo checkout. Requirements: Python 3.12+,
Node 20+, pnpm, Docker, Git.

The clone checks out `main`, which can be ahead of the latest release. To run v0.18.0, run `git checkout v0.18.0` before `make setup`.

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
make setup
make migrate
make doctor
make dev
```

- `make setup` creates `.env` files from checked-in examples and installs Python and web dependencies.
- `make migrate` starts local services (Postgres via Docker) and runs database migrations.
- `make doctor` runs readiness checks and applies safe fixes.
- `make dev` runs the API on port 8000 and the web review console on port 3000.

Open the review console at `http://localhost:3000/vnext`. The detailed walkthrough, with demo data, smoke checks, and a first memory, is in [the alpha quickstart](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/quickstart.md).

## Connect an agent

### MCP

Point any MCP-capable agent or IDE at the Alice server. For the packaged SQLite runtime, use the `uvx` config from the Quickstart above. For the full Postgres stack from a checkout:

```json
{
  "mcpServers": {
    "alice": {
      "command": "/ABSOLUTE/PATH/TO/AliceMemory/.venv/bin/python",
      "args": ["-m", "alicebot_api.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/TO/AliceMemory",
      "env": {
        "DATABASE_URL": "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot",
        "ALICEBOT_AUTH_USER_ID": "00000000-0000-0000-0000-000000000001"
      }
    }
  }
}
```

The default MCP surface is three tools:

- `alice_memory_commit`: **record one fact as durable, immediately recallable memory.** This is the verb for ordinary memory, including when the user has not asked the agent to remember. Policy-checked: committed, confirmation-required, review-required, or rejected
- `alice_recall`: search memory (full-text, vector and entity-graph results fused by rank, with vectors only when an embeddings endpoint is set; hard-scopable by thread, task, project, person, time, and memory type). Also returns matching passages from captured documents under `sources`, with an excerpt to read and quote; `results` are facts Alice asserts, `sources` are material the user imported, and the same scope fence applies to both
- `alice_resume`: resumption brief for a project or thread

The other eight core tools (`alice_capture`, `alice_context_pack`, `alice_open_loops`, `alice_recent_decisions`, `alice_memory_review`, `alice_memory_correct`, `alice_memory_manage`, `alice_explain`) stay defined and become listed and callable when `ALICE_MCP_FULL_TOOLS=1`. Capture stores a source; candidates stay unsearchable as memories. Import is a source. Commit is a fact.

On main, not yet released: folder-import receipt items name the file, a withheld ChatGPT title is counted, the Postgres doctor names `DELETE /v0/vnext/sources/{id}`, candidate capture withholds a token and committing that withheld text is refused, a flagged import line, message, or title is withheld and the rest is imported, an OpenCode dry run shows top-level booleans and numbers, Codex is opt-in with `--host codex`, and the Claude Code plugin directory is in the repo. In v0.18.0 those receipt items omit the file, that title is not counted, the doctor says `delete_source`, the candidate response echoes the token, capture stores a low-entropy AKIA-shaped key inside the file, `"enabled": false` prints as `<hidden>`, there is no Codex install, and there is no Claude Code plugin. On main, `--host codex` also writes a SessionStart hook to `hooks.json`, which Codex skips until you trust it once at "Hooks need review" or with `/hooks`. In v0.18.0 there is no Codex hook.

On main, not yet released: the session brief omits a superseded fact and a source line whose captured sentence was corrected or superseded later, while recall and the context pack keep that old passage and set `derived_memory_corrected`. In v0.18.0 the brief still prints the old sentence as a fact or a source line, and the excerpt is unmarked.

On main, not yet released: Codex's default mode no longer asks before `alice_recall`, `alice_resume`, `alice_context_pack`, `alice_recent_decisions`, `alice_explain`, `alice_memory_commit`, or `alice_capture`. `alice_memory_review`, `alice_memory_correct`, `alice_memory_manage`, and `alice_open_loops` are marked destructive. In v0.18.0 these tools declare no hints, so Codex asks before every call.

On main, not yet released: a long session-brief note is cut at 1,500 characters with a marker outside the quote, later short items still fit, and the brief stays under 9,500 characters. The reserve is UTF-16 code units and includes the caller's newline, so the brief is at most 9,499 minus that reserve. The hook's final cap drops whole trailing lines and does not cut inside a line. `alice-memory doctor` prints `N / 9500 characters`. A newest fact of about 50,000 UTF-8 bytes or more no longer empties the brief; its facts and loops still print. In v0.18.0 that note was dropped when it did not fit the token budget, a brief of many shorter lines could reach about 16,000 characters, the doctor line was a token estimate, and a fact that long made the hook print `{}`.

Calling directly from a human client (Claude Desktop, an IDE)? `alice_memory_commit` needs only `title` and `canonical_text`, with no identity fields. Agent integrations declare `agent_id` and `agent_type`; see [agent integration](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/agent-integration.md).

The write verbs follow one contract. Outcomes, audit guarantees, and honest boundaries per verb are documented in the [Memory Operations Protocol](https://github.com/samrusani/AliceMemory/blob/main/docs/memory-operations-protocol.md). Removed backing services no longer have MCP tools. Retained long-tail memory tools require `ALICE_MCP_LEGACY_TOOLS=1` and append to whatever core set is enabled; exactly `alice_task_brief`, `alice_task_brief_show`, and `alice_task_brief_compare` additionally require `ALICE_LEGACY_SURFACES=1`. All legacy tools require a deliberately keyless local-operator deployment; a server bound with `ALICE_AGENT_API_KEY` exposes only the enabled core set.

Custom agents calling the HTTP API authenticate with per-agent API keys. See [agent integration](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/agent-integration.md).

### Embeddings

Semantic search works with any OpenAI-compatible embeddings endpoint, such as Ollama, LM Studio, or OpenAI:

```bash
ALICE_EMBEDDINGS_BASE_URL=http://localhost:11434/v1
ALICE_EMBEDDINGS_MODEL=nomic-embed-text
ALICE_EMBEDDINGS_API_KEY=            # only if the endpoint requires one
```

Search fuses full-text, vector, and entity-graph results by reciprocal rank.
On SQLite that is FTS5 and cosine similarity. On Postgres it is full-text search
and pgvector 0.8+ (iterative HNSW). With no embeddings endpoint, search skips the
vector list and the retrieval trace says so.

## Benchmark

A LongMemEval_s receipt of 81.2% mean over three independent full runs (80.8 / 81.0 / 81.8; 404-409 of 500) was measured 2026-07-18/19 on the published `v0.12.0` tag with the privileged `store_chunks` harness. That path reads chunks straight from the store. No MCP tool offers it, and it is not the product path. `pack_excerpts` is the product-path mode and is not yet a published score. Per-question evidence for all three runs, the reader/judge/embedding configuration, and the reproduction script are committed to this repo. Multi-session is the weakest category at roughly 63%. The 30-question abstention subset is noisy across runs (76.7 / 90.0 / 83.3) and should not be quoted to one decimal. The earlier single run of 79.4% (397/500) from 2026-07-07 is retained as evidence.

## Status

`v0.18.0` is the latest published release and remains the install, checksum,
and release-note baseline (the `v0.13.0` tag was never published;
superseded). Its tag, release record, and published artifacts
are immutable. `v0.12.0` was the structural refactor release. Structure only. Zero behavior change.
Alice is a public-alpha, pre-1.0 project.
What that means in practice:

- **Local-first, single-user.** One operator, one machine (or one headless server reached over SSH).
- **Review-governed writes.** Agents propose or commit through policy; outcomes are commit, confirm, review, or reject. The review step is the trust boundary for durable memory.
- **No hosted service.** There is no cloud offering yet; you run Alice yourself.
- **No channels or bundled chat runtime.** Telegram, hosted administration,
  chief-of-staff/chat/model-pack features, and the public `/v0/responses` chat
  endpoint are not part of the current product. Retained `/v1/runtime/invoke`
  still uses internal durable response-job/provider machinery.
- **No managed OAuth or automatic polling.** Temporary manual-token Gmail and
  Calendar compatibility is unmounted by default behind
  `ALICE_LEGACY_SURFACES=1`; Alice does not provide managed consent or syncing.
- **No automatic capture from arbitrary conversation.** Durable memory comes from explicit commits, reviewable proposals, or captured sources, never from silent transcript mining.
- **No OCR or transcription execution.** Alice accepts text extracted by an
  external tool; it does not run OCR or transcription models.

## Docs

- [Quickstart walkthrough](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/quickstart.md)
- [Agent integration](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/agent-integration.md)
- [MCP tools](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/mcp-tools.md)
- [Custom agent guide](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/custom-agent-guide.md)
- [Known limitations](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/known-limitations.md)
- [Backup and restore](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/backup-and-restore.md)
- [Disaster recovery](https://github.com/samrusani/AliceMemory/blob/main/docs/runbooks/disaster-recovery.md)
- [Health and monitoring](https://github.com/samrusani/AliceMemory/blob/main/docs/runbooks/health-and-monitoring.md)
- [Upgrade v0.12.0 to current](https://github.com/samrusani/AliceMemory/blob/main/docs/runbooks/upgrade-v0.12-to-current.md)
- [Security and privacy](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/security-and-privacy.md)
- [v0.18.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.18.0-release-notes.md)
- [All release notes](https://github.com/samrusani/AliceMemory/releases)
- [Release procedure](https://github.com/samrusani/AliceMemory/blob/main/RELEASING.md)
- [Architecture](https://github.com/samrusani/AliceMemory/blob/main/ARCHITECTURE.md)
- [Roadmap](https://github.com/samrusani/AliceMemory/blob/main/ROADMAP.md)
- [Changelog](https://github.com/samrusani/AliceMemory/blob/main/CHANGELOG.md)

## How it is built

Most of the code is written by AI coding agents (Codex, Claude Code and Cursor's agent). One agent builds each change and another reviews it. A new test only counts once breaking the code on purpose makes it fail, and CI runs the installer against real, pinned versions of the agent hosts it writes config for. The maintainer sets the direction and publishes each release.

## Contributing

Issues, integrations, importers, and eval contributions are welcome. See [CONTRIBUTING.md](https://github.com/samrusani/AliceMemory/blob/main/CONTRIBUTING.md).

## Security

Security posture: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code.

If you discover a security issue, follow the process in [SECURITY.md](https://github.com/samrusani/AliceMemory/blob/main/SECURITY.md).

## License

MIT. See [LICENSE](https://github.com/samrusani/AliceMemory/blob/main/LICENSE).
