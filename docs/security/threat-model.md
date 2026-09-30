# Shipped-Product Threat Model

## Overview

Alice is an agent continuity and governed-memory layer. Its primary runtime
surfaces are the HTTP API and `/vnext` operator UI, a core MCP server over
stdio, PostgreSQL or a local SQLite on-ramp, import/capture connectors, model
providers, a scheduler, and local export/backup/log paths.

This model covers the default local-first, single-user, self-hosted product:
the HTTP API and `/vnext` operator UI, the core MCP tools over stdio (three by
default, eleven with `ALICE_MCP_FULL_TOOLS=1`), per-agent API keys, PostgreSQL
with per-user RLS, the SQLite on-ramp, connectors/importers, model providers,
the scheduler, exports/backups, and local logs.

The frozen Phase 5 Stage A carrier exposes 183 default HTTP operations, of which
71 are `/v0/vnext`, and 232 operations when `ALICE_LEGACY_SURFACES=1`; focused
closure tests reproduced those exact sets. Legacy surfaces are off
by default and the flag is read at import time, so changing it requires a
process restart.

## Threat Model, Trust Boundaries, And Assumptions

### Deployment Assumptions

Explicitly out of scope are multi-tenant hosting, a managed control plane, SLA
claims, protection from a compromised host/root account, and the security of
third-party model providers beyond Alice's validation and disclosure controls.

Keyless operation assumes the API is loopback-only and every process and OS
user that can reach it is trusted as the owner. A finding that requires public
exposure of a deliberately keyless port therefore violates the supported
deployment assumption; a code path that bypasses an active-key boundary does
not.

### Assets And Security Objectives

| Asset | Objective |
| --- | --- |
| Memories, source evidence, artifacts, traces, and exports | Preserve confidentiality and user/project scoping; keep provenance truthful. |
| Review decisions and lifecycle state | Prevent privilege escalation, cross-project action, forged reviewer identity, and mutation after terminal decisions. |
| Agent keys, provider credentials, connector secrets, database URLs, and one-time capabilities | Minimize exposure, store verifiers/references rather than reusable plaintext where supported, and never place broad credentials in hostile page context. |
| Event and revision history | Preserve audit skeletons, authenticated actor attribution, and append-only or controlled-redaction invariants. |
| Retrieval and generated output | Treat imported/provider text as untrusted data; do not let content become policy or broaden authorization. |
| Runtime availability | Bound inputs and external work sufficiently for the supported single-user deployment; make remaining resource-exhaustion risk visible. |

### Actors

- **Local owner/operator:** controls the host, config, database, UI, and key
  lifecycle. Fully trusted in keyless local mode.
- **Keyed agent:** trusted only for the identity, permission profile, user, and
  optional project scope bound to its key.
- **Local unkeyed process:** equivalent to the owner only while the deployment
  deliberately remains keyless and loopback-only.
- **Visited web page:** hostile. Its DOM, JavaScript, extensions, service workers,
  and origin can inspect bookmarklet state and influence captured content.
- **Imported content or source directory:** hostile data. Files may be malformed,
  oversized, symlinked, or changed concurrently.
- **Provider/connector service:** external and potentially faulty, malicious, or
  compromised; it receives configured prompts/data and may return hostile
  content or errors.
- **Remote network client:** untrusted unless authenticated through the supported
  key and deployment boundary.
- **Other local OS user or container peer:** out of the keyless trust set; host
  permissions and network binding must exclude it.

### Input Control

- **Attacker-controlled in supported deployments:** authenticated HTTP and MCP
  payloads from a compromised/low-privilege agent; visited-page DOM, URL,
  selection, scripts, and origin; imported files and metadata; provider and
  connector responses; FTS/search text; and any remote request admitted by the
  configured reverse proxy.
- **Operator-controlled:** environment variables, database URLs, provider
  endpoints, CORS origins, feature flags, import roots, backup/export paths,
  secrets, key issuance/revocation, and host/proxy/firewall configuration.
  These remain dangerous inputs but normally require an owner mistake or
  compromised owner account.
- **Developer-controlled:** migrations, route/policy registries, OpenAPI
  contracts, fixed SQL fragments, dependency manifests/locks, CI workflows,
  and release gates. Compromise here is a build or supply-chain threat.

### Trust Boundaries

