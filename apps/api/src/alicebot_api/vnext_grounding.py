"""Query-entity grounding: a pack-level retrieval STATISTIC, not advice.

When a query names a specific entity ("Sapiens", "Marcus Chen",
northwind.example) that the corpus has never seen, the context pack should
say so explicitly: an answer synthesized about an entity with zero
retrieval support is a hallucination waiting to happen. This module
computes that statistic; ``compile_context_pack`` attaches it as
``pack["grounding"]`` and mirrors it into the trace.

Everything here is deterministic and READ-ONLY (no store writes, no
events, no model calls). The honesty constraints are load-bearing:

- Salience is decided from the QUERY SURFACE ONLY (capitalized spans,
  quoted titles, domains, @handles, mid-sentence capitalized tokens,
  and attribute-qualified lowercase compounds like "30-gallon tank" or
  "my snake plant"). Nothing downstream may key off benchmark labels
  or query metadata.
- The claim "no stored memories mention X" is only made when EVERY
  available check misses: the entity substrate (names AND aliases via
  ``find_entities_by_names``) and cheap one-row FTS existence probes
  over source chunks and memories (``match_any=True``, so ANY token of
  a multi-token name anywhere in the corpus counts as support). All
  errors lean toward NOT flagging: a false "unmentioned" line is worse
  than a missed one, because it pushes the reader toward abstaining
  from an answerable question.
- Stores without any probe surface produce ``None`` ("cannot check"),
  never an unsupported claim.

Salience is deliberately more conservative than
``vnext_entities.extract_entity_candidates``: the acronym rule is
excluded (questions are full of common-noun acronyms -- TV, GPS, ID),
sentence-initial single capitalized tokens are ordinary English and
never salient, and leading interrogative/auxiliary words are stripped
from capitalized spans ("Did Marcus Chen ..." -> "Marcus Chen").
"""

from __future__ import annotations

from collections.abc import Sequence
import re

from alicebot_api.vnext_entities import (
    ENTITY_EXTRACTION_BLOCKLIST,
    extract_entity_candidates,
    store_supports_entity_linking,
)
from alicebot_api.vnext_entity_names import normalize_entity_name
from alicebot_api.vnext_repositories import JsonObject


MAX_GROUNDING_ENTITIES = 5
"""Cap on salient entities per query (order of first appearance).

Grounding is a cheap side statistic: a handful of one-row probes, never
a scan. Queries naming more entities than this keep the first few.
"""

# Capitalized-span rules from extract_entity_candidates that may seed a
# salient entity. The acronym rule is deliberately absent (common-noun
# acronyms saturate questions) and repeated_capitalized cannot fire on
# single-sentence queries anyway.
_QUERY_SPAN_RULES = frozenset(
    {"capitalized_span", "capitalized_span_default", "domain", "handle"}
)

# Interrogative/auxiliary words that glue onto a following capitalized
# name when they open a question ("Did Marcus Chen email me?"). They are
# stripped from the FRONT of capitalized spans only. Kept minimal; words
# already in ENTITY_EXTRACTION_BLOCKLIST (when, where, how, is, are,
# was, were, may, ...) are stripped by extraction itself.
_QUERY_LEADING_STOPWORDS = frozenset(
    {
        "what", "which", "who", "whom", "whose", "why",
        "did", "do", "does", "have", "has", "had",
        "can", "could", "will", "would", "shall", "should", "must", "might",
    }
)

# Mirrors vnext_entities._SINGLE_CAP_RE / _SENTENCE_BOUNDARY_CHARS; kept
# local because grounding is deliberately allowed to stay MORE
# conservative than extraction as either evolves.
_SINGLE_CAP_RE = re.compile(r"(?<![\w@.'-])[A-Z][a-z0-9'’-]*[a-z0-9](?![\w'-])")
_SENTENCE_BOUNDARY_CHARS = ".!?\n\r:;\"'()[]{}<>*#|—–-•"

