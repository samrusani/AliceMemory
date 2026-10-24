"""What text of a source chunk may be sent to an embeddings endpoint, and in what form.

Memories have always been embedded when ``ALICE_EMBEDDINGS_BASE_URL`` and
``ALICE_EMBEDDINGS_MODEL`` are set. Source chunks are not, and setting those two
variables does not start it. Sending the text of a note to an endpoint is its own
opt-in, ``ALICE_SOURCE_EMBEDDINGS=on``, and this module holds the rules that
opt-in carries. It reads the environment and the text and decides. It opens no
connection and makes no request, so every rule here is testable on its own.

The rules, in the order a chunk meets them:

* Nothing is sent unless ``ALICE_SOURCE_EMBEDDINGS`` is ``on``.
* Source text goes only to an endpoint on this machine: ``localhost``, a name under ``.localhost``, an
  IPv4 address in 127.0.0.0/8 written as four decimal numbers, or ``::1``, with no proxy of the
  environment that would carry the request. Any other host name, including one that resolves to
  loopback, any other address and every IPv4-mapped IPv6 form is not on this machine. For such an
  endpoint nothing is sent and every chunk stays pending. Remote endpoints are not supported for
  source embeddings; memory embeddings are not governed by this module.
* A chunk is not sent when the credential check flags it (the floor and the
  commit door's check, on the whole chunk and on the whole text that would be
  sent), when it holds only links and labels, when it is shorter than a person
  could read as a note, or when its source is deleted, replaced or scrubbed.
* A label is not a rule of this module. The sensitivity, domain and project of a
  source do not decide whether its chunks are sent to an endpoint on this machine,
  so a change of label is not an egress event.
* The rules are applied to the rows as they are when the text is chosen, and again
  just before each batch is sent. A change committed after that last read has
  read the source, whether the request has begun or not, is not seen by the batch.
* The connection is the last gate: once it is made, and before anything is
  written, the address of the connected socket must be loopback (``provider_http``),
  so a name under ``.localhost`` that resolves elsewhere gets no byte.

The text that is sent is the source title, the headings above the chunk, and the
chunk, behind the model's document prefix. Only that text changes. The chunk text
that is stored and returned never does.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SOURCE_EMBEDDINGS_ENV = "ALICE_SOURCE_EMBEDDINGS"
SOURCE_EMBEDDINGS_MAX_VECTORS_ENV = "ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS"

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

#: What a receipt says about deleted chunk vectors. Deleting a vector is a logical deletion, as deleting chunk text is.
CHUNK_VECTOR_RETAINED_DATA = (
    "Chunk vectors are deleted with the source and zeroed where the database frees them, but deleting is logical: "
    "the write-ahead log, unused space in the vault file and earlier backups can keep vector bytes until the vault "
    "is checkpointed and rebuilt with VACUUM, and a vector can be inverted to approximate the words it was made from."
)

# Why a chunk is not sent. These strings are what receipts and the doctor print.
SKIP_SOURCE_RETIRED = "source_retired"
SKIP_CREDENTIAL = "credential"
SKIP_LINK_ONLY = "link_only"
SKIP_TOO_SHORT = "too_short"
SKIP_REASONS: tuple[str, ...] = (
    SKIP_SOURCE_RETIRED,
    SKIP_CREDENTIAL,
    SKIP_LINK_ONLY,
    SKIP_TOO_SHORT,
)

# Why nothing was sent at all.
OFF_SOURCE_EMBEDDINGS = "source_embeddings_off"
OFF_NO_PROVIDER = "no_embedding_provider"
OFF_ENDPOINT_NOT_LOCAL = "endpoint_not_on_this_machine"
OFF_DRY_RUN = "dry_run"

#: What a receipt, the doctor and the vector stage say when the endpoint is not on this machine.
ENDPOINT_NOT_LOCAL_MESSAGE = "source embeddings need an endpoint on this machine"

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
    """The two settings of the source embedding opt-in, read from the environment."""

    enabled: bool
    max_vectors: int


def source_embedding_settings(environ: Mapping[str, str] | None = None) -> SourceEmbeddingSettings:
    """The settings the environment gives. Only the value ``on`` turns the opt-in on.

    A vault cap that is not a whole number from 1 to 100,000,000 is ignored with a warning and the
    default is used.
    """

    source = os.environ if environ is None else environ
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
    return SourceEmbeddingSettings(enabled=_is_on(source.get(SOURCE_EMBEDDINGS_ENV)), max_vectors=cap)


def source_embeddings_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return source_embedding_settings(environ).enabled


# -- the endpoint --------------------------------------------------------------------------------


def _is_loopback_literal(host: str) -> bool:
    """True for an IPv4 address in 127.0.0.0/8 written as four decimal numbers, and for ``::1``.

    Only those spellings count. An address written another way (``127.1``, ``2130706433``, ``0x7f000001``), a zone
    id, and an IPv4-mapped IPv6 form such as ``::ffff:127.0.0.1`` are not literals of this rule and are not on this
    machine as far as it is concerned: they are read by a person as loopback and by a resolver as whatever it makes
    of them, and a rule that sends note text should not depend on the difference.
    """

    if "%" in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return address == ipaddress.IPv6Address("::1")
    return address.is_loopback and host.count(".") == 3 and all(part.isdigit() for part in host.split("."))


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

    The host is ``localhost``, a name under ``.localhost``, an IPv4 address in 127.0.0.0/8 written as four decimal
    numbers, or ``::1``, and no proxy of the environment would carry the request (a proxy would take the text off the
    machine). A host name is not resolved: any other name is not on this machine, whatever it points at today, and
    neither is any other spelling of an address, an IPv4-mapped IPv6 form included.
    """

    try:
        parts = urlsplit(base_url.strip())
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not hostname:
        return False
    host = hostname.casefold()
    loopback = host == "localhost" or host.endswith(".localhost") or _is_loopback_literal(host)
    return loopback and not _carried_by_proxy(base_url)


