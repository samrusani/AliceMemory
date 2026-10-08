# Doctor-first Onboarding

Run doctor before dogfooding and before asking an agent to rely on Alice.

```bash
alicebot vnext doctor --fix-safe --ci
```

Doctor checks:

- required vNext migration tables
- connector settings rows
- connector state rows
- surviving connector secret-reference posture
- scheduler daemon posture
- connector failure posture
- local `/vnext?mode=live` CORS posture when a browser API URL is configured

Expected success:

- `blocking_failure_count` is `0`
- status is `pass` or `warn`
- warnings include a recommended fix
- no secret value appears in output

Unreleased (on main, not in v0.20.0): the doctors report `derived labels: N below
their inputs, M unverified` as a warning. If the label check cannot read the
store, they report `derived labels: unavailable; run labels check`, also as a
warning. A failed read is never reported as zero rows. Run the store's `labels
check` command to inspect the cause, then `labels repair` for stale labels;
missing inputs need restoring or regeneration.

Unreleased (on main, not in v0.20.0): two checks read the content of the vault, `flagged_sources` (the ids of
stored sources that carry credential material) and `derived_labels` (the counts above). `GET /v0/vnext/doctor` and
`POST /v0/vnext/doctor/run` run them only for the owner and an unbound admin key. A trusted key passes the operator
gate with a ceiling below confidential, so it receives both with status `skipped`, the message "Content diagnostics
are available to the owner and an unbound admin key." and no id, count or text taken from the vault. Its `status`,
`warning_count` and `recommended_fixes` leave them out, and every other check is the same for every caller. Other
profiles and project-bound keys are refused by the gate (HTTP 403). The `doctor` field of the workspace response
follows the same rule. The `alicebot vnext doctor` command reads the local vault as its owner and is not limited.
In v0.20.0 the routes returned the `flagged_sources` ids to a trusted key.

Common fixes:

```bash
./scripts/migrate.sh
alicebot vnext doctor --fix-safe --ci
alicebot vnext connectors health
alicebot vnext scheduler daemon start --foreground --once
CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
```

Use the broader alpha gate when doctor is clean:

```bash
alicebot vnext alpha check
```
