# Single-Tenant Self-Hosted Deployment

This is the supported Phase 5 deployment shape for one Alice owner on one
Linux host. It is a hardened operating contract, not cloud-provider IaC and
not evidence that Alice was exercised on a public cloud. The checked-in CI
smoke validates the configuration contract only. A release cannot claim an
executed cloud deployment until the owner supplies the sanitized
`owner_real_host_deployment_receipt` described below.

## Claim boundary

This guide covers:

- one VM or container host controlled by one owner;
- one Alice API process and one prebuilt Alice web process, both bound only to
  loopback;
- Caddy as the only public process, terminating public TLS, requiring a valid
  operator client certificate, and proxying to the two loopback services;
- PostgreSQL 16 with pgvector 0.8 or newer over certificate-verified TLS;
- separate `alicebot_admin` migration, `alicebot_app` runtime,
  `alicebot_backup` read-only dump, and `alicebot_drill` lifecycle roles;
- deployment-time environment/secret injection, scheduled encrypted backups,
  restore drills, and conservative upgrades.

It does **not** provide or claim multi-tenant isolation, an SLA, high availability,
zero-downtime deploys, a managed database, managed backup,
managed alert delivery, automatic failover, or cloud-provider support. The
operator owns the host, PostgreSQL, DNS, certificate reachability, monitoring,
backup retention, restore testing, and incident response.

## Security posture

**Keyless equals local-machine-owner trust.** Before the first active agent key
exists, local processes that can reach the loopback API share the machine
owner's trust. That compatibility mode is not internet authentication. Never
publish Alice while it is keyless, and never expose ports 3000 or 8000 through
a public address, port-forward, load balancer, or container publish rule.

From v0.20.0, while keyless, the API also refuses a
request whose `Host` is not `localhost`, `127.0.0.1` or `::1` (or a name listed
in `ALICEBOT_ALLOWED_HOSTS`, exact names separated by commas) and a request whose
`Origin` is neither a `CORS_ALLOWED_ORIGINS` entry nor its own origin. This stops
a page on another name that resolves to 127.0.0.1 from calling the API. A request
with an agent key is not checked, and this topology, which requires a key before
Caddy starts, is unchanged.

From v0.20.0, Alice refuses a request body over 4 MiB with
HTTP 413 before it reads it (`ALICEBOT_MAX_REQUEST_BODY_BYTES`, in bytes), and
the connector sync routes, which take lists of whole documents, have a separate
cap of 32 MiB (`ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES`). The Caddy example caps
the body at the proxy too, with `request_body { max_size 4MB }` in the site block,
so an oversized upload is refused before it reaches Alice. That Caddy cap also
applies to connector sync requests; raise it for those paths if you send large
documents through Caddy.

Once any active agent key exists, keyless `/v0/vnext` calls are rejected. The
browser console requires an unbound `admin_agent` key for the complete review
surface and holds it only in browser memory for that mounted session. Create
that key while the firewall is still closed, before opening the firewall to
Caddy. Keep the raw value in the owner's secret manager and revoke it when its
operator session is no longer needed.

The legacy compatibility routers remain unmounted with
`ALICE_LEGACY_SURFACES=0`, and the production legacy gate remains closed with
`LEGACY_V0_ENABLED_OUTSIDE_DEV=false`. The production middleware distinguishes
the agent-key-protected `/v0/vnext` family from legacy `/v0` routes: vNext may
pass through the authenticated TLS proxy, while legacy routes stay disabled.
This route distinction and the agent-key auth sweep are release-gated runtime
contracts, not something the proxy should emulate.

Do not add another proxy without revisiting Caddy's trust configuration and
Alice's exact `TRUSTED_PROXY_IPS` allowlist. Caddy must preserve the real client
IP. The proxy authentication control is mutual TLS (mTLS), not HTTP Basic,
because Alice needs the HTTP `Authorization` header for its own Bearer agent
key.

## Topology and firewall

```text
browser/agent
     |
     | mTLS HTTPS :443 (HTTP :80 only for ACME and redirect)
     v
Caddy on the one host
     |-- 127.0.0.1:3000  Alice web
     `-- 127.0.0.1:8000  Alice API
                              |
                              `-- TLS verify-full --> PostgreSQL 16 + pgvector
```

Permit inbound TCP 80 and 443 to Caddy. Deny inbound 3000, 8000, 5432, and the
Caddy admin port. If PostgreSQL is remote, its network policy should allow
5432 only from this host's private egress identity. The application host must
be able to resolve the database hostname exactly as it appears in the server
certificate.

