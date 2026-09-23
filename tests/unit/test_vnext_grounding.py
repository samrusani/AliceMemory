"""Tests for query-entity grounding (``vnext_grounding``).

The hard requirements under test:

- Salience is CONSERVATIVE and query-surface only: generic nouns,
  blocklisted words, sentence-initial capitals, and acronyms are never
  flagged; no code path sees benchmark labels.
- Support errs toward NOT flagging: entity-substrate hit OR a one-row
  FTS probe hit (chunks or memories, ``match_any``) counts as support,
  on both store backends.
- ``pack["grounding"]`` exists ONLY when at least one salient entity
  has zero corpus support; every ungated query takes the byte-identical
  old path (no pack key, no trace key, zero probe calls).
"""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_grounding import (
    MAX_GROUNDING_ENTITIES,
    compute_query_grounding,
    corpus_support,
    salient_query_entities,
)
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore


# -- salience: conservatism first ---------------------------------------------------


def test_generic_queries_have_no_salient_entities() -> None:
    for query in (
        "what did I have for dinner last week?",
        "how many books did I read this year?",
        "Did I ever mention my favorite restaurant?",
        "What was the total cost of my home renovation?",
        "When is my next dentist appointment?",
        "What did I do on Monday?",  # weekday: blocklisted
        "Remind me tomorrow about the gym.",  # sentence-initial capital only
        "",
        "   ",
    ):
        assert salient_query_entities(query) == (), query


def test_acronyms_are_never_salient() -> None:
    # Common-noun acronyms saturate questions; the acronym rule is
    # deliberately excluded from grounding salience.
    assert salient_query_entities("Which TV show did I binge on the flight?") == ()
    assert salient_query_entities("What was my GPS route to the gym?") == ()


def test_sentence_initial_single_capital_is_not_salient() -> None:
    assert salient_query_entities("Lisbon plans?") == ()
    assert salient_query_entities("Biscuit chewed the couch. Biscuit again!") == ()


def test_mid_sentence_single_capital_is_salient() -> None:
    assert salient_query_entities("When did I visit Lisbon with my sister?") == ("Lisbon",)


def test_capitalized_span_is_salient_and_leading_interrogative_is_stripped() -> None:
    assert salient_query_entities("Did Marcus Chen email me about the offsite?") == ("Marcus Chen",)
    assert salient_query_entities("I met Marcus Chen at the fund dinner, right?") == ("Marcus Chen",)


def test_quoted_title_is_salient_only_with_a_capital_letter() -> None:
    assert salient_query_entities('Have I read "Sapiens" yet?') == ("Sapiens",)
    assert salient_query_entities("Did I finish “The Name of the Wind”?") == ("The Name of the Wind",)
    # Lowercase quoted phrases are ordinary emphasis, not names.
    assert salient_query_entities('Did I ever say "the usual order" to the barista?') == ()


def test_single_quoted_title_is_salient_but_possessives_never_open_a_span() -> None:
    assert salient_query_entities("How many pages are left in 'Sapiens'?") == ("Sapiens",)
    # Possessive apostrophes must not pair up into a phantom quoted span.
    assert salient_query_entities("what's in the neighbors' shed or the kids' room?") == ()


def test_honorific_names_yield_the_surname_never_the_bare_honorific() -> None:
    assert salient_query_entities("How often do I see Dr. Johnson?") == ("Johnson",)
    assert salient_query_entities("How often do I see my therapist, Dr. Smith?") == ("Smith",)
    assert salient_query_entities("What did I promise Mrs. Thompson last week?") == ("Thompson",)


def test_contractions_possessives_and_weekday_plurals_are_handled() -> None:
    # "I'm"/"I've" are pronoun contractions, not names.
    assert salient_query_entities("I was thinking, but I'm not sure what I've planned.") == ()
    # Possessive names shed the suffix; the name itself is salient.
    assert salient_query_entities("How many of my friend Emma's recipes have I tried?") == ("Emma",)
    # Pluralized blocklist words (weekdays) are ordinary schedule talk.
    assert salient_query_entities("How much earlier do I wake up on Fridays than on Tuesdays?") == ()


def test_domain_and_dedupe_and_appearance_order() -> None:
    names = salient_query_entities("Did Marcus Chen move northwind.example to Lisbon? Ask Marcus Chen.")
    assert names == ("Marcus Chen", "northwind.example", "Lisbon")


