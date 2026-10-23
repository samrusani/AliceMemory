"""What text of a source chunk may be sent to an embeddings endpoint, and in what form.

Memories have always been embedded when ``ALICE_EMBEDDINGS_BASE_URL`` and
``ALICE_EMBEDDINGS_MODEL`` are set. Source chunks are not, and setting those two
variables does not start it. Sending the text of a note to an endpoint is its own
opt-in, ``ALICE_SOURCE_EMBEDDINGS=on``, and this module holds the rules that
opt-in carries. It reads the environment and the text and decides. It opens no
connection and makes no request, so every rule here is testable on its own.

The rules, in the order a chunk meets them:

* Nothing is sent unless ``ALICE_SOURCE_EMBEDDINGS`` is ``on``.
* An endpoint that is not on this machine also needs
  ``ALICE_SOURCE_EMBEDDINGS_REMOTE=on``. With it, only a chunk of a source whose
  sensitivity is at or under ``ALICE_SOURCE_EMBEDDINGS_MAX_SENSITIVITY`` (default
  ``internal``) is sent. On this machine the ceiling does not apply.
* A chunk is never sent when the credential check flags it (the floor and the
  commit door's check, on the whole chunk and on the whole text that would be
  sent), when it holds only links and labels, when it is shorter than a person
  could read as a note, or when its source is deleted, replaced or scrubbed.

The text that is sent is the source title, the headings above the chunk, and the
chunk, behind the model's document prefix. Only that text changes. The chunk text
that is stored and returned never does.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SOURCE_EMBEDDINGS_ENV = "ALICE_SOURCE_EMBEDDINGS"
SOURCE_EMBEDDINGS_REMOTE_ENV = "ALICE_SOURCE_EMBEDDINGS_REMOTE"
SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV = "ALICE_SOURCE_EMBEDDINGS_MAX_SENSITIVITY"
SOURCE_EMBEDDINGS_MAX_VECTORS_ENV = "ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS"

DEFAULT_MAX_SENSITIVITY = "internal"
#: The most chunk vectors one vault keeps. 250,000 vectors of 768 floats are about 750 MB on disk.
DEFAULT_MAX_VECTORS = 250_000
MIN_MAX_VECTORS = 1
MAX_MAX_VECTORS = 100_000_000

#: A chunk with fewer readable characters than this is not sent (a heading and a word).
MIN_READABLE_CHARS = 32

#: Version of the vector row's own signature (which columns decide that a vector is current).
CHUNK_SIGNATURE_VERSION = 1
#: Version of the header rule: the title, the heading path and the way they join the chunk.
HEADER_RULE_VERSION = 1
#: Longest title and heading path that go in the header, in characters.
HEADER_TITLE_MAX_CHARS = 200
HEADER_PATH_MAX_CHARS = 300

# Why a chunk is not sent. These strings are what receipts and the doctor print.
SKIP_SOURCE_RETIRED = "source_retired"
SKIP_CREDENTIAL = "credential"
SKIP_SENSITIVITY_CEILING = "sensitivity_ceiling"
SKIP_LINK_ONLY = "link_only"
SKIP_TOO_SHORT = "too_short"
SKIP_REASONS: tuple[str, ...] = (
    SKIP_SOURCE_RETIRED,
    SKIP_CREDENTIAL,
    SKIP_SENSITIVITY_CEILING,
    SKIP_LINK_ONLY,
    SKIP_TOO_SHORT,
)

# Why nothing was sent at all.
OFF_SOURCE_EMBEDDINGS = "source_embeddings_off"
OFF_NO_PROVIDER = "no_embedding_provider"
OFF_REMOTE_NOT_ALLOWED = "remote_endpoint_not_allowed"
OFF_DRY_RUN = "dry_run"

#: How sensitive a source is, for the ceiling. ``unknown`` is held at ``private``: a label that
#: nobody set cannot be shown to be at or under ``internal``. (Other reads give ``unknown`` the
#: rank of ``internal``; this table is the one place the egress rule differs.)
EGRESS_SENSITIVITY_RANK: Mapping[str, int] = {
    "public": 1,
    "internal": 2,
    "unknown": 3,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}

_WARNED: set[tuple[str, str]] = set()


def _warn_once(name: str, value: str, message: str) -> None:
    if (name, value) in _WARNED:
        return
    _WARNED.add((name, value))
    logger.warning(message)


def _is_on(raw: str | None) -> bool:
    return raw is not None and raw.strip().casefold() == "on"


@dataclass(frozen=True, slots=True)
class SourceEmbeddingSettings:
    """The four settings of the source embedding opt-in, read from the environment."""

    enabled: bool
    remote_allowed: bool
    max_sensitivity: str
    max_vectors: int

    @property
    def max_sensitivity_rank(self) -> int:
        return EGRESS_SENSITIVITY_RANK[self.max_sensitivity]


def source_embedding_settings(environ: Mapping[str, str] | None = None) -> SourceEmbeddingSettings:
    """The settings the environment gives. Only the value ``on`` turns an opt-in on.

    A ceiling that names no sensitivity level is ignored with a warning and the default,
    ``internal``, is used. A vault cap that is not a whole number from 1 to 100,000,000 is
    ignored the same way.
    """

    source = os.environ if environ is None else environ
    ceiling_raw = source.get(SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV)
    ceiling = DEFAULT_MAX_SENSITIVITY
    if ceiling_raw is not None and ceiling_raw.strip():
        candidate = ceiling_raw.strip().casefold()
        if candidate in EGRESS_SENSITIVITY_RANK:
            ceiling = candidate
        else:
            _warn_once(
                SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
                ceiling_raw,
                f"{SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV} names no sensitivity level; using {DEFAULT_MAX_SENSITIVITY}",
            )
    cap_raw = source.get(SOURCE_EMBEDDINGS_MAX_VECTORS_ENV)
    cap = DEFAULT_MAX_VECTORS
    if cap_raw is not None and cap_raw.strip():
        try:
            parsed = int(cap_raw.strip())
        except ValueError:
            parsed = 0
        if MIN_MAX_VECTORS <= parsed <= MAX_MAX_VECTORS:
            cap = parsed
        else:
            _warn_once(
                SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
                cap_raw,
                f"{SOURCE_EMBEDDINGS_MAX_VECTORS_ENV} must be a whole number from {MIN_MAX_VECTORS} to "
                f"{MAX_MAX_VECTORS}; using {DEFAULT_MAX_VECTORS}",
            )
    return SourceEmbeddingSettings(
        enabled=_is_on(source.get(SOURCE_EMBEDDINGS_ENV)),
        remote_allowed=_is_on(source.get(SOURCE_EMBEDDINGS_REMOTE_ENV)),
        max_sensitivity=ceiling,
        max_vectors=cap,
    )


def source_embeddings_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return source_embedding_settings(environ).enabled


# -- the endpoint --------------------------------------------------------------------------------


def _parse_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = hostname.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    try:
        # The IPv4 spellings a socket accepts: 2130706433, 0x7f000001, 017700000001, 127.1.
        return ipaddress.IPv4Address(socket.inet_aton(candidate))
    except (OSError, ipaddress.AddressValueError):
        return None


def _carried_by_proxy(base_url: str) -> bool:
    """True when an HTTP proxy of the environment would carry a request to ``base_url``."""

    try:
        parts = urlsplit(base_url)
        proxies = urllib.request.getproxies()
        if parts.scheme not in proxies:
            return False
        hostport = parts.netloc.rsplit("@", 1)[-1]
        return not urllib.request.proxy_bypass(hostport)
    except Exception:  # noqa: BLE001 - an unreadable proxy setting is read as a proxy
        return True


def endpoint_is_loopback(base_url: str) -> bool:
    """True only when requests to ``base_url`` stay on this machine.

    The host is ``localhost``, a name under ``.localhost``, or an address in 127.0.0.0/8 or
    ``::1``, in any spelling a socket reads, and no proxy of the environment carries the
    request (a proxy would take the text off the machine). A host name is not resolved: any
    other name is a remote endpoint, whatever it points at today.
    """

    try:
        parts = urlsplit(base_url.strip())
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not hostname:
        return False
    host = hostname.casefold().rstrip(".")
    loopback = host == "localhost" or host.endswith(".localhost")
    if not loopback:
        address = _parse_ip(host)
        if address is None:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        loopback = (mapped or address).is_loopback
    return loopback and not _carried_by_proxy(base_url)


@dataclass(frozen=True, slots=True)
class EgressPosture:
    """Whether source text may be sent to the configured endpoint, and under which ceiling.

    ``reason`` is set when nothing may be sent at all (one of the ``OFF_*`` strings).
    ``ceiling_rank`` is ``None`` when the endpoint is on this machine and the sensitivity
    ceiling does not apply.
    """

    may_send: bool
    reason: str | None
    remote: bool
    ceiling_rank: int | None
    ceiling: str | None


def egress_posture(
    settings: SourceEmbeddingSettings,
    *,
    provider_configured: bool,
    base_url: str = "",
) -> EgressPosture:
    if not settings.enabled:
        return EgressPosture(False, OFF_SOURCE_EMBEDDINGS, False, None, None)
    if not provider_configured:
        return EgressPosture(False, OFF_NO_PROVIDER, False, None, None)
    if endpoint_is_loopback(base_url):
        return EgressPosture(True, None, False, None, None)
    if not settings.remote_allowed:
        return EgressPosture(False, OFF_REMOTE_NOT_ALLOWED, True, None, None)
    return EgressPosture(True, None, True, settings.max_sensitivity_rank, settings.max_sensitivity)


# -- the model's prefixes -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrefixRule:
    """The text a model family expects before a document and before a query."""

    family: str
    document: str
    query: str
    version: int


#: The prefix rules, one for each model family that needs them. A model that is not here is
#: sent no prefix. A family's ``version`` goes up when its rule changes, so only the vectors
#: of that family are made again. nomic-embed-text is trained with these two prefixes.
PREFIX_RULES: tuple[PrefixRule, ...] = (
    PrefixRule(family="nomic-embed-text", document="search_document: ", query="search_query: ", version=1),
)


def model_family(model: str) -> str:
    """The family part of a model name: lower case, without a registry path or a tag."""

    name = model.strip().casefold().rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def prefix_rule(model: str) -> PrefixRule | None:
    family = model_family(model)
    for rule in PREFIX_RULES:
        if family.startswith(rule.family):
            return rule
    return None


def document_prefix(model: str) -> str:
    rule = prefix_rule(model)
    return rule.document if rule is not None else ""


def query_prefix(model: str) -> str:
    rule = prefix_rule(model)
    return rule.query if rule is not None else ""


def prefix_version(model: str) -> int:
    """The version of the model's rule, or 0 when the model is sent no prefix."""

    rule = prefix_rule(model)
    return rule.version if rule is not None else 0


