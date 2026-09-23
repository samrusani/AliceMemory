# Disaster Recovery

This runbook restores one self-hosted, single-tenant Alice installation. It
does not define an SLA or a managed backup service. Choose and document your
own recovery-point objective (backup frequency) and recovery-time objective
(time to provision, restore, verify, and cut over).

The destructive commands below always target a new or disposable database
first. Never rehearse against the only copy of production data.

## Before an incident

1. Record the Alice version and source commit deployed with each backup.
2. Encrypt backups off-machine and restrict access as you would for the live
   database; memory content is sensitive plaintext.
3. Retain the database roles, extensions, TLS settings, and environment
   configuration separately from the data dump. Portable SQLite JSONL omits
   users, agent keys, embeddings, and deleted rows.
4. Schedule backups and run a restore drill regularly. A successful backup
   command without a successful restore drill is not recovery evidence.

The repository drill exercises both backends, the v0.12.0 upgrade path, and
monitoring contracts. It creates and destroys only private SQLite fixtures and
randomly named `alice_phase5_ops_*` PostgreSQL databases:

```bash
./.venv/bin/python scripts/run_phase5_ops_evidence.py --backend all \
  --work-dir "$(mktemp -d)" \
  --output artifacts/phase5/ops-evidence.json
```

PostgreSQL mode requires `DATABASE_LIFECYCLE_URL`, `DATABASE_ADMIN_URL`,
`DATABASE_URL`, and `DATABASE_BACKUP_URL`, PostgreSQL 16 `pg_dump` and
`pg_restore`, a PostgreSQL 16 server, and four distinct roles:

- `alicebot_drill` is used by the drill only to create, grant, and drop random
  disposable databases. It needs `CREATEDB`, but it remains `NOSUPERUSER`,
  `NOCREATEROLE`, and `NOBYPASSRLS`;
- `alicebot_admin` runs migrations, verification, and restore with
  `NOSUPERUSER`, `NOCREATEDB`, and `NOBYPASSRLS`;
- `alicebot_app` performs authenticated application verification;
- `alicebot_backup` runs only the dump. It is a non-superuser read role with
  `BYPASSRLS`, which is required to read forced-RLS tables completely.

The lifecycle identity owns the disposable database. It grants
`alicebot_admin` database `CONNECT` and `CREATE`, plus `USAGE` and `CREATE`
with grant option on the disposable `public` schema. It grants the app and
backup roles database access and explicit `USAGE` on `public`, but it does not
run migrations, dumps, restores, or application queries. Compatible `pgcrypto`
and `vector` extensions must be preinstalled in `template1` by the database
operator so each disposable target inherits them. A same-host
peer-authenticated lifecycle URL is valid only when the drill process runs
under the peer-mapped operating-system account.

PostgreSQL cannot restrict `CREATEDB` to the drill database-name prefix, so a
compromised lifecycle credential could create arbitrary databases and exhaust
cluster storage. Restrict its login source, protect and rotate it, monitor
database creation, and revoke it when automated drills are not required. A
manual same-host lifecycle can instead use the local PostgreSQL superuser
through peer authentication, outside the automated script contract.

The drill validates all three major versions before creating a disposable
database and fails closed on missing, malformed, or mismatched version
evidence. Do not use a newer client major: its archive prologue may contain
settings PostgreSQL 16 cannot restore. Exit `0` and report status `passed` are
required. The JSON report is sanitized; the temporary databases, dumps, and
memory text are removed on exit. A failed subprocess writes bounded,
credential-scrubbed stderr to the process stderr under its stable failure code.
That diagnostic never enters the JSON receipt. Failure to drop the randomly
named PostgreSQL database is a failed drill with `postgres_cleanup_failed`,
never a passed receipt. An operator must remove the named-by-prefix fixture
before retrying. CI runs the same command in
`.github/workflows/ops-evidence.yml` and retains only the sanitized report.

