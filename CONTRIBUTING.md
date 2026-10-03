# Contributing

Thanks for contributing to Alice.

## Scope Discipline

Keep each change narrowly scoped and avoid unrelated refactors. `RULES.md` defines the standing product and engineering rules.

## Local Setup

```bash
make setup
make migrate
make doctor
```

## Required Validation

Before opening a PR, run the same static, Python, LongMemEval, and web gates CI
uses:

```bash
make release-static
make test-python
make test-longmemeval
make test-web
git diff --check
```

`make test-python` retains the aggregate coverage floor and enforces a ratcheted
per-file floor for the production FastAPI module. `make test-longmemeval` is
model-free and validates the checked-in compact dataset manifest even when the
ignored 277 MB benchmark dataset is absent. `make test-web` includes unit,
per-file coverage, type, lint, production build, bundle-budget, real-browser,
and accessibility checks.

Changes that advertise Python 3.13 or 3.14 compatibility must also pass the
representative SQLite/config/MCP functional selection under those interpreters;
CI runs that matrix for every pull request.

The Postgres integration commands need the role-separated test environment
documented in `RELEASING.md`. CI runs the suite with the legacy surfaces
mounted, then runs the default-surface smoke with them unset:

```bash
ALICE_LEGACY_SURFACES=1 ./.venv/bin/python -m pytest tests/integration -q
env -u ALICE_LEGACY_SURFACES -u ALICE_MCP_LEGACY_TOOLS -u ALICE_AGENT_API_KEY \
  ./.venv/bin/python -m pytest tests/integration/test_default_surface_integration.py \
  tests/integration/test_openai_agents_sdk_tool.py -q --require-executed-tests
```

Both read `DATABASE_URL` (the app role) and `DATABASE_ADMIN_URL` (the admin
role). Without them, the tests use the same local roles as `.env.example`. The
database needs pgvector 0.8 or later. `make test-python` runs the first
command, so it needs that database too.

Run the Hermes bridge checks when the change touches the Hermes bridge:

```bash
./.venv/bin/python scripts/run_hermes_memory_provider_smoke.py
./.venv/bin/python scripts/run_hermes_mcp_smoke.py
./.venv/bin/python scripts/run_hermes_bridge_demo.py
```

For connector changes:

```bash
./.venv/bin/python -m pytest tests/unit/test_vnext_connectors.py -q
./.venv/bin/python -c 'from alicebot_api.cli import main; raise SystemExit(main(["vnext", "connectors", "list"]))'
```

## Pull Request Expectations

- Keep PR scope narrow.
- Update docs when behavior or command paths change.
- Include exact commands executed and pass/fail evidence.
- If browser checks fail, include the retained Playwright trace/report artifact
  from CI when diagnosing the failure.
- Complete the protected-path `Upgrade Overview` when the PR touches paths listed in `PROTECTED_PATHS.md`.
- Do not introduce claims that outrun shipped functionality.
- Use synthetic fixtures only for public demo data.

## Architecture and Rules

Read before making non-trivial changes:

- `ARCHITECTURE.md`
- `RULES.md`
- `PROTECTED_PATHS.md`
- `docs/vnext/contributor-guide.md`
