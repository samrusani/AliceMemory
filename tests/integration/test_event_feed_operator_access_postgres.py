"""The operator event feeds retain the access rule described on the pages."""

import pytest

from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


@pytest.mark.parametrize("route", [
    "/v0/vnext/workspace", "/v0/vnext/agents/policy-telemetry",
    "/v0/vnext/connectors/local_folder/status", "/v0/vnext/traces/sources/{source_id}",
    "/v0/vnext/memories/{memory_id}/audit", "/v0/vnext/context-tree",
])
def test_event_feed_operator_access_for_every_profile(label_harness, route):
    """The page changes if the operator access rule changes.

    Mutation: return None instead of evaluating http.operator.access in main.py.
    """
    harness = label_harness
    if "{source_id}" in route:
        route = route.format(source_id=harness.source()["id"])
    if "{memory_id}" in route:
        route = route.format(memory_id=harness.memory()["id"])
    status, _, _ = harness.request("GET", route)
    assert status == 200
    for profile in PERMISSION_PROFILES:
        for bound in (False, True):
            key = harness.key(profile, project="alpha" if bound else None)
            status, _, _ = harness.request("GET", route, key=key)
            expected = 200 if not bound and profile in {"trusted_local_agent", "admin_agent"} else 403
            assert status == expected, (profile, bound, route)
    status, _, _ = harness.request("GET", route)
    assert status == 401


def test_source_review_event_trace_uses_operator_access(label_harness):
    """The saved-quotes page changes if the source-review gate changes.

    Mutation: return None instead of evaluating http.operator.access in main.py.
    """
    harness = label_harness
    source = harness.source()
    route = f"/v0/vnext/sources/{source['id']}/review"
    payload = {"action": "review"}
    status, body, _ = harness.request("POST", route, payload=payload)
    assert status == 200 and "events" in body["trace"]
    for profile in PERMISSION_PROFILES:
        for bound in (False, True):
            key = harness.key(profile, project="alpha" if bound else None)
            status, body, _ = harness.request("POST", route, payload=payload, key=key)
            expected = 200 if not bound and profile in {"trusted_local_agent", "admin_agent"} else 403
            assert status == expected, (profile, bound, body)
            if status == 200:
                assert "events" in body["trace"]
    status, _, _ = harness.request("POST", route, payload=payload)
    assert status == 401
