from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_entities import (
    ENTITY_EXTRACTION_BLOCKLIST,
    ENTITY_EXTRACTION_SKIP_SENSITIVITIES,
    ENTITY_MENTION_EDGE_TYPE,
    LONG_TEXT_CHAR_THRESHOLD,
    LONG_TEXT_SPAN_REPEAT_MINIMUM,
    MAX_LINKED_ENTITIES_PER_TEXT,
    PERSON_ABOUT_EDGE_TYPE,
    RULE_CONFIDENCE,
    EntityCandidate,
    EntityLinkingService,
    derive_person_name_from_title,
    extract_entity_candidates,
    select_candidates_for_linking,
    store_supports_entity_linking,
)
from alicebot_api.vnext_entity_names import ENTITY_TYPES


OBSERVED_AT = "2026-07-01T10:00:00Z"
LATER_OBSERVED_AT = "2026-07-02T09:30:00Z"
EARLIER_OBSERVED_AT = "2026-06-20T08:00:00Z"


# -- extraction: rule by rule -----------------------------------------------------


def _by_normalized(text: str) -> dict[str, object]:
    return {candidate.normalized: candidate for candidate in extract_entity_candidates(text)}


# Behavior change (LongMemEval noise fix): a bare "First Last" span is
# no longer positive evidence of a person — brand names share the exact
# same shape. Without honorific/context evidence the span defaults to
# 'other' via the lower-confidence capitalized_span_default rule.
def test_bare_two_token_span_defaults_to_other_not_person() -> None:
    candidates = extract_entity_candidates("Yesterday Jane Doe shipped the retrieval fix.")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name == "Jane Doe"
    assert candidate.normalized == "jane doe"
    assert candidate.entity_type == "other"
    assert candidate.source_rule == "capitalized_span_default"
    assert candidate.confidence == RULE_CONFIDENCE["capitalized_span_default"]


def test_relational_context_promotes_two_token_span_to_person() -> None:
    for text in (
        "Met with Jane Doe about the fund strategy.",  # "with X" cue before
        "Met Jane Doe at the offsite.",  # "met X" cue before
        "Dinner with my friend Jane Doe tomorrow.",  # relational noun before
        "Jane Doe said the round is closing.",  # "X said" cue directly after
        "Jane Doe told me the round is closing.",  # "X told" cue directly after
    ):
        candidates = extract_entity_candidates(text)
        assert len(candidates) == 1, text
        assert candidates[0].normalized == "jane doe", text
        assert candidates[0].entity_type == "person", text
        assert candidates[0].source_rule == "capitalized_span", text
        assert candidates[0].confidence == RULE_CONFIDENCE["capitalized_span"], text


def test_brandish_two_token_spans_are_not_typed_person() -> None:
    candidates = _by_normalized(
        "I bought a hoodie from Street Threads, browsed Hype Street, "
        "and returned jeans at Urban Edge before joining Culture Club."
    )

    for key in ("street threads", "hype street", "urban edge", "culture club"):
        assert candidates[key].entity_type == "other", key
        assert candidates[key].source_rule == "capitalized_span_default", key
        assert candidates[key].confidence == RULE_CONFIDENCE["capitalized_span_default"], key


def test_org_suffix_span_guesses_organization() -> None:
    # "met X" is a person cue, but org-suffix evidence is checked first.
    candidates = _by_normalized("We met Northwind Capital and Redwood Labs about the raise.")

    assert candidates["northwind capital"].entity_type == "organization"
    assert candidates["redwood labs"].entity_type == "organization"


def test_group_and_systems_suffixes_guess_organization() -> None:
    candidates = _by_normalized("Halcyon Group and Verdant Systems signed the pilot.")

    assert candidates["halcyon group"].entity_type == "organization"
    assert candidates["verdant systems"].entity_type == "organization"


def test_honorific_span_guesses_person_even_with_three_tokens() -> None:
    candidates = extract_entity_candidates("They invited Dr Jane Doe to speak.")

    assert len(candidates) == 1
    assert candidates[0].normalized == "dr jane doe"
    assert candidates[0].entity_type == "person"


