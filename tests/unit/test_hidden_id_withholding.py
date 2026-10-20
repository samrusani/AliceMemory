"""A key with limits is not shown the id of a row it may not read.

The ids below hold hex letters on purpose. An id made of digits only reads the same in upper case, so a filter that
compared one spelling of it would pass every test.

Mutations that these tests fail, each one alone:

* ``strip_hidden_ids`` returns ``value`` before it walks: ``test_listing_fields_lose_a_hidden_id_and_the_count_matches``.
* ``_tokens`` and ``_whole_id`` take the text of a token as it was written, without ``identifier``, or accept only the
  canonical spelling: ``test_every_spelling_of_a_hidden_id_is_removed_when_it_stands_alone`` fails for the compact, braced,
  ``urn:uuid:``, upper case, mixed case and odd-hyphen spellings.
* ``_Judge.withheld`` stops asking whether the id is readable at a typed position, or ``output_artifact_id`` leaves the
  typed scalar fields: ``test_a_hidden_id_and_a_missing_id_leave_the_same_answer``.
* ``_fix_counts`` is not called, drops the zero rule or the rule for a list that lost nothing, or the stale sweep and
  roll-up relations go: the tests of counts below.
* ``withhold_json_body`` reads the user from the query, skips the early exit for a body without an id, passes a limited key
  with no user through, or the layer rebuilds the headers from a dict: the tests of the layer below.
* ``withhold_with`` always opens a store, or ignores the ids the caller named.
* ``runner.main`` calls the handler outside ``reader_scope``: ``test_a_reader_a_command_remembers_ends_with_the_command``.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_hidden_ids import (
    HiddenIdFilterError,
    apply_hidden_id_withholding,
    echo_of_arguments,
    echo_of_request,
    judged_ids,
    strip_hidden_ids,
    withhold_json_body,
    withhold_with,
)

HIDDEN = "abcdef12-3456-4789-8abc-def012345678"
VISIBLE = "bbbbbbbb-2222-4222-8222-222222222222"
SECOND_VISIBLE = "bbbbbbbb-2222-4222-8222-333333333333"
SOURCE = "cccccccc-3333-4333-8333-333333333333"
VISIBLE_SOURCE = "cccccccc-3333-4333-8333-444444444444"
LOOP = "dddddddd-4444-4444-8444-444444444444"
ARTIFACT = "eeeeeeee-5555-4555-8555-555555555555"
BELIEF = "9a9a9a9a-6666-4666-8666-666666666666"
VISIBLE_BELIEF = "9a9a9a9a-6666-4666-8666-777777777777"
MISSING = "fafafafa-9999-4999-8999-999999999999"


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(agent_id="reader", permission_profile=profile, auth="agent_api_key")


class _Store:
    def __init__(self) -> None:
        self.reads = 0
        self.rows = {
            "memory": [
                {"id": HIDDEN, "domain": "project", "sensitivity": "confidential"},
                {"id": VISIBLE, "domain": "project", "sensitivity": "public"},
                {"id": SECOND_VISIBLE, "domain": "project", "sensitivity": "public"},
            ],
            "source": [
                {"id": SOURCE, "domain": "project", "sensitivity": "confidential"},
                {"id": VISIBLE_SOURCE, "domain": "project", "sensitivity": "public"},
            ],
            "open_loop": [{"id": LOOP, "domain": "project", "sensitivity": "confidential"}],
            "artifact": [{"id": ARTIFACT, "domain": "project", "sensitivity": "confidential"}],
            "belief": [
                {"id": BELIEF, "domain": "project", "sensitivity": "confidential"},
                {"id": VISIBLE_BELIEF, "domain": "project", "sensitivity": "public"},
            ],
        }

    def read_label_rows(self, kind: str, ids: list[str]):
        self.reads += 1
        return [row for row in self.rows.get(kind, []) if row["id"] in ids]


def _echo(**inputs: set[str]) -> dict[str, frozenset[str]]:
    """What a caller supplied, by the name of the input."""

    return {name: frozenset(ids) for name, ids in inputs.items()}


def _shown(payload, *, profile: str = "trusted_local_agent", store=None, echo=None):
    return apply_hidden_id_withholding(store or _Store(), _identity(profile), payload, echo=echo)


def _spellings(canonical: str) -> dict[str, str]:
    """Spellings of one id that ``uuid.UUID`` takes, none of them the canonical one."""

    compact = canonical.replace("-", "")
    groups = canonical.split("-")
    return {
        "upper case": canonical.upper(),
        "first group upper case": "-".join([groups[0].upper(), *groups[1:]]),
        "last group upper case": "-".join([*groups[:-1], groups[-1].upper()]),
        "compact": compact,
        "compact upper case": compact.upper(),
        "braces": "{" + canonical + "}",
        "braces upper case": "{" + canonical.upper() + "}",
        "urn": "urn:uuid:" + canonical,
        "urn upper case": "URN:UUID:" + canonical.upper(),
        "uuid prefix": "uuid:" + canonical,
        "groups of four": "-".join(compact[i : i + 4] for i in range(0, 32, 4)),
        "groups of eight": "-".join(compact[i : i + 8] for i in range(0, 32, 8)),
        "hyphens at both ends": "-" + canonical + "-",
        "doubled hyphens": canonical.replace("-", "--"),
    }


def _digits(text: str) -> str:
    return re.sub(r"[^0-9a-f]", "", text.lower())


def test_the_ids_of_these_tests_hold_hex_letters() -> None:
    """An upper case spelling that equals the canonical one would hide a filter that compares one spelling."""

    assert HIDDEN.upper() != HIDDEN
    assert all(spelling != HIDDEN for spelling in _spellings(HIDDEN).values())
    # Most spellings do not hold the canonical one as a substring, so a filter that looks for it misses them.
    assert sum(HIDDEN not in spelling for spelling in _spellings(HIDDEN).values()) >= 9


def _payload() -> dict:
    return {
        "items": [{"id": VISIBLE, "metadata_json": {"candidate_memory_ids": [HIDDEN, VISIBLE]}}],
        "count": 1,
        "source_refs": [f"source:{SOURCE}", f"memory:{VISIBLE}"],
        "artifact_id": ARTIFACT,
        "content_markdown": f"The note {HIDDEN} stays in words only.",
        "member_snapshots": [{"id": HIDDEN, "status": "active"}, {"id": VISIBLE, "status": "active"}],
        "open_loop_ids": [LOOP, VISIBLE],
        "detail": f"missing {HIDDEN}",
    }


def test_listing_fields_lose_a_hidden_id_and_the_count_matches() -> None:
    hidden = {HIDDEN, SOURCE, LOOP, ARTIFACT}
    payload = strip_hidden_ids(_payload(), hidden)
    assert payload["items"][0]["metadata_json"]["candidate_memory_ids"] == [VISIBLE]
    assert payload["source_refs"] == [f"memory:{VISIBLE}"]
    assert payload["artifact_id"] is None
    assert HIDDEN not in payload["content_markdown"]
    assert "stays in words only" in payload["content_markdown"]
    assert payload["member_snapshots"] == [{"id": VISIBLE, "status": "active"}]
    assert payload["open_loop_ids"] == [VISIBLE]
    # Free text is the writer's words, so an id in it is not looked for.
    assert payload["detail"] == f"missing {HIDDEN}"
    assert payload["count"] == 1


def test_an_archived_row_is_withheld_and_a_forgotten_row_stays() -> None:
    archived = "66666666-6666-4666-8666-666666666666"
    forgotten = "77777777-7777-4777-8777-777777777777"
    deleted = "88888888-8888-4888-8888-888888888888"
    store = _Store()
    store.rows["memory"].extend(
        [
            {"id": archived, "domain": "project", "sensitivity": "public", "status": "archived"},
            {"id": forgotten, "domain": "project", "sensitivity": "public", "status": "forgotten"},
        ]
    )
    store.rows["source"].append(
        {"id": deleted, "domain": "project", "sensitivity": "public", "deleted_at": "2026-01-01T00:00:00Z"}
    )
    payload = {"memory_ids": [archived, forgotten, VISIBLE], "source_id": deleted}
    limited = apply_hidden_id_withholding(store, _identity("read_only_agent"), payload)
    assert limited["memory_ids"] == [forgotten, VISIBLE]
    assert limited["source_id"] is None


def test_an_id_stays_where_the_answer_repeats_the_input_that_held_it() -> None:
    """The id in the request is not a row the caller discovered, at the key that repeats it. Nowhere else."""

    payload = {
        "target_id": HIDDEN,
        "source_refs": [f"source:{SOURCE}", f"memory:{HIDDEN}"],
        "candidate_memory_ids": [HIDDEN, VISIBLE],
    }
    shown = strip_hidden_ids(payload, {HIDDEN, SOURCE}, echo=_echo(target_id={HIDDEN}))
    assert shown["target_id"] == HIDDEN
    assert shown["source_refs"] == []
    assert shown["candidate_memory_ids"] == [VISIBLE]


def test_a_field_the_product_does_not_write_is_dropped_from_a_ref_that_names_a_row_even_when_the_caller_sent_it() -> None:
    """The saved-quote reader drops such a field for a hidden row, so a missing row must leave the same entry."""

    refs = [{"origin": f"memory:{MISSING}", "memory_id": MISSING, "quote": "q"}, {"origin": f"memory:{VISIBLE}"}]
    shown = _shown({"source_refs": refs}, echo=_echo(source_refs={MISSING}))
    assert shown == {"source_refs": [{"memory_id": MISSING, "quote": "q"}, {"origin": f"memory:{VISIBLE}"}]}
    assert _shown({"source_refs": [{"origin": f"memory:{HIDDEN}", "memory_id": HIDDEN}]}) == {"source_refs": [{"memory_id": None}]}


def test_an_id_in_a_ref_that_may_name_a_chunk_or_a_session_stays_when_it_names_no_row() -> None:
    """The saved-quote reader leaves such an id alone, because it cannot tell it from the id of a chunk or a session."""

    refs = [
        {"origin": MISSING, "chunk_id": MISSING, "memory_id": VISIBLE, "quote": f"words {MISSING}"},
        f"see session {MISSING}",
        f"{MISSING}#chunk-1",
        {"evidence": {"ref_id": MISSING}},
        MISSING,
    ]
    shown = _shown({"source_refs": refs})["source_refs"]
    assert shown == refs[:-1]
    # The words of a quote are the writer's text, so an id in them is not looked for, in a ref as anywhere else.
    quoted = [{"memory_id": VISIBLE, "quote": f"words {HIDDEN}"}, {"memory_id": VISIBLE, "conversation_excerpt": f"see {HIDDEN}"}]
    assert _shown({"source_refs": quoted})["source_refs"] == quoted
    # An id under a field the product reads as a reference names a row, and a bare entry of the list does too.
    assert _shown({"source_refs": [{"id": MISSING}, {"ref": MISSING}, {"source_ids": [MISSING]}]})["source_refs"] == [{"id": None}, {"ref": None}, {"source_ids": []}]


def test_a_prefixed_ref_that_goes_on_after_its_id_is_judged_by_the_id() -> None:
    """``source:<id>#chunk-1`` names the row of the id. A hidden row and a missing row leave one answer."""

    def shown_refs(row_id: str) -> list:
        refs = [
            f"source:{row_id}#chunk-1",
            f"memory:{row_id} some words",
            f"alice://sources/{row_id.upper()}#c",
            f"MEMORY: {row_id}",
        ]
        return _shown({"source_refs": refs})["source_refs"]

    assert shown_refs(HIDDEN) == shown_refs(MISSING) == []
    readable = [f"memory:{VISIBLE}#chunk-1", f"alice://sources/{VISIBLE_SOURCE}#c"]
    assert _shown({"source_refs": readable})["source_refs"] == readable
    # A bare id with words round it is an incidental position: only a stored, unreadable row is withheld there.
    incidental = {"source_refs": [f"{HIDDEN}#frag", f"{MISSING}#frag"]}
    assert _shown(incidental)["source_refs"] == [f"{MISSING}#frag"]


