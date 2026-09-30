# Quickstart (Pointer)

The canonical setup walkthrough lives at [docs/alpha/quickstart.md](../alpha/quickstart.md).

The default setup is one command, `uvx alice-memory install`. It uses one local SQLite file and needs no Docker or Postgres.

For the Postgres stack, the short version is below. The clone checks out `main`, which can be ahead of the latest release. To run v0.19.0, run `git checkout v0.19.0` before `make setup`.

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
make setup
make migrate
make doctor
make dev
```

Then open `http://localhost:3000/vnext`.

For specific follow-ups:

- First memory: [docs/alpha/first-memory.md](../alpha/first-memory.md)
- Agent integration and MCP: [docs/alpha/agent-integration.md](../alpha/agent-integration.md), [docs/alpha/mcp-tools.md](../alpha/mcp-tools.md)
- Local capture connectors: [docs/alpha/dogfooding-guide.md](../alpha/dogfooding-guide.md) and [docs/runbooks/vnext-dogfood-daily-checklist.md](../runbooks/vnext-dogfood-daily-checklist.md)
- Architecture: [architecture.md](architecture.md)