# -- salience: attribute-qualified lowercase compounds (round 3) ---------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # quantifier-qualified: a measured quantity names a specific thing
        ("How many fish are there in my 30-gallon tank?", ("30-gallon tank",)),
        ("Is the 10 gallon aquarium cycling yet?", ("10 gallon aquarium",)),
        ("Where did I put the 5-pound dumbbell?", ("5-pound dumbbell",)),
        # possessive noun-noun compounds with curated heads
        ("How often do I water my snake plant?", ("snake plant",)),
        ("When is my soccer team playing next?", ("soccer team",)),
        ("Did I skip my karate practice last week?", ("karate practice",)),
        ("Has our book club picked the next read?", ("book club",)),
        ("When does my pottery class meet?", ("pottery class",)),
    ],
)
def test_qualified_lowercase_compounds_are_salient(query: str, expected: tuple[str, ...]) -> None:
    assert salient_query_entities(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        # bare lowercase nouns: no qualifier shape, never salient
        "What is the name of my hamster?",
        "the fish tank at the office needs cleaning",
        "did I clean the tank yesterday?",
        # possessive + generic object talk: head not in the curated lexicon
        "Did my credit card payment go through?",
        "What is my phone number?",
        "Where did I buy my new tennis racket from?",
        "what did I write in my journal entry?",
        # generic/descriptive modifiers never qualify
        "How is my work team doing?",
        "my favorite team lost again",
        "when does my new class start?",
        "is my old plant still alive?",
        "did my first practice go okay?",
        "our local club meets on Mondays",
        # time-quantified events are not things ("5-day trip")
        "How many shirts did I pack for my 5-day trip?",
        "I went on a 30-minute walk before lunch.",
        # unit words without the full quantity-unit-noun shape
        "How many miles per gallon was my car getting?",
        "I bought a gallon of milk and a pound of coffee.",
        # blocklisted or stopword modifiers can never qualify
        "what about my may lessons?",
        "did my the team win?",
    ],
)
def test_generic_lowercase_nouns_are_never_salient(query: str) -> None:
    assert salient_query_entities(query) == ()


def test_salient_entities_are_capped() -> None:
    query = (
        "Did Alice Chen, Bob Marley, Carol Danvers, Dave Grohl, "
        "Erin Brock, and Frank Ocean attend the dinner?"
    )
    names = salient_query_entities(query)
    assert len(names) == MAX_GROUNDING_ENTITIES
    assert names[0] == "Alice Chen"


# -- corpus support: sqlite backend --------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    yield connection
    connection.close()


def _store(conn: sqlite3.Connection) -> SQLiteVNextStore:
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Grounding Test")
    return SQLiteVNextStore(conn, user_id)


def _seed_chunk(store: SQLiteVNextStore, text: str) -> None:
    source = store.create_source(
        {
            "source_type": "chat_session",
            "title": "Session",
            "content_hash": f"sha256:{uuid4().hex}",
            "domain": "unknown",
            "sensitivity": "internal",
        }
    )
    store.create_source_chunk(
        {"source_id": str(source["id"]), "chunk_index": 0, "text": text}
    )


def _seed_memory(store: SQLiteVNextStore, text: str) -> None:
    store.create_memory(
        {
            "memory_key": f"fact.{uuid4().hex[:8]}",
            "memory_type": "semantic",
            "title": text[:40],
            "canonical_text": text,
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "value": {"text": text},
        }
    )


def test_corpus_support_entity_table_chunks_and_memories_on_sqlite(conn) -> None:
    store = _store(conn)
    store.create_entity(
        {"entity_type": "person", "name": "Marcus Chen", "normalized_name": "marcus chen"}
    )
    _seed_chunk(store, "we talked about the book Sapiens over coffee")
    _seed_memory(store, "Fact: the user loved visiting Lisbon in spring.")

    support = corpus_support(("Marcus Chen", "Sapiens", "Lisbon", "Zorblatt Nine"), store)

    assert support == {
        "Marcus Chen": True,  # entity substrate
        "Sapiens": True,  # chunk FTS probe
        "Lisbon": True,  # memory FTS probe
        "Zorblatt Nine": False,  # every check missed
    }


def test_corpus_support_matches_entity_aliases_on_sqlite(conn) -> None:
    store = _store(conn)
    store.create_entity(
        {
            "entity_type": "person",
            "name": "Jane Doe",
            "normalized_name": "jane doe",
            "aliases": ["dr jane doe"],
        }
    )
    support = corpus_support(("Dr Jane Doe",), store)
    assert support == {"Dr Jane Doe": True}


def test_partial_token_mention_counts_as_support_on_sqlite(conn) -> None:
    # match_any probe: "Marcus" alone supports "Marcus Chen" -- the
    # conservative direction, because a false "unmentioned" claim is the
    # failure mode grounding exists to avoid.
    store = _store(conn)
    _seed_chunk(store, "Marcus recommended the tapas place downtown")
    support = corpus_support(("Marcus Chen",), store)
    assert support == {"Marcus Chen": True}


