"""Persistent subprocess for real MCP and scheduler calls from one revision."""

import json
import os
import sys
from time import perf_counter
from uuid import UUID

import psycopg

from alicebot_api.db import user_connection
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_scheduler import SchedulerRunRequest
from alicebot_api.vnext_scheduler_runtime import run_now_durable
from alicebot_api.vnext_stores.postgres.query_predicates import _MEMORY_PROJECT_SCOPE_SQL
from tests.unit.test_identity_claim_codes import wire


def _plans(database, user, statements, *, scheduler):
    plans = []
    with user_connection(database, user) as conn:
        seen = set()
        for query, params in statements:
            if not isinstance(query, str) or not query.lstrip().upper().startswith(("SELECT", "WITH")):
                continue
            if query in seen:
                continue
            seen.add(query)
            scoped_sweep = scheduler and _MEMORY_PROJECT_SCOPE_SQL in query and "FROM memories" in query
            prefix = "EXPLAIN (ANALYZE, FORMAT JSON) " if scoped_sweep else "EXPLAIN (FORMAT JSON) "
            plan = conn.execute(prefix + query, params).fetchone()["QUERY PLAN"][0]
            plans.append({
                "cost": plan["Plan"]["Total Cost"],
                "jit": "JIT" in plan,
                "compiled_ms": plan.get("JIT", {}).get("Timing", {}).get("Total", 0),
                "scoped_sweep": scoped_sweep,
            })
    return plans


def main():
    database, uid, keys = json.loads(sys.stdin.readline())
    user = UUID(uid)
    context = MCPRuntimeContext(database_url=database, user_id=user)
    execute = psycopg.Cursor.execute
    for line in sys.stdin:
        name, profile, arguments, mode = json.loads(line)
        scheduler = name == "staleness_sweep"
        os.environ["ALICE_AGENT_API_KEY"] = keys[profile]
        statements = []
        sweep_jit = []

        def record(cursor, query, params=None, **kwargs):
            statements.append((query, params))
            if scheduler and mode == "plan" and isinstance(query, str):
                if "FROM memories" in query and _MEMORY_PROJECT_SCOPE_SQL in query:
                    with cursor.connection.cursor() as probe:
                        execute(probe, "SHOW jit")
                        sweep_jit.append(probe.fetchone()["jit"])
            return execute(cursor, query, params, **kwargs)

        if mode:
            psycopg.Cursor.execute = record
        try:
            started = perf_counter()
            if scheduler:
                result = run_now_durable(
                    database_url=database, user_id=user, request=SchedulerRunRequest(**arguments),
                )
                ok = result["run"]["status"] == "succeeded"
            else:
                failed, result = wire(context, name, arguments)
                ok = not failed
            elapsed_ms = (perf_counter() - started) * 1000
        finally:
            psycopg.Cursor.execute = execute
        plans = _plans(database, user, statements, scheduler=scheduler) if mode == "plan" else []
        print(json.dumps({
            "ok": ok, "error": None if ok else result, "ms": elapsed_ms,
            "statement_count": len(statements), "plans": plans, "sweep_jit": sweep_jit,
        }, default=str), flush=True)


if __name__ == "__main__":
    main()
