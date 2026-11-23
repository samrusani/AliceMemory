"""Transaction and dedicated-session JIT settings on real PostgreSQL."""

from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import pytest

from alicebot_api import db, vnext_key_rejection_events
from alicebot_api.local_workspace import ensure_local_workspace, get_local_workspace
from alicebot_api.vnext_evals import _ephemeral_eval_store
from alicebot_api.vnext_scheduler_runtime import _autocommit_user_store
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def _jit(conn):
    return conn.execute("SELECT current_setting('jit') AS jit").fetchone()["jit"]


@pytest.fixture
def jit_on_url(label_harness):
    # Exercise restoration and catch a missing setup even on servers with
    # JIT off by default or without an LLVM provider.
    return make_conninfo(label_harness.urls["app"], options="-c jit=on")


@pytest.mark.parametrize("helper", [db.user_connection, db.direct_user_connection, db.user_read_snapshot_connection])
def test_user_connection_helpers_disable_jit(label_harness, jit_on_url, helper):
    """Guard: enable JIT in the common tenant setup."""
    with psycopg.connect(jit_on_url, autocommit=True, row_factory=dict_row) as plain:
        assert _jit(plain) == "on"
    with helper(jit_on_url, label_harness.user_id) as conn:
        assert _jit(conn) == "off"


def test_pooled_connection_restores_jit_after_commit_and_rollback(label_harness, jit_on_url):
    """Guard: give pooled JIT session lifetime instead of transaction lifetime."""
    with ConnectionPool(jit_on_url, min_size=1, max_size=1, open=False, kwargs={"row_factory": dict_row}) as pool:
        with pool.connection() as plain:
            assert _jit(plain) == "on"
            pid = plain.info.backend_pid
        with db.pooled_user_connection(pool, label_harness.user_id) as borrowed:
            assert borrowed.info.backend_pid == pid
            assert _jit(borrowed) == "off"
        with pool.connection() as plain:
            assert plain.info.backend_pid == pid and _jit(plain) == "on"
        with pytest.raises(ValueError, match="fixture rollback"):
            with db.pooled_user_connection(pool, label_harness.user_id) as borrowed:
                assert _jit(borrowed) == "off"
                raise ValueError("fixture rollback")
        with pool.connection() as plain:
            assert plain.info.backend_pid == pid and _jit(plain) == "on"

    # Also check restoration to the actual server default after a normal app borrow.
    url = label_harness.urls["app"]
    pool = db._get_connection_pool(url)
    with pool.connection() as plain:
        default = _jit(plain)
        pid = plain.info.backend_pid
    with db.user_connection(url, label_harness.user_id) as borrowed:
        assert borrowed.info.backend_pid == pid and _jit(borrowed) == "off"
    with pool.connection() as plain:
        assert plain.info.backend_pid == pid and _jit(plain) == default


def test_scheduler_keeps_jit_off_and_each_statement_commits(label_harness, jit_on_url):
    """Guard: make the scheduler connection transactional."""
    h = label_harness
    key = str(uuid4())
    with _autocommit_user_store(jit_on_url, h.user_id) as store:
        conn = store.conn
        assert conn.autocommit
        assert _jit(conn) == "off"
        conn.execute(
            "INSERT INTO memories (user_id, memory_key, value, status, source_event_ids) "
            "VALUES (app.current_user_id(), %s, '{}', 'active', '[]')",
            (key,),
        )
        with db.user_read_snapshot_connection(jit_on_url, h.user_id) as second:
            assert second.info.backend_pid != conn.info.backend_pid
            assert second.execute("SELECT memory_key FROM memories WHERE memory_key = %s", (key,)).fetchone() == {
                "memory_key": key,
            }
        assert _jit(conn) == "off"
    assert conn.closed
    with psycopg.connect(jit_on_url, autocommit=True, row_factory=dict_row) as plain:
        assert _jit(plain) == "on"


def test_raw_workspace_context_uses_transaction_local_jit(label_harness, jit_on_url):
    """Guard: enable JIT in the common tenant setup."""
    user = label_harness.user_id
    with psycopg.connect(jit_on_url, row_factory=dict_row) as conn:
        with conn.transaction():
            ensured = ensure_local_workspace(conn, user_account_id=user)
            assert _jit(conn) == "off"
        with conn.transaction():
            assert _jit(conn) == "on"
            assert get_local_workspace(conn, user_account_id=user) == ensured
            assert _jit(conn) == "off"


def test_rollback_audit_uses_shared_jit_setup(label_harness, jit_on_url, monkeypatch):
    """Guard: enable JIT in the common tenant setup."""
    seen = []
    append = vnext_key_rejection_events._append_once

    def observe(store, notice):
        seen.append(_jit(store.conn))
        append(store, notice)

    monkeypatch.setattr(vnext_key_rejection_events, "_append_once", observe)
    error = RuntimeError("synthetic rolled-back key refusal")
    error.key_rejection = vnext_key_rejection_events.KeyRejection(str(uuid4()), {"reason": "fixture"})
    with pytest.raises(RuntimeError, match="synthetic rolled-back key refusal"):
        with db.user_connection(jit_on_url, label_harness.user_id):
            raise error
    assert seen == ["off"]


def test_eval_store_uses_shared_jit_setup(jit_on_url):
    """Guard: enable JIT in the common tenant setup."""
    with _ephemeral_eval_store(jit_on_url) as store:
        assert _jit(store.conn) == "off"
