from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = b"" if payload is None else json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    query_string = urlencode(query_params or {}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_user(
    database_url: str,
    *,
    email: str,
    agent_profile_id: str = "assistant_default",
) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Policy thread", agent_profile_id=agent_profile_id)

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def test_consent_endpoints_upsert_and_list_deterministically(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    first_status, first_payload = invoke_request(
        "POST",
        "/v0/consents",
        payload={
            "user_id": str(seeded["user_id"]),
            "consent_key": "email_marketing",
            "status": "granted",
            "metadata": {"source": "settings"},
        },
    )
    second_status, second_payload = invoke_request(
        "POST",
        "/v0/consents",
        payload={
            "user_id": str(seeded["user_id"]),
            "consent_key": "analytics_tracking",
            "status": "revoked",
            "metadata": {"source": "banner"},
        },
    )
    third_status, third_payload = invoke_request(
        "POST",
        "/v0/consents",
        payload={
            "user_id": str(seeded["user_id"]),
            "consent_key": "email_marketing",
            "status": "revoked",
            "metadata": {"source": "preferences"},
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/consents",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert first_status == 201
    assert second_status == 201
    assert third_status == 200
    assert first_payload["write_mode"] == "created"
    assert second_payload["write_mode"] == "created"
    assert third_payload["write_mode"] == "updated"
    assert third_payload["consent"]["id"] == first_payload["consent"]["id"]
    assert list_status == 200
    assert [item["consent_key"] for item in list_payload["items"]] == [
        "analytics_tracking",
        "email_marketing",
    ]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["consent_key_asc", "created_at_asc", "id_asc"],
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        stored_consents = ContinuityStore(conn).list_consents()

    assert [consent["consent_key"] for consent in stored_consents] == [
        "analytics_tracking",
        "email_marketing",
    ]
    assert stored_consents[1]["status"] == "revoked"
    assert stored_consents[1]["metadata"] == {"source": "preferences"}


def test_policy_endpoints_create_list_and_get_in_priority_order(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    low_priority_status, low_priority_payload = invoke_request(
        "POST",
        "/v0/policies",
        payload={
            "user_id": str(seeded["user_id"]),
            "name": "Require approval for export",
            "action": "memory.export",
            "scope": "profile",
            "effect": "require_approval",
            "priority": 20,
            "active": True,
            "conditions": {"channel": "email"},
            "required_consents": ["email_marketing", "email_marketing"],
            "agent_profile_id": "coach_default",
        },
    )
    high_priority_status, high_priority_payload = invoke_request(
        "POST",
        "/v0/policies",
        payload={
            "user_id": str(seeded["user_id"]),
            "name": "Allow profile read",
            "action": "memory.read",
            "scope": "profile",
            "effect": "allow",
            "priority": 10,
            "active": True,
            "conditions": {},
            "required_consents": [],
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/policies",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/policies/{low_priority_payload['policy']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert low_priority_status == 201
    assert high_priority_status == 201
    assert low_priority_payload["policy"]["agent_profile_id"] == "coach_default"
    assert high_priority_payload["policy"]["agent_profile_id"] is None
    assert low_priority_payload["policy"]["required_consents"] == ["email_marketing"]
    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        high_priority_payload["policy"]["id"],
        low_priority_payload["policy"]["id"],
    ]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["priority_asc", "created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"policy": low_priority_payload["policy"]}


def test_policy_evaluation_allow_records_trace_events(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="email_marketing",
            status="granted",
            metadata={"source": "settings"},
        )
        created_policy = store.create_policy(
            name="Allow export",
            action="memory.export",
            scope="profile",
            effect="allow",
            priority=10,
            active=True,
            conditions={"channel": "email"},
            required_consents=["email_marketing"],
        )

    status_code, payload = invoke_request(
        "POST",
        "/v0/policies/evaluate",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "action": "memory.export",
            "scope": "profile",
            "attributes": {"channel": "email"},
        },
    )

    assert status_code == 200
    assert payload["decision"] == "allow"
    assert payload["matched_policy"]["id"] == str(created_policy["id"])
    assert payload["evaluation"] == {
        "action": "memory.export",
        "scope": "profile",
        "evaluated_policy_count": 1,
        "matched_policy_id": str(created_policy["id"]),
        "order": ["priority_asc", "created_at_asc", "id_asc"],
    }
    assert [reason["code"] for reason in payload["reasons"]] == [
        "matched_policy",
        "policy_effect_allow",
    ]
    assert payload["trace"]["trace_event_count"] == 3

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(UUID(payload["trace"]["trace_id"]))
        trace_events = store.list_trace_events(UUID(payload["trace"]["trace_id"]))

    assert trace["kind"] == "policy.evaluate"
    assert trace["compiler_version"] == "policy_evaluation_v0"
    assert trace["limits"] == {
        "order": ["priority_asc", "created_at_asc", "id_asc"],
        "active_policy_count": 1,
        "consent_count": 1,
    }
    assert [event["kind"] for event in trace_events] == [
        "policy.evaluate.request",
        "policy.evaluate.order",
        "policy.evaluate.decision",
    ]
    assert trace_events[2]["payload"]["decision"] == "allow"
    assert trace_events[2]["payload"]["matched_policy_id"] == str(created_policy["id"])


def test_policy_evaluation_scopes_to_global_and_thread_profile_policies(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(
        migrated_database_urls["app"],
        email="owner@example.com",
        agent_profile_id="coach_default",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        mismatched = store.create_policy(
            agent_profile_id="assistant_default",
            name="Mismatched deny",
            action="memory.export",
            scope="profile",
            effect="deny",
            priority=1,
            active=True,
            conditions={},
            required_consents=[],
        )
        global_policy = store.create_policy(
            agent_profile_id=None,
            name="Global approval",
            action="memory.export",
            scope="profile",
            effect="require_approval",
            priority=5,
            active=True,
            conditions={},
            required_consents=[],
        )
        matched = store.create_policy(
            agent_profile_id="coach_default",
            name="Matched allow",
            action="memory.export",
            scope="profile",
            effect="allow",
            priority=10,
            active=True,
            conditions={},
            required_consents=[],
        )

    status_code, payload = invoke_request(
        "POST",
        "/v0/policies/evaluate",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "action": "memory.export",
            "scope": "profile",
            "attributes": {},
        },
    )

    assert status_code == 200
    assert payload["decision"] == "require_approval"
    assert payload["matched_policy"]["id"] == str(global_policy["id"])
    assert payload["evaluation"]["evaluated_policy_count"] == 2

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace_events = store.list_trace_events(UUID(payload["trace"]["trace_id"]))

    assert trace_events[1]["kind"] == "policy.evaluate.order"
    assert trace_events[1]["payload"]["policy_ids"] == [
        str(global_policy["id"]),
        str(matched["id"]),
    ]
    assert str(mismatched["id"]) not in trace_events[1]["payload"]["policy_ids"]


def test_policy_evaluation_denies_when_required_consent_is_missing(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        ContinuityStore(conn).create_policy(
            name="Allow export with consent",
            action="memory.export",
            scope="profile",
            effect="allow",
            priority=10,
            active=True,
            conditions={},
            required_consents=["email_marketing"],
        )

    status_code, payload = invoke_request(
        "POST",
        "/v0/policies/evaluate",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "action": "memory.export",
            "scope": "profile",
            "attributes": {},
        },
    )

    assert status_code == 200
    assert payload["decision"] == "deny"
    assert [reason["code"] for reason in payload["reasons"]] == [
        "matched_policy",
        "consent_missing",
    ]


def test_policy_evaluation_returns_require_approval(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        created_policy = ContinuityStore(conn).create_policy(
            name="Escalate export",
            action="memory.export",
            scope="profile",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={},
            required_consents=[],
        )

    status_code, payload = invoke_request(
        "POST",
        "/v0/policies/evaluate",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "action": "memory.export",
            "scope": "profile",
            "attributes": {},
        },
    )

    assert status_code == 200
    assert payload["decision"] == "require_approval"
    assert payload["matched_policy"]["id"] == str(created_policy["id"])
    assert payload["reasons"][-1]["code"] == "policy_effect_require_approval"


def test_policy_and_consent_endpoints_enforce_per_user_isolation(migrated_database_urls, monkeypatch) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(consent_key="email_marketing", status="granted", metadata={})
        owner_policy = store.create_policy(
            name="Allow export",
            action="memory.export",
            scope="profile",
            effect="allow",
            priority=10,
            active=True,
            conditions={},
            required_consents=["email_marketing"],
        )

    consent_status, consent_payload = invoke_request(
        "GET",
        "/v0/consents",
        query_params={"user_id": str(intruder["user_id"])},
    )
    policy_status, policy_payload = invoke_request(
        "GET",
        "/v0/policies",
        query_params={"user_id": str(intruder["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/policies/{owner_policy['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )
    evaluation_status, evaluation_payload = invoke_request(
        "POST",
        "/v0/policies/evaluate",
        payload={
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "action": "memory.export",
            "scope": "profile",
            "attributes": {},
        },
    )

    assert consent_status == 200
    assert consent_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["consent_key_asc", "created_at_asc", "id_asc"],
        },
    }
    assert policy_status == 200
    assert policy_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["priority_asc", "created_at_asc", "id_asc"],
        },
    }
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert evaluation_status == 200
    assert evaluation_payload["decision"] == "deny"
    assert evaluation_payload["matched_policy"] is None
    assert evaluation_payload["reasons"] == [
        {
            "code": "no_matching_policy",
            "source": "system",
            "message": "No active policy matched the requested action, scope, and attributes.",
            "policy_id": None,
            "consent_key": None,
        }
    ]
