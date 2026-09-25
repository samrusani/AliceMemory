# Alice vNext

Alice vNext is the active local-first architecture for Alice as the continuity
layer for AI agents: a memory kernel, governed explicit agent memory commits,
reviewable generated briefs, review-gated memory consolidation, procedural
memory, model-backed source-grounded synthesis, local connector-backed evidence
capture, hardened connector settings/state/secrets, agent-facing context packs
and context trees, governed agent proposals, and a local scheduler runtime.

The vNext architecture is now the active Alice product line.
`v0.17.0` is the latest published release and remains the install, checksum,
and latest-release-notes baseline. Its tag, release record, and published
artifacts are immutable.
`v0.12.0` shipped the Phase 3 structural refactor with **Structure only. Zero
behavior change.** It relocates oversized HTTP, store, contract, MCP, and CLI
carriers behind stable public imports and entrypoints.
Alice remains local-first rather than a hosted launch; install the
package entrypoints for normal use and use an editable checkout only for
contributor workflows.

## Product Shape

Alice vNext has three functional layers:

- **Memory kernel**: the local-first persistence, provenance, policy, and event-log substrate. It owns sources, chunks, memories, revisions, graph edges, projects, open loops, artifacts, evals, and connector evidence.
- **Synthesis workflows**: the reviewable workflows on top of the kernel. They generate daily briefs, weekly syntheses, context packs, memory consolidation artifacts, contradiction reports, connection reports, project updates, open-loop reviews, and reviewable artifacts in deterministic or model-backed mode.
- **Agent integration surface**: exposes continuity through CLI, API, and MCP so external agents can capture, retrieve, resume, explain, generate context, navigate read-only context trees, explicitly commit user-directed memory through policy, propose reviewable memory, and trigger governed scheduler workflows without owning the memory database.

## Current Surfaces

- Source capture: manual text, local text/Markdown files, Markdown folders, ChatGPT exports.
- Retrieval: hybrid Postgres full-text + pgvector semantic search with reciprocal-rank fusion, domain/sensitivity filters, and provenance; without a configured embedding endpoint (`ALICE_EMBEDDINGS_BASE_URL`/`ALICE_EMBEDDINGS_MODEL`) it runs full-text-only and says so in traces.
- Synthesis workflows: daily brief, weekly synthesis, connection report, contradiction report, project update, open-loop review, review-only memory consolidation, and a review-first staleness sweep that marks expired or long-unconfirmed working-state memories `stale` without deleting anything.
- Procedural memory: typed end to end. Capture recognizes `Procedure:`/`Playbook:`/`How to` lines as `procedure` candidates (and `Happened:`/`Log:` lines as `episode`), retrieval accepts a `memory_type` filter so agents can recall procedures directly, and context packs include a procedures section. Procedures keep the same review, provenance, correction, supersession, and revision model as all other memory; there is no procedure-specific ranking or auto-classification beyond these typed rules.
- Model-backed intelligence: provider/routing abstraction, local-first model policy, source-grounded sections, prompt hashes, context hashes, model metadata, and deterministic-vs-model comparison mode.
- Quality review: artifact ratings for usefulness, accuracy, source grounding, novel connections, actionability, hallucination risk, verbosity, missed context, and comments.
- Agentic control plane: scoped agent identities, permission profiles, policy decisions, explicit trusted memory commits, inline confirmations, memory proposals, undo/correction/forget lifecycle controls, and Agent Activity audit surface.
- Governed scheduler: disabled-by-default workflow controls, a local daemon runner, due scans, run history, trace IDs, failures, duplicate-run locks, and reviewable artifacts.
- Connectors: local folder/Obsidian scan and watch, browser clipper capture,
  allowlist-aware ingestion of operator-supplied Telegram raw updates (no
  polling/token ownership), generic agent-output ingestion, dedicated settings/state rows, local encrypted
  secret references, retry/cursor hardening, plus deterministic PDF, DOCX, CSV,
  externally extracted screenshot-text, and externally produced voice-transcript
  payload ingestion. Alice does not execute OCR or transcription.
- UI: the local `/vnext` review workspace plus memory, continuity, trace,
  entity, and artifact views. Hosted onboarding/admin, channel, chat,
  chief-of-staff, model-pack, and response pages are not part of v0.11.
- Evals: six production-path suites — `retrieval_quality`,
  `correction_suppression`, `decision_recovery`, `provenance_explanation`,
  `entity_resolution`, and `graph_hop_retrieval`. They run against the backend
  selected by `ALICEBOT_EVAL_DATABASE_URL`; unavailable live-store runs are
  reported as skipped, never as passes. See [eval/README.md](../../eval/README.md)
  for exact mechanisms and targets.

## Start Here

1. Follow the [alpha quickstart](../alpha/quickstart.md) for the install path.
2. Use [first-run checklist](../alpha/first-run.md) and [doctor](../alpha/doctor.md) for onboarding.
3. Review [headless Ubuntu install](../alpha/headless-ubuntu-install.md), the
   [agent integration pack](../alpha/agent-integration.md), and
   [MCP tools](../alpha/mcp-tools.md).
4. Follow [vNext quickstart](quickstart.md) for the broader local workflow.
5. Review [architecture](architecture.md).
6. Review [security and privacy](security-privacy.md) and the [public-preview security posture](../alpha/security-and-privacy.md).
7. Review [local runtime](local-runtime.md) before running scheduler workflows in the background.
8. Use [example ALICE.md](ALICE.example.md) as the first Brain Charter.
9. Use [demo script](demo-video-script.md) and [demo mode](../alpha/demo-mode.md) for a short walkthrough.
10. Use the current [release runbook](../../RELEASING.md) before publishing or
    tagging. The older [vNext preview checklist](../release/vnext-public-release-checklist.md)
    is retained only as historical evidence.
11. Review the [latest published release notes](../release/v0.17.0-release-notes.md)
    and [known limitations](../alpha/known-limitations.md).
12. Review the [dogfood daily checklist](../runbooks/vnext-dogfood-daily-checklist.md) before daily local preview use.
13. Historical build-process summaries are archived under [docs/archive/process/](../archive/process/README.md).

## Alpha Boundary

The public alpha should prove that a technical user can install Alice locally,
capture live local evidence, configure local connector defaults safely, run
readiness checks, and generate a first daily brief. It must not claim managed
connector OAuth, packaged browser extensions, hosted connector polling, cloud
sync, channel transport, OCR/transcription execution, a hosted SLA, or automatic
promotion of generated artifacts into trusted memory.

`v0.17.0` is the latest published release and remains the install, checksum,
and baseline reference.
