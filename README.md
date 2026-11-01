# Alice

<!-- mcp-name: io.github.samrusani/alice-memory -->

**Long-term memory with permissions, for a company's teams of AI agents.**

Alice is a local-first memory service for a company's teams of AI agents (sales, support, product) under one owner. Teams share what one of them learns, and a team's key decides what it may read. Writes land as policy-checked commits or reviewable proposals, and source-backed answers trace to the evidence that was supplied. Agents connect over MCP, HTTP API, or CLI. Public alpha, one maintainer; read the [known limitations](docs/alpha/known-limitations.md) first.

```bash
uvx alice-memory install --data-dir ~/.alice
uvx alice-memory demo --vault ~/Notes --data-dir ~/.alice-demo
```

![import then quote](docs/examples/alice-memory-demo.gif)

`python scripts/showcase_walkthrough.py` builds the demo company in about half a minute and prints a recorded meeting: teams recall what one learned, sales asks what changed and why, support is refused the HR pay and the draft price. It calls no model ([the walkthrough page](docs/showcase/walkthrough.md)).

## Quickstart

```bash
uvx alice-memory install --data-dir ~/.alice
# or, without uv: pip install alice-memory && alice-memory install --data-dir ~/.alice
```

Install writes MCP config for Claude Desktop, Claude Code, Cursor and OpenClaw, plus SessionStart hooks; `--dry-run` writes nothing ([Install with alice-memory](docs/alpha/quickstart.md#install-with-alice-memory)). The PyPI package is [`alice-memory`](https://pypi.org/project/alice-memory/). The name `alice-core` on PyPI belongs to an unrelated project.

The default MCP surface is three tools:

- `alice_memory_commit`: **record one fact as durable, immediately recallable memory.** Policy-checked: committed, confirmation-required, review-required, or rejected.
- `alice_recall`: search memory, with full-text, vector and entity-graph results fused by rank. Also returns matching passages from captured documents under `sources`.
- `alice_resume`: resumption brief for a project or thread.

The other core tools (`alice_capture`, `alice_context_pack`, `alice_open_loops`, `alice_recent_decisions`, `alice_memory_review`, `alice_memory_correct`, `alice_memory_manage`, `alice_explain`) stay defined and become listed and callable when `ALICE_MCP_FULL_TOOLS=1`. From v0.21.0, `alice_what_changed` is one of the other nine core tools. OpenClaw prefixes MCP tool names with the server name, so `alice_recall` reaches the model as `alice__alice_recall`. For the full Postgres stack from a checkout, use the config in [agent integration](docs/alpha/agent-integration.md) ([skill packs](docs/alpha/agent-integration.md#skill-packs)). Embeddings take any OpenAI-compatible endpoint; the cap is in [MCP tools](docs/alpha/mcp-tools.md#embeddings).

## Measured

- Against grep, a note folder and no tool: with Claude, plain file search answers as many retrieval questions as Alice; the gain is permissions: none of 1,524 Alice answers leaked, a shared folder search leaked in 13% to 27% ([Phase 0 baseline](docs/benchmarks/showcase/phase0-baseline.md)).
- With the smaller local model, Alice answers 13 points more than capped grep (28 against 10, p 0.005) ([Phase 0 baseline](docs/benchmarks/showcase/phase0-baseline.md)).
- On the hidden hard set, Alice with passage ranking answers more than capped grep with both models under the registered rules; the connection task stays a weak spot (grep 14 of 25, Alice 6) ([Phase 1 results for v0.21.0](docs/benchmarks/showcase/phase1-v0210-results.md)).
- Passage vectors make the first search find a fact stated in other words more often and cost exact-term lookups; the note folder answers 105 of 145 deep recall questions against 29 with vectors ([passage vectors results](docs/benchmarks/showcase/phase1-passage-vectors-results.md)).

## Status

`v0.23.1` is the latest published release and remains the install, checksum, and release-note baseline. Public alpha, pre-1.0, one maintainer; [Current state](CURRENT_STATE.md) lists the tags that were never published. To run v0.23.1, run `git checkout v0.23.1` before `make setup`.

## Benchmark

The published LongMemEval figures carry a known issue: the model answering each question could see which sessions held the evidence. [Alice on LongMemEval](docs/benchmarks/longmemeval/README.md) gives the figures and the method.

## Docs

[Product overview](docs/overview.md), [agent integration](docs/alpha/agent-integration.md), [MCP tools](docs/alpha/mcp-tools.md), [v0.23.1 release notes](docs/release/v0.23.1-release-notes.md), [all release notes](https://github.com/samrusani/AliceMemory/releases), [changelog](CHANGELOG.md).

## Security

Security posture: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code. To report an issue, follow [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