| Boundary | Crossing | Required control |
| --- | --- | --- |
| Host/network | Client to HTTP API or web UI | Loopback in keyless mode; otherwise TLS reverse proxy, active agent keys, exact CORS origins, and firewall isolation. |
| Browser/operator UI | Trusted Alice page to API | Operator key retained only in mounted-session memory; no key in URL, storage, logs, or page content. |
| Visited-page context | Bookmarklet to clipper capture route | Short-lived, origin-bound, one-time capability; no agent key or reusable `capture_token`; atomic redemption. |
| HTTP identity | Request body/query to authenticated actor | Key record, not payload, controls identity/profile; active-key deployments reject missing/invalid Bearer credentials. |
| Authorization | Actor to target memory/artifact/project | Persisted target scope and sensitivity drive policy; project-bound keys cannot widen; central routes require unbound trusted/admin keys. |
| Application/database | Runtime store operation to PostgreSQL | Application role, transaction-scoped `app.current_user_id`, forced RLS, parameterized SQL; admin URL reserved for migration/recovery. |
| Local process/file | SQLite, secrets, logs, exports, imports | Owner-only paths, alias/symlink checks where implemented, explicit import provenance; SQLite is not a tenant boundary. |
| MCP client/process | JSON-RPC stdio to core tools | Local process trust when keyless; `ALICE_AGENT_API_KEY` binds a key and suppresses legacy handlers lacking equivalent persisted-target authorization. |
| Alice/provider | Outbound model or connector request | Validated provider configuration, credential references, sanitized public errors, restrictive network deployment policy. |
| Content/policy | Source or model text to memory/review action | Content remains data; policy evaluation and review gates are code-controlled. |

### Principal Data Flows

1. A local or keyed caller submits a vNext request with `user_id` and optional
   agent claims. The centralized HTTP boundary resolves the key and replaces or
   rejects claims before the route executes.
2. Policy-aware routes authorize requested scope; target-changing routes re-read
   persisted target scope where required. The store runs under the user's RLS
   principal in PostgreSQL or the owner's local SQLite file.
3. Captured/imported content is normalized into sources, memories, evidence,
   events, and revisions. Review and explicit commit paths control promotion to
   trusted memory.
4. Retrieval applies scope filters before returning context. Provider calls may
   receive selected content; provider responses and errors return through
   bounded contracts and public-error sanitization.
5. The trusted Alice UI requests a browser-clip capability for a normalized
   origin. The visited page submits one capture with that narrow capability;
   the first authorized attempt consumes it. A failed normalization or import
   does not make the capability reusable, so retry requires fresh issuance.
6. Exports, backups, source archives, and logs cross from application state to
   the local filesystem and inherit the sensitivity of the source material.

## Attack Surface, Mitigations, And Attacker Stories

The most important attacker stories are a lower-privilege keyed agent trying to
become an admin or cross project/user scope; a hostile page trying to turn a
clip operation into reusable Alice authority; an imported file changing query,
filesystem, or provenance behavior; an external provider exfiltrating secrets
through errors/redirects or exhausting a worker; and a dependency/build change
compromising published artifacts.

Browser-only CSRF and XSS stories depend on whether an operator exposes Alice to
an untrusted browser origin. Exact CORS origins and loopback defaults reduce the
default likelihood, but the visited-page bookmarklet is intentionally treated
as hostile regardless. Classic multi-tenant session attacks are not a supported
product story because Alice has no managed multi-tenant session boundary; an
active-key or RLS bypass remains in scope.

### Priority Abuse Cases And Controls

| Abuse case | Control/evidence | Residual concern |
| --- | --- | --- |
| Missing key treated as remote anonymous access | Documented local-only boundary; active-key rule rejects keyless requests. | Host/proxy misconfiguration can invalidate the assumption. |
| Payload claims a stronger profile or another project | Key-bound actor/profile/scope, escalation rejection events, policy tests. | Final carrier needs all-route ASGI closure evidence. |
| Cross-user PostgreSQL read/write | Application-role RLS and user-scoped connections. | Admin credentials or a compromised host bypass the product boundary. |
| Broad credential exposed to a visited page | One-time origin-bound clipper capability replaces reusable bookmarklet token. | The page can make its one authorized submission; the UI must show the bound origin. |
| SQL, JSON-path, or FTS injection | Parameter binding, fixed/allowlisted SQL fragments, fail-closed request models, adversarial FTS tests. | Every new dynamic fragment needs review; PostgreSQL hostile-query coverage is narrower than SQLite coverage. |
| File traversal, symlink escape, or source substitution | SQLite portable-import alias/snapshot controls and import provenance. | Markdown, ChatGPT, and OpenClaw directory importers still have the documented symlink/TOCTOU gap. |
| Secret or exception disclosure | Hash/reference storage, recursive secret-field redaction, provider error sanitization, stable public error vocabulary. | Final carrier must close raw-key logging and exact provider-key non-echo proof. |
| Dependency compromise or known advisory | Exact web versions/lockfile, fail-closed npm bulk audit, Dependabot, SHA-pinned Actions, CodeQL, Gitleaks. | No fail-closed Python advisory audit is currently in CI. |
| Resource exhaustion from hostile files/provider responses | Existing size/shape checks and local deployment limits. | Historical partial scan retained multiple low-confidence availability hypotheses for Stage B. |
| Agent resolves its own pending write without asking the user | `alice_memory_commit` with `confirmation_id` resolves only a pending write its caller may resolve (its author, an `admin_agent` key, or the owner), after the same policy and ceiling checks as a write. | The confirm step asks the agent to ask the user. It is not a gate, and Alice cannot tell whether the user was asked. See the 2026-09-30 limitation below. |

