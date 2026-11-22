from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest

from alicebot_api import db


USER_SETUP_SQL = "SELECT set_config('app.current_user_id', %s, %s), set_config('jit', 'off', %s)"


class RecordingCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> tuple[int]:
        return (1,)


class TransactionContext:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> None:
        self.entered = True
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.exited = True
        return None


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()
        self.transaction_context = TransactionContext()

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def transaction(self) -> TransactionContext:
        return self.transaction_context


def test_ping_database_returns_true_when_select_succeeds(monkeypatch) -> None:
    connection = RecordingConnection()
    captured: dict[str, object] = {}

    def fake_connect(database_url: str, **kwargs: object) -> RecordingConnection:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    assert db.ping_database("postgresql://example", timeout_seconds=3) is True
    assert captured["database_url"] == "postgresql://example"
    assert captured["kwargs"] == {"connect_timeout": 3}
    assert connection.cursor_instance.executed == [("SELECT 1", None)]


def test_ping_database_returns_false_on_psycopg_error(monkeypatch) -> None:
    def fake_connect(_database_url: str, **_kwargs: object) -> RecordingConnection:
        raise psycopg.Error("boom")

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    assert db.ping_database("postgresql://example", timeout_seconds=3) is False


def test_set_current_user_sets_database_context() -> None:
    connection = RecordingConnection()
    user_id = uuid4()

    db.set_current_user(connection, user_id)

    assert connection.cursor_instance.executed == [
        (USER_SETUP_SQL, (str(user_id), True, True)),
    ]


def test_set_current_user_account_sets_database_context() -> None:
    connection = RecordingConnection()
    user_account_id = uuid4()

    db.set_current_user_account(connection, user_account_id)

    assert connection.cursor_instance.executed == [
        ("SELECT set_config('app.current_user_account_id', %s, true)", (str(user_account_id),)),
    ]


def test_direct_user_connection_sets_current_user_inside_transaction(monkeypatch) -> None:
    connection = RecordingConnection()
    user_id = uuid4()
    captured: dict[str, object] = {}
    set_current_user_calls: list[tuple[RecordingConnection, object]] = []

    def fake_connect(database_url: str, **kwargs: object) -> RecordingConnection:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return connection

    def fake_set_current_user(conn: RecordingConnection, current_user_id: object) -> None:
        set_current_user_calls.append((conn, current_user_id))

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    monkeypatch.setattr(db, "set_current_user", fake_set_current_user)

    with db.direct_user_connection("postgresql://example", user_id) as conn:
        assert conn is connection
        assert connection.transaction_context.entered is True
        assert connection.transaction_context.exited is False

    assert captured["database_url"] == "postgresql://example"
    assert captured["kwargs"] == {"row_factory": db.dict_row}
    assert set_current_user_calls == [(connection, user_id)]
    assert connection.transaction_context.exited is True


def test_pooled_user_connection_resets_tenant_context_inside_borrow_transaction(monkeypatch) -> None:
    connection = RecordingConnection()
    user_id = uuid4()
    set_current_user_calls: list[tuple[RecordingConnection, object]] = []

    class FakePool:
        def connection(self) -> RecordingConnection:
            return connection

    def fake_set_current_user(conn: RecordingConnection, current_user_id: object) -> None:
        set_current_user_calls.append((conn, current_user_id))

    monkeypatch.setattr(db, "set_current_user", fake_set_current_user)

    with db.pooled_user_connection(FakePool(), user_id) as conn:
        assert conn is connection
        assert connection.transaction_context.entered is True
        assert connection.transaction_context.exited is False

    assert set_current_user_calls == [(connection, user_id)]
    assert connection.transaction_context.exited is True


def test_user_connection_uses_configured_lazy_pool(monkeypatch) -> None:
    connection = RecordingConnection()
    user_id = uuid4()

    class FakePool:
        def connection(self) -> RecordingConnection:
            return connection

    monkeypatch.setattr(db, "_get_connection_pool", lambda _url: FakePool())

    with db.user_connection("postgresql://pooled", user_id) as conn:
        assert conn is connection

    assert connection.cursor_instance.executed == [
        (USER_SETUP_SQL, (str(user_id), True, True)),
    ]
