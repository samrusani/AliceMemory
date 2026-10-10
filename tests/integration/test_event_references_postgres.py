"""On PostgreSQL the feed and the count judge an event by the rows it names, through the native queries.

The unit test runs the same cases on SQLite. Here the count reads the cut-down payload with SQL, leaves out the source
events that name no other row and counts those natively, and a graph edge is read by its ends (a belief end too, which only
this store can read). Each number is compared with the per-row guard of a store that offers none of the native shortcuts.
The id of a project that an event repeats from the client, and the replacement of a corrected memory, are judged here as well.
"""
import json
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

        shown, withheld = assert_events_are_judged_by_the_rows_they_name(store, oracle=PerTarget(), beliefs=True)
    assert len(shown) == 18 and len(withheld) == 33


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


CEILING = ("public", "internal", "private", "unknown")


def _readable_events(store):
    """The events a caller with the ceiling of a trusted key is shown, and the number the count query reaches."""
    with label_read_scope(store):
        guard = LabelGuard(store, active=True, sensitivity_allowed=CEILING)
        kept = guard.admit_events(store.list_events())
        return kept, guard.readable_event_count()


def test_a_project_id_an_event_repeats_in_another_spelling_is_judged_as_the_project(label_harness):
    """A client may send the id of a project in capitals or with spaces round it, and the event repeats what it sent.

    A source assigned to a readable project twice, once with the lower-case id and once with the capitals, gives two
    events that a trusted key is shown. A project the key may not read hides the event whatever the
    spelling, and text that is not an id of any project hides it too.
    """
    h = label_harness
    open_project, secret_project = str(uuid4()), str(uuid4())
    with h.store() as store:
        store.create_project({"id": open_project, "name": "Open", "slug": f"open-{open_project}", "domain": "project", "sensitivity": "public"})
        store.create_project({"id": secret_project, "name": "Secret", "slug": f"secret-{secret_project}", "domain": "project", "sensitivity": "confidential"})
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    status, body, _ = h.request("POST", "/v0/vnext/sources", key=admin, payload={"raw_text": "A public note about Atlas.", "title": "Public note", "domain": "project", "sensitivity": "public"})
    assert status in (200, 201), body
    source_id = body["source_id"]
    spellings = [open_project, open_project.upper(), f"  {open_project}  ", f" {open_project.upper()} "]
    for spelling in spellings:
        status, body, _ = h.request("POST", f"/v0/vnext/sources/{source_id}/review", key=admin, payload={"action": "assign_project", "project_id": spelling, "confirm_label_hide": True})
        assert status == 200, body
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200, workspace
    assigned = [event for event in workspace["recent_events"] if event["event_type"] == "source.assigned_project"]
    assert sorted(event["payload_json"]["project_id"] for event in assigned) == sorted(spellings)

    with h.store() as store:
        def append(project_id):
            return str(store.append_event(build_event_log_record(event_type="source.assigned_project", actor_type="system", target_type="entity", target_id=str(uuid4()), payload={"project_id": project_id}))["id"])

        shown = {append(spelling) for spelling in (open_project, open_project.upper(), f"  {open_project}  ", f"\t{open_project.upper()}\n")}
        withheld = {append(spelling) for spelling in (secret_project, secret_project.upper(), f"  {secret_project}  ", str(uuid4()), str(uuid4()).upper(), "alpha-team")}
        kept, count = _readable_events(store)
        kept_ids = {str(event["id"]) for event in kept}
        assert shown <= kept_ids and not withheld & kept_ids
        assert count == len(kept)
        # The per-row guard of a store with no native shortcut reaches the same events.
        class PerTarget:
            def read_label_rows(self, kind, ids):
                return store.read_label_rows(kind, ids)

            def iter_label_events(self):
                return store.iter_label_events()

        with label_read_scope(PerTarget()):
            assert LabelGuard(PerTarget(), active=True, sensitivity_allowed=CEILING).readable_event_count() == len(kept)


