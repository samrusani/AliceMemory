# Health And Monitoring

Alice exposes several useful signals, but no single endpoint proves the whole
system healthy. Monitor each signal for what it actually checks.

## `/healthz`

`GET /healthz` performs a bounded PostgreSQL connectivity check. It returns
HTTP 200 with top-level status `ok` when that check succeeds and HTTP 503 with
status `degraded` when it fails.

The response also names Redis and object storage as `not_checked`. Those are
truth labels, not successful checks. Do not turn `/healthz` into an all-services
availability claim, and do not alert on the redacted Redis URL as though it
were a probe result.

Recommended alerts:

- immediate: `/healthz` returns 503 or times out for two consecutive probes;
- capacity: database connections, disk space, WAL growth, and backup age cross
  operator-defined thresholds;
- release drift: `alembic_version.version_num` differs from the dynamic head in
  the deployed Alice artifact.

The SQLite MCP on-ramp does not use this PostgreSQL endpoint. Monitor its
process, database-file availability, free disk space, backup age, and periodic
`PRAGMA integrity_check` instead.

## Doctor and connector signals

`GET /v0/vnext/doctor?ci=true` and `POST /v0/vnext/doctor/run` provide the
application readiness checks. `GET /v0/vnext/connectors/health` reports
connector telemetry. These vNext routes obey the normal Alice user/agent-key
boundary; monitoring clients must authenticate once keys exist. A monitoring key with a
ceiling (a trusted key) receives the `flagged_sources` and `derived_labels` checks as
`skipped`; the owner and an unbound admin key receive them (see
[Doctor](../alpha/doctor.md)).

Treat a doctor `fail` or nonzero blocking-failure count as an operator action.
A `warn` is not automatically an outage: inspect the named check and its
recommended fix. Connector counters and timestamps prove only Alice's view of
that connector; they are not third-party provider SLAs.

## Scheduler status and a stuck scheduler

The scheduler daemon writes its status file (normally under Alice's runtime
directory), while the vNext scheduler status reports durable workflows and
runs. Watch both.

A scheduler is **stuck** when it reports that it should be running but either:

- `ownership_verified` is false, so the PID/status/owner records do not bind to
  the same live process; or
- `last_heartbeat_at` is missing or older than the greater of 60 seconds and
  three times `interval_seconds`.

Also alert as degraded when `last_error_code` is nonempty or expired claims are
observed (`expired_claim_count` or a nonzero reaped-claim result). Those may be
recoverable rather than stuck, but they require an operator to inspect the
recent durable run and event rows. A `started` run whose claim lease expires is
not success; Alice fences and reaps expired claims.

Useful commands:

```bash
alicebot vnext scheduler daemon status
alicebot vnext scheduler status
alicebot vnext doctor --ci
```

The exact CLI spelling is visible in `alicebot vnext scheduler --help` for the
installed release. Do not delete PID, owner, or status files to manufacture a
green status; stop the daemon through its command, preserve the files for
diagnosis, then restart.

## Executed monitoring contract

The Phase 5 evidence script executes both health payload branches and synthetic
scheduler cases covering healthy, disabled, degraded, and stuck states:

```bash
./.venv/bin/python scripts/run_phase5_ops_evidence.py --backend sqlite
```

Its sanitized `health_and_monitoring` receipt proves the response labels and
stuck-classification fields. It does not prove an external alerting system,
network path, provider availability, or a production scheduler process; those
remain deployment responsibilities.

## Minimum operator dashboard

Track at least:

- API process and `/healthz` status/latency;
- dynamic Alembic head versus deployed database revision;
- scheduler `running`, `ownership_verified`, heartbeat age, last error, due
  work, expired/reaped claims, and recent failed runs;
- disk/database size, free space, connection saturation, and PostgreSQL WAL;
- last successful backup time, backup hash, off-machine copy age, and last
  successful restore-drill time;
- connector last-success/last-error counters when connectors are enabled.

Exclude database URLs, API/provider keys, raw memory text, prompts, and trace
payloads from metrics labels and logs.
