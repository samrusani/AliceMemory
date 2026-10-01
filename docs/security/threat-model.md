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

Unreleased (on main, not in v0.19.2): keyless operation also checks the name a
request uses, because a browser on the owner's machine can reach a loopback API
without being a trusted process. DNS rebinding points a name the attacker owns
at 127.0.0.1, so a page the attacker serves can call the API with the attacker's
name in `Host` and read the answer as same-origin. The two keyless gates (the
`/v0/vnext` gate and the `/v1` gate) therefore refuse a request that carries no
agent key unless its `Host` is `localhost`, `127.0.0.1` or `::1` (any port,
case-insensitive, an IPv6 literal in brackets, one trailing dot allowed) or an
exact name the operator lists in `ALICEBOT_ALLOWED_HOSTS`. Nothing is matched by
prefix, suffix or wildcard. A missing, repeated or malformed `Host` is refused,
and `X-Forwarded-Host` and `Forwarded` are never read. If an `Origin` header is
present it must be an exact entry of `CORS_ALLOWED_ORIGINS` or the request's own
origin (the same host and port as the validated `Host`), and `null` is refused.
That second rule also stops a cross-origin request that needs no rebinding, such
as a form post to `POST /v1/evals/runs`, which a `Host` check cannot see because
the request names the right host. A wildcard in `CORS_ALLOWED_ORIGINS` does not
admit a keyless cross-origin request. The browser-clipper capture route keeps its
exemption: a visited page calls it by design and its one-time capability is the
credential. A request with an agent key is not checked, so keyed traffic and the
reverse-proxy topology are unchanged. A refused request gets the gate's usual
401. In v0.19.2 both gates looked at the peer address only, so a rebound request
reached the API. The browser leg of the attack has not been reproduced in a real
browser; the rule holds without it. The legacy `/v0` routes, which are served
only in development and test or with `LEGACY_V0_ENABLED_OUTSIDE_DEV`, take no
agent key, so the identity layer applies the same rule to every request to them,
whatever `Authorization` header it carries. A CORS preflight is not refused by it.

