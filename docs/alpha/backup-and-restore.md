# Backup And Restore

Back up Alice before upgrading, changing embedding providers, or performing
bulk lifecycle work. Test a restore before treating any file as a backup.

## SQLite on-ramp

Use the packaged export command with `--out`. It verifies and copies a stable
read-only replica of the source database and active WAL into a private
temporary directory, then takes a consistent SQLite online-backup snapshot
from that replica. Any compatible bootstrap upgrade applies only to the
private snapshot. Export does not bootstrap, upgrade, chmod, or open the live
source through SQLite, and it does not change source schema, logical content,
file bytes (including the volatile `-shm` file), or permissions. Ordinary
filesystem reads may still update access-time metadata. A source that stays
busy through the bounded snapshot retries fails clearly so you can quiesce
writers and retry. An unknown `--user-id`, corrupt source, unknown column, or
unknown application table fails before any JSONL is published. This prevents
an older Alice exporter from silently dropping user-owned state introduced by
a newer schema.

The versioned JSONL is written through a `0600` sibling temporary file,
`fsync`, and atomic replacement:

```bash
alice-memory export \
  --data-dir ~/.alice \
  --out ~/alice-backups/alice-$(date +%Y%m%d-%H%M%S).jsonl
```

The JSONL contains sensitive plaintext. `0600` limits local file access but
does not encrypt the backup; use encrypted storage and protect off-machine
copies according to your threat model.

Always use `--out` for a durable backup. Shell-created files inherit the
shell's permissions and are not atomically replaced. More importantly,
**never redirect stdout to the SQLite database or its `-wal`, `-shm`, or
`-journal` sidecars**: the shell truncates its destination before
`alice-memory` starts, so no in-process alias check can prevent data loss.

A v2 backup contains a schema version and fingerprint, per-record counts, and
a SHA-256 footer over the canonical portable data records. That digest is
stable across export, fresh import, and re-export when the records are
unchanged; volatile manifest fields such as `exported_at` are not part of the
digest. Import first copies the selected file from one stable source handle
into an owner-only private snapshot. It validates and decodes that immutable
snapshot once, then reuses the parsed, FK-ordered records during restore.
Validated records are streamed through an owner-only disk spool, so restore
memory stays bounded instead of retaining the complete decoded graph in RAM.
Replacing the original path after import starts cannot substitute different
bytes between validation and insertion.
Restore into a new path first:

```bash
alice-memory import \
  --db ~/alice-restore-test/memory.db \
  --in ~/alice-backups/alice-20260711-120000.jsonl

alice-memory mcp --db ~/alice-restore-test/memory.db
```

Export output and import input paths are rejected if they lexically name, or
resolve/link to, the database itself or its `-wal`, `-shm`, or `-journal`
sidecars.

Every restore is built and schema-upgraded in a private staged database.
New-target restore uses atomic no-clobber publication and will not replace a
target that appears concurrently. Existing-target restore publishes staged
schema and data together through SQLite's backup write transaction; an error
leaves the original target's data and schema intact. Quiesce writers while
restoring an existing target, because restore semantics intentionally publish
the staged snapshot as the new database state. `--mode skip` skips only
field-for-field identical IDs; different content with the same ID aborts
instead of merging incompatible snapshots. `--mode fail` aborts on every
collision. Exit `0` means restore and post-commit reporting/hardening
completed; exit `1` means publication did not occur. Exit `2` means the
publication committed but a post-commit condition or reporting step failed.
The records are present: inspect stderr and the target path, and do not
blindly retry.

`--quarantine` is the owner's recovery path when a backup holds a credential
and the source vault is gone. It removes the credential from the named
memory and from the records derived from it, and it reports any other copies
it finds. `--db` is a SQLite file path. A Postgres URL is refused and no
file is written.

```bash
alice-memory import \
  --db ~/alice-restore-test/memory.db \
  --in ~/alice-backups/alice-20260711-120000.jsonl \
  --quarantine <memory_id>[,<memory_id>...]
```