Receipt identity does not pretend that Git `HEAD` contains an uncommitted
carrier. It records `source_head_commit`, that commit's `source_head_tree`, a
`carrier_state` of `clean` or `dirty`, and `carrier_snapshot_sha256` over the
actual carrier. The snapshot includes tracked changes and deletions plus all
non-ignored untracked files. It hashes file contents, types, modes, and symlink
targets without following a symlink outside the repository. Git-ignored drill
outputs such as `artifacts/` are excluded through the repository's standard
ignore rules.

## SQLite physical recovery

Use a physical copy when you need every local byte, including signed embedding
vectors and the vector-cache invalidation stamp.

1. Stop every Alice MCP/API process that can write the database.
2. Checkpoint the WAL and require the first returned value to be `0` (not
   busy):

   ```bash
   sqlite3 ~/.alice/memory.db 'PRAGMA wal_checkpoint(TRUNCATE);'
   sqlite3 ~/.alice/memory.db 'PRAGMA integrity_check;'
   ```

3. Copy the main database only after the successful checkpoint. Preserve
   owner-only permissions and hash the copy:

   ```bash
   install -m 0600 ~/.alice/memory.db ~/alice-backups/memory.db
   shasum -a 256 ~/alice-backups/memory.db
   ```

4. Restore into a new owner-only directory, never over the only live copy:

   ```bash
   install -d -m 0700 ~/alice-restore-test
   install -m 0600 ~/alice-backups/memory.db ~/alice-restore-test/memory.db
   sqlite3 ~/alice-restore-test/memory.db 'PRAGMA integrity_check;'
   alice-memory mcp --db ~/alice-restore-test/memory.db
   ```

5. Run a known recall query and inspect a memory that has an embedding. Only
   then stop the live process, preserve the failed database family (`.db`,
   `-wal`, `-shm`, `-journal`), and atomically replace it with the verified
   restore.

If the checkpoint reports busy, do not copy just the main file. Find the
remaining writer or use the `alice-memory export` online-snapshot path in
[Backup and restore](../alpha/backup-and-restore.md).

## SQLite portable recovery

Portable JSONL is for user-owned memory-graph portability, not a physical
clone:

```bash
alice-memory export --db ~/.alice/memory.db \
  --out ~/alice-backups/alice.jsonl
alice-memory import --db ~/alice-restore-test/memory.db \
  --in ~/alice-backups/alice.jsonl
alice-memory reindex-embeddings --db ~/alice-restore-test/memory.db
```

If the backup holds a credential and the source vault is gone,
`--quarantine <memory_id>[,<memory_id>...]` is the owner's recovery path.
It is not a way to import that credential. The SHA-256 footer is still
checked on the file as given. Each named memory is stored as `rejected`
with its text replaced by `[quarantined on import]`, so recall, resume,
and a context pack do not return it. The receipt lists the ids and counts,
not the removed text. A second import of the same file with the same ids
skips those identical redacted rows. See
[Backup and restore](../alpha/backup-and-restore.md).

Require the export/import/re-export canonical SHA-256 footer and record counts
to match. A quarantined memory will not match its original export, because
its text was replaced. FTS recall works immediately after import. Vector recall does not:
portable JSONL omits embedding vectors, so configure the intended embedding
provider and reindex before cutover.

## PostgreSQL recovery

Run PostgreSQL 16 client tools against the PostgreSQL 16 server with libpq
environment variables so credentials do not appear in process arguments.
Create the dedicated backup login as a database superuser or another identity
allowed to manage role attributes:

```sql
CREATE ROLE alicebot_backup
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT CONNECT ON DATABASE alicebot TO alicebot_backup;
\connect alicebot
GRANT USAGE ON SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON TABLES TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO alicebot_backup;
```

`BYPASSRLS` can read every Alice row. Restrict this login to the backup path,
protect and rotate its credential, and do not use it for migrations or restore.
On a co-located database, a manual `sudo -u postgres pg_dump` over the local
Unix socket avoids a stored backup credential. That peer-authenticated
alternative does not apply to a remote database.

