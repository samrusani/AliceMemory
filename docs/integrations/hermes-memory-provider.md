# Hermes External Memory Provider: Alice

This guide installs Alice as a Hermes **external memory provider**.
For the canonical bridge operator path and config examples, see:

- `docs/integrations/hermes-bridge-operator-guide.md`
- `docs/integrations/hermes-provider-plus-mcp-why.md`

Hermes behavior with this provider:

- built-in `MEMORY.md` and `USER.md` stay active
- one external provider can be active at a time (`memory.provider`)
- Alice provider adds continuity recall/resumption/open-loop tools and prefetch

## What The Provider Adds

- `alice_recall`: deterministic continuity recall with provenance
- `alice_resumption_brief`: last decision, next action, open loops, recent changes
- `alice_open_loops`: open-loop dashboard retrieval
- prefetch: turn-start context assembled from Alice resumption brief
- bridge-phase lifecycle contract for `prefetch`, `queue_prefetch`, `sync_turn`, and `on_session_end`
- deterministic capture dedupe for repeated callback execution
- B2 auto-capture pipeline for `sync_turn`: `alice_capture_candidates` then `alice_commit_captures`
- capture mode policy support: `manual`, `assist` (default), `auto`
- local status readiness reporting without live network calls

## Continuity Model Mapping

The provider maps Alice continuity responses into Hermes provider hooks:

| Hermes provider hook | Alice endpoint | Mapping |
|---|---|---|
| `prefetch(query)` | `GET /v0/continuity/resumption-brief` | renders last decision, next action, open loops, and recent changes into ephemeral context |
| `queue_prefetch(query)` | `GET /v0/continuity/resumption-brief` | asynchronously prebuilds pre-turn context cache for next `prefetch` |
| `sync_turn(user, assistant)` | `POST /v0/continuity/captures/candidates` + `POST /v0/continuity/captures/commit` | post-turn extraction/commit pipeline with mode policy and duplicate-write suppression |
| `on_session_end()` | same as queued `sync_turn` pipeline work | deterministic flush of pending capture/commit work before provider shutdown |
| `alice_recall` tool | `GET /v0/continuity/recall` | returns ranked continuity objects with provenance and scope filters |
| `alice_resumption_brief` tool | `GET /v0/continuity/resumption-brief` | returns structured resume sections for deterministic follow-through |
| `alice_open_loops` tool | `GET /v0/continuity/open-loops` | returns waiting/blocker/stale/next-action open-loop groups |

## Install

Install into the Hermes memory plugin directory used by your active Hermes Python environment:

```bash
./scripts/install_hermes_alice_memory_provider.py
```

Optional flags:

- `--force` to replace an existing install
- `--symlink` for local development iteration
- `--destination-root /path/to/hermes/plugins/memory` to target a specific Hermes install

The installer copies the provider into Hermes. The copy does not change when you upgrade Alice, and the provider is not in the wheel. Run the installer again with `--force` to pick up a newer provider. A `--symlink` install follows the repository.

## Configure

Recommended Hermes `config.yaml` examples are published here:

- `docs/integrations/examples/hermes-config.provider-plus-mcp.yaml` (recommended)
- `docs/integrations/examples/hermes-config.mcp-only.yaml` (fallback)

Use the Hermes setup flow:

```bash
hermes memory setup
```

Select `alice` and provide:

- `base_url`: Alice API base URL (example `http://127.0.0.1:8000`)
- `user_id`: Alice user UUID scope

Security rules enforced by the provider:

- `https://` is required for non-loopback hosts.
- `http://` is only allowed for loopback/local development hosts (`localhost`, `127.0.0.1`, `::1`).
- the provider sends user scope in `X-AliceBot-User-Id` header (not URL query params).

Config is saved to:

- `$HERMES_HOME/alice_memory_provider.json`

Manual activation:

```bash
hermes config set memory.provider alice
```

## Verify

Check provider selection:

```bash
hermes memory status
```

Run provider smoke validation from this repository:

```bash
./.venv/bin/python scripts/run_hermes_memory_provider_smoke.py
```

Run the one-command bridge demo (provider smoke + MCP smoke):

```bash
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

Smoke output includes `structural.bridge_status` with:

- `ready`: bridge-phase config readiness
- `errors`: invalid config state details (if any)
- `legacy_config_keys`: legacy keys still accepted for compatibility
- `lifecycle_hooks`: readiness for `prefetch`, `queue_prefetch`, `sync_turn`, `on_session_end`

Optional live prefetch test:

```bash
./.venv/bin/python scripts/run_hermes_memory_provider_smoke.py \
  --live-prefetch-query "release gating decision" \
  --alice-base-url "http://127.0.0.1:8000" \
  --alice-user-id "00000000-0000-0000-0000-000000000001"
