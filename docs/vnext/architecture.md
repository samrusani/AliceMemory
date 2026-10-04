# vNext Architecture

Alice vNext is organized around inspectable continuity rather than generic notes or hidden chat summaries.

## Layers

### Memory Kernel

The kernel owns durable state and policy boundaries:

- `sources` and `source_chunks` preserve raw evidence and normalized text.
- `memories` store typed candidate or accepted claims with status, confidence, domain, and sensitivity. `procedure` is a typed memory class for repeatable playbooks: capture extracts `Procedure:`/`Playbook:`/`How to` lines as `procedure` candidates, retrieval filters by `memory_type`, and context packs carry a procedures section. Procedures use the same store, provenance, review, correction, and revision semantics as other memory; there is no procedure-specific ranking or auto-classification beyond these typed rules.
- `memory_revisions` preserve correction, promotion, supersession, and rejection history.
- `provenance_links` connect memories, artifacts, graph edges, projects, and open loops back to source chunks.
- `event_log` records append-only system events for mutation, connector sync, artifact generation, and review.
- `brain_charters` store the user-editable operating agreement (the `ALICE.md` charter).
- `artifact_quality_ratings` store human review scores and comments for generated artifacts.
- `agent_identities`, `scheduler_workflows`, and `scheduler_runs` persist governed agent and schedule state across restarts.

### Synthesis Workflows

Synthesis workflows generate reviewable outputs from the kernel:

- context packs for retrieval
- read-only agent context trees
- daily briefs
- weekly syntheses
- memory consolidation artifacts
- connection reports
- contradiction reports
- project update candidates
- open-loop extraction and review
- generated artifacts with Markdown export

Each synthesis workflow can run in deterministic mode or model-backed mode. Model-backed artifacts store provider, model, routing, policy mode, prompt hash, input context hash, trace ID, source references, and grounded sections for facts, inferences, recommendations, uncertainties, contradictions considered, and open questions.

Generated artifacts are not trusted memory by default. Promotion stays explicit and reviewable. Model-backed project updates, weekly insights, connections, and contradictions create only candidate memories or graph edges until a human accepts them.

### Agent Integration Surface

The agent integration surface exposes Alice to external tools without letting those tools own Alice state:

- CLI commands for local workflows
- API endpoints for product surfaces
- MCP tools for agent environments
- connector payload ingestion for source evidence
- scoped agent identities and permission profiles
- governed scheduler controls for local proactive workflows
- read-only context tree navigation over existing projects, memories, sources, open loops, artifacts, and recent traces

Agents can request context, generate artifacts, propose memory, and run allowed
core scheduler workflows, but durable mutation still passes through kernel
policies, review state, provenance, and event logging. The legacy task/execution
engine is unmounted by default behind `ALICE_LEGACY_SURFACES=1`.

Agent-originated HTTP calls authenticate with per-agent API keys. Create one with `alicebot agent keys create --agent-id <id> --profile <profile>` and send it as `Authorization: Bearer <key>`. The key record overrides any payload-supplied identity; payloads may only downgrade the granted permission profile. Keyless agent calls work only while zero active keys exist. MCP binds a key through the `ALICE_AGENT_API_KEY` environment variable.

## Data Flow

1. Raw input arrives from manual capture, import, or connector payload.
2. The kernel stores the raw evidence, content hash, connector metadata, domain, sensitivity, and timestamps.
3. Capture splits text into chunks and proposes candidate memories.
4. Synthesis workflows retrieve allowed evidence and generate reviewable deterministic or model-backed artifacts. Retrieval is hybrid: full-text plus vector search fused with reciprocal-rank fusion (SQLite FTS5 and cosine similarity on the default install, Postgres full-text search and pgvector on the full stack). Without a configured embedding endpoint (`ALICE_EMBEDDINGS_BASE_URL`/`ALICE_EMBEDDINGS_MODEL`), retrieval runs full-text-only and states that in traces.
5. Review actions accept, edit, reject, supersede, close, snooze, or promote.
6. Quality review actions rate artifacts for usefulness, accuracy, source grounding, novelty, actionability, hallucination risk, verbosity, missed context, and comments.
7. Event log records write paths for audit. The vNext event log is an append-only audit trail, not a temporal query surface: vNext does not yet answer as-of or history questions over memory state.
8. Agent and scheduler actions add agent identity, policy decision, run ID, trace ID, target ID, and workflow metadata where applicable.

## Memory Consolidation

The `memory_consolidation` scheduler workflow scans accepted memories, reviewed sources, generated artifacts, recent events, corrections/contradictions reflected in memory state, and artifact quality ratings. It produces a reviewable `memory_consolidation` artifact and may create candidate memories with source references. Consolidation can now emit review-governed deduplication proposals for overlapping memories, preserves every original until review, and uses a stable workflow digest so identical scoped inputs cannot create duplicate artifacts or candidates. Automatic merging remains out of scope.

Consolidation never updates or promotes trusted memory automatically. Candidate memories must pass the normal `/vnext` review, correction, supersession, and audit paths before they affect recall.

## Memory Staleness

The `staleness_sweep` scheduler workflow (daily by default, disabled until explicitly enabled like every workflow) marks memories `stale`; it never deletes, archives, or supersedes them:

