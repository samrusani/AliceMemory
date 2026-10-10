"""The events of a source trace are shown to a caller with limits only when every row they name may be read by that caller.

``GET /v0/vnext/traces/sources/{id}`` and the trace that a source review embeds list the events that target the source or
a row of the trace, and the payload of an event can name another row: a correction names the memory that replaced the
target. The workspace feed judges those ids with ``admit_events``. The trace kept only the events whose target it listed,
so the payload of a correction still named a replacement above the ceiling of the key. The loader now runs its events
through the same judgment, for a caller the operator gate lets in (the owner, an unbound admin key and an unbound trusted
key) and for a caller it does not (the loader answers them as a missing source).

The loader reads the artifacts of a source through a Postgres method, so a small store adds an empty one to the SQLite
store. The trace route is tested through the application on PostgreSQL in
``tests/integration/test_source_trace_events_postgres.py``.

The read of a caller with limits stops after ``EVENT_FEED_SCAN_LIMIT`` events, as the event feeds do, so its cost does not
grow with the number of events it may not read, and the trace says it is incomplete when events lie beyond that reach.
Only the events the caller can be shown count against the reach: the store reads the events aimed at the source or a row
of the trace, so the chunk events of a big source (they name the source in their payload and are aimed at the chunk) and
the other events that only name it cost nothing.

Mutations (``scripts/derived_label_mutations.json``): hand the events to the caller without ``admit_events`` (the
replacement above the ceiling is named in the trace); drop the target filter of a caller with limits (an event about a row
the trace does not list is shown); build the guard for every caller (the owner and an unbound admin key lose events);
let a refused profile through (it is shown a trace); drop the scan limit (the read goes on past the reach); stop the read
one event short of the reach (a trace that fits is called incomplete); report an exhausted read as complete; read the
events of every target (the chunk events of a big source use up the reach); leave out the narrowing in either store.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.routers._vnext_shared import _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT, _vnext_load_source_trace
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_guard import EVENT_FEED_SCAN_LIMIT, label_read_scope
from alicebot_api.vnext_label_writes import without_insert_floor

USER = "11111111-1111-4111-8111-111111111111"
OWNER = None
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")
TRUSTED = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent", auth="agent_api_key")


class TraceStore(SQLiteVNextStore):
    """The SQLite store with the two readers of the trace that only PostgreSQL has. It records how deep the events are read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_fetch_limits: list[int] = []

    def list_events_for_source_trace(self, **kwargs):
        self.event_fetch_limits.append(kwargs["limit"])
        return super().list_events_for_source_trace(**kwargs)

    def list_artifacts_referencing_source(self, *, source_id, limit=500):
        return []

    def list_source_chunks(self, source_id, *, limit=None):
        rows = super().list_source_chunks(source_id)
        return rows[:limit] if limit is not None else rows


class LaxTraceStore(TraceStore):
    """A store that does not narrow the events to the ids it is handed, so only the loader's own filter keeps them out."""

    def list_events_for_source_trace(self, **kwargs):
        kwargs["target_ids"] = None
        return super().list_events_for_source_trace(**kwargs)


def _open_store(tmp_path, store_class):
    path = tmp_path / "trace.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        yield store_class(conn, USER)


@pytest.fixture
def store(tmp_path):
    yield from _open_store(tmp_path, TraceStore)


@pytest.fixture
def lax_store(tmp_path):
    yield from _open_store(tmp_path, LaxTraceStore)


def _append(store, **record):
    return str(store.append_event(build_event_log_record(actor_type="system", **record))["id"])