def test_three_token_span_without_suffix_or_honorific_guesses_other() -> None:
    candidates = extract_entity_candidates("The Alice Continuity Kernel handles memory writes.")

    assert len(candidates) == 1
    # Leading blocklisted "The" is stripped from the span.
    assert candidates[0].name == "Alice Continuity Kernel"
    assert candidates[0].entity_type == "other"


def test_all_caps_acronym_extracts_between_2_and_6_chars() -> None:
    candidates = _by_normalized("The MCP server uses RRF fusion but not HTTPX yet.")

    assert candidates["mcp"].source_rule == "acronym"
    assert candidates["mcp"].entity_type == "other"
    assert candidates["mcp"].confidence == RULE_CONFIDENCE["acronym"]
    assert "rrf" in candidates
    assert "httpx" in candidates


def test_single_letter_and_overlong_caps_are_not_acronyms() -> None:
    candidates = _by_normalized("Grade A material for the ALICEBOTKERNEL run.")

    assert "a" not in candidates
    assert "alicebotkernel" not in candidates


def test_handle_extracts_person_and_skips_email_addresses() -> None:
    candidates = _by_normalized("Ping @janedoe, not jane@northwind.example, when it lands.")

    assert candidates["janedoe"].name == "@janedoe"
    assert candidates["janedoe"].entity_type == "person"
    assert candidates["janedoe"].source_rule == "handle"
    # The email's local part never becomes a handle; its domain still
    # resolves through the domain rule.
    assert candidates["northwind.example"].source_rule == "domain"


def test_bare_domain_extracts_organization_and_skips_file_suffixes() -> None:
    candidates = _by_normalized("Read northwind.example before editing notes.md or app.py today.")

    assert candidates["northwind.example"].entity_type == "organization"
    assert candidates["northwind.example"].confidence == RULE_CONFIDENCE["domain"]
    assert "notes.md" not in candidates
    assert "app.py" not in candidates


def test_numeric_dotted_values_are_not_domains() -> None:
    assert extract_entity_candidates("pi is 3.14 and the build is v1.2") == ()


def test_repeated_single_capitalized_token_requires_two_occurrences() -> None:
    none_found = extract_entity_candidates("Ask Hermes about the deploy window tomorrow.")
    repeated = extract_entity_candidates(
        "Hermes shipped the fix today. Everyone later thanked Hermes for it."
    )

    assert none_found == ()
    assert len(repeated) == 1
    assert repeated[0].normalized == "hermes"
    assert repeated[0].source_rule == "repeated_capitalized"
    assert repeated[0].confidence == RULE_CONFIDENCE["repeated_capitalized"]


def test_sentence_initial_only_single_words_are_skipped() -> None:
    # Both occurrences start a sentence: ordinary English capitalization,
    # not evidence of an entity.
    candidates = extract_entity_candidates("Hermes shipped the fix. Hermes left early.")

    assert candidates == ()


def test_blocklist_drops_weekday_month_and_sentence_starters() -> None:
    for token in ("monday", "january", "the", "this"):
        assert token in ENTITY_EXTRACTION_BLOCKLIST

    candidates = extract_entity_candidates(
        "This happened on Monday. Monday was rough. January was better than December January."
    )

    assert candidates == ()


def test_blocklisted_edge_tokens_are_stripped_from_spans() -> None:
    candidates = extract_entity_candidates("The Alice Core launch happens after The Alice Core review.")

    assert [candidate.name for candidate in candidates] == ["Alice Core"]


def test_span_occurrences_do_not_double_count_into_the_single_token_rule() -> None:
    candidates = extract_entity_candidates(
        "Jane Doe wrote the plan. Later Jane Doe revised the plan."
    )

    assert [candidate.normalized for candidate in candidates] == ["jane doe"]


def test_empty_and_blank_text_yield_no_candidates() -> None:
    assert extract_entity_candidates("") == ()
    assert extract_entity_candidates("   \n\t  ") == ()


def test_all_confidences_stay_inside_the_documented_band() -> None:
    assert set(RULE_CONFIDENCE) == {
        "capitalized_span",
        "capitalized_span_default",
        "domain",
        "handle",
        "acronym",
        "repeated_capitalized",
    }
    for confidence in RULE_CONFIDENCE.values():
        assert 0.5 <= confidence <= 0.8
    # Evidence-less spans must rank below evidence-backed ones so cap
    # selection and future re-typing flows can target them.
    assert RULE_CONFIDENCE["capitalized_span_default"] < RULE_CONFIDENCE["capitalized_span"]


