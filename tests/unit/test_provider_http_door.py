"""Every outbound provider call goes through one door that follows no redirect and checks the peer (DB-009).

The internal security review of v0.19.0 found that the provider helpers, the
embeddings, reranker and fact-key clients and the vNext brain client all called
the standard library's ``urlopen``. It follows up to ten redirects and sends the
request's headers, ``Authorization`` and ``api-key`` included, on to the target,
and a POST answered 301, 302 or 303 becomes a bodyless GET. The provider helpers
also checked the base URL once, before the request, so a DNS answer that changed
after the check was never held to the outbound policy.

``open_provider_url`` is the one door. The tests use real sockets on 127.0.0.1
only: a redirecting server, a target that records every request, a raw TCP sink
that records every connection and byte, and a small proxy. DNS is a ``getaddrinfo``
shim, so nothing leaves the machine. Each test names the mutation that must fail
it.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import uuid4

import pytest

import alicebot_api.azure_provider_helpers as azure_helpers
import alicebot_api.calendar as calendar_module
import alicebot_api.gmail as gmail_module
import alicebot_api.local_provider_helpers as local_helpers
import alicebot_api.provider_http as provider_http
import alicebot_api.response_generation as response_generation
import alicebot_api.vnext_embeddings as vnext_embeddings
import alicebot_api.vnext_fact_keys as vnext_fact_keys
import alicebot_api.vnext_model_intelligence as vnext_model_intelligence
import alicebot_api.vnext_reranker as vnext_reranker
from alicebot_api.contracts import ModelInvocationRequest
from alicebot_api.provider_http import OutboundPeerRefused, open_provider_url
from alicebot_api.provider_runtime import build_provider_test_model_request
from alicebot_api.provider_security import (
    REDIRECT_NOTE,
    redirect_note,
    sanitize_provider_error_message,
)
from alicebot_api.response_generation import (
    ModelProviderUnavailableError,
    OpenAICompatibleTransportConfig,
    invoke_openai_compatible_model,
)
from alicebot_api.vnext_embeddings import OpenAICompatibleEmbeddingProvider
from alicebot_api.vnext_fact_keys import OpenAICompatibleFactKeyProvider
from alicebot_api.vnext_model_intelligence import OpenAIResponsesBrainModelProvider
from alicebot_api.vnext_reranker import OpenAICompatibleRerankProvider

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "apps" / "api" / "src" / "alicebot_api"
# Built at run time so the source carries no credential-shaped literal.
TOKEN = "sk-fake-" + "abcdefghijklmnopqrstuvwx"
PUBLIC_IP = "93.184.216.34"  # a global address, used only as a DNS answer. Nothing connects to it.
REBIND_HOST = "rebind.example.test"
REDIRECT_CODES = (301, 302, 303, 307, 308)


# Local servers.


class _Target(BaseHTTPRequestHandler):
    """Where a redirect points. Records every request, with its Authorization header."""

    hits: list[tuple[str, str, str | None]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _answer(self) -> None:
        _Target.hits.append((self.command, self.path, self.headers.get("Authorization") or self.headers.get("api-key")))
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer


class _Hop(BaseHTTPRequestHandler):
    """Answers every request with a redirect to ``target``."""

    code = 302
    target = ""

    def log_message(self, *args: Any) -> None:
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(_Hop.code)
        self.send_header("Location", _Hop.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _answer


class _Json(BaseHTTPRequestHandler):
    """Answers every request with 200 and a body ``payload`` that tests set."""

    payload: bytes = b"{}"

    def log_message(self, *args: Any) -> None:
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_Json.payload)))
        self.end_headers()
        self.wfile.write(_Json.payload)

    do_GET = do_POST = _answer


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class _Sink:
    """A raw TCP listener on 127.0.0.1 that records every connection and every byte it receives."""

    def __init__(self, *, reply: bytes = b"") -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = int(self.server.getsockname()[1])
        self.connections = 0
        self.received = b""
        self.reply = reply
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.server.settimeout(0.05)
        while not self._stop:
            try:
                client, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._read, args=(client,), daemon=True).start()

    def _read(self, client: socket.socket) -> None:
        client.settimeout(0.3)
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                self.received += data
                if self.reply:
                    client.sendall(self.reply)
                    self.reply = b""
        except (TimeoutError, OSError):
            pass
        finally:
            client.close()

    def close(self) -> None:
        self._stop = True
        self.server.close()
        self._thread.join(2)


@contextmanager
def _sink(**kwargs: Any) -> Iterator[_Sink]:
    sink = _Sink(**kwargs)
    try:
        yield sink
    finally:
        sink.close()


def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scaffolding for tests about redirects: let the first hop, which is on loopback, through the policy."""

    monkeypatch.setattr(provider_http, "_is_disallowed_ip", lambda ip: False)
    monkeypatch.setattr(local_helpers, "validate_provider_base_url", lambda url, **kwargs: url)
    monkeypatch.setattr(azure_helpers, "validate_provider_base_url", lambda url, **kwargs: url)


