"""Fact-augmented retrieval keys for memories.

A memory that only says "Bike-a-Thon raised $5,000" is invisible to the
category-phrased question "what was the charity event fundraising
total?" under strict lexical search: the query and the memory share no
token. This module derives a small set of alternate retrieval phrasings
-- hypernym/category words, attribute names, unit/amount spellings --
and stores them in ``memories.fact_keys``, which both backends feed into
the memory FTS index (the Postgres ``search_tsv`` ``'D'`` weight from
migration ``20260707_0082``; the SQLite ``memories_fts`` ``fact_keys``
column at bm25 weight 0.1). Derived keys make memories FINDABLE without
letting them outrank direct text matches.

Two tiers:

- (a) deterministic -- entity/attribute recombination from the memory's
  own fields: hypernym-lexicon hits over title/canonical_text/summary,
  currency/percent/unit phrasings, shallow ``value`` attribute pairs,
  linked-entity names/aliases/type words, and ``memory_key`` path words
  (Postgres ``search_tsv`` does not index ``memory_key``, so this also
  closes that parity gap). Pure Python, microseconds per memory, always
  available.
- (b) optional model tier behind the same env seam as
  ``alicebot_api.vnext_embeddings``: ``ALICE_FACT_KEYS_BASE_URL`` +
  ``ALICE_FACT_KEYS_MODEL`` (plus ``ALICE_FACT_KEYS_API_KEY`` when the
  endpoint needs one) select an OpenAI-compatible ``/chat/completions``
  endpoint. Unconfigured (the default) means tier (a) is the entire
  behavior -- no network call is ever made. Provider failures degrade to
  tier (a) and are logged to the event log as
  ``memory.fact_keys_failed``, mirroring ``attach_memory_embedding``.

Honesty and safety properties:

- Keys are derived ONLY from the memory row itself (and optionally its
  linked entities); no benchmark labels or question metadata are read.
- Keys are indexable TEXT ONLY: short sanitized phrases, capped in count
  (``MAX_FACT_KEYS``) and length (``MAX_FACT_KEY_LENGTH``), stored in a
  plain text column and consumed exclusively by full-text indexing --
  they are never rendered into prompts or executed as instructions.
- Population sites are explicit: the memory-commit write path calls
  :func:`attach_memory_fact_keys` (one integration line, pinned to the
  deterministic tier so commits never make a synchronous model call),
  and ``scripts/backfill_memory_fact_keys.py`` covers pre-existing rows
  (where the model tier, when configured, is allowed to run).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request

from alicebot_api.provider_http import open_provider_url
from alicebot_api.provider_security import redirect_note
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_repositories import JsonObject


FACT_KEYS_BASE_URL_ENV = "ALICE_FACT_KEYS_BASE_URL"
FACT_KEYS_MODEL_ENV = "ALICE_FACT_KEYS_MODEL"
FACT_KEYS_API_KEY_ENV = "ALICE_FACT_KEYS_API_KEY"
DEFAULT_FACT_KEYS_TIMEOUT_SECONDS = 30

MAX_FACT_KEYS = 8
MAX_FACT_KEY_LENGTH = 80
MAX_FACT_KEY_WORDS = 12
MAX_PROVIDER_KEY_WORDS = 8
FACT_KEY_SEPARATOR = "; "
FACT_KEY_DERIVATION_ERROR_CODE = "fact_key_derivation_failed"
FACT_KEY_DERIVATION_ERROR_MESSAGE = "Memory fact-key derivation failed"

logger = logging.getLogger(__name__)


class VNextFactKeyConfigurationError(ValueError):
    """Raised when fact-key input or configuration is invalid."""


class VNextFactKeyProviderError(RuntimeError):
    """Raised when the fact-key model endpoint request fails."""


class FactKeyProvider(Protocol):
    provider: str
    model: str

    def suggest_keys(self, text: str) -> list[str]: ...


# --------------------------------------------------------------------------
# Deterministic tier (a)
# --------------------------------------------------------------------------

# Generic English hypernym lexicon: instance/trigger phrases -> the
# category phrasing a question is likely to use. Deliberately small and
# conservative; every category phrase is a handful of common nouns. The
# retrieval_quality eval thresholds guard against precision drift.
_CATEGORY_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "charity event fundraiser fundraising",
        (
            "bike-a-thon",
            "bikeathon",
            "walk-a-thon",
            "walkathon",
            "jog-a-thon",
            "dance-a-thon",
            "read-a-thon",
            "bake sale",
            "charity auction",
            "charity run",
            "charity gala",
            "fundraiser",
            "fund-raiser",
            "fundraising",
            "donation drive",
            "raffle",
            "telethon",
            "gala",
        ),
    ),
    (
        "pet dog animal",
        (
            "golden retriever",
            "labrador",
            "poodle",
            "beagle",
            "dachshund",
            "corgi",
            "terrier",
            "bulldog",
            "chihuahua",
            "puppy",
        ),
    ),
    ("pet cat animal", ("kitten", "tabby", "siamese cat", "calico")),
    (
        "vehicle car",
        ("sedan", "suv", "hatchback", "minivan", "coupe", "convertible", "pickup truck", "crossover"),
    ),
    ("vehicle bike motorcycle", ("motorbike", "scooter", "moped", "e-bike")),
    (
        "home housing residence",
        ("apartment", "condo", "studio apartment", "townhouse", "duplex", "loft"),
    ),
    (
        "job role profession occupation",
        (
            "engineer",
            "developer",
            "designer",
            "teacher",
            "professor",
            "nurse",
            "doctor",
            "accountant",
            "lawyer",
            "barista",
            "chef",
            "consultant",
            "analyst",
            "architect",
            "electrician",
            "plumber",
            "recruiter",
            "therapist",
        ),
    ),
    (
        "exercise fitness workout activity",
        (
            "yoga",
            "pilates",
            "marathon",
            "triathlon",
            "jog",
            "jogging",
            "hike",
            "hiking",
            "swim",
            "swimming",
            "cycling",
            "spin class",
            "gym",
            "crossfit",
            "zumba",
        ),
    ),
    ("book reading", ("novel", "memoir", "biography", "paperback", "audiobook", "book club")),
    (
        "movie film show television",
        ("sitcom", "documentary", "thriller", "rom-com", "miniseries", "season finale"),
    ),
    (
        "restaurant food dining meal",
        ("restaurant", "cafe", "bistro", "diner", "bakery", "sushi", "pizzeria", "food truck", "brunch", "takeout"),
    ),
    (
        "travel trip vacation",
        ("flight", "road trip", "itinerary", "airbnb", "hotel", "hostel", "cruise", "layover", "sightseeing"),
    ),
    (
        "class course education training",
        ("seminar", "workshop", "webinar", "bootcamp", "certification", "lecture", "tutorial"),
    ),
    (
        "health medical condition",
        ("allergy", "allergic", "migraine", "asthma", "physical therapy", "prescription", "medication", "diagnosis"),
    ),
    ("musical instrument music", ("guitar", "piano", "violin", "drums", "ukulele", "cello", "saxophone")),
    ("concert live music performance", ("music festival", "gig", "open mic")),
    ("wedding celebration event", ("engagement party", "bridal shower", "anniversary party")),
    ("birthday celebration party event", ("birthday",)),
    (
        "computer device electronics",
        ("laptop", "desktop", "tablet", "smartphone", "monitor", "headphones"),
    ),
    ("plant gardening", ("succulent", "fern", "orchid", "herb garden", "monstera")),
    ("game gaming hobby", ("board game", "video game", "chess", "puzzle", "poker")),
    ("interview hiring job application", ("job interview", "job offer", "resume")),
)

_LEXICON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        category,
        re.compile(
            r"\b(?:" + "|".join(re.escape(trigger) for trigger in triggers) + r")\b",
            re.IGNORECASE,
        ),
    )
    for category, triggers in _CATEGORY_LEXICON
)

_CURRENCY_WORDS = {"$": "dollars", "€": "euros", "£": "pounds"}
_CURRENCY_PATTERN = re.compile(r"([$€£])\s?(\d[\d,]*(?:\.\d+)?)")
_PERCENT_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.IGNORECASE)

# Abbreviated units -> (expanded unit word, hypernym attribute word).
_UNIT_EXPANSIONS: dict[str, tuple[str, str]] = {
    "km": ("kilometers", "distance"),
    "mi": ("miles", "distance"),
    "kg": ("kilograms", "weight"),
    "lb": ("pounds", "weight"),
    "lbs": ("pounds", "weight"),
    "hr": ("hours", "duration"),
    "hrs": ("hours", "duration"),
    "min": ("minutes", "duration"),
    "mins": ("minutes", "duration"),
}
_UNIT_PATTERN = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?)\s?(" + "|".join(sorted(_UNIT_EXPANSIONS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_ENTITY_TYPE_PHRASES: dict[str, str] = {
    "person": "person",
    "organization": "organization company",
    "project": "project",
    "topic": "topic",
    "technology": "technology tool",
    "market": "market",
    "report": "report",
    "agent": "agent",
}

_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")


def _memory_text_fields(memory: Mapping[str, object]) -> list[str]:
    parts: list[str] = []
    for field in ("title", "canonical_text", "summary"):
        value = memory.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    value = memory.get("value")
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return parts


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _WORD_PATTERN.finditer(text)}


def _sanitize_key(candidate: object, *, max_words: int = MAX_FACT_KEY_WORDS) -> str | None:
    """One indexable phrase: single line, bounded words/length, has a word."""
    if not isinstance(candidate, str):
        return None
    text = re.sub(r"\s+", " ", candidate.replace("\x00", " ")).strip()
    text = text.strip("-*•>\"'` \t").strip()
    if not text or not _WORD_PATTERN.search(text):
        return None
    words = text.split(" ")
    if len(words) > max_words:
        return None
    if len(text) > MAX_FACT_KEY_LENGTH:
        text = text[:MAX_FACT_KEY_LENGTH].rsplit(" ", 1)[0].strip()
        if not text or not _WORD_PATTERN.search(text):
            return None
    return text


def _memory_key_words(memory: Mapping[str, object]) -> str:
    """Path words from a HUMAN-authored memory_key, e.g. ``profile.vehicle``.

    Machine-generated keys (``vnext.capture.<type>.<hex>``,
    ``agentic_memory.<type>.<uuid>``) always carry an id segment; any
    non-alphabetic segment marks the whole key as generated noise --
    deriving "vnext capture semantic" for every captured row would put
    identical tokens on everything.
    """
    memory_key = memory.get("memory_key")
    if not isinstance(memory_key, str) or memory_key.startswith(("vnext.", "agentic_memory.")):
        return ""
    segments = [segment for segment in re.split(r"[._/\-:]+", memory_key) if segment]
    if not segments or not all(segment.isalpha() for segment in segments):
        return ""
    return " ".join(dict.fromkeys(segment.lower() for segment in segments))


# Provenance/identifier ``value`` attributes are plumbing, not retrieval
# phrasings: indexing "source chunk id <uuid>" into FTS puts identical
# high-idf uuid tokens on every captured memory, drowning real matches.
_VALUE_ATTRIBUTE_NAME_EXCLUSIONS = frozenset({"text", "intent", "source_refs", "source_id", "source_chunk_id"})
_IDENTIFIER_ATTRIBUTE_SUFFIXES = ("_id", "_ids")
# A uuid (with hyphens) or a bare hex identifier: 12+ chars drawn only from
# [0-9a-f] is never natural-language vocabulary worth indexing.
_IDENTIFIER_VALUE_PATTERN = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})$",
    re.IGNORECASE,
)


def _is_identifier_attribute(name: str) -> bool:
    return name in _VALUE_ATTRIBUTE_NAME_EXCLUSIONS or name.endswith(_IDENTIFIER_ATTRIBUTE_SUFFIXES)


def _is_identifier_value(raw: object) -> bool:
    return isinstance(raw, str) and _IDENTIFIER_VALUE_PATTERN.match(raw.strip()) is not None


def _value_attribute_keys(memory: Mapping[str, object]) -> list[str]:
    """Shallow ``value`` attribute pairs, e.g. ``{"total": "$5,000"}``.

    Skips provenance/identifier attributes (``source_id``,
    ``source_chunk_id``, anything ending in ``_id``/``_ids``) and any
    string value shaped like a uuid/hex identifier -- those are row
    plumbing, not phrasings a person would search by.
    """
    value = memory.get("value")
    if not isinstance(value, Mapping):
        return []
    keys: list[str] = []
    for name in sorted(str(key) for key in value.keys()):
        if _is_identifier_attribute(name):
            continue
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            continue
        if _is_identifier_value(raw):
            continue
        attribute = " ".join(part for part in re.split(r"[_\-]+", name) if part).strip()
        rendered = f"{attribute} {raw}".strip()
        if attribute and rendered:
            keys.append(rendered)
    return keys


def _amount_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _CURRENCY_PATTERN.finditer(text):
        symbol, amount = match.group(1), match.group(2)
        digits = amount.replace(",", "")
        keys.append(f"{digits} {_CURRENCY_WORDS[symbol]} total amount")
    for match in _PERCENT_PATTERN.finditer(text):
        keys.append(f"{match.group(1)} percent percentage")
    for match in _UNIT_PATTERN.finditer(text):
        number = match.group(1).replace(",", "")
        expanded, hypernym = _UNIT_EXPANSIONS[match.group(2).lower()]
        keys.append(f"{number} {expanded} {hypernym}")
    return keys


def _entity_keys(entities: Iterable[Mapping[str, object]]) -> list[str]:
    keys: list[str] = []
    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        name = entity.get("name")
        entity_type = entity.get("entity_type")
        type_phrase = _ENTITY_TYPE_PHRASES.get(entity_type) if isinstance(entity_type, str) else None
        if isinstance(name, str) and name.strip():
            keys.append(f"{name.strip()} {type_phrase}".strip() if type_phrase else name.strip())
        aliases = entity.get("aliases")
        if isinstance(aliases, (list, tuple)):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    keys.append(alias.strip())
    return keys


def derive_deterministic_fact_keys(
    memory: Mapping[str, object],
    *,
    entities: Iterable[Mapping[str, object]] = (),
    max_keys: int = MAX_FACT_KEYS,
) -> list[str]:
    """Tier (a): derived retrieval keys from the memory row alone.

    Deterministic: same input mapping -> same ordered key list. A
    candidate key is kept only when it contributes at least one token the
    memory's own indexed text does not already have, so fact keys spend
    their capped slots on NEW vocabulary instead of echoing the row.
    """
    text_fields = _memory_text_fields(memory)
    combined = "\n".join(text_fields)
    # Novelty baseline is the memory's TEXT fields (what Postgres
    # search_tsv indexes) -- deliberately not memory_key, so key-path
    # words like "vehicle" become searchable on Postgres too.
    base_tokens = _tokens(combined)

    candidates: list[str] = []
    # 1. Hypernym lexicon hits, ordered by first trigger position.
    matched: list[tuple[int, str]] = []
    for category, pattern in _LEXICON_PATTERNS:
        match = pattern.search(combined)
        if match is not None:
            matched.append((match.start(), category))
    candidates.extend(category for _position, category in sorted(matched))
    # 2. Amount / percent / unit phrasings.
    candidates.extend(_amount_keys(combined))
    # 3. Shallow value attribute pairs.
    candidates.extend(_value_attribute_keys(memory))
    # 4. Linked-entity names, aliases, and type words.
    candidates.extend(_entity_keys(entities))
    # 5. memory_key path words (Postgres search_tsv does not cover memory_key).
    candidates.append(_memory_key_words(memory))

    keys: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _sanitize_key(candidate)
        if key is None:
            continue
        folded = key.lower()
        if folded in seen:
            continue
        if not (_tokens(key) - base_tokens):
            continue
        seen.add(folded)
        keys.append(key)
        if len(keys) >= max_keys:
            break
    return keys


# --------------------------------------------------------------------------
# Optional model tier (b) behind the provider seam
# --------------------------------------------------------------------------

_PROVIDER_INSTRUCTION = (
    "You expand a stored memory into alternate search phrasings. Reply with a "
    "JSON array of at most 8 short phrases (category words, attribute names, "
    "unit or amount spellings) someone might use to look this memory up. "
    "Phrases only; no explanations."
)


class OpenAICompatibleFactKeyProvider:
    """Fact-key client for any OpenAI-compatible ``/chat/completions`` endpoint.

    Same construction as ``OpenAICompatibleEmbeddingProvider``: standard
    library only, works against OpenAI, Ollama's ``/v1``, LM Studio, vLLM.
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = DEFAULT_FACT_KEYS_TIMEOUT_SECONDS,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        normalized_model = model.strip()
        if normalized_base_url == "":
            raise VNextFactKeyConfigurationError("fact-key base_url must not be empty")
        if normalized_model == "":
            raise VNextFactKeyConfigurationError("fact-key model must not be empty")
        self.base_url = normalized_base_url
        self.model = normalized_model
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.timeout_seconds = timeout_seconds

    def suggest_keys(self, text: str) -> list[str]:
        if not isinstance(text, str) or text.strip() == "":
            raise VNextFactKeyConfigurationError("fact-key input text must be a non-empty string")
        payload: JsonObject = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _PROVIDER_INSTRUCTION},
                {"role": "user", "content": text},
            ],
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
            raise VNextFactKeyProviderError(
                f"fact-key endpoint returned HTTP {exc.code}{redirect_note(exc.code)}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise VNextFactKeyProviderError(f"fact-key request failed: {exc}") from exc
        return _parse_provider_keys(_extract_chat_content(response_payload))


def _extract_chat_content(payload: object) -> str:
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
    raise VNextFactKeyProviderError("fact-key response did not include a chat completion message")


def _parse_provider_keys(content: str) -> list[str]:
    """Model output -> sanitized phrases; JSON array preferred, lines tolerated."""
    parsed: object | None = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        array_match = re.search(r"\[.*?\]", content, re.DOTALL)
        if array_match is not None:
            try:
                parsed = json.loads(array_match.group(0))
            except json.JSONDecodeError:
                parsed = None
    raw_items: list[object] = list(parsed) if isinstance(parsed, list) else list(content.splitlines())
    keys: list[str] = []
    for item in raw_items:
        key = _sanitize_key(item, max_words=MAX_PROVIDER_KEY_WORDS)
        if key is not None:
            keys.append(key)
    return keys


def get_fact_key_provider() -> OpenAICompatibleFactKeyProvider | None:
    """Build the configured fact-key provider, or ``None`` when unconfigured.

    Unconfigured means deterministic-tier-only derivation; there is no
    fake or canned model fallback.
    """
    base_url = os.environ.get(FACT_KEYS_BASE_URL_ENV, "").strip()
    model = os.environ.get(FACT_KEYS_MODEL_ENV, "").strip()
    if base_url == "" or model == "":
        return None
    api_key = os.environ.get(FACT_KEYS_API_KEY_ENV, "").strip() or None
    return OpenAICompatibleFactKeyProvider(base_url=base_url, model=model, api_key=api_key)


# --------------------------------------------------------------------------
# Combined derivation and storage helpers
# --------------------------------------------------------------------------


def derive_fact_keys(
    memory: Mapping[str, object],
    *,
    entities: Iterable[Mapping[str, object]] = (),
    provider: FactKeyProvider | None = None,
    max_keys: int = MAX_FACT_KEYS,
) -> list[str]:
    """Derived retrieval keys: deterministic tier first, model tier fills up.

    With ``provider=None`` (the unconfigured default) this is fully
    deterministic. Provider keys are sanitized, deduplicated against the
    deterministic keys, filtered for novelty against the memory's own
    text, and only ever APPEND -- the deterministic keys always survive.
    Provider failures raise ``VNextFactKeyProviderError``; callers that
    must not fail (the commit path) catch it and keep tier (a).
    """
    keys = derive_deterministic_fact_keys(memory, entities=entities, max_keys=max_keys)
    if provider is None or len(keys) >= max_keys:
        return keys
    text = "\n".join(_memory_text_fields(memory))
    if text.strip() == "":
        return keys
    suggested = provider.suggest_keys(text)
    base_tokens = _tokens(text)
    seen = {key.lower() for key in keys}
    for key in suggested:
        folded = key.lower()
        if folded in seen:
            continue
        if not (_tokens(key) - base_tokens):
            continue
        seen.add(folded)
        keys.append(key)
        if len(keys) >= max_keys:
            break
    return keys


def fact_keys_text(keys: Sequence[str]) -> str:
    """Join keys into the single indexable ``memories.fact_keys`` value.

    An empty list renders as ``""`` -- distinct from NULL, which means
    "never derived" and keeps the row visible to the backfill pass.
    """
    return FACT_KEY_SEPARATOR.join(keys)


def split_fact_keys(text: object) -> list[str]:
    if not isinstance(text, str) or text.strip() == "":
        return []
    return [part.strip() for part in text.split(FACT_KEY_SEPARATOR.strip()) if part.strip()]


def attach_memory_fact_keys(
    store: object,
    memory: Mapping[str, object],
    *,
    entities: Iterable[Mapping[str, object]] = (),
    provider: FactKeyProvider | None = None,
    use_env_provider: bool = True,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> bool:
    """Best-effort derive-and-store for a memory row.

    Mirrors ``attach_memory_embedding``: never blocks the memory write.
    Stores without ``update_memory_fact_keys`` skip silently; provider
    failures fall back to the deterministic tier and log a
    ``memory.fact_keys_failed`` event. Always writes at least ``""`` so
    the row reads as processed to the backfill pass.

    ``use_env_provider=False`` pins the call to the deterministic tier
    regardless of ``ALICE_FACT_KEYS_*`` -- the memory-commit path uses it
    so commits NEVER make a synchronous model call (the model tier is for
    the backfill/maintenance entry points).
    """
    update_memory_fact_keys = getattr(store, "update_memory_fact_keys", None)
    if not callable(update_memory_fact_keys):
        return False
    resolved_provider = provider
    if resolved_provider is None and use_env_provider:
        resolved_provider = get_fact_key_provider()
    try:
        keys = derive_fact_keys(memory, entities=entities, provider=resolved_provider)
    except (VNextFactKeyConfigurationError, VNextFactKeyProviderError) as exc:
        logger.error(
            "memory fact-key derivation failed memory_id=%s error_code=%s",
            memory.get("id"),
            FACT_KEY_DERIVATION_ERROR_CODE,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        append_event(
            store,  # type: ignore[arg-type]
            event_type="memory.fact_keys_failed",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory.get("id")),
            trace_id=trace_id,
            payload={
                "error_code": FACT_KEY_DERIVATION_ERROR_CODE,
                "error_message": FACT_KEY_DERIVATION_ERROR_MESSAGE,
                "provider": getattr(resolved_provider, "provider", None),
                "model": getattr(resolved_provider, "model", None),
            },
        )
        keys = derive_deterministic_fact_keys(memory, entities=entities)
    row = update_memory_fact_keys(memory_id=str(memory["id"]), fact_keys=fact_keys_text(keys))
    return row is not None


def apply_fact_keys(
    store: object,
    memory_id: str,
    *,
    entities: Iterable[Mapping[str, object]] = (),
    provider: FactKeyProvider | None = None,
    use_env_provider: bool = True,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> bool:
    """Fetch-then-attach entry point for integrators that only hold an id."""
    get_memory = getattr(store, "get_memory", None)
    if not callable(get_memory):
        return False
    memory = get_memory(str(memory_id))
    if memory is None:
        return False
    return attach_memory_fact_keys(
        store,
        memory,
        entities=entities,
        provider=provider,
        use_env_provider=use_env_provider,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )


def backfill_memory_fact_keys(
    store: object,
    *,
    batch_size: int = 200,
    provider: FactKeyProvider | None = None,
    use_env_provider: bool = True,
) -> JsonObject:
    """Derive fact keys for every memory whose ``fact_keys`` is still NULL.

    Pages with ``list_memories_missing_fact_keys`` (both stores implement
    it) so a run over a large table is O(rows) with bounded memory. Rows
    whose derivation yields nothing are written as ``""`` -- processed,
    empty -- so re-runs converge instead of rescanning them forever.
    """
    lister = getattr(store, "list_memories_missing_fact_keys", None)
    updater = getattr(store, "update_memory_fact_keys", None)
    if not callable(lister) or not callable(updater):
        raise VNextFactKeyConfigurationError(
            "store does not support fact-key backfill "
            "(needs list_memories_missing_fact_keys and update_memory_fact_keys)"
        )
    if batch_size < 1:
        raise VNextFactKeyConfigurationError("batch_size must be at least 1")
    resolved_provider = provider
    if resolved_provider is None and use_env_provider:
        resolved_provider = get_fact_key_provider()
    batches = 0
    updated = 0
    empty = 0
    provider_failures = 0
    after_id: str | None = None
    while True:
        rows = lister(limit=batch_size, after_id=after_id)
        if not rows:
            break
        batches += 1
        after_id = str(rows[-1]["id"])
        for row in rows:
            try:
                keys = derive_fact_keys(row, provider=resolved_provider)
            except (VNextFactKeyConfigurationError, VNextFactKeyProviderError):
                provider_failures += 1
                keys = derive_deterministic_fact_keys(row)
            if updater(memory_id=str(row["id"]), fact_keys=fact_keys_text(keys)) is not None:
                updated += 1
                if not keys:
                    empty += 1
    return {
        "provider": getattr(resolved_provider, "provider", None),
        "model": getattr(resolved_provider, "model", None),
        "batches": batches,
        "updated": updated,
        "empty": empty,
        "provider_failures": provider_failures,
    }


__all__ = [
    "DEFAULT_FACT_KEYS_TIMEOUT_SECONDS",
    "FACT_KEYS_API_KEY_ENV",
    "FACT_KEYS_BASE_URL_ENV",
    "FACT_KEYS_MODEL_ENV",
    "FACT_KEY_SEPARATOR",
    "FactKeyProvider",
    "MAX_FACT_KEYS",
    "MAX_FACT_KEY_LENGTH",
    "OpenAICompatibleFactKeyProvider",
    "VNextFactKeyConfigurationError",
    "VNextFactKeyProviderError",
    "apply_fact_keys",
    "attach_memory_fact_keys",
    "backfill_memory_fact_keys",
    "derive_deterministic_fact_keys",
    "derive_fact_keys",
    "fact_keys_text",
    "get_fact_key_provider",
    "split_fact_keys",
]