def test_json_text_in_a_ref_is_read_like_the_object_it_holds() -> None:
    def shown(row_id: str) -> list:
        refs = [
            json.dumps({"memory_id": row_id, "quote": "q"}),
            json.dumps([{"memory_id": row_id}, f"memory:{row_id}", {"origin": f"memory:{row_id}", "quote": "q"}]),
            "[" + json.dumps(json.dumps({"source_id": row_id})) + "]",
        ]
        return _shown({"source_refs": refs})["source_refs"]

    hidden, missing = shown(HIDDEN), shown(MISSING)
    assert hidden == missing
    assert [json.loads(entry) for entry in hidden[:3]] == [
        {"memory_id": None, "quote": "q"},
        [{"memory_id": None}, {"quote": "q"}],
        [json.dumps({"source_id": None})],
    ]
    # A sentence that holds an id is an incidental position: only the id of a stored row that is hidden is withheld there.
    sentence = {"source_refs": [f"copied from {{not json {HIDDEN}", f"copied from {{not json {MISSING}"]}
    assert _shown(sentence)["source_refs"] == [f"copied from {{not json {MISSING}"]
    readable = [json.dumps({"memory_id": VISIBLE, "quote": "q"})]
    assert _shown({"source_refs": readable})["source_refs"] == readable


def test_a_readable_id_the_caller_sent_is_looked_up_when_the_field_decides() -> None:
    """The id is looked up although the caller sent it, because the field decides, so a readable row is not dropped."""

    refs = [{"origin": f"memory:{VISIBLE}", "memory_id": VISIBLE, "quote": "q"}]
    echo = _echo(source_refs={VISIBLE})
    assert _shown({"source_refs": refs}, echo=echo) == {"source_refs": refs}
    assert judged_ids({"source_refs": refs}, echo) == {VISIBLE}
    assert judged_ids({"source_refs": [{"memory_id": VISIBLE}]}, echo) == set()


