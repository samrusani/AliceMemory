# Protected Paths

These paths gate continuity correctness, upgrade safety, and cross-surface compatibility.
Changes here must not merge silently. If a pull request touches any protected path, it must include a completed `Upgrade Overview` using [UPGRADE_OVERVIEW_TEMPLATE.md](UPGRADE_OVERVIEW_TEMPLATE.md).

## Why This Exists

The protected surface covers the parts of Alice where a small code change can create hidden breakage:

- schema drift that invalidates stored memory or migrations
- evidence-chain changes that weaken explainability or provenance
- trust or promotion changes that quietly alter durable-truth behavior
- API changes that desynchronize CLI, MCP, HTTP, and web consumers

## Protected Areas

### Memory schema

Protected paths:

- `apps/api/alembic/versions/*.py`
- `apps/api/src/alicebot_api/store.py`
- `apps/api/src/alicebot_api/legacy_store/*.py`
- `apps/api/src/alicebot_api/vnext_store.py`
- `apps/api/src/alicebot_api/sqlite_store.py`
- `apps/api/src/alicebot_api/vnext_stores/*.py`
- `apps/api/src/alicebot_api/vnext_stores/postgres/*.py`
- `apps/api/src/alicebot_api/vnext_stores/sqlite/*.py`
- `apps/api/src/alicebot_api/db.py`
- `apps/api/src/alicebot_api/memory.py`
- `apps/api/src/alicebot_api/contracts.py`
- `apps/api/src/alicebot_api/_contracts/*.py`

Invariants:

- existing stored data must remain readable through the upgrade path
- migrations should be additive-first or explicitly sequenced when destructive work is unavoidable
- enum, status, and typed-memory contract changes must be reflected across persistence, serialization, and validation
- schema-affecting changes must state rollout order, backfill needs, and rollback posture

### Evidence pipeline

Protected paths:

- `apps/api/src/alicebot_api/artifacts.py`
- `apps/api/src/alicebot_api/compiler.py`
- `apps/api/src/alicebot_api/continuity_capture.py`
- `apps/api/src/alicebot_api/continuity_evidence.py`
- `apps/api/src/alicebot_api/continuity_explainability.py`
- `apps/api/src/alicebot_api/continuity_review.py`
- `apps/api/src/alicebot_api/importers/*.py`

Invariants:

- evidence links must remain auditable from continuity object back to archived source material
- archived artifact copies and checksums must stay deterministic for the same input
- review, explain, and recall surfaces must preserve provenance rather than inventing opaque synthesis
- importer changes must describe whether previously archived evidence remains valid

### Trust rules

Protected paths:

- `apps/api/src/alicebot_api/contracts.py`
- `apps/api/src/alicebot_api/_contracts/*.py`
- `apps/api/src/alicebot_api/memory.py`
- `apps/api/src/alicebot_api/trusted_fact_promotions.py`

Invariants:

- trust classes, confirmation status, and promotion eligibility must not change meaning silently
- trust posture visible in retrieval, review, and explain output must stay internally consistent
- high-risk or contested memories must remain reviewable and visible to operators
- any trust-rule change must explain how existing stored memories behave after upgrade

### Promotion logic

Protected paths:

- `apps/api/src/alicebot_api/memory.py`
- `apps/api/src/alicebot_api/trusted_fact_promotions.py`

Invariants:

- single-source model output must not become durable trusted patterns without corroboration
- trusted fact pattern and playbook generation must remain deterministic and traceable to source facts
- promotion or cleanup changes must describe how stale patterns, playbooks, or orphaned records are handled
- promotion behavior changes must include compatibility notes for downstream consumers

### Continuity APIs

Protected paths:

- `apps/api/src/alicebot_api/cli.py`
- `apps/api/src/alicebot_api/cli/*.py`
- `apps/api/src/alicebot_api/contracts.py`
- `apps/api/src/alicebot_api/_contracts/*.py`
- `apps/api/src/alicebot_api/main.py`
- `apps/api/src/alicebot_api/routers/*.py`
- `apps/api/src/alicebot_api/mcp_server.py`
- `apps/api/src/alicebot_api/mcp_tools.py`
- `apps/api/src/alicebot_api/mcp/*.py`
- `apps/web/lib/api.ts`

Invariants:

- shared request and response contracts across HTTP, CLI, MCP, and web client code must remain aligned
- non-additive surface changes must be called out explicitly, even for internal consumers
- defaults, enum values, and limit semantics must not drift across surfaces
- API-affecting changes must describe caller impact, validation coverage, and rollback posture

## CI Enforcement

GitHub Actions runs `scripts/check_protected_paths.py` on every pull request.
When a PR touches a protected path, the job requires:

- the matching protected areas to be checked in the PR body
- explicit notes for compatibility impact
- explicit notes for migration / rollout
- explicit notes for operator action
- explicit notes for validation
- explicit notes for rollback

If those fields are missing or left as placeholders, the guardrail job fails.
