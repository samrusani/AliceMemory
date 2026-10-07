"""Persistent revision-isolated probe, allowing interleaved same-data samples."""
import importlib.util
import cProfile
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

repo, backend, location, user, profile, key = sys.argv[1:7]
sys.path[:0] = [str(Path(repo) / "apps/api/src"), repo]
os.environ["DATABASE_URL"] = location
if key:
    os.environ["ALICE_AGENT_API_KEY"] = key
else:
    os.environ.pop("ALICE_AGENT_API_KEY", None)
os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
os.environ["ALICE_LEGACY_SURFACES"] = "1"
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext

context = MCPRuntimeContext(database_url=location, user_id=UUID(user))
arguments = {"query": "synthetic budget observation"}
if profile == "keyless_agent_id":
    arguments["agent_id"] = "budget-keyless"
actions = {"pack": lambda: call_mcp_tool(context, name="alice_context_pack", arguments=arguments),
           "recall": lambda: call_mcp_tool(context, name="alice_recall", arguments=arguments)}
if backend == "postgres":
    from alicebot_api.config import Settings
    from alicebot_api import main
    from alicebot_api.routers import workspaces, vnext_memories
    settings = Settings(database_url=location)
    for module in (main, workspaces, vnext_memories):
        module.get_settings = lambda: settings
    support = Path(__file__).resolve().parents[1] / "integration/derived_labels_postgres_support.py"
    spec = importlib.util.spec_from_file_location("round3_transport", support)
    transport = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = transport
    spec.loader.exec_module(transport)

    def read(path):
        status, body, _headers = transport.invoke("GET", path, user_id=user, key=key)
        assert status == 200, (status, body)
        return body

    actions.update(workspace=lambda: read("/v0/vnext/workspace"), dogfooding=lambda: read("/v0/vnext/dogfooding"))

profiled = set()
for line in sys.stdin:
    name = line.strip()
    if name == "stop":
        break
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    trace = os.environ.get("ALICE_READ_PROFILE")
    profiler = cProfile.Profile() if trace and name not in profiled else None
    if profiler:
        profiler.enable()
    result = actions[name]()
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(Path(trace) / (Path(repo).name + "-" + profile + "-" + name + ".prof")))
        profiled.add(name)
    measurement = {"wall": time.perf_counter() - wall_start, "cpu": time.process_time() - cpu_start}
    if name == "recall":
        measurement["memories"] = len(result["results"])
    elif name == "pack":
        measurement["memories"] = len(result["memories"])
    print(json.dumps(measurement), flush=True)
