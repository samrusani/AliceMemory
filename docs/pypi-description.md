# Alice Memory

Alice is the continuity layer for AI agents. It gives agents a local-first
memory system with provenance, review-governed writes, open-loop tracking,
retrieval, resumption, and explainability across HTTP, CLI, and MCP surfaces.

## Install

With uv:

```bash
uvx alice-memory install
```

Without uv, with Python 3.12 or later:

```bash
pip install alice-memory && alice-memory install
```

Either way, `alice-memory install` writes the MCP config for Claude Desktop,
Claude Code, Cursor, and OpenClaw. Hermes is opt-in: add `--host hermes`.

The package installs the `alice-memory`, `alice`, `alicebot`, `alicebot-mcp`,
and `alice-memory-session-start` commands.

## Learn more

- [Website](https://www.alicememory.com)
- [Source and documentation](https://github.com/samrusani/AliceMemory)
- [Release history](https://github.com/samrusani/AliceMemory/releases)
- [Issue tracker](https://github.com/samrusani/AliceMemory/issues)

Alice is evolving software. Review the project documentation and release
history before deploying it in a production workflow.
