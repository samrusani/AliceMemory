from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
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
    request_headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        request_headers.append((key.lower().encode(), value.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": request_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def identity_header(user_id: UUID | str) -> dict[str, str]:
    return {"x-alicebot-user-id": str(user_id)}


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def test_public_eval_api_runs_lists_and_reads_persisted_report(
    migrated_database_urls,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    user_id = seed_user(
        migrated_database_urls["app"],
        email="public-evals@example.com",
    )

    suites_status, suites_payload = invoke_request(
        "GET",
        "/v1/evals/suites",
        headers=identity_header(user_id),
    )
    assert suites_status == 200
    assert suites_payload["summary"]["suite_count"] == 5
    assert suites_payload["summary"]["case_count"] == 12

    run_status, run_payload = invoke_request(
        "POST",
        "/v1/evals/runs",
        headers=identity_header(user_id),
    )
    assert run_status == 200
    assert run_payload["run"]["status"] == "pass"
    assert run_payload["report"]["summary"]["suite_count"] == 5
    assert run_payload["report"]["summary"]["case_count"] == 12
    eval_run_id = run_payload["run"]["id"]

    runs_status, runs_payload = invoke_request(
        "GET",
        "/v1/evals/runs",
        query_params={"limit": "10"},
        headers=identity_header(user_id),
    )
    assert runs_status == 200
    assert runs_payload["summary"]["returned_count"] == 1
    assert runs_payload["items"][0]["id"] == eval_run_id

    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v1/evals/runs/{eval_run_id}",
        headers=identity_header(user_id),
    )
    assert detail_status == 200
    assert detail_payload["run"]["report_digest"] == run_payload["run"]["report_digest"]
    assert detail_payload["report"] == run_payload["report"]
    assert len(detail_payload["results"]) == 12


def test_public_eval_api_rejects_unknown_suite_key(
    migrated_database_urls,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    user_id = seed_user(
        migrated_database_urls["app"],
        email="public-evals-invalid-suite@example.com",
    )

    status, payload = invoke_request(
        "POST",
        "/v1/evals/runs",
        query_params={"suite_key": "missing_suite"},
        headers=identity_header(user_id),
    )

    assert status == 400
    assert payload["detail"] == {"code": "invalid_request", "message": "The request is invalid"}


def test_public_eval_api_requires_valid_local_identity(
    migrated_database_urls,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    status, payload = invoke_request(
        "GET",
        "/v1/evals/suites",
    )

    assert status == 400
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    invalid_status, invalid_payload = invoke_request(
        "GET",
        "/v1/evals/suites",
        headers=identity_header("not-a-uuid"),
    )
    assert invalid_status == 400
    assert invalid_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