## 1. Prepare a pinned release

Use a fresh non-root service account and a verified Alice release tag or exact
commit. Record the tag and SHA in the deployment receipt. Do not deploy from a
dirty checkout. Install Python 3.12+, Node.js, the repository-declared pnpm
version, PostgreSQL 16 client tools, and a pinned/approved Caddy package. The
reference assets use host processes and no container image. If an operator
translates them to containers, every image must use an immutable
`@sha256:<digest>` reference; a floating tag is not equivalent evidence.

The reference unit names both the service account and its primary group
`alicebot`. Provision that named non-root account and group through the host's
configuration-management layer before installing files, then verify them:

```bash
id alicebot
getent group alicebot
git clone https://github.com/samrusani/AliceMemory.git /opt/alicebot
cd /opt/alicebot
git fetch --tags --force
ALICE_RELEASE_REF='replace-with-verified-tag-or-sha'
git checkout --detach "$ALICE_RELEASE_REF"
git status --short
git rev-parse HEAD

python3 -m venv .venv
./.venv/bin/python -m pip install --require-virtualenv .
corepack enable
corepack prepare pnpm@10.23.0 --activate
pnpm --dir apps/web install --frozen-lockfile
```

The example installs the package from the checkout and keeps the checkout in
place, because the migration script, the seed helper and the evidence scripts
run from it. Pin and inventory the host packages in the operator's own image or
configuration-management layer.

## 2. Inject configuration and secrets

Start from
[`packaging/cloud/single-tenant.env.example`](../../packaging/cloud/single-tenant.env.example).
It is a template, not a usable environment file. A deployment system or secret
manager must replace `${ALICEBOT_DB_APP_PASSWORD}` while rendering an
API runtime file at `/etc/alicebot/runtime.env`. Do not ask systemd
`EnvironmentFile=` to expand it; it does not perform shell interpolation.
Keep host configuration on persistent storage rather than `/run`, which is
normally cleared at reboot. A reference host prepares it as follows:

```bash
sudo install -d -o root -g root -m 0755 /etc/alicebot
sudo install -o root -g alicebot -m 0640 "$RENDERED_RUNTIME_ENV" \
  /etc/alicebot/runtime.env
sudo install -o root -g alicebot -m 0640 "$RENDERED_RECOVERY_ENV" \
  /etc/alicebot/backup-restore.env
sudo install -o root -g root -m 0644 "$POSTGRES_CA_BUNDLE" \
  /etc/alicebot/postgres-ca.pem
sudo install -o root -g root -m 0644 "$CLIENT_CA_CERTIFICATE" \
  /etc/alicebot/client-ca.pem
```

The two environment files contain credentials and are readable only by root
and the Alice service group. Persisting them makes host storage encryption,
backups, access control, and rotation operator responsibilities. The CA
certificates are public trust anchors, not private keys, but root ownership
protects their integrity. Never store a CA private key or a client private key
in `/etc/alicebot`.

Migration and recovery automation must render the separate privileged DSNs
below directly into the appropriate one-shot process environments. These are
shape contracts, not usable credentials:

```dotenv
DATABASE_ADMIN_URL="postgresql://alicebot_admin:${ALICEBOT_DB_ADMIN_PASSWORD}@db.alice.internal:5432/alicebot?sslmode=verify-full&sslrootcert=/etc/alicebot/postgres-ca.pem"
DATABASE_BACKUP_URL="postgresql://alicebot_backup:${ALICEBOT_DB_BACKUP_PASSWORD}@db.alice.internal:5432/alicebot?sslmode=verify-full&sslrootcert=/etc/alicebot/postgres-ca.pem"
DATABASE_LIFECYCLE_URL="postgresql://alicebot_drill:${ALICEBOT_DB_DRILL_PASSWORD}@db.alice.internal:5432/postgres?sslmode=verify-full&sslrootcert=/etc/alicebot/postgres-ca.pem"
```

The runtime, admin, backup, and lifecycle examples deliberately name the same
normalized host, port, TLS mode, and CA while using distinct roles and
passwords. Runtime, admin, and backup name the `alicebot` database; lifecycle
names the `postgres` maintenance database.
DATABASE_ADMIN_URL is required for migrations, but it is not a production API
startup requirement. DATABASE_ADMIN_URL is absent from the API runtime
environment and must not be copied into its supervisor unit.
DATABASE_BACKUP_URL and DATABASE_LIFECYCLE_URL are restricted to external
backup and recovery jobs and must not be copied into the API runtime
environment.

