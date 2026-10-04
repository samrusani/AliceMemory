# Secrets, Keys, Logging, And Redaction Evidence

## Agent API Keys

`alicebot agent keys create` mints an `alice_sk_...` value and returns it once.
The stores persist its SHA-256 verifier and a short prefix for identification,
not the raw key. Verification uses constant-time digest comparison; revocation
and last-used state live on the key record. Treat the creation response and
terminal history as sensitive, and rotate a key if its one-time value was not
captured safely.

The code path and unit tests prove hash-only store input. The Phase 5 carrier
adds HTTP and role-separated database sentinels for invalid/foreign keys, public
responses, logs, events, and cross-user visibility; existing escalation tests
also inspect audit payloads. Final acceptance requires those tests to pass on
the exact carrier. A future external review should still probe successful,
escalation, and internal-error paths end to end rather than infer universal non-disclosure from
the bounded sentinels.

## Browser Clipper Capability

The bookmarklet must never receive a reusable agent key or connector
`capture_token`. The trusted Alice UI issues a random, short-lived capability
bound to one user and normalized web origin. Persistence contains only the
capability hash, expiry, and consumption state. Redemption requires a matching
non-opaque `Origin`. The first authorized capture attempt consumes the row in
the capture transaction even when later content normalization or import fails;
retrying therefore requires a freshly issued capability.

The final carrier acceptance suite must prove valid first use; replay/reload,
other-origin, expired, and tampered rejection; exactly one winner under
concurrent redemption; and absence of raw capabilities from source metadata,
events, traces, error bodies, logs, documentation examples, and OpenAPI examples.

## Provider And Connector Secrets

- Current provider registration stages credentials in the local provider secret
  manager and persists a reference in the provider row. Hosted-era provider
  rows are not adopted into the deterministic local workspace; re-register them
  after upgrade.
- Connector configuration persists `secret_ref` identifiers. The composite
  provider can resolve environment-backed refs or an owner-only encrypted local
  file. Secret-shaped fields are recursively replaced with `***` before
  connector status/event output.
- Provider runtime errors are normalized before public response/telemetry. Base
  URL validation rejects embedded credentials and disallowed local/private
  network targets.
- Legacy plaintext provider-field resolution remains for migration
  compatibility. Do not create new plaintext rows; re-register or rotate legacy
  provider configurations onto the current secret-reference path.

The Phase 5 carrier extends the live provider integration test so the exact
configured credential is observed at the stub transport and proven absent from
successful public payloads, durable telemetry, and captured logs. Final
acceptance requires that test and the provider failure-family sanitization tests
to pass on the exact carrier; a future external review should still test
unexpected exception paths.

## Public Error Boundary

`public_exception_response` maps exceptions to a fixed public vocabulary and
keeps exception type/text out of the serialized body. An AST gate scans
`main.py` and every router, detects direct and delayed `str(exc)` response
patterns, and pins a per-module call manifest in
`tests/unit/test_public_errors.py`.

Private logs can still contain exception detail for operator diagnosis. Protect
log files as sensitive data, restrict access and retention, and never rely on
the public response scrubber to make unsafe exception messages suitable for
logs.

## Redaction Limit

True redaction scrubs governed content from durable memories and coupled
artifacts, revisions, events, ratings, and provenance while retaining the
minimal audit skeleton. PostgreSQL and SQLite integration/unit suites verify the
returned and persisted result.

Some authorization and bundle-building paths read the pre-redaction row into
Python memory before the transactional scrub. This is result-safe but extends
plaintext lifetime in RAM until objects are released and the process memory is
reused. Alice does not claim memory-zeroization guarantees; use host isolation,
swap/dump policy, and process lifecycle controls for highly sensitive data.

## Focused Evidence

```bash
./.venv/bin/pytest -q \
  tests/unit/test_vnext_agent_keys.py \
  tests/unit/test_stage_a_vnext_auth_surface.py \
  tests/unit/test_browser_clip_capabilities.py \
  tests/unit/test_browser_clip_capability_storage.py \
  tests/unit/test_vnext_secrets.py \
  tests/unit/test_provider_secrets.py \
  tests/unit/test_provider_security.py \
  tests/unit/test_public_errors.py \
  tests/integration/test_api_logging_smoke.py \
  tests/integration/test_stage_a_agent_key_isolation.py \
  tests/integration/test_browser_clip_capabilities.py \
  tests/integration/test_provider_runtime_api.py
```

The role-separated true-redaction integration suite and the final-carrier
browser-capability tests are also required. Skips are missing evidence.
