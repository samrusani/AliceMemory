"""Round-four regressions: the clamp lock, the project edge and the source view audit."""
import threading
import time
from uuid import uuid4

import pytest

from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_project_scope import project_identifier_identity, project_scope_identity
from tests.integration.conftest import lock_label_fixture
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401

PA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SOURCE_ROUTE = "/v0/vnext/sources/{source_id}"


def _weekly_rows(h, count=1):
    """Derived rows whose floor needs PA and PB, so an assignment to PA alone is refused."""
    with h.store() as store:
        lock_label_fixture(store)
        sources = [
            store.create_source({"source_type": "note", "title": "s", "content_hash": str(uuid4()), "domain": "project",
                                 "sensitivity": "public", "metadata_json": {"project_scope": [project]}})
            for project in (PA, PB)
        ]
        rows = []
        for _ in range(count):
            metadata = with_derived_from({"discovered_by": "vnext_weekly_synthesis"}, {"sources": sources})
            rows.append(store.create_memory({"memory_key": str(uuid4()), "canonical_text": "weekly", "status": "candidate",
                                             "domain": "project", "sensitivity": "public", "metadata_json": metadata}))
    return rows


def _assign_pa(store, row_id):
    row = store.get_memory(row_id)
    return store.update_memory(
        memory_id=row_id,
        patch={"project_id": PA, "metadata_json": {**row["metadata_json"], "project_scope": [PA]}},
        label_write=True,
    )


def _clamp_events(store, row_id):
    return [event for event in store.list_events(limit=200)
            if event["event_type"] == "memory.labels_raised" and event.get("target_id") == row_id
            and event["payload_json"].get("cause") == "floor_clamped"]


def _labels(row):
    floor = row["metadata_json"].get("project_floor") or []
    return (row["domain"], row["sensitivity"], tuple(row["project_scope"]), frozenset(floor))