The SHA-256 footer is checked on the file exactly as given, before any text
is replaced. A tampered file fails the same way it does without the flag,
including when the flag names an id that is not in the file. An id that is
not a memory record in a valid file is an error and nothing is written.

Each named memory is stored with status `rejected`. Recall, resume, and a
context pack do not return that row. The row is kept. What survives on that
row is its ids, its status, its timestamps, and numeric columns outside
JSON. `memory_key` becomes `quarantined.<memory_id>`. `commit_digest` is
cleared, matching product redaction, so a later commit with the old
idempotency key creates a fresh row through the normal checks.
`extracted_by_model` is replaced. Title, canonical text, summary, trust
reason, and fact keys become the fixed placeholder
`[quarantined on import]`. `value` and `metadata_json` become
`{"quarantined": true}`.

The same placeholder replaces `text_before`, `text_after`, and `reason` on
every revision of that memory, and that revision's `memory_key` becomes
`quarantined.<memory_id>`. The four revision JSON columns (`previous_value`,
`new_value`, `candidate`, and `metadata_json`) become
`{"quarantined": true}` when they were present, and stay null when they were
null. Revision rows stay, because `memory_revisions.memory_id` is a required
foreign key.

Every event that belongs to the memory has its payload replaced with
`{"quarantined": true}`. The payload keeps `memory_id` and
`candidate_memory_id` when those values are a quarantined id, so explain
and a later redact can still link the event. An event belongs to the memory
when its target is that memory, or when its payload `memory_id`,
`candidate_memory_id`, or `replacement_memory_id` is that memory's id.
`integrity_hash` on those events is cleared. It is a SHA-256 of the event
record, including the payload, so a reconstructed original payload could be
checked against a kept hash.

Records that exist only because of the named memory are rewritten with the
same placeholder, and each is counted on the receipt:

- `provenance_links.quote`, where `target_type` is `memory` and the target
  is a named id
- that memory's open loops (title, description, and resolution note)
- graph edges that touch it (the explanation, and metadata set to
  `{"quarantined": true}`)
- names of entities linked only to quarantined memories (`name`,
  `normalized_name`, and `aliases`)
- rollup instance entries whose `memory_id` is a named id
- on a successor, the copied `rationale`, `idempotency_key`, and
  `request_fingerprint`. The successor's own title and text stay.

Shared source chunks are not rewritten. Shared entity names are not
rewritten. After a successful import, `credential_verdict` runs over every
imported text column that was not replaced by `[quarantined on import]` or
`{"quarantined": true}`. The receipt prints table, id, and column for each
hit, and for each shared source chunk and shared entity name, then the
command that removes that record, or `no command removes this today`. It
does not print the matched text.

On success the exit code is 0. The receipt lists the quarantined ids and
the counts. It does not print the removed text.

A second import of the same file with the same `--quarantine` list follows
`--mode skip` (the default). The stored rows already match the redacted
records, so they are skipped, the command exits 0, and the database is
unchanged. `--mode fail` still aborts when any id already exists, including
a quarantined row. Importing that same file again without `--quarantine`
aborts and writes nothing. The file still carries the credential, so the
credential refusal fires before the collision check. Existing rows are
never overwritten. The rejected row and the placeholder stay.

This command restores a SQLite database. It is not a PostgreSQL import.

Portable backups include active sources and chunks, memories and fact keys,
revisions, provenance, entities, graph edges, entity relationship events,
open loops, and the event log. They intentionally omit users, agent API keys,
embedding vectors, and soft-deleted content. References from retained rows to
omitted soft-deleted parents are nulled where nullable; graph edges whose
known endpoints were omitted are excluded. Historical event ids, timestamps,
and integrity hashes are inserted verbatim, except an event quarantined with
a memory: its payload is replaced and its integrity hash is cleared.
The restored rows are rebound to
the importing local user. Configure the intended embedding endpoint and run:

```bash
alice-memory reindex-embeddings --db /path/to/restored-memory.db
```