def test_an_id_the_caller_supplied_stays_in_the_echo_whether_the_row_is_hidden_or_missing() -> None:
    payload = {"source_refs": [f"memory:{HIDDEN}", f"memory:{MISSING}"], "memory_id": MISSING}
    both = _echo(source_refs={HIDDEN, MISSING})
    assert _shown(payload, echo=both) == {**payload, "memory_id": None}
    one = _echo(source_refs={HIDDEN})
    assert _shown(payload, echo=one) == {"source_refs": [f"memory:{HIDDEN}"], "memory_id": None}


# -- item 1: every spelling, found by shape and compared in canonical form --------------------------------------------


@pytest.mark.parametrize("name", sorted(_spellings(HIDDEN)))
def test_every_spelling_of_a_hidden_id_is_removed_when_it_stands_alone(name: str) -> None:
    """The canonical spelling is nowhere in the payload, so a filter that lists spellings, or that compares text
    it did not parse with ``identifier``, leaves the id in."""

    spelling = _spellings(HIDDEN)[name]
    shown = _shown(
        {
            "candidate_memory_ids": [spelling, VISIBLE],
            "memory_id": spelling,
            "source_refs": [f"memory:{spelling}", f"source:{spelling}"],
            "content_markdown": f"One line {spelling} and another.",
        }
    )
    assert shown["candidate_memory_ids"] == [VISIBLE]
    assert shown["memory_id"] is None
    assert shown["source_refs"] == []
    assert _digits(HIDDEN) not in _digits(shown["content_markdown"])
    assert shown["content_markdown"].startswith("One line") and shown["content_markdown"].endswith("and another.")


def test_an_owner_and_an_unbound_admin_key_keep_every_spelling() -> None:
    spellings = list(_spellings(HIDDEN).values())
    payload = {"candidate_memory_ids": spellings, "memory_id": HIDDEN.upper()}
    assert apply_hidden_id_withholding(_Store(), _identity("admin_agent"), payload) == payload
    assert apply_hidden_id_withholding(_Store(), None, payload) == payload


def test_a_token_is_removed_where_it_stands_and_its_neighbours_stay() -> None:
    other = VISIBLE.upper()
    text = f"- memory:{HIDDEN.upper()} says {{{HIDDEN}}} and urn:uuid:{HIDDEN}; [{other}] stays.\n"
    shown = _shown({"content_markdown": text})["content_markdown"]
    assert _digits(HIDDEN) not in _digits(shown)
    assert other in shown and " says " in shown and shown.endswith(" stays.\n")


def test_a_run_of_hex_digits_that_is_not_an_id_is_not_taken_for_one() -> None:
    digest = "ab" * 32
    longer = HIDDEN.replace("-", "") + "ab"
    shown = _shown({"content_markdown": f"{digest} {longer} {HIDDEN[:-1]}", "source_refs": [digest, longer]})
    assert shown["content_markdown"] == f"{digest} {longer} {HIDDEN[:-1]}"
    assert shown["source_refs"] == [digest, longer]


# -- item 2: any field name, any depth, a prefix, a key ----------------------------------------------------------------


@pytest.mark.parametrize(
    "build,expected",
    [
        (lambda i: {"see_also": i}, {"see_also": None}),
        (lambda i: {"related_memory": i}, {"related_memory": None}),
        (lambda i: {"links": {"memory": i, "kept": VISIBLE}}, {"links": {"memory": None, "kept": VISIBLE}}),
        (lambda i: {"refs": [{"id": i, "label": "a"}, {"id": VISIBLE}]}, {"refs": [{"id": None, "label": "a"}, {"id": VISIBLE}]}),
        (lambda i: {"a": {"b": {"c": [{"d": [i, VISIBLE]}]}}}, {"a": {"b": {"c": [{"d": [VISIBLE]}]}}}),
        (lambda i: {"notes": [[i], [VISIBLE]]}, {"notes": [[], [VISIBLE]]}),
        (lambda i: {i: {"note": "n"}, "other": 1}, {"other": 1}),
        (lambda i: {"related": {i.upper(): [1, 2], "ok": True}}, {"related": {"ok": True}}),
    ],
)
@pytest.mark.parametrize("wrap", ["{}", "memory:{}", "source:{}", "urn:uuid:{}", "MEMORY:{}", "artifact:{}", "alice://memories/{}"])
def test_an_id_that_is_the_whole_value_is_removed_under_any_key(build, expected, wrap: str) -> None:
    shown = _shown(build(wrap.format(HIDDEN)))
    assert shown == expected


def test_an_id_inside_free_text_is_not_looked_for() -> None:
    """Ids in the writer's own words are out of scope. The row's own label governs them."""

    payload = {
        "title": f"Notes on {HIDDEN}",
        "note": f"see {HIDDEN} for details",
        "summary": [f"{HIDDEN} was archived", "other"],
        "metadata_json": {"explanation": f"Shares terms with {HIDDEN}"},
    }
    assert _shown(payload) == payload


def test_the_id_of_a_readable_row_stays_wherever_it_stands() -> None:
    payload = {
        "see_also": VISIBLE,
        "candidate_memory_ids": [VISIBLE, f"memory:{SECOND_VISIBLE}"],
        "source_refs": [f"source:{VISIBLE_SOURCE}", {"memory_id": VISIBLE, "quote": "q"}],
        "links": {VISIBLE: 1},
    }
    assert _shown(payload) == payload


def test_a_value_that_is_not_an_id_is_left_alone() -> None:
    payload = {
        "memory_id": "not-an-id-but-long-enough-to-be-checked-by-length",
        "see_also": "a" * 31,
        "candidate_memory_ids": ["notes from the standup", "x" * 64],
        "n": 7,
        "flag": True,
        "nothing": None,
    }
    assert _shown(payload) == payload


# -- item 3: a hidden row and a missing row leave one answer ----------------------------------------------------------


def _typed_payload(row_id: str) -> dict:
    return {
        "candidate_memory_ids": [row_id, VISIBLE],
        "memory_id": row_id,
        "artifact_id": row_id,
        "output_artifact_id": row_id,
        "source_refs": [
            f"memory:{row_id}",
            f"source:{row_id}",
            row_id,
            row_id.upper(),
            {"memory_id": row_id, "quote": "q"},
            {"origin": f"artifact:{row_id}"},
            [row_id],
        ],
        "metadata_json": {
            "derived_from": {"memories": [row_id, VISIBLE], "counts": {"memories": 2}},
            "member_snapshots": [{"id": row_id, "status": "active"}, {"id": VISIBLE}],
            "consolidation": {"cluster_member_ids": [row_id, VISIBLE]},
            "rollup": {"instances": [{"memory_id": row_id}, {"memory_id": VISIBLE}]},
        },
        "related": f"memory:{row_id}",
    }


