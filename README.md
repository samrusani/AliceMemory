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

Alice is a local-first memory service that lets AI agents resume interrupted work, track open loops, recall decisions with provenance, and improve when corrected — instead of re-reading transcripts or trusting opaque summaries.

A LongMemEval_s receipt of 81.2% mean over three independent full runs (80.8 / 81.0 / 81.8; 404-409 of 500) was measured 2026-07-18/19 on the published `v0.12.0` tag with the privileged `store_chunks` harness. That path reads chunks straight from the store. No MCP tool offers it, and it is not the product path. `pack_excerpts` is the product-path mode and is not yet a published score. Per-question evidence for all three runs, the reader/judge/embedding configuration, and the reproduction script are committed to this repo. Multi-session is the weakest category at roughly 63%. The 30-question abstention subset is noisy across runs (76.7 / 90.0 / 83.3) and should not be quoted to one decimal. The earlier single run of 79.4% (397/500) from 2026-07-07 is retained as evidence.

Agents connect over MCP, HTTP API, or CLI. Humans stay in control: agent writes land as policy-checked commits or reviewable proposals, and a local review console is where memory gets approved, corrected, or forgotten. That review boundary is a feature, not a limitation — it is what makes the memory trustworthy enough to act on.

## How Alice compares

Most agent memory tools — mem0, Zep, Letta, and similar — focus on extracting facts from conversations and retrieving them later. That solves recall, and they do it well. Alice focuses on continuity: it stores typed continuity objects (decisions, open loops, resumption briefs) alongside plain memories; source-backed answers trace to the evidence that was supplied; and writes are review-governed, so an agent cannot silently promote a bad extraction into durable truth. Explicit commits may legitimately have no source reference. If you mainly need conversational fact recall, those tools are solid choices. If your agents need to resume work, honor past decisions, and explain why they believe something, that is what Alice is built for.

Alice is a layer, not a lock-in: it runs happily alongside other memory tools, and plenty of stacks will want both — a fact-extraction memory for conversational recall and Alice for governed continuity.

## What Alice stores

- **Memories** — typed, revisioned facts with trust classification and, when
  evidence was supplied, provenance links to that source evidence.
- **Decisions** — what was decided, when, and what superseded it.
- **Open loops** — blockers, waiting-fors, and follow-ups that agents can query, create, and close.
- **Resumption briefs** — "here is where work stopped, and what should happen next" for a project or thread.
- **Provenance and audit** — source-backed memories identify their supplied
  sources; reviews and corrections preserve their audit chain. Explicit
  commits may legitimately have no source reference.

Corrections are first-class: when a memory is corrected or superseded, future recall reflects the correction and the explanation chain shows why.

## Quickstart