def test_a_correction_that_replaces_a_memory_names_the_replacement_and_the_replacement_is_judged(label_harness, monkeypatch):
    """The correction writes memory.reviewed with the id of the new memory. The memory made confidential afterwards is not named."""
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    h = label_harness
    alpha = str(uuid4())
    with h.store() as store:
        store.create_project({"id": alpha, "name": "Atlas", "slug": f"atlas-{alpha}", "domain": "project", "sensitivity": "public"})
        old = store.create_memory({"memory_key": "alpha.pref", "memory_type": "semantic", "title": "Old preference", "canonical_text": "Atlas likes old things", "summary": "old", "value": {"text": "Atlas likes old things"}, "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [alpha]}})
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    call_mcp_tool(MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id), name="alice_memory_correct", arguments={"review_item_id": str(old["id"]), "action": "supersede-existing", "replacement_title": "New preference", "replacement_body": {"text": "Atlas likes new things"}, "reason": "changed"})
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories WHERE supersedes = %s", (str(old["id"]),))
        replacements = [row["id"] for row in cur.fetchall()]
    assert len(replacements) == 1
    replacement = replacements[0]

    def reviewed(feed):
        return [event for event in feed if event["event_type"] == "memory.reviewed" and event["payload_json"].get("replacement_memory_id") == replacement]

    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200, workspace
    assert reviewed(workspace["recent_events"]) and reviewed(workspace["agent_activity"]["recent_events"])
    status, body, _ = h.request("POST", f"/v0/vnext/memories/{replacement}/review", key=admin, payload={"action": "edit", "sensitivity": "confidential"})
    assert status == 200, body
    status, audit, _ = h.request("GET", f"/v0/vnext/memories/{replacement}/audit", key=trusted)
    assert status == 404, audit
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200, workspace
    assert not reviewed(workspace["recent_events"]) and not reviewed(workspace["agent_activity"]["recent_events"])
    for feed in (workspace["recent_events"], workspace["agent_activity"]["recent_events"]):
        for event in feed:
            if replacement in json.dumps(event, default=str):
                # The one event that still carries the id is the update of the superseded memory, whose changes copy the
                # pointer the row keeps. That is the documented exception for the changes of an update.
                assert event["event_type"] == "memory.updated", event["event_type"]
                assert "changes" in event["payload_json"]
    with h.store() as store:
        kept, count = _readable_events(store)
        assert count == len(kept)
        assert not [event for event in kept if event["event_type"] == "memory.reviewed" and replacement in json.dumps(event, default=str)]
    # The count the workspace gives a trusted key is the number of events it may be shown, the corrected memory's included.
    assert workspace["summary"]["event_count"] == count


def test_the_policy_event_of_an_explain_of_a_continuity_object_is_shown_to_the_owner_and_an_unbound_admin_only(label_harness, monkeypatch):
    """A key-bound explain of a continuity object leaves a policy event that names the object, and only for an object the key's
    policy allows. The labels of the object sit in its provenance, in the legacy store, so the guard cannot show that a
    reader with narrower limits may read it: the event is not shown to a caller with limits, in the feed or in the count.
    """
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.store import ContinuityStore

    h = label_harness
    ids = {}
    with h.store() as store:
        legacy = ContinuityStore(store.conn)
        for name, provenance in (("default", {}), ("confidential", {"sensitivity": "confidential", "domain": "project"})):
            capture = legacy.create_continuity_capture_event(raw_content=f"Decision: {name}", explicit_signal="decision", admission_posture="DERIVED", admission_reason="explicit_signal_decision")
            ids[name] = str(legacy.create_continuity_object(capture_event_id=capture["id"], object_type="Decision", status="active", title=f"Decision: {name}", body={"decision_text": name}, provenance=provenance, confidence=0.9)["id"])
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    for key in (admin, trusted):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        for object_id in ids.values():
            try:
                call_mcp_tool(context, name="alice_explain", arguments={"continuity_object_id": object_id})
            except Exception:  # noqa: BLE001 - only the policy event that the authorization wrote is read
                pass
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    with h.store() as store:
        written = [event for event in store.list_events() if event["target_type"] == "continuity_object"]
        # The admin key is allowed both objects and the trusted key the one that carries no label, and no key left an event
        # for an object it may not read.
        assert sorted(event["target_id"] for event in written) == sorted([ids["default"], ids["confidential"], ids["default"]])
        assert all(event["event_type"] == "policy.decision" for event in written)
        kept, count = _readable_events(store)
        assert count == len(kept)
        assert not [event for event in kept if event["target_type"] == "continuity_object"]
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200, workspace
    for feed in (workspace["recent_events"], workspace["agent_activity"]["recent_events"]):
        assert not [event for event in feed if event["target_type"] == "continuity_object"]
        assert not [event for event in feed if any(object_id in json.dumps(event, default=str) for object_id in ids.values())]
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=admin)
    assert status == 200, workspace
    assert [event for event in workspace["agent_activity"]["recent_events"] if event["target_type"] == "continuity_object"]


