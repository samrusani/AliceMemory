"""The workspace transaction runs without PostgreSQL JIT compilation.

The project dashboards read artifacts through a long scope expression. PostgreSQL
estimates that expression above its JIT threshold, so every such read spent about
a quarter of a second compiling a query that runs in two milliseconds. On a vault
with two projects that was most of a 0.6 second page. The setting is local to the
workspace transaction and must never reach another request.
"""
from __future__ import annotations

from alicebot_api.routers import workspaces

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def current_jit(h):
    with h.store() as store:
        return store.conn.execute("SHOW jit").fetchone()["jit"]


def test_workspace_transaction_turns_jit_off_and_leaves_the_next_request_alone(label_harness, monkeypatch):
    """Mutation: drop the SET LOCAL from the route."""
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
