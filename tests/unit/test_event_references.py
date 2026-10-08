"""An event is shown to a caller with limits only when every row it names may be read by that caller.

An event names the row it is about (its target) and, in its payload, the rows it was made with or acted on. The event feed,
the agent feed, the context tree, the dogfooding view and the workspace event count all go through ``admit_events`` and
``readable_event_count``, and these tests pin that both judge the same references:

* a chunk of a source has no label, so its event takes the label of the source its payload names, and an event that names
  no source is not shown;
* a payload field that holds the id of a labelled row is judged whatever the target is (an entity, a task, a connector, a
  scheduler run, a memory);
* a graph edge has no label, so it is shown when each of its labelled ends is;
* the count query and the list query read the same fields, on SQLite and (in the integration test) on PostgreSQL;
* the writers of events are swept: a new target type or a new id field fails the sweep until it is classified here.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_guard import LabelGuard, event_references, label_read_scope
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
        (_event("demo.dataset_loaded", "demo_dataset", "d", source_ids=[SRC], artifact_ids=[MEM], project_ids=[SRC]),
         [("source", SRC), ("artifact", MEM), ("project", SRC)]),
    )
    for event, expected in cases:
        assert sorted(event_references(event)) == sorted(expected), event["event_type"]


LISTED_PAYLOAD_FIELDS = {
    "source_id": "source", "source_ids": "source", "memory_id": "memory", "candidate_memory_id": "memory",
    "candidate_memory_ids": "memory", "rollup_candidate_ids": "memory", "expired_memory_ids": "memory",
    "superseded_member_ids": "memory", "member_id": "memory", "artifact_id": "artifact", "artifact_ids": "artifact",
    "belief_id": "belief", "project_id": "project", "project_ids": "project",
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


def make_event_vault(store):
    """A readable and a hidden source, memory and chunk, and edges between them. Used by the PostgreSQL test too."""
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

    def edge(from_type, from_id, to_id):
        return create_edge({"from_type": from_type, "from_id": from_id, "to_type": "memory", "to_id": to_id, "edge_type": "similar_to", "confidence": 0.9, "created_by": "synthetic"})

    edges = {
        "public": edge("source", str(public["id"]), str(memory["id"])),
        "hidden_end": edge("source", str(public["id"]), str(secret["id"])),
        "unknown_end": edge("source", str(public["id"]), str(uuid4())),
        "entity_end": edge("entity", str(uuid4()), str(memory["id"])),
        "entity_to_hidden": edge("entity", str(uuid4()), str(secret["id"])),
    }
    return public, hidden, memory, secret, chunks, edges


def _append(store, **record):
    return str(store.append_event(build_event_log_record(actor_type="system", **record))["id"])


def assert_events_are_judged_by_the_rows_they_name(store, *, oracle=None):
    """Append events that name rows, then check which a caller with limits is shown and how many it is told of.

    ``oracle`` is a store that offers only the per-row reads, to compare the native count of PostgreSQL with.
    """
    public, hidden, memory, secret, chunks, edges = make_event_vault(store)
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
    assert len(shown) >= 10 and len(withheld) >= 12


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
_ID_FIELD = re.compile(r"(^id$|_id$|_ids$|_refs?$|_references$)")


def _event_writes():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                keywords = {item.arg: item.value for item in node.keywords if item.arg}
                if "event_type" in keywords and ("payload" in keywords or "target_type" in keywords):
                    yield path.relative_to(SOURCE_ROOT), node.lineno, keywords


def _dict_keys(node):
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value
            if isinstance(value, ast.Dict):
                yield from _dict_keys(value)


def test_every_target_type_an_event_writer_names_is_classified():
    known = set(EVENT_TARGET_KINDS) | {EVENT_EDGE_TARGET} | set(EVENT_CHILD_TARGETS) | set(TARGETS_WITHOUT_A_LABEL)
    unclassified = {}
    seen = set()
    for path, line, keywords in _event_writes():
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
    for path, line, keywords in _event_writes():
        payload = keywords.get("payload")
        if payload is None:
            continue
        for key in _dict_keys(payload):
            if _ID_FIELD.search(key) and key not in judged and key not in PAYLOAD_IDS_WITHOUT_A_LABEL:
                unclassified.setdefault(key, f"{path}:{line}")
    assert not unclassified, (
        "an event payload holds an id field that is neither judged by the guard nor listed as the id of an unlabelled row: "
        f"{unclassified}"
    )
    assert not judged & set(PAYLOAD_IDS_WITHOUT_A_LABEL)