```bash
export PGHOST=db.internal PGPORT=5432 PGUSER=alicebot_backup PGDATABASE=alicebot
export PGSSLMODE=verify-full
export PGSSLROOTCERT=/etc/alicebot/postgres-ca.pem
export PGPASSWORD='from-your-secret-manager'
pg_dump --format=custom --no-comments --file=alice.dump
pg_restore --list alice.dump
sha256sum alice.dump
```

Provision a new database with PostgreSQL 16 and pgvector 0.8 or newer. A
database operator must create the compatible `pgcrypto` and `vector` extensions
before restore, or the disposable target must inherit them from `template1`.
Ensure the `alicebot_app` role exists. Create the target through the lifecycle
identity, then restore without changing ownership as `alicebot_admin`:

```bash
PGUSER=alicebot_drill PGDATABASE=postgres \
  PGPASSWORD='drill-password-from-your-secret-manager' \
  createdb alice_restore_test
PGUSER=alicebot_drill PGDATABASE=postgres \
  PGPASSWORD='drill-password-from-your-secret-manager' \
  psql --set=ON_ERROR_STOP=1 <<'SQL'
GRANT CONNECT, CREATE ON DATABASE alice_restore_test TO alicebot_admin;
GRANT CONNECT, TEMPORARY ON DATABASE alice_restore_test TO alicebot_app;
GRANT CONNECT ON DATABASE alice_restore_test TO alicebot_backup;
SQL
PGUSER=alicebot_drill PGDATABASE=alice_restore_test \
  PGPASSWORD='drill-password-from-your-secret-manager' \
  psql --set=ON_ERROR_STOP=1 <<'SQL'
GRANT USAGE, CREATE ON SCHEMA public
  TO alicebot_admin WITH GRANT OPTION;
GRANT USAGE ON SCHEMA public TO alicebot_app, alicebot_backup;
SQL
umask 077
PGUSER=alicebot_admin PGDATABASE=alice_restore_test \
  PGPASSWORD='admin-password-from-your-secret-manager' \
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
PGUSER=alicebot_admin PGDATABASE=alice_restore_test \
  PGPASSWORD='admin-password-from-your-secret-manager' \
  pg_restore --exit-on-error --no-owner --no-comments \
  --use-list=alice.restore.list \
  --dbname=alice_restore_test alice.dump
```

`--no-comments` on the dump prevents new archives from carrying extension
comments. The restore flag also ignores comments in an older archive, avoiding
an extension-ownership failure without making the restoring role a superuser.
The private restore list removes exactly `ACL - SCHEMA public`. The lifecycle
step reconstructs public-schema privileges for admin, app, and backup on the
fresh target. Table, sequence, and non-public schema ACL entries remain in the
restore list. Do not use `--no-acl`, which would also remove the application
privileges defined by migrations.

Against the restored database, verify:

- `SELECT version_num FROM alembic_version` equals the release's dynamic
  Alembic head;
- `SELECT count(*)` matches for users, memories, events, artifacts, and
  artifact ratings;
- a known FTS recall query returns its memory;
- the memory still has `embedding_vector` and a content-matching embedding
  signature;
- application-role access works with RLS enabled and forced.

If the restore is from an older release, follow
[Upgrade v0.12.0 to current](upgrade-v0.12-to-current.md) in the restored
database before verification and cutover.

## Cutover and rollback

1. Quiesce writers and take one final backup.
2. Repeat the integrity, row-count, recall, embedding-signature, migration-head,
   and application-role checks on the candidate.
3. Point Alice at the verified target and check `/healthz`, `/v0/vnext/doctor`,
   and scheduler status.
4. Keep the pre-cutover store read-only until the retention window expires.
5. On any failed proof, point Alice back to that untouched store. Do not merge
   or replay writes until the cause is understood.

Record the backup hash, source-head commit/tree, carrier state/snapshot hash,
database engine version, migration head, check results, start/end time, and
operator. Never record credentials or memory content in the recovery receipt.
