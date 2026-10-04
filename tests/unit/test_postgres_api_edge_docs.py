"""The docs say what v0.20.0 changed at the Postgres API edge and keep v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as published. What v0.20.0 changed (DB-005,
DB-006, DB-009 of the internal security review of v0.19.0) is marked
``From v0.20.0,`` where a document describes the latest
release, and the changelog entry sits under the v0.20.0 heading with the
v0.19.2 behaviour stated beside each change.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "From v0.20.0,"
ENTRY_START = "The Postgres stack's HTTP API edge and the provider clients are hardened, from the internal security review of v0.19.0."


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _v0200_changelog() -> str:
    changelog = _read("CHANGELOG.md")
    return changelog[changelog.index("## v0.20.0 \u2014 2026-10-02") + len("## v0.20.0 \u2014 2026-10-02") : changelog.index("## v0.19.2")]


def _entry() -> str:
    entries = [item for item in _v0200_changelog().split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_entry_sits_under_v0200_and_states_v0192_for_host_and_origin() -> None:
    """One v0.20.0 entry, with the v0.19.2 behaviour stated beside the Host and Origin rule.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete
    the sentence that says v0.19.2 checked the peer address only; delete the
    sentence that says keyed traffic is unchanged; delete the sentence that
    says the legacy ``/v0`` routes get the rule whatever ``Authorization`` header
    they carry; delete the ``ALICEBOT_ALLOWED_HOSTS`` instruction.
    """

    entry = _entry()
    assert "Host and Origin (DB-005):" in entry
    assert "is refused with the usual 401 `authentication_failed` unless its `Host` is `localhost`, `127.0.0.1` or `::1`" in entry
    assert "an exact name listed in the new `ALICEBOT_ALLOWED_HOSTS` setting (comma separated, no wildcard, port or scheme)" in entry
    assert "`X-Forwarded-Host` and `Forwarded` are not read" in entry
    assert "`null` is refused, and a `*` entry does not count" in entry
    assert (
        "a request with an agent key on `/v0/vnext` or `/v1` is not checked, so keyed traffic and the Caddy topology are unchanged"
    ) in entry
    assert "In v0.19.2 both gates checked the peer address only and the legacy routes checked nothing" in entry
    assert "`POST /v1/workspaces/bootstrap` and `POST /v1/evals/runs`" in entry
    assert "A browser attack through that was not reproduced." in entry
    assert "must now list that name in `ALICEBOT_ALLOWED_HOSTS` or use an agent key" in entry
    assert (
        "The legacy `/v0` routes, served in development and test or with `LEGACY_V0_ENABLED_OUTSIDE_DEV`, take no key, so every request to them gets this rule "
        "whatever `Authorization` header it carries, and a CORS preflight is not refused by it."
    ) in entry
    assert "not behind either gate and are not covered" not in entry

    released = _read("CHANGELOG.md")[_read("CHANGELOG.md").index("## v0.19.2") :]
    assert ENTRY_START not in released
    assert "ALICEBOT_ALLOWED_HOSTS" not in released


def test_the_changelog_entry_states_the_size_limit_and_the_nesting_limit_with_v0192_beside_each() -> None:
    """The DB-006 half of the entry: the numbers, the layer, the exceptions, and what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says the limit is the
    outermost layer; change 4 MiB, 32 MiB or 256 levels in the entry; delete the
    sentence that says v0.19.2 limited nothing, or the one that gives its
    measured memory; delete the Caddy sentence; delete the sentence that says
    the cap counts bytes as sent; delete the browser-clip exception.
    """

    entry = _entry()
    assert "Request size and nesting (DB-006):" in entry
    assert "a request body over 4 MiB (4,194,304 bytes, setting `ALICEBOT_MAX_REQUEST_BODY_BYTES`) is refused with HTTP 413" in entry
    assert '`{"code": "request_too_large", "message": "The request body is too large"}` before any layer reads it' in entry
    assert "A declared `Content-Length` over the cap is refused before a byte is read." in entry
    assert "the refusal carries `Connection: close`" in entry
    assert "text written with `\\uXXXX` escapes counts six bytes a character" in entry
    assert "they have their own cap of 32 MiB (setting `ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES`)" in entry
    assert "adds `request_body { max_size 4MB }`, the deployment validator requires it" in entry
    assert "A JSON body nested more than 256 levels deep is refused with HTTP 422" in entry
    assert "one error of type `json_too_deep` with `loc` `[\"body\"]`, with nothing from the body in it" in entry
    assert (
        "The check reads the bytes and never parses the body, so it runs before any layer parses it, and its cost grows "
        "in step with the size of the body, which the cap bounds."
    ) in entry
    assert "In v0.19.2 nothing was limited." in entry
    assert "the server's memory peaked at about 400 MiB for 100 MiB of bytes that are not JSON" in entry
    assert "about 1.5 GiB for a valid JSON body with a 100 MiB string, because the 422 echoes the input" in entry
    assert "712 MiB" not in entry
    assert "A body nested about 975 levels deep or more answered HTTP 500" in entry
    assert "A body nested 257 to about 974 levels deep reached the route" in entry
    assert "The HTTP 422 for a lone surrogate is unchanged." in entry


def test_the_changelog_entry_states_the_provider_redirect_change_with_v0192_beside_it() -> None:
    """The DB-009 half of the entry: no redirect, one door, the peer rule and where it does not apply, and v0.19.2.

    Mutations, each one alone: delete the sentence that tells the operator to
    configure the final URL; delete the sentence that says the embeddings,
    reranker, fact-key and brain clients pass False, or the one that gives the
    proxy exception; delete the v0.19.2 sentence about ``urlopen``; delete the
    sentence that says a provider response is still read whole; say the
    provider helpers enforce nothing.
    """

    entry = _entry()
    assert "Provider redirects (DB-009):" in entry
    assert "no longer follow an HTTP redirect" in entry
    assert "`model provider returned HTTP 302; redirects are not followed; set base_url to the final URL`" in entry
    assert "so an `Authorization` header or an API key is not sent on to it" in entry
    assert "must be configured with its final URL" in entry
    assert "through one function, `open_provider_url` in the new `provider_http` module" in entry
    assert "whose `enforce_public_peer` argument has no default" in entry
    assert "The provider helpers, response generation, Gmail and Calendar pass `True`" in entry
    assert "A name that was public when the base URL was checked and is loopback when the connection is made (DNS rebinding) is refused before a connection is opened" in entry
    assert "A request carried by an HTTP or HTTPS proxy skips that check, because the peer is then the proxy." in entry
    assert "The embeddings, reranker, fact-key and brain clients pass `False`" in entry
    assert "`http://localhost:11434/v1`" in entry
    assert "In v0.19.2 these clients called `urlopen`, which followed up to ten redirects" in entry
    assert "a POST answered 307 or 308 was already refused" in entry
    assert "and the embeddings, reranker, fact-key and brain clients did not check it at all" in entry
    assert "The response body of a provider call is still read whole." in entry


def test_the_provider_change_is_marked_from_v0200_in_the_threat_model_limitations_and_review_brief() -> None:
    """The threat model and the review brief say what v0.20.0 does, marked, and the limitations page states it as it is now.

    The known limitations page lists what is limited now, so it no longer retells that v0.19.2 used the
    standard library opener and followed redirects. That history is pinned on the dated records: the
    changelog entry in ``test_the_changelog_entry_states_the_provider_redirect_change_with_v0192_beside_it`` and the threat
    model here.

    Mutations, each one alone: delete the marker from the trust-boundary row, the
    abuse-case row, the open-items sentence or the review brief; delete the
    residual that names the proxy and the four clients that do not check the
    address, from the abuse-case row or from the limitations bullet; delete
    the clause that tells an operator to use the final URL from the limitations bullet.
    """

    raw = _read("docs/security/threat-model.md")
    rows = [line for line in raw.splitlines() if line.startswith("| Alice/provider |")]
    assert len(rows) == 1 and MARKER in rows[0]
    assert (
        "every provider, embeddings, reranker, fact-key, brain, Gmail and Calendar call goes through one door, "
        "`open_provider_url`"
    ) in rows[0]
    assert "every outbound call" not in rows[0]
    rows = [line for line in raw.splitlines() if line.startswith("| A provider answers with a redirect")]
    assert len(rows) == 1 and MARKER in rows[0]
    assert "A request carried by a proxy is not held to the address rule" in rows[0]
    assert "The embeddings, reranker, fact-key and brain clients can reach a loopback or private address" in rows[0]
    assert "`/v1`, which authenticates and does not authorize" in rows[0]
    model = _flat(raw)
    assert (
        f"the reranker and fact-key clients use the same opener and were not run. {MARKER} no provider client follows a "
        "redirect any more, and the provider helpers, Gmail and Calendar dial only an allowed address (DB-009)."
    ) in model

    bullets = [
        _flat(line)
        for line in _read("docs/alpha/known-limitations.md").splitlines()
        if line.startswith("- no call to a configured provider, embeddings, reranker or fact-key endpoint follows a redirect")
    ]
    assert len(bullets) == 1
    assert "(a provider that redirects must be configured with its final URL)" in bullets[0]
    assert (
        "the provider helpers, Gmail and Calendar dial only an address the outbound policy allows (DB-009)"
    ) in bullets[0]
    assert (
        "The embeddings, reranker, fact-key and brain clients can still reach a loopback or private address, a request "
        "carried by a proxy is not held to the address rule"
    ) in bullets[0]
    assert bullets[0].endswith("and a provider response is read whole")
    assert "standard library opener" not in bullets[0]

    brief = _flat(_read("docs/security/external-review-brief.md"))
    assert f"{MARKER} provider calls go through one function that follows no redirect" in brief
    assert "and a provider response is still read whole." in brief


def test_the_threat_model_names_the_size_limit_and_marks_v0200() -> None:
    """The size paragraph, the abuse-case row and the open-items bullet say it, marked.

    Mutations, each one alone: delete the marker from the paragraph, the row or
    the open-items sentence; delete the sentence that says the cap is no rate
    limit; delete the Caddy sentence.
    """

    raw = _read("docs/security/threat-model.md")
    model = _flat(raw)
    start = model.index(f"{MARKER} the HTTP API caps a request body before any layer reads it")
    paragraph = model[start:].split(" ### Assets")[0]
    assert "answers HTTP 413 for a body over `ALICEBOT_MAX_REQUEST_BODY_BYTES` (4 MiB by default)" in paragraph
    assert "`ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES` (32 MiB by default)" in paragraph
    assert "nested more than 256 levels deep is refused with HTTP 422" in paragraph
    assert "In v0.19.2 the identity layer, the two gates and the framework each read a body of any size" in paragraph
    assert "The cap is a bound on one request, not a rate limit." in paragraph
    assert "`packaging/cloud/Caddyfile.example` sets `request_body { max_size 4MB }`." in paragraph
    rows = [line for line in raw.splitlines() if line.startswith("| Oversized or deeply nested request body")]
    assert len(rows) == 1
    assert MARKER in rows[0]
    assert "the cap is no rate limit" in rows[0]
    assert (
        f"{MARKER} the `Host` and `Origin` rules above are in (DB-005), and a request body over 4 MiB is refused "
        "with HTTP 413 before it is read (DB-006)."
    ) in model


def test_the_threat_model_names_dns_rebinding_and_the_host_rule_and_marks_v0200() -> None:
    """The deployment paragraph, the abuse-case row and the open-items bullet all say what v0.20.0 does, marked.

    Mutations, each one alone: delete the marker from any of the three; delete
    the DNS rebinding sentence; delete the line that says the browser leg was
    not reproduced; delete the lines that say the identity layer applies the rule to
    the legacy ``/v0`` routes; say v0.19.2 checks the Host.
    """

    model = _flat(_read("docs/security/threat-model.md"))
    paragraph = model[model.index(f"{MARKER} keyless operation also checks the name a request uses") :].split(" ### Assets")[0]
    paragraph = paragraph.split("| Memories,")[0]
    assert "DNS rebinding points a name the attacker owns at 127.0.0.1" in paragraph
    assert "`ALICEBOT_ALLOWED_HOSTS`" in paragraph
    assert "`X-Forwarded-Host` and `Forwarded` are never read" in paragraph
    assert "`null` is refused" in paragraph
    assert "In v0.19.2 both gates looked at the peer address only" in paragraph
    assert "The browser leg of the attack has not been reproduced in a real browser" in paragraph
    assert "take no agent key, so the identity layer applies the same rule to every request to them" in paragraph
    assert "whatever `Authorization` header it carries" in paragraph
    assert "are not behind these two gates and are not covered" not in paragraph

    row = [line for line in _read("docs/security/threat-model.md").splitlines() if line.startswith("| DNS rebinding or a cross-origin request")]
    assert len(row) == 1
    assert MARKER in row[0]
    assert "Not reproduced in a real browser" in row[0]

    assert "does not check the `Host` header of a keyless loopback request. From v0.20.0, the `Host` and `Origin` rules above are in (DB-005)" in model


def test_known_limitations_states_the_edge_rules_and_what_they_leave_open() -> None:
    """The bullet states what the HTTP API refuses now and the limits that remain; v0.19.2 is on the dated records.

    The known limitations page lists what is limited now, so it no longer opens with what v0.19.2 did (parsed a body of any
    size before it authenticated, and did not check ``Host``). That is pinned on the dated records:
    the v0.19.2 notes (``test_the_v0192_release_notes_still_list_the_findings_as_open``), the changelog
    entry above and the threat model sentence "does not check the ``Host`` header of a keyless loopback
    request. From v0.20.0, ..." in ``test_the_threat_model_names_dns_rebinding_and_the_host_rule_and_marks_v0200``.

    Mutations, each one alone: change 4 MiB, 32 MiB or 256 in the bullet; delete the ``ALICEBOT_ALLOWED_HOSTS``
    clause or the ``Origin`` clause; delete the sentence that says an agent key is not checked or the clause that
    says the legacy ``/v0`` routes get the rule; delete the no rate limit clause; delete the sentence that says
    the rule was not checked from a real browser or that an allowed name is trusted as this machine.
    """

    bullets = [
        _flat(line)
        for line in _read("docs/alpha/known-limitations.md").splitlines()
        if line.startswith("- the HTTP API refuses a request body over 4 MiB")
    ]
    assert len(bullets) == 1
    bullet = bullets[0]
    assert (
        "- the HTTP API refuses a request body over 4 MiB (32 MiB for the connector sync routes) with HTTP 413 before "
        "any layer reads it, a JSON body nested more than 256 levels deep with HTTP 422 (DB-006), and a keyless request "
        "unless its `Host` is `localhost`, `127.0.0.1`, `::1` or a name listed in `ALICEBOT_ALLOWED_HOSTS`, and an "
        "`Origin` header, if one is sent, is a configured `CORS_ALLOWED_ORIGINS` entry or the request's own origin "
        "(DB-005)."
    ) in bullet
    assert (
        "A request with an agent key on `/v0/vnext` or `/v1` is not checked for `Host` and `Origin`, and the legacy "
        "`/v0` routes apply the rule to every request, because they check no key. A request inside the cap still "
        "costs memory and time, and the cap is no rate limit."
    ) in bullet
    assert bullet.endswith(
        "The Host and Origin rule was checked with raw requests and in process, not from a real browser, and a name "
        "listed in `ALICEBOT_ALLOWED_HOSTS` is trusted as this machine"
    )
    assert "in v0.19.2" not in bullet


def test_security_policy_deployment_guide_and_env_example_mark_the_edge_rules_from_v0200() -> None:
    """SECURITY.md and the deployment guide say it, marked, and the example env documents the settings.

    Mutations: delete the marker from either document; delete the commented
    ``ALICEBOT_ALLOWED_HOSTS`` line from the example env.
    """

    security = _flat(_read("SECURITY.md"))
    assert f"{MARKER} a keyless request must also name this machine in `Host`" in security
    guide = _flat(_read("docs/deployment/single-tenant-self-hosted.md"))
    assert f"{MARKER} while keyless, the API also refuses a request whose `Host` is not `localhost`" in guide
    assert "this topology, which requires a key before Caddy starts, is unchanged" in guide
    env = _read(".env.example")
    assert "# ALICEBOT_ALLOWED_HOSTS=" in env
    assert "# ALICEBOT_MAX_REQUEST_BODY_BYTES=4194304" in env
    assert "# ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES=33554432" in env
    assert f"{MARKER} Alice refuses a request body over 4 MiB with HTTP 413 before it reads it" in guide
    assert "That Caddy cap also applies to connector sync requests" in guide
    caddy = _read("packaging/cloud/Caddyfile.example")
    assert "request_body {\n\t\tmax_size 4MB\n\t}" in caddy


def test_the_legacy_v0_rule_and_the_new_error_family_are_in_the_docs() -> None:
    """SECURITY.md and the example env say the legacy routes get the rule, and the agent guide adds the 413 family.

    Mutations, each one alone: delete the legacy sentence from SECURITY.md or
    from ``.env.example``; delete the ``request_too_large`` family sentence, or
    the one that says the OpenAPI schema does not list the 413, from the agent
    integration guide.
    """

    security = _flat(_read("SECURITY.md"))
    assert "The legacy `/v0` routes, served in development and test, take no key, so they apply this rule to every request." in security
    env = _flat(_read(".env.example").replace("\n# ", "\n"))
    assert "The legacy /v0 routes take no key, so every request to them is checked." in env
    guide = _flat(_read("docs/alpha/agent-integration.md"))
    assert "HTTP 413 with `detail.code` `request_too_large`, a family added to the list above." in guide
    assert "A layer in front of the routes answers the 413, so the OpenAPI schema does not list it." in guide
    assert guide.count("The public families are `authentication_failed`") == 1
    # The list of released families is not edited: the new family is marked as v0.20.0's.
    assert "`internal_error`. Deliberate static route errors" in guide


def test_the_v0192_release_notes_still_list_the_findings_as_open() -> None:
    """Published release notes are immutable. Mutation: edit the DB-005 line in the v0.19.2 notes."""

    notes = _flat(_read("docs/release/v0.19.2-release-notes.md"))
    assert "**A keyless API does not check the Host header (DB-005).**" in notes
    assert "DB-005, DB-006, DB-008, DB-009, DB-010 and DB-011 are not fixed." in notes


def test_no_added_text_uses_an_em_dash_or_an_en_dash() -> None:
    """Mutation: write a dash into the changelog entry or any added paragraph."""

    for text in (
        _v0200_changelog(),
        _read("SECURITY.md"),
        _read("docs/security/threat-model.md"),
        _read("docs/deployment/single-tenant-self-hosted.md"),
        _read(".env.example"),
        _read("apps/api/src/alicebot_api/keyless_edge.py"),
        _read("apps/api/src/alicebot_api/request_limits.py"),
        _read("packaging/cloud/Caddyfile.example"),
        _read("docs/alpha/known-limitations.md"),
        _read("docs/security/external-review-brief.md"),
        _read("apps/api/src/alicebot_api/provider_http.py"),
    ):
        assert "\u2014" not in text
        assert "\u2013" not in text
