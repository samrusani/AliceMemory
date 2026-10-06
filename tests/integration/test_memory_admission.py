from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore


def invoke_admit_memory(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v0/memories/admit",
        "raw_path": b"/v0/memories/admit",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_memory_evidence(database_url: str) -> tuple[UUID, list[UUID]]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "owner@example.com", "Owner")
        thread = store.create_thread("Memory thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            store.append_event(thread["id"], session["id"], "message.user", {"text": "likes black coffee"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "actually likes oat milk"})["id"],
            store.append_event(thread["id"], session["id"], "message.user", {"text": "stop remembering coffee"})["id"],
        ]

    return user_id, event_ids


def test_admit_memory_endpoint_returns_noop_and_persists_nothing_without_value(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id, event_ids = seed_memory_evidence(migrated_database_urls["app"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    status_code, payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": None,
            "source_event_ids": [str(event_ids[0])],
        }
    )

    assert status_code == 200
    assert payload == {
        "decision": "NOOP",
        "reason": "candidate_value_missing",
        "memory": None,
        "revision": None,
    }

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        assert store.list_memories() == []


def test_admit_memory_endpoint_rejects_unknown_source_events(migrated_database_urls, monkeypatch) -> None:
    user_id = uuid4()

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "owner@example.com", "Owner")

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    status_code, payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(uuid4())],
        }
    )

    assert status_code == 400
    assert payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}


def test_admit_memory_endpoint_rejects_invalid_memory_type(migrated_database_urls, monkeypatch) -> None:
    user_id, event_ids = seed_memory_evidence(migrated_database_urls["app"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    status_code, payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(event_ids[0])],
            "memory_type": "unknown_type",
        }
    )

    assert status_code == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_admit_memory_endpoint_persists_add_update_and_delete_revisions(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id, event_ids = seed_memory_evidence(migrated_database_urls["app"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    add_status, add_payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(event_ids[0])],
        }
    )
    update_status, update_payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "source_event_ids": [str(event_ids[1])],
        }
    )
    delete_status, delete_payload = invoke_admit_memory(
        {
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": None,
            "source_event_ids": [str(event_ids[2])],
            "delete_requested": True,
        }
    )

    assert add_status == 200
    assert add_payload["decision"] == "ADD"
    assert update_status == 200
    assert update_payload["decision"] == "UPDATE"
    assert delete_status == 200
    assert delete_payload["decision"] == "DELETE"

    memory_id = UUID(delete_payload["memory"]["id"])
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        memories = store.list_memories()
        revisions = store.list_memory_revisions(memory_id)

    assert len(memories) == 1
    assert memories[0]["id"] == memory_id
    assert memories[0]["status"] == "deleted"
    assert memories[0]["memory_type"] == "preference"
    assert memories[0]["confirmation_status"] == "unconfirmed"
    assert memories[0]["confidence"] is None
    assert memories[0]["salience"] is None
    assert memories[0]["valid_from"] is None
    assert memories[0]["valid_to"] is None
    assert memories[0]["last_confirmed_at"] is None
    assert memories[0]["source_event_ids"] == [str(event_ids[2])]
    assert [revision["sequence_no"] for revision in revisions] == [1, 2, 3]
    assert [revision["action"] for revision in revisions] == ["ADD", "UPDATE", "DELETE"]
    assert revisions[0]["new_value"] == {"likes": "black"}
    assert revisions[1]["previous_value"] == {"likes": "black"}
    assert revisions[1]["new_value"] == {"likes": "oat milk"}
    assert revisions[2]["previous_value"] == {"likes": "oat milk"}
    assert revisions[2]["new_value"] is None

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error, match="append-only"):
                cur.execute(
                    "UPDATE memory_revisions SET action = 'MUTATED' WHERE memory_id = %s",
                    (memory_id,),
                )


def test_memories_and_memory_revisions_respect_per_user_isolation(
    migrated_database_urls,
    monkeypatch,
) -> None:
    owner_id, event_ids = seed_memory_evidence(migrated_database_urls["app"])
    intruder_id = uuid4()
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    status_code, payload = invoke_admit_memory(
        {
            "user_id": str(owner_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "black"},
            "source_event_ids": [str(event_ids[0])],
        }
    )

    assert status_code == 200
    memory_id = UUID(payload["memory"]["id"])

    with user_connection(migrated_database_urls["app"], intruder_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(intruder_id, "intruder@example.com", "Intruder")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM memories WHERE id = %s", (memory_id,))
            memory_count = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) AS count FROM memory_revisions WHERE memory_id = %s",
                (memory_id,),
            )
            revision_count = cur.fetchone()
            cur.execute(
                "UPDATE memories SET status = 'deleted' WHERE id = %s RETURNING id",
                (memory_id,),
            )
            updated_rows = cur.fetchall()

        assert memory_count["count"] == 0
        assert revision_count["count"] == 0
        assert updated_rows == []
        assert store.list_memories() == []
        assert store.list_memory_revisions(memory_id) == []
