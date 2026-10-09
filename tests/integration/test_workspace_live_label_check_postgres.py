"""The owner and admin workspace run the derived-labels check live.

A page load that skips the check, or shows an older answer, would report zero
rows below their inputs on a vault that has a gap. These tests create a real gap
and read it through the workspace payload and the HTTP route with no command run
before, then repair it and read the clean answer on the next load. The workspace
must also stay free of label events, and a restricted key never sees the count.

Mutation: stop passing the content diagnostics to the doctor for the owner and an
unbound admin key.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from alicebot_api.cli import labels
from alicebot_api.routers.workspaces import _vnext_workspace_payload
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_label_writes import without_insert_floor

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401

ADMIN = AgentIdentity(agent_id="live-admin", permission_profile="admin_agent")
TRUSTED = AgentIdentity(agent_id="live-trusted", permission_profile="trusted_local_agent")

GAP = "derived labels: 1 below their inputs, 0 unverified"
CLEAN = "derived labels: 0 below their inputs, 0 unverified"


def context(h):
    return SimpleNamespace(database_url=h.urls["app"], user_id=h.user_id)


def make_gap(h):
    """A report stored public whose input source is confidential."""
    hidden = h.source(sensitivity="confidential")
    with h.store() as store, without_insert_floor():
        report = store.create_memory({
            "memory_key": str(uuid4()), "canonical_text": "Synthetic report", "status": "active",
            "domain": "project", "sensitivity": "public",
            "metadata_json": with_derived_from({"workflow": "project_auto_update"}, {"sources": [hidden]}),
        })
    return hidden, report


def workspace(h, identity):
    with h.store() as store:
        payload = _vnext_workspace_payload(store, identity=identity)
    return payload, next(row for row in payload["doctor"]["checks"] if row["name"] == "derived_labels")


def event_types(h) -> list[str]:
    with h.store() as store:
        return sorted(row["event_type"] for row in store.conn.execute("SELECT event_type FROM event_log").fetchall())


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "admin"])
def test_workspace_reports_a_real_gap_on_the_first_load_and_the_clean_answer_after_repair(label_harness, identity):
    h = label_harness
    make_gap(h)
    with h.store() as store:
        assert label_gap_counts(store) == (1, 0)
        doctor = next(c for c in VNextDoctorService(store).run(ci=True)["checks"] if c["name"] == "derived_labels")
    assert (doctor["status"], doctor["severity"], doctor["message"]) == ("fail", "warning", GAP)

    before = event_types(h)
    payload, check = workspace(h, identity)
    assert (check["status"], check["severity"], check["message"]) == ("fail", "warning", GAP)
    assert check["recommended_fix"] == "alicebot vnext labels repair"
    assert "alicebot vnext labels repair" in payload["doctor"]["recommended_fixes"]
    assert payload["doctor"]["warning_count"] >= 1
    # The first load seeds the default scheduler workflows, as it always did. It records no label event,
    # and a second load writes nothing at all.
    after = event_types(h)
    assert not [name for name in after if name not in before and "label" in name], after
    workspace(h, identity)
    assert event_types(h) == after, "the workspace must not write"

    assert labels._run_vnext_labels_repair(context(h), None).startswith("labels repair updated 1")
    payload, check = workspace(h, identity)
    assert (check["status"], check["message"]) == ("pass", CLEAN)
    assert "alicebot vnext labels repair" not in payload["doctor"]["recommended_fixes"]


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "admin"])
def test_workspace_follows_a_gap_created_after_an_earlier_clean_load(label_harness, identity):
    """Nothing survives a request: a load that was clean says nothing about the next one."""
    h = label_harness
    _payload, clean = workspace(h, identity)
    assert (clean["status"], clean["message"]) == ("pass", CLEAN)
    make_gap(h)
    _payload, check = workspace(h, identity)
    assert (check["status"], check["message"]) == ("fail", GAP)


def test_a_restricted_key_never_sees_the_count(label_harness):
    h = label_harness
    make_gap(h)
    _payload, check = workspace(h, TRUSTED)
    assert check["status"] == "skipped"
    assert check["details"] == {"scope": "filtered_workspace", "evaluated": False}
    assert "below their inputs" not in check["message"]


def test_the_http_route_reports_the_gap_for_the_owner(label_harness):
    """The page a person opens, not only the payload function behind it."""
    h = label_harness
    make_gap(h)
    status, body, _headers = h.request("GET", "/v0/vnext/workspace")
    assert status == 200
    check = next(row for row in body["doctor"]["checks"] if row["name"] == "derived_labels")
    assert (check["status"], check["message"]) == ("fail", GAP)