```

## First Memory Expectations

The provider gives Hermes always-on Alice continuity context and optional post-turn capture. It does not turn every Hermes conversation into trusted memory.

For a first memory test, use [../alpha/first-memory.md](../alpha/first-memory.md).

Operational split:

- provider: recall, prefetch, resumption brief, open-loop lookup, and optional structured `sync_turn` capture
- MCP: `alice_capture` / `alice_memory_commit` (core) for explicit capture and user-directed "remember/save this" instructions. The older `alice_vnext_commit_memory` example is keyless-local legacy compatibility only.
- `/vnext`: review, confirmation, audit, undo, correction, and forget

If a tester sees no memory after normal chat, first verify MCP access to a capture/commit tool (core `alice_capture`, or `alice_vnext_commit_memory` with `ALICE_MCP_LEGACY_TOOLS=1`) and check `/vnext` Memory Review before treating it as a storage bug.

## Single-External-Provider Model

Hermes MemoryManager allows:

- built-in provider (`builtin`) always
- plus at most one external provider (`alice`, `mem0`, `honcho`, etc.)

If a second external provider is registered, Hermes rejects it and keeps the first.

`run_hermes_memory_provider_smoke.py` validates this behavior directly.

## Provider vs MCP vs Skill Pack

Use this split to avoid overlapping integrations:

| Integration | Best for | Runtime shape |
|---|---|---|
| Alice memory provider | always-on continuity prefetch + memory tools inside Hermes memory stack | one external memory provider + built-in `MEMORY.md`/`USER.md` |
| Alice MCP server | Alice tool surface in Hermes: default three tools (`alice_memory_commit`, `alice_recall`, `alice_resume`). The other eight core tools are behind `ALICE_MCP_FULL_TOOLS=1`. Legacy tools can be enabled only for deliberately keyless same-host compatibility; a server bound with `ALICE_AGENT_API_KEY` hides and rejects them. | MCP server attached under `mcp_servers` |
| Hermes Alice skill pack | policy and prompting guidance on when/how to call Alice tools | skill instructions layered on top of provider or MCP |

Practical default:

- choose provider plus MCP as the default deployment shape
- choose MCP-only only when provider install is temporarily blocked
- add skill pack when you want stricter workflow prompting and response policy

## Provider Config Keys

`$HERMES_HOME/alice_memory_provider.json` supports:

- `base_url` (string)
- `user_id` (UUID string)
- `timeout_seconds` (float)
- `prefetch_recall_limit` (int)
- `prefetch_max_recent_changes` (int)
- `prefetch_max_open_loops` (int)
- `prefetch_include_non_promotable_facts` (bool)
- `sync_turn_capture_enabled` (bool, default `false`; when omitted, an explicitly configured `bridge_mode: assist` or `bridge_mode: auto` enables `sync_turn` capture)
- `memory_write_capture_enabled` (bool, default `false`)
- `bridge_mode` (string enum: `manual`, `assist`, `auto`; default `assist`)
- `session_end_flush_timeout_seconds` (float, default `5.0`)

`sync_turn_capture_enabled: false` always wins. Use that when you want bridge recall/prefetch behavior without post-turn capture, even if `bridge_mode` is `assist` or `auto`. `memory_write_capture_enabled` posts to `POST /v0/continuity/captures`.

From v0.18.0, with `sync_turn_capture_enabled` set, or with an explicit `bridge_mode` of `assist` or `auto`, only user-role candidates that match an explicit prefix are auto-saved and the rest are queued. When a candidate extracted from the assistant reply carries a credential, the whole turn is refused, so a valid user decision in that same turn is not saved. `POST /v0/continuity/captures` refuses credential material.

Correction, 2026-09-30: the paragraph above is true of the server route and false for this plugin before version 0.5.2. Plugin 0.5.1, which shipped in v0.18.0 and v0.19.0, joined each turn into one text and split it back into roles by line, so a reply that held a line break followed by `User: decision: ...` reached the route as the user's own text and was auto-saved. (In v0.17.0 that request returned HTTP 422 and nothing was stored.) The same split kept only the first line of each side, and two different turns could share one dedupe fingerprint. Separately, `POST /v1/memory/operations/commit` does not read a candidate's role: in `assist` and `auto` mode it applies an assistant-role candidate that carries an explicit prefix. The Hermes plugin does not call that route, and the rule above covers the `/v0/continuity` capture routes only.

Unreleased (on main, not in v0.19.0): plugin 0.5.2 sends the user text and the assistant text of a turn to `POST /v0/continuity/captures/candidates` as two separate fields and never splits a joined text back into roles. A reply that contains a line starting with `User:` stays assistant text, which is at most queued for review and is never auto-saved. A multi-line user message is sent whole. Each side is capped at 3,800 characters on its own, where the joined text used to be capped once. Two different turns no longer share a dedupe fingerprint. When the candidate routes answer HTTP 404, the plugin posts one labelled `User:` and `Assistant:` text to `POST /v0/continuity/captures`, which reads no roles and derives an object only when the whole text starts with a prefix. An existing install keeps plugin 0.5.1 until you run `./scripts/install_hermes_alice_memory_provider.py --force`; a `--symlink` install picks up the change.

Unreleased (on main, not in v0.19.0): `POST /v1/memory/operations/commit` now reads the role. In `assist` and `auto` mode it applies only a candidate from `user_content` that matched an explicit prefix at confidence 0.9 or more, the rule the `/v0/continuity` capture commit uses. A candidate from the assistant and a phrase match from either role are `review_required`, so the correction above about that route describes v0.19.0 and earlier. The plugin does not call it.

From v0.19.0, `POST /v0/continuity/captures/candidates`
withholds a token in its response and stores nothing. Committing that
withheld text is refused with the same 400 as a credential and stores
nothing. In v0.18.0 that response echoes the token.

In v0.17.0, `POST /v0/continuity/captures` checks for credentials only when the capture derives an object. For a client that sends `user_id` in the body, a capture that derives no object stores its raw text in the capture inbox, credential material included. The Hermes memory-write mirror sends the user id only in a header, so in v0.17.0 it got HTTP 422 and stored nothing. Auto-save is broader: `assist` auto-saves any explicit candidate of an allowed type at confidence 0.9 or more, from either role, and `auto` auto-saves an allowed type at confidence 0.85 or more. The `sync_turn` capture can also fail with HTTP 422, because the plugin sends the user id only in the `X-AliceBot-User-Id` header.

Legacy compatibility keys still accepted for shipped configs:

- `prefetch_limit`
- `max_recent_changes`
- `max_open_loops`
- `include_non_promotable_facts`
- `auto_capture`
- `mirror_memory_writes`
- `capture_mode`