The fastest path is the packaged runtime from PyPI. Needs [uv](https://docs.astral.sh/uv/), which fetches Python for you. Or Python 3.12+ with pip. No Docker, Node or Postgres.

```bash
uvx alice-memory install --data-dir ~/.alice
# or, without uv: pip install alice-memory && alice-memory install --data-dir ~/.alice
```

That writes Claude Desktop, Claude Code, Cursor, and OpenClaw MCP config. Claude Code and Cursor also get a SessionStart hook so the next session can inject the brief. Without `uvx` on PATH, install writes the absolute path of the installed `alice-memory` script into each new host entry, and of `alice-memory-session-start` from the same directory into the hooks (it looks next to the Python running install, then in the user scripts directory, then on PATH, never inside uv's cache, and prints a warning if it finds neither uvx nor the scripts). Each hook runs the same launcher as its host's entry, uvx options included, and the receipt's `launcher:` lines say what each file runs. The hook command is shell-quoted, so a data dir with spaces or quotes is passed as one argument; on Windows, install does not write a hook whose paths hold a quote, `$`, a backtick, `%`, `!`, braces, brackets, a comma or a line break, and prints it to add by hand. On those four hosts, re-running install keeps the data dir and any keys you added to an existing Alice entry (env, timeout, a pinned or absolute `uvx`) unless you pass `--data-dir`, reading the entry's args with the server's own parser; replaces a launcher that no longer runs, or sits in uv's cache, with one that does and prints `launcher: old -> new`; backs up each host file into `<data dir>/backups/host-configs/` before rewriting it, and edits a symlinked file where the link points; and leaves alone an `alice` entry it did not write, or one whose `--data-dir` is a relative path: that host is reported, the other hosts are still written, and the exit code is 1. `--dry-run` prints only the Alice entry and hook. Values from your entry are shown as `<hidden>` except `command`, `type`, `timeout`, `cwd` and `args`; in `args`, every URL prints as its scheme and `<hidden>`, host included, and the value after a flag named with key, token, secret or password is hidden; a `hidden:` line lists each one. A hook's command prints only install's own words (uvx and the alice-memory programs, `--from` with a plain alice-memory spec, `--data-dir` and its value, and the carried uvx options); every other word prints as `<hidden>`. Install never writes a URL into a hook file and never prints one unmasked. An entry pinned below 0.16 (which has no `alice-memory-session-start`), or whose uvx options include anything but `--prerelease`, `--python` and `--python-preference` (such as an index, a URL, or `--with`), gets no new hook, and an existing hook keeps its command, though its `--data-dir` still follows the entry (a `--db` entry's hook keeps its own store). Hermes is opt-in: add `--host hermes`. For Hermes, install writes only the `mcp_servers.alice` lines in `~/.hermes/config.yaml` and keeps every other line byte for byte (an empty `mcp_servers: {}`, `~` or `null` becomes `mcp_servers:`, and a last line with no line break gets one when lines are added after it), after saving a timestamped backup in the same backup directory. On a re-run it keeps the existing entry's data dir, command and pinned args unless you pass `--data-dir`, which moves only the data dir. It also keeps a documented Alice env value when that value is a one-line plain, single-quoted, or double-quoted scalar: `ALICE_MCP_FULL_TOOLS`, `ALICE_MCP_LEGACY_TOOLS`, `ALICE_AGENT_API_KEY`, and `ALICE_LEGACY_SURFACES`. The receipt lists those keys and masks their values the same way as the other hosts. An entry with any other key install did not write is left alone. Install refuses while those keys are present, and the receipt says to edit the entry by hand. If the file uses YAML the installer does not edit, it changes nothing, prints the lines to add by hand, and exits non-zero. The command writes host config. It does not import a vault.

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

Optional, and useful once Alice is connected. [`agent-skills/`](https://github.com/samrusani/AliceMemory/tree/main/agent-skills)
holds a ready-made instruction pack for each host, telling the agent when to reach for
memory rather than leaving it to guess. Copy the directory, not the file:

```bash
cp -R agent-skills/openclaw/alice-project-memory ~/.openclaw/skills/
cp -R agent-skills/hermes/alice-memory ~/.hermes/skills/
```

Both hosts load `<skill-name>/SKILL.md` and read the frontmatter `description` to decide
when the skill applies. A skill grants no tools on its own; it tells an agent how to use
the ones the MCP server already provides.

SQLite mode is the single-agent path and the one most agents should use: it serves the default three tools for one user. Capture, the pack, and review are on the full surface (`ALICE_MCP_FULL_TOOLS=1`). Boundaries are listed in [known limitations](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/known-limitations.md).

> **Install note:** the PyPI package is [`alice-memory`](https://pypi.org/project/alice-memory/). The name `alice-core` on PyPI belongs to an unrelated project.

### Full stack (Postgres + review console)

For the full experience — Postgres/pgvector, the web review console, and core
memory scheduler workflows — run from a repo checkout. Requirements: Python
3.12+, Node 20+, pnpm, Docker, Git.

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

Open the review console at `http://localhost:3000/vnext`. The detailed walkthrough — demo data, smoke checks, first memory — is in [the alpha quickstart](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/quickstart.md).

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

- `alice_memory_commit` — **record one fact as durable, immediately recallable memory.** This is the verb for ordinary memory, including when the user has not asked the agent to remember. Policy-checked: committed, confirmation-required, review-required, or rejected
- `alice_recall` — search memory (full-text plus vector, fused ranking; hard-scopable by thread, task, project, person, time, and memory type). Also returns matching passages from captured documents under `sources`, with an excerpt to read and quote; `results` are facts Alice asserts, `sources` are material the user imported, and the same scope fence applies to both
- `alice_resume` — resumption brief for a project or thread

The other eight core tools (`alice_capture`, `alice_context_pack`, `alice_open_loops`, `alice_recent_decisions`, `alice_memory_review`, `alice_memory_correct`, `alice_memory_manage`, `alice_explain`) stay defined and become listed and callable when `ALICE_MCP_FULL_TOOLS=1`. Capture stores a source; candidates stay unsearchable as memories. Import is a source. Commit is a fact.

Calling directly from a human client (Claude Desktop, an IDE)? `alice_memory_commit` needs only `title` and `canonical_text` — no identity fields. Agent integrations declare `agent_id` and `agent_type`; see [agent integration](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/agent-integration.md).

The write verbs follow one contract. Outcomes, audit guarantees, and honest boundaries per verb are documented in the [Memory Operations Protocol](https://github.com/samrusani/AliceMemory/blob/main/docs/memory-operations-protocol.md). Removed backing services no longer have MCP tools. Retained long-tail memory tools require `ALICE_MCP_LEGACY_TOOLS=1` and append to whatever core set is enabled; exactly `alice_task_brief`, `alice_task_brief_show`, and `alice_task_brief_compare` additionally require `ALICE_LEGACY_SURFACES=1`. All legacy tools require a deliberately keyless local-operator deployment; a server bound with `ALICE_AGENT_API_KEY` exposes only the enabled core set.

Custom agents calling the HTTP API authenticate with per-agent API keys. See [agent integration](https://github.com/samrusani/AliceMemory/blob/main/docs/alpha/agent-integration.md).

### Embeddings

Semantic search works with any OpenAI-compatible embeddings endpoint — Ollama, LM Studio, or OpenAI:

```bash
ALICE_EMBEDDINGS_BASE_URL=http://localhost:11434/v1
ALICE_EMBEDDINGS_MODEL=nomic-embed-text
ALICE_EMBEDDINGS_API_KEY=            # only if the endpoint requires one
```

Search fuses Postgres full-text results with pgvector 0.8+ (iterative HNSW)
similarity using reciprocal-rank fusion. If no embedding endpoint is configured,
search degrades to full-text only and says so explicitly in the retrieval trace.

## Status

`v0.16.0` is the latest published release and remains the install, checksum,
and release-note baseline (the `v0.13.0` tag was never published;
superseded). Its tag, release record, and published artifacts
are immutable.
`v0.12.0` shipped the Phase 3 structural refactor with **Structure only. Zero
behavior change.** It splits oversized HTTP, store, contract, MCP, and CLI
modules behind stable imports and entrypoints.
The published `v0.11.0` runtime narrows the default product to the agent
interface and retrieval/memory core.
Alice is a public-alpha, pre-1.0 project.
What that means in practice:

- **Local-first, single-user.** One operator, one machine (or one headless server reached over SSH).
- **Review-governed writes.** Agents propose or commit through policy; outcomes are commit, confirm, review, or reject. The review console is the trust boundary for durable memory.
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
- [v0.10.4 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.10.4-release-notes.md)
- [v0.11.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.11.0-release-notes.md)
- [v0.11.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.11.1-release-notes.md)
- [v0.12.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.12.0-release-notes.md)
- [v0.15.6 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.15.6-release-notes.md)
- [v0.15.7 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.15.7-release-notes.md)
- [v0.16.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/docs/release/v0.16.0-release-notes.md)
- [Release procedure](https://github.com/samrusani/AliceMemory/blob/main/RELEASING.md)
- [Architecture](https://github.com/samrusani/AliceMemory/blob/main/ARCHITECTURE.md)
- [Roadmap](https://github.com/samrusani/AliceMemory/blob/main/ROADMAP.md)
- [Changelog](https://github.com/samrusani/AliceMemory/blob/main/CHANGELOG.md)

## Contributing

Issues, integrations, importers, and eval contributions are welcome. See [CONTRIBUTING.md](https://github.com/samrusani/AliceMemory/blob/main/CONTRIBUTING.md).

## Security

If you discover a security issue, follow the process in [SECURITY.md](https://github.com/samrusani/AliceMemory/blob/main/SECURITY.md).

## License

MIT — see [LICENSE](https://github.com/samrusani/AliceMemory/blob/main/LICENSE).

`v0.16.0` is the latest published release and remains the install, checksum,
and baseline reference.