# Quoted titles: 2-80 chars on one line, and the content must carry at
# least one capital letter to count as a name. Single-quote variants
# demand a non-word character on BOTH sides of the pair so possessive
# apostrophes ("Tom's book and Alex's car") can never open or close a
# span; double/curly quotes have no such collision.
_QUOTED_SPAN_RE = re.compile(
    r'"([^"\n]{2,80})"'
    r"|“([^”\n]{2,80})”"
    r"|(?<![\w'’])'([^'\n]{2,80})'(?![\w'’])"
    r"|(?<![\w‘’])‘([^‘’\n]{2,80})’(?![\w‘’])"
)
_MAX_QUOTED_TOKENS = 8
_HAS_CAPITAL_RE = re.compile(r"[A-Z]")

# Acronym shape: excluded from salience wholesale, including when a
# stripped span leaves one behind ("Which TV show ..." -> "TV").
_ACRONYM_SHAPE_RE = re.compile(r"[A-Z]{2,6}")

# Honorific + surname ("Dr. Johnson"): the extraction span regex cannot
# cross the "." and the "." makes the surname look sentence-initial to
# the single-token rule, so grounding handles the pair itself. The
# NAME is salient; the bare honorific never is (it names nobody).
_HONORIFIC_NAME_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+([A-Z][a-z][A-Za-z'’-]*)"
)
_HONORIFIC_TOKENS = frozenset({"mr", "mrs", "ms", "dr", "prof", "professor"})

# Possessive suffix on a capitalized token ("Emma's recipes"): the name
# is "Emma", never "Emma's".
_POSSESSIVE_SUFFIX_RE = re.compile(r"['’]s$")

# -- lowercase attribute-qualified nouns (round-3 loss forensics) -------------
#
# A query can name a specific THING without any capital letter when the
# noun carries an explicit qualifier: "my 30-gallon tank", "my snake
# plant", "my soccer team". Bare lowercase nouns are NEVER salient;
# conservatism lives in three stacked gates:
#
#   1. an explicit qualifier SHAPE — a measured quantity ("30-gallon")
#      or a possessive determiner plus a noun modifier ("my snake ..."),
#   2. a small curated HEAD-NOUN lexicon (kept-thing and activity-group
#      heads), so ordinary object talk ("my credit card", "my phone
#      number") can never fire,
#   3. modifier hygiene — generic adjectives ("new", "favorite",
#      "work"), blocklisted words, and digit-led modifiers never qualify
#      the possessive shape.
#
# Both rules admit the full compound surface ("30-gallon tank", "snake
# plant"), so the FTS probe ORs every token variant: any corpus mention
# of "tank(s)", "plant(s)", "snakes", ... counts as support and the
# note stays suppressed — the safe direction.

# Physical measurement units only. Time units (day, week, minute) are
# deliberately absent: "5-day trip" qualifies an EVENT, not a thing.
_MEASURE_UNITS = (
    "gallon", "litre", "liter", "quart", "pint",
    "ounce", "oz", "pound", "lb", "gram", "kilogram", "kg",
    "inch", "foot", "meter", "metre", "acre",
    "watt", "volt", "amp",
)
_QUANTIFIED_NOUN_RE = re.compile(
    r"\b\d+(?:\.\d+)?[- ](?:" + "|".join(_MEASURE_UNITS) + r")s?[- ]([a-z][a-z-]{2,})\b"
)

# Possessive-compound heads: kept things (a specific plant/tank in the
# user's life) and activity groups ("my soccer team", "my book club").
# Curated and short on purpose; growing it requires a false-positive
# table entry per addition (see test_vnext_grounding.py).
_COMPOUND_HEAD_NOUNS = frozenset(
    {
        "plant", "plants", "tree", "trees",
        "tank", "tanks", "aquarium", "terrarium",
        "team", "teams", "practice", "league", "club", "clubs",
        "lesson", "lessons", "class", "classes",
    }
)

# Modifiers that describe rather than name: possessive compounds built
# on these are ordinary object talk ("my new team", "my work club"),
# never a specific named thing.
_GENERIC_COMPOUND_MODIFIERS = frozenset(
    {
        "new", "old", "own", "first", "last", "next", "previous",
        "current", "former", "favorite", "favourite", "usual",
        "regular", "recent", "little", "big", "small", "large",
        "best", "worst", "whole", "entire", "main", "other",
        "second", "third", "local", "nearby", "long", "short",
        "early", "late", "morning", "evening", "afternoon",
        "weekly", "daily", "monthly", "weekend",
        "work", "home", "school", "office", "company", "family",
        "dream", "future", "online", "virtual",
    }
)

_POSSESSIVE_COMPOUND_RE = re.compile(
    r"\b(?:[Mm]y|[Oo]ur)\s+([a-z][a-z-]{2,})\s+([a-z]+)\b"
)


def _overlaps(covered: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < c_end and end > c_start for c_start, c_end in covered)


def _is_sentence_initial(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    return index < 0 or text[index] in _SENTENCE_BOUNDARY_CHARS


def _strip_leading_query_words(surface: str) -> str | None:
    """Drop leading interrogative/auxiliary tokens from a span surface."""
    tokens = surface.split()
    while tokens and tokens[0].casefold() in _QUERY_LEADING_STOPWORDS:
        tokens.pop(0)
    if not tokens:
        return None
    if len(tokens) == 1 and _ACRONYM_SHAPE_RE.fullmatch(tokens[0]):
        # "Which TV show ..." -> "TV": an acronym remnant, never salient.
        return None
    return " ".join(tokens)


def _mask_occurrences(query: str, surface: str, covered: list[tuple[int, int]]) -> None:
    start = query.find(surface)
    while start != -1:
        covered.append((start, start + len(surface)))
        start = query.find(surface, start + 1)


def salient_query_entities(query: str) -> tuple[str, ...]:
    """Conservative, deterministic salient-entity extraction for a query.

    Returns surface names in first-appearance order, deduplicated by
    normalized name and capped at ``MAX_GROUNDING_ENTITIES``. Generic
    lowercase nouns, blocklisted words, sentence-initial capitalized
    tokens, and acronyms are never salient. See the module docstring.
    """
    if not isinstance(query, str) or not query.strip():
        return ()

    found: list[tuple[int, str, str]] = []  # (position, normalized, surface)
    seen: set[str] = set()
    covered: list[tuple[int, int]] = []

    def admit(position: int, surface: str) -> None:
        normalized = normalize_entity_name(surface)
        if not normalized or normalized in ENTITY_EXTRACTION_BLOCKLIST:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        found.append((position, normalized, surface))

    # 1. Deterministic extraction: capitalized spans, domains, @handles.
    #    Leading interrogatives are stripped from spans; the ORIGINAL
    #    surface still masks its text range so its member tokens cannot
    #    resurface through the single-token rule below.
    for candidate in extract_entity_candidates(query):
        if candidate.source_rule not in _QUERY_SPAN_RULES:
            continue
        _mask_occurrences(query, candidate.name, covered)
        stripped = _strip_leading_query_words(candidate.name)
        if stripped is None:
            continue
        admit(max(query.find(candidate.name), 0), stripped)

    # 2. Quoted titles ("Sapiens", “The Name of the Wind”).
    for match in _QUOTED_SPAN_RE.finditer(query):
        inner = next((group for group in match.groups() if group), "").strip()
        if not inner or len(inner.split()) > _MAX_QUOTED_TOKENS:
            continue
        if not _HAS_CAPITAL_RE.search(inner):
            continue
        covered.append(match.span())
        admit(match.start(), inner)

    # 3. Honorific + surname ("Dr. Johnson" -> "Johnson"); the whole
    #    pair is masked so the bare honorific cannot resurface below.
    for match in _HONORIFIC_NAME_RE.finditer(query):
        if _overlaps(covered, match.start(), match.end()):
            continue
        covered.append(match.span())
        admit(match.start(), match.group(1))

    # 4. Single MID-SENTENCE capitalized tokens outside covered ranges
    #    ("When did I visit Lisbon?"). Sentence-initial capitalization is
    #    ordinary English and never salient on its own; neither are
    #    I-contractions ("I'm"), bare honorifics, acronym shapes, or
    #    pluralized blocklist words ("Fridays").
    for match in _SINGLE_CAP_RE.finditer(query):
        if _overlaps(covered, match.start(), match.end()):
            continue
        if _is_sentence_initial(query, match.start()):
            continue
        surface = _POSSESSIVE_SUFFIX_RE.sub("", match.group())
        if len(surface) < 2 or surface.startswith(("I'", "I’")):
            continue
        normalized = normalize_entity_name(surface)
        if normalized in _QUERY_LEADING_STOPWORDS or normalized in _HONORIFIC_TOKENS:
            continue
        if normalized.endswith("s") and normalized[:-1] in ENTITY_EXTRACTION_BLOCKLIST:
            continue
        admit(match.start(), surface)

    # 5. Quantity-qualified lowercase nouns ("my 30-gallon tank"): a
    #    measured quantity is a naming qualifier, so the full compound
    #    is salient. Physical units only; time-qualified events never
    #    fire (see the lexicon comment above).
    for match in _QUANTIFIED_NOUN_RE.finditer(query):
        if _overlaps(covered, match.start(), match.end()):
            continue
        head = match.group(1).casefold()
        if head in ENTITY_EXTRACTION_BLOCKLIST or head in _GENERIC_COMPOUND_MODIFIERS:
            continue
        covered.append(match.span())
        admit(match.start(), match.group())

    # 6. Possessive noun-noun compounds ("my snake plant", "my soccer
    #    team"): possessive determiner + specific modifier + curated
    #    head. All three gates must pass; "my hamster" (no modifier),
    #    "my credit card" (head not curated), and "my work team"
    #    (generic modifier) never fire.
    for match in _POSSESSIVE_COMPOUND_RE.finditer(query):
        if _overlaps(covered, match.start(), match.end()):
            continue
        modifier = match.group(1).casefold()
        head = match.group(2).casefold()
        if head not in _COMPOUND_HEAD_NOUNS:
            continue
        if (
            modifier in ENTITY_EXTRACTION_BLOCKLIST
            or modifier in _GENERIC_COMPOUND_MODIFIERS
            or modifier in _QUERY_LEADING_STOPWORDS
            or modifier in _COMPOUND_HEAD_NOUNS
        ):
            continue
        covered.append(match.span())
        admit(match.start(), f"{match.group(1)} {match.group(2)}")

    found.sort(key=lambda item: item[0])
    return tuple(surface for _position, _normalized, surface in found[:MAX_GROUNDING_ENTITIES])


# Deterministic morphological variants for probe tokens: FTS matches
# whole tokens, so a corpus that says "Hawaiian" must still count as
# support for "Hawaii" (and vice versa). Suffixes are appended AND
# stripped; every variant flows into one OR probe, so extra variants
# only ever suppress an "unmentioned" claim -- the safe direction.
_VARIANT_SUFFIXES = ("s", "es", "n", "an", "ian", "ans", "ians")
_VARIANT_MIN_TOKEN_CHARS = 3
_PROBE_TOKEN_RE = re.compile(r"\w+")


def _probe_query_variants(name: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            tokens.append(token)

    raw_tokens = _PROBE_TOKEN_RE.findall(name)
    # Pure-number tokens are dropped when the name has word tokens: a
    # bare "30" on an unrelated receipt is not a mention of the
    # "30-gallon tank", and letting it count as support would silently
    # suppress a truthful note. The word tokens still OR together in
    # the safe direction (any hit suppresses the note). All-number
    # names keep their digits -- they are the only probe surface.
    has_word_token = any(not token.isdigit() for token in raw_tokens)
    for token in raw_tokens:
        if has_word_token and token.isdigit():
            continue
        add(token)
        if len(token) < _VARIANT_MIN_TOKEN_CHARS:
            continue
        for suffix in _VARIANT_SUFFIXES:
            add(token + suffix)
            if token.casefold().endswith(suffix):
                add(token[: -len(suffix)])
    return " ".join(tokens)


def _probe_rows(
    method: object,
    *,
    query: str,
    domains: Sequence[str] | None,
    sensitivity_allowed: Sequence[str] | None,
) -> list[JsonObject] | None:
    """One cheap one-row FTS existence probe; ``None`` when it cannot run.

    ``match_any=True`` over the name's token VARIANTS is the
    conservative direction: ANY variant of ANY token anywhere in the
    corpus counts as support, so a partial mention ("Marcus" without
    "Chen") or a morphological one ("Hawaiian" for "Hawaii") never
    produces an "unmentioned" claim. Stores that predate the
    ``match_any`` kwarg get the strict single-name call instead (house
    degradation pattern; AND semantics forbid the variant expansion).
    """
    if not callable(method):
        return None
    kwargs = {
        "domains": list(domains) if domains else None,
        "sensitivity_allowed": list(sensitivity_allowed) if sensitivity_allowed else None,
        "limit": 1,
    }
    try:
        return list(method(query=_probe_query_variants(query), match_any=True, **kwargs))
    except TypeError:
        try:
            return list(method(query=query, **kwargs))
        except Exception:
            return None
    except Exception:
        # Grounding is a best-effort statistic. An operational store failure
        # must not abort retrieval or fabricate an unsupported-entity claim.
        # Exception deliberately excludes cancellation/SystemExit and every
        # other BaseException signal.
        return None


def corpus_support(
    entities: Sequence[str],
    store: object,
    *,
    domains: Sequence[str] | None = None,
    sensitivity_allowed: Sequence[str] | None = None,
) -> dict[str, bool] | None:
    """Per-entity corpus support; ``None`` when the store cannot be checked.

    An entity is supported when EITHER the entity substrate resolves it
    (normalized name or alias) OR a one-row FTS probe over source chunks
    or memories hits. Both misses are required before ``False`` -- the
    entity table alone is too sparse to prove absence (single-mention
    entities in long texts are deliberately not linked), and a false
    "unmentioned" claim is the failure mode this module exists to avoid.
    """
    names = [str(name) for name in entities if str(name).strip()]
    if not names:
        return {}
    support: dict[str, bool] = dict.fromkeys(names, False)
    checked_any = False

    if store_supports_entity_linking(store):
        normalized_by_name = {name: normalize_entity_name(name) for name in names}
        lookup_keys = tuple(dict.fromkeys(key for key in normalized_by_name.values() if key))
        known: set[str] = set()
        try:
            entity_rows = store.find_entities_by_names(lookup_keys) if lookup_keys else []
            for row in entity_rows:
                known.add(str(row.get("normalized_name")))
                aliases = row.get("aliases")
                if isinstance(aliases, (list, tuple)):
                    known.update(str(alias) for alias in aliases)
            for name in names:
                if normalized_by_name[name] and normalized_by_name[name] in known:
                    support[name] = True
            checked_any = True
        except Exception:
            # Continue to the independent FTS probes. A failed entity lookup
            # is "unknown", never evidence that an entity is unsupported.
            pass

    chunk_search = getattr(store, "search_source_chunks", None)
    memory_search = getattr(store, "search_memories_fts", None)
    for name in names:
        if support[name]:
            continue
        for method in (chunk_search, memory_search):
            rows = _probe_rows(
                method, query=name, domains=domains, sensitivity_allowed=sensitivity_allowed
            )
            if rows is None:
                continue
            checked_any = True
            if rows:
                support[name] = True
                break

    return support if checked_any else None


def compute_query_grounding(
    store: object,
    query: str,
    *,
    domains: Sequence[str] | None = None,
    sensitivity_allowed: Sequence[str] | None = None,
) -> JsonObject | None:
    """The ``pack["grounding"]`` payload, or ``None`` (the common case).

    Present ONLY when at least one salient query entity has zero corpus
    support::

        {"unsupported_entities": ["Sapiens"], "checked": 2}

    Queries with no salient entities, fully supported entities, or
    stores without a probe surface all return ``None`` so the pack
    schema is byte-stable for every ungated query.
    """
    names = salient_query_entities(query)
    if not names:
        return None
    support = corpus_support(
        names, store, domains=domains, sensitivity_allowed=sensitivity_allowed
    )
    if not support:
        return None
    unsupported = [name for name in names if support.get(name) is False]
    if not unsupported:
        return None
    return {"unsupported_entities": unsupported, "checked": len(names)}


__all__ = [
    "MAX_GROUNDING_ENTITIES",
    "compute_query_grounding",
    "corpus_support",
    "salient_query_entities",
]