def _clear_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_environment(monkeypatch)


# The nine clients, each driven against a base URL.


def _clients(base: str) -> dict[str, Callable[[], Any]]:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    model_request: ModelInvocationRequest = build_provider_test_model_request(
        runtime_provider="openai_responses", model="m", prompt_text="hi"
    )
    return {
        "request_json_get": lambda: local_helpers.request_json(
            method="GET", base_url=base, path="/m", timeout_seconds=5, headers=auth
        ),
        "request_json_post": lambda: local_helpers.request_json(
            method="POST", base_url=base, path="/m", timeout_seconds=5, headers=auth, payload={"a": 1}
        ),
        "request_ok": lambda: local_helpers.request_ok(
            method="GET", base_url=base, path="/m", timeout_seconds=5, headers=auth
        ),
        "azure": lambda: azure_helpers.request_azure_json(
            method="GET", base_url=base, path="/m", api_version="v", timeout_seconds=5, headers={"api-key": TOKEN}
        ),
        "openai_invoke": lambda: invoke_openai_compatible_model(
            transport=OpenAICompatibleTransportConfig(
                base_url=base, api_key=TOKEN, timeout_seconds=5, invoke_path="/responses", auth_mode="bearer"
            ),
            request=model_request,
        ),
        "embeddings": lambda: OpenAICompatibleEmbeddingProvider(base_url=base, model="m", api_key=TOKEN).embed_text("x"),
        "reranker": lambda: OpenAICompatibleRerankProvider(base_url=base, model="m", api_key=TOKEN).complete("p"),
        "fact_keys": lambda: OpenAICompatibleFactKeyProvider(base_url=base, model="m", api_key=TOKEN).suggest_keys("t"),
        "brain": lambda: OpenAIResponsesBrainModelProvider(model="m", base_url=base, api_key=TOKEN).chat(
            prompt="x", temperature=0.0
        ),
    }


CLIENT_NAMES = list(_clients("http://127.0.0.1:1"))


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize("name", CLIENT_NAMES)
def test_a_redirect_is_never_followed_by_any_client(monkeypatch: pytest.MonkeyPatch, name: str, code: int) -> None:
    """Nine clients, five codes: the target gets no request, so no header is sent on, and the error says so.

    The error names the status and tells the operator to set the final base URL.
    Mutations, each one alone: register the standard library's
    ``HTTPRedirectHandler`` in place of ``_NoRedirectHandler`` (33 cases fail: a
    GET follows any code, a POST 301, 302 and 303 becomes a GET); make the
    handler follow the redirect but strip ``Authorization`` and ``api-key`` (the
    same 33 fail, so a header-strip-only fix is caught).
    """

    _allow_loopback(monkeypatch)
    _Target.hits = []
    with _serve(_Target) as target_port, _serve(_Hop) as hop_port:
        _Hop.code = code
        _Hop.target = f"http://127.0.0.1:{target_port}/internal"
        with pytest.raises(Exception) as caught:
            _clients(f"http://127.0.0.1:{hop_port}")[name]()
    assert _Target.hits == [], f"{name} followed a {code} redirect and sent {_Target.hits}"
    message = str(caught.value)
    assert f"HTTP {code}" in message
    assert REDIRECT_NOTE in message