@pytest.mark.parametrize("profile", ["trusted_local_agent", "read_only_agent", "memory_proposal_agent"])
def test_a_hidden_id_and_a_missing_id_leave_the_same_answer(profile: str) -> None:
    hidden = _shown(_typed_payload(HIDDEN), profile=profile)
    missing = _shown(_typed_payload(MISSING), profile=profile)
    assert hidden == missing
    assert HIDDEN not in json.dumps(hidden) and MISSING not in json.dumps(missing)
    assert hidden["candidate_memory_ids"] == [VISIBLE]
    assert hidden["source_refs"] == [{"memory_id": None, "quote": "q"}, {}, []]


def test_an_id_that_names_no_row_at_a_position_the_product_does_not_type_stays() -> None:
    """The filter cannot tell such an id from the id of an event, a task or a chunk. This is the boundary the docs state."""

    shown = _shown({"see_also": MISSING, "event": {"id": MISSING}, "metadata_json": {"chunk": MISSING}})
    assert shown == {"see_also": MISSING, "event": {"id": MISSING}, "metadata_json": {"chunk": MISSING}}
    assert _shown({"see_also": HIDDEN, "event": {"id": HIDDEN}})["see_also"] is None


def test_a_limited_key_loses_every_id_kind_and_the_owner_keeps_them() -> None:
    store = _Store()
    limited = apply_hidden_id_withholding(store, _identity("trusted_local_agent"), _payload())
    assert HIDDEN not in limited["items"][0]["metadata_json"]["candidate_memory_ids"]
    assert SOURCE not in "".join(str(item) for item in limited["source_refs"])
    assert f"memory:{VISIBLE}" in limited["source_refs"]
    assert limited["artifact_id"] is None
    assert LOOP not in limited["open_loop_ids"]
    owner = apply_hidden_id_withholding(store, None, _payload())
    admin = apply_hidden_id_withholding(store, _identity("admin_agent"), _payload())
    assert owner["artifact_id"] == ARTIFACT
    assert admin["artifact_id"] == ARTIFACT
    assert owner is not None and admin["member_snapshots"][0]["id"] == HIDDEN


def test_the_id_of_a_belief_is_left_as_written() -> None:
    """Beliefs are not one of the rows the filter resolves: its lookups are for memories, sources, artifacts and open loops."""

    payload = {
        "belief_ids": [BELIEF, VISIBLE_BELIEF, MISSING],
        "beliefs": [BELIEF],
        "derived_from": {"beliefs": [BELIEF, VISIBLE_BELIEF], "counts": {"beliefs": 2}},
        "source_refs": [f"belief:{BELIEF}", f"belief:{MISSING}"],
    }
    assert _shown(payload) == payload


def test_a_payload_with_nothing_to_withhold_is_the_same_object() -> None:
    payload = {"candidate_memory_ids": [VISIBLE], "items": [{"id": VISIBLE}], "n": 1}
    assert _shown(payload) is payload


# -- item 4: counts ---------------------------------------------------------------------------------------------------


def test_a_count_follows_the_list_the_filter_shortened() -> None:
    payload = {
        "derived_from": {
            "v": 1,
            "memories": [HIDDEN, VISIBLE, SECOND_VISIBLE],
            "sources": [SOURCE, VISIBLE_SOURCE],
            "open_loops": [],
            "counts": {"memories": 3, "sources": 2, "open_loops": 0},
        },
        "input_summary": {"memory_ids": [HIDDEN, VISIBLE], "source_ids": [VISIBLE_SOURCE], "counts": {"memories": 2, "sources": 1}},
        "memory_ids": [HIDDEN, VISIBLE, SECOND_VISIBLE],
        "source_ids": [SOURCE, VISIBLE_SOURCE],
        "input_counts": {"memories": 3, "sources": 2, "artifacts": 6, "active_memories": 9},
        "memory_count": 3,
        "count": 3,
    }
    shown = _shown(payload)
    assert shown["derived_from"]["memories"] == [VISIBLE, SECOND_VISIBLE]
    assert shown["derived_from"]["counts"] == {"memories": 2, "sources": 1, "open_loops": 0}
    assert shown["input_summary"]["counts"] == {"memories": 1, "sources": 1}
    assert shown["input_counts"] == {"memories": 2, "sources": 1, "artifacts": 6, "active_memories": 9}
    assert shown["memory_count"] == 2
    # A bare count beside several lists is recomputed only when it equals the old length of one of them.
    assert shown["count"] == 2


def test_member_instance_and_displayed_counts_follow_the_roll_up() -> None:
    rollup = {
        "member_count": 3,
        "displayed_instance_count": 2,
        "instances_truncated": True,
        "grouping_input_count": 7,
        "member_ids": [HIDDEN, VISIBLE, SECOND_VISIBLE],
        "instances": [{"memory_id": HIDDEN, "text": "t"}, {"memory_id": VISIBLE, "text": "t"}],
    }
    shown = _shown({"value": {"rollup": rollup}})["value"]["rollup"]
    assert shown["member_ids"] == [VISIBLE, SECOND_VISIBLE]
    assert shown["instances"] == [{"memory_id": VISIBLE, "text": "t"}]
    assert shown["member_count"] == 2
    assert shown["displayed_instance_count"] == 1
    assert shown["instances_truncated"] is True
    # What the grouping read is a statistic of the report, not a count of a list the filter shortens.
    assert shown["grouping_input_count"] == 7


def test_a_roll_up_proposal_counts_its_members_as_instances() -> None:
    proposal = {
        "instance_count": 3,
        "displayed_instance_count": 2,
        "instances_truncated": True,
        "member_ids": [VISIBLE, HIDDEN, SECOND_VISIBLE],
        "source_refs": [f"memory:{VISIBLE}", f"memory:{HIDDEN}", f"memory:{SECOND_VISIBLE}"],
    }
    shown = _shown({"rollups": {"proposals": [proposal]}})["rollups"]["proposals"][0]
    assert shown["member_ids"] == [VISIBLE, SECOND_VISIBLE]
    assert shown["source_refs"] == [f"memory:{VISIBLE}", f"memory:{SECOND_VISIBLE}"]
    assert shown["instance_count"] == 2
    # Two instances were shown of three members. The filter cannot say which one was withheld, so it keeps no flag
    # that could tell a key how many members were hidden.
    assert "displayed_instance_count" not in shown
    assert "instances_truncated" not in shown


def test_an_instance_count_that_is_still_right_is_kept() -> None:
    value = {
        "member_count": 3,
        "displayed_instance_count": 2,
        "instances_truncated": True,
        "member_ids": [VISIBLE, SECOND_VISIBLE, HIDDEN],
        "instances": [{"memory_id": VISIBLE}, {"memory_id": SECOND_VISIBLE}],
    }
    shown = _shown({"rollup": value})["rollup"]
    assert shown["member_count"] == 2
    assert shown["displayed_instance_count"] == 2
    assert shown["instances_truncated"] is False


