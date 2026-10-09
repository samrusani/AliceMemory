"""An event is shown to a caller with limits only when every row it names may be read by that caller.

An event names the row it is about (its target) and, in its payload, the rows it was made with or acted on. The event feed,
the agent feed, the context tree, the dogfooding view and the workspace event count all go through ``admit_events`` and
``readable_event_count``, and these tests pin that both judge the same references:

* a chunk of a source has no label, so its event takes the label of the source its payload names, and an event that names
  no source is not shown;
* a payload field that holds the id of a labelled row is judged whatever the target is (an entity, a task, a connector, a
  scheduler run, a memory);
* a graph edge has no label, so it is shown when each of its labelled ends is (a source, a memory or a belief), and an end of
  any other kind, or one that is not stored, is not;
* an id that an event repeats from the client, in capitals or with spaces round it, is judged as the row it names, and an
  id that is not a UUID is not;
* the count query and the list query read the same fields, on SQLite and (in the integration test) on PostgreSQL;
* the writers of events are swept, through the variables and helper functions that build a payload: a new target type or a
  new id field fails the sweep until it is classified here.
"""
from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_guard import (
    LabelGuard,
    event_payload_references,
    event_references,
    event_target_references,
    label_read_scope,
)
from alicebot_api.vnext_label_sql import (
    EVENT_CHILD_TARGETS,
    EVENT_EDGE_TARGET,
    EVENT_PAYLOAD_REFERENCES,
    EVENT_REFERENCE_KEYS,
    EVENT_TARGET_KINDS,
    EVENT_TYPE_REFERENCES,
    event_references_sql,
)
from alicebot_api.vnext_label_writes import without_insert_floor

USER = "11111111-1111-4111-8111-111111111111"
SRC = "11111111-aaaa-4aaa-8aaa-111111111111"
MEM = "22222222-bbbb-4bbb-8bbb-222222222222"


def _event(event_type, target_type=None, target_id=None, **payload):
    return {"event_type": event_type, "target_type": target_type, "target_id": target_id, "payload_json": payload}


# --- what an event names -------------------------------------------------------------------------------------------


def test_a_chunk_event_names_the_source_in_its_payload():
    assert event_references(_event("source_chunk.created", "source_chunk", str(uuid4()), operation="create", source_id=SRC)) == [
        ("source", SRC)
    ]


def test_a_chunk_event_that_names_no_source_names_a_source_that_cannot_be_resolved():
    for payload in ({}, {"source_id": None}, {"source_id": ""}, {"source_id": []}, {"source_id": "  "}):
        assert event_references(_event("source_chunk.created", "source_chunk", str(uuid4()), **payload)) == [("source", "")], payload
    assert event_references({"event_type": "source_chunk.created", "target_type": "source_chunk"}) == [("source", "")]


def test_a_target_of_a_labelled_kind_is_named_and_a_missing_target_id_cannot_be_resolved():
    for kind in sorted(EVENT_TARGET_KINDS):
        assert event_references(_event(f"{kind}.updated", kind, MEM)) == [(kind, MEM)]
        assert event_references(_event(f"{kind}.updated", kind, None)) == [(kind, "")]
    assert event_references(_event("graph_edge.created", EVENT_EDGE_TARGET, MEM)) == [("edge", MEM)]


def test_payload_fields_name_a_row_whatever_the_target_is():
    cases = (
        (_event("entity.mention_recorded", "entity", MEM, source_id=SRC), [("source", SRC)]),
        (_event("entity.relationship_changed", "entity", MEM, source_id=SRC, relationship_event_id=str(uuid4())), [("source", SRC)]),
        (_event("scheduler.run_succeeded", "scheduler_run", MEM, artifact_id=SRC), [("artifact", SRC)]),
        (_event("queue.task_completed", "task", MEM, artifact_id=SRC), [("artifact", SRC)]),
        (_event("connector.item_imported", None, None, source_id=SRC, external_id="x"), [("source", SRC)]),
        (_event("agent.output_ingested", "connector", "agent_output", source_id=SRC, artifact_id=MEM, memory_id=MEM),
         [("source", SRC), ("memory", MEM), ("artifact", MEM)]),
        (_event("memory.quarantine_sweep", "agent", MEM, expired_memory_ids=[SRC, MEM]), [("memory", SRC), ("memory", MEM)]),
        (_event("contradiction.candidate_edge_logged", "graph_edge", MEM, belief_id=SRC), [("edge", MEM), ("belief", SRC)]),
        (_event("project.update_candidate_accepted", "project", MEM, artifact_id=SRC, candidate_memory_id=MEM),
         [("project", MEM), ("memory", MEM), ("artifact", SRC)]),
        (_event("memory.reviewed", "memory", MEM, resolved_action="supersede", replacement_memory_id=SRC),
         [("memory", MEM), ("memory", SRC)]),
        (_event("demo.dataset_loaded", "demo_dataset", "d", source_ids=[SRC], artifact_ids=[MEM], project_ids=[SRC]),
         [("source", SRC), ("artifact", MEM), ("project", SRC)]),
    )
    for event, expected in cases:
        assert sorted(event_references(event)) == sorted(expected), event["event_type"]


