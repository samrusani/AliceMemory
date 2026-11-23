from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

import scripts.seed_local_user as seed_local_user


USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class RecordingCursor(AbstractContextManager["RecordingCursor"]):
    def __init__(self, connection: "RecordingConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        if not self.connection.in_transaction:
            raise AssertionError("seed SQL executed outside the explicit transaction")
        self.connection.executions.append((" ".join(query.split()), params))


class RecordingTransaction(AbstractContextManager["RecordingTransaction"]):
    def __init__(self, connection: "RecordingConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "RecordingTransaction":
        assert self.connection.in_transaction is False
        self.connection.in_transaction = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.connection.in_transaction = False


class RecordingConnection(AbstractContextManager["RecordingConnection"]):
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.in_transaction = False

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        assert self.in_transaction is False

    def transaction(self) -> RecordingTransaction:
        return RecordingTransaction(self)

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)


def test_seed_local_user_sets_rls_context_and_upserts_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = RecordingConnection()
    captured_urls: list[str] = []

    def fake_connect(database_url: str) -> RecordingConnection:
        captured_urls.append(database_url)
        return connection

    monkeypatch.setattr(seed_local_user.psycopg, "connect", fake_connect)

    seed_local_user.seed_local_user(
        database_admin_url="postgresql://migration-role.example/alicebot",
        user_id=USER_ID,
    )

    assert captured_urls == ["postgresql://migration-role.example/alicebot"]
    assert connection.executions == [
        (
            "SELECT set_config('app.current_user_id', %s, %s), set_config('jit', 'off', %s)",
            (str(USER_ID), True, True),
        ),
        (
            (
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, "
                "display_name = EXCLUDED.display_name"
            ),
            (
                USER_ID,
                f"local-alpha-{USER_ID}@alicebot.local",
                "Local Alpha User",
            ),
        ),
    ]


def test_main_requires_the_admin_url_without_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_seed_local_user(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(seed_local_user, "seed_local_user", fake_seed_local_user)

    with pytest.raises(SystemExit, match="DATABASE_ADMIN_URL is required"):
        seed_local_user.main(
            {
                "ALICEBOT_AUTH_USER_ID": str(USER_ID),
                "DATABASE_URL": "postgresql://runtime-role.example/alicebot",
            }
        )

    assert called is False


def test_main_validates_the_configured_user_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_seed_local_user(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(seed_local_user, "seed_local_user", fake_seed_local_user)

    with pytest.raises(SystemExit, match="ALICEBOT_AUTH_USER_ID must be a valid UUID"):
        seed_local_user.main(
            {
                "ALICEBOT_AUTH_USER_ID": "not-a-uuid",
                "DATABASE_ADMIN_URL": "postgresql://migration-role.example/alicebot",
            }
        )

    assert called is False


def test_main_emits_only_a_safe_success_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_admin_url = "postgresql://alicebot_admin:super-secret@db.example/alicebot"
    calls: list[tuple[str, UUID]] = []

    def fake_seed_local_user(*, database_admin_url: str, user_id: UUID) -> None:
        calls.append((database_admin_url, user_id))

    monkeypatch.setattr(seed_local_user, "seed_local_user", fake_seed_local_user)

    assert (
        seed_local_user.main(
            {
                "ALICEBOT_AUTH_USER_ID": str(USER_ID),
                "DATABASE_ADMIN_URL": database_admin_url,
            }
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "local_user_seed=ready\n"
    assert captured.err == ""
    assert database_admin_url not in captured.out
    assert calls == [(database_admin_url, USER_ID)]


def test_main_does_not_echo_the_admin_url_on_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_admin_url = "postgresql://alicebot_admin:super-secret@db.example/alicebot"

    def fake_seed_local_user(**_kwargs: Any) -> None:
        raise seed_local_user.psycopg.OperationalError(
            f"connection failed for {database_admin_url}"
        )

    monkeypatch.setattr(seed_local_user, "seed_local_user", fake_seed_local_user)

    with pytest.raises(SystemExit) as exc_info:
        seed_local_user.main(
            {
                "ALICEBOT_AUTH_USER_ID": str(USER_ID),
                "DATABASE_ADMIN_URL": database_admin_url,
            }
        )

    assert str(exc_info.value) == "local user seed failed (database_error)"
    assert database_admin_url not in str(exc_info.value)
