from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.contracts import MemoryCandidateInput
from alicebot_api.db import user_connection
from alicebot_api.memory import admit_memory_candidate
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


def seed_memory_for_review_labels(database_url: str) -> dict[str, str]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "reviewer@example.com", "Reviewer")
        thread = store.create_thread("Memory review labels thread")
        session = store.create_session(thread["id"], status="active")
        event = store.append_event(
            thread["id"],
            session["id"],
            "message.user",
            {"text": "likes oat milk in coffee"},
        )
        decision = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"likes": "oat milk"},
                source_event_ids=(event["id"],),
            ),
        )

    assert decision.memory is not None
    return {
        "user_id": str(user_id),
        "memory_id": decision.memory["id"],
    }


def seed_intruder(database_url: str) -> UUID:
    intruder_id = uuid4()
    with user_connection(database_url, intruder_id) as conn:
        ContinuityStore(conn).create_user(intruder_id, "intruder@example.com", "Intruder")
    return intruder_id


def test_memory_review_label_endpoints_create_and_list_labels_with_stable_summary_counts(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_for_review_labels(migrated_database_urls["app"])
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

    first_status, first_payload = invoke_request(
        "POST",
        f"/v0/memories/{seeded['memory_id']}/labels",
        payload={
            "user_id": seeded["user_id"],
            "label": "correct",
            "note": "Matches the latest admitted evidence.",
        },
    )
    second_status, second_payload = invoke_request(
        "POST",
        f"/v0/memories/{seeded['memory_id']}/labels",
        payload={
            "user_id": seeded["user_id"],
            "label": "outdated",
            "note": None,
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['memory_id']}/labels",
        query_params={"user_id": seeded["user_id"]},
    )

    assert first_status == 201
    assert first_payload["label"]["memory_id"] == seeded["memory_id"]
    assert first_payload["label"]["reviewer_user_id"] == seeded["user_id"]
    assert first_payload["label"]["label"] == "correct"
    assert first_payload["label"]["note"] == "Matches the latest admitted evidence."
    assert first_payload["summary"] == {
        "memory_id": seeded["memory_id"],
        "total_count": 1,
        "counts_by_label": {
            "correct": 1,
            "incorrect": 0,
            "outdated": 0,
            "insufficient_evidence": 0,
        },
        "order": ["created_at_asc", "id_asc"],
    }

    assert second_status == 201
    assert second_payload["label"]["label"] == "outdated"
    assert second_payload["label"]["note"] is None
    assert second_payload["summary"] == {
        "memory_id": seeded["memory_id"],
        "total_count": 2,
        "counts_by_label": {
            "correct": 1,
            "incorrect": 0,
            "outdated": 1,
            "insufficient_evidence": 0,
        },
        "order": ["created_at_asc", "id_asc"],
    }

    assert list_status == 200
    assert [item["id"] for item in list_payload["items"]] == [
        first_payload["label"]["id"],
        second_payload["label"]["id"],
    ]
    assert list_payload["summary"] == second_payload["summary"]


def test_memory_review_label_listing_uses_deterministic_created_at_then_id_order(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_for_review_labels(migrated_database_urls["app"])
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

    with user_connection(migrated_database_urls["app"], UUID(seeded["user_id"])) as conn:
        store = ContinuityStore(conn)
        created_labels = [
            store.create_memory_review_label(
                memory_id=UUID(seeded["memory_id"]),
                label="incorrect",
                note="Conflicts with the source event.",
            ),
            store.create_memory_review_label(
                memory_id=UUID(seeded["memory_id"]),
                label="insufficient_evidence",
                note="The evidence is too weak.",
            ),
            store.create_memory_review_label(
                memory_id=UUID(seeded["memory_id"]),
                label="outdated",
                note="Superseded by newer behavior.",
            ),
        ]

    status_code, payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['memory_id']}/labels",
        query_params={"user_id": seeded["user_id"]},
    )

    expected_ids = [
        str(label["id"])
        for label in sorted(
            created_labels,
            key=lambda label: (label["created_at"], label["id"]),
        )
    ]

    assert status_code == 200
    assert [item["id"] for item in payload["items"]] == expected_ids
    assert payload["summary"] == {
        "memory_id": seeded["memory_id"],
        "total_count": 3,
        "counts_by_label": {
            "correct": 0,
            "incorrect": 1,
            "outdated": 1,
            "insufficient_evidence": 1,
        },
        "order": ["created_at_asc", "id_asc"],
    }


def test_memory_review_label_list_returns_empty_items_and_zero_filled_summary_for_unlabeled_memory(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_for_review_labels(migrated_database_urls["app"])
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

    status_code, payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['memory_id']}/labels",
        query_params={"user_id": seeded["user_id"]},
    )

    assert status_code == 200
    assert payload == {
        "items": [],
        "summary": {
            "memory_id": seeded["memory_id"],
            "total_count": 0,
            "counts_by_label": {
                "correct": 0,
                "incorrect": 0,
                "outdated": 0,
                "insufficient_evidence": 0,
            },
            "order": ["created_at_asc", "id_asc"],
        },
    }


def test_memory_review_labels_reject_update_and_delete_at_database_level(migrated_database_urls) -> None:
    seeded = seed_memory_for_review_labels(migrated_database_urls["app"])

    with user_connection(migrated_database_urls["app"], UUID(seeded["user_id"])) as conn:
        label = ContinuityStore(conn).create_memory_review_label(
            memory_id=UUID(seeded["memory_id"]),
            label="correct",
            note="Initial review label.",
        )

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory_review_labels SET label = 'incorrect' WHERE id = %s",
                    (label["id"],),
                )

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_review_labels WHERE id = %s",
                    (label["id"],),
                )


def test_memory_review_label_endpoints_enforce_per_user_isolation_and_not_found_behavior(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_memory_for_review_labels(migrated_database_urls["app"])
    intruder_id = seed_intruder(migrated_database_urls["app"])
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

    create_status, create_payload = invoke_request(
        "POST",
        f"/v0/memories/{seeded['memory_id']}/labels",
        payload={
            "user_id": str(intruder_id),
            "label": "incorrect",
            "note": "Should not be able to label another user's memory.",
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        f"/v0/memories/{seeded['memory_id']}/labels",
        query_params={"user_id": str(intruder_id)},
    )

    assert create_status == 404
    assert create_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert list_status == 404
    assert list_payload == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
