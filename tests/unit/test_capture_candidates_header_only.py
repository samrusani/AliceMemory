"""Header-only capture candidates through the full app.

The Hermes plugin sends user_id only in X-AliceBot-User-Id. The middleware
writes that id into the cached JSON body, and the candidates route validates
it. With legacy /v0 disabled, the same POST returns 404 before that rewrite.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import anyio
import pytest

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
    """The candidates handler opened the store connection."""


def _candidate_body(*, user_id: str | None) -> bytes:
    payload: dict[str, object] = {
        "user_content": "Remember the harbour clipboard.",
        "assistant_content": "Noted.",
        "source_kind": "sync_turn",
    }
    if user_id is not None:
        payload["user_id"] = user_id
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if user_id is None:
        assert b"user_id" not in body
    return body


def _post_candidates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings,
    body: bytes,
    header_user_id: str,
) -> tuple[bool, object, int, dict[str, object]]:
    reached = False
    seen_user: object = None

    def block_database(*args: object, **kwargs: object) -> None:
        nonlocal reached, seen_user
        reached = True
        if len(args) >= 2:
            seen_user = args[1]
        else:
            seen_user = kwargs.get("user_id")
        raise _CaptureHandlerReached("capture candidates handler ran")

    monkeypatch.setattr(continuity_router, "user_connection", block_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    status = -1
    payload: dict[str, object] = {}
    try:
        status, payload = _invoke(
            body,
            {
                "content-type": "application/json",
                "X-AliceBot-User-Id": header_user_id,
            },
        )
    except _CaptureHandlerReached:
        pass
    return reached, seen_user, status, payload


@pytest.mark.parametrize(
    ("app_env", "legacy_enabled"),
    (
        ("development", False),
        ("production", True),
    ),
)
@pytest.mark.parametrize("body_has_user_id", (False, True))
def test_header_only_capture_candidates_post_reaches_the_store(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    legacy_enabled: bool,
    body_has_user_id: bool,
) -> None:
    """Header-only clients reach the store when legacy /v0 is enabled.

    This flips the characterization that expected 422. Development enables
    /v0. Production enables it only with the legacy flag. The body is
    otherwise valid. Mutation: delete the ``request._body`` assignment in
    ``_rewrite_user_id_json_body``. The header-only case then stays at 422
    and ``reached`` is false. An exception from the stub is not the kill.
    """

    header_user_id = str(uuid4())
    body = _candidate_body(user_id=header_user_id if body_has_user_id else None)
    reached, seen_user, status, payload = _post_candidates(
        monkeypatch,
        settings=Settings(
            app_env=app_env,
            auth_user_id="",
            legacy_v0_enabled_outside_dev=legacy_enabled,
            database_url="postgresql://db.example/alice",
        ),
        body=body,
        header_user_id=header_user_id,
    )

    assert reached is True
    assert seen_user == UUID(header_user_id)
    assert status != 422
    assert "missing" not in json.dumps(payload.get("detail", ""))


def test_header_only_capture_candidates_post_stays_404_when_legacy_v0_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy /v0 disabled returns 404 before the body rewrite.

    The handler does not open the store. Mutation: skip the legacy-disabled
    response in ``enforce_authenticated_user_identity``. This test then sees
    the handler run.
    """

    header_user_id = str(uuid4())
    reached, _seen_user, status, payload = _post_candidates(
        monkeypatch,
        settings=Settings(
            app_env="production",
            auth_user_id="",
            legacy_v0_enabled_outside_dev=False,
            database_url="postgresql://db.example/alice",
        ),
        body=_candidate_body(user_id=None),
        header_user_id=header_user_id,
    )

    assert reached is False
    assert status == 404
    assert payload["detail"] == "legacy v0 API is disabled outside development and test"