def test_the_stale_sweep_counts_follow_the_marked_ids() -> None:
    payload = {
        "stale_marked_memory_ids": [HIDDEN, VISIBLE],
        "input_counts": {"expired_marked": 2, "unconfirmed_marked": 0, "scanned": 5},
    }
    shown = _shown(payload)
    assert shown["stale_marked_memory_ids"] == [VISIBLE]
    # Both marked ids were counted as expired. One is withheld, so one is left.
    assert shown["input_counts"] == {"expired_marked": 1, "unconfirmed_marked": 0, "scanned": 5}
    split = _shown(
        {
            "stale_marked_memory_ids": [HIDDEN, VISIBLE],
            "input_counts": {"expired_marked": 1, "unconfirmed_marked": 1, "scanned": 5},
        }
    )
    # One was counted as expired and one as unconfirmed. The filter cannot say which one went, so it drops both.
    assert split["input_counts"] == {"scanned": 5}


def test_the_counts_of_a_regenerated_source_and_of_a_capture_follow_their_id_lists() -> None:
    regenerated = {
        "source_id": VISIBLE_SOURCE,
        "memory_ids": [HIDDEN, VISIBLE],
        "open_loop_ids": [LOOP, SECOND_VISIBLE],
        "memory_count": 2,
        "open_loop_count": 2,
    }
    assert _shown(regenerated) == {
        "source_id": VISIBLE_SOURCE,
        "memory_ids": [VISIBLE],
        "open_loop_ids": [SECOND_VISIBLE],
        "memory_count": 1,
        "open_loop_count": 1,
    }
    receipt = {"candidate_memory_ids": [HIDDEN, VISIBLE], "candidate_memory_count": 2, "chunk_count": 2}
    assert _shown(receipt) == {"candidate_memory_ids": [VISIBLE], "candidate_memory_count": 1, "chunk_count": 2}


def test_an_id_nested_fifty_levels_down_is_found() -> None:
    deep: object = [HIDDEN, VISIBLE]
    for level in range(50):
        deep = {f"level_{level}": deep}
    shown = _shown({"metadata_json": deep, "see_also": VISIBLE})
    node = shown["metadata_json"]
    for level in range(49, -1, -1):
        node = node[f"level_{level}"]
    assert node == [VISIBLE]


def test_a_count_of_a_list_that_lost_nothing_is_untouched() -> None:
    payload = {
        "derived_from": {"memories": [VISIBLE], "sources": [VISIBLE_SOURCE], "counts": {"memories": 1, "sources": 1}},
        "count": 2,
        "limit": 50,
        "memory_limit": 50,
    }
    assert _shown(payload) == payload


# -- no store is opened for a payload without an id --------------------------------------------------------------------


def test_no_store_is_opened_when_the_payload_holds_no_id_to_judge() -> None:
    opened: list[int] = []

    @contextmanager
    def open_store():
        opened.append(1)
        yield _Store()

    identity = _identity("trusted_local_agent")
    plain = {"items": [{"id": "not-an-id", "title": f"words {HIDDEN}"}], "count": 1, "digest": "ab" * 32}
    assert withhold_with(open_store, identity, plain) is plain
    assert opened == []
    assert judged_ids(plain) == set()
    assert withhold_with(open_store, identity, {"memory_ids": [HIDDEN]}) == {"memory_ids": []}
    assert opened == [1]
    # An id that stands only in the echo of the caller's own input is not judged either.
    echo = _echo(memory_ids={HIDDEN})
    assert withhold_with(open_store, identity, {"memory_ids": [HIDDEN]}, echo=echo) == {"memory_ids": [HIDDEN]}
    assert opened == [1]
    # The owner and an unbound admin key never open a store.
    assert withhold_with(open_store, None, {"memory_ids": [HIDDEN]}) == {"memory_ids": [HIDDEN]}
    assert withhold_with(open_store, _identity("admin_agent"), {"memory_ids": [HIDDEN]}) == {"memory_ids": [HIDDEN]}
    assert opened == [1]


# -- items 5 and 7 at the layer that reads the request ---------------------------------------------------------------