def test_two_shared_lock_writers_with_a_refused_assignment_both_succeed(label_harness, monkeypatch):
    import alicebot_api.vnext_label_writes as writes
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", False)  # the production setting
    h = label_harness
    rows = _weekly_rows(h, 2)
    with h.store() as store:
        before = [_labels(store.get_memory(str(row["id"]))) for row in rows]
    ready = threading.Barrier(2, timeout=20)
    outcomes = {}

    def writer(index):
        try:
            with h.store() as store:
                store.lock_label_writes()  # the shared grant a caller holds before it edits
                ready.wait()
                _assign_pa(store, str(rows[index]["id"]))
                outcomes[index] = ("ok", store._label_floor_applied)
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            outcomes[index] = (type(exc).__name__, str(exc).splitlines()[0][:80])

    threads = [threading.Thread(target=writer, args=(index,)) for index in (0, 1)]
    [thread.start() for thread in threads]
    deadline = time.monotonic() + 30
    for thread in threads:
        thread.join(max(0.1, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "a writer is still waiting"
    assert outcomes == {0: ("ok", True), 1: ("ok", True)}, outcomes
    with h.store() as store:
        for row, labels in zip(rows, before):
            row_id = str(row["id"])
            assert _labels(store.get_memory(row_id)) == labels
            events = _clamp_events(store, row_id)
            assert len(events) == 1
            payload = events[0]["payload_json"]
            assert payload["previous"] == payload["new"]


def test_refused_assignment_by_a_shared_holder_passes_strict_ordering(label_harness):
    import alicebot_api.vnext_label_writes as writes
    assert writes.STRICT_LOCK_ORDER is True
    h = label_harness
    (row,) = _weekly_rows(h)
    row_id = str(row["id"])
    with h.store() as store:
        labels = _labels(store.get_memory(row_id))
        store.lock_graph_mutation()
        store.lock_label_writes()  # shared only; strict ordering refuses a late upgrade
        graph, shared, exclusive = writes.held_label_locks(store)
        assert (shared, exclusive) == (True, False)
        updated = _assign_pa(store, row_id)
        assert store._label_floor_applied is True
        assert writes.held_label_locks(store)[2] is False
        assert _labels(updated) == labels
    with h.store() as store:
        assert _labels(store.get_memory(row_id)) == labels
        assert len(_clamp_events(store, row_id)) == 1


def _stale_confidential_copy(h):
    """A derived row stored below its input, so a label edit raises the stored label."""
    from alicebot_api.vnext_label_writes import without_insert_floor
    hidden = h.source(sensitivity="confidential")
    with h.store() as store, without_insert_floor():
        metadata = with_derived_from({"workflow": "project_auto_update"}, {"sources": [hidden]})
        return store.create_memory({"memory_key": str(uuid4()), "canonical_text": "stale copy", "status": "candidate",
                                    "domain": "project", "sensitivity": "public", "metadata_json": metadata})


def _global_derived_row(h):
    source = h.source()
    with h.store() as store:
        metadata = with_derived_from({"discovered_by": "vnext_weekly_synthesis"}, {"sources": [source]})
        return store.create_memory({"memory_key": str(uuid4()), "canonical_text": "weekly", "status": "candidate",
                                    "domain": "project", "sensitivity": "public", "metadata_json": metadata})


def _edit_sensitivity(level):
    def edit(store, row_id):
        return store.update_memory(memory_id=row_id, patch={"sensitivity": level}, label_write=True)
    return edit


def test_a_stored_label_change_still_needs_the_exclusive_lock(label_harness, monkeypatch):
    """A shared holder cannot change a stored label: accepted, clamped or unjudged."""
    import alicebot_api.vnext_label_writes as writes
    h = label_harness
    accepted = str(_global_derived_row(h)["id"])
    clamped = str(_stale_confidential_copy(h)["id"])
    unjudged = str(_global_derived_row(h)["id"])
    # Raising a derived row above its inputs stands. Naming public on a copy of a
    # confidential input is clamped back up. Both change the stored label.
    attempts = [(accepted, _edit_sensitivity("confidential")), (clamped, _edit_sensitivity("public"))]

    def refused_for_a_shared_holder(row_id, edit):
        with h.store() as store:
            before = _labels(store.get_memory(row_id))
            store.lock_graph_mutation()
            store.lock_label_writes()
            with pytest.raises(writes.LabelLockOrderError):
                edit(store, row_id)
        with h.store() as store:
            assert _labels(store.get_memory(row_id)) == before

    for row_id, edit in attempts:
        refused_for_a_shared_holder(row_id, edit)
    # Inputs too deep to settle: the edit is stored as asked, so it still changes the label.
    with monkeypatch.context() as patched:
        patched.setattr(writes, "collect_label_rows", lambda *args, **kwargs: ([], True))
        refused_for_a_shared_holder(unjudged, _edit_sensitivity("confidential"))
    # The same edits succeed once the caller holds the exclusive lock first.
    for row_id, edit in attempts:
        with h.store() as store:
            store.lock_graph_mutation()
            writes.acquire_exclusive_label_lock(store)
            edit(store, row_id)
    with h.store() as store:
        assert store.get_memory(accepted)["sensitivity"] == "confidential"
        assert store.get_memory(clamped)["sensitivity"] == "confidential"
        assert len(_clamp_events(store, clamped)) == 1
        assert _clamp_events(store, accepted) == []


def _project_with_slug(h):
    project_id = str(uuid4())
    with h.store() as store:
        store.create_project({"id": project_id, "name": "Alpha Team", "slug": "alpha-team"})
    return project_id


def _assign_over_http(h, row_id, project_id):
    status, body, _ = h.request("POST", f"/v0/vnext/memories/{row_id}/review",
                                payload={"action": "assign_project", "project_id": project_id})
    assert status == 200, body
    return body


def _project_edges(h, row_id):
    with h.store() as store:
        return [edge for edge in store.list_edges(from_id=row_id) if edge["edge_type"] == "belongs_to_project"]


@pytest.mark.parametrize("spelling", ["{uuid}", " {uuid} ", "  alpha-team  ", "\talpha-team\n", "ALPHA-TEAM", "alpha   team"])
def test_a_plain_assignment_writes_the_project_edge_for_every_spelling(label_harness, spelling):
    h = label_harness
    project_id = _project_with_slug(h)
    requested = spelling.format(uuid=project_id)
    with h.store() as store:
        memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "plain candidate", "status": "candidate",
                                      "domain": "project", "sensitivity": "public", "metadata_json": {}})
    row_id = str(memory["id"])
    _assign_over_http(h, row_id, requested)
    with h.store() as store:
        stored = store.get_memory(row_id)["project_scope"]
    assert [project_identifier_identity(item) for item in stored] == [project_identifier_identity(requested)]
    edges = _project_edges(h, row_id)
    assert len(edges) == 1
    assert project_scope_identity(edges[0]["to_id"]) == project_scope_identity(requested)


