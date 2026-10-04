# Azure + AutoGen Integration

Azure is a retained provider adapter. AutoGen orchestration stays outside Alice
and may call the normalized `/v1/runtime/invoke` boundary or, preferably, use
Alice's core MCP memory tools alongside its own model client.

Use `X-AliceBot-User-Id` (or `ALICEBOT_AUTH_USER_ID`) for the local operator;
there is no hosted Alice session or model-pack seam.

```bash
export ALICE_USER_ID=00000000-0000-0000-0000-000000000001
curl -sS -X POST http://127.0.0.1:8000/v1/providers/azure/register \
  -H "X-AliceBot-User-Id: $ALICE_USER_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name":"Azure Primary",
    "base_url":"https://YOUR_RESOURCE.openai.azure.com",
    "auth_mode":"azure_api_key",
    "api_key":"'"$AZURE_API_KEY"'",
    "api_version":"2024-10-21",
    "default_model":"gpt-4.1-mini"
  }'
```

Provider credentials are stored as secret references and never returned in
plaintext. Test with `POST /v1/providers/test`; invoke with
`POST /v1/runtime/invoke` plus one stable `Idempotency-Key` per logical call.
The removed `/v0/responses` endpoint is not an AutoGen chat backend.

To send one AutoGen-style turn through `POST /v1/runtime/invoke` for a registered provider, run the command below. The script reads `ALICEBOT_AUTH_USER_ID`, or takes `--user-id`.

```bash
./.venv/bin/python scripts/run_phase11_autogen_runtime_bridge.py --provider-id <id> --thread-id <thread> --user-message "<text>"
```