Portable JSONL does not contain embedding vectors. A successful import and FTS
recall therefore do not prove vector-search readiness; reindex against the
intended provider and verify its signed-vector coverage before cutover.

For PostgreSQL upgrades or model changes, use
`alicebot vnext memories backfill-embeddings`; it rebuilds missing, unsigned,
and provider/model-incompatible vectors.

Legacy headerless exports remain importable but cannot prove that a
syntactically complete file was not truncated.

## PostgreSQL

The SQLite JSONL command is not a PostgreSQL disaster-recovery tool. Use the
PostgreSQL utilities through a dedicated backup identity and protect the
resulting file as sensitive plaintext. Tables use forced row-level security,
so the database owner cannot produce a complete dump unless it also holds
`BYPASSRLS`. Do not add that privilege to `alicebot_admin`.

Create a separate `alicebot_backup` login as a PostgreSQL superuser or other
role permitted to manage role attributes. It must be a non-superuser with
`BYPASSRLS`, no database-creation privilege, and no role-creation privilege:

```sql
CREATE ROLE alicebot_backup
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT CONNECT ON DATABASE alicebot TO alicebot_backup;
```

On the Alice database, grant only the object reads needed by `pg_dump`. The
default-privilege grants keep later `alicebot_admin` migrations dumpable:

```sql
GRANT USAGE ON SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON TABLES TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO alicebot_backup;
```

`BYPASSRLS` lets this role read every Alice row. Restrict its login source,
rotate and protect its credential, and use it only for backups. It must not run
migrations or restores. A same-host operator can instead run `pg_dump` as the
local PostgreSQL superuser through Unix-socket peer authentication. That
alternative avoids a stored backup credential, but it applies only when the
database and backup process share the host.

Use the dedicated role for the dump and pass `--no-comments`, which omits all
object comments from the archive. This includes extension comments that would
otherwise require extension ownership during a least-privilege restore:

```bash
export PGHOST=db.internal PGPORT=5432 PGUSER=alicebot_backup PGDATABASE=alicebot
export PGSSLMODE=verify-full
export PGSSLROOTCERT=/etc/alicebot/postgres-ca.pem
export PGPASSWORD='from-your-secret-manager'
pg_dump --format=custom --no-comments --file=alice.dump
pg_restore --list alice.dump
```

Using libpq environment variables keeps the credentialed DSN out of process
arguments. Protect the environment and unset `PGPASSWORD` after the command.

Restore as `alicebot_admin`, which must remain `NOSUPERUSER`, `NOCREATEDB`, and
`NOBYPASSRLS`. The target must already contain compatible `pgcrypto` and
`vector` extensions installed by a database operator. The automated disposable
drill inherits them from `template1`; production restore targets can instead
have them provisioned before the restore. Use `--no-comments` on restore too,
so an older archive containing extension comments does not fail:

```bash
export PGUSER=alicebot_drill PGDATABASE=postgres
export PGPASSWORD='drill-password-from-your-secret-manager'
createdb alice_restore_test
psql --dbname=postgres --set=ON_ERROR_STOP=1 <<'SQL'
GRANT CONNECT, CREATE ON DATABASE alice_restore_test TO alicebot_admin;
GRANT CONNECT, TEMPORARY ON DATABASE alice_restore_test TO alicebot_app;
GRANT CONNECT ON DATABASE alice_restore_test TO alicebot_backup;
SQL
psql --dbname=alice_restore_test --set=ON_ERROR_STOP=1 <<'SQL'
GRANT USAGE, CREATE ON SCHEMA public
  TO alicebot_admin WITH GRANT OPTION;
GRANT USAGE ON SCHEMA public TO alicebot_app, alicebot_backup;
SQL
export PGUSER=alicebot_admin PGDATABASE=alice_restore_test
export PGPASSWORD='admin-password-from-your-secret-manager'
umask 077
pg_restore --list alice.dump > alice.restore.full.list
public_schema_acl_count="$(
  awk '$4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public" {
    count++
  } END { print count + 0 }' alice.restore.full.list
)"
test "${public_schema_acl_count}" -eq 1
awk '$4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public" {
  next
} { print }' alice.restore.full.list > alice.restore.list
pg_restore --exit-on-error --no-owner --no-comments \
  --use-list=alice.restore.list \
  --dbname=alice_restore_test alice.dump
```