Before substitution, **percent-encode each database password as URL userinfo**
(RFC 3986), or generate it exclusively from the URL-safe unreserved alphabet
`A-Z a-z 0-9 - . _ ~`. Never insert raw `@`, `:`, `/`, `?`, `#`, or `%`
characters into either DSN; they can change how the URL is parsed. Encode only
the password value, not the complete DSN, and keep the raw and encoded values
out of command arguments and receipts.

Required invariants:

- `APP_ENV=production`;
- `APP_HOST=127.0.0.1` and `ALICE_WEB_HOST=127.0.0.1`;
- one exact origin such as `https://alice.example.com` in `PUBLIC_ORIGIN`,
  `CORS_ALLOWED_ORIGINS`, and `NEXT_PUBLIC_ALICEBOT_API_BASE_URL`;
- no wildcard CORS and `CORS_ALLOW_CREDENTIALS=false`;
- `TRUST_PROXY_HEADERS=true` with the exact trusted peer
  `TRUSTED_PROXY_IPS=127.0.0.1`;
- unique runtime, migration, backup, and lifecycle database passwords injected
  into their respective process environments at deploy time, never committed;
- `sslmode=verify-full` and the same absolute `sslrootcert` path in the
  runtime, migration, backup, and lifecycle URLs;
- the same stable user UUID in `ALICEBOT_AUTH_USER_ID` and
  `NEXT_PUBLIC_ALICEBOT_USER_ID`.

Migration commands consume their one-shot `DATABASE_ADMIN_URL`; backup and
drill lifecycle commands consume their own one-shot URLs; application queries
consume the runtime `DATABASE_URL`. The API must always run through
`alicebot_app`, and its service environment must never contain any privileged
DSN. Restrict access to the separately rendered environments.

Connector and model credentials are separate secret-manager injections. For
the surviving Telegram connector, inject `TELEGRAM_BOT_TOKEN` into the service
environment, then persist only its reference:

```bash
./.venv/bin/alicebot vnext connectors configure telegram \
  --enabled --secret-ref env:TELEGRAM_BOT_TOKEN
```

Do not commit a raw connector token or put provider API keys in
`WORKSPACE_PROVIDER_CONFIGS_JSON`. The `env:TELEGRAM_BOT_TOKEN` value is an
identifier; the environment value it resolves is the secret.

`NEXT_PUBLIC_*` values are build-time public configuration. Before every
build, atomically re-render a mode `0600`
`apps/web/.env.production.local` containing only the public API origin, user
UUID, and the same server-only `PUBLIC_ORIGIN`. Never put database credentials,
provider keys, connector tokens, or other secrets in this file. It is
intentionally Git-ignored deployment-local input and may remain in place for
reproducible rebuilds. The web supervisor must also inject `PUBLIC_ORIGIN` at
runtime. Rebuild the web app whenever any of those values changes:

```bash
git check-ignore -v apps/web/.env.production.local
pnpm --dir apps/web build
```

The ignored file and the ignored `.next` output are excluded from the Git
carrier/source identity. That identity proves the source carrier, not the
rendered deployment configuration or built web artifact. The deployment
receipt and external probes prove the configured public deployment separately.

In this hardened topology, **/vnext is the only live authenticated browser console**.
Its client-side workspace accepts the browser-memory operator key,
and the web API helper permits that Bearer key only for loopback or the exact
configured same-origin HTTPS base. It rejects another host such as
`https://evil.example`, plaintext remote origins, credentials, and base paths.

The other default-navigation pages perform async server-side reads over legacy
`/v0`; the server cannot use the browser-memory Bearer key or the browser's mTLS
client certificate. They therefore remain demo/fixture views in this topology,
not live operational surfaces. Making every page live requires a future BFF or
client-side refactor and is out of scope for this guide.

## 3. Prepare and verify PostgreSQL

Provision PostgreSQL 16 and install pgvector 0.8 or newer. Create four distinct
login roles:

- `alicebot_admin` is `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`; it
  owns the production database and runs migrations and restores.
- `alicebot_app` is `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`; it
  receives only migration-defined runtime grants.
- `alicebot_backup` is `NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS`; it
  receives only the read grants required for a complete dump.
- `alicebot_drill` is `NOSUPERUSER CREATEDB NOCREATEROLE NOBYPASSRLS`; the
  drill script uses it to create, grant access to, and drop randomly named
  disposable drill databases.

A DBA or local PostgreSQL superuser must create the backup role because only a
superuser can grant `BYPASSRLS`. After migrations create the production
objects, apply and retain these read grants:

