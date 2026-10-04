# Contradictions and Trust Calibration

## Requirements and status

- Support status: legacy surface. Its MCP tools are off the default handshake, and the legacy MCP surface is frozen: it gets no new capabilities. The commands, routes and tools on this page are kept working: CI runs their tests against Postgres. New integrations use the core tools.
- Backend: Postgres, for the CLI, the HTTP API and the MCP tools alike. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The four MCP tools below read the continuity store, which exists only on Postgres: on the SQLite `alice-memory` vault they are listed but their calls fail. See [Full stack](../../README.md#full-stack-postgres--review-console).
- Settings: the MCP tools need `ALICE_MCP_LEGACY_TOOLS=1` in the server environment, on a keyless server. A server bound with `ALICE_AGENT_API_KEY` never lists or accepts them. The CLI commands and the HTTP routes need no flag.

## Scope

Contradiction state and trust adjustments are explicit across continuity review, explain, recall, CLI, API, and MCP surfaces.

The behavior:

- contradiction detection for direct fact, preference, temporal, and source-hierarchy conflicts
- persisted contradiction case records with status and resolution fields
- persisted trust-signal rows for contradiction, correction, corroboration, and weak inference
- retrieval penalties for unresolved contradictions
- explain output that surfaces contradiction state and penalty impact

## Detection Model

The detector compares active continuity objects and extracts candidate claims from:

- structured keys such as `fact_key`, `fact_value`, `preference_key`, `preference_value`, and temporal bounds
- fallback text patterns from decision, fact, commitment, waiting-for, blocker, and preference text

Only live continuity objects participate in contradiction detection. `active` and `stale` objects are live candidates, while `superseded` and `deleted` objects keep audit visibility without reopening contradiction penalties.

Detected conflicts are stored as contradiction cases with linkage to continuity objects:

- `canonical_key`
- participating continuity object ids
- contradiction kind
- rationale
- detection payload
- tracked object timestamps used to preserve or reopen prior resolutions

Temporal bounds are normalized to UTC during detection so date-only or naive ISO timestamps do not break overlap checks.

## Trust Calibration

Trust signals are ledger rows in `trust_signals`.

Signal types:

- `contradiction`
- `correction`
- `corroboration`
- `weak_inference`

Each signal records:

- active or inactive state
- direction
- magnitude
- human-readable reason
- optional contradiction linkage
- optional related continuity object linkage

Open contradiction cases apply a negative trust adjustment and a retrieval penalty. Resolved or dismissed contradiction cases keep the audit trail but stop contributing active penalty state.

## Storage

Two tables back this:

- `contradiction_cases`
- `trust_signals`

Both tables ship with indexes, grants, and row-level security policies matching current continuity storage expectations.

## Surfaces

### API

Endpoints:

- `POST /v1/contradictions/detect`
- `GET /v1/contradictions/cases`
- `GET /v1/contradictions/cases/{contradiction_case_id}`
- `POST /v1/contradictions/cases/{contradiction_case_id}/resolve`
- `GET /v1/trust/signals`

### CLI

Commands:

- `alicebot contradictions detect`
- `alicebot contradictions list`
- `alicebot contradictions show`
- `alicebot contradictions resolve`
- `alicebot trust signals`

### MCP

These are legacy MCP tools. Their requirements are at the top of this page. See also [Legacy tool surface](../alpha/mcp-tools.md#legacy-tool-surface).

Tools:

- `alice_contradictions_detect`
- `alice_contradictions_list`
- `alice_contradictions_resolve`
- `alice_trust_signals`

## Retrieval and Explainability

Recall syncs contradiction state for in-scope candidates before ranking. Open contradiction counts and penalty scores are attached to ordering metadata and reduce trust contribution during ranking.

Explain output includes:

- open and resolved contradiction counts
- contradiction kinds
- counterpart object ids
- contradiction-derived penalty score
- active trust signal count

## Verification

Tests cover:

- unit tests for contradiction detection, trust signal persistence, and resolution handling
- migration shape tests for contradiction and trust schema
- API integration covering detect, explain, recall penalty visibility, trust inspection, and resolution auditability
- CLI smoke for contradiction detection and trust inspection
- MCP smoke for contradiction and trust tool output
