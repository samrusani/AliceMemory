# Local Setup and First Result (Pointer)

The canonical setup walkthrough lives at [docs/alpha/quickstart.md](../alpha/quickstart.md).

The default setup is one command, `uvx alice-memory install`. It uses one local SQLite file and needs no Docker or Postgres.

For the Postgres stack, the short version is below. The clone checks out `main`, which can be ahead of the latest release. To run v0.19.2, run `git checkout v0.19.2` before `make setup`.

```bash
git clone https://github.com/samrusani/AliceMemory.git
cd AliceMemory
make setup
make migrate
make doctor
make dev
```

## Alice Lite (Smaller Postgres Profile)

The smallest setup is the SQLite install above: one file, no Docker or Postgres. Alice Lite is a smaller Postgres deployment profile of the same system, not a separate product. Use it when you want the Postgres stack with a smaller footprint:

```bash
./scripts/alice_lite_up.sh
```

This starts the Lite Postgres profile, runs migrations, loads the sample fixture, and runs the API with stdout-only logging. Then, in another terminal:

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS -X POST http://127.0.0.1:8000/v1/workspaces/bootstrap \
  -H "X-AliceBot-User-Id: ${ALICEBOT_AUTH_USER_ID:-00000000-0000-0000-0000-000000000001}"
./.venv/bin/python -m alicebot_api brief --brief-type general --query "local-first startup path"
./.venv/bin/python scripts/run_alice_lite_smoke.py
```

## Logging

Local and Lite runs log to stdout only by default, and access logs stay off. To opt into bounded file logging instead:

```bash
APP_LOG_MODE=file
APP_LOG_PATH=/var/log/alicebot/api.log
APP_LOG_MAX_BYTES=10485760
APP_LOG_BACKUP_COUNT=5
```

File logging rotates at the configured size and keeps the configured number of backups, so logs cannot grow without bound.

## Importers

To load existing data instead of starting from zero:

```bash
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_v1.json
./scripts/load_markdown_sample_data.sh --source fixtures/importers/markdown/workspace_v1.md
./scripts/load_chatgpt_sample_data.sh --source fixtures/importers/chatgpt/workspace_v1.json
```

Repeating a command verifies deterministic dedupe (`status=noop`, duplicate skips). Details: [docs/integrations/importers.md](../integrations/importers.md).