```sql
GRANT CONNECT ON DATABASE alicebot TO alicebot_backup;
GRANT USAGE ON SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO alicebot_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON TABLES TO alicebot_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE alicebot_admin IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO alicebot_backup;
```

The backup role has no ownership or write privileges. `BYPASSRLS` is still
powerful because it can read every tenant row in the granted objects. Protect
and rotate its credential and restrict its network access. For a database on
this same host, the retained backup job may instead run `pg_dump` as the local
PostgreSQL superuser through Unix-socket peer authentication, which avoids a
stored database credential. That alternative does not apply to a remote
database.

`CREATEDB` is cluster-wide. PostgreSQL does not enforce the script's
`alice_phase5_ops_*` naming restriction, so a stolen or misused
`alicebot_drill` credential could create arbitrary databases and consume disk.
Protect and rotate it, restrict its network access, and alert on unexpected
database creation.

The drill script connects `alicebot_drill` to the maintenance database. It
creates each disposable database as the lifecycle role, grants
`alicebot_admin` `CONNECT,CREATE` at the database level, grants
`alicebot_app` `CONNECT,TEMPORARY`, grants `alicebot_backup` `CONNECT`, and
grants `alicebot_admin` `USAGE,CREATE` on schema `public` with grant option.
Extensions must already be inherited from `template1`. Admin then migrates and
restores while lifecycle remains responsible only for database creation,
access grants, and cleanup. The automated script requires the exact
`alicebot_drill` role and rejects a superuser lifecycle identity. A same-host
peer-superuser is only a manual lifecycle alternative outside the automated
script contract, and it is valid only when that manual process runs under the
peer-mapped operating-system account. A passwordless peer URL does not work
from `User=alicebot` and must not be passed to the script.

For a manual disposable target, the lifecycle connection applies the same
grants before admin restore:

```sql
CREATE DATABASE alice_restore_test;
GRANT CONNECT, CREATE ON DATABASE alice_restore_test TO alicebot_admin;
GRANT CONNECT, TEMPORARY ON DATABASE alice_restore_test TO alicebot_app;
GRANT CONNECT ON DATABASE alice_restore_test TO alicebot_backup;
```

After reconnecting to `alice_restore_test` as `alicebot_drill`:

```sql
GRANT USAGE, CREATE ON SCHEMA public
  TO alicebot_admin WITH GRANT OPTION;
GRANT USAGE ON SCHEMA public TO alicebot_app, alicebot_backup;
```

The disposable database must inherit `pgcrypto` and `vector` from `template1`.
On a same-host PostgreSQL installation, the local superuser performs this
one-time cluster operation:

```bash
sudo -u postgres psql --dbname template1 --set ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
SQL
```

This changes the baseline for every future database created from `template1`.
For remote PostgreSQL, the database provider's DBA or superuser must perform
the equivalent operation through its privileged administration path. If the
provider forbids `template1` customization and offers no equivalent
pre-provisioning hook, the reference disposable-restore drill is unsupported
on that service and must not be reported as passed.

With `DATABASE_ADMIN_URL` injected into this one-shot verification process,
verify the server and certificate without putting a credentialed URL in
process arguments:

```bash
./.venv/bin/python - <<'PY'
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_ADMIN_URL"]) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        server_version = int(cursor.fetchone()[0])
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        vector_version = cursor.fetchone()
if server_version // 10000 != 16:
    raise SystemExit("PostgreSQL major must be 16")
if vector_version is None:
    raise SystemExit("pgvector is not installed")
try:
    vector_major_minor = tuple(int(part) for part in str(vector_version[0]).split(".")[:2])
except ValueError as exc:
    raise SystemExit("pgvector version could not be parsed") from exc
if vector_major_minor < (0, 8):
    raise SystemExit("pgvector must be 0.8 or newer")
print("postgres_major=16 pgvector_minimum=0.8 tls_contract=verify-full")
PY
```

This proves the configured CA and hostname accepted the server certificate;
`sslmode=require` alone is not sufficient. Record only versions and status,
not DSNs, certificate paths, or command output containing infrastructure
identifiers.

## 4. Migrate as admin, then run as the application role

Take a backup before every migration. Have the deployment runner inject
`DATABASE_ADMIN_URL` only into the migration command, then run Alembic through
the admin role. `scripts/migrate.sh` fails before Alembic if that variable is
absent; it never falls back to the runtime DSN.

```bash
./scripts/migrate.sh
./.venv/bin/alicebot vnext migrations status
./.venv/bin/python scripts/seed_local_user.py
```

