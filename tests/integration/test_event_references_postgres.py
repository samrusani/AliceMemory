"""On PostgreSQL the feed and the count judge an event by the rows it names, through the native queries.

The unit test runs the same cases on SQLite. Here the count reads the cut-down payload with SQL, leaves out the source
events that name no other row and counts those natively, and a graph edge is read by its ends. Each number is compared with
the per-row guard of a store that offers none of the native shortcuts.
"""
from uuid import uuid4

from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)


def test_events_that_name_a_row_are_judged_alike_by_the_native_count_the_list_and_the_per_row_guard(label_harness):
    from tests.unit.test_event_references import assert_events_are_judged_by_the_rows_they_name

    with label_harness.store() as store:

        class PerTarget:
            """Only the per-row reads: no native count of source events, no prefilter."""

            def read_label_rows(self, kind, ids):
                return store.read_label_rows(kind, ids)

            def iter_label_events(self):
                return store.iter_label_events()

        shown, withheld = assert_events_are_judged_by_the_rows_they_name(store, oracle=PerTarget())
    assert len(shown) >= 10 and len(withheld) >= 12


def test_a_source_event_that_names_another_row_leaves_the_native_source_count(label_harness):
    """The native count of source events judges the target only, so an event with a reference must not be counted by it."""
    h = label_harness
    public, hidden = h.source(sensitivity="public"), h.source(sensitivity="confidential")
    ceiling = ("public", "internal", "private", "unknown")
    with h.store() as store:
        plain = store.append_event(build_event_log_record(event_type="source.reviewed", actor_type="system", target_type="source", target_id=str(public["id"]), payload={}))
        naming_hidden = store.append_event(build_event_log_record(event_type="source.superseded", actor_type="system", target_type="source", target_id=str(public["id"]), payload={"superseded_by": str(hidden["id"])}))
        naming_readable = store.append_event(build_event_log_record(event_type="source.superseded", actor_type="system", target_type="source", target_id=str(public["id"]), payload={"superseded_by": str(public["id"])}))
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=ceiling)
            natively = store.count_source_label_events(sensitivity_allowed=ceiling)
            iterated = {str(row["id"]) for batch in store.iter_label_events(exclude_source_targets=True) for row in batch}
            kept = {str(row["id"]) for row in guard.admit_events(store.list_events())}
            # Events with no reference stay in the native count and out of the iteration, the others do the opposite.
            assert str(plain["id"]) not in iterated
            assert {str(naming_hidden["id"]), str(naming_readable["id"])} <= iterated
            assert natively == len([e for e in store.list_events(target_type="source") if str(e["id"]) not in iterated and str(e["target_id"]) == str(public["id"])])
            assert str(plain["id"]) in kept and str(naming_readable["id"]) in kept and str(naming_hidden["id"]) not in kept
            assert guard.readable_event_count() == len(kept)


def test_the_workspace_feed_reads_past_the_events_a_key_may_not_read(label_harness):
    """Three confidential captures leave the latest events of the log hidden from a trusted key. The feed still fills."""
    import json

    from tests.integration.test_hidden_ids_in_report_metadata_postgres import capture_confidential_source

    h = label_harness
    alpha = str(uuid4())
    with h.store() as store:
        store.create_project({"id": alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"})
        for index in range(25):
            store.append_event(build_event_log_record(event_type="scheduler.due_scan", actor_type="system", payload={"due_count": index}))
        # Sixty events by an agent that name no row, older than the captures, for the agent feed to fill itself with.
        for index in range(60):
            store.append_event(build_event_log_record(event_type="agent.memory_proposed", actor_type="agent", actor_id="older", payload={"index": index}))
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    captures = [capture_confidential_source(h, alpha, admin, statements=False, size=8) for _ in range(3)]
    hidden = set().union(*(capture["ids"] for capture in captures))
    status, body, _ = h.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200, body
    feed = body["recent_events"]
    assert len(feed) == 20, [event["event_type"] for event in feed]
    text = json.dumps(feed, default=str)
    assert not [row_id for row_id in hidden if row_id in text]
    assert body["summary"]["event_count"] >= len(feed)
    agent_feed = body["agent_activity"]["recent_events"]
    assert len(agent_feed) == 50, [event["event_type"] for event in agent_feed]
    assert not [row_id for row_id in hidden if row_id in json.dumps(agent_feed, default=str)]
    # The owner of the data is not limited and reads the newest events of the log, the chunk events among them.
    status, body, _ = h.request("GET", "/v0/vnext/workspace", key=admin)
    assert status == 200 and len(body["recent_events"]) == 20 and len(body["agent_activity"]["recent_events"]) == 50
    assert any(event["event_type"] == "source_chunk.created" for event in body["recent_events"])
