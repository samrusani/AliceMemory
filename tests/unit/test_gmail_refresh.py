from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from uuid import uuid4

import pytest

from alicebot_api.gmail import (
    GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS,
    GMAIL_TOKEN_REFRESH_URL,
    GmailCredentialInvalidError,
    GmailCredentialRefreshError,
    RefreshedGmailCredential,
    refresh_gmail_access_token,
)


class _FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _make_http_error(status_code: int) -> HTTPError:
    return HTTPError(
        GMAIL_TOKEN_REFRESH_URL,
        status_code,
        "upstream error",
        hdrs=None,
        fp=BytesIO(b'{"error":"invalid_grant"}'),
    )


def test_refresh_gmail_access_token_posts_expected_payload_and_returns_expiry(monkeypatch) -> None:
    gmail_account_id = uuid4()
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout: int, enforce_public_peer):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["content_type"] = request.headers["Content-type"]
        seen["accept"] = request.headers["Accept"]
        seen["body"] = parse_qs(request.data.decode("utf-8"))
        return _FakeHTTPResponse(
            json.dumps({"access_token": "token-refreshed", "expires_in": 3600}).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.gmail.open_provider_url", fake_urlopen)

    started_at = datetime.now(UTC)
    refreshed_credential = refresh_gmail_access_token(
        gmail_account_id=gmail_account_id,
        refresh_token="refresh-001",
        client_id="client-001",
        client_secret="secret-001",
    )
    finished_at = datetime.now(UTC)

    assert refreshed_credential == RefreshedGmailCredential(
        access_token="token-refreshed",
        access_token_expires_at=refreshed_credential.access_token_expires_at,
        refresh_token=None,
    )
    assert started_at + timedelta(seconds=3590) <= refreshed_credential.access_token_expires_at <= (
        finished_at + timedelta(seconds=3610)
    )
    assert seen == {
        "url": GMAIL_TOKEN_REFRESH_URL,
        "timeout": GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS,
        "content_type": "application/x-www-form-urlencoded",
        "accept": "application/json",
        "body": {
            "client_id": ["client-001"],
            "client_secret": ["secret-001"],
            "refresh_token": ["refresh-001"],
            "grant_type": ["refresh_token"],
        },
    }


def test_refresh_gmail_access_token_returns_rotated_refresh_token_when_provider_supplies_one(
    monkeypatch,
) -> None:
    gmail_account_id = uuid4()

    def fake_urlopen(_request, timeout: int, enforce_public_peer):
        assert timeout == GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS
        return _FakeHTTPResponse(
            json.dumps(
                {
                    "access_token": "token-refreshed",
                    "expires_in": 3600,
                    "refresh_token": "refresh-rotated",
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.gmail.open_provider_url", fake_urlopen)

    refreshed_credential = refresh_gmail_access_token(
        gmail_account_id=gmail_account_id,
        refresh_token="refresh-001",
        client_id="client-001",
        client_secret="secret-001",
    )

    assert refreshed_credential.refresh_token == "refresh-rotated"


@pytest.mark.parametrize("status_code", [400, 401])
def test_refresh_gmail_access_token_maps_invalid_refresh_rejections_to_invalid_error(
    monkeypatch,
    status_code: int,
) -> None:
    gmail_account_id = uuid4()

    def fake_urlopen(_request, timeout: int, enforce_public_peer):
        assert timeout == GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS
        raise _make_http_error(status_code)

    monkeypatch.setattr("alicebot_api.gmail.open_provider_url", fake_urlopen)

    with pytest.raises(
        GmailCredentialInvalidError,
        match=f"gmail account {gmail_account_id} refresh credentials were rejected",
    ):
        refresh_gmail_access_token(
            gmail_account_id=gmail_account_id,
            refresh_token="refresh-001",
            client_id="client-001",
            client_secret="secret-001",
        )


def test_refresh_gmail_access_token_maps_non_deterministic_http_failure_to_refresh_error(
    monkeypatch,
) -> None:
    gmail_account_id = uuid4()

    def fake_urlopen(_request, timeout: int, enforce_public_peer):
        assert timeout == GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS
        raise _make_http_error(500)

    monkeypatch.setattr("alicebot_api.gmail.open_provider_url", fake_urlopen)

    with pytest.raises(
        GmailCredentialRefreshError,
        match=f"gmail account {gmail_account_id} access token could not be renewed",
    ):
        refresh_gmail_access_token(
            gmail_account_id=gmail_account_id,
            refresh_token="refresh-001",
            client_id="client-001",
            client_secret="secret-001",
        )


@pytest.mark.parametrize(
    ("response_payload", "error"),
    [
        (b"not-json", None),
        (json.dumps({"expires_in": 3600}).encode("utf-8"), None),
        (None, URLError("network down")),
    ],
)
def test_refresh_gmail_access_token_maps_malformed_or_transport_failures_to_refresh_error(
    monkeypatch,
    response_payload: bytes | None,
    error: Exception | None,
) -> None:
    gmail_account_id = uuid4()

    def fake_urlopen(_request, timeout: int, enforce_public_peer):
        assert timeout == GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS
        if error is not None:
            raise error
        assert response_payload is not None
        return _FakeHTTPResponse(response_payload)

    monkeypatch.setattr("alicebot_api.gmail.open_provider_url", fake_urlopen)

    with pytest.raises(
        GmailCredentialRefreshError,
        match=f"gmail account {gmail_account_id} access token could not be renewed",
    ):
        refresh_gmail_access_token(
            gmail_account_id=gmail_account_id,
            refresh_token="refresh-001",
            client_id="client-001",
            client_secret="secret-001",
        )
