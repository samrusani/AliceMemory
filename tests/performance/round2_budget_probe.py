"""Interpreted by either revision; exercise actual MCP and HTTP readers."""
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import time
from uuid import UUID

repo, backend, location, user, profile, key = sys.argv[1:7]
sys.path[:0] = [str(Path(repo) / "apps/api/src"), repo]
os.environ["DATABASE_URL"] = location
os.environ["ALICE_AGENT_API_KEY"] = key
os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
os.environ["ALICE_LEGACY_SURFACES"] = "1"
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext

context = MCPRuntimeContext(database_url=location, user_id=UUID(user))
arguments = {"query": "synthetic budget observation"}


def pack():
    return call_mcp_tool(context, name="alice_context_pack", arguments=arguments)


def recall():
    return call_mcp_tool(context, name="alice_recall", arguments=arguments)


actions = {"pack": pack, "recall": recall}
if backend == "postgres":
    from alicebot_api.config import Settings
    from alicebot_api import main
    from alicebot_api.routers import workspaces, vnext_memories
    settings = Settings(database_url=location)
    for module in (main, workspaces, vnext_memories):
        module.get_settings = lambda: settings
    # Reuse the synthetic ASGI transport from the current test tree while
    # importing the selected revision's app and stores.
    support = Path(__file__).resolve().parents[1] / "integration/derived_labels_postgres_support.py"
    spec = importlib.util.spec_from_file_location("round2_transport", support)
    transport = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = transport
    spec.loader.exec_module(transport)

    def read(path):
        status, body, _headers = transport.invoke("GET", path, user_id=user, key=key)
        assert status == 200, (status, body)
        return body

    actions.update(workspace=lambda: read("/v0/vnext/workspace"),
                   dogfooding=lambda: read("/v0/vnext/dogfooding"))

results = {}
for name, action in actions.items():
    action()
    wall, cpu = [], []
    for _ in range(5):
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        action()
        wall.append(time.perf_counter() - wall_start)
        cpu.append(time.process_time() - cpu_start)
    results[name] = {"minimum_wall": min(wall), "minimum_cpu": min(cpu),
                     "median_wall": statistics.median(wall), "wall": wall, "cpu": cpu}
    if os.environ.get("ALICE_ROUND2_PROFILE_DIR"):
        import cProfile
        output = Path(os.environ["ALICE_ROUND2_PROFILE_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        profiler = cProfile.Profile()
        profiler.runcall(action)
        profiler.dump_stats(str(output / f"{Path(repo).name}-{profile}-{name}.prof"))
print(json.dumps({"profile": profile, "times": results}))