def _tree_event_refs(tree):
    return {child["ref"] for root in tree["roots"] if root["id"] == "root:events" for child in root["children"]}


def test_the_context_tree_shows_the_policy_event_of_an_explain_of_a_continuity_object_to_the_owner_and_an_unbound_admin_only(label_harness, monkeypatch):
    """The tree filters by a selection of sensitivities, and the owner's default one leaves out the confidential levels.
    That selection is not a limit: the owner and an unbound admin key keep the event under it, and a key with limits does
    not see it. The owner can read the route only while no key exists, so its read comes first, with an event written by hand;
    the keys then read it with the event that a real explain leaves. The tool is read with a declared profile and with none."""
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.routers import vnext_retrieval
    from alicebot_api.store import ContinuityStore

    h = label_harness
    with h.store() as store:
        legacy = ContinuityStore(store.conn)
        capture = legacy.create_continuity_capture_event(raw_content="Decision: tree", explicit_signal="decision", admission_posture="DERIVED", admission_reason="explicit_signal_decision")
        object_id = str(legacy.create_continuity_object(capture_event_id=capture["id"], object_type="Decision", status="active", title="Decision: tree", body={"decision_text": "tree"}, provenance={}, confidence=0.9)["id"])
        by_hand = f"event:{store.append_event(build_event_log_record(event_type='policy.decision', actor_type='agent', actor_id='by-hand', target_type='continuity_object', target_id=object_id, payload={'policy_decision': {'decision': 'allowed'}}))['id']}"

    def route(key):
        response = vnext_retrieval.get_vnext_context_tree(user_id=h.user_id, query="", domains=None, sensitivity_allowed=None, limit=50, include_events=True, authorization=f"Bearer {key}" if key else None)
        assert response.status_code == 200, response.body
        return _tree_event_refs(json.loads(response.body))

    # No key exists yet, so the owner may read the route, and the default selection does not hide the event from it.
    assert by_hand in route(None)
    admin, trusted = h.key("admin_agent"), h.key("trusted_local_agent")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    call_mcp_tool(context, name="alice_explain", arguments={"continuity_object_id": object_id})
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    with h.store() as store:
        written = {f"event:{event['id']}" for event in store.list_events() if event["target_type"] == "continuity_object"}
    assert len(written) == 2 and by_hand in written
    (explained,) = written - {by_hand}

    # Through the mounted application, right after the explain so that its event is among the newest the default page holds.
    status, tree, _ = h.request("GET", "/v0/vnext/context-tree", key=admin)
    assert status == 200, tree
    assert explained in _tree_event_refs(tree)
    status, tree, _ = h.request("GET", "/v0/vnext/context-tree", key=trusted)
    assert status == 200, tree
    assert explained not in _tree_event_refs(tree)

    def tool(identity):
        arguments = {"limit": 50, **(identity or {})}
        return _tree_event_refs(call_mcp_tool(context, name="alice_vnext_context_tree", arguments=arguments))

    assert written <= route(admin)
    assert not written & route(trusted)
    assert written <= tool(None)
    assert written <= tool({"agent_id": "declared-admin", "permission_profile": "admin_agent"})
    assert not written & tool({"agent_id": "declared-trusted", "permission_profile": "trusted_local_agent"})