class Vault:
    """A readable source with a readable memory that names it, a memory above the trusted ceiling, and a hidden source."""

    def __init__(self, store):
        self.store = store
        self.source = store.create_source({"source_type": "note", "title": "Visible", "content_hash": str(uuid4()), "sensitivity": "public", "domain": "project"})
        self.source_id = str(self.source["id"])
        self.hidden_source = store.create_source({"source_type": "note", "title": "Hidden", "content_hash": str(uuid4()), "sensitivity": "confidential", "domain": "project"})
        self.hidden_source_id = str(self.hidden_source["id"])
        with without_insert_floor():
            self.visible = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Visible fact", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": self.source_id}})
            self.hidden = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden fact", "status": "active", "domain": "project", "sensitivity": "confidential"})
            self.hidden_citing_source = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden note on the source", "status": "active", "domain": "project", "sensitivity": "confidential", "metadata_json": {"source_id": self.source_id}})
        self.visible_id, self.hidden_id = str(self.visible["id"]), str(self.hidden["id"])
        self.hidden_citing_id = str(self.hidden_citing_source["id"])

    def trace(self, identity):
        with label_read_scope(self.store):
            return _vnext_load_source_trace(store=self.store, source=self.store.get_source(self.source_id), identity=identity)


def _ids(trace):
    return {str(event["id"]) for event in trace["events"]}


def test_a_correction_that_names_a_replacement_above_the_ceiling_is_not_in_the_trace_of_a_key_with_limits(store):
    vault = Vault(store)
    to_hidden = _append(store, event_type="memory.reviewed", target_type="memory", target_id=vault.visible_id, payload={"resolved_action": "supersede", "replacement_memory_id": vault.hidden_id})
    to_visible = _append(store, event_type="memory.reviewed", target_type="memory", target_id=vault.visible_id, payload={"resolved_action": "supersede", "replacement_memory_id": vault.visible_id})
    plain = _append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"operation": "update"})
    trusted = vault.trace(TRUSTED)
    assert {to_visible, plain} <= _ids(trusted) and to_hidden not in _ids(trusted)
    assert vault.hidden_id not in json.dumps(trusted, default=str)
    assert trusted["summary"]["event_count"] == len(trusted["events"])
    # The owner and an unbound admin key are not limited: the trace holds every event of the source, as it did.
    for identity in (OWNER, ADMIN):
        assert {to_hidden, to_visible, plain} <= _ids(vault.trace(identity))


def test_every_payload_field_that_names_a_row_is_judged_in_the_trace(store):
    vault = Vault(store)
    cases = {
        "a candidate memory": ("memory.candidate_created", "memory", vault.visible_id, {"candidate_memory_id": vault.hidden_id}),
        "a list of memories": ("memory.quarantine_sweep", "memory", vault.visible_id, {"expired_memory_ids": [vault.visible_id, vault.hidden_id]}),
        "a source": ("source.reviewed", "source", vault.source_id, {"source_id": vault.hidden_source_id}),
        "a source that replaced the source": ("source.superseded", "source", vault.source_id, {"superseded_by": vault.hidden_source_id}),
        "a memory that replaced the memory": ("agent.memory_superseded", "memory", vault.visible_id, {"superseded_by": vault.hidden_id}),
        "an id that is not a row": ("memory.candidate_created", "memory", vault.visible_id, {"candidate_memory_id": "not-an-id"}),
        "a hidden id in capitals": ("memory.candidate_created", "memory", vault.visible_id, {"candidate_memory_id": vault.hidden_id.upper()}),
    }
    readable = {
        "a readable memory": ("memory.candidate_created", "memory", vault.visible_id, {"candidate_memory_id": vault.visible_id}),
        "a readable source": ("source.reviewed", "source", vault.source_id, {"source_id": vault.source_id}),
        "no row": ("source.reviewed", "source", vault.source_id, {"review_note": "checked"}),
    }
    withheld = {name: _append(store, event_type=kind, target_type=target, target_id=target_id, payload=payload) for name, (kind, target, target_id, payload) in cases.items()}
    shown = {name: _append(store, event_type=kind, target_type=target, target_id=target_id, payload=payload) for name, (kind, target, target_id, payload) in readable.items()}
    trusted = _ids(vault.trace(TRUSTED))
    assert {name for name, event_id in withheld.items() if event_id in trusted} == set()
    assert {name for name, event_id in shown.items() if event_id not in trusted} == set()
    everything = _ids(vault.trace(ADMIN))
    assert set(withheld.values()) | set(shown.values()) <= everything
    assert set(withheld.values()) | set(shown.values()) <= _ids(vault.trace(OWNER))


