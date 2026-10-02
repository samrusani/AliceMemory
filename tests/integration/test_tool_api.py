from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
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


def seed_user(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Tool thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def test_tool_endpoints_create_list_and_get_in_deterministic_order(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    second_status, second_payload = invoke_request(
        "POST",
        "/v0/tools",
        payload={
            "user_id": str(seeded["user_id"]),
            "tool_key": "zeta.fetch",
            "name": "Zeta Fetch",
            "description": "Fetch zeta records.",
            "version": "2.0.0",
            "active": True,
            "tags": ["fetch"],
            "action_hints": ["tool.run"],
            "scope_hints": ["workspace"],
            "domain_hints": [],
            "risk_hints": [],
            "metadata": {"transport": "proxy"},
        },
    )
    first_status, first_payload = invoke_request(
        "POST",
        "/v0/tools",
        payload={
            "user_id": str(seeded["user_id"]),
            "tool_key": "alpha.open",
            "name": "Alpha Open",
            "description": "Open alpha pages.",
            "version": "1.0.0",
            "active": True,
            "tags": ["browser"],
            "action_hints": ["tool.run"],
            "scope_hints": ["workspace"],
            "domain_hints": ["docs"],
            "risk_hints": [],
            "metadata": {"transport": "proxy"},
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tools",
        query_params={"user_id": str(seeded["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tools/{second_payload['tool']['id']}",
        query_params={"user_id": str(seeded["user_id"])},
    )

    assert first_status == 201
    assert second_status == 201
    assert list_status == 200
    assert [item["tool_key"] for item in list_payload["items"]] == ["alpha.open", "zeta.fetch"]
    assert list_payload["summary"] == {
        "total_count": 2,
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
    }
    assert detail_status == 200
    assert detail_payload == {"tool": second_payload["tool"]}
    assert first_payload["tool"]["metadata_version"] == "tool_metadata_v0"


def test_tool_allowlist_evaluation_returns_allowed_denied_and_approval_required_with_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="web_access",
            status="granted",
            metadata={"source": "settings"},
        )
        allowed_tool = store.create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        denied_by_metadata_tool = store.create_tool(
            tool_key="calendar.read",
            name="Calendar Read",
            description="Read calendars.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["calendar"],
            action_hints=["calendar.read"],
            scope_hints=["calendar"],
            domain_hints=[],
            risk_hints=[],
            metadata={},
        )
        denied_by_consent_tool = store.create_tool(
            tool_key="contacts.export",
            name="Contacts Export",
            description="Export contacts.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["contacts"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        approval_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Allow docs browser",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=10,
            active=True,
            conditions={"tool_key": "browser.open", "domain_hint": "docs"},
            required_consents=["web_access"],
        )
        store.create_policy(
            name="Allow contacts export with consent",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=20,
            active=True,
            conditions={"tool_key": "contacts.export", "domain_hint": "docs"},
            required_consents=["contacts_consent"],
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=30,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    status_code, payload = invoke_request(
        "POST",
        "/v0/tools/allowlist/evaluate",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {"channel": "chat"},
        },
    )

    assert status_code == 200
    assert [item["tool"]["id"] for item in payload["allowed"]] == [str(allowed_tool["id"])]
    assert [item["tool"]["id"] for item in payload["approval_required"]] == [str(approval_tool["id"])]
    assert [item["tool"]["id"] for item in payload["denied"]] == [
        str(denied_by_metadata_tool["id"]),
        str(denied_by_consent_tool["id"]),
    ]
    assert [reason["code"] for reason in payload["denied"][0]["reasons"]] == [
        "tool_action_unsupported",
        "tool_scope_unsupported",
    ]
    assert [reason["code"] for reason in payload["denied"][1]["reasons"]] == [
        "tool_metadata_matched",
        "matched_policy",
        "consent_missing",
    ]
    assert payload["summary"] == {
        "action": "tool.run",
        "scope": "workspace",
        "domain_hint": "docs",
        "risk_hint": None,
        "evaluated_tool_count": 4,
        "allowed_count": 1,
        "denied_count": 2,
        "approval_required_count": 1,
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
    }
    assert payload["trace"]["trace_event_count"] == 7

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(UUID(payload["trace"]["trace_id"]))
        trace_events = store.list_trace_events(UUID(payload["trace"]["trace_id"]))

    assert trace["kind"] == "tool.allowlist.evaluate"
    assert trace["compiler_version"] == "tool_allowlist_evaluation_v0"
    assert trace["limits"] == {
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
        "active_tool_count": 4,
        "active_policy_count": 3,
        "consent_count": 1,
    }
    assert [event["kind"] for event in trace_events] == [
        "tool.allowlist.request",
        "tool.allowlist.order",
        "tool.allowlist.decision",
        "tool.allowlist.decision",
        "tool.allowlist.decision",
        "tool.allowlist.decision",
        "tool.allowlist.summary",
    ]
    assert trace_events[2]["payload"]["decision"] == "allowed"
    assert trace_events[-1]["payload"] == {
        "allowed_count": 1,
        "denied_count": 2,
        "approval_required_count": 1,
    }


def test_tool_route_returns_ready_denied_and_approval_required_with_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="web_access",
            status="granted",
            metadata={"source": "settings"},
        )
        ready_tool = store.create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        denied_tool = store.create_tool(
            tool_key="calendar.read",
            name="Calendar Read",
            description="Read calendars.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["calendar"],
            action_hints=["calendar.read"],
            scope_hints=["calendar"],
            domain_hints=[],
            risk_hints=[],
            metadata={},
        )
        approval_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        ready_policy = store.create_policy(
            name="Allow docs browser",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=10,
            active=True,
            conditions={"tool_key": "browser.open", "domain_hint": "docs"},
            required_consents=["web_access"],
        )
        approval_policy = store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=20,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    ready_status, ready_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(ready_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {"channel": "chat"},
        },
    )
    denied_status, denied_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(denied_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )
    approval_status, approval_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(approval_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert ready_status == 200
    assert list(ready_payload) == ["request", "decision", "tool", "reasons", "summary", "trace"]
    assert ready_payload["decision"] == "ready"
    assert ready_payload["request"] == {
        "thread_id": str(seeded["thread_id"]),
        "tool_id": str(ready_tool["id"]),
        "action": "tool.run",
        "scope": "workspace",
        "domain_hint": "docs",
        "risk_hint": None,
        "attributes": {"channel": "chat"},
    }
    assert ready_payload["tool"]["id"] == str(ready_tool["id"])
    assert [reason["code"] for reason in ready_payload["reasons"]] == [
        "tool_metadata_matched",
        "matched_policy",
        "policy_effect_allow",
    ]
    assert ready_payload["summary"] == {
        "thread_id": str(seeded["thread_id"]),
        "tool_id": str(ready_tool["id"]),
        "action": "tool.run",
        "scope": "workspace",
        "domain_hint": "docs",
        "risk_hint": None,
        "decision": "ready",
        "evaluated_tool_count": 1,
        "active_policy_count": 2,
        "consent_count": 1,
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
    }
    assert ready_payload["trace"]["trace_event_count"] == 3

    assert denied_status == 200
    assert denied_payload["decision"] == "denied"
    assert [reason["code"] for reason in denied_payload["reasons"]] == [
        "tool_action_unsupported",
        "tool_scope_unsupported",
    ]
    assert denied_payload["summary"]["decision"] == "denied"

    assert approval_status == 200
    assert approval_payload["decision"] == "approval_required"
    assert approval_payload["summary"]["decision"] == "approval_required"
    assert approval_payload["reasons"][-1] == {
        "code": "policy_effect_require_approval",
        "source": "policy",
        "message": "Policy effect resolved the decision to 'require_approval'.",
        "tool_id": str(approval_tool["id"]),
        "policy_id": str(approval_policy["id"]),
        "consent_key": None,
    }

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        ready_trace = store.get_trace(UUID(ready_payload["trace"]["trace_id"]))
        ready_trace_events = store.list_trace_events(UUID(ready_payload["trace"]["trace_id"]))

    assert ready_trace["kind"] == "tool.route"
    assert ready_trace["compiler_version"] == "tool_routing_v0"
    assert ready_trace["limits"] == {
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
        "evaluated_tool_count": 1,
        "active_policy_count": 2,
        "consent_count": 1,
    }
    assert [event["kind"] for event in ready_trace_events] == [
        "tool.route.request",
        "tool.route.decision",
        "tool.route.summary",
    ]
    assert ready_trace_events[1]["payload"] == {
        "tool_id": str(ready_tool["id"]),
        "tool_key": "browser.open",
        "tool_version": "1.0.0",
        "allowlist_decision": "allowed",
        "routing_decision": "ready",
        "matched_policy_id": str(ready_policy["id"]),
        "reasons": ready_payload["reasons"],
    }
    assert ready_trace_events[2]["payload"] == {
        "decision": "ready",
        "evaluated_tool_count": 1,
        "active_policy_count": 2,
        "consent_count": 1,
    }


def test_tool_route_validates_invalid_thread_and_tool(migrated_database_urls, monkeypatch) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        tool = ContinuityStore(conn).create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )

    invalid_thread_status, invalid_thread_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(uuid4()),
            "tool_id": str(tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )
    invalid_tool_status, invalid_tool_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(uuid4()),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert invalid_thread_status == 400
    assert invalid_thread_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert invalid_tool_status == 400
    assert invalid_tool_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_tool_endpoints_and_allowlist_enforce_per_user_isolation(migrated_database_urls, monkeypatch) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        owner_tool = ContinuityStore(conn).create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/tools",
        query_params={"user_id": str(intruder["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/tools/{owner_tool['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )
    evaluation_status, evaluation_payload = invoke_request(
        "POST",
        "/v0/tools/allowlist/evaluate",
        payload={
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert list_status == 200
    assert list_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
        },
    }
    assert detail_status == 404
    assert detail_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert evaluation_status == 200
    assert evaluation_payload["allowed"] == []
    assert evaluation_payload["denied"] == []
    assert evaluation_payload["approval_required"] == []
    assert evaluation_payload["summary"]["evaluated_tool_count"] == 0


def test_tool_routing_returns_ready_denied_and_approval_required_with_trace(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_user(migrated_database_urls["app"], email="owner@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_consent(
            consent_key="web_access",
            status="granted",
            metadata={"source": "settings"},
        )
        ready_tool = store.create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        denied_tool = store.create_tool(
            tool_key="contacts.export",
            name="Contacts Export",
            description="Export contacts.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["contacts"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        approval_tool = store.create_tool(
            tool_key="shell.exec",
            name="Shell Exec",
            description="Run shell commands.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["shell"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "local"},
        )
        store.create_policy(
            name="Allow docs browser",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=10,
            active=True,
            conditions={"tool_key": "browser.open", "domain_hint": "docs"},
            required_consents=["web_access"],
        )
        store.create_policy(
            name="Allow contacts export with consent",
            action="tool.run",
            scope="workspace",
            effect="allow",
            priority=20,
            active=True,
            conditions={"tool_key": "contacts.export", "domain_hint": "docs"},
            required_consents=["contacts_consent"],
        )
        store.create_policy(
            name="Require shell approval",
            action="tool.run",
            scope="workspace",
            effect="require_approval",
            priority=30,
            active=True,
            conditions={"tool_key": "shell.exec"},
            required_consents=[],
        )

    ready_status, ready_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(ready_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {"channel": "chat"},
        },
    )
    denied_status, denied_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(denied_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "domain_hint": "docs",
            "attributes": {},
        },
    )
    approval_status, approval_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(seeded["user_id"]),
            "thread_id": str(seeded["thread_id"]),
            "tool_id": str(approval_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert ready_status == 200
    assert ready_payload["decision"] == "ready"
    assert ready_payload["tool"]["id"] == str(ready_tool["id"])
    assert ready_payload["summary"] == {
        "thread_id": str(seeded["thread_id"]),
        "tool_id": str(ready_tool["id"]),
        "action": "tool.run",
        "scope": "workspace",
        "domain_hint": "docs",
        "risk_hint": None,
        "decision": "ready",
        "evaluated_tool_count": 1,
        "active_policy_count": 3,
        "consent_count": 1,
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
    }
    assert ready_payload["trace"]["trace_event_count"] == 3

    assert denied_status == 200
    assert denied_payload["decision"] == "denied"
    assert [reason["code"] for reason in denied_payload["reasons"]] == [
        "tool_metadata_matched",
        "matched_policy",
        "consent_missing",
    ]

    assert approval_status == 200
    assert approval_payload["decision"] == "approval_required"
    assert approval_payload["reasons"][-1]["code"] == "policy_effect_require_approval"

    with user_connection(migrated_database_urls["app"], seeded["user_id"]) as conn:
        store = ContinuityStore(conn)
        trace = store.get_trace(UUID(ready_payload["trace"]["trace_id"]))
        trace_events = store.list_trace_events(UUID(ready_payload["trace"]["trace_id"]))

    assert trace["kind"] == "tool.route"
    assert trace["compiler_version"] == "tool_routing_v0"
    assert trace["limits"] == {
        "order": ["tool_key_asc", "version_asc", "created_at_asc", "id_asc"],
        "evaluated_tool_count": 1,
        "active_policy_count": 3,
        "consent_count": 1,
    }
    assert [event["kind"] for event in trace_events] == [
        "tool.route.request",
        "tool.route.decision",
        "tool.route.summary",
    ]
    assert trace_events[1]["payload"]["allowlist_decision"] == "allowed"
    assert trace_events[1]["payload"]["routing_decision"] == "ready"
    assert trace_events[2]["payload"] == {
        "decision": "ready",
        "evaluated_tool_count": 1,
        "active_policy_count": 3,
        "consent_count": 1,
    }


def test_tool_routing_validates_invalid_references_and_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        owner_tool = ContinuityStore(conn).create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )

    invalid_thread_status, invalid_thread_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(uuid4()),
            "tool_id": str(owner_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )
    invalid_tool_status, invalid_tool_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(uuid4()),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )
    isolation_status, isolation_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "tool_id": str(owner_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert invalid_thread_status == 400
    assert invalid_thread_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert invalid_tool_status == 400
    assert invalid_tool_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert isolation_status == 400
    assert isolation_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_tool_route_enforces_per_user_isolation(migrated_database_urls, monkeypatch) -> None:
    owner = seed_user(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_user(migrated_database_urls["app"], email="intruder@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        legacy_gated_router, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"])
    )

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        owner_tool = ContinuityStore(conn).create_tool(
            tool_key="browser.open",
            name="Browser Open",
            description="Open documentation pages.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["browser"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=["docs"],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )

    route_status, route_payload = invoke_request(
        "POST",
        "/v0/tools/route",
        payload={
            "user_id": str(intruder["user_id"]),
            "thread_id": str(intruder["thread_id"]),
            "tool_id": str(owner_tool["id"]),
            "action": "tool.run",
            "scope": "workspace",
            "attributes": {},
        },
    )

    assert route_status == 400
    assert route_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