def test_entity_type_guesses_are_valid_store_entity_types() -> None:
    candidates = extract_entity_candidates(
        "Dr Jane Doe of Northwind Capital pinged @hermes about MCP via northwind.example. "
        "Hermes agreed and later Hermes confirmed."
    )

    assert candidates
    for candidate in candidates:
        assert candidate.entity_type in ENTITY_TYPES


def test_private_or_stricter_sensitivities_are_in_the_skip_set() -> None:
    for sensitivity in ("private", "secret", "confidential", "highly_sensitive", "sacred", "regulated"):
        assert sensitivity in ENTITY_EXTRACTION_SKIP_SENSITIVITIES
    for sensitivity in ("public", "internal", "unknown"):
        assert sensitivity not in ENTITY_EXTRACTION_SKIP_SENSITIVITIES


# -- volume guards: long-text repeat threshold + linking cap ----------------------


_LOWERCASE_FILLER = "plain lowercase filler about budgets and vendor timelines. " * 30


def test_long_text_multi_token_spans_require_repeat() -> None:
    once = _LOWERCASE_FILLER + "We saw Quiet Storm at the show."
    twice = _LOWERCASE_FILLER + "Quiet Storm opened the show. The crowd loved Quiet Storm."
    assert len(once) > LONG_TEXT_CHAR_THRESHOLD

    assert extract_entity_candidates(once) == ()
    repeated = extract_entity_candidates(twice)
    assert [candidate.normalized for candidate in repeated] == ["quiet storm"]
    assert repeated[0].occurrences == LONG_TEXT_SPAN_REPEAT_MINIMUM


def test_short_text_keeps_single_mention_capture() -> None:
    short = "We saw Quiet Storm at the show."
    assert len(short) <= LONG_TEXT_CHAR_THRESHOLD

    candidates = extract_entity_candidates(short)
    assert [candidate.normalized for candidate in candidates] == ["quiet storm"]


def test_dropped_long_text_span_does_not_resurface_as_single_tokens() -> None:
    # "Storm" occurs once inside the below-threshold span and once
    # alone: the span occurrence must stay masked, leaving the single
    # token under its own repeat threshold.
    text = _LOWERCASE_FILLER + "We saw Quiet Storm on stage. Fans praised Storm loudly."
    assert len(text) > LONG_TEXT_CHAR_THRESHOLD

    assert extract_entity_candidates(text) == ()


def test_cap_selection_prefers_confidence_then_frequency_then_order() -> None:
    def _make(normalized: str, confidence: float, occurrences: int) -> EntityCandidate:
        return EntityCandidate(
            name=normalized.title(),
            normalized=normalized,
            entity_type="other",
            confidence=confidence,
            source_rule="acronym",
            occurrences=occurrences,
        )

    weak = [_make(f"weak{index:02d}", 0.6, 1) for index in range(24)]
    frequent = _make("frequent", 0.6, 5)
    strong = _make("strong span", 0.75, 1)
    candidates = [*weak, frequent, strong]  # strongest candidates appear LAST

    survivors = select_candidates_for_linking(candidates, cap=25)

    names = [candidate.normalized for candidate in survivors]
    assert len(names) == 25
    # Confidence beats frequency beats first-appearance; the weakest
    # (last equal-confidence, single-occurrence) candidate is dropped.
    assert "strong span" in names
    assert "frequent" in names
    assert "weak23" not in names
    # Survivors come back in first-appearance order.
    assert names[-2:] == ["frequent", "strong span"]

    # At or under the cap, input passes through untouched.
    assert select_candidates_for_linking(candidates[:3], cap=25) == tuple(candidates[:3])


def test_default_linking_cap_is_25() -> None:
    assert MAX_LINKED_ENTITIES_PER_TEXT == 25