def test_a_caller_with_limits_is_still_shown_only_the_events_that_target_a_row_of_the_trace(store):
    """The target filter stays: an event about a row the trace does not list is not shown, though it names the source."""
    _check_the_target_filter(store)


def test_the_loader_filters_the_targets_itself_when_the_store_does_not_narrow(lax_store):
    """The store narrows the read, and the loader does not depend on it: it keeps its own filter on the events it is handed."""
    _check_the_target_filter(lax_store)


def _check_the_target_filter(store):
    vault = Vault(store)
    about_the_hidden_note = _append(store, event_type="memory.updated", target_type="memory", target_id=vault.hidden_citing_id, payload={"source_id": vault.source_id})
    about_a_stranger = _append(store, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": vault.source_id})
    about_the_source = _append(store, event_type="source.reviewed", target_type="source", target_id=vault.source_id, payload={})
    trusted = vault.trace(TRUSTED)
    assert about_the_source in _ids(trusted)
    assert not {about_the_hidden_note, about_a_stranger} & _ids(trusted)
    assert [str(memory["id"]) for memory in trusted["candidate_memories"]] == [vault.visible_id]
    owner = vault.trace(OWNER)
    assert {about_the_hidden_note, about_a_stranger, about_the_source} <= _ids(owner)


def test_events_above_the_collection_limit_that_name_a_hidden_row_do_not_crowd_out_the_readable_ones(store):
    """The loader reads deeper until it has a full page of admitted events, so many hidden ones in front cost no readable one."""
    vault = Vault(store)
    readable = [_append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"n": index}) for index in range(3)]
    for index in range(_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT + 20):
        _append(store, event_type="memory.reviewed", target_type="memory", target_id=vault.visible_id, payload={"replacement_memory_id": vault.hidden_id, "n": index})
    trusted = vault.trace(TRUSTED)
    assert set(readable) <= _ids(trusted)
    assert not [event for event in trusted["events"] if event["event_type"] == "memory.reviewed"]
    assert trusted["sampling"]["collection_complete"]["events"] is True
    assert vault.hidden_id not in json.dumps(trusted, default=str)
    owner = vault.trace(OWNER)
    assert len(owner["events"]) == _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
    assert owner["sampling"]["collection_complete"]["events"] is False


def test_a_caller_with_limits_reads_no_further_than_the_event_feeds_do_and_the_trace_says_when_it_stopped(store):
    """Events it may not read, newest first, cost it a bounded read. What lies beyond the reach is not shown, and the
    trace is incomplete, so a client does not take the list for all the readable events."""
    vault = Vault(store)
    readable = [_append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"n": index}) for index in range(3)]
    for index in range(EVENT_FEED_SCAN_LIMIT + 50):
        _append(store, event_type="memory.reviewed", target_type="memory", target_id=vault.visible_id, payload={"replacement_memory_id": vault.hidden_id, "n": index})
    store.event_fetch_limits.clear()
    trusted = vault.trace(TRUSTED)
    assert max(store.event_fetch_limits) == EVENT_FEED_SCAN_LIMIT + 1
    assert not set(readable) & _ids(trusted)
    assert not [event for event in trusted["events"] if event["event_type"] == "memory.reviewed"]
    assert trusted["sampling"]["collection_complete"]["events"] is False
    assert "events" in trusted["sampling"]["truncated_collections"] and trusted["sampling"]["trace_complete"] is False
    assert vault.hidden_id not in json.dumps(trusted, default=str)
    assert trusted["summary"]["event_count"] == len(trusted["events"])
    # The owner and an unbound admin key are read as before: one page and one more event, with no judgment to wait for.
    for identity in (OWNER, ADMIN):
        store.event_fetch_limits.clear()
        assert len(vault.trace(identity)["events"]) == _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
        assert store.event_fetch_limits == [_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT + 1]