# -- the signature of a vector --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkSignature:
    """What makes a stored vector the vector of today's configuration."""

    provider: str
    model: str
    endpoint: str
    signature_version: int
    header_version: int
    prefix_version: int


def chunk_signature(provider: object) -> ChunkSignature:
    """The signature a vector made by ``provider`` carries. ``endpoint`` is the memory path's fingerprint."""

    from alicebot_api.vnext_embeddings import endpoint_fingerprint

    model = str(getattr(provider, "model", ""))
    return ChunkSignature(
        provider=str(getattr(provider, "provider", "")),
        model=model,
        endpoint=endpoint_fingerprint(getattr(provider, "base_url", "")),
        signature_version=CHUNK_SIGNATURE_VERSION,
        header_version=HEADER_RULE_VERSION,
        prefix_version=prefix_version(model),
    )


def signature_of_row(row: Mapping[str, object]) -> ChunkSignature:
    return ChunkSignature(
        provider=str(row.get("provider") or ""),
        model=str(row.get("model") or ""),
        endpoint=str(row.get("endpoint") or ""),
        signature_version=int(row.get("signature_version") or 0),  # type: ignore[call-overload]
        header_version=int(row.get("header_version") or 0),  # type: ignore[call-overload]
        prefix_version=int(row.get("prefix_version") or 0),  # type: ignore[call-overload]
    )


