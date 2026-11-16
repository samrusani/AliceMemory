"""Legacy event routes in development are keyless local-machine routes."""

import pytest

from alicebot_api.config import Settings
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


@pytest.mark.parametrize("route", ["threads", "traces"])
def test_legacy_event_routes_ignore_key_profile_in_development(label_harness, monkeypatch, route):
    """The security page changes if these legacy routes start using agent keys.

    Mutation: refuse an authorization header at the legacy-v0 gate in main.py.
    """
    from alicebot_api.routers import memories_legacy

    h = label_harness
    monkeypatch.setattr(memories_legacy, "get_settings", lambda: Settings(database_url=h.urls["app"]))
    with h.store() as vnext:
        store = ContinuityStore(vnext.conn)
        thread = store.create_thread("Synthetic thread")
        session = store.create_session(thread["id"], status="active")
        store.append_event(thread["id"], session["id"], "message.user", {"text": "Synthetic event"})
        trace = store.create_trace(
            user_id=h.user_id, thread_id=thread["id"], kind="context.compile", compiler_version="continuity_v0",
            status="completed", limits={},
        )
        store.append_trace_event(trace_id=trace["id"], sequence_no=1, kind="synthetic.event", payload={"marker": "synthetic"})
    row_id = thread["id"] if route == "threads" else trace["id"]
    path = f"/v0/{route}/{row_id}/events"
    status, body, _ = h.request("GET", path)
    assert status == 200 and len(body["items"]) == 1
    for profile in PERMISSION_PROFILES:
        for project in (None, "alpha"):
            key = h.key(profile, project=project)
            status, body, _ = h.request("GET", path, key=key)
            assert status == 200 and len(body["items"]) == 1, (profile, project, body)
    status, body, _ = h.request("GET", path)
    assert status == 200 and len(body["items"]) == 1
