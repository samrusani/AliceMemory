# Postgres Stack Notes

Audience: technical users and agent builders.

This file stays under the legacy `docs/alpha` path. It describes the Postgres stack: the `/vnext` review console, connectors and the scheduler. The default install is `uvx alice-memory install`. It uses one local SQLite file and does not need Postgres. See [quickstart.md](quickstart.md).

This is a local technical preview, not hosted SaaS, not a production SLA, and not automatic memory autopilot.

## Included

- local vNext runtime
- `/vnext` operator console
- source review and archive
- candidate memory review
- generated artifact review and rating
- open-loop and project-update review
- Doctor/readiness checks
- capture-to-brief traceability
- scheduler visibility and local daemon
- connector health
- synthetic demo dataset
- MCP/API/CLI agent integration path
- Hermes skill guidance
- OpenClaw skill guidance
- custom agent guide
- preview-readiness command

## Who It Is For

- technical users comfortable with local setup
- early adopters testing agent memory
- builders connecting Hermes, OpenClaw, or custom agents

## What It Can Do

- capture local evidence
- compile scoped context packs
- ingest reviewable agent outputs
- create candidate memory proposals
- generate reviewable artifacts
- log policy decisions and provenance
- surface review queues in `/vnext`

## Intentionally Not Included

See [known-limitations.md](known-limitations.md).

## Security And Privacy

See [security-and-privacy.md](security-and-privacy.md). The key rule is unchanged: source evidence, generated artifacts, and agent proposals require human review before trusted memory changes.

## Quickstart

Start with [quickstart.md](quickstart.md), then run:

```bash
alicebot vnext alpha check
```

Headless Ubuntu dogfood readiness uses the current preview install guide at [headless-ubuntu-install.md](headless-ubuntu-install.md), [hermes-dogfood-ubuntu.md](hermes-dogfood-ubuntu.md), `scripts/install-ubuntu.sh`, systemd templates under `packaging/systemd/`, and:

```bash
alicebot vnext alpha check --headless
alicebot vnext smoke headless-ubuntu
```

## Support And Feedback

Use [onboarding.md](onboarding.md) for what to include in reports.