USER = UUID("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _request(*, identity, user_id=USER, query: str = "", path: str = "/v0/vnext/memories/forget") -> SimpleNamespace:
    state = SimpleNamespace(vnext_agent_identity=identity)
    if user_id is not None:
        state.vnext_authenticated_user_id = user_id
    return SimpleNamespace(
        state=state,
        url=SimpleNamespace(path=path, query=query),
        query_params={key: value for key, value in (pair.split("=") for pair in query.split("&") if pair)},
    )


@pytest.fixture
def connections(monkeypatch):
    opened: list[tuple[str, UUID]] = []
    store = _Store()

    @contextmanager
    def fake_connection(url, user_id):
        opened.append((url, user_id))
        yield object()

    import alicebot_api.db as db
    import alicebot_api.vnext_store as vnext_store

    monkeypatch.setattr(db, "user_connection", fake_connection)
    monkeypatch.setattr(vnext_store, "PostgresVNextStore", lambda _conn: store)
    return opened


def test_the_filter_reads_rows_for_the_user_the_key_was_authenticated_for(connections) -> None:
    """The query holds no user_id and the body is the response: only the authenticated user can tell whom to read for."""

    body = json.dumps({"memory": {"metadata_json": {"candidate_memory_ids": [HIDDEN, VISIBLE]}}}).encode()
    request = _request(identity=_identity("trusted_local_agent"), query="")
    updated = withhold_json_body(request, body, database_url="postgresql://db")
    assert updated is not None
    assert json.loads(updated)["memory"]["metadata_json"]["candidate_memory_ids"] == [VISIBLE]
    assert connections == [("postgresql://db", USER)]


def test_the_filter_ignores_a_user_id_in_the_query(connections) -> None:
    other = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    body = json.dumps({"memory_ids": [HIDDEN]}).encode()
    request = _request(identity=_identity("trusted_local_agent"), query=f"user_id={other}")
    assert withhold_json_body(request, body, database_url="postgresql://db") is not None
    assert connections == [("postgresql://db", USER)]


def test_a_limited_key_with_no_authenticated_user_is_refused_not_passed_through(connections) -> None:
    body = json.dumps({"memory_ids": [HIDDEN]}).encode()
    request = _request(identity=_identity("trusted_local_agent"), user_id=None, query=f"user_id={USER}")
    with pytest.raises(HiddenIdFilterError):
        withhold_json_body(request, body, database_url="postgresql://db")
    assert connections == []


def test_no_connection_is_opened_for_a_body_without_an_id(connections) -> None:
    identity = _identity("trusted_local_agent")
    for text in ({"detail": f"words {HIDDEN}", "count": 3, "digest": "ab" * 32}, {"items": []}, {}, []):
        assert withhold_json_body(_request(identity=identity), json.dumps(text).encode(), database_url="postgresql://db") is None
    assert connections == []
    body = json.dumps({"memory_ids": [VISIBLE]}).encode()
    assert withhold_json_body(_request(identity=identity), body, database_url="postgresql://db") is None
    assert len(connections) == 1


def test_a_body_without_an_id_needs_no_authenticated_user(connections) -> None:
    """Nothing is judged, so nothing is refused: the missing user matters only when there is an id to read rows for."""

    request = _request(identity=_identity("trusted_local_agent"), user_id=None)
    assert withhold_json_body(request, json.dumps({"items": [], "count": 0}).encode(), database_url="postgresql://db") is None
    assert connections == []


def test_the_owner_and_an_unbound_admin_key_open_no_connection(connections) -> None:
    body = json.dumps({"memory_ids": [HIDDEN]}).encode()
    for identity in (None, _identity("admin_agent")):
        assert withhold_json_body(_request(identity=identity), body, database_url="postgresql://db") is None
    assert connections == []


# -- an id the caller supplied is the echo of that input and no exemption -------------------------------------------------


def test_an_id_the_caller_supplied_is_not_an_exemption_elsewhere_in_the_answer() -> None:
    """A key that holds an id must not learn which report it is tied to by supplying it."""

    payload = {
        "target_id": HIDDEN,
        "metadata_json": {"candidate_memory_ids": [HIDDEN, VISIBLE], "source_refs": [f"memory:{HIDDEN}"], "see_also": HIDDEN},
        "items": [{"target_id": HIDDEN, "memory_id": HIDDEN}],
    }
    # An input that the answer has no key for exempts nothing.
    nothing = _shown(payload, echo=_echo(probe={HIDDEN}))
    assert HIDDEN not in json.dumps(nothing)
    assert nothing["target_id"] is None
    # An input named like a key repeats at that key, reached through objects, and nowhere else.
    shown = _shown(payload, echo=_echo(target_id={HIDDEN}))
    assert shown["target_id"] == HIDDEN
    assert shown["metadata_json"] == {"candidate_memory_ids": [VISIBLE], "source_refs": [], "see_also": None}
    assert shown["items"] == [{"target_id": None, "memory_id": None}]


def test_a_key_inside_a_list_of_rows_opens_no_scope_but_a_scope_covers_what_is_under_the_key() -> None:
    echo = _echo(source_refs={HIDDEN})
    rows = {"rows": [{"source_refs": [f"memory:{HIDDEN}"]}], "memory": {"value": {"source_refs": [f"memory:{HIDDEN}", f"memory:{SOURCE}"]}}}
    shown = _shown(rows, echo=echo)
    assert shown["rows"] == [{"source_refs": []}]
    # Under the key of the input, every list and object belongs to it, and only the ids the input held stay.
    assert shown["memory"]["value"]["source_refs"] == [f"memory:{HIDDEN}"]
    nested = {"source_refs": [[f"memory:{HIDDEN}"], {"memory_id": HIDDEN, "see_also": [HIDDEN]}]}
    assert _shown(nested, echo=echo)["source_refs"][0] == [f"memory:{HIDDEN}"]
    assert _shown(nested, echo=echo)["source_refs"][1] == {"memory_id": HIDDEN, "see_also": [HIDDEN]}


def test_the_echo_holds_only_the_ids_the_input_held() -> None:
    payload = {"source_refs": [f"memory:{HIDDEN}", f"memory:{SOURCE}", HIDDEN.upper(), SOURCE]}
    shown = _shown(payload, echo=_echo(source_refs={HIDDEN}))
    assert shown["source_refs"] == [f"memory:{HIDDEN}", HIDDEN.upper()]
    # Two inputs: the key of one does not repeat the ids of the other.
    both = {"target_id": HIDDEN, "source_refs": [f"memory:{SOURCE}", f"memory:{HIDDEN}"]}
    shown = _shown(both, echo=_echo(source_refs={HIDDEN}, target_id={SOURCE}))
    assert shown == {"target_id": None, "source_refs": [f"memory:{HIDDEN}"]}


def test_the_ids_that_stand_outside_the_echo_are_still_looked_up() -> None:
    echo = _echo(target_id={HIDDEN})
    assert judged_ids({"target_id": HIDDEN}, echo) == set()
    assert judged_ids({"target_id": HIDDEN, "items": [{"target_id": HIDDEN}]}, echo) == {HIDDEN}
    assert judged_ids({"target_id": HIDDEN, "see_also": HIDDEN}, echo) == {HIDDEN}
    assert judged_ids({"target_id": HIDDEN}, _echo(probe={HIDDEN})) == {HIDDEN}


def test_the_echo_of_a_tool_call_is_its_arguments_by_name() -> None:
    echo = echo_of_arguments({"memory_id": HIDDEN.upper(), "query": f"words {SOURCE}", "limit": 5, "flag": True, "none": None})
    assert echo == {"memory_id": frozenset({HIDDEN}), "query": frozenset({SOURCE})}
    assert echo_of_arguments(None) == {} and echo_of_arguments([HIDDEN]) == {}


def test_a_request_with_no_matched_route_supplies_nothing() -> None:
    request = SimpleNamespace(scope={"path_params": {"target_id": HIDDEN}}, query_params=SimpleNamespace(getlist=lambda _name: [HIDDEN]))
    assert echo_of_request(request, json.dumps({"source_refs": [HIDDEN]}).encode()) == {}
    assert echo_of_request(SimpleNamespace(), b"") == {}


class Thing(BaseModel):
    """The body of the POST route of the throwaway app. A field it does not declare is ignored."""

    model_config = ConfigDict(extra="ignore")
    user_id: str | None = None
    source_refs: list[object] = []
    memory_id: str | None = None


def _layer(*, identity, answer, method: str = "GET", path: str, query: bytes = b"", body: dict | None = None):
    """The withhold layer of the application in front of a throwaway app, called as a client calls it.

    The app has one route with a path parameter and two declared query parameters, and one that takes a JSON body with
    three declared fields. A request's undeclared query parameters and body fields reach neither. ``answer`` builds what
    the route returns from the inputs the route read.
    """

    import anyio
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    import alicebot_api.main as main_module

    app = FastAPI()

    @app.get("/v0/vnext/things/{target_id}")
    def get_thing(target_id: str, artifact_id: str | None = None, limit: int = 10):
        return JSONResponse(answer({"target_id": target_id, "artifact_id": artifact_id}))

    @app.post("/v0/vnext/things")
    def post_thing(thing: Thing):
        return JSONResponse(answer(thing.model_dump()))

    class Gate(BaseHTTPMiddleware):
        """What the vNext gate records for the layer outside it."""

        async def dispatch(self, request, call_next):
            request.state.vnext_agent_identity = identity
            request.state.vnext_authenticated_user_id = USER
            if request.method == "POST":
                request.scope.setdefault("state", {})["withhold_named_body"] = await request.body()
            return await call_next(request)

    app.add_middleware(Gate)
    app.add_middleware(BaseHTTPMiddleware, dispatch=main_module._withhold_hidden_ids)
    raw = json.dumps(body).encode() if body is not None else b""
    messages: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query,
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    answer_bytes = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), json.loads(answer_bytes)


