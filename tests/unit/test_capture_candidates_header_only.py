"""Header-only capture candidates through the full app.

Characterization of a known defect: header-only clients get 422. The Hermes
plugin sends user_id only in X-AliceBot-User-Id. The middleware rewrites a
new Request, and the route still validates the original body.
"""

from __future__ import annotations

import json
from uuid import uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.routers import continuity as continuity_router


def _invoke(body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v0/continuity/captures/candidates",
        "raw_path": b"/v0/continuity/captures/candidates",
        "query_string": b"",
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    status = start["status"]
    assert isinstance(status, int)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return status, parsed


class _CaptureHandlerReached(Exception):
    """The candidates handler ran. This pin expects it not to."""


def test_header_only_capture_candidates_post_returns_422_known_defect(monkeypatch) -> None:
    """Characterization of a known defect: header-only clients get 422. The header
    rewrite fix must flip this test on purpose.

    The body is otherwise valid. The route reports body.user_id missing, and
    the handler does not run. Mutation: make the rewritten body the body the
    route validates (the browser-clip path already sets request._body). The
    stub sets the flag and raises. This test fails on ``reached``.
    """

    reached = False

    def block_database(*_args: object, **_kwargs: object) -> None:
        nonlocal reached
        reached = True
        raise _CaptureHandlerReached("capture candidates handler ran")

    monkeypatch.setattr(continuity_router, "user_connection", block_database)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(app_env="development", auth_user_id=""),
    )
    body = json.dumps(
        {
            "user_content": "Remember the harbour clipboard.",
            "assistant_content": "Noted.",
            "source_kind": "sync_turn",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    assert b"user_id" not in body

    status = -1
    payload: dict[str, object] = {}
    try:
        status, payload = _invoke(
            body,
            {
                "content-type": "application/json",
                "X-AliceBot-User-Id": str(uuid4()),
            },
        )
    except _CaptureHandlerReached:
        pass

    assert reached is False
    assert status == 422
    detail = payload["detail"]
    assert isinstance(detail, list) and detail
    first = detail[0]
    assert isinstance(first, dict)
    assert first["loc"] == ["body", "user_id"]
    assert first["type"] == "missing"
