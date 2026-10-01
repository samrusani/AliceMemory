"""Disclosed rerank stage between fusion and the budget packer (Context API v2).

The retrieval pipeline's rankers (FTS, vector, graph, temporal) fuse with
reciprocal rank fusion; this module adds the standard precision stage on
top: provider-side LISTWISE RELEVANCE SCORING of the fused head, reordering
candidates before the slot/budget spend. Design rules, in order:

* **Dormant unless configured.** ``get_reranker_provider()`` returns
  ``None`` without ``ALICE_RERANKER_BASE_URL`` + ``ALICE_RERANKER_MODEL``
  (the ``vnext_embeddings`` env pattern), and the integration block in
  ``vnext_retrieval`` never calls into this module then: zero provider
  calls, fused order stands, packs byte-identical to the fusion-only path,
  no reranker trace stage.
* **Reorders, never shrinks.** The stage reorders the fused candidate pool;
  the same number of selection slots is filled afterwards (the caller's
  ``max_items`` / section limits and the token-budget packer still decide
  what survives). Policy-excluded candidates (domain/sensitivity) are never
  re-admitted regardless of score.
* **Fail-open.** Any provider failure (HTTP error, timeout, malformed
  scores) keeps the fused order and records the failure in the stage
  record; retrieval never breaks because a reranker endpoint is down.
* **Honest and generic.** The scoring prompt is a frozen module constant
  (``RERANK_PROMPT_TEMPLATE``, sha-pinned in tests so drift is visible)
  containing only generic relevance-scoring language — no query-type
  vocabulary, no benchmark vocabulary. The stage is disclosed in the
  retrieval trace with model, candidate counts, reorder flag, latency, and
  token usage.
* **Deterministic ties.** Equal provider scores fall through the retrieval
  service's content-stable cascade (``content_stable_tiebreak`` — imported,
  not duplicated) with the row id as the final total-order key, so a
  score-tied reorder is ingest-invariant.

The provider call itself is the only non-deterministic step, which is the
point of the stage: relevance judgment happens entirely provider-side
against any OpenAI-compatible ``/chat/completions`` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
import time
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request

from alicebot_api.provider_http import open_provider_url
from alicebot_api.provider_security import redirect_note
from alicebot_api.vnext_ranking import content_stable_tiebreak
from alicebot_api.vnext_repositories import JsonObject


logger = logging.getLogger(__name__)

RERANKER_BASE_URL_ENV = "ALICE_RERANKER_BASE_URL"
RERANKER_MODEL_ENV = "ALICE_RERANKER_MODEL"
RERANKER_API_KEY_ENV = "ALICE_RERANKER_API_KEY"
DEFAULT_RERANKER_TIMEOUT_SECONDS = 30
# Trace stage key; the record's "source" value.
RERANKER_STAGE = "reranker"
# How deep into the fused candidate pools the scoring prompt reaches.
# Bounded so one rerank call stays one modest completion request; the
# reorder happens within this head, the tail keeps its fused order.
RERANK_MEMORY_CANDIDATE_CAP = 48
RERANK_SOURCE_CANDIDATE_CAP = 24
# Per-candidate text budget in the prompt (whitespace-collapsed).
RERANK_TEXT_MAX_CHARS = 600
# Deterministic stand-in for rows with no usable text field.
RERANK_EMPTY_TEXT = "(no content)"
# Content fields offered to the provider, in precedence order. Mirrors the
# search_tsv / memory_embedding_text field set, plus the bare "text" key
# some source-shaped rows carry.
RERANK_TEXT_KEYS = ("title", "canonical_text", "summary", "text")

# Outcome statuses (stage-record values).
RERANK_STATUS_RERANKED = "reranked"
RERANK_STATUS_NOOP = "noop: fewer than two candidates"
RERANK_FAIL_OPEN_PREFIX = "fail_open: "
RERANK_STATUS_FAIL_OPEN_PROVIDER_ERROR = f"{RERANK_FAIL_OPEN_PREFIX}provider_error"

# The frozen listwise scoring prompt. HONESTY CONTRACT: generic relevance
# scoring only — no query-type routing, no benchmark vocabulary, no answer
# synthesis. Committed as a constant and sha-pinned in
# tests/unit/test_vnext_reranker.py so any drift is visible in review.
RERANK_PROMPT_TEMPLATE = (
    "Score each document's relevance to the query.\n"
    "\n"
    "Query: {query}\n"
    "\n"
    "Documents:\n"
    "{documents}\n"
    "\n"
    "Reply with only a JSON array of {count} integers, one per document in "
    "order, each from 0 (unrelated) to 100 (directly relevant). No other text."
)
RERANK_PROMPT_SHA256 = hashlib.sha256(RERANK_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


class VNextRerankerConfigurationError(ValueError):
    """Raised when reranker input or configuration is invalid."""


class VNextRerankerProviderError(RuntimeError):
    """Raised when the reranker endpoint request or response is unusable."""


@dataclass(frozen=True, slots=True)
class RerankCompletion:
    """One raw completion from a rerank provider, plus its token usage."""

    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RerankProvider(Protocol):
    provider: str
    model: str

    def complete(self, prompt: str) -> RerankCompletion: ...


class OpenAICompatibleRerankProvider:
    """Rerank scoring via any OpenAI-compatible ``/chat/completions`` endpoint.

    Works against OpenAI, Ollama's ``/v1``, LM Studio, and vLLM. Uses only
    the standard library, matching the ``vnext_embeddings`` HTTP style.
    ``temperature`` is pinned to 0 so a well-behaved endpoint scores as
    repeatably as it can; determinism is still not guaranteed provider-side,
    which is why the stage is disclosed in the trace.
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = DEFAULT_RERANKER_TIMEOUT_SECONDS,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        normalized_model = model.strip()
        if normalized_base_url == "":
            raise VNextRerankerConfigurationError("reranker base_url must not be empty")
        if normalized_model == "":
            raise VNextRerankerConfigurationError("reranker model must not be empty")
        self.base_url = normalized_base_url
        self.model = normalized_model
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.timeout_seconds = timeout_seconds

    def complete(self, prompt: str) -> RerankCompletion:
        if not isinstance(prompt, str) or prompt.strip() == "":
            raise VNextRerankerConfigurationError("rerank prompt must be a non-empty string")
        payload: JsonObject = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with open_provider_url(request, timeout=self.timeout_seconds, enforce_public_peer=False) as response:
                response_payload = json.loads(response.read())
        except HTTPError as exc:
            raise VNextRerankerProviderError(
                f"reranker endpoint returned HTTP {exc.code}{redirect_note(exc.code)}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise VNextRerankerProviderError(f"reranker request failed: {exc}") from exc
        return _extract_completion(response_payload)


def _extract_completion(payload: object) -> RerankCompletion:
    if not isinstance(payload, dict):
        raise VNextRerankerProviderError("reranker response was not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise VNextRerankerProviderError("reranker response did not include choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise VNextRerankerProviderError("reranker response did not include message content")
    usage = payload.get("usage")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    if isinstance(usage, dict):
        raw_prompt_tokens = usage.get("prompt_tokens")
        raw_completion_tokens = usage.get("completion_tokens")
        if isinstance(raw_prompt_tokens, int) and not isinstance(raw_prompt_tokens, bool):
            prompt_tokens = raw_prompt_tokens
        if isinstance(raw_completion_tokens, int) and not isinstance(raw_completion_tokens, bool):
            completion_tokens = raw_completion_tokens
    return RerankCompletion(
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def get_reranker_provider() -> OpenAICompatibleRerankProvider | None:
    """Build the configured rerank provider, or ``None`` when unconfigured.

    Unconfigured means the rerank stage is DORMANT: fused order stands,
    zero provider calls, no trace stage. There is no heuristic or offline
    rerank fallback — that would be fake intelligence.
    """
    base_url = os.environ.get(RERANKER_BASE_URL_ENV, "").strip()
    model = os.environ.get(RERANKER_MODEL_ENV, "").strip()
    if base_url == "" or model == "":
        return None
    api_key = os.environ.get(RERANKER_API_KEY_ENV, "").strip() or None
    return OpenAICompatibleRerankProvider(base_url=base_url, model=model, api_key=api_key)


def rerank_candidate_text(item: JsonObject) -> str:
    """Whitespace-collapsed, capped prompt text for one candidate row."""
    parts: list[str] = []
    for key in RERANK_TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    text = " ".join(" ".join(dict.fromkeys(parts)).split())
    if text == "":
        return RERANK_EMPTY_TEXT
    return text[:RERANK_TEXT_MAX_CHARS]


def build_rerank_prompt(query: str, texts: Sequence[str]) -> str:
    """Render the frozen prompt for one listwise scoring call."""
    if not texts:
        raise VNextRerankerConfigurationError("rerank prompt needs at least one document")
    documents = "\n".join(f"[{index}] {text}" for index, text in enumerate(texts, start=1))
    return RERANK_PROMPT_TEMPLATE.format(count=len(texts), query=query, documents=documents)


def parse_rerank_scores(content: str, *, expected_count: int) -> list[float]:
    """Extract the score array from a completion, tolerating code fences.

    The first ``[`` … last ``]`` span must parse as a JSON array of exactly
    ``expected_count`` numbers; anything else raises
    ``VNextRerankerProviderError`` (which the caller fails open on).
    """
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end <= start:
        raise VNextRerankerProviderError("reranker completion did not contain a JSON array")
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VNextRerankerProviderError(f"reranker score array did not parse: {exc}") from exc
    if not isinstance(parsed, list):
        raise VNextRerankerProviderError("reranker scores were not a JSON array")
    if len(parsed) != expected_count:
        raise VNextRerankerProviderError(
            f"reranker returned {len(parsed)} scores for {expected_count} documents"
        )
    scores: list[float] = []
    for value in parsed:
        # The prompt contract is deliberately narrower than merely
        # "numeric": each score must be a JSON integer in the inclusive
        # 0..100 range.  Python's JSON decoder otherwise accepts NaN and
        # Infinity, and floats/out-of-range values can corrupt ordering while
        # the trace incorrectly reports a successful rerank.
        if isinstance(value, bool) or not isinstance(value, int):
            raise VNextRerankerProviderError(
                "reranker scores must be integers between 0 and 100"
            )
        if value < 0 or value > 100:
            raise VNextRerankerProviderError(
                "reranker scores must be integers between 0 and 100"
            )
        scores.append(float(value))
    return scores


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """Result of one listwise rerank pass over a candidate item list.

    ``order`` always covers every input index exactly once: the scored head
    reordered by score (descending; equal scores fall through the
    content-stable cascade, id last), followed by the unscored tail in its
    original order. On fail-open and noop paths ``order`` is the identity
    and ``scores`` is ``None``.
    """

    order: tuple[int, ...]
    scores: tuple[float, ...] | None
    status: str
    reordered: bool
    candidates_scored: int
    model: str | None
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_stage_record(self) -> JsonObject:
        return {
            "status": self.status,
            "candidates_scored": self.candidates_scored,
            "reordered": self.reordered,
            "latency_ms": self.latency_ms,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            },
        }


def _identity_outcome(
    count: int,
    *,
    status: str,
    model: str | None,
    latency_ms: int = 0,
) -> RerankOutcome:
    return RerankOutcome(
        order=tuple(range(count)),
        scores=None,
        status=status,
        reordered=False,
        candidates_scored=0,
        model=model,
        latency_ms=latency_ms,
    )


def rerank(
    query: str,
    candidates: Sequence[JsonObject],
    *,
    provider: RerankProvider,
    max_candidates: int,
) -> RerankOutcome:
    """Score the head of ``candidates`` against ``query`` and reorder it.

    Cap semantics: only the first ``max_candidates`` items are sent to the
    provider (one listwise completion call); every later item keeps its
    position after the reordered head. Fewer than two scorable candidates
    is a noop — trivially ordered, ZERO provider calls. Any provider
    failure fails open to the identity order with the failure recorded in
    ``status`` (``fail_open: …``) — the caller keeps fused order.
    """
    if max_candidates < 1:
        raise VNextRerankerConfigurationError("rerank max_candidates must be at least 1")
    total = len(candidates)
    scored_count = min(total, max_candidates)
    if scored_count < 2:
        return _identity_outcome(total, status=RERANK_STATUS_NOOP, model=provider.model)
    head = list(candidates[:scored_count])
    texts = [rerank_candidate_text(item) for item in head]
    prompt = build_rerank_prompt(query, texts)
    started = time.monotonic()
    try:
        completion = provider.complete(prompt)
        scores = parse_rerank_scores(completion.content, expected_count=scored_count)
    except Exception as exc:  # noqa: BLE001 - fail-open is the stage contract
        latency_ms = int(round((time.monotonic() - started) * 1000))
        logger.warning(
            "Reranker failed open error_code=provider_error provider=%s model=%s",
            provider.provider,
            provider.model,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return _identity_outcome(
            total,
            status=RERANK_STATUS_FAIL_OPEN_PROVIDER_ERROR,
            model=provider.model,
            latency_ms=latency_ms,
        )
    latency_ms = int(round((time.monotonic() - started) * 1000))
    head_order = sorted(
        range(scored_count),
        key=lambda index: (
            -scores[index],
            *content_stable_tiebreak(head[index]),
            str(head[index].get("id")),
        ),
    )
    order = (*head_order, *range(scored_count, total))
    return RerankOutcome(
        order=order,
        scores=tuple(scores),
        status=RERANK_STATUS_RERANKED,
        reordered=order != tuple(range(total)),
        candidates_scored=scored_count,
        model=provider.model,
        latency_ms=latency_ms,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
    )


# Exclusion reasons a rerank pass may reorder across; mirrors the coverage
# module's convention. Policy-excluded candidates (domain/sensitivity) are
# never re-admitted by score.
_REORDERABLE_EXCLUSION_REASONS = (None, "trimmed_by_limit")


def rerank_fused_candidates(
    candidates: Sequence[Any],
    *,
    query: str,
    provider: RerankProvider,
    limit: int,
    max_candidates: int,
) -> tuple[list[Any], RerankOutcome]:
    """Rerank a fused ``RetrievalCandidate``-shaped list, preserving slot count.

    ``candidates`` are fused-order dataclass instances (fields
    ``item``/``rank``/``selected``/``exclusion_reason``; rebuilt generically
    via ``dataclasses.replace`` so this module needs no import from the
    retrieval service). The reorderable candidates' head (up to
    ``max_candidates``) is scored listwise; the reranked order then fills
    exactly ``min(limit, reorderable_count)`` selection slots — the same
    count fusion selected, so reranking can promote or demote but NEVER
    shrinks the pack. Policy-excluded candidates keep their exclusion
    reason, stay unselected, and re-rank after the pool.

    Returns ``(candidates, outcome)``. The input list is returned untouched
    — same objects, same ranks — whenever the pass cannot change anything:
    provider noop, fail-open, or a reranked order identical to fused order.
    """
    if limit < 1:
        raise VNextRerankerConfigurationError("rerank limit must be at least 1")
    reorderable = [
        candidate for candidate in candidates if candidate.exclusion_reason in _REORDERABLE_EXCLUSION_REASONS
    ]
    outcome = rerank(
        query,
        [candidate.item for candidate in reorderable],
        provider=provider,
        max_candidates=max_candidates,
    )
    if not outcome.reordered:
        return list(candidates), outcome
    policy_excluded = [
        candidate for candidate in candidates if candidate.exclusion_reason not in _REORDERABLE_EXCLUSION_REASONS
    ]
    reordered = [reorderable[index] for index in outcome.order]
    selected_slots = min(limit, len(reordered))
    rebuilt: list[Any] = []
    for position, candidate in enumerate([*reordered, *policy_excluded], start=1):
        is_reorderable = candidate.exclusion_reason in _REORDERABLE_EXCLUSION_REASONS
        selected = is_reorderable and position <= selected_slots
        if selected:
            exclusion_reason = None
        elif is_reorderable:
            exclusion_reason = "trimmed_by_limit"
        else:
            exclusion_reason = candidate.exclusion_reason
        rebuilt.append(replace(candidate, rank=position, selected=selected, exclusion_reason=exclusion_reason))
    return rebuilt, outcome


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def reranker_stage_record(
    *,
    provider: RerankProvider,
    memories: RerankOutcome,
    sources: RerankOutcome,
) -> JsonObject:
    """Honest trace record for a configured rerank stage (absent when dormant)."""
    return {
        "source": RERANKER_STAGE,
        "provider": provider.provider,
        "model": provider.model,
        "prompt_sha256": RERANK_PROMPT_SHA256,
        "candidates_scored": memories.candidates_scored + sources.candidates_scored,
        "reordered": memories.reordered or sources.reordered,
        "latency_ms": memories.latency_ms + sources.latency_ms,
        "usage": {
            "prompt_tokens": _sum_optional(memories.prompt_tokens, sources.prompt_tokens),
            "completion_tokens": _sum_optional(memories.completion_tokens, sources.completion_tokens),
        },
        "memories": memories.to_stage_record(),
        "sources": sources.to_stage_record(),
    }


def disabled_stage_record(*, provider: RerankProvider, status: str) -> JsonObject:
    """Trace record for a configured-but-skipped rerank stage (e.g. minimal depth)."""
    return {
        "source": RERANKER_STAGE,
        "provider": provider.provider,
        "model": provider.model,
        "prompt_sha256": RERANK_PROMPT_SHA256,
        "status": status,
        "candidates_scored": 0,
        "reordered": False,
        "latency_ms": 0,
    }


__all__ = [
    "DEFAULT_RERANKER_TIMEOUT_SECONDS",
    "OpenAICompatibleRerankProvider",
    "RERANKER_API_KEY_ENV",
    "RERANKER_BASE_URL_ENV",
    "RERANKER_MODEL_ENV",
    "RERANKER_STAGE",
    "RERANK_EMPTY_TEXT",
    "RERANK_FAIL_OPEN_PREFIX",
    "RERANK_MEMORY_CANDIDATE_CAP",
    "RERANK_PROMPT_SHA256",
    "RERANK_PROMPT_TEMPLATE",
    "RERANK_SOURCE_CANDIDATE_CAP",
    "RERANK_STATUS_NOOP",
    "RERANK_STATUS_FAIL_OPEN_PROVIDER_ERROR",
    "RERANK_STATUS_RERANKED",
    "RERANK_TEXT_KEYS",
    "RERANK_TEXT_MAX_CHARS",
    "RerankCompletion",
    "RerankOutcome",
    "RerankProvider",
    "VNextRerankerConfigurationError",
    "VNextRerankerProviderError",
    "build_rerank_prompt",
    "disabled_stage_record",
    "get_reranker_provider",
    "parse_rerank_scores",
    "rerank",
    "rerank_candidate_text",
    "rerank_fused_candidates",
    "reranker_stage_record",
]