Run the seed command after migration and before API workspace bootstrap. It
requires the one-shot `DATABASE_ADMIN_URL` and the configured
`ALICEBOT_AUTH_USER_ID`. The helper sets transaction-local
`app.current_user_id` before its idempotent insert so the forced row-level
security policy permits exactly the configured local user. It does not change
runtime API behavior. If the seed step is skipped, workspace bootstrap returns
`404 not_found` until the local user row exists.

Then discard the one-shot migration environment and start the API with the
runtime template, whose `DATABASE_URL` names `alicebot_app` and which contains
no `DATABASE_ADMIN_URL`, `DATABASE_BACKUP_URL`, or
`DATABASE_LIFECYCLE_URL`. Start the web server with explicit loopback flags. A
supervisor should load `/etc/alicebot/runtime.env` at process start, restart on
failure with backoff, set a sensible file-descriptor limit, and capture
stdout/stderr without environment dumps.

```bash
env -u DATABASE_ADMIN_URL -u DATABASE_BACKUP_URL -u DATABASE_LIFECYCLE_URL ./.venv/bin/python -m alicebot_api.local_server
pnpm --dir apps/web start --hostname 127.0.0.1 --port 3000
```

After the supervisor starts the API, prove the boundary against the **live
process**, not merely the source environment file. The following Linux/systemd
probe reads the process environment privately, never prints it, connects with
that process's runtime URL, and emits only a safe verdict. Adapt the unit name
or use the equivalent container-namespace inspection for another supervisor;
never dump `/proc/.../environ` into logs or a receipt.

```bash
api_pid="$(systemctl show --property MainPID --value alice-api.service)"
test "$api_pid" -gt 0
sudo /opt/alicebot/.venv/bin/python - "$api_pid" <<'PY'
import sys

import psycopg

try:
    raw_environment = open(f"/proc/{int(sys.argv[1])}/environ", "rb").read()
    process_environment = dict(
        entry.split(b"=", 1) for entry in raw_environment.split(b"\0") if b"=" in entry
    )
    privileged_names = {
        b"DATABASE_ADMIN_URL",
        b"DATABASE_BACKUP_URL",
        b"DATABASE_LIFECYCLE_URL",
    }
    if privileged_names.intersection(process_environment):
        raise RuntimeError
    runtime_url = process_environment.get(b"DATABASE_URL")
    if runtime_url is None:
        raise RuntimeError
    with psycopg.connect(runtime_url.decode("utf-8")) as connection:
        session_role, effective_role = connection.execute(
            "SELECT session_user, current_user"
        ).fetchone()
    if session_role != "alicebot_app" or effective_role != "alicebot_app":
        raise RuntimeError
except Exception:
    print("api_runtime_db_boundary=failed")
    raise SystemExit(1)
print("api_runtime_db_boundary=passed role=alicebot_app privileged_dsns_present=false")
PY
```

The owner receipt records only pass/fail and timestamp for this probe. It must
attest `runtime DB role=alicebot_app` for both the PostgreSQL session and
effective roles, plus `admin DSN absent from API service environment`,
`backup DSN absent from API service environment`, and
`lifecycle DSN absent from API service environment`; do not record any DSN,
the process environment, PID, host, or database error details.

Before Caddy or the public firewall is enabled, verify both listeners from the
host:

```bash
curl --fail-with-body http://127.0.0.1:8000/healthz
curl --fail-with-body --head http://127.0.0.1:3000/vnext
```

`/healthz` checks PostgreSQL only. Its Redis and object-storage fields are
`not_checked`; a 200 does not prove the web UI, scheduler, connectors, model
provider, embedding provider, backups, or alert delivery. Treat those as
separate probes.

## 5. Bootstrap identity and authentication before publication

Keep the public firewall closed. Bootstrap the one local workspace through
the loopback API, then create a dedicated unbound operator key by omitting
`--project-scope`:

```bash
curl --fail-with-body --request POST http://127.0.0.1:8000/v1/workspaces/bootstrap
./.venv/bin/alicebot agent keys create \
  --agent-id vnext-operator \
  --profile admin_agent \
  --label "Single-tenant review console"
```

The raw `alice_sk_...` value is shown once. Capture it directly into the
owner's secret manager, confirm `alicebot agent keys list` shows an active key
with `project_scope` null, and do not place the raw value in shell history,
service files, URLs, logs, or a deployment receipt.

Prove that keyed mode closed the compatibility path before opening the
firewall. A keyless request to the operator workspace must return 401; a
request with the unbound key must succeed. Feed the Authorization header to
curl through stdin configuration so the key is not a process argument:

```bash
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "http://127.0.0.1:8000/v0/vnext/workspace?user_id=${ALICEBOT_AUTH_USER_ID}")" = 401

printf 'header = "Authorization: Bearer %s"\n' "$ALICE_AGENT_API_KEY" | \
  curl --fail-with-body --config - \
  "http://127.0.0.1:8000/v0/vnext/workspace?user_id=${ALICEBOT_AUTH_USER_ID}"
```

Run `unset ALICE_AGENT_API_KEY` immediately afterward if the deployment shell
does not need it again.

## 6. Enable the TLS proxy

Copy
[`packaging/cloud/Caddyfile.example`](../../packaging/cloud/Caddyfile.example),
replace the example domain and ACME contact, validate it with the exact Caddy
binary selected by the operator, and install it as an owner-controlled config.
The file routes API paths without stripping them and sends all other requests
to the web process. Caddy's admin listener and both upstreams are loopback. It
requires and verifies a client certificate against the operator-controlled CA
at `/etc/alicebot/client-ca.pem`, enables strict SNI/Host matching, and
preserves Caddy's normal real-client `X-Forwarded-For` behavior. Protect the CA
and client private keys outside the repository. Issue a separate short-lived
client certificate for each authorized browser or agent; rotation or revocation
is an operator procedure and must be tested before relying on it.

The public API matcher is intentionally exact: `/healthz`, the API docs, and
`/v0/vnext` plus its descendants. It does not publish broad `/v0/*` or any
`/v1` route. **Remote /v1 is unsupported** until those routes have application
authentication equivalent to the vNext agent-key boundary. Workspace
bootstrap, provider administration, and runtime `/v1` calls therefore remain
loopback-only operator actions performed before the public firewall opens.

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Start Caddy only after the unbound `admin_agent` key exists. The server
certificate must come from a publicly trusted CA; the client-certificate CA is
operator-owned and independent. Do not use `tls internal` for the public
server endpoint.

Point public DNS at the host, permit inbound 80/443, and confirm Caddy obtained
a publicly trusted server certificate only after the runtime auth gates and
the local keyless/authenticated probes pass on the exact release candidate.

Run these probes from a second machine, not from the deployment host:

```bash
# Both negative probes must fail the TLS handshake. The second certificate
# must be issued by a deliberately untrusted test CA, never the operator CA.
if curl --silent --show-error --fail --output /dev/null \
  https://alice.example.com/healthz; then
  echo "no-client-certificate rejection failed" >&2
  exit 1
fi
if curl --cert "$ALICE_UNTRUSTED_MTLS_CLIENT_CERT" \
  --key "$ALICE_UNTRUSTED_MTLS_CLIENT_KEY" \
  --silent --show-error --fail --output /dev/null \
  https://alice.example.com/healthz; then
  echo "untrusted-client-certificate rejection failed" >&2
  exit 1
fi
curl --cert "$ALICE_MTLS_CLIENT_CERT" --key "$ALICE_MTLS_CLIENT_KEY" \
  --fail-with-body https://alice.example.com/healthz
curl --cert "$ALICE_MTLS_CLIENT_CERT" --key "$ALICE_MTLS_CLIENT_KEY" \
  --fail-with-body --head https://alice.example.com/vnext
curl --cert "$ALICE_MTLS_CLIENT_CERT" --key "$ALICE_MTLS_CLIENT_KEY" \
  --fail-with-body https://alice.example.com/openapi.json

# These paths must resolve to the web 404 boundary (HTML), never Alice's API.
probe_not_api() {
  result="$(curl --cert "$ALICE_MTLS_CLIENT_CERT" \
    --key "$ALICE_MTLS_CLIENT_KEY" --silent --show-error \
    --output /dev/null --write-out '%{http_code} %{content_type}' "$1")"
  case "$result" in
    "404 text/html"*) ;;
    *) echo "public API route boundary failed" >&2; return 1 ;;
  esac
}
probe_not_api https://alice.example.com/v1/workspaces/bootstrap
probe_not_api https://alice.example.com/v0/memories
probe_not_api https://alice.example.com/v0/vnextish
```

The OpenAPI document's `info.version` is the HTTP version probe; compare it to
the checked-out release and also record `./.venv/bin/alicebot --version` on the
host. Confirm the valid-certificate responses carry HSTS and clickjacking
response headers (`Strict-Transport-Security`, CSP `frame-ancestors 'none'`,
and `X-Frame-Options: DENY`). Repeat the keyless 401 and authenticated
workspace probes through the HTTPS origin. Do not save the authenticated
response or raw header in CI artifacts.