### Known Internal Limitations

- `get_settings()` uses a process-wide `lru_cache(maxsize=1)`. That is acceptable
  for Alice's one-configuration-per-process runtime, but embedders that mutate
  environment/config after the first call can observe first-caller-wins state.
- Redaction flows may read content into process memory before validating and
  applying the durable scrub. Returned/persisted results are redacted, but the
  transient plaintext lifetime is a RAM-hygiene limitation.
- The Markdown, ChatGPT, and OpenClaw directory importers can follow an
  outside-root symlink and can reread a file after archiving it. Until remediated,
  import only from an owner-controlled, immutable staging directory containing
  no symlinks.
- Python dependency advisories are monitored by Dependabot but are not checked
  by a fail-closed install-tree audit in CI.
- Confirming a pending write is not a human gate (added 2026-09-30).
  `alice_memory_commit` declares `destructiveHint: false`, which is true of
  adding a fact. Called with `confirmation_id` and `confirmation_action`, the
  same tool instead updates the one pending row it names (`needs_review` to
  `active`, or to `rejected`), writes that row's revision and events, and
  updates the caller's agent identity row when the call carries one. It
  changes no other memory. By the
  approval rule the code records for Codex's default mode, which has not been
  checked against a running Codex, neither call prompts. Every trigger for
  `confirmation_required` is a label the agent writes (confidence, domain,
  sensitivity, contradiction references, source type), and the same tool
  commits the same text directly when it is labelled plainly. So the confirm
  step relies on the agent asking the user, and Alice cannot tell whether it
  did. A keyless call that declares no agent id is recorded as actor type
  `user` with no actor id, which the audit trail cannot tell from the owner.
- A memory id is itself sensitive metadata (added 2026-09-30). The correction
  label on a recalled or packed source excerpt, and the `supersedes` and
  `superseded_by` fields on a context pack's memories, follow the same
  sensitivity, domain, project, person and time fence as the memory reads.
  Four places still carry a memory id without the sensitivity and domain
  fence: `validity.superseded_by_memory_id` on an `alice_recall` result whose
  row still carries a `superseded_by` pointer; the `target_id` of each entry in
  a context pack's `recent_changes`; ids copied into a stored memory's
  `metadata_json`, which the full pack returns; and the `supersession_context`
  section of a `context_depth: high` pack, which walks older and newer
  revisions through a lookup that applies the project, person and time scope
  only and names each revision's id and title, so a hidden revision two or more
  steps from a visible one is named. Each needs a memory the caller cannot
  read, so each differs from the owner's view only for key-bound agents or an
  explicit sensitivity ceiling.
- Stage A tests are team-authored. They reduce review cost; they do not replace
  adversarial testing by the owner-appointed Stage B reviewer.

## Severity Calibration

Severity is calibrated to the documented local-first and keyed single-tenant
profiles, not to a hypothetical public multi-tenant service.

- **Critical:** realistic compromise of the supported release or host boundary
  with catastrophic blast radius, such as unauthenticated remote code execution
  in a supported keyed deployment, compromise of the release pipeline that
  ships attacker code, or extraction/destruction of all user data and broad
  credentials without meaningful preconditions.
- **High:** bypass of an active key or PostgreSQL tenant boundary that exposes or
  irreversibly mutates broad sensitive state; privilege escalation from a
  project/read-only key to admin redaction/review power; or broad provider/agent
  credential disclosure to a realistic remote attacker. Public reachability
  and breadth can raise a Medium to High.
- **Medium:** scoped but material confidentiality/integrity loss, reusable
  browser authority exposed to an ordinary visited page, provenance mismatch or
  outside-root read from an attacker-influenced import directory, or reliable
  service exhaustion through a normal supported input. Strong local-owner
  preconditions or a narrow record set can lower severity.
- **Low:** bounded availability or metadata exposure requiring owner-controlled
  input, unsupported public exposure of a deliberately keyless service,
  hardening gaps on an already compromised local account, or defense-in-depth
  weaknesses without a demonstrated supported attack path.

An issue is not dismissed solely because Alice is local-first: malicious web
pages, imported files, providers, and lower-privilege agent keys are real
untrusted actors. Conversely, a story that assumes the trusted owner is already
root or deliberately publishes a keyless loopback service must state that
precondition and should not be rated like an active-key remote bypass.

## Review And Change Rule

Update this model whenever an external surface, credential type, deployment
assumption, database boundary, importer, or provider transport changes. The
reviewer must bind conclusions to an exact commit and deployment profile. See
the [external-review brief](external-review-brief.md).

Historical scan provenance: target_sha256_ef4c4bf0346dfa7c51c7095b6fcf0a4bb671ad7cb04ba861e4e9e8194fdc283a
Stage A base revision: c9d24243920a694eaf00ad595da392a1478710dd
