"""The events of a source trace name only rows the caller may read, through the mounted application on PostgreSQL.

``GET /v0/vnext/traces/sources/{id}`` and the trace that ``POST /v0/vnext/sources/{id}/review`` embeds list the events that
target the source or a row of the trace. The payload of a correction names the memory that replaced the target, and that
memory can be made confidential afterwards. The trace judges those ids as the workspace feed does. The owner and an unbound
admin key are not limited and keep every event.

The unit test runs the same cases on SQLite (``tests/unit/test_source_trace_events.py``). Here the rows are read through the
PostgreSQL store, the keys are real, and the events are made by the correction tool.
"""
import json
from uuid import uuid4

import pytest

from alicebot_api.routers._vnext_shared import _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT, _vnext_load_source_trace
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_guard import label_read_scope
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)


def _correct(h, monkeypatch, admin, memory_id):
    """Replace a memory through the correction tool and return the id of the new memory."""
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    call_mcp_tool(
        MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id),
        name="alice_memory_correct",
        arguments={"review_item_id": memory_id, "action": "supersede-existing", "replacement_title": "New preference", "replacement_body": {"text": "Atlas likes new things"}, "reason": "changed"},
    )
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories WHERE supersedes = %s", (memory_id,))
        replacements = [row["id"] for row in cur.fetchall()]
    assert len(replacements) == 1
    return replacements[0]


def _reviews_naming(events, replacement):
    return [event for event in events if event["event_type"] == "memory.reviewed" and event["payload_json"].get("replacement_memory_id") == replacement]


def _mentions(events, row_id):
    return sorted({event["event_type"] for event in events if row_id in json.dumps(event, default=str)})


