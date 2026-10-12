"""Persistent revision-isolated probe for the resume and grounding budgets, allowing interleaved same-data samples.

Run as ``resume_budget_probe.py <checkout> <backend> <database url> <user id> <agent key> <domains json>``, then send one action per
line on standard input: ``resume``, ``resume_changes``, ``resume_query``, ``grounding`` or ``stop``. Each answer is one line of
JSON with the wall and CPU seconds the action took and what it returned (the text of the decision, the number of recent
changes, the names the grounding block lists), so a fast action that returns nothing cannot pass a budget. The checkout is
whichever revision the caller names, and nothing here assumes the newer one: the grounding probe reads the signature of
``compute_query_grounding`` to see whether the revision takes the check of a caller with limits.
"""

import inspect
import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID

NAMES_QUERY = "What do we know about Zorblax and Quuxington?"
SENSITIVITY = ["public", "internal", "unknown"]


def decision_text(brief):
    last = brief.get("last_decision")
    return json.loads(last["canonical_text"]) if last else None


def main():
    repo, backend, location, user, key, domains_json = sys.argv[1:7]
    sys.path[:0] = [str(Path(repo) / "apps/api/src"), repo]
    os.environ["DATABASE_URL"] = location
    os.environ["ALICE_AGENT_API_KEY"] = key
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    os.environ["ALICE_LEGACY_SURFACES"] = "1"
    os.environ["ALICE_PROJECT_SCOPING"] = "off"
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    context = MCPRuntimeContext(database_url=location, user_id=UUID(user))
    domains = json.loads(domains_json)

    def resume(arguments):
        brief = call_mcp_tool(context, name="alice_resume", arguments=arguments)["brief"]
        return {"decision": decision_text(brief), "changes": len(brief["recent_changes"]), "loops": len(brief["open_loops"])}

    def grounding():
        from alicebot_api.vnext_grounding import compute_query_grounding

        parameters = inspect.signature(compute_query_grounding).parameters
        takes_check = "readable_memory_rows" in parameters
        if backend == "postgres":
            from alicebot_api.db import user_connection
            from alicebot_api.vnext_store import PostgresVNextStore

            scope = user_connection(location, user)
            wrap = PostgresVNextStore
        else:
            from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

            scope = sqlite_user_connection(location.removeprefix("sqlite:///"), user)
            wrap = lambda conn: SQLiteVNextStore(conn, user)  # noqa: E731
        with scope as conn:
            store = wrap(conn)
            kwargs = {"domains": domains, "sensitivity_allowed": SENSITIVITY}
            if "allow_entity_lookup" in parameters:
                kwargs["allow_entity_lookup"] = False
            if takes_check:
                from alicebot_api.vnext_label_guard import admit_loaded

                kwargs["readable_memory_rows"] = lambda rows: admit_loaded(
                    store, kind="memory", rows=rows, domains=domains, sensitivity_allowed=SENSITIVITY, projects=()
                )
            result = compute_query_grounding(store, NAMES_QUERY, **kwargs)
        return {"unsupported": (result or {}).get("unsupported_entities", [])}

    actions = {
        "resume": lambda: resume({}),
        "resume_changes": lambda: resume({"max_recent_changes": 20}),
        "resume_query": lambda: resume({"query": "hidden"}),
        "grounding": grounding,
    }
    for line in sys.stdin:
        name = line.strip()
        if name == "stop":
            break
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        result = actions[name]()
        measurement = {"wall": time.perf_counter() - wall_start, "cpu": time.process_time() - cpu_start, **result}
        print(json.dumps(measurement), flush=True)


if __name__ == "__main__":
    main()