# -- the text that is sent ------------------------------------------------------------------------

_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FENCE_LINE = re.compile(r"^\s{0,3}(```|~~~)")


def _headings(text: str) -> list[tuple[int, str]]:
    """The markdown headings of a chunk, as (level, title), skipping fenced code."""

    found: list[tuple[int, str]] = []
    fence: str | None = None
    for line in text.split("\n"):
        fenced = _FENCE_LINE.match(line)
        if fenced:
            marker = fenced.group(1)
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is not None:
            continue
        match = _HEADING_LINE.match(line.rstrip())
        if match and match.group(2).strip():
            found.append((len(match.group(1)), " ".join(match.group(2).split())))
    return found


def chunk_heading_paths(chunk_texts: Sequence[str]) -> list[str]:
    """For each chunk of a source, in order, the headings above it joined with `` > ``.

    "Above" is the headings of the chunks before it that still apply. A chunk that opens with a
    heading is under that heading's parents only (its own heading is in its text), so a chunk
    that opens ``## Plan`` under ``# Roadmap`` has the path ``Roadmap``, and a chunk that
    continues a section has the path of the section it continues. A source with no headings has
    an empty path for every chunk.
    """

    stack: list[tuple[int, str]] = []
    paths: list[str] = []
    for text in chunk_texts:
        own = _headings(text)
        first_line = next((line for line in text.split("\n") if line.strip()), "")
        opens_with_heading = bool(own) and _HEADING_LINE.match(first_line.rstrip()) is not None
        if opens_with_heading:
            level = own[0][0]
            above = [entry for entry in stack if entry[0] < level]
        else:
            above = list(stack)
        paths.append(" > ".join(title for _level, title in above))
        for level, title in own:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
    return paths