@pytest.mark.parametrize("scheme", ("ftp", "file", "data"))
def test_a_redirect_to_a_scheme_other_than_http_is_not_followed(monkeypatch: pytest.MonkeyPatch, scheme: str) -> None:
    """A redirect to ftp, file or data goes nowhere: the sink sees no connection and the error is the 302.

    Mutations, each one alone: register ``FTPHandler`` and let the redirect
    handler follow an ftp hop; register ``FileHandler`` or ``DataHandler``.
    """

    _allow_loopback(monkeypatch)
    with _sink() as sink, _serve(_Hop) as hop_port:
        _Hop.code = 302
        _Hop.target = {
            "ftp": f"ftp://127.0.0.1:{sink.port}/x",
            "file": "file:///etc/hosts",
            "data": "data:text/plain,hello",
        }[scheme]
        with pytest.raises(HTTPError) as caught:
            open_provider_url(Request(f"http://127.0.0.1:{hop_port}/m"), timeout=5, enforce_public_peer=True)
    assert caught.value.code == 302
    assert sink.connections == 0


@pytest.mark.parametrize("url", ("ftp://127.0.0.1:9/x", "file:///etc/hosts", "data:text/plain,hello", "gopher://127.0.0.1/x"))
def test_the_opener_speaks_only_http_and_https(url: str) -> None:
    """A URL of any other scheme is an error and opens nothing.

    Mutation: add ``FTPHandler``, ``FileHandler`` or ``DataHandler`` to the
    opener. ``file:///etc/hosts`` is then read.
    """

    with pytest.raises(URLError, match="unknown url type"):
        open_provider_url(Request(url), timeout=2, enforce_public_peer=False)


# DNS rebinding.


def _answers(monkeypatch: pytest.MonkeyPatch, port: int, *, first: str, then: str) -> list[str]:
    """Make ``getaddrinfo`` answer ``first`` for the rebinding name once and ``then`` ever after."""

    real = socket.getaddrinfo
    calls: list[str] = []

    def shim(host: Any, service: Any, *args: Any, **kwargs: Any) -> Any:
        if host != REBIND_HOST:
            return real(host, service, *args, **kwargs)
        address = first if not calls else then
        calls.append(address)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr: tuple[Any, ...] = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", shim)
    return calls


@pytest.mark.parametrize("scheme", ("http", "https"))
def test_a_name_that_resolves_to_loopback_when_it_connects_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    """DNS rebinding: public when the base URL is checked, loopback when the connection is made.

    The door resolves the name itself and dials no address the policy refuses, so the sink sees no
    connection and no byte, for http and for https (the TLS hello would be the first byte). With
    ``enforce_public_peer`` false the same request does reach the sink, which proves the sink would
    see a leak. Mutations, each one alone: delete the peer check from ``_guard`` (the http case
    fails); pass ``enforce_public_peer=False`` to the https connection (the https case fails);
    connect first and check the peer after (the sink sees a connection).
    """

    with _sink() as sink:
        _answers(monkeypatch, sink.port, first=PUBLIC_IP, then="127.0.0.1")
        # The door's own resolution is the second answer, so the first one stands for validation.
        socket.getaddrinfo(REBIND_HOST, sink.port)
        with pytest.raises(URLError) as caught:
            open_provider_url(Request(f"{scheme}://{REBIND_HOST}:{sink.port}/x"), timeout=3, enforce_public_peer=True)
        assert isinstance(caught.value.reason, OutboundPeerRefused)
        assert sink.connections == 0
        assert sink.received == b""

        # The sink answers nothing, so the client fails after it has sent. How it fails does not matter.
        with pytest.raises(Exception):  # noqa: B017
            open_provider_url(Request(f"{scheme}://{REBIND_HOST}:{sink.port}/x"), timeout=1, enforce_public_peer=False)
        assert sink.connections == 1
        assert sink.received != b""


