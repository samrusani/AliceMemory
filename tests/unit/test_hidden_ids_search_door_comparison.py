"""A search door is compared by the kind of row it wrote, because its pack depends on how the id happens to read.

The matrix calls the recall, context-pack, resume and recent-decisions tools with the id of a hidden row as the text of a
query, and with the id of a row that does not exist, and asks that they write the same kinds of row. A random id that reads
as a date or a number, or shares a term with a stored row, gets another pack: other counts, another token estimate, a
warning in one and none in the other. Comparing the payload text made the matrix fail about one run in thirty. These tests
pin what the comparison keeps and what it leaves out.
"""
from __future__ import annotations

import json

from tests.integration.hidden_ids_postgres_support import changes


def _sqlite_event(event_type="agent.context_pack_requested", payload=None):
    return json.dumps(["id", "user", event_type, "agent", "key", "context_pack", "target", "2026-10-09T10:00:00Z", json.dumps(payload or {})])


def _postgres_event(event_type="agent.context_pack_requested", payload=None):
    return json.dumps(
        {"event_type": event_type, "actor_type": "agent", "target_type": "context_pack", "payload_json": payload or {}}, sort_keys=True
    )


def _written(row, table="event_log", **options):
    return changes({table: []}, {table: [row]}, **options)


def test_two_packs_with_different_counts_and_warnings_are_the_same_kind_of_event():
    for make in (_sqlite_event, _postgres_event):
        small = make(payload={"budget": {"token_estimate": 764}, "selected_count": 0, "warnings": ["no_relevant_memories_selected"]})
        large = make(payload={"budget": {"token_estimate": 1054}, "selected_count": 3, "warnings": []})
        assert small != large
        assert _written(small, search=True) == _written(large, search=True), make.__name__
        # Compared as text, as every other door is, they differ.
        assert _written(small) != _written(large), make.__name__


def test_an_event_of_another_type_actor_or_target_is_not_the_same_kind():
    for make in (_sqlite_event, _postgres_event):
        assert _written(make(), search=True) != _written(make(event_type="policy.decision"), search=True), make.__name__
    other_target = json.loads(_postgres_event())
    other_target["target_type"] = "memory"
    assert _written(_postgres_event(), search=True) != _written(json.dumps(other_target, sort_keys=True), search=True)
    other_actor = json.loads(_postgres_event())
    other_actor["actor_type"] = "user"
    assert _written(_postgres_event(), search=True) != _written(json.dumps(other_actor, sort_keys=True), search=True)


def test_an_added_and_a_removed_event_stay_apart_and_a_missing_event_is_a_difference():
    row = _postgres_event()
    assert changes({"event_log": [row]}, {"event_log": []}, search=True) != _written(row, search=True)
    assert changes({"event_log": []}, {"event_log": []}, search=True) == {}
    assert _written(row, search=True) != {}


def test_a_row_of_another_table_is_compared_with_its_numbers_replaced_and_its_words_kept():
    first = json.dumps({"id": "7", "title": "note", "count": 3, "ratio": 0.5})
    second = json.dumps({"id": "7", "title": "note", "count": 9, "ratio": 0.25})
    renamed = json.dumps({"id": "7", "title": "other", "count": 3, "ratio": 0.5})
    assert _written(first, table="open_loops", search=True) == _written(second, table="open_loops", search=True)
    assert _written(first, table="open_loops", search=True) != _written(renamed, table="open_loops", search=True)
    assert _written(first, table="open_loops") != _written(second, table="open_loops")


def test_a_row_that_is_not_json_is_compared_as_it_is():
    assert _written("not json", search=True) == _written("not json", search=True)
    assert _written("not json", search=True) != _written("other text", search=True)