def test_morphological_variant_counts_as_support_on_sqlite(conn) -> None:
    # FTS matches whole tokens: a corpus that says "Hawaiian" must still
    # support "Hawaii" (and the reverse), or the note would be
    # lexically true yet semantically false -- the harmful direction.
    store = _store(conn)
    _seed_chunk(store, "the Hawaiian resort charged 300 a night")
    assert corpus_support(("Hawaii",), store) == {"Hawaii": True}

    reverse = _store(conn)
    _seed_chunk(reverse, "our week in Hawaii was rainy")
    assert corpus_support(("Hawaiian",), reverse) == {"Hawaiian": True}


def test_pure_number_tokens_never_fabricate_support_on_sqlite(conn) -> None:
    # "30" on an unrelated receipt is not a mention of the 30-gallon
    # tank; a bare-number hit must not silently suppress a truthful note.
    store = _store(conn)
    _seed_chunk(store, "the receipt total was 30 dollars at the market")
    assert corpus_support(("30-gallon tank",), store) == {"30-gallon tank": False}


def test_word_tokens_of_a_quantified_compound_still_count_as_support(conn) -> None:
    # Any word-token variant hit suppresses the note -- the safe direction.
    store = _store(conn)
    _seed_chunk(store, "cleaned the tank filter after feeding the fish")
    assert corpus_support(("30-gallon tank",), store) == {"30-gallon tank": True}

    unit_only = _store(conn)
    _seed_chunk(unit_only, "bought two gallons of water for the trip")
    assert corpus_support(("30-gallon tank",), unit_only) == {"30-gallon tank": True}


def test_all_number_names_keep_their_digit_probe_surface(conn) -> None:
    # A purely numeric name has no word tokens; its digits remain the
    # only probe surface, so a corpus mention still counts as support.
    store = _store(conn)
    _seed_chunk(store, "the 991 arrived at the dealership on Friday")
    assert corpus_support(("991",), store) == {"991": True}


def test_corpus_support_empty_and_uncheckable_inputs(conn) -> None:
    assert corpus_support((), _store(conn)) == {}
    # A bare object exposes no probe surface: "cannot check", never a claim.
    assert corpus_support(("Sapiens",), object()) is None


# -- corpus support: postgres backend (recorded cursor, canned rows) ---------------


class _RoutingCursor:
    """Routes fetchall by the table in the last executed statement."""

    def __init__(self, results_by_marker: dict[str, list[dict[str, Any]]]) -> None:
        self.results_by_marker = results_by_marker
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self._last_query = ""

    def __enter__(self) -> "_RoutingCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        if params is not None:
            assert query.count("%s") == len(params)
        self.executed.append((query, params))
        self._last_query = query

    def fetchone(self) -> dict[str, Any] | None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        for marker, rows in self.results_by_marker.items():
            if marker in self._last_query:
                return rows
        return []