@dataclass(frozen=True, slots=True)
class EgressPosture:
    """Whether source text may be sent to the configured endpoint.

    ``reason`` is set when nothing may be sent at all (one of the ``OFF_*`` strings). ``local`` is whether the
    endpoint is on this machine.
    """

    may_send: bool
    reason: str | None
    local: bool = False


def egress_posture(
    settings: SourceEmbeddingSettings,
    *,
    provider_configured: bool,
    base_url: str = "",
) -> EgressPosture:
    """May source text be sent now? Only with the opt-in on, a provider, and an endpoint on this machine."""

    if not settings.enabled:
        return EgressPosture(False, OFF_SOURCE_EMBEDDINGS)
    if not provider_configured:
        return EgressPosture(False, OFF_NO_PROVIDER)
    if not endpoint_is_loopback(base_url):
        return EgressPosture(False, OFF_ENDPOINT_NOT_LOCAL)
    return EgressPosture(True, None, local=True)


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
    source_live: bool,
) -> str | None:
    """Why this chunk is not sent, or ``None`` when it may be.

    One reason, the first that applies in the order of ``SKIP_REASONS``. The credential check reads
    the whole chunk, the text that would be sent (title and headings included), and the two joined.
    The labels of the source are not an input: they do not decide what goes to an endpoint on this machine.
    """

    from alicebot_api.vnext_retrieval import _has_readable_line

    if not source_live:
        return SKIP_SOURCE_RETIRED
    if _flagged(chunk_text, sent_text_without_prefix):
        return SKIP_CREDENTIAL
    if not _has_readable_line(chunk_text):
        return SKIP_LINK_ONLY
    if readable_chars(chunk_text) < MIN_READABLE_CHARS:
        return SKIP_TOO_SHORT
    return None


__all__ = [
    "CHUNK_SIGNATURE_VERSION",
    "CHUNK_VECTOR_RETAINED_DATA",
    "ENDPOINT_NOT_LOCAL_MESSAGE",
    "DEFAULT_MAX_VECTORS",
    "HEADER_RULE_VERSION",
    "MIN_READABLE_CHARS",
    "OFF_DRY_RUN",
    "OFF_NO_PROVIDER",
    "OFF_ENDPOINT_NOT_LOCAL",
    "OFF_SOURCE_EMBEDDINGS",
    "PREFIX_RULES",
    "SKIP_CREDENTIAL",
    "SKIP_LINK_ONLY",
    "SKIP_REASONS",
    "SKIP_SOURCE_RETIRED",
    "SKIP_TOO_SHORT",
    "SOURCE_EMBEDDINGS_ENV",
    "SOURCE_EMBEDDINGS_MAX_VECTORS_ENV",
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
