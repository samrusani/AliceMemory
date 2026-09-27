# Alice docs

Start with one command: `uvx alice-memory install`. It uses one local SQLite file and three MCP tools by default. The Postgres stack adds the `/vnext` review console, capture connectors and the scheduler. See the [quickstart](quickstart.md).

This folder keeps the legacy `docs/alpha` path name. Alice is a local-first package for people who want agent memory and continuity without hosted storage or direct database writes by agents. Alice is the continuity layer for AI agents.

Alice is agent-first, not dashboard-first:

1. Install Alice locally.
2. Connect an existing agent through MCP/API/CLI.
3. Let agents request scoped context packs, submit reviewable outputs, and commit only explicit user-directed memories through Alice policy.
4. On the Postgres stack, use `/vnext` to review, govern, audit, undo, correct, forget, configure, and troubleshoot. On SQLite, review runs through `alice_memory_review` and `alice_memory_correct` (`ALICE_MCP_FULL_TOOLS=1`).

Start here:

- [Quickstart](quickstart.md)
- [First-run checklist](first-run.md)
- [First memory guide](first-memory.md)
- [Local runtime](local-runtime.md)
- [Doctor](doctor.md)
- [Demo mode](demo-mode.md)
- [Review dashboard demo](review-dashboard-demo.md)
- [Headless Ubuntu install](headless-ubuntu-install.md)
- [Agent integration](agent-integration.md)
- [MCP tools](mcp-tools.md)
- [Hermes dogfood on Ubuntu](hermes-dogfood-ubuntu.md)
- [Hermes skill](hermes-skill.md)
- [OpenClaw skill](openclaw-skill.md)
- [Custom agent guide](custom-agent-guide.md)
- [Context-pack recipes](context-pack-recipes.md)
- [Memory proposal recipes](memory-proposal-recipes.md)
- [Agent output ingestion examples](agent-output-ingestion.md)
- [Dogfooding guide](dogfooding-guide.md)
- [Troubleshooting](troubleshooting.md)
- [Backup and restore](backup-and-restore.md)
- [Disaster recovery](../runbooks/disaster-recovery.md)
- [Health and monitoring](../runbooks/health-and-monitoring.md)
- [Upgrade v0.12.0 to current](../runbooks/upgrade-v0.12-to-current.md)
- [Known limitations](known-limitations.md)
- [Security and privacy](security-and-privacy.md)
- [Alpha onboarding](onboarding.md)
- [Postgres stack notes](release-notes.md)

Current alpha posture:

- local runtime, not hosted SaaS
- headless Ubuntu install path for SSH-only dogfood hosts
- one-command SQLite install; the Postgres stack is a technical setup
- reviewable source, artifact, and agent memory proposal flows
- explicit trusted-agent memory commits with confirmation/review/reject policy gates
- no direct Postgres writes by agents
- supported alpha connectors: local folder, browser clipper MVP, document text
  payloads, externally produced voice transcripts, externally extracted
  screenshot text, and agent output ingestion; Alice does not execute OCR or
  transcription
- `/vnext` is the operator console, not the main agent interface