# The LongMemEval-shaped regression: a ~1.7k-char brand-heavy shopping
# note. Pre-fix, extraction returned every brand PLUS the one-off span,
# all typed 'person' (the exactly-2-capitalized-tokens heuristic). Now
# only the repeated brands survive, none typed person.
_BRAND_HAYSTACK = (
    "I've been shopping around for a fall wardrobe refresh and wanted to keep notes. "
    "I checked out Street Threads yesterday and their hoodies looked great, though the "
    "prices at Street Threads run higher than I remembered from last season. The staff "
    "were helpful and the fitting rooms were clean, which honestly matters a lot to me. "
    "After that I walked over to Hype Street because they had a sale banner in the "
    "window. Most of what Hype Street stocks is streetwear basics, and I grabbed two "
    "tees and a cap from the clearance rack near the back of the store. "
    "For jeans I usually go to Urban Edge since their slim cuts fit me best, and the "
    "denim wall at Urban Edge had a buy-one-get-one deal running through the weekend. "
    "I also browsed Culture Club for the first time; a coworker keeps recommending it. "
    "The Culture Club loyalty program gives points on every purchase, which could add "
    "up quickly if I keep shopping there for basics and accessories through winter. "
    "They also teased a Winter Capsule drop next month, but no date was confirmed yet. "
    "Overall the loyalty deal at Culture Club seems better than what Street Threads "
    "offers, and Urban Edge still wins on fit. Hype Street is the cheapest of the "
    "four by a wide margin, but the stitching on their tees felt thin and the sizing "
    "ran small on everything I tried, so I would size up next time for sure. "
    "Budget-wise I want to stay under four hundred for the whole refresh, including "
    "shoes, which probably means skipping the leather jacket until the January sales. "
    "Next weekend I plan to compare return policies and shipping times before deciding "
    "where the bulk of the order goes, and I will update these notes after that trip."
)


def test_longmemeval_brand_haystack_yields_few_non_person_entities() -> None:
    assert len(_BRAND_HAYSTACK) > LONG_TEXT_CHAR_THRESHOLD

    candidates = extract_entity_candidates(_BRAND_HAYSTACK)

    names = {candidate.normalized for candidate in candidates}
    # Only the four repeated brands; the one-off "Winter Capsule" span
    # is below the long-text repeat threshold.
    assert names == {"street threads", "hype street", "urban edge", "culture club"}
    assert all(candidate.entity_type == "other" for candidate in candidates)
    assert all(candidate.source_rule == "capitalized_span_default" for candidate in candidates)
    assert all(
        candidate.occurrences >= LONG_TEXT_SPAN_REPEAT_MINIMUM for candidate in candidates
    )


def test_derive_person_name_from_title_takes_the_head_before_separators() -> None:
    assert derive_person_name_from_title("Jane Doe") == "Jane Doe"
    assert derive_person_name_from_title("Jane Doe — Northwind intro") == "Jane Doe"
    assert derive_person_name_from_title("Jane Doe: GP at Northwind") == "Jane Doe"
    assert derive_person_name_from_title("Jane Doe, investor") == "Jane Doe"
    assert derive_person_name_from_title("Jean-Luc Picard - captain") == "Jean-Luc Picard"
    assert derive_person_name_from_title("...") is None
    assert derive_person_name_from_title("") is None


# -- linking service on live sqlite ------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    yield connection
    connection.close()


def _store(conn: sqlite3.Connection, email: str = "owner@example.com") -> SQLiteVNextStore:
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, email)
    return SQLiteVNextStore(conn, user_id)


def _source(store: SQLiteVNextStore, title: str = "Note") -> str:
    row = store.create_source(
        {
            "source_type": "manual_text",
            "title": title,
            "content_hash": f"sha256:{uuid4().hex}",
            "domain": "professional",
            "sensitivity": "internal",
        }
    )
    return str(row["id"])


def test_store_support_guard_accepts_sqlite_store_and_rejects_bare_objects(conn) -> None:
    assert store_supports_entity_linking(_store(conn)) is True
    assert store_supports_entity_linking(object()) is False


