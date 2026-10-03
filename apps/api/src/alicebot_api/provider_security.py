from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit


ALLOWED_PROVIDER_URL_SCHEMES = frozenset({"http", "https"})
_LOCALHOST_HOSTS = frozenset({"localhost", "localhost.localdomain"})
_HTTP_STATUS_PATTERN = re.compile(r"\bHTTP\s+(\d{3})\b", flags=re.IGNORECASE)
# The statuses the standard library's redirect handler follows. No provider
# client follows them any more (see provider_http).
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
REDIRECT_NOTE = "redirects are not followed; set base_url to the final URL"


class ProviderURLValidationError(ValueError):
    """Raised when a provider base URL violates outbound security policy."""


def redirect_note(status_code: int) -> str:
    """Return the sentence to append to a "returned HTTP <code>" error for a redirect, or an empty string."""

    return f"; {REDIRECT_NOTE}" if status_code in REDIRECT_STATUS_CODES else ""


def validate_provider_base_url(base_url: str, *, require_dns_resolution: bool = True) -> str:
    normalized = base_url.strip()
    if normalized == "":
        raise ProviderURLValidationError("base_url is required")

    parsed = urlsplit(normalized)
    scheme = parsed.scheme.strip().lower()
    if scheme not in ALLOWED_PROVIDER_URL_SCHEMES:
        raise ProviderURLValidationError("base_url scheme must be http or https")

    if parsed.hostname is None or parsed.hostname.strip() == "":
        raise ProviderURLValidationError("base_url host is required")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderURLValidationError("base_url must not include embedded credentials")

    hostname = parsed.hostname.strip().rstrip(".").lower()
    if hostname in _LOCALHOST_HOSTS or hostname.endswith(".localhost"):
        raise ProviderURLValidationError("base_url host is not allowed by outbound policy")

    ip_literal = _parse_ip_literal(hostname)
    if ip_literal is not None and _is_disallowed_ip(ip_literal):
        raise ProviderURLValidationError("base_url host is not allowed by outbound policy")
    if ip_literal is None and require_dns_resolution:
        resolved_addresses = _resolve_hostname_ips(hostname)
        if any(_is_disallowed_ip(address) for address in resolved_addresses):
            raise ProviderURLValidationError("base_url host is not allowed by outbound policy")

    return normalized


def sanitize_provider_error_message(raw_message: str) -> str:
    message = raw_message.strip()
    if message == "":
        return "provider upstream request failed"

    status_match = _HTTP_STATUS_PATTERN.search(message)
    if status_match is not None:
        status_code = int(status_match.group(1))
        return f"provider upstream request failed with HTTP {status_code}{redirect_note(status_code)}"

    lowered = message.lower()
    if "invalid json" in lowered:
        return "provider upstream payload was invalid JSON"
    if "did not include assistant output text" in lowered:
        return "provider upstream payload was missing assistant output text"
    if "unsupported model provider" in lowered:
        return "provider runtime request was invalid"
    if "unsupported provider auth_mode" in lowered:
        return "provider runtime authentication settings were invalid"
    return "provider upstream request failed"


def _parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = hostname
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass

    # Handle IPv4 integer/hex/octal/shorthand forms accepted by common socket parsers
    # (for example: 2130706433, 0x7f000001, 017700000001, 127.1).
    try:
        packed_v4 = socket.inet_aton(candidate)
    except OSError:
        return None
    try:
        return ipaddress.IPv4Address(packed_v4)
    except ipaddress.AddressValueError:
        return None


def _resolve_hostname_ips(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        addrinfo = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ProviderURLValidationError("base_url host could not be resolved") from exc

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        if family == socket.AF_INET:
            candidate = sockaddr[0]
        elif family == socket.AF_INET6:
            candidate = sockaddr[0]
        else:
            continue
        if not isinstance(candidate, str):
            continue
        resolved_ip = _parse_ip_literal(candidate)
        if resolved_ip is not None and resolved_ip not in resolved:
            resolved.append(resolved_ip)

    if not resolved:
        raise ProviderURLValidationError("base_url host could not be resolved")
    return tuple(resolved)


def _is_disallowed_ip(ip_address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip_address.is_multicast:
        return True
    return not ip_address.is_global
