"""Deterministic entity extraction and linking for the vNext temporal graph.

Extraction here is rule-based ONLY: a handful of documented regex and
repetition heuristics plus a small blocklist. There is NO LLM anywhere in
this module and none may be added casually -- a model-backed extractor is
a later, review-gated enhancement that must land behind the usual
generation_mode/model-routing controls, not inside this deterministic
path.

Extraction rules (each candidate records which rule produced it in
``source_rule`` and takes a fixed confidence from ``RULE_CONFIDENCE`` --
deliberately inside the 0.5-0.8 band because every rule is a heuristic,
never certainty):

- ``capitalized_span`` (0.75): two or more consecutive capitalized
  tokens ("Jane Doe", "Northwind Capital") that carry POSITIVE type
  evidence (org suffix, honorific, or a person-context cue -- see the
  type table below). Leading/trailing blocklisted tokens are stripped
  ("The Alice Core" -> "Alice Core"); the span is dropped if fewer than
  two tokens survive.
- ``capitalized_span_default`` (0.60): the same multi-token spans when
  NO type evidence is present ("Street Threads", "Alice Core"). These
  are typed 'other' -- a bare "Two Capitalized Words" shape is NOT
  evidence of a person; LongMemEval-style shopping chatter is full of
  brandish two-token spans.
- ``domain`` (0.70): bare domains such as ``northwind.example``. The final
  label must be alphabetic, and common file suffixes (``notes.md``,
  ``node.js``) are excluded via ``_FILE_SUFFIX_PSEUDO_TLDS``.
- ``handle`` (0.65): ``@handles`` not embedded in an email address.
- ``acronym`` (0.60): ALL-CAPS tokens of 2-6 characters ("MCP", "RRF")
  outside already-matched spans.
- ``repeated_capitalized`` (0.55): single capitalized tokens ONLY when
  they occur at least twice in the text AND at least once mid-sentence.
  Sentence-initial-only capitalization is ordinary English, so a single
  word that never appears mid-sentence is treated as noise.

Coarse ``entity_type`` guesses come from a deliberately small,
documented table (checked in this order):

- organization: span ends in an org suffix (``_ORGANIZATION_SUFFIXES``)
  or the candidate is a bare domain;
- person: span starts with an honorific (``_HONORIFICS``); OR the span
  is exactly two capitalized words AND a cheap relational/possessive
  cue appears in the +-3-token context window around the span -- a cue
  word directly before it ("with X", "met X", "my friend X"; see
  ``_PERSON_CONTEXT_CUES_BEFORE``) or a speech verb immediately after
  it ("X said", "X told me"; see ``_PERSON_CONTEXT_CUES_AFTER``); OR
  the candidate is an @handle;
- other: everything else -- including two-token spans WITHOUT such
  evidence (the pre-LongMemEval default of "exactly two capitalized
  tokens => person" misfiled brand names wholesale).

Volume guards (noisy conversational text must not flood the entity
table): in texts longer than ``LONG_TEXT_CHAR_THRESHOLD`` characters a
multi-token span must occur at least ``LONG_TEXT_SPAN_REPEAT_MINIMUM``
times to become a candidate (one mention in a short note is signal; one
mention in a 10k-char transcript is noise), and linking writes at most
``MAX_LINKED_ENTITIES_PER_TEXT`` candidates per text, selected by
confidence then in-text frequency (``select_candidates_for_linking``).

``EntityLinkingService`` resolves candidates against the entities
substrate (``find_entities_by_names``), records mentions on existing
entities, creates new ones with ``first/last_observed_at`` set to the
caller's event time, and connects the owning source/memory to each
entity with a ``mentions`` graph edge carrying ``observed_at`` event
time. Linking is idempotent per (owner, entity): when the mentions edge
already exists the candidate is skipped entirely (no double mention
count, no duplicate edge).

Alias growth: aliases store NORMALIZED variants (the store convention),
so an alias is only appended when it adds resolution power. The
deterministic path that produces one is the honorific fallback: "Dr
Jane Doe" fails its primary lookup, matches the existing "Jane
Doe" entity via the honorific-stripped key, records the mention
there, and appends ``dr jane doe`` to the entity's aliases so the
next occurrence resolves in one lookup.

Sensitivity: entity rows leak content into ``entities.name`` (a name
IS content), so callers must skip extraction entirely for sources at or
above 'private' -- see ``ENTITY_EXTRACTION_SKIP_SENSITIVITIES``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import re
from typing import Protocol, TypeGuard, cast

from alicebot_api.vnext_entity_names import normalize_entity_name
from alicebot_api.vnext_repositories import JsonObject


# Blocklist of normalized single tokens that never become entity
# candidates on their own and are stripped from span edges: weekdays,
# months, common sentence-starters/function words, the capture-prefix
# vocabulary ("Decision:", "Fact:", ...), and a few all-caps
# conversational acronyms. Kept deliberately small; growing it is cheap
# and reviewable.
ENTITY_EXTRACTION_BLOCKLIST = frozenset(
    {
        # weekdays
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        # months
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        # sentence starters / function words
        "a", "an", "the", "this", "that", "these", "those", "there", "then",
        "when", "where", "while", "after", "before", "however", "meanwhile",
        "also", "and", "but", "because", "although", "though", "unless",
        "until", "if", "in", "on", "at", "by", "to", "of", "as", "or", "not",
        "with", "met",
        "is", "are", "was", "were", "be", "been", "it", "its", "we", "our",
        "i", "my", "you", "your", "he", "his", "she", "her", "they", "their",
        "yes", "no", "please", "thanks", "thank", "hello", "hi", "hey", "dear",
        "today", "tomorrow", "yesterday", "tonight",
        # sentence-starting adverbs/imperatives that otherwise glue onto a
        # following capitalized name ("Later Jane Doe", "Ask Hermes")
        "later", "earlier", "soon", "now", "here", "finally", "suddenly",
        "maybe", "perhaps", "everyone", "someone", "anyone",
        "ask", "tell", "call", "ping", "email", "check",
        # capture line-prefix vocabulary (see vnext_capture._PREFIX_RULES)
        "decision", "preference", "prefer", "remember", "fact", "belief",
        "question", "answer", "commitment", "todo", "note", "procedure",
        "playbook", "happened", "log", "how",
        # all-caps conversational noise the acronym rule would otherwise take
        "ok", "okay", "asap", "fyi", "am", "pm", "tbd", "tba", "eta", "ps",
    }
)

# Sensitivity levels at or above 'private' skip extraction entirely:
# entity names are derived from the text, so an entities row would leak
# private content into a table that is broadly readable. 'secret' is the
# pre-alias spelling of 'highly_sensitive' and is kept for callers that
# pass raw input values.
ENTITY_EXTRACTION_SKIP_SENSITIVITIES = frozenset(
    {
        "private",
        "secret",
        "confidential",
        "highly_sensitive",
        "sacred",
        "regulated",
    }
)

# Fixed per-rule confidence, kept inside the 0.5-0.8 heuristic band.
# capitalized_span is the evidence-backed span rule (org suffix,
# honorific, or person-context cue); capitalized_span_default is the
# same span shape WITHOUT type evidence and sits lower in the band so
# cap selection and future bulk re-typing flows can target it.
RULE_CONFIDENCE = {
    "capitalized_span": 0.75,
    "domain": 0.7,
    "handle": 0.65,
    "acronym": 0.6,
    "capitalized_span_default": 0.6,
    "repeated_capitalized": 0.55,
}

# graph_edges.edge_type is CHECK-constrained on both backends; both
# values below exist in the shared EDGE_TYPES vocabulary. The audit's
# "about" relation for person memories is carried as
# metadata_json.relation on a related_to_person edge because "about" is
# not in the constrained vocabulary (schema files are owned elsewhere).
ENTITY_MENTION_EDGE_TYPE = "mentions"
PERSON_ABOUT_EDGE_TYPE = "related_to_person"

MAX_LINKED_ENTITIES_PER_TEXT = 25
"""Per-text cap on candidates that may become entity/edge writes.

