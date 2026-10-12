"""A report an answer carries is read again only for a caller that has limits.

``readable_artifact`` and ``withhold_unreadable_artifact`` decide what a run, a generation route or a tool answers with. The
owner and an unbound ``admin_agent`` key have no limits, so for them both hand back what they were given and read nothing:
the answers of those callers stay as they were, at the cost they had. A caller with limits has the report read again from the
store, and a report the store no longer holds is not answered. The behaviour on a real vault is tested in
``tests/integration/test_scheduler_run_response_admission_postgres.py``.

Mutations: make the unfenced branch of ``readable_artifact`` read the store; make ``withhold_unreadable_artifact`` return
the report for a caller whose report is not readable.
"""

from __future__ import annotations

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_label_guard import readable_artifact
from alicebot_api.vnext_scheduler import withhold_unreadable_artifact

REPORT = {"id": "11111111-1111-4111-8111-111111111111", "title": "Report", "sensitivity": "confidential"}
RUN = {
    "id": "22222222-2222-4222-8222-222222222222",
    "status": "succeeded",
    "artifact_id": REPORT["id"],
    "metadata_json": {"artifact_id": REPORT["id"], "manual_run": True},
}


class _Store:
    """A store that records every read and holds no report."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def get_artifact(self, artifact_id: str):
        self.reads.append(artifact_id)
        return None


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(agent_id="agent", permission_profile=profile, auth="agent_api_key")


def test_the_owner_and_an_unbound_admin_key_are_handed_the_report_and_nothing_is_read() -> None:
    for identity in (None, _identity("admin_agent")):
        store = _Store()
        assert readable_artifact(store, identity, REPORT) is REPORT
        run, report = withhold_unreadable_artifact(store, identity, run=RUN, artifact=REPORT)
        assert run is RUN and report is REPORT
        assert store.reads == []


def test_a_caller_with_limits_is_not_answered_a_report_the_store_does_not_hold() -> None:
    for profile in ("read_only_agent", "trusted_local_agent", "project_scoped_agent"):
        store = _Store()
        assert readable_artifact(store, _identity(profile), REPORT) is None
        assert store.reads == [REPORT["id"]]


def test_a_run_is_answered_without_the_id_of_a_report_the_caller_may_not_read() -> None:
    store = _Store()
    run, report = withhold_unreadable_artifact(store, _identity("trusted_local_agent"), run=RUN, artifact=REPORT)
    assert report is None
    assert run["artifact_id"] is None and run["metadata_json"] == {"manual_run": True}
    assert run["id"] == RUN["id"] and run["status"] == "succeeded"
    # The run it was given is not changed: the stored run keeps the id for the owner.
    assert RUN["artifact_id"] == REPORT["id"] and RUN["metadata_json"]["artifact_id"] == REPORT["id"]


def test_a_run_that_made_no_report_is_answered_as_it_is() -> None:
    store = _Store()
    failed = {"id": RUN["id"], "status": "failed", "artifact_id": None, "metadata_json": {}}
    run, report = withhold_unreadable_artifact(store, _identity("trusted_local_agent"), run=failed, artifact=None)
    assert run is failed and report is None and store.reads == []