Record the three route checks only as `remote-v1-not-api=passed`,
`remote-non-vnext-v0-not-api=passed`, and
`remote-vnext-lookalike-not-api=passed`, each with a timestamp. Do not retain
response bodies, content, hostnames, or adapted Caddy configuration in the
receipt.

## 7. Monitoring and backups

Monitor at least:

- Caddy TLS renewal, 5xx rate, request latency, and disk use;
- API/web process availability and restart count;
- `/healthz` database reachability, with its DB-only limitation preserved;
- PostgreSQL connections, storage, replication posture if independently
  configured, and certificate expiry;
- Alice scheduler status, heartbeat freshness, failures, and expired claims;
- connector health and last successful capture;
- backup completion, age, encrypted off-host copy, and last restore drill.

Alert delivery is operator-owned and must be tested end to end. This project
does not provide managed alert routing.

Wire PostgreSQL backup to an **external scheduler** such as a systemd timer or
cloud scheduler; do not rely on the Alice workflow scheduler to protect its own
database. Follow [Backup and Restore](../alpha/backup-and-restore.md) and the
[Disaster Recovery runbook](../runbooks/disaster-recovery.md). The scheduled
job must use secret-manager injection, create a custom-format `pg_dump`, verify
the archive, encrypt it before upload, keep an off-host encrypted copy, apply a
documented retention policy, and emit a secret-free success/failure signal.

Schedule the 5.2 PostgreSQL backup/restore drill with a one-shot service like
the following. Render `/etc/alicebot/backup-restore.env` separately with the
role-separated `DATABASE_ADMIN_URL`, `DATABASE_URL`, `DATABASE_BACKUP_URL`, and
`DATABASE_LIFECYCLE_URL` shapes shown above; all four URLs must retain
`sslrootcert=/etc/alicebot/postgres-ca.pem`. Keep the file root-owned,
group-readable by `alicebot`, and mode `0640`. The CA bundle must exist at that
exact persistent path on a VM, or be mounted there read-only in a container.
The dump runs with `alicebot_backup` and `--no-comments`; restore and migration
remain `alicebot_admin` operations, application verification remains an
`alicebot_app` operation, and disposable database create/drop uses
`alicebot_drill`. The service passes DSNs only through its environment, never
through command-line arguments:

```ini
[Unit]
Description=Alice PostgreSQL backup and disposable-restore drill

[Service]
Type=oneshot
User=alicebot
EnvironmentFile=/etc/alicebot/backup-restore.env
BindReadOnlyPaths=/etc/alicebot/postgres-ca.pem
ExecStart=/opt/alicebot/.venv/bin/python /opt/alicebot/scripts/run_phase5_ops_evidence.py --backend postgres --work-dir /var/lib/alicebot/backup-drill --output /var/lib/alicebot/evidence/postgres-backup-restore.json
```

```ini
[Timer]
OnCalendar=weekly
Persistent=true
```

The drill writes the `pg_restore --list` output to a private mode `0600` TOC
file and removes exactly the `ACL - SCHEMA public` entry. The archive's public
schema ACL belongs to the source database owner and cannot be replayed by the
least-privilege admin against a target schema owned by `alicebot_drill`. The
entire public-schema ACL is deliberately reconstructed target-side by the
explicit lifecycle grants above, including `USAGE` for `alicebot_app` and
`alicebot_backup`. All table, sequence, and non-public-schema object ACL
entries must remain in the restore list.

For a manual restore, reproduce the exact filter and fail unless the archive
contains exactly one matching entry:

```bash
umask 077
pg_restore --list alice.dump > alice.restore.full.list
public_schema_acl_count="$(
  awk '$4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public" { count += 1 } END { print count + 0 }' \
    alice.restore.full.list
)"
test "$public_schema_acl_count" = 1
awk '!($4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public")' \
  alice.restore.full.list > alice.restore.list
chmod 0600 alice.restore.full.list alice.restore.list
PGUSER=alicebot_admin PGDATABASE=alice_restore_test \
  PGPASSWORD='admin-password-from-your-secret-manager' \
  pg_restore --exit-on-error --no-owner --no-comments \
  --use-list=alice.restore.list --dbname=alice_restore_test alice.dump
```

Do not use `--no-acl`. That broad option would also discard table, sequence,
and non-public-schema object grants that the restored service needs.

This drill proves dump/restore mechanics and deletes its disposable database;
it is not the retained production backup. The external backup job must still
encrypt and upload its verified archive off-host under the operator's retention
policy. Never add database URL options to the service because doing so exposes
credentials in process arguments.

