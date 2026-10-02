from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
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


def seed_user(database_url: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Magnesium reorder thread")

    return {
        "user_id": user_id,
        "thread_id": thread["id"],
    }


def create_magnesium_tool_and_policy(database_url: str, *, user_id: UUID) -> UUID:
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        tool = store.create_tool(
            tool_key="proxy.echo",
            name="Merchant Proxy",
            description="Deterministic proxy tool",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["proxy", "commerce"],
            action_hints=["place_order"],
            scope_hints=["supplements"],
            domain_hints=["ecommerce"],
            risk_hints=["purchase"],
            metadata={"transport": "proxy"},
        )
        store.create_policy(
            name="Require approval for supplement orders",
            action="place_order",
            scope="supplements",
            effect="require_approval",
            priority=10,
            active=True,
            conditions={"tool_key": "proxy.echo"},
            required_consents=[],
        )

    return tool["id"]


def test_mvp_magnesium_reorder_flow_proves_ship_gate_evidence(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner = seed_user(migrated_database_urls["app"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    tool_id = create_magnesium_tool_and_policy(
        migrated_database_urls["app"],
        user_id=owner["user_id"],
    )

    create_status, create_payload = invoke_request(
        "POST",
        "/v0/approvals/requests",
        payload={
            "user_id": str(owner["user_id"]),
            "thread_id": str(owner["thread_id"]),
            "tool_id": str(tool_id),
            "action": "place_order",
            "scope": "supplements",
            "domain_hint": "ecommerce",
            "risk_hint": "purchase",
            "attributes": {
                "merchant": "Thorne",
                "item": "Magnesium Bisglycinate",
                "quantity": "1",
                "package": "90 capsules",
            },
        },
    )
    assert create_status == 200
    assert create_payload["approval"]["status"] == "pending"

    approve_status, approve_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/approve",
        payload={"user_id": str(owner["user_id"])},
    )
    assert approve_status == 200
    assert approve_payload["approval"]["status"] == "approved"

    execute_status, execute_payload = invoke_request(
        "POST",
        f"/v0/approvals/{create_payload['approval']['id']}/execute",
        payload={"user_id": str(owner["user_id"])},
    )
    assert execute_status == 200
    assert execute_payload["approval"]["id"] == create_payload["approval"]["id"]
    assert execute_payload["result"]["status"] == "completed"
    assert execute_payload["events"] is not None

    result_event_id = execute_payload["events"]["result_event_id"]
    request_event_id = execute_payload["events"]["request_event_id"]
    assert isinstance(result_event_id, str)
    assert isinstance(request_event_id, str)

    add_status, add_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_key": "user.preference.supplement.magnesium_reorder",
            "value": {
                "merchant": "Thorne",
                "item": "Magnesium Bisglycinate",
                "quantity": "1",
                "package": "90 capsules",
            },
            "source_event_ids": [result_event_id, request_event_id],
            "delete_requested": False,
        },
    )
    assert add_status == 200
    assert add_payload["decision"] == "ADD"
    assert add_payload["memory"] is not None
    assert add_payload["revision"] is not None

    update_status, update_payload = invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={
            "user_id": str(owner["user_id"]),
            "memory_key": "user.preference.supplement.magnesium_reorder",
            "value": {
                "merchant": "Thorne",
                "item": "Magnesium Bisglycinate",
                "quantity": "2",
                "package": "90 capsules",
            },
            "source_event_ids": [result_event_id],
            "delete_requested": False,
        },
    )
    assert update_status == 200
    assert update_payload["decision"] == "UPDATE"

    memory_id = UUID(update_payload["memory"]["id"])

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/memories",
        query_params={"user_id": str(owner["user_id"]), "status": "all", "limit": "10"},
    )
    revisions_status, revisions_payload = invoke_request(
        "GET",
        f"/v0/memories/{memory_id}/revisions",
        query_params={"user_id": str(owner["user_id"]), "limit": "10"},
    )

    assert list_status == 200
    assert revisions_status == 200
    assert list_payload["items"][0]["id"] == str(memory_id)
    assert list_payload["items"][0]["memory_key"] == "user.preference.supplement.magnesium_reorder"
    assert list_payload["items"][0]["value"]["quantity"] == "2"
    assert list_payload["items"][0]["source_event_ids"] == [result_event_id]

    assert [revision["action"] for revision in revisions_payload["items"]] == ["ADD", "UPDATE"]
    assert revisions_payload["items"][0]["source_event_ids"] == [result_event_id, request_event_id]
    assert revisions_payload["items"][1]["source_event_ids"] == [result_event_id]
    assert revisions_payload["items"][0]["new_value"]["quantity"] == "1"
    assert revisions_payload["items"][1]["new_value"]["quantity"] == "2"

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        memories = store.list_memories()
        stored_revisions = store.list_memory_revisions(memory_id)

    assert len(memories) == 1
    assert memories[0]["memory_key"] == "user.preference.supplement.magnesium_reorder"
    assert memories[0]["source_event_ids"] == [result_event_id]
    assert [revision["action"] for revision in stored_revisions] == ["ADD", "UPDATE"]