The create and grant commands are lifecycle steps, not `alicebot_admin`
capabilities. Apply the same Alice release's migrations, then run integration
and application smoke tests before cutover. Database roles, extensions, and
grants are deployment concerns; record them alongside the backup without
committing credentials. For a production deployment, add scheduled backups,
retention, off-machine encrypted copies, and periodic restore drills
appropriate to the operator's recovery objectives.

The restore list removes exactly the `ACL - SCHEMA public` entry. The lifecycle
step reconstructs the public-schema privileges for admin, app, and backup on
the fresh target. Table, sequence, and non-public schema ACL entries remain in
the restore list. Do not replace it with `--no-acl`; that would discard the
migration-defined application privileges the restored service needs.

## Upgrade checkpoint

Before `make migrate` on an existing installation:

1. record the running Alice version and source SHA;
2. create and hash a backup;
3. restore it into a disposable target and read representative memories;
4. run the upgrade on that restored copy;
5. verify capture, review/correction, recall, and export before upgrading the
   live database.

## Executed Phase 5 evidence

The repository drill performs a quiesced SQLite physical copy/restore, a
portable export/import/re-export fidelity check, a v0.12.0-to-current upgrade,
and a disposable PostgreSQL dump/restore and migration upgrade:

```bash
./.venv/bin/python scripts/run_phase5_ops_evidence.py --backend all \
  --output artifacts/phase5/ops-evidence.json
```

PostgreSQL mode requires four role-separated URLs supplied through the
environment:

- `DATABASE_LIFECYCLE_URL` is used by the drill only to create, grant, and drop
  its random disposable databases. The required `alicebot_drill` role needs
  `CREATEDB` but remains `NOSUPERUSER`, `NOCREATEROLE`, and `NOBYPASSRLS`.
- `DATABASE_ADMIN_URL` runs migrations, verification, and `pg_restore` as
  `alicebot_admin`.
- `DATABASE_URL` verifies runtime access as `alicebot_app`.
- `DATABASE_BACKUP_URL` runs only `pg_dump` as `alicebot_backup`.

The lifecycle role owns the disposable database and grants `alicebot_admin`
database `CONNECT` and `CREATE`, plus `USAGE` and `CREATE` with grant option on
its `public` schema. It grants the app and backup roles database access and
explicit `USAGE` on `public`. It never runs migrations, dumps, restores, or
application queries. A peer-authenticated lifecycle URL is suitable only when
the drill process runs as the peer-mapped operating-system account.

PostgreSQL cannot scope `CREATEDB` to a database-name prefix. A compromised
`alicebot_drill` credential could create arbitrary databases and exhaust
cluster storage. Restrict its login source, protect and rotate the credential,
monitor database creation, and revoke the role when automated drills are not
required. A manual same-host lifecycle can instead use the local PostgreSQL
superuser through peer authentication, outside the automated script contract.

If a subprocess fails, the command writes bounded, credential-scrubbed
subprocess stderr to its own stderr with the stable failure code. Diagnostic
text is never copied into the sanitized JSON receipt.

See the [disaster-recovery runbook](../runbooks/disaster-recovery.md),
[health and monitoring](../runbooks/health-and-monitoring.md), and
[v0.12.0 upgrade procedure](../runbooks/upgrade-v0.12-to-current.md). The
workflow receipt is sanitized and excludes database URLs, secrets, paths, and
memory content. It identifies an uncommitted carrier with the source HEAD/tree,
an explicit clean/dirty state, and a deterministic snapshot digest rather than
claiming that HEAD contains the working changes.
