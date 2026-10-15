"""Time one vault's workspace, recall, context pack and memory count. One action name per stdin line."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID


def main() -> None:
    repo, location, user, key = sys.argv[1:5]
    sys.path[:0] = [str(Path(repo) / "apps" / "api" / "src"), repo]
    os.environ["DATABASE_URL"] = location
    os.environ["ALICE_AGENT_API_KEY"] = key
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    os.environ["ALICE_LEGACY_SURFACES"] = "1"
    from alicebot_api.config import Settings, get_settings
    from alicebot_api.db import user_connection
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.routers import workspaces
    from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
    from alicebot_api.vnext_store import PostgresVNextStore

    get_settings.cache_clear()
    settings = Settings(database_url=location)
    workspaces.get_settings = lambda: settings
    try:
        from alicebot_api.vnext_label_closure import weekly_parent_query_count
    except ImportError:
        weekly_parent_query_count = None
    context = MCPRuntimeContext(database_url=location, user_id=UUID(user))
    user_id = UUID(user)
    query = os.environ.get("ALICE_WEEKLY_BUDGET_QUERY", "weeklynesttoken")

    def recall():
        return call_mcp_tool(context, name="alice_recall", arguments={"query": query, "limit": 8})

    def pack():
        return call_mcp_tool(context, name="alice_context_pack", arguments={"query": query})

    def workspace():
        response = workspaces.get_vnext_workspace(user_id, authorization="Bearer " + key)
        if response.status_code != 200:
            raise RuntimeError("workspace status " + str(response.status_code))
        return response

    def count():
        with user_connection(location, user_id) as conn:
            store = PostgresVNextStore(conn)
            with label_read_scope(store):
                guard = LabelGuard.for_filters(store, (), ("public", "internal", "private", "unknown"), ())
                counts = guard.readable_status_counts("memory")
                queries = weekly_parent_query_count() if weekly_parent_query_count is not None else None
        return {"counts": counts, "queries": queries}

    actions = {"recall": recall, "pack": pack, "workspace": workspace, "count": count}
    for line in sys.stdin:
        name = line.strip()
        if name == "stop":
            break
        started = time.perf_counter()
        result = actions[name]()
        measurement = {"wall": time.perf_counter() - started}
        if name == "recall":
            measurement["memories"] = len(result["results"])
        elif name == "count":
            measurement["queries"] = result["queries"]
        print(json.dumps(measurement), flush=True)


if __name__ == "__main__":
    main()
