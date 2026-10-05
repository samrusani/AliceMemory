# Authentication And Authorization Evidence

## Shipped Policy

Alice has two supported HTTP postures:

1. **Keyless local-owner mode:** while a user has zero active agent API keys,
   local vNext compatibility accepts owner-trusted calls. The supplied `user_id`
   is not proof of identity, so the API must remain on loopback and inaccessible
   to untrusted local processes.
2. **Keyed mode:** once any active key exists for that user, protected vNext
   requests require a valid `Authorization: Bearer alice_sk_...` key. The key
   record supplies the user, agent identity, maximum permission profile, and
   optional project binding; payload claims cannot change the actor or widen
   privilege/scope.

The one-time browser-clipper capability is scoped to its capture endpoint,
user, origin, expiry, and first redemption. It is not a keyless bypass for any
other operation.

## HTTP Authorization Layers

- `alicebot_api.main` applies the centralized vNext authentication boundary
  before handler dispatch and maintains an exact route classification.
- Route-local policy covers target-aware operations; central operator routes
  require an unbound `trusted_local_agent` or `admin_agent` key in keyed mode.
- Human/admin review decisions retain their stricter action policy; a trusted
  local profile is not automatically an admin review identity.
- Persisted memory, artifact, source, and project scope is authoritative for
  target reads and mutations. Caller metadata cannot relabel a persisted target
  into scope. Unreleased (on main, not in v0.20.0): the persisted labels of a
  derived row are held at or above those of its inputs.
- Authentication failures use 401; authenticated but unauthorized actions use
  403 and stable public response families.

At the Phase 5 base the route inventory was 70 vNext operations. The clipper
issuer makes the frozen carrier inventory 71: 33 route-local-policy and 38
central-operator operations. The focused exact-set and all-route ASGI tests
reproduced this split.

## Store Boundaries

- PostgreSQL runtime connections use `alicebot_app`; user transactions set the
  application principal consumed by forced RLS policies. Cross-user access is
  expected to return no row or reject the mutation.
- `DATABASE_ADMIN_URL` is for migrations and explicit administration, not
  request handling.
- SQLite is a single-user local on-ramp. It mirrors policy behavior in service
  code and protects the database/sidecars with owner-only permissions, but it
  is not a multi-user RLS boundary.

## MCP And Legacy Surfaces

- The default registry exposes three core MCP tools: `alice_memory_commit`,
  `alice_recall`, and `alice_resume`. `ALICE_MCP_FULL_TOOLS=1` exposes all
  eleven.
- A key-bound server uses `ALICE_AGENT_API_KEY`; payload identity cannot replace
  that key's actor or widen its profile/project scope.
- Legacy MCP handlers do not all have the same persisted-target authorization
  contract, so a key-bound MCP server suppresses them.
- `ALICE_LEGACY_SURFACES` is off by default and gates 49 legacy HTTP operations.
  It is read at import time; restart after changing it.

## Repository Evidence

| Claim | Evidence |
| --- | --- |
| Raw key is verified against a hash, bound to one user, and revocation is honored | `apps/api/src/alicebot_api/vnext_agent_keys.py`; `tests/unit/test_vnext_agent_keys.py` |
| Profile and project escalation are rejected and audited | `tests/unit/test_vnext_agent_keys.py`; `tests/unit/test_vnext_main.py` |
| Keyless calls are accepted only before key provisioning, then rejected | `tests/unit/test_vnext_agent_keys.py`; `tests/unit/test_vnext_main.py` |
| Central routes fail closed unless classified and require unbound trusted/admin actors | `tests/unit/test_vnext_main.py` |
| Default and legacy route sets are exact | `tests/integration/test_default_surface_integration.py`; `tests/unit/test_legacy_gated_router_split.py` |
| MCP key binding, core registry, and legacy suppression remain exact | `tests/unit/test_mcp.py`; `tests/integration/test_mcp_server.py` |
| Cross-user database visibility is constrained | role-separated PostgreSQL integration suites and migration RLS assertions |

## Carrier Acceptance And Gaps

The Phase 5 carrier adds `tests/unit/test_stage_a_vnext_auth_surface.py`, a
generated all-route matrix proving that, once a key exists, each registered
vNext operation reaches the central boundary and rejects a missing credential
before handler behavior. It passed on the frozen carrier alongside tests proving
that the capability issuer is central-operator-only and capability redemption
cannot authorize a different route or user.

Run the focused commands in [the Stage A ledger](stage-a-evidence.md), then the
full role-separated integration matrix. A skipped database or MCP posture is a
proof gap, not a passing result.