def test_a_provider_helper_refuses_the_rebound_name_after_it_validated_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through ``request_json``: the base URL passes validation on a public answer, then the connection is refused.

    The error is the one every connection failure is. Mutation: construct the
    helpers' connection with ``enforce_public_peer=False``.
    """

    with _sink() as sink:
        calls = _answers(monkeypatch, sink.port, first=PUBLIC_IP, then="127.0.0.1")
        with pytest.raises(ModelProviderUnavailableError):
            local_helpers.request_json(
                method="GET", base_url=f"http://{REBIND_HOST}:{sink.port}", path="/m", timeout_seconds=3
            )
        assert calls[0] == PUBLIC_IP and "127.0.0.1" in calls[1:]
        assert sink.connections == 0 and sink.received == b""


@pytest.mark.parametrize(
    "address",
    ("127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.1", "169.254.169.254", "fe80::1", "0.0.0.0", "100.64.0.1"),
)
def test_no_disallowed_address_is_dialled(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    """Loopback, mapped loopback, private, link-local, unspecified and shared space: never connected to.

    Mutation: skip the ``_is_disallowed_ip`` test on the resolved addresses in
    ``_connect_to_public_peer``. A connect call is then recorded.
    """

    dialled: list[Any] = []
    real_connect = socket.socket.connect

    def record(self: socket.socket, sockaddr: Any) -> Any:
        dialled.append(sockaddr)
        return real_connect(self, sockaddr)

    _answers(monkeypatch, 9, first=address, then=address)
    monkeypatch.setattr(socket.socket, "connect", record)
    with pytest.raises(URLError) as caught:
        open_provider_url(Request(f"http://{REBIND_HOST}:9/x"), timeout=1, enforce_public_peer=True)
    assert isinstance(caught.value.reason, OutboundPeerRefused)
    assert dialled == []


def test_only_an_allowed_address_is_dialled_and_the_next_is_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name with a refused address first and an allowed one second reaches the allowed one, and never dials the first.

    The policy is narrowed to refuse IPv6 so loopback IPv4 can stand for an allowed address.
    Mutations: dial the addresses in the order given without the policy test (the refused one is
    dialled); stop at the first address that connects without the test.
    """

    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    dialled: list[Any] = []
    real_connect = socket.socket.connect

    def record(self: socket.socket, sockaddr: Any) -> Any:
        dialled.append(sockaddr)
        return real_connect(self, sockaddr)

    real = socket.getaddrinfo

    with _sink(reply=reply) as sink:

        def shim(host: Any, service: Any, *args: Any, **kwargs: Any) -> Any:
            if host != REBIND_HOST:
                return real(host, service, *args, **kwargs)
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", sink.port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", sink.port)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", shim)
        monkeypatch.setattr(provider_http, "_is_disallowed_ip", lambda ip: ip.version == 6)
        monkeypatch.setattr(socket.socket, "connect", record)
        with open_provider_url(Request(f"http://{REBIND_HOST}:{sink.port}/x"), timeout=3, enforce_public_peer=True) as response:
            assert response.read() == b"{}"
    assert dialled == [("127.0.0.1", sink.port)]


def test_the_connected_peer_is_checked_again_before_a_byte_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: an address that passed when it was resolved but is refused as the connected peer.

    The policy is made to allow the first look and refuse the second, as if the answer had changed
    between them. The connection opens, is closed, and nothing is sent. Mutation: delete the check
    of ``sock.getpeername()`` in ``_connect_to_public_peer``. The request is then sent.
    """

    looks: list[str] = []

    def policy(ip: Any) -> bool:
        looks.append(str(ip))
        return len(looks) > 1

    with _sink() as sink:
        monkeypatch.setattr(provider_http, "_is_disallowed_ip", policy)
        with pytest.raises(URLError) as caught:
            open_provider_url(Request(f"http://127.0.0.1:{sink.port}/x"), timeout=3, enforce_public_peer=True)
        assert isinstance(caught.value.reason, OutboundPeerRefused)
        assert sink.received == b""
    assert looks == ["127.0.0.1", "127.0.0.1"]


# The proxy case.


class _Proxy(BaseHTTPRequestHandler):
    """A forward proxy that records the request line and answers with ``mode``: ok, redirect or refuse."""

    seen: list[str] = []
    mode = "ok"
    target = ""

    def log_message(self, *args: Any) -> None:
        pass

    def _forward(self) -> None:
        _Proxy.seen.append(f"{self.command} {self.path}")
        if _Proxy.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", _Proxy.target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"via": "proxy"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _forward

    def do_CONNECT(self) -> None:  # noqa: N802
        _Proxy.seen.append(f"CONNECT {self.path}")
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_a_request_carried_by_a_proxy_is_not_held_to_the_public_peer_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The peer is the proxy, an address the operator chose, so a loopback proxy is used and the request is sent to it.

    Without the proxy the same request, to a name that resolves to loopback, is refused. The
    standard library's ``urlopen`` honoured ``HTTP_PROXY`` and so does the door. Mutation: apply
    the peer check to the connection to the proxy (the request is refused and the proxy sees nothing).
    """

    _Proxy.seen = []
    _Proxy.mode = "ok"
    with _serve(_Proxy) as proxy_port, _sink() as sink:
        _answers(monkeypatch, sink.port, first="127.0.0.1", then="127.0.0.1")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        with open_provider_url(Request("http://provider.example.test/v1/models"), timeout=3, enforce_public_peer=True) as response:
            assert json.loads(response.read()) == {"via": "proxy"}
        assert _Proxy.seen == ["GET http://provider.example.test/v1/models"]
        monkeypatch.delenv("http_proxy")
        with pytest.raises(URLError) as caught:
            open_provider_url(Request(f"http://{REBIND_HOST}:{sink.port}/x"), timeout=3, enforce_public_peer=True)
        assert isinstance(caught.value.reason, OutboundPeerRefused)