def test_a_trace_whose_events_all_lie_within_the_reach_is_complete_for_a_caller_with_limits(store):
    vault = Vault(store)
    held = len(store.list_events_for_source_trace(source_id=vault.source_id, memory_ids=[vault.visible_id], limit=10 * EVENT_FEED_SCAN_LIMIT))
    readable = [_append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"n": index}) for index in range(3)]
    for index in range(EVENT_FEED_SCAN_LIMIT - held - len(readable)):
        _append(store, event_type="memory.reviewed", target_type="memory", target_id=vault.visible_id, payload={"replacement_memory_id": vault.hidden_id, "n": index})
    assert len(store.list_events_for_source_trace(source_id=vault.source_id, memory_ids=[vault.visible_id], limit=10 * EVENT_FEED_SCAN_LIMIT)) == EVENT_FEED_SCAN_LIMIT
    store.event_fetch_limits.clear()
    trusted = vault.trace(TRUSTED)
    assert set(readable) <= _ids(trusted)
    assert not [event for event in trusted["events"] if event["event_type"] == "memory.reviewed"]
    assert trusted["sampling"]["collection_complete"]["events"] is True
    assert max(store.event_fetch_limits) == EVENT_FEED_SCAN_LIMIT + 1


def _chunk_events(store, source_id, count):
    for index in range(count):
        _append(store, event_type="source_chunk.created", target_type="source_chunk", target_id=str(uuid4()), payload={"source_id": source_id, "chunk_index": index})


@pytest.mark.parametrize("extra", [0, 1, 99, 600])
def test_the_chunk_events_of_a_big_source_cost_a_caller_with_limits_nothing_against_the_reach(store, extra):
    """A source with more chunks than the reach has as many chunk events, newer than the events of the source itself. They
    name the source and are aimed at a chunk, so the caller is never shown one, and they must not use up the reach."""
    vault = Vault(store)
    created = _append(store, event_type="source.created", target_type="source", target_id=vault.source_id, payload={})
    captured = _append(store, event_type="source.captured", target_type="source", target_id=vault.source_id, payload={"source_id": vault.source_id})
    on_memory = _append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"source_id": vault.source_id})
    _chunk_events(store, vault.source_id, EVENT_FEED_SCAN_LIMIT + extra)
    chunked = _append(store, event_type="source.chunked", target_type="source", target_id=vault.source_id, payload={"source_id": vault.source_id})
    store.event_fetch_limits.clear()
    trusted = vault.trace(TRUSTED)
    assert {created, captured, on_memory, chunked} <= _ids(trusted)
    assert not [event for event in trusted["events"] if event["event_type"] == "source_chunk.created"]
    assert trusted["sampling"]["collection_complete"]["events"] is True
    assert trusted["summary"]["event_count"] == len(trusted["events"])
    # The read is still bounded by the reach, and the chunk events were not read at all.
    assert max(store.event_fetch_limits) <= EVENT_FEED_SCAN_LIMIT + 1
    # The owner and an unbound admin key are read as before: the newest page of every event of the source, chunks included.
    for identity in (OWNER, ADMIN):
        everyone = vault.trace(identity)
        assert len(everyone["events"]) == _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
        assert {event["event_type"] for event in everyone["events"]} == {"source_chunk.created", "source.chunked"}
        assert everyone["sampling"]["collection_complete"]["events"] is False


def test_events_aimed_at_the_source_still_use_up_the_reach_of_a_caller_with_limits(store):
    """What counts against the reach is the events the caller can be shown: a source with more of those than the reach is
    read as far as the reach, and the trace says it stopped."""
    vault = Vault(store)
    older = _append(store, event_type="source.created", target_type="source", target_id=vault.source_id, payload={})
    for index in range(EVENT_FEED_SCAN_LIMIT + 20):
        _append(store, event_type="source.reviewed", target_type="source", target_id=vault.source_id, payload={"source_id": vault.hidden_source_id, "n": index})
    _chunk_events(store, vault.source_id, 30)
    trusted = vault.trace(TRUSTED)
    assert older not in _ids(trusted) and not trusted["events"]
    assert trusted["sampling"]["collection_complete"]["events"] is False
    assert vault.hidden_source_id not in json.dumps(trusted, default=str)


