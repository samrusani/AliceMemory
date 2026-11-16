"""A readable artifact's event trace is reachable to each key profile."""

from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.operator_route_vault import _EMPTY_DERIVATION


def test_each_profile_can_trace_events_of_a_readable_artifact(label_harness):
    """The saved-quotes page changes if a profile cannot reach its readable artifact trace.

    Mutation: make the artifact trace discard its event rows.
    """
    h = label_harness
    with h.store() as store:
        artifact = store.create_artifact({
            "artifact_type": "daily_brief", "title": "Synthetic", "content_markdown": "Synthetic content",
            "status": "draft", "domain": "project", "sensitivity": "public",
            "metadata_json": {"project_scope": ["alpha"], "derived_from": _EMPTY_DERIVATION},
        })
    path = f"/v0/vnext/traces/artifacts/{artifact['id']}"
    status, body, _ = h.request("GET", path)
    assert status == 200 and body["events"]
    for profile in PERMISSION_PROFILES:
        for project in (None, "alpha"):
            key = h.key(profile, project=project)
            status, body, _ = h.request("GET", path, key=key)
            assert status == 200 and body["events"], (profile, project, body)
    status, _, _ = h.request("GET", path)
    assert status == 401