- Any active memory whose `valid_to` has passed is marked stale, regardless of type: a row that declares its own end of validity is stale once that time arrives.
- Active working-state memories (`open_loop`, `commitment`, `project_state`) whose `last_confirmed_at` is older than a configurable window (180 days by default) are marked stale. Durable types — preferences, semantic facts, procedures, identity and relationship facts — are exempt because stability priors differ by type: silence does not make a preference or a playbook less true, while working state describes a world that moves on without confirmation.
- Every mark writes a memory revision and a `memory.stale_marked` event. Stale memories stay fully auditable and can be re-confirmed or archived through the normal review paths.

Confirm, accept, and correction paths refresh `last_confirmed_at`, so actively reviewed memories do not age out.

## Model Routing

Synthesis workflows use the surviving provider invocation abstraction for
structured extraction, summarization, classification, and embeddings where a
core workflow needs them. Alice does not expose a bundled chat or
OpenAI-compatible `/v0/responses` product endpoint. Retrieval embeddings use any
OpenAI-compatible endpoint (Ollama, LM Studio, OpenAI) via
`ALICE_EMBEDDINGS_BASE_URL`, `ALICE_EMBEDDINGS_MODEL`, and optional
`ALICE_EMBEDDINGS_API_KEY`.

Routing modes are:

- `local_only`
- `cloud_allowed`
- `cloud_requires_approval`
- `model_disabled`

Private, confidential, highly sensitive, sacred, and regulated scopes default to local-only or disabled unless the caller explicitly enables a permitted private cloud path. Public, internal, and professional scopes are configurable. Routing decisions are stored with the artifact and are also visible in scheduler and agent metadata when relevant.

## Agentic Control Plane

Agent-originated API, CLI, and MCP calls can carry `agent_id`, `agent_type`, `agent_run_id`, `task_id`, `project_scope`, and `permission_profile`.

Initial permission profiles are:

- `read_only_agent`
- `project_scoped_agent`
- `trusted_local_agent`
- `memory_proposal_agent`
- `admin_agent`

The policy layer evaluates the requested action, project scope, domain scope, sensitivity scope, workflow type, and write policy. Decisions are `allowed`, `allowed_with_filtering`, `requires_review`, or `blocked`; filtered and blocked outcomes are logged.

Agent memory proposals stay candidate and review items. An explicit agent commit becomes durable only when the configured policy permits it: with a persona of `personal` or `team` configured (`ALICE_MEMORY_PERSONA` or the Brain Charter) and an issued agent key, a commit that nothing escalates is promoted (see [promotion personas](../memory/promotion-personas.md)), and the default is review. Scheduler output and generated artifacts stay review items.

## Governed Scheduler

The local scheduler owns disabled-by-default workflow configuration for:

- `daily_brief`
- `weekly_synthesis`
- `connection_report`
- `contradiction_report`
- `open_loop_review`
- `project_update_scan`
- `memory_consolidation`
- `staleness_sweep`

Daily Brief, Weekly Synthesis, Memory Consolidation, and the Staleness Sweep are full runnable workflows. Local due scans run enabled, unpaused workflows whose `next_run_at` has arrived, then advance the next run timestamp. Other workflow types have persistent configuration, policy-checked control paths, run history, and generated report artifacts. Scheduler runs record status, trace ID, triggering actor, policy decision, generation mode, agent identity when present, output artifact ID, and failure details.

## Connector Boundary

The connector layer has two local-first tiers.

Live local capture supports:

- local folder and Obsidian-style Markdown/text scan or polling watch
- browser clipper capability issuance through `POST /v0/vnext/connectors/browser-clipper/capabilities`, followed by one-time origin-bound capture through `POST /v0/vnext/connectors/browser-clipper/capture`
- generic agent output ingestion through CLI/API/MCP

Deterministic payload ingestion remains available for:

- operator-supplied Telegram raw updates through the allowlist-aware import
  endpoint; Alice does not poll Telegram or manage a bot token
- browser clip JSON
- PDF/DOCX text extracted by an external parser
- CSV text or row payloads
- screenshot text extracted by an external OCR tool
- voice transcripts produced by an external transcription tool

Each connector preserves raw evidence in source metadata, applies conservative default domain/sensitivity, and writes audit events for settings/state changes. Dedicated `connector_settings` rows hold enabled/configured posture, defaults, sync mode, polling interval, validation errors, and metadata. Dedicated `connector_state` rows hold cursors, timestamps, failure posture, counters, and dedupe state. Cursor advancement pauses when an item fails so a broken item is not silently skipped on the next sync.

## Security Model

- Local-first by default.
- No cloud model call is required by the deterministic vNext seed or local-only model-backed mode.
- Model routing prevents private and highly sensitive content from leaving local mode unless explicitly configured.
- Connectors do not execute source instructions.
- Connector secrets are represented by `secret_ref` values and resolved through the secret-provider interface, not returned as normal settings.
- Live connector output is stored as untrusted source material and may only create candidate memories or reviewable artifacts.
- Prompt-injection content from sources is treated as data, not policy, and cannot trigger tool writes. Model prompts mark source content as untrusted context and instruct providers not to execute embedded source instructions.
- Sensitive domains and sensitivities are filtered before context-pack assembly.
- Generated artifacts inherit the highest selected source sensitivity.
- Agents cannot bypass domain/sensitivity filters, review-required workflows, scheduler policy checks, Brain Charter constraints, or the promotion rules.

## Current Production Gap

The connector settings/state/secret posture is local-alpha infrastructure.
Managed OAuth, packaged browser extensions, channels, OCR execution,
transcription execution, hosted connector polling, hosted scheduling, and
automatic memory promotion remain outside this preview. Production deployments
should bind the secret-provider interface to OS keychain or managed secret
infrastructure rather than relying on the local encrypted-file fallback.
