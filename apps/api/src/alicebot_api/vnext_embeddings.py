from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
import json
import logging
import math
import os
import re
from hashlib import sha256
from typing import Iterator, Mapping, NotRequired, Protocol, Sequence, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from alicebot_api.credential_floor import credential_verdict
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_repositories import JsonObject


logger = logging.getLogger(__name__)


EMBEDDING_VECTOR_DIMENSIONS = 1536
EMBEDDINGS_BASE_URL_ENV = "ALICE_EMBEDDINGS_BASE_URL"
EMBEDDINGS_MODEL_ENV = "ALICE_EMBEDDINGS_MODEL"
EMBEDDINGS_API_KEY_ENV = "ALICE_EMBEDDINGS_API_KEY"
EMBEDDINGS_MAX_INPUT_CHARS_ENV = "ALICE_EMBEDDINGS_MAX_INPUT_CHARS"
DEFAULT_EMBEDDINGS_TIMEOUT_SECONDS = 30
MAX_EMBEDDINGS_BATCH_SIZE = 128
# The input cap: the most characters of one memory's text that are sent to the
# embeddings endpoint. A longer text is cut to this many characters before it
# is sent, and the vector stored for it is labelled as made from a cut text.
#
# Why 8,000. The widely used hosted and local models take about 8,000 tokens
# of input (OpenAI's text-embedding-3 models take 8,191, and nomic-embed-text
# and bge-m3 served by Ollama take 8,192). 8,000 characters fits such a model
# even for text that needs a whole token for every character, as dense CJK text
# can. Ordinary English runs about four characters per token, so 8,000
# characters is about 2,000 tokens and an ordinary memory is never cut. Text of
# rare symbols or emoji can take several tokens a character and may still pass
# the model's limit.
#
# What it costs. A memory of 8,001 to 20,000 characters (a commit accepts up to
# 20,000) is embedded from its first 8,000 characters on a model that could
# take more. Full-text and graph search still read all of it. A vault of long
# memories on a large-window model can raise the cap.
#
# A model with a smaller window, such as mxbai-embed-large or Cohere embed v3
# at 512 tokens, or all-minilm at 256, needs ALICE_EMBEDDINGS_MAX_INPUT_CHARS
# set lower: the endpoint either rejects the text or cuts it silently, and
# Alice cannot tell the second from a whole vector.
DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS = 8000
MIN_EMBEDDINGS_MAX_INPUT_CHARS = 256
MAX_EMBEDDINGS_MAX_INPUT_CHARS = 1_000_000
EMBEDDING_SIGNATURE_METADATA_KEY = "_alice_embedding"
# Present in the signature only when the text was cut, and then equal to the
# cap that cut it. A signature without it labels a vector of the whole text.
EMBEDDING_TRUNCATED_SIGNATURE_KEY = "truncated_to_chars"
# Most characters of an endpoint's error message that Alice keeps, and the
# most bytes of an error body it reads to find that message.
PROVIDER_REASON_MAX_CHARS = 300
PROVIDER_ERROR_BODY_READ_BYTES = 4096
PROVIDER_MESSAGE_WITHHELD = "provider message withheld: it carried credential material"
# An endpoint that answers one of these statuses is refusing the content of the
# request, so a smaller request may be accepted. Any other failure (a refused
# connection, a timeout, 401, 404, 429, 5xx) is not about one text and is not
# split.
INPUT_REJECTION_HTTP_STATUSES = frozenset({400, 413, 422})
# Sent once when a batch is rejected, before the batch is split. If the endpoint
# rejects even this, the failure is not about any one text and the batch is not
# split, so a wrong model name costs two requests, not hundreds.
_ISOLATION_PROBE_TEXT = "alice embedding probe"
# The listing of failed memory ids in reindex output.
EMBEDDING_FAILED_IDS_LISTED = 100
EMBEDDING_FAILURE_REASONS_LISTED = 5
LISTED_ID_MAX_CHARS = 128
LISTED_ID_WITHHELD = "(id withheld)"
NO_PROVIDER_REASON = "the embedding failed and the provider gave no reason"
# Version 2 adds the endpoint fingerprint to the signature. Bumping invalidates
# pre-endpoint (v1) vectors so they are re-embedded rather than silently pooled
# across endpoints that share provider/model labels.
EMBEDDING_SIGNATURE_VERSION = 2
EMBEDDING_PREPARATION_ERROR_CODE = "embedding_preparation_failed"
EMBEDDING_PREPARATION_ERROR_MESSAGE = "Memory embedding preparation failed"
EMBEDDING_PERSISTENCE_ERROR_CODE = "embedding_persistence_failed"
EMBEDDING_PERSISTENCE_ERROR_MESSAGE = "Memory embedding persistence failed"
EMBEDDING_STALE_ERROR_CODE = "embedding_stale_discarded"
EMBEDDING_STALE_ERROR_MESSAGE = "Memory changed before its embedding was stored"
STALE_REASON = "the memory changed while its vector was being prepared, so the vector was discarded"
NOT_STORED_REASON = "the vector could not be stored"


class VNextEmbeddingConfigurationError(ValueError):
    """Raised when embedding input or configuration is invalid."""