At a cadence chosen from the operator's recovery objectives, restore a selected
backup into a **disposable restore** database, apply the same release's
migrations, verify row counts and representative authenticated recall, then
destroy the disposable target. A backup upload without a successful restore
drill is not recovery evidence. Keep the restore target isolated from public
traffic and never overwrite the live database to perform a drill.

## 8. Safe upgrade and rollback

For every upgrade:

1. record the current release tag/SHA and migration head;
2. stop writers or enter a documented maintenance window;
3. create, encrypt, and upload a backup;
4. complete a disposable restore and application probe;
5. stage the new verified release and rebuild the web app with the exact public
   origin;
6. inject `DATABASE_ADMIN_URL` only into the one-shot migration process and
   run migrations;
7. discard the migration environment, start the API using only the
   `alicebot_app` runtime URL, and run loopback probes;
8. start Caddy/public traffic and run external TLS, version, `/vnext` after
   operator-key entry, keyless-401, authenticated API, scheduler, and connector
   probes. Do not report fixture-backed navigation pages as live.

Application code can be rolled back only when the deployed schema is declared
compatible with that older release. There is **no in-place schema downgrade**
contract. If a database rollback is required, stop writers and restore the
pre-upgrade backup into a fresh database, verify it, and cut over explicitly.
Do not run Alembic downgrade against the live database and do not point an old
binary at a newer schema on hope alone.

## CI contract smoke and the required real-host receipt

Run the repository validator locally with:

```bash
./.venv/bin/python scripts/run_single_tenant_deployment_smoke.py
./.venv/bin/python -m pytest tests/unit/test_single_tenant_deployment.py -q
```

The CI workflow deliberately emits:

```text
environment=ephemeral_ci
cloud_provider=none
public_dns=false
public_ca=false
evidence_kind=configuration_contract_only
source_head_commit=<Git object id>
source_head_tree=<Git tree object id>
carrier_state=clean|dirty
carrier_snapshot_sha256=<SHA-256>
validated_asset_sha256=<six logical-name SHA-256 values>
```

It checks examples, exact origins, loopback binds/upstreams, database role and
TLS settings, exact proxy trust, fail-closed mTLS, real-client XFF preservation,
immutable workflow pins, claim boundaries, and receipt sanitization. It does
not start Caddy, obtain a certificate, contact a cloud provider, or prove a
real backup schedule. The owner real-host receipt remains blocking.

The source commit/tree identify HEAD. `carrier_state` and the carrier digest
bind the actual tracked plus nonignored-untracked working bytes, including
tracked deletions, file modes, and symlink target text without following the
link. The six per-asset hashes separately bind this guide, the environment
example, the Caddyfile, the workflow, the local-user seed helper, and the web
API source whose exact-origin trust contract the smoke validates. Paths and
file contents never enter the JSON receipt.

Before anyone claims the guide was exercised, the owner must add an out-of-band
sanitized receipt named `owner_real_host_deployment_receipt` with:

- release tag and source SHA;
- `environment=real_single_tenant_host` and the actual cloud/provider class;
- `public_dns=true` and `public_ca=true` after an external TLS probe;
- pass/fail and timestamps for migration, loopback API/web, external health,
  version, `/vnext` after operator-key entry, keyless rejection, authenticated
  workspace, scheduler, backup, and disposable restore checks;
- explicit pass/fail for no-client-certificate rejection,
  untrusted-client-certificate rejection, valid-client-certificate acceptance,
  and HSTS and clickjacking response headers;
- if certificate revocation or rotation is relied on, explicit pass/fail for
  revoked-client-certificate rejection and replacement-certificate acceptance;
- confirmation that only Caddy is publicly reachable and the API/web/database
  ports are not;
- pass/fail and timestamp proving the live API service has runtime DB
  role=`alicebot_app` and its process environment has no `DATABASE_ADMIN_URL`,
  `DATABASE_BACKUP_URL`, or `DATABASE_LIFECYCLE_URL`;
- `remote-v1-not-api`, `remote-non-vnext-v0-not-api`, and
  `remote-vnext-lookalike-not-api` pass/fail with timestamps;
- reviewer identity and date.

The receipt must exclude usernames beyond fixed Alice role names, hostnames,
IP addresses, DSNs, credentials, agent keys or prefixes, certificate paths,
filesystem paths, memory content, HTTP bodies, and raw logs. Until that receipt
is reviewed, the truthful status is “configuration contract validated; real
single-tenant host proof outstanding.”
