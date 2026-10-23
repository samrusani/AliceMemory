# Alice

<!-- mcp-name: io.github.samrusani/alice-memory -->

**The continuity layer for AI agents.**

```bash
uvx alice-memory install --data-dir ~/.alice
uvx alice-memory demo --vault ~/Notes --data-dir ~/.alice-demo
```

![import then quote](docs/examples/alice-memory-demo.gif)

`alice-memory doctor` prints sources, then candidates.

![Local-first](https://img.shields.io/badge/local--first-core-0A7B61)
![MCP](https://img.shields.io/badge/MCP-supported-1f6feb)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-2ea043)

Alice is a local-first memory service for AI agents. It lets them resume interrupted work, track open loops, recall decisions with provenance, and improve when corrected. They do not have to re-read transcripts or trust opaque summaries.

Alice is a public alpha with one maintainer, and most of its code is written by AI coding agents ([how it is built](#how-it-is-built)). Read the [known limitations](docs/alpha/known-limitations.md) before you rely on it.

Agents connect over MCP, HTTP API, or CLI. Humans stay in control. Agent writes land as policy-checked commits or reviewable proposals. On SQLite you approve, correct, or forget memory with the full tool set (`ALICE_MCP_FULL_TOOLS=1`). The Postgres stack adds a local review console.

## How Alice compares

Most agent memory tools, such as mem0, Zep, and Letta, focus on extracting facts from conversations and retrieving them later. Alice focuses on continuity: it stores typed continuity objects (decisions, open loops, resumption briefs) alongside plain memories; source-backed answers trace to the evidence that was supplied; and writes go through policy: memories extracted from captured or imported text stay candidates until someone reviews them, and a fact an agent commits directly is policy-checked and recorded with the writer's identity, so it can be traced and corrected. Confirming a pending write relies on the agent asking the user; it is not a human gate. Explicit commits may legitimately have no source reference. If you mainly need conversational fact recall, those tools are solid choices. If your agents need to resume work, honor past decisions, and explain why they believe something, that is what Alice is built for.

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

That writes Claude Desktop, Claude Code, Cursor, and OpenClaw MCP config, plus a SessionStart hook for Claude Code and Cursor so the next session can inject the brief. It backs up each file before it rewrites it, and `--dry-run` prints the plan and writes nothing. It does not import a vault. See [Install with alice-memory](docs/alpha/quickstart.md#install-with-alice-memory).

Any `--host` replaces the default set, so name every host you want written.

Hermes is opt-in with `--host hermes`, which edits `config.yaml` in Hermes's home. See [Hermes](docs/integrations/hermes.md).

OpenCode is opt-in with `--host opencode`, which writes `opencode.json` or `opencode.jsonc`. See [OpenCode](docs/integrations/opencode.md).

Codex is opt-in with `--host codex`, which edits `config.toml` in Codex's home and adds a SessionStart hook that Codex skips until you trust it once. See [Codex](docs/integrations/codex.md).

The Claude Code plugin in `plugins/alice-memory` installs from the `alicememory` marketplace:

```bash
claude plugin marketplace add samrusani/AliceMemory
claude plugin install alice-memory@alicememory
```

The marketplace file on `main` pins the plugin to the v0.21.1 tag commit, so add it from `main`, not from a checkout of a tag. See [Claude Code plugin](docs/integrations/claude-code-plugin.md), which lists what each tag pins.

For a host that wants a config file, paste this (no `DATABASE_URL`):

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

> **Install note:** the PyPI package is [`alice-memory`](https://pypi.org/project/alice-memory/). The name `alice-core` on PyPI belongs to an unrelated project.

### Full stack (Postgres + review console)

For Postgres with pgvector, the web review console, and the core memory scheduler workflows, run from a repo checkout. Requirements: Python 3.12+, Node 20+, pnpm, Docker, Git. The clone checks out `main`, which can be ahead of the latest release. To run v0.21.1, run `git checkout v0.21.1` before `make setup`.

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
make setup
make migrate
make doctor
make dev
```

`make dev` runs the API on port 8000 and the review console at `http://localhost:3000/vnext`. The [alpha quickstart](docs/alpha/quickstart.md) has the full walkthrough, with demo data and smoke checks.

## Connect an agent

### MCP

Point any MCP-capable agent or IDE at the Alice server. For the packaged SQLite runtime, use the `uvx` config from the Quickstart. For the full Postgres stack from a checkout, use the config in [agent integration](docs/alpha/agent-integration.md).

The default MCP surface is three tools:

- `alice_memory_commit`: **record one fact as durable, immediately recallable memory.** Policy-checked: committed, confirmation-required, review-required, or rejected.
- `alice_recall`: search memory, with full-text, vector and entity-graph results fused by rank. Also returns matching passages from captured documents under `sources`.
- `alice_resume`: resumption brief for a project or thread.

The other core tools (`alice_capture`, `alice_context_pack`, `alice_open_loops`, `alice_recent_decisions`, `alice_memory_review`, `alice_memory_correct`, `alice_memory_manage`, `alice_explain`) stay defined and become listed and callable when `ALICE_MCP_FULL_TOOLS=1`. From v0.21.0, `alice_what_changed` is one of the other nine core tools, and v0.20.2 does not have it. Capture and import store sources; commit stores a fact.

Calling directly from a human client (Claude Desktop, an IDE)? `alice_memory_commit` needs only `title` and `canonical_text`, with no identity fields. Agent integrations declare `agent_id` and `agent_type`, and custom agents calling the HTTP API use per-agent API keys; see [agent integration](docs/alpha/agent-integration.md), which also covers the optional [skill packs](docs/alpha/agent-integration.md#skill-packs). Every tool is listed in [MCP tools](docs/alpha/mcp-tools.md). The retained legacy tools need `ALICE_MCP_LEGACY_TOOLS=1` and a keyless local-operator server; see the [MCP guide](docs/integrations/mcp.md).

### Embeddings

Semantic search works with any OpenAI-compatible embeddings endpoint, such as Ollama, LM Studio, or OpenAI:

```bash
ALICE_EMBEDDINGS_BASE_URL=http://localhost:11434/v1
ALICE_EMBEDDINGS_MODEL=nomic-embed-text
ALICE_EMBEDDINGS_API_KEY=            # only if the endpoint requires one
```

Alice embeds only memories that recall can return, and cuts each memory and each query to its first 8,000 characters; `ALICE_EMBEDDINGS_MAX_INPUT_CHARS` changes that cap. Search fuses full-text, vector, and entity-graph results by reciprocal rank: FTS5 and cosine similarity on SQLite, full-text search and pgvector 0.8+ (iterative HNSW) on Postgres. With no embeddings endpoint, search skips the vector list and the retrieval trace says so. Details are in [MCP tools](docs/alpha/mcp-tools.md#embeddings).

## Benchmark

The published LongMemEval figures carry a known issue: the model answering each question could see which sessions held the evidence. [Alice on LongMemEval](docs/benchmarks/longmemeval/README.md) gives the figures, the method and the issue. The [showcase baseline](docs/benchmarks/showcase/phase0-baseline.md) compares Alice with grep, a note folder and no tool on two question sets, measured on main after v0.20.2. The [Phase 1 results for v0.21.0](docs/benchmarks/showcase/phase1-v0210-results.md) compare passage ranking with the earlier document ranking on those sets and a harder one.

## Status

`v0.21.1` is the latest published release and remains the install, checksum, and release-note baseline. `v0.21.0` is the immediately prior published release. [Current state](CURRENT_STATE.md) lists the tags that were never published. Alice is a public-alpha, pre-1.0 project:

- **Local-first, single-user.** One operator, one machine (or one headless server reached over SSH).
- **Review-governed writes.** Agents propose or commit through policy; outcomes are commit, confirm, review, or reject. The review step is the trust boundary for durable memory.
- **No hosted service.** There is no cloud offering yet; you run Alice yourself.
- **No channels or bundled chat runtime.** Telegram, hosted administration, chief-of-staff and chat features, and the public `/v0/responses` endpoint are not part of the current product.
- **No managed OAuth or automatic polling.** Gmail and Calendar compatibility is manual-token only, and unmounted by default behind `ALICE_LEGACY_SURFACES=1`.
- **No automatic capture from arbitrary conversation.** Durable memory comes from explicit commits, reviewable proposals, or captured sources, never from silent transcript mining.
- **No OCR or transcription execution.** Alice accepts text extracted by an external tool.

## Docs

- [Quickstart walkthrough](docs/alpha/quickstart.md)
- [Agent integration](docs/alpha/agent-integration.md)
- [MCP tools](docs/alpha/mcp-tools.md)
- [Custom agent guide](docs/alpha/custom-agent-guide.md)
- [Known limitations](docs/alpha/known-limitations.md)
- [Backup and restore](docs/alpha/backup-and-restore.md)
- [Single-tenant deployment](docs/deployment/single-tenant-self-hosted.md)
- [Disaster recovery](docs/runbooks/disaster-recovery.md)
- [Health and monitoring](docs/runbooks/health-and-monitoring.md)
- [Upgrade v0.12.0 to current](docs/runbooks/upgrade-v0.12-to-current.md)
- [Security and privacy](docs/alpha/security-and-privacy.md)
- [v0.21.1 release notes](docs/release/v0.21.1-release-notes.md)
- [All release notes](https://github.com/samrusani/AliceMemory/releases)
- [Release procedure](RELEASING.md)
- [Architecture](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Changelog](CHANGELOG.md)

## How it is built

Most of the code is written by AI coding agents (Codex, Claude Code and Cursor's agent). One agent builds each change and another reviews it. A new test only counts once breaking the code on purpose makes it fail, and CI runs the installer against real, pinned versions of four of the seven agent hosts it writes config for. Claude Desktop, Cursor and OpenClaw are not run against a real host in CI. The maintainer sets the direction and publishes each release.

## Contributing

Issues, integrations, importers, and eval contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Security posture: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code.

If you discover a security issue, follow the process in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