def test_linking_creates_new_entities_with_observation_window_and_mention_edges(conn) -> None:
    store = _store(conn)
    source_id = _source(store)
    service = EntityLinkingService(store)

    linked = service.link_entities_for_source(
        source_id=source_id,
        text="We met Jane Doe of Northwind Capital.",
        observed_at=OBSERVED_AT,
    )

    assert [record["action"] for record in linked] == ["created", "created"]
    person = store.get_entity_by_normalized_name("person", "jane doe")
    org = store.get_entity_by_normalized_name("organization", "northwind capital")
    assert person is not None and org is not None
    assert person["mention_count"] == 1
    assert person["first_observed_at"] == OBSERVED_AT
    assert person["last_observed_at"] == OBSERVED_AT
    # Type-correction surface: created entities carry their extraction
    # rule + confidence so review flows can re-type in bulk.
    for entity in (person, org):
        assert entity["metadata_json"]["extraction_rule"] == "capitalized_span"
        assert entity["metadata_json"]["extraction_confidence"] == RULE_CONFIDENCE["capitalized_span"]

    edges = store.list_edges(from_id=source_id)
    assert {(str(edge["to_id"]), str(edge["edge_type"])) for edge in edges} == {
        (str(person["id"]), ENTITY_MENTION_EDGE_TYPE),
        (str(org["id"]), ENTITY_MENTION_EDGE_TYPE),
    }
    for edge in edges:
        assert edge["from_type"] == "source"
        assert edge["to_type"] == "entity"
        assert edge["observed_at"] == OBSERVED_AT
        assert edge["valid_from"] == OBSERVED_AT
        assert edge["created_by"] == "vnext_entity_linker"