LISTED_PAYLOAD_FIELDS = {
    "source_id": "source", "source_ids": "source", "memory_id": "memory", "candidate_memory_id": "memory",
    "candidate_memory_ids": "memory", "rollup_candidate_ids": "memory", "expired_memory_ids": "memory",
    "superseded_member_ids": "memory", "member_id": "memory", "replacement_memory_id": "memory", "artifact_id": "artifact",
    "artifact_ids": "artifact", "belief_id": "belief", "project_id": "project", "project_ids": "project",
}


def test_every_listed_payload_field_is_read_as_the_kind_of_row_it_names():
    """One field at a time, in an event whose target says nothing, so that removing a field from the list fails here.

    The list is written out here as well as in the module: a test that walks the module's own list cannot see a field go.
    """
    assert EVENT_PAYLOAD_REFERENCES == LISTED_PAYLOAD_FIELDS
    for key, kind in LISTED_PAYLOAD_FIELDS.items():
        value = [SRC] if key.endswith("s") else SRC
        assert event_references(_event("entity.updated", "entity", MEM, **{key: value})) == [(kind, SRC)], key


def test_a_replacement_names_the_kind_of_row_the_event_is_about():
    assert event_references(_event("source.superseded", "source", SRC, superseded_by=MEM)) == [("source", SRC), ("source", MEM)]
    assert event_references(_event("belief.superseded", "belief", SRC, superseded_by=MEM)) == [("belief", SRC), ("belief", MEM)]
    for event_type in ("agent.memory_superseded", "agent.memory_undone"):
        assert event_references(_event(event_type, "memory", SRC, superseded_by=MEM)) == [("memory", SRC), ("memory", MEM)]
    # The same field in an event of another family is not read as a row.
    assert event_references(_event("connector.state_updated", "connector", "x", superseded_by=MEM)) == []


def test_a_payload_field_that_is_not_an_id_cannot_be_resolved_and_an_empty_one_names_nothing():
    for value in ({"id": SRC}, 5, True, [SRC, 5], [[SRC]]):
        assert event_references(_event("entity.mention_recorded", "entity", MEM, source_id=value)) == [("source", "")], value
    for value in (None, "", "  ", [], [None]):
        assert event_references(_event("entity.mention_recorded", "entity", MEM, source_id=value)) == [], value
    # A payload that is not an object names nothing.
    assert event_references({"event_type": "entity.created", "target_type": "entity", "payload_json": [SRC]}) == []
    assert event_references({"event_type": "entity.created", "target_type": "entity", "payload_json": None}) == []


def test_the_target_and_the_payload_are_read_apart_and_a_payload_id_keeps_the_spelling_the_event_holds():
    padded = f"  {MEM.upper()}  "
    event = _event("source.assigned_project", "source", SRC, project_id=padded)
    assert event_target_references(event) == [("source", SRC)]
    assert event_payload_references(event) == [("project", padded)]
    assert event_references(event) == [("source", SRC), ("project", padded)]
    for target in (None, "entity", "task", "scheduler_run"):
        assert event_target_references(_event("entity.created", target, MEM, source_id=SRC)) == []
    assert event_target_references(_event("graph_edge.created", EVENT_EDGE_TARGET, MEM, source_id=SRC)) == [("edge", MEM)]
    assert event_payload_references(_event("graph_edge.created", EVENT_EDGE_TARGET, MEM, source_id=SRC)) == [("source", SRC)]
    # A chunk event that names no source has the unresolved source as a payload reference, never as a target.
    assert event_target_references(_event("source_chunk.created", "source_chunk", MEM)) == []
    assert event_payload_references(_event("source_chunk.created", "source_chunk", MEM)) == [("source", "")]


def test_ids_of_rows_without_a_label_are_not_references():
    event = _event(
        "scheduler.artifact_created", "scheduler_run", MEM, scheduler_run_id=str(uuid4()), provenance_link_id=str(uuid4()),
        revision_id=str(uuid4()), quality_rating_id=str(uuid4()), confirmation_id=str(uuid4()), agent_id="a",
        project_scope=[SRC],
    )
    assert event_references(event) == []


def test_the_vocabulary_agrees_with_itself():
    assert set(EVENT_PAYLOAD_REFERENCES.values()) <= EVENT_TARGET_KINDS
    for fields in EVENT_TYPE_REFERENCES.values():
        assert set(fields.values()) <= EVENT_TARGET_KINDS
    assert {kind for kind, _key in EVENT_CHILD_TARGETS.values()} <= EVENT_TARGET_KINDS
    for _kind, key in EVENT_CHILD_TARGETS.values():
        assert EVENT_PAYLOAD_REFERENCES[key]  # the chunk's source field is read as a reference too
    assert set(EVENT_REFERENCE_KEYS) == {*EVENT_PAYLOAD_REFERENCES, *(k for f in EVENT_TYPE_REFERENCES.values() for k in f)}


# --- the SQL that cuts a payload down for the count query ------------------------------------------------------------