def _row_answer(supplied: dict) -> dict:
    """A route that repeats its inputs in an echo and lists a hidden id in a result list and in its metadata."""

    return {
        "target_id": supplied["target_id"],
        "filters": {"artifact_id": supplied["artifact_id"]},
        "items": [{"artifact_id": supplied["artifact_id"], "target_id": supplied["target_id"], "candidate_memory_ids": [HIDDEN]}],
        "metadata_json": {"candidate_memory_ids": [HIDDEN, VISIBLE], "source_refs": [f"memory:{HIDDEN}"], "see_also": HIDDEN},
    }


def test_a_query_parameter_the_route_does_not_declare_supplies_nothing(connections) -> None:
    """The route declares ``artifact_id`` and ``limit``. A parameter that nothing reads exempts nothing, also when its name
    is the name of a key of the answer."""

    identity = _identity("trusted_local_agent")
    seen = []
    for query in (
        b"",
        f"probe={HIDDEN}".encode(),
        f"probe={HIDDEN.upper()}&other=memory:{HIDDEN}&limit=3".encode(),
        f"candidate_memory_ids={HIDDEN}&source_refs=memory:{HIDDEN}&see_also={HIDDEN}&items={HIDDEN}&metadata_json={HIDDEN}".encode(),
    ):
        status, body = _layer(identity=identity, answer=_row_answer, path=f"/v0/vnext/things/{VISIBLE}", query=query)
        assert status == 200
        assert HIDDEN not in json.dumps(body).lower(), query
        seen.append(body)
    assert seen[0] == seen[1] == seen[2] == seen[3]
    assert seen[0]["metadata_json"] == {"candidate_memory_ids": [VISIBLE], "source_refs": [], "see_also": None}


def test_a_declared_parameter_is_echoed_where_the_answer_repeats_it_and_nowhere_else(connections) -> None:
    identity = _identity("trusted_local_agent")
    status, body = _layer(
        identity=identity, answer=_row_answer, path=f"/v0/vnext/things/{HIDDEN}", query=f"artifact_id={ARTIFACT}&limit=2".encode()
    )
    assert status == 200
    # The path parameter and the declared query parameter come back where the answer repeats them.
    assert body["target_id"] == HIDDEN
    assert body["filters"] == {"artifact_id": ARTIFACT}
    # The same ids stand in a list of result rows and in the metadata. They are rows the caller may not read.
    assert body["items"] == [{"artifact_id": None, "target_id": None, "candidate_memory_ids": []}]
    assert body["metadata_json"] == {"candidate_memory_ids": [VISIBLE], "source_refs": [], "see_also": None}


def test_a_path_parameter_repeats_at_the_id_of_the_object_it_names_and_not_in_a_list_of_rows(connections) -> None:
    """The URL names a row, and the answer describes it. Archiving a source makes it unreadable, and its trace still
    carries the id the caller named.

    Mutation: a path parameter is registered under its own name only.
    """

    identity = _identity("trusted_local_agent")

    def answer(supplied: dict) -> dict:
        row = supplied["target_id"]
        return {
            "trace": {"source": {"id": row, "title": "t"}, "events": [{"id": row, "target_id": row}]},
            "id": row,
            "metadata_json": {"parent": {"id": HIDDEN}, "see_also": row},
        }

    status, body = _layer(identity=identity, answer=answer, path=f"/v0/vnext/things/{SOURCE}")
    assert status == 200
    assert body["id"] == SOURCE and body["trace"]["source"]["id"] == SOURCE
    # A list of rows is not the object the URL names, an id the path did not hold is withheld, and so is the same id at a
    # key that is neither the name of the parameter nor ``id``.
    assert body["trace"]["events"] == [{"id": None, "target_id": None}]
    assert body["metadata_json"] == {"parent": {"id": None}, "see_also": None}


def test_a_body_field_the_route_declares_is_echoed_and_one_it_does_not_is_not(connections) -> None:
    identity = _identity("trusted_local_agent")

    def answer(supplied: dict) -> dict:
        return {
            "memory": {
                "value": {"source_refs": supplied["source_refs"]},
                "metadata_json": {"candidate_memory_ids": [HIDDEN], "copy": [f"memory:{HIDDEN}"], "probe": HIDDEN},
            }
        }

    body = {"user_id": str(USER), "source_refs": [f"memory:{HIDDEN}"], "probe": HIDDEN, "candidate_memory_ids": [HIDDEN]}
    status, shown = _layer(identity=identity, answer=answer, method="POST", path="/v0/vnext/things", body=body)
    assert status == 200
    assert shown["memory"]["value"]["source_refs"] == [f"memory:{HIDDEN}"]
    # Copies of the same id under other names are not the echo of the input.
    assert shown["memory"]["metadata_json"] == {"candidate_memory_ids": [], "copy": [], "probe": None}
    # A field that is not declared supplies nothing, so it exempts nothing.
    only_probe = {"user_id": str(USER), "probe": HIDDEN, "candidate_memory_ids": [HIDDEN], "copy": [f"memory:{HIDDEN}"]}
    status, shown = _layer(identity=identity, answer=answer, method="POST", path="/v0/vnext/things", body=only_probe)
    assert HIDDEN not in json.dumps(shown)


def test_a_commit_that_names_a_hidden_id_and_one_that_names_a_missing_id_have_one_echo(connections) -> None:
    identity = _identity("trusted_local_agent")

    def answer(supplied: dict) -> dict:
        return {"memory": {"value": {"source_refs": supplied["source_refs"]}, "metadata_json": {"candidate_memory_ids": []}}}

    shown = {}
    for label, row_id in (("hidden", HIDDEN), ("missing", MISSING)):
        body = {"user_id": str(USER), "source_refs": [f"memory:{row_id}", {"memory_id": row_id, "quote": "q"}]}
        status, shown[label] = _layer(identity=identity, answer=answer, method="POST", path="/v0/vnext/things", body=body)
        assert status == 200
    assert json.dumps(shown["hidden"]).replace(HIDDEN, "<ID>") == json.dumps(shown["missing"]).replace(MISSING, "<ID>")
    assert HIDDEN in json.dumps(shown["hidden"]) and MISSING in json.dumps(shown["missing"])


# -- a long run of prefixes costs time in proportion to its length ---------------------------------------------------------


class _Recorder:
    """Stands in for a compiled pattern and records which string each match was asked about, and where."""

    def __init__(self, pattern) -> None:
        self.pattern = pattern
        self.calls: list[tuple[int, int, int]] = []

    def match(self, string, position=0):
        self.calls.append((id(string), len(string), position))
        return self.pattern.match(string, position)


@pytest.mark.parametrize("function_name", ["_whole_id", "_leading_id"])
def test_a_run_of_kind_prefixes_is_matched_in_place_and_copies_nothing(monkeypatch, function_name: str) -> None:
    """Counted, not timed. Each prefix is one match at a later index of the same string, so the work is the number of
    prefixes. Slicing the rest of the string after every prefix copies it again each time, and the work grows with the
    square of the length: 1.4 MB took two seconds, and 700 KB took more than three on the route.

    Mutation: ``_kind_prefixes_end`` matches against ``text[position:]`` again, as the loop did.
    """

    from alicebot_api import vnext_hidden_ids as module

    function = getattr(module, function_name)
    matches = {}
    for count in (200, 2000):
        text = "source:" * count + HIDDEN
        recorder = _Recorder(module._LEADING_KIND)
        monkeypatch.setattr(module, "_LEADING_KIND", recorder)
        assert function(text) == (HIDDEN, True)
        assert {call[0] for call in recorder.calls} == {id(text)}, "the string was copied"
        assert {call[1] for call in recorder.calls} == {len(text)}
        assert len(recorder.calls) == count + 1
        matches[count] = len(recorder.calls)
    assert matches[2000] <= 11 * matches[200]