def test_the_store_reads_only_the_events_aimed_at_the_ids_it_is_given_before_the_limit(store):
    vault = Vault(store)
    kept = [_append(store, event_type="source.created", target_type="source", target_id=vault.source_id, payload={})]
    kept.append(_append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={"source_id": vault.source_id}))
    stranger = _append(store, event_type="entity.mention_recorded", target_type="entity", target_id=str(uuid4()), payload={"source_id": vault.source_id})
    _chunk_events(store, vault.source_id, 30)
    wanted = {"source_id": vault.source_id, "memory_ids": [vault.visible_id], "limit": 5}
    # Without ids the newest events come first: five chunk events, and none of the events of the trace.
    unnarrowed = store.list_events_for_source_trace(**wanted)
    assert len(unnarrowed) == 5 and not set(kept) & {str(event["id"]) for event in unnarrowed}
    narrowed = store.list_events_for_source_trace(**{**wanted, "limit": 500}, target_ids=[vault.source_id, vault.visible_id])
    assert set(kept) <= {str(event["id"]) for event in narrowed}
    assert {str(event["target_id"]) for event in narrowed} <= {vault.source_id, vault.visible_id}
    assert stranger not in {str(event["id"]) for event in narrowed}
    # The limit applies to the events that remain: the newest of them, and not the newest five chunk events.
    limited = store.list_events_for_source_trace(**wanted, target_ids=[vault.source_id, vault.visible_id])
    assert [str(event["id"]) for event in limited] == [str(event["id"]) for event in narrowed][:5]
    assert {str(event["target_id"]) for event in limited} <= {vault.source_id, vault.visible_id}
    only_source = store.list_events_for_source_trace(**{**wanted, "limit": 500}, target_ids=[vault.source_id])
    assert kept[0] in {str(event["id"]) for event in only_source} and kept[1] not in {str(event["id"]) for event in only_source}
    assert {str(event["target_id"]) for event in only_source} == {vault.source_id}
    # No id at all is no event, and a blank id or a repeated one changes nothing.
    assert store.list_events_for_source_trace(**wanted, target_ids=[]) == []
    blank = store.list_events_for_source_trace(**{**wanted, "limit": 500}, target_ids=["", vault.source_id, vault.source_id])
    assert [str(event["id"]) for event in blank] == [str(event["id"]) for event in only_source]


@pytest.mark.parametrize("profile", ["read_only_agent", "project_scoped_agent", "memory_proposal_agent"])
def test_a_profile_the_operator_gate_refuses_is_answered_as_a_missing_source(store, profile):
    """These profiles never reach the loader over HTTP. If one is handed to it, it gets no trace and no event."""
    vault = Vault(store)
    _append(store, event_type="memory.updated", target_type="memory", target_id=vault.visible_id, payload={})
    assert vault.trace(AgentIdentity(agent_id="refused", permission_profile=profile, auth="agent_api_key")) is None


def test_a_key_bound_to_a_project_is_answered_as_a_missing_source(store):
    vault = Vault(store)
    bound = AgentIdentity(agent_id="bound", permission_profile="trusted_local_agent", auth="agent_api_key", project_scope=("alpha",), project_scope_locked=True)
    assert vault.trace(bound) is None


def test_a_source_above_the_ceiling_is_still_answered_as_a_missing_source_before_any_event_is_read(store):
    vault = Vault(store)
    _append(store, event_type="source.reviewed", target_type="source", target_id=vault.hidden_source_id, payload={})
    with label_read_scope(store):
        assert _vnext_load_source_trace(store=store, source=store.get_source(vault.hidden_source_id), identity=TRUSTED) is None
        assert _vnext_load_source_trace(store=store, source=store.get_source(vault.hidden_source_id), identity=OWNER)["events"]