Unreleased (on main, not in v0.19.2): the HTTP API caps a request body before any
layer reads it, because a request that has not authenticated can still make the
server read and parse what it sends. A pure ASGI layer, registered last so it is
the outermost, answers HTTP 413 for a body over `ALICEBOT_MAX_REQUEST_BODY_BYTES`
(4 MiB by default). It refuses a declared `Content-Length` over the cap before a
byte is read, and for a chunked or undeclared body it counts the bytes the layers
below pull and refuses as soon as the total crosses the cap, with
`Connection: close`. It covers the identity layer, the vNext and `/v1` gates, the
framework's own parse and callers that hold a valid key. The connector sync
routes take lists of whole documents that no model bounds, so they have their own
cap, `ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES` (32 MiB by default). The vNext gate
refuses a keyless request from another peer, or with a foreign Host or Origin,
before it reads the body, as the `/v1` gate already did, and the identity layer
skips its body rewrite for such a request. A JSON body nested more than 256 levels
deep is refused with HTTP 422 before any layer decodes it, with a validation
error of type `json_too_deep` and nothing from the body in it. In v0.19.2 the
identity layer, the two gates and the framework each read a body of any size and
parsed it before authentication: a 100 MiB chunked body took the server from
104 MiB to 712 MiB, and a body nested about 975 levels deep or more raised
`RecursionError` out of a layer and answered HTTP 500. The cap is a bound on one
request, not a rate limit. A client inside the cap can send many requests, and the
cost of a request near the cap is real (a 30.9 MiB connector sync body peaked at
about 330 MiB of server memory), so the reverse proxy should cap the body too:
`packaging/cloud/Caddyfile.example` sets `request_body { max_size 4MB }`.

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
| Alice/provider | Outbound model or connector request | Validated provider configuration, credential references, sanitized public errors, restrictive network deployment policy. Unreleased (on main, not in v0.19.2): every outbound call goes through one door, `open_provider_url`, which follows no redirect and, for the provider helpers, Gmail and Calendar, dials only an address the outbound policy allows. |
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
| A provider answers with a redirect, or its name resolves to an internal address after the base URL was checked | Unreleased (on main, not in v0.19.2): `open_provider_url` refuses every redirect, so the target is never contacted and no `Authorization` or `api-key` header is sent on, and with `enforce_public_peer` it resolves the name when it connects and dials only an allowed address, over http and https. The provider helpers, response generation, Gmail and Calendar enforce it. The embeddings, reranker, fact-key and brain clients do not, because a local Ollama endpoint is a documented setup. | A request carried by a proxy is not held to the address rule, because the peer is then the proxy. The embeddings, reranker, fact-key and brain clients can reach a loopback or private address the operator configured. A provider call's response body is read whole. Any valid key can register a provider on `/v1`, which authenticates and does not authorize. |
| Oversized or deeply nested request body before authentication | Unreleased (on main, not in v0.19.2): a body over 4 MiB (32 MiB for connector sync) is refused with HTTP 413 before any layer reads it, a keyless request from another peer is refused before its body is read, a body nested more than 256 levels is refused with HTTP 422, and the Caddy example caps the body at the proxy. | A request inside the cap still costs memory and time, and the cap is no rate limit. The cap counts bytes as sent. |
| DNS rebinding or a cross-origin request to a keyless loopback API | Unreleased (on main, not in v0.19.2): a keyless request must name `localhost`, `127.0.0.1`, `::1` or an operator-listed host in `Host`, and any `Origin` must be a configured origin or its own. A keyed request is not checked. | Not reproduced in a real browser. The legacy `/v0` routes take no key, so every request to them gets the Host and Origin rule. A name the operator lists in `ALICEBOT_ALLOWED_HOSTS` is trusted as this machine. |
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
- A memory id is itself sensitive metadata (added 2026-09-30, updated
  2026-10-01). From v0.19.2, every place below follows
  the same sensitivity, domain, project, person and time fence as the memory
  reads. The correction label on a recalled or packed source excerpt. The
  `supersedes` and `superseded_by` fields on a context pack's memories.
  `validity.superseded_by_memory_id` and `validity.supersedes_memory_id` on an
  `alice_recall` result, where `superseded: true` stays and only the id is left
  off. The `target_id` of each entry in a context pack's `recent_changes`, where
  an entry about a memory the caller cannot read is dropped and the search for
  older events stops after 2,048, so the list can be short. The id and title of
  each revision in a `context_depth: high` pack's `supersession_context`, where
  a revision the caller cannot read ends the walk and nothing past it is named.
  In the pack pointers, the recall `validity` ids, `recent_changes` and
  `supersession_context`, an id that names no row at all is not a hidden row: a
  scoped read drops it and an unscoped read keeps it, as before. The correction
  label names no id for a link it cannot resolve. In v0.19.0 each of these
  names the id whatever the caller may read, and the `recent_changes` and
  `supersession_context` cases are older than v0.19.0.
  One place still carries a memory id without the fence: ids copied into a
  stored memory's `metadata_json`, which an `alice_context_pack` call with
  `debug: true` returns for the memory types the debug pack lists in its own
  sections (checked: decision, procedure and belief memories). It needs a memory the caller cannot read and another
  memory whose `metadata_json` carries its id. The keyless owner, who runs under
  the default sensitivity ceiling, gets it as well as a key-bound agent, and the
  default pack does not name it. That is the result for `alice_recall`,
  `alice_context_pack`, `alice_resume`, `alice_recent_decisions` and
  `alice_open_loops`. The other tools, such as `alice_recent_changes` and
  `alice_timeline`, were not checked for memory ids.
- Open items from the internal security review of v0.19.0 (added 2026-10-01).
  They are not fixed in v0.19.2, and the v0.19.2 release notes give the detail.
  The Postgres stack's HTTP API parses a JSON request body of any size before it
  authenticates (a 262,057 byte body was read in full before the 401), and it
  does not check the `Host` header of a keyless loopback request. Unreleased (on
  main, not in v0.19.2): the `Host` and `Origin` rules above are in (DB-005), and a
  request body over 4 MiB is refused with HTTP 413 before it is read (DB-006). The
  local-folder scan reads each matching file whole with no size limit, and a
  file swapped for a link between its containment check and its read is read
  from outside the watched folder. The provider helper, which checks the
  configured base URL once, and the embeddings client, which does not check it,
  follow a redirect and send the `Authorization` header to the target; the
  reranker and fact-key clients use the same opener and were not run. Unreleased
  (on main, not in v0.19.2): no provider client follows a redirect any more, and
  the provider helpers, Gmail and Calendar dial only an allowed address (DB-009). Two
  scheduled CI jobs, the real-host canary and archive maintenance, hold
  issue-write authority while they install packages that are not pinned to an
  exact version: the canary installs the latest host CLIs, and archive
  maintenance installs the project's dev extras by version range. Each of
  these needs a precondition: a reachable API, a writable watched folder, a
  provider endpoint an attacker influences, or a compromised package in one of
  those CI installs.
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