class VNextEmbeddingProviderError(RuntimeError):
    """Raised when the embeddings endpoint request fails.

    ``status`` is the HTTP status when the endpoint answered with an error, and
    ``None`` for a transport or payload failure. The message is safe to print:
    an endpoint's own text in it is bounded and has passed the credential check
    (see ``sanitize_provider_message``).
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    base_url: str

    def embed_text(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


class SignedMemoryEmbeddingUpdate(TypedDict):
    """Complete storage contract for one v2 content-derived vector."""

    memory_id: str
    vector: list[float]
    provider: str
    model: str
    endpoint: str
    content_sha256: str
    signature_version: int
    # Only when the text was cut to the provider's input cap; then the cap.
    truncated_to_chars: NotRequired[int]


@dataclass(frozen=True, slots=True)
class DeferredMemoryEmbedding:
    """Immutable id/text snapshot safe to carry across a commit boundary."""

    memory_id: str
    title: str | None
    canonical_text: str | None
    summary: str | None

    @classmethod
    def from_memory(cls, memory: Mapping[str, object]) -> "DeferredMemoryEmbedding":
        memory_id = str(memory.get("id") or "").strip()
        if not memory_id:
            raise VNextEmbeddingConfigurationError(
                "deferred memory embedding inputs require a non-empty id"
            )

        def text_field(name: str) -> str | None:
            value = memory.get(name)
            return value if isinstance(value, str) else None

        return cls(
            memory_id=memory_id,
            title=text_field("title"),
            canonical_text=text_field("canonical_text"),
            summary=text_field("summary"),
        )

    def to_memory_record(self) -> Mapping[str, object]:
        return {
            "id": self.memory_id,
            "title": self.title,
            "canonical_text": self.canonical_text,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class PreparedMemoryEmbedding:
    """Validated vector write prepared without an open database transaction."""

    memory_id: str
    vector: tuple[float, ...]
    provider: str
    model: str
    endpoint: str
    content_sha256: str
    signature_version: int
    truncated_to_chars: int | None = None

    def to_update(self) -> SignedMemoryEmbeddingUpdate:
        update: SignedMemoryEmbeddingUpdate = {
            "memory_id": self.memory_id,
            "vector": list(self.vector),
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "content_sha256": self.content_sha256,
            "signature_version": self.signature_version,
        }
        if self.truncated_to_chars is not None:
            update["truncated_to_chars"] = self.truncated_to_chars
        return update


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingFailure:
    """One memory that did not get a vector.

    ``error_code`` and ``error_message`` are fixed strings and are what the
    event log records. ``reason`` and ``provider_status`` carry what the
    endpoint said, for the person running reindex. They are kept out of
    ``repr`` and out of every stored record: the reason is bounded and passed
    the credential check, but it is the endpoint's text, not Alice's.
    """

    memory_id: str
    error_code: str
    error_message: str
    provider_status: int | None = field(default=None, repr=False, compare=False)
    reason: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingPreparation:
    """Provider result ready for a separate, short persistence transaction."""

    prepared: tuple[PreparedMemoryEmbedding, ...]
    failures: tuple[MemoryEmbeddingFailure, ...]
    provider: str | None
    model: str | None


def _canonical_endpoint(base_url: str) -> str:
    """Normalize URL identity without changing case-sensitive route data."""
    raw = base_url.strip()
    if raw == "":
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        # Invalid URLs are rejected when the provider request is built. Keep a
        # stable fingerprint here without inventing URL semantics.
        return raw
    if hostname is None:
        return raw.rstrip("/")

    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    canonical_host = hostname.casefold()
    if ":" in canonical_host and not canonical_host.startswith("["):
        canonical_host = f"[{canonical_host}]"
    scheme = parsed.scheme.casefold()
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    port_suffix = "" if port is None or default_port else f":{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(
        SplitResult(
            scheme,
            f"{userinfo}{canonical_host}{port_suffix}",
            path,
            parsed.query,
            parsed.fragment,
        )
    )


def endpoint_fingerprint(base_url: object) -> str:
    """Stable, non-secret identity for an embedding endpoint.

    Two endpoints that share a provider/model label but serve different
    coordinate spaces (e.g. distinct hosts) must not have their vectors pooled.
    The base_url is hashed rather than stored verbatim so internal endpoint
    URLs never leak into memory metadata. Providers without a base_url yield an
    empty fingerprint (there is no endpoint to distinguish).
    """
    if not isinstance(base_url, str):
        return ""
    normalized = _canonical_endpoint(base_url)
    if normalized == "":
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()[:16]


def pad_embedding_vector(
    vector: Sequence[float],
    *,
    dimensions: int = EMBEDDING_VECTOR_DIMENSIONS,
) -> list[float]:
    """Zero-pad an embedding to the storage width.

    Zero-padding preserves cosine similarity between vectors from the same
    model, so smaller local models can share the 1536-dim column.
    """
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise VNextEmbeddingConfigurationError("embedding vectors must contain only numbers")
        normalized_value = float(value)
        if not math.isfinite(normalized_value):
            raise VNextEmbeddingConfigurationError("embedding vectors must contain only finite numbers")
        values.append(normalized_value)
    if not values:
        raise VNextEmbeddingConfigurationError("embedding vectors must not be empty")
    if len(values) > dimensions:
        raise VNextEmbeddingConfigurationError(
            f"embedding has {len(values)} dimensions but the storage column holds {dimensions}; "
            "configure an embedding model that emits at most "
            f"{dimensions} dimensions"
        )
    if len(values) < dimensions:
        values.extend(0.0 for _ in range(dimensions - len(values)))
    return values


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def sanitize_provider_message(raw: object, *, secrets: Sequence[str] = ()) -> str | None:
    """A bounded, credential-safe copy of an endpoint's error text, or ``None``.

    Order matters. Every string in ``secrets`` (the configured API key) is
    replaced first, so a key the endpoint echoes back as it was sent never
    survives, whatever its shape. A copy the endpoint alters (a space inserted
    into it, say) is not matched; only the credential check below can catch
    that, and only if the altered copy still looks like a credential. The whole
    text then goes through the credential check before it is cut, so a
    credential that straddles the cut is still seen. A text the check flags is
    replaced by a fixed sentence, never printed in part. Last, control
    characters and runs of whitespace become single spaces, and the result is
    cut to ``PROVIDER_REASON_MAX_CHARS`` characters.
    """

    if not isinstance(raw, str):
        return None
    text = raw
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    if credential_verdict(text) is not None:
        return PROVIDER_MESSAGE_WITHHELD
    text = " ".join(_CONTROL_CHARACTERS.sub(" ", text).split())
    if len(text) > PROVIDER_REASON_MAX_CHARS:
        text = text[: PROVIDER_REASON_MAX_CHARS - 3].rstrip() + "..."
    return text or None


def _provider_error_text(body: str) -> str:
    """The message inside an endpoint's error body, or the body itself.

    Reads the shapes the OpenAI-compatible servers use: ``{"error": {"message":
    ...}}`` (OpenAI, vLLM, LM Studio), ``{"error": "..."}`` (Ollama) and
    ``{"message": ...}`` or ``{"detail": "..."}``.
    """

    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parsed, dict):
        return body
    error = parsed.get("error")
    candidates = (
        error.get("message") if isinstance(error, dict) else error,
        parsed.get("message"),
        parsed.get("detail"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return body


def _read_error_body(exc: HTTPError) -> str:
    """Up to ``PROVIDER_ERROR_BODY_READ_BYTES`` of an HTTP error body, or ``""``."""

    try:
        raw = exc.read(PROVIDER_ERROR_BODY_READ_BYTES)
    except Exception:  # noqa: BLE001 - the body is optional detail on an error already being raised
        return ""
    finally:
        with suppress(Exception):  # closing is best effort
            exc.close()
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    return bytes(raw).decode("utf-8", errors="replace")


_WARNED_INPUT_CAP_VALUES: set[str] = set()


def resolve_embeddings_max_input_chars(raw: str | None) -> int:
    """The input cap for an environment value: the default when unset or invalid.

    A value that is not a whole number from ``MIN_EMBEDDINGS_MAX_INPUT_CHARS``
    to ``MAX_EMBEDDINGS_MAX_INPUT_CHARS`` is ignored with a warning (once per
    value), because a typo in a tuning knob should not stop memory recall from
    starting.
    """

    if raw is None or raw.strip() == "":
        return DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if not MIN_EMBEDDINGS_MAX_INPUT_CHARS <= value <= MAX_EMBEDDINGS_MAX_INPUT_CHARS:
        if raw not in _WARNED_INPUT_CAP_VALUES:
            _WARNED_INPUT_CAP_VALUES.add(raw)
            logger.warning(
                "%s must be a whole number from %d to %d; using %d",
                EMBEDDINGS_MAX_INPUT_CHARS_ENV,
                MIN_EMBEDDINGS_MAX_INPUT_CHARS,
                MAX_EMBEDDINGS_MAX_INPUT_CHARS,
                DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS,
            )
        return DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS
    return value


def embedding_input_cap(provider: object) -> int | None:
    """The provider's input cap in characters, or ``None`` when it declares none.

    A provider that declares no cap (a test double, a future backend) is sent
    whole texts and signs whole-text vectors, as before the cap existed.
    """

    cap = getattr(provider, "max_input_chars", None)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        return None
    return cap


def embedding_text_is_cut(text: str, max_chars: int | None) -> bool:
    return max_chars is not None and len(text) > max_chars


def cut_embedding_text(text: str, max_chars: int | None) -> str:
    """``text`` cut to its first ``max_chars`` characters, or as given when it fits."""

    if max_chars is None or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    return head.rstrip() or head


class OpenAICompatibleEmbeddingProvider:
    """Embeddings client for any OpenAI-compatible ``/embeddings`` endpoint.

    Works against OpenAI, Ollama's ``/v1``, LM Studio, and vLLM. Uses only the
    standard library, matching the vNext model-intelligence HTTP style.

    Every text is cut to ``max_input_chars`` characters before it is sent. The
    signature helpers read the same attribute, so a vector made from a cut text
    is labelled as such.
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = DEFAULT_EMBEDDINGS_TIMEOUT_SECONDS,
        max_input_chars: int = DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        normalized_model = model.strip()
        if normalized_base_url == "":
            raise VNextEmbeddingConfigurationError("embeddings base_url must not be empty")
        if normalized_model == "":
            raise VNextEmbeddingConfigurationError("embeddings model must not be empty")
        if (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or not MIN_EMBEDDINGS_MAX_INPUT_CHARS <= max_input_chars <= MAX_EMBEDDINGS_MAX_INPUT_CHARS
        ):
            raise VNextEmbeddingConfigurationError(
                f"embeddings max_input_chars must be a whole number from "
                f"{MIN_EMBEDDINGS_MAX_INPUT_CHARS} to {MAX_EMBEDDINGS_MAX_INPUT_CHARS}"
            )
        self.base_url = normalized_base_url
        self.model = normalized_model
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.timeout_seconds = timeout_seconds
        self.max_input_chars = max_input_chars

    def _safe_reason(self, raw: object) -> str | None:
        secrets = (self.api_key,) if self.api_key is not None else ()
        return sanitize_provider_message(raw, secrets=secrets)

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > MAX_EMBEDDINGS_BATCH_SIZE:
            raise VNextEmbeddingConfigurationError(
                f"embedding batches are limited to {MAX_EMBEDDINGS_BATCH_SIZE} texts per request"
            )
        for text in texts:
            if not isinstance(text, str) or text.strip() == "":
                raise VNextEmbeddingConfigurationError("embedding input texts must be non-empty strings")
        payload: JsonObject = {
            "model": self.model,
            "input": [cut_embedding_text(text, self.max_input_chars) for text in texts],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read())
        except HTTPError as exc:
            reason = self._safe_reason(_provider_error_text(_read_error_body(exc)))
            raise VNextEmbeddingProviderError(
                f"embeddings endpoint returned HTTP {exc.code}" + (f": {reason}" if reason else ""),
                status=exc.code,
            ) from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VNextEmbeddingProviderError(
                f"embeddings request failed: {self._safe_reason(str(exc)) or type(exc).__name__}"
            ) from exc
        return _extract_embeddings(response_payload, expected_count=len(texts))


