"""A keyless HTTP request must name this machine in Host and must not come from another origin (DB-005).

The internal security review of v0.19.0 found that a keyless API answered the
same whatever the ``Host`` header said, so a name the attacker owns that
resolves to 127.0.0.1 (DNS rebinding) reached it, and that a cross-origin
request with no body still did its work. In v0.19.2 the keyless gates look at
the peer address only.

The tests drive the real app in process over raw ASGI, so every middleware runs
in order, with a fake store in place of Postgres. Each test names the mutation
that must fail it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api import keyless_edge
from alicebot_api.config import Settings
from alicebot_api.keyless_edge import (
    host_refusal,
    keyless_edge_refusal,
    origin_refusal,
    parse_allowed_host,
    parse_host,
)
from tests.unit.api_edge_harness import (
    BOOTSTRAP_PATH,
    CHARTER_PATH,
    EVAL_RUNS_PATH,
    KEY,
    SENTINEL,
    USER,
    V1_LIST_PATH,
    Edge,
)

ROOT = Path(__file__).resolve().parents[2]

HOSTILE_HOSTS = [
    "attacker.invalid",
    "rebind.evil.example:8000",
    "192.168.1.50:8000",
    "127.0.0.1.evil.example",
    "localhost.evil.example:8000",
    "evil.example#127.0.0.1",
    "user@127.0.0.1",
    "127.0.0.1:80x",
    "127.0.0.1:",
    "127.0.0.1:99999",
    "",
    "not a host at all",
    "localhost:8000, evil.example",
    "[::1",
    "[::1]x",
    "::1",
    "localhost\n",
    "127.0.0.2:8000",
    "0.0.0.0:8000",
    # Well-formed names that only end in a loopback name (a suffix match lets them in).
    "notlocalhost",
    "notlocalhost:8000",
    "evil127.0.0.1",
    "x.localhost",
    "x.localhost:8000",
    None,
]
LOOPBACK_HOSTS = [
    "127.0.0.1:8000",
    "127.0.0.1",
    "localhost:8000",
    "LOCALHOST:8000",
    "localhost",
    "localhost.",
    "localhost.:8000",
    "[::1]:8000",
    "[::1]",
    "[0:0:0:0:0:0:0:1]:8000",
]


@pytest.fixture()
def edge(monkeypatch: pytest.MonkeyPatch) -> Edge:
    return Edge(monkeypatch)


# The pure rules.


@pytest.mark.parametrize("host", [h for h in HOSTILE_HOSTS if h is not None])
def test_parse_host_refuses_what_is_not_one_authority(host: str) -> None:
    """Prefix, suffix, userinfo, fragment, list, bad port, bare IPv6 and whitespace do not parse to a loopback name.

    Mutation: replace the fullmatch in ``parse_host`` with a prefix match, or
    drop the port bound. A value above parses to a loopback name.
    """

    parsed = parse_host(host)
    assert parsed is None or parsed[0] not in keyless_edge.LOOPBACK_HOSTS


def test_host_refusal_needs_exactly_one_host_header_and_a_loopback_or_listed_name() -> None:
    """A missing or repeated Host is refused, and a name is allowed only by exact match.

    Mutations: treat a missing Host as allowed; read only the first of two
    Host headers; match the allowed list by suffix; match a loopback name by
    suffix (``name.endswith`` for each name in ``LOOPBACK_HOSTS``), which lets
    ``notlocalhost`` and ``x.localhost`` through.
    """

    assert host_refusal([], ()) == "host_header_missing_or_repeated"
    assert host_refusal(["localhost", "localhost"], ()) == "host_header_missing_or_repeated"
    assert host_refusal(["localhost:8000"], ()) is None
    assert host_refusal(["alice.lan:8000"], ()) == "host_not_allowed"
    assert host_refusal(["alice.lan:8000"], ("alice.lan",)) is None
    assert host_refusal(["evil.alice.lan"], ("alice.lan",)) == "host_not_allowed"
    assert host_refusal(["alice.lan.evil.example"], ("alice.lan",)) == "host_not_allowed"
    assert host_refusal(["Alice.LAN."], ("alice.lan",)) is None
    for suffix_only in ("notlocalhost", "x.localhost", "evil127.0.0.1"):
        assert host_refusal([suffix_only], ()) == "host_not_allowed", suffix_only


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("alice.lan", "alice.lan"),
        ("Alice.LAN", "alice.lan"),
        ("192.168.1.50", "192.168.1.50"),
        ("fd00::5", "fd00::5"),
        ("[fd00::5]", "fd00::5"),
        ("*.alice.lan", None),
        ("*", None),
        ("alice.lan:8000", None),
        ("http://alice.lan", None),
        ("alice.lan/path", None),
        ("", None),
        ("  ", None),
    ],
)
def test_allowed_host_entries_are_exact_names(entry: str, expected: str | None) -> None:
    """No wildcard, port, scheme or path. Mutation: let ``*`` or a port through ``parse_allowed_host``."""

    assert parse_allowed_host(entry) == expected


def test_origin_refusal_table() -> None:
    """Configured origins and the request's own origin pass. Everything else, including ``null``, is refused.

    Mutations: treat ``null`` as no Origin; treat a ``*`` entry as matching;
    ignore the configured list; compare the own origin by prefix.
    """

    own = {"host_value": "127.0.0.1:8000", "request_scheme": "http"}
    configured = ("http://localhost:3000", "http://127.0.0.1:3000")
    assert origin_refusal([], allowed_origins=configured, **own) is None
    assert origin_refusal(["http://localhost:3000"], allowed_origins=configured, **own) is None
    assert origin_refusal(["http://127.0.0.1:8000"], allowed_origins=(), **own) is None
    assert origin_refusal(["https://127.0.0.1:8000"], allowed_origins=(), **own) is None
    assert origin_refusal(["https://evil.example"], allowed_origins=configured, **own) == "origin_not_allowed"
    assert origin_refusal(["null"], allowed_origins=configured, **own) == "origin_not_allowed"
    assert origin_refusal(["null"], allowed_origins=("null",), **own) == "origin_not_allowed"
    assert origin_refusal(["http://localhost:5173"], allowed_origins=configured, **own) == "origin_not_allowed"
    assert origin_refusal(["http://127.0.0.1:8001"], allowed_origins=(), **own) == "origin_not_allowed"
    assert origin_refusal(["http://127.0.0.1:8000.evil.example"], allowed_origins=(), **own) == "origin_not_allowed"
    assert origin_refusal(["http://evil.example"], allowed_origins=("*",), **own) == "origin_not_allowed"
    assert origin_refusal(["*"], allowed_origins=("*",), **own) == "origin_not_allowed"
    assert origin_refusal(["http://localhost:3000", "http://localhost:3000"], allowed_origins=configured, **own) == (
        "origin_header_repeated"
    )


def test_default_ports_compare_equal_to_no_port() -> None:
    """A browser leaves the default port out of Origin, so ``localhost`` and ``http://localhost:80`` agree.

    Mutation: compare the raw port text.
    """

    assert origin_refusal(["http://localhost"], host_value="localhost:80", request_scheme="http", allowed_origins=()) is None
    assert origin_refusal(["http://localhost:80"], host_value="localhost", request_scheme="http", allowed_origins=()) is None
    assert origin_refusal(["https://localhost"], host_value="localhost:443", request_scheme="https", allowed_origins=()) is None
    assert origin_refusal(["http://localhost:81"], host_value="localhost", request_scheme="http", allowed_origins=()) == (
        "origin_not_allowed"
    )


def test_keyless_edge_refusal_checks_host_before_origin() -> None:
    """A hostile Host is the reason given even when the Origin is configured. Mutation: skip the Host check."""

    reason = keyless_edge_refusal(
        host_values=["attacker.invalid"],
        origin_values=["http://localhost:3000"],
        request_scheme="http",
        allowed_hosts=(),
        allowed_origins=("http://localhost:3000",),
    )
    assert reason == "host_not_allowed"


# The vNext gate.


def test_a_keyless_request_with_a_hostile_host_is_refused_before_the_handler(edge: Edge) -> None:
    """Every Host value in the list above that are not this machine get the gate's 401 and no charter text.

    Mutations, each one alone: delete the Host check in
    ``_vnext_protected_http_auth``; match by prefix in ``parse_host``; match a
    loopback name by suffix in ``host_refusal`` (``notlocalhost``, ``x.localhost``
    and ``evil127.0.0.1`` are in the list); treat a missing Host as allowed.
    """

    edge.configure()
    for host in HOSTILE_HOSTS:
        outcome = edge.charter(host=host)
        assert outcome.refused, (host, outcome.status)
        assert SENTINEL.encode() not in outcome.body, host


def test_keyless_loopback_hosts_still_reach_the_handler(edge: Edge) -> None:
    """The names a local client uses keep working, with any port, case and a trailing dot.

    Mutations: drop ``::1`` from ``LOOPBACK_HOSTS``; stop removing the port;
    stop lower-casing the name.
    """

    edge.configure()
    for host in LOOPBACK_HOSTS:
        outcome = edge.charter(host=host)
        assert outcome.status == 200, (host, outcome.status)
        assert SENTINEL.encode() in outcome.body, host


def test_a_bound_user_does_not_change_the_host_rule(edge: Edge) -> None:
    """With ``ALICEBOT_AUTH_USER_ID`` set the hostile Host is still refused. Mutation: skip the check when a user is bound."""

    edge.configure(bound=True)
    assert edge.charter(host="rebind.evil.example:8000").refused
    assert edge.charter(host="127.0.0.1:8000").status == 200


def test_a_same_origin_write_through_a_hostile_host_is_refused_and_writes_nothing(edge: Edge) -> None:
    """The page the attacker serves is same-origin with its own name, so only Host can refuse it.

    Mutation: delete the Host check in ``_vnext_protected_http_auth``. The PUT
    answers 200 and the store holds the text.
    """

    store = edge.configure()
    body = json.dumps({"user_id": USER, "content_markdown": "REWRITTEN"}).encode()
    outcome = edge.call(
        "PUT",
        CHARTER_PATH,
        host="rebind.evil.example:8000",
        origin="http://rebind.evil.example:8000",
        body=body,
        content_type="application/json",
    )
    assert outcome.refused
    assert store.writes == []


def test_forwarded_host_headers_are_never_read(edge: Edge) -> None:
    """A client that sends ``X-Forwarded-Host: 127.0.0.1`` with a hostile Host is still refused.

    Mutation: honour ``X-Forwarded-Host`` or ``Forwarded`` when present.
    """

    edge.configure()
    outcome = edge.charter(
        host="attacker.invalid",
        extra_headers=[("x-forwarded-host", "127.0.0.1:8000"), ("forwarded", "host=127.0.0.1")],
    )
    assert outcome.refused


def test_a_request_with_a_key_ignores_host_and_origin(edge: Edge) -> None:
    """Keyed traffic, such as the Caddy topology, is unchanged: any Host and any Origin pass with a valid key.

    Mutations, each one alone: apply the Host or Origin check to a request that
    carries a Bearer key in the vNext gate, or in the ``/v1`` gate.
    """

    edge.configure(keys=1)
    for host, origin in (("alice.example.com", None), ("alice.example.com", "https://alice.example.com"), ("127.0.0.1:8000", "https://evil.example")):
        outcome = edge.charter(host=host, origin=origin, authorization=f"Bearer {KEY}")
        assert outcome.status == 200, (host, origin, outcome.status)

    edge.configure(keys=1, bound=True)
    for host, origin in (("alice.example.com", None), ("127.0.0.1:8000", "https://evil.example")):
        outcome = edge.call("GET", V1_LIST_PATH, host=host, origin=origin, authorization=f"Bearer {KEY}")
        assert outcome.resolver_calls >= 1, (host, origin)
        assert not outcome.refused, (host, origin)
        assert outcome.reached_handler, (host, origin)


def test_the_operator_host_list_admits_a_named_host_and_nothing_near_it(edge: Edge) -> None:
    """``ALICEBOT_ALLOWED_HOSTS`` names exact hosts. Mutation: ignore the setting, or match it by suffix."""

    edge.configure(allowed_hosts=("alice.lan",))
    assert edge.charter(host="alice.lan:8000").status == 200
    assert edge.charter(host="ALICE.LAN").status == 200
    assert edge.charter(host="evil.alice.lan:8000").refused
    assert edge.charter(host="alice.lan.evil.example").refused
    assert edge.charter(host="other.lan").refused


def test_the_capability_capture_route_keeps_its_exemption(edge: Edge) -> None:
    """The browser-clip capture route is called from a hostile page by design and is gated by its capability.

    A capability-bearing capture with a hostile Host and Origin is not refused at the edge. It
    reaches the route and the route refuses the unknown capability. A capture with no capability
    is refused like any keyless request. Mutation: apply the edge check to capability captures.
    """

    edge.configure()
    capture = "/v0/vnext/connectors/browser-clipper/capture"
    with_capability = json.dumps({"user_id": USER, "url": "https://example.org/a", "capture_capability": "cap-1"}).encode()
    outcome = edge.call(
        "POST",
        capture,
        host="rebind.evil.example:8000",
        origin="https://evil.example",
        body=with_capability,
        content_type="text/plain;charset=UTF-8",
    )
    assert not outcome.refused
    assert outcome.reached_handler
    without = json.dumps({"user_id": USER, "url": "https://example.org/a"}).encode()
    outcome = edge.call(
        "POST",
        capture,
        host="rebind.evil.example:8000",
        origin="https://evil.example",
        body=without,
        content_type="application/json",
    )
    assert outcome.refused


# The /v1 gate.


@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
def test_the_v1_gate_refuses_a_hostile_host_before_anything_runs(edge: Edge, bound: bool) -> None:
    """The ``/v1`` gate has its own check. Mutation: delete the Host check in ``enforce_v1_agent_authentication``."""

    edge.configure(bound=bound)
    for host in HOSTILE_HOSTS:
        outcome = edge.call("GET", V1_LIST_PATH, host=host, extra_headers=[("x-alicebot-user-id", USER)])
        assert outcome.refused, (host, outcome.status)
        assert outcome.resolver_calls == 0, host


def test_the_v1_gate_lets_loopback_hosts_through_to_the_key_check(edge: Edge) -> None:
    """A loopback Host reaches the key resolver. Mutation: refuse every Host on ``/v1``."""

    edge.configure(bound=True)
    for host in LOOPBACK_HOSTS:
        outcome = edge.call("GET", V1_LIST_PATH, host=host)
        assert outcome.resolver_calls == 1, (host, outcome.status)
        assert not outcome.refused, host


# The legacy /v0 routes.

LEGACY_GET_PATHS = (
    "/v0/memories",
    "/v0/entities",
    "/v0/continuity/captures",
    "/v0/continuity/recall",
)
LEGACY_POST_PATH = "/v0/continuity/captures"


@pytest.mark.parametrize("keys", (0, 1), ids=("no-keys", "one-key"))
@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
def test_a_legacy_v0_request_with_a_hostile_host_is_refused_before_the_handler(
    edge: Edge, bound: bool, keys: int
) -> None:
    """The legacy ``/v0`` routes have no key to present, so a rebound page needs the Host rule there too.

    In v0.19.2 and in the first draft of this change a hostile Host reached the
    handler of ``/v0/memories``, ``/v0/entities`` and ``/v0/continuity/*``.

    Mutations, each one alone: delete the Host and Origin check for non-vnext
    paths in ``enforce_authenticated_user_identity``; apply it to
    ``/v0/vnext`` paths only.
    """

    edge.configure(keys=keys, bound=bound)
    for host in HOSTILE_HOSTS:
        for path in LEGACY_GET_PATHS:
            outcome = edge.call("GET", path, host=host, query={"user_id": USER})
            assert outcome.refused, (host, path, outcome.status)
            assert not outcome.reached_handler, (host, path)
        # A write through the page's own origin: only Host can refuse it.
        write = edge.call(
            "POST",
            LEGACY_POST_PATH,
            host=host,
            origin=None if host is None else f"http://{host}",
            body=json.dumps({"user_id": USER, "raw_content": "REWRITTEN"}).encode(),
            content_type="application/json",
        )
        assert write.refused, (host, write.status)
        assert not write.reached_handler, host


@pytest.mark.parametrize("bound", (True, False), ids=("bound", "unbound"))
def test_legacy_v0_loopback_hosts_still_reach_the_handler(edge: Edge, bound: bool) -> None:
    """A local client keeps working on every legacy route, with any loopback spelling.

    Mutation: refuse every Host on the non-vnext paths.
    """

    edge.configure(bound=bound)
    for host in LOOPBACK_HOSTS:
        for path in LEGACY_GET_PATHS:
            outcome = edge.call("GET", path, host=host, query={"user_id": USER})
            assert outcome.reached_handler, (host, path, outcome.status)


def test_a_key_shaped_authorization_header_does_not_exempt_a_legacy_v0_request(edge: Edge) -> None:
    """The legacy routes never check a key, so a header that looks like one proves nothing.

    A rebound page sets any header it likes on its own origin. Mutation: skip
    the legacy Host and Origin check when ``agent_key_from_authorization``
    finds a key-shaped Bearer value.
    """

    edge.configure(keys=1)
    for authorization in (f"Bearer {KEY}", "Bearer alice_sk_v1_notarealkey_" + "0" * 20):
        outcome = edge.call(
            "GET",
            "/v0/memories",
            host="attacker.invalid",
            query={"user_id": USER},
            authorization=authorization,
        )
        assert outcome.refused, authorization
        assert not outcome.reached_handler, authorization


def test_a_cross_origin_request_to_a_legacy_v0_route_is_refused(edge: Edge) -> None:
    """A blind cross-origin request from another page, with a loopback Host, is refused; a configured or own origin is not.

    Mutation: check ``Host`` and skip ``Origin`` on the non-vnext paths.
    """

    edge.configure(bound=True)
    for origin in ("https://evil.example", "null", "http://localhost:5173"):
        for path in LEGACY_GET_PATHS:
            outcome = edge.call("GET", path, origin=origin, query={"user_id": USER})
            assert outcome.refused, (origin, path, outcome.status)
            assert not outcome.reached_handler, (origin, path)
    edge.configure(bound=True, cors_allowed_origins=("http://127.0.0.1:3000",))
    for origin in ("http://127.0.0.1:3000", "http://127.0.0.1:8000"):
        outcome = edge.call("GET", "/v0/memories", origin=origin, query={"user_id": USER})
        assert outcome.reached_handler, origin


def test_the_operator_host_list_admits_a_named_host_on_the_legacy_v0_routes(edge: Edge) -> None:
    """``ALICEBOT_ALLOWED_HOSTS`` applies to the legacy routes as to vNext. Mutation: use only the loopback names there."""

    edge.configure(allowed_hosts=("alice.lan",))
    assert edge.call("GET", "/v0/memories", host="alice.lan:8000", query={"user_id": USER}).reached_handler
    assert edge.call("GET", "/v0/memories", host="evil.alice.lan", query={"user_id": USER}).refused


def test_a_preflight_to_a_legacy_v0_route_is_answered_by_the_cors_layer(edge: Edge) -> None:
    """A preflight reads nothing and is not refused by the Host rule, as on vNext: the CORS layer answers it.

    Mutation: remove the ``OPTIONS`` exemption from the legacy check in
    ``enforce_authenticated_user_identity``. The preflight below then answers 401.
    """

    edge.configure(cors_allowed_origins=("http://127.0.0.1:3000",))
    preflight = [("access-control-request-method", "GET")]
    allowed = edge.call(
        "OPTIONS",
        "/v0/memories",
        host="rebind.evil.example:8000",
        origin="http://127.0.0.1:3000",
        extra_headers=preflight,
    )
    assert allowed.status == 204
    refused = edge.call(
        "OPTIONS",
        "/v0/memories",
        host="rebind.evil.example:8000",
        origin="https://evil.example",
        extra_headers=preflight,
    )
    assert refused.status == 403


# Origin on a keyless request, with no rebinding at all.


def test_cross_origin_requests_with_no_body_are_refused(edge: Edge) -> None:
    """The blind POSTs a Host check cannot see: ``/v1/evals/runs``, ``/v1/workspaces/bootstrap``, and a vNext read.

    Mutations, each one alone: make ``origin_refusal`` return None for every
    origin (both gates share it); treat ``null`` as no Origin.
    """

    edge.configure(bound=True)
    for origin in ("https://evil.example", "null", "http://localhost:5173"):
        for method, path, query in (
            ("POST", EVAL_RUNS_PATH, {"suite_key": "x"}),
            ("POST", BOOTSTRAP_PATH, None),
            ("GET", V1_LIST_PATH, None),
            ("GET", CHARTER_PATH, None),
        ):
            outcome = edge.call(method, path, origin=origin, query=query, extra_headers=[("x-alicebot-user-id", USER)])
            assert outcome.refused, (origin, method, path, outcome.status)
            if path.startswith("/v1"):
                assert outcome.resolver_calls == 0, (origin, path)


def test_configured_and_own_origins_pass(edge: Edge) -> None:
    """A listed origin and the request's own origin reach the handler. Mutation: ignore ``CORS_ALLOWED_ORIGINS``."""

    edge.configure(cors_allowed_origins=("http://127.0.0.1:3000",))
    assert edge.charter(origin="http://127.0.0.1:3000").status == 200
    assert edge.charter(origin="http://127.0.0.1:8000").status == 200
    assert edge.charter(origin="http://127.0.0.1:3001").refused


def test_a_wildcard_cors_entry_does_not_admit_a_keyless_cross_origin_request(edge: Edge) -> None:
    """``CORS_ALLOWED_ORIGINS=*`` widens what a browser may read, not who may write keyless.

    Mutation: treat ``*`` as matching every origin in ``origin_refusal``.
    """

    edge.configure(cors_allowed_origins=("*",))
    assert edge.charter(origin="https://evil.example").refused
    assert edge.charter().status == 200


def test_the_dev_console_origins_in_the_shipped_defaults_are_allowed() -> None:
    """The console at the origins ``.env.example`` lists reaches the API base URL it lists.

    Mutation: make the Origin check refuse a listed origin when the Host is the
    console's API base URL.
    """

    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    origins = tuple(item for item in values["CORS_ALLOWED_ORIGINS"].split(",") if item)
    api_base = values["NEXT_PUBLIC_ALICEBOT_API_BASE_URL"]
    assert origins, "the example file lists no console origin"
    host = api_base.split("://", 1)[1]
    for origin in origins:
        reason = keyless_edge_refusal(
            host_values=[host],
            origin_values=[origin],
            request_scheme="http",
            allowed_hosts=(),
            allowed_origins=origins,
        )
        assert reason is None, (origin, reason)


def test_allowed_hosts_setting_is_read_from_the_environment_and_refuses_a_wildcard() -> None:
    """The environment value is split, normalized and validated. Mutation: skip ``_parse_allowed_hosts``."""

    settings = Settings.from_env({"ALICEBOT_ALLOWED_HOSTS": "Alice.LAN, [fd00::5], alice.lan"})
    assert settings.allowed_hosts == ("alice.lan", "fd00::5")
    assert Settings.from_env({}).allowed_hosts == ()
    for bad in ("*.alice.lan", "alice.lan:8000", "https://alice.lan"):
        with pytest.raises(ValueError, match="ALICEBOT_ALLOWED_HOSTS"):
            Settings.from_env({"ALICEBOT_ALLOWED_HOSTS": bad})
