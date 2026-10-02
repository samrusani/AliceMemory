"""Host and Origin rules for a request that presents no agent key.

A keyless API assumes its callers are processes on the machine. A browser on
that machine breaks the assumption in two ways. DNS rebinding points a name the
attacker owns at 127.0.0.1, so a page the attacker serves can call the API with
``Host: attacker.example``. And a cross-origin request needs no rebinding at all
when it is a "simple" one: a form post or a ``fetch`` with ``no-cors`` reaches
the server, and a route that reads nothing from its body, such as
``POST /v1/evals/runs``, does its work.

Both rules apply only to a request with no Bearer key. A keyed request proves
itself with the key, and a key is not something a rebound page holds.

* The ``Host`` header must name this machine: ``localhost``, ``127.0.0.1`` or
  ``::1`` with any port, or a name the operator lists in
  ``ALICEBOT_ALLOWED_HOSTS``. Nothing is matched by prefix, suffix or wildcard.
  A missing, repeated or malformed ``Host`` is refused. ``X-Forwarded-Host`` and
  ``Forwarded`` are never read: the header the client controls is the one a
  rebound page sends, and a trusted proxy is not part of the keyless topology.
* An ``Origin`` header, when one is present, must be an exact entry of
  ``CORS_ALLOWED_ORIGINS`` or the request's own origin (the same host and port
  as the validated ``Host``, over http or https). ``null`` is refused. A
  wildcard entry in ``CORS_ALLOWED_ORIGINS`` does not count: it widens what a
  browser may read back, and it does not make a keyless write acceptable from
  any page.

The checks are pure functions of header values, so they run without a request.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

    from alicebot_api.config import Settings

# Names a keyless request may use for this machine, whatever the port.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

_NAME = r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\.?"
_AUTHORITY = re.compile(
    rf"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>{_NAME}))(?::(?P<port>[0-9]{{1,5}}))?",
    re.ASCII,
)
_ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<authority>[^/?#\s]+)", re.ASCII)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def parse_host(value: str) -> tuple[str, int | None] | None:
    """Return ``(host name, port)`` for a Host header value, or None if malformed.

    The name is lower case, an IPv6 literal is written in its compressed form,
    and one trailing dot is dropped. The whole value must be one authority: a
    userinfo part, a path, a comma list, a space or a non-numeric port is not
    one.
    """

    match = _AUTHORITY.fullmatch(value)
    if match is None:
        return None
    port_text = match.group("port")
    port = None if port_text is None else int(port_text)
    if port is not None and port > 65535:
        return None
    literal = match.group("v6")
    if literal is not None:
        try:
            return ipaddress.IPv6Address(literal).compressed, port
        except ValueError:
            return None
    name = match.group("name").lower()
    if name.endswith("."):
        name = name[:-1]
    return (name, port) if name else None


def parse_allowed_host(entry: str) -> str | None:
    """Normalize one ``ALICEBOT_ALLOWED_HOSTS`` entry, or return None if it is not an exact name.

    An entry is a bare host name or an IP literal (an IPv6 literal with or
    without brackets). A port, a scheme, a path, a wildcard and a blank are not
    exact names.
    """

    text = entry.strip()
    if not text:
        return None
    if ":" in text and not text.startswith("["):
        try:
            return ipaddress.IPv6Address(text).compressed
        except ValueError:
            return None
    parsed = parse_host(text)
    if parsed is None or parsed[1] is not None:
        return None
    return parsed[0]


def host_refusal(host_values: Sequence[str], allowed_hosts: Collection[str]) -> str | None:
    """Return a reason when the Host header does not name this machine, else None."""

    if len(host_values) != 1:
        return "host_header_missing_or_repeated"
    parsed = parse_host(host_values[0])
    if parsed is None:
        return "host_header_malformed"
    name = parsed[0]
    if name in LOOPBACK_HOSTS or name in allowed_hosts:
        return None
    return "host_not_allowed"


def _origin_authority(origin: str) -> tuple[str, str, int | None] | None:
    match = _ORIGIN.fullmatch(origin)
    if match is None:
        return None
    parsed = parse_host(match.group("authority"))
    if parsed is None:
        return None
    return match.group("scheme"), parsed[0], parsed[1]


def origin_refusal(
    origin_values: Sequence[str],
    *,
    host_value: str,
    request_scheme: str,
    allowed_origins: Collection[str],
) -> str | None:
    """Return a reason when an Origin header is neither configured nor the request's own.

    ``host_value`` is a Host header ``host_refusal`` has already accepted. No
    Origin header is no objection: a client that is not a browser sends none.
    """

    if not origin_values:
        return None
    if len(origin_values) != 1:
        return "origin_header_repeated"
    origin = origin_values[0]
    parsed = _origin_authority(origin)
    if parsed is None:
        return "origin_not_allowed"
    if origin in allowed_origins:
        return None
    own = parse_host(host_value)
    if own is None:
        return "origin_not_allowed"
    scheme, name, port = parsed
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    own_port = own[1]
    if own_port == _DEFAULT_PORTS.get(request_scheme):
        own_port = None
    if name == own[0] and port == own_port:
        return None
    return "origin_not_allowed"


def keyless_edge_refusal(
    *,
    host_values: Sequence[str],
    origin_values: Sequence[str],
    request_scheme: str,
    allowed_hosts: Collection[str],
    allowed_origins: Collection[str],
) -> str | None:
    """Return the reason a keyless request is refused at the edge, or None.

    Host first, because the own-origin comparison is only meaningful for a
    host that is already accepted.
    """

    reason = host_refusal(host_values, allowed_hosts)
    if reason is not None:
        return reason
    return origin_refusal(
        origin_values,
        host_value=host_values[0],
        request_scheme=request_scheme,
        allowed_origins=allowed_origins,
    )


def keyless_request_refusal(request: "Request", settings: "Settings") -> str | None:
    """Return why a keyless request's Host or Origin is refused, or None.

    A request from a loopback peer can still be a browser page the attacker
    serves: DNS rebinding sends it with the attacker's name in ``Host``, and a
    simple cross-origin request sends the attacker's page in ``Origin``. Call
    this only for a request that presents no agent key. The two keyless gates in
    ``main.py`` do, right after their peer-address check.
    """

    return keyless_edge_refusal(
        host_values=request.headers.getlist("host"),
        origin_values=request.headers.getlist("origin"),
        request_scheme=str(request.scope.get("scheme", "http")),
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.cors_allowed_origins,
    )