def chunk_embedding_body(*, title: str | None, heading_path: str, chunk_text: str) -> str:
    """The text of one chunk with its header, before the model's prefix.

    The source title, then the headings above the chunk, each on its own line, then a blank line, then the
    chunk. A part that is empty is left out. This is the text the credential check reads.
    """

    header: list[str] = []
    if isinstance(title, str) and title.strip():
        header.append(" ".join(title.split())[:HEADER_TITLE_MAX_CHARS])
    if heading_path.strip():
        header.append(heading_path[:HEADER_PATH_MAX_CHARS])
    return ("\n".join(header) + "\n\n" + chunk_text) if header else chunk_text


def chunk_embedding_text(
    *,
    title: str | None,
    heading_path: str,
    chunk_text: str,
    model: str,
) -> str:
    """The exact text sent to the endpoint for one chunk: the model's document prefix, then the body."""

    return document_prefix(model) + chunk_embedding_body(title=title, heading_path=heading_path, chunk_text=chunk_text)


def text_sha256(sent_text: str) -> str:
    return sha256(sent_text.encode("utf-8")).hexdigest()


# -- which chunks are sent ------------------------------------------------------------------------


def readable_chars(text: str) -> int:
    """Characters in the lines of a chunk that carry content: not blank, not a link list, not a label."""

    from alicebot_api.vnext_retrieval import _LABEL_ONLY_LINE, _LINK_ONLY_LINE

    total = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or _LINK_ONLY_LINE.match(line) or _LABEL_ONLY_LINE.match(stripped):
            continue
        total += len(stripped)
    return total


def _flagged(*texts: str) -> bool:
    """True when the floor or the commit door flags any of the texts, alone or joined."""

    from alicebot_api.credential_floor import credential_verdict
    from alicebot_api.legacy_credential_check import commit_door_fields_verdict

    if credential_verdict(*texts) is not None:
        return True
    return commit_door_fields_verdict(*texts) is not None


def skip_reason(
    *,
    chunk_text: str,
    sent_text_without_prefix: str,
    source_sensitivity: str | None,
    source_live: bool,
    posture: EgressPosture,
) -> str | None:
    """Why this chunk is not sent, or ``None`` when it may be.

    One reason, the first that applies in the order of ``SKIP_REASONS``. The credential check reads
    the whole chunk, the text that would be sent (title and headings included), and the two joined.
    """

    from alicebot_api.vnext_retrieval import _has_readable_line

    if not source_live:
        return SKIP_SOURCE_RETIRED
    if _flagged(chunk_text, sent_text_without_prefix):
        return SKIP_CREDENTIAL
    if posture.ceiling_rank is not None:
        rank = EGRESS_SENSITIVITY_RANK.get(str(source_sensitivity or "unknown"), EGRESS_SENSITIVITY_RANK["unknown"])
        if rank > posture.ceiling_rank:
            return SKIP_SENSITIVITY_CEILING
    if not _has_readable_line(chunk_text):
        return SKIP_LINK_ONLY
    if readable_chars(chunk_text) < MIN_READABLE_CHARS:
        return SKIP_TOO_SHORT
    return None


__all__ = [
    "CHUNK_SIGNATURE_VERSION",
    "DEFAULT_MAX_SENSITIVITY",
    "DEFAULT_MAX_VECTORS",
    "EGRESS_SENSITIVITY_RANK",
    "HEADER_RULE_VERSION",
    "MIN_READABLE_CHARS",
    "OFF_DRY_RUN",
    "OFF_NO_PROVIDER",
    "OFF_REMOTE_NOT_ALLOWED",
    "OFF_SOURCE_EMBEDDINGS",
    "PREFIX_RULES",
    "SKIP_CREDENTIAL",
    "SKIP_LINK_ONLY",
    "SKIP_REASONS",
    "SKIP_SENSITIVITY_CEILING",
    "SKIP_SOURCE_RETIRED",
    "SKIP_TOO_SHORT",
    "SOURCE_EMBEDDINGS_ENV",
    "SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV",
    "SOURCE_EMBEDDINGS_MAX_VECTORS_ENV",
    "SOURCE_EMBEDDINGS_REMOTE_ENV",
    "ChunkSignature",
    "EgressPosture",
    "PrefixRule",
    "SourceEmbeddingSettings",
    "chunk_embedding_body",
    "chunk_embedding_text",
    "chunk_heading_paths",
    "chunk_signature",
    "document_prefix",
    "egress_posture",
    "endpoint_is_loopback",
    "model_family",
    "prefix_rule",
    "prefix_version",
    "query_prefix",
    "readable_chars",
    "signature_of_row",
    "skip_reason",
    "source_embedding_settings",
    "source_embeddings_enabled",
    "text_sha256",
]