def test_a_correction_that_names_a_replacement_made_confidential_is_not_in_the_trace_of_a_trusted_key(label_harness, monkeypatch):
    h = label_harness
    source = h.source(sensitivity="public")
    source_id = str(source["id"])
    with h.store() as store:
        memory = store.create_memory({"memory_key": "alpha.pref", "memory_type": "semantic", "title": "Old preference", "canonical_text": "Atlas likes old things", "summary": "old", "value": {"text": "Atlas likes old things"}, "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [], "source_id": source_id}})
    memory_id = str(memory["id"])
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    replacement = _correct(h, monkeypatch, admin, memory_id)
    path = f"/v0/vnext/traces/sources/{source_id}"

    # While the replacement is readable, every key is shown the correction that names it.
    for key in (trusted, admin):
        status, trace, _ = h.request("GET", path, key=key)
        assert status == 200, trace
        assert _reviews_naming(trace["events"], replacement), key
    status, body, _ = h.request("POST", f"/v0/vnext/sources/{source_id}/review", key=trusted, payload={"action": "review", "review_note": "checked"})
    assert status == 200, body
    assert _reviews_naming(body["trace"]["events"], replacement)

    status, body, _ = h.request("POST", f"/v0/vnext/memories/{replacement}/review", key=admin, payload={"action": "edit", "sensitivity": "confidential"})
    assert status == 200, body

    # The trusted key is not shown the correction any more, in the route or in the trace a review embeds.
    status, trace, _ = h.request("GET", path, key=trusted)
    assert status == 200, trace
    assert trace["events"] and not _reviews_naming(trace["events"], replacement)
    # The ids of a replacement that remain are in the changes of the update of the superseded memory, the documented exception.
    assert not {"memory.reviewed", "agent.memory_superseded"} & set(_mentions(trace["events"], replacement))
    assert trace["summary"]["event_count"] == len(trace["events"])
    status, body, _ = h.request("POST", f"/v0/vnext/sources/{source_id}/review", key=trusted, payload={"action": "review", "review_note": "again"})
    assert status == 200, body
    assert body["trace"]["events"] and not _reviews_naming(body["trace"]["events"], replacement)
    assert not {"memory.reviewed", "agent.memory_superseded"} & set(_mentions(body["trace"]["events"], replacement))

    # The unbound admin key and the owner are not limited. They keep the correction, and so does the stored log.
    status, trace, _ = h.request("GET", path, key=admin)
    assert status == 200 and _reviews_naming(trace["events"], replacement)
    with h.store() as store:
        with label_read_scope(store):
            owner = _vnext_load_source_trace(store=store, source=store.get_source(source_id), identity=None)
        assert _reviews_naming(owner["events"], replacement)
        expected = {str(row["id"]) for row in store.list_events_for_source_trace(source_id=source_id)}
        assert {str(row["id"]) for row in owner["events"]} == expected


def test_every_payload_field_that_names_a_hidden_row_is_judged_in_the_trace(label_harness):
    h = label_harness
    source = h.source(sensitivity="public")
    source_id = str(source["id"])
    hidden_source = h.source(sensitivity="confidential")
    hidden_source_id = str(hidden_source["id"])
    memory_id = str(h.memory(source=source)["id"])
    with h.store() as store:
        hidden_memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden fact", "status": "active", "domain": "project", "sensitivity": "confidential"})
        hidden_id = str(hidden_memory["id"])
        cases = {
            "a candidate memory": ("memory.candidate_created", "memory", memory_id, {"candidate_memory_id": hidden_id}),
            "a list of memories": ("memory.quarantine_sweep", "memory", memory_id, {"expired_memory_ids": [memory_id, hidden_id]}),
            "a source": ("source.reviewed", "source", source_id, {"source_id": hidden_source_id}),
            "a source that replaced the source": ("source.superseded", "source", source_id, {"superseded_by": hidden_source_id}),
            "a memory that replaced the memory": ("agent.memory_superseded", "memory", memory_id, {"superseded_by": hidden_id}),
            "an id in capitals": ("memory.candidate_created", "memory", memory_id, {"candidate_memory_id": hidden_id.upper()}),
            "an id that is not a row": ("memory.candidate_created", "memory", memory_id, {"candidate_memory_id": "not-an-id"}),
        }
        readable = {
            "a readable memory": ("memory.candidate_created", "memory", memory_id, {"candidate_memory_id": memory_id}),
            "a readable source": ("source.reviewed", "source", source_id, {"source_id": source_id}),
            "no row": ("source.reviewed", "source", source_id, {"review_note": "checked"}),
        }
        withheld = {name: str(store.append_event(build_event_log_record(event_type=kind, actor_type="system", target_type=target, target_id=target_id, payload=payload))["id"]) for name, (kind, target, target_id, payload) in cases.items()}
        shown = {name: str(store.append_event(build_event_log_record(event_type=kind, actor_type="system", target_type=target, target_id=target_id, payload=payload))["id"]) for name, (kind, target, target_id, payload) in readable.items()}
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    path = f"/v0/vnext/traces/sources/{source_id}"
    status, trace, _ = h.request("GET", path, key=trusted)
    assert status == 200, trace
    kept = {str(event["id"]) for event in trace["events"]}
    assert {name for name, event_id in withheld.items() if event_id in kept} == set()
    assert {name for name, event_id in shown.items() if event_id not in kept} == set()
    text = json.dumps(trace, default=str)
    assert hidden_id not in text and hidden_id.upper() not in text and hidden_source_id not in text
    status, trace, _ = h.request("GET", path, key=admin)
    assert status == 200
    assert set(withheld.values()) | set(shown.values()) <= {str(event["id"]) for event in trace["events"]}


def test_hidden_events_in_front_do_not_crowd_out_the_readable_ones(label_harness):
    h = label_harness
    source = h.source(sensitivity="public")
    source_id = str(source["id"])
    memory_id = str(h.memory(source=source)["id"])
    with h.store() as store:
        hidden_id = str(store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Hidden fact", "status": "active", "domain": "project", "sensitivity": "confidential"})["id"])
        readable = [str(store.append_event(build_event_log_record(event_type="memory.updated", actor_type="system", target_type="memory", target_id=memory_id, payload={"n": index}))["id"]) for index in range(3)]
        for index in range(_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT + 20):
            store.append_event(build_event_log_record(event_type="memory.reviewed", actor_type="system", target_type="memory", target_id=memory_id, payload={"replacement_memory_id": hidden_id, "n": index}))
    trusted, admin = h.key("trusted_local_agent"), h.key("admin_agent")
    status, trace, _ = h.request("GET", f"/v0/vnext/traces/sources/{source_id}", key=trusted)
    assert status == 200, trace
    assert set(readable) <= {str(event["id"]) for event in trace["events"]}
    assert not [event for event in trace["events"] if event["event_type"] == "memory.reviewed"]
    assert trace["sampling"]["collection_complete"]["events"] is True
    assert hidden_id not in json.dumps(trace, default=str)
    status, trace, _ = h.request("GET", f"/v0/vnext/traces/sources/{source_id}", key=admin)
    assert status == 200 and len(trace["events"]) == _VNEXT_SOURCE_TRACE_COLLECTION_LIMIT
    assert trace["sampling"]["collection_complete"]["events"] is False


@pytest.mark.parametrize("profile", ["read_only_agent", "memory_proposal_agent"])
def test_a_profile_the_operator_gate_refuses_still_gets_no_trace(label_harness, profile):
    h = label_harness
    source = h.source(sensitivity="public")
    key = h.key(profile)
    status, body, _ = h.request("GET", f"/v0/vnext/traces/sources/{source['id']}", key=key)
    assert status == 403, body
    assert "events" not in body
