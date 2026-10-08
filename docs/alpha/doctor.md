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

### The workspace shows the last full labels check

Unreleased (on main, not in v0.20.0): a full derived-labels check settles every
derived row, which costs more than a workspace page load can afford (about 0.6
seconds on a vault with 5,000 memories and 3,000 sources). The doctor command
(`alicebot vnext doctor`), `alicebot vnext labels check` and `alicebot vnext
labels repair` run the full check and record its result. The owner and an
unbound admin key see that most recent result in the workspace page, instead of
a new full check on each request:

- The workspace line reads `derived labels: N below their inputs, M unverified
  (recorded by labels check at <time>)`. The cause is `labels check`, `labels
  repair` or `doctor`, and the time is when that command finished.
- The record also holds a digest of every row the check read. The workspace
  compares it with the current rows. When they differ, the line adds `The label
  inputs have changed since this check; run alicebot vnext labels check for a
  current result.` The recorded counts stay as they were.
- Before any command has run on a vault there is no record. The workspace then
  reports `derived labels: no recorded check; run alicebot vnext labels check`
  with status `skipped`. A record the workspace cannot read is reported as
  unreadable, never as zero rows.
- The recorded result is an event named `labels.checked` in the audit log. It has
  counts, a cause and a digest, and no row text. Restricted keys never list it,
  and their workspace omits the derived-labels line as before.
- `GET /v0/vnext/doctor` and `POST /v0/vnext/doctor/run` stay live full checks
  and do not record. The workspace page never decides who may read a row: every
  guarded read still settles labels for the request itself.
- If the record cannot be written, the command prints `result was not recorded`
  on its error stream. The check result it reports is unchanged.

To refresh the page's line after a change, run `alicebot vnext labels check`.

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