def test_the_count_query_keeps_exactly_the_fields_the_guard_reads():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE event_log (id TEXT, payload_json TEXT)")
    full = {key: [SRC] if key.endswith("s") else SRC for key in EVENT_REFERENCE_KEYS}
    noise = {"operation": "create", "changes": {"source_id": "nested"}, "agent_id": "a", "revision_id": SRC}
    rows = [("full", json.dumps({**full, **noise})), ("none", json.dumps(noise)), ("bad", "not json"), ("array", "[1]"), ("scalar", "5")]
    conn.executemany("INSERT INTO event_log VALUES (?, ?)", rows)
    cut = {name: value for name, value in conn.execute(f"SELECT id, {event_references_sql(sqlite=True)} FROM event_log")}
    assert json.loads(cut["full"]) == full
    # An event that holds none of the fields has nothing cut out of it, so no object is built for it.
    assert cut["none"] is None
    assert cut["bad"] is None and cut["array"] is None and cut["scalar"] is None
    # An event that holds one of them keeps it and nothing else, and the fields it lacks are null.
    conn.execute("INSERT INTO event_log VALUES ('one', ?)", (json.dumps({**noise, "memory_id": MEM}),))
    one = json.loads(next(value for name, value in conn.execute(f"SELECT id, {event_references_sql(sqlite=True)} FROM event_log") if name == "one"))
    assert one == {**{key: None for key in EVENT_REFERENCE_KEYS}, "memory_id": MEM}


# --- the guard on a real store ---------------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "events.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        yield store


def _row_ids(rows):
    return {str(row["id"]) for row in rows}


def make_event_vault(store, *, beliefs=False):
    """A readable and a hidden source, memory and chunk, and edges between them. Used by the PostgreSQL test too.

    ``beliefs`` stores a belief for each memory (the PostgreSQL store reads the labels of beliefs, the SQLite store does not).
    """
    create_edge = getattr(store, "create_edge", None) or store.create_graph_edge
    public = store.create_source({"source_type": "note", "title": "Visible", "content_hash": str(uuid4()), "sensitivity": "public", "domain": "project"})
    hidden = store.create_source({"source_type": "note", "title": "Hidden", "content_hash": str(uuid4()), "sensitivity": "confidential", "domain": "project"})
    with without_insert_floor():
        memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Visible fact", "status": "active", "domain": "project", "sensitivity": "public"})
        secret = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden fact", "status": "active", "domain": "project", "sensitivity": "confidential"})
    chunks = {
        "public": store.create_source_chunk({"source_id": str(public["id"]), "chunk_index": 0, "text": "visible chunk"}),
        "hidden": store.create_source_chunk({"source_id": str(hidden["id"]), "chunk_index": 0, "text": "hidden chunk"}),
    }

    def edge(from_type, from_id, to_id, to_type="memory"):
        return create_edge({"from_type": from_type, "from_id": from_id, "to_type": to_type, "to_id": to_id, "edge_type": "similar_to", "confidence": 0.9, "created_by": "synthetic"})

    edges = {
        "public": edge("source", str(public["id"]), str(memory["id"])),
        "hidden_end": edge("source", str(public["id"]), str(secret["id"])),
        "unknown_end": edge("source", str(public["id"]), str(uuid4())),
        "entity_end": edge("entity", str(uuid4()), str(memory["id"])),
        "entity_to_hidden": edge("entity", str(uuid4()), str(secret["id"])),
        # An end of a kind the guard cannot judge (an artifact, an open loop), whether or not the row is stored.
        "artifact_end": edge("artifact", str(uuid4()), str(memory["id"])),
        "open_loop_end": edge("source", str(public["id"]), str(uuid4()), to_type="open_loop"),
        "belief_not_stored": edge("source", str(public["id"]), str(uuid4()), to_type="belief"),
    }
    if beliefs:
        readable_belief = store.create_belief({"memory_id": str(memory["id"]), "claim": "Visible fact", "confidence": 0.9})
        hidden_belief = store.create_belief({"memory_id": str(secret["id"]), "claim": "Hidden fact", "confidence": 0.9})
        edges["belief_end"] = edge("source", str(public["id"]), str(readable_belief["id"]), to_type="belief")
        edges["belief_of_hidden_memory"] = edge("memory", str(memory["id"]), str(hidden_belief["id"]), to_type="belief")
    return public, hidden, memory, secret, chunks, edges


def _append(store, **record):
    return str(store.append_event(build_event_log_record(actor_type="system", **record))["id"])


