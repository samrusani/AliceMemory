from __future__ import annotations

from alicebot_api.vnext_entity_names import (
    ENTITY_IMMUTABLE_PATCH_FIELDS,
    ENTITY_TYPES,
    normalize_entity_name,
)


# -- casefolding ---------------------------------------------------------------


def test_normalize_casefolds_names() -> None:
    assert normalize_entity_name("OpenAI") == "openai"
    assert normalize_entity_name("NORTHWIND CAPITAL") == "northwind capital"
    # casefold goes beyond lower(): the German sharp s folds to "ss", so
    # "Straße" and "STRASSE" resolve to the same entity.
    assert normalize_entity_name("Straße") == "strasse"
    assert normalize_entity_name("STRASSE") == "strasse"


# -- whitespace ----------------------------------------------------------------


def test_normalize_collapses_whitespace_runs_and_trims_edges() -> None:
    assert normalize_entity_name("  Sam \t  Rusani\n") == "sam rusani"
    assert normalize_entity_name("Sam Rusani") == "sam rusani"
    # Tabs, newlines, and multi-space runs all collapse to single spaces.
    assert normalize_entity_name("a\tb\nc   d") == "a b c d"


# -- punctuation edges ---------------------------------------------------------


def test_normalize_strips_punctuation_from_token_edges() -> None:
    assert normalize_entity_name('"OpenAI,"') == "openai"
    assert normalize_entity_name("(Anthropic)") == "anthropic"
    assert normalize_entity_name("Inc.") == "inc"
    assert normalize_entity_name("«Northwind»") == "northwind"
    # Bracketing dashes strip; multiple layers of edge punctuation strip too.
    assert normalize_entity_name("-alpha-") == "alpha"
    assert normalize_entity_name("Northwind.Example,") == "northwind.example"


def test_normalize_drops_tokens_that_were_pure_punctuation() -> None:
    assert normalize_entity_name("sam - rusani") == "sam rusani"
    assert normalize_entity_name("alpha -- beta") == "alpha beta"


# -- preserved internal punctuation ----------------------------------------------


def test_normalize_preserves_internal_dots_hyphens_and_apostrophes() -> None:
    assert normalize_entity_name("northwind.example") == "northwind.example"
    assert normalize_entity_name("Agent-First") == "agent-first"
    assert normalize_entity_name("O'Brien") == "o'brien"
    # A trailing dot is edge punctuation, but the internal dot survives.
    assert normalize_entity_name("northwind.example.") == "northwind.example"
    assert normalize_entity_name("jane@northwind.example") == "jane@northwind.example"


# -- empty / whitespace-only input -----------------------------------------------


def test_normalize_returns_empty_string_for_empty_and_whitespace_input() -> None:
    assert normalize_entity_name("") == ""
    assert normalize_entity_name("   ") == ""
    assert normalize_entity_name("\t\n") == ""
    # Names made only of punctuation normalize to "" so the stores' DDL
    # (normalized_name length CHECK) rejects them loudly at write time.
    assert normalize_entity_name("...") == ""
    assert normalize_entity_name("--") == ""


def test_normalize_is_idempotent() -> None:
    samples = [
        "OpenAI",
        '  "Northwind.Example,"  ',
        "Agent-First  Continuity",
        "O'Brien (advisor)",
        "...",
        "",
    ]
    for sample in samples:
        once = normalize_entity_name(sample)
        assert normalize_entity_name(once) == once


# -- shared constants -------------------------------------------------------------


def test_entity_types_cover_the_substrate_domains() -> None:
    assert "person" in ENTITY_TYPES
    assert "organization" in ENTITY_TYPES
    assert "other" in ENTITY_TYPES
    assert len(ENTITY_TYPES) == len(set(ENTITY_TYPES))


def test_immutable_patch_fields_protect_identity_and_the_resolution_key() -> None:
    assert {"id", "user_id", "entity_type", "normalized_name"} <= ENTITY_IMMUTABLE_PATCH_FIELDS
    assert {"created_at", "updated_at", "deleted_at"} <= ENTITY_IMMUTABLE_PATCH_FIELDS
    # Mutable content fields must stay patchable.
    assert "name" not in ENTITY_IMMUTABLE_PATCH_FIELDS
    assert "aliases" not in ENTITY_IMMUTABLE_PATCH_FIELDS
    assert "metadata_json" not in ENTITY_IMMUTABLE_PATCH_FIELDS
