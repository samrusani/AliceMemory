"""Every scheduler workflow keeps a bound key's input admission."""
import json

import pytest

from tests.integration.test_derived_labels_producers_postgres import seed_grid, wire_database
from tests.integration.test_memory_mutations_api import invoke_request


@pytest.mark.parametrize("workflow", ["daily_brief", "weekly_synthesis", "connection_report", "contradiction_report", "memory_consolidation", "staleness_sweep", "open_loop_review", "project_update_scan"])
@pytest.mark.parametrize("scoped", [True, False])
def test_scheduler_never_drops_the_bound_identity(migrated_database_urls, monkeypatch, workflow, scoped):
    app_url = migrated_database_urls["app"]
    wire_database(monkeypatch, app_url)
    user, alpha, beta, rows, key, _, _ = seed_grid(app_url)
    status, body = invoke_request("POST", f"/v0/vnext/scheduler/workflows/{workflow}/run-now",
        payload={"user_id": str(user), "scope": {"projects": [alpha]} if scoped else {},
                 "options": {"generated_for": "2026-10-05", "reference_time": "2026-10-05T12:00:00Z", "source_limit": 50, "memory_limit": 50, "max_items": 50}},
        headers={"authorization": f"Bearer {key}"})
    assert status == 201, body
    text = json.dumps(body, default=str)
    for label, _, row in rows:
        if label != "alpha":
            assert str(row["id"]) not in text
            assert f"SENTINEL_{label.upper()}" not in text


@pytest.mark.parametrize("workflow", ["connection_report", "contradiction_report"])
def test_mcp_generation_adapter_keeps_a_resolved_bound_identity(migrated_database_urls, monkeypatch, workflow):
    from alicebot_api.db import user_connection
    from alicebot_api.vnext_store import PostgresVNextStore
    from alicebot_api.vnext_agent_keys import resolve_agent_identity
    from alicebot_api.mcp.capture_automation import _handle_alice_vnext_generate_artifact
    from alicebot_api.mcp.types import MCPRuntimeContext
    url = migrated_database_urls["app"]
    wire_database(monkeypatch, url)
    user, alpha, beta, rows, key, _, _ = seed_grid(url)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    with user_connection(url, user) as conn:
        identity = resolve_agent_identity(PostgresVNextStore(conn), user_id=user, raw_key=key, payload={})
    context = MCPRuntimeContext(database_url=url, user_id=user, agent_identity=identity, agent_identity_resolved=True)
    response = _handle_alice_vnext_generate_artifact(context, arguments={
        "workflow_type": workflow, "query": "Atlas",
        "project_scope": [alpha]})
    text = json.dumps(response, default=str)
    for label, _, row in rows:
        if label != "alpha":
            assert str(row["id"]) not in text
            assert f"SENTINEL_{label.upper()}" not in text
