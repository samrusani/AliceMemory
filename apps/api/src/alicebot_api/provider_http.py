"""The one outbound HTTP door for provider, embeddings, reranker, fact-key and Google clients.

In v0.19.2 each of those clients called the standard library's ``urlopen``,
which follows up to ten redirects and sends the request's headers on to the
target, ``Authorization`` and ``api-key`` included. A POST answered 301, 302 or
303 becomes a bodyless GET there. The provider helpers checked the base URL once,
before the request, so neither a redirect nor a DNS answer that changed after
the check was held to the outbound policy.

``open_provider_url`` builds its own opener and is the only place in
``apps/api/src`` that opens a URL, apart from the headless reachability check in
``cli/smokes.py``; a test fails for any other call to ``urlopen`` or
``build_opener``.

* Only ``http`` and ``https`` are spoken. The opener has no FTP, file or data
  handler, so such a URL, or a redirect to one, is an error.
* A redirect is never followed. The redirect handler declines every 30x, so the
  answer falls through to the default error handler and the caller sees the
  ``HTTPError`` it already maps to "returned HTTP 30x". The target is never
  contacted, so no header is sent on to it. A provider that really answers with
  a 301 or 308, such as an http to https upgrade, has to be configured with its
  final URL.
* ``enforce_public_peer`` has no default, so each call site states it. When it is
  true the address the connection goes to must pass the policy
  ``validate_provider_base_url`` applies (not loopback, private, link-local,
  multicast or otherwise non-global). The name is resolved when the connection is
  made and only an allowed address is dialled, so a name that was public when the
  base URL was validated and loopback when the connection is made (DNS
  rebinding) is refused before a byte is sent, over http and over https. The
  address actually connected to is checked as well.
* The proxy handler stays, because ``urlopen`` honours ``HTTP_PROXY`` and
  ``HTTPS_PROXY`` and an operator may depend on that. When a proxy carries the
  request the peer is the proxy, an address the operator chose, so the check is
  skipped for that request. A redirect from behind a proxy is still refused.

The embeddings, reranker, fact-key and brain clients pass ``False``: the README
points ``ALICE_EMBEDDINGS_BASE_URL`` at ``http://localhost:11434/v1``, which a
public-peer rule would break, and refusing redirects is the control that matters
there.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.request
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, TypeVar

from alicebot_api.provider_security import (
    REDIRECT_NOTE,
    REDIRECT_STATUS_CODES,
    _is_disallowed_ip,
    redirect_note,
)

__all__ = ["OutboundPeerRefused", "REDIRECT_NOTE", "REDIRECT_STATUS_CODES", "open_provider_url", "redirect_note"]

logger = logging.getLogger(__name__)


class OutboundPeerRefused(OSError):
    """A connection was refused because its address is not allowed by the outbound policy.

    An ``OSError``, so the standard library's opener reports it as a ``URLError``,
    which every client already maps to its "request failed" error.
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # None makes the redirect fall through to the default error handler,
        # which raises the HTTPError for the 30x.
        return None


def _address_of(sockaddr: tuple[Any, ...]) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])


def _refuse(sock: socket.socket | None) -> OutboundPeerRefused:
    if sock is not None:
        sock.close()
    logger.warning("outbound connection refused: the address is not allowed by the outbound policy")
    return OutboundPeerRefused("connection address is not allowed by the outbound policy")


def _connect_to_public_peer(
    address: tuple[str, int],
    timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a connection to ``address``, dialling only addresses the outbound policy allows.

    The name is resolved here, so the answer that is checked is the answer that is
    used. A disallowed address is never dialled, so a refused connection has
    neither connected nor sent a byte. The peer of the connected socket is
    checked again before the socket is returned.
    """

    host, port = address
    last_error: OSError | None = None
    for family, socktype, proto, _canonical, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if _is_disallowed_ip(_address_of(sockaddr)):
            continue
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            last_error = exc
            continue
        if _is_disallowed_ip(_address_of(sock.getpeername())):
            raise _refuse(sock)
        return sock
    if last_error is not None:
        raise last_error
    # Every address the name resolved to was refused, so none was dialled.
    raise _refuse(None)


def _carried_by_proxy(req: urllib.request.Request) -> bool:
    """Report whether a proxy carries the request, so the connection goes to the proxy.

    An http request through a proxy has its selector set to the full URL. An
    https request through one is tunnelled: the opener keeps the request's own
    selector and records the real host as the tunnel host.
    """

    return req.has_proxy() or bool(getattr(req, "_tunnel_host", None))


_Connection = TypeVar("_Connection", bound=HTTPConnection)


def _guard(connection: _Connection, *, enforce_public_peer: bool) -> _Connection:
    if enforce_public_peer:
        # http.client dials through this attribute, for http and, before it
        # wraps the socket in TLS, for https.
        connection._create_connection = _connect_to_public_peer  # type: ignore[attr-defined,assignment]
    return connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, *, enforce_public_peer: bool) -> None:
        super().__init__()
        self._enforce_public_peer = enforce_public_peer

    def http_open(self, req: urllib.request.Request) -> Any:
        enforce = self._enforce_public_peer and not _carried_by_proxy(req)

        def connection(host: str, **kwargs: Any) -> HTTPConnection:
            return _guard(HTTPConnection(host, **kwargs), enforce_public_peer=enforce)

        return self.do_open(connection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, enforce_public_peer: bool) -> None:
        super().__init__()
        self._enforce_public_peer = enforce_public_peer

    def https_open(self, req: urllib.request.Request) -> Any:
        enforce = self._enforce_public_peer and not _carried_by_proxy(req)

        def connection(host: str, **kwargs: Any) -> HTTPSConnection:
            return _guard(HTTPSConnection(host, **kwargs), enforce_public_peer=enforce)

        return self.do_open(connection, req, context=self._context)  # type: ignore[attr-defined]


def _build_opener(*, enforce_public_peer: bool) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    handlers: tuple[urllib.request.BaseHandler, ...] = (
        urllib.request.ProxyHandler(),
        _HTTPHandler(enforce_public_peer=enforce_public_peer),
        _HTTPSHandler(enforce_public_peer=enforce_public_peer),
        _NoRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),
    )
    for handler in handlers:
        opener.add_handler(handler)
    return opener


def open_provider_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    enforce_public_peer: bool,
) -> Any:
    """Open ``request`` and return the response, usable as a context manager like ``urlopen``'s.

    An HTTP error status, redirects included, raises ``urllib.error.HTTPError``;
    a failure to connect raises ``URLError``, and so does a connection refused
    by the outbound policy. See the module docstring for what each argument
    changes. ``enforce_public_peer`` has no default on purpose.
    """

    opener = _build_opener(enforce_public_peer=enforce_public_peer)
    return opener.open(request, timeout=timeout)

