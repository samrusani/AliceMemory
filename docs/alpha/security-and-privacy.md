# Public Alpha Security And Privacy

Alice public preview is local-first.

## Deployment Trust Boundary

**Keyless is local-machine-owner mode, not anonymous network mode.** Keep the
API and web app on loopback and use an SSH tunnel for headless access. In a
keyless deployment, local callers can select the Alice `user_id`; that value is
not an authentication credential. Do not expose the API port to a LAN, public
interface, container network with untrusted peers, or an untrusted browser.

Once any active agent API key exists for a user, protected `/v0/vnext` requests
for that user reject keyless access. Remote access requires active keys plus a
TLS-terminating authenticated reverse proxy, a restrictive CORS allowlist, and
host/firewall controls. Agent keys authenticate Alice calls; they do not encrypt
traffic or harden the host.

Security posture:

- source evidence is review-only
- generated artifacts are review-only
- agent memory proposals are review-only by default; explicit agent commits may become durable only when the configured policy permits it
- trusted memory is not auto-promoted by default. A deployment opts in with `ALICE_MEMORY_PERSONA` set to `personal` or `team`, or with one of those two personas in the owner's Brain Charter. See [memory promotion personas](../memory/promotion-personas.md)
- with the `personal` or `team` persona set, auto-promotion only lifts a write that was waiting for review or confirmation, and only from a writer the server established: an agent whose identity an issued agent key resolved, or the owner through the HTTP memory commit route once a key has been issued. It never lifts a write whose identity the caller only declared, a rejected write, a write from a `memory_proposal_agent`, or a write that hits a hard-floor rule (credential material, instructions aimed at the agent, an agent's own output stored as fact) or an enabled escalation filter. Source evidence, generated artifacts, scheduler output and connector captures are never auto-promoted
- connector secrets should be stored as secret refs
- CLI/API/UI/event/source/artifact output should not print secret values
- prompt-injection source text is data, not policy
- agents are policy-checked by authenticated identity, permission profile, persisted target, project, domain, sensitivity, and action
- a project-bound key inherits its project filter when a read omits one and cannot read or mutate a different project
- all `/v0/vnext` HTTP routes share one authentication boundary: keyless local compatibility ends when the user creates an active key, after which strict Bearer authentication is required
- every route is classified as either target/policy-authorized or central operator-only; once keys exist, central console reads and writes require an unbound `trusted_local_agent` or `admin_agent` key, and unclassified routes fail closed
- artifact get, feedback, quality-rating, review, export, and trace operations authorize the persisted artifact project/domain/sensitivity before returning content or applying a side effect; trace sources are filtered by the same exact-target policy
- the local `/vnext` console accepts an unbound `trusted_local_agent` or `admin_agent` key only through its password field, keeps it only in browser memory for the mounted session, and forwards it only to loopback `/v0/vnext` routes; it never reads the key from environment variables, local storage, URLs, logs, or errors
- `trusted_local_agent` does not grant human/admin review decisions such as artifact acceptance; use a dedicated unbound `admin_agent` key when those actions are needed and revoke it when no longer needed
- the browser-clipper bookmarklet never embeds or prompts for an agent key or a reusable `capture_token`; a trusted Alice UI issues a short-lived, origin-bound, one-time capture capability, while trusted non-browser API clients may use Bearer authentication plus `capture_token`
- MCP servers bound with `ALICE_AGENT_API_KEY` expose only the core surface; legacy handlers fail closed because they do not all implement the same persisted-target authorization contract
- the managed SQLite directory is owner-only; database sidecars and exports are also owner-only

The complete shipped-product threat model, evidence ledger, and open proof gaps
are in [`docs/security/`](../security/README.md). These materials record
automated security scanning and internal adversarial review, findings triaged
and fixed. They are not a third-party review or a security certification.

Recommended alpha defaults:

```bash
alicebot vnext doctor --fix-safe --ci
alicebot vnext smoke secret-redaction
alicebot eval run --suite all
```

Sensitive domain guidance:

- every permission profile except `trusted_local_agent` and `admin_agent` is held back from five domains: family, health, spiritual, legal and financial. The engine does not hold back `personal` or `professional`, and `regulated` is a sensitivity level that the profile's sensitivity ceiling holds. A project-scoped agent should still avoid personal material that is not about its project
- Unreleased (on main, not in v0.20.0): a request that names no domain is held to the same set, so a restricted caller that sends no `domains` reads every domain except those five, `unknown` included. In v0.20.0 the five were removed only from domains the caller listed, and a request that listed none read all of them. See [MCP tools](mcp-tools.md#domains-a-profile-may-read)
- trusted local assistants should request sensitive domains only when necessary
- blocked or filtered policy decisions should be surfaced in `/vnext` Agent Activity

Reporting issues:

- include command output with secrets redacted
- include failing smoke names
- do not include private exports, real Telegram payloads, API tokens, or personal datasets