def _extract_embeddings(payload: object, *, expected_count: int) -> list[list[float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise VNextEmbeddingProviderError("embeddings response did not include a data array")
    rows: list[tuple[int | None, list[float]]] = []
    index_presence: list[bool] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise VNextEmbeddingProviderError("embeddings response item did not include an embedding")
        has_index = "index" in item
        item_index = item.get("index") if has_index else None
        if has_index and (isinstance(item_index, bool) or not isinstance(item_index, int)):
            raise VNextEmbeddingProviderError(
                "embeddings response indices must be non-boolean integers"
            )
        index_presence.append(has_index)
        rows.append(
            (
                item_index,
                pad_embedding_vector(item["embedding"]),
            )
        )
    if len(rows) != expected_count:
        raise VNextEmbeddingProviderError(
            f"embeddings response returned {len(rows)} vectors for {expected_count} inputs"
        )
    indexed = [row_index for row_index, _vector in rows if row_index is not None]
    if any(index_presence) and not all(index_presence):
        raise VNextEmbeddingProviderError(
            "embeddings response must either index every vector or omit every index"
        )
    if indexed:
        expected_indices = list(range(expected_count))
        if sorted(indexed) != expected_indices:
            raise VNextEmbeddingProviderError(
                "embeddings response indices must be an exact 0-based permutation of the inputs"
            )
        rows.sort(key=lambda row: row[0] if row[0] is not None else -1)
    return [vector for _index, vector in rows]


def get_embedding_provider() -> OpenAICompatibleEmbeddingProvider | None:
    """Build the configured embedding provider, or ``None`` when unconfigured.

    Unconfigured means full-text-search-only retrieval; there is no fake or
    hash-based embedding fallback.
    """
    base_url = os.environ.get(EMBEDDINGS_BASE_URL_ENV, "").strip()
    model = os.environ.get(EMBEDDINGS_MODEL_ENV, "").strip()
    if base_url == "" or model == "":
        return None
    api_key = os.environ.get(EMBEDDINGS_API_KEY_ENV, "").strip() or None
    return OpenAICompatibleEmbeddingProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_input_chars=resolve_embeddings_max_input_chars(
            os.environ.get(EMBEDDINGS_MAX_INPUT_CHARS_ENV)
        ),
    )


def memory_embedding_text(memory: Mapping[str, object]) -> str:
    """The text embedded for a memory row: the same fields as ``search_tsv``."""
    parts: list[str] = []
    for key in ("title", "canonical_text", "summary"):
        value = memory.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(dict.fromkeys(parts))


def memory_embedding_content_sha256(memory: Mapping[str, object]) -> str:
    """Digest the exact normalized text used to derive a memory vector."""
    return sha256(memory_embedding_text(memory).encode("utf-8")).hexdigest()


def memory_embedding_signature_is_current(memory: Mapping[str, object]) -> bool:
    """Return whether persisted vector metadata matches the row's current text.

    Lifecycle hooks and database triggers normally clear stale vectors. This
    read-time check remains as a fail-closed guard for legacy adapters, restored
    snapshots, and direct SQL that can bypass those paths.

    The input-cap label (``truncated_to_chars``) is deliberately not compared
    here. A vector of the head of a text is a valid vector of the same model, so
    search keeps using it. Only reindex acts on the label, to make a vector
    again after the cap changes.
    """
    metadata = memory.get("metadata_json")
    if not isinstance(metadata, Mapping):
        return False
    signature = metadata.get(EMBEDDING_SIGNATURE_METADATA_KEY)
    if not isinstance(signature, Mapping):
        return False
    stored_digest = signature.get("content_sha256")
    return (
        signature.get("version") == EMBEDDING_SIGNATURE_VERSION
        and isinstance(signature.get("provider"), str)
        and bool(signature.get("provider"))
        and isinstance(signature.get("model"), str)
        and bool(signature.get("model"))
        and isinstance(signature.get("endpoint"), str)
        and isinstance(stored_digest, str)
        and stored_digest == memory_embedding_content_sha256(memory)
    )


def memory_embedding_signature(
    memory: Mapping[str, object],
    *,
    provider: EmbeddingProvider,
) -> JsonObject:
    """Compatibility metadata for a content-derived memory vector.

    ``content_sha256`` is the digest of the whole text, so an edit to the text
    is always seen. When the provider's input cap cut the text, the signature
    also carries ``truncated_to_chars``, the cap that cut it: the vector is of
    the head of the text and says so, and a later change of the cap makes the
    row stale so reindex makes it again. A text that fits has no such key, so a
    change of the cap re-embeds only the rows whose embedded text changes.
    """
    signature: JsonObject = {
        "version": EMBEDDING_SIGNATURE_VERSION,
        "provider": provider.provider,
        "model": provider.model,
        "endpoint": endpoint_fingerprint(getattr(provider, "base_url", "")),
        "content_sha256": memory_embedding_content_sha256(memory),
    }
    cap = embedding_input_cap(provider)
    if embedding_text_is_cut(memory_embedding_text(memory), cap):
        signature[EMBEDDING_TRUNCATED_SIGNATURE_KEY] = cap
    return signature


def signed_memory_embedding_update(
    memory: Mapping[str, object],
    vector: Sequence[float],
    *,
    provider: EmbeddingProvider,
) -> SignedMemoryEmbeddingUpdate:
    """Build the one complete v2 vector-write contract used by Alice.

    Centralizing this prevents a caller from persisting a vector without the
    provider/model/endpoint/content identity required for safe retrieval.
    The vector is validated and normalized to the storage width before it can
    cross a store boundary.
    """
    try:
        memory_id = str(memory["id"])
    except KeyError as exc:
        raise VNextEmbeddingConfigurationError("memory embedding writes require an id") from exc
    if memory_id.strip() == "":
        raise VNextEmbeddingConfigurationError("memory embedding writes require a non-empty id")
    signature = memory_embedding_signature(memory, provider=provider)
    signature_version = signature["version"]
    if not isinstance(signature_version, int):
        raise VNextEmbeddingConfigurationError("memory embedding signature version must be an integer")
    update: SignedMemoryEmbeddingUpdate = {
        "memory_id": memory_id,
        "vector": pad_embedding_vector(vector),
        "provider": str(signature["provider"]),
        "model": str(signature["model"]),
        "endpoint": str(signature["endpoint"]),
        "content_sha256": str(signature["content_sha256"]),
        "signature_version": signature_version,
    }
    truncated_to_chars = signature.get(EMBEDDING_TRUNCATED_SIGNATURE_KEY)
    if isinstance(truncated_to_chars, int) and not isinstance(truncated_to_chars, bool):
        update["truncated_to_chars"] = truncated_to_chars
    return update


def attach_memory_embedding(
    store: object,
    memory: Mapping[str, object],
    *,
    provider: EmbeddingProvider | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> bool:
    """Best-effort embed-on-write for a memory row.

    Embedding failure never blocks the memory write: failures are logged to
    the event log and the ``embedding_vector`` column stays NULL for the
    ``alicebot vnext memories backfill-embeddings`` pass.
    """
    return (
        attach_memory_embeddings(
            store,
            [memory],
            provider=provider,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
        )
        == 1
    )


def _log_embedding_failure(
    store: object,
    memory_id: str,
    *,
    error_code: str,
    error_message: str,
    provider: str | None,
    model: str | None,
    actor_type: str,
    actor_id: str | None,
    trace_id: str | None,
    provider_status: int | None = None,
) -> None:
    """Best-effort diagnostic for an already best-effort vector write.

    The record holds fixed strings and, when the endpoint answered with an
    error status, that number. It never holds the endpoint's message: that is
    shown to the person running reindex and is not stored.
    """
    payload: JsonObject = {
        "error_code": error_code,
        "error_message": error_message,
        "provider": provider,
        "model": model,
    }
    if provider_status is not None:
        payload["provider_status"] = provider_status
    try:
        with _embedding_write_savepoint(store):
            append_event(
                store,  # type: ignore[arg-type]
                event_type="memory.embedding_failed",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="memory",
                target_id=memory_id,
                trace_id=trace_id,
                payload=payload,
            )
    except Exception:
        # Event persistence is diagnostic only. A second store failure must not
        # turn an optional embedding write into a failed memory operation.
        return


@contextmanager
def _embedding_write_savepoint(store: object) -> Iterator[None]:
    """Isolate one optional vector/event write from its caller transaction."""
    conn = getattr(store, "conn", None)
    transaction = getattr(conn, "transaction", None)
    if callable(transaction):
        with transaction():
            yield
        return
    execute = getattr(conn, "execute", None)
    if callable(execute):
        name = "alice_embedding_write"
        execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            execute(f"ROLLBACK TO SAVEPOINT {name}")
            execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            execute(f"RELEASE SAVEPOINT {name}")
        return
    yield


@dataclass(frozen=True, slots=True)
class EmbeddingTextFailure:
    """Why one text of a batch got no vector.

    ``isolated`` is true when the text was sent alone and refused, so the
    reason is about that text. It is false when the failure was not about one
    text (the batch was not split, or a half failed for a reason that is not
    about its content), so the reason may be about its neighbours too.
    """

    reason: str | None
    status: int | None
    isolated: bool


def embedding_failure_reason(exc: BaseException) -> str | None:
    """A bounded, credential-safe reason for an embedding failure, or ``None``.

    Only the two embedding error types carry text Alice wrote or sanitized. Any
    other exception (a driver error, a bug) is reported without its text.
    """

    if isinstance(exc, (VNextEmbeddingProviderError, VNextEmbeddingConfigurationError)):
        return sanitize_provider_message(str(exc))
    return None


def _text_failure(exc: BaseException, *, isolated: bool) -> EmbeddingTextFailure:
    status = exc.status if isinstance(exc, VNextEmbeddingProviderError) else None
    return EmbeddingTextFailure(embedding_failure_reason(exc), status, isolated)


def _refuses_input(exc: BaseException) -> bool:
    return isinstance(exc, VNextEmbeddingProviderError) and exc.status in INPUT_REJECTION_HTTP_STATUSES


def _embed_checked(provider: EmbeddingProvider, texts: Sequence[str]) -> list[list[float]]:
    vectors = provider.embed_batch(texts)
    if len(vectors) != len(texts):
        raise VNextEmbeddingProviderError(
            f"embedding provider returned {len(vectors)} vectors for {len(texts)} inputs"
        )
    return vectors


def embed_batch_isolating(
    provider: EmbeddingProvider,
    texts: Sequence[str],
) -> list[list[float] | EmbeddingTextFailure]:
    """Embed a batch so that one refused text cannot sink its neighbours.

    The result lines up with ``texts``: a vector, or the failure for that text.
    When the endpoint refuses the whole batch with a status that is about the
    request content (``INPUT_REJECTION_HTTP_STATUSES``), the batch is split in
    half and each half is retried, down to single texts, so the texts that are
    accepted get vectors and the refused ones are named with the endpoint's
    reason.

    Two rules keep a failure that is not about one text from turning into a
    flood of requests. Only content statuses are split: a refused connection, a
    timeout, 401, 404, 429 and 5xx fail the batch with one request. And a probe
    of one trivial text must succeed before any split, so a wrong model name
    that every request meets with a 400 costs two requests, not hundreds. When
    the probe passes, the failure is about the texts, and splitting a batch of
    ``n`` texts takes fewer than ``2n`` further requests.
    """

    batch = list(texts)
    if not batch:
        return []
    try:
        return list(_embed_checked(provider, batch))
    except Exception as exc:  # noqa: BLE001 - any provider failure is a failed batch
        first_error = exc
    if len(batch) == 1:
        return [_text_failure(first_error, isolated=True)]
    if not _refuses_input(first_error):
        return [_text_failure(first_error, isolated=False)] * len(batch)
    try:
        _embed_checked(provider, [_ISOLATION_PROBE_TEXT])
    except Exception:  # noqa: BLE001 - the endpoint is failing for every input
        return [_text_failure(first_error, isolated=False)] * len(batch)

    results: list[list[float] | EmbeddingTextFailure | None] = [None] * len(batch)

    def isolate(low: int, high: int) -> None:
        """Retry the halves of a segment that was refused as a whole."""

        middle = (low + high) // 2
        for start, stop in ((low, middle), (middle, high)):
            try:
                vectors = _embed_checked(provider, batch[start:stop])
            except Exception as exc:  # noqa: BLE001 - recorded per text below
                if stop - start == 1:
                    results[start] = _text_failure(exc, isolated=True)
                elif _refuses_input(exc):
                    isolate(start, stop)
                else:
                    results[start:stop] = [_text_failure(exc, isolated=False)] * (stop - start)
            else:
                results[start:stop] = list(vectors)

    isolate(0, len(batch))
    return [
        result if result is not None else _text_failure(first_error, isolated=False)
        for result in results
    ]


def prepare_memory_embeddings(
    inputs: Sequence[DeferredMemoryEmbedding],
    *,
    provider: EmbeddingProvider | None = None,
    log_failures: bool = True,
) -> MemoryEmbeddingPreparation:
    """Call the provider in batches without touching a database connection.

    A refused text is isolated from its batch (``embed_batch_isolating``), so
    its failure record names that memory and its neighbours still get vectors.
    Failures are written to the process log unless ``log_failures`` is false,
    which a command that reports every failure itself passes so its stderr stays
    the stable error records it documents.
    """
    resolved_provider = provider if provider is not None else get_embedding_provider()
    if resolved_provider is None:
        return MemoryEmbeddingPreparation((), (), None, None)
    embeddable = [
        (item, text)
        for item in inputs
        if (text := memory_embedding_text(item.to_memory_record())) != ""
    ]
    prepared: list[PreparedMemoryEmbedding] = []
    failures: list[MemoryEmbeddingFailure] = []
    for batch_start in range(0, len(embeddable), MAX_EMBEDDINGS_BATCH_SIZE):
        batch = embeddable[batch_start : batch_start + MAX_EMBEDDINGS_BATCH_SIZE]
        outcomes = embed_batch_isolating(resolved_provider, [text for _item, text in batch])
        failed_in_batch = [
            outcome for outcome in outcomes if isinstance(outcome, EmbeddingTextFailure)
        ]
        if failed_in_batch and log_failures:
            logger.warning(
                "memory embedding preparation failed for %d of %d texts error_code=%s reason=%s",
                len(failed_in_batch),
                len(batch),
                EMBEDDING_PREPARATION_ERROR_CODE,
                failed_in_batch[0].reason or NO_PROVIDER_REASON,
            )

        for (item, _text), outcome in zip(batch, outcomes, strict=True):
            if isinstance(outcome, EmbeddingTextFailure):
                failures.append(
                    MemoryEmbeddingFailure(
                        item.memory_id,
                        EMBEDDING_PREPARATION_ERROR_CODE,
                        EMBEDDING_PREPARATION_ERROR_MESSAGE,
                        provider_status=outcome.status,
                        reason=outcome.reason,
                    )
                )
                continue
            try:
                update = signed_memory_embedding_update(
                    item.to_memory_record(), outcome, provider=resolved_provider
                )
                prepared.append(
                    PreparedMemoryEmbedding(
                        memory_id=update["memory_id"],
                        vector=tuple(update["vector"]),
                        provider=update["provider"],
                        model=update["model"],
                        endpoint=update["endpoint"],
                        content_sha256=update["content_sha256"],
                        signature_version=update["signature_version"],
                        truncated_to_chars=update.get("truncated_to_chars"),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one malformed vector must not fail the batch
                if log_failures:
                    logger.exception(
                        "memory embedding signature preparation failed memory_id=%s error_code=%s",
                        item.memory_id,
                        EMBEDDING_PREPARATION_ERROR_CODE,
                    )
                failures.append(
                    MemoryEmbeddingFailure(
                        item.memory_id,
                        EMBEDDING_PREPARATION_ERROR_CODE,
                        EMBEDDING_PREPARATION_ERROR_MESSAGE,
                        reason=embedding_failure_reason(exc),
                    )
                )
    return MemoryEmbeddingPreparation(
        tuple(prepared),
        tuple(failures),
        resolved_provider.provider,
        resolved_provider.model,
    )


@dataclass(frozen=True, slots=True)
class EmbeddingPersistOutcome:
    """What a prepare-and-persist pass did, by memory id.

    ``failed`` names every memory that did not get a vector this pass: one the
    endpoint refused, one whose vector could not be stored, and one that was
    edited while its vector was being prepared (the stale vector is discarded
    and a later pass makes it again). ``truncated`` counts stored vectors made
    from a text the input cap cut.
    """

    attached_ids: tuple[str, ...]
    failed: tuple[MemoryEmbeddingFailure, ...]
    truncated: int = 0

    @property
    def attached(self) -> int:
        return len(self.attached_ids)


def persist_prepared_memory_embeddings_outcome(
    store: object,
    preparation: MemoryEmbeddingPreparation,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> EmbeddingPersistOutcome:
    """Best-effort persistence half of the two-phase embedding contract."""
    update_memory_embedding = getattr(store, "update_memory_embedding", None)
    if not callable(update_memory_embedding):
        return EmbeddingPersistOutcome(
            (),
            (
                *preparation.failures,
                *(
                    MemoryEmbeddingFailure(
                        prepared.memory_id,
                        EMBEDDING_PERSISTENCE_ERROR_CODE,
                        EMBEDDING_PERSISTENCE_ERROR_MESSAGE,
                        reason=NOT_STORED_REASON,
                    )
                    for prepared in preparation.prepared
                ),
            ),
        )
    for failure in preparation.failures:
        _log_embedding_failure(
            store,
            failure.memory_id,
            error_code=failure.error_code,
            error_message=failure.error_message,
            provider=preparation.provider,
            model=preparation.model,
            provider_status=failure.provider_status,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
        )
    attached_ids: list[str] = []
    failed: list[MemoryEmbeddingFailure] = list(preparation.failures)
    truncated = 0
    for prepared in preparation.prepared:
        try:
            with _embedding_write_savepoint(store):
                updated = update_memory_embedding(**prepared.to_update())
            # Bundled stores use the signed content digest as an optimistic
            # compare-and-set token.  ``None`` means the memory changed after
            # provider preparation, so the stale vector must be discarded.
            if updated is not None:
                attached_ids.append(prepared.memory_id)
                if prepared.truncated_to_chars is not None:
                    truncated += 1
            else:
                failed.append(
                    MemoryEmbeddingFailure(
                        prepared.memory_id,
                        EMBEDDING_STALE_ERROR_CODE,
                        EMBEDDING_STALE_ERROR_MESSAGE,
                        reason=STALE_REASON,
                    )
                )
        except Exception:
            logger.exception(
                "memory embedding persistence failed memory_id=%s error_code=%s",
                prepared.memory_id,
                EMBEDDING_PERSISTENCE_ERROR_CODE,
            )
            _log_embedding_failure(
                store,
                prepared.memory_id,
                error_code=EMBEDDING_PERSISTENCE_ERROR_CODE,
                error_message=EMBEDDING_PERSISTENCE_ERROR_MESSAGE,
                provider=prepared.provider,
                model=prepared.model,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
            )
            failed.append(
                MemoryEmbeddingFailure(
                    prepared.memory_id,
                    EMBEDDING_PERSISTENCE_ERROR_CODE,
                    EMBEDDING_PERSISTENCE_ERROR_MESSAGE,
                    reason=NOT_STORED_REASON,
                )
            )
    return EmbeddingPersistOutcome(tuple(attached_ids), tuple(failed), truncated)


def persist_prepared_memory_embeddings(
    store: object,
    preparation: MemoryEmbeddingPreparation,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Best-effort persistence half of the two-phase embedding contract."""
    return persist_prepared_memory_embeddings_outcome(
        store,
        preparation,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    ).attached


def persist_deferred_memory_embeddings_outcome(
    inputs: Sequence[DeferredMemoryEmbedding],
    *,
    store_context: Callable[[], AbstractContextManager[object]],
    provider: EmbeddingProvider | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> EmbeddingPersistOutcome:
    """Prepare vectors without a connection and persist after the primary commit.

    Embeddings are an optional derived index. Once the authoritative memory
    transaction has committed, failure to acquire the follow-up connection,
    write a vector, or commit that follow-up transaction must not turn the
    successful memory operation into an error. Store-level failures are logged
    through ``memory.embedding_failed`` when possible; connection/commit
    failures are logged through the process logger because no usable store may
    remain. The outcome names every memory that did not get a vector.
    """

    if not inputs:
        return EmbeddingPersistOutcome((), ())
    try:
        preparation = prepare_memory_embeddings(inputs, provider=provider)
    except Exception as exc:
        logger.warning(
            "best-effort memory embedding preparation failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return EmbeddingPersistOutcome(
            (),
            tuple(
                MemoryEmbeddingFailure(
                    item.memory_id,
                    EMBEDDING_PREPARATION_ERROR_CODE,
                    EMBEDDING_PREPARATION_ERROR_MESSAGE,
                    reason=embedding_failure_reason(exc),
                )
                for item in inputs
            ),
        )
    if not preparation.prepared and not preparation.failures:
        return EmbeddingPersistOutcome((), ())
    try:
        with store_context() as store:
            return persist_prepared_memory_embeddings_outcome(
                store,
                preparation,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
            )
    except Exception as exc:
        logger.warning(
            "best-effort memory embedding persistence failed after the primary commit: %s: %s",
            type(exc).__name__,
            exc,
        )
        return EmbeddingPersistOutcome(
            (),
            (
                *preparation.failures,
                *(
                    MemoryEmbeddingFailure(
                        prepared.memory_id,
                        EMBEDDING_PERSISTENCE_ERROR_CODE,
                        EMBEDDING_PERSISTENCE_ERROR_MESSAGE,
                        reason=NOT_STORED_REASON,
                    )
                    for prepared in preparation.prepared
                ),
            ),
        )


def persist_deferred_memory_embeddings_best_effort(
    inputs: Sequence[DeferredMemoryEmbedding],
    *,
    store_context: Callable[[], AbstractContextManager[object]],
    provider: EmbeddingProvider | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """The count of vectors stored by ``persist_deferred_memory_embeddings_outcome``."""

    return persist_deferred_memory_embeddings_outcome(
        inputs,
        store_context=store_context,
        provider=provider,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    ).attached


def listable_memory_id(memory_id: object) -> str:
    """A memory id as it may be printed in a failure listing.

    The policy of the import receipts: an id that is very long, holds a
    control character (a newline would start a fake output line) or is itself
    credential-shaped is withheld, so a listing of failures never prints a
    finding. The caller escapes what is returned (JSON output does).
    """

    text = str(memory_id)
    if len(text) > LISTED_ID_MAX_CHARS or not text.isprintable() or credential_verdict(text) is not None:
        return LISTED_ID_WITHHELD
    return text


def summarize_embedding_failures(
    failures: Sequence[MemoryEmbeddingFailure],
) -> dict[str, object]:
    """The failed-id listing and reason tally for reindex output.

    At most ``EMBEDDING_FAILED_IDS_LISTED`` ids are listed, with the rest
    counted in ``failed_ids_omitted``, and at most
    ``EMBEDDING_FAILURE_REASONS_LISTED`` distinct reasons are tallied, most
    common first. Reasons are the bounded, credential-checked text the
    failures already carry.
    """

    reasons = Counter(failure.reason or NO_PROVIDER_REASON for failure in failures)
    tallied = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    return {
        "failed_ids": [
            listable_memory_id(failure.memory_id)
            for failure in failures[:EMBEDDING_FAILED_IDS_LISTED]
        ],
        "failed_ids_omitted": max(0, len(failures) - EMBEDDING_FAILED_IDS_LISTED),
        "failure_reasons": [
            {"reason": reason, "count": count}
            for reason, count in tallied[:EMBEDDING_FAILURE_REASONS_LISTED]
        ],
    }


def attach_memory_embeddings(
    store: object,
    memories: Sequence[Mapping[str, object]],
    *,
    provider: EmbeddingProvider | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Best-effort batched embed-on-write for memory rows.

    Provider calls are bounded to ``MAX_EMBEDDINGS_BATCH_SIZE``. A failed
    provider batch or individual store write is logged for the affected rows
    and never blocks the already-completed memory writes.
    """
    update_memory_embedding = getattr(store, "update_memory_embedding", None)
    if not callable(update_memory_embedding):
        return 0
    try:
        inputs = tuple(DeferredMemoryEmbedding.from_memory(memory) for memory in memories)
    except VNextEmbeddingConfigurationError:
        inputs = tuple(
            DeferredMemoryEmbedding.from_memory(memory)
            for memory in memories
            if str(memory.get("id") or "").strip()
        )
    preparation = prepare_memory_embeddings(inputs, provider=provider)
    return persist_prepared_memory_embeddings(
        store,
        preparation,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )


__all__ = [
    "DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS",
    "DEFAULT_EMBEDDINGS_TIMEOUT_SECONDS",
    "EMBEDDING_VECTOR_DIMENSIONS",
    "EMBEDDINGS_API_KEY_ENV",
    "EMBEDDINGS_BASE_URL_ENV",
    "EMBEDDINGS_MAX_INPUT_CHARS_ENV",
    "EMBEDDINGS_MODEL_ENV",
    "EMBEDDING_FAILED_IDS_LISTED",
    "EMBEDDING_SIGNATURE_METADATA_KEY",
    "EMBEDDING_SIGNATURE_VERSION",
    "EMBEDDING_PREPARATION_ERROR_CODE",
    "EMBEDDING_PREPARATION_ERROR_MESSAGE",
    "EMBEDDING_PERSISTENCE_ERROR_CODE",
    "EMBEDDING_PERSISTENCE_ERROR_MESSAGE",
    "EMBEDDING_STALE_ERROR_CODE",
    "EMBEDDING_STALE_ERROR_MESSAGE",
    "EMBEDDING_TRUNCATED_SIGNATURE_KEY",
    "INPUT_REJECTION_HTTP_STATUSES",
    "LISTED_ID_WITHHELD",
    "MAX_EMBEDDINGS_MAX_INPUT_CHARS",
    "MIN_EMBEDDINGS_MAX_INPUT_CHARS",
    "PROVIDER_MESSAGE_WITHHELD",
    "PROVIDER_REASON_MAX_CHARS",
    "DeferredMemoryEmbedding",
    "EmbeddingPersistOutcome",
    "EmbeddingProvider",
    "EmbeddingTextFailure",
    "MemoryEmbeddingFailure",
    "MemoryEmbeddingPreparation",
    "PreparedMemoryEmbedding",
    "SignedMemoryEmbeddingUpdate",
    "cut_embedding_text",
    "embed_batch_isolating",
    "embedding_failure_reason",
    "embedding_input_cap",
    "embedding_text_is_cut",
    "endpoint_fingerprint",
    "listable_memory_id",
    "MAX_EMBEDDINGS_BATCH_SIZE",
    "OpenAICompatibleEmbeddingProvider",
    "VNextEmbeddingConfigurationError",
    "VNextEmbeddingProviderError",
    "attach_memory_embedding",
    "attach_memory_embeddings",
    "get_embedding_provider",
    "memory_embedding_content_sha256",
    "memory_embedding_signature_is_current",
    "memory_embedding_text",
    "memory_embedding_signature",
    "pad_embedding_vector",
    "persist_deferred_memory_embeddings_outcome",
    "persist_prepared_memory_embeddings",
    "persist_prepared_memory_embeddings_outcome",
    "persist_deferred_memory_embeddings_best_effort",
    "prepare_memory_embeddings",
    "resolve_embeddings_max_input_chars",
    "sanitize_provider_message",
    "signed_memory_embedding_update",
    "summarize_embedding_failures",
]
