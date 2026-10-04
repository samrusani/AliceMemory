# Provider Configuration (OpenAI-Compatible)

This guide covers the provider foundation paths:

- `POST /v1/providers`
- `PATCH /v1/providers/{provider_id}`
- `POST /v1/providers/test`
- `POST /v1/runtime/invoke`
- workspace bootstrap seeding through `WORKSPACE_PROVIDER_CONFIGS_JSON`
- `scripts/run_phase14_openai_compatible_smoke.py`

Scope note: this page documents the OpenAI-compatible foundation path.

> **Upgrading from the hosted workspace model:** hosted-era provider rows are
> tied to their old workspace identity and are orphaned from the deterministic
> local workspace after upgrade. Bootstrap the local workspace and re-register
> each provider; do not expect the retained provider API to adopt or rewrite
> those historical rows.

The smoke helper uses the same local identity and bootstraps its deterministic
workspace before touching provider endpoints. With no provider URL it starts a
temporary local OpenAI-compatible stub:

```bash
./.venv/bin/python scripts/run_phase14_openai_compatible_smoke.py \
  --user-id "$ALICE_USER_ID" \
  --thread-id "$THREAD_ID"
```

## API Registration

Register an OpenAI-compatible provider in the current workspace:

Set `ALICE_USER_ID` to the local operator UUID. The retained provider routes use
`X-AliceBot-User-Id` (or `ALICEBOT_AUTH_USER_ID`), not a hosted bearer session.

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/workspaces/bootstrap" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{}'

curl -sS -X POST "http://127.0.0.1:8000/v1/providers" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_key": "openai_compatible",
    "display_name": "Primary OpenAI-Compatible",
    "base_url": "https://provider.example/v1",
    "api_key": "'"$PROVIDER_API_KEY"'",
    "default_model": "gpt-5-mini"
  }'
```

Capability discovery runs during registration and stores:

- `health_status`
- `health_endpoint`
- `models_endpoint`
- `invoke_endpoint`
- `model_count`
- `models`
- `supports_reasoning`

## Config Registration

Workspace bootstrap can seed OpenAI-compatible providers from config with `WORKSPACE_PROVIDER_CONFIGS_JSON`.

The local/self-hosted adapter surface also allows dedicated `vllm` entries in the same config surface. Keep using this page for the OpenAI-compatible path, and use `docs/integrations/phase11-local-provider-adapters.md` for the dedicated vLLM adapter defaults and registration flow. For the Azure adapter with AutoGen, see `docs/integrations/phase11-azure-autogen.md`.

Example:

```bash
export WORKSPACE_PROVIDER_CONFIGS_JSON='[
  {
    "provider_key": "openai_compatible",
    "display_name": "Configured OpenAI-Compatible",
    "base_url": "https://provider.example/v1",
    "api_key": "provider-secret-key",
    "default_model": "gpt-5-mini",
    "model_list_path": "/models",
    "healthcheck_path": "/models",
    "invoke_path": "/responses",
    "metadata": {
      "source": "workspace_bootstrap"
    }
  }
]'
```

Then bootstrap the deterministic local workspace normally:

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/workspaces/bootstrap" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Providers from config are seeded once per workspace when bootstrap completes. Existing providers with the same `provider_key` and `display_name` are left in place.

## Provider Updates

Update provider configuration and refresh capability discovery:

```bash
curl -sS -X PATCH "http://127.0.0.1:8000/v1/providers/$PROVIDER_ID" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Updated OpenAI-Compatible",
    "base_url": "https://provider.example/v1",
    "default_model": "gpt-4.1-mini",
    "model_list_path": "/models",
    "healthcheck_path": "/models",
    "invoke_path": "/responses"
  }'
```

For bearer-auth OpenAI-compatible providers, send `api_key` when rotating credentials.

## Provider Test

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/providers/test" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "'"$PROVIDER_ID"'",
    "prompt": "Reply with one sentence confirming connectivity."
  }'
```

The provider-test flow persists normalized invocation telemetry with status, latency, response ID, usage, and error detail.

## Runtime Invoke

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/runtime/invoke" \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Idempotency-Key: provider-runtime-$(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "'"$PROVIDER_ID"'",
    "thread_id": "'"$THREAD_ID"'",
    "message": "Summarize runtime status in one sentence."
  }'
```

One-call continuity still runs through the normal continuity compiler and
response trace path. The provider layer adds capability discovery, durable
idempotency, and invocation telemetry without changing continuity semantics.
The public `/v0/responses` chat endpoint is not part of this boundary.
