"""HTTP policy refusals and the Postgres audit row.

Raising AgentPolicyBlockedError out of user_connection rolls the audit
event back with the mutation. Returning 403 from inside the connection
keeps the event. A second connection reads the event.
"""

from __future__ import annotations

from uuid import uuid4

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_projects as vnext_projects_router
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user


def _reasons(event: dict) -> list:
    payload = event.get("payload_json")
    if not isinstance(payload, dict):
        return []
    decision = payload.get("policy_decision")
    if not isinstance(decision, dict):
        return []
    reasons = decision.get("reasons")
    return list(reasons) if isinstance(reasons, list) else []


def _blocked(store: PostgresVNextStore, *, target_type: str, target_id: str) -> list[dict]:
    events = store.list_events(target_type=target_type, target_id=target_id)
    return [
        event
        for event in events
        if event.get("event_type") == "agent.policy_blocked" and "sensitivity_above_agent_ceiling" in _reasons(event)
    ]


def test_http_ceiling_refusal_keeps_the_policy_event(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="ceiling-audit@example.com")
    settings = Settings(database_url=app_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_projects_router, "get_settings", lambda: settings)
    user_id_text = str(user_id)

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": f"memory.{uuid4()}",
                "value": {"text": "A confidential note the keyed agent must not forget."},
                "status": "active",
                "memory_type": "semantic",
                "title": "Confidential note",
                "canonical_text": "A confidential note the keyed agent must not forget.",
                "summary": "A confidential note.",
                "domain": "professional",
                "sensitivity": "confidential",
                "confirmation_status": "confirmed",
            }
        )
        loop = store.create_open_loop(
            {
                "title": "Highly sensitive follow-up",
                "status": "open",
                "domain": "personal",
                "sensitivity": "highly_sensitive",
            }
        )
        _record, raw_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="hermes",
            permission_profile="trusted_local_agent",
        )
    memory_id = str(memory["id"])
    loop_id = str(loop["id"])
    authorization = f"Bearer {raw_key}"

    forget_status, forget_body = invoke_request(
        "POST",
        "/v0/vnext/memories/forget",
        payload={"user_id": user_id_text, "memory_id": memory_id, "reason": "should not land"},
        authorization=authorization,
    )
    assert forget_status == 403, forget_body
    assert "sensitivity_above_agent_ceiling" in forget_body["policy_decision"]["reasons"]

    review_status, review_body = invoke_request(
        "POST",
        f"/v0/vnext/open-loops/{loop_id}/review",
        payload={"user_id": user_id_text, "action": "close"},
        authorization=authorization,
    )
    assert review_status == 403, review_body
    assert "sensitivity_above_agent_ceiling" in review_body["policy_decision"]["reasons"]

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        kept = store.get_memory(memory_id)
        assert kept is not None
        assert kept["status"] == "active"
        assert kept["sensitivity"] == "confidential"
        kept_loop = store.get_open_loop(loop_id)
        assert kept_loop is not None
        assert kept_loop["status"] == "open"
        assert kept_loop["title"] == "Highly sensitive follow-up"
        memory_events = _blocked(store, target_type="memory", target_id=memory_id)
        loop_events = _blocked(store, target_type="open_loop", target_id=loop_id)
    assert memory_events, "forget refusal did not keep agent.policy_blocked"
    assert all(event.get("target_type") == "memory" for event in memory_events)
    assert loop_events, "open-loop refusal did not keep agent.policy_blocked"
    assert all(event.get("target_type") == "open_loop" for event in loop_events)


def test_http_quality_rating_refusal_drops_the_policy_event(migrated_database_urls, monkeypatch) -> None:
    """A quality-rating 403 leaves no agent.policy_blocked row.

    The handler writes the policy events, then AgentPolicyBlockedError
    leaves user_connection, so Postgres rolls those rows back before
    the 403.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="quality-rating-audit@example.com")
    settings = Settings(database_url=app_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_review_router, "get_settings", lambda: settings)
    user_id_text = str(user_id)

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        artifact = store.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Private brief a read-only agent must not rate",
                "content_markdown": "Stored brief text.",
                "status": "needs_review",
                "domain": "professional",
                "sensitivity": "private",
            },
            actor_type="user",
        )
        _record, raw_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="quality-reader",
            permission_profile="read_only_agent",
        )
    artifact_id = str(artifact["id"])

    status, body = invoke_request(
        "POST",
        f"/v0/vnext/artifacts/{artifact_id}/quality-ratings",
        payload={
            "user_id": user_id_text,
            "verbosity": "right_sized",
            "usefulness": 5,
        },
        authorization=f"Bearer {raw_key}",
    )
    assert status == 403, body
    assert "read_only_agent_cannot_write" in body["policy_decision"]["reasons"]

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        ratings = store.list_artifact_quality_ratings(artifact_id=artifact_id, limit=20)
        events = store.list_events(target_type="artifact", target_id=artifact_id)
    blocked = [event for event in events if event.get("event_type") == "agent.policy_blocked"]
    decisions = [event for event in events if event.get("event_type") == "policy.decision"]
    assert ratings == []
    assert blocked == [], "quality-rating refusal kept agent.policy_blocked"
    assert decisions == []
