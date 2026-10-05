"""Strict lock checks read database grants rather than cached state."""
from types import SimpleNamespace

import pytest

from alicebot_api import vnext_label_writes as writes
from alicebot_api.vnext_store import PostgresVNextStore


class Cursor:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def execute(self, query, params=()):
        self.conn.queries.append(query)
        if "pg_advisory_xact_lock_shared" in query:
            self.conn.grants["labels"] = True
        elif "pg_advisory_xact_lock(" in query:
            self.conn.grants["graph"] = True
    def fetchone(self):
        return dict(self.conn.grants)


def _store():
    conn = SimpleNamespace(grants={"graph": False, "labels": False, "exclusive": False}, queries=[])
    conn.cursor = lambda: Cursor(conn)
    return PostgresVNextStore(conn)


def test_strict_s_after_l_is_refused(monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _store()
    store.lock_label_writes()
    with pytest.raises(writes.LabelLockOrderError, match="graph lock must precede"):
        store.lock_graph_mutation()
    assert any("FROM pg_locks" in query for query in store.conn.queries)


def test_a_savepoint_rollback_cannot_leave_a_stale_lock_memo(monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _store()
    store.lock_graph_mutation()
    store.lock_label_writes()
    store.conn.grants.update(graph=False, labels=False, exclusive=False)
    store.lock_graph_mutation()
    store.conn.grants.update(graph=False, labels=True)
    with pytest.raises(writes.LabelLockOrderError):
        store.lock_graph_mutation()
    assert sum("FROM pg_locks" in query for query in store.conn.queries) == 3


def test_strict_changing_hook_requires_exclusive_l(monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _store()
    before = {"domain": "project", "sensitivity": "public"}
    with pytest.raises(writes.LabelLockOrderError, match="exclusive label lock"):
        writes.prepare_label_patch(store, "memory", before, {"sensitivity": "confidential"})
    store.conn.grants["exclusive"] = True
    assert writes.prepare_label_patch(store, "memory", before, {"sensitivity": "confidential"}) == {"sensitivity": "confidential"}


def test_propagation_locks_tables_and_rows_in_order():
    store = _store()
    statements = []
    class RowsCursor(Cursor):
        def execute(self, query, params=()):
            statements.append((query, params))
            self.ids = params[0]
        def fetchall(self):
            return [{"id": row_id} for row_id in self.ids]
    store.conn.cursor = lambda: RowsCursor(store.conn)
    changes = [{"kind": kind, "id": row_id} for kind, row_id in [("memory", "b"), ("artifact", "c"), ("open_loop", "d"), ("project", "e"), ("memory", "a")]]
    writes.lock_settled_label_rows(store, changes)
    assert [query.split("FROM ")[1].split()[0] for query, _params in statements] == list(writes.LABEL_TABLE_ORDER)
    assert all("ORDER BY id FOR UPDATE" in query for query, _params in statements)
    assert statements[-1][1] == (["a", "b"],)


def test_compare_and_set_miss_refuses_the_whole_label_write():
    from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError
    store = SimpleNamespace(_fetch_optional_one=lambda *_: None)
    with pytest.raises(DerivedDomainRepairError, match="changed no row"):
        writes.write_settled_label(store, kind="memory", row_id="missing", domain="health", sensitivity="confidential", metadata={}, project_id=None, expected_domain="project", expected_sensitivity="public")


def test_strict_hook_checks_the_legacy_project_pointer_before_any_update(monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _store()
    before = {"domain": "project", "sensitivity": "public", "project_id": "11111111-1111-4111-8111-111111111111"}
    with pytest.raises(writes.LabelLockOrderError, match="exclusive label lock"):
        writes.prepare_label_patch(store, "memory", before, {"project_id": "22222222-2222-4222-8222-222222222222"})


def test_named_refusal_causes_never_disclose_error_content():
    from psycopg.errors import DivisionByZero, LockNotAvailable
    from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError
    for error, cause in ((writes.LabelPropagationTooLarge("synthetic-private-text"), "propagation_bound"), (DerivedDomainRepairError("changed no row: synthetic-private-text"), "row_changed"), (DerivedDomainRepairError("cycle: synthetic-private-text"), "dependency_cycle"), (writes.LabelLockOrderError("synthetic-private-text"), "lock_order"), (DivisionByZero("synthetic-private-text"), "database_error")):
        status, detail, retry_after = writes.label_error_response(error)
        assert status == 409 and detail.endswith("cause: " + cause) and retry_after is None
        assert "synthetic-private-text" not in detail
    assert writes.label_error_response(LockNotAvailable("synthetic-private-text")) == (503, writes.RETRYABLE_DETAIL, "2")
