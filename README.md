# Alice

<!-- mcp-name: io.github.samrusani/alice-memory -->

**Long-term memory with permissions, for a company's teams of AI agents.**

Alice is a local-first memory service for teams of AI agents (sales, support, product) under one owner. Teams share what one of them learns, and a team's key decides what it may read. Writes land as policy-checked commits or reviewable proposals, and source-backed answers trace to the evidence that was supplied. Agents connect over MCP, HTTP API, or CLI (public alpha, one maintainer; see the [known limitations](docs/alpha/known-limitations.md)).

`python scripts/showcase_walkthrough.py` builds the demo company in about half a minute and prints a scripted meeting: teams recall what one learned, sales asks what changed and why, support is refused the HR pay and the draft price; it calls no model ([walkthrough](docs/showcase/walkthrough.md)).

## Quickstart

```bash
uvx alice-memory install --data-dir ~/.alice
uvx alice-memory demo --vault ~/Notes --data-dir ~/.alice-demo
```

![import then quote](docs/examples/alice-memory-demo.gif)

Install writes MCP config for Claude Desktop, Claude Code, Cursor and OpenClaw, plus SessionStart hooks; `--dry-run` writes nothing ([Install with alice-memory](docs/alpha/quickstart.md#install-with-alice-memory)). The PyPI package is [`alice-memory`](https://pypi.org/project/alice-memory/). The name `alice-core` on PyPI belongs to an unrelated project.

The default MCP surface is three tools:

- `alice_memory_commit`: record one fact as durable, immediately recallable memory; policy-checked into commit, confirmation, review, or rejection.
- `alice_recall`: search memory, fusing full-text, vector and entity-graph results; returns matching passages from captured documents under `sources`.
- `alice_resume`: resumption brief for a project.

## Measured

- Against capped grep, a note folder and no tool, with Claude on the independent company: retrieval questions, Alice 133 of 138, grep 133, the note folder 137; permission questions, Alice 65 of 66, grep 24; none of Alice's 1,524 answers leaked, and a shared folder search leaked in 13% to 27% ([Phase 0 baseline](docs/benchmarks/showcase/phase0-baseline.md)).
- With the smaller local model on the same company: Alice 99 of 138, grep 81, a 13 point difference (28 questions against 10, p 0.005); the note folder answers 92, no claim (p 0.31) ([Phase 0 baseline](docs/benchmarks/showcase/phase0-baseline.md)).
- On the hidden hard set with the smaller local model: Alice with passage ranking 38 questions against grep 18 (p 0.011) under the registered rule, and the connection task is a weak spot (grep 14 of 25, Alice 6) ([Phase 1 results for v0.21.0](docs/benchmarks/showcase/phase1-v0210-results.md)). With Claude on the hard set: Alice 165, grep 165, the note folder 172, no claim; the weak spot is change questions (Alice 33 of 40, grep 37, the note folder 38).
- Passage vectors, smaller local model: the first search finds a fact stated in other words more often (deep questions 21.1% against 13.8%, p 0.0015) and the exact-term floor does not hold; neither family answers more questions with vectors, and the note folder answers 105 of the 145 deep recall questions against 29 with vectors; there is no Claude file-search arm on that set ([passage vectors results](docs/benchmarks/showcase/phase1-passage-vectors-results.md)).

## Status

`v0.23.1` is the latest published release and remains the install, checksum, and release-note baseline. `v0.23.0` is the immediately prior published release. Pre-1.0; [Current state](CURRENT_STATE.md) lists the tags that were never published. To run v0.23.1, run `git checkout v0.23.1` before `make setup`.

## Benchmark

The published LongMemEval figures carry a known issue: the model answering each question could see which sessions held the evidence. [Alice on LongMemEval](docs/benchmarks/longmemeval/README.md) gives the figures and the method.

## Docs

- [Product overview](docs/overview.md), [agent integration](docs/alpha/agent-integration.md), [MCP tools](docs/alpha/mcp-tools.md), [changelog](CHANGELOG.md)
- [v0.23.1 release notes](docs/release/v0.23.1-release-notes.md), [all release notes](https://github.com/samrusani/AliceMemory/releases)

## Security

Security posture: automated security scanning and internal adversarial review, findings triaged and fixed. No one outside the project has audited the code. To report an issue, follow [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