class _Text(str):
    """A string that counts the slices taken of it."""

    slices: list[tuple[int | None, int | None]] = []

    def __getitem__(self, key):
        if isinstance(key, slice):
            _Text.slices.append((key.start, key.stop))
        return str.__getitem__(self, key)


def test_the_prefixes_before_a_printed_id_are_read_backwards_once() -> None:
    """Counted, not timed. A pattern that repeats a group of prefixes in front of the id starts again at every prefix
    of a run, so a run of ten thousand takes the square of that. The prefixes are read backwards from the id instead.

    Mutation: ``content_markdown`` is scanned with a pattern that repeats the group of prefixes before the id.
    """

    work = {}
    for count in (100, 1000):
        _Text.slices = []
        line = _Text("source:" * count + HIDDEN + " and the rest.")
        shown = strip_hidden_ids({"content_markdown": line}, {HIDDEN}, readable=set())
        assert shown["content_markdown"] == " and the rest."
        compared = [bounds for bounds in _Text.slices if bounds[0] is not None and bounds[1] is not None and bounds[1] - bounds[0] <= 17]
        assert len(compared) >= count
        work[count] = len(compared)
    assert work[1000] <= 11 * work[100]


def test_other_prefix_runs_are_linear_too() -> None:
    """A ref made of one prefix repeated, the key of a dict, and a line with many ids each preceded by a prefix."""

    from alicebot_api import vnext_hidden_ids as module

    for count in (50, 500):
        recorder = _Recorder(module._LEADING_KIND)
        original = module._LEADING_KIND
        module._LEADING_KIND = recorder
        try:
            shown = _shown({"source_refs": ["source:" * count + HIDDEN], "x": {"source:" * count + HIDDEN: 1}})
        finally:
            module._LEADING_KIND = original
        assert shown == {"source_refs": [], "x": {}}
        assert len({call[0] for call in recorder.calls}) <= 8  # the strings the filter was given, none of them copied
    many = " ".join(f"uuid:{HIDDEN}" for _ in range(3000))
    assert _shown({"content_markdown": many})["content_markdown"] == " " * 2999


def test_json_text_nested_past_the_decoder_is_read_as_text() -> None:
    """The decoder raises RecursionError for text nested this deep. That must not bring the filter down."""

    deep = "[" * 90_000
    payload = {"source_refs": [deep, deep + f" {HIDDEN}"], "note": "kept"}
    assert judged_ids(payload) == {HIDDEN}
    assert _shown(payload) == {"source_refs": [deep], "note": "kept"}


def test_a_response_header_that_repeats_stays_repeated() -> None:
    """The layer rebuilds the response after it reads the body. Two Set-Cookie headers are two headers."""

    import anyio
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
    from starlette.routing import Route

    import alicebot_api.main as main_module

    def route(_request):
        response = Response(b'{"ok": true}', media_type="application/json")
        for name, value in (
            (b"set-cookie", b"a=1"),
            (b"set-cookie", b"b=2"),
            (b"vary", b"Origin"),
            (b"vary", b"Accept"),
        ):
            response.raw_headers.append((name, value))
        return response

    app = Starlette(routes=[Route("/v0/vnext/probe", route)])
    app.add_middleware(BaseHTTPMiddleware, dispatch=main_module._withhold_hidden_ids)
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v0/vnext/probe",
        "raw_path": b"/v0/vnext/probe",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    headers = [(name.lower(), value) for name, value in start["headers"]]
    assert [value for name, value in headers if name == b"set-cookie"] == [b"a=1", b"b=2"]
    assert [value for name, value in headers if name == b"vary"] == [b"Origin", b"Accept"]
    assert [value for name, value in headers if name == b"content-length"] == [b"12"]
    assert [value for name, value in headers if name == b"content-type"] == [b"application/json"]


def test_a_response_with_no_body_gets_no_content_length() -> None:
    """A 204 has no body and no length. The layer rebuilds the response and must not add one."""

    import anyio
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
    from starlette.routing import Route

    import alicebot_api.main as main_module

    app = Starlette(routes=[Route("/v0/vnext/probe", lambda _request: Response(status_code=204))])
    app.add_middleware(BaseHTTPMiddleware, dispatch=main_module._withhold_hidden_ids)
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/v0/vnext/probe", "raw_path": b"/v0/vnext/probe", "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    assert start["status"] == 204
    assert [name for name, _value in start["headers"] if name.lower() == b"content-length"] == []



@pytest.fixture
def clean_reader():
    """Run a test with no reader remembered, and leave none behind for the next test."""

    from alicebot_api.vnext_hidden_ids import _READER

    token = _READER.set(None)
    try:
        yield
    finally:
        _READER.reset(token)


def test_a_reader_a_command_remembers_ends_with_the_command(monkeypatch, clean_reader) -> None:
    """The remembered reader names a database, a user and a key. The next command must not inherit it.

    Mutation: ``runner.main`` calls the handler outside ``reader_scope``. The reader outlives the command and the next
    command sees it (the contention test of the label lock then opens a connection to a database that was dropped).
    """

    from argparse import Namespace

    from alicebot_api.cli import runner
    from alicebot_api.vnext_hidden_ids import reader_for_dump, remember_reader

    identity = _identity("trusted_local_agent")
    seen: list[tuple] = []

    def handler(_ctx, _args) -> str:
        remember_reader(SimpleNamespace(database_url="postgresql://db", user_id=USER), identity)
        seen.append(reader_for_dump())
        return "done"

    class Parser:
        def __init__(self, run):
            self.run = run

        def parse_args(self, _argv):
            return Namespace(handler=self.run)

    monkeypatch.setattr(runner, "build_parser", lambda: Parser(handler))
    monkeypatch.setattr(runner, "_validate_arguments", lambda _args: None)
    monkeypatch.setattr(runner, "_build_context", lambda _args: object())
    assert runner.main([]) == 0
    assert seen == [("postgresql://db", str(USER), identity)]
    assert reader_for_dump() == (None, None, None)
    # A reader left by code that ran no command is not seen by the command either.
    remember_reader(SimpleNamespace(database_url="postgresql://stale", user_id=USER), identity)
    seen.clear()

    def next_handler(_ctx, _args) -> str:
        seen.append(reader_for_dump())
        return "done"

    monkeypatch.setattr(runner, "build_parser", lambda: Parser(next_handler))
    assert runner.main([]) == 0
    assert seen == [(None, None, None)]