class _RoutingConnection:
    def __init__(self, cursor: _RoutingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RoutingCursor:
        return self._cursor


def test_corpus_support_routes_through_postgres_store_surface() -> None:
    cursor = _RoutingCursor(
        {
            "FROM vnext_entities": [
                {"id": str(uuid4()), "normalized_name": "marcus chen", "aliases": []}
            ],
            "FROM source_chunks_fts": [],  # sqlite-only marker, never hit
            "FROM source_chunks": [],
            "FROM memories": [],
        }
    )
    store = PostgresVNextStore(_RoutingConnection(cursor))

    support = corpus_support(("Marcus Chen", "Zorblatt Nine"), store)

    assert support == {"Marcus Chen": True, "Zorblatt Nine": False}
    executed_sql = " ".join(query for query, _params in cursor.executed)
    assert "FROM vnext_entities" in executed_sql
    assert "source_chunks" in executed_sql  # chunk FTS probe ran
    assert "FROM memories" in executed_sql  # memory FTS probe ran


def test_corpus_support_postgres_chunk_hit_short_circuits_memory_probe() -> None:
    cursor = _RoutingCursor(
        {
            "FROM vnext_entities": [],
            "FROM source_chunks": [{"id": str(uuid4()), "source_id": str(uuid4())}],
        }
    )
    store = PostgresVNextStore(_RoutingConnection(cursor))

    support = corpus_support(("Sapiens",), store)

    assert support == {"Sapiens": True}
    assert not any("FROM memories" in query for query, _params in cursor.executed)


# -- compute_query_grounding ---------------------------------------------------------


def test_grounding_none_without_salient_entities(conn) -> None:
    assert compute_query_grounding(_store(conn), "what did I have for dinner?") is None


def test_grounding_none_when_every_entity_is_supported(conn) -> None:
    store = _store(conn)
    _seed_chunk(store, "the Sapiens paperback arrived on Tuesday")
    assert compute_query_grounding(store, 'Have I read "Sapiens" yet?') is None


def test_grounding_none_for_uncheckable_store() -> None:
    assert compute_query_grounding(object(), 'Have I read "Sapiens" yet?') is None


def test_operational_probe_failures_degrade_to_uncheckable_but_baseexception_escapes() -> None:
    class OperationalFailureStore:
        def search_source_chunks(self, **_kwargs):
            raise RuntimeError("database connection dropped")

        def search_memories_fts(self, **_kwargs):
            raise RuntimeError("database connection dropped")

    assert compute_query_grounding(
        OperationalFailureStore(),
        'Have I read "Sapiens" yet?',
    ) is None

    class ProbeCancelled(BaseException):
        pass

    class CancelledStore:
        def search_source_chunks(self, **_kwargs):
            raise ProbeCancelled("cancelled")

    with pytest.raises(ProbeCancelled, match="cancelled"):
        compute_query_grounding(CancelledStore(), 'Have I read "Sapiens" yet?')


def test_grounding_payload_lists_only_unsupported_entities(conn) -> None:
    store = _store(conn)
    _seed_chunk(store, "we talked about Sapiens over coffee")
    grounding = compute_query_grounding(
        store, 'Did Marcus Chen recommend "Sapiens" to me?'
    )
    assert grounding == {"unsupported_entities": ["Marcus Chen"], "checked": 2}


def test_grounding_fires_for_qualified_lowercase_compound(conn) -> None:
    store = _store(conn)
    _seed_chunk(store, "the receipt total was 30 dollars at the market")
    grounding = compute_query_grounding(
        store, "How many fish are there in my 30-gallon tank?"
    )
    assert grounding == {"unsupported_entities": ["30-gallon tank"], "checked": 1}

    supported = _store(conn)
    _seed_chunk(supported, "set up the new tank for the goldfish")
    assert (
        compute_query_grounding(supported, "How many fish are there in my 30-gallon tank?")
        is None
    )


# -- context pack integration (gated; ungated path byte-identical) -------------------


class _ProbeRecordingStore:
    """Delegating wrapper that records grounding probes (limit == 1)."""

    def __init__(self, inner: SQLiteVNextStore) -> None:
        self._inner = inner
        self.probe_calls: list[tuple[str, str]] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def search_source_chunks(self, **kwargs):
        if kwargs.get("limit") == 1:
            self.probe_calls.append(("chunks", str(kwargs.get("query"))))
        return self._inner.search_source_chunks(**kwargs)

    def search_memories_fts(self, **kwargs):
        if kwargs.get("limit") == 1:
            self.probe_calls.append(("memories", str(kwargs.get("query"))))
        return self._inner.search_memories_fts(**kwargs)


def _seed_decision(store: SQLiteVNextStore) -> None:
    store.create_memory(
        {
            "memory_key": "decision.launch",
            "memory_type": "decision",
            "title": "Launch timing",
            "canonical_text": "Decision: the launch moves to next quarter.",
            "status": "active",
            "domain": "project",
            "sensitivity": "internal",
            "value": {"text": "Launch moves to next quarter."},
        }
    )


def test_ungated_query_adds_no_grounding_and_runs_no_probes(conn) -> None:
    store = _ProbeRecordingStore(_store(conn))
    _seed_decision(store._inner)

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="what did we decide about the launch?")
    )

    assert "grounding" not in pack
    assert "grounding" not in pack["trace"]
    assert store.probe_calls == []  # the probe path never even ran


def test_unsupported_entity_adds_pack_field_and_trace_mirror(conn) -> None:
    store = _store(conn)
    _seed_decision(store)

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="Did Marcus Chen approve the launch?")
    )

    assert pack["grounding"] == {"unsupported_entities": ["Marcus Chen"], "checked": 1}
    assert pack["trace"]["grounding"] == pack["grounding"]


def test_supported_entity_leaves_pack_schema_unchanged(conn) -> None:
    store = _store(conn)
    _seed_decision(store)
    _seed_chunk(store, "Marcus Chen approved the launch plan yesterday")

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="Did Marcus Chen approve the launch?")
    )

    assert "grounding" not in pack
    assert "grounding" not in pack["trace"]


def test_minimal_depth_skips_grounding_entirely(conn) -> None:
    store = _ProbeRecordingStore(_store(conn))
    _seed_decision(store._inner)

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="Did Marcus Chen approve the launch?", context_depth="minimal")
    )

    assert "grounding" not in pack
    assert "grounding" not in pack["trace"]
    assert store.probe_calls == []


def test_grounding_probe_is_read_only(conn) -> None:
    store = _store(conn)
    _seed_decision(store)
    before = len(store.list_events())

    VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="Did Marcus Chen approve the launch?")
    )

    events = store.list_events()[: len(store.list_events()) - before]
    # Only the retrieval event itself; no entity/edge/memory writes.
    assert [event["event_type"] for event in events] == ["retrieval.context_pack_compiled"]
