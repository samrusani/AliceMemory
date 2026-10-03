from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_projects as vnext_projects_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key
from alicebot_api.vnext_store import PostgresVNextStore


def _invoke_get(
    path: str,
    *,
    user_id: UUID,
    authorization: str | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": urlencode({"user_id": str(user_id)}).encode("ascii"),
        "headers": headers,
        # An in-process caller is a loopback peer. Anything else is refused by
        # the keyless loopback gate before key resolution runs, which would
        # make the per-user assertions below pass for the wrong reason.
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body)


def test_agent_key_activation_and_visibility_are_isolated_per_user(
    migrated_database_urls: dict[str, str],
    monkeypatch,
    caplog,
) -> None:
    user_a = uuid4()
    user_b = uuid4()
    with user_connection(migrated_database_urls["app"], user_a) as conn:
        ContinuityStore(conn).create_user(
            user_a,
            f"stage-a-key-owner-{user_a.hex[:12]}@example.invalid",
            "Stage A key owner",
        )
        key_a_record, raw_key_a = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_a,
            agent_id="stage-a-owner-agent",
            permission_profile="trusted_local_agent",
        )
    with user_connection(migrated_database_urls["app"], user_b) as conn:
        ContinuityStore(conn).create_user(
            user_b,
            f"stage-a-keyless-{user_b.hex[:12]}@example.invalid",
            "Stage A keyless user",
        )

    settings = Settings(app_env="test", database_url=migrated_database_urls["app"])
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_projects_router, "get_settings", lambda: settings)

    owner_keyless_status, owner_keyless_payload = _invoke_get(
        "/v0/vnext/projects",
        user_id=user_a,
    )
    keyless_status, keyless_payload = _invoke_get(
        "/v0/vnext/projects",
        user_id=user_b,
    )
    with caplog.at_level("ERROR", logger="alicebot_api.public_errors"):
        foreign_status, foreign_payload = _invoke_get(
            "/v0/vnext/projects",
            user_id=user_b,
            authorization=f"Bearer {raw_key_a}",
        )

    assert owner_keyless_status == 401
    assert owner_keyless_payload == {
        "detail": {"code": "authentication_failed", "message": "Authentication failed"}
    }
    assert keyless_status == 200
    assert keyless_payload["items"] == []
    assert foreign_status == 401
    assert foreign_payload == {
        "detail": {"code": "authentication_failed", "message": "Authentication failed"}
    }

    with user_connection(migrated_database_urls["app"], user_a) as conn:
        owner_store = PostgresVNextStore(conn)
        assert owner_store.count_active_agent_api_keys() == 1
        assert [str(row["id"]) for row in owner_store.list_agent_api_keys()] == [
            str(key_a_record["id"])
        ]
        assert owner_store.get_agent_api_key_by_hash(hash_agent_key(raw_key_a)) is not None

    with user_connection(migrated_database_urls["app"], user_b) as conn:
        other_store = PostgresVNextStore(conn)
        assert other_store.count_active_agent_api_keys() == 0
        assert other_store.list_agent_api_keys() == []
        assert other_store.get_agent_api_key_by_hash(hash_agent_key(raw_key_a)) is None

    assert raw_key_a not in json.dumps(
        [owner_keyless_payload, keyless_payload, foreign_payload],
        sort_keys=True,
    )
    assert raw_key_a not in caplog.text
