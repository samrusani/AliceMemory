"""The /v1 policy commits refusals before dispatch with real agent keys."""

from __future__ import annotations

import pytest
from starlette.responses import JSONResponse

from alicebot_api import main as main_module
from alicebot_api.config import Settings
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import LabelHarness, label_harness  # noqa: F401


@pytest.mark.parametrize(
    ("profile", "project", "reasons"),
    (
        ("read_only_agent", None, {"restricted_sensitivity_filtered", "admin_agent_required_for_v1_route"}),
        ("project_scoped_agent", "alpha", {"admin_agent_required_for_v1_route", "unbound_operator_key_required"}),
    ),
)
def test_v1_refusal_commits_policy_events_without_dispatch(
    label_harness: LabelHarness, monkeypatch: pytest.MonkeyPatch,
    profile: str, project: str | None, reasons: set[str],
) -> None:
    """In v0.24.0 both keys passed authentication and reached dispatch."""
    harness = label_harness
    settings = Settings(database_url=harness.urls["app"], auth_user_id=str(harness.user_id))
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    dispatched: list[str] = []

    async def probe(scope, receive, send):
        dispatched.append(scope["path"])
        await JSONResponse({"reached": True})(scope, receive, send)

    monkeypatch.setattr(main_module.app.router, "middleware_stack", probe)
    admin_key = harness.key("admin_agent")
    with harness.store() as store:
        _, limited_key = create_agent_key(
            store, user_id=harness.user_id, agent_id="v1-policy-fixture",
            permission_profile=profile, project_scope=project,
        )
        before = {event["id"] for event in store.list_events()}
    path = "/v1/policy-probe"
    assert harness.request("POST", path, payload={}, key=admin_key)[:2] == (200, {"reached": True})
    assert harness.request("POST", path, payload={}, key=limited_key)[:2] == (
        403, {"detail": {"code": "forbidden", "message": "The requested action is not permitted"}},
    )
    assert dispatched == [path]

    with harness.store() as store:
        events = [event for event in store.list_events() if event["id"] not in before]
    assert len(events) == 2
    assert {event["event_type"] for event in events} == {"policy.decision", "agent.policy_blocked"}
    assert events[0]["trace_id"] == events[1]["trace_id"]
    for event in events:
        assert (event["actor_type"], event["actor_id"]) == ("agent", "v1-policy-fixture")
        assert (event["target_type"], event["target_id"]) == ("http_route", "/v1")
        payload = event["payload_json"]
        assert payload["agent_identity"]["auth"] == "agent_api_key"
        assert payload["agent_identity"]["permission_profile"] == profile
        assert payload["agent_identity"]["project_scope"] == ([project] if project else [])
        decision = payload["policy_decision"]
        assert (decision["action"], decision["decision"]) == ("http.v1.access", "blocked")
        assert decision["trace_id"] == event["trace_id"]
        assert set(decision["reasons"]) == reasons