Extraction stays pure and unbounded, but one runaway document must not
fan out into hundreds of entity/edge writes. Survivors are selected by
confidence, then in-text frequency -- NOT first-appearance order -- so
a strong candidate late in a noisy text always beats weak early noise
(see ``select_candidates_for_linking``).
"""

LONG_TEXT_CHAR_THRESHOLD = 1500
"""Character length above which a text counts as LONG for extraction.

Long conversational texts (LongMemEval-style transcripts) mention many
incidental capitalized spans exactly once; short notes name the things
they are actually about. Texts longer than this keep the stricter
``LONG_TEXT_SPAN_REPEAT_MINIMUM`` repeat rule; texts at or under it
keep single-mention capture (a person mentioned once in a short note is
signal).
"""

LONG_TEXT_SPAN_REPEAT_MINIMUM = 2
"""Occurrences a multi-token span needs in a LONG text to qualify.

Applies only to ``capitalized_span``/``capitalized_span_default``
candidates in texts longer than ``LONG_TEXT_CHAR_THRESHOLD`` chars: a
span that appears once in a 10k-char transcript is noise, so it must
repeat at least this many times to become a candidate. Occurrences of
below-threshold spans still mask their text range from the weaker
single-token rule (dropping a span must not resurrect its halves).
"""

_ORGANIZATION_SUFFIXES = frozenset(
    {
        "inc", "llc", "ltd", "co", "corp", "corporation", "labs", "gmbh",
        "ventures", "capital", "partners", "group", "systems",
    }
)
_HONORIFICS = frozenset({"mr", "mrs", "ms", "dr", "prof", "professor"})

# Person-context cues: cheap positive evidence that a two-token
# capitalized span names a person. BEFORE cues may appear anywhere in
# the 3 tokens preceding the span ("met up with Jane Doe", "my
# friend Alice Rivers"); AFTER cues must be the FIRST token following
# the span ("Marcus Chen said", "Jane Doe, who ..."), because speech
# verbs further out stop being about the span. Deliberately small and
# relational -- generic subject verbs ("runs", "has", "launched") are
# things brands do in shopping chatter and stay out of this table.
_PERSON_CONTEXT_CUES_BEFORE = frozenset(
    {
        "with", "met", "meet", "meeting", "told", "asked", "thank",
        "thanks", "thanked", "emailed", "pinged", "dear",
        "friend", "colleague", "coworker", "cousin", "brother", "sister",
        "mom", "dad", "wife", "husband", "boss",
    }
)
_PERSON_CONTEXT_CUES_AFTER = frozenset(
    {"said", "says", "told", "tells", "asked", "asks", "replied", "wrote", "mentioned", "who"}
)
# +-3 tokens around the span; 60 chars is enough to carry 3 tokens.
_CONTEXT_WINDOW_TOKENS = 3
_CONTEXT_WINDOW_CHARS = 60
_CONTEXT_WORD_RE = re.compile(r"[A-Za-z']+")

# Domain-rule exclusions: final labels that are file suffixes, not TLDs.
_FILE_SUFFIX_PSEUDO_TLDS = frozenset(
    {
        "md", "txt", "rst", "py", "js", "ts", "tsx", "jsx", "json", "yml",
        "yaml", "toml", "ini", "csv", "tsv", "html", "css", "pdf", "png",
        "jpg", "jpeg", "gif", "svg", "doc", "docx", "xls", "xlsx", "ppt",
        "pptx", "zip", "tar", "gz", "log", "tmp", "lock", "sh", "exe",
    }
)

_SPAN_TOKEN = r"[A-Z][A-Za-z0-9'’_-]*"
_CAP_SPAN_RE = re.compile(rf"(?<![\w@.'-]){_SPAN_TOKEN}(?:[ \t]+{_SPAN_TOKEN})+")
_SPAN_TOKEN_RE = re.compile(_SPAN_TOKEN)
_ACRONYM_RE = re.compile(r"(?<![\w@.-])[A-Z]{2,6}(?![\w-])")
_HANDLE_RE = re.compile(r"(?<![\w@.])@[A-Za-z0-9_]{2,32}(?![\w@])")
_DOMAIN_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}\b",
    re.IGNORECASE,
)
_SINGLE_CAP_RE = re.compile(r"(?<![\w@.'-])[A-Z][a-z0-9'’-]*[a-z0-9](?![\w'-])")
# Characters that end a sentence (or start a list item) for the
# sentence-initial check used by the repeated_capitalized rule.
_SENTENCE_BOUNDARY_CHARS = ".!?\n\r:;\"'()[]{}<>*#|—–-•"


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    name: str
    normalized: str
    entity_type: str
    confidence: float
    source_rule: str
    # In-text occurrence count across all rules for this normalized
    # name; the frequency tie-break for cap selection.
    occurrences: int = 1

    def to_record(self) -> JsonObject:
        return {
            "name": self.name,
            "normalized": self.normalized,
            "entity_type": self.entity_type,
            "confidence": self.confidence,
            "source_rule": self.source_rule,
            "occurrences": self.occurrences,
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_blocked(token: str) -> bool:
    return normalize_entity_name(token) in ENTITY_EXTRACTION_BLOCKLIST


def _overlaps(covered: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < c_end and end > c_start for c_start, c_end in covered)


def _is_sentence_initial(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    return index < 0 or text[index] in _SENTENCE_BOUNDARY_CHARS


def _has_person_context(text: str, start: int, end: int) -> bool:
    """Cheap +-3-token window check for person evidence around a span."""
    before = _CONTEXT_WORD_RE.findall(text[max(0, start - _CONTEXT_WINDOW_CHARS) : start])
    if any(
        token.casefold() in _PERSON_CONTEXT_CUES_BEFORE
        for token in before[-_CONTEXT_WINDOW_TOKENS:]
    ):
        return True
    after = _CONTEXT_WORD_RE.findall(text[end : end + _CONTEXT_WINDOW_CHARS])
    return bool(after) and after[0].casefold() in _PERSON_CONTEXT_CUES_AFTER


def _classify_span(token_normals: list[str], person_context: bool) -> tuple[str, str]:
    """(entity_type, source_rule) for a capitalized span (documented table).

    'person' requires POSITIVE evidence: an honorific prefix, or the
    exact "First Last" two-token shape backed by a relational cue in
    the surrounding context window. A bare two-token span defaults to
    'other' via the lower-confidence ``capitalized_span_default`` rule
    -- brandish spans ("Street Threads", "Urban Edge") are not people.
    """
    if token_normals and token_normals[-1] in _ORGANIZATION_SUFFIXES:
        return "organization", "capitalized_span"
    if token_normals and token_normals[0] in _HONORIFICS:
        return "person", "capitalized_span"
    if len(token_normals) == 2 and person_context:
        return "person", "capitalized_span"
    return "other", "capitalized_span_default"


def _candidate(name: str, entity_type: str, rule: str) -> EntityCandidate | None:
    normalized = normalize_entity_name(name)
    if not normalized or normalized in ENTITY_EXTRACTION_BLOCKLIST:
        return None
    return EntityCandidate(
        name=name,
        normalized=normalized,
        entity_type=entity_type,
        confidence=RULE_CONFIDENCE[rule],
        source_rule=rule,
    )


def extract_entity_candidates(text: str) -> tuple[EntityCandidate, ...]:
    """Deterministic, pure entity extraction over ``text``.

    Returns candidates in first-appearance order, deduplicated by
    normalized name (the highest-confidence rule wins a tie) and
    carrying their in-text ``occurrences`` count. In texts longer than
    ``LONG_TEXT_CHAR_THRESHOLD`` chars, multi-token spans must repeat
    at least ``LONG_TEXT_SPAN_REPEAT_MINIMUM`` times. See the module
    docstring for the rule table and noise controls.
    """
    if not isinstance(text, str) or not text.strip():
        return ()

    found: list[tuple[int, EntityCandidate]] = []
    covered: list[tuple[int, int]] = []
    long_text = len(text) > LONG_TEXT_CHAR_THRESHOLD

    # Rule: capitalized multi-word spans (2+ tokens), edges de-noised.
    # Occurrences are grouped per normalized name first so LONG texts
    # can enforce the repeat threshold; every occurrence still marks
    # its range as covered so a below-threshold span never resurfaces
    # through the single-token rule.
    span_groups: dict[str, list[tuple[int, EntityCandidate]]] = {}
    for match in _CAP_SPAN_RE.finditer(text):
        tokens = [
            (token.start() + match.start(), token.group())
            for token in _SPAN_TOKEN_RE.finditer(match.group())
        ]
        while tokens and _is_blocked(tokens[0][1]):
            tokens.pop(0)
        while tokens and _is_blocked(tokens[-1][1]):
            tokens.pop()
        if len(tokens) < 2:
            continue
        start = tokens[0][0]
        end = tokens[-1][0] + len(tokens[-1][1])
        surface = text[start:end]
        token_normals = [normalize_entity_name(token) for _, token in tokens]
        entity_type, rule = _classify_span(token_normals, _has_person_context(text, start, end))
        candidate = _candidate(surface, entity_type, rule)
        if candidate is None:
            continue
        covered.append((start, end))
        span_groups.setdefault(candidate.normalized, []).append((start, candidate))
    for group in span_groups.values():
        if long_text and len(group) < LONG_TEXT_SPAN_REPEAT_MINIMUM:
            continue
        found.extend(group)

    # Rule: bare domains (northwind.example) -> organization.
    for match in _DOMAIN_RE.finditer(text):
        if _overlaps(covered, match.start(), match.end()):
            continue
        final_label = match.group().rsplit(".", 1)[-1].casefold()
        if not final_label.isalpha() or final_label in _FILE_SUFFIX_PSEUDO_TLDS:
            continue
        candidate = _candidate(match.group(), "organization", "domain")
        if candidate is None:
            continue
        covered.append((match.start(), match.end()))
        found.append((match.start(), candidate))

    # Rule: @handles -> person (handles usually name a person's account).
    for match in _HANDLE_RE.finditer(text):
        if _overlaps(covered, match.start(), match.end()):
            continue
        candidate = _candidate(match.group(), "person", "handle")
        if candidate is None:
            continue
        covered.append((match.start(), match.end()))
        found.append((match.start(), candidate))

    # Rule: ALL-CAPS acronyms, 2-6 chars, outside matched spans.
    for match in _ACRONYM_RE.finditer(text):
        if _overlaps(covered, match.start(), match.end()):
            continue
        candidate = _candidate(match.group(), "other", "acronym")
        if candidate is None:
            continue
        covered.append((match.start(), match.end()))
        found.append((match.start(), candidate))

    # Rule: single capitalized tokens, only when they repeat (>=2 total
    # occurrences) and appear mid-sentence at least once.
    occurrences: dict[str, list[tuple[int, str, bool]]] = {}
    for match in _SINGLE_CAP_RE.finditer(text):
        if _overlaps(covered, match.start(), match.end()):
            continue
        normalized = normalize_entity_name(match.group())
        if not normalized or normalized in ENTITY_EXTRACTION_BLOCKLIST:
            continue
        occurrences.setdefault(normalized, []).append(
            (match.start(), match.group(), _is_sentence_initial(text, match.start()))
        )
    for spots in occurrences.values():
        if len(spots) < 2:
            continue
        if all(sentence_initial for _, _, sentence_initial in spots):
            continue
        first_position, surface, _ = spots[0]
        candidate = _candidate(surface, "other", "repeated_capitalized")
        if candidate is None:
            continue
        # One entry per occurrence so the frequency tie-break sees the
        # real in-text count; dedupe below collapses them again.
        found.extend((position, candidate) for position, _, _ in spots)

    found.sort(key=lambda item: item[0])
    best: dict[str, EntityCandidate] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for _position, candidate in found:
        counts[candidate.normalized] = counts.get(candidate.normalized, 0) + 1
        existing = best.get(candidate.normalized)
        if existing is None:
            best[candidate.normalized] = candidate
            order.append(candidate.normalized)
        elif candidate.confidence > existing.confidence:
            best[candidate.normalized] = candidate
    return tuple(replace(best[key], occurrences=counts[key]) for key in order)


def select_candidates_for_linking(
    candidates: Sequence[EntityCandidate],
    *,
    cap: int = MAX_LINKED_ENTITIES_PER_TEXT,
) -> tuple[EntityCandidate, ...]:
    """Choose which candidates may fan out into entity/edge writes.

    When a text produces more than ``cap`` candidates, survivors are
    selected by confidence (desc), then in-text occurrence count
    (desc), then first-appearance order as the deterministic tie-break
    -- NOT by first appearance alone, so a strong candidate late in a
    noisy text always beats weak early noise. The returned tuple
    preserves first-appearance order for stable linking output.
    """
    if len(candidates) <= cap:
        return tuple(candidates)
    ranked = sorted(
        range(len(candidates)),
        key=lambda index: (-candidates[index].confidence, -candidates[index].occurrences, index),
    )
    return tuple(candidates[index] for index in sorted(ranked[:cap]))


def derive_person_name_from_title(title: str) -> str | None:
    """Title-derived person name for ``person``-type memories.

    Takes the head of the title before the first separator (em dash,
    spaced hyphen, colon, comma, or opening bracket): "Jane Doe --
    Northwind intro" -> "Jane Doe". Returns None when nothing usable
    survives normalization.
    """
    head = re.split(r"—|–|\s-\s|:|,|\(|\[", title, maxsplit=1)[0].strip()
    if not head or not normalize_entity_name(head):
        return None
    return head


_REQUIRED_STORE_METHODS = (
    "find_entities_by_names",
    "get_entity_by_normalized_name",
    "create_entity",
    "record_entity_mention",
    "update_entity",
    "list_edges",
)


class EntityLinkingStore(Protocol):
    def find_entities_by_names(self, names: tuple[str, ...]) -> list[JsonObject]: ...

    def get_entity_by_normalized_name(
        self,
        entity_type: str,
        normalized_name: str,
    ) -> JsonObject | None: ...

    def create_entity(
        self,
        payload: JsonObject,
        *,
        actor_type: str,
    ) -> JsonObject: ...

    def record_entity_mention(
        self,
        *,
        entity_id: str,
        observed_at: object,
        source_id: str | None = None,
        actor_type: str,
    ) -> JsonObject: ...

    def update_entity(
        self,
        *,
        entity_id: str,
        patch: JsonObject,
        actor_type: str,
    ) -> JsonObject: ...

    def list_edges(self, *, from_id: str) -> list[JsonObject]: ...


def store_supports_entity_linking(store: object) -> TypeGuard[EntityLinkingStore]:
    """True when ``store`` exposes the entity + edge surface linking needs.

    Mirrors ``attach_memory_embedding``'s callable guard: fakes and
    reduced stores without the entity substrate silently skip linking
    instead of raising.
    """
    if not all(callable(getattr(store, name, None)) for name in _REQUIRED_STORE_METHODS):
        return False
    return callable(getattr(store, "create_edge", None)) or callable(
        getattr(store, "create_graph_edge", None)
    )


def _honorific_stripped(normalized: str) -> str | None:
    tokens = normalized.split()
    if len(tokens) >= 2 and tokens[0] in _HONORIFICS:
        return " ".join(tokens[1:])
    return None


def _entity_aliases(entity: JsonObject) -> list[str]:
    aliases = entity.get("aliases")
    if isinstance(aliases, (list, tuple)):
        return [str(alias) for alias in aliases]
    return []


class EntityLinkingService:
    """Deterministic candidate-to-entity resolution plus graph edges.

    Uses only the store's entity/edge surface; every timestamp the
    caller passes as ``observed_at`` is EVENT time (when the observation
    happened), matching the graph_edges temporal convention from the
    temporal sprint.
    """

    def __init__(
        self,
        store: object,
        *,
        actor_type: str = "system",
        actor_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.store = cast(EntityLinkingStore, store)
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.trace_id = trace_id

    # -- public linking surface ------------------------------------------------

    def link_entities_for_source(
        self, *, source_id: str, text: str, observed_at: object
    ) -> list[JsonObject]:
        return self._link(
            from_type="source",
            from_id=str(source_id),
            text=text,
            observed_at=observed_at,
            mention_source_id=str(source_id),
        )

    def link_entities_for_memory(
        self, *, memory_id: str, text: str, observed_at: object
    ) -> list[JsonObject]:
        return self._link(
            from_type="memory",
            from_id=str(memory_id),
            text=text,
            observed_at=observed_at,
            mention_source_id=None,
        )

    def link_memory_to_person(
        self, *, memory_id: str, person_name: str, observed_at: object
    ) -> JsonObject | None:
        """Ensure a person entity for ``person_name`` and an "about" edge.

        Closes the "person memory type not linked to the people/entity
        substrate" gap: the memory points at the person entity via a
        ``related_to_person`` edge whose metadata carries
        ``relation: "about"`` (the edge_type vocabulary is
        CHECK-constrained and has no literal "about").
        """
        normalized = normalize_entity_name(person_name)
        if not normalized:
            return None
        entity = self.store.get_entity_by_normalized_name("person", normalized)
        if entity is None:
            entity = self.store.create_entity(
                {
                    "entity_type": "person",
                    "name": person_name,
                    "normalized_name": normalized,
                    "first_observed_at": observed_at,
                    "last_observed_at": observed_at,
                    "mention_count": 1,
                    "metadata_json": {
                        "created_by": "vnext_entity_linker",
                        "source_rule": "person_memory_title",
                        # Type-correction surface: review flows re-type
                        # low-confidence entities in bulk off these keys.
                        "extraction_rule": "person_memory_title",
                        "extraction_confidence": 0.8,
                    },
                },
                actor_type=self.actor_type,
            )
            action = "created"
        else:
            entity = self.store.record_entity_mention(
                entity_id=str(entity["id"]),
                observed_at=observed_at,
                actor_type=self.actor_type,
            )
            action = "mentioned"
        entity_id = str(entity["id"])
        existing = self._existing_edge_keys(str(memory_id))
        edge = None
        if (entity_id, PERSON_ABOUT_EDGE_TYPE) not in existing:
            edge = self._create_edge(
                from_type="memory",
                from_id=str(memory_id),
                to_id=entity_id,
                edge_type=PERSON_ABOUT_EDGE_TYPE,
                confidence=0.8,
                explanation=f'Person memory is about "{person_name}" (title-derived).',
                observed_at=observed_at,
                metadata={"relation": "about", "source_rule": "person_memory_title"},
            )
        return {"entity": entity, "edge": edge, "action": action}

    # -- internals ---------------------------------------------------------------

    def _link(
        self,
        *,
        from_type: str,
        from_id: str,
        text: str,
        observed_at: object,
        mention_source_id: str | None,
    ) -> list[JsonObject]:
        candidates = select_candidates_for_linking(extract_entity_candidates(text))
        if not candidates:
            return []

        lookup_keys: list[str] = []
        for candidate in candidates:
            lookup_keys.append(candidate.normalized)
            fallback = _honorific_stripped(candidate.normalized)
            if fallback:
                lookup_keys.append(fallback)
        rows = self.store.find_entities_by_names(tuple(dict.fromkeys(lookup_keys)))
        # Rows arrive most-mentioned first; first writer per key wins.
        by_key: dict[str, JsonObject] = {}
        for row in rows:
            for key in (str(row["normalized_name"]), *_entity_aliases(row)):
                by_key.setdefault(key, row)

        existing_edges = self._existing_edge_keys(from_id)
        linked: list[JsonObject] = []
        for candidate in candidates:
            entity = by_key.get(candidate.normalized)
            matched_via_fallback = False
            if entity is None:
                fallback = _honorific_stripped(candidate.normalized)
                if fallback is not None:
                    entity = by_key.get(fallback)
                    matched_via_fallback = entity is not None

            if entity is not None and (str(entity["id"]), ENTITY_MENTION_EDGE_TYPE) in existing_edges:
                # Already linked to this owner: idempotent replay, no
                # double mention count, no duplicate edge.
                linked.append({"entity": entity, "edge": None, "action": "already_linked", "candidate": candidate.to_record()})
                continue

            if entity is None:
                entity = self.store.create_entity(
                    {
                        "entity_type": candidate.entity_type,
                        "name": candidate.name,
                        "normalized_name": candidate.normalized,
                        "first_observed_at": observed_at,
                        "last_observed_at": observed_at,
                        "mention_count": 1,
                        "metadata_json": {
                            "created_by": "vnext_entity_linker",
                            "source_rule": candidate.source_rule,
                            # Type-correction surface: review flows can
                            # re-type low-confidence entities in bulk by
                            # filtering on rule + confidence.
                            "extraction_rule": candidate.source_rule,
                            "extraction_confidence": candidate.confidence,
                        },
                    },
                    actor_type=self.actor_type,
                )
                action = "created"
                by_key.setdefault(str(entity["normalized_name"]), entity)
            else:
                entity = self.store.record_entity_mention(
                    entity_id=str(entity["id"]),
                    observed_at=observed_at,
                    source_id=mention_source_id,
                    actor_type=self.actor_type,
                )
                action = "mentioned"
                if matched_via_fallback:
                    entity = self._append_alias(entity, candidate.normalized)
                    by_key.setdefault(candidate.normalized, entity)

            entity_id = str(entity["id"])
            edge = self._create_edge(
                from_type=from_type,
                from_id=from_id,
                to_id=entity_id,
                edge_type=ENTITY_MENTION_EDGE_TYPE,
                confidence=candidate.confidence,
                explanation=(
                    f'Deterministic extraction rule "{candidate.source_rule}" '
                    f'matched "{candidate.name}".'
                ),
                observed_at=observed_at,
                metadata={
                    "source_rule": candidate.source_rule,
                    "surface": candidate.name,
                    "normalized": candidate.normalized,
                },
            )
            existing_edges.add((entity_id, ENTITY_MENTION_EDGE_TYPE))
            linked.append({"entity": entity, "edge": edge, "action": action, "candidate": candidate.to_record()})
        return linked

    def _append_alias(self, entity: JsonObject, alias: str) -> JsonObject:
        """Append a NORMALIZED alias when it adds resolution power."""
        aliases = _entity_aliases(entity)
        if alias == str(entity["normalized_name"]) or alias in aliases:
            return entity
        return self.store.update_entity(
            entity_id=str(entity["id"]),
            patch={"aliases": [*aliases, alias]},
            actor_type=self.actor_type,
        )

    def _existing_edge_keys(self, from_id: str) -> set[tuple[str, str]]:
        edges = self.store.list_edges(from_id=from_id)
        return {(str(edge["to_id"]), str(edge["edge_type"])) for edge in edges}

    def _create_edge(
        self,
        *,
        from_type: str,
        from_id: str,
        to_id: str,
        edge_type: str,
        confidence: float,
        explanation: str,
        observed_at: object,
        metadata: JsonObject,
    ) -> JsonObject:
        create = getattr(self.store, "create_edge", None)
        if not callable(create):
            create = getattr(self.store, "create_graph_edge")
        # Event time: when the linked observation happened; valid_from
        # starts the validity interval at the same instant (house
        # pattern from the connection finder / temporal sprint).
        resolved_observed_at = observed_at if observed_at is not None else _utc_now_iso()
        return create(
            {
                "from_type": from_type,
                "from_id": from_id,
                "to_type": "entity",
                "to_id": to_id,
                "edge_type": edge_type,
                "confidence": confidence,
                "explanation": explanation,
                "created_by": "vnext_entity_linker",
                "observed_at": resolved_observed_at,
                "valid_from": resolved_observed_at,
                "metadata_json": {
                    **metadata,
                    "trace_id": self.trace_id,
                    "generated_by": self.actor_type,
                },
            },
            actor_type=self.actor_type,
        )


__all__ = [
    "ENTITY_EXTRACTION_BLOCKLIST",
    "ENTITY_EXTRACTION_SKIP_SENSITIVITIES",
    "ENTITY_MENTION_EDGE_TYPE",
    "EntityCandidate",
    "EntityLinkingService",
    "LONG_TEXT_CHAR_THRESHOLD",
    "LONG_TEXT_SPAN_REPEAT_MINIMUM",
    "MAX_LINKED_ENTITIES_PER_TEXT",
    "PERSON_ABOUT_EDGE_TYPE",
    "RULE_CONFIDENCE",
    "derive_person_name_from_title",
    "extract_entity_candidates",
    "select_candidates_for_linking",
    "store_supports_entity_linking",
]
