from __future__ import annotations

import atexit
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
import threading
from typing import ContextManager, Protocol, TypeAlias, cast
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import Row, dict_row

PING_DATABASE_SQL = "SELECT 1"
SET_CURRENT_USER_SQL = "SELECT set_config('app.current_user_id', %s, true)"
SET_CURRENT_USER_ACCOUNT_SQL = "SELECT set_config('app.current_user_account_id', %s, true)"
ConnectionRow = dict[str, object]
UserConnection: TypeAlias = psycopg.Connection[ConnectionRow]
DEFAULT_POOL_MAX_SIZE = 10
DEFAULT_POOL_TIMEOUT_SECONDS = 10.0
MAX_POOL_REGISTRY_SIZE = 4
_pool_lock = threading.Lock()
_connection_pools: OrderedDict[str, ConnectionPool[UserConnection]] = OrderedDict()


class ConnectionPoolLike(Protocol):
    """Minimal seam implemented by ``psycopg_pool.ConnectionPool``.

    AliceBot does not require the optional psycopg-pool distribution, but a
    hosted runtime that installs it can use the same tenant/transaction setup
    via ``pooled_user_connection`` instead of forking database context logic.
    """

    def connection(self) -> ContextManager[UserConnection]: ...


def ping_database(database_url: str, timeout_seconds: int) -> bool:
    try:
        with psycopg.connect(database_url, connect_timeout=timeout_seconds) as conn:
            with conn.cursor() as cur:
                cur.execute(PING_DATABASE_SQL)
                cur.fetchone()
        return True
    except psycopg.Error:
        return False


def _set_connection_context(conn: psycopg.Connection[Row], sql: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, (value,))


def set_current_user(conn: psycopg.Connection[Row], user_id: UUID) -> None:
    _set_connection_context(conn, SET_CURRENT_USER_SQL, str(user_id))


def set_current_user_account(conn: psycopg.Connection[Row], user_account_id: UUID) -> None:
    _set_connection_context(conn, SET_CURRENT_USER_ACCOUNT_SQL, str(user_account_id))


@contextmanager
def direct_user_connection(database_url: str, user_id: UUID) -> Iterator[UserConnection]:
    """Unpooled path retained for health checks, migrations, and test tools."""
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            set_current_user(conn, user_id)
            yield conn


@contextmanager
def user_read_snapshot_connection(database_url: str, user_id: UUID) -> Iterator[UserConnection]:
    """One repeatable read, read-only snapshot with transaction-local RLS.

    Set the transaction mode before the first query, including the identity
    query, and clear that identity when the transaction ends.
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            set_current_user(conn, user_id)
            yield conn


def _new_connection_pool(database_url: str) -> ConnectionPool[UserConnection]:
    pool = cast(
        ConnectionPool[UserConnection],
        ConnectionPool(
            conninfo=database_url,
            min_size=0,
            max_size=DEFAULT_POOL_MAX_SIZE,
            timeout=DEFAULT_POOL_TIMEOUT_SECONDS,
            kwargs={"row_factory": dict_row},
            open=False,
        ),
    )
    pool.open()
    return pool


def _get_connection_pool(database_url: str) -> ConnectionPool[UserConnection]:
    """Return a bounded lazy pool, evicting stale test/tenant URLs by LRU."""
    evicted: ConnectionPool[UserConnection] | None = None
    with _pool_lock:
        pool = _connection_pools.get(database_url)
        if pool is not None:
            _connection_pools.move_to_end(database_url)
            return pool
        pool = _new_connection_pool(database_url)
        _connection_pools[database_url] = pool
        if len(_connection_pools) > MAX_POOL_REGISTRY_SIZE:
            _evicted_url, evicted = _connection_pools.popitem(last=False)
    if evicted is not None:
        evicted.close()
    return pool


def close_connection_pools() -> None:
    """Close every lazy pool; safe for app shutdown hooks and test cleanup."""
    with _pool_lock:
        pools = list(_connection_pools.values())
        _connection_pools.clear()
    for pool in pools:
        pool.close()


@contextmanager
def user_connection(database_url: str, user_id: UUID) -> Iterator[UserConnection]:
    """Borrow a normal application connection from the bounded lazy pool."""
    with pooled_user_connection(_get_connection_pool(database_url), user_id) as conn:
        yield conn


@contextmanager
def pooled_user_connection(pool: ConnectionPoolLike, user_id: UUID) -> Iterator[UserConnection]:
    """Borrow a pooled connection with the same transaction-scoped RLS context.

    Keeping ``set_current_user`` inside ``conn.transaction()`` is essential:
    ``set_config(..., true)`` is LOCAL and is cleared before the connection is
    returned to the pool, so tenant identity cannot leak to its next borrower.
    """
    with pool.connection() as conn:
        with conn.transaction():
            set_current_user(conn, user_id)
            yield conn


__all__ = [
    "ConnectionPoolLike",
    "DEFAULT_POOL_MAX_SIZE",
    "MAX_POOL_REGISTRY_SIZE",
    "UserConnection",
    "close_connection_pools",
    "direct_user_connection",
    "ping_database",
    "pooled_user_connection",
    "set_current_user",
    "set_current_user_account",
    "user_connection",
    "user_read_snapshot_connection",
]


atexit.register(close_connection_pools)