def assert_events_are_judged_by_the_rows_they_name(store, *, oracle=None, beliefs=False):
    """Append events that name rows, then check which a caller with limits is shown and how many it is told of.

    ``oracle`` is a store that offers only the per-row reads, to compare the native count of PostgreSQL with. ``beliefs`` is
    true for a store that reads the labels of beliefs (PostgreSQL), so that an edge to a belief of a readable memory is shown.
    """
    public, hidden, memory, secret, chunks, edges = make_event_vault(store, beliefs=beliefs)
    pid, hid, mid, sid = (str(row["id"]) for row in (public, hidden, memory, secret))
    shown, withheld = {}, {}

    def case(name, expected, **record):
        (shown if expected else withheld)[name] = _append(store, **record)

    case("chunk of a readable source", True, event_type="source_chunk.created", target_type="source_chunk", target_id=str(chunks["public"]["id"]), payload={"operation": "create", "source_id": pid})
    case("chunk of a hidden source", False, event_type="source_chunk.created", target_type="source_chunk", target_id=str(chunks["hidden"]["id"]), payload={"operation": "create", "source_id": hid})
    case("chunk that names no source", False, event_type="source_chunk.created", target_type="source_chunk", target_id=str(uuid4()), payload={})
    case("chunk that names a source that is not stored", False, event_type="source_chunk.created", target_type="source_chunk", target_id=str(uuid4()), payload={"source_id": str(uuid4())})
    case("chunk that names something that is not an id", False, event_type="source_chunk.created", target_type="source_chunk", target_id=str(uuid4()), payload={"source_id": {"id": pid}})
    case("entity mention of a readable source", True, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"operation": "record_mention", "source_id": pid})
    case("entity mention of a hidden source", False, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"operation": "record_mention", "source_id": hid})
    case("import of a hidden source", False, event_type="connector.item_imported", payload={"connector_name": "x", "source_id": hid})
    case("import of a readable source", True, event_type="connector.item_imported", payload={"connector_name": "x", "source_id": pid})
    case("sweep of readable memories", True, event_type="memory.quarantine_sweep", target_type="agent", target_id="a", payload={"expired_memory_ids": [mid]})
    case("sweep that includes a hidden memory", False, event_type="memory.quarantine_sweep", target_type="agent", target_id="a", payload={"expired_memory_ids": [mid, sid]})
    case("readable memory replaced by a hidden one", False, event_type="agent.memory_superseded", target_type="memory", target_id=mid, payload={"superseded_by": sid})
    case("readable memory replaced by a readable one", True, event_type="agent.memory_superseded", target_type="memory", target_id=mid, payload={"superseded_by": mid})
    case("hidden memory", False, event_type="memory.updated", target_type="memory", target_id=sid, payload={})
    case("readable source replaced by a hidden one", False, event_type="source.superseded", target_type="source", target_id=pid, payload={"superseded_by": hid})
    case("readable source with a payload that names a readable source", True, event_type="source.superseded", target_type="source", target_id=pid, payload={"superseded_by": pid})
    case("readable memory with a note about a hidden source", False, event_type="memory.candidate_created", target_type="memory", target_id=mid, payload={"source_id": hid})
    case("labels raised on an unlabelled target", False, event_type="entity.labels_raised", target_type="entity", target_id=str(uuid4()), payload={})
    case("unrelated agent event", True, event_type="agent.identity_upserted", target_type="agent_identity", target_id="a", payload={"agent_id": "a"})
    case("event with no target", True, event_type="scheduler.due_scan", payload={"due_count": 0})
    case("edge between readable rows", True, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["public"]["id"]), payload={"operation": "create"})
    case("edge to a hidden memory", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["hidden_end"]["id"]), payload={"operation": "create"})
    case("edge to a row that is not stored", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["unknown_end"]["id"]), payload={"operation": "create"})
    case("edge from an entity to a readable memory", True, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["entity_end"]["id"]), payload={"operation": "create"})
    case("edge from an entity to a hidden memory", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["entity_to_hidden"]["id"]), payload={"operation": "create"})
    case("edge that is not stored", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(uuid4()), payload={})
    case("edge with an artifact end", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["artifact_end"]["id"]), payload={"operation": "create"})
    case("edge with an open loop end", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["open_loop_end"]["id"]), payload={"operation": "create"})
    case("edge to a belief that is not stored", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["belief_not_stored"]["id"]), payload={"operation": "create"})
    if beliefs:
        case("edge to a belief of a readable memory", True, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["belief_end"]["id"]), payload={"operation": "create"})
        case("edge to a belief of a hidden memory", False, event_type="graph_edge.created", target_type="graph_edge", target_id=str(edges["belief_of_hidden_memory"]["id"]), payload={"operation": "create"})
    # A memory replaced by another one in a correction: the event names the replacement as well as the memory.
    case("memory corrected by a readable memory", True, event_type="memory.reviewed", target_type="memory", target_id=mid, payload={"resolved_action": "supersede", "replacement_memory_id": mid})
    case("memory corrected by a hidden memory", False, event_type="memory.reviewed", target_type="memory", target_id=mid, payload={"resolved_action": "supersede", "replacement_memory_id": sid})
    case("memory corrected by a memory that is not stored", False, event_type="memory.reviewed", target_type="memory", target_id=mid, payload={"replacement_memory_id": str(uuid4())})
    # An id a client sent and an event repeats is judged as the row it names, in capitals or with spaces round it.
    for spelling, text in (("in capitals", lambda value: value.upper()), ("with spaces", lambda value: f"  {value}  "), ("in capitals with spaces", lambda value: f" {value.upper()}\t")):
        case(f"readable source named {spelling}", True, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": text(pid)})
        case(f"hidden source named {spelling}", False, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": text(hid)})
        case(f"readable memories named {spelling}", True, event_type="memory.quarantine_sweep", target_type="agent", target_id="a", payload={"expired_memory_ids": [text(mid), mid]})
        case(f"a hidden memory among the readable ones named {spelling}", False, event_type="memory.quarantine_sweep", target_type="agent", target_id="a", payload={"expired_memory_ids": [mid, text(sid)]})
    case("source named by text that is not a UUID", False, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": "alpha-team"})
    case("source named by a UUID of no stored row", False, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": str(uuid4()).upper()})
    # The id in the target column is the one the store wrote and is read as written, as the native count of source events reads it.
    case("readable source targeted in capitals", False, event_type="source.reviewed", target_type="source", target_id=pid.upper(), payload={})
    case("readable memory targeted in capitals", False, event_type="memory.updated", target_type="memory", target_id=mid.upper(), payload={})

    ceiling = ("public", "internal", "private", "unknown")
    everything = store.list_events()
    for options in ({"sensitivity_allowed": ceiling}, {"sensitivity_allowed": ceiling, "domains": ("project",)}):
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, **options)
            kept = _row_ids(guard.admit_events(everything))
            assert set(shown.values()) <= kept, {name: row for name, row in shown.items() if row not in kept}
            assert not set(withheld.values()) & kept, {name: row for name, row in withheld.items() if row in kept}
            # The count query reads the cut-down payload of the same events and reaches the same number.
            assert guard.readable_event_count() == len(kept)
            # The list query with its prefilter keeps the same events.
            assert _row_ids(guard.admit_events(store.list_events(reject_sensitivity_allowed=ceiling))) == kept
        if oracle is not None:
            with label_read_scope(oracle):
                assert LabelGuard(oracle, active=True, **options).readable_event_count() == len(kept)
    # A caller without limits is shown every event.
    with label_read_scope(store):
        unlimited = LabelGuard(store, active=False)
        assert _row_ids(unlimited.admit_events(everything)) == _row_ids(everything)
    return shown, withheld


def test_events_that_name_a_row_the_caller_cannot_read_are_not_shown_and_not_counted(world):
    shown, withheld = assert_events_are_judged_by_the_rows_they_name(world)
    assert len(shown) == 17 and len(withheld) == 31


def test_on_sqlite_an_event_that_names_an_artifact_a_belief_or_a_project_is_not_shown_to_a_caller_with_limits(world):
    """The SQLite store reads the labels of sources, memories and open loops only, so it cannot show that these are readable.

    The changelog says so. A caller without limits is shown the events as before.
    """
    store = world
    events = [
        _append(store, event_type="queue.task_completed", target_type="task", target_id=str(uuid4()), payload={"artifact_id": str(uuid4())}),
        _append(store, event_type="contradiction.candidate_edge_logged", target_type="entity", target_id=str(uuid4()), payload={"belief_id": str(uuid4())}),
        _append(store, event_type="source.assigned_project", target_type="entity", target_id=str(uuid4()), payload={"project_id": str(uuid4())}),
    ]
    with label_read_scope(store):
        limited = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        assert not set(events) & _row_ids(limited.admit_events(store.list_events()))
        assert limited.readable_event_count() == len(limited.admit_events(store.list_events()))
        unlimited = LabelGuard(store, active=False)
        assert set(events) <= _row_ids(unlimited.admit_events(store.list_events()))


def test_the_row_stored_under_the_spelling_given_is_the_one_judged_when_two_rows_differ_only_in_case(world):
    """SQLite keeps ids as written, so two sources can differ only in case. An id an event repeats is judged by the row stored
    under that spelling, and by the canonical spelling only when there is none."""
    store = world
    canonical = str(uuid4())
    store.create_source({"id": canonical.upper(), "source_type": "note", "title": "Upper", "content_hash": "upper", "sensitivity": "confidential", "domain": "project"})
    store.create_source({"id": canonical, "source_type": "note", "title": "Lower", "content_hash": "lower", "sensitivity": "public", "domain": "project"})
    mixed = canonical[:4].upper() + canonical[4:]
    named = {
        "upper": canonical.upper(),
        "upper with spaces": f"  {canonical.upper()}  ",
        "lower": canonical,
        "lower with spaces": f"  {canonical}  ",
        "mixed": mixed,
    }
    events = {name: _append(store, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": value}) for name, value in named.items()}
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",))
        kept = _row_ids(guard.admit_events(store.list_events()))
        assert {name for name, event_id in events.items() if event_id in kept} == {"lower", "lower with spaces", "mixed"}
        assert guard.readable_event_count() == len(guard.admit_events(store.list_events()))


def test_a_caller_bound_to_a_project_sees_a_chunk_only_for_a_source_in_that_project(world):
    store = world
    inside = store.create_source({"source_type": "note", "title": "In", "content_hash": str(uuid4()), "sensitivity": "public", "domain": "project", "metadata_json": {"project_scope": ["P1"]}})
    outside = store.create_source({"source_type": "note", "title": "Out", "content_hash": str(uuid4()), "sensitivity": "public", "domain": "project", "metadata_json": {"project_scope": ["P2"]}})
    chunk_in = store.create_source_chunk({"source_id": str(inside["id"]), "chunk_index": 0, "text": "in"})
    chunk_out = store.create_source_chunk({"source_id": str(outside["id"]), "chunk_index": 0, "text": "out"})
    events = store.list_events(target_type="source_chunk")
    assert len(events) == 2
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=("public",), projects=("P1",), all_of=("P1",))
        kept = guard.admit_events(events)
        assert [row["target_id"] for row in kept] == [str(chunk_in["id"])]
        assert guard.readable_event_count() == len(guard.admit_events(store.list_events()))
    assert str(chunk_out["id"]) not in {row["target_id"] for row in kept}


# --- the sweep of the event writers --------------------------------------------------------------------------------------

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
# Target types that name a row without a label of their own, or no row. Each is judged by nothing, and the reason is the
# one a reader of the feed needs: what the id would tell a caller who may not read the rows around it.
TARGETS_WITHOUT_A_LABEL = {
    "agent": "a sweep of the memory quarantine; the memory ids it lists are payload references",
    "agent_api_key": "an agent key, never a stored row of the vault",
    "agent_identity": "an agent identity, never a stored row of the vault",
    "brain_charter": "the single charter of the vault",
    "connector": "a connector setting; the rows it ingests are payload references",
    "context_pack": "the compilation of a context pack; it records counts and the query",
    "context_tree": "a view that records counts",
    "demo_dataset": "the demo loader; the rows it made are payload references",
    "entity": "an entity has no label; the sources it was seen in are payload references",
    "person": "a person has no label",
    "scheduler_run": "a run of a workflow; the artifact it made is a payload reference",
    "scheduler_workflow": "a workflow setting",
    "task": "a queued task; the artifact it made is a payload reference",
    "task_queue": "the queue itself",
    "http_route": "a route that a policy refused",
}
# Id fields of the payload that are not the id of a labelled row, with what each is.
PAYLOAD_IDS_WITHOUT_A_LABEL = {
    "agent_id": "an agent",
    "chat_id": "a chat of a connector",
    "confirmation_id": "an inline confirmation token",
    "connector_id": "a connector",
    "dataset_id": "the demo dataset",
    "external_id": "the id an outside system gave an item",
    "proposal_id": "a proposal token",
    "provenance_link_id": "a link row: it carries no text and its ends are the target and the memory",
    "quality_rating_id": "a rating row; the artifact it rates is the target",
    "relationship_event_id": "an event row",
    "reviewer_id": "the name given with a rating",
    "revision_id": "a revision row of the target memory",
    "run_id": "a run",
    "scheduler_run_id": "a scheduler run",
    "secret_ref": "a name for a secret, never the secret",
    "source_chunk_id": "a chunk of the source named next to it in the same payload",
    "sweep_id": "a sweep token",
    "last_run_id": "a scheduler run",
    "trace_id": "a trace token",
    "request_id": "a request token",
}
# Id fields of the payload that name a row the target is made of. A target of this kind is a derived row, whose label was raised
# to the labels of the rows it was made from when it was made, so the field adds no row the target did not already stand for
# then. Once one of those rows is archived or redacted its id stays listed, as in the other lists of ids that the notes describe.
PAYLOAD_IDS_OF_THE_TARGETS_INPUTS = {
    "skipped_members": "members of a consolidation that were not superseded, with the reason; all are inputs of the accepted memory",
    "supersedes": "the survivor of a merge of memories; one of the members the accepted consolidation was made from",
}
_ID_FIELD = re.compile(r"(^id$|_id$|_ids$|_refs?$|_references$|_members$|^supersedes$)")

# A payload the scan cannot read to the end, with what it holds. Each is a helper that hands on a payload it was given (every
# call of the helper is scanned as a write of its own) or a payload that is spread from an object the scan cannot resolve.
UNFOLLOWED_PAYLOADS = {
    ("vnext_agent_keys.py", "claimed"): "the claim of a rejected key: the name of an agent or of a permission profile",
    ("vnext_capture.py", "payload"): "a helper that adds the agent identity and the policy decision to the payload it is given",
    ("vnext_connectors.py", "payload"): "a helper that adds the connector name to the payload it is given",
    ("vnext_connectors.py", "sync_result.to_record()"): "the counts, cursors and errors of a sync, with source_ids (judged)"
    " and failed_external_ids (the ids an outside system gave its items)",
    ("vnext_event_log.py", "payload"): "append_event, which every writer calls",
    ("vnext_stores/postgres/events_revisions.py", "payload"): "the store's append of an event, which every writer reaches",
    ("vnext_stores/sqlite/events_revisions.py", "payload"): "the store's append of an event, which every writer reaches",
}


@lru_cache(maxsize=None)
def _package_trees():
    return {path.relative_to(SOURCE_ROOT).as_posix(): ast.parse(path.read_text(encoding="utf-8")) for path in sorted(SOURCE_ROOT.rglob("*.py"))}


class PayloadReader:
    """The keys an event payload holds, followed through the variables and the helper functions that build it.

    A payload is read as a dict literal (its nested dicts and ``**`` parts too); as a name, through the dict literals assigned
    to it in the function that writes the event and the keys added to it (``name["key"] = ...``, ``name |= ...``,
    ``name.update(...)``, ``name.setdefault(...)``); as a ``cast``, ``dict`` or JSON-safe wrapper of one of these; and as a
    call of a function that is the only one of its name in the package, through what it returns. What it cannot read is kept
    in ``unfollowed``: a name that is a parameter of the function, a key chosen at run time, a call it cannot resolve.
    """

    WRAPPERS = frozenset({"cast", "dict", "json_safe", "_json_safe"})

    def __init__(self, functions):
        self.functions = functions
        self.unfollowed = []

    @staticmethod
    def _text(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _key_of(self, subscript):
        text = self._text(subscript.slice)
        if text is None:
            self.unfollowed.append(ast.unparse(subscript))
        return {text} if text is not None else set()

    def keys(self, node, chain, seen=frozenset()):
        found = set()
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                text = self._text(key) if key is not None else None
                if text is not None:
                    found.add(text)
                elif key is not None:
                    self.unfollowed.append(ast.unparse(key))
                if key is None or isinstance(value, ast.Dict):
                    found |= self.keys(value, chain, seen)
        elif isinstance(node, ast.Name):
            found |= self._name(node.id, chain, seen)
        elif isinstance(node, ast.IfExp):
            found |= self.keys(node.body, chain, seen) | self.keys(node.orelse, chain, seen)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                found |= self.keys(value, chain, seen)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            found |= self.keys(node.left, chain, seen) | self.keys(node.right, chain, seen)
        elif isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            if callee in self.WRAPPERS:
                if node.args:
                    found |= self.keys(node.args[-1], chain, seen)
                found |= {keyword.arg for keyword in node.keywords if keyword.arg}
            elif len(self.functions.get(callee, ())) == 1:
                function = self.functions[callee][0]
                if function not in seen:
                    for part in ast.walk(function):
                        if isinstance(part, ast.Return) and part.value is not None:
                            found |= self.keys(part.value, [function], seen | {function})
            else:
                self.unfollowed.append(ast.unparse(node))
        elif not isinstance(node, ast.Constant):
            self.unfollowed.append(ast.unparse(node))
        return found

    def _name(self, name, chain, seen):
        found = set()
        for function in reversed(chain):
            if (name, function) in seen:
                return found
            seen = seen | {(name, function)}
            parameters = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs, function.args.vararg, function.args.kwarg) if arg}
            assigned = False
            for part in ast.walk(function):
                if isinstance(part, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = part.targets if isinstance(part, ast.Assign) else [part.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id == name and part.value is not None:
                            assigned = True
                            found |= self.keys(part.value, chain, seen)
                        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == name:
                            found |= self._key_of(target)
                elif isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and isinstance(part.func.value, ast.Name) and part.func.value.id == name:
                    if part.func.attr == "update":
                        for argument in part.args:
                            found |= self.keys(argument, chain, seen)
                        found |= {keyword.arg for keyword in part.keywords if keyword.arg}
                    elif part.func.attr == "setdefault" and part.args:
                        text = self._text(part.args[0])
                        if text is None:
                            self.unfollowed.append(ast.unparse(part))
                        else:
                            found.add(text)
            if assigned:
                return found
            if name in parameters:
                break
        self.unfollowed.append(name)
        return found


def _function_index(trees):
    """Every function of the trees by name."""
    functions = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append(node)
    return functions


def _event_writes(trees):
    """Every call that writes an event (a call with an ``event_type`` and a payload or a target), with the functions round it."""
    for path, tree in trees.items():
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                keywords = {item.arg: item.value for item in node.keywords if item.arg}
                if "event_type" in keywords and ("payload" in keywords or "target_type" in keywords):
                    chain, up = [], node
                    while up in parents:
                        up = parents[up]
                        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            chain.append(up)
                    yield path, node.lineno, keywords, chain[::-1]


def _written_payloads(trees):
    """For every event write with a payload: (path, line, the keys the payload holds, the parts of it that could not be read)."""
    functions = _function_index(trees)
    for path, line, keywords, chain in _event_writes(trees):
        if "payload" in keywords:
            reader = PayloadReader(functions)
            yield path, line, reader.keys(keywords["payload"], chain), reader.unfollowed


def test_every_target_type_an_event_writer_names_is_classified():
    known = set(EVENT_TARGET_KINDS) | {EVENT_EDGE_TARGET} | set(EVENT_CHILD_TARGETS) | set(TARGETS_WITHOUT_A_LABEL)
    unclassified = {}
    seen = set()
    for path, line, keywords, _chain in _event_writes(_package_trees()):
        target = keywords.get("target_type")
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            seen.add(target.value)
            if target.value not in known:
                unclassified[f"{path}:{line}"] = target.value
    assert not unclassified, (
        "an event writer names a target type that no guard judges and that is not listed as unlabelled: "
        f"{unclassified}"
    )
    # The classification is not stale either: every judged type is written somewhere.
    assert (set(EVENT_TARGET_KINDS) | {EVENT_EDGE_TARGET} | set(EVENT_CHILD_TARGETS)) <= seen | {"belief"}, seen


def test_every_id_field_an_event_writer_puts_in_a_payload_is_classified():
    judged = set(EVENT_PAYLOAD_REFERENCES) | {key for fields in EVENT_TYPE_REFERENCES.values() for key in fields}
    unclassified = {}
    for path, line, keys, _unfollowed in _written_payloads(_package_trees()):
        for key in keys:
            if _ID_FIELD.search(key) and key not in judged and key not in PAYLOAD_IDS_WITHOUT_A_LABEL and key not in PAYLOAD_IDS_OF_THE_TARGETS_INPUTS:
                unclassified.setdefault(key, f"{path}:{line}")
    assert not unclassified, (
        "an event payload holds an id field that is neither judged by the guard nor listed as the id of an unlabelled row: "
        f"{unclassified}"
    )
    assert not judged & set(PAYLOAD_IDS_WITHOUT_A_LABEL)
    assert not judged & set(PAYLOAD_IDS_OF_THE_TARGETS_INPUTS)


def test_every_judged_id_field_is_written_by_some_event_writer():
    """The list of judged fields is not stale: a field that no writer puts in a payload any more is taken off."""
    written = set().union(*(keys for _path, _line, keys, _unfollowed in _written_payloads(_package_trees())))
    judged = set(EVENT_PAYLOAD_REFERENCES) | {key for fields in EVENT_TYPE_REFERENCES.values() for key in fields}
    assert judged <= written, sorted(judged - written)


def test_every_payload_the_sweep_cannot_follow_is_listed():
    """A helper that hands on a payload, or a spread of an object the scan cannot resolve, is named here with what it holds."""
    unfollowed = {(path, text) for path, _line, _keys, parts in _written_payloads(_package_trees()) for text in parts}
    assert unfollowed == set(UNFOLLOWED_PAYLOADS), (sorted(unfollowed - set(UNFOLLOWED_PAYLOADS)), sorted(set(UNFOLLOWED_PAYLOADS) - unfollowed))


def test_the_sweep_reads_a_field_that_a_payload_variable_gets_after_it_is_built():
    """The correction of a memory puts the id of the replacement in a payload it built earlier, by assignment to a key."""
    by_path = {}
    for path, _line, keys, _unfollowed in _written_payloads(_package_trees()):
        by_path.setdefault(path, set()).update(keys)
    assert "replacement_memory_id" in by_path["mcp/review.py"]
    assert "superseded_by" in by_path["vnext_memory_commit.py"]
    assert "member_id" in by_path["vnext_memory_commit.py"]


SWEEP_SNIPPET = """
def literal(store):
    append_event(store, event_type="a.b", payload={"reason": "r", "nested": {"inner_id": 1}})

def assigned_keys(store, other):
    payload = {"reason": "r"}
    payload["replacement_id"] = other
    payload |= {"added_ids": []}
    payload.update({"updated_id": 1}, kwarg_id=2)
    payload.setdefault("defaulted_id", 3)
    unrelated = {"not_in_the_payload_id": 4}
    append_event(store, event_type="a.b", payload=payload)

def conditional(store, flag):
    payload: dict = {"base_id": 1} if flag else {"other_id": 2}
    append_event(store, event_type="a.b", payload=cast(JsonObject, _json_safe(payload)))

def spread(store, extra):
    base = {"base_id": 1}
    append_event(store, event_type="a.b", payload={**base, **extra, "own_id": 2})

def built_by_a_helper(store):
    append_event(store, event_type="a.b", payload=make_payload(cause="c"))

def make_payload(*, cause):
    result = {"cause": cause}
    result["helper_id"] = 1
    return result

def handed_on(store, payload):
    append_event(store, event_type="a.b", payload=payload)

def handed_on_and_added_to(store, payload):
    payload["late_id"] = 1
    append_event(store, event_type="a.b", payload=payload)

def chosen_at_run_time(store, name):
    payload = {}
    payload[name] = 1
    append_event(store, event_type="a.b", payload=payload)

def not_followable(store, record):
    append_event(store, event_type="a.b", payload=record.to_record())

def no_payload(store):
    append_event(store, event_type="a.b", target_type="memory", target_id="x")
"""


def test_the_sweep_follows_payloads_through_variables_and_helpers():
    trees = {"snippet.py": ast.parse(SWEEP_SNIPPET)}
    read, functions = {}, _function_index(trees)
    for _path, _line, keywords, chain in _event_writes(trees):
        if "payload" in keywords:
            reader = PayloadReader(functions)
            read[chain[-1].name] = (reader.keys(keywords["payload"], chain), reader.unfollowed)
    assert read["literal"] == ({"reason", "nested", "inner_id"}, [])
    assert read["assigned_keys"] == (
        {"reason", "replacement_id", "added_ids", "updated_id", "kwarg_id", "defaulted_id"}, [])
    assert read["conditional"] == ({"base_id", "other_id"}, [])
    assert read["spread"] == ({"base_id", "own_id"}, ["extra"])
    assert read["built_by_a_helper"] == ({"cause", "helper_id"}, [])
    assert read["handed_on"] == (set(), ["payload"])
    assert read["handed_on_and_added_to"] == ({"late_id"}, ["payload"])
    assert read["chosen_at_run_time"] == (set(), ["payload[name]"])
    assert read["not_followable"] == (set(), ["record.to_record()"])
    assert "no_payload" not in read