def test_an_https_request_through_a_proxy_reaches_the_proxy_and_is_not_refused_as_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An https request is tunnelled with CONNECT. The loopback proxy is dialled and sees the CONNECT.

    The proxy here answers 403, so the request fails as a tunnel error, not as a refused peer.
    Mutation: decide that a proxy carries the request from ``has_proxy()`` alone. An https request
    keeps its own selector and records a tunnel host, so it is then refused before CONNECT.
    """

    _Proxy.seen = []
    with _serve(_Proxy) as proxy_port:
        monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{proxy_port}")
        with pytest.raises(URLError) as caught:
            open_provider_url(Request("https://provider.example.test/v1/models"), timeout=3, enforce_public_peer=True)
    assert not isinstance(caught.value.reason, OutboundPeerRefused)
    assert _Proxy.seen == ["CONNECT provider.example.test:443"]


def test_a_redirect_from_behind_a_proxy_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy answers 302. The target is never contacted. Mutation: follow redirects when a proxy is in use."""

    _Target.hits = []
    _Proxy.seen = []
    _Proxy.mode = "redirect"
    try:
        with _serve(_Target) as target_port, _serve(_Proxy) as proxy_port:
            _Proxy.target = f"http://127.0.0.1:{target_port}/internal"
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            with pytest.raises(HTTPError) as caught:
                open_provider_url(Request("http://provider.example.test/m"), timeout=3, enforce_public_peer=True)
    finally:
        _Proxy.mode = "ok"
    assert caught.value.code == 302
    assert _Target.hits == []
    assert _Proxy.seen == ["GET http://provider.example.test/m"]


# The signature, the timeout and the happy path.