def test_second_source_records_mention_and_widens_the_observation_window(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    first_source = _source(store, "First")
    second_source = _source(store, "Second")

    service.link_entities_for_source(
        source_id=first_source, text="Met Jane Doe about the plan.", observed_at=OBSERVED_AT
    )
    linked = service.link_entities_for_source(
        source_id=second_source, text="Spoke with Jane Doe again.", observed_at=LATER_OBSERVED_AT
    )

    assert [record["action"] for record in linked] == ["mentioned"]
    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity["mention_count"] == 2
    assert entity["first_observed_at"] == OBSERVED_AT
    assert entity["last_observed_at"] == LATER_OBSERVED_AT
    # One mentions edge per owning source.
    assert len(store.list_edges(to_id=str(entity["id"]))) == 2


def test_out_of_order_observation_only_widens_the_window_backwards(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    service.link_entities_for_source(
        source_id=_source(store), text="Met Jane Doe about shipping.", observed_at=OBSERVED_AT
    )

    service.link_entities_for_source(
        source_id=_source(store), text="Met Jane Doe for planning.", observed_at=EARLIER_OBSERVED_AT
    )

    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity["first_observed_at"] == EARLIER_OBSERVED_AT
    assert entity["last_observed_at"] == OBSERVED_AT


def test_relinking_the_same_source_is_idempotent(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    source_id = _source(store)
    text = "We met Jane Doe of Northwind Capital."

    service.link_entities_for_source(source_id=source_id, text=text, observed_at=OBSERVED_AT)
    replay = service.link_entities_for_source(source_id=source_id, text=text, observed_at=LATER_OBSERVED_AT)

    assert [record["action"] for record in replay] == ["already_linked", "already_linked"]
    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity["mention_count"] == 1
    assert entity["last_observed_at"] == OBSERVED_AT
    assert len(store.list_edges(from_id=source_id)) == 2


def test_honorific_variant_matches_existing_entity_and_appends_alias(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    service.link_entities_for_source(
        source_id=_source(store), text="Met with Jane Doe on the call.", observed_at=OBSERVED_AT
    )

    linked = service.link_entities_for_source(
        source_id=_source(store), text="Dr Jane Doe presented.", observed_at=LATER_OBSERVED_AT
    )

    assert [record["action"] for record in linked] == ["mentioned"]
    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity["mention_count"] == 2
    assert entity["aliases"] == ["dr jane doe"]
    # No second entity was created for the honorific variant.
    assert store.get_entity_by_normalized_name("person", "dr jane doe") is None

    # A third variant occurrence resolves through the alias without
    # duplicating it.
    service.link_entities_for_source(
        source_id=_source(store), text="Dr Jane Doe closed the round.", observed_at=LATER_OBSERVED_AT
    )
    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity["mention_count"] == 3
    assert entity["aliases"] == ["dr jane doe"]


def test_memory_linking_creates_memory_to_entity_edges(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    memory = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": "Chatted with Jane Doe about async standups."},
            "status": "active",
            "memory_type": "semantic",
            "title": "Standup preference",
            "canonical_text": "Chatted with Jane Doe about async standups.",
        }
    )

    linked = service.link_entities_for_memory(
        memory_id=str(memory["id"]),
        text=str(memory["canonical_text"]),
        observed_at=OBSERVED_AT,
    )

    assert [record["action"] for record in linked] == ["created"]
    edges = store.list_edges(from_id=str(memory["id"]))
    assert len(edges) == 1
    assert edges[0]["from_type"] == "memory"
    assert edges[0]["edge_type"] == ENTITY_MENTION_EDGE_TYPE
    assert edges[0]["observed_at"] == OBSERVED_AT


def test_linking_caps_writes_and_keeps_the_high_confidence_candidate(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    source_id = _source(store)
    # 30 low-confidence acronyms (0.60) separated by lowercase filler,
    # then one high-confidence org span (0.75) at the very END of the
    # text: first-appearance capping would drop it.
    acronyms = [f"Z{chr(65 + index // 26)}{chr(65 + index % 26)}" for index in range(30)]
    text = "then " + " then ".join(acronyms) + " happened. Later Northwind Capital funded it."

    linked = service.link_entities_for_source(source_id=source_id, text=text, observed_at=OBSERVED_AT)

    assert len(linked) == MAX_LINKED_ENTITIES_PER_TEXT
    assert store.get_entity_by_normalized_name("organization", "northwind capital") is not None
    assert len(store.list_edges(from_id=source_id)) == MAX_LINKED_ENTITIES_PER_TEXT
    assert len(store.list_entities(limit=100)) == MAX_LINKED_ENTITIES_PER_TEXT


def test_link_memory_to_person_creates_person_entity_and_about_edge(conn) -> None:
    store = _store(conn)
    service = EntityLinkingService(store)
    memory = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {},
            "status": "active",
            "memory_type": "person",
            "title": "Jane Doe",
            "canonical_text": "GP at Northwind Capital.",
        }
    )

    result = service.link_memory_to_person(
        memory_id=str(memory["id"]), person_name="Jane Doe", observed_at=OBSERVED_AT
    )
    replay = service.link_memory_to_person(
        memory_id=str(memory["id"]), person_name="Jane Doe", observed_at=LATER_OBSERVED_AT
    )

    assert result["action"] == "created"
    assert result["edge"] is not None
    entity = store.get_entity_by_normalized_name("person", "jane doe")
    assert entity is not None
    assert entity["metadata_json"]["extraction_rule"] == "person_memory_title"
    assert entity["metadata_json"]["extraction_confidence"] == 0.8
    edges = [
        edge
        for edge in store.list_edges(from_id=str(memory["id"]))
        if str(edge["edge_type"]) == PERSON_ABOUT_EDGE_TYPE
    ]
    assert len(edges) == 1
    assert edges[0]["metadata_json"]["relation"] == "about"
    assert edges[0]["observed_at"] == OBSERVED_AT
    # Replay records the mention but never duplicates the edge.
    assert replay["action"] == "mentioned"
    assert replay["edge"] is None
    assert entity["id"] == store.get_entity_by_normalized_name("person", "jane doe")["id"]


def test_linking_is_isolated_between_users_sharing_a_database(conn) -> None:
    store_a = _store(conn, "a@example.com")
    store_b = _store(conn, "b@example.com")
    source_a = _source(store_a)

    EntityLinkingService(store_a).link_entities_for_source(
        source_id=source_a, text="We met Jane Doe of Northwind Capital.", observed_at=OBSERVED_AT
    )

    assert store_b.list_entities() == []
    assert store_b.list_edges(from_id=source_a) == []

    # User B linking the same names creates B-scoped rows, not shared ones.
    source_b = _source(store_b)
    EntityLinkingService(store_b).link_entities_for_source(
        source_id=source_b, text="We met Jane Doe of Northwind Capital.", observed_at=OBSERVED_AT
    )
    entity_a = store_a.get_entity_by_normalized_name("person", "jane doe")
    entity_b = store_b.get_entity_by_normalized_name("person", "jane doe")
    assert entity_a is not None and entity_b is not None
    assert entity_a["id"] != entity_b["id"]
    assert entity_a["mention_count"] == 1
    assert entity_b["mention_count"] == 1
