"""Pure entity-name helpers shared by both vNext stores.

``normalize_entity_name`` produces the canonical lookup key stored in
``entities.normalized_name`` and matched by the stores'
``find_entities_by_names`` / ``get_entity_by_normalized_name`` methods.
It is deliberately dependency-free so ``vnext_store`` (Postgres) and
``sqlite_store`` (SQLite) can both import it without cycles.

Normalization rules:

- ``str.casefold()`` (aggressive lowercasing; also folds e.g. ``ß`` to
  ``ss``) so lookups are case-insensitive.
- Runs of whitespace collapse to single spaces and edges are trimmed.
- Punctuation is stripped from the EDGES of each whitespace token
  (quotes, trailing commas/periods, bracketing dashes) while INTERNAL
  punctuation is preserved, so ``"OpenAI,"`` -> ``openai`` but
  ``northwind.example`` -> ``northwind.example``, ``agent-first`` ->
  ``agent-first``, and ``o'brien`` keeps its apostrophe.
- Tokens that were pure punctuation are dropped.

A name made only of punctuation normalizes to the empty string; the
``entities`` DDL rejects empty ``normalized_name`` values, so such names
fail loudly at write time instead of colliding silently.

Aliases follow the same convention: callers are expected to store
alias values already passed through ``normalize_entity_name`` so the
one-round-trip alias matching in ``find_entities_by_names`` (exact
string equality inside the ``aliases`` JSON array) behaves like the
``normalized_name`` match.
"""

from __future__ import annotations

import unicodedata

# Allowed entities.entity_type values. The Postgres CHECK constraint
# (migration 20260705_0078) and the SQLite CHECK constraint
# (alicebot_api.sqlite_schema.ENTITY_TYPES) both mirror this tuple; the
# migration test asserts all three stay in sync.
ENTITY_TYPES = (
    "person",
    "organization",
    "project",
    "topic",
    "technology",
    "market",
    "report",
    "agent",
    "other",
)

# Fields update_entity refuses to modify on either store: row identity
# (id, user_id), the resolution key (entity_type, normalized_name --
# changing either re-identifies the entity; add an alias or create a new
# entity instead), and the lifecycle timestamps the store itself owns.
ENTITY_IMMUTABLE_PATCH_FIELDS = frozenset(
    {
        "id",
        "user_id",
        "entity_type",
        "normalized_name",
        "created_at",
        "updated_at",
        "deleted_at",
    }
)


def _is_punctuation(character: str) -> bool:
    return unicodedata.category(character).startswith("P")


def normalize_entity_name(text: str) -> str:
    """Return the canonical lookup key for an entity name."""
    folded = str(text).casefold()
    tokens: list[str] = []
    for token in folded.split():
        start = 0
        end = len(token)
        while start < end and _is_punctuation(token[start]):
            start += 1
        while end > start and _is_punctuation(token[end - 1]):
            end -= 1
        stripped = token[start:end]
        if stripped:
            tokens.append(stripped)
    return " ".join(tokens)


__all__ = [
    "ENTITY_IMMUTABLE_PATCH_FIELDS",
    "ENTITY_TYPES",
    "normalize_entity_name",
]