@pytest.mark.parametrize("spelling", ["{uuid}", " {uuid} "])
def test_a_refused_assignment_writes_no_project_edge(label_harness, spelling):
    h = label_harness
    projects = [str(uuid4()), str(uuid4())]
    sources = [h.source(scope=(project,)) for project in projects]
    with h.store() as store:
        for project in projects:
            store.create_project({"id": project, "name": project, "slug": project})
        metadata = with_derived_from({"discovered_by": "vnext_weekly_synthesis"}, {"sources": sources})
        memory = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "Synthetic weekly summary", "status": "candidate",
                                      "domain": "project", "sensitivity": "public", "metadata_json": metadata})
    row_id = str(memory["id"])
    body = _assign_over_http(h, row_id, spelling.format(uuid=projects[0]))
    assert body.get("label_floor_applied") is True
    with h.store() as store:
        assert store.get_memory(row_id)["project_scope"] == []
    assert _project_edges(h, row_id) == []


def _audit(h):
    with h.store() as store:
        events = [dict(event) for event in store.list_events(limit=1000)
                  if str(event["event_type"]).startswith(("policy.", "agent."))]
        identities = sorted(str(row["agent_id"]) for row in store.list_agent_identities(limit=200))
    return events, identities


PROFILES = [
    ("trusted_local_agent", False, True),
    ("admin_agent", False, True),
    ("trusted_local_agent", True, False),
    ("admin_agent", True, False),
    ("read_only_agent", False, False),
    ("memory_proposal_agent", False, False),
    ("project_scoped_agent", True, False),
]


@pytest.mark.parametrize("profile,bound,admitted", PROFILES)
def test_source_view_audit_and_id_check_per_profile(label_harness, profile, bound, admitted):
    h = label_harness
    alpha = str(uuid4())
    with h.store() as store:
        store.create_project({"id": alpha, "name": alpha, "slug": alpha})
    source = h.source(scope=(alpha,))
    key = h.key(profile, project=alpha if bound else None)
    events_before, identities_before = _audit(h)
    known = {str(event["id"]) for event in events_before}

    status, body, _ = h.request("GET", "/v0/vnext/sources/" + str(source["id"]), key=key)
    events, identities = _audit(h)
    new = [event for event in events if str(event["id"]) not in known]
    if admitted:
        assert status == 200 and str(body["id"]) == str(source["id"])
        # An allowed read writes no policy event and records no agent identity.
        assert new == []
        assert identities == identities_before
    else:
        assert status == 403 and "raw_text" not in body
        assert body["policy_decision"]["decision"] == "blocked"
        assert sorted(event["event_type"] for event in new) == ["agent.policy_blocked", "policy.decision"]
        for event in new:
            assert (event["target_type"], event["target_id"]) == ("http_route", SOURCE_ROUTE)
            assert event["payload_json"]["policy_decision"]["decision"] == "blocked"
        # The refusal is recorded against the route and nothing else.
        assert identities == identities_before

    # A malformed id: the gate answers first, so a refused key hears 403 and an admitted key hears 422.
    known = {str(event["id"]) for event in events}
    status, body, _ = h.request("GET", "/v0/vnext/sources/not-a-uuid", key=key)
    new = [event for event in _audit(h)[0] if str(event["id"]) not in known]
    if admitted:
        assert status == 422
        assert [(item["type"], item["loc"]) for item in body["detail"]] == [("uuid_parsing", ["path", "source_id"])]
        assert new == []
    else:
        assert status == 403 and "detail" in body and body["detail"] == "agent policy blocked this action"
        assert sorted(event["event_type"] for event in new) == ["agent.policy_blocked", "policy.decision"]


def test_keyless_owner_source_view_writes_nothing_and_checks_the_id(label_harness):
    h = label_harness
    source = h.source()
    events_before, identities_before = _audit(h)
    status, body, _ = h.request("GET", "/v0/vnext/sources/" + str(source["id"]))
    assert status == 200 and str(body["id"]) == str(source["id"])
    status, body, _ = h.request("GET", "/v0/vnext/sources/not-a-uuid")
    assert status == 422
    assert [(item["type"], item["loc"]) for item in body["detail"]] == [("uuid_parsing", ["path", "source_id"])]
    missing = str(uuid4())
    status, body, _ = h.request("GET", "/v0/vnext/sources/" + missing.upper())
    assert status == 404 and body == {"detail": f"vNext source {missing} was not found"}
    assert _audit(h) == (events_before, identities_before)
