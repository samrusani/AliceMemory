# Security Policy

## Supported Versions

Alice is pre-1.0 software. Security fixes go into the latest published release.
Older releases and development snapshots do not get security backports; move to
the latest published release before reporting or validating a fix. The default
branch is useful for reproducing a forthcoming fix, but it is not a supported
release until it is tagged and published.

## Reporting a Vulnerability

Report security issues privately by email to hello@alicememory.com. If private
vulnerability reporting is enabled for this repository, the Security tab shows a
"Report a vulnerability" button, and you can open a private security advisory
there instead. Include:

- affected component/file
- reproduction steps
- impact assessment
- suggested mitigation (if available)

Do not open public issues for active security vulnerabilities.

## Security Boundaries

- Alice is local-first, single-user, self-hosted software. It is not a
  multi-tenant service boundary.
- **Keyless means local-machine-owner trust.** A keyless vNext deployment is
  safe only when the API remains on loopback and every local process and OS user
  that can reach it is trusted as the Alice owner. In this mode a caller-supplied
  `user_id` is routing context, not proof of identity.
- Once a user has any active agent API key, protected `/v0/vnext` requests for
  that user reject keyless access and require `Authorization: Bearer
  alice_sk_...`. A browser-clipper one-time capability is a deliberately narrow
  exception for its single capture route; it is not a general API credential.
- Do not expose a keyless API or MCP process to a LAN, container peer, public
  interface, or untrusted reverse-proxy client. Remote access requires active
  agent keys, a TLS-terminating authenticated reverse proxy, a restrictive CORS
  allowlist, and host/firewall controls.
- PostgreSQL runtime access uses the application role and per-user RLS. Keep the
  admin database credential confined to migrations and administrative recovery.
  SQLite is a single-user on-ramp protected by owner-only filesystem
  permissions, not by database RLS.
- Public CLI, MCP, connector, and importer surfaces must preserve provenance and
  policy boundaries. Imported and provider-returned text is data, never policy.
- Consequential side effects remain approval-bounded.

The shipped threat model and current evidence boundaries are documented in
[`docs/security/`](docs/security/README.md).

## Browser Clipper Credentials

Visited-page JavaScript is hostile credential context. The bookmarklet must
never contain, request, or persist an agent API key or reusable connector
`capture_token`.

The trusted Alice UI issues a short-lived, origin-bound, one-time capability for
the selected page origin. The visited page can observe that narrow capability,
so it may make the one authorized submission; it cannot replay it, redeem it
from another origin, or turn it into general Alice access. Trusted API clients
may still use Bearer authentication plus a reusable `capture_token`, but must
never pass that reusable token to a bookmarklet or other visited-page script.

## Hardening Notes

- Keep `.env` files local and do not commit secrets.
- Keep API and web services bound to loopback unless the authenticated TLS
  deployment boundary above is in place.
- Treat per-agent API keys (`alicebot agent keys create`) as secrets. Alice
  stores a SHA-256 verifier and short identification prefix, displays the raw
  key once, and supports revocation with `alicebot agent keys revoke`.
- Treat logs, exports, backups, and imported source archives as sensitive user
  data even when application secrets have been redacted.
- Run the release verification and security evidence commands before tagging.
