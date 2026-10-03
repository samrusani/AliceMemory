"""Currency chains: read-time same-slot update chains + stored valid_to.

Covers the lme6/currency-chains feature end to end:

* chain construction — supersession edges, temporal-only order, and mixed
  edge+date groups (``build_currency_chains``);
* collision safety — table-driven negatives where two DIFFERENT facts
  share a key token and must NOT chain (a wrong CURRENT label is worse
  than no label), including the failure shapes measured on the round-5
  LongMemEval stores: episodic event counts, per-night prices, goal
  values ("aimed to raise $200"), pack-theme token bridges ("charity"),
  and year-like numbers read as counts;
* pack rendering — chain members regroup into one contiguous block,
  oldest first, ``[SUPERSEDED as of <date>]`` entries before the single
  ``[CURRENT as of <date>]`` entry positioned last;
* ``valid_to`` stamping — only the approved transitions that already
  write a supersession pointer stamp the retired row;
* dormancy — packs without a confirmable same-key group are byte-identical
  to a build with the feature stubbed out;
* determinism — identical inputs produce identical chains and packs.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_currency import (
    CURRENCY_ANNOTATION_KEY,
    CURRENCY_STAGE,
    CURRENT_STATUS,
    CurrencyChainResult,
    SUPERSEDED_STATUS,
    apply_currency_chains,
    build_currency_chains,
    currency_label_suffix,
    derive_slot_signature,
    memory_event_datetime,
    supersession_event_time,
)
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api import vnext_currency as vnext_currency_module
from alicebot_api import vnext_retrieval as vnext_retrieval_module
from alicebot_api.vnext_source_fence import SourceReadFence


def _memory(
    memory_id: str,
    text: str,
    session_date: str | None,
    *,
    source_id: str = "source-1",
    chunk_index: int = 0,
    **extra: object,
) -> dict[str, object]:
    metadata: dict[str, object] = {"source_id": source_id, "source_chunk_index": chunk_index}
    if session_date is not None:
        metadata["session_date"] = session_date
    return {
        "id": memory_id,
        "canonical_text": text,
        "metadata_json": metadata,
        **extra,
    }


# -- chain construction --------------------------------------------------------


def test_temporal_chain_orders_oldest_first_and_labels_current_last() -> None:
    stale = _memory("stale", "[USER]: The bike-a-thon fundraiser raised $5,000 so far.", "2023/03/10 (Fri) 10:00")
    fresh = _memory(
        "fresh",
        "[USER]: Great news, the bike-a-thon fundraiser total is now $6,200.",
        "2023/05/02 (Tue) 09:00",
        source_id="source-2",
    )
    bystander = _memory("bystander", "[USER]: I adopted a golden retriever named Max.", "2023/04/01 (Sat) 12:00", source_id="source-3")

    result = build_currency_chains([fresh, bystander, stale])

    assert len(result.chains) == 1
    assert result.chains[0].member_ids == ("stale", "fresh")
    assert result.skipped_ambiguous == 0
    assert result.annotations["stale"]["status"] == SUPERSEDED_STATUS
    assert result.annotations["stale"]["label"] == "SUPERSEDED as of 2023-05-02"
    assert result.annotations["fresh"]["status"] == CURRENT_STATUS
    assert result.annotations["fresh"]["label"] == "CURRENT as of 2023-05-02"
    assert "bystander" not in result.annotations

    ordered = apply_currency_chains([fresh, bystander, stale], result)
    # Chain block anchors at the best-ranked member's slot, oldest first,
    # CURRENT last; the non-member keeps its relative order after it.
    assert [memory["id"] for memory in ordered] == ["stale", "fresh", "bystander"]
    assert ordered[0][CURRENCY_ANNOTATION_KEY]["label"] == "SUPERSEDED as of 2023-05-02"
    assert ordered[1][CURRENCY_ANNOTATION_KEY]["label"] == "CURRENT as of 2023-05-02"


def test_edge_only_chain_orders_by_supersession_pointers() -> None:
    old = {"id": "old", "canonical_text": "My rent is $1,800 a month.", "metadata_json": {}, "superseded_by": "new"}
    new = {"id": "new", "canonical_text": "My rent is $2,100 a month.", "metadata_json": {}, "supersedes": "old"}

    result = build_currency_chains([new, old])

    assert len(result.chains) == 1
    assert result.chains[0].member_ids == ("old", "new")
    assert result.annotations["old"]["label"] == "SUPERSEDED"  # undated edge chain: no as-of
    assert result.annotations["new"]["label"] == "CURRENT"


def test_mixed_chain_uses_dates_and_respects_edges() -> None:
    first = _memory("first", "[USER]: The bake sale fundraiser raised $100.", "2023/01/05 (Thu) 10:00")
    second = _memory(
        "second",
        "[USER]: Update: the bake sale fundraiser is at $250.",
        "2023/02/05 (Sun) 10:00",
        source_id="source-2",
        superseded_by="third",
    )
    third = _memory(
        "third",
        "[USER]: Final tally, the bake sale fundraiser hit $400.",
        "2023/03/05 (Sun) 10:00",
        source_id="source-3",
        supersedes="second",
    )

    result = build_currency_chains([third, first, second])

    assert len(result.chains) == 1
    assert result.chains[0].member_ids == ("first", "second", "third")
    assert result.annotations["first"]["status"] == SUPERSEDED_STATUS
    assert result.annotations["first"]["as_of"] == "2023-02-05"  # replaced by the next different value
    assert result.annotations["second"]["as_of"] == "2023-03-05"
    assert result.annotations["third"]["status"] == CURRENT_STATUS


def test_edge_contradicting_dates_skips_the_group() -> None:
    # The edge claims the JANUARY row replaced the MARCH row: pointer state
    # and event dates disagree, so the slot's currency is unknowable.
    march_row = _memory(
        "march-row",
        "[USER]: The bake sale fundraiser raised $100.",
        "2023/03/05 (Sun) 10:00",
        superseded_by="january-row",
    )
    january_row = _memory(
        "january-row",
        "[USER]: The bake sale fundraiser raised $250.",
        "2023/01/05 (Thu) 10:00",
        source_id="source-2",
        supersedes="march-row",
    )

    result = build_currency_chains([march_row, january_row])

    assert result.chains == ()
    assert result.skipped_ambiguous == 1


def test_same_source_document_order_breaks_date_ties() -> None:
    # Two values in ONE session (same session_date): the later chunk wins.
    was = _memory("was", "[USER]: My coin collection fund was at $90.", "2023/03/05 (Sun) 10:00", chunk_index=1)
    now = _memory("now", "[USER]: My coin collection fund is now $120.", "2023/03/05 (Sun) 10:00", chunk_index=4)

    result = build_currency_chains([now, was])

    assert len(result.chains) == 1
    assert result.chains[0].member_ids == ("was", "now")
    assert result.annotations["now"]["status"] == CURRENT_STATUS


# -- collision safety (table-driven negatives) ----------------------------------


@pytest.mark.parametrize(
    ("description", "left", "right", "expect_skip"),
    [
        (
            "same unit class, different topics (body weight vs dumbbells)",
            _memory("l", "[USER]: I weigh 80 kg after the holidays.", "2023/01/10 (Tue) 08:00"),
            _memory("r", "[USER]: I bought new 12 kg dumbbells for curls.", "2023/02/10 (Fri) 08:00", source_id="source-2"),
            True,
        ),
        (
            "same category + same value class, different triggers (adoption fee vs grooming)",
            _memory("l", "[USER]: The golden retriever adoption fee was $200.", "2023/02/11 (Sat) 09:00"),
            _memory("r", "[USER]: Grooming for my poodle costs $50.", "2023/03/11 (Sat) 09:00", source_id="source-2"),
            True,
        ),
        (
            "equal event dates, different values, different sources",
            _memory("l", "[USER]: My marathon fundraiser raised $100 already.", "2023/02/11 (Sat) 09:00"),
            _memory("r", "[USER]: My marathon fundraiser raised $900 already.", "2023/02/11 (Sat) 09:00", source_id="source-2"),
            True,
        ),
        (
            "a multi-valued member can never shape-confirm",
            _memory("l", "[USER]: The raffle fundraiser raised $100 plus $40 in pledges.", "2023/02/11 (Sat) 09:00"),
            _memory("r", "[USER]: The raffle fundraiser total reached $300.", "2023/03/11 (Sat) 09:00", source_id="source-2"),
            True,
        ),
        (
            "an undated member with no edge order",
            _memory("l", "[USER]: The gala fundraiser raised $100.", "2023/02/11 (Sat) 09:00"),
            _memory("r", "[USER]: The gala fundraiser raised $900.", None, source_id="source-2"),
            True,
        ),
        (
            "episodic times without habitual markers never form a slot",
            _memory("l", "[USER]: My client meeting is at 2:00 pm.", "2023/02/11 (Sat) 09:00"),
            _memory("r", "[USER]: The client meeting moved to 4:00 pm.", "2023/02/15 (Wed) 09:00", source_id="source-2"),
            False,  # dormant: no slot keys at all, so not even a skip
        ),
        (
            "episodic event counts are not update chains (two catches, not one slot)",
            _memory("l", "[USER]: I caught 7 largemouth bass on my trip to Lake Michigan with Alex.", "2023/08/11 (Fri) 03:49"),
            _memory("r", "[USER]: Remember that trip when we caught 9 largemouth bass with Alex?", "2023/11/30 (Thu) 00:28", source_id="source-2"),
            True,  # shared key + shared anchors, but no stative cue near either count
        ),
        (
            "two prices of different stays never chain ('$X per night' is not user state)",
            _memory("l", "[USER]: I stayed in a hostel in Tokyo that cost around $30 per night.", "2023/05/26 (Fri) 05:02"),
            _memory("r", "[USER]: The hostel I booked in Kyoto is $45 per night.", "2023/06/10 (Sat) 08:00", source_id="source-2"),
            True,  # 'per' is excluded as a count noun; the dollars pair fails the stative gate
        ),
    ],
)
def test_two_different_facts_sharing_a_key_token_must_not_chain(
    description: str,
    left: dict[str, object],
    right: dict[str, object],
    expect_skip: bool,
) -> None:
    result = build_currency_chains([left, right])

    assert result.chains == (), description
    assert result.annotations == {}, description
    assert result.skipped_ambiguous == (1 if expect_skip else 0), description


def test_stative_cue_chain_matches_the_follower_update_shape() -> None:
    # Modeled on the real LongMemEval follower update: every member states
    # its count in stative/cumulative terms, so the chain forms and both
    # 600-carrying members are CURRENT.
    stale = _memory(
        "stale",
        "[USER]: I just reached 500 followers on Instagram last week, and I want to keep the momentum going.",
        "2023/05/27 (Sat) 07:39",
    )
    fresh = _memory(
        "fresh",
        "[USER]: More Instagram ideas please. By the way, I just checked and I'm now at 600 followers, a nice milestone!",
        "2023/05/28 (Sun) 22:57",
        source_id="source-2",
    )
    echo = _memory(
        "echo",
        "[ASSISTANT]: Congratulations on reaching 600 followers! That's a great milestone for your Instagram!",
        "2023/05/28 (Sun) 22:57",
        source_id="source-2",
        chunk_index=1,
    )

    result = build_currency_chains([fresh, stale, echo])

    assert len(result.chains) == 1
    assert result.chains[0].member_ids == ("stale", "fresh", "echo")
    assert result.annotations["stale"]["status"] == SUPERSEDED_STATUS
    assert result.annotations["stale"]["as_of"] == "2023-05-28"
    assert result.annotations["fresh"]["status"] == CURRENT_STATUS
    assert result.annotations["echo"]["status"] == CURRENT_STATUS


def test_goal_values_never_shape_confirm_a_chain() -> None:
    # Measured failure shape (round-5 store 078150f1): "initially aimed to
    # raise $200" is an aspiration; chaining it SUPERSEDED under the $250
    # outcome would sabotage a goal-vs-outcome question. The prospective
    # marker before the value vetoes the stative cue.
    goal = _memory(
        "goal",
        "[USER]: I recently participated in a charity cycling event where I initially aimed to raise $200 in donations.",
        "2023/05/20 (Sat) 03:00",
    )
    outcome = _memory(
        "outcome",
        "[USER]: The charity cycling event went great and I raised $250 in donations.",
        "2023/05/28 (Sun) 14:28",
        source_id="source-2",
    )
    goal_signature = derive_slot_signature(goal)
    assert goal_signature.values["currency:dollars"] == "200"
    assert "currency:dollars" not in goal_signature.stative_classes
    assert "currency:dollars" in derive_slot_signature(outcome).stative_classes

    result = build_currency_chains([goal, outcome])

    assert result.chains == ()
    assert result.skipped_ambiguous == 1

    # Direction matters: aspiration AFTER a stated value does not veto it.
    trailing = derive_slot_signature(
        {"canonical_text": "[USER]: I just reached 600 followers, and I'm hoping the momentum continues."}
    )
    assert "count:follower" in trailing.stative_classes


def test_pack_theme_tokens_cannot_carry_a_pair() -> None:
    # Measured failure shape (round-5 store 129d1232): a charity-themed
    # pack where a $600 yoga event and a $5,000 bike-a-thon share ONLY the
    # theme token "charity" — different events must not chain on the theme.
    yoga = _memory(
        "yoga",
        "[USER]: I helped organize a charity yoga session that raised $600 for the shelter.",
        "2023/05/01 (Mon) 21:16",
    )
    bikeathon = _memory(
        "bikeathon",
        "[USER]: Speaking of charity, my team's cancer research ride raised $5,000 altogether!",
        "2023/05/01 (Mon) 22:24",
        source_id="source-2",
    )
    fillers = [
        _memory(
            f"filler-{index}",
            f"[ASSISTANT]: Charity work is rewarding; here is charity idea number {'abcdef'[index]}.",
            "2023/05/01 (Mon) 10:00",
            source_id=f"source-filler-{index}",
        )
        for index in range(4)
    ]

    result = build_currency_chains([yoga, bikeathon, *fillers])

    assert result.chains == ()
    assert result.skipped_ambiguous == 1

    # Control: the same two values chain when they share a SPECIFIC token
    # ("bake sale fundraiser") that the rest of the pack does not carry.
    first = _memory(
        "first",
        "[USER]: Our bake sale fundraiser raised $600 so far for the shelter.",
        "2023/05/01 (Mon) 21:16",
    )
    second = _memory(
        "second",
        "[USER]: Update on the bake sale fundraiser: we have now raised $5,000!",
        "2023/05/02 (Tue) 22:24",
        source_id="source-2",
    )
    control = build_currency_chains([first, second, *fillers])
    assert len(control.chains) == 1
    assert control.chains[0].member_ids == ("first", "second")


def test_year_like_numbers_are_never_counts() -> None:
    # "pre-1920 American coins" vs "pre-1900 American coins": the numbers
    # are years, and a count keyed on them would chain two different facts.
    left = _memory("l", "[USER]: I organized my pre-1920 American coins by mint mark.", "2023/02/11 (Sat) 09:00")
    right = _memory("r", "[USER]: My pre-1900 American coins are stored in albums.", "2023/03/11 (Sat) 09:00", source_id="source-2")
    assert not any(key.startswith("count:") for key in derive_slot_signature(left).slot_keys)
    assert not any(key.startswith("count:") for key in derive_slot_signature(right).slot_keys)

    result = build_currency_chains([left, right])

    assert result.chains == ()
    assert result.annotations == {}


def test_slot_signature_hygiene_and_stative_classes() -> None:
    # Function words and currency codes after numbers never become count
    # nouns; separator-less year-range numbers never become count values.
    per_person = derive_slot_signature(
        {"canonical_text": "[USER]: I'm looking to spend around $50-75 per person for the dinner."}
    )
    assert "count:per" not in per_person.slot_keys
    usd_code = derive_slot_signature(
        {"canonical_text": "[ASSISTANT]: Around $18-$45 USD per night is a reasonable range."}
    )
    assert "count:usd" not in usd_code.slot_keys
    # Stative cues: episodic mentions carry the value but not the cue.
    episodic = derive_slot_signature(
        {"canonical_text": "[USER]: I caught 7 largemouth bass on my trip to Lake Michigan."}
    )
    assert episodic.values["count:largemouth"] == "7"
    assert "count:largemouth" not in episodic.stative_classes
    stative = derive_slot_signature(
        {"canonical_text": "[USER]: I just reached 500 followers on Instagram last week."}
    )
    assert stative.values["count:follower"] == "500"
    assert "count:follower" in stative.stative_classes
    # The cue must share the value's sentence: a cue in the PREVIOUS
    # sentence does not leak across the boundary.
    cross_sentence = derive_slot_signature(
        {"canonical_text": "[USER]: Do you have any recommendations? I caught 7 largemouth bass today."}
    )
    assert "count:largemouth" not in cross_sentence.stative_classes


def test_same_value_restatements_stay_silent() -> None:
    # No update signal: labels only appear where a stale value coexists
    # with a newer one. Not a chain, not an ambiguity skip.
    first = _memory("first", "[USER]: The bake sale fundraiser raised $300.", "2023/02/11 (Sat) 09:00")
    echo = _memory(
        "echo",
        "[ASSISTANT]: Congrats on the bake sale fundraiser raising $300!",
        "2023/02/11 (Sat) 09:00",
        chunk_index=1,
    )

    result = build_currency_chains([first, echo])

    assert result.chains == ()
    assert result.skipped_ambiguous == 0
    assert not result.considered


def test_overlapping_candidate_chains_are_all_dropped() -> None:
    # "shared" belongs to a dollars chain with "money" AND a distance chain
    # with "distance-mate"; contradictory labels are possible, so both
    # candidate chains drop and both are disclosed as skips.
    shared = _memory(
        "shared",
        "[USER]: My marathon training fundraiser raised $100 and I ran 5 km today.",
        "2023/02/01 (Wed) 09:00",
    )
    money = _memory(
        "money",
        "[USER]: The marathon training fundraiser raised $250 total.",
        "2023/03/01 (Wed) 09:00",
        source_id="source-2",
    )
    distance_mate = _memory(
        "distance-mate",
        "[USER]: Marathon training update: I ran 12 km today.",
        "2023/04/01 (Sat) 09:00",
        source_id="source-3",
    )

    result = build_currency_chains([shared, money, distance_mate])

    assert result.chains == ()
    assert result.annotations == {}
    assert result.skipped_ambiguous >= 2


def test_rollup_cards_never_join_chains() -> None:
    card = _memory("card", "[USER]: Fundraiser roll-up: $100 then $250.", "2023/05/01 (Mon) 09:00")
    card["metadata_json"]["consolidation"] = {"proposal_kind": "merge"}
    instance = _memory("instance", "[USER]: The school fundraiser raised $100.", "2023/02/01 (Wed) 09:00", source_id="source-2")

    result = build_currency_chains([card, instance])

    assert result.chains == ()
    assert not result.considered


def test_determinism_identical_inputs_identical_chains() -> None:
    rows = [
        _memory("a", "[USER]: The bike-a-thon fundraiser raised $5,000 so far.", "2023/03/10 (Fri) 10:00"),
        _memory("b", "[USER]: The bike-a-thon fundraiser total is now $6,200.", "2023/05/02 (Tue) 09:00", source_id="source-2"),
        _memory("c", "[USER]: I usually swim 2 km at the pool.", "2023/04/02 (Sun) 09:00", source_id="source-3"),
    ]

    def snapshot() -> str:
        result = build_currency_chains([dict(row, metadata_json=dict(row["metadata_json"])) for row in rows])
        return json.dumps(
            {
                "chains": [[chain.chain_id, chain.slot_key, list(chain.member_ids)] for chain in result.chains],
                "annotations": result.annotations,
                "skipped": result.skipped_ambiguous,
            },
            sort_keys=True,
        )

    assert snapshot() == snapshot()


# -- event dates ----------------------------------------------------------------


def test_memory_event_datetime_falls_back_to_the_provenance_source() -> None:
    memory = {"id": "m", "canonical_text": "x", "metadata_json": {"source_id": "src-9"}}
    source = {"id": "src-9", "metadata_json": {"session_date": "2023/05/20 (Sat) 07:47"}}

    event = memory_event_datetime(memory, source_lookup={"src-9": source}.get)

    assert event is not None and event.isoformat().startswith("2023-05-20T07:47")
    # And deliberately no write-clock fallback:
    undated = {"id": "m2", "canonical_text": "x", "metadata_json": {}, "created_at": "2026-01-01T00:00:00Z"}
    assert memory_event_datetime(undated) is None


# -- pack integration (SQLite end to end) ----------------------------------------


def _sqlite_store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Currency Chains Test")
    return SQLiteVNextStore(conn, user_id)


def _seed_source(store: SQLiteVNextStore, source_id: str, session_date: str) -> None:
    store.create_source(
        {
            "id": source_id,
            "source_type": "chat_session",
            "title": f"Chat session on {session_date}",
            "content_hash": f"sha256:{source_id}",
            "metadata_json": {"session_id": source_id, "session_date": session_date},
        }
    )


def _seed_memory(
    store: SQLiteVNextStore,
    *,
    memory_id: str,
    text: str,
    source_id: str | None = None,
    chunk_index: int = 0,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if source_id is not None:
        metadata = {"source_id": source_id, "source_chunk_index": chunk_index}
    return store.create_memory(
        {
            "id": memory_id,
            "memory_key": f"vnext.test.{memory_id}",
            "memory_type": "semantic",
            "title": text[:60],
            "canonical_text": text,
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "value": {"text": text},
            "metadata_json": metadata,
        }
    )


def test_compile_context_pack_renders_the_chain_block_and_trace() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-old", "2023/03/10 (Fri) 10:00")
    _seed_source(store, "session-new", "2023/05/02 (Tue) 09:00")
    _seed_memory(
        store,
        memory_id="11111111-1111-4111-8111-111111111111",
        text="[USER]: The bike-a-thon fundraiser raised $5,000 so far.",
        source_id="session-old",
    )
    _seed_memory(
        store,
        memory_id="22222222-2222-4222-8222-222222222222",
        text="[USER]: Great news, the bike-a-thon fundraiser total is now $6,200.",
        source_id="session-new",
    )

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="bike-a-thon fundraiser"),
        source_fence=SourceReadFence.unfenced()
    )

    memories = pack["relevant_memories"]
    labels = [memory.get(CURRENCY_ANNOTATION_KEY, {}).get("label") for memory in memories]
    assert labels == ["SUPERSEDED as of 2023-05-02", "CURRENT as of 2023-05-02"]
    # current_known_state mirrors the chain order (CURRENT last).
    assert [ref["id"] for ref in pack["current_known_state"]] == [memory["id"] for memory in memories]
    stage = pack["trace"]["stages"][CURRENCY_STAGE]
    assert stage["chains"] == 1
    assert stage["members"] == 2
    assert stage["skipped_ambiguous"] == 0
    assert stage["label_chars"] > 0


def test_compile_context_pack_discloses_ambiguous_skips_without_annotating() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-a", "2023/02/11 (Sat) 09:00")
    _seed_source(store, "session-b", "2023/02/11 (Sat) 09:00")
    _seed_memory(
        store,
        memory_id="33333333-3333-4333-8333-333333333333",
        text="[USER]: My marathon fundraiser raised $100 already.",
        source_id="session-a",
    )
    _seed_memory(
        store,
        memory_id="44444444-4444-4444-8444-444444444444",
        text="[USER]: My marathon fundraiser raised $900 already.",
        source_id="session-b",
    )

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="marathon fundraiser"),
        source_fence=SourceReadFence.unfenced()
    )

    assert all(CURRENCY_ANNOTATION_KEY not in memory for memory in pack["relevant_memories"])
    stage = pack["trace"]["stages"][CURRENCY_STAGE]
    assert stage == {"chains": 0, "members": 0, "skipped_ambiguous": 1, "label_chars": 0}


def test_dormant_pack_is_byte_identical_to_a_feature_stubbed_build(monkeypatch) -> None:
    store = _sqlite_store()
    _seed_source(store, "session-1", "2023/03/10 (Fri) 10:00")
    _seed_memory(
        store,
        memory_id="55555555-5555-4555-8555-555555555555",
        text="[USER]: I adopted a golden retriever named Max.",
        source_id="session-1",
    )
    _seed_memory(
        store,
        memory_id="66666666-6666-4666-8666-666666666666",
        text="[USER]: The bake sale fundraiser raised $300.",
        source_id="session-1",
        chunk_index=1,
    )
    service = VNextRetrievalService(store)

    def build_pack(stubbed: bool) -> str:
        if stubbed:
            monkeypatch.setattr(
                vnext_retrieval_module.vnext_currency,
                "build_currency_chains",
                lambda memories, **kwargs: CurrencyChainResult(
                    chains=(), annotations={}, skipped_ambiguous=0, label_chars=0
                ),
            )
        else:
            monkeypatch.undo()
        pack = service.compile_context_pack(
            VNextRetrievalRequest(query="retriever fundraiser", trace_id="fixed-trace"),
            source_fence=SourceReadFence.unfenced()
        )
        pack["context_pack_id"] = "fixed"  # uuid minted per call; not feature behavior
        return json.dumps(pack, sort_keys=True, default=str)

    # Two distinct slot keys, one member each: no same-key group, so the
    # live feature must produce the byte-identical pack a stubbed build
    # does against the same store.
    live = build_pack(stubbed=False)
    assert live == build_pack(stubbed=True)
    assert CURRENCY_STAGE not in json.loads(live)["trace"]["stages"]
    assert all(
        CURRENCY_ANNOTATION_KEY not in memory
        for memory in json.loads(live)["relevant_memories"]
    )


def test_minimal_depth_never_runs_the_chain_stage() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-old", "2023/03/10 (Fri) 10:00")
    _seed_source(store, "session-new", "2023/05/02 (Tue) 09:00")
    _seed_memory(
        store,
        memory_id="77777777-7777-4777-8777-777777777777",
        text="[USER]: The bike-a-thon fundraiser raised $5,000 so far.",
        source_id="session-old",
    )
    _seed_memory(
        store,
        memory_id="88888888-8888-4888-8888-888888888888",
        text="[USER]: The bike-a-thon fundraiser total is now $6,200.",
        source_id="session-new",
    )

    pack = VNextRetrievalService(store).compile_context_pack(
        VNextRetrievalRequest(query="bike-a-thon fundraiser", context_depth="minimal"),
        source_fence=SourceReadFence.unfenced()
    )

    assert CURRENCY_STAGE not in pack["trace"]["stages"]
    assert all(CURRENCY_ANNOTATION_KEY not in memory for memory in pack["relevant_memories"])


def test_pack_determinism_two_identical_compiles() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-old", "2023/03/10 (Fri) 10:00")
    _seed_source(store, "session-new", "2023/05/02 (Tue) 09:00")
    _seed_memory(
        store,
        memory_id="99999999-9999-4999-8999-999999999999",
        text="[USER]: The bike-a-thon fundraiser raised $5,000 so far.",
        source_id="session-old",
    )
    _seed_memory(
        store,
        memory_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        text="[USER]: The bike-a-thon fundraiser total is now $6,200.",
        source_id="session-new",
    )
    service = VNextRetrievalService(store)

    def compile_snapshot() -> str:
        pack = service.compile_context_pack(
            VNextRetrievalRequest(query="bike-a-thon fundraiser", trace_id="fixed-trace"),
            source_fence=SourceReadFence.unfenced()
        )
        return json.dumps(
            {
                "memories": [
                    (memory["id"], memory.get(CURRENCY_ANNOTATION_KEY))
                    for memory in pack["relevant_memories"]
                ],
                "stage": pack["trace"]["stages"][CURRENCY_STAGE],
            },
            sort_keys=True,
            default=str,
        )

    assert compile_snapshot() == compile_snapshot()


# -- rendering suffix -------------------------------------------------------------


def test_currency_label_suffix_renders_only_annotated_memories() -> None:
    annotated = {"currency": {"label": "CURRENT as of 2023-05-02"}}
    assert currency_label_suffix(annotated) == " [CURRENT as of 2023-05-02]"
    assert currency_label_suffix({}) == ""
    assert currency_label_suffix({"currency": {}}) == ""
    assert currency_label_suffix({"currency": "bogus"}) == ""


# -- valid_to stamping (approved supersessions only) ------------------------------


def test_undo_with_replacement_stamps_valid_to_with_the_successor_event_time() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-new", "2023/05/02 (Tue) 09:00")
    retired = _seed_memory(
        store,
        memory_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        text="[USER]: My rent is $1,800 a month.",
    )
    replacement = _seed_memory(
        store,
        memory_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        text="[USER]: My rent is $2,100 a month.",
        source_id="session-new",
    )
    assert retired.get("valid_to") is None

    VNextMemoryCommitService(store).undo(
        identity=None,
        memory_id=str(retired["id"]),
        superseded_by_memory_id=str(replacement["id"]),
        reason="corrected by replacement",
    )

    stamped = store.get_memory(str(retired["id"]))
    assert stamped["status"] == "superseded"
    assert stamped["superseded_by"] == str(replacement["id"])
    # The successor's provenance session date is its event time.
    assert str(stamped["valid_to"]).startswith("2023-05-02T09:00")


def test_undo_without_replacement_and_forget_do_not_stamp_valid_to() -> None:
    store = _sqlite_store()
    service = VNextMemoryCommitService(store)
    plain_undo = _seed_memory(
        store,
        memory_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        text="[USER]: My rent is $1,800 a month.",
    )
    forgotten = _seed_memory(
        store,
        memory_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        text="[USER]: My rent is $2,100 a month.",
    )

    service.undo(identity=None, memory_id=str(plain_undo["id"]), reason="just undo")
    service.forget(identity=None, memory_id=str(forgotten["id"]), reason="forget it")

    assert store.get_memory(str(plain_undo["id"]))["valid_to"] is None
    assert store.get_memory(str(forgotten["id"]))["valid_to"] is None


def test_existing_earlier_valid_to_is_preserved_by_the_stamp() -> None:
    store = _sqlite_store()
    retired = _seed_memory(
        store,
        memory_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        text="[USER]: My rent is $1,800 a month.",
    )
    store.update_memory(
        memory_id=str(retired["id"]),
        patch={"valid_to": "2022-12-31T00:00:00Z"},
        actor_type="system",
    )
    replacement = _seed_memory(
        store,
        memory_id="12121212-1212-4121-8121-121212121212",
        text="[USER]: My rent is $2,100 a month.",
    )

    VNextMemoryCommitService(store).undo(
        identity=None,
        memory_id=str(retired["id"]),
        superseded_by_memory_id=str(replacement["id"]),
        reason="corrected by replacement",
    )

    assert str(store.get_memory(str(retired["id"]))["valid_to"]).startswith("2022-12-31")


def test_later_or_unbounded_valid_to_is_closed_at_supersession_event_time() -> None:
    store = _sqlite_store()
    _seed_source(store, "session-replacement", "2026/03/01 (Sun) 12:00")
    replacement = _seed_memory(
        store,
        memory_id="13131313-1313-4131-8131-131313131313",
        text="[USER]: My rent is $2,100 a month.",
        source_id="session-replacement",
    )
    service = VNextMemoryCommitService(store)

    for memory_id, valid_to in (
        ("14141414-1414-4141-8141-141414141414", "2030-01-01T00:00:00Z"),
        ("15151515-1515-4151-8151-151515151515", "9999-12-31T23:59:59Z"),
    ):
        retired = _seed_memory(
            store,
            memory_id=memory_id,
            text="[USER]: My rent is $1,800 a month.",
        )
        store.update_memory(
            memory_id=str(retired["id"]),
            patch={"valid_to": valid_to},
            actor_type="system",
        )

        service.undo(
            identity=None,
            memory_id=str(retired["id"]),
            superseded_by_memory_id=str(replacement["id"]),
            reason="corrected by replacement",
        )

        assert str(store.get_memory(str(retired["id"]))["valid_to"]).startswith("2026-03-01T12:00")


def test_supersession_event_time_falls_back_to_created_at() -> None:
    successor = {
        "id": "s",
        "canonical_text": "x",
        "metadata_json": {},
        "created_at": "2026-07-01T12:00:00Z",
    }
    assert supersession_event_time(successor) == "2026-07-01T12:00:00Z"
    assert supersession_event_time({"id": "s", "canonical_text": "x", "metadata_json": {}}) is None


# -- slot signature sanity ---------------------------------------------------------


def test_slot_signature_extracts_counts_amounts_and_habitual_times() -> None:
    signature = derive_slot_signature(
        {
            "canonical_text": (
                "[USER]: I usually hit the gym at 6:00 pm; I now own four bikes "
                "and raised $1,200.50 for the charity auction."
            )
        }
    )
    assert "count:bike" in signature.slot_keys
    assert "currency:dollars" in signature.slot_keys
    assert "category:exercise fitness workout activity" in signature.slot_keys
    assert signature.values["count:bike"] == "4"
    assert signature.values["currency:dollars"] == "1200.5"
    assert signature.values["timeofday"] == "18:00"
    assert "gym" in signature.anchors