def test_enforce_public_peer_and_timeout_are_required_keywords() -> None:
    """No default for either, so a call site must say what it wants. Mutation: give ``enforce_public_peer`` a default."""

    parameters = inspect.signature(open_provider_url).parameters
    for name in ("timeout", "enforce_public_peer"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is inspect.Parameter.empty, name
    with pytest.raises(TypeError):
        open_provider_url(Request("http://127.0.0.1:9/"), timeout=1)  # type: ignore[call-arg]


@pytest.mark.parametrize("enforce", (False, True))
def test_a_silent_peer_times_out_instead_of_hanging(monkeypatch: pytest.MonkeyPatch, enforce: bool) -> None:
    """The caller's timeout is the socket's, on both connection paths. The thread is joined within seconds.

    Mutation: drop ``sock.settimeout(timeout)`` from the guarded connection (the ``True`` case hangs
    until the sink closes it).
    """

    monkeypatch.setattr(provider_http, "_is_disallowed_ip", lambda ip: False)
    outcome: list[BaseException] = []

    def call(port: int) -> None:
        try:
            open_provider_url(Request(f"http://127.0.0.1:{port}/x"), timeout=0.3, enforce_public_peer=enforce)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    silent = socket.create_server(("127.0.0.1", 0))
    silent.listen(5)
    try:
        thread = threading.Thread(target=call, args=(int(silent.getsockname()[1]),), daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    finally:
        silent.close()
    assert len(outcome) == 1
    assert isinstance(outcome[0], (URLError, TimeoutError))


@pytest.mark.parametrize("enforce", (False, True))
def test_an_ordinary_response_comes_back_whole_on_both_paths(monkeypatch: pytest.MonkeyPatch, enforce: bool) -> None:
    """A 200 with a JSON body is returned, readable, and a 404 is an HTTPError with its code.

    Mutation: build the opener without ``HTTPErrorProcessor`` (a 404 is then returned as a response).
    """

    monkeypatch.setattr(provider_http, "_is_disallowed_ip", lambda ip: False)
    _Json.payload = b'{"ok": true}'
    with _serve(_Json) as port:
        with open_provider_url(Request(f"http://127.0.0.1:{port}/x"), timeout=3, enforce_public_peer=enforce) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"ok": True}
    with _sink(reply=b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n") as sink:
        with pytest.raises(HTTPError) as caught:
            open_provider_url(Request(f"http://127.0.0.1:{sink.port}/x"), timeout=3, enforce_public_peer=enforce)
    assert caught.value.code == 404


def test_a_local_embeddings_endpoint_still_works_with_the_door() -> None:
    """The documented setup: ``ALICE_EMBEDDINGS_BASE_URL=http://localhost:11434/v1`` is a loopback server.

    The embeddings client does not require a public peer, so a loopback endpoint is used. Mutation: pass
    ``enforce_public_peer=True`` from the embeddings client. The loopback server is then refused.
    """

    _Json.payload = json.dumps({"data": [{"index": 0, "embedding": [0.5, 0.25]}]}).encode()
    with _serve(_Json) as port:
        vectors = OpenAICompatibleEmbeddingProvider(base_url=f"http://127.0.0.1:{port}/v1", model="m").embed_batch(["x"])
    assert vectors[0][:2] == [0.5, 0.25]


# What each call site asks for.


class _Seen(Exception):
    pass


def _record_flag(monkeypatch: pytest.MonkeyPatch, module: Any, seen: list[dict[str, Any]]) -> None:
    def recorder(request: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _Seen()

    monkeypatch.setattr(module, "open_provider_url", recorder)


_CALL_SITES: tuple[tuple[str, Any, Callable[[], Any], bool], ...] = (
    ("local request_json", local_helpers, lambda: local_helpers.request_json(method="GET", base_url="http://x.example.test", path="/m", timeout_seconds=5), True),
    ("local request_ok", local_helpers, lambda: local_helpers.request_ok(method="GET", base_url="http://x.example.test", path="/m", timeout_seconds=5), True),
    ("azure", azure_helpers, lambda: azure_helpers.request_azure_json(method="GET", base_url="http://x.example.test", path="/m", api_version="v", timeout_seconds=5), True),
    (
        "response generation",
        response_generation,
        lambda: invoke_openai_compatible_model(
            transport=OpenAICompatibleTransportConfig(base_url="http://x.example.test", api_key=TOKEN, timeout_seconds=5, invoke_path="/responses", auth_mode="bearer"),
            request=build_provider_test_model_request(runtime_provider="openai_responses", model="m", prompt_text="hi"),
        ),
        True,
    ),
    ("gmail refresh", gmail_module, lambda: gmail_module.refresh_gmail_access_token(gmail_account_id=uuid4(), refresh_token="r", client_id="c", client_secret="s"), True),
    ("gmail fetch", gmail_module, lambda: gmail_module.fetch_gmail_message_raw_bytes(access_token="t", provider_message_id="m"), True),
    ("calendar list", calendar_module, lambda: calendar_module.fetch_calendar_event_list_payload(access_token="t"), True),
    ("calendar event", calendar_module, lambda: calendar_module.fetch_calendar_event_payload(access_token="t", provider_event_id="e"), True),
    ("embeddings", vnext_embeddings, lambda: OpenAICompatibleEmbeddingProvider(base_url="http://x.example.test", model="m").embed_text("x"), False),
    ("reranker", vnext_reranker, lambda: OpenAICompatibleRerankProvider(base_url="http://x.example.test", model="m").complete("p"), False),
    ("fact keys", vnext_fact_keys, lambda: OpenAICompatibleFactKeyProvider(base_url="http://x.example.test", model="m").suggest_keys("t"), False),
    ("brain", vnext_model_intelligence, lambda: OpenAIResponsesBrainModelProvider(model="m", base_url="http://x.example.test", api_key=TOKEN).chat(prompt="x", temperature=0.0), False),
)


@pytest.mark.parametrize(("label", "module", "call", "expected"), _CALL_SITES, ids=[site[0] for site in _CALL_SITES])
def test_each_call_site_states_whether_it_needs_a_public_peer(
    monkeypatch: pytest.MonkeyPatch, label: str, module: Any, call: Callable[[], Any], expected: bool
) -> None:
    """The provider helpers, response generation, Gmail and Calendar need a public peer. The four vNext clients do not.

    The vNext clients are for a local Ollama, vLLM or LM Studio; refusing redirects is their control.
    The helpers' own base URL check already refuses loopback. Mutation: flip the flag at any one call site.
    """

    monkeypatch.setattr(local_helpers, "validate_provider_base_url", lambda url, **kwargs: url)
    monkeypatch.setattr(azure_helpers, "validate_provider_base_url", lambda url, **kwargs: url)
    seen: list[dict[str, Any]] = []
    _record_flag(monkeypatch, module, seen)
    with pytest.raises(_Seen):
        call()
    assert len(seen) == 1, label
    assert seen[0]["enforce_public_peer"] is expected, label
    assert "timeout" in seen[0], label


# One door, and nothing else.

_FORBIDDEN_NAMES = frozenset(
    {
        "urlopen",
        "build_opener",
        "OpenerDirector",
        "install_opener",
        "urlretrieve",
        "URLopener",
        "FancyURLopener",
        "HTTPConnection",
        "HTTPSConnection",
    }
)
# The headless reachability probe in cli/smokes.py dials a URL the operator typed, on purpose, and may be loopback.
_ALLOWED = {"alicebot_api/provider_http.py": _FORBIDDEN_NAMES, "alicebot_api/cli/smokes.py": frozenset({"urlopen"})}


def _forbidden_uses(source: str, filename: str, allowed: frozenset[str]) -> list[tuple[str, int]]:
    uses: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
        for name in names:
            if name in _FORBIDDEN_NAMES and name not in allowed:
                uses.append((name, getattr(node, "lineno", 0)))
    return uses


def test_no_module_opens_a_url_except_through_the_door() -> None:
    """Nothing under ``apps/api/src`` calls ``urlopen`` or builds an opener, apart from the door and one probe.

    Mutations, each one alone: add a module that calls ``urlopen``; move the reranker back to
    ``urlopen``; import ``HTTPSConnection`` in a client.
    """

    offenders: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC.parent).as_posix()
        uses = _forbidden_uses(path.read_text(encoding="utf-8"), relative, _ALLOWED.get(relative, frozenset()))
        if uses:
            offenders[relative] = uses
    assert offenders == {}


def test_the_guard_finds_each_way_to_open_a_url() -> None:
    """The detector's own cases. Mutation: make it look at ``Name`` nodes only, or skip ``ImportFrom``."""

    cases = {
        "from urllib.request import urlopen\n": ["urlopen"],
        "import urllib.request\nurllib.request.urlopen(r)\n": ["urlopen"],
        "import urllib.request as u\nu.build_opener()\n": ["build_opener"],
        "from urllib.request import OpenerDirector as D\n": ["OpenerDirector"],
        "import http.client\nhttp.client.HTTPSConnection('h')\n": ["HTTPSConnection"],
        "x = open_provider_url(r, timeout=1, enforce_public_peer=True)\n": [],
        "import urllib.parse\nurllib.parse.quote('a')\n": [],
    }
    for source, expected in cases.items():
        found = sorted({name for name, _ in _forbidden_uses(source, "case.py", frozenset())})
        assert found == sorted(expected), source


# What the operator reads.


def test_the_redirect_note_names_the_fix_for_a_redirect_and_nothing_else() -> None:
    """A 301, 302, 303, 307 or 308 gets the note. 200, 304, 404 and 500 do not.

    Mutations: add 304 to the codes; delete the note from ``sanitize_provider_error_message``.
    """

    for code in REDIRECT_CODES:
        assert redirect_note(code) == f"; {REDIRECT_NOTE}"
        assert sanitize_provider_error_message(f"model provider returned HTTP {code}") == (
            f"provider upstream request failed with HTTP {code}; {REDIRECT_NOTE}"
        )
    for code in (200, 300, 304, 404, 500):
        assert redirect_note(code) == ""
        assert sanitize_provider_error_message(f"model provider returned HTTP {code}") == (
            f"provider upstream request failed with HTTP {code}"
        )
    assert "set base_url to the final URL" in REDIRECT_NOTE


def test_the_door_is_the_only_place_the_provider_errors_come_from() -> None:
    """A refused peer is an ``OSError``, so it reaches every client as a ``URLError`` it already maps.

    Mutation: make ``OutboundPeerRefused`` a ``ValueError``. It would then escape the clients.
    """

    assert issubclass(OutboundPeerRefused, OSError)
    assert not issubclass(OutboundPeerRefused, ValueError)
