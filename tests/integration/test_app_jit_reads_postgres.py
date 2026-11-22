"""Check current PostgreSQL query plans through real MCP and scheduler calls."""

from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_scheduler import SchedulerRunRequest
from alicebot_api.vnext_scheduler_runtime import run_now_durable
from alicebot_api.vnext_stores.postgres.query_predicates import _MEMORY_PROJECT_SCOPE_SQL
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.test_app_jit_postgres import jit_on_url  # noqa: F401
from tests.unit.test_identity_claim_codes import wire

QUERY = "synthetic calibration"
PLANNABLE_COMMANDS = {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "VALUES", "TABLE"}


@contextmanager
def _capture_plans(monkeypatch):
    plans = []
    execute = psycopg.Cursor.execute

    def record(cursor, query, params=None, **kwargs):
        statement = query.as_string(cursor.connection) if isinstance(query, sql.Composable) else query
        if isinstance(statement, bytes):
            statement = statement.decode(cursor.connection.info.encoding)
        command = statement.lstrip().split(maxsplit=1)[:1]
        if command and command[0].upper() in PLANNABLE_COMMANDS:
            # Plan on the connection about to run the statement, with its own
            # transaction or session settings. Leave execution to the real call.
            with cursor.connection.cursor(row_factory=dict_row) as probe:
                execute(probe, "EXPLAIN (FORMAT JSON) " + statement, params)
                plan = probe.fetchone()["QUERY PLAN"][0]
            plans.append((statement, plan))
        return execute(cursor, query, params, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(psycopg.Cursor, "execute", record)
        yield plans


def _jit_threshold(jit_on_url):
    with psycopg.connect(jit_on_url, autocommit=True) as conn:
        if not conn.execute("SELECT pg_jit_available()").fetchone()[0]:
            pytest.skip("PostgreSQL does not have a JIT provider")
        threshold = float(conn.execute("SHOW jit_above_cost").fetchone()[0])
        if threshold < 0:
            pytest.skip("PostgreSQL disables compilation at every plan cost")
    return threshold


def _analyze(harness):
    with psycopg.connect(harness.urls["admin"], autocommit=True) as conn:
        conn.execute("ANALYZE memories, memory_revisions, sources, event_log, generated_artifacts, open_loops")


def _seed_published_vault(harness, monkeypatch):
    key = harness.key("admin_agent", project="alpha")
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    for index in range(300):
        fields = {"sensitivity": "private", "domain": "project"}
        if index < 250:
            fields.update(memory_type="semantic", title=f"Synthetic calibration {index}",
                          canonical_text=f"Synthetic calibration {index} tracks observations in an isolated vault.")
        else:
            fields["lesson"] = {
                "applies_when": f"Checking synthetic calibration {index}",
                "what_happened": "The reading drifted.",
                "do_instead": "Reset the fixture and check it again.",
                "paths": ["fixtures/sensor.py"],
            }
        failed, result = wire(context, "alice_memory_commit", fields)
        assert not failed and result["status"] == "committed", result


def test_bound_application_reads_avoid_jit(label_harness, jit_on_url, monkeypatch, tmp_path):
    """Guard: enable JIT in the shared setup and a bound read plans compilation."""
    h = label_harness
    threshold = _jit_threshold(jit_on_url)
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    for env in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    _seed_published_vault(h, monkeypatch)
    _analyze(h)
    key = h.key("project_scoped_agent", project="alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=jit_on_url, user_id=h.user_id)
    for tool in ("alice_recall", "alice_resume", "alice_context_pack", "alice_lessons"):
        arguments = {"task": QUERY, "limit": 3} if tool == "alice_lessons" else {"query": QUERY}
        with _capture_plans(monkeypatch) as plans:
            failed, result = wire(context, tool, arguments)
        assert not failed, result
        assert plans, tool
        if tool == "alice_recall":
            cost = max(plan["Plan"]["Total Cost"] for _, plan in plans)
            assert cost > threshold, (cost, threshold)
        assert not any("JIT" in plan for _, plan in plans), tool


def test_project_scoped_scheduler_sweep_avoids_jit(label_harness, jit_on_url, monkeypatch):
    """Guard: enable JIT in the shared setup and the scheduler plans compilation."""
    h = label_harness
    threshold = _jit_threshold(jit_on_url)
    with h.store() as store:
        store.conn.execute(
            """
            INSERT INTO memories (user_id, memory_key, value, status, source_event_ids,
                                  memory_type, canonical_text, domain, sensitivity, metadata_json)
            SELECT app.current_user_id(), 'sweep-fixture.' || n, '{}', 'active', '[]',
                   'project_state', 'Synthetic task state', 'project', 'internal', %s
            FROM generate_series(1, 1200) AS n
            """,
            (Jsonb({"project_scope": ["alpha"]}),),
        )
    _analyze(h)
    request = SchedulerRunRequest(workflow_type="staleness_sweep", projects=["alpha"],
                                  domains=["project"], sensitivity_allowed=["internal"])
    with _capture_plans(monkeypatch) as plans:
        result = run_now_durable(database_url=jit_on_url, user_id=h.user_id, request=request)
    assert result["run"]["status"] == "succeeded", result
    scoped_costs = [plan["Plan"]["Total Cost"] for statement, plan in plans
                    if "FROM memories" in statement and _MEMORY_PROJECT_SCOPE_SQL in statement]
    assert any(cost > threshold for cost in scoped_costs), (scoped_costs, threshold)
    assert not any("JIT" in plan for _, plan in plans)
