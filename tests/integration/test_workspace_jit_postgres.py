"""The workspace uses the shared PostgreSQL JIT setting."""
from __future__ import annotations

import psycopg

from alicebot_api.routers import workspaces

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def current_jit(h):
    with psycopg.connect(h.urls["app"], autocommit=True) as conn:
        return conn.execute("SHOW jit").fetchone()[0]


def test_workspace_transaction_turns_jit_off_and_leaves_the_next_request_alone(label_harness, monkeypatch):
    """Mutation: enable JIT in the shared user setup."""
    h = label_harness
    default = current_jit(h)
    seen = []

    def payload(store, *, identity=None):
        seen.append(store.conn.execute("SHOW jit").fetchone()["jit"])
        return {"mode": "live"}

    monkeypatch.setattr(workspaces, "_vnext_workspace_payload", payload)
    status, body, _headers = h.request("GET", "/v0/vnext/workspace")
    assert (status, body) == (200, {"mode": "live"})
    assert seen == ["off"]
    assert current_jit(h) == default, "the setting must end with the workspace transaction"
    for _ in range(3):
        h.request("GET", "/v0/vnext/workspace")
        assert current_jit(h) == default
    assert seen == ["off"] * 4
